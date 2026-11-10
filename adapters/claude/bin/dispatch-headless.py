#!/usr/bin/env python3
"""Claude headless dispatch registration/launch wrapper."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import secrets
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path



ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utilities"))
from worker_runtime_home import prepare_worker_home, claude_worker_arguments
from dispatch_contract import (
    _atomic_registry_replace,
    workflow_completion_receipt,  # noqa: E402
    DispatchContractError,
    foreground_review_launch_identity,
    GROUP_REAP_PROOF,
    GOVERNOR_RESERVATION_ENV,
    REPLICA_RESERVATION_ROW_KEYS,
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
    dispatch_state_root,
    PRELAUNCH_PROCESS_BLOCK_REASONS,
    ROUTE_STATE_REFUSAL_REASONS,
    route_state_next_action,
    SUPERVISOR_LEASE_KIND,
    ensure_global_registry_writable,
    headless_attempt_policy,
    launch_orphan_watch,
    launch_reap_watch,
    seal_foreground_result,
    new_attempt_id,
    parse_registry_metadata,
    parent_attempt_binding_is_live,
    resolve_global_registry,
    resolve_dispatch_state_root,
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
from dispatch_summary import launch_summary_owner  # noqa: E402
from artifact_producer import (  # noqa: E402
    ProducerError,
    bind_owner_launch,
    prepare_review_output_binding,
    review_lease_acquire,
)
from dispatch_completion_join import materialize_after_terminal_close  # noqa: E402
from foreground_terminal import settle_foreground_exit, terminal_evidence as foreground_terminal_evidence  # noqa: E402
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
    validate_manifest_mode_axes,
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
from model_config import (  # noqa: E402
    ModelConfigError, headless_model_refusal, inheritance_refusal, main_session_only_models,
    main_session_only_state, resolve_config, restricted_model,
)
from codex_dispatch_terminal import REVIEW_BLOCKING_NOTE, inspect_terminal_attempt  # noqa: E402
from codex_managed_dispatch import (  # noqa: E402
    MANAGED_PARENT_DELIVERY,
    ManagedDispatchError,
    probe_managed_codex_parent,
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
    pin_target,
)
INTENSITY_LEVELS = {"direct", "quick", "standard", "strong", "thorough", "adversarial"}
# Verification rigor is derived from intensity via resolve_qa
# (dispatch_mode_contract.py, the single qa/intensity SoT — CONVENTIONS §1.1).
# `--qa` is no longer a user-facing axis; it is optional and, when omitted,
# derived below. The jobs.log `qa=` field is retained (derived value) for
# fleet-collector compatibility.

# SD-15 (OPERATIONS §5.10 ⑨): limit/auth/capacity deaths are classified by the one shared
# table (route_authority), the same at launch and in liveness for every harness.
from route_authority import DEATH_PATTERNS, scan_anchored_death, scan_death  # noqa: E402,F401


def parser() -> argparse.ArgumentParser:
    p = DISPATCH_MAIN.base_parser(__doc__)
    p.add_argument(
        "--owner-harness",
        default=os.environ.get("AGENT_DISPATCH_OWNER_HARNESS")
        or ("codex" if os.environ.get("CODEX_THREAD_ID") else "claude"),
    )
    DISPATCH_MAIN.add_launch_arguments(p)
    p.add_argument("--parent-harness", default=parent_completion.default_parent_harness("claude"))
    DISPATCH_MAIN.add_parent_runtime_arguments(p)
    p.add_argument("--profile", help="profiles/<name>.yaml masked config home to attach via CLAUDE_CONFIG_DIR")
    p.add_argument("--model-role", default=os.environ.get("CLAUDE_DISPATCH_MODEL_ROLE"))
    p.add_argument("--model-profile", default=os.environ.get("CLAUDE_DISPATCH_MODEL_PROFILE"))
    p.add_argument("--pair-model-profile")
    p.add_argument("--model", default=os.environ.get("CLAUDE_DISPATCH_MODEL"))
    p.add_argument("--effort", default=os.environ.get("CLAUDE_DISPATCH_EFFORT"))
    p.add_argument(
        "--permission-mode",
        choices=("config", *PERMISSION_POSTURES),
        default=os.environ.get("CLAUDE_DISPATCH_PERMISSION_MODE", "config"),
        help="registered headless permission posture (core/OPERATIONS.md §5.10): "
        "`config` reads headless.claude_permission_mode from dispatch-defaults.yaml "
        "(shipped default bypass); `bypass` pins --permission-mode bypassPermissions; "
        "`allowlist` keeps the runtime start mode and pre-approves only harness utilities",
    )
    p.add_argument(
        "--completion-delivery",
        choices=("auto", "supervised", "poll"),
        default=os.environ.get("CLAUDE_DISPATCH_COMPLETION_DELIVERY", "auto"),
        help="standard+ owner completion bridge; auto prefers same-session resume",
    )
    p.add_argument(
        "--allow-unmanaged-parent-poll",
        action="store_true",
        help="operator-only low-level recovery override; dispatch-owner forbids it",
    )
    p.add_argument(
        "--inherit-model-settings",
        action="store_true",
        help="legacy input retained for typed rejection; registered headless Claude dispatch requires an explicit eligible role/model",
    )
    p.add_argument(
        "--early-exit-watch",
        type=float,
        default=float(os.environ.get("CLAUDE_DISPATCH_EARLY_EXIT_WATCH", "8")),
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
        default=float(os.environ.get("CLAUDE_DISPATCH_FOREGROUND_TIMEOUT", "14400")),
        help="maximum child lifetime for foreground-scoped launch; non-positive clamps to the safe default (never waits indefinitely)",
    )
    add_stage_session_arguments(p)
    return p


# One copy, shared by the three wrappers (`route_authority`); the name stays for readers.
completion_gate_fail_fields = route_authority.completion_gate_fail_fields


ModelSelectionError = WRAPPER_COMMON.ModelSelectionError


def role_map(role: str) -> dict[str, str]:
    result = subprocess.run([str(ROOT / "adapters" / "claude" / "bin" / "model-map.sh"), role], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise ModelSelectionError("invalid-dispatch-model-role", (result.stderr or result.stdout).strip())
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return {"model": fields["exact_model_id"], "effort": fields["reasoning"]}


def _model_policy() -> dict[str, str]:
    return WRAPPER_COMMON.model_policy("claude", error=ModelSelectionError)


def _main_session_only_model(model: str) -> bool:
    return WRAPPER_COMMON.main_session_only_model(model, _model_policy)


def _main_session_only_policy_state() -> str:
    return WRAPPER_COMMON.main_session_only_policy_state(_model_policy, error=ModelSelectionError)


def _require_headless_model(model: str, source: str) -> None:
    return WRAPPER_COMMON.require_headless_model(
        model, source, policy=_model_policy, error=ModelSelectionError,
    )


def _model_config_state() -> tuple[str, str]:
    return WRAPPER_COMMON.model_config_state("claude")


def _select_model_settings(args: argparse.Namespace) -> dict[str, str]:
    return DISPATCH_MAIN.select_model_settings(
        _ADAPTER, args, harness="claude", label="Claude", knob="effort",
        role_settings=_role_model_settings,
    )


def _role_model_settings(args: argparse.Namespace) -> dict[str, str]:
    # 2026-07-22 사용자 원칙: 역할의 티어(모델)는 고정, 상황별 조절은 effort만.
    # --model-role + --effort = the sanctioned situational-tuning surface.
    fields = role_map(args.model_role)
    _require_headless_model(fields["model"], f"role:{args.model_role}")
    effort = args.effort or fields["effort"]
    source = "role+effort" if args.effort else "role"
    if args.model_role.startswith("deep ") and effort in ("medium", "low"):
        # 사다리(2026-07-22): deep 기본 xhigh, 아래 단계는 high; medium 이하는
        # '정말 쉬운 것만'의 예외적 선택 — 허용하되 조심을 상기시킨다.
        print(
            f"caution=deep-tier-low-effort role={args.model_role!r} effort={effort} "
            "(step-down is high; medium/low is for genuinely easy work only)",
            file=sys.stderr,
        )
    return {
        "source": source, "role": args.model_role, "profile": "unsealed",
        "tier": "legacy", "granularity": "legacy", "model": fields["model"], "effort": effort,
    }


def resolve_model_settings(args: argparse.Namespace) -> dict[str, str]:
    context = WRAPPER_COMMON.prepare_pair_model_rotation(args, harness="claude")
    selection_args = args
    if context.get("status") == "rotated":
        import copy
        selection_args = copy.copy(args)
        selection_args.model_profile = context["profile"]
    selected = _select_model_settings(selection_args)
    if context.get("status") == "rotated" and selected.get("source") == "profile":
        selected["source"] = "profile+diversity"
    return WRAPPER_COMMON.attach_context_window(selected, "claude", error=ModelSelectionError)


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
        f"worktree={args.worktree}\n",
        "generated",
    )


def dispatch_prompt(
    args: argparse.Namespace,
    task_input: tuple[str, str] | None = None,
) -> tuple[str, str]:
    task, source = task_input or task_prompt(args)
    bootstrap = DISPATCH_MAIN.bind_worker_contract(_ADAPTER, args, profile_type=profile_worker_type(ROOT, args.profile))
    profile_note = (
        f"masked specialization profile={args.profile}; its CLAUDE.md contains only the runtime attach layer and selected specialization"
        if args.profile
        else "profile=-"
    )
    heartbeat = runtime_progress_prompt(args)
    completion_delivery = getattr(args, "resolved_completion_delivery", "poll-fallback")
    supervised = completion_delivery == "session-resume-supervised"
    owner_standard_plus = (
        args.intensity in _STANDARD_PLUS_INTENSITY and args.worker_type == "owner"
    )
    sync_wait_clause = ""
    if owner_standard_plus and supervised:
        sync_wait_clause = supervised_owner_prompt()
    elif owner_standard_plus:
        sync_wait_clause = (
            "Checked polling fallback (Claude session-resume bridge unavailable): poll "
            "synchronously with utilities/dispatch-wait.sh in the current turn until terminal, "
            "then harvest. This fallback is not runtime completion parity (OPERATIONS.md §5.10).\n\n"
        )
    ending = worker_ending_prompt(
        supervised_owner=supervised and args.worker_type == "owner",
    )
    return (
        f"{sync_wait_clause}"
        f"{bootstrap}\n"
        f"{DISPATCH_MAIN.dispatch_metadata_prompt(_ADAPTER, args)}"
        f"- route_state: {'consume the immutable record already validated by the wrapper' if args.route_file else 'validated dispatch metadata'}\n"
        f"- {profile_note}\n\n"
        "Claude realization:\n"
        "- The wrapper validates capability mode, worker unit/mode, route/scope, and the masked profile before launch. Re-run route validation only for a safety recheck.\n"
        f"{contract_read_prompt(args, 'claude')}"
        "- An owner has no worker mode and must not load any unit persona.\n"
        "- Owner workers use the inherited registry, launch checked adapter wrappers directly, consume typed completion receipts, harvest artifacts, and close rows; stage/review/support workers do not dispatch.\n\n"
        f"{DISPATCH_MAIN.assignment_prompt_tail(_ADAPTER, args, task, heartbeat=heartbeat, commit_clause=commit_policy.prompt_clause(args))}"
        f"{ending}",
        source,
    )


# SD-71: names proven by the live `claude -p --help`/tool-enumeration probe
# (_internal/probe_claude_tools.txt, captured 2026-07-19 against Claude Code
# 2.1.215) to be asynchronous wait/scheduling/notification tools — the exact
# class the SD-14/78 contract forbids any registered Claude print turn from
# reaching. A supervised owner resumes only through its process supervisor;
# these runtime-native callbacks are still unrelated and unsafe. This was first reproduced in a conductor and then in
# an execute stage. Never includes Bash or any synchronous tool;
# dispatch-wait.sh stays available only to the reported polling fallback. Update only from fresh probe
# evidence, never from assumption.
PROVEN_ASYNC_DENY = (
    "Monitor", "ScheduleWakeup", "CronCreate", "CronDelete", "CronList",
    "PushNotification", "RemoteTrigger",
)

# The injected completion-delivery prose remains specific to standard+ owners;
# the runtime deny above is broader because every print turn must avoid callbacks.
_STANDARD_PLUS_INTENSITY = {"standard", "strong", "thorough", "adversarial"}


def _bind_runtime_parent(args: argparse.Namespace) -> None:
    """Bind a cross-harness direct child to the actual Codex caller runtime."""

    route_authority.bind_runtime_parent(args, honor_force=False)


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


# core/OPERATIONS.md §5.10 "Registered headless permission posture". A
# registered `claude -p` turn without a start flag inherits the runtime's
# starting permission mode (user `permissions.defaultMode`, else the plan's
# built-in `auto` with the classifier). A non-interactive run has no prompt to
# fall back to when the classifier blocks repeatedly, so a headless owner dies
# as a cascade of denials (observed 2026-09-02 att-e836ebd2...). The wrapper
# therefore pins the start mode itself and records the applied posture on the
# attempt row as evidence.
PERMISSION_POSTURES = ("bypass", "allowlist")
# `allowlist` pins acceptEdits: file edits inside the working directory and
# the added artifact root are auto-approved without the auto-mode classifier,
# and everything else is decided by the explicit allow rules below -- so a
# denied command is a plain tool refusal the model can route around, never a
# classifier cascade.
PERMISSION_MODE_FLAG_VALUES = {
    "bypass": "bypassPermissions", "allowlist": "acceptEdits", "plan": "plan",
}

# A headless session ends when its turn ends, so a Bash call Claude Code moves
# to the background (at the tool timeout, or on request) is lost with it: ~22
# worker logs ended "waiting for the background notification" with no handoff,
# and an owner's backgrounded call later started a turn of its own whose reply
# the supervisor took for the resume answer (2026-09-27). Headless runs keep
# every command in the foreground: backgrounding is always off, a command runs
# 10 minutes unless it asks for more, and at most 50 minutes -- below the
# 1-hour foreground-scoped launch limit and the supervisor's turn timeout.
HEADLESS_BACKGROUND_OFF = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}
HEADLESS_BASH_TIMEOUT_DEFAULTS = {
    "BASH_DEFAULT_TIMEOUT_MS": "600000",
    "BASH_MAX_TIMEOUT_MS": "3000000",
}


def apply_headless_foreground_env(env):
    """Force backgrounding off; default the timeouts (a caller value wins)."""
    env.update(HEADLESS_BACKGROUND_OFF)
    for key, value in HEADLESS_BASH_TIMEOUT_DEFAULTS.items():
        env.setdefault(key, value)
    return env
# The allow rules a registered owner/worker contractually needs: its own
# gate-recording utilities, read-only git, the harness test runners, and
# Edit (which also governs Write) scoped to the worktree and the artifact
# root. Never a bare `Bash` grant.
HARNESS_ALLOWLIST_UTILITIES = (
    "capability-route.py",
    "dispatch-node.py",
    "dispatch-batch.py",
    "dispatch-owner.py",
    "dispatch-harvest.py",
    "stage-dispatch-fallback.py",
    "artifact_producer.py",
    "spec-transaction.py",
)
_READ_ONLY_GIT_RULES = (
    "Bash(git status *)", "Bash(git status)", "Bash(git diff *)", "Bash(git diff)",
    "Bash(git log *)", "Bash(git show *)", "Bash(git rev-parse *)", "Bash(git branch *)",
    "Bash(git worktree list *)", "Bash(git worktree list)",
)
_OWNER_COMMIT_GIT_RULES = ("Bash(git add *)", "Bash(git commit *)")
_TEST_RUNNER_RULES = (
    "Bash(python3 -m unittest *)", "Bash(python3 -m pytest *)", "Bash(pytest *)",
    "Bash(python3 tools/run-tests.py *)",
)
_MANAGED_SETTINGS_PATHS = (
    Path("/etc/claude-code/managed-settings.json"),
    Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
)


def _settings_permissions(paths):
    """Yield the `permissions` object of every readable settings file, in the given order."""
    for path in paths:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        permissions = payload.get("permissions") if isinstance(payload, dict) else None
        if isinstance(permissions, dict):
            yield permissions


def _settings_disable_bypass(paths) -> bool:
    """True when ANY settings scope sets permissions.disableBypassPermissionsMode=disable.

    The settings reference lists the key as honoured from any file (managed,
    user, project, project-local), so every scope is read; precedence does
    not matter for a key whose only effect is to forbid.
    """
    return any(p.get("disableBypassPermissionsMode") == "disable" for p in _settings_permissions(paths))


def _settings_default_mode(paths) -> str:
    """The `permissions.defaultMode` the runtime would inherit (highest precedence first), or ""."""
    for permissions in _settings_permissions(paths):
        value = permissions.get("defaultMode")
        if isinstance(value, str) and value:
            return value
    return ""


def _claude_settings_paths(worktree: str | Path | None = None) -> tuple[Path, ...]:
    """Settings files in precedence order: managed, project-local, project, user.

    Project scope is the worktree the registered session runs in (a linked
    worktree is its own repository root for Claude Code); user scope honours
    CLAUDE_CONFIG_DIR.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    user_home = Path(config_dir) if config_dir else Path.home() / ".claude"
    project: tuple[Path, ...] = ()
    if worktree:
        project_dir = Path(str(worktree)) / ".claude"
        project = (project_dir / "settings.local.json", project_dir / "settings.json")
    return (*_MANAGED_SETTINGS_PATHS, *project, user_home / "settings.json")


