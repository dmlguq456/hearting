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

# W7C producer-cycle environment passed from an owner to its stage workers.
ARTIFACT_PRODUCER_CYCLE_ENV = (
    "AGENT_ARTIFACT_CAMPAIGN_ID", "AGENT_ARTIFACT_CYCLE_ID", "AGENT_ARTIFACT_PRODUCER_ID",
    "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_OUTPUT_DIR",
)

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    GROUP_REAP_PROOF,
    GOVERNOR_RESERVATION_ENV,
    REPLICA_RESERVATION_ROW_KEYS,
    STANDARD_PLUS_INTENSITIES,
    SUPERVISOR_LEASE_KIND,
    anchored_capacity_failure,
    annotate_attempt_row,
    attempt_launch_is_available,
    attempt_launch_state,
    cancel_governor_reservation,
    claim_attempt_row,
    close_attempt_row,
    completion_marker_gate,
    PRELAUNCH_PROCESS_BLOCK_REASONS,
    codex_standard_owner_network_enabled,
    dispatch_state_root,
    ensure_global_registry_writable,
    headless_attempt_policy,
    launch_orphan_watch,
    launch_reap_watch,
    new_attempt_id,
    parse_registry_metadata,
    parent_attempt_binding_is_live,
    resolve_global_registry,
    resolve_agent_home as _resolve_agent_home,
    resolve_live_parent_attempt,
    resolve_model_governor_root,
    replica_batch_expectation,
    reserve_governor_token,
    spawn_claimed_attempt,
    supervisor_lease_path,
    validate_nested_eligibility,
    wait_governor_reservation_claim,
)
from dispatch_summary import launch_summary_owner, owner_root  # noqa: E402
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
    validate_capability_mode,
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
    resolve_runtime_profile,
    validate_registered_profile,
)
from codex_dispatch_terminal import inspect_terminal_attempt  # noqa: E402
from dispatch_completion_join import (  # noqa: E402
    JoinContractError,
    close_wrapper_pass,
    exact_attempt_row,
)
from codex_managed_dispatch import (  # noqa: E402
    MANAGED_PARENT_DELIVERY,
    ManagedDispatchError,
    launch_managed_completion_sidecar,
    probe_managed_codex_parent,
    registered_parent_delivery,
)
QA_LEVELS = {"quick", "light", "standard", "thorough", "adversarial"}
# Verification rigor is derived from intensity — CONVENTIONS §1.1 mapping table (SoT).
# `--qa` is no longer a user-facing axis; optional, derived from --intensity when omitted.
# The jobs.log `qa=` field is retained (derived value) for fleet-collector compatibility.
QA_FROM_INTENSITY = {
    "direct": "light",
    "quick": "quick",
    "standard": "standard",
    "strong": "standard",
    "thorough": "thorough",
    "adversarial": "adversarial",
}
INTENSITY_LEVELS = {"direct", "quick", "standard", "strong", "thorough", "adversarial"}
# standard+ per OPERATIONS.md §5.10 — the SD-78 runtime-owned completion clause
# is scoped to this set for owner (conductor) launches only.
_STANDARD_PLUS_INTENSITY = STANDARD_PLUS_INTENSITIES

# SD-15 (OPERATIONS §5.10 ⑨): immediate limit/auth failure patterns — homomorphic port of the Claude
# wrapper's DEATH_PATTERNS. codex exec surfaces provider limit/auth failures as JSON
# events (`--json`), but a raw tail substring scan still matches the text inside those
# events, so no JSON parsing is needed (same as the Claude tail scan). Runtime-currentness
# (2026-07, openai/codex#9148·#12677·#11434·#4840): codex prints "exceeded retry limit,
# last status: 429 Too Many Requests" / "usage_limit_reached" and generally exits non-zero
# on retry exhaustion, so the launch early-exit watch is realizable (best-effort). The
# shell/other-adapter counterparts (dispatch-liveness.py LIMIT_RE) keep the same list —
# intentional cross-runtime duplication, keep in sync.
DEATH_PATTERNS = [
    ("capacity", r"(?:selected\s+)?model\b.{0,80}\b(?:is\s+)?at capacity\b"),
    ("network-operation-not-permitted", r"operation not permitted|network is unreachable|network access denied"),
    ("session-limit", r"hit your (?:session|usage) limit|session limit reached"),
    ("usage-limit", r"usage[_ ]limit[_ ]reached|usage limit reached|weekly limit|"
     r"rate limit(?:ed)?|provider rate limit|exceeded retry limit|\b429\b"),
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
    Homomorphic with the Claude wrapper's scan_death and dispatch-liveness.py LIMIT_RE.
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
        default=os.environ.get("AGENT_DISPATCH_OWNER_HARNESS") or "codex",
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
            or (
                "claude"
                if os.environ.get("CLAUDE_CODE_SESSION_ID")
                and not os.environ.get("CODEX_THREAD_ID")
                else "codex"
            )
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
        help="do not override model/reasoning; inherit the active Codex config for this dispatch",
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
    force_current = os.environ.get("CODEX_DISPATCH_PARENT_CURRENT_FORCE") == "1"
    current_thread = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    claude_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    caller_harness = (
        os.environ.get("AGENT_DISPATCH_CALLER_HARNESS")
        or ("codex" if current_thread and not claude_session else None)
        or ("claude" if claude_session and not current_thread else None)
    )
    if args.dispatch_depth == 1:
        if current_thread and caller_harness == "codex":
            args.parent_session_id = current_thread
            args.parent_harness = "codex"
            args.parent_slug = None
        elif claude_session and caller_harness == "claude":
            args.parent_session_id = claude_session
            args.parent_harness = "claude"
            args.parent_slug = None
        elif force_current:
            args.parent_slug = None
    elif force_current and current_thread:
        args.parent_session_id = current_thread


def resolve_parent_completion_delivery(args: argparse.Namespace) -> str:
    """Select the checked parent-runtime adapter for a direct Codex child."""
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
        and bool(current_thread)
        and args.parent_session_id == current_thread
    ):
        try:
            args.managed_gateway_binding = probe_managed_codex_parent(
                parent_harness=args.parent_harness,
                parent_session_id=args.parent_session_id,
            )
        except ManagedDispatchError as exc:
            if os.environ.get("AGENT_CODEX_MANAGED_GATEWAY") == "1":
                args.parent_completion_reason = str(exc)
            else:
                args.parent_completion_reason = (
                    "interactive-auto-wake-unsupported"
                )
            return "poll-fallback"
        if args.managed_gateway_binding.thread_advanced:
            args.parent_session_id = args.managed_gateway_binding.thread_id
            args.parent_completion_reason = "managed-thread-advanced"
        else:
            args.parent_completion_reason = "managed-single-ingress-live"
        return MANAGED_PARENT_DELIVERY
    if direct_registered and args.parent_harness == "claude":
        args.parent_completion_reason = "claude-async-rewake-resume"
        return "claude-parent-runtime"
    if direct_registered:
        args.parent_completion_reason = "parent-identity-unmatched"
        return "poll-fallback"
    args.parent_completion_reason = "parent-attempt-owned"
    return "parent-runtime-supervised"


