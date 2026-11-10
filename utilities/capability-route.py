#!/usr/bin/env python3
"""Compile, verify, and complete immutable capability routes.

Module map. This file holds the command line (`main`), compiling and composing
a route (`compile_route`, `compose_route`, `build_continuation_route`, ...),
and the launch-compatibility checks with their per-process caches. Code that
reads module state (those caches, the lazily loaded runtime-activation and
stage-fallback modules, the dispatch-defaults warnings) stays here, because
this file is path-loaded under many module names and each copy keeps its own
state. It also keeps `ROOT` and the `sys.path` line it needs to find its parts.
The rest lives in sibling modules, bottom layer first; each imports only from
the ones above it in this list, and this file re-exports every name they define:

    capability_route_base          constants, path-loaded helpers, route files
    capability_route_continuation  continuation routes, source lineage
    capability_route_dispatch      dispatch evidence, fallback chains, parallel groups
    capability_route_nodes         owner/frame/no-model nodes, sealed modes
    capability_route_compose       compose recipes, readiness, selection pins
    capability_route_scope         execution scope, campaign choice, compose card
    capability_route_verify        verify_route
    capability_route_markers       completion markers, review identity and lineage
    capability_route_revision      producer revisions, tombstones, arbitration
    capability_route_close         terminal observation, close_route, route_status
    capability_route_completion    complete_node, owner closure, subsession stages
    capability_route_worktree      worktrees, attempts, compose receipts

A test that replaces a name with `mock.patch` must patch the module whose code
looks the name up; patching it here reaches only the code in this file and
callers that go through this module. `split_module_fixture.patch_split`
replaces a name in this file and every part that binds it, as one patch did
before the split.
"""
from __future__ import annotations

import argparse, base64, contextlib, copy, fcntl, functools, hashlib, importlib.util, json, locale, os, re, shlex, shutil, stat, subprocess, sys, tempfile, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"utilities"))
from hearting_gates import note_same_work_drift
import artifact_locator as ARTIFACT_LOCATOR
import harness_state_roots as HARNESS_STATE_ROOTS
import git_index_lock
import route_format
import route_identity as ROUTE_IDENTITY
import route_lineage as ROUTE_LINEAGE
import route_plan as ROUTE_PLAN
import dispatch_runtime_support as RUNTIME_SUPPORT
import dispatch_terminal_commit
import model_profile as PROFILE
import review_round_cap as REVIEW_ROUND_CAP
import route_authority as ROUTE_AUTHORITY
import owner_write_advisory as OWNER_WRITE_ADVISORY
import gpu_execution_sandbox as GPU_SANDBOX
import resource_resume as RESOURCE_RESUME
from dispatch_continuation_budget import (COMPATIBILITY_FLOOR, TERMINAL_RESERVE_DEFAULT,
                                          derive_workload_ordinary)
from dispatch_contract import (
    diagnose_repository_start,
    row_is_subsession,
    CANONICAL_PARENT_TRANSPORTS,
    DispatchContractError,
    owner_operation_fence,
    EXECUTION_SURFACES,
    FALLBACK_HOPS,
    PARENT_TRANSPORT_BY_DISPATCH_DEPTH,
    SUCCESS_NOTES,
    deferred_completion,
    verdict_pass,
    success_note,
    WRAPPER_PARENT_SANDBOXES,
    WRAPPER_TRANSPORTS,
    _atomic_registry_replace,
    _delivery_intent_values,
    _terminal_cleanup_values,
    _updated_attempt_metadata,
    claim_terminal_route_locked,
    ensure_terminal_claim_absent,
    terminal_claim_observation,
    agent_home_equivalent,
    attempt_process_quiescence,
    process_namespace_identity,
    terminal_conflict_pending,
    completion_marker_is_current,
    completion_attempt_readiness,
    attempt_row_withdrawn_before_launch,
    attempt_row_terminal_never_started,
    completion_conflict_attempt,
    owner_closure_shape,
    _diff_attribution_execute_launch_head,
    evidence_digest,
    evidence_change_history,
    evidence_change_history_path,
    _evidence_mtime_text,
    evidence_currency,
    note_evidence_change,
    gate_currency,
    completion_superseded,
    original_review_self_claim,
    review_self_disposition_origin,
    review_self_disposition_subject,
    review_self_disposition_lineage,
    ROUTE_STATE_REFUSAL_REASONS,
    route_state_next_action,
    dispatch_state_roots,
    ensure_global_registry_writable,
    parse_registry_metadata,
    resolve_agent_home,
    resolve_completed_alias,
    resolve_dangling_registry,
    resolve_dispatch_state_root,
    stable_state_root,
    validate_attempt_metadata,
)
from stage_session_contract import load_manifest, report_path_from_brief
from dispatch_degradation import record_degradation  # noqa: E402
from dispatch_completion_join import materialize_after_terminal_close  # noqa: E402
from codex_dispatch_terminal import (  # noqa: E402
    REVIEW_BLOCKING_NOTE,
    inspect_terminal_attempt,
)
from replica_batch_contract import verify_manifest as verify_batch_manifest  # noqa: E402


# Re-exported so every name this module defined before the split stays an
# attribute of `capability-route` (callers, older releases and tests use them).
from capability_route_base import (  # noqa: F401
    _attempt_completion_path, _contained_regular_release_file, _exclusive_lock, _git,
    _group_members, _index_published_route, _LAUNCH_CODE_ANCHORS, _realized_auxiliary_nodes,
    _realized_group_ids, _resolve_auxiliary_arbiter, _resolve_owner_attempt_id,
    _review_round_rows, _route_chain_identity, _route_chain_module, _route_chain_parent_ledger,
    _ROUTE_CHAIN_PARENT_SCAN_BYTES, _safe_group_id, _validate_output_scopes,
    _validate_registered_headless_evidence, _verify_selection_pins, arbitration_path,
    atomic_write, AUXILIARY_ARBITER_NODE, AUXILIARY_ARBITER_OWNER_MERGE, BROKER_FIELDS,
    BROKER_FIELDS_V2, canonical, canonical_route_path, canonical_routes_dir,
    classify_route_location, CODEX_COMMAND_STATES, completion_dir,
    CONTINUATION_CONTRACT_VERSION, CONTINUATION_SOURCE_COMMIT_REBIND_VERSION, DEFAULTS,
    DEFAULTS_SPEC, DISPATCH_CONTRACT_VERSION, DISPATCH_EVIDENCE_SCOPE_VERSION,
    EVIDENCE_CONSUMER_DISPATCH_DEPTH, FALLBACK_ORDER, GATE_FIELDS,
    LAUNCH_COMPATIBILITY_TUPLE_VERSION, NATIVE_EVIDENCE_FIELDS, NATIVE_SURFACES,
    NESTED_FAILURE_SCOPES, NESTED_FIELDS, NESTED_SCOPE_FIELDS, NESTED_STATUSES, ORDER,
    owner_merge_auxiliary_groups, REGISTERED_HEADLESS_EVIDENCE_FIELDS,
    REGISTERED_HEADLESS_HARNESSES, REGISTERED_HEADLESS_STATUSES, review_round_records, ROOT,
    route_family_key, route_hash, route_path_is_exact, ROUTE_SCHEMA_VERSION, RouteLineageError,
    runtime_root_hint, SELECTION_PIN_CONTRACT_VERSION, SELECTION_PIN_EFFORT,
    SELECTION_PIN_MODEL, SELECTION_PIN_TARGETS, SPEC, TOPO, TRACKING, UI_SPEC, UI_TEXT,
    VALID_AFFINITY, VALIDATION_BASIS_VERSION, verified_route_lineage, write_once,
)
from capability_route_continuation import (  # noqa: F401
    _assert_pin_matches_grounding, _COMMIT_SHA, _continuation_contract_hash,
    _continuation_gate_release_proof, _continuation_id, _continuation_lineage,
    _continuation_release_authority, _continuation_reused_evidence,
    _continuation_source_commit, _continuation_source_jobs, _continuation_superseded_boundary,
    _evidence_parent_dispatch_depth, _git_commit, _GIT_SHA, _grounding_cwd_lineage_ok,
    _inside_git_worktree, _jobs_path_alias_relieves_mismatch, _launch_tuple_roots,
    _marker_identity_row, _node_mutates_worktree, _note_evidence_changes,
    _registered_marker_fence, _scope_touches_spec, _sha256_record, _single_owner_nodes,
    _source_evidence_prefix, _source_evidence_snapshot, _source_lineage_unverifiable,
    _terminal_owner_for_revised_ancestor, _validate_tracking_evidence,
    _validate_tuple_parent_identity, _verified_ancestor_plan_refresh,
    _verify_continuation_gate_release_proofs, _verify_continuation_route,
    bind_continuation_cycle, partial_group_continuation, publish_continuation_route,
    source_evidence_digest, SOURCE_LINEAGE_GIT_TIMEOUT, source_lineage_verdict, SourceLineage,
    verified_ancestor_plan_refresh, worktree_mutating_scope,
)
from capability_route_dispatch import (  # noqa: F401
    _expand_parallel_groups, _fallback_chain, _parallel_path, _realize_conditional_extensions,
    _realized_parallel_groups, _TERMINAL_PARALLEL_GROUP_GRANDFATHER,
    _validate_dispatch_evidence, _verify_fallback_chain, _workflow_contract,
    WORKFLOW_CONTRACT_VERSION,
)
from capability_route_nodes import (  # noqa: F401
    _capture_unit_frontmatter, _expected_nodes_under_frame_policy, _frame_node,
    _frameless_recipe, _load_sd160_snapshots, _no_model_node, _one_leg_frame_recipe,
    _owner_node, _profile_input_maps, _quick_frame_diversity, _quick_gate_bindings,
    _recipe_has_frame, _resolve_owner_profile, _sd160_snapshot_path, _seal_confirmation_mode,
    _seal_profile_demands, _seal_small_work_confirmation, _seal_terminal_commit_support,
    _stamp_frame_profiles, _unit_blocks_digest, _verify_profile_contract,
    SD160_REGISTRY_DIGEST, SD160_SNAPSHOT_DIR, SD160_UNIT_CATALOG_DIGEST, unit_catalog_digest,
)
from capability_route_compose import (  # noqa: F401
    _apply_selection_pins, _change_pins, _complete_compose_group_consumers,
    _compose_default_children, _compose_default_jobs, _compose_default_owners,
    _compose_graph_order_violation, _compose_inputs, _compose_preparation_key,
    _compose_readiness, _compose_shared_spec_prds, _compose_spec_read_notice,
    _compose_unarbitrated_groups, _dispatch_evidence_matches_cwd, _filter_top_pins,
    _frame_brief_finder, _frame_pin_rows, _inherited_selection_pins, _inherits_frame_worktree,
    _kept_dependencies, _leg_compose_kwargs, _leg_evidence, _main_session_only_models,
    _parse_selection_pins, _pin_change_probe, _record_access_change,
    _route_plan_preparation_key, _seal_framed_worktree, _spec_reads_recorded,
    _turn_peer_source, _unarbitrated_auxiliary_legs, _validate_compose_owner_close,
    _versioned_subgraph, _without_implicit_frame_briefs, COMPOSE_DEFAULT_CAPABILITY,
    COMPOSE_DEFAULT_CHILDREN, COMPOSE_SHAPES, compose_spec_read, compose_subgraph_recipe,
    DECIDED_SHAPES, DEFAULT_ARTIFACT_GUARD, DEFAULT_DRIFT_VERDICT, INTENSITY_SHAPE,
    parse_graph_spec, proposal_readiness, ROUTE_FRAME_CAPABILITY, ROUTE_ORIGINS,
    shape_for_intensity, SHAPE_INTENSITY, SPEC_READ_MARKER_PREFIX, SPEC_READ_SHARED_PREFIX,
)
from capability_route_scope import (  # noqa: F401
    _bind_entry_execution_scope, _CAMPAIGN_ROWS_UNSET, _compose_campaign_line,
    _project_entry_scope_nodes, compose_campaign_hint, COMPOSE_CAMPAIGN_LIST_CAP,
    compose_campaign_selection, compose_campaign_summaries, compose_card,
    compose_decision_lines, compose_default_slug, compose_observations, compose_omission_lines,
    compose_resolve_campaign_key, COMPOSE_SLUG_WORDS, cwd_campaign_keys,
    CWD_CAMPAIGN_KEYS_SHOWN, CWD_CAMPAIGN_SCAN_ROUTES, declared_start_approvals,
    first_campaign_key, project_entry_execution_scope, route_plan_execution_scope,
    route_start_approvals, valid_entry_execution_scope_marker,
)
from capability_route_verify import (  # noqa: F401
    _authenticated_legacy_design_registry, _check_validation_basis, _DEGRADE_VALIDATION_BASIS,
    _entry_scope_legacy_registry, _validate_campaign_selection, _ValidationBasisDegrade,
    classify_validation_basis, verify_route,
)
from capability_route_markers import (  # noqa: F401
    _answered_fix_revisions, _auxiliary_groups_arbitrated_by, _completion_marker_replay,
    _continuation_node_projection, _dependency_revisions, _downstream_node_ids,
    _find_attempt_row_metadata, _marker_attempt_axes, _migrate_completion_dir_forward,
    _next_marker_sequence, _node_revision_records, _notify_reviewer_claim_ignored,
    _parse_auxiliary_findings, _review_input_revision_plan, _review_input_revision_records,
    _review_owner_authority, _review_self_disposition_actor, _review_self_disposition_locked,
    _revision_consumers, _rewrite_migrated_attempt_links, _supersession_records,
    _validate_auxiliary_arbiter, ARBITRATION_SCHEMA_VERSION, preview_review_input_revision,
    publish_review_input_revision, resolve_review_identity, review_lineage_routes,
    REVIEWER_KINDS, revision_basis_verdict, write_completion_marker,
)
from capability_route_revision import (  # noqa: F401
    _arbitration_observation, _producer_revision_plan, _require_entry_scope_review_preview,
    _revision_target, _route_revisions, arbitrate_group, publish_revision_locked,
    restore_consumed_tombstones,
)
from capability_route_close import (  # noqa: F401
    _current_terminal_identity, _gate_releases, _head_commit, _LEGACY_LOCATIONS,
    _LOCATION_SORT_PRIORITY, _outcome_replay_matches, _owner_terminal_observation,
    _promote_historical_false_outcome, _ROUTE_SIDECAR_SUFFIXES, close_route,
    legacy_route_diagnostic, outcome_path, OUTCOME_SCHEMA_VERSION, owner_executed_terminal,
    owner_terminal_prerequisites, PUBLICATION_RESULTS, retire_stale_closure,
    review_independence_observation, ROUTE_RECORD_BASENAME, route_sidecar_kind, route_status,
    terminal_gate_observation, terminal_gate_proven,
)
from capability_route_completion import (  # noqa: F401
    _artifact_delta, _artifact_output_snapshot, _baseline_content_map, _bound_env_attempt,
    _complete_framed_terminal, _complete_node_locked, _content_digest,
    _continuation_closure_marker_compatible, _first_parent_descends_from, _git_changed_files,
    _git_committed_files, _launch_open_cycle_checkpoint, _LEGACY_BASELINE_DIGEST,
    _LIVE_ROW_STATUSES, _mentions, _owner_closure_eligibility, _owner_closure_frontmatter,
    _owner_closure_sealed_pipe, _OWNER_CLOSURE_SUFFIX, _OWNER_CLOSURE_VERDICT,
    _placed_artifact_false_negative, _publish_completion_locked,
    _publish_continuation_owner_closure, _published_owner_chain_marker, _registry_unsafe,
    _REGISTRY_UNSAFE_CHARS, _sole_node_attempt, _unregistered_attempt, complete_node,
    complete_subsession_stage, continuation_owner_closure_plan, load_subdivision_baseline,
    owner_closure_plan, producer_revision_admission, record_subdivision_baseline,
    subdivision_baseline_path, SUBDIVISION_BASELINE_SCHEMA_VERSION,
    validate_continuation_owner_closure,
)
from capability_route_worktree import (  # noqa: F401
    _allocate_branch_ref, _archived_metadata, _attach_prepared_branch,
    _branch_candidate_available, _branch_checked_out, _branch_namespaces, _branch_ref_oid,
    _classify_non_git_task, _cleanup_failed_worktree_branch, _close_route_argument,
    _compose_artifact_root, _compose_git_diagnosis, _continue_after_answer,
    _correct_attempt_argument, _current_owner_attempt, _decide_non_git_compose,
    _emit_compiled_route, _existing_local_branch, _git_common_dir, _isolates_worktree,
    _legacy_inline_finish, _moved_preparation, _preparation_record_path,
    _prepared_record_worktree, _primary_worktree, _read_preparation_record,
    _record_route_chain, _recorded_preparation_roots, _ref_namespace_conflict, _registry_rows,
    _release_owned_branch, _remote_default_base, _resolve_compose_plan, _result_location_line,
    _session_campaign_key, _session_latest_route, _stage_route_owner, _worktree_collision,
    _worktree_cwd, _worktree_git, attempt_route_file, caller_open_route,
    compose_campaign_snapshot, compose_command_snapshot, compose_receipt,
    prepare_isolated_worktree, publish_composed_route, resolve_route_argument,
    ROUTE_ATTEMPT_FIELDS, route_attempts, ROUTE_ID_FORM, route_owner_attempt, stages_block,
)

# Moved classes report the module they were defined in before the split (for
# this path-loaded file, the first copy loaded in the process), so tracebacks
# and reprs read as before.
for _moved_class in (
    SourceLineage, _ValidationBasisDegrade,
):
    if _moved_class.__module__.startswith("capability_route_"):
        _moved_class.__module__ = __name__
del _moved_class
_LAUNCH_ROOT_IDENTITY_CACHE = {}
_LAUNCH_CONTENT_DIGEST_CACHE = {}
_LAUNCH_SOURCE_REVISION_CACHE = {}
_RUNTIME_ACTIVATION = None

def _runtime_activation_module():
    """Load the installer identity implementation without copying its convention."""
    global _RUNTIME_ACTIVATION
    if _RUNTIME_ACTIVATION is None:
        install_root=ROOT/"tools"/"install"
        if str(install_root) not in sys.path:
            sys.path.insert(0,str(install_root))
        spec=importlib.util.spec_from_file_location(
            "_capability_route_runtime_activation",
            install_root/"runtime_activation.py",
        )
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        _RUNTIME_ACTIVATION=module
    return _RUNTIME_ACTIVATION

def _launch_source_revision(path):
    resolved=Path(path).resolve(strict=False)
    key=str(resolved)
    if key not in _LAUNCH_SOURCE_REVISION_CACHE:
        _LAUNCH_SOURCE_REVISION_CACHE[key]=_runtime_activation_module().source_revision(resolved,runtime_launch=True)
    return _LAUNCH_SOURCE_REVISION_CACHE[key]

