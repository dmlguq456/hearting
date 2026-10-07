#!/usr/bin/env python3
"""OpenCode headless dispatch registration/launch wrapper."""

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
from dispatch_contract import (
    _atomic_registry_replace,
    ensure_terminal_claim_absent,
    STANDARD_PLUS_INTENSITIES,  # noqa: E402
    workflow_completion_receipt,
    DispatchContractError,
    foreground_review_launch_identity,
    bytecode_cache_env,
    GROUP_REAP_PROOF,
    GOVERNOR_RESERVATION_ENV,
    REPLICA_RESERVATION_ROW_KEYS,
    anchored_capacity_failure,
    annotate_attempt_row,
    adapter_launch_failure_outcome,
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
    dispatch_state_root,
    dispatch_state_roots,
    PRELAUNCH_PROCESS_BLOCK_REASONS,
    ROUTE_STATE_REFUSAL_REASONS,
    route_state_next_action,
    ensure_global_registry_writable,
    headless_attempt_policy,
    launch_orphan_watch,
    launch_reap_watch,
    seal_foreground_result,
    new_attempt_id,
    parent_attempt_binding_is_live,
    parse_registry_metadata,
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
    SUPERVISOR_LEASE_KIND,
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
from dispatch_summary import launch_summary_owner  # noqa: E402
from artifact_producer import (  # noqa: E402
    ProducerError,
    bind_owner_launch,
    prepare_review_output_binding,
    review_lease_acquire,
)
from dispatch_completion_join import materialize_after_terminal_close  # noqa: E402
from foreground_terminal import settle_foreground_exit  # noqa: E402
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
    assigned_contract,
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
import commit_policy  # noqa: E402
from model_config import (  # noqa: E402
    ModelConfigError, headless_model_refusal, inheritance_refusal, main_session_only_models,
    main_session_only_state, resolve_config, restricted_model,
)
from model_profile import (  # noqa: E402
    TOP_PROFILE,
    ModelProfileError,
    require_top_route,
    resolve_runtime_profile,
    route_selection_pin,
    validate_registered_profile,
)
from codex_managed_dispatch import (
    MANAGED_PARENT_DELIVERY, ManagedDispatchError, probe_managed_codex_parent,
    registered_parent_delivery,
)
import dispatch_parent_completion as parent_completion
from codex_queue_dispatch import launch_codex_queue_completion_sidecar
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
    bind_runtime_parent,
)
INTENSITY_LEVELS = {"direct", "quick", "standard", "strong", "thorough", "adversarial"}
# Verification rigor is derived from intensity via resolve_qa
# (dispatch_mode_contract.py, the single qa/intensity SoT — CONVENTIONS §1.1).
# `--qa` is no longer a user-facing axis; optional, derived from --intensity
# when omitted. The jobs.log `qa=` field is retained (derived value) for
# fleet-collector compatibility.

