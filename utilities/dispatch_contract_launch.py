"""Dispatch contract: attempt launch.

Launch identity, the fenced launch abort, review admission failure and cleanup,
the parent completion window and liveness, the launch registry transaction and
preparation, and resolving the live parent attempt. `spawn_claimed_attempt`
stays in `dispatch_contract.py` because it checks the terminal claim.

Split out of `dispatch_contract.py`, which re-exports every name defined here.
"""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
from typing import NamedTuple
from route_identity import registered_node_identity
from route_authority import linked_worktree_slice as is_linked_worktree_slice

from dispatch_contract_base import (
    AuthoritativeProcessIdentity, canonical_repository_identity, DispatchContractError,
    owner_closure_shape, PARENT_LIVENESS_METADATA_KEYS, ParentAttemptBinding,
    parse_registry_metadata, PID_HOST_NAMESPACE_PROOF, ReviewAdmissionCleanup, row_has_attempt,
)

from dispatch_contract_roots import (
    ensure_global_registry_writable,
)

from dispatch_contract_policy import (
    validate_attempt_metadata,
)

from dispatch_contract_process import (
    _proc_observation, attempt_governed_process_quiescence,
    exact_process_group_signal_authority, process_group_observation, process_identity_is_live,
    process_namespace_identity, signal_exact_process_group, supervisor_lease_is_held,
)

from dispatch_contract_registry import (
    _atomic_registry_replace, _RegistryParse,
)


def _launch_identity_complete(pid: int, identity: dict[str, str]) -> bool:
    observer_namespace = identity.get("pid_observer_ns", "")
    expected_start = identity.get("pid_start", "")
    if not (
        identity.get("pid") == str(pid)
        and expected_start
        and observer_namespace
        and identity.get("pid_ns") == observer_namespace
        and identity.get("pgid") == str(pid)
    ):
        return False
    visibility, actual_start, state = _proc_observation(pid)
    if not (
        visibility == "present"
        and actual_start == expected_start
        and state != "Z"
        and exact_process_group_signal_authority(pid, expected_start)
        == "authoritative"
    ):
        return False

    host_keys = {
        "pid_host",
        "pid_host_start",
        "pid_host_ns",
        "pid_host_proof",
        "pgid_host",
    }
    if any(identity.get(key) for key in host_keys):
        raw_host = identity.get("pid_host", "")
        if not (
            raw_host.isdigit()
            and identity.get("pid_host_start") == expected_start
            and identity.get("pid_host_ns")
            and identity.get("pid_host_proof") == PID_HOST_NAMESPACE_PROOF
            and identity.get("pgid_host") == raw_host
        ):
            return False
    return True


def _abort_fenced_launch(
    proc: subprocess.Popen,
    gate_write: int,
    expected_start: str,
) -> bool:
    """Close an unreleased gate and verify that its exact group is empty."""

    try:
        os.close(gate_write)
    except OSError:
        pass
    try:
        proc.wait(timeout=0.75)
    except (OSError, subprocess.TimeoutExpired):
        status = (
            signal_exact_process_group(proc.pid, expected_start, signal.SIGKILL)
            if expected_start
            else "identity-unverifiable"
        )
        if status != "signalled":
            # A failed exact signal is not permission to fall back to a
            # process-only kill: the group/namespace proof is the safety
            # boundary.  The caller records cleanup as unverified.
            pass
        try:
            proc.wait(timeout=0.75)
        except (OSError, subprocess.TimeoutExpired):
            pass
    group = process_group_observation(proc.pid)
    return proc.poll() is not None and group.state == "empty"


def _review_admission_cleanup_token(cleanup: ReviewAdmissionCleanup | None) -> str:
    if cleanup is None:
        return "unverified"
    if cleanup.status == "verified-post-release-reaped":
        return "verified-post-release-reaped-v1"
    if cleanup.status == "verified-never-launched" and cleanup.review_lease == "released":
        return "verified-lease-released-v1"
    if cleanup.status == "verified-never-launched" and cleanup.review_lease == "never-acquired":
        return "verified-lease-never-acquired-v1"
    return "unverified"


def _unverified_review_admission_cleanup() -> ReviewAdmissionCleanup:
    return ReviewAdmissionCleanup(
        watchdog_group="unverified",
        fenced_child_group="unverified",
        readiness="unverified",
        review_lease="unverified",
        governed_witness="unverified",
        payload_marker="unverified",
        status="unverified",
    )


