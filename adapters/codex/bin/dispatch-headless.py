#!/usr/bin/env python3
"""Codex headless dispatch registration/launch wrapper."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path



ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utilities"))
from codex_permission_profile import (commit_profile_config, config_arguments,
                                      code_stage_profile_config,
                                      recordish_profile_config, named_profiles_available)
from worker_runtime_home import (prepare_worker_home, codex_worker_arguments,
                                 recorded_codex_worker_environment)
import gpu_execution_sandbox as GPU_SANDBOX
from dispatch_contract import (
    _atomic_registry_replace,
    workflow_completion_receipt,  # noqa: E402
    DispatchContractError,
    foreground_review_launch_identity,
    GROUP_REAP_PROOF,
    GOVERNOR_RESERVATION_ENV,
    REPLICA_RESERVATION_ROW_KEYS,
    STANDARD_PLUS_INTENSITIES,
    SUPERVISOR_LEASE_KIND,
    anchored_capacity_failure,
    annotate_attempt_row,
    adapter_launch_failure_outcome,
    bytecode_cache_env,
    git_optional_locks_env,
    launch_mismatch_annotation,
    attempt_launch_is_available,
    attempt_launch_state,
    cancel_governor_reservation,
    claim_attempt_row,
    close_attempt_row,
    completion_marker_gate,
    existing_attempt_launch_state,
    owner_frame_launch_gate,
    recover_preview_gate_after_refusal,
    ensure_terminal_claim_absent,
    PRELAUNCH_PROCESS_BLOCK_REASONS,
    ROUTE_STATE_REFUSAL_REASONS,
    route_state_next_action,
    codex_standard_owner_network_enabled,
    dispatch_state_root,
    dispatch_state_roots,
    resolve_dispatch_state_root,
    ensure_global_registry_writable,
    headless_attempt_policy,
    launch_orphan_watch,
    launch_reap_watch,
    seal_foreground_result,
    new_attempt_id,
    parse_registry_metadata,
    parent_attempt_binding_is_live,
    resolve_global_registry,
    resolve_agent_home as _resolve_agent_home,
    sealed_launch_home,
    parent_lookup_worktree,
    resolve_live_parent_attempt,
    resolve_model_governor_root,
    review_governed_lease_is_held,
    governor_refusal_fields,
    replica_batch_expectation,
    reserve_governor_token,
    runtime_ancestry_binding,
    spawn_claimed_attempt,
    supervisor_lease_path,
    validate_nested_eligibility,
    wait_governor_reservation_claim,
    source_lineage_row_fields,
    diff_attribution_lines,
)
from parent_next_directive import receipt_lines as parent_next_receipt_lines  # noqa: E402
import launch_receipt  # noqa: E402
import dispatch_wrapper_common as WRAPPER_COMMON  # noqa: E402
import dispatch_headless_main as DISPATCH_MAIN  # noqa: E402
import frame_repository
# Identical in every wrapper; the old names stay so callers and mock targets do not move.
_is_report_bundle_publish_stage = WRAPPER_COMMON.is_report_bundle_publish_stage
_route_node_leg_fields = WRAPPER_COMMON.route_node_leg_fields
_supervisor_route = WRAPPER_COMMON.supervisor_route
watch_early_death = WRAPPER_COMMON.watch_early_death
write_reset_cache = WRAPPER_COMMON.write_reset_cache
diff_attribution_prompt = WRAPPER_COMMON.diff_attribution_prompt
fail = WRAPPER_COMMON.fail
jobs_lock = WRAPPER_COMMON.jobs_lock
prepare_review_output_request = WRAPPER_COMMON.prepare_review_output_request
process_start_ticks = WRAPPER_COMMON.process_start_ticks
read_launch_fence_failure = WRAPPER_COMMON.read_launch_fence_failure
resolve_artifact_root = WRAPPER_COMMON.resolve_artifact_root
resolve_report_bundle_root = WRAPPER_COMMON.resolve_report_bundle_root
seed_launch_heartbeat = WRAPPER_COMMON.seed_launch_heartbeat
_registered_owner = WRAPPER_COMMON.registered_owner
completion_state_path = WRAPPER_COMMON.completion_state_path
annotate_job_row = WRAPPER_COMMON.annotate_job_row
from dispatch_summary import launch_summary_owner, summary_writable_dirs  # noqa: E402
from artifact_producer import (  # noqa: E402
    ProducerError,
    bind_owner_launch,
    prepare_review_output_binding,
    review_lease_acquire,
)
from dispatch_lifecycle import (  # noqa: E402
    acquire_foreground_review_admission,
    acquire_review_admission,
    begin_finite_watchdog,
    DETACHED,
    EXISTING_ATTEMPT_NOTES,
    FOREGROUND_SCOPED,
    LIFECYCLES,
    deterministic_post_exit_outcome,
    reconcile_launch_lifecycle,
    launch_review_watchdog,
    wait_foreground,
)
from dispatch_continuation_budget import positive_continuation_limit  # noqa: E402
from dispatch_mode_contract import (  # noqa: E402
    capability_mode_from_route_file,
    DispatchModeContractError,
    normalize_dispatch_modes,
    resolve_qa,
    validate_capability_mode,
    validate_route_mode_axes,
)
from owner_route_binding import (  # noqa: E402
    OwnerRouteBindingError,
    binding_from_environment,
    owner_binding_tuple_failure_fields,
    validate_runtime_requirements,
)
from worker_bootstrap import (
    ARTIFACT_PRODUCER_CYCLE_ENV, artifact_cycle_environment, artifact_context_prompt, released_task_prompt, assignment_prompt, contract_read_prompt, unit_bootstrap_prompt,
    supervised_owner_prompt, worker_ending_prompt,  # noqa: E402
    assigned_contract,
    profile_worker_type,
    render_worker_bootstrap,
    runtime_progress_prompt,
    resolve_worker_type,
)
from stage_session_runtime import (  # noqa: E402
    add_arguments as add_stage_session_arguments,
    bind as bind_stage_session,
    environment as stage_session_environment,
    metadata as stage_session_metadata,
    prompt_fragment as stage_session_prompt,
)
from model_profile import (  # noqa: E402
    TOP_PROFILE,
    ModelProfileError,
    require_top_route,
    resolve_runtime_profile,
    route_selection_pin,
    validate_registered_profile,
)
import commit_policy  # noqa: E402
import harness_state_roots as HARNESS_STATE  # noqa: E402
from model_config import (  # noqa: E402
    ModelConfigError, headless_model_refusal, inheritance_refusal, main_session_only_models,
    main_session_only_state, resolve_config, restricted_model,
)
from codex_dispatch_terminal import REVIEW_BLOCKING_NOTE, inspect_terminal_attempt  # noqa: E402
from foreground_terminal import settle_foreground_exit  # noqa: E402
from dispatch_completion_join import (  # noqa: E402
    JoinContractError,
    close_wrapper_pass,
    exact_attempt_row,
    materialize_after_terminal_close,
)
from codex_managed_dispatch import (  # noqa: E402
    MANAGED_PARENT_DELIVERY,
    ManagedDispatchError,
    probe_managed_codex_parent,
    registered_parent_delivery,
)
from codex_queue_dispatch import launch_codex_queue_completion_sidecar
import dispatch_parent_completion as parent_completion
import owner_write_advisory as OWNER_WRITE_ADVISORY
from execution_access import (  # noqa: E402
    AccessContext,
    ExecutionAccessError,
    adapter_default_roots,
    load_parent_effective_grant,
    publish_effective_grant,
    receipt_fragment as execution_access_receipt_fragment,
    request_path as execution_access_request_path,
)
import route_authority  # noqa: E402
from route_authority import (  # noqa: E402
    bind_access_request as bind_execution_access_request,
    pin_target,
)
# Verification rigor is derived from intensity via resolve_qa
# (dispatch_mode_contract.py, the single qa/intensity SoT — CONVENTIONS §1.1).
# `--qa` is no longer a user-facing axis; optional, derived from --intensity
# when omitted. The jobs.log `qa=` field is retained (derived value) for
# fleet-collector compatibility.
INTENSITY_LEVELS = {"direct", "quick", "standard", "strong", "thorough", "adversarial"}
# standard+ per OPERATIONS.md §5.10 — the SD-78 runtime-owned completion clause
# is scoped to this set for owner (conductor) launches only.
_STANDARD_PLUS_INTENSITY = STANDARD_PLUS_INTENSITIES

# SD-15 (OPERATIONS §5.10 ⑨): limit/auth/capacity deaths are classified by the one shared
# table (route_authority), the same at launch and in liveness for every harness.
from route_authority import DEATH_PATTERNS, scan_anchored_death, scan_death  # noqa: E402,F401


def parser() -> argparse.ArgumentParser:
    p = DISPATCH_MAIN.base_parser(__doc__)
    p.add_argument(
        "--owner-harness",
        default=os.environ.get("AGENT_DISPATCH_OWNER_HARNESS") or "codex",
    )
    DISPATCH_MAIN.add_launch_arguments(p)
    p.add_argument("--parent-harness", default=parent_completion.default_parent_harness("codex"))
    DISPATCH_MAIN.add_parent_runtime_arguments(p)
    p.add_argument(
        "--sandbox",
        choices=("read-only", "workspace-write", "danger-full-access"),
        default=os.environ.get("CODEX_DISPATCH_SANDBOX", "workspace-write"),
    )
    p.add_argument(
        "--approval",
        choices=("untrusted", "on-request", "never", "inherit"),
        default=os.environ.get("CODEX_DISPATCH_APPROVAL", "never"),
    )
    p.add_argument("--model-role", default=os.environ.get("CODEX_DISPATCH_MODEL_ROLE"))
    p.add_argument("--model-profile", default=os.environ.get("CODEX_DISPATCH_MODEL_PROFILE"))
    p.add_argument("--pair-model-profile")
    p.add_argument("--model", default=os.environ.get("CODEX_DISPATCH_MODEL"))
    p.add_argument("--reasoning", default=os.environ.get("CODEX_DISPATCH_REASONING"))
    p.add_argument(
        "--completion-delivery",
        choices=("auto", "supervised", "poll"),
        default=os.environ.get("CODEX_DISPATCH_COMPLETION_DELIVERY", "auto"),
        help="standard+ owner completion bridge; auto prefers App Server session resume",
    )
    p.add_argument(
        "--allow-unmanaged-parent-poll",
        action="store_true",
        help="operator-only low-level recovery override; dispatch-owner forbids it",
    )
    p.add_argument(
        "--inherit-model-settings",
        action="store_true",
        help="legacy input retained for typed rejection; registered headless Codex dispatch requires an explicit eligible role/model",
    )
    p.add_argument("--require-hook-trust", action="store_true")
    p.add_argument("--profile")
    p.add_argument(
        "--early-exit-watch",
        type=float,
        default=float(os.environ.get("CODEX_DISPATCH_EARLY_EXIT_WATCH", "8")),
        help="SD-15: seconds to watch a just-launched child for a limit/auth early death "
        "(0 disables). On detection the jobs.log row is closed done,note=dead-<reason>.",
    )
    p.add_argument("--launch-lifecycle", choices=LIFECYCLES, default=DETACHED)
    p.add_argument(
        "--max-continuations",
        type=positive_continuation_limit,
        help="explicit positive override for a supervised owner continuation budget",
    )
    p.add_argument(
        "--foreground-timeout",
        type=float,
        default=float(os.environ.get("CODEX_DISPATCH_FOREGROUND_TIMEOUT", "14400")),
        help="maximum child lifetime for foreground-scoped launch; non-positive clamps to the safe default (never waits indefinitely)",
    )
    add_stage_session_arguments(p)
    return p


def _bind_runtime_parent(args: argparse.Namespace) -> None:
    """Bind a dispatch-depth-1 Codex job to the actual calling runtime session.

    Callers historically supplied a synthetic ``--parent-session-id``. That
    overrides the parser's CODEX_THREAD_ID default and Fleet cannot repair the
    relationship from cwd when multiple interactive sessions share one repo.
    Dispatch-depth-2 workers keep their explicit conductor/owner envelope; the legacy
    force switch remains available when a checked fallback intentionally rebinds it.
    """

    route_authority.bind_runtime_parent(args, honor_force=True)


def resolve_parent_completion_delivery(args: argparse.Namespace) -> str:
    return WRAPPER_COMMON.resolve_parent_completion_delivery(
        args, probe=probe_managed_codex_parent,
    )


def bind_parent_completion_delivery(args: argparse.Namespace) -> None:
    return WRAPPER_COMMON.bind_parent_completion_delivery(
        args, probe=probe_managed_codex_parent,
    )


def validate_interactive_parent_launch(args: argparse.Namespace) -> None:
    parent_completion.validate_interactive_parent_launch(args)


def launch_parent_completion_sidecar(args: argparse.Namespace, jobs: Path) -> None:
    return WRAPPER_COMMON.launch_parent_completion_sidecar(
        args, jobs, launch=launch_codex_queue_completion_sidecar,
        annotate=annotate_attempt_row,
    )


# One copy, shared by the three wrappers (`route_authority`); the name stays for readers.
completion_gate_fail_fields = route_authority.completion_gate_fail_fields


terminal_receipt_fields = launch_receipt.terminal_fields  # shared launch receipt fields


def task_prompt(args: argparse.Namespace) -> tuple[str, str]:
    if args.prompt_file and args.prompt_text:
        raise ValueError("--prompt-file and --prompt-text are mutually exclusive")
    if args.prompt_file:
        path = Path(args.prompt_file)
        return path.read_text(encoding="utf-8"), str(path)
    if args.prompt_text:
        return args.prompt_text, "inline"
    return (
        "Run the requested portable harness work.\n"
        f"capability={args.capability}\ncapability_mode={args.capability_mode}\n"
        f"worker_mode={args.worker_mode or '-'}\nqa={args.qa}\n"
        f"intensity={args.intensity}\ndispatch_depth={args.dispatch_depth}\nparent={args.parent_slug or '-'}\n"
        f"worktree={args.worktree}\n",
        "generated",
    )


def qa_track(capability: str) -> str:
    if capability.startswith("code-") or capability == "autopilot-code":
        return "code"
    if capability in {"autopilot-research"} or capability.startswith("analyze-"):
        return "research"
    if capability in {"autopilot-draft", "autopilot-refine"} or capability.startswith("draft-"):
        return "doc"
    return "general"


def toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def role_map(role: str) -> dict[str, str]:
    result = subprocess.run(
        [str(ROOT / "adapters" / "codex" / "bin" / "model-map.sh"), role],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ValueError(detail or f"preflight role lookup failed for {role}")
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    return fields


ModelSelectionError = WRAPPER_COMMON.ModelSelectionError


def _model_policy() -> dict[str, str]:
    return WRAPPER_COMMON.model_policy("codex", error=ModelSelectionError)


def _main_session_only_model(model: str) -> bool:
    """A selected user copy that omits CFG_MAIN_SESSION_ONLY_MODELS -- every
    copy written before 2026-09-10 -- carries no restriction: failing closed
    there would have stopped every codex dispatch until the copy was edited,
    and the shipped default declares the key."""

    return WRAPPER_COMMON.main_session_only_model(model, _model_policy)


def _main_session_only_policy_state() -> str:
    """`declared` or `absent` (review R1 M3): a selected user copy without the
    key is unrestricted, and that fact must be visible on the receipt."""

    return WRAPPER_COMMON.main_session_only_policy_state(_model_policy, error=ModelSelectionError)


def _require_headless_model(model: str, source: str) -> None:
    return WRAPPER_COMMON.require_headless_model(
        model, source, policy=_model_policy, error=ModelSelectionError,
    )


def _model_config_state() -> tuple[str, str]:
    return WRAPPER_COMMON.model_config_state("codex")


def _select_model_settings(args: argparse.Namespace) -> dict[str, str]:
    return DISPATCH_MAIN.select_model_settings(
        _ADAPTER, args, harness="codex", label="Codex", knob="reasoning",
        role_settings=_role_model_settings,
    )


def _role_model_settings(args: argparse.Namespace) -> dict[str, str]:
    try:
        fields = role_map(args.model_role)
    except ValueError as exc:
        raise ModelSelectionError(
            "invalid-dispatch-model-role",
            str(exc),
        ) from exc
    model = fields.get("exact_model_id")
    reasoning = fields.get("reasoning")
    if not model or not reasoning:
        raise ModelSelectionError("invalid-dispatch-model-role", "role map did not return model and reasoning")
    if model in {"role-set", "role-profile", "unconfigured"}:
        raise ModelSelectionError(
            "invalid-dispatch-model-role",
            f"model role {args.model_role!r} resolved to non-runnable model={model}",
        )
    _require_headless_model(model, f"role:{args.model_role}")
    # 역할 티어 고정 + 상황별 reasoning 오버라이드 (2026-07-22 사용자 원칙).
    if args.reasoning:
        if args.model_role.startswith("deep ") and args.reasoning in ("medium", "low"):
            # 사다리: deep 기본 xhigh → 아래는 high; medium 이하는 '정말 쉬운 것만'.
            print(
                f"caution=deep-tier-low-effort role={args.model_role!r} reasoning={args.reasoning} "
                "(step-down is high; medium/low is for genuinely easy work only)",
                file=sys.stderr,
            )
        return {
            "source": "role+effort", "role": args.model_role, "profile": "unsealed",
            "tier": "legacy", "granularity": "legacy", "model": model, "reasoning": args.reasoning,
        }
    return {
        "source": "role", "role": args.model_role, "profile": "unsealed",
        "tier": "legacy", "granularity": "legacy", "model": model, "reasoning": reasoning,
    }


def resolve_model_settings(args: argparse.Namespace) -> dict[str, str]:
    context = WRAPPER_COMMON.prepare_pair_model_rotation(args, harness="codex")
    selection_args = args
    if context.get("status") == "rotated":
        import copy
        selection_args = copy.copy(args)
        selection_args.model_profile = context["profile"]
    selected = _select_model_settings(selection_args)
    if context.get("status") == "rotated" and selected.get("source") == "profile":
        selected["source"] = "profile+diversity"
    return WRAPPER_COMMON.attach_context_window(selected, "codex", error=ModelSelectionError)


# Who may commit, decided once for every harness (commit_policy).
_worktree_mutating_write_scope = commit_policy.worktree_mutating_write_scope
_worktree_git_dirs = commit_policy.worktree_git_dirs
_is_linked_worktree = commit_policy.is_linked_worktree
is_no_commit_stage = commit_policy.no_commit_stage
commit_grant_target = commit_policy.may_commit
linked_worktree_git_writable_dirs = commit_policy.commit_git_metadata_dirs


def owner_write_advisories(args):
    """Observe the applied owner sandbox/grant without changing launch inputs."""
    gpu_choice = getattr(args, "gpu_execution_selection", None)
    if getattr(args, "worker_type", None) != "owner" and gpu_choice is None:
        return []
    route_file = getattr(args, "route_file", None) or getattr(
        getattr(args, "owner_route_binding", None), "route_file", None)
    route = None
    if route_file:
        try:
            route = json.loads(Path(route_file).read_text())
        except (OSError, ValueError):
            pass  # Advisory failure never becomes a new launch gate.
    if not isinstance(route, dict):
        route = {"cwd": str(args.worktree), "nodes": [
            {"write_scope": (getattr(args, "write_scope", None) or "").split(";")}]}
    if getattr(args, "worker_type", None) != "owner":
        return GPU_SANDBOX.advisory(route, owner_harness="codex", selection=gpu_choice, applied=True)
    grant = getattr(args, "execution_access_grant", None)
    # build_grant preserves request.writable_roots, including roots absorbed
    # by existing defaults; it never adds adapter default roots to this field.
    return OWNER_WRITE_ADVISORY.advisories(
        route, owner_harness="codex", sandbox=effective_runtime_sandbox(args),
        git_writable_roots=linked_worktree_git_writable_dirs(args),
        explicit_writable_roots=getattr(grant, "writable_roots", ()),
        gpu_selection=getattr(args, "gpu_execution_selection", None))


def dispatch_prompt(
    args: argparse.Namespace,
    task_input: tuple[str, str] | None = None,
) -> tuple[str, str]:
    task, source = task_input or task_prompt(args)
    bootstrap = DISPATCH_MAIN.bind_worker_contract(_ADAPTER, args, profile_type=profile_worker_type(ROOT, args.profile))
    route_state = (
        "consume the assigned route only (wrapper-validated immutable record)"
        if args.route_file
        else "validated dispatch metadata"
    )
    heartbeat = runtime_progress_prompt(args)
    no_commit_clause = commit_policy.prompt_clause(args)
    completion_delivery = getattr(args, "resolved_completion_delivery", "poll-fallback")
    supervised = completion_delivery == "app-server-supervised"
    owner_standard_plus = (
        args.intensity in _STANDARD_PLUS_INTENSITY and args.worker_type == "owner"
    )
    sync_wait_clause = ""
    if owner_standard_plus and supervised:
        sync_wait_clause = supervised_owner_prompt()
    elif owner_standard_plus:
        sync_wait_clause = (
            "Checked polling fallback (App Server completion bridge unavailable): immediately "
            "after a child is registered, run only utilities/dispatch-wait.sh --attempt-id "
            "<exact-id> --max 600 until terminal, then use exact-attempt preflight harvest. "
            "Do not inspect child transcripts/logs, source, artifacts, git state, or perform "
            "parallel work while a registered child remains open. This fallback is not runtime "
            "completion parity (OPERATIONS.md §5.10).\n\n"
        )
    ending = worker_ending_prompt(
        supervised_owner=supervised and args.worker_type == "owner",
    )
    return (
        f"{sync_wait_clause}"
        f"{bootstrap}\n"
        f"{DISPATCH_MAIN.dispatch_metadata_prompt(_ADAPTER, args)}"
        f"- route_state: {route_state}\n\n"
        "Codex realization:\n"
        f"{contract_read_prompt(args, 'codex')}"
        "- An owner has no worker mode and must not load any unit persona path.\n"
        f"- Run $AGENT_HOME/adapters/codex/bin/preflight.sh qa-policy {args.qa} {qa_track(args.capability)} and keep its required assurance in the artifact.\n"
        "- The wrapper already validated capability mode, worker unit/mode, QA, artifact root, and any route record. Re-run worker-route only for a safety recheck.\n"
        "- Codex may still auto-discover project AGENTS.md; do not explicitly load the full harness adapter bootstrap or another runtime's adapter.\n\n"
        f"{DISPATCH_MAIN.assignment_prompt_tail(_ADAPTER, args, task, heartbeat=heartbeat, commit_clause=no_commit_clause)}"
        f"{ending}",
        source,
    )


def apply_gpu_execution_sandbox(args: argparse.Namespace) -> None:
    """Consume the validated route's selection before grants or registration."""
    args.gpu_execution_selection = None
    args.gpu_execution_scope = False
    owner = args.dispatch_depth == 1 and args.worker_type == "owner"
    if not owner and args.dispatch_depth != 2:
        return
    route_file = getattr(args, "route_file", None) or getattr(
        getattr(args, "owner_route_binding", None), "route_file", None)
    if not route_file:
        return
    route = route_authority.route_in_force(json.loads(Path(route_file).read_text(encoding="utf-8")))
    cli_explicit = any(arg == "--sandbox" or arg.startswith("--sandbox=")
                       for arg in getattr(args, "replacement_input_argv", []))
    selection = GPU_SANDBOX.select(
        route, owner=owner, node=getattr(args, "route_node", None),
        requested=args.sandbox if cli_explicit else None)
    if not selection["gpu_scope"]:
        return
    if owner:
        rows = [row for row in (route.get("dispatch_evidence") or {}).get("tuples", [])
                if row.get("parent_harness") == "codex"]
        # Pin history may retain other sandbox variants. Admission needs a
        # supported tuple for this actual owner, not agreement of every row.
        if rows and not any(
                row.get("parent_sandbox") == selection["sandbox"]
                and row.get("parent_transport") == "headless"
                and row.get("launch_authority") == "conductor"
                and row.get("status") == "supported"
                and row.get("checked_worktree")
                and Path(row["checked_worktree"]).resolve() == Path(args.worktree).resolve()
                for row in rows):
            raise DispatchContractError("dispatch-evidence-parent-runtime-mismatch",
                                        "selected GPU owner sandbox differs from its checked tuple")
    args.gpu_execution_selection = selection
    args.gpu_execution_scope = bool(selection["gpu_scope"])
    args.sandbox = selection["sandbox"]


