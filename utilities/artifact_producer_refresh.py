"""Artifact producer: refreshing sealed cycles (§45 D-124/D-125).

Cycle fact scans, provisional completion, the prescan, the bounded refresh
walk and its state, `refresh_cycle`/`refresh_sweep`, and the sealed-record
commit they share with finalize.

Split out of `artifact_producer.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import fcntl
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import artifact_admission
import artifact_payload
import artifact_cycle_titles
import artifact_identity
import artifact_index
import artifact_lifecycle
import artifact_locator
import artifact_manifest
import artifact_directory_bundle
import route_lineage
import dispatch_lock_order

from artifact_producer_store import (
    _canonical, _digest, _earlier_documents, _ensure_dir, _excluded_by_roots, _json_bytes,
    _read_json, _record_cycle_manifest_path, _rfc3339, _stream_file_facts, _walk_cycle_files,
    _write_atomic, _write_cycle_record, ALGORITHM_VERSION, cycle_dir, cycle_record_path,
    journal_path, producer_dir, ProducerError, read_cycle_record,
)

from artifact_producer_routes import (
    _finalize_route, _remove_journal, _write_journal, cycle_route_admission, load_route,
    route_is_closed,
)

from artifact_producer_history import (
    _close_history_line, _deliver_pending, _flush_cycle_pending_locked, _history_actor,
    _history_deliver_locked, _history_id, _history_module, _merge_pending,
    _observe_control_changes, _refresh_history_lines, _with_pending, _write_history_pending,
)

from artifact_producer_interim import (
    CHECKPOINT_DIR, checkpoint_interval_seconds, closed_cycle_refresh_off, remove_interim,
)

from artifact_producer_manifest import (
    _browser_profile, _bucket_type, _bundle_inventory_holds, _bundle_tree_fingerprints,
    _is_cycle_root_support, _media_type, _observe_marker_directory, _output_paths,
    _outside_inclusion_rule, _payload_inner, _payload_root, _scan_directory_bundles,
    _with_audience,
)

from artifact_producer_lease import (
    _live_review_lease, _path_entry_present,
)


def _manifest_bundle_directories(document: Mapping[str, Any]) -> Set[str]:
    """The raw trees already represented by directory-bundle rows in a sealed document."""
    bundle_artifacts = {row.get("artifact_id"): row for row in document.get("artifacts", [])
                        if isinstance(row, Mapping) and row.get("type") == "directory-bundle"}
    directories: Set[str] = set()
    for row in document.get("artifact_revisions", []) or []:
        if not isinstance(row, Mapping) or row.get("artifact_id") not in bundle_artifacts:
            continue
        artifact = bundle_artifacts[row.get("artifact_id")]
        title = artifact.get("title")
        if (row.get("media_type") == artifact_directory_bundle.MEDIA_TYPE
                and isinstance(title, str) and title and ".." not in title.split("/")):
            directories.add("artifacts/" + title)
    return directories


def _scan_cycle_facts(directory: Path, *, excluded: List[str],
                      excluded_symlinks: List[str],
                      known: Optional[Mapping[str, Sequence[Any]]] = None,
                      fingerprints: Optional[Dict[str, List[int]]] = None,
                      marker_observations: Optional[Dict[str, Any]] = None,
                      excluded_roots: Optional[List[str]] = None) -> List[Tuple[str, str, int]]:
    """The manifest's file facts `(path, digest, size)` under the one inclusion rule.

    Same rule as `_enumerate_output`, read in chunks.  A path a manifest cannot
    name (not a regular file, not a valid locator) is left out and listed as
    excluded: a refresh never turns such a file into a failure.  `known` maps a
    path to `(size, mtime_ns, inode, digest)` from a scan made before the lock was
    taken: a file whose fingerprint still matches keeps that digest instead of
    being read again.  `fingerprints`, when given, receives each file's fingerprint as it was
    before its bytes were read, so a later `lstat` tells whether the digest still stands."""
    facts: List[Tuple[str, str, int]] = []
    artifacts = directory / "artifacts"
    if not artifacts.is_dir() or artifacts.is_symlink():
        raise ProducerError("artifacts-dir-missing", str(artifacts))
    document = _read_json(directory / "manifest.json")
    bundle_directories = _manifest_bundle_directories(document) if isinstance(document, Mapping) else set()
    for entry in _walk_cycle_files(directory, marker_observations=marker_observations,
                                   excluded_roots=excluded_roots, excluded=excluded,
                                   skip_directories=tuple(bundle_directories)):
        rel = entry.relative_to(directory).as_posix()
        if rel == artifact_locator.CYCLE_BINDING or _payload_root(rel) is None:
            continue
        if any(rel.startswith(path + "/") for path in bundle_directories):
            continue
        if os.path.islink(str(entry)):
            excluded_symlinks.append(rel)
            continue
        if _outside_inclusion_rule(rel) or not entry.is_file() or not artifact_manifest.validate_locator_path(rel).ok:
            excluded.append(rel)
            continue
        remembered = (known or {}).get(rel)
        try:
            st = os.lstat(str(entry))
        except OSError:
            st = None
        if remembered is not None and st is not None and list(remembered[:3]) == _fingerprint(st):
            facts.append((rel, remembered[3], st.st_size))
            if fingerprints is not None:
                fingerprints[rel] = _fingerprint(st)
            continue
        digest, size = _stream_file_facts(entry)
        facts.append((rel, digest, size))
        if fingerprints is not None and st is not None:
            fingerprints[rel] = _fingerprint(st)
    return facts


def _refreshed_document(root: Path, record: Mapping[str, Any], document: Mapping[str, Any],
                        facts: Sequence[Tuple[str, str, int]], *, allocator: artifact_identity.IdAllocator,
                        now: Optional[float],
                        removable: Optional[Iterable[str]] = None,
                        excluded_roots: Sequence[str] = (),
                        ) -> Tuple[Optional[Dict[str, Any]], Dict[str, List[str]]]:
    """The cycle's next document, or `None` when no file differs from `document`.

    Unchanged rows are carried over untouched; a changed file keeps its
    `artifact_id` and gets a new revision; a new file gets a new artifact; a file
    that is gone leaves the rows (its events stay).  A row the inclusion rule
    would not list today is neither changed nor removed.  `cycle`, `routes[]` and
    the terminal fields are never touched.  `removable`, when given, names the only
    declared paths a missing fact may remove: a scan that stopped early has not seen
    the rest of the tree, so a file it did not visit is never read as deleted."""
    declared = {row.get("locator", {}).get("path"): row
                for row in document.get("artifact_revisions", []) or [] if isinstance(row, dict)}
    present = {rel: (digest, size) for rel, digest, size in facts}
    changes: Dict[str, List[str]] = {"added": [], "modified": [], "removed": [], "reclassified": []}
    for rel in sorted(declared):
        if _excluded_by_roots(rel, excluded_roots):
            changes["reclassified"].append(rel)
    for rel, (digest, size) in sorted(present.items()):
        row = declared.get(rel)
        if row is None:
            changes["added"].append(rel)
        elif row.get("content_digest") != digest or row.get("byte_size") != size:
            changes["modified"].append(rel)
    removable_set = None if removable is None else set(removable)
    for rel in sorted(declared):
        if (rel not in present and not _outside_inclusion_rule(rel)
                and not _excluded_by_roots(rel, excluded_roots)
                and (removable_set is None or rel in removable_set)):
            changes["removed"].append(rel)
    if not any(changes.values()):
        return None, changes
    revision_id = allocator.allocate("manifest_revision")
    when = _rfc3339(now)
    routes = [row for row in document.get("routes", []) or [] if isinstance(row, dict)]
    route_id = routes[-1]["route_id"] if routes else record.get("route_id")

    def provenance(digest: str) -> Dict[str, Any]:
        return {"source_manifest_id": document["manifest_id"], "source_revision_id": revision_id,
                "producer_route_id": route_id, "algorithm_version": ALGORITHM_VERSION,
                "schema_version": 1, "source_digest": digest}

    removed = set(changes["removed"])
    reclassified = set(changes["reclassified"])
    gone_artifacts = {row["artifact_id"] for rel, row in declared.items() if rel in removed}
    retired_artifacts = {row["artifact_id"] for rel, row in declared.items() if rel in reclassified}
    modified = set(changes["modified"])
    artifacts = [dict(row) for row in document.get("artifacts", []) or []
                 if isinstance(row, dict) and row.get("artifact_id") not in (gone_artifacts | retired_artifacts)]
    revisions: List[Dict[str, Any]] = []
    candidate_events = [dict(row) for row in document.get("events", []) or []]
    removed_event_ids = artifact_manifest.dispensable_retired_recording_ids(
        candidate_events, retired_artifacts)
    events = [row for row in candidate_events if row.get("event_id") not in removed_event_ids]
    for row in document.get("artifact_revisions", []) or []:
        rel = row.get("locator", {}).get("path")
        if rel in removed or rel in reclassified:
            continue
        if rel in modified:
            digest, size = present[rel]
            row = dict(row, artifact_revision_id=allocator.allocate("artifact_revision"),
                       content_digest=digest, byte_size=size, media_type=_media_type(rel),
                       provenance=provenance(digest))
            events.append({
                "event_id": allocator.allocate("event"), "stream_id": allocator.allocate("stream"),
                "stream_sequence": 1, "event_type": "artifact.revision.recorded", "target_id": row["artifact_id"],
                "actor": {"kind": "producer", "id": record["producer_id"]}, "recorded_at": when,
                "provenance": row["provenance"], "evidence_ids": [], "payload": {"locator": rel}})
        revisions.append(dict(row))
    for rel in changes["added"]:
        digest, size = present[rel]
        artifact_id = allocator.allocate("artifact")
        inner = _payload_inner(rel)
        root_support = _is_cycle_root_support(rel)
        artifacts.append(_with_audience({"artifact_id": artifact_id, "cycle_id": record["cycle_id"],
                                         "role": "support" if root_support else "output",
                                         "type": "file" if root_support else _bucket_type(inner),
                                         "capability": record["capability"],
                                         "title": rel if root_support else inner}, rel))
        revisions.append({
            "artifact_revision_id": allocator.allocate("artifact_revision"), "artifact_id": artifact_id,
            "revision_sequence": 1, "content_digest": digest, "byte_size": size, "media_type": _media_type(rel),
            "locator": {"kind": "cycle-relative", "path": rel}, "provenance": provenance(digest)})
        events.append({
            "event_id": allocator.allocate("event"), "stream_id": allocator.allocate("stream"),
            "stream_sequence": 1, "event_type": "artifact.revision.recorded", "target_id": artifact_id,
            "actor": {"kind": "producer", "id": record["producer_id"]}, "recorded_at": when,
            "provenance": provenance(digest), "evidence_ids": [], "payload": {"locator": rel}})
    refreshed = dict(document)
    refreshed.update(manifest_revision_id=revision_id, artifacts=artifacts, artifact_revisions=revisions,
                     events=events)
    return refreshed, changes


def _reclassified_artifact_ids(document: Mapping[str, Any], changes: Mapping[str, Sequence[str]]) -> set[str]:
    paths = set(changes.get("reclassified", []))
    return {row["artifact_id"] for row in document.get("artifact_revisions", []) or []
            if isinstance(row, dict) and row.get("locator", {}).get("path") in paths
            and isinstance(row.get("artifact_id"), str)}


def _drop_unpublished_snapshot(root: Path, cycle_id: str, journal: Optional[Mapping[str, Any]]) -> None:
    """A journal that never reached its commit point leaves no copy behind: a
    preserved document is a published one."""
    revision_id = (journal or {}).get("manifest_revision_id")
    if not isinstance(revision_id, str) or not artifact_identity.is_well_formed(revision_id, "manifest_revision"):
        return
    try:
        artifact_lifecycle.manifest_snapshot_path(root, cycle_id, revision_id).unlink()
    except (OSError, artifact_lifecycle.LifecycleError):
        pass


def _leaf_terminal_additions(root: Path, record: Mapping[str, Any], document: Mapping[str, Any],
                             facts: Sequence[Tuple[str, str, int]], *,
                             allocator: artifact_identity.IdAllocator, revision_id: str,
                             now: Optional[float]) -> Optional[Dict[str, Any]]:
    """The route row and terminal record a lineage leaf adds when it closes a closed cycle again.

    §45 D-127: a route that continues a closed cycle and closes leaves one more completion
    record; the earlier route's row and record stay as they were.  A route already in the
    document, one still open, one the lineage does not admit, or one whose terminal proof
    does not verify adds nothing -- a refresh never fails for want of one."""
    try:
        leaf = _finalize_route(root, record)
    except ProducerError:
        return None
    if (artifact_lifecycle.manifest_route_row(document, route_id=leaf["route_id"]) is not None
            or not route_is_closed(root, leaf)):
        return None
    identity = artifact_lifecycle.read_root_identity(root)
    if identity is None or not cycle_route_admission(root, record, leaf, finalize=True).allow:
        return None
    try:
        binding, sealed_route = artifact_lifecycle.bind_existing_runtime_route(
            root, route_lineage.canonical_route_path(root, leaf["route_id"]),
            expected_root_id=identity.artifact_root_id)
    except artifact_lifecycle.LifecycleError:
        return None
    if sealed_route.get("route_hash") != leaf["route_hash"]:
        return None
    event_id = allocator.allocate("event")
    cycle_digest = _digest(_canonical([[rel, digest] for rel, digest, _size in facts]))
    event = {
        "event_id": event_id, "stream_id": allocator.allocate("stream"), "stream_sequence": 1,
        "event_type": "route.terminal.recorded", "target_id": record["cycle_id"],
        "actor": {"kind": "system", "id": "capability-route"}, "recorded_at": _rfc3339(now),
        "provenance": {"source_manifest_id": document["manifest_id"], "source_revision_id": revision_id,
                       "producer_route_id": leaf["route_id"], "algorithm_version": ALGORITHM_VERSION,
                       "schema_version": 1, "source_digest": cycle_digest},
        "evidence_ids": [], "payload": {}}
    row = {"artifact_root_id": identity.artifact_root_id, "route_id": leaf["route_id"],
           "route_hash": leaf["route_hash"], "terminal_marker": "pending", "terminal_evidence_id": event_id}
    return {"row": row, "event": event, "binding": binding, "route": sealed_route}


def _provisional_completion_context(root: Path, record: Mapping[str, Any],
                                    expected_root_id: Optional[str], *,
                                    jobs: Optional[Path] = None) -> Optional[Tuple[Any, Dict[str, Any]]]:
    """The live conditions under which a provisional active cycle may become completed.

    Route, lease, closed/admitted route, exact proven non-abandoned outcome: the one place both
    the candidate builder and the publication re-check ask, so they cannot drift apart."""
    try:
        route = _finalize_route(root, record)
    except ProducerError:
        return None
    if (route.get("artifact_root") is None
            or record.get("abandon_reason") or record.get("deleted_at")
            or _live_review_lease(root, record["cycle_id"]) is not None
            or not route_is_closed(root, route)
            or not cycle_route_admission(root, record, route, finalize=True).allow):
        return None
    jobs_path = Path(jobs) if jobs is not None else (
        Path(os.environ["AGENT_DISPATCH_JOBS"]) if os.environ.get("AGENT_DISPATCH_JOBS") else None)
    try:
        binding, sealed_route = artifact_lifecycle.bind_existing_runtime_route(
            root, route_lineage.canonical_route_path(root, route["route_id"]),
            expected_root_id=expected_root_id)
        outcome_raw = Path(binding.outcome_file).read_bytes()
        outcome = json.loads(outcome_raw.decode("utf-8"))
        route_module = artifact_lifecycle._load_capability_route()
        gates = route_module.terminal_gate_observation(
            sealed_route, jobs=jobs_path, exact_terminal=outcome.get("terminal_commit_id") is not None)
        from workflow_state import WorkflowLedger
        workflow = WorkflowLedger(
            sealed_route["route_id"], sealed_route["route_hash"],
            jobs=jobs_path).read_only_state()
        if workflow["workflow_state"] == "CANCELLED" or workflow["journal_unreadable"]:
            return None
    except (artifact_lifecycle.LifecycleError, OSError, ValueError, UnicodeError):
        return None
    if (sealed_route.get("route_hash") != route.get("route_hash")
            or route_module.terminal_gate_proven(gates) is not True):
        return None
    if (outcome.get("route_id") != binding.route_id
            or outcome.get("route_hash") != binding.route_hash
            or outcome.get("route_file") != binding.route_file
            or outcome.get("terminal_gate_proven") is not True
            or outcome.get("autoclose") is not None
            or outcome.get("disposition") in ("abandoned", "operator-decision", "cancelled")):
        return None
    return binding, sealed_route, outcome


def _manifest_terminal_source_digest(root: Path, record: Mapping[str, Any],
                                     document: Mapping[str, Any]) -> Optional[str]:
    """Resolve snapshot artifact facts through the producer's original scan algorithm."""
    manifest_path = _record_cycle_manifest_path(root, record)
    current = _read_json(manifest_path)
    if (not isinstance(current, Mapping)
            or artifact_manifest.canonical_bytes(current) != artifact_manifest.canonical_bytes(document)):
        return None
    directory = manifest_path.parent
    known: Dict[str, List[Any]] = {}
    snapshot_facts: List[Tuple[str, str, int]] = []
    for row in document.get("artifact_revisions", []) or []:
        if not isinstance(row, Mapping):
            return None
        locator = row.get("locator")
        rel = locator.get("path") if isinstance(locator, Mapping) else None
        digest, size = row.get("content_digest"), row.get("byte_size")
        if (not isinstance(rel, str) or not isinstance(digest, str)
                or not isinstance(size, int) or isinstance(size, bool)):
            return None
        snapshot_facts.append((rel, digest, size))
        try:
            observed = os.lstat(str(directory / rel))
            if stat.S_ISREG(observed.st_mode):
                known[rel] = [*_fingerprint(observed), digest]
        except OSError:
            pass
    try:
        facts = _scan_cycle_facts(directory, excluded=[], excluded_symlinks=[], known=known)
    except (OSError, ProducerError, ValueError, TypeError):
        return None
    if sorted(facts) != sorted(snapshot_facts):
        return None
    return _digest(_canonical([[rel, digest] for rel, digest, _size in facts]))