# SD-15 (OPERATIONS §5.10 ⑨): limit/auth/capacity deaths are classified by the one shared
# table (route_authority), the same at launch and in liveness for every harness.
from route_authority import DEATH_PATTERNS, scan_anchored_death, scan_death  # noqa: E402,F401
from route_authority import ANSI_RE as _ANSI_RE  # noqa: E402,F401


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
        default=os.environ.get("AGENT_DISPATCH_OWNER_HARNESS") or "opencode",
    )
    p.add_argument("--agent", default="build")
    p.add_argument("--model-role", default=os.environ.get("OPENCODE_DISPATCH_MODEL_ROLE"))
    p.add_argument("--model-profile", default=os.environ.get("OPENCODE_DISPATCH_MODEL_PROFILE"))
    p.add_argument("--model", default=os.environ.get("OPENCODE_DISPATCH_MODEL"))
    p.add_argument("--variant", default=os.environ.get("OPENCODE_DISPATCH_VARIANT"))
    p.add_argument(
        "--inherit-model-settings",
        action="store_true",
        help="do not override model/variant; inherit the active OpenCode config for this dispatch",
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
    p.add_argument("--parent-harness", default=parent_completion.default_parent_harness("opencode"))
    p.add_argument(
        "--allow-unmanaged-parent-poll", action="store_true",
        help="operator-only recovery: permit explicit bounded polling for an unmanaged Codex parent",
    )
    p.add_argument("--parent-transport", default=os.environ.get("AGENT_DISPATCH_CURRENT_TRANSPORT") or "unknown")
    p.add_argument("--parent-sandbox", default=os.environ.get("AGENT_DISPATCH_CURRENT_SANDBOX") or "unknown")
    # default None (not "unknown"): an explicitly supplied `--nested-eligibility
    # unknown` must stay distinguishable from an absent flag — explicit evidence,
    # even unknown, is never overwritten by the internal probe.
    p.add_argument("--nested-eligibility", choices=("supported", "unsupported", "unknown"), default=None)
    p.add_argument("--eligibility-source", default="")
    p.add_argument("--eligibility-failure-class", default="")
    p.add_argument("--log-dir")
    p.add_argument("--launch-lifecycle", choices=LIFECYCLES, default=DETACHED)
    p.add_argument(
        "--foreground-timeout",
        type=float,
        default=float(os.environ.get("OPENCODE_DISPATCH_FOREGROUND_TIMEOUT", "3600")),
        help="maximum child lifetime for foreground-scoped launch",
    )
    p.add_argument(
        "--early-exit-watch",
        type=float,
        default=float(os.environ.get("OPENCODE_DISPATCH_EARLY_EXIT_WATCH", "8")),
        help="SD-15: seconds to watch a just-launched child for a limit/auth early death "
        "(0 disables). On detection the jobs.log row is closed done,note=dead-<reason>. "
        "Note: OpenCode may hang on limit (#8203) rather than exit; hangs are caught by "
        "dispatch-liveness's log scan, not this watch.",
    )
    add_stage_session_arguments(p)
    return p


# One copy, shared by the three wrappers (`route_authority`); the name stays for readers.
completion_gate_fail_fields = route_authority.completion_gate_fail_fields


class ModelSelectionError(ValueError):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason


def role_map(role: str) -> dict[str, str]:
    result = subprocess.run(
        [str(ROOT / "adapters" / "opencode" / "bin" / "preflight.sh"), "role", role],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ModelSelectionError("invalid-dispatch-model-role", detail or f"preflight role lookup failed for {role}")
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    return fields


def _model_policy() -> dict[str, str]:
    try:
        values, _receipt = resolve_config("opencode", source_root=ROOT)
    except ModelConfigError as exc:
        raise ModelSelectionError(
            "dispatch-model-policy-unavailable", str(exc)
        ) from exc
    return values


def _main_session_only_model(model: str) -> bool:
    return restricted_model(model, main_session_only_models(_model_policy()))


def _main_session_only_policy_state() -> str:
    try:
        return main_session_only_state(_model_policy())
    except ModelSelectionError:
        return "unavailable"


def _require_headless_model(model: str, source: str) -> None:
    refusal = headless_model_refusal(_model_policy(), model, source)
    if refusal:
        raise ModelSelectionError(*refusal)


def _model_config_state() -> tuple[str, str]:
    return WRAPPER_COMMON.model_config_state("opencode")


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
        if args.model_profile or args.model_role or args.model or args.variant:
            raise ModelSelectionError(
                "invalid-dispatch-model-selection",
                "--inherit-model-settings is mutually exclusive with --model-profile, --model-role, --model, and --variant",
            )
        refusal = inheritance_refusal(_model_policy(), "OpenCode")
        if refusal:
            raise ModelSelectionError(*refusal)
        return {
            "source": "inherit", "role": "inherit", "profile": "unsealed",
            "tier": "inherit", "granularity": "legacy", "model": "inherit", "variant": "inherit",
        }
    if args.model_profile:
        if not args.model_role and args.worker_type != "owner":
            raise ModelSelectionError(
                "model-profile-role-required",
                "route-bound --model-profile requires the independently sealed --model-role",
            )
        if bool(args.model) != bool(args.variant):
            raise ModelSelectionError(
                "invalid-dispatch-model-selection",
                "capacity override requires --model and --variant together",
            )
        if (args.model or args.variant) and not args.capacity_retry:
            raise ModelSelectionError(
                "model-profile-override-forbidden",
                "a route-sealed model profile may use a concrete override only on a checked capacity retry "
                "(to choose a model, seal it at compose time: capability-route.py compose --pin <target>=<harness>:<model>[@<effort>])",
            )
        if args.model_profile == TOP_PROFILE and args.model:
            # No cascade in or out, on every adapter (combined review m5):
            # nothing runs under the `top` label but the top model itself,
            # even where `top` collapses onto another tier. This adapter
            # must read the *requested* profile, not the resolved one:
            # opencode resolves `top` to `collapsed-top-to-balanced-deep`, so a check
            # on `resolved["profile"]` (what claude and codex use, where the
            # label survives) would never fire here (guard review m3).
            raise ModelSelectionError(
                "profile-top-override-forbidden",
                "the top exception profile admits no concrete --model override, capacity retry included",
            )
        try:
            resolved, _receipt = resolve_runtime_profile(
                "opencode", args.model_profile, source_root=ROOT
            )
        except ModelProfileError as exc:
            raise ModelSelectionError("invalid-dispatch-model-profile", str(exc)) from exc
        # A route-sealed selection pin (`compose --pin`) beats the profile's
        # own model; a checked capacity retry still replaces it, and the receipt
        # then says `pin+capacity` with the pinned model so nothing is
        # overwritten silently.
        try:
            pin = route_selection_pin(
                getattr(args, "route_file", None)
                or getattr(getattr(args, "owner_route_binding", None), "route_file", None),
                worker_type=args.worker_type, adapter="opencode",
            )
        except ModelProfileError as exc:
            raise ModelSelectionError(exc.reason, str(exc)) from exc
        model = args.model or resolved["model"]
        variant = args.variant or resolved["budget"]
        source = "profile+capacity" if args.model else "profile"
        if pin["status"] == "applied":
            if not args.model:
                model, variant = pin["model"], pin["effort"] or resolved["budget"]
            source = "pin+capacity" if args.model else "pin"
            if route_authority.pin_target(args.worker_type) != "frame":
                _require_headless_model(model, source)
        elif args.model_profile != TOP_PROFILE:
            # The route-sealed `top` profile is the one door to a main-session-only
            # model, as on the other adapters.
            _require_headless_model(model, f"profile:{args.model_profile}")
        return {
            "source": source,
            "role": args.model_role or "_kernel/owner",
            "profile": resolved["profile"],
            "tier": resolved["tier"],
            "granularity": resolved["granularity"],
            "model": model,
            "variant": variant,
            "pin_status": pin["status"],
            **({"pin_model": pin["model"]} if pin["status"] == "applied" else {}),
        }
    if args.model_role and args.model:
        raise ModelSelectionError(
            "invalid-dispatch-model-selection",
            "--model-role is mutually exclusive with --model (tier-hopping); "
            "situational tuning keeps the role's tier and adjusts --variant only",
        )
    if args.model_role:
        fields = role_map(args.model_role)
        model = fields.get("model")
        variant = fields.get("variant")
        if not model or not variant:
            raise ModelSelectionError("invalid-dispatch-model-role", "role map did not return model and variant")
        if model == "opencode-default" or variant == "runtime-default":
            raise ModelSelectionError(
                "invalid-dispatch-model-role",
                f"model role {args.model_role!r} resolved to runtime defaults; configure AGENT_MODEL_* and AGENT_VARIANT_* or pass --model/--variant",
            )
        _require_headless_model(model, f"role:{args.model_role}")
        # 역할 티어 고정 + 상황별 variant 오버라이드 (2026-07-22 사용자 원칙).
        if args.variant:
            return {
                "source": "role+effort", "role": args.model_role, "profile": "unsealed",
                "tier": "legacy", "granularity": "legacy", "model": model, "variant": args.variant,
            }
        return {
            "source": "role", "role": args.model_role, "profile": "unsealed",
            "tier": "legacy", "granularity": "legacy", "model": model, "variant": variant,
        }
    if not args.model and not args.variant:
        raise ModelSelectionError(
            "missing-dispatch-model-selection",
            "main dispatch must choose --model-role, --model with --variant, or --inherit-model-settings",
        )
    if not args.model or not args.variant:
        raise ModelSelectionError(
            "invalid-dispatch-model-selection",
            "--model and --variant must be provided together",
        )
    _require_headless_model(args.model, "explicit")
    return {
        "source": "explicit", "role": "-", "profile": "unsealed",
        "tier": "explicit", "granularity": "legacy", "model": args.model, "variant": args.variant,
    }


def prepare_nested_runtime(worktree: Path, attempt_id: str, environ=None,
                           state_root: Path | str | None = None) -> dict[str, str]:
    """Keep each attempt's OpenCode state beside the canonical dispatch registry.

    OpenCode writes dependency state beside its config as well as under XDG
    data/cache/state. User config and the existing auth are linked for reading;
    no credentials or user configuration are copied or rewritten.
    """
    env = os.environ if environ is None else environ
    if not re.fullmatch(r"att-[A-Za-z0-9_-]+", attempt_id):
        raise DispatchContractError("nested-opencode-attempt-invalid")
    if state_root is None:
        jobs = env.get("AGENT_DISPATCH_JOBS")
        if not jobs:
            raise DispatchContractError("nested-opencode-state-root-unavailable")
        state_root = Path(jobs).expanduser().resolve(strict=False).parent
    state_root = Path(state_root).resolve()
    runtime = state_root / "opencode-runtime" / attempt_id
    if not runtime.resolve().is_relative_to(state_root):
        raise DispatchContractError("nested-opencode-runtime-outside-state-root")
    values = {}
    for kind in ("data", "cache", "state", "config"):
        directory = runtime / kind
        if not directory.resolve().is_relative_to(state_root):
            raise DispatchContractError("nested-opencode-runtime-outside-state-root")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        values[f"XDG_{kind.upper()}_HOME"] = str(directory)
    source_data = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    auth = source_data / "opencode" / "auth.json"
    if auth.is_file():
        destination = Path(values["XDG_DATA_HOME"]) / "opencode" / "auth.json"
        destination.parent.mkdir(exist_ok=True, mode=0o700)
        if destination.is_symlink() and destination.resolve() == auth.resolve():
            pass
        elif destination.exists() or destination.is_symlink():
            raise DispatchContractError("nested-opencode-auth-link-conflict", str(destination))
        else:
            destination.symlink_to(auth.resolve())
    source_config = Path(env.get("OPENCODE_CONFIG_DIR") or
                         Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode")
    config = Path(values["XDG_CONFIG_HOME"]) / "opencode"
    config.mkdir(exist_ok=True, mode=0o700)
    # These files are dependency-manager outputs. Configuration and relative
    # plugin/command/agent inputs remain links to the caller's original bytes.
    generated = {".git", ".gitignore", "node_modules", "package.json", "package-lock.json", "bun.lock", "bun.lockb"}
    if source_config.is_dir():
        for source in source_config.iterdir():
            if source.name in generated:
                continue
            link = config / source.name
            if link.is_symlink() and link.resolve() == source.resolve():
                continue
            if link.exists() or link.is_symlink():
                raise DispatchContractError("nested-opencode-config-link-conflict", str(link))
            link.symlink_to(source.resolve(), target_is_directory=source.is_dir())
    if env.get("OPENCODE_CONFIG_DIR"):
        values["OPENCODE_CONFIG_DIR"] = str(config)
    return values


def qa_track(capability: str) -> str:
    if capability.startswith("code-") or capability == "autopilot-code":
        return "code"
    if capability == "autopilot-research" or capability.startswith("analyze-"):
        return "research"
    if capability in {"autopilot-draft", "autopilot-refine"} or capability.startswith("draft-"):
        return "doc"
    return "general"


def scoped_external_directory_config(
    artifact_root: str,
    report_bundle_root: str | None = None,
    execution_access_roots: tuple[Path, ...] = (),
    execution_access_read_roots: tuple[Path, ...] = (),
    *,
    agent_home: Path | None = None,
    worktree: str | None = None,
    selected_agent: str | None = None,
) -> str:
    raw = os.environ.get("OPENCODE_CONFIG_CONTENT", "").strip()
    try:
        config = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        raise ValueError(f"OPENCODE_CONFIG_CONTENT is not valid JSON: {e}") from e
    if not isinstance(config, dict):
        raise ValueError("OPENCODE_CONFIG_CONTENT must contain a JSON object")

    permission = config.get("permission")
    if permission is None:
        permission = {}
    elif isinstance(permission, str):
        permission = {"*": permission}
    elif not isinstance(permission, dict):
        raise ValueError("OpenCode permission config must be a string or object")
    else:
        permission = dict(permission)

    def effective_tool(subject, tool, default=None):
        # Native fromConfig flattens every matching outer wildcard in order.
        # A scalar replaces all paths, whereas an object replaces only its
        # matching patterns. Preserve those rules before moving the tool last.
        value = None if default is None else {"*": default}
        for key, candidate in subject.items():
            wildcard = re.escape(key.replace("\\", "/")).replace(r"\ ", " ").replace(r"\*", ".*").replace(r"\?", ".")
            if wildcard.endswith(" .*"):
                wildcard = wildcard[:-3] + "( .*)?"
            if not re.fullmatch(wildcard, tool, re.DOTALL):
                continue
            if isinstance(candidate, str):
                value = {"*": candidate}
            elif isinstance(candidate, dict):
                if value is None:
                    value = {}
                for pattern, action in candidate.items():
                    value.pop(pattern, None)
                    value[pattern] = action
            else:
                raise ValueError(f"OpenCode {tool} permission must be a string or object")
        return value

    # Keep the existing headless projection before normalizing native order:
    # no explicit external rule means deny outside the scoped launch roots,
    # even when a global catchall allows/asks for other tools (SD-15).
    if agent_home is not None and "external_directory" not in permission:
        permission["external_directory"] = "deny"
    original_permission = dict(permission)
    external = (effective_tool(original_permission, "external_directory")
                if agent_home is not None else permission.get("external_directory"))
    if external is None:
        # SD-15/1(b): headless has no human to answer an "ask" prompt, so the
        # runtime auto-rejects it -- and that auto-reject truncates the
        # session outright (no terminal envelope, exit varies). "deny"
        # instead returns a structured tool error to the model, which the
        # model can recover from and keep working; measured 2026-08-07
        # (dev_logs/section4_permission_deny_experiment.md): identical
        # out-of-scope-path prompt died right after auto-reject under "ask"
        # (no completion) and reached the final handoff under "deny".
        rules: dict[str, str] = {"*": "deny"}
    elif isinstance(external, str):
        rules = {"*": external}
    elif isinstance(external, dict):
        rules = dict(external)
    else:
        raise ValueError("OpenCode external_directory permission must be a string or object")

    # The launch environment and this permission projection use the same
    # resolved agent home. Keep its lexical alias as well as the canonical
    # directory: native tools can check either form of a symlinked install.
    contract_roots = () if agent_home is None else tuple(dict.fromkeys((
        str(agent_home / "capabilities"),
        str((agent_home / "capabilities").resolve()),
    )))
    for root in (artifact_root, report_bundle_root, *execution_access_roots, *contract_roots):
        if not root:
            continue
        root = str(root)
        for pattern in (root, f"{root}/**"):
            rules.pop(pattern, None)
            rules[pattern] = "allow"
    # Requested read-only roots (OpenCode grant only): same read visibility
    # as the writable roots above, but edits stay denied. A root the write
    # side already covers keeps its writable rule (write wins); a broader
    # read root re-asserts the covered writable paths afterwards so the deny
    # cannot swallow them. Native last-match order applies throughout, and
    # the default deny outside every listed root (SD-15) is unchanged.
    def _path_forms(value) -> set[str]:
        text = os.path.normpath(str(value))
        forms = {text}
        try:
            forms.add(os.path.normpath(os.path.realpath(text)))
        except (OSError, RuntimeError, ValueError):
            pass
        return forms

    def _covers(outer, inner) -> bool:
        for left in _path_forms(outer):
            for right in _path_forms(inner):
                if right == left or right.startswith(left + os.sep):
                    return True
        return False

    def _strictly_under(outer, inner) -> bool:
        for left in _path_forms(outer):
            for right in _path_forms(inner):
                if right != left and right.startswith(left + os.sep):
                    return True
        return False

    write_side = [root for root in (artifact_root, report_bundle_root, *execution_access_roots) if root]
    # The worktree is a writable area too: a read root inside it stays writable there, as its
    # grant records (`read-only-root-writable`). A read root that holds a writable area keeps
    # its deny with the area re-allowed below; one holding the worktree is refused as too broad.
    covered_roots = [str(root) for root in write_side] + ([str(worktree)] if worktree else []) + list(contract_roots)
    read_deny_roots = [str(root) for root in execution_access_read_roots
                       if root and not any(_covers(cover, root) for cover in covered_roots)]
    write_keep_roots = [str(root) for root in write_side
                        if any(_strictly_under(read, root) for read in read_deny_roots)]
    for root in read_deny_roots:
        for pattern in (root, f"{root}/**"):
            rules.pop(pattern, None)
            rules[pattern] = "allow"
    if contract_roots:
        permission.pop("external_directory", None)
    permission["external_directory"] = rules
    if contract_roots or read_deny_roots:
        edit = effective_tool(original_permission, "edit", "allow")
        if isinstance(edit, str):
            edit_rules = {"*": edit}
        elif isinstance(edit, dict):
            edit_rules = dict(edit)
        else:
            raise ValueError("OpenCode edit permission must be a string or object")
        for root in (*contract_roots, *read_deny_roots):
            # v1 native edit/write/patch ask against paths relative to the
            # worktree, unlike external_directory's absolute directory glob.
            edit_paths = (root,) if worktree is None else (
                root, os.path.relpath(root, worktree),
            )
            for pattern in (pattern for directory in edit_paths
                            for pattern in (directory, f"{directory}/**")):
                edit_rules.pop(pattern, None)
                edit_rules[pattern] = "deny"
        for root in write_keep_roots:
            edit_paths = (root,) if worktree is None else (
                root, os.path.relpath(root, worktree),
            )
            for pattern in (pattern for directory in edit_paths
                            for pattern in (directory, f"{directory}/**")):
                edit_rules.pop(pattern, None)
                edit_rules[pattern] = "allow"
        permission.pop("edit", None)
        permission["edit"] = edit_rules
    config["permission"] = permission
    if (contract_roots or read_deny_roots) and selected_agent:
        # Native agent permissions merge after global permissions. Overlay only
        # the selected agent, without giving its other tools a new default.
        agents = config.get("agent", {})
        if not isinstance(agents, dict):
            raise ValueError("OpenCode agent config must be an object")
        agents = dict(agents)
        selected = agents.get(selected_agent, {})
        if not isinstance(selected, dict):
            raise ValueError("OpenCode selected agent config must be an object")
        selected = dict(selected)
        local = selected.get("permission", {})
        if isinstance(local, str):
            local = {"*": local}
        elif isinstance(local, dict):
            local = dict(local)
        else:
            raise ValueError("OpenCode agent permission must be a string or object")
        original_local = dict(local)
        # Triples accumulate into one overlay per tool in listed order, so a
        # later triple wins on overlap (native last-match) without wiping an
        # earlier triple's unrelated patterns.
        overlaid: dict[str, dict] = {}
        for tool, action, roots in (("external_directory", "allow", contract_roots),
                                    ("edit", "deny", contract_roots),
                                    ("external_directory", "allow", read_deny_roots),
                                    ("edit", "deny", read_deny_roots)):
            if not roots:
                continue
            if tool not in overlaid:
                old = effective_tool(original_local, tool)
                if old is None:
                    overlaid[tool] = {}
                elif isinstance(old, str):
                    overlaid[tool] = {"*": old}
                elif isinstance(old, dict):
                    overlaid[tool] = dict(old)
                else:
                    raise ValueError(f"OpenCode agent {tool} permission must be a string or object")
            overlay = overlaid[tool]
            for root in roots:
                directories = (root,)
                if tool == "edit" and worktree is not None:
                    directories += (os.path.relpath(root, worktree),)
                for directory in directories:
                    for pattern in (directory, f"{directory}/**"):
                        overlay.pop(pattern, None)
                        overlay[pattern] = action
        for tool, overlay in overlaid.items():
            local.pop(tool, None)
            local[tool] = overlay
        if write_keep_roots:
            # Write wins inside the selected agent too: explicit edit allow
            # chained after the read deny above, from whatever edit overlay
            # the read roots just produced.
            current = local.get("edit")
            if isinstance(current, dict):
                overlay = dict(current)
            else:
                old = effective_tool(original_local, "edit")
                if old is None:
                    overlay = {}
                elif isinstance(old, str):
                    overlay = {"*": old}
                elif isinstance(old, dict):
                    overlay = dict(old)
                else:
                    raise ValueError("OpenCode agent edit permission must be a string or object")
            for root in write_keep_roots:
                directories = (root,) if worktree is None else (
                    root, os.path.relpath(root, worktree),
                )
                for directory in directories:
                    for pattern in (directory, f"{directory}/**"):
                        overlay.pop(pattern, None)
                        overlay[pattern] = "allow"
            local.pop("edit", None)
            local["edit"] = overlay
        selected["permission"] = local
        agents[selected_agent] = selected
        config["agent"] = agents
    return json.dumps(config, ensure_ascii=False, separators=(",", ":"))


def deny_commands(config_content: str, commands, selected_agent: str | None = None) -> str:
    """Add `bash` deny rules for `commands` to an OpenCode config, globally and for the agent.

    Native permissions apply in order and the last match wins, so the merged
    `bash` entry moves last; a selected agent's permissions merge after the
    global ones, so it receives the same rules.
    """
    config = json.loads(config_content) if config_content else {}

    def with_denies(permission):
        if isinstance(permission, str):
            permission = {"*": permission}
        permission = dict(permission or {})
        bash = permission.pop("bash", None)
        rules = {"*": bash} if isinstance(bash, str) else dict(bash or {})
        for command in commands:
            for pattern in (command, f"{command} *"):
                rules.pop(pattern, None)
                rules[pattern] = "deny"
        permission["bash"] = rules
        return permission

    config["permission"] = with_denies(config.get("permission"))
    if selected_agent:
        agents = dict(config.get("agent") or {})
        selected = dict(agents.get(selected_agent) or {})
        selected["permission"] = with_denies(selected.get("permission"))
        agents[selected_agent] = selected
        config["agent"] = agents
    return json.dumps(config, ensure_ascii=False, separators=(",", ":"))


def prompt(args: argparse.Namespace) -> tuple[str, str]:
    if args.prompt_file and args.prompt_text:
        raise ValueError("--prompt-file and --prompt-text are mutually exclusive")
    if args.prompt_file:
        path = Path(args.prompt_file)
        task, source = path.read_text(encoding="utf-8"), str(path)
    elif args.prompt_text:
        task, source = args.prompt_text, "inline"
    else:
        task, source = "Run the requested portable harness work.", "generated"
    args.replacement_raw_task = task
    args.assignment_sha256 = "sha256:" + hashlib.sha256(task.encode("utf-8")).hexdigest()
    args.worker_type = resolve_worker_type(
        explicit=args.worker_type,
        dispatch_depth=args.dispatch_depth,
        worker_role=args.worker_role,
        route_node=args.route_node,
        profile_type=None,
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
    heartbeat = runtime_progress_prompt()
    return (
        f"{bootstrap}\n"
        "Dispatch metadata:\n"
        f"- capability: {args.capability}\n"
        f"- capability_mode: {args.capability_mode}\n"
        f"- worker_mode: {args.worker_mode or '-'}\n"
        f"- qa: {args.qa}\n"
        f"- intensity: {args.intensity}\n"
        f"- dispatch_depth: {args.dispatch_depth}\n"
        f"- worker_type: {args.worker_type}\n"
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
        f"- route_state: {'consume the immutable record already validated by the wrapper' if args.route_file else 'validated dispatch metadata'}\n\n"
        "OpenCode realization:\n"
        "- The wrapper already validated capability mode, optional worker mode, QA, artifact-root access, and any route record. Use worker-route only for a safety recheck.\n"
        "- The typed bootstrap contains the portable unit. A dispatch-depth-1 capability owner must not load any worker mode.\n"
        f"- Run adapters/opencode/bin/preflight.sh qa-policy {args.qa} {qa_track(args.capability)} and keep its required assurance in the artifact.\n"
        f"{contract_read_prompt(args, 'opencode')}"
        "- Preserve the reported QA/tool contracts in the artifact; owner workers launch checked adapter wrappers directly.\n\n"
        f"{heartbeat}"
        f"{commit_policy.prompt_clause(args)}"
        f"{stage_session_prompt(args)}"
        f"{released_task_prompt(args)}"
        f"{unit_bootstrap_prompt(args, task, os.environ)}"
        f"{assignment_prompt(args, task, os.environ)}"
        "End with the kernel's exact three-line handoff as the entire final message — "
        "no summary sentence before it, nothing after it.\n",
        source,
    )

def _supervised_owner(args: argparse.Namespace) -> bool:
    """A sealed route binding supervises its owner; a registered depth-1 owner
    without one (the quick and solo shape) is supervised too. Route identity
    stays separate from the reason to supervise."""
    if getattr(args, "owner_route_binding", None):
        return True
    return (getattr(args, "dispatch_depth", None) == 1
            and getattr(args, "worker_type", None) == "owner"
            and getattr(args, "intensity", None) not in STANDARD_PLUS_INTENSITIES)


def initialize_supervised_owner_input(args: argparse.Namespace, jobs: Path) -> None:
    WRAPPER_COMMON.initialize_owner_input_when(
        args, jobs, supervised=_supervised_owner(args), input_kind="opencode-next-turn")


def shell_command(args: argparse.Namespace, prompt_path: Path, log_path: Path) -> str:
    if _supervised_owner(args):
        lease = (supervisor_lease_path(args.jobs_path, args.attempt_id) if args.attempt_id
                 else dispatch_state_root(args.jobs_path) / "supervisor-state" / "preview-only.lease")
        cmd = [
            sys.executable, str(ROOT / "utilities" / "claude-session-supervisor.py"),
            "--runtime-harness", "opencode", "--worktree", args.worktree,
            "--jobs", str(args.jobs_path), "--parent-attempt-id", args.attempt_id or "unassigned",
            "--state-file", str(lease.with_suffix(".json")), "--lease-file", str(lease),
        ]
        route = _supervisor_route(args)
        if route:
            cmd += ["--route-file", route[0], "--route-id", route[1], "--route-hash", route[2]]
        cmd += ["--opencode-agent", args.agent]
        if args.resolved_model_settings["source"] != "inherit":
            cmd += ["--model", args.resolved_model_settings["model"],
                    "--variant", args.resolved_model_settings["variant"]]
        return (" ".join(shlex.quote(x) for x in cmd)
                + f" < {shlex.quote(str(prompt_path))} >> {shlex.quote(str(log_path))} 2>&1")
    cmd = [
        "opencode",
        "run",
        "--dir",
        args.worktree,
        "--format",
        "json",
        "--agent",
        args.agent,
    ]
    if args.resolved_model_settings["source"] != "inherit":
        cmd += ["--model", args.resolved_model_settings["model"]]
        if args.resolved_model_settings["variant"] != "runtime-default":
            cmd += ["--variant", args.resolved_model_settings["variant"]]
    prompt_arg = f'"$(cat -- {shlex.quote(str(prompt_path))})"'
    return " ".join(shlex.quote(x) for x in cmd) + f" {prompt_arg} >> {shlex.quote(str(log_path))} 2>&1"


def _effective_parent_cwd(args):
    return parent_completion.effective_parent_cwd(args)


def resolve_parent_completion_delivery(args: argparse.Namespace) -> str:
    return parent_completion.resolve_parent_completion_delivery(
        args, probe=probe_managed_codex_parent)


def bind_parent_completion_delivery(args: argparse.Namespace) -> None:
    args.parent_completion_delivery = resolve_parent_completion_delivery(args)


def validate_interactive_parent_launch(args: argparse.Namespace) -> None:
    parent_completion.validate_interactive_parent_launch(args)


def launch_parent_completion_sidecar(args: argparse.Namespace, jobs: Path) -> None:
    parent_completion.launch_parent_completion_sidecar(
        args, jobs, launch=launch_codex_queue_completion_sidecar, annotate=annotate_attempt_row)


def acquire_review_lease_after_claim(
    args, jobs: Path, identity: dict[str, str]
) -> dict[str, str]:
    if not args.review_output:
        return {}
    binding = args.review_output_binding
    budget = getattr(args, "watchdog_budget", None)
    if budget is None:
        raise ProducerError("review-watchdog-budget-missing")
    witness_metadata = {
        "attempt_id": args.attempt_id,
        "review_cycle_id": binding["cycle_id"],
        "review_governed_lease": "summary-flock-v1",
        "review_governed_lease_nonce": args.review_governed_lease_nonce,
    }
    witness_unlocked = lambda: not review_governed_lease_is_held(
        Path(args.artifact_root), witness_metadata
    )
    if args.launch_lifecycle == DETACHED:
        handle = getattr(args, "review_watchdog_handle", None)
        if handle is None:
            raise ProducerError("review-watchdog-handle-missing")
        return acquire_review_admission(
            handle=handle, budget=budget, identity=identity,
            root=Path(args.artifact_root), cycle_id=binding["cycle_id"],
            attempt_id=args.attempt_id, review_output=binding["output_path"],
            binding=binding, jobs=jobs, nonce=args.review_governed_lease_nonce,
            lease_acquire=review_lease_acquire, witness_probe=witness_unlocked,
        )
    return acquire_foreground_review_admission(
        budget=budget, identity=identity, root=Path(args.artifact_root),
        cycle_id=binding["cycle_id"], attempt_id=args.attempt_id,
        review_output=binding["output_path"], binding=binding, jobs=jobs,
        lease_acquire=review_lease_acquire, witness_probe=witness_unlocked,
    )


def attach_summary_owner(args, log_path: Path, prompt_path: Path, identity):
    review = args.review_output_binding
    return launch_summary_owner(
        attempt_id=args.attempt_id,
        harness="opencode",
        transcript=log_path,
        prompt_path=prompt_path,
        target_pid=int(identity["pid"]),
        target_start=identity["pid_start"],
        review_artifact_root=review["artifact_root"] if review else None,
        review_cycle_id=review["cycle_id"] if review else None,
        review_lease_nonce=args.review_governed_lease_nonce if review else None,
    )

def append_job(jobs: Path, args: argparse.Namespace) -> bool:
    return WRAPPER_COMMON.append_job(
        jobs, args, harness="opencode", runtime_sandbox="adapter-default", effort_key="variant",
        claim=claim_attempt_row, marker_gate=completion_marker_gate,
    )


def close_job_row(jobs: Path, slug: str, worktree: str, reason: str, reset: str, attempt_id: str | None = None) -> bool:
    """SD-15: flip this dispatch's own open row to done with a dead-<reason> note.

    Matches by (slug, worktree, status==open) under the same flock append_job uses.
    Appends note=dead-<reason>[,reset=<reset>] to the pipe column. Idempotent: returns
    False if no matching open row is found. Homomorphic with the Claude/codex wrappers.
    """
    if attempt_id:
        evidence = {"reset": reset} if reset else {}
        if reason == "capacity":
            evidence.update(failure_class="capacity", detected_by="anchored-early-exit")
        closed = close_attempt_row(jobs, attempt_id, f"dead-{reason}", evidence=evidence)
        if closed:
            materialize_after_terminal_close(jobs, attempt_id)
        return closed
    if not jobs.is_file():
        return False
    with jobs_lock(jobs):
        lines = jobs.read_text(encoding="utf-8").splitlines(keepends=True)
        changed = False
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 6:
                continue
            ts, status, repo, wt, row_slug, pipe = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
            if status != "open" or row_slug != slug or wt != worktree:
                continue
            metadata = parse_registry_metadata(pipe)
            if metadata.get("attempt_schema_version") != "2":
                continue
            if attempt_id and f"attempt_id={attempt_id}" not in pipe.split(","):
                continue
            pipe += f",note=dead-{reason}"
            if reason == "capacity":
                pipe += ",failure_class=capacity,detected_by=anchored-early-exit"
            if reset:
                pipe += f",reset={reset}"
            lines[i] = f"{ts}\tdone\t{repo}\t{wt}\t{row_slug}\t{pipe}\n"
            changed = True
            break
        if changed:
            # Lock-free readers (join, owner input) must never see a truncated registry.
            _atomic_registry_replace(jobs, "".join(lines).splitlines())
        return changed


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
    # pointer (~/.config/opencode/hearting) so opencode's deliberate
    # bundle-first priority is preserved without forking the resolver's
    # fallback chain.
    return _resolve_agent_home(
        runtime_pointer=Path.home() / ".config" / "opencode" / "hearting"
    )


def check_runtime_projection(worktree: str) -> int:
    result = subprocess.run(
        [str(ROOT / "adapters" / "opencode" / "bin" / "preflight.sh"), "headless", "--check", worktree],
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
    return result.returncode



def validate_preflight(kind: str, command: str, value: str, reason: str) -> int:
    result = subprocess.run(
        [str(ROOT / "adapters" / "opencode" / "bin" / "preflight.sh"), command, value],
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


def validate_dispatch_metadata(args: argparse.Namespace) -> int:
    rc = validate_preflight("capability", "capability-info", args.capability, "invalid-dispatch-capability")
    if rc != 0:
        return rc
    capability_info = subprocess.run(
        [str(ROOT / "adapters" / "opencode" / "bin" / "preflight.sh"), "capability-info", args.capability],
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
    WRAPPER_COMMON.bind_internal_eligibility_probe(args, "opencode")


def validate_route_record(args: argparse.Namespace) -> int:
    routed=any((args.route_id,args.route_hash,args.route_node,args.registry_digest))
    if routed and not args.route_file: return fail("route-record-required",65,route_id=args.route_id or "-")
    if not args.route_file: return 0
    required=("route_id","route_hash","route_node","registry_digest","write_scope")
    missing=[name for name in required if not getattr(args,name)]
    if missing: return fail("route-metadata-missing",65,fields=",".join(missing))
    try:
        route_record=json.loads(Path(args.route_file).read_text(encoding="utf-8"))
    except (OSError,ValueError):
        route_record={}
    if route_record.get("schema_version") != 2 or "broker_contract_version" in route_record:
        return fail("legacy-broker-route-read-only",65,route_file=args.route_file,child_spawned="0")
    try:
        validate_runtime_requirements(route_record, args.route_node)
    except OwnerRouteBindingError as exc:
        return fail(str(exc),69,child_spawned="0",fallback="inline-or-main")
    try:
        validate_route_mode_axes(args, route_record)
    except DispatchModeContractError as exc:
        return fail(exc.reason, 65, **exc.fields, child_spawned="0")
    command=[sys.executable,str(ROOT/"utilities"/"worker-route-guard.py"),"validate",
        "--route",args.route_file,"--node",args.route_node,"--cwd",args.worktree,
        "--artifact-root",args.artifact_root,"--capability",args.capability,
        "--intensity",args.intensity,"--write-scope",args.write_scope,
        "--route-id",args.route_id,"--route-hash",args.route_hash,
        "--registry-digest",args.registry_digest,"--unit",args.unit,
        "--launch-phase",args.action,
        "--model-role",args.model_role or "","--model-profile",args.model_profile or ""]
    if args.attempt_id:
        command += ["--current-attempt", args.attempt_id]
    result=subprocess.run(command,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if result.returncode:
        if result.stdout: print(result.stdout,end="")
        if result.stderr: print(result.stderr,end="",file=sys.stderr)
        return fail(
            "worker-route-validation-failed", result.returncode,
            route_file=args.route_file, registered="0", started="0",
            child_spawned="0",
        )
    args.route_validation=result.stdout.strip()
    refusal = route_authority.completion_gate(
        args, args.action, args.agent_home, route_authority.prelaunch_registry(args),
        gate=completion_marker_gate)
    if refusal:
        reason, code, fields = refusal
        return fail(reason, code, **fields)
    return 0


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv[1:])
    args.replacement_input_argv = list(argv[1:])
    # parent_sandbox is used here only to tighten the override gate, never to
    # loosen it, so consuming it before the tuple-validity check at :1136 is
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
    action = "start" if args.start else "register" if args.register else "dry-run"
    args.action = action
    args.command_attempt_id = args.attempt_id
    if action == "dry-run":
        args.attempt_id = None
    bind_runtime_parent(args)
    if args.broker_request_id or args.launch_authority == "ancestor-broker":
        return fail("launch-broker-retired", 76, child_spawned="0")
    args.agent_home = resolve_agent_home()
    worktree = Path(args.worktree)
    if not worktree.is_dir():
        return fail("worktree-not-found", 66, worktree=args.worktree)
    if subprocess.run(["git", "-C", args.worktree, "rev-parse", "--is-inside-work-tree"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        return fail("not-a-git-worktree", 65, worktree=args.worktree)
    try:
        args.artifact_root = resolve_artifact_root(args.worktree)
        args.report_bundle_root = resolve_report_bundle_root(args.route_file, args.route_node)
        args.opencode_config_content = scoped_external_directory_config(
            args.artifact_root,
            str(args.report_bundle_root) if args.report_bundle_root is not None else None,
            agent_home=args.agent_home,
            worktree=args.worktree,
            selected_agent=args.agent,
        )
    except ValueError as e:
        return fail("artifact-root-access-config-failed", 64, detail=str(e), worktree=args.worktree)
    args.worker_type = resolve_worker_type(
        explicit=args.worker_type,
        dispatch_depth=args.dispatch_depth,
        worker_role=args.worker_role,
        route_node=args.route_node,
        profile_type=None,
    )
    try:
        normalize_dispatch_modes(
            args,
            default_capability_mode=capability_mode_from_route_file(args.route_file),
        )
    except DispatchModeContractError as exc:
        return fail(exc.reason, 64, **exc.fields, child_spawned="0")
    rc = validate_dispatch_metadata(args)
    if rc != 0:
        return rc
    args.eligibility_probe = "-"
    bind_internal_eligibility_probe(args)
    try:
        validate_nested_eligibility(
            dispatch_depth=args.dispatch_depth, action=action, parent_harness=args.parent_harness,
            parent_transport=args.parent_transport, parent_sandbox=args.parent_sandbox,
            child_harness="opencode", launch_authority=args.launch_authority,
            status=args.nested_eligibility, source=args.eligibility_source,
        )
    except DispatchContractError as e:
        return fail(
            e.reason, 69, detail=e.detail,
            parent_harness=args.parent_harness or "-",
            parent_transport=args.parent_transport or "-",
            parent_sandbox=args.parent_sandbox or "-",
            child_harness="opencode",
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
            harness="opencode",
        )
        if args.owner_route_binding:
            failure_fields = owner_binding_tuple_failure_fields(
                dispatch_depth=args.dispatch_depth, worker_type=args.worker_type, route_file=args.route_file)
            if failure_fields:
                return fail("owner-route-binding-tuple-invalid", 65, child_spawned="0", **failure_fields)
    except OwnerRouteBindingError as exc:
        return fail(str(exc),65,child_spawned="0")
    rc = validate_route_record(args)
    if rc != 0:
        return rc
    args.replica_batch_expectation = None
    if action in {"register", "start"}:
        try:
            args.replica_batch_expectation = replica_batch_expectation(
                args.route_file,
                args.route_node,
                action,
                attempt_id=args.attempt_id or "",
                parent_attempt_id=args.parent_attempt_id or "",
                harness="opencode",
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
            intensity=args.intensity, harness="opencode",
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
    bind_parent_completion_delivery(args)
    try:
        validate_interactive_parent_launch(args)
    except DispatchContractError as exc:
        return fail(exc.reason, 69, detail=exc.detail, child_spawned="0")
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
    if args.start and shutil.which("opencode") is None:
        return fail("opencode-command-unavailable", 69, worktree=args.worktree)
    if args.start:
        rc = check_runtime_projection(args.worktree)
        if rc != 0:
            return rc

    agent_home = args.agent_home
    try:
        registry = resolve_global_registry(agent_home, args.jobs, args.dispatch_depth, action)
        jobs = registry.path
        args.jobs_path = jobs
        from review_input import prepare_request as prepare_review_input
        prepare_review_input(args)
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
    # Item 5-1 (SD-48~50 exact parent binding), ported from the Claude wrapper.
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
            reason = ("live-parent-not-found" if isinstance(e, DispatchContractError)
                      and e.reason == "parent-attempt-not-found" else
                      e.reason if isinstance(e, DispatchContractError) else "parent-repo-unreadable")
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
                harness="opencode",
                fallback_hop=args.fallback_hop,
                fallback_ordinal=args.fallback_ordinal,
            )
        except DispatchContractError as exc:
            return fail(exc.reason, 65, detail=exc.detail, child_spawned="0")
    try:
        default_roots = adapter_default_roots(args)
        args.execution_access_grant = route_authority.bind_launch_access(
            args, runtime="opencode", default_roots=default_roots)
        if args.execution_access_grant is not None:
            args.opencode_config_content = scoped_external_directory_config(
                args.artifact_root,
                str(args.report_bundle_root)
                if args.report_bundle_root is not None
                else None,
                args.execution_access_grant.additional_writable_roots,
                args.execution_access_grant.read_roots,
                agent_home=args.agent_home,
                worktree=args.worktree,
                selected_agent=args.agent,
            )
    except ExecutionAccessError as exc:
        return fail(exc.reason, 64, detail=exc.detail, child_spawned="0")
    except ValueError as exc:
        return fail(
            "artifact-root-access-config-failed",
            64,
            detail=str(exc),
            child_spawned="0",
        )
    if commit_policy.no_commit_stage(args):
        # A no-commit worker (commit_policy) is refused `git commit` by the runtime too.
        args.opencode_config_content = deny_commands(
            args.opencode_config_content, commit_policy.COMMIT_COMMANDS, args.agent)
    args.nested_runtime_env = {}
    if action == "start" and args.dispatch_depth == 2 and args.parent_harness == "codex":
        try:
            args.nested_runtime_env = prepare_nested_runtime(
                Path(args.worktree), args.attempt_id, state_root=dispatch_state_root(args.jobs_path))
        except (DispatchContractError, OSError) as exc:
            return fail(getattr(exc, "reason", "nested-opencode-runtime-unavailable"), 73,
                        detail=str(exc), child_spawned="0")
    log_dir = (
        Path(args.log_dir)
        if args.log_dir
        else dispatch_state_root(args.jobs_path) / "logs"
    )
    prompt_text, prompt_source = prompt(args)
    from review_input import prompt_block as review_input_prompt
    prompt_text += review_input_prompt(args)
    from dispatch_replacement import recovery_instructions
    prompt_text += recovery_instructions(args)
    if action == "start" and args.replica_batch_expectation is not None:
        try:
            args.replica_batch_expectation = replica_batch_expectation(
                args.route_file,
                args.route_node,
                action,
                attempt_id=args.attempt_id,
                parent_attempt_id=args.parent_attempt_id or "",
                harness="opencode",
                fallback_hop=args.fallback_hop,
                fallback_ordinal=args.fallback_ordinal,
                assignment_sha256=args.assignment_sha256,
            )
        except DispatchContractError as exc:
            return fail(exc.reason, 65, detail=exc.detail, child_spawned="0")
    prompt_name = (
        f"{args.slug}.{args.command_attempt_id}.opencode.prompt.txt"
        if args.command_attempt_id
        else f"{args.slug}.opencode.prompt.txt"
    )
    prompt_path = log_dir / prompt_name
    log_name = (f"{args.slug}.{args.command_attempt_id}.opencode.jsonl"
                if args.command_attempt_id else f"{args.slug}.opencode.jsonl")
    log_path = log_dir / log_name
    args.log_path = log_path
    command = shell_command(args, prompt_path, log_path)

    governor = ROOT / "utilities" / "model-worker-governor.py"
    try:
        governor_root = resolve_model_governor_root(args.artifact_root)
    except DispatchContractError as exc:
        return fail(exc.reason, 73, detail=exc.detail, child_spawned="0")
    reservation_token = ""
    args.replica_batch_reservation = {}
    # How this attempt's completion is delivered, before its row records it.
    args.resolved_completion_delivery = "session-resume-supervised" if _supervised_owner(args) else "one-shot"
    args.completion_delivery_reason = "ok" if _supervised_owner(args) else "not-applicable"
    if action in ("register", "start"):
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
            "AGENT_DISPATCH_WORKER_MODE": args.worker_mode or "",
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
            "AGENT_REVIEW_OUTPUT": (
                args.review_output_binding["output_path"]
                if args.review_output_binding else ""
            ),
            "AGENT_REVIEW_CYCLE_ID": (
                args.review_output_binding["cycle_id"]
                if args.review_output_binding else ""
            ),
            "AGENT_REVIEW_PRODUCER_ID": (
                args.review_output_binding["producer_id"]
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
            # Same rule as the other two adapters: a worker inherits AGENT_HOME
            # at the managed release; keep its bytecode out of that tree.
            **bytecode_cache_env(),
            **parent_completion.worker_runtime_identity("opencode"),
            "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
            "AGENT_DISPATCH_CURRENT_SANDBOX": "adapter-default",
            **stage_session_environment(args),
            "AGENT_DISPATCH_COMPLETION_MODE": (
                "supervised" if _supervised_owner(args) else "poll"
            ),
            "OPENCODE_CONFIG_CONTENT": args.opencode_config_content,
            **args.nested_runtime_env,
            # Headless liveness contract: the OpenCode runtime child exposes
            # the dispatch slug to the plugin, which records a plugin-load
            # marker at init and touches <log_dir>/<slug>.heartbeat on every
            # session.idle event. dispatch-liveness.py inspects both as a
            # secondary alive signal independent of the OpenCode SQLite mtime.
            "OPENCODE_DISPATCH_SLUG": args.slug,
        }
        if _supervised_owner(args):
            lease = supervisor_lease_path(jobs, args.attempt_id)
            dispatch_env["AGENT_DISPATCH_COMPLETION_STATE_FILE"] = str(lease.with_suffix(".json"))
            dispatch_env["AGENT_DISPATCH_SUPERVISOR_LEASE_FILE"] = str(lease)
        else:
            dispatch_env.pop("AGENT_DISPATCH_COMPLETION_STATE_FILE", None)
            dispatch_env.pop("AGENT_DISPATCH_SUPERVISOR_LEASE_FILE", None)
        if args.worker_role:
            dispatch_env["AGENT_DISPATCH_WORKER_ROLE"] = args.worker_role
        else:
            dispatch_env.pop("AGENT_DISPATCH_WORKER_ROLE", None)
        if args.execution_access_grant is not None:
            dispatch_env["AGENT_DISPATCH_EXECUTION_ACCESS_FILE"] = str(args.execution_access_grant.source_path)
        if args.unit:
            dispatch_env["AGENT_DISPATCH_UNIT"] = args.unit
        else:
            dispatch_env.pop("AGENT_DISPATCH_UNIT", None)
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
            "runtime_sandbox": "adapter-default",
        }
        from dispatch_capacity_evidence import launch_scope
        launch_metadata.update(launch_scope("opencode", dispatch_env))
        grant_route_id, grant_route_hash = args.route_id, args.route_hash
        if not grant_route_id and not grant_route_hash and args.owner_route_binding:
            grant_route_id = args.owner_route_binding.route_id
            grant_route_hash = args.owner_route_binding.route_hash
        if grant_route_id and grant_route_hash:
            effective_path, effective_sha256 = publish_effective_grant(
                jobs=jobs, attempt_id=args.attempt_id, route_id=grant_route_id,
                route_hash=grant_route_hash, runtime="opencode", sandbox="adapter-default",
                grant=args.execution_access_grant, default_writable_roots=default_roots,
                network_allowed=False,
            )
            launch_metadata["execution_access_effective_file"] = str(effective_path)
            launch_metadata["execution_access_effective_sha256"] = effective_sha256
        if getattr(args, "nested_runtime_env", None):
            launch_metadata["opencode_runtime_dir"] = str(
                Path(args.nested_runtime_env["XDG_DATA_HOME"]).parent)
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
                # The claimed row says nothing about whether its process still
                # runs; the shared core reads that from the process itself.
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
            outcome = adapter_launch_failure_outcome(jobs, args.attempt_id, exc.reason)
            annotate_attempt_row(jobs, args.attempt_id, {"launch_outcome": outcome})
            cancel_governor_reservation(governor, governor_root, reservation_token)
            reason = (
                "summary-owner-launch-failed"
                if exc.reason.startswith("attempt-pre-release-")
                else "launch-error"
            )
            close_job_row(jobs, args.slug, args.worktree, reason, "", args.attempt_id)
            return fail(exc.reason, 73, detail=exc.detail, attempt_id=args.attempt_id)
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
        start_ticks = launch_metadata.get("pid_start", "")
        try:
            args.governor_reservation = wait_governor_reservation_claim(
                governor, governor_root, reservation_token, proc,
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

        else:
            # SD-15: detached launches retain the short early-death watch.
            death = watch_early_death(proc, log_path, args.early_exit_watch)
            if death:
                reason, reset = death
                close_job_row(jobs, args.slug, args.worktree, reason, reset, args.attempt_id)
                if reason != "capacity":
                    write_reset_cache(agent_home, "opencode", reason, reset, jobs)
                args.early_death = (reason, reset)

    print("check=ok")
    print("adapter=opencode")
    print("runtime_surface=opencode-run-headless")
    print(f"status={action}")
    print(f"worktree={args.worktree}")
    print(f"artifact_root={args.artifact_root}")
    print("artifact_write_scope=canonical-only")
    print("external_directory_permission=scoped-allow")
    print(f"slug={args.slug}")
    print(f"capability={args.capability}")
    print(f"capability_mode={args.capability_mode}")
    print(f"worker_mode={args.worker_mode or '-'}")
    print(f"qa={args.qa}")
    print(f"intensity={args.intensity}")
    print(f"dispatch_depth={args.dispatch_depth}")
    print(f"eligibility_probe={getattr(args, 'eligibility_probe', None) or '-'}")
    print(f"parent={args.parent_slug or '-'}")
    print(f"parent_session_id={args.parent_session_id or '-'}")
    print(f"parent_attempt_id={args.parent_binding.attempt_id if getattr(args, 'parent_binding', None) else '-'}")
    args.resolved_completion_delivery = "session-resume-supervised" if _supervised_owner(args) else "one-shot"
    for line in launch_receipt.completion_lines(args):
        print(line)
    print(f"worker_role={args.worker_role or '-'}")
    print(f"worker_type={args.worker_type}")
    print(f"assigned_contract={args.assigned_contract}")
    print(f"unit={args.unit or '-'}")
    print(f"owner={args.capability_owner or '-'}")
    print(f"owner_harness={args.owner_harness or '-'}")
    print(f"route_file={args.route_file or '-'}")
    print(f"route_validation={getattr(args, 'route_validation', None) or '-'}")
    print(f"agent={args.agent}")
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
    print(f"variant={settings['variant']}")
    leg_class, auxiliary_check = _route_node_leg_fields(args)
    print(f"leg_class={leg_class}")
    print(f"auxiliary_check={auxiliary_check}")
    print(f"parent_cross={os.environ.get('AGENT_DISPATCH_PARENT_CROSS', '-')}")
    print(f"sole_gate={os.environ.get('AGENT_DISPATCH_SOLE_GATE', '-')}")
    for line in launch_receipt.attempt_lines(
            args, jobs=jobs, registry_source=registry.source, action=action,
            launch_state=attempt_launch_state(jobs, args.attempt_id, claimed=args.attempt_claimed, action=action)):
        print(line)
    print(f"prompt_source={prompt_source}")
    print(f"prompt_file={prompt_path}")
    print(f"log_file={log_path}")
    print(f"command={command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
