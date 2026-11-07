"""Artifact producer: cycle and campaign operations (§45 D-126).

Related/superseded marks, `cycle-mark`, `cycle-move`, `delete`, and
`reconcile_root`, which finds folders moved, renamed or removed by hand.

Split out of `artifact_producer.py`, which re-exports every name defined here.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import artifact_admission
import artifact_identity
import artifact_index
import artifact_lifecycle
import artifact_locator
import artifact_manifest
import artifact_campaign

from artifact_producer_store import (
    _campaign_path, _campaigns_by_key, _closed_record, _cycle_rel, _disposition_value,
    _earlier_documents, _folder_cycle_id, _fsync_dir, _json_bytes, _LayoutScan, _read_json,
    _rfc3339, _write_atomic, _write_campaign, _write_campaign_runtime_record,
    _write_cycle_record, campaign_dir, campaign_runtime_record, CONTRACT, cycle_dir,
    cycle_disposition, is_active, list_cycle_records, ProducerError, read_campaign,
    read_campaign_tombstone, read_cycle_record, RELATED_KINDS,
)

from artifact_producer_routes import (
    _remove_journal, _write_journal,
)

from artifact_producer_history import (
    _campaign_lines_locked, _command_line, _command_stamp, _flush_campaign_pending_locked,
    _flush_cycle_pending_locked, _HAND_ACTOR, _history_by, _history_deliver_locked,
    _last_known_path, _locator_suffix, _merge_pending, _observe_root_controls,
    _preserve_current_manifest, _read_manifest_raw, _UNSET, _with_cycle_lines, _with_pending,
)

from artifact_producer_interim import (
    remove_interim,
)

from artifact_producer_manifest import (
    _cycle_relative_primary,
)

from artifact_producer_refresh import (
    REFRESH_ADMISSION_WAIT_SECONDS, refresh_cycle,
)


# ---------------------------------------------------------------------------
# campaign relationships and supersession side records (D-81)
# ---------------------------------------------------------------------------


def _find_campaign_by_key_any_state(root: Path, key: str) -> Optional[Dict[str, Any]]:
    """Like `find_campaign_by_key`, but not restricted to `state == "active"` --
    a `related[]` row may point at a campaign that is already superseded."""
    rows = _campaigns_by_key(root, key)
    return rows[0] if rows else None


def validate_related(root: Path, related: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """D-81 pure validation of `campaign.json` `related[]` rows -- no write.
    Returns a list of `{"index", "code", "detail"}` violation rows; empty
    means every row resolves."""
    root = Path(root).resolve()
    violations: List[Dict[str, Any]] = []
    for i, row in enumerate(related):
        if not isinstance(row, Mapping):
            violations.append({"index": i, "code": "campaign-related-invalid", "detail": "not-an-object"})
            continue
        kind = row.get("kind")
        if kind not in RELATED_KINDS:
            violations.append({"index": i, "code": "campaign-related-invalid", "detail": f"kind:{kind}"})
            continue
        campaign_id = row.get("campaign_id")
        key = row.get("key")
        if not campaign_id and not key:
            violations.append({"index": i, "code": "campaign-related-invalid",
                               "detail": "missing-campaign_id-and-key"})
            continue
        found = read_campaign(root, campaign_id) if campaign_id else None
        if found is None and key:
            found = _find_campaign_by_key_any_state(root, key)
        if found is None:
            violations.append({"index": i, "code": "campaign-related-unresolved",
                               "detail": str(campaign_id or key)})
    return violations


def set_campaign_related(root: Path, campaign_id: str, *, related: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """D-81: producer-internal API. `campaigns/<camp>/campaign.json` is the
    `campaign-record-machine-managed` write surface -- no general writer, hook,
    or agent may call this."""
    root = Path(root).resolve()
    violations = validate_related(root, related)
    if violations:
        first = violations[0]
        raise ProducerError(first["code"], first["detail"])
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
    try:
        campaign = read_campaign(root, campaign_id)
        if campaign is None:
            raise ProducerError("campaign-unknown", campaign_id)
        campaign = dict(campaign)
        campaign["related"] = [dict(row) for row in related]
        _write_campaign(root, campaign, exclusive=False)
        return {"status": "updated", "campaign_id": campaign_id, "related": campaign["related"]}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def mark_cycle_superseded(
    root: Path, cycle_id: str, *, superseded_by: Sequence[str], superseded_event_id: str,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """The older name of `cycle-mark --superseded-by` (D-81, now D-126): the mark is the record's
    `disposition`, written by the one disposition writer.  The cycle's state, files and manifest
    stay as they were, so a marked cycle is as finished and as writable as before."""
    out = cycle_mark(root, cycle_id, superseded_by=list(superseded_by), superseded_event_id=superseded_event_id,
                     now=now)
    mark = out["disposition"]
    return {"status": "updated", "cycle_id": cycle_id, "disposition": mark,
            "superseded_by": list(mark.get("superseded_by") or []), "superseded_event_id": superseded_event_id}


def mark_campaign_superseded(root: Path, campaign_id: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    """D-81: a campaign may be marked `superseded` only once every cycle it owns is set aside:
    marked (`cycle-mark`, or the earlier `state: superseded`) or deleted."""
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        campaign = read_campaign(root, campaign_id)
        if campaign is None:
            raise ProducerError("campaign-unknown", campaign_id)
        for cycle_id in campaign.get("cycles", []):
            record = read_cycle_record(root, cycle_id)
            if record is None or not (record.get("deleted_at") or cycle_disposition(record) is not None):
                raise ProducerError("campaign-has-live-cycles", campaign_id)
        artifact_locator.prepare_index_update(root, [campaign_id])
        updated = dict(campaign)
        updated["state"] = "superseded"
        _write_campaign(root, updated, exclusive=False)
        artifact_locator.update_indexes(root, [campaign_id])
        return {"status": "updated", "campaign_id": campaign_id, "state": "superseded"}
    finally:
        artifact_admission._release_lock(root, lock_fd)


# -- cycle-mark -------------------------------------------------------------------


def cycle_mark(root: Path, cycle_id: str, *, discard: bool = False, superseded_by: Optional[Sequence[str]] = None,
               clear: bool = False, primary: Optional[str] = None, reason: Optional[str] = None,
               now: Optional[float] = None, superseded_event_id: Optional[str] = None) -> Dict[str, Any]:
    """D-126: mark a cycle discarded or superseded, take the mark off, or name its primary document.

    The first three change one field of the cycle record, `disposition`: the folder, the files,
    the manifest and whether the cycle is finished are as they were.  `primary` makes another
    document the one that stands for the cycle: a new manifest document that differs from the
    last one only in two `role` values (the earlier document is kept as it was published)."""
    root = Path(root).resolve()
    chosen = [name for name, on in (("--discard", discard), ("--superseded-by", superseded_by is not None),
                                    ("--clear", clear), ("--primary", primary is not None)) if on]
    if len(chosen) != 1:
        raise ProducerError("request-invalid", "cycle-mark takes exactly one of --discard, --superseded-by, --clear, --primary")
    if primary is not None:
        return _mark_primary(root, cycle_id, primary, reason=reason, now=now)
    stamp = _command_stamp()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        directory = None
        try:
            directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
        except (ProducerError, artifact_locator.LocatorError):
            pass
        before = cycle_disposition(record)
        marked_by = _history_by("human")
        when = _rfc3339(now)
        if clear:
            if "disposition" not in record:
                return {"status": "unchanged", "cycle_id": cycle_id}
            updated = {key: value for key, value in record.items() if key != "disposition"}
            after_mark, operation = None, "update"  # the recorder deletes only a `state`; a taken-off mark is an update to nothing
        else:
            if discard:
                mark: Dict[str, Any] = {"kind": "discarded"}
            else:
                named = list(dict.fromkeys(superseded_by or ()))
                for other in named:
                    if not artifact_identity.is_well_formed(other, "cycle") or read_cycle_record(root, other) is None:
                        raise ProducerError("cycle-unknown", str(other))
                mark = {"kind": "superseded"}
                if named:
                    mark["superseded_by"] = named
                if superseded_event_id:
                    mark["superseded_event_id"] = superseded_event_id
            mark.update(reason=reason if isinstance(reason, str) and reason.strip() else None,
                        marked_at=when, marked_by=marked_by)
            updated = dict(record, disposition=mark)
            after_mark, operation = mark, ("add" if before is None else "update")
        if directory is not None:
            _preserve_current_manifest(root, record, directory)
        line = _command_line(
            command="cycle-mark", stamp=stamp, target_type="cycle", target_id=cycle_id,
            target_path=_last_known_path(root, record, _cycle_rel(root, directory) if directory is not None else ""),
            operation=operation,
            field="disposition", before=_disposition_value(before), after=_disposition_value(after_mark),
            reason=reason, now=now)
        _write_cycle_record(root, _with_cycle_lines(updated, [line]), exclusive=False)
        _flush_cycle_pending_locked(root, cycle_id)
        result = {"status": "cleared" if clear else "marked", "cycle_id": cycle_id}
        if after_mark is not None:
            result["disposition"] = after_mark
        return result
    finally:
        artifact_admission._release_lock(root, lock_fd)


# -- the next manifest document of a closed cycle, for a change that is not a file change ---


def _publish_document_locked(root: Path, record: Mapping[str, Any], directory: Path, raw: bytes,
                             document: Mapping[str, Any], refreshed: Dict[str, Any],
                             lines: Sequence[Mapping[str, Any]], *, now: Optional[float],
                             record_after: Optional[Mapping[str, Any]] = None,
                             moved_fields: Sequence[str] = (),
                             index: Optional[artifact_index.IndexDocument] = None,
                             cycle_path: Optional[str] = None,
                             digest: Optional[str] = None) -> Dict[str, Any]:
    """Publish `refreshed` as the cycle's current document (the admission lock is held).

    The same order a refresh keeps: the earlier document is preserved, the history lines go to the
    recorder (what it cannot take waits in the record), the new document is preserved, the manifest is
    replaced atomically, then the index row is swapped on the earlier digest and the record follows.
    `record_after` is the record the caller already changed (a move); `cycle_path` is where the folder
    is now; `digest` is the earlier document's digest when the caller already has it.  Returns the
    record as written."""
    cycle_id = record["cycle_id"]
    manifest_path = directory / "manifest.json"
    if digest is None:
        digest = artifact_manifest.manifest_digest(document)
    artifact_lifecycle.preserve_manifest_snapshot(root, cycle_id, raw)
    earlier = {old.get("manifest_revision_id"): old for old in _earlier_documents(root, cycle_id, refreshed)}
    earlier[document.get("manifest_revision_id")] = dict(document)
    report = artifact_manifest.validate_update(refreshed, preserved=list(earlier.values()), previous=document,
                                               changeable_cycle_fields=moved_fields)
    if not report.ok:
        raise ProducerError("manifest-invalid", ";".join(v.code for v in report.violations))
    new_digest = artifact_manifest.manifest_digest(refreshed)
    new_raw = artifact_manifest.canonical_bytes(refreshed)
    if index is None:
        index = artifact_admission.load_index(root)  # the one read of this locked section
    row = index.manifests.get(cycle_id)
    if isinstance(row, dict) and row.get("manifest_digest") != digest:
        raise ProducerError("already-sealed-mismatch", "index")
    identity = artifact_lifecycle.read_root_identity(root)
    index_report = artifact_index.check(
        index, refreshed, idempotency_key=cycle_id, manifest_digest=new_digest,
        repository_id=identity.repository_id if identity else None,
        replaces_manifest_digest=digest if isinstance(row, dict) else None,
        known_parent_cycle_ids=(set(artifact_admission._producer_cycle_ids(root)) |
            ({record["parent_cycle_id"]} if record.get("parent_cycle_id") and
             (record.get("relocation") or {}).get("external_parent_root") else set())))
    if not index_report.ok:
        raise ProducerError("index-rejected", ";".join(v.code for v in index_report.violations))
    base_record = dict(record_after if record_after is not None else record)
    pending = _merge_pending(base_record.get("history_pending") or [], lines)
    if _history_deliver_locked(root, pending):
        pending = []
    where = cycle_path or _cycle_rel(root, directory)
    _write_journal(root, cycle_id, state="refreshing", manifest_digest=new_digest, previous_manifest_digest=digest,
                   cycle_path=where, manifest_revision_id=refreshed["manifest_revision_id"], history_pending=pending)
    artifact_lifecycle.preserve_manifest_snapshot(root, cycle_id, new_raw)
    # COMMIT POINT: atomic replacement of the current document.
    _write_atomic(manifest_path, new_raw)
    index = artifact_index.apply(index, refreshed, cycle_path=where, manifest_digest=new_digest,
                                 idempotency_key=cycle_id)
    artifact_admission._write_index(root, index)
    written = _with_pending(dict(base_record, manifest_digest=new_digest,
                                 cycle_state=refreshed["cycle"]["state"]), pending)
    _write_cycle_record(root, written, exclusive=False)
    _remove_journal(root, cycle_id)
    return written


def _next_document(document: Mapping[str, Any], allocator: artifact_identity.IdAllocator) -> Dict[str, Any]:
    refreshed = json.loads(json.dumps(document))
    refreshed["manifest_revision_id"] = allocator.allocate("manifest_revision")
    return refreshed


def _mark_primary(root: Path, cycle_id: str, primary: str, *, reason: Optional[str],
                  now: Optional[float]) -> Dict[str, Any]:
    """`cycle-mark --primary`: the named document becomes the cycle's primary one (D-126)."""
    stamp = _command_stamp()

    def current() -> Tuple[Dict[str, Any], Path, Optional[Tuple[bytes, Dict[str, Any]]], str]:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        if not _closed_record(record) or record.get("deleted_at"):
            raise ProducerError("request-invalid", f"{cycle_id}: --primary names a document of a closed cycle")
        directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
        rel = _cycle_relative_primary(primary, directory)
        if not rel or not rel.startswith("artifacts/"):
            raise ProducerError("request-invalid", f"--primary is a cycle-relative path (artifacts/...): {primary}")
        return record, directory, _read_manifest_raw(directory), rel

    record, directory, found, rel = current()
    listed = lambda doc: any(row.get("locator", {}).get("path") == rel for row in doc.get("artifact_revisions", []) or [])
    if found is None:
        raise ProducerError("request-invalid", f"{cycle_id}: manifest-absent")
    if not listed(found[1]):
        # The file may be one the cycle has not been observed with yet: look once, then ask again.
        refresh_cycle(root, cycle_id, trigger="explicit", now=now)
        record, directory, found, rel = current()
        if found is None or not listed(found[1]):
            raise ProducerError("request-invalid", f"{cycle_id}: primary-not-in-manifest: {rel}")
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        record, directory, found, rel = current()
        raw, document = found  # type: ignore[misc]
        revisions = {row["locator"]["path"]: row for row in document.get("artifact_revisions", []) or []}
        target = revisions.get(rel)
        if target is None:
            raise ProducerError("request-invalid", f"{cycle_id}: primary-not-in-manifest: {rel}")
        artifacts = [dict(row) for row in document.get("artifacts", []) or []]
        old = [row for row in artifacts if row.get("role") == "primary"]
        if len(old) == 1 and old[0]["artifact_id"] == target["artifact_id"]:
            return {"status": "unchanged", "cycle_id": cycle_id, "primary": rel}
        path_of = {row["artifact_id"]: path for path, row in revisions.items()}
        old_path = path_of.get(old[0]["artifact_id"]) if old else None
        for row in artifacts:
            if row["artifact_id"] == target["artifact_id"]:
                row["role"] = "primary"
            elif row.get("role") == "primary":
                row["role"] = "output"
        refreshed = _next_document(document, artifact_identity.IdAllocator())
        refreshed["artifacts"] = artifacts
        line = _command_line(
            command="cycle-mark", stamp=stamp, target_type="cycle", target_id=cycle_id,
            target_path=_cycle_rel(root, directory), operation="update", field="primary",
            before={"value": old_path}, after={"value": rel}, reason=reason, now=now)
        _publish_document_locked(root, record, directory, raw, document, refreshed, [line], now=now)
        artifact_locator.update_indexes(root, [record["campaign_id"]])
        _flush_cycle_pending_locked(root, cycle_id)
        return {"status": "marked", "cycle_id": cycle_id, "primary": rel,
                "manifest_revision_id": refreshed["manifest_revision_id"]}
    finally:
        artifact_admission._release_lock(root, lock_fd)