def _historical_handoff_completion_context(root: Path, record: Mapping[str, Any],
                                          document: Mapping[str, Any],
                                          expected_binding: Optional[Mapping[str, Any]], *,
                                          jobs: Optional[Path] = None):
    """Prove the sole historical abandoned shape that exact finish may complete.

    Every owner-pin edge, the abandoned manifest snapshot, the current producer
    binding, and the terminal claim are re-read. Audit route bindings and the
    abandoned enum are never used as authority.
    """
    try:
        import dispatch_contract
        import dispatch_terminal_commit as DTC
        import route_parent_close

        jobs_path = Path(jobs) if jobs is not None else (
            Path(os.environ["AGENT_DISPATCH_JOBS"]) if os.environ.get("AGENT_DISPATCH_JOBS") else None)
        if jobs_path is None:
            return None
        if not isinstance(expected_binding, Mapping) or expected_binding.get("contract") != DTC.CONTRACT:
            return None
        cycle_id = record.get("cycle_id")
        route_id, route_hash = expected_binding.get("route_id"), expected_binding.get("route_hash")
        owner_attempt = expected_binding.get("owner_attempt_id")
        if (record.get("state") != "sealed" or record.get("cycle_state") != "abandoned"
                or record.get("deleted_at")
                or record.get("abandon_reason") not in (None, "operator-decision")
                or not cycle_id or not route_id or not route_hash or not owner_attempt
                or document.get("cycle", {}).get("state") != "abandoned"):
            return None

        record_path = cycle_record_path(root, cycle_id)
        record_stat = record_path.lstat()
        if (not stat.S_ISREG(record_stat.st_mode) or record_path.is_symlink()
                or record_path.resolve() != record_path):
            return None
        raw_record = record_path.read_bytes()
        stored_record = json.loads(raw_record.decode("utf-8"))
        if not isinstance(stored_record, dict) or stored_record != dict(record):
            return None
        identity = artifact_lifecycle.read_root_identity(root)
        if identity is None:
            return None
        root_identity = {"repository_id": identity.repository_id,
                         "artifact_root_id": identity.artifact_root_id}
        if (expected_binding.get("artifact_root") != str(Path(root).resolve())
                or expected_binding.get("root_identity") != root_identity
                or expected_binding.get("cycle_id") != cycle_id
                or expected_binding.get("campaign_id") != record.get("campaign_id")
                or expected_binding.get("producer_id") != record.get("producer_id")
                or expected_binding.get("cycle_record_digest") != DTC.cycle_identity_digest(record)
                or expected_binding.get("route_file") != str(
                    route_lineage.canonical_route_path(root, route_id))):
            return None

        cycle = document.get("cycle") or {}
        input_digest = _digest(_canonical({
            "route_id": record.get("route_id"), "route_hash": record.get("route_hash"),
            "capability": record.get("capability"), "intensity": record.get("intensity"),
        }))
        if (document.get("artifact_root_id") != identity.artifact_root_id
                or document.get("repository_id") != identity.repository_id
                or cycle.get("cycle_id") != cycle_id
                or cycle.get("campaign_id") != record.get("campaign_id")
                or cycle.get("parent_cycle_id") != record.get("parent_cycle_id")
                or cycle.get("started_on") != record.get("started_on")
                or cycle.get("input_digest") != input_digest
                or document.get("producer", {}).get("producer_id") != record.get("producer_id")):
            return None

        snapshots = [entry[1] for entry in artifact_lifecycle.read_manifest_snapshots(root, cycle_id)
                     if isinstance(entry, (list, tuple)) and len(entry) > 1
                     and isinstance(entry[1], Mapping)]
        document_digest = artifact_manifest.manifest_digest(document)
        if not any(artifact_manifest.manifest_digest(old) == document_digest
                   and artifact_manifest.canonical_bytes(old) == artifact_manifest.canonical_bytes(document)
                   for old in snapshots):
            return None

        route_path = route_lineage.canonical_route_path(root, route_id)
        route = load_route(root, route_path, expected_identity={"route_id": route_id,
                                                                 "route_hash": route_hash})
        if (expected_binding.get("route_id") != route.get("route_id")
                or route.get("capability") != record.get("capability")
                or route.get("effective_intensity") != record.get("intensity")):
            return None
        lineage = route_lineage.verified_route_lineage(route, artifact_root=root, observe=True)
        forward = list(reversed(lineage))
        if (len(forward) < 2 or forward[0].get("route_id") != record.get("route_id")
                or forward[0].get("route_hash") != record.get("route_hash")
                or any(item.get("capability") != record.get("capability")
                       or item.get("effective_intensity") != record.get("intensity")
                       for item in forward)):
            return None
        admission = cycle_route_admission(root, record, route, finalize=True, validation_only=True)
        if not admission.allow:
            return None
        for predecessor, successor in zip(forward, forward[1:]):
            proof = route_parent_close.verified_owner_pin_handoff_close(predecessor, jobs=jobs_path)
            prepared = (proof or {}).get("successor")
            if (not proof or not isinstance(prepared, Mapping)
                    or prepared.get("route_id") != successor.get("route_id")
                    or prepared.get("route_hash") != successor.get("route_hash")):
                return None

        old_routes = document.get("routes") or []
        if (not isinstance(old_routes, list) or len(old_routes) not in (1, 2)
                or not all(isinstance(row, Mapping) for row in old_routes)):
            return None
        predecessor_row = old_routes[0]
        if (predecessor_row.get("artifact_root_id") != identity.artifact_root_id
                or predecessor_row.get("route_id") != forward[-2].get("route_id")
                or predecessor_row.get("route_hash") != forward[-2].get("route_hash")
                or predecessor_row.get("terminal_marker") != "pending"
                or predecessor_row.get("terminal_evidence_id") != ""):
            return None
        historical_leaf_row = None
        historical_terminal_event = None
        all_terminal_events = [event for event in document.get("events", []) or []
                               if isinstance(event, Mapping)
                               and event.get("event_type") == "route.terminal.recorded"]
        terminal_events = [event for event in all_terminal_events if event.get("target_id") == cycle_id]
        if len(old_routes) == 1:
            if all_terminal_events:
                return None
        else:
            historical_leaf_row = old_routes[1]
            if (historical_leaf_row.get("artifact_root_id") != identity.artifact_root_id
                    or historical_leaf_row.get("route_id") != forward[-1].get("route_id")
                    or historical_leaf_row.get("route_hash") != forward[-1].get("route_hash")
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}",
                                        str(historical_leaf_row.get("terminal_marker") or ""))
                    or not historical_leaf_row.get("terminal_evidence_id")
                    or len(terminal_events) != 1 or len(all_terminal_events) != 1
                    or terminal_events[0].get("event_id") != historical_leaf_row.get("terminal_evidence_id")
                    or terminal_events[0].get("provenance", {}).get("source_manifest_id")
                        != document.get("manifest_id")
                    or terminal_events[0].get("provenance", {}).get("source_revision_id")
                        != document.get("manifest_revision_id")
                    or terminal_events[0].get("provenance", {}).get("producer_route_id") != route_id
                    or terminal_events[0].get("provenance", {}).get("algorithm_version") != ALGORITHM_VERSION
                    or terminal_events[0].get("provenance", {}).get("schema_version") != 1
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(
                        terminal_events[0].get("provenance", {}).get("source_digest") or ""))
                    or terminal_events[0].get("payload", {}).get("artifact_root_id") != identity.artifact_root_id
                    or terminal_events[0].get("payload", {}).get("route_id") != route_id
                    or terminal_events[0].get("payload", {}).get("route_hash") != route_hash
                    or terminal_events[0].get("payload", {}).get("terminal_marker_digest")
                        != historical_leaf_row.get("terminal_marker")
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(
                        terminal_events[0].get("payload", {}).get("outcome_digest") or ""))):
                return None
            historical_terminal_event = terminal_events[0]
            try:
                source_snapshot = artifact_lifecycle.find_manifest_snapshot(
                    root, cycle_id,
                    manifest_revision_id=historical_terminal_event["provenance"]["source_revision_id"])
                source_digest = (_manifest_terminal_source_digest(root, record, source_snapshot)
                                 if isinstance(source_snapshot, Mapping)
                                 and artifact_manifest.validate(source_snapshot).ok
                                 and artifact_manifest.manifest_digest(source_snapshot)
                                     == artifact_manifest.manifest_digest(document)
                                 and artifact_manifest.canonical_bytes(source_snapshot)
                                     == artifact_manifest.canonical_bytes(document)
                                 else None)
            except (KeyError, TypeError, ValueError, UnicodeError):
                return None
            if (not isinstance(source_snapshot, Mapping)
                    or source_snapshot.get("manifest_id") != historical_terminal_event[
                        "provenance"].get("source_manifest_id")
                    or source_snapshot.get("manifest_revision_id") != historical_terminal_event[
                        "provenance"].get("source_revision_id")
                    or source_digest != historical_terminal_event[
                        "provenance"].get("source_digest")):
                return None
        abandoned = [event for event in document.get("events", []) or []
                     if isinstance(event, Mapping) and event.get("event_type") == "cycle.abandoned"
                     and event.get("target_id") == cycle_id]
        if (len(abandoned) != 1
                or (abandoned[0].get("payload") or {}).get("abandon_reason") != "operator-decision"
                or (abandoned[0].get("provenance") or {}).get("producer_route_id")
                    != forward[-2].get("route_id")
                or any(event.get("event_type") == "cycle.completed"
                       for event in document.get("events", []) or [] if isinstance(event, Mapping))):
            return None

        binding_result = DTC.load_producer_binding(
            artifact_root=root, route_id=route_id, owner_attempt_id=owner_attempt)
        if dict(binding_result.binding or {}) != dict(expected_binding):
            return None
        binding_stat = binding_result.path.lstat()
        if (not stat.S_ISREG(binding_stat.st_mode) or binding_result.path.is_symlink()
                or binding_result.path.resolve() != binding_result.path):
            return None
        owner_binding = DTC.validate_owner_route(
            jobs=jobs_path, route_file=route_path, owner_attempt_id=owner_attempt)
        if (owner_binding.route_id != route_id or owner_binding.route_hash != route_hash
                or owner_binding.route_file != str(route_path) or not route_is_closed(root, route)
                or _live_review_lease(root, cycle_id) is not None):
            return None

        route_module = artifact_lifecycle._load_capability_route()
        gates = route_module.terminal_gate_observation(route, jobs=jobs_path, exact_terminal=True)
        if route_module.terminal_gate_proven(gates) is not True:
            return None
        from workflow_state import WorkflowLedger
        workflow = WorkflowLedger(route_id, route_hash, jobs=jobs_path).read_only_state()
        if workflow.get("journal_unreadable") or workflow.get("workflow_state") == "CANCELLED":
            return None
        binding, sealed_route = artifact_lifecycle.bind_existing_runtime_route(
            root, route_path, expected_root_id=identity.artifact_root_id)
        outcome_path = Path(binding.outcome_file)
        outcome_stat = outcome_path.lstat()
        if (not stat.S_ISREG(outcome_stat.st_mode) or outcome_path.is_symlink()
                or outcome_path.resolve() != outcome_path):
            return None
        outcome_raw = outcome_path.read_bytes()
        outcome = json.loads(outcome_raw.decode("utf-8"))
        if (binding.route_id != route_id or binding.route_hash != route_hash
                or outcome.get("route_id") != route_id or outcome.get("route_hash") != route_hash
                or outcome.get("route_file") != str(route_path)
                or outcome.get("terminal_gate_proven") is not True
                or outcome.get("terminal_owner_attempt_id") != owner_attempt
                or outcome.get("autoclose") is not None
                or outcome.get("disposition") in {"abandoned", "operator-decision", "cancelled"}):
            return None

        terminal_state_path = DTC.terminal_slot(root, route_id, owner_attempt) / "terminal-commit.json"
        terminal_state_stat = terminal_state_path.lstat()
        if (not stat.S_ISREG(terminal_state_stat.st_mode) or terminal_state_path.is_symlink()
                or terminal_state_path.resolve() != terminal_state_path):
            return None
        terminal_state = json.loads(terminal_state_path.read_text(encoding="utf-8"))
        marker_digest = DTC.terminal_marker_digest(list(gates.values()))
        if historical_leaf_row is not None:
            try:
                expected_evidence = artifact_lifecycle._derive_terminal_evidence(
                    document, binding, sealed_route, recorded_gates=gates)
            except (artifact_lifecycle.LifecycleError, OSError, ValueError):
                return None
            expected_leaf = artifact_lifecycle.manifest_route_row(
                expected_evidence, route_id=route_id)
            expected_event = next((event for event in expected_evidence.get("events", [])
                                   if event.get("event_id") == historical_leaf_row.get("terminal_evidence_id")), None)
            if (not isinstance(expected_leaf, Mapping)
                    or dict(expected_leaf) != dict(historical_leaf_row)
                    or not isinstance(expected_event, Mapping)
                    or expected_event.get("payload") != historical_terminal_event.get("payload")
                    or historical_terminal_event.get("payload", {}).get("outcome_digest")
                        != _digest(outcome_raw)):
                return None
        commit_id = DTC.terminal_commit_id(route_id=route_id, route_hash=route_hash,
            owner_attempt_id=owner_attempt, marker_digest=marker_digest,
            producer_digest=binding_result.digest)
        if (not isinstance(terminal_state, dict) or terminal_state.get("state") != "route-closed"
                or terminal_state.get("route_id") != route_id
                or terminal_state.get("route_hash") != route_hash
                or terminal_state.get("owner_attempt_id") != owner_attempt
                or terminal_state.get("terminal_marker_digest") != marker_digest
                or terminal_state.get("producer_binding_digest") != binding_result.digest
                or terminal_state.get("terminal_commit_id") != commit_id
                or outcome.get("terminal_marker_digest") != marker_digest
                or outcome.get("terminal_commit_id") != commit_id):
            return None
        claim = dispatch_contract.terminal_claim_observation(jobs_path, route_id, owner_attempt)
        claim_proof = (claim or {}).get("proof")
        if (not isinstance(claim_proof, Mapping)
                or claim.get("route_id") != route_id
                or claim.get("owner_attempt_id") != owner_attempt
                or claim_proof.get("terminal_commit_id") != commit_id
                or claim_proof.get("terminal_marker_digest") != marker_digest
                or claim_proof.get("producer_binding_digest") != binding_result.digest):
            return None
        request = DTC.TerminalCommitRequest(route_file=route_path, owner_attempt_id=owner_attempt,
                                            jobs=jobs_path, artifact_root=root)
        children = DTC._prove_route_children(request, route, gates)
        if children.status != "proved":
            return None
        return binding, sealed_route, outcome, snapshots, gates
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, AttributeError, ProducerError,
            artifact_lifecycle.LifecycleError, route_lineage.RouteLineageError,
            dispatch_lock_order.LockOrderError):
        return None


