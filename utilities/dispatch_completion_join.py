#!/usr/bin/env python3
"""Runtime-owned exact-batch join for registered headless children.

The joiner never returns child output.  It snapshots attempts bound to one
``parent_attempt_id``, waits until every attempt is either closed or requires
typed harvest, and emits one bounded JSON receipt for the session supervisor.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import sys
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    close_attempt_row,
    observed_attempt_liveness,
)
from codex_dispatch_terminal import (  # noqa: E402
    inspect_terminal_attempt,
    terminal_envelope_observed,
)
OPEN_STATES = frozenset({"open", "running"})
SCHEMA_VERSION = 2
STATE_SCHEMA_VERSION = 1
MAX_STATE_BYTES = 16384
MAX_BATCH_ATTEMPTS = 4
SESSION_PARENT_DELIVERY = "codex-stop-hook"
MANAGED_SESSION_PARENT_DELIVERY = "codex-managed-gateway"
SESSION_PARENT_DELIVERIES = frozenset(
    {SESSION_PARENT_DELIVERY, MANAGED_SESSION_PARENT_DELIVERY}
)
SUCCESS_NOTES = frozenset({"completed-marker", "completed-supervisor"})


class JoinContractError(RuntimeError):
    """A registry or liveness boundary could not be proved."""


def required_action_for_attempt(status: str, metadata: dict[str, str]) -> str:
    """Return the one typed follow-up that the exact registry row permits."""

    if status in OPEN_STATES:
        return "complete-open"
    if status != "done":
        raise JoinContractError("owned-row-status-invalid")
    if metadata.get("failure_class") == "pass" or metadata.get("note") in SUCCESS_NOTES:
        return "advance-completed"
    return "inspect-done-failure"


@dataclass(frozen=True)
class ChildRow:
    order: int
    status: str
    slug: str
    attempt_id: str
    raw: str
    metadata: dict[str, str]


@dataclass(frozen=True)
class ParentSessionState:
    attempt_ids: frozenset[str]
    delivered_attempt_ids: frozenset[str]


@dataclass(frozen=True)
class SupervisorShellAction:
    kind: str
    attempt_id: str = ""


@dataclass(frozen=True)
class SupervisedDispatchContext:
    """Immutable owner boundary used while a supervised batch is parked."""

    jobs: Path
    route_file: Path
    route_id: str
    parent_attempt_id: str
    route: dict[str, object]
    rows: tuple[ChildRow, ...]


def _safe_identity(value: str) -> bool:
    return (
        0 < len(value.encode("utf-8")) <= 256
        and "," not in value
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def write_supervisor_state(
    path: Path | None,
    parent_attempt_id: str,
    delivered_attempt_ids: set[str],
) -> None:
    """Atomically publish the bounded phase state consumed by native hooks."""

    if path is None:
        return
    if (
        not path.is_absolute()
        or not _safe_identity(parent_attempt_id)
        or any(not _safe_identity(attempt) for attempt in delivered_attempt_ids)
    ):
        raise JoinContractError("supervisor-state-contract-invalid")
    value = {
        "schema_version": STATE_SCHEMA_VERSION,
        "parent_attempt_id": parent_attempt_id,
        "delivered_attempt_ids": sorted(delivered_attempt_ids),
    }
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise JoinContractError("supervisor-state-oversized")
    temporary: Path | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=".supervisor-state.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise JoinContractError("supervisor-state-unwritable") from exc


def read_supervisor_state(
    path: Path | None,
    parent_attempt_id: str,
) -> set[str] | None:
    """Return delivered attempts, or None for missing/invalid state."""

    if path is None or not path.is_absolute() or not _safe_identity(parent_attempt_id):
        return None
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != STATE_SCHEMA_VERSION
        or value.get("parent_attempt_id") != parent_attempt_id
    ):
        return None
    raw = value.get("delivered_attempt_ids")
    if not isinstance(raw, list) or len(raw) > 64:
        return None
    delivered: set[str] = set()
    for attempt in raw:
        if not isinstance(attempt, str) or not _safe_identity(attempt) or attempt in delivered:
            return None
        delivered.add(attempt)
    return delivered


def remove_supervisor_state(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # A unique attempt path cannot wake a later owner. Reconciliation may
        # remove an unreadable leftover after the process exits.
        pass


def parent_session_state_path(jobs: Path, parent_session_id: str) -> Path:
    """Return a non-identifying, registry-scoped phase-state path."""

    if not jobs.is_absolute() or not _safe_identity(parent_session_id):
        raise JoinContractError("parent-session-state-contract-invalid")
    digest = hashlib.sha256(parent_session_id.encode("utf-8")).hexdigest()
    return jobs.resolve(strict=False).parent / "parent-session-state" / f"{digest}.json"


@contextmanager
def _parent_session_state_lock(path: Path):
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = path.with_name(f"{path.name}.lock")
        with lock_path.open("a+", encoding="utf-8") as handle:
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise JoinContractError("parent-session-state-lock-unavailable") from exc


def _write_parent_session_state_unlocked(
    path: Path,
    parent_session_id: str,
    delivered_attempt_ids: set[str],
    attempt_ids: set[str],
) -> None:
    if (
        not path.is_absolute()
        or not _safe_identity(parent_session_id)
        or not attempt_ids
        or len(attempt_ids) > MAX_BATCH_ATTEMPTS
        or len(delivered_attempt_ids) > MAX_BATCH_ATTEMPTS
        or not delivered_attempt_ids.issubset(attempt_ids)
        or any(not _safe_identity(attempt) for attempt in attempt_ids)
        or any(not _safe_identity(attempt) for attempt in delivered_attempt_ids)
    ):
        raise JoinContractError("parent-session-state-contract-invalid")
    value = {
        "schema_version": STATE_SCHEMA_VERSION,
        "parent_session_id_sha256": hashlib.sha256(
            parent_session_id.encode("utf-8")
        ).hexdigest(),
        "attempt_ids": sorted(attempt_ids),
        "delivered_attempt_ids": sorted(delivered_attempt_ids),
        "phase": (
            "delivered"
            if delivered_attempt_ids == attempt_ids
            else "pending"
        ),
    }
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise JoinContractError("parent-session-state-oversized")
    temporary: Path | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=".parent-session-state.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise JoinContractError("parent-session-state-unwritable") from exc


def write_parent_session_state(
    path: Path,
    parent_session_id: str,
    delivered_attempt_ids: set[str],
    *,
    attempt_ids: set[str] | None = None,
) -> None:
    """Atomically publish one pending or delivered interactive Stop batch."""

    registered = set(delivered_attempt_ids if attempt_ids is None else attempt_ids)
    if not registered:
        raise JoinContractError("parent-session-state-contract-invalid")
    with _parent_session_state_lock(path):
        _write_parent_session_state_unlocked(
            path,
            parent_session_id,
            set(delivered_attempt_ids),
            registered,
        )


def _read_parent_session_state_unlocked(
    path: Path,
    parent_session_id: str,
) -> ParentSessionState | None:
    if not path.is_absolute() or not _safe_identity(parent_session_id):
        return None
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    expected_digest = hashlib.sha256(parent_session_id.encode("utf-8")).hexdigest()
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != STATE_SCHEMA_VERSION
        or value.get("parent_session_id_sha256") != expected_digest
    ):
        return None
    raw_delivered = value.get("delivered_attempt_ids")
    raw_attempts = value.get("attempt_ids", raw_delivered)
    if (
        not isinstance(raw_attempts, list)
        or not isinstance(raw_delivered, list)
        or not raw_attempts
        or len(raw_attempts) > MAX_BATCH_ATTEMPTS
        or len(raw_delivered) > MAX_BATCH_ATTEMPTS
    ):
        return None
    parsed: list[set[str]] = []
    for raw in (raw_attempts, raw_delivered):
        values: set[str] = set()
        for attempt in raw:
            if (
                not isinstance(attempt, str)
                or not _safe_identity(attempt)
                or attempt in values
            ):
                return None
            values.add(attempt)
        parsed.append(values)
    attempts, delivered = parsed
    expected_phase = (
        "delivered"
        if delivered == attempts
        else "pending"
        if not delivered
        else "invalid"
    )
    if (
        not delivered.issubset(attempts)
        or value.get("phase", expected_phase) != expected_phase
    ):
        return None
    return ParentSessionState(frozenset(attempts), frozenset(delivered))


def read_parent_session_batch_state(
    path: Path,
    parent_session_id: str,
) -> ParentSessionState | None:
    """Return the exact registered and delivered sets for one native Stop batch."""

    return _read_parent_session_state_unlocked(path, parent_session_id)


def read_parent_session_state(
    path: Path,
    parent_session_id: str,
) -> set[str] | None:
    """Return delivered attempts, or None for missing/foreign/invalid state."""

    state = read_parent_session_batch_state(path, parent_session_id)
    return set(state.delivered_attempt_ids) if state is not None else None


def register_parent_session_attempt(
    path: Path,
    parent_session_id: str,
    attempt_id: str,
) -> None:
    """Bind a newly spawned direct child before the parent reaches Stop."""

    if not _safe_identity(attempt_id):
        raise JoinContractError("parent-session-state-contract-invalid")
    with _parent_session_state_lock(path):
        state = _read_parent_session_state_unlocked(path, parent_session_id)
        if state is not None and not state.delivered_attempt_ids:
            attempts = set(state.attempt_ids)
            attempts.add(attempt_id)
        else:
            attempts = {attempt_id}
        _write_parent_session_state_unlocked(
            path,
            parent_session_id,
            set(),
            attempts,
        )


def consume_parent_session_attempt(
    path: Path,
    parent_session_id: str,
    attempt_id: str,
    *,
    before_consume: Callable[[], bool] | None = None,
    allow_pending: bool = False,
) -> bool:
    """Consume one exact receipt after a successful typed harvest.

    ``allow_pending`` exists only for migration from the retired native Stop
    bridge. Its caller must already have proved the exact attempt terminal.
    """

    if not _safe_identity(attempt_id):
        return False
    with _parent_session_state_lock(path):
        state = _read_parent_session_state_unlocked(path, parent_session_id)
        if state is None or attempt_id not in state.attempt_ids:
            return False
        if not allow_pending and attempt_id not in state.delivered_attempt_ids:
            return False
        if before_consume is not None and not before_consume():
            raise JoinContractError("parent-session-consume-commit-failed")
        attempts = set(state.attempt_ids)
        delivered = set(state.delivered_attempt_ids)
        attempts.remove(attempt_id)
        delivered.discard(attempt_id)
        if attempts:
            _write_parent_session_state_unlocked(
                path,
                parent_session_id,
                delivered,
                attempts,
            )
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise JoinContractError("parent-session-state-unwritable") from exc
        return True


def remove_parent_session_state(path: Path | None) -> None:
    remove_supervisor_state(path)


def _local_contract_path(base: Path, raw: str, relative: str) -> bool:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    roots = [ROOT]
    agent_home = os.environ.get("AGENT_HOME")
    if agent_home and (Path(agent_home) / "core" / "CORE.md").is_file():
        roots.append(Path(agent_home))
    resolved_base = base.resolve()
    for parent in (resolved_base, *resolved_base.parents):
        if (parent / "core" / "CORE.md").is_file():
            roots.append(parent)
            break
    return any(resolved == (root / relative).resolve() for root in roots)


def _parse_long_options(
    tokens: list[str],
    valued: set[str],
    switches: set[str],
) -> dict[str, list[str]] | None:
    parsed: dict[str, list[str]] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in switches:
            parsed.setdefault(token, []).append("1")
            index += 1
            continue
        if token.startswith("--") and "=" in token:
            option, option_value = token.split("=", 1)
            if option not in valued or not option_value:
                return None
            parsed.setdefault(option, []).append(option_value)
            index += 1
            continue
        if token not in valued or index + 1 >= len(tokens):
            return None
        parsed.setdefault(token, []).append(tokens[index + 1])
        index += 2
    return parsed


def _resolved_from(base: Path, raw: str) -> Path | None:
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        return candidate.resolve()
    except OSError:
        return None


def _selected_long_options(
    tokens: list[str], selected: set[str]
) -> dict[str, list[str]] | None:
    """Read selected opaque adapter options without accepting missing values."""

    values: dict[str, list[str]] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        matched = next(
            (option for option in selected if token.startswith(option + "=")),
            None,
        )
        if matched is not None:
            value = token[len(matched) + 1 :]
            if not value:
                return None
            values.setdefault(matched, []).append(value)
            index += 1
            continue
        if token in selected:
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                return None
            values.setdefault(token, []).append(tokens[index + 1])
            index += 2
            continue
        index += 1
    return values


def _strict_supervisor_binding_requested(
    *,
    jobs: Path | None,
    parent_attempt_id: str,
    route_file: Path | None,
    route_id: str,
) -> bool:
    return bool(
        jobs
        or parent_attempt_id
        or route_file
        or route_id
        or os.environ.get("AGENT_DISPATCH_COMPLETION_MODE") == "supervised"
    )


def _supervised_dispatch_context(
    *,
    jobs: Path | None,
    parent_attempt_id: str,
    route_file: Path | None,
    route_id: str,
    open_attempt_ids: set[str],
) -> SupervisedDispatchContext:
    """Resolve the exact owner route/registry tuple or fail closed."""

    raw_jobs = jobs or (
        Path(os.environ["AGENT_DISPATCH_JOBS"])
        if os.environ.get("AGENT_DISPATCH_JOBS")
        else None
    )
    raw_route = route_file or (
        Path(os.environ["AGENT_ROUTE_FILE"])
        if os.environ.get("AGENT_ROUTE_FILE")
        else None
    )
    expected_parent = parent_attempt_id or os.environ.get(
        "AGENT_DISPATCH_ATTEMPT_ID", ""
    )
    expected_route_id = route_id or os.environ.get("AGENT_ROUTE_ID", "")
    if (
        raw_jobs is None
        or raw_route is None
        or not raw_jobs.is_absolute()
        or not raw_route.is_absolute()
        or not expected_parent
        or not expected_route_id
    ):
        raise JoinContractError("supervisor-dispatch-binding-missing")
    try:
        canonical_jobs = raw_jobs.resolve()
        canonical_route = raw_route.resolve()
        route = json.loads(canonical_route.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JoinContractError("supervisor-dispatch-binding-unreadable") from exc
    if not isinstance(route, dict) or route.get("route_id") != expected_route_id:
        raise JoinContractError("supervisor-route-id-mismatch")
    rows = current_children(canonical_jobs, expected_parent)
    indexed = {row.attempt_id: row for row in rows}
    if not open_attempt_ids or not open_attempt_ids.issubset(indexed):
        raise JoinContractError("supervisor-open-attempt-binding-mismatch")
    return SupervisedDispatchContext(
        jobs=canonical_jobs,
        route_file=canonical_route,
        route_id=expected_route_id,
        parent_attempt_id=expected_parent,
        route=route,
        rows=tuple(rows),
    )


def _command_paths_match(
    *,
    base: Path,
    route_values: list[str],
    jobs_values: list[str],
    context: SupervisedDispatchContext,
) -> bool:
    return (
        len(route_values) == 1
        and len(jobs_values) == 1
        and _resolved_from(base, route_values[0]) == context.route_file
        and _resolved_from(base, jobs_values[0]) == context.jobs
    )


def _row_matches_current_route(
    row: ChildRow, context: SupervisedDispatchContext
) -> bool:
    route_path = _resolved_from(
        Path(row.metadata.get("worktree", "/")),
        row.metadata.get("route_file", ""),
    )
    return (
        row.metadata.get("parent_attempt_id") == context.parent_attempt_id
        and row.metadata.get("route_id") == context.route_id
        and route_path == context.route_file
    )


def _declared_parallel_nodes(
    route: dict[str, object], group: str
) -> tuple[dict[str, dict[str, object]], bool] | None:
    """Return a bounded group and whether it uses the canonical v2 axis."""

    raw_nodes = route.get("nodes")
    if not isinstance(raw_nodes, list):
        return None
    nodes = {
        str(node.get("id")): node
        for node in raw_nodes
        if isinstance(node, dict)
        and (node.get("parallel_group") or node.get("replica_group")) == group
    }
    if (
        not 2 <= len(nodes) <= 4
        or "" in nodes
        or any(node.get("dispatch_depth") != 2 for node in nodes.values())
    ):
        return None
    canonical = any(node.get("parallel_group") == group for node in nodes.values())
    if canonical and any(node.get("parallel_group") != group for node in nodes.values()):
        return None
    return nodes, canonical


def _parallel_row_matches(
    row: ChildRow,
    *,
    context: SupervisedDispatchContext,
    group: str,
    node_ids: set[str],
    declared_size: int,
    canonical: bool,
) -> bool:
    metadata = row.metadata
    node = metadata.get("route_node", "")
    row_group = metadata.get("parallel_group") or metadata.get("replica_group")
    expected_kind = "parallel-batch" if canonical else "replica-batch"
    return (
        _row_matches_current_route(row, context)
        and node in node_ids
        and row_group == group
        and (not canonical or metadata.get("parallel_group") == group)
        and metadata.get("reservation_kind") == expected_kind
        and metadata.get("batch_declared_size") == str(declared_size)
        and metadata.get("batch_group") == group
        and metadata.get("batch_route_id") == context.route_id
        and metadata.get("batch_parent_attempt_id") == context.parent_attempt_id
        and metadata.get("batch_attempt_id") == row.attempt_id
        and metadata.get("batch_route_node") == node
    )


def _bound_batch_start(
    *,
    base: Path,
    options: dict[str, list[str]],
    group: str,
    open_attempt_ids: set[str],
    context: SupervisedDispatchContext,
) -> bool:
    if not _command_paths_match(
        base=base,
        route_values=options.get("--route", []),
        jobs_values=options.get("--jobs", []),
        context=context,
    ):
        return False
    declaration = _declared_parallel_nodes(context.route, group)
    if declaration is None:
        return False
    declared, canonical = declaration
    node_ids = set(declared)
    declared_size = len(node_ids)
    pending_rows = [
        row for row in context.rows if row.attempt_id in open_attempt_ids
    ]
    if not pending_rows or any(
        not _parallel_row_matches(
            row,
            context=context,
            group=group,
            node_ids=node_ids,
            declared_size=declared_size,
            canonical=canonical,
        )
        for row in pending_rows
    ):
        return False

    route_group_rows = [
        row
        for row in context.rows
        if row.metadata.get("route_node") in node_ids
        or row.metadata.get("parallel_group") == group
        or row.metadata.get("replica_group") == group
        or row.metadata.get("batch_group") == group
    ]
    exact_rows = [
        row
        for row in route_group_rows
        if _parallel_row_matches(
            row,
            context=context,
            group=group,
            node_ids=node_ids,
            declared_size=declared_size,
            canonical=canonical,
        )
    ]
    # A parked recovery may fill exactly one missing leg. Fewer than N-1 rows
    # would create a partial new batch; N rows means the group was already
    # claimed. Duplicate/malformed rows cannot authorize recovery either.
    return (
        len(route_group_rows) == declared_size - 1
        and len(exact_rows) == declared_size - 1
        and len({row.metadata.get("route_node") for row in exact_rows})
        == declared_size - 1
    )


def _bound_dispatch_node_start(
    *,
    base: Path,
    options: dict[str, list[str]],
    trailing: list[str],
    open_attempt_ids: set[str],
    context: SupervisedDispatchContext,
) -> bool:
    selected = _selected_long_options(
        trailing,
        {
            "--jobs",
            "--parent-attempt-id",
            "--route-file",
            "--route-id",
            "--route-node",
            "--dispatch-depth",
            "--parent",
        },
    )
    if selected is None or any(
        option in selected
        for option in {
            "--route-file",
            "--route-id",
            "--route-node",
            "--dispatch-depth",
            "--parent",
        }
    ):
        return False
    if not _command_paths_match(
        base=base,
        route_values=options.get("--route", []),
        jobs_values=selected.get("--jobs", []),
        context=context,
    ):
        return False
    explicit_parent = selected.get("--parent-attempt-id", [])
    if len(explicit_parent) > 1 or (
        explicit_parent and explicit_parent != [context.parent_attempt_id]
    ):
        return False
    raw_nodes = context.route.get("nodes")
    if not isinstance(raw_nodes, list):
        return False
    node_id = options["--node"][0]
    matches = [
        node
        for node in raw_nodes
        if isinstance(node, dict) and node.get("id") == node_id
    ]
    if (
        len(matches) != 1
        or matches[0].get("dispatch_depth") != 2
        or matches[0].get("parallel_group")
        or matches[0].get("replica_group")
    ):
        return False
    pending_rows = [
        row for row in context.rows if row.attempt_id in open_attempt_ids
    ]
    if not pending_rows or any(
        not _row_matches_current_route(row, context) for row in pending_rows
    ):
        return False
    return not any(row.metadata.get("route_node") == node_id for row in context.rows)


def classify_supervised_shell_command(
    *,
    base: Path,
    command: str,
    open_attempt_ids: set[str],
    parent_slug: str,
    jobs: Path | None = None,
    parent_attempt_id: str = "",
    route_file: Path | None = None,
    route_id: str = "",
) -> SupervisorShellAction | None:
    """Recognize only exact harvest or one additional parent-bound dispatch."""

    if not command or not open_attempt_ids or re_search_shell_composition(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None

    if (
        len(tokens) >= 2
        and _local_contract_path(base, tokens[0], "adapters/codex/bin/preflight.sh")
        and tokens[1] == "harvest"
    ):
        options = _parse_long_options(
            tokens[2:],
            {"--attempt-id", "--status", "--completion"},
            {"--mark-done", "--keep-home", "--failure-detail"},
        )
        if options is None or len(options.get("--attempt-id", [])) != 1:
            return None
        if any(len(values) != 1 for values in options.values()):
            return None
        attempt = options["--attempt-id"][0]
        if (
            attempt not in open_attempt_ids
            or options.get("--status", ["open"])[0] not in {"open", "all"}
        ):
            return None
        return SupervisorShellAction("harvest", attempt)

    strict_binding = _strict_supervisor_binding_requested(
        jobs=jobs,
        parent_attempt_id=parent_attempt_id,
        route_file=route_file,
        route_id=route_id,
    )
    context: SupervisedDispatchContext | None = None
    if strict_binding:
        try:
            context = _supervised_dispatch_context(
                jobs=jobs,
                parent_attempt_id=parent_attempt_id,
                route_file=route_file,
                route_id=route_id,
                open_attempt_ids=open_attempt_ids,
            )
        except JoinContractError:
            return None

    dispatch_tokens = tokens
    if tokens[0] in {"python", "python3"}:
        if len(tokens) < 2:
            return None
        dispatch_tokens = tokens[1:]
    is_batch = False
    if _local_contract_path(base, dispatch_tokens[0], "adapters/codex/bin/preflight.sh"):
        if len(dispatch_tokens) < 2 or dispatch_tokens[1] != "dispatch-batch":
            return None
        dispatch_tokens = [str(ROOT / "utilities" / "dispatch-batch.py"), *dispatch_tokens[2:]]
        is_batch = True
    elif _local_contract_path(base, dispatch_tokens[0], "utilities/dispatch-batch.py"):
        is_batch = True
    elif not _local_contract_path(base, dispatch_tokens[0], "utilities/dispatch-node.py"):
        return None

    if is_batch:
        options = _parse_long_options(
            dispatch_tokens[1:],
            {
                "--route",
                "--parallel-group",
                "--replica-group",
                "--action",
                "--slug-prefix",
                "--parent",
                "--qa",
                "--jobs",
                "--prompt-text",
            },
            {"--allow-degraded-independence"},
        )
        required = {
            "--route",
            "--action",
            "--slug-prefix",
            "--parent",
        }
        canonical_groups = options.get("--parallel-group", []) if options else []
        legacy_groups = options.get("--replica-group", []) if options else []
        group_values = canonical_groups or legacy_groups
        aliases_match = not (
            canonical_groups
            and legacy_groups
            and canonical_groups != legacy_groups
        )
        if (
            options is None
            or not parent_slug
            or any(len(values) != 1 for values in options.values())
            or not required.issubset(options)
            or len(group_values) != 1
            or not aliases_match
            or options["--action"] != ["start"]
            or options["--parent"] != [parent_slug]
        ):
            return None
        group = group_values[0]
        if context is not None and not _bound_batch_start(
            base=base,
            options=options,
            group=group,
            open_attempt_ids=open_attempt_ids,
            context=context,
        ):
            return None
        return SupervisorShellAction("dispatch-batch")

    try:
        separator = dispatch_tokens.index("--")
    except ValueError:
        separator = len(dispatch_tokens)
    options = _parse_long_options(
        dispatch_tokens[1:separator],
        {
            "--route",
            "--node",
            "--adapter",
            "--action",
            "--slug",
            "--qa",
            "--parent",
            "--prompt-text",
        },
        set(),
    )
    required = {"--route", "--node", "--adapter", "--action", "--slug", "--parent"}
    if (
        options is None
        or not parent_slug
        or any(len(values) != 1 for values in options.values())
        or not required.issubset(options)
        or options["--action"] != ["start"]
        or options["--parent"] != [parent_slug]
        or options["--adapter"][0] not in {"claude", "codex", "opencode"}
    ):
        return None
    if context is not None and not _bound_dispatch_node_start(
        base=base,
        options=options,
        trailing=dispatch_tokens[separator + 1 :] if separator < len(dispatch_tokens) else [],
        open_attempt_ids=open_attempt_ids,
        context=context,
    ):
        return None
    return SupervisorShellAction("dispatch")


def re_search_shell_composition(command: str) -> bool:
    return (
        any(char in command for char in "\n\r;&|<>")
        or chr(96) in command
        or "$(" in command
    )


def _metadata(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        values[key] = value
    return values


def current_children(
    jobs: Path,
    parent_attempt_id: str,
    expected_attempts: set[str] | None = None,
) -> list[ChildRow]:
    """Return latest exact-attempt rows owned by ``parent_attempt_id``.

    Foreign parents, legacy slug-only rows, and same-slug retries are ignored.
    A matching v2 row that lacks its attempt identity fails closed.
    """

    if not parent_attempt_id:
        raise JoinContractError("parent-attempt-id-missing")
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        raise JoinContractError("registry-unreadable") from exc

    latest: dict[str, ChildRow] = {}
    for order, line in enumerate(lines):
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = _metadata(fields[5])
        if meta.get("parent_attempt_id") != parent_attempt_id:
            continue
        if meta.get("attempt_schema_version") != "2":
            raise JoinContractError("owned-row-schema-invalid")
        attempt_id = meta.get("attempt_id", "")
        if not attempt_id:
            raise JoinContractError("owned-row-attempt-id-missing")
        if expected_attempts is not None and attempt_id not in expected_attempts:
            continue
        latest[attempt_id] = ChildRow(
            order=order,
            status=fields[1],
            slug=fields[4],
            attempt_id=attempt_id,
            raw=line,
            metadata=meta,
        )

    if expected_attempts is not None:
        missing = expected_attempts.difference(latest)
        if missing:
            raise JoinContractError("expected-attempt-missing")
    return sorted(latest.values(), key=lambda row: row.order)


def current_session_children(
    jobs: Path,
    parent_session_id: str,
    expected_attempts: set[str] | None = None,
    parent_completion_delivery: str = SESSION_PARENT_DELIVERY,
) -> list[ChildRow]:
    """Return exact direct children owned by one interactive Codex session.

    Only rows explicitly stamped for the selected parent-runtime delivery
    surface qualify. Unmarked rows remain on the disclosed polling fallback.
    The initial lookup selects current open/running rows; an expected snapshot
    continues to follow those same attempts after their status changes.
    """

    if not parent_session_id:
        raise JoinContractError("parent-session-id-missing")
    if parent_completion_delivery not in SESSION_PARENT_DELIVERIES:
        raise JoinContractError("parent-completion-delivery-invalid")
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        raise JoinContractError("registry-unreadable") from exc

    latest: dict[str, ChildRow] = {}
    for order, line in enumerate(lines):
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = _metadata(fields[5])
        if (
            meta.get("parent_sid") != parent_session_id
            or meta.get("parent_completion_delivery")
            != parent_completion_delivery
        ):
            continue
        if (
            meta.get("launch_claimed") == "0"
            or meta.get("parent_completion_harvested") == "1"
        ):
            continue
        if (
            meta.get("attempt_schema_version") != "2"
            or meta.get("dispatch_depth") != "1"
            or meta.get("execution_surface") != "registered-headless"
            or meta.get("registered_worker") != "1"
            or meta.get("launch_claimed") != "1"
        ):
            raise JoinContractError("owned-session-row-contract-invalid")
        attempt_id = meta.get("attempt_id", "")
        if not attempt_id:
            raise JoinContractError("owned-session-row-attempt-id-missing")
        if expected_attempts is not None and attempt_id not in expected_attempts:
            continue
        latest[attempt_id] = ChildRow(
            order=order,
            status=fields[1],
            slug=fields[4],
            attempt_id=attempt_id,
            raw=line,
            metadata=meta,
        )

    if len(latest) > MAX_BATCH_ATTEMPTS:
        raise JoinContractError("owned-session-batch-oversized")
    if expected_attempts is not None:
        missing = expected_attempts.difference(latest)
        if missing:
            raise JoinContractError("expected-attempt-missing")
    rows = sorted(latest.values(), key=lambda row: row.order)
    if expected_attempts is None:
        rows = [row for row in rows if row.status in OPEN_STATES]
    return rows


def pending_attempt_ids(rows: list[ChildRow]) -> set[str]:
    """Return children that are open or terminal-but-not-yet-quiescent."""

    pending: set[str] = set()
    for row in rows:
        observed = observed_attempt_liveness(
            row.status,
            row.metadata,
            terminal_envelope=terminal_envelope_observed(
                row.metadata.get("log_file")
            ),
        )
        if observed.state in {"alive", "unverifiable"}:
            pending.add(row.attempt_id)
    return pending


def _liveness_state(
    row: ChildRow,
    command: list[str],
    env: dict[str, str],
    timeout: float = 30.0,
) -> str:
    """Return ``alive`` or ``terminal`` without exposing liveness output."""

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        fields = row.raw.split("\t")
        if len(fields) == 6 and fields[1] == "running":
            fields[1] = "open"
        handle.write("\t".join(fields) + "\n")
        registry = Path(handle.name)
    try:
        result = subprocess.run(
            [*command, str(registry)],
            env={**env, "AGENT_DISPATCH_JOBS": str(registry)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(0.1, timeout),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JoinContractError("liveness-contract-unavailable") from exc
    finally:
        try:
            registry.unlink()
        except OSError:
            pass
    if result.returncode == 0:
        return "alive"
    if result.returncode == 3:
        return "terminal"
    raise JoinContractError("liveness-contract-failed")


def _join_snapshot(
    *,
    initial: list[ChildRow],
    refresh: Callable[[set[str]], list[ChildRow]],
    identity: dict[str, str],
    interval: float,
    timeout: float,
    liveness_command: list[str] | None,
    liveness_probe_timeout: float,
    env: dict[str, str] | None,
) -> dict[str, object]:
    """Join one immutable exact-attempt snapshot."""

    command = liveness_command or [str(ROOT / "utilities" / "dispatch-liveness.sh")]
    runtime_env = dict(os.environ if env is None else env)
    interval = max(0.05, interval)
    timeout = max(0.0, timeout)
    if not initial:
        return {
            "schema_version": SCHEMA_VERSION,
            "state": "no-children",
            **identity,
            "children": [],
        }
    snapshot = {row.attempt_id for row in initial}
    started = time.monotonic()

    while True:
        rows = refresh(snapshot)
        children: list[dict[str, str]] = []
        pending = False
        for row in rows:
            observed = observed_attempt_liveness(
                row.status,
                row.metadata,
                terminal_envelope=terminal_envelope_observed(
                    row.metadata.get("log_file")
                ),
            )
            if row.status == "done":
                if observed.state == "terminal":
                    readiness, reason = "ready", "registry-closed"
                else:
                    readiness = "pending"
                    reason = (
                        "process-alive"
                        if observed.state == "alive"
                        else "process-unverifiable"
                    )
                    pending = True
            elif row.status in OPEN_STATES:
                if observed.state == "alive":
                    readiness, reason = "pending", "process-alive"
                    pending = True
                elif observed.state == "reconcile-needed":
                    readiness, reason = "ready", "terminal-observed"
                else:
                    probe = _liveness_state(
                        row, command, runtime_env, liveness_probe_timeout
                    )
                    if probe == "terminal":
                        readiness, reason = "ready", "terminal-observed"
                    else:
                        readiness = "pending"
                        reason = "process-unverifiable"
                        pending = True
            else:
                raise JoinContractError("owned-row-status-invalid")
            children.append(
                {
                    "attempt_id": row.attempt_id,
                    "slug": row.slug,
                    "status": row.status,
                    "readiness": readiness,
                    "reason": reason,
                    "required_action": required_action_for_attempt(
                        row.status, row.metadata
                    ),
                }
            )
        if not pending:
            return {
                "schema_version": SCHEMA_VERSION,
                "state": "ready",
                **identity,
                "children": children,
            }
        if time.monotonic() - started >= timeout:
            return {
                "schema_version": SCHEMA_VERSION,
                "state": "timeout",
                **identity,
                "children": children,
            }
        time.sleep(interval)


def join_batch(
    *,
    jobs: Path,
    parent_attempt_id: str,
    expected_attempts: set[str] | None = None,
    interval: float = 2.0,
    timeout: float = 3600.0,
    liveness_command: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Join one immutable child batch sealed to an exact parent attempt."""

    initial = current_children(jobs, parent_attempt_id, expected_attempts)
    return _join_snapshot(
        initial=initial,
        refresh=lambda snapshot: current_children(jobs, parent_attempt_id, snapshot),
        identity={"parent_attempt_id": parent_attempt_id},
        interval=interval,
        timeout=timeout,
        liveness_command=liveness_command,
        liveness_probe_timeout=30.0,
        env=env,
    )


