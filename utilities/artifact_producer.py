#!/usr/bin/env python3
"""W7C artifact write-cutover: producer begin/finalize lifecycle.

Correction to the W7 relocation: new cycle output is written in place under
`<artifact-root>/campaigns/<campaign-locator>/<cycle-locator>/artifacts/` and the
typed IDs are issued by `begin` *before* the first write (D-2, D-4).  The
step-1 modules stay the only lineage authorities: `artifact_identity` issues
IDs, `artifact_manifest` validates the closed D-6 schema, `artifact_index`
guards uniqueness, and `artifact_admission` owns the root identity, the global
mutex, and the derived index.

Layout (D-2, closed):

    campaigns/<campaign-locator>/campaign.json              mutable campaign record
    campaigns/<campaign-locator>/<cycle-locator>/.cycle.json stable-ID locator binding + started_on
    campaigns/<campaign-locator>/<cycle-locator>/artifacts  producer output (open)
    campaigns/<campaign-locator>/<cycle-locator>/manifest.json finalize commit point
    shared/<spec|analysis|research>/<ref>/reference.json
    shared/<kind>/<ref>/revisions/<rrev>/...          immutable revision
    .runtime/artifact-producer/v1/cutover.json        cutover state (approval-gated)
    .runtime/artifact-producer/v1/cycles/<cyc>.json   cycle record open|sealed|abandoned
    .runtime/artifact-producer/v1/journal/<cyc>.json  finalize crash journal
    .runtime/artifact-producer/v1/shared-journal/<rrev>.json
    .runtime/artifact-producer/v1/campaigns/<camp>.json     history lines waiting for the recorder; what is kept of a deleted campaign

A cycle's place, mark and existence are changed by `cycle-move`, `cycle-mark` and `delete` (§45 D-126);
a folder moved, renamed or removed by hand is found by the next listing, `begin` or campaign close
(`reconcile_root`).  None of them asks for approval or a confirmation.

Two cutover states.  While `cutover.json` is absent (`inactive`) the legacy
top-level buckets remain writable (compatibility window) and `begin` reports
`layout=legacy`; once the approval package activates the root, `begin` issues
a cycle and every new write outside an open cycle's `artifacts/` is denied.
Shared revisions are immutable in both states and are only created by
`admit-shared` from a sealed cycle.  Research is admitted to `shared/` only
with an explicit promotion (D-3).

Module map. This file holds the command line, `begin` and the route
environment, `status`, the write oracle (`check_write`, `require_cycle_output`,
`resolve_output_dir`), `recover`, and `finalize_exact_cycle` /
`verify_finalized_cycle` (read by AST from every runtime root by
`dispatch_runtime_support`, so they must stay defined here). The rest lives in
sibling modules, bottom layer first; each imports only from the ones above it
in this list, and this file re-exports every name they define:

    artifact_producer_store       constants, file primitives, records, cutover state
    artifact_producer_routes      route-to-cycle binding, admission, lineage
    artifact_producer_history     history lines waiting for the recorder
    artifact_producer_interim     interim manifests, reservations, checkpoint state
    artifact_producer_manifest    output enumeration, bundles, build_manifest
    artifact_producer_lease       review-output leases
    artifact_producer_refresh     refreshing sealed cycles (D-124/D-125)
    artifact_producer_spec        spec publication checks, shared-revision journal
    artifact_producer_finalize    finalize, sealed-cycle verification, recovery
    artifact_producer_shared      shared references, publication decisions, admit_shared
    artifact_producer_checkpoint  checkpoint runs
    artifact_producer_cycle_ops   cycle-mark, cycle-move, delete, reconcile (D-126)

A test that replaces a name with `mock.patch` must patch the module whose code
looks the name up; patching it here reaches only the code in this file and
callers that go through `artifact_producer.<name>`. `split_module_fixture.patch_split`
replaces a name in this file and every part that binds it, as one patch did
before the split.
"""
from __future__ import annotations

import argparse
import contextlib
import contextvars
import copy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import fcntl
import functools
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission  # noqa: E402
import artifact_payload  # noqa: E402
import artifact_cycle_titles  # noqa: E402
import artifact_identity  # noqa: E402
import artifact_index  # noqa: E402
import artifact_lifecycle  # noqa: E402
import artifact_locator  # noqa: E402
import artifact_manifest  # noqa: E402
import artifact_directory_bundle  # noqa: E402
import artifact_campaign  # noqa: E402
import route_identity  # noqa: E402
import route_lineage  # noqa: E402
import directory_record_index  # noqa: E402
import dispatch_contract  # noqa: E402
import dispatch_lock_order  # noqa: E402
import dispatch_terminal_commit  # noqa: E402
from runtime_observation import ObservationContext, stable_read_bytes  # noqa: E402
from dispatch_contract import (  # noqa: E402
    _PROCESS_IDENTITY_METADATA_KEYS,
    REVIEW_GOVERNED_LEASE_KIND,
    REVIEW_GOVERNED_LEASE_NONCE_RE,
    encode_review_output_locator,
    review_governed_lease_is_held,
    review_lease_record_digest,
    process_start_ticks,
    resolve_dispatch_state_root,
    resolve_agent_home,
    review_output_binding_digest,
    review_output_write_authorized,
    validate_review_output_binding,
    review_holder_disposition,
)
from dispatch_lifecycle import (
    FiniteWatchdogBudget,
    begin_finite_watchdog,
    remaining_watchdog_seconds,
)


# Re-exported so every name this module defined before the split stays an
# attribute of `artifact_producer` (callers, older releases and tests use them).
from artifact_producer_store import (  # noqa: F401
    _admitted_cycle_rows, _artifact_index_observation, _ARTIFACT_INDEX_PASS,
    _ArtifactIndexObservation, _campaign_path, _campaigns_by_key, _canonical, _closed_record,
    _CLOSED_RECORD_STATES, _copy_tree_files, _cycle_rel, _digest, _disposition_value,
    _earlier_documents, _ensure_dir, _excluded_by_roots, _fallback_blocks, _folder_cycle_id,
    _fsync_dir, _HASH_CHUNK_BYTES, _HISTORICAL_OBSERVATIONS, _HISTORICAL_PROJECTIONS,
    _is_payload_directory, _is_runtime_owned_top_level, _json_bytes, _KEY_RE, _LayoutScan,
    _legacy_content_names, _published_cycle_state, _QUIET_HISTORY, _read_json,
    _record_content_digest, _record_cycle_manifest_path, _reference_path, _rfc3339,
    _rfc3339_precise, _rfc3339_to_epoch, _SEALED_CYCLE_STATES, _stream_file_facts,
    _valid_cycle_state, _walk_cycle_files, _walk_files, _write_atomic, _write_campaign,
    _write_campaign_runtime_record, _write_cycle_binding, _write_cycle_record,
    _write_exclusive, ABANDON_REASONS, activate, ACTIVATION_KINDS, ALGORITHM_VERSION,
    artifact_index_observation_scope, BLOCKED, BUCKET_TYPES, campaign_dir,
    campaign_or_tombstone, CAMPAIGN_RUNTIME_DIR, campaign_runtime_path,
    campaign_runtime_record, CANONICAL_ROOTS, CAPABILITY_BUCKETS, classify_campaign_key,
    classify_root, CLOSED_BINDABLE_STATES, COMPAT_OVERRIDE_FIELDS, COMPAT_OVERRIDE_NAME,
    compat_override_path, CONTRACT, cutover_path, cycle_dir, cycle_disposition,
    cycle_record_closed, cycle_record_path, default_bucket, ENTRY_CAPABILITIES,
    find_campaign_by_key, INACTIVE_FALLBACK_ENV, inactive_fallback_level, INTENSITIES,
    INTERNAL_CAPABILITIES, is_active, journal_path, legacy_fallback_state, LEGACY_TOP_LEVEL,
    LEGACY_WRITE_HINT, list_cycle_records, MEDIA_TYPES, OK, PRIMARY_CANDIDATES, producer_dir,
    PRODUCER_REL, ProducerError, PROVISIONAL_SEAL_WARNING, read_campaign,
    read_campaign_tombstone, read_compat_override, read_cutover, read_cycle_record,
    read_relayout_state, RECORD_SEGMENTS, RELATED_KINDS, RELAYOUT_STATE_NAME,
    relayout_state_path, REVIEW_LEASE_REL, REVISION_RECORD_NAME, ROOT_CLASSES,
    RUNTIME_OWNED_EXACT, shared_journal_path, SHARED_KINDS, STAGE_CAPABILITIES,
    STATUS_DOCUMENT_NAMES, SUPPORT_SEGMENTS, transition_window_closed, USAGE,
    USER_DOCUMENT_NAMES, validate_time_bounded_grant, WAIVER_FIELDS,
)
from artifact_producer_routes import (  # noqa: F401
    _bind_cycle_route_locked, _binding_core, _closed_for_successor,
    _closed_unexecuted_continuation, _D120_NEXT_ACTION, _d120_next_action, _edge_stamps_hold,
    _finalize_route, _handed_over_routes, _indexed_route_children,
    _inline_producer_binding_check, _lineage_children, _qualified_continuation,
    _remove_journal, _route_binding_entry, _ROUTE_EDGES, _route_edges, _ROUTE_EDGES_SETTLE_NS,
    _ROUTE_ID_RE, _route_node, _RouteEdges, _routes_dir, _routes_dir_signature,
    _unexecuted_continuation, _write_journal, Admission, bind_cycle_route,
    closed_lineage_handover, cycle_route_admission, finalize_lineage_evidence, LineageHandover,
    load_route, resolve_cycle_manifest_route, resolve_route_argument, route_cycle_for,
    route_is_closed,
)
from artifact_producer_history import (  # noqa: F401
    _campaign_lines_locked, _close_history_line, _command_line, _command_stamp,
    _control_file_key, _control_keys, _control_keys_settled, _CONTROL_MEMO_SETTLE_NS,
    _deliver_pending, _flush_campaign_pending_locked, _flush_cycle_pending_locked, _HAND_ACTOR,
    _history_actor, _history_by, _history_deliver_locked, _history_file_ref, _history_id,
    _history_module, _HISTORY_TOKEN, _last_known_path, _locator_suffix, _merge_pending,
    _observe_control_changes, _observe_root_controls, _preserve_current_manifest,
    _QUIET_HISTORY_SECONDS, _read_control_memo, _read_history_quiet, _read_manifest_raw,
    _refresh_history_lines, _UNSET, _with_cycle_lines, _with_pending, _write_control_memo,
    _write_history_pending, _write_history_quiet, CONTROL_MEMO_NAME, deliver_pending_history,
    HISTORY_QUIET_NAME, record_campaign_state_line,
)
from artifact_producer_interim import (  # noqa: F401
    _checkpoint_limits_path, _checkpoint_lock, _checkpoint_lock_path, _ledger_row,
    _remove_empty_cycle, _reservation_from_document, _reservation_from_rows,
    _reservation_payload, _reserved_revision, _reserved_row, _route_release_supports_interim,
    _STALE_TEMP_SECONDS, _sweep_orphan_interims, _well_formed_or_none, _without_event_reuse,
    CHECKPOINT_DIR, CHECKPOINT_EXCLUDED_SUFFIXES, CHECKPOINT_FINALIZE_LOCK_SECONDS,
    CHECKPOINT_INTERVAL_ENV, checkpoint_interval_seconds, CHECKPOINT_LIMITS_FILE,
    checkpoint_limits_from_config, CHECKPOINT_LIMITS_MAX_BYTES,
    CHECKPOINT_MIN_INTERVAL_SECONDS, CHECKPOINT_STALE_SECONDS, checkpoint_state_path,
    CHECKPOINT_TRIGGERS, CheckpointLimits, closed_cycle_refresh_off,
    CLOSED_CYCLE_REFRESH_OFF_FILE, INTERIM_SUPPORT_MARKER, InterimReservation,
    OPEN_MANIFEST_DIR, open_manifest_path, read_interim_reservation, remove_interim,
    RESERVATION_HISTORY_LIMIT, reservation_path, RESERVATION_SCHEMA_VERSION, ReservedRevision,
)
from artifact_producer_manifest import (  # noqa: F401
    _browser_profile, _bucket_type, _bundle_exact_evidence_facts, _bundle_inventory_holds,
    _bundle_locator, _bundle_tree_fingerprints, _bundle_workflow_evidence_facts,
    _choose_primary, _cycle_relative_primary, _document_name, _enumerate_output,
    _is_cycle_root_support, _is_record_locator, _is_support_locator, _media_type,
    _observe_marker_directory, _output_paths, _output_placements, _outside_inclusion_rule,
    _payload_inner, _payload_root, _placed_locator, _publish_bundle_descriptors,
    _resolve_shared_pin, _scan_directory_bundles, _shared_pin_reference_path,
    _shared_pin_revision_path, _STATUS_RANK, _status_rank, _TEMPORARY_FILE_SUFFIXES,
    _unmanifestable_reason, _with_audience, artifact_audience, build_manifest,
    official_spec_primary, official_spec_primary_path, output_invalid_detail,
    OUTPUT_INVALID_NAMED, OUTPUT_INVALID_NAMED_BYTES, placed_output_proof,
    resolve_placed_output, spec_scope_components, validate_shared_reference_pins,
)
from artifact_producer_lease import (  # noqa: F401
    _is_v2_review_lease, _lease_record_is_live, _lease_record_state, _live_review_lease,
    _path_entry_present, _raise_if_recovery_fenced, _raise_if_review_publication_started,
    _review_lease_dir, _review_lease_observation, _review_lease_path,
    prepare_review_output_binding, review_lease_acquire, review_lease_release,
    review_lease_status,
)
from artifact_producer_refresh import (  # noqa: F401
    _binding_route_unrecorded, _bounded_scan, _commit_sealed, _confirmed_absent,
    _cycle_ids_with_records, _drop_unpublished_snapshot, _evaluate_provisional_completion,
    _fingerprint, _historical_handoff_completion_context, _history_published,
    _keep_prescan_digests, _leaf_terminal_additions, _manifest_bundle_directories,
    _manifest_terminal_source_digest, _Prescan, _prescan_cycle, _prescan_holds,
    _prescan_misses, _prescan_open_cycle, _provisional_completion_context,
    _provisional_completion_holds, _provisional_completion_projection, _pruned_directory,
    _publish_refresh, _read_refresh_state, _recheck_candidate, _reclassified_artifact_ids,
    _refresh_cursor_path, _refresh_cycle_locked, _refresh_cycle_observed,
    _refresh_journal_resume, _refresh_lock, _refresh_state_dir, _refresh_state_path,
    _refreshed_document, _RefreshScan, _remember_scan, _reread_misses, _scan_cycle_facts,
    _StaleScan, _UNLIMITED, _walk_sorted, _write_refresh_state, REFRESH_ABSENCE_CHECK_FLOOR,
    REFRESH_ADMISSION_WAIT_SECONDS, refresh_cycle, REFRESH_DIR, REFRESH_MAX_HASH_BYTES,
    REFRESH_MAX_SECONDS, REFRESH_MAX_WALK_ENTRIES, refresh_sweep, RefreshBudget,
)
from artifact_producer_spec import (  # noqa: F401
    _check_published_provenance, _check_requested_base, _check_shared_base, _commit_shared,
    _cycle_document_for_digest, _legacy_adopted_spec_base, _merge_spec_publication,
    _published_source_matches, _published_source_rows, _sealed_source_files,
    _spec_admission_base, _spec_bytes, _spec_inventory, _spec_inventory_digest,
    _spec_seed_components, _validate_shared_journal, _verified_shared_spec,
    _verify_completed_spec_publication, _verify_exact_spec_publication,
    _verify_spec_publication, component_set, SPEC_BASE_RECEIPT,
)
from artifact_producer_finalize import (  # noqa: F401
    _authorize_active_cleanup, _finalize_deleted_locked, _is_recoverable_locator_defect,
    _recover_exact_cycle_locked, _recover_locked, _recovery_open_candidates,
    _verify_deleted_cycle, _verify_sealed_cycle_locked, finalize, observe_finalized_cycle,
)
from artifact_producer_shared import (  # noqa: F401
    _code_cycle_publication_decision, _code_cycle_snapshot_publication_obligation,
    _code_cycle_snapshot_spec_facts, _code_cycle_snapshot_spec_hints, _code_spec_tree_changed,
    _finished_spec_publication, _latest_component_set, _revision_component_set,
    _scan_component_set, _shared_source_intersects_bundle, _spec_source_manifest_hints,
    _UNKNOWN_BASE, admit_shared, CANONICAL_SINGLE_REFERENCE_KINDS, check_component_sets,
    completed_route_spec_publication, completed_spec_publication, find_reference_by_key,
    list_references,
)
from artifact_producer_checkpoint import (  # noqa: F401
    _checkpoint_commit, _checkpoint_cycle, _checkpoint_scan, _checkpoint_target, checkpoint,
)
from artifact_producer_cycle_ops import (  # noqa: F401
    _adopt_location_locked, _drop_group_membership, _edit_campaign_members,
    _find_campaign_by_key_any_state, _hand_changes, _hand_changes_hold,
    _look_again_before_gone, _manifest_fingerprint, _mark_primary, _next_document,
    _parent_chain_has, _PreManifest, _preread_manifests, _publish_document_locked,
    _reconcile_locked_run, _remove_folder, _reparent_children_locked,
    _resolve_campaign_argument, _retarget_index_paths, _retire_rows, _revive_cycle_locked,
    _scan_layout, _swap_index_path_locked, _tombstone_cycle_locked, cycle_mark, cycle_move,
    delete_campaign, delete_cycle, mark_campaign_superseded, mark_cycle_superseded,
    reconcile_root, set_campaign_related, validate_related,
)


