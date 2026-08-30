#!/usr/bin/env python3
"""Shared adapter projection for portable stage-session metadata and ledgers."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import sys

import artifact_producer
from dispatch_contract import DispatchContractError, validate_attempt_metadata

STATE_BUCKET = (".runtime", "stage-sessions")


def default_state_dir(artifact_root: Path, route_id: str) -> Path:
    """`<artifact-root>/.runtime/stage-sessions/<route_id>` (CORE §3 `C-RT`)."""
    return Path(artifact_root).joinpath(*STATE_BUCKET) / route_id


# SD-119 R3: a chain-scoped sub-session's worker contract obligation is that
# `dispatch_subsession_handoff.classify_handoff()` returning anything but
# `"ok"` already keeps this index from ever starting (dispatch_subsession_
# advance.py's chain-advance gate refuses claim/register/start beforehand) --
# so by construction a bound successor session is never mid-edit without a
# handoff that classified `ok` at start time. The worker contract obligation
# layered on top of that server-side gate is procedural, not a second runtime
# check: the successor MUST read that handoff before its first edit
# (`roles/worker-bootstrap.md`), since it is this session's only carrier of
# the predecessor's completed items, exact next command, invariants, and
# forbidden files -- nothing else transfers context across the attempt
# boundary. `metadata()` below is unchanged by R3.


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--subsession-id")
    parser.add_argument("--subsession-index", type=int)
    parser.add_argument("--subsession-count", type=int)
    parser.add_argument("--subsession-mode", choices=("serial", "parallel"))
    parser.add_argument("--subsession-purpose", choices=("planned", "gap-retry"), default="planned")
    parser.add_argument("--session-chain-id")
    parser.add_argument("--phase-brief")
    parser.add_argument("--stage-authority", type=int, choices=(0, 1), default=1)
    parser.add_argument("--fixed-file", action="append", default=[])
    parser.add_argument("--narrow-verify")
    parser.add_argument("--expected-round-trips", type=int)
    parser.add_argument("--state-dir")


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_files(values: list[str]) -> str:
    return _sha_text("\0".join(sorted(values)))


def bind(args: argparse.Namespace, *, artifact_root: str | Path, action: str) -> None:
    """Validate a complete declaration, derive its ledger, and initialize it."""

    values = (
        args.subsession_id, args.subsession_index, args.subsession_count,
        args.subsession_mode, args.session_chain_id, args.phase_brief,
        args.narrow_verify, args.expected_round_trips,
    )
    if any(value is not None for value in values) and not all(value is not None for value in values):
        raise DispatchContractError("subsession-arguments-incomplete", "all stage-session axes are required")
    if not args.subsession_id:
        if args.stage_authority != 1:
            raise DispatchContractError(
                "stage-authority-zero-without-subsession", str(args.route_node or "")
            )
        args.state_ledger = ""
        args.fixed_files_sha256 = ""
        args.narrow_verify_sha256 = ""
        args.phase_brief_sha256 = ""
        return
    if args.stage_authority != 0:
        raise DispatchContractError("subsession-stage-authority-forbidden", args.subsession_id)
    if args.dispatch_depth != 2 or not args.route_id or not args.route_node:
        raise DispatchContractError("subsession-route-binding-invalid", args.subsession_id)
    if not args.attempt_id:
        if action == "dry-run":
            args.attempt_id = "att-dry-run-stage-session"
        else:
            raise DispatchContractError("subsession-attempt-id-missing", args.subsession_id)
    worktree = Path(args.worktree).resolve()
    fixed: list[str] = []
    for raw in args.fixed_file:
        path = Path(raw)
        if not path.is_absolute():
            path = worktree / path
        path = path.resolve(strict=False)
        try:
            path.relative_to(worktree)
        except ValueError as exc:
            raise DispatchContractError("subsession-fixed-file-outside-worktree", str(path)) from exc
        if any(char in raw for char in "*?[]"):
            raise DispatchContractError("subsession-fixed-file-not-exact", raw)
        fixed.append(str(path))
    args.fixed_file = sorted(set(fixed))
    if not args.fixed_file:
        raise DispatchContractError("subsession-fixed-files-missing", args.subsession_id)
    phase_brief = Path(args.phase_brief).resolve()
    if not phase_brief.is_file():
        raise DispatchContractError("subsession-phase-brief-missing", str(phase_brief))
    args.phase_brief = str(phase_brief)
    root = Path(artifact_root).resolve()
    state_dir = (
        Path(args.state_dir).resolve()
        if args.state_dir
        else default_state_dir(root, args.route_id)
    )
    try:
        state_dir.relative_to(root)
    except ValueError as exc:
        raise DispatchContractError("subsession-state-dir-outside-artifact-root", str(state_dir)) from exc
    args.state_ledger = str(state_dir / f"{args.attempt_id}.md")
    # W7D: the ledger is runtime state, never a durable artifact.  Once the
    # producer cutover is active a legacy top-level bucket (the pre-W7D
    # default was `plans/stage-sessions/...`) is write-denied, so the same
    # oracle hooks use decides here before anything is created.
    verdict = artifact_producer.check_write(root, Path(args.state_ledger))
    if verdict.get("verdict") != "allow":
        raise DispatchContractError(
            "subsession-state-dir-write-denied",
            f"{verdict.get('reason')}: {args.state_ledger}",
        )
    args.fixed_files_sha256 = _sha_files(args.fixed_file)
    args.narrow_verify_sha256 = _sha_text(args.narrow_verify)
    args.phase_brief_sha256 = hashlib.sha256(phase_brief.read_bytes()).hexdigest()
    axes = {
        "attempt_schema_version": 2,
        "dispatch_depth": args.dispatch_depth,
        "transport": "headless",
        "execution_surface": args.execution_surface,
        "registered_worker": args.registered_worker,
        "fallback_hop": args.fallback_hop,
        "route_id": args.route_id,
        "route_node": args.route_node,
        "subsession_id": args.subsession_id,
        "stage_authority": args.stage_authority,
        "session_chain_id": args.session_chain_id,
        "subsession_index": args.subsession_index,
        "subsession_count": args.subsession_count,
        "subsession_mode": args.subsession_mode,
        "subsession_purpose": args.subsession_purpose,
        "phase_brief": args.phase_brief,
        "phase_brief_sha256": args.phase_brief_sha256,
        "state_ledger": args.state_ledger,
        "fixed_files_sha256": args.fixed_files_sha256,
        "narrow_verify_sha256": args.narrow_verify_sha256,
        "expected_round_trips": args.expected_round_trips,
    }
    if args.subsession_mode == "parallel":
        # The sealed batch reservation is injected later; the adapter wrapper
        # still refuses a standalone parallel declaration before registration.
        axes["parallel_group"] = getattr(args, "replica_batch_expectation", None) or "pending-batch"
    validate_attempt_metadata(axes)
    if action not in {"register", "start"}:
        return
    command = [
        sys.executable, str(Path(__file__).resolve().parent / "worker-state-ledger.py"),
        "init", "--path", args.state_ledger, "--attempt-id", args.attempt_id,
        "--current-slice", f"{args.route_node}/{args.subsession_id}",
        "--next-action", f"read {args.phase_brief}",
        "--invariant", f"one stage gate remains owned by {args.capability_owner or 'depth-1 owner'}",
    ]
    for file in args.fixed_file:
        command += ["--fixed-file", file]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise DispatchContractError(
            "subsession-ledger-init-failed", (result.stderr or result.stdout).strip()
        )


def metadata(args: argparse.Namespace) -> str:
    if not getattr(args, "subsession_id", None):
        return ""
    return (
        f",subsession_id={args.subsession_id},stage_authority=0"
        f",session_chain_id={args.session_chain_id}"
        f",subsession_index={args.subsession_index},subsession_count={args.subsession_count}"
        f",subsession_mode={args.subsession_mode},subsession_purpose={args.subsession_purpose}"
        f",phase_brief={args.phase_brief},phase_brief_sha256={args.phase_brief_sha256}"
        f",state_ledger={args.state_ledger},fixed_files_sha256={args.fixed_files_sha256}"
        f",narrow_verify_sha256={args.narrow_verify_sha256}"
        f",expected_round_trips={args.expected_round_trips}"
    )


def environment(args: argparse.Namespace) -> dict[str, str]:
    if not getattr(args, "subsession_id", None):
        return {"AGENT_DISPATCH_STAGE_AUTHORITY": "1"}
    return {
        "AGENT_DISPATCH_SUBSESSION_ID": args.subsession_id,
        "AGENT_DISPATCH_SESSION_CHAIN_ID": args.session_chain_id,
        "AGENT_DISPATCH_SUBSESSION_MODE": args.subsession_mode,
        "AGENT_DISPATCH_SUBSESSION_PURPOSE": args.subsession_purpose,
        "AGENT_DISPATCH_STAGE_AUTHORITY": "0",
        "AGENT_WORKER_STATE_LEDGER": args.state_ledger,
        "AGENT_WORKER_PHASE_BRIEF": args.phase_brief,
        "AGENT_WORKER_FIXED_FILES_SHA256": args.fixed_files_sha256,
        "AGENT_WORKER_NARROW_VERIFY": args.narrow_verify,
    }


def prompt_fragment(args: argparse.Namespace) -> str:
    if not getattr(args, "subsession_id", None):
        return ""
    files = "\n".join(f"  - {file}" for file in args.fixed_file)
    return (
        "Stage sub-session contract:\n"
        f"- sub-session: {args.subsession_id} ({args.subsession_index}/{args.subsession_count}, {args.subsession_mode}, {args.subsession_purpose})\n"
        "- stage_authority: 0; never publish the route-node completion marker\n"
        f"- phase brief: {args.phase_brief}\n"
        f"- persistent compact anchor: {args.state_ledger}\n"
        f"- narrow verify: {args.narrow_verify}\n"
        "- fixed files (exhaustive; out-of-list work stops with a handoff):\n"
        f"{files}\n"
        f"- after at most three edits or one verify round trip, update: python3 {Path(__file__).resolve().parent / 'worker-state-ledger.py'} update --path {args.state_ledger} --attempt-id {args.attempt_id} ...\n"
        "- read the phase brief, prior bounded handoff, and ledger; do not reload the full spec unless the brief names it\n\n"
    )
