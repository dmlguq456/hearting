"""`launch-tuple` durable record leaf module (SD-114, plan.md §5).

A dependency leaf: this module imports only `dispatch_contract`'s home/state-
root resolvers and nothing else `dispatch_*`, so it can be imported from any
producer without risk of a circular import.

Two independent JSONL ledgers live under `<dispatch_state_root>/launch-tuple/`:

- `<route_id>.jsonl` -- one row per (tuple, rejection_class, owner_attempt)
  candidate rejection, written by `record_rejection()`.
- `_report/<route_id>.jsonl` -- one row per invocation, written by
  `write_report()`, the report-only stage-1 finalizer.

Both share the SD-93 flock/append idiom (`dispatch_degradation.py`) but are
a separate ledger, and unlike SD-93's writer, `record_rejection()` never
swallows a write failure -- it returns a typed
`("launch-tuple-evidence-unrecorded", detail)` pair instead (§5.5).
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dispatch_contract import resolve_agent_home  # noqa: E402
from dispatch_contract import resolve_dispatch_state_root  # noqa: E402

SCHEMA_VERSION = 1
REJECTION_CLASSES = frozenset({
    "allocation-skip", "candidate-unsupported", "sealed-parent-not-live",
})
TUPLE_FIELDS = (
    "parent_harness", "parent_transport", "parent_sandbox",
    "child_harness", "launch_authority",
)
_LOCK_DEADLINE_SECONDS = 0.25


def _home() -> str:
    return str(resolve_agent_home())


def _launch_tuple_root(state_root: str | Path) -> Path:
    return Path(state_root) / "launch-tuple"


def _route_filename(route_id: str | None) -> str:
    return f"{route_id}.jsonl" if isinstance(route_id, str) and route_id else "_unattributed.jsonl"


def _parse_rows(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _read_plain(root: Path, filename: str) -> list[dict]:
    """Unlocked read for the pure-query surface (`spent_tuples`, §5.4) --
    never creates a `.lock` file, never blocks, tolerates a torn trailing
    line from a concurrent writer by skipping it."""

    path = root / filename
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _parse_rows(text)


def _append_locked_dedup(root: Path, filename: str, event_id: str, row: dict) -> Path | None:
    """Shared flock+read+append critical section: holds the lock across the
    convergence check (§5.3) and the append itself so a concurrent duplicate
    write can never race past the dedup check."""

    os.makedirs(root, exist_ok=True)
    path = root / filename
    deadline = time.monotonic() + _LOCK_DEADLINE_SECONDS
    with open(str(path) + ".lock", "a+") as lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError) as exc:
                if getattr(exc, "errno", None) not in (errno.EACCES, errno.EAGAIN) or time.monotonic() >= deadline:
                    return None
                time.sleep(0.005)
        try:
            if path.is_file():
                for existing in _parse_rows(path.read_text(encoding="utf-8", errors="replace")):
                    if existing.get("event_id") == event_id:
                        return path
            payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return path


def _append_locked(root: Path, filename: str, payload: bytes) -> Path | None:
    """Shared flock+append idiom (`dispatch_degradation.py`'s idiom, replicated
    per §5.1 -- this module deliberately does not import that sibling). Used
    by `write_report()`, which has no convergence/dedup requirement."""

    os.makedirs(root, exist_ok=True)
    path = root / filename
    deadline = time.monotonic() + _LOCK_DEADLINE_SECONDS
    with open(str(path) + ".lock", "a+") as lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError) as exc:
                if getattr(exc, "errno", None) not in (errno.EACCES, errno.EAGAIN) or time.monotonic() >= deadline:
                    return None
                time.sleep(0.005)
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return path


def _event_id(prefix: str, identity: list) -> str:
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    return f"{prefix}-{digest}"


def rejection_event_id(route_id, route_node, tuple_key, owner_attempt_id, rejection_class) -> str:
    """§5.3 -- the sole convergence key: same tuple, same rejection_class,
    same owner attempt always yields the same event_id, so a repeated
    invocation by the same owner attempt never grows the ledger (B47-9)."""

    return _event_id(
        "lt", [route_id, route_node, tuple_key, owner_attempt_id, rejection_class]
    )


def record_rejection(
    state_root: str | Path,
    *,
    route: dict,
    node: dict,
    tuple_key: str,
    rejection_class: str,
    evidence_ref: str,
    owner_attempt_id: str,
) -> Path | tuple[str, str]:
    """Producer. §5.5: failure is never swallowed -- returns a typed
    `("launch-tuple-evidence-unrecorded", detail)` pair instead of raising or
    silently returning ``None``."""

    if rejection_class not in REJECTION_CLASSES:
        return ("launch-tuple-rejection-class-invalid", f"unknown rejection_class={rejection_class!r}")
    route_id = route.get("route_id") if isinstance(route, dict) else None
    route_node = node.get("id") if isinstance(node, dict) else None
    route_hash = route.get("route_hash") if isinstance(route, dict) else None
    owner_attempt_id = owner_attempt_id if isinstance(owner_attempt_id, str) and owner_attempt_id else "-"
    try:
        event_id = rejection_event_id(route_id, route_node, tuple_key, owner_attempt_id, rejection_class)
        root = _launch_tuple_root(state_root)
        filename = _route_filename(route_id)
        row = {
            "schema_version": SCHEMA_VERSION,
            "route_id": route_id, "route_node": route_node, "route_hash": route_hash,
            "owner_attempt_id": owner_attempt_id,
            "tuple_key": tuple_key,
            "rejection_class": rejection_class,
            "evidence_ref": evidence_ref,
            "observed_at": time.time(),
            "event_id": event_id,
        }
        written = _append_locked_dedup(root, filename, event_id, row)
        if written is None:
            return ("launch-tuple-evidence-unrecorded", "write-failed")
        return written
    except BaseException as exc:  # noqa: BLE001 - typed refusal, never a crash
        return ("launch-tuple-evidence-unrecorded", f"{type(exc).__name__}:{exc}")


def spent_tuples(
    state_root: str | Path, route_id: str, node_id: str, *, route_hash: str | None
) -> dict[str, dict]:
    """Pure query (§5.4) -- reads only, never writes, never touches state
    outside its arguments. Record absence is `unspent`, not `spent`
    (absence is "no judgment", not "healthy")."""

    root = _launch_tuple_root(state_root)
    rows = _read_plain(root, _route_filename(route_id))
    spent: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("schema_version") != SCHEMA_VERSION:
            continue
        if row.get("route_id") != route_id or row.get("route_node") != node_id:
            continue
        if row.get("route_hash") != route_hash:
            continue
        key = row.get("tuple_key")
        if not isinstance(key, str):
            continue
        parts = key.split("/")
        if len(parts) != len(TUPLE_FIELDS) or any(not part for part in parts):
            continue
        spent[key] = row
    return spent


@dataclass
class ReportOnlyObservation:
    """Accumulator for report-only stage 1 (§6). ②-C's `_dispatch()` arms it
    once before the candidate loop and reads/writes only these fields --
    `failed_tuples` is set the same way as `universe`/`spent`: a plain
    attribute assignment after `arm()`, never through a new parameter that
    would widen this dataclass's constructor surface.
    """

    armed: bool = False
    state_root: str | Path | None = None
    route_id: str | None = None
    route_node: str | None = None
    route_hash: str | None = None
    owner_attempt_id: str | None = None
    universe: list[str] = field(default_factory=list)
    spent: dict[str, dict] = field(default_factory=dict)
    failed_tuples: frozenset[str] = field(default_factory=frozenset)
    unrecorded: int = 0
    observed_at: float | None = None

    def arm(self, state_root: str | Path, route: dict, node: dict, owner_attempt_id: str) -> None:
        self.armed = True
        self.state_root = state_root
        self.route_id = route.get("route_id") if isinstance(route, dict) else None
        self.route_node = node.get("id") if isinstance(node, dict) else None
        self.route_hash = route.get("route_hash") if isinstance(route, dict) else None
        self.owner_attempt_id = owner_attempt_id if isinstance(owner_attempt_id, str) and owner_attempt_id else "-"

    def note_unrecorded(self, detail: str) -> None:
        self.unrecorded += 1


def write_report(observation: "ReportOnlyObservation") -> Path | tuple[str, str] | None:
    """report-only finalizer (§6.2). ``armed=False`` is a no-op returning
    ``None``. Self-failure is typed to stderr only -- it must never raise,
    because the caller runs this from a `finally` block where an exception
    would corrupt the real exit code (B47-11)."""

    if not observation.armed:
        return None
    try:
        spent_seen = sum(1 for key in observation.universe if key in observation.spent)
        suppression_candidates = sum(
            1 for key in observation.universe
            if key in observation.spent and key not in observation.failed_tuples
        )
        observed_at = time.time()
        observation.observed_at = observed_at
        route_id = observation.route_id
        row = {
            "schema_version": SCHEMA_VERSION,
            "route_id": route_id,
            "route_node": observation.route_node,
            "route_hash": observation.route_hash,
            "owner_attempt_id": observation.owner_attempt_id,
            "observed_at": observed_at,
            "spent_seen": spent_seen,
            "suppression_candidates": suppression_candidates,
            "unrecorded": observation.unrecorded,
        }
        row["event_id"] = _event_id(
            "ltr", [route_id, observation.route_node, observation.owner_attempt_id, observed_at]
        )
        payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        root = _launch_tuple_root(observation.state_root) / "_report"
        filename = _route_filename(route_id)
        try:
            written = _append_locked(root, filename, payload)
        except OSError:
            written = None
        if written is None:
            print("launch-tuple report-only: write-failed", file=sys.stderr)
            return ("launch-tuple-report-unrecorded", "write-failed")
        print(
            f"launch-tuple report-only: spent_seen={spent_seen} "
            f"suppression_candidates={suppression_candidates} unrecorded={observation.unrecorded}",
            file=sys.stderr,
        )
        return written
    except BaseException as exc:  # noqa: BLE001 - finalizer must never raise
        print(f"launch-tuple report-only: {type(exc).__name__}:{exc}", file=sys.stderr)
        return ("launch-tuple-report-unrecorded", f"{type(exc).__name__}:{exc}")
