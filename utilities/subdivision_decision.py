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

def _gap_path(ledger_path: str | Path) -> Path:
    return Path(str(ledger_path)[:-6] + ".gaps.jsonl") if str(ledger_path).endswith(".jsonl") else Path(str(ledger_path) + ".gaps")

def _action_identity() -> str:
    """One identity per lookup occurrence.

    The spec fixes `event_id` at the owner's permission lookup, so it must not
    move for the rest of that one action -- but two *different* actions on the
    same route/node (a refused plan, then a corrected one) are different
    decisions and must not collide. Route/node/writer/action alone made them
    identical, which let a later, contradicting record be swallowed as a
    duplicate. The attempt id and pid make the identity attributable; the
    monotonic clock and random tail keep two lookups in one process apart.
    """

    return ":".join((
        os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") or "-",
        str(os.getpid()), str(time.monotonic_ns()), os.urandom(8).hex(),
    ))

def _context(route: dict, node: dict, *, writer: str, action: str, jobs=None,
             action_identity: str | None = None) -> dict:
    permission = node.get("subdivision")
    payload = {"route_id": route.get("route_id"), "route_hash": route.get("route_hash"),
               "route_node": node.get("id"), "capability": route.get("capability"),
               "requested_intensity": route.get("requested_intensity", route.get("intensity")),
               "effective_intensity": route.get("effective_intensity"), "permission": permission,
               "writer": writer, "action": action}
    identity = action_identity or _action_identity()
    event_id = hashlib.sha256(_canonical({**payload, "action_identity": identity}).encode()).hexdigest()
    return {**payload, "event_id": event_id, "action_identity": identity,
            "ledger_path": str(_path(str(route.get("route_id")), jobs))}

def lookup(route: dict, node: dict, jobs=None, writer="subdivision", action="admit",
           action_identity: str | None = None) -> dict:
    """Fix one `event_id` for this action. Pass `action_identity` only to rebuild
    the *same* context in another process (a resumed start of one admission)."""

    return _context(route, node, writer=writer, action=action, jobs=jobs,
                    action_identity=action_identity)

# The decision content of a row: everything that makes two records the same
# record. `ts`, `phase` and `evidence_digest` are excluded -- a replay of one
# decision differs in when it was written, not in what it decided.
_CONTENT_KEYS = ("event_id", "route_id", "route_hash", "route_node", "capability",
                 "requested_intensity", "effective_intensity", "permission",
                 "decision", "reason", "manifest_sha256", "slice_count", "writer")


def _content_digest(row: dict) -> str:
    return hashlib.sha256(_canonical({key: row.get(key) for key in _CONTENT_KEYS}).encode()).hexdigest()


def _row(context, decision, reason, manifest_sha256, slice_count, phase: int = 0):
    if decision not in DECISIONS or reason not in REASONS or reason not in _PAIRS[decision]:
        raise DecisionError("invalid-subdivision-decision")
    row = {"schema_version": SCHEMA_VERSION, "event_id": context["event_id"], "ts": time.time(),
           "route_id": context.get("route_id"), "route_hash": context.get("route_hash"),
           "route_node": context.get("route_node"), "capability": context.get("capability"),
           "requested_intensity": context.get("requested_intensity"), "effective_intensity": context.get("effective_intensity"),
           "permission": context.get("permission"), "decision": decision, "reason": reason,
           "manifest_sha256": manifest_sha256, "slice_count": slice_count, "writer": context.get("writer")}
    if phase:
        row["phase"] = phase
    row["evidence_digest"] = hashlib.sha256(_canonical({k:v for k,v in row.items() if k != "evidence_digest"}).encode()).hexdigest()
    return row

