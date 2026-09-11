"""Shared parent completion selection and pre-spawn delivery ownership.

The child runtime owns execution. The witnessed parent runtime owns receipt
transport. All adapters use this boundary before publishing a started child.
"""
from __future__ import annotations
import os
import json
import re
from pathlib import Path
from codex_managed_dispatch import (
    MANAGED_PARENT_DELIVERY, ManagedDispatchError, probe_managed_codex_parent,
    launch_managed_completion_sidecar, registered_parent_delivery,
)
from dispatch_contract import DispatchContractError, annotate_attempt_row


def interactive_parent_identity(environ=None) -> tuple[str, str]:
    """Resolve the caller's native identity, independently of the child adapter."""
    env = os.environ if environ is None else environ
    sessions = {
        "codex": env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID") or "",
        "claude": env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDE_SESSION_ID") or "",
        "opencode": env.get("OPENCODE_SESSION_ID") or "",
    }
    explicit = env.get("AGENT_DISPATCH_CALLER_HARNESS") or env.get("AGENT_DISPATCH_CURRENT_HARNESS")
    if explicit:
        if explicit not in sessions:
            raise DispatchContractError("caller-harness-invalid")
        return explicit, sessions[explicit]
    detected = [(harness, session) for harness, session in sessions.items() if session]
    if len(detected) > 1:
        raise DispatchContractError("caller-harness-ambiguous")
    return detected[0] if detected else ("", "")


def default_parent_session_id(environ=None) -> str | None:
    env = os.environ if environ is None else environ
    return env.get("AGENT_DISPATCH_PARENT_SESSION_ID") or interactive_parent_identity(env)[1] or None


def default_parent_harness(fallback: str, environ=None) -> str:
    """A selected child's runtime never replaces its caller's identity."""
    env = os.environ if environ is None else environ
    return interactive_parent_identity(env)[0] or env.get("AGENT_DISPATCH_OWNER_HARNESS") or fallback


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


def effective_parent_cwd(args) -> str:
    """Use explicit/native parent evidence, then the actual launch directory.

    A Git worktree relationship is not evidence of where the parent lives.
    """
    if getattr(args, "parent_cwd", None):
        return os.path.realpath(args.parent_cwd)
    if getattr(args, "parent_harness", "codex") == "codex":
        derived = _codex_thread_cwd(getattr(args, "parent_session_id", None))
        if derived:
            return derived
    return os.path.realpath(os.getcwd())


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