def _allowlist_rules(agent_home: Path, worktree: str | Path | None, artifact_root: str | Path | None,
                     worker_type: str | None, *, commit_expected: bool = False,
                     task_publish_scope: dict[str, str] | None = None,
                     merge_observation_only: bool = False) -> tuple[str, ...]:
    """The explicit allow rules a registered worker/owner contractually needs (never bare Bash)."""
    rules: list[str] = []
    if not merge_observation_only:
        for name in HARNESS_ALLOWLIST_UTILITIES:
            utility = agent_home / "utilities" / name
            rules += [
                f"Bash(python3 {utility} *)",
                f"Bash(python3 {utility})",
                f"Bash({utility} *)",
                f"Bash({utility})",
            ]
        rules += list(_READ_ONLY_GIT_RULES)
    else:
        rules += ["Bash(git status *)", "Bash(git status)", "Bash(git diff *)", "Bash(git diff)",
                  "Bash(git log *)", "Bash(git show *)", "Bash(git rev-parse *)",
                  "Bash(git branch --show-current)", "Bash(git worktree list *)",
                  "Bash(git worktree list)"]
    if commit_expected and not merge_observation_only:
        # Route-bound single-session mutation workers may commit when the sealed
        # node says so; declared slices never receive this allowlist.
        rules += list(_OWNER_COMMIT_GIT_RULES)
    if task_publish_scope is not None and not merge_observation_only:
        rules += [f"Bash({command})" for command in commit_policy.task_publish_commands(task_publish_scope)]
    if not merge_observation_only:
        rules += list(_TEST_RUNNER_RULES)
    # Edit rules also govern Write (Write path rules are never consulted);
    # `//` anchors an absolute path. Reads inside the working directory and
    # the `--add-dir` artifact root need no rule.
    scopes = (artifact_root,) if merge_observation_only else (worktree, artifact_root)
    for scope in scopes:
        if scope:
            rules.append(f"Edit(//{str(Path(str(scope))).lstrip('/')}/**)")
    return tuple(rules)