def _evaluate_provisional_completion(root: Path, record: Mapping[str, Any], candidate: Mapping[str, Any],
                                     binding: Any, outcome: Mapping[str, Any], *,
                                     preserved: Optional[Sequence[Mapping[str, Any]]] = None,
                                     first_completion: bool = False) -> bool:
    """First-completion proof of a completed candidate: required roles, payload, marker, terminal digests."""
    try:
        decision = artifact_lifecycle.evaluate_cycle_completion(
            candidate, content_root=_record_cycle_manifest_path(root, record).parent,
            route_file=route_lineage.canonical_route_path(root, binding.route_id),
            expected_root_id=binding.artifact_root_id, payload_verified=False,
            inline_finish_id=outcome.get("inline_finish_id"), preserved=preserved,
            first_completion=first_completion)
    except (artifact_lifecycle.LifecycleError, OSError, ValueError):
        return False
    return decision.ok


def _provisional_completion_holds(root: Path, record: Mapping[str, Any], candidate: Mapping[str, Any],
                                  expected_binding: Optional[Mapping[str, Any]] = None, *,
                                  jobs: Optional[Path] = None) -> bool:
    """Re-ask, at publication, what the candidate was built on (called under the admission lock)."""
    manifest_path = _record_cycle_manifest_path(root, record)
    previous = _read_json(manifest_path)
    if not isinstance(previous, Mapping):
        return False
    state = (previous.get("cycle") or {}).get("state")
    if state == "abandoned":
        context = _historical_handoff_completion_context(
            root, record, previous, expected_binding, jobs=jobs)
        if context is None:
            return False
        binding, _sealed_route, outcome, preserved, _gates = context
        return _evaluate_provisional_completion(root, record, candidate, binding, outcome,
                                                preserved=preserved, first_completion=True)
    context = _provisional_completion_context(
        root, record, candidate.get("artifact_root_id"), jobs=jobs)
    if context is None:
        return False
    binding, _sealed_route, outcome = context
    return _evaluate_provisional_completion(root, record, candidate, binding, outcome)