def status(root: Path) -> Dict[str, Any]:
    root = Path(root).resolve()
    identity = artifact_lifecycle.read_root_identity(root)
    records = list_cycle_records(root)
    counts: Dict[str, int] = {}
    for row in records:
        counts[row.get("state", "?")] = counts.get(row.get("state", "?"), 0) + 1
    journal_dir = producer_dir(root) / "journal"
    pending = sorted(p.stem for p in journal_dir.glob("*.json")) if journal_dir.is_dir() else []
    klass = classify_root(root)
    fallback = legacy_fallback_state(root, classification=klass)
    specs = [r for r in records if r.get("capability") == "autopilot-spec" and r.get("state") == "sealed"]
    code_specs = []
    resolved_references: Optional[Dict[str, Dict[str, Any]]] = None
    unresolved_code_references = False
    for record in records:
        if record.get("capability") != "autopilot-code" or record.get("state") != "sealed":
            continue
        decision = _code_cycle_publication_decision(
            root, record, list(resolved_references.values()) if resolved_references is not None else None)
        rows = decision.get("references")
        if rows is not None:
            if resolved_references is None:
                resolved_references = {}
            for reference in rows:
                reference_id = reference.get("shared_reference_id")
                if isinstance(reference_id, str):
                    resolved_references[reference_id] = reference
        if decision["obligation"]:
            code_specs.append(record)
            if rows is None:
                unresolved_code_references = True
    publication_cycles = [*specs, *code_specs]
    if not publication_cycles:
        references = []
    elif specs or unresolved_code_references or resolved_references is None:
        references = list_references(root, "spec")
    else:
        references = list(resolved_references.values())
    # Read shared revision metadata once per status call, not once per cycle.
    # This is an in-call projection, never a cache or publication authority.
    lineage: Dict[str, Any] = {}
    for reference in references:
        ref_id = reference["shared_reference_id"]
        for revision_id in reversed(reference.get("revisions", [])):
            revision = _read_json(root / "shared/spec" / ref_id / "revisions" / revision_id / REVISION_RECORD_NAME)
            if revision is not None:
                source = revision.get("source")
                source_cycle = source.get("cycle_id") if isinstance(source, Mapping) else None
                if isinstance(source_cycle, str):
                    lineage.setdefault(source_cycle, {}).setdefault(ref_id, []).append((revision_id, revision))
    publications = [completed_spec_publication(root, cycle_id=r["cycle_id"], references=references,
                                              known_publications=lineage.get(r["cycle_id"], {}))
                    for r in publication_cycles]
    return {
        "artifact_root": str(root),
        "cutover": read_cutover(root),
        "identity": identity.to_payload() if identity else None,
        "cycle_counts": counts,
        "open_cycles": [r["cycle_id"] for r in records if r.get("state") == "open"],
        "pending_journals": pending,
        "shared_spec_publications": [p for p in publications if p["status"] != "not-applicable"],
        "root_classification": klass["state"],
        "activation_kind": read_cutover(root).get("activation_kind", "approval") if klass["state"] == "active" else None,
        "legacy_fallback": fallback,
    }


# ---------------------------------------------------------------------------
# begin
# ---------------------------------------------------------------------------


# SD-163: read-only lookup of a prior cycle's same-named output. Every failure
# below is "not found"; nothing here refuses, gates, or writes.
INPUT_SOURCE_MAX_DEPTH = 6
INPUT_SOURCE_MAX_ENTRIES = 4096
INPUT_SOURCE_MAX_CANDIDATES = 16


def _input_target(name: Any) -> Optional[Tuple[Tuple[str, ...], bool]]:
    """Return (path components, is_dir) for a declared input, or None when it is not a plain path."""
    if not isinstance(name, str):
        return None
    is_dir = name.endswith("/**")
    parts = tuple((name[:-3] if is_dir else name).split("/"))
    if any(part in ("", ".", "..") or any(char in part for char in "*?[<>") for part in parts):
        return None
    return parts, is_dir


def _cycle_artifacts_dir(root: Path, cycle_id: str) -> Optional[Path]:
    record = read_cycle_record(root, cycle_id)
    if record is None:
        return None
    artifacts = cycle_dir(root, record["campaign_id"], cycle_id, record) / "artifacts"
    if artifacts.is_symlink() or not artifacts.is_dir():
        return None
    resolved = artifacts.resolve(strict=True)
    return resolved if resolved.is_relative_to(Path(root).resolve()) else None


def _scan_cycle_artifacts(artifacts: Path) -> Optional[List[Tuple[str, bool]]]:
    """List (relative posix path, is_dir) below artifacts/; symlinks are skipped, caps give None."""
    found: List[Tuple[str, bool]] = []
    stack = [(artifacts, 0)]
    visited = 0
    while stack:
        directory, depth = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > INPUT_SOURCE_MAX_ENTRIES:
                    return None
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                if not is_dir and not entry.is_file(follow_symlinks=False):
                    continue
                found.append((Path(entry.path).relative_to(artifacts).as_posix(), is_dir))
                if is_dir and depth < INPUT_SOURCE_MAX_DEPTH:
                    stack.append((Path(entry.path), depth + 1))
    return found


def _session_cycle_ids(root: Path, campaign: Mapping[str, Any], route_chain_identity,
                       begin_cycle: Mapping[str, str]) -> set:
    """Cycles of the routes this session composed for the campaign, read from its route-chain ledger.

    A ledger line names a route, never a cycle. A cycle record names its begin route, so a line
    maps to a cycle directly when that route began one, and through its verified lineage when it is
    a continuation of a route that did. The cycle's own audit copy of rebound routes is not read.
    """
    try:
        harness, session_id = route_chain_identity[:2]
        tools_dir = Path(__file__).resolve().parents[1] / "tools"
        if str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        from fleet import route_chain
        if route_chain.WRITER_SUPPORT.get(harness) != "env":
            return set()
        resolved = Path(root).resolve()
        composed = {line["route_id"]
                    for line in route_chain.read_tail(harness, session_id, max_bytes=1024 * 1024)
                    if line.get("event") in route_chain.COMPOSING_EVENTS
                    and line.get("campaign_key") == campaign.get("key")
                    and Path(str(line.get("artifact_root") or "")).resolve() == resolved}
    except Exception:
        return set()
    cycles = set()
    for route_id in composed:
        found = begin_cycle.get(route_id)
        if found is None:
            try:
                route = load_route(root, route_lineage.canonical_route_path(root, route_id))
                for ancestor in route_lineage.verified_route_lineage(route, artifact_root=root):
                    found = found or begin_cycle.get(ancestor["route_id"])
            except Exception:
                continue
        if found is not None:
            cycles.add(found)
    return cycles


def same_flow_source_cycle(root: Path, campaign_id: str, *, capability: str,
                           route_chain_identity=None, before: Optional[str] = None) -> Optional[str]:
    """The most recent cycle of the same flow, or None; never another flow's cycle.

    Order: the composing session's own route cycle in this campaign, then the
    latest cycle whose route capability is ``capability``. ``before`` keeps only
    cycles that started earlier than that cycle.
    """
    campaign = read_campaign(root, campaign_id)
    cycles = (campaign or {}).get("cycles")
    if (not campaign or campaign.get("degraded") is True or campaign.get("key") == UNASSIGNED_KEY
            or not isinstance(cycles, list) or not cycles):
        return None
    if before is not None:
        if before not in cycles:
            return None
        cycles = cycles[:cycles.index(before)]
    # Only the campaign's own cycles can answer, so only their records are read (not every
    # record of the root); `read_cycle_record` recovers a removed record as the listing does.
    records = {}
    for cycle_id in dict.fromkeys(cycles):
        row = read_cycle_record(root, cycle_id) if isinstance(cycle_id, str) else None
        if row is not None and row.get("campaign_id") == campaign_id:
            records[cycle_id] = row
    begin_cycle = {rec["route_id"]: cycle_id for cycle_id, rec in records.items() if rec.get("route_id")}
    session_cycles = (_session_cycle_ids(root, campaign, route_chain_identity, begin_cycle)
                      if route_chain_identity else set())
    for cycle_id in reversed(cycles):
        if cycle_id in session_cycles:
            return cycle_id
    for cycle_id in reversed(cycles):
        rec = records.get(cycle_id)
        route_capability = rec.get("route_capability") if rec else None
        if not route_capability and rec and rec.get("route_file"):
            try:
                route_file = Path(rec["route_file"]).resolve(strict=True)
                if route_file.is_relative_to(Path(root).resolve()):
                    route_capability = (_read_json(route_file) or {}).get("capability")
            except Exception:
                pass
        if route_capability == capability:
            return cycle_id
    return None


def input_source_cycle(root: Path, *, parent_cycle_id: Optional[str] = None,
                       campaign_key: Optional[str] = None, capability: Optional[str] = None,
                       route_chain_identity=None) -> Optional[str]:
    """The one source cycle: the parent, else the joined campaign's most recent cycle."""
    if parent_cycle_id:
        return parent_cycle_id
    if not campaign_key or campaign_key == UNASSIGNED_KEY:
        return None
    decision = classify_campaign_key(list_campaign_summaries(
        root, active_only=False, include_dispositions=False), campaign_key)
    if decision.get("mode") not in ("join", "reopen"):
        return None
    return (same_flow_source_cycle(root, decision["campaign_id"], capability=capability,
                                   route_chain_identity=route_chain_identity)
            if capability else None)


def input_source_finder(root: Path, *, parent_cycle_id: Optional[str] = None,
                        campaign_key: Optional[str] = None, capability: Optional[str] = None,
                        route_chain_identity=None):
    """Return find(name) -> {"cycle_id", "path"} | None, resolving the source lazily and once."""
    root = Path(root)
    memo: Dict[str, Optional[Dict[str, str]]] = {}
    loaded: Dict[str, Any] = {}

    def lookup(name: str) -> Optional[Dict[str, str]]:
        if not loaded:
            loaded["entries"] = None
            cycle_id = input_source_cycle(root, parent_cycle_id=parent_cycle_id, campaign_key=campaign_key,
                                          capability=capability, route_chain_identity=route_chain_identity)
            artifacts = _cycle_artifacts_dir(root, cycle_id) if cycle_id else None
            if artifacts is not None:
                loaded.update(cycle_id=cycle_id, artifacts=artifacts,
                              entries=_scan_cycle_artifacts(artifacts))
        target = _input_target(name)
        if target is None or loaded["entries"] is None:
            return None
        parts, is_dir = target
        rels = [rel for rel, entry_dir in loaded["entries"]
                if entry_dir == is_dir and tuple(rel.split("/"))[-len(parts):] == parts]
        if not rels or len(rels) > INPUT_SOURCE_MAX_CANDIDATES:
            return None
        # Shortest cycle-relative path by character length, then lexicographic; never by recency.
        chosen = loaded["artifacts"] / min(rels, key=lambda rel: (len(rel), rel))
        resolved = chosen.resolve(strict=True)
        if not resolved.is_relative_to(loaded["artifacts"]):
            return None
        return {"cycle_id": loaded["cycle_id"], "path": resolved.relative_to(root.resolve()).as_posix()}

    def find(name: str) -> Optional[Dict[str, str]]:
        if name not in memo:
            try:
                memo[name] = lookup(name)
            except Exception:  # SD-163: every lookup failure is "not found"
                memo[name] = None
        return dict(memo[name]) if memo[name] else None

    return find


def _composing_anchor(route_id, root=None):
    """The depth-0 session whose route-chain ledger says it composed ``route_id``, else None.

    A route file is written once, so its mtime bounds the ledgers that can hold the compose line.
    """
    try:
        created = None
        try:
            created = (Path(root) / ".runtime" / "routes" / f"{route_id}.json").stat().st_mtime if root else None
        except OSError:
            pass
        tools_dir = Path(__file__).resolve().parents[1] / "tools"
        if str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        from fleet import route_chain
        return route_chain.composing_anchor(route_id, not_before=created)
    except Exception:
        return None