def _merge_review_cleanup_group_proof(
    cleanup: ReviewAdmissionCleanup | None, registered_group_empty: bool,
) -> ReviewAdmissionCleanup | None:
    """Fold the transaction's exact registered-group proof into callback proof."""

    if cleanup is None:
        return None
    watchdog_group = "empty" if registered_group_empty else "unverified"
    verified = (
        watchdog_group == "empty"
        and cleanup.fenced_child_group == "empty"
        and cleanup.readiness == "closed-removed"
        and cleanup.review_lease in {"released", "never-acquired"}
        and cleanup.governed_witness == "unlocked"
    )
    status = (
        "verified-post-release-reaped"
        if cleanup.status == "verified-post-release-reaped"
        else "verified-never-launched"
    ) if verified else "unverified"
    return ReviewAdmissionCleanup(
        watchdog_group=watchdog_group,
        fenced_child_group=cleanup.fenced_child_group,
        readiness=cleanup.readiness,
        review_lease=cleanup.review_lease,
        governed_witness=cleanup.governed_witness,
        payload_marker=(
            "may-have-started" if status == "verified-post-release-reaped"
            else "absent"
        ) if verified else "unverified",
        status=status,
    )


def _record_review_admission_failure(
    jobs: Path, attempt_id: str, cleanup: ReviewAdmissionCleanup | None,
    *, post_release: bool = False,
) -> bool:
    """Annotate a claimed row using one lock acquisition, never from abort()."""

    token = _review_admission_cleanup_token(cleanup)
    if post_release and token.startswith("verified-"):
        token = "verified-post-release-reaped-v1"
    try:
        with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            lines = Path(jobs).read_text(encoding="utf-8", errors="replace").splitlines()
            matches: list[tuple[int, list[str], dict[str, str]]] = []
            for index, line in enumerate(lines):
                fields = line.split("\t")
                if len(fields) != 6:
                    continue
                metadata = parse_registry_metadata(fields[5])
                if metadata.get("attempt_id") == attempt_id:
                    matches.append((index, fields, metadata))
            if len(matches) != 1 or matches[0][2].get("launch_claimed") != "1":
                return False
            index, fields, metadata = matches[0]
            updates = {
                "launch_outcome": "post-release-failed" if post_release else "never-launched",
                "review_admission": "aborted" if not post_release else "commit-failed",
                "review_admission_cleanup": token,
            }
            parts = [
                part for part in fields[5].split(",")
                if part.split("=", 1)[0] not in updates
            ]
            parts.extend(f"{key}={value}" for key, value in sorted(updates.items()))
            fields[5] = ",".join(parts)
            lines[index] = "\t".join(fields)
            _atomic_registry_replace(Path(jobs), lines)
            return token != "unverified"
    except (OSError, DispatchContractError):
        return False



def adapter_launch_failure_outcome(jobs: Path, attempt_id: str, reason: str) -> str:
    """Preserve the launch transaction's outcome when an adapter reports it."""
    rows = []
    for line in Path(jobs).read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) == 6 and row_has_attempt(fields[5], attempt_id):
            rows.append(parse_registry_metadata(fields[5]))
    if len(rows) != 1:
        raise DispatchContractError("attempt-row-not-unique", attempt_id)
    metadata = rows[0]
    if metadata.get("review_admission") in {"aborted", "commit-failed"}:
        outcome = metadata.get("launch_outcome", "")
        if outcome in {"never-launched", "post-release-failed"}:
            return outcome
        raise DispatchContractError("review-admission-outcome-missing", attempt_id)
    return {
        "attempt-launch-identity-record-failed": "reaped-before-publish",
        "attempt-launch-cleanup-unverified": "launch-cleanup-unverified",
    }.get(reason, "never-launched")

def _parent_liveness_evidence(
    jobs: Path, metadata: dict[str, str]
) -> tuple[bool, str, AuthoritativeProcessIdentity | None]:
    # Parent authority belongs to the governed process or its exact supervisor
    # lease. A tagged descendant (including this guard) is neither that process
    # nor evidence against the namespace-only lease fallback (SD-OPEN-61).
    process = attempt_governed_process_quiescence(metadata)
    if process.state == "live" and process.identity is not None:
        return True, "process", process.identity
    if (
        process.state == "unverifiable"
        and process.reason == "process-namespace-unverifiable"
        and supervisor_lease_is_held(jobs, metadata)
    ):
        return True, "supervisor-lease", None
    return False, process.reason, None