def _provisional_completion_projection(root: Path, record: Mapping[str, Any], document: Mapping[str, Any],
                                       facts: Sequence[Tuple[str, str, int]], *, revision_id: str,
                                       allocator: artifact_identity.IdAllocator,
                                       now: Optional[float],
                                       expected_binding: Optional[Mapping[str, Any]] = None,
                                       previous_document: Optional[Mapping[str, Any]] = None,
                                       jobs: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Bind exact late terminal proof to an active or verified handoff revision."""
    if not isinstance(document.get("cycle"), Mapping):
        return None
    document_state = document["cycle"].get("state")
    if document_state == "abandoned":
        history_source = previous_document or document
        context = _historical_handoff_completion_context(
            root, record, history_source, expected_binding, jobs=jobs)
        if context is None:
            return None
        binding, sealed_route, outcome, preserved, gates = context
        additions = None
        if len(document.get("routes", []) or []) == 1:
            additions = _leaf_terminal_additions(root, record, document, facts,
                allocator=allocator, revision_id=revision_id, now=now)
            if additions is None or additions["binding"].route_id != binding.route_id:
                return None
        candidate = json.loads(json.dumps(document))
        candidate["manifest_revision_id"] = revision_id
        candidate["cycle"]["state"] = "completed"
        cycle_digest = _digest(_canonical([[rel, digest, size] for rel, digest, size in facts]))
        provenance = {"source_manifest_id": document["manifest_id"],
                      "source_revision_id": revision_id, "producer_route_id": binding.route_id,
                      "algorithm_version": ALGORITHM_VERSION, "schema_version": 1,
                      "source_digest": cycle_digest}
        cycle_event = {"event_id": allocator.allocate("event"), "stream_id": allocator.allocate("stream"),
                       "stream_sequence": 1, "event_type": "cycle.completed",
                       "target_id": record["cycle_id"],
                       "actor": {"kind": "producer", "id": record["producer_id"]},
                       "recorded_at": _rfc3339(now), "provenance": provenance,
                       "evidence_ids": [], "payload": {}}
        candidate["events"] = list(candidate.get("events", [])) + [cycle_event]
        if additions is not None:
            candidate["routes"] = list(candidate.get("routes", [])) + [additions["row"]]
            candidate["events"].append(additions["event"])
            try:
                candidate = artifact_lifecycle._derive_terminal_evidence(
                    candidate, additions["binding"], additions["route"], recorded_gates=gates)
            except (artifact_lifecycle.LifecycleError, OSError, ValueError):
                return None
        completion_history = list(preserved)
        if not _evaluate_provisional_completion(root, record, candidate, binding, outcome,
                                               preserved=completion_history, first_completion=True):
            return None
        return candidate
    if document_state != "active":
        return None
    context = _provisional_completion_context(
        root, record, document.get("artifact_root_id"), jobs=jobs)
    if context is None:
        return None
    binding, sealed_route, outcome = context
    rows = [row for row in document.get("routes", []) or [] if isinstance(row, dict)
            and row.get("artifact_root_id") == binding.artifact_root_id
            and row.get("route_id") == binding.route_id and row.get("route_hash") == binding.route_hash]
    if len(rows) != 1 or rows[0].get("terminal_marker") != "pending" or rows[0].get("terminal_evidence_id"):
        return None
    candidate = json.loads(json.dumps(document))
    candidate["manifest_revision_id"] = revision_id
    candidate["cycle"]["state"] = "completed"
    when = _rfc3339(now)
    cycle_digest = _digest(_canonical([[rel, digest, size] for rel, digest, size in facts]))
    provenance = {"source_manifest_id": document["manifest_id"], "source_revision_id": revision_id,
                  "producer_route_id": binding.route_id, "algorithm_version": ALGORITHM_VERSION,
                  "schema_version": 1, "source_digest": cycle_digest}
    cycle_event = {"event_id": allocator.allocate("event"), "stream_id": allocator.allocate("stream"),
                   "stream_sequence": 1, "event_type": "cycle.completed", "target_id": record["cycle_id"],
                   "actor": {"kind": "producer", "id": record["producer_id"]}, "recorded_at": when,
                   "provenance": provenance, "evidence_ids": [], "payload": {}}
    terminal_event = {"event_id": allocator.allocate("event"), "stream_id": allocator.allocate("stream"),
                      "stream_sequence": 1, "event_type": "route.terminal.recorded",
                      "target_id": record["cycle_id"],
                      "actor": {"kind": "system", "id": "capability-route"}, "recorded_at": when,
                      "provenance": provenance, "evidence_ids": [], "payload": {}}
    row = next(row for row in candidate["routes"] if row.get("artifact_root_id") == binding.artifact_root_id
               and row.get("route_id") == binding.route_id and row.get("route_hash") == binding.route_hash)
    row["terminal_evidence_id"] = terminal_event["event_id"]
    candidate["events"] = list(candidate.get("events", [])) + [cycle_event, terminal_event]
    try:
        candidate = artifact_lifecycle._derive_terminal_evidence(candidate, binding, sealed_route)
    except (artifact_lifecycle.LifecycleError, OSError, ValueError):
        return None
    return candidate if _evaluate_provisional_completion(root, record, candidate, binding, outcome) else None


def _binding_route_unrecorded(manifest_path: Path, binding: Mapping[str, Any]) -> bool:
    """Whether the binding names a route (by id, or by hash) the current manifest has no row for."""
    document = _read_json(manifest_path) if _path_entry_present(manifest_path) else None
    if document is None or not (binding.get("route_id") or binding.get("route_hash")):
        return False
    return artifact_lifecycle.manifest_route_row(
        document, route_id=binding.get("route_id"), route_hash=binding.get("route_hash")) is None


def _refresh_journal_resume(root: Path, record: Mapping[str, Any], journal: Mapping[str, Any],
                            *, now: Optional[float] = None) -> bool:
    """Finish a refresh that stopped between its first durable step and its last.

    The journal names the document the refresh meant to publish; its preserved
    copy holds the same bytes.  When `manifest.json` is still the earlier
    document, the copy is published as it was; when it already is the new one,
    only the record and index remain.  A manifest that is neither is not this
    refresh's to settle.  Returns whether the journal was a refresh."""
    if journal.get("state") != "refreshing":
        return False
    cycle_id = record["cycle_id"]
    manifest_path = _record_cycle_manifest_path(root, record)
    new_digest = journal.get("manifest_digest")
    previous_digest = journal.get("previous_manifest_digest")
    current = _read_json(manifest_path)
    current_digest = artifact_manifest.manifest_digest(current) if current is not None else None
    if current_digest == previous_digest and current_digest != new_digest:
        copy = artifact_lifecycle.find_manifest_snapshot(root, cycle_id, manifest_digest=new_digest)
        if copy is None:
            # Nothing was published and the copy never became durable: drop the journal.
            _remove_journal(root, cycle_id)
            return True
        _write_atomic(manifest_path, artifact_manifest.canonical_bytes(copy))
        current, current_digest = copy, new_digest
    if current_digest != new_digest:
        # A person changed/deleted the current file after publication. Leave
        # those bytes alone and abandon this old write intent automatically.
        carried = journal.get("history_pending")
        if isinstance(carried, list):
            _write_cycle_record(root, _with_pending(record, _merge_pending(record.get("history_pending") or [],
                                [entry for entry in carried if isinstance(entry, dict)])),
                                exclusive=False)
            _flush_cycle_pending_locked(root, cycle_id)
        _remove_journal(root, cycle_id)
        return True
    carried = journal.get("history_pending")
    if isinstance(carried, list):
        # History lines the refresh could not hand over before it stopped belong to the record.
        record = _with_pending(record, _merge_pending(record.get("history_pending") or [],
                               [entry for entry in carried if isinstance(entry, dict)]))
    _commit_sealed(root, record, current, new_digest, now=now, previous_digest=previous_digest)
    return True


class _StaleScan(Exception):
    """What a closed cycle's files were read as before the lock no longer holds under it."""


@dataclass
class _Prescan:
    """A cycle's files as they were read with no lock held: the manifest digest they were
    compared with, the `(path, digest, size)` facts, and each file's fingerprint.

    `open_cycle` marks the first close of a cycle with no manifest yet: `directory` and `paths`
    (every file it listed) are what the refresh lock's `lstat` check walks, `violations` and
    `error` are what listing it found (raised under the lock, where the close always raised them),
    and `complete` is false when a budget stopped the reading."""
    manifest_digest: str
    facts: List[Tuple[str, str, int]]
    fingerprints: Dict[str, List[int]]
    excluded: List[str]
    excluded_symlinks: List[str]
    excluded_roots: List[str] = field(default_factory=list)
    marker_observations: Dict[str, Any] = field(default_factory=dict)
    open_cycle: bool = False
    complete: bool = True
    directory: Optional[Path] = None
    paths: List[str] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)
    error: Optional[ProducerError] = None
    bundles: List[artifact_directory_bundle.Tree] = field(default_factory=list)
    bundle_fingerprints: Dict[str, List[int]] = field(default_factory=dict)
    bundle_selection: List[str] = field(default_factory=list)
    root: Optional[Path] = None
    cycle_id: Optional[str] = None


def _prescan_cycle(root: Path, cycle_id: str, *,
                   unless_recorded: Optional[Mapping[str, Any]] = None,
                   include_open: bool = False, exclude_hidden: bool = False,
                   budget: Optional["RefreshBudget"] = None) -> Optional[_Prescan]:
    """Walk and hash a cycle's files before any lock is taken (§45 D-124).

    An explicit `finalize` or `admit-shared` of a closed cycle compares its files with the manifest;
    reading them here means the admission lock only has to `lstat` what changed.  `None` when
    there is nothing to read (the cycle is not closed, has no manifest, or `unless_recorded` names
    a route the manifest already holds) or the read could not be made: an optimisation, never an
    error.  With `include_open`, `finalize` reads an open cycle's files the same way for its first
    close; `budget` then stops the reading between files (`complete` false), keeping the digests read."""
    try:
        record = read_cycle_record(root, cycle_id)
        if record is None or record.get("deleted_at"):
            return None
        if record.get("state") == "open":
            return _prescan_open_cycle(root, record, exclude_hidden=exclude_hidden, budget=budget) \
                if include_open else None
        if record.get("state") != "sealed":
            return None
        manifest_path = _record_cycle_manifest_path(root, record)
        if not _path_entry_present(manifest_path):
            return None
        if unless_recorded is not None and not _binding_route_unrecorded(manifest_path, unless_recorded):
            return None
        if not stat.S_ISREG(os.lstat(str(manifest_path)).st_mode):
            return None  # a link or a pipe is for the locked command to refuse, never to be read here
        document = json.loads(manifest_path.read_bytes().decode("utf-8"))
        scan = _Prescan(artifact_manifest.manifest_digest(document), [], {}, [], [])
        # A sealed cycle's current directory selection is only a race snapshot. Its
        # manifest decides whether those paths stay file rows or are bundle coverage.
        scan.bundle_selection = list(artifact_directory_bundle.select_outermost(
            manifest_path.parent / "artifacts",
            prune_directory=lambda path: _observe_marker_directory(
                manifest_path.parent, path, scan.marker_observations, scan.excluded_roots, scan.excluded)))
        scan.facts = _scan_cycle_facts(
            manifest_path.parent, excluded=scan.excluded, excluded_symlinks=scan.excluded_symlinks,
            known=_read_refresh_state(root, cycle_id).get("files", {}), fingerprints=scan.fingerprints,
            marker_observations=scan.marker_observations, excluded_roots=scan.excluded_roots)
        return scan
    except Exception:  # noqa: BLE001 -- only an optimisation
        return None


def _prescan_open_cycle(root: Path, record: Mapping[str, Any], *, exclude_hidden: bool,
                        budget: Optional["RefreshBudget"]) -> Optional[_Prescan]:
    """The first close's read: list an open cycle, then read each file once, outside every lock.

    A file whose fingerprint is the one an earlier look left in the refresh bookkeeping keeps
    that digest.  A file that moves while it is read has no fingerprint here, so the `lstat`
    check under the lock finds it.  A budget is looked at between files, never inside one, and
    the first file read is always finished."""
    cycle_id = record["cycle_id"]
    directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
    if _path_entry_present(directory / "manifest.json"):
        return None  # a close that stopped after its manifest is for recovery to finish
    scan = _Prescan("", [], {}, [], [], open_cycle=True, directory=directory, root=root, cycle_id=cycle_id)
    try:
        scan.bundles = _scan_directory_bundles(
            directory, selected_out=scan.bundle_selection, marker_observations=scan.marker_observations,
            excluded_roots=scan.excluded_roots, excluded=scan.excluded)
        for tree in scan.bundles:
            scan.bundle_fingerprints.update(_bundle_tree_fingerprints(tree))
        bundle_dirs = [tree.directory for tree in scan.bundles]
        paths, scan.violations = _output_paths(
            directory, exclude_hidden=exclude_hidden, excluded=scan.excluded,
            excluded_symlinks=scan.excluded_symlinks, marker_observations=scan.marker_observations,
            excluded_roots=scan.excluded_roots, skip_directories=bundle_dirs)
    except ProducerError as error:
        scan.error = error  # raised where the locked close always raised it
        return scan
    if scan.violations:
        # The close is refused on these names (`output-invalid`, raised where the locked close
        # always raised it, after the refusals that come before it there); the digests of the
        # other files would decide nothing, so none is read.
        return scan
    bundle_dirs = [tree.directory for tree in scan.bundles]
    journal = _read_json(journal_path(root, cycle_id)) or {}
    owned_paths = {row.get("locator") for row in journal.get("directory_bundles", [])
                   if isinstance(row, Mapping) and isinstance(row.get("locator"), str)}
    paths = [(rel, entry) for rel, entry in paths
             if not any(rel.startswith(prefix + "/") for prefix in bundle_dirs)
             and rel not in owned_paths]
    scan.paths = [rel for rel, _entry in paths]
    scan.paths.extend(scan.bundle_fingerprints)
    known = _read_refresh_state(root, cycle_id).get("files", {})
    for rel, entry in paths:
        try:
            before = os.lstat(str(entry))
        except OSError:
            continue
        fingerprint = _fingerprint(before)
        remembered = known.get(rel)
        if remembered is not None and list(remembered[:3]) == fingerprint:
            scan.facts.append((rel, remembered[3], before.st_size))
            scan.fingerprints[rel] = fingerprint
            continue
        if budget is not None and budget.hashed_files and budget.hash_exhausted():
            scan.complete = False
            break
        try:
            digest, size = _stream_file_facts(entry)
            after = os.lstat(str(entry))
        except (ProducerError, OSError):
            continue  # moved while it was read, or not a plain file now
        if budget is not None:
            budget.hashed_bytes += size
            budget.hashed_files += 1
        if _fingerprint(after) == fingerprint:
            scan.facts.append((rel, digest, size))
            scan.fingerprints[rel] = fingerprint
    return scan