def bind_parent_completion_delivery(args: argparse.Namespace) -> None:
    args.parent_completion_delivery = resolve_parent_completion_delivery(args)


def validate_interactive_parent_launch(args: argparse.Namespace) -> None:
    """Never let an ordinary Codex parent enter a model-owned wait loop."""

    direct_registered = (
        getattr(args, "action", "") in {"register", "start"}
        and args.dispatch_depth == 1
        and args.launch_lifecycle == DETACHED
        and args.execution_surface == "registered-headless"
        and bool(args.registered_worker)
        and bool(args.parent_session_id)
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
    """Prelaunch one exact joiner before the managed direct child spawn claim."""

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
                {
                    "managed_delivery_state": "sidecar-launch-failed",
                },
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
        # The immutable delivery stamp still lets this exact sidecar join. Keep
        # the launch successful while making the observability loss explicit.
        args.managed_sidecar_state = "running-unrecorded"
        args.managed_sidecar_reason = "sidecar-metadata-unrecorded"


def fail(reason: str, code: int, **fields: str) -> int:
    print("check=failed")
    print(f"reason={reason}")
    for key, value in fields.items():
        print(f"{key}={value}")
    return code


def read_launch_fence_failure(fd: int) -> dict[str, object] | None:
    """Read and close the fence's private, close-on-exec failure channel."""
    try:
        os.set_blocking(fd, False)
        try:
            raw = os.read(fd, 16384)
        except BlockingIOError:
            return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if not raw:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != 1
        or not isinstance(record.get("reason"), str)
        or not isinstance(record.get("detail"), str)
    ):
        return None
    return record


def terminal_receipt_fields(terminal: dict | None) -> dict[str, str]:
    """Return only bounded typed terminal metadata for the launch receipt."""
    value = terminal or {
        "state": "absent",
        "source": "none",
        "verdict": "-",
        "artifact_state": "unchecked",
        "blocker_reason": "-",
    }
    artifact_state = str(value["artifact_state"])
    return {
        "handoff_state": str(value["state"]),
        "handoff_source": str(value["source"]),
        "handoff_verdict": str(value["verdict"]),
        "artifact_state": artifact_state,
        "artifact_readable": "1" if artifact_state == "readable" else "0",
        "artifact_path_b64": str(value.get("artifact_path_b64", "-")),
        "blocker_reason": str(value["blocker_reason"]),
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
        f"intensity={args.intensity}\ndispatch_depth={args.dispatch_depth}\nparent={args.parent_slug or '-'}\n"
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
        if args.model_profile or args.model_role or args.model or args.reasoning:
            raise ModelSelectionError(
                "invalid-dispatch-model-selection",
                "--inherit-model-settings is mutually exclusive with --model-profile, --model-role, --model, and --reasoning",
            )
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
                "a route-sealed model profile may use a concrete override only on a checked capacity retry",
            )
        try:
            resolved, _receipt = resolve_runtime_profile(
                "codex", args.model_profile, source_root=ROOT
            )
        except ModelProfileError as exc:
            raise ModelSelectionError("invalid-dispatch-model-profile", str(exc)) from exc
        return {
            "source": "profile+capacity" if args.model else "profile",
            "role": args.model_role or "_kernel/owner",
            "profile": resolved["profile"],
            "tier": resolved["tier"],
            "granularity": resolved["granularity"],
            "model": args.model or resolved["model"],
            "reasoning": args.reasoning or resolved["budget"],
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
            "main dispatch must choose --model-role, --model with --reasoning, or --inherit-model-settings",
        )
    if not args.model or not args.reasoning:
        raise ModelSelectionError(
            "invalid-dispatch-model-selection",
            "--model and --reasoning must be provided together",
        )
    return {
        "source": "explicit", "role": "-", "profile": "unsealed",
        "tier": "explicit", "granularity": "legacy", "model": args.model, "reasoning": args.reasoning,
    }


def _worktree_mutating_write_scope(write_scope: str | None) -> bool:
    if not write_scope:
        return False
    return any(
        part.strip() in ("source/**", "source") or part.strip().startswith("source/")
        for part in write_scope.split(";")
    )


def _worktree_git_dirs(worktree) -> tuple[Path, Path] | None:
    """Resolve (git-dir, git-common-dir) for a worktree, or None if unprovable."""
    try:
        root = Path(worktree).resolve()
        values = []
        for flag in ("--git-dir", "--git-common-dir"):
            result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", flag],
                text=True, capture_output=True, check=True,
            )
            value = Path(result.stdout.strip())
            values.append(value.resolve() if value.is_absolute() else (root / value).resolve())
        return values[0], values[1]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _is_linked_worktree(worktree, agent_home) -> bool:
    """Identify a real linked worktree from Git metadata, not path inequality."""
    dirs = _worktree_git_dirs(worktree)
    if dirs is None:
        # A mutating stage whose Git topology cannot be proved is treated as
        # linked/protected, preserving the no-commit safety boundary.
        return True
    git_dir, common_dir = dirs
    return git_dir != common_dir