def _configured_permission_posture() -> tuple[str, str]:
    """Read headless.claude_permission_mode from dispatch-defaults.yaml.

    An unreadable or invalid config falls back to the shipped default rather
    than refusing the launch: the posture is a runtime guard for the worker,
    not a config gate, and the reason word says the file was not honoured.
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "dispatch_defaults", ROOT / "utilities" / "dispatch-defaults.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config_path = module.default_config_path()
        config = module.load_and_validate(config_path, module.default_topology_path())
        policy = module.query_headless_policy(config)
    except Exception:  # noqa: BLE001 -- posture never blocks a launch on config trouble
        return "bypass", "config-invalid-shipped-default"
    return str(policy["claude_permission_mode"]), str(policy["source"])


def resolve_permission_posture(args: argparse.Namespace) -> dict[str, object]:
    """Resolve the applied permission posture.

    Returns {mode, reason, mode_flag, allowed_tools, inherited_default_mode}.
    `allowed_tools` is emitted in BOTH postures: under bypass the rules have no
    effect, but if the runtime demotes the session (a disable set in a scope
    this resolver could not read) the worker still holds what it needs.
    `inherited_default_mode` is the settings `defaultMode` the session would
    have started in without the pin -- recorded so the ledger names the real
    inherited mode, not a guess about the plan default.
    """
    requested = getattr(args, "permission_mode", None) or "config"
    if requested == "config":
        mode, reason = _configured_permission_posture()
    else:
        mode, reason = requested, "launch-explicit"
    worktree = getattr(args, "worktree", None)
    settings_paths = _claude_settings_paths(worktree)
    projection = (
        commit_policy.launch_permission_projection(args)
        if isinstance(getattr(args, "_launch_permission_projection", None), dict)
        else commit_policy.capture_launch_permission_projection(args)
    )
    args.merge_observation_only = bool(projection["observation_only"])
    if args.merge_observation_only:
        mode, reason = "plan", "active-git-operation"
    elif mode == "bypass":
        # Where bypass cannot apply the runtime refuses to start (root/sudo)
        # or does not enter bypass (disableBypassPermissionsMode in any
        # settings scope); demote to the allowlist posture with a typed reason
        # instead of dying opaque or attesting a posture that never applied.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            mode, reason = "allowlist", "root-refuses-bypass"
        elif _settings_disable_bypass(settings_paths):
            mode, reason = "allowlist", "settings-disable-bypass"
    agent_home = Path(str(getattr(args, "agent_home", "") or ROOT))
    worker_type = getattr(args, "worker_type", None)
    commit_expected = bool(projection["may_commit"])
    args.task_publish_scope = projection["task_publish_scope"]
    allowed = _allowlist_rules(
        agent_home, worktree, getattr(args, "artifact_root", None), worker_type,
        commit_expected=commit_expected,
        task_publish_scope=args.task_publish_scope,
        merge_observation_only=args.merge_observation_only,
    )
    return {
        "mode": mode,
        "reason": reason,
        "mode_flag": PERMISSION_MODE_FLAG_VALUES[mode],
        "allowed_tools": allowed,
        "inherited_default_mode": _settings_default_mode(settings_paths) or "-",
    }


_foreground_terminal_evidence = foreground_terminal_evidence  # the shared foreground close's evidence


def _permission_posture(args: argparse.Namespace) -> dict[str, object]:
    posture = getattr(args, "resolved_permission_posture", None)
    if posture is None:
        posture = resolve_permission_posture(args)
        args.resolved_permission_posture = posture
    return posture


def _async_deny_tools(args: argparse.Namespace) -> tuple[str, ...]:
    """SD-71/78: deny proven-fatal async tools for every Claude print turn."""
    return PROVEN_ASYNC_DENY


def _read_only_projection(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """The route contracts and request's read-only roots as Claude realizes them
    (`harness_capabilities` access means): each root is an `--add-dir`, and the
    roots the grant keeps unwritten get an Edit deny rule (Edit rules also govern Write)."""
    grant = getattr(args, "execution_access_grant", None)
    home = getattr(args, "agent_home", None)
    source = tuple(dict.fromkeys((
        *(() if home is None else route_authority.contract_read_roots(home)),
        *getattr(args, "contract_read_roots", ()),
    )))
    writable = [Path(args.worktree).resolve(), *(grant.writable_roots if grant else ())]
    if getattr(args, "artifact_root", None) is not None:
        writable.append(Path(args.artifact_root).resolve())
    source = tuple(root for root in source if root.is_dir() and not any(
        root.resolve().is_relative_to(area) or area.is_relative_to(root.resolve())
        for area in writable))
    reads = tuple(dict.fromkeys((*source, *(grant.read_roots if grant else ()))))
    denied = tuple(dict.fromkeys((*source, *(grant.unwritable_read_roots if grant else ()))))
    return ([str(root) for root in reads],
            [f"Edit(//{str(root).lstrip('/')}/**)" for root in denied])


def _async_wait_policy(args: argparse.Namespace) -> str:
    return WRAPPER_COMMON.async_wait_policy(
        args, supervised=getattr(args, "resolved_completion_delivery", "") == "session-resume-supervised",
        deny_tools=_async_deny_tools,
    )


def _completion_owner(args: argparse.Namespace) -> bool:
    return WRAPPER_COMMON.completion_owner(args, registered=_registered_owner)


# SD-OPEN-63: `claude --help` piped through a PIPE truncates at a buffer boundary
# (8192/16384 bytes measured) before `--session-id` appears, even though the
# vendor docs both document the flag and state help does not enumerate every
# flag. A PIPE-truncated substring match therefore reported "unsupported" for a
# runtime that supports resume. Capturing help into a regular file removes the
# PIPE buffer limit; the completeness sentinel below still guards against any
# other source of a short/partial capture (timeout, non-zero exit, vendor
# output change) before a flag absence is trusted as a real "unsupported".
_CLAUDE_HELP_COMPLETENESS_SENTINEL = "update|upgrade"
_CLAUDE_HELP_MIN_COMPLETE_BYTES = 20000


class ClaudeResumeProbe:
    """Result of probing `claude --help` for `--resume`/`--session-id` support.

    `status` is one of `supported`/`unsupported`/`indeterminate`. `unsupported`
    is only returned when the captured help is provably complete (sentinel +
    minimum size) -- an incomplete or failed capture is always
    `indeterminate`, never treated as evidence of non-support.
    """

    __slots__ = ("status", "reason", "exit_code", "stdout_bytes", "flags_seen", "source")

    def __init__(self, status, reason, exit_code, stdout_bytes, flags_seen, source):
        self.status = status
        self.reason = reason
        self.exit_code = exit_code
        self.stdout_bytes = stdout_bytes
        self.flags_seen = flags_seen
        self.source = source


def probe_claude_session_resume() -> ClaudeResumeProbe:
    if shutil.which("claude") is None:
        return ClaudeResumeProbe("indeterminate", "binary-absent", None, 0, (), "help-file")
    try:
        # An anonymous regular file preserves truncation-resistant capture and
        # closes storage creation/read errors inside the same typed probe path.
        with tempfile.TemporaryFile(mode="w+b") as sink:
            result = subprocess.run(
                ["claude", "--help"],
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                timeout=10,
                check=False,
            )
            sink.seek(0)
            raw = sink.read()
    except subprocess.TimeoutExpired:
        return ClaudeResumeProbe("indeterminate", "timeout", None, 0, (), "help-file")
    except OSError:
        return ClaudeResumeProbe("indeterminate", "probe-error", None, 0, (), "help-file")
    stdout_bytes = len(raw)
    if result.returncode != 0:
        return ClaudeResumeProbe(
            "indeterminate", "nonzero-exit", result.returncode, stdout_bytes, (), "help-file"
        )
    text = raw.decode("utf-8", errors="replace")
    flags_seen = tuple(flag for flag in ("--resume", "--session-id") if flag in text)
    complete = (
        stdout_bytes >= _CLAUDE_HELP_MIN_COMPLETE_BYTES
        and _CLAUDE_HELP_COMPLETENESS_SENTINEL in text
    )
    if not complete:
        return ClaudeResumeProbe(
            "indeterminate", "help-truncated", result.returncode, stdout_bytes, flags_seen, "help-file"
        )
    if "--resume" in flags_seen and "--session-id" in flags_seen:
        return ClaudeResumeProbe(
            "supported", "ok", result.returncode, stdout_bytes, flags_seen, "help-file"
        )
    return ClaudeResumeProbe(
        "unsupported",
        "flags-absent-in-complete-help",
        result.returncode,
        stdout_bytes,
        flags_seen,
        "help-file",
    )


def resolve_completion_delivery(args: argparse.Namespace) -> str:
    """SD-OPEN-63 (D-2): a probe result of `indeterminate` never silently
    degrades to `poll-fallback` -- it is a typed refusal so a `standard+` auto
    owner never loses supervision without a preserved reason. Only a provably
    complete negative probe (`unsupported`) or an explicit operator `poll`
    request produces `poll-fallback`."""
    requested = args.completion_delivery
    if not _registered_owner(args):
        if requested == "supervised":
            raise DispatchContractError(
                "completion-delivery-ineligible",
                "supervised completion is scoped to registered dispatch-depth-1 owners",
            )
        return "one-shot"
    standard_plus = _completion_owner(args)
    if requested == "poll":
        return "poll-fallback" if standard_plus else "one-shot"
    probe = getattr(args, "completion_probe", None)
    if probe is None:
        probe = probe_claude_session_resume()
        args.completion_probe = probe
    if probe.status == "supported":
        args.completion_delivery_reason = "ok"
        return "session-resume-supervised"
    if not standard_plus:
        # A quick owner never loses a launch it had before: without a proven
        # resume surface it runs one-shot, and only an explicit request refuses.
        if requested == "supervised":
            raise DispatchContractError(
                "claude-session-resume-unavailable" if probe.status == "unsupported"
                else "claude-session-resume-indeterminate",
                "claude --help did not expose --session-id and --resume; no owner was launched"
                if probe.status == "unsupported"
                else f"claude --help probe was indeterminate ({probe.reason}); refusing to silently fall back",
            )
        return "one-shot"
    if probe.status == "unsupported":
        args.completion_delivery_reason = "claude-session-resume-unsupported"
        if requested == "supervised":
            raise DispatchContractError(
                "claude-session-resume-unavailable",
                "claude --help did not expose --session-id and --resume; no owner was launched",
            )
        return "poll-fallback"
    raise DispatchContractError(
        "claude-session-resume-indeterminate",
        f"claude --help probe was indeterminate ({probe.reason}); refusing to silently fall back",
    )


def initialize_supervised_owner_input(args: argparse.Namespace, jobs: Path) -> None:
    WRAPPER_COMMON.initialize_owner_input_when(
        args, jobs, supervised=args.resolved_completion_delivery == "session-resume-supervised", input_kind="claude-next-turn")


def completion_lease_path(args: argparse.Namespace) -> Path:
    if not args.attempt_id:
        return dispatch_state_root(args.jobs_path) / "supervisor-state" / "preview-only.lease"
    return supervisor_lease_path(args.jobs_path, args.attempt_id)


_COMPACTION_ENV_KEYS = {
    "window": "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "pct_override": "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
    "disable_auto_compact_env": "DISABLE_AUTO_COMPACT",
    "disable_compact_env": "DISABLE_COMPACT",
    "max_output_env": "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
}


def claude_compaction_inputs(env: dict[str, str]) -> dict[str, str]:
    """Allowlisted inputs of Claude's auto-compaction consumer for this exact launch.

    Claude assigns its settings `env` block over the inherited environment at startup, so
    the home settings value is recorded beside the launch env value; the Fleet collector
    treats a disagreement as unknown instead of choosing one.
    """
    settings_env: dict = {}
    try:
        document = json.loads(
            (Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json").read_text(encoding="utf-8"))
        candidate = document.get("env") if isinstance(document, dict) else None
        settings_env = candidate if isinstance(candidate, dict) else {}
    except (KeyError, OSError, ValueError):
        pass

    def text(value) -> str:
        return "absent" if value is None or value == "" else str(value)[:128]

    metadata: dict[str, str] = {}
    for field, name in _COMPACTION_ENV_KEYS.items():
        key = "claude_context_window_request" if field == "window" else f"claude_compaction_{field}"
        metadata[key] = text(env.get(name))
        metadata[f"claude_settings_{field}"] = text(settings_env.get(name))
    return metadata


def apply_context_window_environment(env: dict[str, str], settings: dict[str, str]) -> dict[str, str]:
    """Set Claude's launch-scoped compaction budget after home projection."""
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = settings["context_window"]
    return env