def join_session_batch(
    *,
    jobs: Path,
    parent_session_id: str,
    expected_attempts: set[str] | None = None,
    parent_completion_delivery: str = SESSION_PARENT_DELIVERY,
    interval: float = 2.0,
    timeout: float = 540.0,
    liveness_command: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Join one exact batch sealed to an interactive parent session."""

    initial = current_session_children(
        jobs,
        parent_session_id,
        expected_attempts,
        parent_completion_delivery,
    )
    return _join_snapshot(
        initial=initial,
        refresh=lambda snapshot: current_session_children(
            jobs,
            parent_session_id,
            snapshot,
            parent_completion_delivery,
        ),
        identity={"parent_session_id": parent_session_id},
        interval=interval,
        timeout=timeout,
        liveness_command=liveness_command,
        liveness_probe_timeout=5.0,
        env=env,
    )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--jobs", default=os.environ.get("AGENT_DISPATCH_JOBS"))
    value.add_argument(
        "--parent-attempt-id",
        default=None,
    )
    value.add_argument("--parent-session-id")
    value.add_argument(
        "--parent-completion-delivery",
        choices=sorted(SESSION_PARENT_DELIVERIES),
        default=SESSION_PARENT_DELIVERY,
    )
    value.add_argument("--attempt-id", action="append", default=[])
    value.add_argument("--interval", type=float, default=2.0)
    value.add_argument("--timeout", type=float, default=3600.0)
    value.add_argument("--liveness-command")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.parent_attempt_id and not args.parent_session_id:
        args.parent_attempt_id = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    identity_name = (
        "parent_session_id" if args.parent_session_id else "parent_attempt_id"
    )
    identity_value = args.parent_session_id or args.parent_attempt_id or "-"
    if not args.jobs:
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "state": "contract-error",
            identity_name: identity_value,
            "reason": "jobs-path-missing",
            "children": [],
        }
        print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
        return 69
    liveness = [args.liveness_command] if args.liveness_command else None
    try:
        if bool(args.parent_attempt_id) == bool(args.parent_session_id):
            raise JoinContractError("parent-identity-ambiguous")
        if args.parent_session_id:
            receipt = join_session_batch(
                jobs=Path(args.jobs),
                parent_session_id=args.parent_session_id,
                expected_attempts=(
                    set(args.attempt_id) if args.attempt_id else None
                ),
                parent_completion_delivery=args.parent_completion_delivery,
                interval=args.interval,
                timeout=args.timeout,
                liveness_command=liveness,
            )
        else:
            receipt = join_batch(
                jobs=Path(args.jobs),
                parent_attempt_id=args.parent_attempt_id or "",
                expected_attempts=(
                    set(args.attempt_id) if args.attempt_id else None
                ),
                interval=args.interval,
                timeout=args.timeout,
                liveness_command=liveness,
            )
    except JoinContractError as exc:
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "state": "contract-error",
            identity_name: identity_value,
            "reason": str(exc),
            "children": [],
        }
    print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
    return {"ready": 0, "no-children": 2, "timeout": 3}.get(str(receipt["state"]), 69)


def _row_worktree(row: ChildRow) -> str:
    fields = str(getattr(row, "raw", "")).split("\t")
    return fields[3] if len(fields) == 6 else ""


def route_completion_evidence(
    metadata: dict[str, str], *, worktree: str
) -> tuple[str | None, str]:
    """Derive a readable in-root artifact path from a route-bound child's own
    terminal envelope, or ``(None, reason)`` when it cannot legally complete.

    Shared by `close_finished_child` (supervisor path) and
    `dispatch-harvest.py --mark-done` (SD-70/78, round_1 finding 5) so both
    derive completion evidence through the identical fail-closed predicate:
    a valid envelope, a PASS verdict, and a readable in-root artifact. Never
    a raw untrusted path — the inspector hands back only a bounded url-safe
    base64 form.

    The reason distinguishes a bad base64 payload (`evidence-undecodable`)
    from an empty or non-absolute decoded path (`evidence-absent`) — both
    used to collapse into one bare `None`, which lost the distinction a
    reader debugging a stuck row needs (round_1 review advisory 4).
    """

    terminal = inspect_terminal_attempt(
        metadata.get("log_file"),
        worktree=worktree,
        artifact_root_metadata=metadata.get("artifact_root"),
    )
    if terminal.get("state") != "valid":
        return None, "evidence-not-valid"
    if str(terminal.get("verdict")) != "PASS":
        return None, "evidence-not-pass"
    if terminal.get("artifact_state") != "readable":
        return None, "evidence-not-readable"
    encoded = str(terminal.get("artifact_path_b64") or "")
    try:
        artifact = base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        ).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None, "evidence-undecodable"
    if not artifact or not Path(artifact).is_absolute():
        return None, "evidence-absent"
    return artifact, ""


def close_finished_child(row: ChildRow, *, jobs: str | Path) -> str:
    """Close one finished-but-open child from its own terminal evidence.

    A route-bound node may only be closed through the completion-marker path
    (OPERATIONS §5.10, SD-70); `dispatch-harvest --mark-done` refuses it with
    `route-completion-required`. The supervised-parent park hook in turn admits
    only that harvest command (`classify_supervised_shell_command`), so a
    supervised owner cannot execute the one command that would work: the model
    has no legal exit and the batch deadlocks until the owner is killed. A
    supervisor is not park-guarded and already owns the join outside the model
    loop, so it performs this closure itself.

    Completion is never invented — without a valid terminal envelope naming an
    in-root artifact the child stays open and the caller keeps its existing
    failure. Returns ``""`` on success, else a short skip reason.
    """

    metadata = getattr(row, "metadata", {}) or {}
    route_file = metadata.get("route_file")
    route_node = metadata.get("route_node")
    if not route_file or not route_node:
        return "not-route-bound"
    terminal = inspect_terminal_attempt(
        metadata.get("log_file"),
        worktree=_row_worktree(row),
        artifact_root_metadata=metadata.get("artifact_root"),
    )
    if terminal.get("state") != "valid":
        # Carry the inspector's own enum: a bare `terminal-invalid` sends the
        # next reader back through the parser to learn whether the envelope was
        # missing, malformed, or a runtime error.  The enum is a fixed vocabulary,
        # never raw agent text.
        skip = "terminal-%s" % (terminal.get("state") or "absent")
        detail = terminal.get("reason")
        reason = f"{skip}:{detail}" if detail else skip
        if (
            str(terminal.get("state")) == "invalid"
            and str(detail or "").startswith("artifact-")
            and _close_invalid_envelope_child(row, jobs=jobs, reason=reason)
        ):
            return ""
        return reason
    verdict = str(terminal.get("verdict"))
    if verdict in ("BLOCKED", "FAIL"):
        # SD-72/SD-78 (round_1 finding 1): a valid terminal handoff carrying
        # BLOCKED or FAIL closes typed IMMEDIATELY on the verdict alone — this
        # must run before the artifact_state branch below, because the
        # inspector accepts a readable artifact independently of verdict
        # (codex_dispatch_terminal.py). Without this ordering a readable
        # BLOCKED/FAIL envelope would fall through to route completion and
        # manufacture a PASS for a worker that never passed. Route completion
        # is reachable only for a PASS verdict, never for BLOCKED/FAIL.
        note = "dead-worker-blocked" if verdict == "BLOCKED" else "dead-worker-fail"
        reason = "typed-%s" % verdict.lower()
        if _close_invalid_envelope_child(
            row,
            jobs=jobs,
            reason=reason,
            note=note,
            classifier_source="completion-join-terminal-verdict-v1",
        ):
            return ""
        return reason
    if terminal.get("artifact_state") != "readable":
        reason = "evidence-%s" % (terminal.get("artifact_state") or "absent")
        if verdict == "PASS" and _close_invalid_envelope_child(
            row, jobs=jobs, reason=reason
        ):
            return ""
        return reason
    artifact, evidence_reason = route_completion_evidence(
        metadata, worktree=_row_worktree(row)
    )
    if artifact is None:
        return evidence_reason
    command = [
        sys.executable,
        str(ROOT / "utilities" / "capability-route.py"),
        "complete",
        "--route", str(route_file),
        "--node", str(route_node),
        "--evidence", artifact,
        "--jobs", str(jobs),
        "--attempt-id", row.attempt_id,
    ]
    for flag, key in (
        ("--dispatch-depth", "dispatch_depth"),
        ("--transport", "transport"),
        ("--execution-surface", "execution_surface"),
        ("--registered-worker", "registered_worker"),
        ("--fallback-hop", "fallback_hop"),
    ):
        value = metadata.get(key)
        if value:
            command += [flag, str(value)]
    return run_route_completion(command)


def _close_invalid_envelope_child(
    row: ChildRow,
    *,
    jobs: str | Path,
    reason: str,
    note: str = "dead-invalid-envelope",
    classifier_source: str = "completion-join-invalid-envelope-v1",
) -> bool:
    """Close a quiescent child whose terminal envelope can never legally complete.

    The worker reached its semantic terminal — an envelope exists — but named
    no readable in-root artifact file, so the completion-marker path (SD-70)
    can never run for this attempt and a remediation continuation cannot
    change the outcome. Recording a typed death keeps the route node
    incomplete for re-dispatch instead of deadlocking the supervised owner
    into `owned-children-remain-open-after-resume`. A live or unverifiable
    process keeps the row open — this never races a still-draining worker.
    """

    observed = observed_attempt_liveness(
        row.status, row.metadata, terminal_envelope=True
    )
    if observed.state != "reconcile-needed" or observed.process_state != "quiescent":
        return False
    try:
        return close_attempt_row(
            Path(jobs),
            row.attempt_id,
            note,
            evidence={
                "classifier_source": classifier_source,
                "reconcile_reason": reason,
            },
        )
    except DispatchContractError:
        return False


def run_route_completion(command: list[str]) -> str:
    """Named seam for the completion invocation — tests replace only this."""

    try:
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=60.0, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "completion-%s" % type(exc).__name__
    return "" if result.returncode == 0 else "completion-rejected"


def reconcile_finished_children(
    rows: dict[str, ChildRow], unresolved: set[str], *, jobs: str | Path
) -> dict[str, str]:
    """Attempt evidence-backed closure for each unresolved child.

    Returns ``{attempt_id: reason}`` where an empty reason means closed.
    """

    outcomes: dict[str, str] = {}
    for attempt in sorted(unresolved):
        row = rows.get(attempt)
        if row is None:
            continue
        outcomes[attempt] = close_finished_child(row, jobs=jobs)
    return outcomes


if __name__ == "__main__":
    raise SystemExit(main())