def _parent_output_dir(root: Path, record: Mapping[str, Any], route: Optional[Mapping[str, Any]],
                       route_chain_identity=None) -> Optional[str]:
    """Absolute artifacts/ of the source cycle: parent, else sealed input_sources, else the campaign's previous cycle."""
    try:
        cycle_id = record.get("parent_cycle_id")
        for node in (route or {}).get("nodes") or []:
            if cycle_id:
                break
            sources = node.get("input_sources") if isinstance(node, Mapping) else None
            for source in (sources.values() if isinstance(sources, Mapping) else ()):
                cycle_id = cycle_id or (source.get("cycle_id") if isinstance(source, Mapping) else None)
        cycle_id = cycle_id or same_flow_source_cycle(
            root, record["campaign_id"], capability=(route or {}).get("capability") or record.get("route_capability"),
            route_chain_identity=route_chain_identity, before=record["cycle_id"])
        directory = _cycle_artifacts_dir(root, cycle_id) if cycle_id and cycle_id != record["cycle_id"] else None
        return str(directory) if directory else None
    except Exception:  # SD-163: every lookup failure is "not found"
        return None


def _env_for(root: Path, record: Mapping[str, Any], route: Optional[Mapping[str, Any]] = None,
             route_chain_identity=None) -> Dict[str, str]:
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    env = {
        "AGENT_ARTIFACT_ROOT": str(root),
        "AGENT_ARTIFACT_CAMPAIGN_ID": record["campaign_id"],
        "AGENT_ARTIFACT_CYCLE_ID": record["cycle_id"],
        "AGENT_ARTIFACT_PRODUCER_ID": record["producer_id"],
        "AGENT_ARTIFACT_CYCLE_DIR": str(directory),
        "AGENT_ARTIFACT_OUTPUT_DIR": str(directory / "artifacts"),
    }
    import worker_bootstrap
    runtime_context = {}
    if route and route.get("source_commit") == "unversioned":
        runtime_context = {"repository_kind": "non-git", "source_root": route.get("cwd")}
    runtime_candidate = worker_bootstrap.runtime_dir_candidate(root, **runtime_context)
    if runtime_candidate:
        env["AGENT_RUNTIME_DIR"] = runtime_candidate
    # This is an already-declared producer context. Carrying it through the
    # existing artifact env lets the next explicit same-campaign cycle join
    # without an agent remembering a group ID or changing route lineage.
    import artifact_workflow_groups
    group_id = artifact_workflow_groups.group_for_cycle(root, record["campaign_id"], record["cycle_id"])
    if group_id:
        env["AGENT_ARTIFACT_WORKFLOW_GROUP_ID"] = group_id
    if route_chain_identity is None and route:
        route_chain_identity = _composing_anchor(route.get("route_id"), root)
    parent_output = _parent_output_dir(root, record, route, route_chain_identity)
    if parent_output:
        env["AGENT_ARTIFACT_PARENT_OUTPUT_DIR"] = parent_output
    return env


# Total time an owner launch waits for the admission lock while a holder on a
# slow disk keeps it. Past it nothing has started and the caller is told to run
# start again later.
OWNER_LAUNCH_ADMISSION_WAIT_SECONDS = 120.0
_RECONCILE_FINISHED = frozenset({"unchanged", "reconciled", "inactive"})


def _begin_waiting_for_admission(root: Path, **kwargs: Any) -> Dict[str, Any]:
    """`begin`, asking again while the admission lock is held, up to the launch wait bound.

    AdmissionBusy surfaces when the lock is taken, before anything is written,
    and the steps ahead of it are read-only, so asking again is safe. Past the
    bound nothing has started: the typed error tells the caller to run start
    again later.
    """
    deadline = time.monotonic() + OWNER_LAUNCH_ADMISSION_WAIT_SECONDS
    # A node begin never looks for hand changes, and neither does the launch binding: it only
    # resumes the cycle the same start's preparation began (and looked for) seconds before.
    # Nor does a frame leg joining the cycle of its live sibling leg (`sibling_looked`), whenever
    # this route's later start launches it: the sibling's preparation looked the root over and
    # handed the waiting history lines over before beginning this cycle. A hand change or a line
    # since then is found by the next begin, listing or campaign close (reconcile is best effort).
    sibling_looked = bool(kwargs.pop("sibling_looked", False))
    looked = kwargs.get("node_id") is not None or bool(kwargs.get("resume_only")) or sibling_looked
    while True:
        if not looked:
            # §45 D-126 before the cycle begins. A look that finished is not repeated while
            # asking again only waits for the lock; one that could not finish (skipped, e.g.
            # its own lock wait ran out) is taken again before the next try.
            looked = reconcile_root(root).get("status") in _RECONCILE_FINISHED
        try:
            return begin(root, _reconcile=False, _deliver_history=not sibling_looked, **kwargs)
        except artifact_admission.AdmissionBusy:
            if time.monotonic() >= deadline:
                raise ProducerError(
                    "admission-busy",
                    f"admission lock held past {OWNER_LAUNCH_ADMISSION_WAIT_SECONDS:g}s; "
                    "nothing started; run start again later")


def bind_owner_launch(args, jobs: Path, *, environ=None) -> Optional[Dict[str, Any]]:
    """Publish the owner binding at the registered launch seam (§13.53.3).

    The wrapper has claimed the owner row but has not spawned it yet. Reusing
    begin's resume-only success path preserves every admission check and keeps
    settlement read-only.
    """
    environ = os.environ if environ is None else environ
    if not dispatch_contract.is_runtime_owner_launch(args) or not environ.get("AGENT_ARTIFACT_CYCLE_ID"):
        return None
    owner_binding = getattr(args, "owner_route_binding", None)
    route_file = (owner_binding.get("route_file") if isinstance(owner_binding, Mapping)
                  else getattr(owner_binding, "route_file", None)) or getattr(args, "route_file", None)
    if not route_file:
        return None
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
        root = Path(route["artifact_root"]).resolve()
        validated_route = load_route(root, Path(route_file))
        existing_open = route_cycle_for(root, validated_route)
        env_cycle = environ["AGENT_ARTIFACT_CYCLE_ID"]
        if existing_open is not None and existing_open["cycle_id"] != env_cycle:
            raise ProducerError("producer-binding-mismatch",
                                f"launch-cycle={env_cycle} bound={existing_open['cycle_id']}")
        result = _begin_waiting_for_admission(
            root, route_file=Path(route_file), capability=route["capability"],
            intensity=route["effective_intensity"], require_cycle=True,
            jobs=Path(jobs), owner_attempt_id=args.attempt_id, resume_only=True)
        if result.get("cycle_id") != env_cycle:
            raise ProducerError("producer-binding-mismatch",
                                f"launch-cycle={env_cycle} bound={result.get('cycle_id', '')}")
        return result
    except ProducerError:
        raise
    except Exception as exc:
        detail = str(getattr(exc, "detail", "") or exc)
        raise ProducerError(getattr(exc, "code", type(exc).__name__), detail) from exc


def prepare_route_artifact_env(route_file: Path, *, start: bool, jobs: Path,
                               require_cycle: bool = True, sibling_looked: bool = False) -> Dict[str, str]:
    """Resolve the route's own output context; callers need not copy begin's env.

    Start owns idempotent preparation. Readiness checks only read an existing
    cycle. No inherited cycle or 'latest' directory participates in selection.
    `sibling_looked`: a live sibling leg of the same route prepared this cycle (in this start
    or an earlier one), so this preparation does not walk the root for hand changes or waiting
    history lines again; the next begin, listing or campaign close does.
    """
    raw = _read_json(route_file)
    if not isinstance(raw, dict) or not raw.get("artifact_root"):
        raise ProducerError("route-artifact-root-missing", str(route_file))
    root = Path(raw["artifact_root"]).resolve()
    route = load_route(root, route_file)
    if start:
        context = (route.get("work_request") or {}).get("workflow_group_context")
        selection = {}
        if context:
            from work_start import group_context_matches_campaign
            if not group_context_matches_campaign(route, context):
                raise ProducerError("workflow-group-campaign-mismatch", context["campaign_id"])
            selection = {"workflow_group_id": context["group_id"]}
        return _begin_waiting_for_admission(
            root, route_file=route_file, capability=route["capability"],
            intensity=route["effective_intensity"], require_cycle=require_cycle, jobs=jobs,
            sibling_looked=sibling_looked, **selection)["env"]
    record = route_cycle_for(root, route)
    if record is None:
        return {"AGENT_ARTIFACT_ROOT": str(root), **{name: "" for name in (
            "AGENT_ARTIFACT_CAMPAIGN_ID", "AGENT_ARTIFACT_CYCLE_ID", "AGENT_ARTIFACT_PRODUCER_ID",
            "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_OUTPUT_DIR")}}
    return _env_for(root, record, route)


def _route_naming(
    route: Mapping[str, Any], campaign: Optional[Mapping[str, Any]],
    *, title: Optional[str], goal: Optional[str], root: Optional[Path] = None,
) -> Tuple[str, str, str, bool]:
    """Return canonical slug, display title, source, and truncation fact.

    Slugless records are pre-W7I routes.  During the explicit transition
    window they remain usable and are marked so migration can distinguish
    them from routes that sealed a slug.  D-91 closes that window when the
    root's relayout completes; from then on a slugless route is a typed
    refusal, never a silent derived name.
    """
    route_slug = route.get("slug")
    if isinstance(route_slug, str) and route_slug:
        slug, normalized_truncated = artifact_locator.slugify(route_slug)
        return slug, str(title or slug), "route", bool(route.get("slug_truncated")) or normalized_truncated
    if root is not None and transition_window_closed(root):
        raise ProducerError("route-slug-missing", str(route.get("route_id") or "route"))
    candidates = []
    if campaign is not None:
        candidates.extend((campaign.get("slug"), campaign.get("title")))
    candidates.extend((title, goal))
    if campaign is not None:
        candidates.append(campaign.get("goal"))
    raw = next((str(value) for value in candidates if isinstance(value, str) and value.strip()), "unnamed")
    slug, truncated = artifact_locator.slugify(raw, fallback="unnamed")
    display_title = str(title or (campaign or {}).get("title") or slug)
    return slug, display_title, "derived-legacy-route", truncated


UNASSIGNED_KEY = "_unassigned"


def _campaign_naming(campaign_key: Optional[str]) -> Tuple[str, str, str, bool]:
    """Return (slug, title, slug_source, truncated) for a *campaign* record.

    A campaign is the work stream the agent proposed; its locator and title
    come from that proposal, never from the first route that happened to
    join it.  Deriving the campaign name from the first cycle's slug produced
    ``<date>_project-c-analysis-cx`` for the stream ``project-c-icassp``
    (Project-C 2026-09-15).  The reserved `_unassigned` container keeps its
    fixed name.
    """
    if campaign_key is None:
        return "unassigned", UNASSIGNED_KEY, "reserved", False
    slug, truncated = artifact_locator.slugify(campaign_key, fallback="stream")
    return slug, campaign_key, "campaign-key", truncated


RECOVERED_SOURCE = "retirement-backup-mtime"


def default_backup_store() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "hearting" / "artifact-retirement"


def _retirement_mtime_index(run_dir: Path) -> Dict[str, int]:
    """``source path -> mtime`` of a retirement archive, cached beside it.

    The archive is the pre-migration bytes with their original mtimes (the
    W7C/W7G copies lost theirs). Listing a multi-GB gzip means decompressing
    it once; the cache is keyed by the seal's archive digest."""
    import tarfile
    seal = _read_json(run_dir / "backup-seal.json") or {}
    cache_path = run_dir / "mtime-index.json"
    cached = _read_json(cache_path)
    if (isinstance(cached, dict) and cached.get("archive_sha256") == seal.get("archive_sha256")
            and isinstance(cached.get("members"), dict)):
        return {str(k): int(v) for k, v in cached["members"].items()}
    members: Dict[str, int] = {}
    with tarfile.open(run_dir / "retired-sources.tar.gz", "r:gz") as archive:
        for member in archive:
            if member.isfile():
                members[member.name] = int(member.mtime)
    try:
        _write_atomic(cache_path, _json_bytes({"schema_version": 1, "archive_sha256": seal.get("archive_sha256"),
                                               "members": members}))
    except OSError:
        pass  # the cache is a convenience; the archive stays the source
    return members


def _retirement_digest_index(store: Path, root_id: str) -> Tuple[Dict[str, Tuple[str, int, str]], List[str]]:
    """``sha256 -> (source path, mtime, run)`` over every retirement run of a root."""
    by_sha: Dict[str, Tuple[str, int, str]] = {}
    runs: List[str] = []
    base = store / root_id
    if not base.is_dir():
        return by_sha, runs
    for run_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        manifest = run_dir / "retired-manifest.jsonl"
        if not manifest.is_file() or not (run_dir / "retired-sources.tar.gz").is_file():
            continue
        mtimes = _retirement_mtime_index(run_dir)
        runs.append(run_dir.name)
        for line in manifest.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            source, sha = row.get("source"), row.get("sha256")
            if isinstance(source, str) and isinstance(sha, str) and source in mtimes:
                by_sha.setdefault(sha, (source, mtimes[source], run_dir.name))
    return by_sha, runs