class ParentCompletionWindow(NamedTuple):
    """Whether an exact live parent still owns delivery of a child's result."""

    deferred: bool
    source: str


def parent_completion_window(
    jobs: Path, child_fields: list[str], child_metadata: dict[str, str]
) -> ParentCompletionWindow:
    """Decide whether a proven-live exact parent still owns this child's completion.

    F-1: extends the S-3 missing-result closure axis with the delivering
    parent's liveness, so reap-watch does not race a still-live conductor's
    ``capability-route.py complete``. Never acquires ``<jobs>.lock`` (SD-49) —
    callers under the lock re-evaluate this as the sole authoritative
    decision point (see ``still_orphan`` in ``dispatch-registry.py`` for the
    same unlocked-read precedent).
    """

    parent_attempt_id = child_metadata.get("parent_attempt_id", "")
    if not parent_attempt_id:
        return ParentCompletionWindow(False, "parent-attempt-absent")
    try:
        lines = Path(jobs).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ParentCompletionWindow(False, "parent-attempt-absent")
    all_matches: list[tuple[list[str], dict[str, str]]] = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") == parent_attempt_id:
            all_matches.append((fields, metadata))
    if not all_matches:
        return ParentCompletionWindow(False, "parent-attempt-absent")
    open_matches = [m for m in all_matches if m[0][1] in {"open", "running"}]
    if not open_matches:
        return ParentCompletionWindow(False, "parent-attempt-not-open")
    if len(open_matches) > 1:
        return ParentCompletionWindow(False, "parent-attempt-ambiguous")
    parent_fields, parent_metadata = open_matches[0]
    try:
        validate_attempt_metadata(parent_metadata)
    except DispatchContractError:
        return ParentCompletionWindow(False, "parent-contract-invalid")
    # Strict AND on the same two axes `spawn_claimed_attempt` already treats as
    # the canonical depth-1 owner identity (dispatch_depth == "1" and
    # worker_type == "owner"); depth-3 dispatch is forbidden, so no other
    # parent role exists to widen this against (plan-check round 1, finding 3).
    same_identity = (
        parent_metadata.get("dispatch_depth") == "1"
        and parent_metadata.get("worker_type") == "owner"
        and (parent_fields[3] == child_fields[3] or is_linked_worktree_slice(child_metadata))
        and canonical_repository_identity(parent_fields[2])
        == canonical_repository_identity(child_fields[2])
        and parent_fields[4] == child_metadata.get("parent")
    )
    if not same_identity:
        return ParentCompletionWindow(False, "parent-identity-foreign")
    live, reason, _identity = _parent_liveness_evidence(jobs, parent_metadata)
    if not live:
        return ParentCompletionWindow(False, f"parent-not-live:{reason}")
    return ParentCompletionWindow(True, f"parent-live:{reason}")


def _parent_metadata_matches_binding(
    metadata: dict[str, str], binding: ParentAttemptBinding
) -> bool:
    return tuple(
        (key, metadata.get(key, "")) for key in PARENT_LIVENESS_METADATA_KEYS
    ) == binding.liveness_metadata_fingerprint


def _parent_binding_is_live_from_metadata(
    jobs: Path,
    metadata: dict[str, str],
    binding: ParentAttemptBinding,
) -> bool:
    if not _parent_metadata_matches_binding(metadata, binding):
        return False
    if (
        binding.observed_pid is not None
        and binding.observed_pid_start
    ):
        return process_identity_is_live(
            binding.observed_pid, binding.observed_pid_start
        )
    live, _source, _identity = _parent_liveness_evidence(jobs, metadata)
    return live