# -- cycle-move -------------------------------------------------------------------------


def _resolve_campaign_argument(root: Path, value: str) -> Dict[str, Any]:
    """A campaign by its ID or by its key (an active one first, any state otherwise)."""
    if artifact_identity.is_well_formed(value, "campaign"):
        campaign = read_campaign(root, value)
    else:
        rows = _campaigns_by_key(root, value)
        campaign = next((row for row in rows if row.get("state") == "active"), rows[0] if rows else None)
    if campaign is None:
        raise ProducerError("campaign-unknown", str(value))
    return campaign


def _parent_chain_has(root: Path, start: Optional[str], target: str) -> bool:
    seen: Set[str] = set()
    node = start
    while node and node not in seen:
        if node == target:
            return True
        seen.add(node)
        node = (read_cycle_record(root, node) or {}).get("parent_cycle_id")
    return False


def _drop_group_membership(root: Path, campaign_id: str, cycle_ids: Sequence[str]) -> None:
    """A cycle that left a campaign no longer belongs to that campaign's workflow groups.

    Best effort and outside the admission lock: a declaration that cannot be read keeps its rows."""
    try:
        import artifact_workflow_groups
        plan = artifact_workflow_groups.prepare_withdrawal(root, campaign_id, list(cycle_ids))
        if plan is not None:
            artifact_workflow_groups.apply(root, plan, lock_timeout=0)
    except Exception:  # noqa: BLE001
        pass


