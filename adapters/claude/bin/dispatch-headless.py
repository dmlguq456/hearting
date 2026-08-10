#!/usr/bin/env python3
"""Claude headless dispatch registration/launch wrapper."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    GROUP_REAP_PROOF,
    GOVERNOR_RESERVATION_ENV,
    REPLICA_RESERVATION_ROW_KEYS,
    anchored_capacity_failure,
    annotate_attempt_row,
    attempt_launch_is_available,
    attempt_launch_state,
    cancel_governor_reservation,
    claim_attempt_row,
    close_attempt_row,
    completion_marker_gate,
    PRELAUNCH_PROCESS_BLOCK_REASONS,
    ensure_global_registry_writable,
    headless_attempt_policy,
    launch_orphan_watch,
    launch_reap_watch,
    new_attempt_id,
    parse_registry_metadata,
    parent_attempt_binding_is_live,
    resolve_global_registry,
    resolve_live_parent_attempt,
    resolve_model_governor_root,
    replica_batch_expectation,
    reserve_governor_token,
    spawn_claimed_attempt,
    validate_nested_eligibility,
    wait_governor_reservation_claim,
)
from dispatch_summary import launch_summary_owner  # noqa: E402
from dispatch_lifecycle import (  # noqa: E402
    DETACHED,
    FOREGROUND_SCOPED,
    LIFECYCLES,
    reconcile_launch_lifecycle,
    wait_foreground,
)
from dispatch_continuation_budget import positive_continuation_limit  # noqa: E402
from dispatch_mode_contract import (  # noqa: E402
    capability_mode_from_route_file,
    DispatchModeContractError,
    normalize_dispatch_modes,
    validate_manifest_mode_axes,
    validate_route_mode_axes,
)
from owner_route_binding import (  # noqa: E402
    OwnerRouteBindingError,
    binding_from_environment,
    validate_runtime_requirements,
)
from worker_bootstrap import (  # noqa: E402
    assigned_contract,
    profile_worker_type,
    render_worker_bootstrap,
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
    ModelProfileError,
    resolve_profile,
    validate_registered_profile,
)
from codex_dispatch_terminal import inspect_terminal_attempt  # noqa: E402
from codex_managed_dispatch import (  # noqa: E402
    MANAGED_PARENT_DELIVERY,
    ManagedDispatchError,
    launch_managed_completion_sidecar,
    probe_managed_codex_parent,
    registered_parent_delivery,
)
QA_LEVELS = {"quick", "light", "standard", "thorough", "adversarial"}
INTENSITY_LEVELS = {"direct", "quick", "standard", "strong", "thorough", "adversarial"}
# Verification rigor is derived from intensity — CONVENTIONS §1.1 mapping table (SoT).
# `--qa` is no longer a user-facing axis; it is optional and, when omitted, derived here.
# The jobs.log `qa=` field is retained (derived value) for fleet-collector compatibility.
QA_FROM_INTENSITY = {
    "direct": "light",
    "quick": "quick",
    "standard": "standard",
    "strong": "standard",
    "thorough": "thorough",
    "adversarial": "adversarial",
}

# SD-15 (OPERATIONS §5.10 ⑨): immediate limit/auth failure patterns shared
# between launch-time early-exit detection and the liveness/wait DEAD verdict.
# Each tuple is (reason, lowercase substring regex); the first match wins.
# utilities/dispatch-liveness.sh intentionally duplicates the list as LIMIT_RE
# across the Python/shell runtime boundary; keep the two synchronized.
DEATH_PATTERNS = [
    ("capacity", r"(?:selected\s+)?model\b.{0,80}\b(?:is\s+)?at capacity\b"),
    ("network-operation-not-permitted", r"operation not permitted|network is unreachable|network access denied"),
    ("session-limit", r"hit your (?:session|usage) limit|session limit reached"),
    ("usage-limit", r"usage limit reached|weekly limit|rate limit(?:ed)?|\b429\b"),
    ("auth", r"invalid api key|authentication_error|not logged in|please run /login|unauthorized|\b401\b"),
    ("credit", r"credit balance is too low|insufficient (?:credit|quota|funds)"),
]
_RESET_RE = re.compile(
    r"resets?(?:\s+at)?\s+([0-9]{1,2}:[0-9]{2}\s*(?:am|pm)?|[0-9]{1,2}\s*(?:am|pm))",
    re.I,
)


def scan_death(text: str) -> tuple[str, str] | None:
    """Return (reason, reset) if the log text shows a limit/auth death, else None.

    reset is a best-effort human string ('3pm', '15:45', ...) or '' when absent.
    Shared contract with dispatch-liveness.sh LIMIT_RE — keep the pattern lists in sync.
    """
    low = text.lower()
    reason = ""
    for name, pat in DEATH_PATTERNS:
        if re.search(pat, low):
            reason = name
            break
    if not reason:
        return None
    m = _RESET_RE.search(text)
    reset = re.sub(r"\s+", "", m.group(1)) if m else ""
    return reason, reset


def scan_anchored_death(text: str) -> tuple[str, str] | None:
    """Inspect only terse terminal CLI lines, never completion-report prose."""
    for line in [line.strip() for line in text.splitlines() if line.strip()][-3:]:
        if len(line) > 200:
            continue
        death = scan_death(line)
        if death:
            if death[0] == "capacity" and not anchored_capacity_failure(line):
                continue
            return death
    return None


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
        default=os.environ.get("AGENT_DISPATCH_PARENT_SESSION_ID")
        or os.environ.get("CODEX_THREAD_ID")
        or os.environ.get("CLAUDE_CODE_SESSION_ID"),
    )
    p.add_argument(
        "--parent-cwd",
        default=os.environ.get("AGENT_DISPATCH_PARENT_CWD") or None,
    )
    p.add_argument("--worker-role", help="legacy compatibility metadata; not bootstrap identity")
    p.add_argument("--worker-type", choices=("owner", "stage", "review", "support"))
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
    p.add_argument(
        "--owner-harness",
        default=os.environ.get("AGENT_DISPATCH_OWNER_HARNESS")
        or ("codex" if os.environ.get("CODEX_THREAD_ID") else "claude"),
    )
    p.add_argument("--prompt-file")
    p.add_argument("--prompt-text")
    p.add_argument("--jobs")
    p.add_argument("--attempt-id")
    p.add_argument("--broker-request-id")
    p.add_argument("--fallback-ordinal", type=int, default=0)
    p.add_argument("--fallback-hop")
    p.add_argument("--execution-surface", default="registered-headless")
    p.add_argument("--registered-worker", type=int, choices=(0, 1), default=1)
    p.add_argument("--capacity-retry", type=int, choices=(0, 1), default=0)
    p.add_argument("--prior-attempt-id")
    p.add_argument("--cooled-model")
    p.add_argument("--selection-source")
    p.add_argument("--launch-authority", choices=("conductor", "ancestor-broker"), default="conductor")
    p.add_argument(
        "--parent-harness",
        default=(
            os.environ.get("AGENT_DISPATCH_CURRENT_HARNESS")
            or os.environ.get("AGENT_DISPATCH_CALLER_HARNESS")
            or os.environ.get("AGENT_DISPATCH_OWNER_HARNESS")
            or ("codex" if os.environ.get("CODEX_THREAD_ID") else "claude")
        ),
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
    p.add_argument("--profile", help="profiles/<name>.yaml masked config home to attach via CLAUDE_CONFIG_DIR")
    p.add_argument("--model-role", default=os.environ.get("CLAUDE_DISPATCH_MODEL_ROLE"))
    p.add_argument("--model-profile", default=os.environ.get("CLAUDE_DISPATCH_MODEL_PROFILE"))
    p.add_argument("--model", default=os.environ.get("CLAUDE_DISPATCH_MODEL"))
    p.add_argument("--effort", default=os.environ.get("CLAUDE_DISPATCH_EFFORT"))
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
        default=float(os.environ.get("CLAUDE_DISPATCH_FOREGROUND_TIMEOUT", "3600")),
        help="maximum child lifetime for foreground-scoped launch; non-positive clamps to the safe default (never waits indefinitely)",
    )
    add_stage_session_arguments(p)
    return p


def fail(reason: str, code: int, **fields: str) -> int:
    print("check=failed")
    print(f"reason={reason}")
    for key, value in fields.items():
        print(f"{key}={value}")
    return code


class ModelSelectionError(ValueError):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason


def role_map(role: str) -> dict[str, str]:
    result = subprocess.run([str(ROOT / "adapters" / "claude" / "bin" / "model-map.sh"), role], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise ModelSelectionError("invalid-dispatch-model-role", (result.stderr or result.stdout).strip())
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return {"model": fields["exact_model_id"], "effort": fields["reasoning"]}


def _model_policy() -> dict[str, str]:
    path = ROOT / "adapters" / "claude" / "config" / "models.conf"
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ModelSelectionError(
            "dispatch-model-policy-unavailable", str(exc)
        ) from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in {'"', "'"}:
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            value = value.split("#", 1)[0].strip()
        values[key.strip()] = value
    return values


def _main_session_only_model(model: str) -> bool:
    tokens = set(re.split(r"[^a-z0-9]+", model.lower()))
    policy = _model_policy()
    if "CFG_MAIN_SESSION_ONLY_MODELS" not in policy:
        raise ModelSelectionError(
            "dispatch-model-policy-unavailable",
            "CFG_MAIN_SESSION_ONLY_MODELS is not declared",
        )
    restricted = policy["CFG_MAIN_SESSION_ONLY_MODELS"].split()
    return any(alias.lower() in tokens for alias in restricted)


def _require_headless_model(model: str, source: str) -> None:
    if _main_session_only_model(model):
        raise ModelSelectionError(
            "headless-main-session-only-model",
            f"model selected by {source} is interactive dispatch-depth-0 main-session only",
        )


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
    if args.inherit_model_settings:
        if args.model_profile or args.model_role or args.model or args.effort:
            raise ModelSelectionError(
                "invalid-dispatch-model-selection",
                "--inherit-model-settings is mutually exclusive with --model-profile, --model-role, --model, and --effort",
            )
        raise ModelSelectionError(
            "headless-model-inheritance-ineligible",
            "registered headless Claude dispatch cannot prove that inherited main-session settings exclude a main-only model; select --model-role or --model with --effort",
        )
    if args.model_profile:
        if not args.model_role and args.worker_type != "owner":
            raise ModelSelectionError(
                "model-profile-role-required",
                "route-bound --model-profile requires the independently sealed --model-role",
            )
        if bool(args.model) != bool(args.effort):
            raise ModelSelectionError(
                "invalid-dispatch-model-selection",
                "capacity override requires --model and --effort together",
            )
        if (args.model or args.effort) and not args.capacity_retry:
            raise ModelSelectionError(
                "model-profile-override-forbidden",
                "a route-sealed model profile may use a concrete override only on a checked capacity retry",
            )
        try:
            resolved = resolve_profile(
                "claude", ROOT / "adapters" / "claude" / "config" / "models.conf", args.model_profile
            )
        except ModelProfileError as exc:
            raise ModelSelectionError("invalid-dispatch-model-profile", str(exc)) from exc
        model = args.model or resolved["model"]
        _require_headless_model(model, f"profile:{args.model_profile}")
        return {
            "source": "profile+capacity" if args.model else "profile",
            "role": args.model_role or "_kernel/owner",
            "profile": resolved["profile"],
            "tier": resolved["tier"],
            "granularity": resolved["granularity"],
            "model": model,
            "effort": args.effort or resolved["budget"],
        }
    if args.model_role and args.model:
        raise ModelSelectionError(
            "invalid-dispatch-model-selection",
            "--model-role is mutually exclusive with --model (tier-hopping); "
            "situational tuning keeps the role's tier and adjusts --effort only",
        )
    if args.model_role:
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
    if not args.model and not args.effort:
        raise ModelSelectionError(
            "missing-dispatch-model-selection",
            "main dispatch must choose --model-role or --model with --effort",
        )
    if not args.model or not args.effort:
        raise ModelSelectionError(
            "invalid-dispatch-model-selection",
            "--model and --effort must be provided together",
        )
    _require_headless_model(args.model, "explicit")
    return {
        "source": "explicit", "role": "-", "profile": "unsealed",
        "tier": "explicit", "granularity": "legacy", "model": args.model, "effort": args.effort,
    }


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


def resolve_artifact_root(worktree: str) -> str:
    result = subprocess.run(
        [str(ROOT / "utilities" / "artifact-root.sh"), worktree],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value or not Path(value).is_absolute():
        detail = (result.stderr or result.stdout or "invalid artifact root").strip()
        raise ValueError(detail)
    return value


def _is_report_bundle_publish_stage(route_file: str | None, route_node: str | None) -> bool:
    if not route_file or route_node != "publish":
        return False
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected = {
        "id": "publish", "kind": "capability-owner", "unit": "_kernel/owner",
        "completion_gate": "lab-publish", "dispatch_depth": 1,
    }
    return route.get("capability") == "autopilot-lab" and any(
        all(node.get(key) == value for key, value in expected.items())
        for node in route.get("nodes", []) if isinstance(node, dict)
    )


def resolve_report_bundle_root(route_file: str | None, route_node: str | None) -> Path | None:
    if not _is_report_bundle_publish_stage(route_file, route_node):
        return None
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "report-bundle.py"), "root", "--optional"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0:
        raise ValueError((result.stderr or result.stdout or "invalid report bundle root").strip())
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("configured report bundle root is not a safe directory")
    return path


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
        root=ROOT,
    )
    profile_note = (
        f"masked specialization profile={args.profile}; its CLAUDE.md contains only the runtime attach layer and selected specialization"
        if args.profile
        else "profile=-"
    )
    heartbeat = ""
    if args.attempt_id and args.route_id and args.route_node:
        base = (
            f"python3 {shlex.quote(str(ROOT / 'utilities/dispatch-progress.py'))} heartbeat "
            f"--attempt-id {shlex.quote(args.attempt_id)} "
            f"--route-id {shlex.quote(args.route_id)} "
            f"--route-node {shlex.quote(args.route_node)} "
            "--jobs \"$AGENT_DISPATCH_JOBS\""
        )
        heartbeat = (
            "Stage progress contract (SD-58):\n"
            f"- Emit analysis on entry: {base} --phase analysis --kind registry --evidence analysis-entered\n"
            "- After a real tool call, write, test, or artifact update, run the same command with phase tool|file-write|test|artifact and kind tool|file|test|artifact plus a deterministic id/signature.\n"
            "- Repeated prose or an unchanged phase/evidence pair is not progress. Emit terminal only after the assigned artifact is durable.\n\n"
        )
    completion_delivery = getattr(args, "resolved_completion_delivery", "poll-fallback")
    supervised = completion_delivery == "session-resume-supervised"
    owner_standard_plus = (
        args.intensity in _STANDARD_PLUS_INTENSITY and args.worker_type == "owner"
    )
    sync_wait_clause = ""
    if owner_standard_plus and supervised:
        sync_wait_clause = (
            "Runtime-owned completion join (SD-78): register every separable child in the "
            "current batch, then end this turn with exactly `runtime_wait: registered-children`. "
            "Do not call dispatch-wait, Monitor, liveness, or scheduling/wakeup tools. The "
            "session supervisor joins all exact parent_attempt_id children outside the model "
            "and resumes this same Claude session once with a typed bounded receipt. On resume, "
            "harvest only the listed exact attempts; never emit the final handoff with an open "
            "owned child.\n\n"
        )
    elif owner_standard_plus:
        sync_wait_clause = (
            "Checked polling fallback (Claude session-resume bridge unavailable): poll "
            "synchronously with utilities/dispatch-wait.sh in the current turn until terminal, "
            "then harvest. This fallback is not runtime completion parity (OPERATIONS.md §5.10).\n\n"
        )
    # "nothing after it" alone reads as permission to put a summary sentence
    # *before* the block, and that is how two 2026-07-28 pipelines lost their
    # terminal envelope with correct artifacts already on disk. State both ends.
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
        f"- model_role: {getattr(args, 'resolved_model_settings', {}).get('role') or args.model_role or '-'}\n"
        f"- model_profile: {getattr(args, 'resolved_model_settings', {}).get('profile') or getattr(args, 'model_profile', None) or '-'}\n"
        f"- parent: {args.parent_slug or '-'}\n"
        f"- parent_session_id: {args.parent_session_id or '-'}\n"
        f"- owner: {args.capability_owner or '-'}\n"
        f"- owner_harness: {args.owner_harness or '-'}\n"
        f"- worktree: {args.worktree}\n"
        f"- artifact_root: {args.artifact_root}\n"
        f"- route_state: {'consume the immutable record already validated by the wrapper' if args.route_file else 'validated dispatch metadata'}\n"
        f"- {profile_note}\n\n"
        "Claude realization:\n"
        "- The wrapper validates capability mode, worker unit/mode, route/scope, and the masked profile before launch. Re-run route validation only for a safety recheck.\n"
        f"- Read only the exposed {args.assigned_contract} Skill, named artifacts, and selected specialization. General Claude custom subagents may still inherit project CLAUDE.md; do not manually load a full harness bootstrap.\n"
        "- An owner has no worker mode and must not load any unit persona.\n"
        "- Owner workers use the inherited registry, launch checked adapter wrappers directly, consume typed completion receipts, harvest artifacts, and close rows; stage/review/support workers do not dispatch.\n\n"
        f"{heartbeat}"
        f"{stage_session_prompt(args)}"
        "Assignment:\n"
        f"{task.rstrip()}\n\n"
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

    current_thread = os.environ.get("CODEX_THREAD_ID") or os.environ.get(
        "CODEX_SESSION_ID"
    )
    claude_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    caller_harness = (
        os.environ.get("AGENT_DISPATCH_CALLER_HARNESS")
        or ("codex" if current_thread and not claude_session else None)
        or ("claude" if claude_session and not current_thread else None)
    )
    if args.dispatch_depth == 1:
        if current_thread and caller_harness == "codex":
            args.parent_session_id = current_thread
            args.parent_slug = None
            args.parent_harness = "codex"
        elif claude_session and caller_harness == "claude":
            args.parent_session_id = claude_session
            args.parent_slug = None
            args.parent_harness = "claude"


def resolve_parent_completion_delivery(args: argparse.Namespace) -> str:
    """Select wake mechanics from the parent runtime, never the child runtime."""

    args.managed_gateway_binding = None
    current_thread = os.environ.get("CODEX_THREAD_ID") or os.environ.get(
        "CODEX_SESSION_ID"
    )
    direct_registered = (
        getattr(args, "action", "") in {"register", "start"}
        and args.dispatch_depth == 1
        and args.launch_lifecycle == DETACHED
        and args.execution_surface == "registered-headless"
        and bool(args.registered_worker)
        and bool(args.parent_session_id)
        and os.environ.get("AGENT_DISPATCH_CHILD") != "1"
    )
    if (
        direct_registered
        and args.parent_harness == "codex"
        and args.parent_session_id == current_thread
    ):
        try:
            args.managed_gateway_binding = probe_managed_codex_parent(
                parent_harness=args.parent_harness,
                parent_session_id=args.parent_session_id,
            )
        except ManagedDispatchError as exc:
            args.parent_completion_reason = (
                str(exc)
                if os.environ.get("AGENT_CODEX_MANAGED_GATEWAY") == "1"
                else "interactive-auto-wake-unsupported"
            )
            return "poll-fallback"
        args.parent_completion_reason = "managed-single-ingress-live"
        return MANAGED_PARENT_DELIVERY
    if direct_registered and args.parent_harness == "claude":
        args.parent_completion_reason = "claude-async-rewake-resume"
        return "claude-parent-runtime"
    args.parent_completion_reason = "parent-attempt-owned"
    return "parent-runtime-supervised"


def bind_parent_completion_delivery(args: argparse.Namespace) -> None:
    args.parent_completion_delivery = resolve_parent_completion_delivery(args)


def validate_interactive_parent_launch(args: argparse.Namespace) -> None:
    """Never let a Codex caller wait in-model for a cross-harness owner."""

    direct_registered = (
        getattr(args, "action", "") in {"register", "start"}
        and args.dispatch_depth == 1
        and args.launch_lifecycle == DETACHED
        and args.execution_surface == "registered-headless"
        and bool(args.registered_worker)
        and bool(args.parent_session_id)
        and os.environ.get("AGENT_DISPATCH_CHILD") != "1"
    )
    if not (
        direct_registered
        and args.parent_harness == "codex"
        and args.parent_completion_delivery == "poll-fallback"
    ):
        return
    if getattr(args, "allow_unmanaged_parent_poll", False):
        args.parent_completion_reason = "operator-authorized-unmanaged-poll"
        return
    raise DispatchContractError(
        "managed-entry-required",
        "unmanaged interactive Codex parents cannot register or start a detached owner; restart through preflight.sh managed-entry",
    )


def launch_parent_completion_sidecar(
    args: argparse.Namespace,
    jobs: Path,
) -> None:
    args.managed_sidecar_state = "not-selected"
    args.managed_sidecar_reason = "-"
    if args.parent_completion_delivery != MANAGED_PARENT_DELIVERY:
        return
    binding = getattr(args, "managed_gateway_binding", None)
    if binding is None:
        args.managed_sidecar_state = "launch-failed"
        args.managed_sidecar_reason = "managed-binding-missing"
        return
    try:
        sidecar = launch_managed_completion_sidecar(
            binding=binding,
            jobs=jobs,
            parent_session_id=args.parent_session_id or "",
            attempt_ids={args.attempt_id},
        )
    except ManagedDispatchError as exc:
        args.managed_sidecar_state = "launch-failed"
        args.managed_sidecar_reason = str(exc)
        try:
            annotate_attempt_row(
                jobs,
                args.attempt_id,
                {"managed_delivery_state": "sidecar-launch-failed"},
            )
        except DispatchContractError:
            pass
        return
    args.managed_sidecar_state = "running"
    args.managed_sidecar_pid = sidecar.pid
    args.managed_sealed_batch_id = sidecar.sealed_batch_id
    args.managed_sidecar_log = sidecar.log_file
    try:
        recorded = annotate_attempt_row(
            jobs,
            args.attempt_id,
            {
                "managed_delivery_state": "sidecar-running",
                "managed_sealed_batch_id": sidecar.sealed_batch_id,
                "managed_sidecar_pid": str(sidecar.pid),
                "managed_sidecar_log": str(sidecar.log_file),
            },
        )
    except DispatchContractError:
        recorded = False
    if not recorded:
        args.managed_sidecar_state = "running-unrecorded"
        args.managed_sidecar_reason = "sidecar-metadata-unrecorded"


def _async_deny_tools(args: argparse.Namespace) -> tuple[str, ...]:
    """SD-71/78: deny proven-fatal async tools for every Claude print turn."""
    return PROVEN_ASYNC_DENY


def _async_wait_policy(args: argparse.Namespace) -> str:
    if getattr(args, "resolved_completion_delivery", "") == "session-resume-supervised":
        return "runtime-supervised"
    return "deny-proven" if _async_deny_tools(args) else "unsupported"


def _completion_owner(args: argparse.Namespace) -> bool:
    return (
        args.dispatch_depth == 1
        and args.worker_type == "owner"
        and args.intensity in _STANDARD_PLUS_INTENSITY
    )


def claude_session_resume_available() -> bool:
    if shutil.which("claude") is None:
        return False
    try:
        result = subprocess.run(
            ["claude", "--help"],
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "--resume" in result.stdout and "--session-id" in result.stdout


def resolve_completion_delivery(args: argparse.Namespace) -> str:
    requested = args.completion_delivery
    if not _completion_owner(args):
        if requested == "supervised":
            raise DispatchContractError(
                "completion-delivery-ineligible",
                "supervised completion is scoped to registered standard+ dispatch-depth-1 owners",
            )
        return "one-shot"
    if requested == "poll":
        return "poll-fallback"
    if claude_session_resume_available():
        return "session-resume-supervised"
    if requested == "supervised":
        raise DispatchContractError(
            "claude-session-resume-unavailable",
            "claude --help did not expose --session-id and --resume; no owner was launched",
        )
    return "poll-fallback"


def completion_state_path(args: argparse.Namespace) -> Path:
    attempt_id = args.attempt_id or "att-dry-run-placeholder"
    if re.fullmatch(r"att-[A-Za-z0-9._-]{1,240}", attempt_id) is None:
        raise DispatchContractError(
            "completion-state-attempt-invalid",
            "supervised completion requires a path-safe exact attempt id",
        )
    return Path(args.agent_home) / ".dispatch" / "supervisor-state" / f"{attempt_id}.json"


def shell_command(args: argparse.Namespace, prompt_path: Path, log_path: Path) -> str:
    if getattr(args, "resolved_completion_delivery", "one-shot") == "session-resume-supervised":
        command = [
            sys.executable,
            str(ROOT / "utilities" / "claude-session-supervisor.py"),
            "--worktree", args.worktree,
            "--jobs", str(args.jobs_path),
            "--parent-attempt-id", args.attempt_id or "unassigned",
            "--state-file", str(completion_state_path(args)),
            "--add-dir", str(args.artifact_root),
        ]
        if getattr(args, "report_bundle_root", None) is not None:
            command += ["--add-dir", str(args.report_bundle_root)]
        if getattr(args, "owner_route_binding", None):
            command += [
                "--route-file", args.owner_route_binding.route_file,
                "--route-id", args.owner_route_binding.route_id,
                "--route-hash", args.owner_route_binding.route_hash,
            ]
        if getattr(args, "max_continuations", None) is not None:
            command += ["--max-continuations", str(args.max_continuations)]
        if args.resolved_model_settings["source"] != "inherit":
            command += [
                "--model", args.resolved_model_settings["model"],
                "--effort", args.resolved_model_settings["effort"],
            ]
        for tool in _async_deny_tools(args):
            command += ["--disallowed-tool", tool]
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
    if getattr(args, "report_bundle_root", None) is not None:
        cmd += ["--add-dir", str(args.report_bundle_root)]
    if args.resolved_model_settings["source"] != "inherit":
        cmd += [
            "--model",
            args.resolved_model_settings["model"],
            "--effort",
            args.resolved_model_settings["effort"],
        ]
    deny = _async_deny_tools(args)
    if deny:
        cmd += ["--disallowedTools", ",".join(deny)]
    inner = " ".join(shlex.quote(x) for x in cmd)
    return (
        f"cd {shlex.quote(args.worktree)} && "
        f"{inner} < {shlex.quote(str(prompt_path))} >> {shlex.quote(str(log_path))} 2>&1"
    )


@contextmanager
def jobs_lock(jobs: Path):
    jobs.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{jobs}.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield lock_path
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _effective_parent_cwd(args):
    """Where the DISPATCHING session lives — not merely where the wrapper ran.

    Orchestrators routinely `cd` into the task worktree before dispatching, so a raw
    getcwd() records the child's own worktree — a path that can never anchor the
    parent session row in Fleet (observed: Codex dispatch-depth-1 jobs stayed orphan,
    2026-07-16). When the launch cwd sits inside the task worktree and that worktree
    is linked, back-map to the primary checkout instead; explicit --parent-cwd or
    AGENT_DISPATCH_PARENT_CWD still wins via args.parent_cwd.
    """
    cwd = os.path.realpath(args.parent_cwd or os.getcwd())
    try:
        wt = os.path.realpath(args.worktree)
    except (OSError, TypeError):
        return cwd
    if args.parent_cwd is None and (cwd == wt or cwd.startswith(wt + os.sep)):
        try:
            out = subprocess.check_output(
                ["git", "-C", wt, "worktree", "list", "--porcelain"],
                text=True, stderr=subprocess.DEVNULL)
            first = next((ln.split(" ", 1)[1] for ln in out.splitlines()
                          if ln.startswith("worktree ")), None)
            if first and os.path.realpath(first) != wt:
                return os.path.realpath(first)
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    return cwd


def append_job(jobs: Path, args: argparse.Namespace) -> bool:
    repo = subprocess.check_output(["git", "-C", args.worktree, "rev-parse", "--show-toplevel"], text=True).strip()
    pipe = (
        f"capability={args.capability},capability_mode={args.capability_mode},qa={args.qa},"
        f"intensity={args.intensity},attempt_schema_version=2,"
        f"dispatch_depth={args.dispatch_depth},transport=headless,"
        f"execution_surface={args.execution_surface},"
        f"registered_worker={int(bool(args.registered_worker))},"
        f"fallback_hop={args.fallback_hop},harness=claude"
    )
    if args.parent_slug:
        pipe += f",parent={args.parent_slug}"
    if getattr(args, "parent_binding", None) is not None:
        binding = args.parent_binding
        pipe += (
            f",parent_attempt_id={binding.attempt_id}"
            f",parent_pid={binding.pid},parent_pid_start={binding.pid_start}"
            f",parent_pid_scope={binding.pid_scope}"
            f",parent_liveness_source={binding.liveness_source}"
        )
        if binding.pid_host is not None:
            pipe += (
                f",parent_pid_host={binding.pid_host}"
                f",parent_pid_host_start={binding.pid_host_start}"
            )
    if args.parent_session_id:
        pipe += f",parent_sid={args.parent_session_id}"
    if args.parent_slug or args.parent_session_id:
        # OPERATIONS §5.10 pipe contract lists parent_cwd; without it a cross-harness
        # child whose parent_sid is synthetic can never nest in Fleet (2026-07-15).
        pipe += f",parent_cwd={_effective_parent_cwd(args)}"
    if args.worker_role:
        pipe += f",worker_role={args.worker_role}"
    if args.worker_mode:
        pipe += f",worker_mode={args.worker_mode}"
    pipe += f",worker_type={args.worker_type},runtime_sandbox=adapter-default"
    for key, value in sorted(args.launch_lifecycle_resolution.metadata().items()):
        pipe += f",{key}={value}"
    pipe += f",assigned_contract={args.assigned_contract}"
    if args.unit:
        pipe += f",unit={args.unit}"
    if args.capability_owner:
        pipe += f",owner={args.capability_owner}"
    if args.owner_harness:
        pipe += f",owner_harness={args.owner_harness}"
    if args.dispatch_depth >= 2:
        pipe += (
            f",parent_harness={args.parent_harness},parent_transport={args.parent_transport}"
            f",parent_sandbox={args.parent_sandbox},child_harness=claude"
            f",nested_eligibility={args.nested_eligibility},eligibility_source={args.eligibility_source}"
            f",eligibility_failure_class={args.eligibility_failure_class or '-'}"
            f",eligibility_probe={getattr(args, 'eligibility_probe', None) or '-'}"
        )
    for key in ("route_file", "route_id", "route_hash", "route_node", "registry_digest", "write_scope", "completion_gate", "harness_affinity"):
        value = getattr(args, key)
        if value:
            pipe += f",{key}={value}"
    if getattr(args, "owner_route_binding", None):
        pipe += (
            f",owner_route_file={args.owner_route_binding.route_file}"
            f",owner_route_id={args.owner_route_binding.route_id}"
            f",owner_route_hash={args.owner_route_binding.route_hash}"
        )
    settings = args.resolved_model_settings
    pipe += (
        f",model_source={settings['source']},model_role={settings['role']}"
        f",model_profile={settings['profile']},model_tier={settings['tier']}"
        f",profile_granularity={settings['granularity']}"
        f",model={settings['model']},effort={settings['effort']}"
    )
    pipe += f",async_wait_policy={_async_wait_policy(args)}"
    pipe += f",completion_delivery={args.resolved_completion_delivery}"
    pipe += (
        f",parent_completion_delivery={args.parent_completion_delivery}"
        f",parent_completion_reason={args.parent_completion_reason}"
    )
    if args.profile:
        pipe += f",profile={args.profile}"
    pipe += f",artifact_root={args.artifact_root},log_file={args.log_path}"
    pipe += stage_session_metadata(args)
    if args.attempt_id:
        pipe += (
            f",attempt_id={args.attempt_id},launch_authority={args.launch_authority}"
            f",fallback_ordinal={args.fallback_ordinal},launch_fence=registry-v1"
        )
    replica_reservation = getattr(args, "replica_batch_reservation", {})
    if replica_reservation:
        pipe += (
            f",parallel_group={replica_reservation['batch_group']}"
            f",replica_group={replica_reservation['batch_group']}"
        )
        for key in REPLICA_RESERVATION_ROW_KEYS:
            if key in replica_reservation:
                pipe += f",{key}={replica_reservation[key]}"
    if args.capacity_retry:
        pipe += (
            f",capacity_retry=1,prior_attempt_id={args.prior_attempt_id}"
            f",cooled_model={args.cooled_model},selection_source={args.selection_source}"
        )
    if args.broker_request_id:
        pipe += f",broker_request_id={args.broker_request_id}"
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    row = f"{ts}\topen\t{repo}\t{args.worktree}\t{args.slug}\t{pipe}"
    exclusive = ({"route_id": args.route_id, "route_node": args.route_node,
                  "capacity_retry": "1"} if args.capacity_retry else None)
    quick_exclusive = ({"route_id": args.route_id, "route_node": args.route_node}
                       if getattr(args, "quick_attempt", False) else None)
    preclaim = None
    if args.action == "start" and args.route_file:
        preclaim = lambda lines: completion_marker_gate(
            args.route_file,
            args.route_node,
            args.action,
            args.agent_home,
            jobs,
            registry_lines=lines,
            attempt_id=args.attempt_id,
        )
    args.launch_preclaim = preclaim
    return claim_attempt_row(
        jobs, args.attempt_id, row, launch=False,
        exclusive_metadata=exclusive,
        exclusive_live_metadata=quick_exclusive,
        terminal_attempt_limit=getattr(args, "quick_attempt_limit", None),
        replacement_attempt_limit=getattr(args, "replacement_attempt_limit", 0),
        replacement_notes=getattr(args, "replacement_notes", frozenset()),
        preclaim=None,
    )


def close_job_row(jobs: Path, slug: str, worktree: str, reason: str, reset: str, attempt_id: str | None = None) -> bool:
    """SD-15: flip this dispatch's own open row to done with a dead-<reason> note.

    Matches by (slug, worktree, status==open) under the same flock the writer uses,
    so a concurrent conductor appending other rows is serialized. Appends
    note=dead-<reason>[,reset=<reset>] to the pipe column (kv style). Idempotent:
    returns False if no matching open row is found.
    """
    if attempt_id:
        evidence = {"reset": reset} if reset else {}
        if reason == "capacity":
            evidence.update(failure_class="capacity", detected_by="anchored-early-exit")
        return close_attempt_row(jobs, attempt_id, f"dead-{reason}", evidence=evidence)
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
            jobs.write_text("".join(lines), encoding="utf-8")
        return changed


def annotate_job_row(jobs: Path, slug: str, worktree: str, extra_kv: str, attempt_id: str | None = None) -> bool:
    """Append `,<extra_kv>` to the pipe column of this dispatch's open row.

    Same match keys and flock discipline as close_job_row. Used right after
    launch to record the child pid (`pid=<n>`, OPERATIONS §5.10 job registry)
    so dispatch-liveness judges the child by process instead of transcript
    mtime — a conductor sharing the child's worktree keeps the transcript
    directory fresh and masks an exited child behind ALIVE (shared-worktree
    aliasing, observed 2026-07-13).
    """
    if not jobs.is_file():
        return False
    with jobs_lock(jobs):
        lines = jobs.read_text(encoding="utf-8").splitlines(keepends=True)
        for i, line in enumerate(lines):
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
            lines[i] = f"{ts}\t{status}\t{repo}\t{wt}\t{row_slug}\t{pipe},{extra_kv}\n"
            jobs.write_text("".join(lines), encoding="utf-8")
            return True
    return False


def process_start_ticks(pid: int) -> str:
    try:
        return (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").split()[21]
    except (OSError, IndexError):
        return ""


def seed_launch_heartbeat(args: argparse.Namespace, jobs: Path, pid: int, start: str) -> str:
    if not (args.attempt_id and args.route_id and args.route_node):
        return "not-route-bound"
    result = subprocess.run(
        [sys.executable, str(ROOT / "utilities/dispatch-progress.py"), "heartbeat",
         "--attempt-id", args.attempt_id, "--route-id", args.route_id,
         "--route-node", args.route_node, "--jobs", str(jobs),
         "--phase", "launch", "--kind", "registry",
         "--evidence", f"pid={pid};start={start or '-'}"],
        cwd=ROOT, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False,
    )
    return "ok" if result.returncode == 0 else "failed"


def write_reset_cache(agent_home: Path, harness: str, reason: str, reset: str) -> None:
    """SD-15↔SD-16: cache the last known limit reset for usage-check.sh to read.

    File `.dispatch/usage-reset.<harness>` holds one line: `<iso-ts> <reason> <reset>`.
    Best-effort — a cache write failure never blocks dispatch bookkeeping.
    """
    try:
        cache = agent_home / ".dispatch" / f"usage-reset.{harness}"
        cache.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        cache.write_text(f"{ts} {reason} {reset}\n", encoding="utf-8")
    except OSError:
        pass


def watch_early_death(
    proc: subprocess.Popen, log_path: Path, watch_secs: float
) -> tuple[str, str] | None:
    """SD-15: poll a just-launched child for a limit/auth early death.

    Returns (reason, reset) if the child exits within watch_secs and its log tail
    matches a DEATH_PATTERN. SD-59 capacity is the one proactive exception: an
    anchored live capacity line interrupts the exact process group for failover.
    Otherwise returns None. Polls in 0.5s steps.
    """
    if watch_secs <= 0:
        return None
    deadline = time.monotonic() + watch_secs
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            live_tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            live_tail = ""
        live_death = scan_anchored_death(live_tail)
        if live_death and live_death[0] == "capacity":
            try:
                os.killpg(proc.pid, signal.SIGINT)
                proc.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            return live_death
        time.sleep(0.5)
    if proc.poll() is None:
        return None  # still alive past the watch window — not an early death
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        tail = ""
    death = scan_anchored_death(tail)
    if death:
        return death
    if proc.returncode:
        return f"launch-exit-{proc.returncode}", ""
    return None


def resolve_agent_home() -> Path:
    # Mirror utilities/agent-home.sh preference order so the wrapper (writer of
    # jobs.log) and the shell readers (dispatch-liveness / dispatch-wait / the
    # conductor Stop gate) agree on ONE registry root. When AGENT_HOME is unset,
    # falling straight back to ROOT (=worktree) split the registry: the wrapper
    # wrote jobs.log under the worktree while the readers looked under
    # $HOME/hearting/.dispatch — so the liveness/Stop layer never saw the
    # rows the wrapper appended (SD-14b② registry gap).
    def _valid(p):
        return bool(p) and (Path(p) / "core" / "CORE.md").is_file()

    for cand in (
        os.environ.get("AGENT_HOME"),
        os.environ.get("CLAUDE_HOME"),
        str(Path.home() / "hearting"),
        str(Path.home() / "agent_setting"),
        str(Path.home() / ".claude"),
    ):
        if _valid(cand):
            return Path(cand)
    return ROOT


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
    """SD-66 fix-forward: run the nested-eligibility probe in-wrapper when a
    dispatch-depth-2 ``--start`` carries no explicit evidence, instead of failing
    closed on missing flags a caller never had reason to supply by hand.

    Triggers only when both evidence options are still at their parser
    default (``unknown``/empty) and the parent identity needed to run the
    probe is fully known. Explicit supported/unsupported/unknown/partial
    evidence, dispatch-depth-1, and dry-run/register never reach this function's
    trigger path (callers gate on depth/action before calling it). The probe's
    own JSON status is trusted only when every identity field it echoes back
    matches the request; a malformed/mismatched/erroring probe leaves
    ``nested_eligibility`` at its unknown default so `validate_nested_eligibility`
    still fails closed.
    """
    if args.dispatch_depth < 2 or args.action != "start":
        return
    if getattr(args, "nested_eligibility_explicit", False):
        return
    if args.nested_eligibility != "unknown" or args.eligibility_source:
        return
    if not all((args.parent_harness, args.parent_transport, args.parent_sandbox, args.launch_authority)):
        return
    if "unknown" in (args.parent_harness, args.parent_transport, args.parent_sandbox):
        return
    args.eligibility_probe = "internal"
    probe = ROOT / "utilities" / "nested-dispatch-eligibility.py"
    result = subprocess.run(
        [
            sys.executable, str(probe),
            "--parent-harness", args.parent_harness,
            "--parent-transport", args.parent_transport,
            "--parent-sandbox", args.parent_sandbox,
            "--child-harness", "claude",
            "--launch-authority", args.launch_authority,
            "--worktree", args.worktree,
            "--json",
        ],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    try:
        row = json.loads(result.stdout)
    except (ValueError, TypeError):
        return
    if (
        row.get("parent_harness") != args.parent_harness
        or row.get("parent_transport") != args.parent_transport
        or row.get("parent_sandbox") != args.parent_sandbox
        or row.get("child_harness") != "claude"
        or row.get("launch_authority") != args.launch_authority
        or row.get("status") not in ("supported", "unsupported", "unknown")
    ):
        return
    if row["status"] == "supported" and result.returncode != 0:
        # A failed probe process cannot mint launch-eligible evidence, even if
        # its stdout says supported; checked unsupported/unknown results keep
        # their nonzero-rc path and still fail closed downstream.
        return
    args.nested_eligibility = row["status"]
    args.eligibility_source = row.get("probe_source") or ""
    args.eligibility_failure_class = row.get("failure_class") or ""


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
    routed = any((args.route_id, args.route_hash, args.route_node, args.registry_digest))
    if routed and not args.route_file:
        return fail("route-record-required", 65, route_id=args.route_id or "-")
    if not args.route_file:
        return 0
    required = ("route_id", "route_hash", "route_node", "registry_digest", "write_scope")
    missing = [name for name in required if not getattr(args, name)]
    if missing: return fail("route-metadata-missing", 65, fields=",".join(missing))
    try:
        route_record = json.loads(Path(args.route_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        route_record = {}
    if route_record.get("schema_version") != 2 or "broker_contract_version" in route_record:
        return fail("legacy-broker-route-read-only", 65, route_file=args.route_file, child_spawned="0")
    try:
        validate_runtime_requirements(route_record, args.route_node)
    except OwnerRouteBindingError as exc:
        return fail(str(exc), 69, child_spawned="0", fallback="inline-or-main")
    try:
        validate_route_mode_axes(args, route_record)
    except DispatchModeContractError as exc:
        return fail(exc.reason, 65, **exc.fields, child_spawned="0")
    command = [sys.executable, str(ROOT/"utilities"/"worker-route-guard.py"), "validate",
        "--route", args.route_file, "--node", args.route_node, "--cwd", args.worktree,
        "--artifact-root", args.artifact_root, "--capability", args.capability,
        "--intensity", args.intensity, "--write-scope", args.write_scope,
        "--route-id", args.route_id, "--route-hash", args.route_hash,
        "--registry-digest", args.registry_digest,
        "--unit", args.unit,
        "--model-role", args.model_role or "",
        "--model-profile", args.model_profile or ""]
    if args.attempt_id:
        command += ["--current-attempt", args.attempt_id]
    result=subprocess.run(command,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if result.returncode:
        if result.stdout: print(result.stdout,end="")
        if result.stderr: print(result.stderr,end="",file=sys.stderr)
        return fail("worker-route-validation-failed",result.returncode,route_file=args.route_file)
    args.route_validation=result.stdout.strip()
    # Preserve dependency failure ordering, then recheck with the validated
    # global registry immediately before the attempt claim.
    early_jobs = Path(
        args.jobs
        or os.environ.get("AGENT_DISPATCH_JOBS", "")
        or args.agent_home / ".dispatch" / "jobs.log"
    )
    try:
        completion_marker_gate(
            args.route_file, args.route_node, args.action, args.agent_home,
            early_jobs, attempt_id=args.attempt_id,
        )
    except DispatchContractError as e:
        return fail(
            e.reason,
            78 if e.reason in PRELAUNCH_PROCESS_BLOCK_REASONS else 65,
            detail=e.detail,
            child_spawned="0",
        )
    return 0


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv[1:])
    args.launch_lifecycle_resolution = reconcile_launch_lifecycle(
        args.launch_lifecycle, dict(os.environ)
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
    _bind_runtime_parent(args)
    if args.broker_request_id or args.launch_authority == "ancestor-broker":
        return fail("launch-broker-retired", 76, child_spawned="0")
    args.agent_home = resolve_agent_home()
    bind_parent_completion_delivery(args)
    worktree = Path(args.worktree)
    if not worktree.is_dir():
        return fail("worktree-not-found", 66, worktree=args.worktree)
    if subprocess.run(
        ["git", "-C", args.worktree, "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode != 0:
        return fail("not-a-git-worktree", 65, worktree=args.worktree)
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
    rc = validate_dispatch_modes(args)
    if rc != 0:
        return rc
    if args.qa is None:
        args.qa = QA_FROM_INTENSITY.get(args.intensity, "standard")
    if args.qa not in QA_LEVELS:
        return fail(
            "invalid-dispatch-qa",
            64,
            qa=args.qa,
            allowed_qa="quick,light,standard,thorough,adversarial",
        )
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
    args.eligibility_probe = "-"
    bind_internal_eligibility_probe(args)
    try:
        validate_nested_eligibility(
            dispatch_depth=args.dispatch_depth, action=action, parent_harness=args.parent_harness,
            parent_transport=args.parent_transport, parent_sandbox=args.parent_sandbox,
            child_harness="claude", launch_authority=args.launch_authority,
            status=args.nested_eligibility, source=args.eligibility_source,
        )
    except DispatchContractError as e:
        return fail(
            e.reason, 69, detail=e.detail,
            parent_harness=args.parent_harness or "-",
            parent_transport=args.parent_transport or "-",
            parent_sandbox=args.parent_sandbox or "-",
            child_harness="claude",
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
            harness="claude",
        )
        if args.owner_route_binding and (
            args.dispatch_depth != 1 or args.worker_type != "owner" or args.route_file
        ):
            raise OwnerRouteBindingError("owner-route-binding-tuple-invalid")
    except OwnerRouteBindingError as exc:
        return fail(str(exc), 65, child_spawned="0")
    rc=validate_route_record(args)
    if rc != 0: return rc
    args.replica_batch_expectation = None
    if action in {"register", "start"}:
        try:
            args.replica_batch_expectation = replica_batch_expectation(
                args.route_file,
                args.route_node,
                action,
                attempt_id=args.attempt_id or "",
                parent_attempt_id=args.parent_attempt_id or "",
                harness="claude",
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
            intensity=args.intensity, harness="claude",
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
    except ModelSelectionError as e:
        fields = {"detail": str(e), "child_spawned": "0"}
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
    if args.start and shutil.which("claude") is None:
        return fail("claude-command-unavailable", 69, worktree=args.worktree)

    agent_home = args.agent_home
    try:
        registry = resolve_global_registry(agent_home, args.jobs, args.dispatch_depth, action)
        jobs = registry.path
        args.attempt_id = new_attempt_id(args.attempt_id) if action in ("register", "start") else args.attempt_id
        if action in ("register", "start"):
            ensure_global_registry_writable(jobs)
    except DispatchContractError as e:
        return fail(e.reason, 73, detail=e.detail, child_spawned="0")
    try:
        bind_stage_session(args, artifact_root=args.artifact_root, action=action)
    except DispatchContractError as e:
        return fail(e.reason, 65, detail=e.detail, child_spawned="0")
    try:
        completion_marker_gate(
            args.route_file, args.route_node, action, agent_home, jobs,
            attempt_id=args.attempt_id,
        )
    except DispatchContractError as e:
        return fail(e.reason, 78 if e.reason in PRELAUNCH_PROCESS_BLOCK_REASONS else 65,
                    detail=e.detail, child_spawned="0")
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
                worktree=args.worktree,
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
                harness="claude",
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
        args.resolved_completion_delivery = resolve_completion_delivery(args)
        if args.resolved_completion_delivery == "session-resume-supervised":
            completion_state_path(args)
    except DispatchContractError as e:
        return fail(e.reason, 69, detail=e.detail, child_spawned="0")
    log_dir = Path(args.log_dir) if args.log_dir else agent_home / ".dispatch" / "logs"
    home_root = agent_home / ".dispatch" / "homes"
    instance_dir = home_root / f"{args.slug}.{args.profile}" if args.profile else None

    task_input = task_prompt(args)
    prompt_text, prompt_source = dispatch_prompt(args, task_input)
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
                harness="claude",
                fallback_hop=args.fallback_hop,
                fallback_ordinal=args.fallback_ordinal,
                assignment_sha256=assignment_sha256,
            )
        except DispatchContractError as exc:
            return fail(exc.reason, 65, detail=exc.detail, child_spawned="0")
    prompt_name = (
        f"{args.slug}.{args.attempt_id}.claude.prompt.txt"
        if args.attempt_id
        else f"{args.slug}.claude.prompt.txt"
    )
    prompt_path = log_dir / prompt_name
    log_name = (
        f"{args.slug}.{args.attempt_id}.claude.jsonl"
        if args.attempt_id
        else f"{args.slug}.claude.jsonl"
    )
    log_path = log_dir / log_name
    args.log_path = log_path
    command = shell_command(args, prompt_path, log_path)

    if action in ("register", "start"):
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)

    if action == "start" and args.profile:
        # Gate-first, then create -> register -> launch: a --check failure
        # must not leave an instance home behind (no leak on gate failure).
        rc = build_home_gate(agent_home, args.profile, ["--check"], "profile-check-failed")
        if rc != 0:
            return rc
        rc = build_home_gate(
            agent_home,
            args.profile,
            ["--instance", args.slug, "--home-root", str(home_root)],
            "profile-build-failed",
        )
        if rc != 0:
            return rc

    governor = ROOT / "utilities" / "model-worker-governor.py"
    try:
        governor_root = resolve_model_governor_root(args.artifact_root)
    except DispatchContractError as exc:
        return fail(exc.reason, 73, detail=exc.detail, child_spawned="0")
    reservation_token = ""
    args.replica_batch_reservation = {}
    if action in ("register", "start"):
        # Register before launch so harvest can always reclaim the home even
        # if the launch itself never comes up.
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
                return fail(exc.reason, 75, detail=exc.detail, child_spawned="0")
        try:
            args.attempt_claimed = append_job(jobs, args)
            if action == "start" and not args.attempt_claimed:
                args.attempt_claimed = attempt_launch_is_available(
                    jobs, args.attempt_id
                )
            if args.attempt_claimed:
                recorded_delivery = registered_parent_delivery(
                    jobs, args.attempt_id
                )
                if recorded_delivery != args.parent_completion_delivery:
                    raise DispatchContractError(
                        "attempt-parent-delivery-changed",
                        (
                            f"registered={recorded_delivery} "
                            f"current={args.parent_completion_delivery}"
                        ),
                    )
        except DispatchContractError as e:
            cancel_governor_reservation(governor, governor_root, reservation_token)
            return fail(e.reason, 73, detail=e.detail, child_spawned="0")
        except ManagedDispatchError as e:
            cancel_governor_reservation(governor, governor_root, reservation_token)
            return fail(str(e), 73, child_spawned="0")
        if args.attempt_claimed:
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
    else:
        args.attempt_claimed = False

    if action == "start" and not args.attempt_claimed:
        cancel_governor_reservation(governor, governor_root, reservation_token)

    if action == "start" and args.attempt_claimed:
        env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_DISPATCH_BROKER_")}
        env.update({
            "AGENT_SESSION_ROLE": "worker",
            "CLAUDE_CODE_CHILD_SESSION": "1",
            "AGENT_DISPATCH_CHILD": "1",
            "AGENT_DISPATCH_DEPTH": str(args.dispatch_depth),
            "AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION": "2",
            "AGENT_DISPATCH_TRANSPORT": "headless",
            "AGENT_DISPATCH_EXECUTION_SURFACE": args.execution_surface,
            "AGENT_DISPATCH_REGISTERED_WORKER": str(int(bool(args.registered_worker))),
            "AGENT_DISPATCH_FALLBACK_HOP": args.fallback_hop,
            "AGENT_DISPATCH_PARENT_CWD": (_effective_parent_cwd(args) if (args.parent_slug or args.parent_session_id) else ""),
            "AGENT_DISPATCH_INTENSITY": args.intensity,
            "AGENT_DISPATCH_CAPABILITY_MODE": args.capability_mode,
            # This session's own slug — the conductor Stop gate / dispatch-wait
            # identify "open child rows whose parent= equals MY slug" and cannot
            # do so from AGENT_DISPATCH_PARENT_SLUG (which points at the parent).
            "AGENT_DISPATCH_SELF_SLUG": args.slug,
            "AGENT_DISPATCH_PARENT_SLUG": args.parent_slug or "",
            "AGENT_DISPATCH_ATTEMPT_ID": args.attempt_id,
            "AGENT_DISPATCH_PARENT_ATTEMPT_ID": args.parent_attempt_id or "",
            "AGENT_DISPATCH_PARENT_SESSION_ID": args.parent_session_id or "",
            "AGENT_DISPATCH_WORKER_TYPE": args.worker_type,
            "AGENT_DISPATCH_ASSIGNED_CONTRACT": args.assigned_contract,
            "AGENT_DISPATCH_OWNER": args.capability_owner or "",
            "AGENT_DISPATCH_OWNER_HARNESS": args.owner_harness or "",
            "AGENT_ARTIFACT_ROOT": args.artifact_root,
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
            "AGENT_DISPATCH_JOBS": str(jobs),
            "AGENT_DISPATCH_CURRENT_HARNESS": "claude",
            "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
            "AGENT_DISPATCH_CURRENT_SANDBOX": "adapter-default",
            **stage_session_environment(args),
            "AGENT_DISPATCH_COMPLETION_MODE": (
                "supervised"
                if args.resolved_completion_delivery == "session-resume-supervised"
                else "poll"
            ),
        })
        if args.worker_role:
            env["AGENT_DISPATCH_WORKER_ROLE"] = args.worker_role
        else:
            env.pop("AGENT_DISPATCH_WORKER_ROLE", None)
        if args.worker_mode:
            env["AGENT_DISPATCH_WORKER_MODE"] = args.worker_mode
        else:
            env.pop("AGENT_DISPATCH_WORKER_MODE", None)
        if args.unit:
            env["AGENT_DISPATCH_UNIT"] = args.unit
        else:
            env.pop("AGENT_DISPATCH_UNIT", None)
        if args.profile:
            env["CLAUDE_CONFIG_DIR"] = str(instance_dir)
        if args.resolved_completion_delivery == "session-resume-supervised":
            env["AGENT_DISPATCH_COMPLETION_STATE_FILE"] = str(
                completion_state_path(args)
            )
        else:
            env.pop("AGENT_DISPATCH_COMPLETION_STATE_FILE", None)
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
        def spawn_worker(gate_fd: int) -> subprocess.Popen:
            return subprocess.Popen(
                [
                    sys.executable, str(ROOT / "utilities" / "launch-fence.py"),
                    "--parent-pid", str(os.getpid()),
                    "--gate-fd", str(gate_fd),
                    "--jobs", str(jobs), "--attempt-id", args.attempt_id,
                    "--post-release-parent-death-signal",
                    "kill" if args.launch_lifecycle == FOREGROUND_SCOPED else "none",
                    "--",
                    sys.executable, str(governor), "--root", str(governor_root),
                    "run", "--class", "dispatch", "--", "sh", "-c", command,
                ],
                env=env,
                start_new_session=True,
                pass_fds=(gate_fd,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        launch_metadata = args.launch_lifecycle_resolution.metadata()
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
                pre_release=lambda identity: launch_summary_owner(
                    attempt_id=args.attempt_id,
                    harness="claude",
                    transcript=log_path,
                    target_pid=int(identity["pid"]),
                    target_start=identity["pid_start"],
                ),
            )
        except DispatchContractError as exc:
            if exc.reason == "attempt-launch-already-claimed":
                cancel_governor_reservation(
                    governor, governor_root, reservation_token
                )
                print("check=ok")
                print("status=start")
                print(f"attempt_id={args.attempt_id}")
                print("duplicate_attempt=1")
                print("launch_state=existing-active")
                print("registered=0")
                print("started=0")
                print("child_spawned=0")
                print("reason=attempt-launch-already-claimed")
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
            outcome = (
                "reaped-before-publish"
                if exc.reason == "attempt-launch-identity-record-failed"
                else (
                    "launch-cleanup-unverified"
                    if exc.reason == "attempt-launch-cleanup-unverified"
                    else "never-launched"
                )
            )
            annotate_attempt_row(jobs, args.attempt_id, {"launch_outcome": outcome})
            cancel_governor_reservation(governor, governor_root, reservation_token)
            close_job_row(jobs, args.slug, args.worktree, reason, "", args.attempt_id)
            return fail(
                exc.reason, 73, detail=exc.detail,
                attempt_id=args.attempt_id, child_spawned="0",
            )
        except OSError as exc:
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
            )
        except DispatchContractError as exc:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=0.5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=0.5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
            cancel_governor_reservation(governor, governor_root, reservation_token)
            close_job_row(
                jobs, args.slug, args.worktree,
                "governor-reservation-transfer", "", args.attempt_id,
            )
            return fail(
                exc.reason, 75, detail=exc.detail,
                attempt_id=args.attempt_id, child_spawned="1",
            )
        # Shared-worktree aliasing (OPERATIONS §5.10 signal order ①): record
        # the child pid so liveness can use a process signal. Conductor activity
        # in the same worktree can contaminate transcript mtime.
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
        if args.launch_lifecycle == FOREGROUND_SCOPED:
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
            terminal = inspect_terminal_attempt(
                log_path,
                worktree=args.worktree,
                artifact_root_metadata=args.artifact_root,
            )
            args.terminal_inspection = terminal
            args.terminal_verdict = (
                terminal.get("verdict") if terminal.get("state") == "valid" else None
            )
            terminal_note = (
                terminal.get("failure_note", "")
                if terminal.get("state") == "valid"
                else ""
            )
            if outcome.failure and terminal.get("state") == "valid" and not terminal_note:
                terminal_note = "completed-terminal-handoff"
            terminal_closed = False
            if terminal_note:
                terminal_closed = close_attempt_row(
                    jobs,
                    args.attempt_id,
                    terminal_note,
                    evidence={
                        "detected_by": "foreground-terminal-handoff",
                        "failure_class": terminal["failure_class"],
                        "terminal_event": terminal["terminal_event"],
                        "log_file": str(log_path),
                    },
                )
                args.worker_failure = terminal_note
            if outcome.failure and not terminal_closed:
                close_job_row(
                    jobs, args.slug, args.worktree, outcome.failure, "", args.attempt_id
                )
        else:
            # SD-15: detached launches retain the short early-death watch.
            death = watch_early_death(proc, log_path, args.early_exit_watch)
            if death:
                reason, reset = death
                close_job_row(jobs, args.slug, args.worktree, reason, reset, args.attempt_id)
                if reason != "capacity":
                    write_reset_cache(agent_home, "claude", reason, reset)
                args.early_death = (reason, reset)

    print("check=ok")
    print("adapter=claude")
    print("runtime_surface=claude-print-headless")
    print(f"completion_delivery={args.resolved_completion_delivery}")
    print(f"parent_completion_delivery={args.parent_completion_delivery}")
    print(f"parent_completion_reason={args.parent_completion_reason}")
    print(f"managed_sidecar_state={getattr(args, 'managed_sidecar_state', 'not-started')}")
    print(f"managed_sidecar_reason={getattr(args, 'managed_sidecar_reason', '-')}")
    print(f"managed_sidecar_pid={getattr(args, 'managed_sidecar_pid', '-')}")
    print(f"managed_sealed_batch_id={getattr(args, 'managed_sealed_batch_id', '-')}")
    print(f"managed_sidecar_log={getattr(args, 'managed_sidecar_log', '-')}")
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
    print(f"model_role={settings['role']}")
    print(f"model_profile={settings['profile']}")
    print(f"model_tier={settings['tier']}")
    print(f"profile_granularity={settings['granularity']}")
    print(f"model={settings['model']}")
    print(f"effort={settings['effort']}")
    print(f"profile={args.profile or '-'}")
    print(f"instance_home={instance_dir if instance_dir else '-'}")
    print(f"job_registry={jobs}")
    print("broker_lifecycle=retired")
    print(
        "governor_reservation="
        + (str(getattr(args, "governor_reservation", {}).get("state", "-")))
    )
    print(f"registry_authority={registry.source}")
    print(f"attempt_id={args.attempt_id or '-'}")
    print(f"launch_authority={args.launch_authority}")
    print(f"fallback_ordinal={args.fallback_ordinal}")
    print(f"fallback_hop={args.fallback_hop}")
    print(f"execution_surface={args.execution_surface}")
    print(f"registered_worker={int(bool(args.registered_worker))}")
    print(f"registry_lock={jobs}.lock")
    print(f"duplicate_attempt={0 if args.attempt_claimed or action == 'dry-run' else 1}")
    print(
        "launch_state="
        + attempt_launch_state(
            jobs, args.attempt_id, claimed=args.attempt_claimed, action=action
        )
    )
    print(f"registered={1 if args.attempt_claimed else 0}")
    print(f"started={1 if action == 'start' and args.attempt_claimed else 0}")
    print(f"child_pid={getattr(args, 'child_pid', None) or '-'}")
    print(f"child_pid_start={getattr(args, 'child_pid_start', None) or '-'}")
    print(f"launch_heartbeat={getattr(args, 'launch_heartbeat', 'not-started')}")
    print(f"launch_lifecycle={args.launch_lifecycle}")
    print(f"launch_lifecycle_requested={args.launch_lifecycle_requested}")
    print(f"launch_lifecycle_reselection={args.launch_lifecycle_resolution.reselection}")
    print(f"worker_exit={getattr(args, 'worker_exit', '-')}")
    print(f"worker_failure={getattr(args, 'worker_failure', None) or '-'}")
    print(f"terminal_verdict={getattr(args, 'terminal_verdict', None) or '-'}")
    early_death = getattr(args, "early_death", None)
    if early_death:
        reason, reset = early_death
        print(f"early_death={reason}")
        print(f"early_death_reset={reset or '-'}")
        print(f"row_closed=done,note=dead-{reason}")
    else:
        print("early_death=-")
    print(f"prompt_source={prompt_source}")
    print(f"prompt_file={prompt_path}")
    print(f"log_file={log_path}")
    print(f"command={command}")
    return (
        75
        if getattr(args, "managed_sidecar_state", "") == "launch-failed"
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
