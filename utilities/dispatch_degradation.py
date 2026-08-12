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

_KINDS = {"degradation", "chain-exhausted", "leg-failure"}
_HOPS = {"same-harness-headless", "cross-harness-headless", "native-subagent", "inline"}
_SURFACES = {"registered-headless", "codex-native-subagent", "claude-subagent", "inline"}
_OPTIONAL = {"fallback_ordinal", "fleet_visibility", "reason", "detail", "registered_worker", "capability", "completion_gate", "route_file", "parent", "parent_attempt_id", "parent_pid", "parent_pid_start", "harness", "attempt_trace", "prior_attempt_ids", "last_direct_failure", "child_proof", "parallel_group", "parallel_leg_index", "parallel_leg_count", "attempt_id", "exit_code", "launch_state", "event_id"}

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

def record_degradation(*, route_id=None, route_node=None, route_hash=None, dispatch_depth=0, fallback_hop=None, execution_surface=None, writer=None, kind="degradation", agent_home=None, jobs=None, **fields):
    """Append one bounded record; every failure is intentionally swallowed."""
    try:
        depth = int(dispatch_depth)
        if depth not in {1, 2} or kind not in _KINDS or writer not in {"stage-dispatch-fallback.py", "dispatch-batch.py"} or (fallback_hop is not None and fallback_hop not in _HOPS):
            return None
        row = {"schema_version": 1, "kind": kind, "ts": time.time(), "route_id": route_id if isinstance(route_id, str) else None, "route_node": route_node if isinstance(route_node, str) else None, "route_hash": route_hash if isinstance(route_hash, str) else None, "dispatch_depth": depth, "fallback_hop": fallback_hop, "execution_surface": execution_surface if execution_surface in _SURFACES else None, "writer": writer}
        for key in _OPTIONAL:
            if key in fields and fields[key] is not None:
                row[key] = fields[key]
        for key, limit in (("reason", 160), ("detail", 512), ("attempt_trace", 2048)):
            if key in row:
                row[key] = _clip(row[key], limit)
        row["event_id"] = fields.get("event_id") or _event_id(row)
        payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        root = str(_resolve_dispatch_state_root(agent_home or _home(), jobs) / "degradations")
        os.makedirs(root, exist_ok=True)
        filename = route_id + ".jsonl" if isinstance(route_id, str) and route_id else "_unattributed.jsonl"
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
    except BaseException:
        return None