@contextmanager
def _launch_serialization_lock(jobs: Path, attempt_id: str):
    """Serialize only one exact attempt; unrelated preparation never queues here.

    Old launchers may still use the registry-wide launch lock. The fresh row
    commit, rather than this preparation lock, arbitrates against those writers.
    Keep the same no-follow, owner and single-link file checks.
    """
    key = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()
    path = Path(f"{jobs}.launch.{key}.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1):
            raise DispatchContractError("attempt-launch-lock-invalid")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def parent_attempt_binding_is_live(
    jobs: str | Path, binding: ParentAttemptBinding
) -> bool:
    """Observe one exact parent without contending with registry writers.

    Registry writers replace complete files atomically. This read can also be
    called while a writer owns jobs.log.lock; opening that lock again would
    deadlock on a different open file description. Mutation admission keeps
    its separate fresh, lock-held parent check in spawn_claimed_attempt.
    """

    registry = Path(jobs).expanduser().resolve(strict=False)
    try:
        from dispatch_registry_cache import registry_view
        records = registry_view(registry).completion_attempt_records(binding.attempt_id)
        matches = [record for record in records if record.status in {"open", "running"}]
        if len(matches) != 1:
            return False
        record = matches[0]
        fields, metadata = record.fields, record.completion_metadata()
        try:
            validate_attempt_metadata(metadata)
        except DispatchContractError:
            return False
        if (
            fields[4] != binding.slug
            or fields[3] != binding.worktree
            or (fields[2] != binding.repository_path if binding.repository_path
                else canonical_repository_identity(fields[2]) != binding.repository_identity)
        ):
            return False
        return _parent_binding_is_live_from_metadata(registry, metadata, binding)
    except OSError:
        return False


@contextmanager
def _launch_registry_transaction(jobs: Path):
    """Only local registry/fence reads and exact row writes belong here."""
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _launch_row(lines, attempt_id, *, reason="attempt-row-not-unique"):
    matches = []
    for index, line in enumerate(lines):
        fields = line.split("\t")
        if len(fields) == 6 and row_has_attempt(fields[5], attempt_id):
            matches.append((index, fields, parse_registry_metadata(fields[5])))
    if len(matches) != 1:
        raise DispatchContractError(reason, attempt_id)
    return matches[0]


def _launch_row_value(fields):
    # These existing observer/provenance annotations grant no launch authority.
    # Keep status, process identity, claims, cancellation, terminal evidence and
    # every unknown key exact, including the sealed source/review bindings.
    metadata = parse_registry_metadata(fields[5])
    for key in ("resolved_model", "updated_at",
                "caller_sid", "caller_harness", "caller_pane", "caller_cwd",
                "summary_owner", "summary_sid", "summary_state_file",
                "summary_owner_pid", "summary_owner_pid_start", "summary_owner_pid_scope",
                "summary_owner_pid_host", "summary_owner_pid_host_start", "summary_owner_pid_host_ns",
                "summary_owner_pid_observer_ns", "summary_owner_pid_host_proof"):
        metadata.pop(key, None)
    return (*fields[:5], tuple(sorted(metadata.items())))


_LAUNCH_COMPLETION_READS = contextvars.ContextVar("launch_completion_reads", default=None)


@contextmanager
def _launch_completion_reads():
    # Per preparation, including nested common readers; never shared between
    # concurrent legs or replayed as callbacks under the registry lock.
    reads = {"nodes": [], "attempts": set(), "groups": set(), "exact": False}
    token = _LAUNCH_COMPLETION_READS.set(reads)
    try:
        yield reads
    finally:
        _LAUNCH_COMPLETION_READS.reset(token)


def _launch_completion_marker_input(marker):
    reads = _LAUNCH_COMPLETION_READS.get()
    if reads is None or not marker:
        return
    if marker.get("attempt_id"):
        reads["attempts"].add(marker["attempt_id"])
    if marker.get("stage_authority") == "owner-chain" and marker.get("session_chain_id"):
        reads["groups"].add(marker["session_chain_id"])


def _launch_completion_input(route, node, marker=None):
    reads = _LAUNCH_COMPLETION_READS.get()
    if reads is None:
        return
    _launch_completion_marker_input(marker)
    if not route.get("route_id") or not node.get("id"):
        reads["exact"] = True
        return
    scope = (route["route_id"], dict(node))
    if scope not in reads["nodes"]:
        reads["nodes"].append(scope)
    if marker:
        if owner_closure_shape(marker) == "continuation":
            # The existing closure reader consumes all review rounds in these
            # source routes, including competitors absent from its old proof.
            proof = marker.get("owner_closure_proof")
            lineage = proof.get("lineage") if isinstance(proof, dict) else None
            if not isinstance(lineage, list) or not lineage:
                reads["exact"] = True
            else:
                for source in lineage:
                    if not isinstance(source, dict) or not source.get("route_id"):
                        reads["exact"] = True
                    elif (source["route_id"], dict(node)) not in reads["nodes"]:
                        reads["nodes"].append((source["route_id"], dict(node)))


def _launch_dependencies(lines, fields, callbacks=(), reads=None, parsed=None):
    """Pin prepared row inputs, including absent competitors, without a TTL.

    Shared gates depend on their consumed nodes and exact attempt lineage. An opaque
    callback can depend on any registry row, so its whole input is pinned. The
    shared wrapper explicitly declares its repository/route-scoped gates.
    Unrelated projects can append or complete without invalidating native gates.
    """
    if (reads and reads["exact"]) or any(callback is not None and not getattr(callback, "_dispatch_registry_scoped", False)
           for callback in callbacks):
        return tuple(lines)
    metadata = parse_registry_metadata(fields[5])
    relation_keys = ("attempt_id", "parent_attempt_id", "automatic_retry_of", "prior_attempt_id",
                     "replacement_original_attempt_id", "replacement_attempt_id")
    group_keys = ("session_chain_id", "replacement_family_id")
    attempts = {metadata[k] for k in relation_keys if metadata.get(k)}
    executions = {metadata[k] for k in (
        "attempt_id", "automatic_retry_of", "prior_attempt_id", "replacement_original_attempt_id")
        if metadata.get(k)}
    groups = {metadata[k] for k in group_keys if metadata.get(k)}
    if reads:
        attempts.update(reads["attempts"])
        groups.update(reads["groups"])
    nodes = {metadata.get("route_node")}
    for callback in callbacks:
        nodes.update(getattr(callback, "_dispatch_registry_nodes", ()))
    rows = (parsed or _RegistryParse()).rows(lines)
    selected = set()
    # Parallel slices of one chain own disjoint files and start side by side: a
    # sibling's claim or process identity changes nothing this launch consumed. A
    # marker that consumed the chain, this attempt's own lineage and a serial chain
    # stay pinned.
    own_chain = metadata.get("session_chain_id", "") if metadata.get("subsession_mode") == "parallel" else ""
    if reads and own_chain in reads["groups"]:
        own_chain = ""
    own_lineage = frozenset(attempts)
    # Include ancestors and successors even when an adopted parent has moved
    # to a different route or worktree. This is an in-memory closure only.
    changed = True
    while changed:
        changed = False
        for index, entry in enumerate(rows):
            if entry is None:
                continue
            candidate, meta = entry
            # A sibling is left out before any link test: the marker read adds this
            # node to the scope, which would pin every row of the chain again.
            if (own_chain and meta.get("session_chain_id") == own_chain
                    and meta.get("subsession_mode") == "parallel"
                    and meta.get("attempt_id") not in own_lineage
                    and not any(meta.get(k) in own_lineage for k in (
                        "automatic_retry_of", "prior_attempt_id", "replacement_original_attempt_id"))):
                continue
            same_worktree = candidate[2:4] == fields[2:4]
            # A different node's ordinary registration/start/completion does
            # not change this admission. In particular, parallel batch legs
            # must be able to reach their own launch fences concurrently.
            # Missing route identity stays conservatively in the read set.
            scoped = (bool(metadata.get("route_id") and metadata.get("route_node"))
                      and meta.get("route_id") == metadata.get("route_id")
                      and meta.get("route_node") in nodes)
            for route_id, node in (reads or {}).get("nodes", ()):
                try:
                    row_route, _row_hash, row_node = registered_node_identity(meta, node)
                except ValueError:
                    # Conflicting aliases must not disappear from a consumed
                    # scope, even when the old reader accepted their identity.
                    # A node-less owner cannot stand for an ordinary stage:
                    # it is only that stage's ancestor, not consumed execution.
                    owner_node = (node.get("kind") == "capability-owner"
                                  and node.get("unit") == "_kernel/owner"
                                  and node.get("dispatch_depth") == 1 and node.get("terminal") is True)
                    if ((meta.get("route_id") == route_id and meta.get("route_node") == node["id"])
                            or (owner_node and meta.get("owner_route_id") == route_id)):
                        scoped = True
                    continue
                if ((row_route == route_id and row_node == node["id"])
                        or (meta.get("route") == route_id and meta.get("route_node") == node["id"])):
                    scoped = True
            unknown = same_worktree and not all(
                metadata.get(k) and meta.get(k) for k in ("route_id", "route_node"))
            child = meta.get("parent_attempt_id") in executions
            successor = any(meta.get(k) in attempts for k in (
                "automatic_retry_of", "prior_attempt_id", "replacement_original_attempt_id"))
            linked = (scoped or unknown or child or successor or meta.get("attempt_id") in attempts
                      or any(meta.get(k) in groups for k in group_keys))
            if linked:
                if index not in selected:
                    selected.add(index)
                    attempts.update(meta[k] for k in relation_keys if meta.get(k))
                    groups.update(meta[k] for k in group_keys if meta.get(k))
                    changed = True
                # Quiescence of consumed work includes its descendants. A
                # common live parent is pinned, but its independent children
                # are not all dependencies of this launch.
                execution_successor = any(meta.get(k) in executions for k in (
                    "automatic_retry_of", "prior_attempt_id", "replacement_original_attempt_id"))
                if scoped or child or execution_successor:
                    if meta.get("attempt_id") and meta["attempt_id"] not in executions:
                        executions.add(meta["attempt_id"])
                        changed = True
    return tuple(_launch_row_value(rows[index][0]) for index in sorted(selected))


def _dependencies_kept(jobs, prepared, fresh) -> bool:
    """The rows a launch's preparation pinned are unchanged under the lock.

    Settled rows the registry archive moved out meanwhile are byte-exact in the archive
    and change none of their facts, so their absence is not a change (a launch without a
    route pins every row of its worktree, old settled ones included)."""
    if prepared == fresh:
        return True
    from dispatch_registry_archive import moved_only
    return moved_only(jobs, prepared, fresh)


def _launch_cancellation_routes(jobs, lines, metadata, parsed=None):
    """Resolve foreign/adopted owner routes outside every global lock."""
    routes = {}
    related = [metadata]
    from owner_route_binding import OwnerRouteBindingError, resolve_owner_route_lifecycle
    rows = [entry for entry in (parsed or _RegistryParse()).rows(lines) if entry is not None]
    by_id = {meta.get("attempt_id"): meta for _fields, meta in rows}
    current, seen = metadata, set()
    while current.get("attempt_id") not in seen:
        seen.add(current.get("attempt_id"))
        if current.get("worker_type") == "owner":
            try:
                binding, _ = resolve_owner_route_lifecycle(
                    jobs, owner_attempt_id=current.get("attempt_id"), registry_rows=rows)
                if binding is not None:
                    routes[binding.route_id] = binding.route_hash
            except OwnerRouteBindingError:
                pass  # Same unknown-owner compatibility as row_requested.
            break
        current = by_id.get(current.get("parent_attempt_id"))
        if current is None:
            break
        related.append(current)
    for meta in related:
        for id_key, hash_key in (("route_id", "route_hash"), ("owner_route_id", "owner_route_hash"),
                                 ("parent_close_route_id", "parent_close_route_hash")):
            if meta.get(id_key) and meta.get(hash_key):
                routes[meta[id_key]] = meta[hash_key]
    return tuple(sorted(routes.items()))


def _launch_parent_observation(jobs, lines, child_fields, child_meta, binding):
    if binding is None:
        return
    parents = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) == 6 and fields[1] in {"open", "running"}:
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") == binding.attempt_id:
                parents.append((fields, metadata))
    if len(parents) != 1:
        raise DispatchContractError("parent-attempt-not-live", binding.attempt_id)
    fields, metadata = parents[0]
    try:
        validate_attempt_metadata(metadata)
    except DispatchContractError as exc:
        raise DispatchContractError("parent-attempt-not-live", binding.attempt_id) from exc
    same = (metadata.get("dispatch_depth") == "1" and metadata.get("worker_type") == "owner"
            and canonical_repository_identity(fields[2]) == binding.repository_identity
            and canonical_repository_identity(child_fields[2]) == binding.repository_identity
            and (fields[3] == child_fields[3] or is_linked_worktree_slice(child_meta))
            and fields[4] == child_meta.get("parent")
            and child_meta.get("parent_attempt_id") == binding.attempt_id
            and fields[3] == binding.worktree and fields[4] == binding.slug
            and _parent_metadata_matches_binding(metadata, binding))
    if not same:
        raise DispatchContractError("parent-attempt-identity-changed", binding.attempt_id)
    if not _parent_binding_is_live_from_metadata(jobs, metadata, binding):
        raise DispatchContractError("parent-attempt-not-live", binding.attempt_id)