def _route_declares_terminal_commit_support(route_file: str) -> bool:
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return route.get("runtime_support", {}).get("terminal_commit") is True


def shell_command(args: argparse.Namespace, prompt_path: Path, log_path: Path) -> str:
    prompt_path, log_path = Path(frame_repository.native_path(prompt_path)), Path(frame_repository.native_path(log_path))
    if getattr(args, "resolved_completion_delivery", "one-shot") == "session-resume-supervised":
        command = [
            sys.executable,
            str(ROOT / "utilities" / "claude-session-supervisor.py"),
            "--worktree", args.worktree,
            "--jobs", str(args.jobs_path),
            "--parent-attempt-id", args.attempt_id or "unassigned",
            "--state-file", str(completion_state_path(args)),
            "--lease-file", str(completion_lease_path(args)),
            # Refresh permissions on the existing same-session resume path.
            "--turn-transport", "resume-process",
            "--add-dir", str(args.artifact_root),
        ]
        if getattr(args, "runtime_dir", ""):
            command += ["--add-dir", str(args.runtime_dir)]
        if getattr(args, "report_bundle_root", None) is not None:
            command += ["--add-dir", str(args.report_bundle_root)]
        if getattr(args, "execution_access_grant", None) is not None:
            for writable_dir in args.execution_access_grant.additional_writable_roots:
                command += ["--add-dir", frame_repository.native_path(writable_dir)]
        read_dirs, read_deny = _read_only_projection(args)
        for read_dir in read_dirs:
            command += ["--add-dir", read_dir]
        route = _supervisor_route(args)
        if route:
            command += ["--route-file", route[0], "--route-id", route[1], "--route-hash", route[2]]
            if _route_declares_terminal_commit_support(route[0]):
                command += ["--enable-terminal-commit"]
        if getattr(args, "max_continuations", None) is not None:
            command += ["--max-continuations", str(args.max_continuations)]
        if args.resolved_model_settings["source"] != "inherit":
            command += [
                "--model", args.resolved_model_settings["model"],
                "--effort", args.resolved_model_settings["effort"],
            ]
        for tool in (*_async_deny_tools(args), *read_deny):
            command += ["--disallowed-tool", tool]
        posture = _permission_posture(args)
        command += ["--permission-mode", str(posture["mode_flag"])]
        for tool in posture["allowed_tools"]:
            command += ["--allowed-tool", tool]
        return (
            " ".join(shlex.quote(x) for x in command)
            + f" < {shlex.quote(str(prompt_path))} >> {shlex.quote(str(log_path))} 2>&1"
        )
    # `claude -p` reads the prompt from stdin when no positional prompt is
    # given and prints the response non-interactively, mirroring the codex
    # wrapper's file-piped `codex exec ... < prompt_path` invocation.
    cmd = [
        "claude", "-p", "--add-dir", args.artifact_root,
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
    ]
    if getattr(args, "runtime_dir", ""):
        cmd += ["--add-dir", str(args.runtime_dir)]
    cmd += claude_worker_arguments(getattr(args, "worker_runtime_env", None))
    if getattr(args, "report_bundle_root", None) is not None:
        cmd += ["--add-dir", str(args.report_bundle_root)]
    if getattr(args, "execution_access_grant", None) is not None:
        for writable_dir in args.execution_access_grant.additional_writable_roots:
            cmd += ["--add-dir", frame_repository.native_path(writable_dir)]
    read_dirs, read_deny = _read_only_projection(args)
    for read_dir in read_dirs:
        cmd += ["--add-dir", read_dir]
    if args.resolved_model_settings["source"] != "inherit":
        cmd += [
            "--model",
            args.resolved_model_settings["model"],
            "--effort",
            args.resolved_model_settings["effort"],
        ]
    deny = (*_async_deny_tools(args), *read_deny)
    if deny:
        cmd += ["--disallowedTools", ",".join(deny)]
    posture = _permission_posture(args)
    cmd += ["--permission-mode", str(posture["mode_flag"])]
    if posture["allowed_tools"]:
        cmd += ["--allowedTools", ",".join(posture["allowed_tools"])]
    inner = " ".join(shlex.quote(x) for x in cmd)
    return (
        f"cd {shlex.quote(frame_repository.native_worktree(args.worktree))} && "
        f"{inner} < {shlex.quote(str(prompt_path))} >> {shlex.quote(str(log_path))} 2>&1"
    )


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
        harness="claude", summary_launcher=launch_summary_owner,
        runtime_home=(getattr(args, "worker_runtime_env", None) or {}).get("CLAUDE_CONFIG_DIR"),
    )