def uses_enclosing_codex_sandbox(args: argparse.Namespace) -> bool:
    """The checked foreground child remains inside the parent's OS sandbox."""
    return (
        not getattr(args, "gpu_execution_scope", False)
        and args.sandbox == "workspace-write"
        and getattr(args, "launch_lifecycle", DETACHED) == FOREGROUND_SCOPED
        and os.environ.get("AGENT_DISPATCH_CHILD") == "1"
        and getattr(args, "dispatch_depth", 1) >= 2
        and getattr(args, "parent_harness", None) == "codex"
        and getattr(args, "parent_transport", None) == "headless"
        and getattr(args, "parent_sandbox", None) == "workspace-write"
    )


def effective_runtime_sandbox(args: argparse.Namespace) -> str:
    """Avoid nested mounts while preserving caller sandbox constraints."""
    if commit_policy.launch_permission_projection(args)["observation_only"]:
        return "read-only"
    return base_runtime_sandbox(args)


def base_runtime_sandbox(args: argparse.Namespace) -> str:
    """Return the route/parent sandbox before the temporary Git-state overlay."""
    return "danger-full-access" if uses_enclosing_codex_sandbox(args) else args.sandbox


def invalid_codex_mount_target(args: argparse.Namespace, worktree: Path) -> Path | None:
    """Return an invalid `.codex` destination when Codex will mount a sandbox."""

    target = worktree / ".codex"
    if effective_runtime_sandbox(args) == "danger-full-access":
        return None
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        return target
    return None