def _launch_content_digest(path):
    """Digest only immutable code anchors, representing missing anchors explicitly."""
    resolved=Path(path).resolve(strict=False)
    key=str(resolved)
    if key not in _LAUNCH_CONTENT_DIGEST_CACHE:
        rows=[]
        for relative in _LAUNCH_CODE_ANCHORS:
            anchor=resolved/relative
            try:
                data=anchor.read_bytes() if anchor.is_file() else None
            except OSError:
                data=None
            rows.append({
                "anchor":relative,
                "state":"file" if data is not None else "missing",
                "sha256":hashlib.sha256(data).hexdigest() if data is not None else None,
            })
        _LAUNCH_CONTENT_DIGEST_CACHE[key]="sha256:"+hashlib.sha256(canonical(rows)).hexdigest()
    return _LAUNCH_CONTENT_DIGEST_CACHE[key]

def _verified_immutable_release_identity(path):
    """Return the bounded identity of one complete managed-release code root.

    A marker is necessary but never sufficient: the installed release version,
    whole immutable release revision, and closed launch-anchor digest must all
    agree.  `published_at` proves marker completeness but is intentionally not
    a comparison axis; the revision digest still covers its exact bytes.
    """
    try:
        root=Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not root.is_dir():
        return None
    marker_path=_contained_regular_release_file(root,".hearting-release.json")
    version_path=_contained_regular_release_file(root,"RELEASE_VERSION")
    if marker_path is None or version_path is None:
        return None
    try:
        if marker_path.stat().st_size > 16_384 or version_path.stat().st_size > 128:
            return None
        marker=json.loads(marker_path.read_text(encoding="utf-8"))
        release_version=version_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(marker,dict) or type(marker.get("schema")) is not int:
        return None
    version=marker.get("version")
    archive_sha256=marker.get("archive_sha256")
    published_at=marker.get("published_at")
    if (
        marker["schema"] != 1
        or not isinstance(version,str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}",version) is None
        or release_version != version
        or not isinstance(archive_sha256,str)
        or re.fullmatch(r"[0-9a-f]{64}",archive_sha256) is None
        or not isinstance(published_at,str)
        or not published_at
        or len(published_at) > 256
    ):
        return None
    if any(_contained_regular_release_file(root,relative) is None
           for relative in _LAUNCH_CODE_ANCHORS):
        return None
    try:
        release_id=_launch_source_revision(root)
        content_digest=_launch_content_digest(root)
    except (OSError, RuntimeError):
        return None
    prefix=f"release:{version}:"
    if (
        not release_id.startswith(prefix)
        or re.fullmatch(r"[0-9a-f]{12}",release_id[len(prefix):]) is None
        or re.fullmatch(r"sha256:[0-9a-f]{64}",content_digest) is None
    ):
        return None
    return {
        "schema":marker["schema"],"version":version,
        "archive_sha256":archive_sha256,"release_id":release_id,
        "content_digest":content_digest,
    }

def immutable_code_root_equivalent(a,b):
    """Compare route code roots without weakening general path equivalence.

    Symlink aliases retain the existing resolved-path behavior.  Distinct
    physical paths compare by content only when both are complete, verified
    immutable managed-release copies.  Mutable/state callers must continue to
    use `agent_home_equivalent()` directly.
    """
    if agent_home_equivalent(a,b):
        return True
    left=_verified_immutable_release_identity(a)
    right=_verified_immutable_release_identity(b)
    return left is not None and left == right

def _forget_launch_path(path):
    """Drop every memoized identity naming this path, before any of them is read.

    The caches exist for immutable code roots, but a route cwd is the mutation
    worktree and moves under them: a process that already sealed that cwd (an
    earlier compile, a test) would otherwise hand a continuation the HEAD as it
    was then.

    Every identity for the path goes, not just the one being re-read. Under dev
    activation `runtime_root`, `launch_home` and `registry_root` can *be* that
    same path, and re-reading one of them while the others answer from cache
    puts two release ids for one path in a single tuple -- the same
    two-sources-one-value shape defect C was (review round 2, S2). Eviction is
    keyed by resolved path, so identities for genuinely different paths are
    untouched.
    """
    resolved=str(Path(path).expanduser().resolve(strict=False))
    for key in [key for key in _LAUNCH_ROOT_IDENTITY_CACHE if key[1]==resolved]:
        _LAUNCH_ROOT_IDENTITY_CACHE.pop(key,None)
    _LAUNCH_SOURCE_REVISION_CACHE.pop(resolved,None)
    _LAUNCH_CONTENT_DIGEST_CACHE.pop(resolved,None)

def _launch_root_identity(kind, path, *, resolver_identity=None):
    """Return one memoized code-root identity or runtime-bound mutable path identity."""
    resolved=Path(path).expanduser().resolve(strict=False)
    resolver_key=None
    if resolver_identity is not None:
        resolver_key=(
            resolver_identity.get("path"), resolver_identity.get("release_id"),
            resolver_identity.get("content_digest"),
        )
    key=(kind,str(resolved),resolver_key)
    if key not in _LAUNCH_ROOT_IDENTITY_CACHE:
        if resolver_identity is None:
            # A mutable project cwd is compared by first-parent HEAD lineage,
            # never by installer dirty-content hashes. On 2026-09-27 home-app
            # had >100k untracked runtime files under a tracked directory;
            # hashing them here exhausted the pre-claim fence's 60s budget.
            # A cwd without HEAD has no lineage to compare and reads
            # "unversioned", like its source pin: hashing it instead read a
            # whole home folder on every start and resume. Code roots retain
            # their complete release identity, including when a development
            # checkout or a release tree (RELEASE_VERSION) is the grounding cwd.
            if kind == "grounding_cwd" and str(resolved) not in _LAUNCH_SOURCE_REVISION_CACHE:
                try:
                    head=subprocess.run(
                        ["git","-C",str(resolved),"rev-parse","--verify","HEAD"],
                        capture_output=True,text=True,timeout=10,
                        env={**os.environ,"GIT_OPTIONAL_LOCKS":"0"},
                    )
                except (OSError,subprocess.TimeoutExpired) as exc:
                    raise ValueError("grounding-cwd-revision-unverifiable") from exc
                if head.returncode == 0 and _GIT_SHA.fullmatch(head.stdout.strip()):
                    release_id=head.stdout.strip()
                elif (resolved/"RELEASE_VERSION").is_file():
                    release_id=_launch_source_revision(resolved)
                else:
                    release_id="unversioned"
            else:
                release_id=_launch_source_revision(resolved)
            content_digest=_launch_content_digest(resolved)
        else:
            release_id=resolver_identity["release_id"]
            content_digest=resolver_identity["content_digest"]
        binding_digest="sha256:"+hashlib.sha256(canonical({
            "kind":kind,"path":str(resolved),"release_id":release_id,
            "content_digest":content_digest,
        })).hexdigest()
        _LAUNCH_ROOT_IDENTITY_CACHE[key]={
            "kind":kind,"path":str(resolved),"release_id":release_id,
            "content_digest":content_digest,"binding_digest":binding_digest,
        }
    return json.loads(json.dumps(_LAUNCH_ROOT_IDENTITY_CACHE[key]))

def launch_compatibility_tuple(*, artifact_root, jobs=None, cwd=None, refresh_cwd=False):
    """Compute v1 bounded launch identities without hashing mutable state contents."""
    runtime_root=Path(resolve_agent_home()).resolve(strict=False)
    grounding_cwd=Path(cwd if cwd is not None else Path.cwd()).resolve(strict=False)
    if refresh_cwd:
        # Up front, before the first identity is read, so every identity in this
        # tuple comes from one reading of the tree.
        _forget_launch_path(grounding_cwd)
    runtime_identity=_launch_root_identity("runtime_root",runtime_root)
    # Compute every executable root before the mutable cwd: a route may run
    # inside adapters/, which is itself the wrapper code root.
    wrapper_identity=_launch_root_identity("wrapper_root",runtime_root/"adapters")
    result={
        "tuple_version":LAUNCH_COMPATIBILITY_TUPLE_VERSION,
        "registry_root":_launch_root_identity("registry_root",TOPO.ROOT),
        "launch_home":_launch_root_identity("launch_home",runtime_root),
        "runtime_root":runtime_identity,
        "grounding_roots":{
            "cwd":_launch_root_identity("grounding_cwd",grounding_cwd),
            "artifact_root":_launch_root_identity(
                "grounding_artifact_root",artifact_root,
                resolver_identity=runtime_identity,
            ),
        },
        "wrapper_root":wrapper_identity,
    }
    try:
        jobs_path=resolve_dispatch_state_root(resolve_agent_home(),jobs)/"jobs.log"
        result["jobs_path"]=_launch_root_identity(
            "jobs_path",jobs_path,resolver_identity=runtime_identity,
        )
    except (DispatchContractError,OSError,ValueError) as exc:
        reason=exc.reason if isinstance(exc,DispatchContractError) else type(exc).__name__
        unresolved={
            "kind":"jobs_path","path":None,
            "release_id":runtime_identity["release_id"],
            "content_digest":runtime_identity["content_digest"],
            "unresolved":reason,
        }
        unresolved["binding_digest"]="sha256:"+hashlib.sha256(canonical(unresolved)).hexdigest()
        result["jobs_path"]=unresolved
    return result

def revalidate_launch_compatibility(route):
    """Compare a route's sealed launch tuple with this process's current roots."""
    sealed=route.get("launch_compatibility_tuple")
    if sealed is None:
        return True,{"tuple":"absent-legacy"}
    fresh={"contract_version":LAUNCH_COMPATIBILITY_TUPLE_VERSION}
    fresh.update(launch_compatibility_tuple(
        artifact_root=route.get("artifact_root","."),cwd=route.get("cwd","."),
    ))
    mismatches={}
    malformed_roots=False
    if not isinstance(sealed,dict):
        return False,{"tuple":{"expected":sealed,"actual":fresh}}
    for field in ("contract_version","tuple_version"):
        if sealed.get(field) != fresh.get(field):
            mismatches[field]={"expected":sealed.get(field),"actual":fresh.get(field)}
    expected_roots=_launch_tuple_roots(sealed)
    actual_roots=_launch_tuple_roots(fresh)
    release_moved=ROUTE_AUTHORITY.release_moved(
        expected_roots,actual_roots,
        managed_release=lambda path:_verified_immutable_release_identity(path) is not None,
    )
    identity_fields=("kind","path","release_id","content_digest","binding_digest")
    for name,expected in expected_roots.items():
        actual=actual_roots[name]
        if not isinstance(expected,dict) or not isinstance(actual,dict):
            malformed_roots=True
            mismatches[name]={"expected":expected,"actual":actual}
            continue
        changed={field:{"expected":expected.get(field),"actual":actual.get(field)}
                 for field in identity_fields if expected.get(field) != actual.get(field)}
        if expected.get("unresolved") != actual.get("unresolved"):
            changed["unresolved"]={
                "expected":expected.get("unresolved"),"actual":actual.get("unresolved"),
            }
        if (
            changed and name == "grounding_roots.cwd"
            and set(changed) <= {"release_id","content_digest","binding_digest"}
            and _grounding_cwd_lineage_ok(
                actual.get("path"),expected.get("release_id"),actual.get("release_id"),
            )
        ):
            changed={}
        if (
            changed and name == "jobs_path"
            and _jobs_path_alias_relieves_mismatch(route,expected,actual)
        ):
            changed={}
        if changed and name in release_moved:
            changed={}  # the installed release moved: where the launch runs, not the work
        if changed:
            mismatches[name]={
                "expected":expected,"actual":actual,"fields":sorted(changed),
            }
    if mismatches and not malformed_roots:
        note_same_work_drift("launch-runtime-root-mismatch", ",".join(sorted(mismatches)))
        return True, mismatches
    return not mismatches,mismatches

def build_continuation_route(
    source_route,*,resume_from_node,requested_boundary,reason,
    artifact_root,lineage_operation="resume",thread_id=None,new_thread_id=None,
    forked_from_id=None,last_turn_id=None,ephemeral=False,
    partial_group=None,dispatch_evidence=None,
):
    """Generate one official route suffix without invoking the generic compiler."""
    source_nodes=source_route.get("nodes") or []
    node_ids=[str(node.get("id")) for node in source_nodes]
    requested_blocker=(
        None if requested_boundary in node_ids else "requested-boundary-unknown"
    )
    first_blocker=None
    if resume_from_node not in node_ids:
        first_blocker="first-runnable-node-unknown"
        resume_index=None
    else:
        resume_index=node_ids.index(resume_from_node)
    inherited_refresh=source_route.get("ancestor_plan_refresh")
    if inherited_refresh is not None:
        verified_ancestor_plan_refresh(source_route)
    else:
        inherited_refresh=_verified_ancestor_plan_refresh(source_route)
        if inherited_refresh is not None and resume_from_node!="plan-check":
            first_blocker="ancestor-revision-affected-node-skipped"
    reused_ids=node_ids[:resume_index] if resume_index is not None else []
    reused=[]; reused_turns={}; evidence_digest=_sha256_record([])
    if first_blocker is None:
        reused,evidence_digest,reused_turns,reuse_error,failed_node=(
            _source_evidence_prefix(source_route,reused_ids)
        )
        if reuse_error:
            restart_node=_continuation_superseded_boundary(
                source_route,failed_node,reuse_error
            )
            if restart_node is not None:
                # Keep the old PASS historical and cut the reusable prefix at
                # the first review that cannot prove its source. The ordinary
                # suffix starts there and receives a new source binding.
                resume_index=node_ids.index(restart_node)
                resume_from_node=restart_node
                reused_ids=node_ids[:resume_index]
                reused,evidence_digest,reused_turns,first_blocker,_failed_node=(
                    _source_evidence_prefix(source_route,reused_ids)
                )
            else:
                first_blocker=reuse_error
    lineage=_continuation_lineage(
        source_route,reused,reused_turns,operation=lineage_operation,
        thread_id=thread_id,new_thread_id=new_thread_id,
        forked_from_id=forked_from_id,last_turn_id=last_turn_id,
        ephemeral=ephemeral,
    )
    identity={
        "source_route_id":source_route.get("route_id"),
        "source_route_hash":source_route.get("route_hash"),
        "resume_from_node":resume_from_node,
        "requested_boundary":requested_boundary,
        "reason":reason,
        "source_evidence_digest":evidence_digest,
        "lineage":lineage,
    }
    if dispatch_evidence is not None:
        dispatch_evidence=_validate_dispatch_evidence(
            dispatch_evidence,source_route.get("dispatch_contract_version") or DISPATCH_CONTRACT_VERSION,
            _evidence_parent_dispatch_depth(source_nodes,source_route.get("owner_dispatch_depth",1)),
            expected_worktree=source_route.get("cwd"),require_scope=True)
        identity.update({"dispatch_evidence_override":True,"dispatch_evidence":dispatch_evidence})
    if inherited_refresh is not None:
        identity["ancestor_revision_digest"]=_sha256_record(inherited_refresh)
    continuation_id=_continuation_id(identity)
    edge={
        "edge_version":1,
        "edge_id":_sha256_record({
            "from_route_id":source_route.get("route_id"),
            "from_route_hash":source_route.get("route_hash"),
            "to_continuation_id":continuation_id,
            "reason":reason,
        }),
        "operation":"continuation",
        "from_route_id":source_route.get("route_id"),
        "from_route_hash":source_route.get("route_hash"),
        "to_continuation_id":continuation_id,
        "reason":reason,
        "source_verdict_preserved":True,
    }
    result={
        "continuation_contract_version":CONTINUATION_CONTRACT_VERSION,
        **identity,
        "continuation_id":continuation_id,
        "first_runnable_node":(
            resume_from_node if resume_index is not None else None
        ),
        "requested_boundary_blocker":requested_blocker,
        "first_runnable_blocker":first_blocker,
        "lineage_operation":lineage_operation,
        "runtime_lineage":lineage,
        "source_route_supersession":edge,
        "supersession_edges":[
            *json.loads(json.dumps(source_route.get("supersession_edges") or [])),
            edge,
        ],
        "reused_nodes":reused,
        "new_nodes":[],
        "partial_group_continuation":None,
    }
    if inherited_refresh is not None:
        result["ancestor_plan_refresh"]=json.loads(json.dumps(inherited_refresh))
    if partial_group is not None and first_blocker is None:
        result["partial_group_continuation"]=partial_group_continuation(
            source_route,**partial_group
        )
    if requested_blocker or first_blocker:
        return result
    route_nodes=[]
    descriptors=[]
    for offset,source_node in enumerate(source_nodes[resume_index:]):
        source_contract_hash=_continuation_contract_hash(source_node)
        node=_continuation_node_projection(source_node,reused)
        if dispatch_evidence is not None and node.get("dispatch_depth")==2:
            node["fallback_hops"]=_fallback_chain(
                dispatch_evidence,source_route.get("dispatch_contract_version") or DISPATCH_CONTRACT_VERSION,
                expected_worktree=source_route.get("cwd"),require_scope=True)
        if (inherited_refresh is not None
                and source_route["route_id"]==inherited_refresh["source_route_id"]
                and node["id"]=="plan-check"):
            dependencies=node.get("reused_dependencies") or []
            if (len(dependencies)!=1 or dependencies[0].get("node_id")!="plan"
                    or dependencies[0].get("marker_digest")!=inherited_refresh["old_marker_digest"]):
                raise ValueError("ancestor-revision-review-dependency-invalid")
            dependencies[0]["marker_digest"]=inherited_refresh["current_marker_digest"]
        route_nodes.append(node)
        descriptors.append({
            "node_id":str(source_node["id"]),
            "source_contract_hash":source_contract_hash,
            "realized_contract_hash":_continuation_contract_hash(node),
            "attempt_authority":"granted" if offset==0 else "pending-dependency",
            "new_attempt_count":0,
        })
    result["new_nodes"]=descriptors
    # A continuation is sealed at resume time: ground it in the cwd as it is now,
    # not as a memoized earlier seal of the same process saw it (defect C pairing).
    launch={
        "contract_version":LAUNCH_COMPATIBILITY_TUPLE_VERSION,
        **launch_compatibility_tuple(
            artifact_root=artifact_root,cwd=source_route.get("cwd"),refresh_cwd=True,
        ),
    }
    inherited_keys=(
        "schema_version","capability","capability_mode","slug","slug_truncated",
        "campaign_key","parent_cycle_id","campaign_unassigned","work_request",
        "selection_pins","requested_intensity",
        "effective_intensity","owner_model_profile","execution_topology",
        "owner_dispatch_depth","max_dispatch_depth","tracking",
        "tracked_gate_evidence","spec_touch","artifact_storage_contract_version","cwd","source_commit","source_repository",
        "registry_digest","capability_registry_digest","dispatch_defaults_digest","dispatch_allocation",
        "owner_harness_policy","selection","codex_execution_sandbox","human_gates","human_gate_bindings",
        "confirmation_mode","small_work_confirmation",
        "resume_retry_boundaries","dispatch_evidence","dispatch_contract_version",
        "dispatch_evidence_scope_version","registered_headless_candidates",
        "registered_headless_policy","unit_catalog_digest","validation_basis",
        # Profile-demand sealing is part of the route identity. Continuation
        # suffixes must carry it forward so verification can replay the same
        # resolver contract instead of treating an annotated source as legacy.
        "profile_selection_contract_version","profile_demands","explicit_profiles",
        "owner_profile_demand","owner_profile_selection",
        # SD-OPEN-46: a composed source's composition fields must ride the
        # suffix, or the continuation presents itself as a preset route and the
        # embedded composed_recipe loses its hash seal. (`route_origin`/`shape`
        # live inside `selection`, inherited above.)
        "composed","composed_recipe",
        # An approved route plan's leg keeps pointing at its plan when it resumes.
        "route_plan", "framed_worktree",
    )
    inheritance=ROUTE_AUTHORITY.route_in_force(source_route)
    route={key:json.loads(json.dumps(inheritance[key]))
           for key in inherited_keys if key in inheritance}
    pins=ROUTE_AUTHORITY.selection_pin_rows(source_route)
    if pins:
        route["selection_pins"]={"contract_version":SELECTION_PIN_CONTRACT_VERSION,**pins}
    route.update(result)
    if route.get("profile_selection_contract_version") == 1:
        retained = {node["id"] for node in route_nodes} | {"__owner__"}
        for key in ("profile_demands", "explicit_profiles"):
            route[key] = {k: v for k, v in route.get(key, {}).items() if k in retained}
    # Defect C: the pin must name the same commit the grounding tuple above sealed.
    source_commit,source_commit_rebind=_continuation_source_commit(source_route)
    if source_commit is not None:
        route["source_commit"]=source_commit
    if source_commit_rebind is not None:
        route["source_commit_rebind"]=source_commit_rebind
    _assert_pin_matches_grounding(route.get("source_commit"),launch)
    route["artifact_root"]=str(Path(artifact_root).resolve(strict=False))
    route["nodes"]=route_nodes
    # A continuation is a suffix, not a copy of the source graph. An entry gate
    # whose target was cut is gone. A raised gate whose raiser was cut but target
    # remains may disappear only with the exact source proceed evidence sealed.
    suffix_ids={str(node["id"]) for node in route_nodes}
    source_raisers={
        str((node.get("continuation") or {}).get("gate")):str(node.get("id"))
        for node in source_nodes
        if (node.get("continuation") or {}).get("kind")=="human-gate"
    }
    projected_bindings=[]; release_proofs=[]
    for row in (source_route.get("human_gate_bindings") or []):
        if str(row.get("node")) not in suffix_ids:
            continue
        gate=str(row.get("gate"))
        raiser=source_raisers.get(gate)
        if raiser is not None and raiser not in suffix_ids:
            release_proofs.append(_continuation_gate_release_proof(source_route,gate))
            continue
        projected_bindings.append(json.loads(json.dumps(row)))
    route["human_gate_bindings"]=projected_bindings
    route["human_gates"]=sorted({str(row["gate"]) for row in projected_bindings})
    route["reused_human_gate_releases"]=release_proofs
    registry=TOPO.load_registry()
    try:
        TOPO._validate_continuations(
            route,registry,route_nodes,{str(node["id"]):node for node in route_nodes},
        )
    except TOPO.TopologyError as exc:
        raise ValueError("continuation-human-gate-unrepresentable:"+str(exc)) from exc
    route["parallel_groups"]=_realized_parallel_groups(route_nodes)
    route["conditional_extensions"]=[]
    route["completion_gates"]=sorted({
        str(node.get("terminal_gate") or node.get("completion_gate"))
        for node in route_nodes if node.get("terminal") is True
    })
    route["workflow_contract"]={
        "schema_version":WORKFLOW_CONTRACT_VERSION,
        "states":list((source_route.get("workflow_contract") or {}).get("states") or []),
        "failure_states":list((source_route.get("workflow_contract") or {}).get("failure_states") or []),
        "terminal_nodes":[
            str(node["id"]) for node in route_nodes if node.get("terminal") is True
        ],
        "continuations":{
            str(node["id"]):json.loads(json.dumps(node.get("continuation")))
            for node in route_nodes if node.get("terminal") is not True
        },
        "human_gate_bindings":json.loads(json.dumps(route.get("human_gate_bindings") or [])),
    }
    route["launch_compatibility_tuple"]=launch
    route["advance_generation"]=int(source_route.get("advance_generation") or 0)+1
    digest=route_hash(route)
    route["route_hash"]=digest
    route["route_id"]="rt-"+digest.split(":",1)[1][:16]
    owner_attempt_id=_resolve_owner_attempt_id()
    route["owner_attempt_id"]=owner_attempt_id
    route["route_family_key"]=route_family_key(
        route.get("capability"),route.get("cwd"),route.get("capability_mode"),owner_attempt_id,
    )
    return route

