"""Best-effort append-only ledger for fallback degradation evidence (SD-93)."""
import errno
import fcntl
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dispatch_contract import resolve_agent_home as _resolve_agent_home  # noqa: E402
from dispatch_contract import resolve_dispatch_state_root as _resolve_dispatch_state_root  # noqa: E402
from dispatch_contract import bounded_candidate_outcomes, candidate_detail, diagnostic_token  # noqa: E402

_KINDS = {"degradation", "chain-exhausted", "leg-failure", "writer-unregistered"}
_HOPS = {"same-harness-headless", "cross-harness-headless", "native-subagent", "inline"}
_SURFACES = {"registered-headless", "codex-native-subagent", "claude-subagent", "inline"}
# B47-4: this set is sealed by enumeration, never by a size assertion. An
# unknown writer is not silently dropped; record_degradation() below routes
# it to `_unattributed.jsonl` with kind="writer-unregistered" instead.
_WRITERS = {
    "dispatch_contract.py",
    "stage-dispatch-fallback.py",
    "dispatch-batch.py",
    "capability-route.py",
    "dispatch-reap-watch.py",
    "dispatch_completion_join.py",
}
_OPTIONAL = {"fallback_ordinal", "fleet_visibility", "reason", "detail", "registered_worker", "capability", "completion_gate", "route_file", "parent", "parent_attempt_id", "parent_pid", "parent_pid_start", "harness", "attempt_trace", "prior_attempt_ids", "last_direct_failure", "candidate_outcomes", "candidate_outcomes_omitted", "child_proof", "parallel_group", "parallel_leg_index", "parallel_leg_count", "attempt_id", "exit_code", "launch_state", "event_id", "leg_class", "auxiliary_check", "parent_cross", "cause", "sole_gate", "subsession_id", "slice_manifest_sha256", "tier_note"}

def _home():
    return str(_resolve_agent_home())

def _clip(value, limit):
    if value is None:
        return None
    value = str(value)
    return value if len(value) <= limit else value[:max(0, limit - 1)] + "…"

def _event_id(row):
    identity = [row.get(k) for k in ("kind", "route_id", "route_node", "attempt_id", "parallel_leg_index", "parallel_leg_count", "fallback_ordinal", "writer")]
    return "dg-" + hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:24]

def _append_locked(root, filename, payload):
    """Shared flock+append idiom; returns the written path or None on any failure."""
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, filename)
    deadline = time.monotonic() + 0.25
    with open(path + ".lock", "a+") as lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError) as exc:
                if getattr(exc, "errno", None) not in (errno.EACCES, errno.EAGAIN) or time.monotonic() >= deadline:
                    return None
                time.sleep(0.005)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return path

def record_degradation(*, route_id=None, route_node=None, route_hash=None, dispatch_depth=0, fallback_hop=None, execution_surface=None, writer=None, kind="degradation", agent_home=None, jobs=None, **fields):
    """Append one bounded record; every failure is intentionally swallowed."""
    try:
        root = str(_resolve_dispatch_state_root(agent_home or _home(), jobs) / "degradations")
        if writer not in _WRITERS:
            # B47-4: an unregistered writer is a contract violation, not a
            # write failure -- it still leaves an observable trace, just not
            # in the writer's normal shard.
            unattributed_row = {
                "schema_version": 1, "kind": "writer-unregistered", "ts": time.time(),
                "route_id": route_id if isinstance(route_id, str) else None,
                "route_node": route_node if isinstance(route_node, str) else None,
                "route_hash": route_hash if isinstance(route_hash, str) else None,
                "writer": writer,
            }
            unattributed_row["event_id"] = _event_id(unattributed_row)
            unattributed_payload = (json.dumps(unattributed_row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            try:
                _append_locked(root, "_unattributed.jsonl", unattributed_payload)
            except BaseException:
                pass
            return None
        depth = int(dispatch_depth)
        if depth not in {1, 2} or kind not in _KINDS or (fallback_hop is not None and fallback_hop not in _HOPS):
            return None
        row = {"schema_version": 1, "kind": kind, "ts": time.time(), "route_id": route_id if isinstance(route_id, str) else None, "route_node": route_node if isinstance(route_node, str) else None, "route_hash": route_hash if isinstance(route_hash, str) else None, "dispatch_depth": depth, "fallback_hop": fallback_hop, "execution_surface": execution_surface if execution_surface in _SURFACES else None, "writer": writer}
        for key in _OPTIONAL:
            if key in fields and fields[key] is not None:
                row[key] = fields[key]
        if "reason" in row:
            reason = row["reason"]
            if isinstance(reason, str) and len(reason) > 160:
                token = diagnostic_token(reason, 159)
                row["reason"] = token + "…" if len(token) == 159 else ""
            else:
                row["reason"] = diagnostic_token(reason)
        if "detail" in row:
            row["detail"] = candidate_detail(row.get("reason", ""), row["detail"])
        if "attempt_trace" in row:
            row["attempt_trace"] = _clip(row["attempt_trace"], 2048)
        if "candidate_outcomes" in row:
            row["candidate_outcomes"], row["candidate_outcomes_omitted"] = bounded_candidate_outcomes(
                row["candidate_outcomes"], row.get("candidate_outcomes_omitted", 0))
        if "last_direct_failure" in row:
            if not isinstance(row["last_direct_failure"], str):
                return None
            row["last_direct_failure"] = diagnostic_token(row["last_direct_failure"], 256)
        row["event_id"] = fields.get("event_id") or _event_id(row)
        payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        filename = route_id + ".jsonl" if isinstance(route_id, str) and route_id else "_unattributed.jsonl"
        return _append_locked(root, filename, payload)
    except BaseException:
        return None


def route_rows(route_id, *, agent_home=None, jobs=None):
    """The ledger rows recorded for one route, oldest first; [] when there are none or the ledger is unreadable."""
    if not isinstance(route_id, str) or not route_id or "/" in route_id or route_id.startswith("."):
        return []
    try:
        path = _resolve_dispatch_state_root(agent_home or _home(), jobs) / "degradations" / (route_id + ".jsonl")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # an unresolvable root or a missing ledger reads as no rows
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("route_id") == route_id:
            rows.append(row)
    return rows