def recover_cycle_times(root: Path, *, backup_store: Optional[Path] = None,
                        apply: bool = False) -> Dict[str, Any]:
    """Recover the start time of cycles that only know their work's date.

    W7G resplit and W7H residue cycles were built from copies whose mtimes
    were the copy time, so their records carry a date (or the resplit run
    time) and no clock. The retirement backup of the same root keeps the
    original files with their original mtimes, and its manifest keys them by
    sha256. Each sealed artifact revision's ``content_digest`` therefore leads
    back to the original file; the earliest such mtime is the cycle's
    ``recovered_started_on`` (UTC, second precision), kept beside the
    untouched ``started_on`` with its evidence. An mtime is a *last* write:
    when the earliest one lands after the folder's date (a later bulk
    rewrite), it cannot be the start, so it is stored as evidence only
    (``recovered_earliest_write``) and the display keeps the date. Earlier
    than the folder date means the folder date was wrong (a copy date) and
    the recovered time wins. Dry run by default; ``apply``
    holds the producer admission lock, journals every record pre-image under
    ``.runtime/artifact-producer/v1/time-recovery/`` and rebuilds the indexes.
    """
    root = Path(root).resolve()
    store = Path(backup_store) if backup_store is not None else default_backup_store()
    identity = artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise ProducerError("root-identity-missing", str(root))
    by_sha, runs = _retirement_digest_index(store, identity.artifact_root_id)
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT) if apply else None
    journal: List[Dict[str, Any]] = []
    try:
        rows: List[Dict[str, Any]] = []
        for record_path in sorted((producer_dir(root) / "cycles").glob("cyc_*.json")):
            record = _read_json(record_path)
            if not isinstance(record, dict):
                continue
            if not (record.get("derived_from_cycle_id") or record.get("started_on_source")):
                continue  # a producer-born cycle already has its real clock
            cycle_id = record["cycle_id"]
            row: Dict[str, Any] = {"cycle_id": cycle_id, "locator": record.get("locator"),
                                   "previous": record.get("recovered_started_on")}
            try:
                directory = artifact_locator.resolve_path(root, cycle_id)
            except artifact_locator.LocatorError as exc:
                rows.append({**row, "action": "unresolved", "detail": exc.code}); continue
            if directory is None:
                rows.append({**row, "action": "unresolved", "detail": "no-directory"}); continue
            manifest = _read_json(directory / "manifest.json")
            if not isinstance(manifest, dict):
                rows.append({**row, "action": "no-manifest"}); continue
            digests = [str(rev.get("content_digest", "")).split(":", 1)[-1]
                       for rev in manifest.get("artifact_revisions", []) if isinstance(rev, dict)]
            hits = [by_sha[d] for d in digests if d in by_sha]
            row.update({"matched": len(hits), "total": len(digests)})
            if not digests:
                rows.append({**row, "action": "no-artifacts"}); continue
            if not hits:
                rows.append({**row, "action": "no-match"}); continue
            earliest = min(h[1] for h in hits)
            latest = max(h[1] for h in hits)
            recovered = _rfc3339(earliest)
            folder_date = str(record.get("locator") or "")[:10]
            usable = not folder_date or recovered[:10] <= folder_date
            evidence = {"matched": len(hits), "total": len(digests), "earliest": recovered,
                        "latest": _rfc3339(latest), "backup_runs": sorted({h[2] for h in hits})}
            row.update({"recovered_started_on": recovered if usable else None,
                        "earliest_write": recovered, "latest_write": evidence["latest"],
                        "backup_run": evidence["backup_runs"],
                        "folder_date_agrees": recovered[:10] == folder_date,
                        "display": "recovered" if usable else "evidence-only"})
            if (record.get("recovered_earliest_write") == recovered
                    and record.get("recovered_started_on") == (recovered if usable else None)):
                rows.append({**row, "action": "already"}); continue
            row["action"] = "recovered" if apply else "would-recover"
            if apply:
                journal.append({"cycle_id": cycle_id, "pre": dict(record)})
                record["recovered_earliest_write"] = recovered
                record["recovered_started_on_evidence"] = evidence
                if usable:
                    record["recovered_started_on"] = recovered
                    record["recovered_started_on_source"] = RECOVERED_SOURCE
                else:
                    record.pop("recovered_started_on", None)
                    record.pop("recovered_started_on_source", None)
                _write_cycle_record(root, record, exclusive=False)
            rows.append(row)
        counts: Dict[str, int] = {}
        for row in rows:
            counts[row["action"]] = counts.get(row["action"], 0) + 1
        result: Dict[str, Any] = {"status": "applied" if apply else "dry-run", "artifact_root": str(root),
                                  "backup_store": str(store), "backup_runs": runs, "counts": counts,
                                  "cycles": rows}
        if apply and journal:
            journal_dir = producer_dir(root) / "time-recovery"
            _ensure_dir(journal_dir)
            journal_file = journal_dir / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                                          + "-" + os.urandom(3).hex() + ".jsonl")
            _write_exclusive(journal_file, "".join(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
                                                    for entry in journal).encode("utf-8"), 0o600)
            result["journal"] = str(journal_file)
            artifact_locator.rebuild_indexes(root)
        return result
    finally:
        if lock_fd is not None:
            artifact_admission._release_lock(root, lock_fd)


def backfill_cycle_bindings(root: Path, *, apply: bool = False) -> Dict[str, Any]:
    """Add ``started_on`` to readable-layout ``.cycle.json`` bindings that predate it.

    The value follows ``artifact_locator.display_started_on``: a resplit cycle's
    work date (D-79 ``resplit_started_on``, date-only), else the record's own
    ``started_on``, else the sealed manifest's. Nothing is estimated from
    directory names or mtimes: the field is display data and the record wins
    (D-88). A binding that
    already carries a different time is reported as ``conflict`` and left
    alone. Dry run by default; ``apply`` holds the producer admission lock,
    replaces each binding atomically and rebuilds the indexes. Idempotent.
    """
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT) if apply else None
    try:
        rows: List[Dict[str, Any]] = []
        counts: Dict[str, int] = {}
        for campaign_path in artifact_locator.iter_campaign_dirs(root):
            for entry, layout in artifact_locator.iter_cycle_dirs(campaign_path):
                if layout != "readable":
                    continue
                rel = entry.relative_to(root).as_posix()
                try:
                    binding = artifact_locator.read_cycle_binding(entry)
                except artifact_locator.LocatorError as exc:
                    rows.append({"path": rel, "action": "invalid", "detail": exc.code})
                    continue
                if binding is None:
                    rows.append({"path": rel, "action": "no-binding"})
                    continue
                cycle_id = binding["cycle_id"]
                record = artifact_locator.read_cycle_record(root, cycle_id) or {}
                manifest = artifact_locator._read_json(entry / "manifest.json") or {}
                started_on = artifact_locator.display_started_on(record, manifest)
                source = None
                if started_on is not None:
                    # `display_started_on` may have trimmed a placeholder clock; match on the prefix.
                    source = ("record:resplit_started_on" if str(record.get("resplit_started_on")).startswith(started_on)
                              else "record" if str(record.get("started_on")).startswith(started_on) else "manifest")
                row: Dict[str, Any] = {"cycle_id": cycle_id, "path": rel, "source": source,
                                       "started_on": started_on}
                if started_on is None:
                    row["action"] = "missing"
                elif "started_on" in binding:
                    row["action"] = "present" if binding["started_on"] == started_on else "conflict"
                    if row["action"] == "conflict":
                        row["binding_started_on"] = binding["started_on"]
                else:
                    row["action"] = "added" if apply else "would-add"
                    if apply:
                        data = artifact_locator.cycle_binding_bytes(
                            binding["campaign_id"], cycle_id, started_on=started_on)
                        _write_atomic(entry / artifact_locator.CYCLE_BINDING, data)
                rows.append(row)
        for row in rows:
            counts[row["action"]] = counts.get(row["action"], 0) + 1
        if apply and counts.get("added"):
            artifact_locator.rebuild_indexes(root)
        return {"status": "applied" if apply else "dry-run", "artifact_root": str(root),
                "counts": counts, "cycles": rows}
    finally:
        if lock_fd is not None:
            artifact_admission._release_lock(root, lock_fd)


_CAMPAIGN_SUMMARY_INPUT = contextvars.ContextVar("campaign_summary_input", default=None)


@contextlib.contextmanager
def campaign_summary_scope():
    """Share validated shallow classifications for one input, never admission."""
    if _CAMPAIGN_SUMMARY_INPUT.get() is not None:
        yield
        return
    token = _CAMPAIGN_SUMMARY_INPUT.set({})
    try:
        yield
    finally:
        _CAMPAIGN_SUMMARY_INPUT.reset(token)


def list_campaign_summaries(root: Path, *, active_only: bool = True,
                            include_dispositions: bool = True) -> List[Dict[str, Any]]:
    """Rich CLI listing by default; internal classification can omit cycle census.

    Only the shallow path shares one input's fold. Writers keep their own fresh
    campaign reads and admission lock; the scope never changes their authority.
    """
    cache = None if include_dispositions else _CAMPAIGN_SUMMARY_INPUT.get()
    key = (os.path.abspath(root), active_only)
    if cache is not None and key in cache:
        value = cache[key]
        if isinstance(value, Exception):
            raise value
        return value
    try:
        rows = _list_campaign_summaries(root, active_only=active_only,
                                        include_dispositions=include_dispositions)
    except Exception as exc:
        if cache is not None:
            cache[key] = exc
        raise
    if cache is not None:
        cache[key] = rows
    return rows


def _list_campaign_summaries(root: Path, *, active_only: bool,
                             include_dispositions: bool) -> List[Dict[str, Any]]:
    """Pure read-only listing of the root's campaigns for callers that
    must show the agent which work streams already exist (compose).

    Each row uses the same validated campaign event fold as admission.
    This performs no reconcile, history flush, or index heal; writer
    commands (`campaign-close`, `compose`, `campaign-recover`) own repairs.
    """
    root = Path(root)
    rows: List[Dict[str, Any]] = []
    for entry in artifact_locator.iter_campaign_dirs(root):
        try:
            record, _ = artifact_campaign.read_json(root, entry / "campaign.json")
        except artifact_campaign.CampaignError:
            continue
        if not isinstance(record.get("campaign_id"), str):
            continue
        try:
            folded = artifact_campaign.campaign_state(root, entry / "campaign.json", record)
            state = folded.state
            projection_pending = folded.projection_pending
            state_error = None
        except artifact_campaign.CampaignError as exc:
            state, state_error = "invalid", exc.code
            projection_pending = False
        if active_only and state != "active":
            continue
        cycles = record.get("cycles")
        marks: Dict[str, int] = {}
        for cycle_id in cycles if include_dispositions and isinstance(cycles, list) else []:
            mark = cycle_disposition(read_cycle_record(root, cycle_id) or {})
            if mark is not None:
                marks[mark["kind"]] = marks.get(mark["kind"], 0) + 1
        rows.append({
            **({"dispositions": marks, "all_set_aside": sum(marks.values()) == len(cycles)} if marks else {}),
            "campaign_id": record["campaign_id"],
            "key": record.get("key"),
            "title": record.get("title"),
            "goal": record.get("goal"),
            "locator": entry.name,
            "state": state,
            **({"state_error": state_error} if state_error else {}),
            "projection_pending": projection_pending,
            "degraded": record.get("degraded") is True,
            "cycle_count": len(cycles) if isinstance(cycles, list) else 0,
            "created_on": str(record.get("created_on") or ""),
        })
    rows.sort(key=lambda row: (row["created_on"], str(row["key"])), reverse=True)
    return rows


def _campaign_degradation(campaign: Mapping[str, Any]) -> Dict[str, Any]:
    if campaign.get("degraded") is True:
        return {"degraded": True, "degraded_reason": campaign.get("degraded_reason", "campaign-unassigned")}
    return {}


# Key under which `_begin_cycle_record` hands `begin` what the env is read from: the env reads
# the root's other records, so it is resolved after the admission lock is released.
_ENV_SOURCE = "_env_source"