_STAGE_FALLBACK=None

def _stage_fallback():
    """Load the shared registry reader the launch guard uses, once."""
    global _STAGE_FALLBACK
    if _STAGE_FALLBACK is None:
        spec=importlib.util.spec_from_file_location(
            "_capability_route_stage_fallback",ROOT/"utilities"/"stage-dispatch-fallback.py",
        )
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _STAGE_FALLBACK=module
    return _STAGE_FALLBACK

# Recommendation findings of the dispatch-defaults policy (`load_validated`)
# collected while a route is sealed; `compose` prints them as `warning=` lines.
# Warnings never change what is sealed beyond the normalization the loader
# already applied.
DISPATCH_DEFAULTS_WARNINGS = []


def _note_warning(line):
    if line not in DISPATCH_DEFAULTS_WARNINGS:
        DISPATCH_DEFAULTS_WARNINGS.append(line)


def _seal_dispatch_defaults(nodes, capability, owner_profile=None):
    """Return defaults digest/allocation and stamp each dispatch-depth-2 node's
    harness_affinity, BEFORE route_hash is computed. Absent config -> all
    'unspecified' + digest None. Structurally corrupt config -> fail-loud
    (reused loader validator), surfaced as ValueError so main() exits 64; a
    recommendation the config breaks is a `warning=` line, not a failure.
    Each depth-2 node and each depth-1 frame leg carries its profile's sealed
    harness_policy, so a frame launched later reads the same bands the route
    was sealed with instead of re-reading the live file. registry_digest is a
    separate field and is never touched here."""
    config_path = DEFAULTS.default_config_path()
    if not os.path.exists(config_path):
        for node in nodes:
            if node.get("dispatch_depth") == 2:
                node["harness_affinity"] = "unspecified"
                node["harness_policy"] = None
            elif _frame_node(node):
                node["harness_policy"] = None
        return None, None, None
    try:
        cfg, findings = DEFAULTS.load_validated(config_path, DEFAULTS.default_topology_path())
    except DEFAULTS.DefaultsConfigError as exc:
        raise ValueError(f"corrupt dispatch-defaults config: {exc}")
    for finding in findings:
        _note_warning(f"warning=dispatch-defaults:{finding}")
    for node in nodes:
        if node.get("dispatch_depth") == 2:
            node["harness_affinity"] = DEFAULTS.query_stage_affinity(
                cfg, capability, node.get("parallel_anchor", node["id"])
            )
            node["harness_policy"] = DEFAULTS.query_profile_policy(
                cfg, node["model_profile"]
            )
        elif _frame_node(node):
            node["harness_policy"] = DEFAULTS.query_profile_policy(
                cfg, node["model_profile"]
            )
    return (
        "sha256:" + hashlib.sha256(canonical(cfg)).hexdigest(),
        DEFAULTS.query_allocation(cfg),
        DEFAULTS.query_profile_policy(cfg, owner_profile) if owner_profile else None,
    )

def _validation_basis():
    """Seal which install root produced `registry_digest`/`unit_catalog_digest`.

    `runtime_root` is normalized to an absolute, resolved path even though
    `resolve_agent_home()` returns its candidate unnormalized -- a relative
    `AGENT_HOME`/`CLAUDE_HOME` must not seal a relative `runtime_root`, since
    the close-time structural gate treats a non-absolute required field as a
    forged record with no legitimate producer.
    """
    runtime_root = Path(resolve_agent_home()).resolve(strict=False)
    registry_root = TOPO.ROOT
    unit_catalog_root = ROOT
    return {
        "basis_version": VALIDATION_BASIS_VERSION,
        "registry_root": str(registry_root),
        "unit_catalog_root": str(unit_catalog_root),
        "runtime_root": str(runtime_root),
        "runtime_root_validated": (runtime_root/"core"/"CORE.md").is_file(),
        "runtime_root_match": immutable_code_root_equivalent(registry_root, runtime_root),
    }

def compile_route(capability, capability_mode, requested_intensity, cwd, artifact_root,
                  predicates=(), signals=(), transport=None,
                  transport_evidence="caller-selected", inline_reason=None,
                  tracking="tracked", tracked_gate_evidence=None, dispatch_evidence=None,
                  registered_headless_evidence=None, slug=None,
                         route_origin="preset", shape=None, profile_demands=None,
                         explicit_profiles=None, campaign_key=None, parent_cycle_id=None, profile=None,
                         route_plan=None, frameless=False, frame_legs=2):
    registry=TOPO.load_registry(); TOPO.validate_registry(registry)
    recipe=TOPO.resolve_recipe(registry, capability, capability_mode)
    return _compile_from_recipe(
        registry, recipe, capability, capability_mode, requested_intensity, cwd, artifact_root,
        predicates=predicates, signals=signals, transport=transport,
        transport_evidence=transport_evidence, inline_reason=inline_reason,
        tracking=tracking, tracked_gate_evidence=tracked_gate_evidence,
        dispatch_evidence=dispatch_evidence,
        registered_headless_evidence=registered_headless_evidence, slug=slug,
        campaign_key=campaign_key, parent_cycle_id=parent_cycle_id,
        route_origin=route_origin, shape=shape, profile_demands=profile_demands,
        explicit_profiles=explicit_profiles, profile=profile,
        route_plan=route_plan, frameless=frameless, frame_legs=frame_legs)


def compose_route(*, capability, capability_mode, shape, graph, slug, cwd, artifact_root,
                  intensity=None, signals=(), spec_read=None, drift_verdict=None,
                  tracking=None, artifact_guard=None, children=None, parent_harness="claude",
                  dispatch_evidence=None, registered_headless_evidence=None,
                  transport_evidence="compose-default", jobs=None, profile_demands=None, explicit_profiles=None,
                  campaign_key=None, parent_cycle_id=None, profile=None, work_request=None, unassigned=False,
                  selection_pins=None, route_plan=None, frameless=False, execution_scope=None,
                  extra_stages=None, selection_only=False, capability_source=None):
    """Resolve every default, then compile through the ordinary sealer.

    `route_plan` (a binding read by `route_plan.read_route_plan`) seals `{decision, digest, index}`
    and compiles without frame nodes; `frameless` alone is the same compile without the seal, the form
    a proposal leg is validated in. A `solo` or `staged` shape is chosen for work that is already
    decided, so it compiles without the recipe's frame pair too; `framed` is where a frame runs.
    """
    import route_plan as RP
    if route_plan is not None and shape == "framed":
        leg = route_plan["leg"]
        shape = leg["shape"]
        capability = capability or leg["capability"]
        capability_mode = capability_mode or leg.get("mode")
        graph = graph or (",".join(leg["graph"]) if leg.get("graph") else None)
        intensity = intensity or leg.get("intensity")
    given_intensity, given_capability = intensity, capability
    if capability is None and shape != "framed":
        capability = COMPOSE_DEFAULT_CAPABILITY
    sealed_plan = RP.sealed_form(route_plan) if route_plan is not None else None
    frameless = frameless or route_plan is not None or shape in DECIDED_SHAPES
    if execution_scope is None and route_plan is not None:
        execution_scope = route_plan_execution_scope(route_plan)
    if execution_scope is not None and execution_scope not in ("complete", "report"):
        raise ValueError("compose-shape-invalid:execution-scope")
    if shape not in COMPOSE_SHAPES:
        raise ValueError(f"compose-shape-invalid:{shape}")
    routing_hints = None
    frame_leg_choice = None
    if shape == "framed":
        # The shape itself picks the compiler-internal capability. Whatever was typed for the
        # ordinary shapes is only a hint for the work request: recorded, never sealed as the
        # route's capability, never refused as an argument conflict, never applied to a model.
        routing_hints = {key: value for key, value in (
            ("capability", capability), ("capability_mode", capability_mode),
            ("graph", graph), ("profile", profile)) if value}
        capability, capability_mode, graph, profile = ROUTE_FRAME_CAPABILITY, "default", None, None
        if intensity is not None and intensity not in ORDER:
            raise ValueError("invalid intensity")
        # The framed route stays standard; its declared frame width is sealed separately.
        request_text = work_request.get("text", "") if isinstance(work_request, dict) else ""
        frame_leg_choice = RP.frame_leg_decision(intensity, request_text, routing_hints)
        frame_legs = frame_leg_choice[0]
        intensity = None
    resume_graph = (shape in ("direct", "solo") and RESOURCE_RESUME.selected(
        capability, capability_mode or "setup", graph.split(",") if graph else None))
    if shape != "staged" and graph and not resume_graph:
        raise ValueError(f"compose-graph-only-staged:{shape}")
    registry = TOPO.load_registry()
    # A capability may own several recipes (autopilot-lab: setup, eval); the
    # requested mode picks the recipe that declares it. The internal framed
    # capability exists only for the framed shape.
    candidates = [r for r in registry["recipes"] if r["capability"] == capability
                  and (capability != ROUTE_FRAME_CAPABILITY or shape == "framed")]
    if not candidates:
        raise ValueError(f"compose-capability-unknown:{capability}")
    if capability_mode is None:
        base = next((r for r in candidates if "dev" in r["modes"]), candidates[0])
        capability_mode = "dev" if "dev" in base["modes"] else sorted(base["modes"])[0]
    base = next((r for r in candidates if capability_mode in r["modes"]), None)
    if base is None:
        modes = sorted({mode for r in candidates for mode in r["modes"]})
        raise ValueError(f"compose-mode-unknown:{capability_mode} (modes: {','.join(modes)})")
    requested = intensity or SHAPE_INTENSITY[shape]
    if requested not in ORDER:
        raise ValueError("invalid intensity")
    if shape == "direct" and requested != "direct":
        raise ValueError("compose-shape-intensity-mismatch:direct (allowed: direct)")
    if shape == "solo" and requested != "quick":
        raise ValueError("compose-shape-intensity-mismatch:solo (allowed: quick)")
    if shape == "staged" and ORDER[requested] < ORDER["standard"]:
        raise ValueError("compose-shape-intensity-mismatch:staged (allowed: standard,strong,thorough,adversarial)")
    cwd = str(Path(cwd).resolve(strict=True))
    artifact_root = str(Path(artifact_root).resolve())
    # The campaign is the agent's proposal for the work stream (CONVENTIONS
    # "Campaign or cycle").  A route sealed without one silently landed in the
    # root's degraded `_unassigned` container (Project-C 2026-09-15: nine
    # routes, zero keys), so compose demands the proposal, shows the streams
    # that already exist, and accepts only an explicit opt-out.
    if unassigned and (campaign_key is not None or parent_cycle_id is not None):
        raise ValueError("compose-campaign-selection-conflict:--unassigned excludes --campaign-key and --parent-cycle")
    if campaign_key is None and parent_cycle_id is None and not unassigned:
        campaign_key = first_campaign_key(artifact_root, cwd)
        if campaign_key is None:
            raise ValueError("compose-campaign-key-required:" + compose_campaign_hint(artifact_root, cwd))
    if tracking is None:
        tracking = "tracked" if shape in ("staged", "framed") or resume_graph else "untracked"
    gate = None if selection_only else {
        "spec_read": compose_spec_read(cwd, artifact_root, spec_read),
        "drift_verdict": drift_verdict or DEFAULT_DRIFT_VERDICT,
        "workflow_mode": tracking,
        "artifact_guard": {"satisfied": True, "source": artifact_guard or DEFAULT_ARTIFACT_GUARD},
    }
    predicates = list(base["direct_predicates"]) if shape == "direct" else []
    if resume_graph:
        predicates = [p for p in predicates if p not in ("no-resource-run", "no-independent-verifier")]
    signals = sorted(set(signals or ()))
    if shape == "direct" and signals:
        raise ValueError("compose-direct-signals-conflict")
    if selection_pins:
        enabled = DEFAULTS.current_enabled_harnesses()
        for pin in selection_pins.values():
            if pin["harness"] not in enabled:
                raise ValueError("explicit-adapter-disabled-by-user-policy:" + pin["harness"])
    readiness = None
    find_input = None
    frame_ids = [n["id"] for n in base["standard_plus"]["nodes"] if _frame_node(n)]
    planned_full = shape == "staged" and not graph and frameless and bool(frame_ids)
    if shape == "staged" and (graph or planned_full or (execution_scope == "report" and frameless)) and not unassigned:
        try:
            import artifact_producer
            find_input = artifact_producer.input_source_finder(
                artifact_root, parent_cycle_id=parent_cycle_id, campaign_key=campaign_key,
                capability=capability, route_chain_identity=_route_chain_identity("compose", None))
        except ImportError:
            find_input = None
    capabilities = {recipe["capability"] for recipe in registry["recipes"]
                    if recipe["capability"] != ROUTE_FRAME_CAPABILITY}
    if find_input is not None and frameless:
        if parent_cycle_id is None and route_plan is None:
            find_input = _without_implicit_frame_briefs(registry, find_input)
        find_input = _frame_brief_finder(registry, capability, find_input)
    if planned_full:
        # No graph: the recipe's own order, minus the frame nodes the plan already ran.
        graph_spec = [(n["id"], None) for n in base["standard_plus"]["nodes"] if not _frame_node(n)]
    else:
        graph_spec = parse_graph_spec(graph, capabilities) if (shape == "staged" or resume_graph) and graph else None
    if shape == "staged" and execution_scope == "report":
        source_graph = ([f"{node}" + (f":{unit}" if unit else "") for node, unit in graph_spec]
                        if graph_spec is not None else None)
        projected = project_entry_execution_scope({"capability": capability, "mode": capability_mode,
                                                   "shape": shape, "graph": source_graph}, "report", registry)
        graph_spec = parse_graph_spec(",".join(projected.get("graph") or []), capabilities)
    if extra_stages is None and isinstance(route_plan, dict) and isinstance(route_plan.get("leg"), dict):
        extra_stages = route_plan["leg"].get("extra_stages")   # the plan's own stages, from the sealed leg
    selected_recipe = (compose_subgraph_recipe(registry, base, graph_spec, find_input,
                                                preserve_base_dependencies=execution_scope == "report",
                                                extra_stages=extra_stages, group_intensity=requested,
                                                complete_group_consumers=True)
                       if graph_spec is not None else base)
    if selection_only:
        if shape == "direct":
            nodes = [{"id": "inline", "write_scope": list(base["quick"]["write_scope"])}]
        elif shape == "solo" and not signals:
            if resume_graph and RESOURCE_RESUME.recipe_selected(base):
                nodes = RESOURCE_RESUME.nodes(base, registry["owner_profile_by_intensity"]["quick"])
            else:
                nodes = [{"id": "one-shot", "write_scope": list(base["quick"]["write_scope"])}]
        else:
            nodes = json.loads(json.dumps(selected_recipe["standard_plus"]["nodes"]))
            selected_intensity = "standard" if requested == "auto" else requested
            inferred_intensity = "standard" if signals else "quick"
            effective_intensity = max((selected_intensity, inferred_intensity), key=ORDER.get)
            nodes = _expand_parallel_groups(
                nodes, selected_recipe["standard_plus"].get("parallel_groups"), effective_intensity,
                capability, auxiliary_check_units=registry.get("auxiliary_check_units"))
        return {"capability": capability, "shape": shape, "cwd": cwd,
                "artifact_root": artifact_root, "slug": slug, "nodes": nodes}
    if execution_scope == "report" and shape == "staged" and graph_spec is not None and profile is None:
        # The narrowed graph is made only from existing recipe/catalog parts. Keep each
        # stage's declared profile so a report boundary adds no new profile-demand input.
        explicit_profiles = dict(explicit_profiles or {})
        for node in selected_recipe["standard_plus"]["nodes"]:
            if node.get("model_profile") and node["id"] not in explicit_profiles:
                explicit_profiles[node["id"]] = node["model_profile"]
    owner_only = shape == "staged" and _single_owner_nodes(selected_recipe["standard_plus"]["nodes"])
    gpu_intensity = max(("standard" if requested == "auto" else requested,
                        "standard" if signals else "quick"), key=ORDER.get)
    gpu_nodes = _expand_parallel_groups(
        json.loads(json.dumps(selected_recipe["standard_plus"]["nodes"])),
        selected_recipe["standard_plus"].get("parallel_groups"), gpu_intensity, capability,
        auxiliary_check_units=registry.get("auxiliary_check_units"))
    gpu_route = {"capability": capability, "nodes": gpu_nodes,
                 "selection": {"promotion_signals": [{"signal": s} for s in signals]}}
    if (shape == "staged" and not owner_only and dispatch_evidence is not None
            and GPU_SANDBOX.fresh_gpu_resource_nodes(gpu_route)):
        dispatch_evidence = _validate_dispatch_evidence(
            dispatch_evidence, DISPATCH_CONTRACT_VERSION,
            expected_worktree=cwd, require_scope=True)
        choice = GPU_SANDBOX.select_fresh(gpu_route)
        codex_rows = [row for row in dispatch_evidence.get("tuples", [])
                      if row.get("parent_harness") == "codex"]
        if any(row.get("parent_sandbox") != choice["sandbox"] for row in codex_rows):
            _fallback_chain(dispatch_evidence, expected_worktree=cwd, require_scope=True)
            # A planning frame checked its own normal owner. The selected GPU
            # leg needs a real prospective check of its chosen Codex owner,
            # before its tuple is sealed; never relabel the frame's evidence.
            readiness = _compose_readiness(cwd, jobs or _compose_default_jobs(), "codex",
                sorted({row["child_harness"] for row in codex_rows}), gpu_route=gpu_route)
            dispatch_evidence = {**dispatch_evidence, "tuples": [
                row for row in dispatch_evidence["tuples"] if row.get("parent_harness") != "codex"
            ] + readiness["tuples"]}
    owner_pin = (selection_pins or {}).get("owner", {}).get("harness")
    if shape in ("staged", "framed") and not owner_only and dispatch_evidence is None:
        readiness = _compose_readiness(cwd, jobs or _compose_default_jobs(), parent_harness,
                                       children or _compose_default_children(selection_pins),
                                       gpu_route=gpu_route if shape == "staged" else None,
                                       owner_harnesses=_compose_default_owners(selection_pins))
        dispatch_evidence = {"tuples": readiness["tuples"], "native_subagent": []}
    if shape == "staged" and not owner_only and owner_pin and not any(
            row.get("parent_harness") == owner_pin
            for row in (dispatch_evidence or {}).get("tuples", [])):
        # A frame's evidence describes its conductor. Check the selected owner
        # only when absent; a checked refusal is already its probe result.
        readiness = _compose_readiness(cwd, jobs or _compose_default_jobs(), owner_pin,
                                       children or _compose_default_children(selection_pins),
                                       gpu_route=gpu_route)
        dispatch_evidence = {**dispatch_evidence,
                             "tuples": dispatch_evidence["tuples"] + readiness["tuples"]}
    if (shape == "solo" or owner_only or resume_graph) and registered_headless_evidence is None:
        readiness = readiness or _compose_readiness(
            cwd, jobs or _compose_default_jobs(), parent_harness,
            children or _compose_default_children(selection_pins))
        registered_headless_evidence = {"candidates": readiness["candidates"]}
    common = dict(
        signals=signals, transport=None, transport_evidence=transport_evidence,
        tracking=tracking, tracked_gate_evidence=gate, slug=slug,
        campaign_key=campaign_key, parent_cycle_id=parent_cycle_id,
        dispatch_evidence=dispatch_evidence,
        registered_headless_evidence=registered_headless_evidence,
        route_origin="compose", shape=shape,
        profile_demands=profile_demands, explicit_profiles=explicit_profiles, profile=profile,
        route_plan=sealed_plan, frameless=frameless,
        **({"frame_legs": frame_legs} if shape == "framed" else {}),
    )
    if graph_spec is not None:
        recipe = selected_recipe
        route = compile_composed_route(
            recipe, capability_mode, requested, cwd, artifact_root,
            predicates=predicates, inline_reason=None, **common)
    else:
        route = compile_route(
            capability, capability_mode, requested, cwd, artifact_root,
            predicates=predicates, inline_reason="atomic-direct" if shape == "direct" else None,
            **common)
    if unassigned:
        route["campaign_unassigned"] = True
    # Observational provenance in the existing basis list, shared by compose
    # and later start; it never changes stage/profile selection or approvals.
    route["selection"]["selection_basis"].extend([
        {"axis": "compose-intensity", "signal": given_intensity or SHAPE_INTENSITY[shape],
         "source": "explicit" if given_intensity is not None else "shape-default"},
        {"axis": "compose-capability", "signal": given_capability or route["capability"],
         "source": ("frame-shape" if shape == "framed" else capability_source or
                   ("explicit" if given_capability is not None else "compose-default"))},
    ])
    if shape == "framed":
        route["selection"]["selection_basis"].append({
            "axis": "frame-legs", "signal": str(frame_leg_choice[0]),
            "source": frame_leg_choice[1], "reason": frame_leg_choice[2],
        })
    if work_request is not None:
        from work_start import capture_request_context
        if routing_hints:
            work_request = {**work_request, "routing_hints": routing_hints}
        route["work_request"] = capture_request_context(work_request, route)
    if selection_pins:
        _apply_selection_pins(route, selection_pins)
    if execution_scope is not None:
        route = _bind_entry_execution_scope(route, execution_scope)
        if work_request is not None:
            request = dict(route.get("work_request") or {})
            request["text"] = request.get("text", "").rstrip() + f"\n\nExecution scope: {execution_scope}\n"
            route["work_request"] = request
    # The compose input provenance also participates in the route seal.
    route["route_hash"] = route_hash(route)
    route["route_id"] = ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
    return route