def _keep_prescan_digests(root: Path, cycle_id: str, prescan: _Prescan) -> None:
    """Leave the digests a first close read where the next look finds them (best effort).

    A close that read only part of a big cycle continues from them, and the first refresh after
    the close does not hash the whole cycle again.  `observed_at` is left as it was: this is no
    refresh, so it starts no minimum interval."""
    try:
        state = _read_refresh_state(root, cycle_id)
        files = dict(state.get("files", {}))
        for rel, digest, _size in prescan.facts:
            if rel in prescan.fingerprints:
                files[rel] = [*prescan.fingerprints[rel], digest]
        _write_refresh_state(root, cycle_id, cursor=None, files=files, observed_at=state.get("observed_at"))
    except (OSError, ProducerError):
        pass


def _prescan_misses(directory: Path, prescan: _Prescan, rels: Sequence[str], *,
                    absent: Sequence[str] = ()) -> List[str]:
    """Under a lock, with `lstat` only: the files whose state is no longer what the scan saw.

    `rels` are files the scan read (their fingerprint must be the same), `absent` are files it
    found gone (they must still be)."""
    misses: List[str] = []
    for rel in rels:
        try:
            info = os.lstat(str(directory / rel))
            expected = prescan.bundle_fingerprints.get(rel, prescan.fingerprints.get(rel))
            held = list(artifact_directory_bundle._identity(info)) == expected if rel in prescan.bundle_fingerprints else _fingerprint(info) == expected
        except OSError:
            held = False
        if not held:
            misses.append(rel)
    for rel in absent:
        try:
            os.lstat(str(directory / rel))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            pass
        misses.append(rel)
    for rel, observation in prescan.marker_observations.items():
        directory_changed, marker_changed = artifact_payload.snapshot_differences(
            directory / rel, observation[1])
        if marker_changed:
            misses.append("@marker-selection")
            break
        if directory_changed:
            misses.append("@bundle-selection")
            break
    if any(not _bundle_inventory_holds(directory, tree) for tree in prescan.bundles):
        misses.extend(tree.directory for tree in prescan.bundles
                      if tree.directory not in misses and not _bundle_inventory_holds(directory, tree))
    return misses


def _prescan_holds(directory: Path, prescan: _Prescan, changes: Mapping[str, Sequence[str]]) -> bool:
    """Under the lock, with `lstat` only: is what the scan saw of the changed files still true?"""
    return not _prescan_misses(directory, prescan, list(changes.get("added", [])) + list(changes.get("modified", [])),
                               absent=changes.get("removed", []))


def _reread_misses(prescan: _Prescan, misses: Sequence[str]) -> None:
    """The first close's second look, for a scan that no longer held: read only the files that moved.

    The refresh lock is held and the admission lock is not.  A file that is gone leaves the
    facts; one that still moves while it is read fails the close with `output-changed-while-reading`."""
    directory = prescan.directory
    read = {rel: (digest, size) for rel, digest, size in prescan.facts}
    gone = set()
    if "@bundle-selection" in misses or "@marker-selection" in misses:
        try:
            prescan.marker_observations.clear()
            prescan.excluded_roots.clear()
            prescan.excluded.clear()
            prescan.bundles = _scan_directory_bundles(
                directory, selected_out=prescan.bundle_selection, marker_observations=prescan.marker_observations,
                excluded_roots=prescan.excluded_roots, excluded=prescan.excluded)
            prescan.bundle_fingerprints = {}
            for tree in prescan.bundles:
                prescan.bundle_fingerprints.update(_bundle_tree_fingerprints(tree))
            prescan.fingerprints = {}
            prescan.paths = []
            paths, prescan.violations = _output_paths(
                directory, excluded=prescan.excluded, excluded_symlinks=prescan.excluded_symlinks,
                marker_observations=prescan.marker_observations, excluded_roots=prescan.excluded_roots,
                skip_directories=[tree.directory for tree in prescan.bundles])
            bundle_dirs = [tree.directory for tree in prescan.bundles]
            journal = _read_json(journal_path(prescan.root, prescan.cycle_id)) or {}
            owned_paths = {row.get("locator") for row in journal.get("directory_bundles", [])
                           if isinstance(row, Mapping) and isinstance(row.get("locator"), str)}
            paths = [(rel, entry) for rel, entry in paths
                     if rel not in owned_paths and not any(rel.startswith(prefix + "/") for prefix in bundle_dirs)]
            prescan.facts = []
            for rel, entry in paths:
                info = os.lstat(entry)
                digest_value, size = _stream_file_facts(entry)
                after = os.lstat(entry)
                if _fingerprint(info) != _fingerprint(after):
                    raise ProducerError("output-changed-while-reading", rel)
                prescan.paths.append(rel)
                prescan.fingerprints[rel] = _fingerprint(info)
                prescan.facts.append((rel, digest_value, size))
            prescan.paths.extend(prescan.bundle_fingerprints)
            return
        except (ProducerError, OSError, artifact_directory_bundle.DirectoryBundleError) as exc:
            raise ProducerError("output-changed-while-reading", str(exc)) from exc
    if any(rel in prescan.bundle_fingerprints for rel in misses):
        old_bundle_paths = set(prescan.bundle_fingerprints)
        prescan.bundles = _scan_directory_bundles(prescan.directory)
        prescan.bundle_fingerprints = {}
        for tree in prescan.bundles:
            prescan.bundle_fingerprints.update(_bundle_tree_fingerprints(tree))
        prescan.paths = [rel for rel in prescan.paths if rel not in old_bundle_paths]
        prescan.paths.extend(prescan.bundle_fingerprints)
    for rel in misses:
        if rel in prescan.bundle_fingerprints:
            continue
        path = directory / rel
        try:
            before = os.lstat(str(path))
            read[rel] = _stream_file_facts(path)
            after = os.lstat(str(path))
        except (FileNotFoundError, NotADirectoryError):
            read.pop(rel, None)
            prescan.fingerprints.pop(rel, None)
            gone.add(rel)
            continue
        if _fingerprint(after) != _fingerprint(before):
            raise ProducerError("output-changed-while-reading", str(path))
        prescan.fingerprints[rel] = _fingerprint(before)
    prescan.paths = [rel for rel in prescan.paths if rel not in gone]
    prescan.facts = [(rel, *read[rel]) for rel in prescan.paths if rel in read]


def _refresh_cycle_locked(root: Path, record: Mapping[str, Any], *, now: Optional[float] = None,
                          allocator: Optional[artifact_identity.IdAllocator] = None,
                          crash_after_manifest: bool = False,
                          prescan: Optional[_Prescan] = None, last_try: bool = True,
                          expected_binding: Optional[Mapping[str, Any]] = None,
                          jobs: Optional[Path] = None,
                          trigger: str = "finalize") -> Dict[str, Any]:
    """Re-finalize a closed cycle: publish its next manifest document if any file changed.

    The admission lock is held and no folder is walked or hashed under it: `prescan` is the
    files as they were read before the lock, and only the changed files' fingerprints, the removed
    files' absence and the manifest digest are looked at again here (§45 D-124).  A scan that no
    longer holds raises `_StaleScan` for the caller to release the lock and read again; with
    `last_try` it publishes no file change this time instead (the next look finds the same one).
    Order, so that a crash anywhere resumes by itself (`_refresh_journal_resume`): the earlier
    document is preserved (a cycle closed before copies existed is first seen here), the journal
    names the new document, the new document is preserved before it is published,
    `manifest.json` is replaced atomically, then the index row is swapped on its earlier digest and
    the record follows.  Nothing changed means nothing is written."""
    cycle_id = record["cycle_id"]
    manifest_path = _record_cycle_manifest_path(root, record)
    directory = manifest_path.parent
    try:
        raw = manifest_path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProducerError("already-sealed-mismatch", str(manifest_path)) from exc
    digest = artifact_manifest.manifest_digest(document)
    # The index row is read once, here, and answers for the whole refresh: a manifest the
    # index does not carry is not something to publish a next document over. The small
    # head projection answers exactly this row when the generation is current; only an
    # actual publish below needs the full mutable document.
    head = artifact_admission.load_index_head(root)
    index = None
    if head is not None:
        row = head.manifests.get(cycle_id)
    else:
        index = artifact_admission.load_index(root)
        row = index.manifests.get(cycle_id)
    if not isinstance(row, dict) or row.get("manifest_digest") != digest:
        previous = artifact_lifecycle.find_manifest_snapshot(
            root, cycle_id, manifest_digest=record.get("manifest_digest"))
        if previous is None or not isinstance(row, dict) or row.get("manifest_digest") != record.get("manifest_digest"):
            return {"status": "already-sealed", "cycle_id": cycle_id, "refreshed": False,
                    "manifest_digest": record.get("manifest_digest"), "changes": {"added": [], "modified": [], "removed": [], "reclassified": []}}
        # The current file is ordinary editable material. Continue observing
        # payloads against the recorded revision without changing past proof.
        document = previous
        raw = artifact_manifest.canonical_bytes(document)
        digest = record["manifest_digest"]
    alloc = allocator or artifact_identity.IdAllocator()
    deferred = False
    refreshed: Optional[Dict[str, Any]] = None
    if prescan is not None:
        excluded, excluded_symlinks = list(prescan.excluded), list(prescan.excluded_symlinks)
        facts = prescan.facts
        refreshed, changes = _refreshed_document(
            root, record, document, facts, allocator=alloc, now=now,
            excluded_roots=prescan.excluded_roots)
        deferred = refreshed is not None and not _prescan_holds(directory, prescan, changes)
    else:
        deferred = True
    if deferred:
        if not last_try:
            raise _StaleScan(cycle_id)
        # The document as it is: no file change is published this time, a terminal record still can be.
        excluded, excluded_symlinks, refreshed = [], [], None
        facts = [(r["locator"]["path"], r["content_digest"], r["byte_size"])
                 for r in document.get("artifact_revisions", []) or [] if isinstance(r, dict)]
        changes = {"added": [], "modified": [], "removed": [], "reclassified": []}
    revision_id = refreshed["manifest_revision_id"] if refreshed is not None else alloc.allocate("manifest_revision")
    provisional = _provisional_completion_projection(root, record, refreshed or document, facts,
                                                     revision_id=revision_id, allocator=alloc, now=now,
                                                     expected_binding=expected_binding,
                                                     previous_document=document, jobs=jobs)
    if provisional is not None:
        refreshed = provisional
        additions = None
    else:
        additions = _leaf_terminal_additions(root, record, document, facts, allocator=alloc,
                                             revision_id=revision_id, now=now)
    if additions is not None:
        base = json.loads(json.dumps(refreshed if refreshed is not None
                                     else dict(document, manifest_revision_id=revision_id)))
        base["routes"] = list(base.get("routes", [])) + [additions["row"]]
        base["events"] = list(base.get("events", [])) + [additions["event"]]
        try:
            refreshed = artifact_lifecycle._derive_terminal_evidence(base, additions["binding"], additions["route"])
        except artifact_lifecycle.LifecycleError:
            additions = None
    result = {"status": "already-sealed", "cycle_id": cycle_id, "refreshed": refreshed is not None,
              "changes": changes, "excluded_hidden": excluded, "excluded_symlinks": excluded_symlinks,
              "manifest_digest": digest}
    if deferred:
        result["deferred"] = "files-changed-during-read"
    if additions is not None:
        result["terminal_added"] = additions["row"]["route_id"]
    if refreshed is None:
        carried = list(record.get("history_pending") or [])
        if carried and _history_deliver_locked(root, carried):
            _write_history_pending(root, record, [])
        return result
    artifact_lifecycle.preserve_manifest_snapshot(root, cycle_id, raw)
    earlier = _earlier_documents(root, cycle_id, refreshed)
    report = artifact_manifest.validate_update(refreshed, preserved=earlier, previous=document,
        retired_artifact_ids=_reclassified_artifact_ids(document, changes))
    if not report.ok:
        raise ProducerError("manifest-invalid", ";".join(v.code for v in report.violations))
    new_digest = artifact_manifest.manifest_digest(refreshed)
    new_raw = artifact_manifest.canonical_bytes(refreshed)
    identity = artifact_lifecycle.read_root_identity(root)
    if index is None:
        index = artifact_admission.load_index(root)
    index_report = artifact_index.check(
        index, refreshed, idempotency_key=cycle_id, manifest_digest=new_digest,
        repository_id=identity.repository_id if identity else None,
        replaces_manifest_digest=digest)
    if not index_report.ok:
        raise ProducerError("index-rejected", ";".join(v.code for v in index_report.violations))
    cycle_path = os.path.relpath(str(directory), str(root))
    # §45 D-125: the file lines go to the recorder before the new document is published; a
    # recorder that is not there (or fails) leaves them in the record, so the explicit command
    # still publishes current bytes and the next trigger hands the lines over.
    lines = _refresh_history_lines(root, record, directory, document, refreshed, changes, trigger=trigger,
                                   excluded_roots=prescan.excluded_roots if prescan else (),
                                   now=time.time() if now is None else float(now))
    pending = _merge_pending(record.get("history_pending") or [], lines)
    if _history_deliver_locked(root, pending):
        pending = []
    _write_journal(root, cycle_id, state="refreshing", manifest_digest=new_digest,
                   previous_manifest_digest=digest, cycle_path=cycle_path,
                   manifest_revision_id=refreshed["manifest_revision_id"], history_pending=pending)
    artifact_lifecycle.preserve_manifest_snapshot(root, cycle_id, new_raw)
    # COMMIT POINT: atomic replacement of the current document.
    _write_atomic(manifest_path, new_raw)
    if crash_after_manifest:  # test hook: simulate a crash after the commit point
        raise artifact_admission.AdmissionRecoveryRequired("simulated crash after manifest refresh")
    try:
        _commit_sealed(root, _with_pending(record, pending), refreshed, new_digest, now=now, index=index,
                       previous_digest=digest, excluded_roots=prescan.excluded_roots if prescan else ())
    except BaseException as exc:
        raise artifact_admission.AdmissionRecoveryRequired(
            f"cycle {cycle_id} manifest refreshed but post-publish update failed; run recover") from exc
    result.update(manifest_digest=new_digest, manifest_path=str(manifest_path))
    return result


