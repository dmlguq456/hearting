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
import time
from datetime import datetime, timezone
from pathlib import Path



ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utilities"))
from review_input import preview_request_nodes
from codex_permission_profile import commit_profile_config, config_arguments
from worker_runtime_home import prepare_worker_home, codex_worker_arguments
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
from dispatch_summary import launch_summary_owner, owner_root  # noqa: E402
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
    supervised_owner_prompt,  # noqa: E402
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
    p = argparse.ArgumentParser(description=__doc__)
    action = p.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="print the command without writing jobs.log")
    action.add_argument("--register", action="store_true", help="append an open job without launching")
    action.add_argument("--start", action="store_true", help="append an open job and launch in background")
    p.add_argument("--worktree", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--capability", required=True)
    p.add_argument("--capability-mode", help="entry capability mode (for example dev)")
    p.add_argument("--worker-mode", help="non-owner unit/persona compatibility path")
    p.add_argument("--mode", help="legacy compatibility input; scalar=capability, slash=worker")
    p.add_argument("--qa", default=None)  # optional/derived from --intensity (CONVENTIONS §1.1)
    p.add_argument("--intensity", default="standard")
    p.add_argument("--dispatch-depth", dest="dispatch_depth", type=int, default=1)
    p.add_argument(
        "--parent", dest="parent_slug",
        help="logical parent slug (never an attempt id)",
    )
    p.add_argument(
        "--parent-attempt-id",
        default=os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") or None,
        help="exact parent attempt id (never a slug)",
    )
    p.add_argument(
        "--parent-session-id",
        default=parent_completion.default_parent_session_id(),
    )
    p.add_argument(
        "--parent-cwd",
        default=os.environ.get("AGENT_DISPATCH_PARENT_CWD") or None,
    )
    p.add_argument("--worker-role", help="legacy compatibility metadata; not bootstrap identity")
    p.add_argument("--worker-type", choices=("owner", "stage", "review", "support", "frame"))
    p.add_argument("--review-output", help="exact durable report path for a route-free review worker")
    from review_input import add_arguments as review_input_arguments
    review_input_arguments(p)
    p.add_argument("--unit", default="", help="catalog unit ref for the assigned route node (roles/units/<unit>.md)")
    p.add_argument("--assigned-contract")
    p.add_argument("--owner", dest="capability_owner")
    p.add_argument("--route-file")
    p.add_argument("--route-id")
    p.add_argument("--route-hash")
    p.add_argument("--route-node")
    p.add_argument("--registry-digest")
    p.add_argument("--write-scope")
    p.add_argument("--completion-gate")
    p.add_argument("--harness-affinity")
    p.add_argument("--explicit-adapter")  # the request a sealed worker/owner pin replaced; recorded only
    p.add_argument(
        "--owner-harness",
        default=os.environ.get("AGENT_DISPATCH_OWNER_HARNESS") or "codex",
    )
    p.add_argument("--prompt-file")
    p.add_argument("--prompt-text")
    p.add_argument("--execution-access-file")
    p.add_argument("--jobs")
    p.add_argument("--attempt-id")
    p.add_argument("--broker-request-id")
    p.add_argument("--fallback-ordinal", type=int, default=0)
    p.add_argument("--fallback-hop")
    p.add_argument("--execution-surface", default="registered-headless")
    p.add_argument("--registered-worker", type=int, choices=(0, 1), default=1)
    p.add_argument("--capacity-retry", type=int, choices=(0, 1), default=0)
    p.add_argument("--prior-attempt-id")
    p.add_argument("--automatic-retry-of", help="Exact failed predecessor; revalidated atomically at claim")
    p.add_argument("--cooled-model")
    p.add_argument("--selection-source")
    p.add_argument("--launch-authority", choices=("conductor", "ancestor-broker"), default="conductor")
    p.add_argument("--parent-harness", default=parent_completion.default_parent_harness("codex"))
    p.add_argument("--parent-transport", default=os.environ.get("AGENT_DISPATCH_CURRENT_TRANSPORT") or "unknown")
    p.add_argument("--parent-sandbox", default=os.environ.get("AGENT_DISPATCH_CURRENT_SANDBOX") or "unknown")
    # default None (not "unknown"): an explicitly supplied `--nested-eligibility
    # unknown` must stay distinguishable from an absent flag — explicit evidence,
    # even unknown, is never overwritten by the internal probe.
    p.add_argument("--nested-eligibility", choices=("supported", "unsupported", "unknown"), default=None)
    p.add_argument("--eligibility-source", default="")
    p.add_argument("--eligibility-failure-class", default="")
    p.add_argument("--log-dir")
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
        default=float(os.environ.get("CODEX_DISPATCH_FOREGROUND_TIMEOUT", "3600")),
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


class ModelSelectionError(ValueError):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason


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


def resolve_model_settings(args: argparse.Namespace) -> dict[str, str]:
    try:
        validate_registered_profile(
            args.model_profile,
            registered_worker=bool(args.registered_worker),
            dispatch_depth=args.dispatch_depth,
            worker_type=args.worker_type,
        )
    except ModelProfileError as exc:
        raise ModelSelectionError("invalid-dispatch-model-profile", str(exc)) from exc
    try:
        binding = getattr(args, "owner_route_binding", None)
        require_top_route(
            getattr(args, "route_file", None) or getattr(binding, "route_file", None),
            profile=args.model_profile or "",
            # Pass the launching node so a frame anchor leg is checked against
            # ITS OWN sealed profile, not the owner's. Without this the route
            # compiles `top` onto the anchor and this wrapper refuses it --
            # and because each harness has its own copy of this call, omitting
            # it in one place breaks that one harness only.
            node=getattr(args, "route_node", None),
        )
    except ModelProfileError as exc:
        raise ModelSelectionError(exc.reason, str(exc)) from exc
    if args.inherit_model_settings:
        if args.model_profile or args.model_role or args.model or args.reasoning:
            raise ModelSelectionError(
                "invalid-dispatch-model-selection",
                "--inherit-model-settings is mutually exclusive with --model-profile, --model-role, --model, and --reasoning",
            )
        # The shared rule (model_config.inheritance_refusal): refused while this
        # adapter declares a main-session-only model -- on this machine the
        # interactive default IS that model -- and off the documented surface.
        refusal = inheritance_refusal(_model_policy(), "Codex")
        if refusal:
            raise ModelSelectionError(*refusal)
        return {
            "source": "inherit", "role": "inherit", "profile": "unsealed",
            "tier": "inherit", "granularity": "legacy", "model": "inherit", "reasoning": "inherit",
        }
    if args.model_profile:
        if not args.model_role and args.worker_type != "owner":
            raise ModelSelectionError(
                "model-profile-role-required",
                "route-bound --model-profile requires the independently sealed --model-role",
            )
        if bool(args.model) != bool(args.reasoning):
            raise ModelSelectionError(
                "invalid-dispatch-model-selection",
                "capacity override requires --model and --reasoning together",
            )
        if (args.model or args.reasoning) and not args.capacity_retry:
            raise ModelSelectionError(
                "model-profile-override-forbidden",
                "a route-sealed model profile may use a concrete override only on a checked capacity retry "
                "(to choose a model, seal it at compose time: capability-route.py compose --pin <target>=<harness>:<model>[@<effort>])",
            )
        try:
            resolved, _receipt = resolve_runtime_profile(
                "codex", args.model_profile, source_root=ROOT
            )
        except ModelProfileError as exc:
            raise ModelSelectionError("invalid-dispatch-model-profile", str(exc)) from exc
        model = args.model or resolved["model"]
        if resolved["profile"] == TOP_PROFILE and args.model:
            # No cascade in or out: nothing runs under the `top` label but the
            # top model itself (top review m1).
            raise ModelSelectionError(
                "profile-top-override-forbidden",
                "the top exception profile admits no concrete --model override, capacity retry included",
            )
        # A route-sealed selection pin (`compose --pin`) beats the profile's
        # own model; a checked capacity retry still replaces it, and the receipt
        # then says `pin+capacity` with the pinned model so nothing is
        # overwritten silently.
        try:
            pin = route_selection_pin(
                getattr(args, "route_file", None)
                or getattr(getattr(args, "owner_route_binding", None), "route_file", None),
                worker_type=args.worker_type, adapter="codex",
            )
        except ModelProfileError as exc:
            raise ModelSelectionError(exc.reason, str(exc)) from exc
        pin_model = pin.get("model")
        reasoning = args.reasoning or resolved["budget"]
        if pin["status"] == "applied":
            if not args.model:
                model, reasoning = pin_model, pin["effort"] or resolved["budget"]
            source = "pin+capacity" if args.model else "pin"
            if pin_target(args.worker_type) != "frame":
                # Compose already dropped a main-session-only pin model; this
                # guards a route edited after sealing and the model a capacity
                # retry actually substitutes.
                _require_headless_model(model, source)
        elif resolved["profile"] == TOP_PROFILE:
            # The one door to the main-session-only model from registered
            # dispatch: a route-sealed `top` profile (2026-09-09 사용자 결정).
            # The waiver covers exactly the resolved model.
            source = "profile-top"
        else:
            _require_headless_model(model, f"profile:{args.model_profile}")
            source = "profile+capacity" if args.model else "profile"
        return {
            "source": source,
            "role": args.model_role or "_kernel/owner",
            "profile": resolved["profile"],
            "tier": resolved["tier"],
            "granularity": resolved["granularity"],
            "model": model,
            "reasoning": reasoning,
            "pin_status": pin["status"],
            **({"pin_model": pin_model} if pin["status"] == "applied" else {}),
        }
    if args.model_role and args.model:
        raise ModelSelectionError(
            "invalid-dispatch-model-selection",
            "--model-role is mutually exclusive with --model (tier-hopping); "
            "situational tuning keeps the role's tier and adjusts --reasoning only",
        )
    if args.model_role:
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
    if not args.model and not args.reasoning:
        raise ModelSelectionError(
            "missing-dispatch-model-selection",
            "main dispatch must choose --model-role or --model with --reasoning",
        )
    if not args.model or not args.reasoning:
        raise ModelSelectionError(
            "invalid-dispatch-model-selection",
            "--model and --reasoning must be provided together",
        )
    _require_headless_model(args.model, "explicit")
    return {
        "source": "explicit", "role": "-", "profile": "unsealed",
        "tier": "explicit", "granularity": "legacy", "model": args.model, "reasoning": args.reasoning,
    }


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
    args.worker_type = resolve_worker_type(
        explicit=args.worker_type,
        dispatch_depth=args.dispatch_depth,
        worker_role=args.worker_role,
        route_node=args.route_node,
        profile_type=profile_worker_type(ROOT, args.profile),
    )
    bootstrap = render_worker_bootstrap(ROOT, args.worker_type, unit=(args.unit or None))
    args.assigned_contract = assigned_contract(
        capability=args.capability,
        worker_type=args.worker_type,
        route_node=args.route_node,
        completion_gate=args.completion_gate,
        explicit=args.assigned_contract,
        unit=args.unit,
        root=ROOT,
    )
    route_state = (
        "consume the assigned route only (wrapper-validated immutable record)"
        if args.route_file
        else "validated dispatch metadata"
    )
    heartbeat = runtime_progress_prompt()
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
    # Both ends stated: "nothing after it" alone reads as permission to put a
    # summary sentence before the block (2026-07-28 envelope losses).
    ending = (
        "End a child-registration turn only with `runtime_wait: registered-children`. "
        "When the full route is complete, end with the kernel's exact three-line handoff "
        "as the entire final message — no summary sentence before it, nothing after it.\n"
        if supervised and owner_standard_plus
        else "End with the kernel's exact three-line handoff as the entire final message — "
        "no summary sentence before it, nothing after it.\n"
    )
    return (
        f"{sync_wait_clause}"
        f"{bootstrap}\n"
        "Dispatch metadata:\n"
        f"- capability: {args.capability}\n"
        f"- capability_mode: {args.capability_mode}\n"
        f"- worker_mode: {args.worker_mode or '-'}\n"
        f"- qa: {args.qa}\n"
        f"- intensity: {args.intensity}\n"
        f"- dispatch_depth: {args.dispatch_depth}\n"
        f"- worker_type: {args.worker_type}\n"
        f"- guard_session_id: {args.attempt_id}\n"
        f"- assigned_contract: {args.assigned_contract}\n"
        f"- route_node: {args.route_node or '-'}\n"
        # quick carries it as an argument, standard+ as the env binding;
        # the owner needs the path either way.
        f"- route_file: {getattr(args, 'route_file', None) or getattr(getattr(args, 'owner_route_binding', None), 'route_file', None) or '-'}\n"
        f"{diff_attribution_prompt(args)}"
        f"- model_role: {getattr(args, 'resolved_model_settings', {}).get('role') or args.model_role or '-'}\n"
        f"- model_profile: {getattr(args, 'resolved_model_settings', {}).get('profile') or getattr(args, 'model_profile', None) or '-'}\n"
        f"- parent: {args.parent_slug or '-'}\n"
        f"- parent_session_id: {args.parent_session_id or '-'}\n"
        f"- owner: {args.capability_owner or '-'}\n"
        f"- owner_harness: {args.owner_harness or '-'}\n"
        f"- worktree: {args.worktree}\n"
        f"- artifact_root: {args.artifact_root}\n"
        f"{artifact_context_prompt(os.environ)}"
        f"- route_state: {route_state}\n\n"
        "Codex realization:\n"
        f"{contract_read_prompt(args, 'codex')}"
        "- An owner has no worker mode and must not load any unit persona path.\n"
        f"- Run $AGENT_HOME/adapters/codex/bin/preflight.sh qa-policy {args.qa} {qa_track(args.capability)} and keep its required assurance in the artifact.\n"
        "- The wrapper already validated capability mode, worker unit/mode, QA, artifact root, and any route record. Re-run worker-route only for a safety recheck.\n"
        "- Before each edit run $AGENT_HOME/adapters/codex/bin/preflight.sh write <file>; preserve required test and tool-contract checks in the artifact.\n"
        "- Codex may still auto-discover project AGENTS.md; do not explicitly load the full harness adapter bootstrap or another runtime's adapter.\n\n"
        f"{heartbeat}"
        f"{no_commit_clause}"
        f"{stage_session_prompt(args)}"
        f"{released_task_prompt(args)}"
        f"{unit_bootstrap_prompt(args, task, os.environ)}"
        f"{assignment_prompt(args, task, os.environ)}"
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
        for row in (route.get("dispatch_evidence") or {}).get("tuples", []):
            if row.get("parent_harness") == "codex" and row.get("parent_sandbox") != selection["sandbox"]:
                raise DispatchContractError("dispatch-evidence-parent-runtime-mismatch",
                                            "selected GPU owner sandbox differs from its checked tuple")
    args.gpu_execution_selection = selection
    args.gpu_execution_scope = True
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
progress_writable_dirs = HARNESS_STATE.progress_writable_dirs
registry_writable_launch = HARNESS_STATE.registry_writable_launch


def nested_owner_writable_dirs(args: argparse.Namespace) -> tuple[Path, ...]:
    """Expose only the runtime scratch roots an owner-network-widened Codex owner
    may need downstream. A pure query -- see `ensure_owner_writable_dirs` for the
    one place these directories are created before launch."""

    if not getattr(args, "nested_headless_network", False):
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
    summary_owner_root = owner_root()
    candidates = (
        canonical_registry_root,
        claude_config / "session-env",
        summary_owner_root,
    )
    return tuple(path.resolve() for path in candidates if path.is_dir())


def ensure_owner_writable_dirs(args: argparse.Namespace) -> None:
    """Create every directory this launch will grant sandbox write access to,
    once, before the child command is built. A query function (above) never
    has this side effect -- a create failure here is a typed launch failure,
    not a silently narrowed grant list."""

    to_create = list(progress_writable_dirs(args))
    if getattr(args, "nested_headless_network", False):
        to_create.append(owner_root())
    if spec_read_marker_required(args):
        to_create.append(_spec_grounding_dir(args))
    if getattr(args, "route_id", None) or getattr(
        args, "nested_headless_network", False
    ):
        to_create.append(_core_grounding_dir(args))
    for path in to_create:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
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
            args.resolved_stage_telemetry_transport = (
                "app-server-one-turn" if codex_app_server_available() else "raw-exec"
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


def shell_command(args: argparse.Namespace, prompt_path: Path, log_path: Path) -> str:
    writer = [sys.executable, str(ROOT / "utilities" / "codex-jsonl-writer.py"),
              "--log", str(log_path), "--attempt", str(getattr(args, "command_attempt_id", "") or ""), "--"]
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
        if getattr(args, "report_bundle_root", None) is not None:
            command += ["--writable-root", str(args.report_bundle_root)]
        if owner_supervised:
            route = _supervisor_route(args)
            if route:
                command += ["--route-file", route[0], "--route-id", route[1], "--route-hash", route[2]]
            if getattr(args, "max_continuations", None) is not None:
                command += ["--max-continuations", str(args.max_continuations)]
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
        return (
            " ".join(shlex.quote(x) for x in [*writer, *command])
            + f" < {shlex.quote(str(prompt_path))}"
        )
    cmd = [
        "codex",
        "exec",
        "--ephemeral",
        "--cd",
        args.worktree,
        "--add-dir",
        args.artifact_root,
    ]
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
        cmd += ["--add-dir", str(writable_dir)]
    if spec_read_marker_required(args):
        # The same read obligation and exact grant as the supervisor builder.
        cmd += ["--add-dir", str(_spec_grounding_dir(args))]
    for writable_dir in route_bound_worker_writable_dirs(args):
        # SD-72: an ordinary route-bound depth-2 worker also runs the portable
        # core-read guard hook and must record its own .core-grounding marker,
        # independent of the owner-only nested_headless_network network grant.
        cmd += ["--add-dir", str(writable_dir)]
    for writable_dir in linked_worktree_git_writable_dirs(args):
        # Commit-expected linked-worktree workers get exact Git metadata dirs;
        # the common-dir root, hooks/, and config stay ungranted.
        cmd += ["--add-dir", str(writable_dir)]
    if getattr(args, "execution_access_grant", None) is not None:
        for writable_dir in args.execution_access_grant.additional_writable_roots:
            cmd += ["--add-dir", str(writable_dir)]
    profile = commit_profile_config(
        args.worktree, [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--add-dir"],
        effective_runtime_sandbox(args), args.nested_headless_network,
        primary_commit=commit_grant_target(args),
    )
    if profile is not None:
        cmd += config_arguments(profile)
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
    cmd += [
        "--json",
        "-",
    ]
    return " ".join(shlex.quote(x) for x in [*writer, *cmd]) + f" < {shlex.quote(str(prompt_path))}"


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
    """Grant network only to a standard+ dispatch-depth-1 Codex capability owner."""

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


def annotate_job_row(jobs: Path, slug: str, worktree: str, extra_kv: str, attempt_id: str | None = None) -> bool:
    """Attach launch identity to the exact open attempt row."""
    if not jobs.is_file():
        return False
    with jobs_lock(jobs):
        lines = jobs.read_text(encoding="utf-8").splitlines(keepends=True)
        for i, line in enumerate(lines):
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 6:
                continue
            ts, status, repo, wt, row_slug, pipe = parts[:6]
            if status != "open" or row_slug != slug or wt != worktree:
                continue
            metadata = parse_registry_metadata(pipe)
            if metadata.get("attempt_schema_version") != "2":
                continue
            if attempt_id and f"attempt_id={attempt_id}" not in pipe.split(","):
                continue
            lines[i] = f"{ts}\t{status}\t{repo}\t{wt}\t{row_slug}\t{pipe},{extra_kv}\n"
            # Lock-free readers (join, owner input) must never see a truncated registry.
            _atomic_registry_replace(jobs, "".join(lines).splitlines())
            return True
    return False


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
    try:
        args.qa = resolve_qa(args.intensity, args.qa)
    except DispatchModeContractError as exc:
        return fail(exc.reason, 64, **exc.fields)
    if args.intensity not in INTENSITY_LEVELS:
        return fail(
            "invalid-dispatch-intensity",
            64,
            intensity=args.intensity,
            allowed_intensity="direct,quick,standard,strong,thorough,adversarial",
        )
    if args.dispatch_depth not in (1, 2):
        return fail("invalid-dispatch-depth", 64, dispatch_depth=str(args.dispatch_depth), allowed_dispatch_depth="1,2")
    if args.dispatch_depth == 2 and not args.parent_slug:
        return fail("missing-dispatch-parent", 64, dispatch_depth=str(args.dispatch_depth))
    if args.dispatch_depth == 2 and args.intensity in {"direct", "quick"}:
        return fail("invalid-depth-two-intensity", 64, dispatch_depth=str(args.dispatch_depth), intensity=args.intensity)
    return 0


def bind_internal_eligibility_probe(args: argparse.Namespace) -> None:
    WRAPPER_COMMON.bind_internal_eligibility_probe(args, "codex")


def validate_route_record(args: argparse.Namespace) -> int:
    return WRAPPER_COMMON.validate_route_record(
        args, marker_gate=completion_marker_gate,
    )


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv[1:])
    args.replacement_input_argv = list(argv[1:])
    # parent_sandbox is used here only to tighten the override gate, never to
    # loosen it, so consuming it before the tuple-validity check at :1939 is
    # safe even though it has not yet been validated.
    args.launch_lifecycle_resolution = reconcile_launch_lifecycle(
        args.launch_lifecycle, dict(os.environ), parent_sandbox=args.parent_sandbox
    )
    args.launch_lifecycle_requested = args.launch_lifecycle_resolution.requested
    args.launch_lifecycle = args.launch_lifecycle_resolution.effective
    args.nested_eligibility_explicit = args.nested_eligibility is not None
    if args.nested_eligibility is None:
        args.nested_eligibility = "unknown"
    if args.capacity_retry and not all(
        (args.prior_attempt_id, args.cooled_model, args.selection_source)
    ):
        return fail("capacity-retry-evidence-missing", 64, child_spawned="0")
    if not Path(args.worktree).is_absolute():
        return fail("worktree-must-be-absolute", 64, worktree=args.worktree)
    args.worktree = str(Path(args.worktree).resolve())
    forced_sandbox = os.environ.get("CODEX_DISPATCH_SANDBOX_FORCE")
    if forced_sandbox:
        if forced_sandbox not in ("read-only", "workspace-write", "danger-full-access"):
            return fail("invalid-forced-dispatch-sandbox", 64, sandbox=forced_sandbox)
        args.sandbox = forced_sandbox
    _bind_runtime_parent(args)
    action = "start" if args.start else "register" if args.register else "dry-run"
    args.action = action
    args.command_attempt_id = args.attempt_id
    if action == "dry-run":
        # Preview output must never mint or echo an identity that resembles a
        # registered attempt receipt.
        args.attempt_id = None
    if args.broker_request_id or args.launch_authority == "ancestor-broker":
        return fail("launch-broker-retired", 76, child_spawned="0")
    # The whole tree runs on the release this launch resolves (OPERATIONS §5.9a): one real
    # path reaches the child environment, its allow rules and the row, whichever spelling
    # (`hearting run`, a `current` pointer) the caller exported.
    args.agent_home = sealed_launch_home(resolve_agent_home())
    bind_parent_completion_delivery(args)
    worktree = Path(args.worktree)
    if not worktree.is_dir():
        return fail("worktree-not-found", 66, worktree=args.worktree)
    if subprocess.run(["git", "-C", args.worktree, "rev-parse", "--is-inside-work-tree"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        return fail("not-a-git-worktree", 65, worktree=args.worktree)
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
    args.eligibility_probe = "-"
    bind_internal_eligibility_probe(args)
    try:
        validate_nested_eligibility(
            dispatch_depth=args.dispatch_depth, action=action, parent_harness=args.parent_harness,
            parent_transport=args.parent_transport, parent_sandbox=args.parent_sandbox,
            child_harness="codex", launch_authority=args.launch_authority,
            status=args.nested_eligibility, source=args.eligibility_source,
        )
    except DispatchContractError as e:
        return fail(
            e.reason, 69, detail=e.detail,
            parent_harness=args.parent_harness or "-",
            parent_transport=args.parent_transport or "-",
            parent_sandbox=args.parent_sandbox or "-",
            child_harness="codex",
            launch_authority=args.launch_authority,
            nested_eligibility=args.nested_eligibility,
            eligibility_source=args.eligibility_source or "-",
            eligibility_failure_class=args.eligibility_failure_class or "-",
            eligibility_probe=args.eligibility_probe,
        )
    try:
        args.owner_route_binding = binding_from_environment(
            dict(os.environ),
            worktree=args.worktree,
            capability=args.capability,
            capability_mode=args.capability_mode,
            intensity=args.intensity,
            harness="codex",
        )
        if args.owner_route_binding:
            failure_fields = owner_binding_tuple_failure_fields(
                dispatch_depth=args.dispatch_depth, worker_type=args.worker_type, route_file=args.route_file)
            if failure_fields:
                return fail("owner-route-binding-tuple-invalid", 65, child_spawned="0", **failure_fields)
    except OwnerRouteBindingError as exc:
        return fail(str(exc), 65, child_spawned="0")
    rc = validate_route_record(args)
    if rc != 0:
        return rc
    try:
        apply_gpu_execution_sandbox(args)
    except (DispatchContractError, OSError, ValueError) as exc:
        return fail(getattr(exc, "reason", "gpu-execution-selection-invalid"), 65,
                    detail=str(exc), child_spawned="0")
    args.replica_batch_expectation = None
    if action in {"register", "start"}:
        try:
            args.replica_batch_expectation = replica_batch_expectation(
                args.route_file,
                args.route_node,
                action,
                attempt_id=args.attempt_id or "",
                parent_attempt_id=args.parent_attempt_id or "",
                harness="codex",
                fallback_hop=args.fallback_hop,
                fallback_ordinal=args.fallback_ordinal,
            )
        except DispatchContractError as exc:
            return fail(exc.reason, 65, detail=exc.detail, child_spawned="0")
        if (
            args.replica_batch_expectation is not None
            and not os.environ.get(GOVERNOR_RESERVATION_ENV)
        ):
            return fail(
                "parallel-group-batch-required",
                65,
                detail="parallel-group start requires dispatch-batch admission",
                child_spawned="0",
            )
    try:
        attempt_policy = headless_attempt_policy(
            route_file=args.route_file, route_node=args.route_node,
            intensity=args.intensity, harness="codex",
            dispatch_depth=args.dispatch_depth, parent_slug=args.parent_slug,
            execution_surface=args.execution_surface,
            registered_worker=bool(args.registered_worker),
            fallback_hop=args.fallback_hop,
            fallback_ordinal=args.fallback_ordinal,
            parent_harness=args.parent_harness,
            parent_transport=args.parent_transport,
            parent_sandbox=args.parent_sandbox,
            launch_authority=args.launch_authority,
        )
    except DispatchContractError as e:
        return fail(e.reason, 65, detail=e.detail, child_spawned="0")
    args.fallback_hop = str(attempt_policy["fallback_hop"])
    args.fallback_ordinal = int(attempt_policy["fallback_ordinal"])
    args.quick_attempt = bool(attempt_policy["quick"])
    args.quick_attempt_limit = attempt_policy["terminal_attempt_limit"]
    args.replacement_attempt_limit = attempt_policy["replacement_attempt_limit"]
    args.replacement_notes = attempt_policy["replacement_notes"]
    try:
        args.resolved_model_settings = resolve_model_settings(args)
        from model_profile import selection_receipt, ModelProfileError
        try:
            args.profile_selection_receipt = selection_receipt(args)
        except (ModelProfileError, OSError, ValueError) as exc:
            return fail(getattr(exc, "reason", "profile-selection-invalid"), 65, child_spawned="0")
    except ModelSelectionError as e:
        fields = {"detail": str(e)}
        if args.model_role:
            fields["model_role"] = args.model_role
        return fail(e.reason, 64, **fields)
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
    try:
        prepare_review_output_request(args)
    except ProducerError as exc:
        return fail(
            exc.code, 65, detail=exc.detail, registry_mutation="0",
            child_spawned="0",
        )
    try:
        bind_stage_session(args, artifact_root=args.artifact_root, action=action)
    except DispatchContractError as e:
        return fail(e.reason, 65, detail=e.detail, child_spawned="0")
    refusal = route_authority.completion_gate(
        args, action, agent_home, jobs, gate=completion_marker_gate,
        before=(lambda: owner_frame_launch_gate(args.owner_route_binding, action, agent_home, jobs),))
    if refusal:
        reason, code, fields = refusal
        return fail(reason, code, **fields)
    args.parent_binding = None
    if args.dispatch_depth == 2 and action in ("register", "start"):
        try:
            repo = subprocess.check_output(
                ["git", "-C", args.worktree, "rev-parse", "--show-toplevel"],
                text=True,
            ).strip()
            args.parent_binding = resolve_live_parent_attempt(
                jobs,
                parent_slug=args.parent_slug or "",
                repo=repo,
                worktree=(
                    parent_lookup_worktree(
                        args.worktree, args.route_file, subsession=True,
                        parent_attempt_id=args.parent_attempt_id,
                    )
                    if getattr(args, "subsession_id", None) else args.worktree
                ),
                expected_attempt_id=args.parent_attempt_id,
                expected_harness=args.parent_harness,
                expected_transport=args.parent_transport,
                expected_sandbox=args.parent_sandbox,
            )
            args.parent_attempt_id = args.parent_binding.attempt_id
        except (DispatchContractError, subprocess.SubprocessError) as e:
            reason = e.reason if isinstance(e, DispatchContractError) else "parent-repo-unreadable"
            detail = e.detail if isinstance(e, DispatchContractError) else str(e)
            return fail(reason, 73, detail=detail, child_spawned="0")
    if action == "start" and args.replica_batch_expectation is not None:
        try:
            args.replica_batch_expectation = replica_batch_expectation(
                args.route_file,
                args.route_node,
                action,
                attempt_id=args.attempt_id,
                parent_attempt_id=args.parent_attempt_id or "",
                harness="codex",
                fallback_hop=args.fallback_hop,
                fallback_ordinal=args.fallback_ordinal,
            )
        except DispatchContractError as exc:
            return fail(exc.reason, 65, detail=exc.detail, child_spawned="0")
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
    if action == "start" and args.replica_batch_expectation is not None:
        try:
            args.replica_batch_expectation = replica_batch_expectation(
                args.route_file,
                args.route_node,
                action,
                attempt_id=args.attempt_id,
                parent_attempt_id=args.parent_attempt_id or "",
                harness="codex",
                fallback_hop=args.fallback_hop,
                fallback_ordinal=args.fallback_ordinal,
                assignment_sha256=assignment_sha256,
            )
        except DispatchContractError as exc:
            return fail(exc.reason, 65, detail=exc.detail, child_spawned="0")
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

    governor = ROOT / "utilities" / "model-worker-governor.py"
    try:
        governor_root = resolve_model_governor_root(args.artifact_root)
    except DispatchContractError as exc:
        return fail(exc.reason, 73, detail=exc.detail, child_spawned="0")
    reservation_token = ""
    args.replica_batch_reservation = {}
    if action in ("register", "start"):
        try:
            assert_register_mkdir_containment(args, prompt_path.parent, log_path.parent)
        except DispatchContractError as e:
            return fail(e.reason, 73, detail=e.detail, child_spawned="0")
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if action == "start":
            try:
                reservation_token, args.replica_batch_reservation = reserve_governor_token(
                    governor,
                    governor_root,
                    "dispatch",
                    provided_token=os.environ.get(GOVERNOR_RESERVATION_ENV, ""),
                    expected_reservation=args.replica_batch_expectation,
                )
            except DispatchContractError as exc:
                return fail(exc.reason, 75, detail=exc.detail, child_spawned="0",
                            **governor_refusal_fields(exc))
        try:
            args.attempt_claimed = append_job(jobs, args)
            if action == "start" and not args.attempt_claimed:
                args.attempt_claimed = attempt_launch_is_available(
                    jobs, args.attempt_id
                )
            parent_completion.validate_registered_delivery(args, jobs, read=registered_parent_delivery)
        except DispatchContractError as e:
            cancel_governor_reservation(governor, governor_root, reservation_token)
            return fail(e.reason, 73, detail=e.detail, child_spawned="0")
        except ManagedDispatchError as e:
            cancel_governor_reservation(governor, governor_root, reservation_token)
            return fail(str(e), 73, child_spawned="0")
        if args.attempt_claimed:
            initialize_supervised_owner_input(args, jobs)
            try:
                prompt_path.write_text(prompt_text, encoding="utf-8")
            except OSError as exc:
                annotate_attempt_row(
                    jobs, args.attempt_id, {"launch_outcome": "never-launched"}
                )
                cancel_governor_reservation(governor, governor_root, reservation_token)
                close_job_row(
                    jobs, args.slug, args.worktree,
                    "prompt-materialization-failed", "", args.attempt_id,
                )
                return fail(
                    "prompt-materialization-failed", 73,
                    detail=str(exc), child_spawned="0",
                )
            if action == "start":
                try:
                    bind_owner_launch(args, jobs)
                except ProducerError as exc:
                    annotate_attempt_row(jobs, args.attempt_id, {"launch_outcome": "never-launched"})
                    cancel_governor_reservation(governor, governor_root, reservation_token)
                    close_job_row(jobs, args.slug, args.worktree, "producer-binding-failed", "", args.attempt_id)
                    return fail(exc.code, 73, detail=exc.detail, child_spawned="0")
    else:
        args.attempt_claimed = False
    if action == "start" and not args.attempt_claimed:
        cancel_governor_reservation(governor, governor_root, reservation_token)
    if action == "start" and args.attempt_claimed:
        dispatch_env = {
            **{key: value for key, value in os.environ.items() if not key.startswith("AGENT_DISPATCH_BROKER_")},
            "AGENT_SESSION_ROLE": "worker",
            "AGENT_DISPATCH_CHILD": "1",
            "AGENT_DISPATCH_DEPTH": str(args.dispatch_depth),
            "AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION": "2",
            "AGENT_DISPATCH_TRANSPORT": "headless",
            "AGENT_DISPATCH_EXECUTION_SURFACE": args.execution_surface,
            "AGENT_DISPATCH_REGISTERED_WORKER": str(int(bool(args.registered_worker))),
            "AGENT_DISPATCH_FALLBACK_HOP": args.fallback_hop,
            "AGENT_DISPATCH_INTENSITY": args.intensity,
            "AGENT_DISPATCH_CAPABILITY_MODE": args.capability_mode,
            "AGENT_DISPATCH_SELF_SLUG": args.slug,
            "AGENT_DISPATCH_PARENT_SLUG": args.parent_slug or "",
            "AGENT_DISPATCH_ATTEMPT_ID": args.attempt_id,
            "AGENT_DISPATCH_PARENT_ATTEMPT_ID": args.parent_attempt_id or "",
            "AGENT_DISPATCH_PARENT_SESSION_ID": args.parent_session_id or "",
            "AGENT_DISPATCH_PARENT_CWD": (_effective_parent_cwd(args) if (args.parent_slug or args.parent_session_id) else ""),
            "AGENT_DISPATCH_WORKER_TYPE": args.worker_type,
            "AGENT_DISPATCH_ASSIGNED_CONTRACT": args.assigned_contract,
            "AGENT_DISPATCH_OWNER": args.capability_owner or "",
            "AGENT_DISPATCH_OWNER_HARNESS": args.owner_harness or "",
            "AGENT_ARTIFACT_ROOT": args.artifact_root,
            "AGENT_DISPATCH_WORKTREE": (
                args.review_output_binding["worktree"]
                if args.review_output_binding else args.worktree
            ),
            "AGENT_REVIEW_CYCLE_ID": (
                args.review_output_binding["cycle_id"]
                if args.review_output_binding else ""
            ),
            "AGENT_REVIEW_PRODUCER_ID": (
                args.review_output_binding["producer_id"]
                if args.review_output_binding else ""
            ),
            "AGENT_REVIEW_OUTPUT": (
                args.review_output_binding["output_path"]
                if args.review_output_binding else ""
            ),
            # W7C producer lifecycle: the owner's open cycle (issued by
            # `artifact_producer.py begin` before the first write) is passed
            # through unchanged so stage workers write into the same
            # `campaigns/<camp>/cycles/<cyc>/artifacts/` and never issue a
            # second lineage.
            **artifact_cycle_environment(os.environ),
            "REPORT_BUNDLE_ROOT": str(args.report_bundle_root or ""),
            "AGENT_ROUTE_FILE": (
                args.route_file
                or (args.owner_route_binding.route_file if args.owner_route_binding else "")
            ),
            "AGENT_ROUTE_ID": (
                args.route_id
                or (args.owner_route_binding.route_id if args.owner_route_binding else "")
            ),
            "AGENT_ROUTE_NODE": args.route_node or "",
            "AGENT_MODEL_GOVERNOR_ROOT": str(governor_root),
            GOVERNOR_RESERVATION_ENV: reservation_token,
            "AGENT_HOME": str(args.agent_home),
            "AGENT_DISPATCH_JOBS": str(jobs),
            # A worker inherits AGENT_HOME at the managed release; keep its
            # bytecode out of that immutable tree (defect Q pairing).
            **bytecode_cache_env(),
            **parent_completion.worker_runtime_identity("codex"),
            "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
            "AGENT_DISPATCH_CURRENT_SANDBOX": effective_runtime_sandbox(args),
            "AGENT_DISPATCH_COMPLETION_MODE": (
                "supervised"
                if args.resolved_completion_delivery == "app-server-supervised"
                else "poll"
            ),
            **stage_session_environment(args),
        }
        if args.execution_access_grant is not None:
            dispatch_env["AGENT_DISPATCH_EXECUTION_ACCESS_FILE"] = str(args.execution_access_grant.source_path)
        if args.worker_role:
            dispatch_env["AGENT_DISPATCH_WORKER_ROLE"] = args.worker_role
        else:
            dispatch_env.pop("AGENT_DISPATCH_WORKER_ROLE", None)
        if args.worker_mode:
            dispatch_env["AGENT_DISPATCH_WORKER_MODE"] = args.worker_mode
        else:
            dispatch_env.pop("AGENT_DISPATCH_WORKER_MODE", None)
        if args.unit:
            dispatch_env["AGENT_DISPATCH_UNIT"] = args.unit
        else:
            dispatch_env.pop("AGENT_DISPATCH_UNIT", None)
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
        launch_parent_completion_sidecar(args, jobs)
        if args.managed_sidecar_state == "launch-failed":
            annotate_attempt_row(
                jobs, args.attempt_id, {"launch_outcome": "never-launched"}
            )
            cancel_governor_reservation(
                governor, governor_root, reservation_token
            )
            close_job_row(
                jobs,
                args.slug,
                args.worktree,
                "managed-sidecar-launch-failed",
                "",
                args.attempt_id,
            )
            return fail(
                "managed-sidecar-launch-failed",
                75,
                detail=args.managed_sidecar_reason,
                attempt_id=args.attempt_id,
                child_spawned="0",
            )
        fence_failure_read_fd, fence_failure_write_fd = os.pipe()
        args.watchdog_budget = begin_finite_watchdog(args.foreground_timeout)
        def spawn_worker(gate_fd: int) -> subprocess.Popen:
            fence_command = [
                sys.executable, str(ROOT / "utilities" / "launch-fence.py"),
                "--parent-pid", str(os.getpid()),
                "--gate-fd", str(gate_fd),
                "--failure-fd", str(fence_failure_write_fd),
                "--jobs", str(jobs), "--attempt-id", args.attempt_id,
            ]
            if args.route_file:
                fence_command.extend(
                    [
                        "--route-file", args.route_file,
                        "--launch-phase", args.action,
                    ]
                )
            fence_command.extend(
                [
                    "--post-release-parent-death-signal",
                    "kill" if args.launch_lifecycle == FOREGROUND_SCOPED else "none",
                    "--",
                    sys.executable, str(governor), "--root", str(governor_root),
                    "run", "--class", "dispatch", "--", "sh", "-c", command,
                ]
            )
            if args.review_output and args.launch_lifecycle == DETACHED:
                try:
                    args.review_watchdog_handle = launch_review_watchdog(
                        fence_command, gate_fd=gate_fd, budget=args.watchdog_budget,
                        attempt_id=args.attempt_id,
                        nonce=args.review_governed_lease_nonce,
                        failure_fd=fence_failure_write_fd, env=dispatch_env,
                        lease_release_spec={
                            "root": args.artifact_root,
                            "cycle_id": args.review_output_binding["cycle_id"],
                            "attempt_id": args.attempt_id,
                        }, jobs=jobs,
                    )
                finally:
                    try:
                        os.close(fence_failure_write_fd)
                    except OSError:
                        pass
                return args.review_watchdog_handle.process
            try:
                return subprocess.Popen(
                    fence_command,
                    start_new_session=True,
                    env=dispatch_env,
                    pass_fds=(gate_fd, fence_failure_write_fd),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            finally:
                try:
                    os.close(fence_failure_write_fd)
                except OSError:
                    pass
        launch_metadata = {
            **args.launch_lifecycle_resolution.metadata(),
            "runtime_sandbox": effective_runtime_sandbox(args),
            "runtime_home": dispatch_env["CODEX_HOME"],
        }
        from dispatch_capacity_evidence import launch_scope
        launch_metadata.update(launch_scope("codex", dispatch_env))
        grant_route_id, grant_route_hash = args.route_id, args.route_hash
        if not grant_route_id and not grant_route_hash and args.owner_route_binding:
            grant_route_id = args.owner_route_binding.route_id
            grant_route_hash = args.owner_route_binding.route_hash
        if grant_route_id and grant_route_hash:
            effective_path, effective_sha256 = publish_effective_grant(
                jobs=jobs, attempt_id=args.attempt_id, route_id=grant_route_id,
                route_hash=grant_route_hash,
                runtime=("codex-app-server" if args.resolved_completion_delivery == "app-server-supervised" else "codex-exec"),
                sandbox=effective_runtime_sandbox(args), grant=args.execution_access_grant,
                default_writable_roots=default_roots,
                network_allowed=args.nested_headless_network,
                execution_selection=args.gpu_execution_selection,
            )
            launch_metadata["execution_access_effective_file"] = str(effective_path)
            launch_metadata["execution_access_effective_sha256"] = effective_sha256
        if args.dispatch_depth >= 2 and os.environ.get("AGENT_DISPATCH_CHILD") == "1":
            launch_metadata["pid_scope"] = "namespace-local"
        try:
            proc, launch_metadata = spawn_claimed_attempt(
                jobs,
                args.attempt_id,
                parent_binding=args.parent_binding,
                spawn=spawn_worker,
                launch_metadata=launch_metadata,
                preclaim=getattr(args, "launch_preclaim", None),
                pre_release=lambda identity: attach_summary_owner(
                    args, log_path, prompt_path, identity
                ),
                post_claim=lambda identity: acquire_review_lease_after_claim(args, jobs, identity),
            )
        except DispatchContractError as exc:
            for fd in (fence_failure_read_fd, fence_failure_write_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
            if exc.reason == "attempt-launch-already-claimed":
                cancel_governor_reservation(
                    governor, governor_root, reservation_token
                )
                print("check=ok")
                print("status=start")
                print(f"attempt_id={args.attempt_id}")
                print("duplicate_attempt=1")
                existing_state, existing_reason = existing_attempt_launch_state(
                    jobs, args.attempt_id
                )
                print(f"launch_state={existing_state}")
                print("registered=0")
                print("started=0")
                print("child_spawned=0")
                print(f"reason={existing_reason}")
                if existing_state in EXISTING_ATTEMPT_NOTES:
                    print(f"note={EXISTING_ATTEMPT_NOTES[existing_state]}")
                return 0
            reason = (
                "parent-exited"
                if exc.reason.startswith("parent-attempt-")
                else (
                    "summary-owner-launch-failed"
                    if exc.reason.startswith("attempt-pre-release-")
                    else "launch-error"
                )
            )
            outcome = adapter_launch_failure_outcome(jobs, args.attempt_id, exc.reason)
            annotate_attempt_row(jobs, args.attempt_id, {"launch_outcome": outcome})
            cancel_governor_reservation(governor, governor_root, reservation_token)
            close_job_row(jobs, args.slug, args.worktree, reason, "", args.attempt_id)
            return fail(
                exc.reason, 73, detail=exc.detail,
                attempt_id=args.attempt_id,
                child_spawned="1" if outcome == "post-release-failed" else "0",
            )
        except OSError as exc:
            for fd in (fence_failure_read_fd, fence_failure_write_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
            annotate_attempt_row(
                jobs, args.attempt_id, {"launch_outcome": "never-launched"}
            )
            cancel_governor_reservation(governor, governor_root, reservation_token)
            close_job_row(jobs, args.slug, args.worktree, "launch-error", "", args.attempt_id)
            return fail("child-launch-failed", 70, detail=str(exc), attempt_id=args.attempt_id)
        try:
            args.governor_reservation = wait_governor_reservation_claim(
                governor,
                governor_root,
                reservation_token,
                proc,
                expected_reservation=args.replica_batch_expectation,
                watchdog_receipt=(args.review_watchdog_handle.receipt
                                  if getattr(args, "review_watchdog_handle", None) else None),
            )
        except DispatchContractError as exc:
            fence_failure, fence_released = read_launch_fence_failure(
                fence_failure_read_fd
            )
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5.0 if getattr(args, "review_watchdog_handle", None) else 0.5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5.0 if getattr(args, "review_watchdog_handle", None) else 0.5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
            cancel_governor_reservation(governor, governor_root, reservation_token)
            if fence_failure is not None:
                reason = str(fence_failure["reason"])
                # The fence knows exactly which sealed root disagreed with the
                # live one. This is a diagnostic on a path that is ALREADY
                # failing, so it can never be allowed to prevent the row from
                # closing: a raise here would leak the attempt open with a sealed
                # launch_home, which then pins its release against pruning.
                annotation = {"launch_outcome": "never-launched"}
                try:
                    annotation.update(
                        launch_mismatch_annotation(str(fence_failure["detail"]))
                    )
                    annotate_attempt_row(jobs, args.attempt_id, annotation)
                except DispatchContractError:
                    annotate_attempt_row(
                        jobs, args.attempt_id, {"launch_outcome": "never-launched"}
                    )
                close_job_row(
                    jobs, args.slug, args.worktree, reason, "", args.attempt_id,
                )
                return fail(
                    reason, 73, detail=str(fence_failure["detail"]),
                    attempt_id=args.attempt_id, registered="0", started="0",
                    child_spawned="0",
                )
            post_exit = deterministic_post_exit_outcome(
                proc, fence_released=fence_released,
            )
            if post_exit.launch_outcome:
                annotate_attempt_row(
                    jobs,
                    args.attempt_id,
                    {
                        key: value
                        for key, value in {
                            "launch_outcome": post_exit.launch_outcome,
                            "group_reap_proof": post_exit.group_reap_proof,
                            "group_reap_pgid": post_exit.group_reap_pgid,
                        }.items()
                        if value
                    },
                )
            close_job_row(
                jobs, args.slug, args.worktree,
                "governor-reservation-transfer", "", args.attempt_id,
            )
            return fail(
                exc.reason, 75, detail=exc.detail,
                attempt_id=args.attempt_id, child_spawned="1",
            )
        read_launch_fence_failure(fence_failure_read_fd)
        foreground_review_handoff = (
            getattr(args, "launch_lifecycle", DETACHED) == FOREGROUND_SCOPED
            and getattr(args, "dispatch_depth", None) == 1
            and getattr(args, "worker_type", None) == "review"
            and getattr(args, "execution_surface", None) == "registered-headless"
            and bool(getattr(args, "registered_worker", False))
            and not any(foreground_review_launch_identity(args).values())
        )
        start_ticks = launch_metadata.get("pid_start", "")
        if (args.dispatch_depth == 1 and args.worker_type == "owner"
                and args.launch_lifecycle == DETACHED):
            try:
                watcher_pid = launch_orphan_watch(
                    jobs, agent_home, args.attempt_id, proc.pid, start_ticks or "")
            except DispatchContractError as exc:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                close_job_row(
                    jobs, args.slug, args.worktree,
                    "orphan-watch-launch-error", "", args.attempt_id)
                return fail(exc.reason, 70, detail=exc.detail, child_spawned="0")
            annotate_job_row(
                jobs, args.slug, args.worktree,
                f"orphan_watch=post-exit,orphan_watch_pid={watcher_pid}",
                args.attempt_id,
            )
        if args.launch_lifecycle == DETACHED:
            try:
                reap_watch_pid = launch_reap_watch(
                    jobs,
                    args.attempt_id,
                    proc.pid,
                    start_ticks or "",
                    int(launch_metadata.get("pgid", "0")),
                )
            except (DispatchContractError, ValueError) as exc:
                reason = getattr(exc, "reason", "reap-watch-identity-invalid")
                detail = getattr(exc, "detail", str(exc))
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                close_job_row(
                    jobs, args.slug, args.worktree,
                    "reap-watch-launch-error", "", args.attempt_id,
                )
                return fail(reason, 70, detail=detail, child_spawned="0")
            annotate_attempt_row(
                jobs,
                args.attempt_id,
                {"reap_watch": "post-exit", "reap_watch_pid": str(reap_watch_pid)},
            )
        args.child_pid = proc.pid
        args.child_pid_start = start_ticks
        args.launch_heartbeat = seed_launch_heartbeat(args, jobs, proc.pid, start_ticks)
        if args.launch_lifecycle == FOREGROUND_SCOPED and foreground_review_handoff:
            binding = args.parent_binding
            try:
                outcome = wait_foreground(
                    proc, args.foreground_timeout,
                    watchdog_budget=args.watchdog_budget,
                    parent_pid=binding.observed_pid if binding else None,
                    parent_pid_start=binding.observed_pid_start if binding else None,
                    parent_is_live=(
                        (lambda: parent_attempt_binding_is_live(jobs, binding))
                        if binding else None
                    ),
                )
                foreground_seal = seal_foreground_result(
                    jobs, args.attempt_id, proc.pid, start_ticks or "",
                    int(launch_metadata.get("pgid", "0")),
                    exit_code=outcome.exit_code, failure=outcome.failure,
                    group_empty=outcome.group_empty,
                )
                reap_watch_pid = launch_reap_watch(
                    jobs, args.attempt_id, proc.pid, start_ticks or "",
                    int(launch_metadata.get("pgid", "0")),
                    foreground_seal=foreground_seal,
                )
            except (DispatchContractError, ValueError) as exc:
                reason = getattr(exc, "reason", "foreground-outcome-seal-error")
                detail = getattr(exc, "detail", str(exc))
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                close_job_row(jobs, args.slug, args.worktree, reason, "", args.attempt_id)
                return fail(reason, 70, detail=detail, child_spawned="0")
            annotate_attempt_row(
                jobs, args.attempt_id,
                {"reap_watch": "post-exit", "reap_watch_pid": str(reap_watch_pid)},
            )
            args.worker_exit = outcome.exit_code
            args.worker_failure = outcome.failure
        elif args.launch_lifecycle == FOREGROUND_SCOPED:
            binding = args.parent_binding
            outcome = wait_foreground(
                proc,
                args.foreground_timeout,
                parent_pid=binding.observed_pid if binding else None,
                parent_pid_start=binding.observed_pid_start if binding else None,
                parent_is_live=(
                    (lambda: parent_attempt_binding_is_live(jobs, binding))
                    if binding
                    else None
                ),
                watchdog_budget=args.watchdog_budget,
            )
            annotate_attempt_row(
                jobs,
                args.attempt_id,
                (
                    {
                        "launch_outcome": "governed-process-reaped",
                        "group_reap_proof": GROUP_REAP_PROOF,
                        "group_reap_pgid": str(proc.pid),
                    }
                    if outcome.group_empty
                    else {"launch_outcome": "governed-process-reap-unverified"}
                ),
            )
            args.worker_exit = outcome.exit_code
            args.worker_failure = outcome.failure
            settled = settle_foreground_exit(
                jobs, args.attempt_id, log_path, outcome, worktree=args.worktree,
                artifact_root=args.artifact_root, worker_type=args.worker_type,
                legacy_close=lambda failure: close_job_row(
                    jobs, args.slug, args.worktree, failure, "", args.attempt_id),
            )
            terminal = settled["inspection"]
            args.terminal_inspection = terminal
            args.terminal_verdict = settled["verdict"]
            args.worker_failure = settled["worker_failure"]
            terminal_closed = settled["closed"]
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
        else:
            # SD-15: detached launches retain the short early-death watch.
            death = watch_early_death(proc, log_path, args.early_exit_watch)
            if death:
                reason, reset = death
                close_job_row(jobs, args.slug, args.worktree, reason, reset, args.attempt_id)
                if reason != "capacity":
                    write_reset_cache(agent_home, "codex", reason, reset, args.jobs_path)
                args.early_death = (reason, reset)

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
    print(f"status={action}")
    print(f"worktree={args.worktree}")
    print(f"artifact_root={args.artifact_root}")
    print("artifact_write_scope=canonical-only")
    print(f"slug={args.slug}")
    print(f"capability={args.capability}")
    print(f"capability_mode={args.capability_mode}")
    print(f"worker_mode={args.worker_mode or '-'}")
    print(f"qa={args.qa}")
    print(f"intensity={args.intensity}")
    print(f"dispatch_depth={args.dispatch_depth}")
    print(f"eligibility_probe={getattr(args, 'eligibility_probe', None) or '-'}")
    print(f"parent={args.parent_slug or '-'}")
    print(f"parent_attempt_id={args.parent_binding.attempt_id if args.parent_binding else '-'}")
    print(f"parent_session_id={args.parent_session_id or '-'}")
    print(f"worker_role={args.worker_role or '-'}")
    print(f"worker_type={args.worker_type}")
    print(f"assigned_contract={args.assigned_contract}")
    print(f"unit={args.unit or '-'}")
    print(f"owner={args.capability_owner or '-'}")
    print(f"owner_harness={args.owner_harness or '-'}")
    print(f"route_file={args.route_file or '-'}")
    print(f"route_validation={getattr(args, 'route_validation', None) or '-'}")
    settings = args.resolved_model_settings
    print(f"model_source={settings['source']}")
    print(f"model_pin_status={settings.get('pin_status', 'none')}")
    if getattr(args, 'explicit_adapter', None):
        print(f"explicit_adapter={args.explicit_adapter}")
    if settings.get("pin_model"):
        print(f"model_pin={settings['pin_model']}")
    print(f"model_role={settings['role']}")
    print(f"model_profile={settings['profile']}")
    print(f"model_tier={settings['tier']}")
    _config_source, _config_reason = _model_config_state()
    print(f"model_config_source={_config_source}")
    print(f"model_config_reason={_config_reason}")
    print(f"profile_granularity={settings['granularity']}")
    print(f"main_session_only_policy={_main_session_only_policy_state()}")
    for key, value in sorted(getattr(args, "profile_selection_receipt", {}).items()):
        print(f"{key}={value}")
    print(f"model={settings['model']}")
    print(f"reasoning={settings['reasoning']}")
    print(f"approval={args.approval}")
    leg_class, auxiliary_check = _route_node_leg_fields(args)
    print(f"leg_class={leg_class}")
    print(f"auxiliary_check={auxiliary_check}")
    print(f"parent_cross={os.environ.get('AGENT_DISPATCH_PARENT_CROSS', '-')}")
    print(f"sole_gate={os.environ.get('AGENT_DISPATCH_SOLE_GATE', '-')}")
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
    print(f"prompt_source={prompt_source}")
    print(f"prompt_file={prompt_path}")
    print(f"log_file={log_path}")
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