def compile_proposal_leg(leg, index, *, frame_route, frame_cycle_id, readiness):
    """Memory compile of one proposal leg in the frame-less form a `--route-plan` compile uses.

    `readiness` is a zero-argument probe, called only for a leg that needs checked dispatch evidence.
    Nothing is written, started or recorded: no route file, no route-chain line, no producer call.
    The registry, graph, order, unit, scope and gate rules are the ones compose applies when it seals.
    """
    import route_plan as RP
    kwargs = _leg_compose_kwargs(RP.leg_arguments(leg), frame_route=frame_route, frame_cycle_id=frame_cycle_id,
                                 slug=f"{frame_route.get('slug') or 'framed'}-leg{index}")
    probe = (lambda: readiness(kwargs["cwd"])) if leg.get("cwd") else readiness
    return compose_route(**kwargs, **_leg_evidence(leg, probe), frameless=True,
                         extra_stages=leg.get("extra_stages"))


def compile_first_leg(leg, *, frame_route, frame_cycle_id, context, binding, work_request, readiness, index=0):
    import frame_repository
    repository = frame_repository.RepositoryBinding(binding.get("record"), index)
    if index and repository.active:
        # A later leg keeps the dedicated repository only while it names that
        # same approved destination. Compare lexical input before resolving any
        # mutable ancestor; explicit alternate repositories keep their policy.
        raw = Path(leg.get("cwd") or frame_route["cwd"]).expanduser()
        destination = os.path.abspath(raw if raw.is_absolute() else Path(context["cwd"]) / raw)
        if destination != repository.setup["cwd"]:
            repository = frame_repository.RepositoryBinding(None)
    with repository:
        return _compile_first_leg(leg, frame_route=frame_route, frame_cycle_id=frame_cycle_id,
                                  context=context, binding=binding, work_request=work_request,
                                  readiness=readiness, index=index, repository=repository)


def _compile_first_leg(leg, *, frame_route, frame_cycle_id, context, binding, work_request, readiness,
                       index, repository):
    """An approved leg, compiled in memory with its route-plan reference sealed.

    Same arguments the printed compose command carries: the leg's shape, graph, capability, mode and
    intensity, `--route-plan <record>#<index>`, the previous cycle as parent and the same campaign.
    The first leg compiles from the frame route; a later leg from the leg before it.
    """
    import route_plan as RP
    kwargs = _leg_compose_kwargs(RP.leg_arguments(leg), frame_route=frame_route, frame_cycle_id=frame_cycle_id,
                                 slug=f"{context['slug']}-leg{index}")
    kwargs["cwd"] = RP.leg_cwd(leg, frame_route["cwd"], base_cwd=context["cwd"])
    repository.verify(kwargs["cwd"])
    owner = ((kwargs["selection_pins"] or {}).get("owner") or {}).get("harness") or context.get("owner")
    if work_request is not None:
        work_request = {**work_request, "owner_harness": owner or work_request.get("owner_harness")}
    selection = compose_route(**kwargs, work_request=work_request, route_plan=binding,
                              parent_harness=owner or "claude", selection_only=True)
    repository.verify(kwargs["cwd"])
    diagnosis = _compose_git_diagnosis(kwargs["cwd"])
    if (diagnosis.get("state") == "non-git" and selection.get("shape") != "direct"
            and _isolates_worktree(selection)):
        raise ValueError(UI_TEXT.translator()("route.error.not_git_worker"))
    frame_preparation = frame_route.get("framed_worktree") or {}
    inherit_frame_allocation = _inherits_frame_worktree(frame_route, leg, index, kwargs["cwd"])
    use_new_repository = repository.active
    if _isolates_worktree(selection) and not inherit_frame_allocation and not use_new_repository:
        worktree = prepare_isolated_worktree(
            kwargs["cwd"], frame_route.get("slug"), artifact_root=frame_route["artifact_root"],
            preparation_key=_route_plan_preparation_key(binding), explicitly_approved=bool(leg.get("cwd")))
        if worktree.get("state") == "failed":
            raise ValueError("source-worktree-preparation-failed:" + worktree.get("reason", "unknown")
                             + (":" + worktree["detail"] if worktree.get("detail") else ""))
        if worktree.get("cwd"):
            kwargs["cwd"] = worktree["cwd"]
    # The probe is collected only after preparation, against the exact cwd that will be sealed.
    route = compose_route(**kwargs, **_leg_evidence(leg, lambda: readiness(kwargs["cwd"])),
                          work_request=work_request, route_plan=binding, parent_harness=owner or "claude")
    repository.verify(route["cwd"])
    if inherit_frame_allocation:
        route = _seal_framed_worktree(route, {**frame_preparation, "state": "inherited",
                                              "from_route_id": frame_route["route_id"]})
    scope = route_plan_execution_scope(binding)
    if scope in ("complete", "report"):
        route = _bind_entry_execution_scope(route, scope)
    return route


def compile_composed_route(composed_recipe, capability_mode, requested_intensity, cwd, artifact_root,
                           **kwargs):
    """Compile a compose-on-demand recipe through the SAME validate/seal path (composed: true)."""
    registry=TOPO.load_registry(); TOPO.validate_registry(registry)
    if not isinstance(composed_recipe, dict): raise ValueError("composed recipe must be an object")
    TOPO._validate_recipe(
        composed_recipe, registry,
        registry["owner_profile_by_intensity"]["standard"],
    )
    # SAME validator means gates too: without this, a composed recipe could carry a
    # forged completion gate that no registry contract backs (2026-07-22 verify finding).
    TOPO._validate_gate_contracts(composed_recipe, registry)
    _validate_compose_owner_close(registry, composed_recipe,
                                 "standard" if requested_intensity == "auto" else requested_intensity)
    if capability_mode not in composed_recipe.get("modes", []):
        raise ValueError("composed recipe does not declare the requested capability mode")
    return _compile_from_recipe(
        registry, composed_recipe, composed_recipe["capability"], capability_mode,
        requested_intensity, cwd, artifact_root, composed=True, **kwargs)