# Which harness state this launch writes, decided once for every harness (harness_state_roots);
# this adapter opens it in its OS sandbox.
spec_read_marker_required = HARNESS_STATE.spec_read_marker_required
_spec_grounding_dir = HARNESS_STATE.spec_grounding_dir
_core_grounding_dir = HARNESS_STATE.core_grounding_dir
route_bound_worker_writable_dirs = HARNESS_STATE.route_bound_worker_writable_dirs
nested_opencode_store_writable_dirs = HARNESS_STATE.nested_opencode_store_writable_dirs
progress_writable_dirs = HARNESS_STATE.progress_writable_dirs
registry_writable_launch = HARNESS_STATE.registry_writable_launch


def nested_owner_writable_dirs(args: argparse.Namespace) -> tuple[Path, ...]:
    """Expose only the runtime scratch roots an owner-network-widened Codex owner
    may need downstream. A pure query -- see `ensure_owner_writable_dirs` for the
    one place these directories are created before launch."""

    network_eligible = getattr(
        args, "nested_headless_network_eligible",
        getattr(args, "nested_headless_network", False),
    )
    if not network_eligible:
        return ()
    claude_config = Path(
        os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    ).expanduser()
    # SD-49: the owner must register every depth-2 attempt in the inherited
    # canonical registry. Expose that registry's exact directory so the owner
    # can take jobs.log.lock without widening access to the rest of agent home.
    canonical_registry_root = dispatch_state_root(args.jobs_path)
    # SD-72: dispatch-depth-2 launches inside the owner sandbox spawn a summary owner
    # that writes exact-attempt state under the fleet titles root; without
    # write access the pre-release fence closes every child as
    # `summary-owner-launch-failed`/`never-launched`.
    summary_dirs = summary_writable_dirs()
    opencode_store_dirs = nested_opencode_store_writable_dirs(args)
    candidates = (
        canonical_registry_root,
        claude_config / "session-env",
        *summary_dirs,
        *opencode_store_dirs,
    )
    result = []
    seen = set()
    opencode_roots = set(opencode_store_dirs)
    for path in candidates:
        if path in opencode_roots:
            # Keep the final app directory lexical and recheck it at the
            # adapter boundary. Never turn a leaf symlink into a wider mount.
            path = path.parent.resolve(strict=False) / path.name
            if path.is_symlink():
                continue
        else:
            path = path.resolve()
        if path in summary_dirs or path in opencode_store_dirs or path.is_dir():
            if path not in seen:
                seen.add(path)
                result.append(path)
    return tuple(result)


def ensure_owner_writable_dirs(args: argparse.Namespace) -> None:
    """Create every directory this launch will grant sandbox write access to,
    once, before the child command is built. A query function (above) never
    has this side effect -- a create failure here is a typed launch failure,
    not a silently narrowed grant list."""

    to_create = list(progress_writable_dirs(args))
    to_create.extend(route_bound_worker_writable_dirs(args))
    if nested_owner_writable_dirs(args):
        to_create.extend(summary_writable_dirs())
        to_create.extend(nested_opencode_store_writable_dirs(args))
    if spec_read_marker_required(args):
        to_create.append(_spec_grounding_dir(args))
    if getattr(args, "route_id", None) or getattr(
        args, "nested_headless_network", False
    ):
        to_create.append(_core_grounding_dir(args))
    for path in to_create:
        try:
            Path(frame_repository.execution_path(path)).mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise DispatchContractError("owner-writable-root-uncreatable", f"{path}: {exc}")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def assert_register_mkdir_containment(args: argparse.Namespace, *paths: Path) -> None:
    """`--action register`/`start` must never mkdir outside the granted set
    (verification requirement (c) / plan-check round-1 Finding 2)."""

    granted = (
        (dispatch_state_root(args.jobs_path),)
        + nested_owner_writable_dirs(args)
        + route_bound_worker_writable_dirs(args)
    )
    for path in paths:
        resolved = path.resolve(strict=False)
        if not any(_is_within(resolved, root) for root in granted):
            raise DispatchContractError(
                "register-mkdir-outside-granted-root", str(resolved)
            )


def validate_nested_owner_registry_projection(args: argparse.Namespace) -> None:
    """Fail before model launch if the canonical registry is absent from the sandbox."""

    if not getattr(args, "nested_headless_network", False):
        return
    registry_root = dispatch_state_root(args.jobs_path)
    if registry_root not in nested_owner_writable_dirs(args):
        raise DispatchContractError(
            "owner-registry-sandbox-unwritable",
            f"canonical registry root is not projected writable: {registry_root}",
        )


def _completion_owner(args: argparse.Namespace) -> bool:
    return WRAPPER_COMMON.completion_owner(args, registered=_registered_owner)


def codex_app_server_available() -> bool:
    if shutil.which("codex") is None:
        return False
    try:
        result = subprocess.run(
            ["codex", "app-server", "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def resolve_completion_delivery(args: argparse.Namespace) -> str:
    """SD-OPEN-63 (3.1): Codex's `codex_app_server_available()` is already an
    exit-code feature probe, not a help-string substring match, so its
    judgment mechanism is unchanged. Only the reason is now sealed onto
    `args.completion_delivery_reason` so an auto degrade to `poll-fallback`
    carries the same preserved-evidence contract as the Claude adapter."""
    requested = args.completion_delivery
    if not _registered_owner(args):
        if requested == "supervised":
            raise DispatchContractError(
                "completion-delivery-ineligible",
                "supervised completion is scoped to registered dispatch-depth-1 owners",
            )
        if getattr(args, "worker_type", None) in {"stage", "review"}:
            available = codex_app_server_available()
            if not available:
                from verification_runtime import bound_lab_context
                if bound_lab_context(args) is not None:
                    raise DispatchContractError("loopback-only-unsupported", "local verification needs the checked App Server transport")
            args.resolved_stage_telemetry_transport = (
                "app-server-one-turn" if available else "raw-exec"
            )
        return "one-shot"
    standard_plus = _completion_owner(args)
    if requested == "poll":
        return "poll-fallback" if standard_plus else "one-shot"
    if codex_app_server_available():
        args.completion_delivery_reason = "ok"
        return "app-server-supervised"
    if requested == "supervised":
        raise DispatchContractError(
            "codex-app-server-unavailable",
            "codex app-server --help did not pass; no owner attempt was launched",
        )
    if not standard_plus:
        # A quick owner keeps the one-shot launch it had before.
        return "one-shot"
    args.completion_delivery_reason = "codex-app-server-unavailable"
    return "poll-fallback"


def completion_lease_path(args: argparse.Namespace) -> Path:
    if not args.attempt_id:
        return Path(args.jobs_path).resolve().parent / "supervisor-state" / "preview-only.lease"
    attempt_id = args.attempt_id
    return supervisor_lease_path(args.jobs_path, attempt_id)


def initialize_supervised_owner_input(args: argparse.Namespace, jobs: Path) -> None:
    WRAPPER_COMMON.initialize_owner_input_when(
        args, jobs, supervised=args.resolved_completion_delivery == "app-server-supervised", input_kind="codex-active-turn")


def _writer_shell_command(command, attempt_id, prompt_path: Path | None, log_path: Path) -> str:
    prompt_path = Path(frame_repository.native_path(prompt_path)) if prompt_path is not None else None
    log_path = Path(frame_repository.native_path(log_path))
    writer = [sys.executable, str(ROOT / "utilities" / "codex-jsonl-writer.py"),
              "--log", str(log_path), "--attempt", str(attempt_id or ""), "--"]
    rendered = shlex.join([*writer, *command])
    return rendered + (f" < {shlex.quote(str(prompt_path))}" if prompt_path is not None else "")


def shell_command(args: argparse.Namespace, prompt_path: Path, log_path: Path) -> str:
    prompt_path, log_path = Path(frame_repository.native_path(prompt_path)), Path(frame_repository.native_path(log_path))
    owner_supervised = (
        getattr(args, "resolved_completion_delivery", "one-shot")
        == "app-server-supervised"
    )
    stage_one_turn = (
        getattr(args, "resolved_stage_telemetry_transport", "raw-exec")
        == "app-server-one-turn"
    )
    if owner_supervised or stage_one_turn:
        command = [
            sys.executable,
            str(ROOT / "utilities" / "codex-app-server-supervisor.py"),
            "--worktree", args.worktree,
        ]
        if owner_supervised:
            command += [
                "--jobs", str(args.jobs_path),
                "--parent-attempt-id", args.attempt_id or "unassigned",
                "--state-file", str(completion_state_path(args)),
                "--lease-file", str(completion_lease_path(args)),
            ]
        else:
            command += ["--one-turn"]
        command += [
            "--sandbox", effective_runtime_sandbox(args),
            "--approval", args.approval,
            "--writable-root", str(args.artifact_root),
        ]
        if getattr(args, "runtime_dir", ""):
            command += ["--writable-root", str(args.runtime_dir)]
        if getattr(args, "report_bundle_root", None) is not None:
            command += ["--writable-root", str(args.report_bundle_root)]
        if owner_supervised:
            route = _supervisor_route(args)
            if route:
                command += ["--route-file", route[0], "--route-id", route[1], "--route-hash", route[2]]
            if getattr(args, "max_continuations", None) is not None:
                command += ["--max-continuations", str(args.max_continuations)]
        elif getattr(args, "route_file", None) and not (
            getattr(args, "repository_kind", None) == "non-git"
            and uses_enclosing_codex_sandbox(args)
        ):
            command += ["--route-file", args.route_file,
                        "--route-id", getattr(args, "route_id", "") or "",
                        "--route-hash", getattr(args, "route_hash", "") or ""]
        if commit_grant_target(args):
            # The supervisor builds the native profile; it only needs to know
            # whether this launch may commit (a primary checkout then gets the
            # commit-only `.git` grant).
            command += ["--primary-git-commit"]
        if registry_writable_launch(args):
            command += [
                "--writable-root",
                str(dispatch_state_root(args.jobs_path)),
            ]
        else:
            # Everyone else still has to be able to heartbeat. Grant the two
            # progress directories only -- the state root above stays reserved for
            # the launches that genuinely need the registry.
            for progress_dir in progress_writable_dirs(args):
                command += ["--writable-root", str(progress_dir)]
        for writable_dir in nested_owner_writable_dirs(args):
            command += ["--writable-root", str(writable_dir)]
        if spec_read_marker_required(args):
            command += ["--writable-root", str(_spec_grounding_dir(args))]
        for writable_dir in route_bound_worker_writable_dirs(args):
            command += ["--writable-root", str(writable_dir)]
        for writable_dir in linked_worktree_git_writable_dirs(args):
            # Commit-expected linked-worktree runs get exact Git metadata dirs.
            # No-commit stages receive none of these roots.
            command += ["--writable-root", str(writable_dir)]
        if getattr(args, "execution_access_grant", None) is not None:
            for writable_dir in args.execution_access_grant.additional_writable_roots:
                command += ["--writable-root", str(writable_dir)]
        if args.nested_headless_network:
            command += ["--network-access"]
        if args.resolved_model_settings["source"] != "inherit":
            command += [
                "--model", args.resolved_model_settings["model"],
                "--reasoning", args.resolved_model_settings["reasoning"],
            ]
        command += ["--model-context-window", args.resolved_model_settings["context_window"]]
        return _writer_shell_command(command, getattr(args, "command_attempt_id", ""),
                                     prompt_path, log_path)
    from verification_runtime import bound_lab_context
    if bound_lab_context(args) is not None:
        # A legacy exec config can override a named profile with sandbox_mode.
        # This requirement uses the negotiated App Server profile selector.
        raise DispatchContractError("loopback-only-unsupported", "local verification needs the checked App Server transport")
    cmd = [
        "codex",
        "exec",
        "--ephemeral",
        "--cd",
        frame_repository.native_worktree(args.worktree),
        "--add-dir",
        args.artifact_root,
    ]
    if getattr(args, "runtime_dir", ""):
        cmd += ["--add-dir", str(args.runtime_dir)]
    cmd += codex_worker_arguments(getattr(args, "worker_runtime_env", None))
    if getattr(args, "report_bundle_root", None) is not None:
        cmd += ["--add-dir", str(args.report_bundle_root)]
    if registry_writable_launch(args):
        # A dispatch-depth-1 conductor must update the canonical attempt registry and
        # materialize child prompt/transcript files under the canonical dispatch
        # state root. A route-bound worker (depth-1 quick owner or depth-2 stage)
        # needs the same root for its own `complete`/close (SD-OPEN-64). Network
        # remains owner-only below.
        cmd += [
            "--add-dir",
            str(dispatch_state_root(args.jobs_path)),
        ]
    else:
        # Same rule as the sandboxed builder above: every attempt has to be able
        # to record progress, so grant the two progress directories and nothing
        # more. This branch serves one-shot and poll-fallback delivery, which the
        # app-server builder never reaches.
        for progress_dir in progress_writable_dirs(args):
            cmd += ["--add-dir", str(progress_dir)]
    for writable_dir in nested_owner_writable_dirs(args):
        # Core read markers and Claude's Bash pre-exec snapshot are the only
        # home-scoped writes needed by a recursive standard+ Codex owner.
        cmd += ["--add-dir", frame_repository.native_path(writable_dir)]
    if spec_read_marker_required(args):
        # The same read obligation and exact grant as the supervisor builder.
        cmd += ["--add-dir", str(_spec_grounding_dir(args))]
    for writable_dir in route_bound_worker_writable_dirs(args):
        # SD-72: an ordinary route-bound depth-2 worker also runs the portable
        # core-read guard hook and must record its own .core-grounding marker,
        # independent of the owner-only nested_headless_network network grant.
        cmd += ["--add-dir", frame_repository.native_path(writable_dir)]
    for writable_dir in linked_worktree_git_writable_dirs(args):
        # Commit-expected linked-worktree workers get exact Git metadata dirs;
        # the common-dir root, hooks/, and config stay ungranted.
        cmd += ["--add-dir", frame_repository.native_path(writable_dir)]
    if getattr(args, "execution_access_grant", None) is not None:
        for writable_dir in args.execution_access_grant.additional_writable_roots:
            cmd += ["--add-dir", frame_repository.native_path(writable_dir)]
    write_roots = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--add-dir"]
    if getattr(args, "repository_kind", "git") == "non-git":
        if uses_enclosing_codex_sandbox(args):
            # The non-git parent already runs inside its checked recordish OS sandbox.
            # This child inherits that boundary, so it can reuse the git branch's sandbox mode.
            cmd += ["--skip-git-repo-check", "--sandbox", "danger-full-access"]
            profile = None
        else:
            profile = recordish_profile_config(
                args.worktree, write_roots,
                args.route_file or os.environ.get("AGENT_OWNER_ROUTE_FILE"),
                network=args.nested_headless_network,
            )
            if profile is None:
                raise RuntimeError("codex-non-git-recordish-profile-unsupported")
            cmd += ["--skip-git-repo-check", *config_arguments(frame_repository.runtime_paths(profile))]
    else:
        stage_route = None
        stage_node = getattr(args, "route_node", None)
        if getattr(args, "route_file", None) and stage_node:
            try:
                stage_route = json.loads(Path(args.route_file).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                stage_route = None
        profile, args.local_network = code_stage_profile_config(
            args.worktree, write_roots,
            effective_runtime_sandbox(args), args.nested_headless_network,
            primary_commit=commit_grant_target(args),
            route=stage_route if isinstance(stage_route, dict) else None,
            node_id=stage_node,
        )
        if profile is not None:
            cmd += config_arguments(frame_repository.runtime_paths(profile))
        else:
            cmd += ["--sandbox", effective_runtime_sandbox(args)]
    if args.nested_headless_network and profile is None:
        cmd += ["-c", "sandbox_workspace_write.network_access=true"]
    if args.resolved_model_settings["source"] != "inherit":
        model = args.resolved_model_settings["model"]
        reasoning = args.resolved_model_settings["reasoning"]
        cmd += [
            "--model",
            model,
            "-c",
            f"model_reasoning_effort={toml_string(reasoning)}",
        ]
    if args.approval != "inherit":
        cmd += [
            "-c",
            f"approval_policy={toml_string(args.approval)}",
        ]
    cmd += ["-c", f"model_context_window={args.resolved_model_settings['context_window']}"]
    cmd += [
        "--json",
        "-",
    ]
    return _writer_shell_command(cmd, getattr(args, "command_attempt_id", ""), prompt_path, log_path)


def _recorded_effective_grant(jobs: Path, attempt_id: str, metadata: dict[str, str],
                              route_id: str, route_hash: str) -> dict:
    import execution_access

    state_root = Path(jobs).resolve(strict=False).parent
    expected = state_root / "execution-access" / "attempts" / attempt_id / "effective.json"
    if (metadata.get("execution_access_effective_file") != str(expected)
            or not re.fullmatch(r"[0-9a-f]{64}", metadata.get("execution_access_effective_sha256", ""))):
        raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                    "execution_access_effective_file")
    try:
        raw = execution_access._read_bounded_regular_file(expected, execution_access.MAX_REQUEST_BYTES)
    except execution_access.ExecutionAccessError as exc:
        raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                    "execution_access_effective_file") from exc
    if hashlib.sha256(raw).hexdigest() != metadata["execution_access_effective_sha256"]:
        raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                    "execution_access_effective_sha256")
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=execution_access._pairs_no_duplicates)
    except (UnicodeDecodeError, ValueError, execution_access.ExecutionAccessError) as exc:
        raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                    "execution_access_effective_file") from exc
    if (not isinstance(record, dict) or record.get("schema_version") != 1
            or record.get("attempt_id") != attempt_id
            or record.get("route_id") != route_id or record.get("route_hash") != route_hash
            or record.get("runtime") != "codex-app-server"
            or record.get("sandbox") != metadata.get("runtime_sandbox")
            or type(record.get("network_allowed")) is not bool
            or not isinstance(record.get("writable_roots"), list)):
        raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                    "execution_access_effective_file")
    roots = record["writable_roots"]
    if any(not isinstance(root, str) or not Path(root).is_absolute()
           or str(Path(root).resolve(strict=False)) != root for root in roots):
        raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                    "execution_access_writable_roots")
    return record