def is_no_commit_stage(args: argparse.Namespace) -> bool:
    """SD-69: a linked-worktree Codex depth-2 mutation stage never commits.

    The boundary is contractual, not a sandbox impossibility: parallel stages
    must not race HEAD, so the owner integrates and commits once after the
    stage's own PASS gate (core/OPERATIONS.md SD-69). A dispatch-depth-1 owner
    is commit-expected in its linked worktree and gets the exact primary Git
    metadata directories via `linked_worktree_git_writable_dirs` instead.
    """
    return (
        getattr(args, "worker_type", None) == "stage"
        and _worktree_mutating_write_scope(getattr(args, "write_scope", None))
        and _is_linked_worktree(args.worktree, args.agent_home)
    )


def linked_worktree_git_writable_dirs(args: argparse.Namespace) -> tuple[Path, ...]:
    """Primary Git metadata dirs a commit-expected linked-worktree run needs.

    Codex resolves a linked worktree's real git dir and allows it on its own
    only under the default ``~/.codex`` home; with any custom ``CODEX_HOME``
    (the masked dispatch home) that built-in allowance is inactive and
    ``git commit`` fails on ``index.lock`` with EROFS (verified against
    codex-cli 0.148.0, 2026-08-21). The wrapper therefore grants the exact
    directories a commit touches: the per-worktree git dir plus the common
    dir's ``objects``/``refs``/``logs``. The common-dir root itself stays
    ungranted so ``hooks/`` and ``config`` remain read-only — a worker must
    not be able to plant code a later unsandboxed session would execute.
    Only the dispatch-depth-1 owner is commit-expected; every other worker
    type (including non-mutating depth-2 stages) gets no Git metadata grant.
    """
    if getattr(args, "worker_type", None) != "owner":
        return ()
    dirs = _worktree_git_dirs(getattr(args, "worktree", ""))
    if dirs is None:
        return ()
    git_dir, common_dir = dirs
    if git_dir == common_dir:
        return ()
    return (git_dir, common_dir / "objects", common_dir / "refs", common_dir / "logs")


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
    route_state = (
        "consume the assigned route only (wrapper-validated immutable record)"
        if args.route_file
        else "validated dispatch metadata"
    )
    heartbeat = ""
    if args.attempt_id and args.route_id and args.route_node:
        base = (
            f"{shlex.quote(str(ROOT / 'adapters/codex/bin/preflight.sh'))} stage-heartbeat "
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
    no_commit_clause = (
        "No-commit worker (SD-69):\n"
        "- You are a no-commit worker: produce source diff, tests, and evidence; do NOT `git commit`.\n"
        "- A trusted dispatch-depth-0/Claude boundary commits after this stage's own PASS gate and confirms diff attribution.\n\n"
        if is_no_commit_stage(args) else ""
    )
    completion_delivery = getattr(args, "resolved_completion_delivery", "poll-fallback")
    supervised = completion_delivery == "app-server-supervised"
    owner_standard_plus = (
        args.intensity in _STANDARD_PLUS_INTENSITY and args.worker_type == "owner"
    )
    sync_wait_clause = ""
    if owner_standard_plus and supervised:
        sync_wait_clause = (
            "Runtime-owned completion join (SD-78): register every separable child in the "
            "current batch with --start. Confirm that the start receipt itself says "
            "registered=1, started=1, and child_spawned=1; check=ok, a dry-run attempt id, "
            "or a register-only receipt is not launch evidence. Only then end this turn with "
            "exactly `runtime_wait: registered-children`. "
            "Do not call dispatch-wait, liveness, Monitor, or any scheduling/wakeup tool. The "
            "App Server supervisor joins all exact parent_attempt_id children outside the model "
            "and resumes this same thread once with a typed bounded receipt. On resume, harvest "
            "only the listed exact attempts. Do not emit the final three-line handoff while an "
            "owned child remains open.\n\n"
        )
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
        f"- model_role: {getattr(args, 'resolved_model_settings', {}).get('role') or args.model_role or '-'}\n"
        f"- model_profile: {getattr(args, 'resolved_model_settings', {}).get('profile') or getattr(args, 'model_profile', None) or '-'}\n"
        f"- parent: {args.parent_slug or '-'}\n"
        f"- parent_session_id: {args.parent_session_id or '-'}\n"
        f"- owner: {args.capability_owner or '-'}\n"
        f"- owner_harness: {args.owner_harness or '-'}\n"
        f"- worktree: {args.worktree}\n"
        f"- artifact_root: {args.artifact_root}\n"
        f"- route_state: {route_state}\n\n"
        "Codex realization:\n"
        f"- Read only $AGENT_HOME/adapters/codex/skills/{args.assigned_contract}/SKILL.md; the typed bootstrap above already contains the exact portable unit persona.\n"
        "- An owner has no worker mode and must not load any unit persona path.\n"
        f"- Run $AGENT_HOME/adapters/codex/bin/preflight.sh qa-policy {args.qa} {qa_track(args.capability)} and keep its required assurance in the artifact.\n"
        "- The wrapper already validated capability mode, worker unit/mode, QA, artifact root, and any route record. Re-run worker-route only for a safety recheck.\n"
        "- Before each edit run $AGENT_HOME/adapters/codex/bin/preflight.sh write <file>; preserve required test and tool-contract checks in the artifact.\n"
        "- Codex may still auto-discover project AGENTS.md; do not explicitly load the full harness adapter bootstrap or another runtime's adapter.\n\n"
        f"{heartbeat}"
        f"{no_commit_clause}"
        f"{stage_session_prompt(args)}"
        "Assignment:\n"
        f"{task.rstrip()}\n\n"
        f"{ending}",
        source,
    )


