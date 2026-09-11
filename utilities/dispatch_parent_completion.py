"""Shared parent completion selection and pre-spawn delivery ownership.

The child runtime owns execution. The witnessed parent runtime owns receipt
transport. All adapters use this boundary before publishing a started child.
"""
from __future__ import annotations
import os
from pathlib import Path
from codex_managed_dispatch import (
    MANAGED_PARENT_DELIVERY, ManagedDispatchError, probe_managed_codex_parent,
    launch_managed_completion_sidecar, registered_parent_delivery,
)
from dispatch_contract import DispatchContractError, annotate_attempt_row


def _direct_registered_parent(args) -> bool:
    return (
        getattr(args, "action", "") in {"register", "start"}
        and args.dispatch_depth == 1
        and args.execution_surface == "registered-headless"
        and bool(args.registered_worker)
        and bool(args.parent_session_id)
        and os.environ.get("AGENT_DISPATCH_CHILD") != "1"
    )


def resolve_parent_completion_delivery(args, *, probe=probe_managed_codex_parent) -> str:
    """Select completion from the witnessed parent, independently of the child."""
    args.managed_gateway_binding = None
    current_thread = os.environ.get("CODEX_THREAD_ID") or os.environ.get(
        "CODEX_SESSION_ID"
    )
    direct_registered = _direct_registered_parent(args)
    if (
        direct_registered
        and args.parent_harness == "codex"
        and bool(current_thread)
        and args.parent_session_id == current_thread
    ):
        try:
            args.managed_gateway_binding = probe(
                parent_harness=args.parent_harness,
                parent_session_id=args.parent_session_id,
            )
        except ManagedDispatchError as exc:
            if os.environ.get("AGENT_CODEX_MANAGED_GATEWAY") == "1":
                args.parent_completion_reason = str(exc)
                args.parent_completion_reason_class = (
                    getattr(exc, "reason_class", "") or "-"
                )
            else:
                args.parent_completion_reason = (
                    "interactive-auto-wake-unsupported"
                )
                args.parent_completion_reason_class = "-"
            return "poll-fallback"
        if getattr(args.managed_gateway_binding, "thread_advanced", False):
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


def validate_interactive_parent_launch(args) -> None:
    """Never let an ordinary Codex parent enter a model-owned wait loop."""

    if not (
        _direct_registered_parent(args)
        and args.parent_harness == "codex"
        and args.parent_completion_delivery == "poll-fallback"
    ):
        return
    if getattr(args, "allow_unmanaged_parent_poll", False):
        args.parent_completion_reason = "operator-authorized-unmanaged-poll"
        return
    raise DispatchContractError(
        "managed-entry-required",
        "unmanaged interactive Codex parents cannot register or start a worker without a completion carrier; restart through preflight.sh managed-entry",
    )


def launch_parent_completion_sidecar(
    args,
    jobs: Path,
    *, launch=launch_managed_completion_sidecar, annotate=annotate_attempt_row,
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
        sidecar = launch(
            binding=binding,
            jobs=jobs,
            parent_session_id=args.parent_session_id or "",
            attempt_ids={args.attempt_id},
        )
    except ManagedDispatchError as exc:
        args.managed_sidecar_state = "launch-failed"
        args.managed_sidecar_reason = str(exc)
        try:
            annotate(
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
        recorded = annotate(
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

def validate_registered_delivery(args, jobs, *, read=registered_parent_delivery):
    """Registration seals one transport; a later start cannot silently replace it."""
    if not args.attempt_claimed:
        return
    recorded = read(jobs, args.attempt_id)
    if recorded != args.parent_completion_delivery:
        raise DispatchContractError(
            "attempt-parent-delivery-changed",
            f"registered={recorded} current={args.parent_completion_delivery}")