def _begin_cycle_record(
    root: Path,
    *,
    route_file: Path,
    capability: str,
    intensity: str,
    node_id: Optional[str] = None,
    campaign_id: Optional[str] = None,
    campaign_key: Optional[str] = None,
    title: Optional[str] = None,
    goal: Optional[str] = None,
    parent_cycle_id: Optional[str] = None,
    workflow_group_id: Optional[str] = None,
    workflow_stage_label: Optional[str] = None,
    require_cycle: bool = False,
    shared_reference_pins: Optional[Sequence[Mapping[str, Any]]] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    now: Optional[float] = None,
    jobs: Optional[Path] = None,
    owner_attempt_id: Optional[str] = None,
    resume_only: bool = False,
) -> Dict[str, Any]:
    dispatch_terminal_commit.require_current_cleanup("producer-begin", jobs=jobs)
    root = Path(root).resolve()
    if capability not in ENTRY_CAPABILITIES + STAGE_CAPABILITIES + INTERNAL_CAPABILITIES:
        raise ProducerError("capability-unknown", capability)
    if intensity not in INTENSITIES:
        raise ProducerError("intensity-unknown", intensity)
    resolved_route_file = resolve_route_argument(root, Path(route_file))
    route = load_route(root, resolved_route_file)
    # A sealed proposal survives dispatch; CLI may confirm, never override it.
    for field, supplied in (("campaign_key", campaign_key), ("parent_cycle_id", parent_cycle_id)):
        if field in route and supplied is not None and supplied != route[field]:
            raise ProducerError("route-campaign-selection-conflict", field)
    campaign_key = route.get("campaign_key", campaign_key)
    parent_cycle_id = route.get("parent_cycle_id", parent_cycle_id)
    if campaign_key is not None and (not isinstance(campaign_key, str) or not _KEY_RE.fullmatch(campaign_key)):
        raise ProducerError("campaign-key-invalid", str(campaign_key))
    if parent_cycle_id is not None and not artifact_identity.is_well_formed(parent_cycle_id, "cycle"):
        raise ProducerError("parent-cycle-invalid", str(parent_cycle_id))
    route_capability = route["capability"]
    if capability in ENTRY_CAPABILITIES + INTERNAL_CAPABILITIES and route_capability != capability:
        raise ProducerError("route-capability-mismatch", f"{route_capability}!={capability}")
    if route["effective_intensity"] != intensity:
        raise ProducerError("route-intensity-mismatch", f"{route['effective_intensity']}!={intensity}")
    node = _route_node(route, node_id)
    if route_is_closed(root, route):
        raise ProducerError("route-already-closed", route["route_id"])
    alloc = allocator or artifact_identity.IdAllocator()
    klass = classify_root(root)
    if klass["state"] == "malformed":
        raise ProducerError("cutover-record-malformed", klass["reason"])
    if klass["state"] == "inactive-empty":
        if resume_only:
            raise ProducerError("producer-binding-required", "route-cycle-absent")
        # D-73: bootstrap-first identity. MUST stay above the admission lock at
        # :547 -- activate() acquires the same lock and would self-deadlock.
        activate(root,
                 repository_id=alloc.allocate("repository"),
                 artifact_root_id=alloc.allocate("artifact_root"),
                 activation_kind="bootstrap-empty-root",
                 adopt_existing_identity=True,
                 now=now)
    elif klass["state"] == "inactive-with-legacy":
        fallback = legacy_fallback_state(root, now=now, classification=klass)
        if _fallback_blocks(fallback):
            raise ProducerError("cutover-inactive-fallback-denied",
                                fallback["override"]["reason"] or "override-absent")
        if require_cycle:
            raise ProducerError("cutover-inactive", "activation required before cycle issuance")
        return {
            "status": "legacy-compat",
            "layout": "legacy",
            "route_id": route["route_id"],
            "reason": "cutover-inactive",
            "env": {"AGENT_ARTIFACT_ROOT": str(root)},
            "legacy_fallback": fallback,
        }
    # active, or just bootstrapped: fall through to the existing cycle path.
    identity = artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise ProducerError("root-identity-missing")
    # SD-120 A1: only registered owner/worker contexts participate.  The
    # canonical jobs path and attempt identity come from the runtime; callers
    # do not get a route/owner override surface.
    binding_jobs = Path(jobs or os.environ.get("AGENT_DISPATCH_JOBS", ""))
    if not binding_jobs.is_absolute() or not binding_jobs.is_file():
        binding_jobs = None  # type: ignore[assignment]
    binding_owner = owner_attempt_id or os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
    if node_id is not None:
        binding_owner = os.environ.get("AGENT_DISPATCH_PARENT_ATTEMPT_ID", binding_owner)
    owner_begin = node_id is None
    if binding_jobs is not None and binding_owner:
        try:
            owner_binding = dispatch_terminal_commit.validate_owner_route(
                jobs=binding_jobs, route_file=resolved_route_file, owner_attempt_id=binding_owner)
            existing_open = route_cycle_for(root, route)
            binding_path = dispatch_terminal_commit.producer_binding_path(
                root, owner_binding.route_id, binding_owner)
            if binding_path.exists():
                loaded = dispatch_terminal_commit.load_producer_binding(
                    artifact_root=root, route_id=owner_binding.route_id, owner_attempt_id=binding_owner)
                if existing_open is None or loaded.binding.get("cycle_id") != existing_open.get("cycle_id"):
                    raise dispatch_terminal_commit.TerminalCommitError("transaction-conflict", str(binding_path))
            elif not owner_begin and existing_open is None:
                raise dispatch_terminal_commit.TerminalCommitError("producer-binding-required", str(binding_path))
        except dispatch_terminal_commit.TerminalCommitError as exc:
            raise ProducerError(exc.code, exc.detail) from exc
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        # W7G owns a root-wide resplit from R2 through R3.  Check its atomic
        # claim while holding the same admission mutex used for locator and
        # campaign updates, so begin cannot race the gap before its first
        # journal is durable or lose a campaign cycles[] update.
        resplit_lock = producer_dir(root) / "resplit.lock"
        if resplit_lock.exists() or resplit_lock.is_symlink():
            detail = _read_json(resplit_lock)
            raise ProducerError("resplit-in-progress", json.dumps(detail or {}, sort_keys=True))
        resumable = route_cycle_for(root, route)
        if resumable is not None and resumable.get("deleted_at"):
            resumable = None  # a deleted folder is never resumed; the route's next cycle is a new one
        if resume_only and resumable is None:
            raise ProducerError("producer-binding-required", "route-cycle-absent")
        campaign: Optional[Dict[str, Any]] = None
        parent = None
        campaign_reopen_event_id = None
        requested_selection = None
        if campaign_id:
            campaign = read_campaign(root, campaign_id)
            if campaign is None:
                raise ProducerError("campaign-unknown", campaign_id)
            requested_selection = {"by": "campaign_id", "value": campaign_id}
        elif campaign_key:
            keyed = _campaigns_by_key(root, campaign_key)
            choice = classify_campaign_key(keyed, campaign_key)
            if choice["mode"] == "blocked":
                raise ProducerError(choice["code"], campaign_key)
            campaign = next((row for row in keyed if row.get("campaign_id") == choice.get("campaign_id")), None)
            requested_selection = {"by": "campaign_key", "value": campaign_key}
        if parent_cycle_id:
            parent = read_cycle_record(root, parent_cycle_id)
            if parent is None:
                raise ProducerError("parent-cycle-not-joinable", parent_cycle_id)
            if parent.get("deleted_at") and read_campaign(root, parent["campaign_id"]) is None:
                parent = None
                parent_cycle_id = None
            # §45 D-123: a parent is a reference to any cycle of this root, in any
            # state and any campaign; it selects a campaign only when none was named.
            if campaign is None and parent is not None:
                campaign = read_campaign(root, parent["campaign_id"])
                if campaign is None:
                    raise ProducerError("campaign-unknown", parent["campaign_id"])
                requested_selection = {"by": "parent_cycle", "value": parent_cycle_id}
        import artifact_workflow_groups
        # Ambient context is accepted only for the exact selected campaign;
        # the parent-cycle edge alone never selects or implies a group.
        inherited_group_id = (
            os.environ.get("AGENT_ARTIFACT_WORKFLOW_GROUP_ID")
            if campaign is not None and os.environ.get("AGENT_ARTIFACT_CAMPAIGN_ID") == campaign["campaign_id"]
            else None
        )
        selected_group_id = workflow_group_id or inherited_group_id
        if selected_group_id and owner_begin:
            if campaign is None:
                raise ProducerError("workflow-group-campaign-required")
            if workflow_group_id and inherited_group_id and workflow_group_id != inherited_group_id:
                raise ProducerError("workflow-group-context-conflict", workflow_group_id)
            try:
                early_title = (resumable.get("title") if resumable is not None
                               else _route_naming(route, campaign, title=title, goal=goal, root=root)[1])
                early_label = workflow_stage_label or artifact_workflow_groups.stage_label_from_title(early_title)
                artifact_workflow_groups.preflight_join_locked(
                    root, campaign["campaign_id"], selected_group_id, early_label,
                    cycle_id=resumable["cycle_id"] if resumable is not None else None)
            except artifact_workflow_groups.WorkflowGroupError as exc:
                raise ProducerError(exc.code, exc.detail) from exc
        if workflow_stage_label is not None:
            try:
                artifact_workflow_groups._text(workflow_stage_label, 40, "stage-label-invalid")
            except artifact_workflow_groups.WorkflowGroupError as exc:
                raise ProducerError(exc.code, exc.detail) from exc
        if campaign is not None:
            if campaign.get("state") == "satisfied":
                try:
                    reopened = artifact_campaign._reopen_locked(
                        root, _campaign_path(root, campaign["campaign_id"], campaign),
                        route_id=route["route_id"], requested_selection=requested_selection)
                except artifact_campaign.CampaignError as exc:
                    raise ProducerError(exc.code, exc.detail) from exc
                campaign_reopen_event_id = reopened.get("event_id")
                campaign = read_campaign(root, campaign["campaign_id"])
            if campaign is None or campaign.get("state") != "active":
                raise ProducerError("campaign-not-active", campaign_id or parent_cycle_id or campaign_key)
            if campaign_key is not None and campaign.get("key") != campaign_key:
                raise ProducerError("campaign-key-mismatch", campaign_key)
        # Idempotent per route: one open cycle per verified lineage (D-120). A
        # continuation resuming an ancestor's open cycle is the same idempotent
        # path with `rebound=True` and an extended audit record.
        if resumable is not None:
            record = resumable
            admission = cycle_route_admission(root, record, route)
            if not admission.allow:
                raise ProducerError(admission.reason, admission.detail)
            bound_campaign = read_campaign(root, record["campaign_id"])
            if bound_campaign is not None and bound_campaign.get("state") == "satisfied" and owner_begin:
                try:
                    reopened = artifact_campaign._reopen_locked(
                        root, _campaign_path(root, bound_campaign["campaign_id"], bound_campaign),
                        route_id=route["route_id"])
                except artifact_campaign.CampaignError as exc:
                    raise ProducerError(exc.code, exc.detail) from exc
                campaign_reopen_event_id = reopened.get("event_id")
                bound_campaign = read_campaign(root, record["campaign_id"])
            if bound_campaign is None or bound_campaign.get("state") != "active":
                raise ProducerError("campaign-not-active", record["campaign_id"])
            if ((campaign is not None and campaign["campaign_id"] != record["campaign_id"])
                    or (campaign_key is not None and campaign_key != bound_campaign.get("key"))
                    or (parent_cycle_id is not None and parent_cycle_id != record.get("parent_cycle_id"))):
                raise ProducerError("cycle-campaign-selection-conflict", record["cycle_id"])
            rebound = record.get("route_id") != route["route_id"]
            # D-120: only an owner `begin --route <continuation>` writes the
            # audit trail. A worker `begin --node` judges the same admission
            # (raised above) but never mutates the record.
            if rebound and owner_begin:
                _bind_cycle_route_locked(root, record, route)
            if selected_group_id and owner_begin:
                stage_label = workflow_stage_label or artifact_workflow_groups.stage_label_from_title(record.get("title"))
                try:
                    artifact_workflow_groups.join_at_begin_locked(
                        root, record["campaign_id"], record["cycle_id"], selected_group_id, stage_label)
                except artifact_workflow_groups.WorkflowGroupError as exc:
                    raise ProducerError(exc.code, exc.detail) from exc
            if binding_jobs is not None and binding_owner:
                try:
                    dispatch_terminal_commit.publish_producer_binding(
                        artifact_root=root, jobs=binding_jobs, route_file=resolved_route_file,
                        owner_attempt_id=binding_owner, cycle_id=record["cycle_id"],
                        owner_begin=owner_begin, owner=owner_binding)
                except dispatch_terminal_commit.TerminalCommitError as exc:
                    raise ProducerError(exc.code, exc.detail) from exc
            title_updated = False
            if owner_begin and title is not None:
                # A repeated owner begin is the existing metadata edit surface.
                # Reread after a continuation bind to retain its route audit.
                current = read_cycle_record(root, record["cycle_id"])
                directory = cycle_dir(root, current["campaign_id"], current["cycle_id"], current)
                if current.get("state") == "open" and not (directory / "manifest.json").exists():
                    display_title = _route_naming(route, bound_campaign, title=title, goal=goal, root=root)[1]
                    if current.get("title") != display_title:
                        record = {**current, "title": display_title}
                        _write_cycle_record(root, record, exclusive=False)
                        title_updated = True
            return {
                "status": "resumed", "layout": "cycle", "campaign_id": record["campaign_id"],
                "cycle_id": record["cycle_id"], "producer_id": record["producer_id"],
                "cycle_dir": str(cycle_dir(root, record["campaign_id"], record["cycle_id"], record)),
                _ENV_SOURCE: (root, record, route),
                **({"rebound": True} if rebound else {}),
                **({"title_updated": True} if title_updated else {}),
                **({"campaign_reopened": True, "campaign_reopen_event_id": campaign_reopen_event_id}
                   if campaign_reopen_event_id else {}),
                **_campaign_degradation(bound_campaign),
            }
        if campaign is not None:
            artifact_locator.prepare_index_update(root, [campaign["campaign_id"]])
        head = artifact_admission.load_index_head(root)
        index = artifact_admission.load_index(root) if head is None else None
        if campaign is None and campaign_key is None:
            campaign = find_campaign_by_key(root, UNASSIGNED_KEY)
        campaign_created = False
        slug, display_title, slug_source, slug_truncated = _route_naming(
            route, campaign, title=title, goal=goal, root=root)
        started_on = _rfc3339(now)
        if campaign is None:
            new_campaign_id = alloc.allocate("campaign")
            while (head.has_prefixed_stable_id(new_campaign_id) if head is not None
                   else new_campaign_id in index.stable_ids):
                new_campaign_id = alloc.allocate("campaign")
            # The campaign is named from the proposed stream key; the route
            # slug names only this cycle (CONVENTIONS "Campaign or cycle").
            campaign_slug, campaign_title, campaign_slug_source, campaign_slug_truncated = (
                _campaign_naming(campaign_key))
            locator, locator_suffix = artifact_locator.allocate_locator(
                root / "campaigns", started_on, campaign_slug)
            campaign = {
                "schema_version": 1,
                "contract": CONTRACT,
                "campaign_id": new_campaign_id,
                "key": campaign_key or UNASSIGNED_KEY,
                **({"degraded": True, "degraded_reason": "campaign-unassigned"} if campaign_key is None else {}),
                "slug": campaign_slug,
                "title": campaign_title,
                "slug_source": campaign_slug_source,
                "slug_truncated": campaign_slug_truncated,
                "locator": locator,
                "locator_suffix": locator_suffix,
                "goal": (goal or f"{route_capability} cycle output") if campaign_key else "Work stream not proposed",
                "completion_criterion": {"statement": artifact_campaign.DEFAULT_COMPLETION_CRITERION},
                "state": "active",
                "created_on": started_on,
                "cycles": [],
            }
            campaign_created = True
            # The readable locator is persisted before children are created.
            _write_campaign(root, campaign, exclusive=True)
        else:
            # Existing W7 records keep their physical path until Cycle B, but
            # missing display fields are filled from this route for hybrid joins.
            campaign = dict(campaign)
            changed = False
            existing_key = campaign.get("key")
            fill_slug, fill_title, fill_source, fill_truncated = _campaign_naming(
                None if existing_key in (None, UNASSIGNED_KEY) else str(existing_key))
            for key, value in (
                ("slug", fill_slug), ("title", fill_title), ("slug_source", fill_source),
                ("slug_truncated", fill_truncated),
            ):
                if key not in campaign:
                    campaign[key] = value
                    changed = True
            # A campaign promoted out of `_unassigned` by the §37 metadata
            # amendment keeps the reserved placeholder title (the amendment
            # writes key/goal only); every later manifest would seal
            # `campaign.title = "_unassigned"`.  The key is the stream name.
            if existing_key not in (None, UNASSIGNED_KEY) and campaign.get("title") == UNASSIGNED_KEY:
                campaign["title"] = fill_title
                changed = True
            if changed:
                _write_campaign(root, campaign, exclusive=False)
        new_cycle_id = alloc.allocate("cycle")
        while ((head.has_prefixed_stable_id(new_cycle_id) if head is not None
                else new_cycle_id in index.stable_ids)
               or cycle_record_path(root, new_cycle_id).exists()):
            new_cycle_id = alloc.allocate("cycle")
        producer_id = alloc.allocate("producer")
        campaign_path = campaign_dir(root, campaign["campaign_id"], campaign)
        cycle_locator, cycle_locator_suffix = artifact_locator.allocate_locator(
            campaign_path, started_on, slug)
        record = {
            "schema_version": 1,
            "contract": CONTRACT,
            "cycle_id": new_cycle_id,
            "campaign_id": campaign["campaign_id"],
            "producer_id": producer_id,
            "parent_cycle_id": parent_cycle_id,
            **({"parent_cycle_state_at_begin": parent["state"]} if parent is not None else {}),
            "capability": capability,
            "route_capability": route_capability,
            "intensity": intensity,
            "route_id": route["route_id"],
            "route_hash": route["route_hash"],
            "route_file": str(resolved_route_file.resolve()),
            "node_id": node["id"] if node else None,
            "state": "open",
            "started_on": started_on,
            "sealed_on": None,
            "manifest_digest": None,
            "slug": slug,
            "title": display_title,
            "slug_source": slug_source,
            "slug_truncated": slug_truncated,
            "locator": cycle_locator,
            "locator_suffix": cycle_locator_suffix,
        }
        if shared_reference_pins is not None:
            record["shared_reference_pins"] = [dict(pin) for pin in shared_reference_pins]
        target = campaign_path / cycle_locator
        if target.exists():
            raise ProducerError("cycle-dir-exists", str(target))
        # Order: durable record first (crash before dir => recover drops the
        # record), then the folder.  Nothing here is visible to the index until
        # finalize's manifest commit point.
        _write_cycle_record(root, record, exclusive=True)
        _ensure_dir(target / "artifacts")
        campaign["cycles"] = list(campaign.get("cycles", [])) + [new_cycle_id]
        _write_campaign(root, campaign, exclusive=False)
        _write_cycle_binding(target, campaign["campaign_id"], new_cycle_id, started_on=started_on)
        if binding_jobs is not None and binding_owner:
            try:
                dispatch_terminal_commit.publish_producer_binding(
                    artifact_root=root, jobs=binding_jobs, route_file=resolved_route_file,
                    owner_attempt_id=binding_owner, cycle_id=new_cycle_id,
                    owner_begin=owner_begin, owner=owner_binding)
            except dispatch_terminal_commit.TerminalCommitError as exc:
                raise ProducerError(exc.code, exc.detail) from exc
        artifact_locator.update_indexes(root, [campaign["campaign_id"]])
        if selected_group_id and owner_begin:
            stage_label = workflow_stage_label or artifact_workflow_groups.stage_label_from_title(display_title)
            try:
                artifact_workflow_groups.join_at_begin_locked(
                    root, campaign["campaign_id"], new_cycle_id, selected_group_id, stage_label)
            except artifact_workflow_groups.WorkflowGroupError as exc:
                # The issued cycle is resumable by this exact route. Its next
                # begin retries the same group append under admission lock.
                raise ProducerError(exc.code, exc.detail) from exc
        return {
            "status": "begun", "layout": "cycle", "campaign_id": campaign["campaign_id"],
            "cycle_id": new_cycle_id, "producer_id": producer_id, "cycle_dir": str(target),
            "campaign_created": campaign_created, _ENV_SOURCE: (root, record, route),
            **({"campaign_reopened": True, "campaign_reopen_event_id": campaign_reopen_event_id}
               if campaign_reopen_event_id else {}),
            **_campaign_degradation(campaign),
        }
    finally:
        artifact_admission._release_lock(root, lock_fd)