def effective_runtime_sandbox(args: argparse.Namespace) -> str:
    """Avoid nesting Codex's mount sandbox inside an already checked Codex sandbox."""

    if (
        getattr(args, "launch_lifecycle", DETACHED) == FOREGROUND_SCOPED
        and os.environ.get("AGENT_DISPATCH_CHILD") == "1"
        and getattr(args, "dispatch_depth", 1) >= 2
        and getattr(args, "parent_harness", None) == "codex"
        and getattr(args, "parent_transport", None) == "headless"
        and getattr(args, "parent_sandbox", None) == "workspace-write"
    ):
        return "danger-full-access"
    return args.sandbox


def invalid_codex_mount_target(args: argparse.Namespace, worktree: Path) -> Path | None:
    """Return an invalid `.codex` destination when Codex will mount a sandbox."""

    target = worktree / ".codex"
    if effective_runtime_sandbox(args) == "danger-full-access":
        return None
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        return target
    return None


def _spec_grounding_dir(args: argparse.Namespace) -> Path:
    return Path(args.agent_home) / ".spec-grounding"


def _core_grounding_dir(args: argparse.Namespace) -> Path:
    return Path(args.agent_home) / ".core-grounding"


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


def route_bound_worker_writable_dirs(args: argparse.Namespace) -> tuple[Path, ...]:
    """Grant set for the portable read-guard state directories.

    Two launch shapes need them (review F-3): an ordinary registered
    `dispatch_depth==2` Codex worker (plan-check round-1 Finding 1), and a
    standard+ `nested_headless_network` owner launched without a `route_id` --
    SD-72 grants the owner `.spec-grounding`/`.core-grounding` unconditionally,
    so gating on `route_id` alone reopened the EROFS this cycle closes. A pure
    query -- see `ensure_owner_writable_dirs`."""

    if not (
        getattr(args, "route_id", None)
        or getattr(args, "nested_headless_network", False)
    ):
        return ()
    return tuple(
        path.resolve() for path in (_core_grounding_dir(args),) if path.is_dir()
    )


def ensure_owner_writable_dirs(args: argparse.Namespace) -> None:
    """Create every directory this launch will grant sandbox write access to,
    once, before the child command is built. A query function (above) never
    has this side effect -- a create failure here is a typed launch failure,
    not a silently narrowed grant list."""

    to_create = []
    if getattr(args, "nested_headless_network", False):
        to_create.append(owner_root())
    if getattr(args, "route_id", None) or getattr(
        args, "nested_headless_network", False
    ):
        to_create.append(_spec_grounding_dir(args))
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
    return (
        args.dispatch_depth == 1
        and args.worker_type == "owner"
        and args.intensity in _STANDARD_PLUS_INTENSITY
    )


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
    if codex_app_server_available():
        return "app-server-supervised"
    if requested == "supervised":
        raise DispatchContractError(
            "codex-app-server-unavailable",
            "codex app-server --help did not pass; no owner attempt was launched",
        )
    return "poll-fallback"


def completion_state_path(args: argparse.Namespace) -> Path:
    state_root = dispatch_state_root(args.jobs_path)
    if not args.attempt_id:
        return state_root / "supervisor-state" / "preview-only.json"
    attempt_id = args.attempt_id
    if re.fullmatch(r"att-[A-Za-z0-9._-]{1,240}", attempt_id) is None:
        raise DispatchContractError(
            "completion-state-attempt-invalid",
            "supervised completion requires a path-safe exact attempt id",
        )
    return state_root / "supervisor-state" / f"{attempt_id}.json"


def completion_lease_path(args: argparse.Namespace) -> Path:
    if not args.attempt_id:
        return Path(args.jobs_path).resolve().parent / "supervisor-state" / "preview-only.lease"
    attempt_id = args.attempt_id
    return supervisor_lease_path(args.jobs_path, attempt_id)