def _compile_from_recipe(registry, recipe, capability, capability_mode, requested_intensity,
                         cwd, artifact_root, predicates=(), signals=(), transport=None,
                         transport_evidence="caller-selected", inline_reason=None,
                         tracking="tracked", tracked_gate_evidence=None, dispatch_evidence=None,
                         registered_headless_evidence=None, slug=None, composed=False,
                  route_origin="preset", shape=None, profile_demands=None,
                  explicit_profiles=None, campaign_key=None, parent_cycle_id=None, profile=None,
                  route_plan=None, frameless=False, frame_legs=2):
    dispatch_terminal_commit.require_current_cleanup("route-compile")
    if route_origin not in ROUTE_ORIGINS: raise ValueError("invalid route origin")
    if (capability==ROUTE_FRAME_CAPABILITY) != (shape=="framed"):
        raise ValueError(f"compose-shape-invalid:{shape}")
    if capability==ROUTE_FRAME_CAPABILITY and frame_legs==1:
        recipe=_one_leg_frame_recipe(recipe)
    cwd=Path(cwd).resolve(strict=True); artifact=Path(artifact_root).resolve()
    if not cwd.is_absolute() or not artifact.is_absolute(): raise ValueError("cwd and artifact root must be absolute")
    slug_fields={}
    if slug is not None:
        # A caller-typed leading date is dropped once, here at the origin: every
        # locator built from this slug prefixes the record's own date, so KWS_Net
        # 2026-09-10 accumulated names like `2026-09-10_2026-09-10-r5-...` and one
        # `2026-09-09_2026-09-10-r4-...` whose two dates disagreed. Migration
        # naming does not pass through here and keeps both dates by design.
        canonical_slug,slug_truncated=ARTIFACT_LOCATOR.slugify(
            ARTIFACT_LOCATOR.strip_leading_date(slug))
        slug_fields={"slug":canonical_slug,"slug_truncated":slug_truncated}
    known_pred=set(recipe["direct_predicates"]); predicates=sorted(set(predicates))
    unknown=set(predicates)-known_pred
    if unknown: raise ValueError("unknown predicates: "+",".join(sorted(unknown)))
    signals=sorted(set(signals))
    if set(signals) & TRACKING: raise ValueError("tracking cannot be an escalation signal")
    unknown=set(signals)-set(recipe["promotion_signals"])
    if unknown: raise ValueError("unknown promotion signals: "+",".join(sorted(unknown)))
    requested="standard" if requested_intensity=="auto" else requested_intensity
    if requested not in ORDER: raise ValueError("invalid intensity")
    if transport is not None and transport not in WRAPPER_TRANSPORTS:
        raise ValueError(f"invalid transport: {transport!r}")
    inferred="standard" if signals else ("direct" if set(predicates)==known_pred else "quick")
    effective=max((requested,inferred),key=ORDER.get)
    resume_recipe = composed and RESOURCE_RESUME.recipe_selected(recipe) and effective == "quick"
    if composed and effective in ("direct","quick") and not resume_recipe:
        raise ValueError("composed routes require a standard+ effective intensity")
    registered_headless_candidates=None
    if effective=="direct":
        transport="interactive"
        if inline_reason is None: inline_reason="atomic-direct"
        owner_model_profile=None
        nodes=[{"id":"inline","kind":"capability-owner","dispatch_depth":0,"role":"orchestrator",
                "write_scope":recipe["quick"]["write_scope"],"resource_class":"normal",
                "execution_surface":"inline","registered_worker":False,
                "completion_gate":"inline-complete",
                "terminal":True,"terminal_gate":"inline-complete"}]
        gates=["inline-complete"]
        # compose fills every direct predicate itself (it has no --predicate
        # flag); label them so the record never claims the caller asserted them.
        predicate_source="compose-default" if route_origin=="compose" else "caller"
        selection_basis=[{"axis":"direct-predicate","signal":p,"source":predicate_source} for p in predicates]
    elif effective=="quick":
        if transport not in (None, "headless"):
            raise ValueError(f"invalid quick transport: {transport!r}")
        if frameless:
            # A leg of an approved route plan was framed already: its one-shot runs without the quick frame pair.
            recipe=_frameless_recipe(recipe)
        if (requested=="direct" and set(predicates)!=known_pred
                and registered_headless_evidence is None):
            # H6: an explicit direct request whose predicates do not all hold
            # used to be silently promoted to quick and died as an opaque
            # `quick-headless-unavailable`. Refuse instead and name the gap.
            # With checked quick evidence the promotion still compiles
            # (`test_ambiguous_quick`); the gaps stay recorded in
            # selection_basis either way.
            raise ValueError(
                "direct-predicate-gap:"
                +",".join(sorted(known_pred-set(predicates))))
        registered_headless_candidates=_validate_registered_headless_evidence(
            registered_headless_evidence
        )
        # Quick's supported harness inventory is carried entirely by this candidate
        # list -- `route_authority.depth1_supported_harnesses` reads it, and
        # `dispatch-owner.py` refuses an `--adapter` outside it. How many
        # harnesses it supports decides the pair's diversity, sealed on both
        # frame nodes by `_quick_frame_diversity` (shared with `verify_route`).
        #
        # Deliberately NOT touched: `EVIDENCE_CONSUMER_DISPATCH_DEPTH` and the
        # depth-2 evidence-consumer path. Quick has no depth-2 node, so making
        # that depth configurable would mean redefining five call sites plus
        # fallback-chain attachment for no gain here.
        _frame_diversity=_quick_frame_diversity(registered_headless_candidates)
        transport="headless"
        owner_model_profile=registry["owner_profile_by_intensity"]["quick"]
        # What quick's frame legs LOSE compared to standard+: no
        # `dispatch_evidence.tuples` per-field sealing of
        # parent_transport/parent_sandbox/child_harness, and no `fallback_hops`
        # chain at all. Recovery from a dead quick frame leg is an explicit
        # depth-0 re-launch, never a machine fallback hop. Do not read quick's
        # frame pair as carrying the standard+ guarantee.
        _quick_frame=lambda node_id,profile:{
                "id":node_id,"kind":"map-worker","depends_on":[],"role":"deep maker",
                "unit":"plan/frame","worker_type":"frame","dispatch_depth":1,
                "launch_authority":"depth-0","model_profile":profile,
                "inputs":["task"],
                "outputs":[f"shards/{node_id}/direction-brief.md"],
                "write_scope":[f"shards/{node_id}/**"],"resource_class":"normal",
                "execution_surface":"registered-headless","registered_worker":True,
                "completion_gate":"quick-frame",
                "harness_diversity":_frame_diversity,
                "continuation":{"kind":"human-gate","gate":"frame-review"},
                "advance_class":"runtime-eligible","commit_expected":False}
        nodes=[_quick_frame("frame","balanced-deep"),
               _quick_frame("frame-alternative","light"),
               {"id":"one-shot","kind":recipe["quick"]["worker_kind"],"dispatch_depth":1,"role":"orchestrator",
                "depends_on":["frame","frame-alternative"],
                "unit":"_kernel/owner","worker_type":"owner",
                "model_profile":owner_model_profile,
                "write_scope":recipe["quick"]["write_scope"],"resource_class":"normal",
                "execution_surface":"registered-headless","registered_worker":True,
                "completion_gate":"quick-complete",
                "terminal":True,"terminal_gate":"quick-complete"}]
        if recipe["quick"].get("inline_human_gates"):
            nodes[-1]["inline_human_gates"] = list(recipe["quick"]["inline_human_gates"])
        gates=["quick-frame","quick-complete"]
        if not _recipe_has_frame(recipe):
            nodes = [nodes[-1]]
            nodes[0]["depends_on"] = []
            gates = ["quick-complete"]
        if resume_recipe:
            nodes = RESOURCE_RESUME.nodes(recipe, owner_model_profile)
            gates = ["resource-exit", "quick-complete"]
        selection_basis=[{"axis":"direct-predicate-gap","signal":p,"source":"compiler"} for p in sorted(known_pred-set(predicates))]
    else:
        if transport not in (None, "headless"):
            raise ValueError(f"invalid standard+ transport: {transport!r}")
        transport="headless"
        owner_model_profile=registry["owner_profile_by_intensity"][effective]
        nodes=json.loads(json.dumps(recipe["standard_plus"]["nodes"])); gates=recipe["completion_gates"]
        nodes=_expand_parallel_groups(
            nodes, recipe["standard_plus"].get("parallel_groups"), effective,
            capability,
            auxiliary_check_units=registry.get("auxiliary_check_units"),
        )
        for node in nodes:
            node.pop("fallback_hops", None)
        selection_basis=[{"axis":"promotion","signal":s,"source":"caller"} for s in signals]
    _validate_output_scopes(nodes)
    if effective != "direct" and inline_reason is not None:
        raise ValueError("inline_reason only applies to direct")
    if effective=="direct" and inline_reason not in registry["inline_reasons"]:
        raise ValueError("structured inline_reason required")
    evidence=_validate_tracking_evidence(tracking, tracked_gate_evidence)
    checked_dispatch=None
    if effective not in ("direct","quick") and _single_owner_nodes(nodes):
        registered_headless_candidates = _validate_registered_headless_evidence(registered_headless_evidence)
    elif effective not in ("direct","quick"):
        parent_dispatch_depth=_evidence_parent_dispatch_depth(
            nodes, recipe["standard_plus"]["owner_dispatch_depth"])
        checked_dispatch=_validate_dispatch_evidence(
            dispatch_evidence, DISPATCH_CONTRACT_VERSION, parent_dispatch_depth,
            expected_worktree=cwd, require_scope=True)
        chain=_fallback_chain(
            checked_dispatch, DISPATCH_CONTRACT_VERSION, parent_dispatch_depth,
            expected_worktree=cwd, require_scope=True)
        for node in nodes:
            if node.get("dispatch_depth")==2:
                node["fallback_hops"]=json.loads(json.dumps(chain))
    if profile is not None:
        if profile not in PROFILE.PORTABLE_PROFILES:
            raise ValueError("profile-explicit-unknown:" + str(profile))
        explicit_profiles = {**{n["id"]: profile for n in nodes if not _no_model_node(n)},
                             "__owner__": profile, **(explicit_profiles or {})}
    profile_demands, explicit_profiles = _profile_input_maps(nodes, profile_demands, explicit_profiles)
    owner_demand = profile_demands.get("__owner__")
    owner_model_profile, owner_profile_selection = _resolve_owner_profile(
        effective, registry, owner_demand, explicit_profiles.get("__owner__"))
    resolved_owner_profile = owner_profile_selection["resolved_profile"]
    if owner_demand is not None or "__owner__" in explicit_profiles:
        # Quick's one-shot is the owner process, so there is one selection.
        # Semantic owner stages in standard+ remain independently selected.
        for node in nodes:
            if _owner_node(node, effective):
                node["model_profile"] = resolved_owner_profile
                node["profile_explicit"] = True
                node["profile_demand"] = owner_demand
    # The frame bootstrap tier ladder, applied for BOTH shapes at once, after
    # `resolved_owner_profile` and before `_seal_profile_demands`. Quick's
    # frame pair is built literally above by `_quick_frame`, and the five
    # standard+ recipes declare theirs in `topologies.json` -- both carry a
    # static `model_profile` that CANNOT be right, because the correct value
    # depends on the owner profile this route just resolved, which no static
    # recipe field can see. Stamping unconditionally is exactly what demotes
    # those static values to placeholders instead of letting one decision live
    # in two homes. The one home is `model_profile.FRAME_PROFILE_LADDER`.
    _stamp_frame_profiles(nodes, resolved_owner_profile, owner_demand)
    legacy_nodes = not composed or _versioned_subgraph(registry, recipe)
    _seal_profile_demands(nodes, profile_demands, explicit_profiles, legacy=legacy_nodes)
    for node in nodes:
        if _owner_node(node, effective) and node["model_profile"] != owner_model_profile:
            raise ValueError("owner-node-profile-selection-conflict:" + node["id"])
    dispatch_defaults_digest,dispatch_allocation,owner_harness_policy=_seal_dispatch_defaults(
        nodes, capability, owner_model_profile
    )
    spec_touch=any(_scope_touches_spec(scope) for node in nodes for scope in node["write_scope"])
    # Seal the same finite workload derivation used by legacy bound routes.
    _continuation_declared_nodes=len(nodes)
    _continuation_retry_slots=len(set(recipe["resume_retry_boundaries"]))
    _continuation_review_round_cap=REVIEW_ROUND_CAP.max_review_rounds(effective)
    _continuation_terminal_nodes=sum(1 for node in nodes if node.get("terminal") is True)
    _continuation_ordinary=derive_workload_ordinary(
        declared_nodes=_continuation_declared_nodes,
        retry_slots=_continuation_retry_slots,
        review_round_cap=_continuation_review_round_cap,
        terminal_nodes=_continuation_terminal_nodes,
    )
    continuation_budget={
      "contract_version":1,
      "declared_nodes":_continuation_declared_nodes,
      "review_round_cap":_continuation_review_round_cap,
      "retry_slots":_continuation_retry_slots,
      "terminal_nodes":_continuation_terminal_nodes,
      "gap":1,"retry":1,"reserved":TERMINAL_RESERVE_DEFAULT,
      "ordinary":_continuation_ordinary,
      "limit":_continuation_ordinary+TERMINAL_RESERVE_DEFAULT,
    }
    validation_basis=_validation_basis()
    payload={
      "schema_version":ROUTE_SCHEMA_VERSION,"capability":capability,"capability_mode":capability_mode,
      "requested_intensity":requested_intensity,"effective_intensity":effective,
      "owner_model_profile":owner_model_profile,
      "profile_selection_contract_version":1,
      "persona_independence_contract_version":1,
      "profile_demands":profile_demands,"explicit_profiles":explicit_profiles,
      "owner_profile_demand":owner_demand,"owner_profile_selection":owner_profile_selection,
      "execution_topology":("resource+verification" if resume_recipe else "inline" if effective=="direct" else recipe["quick"]["topology"] if effective=="quick" else recipe["topology_class"]),
      "owner_dispatch_depth":0 if effective=="direct" else (recipe["quick"]["owner_dispatch_depth"] if effective=="quick" else recipe["standard_plus"]["owner_dispatch_depth"]),
      "max_dispatch_depth":recipe["quick"]["max_dispatch_depth"] if effective=="quick" else (0 if effective=="direct" else recipe["standard_plus"]["max_dispatch_depth"]),
      "tracking":tracking,"tracked_gate_evidence":evidence,"spec_touch":spec_touch,
      "artifact_storage_contract_version":2,
      "cwd":str(cwd),"artifact_root":str(artifact),"source_commit":_git_commit(cwd),
      "registry_digest":TOPO.registry_digest(registry),
      "capability_registry_digest":TOPO.capability_registry_digest(
          registry, capability, [recipe] if composed else ()),
      "dispatch_defaults_digest":dispatch_defaults_digest,
      "dispatch_allocation":dispatch_allocation,
      "owner_harness_policy":owner_harness_policy,
      "confirmation_mode":_seal_confirmation_mode(),
      "small_work_confirmation":_seal_small_work_confirmation(),
      "selection":{"direct_predicates":predicates,"promotion_signals":[{"signal":s,"source":"caller"} for s in signals],
                   "selection_basis":selection_basis,
                   "escalation_basis":[{"signal":s,"source":"caller"} for s in signals],
                   "transport":transport,"transport_evidence":transport_evidence,"inline_reason":inline_reason,
                   "route_origin":route_origin,"shape":shape or shape_for_intensity(effective)},
      "continuation_budget":continuation_budget,
      "nodes":nodes,"parallel_groups":_realized_parallel_groups(nodes),
      "conditional_extensions":_realize_conditional_extensions(recipe, effective),
      "completion_gates":gates,
      # The portable recipe owns whether this capability has a frame layer.
      # Quick must not extend it to capabilities outside the five recipes.
      "human_gates":(sorted(set(recipe["human_gates"])|{"frame-review"})
                     if effective=="quick" and _recipe_has_frame(recipe) else recipe["human_gates"]),
      # Quick binds the frame gate at `one-shot`'s entry: that is quick's only
      # fence point, and without it quick runs with no check at all. Only
      # `direct` (which has no owner and no worker) still binds nothing.
      "human_gate_bindings":json.loads(json.dumps(
          _quick_gate_bindings(recipe) if effective=="quick"
          else recipe["human_gate_bindings"] if effective!="direct" else [])),
      "workflow_contract":_workflow_contract(
          registry, nodes,
          _quick_gate_bindings(recipe) if effective=="quick"
          else recipe["human_gate_bindings"] if effective!="direct" else []),
      "resume_retry_boundaries":recipe["resume_retry_boundaries"],
      "dispatch_evidence":checked_dispatch,
      "dispatch_contract_version":DISPATCH_CONTRACT_VERSION,
      "registered_headless_candidates":registered_headless_candidates,
      "registered_headless_policy":"serial-attempt" if effective=="quick" else None,
      "unit_catalog_digest":unit_catalog_digest(),
      "validation_basis":validation_basis,
      "launch_compatibility_tuple":{
          "contract_version":LAUNCH_COMPATIBILITY_TUPLE_VERSION,
          **launch_compatibility_tuple(artifact_root=artifact,cwd=cwd),
      },
      "advance_generation":0,
      "runtime_support":{"terminal_commit":_seal_terminal_commit_support(validation_basis),
                         "terminal_commit_contract":RUNTIME_SUPPORT.TERMINAL_COMMIT_CONTRACT,
                         "terminal_handoff_contract":RUNTIME_SUPPORT.TERMINAL_HANDOFF_CONTRACT,
                         "producer_binding_contract":RUNTIME_SUPPORT.PRODUCER_BINDING_CONTRACT}}
    if GPU_SANDBOX.fresh_gpu_resource_nodes(payload):
        payload["codex_execution_sandbox"] = GPU_SANDBOX.select_fresh(payload)
    payload.update(slug_fields)
    for key, value in (("campaign_key", campaign_key), ("parent_cycle_id", parent_cycle_id)):
        if value is not None:
            _validate_campaign_selection(key, value)
            payload[key] = value
    if checked_dispatch is not None:
        payload["dispatch_evidence_scope_version"]=DISPATCH_EVIDENCE_SCOPE_VERSION
    if composed:
        payload["composed"]=True
        payload["composed_recipe"]=json.loads(json.dumps(recipe))
    if route_plan is not None:
        payload["route_plan"]=json.loads(json.dumps(route_plan))
    import frame_repository
    source_repository = frame_repository.capture_source(cwd)
    if source_repository:
        payload["source_repository"] = source_repository
    digest=route_hash(payload); payload["route_hash"]=digest; payload["route_id"]=ROUTE_IDENTITY.route_id_from_hash(digest)
    owner_attempt_id=_resolve_owner_attempt_id()
    payload["owner_attempt_id"]=owner_attempt_id
    payload["route_family_key"]=route_family_key(capability,cwd,capability_mode,owner_attempt_id)
    return payload

def _route_autoclose(artifact_root, trigger, route=None):
    """The runtime closes routes nobody works on any more (utilities/route_autoclose.py).
    Bookkeeping only: it never fails or blocks the command that triggered it.
    With `route`, only that route's own campaign is swept; a campaign not yet begun has nothing to close."""
    try:
        import route_autoclose
        api=sys.modules.get(__name__)
        if api is None: return
        scope={}
        if route is not None:
            found=route_autoclose.campaign_of_route(Path(artifact_root),route)
            if found is None: return
            scope={"scope_campaign_id":found[0],"scope_dir":found[1],
                   "scope_key":route_autoclose.campaign_key_of(route)}
        route_autoclose.report(route_autoclose.sweep(artifact_root,api=api,trigger=trigger,**scope))
    except Exception as exc:  # noqa: BLE001
        print(f"route_autoclose error={type(exc).__name__}",file=sys.stderr)