def _edit_campaign_members(root: Path, folder: Optional[Path], cycle_id: str, *, joining: bool) -> None:
    """The cycle joins or leaves the member list of the campaign whose folder is `folder`.

    Read and written at the folder it is in, never through the locator: while a cycle is between two
    campaigns the locator's scan of either one would refuse."""
    if folder is None:
        return
    path = folder / "campaign.json"
    raw = _read_json(path)
    if raw is None:
        return
    campaign = artifact_campaign.fold_campaign(root, path, raw)
    members = [cid for cid in campaign.get("cycles", []) if cid != cycle_id]
    if joining:
        members.append(cycle_id)
    if members == list(campaign.get("cycles", [])):
        return
    updated = dict(campaign, cycles=members)
    artifact_campaign.check_campaign_write(root, path, updated)
    _write_atomic(path, _json_bytes(updated))


def _adopt_location_locked(root: Path, record: Mapping[str, Any], new_directory: Path, *,
                           command: str, stamp: str, reason: Optional[str], now: Optional[float],
                           by: str, parent: Any = _UNSET, old_campaign_folder: Optional[Path] = None,
                           index: Optional[artifact_index.IndexDocument] = None,
                           manifest: Any = _UNSET, manifest_digest: Optional[str] = None,
                           ) -> Tuple[Dict[str, Any], List[str]]:
    """Make the records say what the folders say: the cycle now sits at `new_directory`.

    The one procedure behind `cycle-move` and the finding of a hand-made move (the admission lock is
    held; the folder is already where it is).  The cycle's own binding, record, campaign lists, current
    manifest (a new document, the earlier one preserved) and index row follow, in an order a stopped run
    is finished by the next look.  `parent` (when given) is the new parent cycle ID or `None`.  Returns
    the record as written and the campaign IDs whose locator index is now stale.  `manifest` (the
    `_read_manifest_raw` result, `None` for none) and its `manifest_digest` are what a reconcile read
    before it took the lock; left out, the folder's manifest is read here."""
    cycle_id = record["cycle_id"]
    import artifact_request
    anchor = artifact_request.scope_anchor(new_directory) if record.get("request_binding") else None
    if anchor is not None and artifact_request.execution_dir(root, record, anchor=anchor) != new_directory:
        raise ProducerError("record-locator-invalid", str(new_directory))
    target_campaign_dir = anchor.parent if anchor is not None else new_directory.parent
    new_locator = anchor.name if anchor is not None else new_directory.name
    target = _read_json(target_campaign_dir / "campaign.json")
    if target is None or not artifact_identity.is_well_formed(target.get("campaign_id"), "campaign"):
        raise ProducerError("campaign-unknown", target_campaign_dir.name)
    old_campaign_id, new_campaign_id = record["campaign_id"], target["campaign_id"]
    started = record.get("started_on")
    where = _cycle_rel(root, new_directory)
    lines: List[Dict[str, Any]] = []
    changed: List[str] = []
    updated = dict(record)
    if old_campaign_id != new_campaign_id:
        updated["campaign_id"] = new_campaign_id
        updated["moved_at"] = _rfc3339(now)
        lines.append(_command_line(
            command=command, stamp=stamp, target_type="cycle", target_id=cycle_id, target_path=where,
            operation="move", field="campaign", before={"value": old_campaign_id},
            after={"value": new_campaign_id}, reason=reason, now=now, by=by))
        changed += [old_campaign_id, new_campaign_id]
    if updated.get("locator") != new_locator:
        if old_campaign_id == new_campaign_id:
            before_directory = (old_campaign_folder or target_campaign_dir) / str(record.get("locator") or cycle_id)
            if anchor is not None:
                before_directory = before_directory / Path(*artifact_request.EXECUTIONS) / cycle_id
            before_path = _cycle_rel(root, before_directory)
            lines.append(_command_line(
                command=command, stamp=stamp, target_type="cycle", target_id=cycle_id, target_path=where,
                operation="move", field="path", before={"value": before_path}, after={"value": where},
                reason=reason, now=now, by=by))
        base = artifact_locator.locator_base(started, record.get("slug") or "") if started else ""
        updated["locator"] = new_locator
        updated["locator_suffix"] = _locator_suffix(base, new_locator)
        changed.append(new_campaign_id)
    if parent is not _UNSET and parent != record.get("parent_cycle_id"):
        updated["parent_cycle_id"] = parent
        if parent is None:
            updated.pop("parent_cycle_state_at_begin", None)
        else:
            updated["parent_cycle_state_at_begin"] = (read_cycle_record(root, parent) or {}).get("state", "open")
        lines.append(_command_line(
            command=command, stamp=stamp, target_type="cycle", target_id=cycle_id, target_path=where,
            operation="update", field="parent", before={"value": record.get("parent_cycle_id")},
            after={"value": parent}, reason=reason, now=now, by=by))
        changed.append(new_campaign_id)
    # The binding the folder carries names the campaign it sits under.
    binding = artifact_locator.read_cycle_binding(new_directory / artifact_locator.CYCLE_BINDING)
    if binding is None or binding.get("campaign_id") != new_campaign_id or binding.get("cycle_id") != cycle_id:
        _write_atomic(new_directory / artifact_locator.CYCLE_BINDING, artifact_locator.cycle_binding_bytes(
            new_campaign_id, cycle_id, started_on=started if artifact_locator.started_on_is_valid(started) else None))
    # The campaign lists: the cycle leaves one and joins the other.
    if old_campaign_id != new_campaign_id:
        _edit_campaign_members(root, old_campaign_folder, cycle_id, joining=False)
        _edit_campaign_members(root, target_campaign_dir, cycle_id, joining=True)
    pending_record = _with_cycle_lines(updated, lines)
    if manifest is _UNSET:
        manifest = _read_manifest_raw(new_directory) if _closed_record(record) else None
    if manifest is not None:
        raw, document = manifest
        observed_digest = manifest_digest or artifact_manifest.manifest_digest(document)
        if observed_digest != record.get("manifest_digest"):
            previous = artifact_lifecycle.find_manifest_snapshot(
                root, cycle_id, manifest_digest=record.get("manifest_digest"))
            if previous is not None:
                document = previous
                raw = artifact_manifest.canonical_bytes(document)
                manifest_digest = record["manifest_digest"]
        refreshed = _next_document(document, artifact_identity.IdAllocator())
        if (record.get("relocation") or {}).get("original_root"):
            import artifact_cross_root_move
            refreshed = artifact_cross_root_move.relocate_document(root, refreshed, record["relocation"])
        campaign_row = dict(refreshed.get("campaign") or {})
        campaign_row.update(campaign_id=new_campaign_id, goal=str(target.get("goal", "")),
                            title=str(target.get("title", "")),
                            completion_criterion={"statement": str(
                                (target.get("completion_criterion") or {}).get("statement", ""))})
        refreshed["campaign"] = campaign_row
        refreshed["cycle"] = dict(refreshed["cycle"], campaign_id=new_campaign_id,
                                  parent_cycle_id=updated.get("parent_cycle_id"))
        if refreshed["campaign"] == document.get("campaign") and refreshed["cycle"] == document.get("cycle"):
            written = pending_record  # nothing the document says changed (only a folder name)
            _write_cycle_record(root, written, exclusive=False)
            _swap_index_path_locked(root, cycle_id, where, index=index)
        else:
            written = _publish_document_locked(
                root, record, new_directory, raw, document, refreshed, lines, now=now, record_after=updated,
                moved_fields=("campaign_id", "parent_cycle_id"), index=index, cycle_path=where,
                digest=manifest_digest)
    else:
        written = pending_record
        _write_cycle_record(root, written, exclusive=False)
        if _closed_record(record):
            _swap_index_path_locked(root, cycle_id, where, index=index)
    # An explicit request-folder move already moved these bytes. Apply the
    # same existing metadata/history operation to each logical scope too.
    if command == "cycle-move" and (old_campaign_id != new_campaign_id or record.get("locator") != new_locator):
        for member in artifact_request.members(root, new_directory):
            child = read_cycle_record(root, member.name)
            _, touched = _adopt_location_locked(
                root, child, member, command=command, stamp=stamp, reason=reason, now=now,
                by=by, old_campaign_folder=old_campaign_folder)
            changed += touched
    return written, list(dict.fromkeys(changed))