def _prepare_attempt_launch(jobs, attempt_id, *, parent_binding, respawn_of, preclaim,
                            expected_fields=None, published=False, phase="pre-release"):
    with _launch_completion_reads() as reads:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        index, fields, metadata = _launch_row(lines, attempt_id)
        validate_attempt_metadata(metadata)
        import route_parent_close
        if route_parent_close.row_requested(metadata, jobs):
            raise DispatchContractError("cancelled-by-parent")
        if expected_fields is not None and _launch_row_value(fields) != _launch_row_value(expected_fields):
            raise DispatchContractError(f"attempt-{phase}-identity-changed", attempt_id)
        if fields[1] not in {"open", "running"}:
            raise DispatchContractError("attempt-not-open", attempt_id)
        if not published:
            if respawn_of is None:
                if metadata.get("launch_claimed") == "1":
                    raise DispatchContractError("attempt-launch-already-claimed", attempt_id)
                if metadata.get("launch_claimed") != "0":
                    raise DispatchContractError("attempt-launch-claim-invalid", attempt_id)
            else:
                if (parent_binding is not None or metadata.get("worker_type") != "owner"
                        or metadata.get("dispatch_depth") != "1" or metadata.get("harness") != "codex"
                        or metadata.get("launch_claimed") != "1" or metadata.get("launch_started") != "1"
                        or (metadata.get("pid", ""), metadata.get("pid_start", ""))
                        != tuple(map(str, respawn_of))):
                    raise DispatchContractError("attempt-respawn-identity-changed", attempt_id)
                governed = attempt_governed_process_quiescence(metadata)
                if governed.state != "quiescent":
                    reason = ("attempt-respawn-consumer-live" if governed.state == "live"
                              else "attempt-respawn-consumer-unverifiable")
                    raise DispatchContractError(reason, governed.reason)
                if supervisor_lease_is_held(jobs, metadata):
                    raise DispatchContractError("attempt-respawn-lease-held", attempt_id)
        if respawn_of is None:
            from dispatch_replacement import admission, validate_owned_launch
            from review_input import validate_launch
            from review_source_subject import SourceSubjectError, validate_attempt
            # Initial eligibility differs from validation of this already owned
            # fenced identity. Both paths retain the current claim/input contract.
            if published:
                validate_owned_launch(jobs, lines, metadata)
            else:
                admission(jobs, lines, metadata)
            validate_launch(jobs, metadata)
            if preclaim is not None:
                preclaim(lines)
        # Canonical repository/foreign route observation can block on NAS; finish
        # it before the last content observation, still with the payload fenced.
        routes = _launch_cancellation_routes(jobs, lines, metadata)
        try:
            _launch_parent_observation(jobs, lines, fields, metadata, parent_binding)
            if respawn_of is None:
                try:
                    validate_attempt(jobs, fields, metadata)
                except SourceSubjectError as exc:
                    raise DispatchContractError(exc.reason, exc.detail) from exc
            # Source observation may take time. Recheck local process/declared
            # supervisor-lease liveness afterward, without canonicalizing NAS paths
            # or reloading foreign routes. The transaction pins that parent's row.
            if parent_binding is not None:
                _parent_index, _parent_fields, parent_meta = _launch_row(lines, parent_binding.attempt_id)
                if not _parent_binding_is_live_from_metadata(jobs, parent_meta, parent_binding):
                    raise DispatchContractError("parent-attempt-not-live", parent_binding.attempt_id)
        except DispatchContractError as exc:
            if exc.reason == "parent-attempt-not-live" and (published or expected_fields is not None):
                suffix = "after-post-claim" if published else "after-spawn"
                raise DispatchContractError("parent-attempt-not-live-" + suffix, exc.detail) from exc
            raise
        return lines, index, fields, metadata, routes, reads