def append_job(jobs: Path, args: argparse.Namespace) -> bool:
    posture = _permission_posture(args)
    return WRAPPER_COMMON.append_job(
        jobs, args, harness="claude", runtime_sandbox="adapter-default", effort_key="effort",
        adapter_fields=(
            f",async_wait_policy={_async_wait_policy(args)}"
            f",permission_mode={posture['mode']},permission_mode_reason={posture['reason']}"
            f",permission_inherited_mode={posture['inherited_default_mode']}"
        ),
        claim=claim_attempt_row, marker_gate=completion_marker_gate,
    )


def close_job_row(jobs: Path, slug: str, worktree: str, reason: str, reset: str, attempt_id: str | None = None) -> bool:
    return WRAPPER_COMMON.close_job_row(
        jobs, slug, worktree, reason, reset, attempt_id,
        materialize=materialize_after_terminal_close,
    )


def resolve_agent_home() -> Path:
    # Delegates to the one canonical resolver (utilities/dispatch_contract.py)
    # so this wrapper (writer of jobs.log) and every other consumer -- shell
    # readers (dispatch-liveness / dispatch-wait / the conductor Stop gate),
    # guard hooks, fleet -- agree on ONE root. The prior local reimplementation
    # fell back to ROOT (=worktree) and skipped the XDG `current` pointer,
    # which could split the registry from the agent-home-relative readers
    # (SD-14b② registry gap).
    return _resolve_agent_home()