def shell_command(args: argparse.Namespace, prompt_path: Path, log_path: Path) -> str:
    if getattr(args, "resolved_completion_delivery", "one-shot") == "app-server-supervised":
        command = [
            sys.executable,
            str(ROOT / "utilities" / "codex-app-server-supervisor.py"),
            "--worktree", args.worktree,
            "--jobs", str(args.jobs_path),
            "--parent-attempt-id", args.attempt_id or "unassigned",
            "--state-file", str(completion_state_path(args)),
            "--lease-file", str(completion_lease_path(args)),
            "--sandbox", effective_runtime_sandbox(args),
            "--approval", args.approval,
            "--writable-root", str(args.artifact_root),
        ]
        if getattr(args, "report_bundle_root", None) is not None:
            command += ["--writable-root", str(args.report_bundle_root)]
        if getattr(args, "owner_route_binding", None):
            command += [
                "--route-file", args.owner_route_binding.route_file,
                "--route-id", args.owner_route_binding.route_id,
                "--route-hash", args.owner_route_binding.route_hash,
            ]
        if getattr(args, "max_continuations", None) is not None:
            command += ["--max-continuations", str(args.max_continuations)]
        if args.nested_headless_network or (
            args.dispatch_depth == 2 and args.route_id and args.attempt_id
        ):
            command += [
                "--writable-root",
                str(dispatch_state_root(args.jobs_path)),
            ]
        for writable_dir in nested_owner_writable_dirs(args):
            command += ["--writable-root", str(writable_dir)]
        if args.route_id or args.nested_headless_network:
            command += ["--writable-root", str(_spec_grounding_dir(args))]
        for writable_dir in route_bound_worker_writable_dirs(args):
            command += ["--writable-root", str(writable_dir)]
        for writable_dir in linked_worktree_git_writable_dirs(args):
            # SD-69: a commit-expected linked-worktree run gets the exact
            # primary Git metadata dirs; no-commit stages get none of these.
            command += ["--writable-root", str(writable_dir)]
        if args.nested_headless_network:
            command += ["--network-access"]
        if args.resolved_model_settings["source"] != "inherit":
            command += [
                "--model", args.resolved_model_settings["model"],
                "--reasoning", args.resolved_model_settings["reasoning"],
            ]
        return (
            " ".join(shlex.quote(x) for x in command)
            + f" < {shlex.quote(str(prompt_path))} >> {shlex.quote(str(log_path))} 2>&1"
        )
    cmd = [
        "codex",
        "exec",
        "--cd",
        args.worktree,
        "--add-dir",
        args.artifact_root,
    ]
    if getattr(args, "report_bundle_root", None) is not None:
        cmd += ["--add-dir", str(args.report_bundle_root)]
    if args.nested_headless_network or (args.dispatch_depth == 2 and args.route_id and args.attempt_id):
        # A dispatch-depth-1 conductor must update the canonical attempt registry and
        # materialize child prompt/transcript files under the canonical dispatch
        # state root. A route-bound dispatch-depth-2 stage needs the same narrow
        # writable root for its own SD-58 heartbeat. Network remains owner-only below.
        cmd += [
            "--add-dir",
            str(dispatch_state_root(args.jobs_path)),
        ]
    for writable_dir in nested_owner_writable_dirs(args):
        # Core read markers and Claude's Bash pre-exec snapshot are the only
        # home-scoped writes needed by a recursive standard+ Codex owner.
        cmd += ["--add-dir", str(writable_dir)]
    if args.route_id or args.nested_headless_network:
        # SD-69: a route-bound worker needs the exact primary spec-grounding
        # marker directory writable (and SD-72 grants a standard+ owner the
        # same directory unconditionally, route or not — review F-3).
        # Intentionally narrow — never all of agent home.
        cmd += ["--add-dir", str(_spec_grounding_dir(args))]
    for writable_dir in route_bound_worker_writable_dirs(args):
        # SD-72: an ordinary route-bound depth-2 worker also runs the portable
        # core-read guard hook and must record its own .core-grounding marker,
        # independent of the owner-only nested_headless_network network grant.
        cmd += ["--add-dir", str(writable_dir)]
    for writable_dir in linked_worktree_git_writable_dirs(args):
        # SD-69: only a commit-expected linked-worktree owner gets the exact
        # primary Git metadata dirs a commit touches; the common-dir root
        # itself, hooks/, and config stay ungranted, and every other worker
        # type (no-commit mutation stages included) gets none of these.
        cmd += ["--add-dir", str(writable_dir)]
    cmd += ["--sandbox", effective_runtime_sandbox(args)]
    if args.nested_headless_network:
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
    return " ".join(shlex.quote(x) for x in cmd) + f" < {shlex.quote(str(prompt_path))} >> {shlex.quote(str(log_path))} 2>&1"


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


_CODEX_THREAD_ID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_ROLLOUT_META_SCAN_LINES = 8


def _codex_session_store_roots() -> list[Path]:
    """Candidate rollout stores for the CALLING session, most specific first."""
    roots: list[Path] = []
    for raw in (os.environ.get("CODEX_SQLITE_HOME"), os.environ.get("CODEX_HOME"), "~/.codex"):
        if not raw:
            continue
        try:
            root = Path(raw).expanduser() / "sessions"
        except (OSError, RuntimeError, ValueError):
            continue
        if root not in roots:
            roots.append(root)
    return roots


def _codex_thread_cwd(session_id):
    """Read-only: the cwd the parent Codex thread itself was started in.

    Resolved from that thread's rollout ``session_meta.cwd``. Every miss — bad id,
    no store, missing or ambiguous rollout, unreadable file, absent meta, vanished
    path — returns None so the caller falls through to the launch-cwd tier. Never
    guesses.
    """
    if not session_id or not _CODEX_THREAD_ID_RE.fullmatch(session_id):
        return None
    suffix = "-" + session_id + ".jsonl"
    for root in _codex_session_store_roots():
        try:
            candidates = [p for p in root.rglob("rollout-*.jsonl") if p.name.endswith(suffix)]
        except (OSError, ValueError):
            continue
        if len(candidates) != 1:
            continue
        try:
            with candidates[0].open("r", encoding="utf-8", errors="replace") as fh:
                for _ in range(_ROLLOUT_META_SCAN_LINES):
                    line = fh.readline()
                    if not line:
                        break
                    try:
                        record = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(record, dict) or record.get("type") != "session_meta":
                        continue
                    payload = record.get("payload")
                    cwd = payload.get("cwd") if isinstance(payload, dict) else None
                    if isinstance(cwd, str) and cwd and os.path.isdir(cwd):
                        return os.path.realpath(cwd)
                    return None
        except OSError:
            continue
    return None


def _effective_parent_cwd(args):
    """Where the DISPATCHING session lives — not merely where the wrapper ran.

    Evidence tiers, strongest first:

    1. Explicit --parent-cwd / AGENT_DISPATCH_PARENT_CWD via args.parent_cwd.
    2. The parent Codex thread's own rollout ``session_meta.cwd``. Codex sessions
       routinely run harness utilities as `cd $AGENT_HOME && …`, so the wrapper's
       getcwd() records AGENT_HOME rather than where the session lives (observed:
       a managed-Codex thread living in a sibling app repo recorded
       parent_cwd=<agent-home> and stayed orphan in Fleet whenever
       the exact parent_sid match was unavailable, 2026-07-30). A non-Codex parent
       has no thread rollout, so this tier is a silent no-op for it.
    3. Launch getcwd(), back-mapped to the primary checkout when an orchestrator
       `cd`-ed into the linked task worktree before dispatching (2026-07-16).
    """
    if not args.parent_cwd:
        derived = _codex_thread_cwd(getattr(args, "parent_session_id", None))
        if derived:
            return derived
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