def parent_lookup_worktree(
    worktree: str,
    route_file: str | Path | None,
    *,
    subsession: bool,
    parent_attempt_id: str | None,
) -> str:
    """The worktree column a slice's live-parent lookup compares against.

    An owner's registry row carries the route cwd, while a slice may launch in a
    linked worktree of the same repository. Only a slice that names its owner by
    the exact inherited attempt id is looked up at the route cwd (the id keeps a
    same-slug owner from being picked by accident); every other launch compares
    the worktree it was given.
    """

    if not subsession or not parent_attempt_id or not route_file:
        return worktree
    try:
        route_cwd = json.loads(Path(route_file).read_text(encoding="utf-8")).get("cwd")
    except (OSError, ValueError, AttributeError):
        return worktree
    if (
        not isinstance(route_cwd, str)
        or not route_cwd
        or route_cwd == worktree
        or canonical_repository_identity(route_cwd) != canonical_repository_identity(worktree)
    ):
        return worktree
    return route_cwd


def resolve_live_parent_attempt(
    jobs: Path,
    *,
    parent_slug: str,
    repo: str,
    worktree: str,
    expected_attempt_id: str | None = None,
    expected_harness: str | None = None,
    expected_transport: str | None = None,
    expected_sandbox: str | None = None,
) -> ParentAttemptBinding:
    """Resolve exactly one open, live depth-1 owner before a depth-2 claim.

    A slug is only a lookup constraint.  Teardown authority is the returned
    attempt id, and a same-slug retry cannot satisfy an explicitly inherited
    parent attempt id.
    """

    if not parent_slug:
        raise DispatchContractError("parent-slug-required", "depth-2 parent is required")
    requested_repository = canonical_repository_identity(repo)
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        candidates: list[tuple[list[str], dict[str, str]]] = []
        for line in lines:
            fields = line.split("\t")
            if len(fields) != 6 or fields[1] not in {"open", "running"}:
                continue
            if (
                canonical_repository_identity(fields[2]) != requested_repository
                or fields[3] != worktree
                or fields[4] != parent_slug
            ):
                continue
            metadata = parse_registry_metadata(fields[5])
            try:
                validate_attempt_metadata(metadata)
            except DispatchContractError:
                continue
            if metadata.get("dispatch_depth") != "1" or metadata.get("worker_type") != "owner":
                continue
            if expected_attempt_id and metadata.get("attempt_id") != expected_attempt_id:
                continue
            expected_runtime = {
                "harness": expected_harness,
                "transport": expected_transport,
                "runtime_sandbox": expected_sandbox,
            }
            if any(
                value is not None and metadata.get(key) != value
                for key, value in expected_runtime.items()
            ):
                continue
            candidates.append((fields, metadata))

        if not candidates:
            reason = "parent-attempt-not-found" if expected_attempt_id else "live-parent-not-found"
            raise DispatchContractError(reason, expected_attempt_id or parent_slug)
        if len(candidates) != 1:
            raise DispatchContractError(
                "parent-attempt-ambiguous",
                f"parent={parent_slug} candidates={len(candidates)}",
            )
        parent_fields, metadata = candidates[0]
        attempt_id = metadata.get("attempt_id", "")
        raw_pid = metadata.get("pid", "")
        pid_start = metadata.get("pid_start", "")
        raw_host = metadata.get("pid_host", "")
        host_start = metadata.get("pid_host_start", "") or pid_start
        if not attempt_id or not raw_pid.isdigit() or not pid_start:
            raise DispatchContractError("parent-process-identity-missing", attempt_id or parent_slug)
        pid = int(raw_pid)
        host_pid = int(raw_host) if raw_host.isdigit() else None
        live, liveness_source, observed = _parent_liveness_evidence(jobs, metadata)
        if not live:
            # A host PID absent inside a worker namespace is not proof of death.
            # Preserve the actual authority verdict and both observer identities
            # so operators can distinguish it from positive PID reuse/death.
            detail = json.dumps({
                "attempt_id": attempt_id,
                "liveness_reason": liveness_source,
                "pid": raw_pid,
                "pid_start": pid_start,
                "recorded_observer_ns": metadata.get("pid_observer_ns", ""),
                "observer_ns": process_namespace_identity() or "",
            }, sort_keys=True)
            raise DispatchContractError("parent-attempt-not-live", detail)
        return ParentAttemptBinding(
            attempt_id=attempt_id,
            pid=pid,
            pid_start=pid_start,
            pid_scope=metadata.get("pid_scope", "host-visible"),
            pid_host=host_pid,
            pid_host_start=host_start,
            observed_pid=observed.pid if observed is not None else None,
            observed_pid_start=observed.expected_start if observed is not None else "",
            liveness_source=liveness_source,
            harness=metadata.get("harness", ""),
            transport=metadata.get("transport", ""),
            runtime_sandbox=metadata.get("runtime_sandbox", ""),
            repository_identity=requested_repository,
            repository_path=parent_fields[2],
            worktree=parent_fields[3],
            slug=parent_fields[4],
            liveness_metadata_fingerprint=tuple(
                (key, metadata.get(key, ""))
                for key in PARENT_LIVENESS_METADATA_KEYS
            ),
        )