def _observe_after_begin(root: Path, result: Mapping[str, Any]) -> None:
    """§45 D-124: an owner `begin` has the cycles it builds on looked at (detached, once per interval).

    The parent the new cycle names and the cycle a continuing route resumes may have changed since
    they closed.  The launcher never raises and never waits; this must not make `begin` slower or fail."""
    try:
        import artifact_checkpoint_trigger

        cycle_id = result.get("cycle_id")
        record = read_cycle_record(root, cycle_id) if cycle_id else None
        wanted = [record.get("parent_cycle_id")] if record else []
        if result.get("status") == "resumed":
            wanted.append(cycle_id)
        for observed in dict.fromkeys(item for item in wanted if item):
            artifact_checkpoint_trigger.launch(trigger="begin", key=f"begin-{observed}",
                                               artifact_root=str(root), cycle_id=observed)
    except Exception:  # noqa: BLE001 -- observing other cycles never fails a begin
        pass


def begin(root: Path, *, _reconcile: bool = True, _deliver_history: bool = True, **kwargs: Any) -> Dict[str, Any]:
    if _reconcile and kwargs.get("node_id") is None:
        reconcile_root(root)  # §45 D-126: what was moved or removed by hand is found before a cycle is begun
    result = _begin_cycle_record(root, **kwargs)
    source = result.pop(_ENV_SOURCE, None)
    if source is not None:
        # The admission lock only reserves the cycle; what its env names (the source cycle,
        # the workflow group, the runtime dir) is read after the lock is released.
        result["env"] = _env_for(*source)
    if kwargs.get("node_id") is None and result.get("layout") == "cycle":
        _observe_after_begin(Path(root).resolve(), result)
        if not kwargs.get("resume_only") and _deliver_history:
            # The same start's preparation handed waiting lines over seconds before (skipping the
            # records its look had just seen with none); a line still waiting goes with the next trigger.
            deliver_pending_history(root)
    if result.get("title_updated"):
        # The existing publisher rereads the current open manifest under the
        # admission -> checkpoint lock order. No checkpoint scan or payload
        # rehash is needed for a title-only change.
        artifact_cycle_titles.emit_after_checkpoint(root, {"cycle_id": result["cycle_id"]})
    if (kwargs.get("node_id") is None and result.get("layout") == "cycle"
            and kwargs.get("capability") == "autopilot-spec"):
        # The admission lock has been released. Standard+ seeds before a review
        # worker can write verdict.json. Direct has no preceding review worker;
        # its transaction selects the seed through the existing --spec-root.
        route = load_route(Path(root).resolve(), resolve_route_argument(Path(root).resolve(), Path(kwargs["route_file"])))
        if route.get("spec_touch") and route["effective_intensity"] != "direct":
            import importlib.util
            module_path = Path(__file__).with_name("spec-transaction.py")
            spec = importlib.util.spec_from_file_location("spec_transaction_preseed", module_path)
            transaction = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(transaction)
            transaction.preseed_owner_cycle(Path(root).resolve(), Path(result["cycle_dir"]), route=route)
    return result


def finalize_exact_cycle(root: Path, *, cycle_id: str, expected_binding: Mapping[str, Any],
                         state: str = "completed", **kwargs: Any) -> Dict[str, Any]:
    """Finalize one bound cycle under one admission lock, without root recovery."""
    root = Path(root).resolve()
    if expected_binding.get("kind") == "inline_producer_binding_v1":
        # Fast refusal before entering finalize; finalize repeats this *under*
        # its admission lock, so this precheck grants no write authority.
        _inline_producer_binding_check(root, cycle_id, expected_binding)
    _authorize_active_cleanup(root, "finalize-forward-recovery", root, cycle_id)
    # Enter public finalize with no lock held. It owns the one admission
    # boundary encompassing exact recovery, lease check and manifest commit.
    return finalize(root, cycle_id=cycle_id, state=state,
                    expected_binding=expected_binding, _recovery_scope="exact", **kwargs)


def verify_finalized_cycle(root: Path, *, cycle_id: str, expected_binding: Mapping[str, Any],
                           expected_manifest_digest: Optional[str] = None):
    """Read-only proof under admission lock; never repairs an unsealed cycle.

    A cycle edited or refreshed after it closed is still finalized (§45 D-127):
    the proof is the record, the current manifest and the index agreeing, not the
    files keeping their bytes.  `expected_manifest_digest` names the revision the
    caller recorded when it finished (see `_verify_sealed_cycle_locked`)."""
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
    try:
        if expected_binding.get("kind") == "inline_producer_binding_v1":
            _inline_producer_binding_check(root, cycle_id, expected_binding)
        if _live_review_lease(root, cycle_id) is not None:
            raise ProducerError("cycle-finalize-blocked-live-review", cycle_id)
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        if record.get("deleted_at"):
            return _verify_deleted_cycle(record, expected_binding, expected_manifest_digest)
        return _verify_sealed_cycle_locked(root, record, expected_binding,
                                           expected_manifest_digest=expected_manifest_digest)
    finally:
        artifact_admission._release_lock(root, lock_fd)


def recover(root: Path, *, now: Optional[float] = None) -> Dict[str, Any]:
    dispatch_terminal_commit.require_current_cleanup("root-recover")
    root = Path(root).resolve()
    # §45 D-126: what was moved or removed by hand is found first, and the folders are read before
    # the lock is taken (D-124).
    reconcile_root(root, now=now)
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        step1 = artifact_admission._recover_locked(root, now=now)
        producer = _recover_locked(root, now=now)
        locator_index = artifact_locator.verify_indexes(root, repair=True)
        # duplicate-copy: an unrelated campaign's defect stops only itself
        # (locator_index stays "problems", no repair write for it) and the
        # producer sweep's own isolated records (Step 3.9), never this call's
        # exit status -- `recover` keeps reporting success, typed, so a caller
        # retrying its own unrelated seal is not blocked by someone else's
        # hand-copied folder (Step 3.9b).
        status = ("recovered"
                  if not producer.get("unresolved") and locator_index.get("status") in {"current", "rebuilt"}
                  else "recovered-with-problems")
        return {"status": status, "admission": step1, "producer": producer, "locator_index": locator_index}
    finally:
        artifact_admission._release_lock(root, lock_fd)


# ---------------------------------------------------------------------------
# write policy (used by hooks and writers)
# ---------------------------------------------------------------------------


def _relative(root: Path, target: Path) -> Optional[str]:
    root = Path(root).resolve()
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    # Resolve the deepest existing ancestor so a not-yet-created target still
    # normalizes; the leaf is appended unchanged.
    probe = candidate
    tail: List[str] = []
    while not probe.exists() and probe.parent != probe:
        tail.insert(0, probe.name)
        probe = probe.parent
    resolved = probe.resolve()
    for part in tail:
        resolved = resolved / part
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return None


def _quick_refine_write_gate(root: Path, target: Path, route=None) -> None:
    relative = _relative(Path(root).resolve(), Path(target))
    if relative is None:
        return
    parts = relative.split("/")
    if parts[:1] == ["campaigns"]:
        index = 4 if len(parts) > 2 and parts[2] == "cycles" else 3
        if len(parts) <= index or parts[index] != "artifacts":
            return
        parts = parts[index + 1:]
    if len(parts) < 2 or parts[0] not in {"documents", "research"}:
        return
    if route is None:
        path = os.environ.get("AGENT_ROUTE_FILE") or os.environ.get("AGENT_OWNER_ROUTE_FILE")
        if not path:
            return  # The route/material guard independently requires a binding.
        route = _read_json(Path(path))
        if not isinstance(route, dict):
            raise ProducerError("inline-gate-route-unreadable")
    if route.get("capability") != "autopilot-refine":
        return
    scoped = (type(route.get("entry_scope_contract_version")) is int
              and route.get("entry_scope_contract_version") == 1
              and route.get("entry_execution_scope") in {"complete", "report"})
    if scoped and route.get("entry_execution_scope") == "report":
        raise ProducerError("legacy-top-level-write-denied", "report scope is read-only")
    if scoped and route.get("entry_execution_scope") == "complete":
        return
    if "_internal" in parts:
        return
    if route.get("effective_intensity") != "quick":
        return
    node = next((n for n in route.get("nodes", []) if n.get("id") == "one-shot"), {})
    if node.get("inline_human_gates") != ["preview-disposition"]:
        raise ProducerError("inline-gate-binding-missing")
    import workflow_state as WS
    try:
        WS.require_inline_gate_release(route, node, jobs=os.environ.get("AGENT_DISPATCH_JOBS") or None)
    except (WS.WorkflowStateError, OSError, ValueError) as exc:
        raise ProducerError("quick-preview-approval-required", str(exc)) from exc


def require_cycle_output(
    root: Path, target: Path, *, cycle_id: Optional[str] = None, route_id: Optional[str] = None,
) -> Optional[Path]:
    """Bind writes and completion evidence to the producer's issued cycle.

    Route lookup recovers omitted environment context from producer records;
    directory names, recency and a caller-supplied output path are not
    authority. When the sealed route file for `route_id` is readable, lookup
    and write admission both go through the lineage-aware D-120 path
    (`route_cycle_for` + `cycle_route_admission`), so a continuation may write
    the cycle its lineage opened. Only a genuinely missing canonical route
    file permits the legacy exact begin-route match, including an explicitly
    selected legacy cycle. Existing but unreadable or malformed proof refuses.
    """
    record = read_cycle_record(root, cycle_id) if cycle_id else None
    if cycle_id and record is None:
        raise ProducerError("cycle-unknown", cycle_id)
    lineage_checked = False
    route = None
    if route_id:
        if not isinstance(route_id, str) or not _ROUTE_ID_RE.fullmatch(route_id):
            raise ProducerError("route-lineage-unverified", f"route={route_id}")
        route_path = route_lineage.canonical_route_path(root, route_id)
        try:
            route_stat = route_path.lstat()
        except FileNotFoundError:
            route_stat = None
        except OSError as exc:
            raise ProducerError("route-lineage-unverified", f"route-unreadable={route_id}") from exc
        if (route_stat is not None and (not stat.S_ISREG(route_stat.st_mode)
                or route_path.is_symlink() or route_path.resolve() != route_path)):
            raise ProducerError("route-lineage-unverified", f"route-kind={route_id}")
        route = _read_json(route_path) if route_stat is not None else None
        if isinstance(route, dict) and route.get("route_id") == route_id:
            if record is None:
                record = route_cycle_for(root, route)
            if record is not None:
                admission = cycle_route_admission(root, record, route)
                if not admission.allow:
                    raise ProducerError(admission.reason, admission.detail)
                if cycle_id:
                    # An explicit selector must not bypass the shared lookup's
                    # refusal of multiple open cycles in the same lineage.
                    selected = route_cycle_for(root, route)
                    if selected is None or selected.get("cycle_id") != record.get("cycle_id"):
                        raise ProducerError("cycle-route-binding-mismatch", f"cycle={cycle_id} route={route_id}")
                lineage_checked = True
        elif route_stat is not None:
            # Existing but malformed proof is never a legacy missing route.
            raise ProducerError("route-lineage-unverified", f"route={route_id}")
        elif record is None:
            # Legacy records can lack a canonical route. Preserve only their
            # original exact begin-route match; continuation still needs proof.
            candidates = list_cycle_records(root, route_ids={route_id})
            opened = [item for item in candidates if item.get("state") == "open"]
            candidates = opened or candidates
            if len(candidates) > 1:
                raise ProducerError("route-cycle-binding-ambiguous", route_id)
            record = candidates[0] if candidates else None
    if record is None:
        return None
    if route_id and not lineage_checked and record.get("route_id") != route_id:
        raise ProducerError("cycle-route-binding-mismatch", f"cycle={record['cycle_id']} route={route_id}")
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    output = directory / "artifacts"
    try:
        relative = Path(target).resolve().relative_to(directory.resolve()).as_posix()
    except ValueError as exc:
        raise ProducerError("artifact-outside-bound-cycle", f"cycle={record['cycle_id']} output_dir={output}") from exc
    if _payload_root(relative) is None:
        raise ProducerError("artifact-outside-bound-cycle", f"cycle={record['cycle_id']} output_dir={output}")
    return output