def _recorded_argv_value(argv, option):
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        return None
    indexes = [index for index, item in enumerate(argv) if item == option]
    if len(indexes) != 1 or indexes[0] + 1 >= len(argv):
        return None
    value = argv[indexes[0] + 1]
    return None if value.startswith("--") else value


def _resolve_recorded_supervisor_values(record: dict, fields: list[str],
                                        metadata: dict[str, str], jobs: Path,
                                        effective: dict, *, recorded_home: str = "") -> dict:
    """Resolve the bound values needed to render a recorded owner launch.

    New records carry ``runtime_dir`` and ``commit_policy.may_commit``. For
    older records, recover the normal runtime root only when the verified grant
    and launch relationships leave one consistent candidate. Never consult the
    recovering caller's environment for either value.
    """
    from types import SimpleNamespace
    import commit_policy

    resolved = record.get("resolved")
    applied = record.get("applied_permissions")
    if not isinstance(resolved, dict) or not isinstance(applied, dict):
        raise DispatchContractError("consumer-respawn-launch-record-unavailable", "resolved")
    worktree = Path(fields[3])
    if (not worktree.is_absolute() or str(worktree.resolve(strict=False)) != record.get("worktree")
            or not worktree.is_dir()):
        raise DispatchContractError("consumer-respawn-launch-record-unavailable", "worktree")
    artifact_root = Path(metadata.get("artifact_root", ""))
    if not artifact_root.is_absolute() or not artifact_root.is_dir():
        raise DispatchContractError("consumer-respawn-launch-record-unavailable", "artifact_root")
    roots = {Path(root) for root in effective["writable_roots"]}
    if artifact_root not in roots:
        raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                    "execution_access_writable_roots:artifact_root")

    route_file = metadata.get("owner_route_file") or metadata.get("route_file") or ""
    route_id = metadata.get("owner_route_id") or metadata.get("route_id") or ""
    route_hash = metadata.get("owner_route_hash") or metadata.get("route_hash") or ""
    if (not Path(route_file).is_absolute() or not Path(route_file).is_file()
            or not route_id or not re.fullmatch(r"sha256:[0-9a-f]{64}", route_hash)):
        raise DispatchContractError("consumer-respawn-launch-record-unavailable", "route")
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DispatchContractError("consumer-respawn-launch-record-unavailable", "route") from exc
    if (route.get("route_id") != route_id or route.get("route_hash") != route_hash
            or str(Path(route.get("artifact_root", "")).resolve(strict=False))
               != str(artifact_root.resolve(strict=False))):
        raise DispatchContractError("consumer-respawn-launch-record-unavailable", "route")
    sealed_route_id = record.get("owner_route_id") or record.get("route_id")
    if sealed_route_id and sealed_route_id != route_id:
        raise DispatchContractError("consumer-respawn-launch-record-unavailable", "route_id")

    runtime_roots = roots - {artifact_root}
    if "runtime_dir" in resolved:
        runtime_value = resolved["runtime_dir"]
        if not isinstance(runtime_value, str) or not Path(runtime_value).is_absolute():
            raise DispatchContractError("consumer-respawn-launch-record-unavailable", "runtime_dir")
        runtime_dir = Path(runtime_value)
        if (not runtime_dir.is_dir() or runtime_dir.is_symlink()
                or str(runtime_dir.resolve(strict=False)) != runtime_value):
            raise DispatchContractError("consumer-respawn-launch-record-unavailable", "runtime_dir")
        if runtime_dir not in runtime_roots:
            raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                        "execution_access_writable_roots:runtime_dir")
    else:
        if Path(str(route.get("cwd") or "")).resolve(strict=False) != worktree:
            raise DispatchContractError("consumer-respawn-launch-record-unavailable", "runtime_dir")
        # The normal tracked-worktree choice is artifact-root's sibling
        # ``.agent_runtime``. Unversioned routes use the same generated
        # project-runtimes location under the sealed dispatch state namespace.
        normal_runtime = None
        worktree_git_dirs = commit_policy.worktree_git_dirs(worktree)
        if route.get("source_commit") == "unversioned" and not worktree_git_dirs:
            state_root = dispatch_state_root(jobs)
            if state_root.name == "dispatch":
                digest = hashlib.sha256(str(artifact_root.resolve(strict=False)).encode("utf-8")).hexdigest()
                normal_runtime = state_root.parent / "project-runtimes" / digest
        else:
            normal_runtime = artifact_root.resolve(strict=False).parent / ".agent_runtime"
        if normal_runtime is not None:
            normal_runtime = normal_runtime.resolve(strict=False)

        # Additional request roots were outside the launcher's default roots,
        # which already included its runtime. They remain writable but are not
        # evidence for a different runtime. Use only the same bound request.
        additional_roots = set()
        access = applied.get("execution_access")
        if isinstance(access, dict) and access.get("additional_writable_roots"):
            additional = access["additional_writable_roots"]
            requested = access.get("writable_roots")
            if (not isinstance(additional, list)
                    or any(not isinstance(root, str) for root in additional)
                    or not isinstance(requested, list)
                    or any(not isinstance(root, str) for root in requested)
                    or not access.get("source_path")
                    or access["source_path"] != effective.get("request_path")
                    or not re.fullmatch(r"[0-9a-f]{64}", str(access.get("request_sha256", "")))
                    or access["request_sha256"] != effective.get("request_sha256")):
                raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                            "execution_access_request")
            additional_roots = {Path(root) for root in additional}
            if (not additional_roots.issubset(roots)
                    or not additional_roots.issubset({Path(root) for root in requested})):
                raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                            "execution_access_request")

        git_roots = set(worktree_git_dirs or ())
        git_roots.update(commit_policy.commit_git_metadata_dirs(SimpleNamespace(
            worktree=str(worktree), worker_type="owner", dispatch_depth=1,
            route_file=route_file, owner_route_file=route_file,
        )))
        dispatch_root = dispatch_state_root(jobs).resolve(strict=False)
        recorded_home_path = Path(recorded_home).resolve(strict=False) if recorded_home else None
        candidates = set()
        if (normal_runtime is not None and normal_runtime in runtime_roots
                and normal_runtime not in additional_roots):
            candidates.add(normal_runtime)
        for candidate in runtime_roots - additional_roots:
            if candidate == normal_runtime:
                continue
            if (candidate == Path("/") or candidate in git_roots
                    or candidate == dispatch_root or candidate.is_relative_to(dispatch_root)
                    or dispatch_root.is_relative_to(candidate)
                    or candidate == worktree or candidate.is_relative_to(worktree)
                    or worktree.is_relative_to(candidate)
                    or candidate == artifact_root or candidate.is_relative_to(artifact_root)
                    or artifact_root.is_relative_to(candidate)
                    or (recorded_home_path is not None
                        and (candidate == recorded_home_path
                             or candidate.is_relative_to(recorded_home_path)
                             or recorded_home_path.is_relative_to(candidate)))
                    or "releases" in candidate.parts or ".harness" in candidate.parts):
                continue
            if (candidate.is_dir() and not candidate.is_symlink()
                    and str(candidate.resolve(strict=False)) == str(candidate)):
                candidates.add(candidate)

        home_value = metadata.get("runtime_home", "")
        home_matches = []
        if home_value and Path(home_value).is_absolute():
            home = Path(home_value).resolve(strict=False)
            home_matches = [candidate for candidate in candidates
                            if home == candidate or home.is_relative_to(candidate)]
        if len(home_matches) == 1:
            runtime_dir = home_matches[0]
        elif len(home_matches) > 1:
            raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                        "runtime_dir-ambiguous")
        elif len(candidates) == 1:
            runtime_dir = next(iter(candidates))
        elif not candidates:
            raise DispatchContractError("consumer-respawn-launch-record-unavailable", "runtime_dir")
        else:
            raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                        "runtime_dir-ambiguous")
        if (not runtime_dir.is_dir() or runtime_dir.is_symlink()
                or str(runtime_dir.resolve(strict=False)) != str(runtime_dir)
                or runtime_dir not in runtime_roots):
            raise DispatchContractError("consumer-respawn-launch-record-unavailable", "runtime_dir")

    projection_args = SimpleNamespace(worktree=str(worktree), worker_type="owner", dispatch_depth=1,
                                      route_file=route_file, owner_route_file=route_file)
    projected_may_commit = commit_policy.may_commit(projection_args)
    if "commit_policy" not in applied:
        may_commit = projected_may_commit
    else:
        commit_record = applied["commit_policy"]
        if not isinstance(commit_record, dict) or type(commit_record.get("may_commit")) is not bool:
            raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                        "commit_policy.may_commit")
        if projected_may_commit != commit_record["may_commit"]:
            raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                        "commit_policy.may_commit-changed")
        may_commit = commit_record["may_commit"]
    if may_commit:
        required_git_roots = set(commit_policy.commit_git_metadata_dirs(projection_args))
        if not required_git_roots.issubset(roots):
            raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                        "execution_access_writable_roots:commit_policy")

    return {"runtime_dir": runtime_dir, "may_commit": may_commit,
            "route_file": route_file, "route_id": route_id, "route_hash": route_hash,
            "route": route}


