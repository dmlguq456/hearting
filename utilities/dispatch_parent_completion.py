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
    ManagedDispatchError, probe_managed_codex_parent,
    registered_parent_delivery,
)
from dispatch_contract import (DispatchContractError, annotate_attempt_row,
                               parse_registry_metadata, supervisor_lease_is_held)
# Who the caller is, and which session a new attempt reports to, is a route
# authority judgment (`route_authority`); these names remain for importers.
from route_authority import (caller_identity as interactive_parent_identity,  # noqa: F401
                             default_parent_harness, default_parent_session_id)
from harness_capabilities import CARRIER_ENV, parent_completion as declared_parent_completion


def worker_runtime_identity(harness: str) -> dict[str, str]:
    """The launched worker becomes the caller of its own subsequent children."""
    return {"AGENT_DISPATCH_CURRENT_HARNESS": harness,
            "AGENT_DISPATCH_CALLER_HARNESS": harness}


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


def parent_supervisor_is_live(args) -> bool:
    attempt = getattr(args, "parent_attempt_id", None) or os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    jobs = getattr(args, "jobs", None) or os.environ.get("AGENT_DISPATCH_JOBS")
    if not attempt or not jobs:
        return False
    try:
        matches = []
        for line in Path(jobs).read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") == attempt:
                matches.append((fields[1], metadata))
        return (len(matches) == 1 and matches[0][0] in {"open", "running"}
                and supervisor_lease_is_held(jobs, matches[0][1]))
    except (OSError, ValueError, DispatchContractError):
        return False


def _parent_reachable(args, declared: dict) -> bool:
    """The declared carrier reaches this parent (`harness_capabilities` parent_proof)."""
    if declared["parent_proof"] == "runtime-hook":
        return True
    if declared["parent_proof"] == "carrier-env":
        return bool(args.parent_session_id) and (
            os.environ.get(CARRIER_ENV) == f"{declared['carrier']}:{args.parent_session_id}")
    try:
        harness, session = interactive_parent_identity()
    except DispatchContractError:
        return False
    return bool(session) and (harness, session) == (args.parent_harness, args.parent_session_id)


def resolve_parent_completion_delivery(args, *, probe=probe_managed_codex_parent) -> str:
    """Select completion from the native parent runtime, not the child.

    What each parent runtime carries is its adapter's declaration
    (`harness_capabilities`); this is the one place that decides from it.
    """
    args.managed_gateway_binding = None
    if _direct_registered_parent(args):
        declared = declared_parent_completion(args.parent_harness)
        if declared["carrier"] and _parent_reachable(args, declared):
            args.parent_completion_reason = declared["reason"]
            return declared["carrier"]
        args.parent_completion_reason = "parent-identity-unmatched"
        return "poll-fallback"
    if getattr(args, "capability", None) == "unit" and os.environ.get("AGENT_DISPATCH_CHILD") == "1":
        # A unit leg a worker started: a supervisor joins only the children it declared, so this leg would never
        # wake its caller. The caller runs the printed bounded wait and reads the result there.
        args.parent_completion_reason = "caller-worker-waits"
        return "poll-fallback"
    if parent_supervisor_is_live(args):
        args.parent_completion_reason = "parent-attempt-owned"
        return "parent-runtime-supervised"
    args.parent_completion_reason = "parent-supervisor-unavailable"
    return "poll-fallback"


def validate_interactive_parent_launch(args) -> None:
    """A parent whose runtime refuses a model-owned wait never silently gets one."""

    if not (
        _direct_registered_parent(args)
        and args.parent_completion_delivery == "poll-fallback"
        and declared_parent_completion(args.parent_harness)["without_carrier"] == "refuse"
    ):
        return
    if getattr(args, "allow_unmanaged_parent_poll", False):
        args.parent_completion_reason = "operator-authorized-unmanaged-poll"
        return
    raise DispatchContractError(
        "native-parent-identity-unproven",
        f"{args.parent_harness} completion delivery requires the calling session to be the "
        f"registered parent {args.parent_session_id}; inspect the native session identity",
    )


def launch_parent_completion_sidecar(
    args,
    jobs: Path,
    *, launch=None, annotate=annotate_attempt_row,
) -> None:
    """Prelaunch one exact joiner before the direct child spawn claim."""

    args.managed_sidecar_state = "not-selected"
    args.managed_sidecar_reason = "-"
    if args.parent_completion_delivery != "codex-native-queue":
        return
    try:
        if launch is None:
            from codex_queue_dispatch import launch_codex_queue_completion_sidecar
            launch = launch_codex_queue_completion_sidecar
        sidecar = launch(
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