def check_write(root: Path, target: Path) -> Dict[str, Any]:
    """Classify one prospective write under the artifact root.

    Returns {verdict: allow|deny, reason, layout, cutover, bucket, cycle_id}.
    """
    root = Path(root).resolve()
    rel = _relative(root, Path(target))
    active = is_active(root)
    base = {"cutover": "active" if active else "inactive", "target": str(target)}
    try:
        _quick_refine_write_gate(root, target)
    except ProducerError as exc:
        return {**base, "verdict": "deny", "reason": exc.code, "detail": exc.detail, "layout": "inline-gate"}
    try:
        _authorize_active_cleanup(root, "partial-report", Path(target), None)
    except ProducerError as exc:
        return {**base, "verdict": "deny", "reason": exc.code, "layout": "cleanup"}
    if rel is None:
        return {**base, "verdict": "allow", "reason": "outside-artifact-root", "layout": None}
    parts = rel.split("/")
    top = parts[0]
    if top.startswith(".") or top == "_scratch":
        return {**base, "verdict": "allow", "reason": "runtime-owned", "layout": "runtime"}
    if top == "shared":
        return {**base, "verdict": "deny", "reason": "shared-revision-immutable", "layout": "shared"}
    if top == "campaigns":
        try:
            locator_mapping, _rows = artifact_locator.scan_index(root)
            target_resolved = Path(target).resolve(strict=False)
            campaign_parts = rel.split("/")
            if len(campaign_parts) >= 3:
                cycle_relative = "/".join(campaign_parts[:3])
                cycle_id = next((identifier for identifier, path in locator_mapping.items()
                                 if path == cycle_relative), None)
                cycle_record = read_cycle_record(root, cycle_id) if cycle_id else None
                if cycle_record:
                    import inline_finish
                    pending = inline_finish.pending_for_cycle(root, cycle_id)
                    if pending and pending.get("state") != "finished":
                        return {**base, "verdict":"deny", "reason":"finish-in-progress",
                                "layout":"cycle", "cycle_id":cycle_id}
        except ProducerError as exc:
            return {**base, "verdict":"deny", "reason":exc.code, "detail":exc.detail, "layout":"cycle"}
        except (OSError, ValueError) as exc:
            return {**base, "verdict":"deny", "reason":"finish-state-unreadable", "detail":str(exc), "layout":"cycle"}
        try:
            require_cycle_output(
                root, target, cycle_id=os.environ.get("AGENT_ARTIFACT_CYCLE_ID"),
                # A node's active route is its write authority. The enclosing
                # owner must not mask a foreign or invalid child route.
                route_id=os.environ.get("AGENT_ROUTE_ID") or os.environ.get("AGENT_OWNER_ROUTE_ID"),
            )
        except ProducerError as exc:
            return {**base, "verdict": "deny", "reason": exc.code, "detail": exc.detail, "layout": "cycle"}
        legacy = len(parts) >= 5 and parts[2] == "cycles"
        readable = len(parts) >= 4 and parts[2] != "cycles"
        if not legacy and not readable:
            return {**base, "verdict": "deny", "reason": "campaign-record-machine-managed", "layout": "cycle"}
        artifacts_index = 4 if legacy else 3
        cycle_path = root.joinpath(*parts[:artifacts_index])
        campaign_path = root / "campaigns" / parts[1]
        campaign = _read_json(campaign_path / "campaign.json") or {}
        campaign_id = campaign.get("campaign_id")
        cycle_id = None
        record = None
        try:
            locator_mapping, _rows = artifact_locator.scan_index(root)
            cycle_relative = cycle_path.resolve().relative_to(root).as_posix()
            cycle_id = next(
                (identifier for identifier, path in locator_mapping.items() if path == cycle_relative),
                None,
            )
        except (artifact_locator.LocatorError, OSError, RuntimeError, ValueError):
            cycle_id = None
        if isinstance(cycle_id, str):
            record = read_cycle_record(root, cycle_id)
        manifest = _read_json(cycle_path / "manifest.json")
        manifest_cycle = manifest.get("cycle") if isinstance(manifest, dict) else None
        if cycle_id is None and isinstance(manifest_cycle, dict):
            cycle_id = manifest_cycle.get("cycle_id")
        if (len(parts) <= artifacts_index + 1
                or parts[artifacts_index] not in {"artifacts", "_internal"}):
            return {**base, "verdict": "deny", "reason": "outside-cycle-artifacts", "layout": "cycle",
                    "cycle_id": cycle_id}
        try:
            observed = os.lstat(target)
            node_kind = ("symlink" if stat.S_ISLNK(observed.st_mode) else
                         "regular" if stat.S_ISREG(observed.st_mode) else
                         "directory" if stat.S_ISDIR(observed.st_mode) else "special")
        except FileNotFoundError:
            node_kind = "missing"
        except OSError:
            node_kind = "special"
        classification = artifact_manifest.classify_artifact_path(
            str(root), campaign_path.relative_to(root).as_posix(),
            cycle_path.relative_to(root).as_posix(), "payload", rel, node_kind,
            prospective=True,
        )
        if not classification.allowed:
            reason = classification.reason or "outside-cycle-artifacts"
            return {**base, "verdict": "deny", "reason": reason,
                    "layout": "cycle", "cycle_id": cycle_id}
        if record is None:
            return {**base, "verdict": "deny", "reason": "cycle-unknown", "layout": "cycle", "cycle_id": cycle_id}
        # §45 D-123: a closed cycle takes writes like an open one; only where the
        # path is (above) and which cycle owns it (here) decide.
        bucket = (parts[artifacts_index + 1]
                  if parts[artifacts_index] == "artifacts" and len(parts) > artifacts_index + 2 else None)
        return {**base, "verdict": "allow", "reason": "open-cycle-artifacts", "layout": "cycle",
                "cycle_id": cycle_id, "campaign_id": campaign_id, "bucket": bucket,
                "output_dir": str(cycle_path / "artifacts")}
    if active:
        denial = {**base, "verdict": "deny", "reason": "legacy-top-level-write-denied", "layout": "legacy",
                  "bucket": top, "hint": LEGACY_WRITE_HINT}
        # Item 7: name where the caller's own cycle actually expects this write,
        # so a stage worker's error names its fix rather than just the refusal.
        # The reason token above stays exactly what it was (D-86: the fleet
        # cutover gate compares it verbatim).
        output_dir = os.environ.get("AGENT_ARTIFACT_OUTPUT_DIR")
        if output_dir:
            denial["expected_output_dir"] = output_dir
        return denial
    klass = classify_root(root)
    if klass["state"] == "malformed":
        # Same reason string as begin(). A damaged cutover record does not
        # slip out through an unmarked legacy allow (D-74: no unmarked allow
        # on any of the three surfaces).
        return {**base, "verdict": "deny", "reason": "cutover-record-malformed",
                "layout": "legacy", "bucket": top, "detail": klass["reason"]}
    fallback = legacy_fallback_state(root, classification=klass)
    if _fallback_blocks(fallback):
        return {**base, "verdict": "deny", "reason": "cutover-inactive-fallback-denied",
                "layout": "legacy", "bucket": top, "legacy_fallback": fallback}
    result = {**base, "verdict": "allow", "reason": "legacy-compat-window", "layout": "legacy", "bucket": top}
    if fallback is not None:
        result["legacy_fallback"] = fallback
    return result


def cycle_bucket(root: Path, target: Path) -> Optional[Tuple[str, str]]:
    """Return (bucket, cycle_id) for a path inside a cycle's artifacts."""
    rel = _relative(Path(root), Path(target))
    if rel is None:
        return None
    parts = rel.split("/")
    if parts[0] != "campaigns":
        return None
    legacy = len(parts) >= 7 and parts[2] == "cycles" and parts[4] == "artifacts"
    readable = len(parts) >= 6 and parts[2] != "cycles" and parts[3] == "artifacts"
    if not legacy and not readable:
        return None
    bucket_index = 5 if legacy else 4
    artifacts_index = 4 if legacy else 3
    cycle_path = Path(root).resolve().joinpath(*parts[:artifacts_index])
    try:
        mapping, _rows = artifact_locator.scan_index(Path(root))
        relative = cycle_path.resolve().relative_to(Path(root).resolve()).as_posix()
    except (artifact_locator.LocatorError, OSError, RuntimeError, ValueError):
        return None
    for cycle_id, path in mapping.items():
        if path != relative:
            continue
        if read_cycle_record(root, cycle_id):
            return parts[bucket_index], cycle_id
    return None


def resolve_output_dir(root: Path, bucket: str, *, cycle_dir_hint: Optional[str] = None) -> Tuple[Path, str]:
    """Where a writer must place `<bucket>/...` output: cycle layout or legacy."""
    root = Path(root).resolve()
    _authorize_active_cleanup(root, "partial-report", root, os.environ.get("AGENT_ARTIFACT_CYCLE_ID"))
    hint = cycle_dir_hint or os.environ.get("AGENT_ARTIFACT_CYCLE_DIR")
    if hint:
        directory = Path(hint)
        verdict = check_write(root, directory / "artifacts" / bucket / "probe")
        if verdict["verdict"] != "allow":
            raise ProducerError(verdict["reason"], str(directory))
        return directory / "artifacts" / bucket, "cycle"
    if is_active(root):
        raise ProducerError("legacy-top-level-write-denied", f"{bucket}: {LEGACY_WRITE_HINT}")
    klass = classify_root(root)
    if klass["state"] == "malformed":
        raise ProducerError("cutover-record-malformed", klass["reason"] or bucket)
    fallback = legacy_fallback_state(root, classification=klass)
    if _fallback_blocks(fallback):
        raise ProducerError("cutover-inactive-fallback-denied", bucket)
    return root / bucket, "legacy"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _route_autoclose(root: Path, trigger: str, campaign: Optional[str] = None) -> None:
    """Close routes nobody works on before campaign bookkeeping reads them
    (route_autoclose.py).  Bookkeeping only: it never fails the command.
    `campaign` limits `campaign-close` bookkeeping to that campaign."""
    try:
        import artifact_cutover
        import route_autoclose
        campaign_id = None
        scope = {}
        if campaign is not None:
            path = artifact_campaign.campaign_path(root, campaign)
            record, _raw = artifact_campaign.read_json(root, path)
            campaign_id = record.get("campaign_id")
            scope = {"scope_campaign_id": campaign_id, "scope_dir": path.parent,
                     "scope_key": record.get("key")}
        route_autoclose.report(route_autoclose.sweep(
            root, api=artifact_cutover._route_module(), trigger=trigger,
            campaign_id=campaign_id, **scope))
    except Exception as exc:  # noqa: BLE001
        print(f"route_autoclose error={type(exc).__name__}", file=sys.stderr)


def _print(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True))


def _route_cli_context(argv: Sequence[str]):
    values = list(argv)
    command = values[0] if values else ""
    if command not in {"begin", "resolve-output"}:
        return values, None
    if not (os.environ.get("AGENT_ROUTE_FILE") or os.environ.get("AGENT_ROUTE_ID")):
        return values, None
    def option_value(name):
        for index, value in enumerate(values):
            if value == name and index + 1 < len(values):
                return values[index + 1]
            if value.startswith(name + "="):
                return value.split("=", 1)[1]
        return None
    explicit_root = option_value("--artifact-root")
    explicit_route = option_value("--route") if command == "begin" else None
    try:
        import artifact_context
        context = artifact_context.resolve_context(
            environ=os.environ, route=explicit_route,
            node_id=option_value("--node"),
            explicit={"artifact_root": explicit_root} if explicit_root else None,
        )
    except Exception as exc:
        if getattr(exc, "code", None):
            raise ProducerError(exc.code, getattr(exc, "detail", str(exc))) from exc
        raise
    if not context.route or not context.artifact_root:
        return values, None
    supplied_node = option_value("--node")
    env_node = os.environ.get("AGENT_ROUTE_NODE")
    if supplied_node and env_node and supplied_node != env_node:
        raise ProducerError("route-node-mismatch", supplied_node)
    defaults = {"--artifact-root": str(context.artifact_root)}
    if command == "begin":
        defaults["--route"] = str(context.artifact_root / ".runtime" / "routes"
                                  / f"{context.route['route_id']}.json")
        if context.node_id:
            defaults["--node"] = context.node_id
    insertion = 1
    for flag, value in defaults.items():
        if option_value(flag) is None:
            values[insertion:insertion] = [flag, value]
            insertion += 2
    return values, context


