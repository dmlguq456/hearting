#!/usr/bin/env python3
"""The lines every adapter wrapper's launch receipt shares.

A parent reads the same receipt whichever harness ran its child, so the
fields that describe completion delivery, the registry attempt, the launch
lifecycle and the worker's terminal result are printed here once. Each
wrapper keeps only the lines that translate its own runtime (model and
effort, permission or sandbox posture, nested homes). The OpenCode receipt
used to lack `completion_delivery`, `registry_lock`, `terminal_verdict` and
the sidecar fields because each wrapper kept its own copy (audit §4 #8, A21).
"""
from __future__ import annotations

import os
from typing import Iterable

from harness_capabilities import CARRIER_ENV
from parent_next_directive import receipt_lines as parent_next_receipt_lines


def _opencode_pre_carrier_notice(args) -> str | None:
    """One line when an OpenCode parent launches an owner without the wake carrier.

    The carrier env comes from the server plugin's `shell.env` hook, so its
    absence means the OpenCode server has not loaded the current plugin (an
    older plugin, or a V2 package it never loaded). Plugins load in the server,
    not the TUI, so relaunching only the session does not attach the carrier.
    The line names that state, the bounded wait this completion uses instead,
    and the server-level fix; it adds no gate or input.
    """
    if getattr(args, "parent_harness", None) != "opencode":
        return None
    if getattr(args, "parent_completion_delivery", "") != "poll-fallback":
        return None
    if getattr(args, "parent_completion_reason", "") != "parent-identity-unmatched":
        return None
    carrier = os.environ.get(CARRIER_ENV) or ""
    if carrier.startswith("opencode-turn:"):
        return None
    return ("opencode_carrier_notice=이 OpenCode 서버에 하팅 완료 깨움 플러그인이 로드되지 않았다 — "
            "이번 완료는 위 bounded-wait 명령으로 확인한다. 자동 깨움은 TUI 세션이 아니라 "
            "OpenCode 서버가 현재 플러그인을 다시 읽어야 붙는다"
            "(서비스 모드는 opencode service restart, 같은 서버의 모든 세션에 영향)")


def completion_lines(args) -> list[str]:
    """How the parent learns this attempt finished, and the completion sidecar's state."""
    return [
        f"completion_delivery={getattr(args, 'resolved_completion_delivery', None) or '-'}",
        f"completion_delivery_reason={getattr(args, 'completion_delivery_reason', None) or 'not-applicable'}",
        f"parent_completion_delivery={getattr(args, 'parent_completion_delivery', None) or '-'}",
        f"parent_completion_reason={getattr(args, 'parent_completion_reason', None) or 'unspecified'}",
        f"parent_completion_reason_class={getattr(args, 'parent_completion_reason_class', '-')}",
        f"managed_sidecar_state={getattr(args, 'managed_sidecar_state', 'not-started')}",
        f"managed_sidecar_reason={getattr(args, 'managed_sidecar_reason', '-')}",
        f"managed_sidecar_pid={getattr(args, 'managed_sidecar_pid', '-')}",
        f"managed_sealed_batch_id={getattr(args, 'managed_sealed_batch_id', '-')}",
        f"managed_sidecar_log={getattr(args, 'managed_sidecar_log', '-')}",
    ]


def terminal_fields(terminal: dict | None) -> dict[str, str]:
    """Bounded typed terminal metadata for the launch receipt."""
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