def _swap_index_path_locked(root: Path, cycle_id: str, where: str, *,
                            index: Optional[artifact_index.IndexDocument] = None) -> None:
    index = index if index is not None else artifact_admission.load_index(root)
    row = index.cycles.get(cycle_id)
    if isinstance(row, dict) and row.get("cycle_path") != where:
        cycles = dict(index.cycles)
        cycles[cycle_id] = dict(row, cycle_path=where)
        artifact_admission._write_index(root, replace(index, cycles=cycles))


def _retarget_index_paths(root: Path, old_prefix: str, new_prefix: str) -> None:
    """A campaign folder was renamed: the index rows of its cycles name the new folder (one read, one write)."""
    index = artifact_admission.load_index(root)
    cycles = dict(index.cycles)
    changed = False
    for cycle_id, row in index.cycles.items():
        path = row.get("cycle_path") if isinstance(row, dict) else None
        if isinstance(path, str) and (path == old_prefix or path.startswith(old_prefix + "/")):
            cycles[cycle_id] = dict(row, cycle_path=new_prefix + path[len(old_prefix):])
            changed = True
    if changed:
        artifact_admission._write_index(root, replace(index, cycles=cycles))


def cycle_move(root: Path, cycle_id: Optional[str] = None, *, campaign: Optional[str] = None, parent: Optional[str] = None,
               no_parent: bool = False, reason: Optional[str] = None, now: Optional[float] = None,
               target_artifact_root: Optional[Path] = None, source_campaign: Optional[str] = None,
               attach_logs: Sequence[str] = (), dry_run: bool = False) -> Dict[str, Any]:
    """D-126: move a cycle to another campaign and/or change its parent.

    The cycle keeps its ID.  The folder moves under the target campaign (a name taken there gets
    the smallest unused `-2`, `-3` ... suffix, D-90), and the binding, record, campaign lists,
    manifest (a new document; the earlier one is preserved), index and locator index follow.  A
    target that is closed is opened again, as a `begin` would; one that is set aside takes the cycle
    without changing its own state.  Open cycles move too (they have no manifest to write)."""
    root = Path(root).resolve()
    target_root = Path(target_artifact_root).resolve() if target_artifact_root else root
    if target_root != root or source_campaign is not None or attach_logs or dry_run:
        import artifact_cross_root_move as cross_move
        return cross_move.move(root, target_root, cycle_id=cycle_id, source_campaign=source_campaign,
                               campaign=campaign, attach_logs=attach_logs, dry_run=dry_run,
                               parent=parent, no_parent=no_parent, reason=reason, now=now)
    if parent is not None and no_parent:
        raise ProducerError("request-invalid", "--parent and --no-parent cannot be combined")
    if campaign is None and parent is None and not no_parent:
        raise ProducerError("request-invalid", "cycle-move changes the campaign and/or the parent")
    stamp = _command_stamp()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    stale: List[str] = []
    old_campaign_id = None
    try:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        if record.get("deleted_at"):
            raise ProducerError("cycle-unknown", f"{cycle_id}: deleted")
        old_campaign_id = record["campaign_id"]
        new_parent: Any = _UNSET
        if no_parent:
            new_parent = None
        elif parent is not None:
            if not artifact_identity.is_well_formed(parent, "cycle") or read_cycle_record(root, parent) is None:
                raise ProducerError("parent-cycle-not-joinable", str(parent))
            if parent == cycle_id or _parent_chain_has(root, parent, cycle_id):
                raise ProducerError("parent-cycle-invalid", str(parent))
            new_parent = parent
        target = _resolve_campaign_argument(root, campaign) if campaign is not None else None
        source = cycle_dir(root, old_campaign_id, cycle_id, record)
        import artifact_request
        member_ids = [path.name for path in artifact_request.members(root, source)]
        destination = source
        if target is not None and target["campaign_id"] != old_campaign_id:
            artifact_locator.prepare_index_update(root, [old_campaign_id, target["campaign_id"]])
            if target.get("state") == "satisfied":
                try:
                    artifact_campaign._reopen_locked(
                        root, _campaign_path(root, target["campaign_id"], target), reason="cycle-move")
                except artifact_campaign.CampaignError as exc:
                    raise ProducerError(exc.code, exc.detail) from exc
            target_dir = campaign_dir(root, target["campaign_id"], target)
            if not source.is_dir() or source.is_symlink():
                raise ProducerError("record-locator-invalid", str(source))
            keep = target_dir / str(record.get("locator") or source.name)
            if os.path.lexists(str(keep)):
                locator_name, _suffix = artifact_locator.allocate_locator(
                    target_dir, record["started_on"], record.get("slug") or "")
            else:
                locator_name = keep.name
            destination = target_dir / locator_name
            # The folder moves first; whatever stops after this is what the next look finishes.
            os.rename(str(source), str(destination))
            _fsync_dir(target_dir)
            _fsync_dir(source.parent)
        if destination == source and new_parent is _UNSET:
            return {"status": "unchanged", "cycle_id": cycle_id, "campaign_id": old_campaign_id,
                    "cycle_dir": str(source)}
        written, stale = _adopt_location_locked(
            root, record, destination, command="cycle-move", stamp=stamp, reason=reason, now=now,
            by="human", parent=new_parent, old_campaign_folder=source.parent)
        stale = list(dict.fromkeys(stale + [old_campaign_id, written["campaign_id"]]))
        artifact_locator.update_indexes(root, stale)
        _flush_cycle_pending_locked(root, cycle_id)
    finally:
        artifact_admission._release_lock(root, lock_fd)
    if old_campaign_id != written["campaign_id"]:
        _drop_group_membership(root, old_campaign_id, [cycle_id, *member_ids])
    return {"status": "moved", "cycle_id": cycle_id, "campaign_id": written["campaign_id"],
            "parent_cycle_id": written.get("parent_cycle_id"), "cycle_dir": str(destination)}


# -- delete -------------------------------------------------------------------------------


