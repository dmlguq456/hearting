#!/usr/bin/env python3
"""Portable SD-48/49 primitives used by headless dispatch adapters.

Module map. This file holds the terminal claim (`TERMINAL_CLAIM_CONTRACT`,
`claim_terminal_route_locked`, `terminal_claim_observation`,
`ensure_terminal_claim_absent`, read by AST from every runtime root by
`dispatch_runtime_support`, so they must stay defined here) and the code that
checks it before committing a launch or an attempt row (`spawn_claimed_attempt`,
`claim_attempt_row`, `claim_stage_advance`). The rest lives in sibling modules,
bottom layer first; each imports only from the ones above it in this list, and
this file re-exports every name they define:

    dispatch_contract_base      constants, types, registry-row helpers, agent home
    dispatch_contract_roots     state roots, global registry, aliases
    dispatch_contract_policy    attempt metadata validation, headless policy
    dispatch_contract_process   process identity, scans, quiescence, liveness
    dispatch_contract_review    review outputs and governed review leases
    dispatch_contract_governor  model-governor reservations
    dispatch_contract_registry  registry rows, sealed results, advance claims
    dispatch_contract_launch    attempt launch and parent resolution
    dispatch_contract_gates     gates and completion-marker currency
    dispatch_contract_delivery  marker-bound delivery, launch start
    dispatch_contract_rows      attempt rows after launch

A test that replaces a name with `mock.patch` must patch the module whose code
looks the name up; patching it here reaches only the code in this file and
callers that go through `dispatch_contract.<name>`. `split_module_fixture.patch_split`
replaces a name in this file and every part that binds it, as one patch did
before the split.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import contextvars
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import functools
import hashlib
import json
import math
import os
import operator
from pathlib import Path
import re
import signal
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import unicodedata
from typing import Callable, Iterator, Mapping, NamedTuple
from types import MappingProxyType

# Keep the actual import tree visible to the installer's existing /proc in-use
# scan even when argv names ~/.claude/hooks or another rotating projection.
# The descriptor closes on exec/process exit; no runtime state is written.
_CODE_ROOT_FD = os.open(Path(__file__).resolve().parents[1],
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))

from hearting_gates import note_same_work_drift
from session_identity import session_label
from route_identity import registered_node_identity
from governor_identity import close_witness, create_witness
from dispatch_attempt_policy import (decide_attempt, SUBSESSION_NOTE, SUCCESS_NOTES, committed_outcome,
                                     deferred_completion, verdict_pass, success_note,
                                     DEFERRED_COMPLETION_NOTE, DEFERRED_COMPLETION_SOURCE,
                                     terminal_conflict_identity, terminal_conflicts, terminal_conflict_pending)
from dispatch_receipt_identity import receipt_digest as shared_receipt_digest, unseal_receipt


# Sub-session standing is a route authority judgment; the name stays for its readers.
from route_authority import linked_worktree_slice as is_linked_worktree_slice  # noqa: E402
from route_authority import depth1_supported_harnesses, route_in_force  # noqa: E402


# Re-exported so every name this module defined before the split stays an
# attribute of `dispatch_contract` (callers, older releases and tests use them).
from dispatch_contract_base import (  # noqa: F401
    _cancellation_quiescence_receipt_record, _delivery_commit_proven,
    _DELIVERY_INTENT_IMMUTABLE_KEYS, _delivery_intent_values, _DIAGNOSTIC_COUNT_MAX,
    _DIAGNOSTIC_DETAILS, _diff_attribution_downstream_of_execute,
    _diff_attribution_execute_launch_head, _FOREGROUND_FIXED_FAILURES,
    _foreground_review_eligibility_reason, _has_persisted_process_identity,
    _KNOWN_DELIVERY_RECIPIENT_KINDS, _MAX_DELIVERY_RECEIPT_BYTES, _MODULE_ROOT,
    _NO_START_PROCESS_IDENTITY_KEYS, _parse_review_metadata, _PROCESS_IDENTITY_METADATA_KEYS,
    _REGISTRY_METADATA_CACHE_BYTES, _REGISTRY_METADATA_CACHE_SIZE, _registry_metadata_items,
    _REGISTRY_METADATA_SHARED_MAX, _REGISTRY_METADATA_SHARED_VALUE_CHARS, _RegistryCacheInfo,
    _RegistryMetadataCache, _ROUTE_MODULE, _route_module, _SD105_CANCELLED_NOTE,
    _terminal_delivery_receipt, _updated_attempt_metadata, agent_home_equivalent,
    ARTIFACT_PROOF_RECEIPT, ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT, ATTEMPT_DESCENDANT_ENV, CLEANUP_OWNER_ENV,
    ATTEMPT_DESCENDANT_PROOF, ATTEMPT_DESCENDANT_RESIDUE_PROOF, ATTEMPT_MUTABLE_METADATA,
    attempt_row_never_started, ATTEMPT_SCHEMA_VERSION, ATTEMPT_TERMINAL_EVIDENCE_KEYS,
    AttemptExecutionObservation, AttemptReadiness, AuthoritativeProcessIdentity,
    AUTOMATIC_RECEIPTLESS_CLASSIFIER, bounded_candidate_outcomes,
    CANCELLATION_QUIESCENCE_MAX_WAIT_SECONDS, CANCELLATION_QUIESCENCE_POLL_SECONDS,
    candidate_count, candidate_detail, CANDIDATE_PAYLOAD_BYTES, CANONICAL_PARENT_TRANSPORTS,
    canonical_repository_identity, codex_owner_network_posture, codex_standard_owner_network_enabled,
    diagnose_repository_start, diagnostic_line, diagnostic_token, diff_attribution_lines,
    DispatchContractError, ELIGIBILITY, end_with_test, EXECUTION_SURFACES, FALLBACK_HOPS,
    FOREGROUND_FAILURES, FOREGROUND_OUTCOME_KEYS, FOREGROUND_OUTCOME_SOURCE,
    foreground_review_eligible, foreground_review_launch_identity, governance_environment,
    GOVERNOR_RESERVATION_ENV, GROUP_REAP_PROOF, LAUNCH_AUTHORITIES, launch_repository_identity,
    MarkerBoundCompletionProof, MarkerBoundDeliveryResult, NAMESPACE_PORTABLE_TEARDOWN_RECEIPT,
    ObservedAttemptLiveness, owner_closure_shape, PARENT_LIVENESS_METADATA_KEYS,
    PARENT_TRANSPORT_BY_DISPATCH_DEPTH, ParentAttemptBinding, ParentExtinctionEvidence,
    parse_registry_metadata, PID_HOST_NAMESPACE_PROOF, PostClaimAdmission,
    PRELAUNCH_PROCESS_BLOCK_REASONS, PRELAUNCH_WAIT_GRACE_SECONDS,
    PRELAUNCH_WAIT_INTERVAL_MAX_SECONDS, ProcessGroupObservation, ProcessQuiescence,
    QuiescenceProof, RECOVERY_CONTRACT_VERSION, RegistrySelection,
    REPLICA_RESERVATION_ROW_KEYS, resolve_agent_home, resolve_model_governor_root,
    REVIEW_GOVERNED_LEASE_KIND, REVIEW_GOVERNED_LEASE_NONCE_RE,
    REVIEW_OUTPUT_BINDING_SCHEMA_VERSION, REVIEW_POST_CLAIM_METADATA_KEYS,
    ReviewAdmissionCleanup, ReviewHolderDisposition, ROUTE_IDENTITY_METADATA_KEYS,
    route_state_next_action, ROUTE_STATE_REFUSAL_REASONS, row_has_attempt, row_is_subsession,
    sealed_launch_home, SealedForegroundOutcome, SESSION_CHAIN_ID_RE,
    SOURCE_LINEAGE_ROW_FIELD_KEYS, source_lineage_row_fields, STANDARD_PLUS_INTENSITIES,
    SUBSESSION_ID_RE, SUPERVISOR_LEASE_KIND, SUPERVISOR_LEASE_NONCE_RE,
    validated_unversioned_route, work_root_identity, WRAPPER_PARENT_HARNESSES,
    WRAPPER_PARENT_SANDBOXES, WRAPPER_TRANSPORTS,
)
from dispatch_contract_roots import (  # noqa: F401
    _absolute, _alias_digest_well_formed, _alias_record_valid, _ensure_new_directory_mode,
    _ensure_new_file_mode, _fallback_registry, _is_stable_dispatch_root, _mode_bits,
    _registry_safe, _REGISTRY_UNSAFE, _validated_registry_path, _versioned_source_layout,
    bytecode_cache_env, DanglingRegistryResolution, dispatch_state_root, dispatch_state_roots,
    ensure_global_registry_writable, git_optional_locks_env, inherited_jobs_argument,
    launch_mismatch_annotation, LAUNCH_MISMATCH_VALUE_MAX, legacy_read_window_may_close,
    LEGACY_READ_WINDOW_MIN_SUPPORTED_RELEASES, MIGRATION_ALIAS_DIGEST_PATTERN,
    MIGRATION_ALIAS_RECORD_VERSION, MIGRATION_JOURNAL_FILENAME, read_migration_journal,
    resolve_completed_alias, resolve_dangling_registry, resolve_dispatch_state_root,
    resolve_global_registry, stable_state_root, state_root, validate_dispatch_log_dir,
    validate_nested_eligibility,
)
from dispatch_contract_policy import (  # noqa: F401
    _registered_worker, headless_attempt_policy, validate_attempt_metadata,
)
from dispatch_contract_process import (  # noqa: F401
    _artifact_proof_receipt, _ATTEMPT_CARRIER_SCRIPTS, _ATTEMPT_INTERPRETERS,
    _attempt_process_quiescence_impl, _ATTEMPT_RUNTIME_NAMES,
    _cancellation_quiescence_binding_digest, _CANCELLATION_QUIESCENCE_BINDING_KEYS,
    _cancellation_receipt_reason, _canonical_sha256, _cleanup_observation_diagnostic,
    _cleanup_receipt_reason, _current_observer_is_host_like,
    _declared_supervisor_lease_metadata, _denied_process_cgroup,
    _denied_process_outside_attempt, _detached_group_drain_receipt, _foreground_reap_receipt,
    _forked_across_uid_boundary, _in_user_manager_scope, _namespace_extinct_quiescence,
    _open_supervisor_lease, _parent_leader_pids, _PID_NAMESPACE_LINK,
    _portable_cleanup_receipt, _portable_teardown_receipt_digest, _post_exit_receipt_reason,
    _proc_observation, _process_group_from_scan, _PROCESS_TABLE_SCAN,
    _procfs_root_namespace_member, _recorded_launch_cgroup, _recorded_pid_namespace_state,
    _runtime_ancestry_harness_process, _runtime_ancestry_proc_stat,
    _service_cgroup_outside_launch, _settled_cleanup_receipt, _supervisor_lease_metadata_valid,
    _supervisor_lease_payload, _supervisor_lease_payload_matches, _tag_access_observation,
    _tag_scan_authority, _tagged_descendants_from_scan, _tagged_residue_receipt,
    _terminal_cleanup_values, _unified_cgroup_path, _validated_supervisor_lease_path,
    attempt_execution_observation, attempt_governed_process_quiescence,
    attempt_identity_lineage, attempt_process_quiescence, attempt_scan_namespace_authority,
    attempt_tagged_descendants, authoritative_process_identities, cancellation_receipt_reason,
    CLEANUP_RECEIPT_TYPE, exact_process_group_signal_authority, hold_supervisor_lease,
    LAUNCH_CGROUP_METADATA_KEYS, launch_cgroup_values, local_identity_namespace_authority,
    NAMESPACE_EXTINCT_REASON, namespace_gone, observed_attempt_liveness,
    observed_supervised_owner_liveness, observer_namespace_extinct,
    PARENT_EXTINCTION_TERMINAL_STATUSES, pidfd_open, post_exit_receipt_reason,
    process_cgroup_namespace_identity, process_group_members, process_group_observation,
    process_identity_disposition, process_identity_is_live, process_launch_cgroup,
    process_launch_identity, process_namespace_identity, process_namespace_pids,
    process_observation, process_start_ticks, process_state, process_table_scan_scope,
    process_unified_cgroup, ProcessTableScan, prove_attempt_quiescence,
    recorded_pid_namespace_state, remove_supervisor_lease, residue_live_pids,
    resolve_parent_extinction, runtime_ancestry_binding, scan_process_table,
    signal_exact_process_group, supervisor_lease_is_held, supervisor_lease_path,
    tagged_residue_receipt, terminal_receipt_incomplete,
)
from dispatch_contract_review import (  # noqa: F401
    _review_epoch, decode_review_output_locator, encode_review_output_locator,
    review_governed_lease_is_held, review_governed_lease_path, review_governed_lease_payload,
    review_holder_disposition, review_lease_record_digest, review_output_binding_digest,
    review_output_write_authorized, validate_review_output_binding,
)
from dispatch_contract_governor import (  # noqa: F401
    _governor_json, _GOVERNOR_WITNESS_HANDLES, _governor_witness_key, _return_governor_witness,
    _validate_replica_reservation, cancel_governor_reservation, governor_refusal_fields,
    replica_batch_expectation, reservation_claim_timeout, RESERVATION_CLAIM_TIMEOUT_DEFAULT,
    RESERVATION_CLAIM_TIMEOUT_ENV, reserve_governor_token, wait_governor_reservation_claim,
)
from dispatch_contract_registry import (  # noqa: F401
    _append_registry_rows, _atomic_registry_replace, _FOREGROUND_FAILURE_SUFFIX,
    _foreground_outcome_values, _foreground_outcome_values_from_pipe, _foreground_row,
    _FOREGROUND_SIGNED_INTEGER, _immutable_attempt_identity, _RegistryParse,
    _review_watchdog_outcome_identity, _stable_recovery_attempt_id,
    _stable_stage_advance_attempt_id, _stage_advance_claim_key_digest, attempt_raw_row_sha256,
    claim_subsession_advance, detached_review_outcome_from_pipe, launched_attempt_identity,
    new_attempt_id, seal_cancellation_quiescence_receipt, seal_detached_review_result,
    seal_foreground_result, StageAdvanceRegistryClaim, SubsessionAdvanceRegistryClaim,
)
from dispatch_contract_launch import (  # noqa: F401
    _abort_fenced_launch, _dependencies_kept, _launch_cancellation_routes,
    _launch_completion_input, _launch_completion_marker_input, _LAUNCH_COMPLETION_READS,
    _launch_completion_reads, _launch_dependencies, _launch_identity_complete,
    _launch_parent_observation, _launch_registry_transaction, _launch_row, _launch_row_value,
    _launch_serialization_lock, _merge_review_cleanup_group_proof,
    _parent_binding_is_live_from_metadata, _parent_liveness_evidence,
    _parent_metadata_matches_binding, _prepare_attempt_launch,
    _record_review_admission_failure, _review_admission_cleanup_token,
    _unverified_review_admission_cleanup, adapter_launch_failure_outcome,
    parent_attempt_binding_is_live, parent_completion_window, parent_lookup_worktree,
    ParentCompletionWindow, resolve_live_parent_attempt,
)
from dispatch_contract_gates import (  # noqa: F401
    _auxiliary_arbitration_gate, _completion_instant, _evidence_mtime_text,
    _frame_capacity_failures, _frame_pair_attempt_gate, _human_gate_entry_fence,
    _marker_link_current, _marker_provenance_currency, _marker_schema_identity_ok,
    _older_than_upstream, _owner_executed_node, _release_proof_or_refuse,
    _resource_stage_marker_current, _review_report_hash_receipt, _sibling_attempt_gate,
    _superseded_marker_predecessor, _upstream_completed_later, ANSWERED_CURRENCY_STATES,
    completed_marker_verdict_contradicts, completion_attempt_readiness,
    completion_conflict_attempt, completion_marker_gate, completion_marker_is_current,
    completion_superseded, evidence_change_history, evidence_change_history_path,
    evidence_currency, evidence_digest, FENCED_HUMAN_GATES, frame_harness_admission,
    gate_currency, GATE_RECEIPT_DELIVERY_ENV, GateCurrency, human_gate_entry_fence,
    note_evidence_change, observe_terminal_review_failure, original_review_self_claim,
    owner_frame_launch_gate, owner_operation_fence, OWNER_OPERATION_GATES,
    owner_operation_gates, raise_preview_gate_for_node, record_evidence_change,
    record_frame_launch_degradation, recover_preview_gate_after_refusal, reopened_output_path,
    resource_completion_reuse_proof, resource_stage_next_action, RESOURCE_STAGE_UNOBSERVABLE,
    resource_stage_unobservable, RESOURCE_STAGE_UNSETTLED, resource_stage_unsettled,
    review_self_blocked_basis, review_self_disposition_lineage, review_self_disposition_origin,
    review_self_disposition_subject, REVIEW_SELF_PROVENANCE_BLOCKER,
)
from dispatch_contract_delivery import (  # noqa: F401
    _marker_bound_current_marker, _marker_bound_latest_rows,
    _marker_bound_prepare_marker_proof, _MARKER_BOUND_PROCESS_KEYS, _marker_bound_row_verdict,
    attempt_launch_is_available, mark_attempt_launch_started,
    marker_bound_delivery_transaction, marker_bound_process_identity,
    recover_unstarted_attempt, SUBSESSION_CHAIN_REFUSAL_CLASSIFIER,
    SUBSESSION_TERMINAL_CLASSIFIER,
)
from dispatch_contract_rows import (  # noqa: F401
    _automatic_retry_admission, _commit_attempt_terminal, _never_launched_same_work,
    _resolve_attempt_cleanup, _resolve_residue_drain, _retry_parent_matches,
    _retry_registration_census, _retry_route_related, _review_source_registration_fence,
    _row_identity, _settlement_metadata_pipe, annotate_attempt_row, annotate_attempt_row_if,
    attempt_launch_state, attempt_row_terminal_never_started,
    attempt_row_withdrawn_before_launch, close_attempt_row, close_attempt_row_if,
    close_refused_chain_rows, existing_attempt_launch_state, is_runtime_owner_launch,
    launch_orphan_watch, launch_reap_watch, probe_owner_supervision,
    reconcile_attempt_terminal, reconcile_local_registry, RefusedChainClose,
    resolve_attempt_cleanup, resolve_residue_drain, resolve_terminal_conflict,
    SupervisionProbe, terminal_attempt_never_started, workflow_completion_receipt,
)

# Moved classes report the module they were defined in before the split, so
# tracebacks, reprs and pickles read as before (`dispatch_contract.<Class>`).
for _moved_class in (
    AttemptExecutionObservation, AttemptReadiness, AuthoritativeProcessIdentity,
    DanglingRegistryResolution, DispatchContractError, GateCurrency,
    MarkerBoundCompletionProof, MarkerBoundDeliveryResult, ObservedAttemptLiveness,
    ParentAttemptBinding, ParentCompletionWindow, ParentExtinctionEvidence, PostClaimAdmission,
    ProcessGroupObservation, ProcessQuiescence, ProcessTableScan, QuiescenceProof,
    RefusedChainClose, RegistrySelection, ReviewAdmissionCleanup, ReviewHolderDisposition,
    SealedForegroundOutcome, StageAdvanceRegistryClaim, SubsessionAdvanceRegistryClaim,
    SupervisionProbe, _RegistryCacheInfo, _RegistryMetadataCache, _RegistryParse,
):
    if _moved_class.__module__.startswith("dispatch_contract_"):
        _moved_class.__module__ = __name__
del _moved_class


# SD-120/121 terminal claim primitive.  The caller must already own the
# existing jobs.log.lock; this module never acquires another lock for a claim.
TERMINAL_CLAIM_CONTRACT = "terminal-claim/v1"


def terminal_claim_path(jobs: str | Path, route_id: str, owner_attempt_id: str) -> Path:
    """Derive the sole claim record below the dispatch state root."""
    root = dispatch_state_root(jobs)
    safe = lambda value: re.fullmatch(r"[A-Za-z0-9._-]+", str(value)) is not None
    if not safe(route_id) or not safe(owner_attempt_id):
        raise DispatchContractError("terminal-claim-identity-invalid", "unsafe component")
    return root / "terminal-fences" / "v1" / str(route_id) / str(owner_attempt_id) / "claim.json"


def _terminal_claim_lock_owned(lock_fd: object, jobs: str | Path | None = None) -> bool:
    """Validate the explicit lock handle without trying to acquire a lock."""
    try:
        if not (isinstance(lock_fd, int) and lock_fd >= 0):
            return False
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode):
            return False
        try:
            target = os.path.realpath(os.readlink(f"/proc/self/fd/{lock_fd}"))
            if jobs is not None:
                expected_path = str(Path(jobs).resolve()) + ".lock"
                if target != expected_path:
                    return False
            elif not target.endswith("jobs.log.lock"):
                return False
            expected = os.stat(target)
            held = Path(f"/proc/self/fdinfo/{lock_fd}").read_text()
            owns_write_lock = any("FLOCK" in line.split() and "WRITE" in line.split()
                                  for line in held.splitlines() if line.startswith("lock:"))
            return owns_write_lock and (info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino)
        except (OSError, ValueError):
            return False
    except (OSError, TypeError, ValueError):
        return False


def _terminal_claim_read(path: Path) -> dict | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            value = json.loads(handle.read().decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise DispatchContractError("terminal-claim-record-invalid", str(path)) from exc
    if not isinstance(value, dict):
        raise DispatchContractError("terminal-claim-record-invalid", str(path))
    return value


def terminal_claim_observation(jobs: str | Path, route_id: str, owner_attempt_id: str) -> dict | None:
    """Non-blocking atomic single-read; callers use this inside their jobs lock."""
    return _terminal_claim_read(terminal_claim_path(jobs, route_id, owner_attempt_id))


def ensure_terminal_claim_absent(jobs: str | Path, route_id: str,
                                 owner_attempt_id: str) -> None:
    """Reject a registry mutation after the exact terminal claim is published.

    This is deliberately only an atomic single-read.  The caller owns the
    existing ``jobs.log.lock`` critical section; this helper never acquires a
    lock and therefore cannot change the established lock order.
    """
    # A bounded cleanup may precede a terminal commit (e.g. failed markers).
    # Its caller still cannot create a fresh route/child to escape the scope.
    if os.environ.get("AGENT_DISPATCH_ATTEMPT_ID"):
        from dispatch_terminal_commit import require_current_cleanup
        require_current_cleanup("dispatch", jobs=jobs)
    # Existing child/retry/marker writers carry a child attempt, whereas the
    # fence belongs to its registered owner. Resolve that identity under the
    # caller's jobs lock instead of probing a child-named fence that cannot exist.
    identities = {owner_attempt_id}
    for line in Path(jobs).read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") == owner_attempt_id and metadata.get("parent_attempt_id"):
            identities.add(metadata["parent_attempt_id"])
    if route_id and any(identity and terminal_claim_observation(jobs, route_id, identity) is not None
                        for identity in identities):
        raise DispatchContractError(
            "terminal-claim-conflict",
            f"route_id={route_id},owner_attempt_id={owner_attempt_id}",
        )


def claim_terminal_route_locked(jobs: str | Path, route_id: str, owner_attempt_id: str,
                                *, lock_fd: int | None = None, proof: dict | None = None) -> dict:
    """Publish an immutable claim while the caller's jobs lock is held.

    ``lock_fd`` is the already-open jobs.log.lock descriptor.  The helper does
    not acquire, flock, or release it; this preserves node -> jobs -> producer
    lock ordering and makes subprocess lock ownership an explicit boundary.
    """
    if not _terminal_claim_lock_owned(lock_fd, jobs):
        raise DispatchContractError("terminal-claim-requires-jobs-lock", "lock_fd")
    path = terminal_claim_path(jobs, route_id, owner_attempt_id)
    record = {"schema_version": 1, "contract": TERMINAL_CLAIM_CONTRACT,
              "route_id": route_id, "owner_attempt_id": owner_attempt_id,
              "proof": proof or {}}
    data = json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        existing = _terminal_claim_read(path)
        if existing == record:
            return existing
        raise DispatchContractError("terminal-claim-conflict", str(path))
    fd, temporary_name = tempfile.mkstemp(prefix=".claim-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _terminal_claim_read(path) != record:
                raise DispatchContractError("terminal-claim-conflict", str(path))
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        temporary.unlink(missing_ok=True)
    return record

from dispatch_pending_delivery import RECIPIENT_KINDS
from replica_batch_contract import (
    DIGEST,
    ReplicaBatchContractError,
    verify_manifest,
)
from stage_session_contract import load_manifest


# One copy of the terminal capacity judgment (route_authority); the name stays for readers.
from route_authority import anchored_capacity_failure  # noqa: E402,F401


def _launch_local_fences(jobs, metadata, cancellation_routes, *, registration=False):
    """Fresh local terminal claims and close journals, never route/source readers.

    These existing records live beside jobs, and their writers serialize with
    this same jobs lock. Git, route and foreign-parent observations complete
    outside it. The commit also rechecks exact local PID/start or lease liveness.
    """
    import route_parent_close
    if route_parent_close.requested(metadata):
        raise DispatchContractError("cancelled-by-parent")
    owner = metadata.get("parent_attempt_id") or metadata.get("attempt_id")
    route_ids = {metadata.get("route_id"), metadata.get("owner_route_id")}
    for route_id in sorted(value for value in route_ids if value and value != "-"):
        ensure_terminal_claim_absent(jobs, route_id, owner)
    for route_id, route_hash in cancellation_routes:
        intent = route_parent_close.intent({"route_id": route_id, "route_hash": route_hash}, jobs)
        # A closed route cannot admit a new successor that was never in its
        # old attempt census. Spawn only tests its already registered child.
        if intent and (registration or metadata.get("attempt_id") in intent["attempts"]):
            raise DispatchContractError("cancelled-by-parent")


def _commit_launch_preparation(jobs, fresh, prepared, *, parent_binding, preclaim, phase):
    lines, _index, fields, metadata, routes, reads = prepared
    index, current_fields, current_meta = _launch_row(
        fresh, metadata["attempt_id"], reason=f"attempt-{phase}-row-not-unique")
    _launch_local_fences(jobs, current_meta, routes)
    if (_launch_row_value(current_fields) != _launch_row_value(fields)
            or not _dependencies_kept(jobs, _launch_dependencies(lines, fields, (preclaim,), reads),
                                      _launch_dependencies(fresh, fields, (preclaim,), reads))):
        raise DispatchContractError(f"attempt-{phase}-identity-changed", metadata["attempt_id"])
    if parent_binding is not None:
        # A parent can exit while we wait for jobs.lock even when its row is
        # unchanged. Only exact local PID/start or the already resolved local
        # supervisor lease belongs here; NAS/foreign observations stay outside.
        _parent_index, parent_fields, parent_meta = _launch_row(fresh, parent_binding.attempt_id)
        live = False
        if parent_fields[1] in {"open", "running"} and _parent_metadata_matches_binding(parent_meta, parent_binding):
            if parent_binding.observed_pid is not None:
                live = process_identity_is_live(parent_binding.observed_pid, parent_binding.observed_pid_start)
            elif parent_binding.liveness_source == "supervisor-lease":
                live = supervisor_lease_is_held(jobs, parent_meta)
        if not live:
            suffix = "after-post-claim" if phase == "post-claim" else "after-spawn"
            raise DispatchContractError("parent-attempt-not-live-" + suffix, parent_binding.attempt_id)
    return index, current_fields, current_meta


def spawn_claimed_attempt(
    jobs: Path,
    attempt_id: str,
    *,
    parent_binding: ParentAttemptBinding | None,
    spawn: Callable[[int], subprocess.Popen],
    launch_metadata: dict[str, str] | None = None,
    preclaim: Callable[[list[str]], None] | None = None,
    pre_release: Callable[[dict[str, str]], dict[str, str] | None] | None = None,
    post_claim: Callable[[dict[str, str]], dict[str, str] | PostClaimAdmission | None] | None = None,
    respawn_of: tuple[str, str] | None = None,
) -> tuple[subprocess.Popen, dict[str, str]]:
    """Prepare externally, publish an exact fenced identity, then release once.

    Source/route/process observers and all callbacks run with neither global
    jobs nor legacy global launch lock held. Per-attempt serialization spans
    preparation and cleanup. Fresh registry transactions arbitrate against both
    current and legacy launchers, cancellation and parent/lineage mutations.
    """
    if not attempt_id:
        raise DispatchContractError("attempt-id-required")
    ensure_global_registry_writable(jobs)
    with _launch_serialization_lock(jobs, attempt_id):
        initial = _prepare_attempt_launch(jobs, attempt_id, parent_binding=parent_binding,
                                          respawn_of=respawn_of, preclaim=preclaim)
        gate_read, gate_write = os.pipe()
        try:
            proc = spawn(gate_read)
        except BaseException:
            os.close(gate_write)
            raise
        finally:
            os.close(gate_read)
        identity = {}
        admission = None
        admission_cleanup = None
        released = False
        failure_context = "launch-preparation-failed"

        def cleanup_failure(reason):
            nonlocal admission_cleanup
            watchdog_owned = admission is not None and bool(admission.metadata.get("review_fence_pid"))
            returned = None
            if watchdog_owned:
                if not released:
                    try:
                        os.close(gate_write)
                    except OSError:
                        pass
                try:
                    returned = admission.abort(reason)
                except BaseException:
                    returned = _unverified_review_admission_cleanup()
                if returned is None:
                    returned = _unverified_review_admission_cleanup()
                group_empty = (attempt_scan_namespace_authority(identity) and proc.poll() is not None
                               and process_group_observation(int(identity["pgid"])).state == "empty")
            else:
                group_empty = _abort_fenced_launch(proc, -1 if released else gate_write,
                                                   identity.get("pid_start", ""))
                if admission is not None:
                    try:
                        returned = admission.abort(reason)
                    except BaseException:
                        returned = _unverified_review_admission_cleanup()
                    if returned is None:
                        returned = _unverified_review_admission_cleanup()
            if returned is not None:
                admission_cleanup = returned
            admission_cleanup = _merge_review_cleanup_group_proof(admission_cleanup, group_empty)
            annotated = True
            if admission is not None or admission_cleanup is not None:
                annotated = _record_review_admission_failure(
                    jobs, attempt_id, admission_cleanup, post_release=released)
            return (bool(group_empty) and bool(annotated)
                    and (admission_cleanup is None
                         or _review_admission_cleanup_token(admission_cleanup) != "unverified"))

        try:
            identity = process_launch_identity(proc.pid)
            identity.update(process_launch_cgroup(proc.pid))
            provided = {key: str(value) for key, value in (launch_metadata or {}).items()
                        if value not in (None, "")}
            conflicts = sorted((_PROCESS_IDENTITY_METADATA_KEYS | set(LAUNCH_CGROUP_METADATA_KEYS))
                               .intersection(provided))
            if conflicts:
                raise DispatchContractError("attempt-launch-identity-metadata-conflict", ",".join(conflicts))
            if not _launch_identity_complete(proc.pid, identity):
                raise DispatchContractError("attempt-launch-identity-incomplete", f"pid={proc.pid}")
            observer = {}
            if pre_release is not None:
                try:
                    observer = {key: str(value) for key, value in (pre_release(dict(identity)) or {}).items()
                                if value not in (None, "")}
                except BaseException as exc:
                    raise DispatchContractError("attempt-pre-release-callback-failed", str(exc)) from exc
            conflicts = sorted(({*_PROCESS_IDENTITY_METADATA_KEYS, *LAUNCH_CGROUP_METADATA_KEYS,
                                 "launch_claimed"}).intersection(observer))
            if conflicts:
                raise DispatchContractError("attempt-pre-release-metadata-conflict", ",".join(conflicts))
            identity.update(provided)
            identity.update(observer)
            if respawn_of is not None:
                lineage = []
                for entry in initial[3].get("owner_identity_lineage", "").split("+"):
                    pid, sep, start = entry.partition(":")
                    if sep and pid.isdigit() and start.isdigit() and entry not in lineage:
                        lineage.append(entry)
                previous = ":".join(map(str, respawn_of))
                if previous not in lineage:
                    lineage.append(previous)
                identity["owner_identity_lineage"] = "+".join(lineage)
            prepared = _prepare_attempt_launch(
                jobs, attempt_id, parent_binding=parent_binding, respawn_of=respawn_of,
                preclaim=preclaim, expected_fields=initial[2])
            failure_context = "launch-identity-record-failed"
            try:
                with _launch_registry_transaction(jobs) as fresh:
                    index, fields, _meta = _commit_launch_preparation(
                        jobs, fresh, prepared, parent_binding=parent_binding,
                        preclaim=preclaim, phase="pre-release")
                    parts = [part for part in fields[5].split(",")
                             if part.split("=", 1)[0] not in {*identity, "launch_claimed"}]
                    parts.extend(f"{key}={value}" for key, value in sorted(identity.items()))
                    parts.append("launch_claimed=1")
                    fields[5] = ",".join(parts)
                    fresh[index] = "\t".join(fields)
                    _atomic_registry_replace(jobs, fresh)
                    published_fields = list(fields)
            except OSError as exc:
                raise DispatchContractError("attempt-launch-identity-record-failed", str(exc)) from exc
            post_metadata = {}
            if post_claim is not None:
                failure_context = "post-claim-callback-failed"
                try:
                    raw = post_claim(dict(identity))
                    if isinstance(raw, PostClaimAdmission):
                        admission, raw = raw, raw.metadata
                    if raw is not None and not isinstance(raw, Mapping):
                        raise DispatchContractError("attempt-post-claim-result-invalid", type(raw).__name__)
                    post_metadata = {key: str(value) for key, value in (raw or {}).items()
                                     if value not in (None, "")}
                except BaseException as exc:
                    if isinstance(getattr(exc, "cleanup", None), ReviewAdmissionCleanup):
                        admission_cleanup = exc.cleanup
                    raise DispatchContractError("attempt-post-claim-callback-failed", str(exc)) from exc
                invalid = sorted(set(post_metadata) - REVIEW_POST_CLAIM_METADATA_KEYS)
                invalid_values = sorted(key for key, value in post_metadata.items()
                                        if any(char in value for char in ",\t\r\n"))
                if invalid or invalid_values:
                    failure_context = "post-claim-metadata-invalid"
                    raise DispatchContractError("attempt-post-claim-metadata-invalid",
                                                ",".join(invalid or invalid_values))
            failure_context = "post-claim-record-failed"
            # Every launch, including one with no observer/admission callback,
            # observes full content immediately before the final fresh commit.
            prepared = _prepare_attempt_launch(
                jobs, attempt_id, parent_binding=parent_binding, respawn_of=respawn_of,
                preclaim=preclaim, expected_fields=published_fields, published=True, phase="post-claim")
            with _launch_registry_transaction(jobs) as fresh:
                index, fields, meta = _commit_launch_preparation(
                    jobs, fresh, prepared, parent_binding=parent_binding,
                    preclaim=preclaim, phase="post-claim")
                collisions = sorted(key for key, value in post_metadata.items()
                                    if meta.get(key) not in (None, value))
                if collisions:
                    raise DispatchContractError("attempt-post-claim-metadata-conflict", ",".join(collisions))
                if post_metadata:
                    parts = [part for part in fields[5].split(",")
                             if part.split("=", 1)[0] not in post_metadata]
                    parts.extend(f"{key}={value}" for key, value in sorted(post_metadata.items()))
                    fields[5] = ",".join(parts)
                    fresh[index] = "\t".join(fields)
                    _atomic_registry_replace(jobs, fresh)
                    identity.update(post_metadata)
                failure_context = "gate-write-failed"
                try:
                    os.write(gate_write, b"1")
                except OSError as exc:
                    raise DispatchContractError("attempt-launch-fence-release-failed", str(exc)) from exc
                released = True
                os.close(gate_write)
            if admission is not None:
                failure_context = "post-release-commit-failed"
                try:
                    admission.commit()
                except BaseException as exc:
                    raise DispatchContractError("attempt-post-claim-commit-failed", str(exc)) from exc
            return proc, identity
        except BaseException as exc:
            verified = cleanup_failure(failure_context)
            if not verified:
                raise DispatchContractError("attempt-launch-cleanup-unverified", str(exc)) from exc
            if isinstance(exc, OSError):
                raise DispatchContractError("attempt-post-claim-record-failed", str(exc)) from exc
            raise


def claim_stage_advance(
    jobs: Path,
    *,
    stage_advance_id: str,
    route_hash: str,
    successor_node: str,
    advance_generation: int,
    source_route_id: str,
    predecessor_attempt_id: str,
) -> StageAdvanceRegistryClaim:
    """CAS one stable stage-advance claim inside `<jobs>.lock`, or replay the
    identical claim for a repeated `stage_advance_id` (A-16). A distinct
    `claim_key` already claimed by a DIFFERENT `stage_advance_id` is
    `stage-advance-claim-conflict` (A-4) -- one generation, one successor, one
    claimant, no matter which predecessor closed first.
    """

    if (
        not stage_advance_id.startswith("sadv-")
        or not route_hash
        or not successor_node
        or not source_route_id
        or not predecessor_attempt_id
        or type(advance_generation) is not int
    ):
        raise DispatchContractError("stage-advance-claim-identity-incomplete")
    ensure_global_registry_writable(jobs)
    claim_key = (route_hash, successor_node, advance_generation)
    claims_dir = jobs.parent / "stage_advance" / "claims"
    claim_path = claims_dir / f"{_stage_advance_claim_key_digest(claim_key)}.json"
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        ensure_terminal_claim_absent(jobs, source_route_id, predecessor_attempt_id)
        if claim_path.is_file():
            try:
                existing = json.loads(claim_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise DispatchContractError("stage-advance-claim-record-invalid") from exc
            if existing.get("stage_advance_id") == stage_advance_id:
                return StageAdvanceRegistryClaim(
                    stage_advance_id=stage_advance_id,
                    claim_key=claim_key,
                    successor_attempt_id=existing["successor_attempt_id"],
                    replayed=True,
                )
            raise DispatchContractError("stage-advance-claim-conflict")
        successor_attempt_id = _stable_stage_advance_attempt_id(
            stage_advance_id, successor_node
        )
        record = {
            "stage_advance_id": stage_advance_id,
            "claim_key": list(claim_key),
            "successor_attempt_id": successor_attempt_id,
            "source_route_id": source_route_id,
            "predecessor_attempt_id": predecessor_attempt_id,
            "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        claims_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".claim.", dir=str(claims_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, claim_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    return StageAdvanceRegistryClaim(
        stage_advance_id=stage_advance_id,
        claim_key=claim_key,
        successor_attempt_id=successor_attempt_id,
        replayed=False,
    )


# Stable facts of one attempt's work. A row whose launcher never claimed it may be
# relaunched by a new launcher whose per-launch values differ; the one definition of
# "same work, another launcher" is `route_authority`'s.
from route_authority import RELAUNCH_STABLE_KEYS as _RELAUNCH_STABLE_KEYS  # noqa: E402


def claim_attempt_row(
    jobs: Path,
    attempt_id: str,
    row: str,
    *,
    launch: bool = False,
    exclusive_metadata: dict[str, str] | None = None,
    exclusive_live_metadata: dict[str, str] | None = None,
    terminal_attempt_limit: int | None = None,
    replacement_attempt_limit: int = 0,
    replacement_notes: frozenset[str] = frozenset(),
    mutation_precheck: Callable[[list[str]], None] | None = None,
    preclaim: Callable[[list[str]], None] | None = None,
) -> bool:
    """Prepare external admission, then commit one fresh local registry transition.

    Same-attempt preparation is serialized; unrelated jobs never wait for its
    source, route, marker, process or arbitrary callback observations.
    """
    if not attempt_id:
        raise DispatchContractError("attempt-id-required", "registered dispatches require an attempt id")
    fields = row.rstrip("\n").split("\t")
    if len(fields) != 6:
        raise DispatchContractError("invalid-registry-row", "expected six tab-separated fields")
    metadata = parse_registry_metadata(fields[5])
    validate_attempt_metadata(metadata)
    if metadata.get("attempt_id") != attempt_id:
        raise DispatchContractError("attempt-row-identity-mismatch", attempt_id)
    ensure_global_registry_writable(jobs)
    with _launch_serialization_lock(jobs, attempt_id), _launch_completion_reads() as reads:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        parsed = _RegistryParse()
        from dispatch_replacement import replacement_row
        row, replacement = replacement_row(jobs, lines, row)
        fields = row.split("\t")
        metadata = parse_registry_metadata(fields[5])
        existing = [list(entry[0]) for entry in parsed.rows(lines)
                    if entry is not None and entry[1].get("attempt_id") == attempt_id]
        from review_input import validate_launch
        if mutation_precheck is not None:
            mutation_precheck(lines)
        eligible = not existing or (len(existing) == 1 and existing[0][1] == "open"
                     and parse_registry_metadata(existing[0][5]).get("launch_claimed") != "1")
        if launch and eligible:
            _automatic_retry_admission(lines, metadata, jobs=jobs)
            if preclaim is not None:
                preclaim(lines)
        # Seal/observe source after permission and mutation callbacks, without
        # adding another full inventory to registration.
        fields, metadata = _review_source_registration_fence(
            jobs, fields, metadata, new_attempt=not existing)
        # Revision admission consumes the sealed subject (including document
        # identity across checklist progress), as it did before unlocked prep.
        validate_launch(jobs, metadata)
        routes = _launch_cancellation_routes(jobs, lines, metadata, parsed)
        return _commit_attempt_row(
            jobs, attempt_id, "\t".join(fields), prepared_lines=lines, prepared_parse=parsed,
            prepared_replacement=replacement, prepared_reads=reads, cancellation_routes=routes, launch=launch,
            exclusive_metadata=exclusive_metadata, exclusive_live_metadata=exclusive_live_metadata,
            terminal_attempt_limit=terminal_attempt_limit, replacement_attempt_limit=replacement_attempt_limit,
            replacement_notes=replacement_notes, mutation_precheck=mutation_precheck, preclaim=preclaim)


def _commit_attempt_row(
    jobs: Path,
    attempt_id: str,
    row: str,
    *,
    prepared_lines: list[str],
    prepared_parse: _RegistryParse | None = None,
    prepared_replacement: bool,
    prepared_reads: dict,
    cancellation_routes: tuple,
    launch: bool = False,
    exclusive_metadata: dict[str, str] | None = None,
    exclusive_live_metadata: dict[str, str] | None = None,
    terminal_attempt_limit: int | None = None,
    replacement_attempt_limit: int = 0,
    replacement_notes: frozenset[str] = frozenset(),
    mutation_precheck: Callable[[list[str]], None] | None = None,
    preclaim: Callable[[list[str]], None] | None = None,
) -> bool:
    if not attempt_id:
        raise DispatchContractError("attempt-id-required", "registered dispatches require an attempt id")
    row_fields = row.rstrip("\n").split("\t")
    if len(row_fields) != 6:
        raise DispatchContractError("invalid-registry-row", "expected six tab-separated fields")
    row_metadata = parse_registry_metadata(row_fields[5])
    validate_attempt_metadata(row_metadata)
    if row_metadata.get("attempt_id") != attempt_id:
        raise DispatchContractError("attempt-row-identity-mismatch", attempt_id)
    ensure_global_registry_writable(jobs)
    # The rows the preparation parsed keep their parse; only rows written since are new text.
    parsed = prepared_parse or _RegistryParse()
    lock_path = Path(f"{jobs}.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        rows = parsed.rows(lines)
        # Arbitrary external callbacks have already run. Pin their input and
        # the prepared lineage, then run only fresh local census/fence checks.
        _launch_local_fences(jobs, row_metadata, cancellation_routes, registration=True)
        # A terminal writer or a legacy launcher may win during preparation.
        # Preserve idempotent False for that exact work, including close races.
        for entry in rows:
            if entry is not None and entry[1].get("attempt_id") == attempt_id:
                fields, metadata = entry
                if (_immutable_attempt_identity(fields) == _immutable_attempt_identity(row_fields)
                        and (not launch or metadata.get("launch_claimed") == "1"
                             or fields[1] != "open")):
                    return False
        def pin_preparation():
            callbacks = (mutation_precheck, preclaim if launch else None)
            if not _dependencies_kept(
                    jobs, _launch_dependencies(prepared_lines, row_fields, callbacks, prepared_reads, parsed),
                    _launch_dependencies(lines, row_fields, callbacks, prepared_reads, parsed)):
                raise DispatchContractError("attempt-registration-dependency-changed", attempt_id)

        automatic_replacement = prepared_replacement
        # A serial successor consumes its predecessors just as a DAG edge
        # consumes markers. Recheck the same conflict policy at actual claim,
        # including an observation arriving after the coordinator's preview.
        if launch and row_metadata.get("session_chain_id"):
            for entry in rows:
                if entry is None:
                    continue
                prior = entry[1]
                if (prior.get("session_chain_id") == row_metadata["session_chain_id"]
                        and terminal_conflict_pending(prior)):
                    raise DispatchContractError("terminal-evidence-conflict", prior.get("attempt_id", ""))
        for index, entry in enumerate(rows):
            if entry is not None and entry[1].get("attempt_id") == attempt_id:
                fields, metadata = list(entry[0]), entry[1]
                validate_attempt_metadata(metadata)
                if _immutable_attempt_identity(fields) != _immutable_attempt_identity(row_fields):
                    if not _never_launched_same_work(fields, metadata, row_fields, row_metadata):
                        raise DispatchContractError(
                            "attempt-identity-conflict",
                            f"attempt_id={attempt_id}",
                        )
                    # A launcher stopped before its claim left only this row; the
                    # new launcher's row (its own lease nonce, release, parent
                    # runtime) takes its place.
                    fields = list(row_fields)
                    if not launch:
                        pin_preparation()
                        lines[index] = "\t".join(fields) + ",launch_claimed=0"
                        _atomic_registry_replace(jobs, lines)
                        return False
                if not launch or metadata.get("launch_claimed") == "1" or fields[1] != "open":
                    return False
                _retry_registration_census(lines, row_metadata)
                pin_preparation()
                pipe = ",".join(part for part in fields[5].split(",") if not part.startswith("launch_claimed="))
                fields[5] = pipe + ",launch_claimed=1"
                lines[index] = "\t".join(fields)
                _atomic_registry_replace(jobs, lines)
                return True
        if exclusive_metadata:
            for entry in rows:
                if entry is None:
                    continue
                metadata = entry[1]
                if all(metadata.get(key) == value for key, value in exclusive_metadata.items()):
                    return False
        if exclusive_live_metadata:
            matching_terminal_attempts = set()
            replacement_attempts = set()
            for entry in rows:
                if entry is None:
                    continue
                fields, metadata = entry
                if not all(
                    metadata.get(key) == value
                    for key, value in exclusive_live_metadata.items()
                ):
                    continue
                validate_attempt_metadata(metadata)
                if fields[1] in {"open", "running"}:
                    return False
                if fields[1] == "done" and metadata.get("attempt_id"):
                    # A failed terminal note in replacement_notes counts
                    # against the separate replacement budget instead of the
                    # ordinary terminal_attempt_limit -- a success (note
                    # absent, or a passing note like completed-marker /
                    # completed-supervisor) still counts as ordinary so a
                    # duplicate launch after success stays refused.
                    if metadata.get("note") in replacement_notes:
                        replacement_attempts.add(metadata["attempt_id"])
                    else:
                        matching_terminal_attempts.add(metadata["attempt_id"])
            if (
                not automatic_replacement
                and terminal_attempt_limit is not None
                and len(matching_terminal_attempts) >= terminal_attempt_limit
            ):
                raise DispatchContractError(
                    "quick-registered-headless-exhausted",
                    f"terminal_attempts={len(matching_terminal_attempts)} limit={terminal_attempt_limit}",
                )
            if not automatic_replacement and len(replacement_attempts) > replacement_attempt_limit:
                raise DispatchContractError(
                    "quick-replacement-attempts-exhausted",
                    f"replacement_attempts={len(replacement_attempts)} limit={replacement_attempt_limit}",
                )
        if launch:
            _retry_registration_census(lines, row_metadata)
        pin_preparation()
        row_fields[5] += f",launch_claimed={1 if launch else 0}"
        _append_registry_rows(jobs, ["\t".join(row_fields)])
        return True