# ---------------------------------------------------------------------------
# refresh without a long lock: bounded scan, cursor, history (§45 D-124, D-125)
# ---------------------------------------------------------------------------
#
# A closed cycle is looked at in three steps and only the last one takes a lock:
#
#   1. walk, lstat and hash the files with no lock at all, inside one budget
#      (`RefreshBudget`), reusing the digest of a file whose (size, mtime_ns,
#      inode) is what the last scan saw;
#   2. under the cycle's own *refresh* lock (a file under `checkpoints/refresh/`
#      that nothing but a refresh takes, so the open-cycle order "admission, then
#      checkpoint" of `finalize` is never crossed) re-check the changed files and
#      the manifest digest with `lstat` only;
#   3. two short admission sections: the history lines, then the manifest, index
#      row and record.  A recorder that is not there leaves the lines in the
#      record's `history_pending`; a recorder that fails leaves the manifest as
#      it was, and the next trigger finds the same change again.
#
# Nothing here raises to the command that triggered it: a lock that is busy, a
# file that moved while it was read or a recorder that failed is a `skipped`.

REFRESH_DIR = "refresh"
REFRESH_MAX_WALK_ENTRIES = 20000
REFRESH_MAX_HASH_BYTES = 256 * 1024 * 1024
REFRESH_MAX_SECONDS = 60.0
REFRESH_ADMISSION_WAIT_SECONDS = 5.0
REFRESH_ABSENCE_CHECK_FLOOR = 1000
_UNLIMITED = float("inf")


class RefreshBudget:
    """One refresh run's share of walking, hashing and time.

    The budget is looked at between files, never inside one, and a run always
    finishes the first file it starts hashing, so a file larger than the whole
    byte budget is still reflected by a single run.  `unlimited()` is for the
    caller that asked for the answer now (an explicit `finalize` or checkpoint)."""

    def __init__(self, max_walk_entries: float = REFRESH_MAX_WALK_ENTRIES,
                 max_hash_bytes: float = REFRESH_MAX_HASH_BYTES,
                 max_seconds: float = REFRESH_MAX_SECONDS) -> None:
        self.max_walk_entries = max_walk_entries
        self.max_hash_bytes = max_hash_bytes
        self.max_seconds = max_seconds
        self.walked = 0
        self.hashed_bytes = 0
        self.hashed_files = 0
        self._started = time.monotonic()

    @classmethod
    def unlimited(cls) -> "RefreshBudget":
        return cls(_UNLIMITED, _UNLIMITED, _UNLIMITED)

    def walk_exhausted(self) -> bool:
        return self.walked >= self.max_walk_entries or time.monotonic() - self._started >= self.max_seconds

    def hash_exhausted(self) -> bool:
        return self.hashed_bytes >= self.max_hash_bytes or time.monotonic() - self._started >= self.max_seconds

    def exhausted(self) -> bool:
        return self.walk_exhausted() or self.hash_exhausted()

    def deadline(self) -> Optional[float]:
        """The `time.monotonic()` value its time share ends at, `None` when it has none."""
        return None if self.max_seconds == _UNLIMITED else self._started + self.max_seconds


def _fingerprint(st: os.stat_result) -> List[int]:
    return [st.st_size, st.st_mtime_ns, st.st_ino]


def _refresh_state_dir(root: Path) -> Path:
    return producer_dir(root) / CHECKPOINT_DIR / REFRESH_DIR


def _refresh_state_path(root: Path, cycle_id: str) -> Path:
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        raise ProducerError("cycle-id-invalid", str(cycle_id))
    return _refresh_state_dir(root) / f"{cycle_id}.json"


def _refresh_cursor_path(root: Path) -> Path:
    return _refresh_state_dir(root) / "cursor.json"