def _tombstone_cycle_locked(root: Path, record: Mapping[str, Any], *, where: str, command: str, stamp: str,
                            reason: Optional[str], now: Optional[float], by: str,
                            digest: Optional[str]) -> Dict[str, Any]:
    """The cycle record says the cycle is deleted (the admission lock is held).

    The record stays, with the time and its history line, so the ID is never issued again and an earlier
    reference reads "deleted".  An open cycle's interim files go with it."""
    line = _command_line(
        command=command, stamp=stamp, target_type="cycle", target_id=record["cycle_id"], target_path=where,
        operation="delete", field="state", before={"value": {"manifest_digest": digest, "path": where}},
        after={"value": None}, reason=reason, now=now, by=by)
    updated = dict(record, deleted_at=_rfc3339(now), deleted_by=command)  # a record of who, not a gate
    written = _with_cycle_lines(updated, [line])
    _write_cycle_record(root, written, exclusive=False)
    if record.get("state") == "open":
        remove_interim(root, record["cycle_id"])
    return written


def _retire_rows(root: Path, cycle_ids: Sequence[str]) -> None:
    """Drop the cycles' current rows from the index; every ID they declared stays owned (one read, one write)."""
    index = artifact_admission.load_index(root)
    retired = index
    for cycle_id in cycle_ids:
        retired = artifact_index.retire(retired, cycle_id)
    if retired is not index:
        artifact_admission._write_index(root, retired)


def _remove_folder(root: Path, directory: Path) -> None:
    root_resolved = Path(root).resolve()
    if directory.is_symlink():
        raise ProducerError("record-locator-invalid", str(directory))
    if not directory.exists():
        return
    resolved = directory.resolve()
    if root_resolved not in resolved.parents or (root_resolved / "campaigns") not in resolved.parents:
        raise ProducerError("record-locator-invalid", str(directory))
    shutil.rmtree(directory)
    _fsync_dir(directory.parent)