def attempt_lines(args, *, jobs, registry_source: str, action: str, launch_state: str,
                  after_lifecycle: Iterable[str] = (), before_early_death: Iterable[str] = ()) -> list[str]:
    """The registry attempt, the parent's next action, the lifecycle and the worker's result.

    `after_lifecycle` and `before_early_death` are the wrapper's own translated lines,
    kept at the place each wrapper printed them before.
    """
    lines = [
        f"job_registry={jobs}",
        "broker_lifecycle=retired",
        "governor_reservation=" + str(getattr(args, "governor_reservation", {}).get("state", "-")),
        f"registry_authority={registry_source}",
        f"preview={1 if action == 'dry-run' else 0}",
        f"attempt_id={args.attempt_id or '-'}",
        f"launch_authority={args.launch_authority}",
        f"fallback_ordinal={args.fallback_ordinal}",
        f"fallback_hop={args.fallback_hop}",
        f"execution_surface={args.execution_surface}",
        f"registered_worker={int(bool(args.registered_worker))}",
        f"registry_lock={jobs}.lock",
        f"duplicate_attempt={0 if args.attempt_claimed or action == 'dry-run' else 1}",
        f"launch_state={launch_state}",
        f"registered={1 if args.attempt_claimed else 0}",
        f"started={1 if action == 'start' and args.attempt_claimed else 0}",
    ]
    spawned_child = int(action == "start" and bool(args.attempt_claimed)
                        and bool(getattr(args, "child_pid", None)))
    lines.append(f"child_spawned={spawned_child}")
    if spawned_child:
        # The receipt states the parent's next action itself, so a parent does not
        # carry the completion-delivery taxonomy in its own instructions.
        lines += parent_next_receipt_lines(getattr(args, "parent_completion_delivery", ""),
                                           args.attempt_id, agent_home=args.agent_home)
        notice = _opencode_pre_carrier_notice(args)
        if notice:
            lines.append(notice)
    lines += [
        f"child_pid={getattr(args, 'child_pid', None) or '-'}",
        f"child_pid_start={getattr(args, 'child_pid_start', None) or '-'}",
        f"launch_heartbeat={getattr(args, 'launch_heartbeat', 'not-started')}",
        f"launch_lifecycle={args.launch_lifecycle}",
        f"launch_lifecycle_requested={args.launch_lifecycle_requested}",
        f"launch_lifecycle_reselection={args.launch_lifecycle_resolution.reselection}",
        f"launch_lifecycle_override={args.launch_lifecycle_resolution.override}",
        *after_lifecycle,
        f"worker_exit={getattr(args, 'worker_exit', '-')}",
        f"worker_failure={getattr(args, 'worker_failure', None) or '-'}",
        f"terminal_verdict={getattr(args, 'terminal_verdict', None) or '-'}",
        *(f"{key}={value}" for key, value in terminal_fields(getattr(args, "terminal_inspection", None)).items()),
        *before_early_death,
    ]
    early_death = getattr(args, "early_death", None)
    if early_death:
        reason, reset = early_death
        lines += [f"early_death={reason}", f"early_death_reset={reset or '-'}",
                  f"row_closed=done,note=dead-{reason}"]
    else:
        lines.append("early_death=-")
    return lines


def identity_lines(args, action: str, *, after_scope: Iterable[str] = ()) -> Iterable[str]:
    """The launch's dispatch identity, from its action through its parent slug."""
    yield f"status={action}"
    yield f"worktree={args.worktree}"
    yield f"artifact_root={args.artifact_root}"
    yield "artifact_write_scope=canonical-only"
    yield from after_scope
    yield f"slug={args.slug}"
    yield f"capability={args.capability}"
    yield f"capability_mode={args.capability_mode}"
    yield f"worker_mode={args.worker_mode or '-'}"
    yield f"qa={args.qa}"
    yield f"intensity={args.intensity}"
    yield f"dispatch_depth={args.dispatch_depth}"
    yield f"eligibility_probe={getattr(args, 'eligibility_probe', None) or '-'}"
    yield f"parent={args.parent_slug or '-'}"


def worker_lines(args) -> Iterable[str]:
    """The worker's role, contract, owner and route."""
    yield f"worker_role={args.worker_role or '-'}"
    yield f"worker_type={args.worker_type}"
    yield f"assigned_contract={args.assigned_contract}"
    yield f"unit={args.unit or '-'}"
    yield f"owner={args.capability_owner or '-'}"
    yield f"owner_harness={args.owner_harness or '-'}"
    yield f"route_file={args.route_file or '-'}"
    yield f"route_validation={getattr(args, 'route_validation', None) or '-'}"


def model_lines(args, settings, *, config_state, main_session_only_policy,
                after_source=lambda: ()) -> Iterable[str]:
    """The resolved model selection, from its source through the model name.

    The callables are read at the line they print, as the wrappers did inline.
    """
    yield f"model_source={settings['source']}"
    yield from after_source()
    yield f"model_pin_status={settings.get('pin_status', 'none')}"
    if getattr(args, 'explicit_adapter', None):
        yield f"explicit_adapter={args.explicit_adapter}"
    if settings.get("pin_model"):
        yield f"model_pin={settings['pin_model']}"
    yield f"model_role={settings['role']}"
    yield f"model_profile={settings['profile']}"
    yield f"model_tier={settings['tier']}"
    config_source, config_reason = config_state()
    yield f"model_config_source={config_source}"
    yield f"model_config_reason={config_reason}"
    yield f"profile_granularity={settings['granularity']}"
    yield f"main_session_only_policy={main_session_only_policy()}"
    for key, value in sorted(getattr(args, "profile_selection_receipt", {}).items()):
        yield f"{key}={value}"
    yield f"model={settings['model']}"


def leg_lines(args, *, leg_fields) -> Iterable[str]:
    """The route node's leg and the parent's cross-harness and sole-gate markers."""
    leg_class, auxiliary_check = leg_fields(args)
    yield f"leg_class={leg_class}"
    yield f"auxiliary_check={auxiliary_check}"
    yield f"parent_cross={os.environ.get('AGENT_DISPATCH_PARENT_CROSS', '-')}"
    yield f"sole_gate={os.environ.get('AGENT_DISPATCH_SOLE_GATE', '-')}"


def prompt_lines(prompt_source: str, prompt_path, log_path) -> Iterable[str]:
    yield f"prompt_source={prompt_source}"
    yield f"prompt_file={prompt_path}"
    yield f"log_file={log_path}"