def _route_node_leg_fields(args):
    """Read the sealed leg_class/auxiliary_check off this wrapper's route node.

    W1c projection source: the fields are stamped by the compiler during
    parallel-group expansion, so the wrapper reads its own sealed node instead
    of trusting a second, independently-produced value. Missing node/fields
    project the explicit absence marker `-`.
    """
    route_file = getattr(args, "route_file", None)
    route_node = getattr(args, "route_node", None)
    if not route_file or not route_node:
        return "-", "-"
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "-", "-"
    for node in route.get("nodes", []):
        if isinstance(node, dict) and node.get("id") == route_node:
            return (
                str(node.get("leg_class") or "-"),
                str(node.get("auxiliary_check") or "-"),
            )
    return "-", "-"


def append_job(jobs: Path, args: argparse.Namespace) -> bool:
    repo = subprocess.check_output(["git", "-C", args.worktree, "rev-parse", "--show-toplevel"], text=True).strip()
    pipe = (
        f"capability={args.capability},capability_mode={args.capability_mode},qa={args.qa},"
        f"intensity={args.intensity},attempt_schema_version=2,"
        f"dispatch_depth={args.dispatch_depth},transport=headless,"
        f"execution_surface={args.execution_surface},"
        f"registered_worker={int(bool(args.registered_worker))},"
        f"fallback_hop={args.fallback_hop},harness=codex,"
        f"completion_delivery={args.resolved_completion_delivery},"
        f"parent_completion_delivery={args.parent_completion_delivery},"
        f"parent_completion_reason={getattr(args, 'parent_completion_reason', 'unspecified')}"
    )
    if args.resolved_completion_delivery == "app-server-supervised":
        pipe += (
            f",supervisor_lease={SUPERVISOR_LEASE_KIND}"
            f",supervisor_lease_file={completion_lease_path(args)}"
            f",supervisor_lease_nonce={secrets.token_hex(32)}"
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
    pipe += f",worker_type={args.worker_type}"
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
            f",parent_sandbox={args.parent_sandbox},child_harness=codex"
            f",nested_eligibility={args.nested_eligibility},eligibility_source={args.eligibility_source}"
            f",eligibility_failure_class={args.eligibility_failure_class or '-'}"
            f",eligibility_probe={getattr(args, 'eligibility_probe', None) or '-'}"
        )
    pipe += f",runtime_sandbox={effective_runtime_sandbox(args)}"
    for key, value in sorted(args.launch_lifecycle_resolution.metadata().items()):
        pipe += f",{key}={value}"
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
        f",model={settings['model']},reasoning={settings['reasoning']}"
    )
    if args.approval != "inherit":
        pipe += f",approval={args.approval}"
    if args.profile:
        pipe += f",profile={args.profile}"
    # launch_home seals the resolved AGENT_HOME this wrapper launched under, so a
    # reader (fleet) can locate the default log dir without guessing the install
    # layout — the registry row may live in a different runtime home than the logs.
    pipe += (
        f",artifact_root={args.artifact_root},log_file={args.log_path}"
        f",launch_home={args.agent_home}"
    )
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
    leg_class, auxiliary_check = _route_node_leg_fields(args)
    pipe += f",leg_class={leg_class},auxiliary_check={auxiliary_check}"
    if args.capacity_retry:
        pipe += (
            f",capacity_retry=1,prior_attempt_id={args.prior_attempt_id}"
            f",cooled_model={args.cooled_model},selection_source={args.selection_source}"
        )
    if args.broker_request_id:
        pipe += f",broker_request_id={args.broker_request_id}"
    if is_no_commit_stage(args):
        pipe += ",no_commit=1"
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


def nested_headless_network_enabled(args: argparse.Namespace) -> bool:
    """Grant network only to a standard+ dispatch-depth-1 Codex capability owner."""

    return codex_standard_owner_network_enabled(
        dispatch_depth=args.dispatch_depth,
        worker_type=args.worker_type,
        intensity=args.intensity,
        sandbox=args.sandbox,
    )


