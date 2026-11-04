"""Artifact producer: history lines waiting for the recorder.

Pending history lines on cycle and campaign records, their delivery to
`artifact_history`, and the observation of control files changed by hand.

Split out of `artifact_producer.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import artifact_admission
import artifact_lifecycle
import artifact_locator
import artifact_manifest

from artifact_producer_store import (
    _closed_record, _cycle_rel, _digest, _LayoutScan, _QUIET_HISTORY, _read_json,
    _record_content_digest, _record_cycle_manifest_path, _write_atomic,
    _write_campaign_runtime_record, _write_cycle_record, campaign_dir, CAMPAIGN_RUNTIME_DIR,
    campaign_runtime_record, CONTRACT, cycle_record_path, journal_path, producer_dir,
    ProducerError, read_cycle_record,
)


_HISTORY_TOKEN = re.compile(r"^[\x21-\x7e]{1,128}$")


# -- the history lines of D-125 --------------------------------------------


def _history_module():
    """The one history recorder, or `None` when it is not importable (not merged yet)."""
    try:
        import artifact_history  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 -- a missing or broken recorder is a pending line, never a failure
        return None
    if not all(callable(getattr(artifact_history, name, None)) for name in ("make_event", "publish_events_locked")):
        return None
    return artifact_history


def _history_id(prefix: str, *parts: str) -> str:
    """A stable ID, so that a line delivered twice after a crash is the same line."""
    return prefix + "_" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _history_actor(default_by: str) -> Dict[str, Any]:
    """`make_event` actor keyword, from the recorder's own rules: a person or an agent session through
    `actor_from_env(default_by)`, a runtime that observed a change without seeing who wrote it
    (`rule`) with the session markers it carries.  Without a recorder the line keeps only `actor_by`."""
    module = _history_module()
    if module is None:
        return {"actor_by": default_by}
    try:
        if default_by != "rule":
            return {"actor": module.actor_from_env(default_by)}

        def marker(name: str) -> Optional[str]:
            value = os.environ.get(name)
            return value if isinstance(value, str) and _HISTORY_TOKEN.match(value) else None

        attempt = marker("AGENT_DISPATCH_ATTEMPT_ID")
        return {"actor": module.make_actor(
            "rule", session=attempt, harness=marker("AGENT_DISPATCH_CURRENT_HARNESS"),
            route=marker("AGENT_ROUTE_ID"), attempt=attempt)}
    except Exception:  # noqa: BLE001 -- an actor the recorder cannot build is recorded as the default
        return {"actor_by": default_by}


def _history_by(default_by: str) -> str:
    """Who the recorder says is acting (`human`, `agent`, `rule`), for a record field that names it."""
    actor = _history_actor(default_by)
    return str((actor.get("actor") or {}).get("by") or actor.get("actor_by") or default_by)


def _history_file_ref(row: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if row is None:
        return {"value": None}
    return {"digest": row["content_digest"], "bytes": row["byte_size"]}


def _refresh_history_lines(root: Path, record: Mapping[str, Any], directory: Path,
                           previous: Mapping[str, Any], refreshed: Mapping[str, Any],
                           changes: Mapping[str, Sequence[str]], *, trigger: str,
                           excluded_roots: Sequence[str] = (),
                           now: float) -> List[Dict[str, Any]]:
    """One `make_event` argument set for each file added, changed or removed since the close.

    File content is only ever a digest and a size.  The IDs come from the cycle, the
    new document and the file, and the time is fixed here, so a line that is built again
    for the same change is the same line."""
    old_rows = {row["locator"]["path"]: row for row in previous.get("artifact_revisions", []) or []}
    new_rows = {row["locator"]["path"]: row for row in refreshed.get("artifact_revisions", []) or []}
    cycle_id = record["cycle_id"]
    revision_id = refreshed["manifest_revision_id"]
    transaction_id = _history_id("htxn", "refresh", cycle_id, revision_id)
    actor = _history_actor("rule")
    cycle_rel = Path(os.path.relpath(str(directory), str(root))).as_posix()
    lines: List[Dict[str, Any]] = []
    for operation, key in (("add", "added"), ("update", "modified"), ("delete", "removed")):
        for rel in changes.get(key, []):
            before, after = old_rows.get(rel), new_rows.get(rel)
            target = after or before
            if target is None:
                continue
            lines.append({
                "kind": "artifact", "target_type": "artifact", "target_id": target["artifact_id"],
                "target_path": f"{cycle_rel}/{rel}", "operation": operation, "field": rel,
                "before": _history_file_ref(before), "after": _history_file_ref(after),
                "reason": trigger, "transaction_id": transaction_id,
                "event_id": _history_id("hevt", "refresh", cycle_id, revision_id, operation, rel),
                "now": now, **actor})
    retired = set(changes.get("reclassified", []))
    for rel_root in sorted(set(excluded_roots)):
        paths = [rel for rel in retired if rel == rel_root or rel.startswith(rel_root + "/")]
        if not paths:
            continue
        rows = [old_rows[rel] for rel in paths if rel in old_rows]
        declared_bytes = sum(int(row.get("byte_size", 0)) for row in rows)
        before = {"value": {"kind": "runtime", "rows": len(rows), "declared_bytes": declared_bytes}}
        after = {"value": {"kind": "runtime", "rows": 0, "declared_bytes": 0}}
        lines.append({
            "kind": "artifact", "target_type": "cycle", "target_id": cycle_id,
            "target_path": f"{cycle_rel}/{rel_root}", "operation": "update",
            "field": "excluded_payload", "before": before, "after": after,
            "reason": trigger, "transaction_id": _history_id("htxn", "excluded-payload", cycle_id),
            "event_id": _history_id("hevt", "excluded-payload", cycle_id, rel_root),
            "now": now, **actor})
    return lines


def _close_history_line(root: Path, record: Mapping[str, Any], document: Mapping[str, Any], digest: str,
                        directory: Path, *, excluded: int, now: Optional[float]) -> Dict[str, Any]:
    """The one lifecycle line a first close leaves: state, manifest digest and revision, file count."""
    revision_id = document["manifest_revision_id"]
    route_id = record.get("route_id")
    return {
        "kind": "lifecycle", "target_type": "cycle", "target_id": record["cycle_id"],
        "target_path": Path(os.path.relpath(str(directory), str(root))).as_posix(),
        "operation": "update", "field": "state", "before": {"value": "open"},
        "after": {"value": {"state": document["cycle"]["state"], "manifest_digest": digest,
                            "revision_id": revision_id,
                            "files": len(document.get("artifact_revisions", []) or []), "excluded": excluded}},
        "reason": f"finalize {route_id}" if route_id else "finalize",
        "transaction_id": _history_id("htxn", "close", record["cycle_id"], revision_id),
        "event_id": _history_id("hevt", "close", record["cycle_id"], revision_id),
        "now": time.time() if now is None else float(now), **_history_actor("human")}


def _history_deliver_locked(root: Path, lines: Sequence[Mapping[str, Any]]) -> bool:
    """Hand lines to the recorder while the admission lock is held.

    `False` when there is no recorder or it failed: the caller keeps the lines (and
    must not call a change recorded).  A line the recorder rejects as malformed is
    dropped rather than kept forever: it could never be delivered."""
    if not lines:
        return True
    module = _history_module()
    if module is None:
        return False
    try:
        events = []
        for line in lines:
            try:
                events.append(module.make_event(**dict(line)))
            except Exception:  # noqa: BLE001
                continue
        if events:
            module.publish_events_locked(root, events)
        return True
    except Exception:  # noqa: BLE001 -- the recorder failing never fails the change
        return False


def _merge_pending(carried: Sequence[Mapping[str, Any]], lines: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    seen = {entry.get("event_id") for entry in carried}
    return [dict(entry) for entry in carried] + [dict(line) for line in lines if line.get("event_id") not in seen]


def _with_pending(record: Mapping[str, Any], pending: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    updated = dict(record)
    if pending:
        updated["history_pending"] = [dict(entry) for entry in pending]
    else:
        updated.pop("history_pending", None)
    return updated


def _write_history_pending(root: Path, record: Mapping[str, Any], pending: Sequence[Mapping[str, Any]]) -> None:
    """Rewrite a cycle record with only its `history_pending` changed (the admission lock is held).

    No locator row input is touched, so this is a leaf record write with no index effect."""
    _write_cycle_record(root, _with_pending(record, pending), exclusive=False)


def _deliver_pending(root: Path, cycle_id: str, *, timeout: float) -> str:
    """Hand a cycle record's `history_pending` lines to the recorder and clear what was taken."""
    if _history_module() is None:
        return "pending"
    fd = artifact_admission._acquire_lock(root, timeout)
    try:
        record = read_cycle_record(root, cycle_id)
        pending = (record or {}).get("history_pending") or []
        if not pending:
            return "none"
        if not _history_deliver_locked(root, pending):
            return "pending"
        _write_history_pending(root, record, [])
        return "delivered"
    finally:
        artifact_admission._release_lock(root, fd)
