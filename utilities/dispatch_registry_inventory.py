"""SD-115 registry retention & inventory leaf module (plan.md §4.3).

A dependency leaf like `dispatch_launch_tuple.py`: this module imports only
`dispatch_contract`'s resolvers and nothing else `dispatch_*`, so it can be
imported from `tools/install/distribution.py`'s runtime siblings and from
`dispatch-registry.py` without risk of a circular import.

Records live under `<dispatch_state_root>/inventory/` (horizon + gaps) and
`<dispatch_state_root>/archive/<archive_id>/` (read-only historical import).
Lock nesting order, whenever more than one lock would need to be held at
once, is fixed as `horizon.lock -> gaps.lock -> archive/<id>.lock`; no
operation in this module acquires more than one lock at a time today, but
the order is pinned here so a future addition cannot invert it.

Archive rows are a one-time, read-only import of a historical corpus
(SD-115 axis 2, §13.34.3-(3)): `inventory_query(..., include_archive=True)`
tags each archived row `source="archive"` and callers MUST NOT feed those
rows into any liveness/terminal/marker judgment -- they only answer
historical queries.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dispatch_contract import resolve_agent_home  # noqa: E402
from dispatch_contract import resolve_dispatch_state_root  # noqa: E402

SCHEMA_VERSION = 1
DISCOVERED_BY = frozenset({"gap-census", "forced-prune"})
_LOCK_DEADLINE_SECONDS = 0.25


@dataclass(frozen=True)
class InventoryResult:
    """§13.34.3-(4): deliberately defines no `__int__`/`__len__`/`__index__`
    so a caller can never silently coerce this result to a bare number."""

    rows: tuple
    inventory_complete: bool
    reasons: tuple


def _home() -> str:
    return str(resolve_agent_home())


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_id(row: dict) -> str:
    payload = {key: value for key, value in row.items() if key != "event_id"}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _short_hex(*parts, length: int = 16) -> str:
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return digest[:length]


def _inventory_root(state_root) -> Path:
    return Path(state_root) / "inventory"


def _archive_root(state_root) -> Path:
    return Path(state_root) / "archive"


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


def _read_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _parse_lines(text)


def _with_lock(lock_path: Path, fn):
    os.makedirs(lock_path.parent, exist_ok=True)
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


def _append_line(path: Path, row: dict) -> bool:
    try:
        os.makedirs(path.parent, exist_ok=True)
        payload = (_canonical(row) + "\n").encode("utf-8")
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def read_horizon(state_root):
    return _read_json(_inventory_root(state_root) / "horizon.json")


def record_horizon(
    state_root, *, root_epoch, first_complete_observation, evidence_digest,
    cited_ledger_snapshot_digest, now=None,
) -> tuple:
    root = _inventory_root(state_root)
    lock_path = root / "horizon.lock"

    def _do():
        previous = read_horizon(state_root)
        row = {
            "schema_version": SCHEMA_VERSION,
            "previous": {
                "root_epoch": previous.get("root_epoch") if previous else None,
                "first_complete_observation": previous.get("first_complete_observation") if previous else None,
                "evidence_digest": previous.get("evidence_digest") if previous else None,
            },
            "next": {
                "root_epoch": root_epoch,
                "first_complete_observation": first_complete_observation,
                "evidence_digest": evidence_digest,
            },
            "cited_ledger_snapshot_digest": cited_ledger_snapshot_digest,
            "recorded_at": _rfc3339(_now(now)),
        }
        row["event_id"] = _event_id(row)
        # Provenance append is the precondition for the horizon.json write
        # (C47-3): a horizon move is never observable without its evidence.
        if not _append_line(root / "horizon-provenance.jsonl", row):
            return ("horizon-provenance-unrecorded", "provenance-write-failed")
        document = {
            "schema_version": SCHEMA_VERSION,
            "root_epoch": root_epoch,
            "first_complete_observation": first_complete_observation,
            "evidence_digest": evidence_digest,
            "recorded_at": row["recorded_at"],
        }
        try:
            os.makedirs(root, exist_ok=True)
            tmp_path = root / "horizon.json.tmp"
            tmp_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, root / "horizon.json")
        except OSError as exc:
            return ("horizon-provenance-unrecorded", f"horizon-write-failed:{exc}")
        return ("", "")

    result = _with_lock(lock_path, _do)
    if result is None:
        return ("horizon-provenance-unrecorded", "lock-unavailable")
    return result


def record_gap(
    state_root, *, from_ts, to_ts, evidence_digest, cited_ledger_snapshot_digest,
    recoverable, discovered_by, now=None,
) -> tuple:
    if discovered_by not in DISCOVERED_BY:
        return ("registry-gap-unrecorded", f"unknown discovered_by={discovered_by!r}")
    root = _inventory_root(state_root)
    lock_path = root / "gaps.lock"

    def _do():
        gap_id = f"gap-{_short_hex(from_ts, to_ts, evidence_digest)}"
        row = {
            "schema_version": SCHEMA_VERSION,
            "gap_id": gap_id,
            "from_ts": from_ts,
            "to_ts": to_ts,
            "evidence_digest": evidence_digest,
            "cited_ledger_snapshot_digest": cited_ledger_snapshot_digest,
            "recoverable": bool(recoverable),
            "discovered_by": discovered_by,
            "recorded_at": _rfc3339(_now(now)),
        }
        row["event_id"] = _event_id(row)
        if not _append_line(root / "gaps.jsonl", row):
            return ("registry-gap-unrecorded", "write-failed")
        return ("", "")

    result = _with_lock(lock_path, _do)
    if result is None:
        return ("registry-gap-unrecorded", "lock-unavailable")
    return result


def read_gaps(state_root) -> tuple:
    rows = _read_jsonl(_inventory_root(state_root) / "gaps.jsonl")
    return tuple(
        row for row in rows
        if isinstance(row, dict) and row.get("schema_version") == SCHEMA_VERSION
    )


def import_archive(state_root, source_path, *, now=None) -> tuple:
    source = Path(source_path)
    try:
        content = source.read_bytes()
    except OSError as exc:
        return ("", "registry-archive-import-failed", f"unreadable:{exc}")
    content_digest = "sha256:" + hashlib.sha256(content).hexdigest()
    archive_id = f"arc-{_short_hex(content_digest)}"
    root = _archive_root(state_root) / archive_id
    lock_path = root / "import.lock"

    def _do():
        provenance_path = root / "provenance.json"
        existing = _read_json(provenance_path)
        if existing is not None and existing.get("content_digest") == content_digest:
            # C47-6: a re-import of the same content copies nothing.
            return (archive_id, "", "")
        text = content.decode("utf-8", errors="replace")
        lines = text.splitlines()
        rows_path = root / "rows.jsonl"
        try:
            os.makedirs(root, exist_ok=True)
            with open(str(rows_path), "w", encoding="utf-8") as handle:
                for index, raw in enumerate(lines, start=1):
                    row = {
                        "schema_version": SCHEMA_VERSION, "archive_id": archive_id,
                        "source_line": index, "raw": raw,
                    }
                    handle.write(_canonical(row) + "\n")
        except OSError as exc:
            return ("", "registry-archive-import-failed", f"rows-write-failed:{exc}")
        provenance = {
            "schema_version": SCHEMA_VERSION,
            "archive_id": archive_id,
            "source_path": str(source.resolve()),
            "row_count": len(lines),
            "content_digest": content_digest,
            "imported_at": _rfc3339(_now(now)),
        }
        try:
            tmp_path = root / "provenance.json.tmp"
            tmp_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, provenance_path)
        except OSError as exc:
            return ("", "registry-archive-import-failed", f"provenance-write-failed:{exc}")
        return (archive_id, "", "")

    result = _with_lock(lock_path, _do)
    if result is None:
        return ("", "registry-archive-import-failed", "lock-unavailable")
    return result


def read_archive(state_root, archive_id) -> tuple:
    return tuple(_read_jsonl(_archive_root(state_root) / archive_id / "rows.jsonl"))


def attempt_ids_under(root_path) -> frozenset:
    """Pure: extract the `attempt_id=` set from a `jobs.log`-shaped registry
    file at `<root_path>/jobs.log`. Absence is an empty set, not a failure."""

    jobs = Path(root_path) / "jobs.log"
    if not jobs.is_file():
        return frozenset()
    try:
        text = jobs.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    ids: set = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 6:
            continue
        for item in fields[5].split(","):
            if item.startswith("attempt_id="):
                value = item[len("attempt_id="):]
                if value:
                    ids.add(value)
    return frozenset(ids)


def _intersects(a_from, a_to, b_from, b_to) -> bool:
    return a_from <= b_to and b_from <= a_to


def _archive_rows(state_root) -> list:
    archive_root = _archive_root(state_root)
    if not archive_root.is_dir():
        return []
    rows = []
    for archive_dir in sorted(archive_root.glob("*")):
        for row in read_archive(state_root, archive_dir.name):
            tagged = dict(row)
            tagged["source"] = "archive"
            rows.append(tagged)
    return rows


def inventory_query(state_root, *, from_ts=None, to_ts=None, include_archive=False) -> InventoryResult:
    rows = _archive_rows(state_root) if include_archive else []
    horizon = read_horizon(state_root)
    if horizon is None:
        # Horizon absence marks the *whole* corpus incomplete -- absence is
        # never read as "complete" (§13.34.3-(4)).
        return InventoryResult(rows=tuple(rows), inventory_complete=False, reasons=("horizon-absent",))
    reasons: list = []
    first_complete = horizon.get("first_complete_observation")
    if from_ts is not None and first_complete is not None and from_ts < first_complete:
        reasons.append("before-horizon")
    if from_ts is not None and to_ts is not None:
        for gap in read_gaps(state_root):
            if _intersects(from_ts, to_ts, gap.get("from_ts", ""), gap.get("to_ts", "")):
                reasons.append(f"gap-intersect:{gap.get('gap_id')}")
    complete = len(reasons) == 0
    return InventoryResult(rows=tuple(rows), inventory_complete=complete, reasons=tuple(reasons))