def prepare_nested_codex_home(worktree: Path, source_home: Path | None = None) -> Path:
    """Create a writable Codex home inside the owner's sandbox.

    Recursive ``codex exec`` needs to write session/app-server state. Pointing
    it at the user's normal CODEX_HOME fails under workspace-write even when
    network is enabled. The projection keeps mutable state inside the owner
    worktree, links the existing credential/config read-only, and installs only
    harness-owned runtime links. Credentials are never copied or modified.
    """

    source = (source_home or Path(os.environ.get("CODEX_HOME", "~/.codex"))).expanduser().resolve()
    destination = worktree / ".dispatch" / "nested-codex-home"
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o700)

    # Runtime projection identity follows the installed/canonical AGENT_HOME,
    # not the source-only task worktree containing this wrapper. Otherwise a
    # nested eligibility check compares a worktree-linked local CODEX_HOME with
    # the inherited canonical AGENT_HOME and rejects a valid recursive launch.
    projection_root = resolve_agent_home().resolve()
    installer = projection_root / "adapters" / "codex" / "bin" / "install-runtime-projection.sh"
    env = {**os.environ, "AGENT_HOME": str(projection_root), "CODEX_HOME": str(destination)}
    result = subprocess.run(
        [str(installer), "--skills-mode", "native"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise DispatchContractError(
            "nested-codex-home-projection-failed",
            (result.stderr or result.stdout).strip() or f"exit-{result.returncode}",
        )

    auth = source / "auth.json"
    if not auth.is_file():
        fallback = Path.home() / ".codex" / "auth.json"
        auth = fallback if fallback.is_file() else auth
    if not auth.is_file():
        raise DispatchContractError("nested-codex-auth-missing", str(auth))

    for name, target in (("auth.json", auth), ("config.toml", source / "config.toml")):
        if not target.is_file():
            continue
        link = destination / name
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise DispatchContractError("nested-codex-home-collision", str(link))
        link.symlink_to(target)
    return destination


def close_job_row(jobs: Path, slug: str, worktree: str, reason: str, reset: str, attempt_id: str | None = None) -> bool:
    """SD-15: flip this dispatch's own open row to done with a dead-<reason> note.

    Matches by (slug, worktree, status==open) under the same flock the writer uses,
    so a concurrent conductor appending other rows is serialized. Appends
    note=dead-<reason>[,reset=<reset>] to the pipe column (kv style). Idempotent:
    returns False if no matching open row is found. Homomorphic with the Claude wrapper.
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


def write_reset_cache(agent_home: Path, harness: str, reason: str, reset: str, jobs: Path | None = None) -> None:
    """SD-15↔SD-16: cache the last known limit reset for usage-check.sh to read.

    File `.dispatch/usage-reset.<harness>` holds one line: `<iso-ts> <reason> <reset>`.
    Best-effort — a cache write failure never blocks dispatch bookkeeping.
    """
    try:
        cache = (dispatch_state_root(jobs) if jobs else agent_home / ".dispatch") / f"usage-reset.{harness}"
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
    Otherwise returns None. Polls in 0.5s steps. ADAPTATION note:
    codex exec exits non-zero on retry exhaustion so this launch-watch axis is realized;
    a runtime that *hangs* on limit instead of exiting (see OpenCode #8203) escapes this
    watch and is caught later by dispatch-liveness's log scan instead.
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
    # Delegates to the one canonical resolver, passing this runtime's bundle
    # pointer (~/.codex/hearting) so codex's deliberate bundle-first priority
    # (immutable runtime activation, session pinning) is preserved without
    # forking the resolver's fallback chain.
    return _resolve_agent_home(runtime_pointer=Path.home() / ".codex" / "hearting")


def ensure_runtime_home_projection(worktree: Path) -> Path | None:
    """Expose the active Codex session store to Fleet without copying runtime state."""
    runtime_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve()
    link = worktree / ".dispatch" / "codex-home"
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            return None
        link.symlink_to(runtime_home, target_is_directory=True)
        return link
    except OSError:
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
    return 0


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
    still fails closed. Preserves Codex's owner-network contract and headless
    readiness gates, which live inside the probe itself, not here.
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
            "--child-harness", "codex",
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
        or row.get("child_harness") != "codex"
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


def validate_route_record(args: argparse.Namespace) -> int:
    routed = any((args.route_id, args.route_hash, args.route_node, args.registry_digest))
    if routed and not args.route_file:
        return fail("route-record-required", 65, route_id=args.route_id or "-")
    if not args.route_file:
        return 0
    required = ("route_id", "route_hash", "route_node", "registry_digest", "write_scope")
    missing = [name for name in required if not getattr(args, name)]
    if missing:
        return fail("route-metadata-missing", 65, fields=",".join(missing))
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
    command = [sys.executable, str(ROOT / "utilities" / "worker-route-guard.py"), "validate",
        "--route", args.route_file, "--node", args.route_node, "--cwd", args.worktree,
        "--artifact-root", args.artifact_root, "--capability", args.capability,
        "--intensity", args.intensity, "--write-scope", args.write_scope,
        "--route-id", args.route_id, "--route-hash", args.route_hash,
        "--registry-digest", args.registry_digest,
        "--unit", args.unit,
        "--launch-phase", args.action,
        "--model-role", args.model_role or "",
        "--model-profile", args.model_profile or ""]
    if args.attempt_id:
        command += ["--current-attempt", args.attempt_id]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        if result.stdout: print(result.stdout, end="")
        if result.stderr: print(result.stderr, end="", file=sys.stderr)
        return fail(
            "worker-route-validation-failed", result.returncode,
            route_file=args.route_file, registered="0", started="0",
            child_spawned="0",
        )
    args.route_validation = result.stdout.strip()
    # Run the dependency gate before runtime-projection and parent-registry
    # diagnostics so an invalid DAG edge is the stable first failure. The
    # authoritative registry path is revalidated immediately before claim.
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
    forced_sandbox = os.environ.get("CODEX_DISPATCH_SANDBOX_FORCE")
    if forced_sandbox:
        if forced_sandbox not in ("read-only", "workspace-write", "danger-full-access"):
            return fail("invalid-forced-dispatch-sandbox", 64, sandbox=forced_sandbox)
        args.sandbox = forced_sandbox
    _bind_runtime_parent(args)
    action = "start" if args.start else "register" if args.register else "dry-run"
    args.action = action
    if action == "dry-run":
        # Preview output must never mint or echo an identity that resembles a
        # registered attempt receipt.
        args.attempt_id = None
    if args.broker_request_id or args.launch_authority == "ancestor-broker":
        return fail("launch-broker-retired", 76, child_spawned="0")
    args.agent_home = resolve_agent_home()
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
        if args.owner_route_binding and (
            args.dispatch_depth != 1 or args.worker_type != "owner" or args.route_file
        ):
            raise OwnerRouteBindingError("owner-route-binding-tuple-invalid")
    except OwnerRouteBindingError as exc:
        return fail(str(exc), 65, child_spawned="0")
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
            home_root = resolve_agent_home() / ".dispatch" / "homes"
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
            build_result = subprocess.run(
                ["python3", str(build_home), args.profile, "--instance", args.slug, "--home-root", str(home_root)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if build_result.returncode != 0:
                if build_result.stdout:
                    print(build_result.stdout, end="")
                if build_result.stderr:
                    print(build_result.stderr, end="", file=sys.stderr)
                return fail("profile-build-failed", 3, profile=args.profile)
            profile_home = home_root / f"{args.slug}.{args.profile}"

    runtime_home_projection = None
    if args.start and profile_home is None:
        runtime_home_projection = ensure_runtime_home_projection(worktree)

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
                harness="codex",
                fallback_hop=args.fallback_hop,
                fallback_ordinal=args.fallback_ordinal,
                assignment_sha256=assignment_sha256,
            )
        except DispatchContractError as exc:
            return fail(exc.reason, 65, detail=exc.detail, child_spawned="0")
    args.nested_headless_network = nested_headless_network_enabled(args)
    try:
        validate_nested_owner_registry_projection(args)
    except DispatchContractError as e:
        return fail(e.reason, 73, detail=e.detail, child_spawned="0")
    args.nested_codex_home = None
    args.nested_codex_home_path = (
        worktree / ".dispatch" / "nested-codex-home"
        if args.nested_headless_network else None
    )
    if action == "start" and args.nested_headless_network:
        try:
            args.nested_codex_home = prepare_nested_codex_home(worktree)
        except DispatchContractError as e:
            return fail(e.reason, 73, detail=e.detail, child_spawned="0")
    prompt_name = (
        f"{args.slug}.{args.attempt_id}.codex.prompt.txt"
        if args.attempt_id
        else f"{args.slug}.codex.prompt.txt"
    )
    prompt_path = log_dir / prompt_name
    # Every registered attempt gets a distinct transcript.  Reusing the legacy
    # slug-only path lets a later retry append another turn to the same JSONL,
    # so harvesting the earlier row can accidentally select the newer verdict.
    # PID-less legacy readers still retain their slug-only fallback.
    log_name = (
        f"{args.slug}.{args.attempt_id}.codex.jsonl"
        if args.attempt_id
        else f"{args.slug}.codex.jsonl"
    )
    log_path = log_dir / log_name
    args.log_path = log_path
    try:
        ensure_owner_writable_dirs(args)
    except DispatchContractError as e:
        return fail(e.reason, 73, detail=e.detail, child_spawned="0")
    command = shell_command(args, prompt_path, log_path)

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
            # W7C producer lifecycle: the owner's open cycle (issued by
            # `artifact_producer.py begin` before the first write) is passed
            # through unchanged so stage workers write into the same
            # `campaigns/<camp>/cycles/<cyc>/artifacts/` and never issue a
            # second lineage.
            **{key: os.environ.get(key, "") for key in ARTIFACT_PRODUCER_CYCLE_ENV},
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
            "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
            "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
            "AGENT_DISPATCH_CURRENT_SANDBOX": effective_runtime_sandbox(args),
            "AGENT_DISPATCH_COMPLETION_MODE": (
                "supervised"
                if args.resolved_completion_delivery == "app-server-supervised"
                else "poll"
            ),
            **stage_session_environment(args),
        }
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
        if args.nested_codex_home is not None:
            dispatch_env["CODEX_HOME"] = str(args.nested_codex_home)
        elif profile_home is not None:
            dispatch_env["CODEX_HOME"] = str(profile_home)
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
        }
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
                    harness="codex",
                    transcript=log_path,
                    prompt_path=prompt_path,
                    target_pid=int(identity["pid"]),
                    target_start=identity["pid_start"],
                ),
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
            )
        except DispatchContractError as exc:
            fence_failure = read_launch_fence_failure(fence_failure_read_fd)
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
            if fence_failure is not None:
                reason = str(fence_failure["reason"])
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
            close_job_row(
                jobs, args.slug, args.worktree,
                "governor-reservation-transfer", "", args.attempt_id,
            )
            return fail(
                exc.reason, 75, detail=exc.detail,
                attempt_id=args.attempt_id, child_spawned="1",
            )
        read_launch_fence_failure(fence_failure_read_fd)
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
            elif (
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
    print(f"completion_delivery={args.resolved_completion_delivery}")
    print(f"parent_completion_delivery={args.parent_completion_delivery}")
    print(f"parent_completion_reason={getattr(args, 'parent_completion_reason', 'unspecified')}")
    print(f"managed_sidecar_state={getattr(args, 'managed_sidecar_state', 'not-started')}")
    print(f"managed_sidecar_reason={getattr(args, 'managed_sidecar_reason', '-')}")
    print(f"managed_sidecar_pid={getattr(args, 'managed_sidecar_pid', '-')}")
    print(f"managed_sealed_batch_id={getattr(args, 'managed_sealed_batch_id', '-')}")
    print(f"managed_sidecar_log={getattr(args, 'managed_sidecar_log', '-')}")
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
    print(f"model_role={settings['role']}")
    print(f"model_profile={settings['profile']}")
    print(f"model_tier={settings['tier']}")
    print(f"profile_granularity={settings['granularity']}")
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
    print(f"job_registry={jobs}")
    print("broker_lifecycle=retired")
    print(
        "governor_reservation="
        + (str(getattr(args, "governor_reservation", {}).get("state", "-")))
    )
    print(f"registry_authority={registry.source}")
    print(f"preview={1 if action == 'dry-run' else 0}")
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
    print(
        "child_spawned="
        + str(
            int(
                action == "start"
                and bool(args.attempt_claimed)
                and bool(getattr(args, "child_pid", None))
            )
        )
    )
    print(f"child_pid={getattr(args, 'child_pid', None) or '-'}")
    print(f"child_pid_start={getattr(args, 'child_pid_start', None) or '-'}")
    print(f"launch_heartbeat={getattr(args, 'launch_heartbeat', 'not-started')}")
    print(f"launch_lifecycle={args.launch_lifecycle}")
    print(f"launch_lifecycle_requested={args.launch_lifecycle_requested}")
    print(f"launch_lifecycle_reselection={args.launch_lifecycle_resolution.reselection}")
    print(f"runtime_sandbox={effective_runtime_sandbox(args)}")
    print(f"worker_exit={getattr(args, 'worker_exit', '-')}")
    print(f"worker_failure={getattr(args, 'worker_failure', None) or '-'}")
    print(f"terminal_verdict={getattr(args, 'terminal_verdict', None) or '-'}")
    for key, value in terminal_receipt_fields(
        getattr(args, "terminal_inspection", None)
    ).items():
        print(f"{key}={value}")
    print(f"require_hook_trust={1 if args.require_hook_trust else 0}")
    print(f"nested_headless_network={1 if args.nested_headless_network else 0}")
    print(f"nested_codex_home={args.nested_codex_home_path or '-'}")
    print(
        "nested_owner_writable_dirs="
        + (";".join(map(str, nested_owner_writable_dirs(args))) or "-")
    )
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