_UNSET = object()
_HAND_ACTOR = "rule"


def _command_stamp() -> str:
    return str(time.time_ns())


def _command_line(*, command: str, stamp: str, target_type: str, target_id: str, target_path: str,
                  operation: str, field: str, before: Mapping[str, Any], after: Mapping[str, Any],
                  reason: Optional[str], now: Optional[float], by: str = "human") -> Dict[str, Any]:
    """One `make_event` argument set for a command run.  The IDs come from the run, so a line that
    is handed over twice after a crash is the same line, and two runs of one command are two lines."""
    return {
        "kind": "lifecycle", "target_type": target_type, "target_id": target_id, "target_path": target_path,
        "operation": operation, "field": field, "before": dict(before), "after": dict(after),
        "reason": reason or command,
        "transaction_id": _history_id("htxn", command, target_id, stamp),
        "event_id": _history_id("hevt", command, target_type, target_id, field, operation, stamp),
        "now": time.time() if now is None else float(now), **_history_actor(by)}


def _with_cycle_lines(record: Mapping[str, Any], lines: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    return _with_pending(record, _merge_pending(record.get("history_pending") or [], lines))


def _flush_cycle_pending_locked(root: Path, cycle_id: str) -> None:
    record = read_cycle_record(root, cycle_id)
    pending = (record or {}).get("history_pending") or []
    if pending and _history_deliver_locked(root, pending):
        _write_history_pending(root, record, [])


def _campaign_lines_locked(root: Path, campaign_id: str, lines: Sequence[Mapping[str, Any]]) -> None:
    """Keep campaign-level lines in the campaign's runtime record, then try to hand them over."""
    runtime = campaign_runtime_record(root, campaign_id) or {
        "schema_version": 1, "contract": CONTRACT, "campaign_id": campaign_id}
    pending = _merge_pending(runtime.get("history_pending") or [], lines)
    if pending:
        runtime["history_pending"] = pending
    else:
        runtime.pop("history_pending", None)
    _write_campaign_runtime_record(root, runtime)
    _flush_campaign_pending_locked(root, campaign_id)


def _flush_campaign_pending_locked(root: Path, campaign_id: str) -> None:
    runtime = campaign_runtime_record(root, campaign_id)
    pending = (runtime or {}).get("history_pending") or []
    if not pending or not _history_deliver_locked(root, pending):
        return
    runtime = dict(runtime)
    runtime.pop("history_pending", None)
    _write_campaign_runtime_record(root, runtime)


def record_campaign_state_line(root: Path, campaign_id: str, *, before: str, after: str,
                               reason: Optional[str], event_id: Optional[str], command: str) -> None:
    """D-125: a campaign close or reopen leaves one `state` line (the admission lock is held)."""
    path = None
    try:
        path = campaign_dir(root, campaign_id)
    except ProducerError:
        pass
    rel = Path(os.path.relpath(str(path), str(Path(root)))).as_posix() if path is not None else f"campaigns/{campaign_id}"
    line = _command_line(command=command, stamp=event_id or _command_stamp(), target_type="campaign",
                         target_id=campaign_id, target_path=rel, operation="update", field="state",
                         before={"value": before}, after={"value": after}, reason=reason, now=None)
    _campaign_lines_locked(root, campaign_id, [line])


def deliver_pending_history(root: Path, *, wait: float = 0.0) -> int:
    """Hand every pending line of the root's cycles and campaigns to the recorder; the number handed over.

    Cheap when nothing waits and when there is no recorder.  Never raises: a busy lock or a
    recorder that fails leaves the lines where they are for the next trigger."""
    try:
        root = Path(root).resolve()
        if _history_module() is None:
            return 0
        seen = _QUIET_HISTORY.get(str(root))
        # Records the observation this start just made saw with no waiting line are not opened again.
        quiet = seen[1] if seen is not None and time.monotonic() - seen[0] <= _QUIET_HISTORY_SECONDS else set()
        waiting: List[Tuple[str, str]] = []
        for kind, directory in (("cycle", producer_dir(root) / "cycles"),
                                ("campaign", producer_dir(root) / CAMPAIGN_RUNTIME_DIR)):
            try:
                names = sorted(os.listdir(str(directory)))
            except OSError:
                continue
            for name in names:
                if not name.endswith(".json") or (kind == "cycle" and name[:-5] in quiet):
                    continue
                try:
                    if b'"history_pending"' in (directory / name).read_bytes():
                        waiting.append((kind, name[:-5]))
                except OSError:
                    continue
        if not waiting:
            return 0
        if artifact_admission.holds_lock(root):
            fd = None
        else:
            fd = artifact_admission.try_acquire_lock(root) if wait <= 0 else artifact_admission._acquire_lock(root, wait)
            if fd is None:
                return 0
        handed = 0
        try:
            for kind, identifier in waiting:
                if kind == "cycle":
                    before = (read_cycle_record(root, identifier) or {}).get("history_pending") or []
                    _flush_cycle_pending_locked(root, identifier)
                    after = (read_cycle_record(root, identifier) or {}).get("history_pending") or []
                else:
                    before = (campaign_runtime_record(root, identifier) or {}).get("history_pending") or []
                    _flush_campaign_pending_locked(root, identifier)
                    after = (campaign_runtime_record(root, identifier) or {}).get("history_pending") or []
                handed += max(0, len(before) - len(after))
        finally:
            if fd is not None:
                artifact_admission._release_lock(root, fd)
        return handed
    except Exception:  # noqa: BLE001 -- handing lines over never fails the trigger
        return 0


def _last_known_path(root: Path, record: Mapping[str, Any], where: Optional[str] = None) -> str:
    """Where a cycle (or, with no `locator`, a campaign) last sat, for a line that needs a path when the
    folder is already gone: the path it was found at, else what the locator map still says, else what
    its record names."""
    if where:
        return where
    try:
        published = artifact_locator._load_index(root) or {}
    except Exception:  # noqa: BLE001 -- an unreadable map only means the record's own words are used
        published = {}
    campaign_id, locator = record.get("campaign_id"), record.get("locator")
    last = published.get(record.get("cycle_id"))
    if isinstance(last, str) and last:
        return last
    if locator:
        base = published.get(campaign_id)
        return f"{base if isinstance(base, str) and base else f'campaigns/{campaign_id}'}/{locator}"
    return f"campaigns/{campaign_id or record.get('cycle_id')}"


def _locator_suffix(base: str, locator: str) -> str:
    return locator[len(base):] if locator.startswith(base) and re.fullmatch(r"(-\d+)?", locator[len(base):]) else ""


def _read_manifest_raw(directory: Path) -> Optional[Tuple[bytes, Dict[str, Any]]]:
    path = directory / "manifest.json"
    try:
        if path.is_symlink() or not path.is_file():
            return None
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return (raw, document) if isinstance(document, dict) else None


def _observe_control_changes(root: Path, cycle_id: str, *, now: Optional[float] = None,
                             directory: Optional[Path] = None, locator_cache: bool = False,
                             record: Any = _UNSET, stored: Any = _UNSET) -> bool:
    """Log ordinary record edits/deletions through the existing recorder.

    No payload is restored. Bookkeeping can be read from its admitted revision
    and written again by an ordinary operation. Observation is best effort and
    never changes a completion decision or fails the caller.

    A caller that has just read the cycle passes `record` (what
    `read_cycle_record` answers) and `stored` (the record file as parsed, None
    when absent) so the same files are not read again; a change found from
    them is confirmed against a fresh read under the lock, as before.

    True means there was nothing to log or update (the files say what the
    record expects); False means a change was found, written or left for later.
    """
    try:
        if record is _UNSET:
            record = read_cycle_record(root, cycle_id)
        if record is None or record.get("deleted_at") or not _closed_record(record):
            return True
        directory = directory or _record_cycle_manifest_path(root, record).parent
        if not directory.is_dir() or directory.is_symlink():
            return True
        binding = artifact_locator.cycle_binding_bytes(record["campaign_id"], cycle_id,
                                                       started_on=record.get("started_on"))
        journal = _read_json(journal_path(root, cycle_id)) or {}
        manifest_expected = (journal.get("manifest_digest") if journal.get("state") == "refreshing"
                             else record.get("manifest_digest"))
        paths = [(directory / "manifest.json", manifest_expected),
                 (directory / artifact_locator.CYCLE_BINDING, _digest(binding)),
                 (cycle_record_path(root, cycle_id), record.get("control_record_digest") or _record_content_digest(record))]
        if locator_cache:
            expected = artifact_locator.index_observation_baselines(root)
            paths += [(root / "campaigns" / name, expected.get(name)) for name in ("INDEX.json", "INDEX.md")]
        prior = dict(record.get("control_observations") or {})
        baselines = dict(record.get("control_baselines") or {})
        stamp = _command_stamp()
        updates = {}
        lines = []
        for path, expected in paths:
            rel = path.relative_to(root).as_posix()
            if path.parent == root / "campaigns":
                after = _digest(path.read_bytes()) if path.is_file() and not path.is_symlink() else None
            elif path == cycle_record_path(root, cycle_id):
                parsed = _read_json(path) if stored is _UNSET else stored
                after = _record_content_digest(parsed) if parsed is not None else None
            elif path.is_symlink() or not path.is_file():
                after = None
            elif path.name == "manifest.json":
                parsed = _read_json(path)
                after = artifact_manifest.manifest_digest(parsed) if parsed is not None else _digest(path.read_bytes())
            else:
                after = _digest(path.read_bytes())
            before = prior.get(rel, expected) if baselines.get(rel, expected) == expected else expected
            baselines[rel] = expected
            if after == expected:
                if before != after:
                    updates[rel] = after
                continue
            if before == after:
                continue
            updates[rel] = _record_content_digest(record) if path == cycle_record_path(root, cycle_id) else after
            lines.append({"kind": "artifact", "target_type": "artifact", "target_id": cycle_id,
                          "target_path": rel, "operation": "delete" if after is None else "update",
                          "field": "content", "before": {"value": before}, "after": {"value": after},
                          "reason": "filesystem-change", **_history_actor("rule"),
                          "transaction_id": _history_id("htxn", cycle_id, rel, stamp),
                          "event_id": _history_id("hevt", cycle_id, rel, stamp),
                          "now": time.time() if now is None else float(now)})
        if not lines and not updates:
            return True
        fd = artifact_admission.try_acquire_lock(root)
        if fd is None:
            return False
        try:
            current = read_cycle_record(root, cycle_id)
            if current != record:
                return False
            prior.update(updates)
            pending = _merge_pending(record.get("history_pending") or [], lines)
            _write_cycle_record(root, _with_pending(dict(record, control_observations=prior,
                                                       control_baselines=baselines), pending), exclusive=False)
            _flush_cycle_pending_locked(root, cycle_id)
        finally:
            artifact_admission._release_lock(root, fd)
        return False
    except Exception:  # noqa: BLE001 -- a history observation cannot block ordinary work
        return False


# A closed cycle whose observed files are, to the `lstat`, exactly as a quiet observation left them
# says the same thing again; `reconcile_root` keeps those identities in one derived file so a later
# pass need not read the record, manifest and binding of every finished cycle once more. Any change of
# identity (content, inode, size, mtime or ctime) means the cycle is read and observed as before; a
# missing, unreadable or foreign memo means every cycle is. Identities younger than the settle margin
# (measured from before the look) are never kept: a write within one timestamp tick of the look could
# otherwise go unseen. On NFS an edit made on another host may show its new identity only after the
# client's attribute cache expires (about a minute by default); until then that cycle is skipped, then
# observed. Bump the memo `schema` whenever what `_observe_control_changes` reads or expects changes.
CONTROL_MEMO_NAME = "control-observation-memo.json"
_CONTROL_MEMO_SETTLE_NS = 2_000_000_000
_QUIET_HISTORY_SECONDS = 300.0
HISTORY_QUIET_NAME = "history-quiet-records.json"


def _control_file_key(path: Path) -> Optional[List[int]]:
    try:
        seen = os.lstat(str(path))
    except (FileNotFoundError, NotADirectoryError):
        return None
    return [seen.st_dev, seen.st_ino, seen.st_size, seen.st_mtime_ns, seen.st_ctime_ns, seen.st_mode]


def _control_keys(root: Path, cycle_id: str, directory: Path) -> Optional[List[Optional[List[int]]]]:
    """The `lstat` identities of what `_observe_control_changes` reads for one cycle (None if unreadable)."""
    try:
        return [_control_file_key(path) for path in (
            cycle_record_path(root, cycle_id), journal_path(root, cycle_id),
            directory / "manifest.json", directory / artifact_locator.CYCLE_BINDING)]
    except (OSError, ProducerError):
        return None


def _control_keys_settled(keys: Sequence[Optional[List[int]]], now_ns: int) -> bool:
    return all(key is None or (now_ns - key[3] > _CONTROL_MEMO_SETTLE_NS
                               and now_ns - key[4] > _CONTROL_MEMO_SETTLE_NS) for key in keys)


def _read_control_memo(root: Path) -> Dict[str, Any]:
    document = _read_json(producer_dir(root) / CONTROL_MEMO_NAME)
    cycles = document.get("cycles") if document and document.get("schema") == 1 else None
    return cycles if isinstance(cycles, dict) else {}


def _write_control_memo(root: Path, cycles: Mapping[str, Any]) -> None:
    try:
        data = json.dumps({"schema": 1, "cycles": cycles}, sort_keys=True, separators=(",", ":")).encode()
        _write_atomic(producer_dir(root) / CONTROL_MEMO_NAME, data, 0o600)
    except OSError:
        pass  # a derived accelerator: the next pass reads every cycle instead


def _read_history_quiet(root: Path) -> Dict[str, Any]:
    document = _read_json(producer_dir(root) / HISTORY_QUIET_NAME)
    records = document.get("records") if document and document.get("schema") == 1 else None
    return records if isinstance(records, dict) else {}


def _write_history_quiet(root: Path, records: Mapping[str, Any]) -> None:
    try:
        data = json.dumps({"schema": 1, "records": records}, sort_keys=True, separators=(",", ":")).encode()
        _write_atomic(producer_dir(root) / HISTORY_QUIET_NAME, data, 0o600)
    except OSError:
        pass  # a derived accelerator: the next delivery reads every record instead


def _observe_root_controls(root: Path, scan: _LayoutScan, *, now: Optional[float]) -> None:
    """Observe every cycle the scan found, reading each record once and skipping quiet unchanged ones.

    The earliest cycle also watches the campaign locator cache, so it is always observed."""
    memo = _read_control_memo(root)
    look_ns = time.time_ns()  # identities are judged against the moment before they were read
    keys = {cid: _control_keys(root, cid, folder) for cid, folder in scan.cycles.items()}
    quiet = {cid: memo[cid] for cid, folder in scan.cycles.items()
             if keys[cid] is not None and isinstance(memo.get(cid), dict)
             and memo[cid].get("dir") == _cycle_rel(root, folder) and memo[cid].get("keys") == keys[cid]}
    stored: Dict[str, Any] = {}
    records: Dict[str, Any] = {}

    def load(cid: str) -> None:
        stored[cid] = _read_json(cycle_record_path(root, cid))
        records[cid] = stored[cid] if stored[cid] is not None else read_cycle_record(root, cid)

    for cid in scan.cycles:
        if cid not in quiet:
            load(cid)

    def started(cid: str) -> str:
        source = quiet[cid] if cid in quiet else (records[cid] or {})
        return source.get("started_on") or ""

    remembered: Dict[str, Any] = {}
    no_history: Set[str] = set()
    vouched = _read_history_quiet(root)
    still_quiet: Dict[str, Any] = {}

    def without_history(cycle_id: str, found: Any) -> None:
        """`found` is the record as read after its identity was taken this look."""
        if not isinstance(found, dict) or found.get("history_pending"):
            return
        no_history.add(cycle_id)
        record_key = keys[cycle_id][0] if keys[cycle_id] else None
        if record_key is not None and _control_keys_settled([record_key], look_ns):
            still_quiet[cycle_id] = record_key

    for position, cycle_id in enumerate(sorted(scan.cycles, key=lambda cid: (started(cid), cid))):
        if cycle_id in quiet and position != 0:
            remembered[cycle_id] = quiet[cycle_id]
            record_key = keys[cycle_id][0]
            if record_key is not None and vouched.get(cycle_id) == record_key:
                no_history.add(cycle_id)
                still_quiet[cycle_id] = record_key
            else:
                without_history(cycle_id, _read_json(cycle_record_path(root, cycle_id)))  # once per identity
            continue
        if cycle_id not in stored:
            load(cycle_id)
        folder = scan.cycles[cycle_id]
        calm = _observe_control_changes(root, cycle_id, now=now, directory=folder,
                                        locator_cache=position == 0, record=records[cycle_id],
                                        stored=stored[cycle_id])
        if calm:
            without_history(cycle_id, stored[cycle_id])
        if (calm and stored[cycle_id] is not None and keys[cycle_id] is not None
                and _control_keys_settled(keys[cycle_id], look_ns)):
            remembered[cycle_id] = {"dir": _cycle_rel(root, folder), "keys": keys[cycle_id],
                                    "started_on": stored[cycle_id].get("started_on")}
    _QUIET_HISTORY[str(root)] = (time.monotonic(), no_history)
    if remembered != memo:
        _write_control_memo(root, remembered)
    if still_quiet != vouched:
        _write_history_quiet(root, still_quiet)


def _preserve_current_manifest(root: Path, record: Mapping[str, Any], directory: Path) -> None:
    """Keep the manifest as it is now (a cycle closed before copies existed has none yet)."""
    if not _closed_record(record):
        return
    found = _read_manifest_raw(directory)
    if found is None:
        return
    try:
        artifact_lifecycle.preserve_manifest_snapshot(root, record["cycle_id"], found[0])
    except artifact_lifecycle.LifecycleError:
        pass
