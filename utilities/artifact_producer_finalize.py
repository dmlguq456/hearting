"""Artifact producer: finalize, sealed-cycle verification and recovery.

`finalize`, deleted- and exact-cycle recovery, the sealed-cycle verifier,
`observe_finalized_cycle`, and root-wide journal recovery.

Split out of `artifact_producer.py`, which re-exports every name defined here.

`finalize_exact_cycle` and `verify_finalized_cycle` stay defined in
`artifact_producer.py`: `dispatch_runtime_support.probe_runtime` reads them
there by AST in every runtime root, including from older releases.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import artifact_admission
import artifact_identity
import artifact_index
import artifact_lifecycle
import artifact_locator
import artifact_manifest
import route_identity
import route_lineage
import dispatch_lock_order
import dispatch_terminal_commit
from runtime_observation import stable_read_bytes

from artifact_producer_store import (
    _artifact_index_observation, _canonical, _closed_record, _digest, _earlier_documents,
    _folder_cycle_id, _HISTORICAL_OBSERVATIONS, _HISTORICAL_PROJECTIONS,
    _published_cycle_state, _read_json, _record_cycle_manifest_path, _reference_path, _rfc3339,
    _stream_file_facts, _write_cycle_record, _write_exclusive, ABANDON_REASONS, cycle_dir,
    cycle_record_path, journal_path, list_cycle_records, producer_dir, ProducerError,
    PROVISIONAL_SEAL_WARNING, read_cycle_record,
)

from artifact_producer_routes import (
    _bind_cycle_route_locked, _finalize_route, _inline_producer_binding_check, _remove_journal,
    _write_journal, cycle_route_admission, load_route, resolve_cycle_manifest_route,
    route_is_closed,
)

from artifact_producer_history import (
    _observe_control_changes,
)

from artifact_producer_interim import (
    _checkpoint_lock, _remove_empty_cycle, _sweep_orphan_interims, _without_event_reuse,
    CHECKPOINT_FINALIZE_LOCK_SECONDS, read_interim_reservation, remove_interim,
)

from artifact_producer_manifest import (
    _bundle_exact_evidence_facts, _cycle_relative_primary, _output_paths, _output_placements,
    _placed_locator, _publish_bundle_descriptors, _scan_directory_bundles, build_manifest,
    output_invalid_detail,
)

from artifact_producer_lease import (
    _is_v2_review_lease, _live_review_lease, _path_entry_present, _raise_if_recovery_fenced,
)

from artifact_producer_refresh import (
    _binding_route_unrecorded, _commit_sealed, _drop_unpublished_snapshot,
    _keep_prescan_digests, _Prescan, _prescan_cycle, _prescan_misses, _refresh_cycle_locked,
    _refresh_journal_resume, _refresh_lock, _reread_misses, _StaleScan,
)

from artifact_producer_spec import (
    _commit_shared, _validate_shared_journal,
)


def _authorize_active_cleanup(root: Path, operation: str, target: Path, cycle_id: Optional[str]) -> None:
    try:
        dispatch_terminal_commit.require_current_cleanup(operation, target=target, cycle_id=cycle_id)
    except dispatch_terminal_commit.TerminalCommitError as exc:
        raise ProducerError("cleanup-scope-violation", exc.detail) from exc


def _finalize(
    root: Path,
    *,
    cycle_id: str,
    state: str = "completed",
    primary: Optional[str] = None,
    publication: str = "not-offered",
    allow_open_route: bool = False,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    now: Optional[float] = None,
    crash_after_manifest: bool = False,
    exclude_hidden: bool = False,
    adopt_root_outputs: Sequence[str] = (),
    abandon_reason: Optional[str] = None,
    force_abandon_ignoring_lease: bool = False,
    support_locators: Sequence[str] = (),
    expected_binding: Optional[Mapping[str, Any]] = None,
    jobs: Optional[Path] = None,
    lock_timeout: Optional[float] = None,
    exclude_symlinks: bool = False,
    _admission_lock_fd: Optional[int] = None,
    _recovery_scope: str = "root",
    _prescan: Optional[_Prescan] = None,
    _last_try: bool = False,
    _scan_budget: Optional["RefreshBudget"] = None,
) -> Dict[str, Any]:
    """`lock_timeout` bounds the lock waits (default: the refresh, admission and
    checkpoint defaults); a background sweep passes 0 so a held lock defers it.
    The first close of an open cycle reads its files before the admission lock; `_scan_budget`
    (a background sweep's time) lets it stop between files and answer `deferred`.
    Symbolic links are always left out of the manifest and recorded as
    `excluded_symlinks` on the result and the cycle record (`exclude_symlinks`
    is accepted for old callers and changes nothing)."""
    root = Path(root).resolve()
    jobs = Path(jobs) if jobs is not None else (
        Path(os.environ["AGENT_DISPATCH_JOBS"]) if os.environ.get("AGENT_DISPATCH_JOBS") else None)
    if _recovery_scope != "exact":
        dispatch_terminal_commit.require_current_cleanup("producer-finalize")
    _authorize_active_cleanup(root, "finalize-forward-recovery", root, cycle_id)
    if expected_binding is None:
        _observe_control_changes(root, cycle_id, now=now)
    if state not in {"completed", "abandoned"}:
        raise ProducerError("finalize-state-invalid", state)
    if _admission_lock_fd is not None:
        raise ProducerError("finalize-reentry-forbidden", cycle_id)
    # PRD §13.53.4(3) names the producer admission mutex as the lock that must
    # be released before `finalize()` is entered. The check above only catches
    # a caller that *passes* its fd; a caller holding the lock in its own
    # variable would otherwise block on `flock` for the full admission timeout
    # and surface as "busy". Refuse it here, typed, at the boundary.
    try:
        dispatch_lock_order.assert_not_held("producer-admission", "producer-finalize")
    except dispatch_lock_order.LockOrderError as error:
        raise ProducerError("finalize-reentry-forbidden", error.detail or cycle_id) from error
    alloc = allocator or artifact_identity.IdAllocator()
    # A cycle's files are read here, before the admission lock: a closed cycle closed again is
    # compared with its manifest, an open one is read for its first close (named files being
    # adopted are moved under the lock, so that close reads in it).  Under the lock only `lstat`
    # has to confirm what was read (§45 D-124); a scan found out of date is read once more
    # (`_prescan` on that second call).
    prescan = _prescan if _last_try else _prescan_cycle(
        root, cycle_id, unless_recorded=expected_binding, include_open=True,
        exclude_hidden=exclude_hidden, budget=_scan_budget)
    lock_fd = _admission_lock_fd
    interim_guard = contextlib.ExitStack()
    sweep_unresolved: List[Dict[str, Any]] = []
    sealed: Optional[Dict[str, Any]] = None
    kept_scan: Optional[_Prescan] = None

    def _finish(payload: Dict[str, Any]) -> Dict[str, Any]:
        # duplicate-copy: an unrelated campaign the recovery sweep had to
        # isolate (§3.6a) is surfaced on every return, not swallowed.
        if sweep_unresolved:
            payload = dict(payload)
            payload["recovery_unresolved"] = sweep_unresolved
        return payload

    try:
        if prescan is not None and prescan.open_cycle:
            # First close.  The cycle's own refresh lock (before the admission lock, as a refresh
            # takes them) holds off a second close of it while `lstat` confirms what was read; the
            # admission lock then covers only the short write section below.  A read that found
            # invalid names stopped there and read no file (`_prescan_open_cycle`), so no name it
            # listed is `lstat`-confirmed here: the `output-invalid` refusal rests on that listing,
            # and a name fixed after it is seen by the next close.
            if not prescan.complete:
                _keep_prescan_digests(root, cycle_id, prescan)
                return {"status": "deferred", "reason": "scan-budget", "cycle_id": cycle_id}
            interim_guard.enter_context(_refresh_lock(
                root, cycle_id, timeout=CHECKPOINT_FINALIZE_LOCK_SECONDS if lock_timeout is None else lock_timeout))
            misses = _prescan_misses(prescan.directory, prescan, prescan.paths)
            if misses and not _last_try:
                raise _StaleScan(cycle_id)
            if "@marker-selection" in misses:
                _keep_prescan_digests(root, cycle_id, prescan)
                return {"status": "deferred", "reason": "files-changed-during-read", "cycle_id": cycle_id}
            if misses and _scan_budget is not None:
                _keep_prescan_digests(root, cycle_id, prescan)
                return {"status": "deferred", "reason": "files-changed-during-read", "cycle_id": cycle_id}
            if misses:
                _reread_misses(prescan, misses)
        # The root sweep's look at every open cycle is taken before the lock (§45 D-124); under
        # it only the cycles that look has work for, and this one, are read again.
        open_candidates = _recovery_open_candidates(root, cycle_id) if _recovery_scope == "root" else None
        if lock_fd is None:
            lock_fd = artifact_admission._acquire_lock(
                root, artifact_admission.LOCK_TIMEOUT_DEFAULT if lock_timeout is None else lock_timeout, now=now)
        if (expected_binding and isinstance(expected_binding, Mapping)
                and expected_binding.get("kind") == "inline_producer_binding_v1"):
            # The admission mutex is held. Check the actual route's verified
            # lineage, including valid continuations of the begin route.
            _inline_producer_binding_check(root, cycle_id, expected_binding)
        import inline_finish
        finish = inline_finish.pending_for_cycle(root, cycle_id)
        if finish and finish.get("state") != "finished":
            permitted = (
                isinstance(expected_binding, Mapping)
                and expected_binding.get("kind") == "inline_producer_binding_v1"
                and expected_binding.get("inline_finish_id") == finish.get("inline_finish_id")
                and finish.get("state") == "route-closed"
            )
            if not permitted:
                raise ProducerError("finish-in-progress", cycle_id)
        if _recovery_scope == "root":
            pre = read_cycle_record(root, cycle_id)
            sweep = _recover_locked(root, now=now,
                                    target_campaign_id=pre["campaign_id"] if pre else None,
                                    open_candidates=open_candidates)
            sweep_unresolved = sweep.get("unresolved", [])
        elif _recovery_scope == "exact":
            _recover_exact_cycle_locked(root, cycle_id, expected_binding, now=now)
        else:
            raise ProducerError("recovery-scope-invalid", _recovery_scope)
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        if record.get("deleted_at"):
            return _finish(_finalize_deleted_locked(root, record, state=state, now=now,
                                                    abandon_reason=abandon_reason))
        if record.get("state") == "sealed":
            # Storage sealing is not task completion: the manifest commit is an
            # immutable snapshot, and the *published* cycle state
            # (`_published_cycle_state`) is the only thing a re-finalize
            # request can be judged against. Any request whose `state` does
            # not match that published state is a conflict -- no flag makes
            # it idempotent, because the storage schema does not persist the
            # request fingerprint needed to prove "same retry" (D-8).
            published = _published_cycle_state(root, record)
            if published != state:
                if published in {"active", "abandoned"} and state == "completed":
                    if published == "abandoned" and expected_binding is None:
                        raise ProducerError(
                            "finalize-state-conflict",
                            f"{cycle_id}: requested=completed published_cycle_state=abandoned storage_state=sealed",
                        )
                    refreshed = _refresh_cycle_locked(root, record, now=now, allocator=alloc,
                                                      crash_after_manifest=crash_after_manifest,
                                                      prescan=prescan, last_try=_last_try,
                                                      expected_binding=expected_binding, jobs=jobs)
                    latest = read_cycle_record(root, cycle_id)
                    completed_state = _published_cycle_state(root, latest) if latest else None
                    if completed_state != "completed":
                        raise ProducerError(
                            "finalize-state-conflict",
                            f"{cycle_id}: requested=completed published_cycle_state={published} storage_state=sealed",
                        )
                    return _finish({**refreshed, "storage_state": "sealed", "cycle_state": "completed"})
                raise ProducerError(
                    "finalize-state-conflict",
                    f"{cycle_id}: requested={state} published_cycle_state={published} storage_state=sealed",
                )
            # General relocation compatibility may judge an absent manifest
            # from its state cache. Terminal exact callers still require every
            # durable proof; an already-sealed status is never that proof.
            manifest_path = _record_cycle_manifest_path(root, record)
            if expected_binding is not None:
                if _binding_route_unrecorded(manifest_path, expected_binding):
                    # A route continuing this closed cycle finishes it again (§45 D-127): the
                    # document gets that route's own terminal record before it is proved.
                    _refresh_cycle_locked(root, record, now=now, allocator=alloc, prescan=prescan,
                                          last_try=_last_try, expected_binding=expected_binding, jobs=jobs)
                    record = read_cycle_record(root, cycle_id)
                verified = _verify_sealed_cycle_locked(root, record, expected_binding)
                return _finish({**verified, "storage_state": "sealed", "cycle_state": published})
            if _path_entry_present(manifest_path):
                # §45 D-123/D-124: closing again is the refresh -- the closed
                # cycle's files are compared with its manifest and the next
                # document is published when they differ.
                refreshed = _refresh_cycle_locked(root, record, now=now, allocator=alloc,
                                                  crash_after_manifest=crash_after_manifest,
                                                  prescan=prescan, last_try=_last_try,
                                                  expected_binding=expected_binding, jobs=jobs)
                return _finish({**refreshed, "storage_state": "sealed", "cycle_state": published})
            return _finish({"status": "already-sealed", "cycle_id": cycle_id,
                    "manifest_digest": record.get("manifest_digest"),
                    "storage_state": "sealed", "cycle_state": published})
        if record.get("state") != "open":
            raise ProducerError("cycle-not-open", record.get("state", "?"))
        first_close_read = prescan is not None and prescan.open_cycle and not adopt_root_outputs
        if not first_close_read and not (_last_try or adopt_root_outputs):
            raise _StaleScan(cycle_id)  # the cycle was not open when it was read: read it, lock released
        # The open-cycle checkpoint assigns IDs under this lock; holding it until
        # the interim document is removed keeps a concurrent checkpoint from
        # republishing IDs the sealed manifest did not take.
        interim_guard.enter_context(_checkpoint_lock(
            root, cycle_id, timeout=CHECKPOINT_FINALIZE_LOCK_SECONDS if lock_timeout is None else lock_timeout))
        # A live review lease protects the report's exact write window from
        # both terminal outcomes.  The check remains under the producer
        # admission lock and happens before any terminal mutation.
        live_review = _live_review_lease(root, cycle_id, now=now)
        live_record = _read_json(live_review) if live_review is not None else None
        if state == "abandoned":
            # SD-117 L1 before L3 (plan-check C-2): live-lease enforcement
            # comes first -- a live registered review lease refuses the
            # abandon outright, zero events, zero record-state change
            # (E47-2), before the abandon_reason vocabulary is even
            # consulted.
            if not force_abandon_ignoring_lease and live_review is not None:
                lease = live_record
                reason = ("cycle-finalize-blocked-live-review"
                          if _is_v2_review_lease(lease)
                          else "cycle-abandon-blocked-live-review")
                raise ProducerError(reason, cycle_id)
            if force_abandon_ignoring_lease and abandon_reason not in (None, "operator-override-live-review"):
                raise ProducerError("abandon-reason-required", str(abandon_reason))
            if force_abandon_ignoring_lease:
                abandon_reason = "operator-override-live-review"
            if abandon_reason not in ABANDON_REASONS:
                raise ProducerError("abandon-reason-required", str(abandon_reason))
        elif live_review is not None:
            # SD-120 completed settlement consumes the v1/v2 union. Keep the
            # legacy abandon override above and the v2-only root recovery
            # policy separate from this completed publication boundary.
            raise ProducerError("cycle-finalize-blocked-live-review", cycle_id)
        directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
        # D-120: the route that seals this cycle is the unique T(C) leaf, not
        # necessarily the begin route -- an inherited (rebound) cycle's begin
        # route may long since have a continuation writing it. A completion
        # controller that already names its exact route in the binding seals
        # that route (hash-verified here) after the same admission, instead of a
        # walk that could pick a different leaf.
        binding_route_id = (expected_binding.get("route_id")
                            if isinstance(expected_binding, Mapping) else None)
        binding_route_hash = (expected_binding.get("route_hash")
                              if isinstance(expected_binding, Mapping) else None)
        if isinstance(binding_route_id, str) and binding_route_id and isinstance(binding_route_hash, str):
            route = load_route(root, route_lineage.canonical_route_path(root, binding_route_id),
                               expected_identity={"route_id": binding_route_id,
                                                  "route_hash": binding_route_hash})
        else:
            try:
                route = _finalize_route(root, record,
                                        deadline=_scan_budget.deadline() if _scan_budget is not None else None)
            except ProducerError as exc:
                if exc.code != "scan-in-progress":
                    raise
                if prescan is not None and prescan.open_cycle:
                    _keep_prescan_digests(root, cycle_id, prescan)   # the next try reads only what is left
                return _finish({"status": "deferred", "reason": "scan-budget", "cycle_id": cycle_id})
        admission = cycle_route_admission(root, record, route, finalize=True)
        if not admission.allow:
            raise ProducerError(admission.reason, admission.detail)
        if _bind_cycle_route_locked(root, record, route, sealing=True)["written"]:
            # `_commit_sealed` below reseals from this local copy; refresh it
            # so the binding write just made is not clobbered back to stale.
            record = read_cycle_record(root, cycle_id)
        artifact_locator.prepare_index_update(root, [record["campaign_id"]])
        adopted_root_outputs: List[str] = []
        if adopt_root_outputs and state != "abandoned":
            raise ProducerError("root-output-adoption-requires-abandoned")
        adoption_moves: List[Tuple[str, Path, Path]] = []
        seen_adoptions: set[str] = set()
        for name in adopt_root_outputs:
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise ProducerError("root-output-adoption-name-invalid", name)
            if name in seen_adoptions:
                raise ProducerError("root-output-adoption-duplicate", name)
            seen_adoptions.add(name)
            source = directory / name
            target = directory / "artifacts" / name
            if source.is_symlink() or not source.is_file():
                raise ProducerError("root-output-adoption-source-invalid", name)
            if target.exists() or target.is_symlink():
                raise ProducerError("root-output-adoption-target-exists", name)
            adoption_moves.append((name, source, target))
        for name, source, target in adoption_moves:
            os.replace(source, target)
            adopted_root_outputs.append(name)
        placements = _output_placements(root, record)
        primary = _placed_locator(_cycle_relative_primary(primary, directory), placements)
        support_locators = tuple(_placed_locator(value, placements) for value in support_locators)
        excluded_hidden: List[str] = []
        excluded_symlinks: List[str] = []
        bundle_trees: List[artifact_directory_bundle.Tree] = []
        if first_close_read:
            if prescan.error is not None:
                raise prescan.error
            violations, facts = prescan.violations, prescan.facts
            bundle_trees = prescan.bundles
            excluded_hidden, excluded_symlinks = list(prescan.excluded), list(prescan.excluded_symlinks)
        else:
            # Fallback after a just-completed adoption uses the same bounded
            # facts and raw-tree collector as the unlocked first-close scan.
            # Marker bytes were observed before the admission lock; this pass
            # reuses that snapshot after the controlled root-file moves.
            if prescan is not None and prescan.open_cycle:
                bundle_trees = prescan.bundles
                marker_observations = prescan.marker_observations
                excluded_roots = prescan.excluded_roots
                marker_read = False
            else:
                bundle_trees = _scan_directory_bundles(directory)
                marker_observations, excluded_roots, marker_read = None, None, True
            paths, violations = _output_paths(directory, exclude_hidden=exclude_hidden,
                                              excluded=excluded_hidden,
                                              excluded_symlinks=excluded_symlinks,
                                              marker_observations=marker_observations,
                                              excluded_roots=excluded_roots,
                                              skip_directories=[tree.directory for tree in bundle_trees],
                                              read_markers=marker_read)
            bundle_dirs = [tree.directory for tree in bundle_trees]
            journal = _read_json(journal_path(root, cycle_id)) or {}
            owned = {row.get("locator") for row in journal.get("directory_bundles", [])
                     if isinstance(row, Mapping)}
            paths = [(rel, entry) for rel, entry in paths
                     if rel not in owned and not any(rel.startswith(prefix + "/") for prefix in bundle_dirs)]
            facts = []
            if not violations:
                for rel, entry in paths:
                    digest_value, size = _stream_file_facts(entry)
                    facts.append((rel, digest_value, size))
        if violations:
            raise ProducerError("output-invalid", output_invalid_detail(violations))
        for rel, digest_value, size in _bundle_exact_evidence_facts(
                root, record, directory, bundle_trees, route, primary):
            if all(row[0] != rel for row in facts):
                facts.append((rel, digest_value, size))
        bundle_facts, bundle_metadata = _publish_bundle_descriptors(root, cycle_id, directory, bundle_trees)
        facts.extend(bundle_facts)
        if not facts:
            # D-9: no durable output, no lineage. Unchanged except that an
            # abandoned empty cycle also carries its sealed abandon_reason
            # (E47-5: `_remove_empty_cycle` call and returned `status` stay
            # byte-identical either way).
            _remove_empty_cycle(root, record)
            record = dict(record)
            record["state"] = "abandoned" if state == "abandoned" else "no-lineage"
            record["sealed_on"] = _rfc3339(now)
            if state == "abandoned":
                record["abandon_reason"] = abandon_reason
            _write_cycle_record(root, record, exclusive=False)
            remove_interim(root, cycle_id)
            return _finish({"status": "no-lineage", "cycle_id": cycle_id, "lineage_committed": False})
        # §45 D-123: a parent is a reference to a cycle of this root, not an
        # ordering constraint; the child closes whether the parent is open,
        # closed, or already deleted. The index only needs to know it is a cycle
        # of this root.
        parent_ids = (frozenset({record["parent_cycle_id"]})
                      if record.get("parent_cycle_id") and read_cycle_record(root, record["parent_cycle_id"])
                      else frozenset())
        reserved, reservation = read_interim_reservation(root, record)
        identity = artifact_lifecycle.read_root_identity(root)
        index = artifact_admission.load_index(root)
        # A reservation never blocks a seal: when a reused field makes the
        # document fail, rebuild with fresh events, then with fresh IDs, and say so.
        attempts: List[Tuple[Optional[InterimReservation], Optional[str]]] = [(reserved, None)]
        if reserved is not None:
            attempts += [(_without_event_reuse(reserved), "events-fresh"), (None, "ids-fresh")]
        for candidate, rebuilt in attempts:
            document = build_manifest(
                root, record, route, (), state=state,
                primary=_cycle_relative_primary(primary, directory),
                allow_open_route=allow_open_route, allocator=alloc, now=now,
                abandon_reason=abandon_reason, support_locators=support_locators,
                reserved=candidate, facts=facts,
                bundle_metadata=bundle_metadata,
            )
            report = artifact_manifest.validate(document)
            if not report.ok:
                failure = ProducerError("manifest-invalid", ";".join(v.code for v in report.violations))
                continue
            digest = artifact_manifest.manifest_digest(document)
            index_report = artifact_index.check(
                index, document, idempotency_key=cycle_id, manifest_digest=digest,
                repository_id=identity.repository_id if identity else None,
                known_parent_cycle_ids=parent_ids,
            )
            if not index_report.ok:
                failure = ProducerError("index-rejected", ";".join(v.code for v in index_report.violations))
                continue
            reserved, interim_rebuilt, failure = candidate, rebuilt, None
            break
        if failure is not None:
            raise failure
        if state == "completed" and route_is_closed(root, route):
            completion = artifact_lifecycle.evaluate_cycle_completion(
                document, content_root=directory,
                route_file=route_lineage.canonical_route_path(root, route["route_id"]),
                publication=publication, expected_root_id=identity.artifact_root_id if identity else None,
                inline_finish_id=(expected_binding.get("inline_finish_id")
                                  if isinstance(expected_binding, Mapping) else None),
                payload_verified=first_close_read,  # read in chunks and `lstat`-confirmed above
            )
            if not completion.ok:
                raise ProducerError(
                    "completion-rejected",
                    ";".join(f"{v.code}:{v.detail}" for v in completion.reasons),
                )
        manifest_path = directory / "manifest.json"
        if manifest_path.exists():
            raise ProducerError("manifest-already-present", str(manifest_path))
        cycle_path = os.path.relpath(str(directory), str(root))
        skipped_continuations = [{"route_id": route_id, "reason": "never-run"}
                                 for route_id in admission.skipped]
        if (excluded_symlinks or (prescan and prescan.excluded_roots)
                or skipped_continuations or record.get("skipped_continuations")):
            # Written before the manifest is published so a crash after the commit
            # point recovers from the on-disk record without losing the exclusion
            # or the branch this seal passed over.
            record = dict(record)
            record["excluded_symlinks"] = excluded_symlinks
            record["excluded_roots"] = sorted(set(prescan.excluded_roots if prescan else ()))
            if skipped_continuations:
                record["skipped_continuations"] = skipped_continuations
            else:
                # A retry after a crash between the record write and the manifest
                # commit may carry a branch this seal did not pass over; reconcile
                # it instead of leaving a stale advisory claim.
                record.pop("skipped_continuations", None)
            _write_cycle_record(root, record, exclusive=False)
        manifest_bytes = artifact_manifest.canonical_bytes(document)
        _write_journal(root, cycle_id, state="sealing", manifest_digest=digest, cycle_path=cycle_path,
                       manifest_revision_id=document["manifest_revision_id"])
        # §45 D-124: the document is kept as published before it is published.
        artifact_lifecycle.preserve_manifest_snapshot(root, cycle_id, manifest_bytes)
        # COMMIT POINT: exclusive manifest creation.
        _write_exclusive(manifest_path, manifest_bytes)
        if crash_after_manifest:  # test hook: simulate a crash after the commit point
            raise artifact_admission.AdmissionRecoveryRequired("simulated crash after manifest publish")
        try:
            _write_journal(root, cycle_id, state="published", manifest_digest=digest, cycle_path=cycle_path)
            # The admission lock has been held since `index` was read, so it is still current.
            _commit_sealed(root, record, document, digest, now=now, index=index,
                           excluded_count=len(excluded_hidden) + len(excluded_symlinks),
                           excluded_roots=prescan.excluded_roots if prescan else ())
        except BaseException as exc:
            raise artifact_admission.AdmissionRecoveryRequired(
                f"cycle {cycle_id} manifest published but post-publish update failed; run recover"
            ) from exc
        sealed_result = {"excluded_hidden": excluded_hidden, "excluded_symlinks": excluded_symlinks,
            "adopted_root_outputs": adopted_root_outputs,
            "status": "sealed", "cycle_id": cycle_id, "campaign_id": record["campaign_id"],
            "manifest_digest": digest, "manifest_path": str(manifest_path),
            "artifact_count": len(facts), "lineage_committed": True, "cycle_state": document["cycle"]["state"],
            "storage_state": "sealed",
        }
        if skipped_continuations:
            sealed_result["skipped_continuations"] = skipped_continuations
        if reservation != "absent":
            # The interim document's IDs were (or, when unusable, were not)
            # carried into the sealed manifest; say which.
            kept = reserved.artifacts if reserved is not None else {}
            sealed_result["interim_ids"] = reservation
            if interim_rebuilt:
                sealed_result["interim_ids_rebuilt"] = interim_rebuilt
            sealed_result["interim_ids_kept"] = sum(
                1 for row in document["artifact_revisions"]
                if row["locator"]["path"] in kept
                and kept[row["locator"]["path"]].artifact_id == row["artifact_id"])
        if document["cycle"]["state"] == "active":
            sealed_result["provisional"] = True
            sealed_result["warning"] = PROVISIONAL_SEAL_WARNING
        sealed = _finish(sealed_result)
        if first_close_read:
            kept_scan = prescan
    except _StaleScan:
        pass  # read again below, with the lock released
    finally:
        interim_guard.close()
        if lock_fd is not None:
            artifact_admission._release_lock(root, lock_fd)
    if sealed is not None:
        if kept_scan is not None:
            _keep_prescan_digests(root, cycle_id, kept_scan)  # the first refresh need not hash it all again
        return sealed
    return _finalize(
        root, cycle_id=cycle_id, state=state, primary=primary, publication=publication,
        allow_open_route=allow_open_route, allocator=alloc, now=now, crash_after_manifest=crash_after_manifest,
        exclude_hidden=exclude_hidden, adopt_root_outputs=adopt_root_outputs, abandon_reason=abandon_reason,
        force_abandon_ignoring_lease=force_abandon_ignoring_lease, support_locators=support_locators,
        expected_binding=expected_binding, jobs=jobs, lock_timeout=lock_timeout,
        exclude_symlinks=exclude_symlinks,
        _recovery_scope=_recovery_scope, _last_try=True, _scan_budget=_scan_budget,
        _prescan=_prescan_cycle(root, cycle_id, unless_recorded=expected_binding,
                                include_open=True, exclude_hidden=exclude_hidden,
                                budget=_scan_budget))


def finalize(root: Path, *, cycle_id: str, **kwargs: Any) -> Dict[str, Any]:
    """Seal one cycle and request a best-effort entrance refresh without waiting.

    The entrance worker runs outside the admission lock and the caller's stack;
    neither a slow refresh nor its failure changes the seal result."""
    result = _finalize(root, cycle_id=cycle_id, **kwargs)
    try:
        import artifact_entrance  # lazy: the seal never depends on the entrance
        artifact_entrance.request_refresh(Path(root), cycle_id=cycle_id)
    except Exception as exc:  # noqa: BLE001 -- the entrance never changes or fails a seal
        print(f"artifact_entrance skipped error={type(exc).__name__}", file=sys.stderr)
    return result


def _finalize_deleted_locked(root: Path, record: Mapping[str, Any], *, state: str, now: Optional[float],
                             abandon_reason: Optional[str]) -> Dict[str, Any]:
    """A cycle whose folder was deleted (§45 D-126) is closed by what is left of it.

    A cycle that was still running ends as a cycle with no output -- the existing `no-lineage`
    (or `abandoned`) record -- and nothing is written to disk: the folder is not made again and no
    manifest is published, so the route's work is never a material success.  A cycle that had
    closed is as it was closed; the answer carries `deleted`."""
    cycle_id = record["cycle_id"]
    if record.get("state") == "open":
        ended = dict(record)
        ended["state"] = "abandoned" if state == "abandoned" else "no-lineage"
        ended["sealed_on"] = _rfc3339(now)
        if state == "abandoned":
            ended["abandon_reason"] = abandon_reason if abandon_reason in ABANDON_REASONS else "route-unrecoverable"
        _write_cycle_record(root, ended, exclusive=False)
        remove_interim(root, cycle_id)
        return {"status": "no-lineage", "cycle_id": cycle_id, "lineage_committed": False, "deleted": True}
    if _closed_record(record):
        return {"status": "already-sealed", "cycle_id": cycle_id, "manifest_digest": record.get("manifest_digest"),
                "storage_state": "sealed", "cycle_state": record.get("cycle_state"), "deleted": True}
    return {"status": "no-lineage", "cycle_id": cycle_id, "lineage_committed": False, "deleted": True}


def _recover_exact_cycle_locked(root: Path, cycle_id: str, expected_binding: Optional[Mapping[str, Any]] = None,
                                *, now: Optional[float] = None) -> Dict[str, Any]:
    """Recover only one cycle journal while the admission lock is held."""
    journal_path = producer_dir(root) / "journal" / f"{cycle_id}.json"
    journal = _read_json(journal_path)
    if _path_entry_present(journal_path) and journal is None:
        raise ProducerError("cycle-journal-invalid", cycle_id)
    if journal and journal.get("cycle_id", cycle_id) != cycle_id:
        raise ProducerError("cycle-journal-identity-mismatch", cycle_id)
    if journal and expected_binding and journal.get("manifest_digest") != expected_binding.get("manifest_digest", journal.get("manifest_digest")):
        raise ProducerError("cycle-journal-binding-mismatch", cycle_id)
    if expected_binding:
        record = read_cycle_record(root, cycle_id)
        for key in ("campaign_id", "cycle_id", "producer_id"):
            expected = expected_binding.get(key)
            if expected is not None and (record is None or record.get(key) != expected):
                raise ProducerError("cycle-journal-binding-mismatch", key)
    record = read_cycle_record(root, cycle_id)
    if record is None:
        raise ProducerError("cycle-unknown", cycle_id)
    if _live_review_lease(root, cycle_id, now=now) is not None:
        raise ProducerError("cycle-finalize-blocked-live-review", cycle_id)
    if record.get("state") == "sealed":
        if journal is not None and _refresh_journal_resume(root, record, journal, now=now):
            record = read_cycle_record(root, cycle_id)
        if expected_binding and _binding_route_unrecorded(_record_cycle_manifest_path(root, record), expected_binding):
            # A route that continues this closed cycle has no record in it yet; there is
            # nothing to recover, and `finalize` writes that record before it proves it.
            return {"status": "already-sealed", "cycle_id": cycle_id}
        return _verify_sealed_cycle_locked(root, record, expected_binding)
    if record.get("state") != "open":
        raise ProducerError("cycle-not-open", cycle_id)
    directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
    path = directory / "manifest.json"
    if _path_entry_present(path):
        document = _read_json(path)
        if (journal is None or document is None
                or (root / str(journal.get("cycle_path", ""))).resolve() != directory.resolve()
                or artifact_manifest.manifest_digest(document) != journal.get("manifest_digest")):
            raise ProducerError("cycle-journal-manifest-mismatch", cycle_id)
        identity = artifact_lifecycle.read_root_identity(root)
        manifest_route_file, _manifest_route = resolve_cycle_manifest_route(root, record, document)
        completion = artifact_lifecycle.evaluate_cycle_completion(
            document, content_root=directory, route_file=manifest_route_file,
            expected_root_id=identity.artifact_root_id if identity else None,
            inline_finish_id=expected_binding.get("inline_finish_id") if isinstance(expected_binding, Mapping) else None)
        if not completion.ok:
            raise ProducerError("completion-rejected", ";".join(v.code for v in completion.reasons))
        _commit_sealed(root, record, document, journal["manifest_digest"], now=now)
        return _verify_sealed_cycle_locked(root, read_cycle_record(root, cycle_id), expected_binding)
    if journal is not None:
        if journal.get("directory_bundles"):
            # Preparing descriptor ownership is before the manifest commit
            # point. Keep it so the same finalize can resume the owned path.
            return {"status": "open", "cycle_id": cycle_id, "directory_bundles_prepared": True}
        # Nothing crossed the manifest commit point; the same cycle can
        # re-enter finalize. No other journal or cycle is read or changed.
        _drop_unpublished_snapshot(root, cycle_id, journal)
        _remove_journal(root, cycle_id)
    return {"status": "open", "cycle_id": cycle_id}


def _verify_sealed_cycle_locked(root: Path, record: Mapping[str, Any],
                                expected_binding: Optional[Mapping[str, Any]] = None,
                                expected_manifest_digest: Optional[str] = None, *,
                                historical: bool = False) -> Dict[str, Any]:
    """Prove a closed cycle's record, current manifest and index agree (§45 D-127).

    A D-9 no-lineage record proves the bound identity. Before the owner envelope
    is sealed, it also proves there are no files the producer would seal.
    What is checked is identity and consistency: the cycle record, the current
    `manifest.json` and the index row all describe the same document, and the
    terminal record that closed the cycle is in it.  Whether the files still
    hold those bytes is not asked here -- a later edit is the next refresh's
    business, never a reason to call a finished cycle unfinished.
    `expected_manifest_digest` is the revision a caller recorded when it
    finished; it may be an earlier one, and the result then says the cycle was
    updated since (`updated_since`) while still returning that recorded digest.
    """
    if record.get("state") not in {"sealed", "no-lineage"} or not record.get("sealed_on"):
        raise ProducerError("already-sealed-mismatch", "cycle-record")
    if expected_binding is not None:
        for key in ("cycle_id", "producer_id"):
            expected = expected_binding.get(key)
            if expected is not None and record.get(key) != expected:
                raise ProducerError("already-sealed-mismatch", key)
        if (expected_binding.get("cycle_record_digest")
                and not dispatch_terminal_commit.cycle_identity_matches(
                    record, expected_binding["cycle_record_digest"],
                    campaign_id=expected_binding.get("campaign_id"))):
            raise ProducerError("already-sealed-mismatch", "cycle-identity")
    if record.get("state") == "no-lineage":
        # D-9's other successful terminal shape has no manifest or index row.
        # Never manufacture an empty seal or rejudge completed output inventory.
        if not expected_binding or not expected_binding.get("cycle_record_digest"):
            raise ProducerError("already-sealed-mismatch", "cycle-identity")
        if "manifest_digest" not in record or record["manifest_digest"] is not None:
            raise ProducerError("already-sealed-mismatch", "manifest")
        if expected_manifest_digest is not None:
            raise ProducerError("already-sealed-mismatch", "manifest")
        if not historical:
            manifest_path = _record_cycle_manifest_path(root, record, missing_if_reused=True)
            if manifest_path is not None and _path_entry_present(manifest_path.parent):
                directory = manifest_path.parent
                bundles = _scan_directory_bundles(directory)
                paths, violations = _output_paths(
                    directory, skip_directories=[tree.directory for tree in bundles])
                if bundles or paths or violations:
                    raise ProducerError("already-sealed-mismatch", "no-lineage-output")
        return {"status": "no-lineage", "cycle_id": record["cycle_id"], "lineage_committed": False}
    manifest_path = _record_cycle_manifest_path(root, record)
    directory = manifest_path.parent
    try:
        if historical:
            def load_historical_manifest():
                raw = stable_read_bytes(manifest_path)
                document = json.loads(raw.decode("utf-8"))
                canonical = artifact_manifest.canonical_bytes(document)
                return (document, raw == canonical,
                        artifact_manifest.manifest_digest(document))

            observed_document, canonical_match, digest = _HISTORICAL_OBSERVATIONS.memoize(
                ("sealed-manifest", str(manifest_path.resolve(strict=False))),
                [manifest_path], load_historical_manifest,
                weight=lambda value: max(1, manifest_path.stat().st_size * 4),
            )
            document = observed_document
        else:
            raw = manifest_path.read_bytes()
            document = json.loads(raw.decode("utf-8"))
            canonical = artifact_manifest.canonical_bytes(document)
            canonical_match = raw == canonical
            digest = artifact_manifest.manifest_digest(document)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProducerError("already-sealed-mismatch", str(manifest_path)) from exc
    if not canonical_match or digest != record.get("manifest_digest"):
        raise ProducerError("already-sealed-mismatch", "manifest")
    identity = artifact_lifecycle.read_root_identity(root)
    if historical:
        # load_index's missing-index fallback allocates a root identity.
        # Observation must refuse missing/corrupt index bytes without repair.
        try:
            index_projection = _artifact_index_observation(
                root, cycle_id=record["cycle_id"])
        except (OSError, ValueError, UnicodeError) as exc:
            raise ProducerError("already-sealed-mismatch", "index-missing") from exc
        if index_projection.schema_error:
            raise ValueError(index_projection.schema_error)
        index_root_id = index_projection.artifact_root_id
        row = index_projection.manifest_row(record["cycle_id"])
        cycle_index_row = index_projection.cycle_row(record["cycle_id"])
    else:
        head = artifact_admission.load_index_head(root)
        if head is not None:
            index_root_id = head.artifact_root_id
            row = head.manifests.get(record["cycle_id"])
            cycle_index_row = head.cycles.get(record["cycle_id"])
        else:
            index = artifact_admission.load_index(root)
            index_root_id = index.artifact_root_id
            row = index.manifests.get(record["cycle_id"]) if hasattr(index, "manifests") else None
            cycle_index_row = index.cycles.get(record["cycle_id"]) if hasattr(index, "cycles") else None
    if not isinstance(row, dict) or row.get("manifest_digest") != digest:
        raise ProducerError("already-sealed-mismatch", "index")
    if expected_binding is not None:
        cycle_id = record["cycle_id"]
        cycle = document.get("cycle", {})
        if (identity is None or index_root_id != identity.artifact_root_id
                or document.get("artifact_root_id") != identity.artifact_root_id
                or cycle.get("cycle_id") != cycle_id
                or cycle.get("campaign_id") != record["campaign_id"]
                or document.get("producer", {}).get("producer_id") != record["producer_id"]):
            raise ProducerError("already-sealed-mismatch", "index-identity")
        # Use the canonical writer's projection, scoped to this exact cycle.
        # Root-wide rebuild/repair could touch unrelated open cycles and is
        # not evidence that this transaction's two index rows were applied.
        relative_path = ((record.get("journal_migration") or {}).get("source")
                         or os.path.relpath(str(directory), str(root)))
        def expected_rows():
            expected = artifact_index.apply(
                artifact_index.empty(identity.artifact_root_id), document,
                cycle_path=relative_path, manifest_digest=digest, idempotency_key=cycle_id)
            return expected.manifests[cycle_id], expected.cycles[cycle_id]
        if historical:
            # This pure projection remains reusable even if a later check
            # rejects the current index/outcome. Rejection stays rejection;
            # it need not repeat an unchanged manifest's whole-tree apply.
            expected_manifest, expected_cycle = _HISTORICAL_PROJECTIONS.memoize(
                ("manifest-index-rows", str(manifest_path.resolve(strict=False)),
                 cycle_id, digest, identity.artifact_root_id, relative_path),
                [manifest_path], expected_rows,
                weight=lambda value: 128 + 3 * len(_canonical(value)))
        else:
            expected_manifest, expected_cycle = expected_rows()
        if row != expected_manifest or cycle_index_row != expected_cycle:
            raise ProducerError("already-sealed-mismatch", "index-projection")
    updated_since = False
    if expected_manifest_digest is not None and expected_manifest_digest != digest:
        # The caller finished at an earlier revision of this cycle.  A preserved
        # copy proves it existed; with none (the cycle changed before it was
        # first observed), the caller's own terminal record is the proof.
        updated_since = True
    if historical:
        # The exact outcome/event seals the past. Do not re-enter live route
        # admission (cwd, evidence payload or producer refresh) to observe it.
        rows = [r for r in document.get("routes", [])
                if r.get("route_id") == (expected_binding or {}).get("route_id")]
        if len(rows) != 1 or rows[0].get("route_hash") != expected_binding.get("route_hash"):
            raise ProducerError("already-sealed-mismatch", "historical-route")
        route_path = route_lineage.canonical_route_path(root, rows[0]["route_id"])
        route = json.loads(route_path.read_text())
        if (route.get("route_hash") != route_identity.route_hash(route)
                or route.get("route_id") != rows[0]["route_id"] or route.get("route_hash") != rows[0]["route_hash"]):
            raise ProducerError("already-sealed-mismatch", "historical-route-identity")
        outcome_path = artifact_lifecycle.canonical_outcome_path(root, rows[0]["route_id"])
        outcome = json.loads(outcome_path.read_text())
        marker_digest = artifact_lifecycle._marker_digest(dispatch_terminal_commit._route_module(), route,
            recorded=True, recorded_gates=outcome.get("terminal_gates"))
        events = [e for e in document.get("events", [])
                  if e.get("event_id") == rows[0].get("terminal_evidence_id")]
        payload = {"artifact_root_id": identity.artifact_root_id,
            "route_id": rows[0]["route_id"], "route_hash": rows[0]["route_hash"],
            "outcome_digest": artifact_lifecycle._sha256_path(outcome_path),
            "terminal_marker_digest": marker_digest}
        if (document.get("cycle", {}).get("state") != "completed"
                or rows[0].get("terminal_marker") != marker_digest or len(events) != 1
                or events[0].get("event_type") != "route.terminal.recorded"
                or events[0].get("target_id") != record["cycle_id"] or events[0].get("payload") != payload
                or not artifact_manifest.validate_update(document,
                    preserved=_earlier_documents(root, record["cycle_id"], document), published=True).ok):
            raise ProducerError("already-sealed-mismatch", "historical-completion-evidence")
        return {"status": "already-sealed", "cycle_id": record["cycle_id"], "manifest_digest": digest}
    manifest_route_file, _manifest_route = resolve_cycle_manifest_route(
        root, record, document, route_id=(expected_binding or {}).get("route_id"))
    if document.get("cycle", {}).get("state") == "completed":
        completion = artifact_lifecycle.evaluate_cycle_completion(
            document, content_root=directory, route_file=manifest_route_file,
            expected_root_id=identity.artifact_root_id if identity else None,
            inline_finish_id=(expected_binding.get("inline_finish_id")
                              if isinstance(expected_binding, Mapping) else None),
            preserved=_earlier_documents(root, record["cycle_id"], document))
        if not completion.ok:
            raise ProducerError("already-sealed-mismatch", "completion-evidence")
    result = {"status": "already-sealed", "cycle_id": record["cycle_id"],
              "manifest_digest": expected_manifest_digest or digest}
    if updated_since:
        result["current_manifest_digest"] = digest
        result["updated_since"] = True
    return result


def _verify_deleted_cycle(record: Mapping[str, Any], expected_binding: Optional[Mapping[str, Any]],
                          expected_manifest_digest: Optional[str]) -> Dict[str, Any]:
    """What is left of a deleted cycle (§45 D-127): its record.  The finish is read as it was recorded, and
    the cycle's absence is said (`deleted`), not made a failure.  The cycle's identity is still checked."""
    if expected_binding is not None:
        for key in ("cycle_id", "producer_id"):
            expected = expected_binding.get(key)
            if expected is not None and record.get(key) != expected:
                raise ProducerError("already-sealed-mismatch", key)
        if (expected_binding.get("cycle_record_digest")
                and not dispatch_terminal_commit.cycle_identity_matches(
                    record, expected_binding["cycle_record_digest"],
                    campaign_id=expected_binding.get("campaign_id"))):
            raise ProducerError("already-sealed-mismatch", "cycle-identity")
    result: Dict[str, Any] = {"status": "already-sealed", "cycle_id": record["cycle_id"], "deleted": True,
                              "deleted_at": record.get("deleted_at")}
    digest = expected_manifest_digest or record.get("manifest_digest")
    if digest:
        result["manifest_digest"] = digest
    return result


def observe_finalized_cycle(root: Path, *, cycle_id: str, expected_binding: Mapping[str, Any],
                            expected_manifest_digest: Optional[str] = None):
    """Pure historical reader: no admission lock, refresh or producer writes.

    In-flight inconsistent revisions fail conservatively and can be read again.
    Writers retain their locked verification boundary.
    """
    root = Path(root).resolve()
    if _live_review_lease(root, cycle_id) is not None:
        raise ProducerError("cycle-finalize-blocked-live-review", cycle_id)
    record = read_cycle_record(root, cycle_id)
    if record is None:
        raise ProducerError("cycle-unknown", cycle_id)
    if record.get("deleted_at"):
        return _verify_deleted_cycle(record, expected_binding, expected_manifest_digest)
    if record.get("state") == "no-lineage":
        # The envelope already proved no output; later locator reuse is unrelated.
        return _verify_sealed_cycle_locked(root, record, expected_binding,
            expected_manifest_digest=expected_manifest_digest, historical=True)
    # Cache only the immutable historical consistency proof. Review leases
    # above and the terminal controller's child/process/claim checks remain
    # fresh. Missing optional markers/snapshot directories depend on their
    # nearest existing parent so recreation invalidates the proof as well.
    dependencies = [cycle_record_path(root, cycle_id),
                    _record_cycle_manifest_path(root, record),
                    artifact_admission._index_path(root),
                    root / artifact_admission.ADMISSION_REL / "root-identity.json"]
    route_id = expected_binding.get("route_id")
    if route_id:
        route_path = route_lineage.canonical_route_path(root, route_id)
        dependencies.extend((route_path, artifact_lifecycle.canonical_outcome_path(root, route_id)))
        route = json.loads(route_path.read_text())
        module = dispatch_terminal_commit._route_module()
        dependencies.extend(module.completion_dir(route_id) / f"{node['id']}.json"
                            for node in route.get("nodes", []) if node.get("terminal") is True)
    snapshots = artifact_lifecycle.manifest_snapshot_dir(root, cycle_id)
    dependencies.append(snapshots)
    if snapshots.is_dir():
        dependencies.extend(path for path in snapshots.iterdir() if path.suffix == ".json")
    observed_paths = []
    for dependency in dependencies:
        while not dependency.exists() and dependency != dependency.parent:
            dependency = dependency.parent
        observed_paths.append(dependency)
    key = ("sealed-cycle-consistency", str(root), cycle_id,
           _digest(_canonical(record)), _digest(_canonical(dict(expected_binding))),
           expected_manifest_digest)
    result = _HISTORICAL_PROJECTIONS.memoize(key, observed_paths,
        lambda: _verify_sealed_cycle_locked(root, record, expected_binding,
            expected_manifest_digest=expected_manifest_digest, historical=True), weight=512)
    return dict(result)


# ---------------------------------------------------------------------------
# recover
# ---------------------------------------------------------------------------


def _is_recoverable_locator_defect(exc: BaseException) -> bool:
    """§3.6a's isolation only ever catches a *locator* defect on the record
    being resolved -- never any other `ProducerError` (a live-review fence, a
    digest mismatch, ...), which must keep propagating unconditionally."""

    if isinstance(exc, artifact_locator.LocatorError):
        return True
    return isinstance(exc, ProducerError) and exc.code == "record-locator-invalid"


def _recovery_open_candidates(root: Path, cycle_id: Optional[str] = None) -> set:
    """Before the admission lock: the open cycles the root sweep may have work for, and ``cycle_id``.

    An open cycle sitting in the folder the published locator map names, with its own binding
    and no `manifest.json`, is one the sweep only lists as open, so it is not read again under
    the lock.  Everything else -- a folder gone or unpublished, a manifest written and not
    recorded, a binding naming another cycle, and every open cycle of the campaign being closed
    (whose locator defects the sweep raises) -- stays the sweep's, resolved there as before.
    Only the map, an `lstat` and the small binding file are read here.  A cycle that turns into
    one after this look is the next sweep's (the sweep is already best effort for every cycle
    but the one being closed)."""
    wanted = {cycle_id} if cycle_id else set()
    root = Path(root)
    try:
        published = artifact_locator._load_index(root) or {}
    except Exception:  # noqa: BLE001 -- an unreadable map leaves every open cycle to the sweep
        published = {}
    target = read_cycle_record(root, cycle_id) if cycle_id else None
    target_campaign = (target or {}).get("campaign_id")
    for record in list_cycle_records(root):
        if (record.get("state") != "open" or record.get("deleted_at")
                or (record.get("relocation") or {}).get("artifact_root")):
            continue
        where = published.get(record.get("cycle_id"))
        if record.get("campaign_id") != target_campaign and isinstance(where, str) and where:
            folder = root / where
            try:
                quiet = (stat.S_ISDIR(os.lstat(str(folder)).st_mode)
                         and not _path_entry_present(folder / "manifest.json")
                         and _folder_cycle_id(folder) == record["cycle_id"])
            except OSError:
                quiet = False
            if quiet:
                continue
        wanted.add(record["cycle_id"])
    return wanted


def _recover_locked(root: Path, *, now: Optional[float] = None,
                    target_campaign_id: Optional[str] = None,
                    open_candidates: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Root-scope crash recovery. Visits every open record and journal entry.

    A record whose campaign is not `target_campaign_id` is isolated (§3.6a):
    a locator defect while resolving *that* record (a hand-copied campaign
    folder, a binding conflict) is reported in ``unresolved`` and the record
    is left exactly as found -- no write, still ``open``, journal untouched --
    so the next sweep retries it unchanged. A record whose campaign *is* the
    target still raises, because that is the campaign this operation touches.
    ``target_campaign_id=None`` (bare ``recover()``) isolates every record.
    """

    result: Dict[str, Any] = {
        "rolled_forward": [], "rolled_back": [], "dropped": [], "open": [], "unresolved": [],
    }
    journal_dir = producer_dir(root) / "journal"
    if journal_dir.is_dir():
        for entry in sorted(journal_dir.glob("*.json")):
            journal = _read_json(entry)
            if journal is None:
                entry.unlink()
                continue
            cycle_id = journal.get("cycle_id", entry.stem)
            record = read_cycle_record(root, cycle_id)
            if record is None:
                entry.unlink()
                result["dropped"].append(cycle_id)
                continue
            manifest_path = root / str(journal.get("cycle_path", "")) / "manifest.json"
            document = _read_json(manifest_path)
            if journal.get("state") == "refreshing" and record.get("state") == "sealed":
                # A refresh of a closed cycle stopped part-way; finish it as it was meant.
                _raise_if_recovery_fenced(root, cycle_id, now=now)
                _refresh_journal_resume(root, record, journal, now=now)
                result["rolled_forward"].append(cycle_id)
                continue
            if document is not None and artifact_manifest.manifest_digest(document) == journal.get("manifest_digest"):
                _raise_if_recovery_fenced(root, cycle_id, now=now)
                try:
                    artifact_locator.prepare_index_update(root, [record["campaign_id"]])
                    _commit_sealed(root, record, document, journal["manifest_digest"], now=now)
                except (artifact_locator.LocatorError, ProducerError) as exc:
                    if not _is_recoverable_locator_defect(exc) or record["campaign_id"] == target_campaign_id:
                        raise
                    result["unresolved"].append({
                        "cycle_id": cycle_id, "campaign_id": record["campaign_id"],
                        "code": exc.code, "detail": exc.detail, "phase": "journal",
                    })
                    continue
                result["rolled_forward"].append(cycle_id)
            else:
                # Publication was prepared from validated bytes. A later edit
                # or deletion is history, not authority for a different close.
                original = artifact_lifecycle.find_manifest_snapshot(
                    root, cycle_id, manifest_digest=journal.get("manifest_digest"))
                if original is not None:
                    _raise_if_recovery_fenced(root, cycle_id, now=now)
                    try:
                        artifact_locator.prepare_index_update(root, [record["campaign_id"]])
                        _commit_sealed(root, record, original, journal["manifest_digest"], now=now)
                    except (artifact_locator.LocatorError, ProducerError) as exc:
                        if not _is_recoverable_locator_defect(exc) or record["campaign_id"] == target_campaign_id:
                            raise
                        result["unresolved"].append({
                            "cycle_id": cycle_id, "campaign_id": record["campaign_id"],
                            "code": exc.code, "detail": exc.detail, "phase": "journal",
                        })
                        continue
                    result["rolled_forward"].append(cycle_id)
                    continue
                if journal.get("directory_bundles"):
                    result["open"].append(cycle_id)
                    continue
                # Crash before the commit point: cycle stays open.
                _drop_unpublished_snapshot(root, cycle_id, journal)
                entry.unlink()
                result["rolled_back"].append(cycle_id)
    # `open_candidates`: what `_recovery_open_candidates` found before the lock; without it
    # (an explicit `recover`) every record is read here.
    records = (list_cycle_records(root) if open_candidates is None else
               [row for row in (read_cycle_record(root, item) for item in sorted(set(open_candidates)))
                if row is not None])
    for record in records:
        if (record.get("state") != "open" or record.get("deleted_at")
                or (record.get("relocation") or {}).get("artifact_root")):
            continue
        try:
            directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
            if not directory.is_dir():
                dropped = dict(record)
                dropped["state"] = "dropped"
                dropped["sealed_on"] = _rfc3339(now)
                _write_cycle_record(root, dropped, exclusive=False)
                result["dropped"].append(record["cycle_id"])
                continue
            if (directory / "manifest.json").is_file():
                document = _read_json(directory / "manifest.json")
                if document is not None:
                    _raise_if_recovery_fenced(root, record["cycle_id"], now=now)
                    digest = artifact_manifest.manifest_digest(document)
                    published = artifact_lifecycle.find_manifest_snapshot(root, record["cycle_id"], manifest_digest=digest)
                    if published is None:
                        # Older interrupted publications may have no copy or
                        # journal. Use their actual terminal proof, never a
                        # state field alone, before admitting a new result.
                        identity = artifact_lifecycle.read_root_identity(root)
                        cycle = document.get("cycle") or {}
                        valid_identity = (identity is not None
                                          and document.get("artifact_root_id") == identity.artifact_root_id
                                          and cycle.get("cycle_id") == record["cycle_id"]
                                          and cycle.get("campaign_id") == record["campaign_id"]
                                          and (document.get("producer") or {}).get("producer_id") == record["producer_id"])
                        try:
                            route_file, _ = resolve_cycle_manifest_route(root, record, document)
                            completion = artifact_lifecycle.evaluate_cycle_completion(
                                document, content_root=directory, route_file=route_file,
                                expected_root_id=identity.artifact_root_id if identity else None)
                            valid = valid_identity and completion.ok
                        except (ProducerError, artifact_lifecycle.LifecycleError, OSError, ValueError):
                            valid = False
                        if not valid:
                            result["open"].append(record["cycle_id"])
                            continue
                    artifact_locator.prepare_index_update(root, [record["campaign_id"]])
                    _commit_sealed(root, record, document, artifact_manifest.manifest_digest(document), now=now)
                    result["rolled_forward"].append(record["cycle_id"])
                    continue
        except (artifact_locator.LocatorError, ProducerError) as exc:
            if not _is_recoverable_locator_defect(exc) or record["campaign_id"] == target_campaign_id:
                raise
            result["unresolved"].append({
                "cycle_id": record["cycle_id"], "campaign_id": record["campaign_id"],
                "code": exc.code, "detail": exc.detail, "phase": "open",
            })
            continue
        result["open"].append(record["cycle_id"])
    shared_dir = producer_dir(root) / "shared-journal"
    if shared_dir.is_dir():
        for entry in sorted(shared_dir.glob("*.json")):
            journal = _read_json(entry)
            try:
                _validate_shared_journal(root, entry, journal)
            except ProducerError as exc:
                result["unresolved"].append({"revision_id": entry.stem, "code": exc.code,
                                             "detail": exc.detail, "phase": "shared"})
                continue
            staging = root / journal["staging"]
            target = root / journal["target"]
            if journal.get("state") == "staging" and staging.is_dir():
                reference = _read_json(_reference_path(root, journal["kind"], journal["reference_id"])) or {}
                if target.exists() or journal["expected_previous_revision_id"] != reference.get("latest_revision_id"):
                    result["unresolved"].append({"revision_id": entry.stem, "code": "shared-base-mismatch",
                                                 "detail": "staging ownership or base changed", "phase": "shared"})
                    continue
                shutil.rmtree(str(staging))
                entry.unlink()
                result["rolled_back"].append(journal.get("revision_id", entry.stem))
            elif target.is_dir():
                try:
                    _commit_shared(root, journal)
                except ProducerError as exc:
                    result["unresolved"].append({"revision_id": journal.get("revision_id"),
                                                 "code": exc.code, "detail": exc.detail, "phase": "shared"})
                    continue
                result["rolled_forward"].append(journal.get("revision_id", entry.stem))
            elif journal.get("state") == "published":
                result["unresolved"].append({"revision_id": entry.stem, "code": "shared-journal-mismatch",
                                             "detail": "published target missing", "phase": "shared"})
            else:
                entry.unlink()
                result["rolled_back"].append(journal.get("revision_id", entry.stem))
    removed_interims = _sweep_orphan_interims(root)
    if removed_interims:
        result["interims_removed"] = removed_interims
    return result
