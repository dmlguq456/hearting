"""SD-119 owner-resume census leaf module (plan.md §4).

`utilities/claude-session-supervisor.py:346-347` `emit()` is stdout-only
(`print(json.dumps(...), flush=True)`); no `dispatch.supervisor.parked`-family
event survives the process. SD-119's `runtime_joins` (A-4) requires a durable
census of owner-resume events, so this module is that durable surface -- the
only one -- for `subsession_owner_resume_v1`.

Records live under `<dispatch_state_root>/subsession-resume/<chain_id>.jsonl`
(append-only, flock), the same shape as `dispatch_budget_record.py`
(`_canonical`/`_event_id`/`_ledger_path`/`_with_lock`/`_append`). This module
is a dependency leaf: it imports only `dispatch_contract`'s resolvers.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dispatch_contract import resolve_dispatch_state_root  # noqa: E402,F401

SCHEMA_VERSION = 1
EVENT_TYPE = "subsession_owner_resume_v1"
_LOCK_DEADLINE_SECONDS = 0.25


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_id(row: dict) -> str:
    payload = {key: value for key, value in row.items() if key != "event_id"}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _ledger_path(state_root, chain_id: str) -> Path:
    return Path(state_root) / "subsession-resume" / f"{chain_id}.jsonl"


def _parse_lines(text: str) -> list:
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


def _with_lock(path: Path, fn):
    os.makedirs(path.parent, exist_ok=True)
    lock_path = Path(str(path) + ".lock")
    deadline = time.monotonic() + _LOCK_DEADLINE_SECONDS
    with open(str(lock_path), "a+") as lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError) as exc:
                if getattr(exc, "errno", None) not in (errno.EACCES, errno.EAGAIN) or time.monotonic() >= deadline:
                    return None
                time.sleep(0.005)
        try:
            return fn()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def read_rows(state_root, chain_id: str) -> tuple:
    path = _ledger_path(state_root, chain_id)
    if not path.is_file():
        return ()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    return tuple(
        row for row in _parse_lines(text)
        if isinstance(row, dict) and row.get("schema_version") == SCHEMA_VERSION
        and row.get("event_type") == EVENT_TYPE
    )


def _append(path: Path, row: dict) -> bool:
    try:
        payload = (_canonical(row) + "\n").encode("utf-8")
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def record_resume(
    state_root, *, route_id, route_hash, route_node, chain_id, manifest_sha256,
    delivery_id, now=None,
) -> tuple:
    """CAS append keyed on `delivery_id`. A replayed `delivery_id` dedupes to
    the existing row -- the census (`unique_delivery_ids`) never double-counts
    the same aggregate owner-resume delivery."""

    path = _ledger_path(state_root, chain_id)

    def _do():
        for existing in read_rows(state_root, chain_id):
            if existing.get("delivery_id") == delivery_id:
                return (True, "subsession-owner-resume-replayed")
        row = {
            "schema_version": SCHEMA_VERSION,
            "event_type": EVENT_TYPE,
            "route_id": route_id,
            "route_hash": route_hash,
            "route_node": route_node,
            "chain_id": chain_id,
            "manifest_sha256": manifest_sha256,
            "delivery_id": delivery_id,
            "ts": _rfc3339(_now(now)),
        }
        row["event_id"] = _event_id(row)
        if not _append(path, row):
            return (False, "subsession-owner-resume-unrecorded:write-failed")
        return (True, "")

    result = _with_lock(path, _do)
    if result is None:
        return (False, "subsession-owner-resume-unrecorded:lock-unavailable")
    return result


def unique_delivery_ids(state_root, chain_id: str) -> int:
    """`runtime_joins` derives from this count, and ONLY this count (A-4) --
    never a supervisor-local or hardcoded constant."""

    return len({row["delivery_id"] for row in read_rows(state_root, chain_id)})