def _checkpoint_cli(args: argparse.Namespace) -> Dict[str, Any]:
    route_file: Optional[Path] = None
    root_value = args.artifact_root or os.environ.get("AGENT_ARTIFACT_ROOT") or ""
    if args.route:
        route_file = Path(args.route)
        if not root_value:
            raw = _read_json(route_file)
            root_value = str((raw or {}).get("artifact_root") or "")
    cycle_id = args.cycle or (None if args.route else os.environ.get("AGENT_ARTIFACT_CYCLE_ID") or None)
    if not root_value:
        raise ProducerError("checkpoint-target-required", "--artifact-root or $AGENT_ARTIFACT_ROOT")
    root = Path(root_value)
    if route_file is not None:
        route_file = resolve_route_argument(root, route_file)
    return checkpoint(root, cycle_id=cycle_id, route_file=route_file, trigger=args.trigger)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0], allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True,
                                parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))

    p = sub.add_parser("activate")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--repository-id", required=True)
    p.add_argument("--artifact-root-id", required=True)
    p.add_argument("--w7-campaign-id")
    p.add_argument("--w7-cycle-id")
    p.add_argument("--w7-handoff-sha256")
    p.add_argument("--w7-map-sha256")
    p.add_argument("--w7-shared", action="append", default=[], help="kind=ref_id:rrev_id")
    p.add_argument("--approval-receipt-sha256")

    p = sub.add_parser("status")
    p.add_argument("--artifact-root", required=True)

    p = sub.add_parser("campaign-list", help="summarize the root's campaigns (keys, titles, cycle counts)")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--all-states", action="store_true", help="include satisfied/superseded campaigns")

    p = sub.add_parser("campaign-export", help="pure read of every campaign's current state (artifact-campaign-current/v1)")
    p.add_argument("--artifact-root", required=True)

    p = sub.add_parser("cycle-binding-backfill",
                       help="add started_on to .cycle.json bindings written before the field existed")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--apply", action="store_true", help="write the bindings (default: dry run)")

    p = sub.add_parser("cycle-time-recovery",
                       help="recover migrated cycles' start times from the retirement backup's original mtimes")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--backup-store", help="retirement store (default: $XDG_STATE_HOME/hearting/artifact-retirement)")
    p.add_argument("--apply", action="store_true", help="write recovered_started_on into records (default: dry run)")

    p = sub.add_parser("cycle-display-titles-backfill",
                       help="declare cycle display titles for Cairn from sealed cycles (default: dry run)")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--apply", action="store_true", help="write the declaration (default: dry run)")
    p.add_argument("--report", help="write a markdown report to this path")
    p.add_argument("--out", help="write the candidate declaration bytes to this path (outside the artifact root)")
    p.add_argument("--reader-dir", help="Cairn app_dir to gate --apply against (default: ~/.config/cairn-sync/config.json)")
    p.add_argument("--expect-post-digest", help="refuse --apply unless the computed post-digest matches")
    p.add_argument("--restore-journal", help="restore the declaration from a prior --apply's journal")

    for command in ("campaign-status", "campaign-close", "campaign-reopen", "campaign-recover"):
        p = sub.add_parser(command, help="inspect, close, reopen, or recover a campaign")
        p.add_argument("--artifact-root", required=True)
        p.add_argument("--campaign", required=True, help="campaign ID or campaign.json path")
        if command in {"campaign-close", "campaign-reopen"}:
            p.add_argument("--reason")

    p = sub.add_parser("cycle-move", help="move cycles, merge campaigns or attach logs (keeps IDs)")
    p.add_argument("--artifact-root", required=True)
    selector = p.add_mutually_exclusive_group(required=True)
    selector.add_argument("--cycle")
    selector.add_argument("--source-campaign")
    p.add_argument("--target-artifact-root")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--attach-logs", action="append", default=[])
    p.add_argument("--campaign", help="target campaign: its ID or its key")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--parent", help="the cycle this one follows")
    group.add_argument("--no-parent", action="store_true", help="take the parent link off")
    p.add_argument("--reason")

    p = sub.add_parser("cycle-mark", help="mark a cycle discarded or superseded, take the mark off, "
                                          "or name the document that stands for it")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle", required=True)
    modes = p.add_mutually_exclusive_group(required=True)
    modes.add_argument("--discard", action="store_true")
    modes.add_argument("--superseded-by", help="comma-separated IDs of the cycles that replace this one")
    modes.add_argument("--clear", action="store_true", help="take the mark off")
    modes.add_argument("--primary", help="cycle-relative path (artifacts/...) of the document that stands for the cycle")
    p.add_argument("--reason")

    p = sub.add_parser("delete", help="delete a cycle or a whole campaign (the records and preserved copies stay)")
    p.add_argument("--artifact-root", required=True)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--cycle")
    target.add_argument("--campaign", help="campaign ID or key")
    p.add_argument("--reason")

    p = sub.add_parser("begin")
    p.add_argument("--artifact-root", required=True)
    p.add_argument(
        "--route",
        required=True,
        help="path to the route file, or a bare route id resolved under the artifact root",
    )
    p.add_argument("--node", default=None)
    p.add_argument("--capability", required=True)
    p.add_argument("--intensity", required=True)
    p.add_argument("--campaign", help="existing campaign id to add this cycle to")
    p.add_argument("--campaign-key",
                   help="the work stream this cycle belongs to; reuses the active "
                        "campaign holding that key. Defaults to the sealed route's key. "
                        "With no campaign/key/parent selection, uses the root's "
                        "_unassigned campaign and reports degraded=true. Size: a stream with a "
                        "one-sentence closing condition — not a project name, not a one-cycle task")
    p.add_argument("--title")
    p.add_argument("--goal")
    p.add_argument("--parent-cycle",
                   help="open or sealed predecessor; inherits its campaign and records "
                        "causality, never input or completion approval")
    p.add_argument("--workflow-group-id", help="explicit existing group for this campaign; "
                   "the same-campaign producer context is inherited when omitted")
    p.add_argument("--workflow-stage-label", help="display label for an explicitly grouped cycle")
    p.add_argument("--require-cycle", action="store_true")
    p.add_argument("--shared-reference", action="append", default=[],
                   help="<kind>:<ref>:<rrev>[:<content_digest>], repeatable")
    p.add_argument("--env-file", help="write KEY=VALUE lines for the producer environment")

    p = sub.add_parser("finalize")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle", required=True)
    p.add_argument("--state", default="completed", choices=["completed", "abandoned"])
    p.add_argument("--primary", help="primary artifact as a cycle-relative locator "
                   "(artifacts/<file> or artifacts/<folder>/<file>); an absolute path inside this cycle's artifacts/ is accepted")
    p.add_argument("--publication", default="not-offered")
    p.add_argument("--allow-open-route", action="store_true")
    p.add_argument("--adopt-root-output", action="append", default=[])
    p.add_argument("--abandon-reason", choices=sorted(ABANDON_REASONS))
    p.add_argument("--force-abandon-ignoring-lease", action="store_true")

    p = sub.add_parser("checkpoint",
                       help="publish an open cycle's interim manifest (the sealed schema with "
                            "cycle.state=open) for readers such as Cairn; rate-limited per cycle")
    p.add_argument("--artifact-root",
                   help="default: $AGENT_ARTIFACT_ROOT, else the route file's artifact_root")
    p.add_argument("--cycle", help="default: $AGENT_ARTIFACT_CYCLE_ID when --route is absent")
    p.add_argument("--route", help="route file or bare route id; selects the route's one open cycle")
    p.add_argument("--trigger", default="explicit", choices=CHECKPOINT_TRIGGERS)

    p = sub.add_parser("review-lease")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle", required=True)
    p.add_argument("--attempt")
    p.add_argument("--deadline-seconds", type=float, default=900.0)
    p.add_argument("operation", choices=["acquire", "release", "status"])

    p = sub.add_parser("admit-shared")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--reference")
    p.add_argument("--key")
    p.add_argument("--title")
    p.add_argument("--promote-research", action="store_true")
    p.add_argument("--promotion-evidence")
    p.add_argument("--drop-component", action="append", default=[], metavar="NAME",
                   help="D-87 (b): drop this top-level component from the reference "
                        "(repeatable); the only way to shrink the component set")
    p.add_argument("--drop-reason", help="reason recorded with --drop-component")
    p.add_argument("--base-revision", help="actual base revision of an unseeded spec, or none for initial publication")
    p.add_argument("--new-reference", action="store_true",
                   help="allow a second reference of a canonical-singular kind (spec) when neither --reference nor --key matches")

    p = sub.add_parser("check-components")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--reference", required=True)
    p.add_argument("--from-revision", help="first revision of the inspected window")
    p.add_argument("--to-revision", help="last revision of the inspected window")

    p = sub.add_parser("recover")
    p.add_argument("--artifact-root", required=True)

    p = sub.add_parser("check-write")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--file", required=True)

    p = sub.add_parser("resolve-output")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--cycle-dir")

    try:
        prepared_argv, _route_context = _route_cli_context(sys.argv[1:] if argv is None else argv)
    except ProducerError as exc:
        _print({"status": "blocked", "reason": exc.code, "detail": exc.detail})
        return BLOCKED
    args = parser.parse_args(prepared_argv)
    try:
        if args.command not in {"check-write"}:
            dispatch_terminal_commit.require_current_cleanup("producer-" + args.command)
        if args.command == "checkpoint":
            _print(_checkpoint_cli(args))
            return OK
        root = Path(args.artifact_root)
        if args.command == "activate":
            w7: Dict[str, Any] = {}
            if args.w7_campaign_id:
                w7["campaign_id"] = args.w7_campaign_id
            if args.w7_cycle_id:
                w7["cycle_id"] = args.w7_cycle_id
            if args.w7_handoff_sha256:
                w7["handoff_sha256"] = args.w7_handoff_sha256
            if args.w7_map_sha256:
                w7["compatibility_map_sha256"] = args.w7_map_sha256
            shared: Dict[str, Any] = {}
            for row in args.w7_shared:
                kind, _, ids = row.partition("=")
                ref, _, rrev = ids.partition(":")
                shared[kind] = {"shared_reference_id": ref, "shared_reference_revision_id": rrev}
            if shared:
                w7["shared"] = shared
            result = activate(root, repository_id=args.repository_id, artifact_root_id=args.artifact_root_id,
                              w7=w7, approval_receipt_sha256=args.approval_receipt_sha256)
        elif args.command == "status":
            result = status(root)
        elif args.command == "campaign-list":
            rows = list_campaign_summaries(root, active_only=not args.all_states)
            result = {"status": "ok", "artifact_root": str(Path(root).resolve()), "campaigns": rows}
        elif args.command == "campaign-export":
            # Pure read: captured bytes, no writes or locks; only an
            # OS-unreadable root is exit 65, never a content verdict.
            try:
                result = artifact_campaign.export_current(root)
            except OSError as exc:
                _print({"status": "blocked", "code": "root-unreadable", "detail": str(exc)[:200]})
                return 65
        elif args.command == "cycle-binding-backfill":
            result = backfill_cycle_bindings(root, apply=args.apply)
        elif args.command == "cycle-time-recovery":
            result = recover_cycle_times(root, apply=args.apply,
                                         backup_store=Path(args.backup_store) if args.backup_store else None)
        elif args.command == "cycle-display-titles-backfill":
            if args.restore_journal:
                result = artifact_cycle_titles.restore_backfill(root, Path(args.restore_journal))
            else:
                result = artifact_cycle_titles.backfill(
                    root, apply=args.apply,
                    report_path=Path(args.report) if args.report else None,
                    out_path=Path(args.out) if args.out else None,
                    reader_dir=Path(args.reader_dir) if args.reader_dir else None,
                    expect_post_digest=args.expect_post_digest,
                )
            _print(result)
            return BLOCKED if str(result.get("status", "")).startswith("refused") else OK
        elif args.command == "campaign-status":
            import artifact_cross_root_move as cross_move
            try:
                historical_path, historical = cross_move._campaign(
                    root, args.campaign,
                    read_record=lambda path: artifact_campaign.read_json(root, path)[0])
            except ProducerError:
                historical = {}
            if historical.get("relocation"):
                result = {"status": historical["state"], "state": historical["state"],
                          "campaign_id": historical["campaign_id"], "canonical": historical["relocation"]}
            else:
                # Pure read: no route sweep, no reconcile, no history flush.
                # Writer paths (`campaign-close`, `compose`) keep the sweep.
                result = artifact_campaign.status(root, args.campaign)
        elif args.command == "campaign-close":
            _route_autoclose(root, "campaign-close", campaign=args.campaign)
            reconcile_root(root)
            result = artifact_campaign.close(root, args.campaign, reason=args.reason)
        elif args.command == "campaign-reopen":
            result = artifact_campaign.reopen(root, args.campaign, reason=args.reason)
        elif args.command == "campaign-recover":
            result = artifact_campaign.recover(root, args.campaign)
        elif args.command == "cycle-move":
            result = cycle_move(root, args.cycle, campaign=args.campaign, parent=args.parent,
                                no_parent=args.no_parent, reason=args.reason, target_artifact_root=args.target_artifact_root,
                                source_campaign=args.source_campaign, attach_logs=args.attach_logs, dry_run=args.dry_run)
        elif args.command == "cycle-mark":
            result = cycle_mark(
                root, args.cycle, discard=args.discard,
                superseded_by=[v.strip() for v in args.superseded_by.split(",") if v.strip()]
                if args.superseded_by is not None else None,
                clear=args.clear, primary=args.primary, reason=args.reason)
        elif args.command == "delete":
            result = (delete_cycle(root, args.cycle, reason=args.reason) if args.cycle
                      else delete_campaign(root, args.campaign, reason=args.reason))
        elif args.command == "begin":
            pins: List[Dict[str, Any]] = []
            for row in args.shared_reference:
                parts = row.split(":", 3)
                if len(parts) < 3:
                    raise ProducerError("shared-reference-pin-invalid", row)
                kind, ref_id, rrev_id = parts[0], parts[1], parts[2]
                pin: Dict[str, Any] = {
                    "kind": kind, "shared_reference_id": ref_id, "shared_reference_revision_id": rrev_id,
                }
                if len(parts) > 3:
                    pin["content_digest"] = parts[3]
                pins.append(pin)
            result = begin(root, route_file=Path(args.route), capability=args.capability,
                           intensity=args.intensity, node_id=args.node, campaign_id=args.campaign,
                           campaign_key=args.campaign_key, title=args.title, goal=args.goal,
                           parent_cycle_id=args.parent_cycle,
                           workflow_group_id=args.workflow_group_id,
                           workflow_stage_label=args.workflow_stage_label,
                           require_cycle=args.require_cycle,
                           shared_reference_pins=pins or None)
            if args.env_file:
                lines = "".join(f"{k}={v}\n" for k, v in result.get("env", {}).items())
                Path(args.env_file).write_text(lines, encoding="utf-8")
        elif args.command == "finalize":
            result = finalize(root, cycle_id=args.cycle, state=args.state, primary=args.primary,
                              publication=args.publication, allow_open_route=args.allow_open_route,
                              adopt_root_outputs=args.adopt_root_output,
                              abandon_reason=args.abandon_reason,
                              force_abandon_ignoring_lease=args.force_abandon_ignoring_lease)
            if result.get("warning"):
                print(result["warning"], file=sys.stderr)
            record = read_cycle_record(root, args.cycle)
            if record is not None and record.get("capability") in {"autopilot-spec", "autopilot-code"}:
                # Legacy normal completion/recovery has no owner envelope.
                # Finalize has released its lock before existing admission runs.
                publication = completed_spec_publication(root, cycle_id=args.cycle, settle=True)
                if publication.get("status") != "not-applicable":
                    result["shared_publication"] = publication
        elif args.command == "review-lease":
            if args.operation == "acquire":
                if not args.attempt:
                    raise ProducerError("review-lease-attempt-required")
                result = review_lease_acquire(root, cycle_id=args.cycle, attempt_id=args.attempt,
                                              deadline_seconds=args.deadline_seconds)
            elif args.operation == "release":
                if not args.attempt:
                    raise ProducerError("review-lease-attempt-required")
                result = review_lease_release(root, cycle_id=args.cycle, attempt_id=args.attempt)
            else:
                result = review_lease_status(root, cycle_id=args.cycle, attempt_id=args.attempt)
        elif args.command == "admit-shared":
            result = admit_shared(root, cycle_id=args.cycle, kind=args.kind, source=args.source,
                                  reference_id=args.reference, key=args.key, title=args.title,
                                  promote_research=args.promote_research,
                                  promotion_evidence=args.promotion_evidence,
                                  drop_components=args.drop_component,
                                  drop_reason=args.drop_reason,
                                  allow_new_reference=args.new_reference, base_revision=args.base_revision)
        elif args.command == "check-components":
            result = check_component_sets(root, args.kind, args.reference,
                                          from_revision=args.from_revision,
                                          to_revision=args.to_revision)
            _print(result)
            return OK if result["violations"] == 0 else BLOCKED
        elif args.command == "recover":
            result = recover(root)
        elif args.command == "check-write":
            result = check_write(root, Path(args.file))
            _print(result)
            return OK if result["verdict"] == "allow" else BLOCKED
        elif args.command == "resolve-output":
            if _route_context and _route_context.node:
                if args.cycle_dir and Path(args.cycle_dir).resolve(strict=False) != _route_context.cycle_dir:
                    raise ProducerError("artifact-cycle-binding-mismatch", args.cycle_dir)
                try:
                    binding = _route_context.resolve_bucket(args.bucket)
                except Exception as exc:
                    raise ProducerError(getattr(exc, "code", "artifact-output-role-missing"), str(exc)) from exc
                directory, layout = binding.path, "cycle"
            else:
                directory, layout = resolve_output_dir(root, args.bucket, cycle_dir_hint=args.cycle_dir)
            result = {"status": "ok", "output_dir": str(directory), "layout": layout}
            if layout == "legacy":
                fallback = legacy_fallback_state(root)
                if fallback is not None:
                    result["legacy_fallback"] = fallback
        else:  # pragma: no cover
            parser.error("unknown command")
            return USAGE
    except ProducerError as exc:
        _print({"status": "blocked", "reason": exc.code, "detail": exc.detail})
        return BLOCKED
    except artifact_cycle_titles.CycleTitlesError as exc:
        _print({"status": "blocked", "reason": exc.code, "detail": exc.detail})
        return BLOCKED
    except artifact_admission.AdmissionBusy as exc:
        _print({"status": "blocked", "reason": "admission-busy", "detail": str(exc)})
        return BLOCKED
    except artifact_admission.AdmissionRecoveryRequired as exc:
        _print({"status": "blocked", "reason": "recovery-required", "detail": str(exc)})
        return BLOCKED
    except (artifact_lifecycle.LifecycleError, artifact_campaign.CampaignError) as exc:
        _print({"status": "blocked", "reason": exc.code, "detail": exc.detail})
        return BLOCKED
    except (OSError, ValueError) as exc:
        _print({"status": "blocked", "reason": "request-invalid", "detail": str(exc)})
        return BLOCKED
    _print(result)
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
