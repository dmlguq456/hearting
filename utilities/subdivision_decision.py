#!/usr/bin/env python3
"""SD-119 subdivision decision ledger (append-only, fail-open observation)."""
from __future__ import annotations

import errno, fcntl, hashlib, json, os, time
from pathlib import Path
from typing import Any

from dispatch_contract import resolve_agent_home, resolve_dispatch_state_root

SCHEMA_VERSION = 1
DECISIONS = frozenset({"not-eligible", "considered-declined", "refused", "admitted"})
REASONS = frozenset({
    "subdivision-not-permitted", "intensity-below-min", "surface-unreachable",
    "owner-declined-not-separable", "plan-declared-no-slices", "slice-count-out-of-range",
    "disjointness-unproven", "fixed-file-outside-scope", "fixed-file-not-exact",
    "fixed-file-overlap", "baseline-unavailable", "scope-unproven",
    "governor-capacity-insufficient", "artifact-base-invalid", "artifact-root-unavailable",
    "artifact-scan-cap-exceeded", "",
})
_PAIRS = {
    "not-eligible": {"subdivision-not-permitted", "intensity-below-min", "surface-unreachable"},
    "considered-declined": {"owner-declined-not-separable", "plan-declared-no-slices", "slice-count-out-of-range"},
    "refused": {"disjointness-unproven", "fixed-file-outside-scope", "fixed-file-not-exact", "fixed-file-overlap", "baseline-unavailable", "scope-unproven", "governor-capacity-insufficient", "artifact-base-invalid", "artifact-root-unavailable", "artifact-scan-cap-exceeded"},
    "admitted": {""},
}

class DecisionError(ValueError):
    pass

def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def _path(route_id: str, jobs: str | Path | None) -> Path:
    state = resolve_dispatch_state_root(resolve_agent_home(), explicit_jobs=jobs)
    return Path(state) / "subdivision" / f"{route_id}.jsonl"

def _context(route: dict, node: dict, *, writer: str, action: str, jobs=None) -> dict:
    permission = node.get("subdivision")
    payload = {"route_id": route.get("route_id"), "route_hash": route.get("route_hash"),
               "route_node": node.get("id"), "capability": route.get("capability"),
               "requested_intensity": route.get("requested_intensity", route.get("intensity")),
               "effective_intensity": route.get("effective_intensity"), "permission": permission,
               "writer": writer, "action": action}
    event_id = hashlib.sha256(_canonical(payload).encode()).hexdigest()
    return {**payload, "event_id": event_id, "ledger_path": str(_path(str(route.get("route_id")), jobs))}

def lookup(route: dict, node: dict, jobs=None, writer="subdivision", action="admit") -> dict:
    return _context(route, node, writer=writer, action=action, jobs=jobs)

def _row(context, decision, reason, manifest_sha256, slice_count):
    if decision not in DECISIONS or reason not in REASONS or reason not in _PAIRS[decision]:
        raise DecisionError("invalid-subdivision-decision")
    row = {"schema_version": SCHEMA_VERSION, "event_id": context["event_id"], "ts": time.time(),
           "route_id": context.get("route_id"), "route_hash": context.get("route_hash"),
           "route_node": context.get("route_node"), "capability": context.get("capability"),
           "requested_intensity": context.get("requested_intensity"), "effective_intensity": context.get("effective_intensity"),
           "permission": context.get("permission"), "decision": decision, "reason": reason,
           "manifest_sha256": manifest_sha256, "slice_count": slice_count, "writer": context.get("writer")}
    row["evidence_digest"] = hashlib.sha256(_canonical({k:v for k,v in row.items() if k != "evidence_digest"}).encode()).hexdigest()
    return row

def commit(context: dict, decision: str, reason: str, manifest_sha256: str | None = None, slice_count: int = 0) -> dict:
    row = _row(context, decision, reason, manifest_sha256, slice_count)
    path = Path(context["ledger_path"])
    result = {"event_id": context["event_id"], "appended": False, "duplicate": False, "warning": None}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path) + ".lock", "a+") as lock:
            if not _lock(lock):
                result["warning"] = "subdivision-decision-unrecorded"
                return result
            try:
                if path.is_file():
                    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                        try:
                            if json.loads(line).get("event_id") == row["event_id"]:
                                result["duplicate"] = True
                                return result
                        except (ValueError, AttributeError):
                            continue
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                os.write(fd, (_canonical(row) + "\n").encode()); os.close(fd)
                result["appended"] = True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except (OSError, TypeError, ValueError) as exc:
        result["warning"] = "subdivision-decision-unrecorded"
        result["detail"] = str(exc)
    return result

def _lock(lock) -> bool:
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return True
    except OSError:
        return False

def inventory(context: dict) -> dict:
    path = Path(context["ledger_path"])
    if not path.exists():
        return {"health": "absent", "inventory_complete": False, "warning": "no-record-observed", "rows": []}
    try:
        rows, corrupt = [], False
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip(): continue
            try: rows.append(json.loads(line))
            except ValueError: corrupt = True
        return {"health": "corrupt" if corrupt else "healthy", "inventory_complete": not corrupt,
                "warning": "subdivision-decision-ledger-corrupt" if corrupt else None, "rows": rows}
    except OSError as exc:
        return {"health": "unreadable", "inventory_complete": False, "warning": "subdivision-decision-ledger-unreadable", "detail": str(exc), "rows": []}