def recorded_supervisor_command(record: dict, fields: list[str], metadata: dict[str, str],
                                jobs: Path, state_path: Path, lease_path: Path,
                                prompt_path: Path | None, log_path: Path,
                                effective: dict, *, recorded_values: dict | None = None,
                                recorded_home: str = "") -> str:
    """Render the normal supervised-owner command from its sealed launch tuple."""

    resolved = record.get("resolved")
    applied = record.get("applied_permissions")
    if not isinstance(resolved, dict) or not isinstance(applied, dict):
        raise DispatchContractError("consumer-respawn-launch-record-unavailable", "resolved")
    worktree = Path(fields[3])
    if recorded_values is None:
        recorded_values = _resolve_recorded_supervisor_values(
            record, fields, metadata, jobs, effective, recorded_home=recorded_home)
    sandbox = metadata.get("runtime_sandbox", "")
    if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        raise DispatchContractError("consumer-respawn-launch-record-unavailable", "runtime_sandbox")
    approval = metadata.get("approval") or applied.get("approval")
    if approval is None:
        approval = _recorded_argv_value(record.get("argv"), "--approval")
    if approval not in {"untrusted", "on-request", "never", "inherit"}:
        raise DispatchContractError("consumer-respawn-launch-record-unavailable", "approval")

    settings = resolved.get("resolved_model_settings")
    if (not isinstance(settings, dict)
            or any(not isinstance(settings.get(key), str) or not settings.get(key)
                   for key in ("source", "model", "reasoning", "context_window"))):
        raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                    "resolved_model_settings")
    runtime_dir = recorded_values["runtime_dir"]
    if runtime_dir not in {Path(root) for root in effective["writable_roots"]}:
        raise DispatchContractError("consumer-respawn-launch-record-unavailable",
                                    "execution_access_writable_roots:runtime_dir")
    route_file = recorded_values["route_file"]
    route_id = recorded_values["route_id"]
    route_hash = recorded_values["route_hash"]

    command = [sys.executable, str(ROOT / "utilities" / "codex-app-server-supervisor.py"),
               "--worktree", str(worktree), "--jobs", str(jobs),
               "--parent-attempt-id", metadata["attempt_id"],
               "--state-file", str(state_path), "--lease-file", str(lease_path),
               "--sandbox", sandbox, "--approval", approval]
    for root in effective["writable_roots"]:
        command.extend(("--writable-root", root))
    if effective["network_allowed"]:
        command.append("--network-access")
    if route_file and route_id and route_hash:
        command.extend(("--route-file", route_file, "--route-id", route_id,
                        "--route-hash", route_hash))
    maximum = _recorded_argv_value(record.get("argv"), "--max-continuations")
    if maximum is not None:
        command.extend(("--max-continuations", maximum))
    if recorded_values["may_commit"]:
        command.append("--primary-git-commit")
    if settings["source"] != "inherit":
        command.extend(("--model", settings["model"], "--reasoning", settings["reasoning"]))
    command.extend(("--model-context-window", settings["context_window"]))
    return _writer_shell_command(command, metadata["attempt_id"], None, log_path)


def respawn_supervised_owner(jobs, attempt_id, *, expected_identity, check_only=False):
    from dispatch_completion_join import exact_attempt_row
    row = exact_attempt_row(Path(jobs), attempt_id)
    route_file = row.metadata.get("owner_route_file") or row.metadata.get("route_file")
    try:
        route = json.loads(Path(route_file).read_text()) if route_file else {}
    except (OSError, ValueError):
        route = {}  # the existing recovery validation supplies its typed reason
    try:
        with frame_repository.for_route(route, route_file):
            return _respawn_supervised_owner(jobs, attempt_id,
                expected_identity=expected_identity, check_only=check_only)
    except frame_repository.RepositoryBindingError as exc:
        return {"state": "not-started", "reason": exc.reason, "detail": str(exc)}