def _reparent_children_locked(root: Path, deleted_ids: Sequence[str], *, command: str, stamp: str,
                              reason: Optional[str], now: Optional[float], by: str,
                              folders: Optional[Mapping[str, Path]] = None
                              ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """No surviving cycle names a cycle that is gone as its parent (the admission lock is held).

    Whoever names a deleted cycle (the ones deleted now, an earlier delete, or an ID with no record)
    takes the nearest ancestor that is still there, or none, as `cycle-move --parent` would: the
    record, a closed cycle's manifest (a new document) and the index follow and one history line is
    left.  A child whose folder cannot be found has its record mended and nothing else; it is reported
    with `folder: "missing"`.  Returns the rows `{cycle_id, before, after}` and the campaign IDs whose
    locator index is now stale.  `folders` are the paths a reconcile already found."""
    records = {record["cycle_id"]: record for record in list_cycle_records(root) if record.get("cycle_id")}
    gone = set(deleted_ids)

    def departed(cycle_id: str) -> bool:
        record = records.get(cycle_id)
        return cycle_id in gone or record is None or bool(record.get("deleted_at"))

    def surviving_ancestor(child_id: str, start: str) -> Optional[str]:
        seen: Set[str] = set()
        node: Optional[str] = start
        while node and node not in seen and node != child_id:
            if not departed(node):
                return node
            seen.add(node)
            node = (records.get(node) or {}).get("parent_cycle_id")
        return None

    rows: List[Dict[str, Any]] = []
    stale: List[str] = []
    for child_id, child in sorted(records.items()):
        before = child.get("parent_cycle_id")
        if child.get("deleted_at") or not before or not departed(before):
            continue
        after = surviving_ancestor(child_id, before)
        folder = (folders or {}).get(child_id)
        if folder is None:
            try:
                folder = cycle_dir(root, child["campaign_id"], child_id, child)
            except (ProducerError, artifact_locator.LocatorError):
                folder = None
        row: Dict[str, Any] = {"cycle_id": child_id, "before": before, "after": after}
        import artifact_request
        anchor = artifact_request.scope_anchor(folder) if folder is not None else None
        campaign_folder = anchor.parent if anchor is not None else folder.parent if folder is not None else None
        if folder is not None and folder.is_dir() and (campaign_folder / "campaign.json").is_file():
            _written, touched = _adopt_location_locked(
                root, child, folder, command=command, stamp=stamp, reason=reason, now=now, by=by, parent=after)
            stale += touched
        else:
            updated = dict(child, parent_cycle_id=after)
            if after is None:
                updated.pop("parent_cycle_state_at_begin", None)
            else:
                updated["parent_cycle_state_at_begin"] = (records.get(after) or {}).get("state", "open")
            line = _command_line(
                command=command, stamp=stamp, target_type="cycle", target_id=child_id,
                target_path=_last_known_path(root, child), operation="update", field="parent",
                before={"value": before}, after={"value": after}, reason=reason, now=now, by=by)
            _write_cycle_record(root, _with_cycle_lines(updated, [line]), exclusive=False)
            row["folder"] = "missing"
        _flush_cycle_pending_locked(root, child_id)
        rows.append(row)
    return rows, stale


def delete_cycle(root: Path, cycle_id: str, *, reason: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """D-126: delete a cycle's folder and take it out of the index and the lists.

    The record stays (with `deleted_at`), the preserved manifest copies stay, and what the cycle
    published to `shared/` stays.  The current manifest is copied *before* the folder goes, so a
    cycle closed before copies existed still has its last document.  A run that stopped between its
    steps is finished by the next call.  A route that was still running ends as a route with no
    output (`finalize` says so); nothing puts the folder back."""
    root = Path(root).resolve()
    stamp = _command_stamp()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        try:
            directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
        except (ProducerError, artifact_locator.LocatorError):
            directory = None
        finishing = bool(record.get("deleted_at"))
        already_gone = finishing and (directory is None or not directory.exists())
        if already_gone and record.get("route_capability") != "route-frame":
            return {"status": "already-deleted", "cycle_id": cycle_id}
        campaign_id = record["campaign_id"]
        affected = {cycle_id: (record, directory)}
        import artifact_request
        if directory is not None and directory.is_dir():
            for member in artifact_request.members(root, directory, include_deleted=True):
                affected[member.name] = (read_cycle_record(root, member.name), member)
        if already_gone and record.get("route_capability") == "route-frame":
            # All member tombstones precede physical removal. A retry after
            # removal can finish index/list cleanup from those existing rows.
            for child in list_cycle_records(root):
                if (child.get("deleted_at") and child.get("deleted_by") == "delete"
                        and (child.get("request_binding") or {}).get("cycle_id") == cycle_id):
                    affected[child["cycle_id"]] = (child, None)
        deleted_ids = list(affected)
        campaigns = list(dict.fromkeys(item[0]["campaign_id"] for item in affected.values()))
        # Preserve every closed envelope before deleting the shared folder;
        # deleting the frame is not completion of a member's route or work.
        for cid in sorted(affected, key=lambda cid: (cid == cycle_id, cid)):
            current, folder = affected[cid]
            if current.get("deleted_at"):
                continue
            where = _last_known_path(root, current, _cycle_rel(root, folder) if folder is not None else "")
            digest = current.get("manifest_digest")
            if folder is not None:
                _preserve_current_manifest(root, current, folder)
                found = _read_manifest_raw(folder)
                if found is not None:
                    digest = artifact_manifest.manifest_digest(found[1])
            _tombstone_cycle_locked(root, current, where=where, command="delete", stamp=stamp, reason=reason,
                                   now=now, by="human", digest=digest)
        if directory is not None:
            _remove_folder(root, directory)
        for cid in campaigns:
            campaign = read_campaign(root, cid)
            if campaign is not None and any(c in deleted_ids for c in campaign.get("cycles", [])):
                _write_campaign(root, dict(campaign, cycles=[c for c in campaign["cycles"] if c not in deleted_ids]), exclusive=False)
        _retire_rows(root, deleted_ids)
        reparented, touched = _reparent_children_locked(
            root, deleted_ids, command="delete", stamp=stamp, reason=reason, now=now, by="human")
        artifact_locator.update_indexes(root, list(dict.fromkeys(campaigns + touched)))
        for cid in deleted_ids:
            _flush_cycle_pending_locked(root, cid)
    finally:
        artifact_admission._release_lock(root, lock_fd)
    for cid in campaigns:
        _drop_group_membership(root, cid, deleted_ids)
    if already_gone:
        return {"status": "already-deleted", "cycle_id": cycle_id}
    done = {"status": "deleted", "cycle_id": cycle_id, "campaign_id": campaign_id}
    if len(deleted_ids) > 1:
        done["deleted_cycle_ids"] = deleted_ids
    return dict(done, reparented=reparented) if reparented else done


def delete_campaign(root: Path, campaign: str, *, reason: Optional[str] = None,
                    now: Optional[float] = None) -> Dict[str, Any]:
    """D-126: delete a campaign's folder with every cycle in it.

    Each member leaves what a deleted cycle leaves, and the campaign's own identity, last path and
    time are kept in `.runtime/artifact-producer/v1/campaigns/<id>.json` (that is a record of the
    root's runtime, not a folder or a `campaign.json`: nothing lists the campaign again)."""
    root = Path(root).resolve()
    stamp = _command_stamp()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        if artifact_identity.is_well_formed(campaign, "campaign") and read_campaign(root, campaign) is None:
            if read_campaign_tombstone(root, campaign) is not None:
                return {"status": "already-deleted", "campaign_id": campaign}
        found = _resolve_campaign_argument(root, campaign)
        campaign_id = found["campaign_id"]
        folder = campaign_dir(root, campaign_id, found)
        last_path = _cycle_rel(root, folder)
        by = "human"
        members, _detached = artifact_campaign.campaign_records(root, campaign_id)
        gone: List[str] = []
        for member in members:
            if member.get("deleted_at"):
                continue
            member_dir = None
            try:
                member_dir = cycle_dir(root, campaign_id, member["cycle_id"], member)
            except (ProducerError, artifact_locator.LocatorError):
                pass
            digest = member.get("manifest_digest")
            if member_dir is not None:
                _preserve_current_manifest(root, member, member_dir)
            _tombstone_cycle_locked(
                root, member, where=_cycle_rel(root, member_dir) if member_dir is not None else "",
                command="delete", stamp=stamp, reason=reason, now=now, by=by, digest=digest)
            gone.append(member["cycle_id"])
        runtime = campaign_runtime_record(root, campaign_id) or {
            "schema_version": 1, "contract": CONTRACT, "campaign_id": campaign_id}
        runtime.update(key=found.get("key"), title=found.get("title"), goal=found.get("goal"),
                       locator=found.get("locator"), last_path=last_path, deleted_at=_rfc3339(now),
                       cycles=list(found.get("cycles", [])), state=found.get("state"))
        line = _command_line(
            command="delete", stamp=stamp, target_type="campaign", target_id=campaign_id, target_path=last_path,
            operation="delete", field="state", before={"value": {"manifest_digest": None, "path": last_path}},
            after={"value": None}, reason=reason, now=now, by=by)
        runtime["history_pending"] = _merge_pending(runtime.get("history_pending") or [], [line])
        _write_campaign_runtime_record(root, runtime)
        _remove_folder(root, folder)
        _retire_rows(root, gone)
        reparented, touched = _reparent_children_locked(
            root, gone, command="delete", stamp=stamp, reason=reason, now=now, by=by)
        artifact_locator.update_indexes(root, list(dict.fromkeys([campaign_id] + touched)))
        for cycle_id in gone:
            _flush_cycle_pending_locked(root, cycle_id)
        _flush_campaign_pending_locked(root, campaign_id)
    finally:
        artifact_admission._release_lock(root, lock_fd)
    done = {"status": "deleted", "campaign_id": campaign_id, "cycle_ids": gone}
    return dict(done, reparented=reparented) if reparented else done


def _scan_layout(root: Path) -> _LayoutScan:
    """Where every campaign and cycle ID sits, read from the folders alone (a `lstat` and a few small files each).

    A symbolic link, an unreadable folder, a folder that carries no ID or an ID seen twice makes the
    answer `complete=False`: such a scan can say where something moved to, never that something is gone."""
    scan = _LayoutScan({}, {}, {}, set(), True)
    base = Path(root) / "campaigns"
    try:
        names = sorted(os.listdir(str(base)))
    except FileNotFoundError:
        return scan
    except OSError:
        scan.complete = False
        return scan
    for name in names:
        if name.startswith("."):
            continue
        entry = base / name
        try:
            mode = os.lstat(str(entry)).st_mode
        except OSError:
            scan.complete = False
            continue
        if stat.S_ISLNK(mode):
            scan.complete = False
            continue
        if not stat.S_ISDIR(mode):
            continue
        campaign = _read_json(entry / "campaign.json")
        campaign_id = campaign.get("campaign_id") if campaign else None
        if not artifact_identity.is_well_formed(campaign_id, "campaign"):
            scan.complete = False
            continue
        if campaign_id in scan.campaigns:
            scan.duplicates.add(campaign_id)
            scan.complete = False
            continue
        scan.campaigns[campaign_id] = entry
        try:
            children = sorted(os.listdir(str(entry)))
        except OSError:
            scan.complete = False
            continue
        for child in children:
            path = entry / child
            if child.startswith(".") or child in {"journals", artifact_locator.CAMPAIGN_EVENTS_DIR}:
                continue
            try:
                mode = os.lstat(str(path)).st_mode
            except OSError:
                scan.complete = False
                continue
            if stat.S_ISLNK(mode):
                scan.complete = False
                continue
            if not stat.S_ISDIR(mode):
                continue
            if child == "cycles":
                scan.complete = False  # an old layout: moves are not judged from it
                continue
            cycle_id = _folder_cycle_id(path)
            if cycle_id is None:
                scan.complete = False
                continue
            if cycle_id in scan.cycles or cycle_id in scan.duplicates:
                scan.duplicates.add(cycle_id)
                scan.cycles.pop(cycle_id, None)
                scan.folder_campaign.pop(cycle_id, None)
                scan.complete = False
                continue
            scan.cycles[cycle_id] = path
            scan.folder_campaign[cycle_id] = campaign_id
            import artifact_request
            for member in artifact_request.members(root, path):
                member_id = member.name
                if member_id in scan.cycles or member_id in scan.duplicates:
                    scan.duplicates.add(member_id)
                    scan.cycles.pop(member_id, None)
                    scan.folder_campaign.pop(member_id, None)
                    scan.complete = False
                    continue
                scan.cycles[member_id] = member
                scan.folder_campaign[member_id] = campaign_id
    return scan


def _hand_changes(root: Path, scan: _LayoutScan, published: Mapping[str, str]) -> Dict[str, Any]:
    """What the folders say that the records do not (read-only)."""
    changes: Dict[str, Any] = {"campaign_paths": [], "cycle_moves": [], "cycle_revive": [],
                               "cycle_gone": [], "campaign_gone": []}
    for campaign_id, folder in sorted(scan.campaigns.items()):
        campaign = _read_json(folder / "campaign.json") or {}
        # A record that never carried a readable locator (an old W7 campaign) is not rewritten.
        if isinstance(campaign.get("locator"), str) and campaign["locator"] != folder.name:
            changes["campaign_paths"].append((campaign_id, folder))
    for cycle_id, folder in sorted(scan.cycles.items()):
        import artifact_request
        anchor = artifact_request.scope_anchor(folder)
        if published.get(cycle_id) == _cycle_rel(root, folder) and anchor is None:
            continue
        record = read_cycle_record(root, cycle_id)
        if record is not None and record.get("deleted_at"):
            # Only what a reconcile took for gone comes back with its folder; an explicit `delete` stays deleted.
            if record.get("deleted_by") == "reconcile" and read_campaign_tombstone(
                    root, scan.folder_campaign[cycle_id]) is None:
                changes["cycle_revive"].append((cycle_id, folder))
            continue
        if record is None:
            continue
        expected_locator = anchor.name if anchor is not None else folder.name
        if record.get("campaign_id") != scan.folder_campaign[cycle_id] or (
                isinstance(record.get("locator"), str) and record["locator"] != expected_locator):
            changes["cycle_moves"].append((cycle_id, folder))
    if scan.complete:
        for identifier in sorted(published):
            if identifier in scan.cycles or identifier in scan.campaigns or identifier in scan.duplicates:
                continue
            if artifact_identity.is_well_formed(identifier, "cycle"):
                record = read_cycle_record(root, identifier)
                if (record is not None and not record.get("deleted_at") and not record.get("journal_migration")
                        and _closed_record(record)):
                    changes["cycle_gone"].append(identifier)
            elif artifact_identity.is_well_formed(identifier, "campaign"):
                if read_campaign_tombstone(root, identifier) is None:
                    changes["campaign_gone"].append(identifier)
    return changes


class _PreManifest(NamedTuple):
    """A closed cycle's manifest as read before the lock, with the `lstat` fingerprint it was read under."""
    found: Optional[Tuple[bytes, Dict[str, Any]]]
    digest: Optional[str]
    fingerprint: Optional[Tuple[int, int, int]]


def _manifest_fingerprint(folder: Path) -> Optional[Tuple[int, int, int]]:
    try:
        seen = os.lstat(str(Path(folder) / "manifest.json"))
    except OSError:
        return None
    return (seen.st_size, seen.st_mtime_ns, seen.st_ino)


def _preread_manifests(root: Path, changes: Mapping[str, Any]) -> Dict[str, _PreManifest]:
    """Read, with no lock held, each moved cycle's surviving manifest and identity."""
    out: Dict[str, _PreManifest] = {}
    for cycle_id, folder in list(changes["cycle_moves"]) + list(changes["cycle_revive"]):
        record = read_cycle_record(root, cycle_id)
        if record is None:
            continue
        fingerprint = _manifest_fingerprint(folder)
        found = _read_manifest_raw(folder)
        out[cycle_id] = _PreManifest(
            found, artifact_manifest.manifest_digest(found[1]) if found is not None else None, fingerprint)
    return out


def reconcile_root(root: Path, *, now: Optional[float] = None) -> Dict[str, Any]:
    """D-126: find what was moved, renamed or removed by hand and make the records say so.

    A cycle folder carried to another campaign, a folder renamed, a folder or a whole campaign removed
    is noticed by the next listing, begin or campaign close: the cycle (or campaign) keeps its ID, the
    records, lists, manifest and index follow, and one history line (by `rule`) is left.  No error, no
    recovery command.  Nothing is taken for gone unless every folder of the root could be read; a
    copy, a link or a folder with no ID is left alone.  Best effort: it never fails its caller."""
    try:
        root = Path(root).resolve()
        if not is_active(root):
            return {"status": "inactive"}
        # The folders are read with no lock held (§45 D-124); the lock only confirms what was found.
        # A change that no longer holds when the lock is taken is looked for once more.
        for _attempt in range(2):
            published = artifact_locator._load_index(root)
            if published is None:
                # INDEX is a disposable cache. The admission rows retain
                # last locations even when the entire campaigns tree is gone.
                index = artifact_admission.load_index(root)
                published = {}
                for cycle_id, row in index.cycles.items():
                    where = row.get("cycle_path") if isinstance(row, dict) else None
                    record = read_cycle_record(root, cycle_id)
                    if isinstance(where, str) and record is not None:
                        published[cycle_id] = where
                        published[record["campaign_id"]] = str(Path(where).parent)
            scan = _scan_layout(root)
            _observe_root_controls(root, scan, now=now)
            changes = _hand_changes(root, scan, published)
            if not any(changes.values()):
                return {"status": "unchanged"}
            seen: frozenset = frozenset()
            if changes["cycle_gone"] or changes["campaign_gone"]:
                changes, seen = _look_again_before_gone(root, changes)
                if not any(changes.values()):
                    return {"status": "unchanged"}
            result = _reconcile_locked_run(root, now, scan, published, changes, seen,
                                           _preread_manifests(root, changes))
            if result is not None:
                return result
        return {"status": "skipped", "reason": "layout-changed-during-scan"}
    except Exception as exc:  # noqa: BLE001 -- finding a hand-made change never fails the command that asked
        return {"status": "skipped", "reason": type(exc).__name__, "detail": str(exc)}


def _revive_cycle_locked(root: Path, record: Mapping[str, Any], folder: Path, *, stamp: str,
                         now: Optional[float], by: str,
                         old_campaign_folder: Optional[Path],
                         manifest: Any = _UNSET, manifest_digest: Optional[str] = None) -> List[str]:
    """A cycle a reconcile took for gone has its folder again (the admission lock is held).

    The tombstone comes off, the location is adopted as a hand-made move is, the cycle joins its campaign's
    list, the index gets its row from the current manifest, and one line says it is back.  Returns the campaign
    IDs whose locator index is now stale.  `manifest` and `manifest_digest` are what the reconcile read
    before the lock (see `_adopt_location_locked`)."""
    cycle_id = record["cycle_id"]
    where = _cycle_rel(root, folder)
    revived = {key: value for key, value in record.items() if key not in ("deleted_at", "deleted_by")}
    index = artifact_admission.load_index(root)  # the one read of this locked section
    found = manifest if manifest is not _UNSET else (_read_manifest_raw(folder) if _closed_record(record) else None)
    if found is not None and (cycle_id not in index.cycles or cycle_id not in index.manifests):
        # The row comes back first, from the manifest the folder carries; a changed campaign is then an
        # ordinary replacement of that row.
        digest = manifest_digest or artifact_manifest.manifest_digest(found[1])
        index = artifact_index.apply(index, found[1], cycle_path=where, manifest_digest=digest,
                                     idempotency_key=cycle_id)
        artifact_admission._write_index(root, index)
        revived["manifest_digest"] = digest
    written, touched = _adopt_location_locked(
        root, revived, folder, command="reconcile", stamp=stamp, reason="reconcile", now=now, by=by,
        old_campaign_folder=old_campaign_folder, index=index, manifest=found,
        manifest_digest=manifest_digest)
    import artifact_request
    anchor = artifact_request.scope_anchor(folder)
    _edit_campaign_members(root, anchor.parent if anchor is not None else folder.parent, cycle_id, joining=True)
    line = _command_line(
        command="reconcile", stamp=stamp, target_type="cycle", target_id=cycle_id, target_path=where,
        operation="update", field="path", before={"value": None}, after={"value": where},
        reason="reconcile", now=now, by=by)
    _write_cycle_record(root, _with_cycle_lines(written, [line]), exclusive=False)
    return touched + [written["campaign_id"]]


def _look_again_before_gone(root: Path, changes: Mapping[str, Any]) -> Tuple[Dict[str, Any], frozenset]:
    """A folder carried from a campaign the scan had not reached yet into one it had already read is
    in neither: before anything is taken for gone, the folders are read once more (no lock held).

    What the second reading sees is left out of the gone lists (the next look finds where it moved to),
    and a second reading that is not complete takes nothing for gone.  Returns the changes and the cycle
    IDs the second reading saw."""
    again = _scan_layout(root)
    if not again.complete:
        return dict(changes, cycle_gone=[], campaign_gone=[]), frozenset()
    seen = frozenset(again.cycles) | frozenset(again.duplicates)
    return dict(
        changes, cycle_gone=[item for item in changes["cycle_gone"] if item not in seen],
        campaign_gone=[item for item in changes["campaign_gone"]
                       if item not in again.campaigns and item not in again.duplicates]), seen


def _hand_changes_hold(root: Path, changes: Mapping[str, Any], published: Mapping[str, str],
                       manifests: Optional[Mapping[str, _PreManifest]] = None) -> bool:
    """Under the lock, with `lstat` and the small files that name an ID: is each change the scan found still there?"""
    def gone(relative: str) -> bool:
        try:
            os.lstat(str(Path(root) / relative))
        except (FileNotFoundError, NotADirectoryError):
            return True
        return False

    def folder_is(path: Path) -> bool:
        try:
            return stat.S_ISDIR(os.lstat(str(path)).st_mode)
        except OSError:
            return False

    for campaign_id, folder in changes["campaign_paths"]:
        campaign = _read_json(folder / "campaign.json") if folder_is(folder) else None
        if not campaign or campaign.get("campaign_id") != campaign_id or campaign.get("locator") == folder.name:
            return False
    for cycle_id, folder in list(changes["cycle_moves"]) + list(changes["cycle_revive"]):
        pre = (manifests or {}).get(cycle_id)
        document = (pre.found[1] if pre is not None and pre.found is not None else {}) \
            if manifests is not None else None
        if not folder_is(folder) or _folder_cycle_id(folder, document) != cycle_id:
            return False
    for identifier in list(changes["cycle_gone"]) + list(changes["campaign_gone"]):
        if identifier not in published or not gone(published[identifier]):
            return False
    return True


def _reconcile_locked_run(root: Path, now: Optional[float], scan: _LayoutScan, published: Mapping[str, str],
                          changes: Mapping[str, Any], seen: frozenset = frozenset(),
                          manifests: Optional[Mapping[str, _PreManifest]] = None) -> Optional[Dict[str, Any]]:
    """Apply what `reconcile_root` found, under the admission lock.  `None` when what was found
    no longer holds, for the caller to look again.  `manifests` are the closed cycles' manifests read
    before the lock; under it only their `lstat` fingerprint is looked at (a manifest that changed since
    is for the next look).  Left out, they are read here."""
    held = artifact_admission.holds_lock(root)
    lock_fd = None if held else artifact_admission._acquire_lock(root, REFRESH_ADMISSION_WAIT_SECONDS, now=now)
    try:
        current_index = artifact_locator._load_index(root)
        if ((current_index is not None and current_index != published)
                or not _hand_changes_hold(root, changes, published, manifests)):
            return None
        if manifests is not None:
            for cycle_id, folder in list(changes["cycle_moves"]) + list(changes["cycle_revive"]):
                current = read_cycle_record(root, cycle_id)
                if current is None:
                    continue
                pre = manifests.get(cycle_id)
                if pre is None or pre.fingerprint != _manifest_fingerprint(folder):
                    return None
        stamp = _command_stamp()
        by = _HAND_ACTOR
        stale: List[str] = []
        result: Dict[str, Any] = {"status": "reconciled", "campaign_paths": [], "cycle_moves": [],
                                  "cycle_gone": [], "campaign_gone": []}
        for campaign_id, folder in changes["campaign_paths"]:
            campaign = _read_json(folder / "campaign.json") or {}
            old_rel = f"campaigns/{campaign.get('locator')}"
            base = artifact_locator.locator_base(str(campaign.get("created_on") or ""), campaign.get("slug") or "") \
                if campaign.get("created_on") else ""
            folded = artifact_campaign.fold_campaign(root, folder / "campaign.json", campaign)
            updated = dict(folded, locator=folder.name, locator_suffix=_locator_suffix(base, folder.name))
            artifact_campaign.check_campaign_write(root, folder / "campaign.json", updated)
            _write_atomic(folder / "campaign.json", _json_bytes(updated))
            _campaign_lines_locked(root, campaign_id, [_command_line(
                command="reconcile", stamp=stamp, target_type="campaign", target_id=campaign_id,
                target_path=_cycle_rel(root, folder), operation="update", field="path",
                before={"value": old_rel}, after={"value": _cycle_rel(root, folder)}, reason="reconcile",
                now=now, by=by)])
            _retarget_index_paths(root, old_rel, _cycle_rel(root, folder))
            stale.append(campaign_id)
            result["campaign_paths"].append(campaign_id)
        left: Dict[str, List[str]] = {}
        for cycle_id, folder in changes["cycle_moves"]:
            record = read_cycle_record(root, cycle_id)
            if record is None:
                continue
            was = record["campaign_id"]
            pre = (manifests or {}).get(cycle_id)
            read = {"manifest": pre.found, "manifest_digest": pre.digest} if pre is not None else {}
            written, touched = _adopt_location_locked(
                root, record, folder, command="reconcile", stamp=stamp, reason="reconcile", now=now, by=by,
                old_campaign_folder=scan.campaigns.get(was), **read)
            _flush_cycle_pending_locked(root, cycle_id)
            stale += touched + [was]
            if was != written["campaign_id"]:
                left.setdefault(was, []).append(cycle_id)
            result["cycle_moves"].append(cycle_id)
        for cycle_id, folder in changes["cycle_revive"]:
            record = read_cycle_record(root, cycle_id)
            if record is None or record.get("deleted_by") != "reconcile" or not record.get("deleted_at"):
                continue
            was = record["campaign_id"]
            pre = (manifests or {}).get(cycle_id)
            read = {"manifest": pre.found, "manifest_digest": pre.digest} if pre is not None else {}
            touched = _revive_cycle_locked(root, record, folder, stamp=stamp, now=now, by=by,
                                           old_campaign_folder=scan.campaigns.get(was), **read)
            _flush_cycle_pending_locked(root, cycle_id)
            stale += touched + [was]
            result["cycle_moves"].append(cycle_id)  # back where a folder is: no field of its own
        gone_cycles: List[str] = []
        for cycle_id in changes["cycle_gone"]:
            record = read_cycle_record(root, cycle_id)
            if (record is None or record.get("deleted_at") or record.get("journal_migration")
                    or (record.get("relocation") or {}).get("artifact_root")):
                continue
            where = _last_known_path(root, record, published.get(cycle_id, ""))
            _tombstone_cycle_locked(root, record, where=where, command="reconcile", stamp=stamp,
                                    reason="reconcile", now=now, by=by, digest=record.get("manifest_digest"))
            _edit_campaign_members(root, scan.campaigns.get(record["campaign_id"]), cycle_id, joining=False)
            stale.append(record["campaign_id"])
            gone_cycles.append(cycle_id)
            result["cycle_gone"].append(cycle_id)
        for campaign_id in changes["campaign_gone"]:
            last_path = published.get(campaign_id, "") or f"campaigns/{campaign_id}"
            members = [r for r in list_cycle_records(root) if r.get("campaign_id") == campaign_id]
            for member in members:
                if (member.get("deleted_at") or not _closed_record(member) or member["cycle_id"] in seen
                        or (member.get("relocation") or {}).get("artifact_root")):
                    continue
                _tombstone_cycle_locked(root, member, where=_last_known_path(root, member, published.get(member["cycle_id"], "")),
                                        command="reconcile", stamp=stamp, reason="reconcile", now=now, by=by,
                                        digest=member.get("manifest_digest"))
                gone_cycles.append(member["cycle_id"])
            runtime = campaign_runtime_record(root, campaign_id) or {
                "schema_version": 1, "contract": CONTRACT, "campaign_id": campaign_id}
            runtime.update(last_path=last_path, deleted_at=_rfc3339(now),
                           cycles=[m["cycle_id"] for m in members])
            _write_campaign_runtime_record(root, runtime)
            _campaign_lines_locked(root, campaign_id, [_command_line(
                command="reconcile", stamp=stamp, target_type="campaign", target_id=campaign_id,
                target_path=last_path, operation="delete", field="state",
                before={"value": {"manifest_digest": None, "path": last_path}}, after={"value": None},
                reason="reconcile", now=now, by=by)])
            stale.append(campaign_id)
            result["campaign_gone"].append(campaign_id)
        if gone_cycles:
            _retire_rows(root, gone_cycles)
            for cycle_id in gone_cycles:
                _flush_cycle_pending_locked(root, cycle_id)
            reparented, touched = _reparent_children_locked(
                root, gone_cycles, command="reconcile", stamp=stamp, reason="reconcile", now=now, by=by,
                folders=scan.cycles)
            stale += touched
            if reparented:
                result["reparented"] = reparented
        if stale:
            artifact_locator.update_indexes(root, list(dict.fromkeys(stale)))
    finally:
        if lock_fd is not None:
            artifact_admission._release_lock(root, lock_fd)
    for campaign_id, cycle_ids in left.items():
        _drop_group_membership(root, campaign_id, cycle_ids)
    return result