@contextlib.contextmanager
def _refresh_lock(root: Path, cycle_id: str, *, timeout: float):
    """The cycle's refresh lock.  Only a refresh and the first close of an open cycle take it, and
    both take it before the admission lock, so it can be held while that lock is waited for."""
    path = _refresh_state_path(root, cycle_id).with_suffix(".lock")
    _ensure_dir(path.parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ProducerError("refresh-lock-busy", cycle_id)
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _read_refresh_state(root: Path, cycle_id: str) -> Dict[str, Any]:
    """The last scan's bookkeeping: `cursor`, `observed_at` and `files`
    (path -> [size, mtime_ns, inode, digest]).  It has no authority: a missing or
    odd file just means the next run starts over."""
    try:
        raw = _read_json(_refresh_state_path(root, cycle_id))
    except ProducerError:
        return {}
    if not isinstance(raw, dict):
        return {}
    files = raw.get("files")
    kept: Dict[str, List[Any]] = {}
    if isinstance(files, dict):
        for rel, row in files.items():
            if (isinstance(rel, str) and isinstance(row, list) and len(row) == 4
                    and all(isinstance(v, int) and not isinstance(v, bool) for v in row[:3])
                    and isinstance(row[3], str)):
                kept[rel] = row
    cursor = raw.get("cursor")
    observed = raw.get("observed_at")
    return {"cursor": cursor if isinstance(cursor, str) and cursor else None, "files": kept,
            "observed_at": observed if isinstance(observed, (int, float)) and not isinstance(observed, bool) else None}


def _write_refresh_state(root: Path, cycle_id: str, *, cursor: Optional[str], files: Mapping[str, Sequence[Any]],
                         observed_at: Optional[float]) -> None:
    path = _refresh_state_path(root, cycle_id)
    _ensure_dir(path.parent)
    _write_atomic(path, _json_bytes({"schema_version": 1, "cycle_id": cycle_id, "cursor": cursor,
                                     "observed_at": observed_at, "files": dict(sorted(files.items()))}), 0o644)


@dataclass
class _RefreshScan:
    facts: Dict[str, Tuple[str, int, List[int]]] = field(default_factory=dict)
    visited: Set[str] = field(default_factory=set)
    excluded: List[str] = field(default_factory=list)
    excluded_symlinks: List[str] = field(default_factory=list)
    excluded_roots: List[str] = field(default_factory=list)
    marker_observations: Dict[str, Any] = field(default_factory=dict)
    complete: bool = True
    cursor: Optional[str] = None
    from_top: bool = True


def _pruned_directory(parts: Sequence[str]) -> bool:
    """A directory the inclusion rule leaves out whole (hidden component or `__pycache__`)."""
    return (any(part.startswith(".") or part == "__pycache__" for part in parts)
            or _browser_profile(parts, directory=True))


def _walk_sorted(top: str, prefix: Tuple[str, ...], after: Optional[Tuple[str, ...]],
                 bundle_directories: Sequence[Tuple[str, ...]] = (), marker_callback=None):
    """Entries under `top` in name order, depth first, as `(parts, DirEntry, is_dir)`.

    `after` is the last entry an earlier run finished: entries up to it are not
    yielded as cursor positions. Marker directories are still observed so excluded
    roots remain available to preserve prior rows. A later directory the rule leaves
    out is yielded once and not entered."""
    try:
        entries = sorted(os.scandir(top), key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        parts = prefix + (entry.name,)
        is_dir = entry.is_dir(follow_symlinks=False)
        inside_bundle = any(parts[:len(bundle)] == bundle for bundle in bundle_directories)
        marker_excluded = False
        if is_dir and not inside_bundle and not _pruned_directory(parts) and marker_callback is not None:
            marker_excluded = bool(marker_callback(entry.path, parts))
        enter = is_dir and not _pruned_directory(parts) and not inside_bundle and not marker_excluded
        if after is not None:
            head = after[:len(parts)]
            if parts < head:
                continue
            if parts == after:
                if enter:
                    yield from _walk_sorted(entry.path, parts, None, bundle_directories, marker_callback)
                continue
            if parts == head:
                if enter:
                    yield from _walk_sorted(entry.path, parts, after, bundle_directories, marker_callback)
                continue
        if marker_excluded:
            # The observation above is retained even when cursor ordering skips this
            # entry or the cursor lies inside it; excluded payload is never entered.
            yield parts, entry, is_dir
            continue
        yield parts, entry, is_dir
        if enter:
            yield from _walk_sorted(entry.path, parts, None, bundle_directories, marker_callback)


def _bounded_scan(directory: Path, *, cursor: Optional[str], known: Mapping[str, Sequence[Any]],
                  budget: RefreshBudget) -> _RefreshScan:
    """Walk both exact cycle payload roots without a lock, within one shared budget.

    Resume positions and file facts use full cycle-relative locators. Older
    prefixless cursors are interpreted as artifacts-relative; invalid cursors
    restart this observation from the top so they cannot skip support files.
    §47 bundle pruning remains limited to artifacts raw-results.
    """
    artifacts = directory / "artifacts"
    if not artifacts.is_dir() or artifacts.is_symlink():
        raise ProducerError("artifacts-dir-missing", str(artifacts))
    roots = [("artifacts", artifacts)]
    internal = directory / "_internal"
    if internal.is_dir() and not internal.is_symlink():
        roots.append(("_internal", internal))

    after: Optional[Tuple[str, ...]] = None
    if cursor:
        candidate = cursor
        if candidate.split("/", 1)[0] not in {"artifacts", "_internal"}:
            candidate = "artifacts/" + candidate
        if artifact_manifest.validate_locator_path(candidate).ok:
            parts = tuple(candidate.split("/"))
            if parts[0] in {"artifacts", "_internal"}:
                after = parts
    normalized_cursor = "/".join(after) if after else None
    scan = _RefreshScan(from_top=after is None)
    roots_to_walk = []
    for root_name, top in roots:
        observation = artifact_payload.observe_directory(top, payload_root=top)
        scan.marker_observations[root_name] = observation
        if observation[0]:
            scan.excluded_roots.append(root_name)
            scan.excluded.append(root_name + "/")
        else:
            roots_to_walk.append((root_name, top))
    roots = roots_to_walk
    last_parts: Optional[Tuple[str, ...]] = None
    processed = 0
    document = _read_json(directory / "manifest.json")
    bundle_directories = _manifest_bundle_directories(document) if isinstance(document, Mapping) else set()
    bundle_parts = {tuple(path.split("/")) for path in bundle_directories}
    cursor_root = after[0] if after else None
    root_order = {name: index for index, (name, _top) in enumerate(
        (("artifacts", artifacts), ("_internal", internal)))}
    cursor_index = root_order.get(cursor_root, -1) if cursor_root else -1

    def inspect_marker(path: str, parts: Tuple[str, ...]) -> bool:
        rel = "/".join(parts)
        root_name = parts[0]
        observation = artifact_payload.observe_directory(Path(path), payload_root=directory / root_name)
        scan.marker_observations[rel] = observation
        if observation[0]:
            scan.excluded_roots.append(rel)
        return observation[0]

    for root_name, top in roots:
        if cursor_index >= 0 and root_order[root_name] < cursor_index:
            continue
        root_after = after if cursor_root == root_name else None
        for parts, entry, is_dir in _walk_sorted(
                str(top), (root_name,), root_after, bundle_parts, marker_callback=inspect_marker):
            if processed and budget.walk_exhausted():
                scan.complete = False
                scan.cursor = "/".join(last_parts) if last_parts else normalized_cursor
                return scan
            budget.walked += 1
            processed += 1
            rel = "/".join(parts)
            if parts in bundle_parts:
                last_parts = parts
                continue
            if entry.is_symlink():
                scan.excluded_symlinks.append(rel)
                last_parts = parts
                continue
            if is_dir:
                if _pruned_directory(parts) or rel in scan.excluded_roots:
                    scan.excluded.append(rel + "/")
                last_parts = parts
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                scan.visited.add(rel)
                last_parts = parts
                continue
            if not stat.S_ISREG(st.st_mode) or _outside_inclusion_rule(rel) \
                    or not artifact_manifest.validate_locator_path(rel).ok:
                scan.excluded.append(rel)
                last_parts = parts
                continue
            scan.visited.add(rel)
            fingerprint = _fingerprint(st)
            remembered = known.get(rel)
            if remembered is not None and list(remembered[:3]) == fingerprint:
                scan.facts[rel] = (remembered[3], st.st_size, fingerprint)
                last_parts = parts
                continue
            if budget.hashed_files and budget.hash_exhausted():
                scan.visited.discard(rel)
                scan.complete = False
                scan.cursor = "/".join(last_parts) if last_parts else normalized_cursor
                return scan
            try:
                digest, size = _stream_file_facts(Path(entry.path))
                after_stat = os.lstat(entry.path)
            except (ProducerError, OSError):
                last_parts = parts
                continue  # moved while it was read, or not a plain file now: the next run looks again
            budget.hashed_bytes += size
            budget.hashed_files += 1
            if _fingerprint(after_stat) != fingerprint:
                last_parts = parts
                continue
            scan.facts[rel] = (digest, size, fingerprint)
            last_parts = parts
    return scan


def _confirmed_absent(directory: Path, document: Mapping[str, Any], scan: _RefreshScan,
                      budget: RefreshBudget) -> List[str]:
    """Declared files this run did not see and that are really gone (`lstat`).

    A scan that stopped early has not seen the rest of the tree, so a file it did not
    visit is never read as deleted: only an `lstat` that finds nothing is."""
    gone: List[str] = []
    remaining = (_UNLIMITED if budget.max_walk_entries == _UNLIMITED
                 else max(0, int(budget.max_walk_entries) - budget.walked))
    allowance = remaining + REFRESH_ABSENCE_CHECK_FLOOR
    bundle_directories = _manifest_bundle_directories(document)
    for row in document.get("artifact_revisions", []) or []:
        rel = row.get("locator", {}).get("path") if isinstance(row, dict) else None
        if (not isinstance(rel, str) or rel in scan.visited or _outside_inclusion_rule(rel)
                or any(rel.startswith(path + "/") for path in bundle_directories)):
            continue
        parts = rel.split("/")
        marker_excluded = False
        if len(parts) > 1 and parts[0] in {"artifacts", "_internal"}:
            for depth in range(1, len(parts)):
                parent = "/".join(parts[:depth])
                if parent not in scan.marker_observations:
                    if allowance <= 0:
                        return gone
                    allowance -= 1
                    budget.walked += 1
                    observation = artifact_payload.observe_directory(
                        directory / parent, payload_root=directory / parts[0])
                    scan.marker_observations[parent] = observation
                    if observation[0]:
                        scan.excluded_roots.append(parent)
                observation = scan.marker_observations[parent]
                if observation[0]:
                    marker_excluded = True
                    scan.excluded.append(parent + "/")
                    break
        if marker_excluded or _excluded_by_roots(rel, scan.excluded_roots):
            continue
        if allowance <= 0:
            break
        allowance -= 1
        budget.walked += 1
        try:
            os.lstat(str(directory / rel))
        except (FileNotFoundError, NotADirectoryError):
            gone.append(rel)
        except OSError:
            continue
    return gone


def _recheck_candidate(directory: Path, manifest_path: Path, raw: bytes, scan: _RefreshScan,
                       changes: Mapping[str, Sequence[str]]) -> bool:
    """Under the refresh lock, with `lstat` only: is what the scan saw still true?"""
    try:
        if manifest_path.read_bytes() != raw:
            return False
        for rel, observation in scan.marker_observations.items():
            if not artifact_payload.snapshot_matches(directory / rel, observation[1]):
                return False
        for rel in list(changes.get("added", [])) + list(changes.get("modified", [])):
            if _fingerprint(os.lstat(str(directory / rel))) != scan.facts[rel][2]:
                return False
        for rel in changes.get("removed", []):
            try:
                os.lstat(str(directory / rel))
            except (FileNotFoundError, NotADirectoryError):
                continue
            return False
    except OSError:
        return False
    return True


def _remember_scan(root: Path, cycle_id: str, scan: _RefreshScan, state: Mapping[str, Any], *,
                   published: bool, observe: bool, clock: float) -> None:
    """Keep the scan's fingerprints and cursor where the next run finds them.

    Written when a document was published, when the run stopped early, and when a sweep
    looks at a cycle for the first time; never for a cycle a trigger named that had
    nothing to say (that run touches nothing)."""
    previous = dict(state.get("files", {}))
    files = {rel: [*fp, digest] for rel, (digest, _size, fp) in scan.facts.items()}
    if not (scan.complete and scan.from_top):
        files = {**previous, **files}
    if not (published or not scan.complete or (observe and files != previous)
            or (scan.complete and state.get("cursor") is not None)):
        return
    try:
        _write_refresh_state(root, cycle_id, cursor=scan.cursor, files=files, observed_at=clock)
    except (OSError, ProducerError):
        pass


def refresh_cycle(
    root: Path, cycle_id: str, *, trigger: str = "explicit", now: Optional[float] = None,
    budget: Optional[RefreshBudget] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    observe: bool = False, crash_after_manifest: bool = False,
    jobs: Optional[Path] = None,
) -> Dict[str, Any]:
    """Observe one closed cycle (§45 D-124): publish its next manifest document if a file changed.

    Every outcome is a result, none is an error for the caller: `emitted` (a new
    document is current), `unchanged`, or `skipped` with a `reason` (busy, the cycle moved
    while it was scanned, the recorder was unavailable, ...) -- a skipped run changed
    nothing and the next trigger finds the same work again.  A run that changes nothing
    writes nothing.  `explicit` ignores the interval and the budget."""
    root = Path(root).resolve()
    _observe_control_changes(root, cycle_id, now=now)
    clock = time.time() if now is None else float(now)
    base = {"cycle_id": cycle_id, "trigger": trigger}
    if budget is None:
        budget = RefreshBudget.unlimited() if trigger == "explicit" else RefreshBudget()

    def skipped(reason: str, **extra: Any) -> Dict[str, Any]:
        return {"status": "skipped", "reason": reason, **base, **extra}

    if closed_cycle_refresh_off():
        return skipped("refresh-off")
    try:
        return _refresh_cycle_observed(root, cycle_id, trigger=trigger, clock=clock, budget=budget,
                                       allocator=allocator, observe=observe,
                                       crash_after_manifest=crash_after_manifest, base=base, skipped=skipped,
                                       jobs=jobs)
    except (artifact_admission.AdmissionBusy, dispatch_lock_order.LockOrderError) as exc:
        return skipped("busy", detail=str(exc))
    except ProducerError as exc:
        return skipped("busy" if exc.code == "refresh-lock-busy" else exc.code, detail=exc.detail)
    except (artifact_lifecycle.LifecycleError, artifact_locator.LocatorError, OSError, ValueError) as exc:
        return skipped(type(exc).__name__, detail=str(exc))


def _refresh_cycle_observed(root: Path, cycle_id: str, *, trigger: str, clock: float, budget: RefreshBudget,
                            allocator: Optional[artifact_identity.IdAllocator], observe: bool,
                            crash_after_manifest: bool, base: Mapping[str, Any], skipped,
                            jobs: Optional[Path] = None) -> Dict[str, Any]:
    record = read_cycle_record(root, cycle_id)
    if record is None:
        return skipped("cycle-unknown")
    if record.get("journal_migration"):
        return skipped("consolidated-to-journal")
    if record.get("deleted_at"):
        return skipped("cycle-deleted")
    if record.get("state") != "sealed":
        return skipped("cycle-not-closed", cycle_state=record.get("state"))
    manifest_path = _record_cycle_manifest_path(root, record)
    if not _path_entry_present(manifest_path):
        return skipped("manifest-absent")
    if journal_path(root, cycle_id).exists():
        return skipped("journal-pending")  # the next locked command finishes it
    raw = manifest_path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return skipped("manifest-unreadable")
    digest = artifact_manifest.manifest_digest(document)
    if record.get("manifest_digest") != digest:
        return {"status": "unchanged", **base, "manifest_digest": record.get("manifest_digest")}
    state = _read_refresh_state(root, cycle_id)
    observed_at = state.get("observed_at")
    if (trigger != "explicit" and observed_at is not None and state.get("cursor") is None
            and clock - observed_at < checkpoint_interval_seconds()):
        return skipped("min-interval", next_eligible_at=_rfc3339(observed_at + checkpoint_interval_seconds()))
    directory = manifest_path.parent
    scan = _bounded_scan(directory, cursor=None if trigger == "explicit" else state.get("cursor"),
                         known=state.get("files", {}), budget=budget)
    gone = _confirmed_absent(directory, document, scan, budget)
    alloc = allocator or artifact_identity.IdAllocator()
    facts = [(rel, digest_, size) for rel, (digest_, size, _fp) in sorted(scan.facts.items())]
    refreshed, changes = _refreshed_document(
        root, record, document, facts, allocator=alloc, now=clock,
        removable=gone, excluded_roots=scan.excluded_roots)
    if scan.complete:
        revision_id = refreshed["manifest_revision_id"] if refreshed is not None else alloc.allocate("manifest_revision")
        provisional = _provisional_completion_projection(root, record, refreshed or document, facts,
                                                         revision_id=revision_id, allocator=alloc, now=clock,
                                                         jobs=jobs)
        if provisional is not None:
            refreshed = provisional
    result = {"status": "unchanged", **base, "changes": changes, "complete": scan.complete,
              "cursor": scan.cursor, "walked": budget.walked, "hashed_bytes": budget.hashed_bytes,
              "excluded_hidden": scan.excluded, "excluded_symlinks": scan.excluded_symlinks,
              "manifest_digest": digest}
    if refreshed is None:
        if record.get("history_pending"):
            result["history"] = _deliver_pending(root, cycle_id, timeout=REFRESH_ADMISSION_WAIT_SECONDS)
        _remember_scan(root, cycle_id, scan, state, published=False, observe=observe, clock=clock)
        return result
    new_digest = artifact_manifest.manifest_digest(refreshed)
    earlier = {old.get("manifest_revision_id"): old for old in _earlier_documents(root, cycle_id, refreshed)}
    earlier[document.get("manifest_revision_id")] = document
    report = artifact_manifest.validate_update(refreshed, preserved=list(earlier.values()), previous=document,
        retired_artifact_ids=_reclassified_artifact_ids(document, changes))
    if not report.ok:
        return skipped("manifest-invalid", detail=";".join(v.code for v in report.violations))
    lines = _refresh_history_lines(root, record, directory, document, refreshed, changes, trigger=trigger,
                                   excluded_roots=scan.excluded_roots, now=clock)
    with _refresh_lock(root, cycle_id, timeout=0.0):
        outcome = _publish_refresh(
            root, cycle_id, manifest_path, raw, digest, refreshed, new_digest, scan, changes, lines,
            now=clock, crash_after_manifest=crash_after_manifest, jobs=jobs)
    if outcome is not None:
        return skipped(outcome)
    _remember_scan(root, cycle_id, scan, state, published=True, observe=observe, clock=clock)
    result.update(status="emitted", manifest_digest=new_digest, manifest_path=str(manifest_path),
                  history="pending" if not _history_published(root, cycle_id) else "published")
    return result


def _history_published(root: Path, cycle_id: str) -> bool:
    record = read_cycle_record(root, cycle_id) or {}
    return not record.get("history_pending")


def _publish_refresh(root: Path, cycle_id: str, manifest_path: Path, raw: bytes, digest: str,
                     refreshed: Mapping[str, Any], new_digest: str, scan: _RefreshScan,
                     changes: Mapping[str, Sequence[str]], lines: Sequence[Mapping[str, Any]], *,
                     now: float, crash_after_manifest: bool,
                     expected_binding: Optional[Mapping[str, Any]] = None,
                     jobs: Optional[Path] = None) -> Optional[str]:
    """Publish a prepared next document; `None` on success, else why this run skipped.

    The refresh lock is held and the admission lock is not.  The files are re-checked with
    `lstat`, then one admission section does the rest in the D-125 order: the record, the
    current document, a provisional completion's live conditions and the index row are read again, the history lines go to the recorder, the
    document is replaced, the index row is swapped and the record follows.  Anything that no
    longer holds writes nothing, history included, so a refresh that was not published leaves no
    line behind.  A recorder that is not there leaves the lines in the record; one that fails
    leaves the manifest as it was."""
    directory = manifest_path.parent
    if not _recheck_candidate(directory, manifest_path, raw, scan, changes):
        return "superseded"
    fd = artifact_admission._acquire_lock(root, REFRESH_ADMISSION_WAIT_SECONDS)
    try:
        record = read_cycle_record(root, cycle_id)
        if (record is None or record.get("state") != "sealed" or record.get("manifest_digest") != digest
                or _record_cycle_manifest_path(root, record) != manifest_path):
            return "superseded"
        if not _recheck_candidate(directory, manifest_path, raw, scan, changes):
            return "superseded"
        # An active -> completed candidate was decided from observations taken before this
        # lock; a lease, route or outcome that moved since keeps the document as it is.
        previous_state = (json.loads(raw.decode("utf-8")).get("cycle") or {}).get("state")
        if ((refreshed.get("cycle") or {}).get("state") == "completed"
                and previous_state in {"active", "abandoned"}
                and not _provisional_completion_holds(root, record, refreshed, expected_binding, jobs=jobs)):
            return "superseded"
        index = artifact_admission.load_index(root)  # the one read of this locked section
        row = index.manifests.get(cycle_id)
        if not isinstance(row, dict) or row.get("manifest_digest") != digest:
            return "index-mismatch"
        identity = artifact_lifecycle.read_root_identity(root)
        index_report = artifact_index.check(
            index, refreshed, idempotency_key=cycle_id, manifest_digest=new_digest,
            repository_id=identity.repository_id if identity else None, replaces_manifest_digest=digest)
        if not index_report.ok:
            return "index-rejected"
        new_raw = artifact_manifest.canonical_bytes(refreshed)
        cycle_path = os.path.relpath(str(directory), str(root))
        pending = _merge_pending(record.get("history_pending") or [], lines)
        artifact_lifecycle.preserve_manifest_snapshot(root, cycle_id, raw)
        if _history_module() is not None:
            if not _history_deliver_locked(root, pending):
                return "history-unavailable"  # history first, manifest second: the same change is found again
            pending = []
        _write_journal(root, cycle_id, state="refreshing", manifest_digest=new_digest,
                       previous_manifest_digest=digest, cycle_path=cycle_path,
                       manifest_revision_id=refreshed["manifest_revision_id"], history_pending=pending)
        artifact_lifecycle.preserve_manifest_snapshot(root, cycle_id, new_raw)
        # COMMIT POINT: atomic replacement of the current document.
        _write_atomic(manifest_path, new_raw)
        if crash_after_manifest:  # test hook: simulate a crash after the commit point
            raise artifact_admission.AdmissionRecoveryRequired("simulated crash after manifest refresh")
        try:
            _commit_sealed(root, _with_pending(record, pending), refreshed, new_digest, now=now,
                           index=index, previous_digest=digest, excluded_roots=scan.excluded_roots)
        except BaseException as exc:
            raise artifact_admission.AdmissionRecoveryRequired(
                f"cycle {cycle_id} manifest refreshed but post-publish update failed; run recover") from exc
    finally:
        artifact_admission._release_lock(root, fd)
    return None


def _cycle_ids_with_records(root: Path) -> List[str]:
    directory = producer_dir(root) / "cycles"
    if not directory.is_dir():
        return []
    return sorted(entry.stem for entry in directory.iterdir()
                  if entry.suffix == ".json" and artifact_identity.is_well_formed(entry.stem, "cycle"))


def refresh_sweep(
    root: Path, *, trigger: str = "turn-end", now: Optional[float] = None,
    budget: Optional[RefreshBudget] = None, first_cycle_id: Optional[str] = None,
    skip_cycle_id: Optional[str] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
) -> Dict[str, Any]:
    """Look at the root's closed cycles in turn, with what is left of one budget (§45 D-124).

    `first_cycle_id` is the cycle a trigger names, looked at before the rest whatever its
    state.  The rest follow a cursor that moves around all closed cycles in ID order: no
    date or count leaves a cycle out, and an old one is reached on a later round.  The cursor
    is bookkeeping under `checkpoints/refresh/`, with no authority; a run that could not finish
    a cycle leaves it before that cycle so the next run resumes there."""
    root = Path(root).resolve()
    clock = time.time() if now is None else float(now)
    budget = budget or (RefreshBudget.unlimited() if trigger == "explicit" else RefreshBudget())
    out: Dict[str, Any] = {"status": "swept", "trigger": trigger, "visited": 0, "refreshed": [], "skipped": [],
                           "cursor": None}
    if closed_cycle_refresh_off():
        out["status"] = "off"
        return out

    def look(cycle_id: str, *, observe: bool) -> Dict[str, Any]:
        try:
            result = refresh_cycle(root, cycle_id, trigger=trigger, now=clock, budget=budget,
                                   allocator=allocator, observe=observe)
        except Exception as exc:  # noqa: BLE001 -- one cycle's trouble never stops the sweep
            return {"status": "skipped", "reason": type(exc).__name__}
        if result["status"] == "emitted":
            out["refreshed"].append(cycle_id)
        elif result["status"] == "skipped" and result.get("reason") != "cycle-not-closed":
            out["skipped"].append({"cycle_id": cycle_id, "reason": result.get("reason")})
        return result

    if first_cycle_id:
        look(first_cycle_id, observe=False)
        out["visited"] += 1
    cursor_path = _refresh_cursor_path(root)
    cursor = _read_json(cursor_path) or {}
    last_sweep = cursor.get("last_sweep_at")
    if (trigger != "explicit" and isinstance(last_sweep, (int, float)) and not isinstance(last_sweep, bool)
            and clock - last_sweep < checkpoint_interval_seconds()):
        out["rotation"] = "min-interval"
        return out
    ids = _cycle_ids_with_records(root)
    if not ids:
        return out
    after = cursor.get("after") if isinstance(cursor.get("after"), str) else None
    start = 0
    if after is not None:
        start = next((i for i, cid in enumerate(ids) if cid > after), 0)
    last_done, looked = after, 0
    for cycle_id in ids[start:] + ids[:start]:
        if looked and budget.exhausted():
            break
        if cycle_id in (first_cycle_id, skip_cycle_id):
            last_done = cycle_id
            continue
        record = read_cycle_record(root, cycle_id)
        if record is None or record.get("state") != "sealed":
            last_done = cycle_id
            continue
        result = look(cycle_id, observe=True)
        looked += 1
        out["visited"] += 1
        if result["status"] != "skipped" and not result.get("complete", True):
            break  # unfinished: the next run resumes inside this cycle
        last_done = cycle_id
    out["cursor"] = last_done
    if looked:
        try:
            _ensure_dir(cursor_path.parent)
            _write_atomic(cursor_path, _json_bytes({"schema_version": 1, "after": last_done,
                                                    "last_sweep_at": clock}), 0o644)
        except (OSError, ProducerError):
            pass
    return out


def _commit_sealed(
    root: Path, record: Mapping[str, Any], document: Mapping[str, Any], digest: str, *, now: Optional[float],
    index: Optional[artifact_index.IndexDocument] = None, previous_digest: Optional[str] = None,
    excluded_count: Optional[int] = None, excluded_roots: Sequence[str] = (),
) -> None:
    """Apply a published manifest to the index and the cycle record.

    The first close admits the cycle to the index.  A later document of an
    already closed cycle (`previous_digest` names the one it replaces) swaps the
    cycle's row instead, on that digest (§45 D-124), keeps `sealed_on`, and does
    not announce a close again.  `index`, when given, is the one read the caller
    already made under the admission lock."""
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    if index is None:
        index = artifact_admission.load_index(root)
    row = index.manifests.get(record["cycle_id"])
    already_closed = record.get("state") == "sealed"
    if row is None:
        index = artifact_index.apply(
            index, document, cycle_path=os.path.relpath(str(directory), str(root)),
            manifest_digest=digest, idempotency_key=record["cycle_id"],
        )
        artifact_admission._write_index(root, index)
    elif row.get("manifest_digest") != digest:
        if previous_digest is None or row.get("manifest_digest") != previous_digest:
            raise ProducerError("already-sealed-mismatch", "index")
        index = artifact_index.apply(
            index, document, cycle_path=os.path.relpath(str(directory), str(root)),
            manifest_digest=digest, idempotency_key=record["cycle_id"],
        )
        artifact_admission._write_index(root, index)
    sealed = dict(record)
    sealed["state"] = "sealed"
    if not (already_closed and record.get("sealed_on")):
        sealed["sealed_on"] = _rfc3339(now)
    sealed["manifest_digest"] = digest
    sealed["cycle_state"] = document["cycle"]["state"]
    excluded_roots = tuple(excluded_roots or record.get("excluded_roots") or ())
    sealed["excluded_roots"] = sorted(set(excluded_roots))
    if not already_closed:
        # §45 D-125: a first close leaves one history line.  A recorder that is not there (or
        # fails) leaves the line in the record for the next trigger; it never fails the close.
        line = _close_history_line(
            root, sealed, document, digest, directory, now=now,
            excluded=excluded_count if excluded_count is not None
            else len(record.get("excluded_symlinks") or []))
        root_lines = []
        cycle_rel = Path(os.path.relpath(str(directory), str(root))).as_posix()
        for rel_root in sorted(set(excluded_roots)):
            root_lines.append({
                "kind": "artifact", "target_type": "cycle", "target_id": record["cycle_id"],
                "target_path": f"{cycle_rel}/{rel_root}", "operation": "update",
                "field": "excluded_payload", "before": {"value": {"kind": "runtime"}},
                "after": {"value": {"kind": "runtime"}},
                "reason": "finalize", "transaction_id": _history_id("htxn", "excluded-payload", record["cycle_id"]),
                # Stable cycle+root identity intentionally records one line per root across reclassification.
                "event_id": _history_id("hevt", "excluded-payload", record["cycle_id"], rel_root),
                "now": now, **_history_actor("rule")})
        pending = _merge_pending(sealed.get("history_pending") or [], [line, *root_lines])
        sealed = _with_pending(sealed, [] if _history_deliver_locked(root, pending) else pending)
    _write_cycle_record(root, sealed, exclusive=False)
    _write_journal(root, record["cycle_id"], state="committed", manifest_digest=digest,
                   cycle_path=os.path.relpath(str(directory), str(root)))
    _remove_journal(root, record["cycle_id"])
    remove_interim(root, record["cycle_id"])
    # The seal is committed above.  The campaign locator index is a rebuildable view (readers scan
    # the records), so a failed write only warns; the next update of this campaign rescans it.
    try:
        artifact_locator.update_indexes(root, [record["campaign_id"]])
    except (OSError, artifact_locator.LocatorError) as exc:
        print(f"locator_index_update skipped campaign={record['campaign_id']} "
              f"error={type(exc).__name__}", file=sys.stderr)
    if already_closed:
        return
    artifact_cycle_titles.emit_after_seal_locked(root, sealed, document, directory / "manifest.json")
    try:
        import artifact_workflow_group_review  # lazy: it imports this module
        artifact_workflow_group_review.launch_after_seal(root, sealed)  # groups and metadata, one job
    except Exception:  # noqa: BLE001 -- the review trigger never changes a seal
        pass