def build_home_gate(agent_home: Path, profile: str, extra: list[str], reason: str) -> int:
    build_home = agent_home / "tools" / "profile" / "build-home.py"
    result = subprocess.run(
        [sys.executable, str(build_home), profile, *extra],
        cwd=agent_home,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode == 0:
        return 0
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return fail(reason, 3, profile=profile)


def bind_internal_eligibility_probe(args: argparse.Namespace) -> None:
    WRAPPER_COMMON.bind_internal_eligibility_probe(args, "claude")


def validate_dispatch_modes(args: argparse.Namespace) -> int:
    try:
        validate_manifest_mode_axes(
            ROOT,
            args.capability,
            args.capability_mode,
            args.worker_mode,
        )
    except DispatchModeContractError as exc:
        return fail(exc.reason, 64, **exc.fields)
    return 0


def validate_route_record(args: argparse.Namespace) -> int:
    return WRAPPER_COMMON.validate_route_record(
        args, marker_gate=completion_marker_gate, runtime_harness="claude",
    )


# Shared main() phases read this wrapper's names at call time (see dispatch_headless_main).
_ADAPTER = DISPATCH_MAIN.AdapterNamespace(globals())


@frame_repository.process_entry
def main(argv: list[str]) -> int:
    args = parser().parse_args(argv[1:])
    rc = DISPATCH_MAIN.bind_entry(_ADAPTER, args, argv)
    if rc is not None:
        return rc
    action = DISPATCH_MAIN.bind_action(args)
    _bind_runtime_parent(args)
    if args.broker_request_id or args.launch_authority == "ancestor-broker":
        return fail("launch-broker-retired", 76, child_spawned="0")
    # One execution root per launch (OPERATIONS §5.9a): the installed release when it reads this
    # route's contract, else the release the parent tree inherited. Guard, child environment and
    # the row's `launch_home` (the release this launch ran on) all follow it.
    args.agent_home = WRAPPER_COMMON.select_execution_root(
        ROOT, sealed_launch_home(resolve_agent_home()), "claude", argv[1:],
        route_file=args.route_file or os.environ.get("AGENT_OWNER_ROUTE_FILE"))
    bind_parent_completion_delivery(args)
    rc = DISPATCH_MAIN.bind_repository(_ADAPTER, args)
    if rc is not None:
        return rc
    worktree = Path(args.worktree)
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
    rc = validate_dispatch_modes(args)
    if rc != 0:
        return rc
    rc = DISPATCH_MAIN.validate_dispatch_shape(_ADAPTER, args, qa_fields={"child_spawned": "0"})
    if rc != 0:
        return rc
    rc = DISPATCH_MAIN.check_nested_eligibility(_ADAPTER, args, action, "claude")
    if rc is not None:
        return rc
    rc = DISPATCH_MAIN.bind_owner_route(_ADAPTER, args, "claude")
    if rc is not None:
        return rc
    rc=validate_route_record(args)
    if rc != 0: return rc
    rc = DISPATCH_MAIN.expect_replica_batch(_ADAPTER, args, action, "claude")
    if rc is not None:
        return rc
    rc = DISPATCH_MAIN.apply_attempt_policy(_ADAPTER, args, "claude")
    if rc is not None:
        return rc
    rc = DISPATCH_MAIN.resolve_launch_models(_ADAPTER, args, model_error_fields={"child_spawned": "0"})
    if rc is not None:
        return rc
    args.resolved_permission_posture = resolve_permission_posture(args)
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
    if args.start and shutil.which("claude") is None:
        return fail("claude-command-unavailable", 69, worktree=args.worktree)

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
    rc = DISPATCH_MAIN.rebind_replica_batch(_ADAPTER, args, action, "claude")
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
        if args.resolved_completion_delivery == "session-resume-supervised":
            completion_state_path(args)
            completion_lease_path(args)
    except DispatchContractError as e:
        return fail(e.reason, 69, detail=e.detail, child_spawned="0")
    try:
        default_roots = adapter_default_roots(args)
        args.execution_access_grant = route_authority.bind_launch_access(
            args,
            runtime=(
                "claude-supervisor"
                if args.resolved_completion_delivery == "session-resume-supervised"
                else "claude-cli"
            ),
            default_roots=default_roots,
        )
    except ExecutionAccessError as exc:
        return fail(exc.reason, 64, detail=exc.detail, child_spawned="0")
    log_dir = (
        Path(args.log_dir)
        if args.log_dir
        else dispatch_state_root(args.jobs_path) / "logs"
    )
    home_root = dispatch_state_root(args.jobs_path) / "homes"
    instance_dir = home_root / f"{args.slug}.{args.profile}" if args.profile else None

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
    rc = DISPATCH_MAIN.rebind_replica_batch(_ADAPTER, args, action, "claude", assignment_sha256=assignment_sha256)
    if rc is not None:
        return rc
    prompt_name = (
        f"{args.slug}.{args.command_attempt_id}.claude.prompt.txt"
        if args.command_attempt_id
        else f"{args.slug}.claude.prompt.txt"
    )
    prompt_path = log_dir / prompt_name
    log_name = (
        f"{args.slug}.{args.command_attempt_id}.claude.jsonl"
        if args.command_attempt_id
        else f"{args.slug}.claude.jsonl"
    )
    log_path = log_dir / log_name
    args.log_path = log_path
    command = None if action == 'start' else shell_command(args, prompt_path, log_path)

    if action in ("register", "start"):
        Path(frame_repository.execution_path(prompt_path.parent)).mkdir(parents=True, exist_ok=True)
        Path(frame_repository.execution_path(log_path.parent)).mkdir(parents=True, exist_ok=True)

    if action == "start" and args.profile:
        # Gate-first, then create -> register -> launch: a --check failure
        # must not leave an instance home behind (no leak on gate failure).
        rc = build_home_gate(agent_home, args.profile, ["--check"], "profile-check-failed")
        if rc != 0:
            return rc

    claim = DISPATCH_MAIN.claim_registered_attempt(
        _ADAPTER, args, action, jobs=jobs, prompt_path=prompt_path, prompt_text=prompt_text)
    if claim.rc is not None:
        return claim.rc
    governor, governor_root, reservation_token = claim.governor, claim.governor_root, claim.reservation_token

    if action == "start" and args.attempt_claimed:
        env = DISPATCH_MAIN.worker_environment(
            _ADAPTER, args, harness="claude", jobs=jobs, governor_root=governor_root,
            reservation_token=reservation_token,
            permission_env=commit_policy.runtime_permission_context_env(
                args, adapter="claude",
                permission_mode_requested=getattr(args, "permission_mode", None) or "config",
                agent_home=str(args.agent_home),
            ),
            current_sandbox="adapter-default",
            supervised=args.resolved_completion_delivery == "session-resume-supervised",
            extra={"CLAUDE_CODE_CHILD_SESSION": "1"},
        )
        DISPATCH_MAIN.apply_worker_fields(env, args)
        apply_headless_foreground_env(env)
        args.worker_runtime_env = prepare_worker_home(args.agent_home, 'claude', args.worker_type,
                                                       args.attempt_id, env=env, profile=args.profile)
        env.update(args.worker_runtime_env)
        # Apply after the masked worker home and all inherited environment
        # updates so every direct/supervised/resumed native child sees this
        # launch's captured operating budget.
        apply_context_window_environment(env, args.resolved_model_settings)
        instance_dir = Path(env["CLAUDE_CONFIG_DIR"])
        command = shell_command(args, prompt_path, log_path)
        if args.resolved_completion_delivery == "session-resume-supervised":
            env["AGENT_DISPATCH_COMPLETION_STATE_FILE"] = str(
                completion_state_path(args)
            )
            env["AGENT_DISPATCH_SUPERVISOR_LEASE_FILE"] = str(
                completion_lease_path(args)
            )
        else:
            env.pop("AGENT_DISPATCH_COMPLETION_STATE_FILE", None)
            env.pop("AGENT_DISPATCH_SUPERVISOR_LEASE_FILE", None)
        launch_spec = DISPATCH_MAIN.LaunchSpec(
            harness="claude",
            runtime_sandbox=lambda: "adapter-default",
            metadata_head=lambda env: {
                "runtime_home": env["CLAUDE_CONFIG_DIR"],
                **claude_compaction_inputs(env),
            },
            grant_runtime=lambda: (
                "claude-supervisor"
                if args.resolved_completion_delivery == "session-resume-supervised"
                else "claude-cli"
            ),
            grant_kwargs=lambda: {"network_allowed": bool(getattr(args, "task_publish_scope", None))},
        )
        rc = DISPATCH_MAIN.launch_claimed_attempt(
            _ADAPTER, launch_spec, args, jobs=jobs, env=env, command=command, claim=claim,
            log_path=log_path, prompt_path=prompt_path, agent_home=agent_home,
            default_roots=default_roots,
        )
        if rc is not None:
            return rc

    print("check=ok")
    print("adapter=claude")
    print("runtime_surface=claude-print-headless")
    for line in launch_receipt.completion_lines(args):
        print(line)
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
    print(f"effort={settings['effort']}")
    print(f"context_window_requested={settings['context_window']}")
    print(f"context_window_source={settings['context_window_source']}")
    print("context_window_native_application=env:CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    posture = _permission_posture(args)
    print(f"permission_mode={posture['mode']}")
    print(f"permission_mode_reason={posture['reason']}")
    print(f"permission_inherited_mode={posture['inherited_default_mode']}")
    for line in launch_receipt.leg_lines(args, leg_fields=_route_node_leg_fields):
        print(line)
    print(f"profile={args.profile or '-'}")
    print(f"instance_home={instance_dir if instance_dir else '-'}")
    for line in launch_receipt.attempt_lines(
            args, jobs=jobs, registry_source=registry.source, action=action,
            launch_state=attempt_launch_state(jobs, args.attempt_id, claimed=args.attempt_claimed, action=action)):
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