def _respawn_supervised_owner(jobs, attempt_id, *, expected_identity, check_only=False):
    """Restore a Codex consumer on the exact launched owner row, from its record.

    `check_only` runs the same record checks and stops before any reservation or
    spawn, so a live consumer is only moved when it can be restored."""
    from types import SimpleNamespace
    import dispatch_replacement
    import dispatch_completion_join as JOIN
    import route_authority
    from artifact_producer import ProducerError, prepare_route_artifact_env

    jobs = Path(jobs).resolve(strict=True)
    row = JOIN.exact_attempt_row(jobs, attempt_id)
    row_fields = row.raw.split("\t")
    if len(row_fields) != 6:
        return {"state": "not-started", "reason": "attempt-respawn-row-invalid"}
    metadata = dict(row.metadata)
    expected = (str(expected_identity[0]), str(expected_identity[1]))
    if ((metadata.get("pid", ""), metadata.get("pid_start", "")) != expected
            or row.status not in {"open", "running"}):
        return {"state": "not-started", "reason": "attempt-respawn-identity-changed"}
    try:
        launch = dispatch_replacement.launch_input(jobs, attempt_id, metadata)
    except DispatchContractError as exc:
        return {"state": "not-started",
                "reason": "consumer-respawn-launch-record-unavailable:replacement_input"}
    if (launch.get("harness") != "codex" or launch.get("attempt_id") != attempt_id
            or launch.get("jobs") != str(jobs)
            or Path(launch.get("worktree", "")).resolve(strict=False)
               != Path(row_fields[3]).resolve(strict=False)):
        return {"state": "not-started",
                "reason": "consumer-respawn-launch-record-unavailable:attempt_binding"}
    resolved = launch.get("resolved")
    if not isinstance(resolved, dict):
        return {"state": "not-started",
                "reason": "consumer-respawn-launch-record-unavailable:resolved"}

    launch_home = launch.get("launch_home")
    if (not isinstance(launch_home, str) or not Path(launch_home).is_absolute()
            or launch_home != metadata.get("launch_home")
            or not (Path(launch_home) / "core" / "CORE.md").is_file()):
        return {"state": "not-started",
                "reason": "consumer-respawn-launch-record-unavailable:launch_home"}
    runtime_home = metadata.get("runtime_home", "")
    if not runtime_home or not Path(runtime_home).is_absolute():
        return {"state": "not-started",
                "reason": "consumer-respawn-launch-record-unavailable:runtime_home"}
    try:
        worker_env = recorded_codex_worker_environment(launch_home, runtime_home, attempt_id)
    except (OSError, ValueError, TypeError) as exc:
        return {"state": "not-started",
                "reason": "consumer-respawn-launch-record-unavailable:worker_home_provenance"}

    lease_path = supervisor_lease_path(jobs, attempt_id)
    if metadata.get("supervisor_lease_file") != str(lease_path):
        return {"state": "not-started",
                "reason": "consumer-respawn-launch-record-unavailable:supervisor_lease_file"}
    state_path = lease_path.with_suffix(".json")
    log_value = metadata.get("log_file", "")
    if not log_value or not Path(log_value).is_absolute():
        return {"state": "not-started",
                "reason": "consumer-respawn-launch-record-unavailable:log_file"}
    log_path = Path(log_value)
    if not log_path.parent.is_dir() or not log_path.name.endswith(".jsonl"):
        return {"state": "not-started",
                "reason": "consumer-respawn-launch-record-unavailable:log_file"}
    summary_prompt = log_path.with_name(log_path.name[:-len(".jsonl")] + ".prompt.txt")
    if summary_prompt.is_symlink() or not summary_prompt.is_file():
        return {"state": "not-started",
                "reason": "consumer-respawn-launch-record-unavailable:summary_prompt"}

    route_file = metadata.get("owner_route_file") or metadata.get("route_file") or ""
    route_id = metadata.get("owner_route_id") or metadata.get("route_id") or ""
    route_hash = metadata.get("owner_route_hash") or metadata.get("route_hash") or ""
    effective = _recorded_effective_grant(jobs, attempt_id, metadata, route_id, route_hash)
    try:
        recorded_values = _resolve_recorded_supervisor_values(
            launch, row_fields, metadata, jobs, effective,
            recorded_home=worker_env.get("HOME", ""))
        shell = recorded_supervisor_command(
            launch, row_fields, metadata, jobs, state_path, lease_path,
            None, log_path, effective, recorded_values=recorded_values,
        )
    except DispatchContractError as exc:
        return {"state": "not-started", "reason": exc.reason + ":" + exc.detail}

    artifact_root = str(Path(metadata["artifact_root"]).resolve(strict=False))
    runtime_dir = str(recorded_values["runtime_dir"])
    try:
        artifact_env = prepare_route_artifact_env(
            Path(route_file), start=False, jobs=jobs, require_cycle=False)
    except (ProducerError, OSError, ValueError, TypeError) as exc:
        return {"state": "not-started",
                "reason": "consumer-respawn-launch-record-unavailable:artifact_cycle"}
    if artifact_env.get("AGENT_ARTIFACT_ROOT") != artifact_root:
        return {"state": "not-started",
                "reason": "consumer-respawn-launch-record-unavailable:artifact_root"}
    # Artifact-cycle selection has no runtime-root input. Bind its returned
    # environment to the one path resolved above before any recovery prompt or
    # child environment is constructed.
    artifact_env["AGENT_RUNTIME_DIR"] = runtime_dir

    parent_session = resolved.get("parent_session_id") or metadata.get("parent_sid", "")
    if metadata.get("parent_sid") and parent_session != metadata.get("parent_sid"):
        return {"state": "not-started",
                "reason": "consumer-respawn-launch-record-unavailable:parent_session_id"}
    owner_binding = SimpleNamespace(route_file=route_file, route_id=route_id, route_hash=route_hash)
    args = SimpleNamespace(
        attempt_id=attempt_id, command_attempt_id=attempt_id, jobs_path=jobs,
        worktree=row_fields[3], slug=row_fields[4], artifact_root=artifact_root,
        runtime_dir=runtime_dir, route_file=route_file, route_id=route_id,
        route_hash=route_hash, route_node=launch.get("route_node") or metadata.get("route_node", ""),
        owner_route_binding=owner_binding, worker_type="owner", dispatch_depth=1,
        capability=resolved.get("capability", ""), capability_mode=resolved.get("capability_mode", ""),
        worker_mode=resolved.get("worker_mode", ""), worker_role=metadata.get("worker_role", ""),
        assigned_contract=resolved.get("assigned_contract", ""), unit=resolved.get("unit", ""),
        capability_owner=metadata.get("owner", ""), owner_harness=metadata.get("owner_harness", ""),
        intensity=resolved.get("intensity", ""), approval=metadata.get("approval") or
            launch.get("applied_permissions", {}).get("approval", ""),
        launch_lifecycle=metadata.get("launch_lifecycle", ""),
        report_bundle_root=resolve_report_bundle_root(route_file,
            launch.get("route_node") or metadata.get("route_node")),
        worker_runtime_env=worker_env, review_output_binding=None, review_governed_lease_nonce="",
    )
    if not args.launch_lifecycle:
        return {"state": "not-started",
                "reason": "consumer-respawn-launch-record-unavailable:launch_lifecycle"}

    dispatch_env = dict(os.environ)
    dispatch_env.update(artifact_env)
    dispatch_env.update({
        "AGENT_SESSION_ROLE": "worker",
        "AGENT_DISPATCH_CHILD": "1",
        "AGENT_DISPATCH_DEPTH": "1",
        "AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION": "2",
        "AGENT_DISPATCH_TRANSPORT": "headless",
        "AGENT_DISPATCH_EXECUTION_SURFACE": metadata.get("execution_surface", "registered-headless"),
        "AGENT_DISPATCH_REGISTERED_WORKER": metadata.get("registered_worker", "1"),
        "AGENT_DISPATCH_FALLBACK_HOP": metadata.get("fallback_hop", ""),
        "AGENT_DISPATCH_INTENSITY": str(resolved.get("intensity") or ""),
        "AGENT_DISPATCH_CAPABILITY_MODE": str(resolved.get("capability_mode") or ""),
        "AGENT_DISPATCH_SELF_SLUG": row_fields[4],
        "AGENT_DISPATCH_PARENT_SLUG": metadata.get("parent", ""),
        "AGENT_DISPATCH_ATTEMPT_ID": attempt_id,
        "AGENT_DISPATCH_PARENT_ATTEMPT_ID": str(resolved.get("parent_attempt_id") or ""),
        "AGENT_DISPATCH_PARENT_SESSION_ID": str(parent_session or ""),
        "AGENT_DISPATCH_PARENT_CWD": metadata.get("parent_cwd", ""),
        "AGENT_DISPATCH_WORKER_TYPE": "owner",
        "AGENT_DISPATCH_ASSIGNED_CONTRACT": str(resolved.get("assigned_contract") or ""),
        "AGENT_DISPATCH_OWNER": str(metadata.get("owner") or ""),
        "AGENT_DISPATCH_OWNER_HARNESS": str(metadata.get("owner_harness") or ""),
        "AGENT_ARTIFACT_ROOT": artifact_root,
        "AGENT_DISPATCH_WORKTREE": row_fields[3],
        "AGENT_RUNTIME_DIR": runtime_dir,
        "REPORT_BUNDLE_ROOT": str(args.report_bundle_root or ""),
        "AGENT_ROUTE_FILE": route_file,
        "AGENT_ROUTE_ID": route_id,
        "AGENT_ROUTE_HASH": route_hash,
        "AGENT_ROUTE_NODE": str(args.route_node or ""),
        "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
        "AGENT_DISPATCH_CURRENT_SANDBOX": metadata["runtime_sandbox"],
        "AGENT_DISPATCH_COMPLETION_MODE": "supervised",
        "AGENT_DISPATCH_COMPLETION_STATE_FILE": str(state_path),
        "AGENT_DISPATCH_SUPERVISOR_LEASE_FILE": str(lease_path),
        "AGENT_DISPATCH_JOBS": str(jobs),
        # The child's utilities and hooks run from the release that governs it (ROOT, the
        # governor's and `respawn_release`); the recorded launch_home stays the source
        # of the worker home, so the same thread and its stores are kept.
        "AGENT_HOME": str(ROOT),
        "PYTHONPYCACHEPREFIX": str(dispatch_state_root(jobs).parent / "pycache"),
        GOVERNOR_RESERVATION_ENV: "",
        "AGENT_OWNER_ROUTE_FILE": route_file,
        "AGENT_OWNER_ROUTE_ID": route_id,
        "AGENT_OWNER_ROUTE_HASH": route_hash,
    })
    if metadata.get("worker_role"):
        dispatch_env["AGENT_DISPATCH_WORKER_ROLE"] = metadata["worker_role"]
    else:
        dispatch_env.pop("AGENT_DISPATCH_WORKER_ROLE", None)
    if resolved.get("worker_mode"):
        dispatch_env["AGENT_DISPATCH_WORKER_MODE"] = str(resolved["worker_mode"])
    else:
        dispatch_env.pop("AGENT_DISPATCH_WORKER_MODE", None)
    if resolved.get("unit"):
        dispatch_env["AGENT_DISPATCH_UNIT"] = str(resolved["unit"])
    else:
        dispatch_env.pop("AGENT_DISPATCH_UNIT", None)
    applied = launch.get("applied_permissions") or {}
    nested_network = applied.get("nested_headless_network")
    if type(nested_network) is not bool:
        return {"state": "not-started",
                "reason": "consumer-respawn-launch-record-unavailable:nested_headless_network"}
    if nested_network:
        dispatch_env["AGENT_NESTED_HEADLESS_NETWORK"] = "1"
    else:
        dispatch_env.pop("AGENT_NESTED_HEADLESS_NETWORK", None)
    access = applied.get("execution_access")
    request_path = effective.get("request_path")
    source_path = access.get("source_path") if isinstance(access, dict) else None
    if request_path:
        if not source_path or str(Path(source_path)) != request_path:
            return {"state": "not-started",
                    "reason": "consumer-respawn-launch-record-unavailable:execution_access_request"}
        dispatch_env["AGENT_DISPATCH_EXECUTION_ACCESS_FILE"] = request_path
    else:
        dispatch_env.pop("AGENT_DISPATCH_EXECUTION_ACCESS_FILE", None)
    dispatch_env.update(worker_env)
    dispatch_env["AGENT_RUNTIME_DIR"] = runtime_dir
    dispatch_env.update(parent_completion.worker_runtime_identity("codex"))
    dispatch_env.update(git_optional_locks_env())
    permission_args = SimpleNamespace(
        worker_type="owner", dispatch_depth=1, capability=str(resolved.get("capability") or ""),
        worktree=row_fields[3], route_file=route_file, route_id=route_id, route_hash=route_hash,
        artifact_root=artifact_root, artifact_output_dir=artifact_env.get("AGENT_ARTIFACT_OUTPUT_DIR", ""),
        attempt_id=attempt_id,
    )
    dispatch_env.update(commit_policy.runtime_permission_context_env(permission_args))
    if check_only:
        return {"state": "ready", "launch_home": launch_home, "respawn_release": str(ROOT),
                "runtime_dir": runtime_dir}
    governor = ROOT / "utilities" / "model-worker-governor.py"
    try:
        governor_root = resolve_model_governor_root(artifact_root, environ=dispatch_env)
        token, _reservation = reserve_governor_token(
            governor, governor_root, "dispatch",
            provided_token=os.environ.get(GOVERNOR_RESERVATION_ENV, ""),
        )
    except DispatchContractError as exc:
        return {"state": "not-started", "reason": exc.reason, "detail": exc.detail}
    dispatch_env["AGENT_MODEL_GOVERNOR_ROOT"] = str(governor_root)
    dispatch_env[GOVERNOR_RESERVATION_ENV] = token

    prompt_text = (
        "Continue the same work in this same thread; the runtime restored this owner's input consumer. "
        "Pending corrections follow. Preserve active children and registered resources; do not relaunch them."
    )
    failure_read_fd, failure_write_fd = os.pipe()
    proc = None
    try:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", dir=runtime_dir) as prompt_stream:
            prompt_stream.write(prompt_text)
            prompt_stream.flush()
            prompt_stream.seek(0)

            def spawn_worker(gate_fd):
                fence = [
                    sys.executable, str(ROOT / "utilities" / "launch-fence.py"),
                    "--parent-pid", str(os.getpid()), "--gate-fd", str(gate_fd),
                    "--failure-fd", str(failure_write_fd),
                    "--jobs", str(jobs), "--attempt-id", attempt_id,
                ]
                fence.extend(("--route-file", route_file, "--launch-phase", "start"))
                fence.extend((
                    "--post-release-parent-death-signal",
                    "kill" if args.launch_lifecycle == FOREGROUND_SCOPED else "none",
                    "--", sys.executable, str(governor), "--root", str(governor_root),
                    "run", "--class", "dispatch", "--", "sh", "-c", shell,
                ))
                child = subprocess.Popen(
                    fence, start_new_session=True,
                    **frame_repository.child_options(env=dispatch_env,
                        pass_fds=(gate_fd, failure_write_fd)), stdin=prompt_stream,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                try:
                    os.close(failure_write_fd)
                except OSError:
                    pass
                return child

            process, identity = spawn_claimed_attempt(
                jobs, attempt_id, parent_binding=None, spawn=spawn_worker,
                respawn_of=expected,
                pre_release=lambda new_identity: attach_summary_owner(
                    args, log_path, summary_prompt, new_identity),
            )
            proc = process
            wait_governor_reservation_claim(governor, governor_root, token, process)
    except DispatchContractError as exc:
        cancel_governor_reservation(governor, governor_root, token)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        failure = None
        if proc is not None:
            failure, _released = read_launch_fence_failure(failure_read_fd)
        reason = str(failure.get("reason")) if failure else exc.reason
        detail = str(failure.get("detail")) if failure else exc.detail
        return {"state": "not-started", "reason": reason, "detail": detail,
                "launch_home": launch_home, "respawn_release": str(ROOT),
                "runtime_dir": runtime_dir}
    except (OSError, ValueError, TypeError) as exc:
        cancel_governor_reservation(governor, governor_root, token)
        return {"state": "not-started", "reason": "consumer-respawn-launch-failed",
                "detail": type(exc).__name__, "launch_home": launch_home,
                "respawn_release": str(ROOT), "runtime_dir": runtime_dir}
    finally:
        for fd in (failure_read_fd, failure_write_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    refreshed = JOIN.exact_attempt_row(jobs, attempt_id)
    current = refreshed.metadata
    if ((current.get("pid"), current.get("pid_start")) != (str(process.pid), identity.get("pid_start"))
            or current.get("launch_claimed") != "1"):
        return {"state": "not-started", "reason": "attempt-respawn-publication-unconfirmed"}
    return {
        "state": "respawned", "pid": current.get("pid"),
        "pid_start": current.get("pid_start"),
        "owner_identity_lineage": current.get("owner_identity_lineage", ""),
        "launch_home": launch_home, "respawn_release": str(ROOT),
        "runtime_dir": runtime_dir,
    }


def _effective_parent_cwd(args):
    return parent_completion.effective_parent_cwd(args)


def acquire_review_lease_after_claim(
    args, jobs: Path, identity: dict[str, str]
) -> dict[str, str]:
    return WRAPPER_COMMON.acquire_review_lease_after_claim(
        args, jobs, identity,
        detached=DETACHED,
        lease_held=lambda *a, **k: review_governed_lease_is_held(*a, **k),
        acquire_admission=acquire_review_admission,
        acquire_foreground_admission=acquire_foreground_review_admission,
        lease_acquire=review_lease_acquire,
    )


def attach_summary_owner(args, log_path: Path, prompt_path: Path, identity):
    return WRAPPER_COMMON.attach_summary_owner(
        args, log_path, prompt_path, identity,
        harness="codex", summary_launcher=launch_summary_owner,
        runtime_home=(getattr(args, "worker_runtime_env", None) or {}).get("CODEX_HOME"),
    )

def append_job(jobs: Path, args: argparse.Namespace) -> bool:
    sandbox = effective_runtime_sandbox(args)
    return WRAPPER_COMMON.append_job(
        jobs, args, harness="codex", runtime_sandbox=sandbox, effort_key="reasoning",
        adapter_fields=f",approval={args.approval}" if args.approval != "inherit" else "",
        replacement_sandbox=sandbox,
        claim=claim_attempt_row, marker_gate=completion_marker_gate,
    )


def nested_headless_network_enabled(args: argparse.Namespace) -> bool:
    """Apply the shared current-state fence to the existing owner network grant."""
    projection = commit_policy.launch_permission_projection(args)
    return ((nested_headless_network_eligible(args)
             or bool(projection["task_publish_scope"]))
            and not projection["observation_only"])


def nested_headless_network_eligible(args: argparse.Namespace) -> bool:
    """Resolve the route's stable network capacity, before Git-state projection."""
    return codex_standard_owner_network_enabled(
        dispatch_depth=args.dispatch_depth,
        worker_type=args.worker_type,
        intensity=args.intensity,
        sandbox=args.sandbox,
        gpu_resource_owner=getattr(args, "gpu_execution_scope", False),
    )


def nested_codex_home_path(worktree: Path, jobs: Path | None = None,
                           release_root: Path | None = None) -> Path:
    """Select the external canonical runtime-home scope for this worktree and release.

    The home links one release, so owners of two releases in one worktree each get their
    own: a later owner re-linking a shared home would fail the earlier owner's next child
    start (`codex-runtime-projection-mismatch`)."""
    def owned_directory_or_ancestor(path):
        while not path.exists() and not path.is_symlink():
            path = path.parent
        return path.is_dir() and not path.is_symlink() and path.stat().st_uid == os.geteuid()

    worktree = Path(worktree).resolve()
    canonical_jobs = Path(jobs or os.environ.get("AGENT_DISPATCH_JOBS", ""))
    if canonical_jobs.is_absolute():
        state_root = dispatch_state_root(canonical_jobs)
        key = hashlib.sha256((str(worktree) + "\0worker-v1").encode()).hexdigest()[:32]
        if release_root is not None:
            key += "." + hashlib.sha256(str(Path(release_root).resolve()).encode()).hexdigest()[:12]
        fallback = state_root / "homes" / "codex" / key
        if (fallback.resolve().is_relative_to(state_root)
                and owned_directory_or_ancestor(fallback)):
            return fallback
    raise DispatchContractError("nested-codex-home-projection-failed",
                                "no user-owned runtime home in the canonical dispatch state root")


def prepare_nested_codex_home(worktree: Path, source_home: Path | None = None,
                              *, jobs: Path | None = None,
                              projection_root: Path | None = None) -> Path:
    """Create a writable Codex home inside the owner's sandbox.

    Recursive ``codex exec`` needs to write session/app-server state. Pointing
    it at the user's normal CODEX_HOME fails under workspace-write even when
    network is enabled. The projection keeps mutable state in existing owner
    writable scope, links the existing credential/config read-only, and installs only
    harness-owned runtime links. Credentials are never copied or modified.
    """

    source = (source_home or Path(os.environ.get("CODEX_HOME", "~/.codex"))).expanduser().resolve()
    # Runtime projection identity follows the installed/canonical AGENT_HOME,
    # not the source-only task worktree containing this wrapper. Otherwise a
    # nested eligibility check compares a worktree-linked local CODEX_HOME with
    # the inherited canonical AGENT_HOME and rejects a valid recursive launch.
    projection_root = Path(projection_root or resolve_agent_home().resolve())
    destination = nested_codex_home_path(worktree, jobs, projection_root)
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o700)

    auth = source / "auth.json"
    if not auth.is_file():
        fallback = Path.home() / ".codex" / "auth.json"
        auth = fallback if fallback.is_file() else auth
    if not auth.is_file():
        raise DispatchContractError("nested-codex-auth-missing", str(auth))

    prepare_worker_home(projection_root, 'codex', 'owner', 'nested-owner',
                        env={**os.environ, 'CODEX_HOME': str(source),
                             'AGENT_DISPATCH_JOBS': str(jobs or resolve_dispatch_state_root(projection_root) / 'jobs.log')},
                        destination=destination)
    return destination


def close_job_row(jobs: Path, slug: str, worktree: str, reason: str, reset: str, attempt_id: str | None = None) -> bool:
    return WRAPPER_COMMON.close_job_row(
        jobs, slug, worktree, reason, reset, attempt_id,
        materialize=materialize_after_terminal_close,
    )


def resolve_agent_home() -> Path:
    # Delegates to the one canonical resolver, passing this runtime's bundle
    # pointer (~/.codex/hearting) so codex's deliberate bundle-first priority
    # (immutable runtime activation, session pinning) is preserved without
    # forking the resolver's fallback chain.
    return _resolve_agent_home(runtime_pointer=Path.home() / ".codex" / "hearting")


def child_runtime_homes(args: argparse.Namespace, profile_home: Path | None) -> dict[str, str]:
    """The Codex home a child runs with.

    The owner-only home is linked to the release this launch resolved, the same release the
    child gets as `AGENT_HOME` (OPERATIONS §5.9a): after a later pointer change the two still
    agree, and the child's own starts pass the projection check.
    """
    if args.nested_codex_home is not None:
        return {"CODEX_HOME": str(args.nested_codex_home)}
    if profile_home is not None:
        return {"CODEX_HOME": str(profile_home)}
    return {}


def ensure_runtime_home_projection(worktree: Path) -> Path | None:
    """Deprecated observation hook; liveness resolves canonical external homes."""
    return None


def check_runtime_projection(worktree: str, require_hook_trust: bool) -> int:
    command = [str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh"), "headless", "--check"]
    if require_hook_trust:
        command.append("--require-hook-trust")
    command.append(worktree)
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        lines = (result.stdout or "").splitlines()
        if not any(line.startswith("reason=") for line in lines):
            # The link-by-link projection check names each failure inside its own line; the
            # receipt still needs the one reason a launcher reads.
            first = next((line for line in lines if ":failed" in line), "") or f"exit-{result.returncode}"
            return fail("codex-runtime-projection-mismatch", result.returncode, detail=first[:300])
    return result.returncode


def validate_preflight(kind: str, command: str, value: str, reason: str) -> int:
    result = subprocess.run(
        [str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh"), command, value],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode == 0:
        return 0
    rc = fail(reason, result.returncode or 64, **{kind: value})
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return rc


def validate_dispatch_inputs(args: argparse.Namespace) -> int:
    rc = validate_preflight("capability", "capability-info", args.capability, "invalid-dispatch-capability")
    if rc != 0:
        return rc
    capability_info = subprocess.run(
        [str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh"), "capability-info", args.capability],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    try:
        validate_capability_mode(args.capability, args.capability_mode, capability_info.stdout)
    except DispatchModeContractError as exc:
        return fail(exc.reason, 64, **exc.fields)
    if args.worker_mode:
        rc = validate_preflight(
            "worker_mode", "mode-info", args.worker_mode, "invalid-dispatch-worker-mode"
        )
        if rc != 0:
            return rc
    return DISPATCH_MAIN.validate_dispatch_shape(_ADAPTER, args, qa_fields={})


def bind_internal_eligibility_probe(args: argparse.Namespace) -> None:
    WRAPPER_COMMON.bind_internal_eligibility_probe(args, "codex")


def validate_route_record(args: argparse.Namespace) -> int:
    return WRAPPER_COMMON.validate_route_record(
        args, marker_gate=completion_marker_gate, runtime_harness="codex",
        runtime_sandbox=(effective_runtime_sandbox(args)
                         if getattr(args, "sandbox", None) else None),
    )


def complete_foreground_wrapper_pass(args, jobs: Path, outcome, terminal: dict, terminal_closed: bool) -> None:
    """Complete a settled foreground PASS route node from the outer wrapper."""
    if (
        not outcome.failure
        and not terminal_closed
        and args.registered_worker
        and args.route_file
        and args.route_node
        and terminal.get("state") == "valid"
        and terminal.get("verdict") == "PASS"
        and terminal.get("artifact_state") == "readable"
    ):
        # Only this outer wrapper can prove its governed process group
        # reaped.  Complete after that receipt, before returning.
        try:
            wrapper_row = exact_attempt_row(jobs, args.attempt_id)
            completion_reason = close_wrapper_pass(wrapper_row, jobs=jobs)
        except JoinContractError as exc:
            completion_reason = str(exc)
        if completion_reason:
            args.worker_failure = "route-completion-rejected"


# Shared main() phases read this wrapper's names at call time (see dispatch_headless_main).
_ADAPTER = DISPATCH_MAIN.AdapterNamespace(globals())


@frame_repository.process_entry
def main(argv: list[str]) -> int:
    args = parser().parse_args(argv[1:])
    rc = DISPATCH_MAIN.bind_entry(_ADAPTER, args, argv)
    if rc is not None:
        return rc
    forced_sandbox = os.environ.get("CODEX_DISPATCH_SANDBOX_FORCE")
    if forced_sandbox:
        if forced_sandbox not in ("read-only", "workspace-write", "danger-full-access"):
            return fail("invalid-forced-dispatch-sandbox", 64, sandbox=forced_sandbox)
        args.sandbox = forced_sandbox
    _bind_runtime_parent(args)
    action = DISPATCH_MAIN.bind_action(args)
    if args.broker_request_id or args.launch_authority == "ancestor-broker":
        return fail("launch-broker-retired", 76, child_spawned="0")
    # One execution root per launch (OPERATIONS §5.9a): the installed release when it reads this
    # route's contract, else the release the parent tree inherited. Guard, child environment and
    # the row's `launch_home` (the release this launch ran on) all follow it.
    args.agent_home = WRAPPER_COMMON.select_execution_root(
        ROOT, sealed_launch_home(resolve_agent_home()), "codex", argv[1:],
        route_file=args.route_file or os.environ.get("AGENT_OWNER_ROUTE_FILE"))
    bind_parent_completion_delivery(args)
    rc = DISPATCH_MAIN.bind_repository(_ADAPTER, args)
    if rc is not None:
        return rc
    worktree = Path(args.worktree)
    invalid_mount = invalid_codex_mount_target(args, worktree)
    if invalid_mount is not None:
        return fail(
            "invalid-worktree-codex-mount-target",
            65,
            detail=".codex must be a directory while the Codex sandbox is enabled",
            failure_scope="exact-worktree",
            codex_command="ok" if shutil.which("codex") else "unavailable",
            retry_on_isolated_worktree="1",
            path=str(invalid_mount),
            child_spawned="0",
        )
    try:
        args.artifact_root = resolve_artifact_root(args.worktree)
        args.runtime_dir = WRAPPER_COMMON.prepare_runtime_root(
            args.artifact_root, repository_kind=args.repository_kind, source_root=args.worktree)
        args.report_bundle_root = resolve_report_bundle_root(args.route_file, args.route_node)
    except ValueError as e:
        return fail("writable-root-resolution-failed", 64, detail=str(e), worktree=args.worktree)
    args.worker_type = resolve_worker_type(
        explicit=args.worker_type,
        dispatch_depth=args.dispatch_depth,
        worker_role=args.worker_role,
        route_node=args.route_node,
        profile_type=profile_worker_type(ROOT, args.profile),
    )
    try:
        normalize_dispatch_modes(
            args,
            default_capability_mode=capability_mode_from_route_file(args.route_file),
        )
    except DispatchModeContractError as exc:
        return fail(exc.reason, 64, **exc.fields, child_spawned="0")
    rc = validate_dispatch_inputs(args)
    if rc != 0:
        return rc
    rc = DISPATCH_MAIN.check_nested_eligibility(_ADAPTER, args, action, "codex")
    if rc is not None:
        return rc
    rc = DISPATCH_MAIN.bind_owner_route(_ADAPTER, args, "codex")
    if rc is not None:
        return rc
    rc = validate_route_record(args)
    if rc != 0:
        return rc
    try:
        apply_gpu_execution_sandbox(args)
    except (DispatchContractError, OSError, ValueError) as exc:
        return fail(getattr(exc, "reason", "gpu-execution-selection-invalid"), 65,
                    detail=str(exc), child_spawned="0")
    rc = DISPATCH_MAIN.expect_replica_batch(_ADAPTER, args, action, "codex")
    if rc is not None:
        return rc
    rc = DISPATCH_MAIN.apply_attempt_policy(_ADAPTER, args, "codex")
    if rc is not None:
        return rc
    rc = DISPATCH_MAIN.resolve_launch_models(_ADAPTER, args, model_error_fields={})
    if rc is not None:
        return rc
    try:
        validate_interactive_parent_launch(args)
    except DispatchContractError as exc:
        return fail(
            exc.reason,
            69,
            detail=exc.detail,
            parent_completion_delivery=args.parent_completion_delivery,
            parent_completion_reason=args.parent_completion_reason,
            child_spawned="0",
        )
    if args.start and shutil.which("codex") is None:
        return fail("codex-command-unavailable", 69, worktree=args.worktree)
    profile_home: Path | None = None
    if args.start:
        rc = check_runtime_projection(args.worktree, args.require_hook_trust)
        if rc != 0:
            return rc
        if args.profile:
            profile_registry = resolve_global_registry(
                args.agent_home, args.jobs, args.dispatch_depth, action
            )
            home_root = dispatch_state_root(profile_registry.path) / "homes"
            build_home = resolve_agent_home() / "tools" / "profile" / "build-home.py"
            check_result = subprocess.run(
                ["python3", str(build_home), args.profile, "--check"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if check_result.returncode != 0:
                if check_result.stdout:
                    print(check_result.stdout, end="")
                if check_result.stderr:
                    print(check_result.stderr, end="", file=sys.stderr)
                return fail("invalid-dispatch-profile", 3, profile=args.profile)

    runtime_home_projection = None
    if args.start and profile_home is None:
        runtime_home_projection = ensure_runtime_home_projection(worktree)

    agent_home = args.agent_home
    try:
        registry = resolve_global_registry(agent_home, args.jobs, args.dispatch_depth, action)
        jobs = registry.path
        args.attempt_id = new_attempt_id(args.attempt_id) if action in ("register", "start") else args.attempt_id
        if action in ("register", "start"):
            args.command_attempt_id = args.attempt_id
            ensure_global_registry_writable(jobs)
    except DispatchContractError as e:
        return fail(e.reason, 73, detail=e.detail, child_spawned="0")
    rc = DISPATCH_MAIN.registry_gates(_ADAPTER, args, action, agent_home=agent_home, jobs=jobs)
    if rc is not None:
        return rc
    rc = DISPATCH_MAIN.bind_live_parent(_ADAPTER, args, action, jobs=jobs)
    if rc is not None:
        return rc
    rc = DISPATCH_MAIN.rebind_replica_batch(_ADAPTER, args, action, "codex")
    if rc is not None:
        return rc
    args.worker_type = resolve_worker_type(
        explicit=args.worker_type,
        dispatch_depth=args.dispatch_depth,
        worker_role=args.worker_role,
        route_node=args.route_node,
        profile_type=profile_worker_type(ROOT, args.profile),
    )
    args.jobs_path = jobs
    try:
        from review_input import prepare_request as prepare_review_input
        prepare_review_input(args)
    except DispatchContractError as exc:
        return fail(exc.reason, 65, detail=exc.detail, child_spawned="0", registry_mutation="0")
    args.completion_delivery_reason = "not-applicable"
    try:
        args.resolved_completion_delivery = resolve_completion_delivery(args)
        if args.resolved_completion_delivery == "app-server-supervised":
            completion_state_path(args)
            completion_lease_path(args)
    except DispatchContractError as e:
        return fail(e.reason, 69, detail=e.detail, child_spawned="0")
    log_dir = (
        Path(args.log_dir)
        if args.log_dir
        else dispatch_state_root(args.jobs_path) / "logs"
    )
    task_input = task_prompt(args)
    args.replacement_raw_task = task_input[0]
    prompt_text, prompt_source = dispatch_prompt(args, task_input)
    from review_input import prompt_block as review_input_prompt
    prompt_text += review_input_prompt(args)
    from dispatch_replacement import recovery_instructions
    prompt_text += recovery_instructions(args)
    assignment_sha256 = "sha256:" + hashlib.sha256(
        task_input[0].encode("utf-8")
    ).hexdigest()
    rc = DISPATCH_MAIN.rebind_replica_batch(_ADAPTER, args, action, "codex", assignment_sha256=assignment_sha256)
    if rc is not None:
        return rc
    permission_projection = commit_policy.capture_launch_permission_projection(args)
    args.task_publish_scope = permission_projection["task_publish_scope"]
    args.nested_headless_network_eligible = (
        nested_headless_network_eligible(args) or bool(args.task_publish_scope)
    )
    args.nested_headless_network = nested_headless_network_enabled(args)
    try:
        default_roots = adapter_default_roots(
            args,
            progress_writable_dirs(args),
            nested_owner_writable_dirs(args),
            route_bound_worker_writable_dirs(args),
            linked_worktree_git_writable_dirs(args),
            (
                (_spec_grounding_dir(args),)
                if spec_read_marker_required(args)
                else ()
            ),
            (
                (dispatch_state_root(args.jobs_path),)
                if registry_writable_launch(args)
                else ()
            ),
        )
        args.execution_access_grant = route_authority.bind_launch_access(
            args,
            runtime=(
                "codex-app-server"
                if args.resolved_completion_delivery == "app-server-supervised"
                else "codex-exec"
            ),
            default_roots=default_roots,
            network_available=args.nested_headless_network,
            # A GPU child may use the network its exact live parent was granted.
            parent_network=args.gpu_execution_scope,
            effective_sandbox=effective_runtime_sandbox(args),
            gpu_resource_scope=(args.gpu_execution_scope
                                and effective_runtime_sandbox(args) == "danger-full-access"),
            inherit_parent_sandbox=uses_enclosing_codex_sandbox(args),
        )
    except ExecutionAccessError as exc:
        return fail(exc.reason, 64, detail=exc.detail, child_spawned="0")
    for advisory in owner_write_advisories(args):
        print(OWNER_WRITE_ADVISORY.RECEIPT_KEY + json.dumps(advisory, ensure_ascii=False), flush=True)
        print(advisory["message"], file=sys.stderr, flush=True)
    try:
        validate_nested_owner_registry_projection(args)
    except DispatchContractError as e:
        return fail(e.reason, 73, detail=e.detail, child_spawned="0")
    args.nested_codex_home = None
    try:
        # The home projects the release this launch resolved (`args.agent_home`), the release
        # the child runs: a later pointer change moves neither of them.
        args.nested_codex_home_path = (
            nested_codex_home_path(worktree, args.jobs_path, args.agent_home)
            if args.nested_headless_network else None
        )
        if action == "start" and args.nested_headless_network:
            args.nested_codex_home = prepare_nested_codex_home(
                worktree, jobs=args.jobs_path, projection_root=args.agent_home)
    except DispatchContractError as e:
        return fail(e.reason, 73, detail=e.detail, child_spawned="0")
    prompt_name = (
        f"{args.slug}.{getattr(args, 'command_attempt_id', None)}.codex.prompt.txt"
        if getattr(args, "command_attempt_id", None)
        else f"{args.slug}.codex.prompt.txt"
    )
    prompt_path = log_dir / prompt_name
    # Every registered attempt gets a distinct transcript.  Reusing the legacy
    # slug-only path lets a later retry append another turn to the same JSONL,
    # so harvesting the earlier row can accidentally select the newer verdict.
    # PID-less legacy readers still retain their slug-only fallback.
    log_name = (
        f"{args.slug}.{getattr(args, 'command_attempt_id', None)}.codex.jsonl"
        if getattr(args, "command_attempt_id", None)
        else f"{args.slug}.codex.jsonl"
    )
    log_path = log_dir / log_name
    args.log_path = log_path
    try:
        ensure_owner_writable_dirs(args)
    except DispatchContractError as e:
        return fail(e.reason, 73, detail=e.detail, child_spawned="0")
    # Preparation may create existing bootstrap roots used by the builders.
    # Publish the same defaults that the command below actually applies.
    default_roots = adapter_default_roots(
        args, default_roots, nested_owner_writable_dirs(args), route_bound_worker_writable_dirs(args),
    )
    command = None if action == 'start' else shell_command(args, prompt_path, log_path)

    def before_claim():
        try:
            assert_register_mkdir_containment(args, prompt_path.parent, log_path.parent)
        except DispatchContractError as e:
            return fail(e.reason, 73, detail=e.detail, child_spawned="0")
        Path(frame_repository.execution_path(prompt_path.parent)).mkdir(parents=True, exist_ok=True)
        Path(frame_repository.execution_path(log_path.parent)).mkdir(parents=True, exist_ok=True)
        return None
    claim = DISPATCH_MAIN.claim_registered_attempt(
        _ADAPTER, args, action, jobs=jobs, prompt_path=prompt_path, prompt_text=prompt_text, before_claim=before_claim)
    if claim.rc is not None:
        return claim.rc
    governor, governor_root, reservation_token = claim.governor, claim.governor_root, claim.reservation_token
    if action == "start" and args.attempt_claimed:
        dispatch_env = DISPATCH_MAIN.worker_environment(
            _ADAPTER, args, harness="codex", jobs=jobs, governor_root=governor_root,
            reservation_token=reservation_token,
            permission_env=commit_policy.runtime_permission_context_env(
                args, adapter="codex",
                base_sandbox=base_runtime_sandbox(args),
                network_eligible=bool(args.nested_headless_network_eligible),
            ),
            current_sandbox=effective_runtime_sandbox(args),
            supervised=args.resolved_completion_delivery == "app-server-supervised",
        )
        DISPATCH_MAIN.apply_worker_fields(dispatch_env, args)
        if args.nested_headless_network:
            dispatch_env["AGENT_NESTED_HEADLESS_NETWORK"] = "1"
        else:
            dispatch_env.pop("AGENT_NESTED_HEADLESS_NETWORK", None)
        if args.resolved_completion_delivery == "app-server-supervised":
            dispatch_env["AGENT_DISPATCH_COMPLETION_STATE_FILE"] = str(
                completion_state_path(args)
            )
            dispatch_env["AGENT_DISPATCH_SUPERVISOR_LEASE_FILE"] = str(
                completion_lease_path(args)
            )
        else:
            dispatch_env.pop("AGENT_DISPATCH_COMPLETION_STATE_FILE", None)
            dispatch_env.pop("AGENT_DISPATCH_SUPERVISOR_LEASE_FILE", None)
        # All types receive a minimal home, including first-level reviews and
        # support callers. The nested owner remains inside its declared scope.
        args.worker_runtime_env = prepare_worker_home(
            args.agent_home, 'codex', args.worker_type, args.attempt_id,
            env=dispatch_env, destination=args.nested_codex_home, profile=args.profile)
        dispatch_env.update(args.worker_runtime_env)
        command = shell_command(args, prompt_path, log_path)
        launch_spec = DISPATCH_MAIN.LaunchSpec(
            harness="codex",
            runtime_sandbox=lambda: effective_runtime_sandbox(args),
            metadata_head=lambda env: {"runtime_home": env["CODEX_HOME"]},
            scope_kwargs=lambda: {
                "sandbox": effective_runtime_sandbox(args),
                "network": bool(args.nested_headless_network),
                "read_roots": (args.execution_access_grant.read_roots
                               if args.execution_access_grant else ()),
            },
            grant_runtime=lambda: (
                "codex-app-server"
                if args.resolved_completion_delivery == "app-server-supervised"
                else "codex-exec"
            ),
            grant_kwargs=lambda: {
                "unmet": getattr(args, "local_network_unmet", ()),
                "network_allowed": args.nested_headless_network,
                "execution_selection": args.gpu_execution_selection,
            },
            after_foreground_settle=lambda outcome, terminal, terminal_closed: complete_foreground_wrapper_pass(
                args, jobs, outcome, terminal, terminal_closed),
        )
        rc = DISPATCH_MAIN.launch_claimed_attempt(
            _ADAPTER, launch_spec, args, jobs=jobs, env=dispatch_env, command=command, claim=claim,
            log_path=log_path, prompt_path=prompt_path, agent_home=agent_home,
            default_roots=default_roots,
        )
        if rc is not None:
            return rc

    print("check=ok")
    print("adapter=codex")
    print("runtime_surface=codex-exec-headless")
    for line in launch_receipt.completion_lines(args):
        print(line)
    print(
        "supervisor_lease_file="
        + (
            str(completion_lease_path(args))
            if args.resolved_completion_delivery == "app-server-supervised"
            else "-"
        )
    )
    for line in launch_receipt.identity_lines(args, action):
        print(line)
    print(f"parent_attempt_id={args.parent_binding.attempt_id if args.parent_binding else '-'}")
    print(f"parent_session_id={args.parent_session_id or '-'}")
    for line in launch_receipt.worker_lines(args):
        print(line)
    settings = args.resolved_model_settings
    for line in launch_receipt.model_lines(
            args, settings, config_state=_model_config_state,
            main_session_only_policy=_main_session_only_policy_state):
        print(line)
    print(f"reasoning={settings['reasoning']}")
    print(f"context_window_requested={settings['context_window']}")
    print(f"context_window_source={settings['context_window_source']}")
    print("context_window_native_application=codex-config:model_context_window")
    print(f"approval={args.approval}")
    for line in launch_receipt.leg_lines(args, leg_fields=_route_node_leg_fields):
        print(line)
    print(f"profile={args.profile or '-'}")
    print(f"runtime_home_projection={runtime_home_projection or '-'}")
    for line in launch_receipt.attempt_lines(
            args, jobs=jobs, registry_source=registry.source, action=action,
            launch_state=attempt_launch_state(jobs, args.attempt_id, claimed=args.attempt_claimed, action=action),
            after_lifecycle=[f"runtime_sandbox={effective_runtime_sandbox(args)}"],
            before_early_death=[
                f"require_hook_trust={1 if args.require_hook_trust else 0}",
                f"nested_headless_network={1 if args.nested_headless_network else 0}",
                f"nested_codex_home={args.nested_codex_home_path or '-'}",
                "nested_owner_writable_dirs=" + (";".join(map(str, nested_owner_writable_dirs(args))) or "-"),
            ]):
        print(line)
    for line in launch_receipt.prompt_lines(prompt_source, prompt_path, log_path):
        print(line)
    if command is None:
        command = shell_command(args, prompt_path, log_path)
    print(f"command={command}")
    return (
        75
        if getattr(args, "managed_sidecar_state", "") == "launch-failed"
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