@compose_command_snapshot
def main():
    from dispatch_parent_completion import default_parent_harness
    p=argparse.ArgumentParser(allow_abbrev=False)
    sub=p.add_subparsers(dest="command",required=True,
                         parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))
    c=sub.add_parser("compile"); c.add_argument("--capability",required=True); c.add_argument("--capability-mode",default="default")
    c.add_argument("--slug",required=True)
    c.add_argument("--campaign-key",help="explicit work stream passed to the producer owner")
    c.add_argument("--parent-cycle",help="open or sealed predecessor cycle; causal link, not input approval")
    c.add_argument("--profile-demands", help="JSON file mapping node ids and __owner__ to full SD-88 demands")
    c.add_argument("--explicit-profiles", help="JSON file mapping demanded node ids to explicit profiles")
    c.add_argument("--intensity",default="auto"); c.add_argument("--cwd",required=True); c.add_argument("--artifact-root",required=True)
    c.add_argument("--predicate",action="append",default=[]); c.add_argument("--signal",action="append",default=[])
    c.add_argument("--transport",default=None); c.add_argument("--transport-evidence",default="caller-selected")
    c.add_argument("--inline-reason"); c.add_argument("--tracking",choices=sorted(TRACKING),default="tracked")
    c.add_argument("--dispatch-evidence",help="JSON file with checked nested tuples/native evidence")
    c.add_argument("--registered-headless-evidence",help="JSON file with checked quick or single-owner candidates")
    c.add_argument("--composed-recipe",help="JSON file with a compose-on-demand recipe (sealed composed: true)")
    # The tracked-gate fields take compose's defaults; an explicit value is recorded as given.
    c.add_argument("--spec-read",default=None,help="default: compose's spec check")
    c.add_argument("--drift-verdict",default=DEFAULT_DRIFT_VERDICT)
    c.add_argument("--workflow-mode",choices=sorted(TRACKING),default=None,help="default: --tracking")
    c.add_argument("--artifact-guard",default=DEFAULT_ARTIFACT_GUARD)
    c.add_argument("--output")
    cp=sub.add_parser("compose",help="preset-free work route: name the shape (and stage subgraph), defaults fill the rest")
    cp.add_argument("--slug",default=None,help="default: named from the task's first line with ASCII words")
    cp.add_argument("--start",action="store_true",help="prepare and start the selected work; the runtime owns frame launches and waiting")
    cp.add_argument("--prompt-file",type=Path,help="the user's task, stored with the route for frame and owner execution")
    cp.add_argument("--owner",choices=("claude","codex","opencode"),help="explicit owner runtime; otherwise use normal selection (same as --pin owner=<harness>)")
    cp.add_argument("--pin",action="append",default=[],metavar="TARGET=HARNESS[:MODEL[@EFFORT]]",
                    help="choose the tool (and optionally model and effort) once; the route seals it for the owner, "
                         "frame legs or depth-2 workers and every resume/replacement reuses it. TARGET is owner|frame|worker, "
                         "e.g. --pin owner=opencode:<provider/model>@<effort> --pin worker=opencode. "
                         "Top models are accepted for frame only (owner/worker keep the tool and drop the model with a warning)")
    cp.add_argument("--campaign-key",help="the work stream this route joins or creates (default: this session's latest campaign in the same artifact root; a folder name or near spelling of one active campaign joins it); `artifact_producer.py campaign-list` shows active keys. Size it as a stream with a one-sentence closing condition — not a project name, not a one-cycle task (join the stream that task serves)")
    cp.add_argument("--unassigned",action="store_true",help="explicit opt-out: keep this work in the root's degraded _unassigned container, proposing no stream")
    cp.add_argument("--parent-cycle",help="open or sealed predecessor cycle; causal link, not input approval")
    cp.add_argument("--plan",default=None,help="optional declared capability sequence for this session's route chain, e.g. research,draft,apply; shown in Fleet, not sealed into the route")
    cp.add_argument("--route-plan",default=None,metavar="RECORD#INDEX",help="runtime-generated: leg INDEX of an approved route decision record; compiles without frame nodes and seals its reference")
    cp.add_argument("--profile-demands", help="JSON file mapping node ids and __owner__ to full SD-88 demands")
    cp.add_argument("--explicit-profiles", help="JSON file mapping demanded node ids to explicit profiles")
    cp.add_argument("--profile", choices=sorted(PROFILE.PORTABLE_PROFILES),
                    help="explicit model budget for owner and model nodes; node-specific --explicit-profiles takes precedence")
    cp.add_argument("--shape",choices=COMPOSE_SHAPES,default=None,help="direct (inline) | solo (one registered owner) | staged (capability recipe, optionally narrowed by --graph) | framed (two top frame legs propose the route by default; explicit quick/direct may use one deep leg for unprotected work; --capability, --capability-mode, --graph and --profile are recorded as hints only); default: framed; use --shape direct for inline work; --graph without --shape is a frame routing hint")
    cp.add_argument("--graph",default=None,help="optional staged subgraph in your order (see `capability-route.py stages --capability <cap>` for valid ids); incompatible inherited parallel presets are omitted; optional :unit override, e.g. execute,test,report")
    cp.add_argument("--execution-scope", choices=("complete", "report"), default=None,
                    help="optional scope already selected in the existing start confirmation; it is not approval evidence")
    cp.add_argument("--capability",default=None,help=f"default {COMPOSE_DEFAULT_CAPABILITY}; a hint only with --shape framed"); cp.add_argument("--capability-mode",default=None)
    cp.add_argument("--intensity",default=None,help="default by shape: direct/quick/standard; framed defaults to two frame legs, while explicit quick/direct may use one for unprotected work; staged accepts strong+")
    cp.add_argument("--cwd",default=None,help="default: current directory"); cp.add_argument("--artifact-root",default=None,help="default: utilities/artifact-root.sh for cwd")
    cp.add_argument("--signal",action="append",default=[],
                    help="promotion signal (repeatable), e.g. shared-contract: promotes shared spec/contract "
                         "work to a registered owner by forcing at least standard effective intensity, "
                         "even when --intensity would otherwise infer direct/quick")
    cp.add_argument("--spec-read",default="auto",help="auto: refuse when a spec/prd.md exists unless you name it here")
    cp.add_argument("--drift-verdict",default=None); cp.add_argument("--tracking",choices=sorted(TRACKING),default=None)
    cp.add_argument("--artifact-guard",default=None)
    cp.add_argument("--children",default=None,help="comma list of child harnesses to probe for staged/solo (default: every harness enabled in the dispatch-defaults policy, plus pinned ones)")
    cp.add_argument("--parent-harness",default=None,choices=("claude","codex","opencode"),help="default: actual parent runtime")
    cp.add_argument("--jobs",default=None,help="registry for the readiness probe (default AGENT_DISPATCH_JOBS or the stable state root)")
    cp.add_argument("--dispatch-evidence",help="checked evidence JSON (skips the live probe)")
    cp.add_argument("--registered-headless-evidence",help="checked quick or single-owner candidates JSON (skips the live probe)")
    cp.add_argument("--transport-evidence",default="compose-default")
    cp.add_argument("--explain",action="store_true",help="print the [경로] card and the sealed graph without writing the route")
    cp.add_argument("--output")
    cp.add_argument("--full-record",action="store_true",help="print all sealed evidence; default prints choices and the canonical route_file")
    cp.add_argument("--help-all",action="help",help="also show advanced/compatibility inputs")
    if "--help-all" not in sys.argv:
        advanced = {"route_plan", "parent_cycle", "profile_demands", "explicit_profiles", "intensity", "artifact_root",
                    "drift_verdict", "tracking", "artifact_guard", "parent_harness", "jobs",
                    "dispatch_evidence", "registered_headless_evidence", "transport_evidence", "output", "full_record"}
        for option in cp._actions:
            if option.dest in advanced:
                option.help = argparse.SUPPRESS
    start=sub.add_parser("start",help="continue one sealed work request; existing attempts are reused")
    start.add_argument("--route",type=Path,default=None,
                       help="default: this session's newest open route, else the one open route of this cwd")
    start.add_argument("--jobs",type=Path,default=None)
    start.add_argument("--wait",action="store_true",help="the receipt's bounded wait for a parent without an automatic carrier")
    start.add_argument("--interview",type=Path,help="semantic frame question; runtime owns its registration and cycle fields")
    start.add_argument("--answers",type=Path,help="actual native answers; runtime records intent and releases the gate")
    start.add_argument("--decision",choices=("proceed","revise","stop"),default="proceed")
    start.add_argument("--pin",action="append",default=[],metavar="TARGET=HARNESS[:MODEL[@EFFORT]]",
                       help="the route's parent moves an owner, frame or worker pin; the sealed route stays, the "
                            "change is recorded beside it and applies from that target's next launch")
    finish=sub.add_parser("finish",help="finish one current-session direct route and seal its exact producer cycle")
    finish.add_argument("--route",help="route file or route id (rt-...); defaults to this session's latest route here")
    finish.add_argument("--evidence",required=True,metavar="FILE|URL",
                        help="a file anywhere or an http(s) URL; finish records it inside the route's cycle")
    finish.add_argument("--summary-file",required=True,type=Path)
    finish.add_argument("--commit",help="full result commit; defaults to current HEAD on first claim")
    correction=sub.add_parser("correct", help="deliver a correction to one existing owner; without a message file, inspect its receipts")
    correction.add_argument("--attempt-id", help="the owner attempt; a route id or route file names that route's "
                                                 "current owner (default: this session's open route)")
    correction.add_argument("--route", dest="correct_route", help="route id or file; same as --attempt-id <route>")
    correction.add_argument("--jobs", type=Path)
    correction.add_argument("--message-file", type=Path)
    correction.add_argument("--request-id", help="stable idempotency key; defaults to the message digest")
    co=sub.add_parser("continuation")
    co.add_argument("--source-route",required=True)
    co.add_argument("--resume-from-node",required=True)
    co.add_argument("--requested-boundary",required=True)
    co.add_argument("--reason",required=True)
    co.add_argument("--artifact-root",help="defaults to the source route's own artifact root")
    co.add_argument("--output")
    co.add_argument("--dispatch-evidence",help="optional checked nested evidence for this continuation; source evidence stays unchanged")
    co.add_argument("--lineage-operation",choices=("resume","fork"),default="resume")
    co.add_argument("--thread-id")
    co.add_argument("--new-thread-id")
    co.add_argument("--forked-from-id")
    co.add_argument("--last-turn-id")
    co.add_argument("--ephemeral",action="store_true")
    co.add_argument("--partial-group-manifest")
    co.add_argument("--source-group-id")
    co.add_argument("--failed-source-attempt-id")
    co.add_argument("--gap-leg-id")
    v=sub.add_parser("verify"); v.add_argument("--route",required=True); v.add_argument("--cwd")
    v.add_argument("--launch-phase",choices=("dry-run","register","start"))
    n=sub.add_parser("node"); n.add_argument("--route",required=True); n.add_argument("--node",required=True)
    d=sub.add_parser("complete"); d.add_argument("--route",required=False,default=None); d.add_argument("--node",required=True); d.add_argument("--evidence",required=False,default=None); d.add_argument("--output")
    d.add_argument("--inline",action="store_true",help="owner runs a declared stage itself: derive route/attempt axes, keep the long form working")
    d.add_argument("--reason",help="one line naming what was done inline (stored with a synthesized evidence file when --evidence is omitted)")
    d.add_argument("--jobs",help="canonical registry path for a registered attempt "
                                 "(default AGENT_DISPATCH_JOBS once an attempt is named)")
    d.add_argument("--attempt-id",help="exact current attempt, or an official continuation's blocking source review "
                                       "(default AGENT_DISPATCH_ATTEMPT_ID when that row is this node's attempt)")
    d.add_argument("--check",action="store_true",help="read-only check of exact current or ancestor owner-closure authority; publishes nothing")
    d.add_argument("--dispatch-depth",type=int)
    d.add_argument("--transport")
    d.add_argument("--execution-surface")
    d.add_argument("--registered-worker",choices=("0","1","false","true"))
    d.add_argument("--fallback-hop")
    d.add_argument("--subsession-manifest",help="aggregate declared sub-sessions into this one stage gate")
    d.add_argument("--reviewer-attempt",
                   help="review-worker node only: the registered review attempt that produced the verdict; "
                        "verified against --jobs and downgraded to owner-inline when the row is absent "
                        "or is not worker_type=review")
    d.add_argument("--reviewer-subagent",
                   help="review-worker node only: transcript path of the native subagent that produced "
                        "the verdict; recorded with its sha256 and counted as independent review")
    ar=sub.add_parser("arbitrate"); ar.add_argument("--route",required=True)
    ar.add_argument("--group",required=True,help="realized auxiliary-bearing parallel group id")
    ar.add_argument("--evidence",required=True,help="owner merge record carrying auxiliary_findings_considered")
    ar.add_argument("--output")
    rv=sub.add_parser("revise",help="SD-154: record that a gate's evidence changed after its marker published")
    rv.add_argument("--route",required=True)
    rv.add_argument("--node",required=True)
    rv.add_argument("--evidence",required=True,help="the node's corrected gate evidence (file or directory)")
    rv.add_argument("--basis",required=True,choices=("review-findings","user-direction","owner-correction"))
    rv.add_argument("--answers",help="comma list of downstream review/test verdict attempt ids (basis=review-findings)")
    rv.add_argument("--direction",help="gate release id or readable memo path (basis=user-direction)")
    rv.add_argument("--reason",help="owner's stated reason (basis=owner-correction)")
    rv.add_argument("--jobs",help="canonical registry path; required to verify basis=review-findings")
    rv.add_argument("--author-attempt-id",help="the recording attempt; default AGENT_DISPATCH_ATTEMPT_ID")
    rv.add_argument("--recorded-by",default="owner")
    rv.add_argument("--output")
    cl=sub.add_parser("close"); cl.add_argument("--route",required=True,help="route file path or route id (rt-...); "
                                                 "an attempt id that is not its route's owner stops that attempt "
                                                 "alone and leaves the route open")
    cl.add_argument("--artifact-root",default=None,
                    help="for a route id: default AGENT_ARTIFACT_ROOT, else utilities/artifact-root.sh for the current directory")
    cl.add_argument("--commit",help="result commit; defaults to HEAD in the route cwd")
    cl.add_argument("--summary",help="one line naming what the route produced")
    cl.add_argument("--stop-resources", action="store_true",
                    help="also stop resource runs exactly linked to this route; default preserves them")
    cl.add_argument("--allow-unproven",action="store_true",
                     help="accepted for compatibility; close always records terminal_gate_proven=false with a "
                          "terminal-gate-unproven warning when the terminal node has not completed")
    st=sub.add_parser("status"); st.add_argument("--artifact-root",default=None,help="default: utilities/artifact-root.sh for cwd")
    st.add_argument("--open-only",action="store_true",help="list only routes with no recorded outcome")
    st.add_argument("target",nargs="?",default=None,
                    help="optional route id, route file or attempt id: report only that route and its attempts")
    st.add_argument("--route","--route-id","--attempt-id",dest="route",default=None,help="same as the positional target")
    st.add_argument("--jobs",type=Path,default=None)
    sg=sub.add_parser("stages",help="list a capability's (or every capability's) stage ids, in recipe order, for --graph")
    sg.add_argument("--capability",default=None,help="default: every capability")
    sg.add_argument("--json",action="store_true")
    a=p.parse_args()
    if a.command not in {"verify", "node", "status", "close", "finish", "stages"} and not (a.command == "complete" and a.check):
        dispatch_terminal_commit.require_current_cleanup("route-" + a.command)
    if a.command=="stages":
        registry=TOPO.load_registry()
        rows=[r for r in registry["recipes"] if r["capability"]!=ROUTE_FRAME_CAPABILITY
              and (a.capability is None or r["capability"]==a.capability)]
        if not rows:
            raise ValueError(f"unknown capability: {a.capability}")
        blocks=[stages_block(registry,recipe) for recipe in rows]
        if a.json:
            print(json.dumps(blocks,sort_keys=True))
        else:
            def line(node):
                choices=f" unit_choices={','.join(node['unit_choices'])}" if node["unit_choices"] else ""
                group=f" parallel_group={node['parallel_group']}" if node["parallel_group"] else ""
                gates=f" human_gate={','.join(node['human_gates'])}" if node["human_gates"] else ""
                terminal=" terminal=1" if node["terminal"] else ""
                approval=f" start_approval={node['start_approval']}" if node["start_approval"] else ""
                extra=f" optional_in={','.join(node['optional_inputs'])}" if node["optional_inputs"] else ""
                if node["after"]: extra+=f" after={','.join(node['after'])}"
                if node["before"]: extra+=f" before={','.join(node['before'])}"
                return (f"{node['id']} unit={node['unit']}{choices}{group}{gates}{terminal}"
                        f" part={node['part']} shareable={int(node['shareable'])} optional={int(node['optional'])}"
                        f"{approval} in={','.join(node['inputs'])}{extra} out={','.join(node['outputs'])}"
                        f" -- {node['summary']}")
            for block in blocks:
                print(f"capability={block['capability']} modes={','.join(block['modes'])} "
                      f"topology={block['topology_class']}")
                for node in block["nodes"]:
                    print("  "+line(node))
                for node in block["borrowable"]:
                    print("  borrow "+line(node))
        return 0
    if a.command=="compose":
        shape=a.shape or "framed"
        if a.start and (a.explain or a.prompt_file is None):
            raise ValueError("compose-start-requires-task: use --start --prompt-file <task>, without --explain")
        compose_task = a.prompt_file.read_text() if a.prompt_file else ""
        cwd_was_explicit = a.cwd is not None
        cwd=a.cwd or os.getcwd()
        promotion_cwd = cwd
        artifact_root=a.artifact_root or _compose_artifact_root(cwd)
        if a.campaign_key is None and a.parent_cycle is None and not a.unassigned:
            a.campaign_key=_session_campaign_key(artifact_root)
            if a.campaign_key:
                print(f"campaign_key_default={a.campaign_key} source=this-session-latest-route",file=sys.stderr)
            else:
                # A session's first compose joins this folder's one active stream; with several, the
                # refusal names them first.
                here=cwd_campaign_keys(artifact_root,cwd)
                if len(here)==1:
                    a.campaign_key=here[0]
                    print(f"campaign_key_default={a.campaign_key} source=this-folder-only-active-stream",file=sys.stderr)
        elif a.campaign_key is not None:
            a.campaign_key,given=compose_resolve_campaign_key(artifact_root,a.campaign_key)
            if given is not None:
                print(f"campaign_key_resolved={a.campaign_key} given={given}",file=sys.stderr)
        if a.slug is None:
            a.slug=compose_default_slug(compose_task,
                                        capability=a.capability or (None if shape=="framed" else COMPOSE_DEFAULT_CAPABILITY),
                                        shape=shape)
        route_plan_binding = None
        if a.route_plan:
            # An explicit approved plan must never become an unrelated default route.
            import route_plan as RP
            try:
                route_plan_binding = RP.read_route_plan(a.route_plan, artifact_root)
            except (OSError, ValueError) as exc:
                raise ValueError(f"compose-route-plan-refused: {exc}") from exc
        a._route_chain_plan = _resolve_compose_plan(a, route_plan_binding)
        DISPATCH_DEFAULTS_WARNINGS.clear()
        pins=_parse_selection_pins(a.pin,a.owner)
        if route_plan_binding is not None:
            if shape == "framed":
                leg = route_plan_binding["leg"]
                shape = leg["shape"]
                a.capability = a.capability or leg["capability"]
                a.capability_mode = a.capability_mode or leg.get("mode")
                a.graph = a.graph or (",".join(leg["graph"]) if leg.get("graph") else None)
                a.intensity = a.intensity or leg.get("intensity")
            # A continuation leg keeps the pins its frame was composed with; a pin given here replaces only its own target.
            pins={**_inherited_selection_pins(route_plan_binding,artifact_root),**pins}
            if a.cwd is None:
                context=route_plan_binding["record"]["decision"]["first_leg_compose"]["context"]
                cwd=RP.leg_cwd(route_plan_binding["leg"],cwd,base_cwd=context["cwd"])
        non_git_decision = None
        capability_source = None
        capability_unresolved = False
        if a.start and not a.explain:
            diagnosis = {**_compose_git_diagnosis(cwd), "cwd": str(Path(cwd).resolve())}
            try:
                dispatch_depth = int(os.environ.get("AGENT_DISPATCH_DEPTH") or 0)
            except (TypeError, ValueError):
                dispatch_depth = -1
            if dispatch_depth < 0:
                diagnosis = {"state": "error", "cwd": str(Path(cwd).resolve()),
                             "detail": "invalid AGENT_DISPATCH_DEPTH"}
            requested_shape = (route_plan_binding["leg"].get("shape")
                               if route_plan_binding is not None else a.shape)
            import route_plan as RP
            non_git_decision = _decide_non_git_compose(
                diagnosis, requested_shape, dispatch_depth,
                bool(os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
                     or os.environ.get("AGENT_DISPATCH_REGISTERED_WORKER", "").lower() in {"1", "true"}),
                a.capability, new_repository=bool(RP.requested_repository({"cwd": str(cwd),
                    "work_request": {"text": compose_task}})))
            if non_git_decision["kind"] == "error":
                raise ValueError("compose-start-git-diagnosis: " + str(non_git_decision.get("detail")))
            if non_git_decision["kind"] == "refuse":
                raise ValueError(f"{non_git_decision['cause']}; {non_git_decision['next']}")
            if non_git_decision["kind"] == "direct":
                shape = "direct"
                capability = a.capability
                capability_mode = a.capability_mode
                original_mode_hint = a.capability_mode
                if a.capability is not None:
                    capability_source = "explicit"
                else:
                    classified = _classify_non_git_task(compose_task)
                    if classified is None:
                        capability = COMPOSE_DEFAULT_CAPABILITY
                        capability_mode = None
                        capability_source = "non-git-unresolved"
                        capability_unresolved = True
                    else:
                        capability, inferred_mode = classified
                        capability_mode = a.capability_mode or inferred_mode
                        capability_source = "non-git-task-classification"
                a.capability = capability if a.capability is None else a.capability
                a.capability_mode = capability_mode
                # Direct is an execution choice, not permission to discard the
                # caller's graph, profiles, pins or promotion/dispatch inputs.
                # The ordinary compiler keeps their existing validity rules.
                a.intensity = a.intensity or "direct"
                a._non_git_mode_hint = (original_mode_hint if capability_unresolved else None)
                non_git_decision["capability_unresolved"] = capability_unresolved
        pins,pin_warnings=_filter_top_pins(pins)
        owner_pin=(pins.get("owner") or {}).get("harness")
        compose_args=dict(
            capability=a.capability,
            capability_mode=a.capability_mode,shape=shape,graph=a.graph,
            slug=a.slug,cwd=cwd,artifact_root=artifact_root,intensity=a.intensity,signals=a.signal,
            campaign_key=a.campaign_key,parent_cycle_id=a.parent_cycle,unassigned=a.unassigned,
            spec_read=a.spec_read,drift_verdict=a.drift_verdict,tracking=a.tracking,
            artifact_guard=a.artifact_guard,
            children=[c.strip() for c in a.children.split(",") if c.strip()] if a.children else None,
            parent_harness=owner_pin or a.parent_harness or ("claude" if shape=="direct" else default_parent_harness("claude")),
            dispatch_evidence=json.loads(Path(a.dispatch_evidence).read_text()) if a.dispatch_evidence else None,
            registered_headless_evidence=(json.loads(Path(a.registered_headless_evidence).read_text())
                                          if a.registered_headless_evidence else None),
            transport_evidence=a.transport_evidence,jobs=a.jobs,
            profile_demands=json.loads(Path(a.profile_demands).read_text()) if a.profile_demands else None,
            explicit_profiles=json.loads(Path(a.explicit_profiles).read_text()) if a.explicit_profiles else None,
            profile=a.profile,
            work_request={"text":compose_task,"owner_harness":owner_pin} if a.prompt_file else None,
            selection_pins=pins or None,route_plan=route_plan_binding,
            execution_scope=a.execution_scope,
            capability_source=capability_source,
        )
        if capability_unresolved and a._non_git_mode_hint:
            compose_args["work_request"]["routing_hints"] = {"capability_mode": a._non_git_mode_hint}
        worktree=None
        if a.start and not a.explain:
            selection = compose_route(**compose_args, selection_only=True)
            is_framed = selection.get("shape") == "framed"
            non_git_worker = bool(a.start and not a.explain
                                  and non_git_decision.get("kind") == "worker-candidate")
            if non_git_worker and _isolates_worktree(selection):
                raise ValueError(f"{non_git_decision['cause']}; source-mutating worker route; "
                                 f"{non_git_decision['next']}")
            if _isolates_worktree(selection) or is_framed:
                approved_leg_cwd = bool((route_plan_binding or {}).get("leg", {}).get("cwd"))
                prep_key = _compose_preparation_key(compose_args, route_plan_binding, a.prompt_file)
                prepare_kwargs = {
                    "cwd": cwd, "slug": a.slug, "artifact_root": artifact_root,
                    "preparation_key": prep_key,
                    "explicitly_approved": cwd_was_explicit or approved_leg_cwd,
                }
                if is_framed:
                    prepare_kwargs["framed_start"] = True
                if non_git_worker:
                    worktree = {"state": "skipped", "reason": "non-git-artifact-only"}
                else:
                    worktree = prepare_isolated_worktree(**prepare_kwargs)
                if not (is_framed and worktree.get("state") == "failed"):
                    print("worktree_prepared=" + worktree["state"] + "".join(
                        f" {key}={worktree[key]}" for key in ("path", "branch", "base", "reason")
                        if worktree.get(key)), file=sys.stderr)
                if worktree.get("state") == "failed":
                    if is_framed:
                        detail = " ".join(str(worktree.get("detail") or "").split())
                        reason = " ".join(str(worktree.get("reason") or "unknown").split())
                        print(f"framed_worktree_fallback={reason}" + (f" detail={detail}" if detail else ""),
                              file=sys.stderr)
                    else:
                        raise ValueError("source-worktree-preparation-failed:" + worktree.get("reason", "unknown")
                                         + (":" + worktree["detail"] if worktree.get("detail") else "")
                                         + "; retry the same compose --start request; its identity is retained")
                if worktree.get("cwd") and os.path.realpath(worktree["cwd"]) != os.path.realpath(cwd):
                    final_cwd = worktree["cwd"]
                    compose_args["cwd"] = final_cwd
                    if (compose_args.get("dispatch_evidence") is not None
                            and not _dispatch_evidence_matches_cwd(compose_args["dispatch_evidence"], final_cwd)):
                        compose_args["dispatch_evidence"] = None
                    # Candidate evidence has no checked-worktree binding. Recollect it after a move.
                    compose_args["registered_headless_evidence"] = None
                    DISPATCH_DEFAULTS_WARNINGS.clear()
        route=compose_route(**compose_args)
        if worktree is not None and selection.get("shape") == "framed":
            route = _seal_framed_worktree(route, worktree)
        for line in (*DISPATCH_DEFAULTS_WARNINGS,*pin_warnings):
            print(line,file=sys.stderr)
        _plan_for_card, _plan_source_for_card = a._route_chain_plan
        # Starting this route may create its campaign. Keep this invocation's
        # original choice instead of calling the newly-created stream a join.
        _campaign_for_card = compose_campaign_selection(route)
        if a.explain:
            import work_intent
            print(compose_card(route, _plan_for_card, _plan_source_for_card, owner_harness=owner_pin,
                               campaign_selection=_campaign_for_card,
                               intent_original=(None if route.get("route_plan") else
                                                work_intent.original_excerpt(route.get("cwd")) or None)),
                  file=sys.stderr)
            print("route_file_written=0 explain=1",file=sys.stderr)
            print(json.dumps({"route_id":route["route_id"],"capability":route["capability"],
                              "effective_intensity":route["effective_intensity"],"shape":shape,
                              "composed":bool(route.get("composed")),
                              "nodes":[{"id":n["id"],"unit":n.get("unit"),"dispatch_depth":n.get("dispatch_depth"),
                                        "completion_gate":n.get("completion_gate"),"terminal":n.get("terminal") is True}
                                       for n in route["nodes"]],
                              "human_gates":route.get("human_gates"),"parallel_groups":route.get("parallel_groups"),
                              "campaign":_campaign_for_card,
                              **({"route_plan":route["route_plan"]} if route.get("route_plan") else {}),
                              "advisories":OWNER_WRITE_ADVISORY.advisories(route, owner_harness=owner_pin),
                              "tracked_gate_evidence":route.get("tracked_gate_evidence"),
                              **compose_observations(route)},sort_keys=True))
            return 0
        path = _emit_compiled_route(a,route,artifact_root)
        promotion = None
        try:
            from turn_change_record import link_promotion
            promotion = link_promotion(route, source_cwd=promotion_cwd)
            if promotion:
                print(f"prior_turn_evidence={promotion}", file=sys.stderr)
        except Exception:
            pass  # supplementary evidence never owns starting the workflow
        if a.start:
            from work_start import start_work
            jobs=Path(a.jobs or _compose_default_jobs())
            access_change=_record_access_change(route,jobs)
            print("\n".join(compose_decision_lines(route, non_git_decision)), file=sys.stderr, flush=True)
            started=start_work(route,path,jobs)
            if access_change is not None:
                started={**started,"access_change":access_change}
            if worktree is not None and worktree.get("cwd"):
                started={**started,"worktree":worktree}
            if promotion:
                started['prior_turn_evidence'] = promotion
                started['prior_worktree'] = str(Path(promotion_cwd).resolve())
                started['prior_turn_note'] = (
                    'Dirty edits remain in the original worktree. Read the evidence for original/prepared paths; '
                    'reapply only task-scoped changes when a new worktree was prepared, preserving user data.')
            started={**started, **compose_observations(route)}
            print(json.dumps(started,ensure_ascii=False),flush=True)
        # Bookkeeping runs after the work has started: the start does not depend on it (the sweep
        # never closes this route, and a cycle it seals is never the one this route begins or continues).
        _route_autoclose(artifact_root,"compose",route)
        print(compose_card(route, _plan_for_card, _plan_source_for_card, owner_harness=owner_pin,
                           campaign_selection=_campaign_for_card, include_decisions=not a.start,
                           non_git_decision=non_git_decision),file=sys.stderr)
        if a.start:
            from start_receipt import print_next_line
            print_next_line(started)
        return 0
    if a.command=="correct":
        from dispatch_owner_input import submit, inspect, InputError
        jobs=Path(a.jobs or _compose_default_jobs())
        given=a.attempt_id or a.correct_route
        if given is None:
            found,source,rows=caller_open_route(os.getcwd())
            if found is None:
                print(json.dumps({"state":"no-open-route" if source=="none" else "open-route-ambiguous",
                                  "cwd":os.getcwd(),"open_routes":rows,
                                  "next_step":"Name the owner: --attempt-id <attempt or route id>."},
                                 ensure_ascii=False))
                return 2
            print(f"route_default={found} source={source}",file=sys.stderr)
            given=found
        frame_path = resolve_route_argument(given, jobs)
        frame_raw = json.loads(frame_path.read_text()) if Path(frame_path).is_file() else None
        if ROUTE_PLAN.is_framed_route(frame_raw):
            from work_start import _framed_cycle
            _first = {}
            try:
                _root, _record, _output = _framed_cycle(frame_raw)
                _decision_path = _output / ROUTE_PLAN.RECORD_RELATIVE
                _first = (ROUTE_PLAN.read_record(_decision_path).get("first_leg") or {}) if _decision_path.exists() else {}
            except ValueError:
                pass   # conditions can precede the frame's first start/cycle
            _leg_id = (_first.get("route") or {}).get("route_id")
            _leg_owner = route_owner_attempt(_leg_id, jobs) if _leg_id else None
            if _leg_owner:
                given = _leg_owner
            else:
                from dispatch_owner_input import frame_input
                result = frame_input(verify_route(frame_raw), a.message_file.read_text() if a.message_file else None,
                                     a.request_id, jobs=jobs)
                if result.get("redirect_owner"):
                    given = result["redirect_owner"]
                else:
                    print(json.dumps(result, ensure_ascii=False))
                    from start_receipt import print_next_line
                    print_next_line(result)
                    return 0
        a.attempt_id=_correct_attempt_argument(given,jobs)
        if a.attempt_id!=given:
            print(f"owner_attempt_default={a.attempt_id} route={given}",file=sys.stderr)
        try:
            attempt=_current_owner_attempt(jobs,a.attempt_id)
        except ValueError as exc:
            print(json.dumps({"state":"not-admitted","reason":str(exc),"retry_when":"The owner lineage can be read.",
                              "next_action":"same-command",
                              "next_step":"Retain the correction. Inspect the exact owner; do not restart it or treat a file edit as delivery."}))
            return 69
        try:
            result=(submit(jobs,attempt,a.message_file.read_text(),a.request_id)
                    if a.message_file else inspect(jobs,attempt))
        except InputError as exc:
            refusal={"state":"not-admitted","reason":str(exc),
                     "retry_when":getattr(exc,"retry_when",""),
                     "next_action":getattr(exc,"next_action","same-command"),
                     "next_step":"Retain the correction. Inspect the exact owner; do not restart it or treat a file edit as delivery."}
            print(json.dumps(refusal))
            from start_receipt import print_next_line
            print_next_line(refusal)
            return 69
        if attempt!=a.attempt_id:
            result["redirected_from"]=a.attempt_id
        from dispatch_owner_input import blocked_owner_answers
        if (a.message_file and not result.get("consumer_recovery")
                and (result.get("retained") or blocked_owner_answers(jobs,attempt))):
            # The owner had ended BLOCKED (also when it ended just after this answer was
            # queued): the answer continues the route now, in the same call, through the
            # one shared start (a replacement owner receives it).
            result=_continue_after_answer(jobs,attempt,result)
        print(json.dumps(result,ensure_ascii=False))
        from start_receipt import print_next_line
        print_next_line(result)
        return 0
    if a.command=="start":
        from work_start import start_work
        if a.route is None:
            found,source,rows=caller_open_route(os.getcwd())
            if found is None:
                print(json.dumps({"state":"no-open-route" if source=="none" else "open-route-ambiguous",
                                  "cwd":os.getcwd(),"open_routes":rows,
                                  "next_step":("Run the resume_command of the route to continue."
                                               if rows else "No open route belongs to this session or cwd.")},
                                 ensure_ascii=False))
                return 2
            a.route=Path(found)
            print(f"route_default={found} source={source}",file=sys.stderr)
        a.route=resolve_route_argument(a.route,a.jobs)
        route=verify_route(json.loads(a.route.read_text()))
        jobs=Path(a.jobs or _compose_default_jobs())
        pin_change=_change_pins(route,jobs,a.pin) if a.pin else None
        access_change=_record_access_change(route,jobs)
        _record_route_chain(route, str(Path(a.route).resolve()), "start")
        print("\n".join(compose_decision_lines(route)), file=sys.stderr, flush=True)
        result=start_work(route,a.route,jobs,wait=a.wait,interview=a.interview,answers=a.answers,decision=a.decision)
        if pin_change is not None:
            result={**result,"pin_change":pin_change}
        if access_change is not None:
            result={**result,"access_change":access_change}
        result={**result, **compose_observations(route)}
        print(json.dumps(result,ensure_ascii=False))
        from start_receipt import print_next_line
        print_next_line(result)
        return 0
    if a.command=="compile":
        spec_read=(compose_spec_read(a.cwd,a.artifact_root,None) if a.spec_read is None else
                   {"satisfied":a.spec_read.lower() not in ("0","false","no"),"source":a.spec_read})
        gate={"spec_read":spec_read,
              "drift_verdict":a.drift_verdict,"workflow_mode":a.workflow_mode or a.tracking,
              "artifact_guard":{"satisfied":a.artifact_guard.lower() not in ("0","false","no"),"source":a.artifact_guard}}
        dispatch_evidence=json.loads(Path(a.dispatch_evidence).read_text()) if a.dispatch_evidence else None
        registered_headless_evidence=(
            json.loads(Path(a.registered_headless_evidence).read_text())
            if a.registered_headless_evidence else None
        )
        if a.composed_recipe:
            composed_recipe=json.loads(Path(a.composed_recipe).read_text())
            if composed_recipe.get("capability") != a.capability:
                raise ValueError("composed recipe capability differs from --capability")
            route=compile_composed_route(
                composed_recipe,a.capability_mode,a.intensity,a.cwd,a.artifact_root,
                predicates=a.predicate,signals=a.signal,transport=a.transport,
                transport_evidence=a.transport_evidence,inline_reason=a.inline_reason,
                tracking=a.tracking,tracked_gate_evidence=gate,
                dispatch_evidence=dispatch_evidence,
                registered_headless_evidence=registered_headless_evidence,
                slug=a.slug, campaign_key=a.campaign_key, parent_cycle_id=a.parent_cycle,
                profile_demands=json.loads(Path(a.profile_demands).read_text()) if a.profile_demands else None,
                explicit_profiles=json.loads(Path(a.explicit_profiles).read_text()) if a.explicit_profiles else None,
            )
        else:
            route=compile_route(
                a.capability,a.capability_mode,a.intensity,a.cwd,a.artifact_root,
                a.predicate,a.signal,a.transport,a.transport_evidence,a.inline_reason,
                a.tracking,gate,dispatch_evidence,registered_headless_evidence,
                slug=a.slug, campaign_key=a.campaign_key, parent_cycle_id=a.parent_cycle,
                profile_demands=json.loads(Path(a.profile_demands).read_text()) if a.profile_demands else None,
                explicit_profiles=json.loads(Path(a.explicit_profiles).read_text()) if a.explicit_profiles else None,
            )
        _emit_compiled_route(a,route,a.artifact_root)
    elif a.command=="continuation":
        source_path=resolve_route_argument(a.source_route).resolve(strict=True)
        source=verify_route(json.loads(source_path.read_text(encoding="utf-8")))
        # Keep the source path local to this invocation.  Do not attach it to
        # the verified route payload: continuation lineage hashes cover the
        # sealed route object, and a filesystem locator is runtime context,
        # not route identity.  Adding it here made valid owner-closure proof
        # markers fail their downstream currentness check.
        artifact=Path(a.artifact_root or source["artifact_root"]).resolve(strict=False)
        if artifact != Path(source["artifact_root"]).resolve(strict=False):
            print(
                "route_file_written=0 predecessor_attempts=0 registered=0 "
                "started=0 child_spawned=0",file=sys.stderr,
            )
            raise ValueError("continuation-artifact-root-mismatch")
        partial_values=(
            a.partial_group_manifest,a.source_group_id,
            a.failed_source_attempt_id,a.gap_leg_id,
        )
        if any(partial_values) and not all(partial_values):
            raise ValueError("partial-continuation-input-incomplete")
        partial=None
        if a.partial_group_manifest:
            partial={
                "source_group_id":a.source_group_id,
                "source_batch_manifest":json.loads(
                    Path(a.partial_group_manifest).read_text(encoding="utf-8")
                ),
                "failed_source_attempt_id":a.failed_source_attempt_id,
                "gap_leg_id":a.gap_leg_id,
            }
        try:
            route=build_continuation_route(
                source,resume_from_node=a.resume_from_node,
                requested_boundary=a.requested_boundary,reason=a.reason,
                artifact_root=artifact,lineage_operation=a.lineage_operation,
                thread_id=a.thread_id,new_thread_id=a.new_thread_id,
                forked_from_id=a.forked_from_id,last_turn_id=a.last_turn_id,
                ephemeral=a.ephemeral,partial_group=partial,
                dispatch_evidence=(json.loads(Path(a.dispatch_evidence).read_text(encoding="utf-8"))
                                   if a.dispatch_evidence else None),
            )
        except ValueError:
            # Every other refusal on this command prints the zeroed receipt line
            # before raising; one raised from inside the builder must too, or a
            # reader cannot tell "nothing was launched" from "unknown".
            print(
                "route_file_written=0 predecessor_attempts=0 registered=0 "
                "started=0 child_spawned=0",file=sys.stderr,
            )
            raise
        if (
            route.get("requested_boundary_blocker")
            or route.get("first_runnable_blocker")
        ):
            print(json.dumps(route,sort_keys=True),file=sys.stderr)
            print(
                "route_file_written=0 predecessor_attempts=0 registered=0 "
                "started=0 child_spawned=0",file=sys.stderr,
            )
            raise ValueError("continuation-boundary-blocked")
        launch_tuple=route.get("launch_compatibility_tuple") or {}
        if not immutable_code_root_equivalent(
            (launch_tuple.get("registry_root") or {}).get("path",""),
            (launch_tuple.get("runtime_root") or {}).get("path",""),
        ):
            print(
                "route_file_written=0 predecessor_attempts=0 registered=0 "
                "started=0 child_spawned=0",file=sys.stderr,
            )
            print(runtime_root_hint(),file=sys.stderr)
            raise ValueError("launch-runtime-root-mismatch")
        output_path=Path(a.output) if a.output else canonical_route_path(
            artifact,route["route_id"]
        )
        try:
            publish_continuation_route(route,source,output_path)
        except ValueError as exc:
            if str(exc)=="continuation-source-evidence-drift":
                print(
                    "route_file_written=0 predecessor_attempts=0 registered=0 "
                    "started=0 child_spawned=0",file=sys.stderr,
                )
            raise
        # A continuation is an immutable route for ordinary callers.  For a
        # registered depth-1 owner, publish the forward candidate only after
        # that route has reached disk. The lifecycle adopts it only after an
        # exact child start; a failed proof never rewrites either input.
        try:
            from owner_route_binding import (
                OwnerRouteBindingError,
                publish_owner_route_advance_from_environment,
            )
            route_for_binding = dict(route)
            route_for_binding["route_file"] = str(output_path.resolve())
            advance = publish_owner_route_advance_from_environment(
                os.environ.get("AGENT_DISPATCH_JOBS", ""),
                source_route=source, target_route=route_for_binding,
                environ=os.environ,
            ) if os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") else None
        except OwnerRouteBindingError as exc:
            print(
                f"route_file_written=1 owner_route_advance_written=0 reason={exc}",
                file=sys.stderr,
            )
            raise ValueError(str(exc)) from exc
        if advance is not None:
            print("owner_route_advance_written=1", file=sys.stderr)
        # rule 6: a depth-1 owner records its continuation only once it actually advanced
        # the bound route (an unadvanced attempt leaves the predecessor route's own ●,
        # which is honest — nothing here claims progress the owner has not proven).
        if os.environ.get("AGENT_DISPATCH_DEPTH") != "1" or advance is not None:
            _record_route_chain(route, str(output_path.resolve()), "continuation")
        # Publication is a candidate. The existing producer begin path records
        # cycle admission when execution enters it; a refused batch never
        # transfers the predecessor's closing responsibility.
        print("cycle_binding_bound=0 cycle_binding_deferred=1 basis=execution-begin",file=sys.stderr)
        print(f"route_file={output_path.resolve()}",file=sys.stderr)
        print(json.dumps(route,sort_keys=True))
    elif a.command=="status":
        target=a.target or a.route
        if target:
            route_file=resolve_route_argument(target,a.jobs)
            if not route_file.is_file():
                raise ValueError(f"route-not-found: {target} (pass a route id, route file or attempt id)")
            try:
                sealed_root=json.loads(route_file.read_text(encoding="utf-8")).get("artifact_root")
            except (OSError,ValueError):
                sealed_root=None
            rows=route_status(a.artifact_root or sealed_root or _compose_artifact_root(os.getcwd()),
                              open_only=a.open_only,only=route_file)
            if not rows and not a.open_only:
                rows=route_status(route_file.parent,only=route_file)
            for row in rows:
                row["attempts"]=route_attempts(row["route_id"],a.jobs)
        else:
            rows=route_status(a.artifact_root or _compose_artifact_root(os.getcwd()), open_only=a.open_only)
        from parent_next_directive import resume_command
        for row in rows:
            if not row["closed"] and not row.get("read_only"):
                row["resume_command"]=resume_command(row["route_file"],agent_home=ROOT)
        print(json.dumps(rows,sort_keys=True,indent=2))
    elif a.command=="finish":
        a.route=Path(_close_route_argument(a))
        raw=json.loads(a.route.read_text(encoding="utf-8"))
        route=verify_route(raw, raw.get("cwd"))
        import inline_finish
        admission = inline_finish.admit(route, allow_legacy=True)
        if admission[0].state == "modern":
            try:
                receipt=inline_finish.finish(a,route,a.route,sys.modules[__name__], _admission=admission)
            except inline_finish.InlineFinishError as exc:
                if str(exc) != "finish-route-cycle-missing":
                    raise
                import artifact_producer
                if artifact_producer.classify_root(Path(route["artifact_root"]))["state"] != "inactive-with-legacy":
                    raise
                receipt=_legacy_inline_finish(a,route,a.route,sys.modules[__name__], _admission=admission)
        else:
            receipt=_legacy_inline_finish(a,route,a.route,sys.modules[__name__], _admission=admission)
        print(json.dumps(receipt,sort_keys=True),flush=True)
        # Last, on stderr: stdout keeps exactly the one receipt its readers parse.
        location=_result_location_line(route)
        if location:
            print(location,file=sys.stderr)
        # The finished branch's PR names its next chain step (review request, fix, publish)
        # from GitHub's current state; read-only, best effort, silent without a GitHub remote.
        try:
            import importlib.util as _ilu
            _spec=_ilu.spec_from_file_location("pr_chain", Path(__file__).with_name("pr-chain.py"))
            _chain=_ilu.module_from_spec(_spec); _spec.loader.exec_module(_chain)
            chain=("" if route.get("source_commit") in (None,"","unversioned")
                   else _chain.finish_line(route.get("cwd"),capability=route.get("capability")))
        except Exception:
            chain=""
        if chain:
            print(chain,file=sys.stderr)
    else:
        if a.command=="close":
            a.route=_close_route_argument(a)
            if getattr(a,"named_attempt",None):
                # An attempt that is not its route's owner (a stage worker, one frame leg) is
                # stopped alone; its route, owner and every other attempt keep running.
                import route_parent_close
                stopped=route_parent_close.stop_attempt(a.named_attempt)
                if stopped is not None:
                    print(json.dumps(route_parent_close.explain_stop(stopped,UI_TEXT.translator()),sort_keys=True))
                    return
        if a.command=="complete" and getattr(a, "inline", False) and not getattr(a, "route", None):
            try:
                from owner_route_binding import default_owner_route_file
                a.route = default_owner_route_file()
            except Exception as exc:
                raise ValueError(str(exc)) from exc
        if a.command=="complete" and not getattr(a, "inline", False):
            if not getattr(a, "route", None) or not getattr(a, "evidence", None):
                print("capability-route: complete requires --route and --evidence (omit only with --inline)", file=sys.stderr)
                raise SystemExit(2)
        if a.command == "revise":
            a.route = resolve_route_argument(a.route, a.jobs)
        route=verify_route(
            json.loads(Path(a.route).read_text()), getattr(a,"cwd",None),
            allow_stale_registry=a.command=="close",
        )
        if a.command=="verify":
            if a.launch_phase:
                compatible,mismatches=revalidate_launch_compatibility(route)
                if mismatches.get("tuple") == "absent-legacy":
                    print("registered=0 started=0 child_spawned=0",file=sys.stderr)
                    raise ValueError("launch-compatibility-tuple-required")
                if not compatible:
                    name=sorted(mismatches)[0]
                    mismatch=mismatches[name]
                    print(
                        "launch-runtime-root-mismatch "
                        f"phase={a.launch_phase} mismatch={name}:"
                        f"expected={canonical(mismatch.get('expected',mismatch)).decode()}:"
                        f"actual={canonical(mismatch.get('actual',mismatch)).decode()}"
                        " | " + runtime_root_hint(route),
                        file=sys.stderr,
                    )
                    print("registered=0 started=0 child_spawned=0",file=sys.stderr)
                    raise ValueError("launch-runtime-root-mismatch")
            print(f"route_id={route['route_id']}\nroute_hash={route['route_hash']}")
            if getattr(a,"cwd",None) and _COMMIT_SHA.fullmatch(str(route.get("source_commit") or "")):
                # Publish the shared lineage verdict for CLI consumers.
                verdict=source_lineage_verdict(route["cwd"],route["source_commit"])
                observed=(
                    route["source_commit"] if verdict.kind=="exact"
                    else verdict.commits[0] if verdict.commits else None
                )
                print("source_lineage="+json.dumps({
                    "kind":verdict.kind,"sealed":route["source_commit"],
                    "observed":observed,"distance":verdict.distance,
                    "branch":verdict.branch,"reason":verdict.reason,
                },sort_keys=True))
        elif a.command=="arbitrate":
            evidence=Path(a.evidence).resolve()
            if not evidence.is_file(): raise SystemExit("arbitration evidence missing")
            record=arbitrate_group(route,a.group,evidence)
            if a.output: atomic_write(a.output, record)
            print(json.dumps(record,sort_keys=True))
        elif a.command=="revise":
            evidence=Path(a.evidence).resolve()
            if not (evidence.is_file() or evidence.is_dir()):
                raise SystemExit("revision evidence missing")
            answers=tuple(x.strip() for x in (a.answers or "").split(",") if x.strip())
            author_attempt_id=a.author_attempt_id or os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
            if not author_attempt_id:
                raise ValueError("revise-requires-author-attempt-id")
            result=publish_revision_locked(
                route,a.node,evidence,basis=a.basis,answers=answers,
                direction=a.direction,reason=a.reason,
                author_attempt_id=author_attempt_id,recorded_by=a.recorded_by,
                jobs=Path(a.jobs) if a.jobs else None,
            )
            marker=result.get("marker") or result["input_revision"]
            if a.output: atomic_write(a.output, marker)
            print(json.dumps(marker,sort_keys=True))
            print(f"tombstoned={','.join(result['tombstoned']) or '-'}",file=sys.stderr)
            print(f"kept={','.join(result.get('kept') or []) or '-'}",file=sys.stderr)
        elif a.command=="close":
            # An unproven terminal gate is recorded and warned about below, never refused:
            # the refusal was bypassed almost every time it fired.
            import route_parent_close
            # Running frame legs are this route's work too: they stop before the record closes.
            legs=route_parent_close.stop_route_legs(route)
            if legs and legs["state"]!="cancelled":
                print(json.dumps(legs,sort_keys=True))
                return
            if legs:
                print("stopped_attempts="+",".join(legs["stopped_attempts"]),file=sys.stderr)
            outcome=route_parent_close.close(route, a.route, stop_resources=a.stop_resources,
                                            summary=a.summary, commit=a.commit)
            if outcome is None:
                outcome,created=close_route(route,a.route,a.commit,a.summary,allow_unproven=True)
            else:
                created=True
                if outcome.get("state")=="cancelled" and outcome.get("owner_attempt_id"):
                    # The close stopped this route's running owner; its worktree is left as it was.
                    print("stopped_owner="+outcome["owner_attempt_id"],file=sys.stderr)
                from work_start import pin_handoff_continuation
                outcome=pin_handoff_continuation(route, a.route, route_parent_close.jobs_path(),
                                                {**outcome,"summary":a.summary or outcome.get("summary")})
                print(json.dumps(outcome,sort_keys=True))
                return
            print(json.dumps(outcome,sort_keys=True))
            if not created: print("capability-route: route already closed",file=sys.stderr)
            if outcome.get("review_independence_degraded"):
                print(
                    "capability-route: completed-review-degraded "
                    f"route_id={outcome['route_id']} "
                    f"nodes={','.join(outcome['review_independence_degraded'])} "
                    "-- these gates were not independently reviewed",
                    file=sys.stderr,
                )
            if outcome.get("terminal_gate_proven") is False:
                reasons={node_id:row.get("reason") for node_id,row in
                         (outcome.get("terminal_gates") or {}).items() if not row.get("passed")}
                print(f"capability-route: terminal-gate-unproven route_id={outcome['route_id']} "
                      f"reasons={json.dumps(reasons,sort_keys=True)}",file=sys.stderr)
        else:
            node=next((x for x in route["nodes"] if x["id"]==a.node),None)
            if not node: raise SystemExit("unknown route node")
            if a.command=="node": print(json.dumps(node,sort_keys=True))
            else:
                if getattr(a, "inline", False):
                    if a.dispatch_depth is None:
                        try:
                            a.dispatch_depth = int(node.get("dispatch_depth"))
                        except (TypeError, ValueError):
                            pass
                    if a.transport is None:
                        a.transport = "headless"
                    if a.execution_surface is None:
                        a.execution_surface = "inline"
                    if a.registered_worker is None:
                        a.registered_worker = "0"
                    if a.fallback_hop is None:
                        a.fallback_hop = "inline"
                    if not a.attempt_id:
                        owner_attempt = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") or ""
                        if owner_attempt:
                            a.attempt_id = f"{owner_attempt}-{a.node}-inline"
                    if not getattr(a, "evidence", None) and getattr(a, "reason", None):
                        try:
                            import artifact_producer as _ap
                            import hashlib as _hl
                            artifact_root = Path(route.get("artifact_root") or "")
                            record = _ap.route_cycle_for(artifact_root, route)
                            if record is None:
                                raise ValueError("inline-evidence-no-bound-cycle: pass --evidence with a cycle-local file")
                            out_dir = _ap.cycle_dir(artifact_root, record["campaign_id"],
                                                    record["cycle_id"], record) / "artifacts"
                            out_dir.mkdir(parents=True, exist_ok=True)
                            reason_text = f"# inline {a.node}\n\n{str(a.reason).strip()}\n"
                            digest = _hl.sha1(str(a.reason).strip().encode("utf-8")).hexdigest()[:8]
                            synth_path = out_dir / f"{route.get('route_id')}.{a.node}.{digest}.inline.md"
                            if synth_path.is_file():
                                if synth_path.read_text(encoding="utf-8") != reason_text:
                                    raise ValueError("inline-evidence-path-collision: retry with --evidence")
                                a._synthesized_evidence = None
                            else:
                                synth_path.write_text(reason_text, encoding="utf-8")
                                a._synthesized_evidence = str(synth_path)
                            a.evidence = str(synth_path)
                        except (OSError, ValueError, KeyError) as exc:
                            if isinstance(exc, ValueError) and str(exc).startswith(("inline-evidence-",)):
                                raise
                            raise ValueError(f"inline-evidence-unwritable:{exc}") from exc
                    if getattr(a, "reason", None):
                        print(f"inline-reason={a.reason}", file=sys.stderr)
                if not getattr(a, "evidence", None):
                    raise SystemExit("completion evidence missing")
                evidence=Path(a.evidence).resolve()
                # A completion artifact is one file or one directory of them; the
                # envelope inspector accepts both and `evidence_digest` names both.
                if not (evidence.is_file() or evidence.is_dir()):
                    raise SystemExit("completion evidence missing")
                # C-25b: check before completion runs, so a colliding `--output`
                # neither overwrites the pre-existing file's bytes nor leaves a
                # completion marker written behind a refused copy.
                if a.output and Path(a.output).exists():
                    raise ValueError("completion-output-exists")
                if a.reviewer_attempt and a.reviewer_subagent:
                    raise ValueError("reviewer-claim-conflict")
                review_claim=None
                if a.reviewer_attempt:
                    review_claim={"kind":"registered-worker","attempt_id":a.reviewer_attempt}
                elif a.reviewer_subagent:
                    review_claim={"kind":"native-subagent","transcript":a.reviewer_subagent}
                raw_axes=(a.dispatch_depth,a.transport,a.execution_surface,a.registered_worker,a.fallback_hop)
                explicit_attempt_metadata=None
                if any(value is not None for value in raw_axes):
                    explicit_attempt_metadata={
                        "attempt_schema_version":2,
                        "dispatch_depth":a.dispatch_depth,
                        "transport":a.transport,
                        "execution_surface":a.execution_surface,
                        "registered_worker":a.registered_worker,
                        "fallback_hop":a.fallback_hop,
                    }
                # A registered worker completing its own node may omit `--jobs` and
                # `--attempt-id`: the environment already names both. A call that states
                # attempt axes (the inline/unregistered form) or completes a resource run
                # takes nothing implicitly; an explicit value always wins.
                jobs,attempt_id=a.jobs,a.attempt_id
                inherited_jobs=os.environ.get("AGENT_DISPATCH_JOBS") or None
                if explicit_attempt_metadata is None and node.get("kind")!="resource-runner":
                    if a.subsession_manifest:
                        jobs=jobs or inherited_jobs
                    else:
                        if attempt_id is None and (jobs or inherited_jobs):
                            attempt_id=_bound_env_attempt(route,node,jobs or inherited_jobs)
                        if attempt_id and not jobs:
                            jobs=inherited_jobs
                if a.check:
                    if a.output or review_claim or explicit_attempt_metadata or a.subsession_manifest:
                        raise ValueError("owner-closure-check-requires-exact-jobs-attempt-and-no-overrides")
                    if node.get("kind") != "review-worker":
                        # Owner closure answers only a blocking review. This
                        # read-only question has no answer for a resource node.
                        result = {"result": "not-applicable", "read_only": True,
                                  "route_id": route["route_id"], "node_id": a.node,
                                  "reason": "owner-closure-review-nodes-only"}
                        if node.get("kind") == "resource-runner":
                            result["next_action"] = (
                                "use the resource node's existing start path; a completed compute-hosts "
                                "candidate may be supplied with --compute-run <run-id>; a route-bound "
                                "exit-0 run whose receipt names only a leftover `running` row is adopted "
                                "and that row is settled; a finished run this route's owner launched "
                                "directly (route provenance, no resource row) is adopted with its "
                                "evidence recorded")
                        print(json.dumps(result, sort_keys=True))
                        return
                    if not jobs or not attempt_id:
                        raise ValueError("owner-closure-check-requires-exact-jobs-attempt-and-no-overrides")
                    proof = owner_closure_plan(route, node, evidence, jobs, attempt_id)
                    print(json.dumps({"result": "ready", "read_only": True, "route_id": route["route_id"],
                                      "node_id": a.node, "owner_closure_proof": proof}, sort_keys=True))
                    return
                if a.subsession_manifest:
                    if review_claim:
                        raise ValueError("reviewer-claim-unsupported-on-subsession-gate")
                    if not jobs or attempt_id or explicit_attempt_metadata is not None:
                        raise ValueError("subsession completion requires --jobs and forbids attempt axes")
                    route["_route_file"]=str(Path(a.route).resolve())
                    marker,row=complete_subsession_stage(
                        route,node,a.node,evidence,a.subsession_manifest,jobs,
                    )
                else:
                    try:
                        marker,row=complete_node(
                            route,node,a.node,evidence,
                            jobs=jobs,
                            attempt_id=attempt_id,
                            explicit_attempt_metadata=explicit_attempt_metadata,
                            review_claim=review_claim,
                        )
                    except Exception:
                        synth = getattr(a, "_synthesized_evidence", None)
                        if synth:
                            try:
                                Path(synth).unlink()
                            except OSError:
                                pass
                        raise
                if a.output: atomic_write(a.output, marker)
                print(json.dumps(marker,sort_keys=True))
                if row: print(json.dumps(row,sort_keys=True))
                if marker.get("review_independence") in ("degraded","owner-overridden"):
                    # Typed and on stderr, so an owner that closed its own review
                    # node cannot finish the stage without being told -- and so
                    # the §0.5 completion card has something to quote.
                    reason=(marker.get("reviewer_downgrade_reason")
                            or marker.get("review_gate_closure") or "-")
                    print(
                        "capability-route: completed-review-degraded "
                        f"route_id={route['route_id']} node={a.node} "
                        f"independence={marker['review_independence']} "
                        f"reviewer_kind={marker['reviewer_kind']} "
                        f"reason={reason}",
                        file=sys.stderr,
                    )

if __name__=="__main__":
    try: main()
    # OSError too: completion walks a worker-supplied tree, and a refusal there
    # must be a typed line, never a traceback (review S-a).
    except (ValueError,OSError,TOPO.TopologyError,DEFAULTS.DefaultsConfigError) as exc:
        print(f"capability-route: {exc}",file=sys.stderr)
        if getattr(exc,"next_action",None)=="same-command" and getattr(exc,"retry_when",""):
            # A typed wait, not a refusal: the same command is the way on.
            print(f"capability-route: next_action=same-command retry_when={exc.retry_when}",file=sys.stderr)
        raise SystemExit(64)
    except Exception as exc:
        # A producer refusal (e.g. cutover-inactive) is a typed line too, never a traceback.
        import artifact_producer
        if not isinstance(exc, artifact_producer.ProducerError): raise
        print(f"capability-route: {exc}",file=sys.stderr); raise SystemExit(64)