def commit(context: dict, decision: str, reason: str, manifest_sha256: str | None = None, slice_count: int = 0) -> dict:
    """Append this action's decision exactly once.

    Dedup is *exact replay only*: the same event with the same content is the
    same record and is not written twice. The same `event_id` carrying a
    different decision, reason, manifest or slice count is NOT a replay -- the
    first decision is kept (append-only, never overwritten) and the new one is
    appended as a later `phase` of that event with `conflict=True`, so a
    disagreement can never be reported to a caller as a silent duplicate PASS.
    """

    row = _row(context, decision, reason, manifest_sha256, slice_count)
    path = Path(context["ledger_path"])
    result = {"event_id": context["event_id"], "appended": False, "duplicate": False,
              "conflict": False, "warning": None}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path) + ".lock", "a+") as lock:
            if not _lock(lock):
                _note_gap(path, row, "lock-unavailable")
                result["warning"] = "subdivision-decision-unrecorded"
                return result
            try:
                prior = []
                if path.is_file():
                    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                        try:
                            existing = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(existing, dict) and existing.get("event_id") == row["event_id"]:
                            prior.append(existing)
                if any(_content_digest(existing) == _content_digest(row) for existing in prior):
                    result["duplicate"] = True
                    return result
                if prior:
                    result["conflict"] = True
                    result["warning"] = "subdivision-decision-conflict"
                    result["initial_decision"] = prior[0].get("decision")
                    result["initial_reason"] = prior[0].get("reason")
                    row = _row(context, decision, reason, manifest_sha256, slice_count,
                               phase=len(prior))
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                os.write(fd, (_canonical(row) + "\n").encode()); os.close(fd)
                result["appended"] = True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except (OSError, TypeError, ValueError) as exc:
        _note_gap(path, row, str(exc))
        result["warning"] = "subdivision-decision-unrecorded"
        result["detail"] = str(exc)
    return result


# Fail-open is harmless to dispatch, but a lost record must not later read as
# "no decision was attempted" (M-3). Every failed append leaves a gap marker
# beside the ledger, and -- when even that cannot be written -- in this
# process, so `inventory()` can say `inventory_complete=false` instead of
# reporting a file that happens to parse as complete.
_UNRECORDED: dict[str, list[dict[str, Any]]] = {}


def _note_gap(path: Path, row: dict, detail: str) -> None:
    gap = {"schema_version": SCHEMA_VERSION, "event_id": row.get("event_id"),
           "ts": time.time(), "route_id": row.get("route_id"),
           "route_node": row.get("route_node"), "writer": row.get("writer"),
           "warning": "subdivision-decision-unrecorded", "detail": detail}
    _UNRECORDED.setdefault(str(path), []).append(gap)
    try:
        gap_path = _gap_path(path)
        gap_path.parent.mkdir(parents=True, exist_ok=True)
        with open(gap_path, "a", encoding="utf-8") as handle:
            handle.write(_canonical(gap) + "\n")
    except OSError:
        return

def _lock(lock) -> bool:
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return True
    except OSError:
        return False

def _observed_gaps(path: Path) -> tuple[list[dict], bool]:
    """Gap markers for this ledger: durable ones first, then this process's.

    The bool is `unreadable`: a gap file that exists but cannot be read is
    itself an observation failure, not an absence of gaps.
    """

    gaps: list[dict] = []
    gap_path = _gap_path(path)
    unreadable = False
    try:
        if gap_path.is_file():
            for line in gap_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip(): continue
                try: gaps.append(json.loads(line))
                except ValueError: unreadable = True
    except OSError:
        unreadable = True
    gaps.extend(_UNRECORDED.get(str(path), ()))
    return gaps, unreadable


def inventory(context: dict) -> dict:
    """Read the ledger for one route.

    `inventory_complete` is true only when every decision this root knows about
    is present and parseable. A missing file with no gap marker is `absent`
    ("no attempt observed"); a missing or parseable file *with* gap markers is
    `incomplete` ("a decision was attempted and its record was lost") -- the
    two must never be reported as the same state, and neither is complete.
    """

    path = Path(context["ledger_path"])
    gaps, gaps_unreadable = _observed_gaps(path)
    if not path.exists():
        if gaps or gaps_unreadable:
            return {"health": "incomplete", "inventory_complete": False,
                    "warning": "subdivision-decision-record-gap", "rows": [], "gaps": gaps}
        return {"health": "absent", "inventory_complete": False, "warning": "no-record-observed", "rows": [], "gaps": []}
    try:
        rows, corrupt = [], False
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip(): continue
            try: rows.append(json.loads(line))
            except ValueError: corrupt = True
        if corrupt:
            return {"health": "corrupt", "inventory_complete": False,
                    "warning": "subdivision-decision-ledger-corrupt", "rows": rows, "gaps": gaps}
        if gaps or gaps_unreadable:
            return {"health": "incomplete", "inventory_complete": False,
                    "warning": "subdivision-decision-record-gap", "rows": rows, "gaps": gaps}
        return {"health": "healthy", "inventory_complete": True, "warning": None, "rows": rows, "gaps": []}
    except OSError as exc:
        return {"health": "unreadable", "inventory_complete": False, "warning": "subdivision-decision-ledger-unreadable", "detail": str(exc), "rows": [], "gaps": gaps}
