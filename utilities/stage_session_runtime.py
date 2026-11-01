#!/usr/bin/env python3
"""Shared adapter projection for portable stage-session metadata and ledgers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import artifact_producer
from stage_session_contract import (
    is_glob_path, report_path_from_brief, slice_files_sha256, slice_text_sha256,
)
from dispatch_contract import DispatchContractError, validate_attempt_metadata
# The declaration judgment is a route authority one; `declared` stays for importers.
from route_authority import declared_subsession as declared  # noqa: F401

STATE_BUCKET = (".runtime", "stage-sessions")
_BRIEF_PATH_TOKEN = re.compile(
    r"(?<![A-Za-z0-9._~+*/?{}\[\]!@#$%^&=:-])"
    r"[A-Za-z0-9._~+*/?{}\[\]!@#$%^&=:-]*"
    r"/[A-Za-z0-9._~+*/?{}\[\]!@#$%^&=:-]*"
)


def _artifact_fence(args, worktree: Path) -> tuple[list[str], list[str]] | None:
    """Derive a sub-session fence from its bound route and brief.

    An artifact-only node's list is the brief's and the declared artifact outputs. A node that
    also has source scope keeps its declared fixed files and adds the brief's artifact outputs
    that fall in its artifact scope (a serial or gap-retry session); a parallel slice keeps its
    declared list, so slices stay disjoint. Write authority is the node's `write_scope` and the
    write oracle, not this list.
    """
    route_file = getattr(args, "route_file", None)
    if not route_file:
        return None
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(route, dict):
        return None
    import artifact_context
    node_scope = next((node.get("write_scope", []) for node in route.get("nodes", [])
                       if node.get("id") == args.route_node), [])
    mixed = any(artifact_context.classify_scope(value) != "artifact" for value in node_scope)
    if mixed and (getattr(args, "subsession_mode", None) == "parallel"
                  or not any(artifact_context.classify_scope(value) == "artifact" for value in node_scope)):
        return None
    from worker_bootstrap import (
        _child_scope_environment, _linked_slice_source_worktree, resolve_node_scope,
    )
    source_worktree = _linked_slice_source_worktree(args, route)
    environ = _child_scope_environment(args, route, os.environ, source_worktree)
    scope = resolve_node_scope(
        route, args.route_node, environ,
        parent_attempt_id=getattr(args, "parent_attempt_id", None),
        route_file=route_file, source_worktree=source_worktree,
        source_worktree_verified=source_worktree is not None,
    )
    if not scope.output_dir or not scope.write_scope:
        return None
    artifact_values = [value for value in scope.write_scope
                       if artifact_context.classify_scope(value) == "artifact"]
    if not mixed and len(artifact_values) != len(scope.write_scope):
        return None

    output_dir = Path(scope.output_dir).resolve(strict=False)
    artifact_scopes = []
    for value in artifact_values:
        path = Path(value)
        if path.is_absolute():
            try:
                value = path.relative_to(output_dir).as_posix()
            except ValueError:
                continue
        artifact_scopes.append(value)

    def exact_outputs(values):
        found = set()
        for value in values:
            if not isinstance(value, str) or not value or artifact_context.is_semantic_output(value):
                continue
            if (any(char in value for char in "*?[]<>{}") or value.endswith("/")
                    or "://" in value):
                continue
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = output_dir / candidate
            candidate = candidate.resolve(strict=False)
            if candidate.is_dir():
                continue
            try:
                relative = candidate.relative_to(output_dir).as_posix()
            except ValueError:
                continue
            if relative and artifact_context.scope_matches(relative, artifact_scopes):
                found.add(str(candidate))
        return found

    brief = Path(args.phase_brief).read_text(encoding="utf-8")
    def strip_emphasis(value):
        for delimiter in ("**", "__", "*", "_"):
            if (value.startswith(delimiter) and value.endswith(delimiter)
                    and len(value) > 2 * len(delimiter)):
                return value[len(delimiter):-len(delimiter)]
        return value

    tokens = [strip_emphasis(match.group(0).rstrip("."))
              for match in _BRIEF_PATH_TOKEN.finditer(brief)]
    outputs = exact_outputs(tokens)
    declared_outputs = exact_outputs(args.fixed_file)
    if mixed:
        return sorted(set(args.fixed_file) | outputs), []
    effective = outputs | declared_outputs
    if not effective:
        effective = exact_outputs(scope.outputs)
    if not effective:
        # With no concrete brief or output paths, carry the node's declared
        # artifact write patterns into the ledger as bounded directory fences.
        effective = {
            str(output_dir / value)
            for value in artifact_scopes
            if (not artifact_context.is_semantic_output(value)
                and not value.endswith("/")
                and (value.endswith("/**")
                     or not any(char in value for char in "*?[]<>{}")))
        }
    effective = sorted(effective)
    readonly = sorted(set(args.fixed_file) - declared_outputs)
    return effective, readonly


def default_state_dir(artifact_root: Path, route_id: str) -> Path:
    """`<artifact-root>/.runtime/stage-sessions/<route_id>` (CORE §3 `C-RT`)."""
    return Path(artifact_root).joinpath(*STATE_BUCKET) / route_id


def _resolve_report_output(args) -> str:
    """Resolve the phase-brief report under this route node's exact cycle scope."""
    route_file = getattr(args, "route_file", None)
    if not route_file or not getattr(args, "phase_brief", None):
        return ""
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return ""
    if not isinstance(route, dict):
        return ""
    nodes = route.get("nodes")
    if not isinstance(nodes, list):
        return ""
    node = next((item for item in nodes
                 if isinstance(item, dict) and item.get("id") == args.route_node), None)
    if node is None:
        return ""
    brief_text = Path(args.phase_brief).read_text(encoding="utf-8")
    raw = report_path_from_brief(
        brief_text,
        session_id=args.subsession_id,
        route_node=args.route_node,
        node=node,
    )
    if not raw:
        return ""
    import artifact_context
    from worker_bootstrap import _child_scope_environment, resolve_node_scope

    # Reports belong to the sealed route's cycle. The wrapper separately
    # validates the execution worktree and exact live parent before launch.
    environ = _child_scope_environment(args, route, os.environ)
    if route.get("cwd"):
        environ["AGENT_DISPATCH_WORKTREE"] = str(route["cwd"])
    scope = resolve_node_scope(
        route, args.route_node, environ,
        parent_attempt_id=getattr(args, "parent_attempt_id", None),
        route_file=route_file,
    )
    if not scope.output_dir:
        if not report_path_from_brief(
                brief_text, session_id=args.subsession_id,
                route_node=args.route_node, node=node, explicit_only=True):
            return ""
        raise DispatchContractError("subsession-report-cycle-output-missing", args.subsession_id)
    output_dir = Path(scope.output_dir).resolve(strict=False)
    candidate = output_dir / raw
    resolved = candidate.resolve(strict=False)
    if resolved != candidate:
        raise DispatchContractError("subsession-report-path-symlink", str(candidate))
    try:
        relative = resolved.relative_to(output_dir).as_posix()
    except ValueError as exc:
        raise DispatchContractError("subsession-report-path-outside-cycle", str(resolved)) from exc
    artifact_scopes = []
    for value in scope.write_scope:
        if artifact_context.classify_scope(value) != "artifact":
            continue
        path = Path(value)
        if path.is_absolute():
            try:
                value = path.relative_to(output_dir).as_posix()
            except ValueError:
                continue
        artifact_scopes.append(value)
    if not artifact_context.scope_matches(relative, artifact_scopes):
        raise DispatchContractError("subsession-report-path-outside-write-scope", relative)
    verdict = artifact_producer.check_write(Path(route["artifact_root"]).resolve(), resolved)
    if verdict.get("verdict") != "allow":
        raise DispatchContractError(
            "subsession-report-write-denied",
            f"{verdict.get('reason')}: {resolved}",
        )
    return str(resolved)


def _predecessor_handoff_path(args) -> str:
    """Only a serial successor has a predecessor handoff to read."""
    if (
        getattr(args, "subsession_mode", None) != "serial"
        or (getattr(args, "subsession_index", 0) or 0) < 2
    ):
        return ""
    route_file = getattr(args, "route_file", None)
    if not route_file or not getattr(args, "session_chain_id", None):
        return ""
    route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    import dispatch_subsession_handoff

    return str(dispatch_subsession_handoff.handoff_path(
        Path(route["artifact_root"]), route["route_id"], args.session_chain_id,
    ))


# SD-119 R3: a chain-scoped sub-session's worker contract obligation is that
# `dispatch_subsession_handoff.classify_handoff()` returning anything but
# `"ok"` already keeps this index from ever starting (dispatch_subsession_
# advance.py's chain-advance gate refuses claim/register/start beforehand) --
# so by construction a bound successor session is never mid-edit without a
# handoff that classified `ok` at start time. The worker contract obligation
# layered on top of that server-side gate is procedural, not a second runtime
# check: the successor MUST read that handoff before its first edit
# (`roles/worker-contexts/subsession.md`), since it is this session's only carrier of
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


_sha_text = slice_text_sha256
_sha_files = slice_files_sha256


def bind(args: argparse.Namespace, *, artifact_root: str | Path, action: str) -> None:
    """Validate a complete declaration, derive its ledger, and initialize it."""

    if not declared(args):
        args.state_ledger = ""
        args.fixed_files_sha256 = ""
        args.narrow_verify_sha256 = ""
        args.phase_brief_sha256 = ""
        return
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
        if is_glob_path(path, worktree=worktree):
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
    try:
        fence = _artifact_fence(args, worktree)
    except (OSError, ValueError, KeyError, TypeError, DispatchContractError):
        # Fence derivation is optional. Preserve the pre-existing declared-file
        # behavior if route loading or its scope resolver cannot complete.
        fence = None
    args.effective_fixed_files, args.readonly_input_files = fence or (args.fixed_file, [])
    args.report_file = _resolve_report_output(args)
    if args.report_file:
        args.effective_fixed_files = sorted(set(args.effective_fixed_files) | {args.report_file})
        if fence is not None and args.report_file in fence[0]:
            try:
                route = json.loads(Path(args.route_file).read_text(encoding="utf-8"))
                from worker_bootstrap import qa_primary_items_sidecar
                sidecar = qa_primary_items_sidecar(
                    route, args.route_node, args.report_file)
            except (OSError, TypeError, ValueError):
                sidecar = ""
            if sidecar:
                args.effective_fixed_files = sorted(
                    set(args.effective_fixed_files) | {sidecar})
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
    for file in args.effective_fixed_files:
        command += ["--fixed-file", file]
    for file in args.readonly_input_files:
        command += ["--forbidden-file", file]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise DispatchContractError(
            "subsession-ledger-init-failed", (result.stderr or result.stdout).strip()
        )


def metadata(args: argparse.Namespace) -> str:
    if not getattr(args, "subsession_id", None):
        return ""
    # A parallel row must name the batch that admitted it (`validate_attempt_metadata`).
    # A route-leg group's governor reservation supplies `parallel_group`; a subdivision
    # slice belongs to no route-leg group, so its sealed chain id is the batch.
    batch = (
        f",batch_group={args.session_chain_id}"
        if args.subsession_mode == "parallel" and not getattr(args, "replica_batch_reservation", None)
        else ""
    )
    return batch + (
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
    files = "\n".join(f"  - {file}" for file in getattr(args, "effective_fixed_files", args.fixed_file))
    readonly = getattr(args, "readonly_input_files", [])
    readonly_line = ("- declared worktree inputs are read-only (forbidden files):\n"
                     + "\n".join(f"  - {file}" for file in readonly) + "\n") if readonly else ""
    report_line = (f"- artifact report: {args.report_file} (write the report here; this is separate from worktree fixed_files)\n"
                   if getattr(args, "report_file", "") else "")
    predecessor = _predecessor_handoff_path(args)
    predecessor_line = (f"- read the predecessor chain handoff before editing: {predecessor}\n"
                        if predecessor else "")
    context = (Path(__file__).resolve().parents[1] / "roles/worker-contexts/subsession.md").read_text(encoding="utf-8")
    return (
        context + "\n"
        "Stage sub-session contract:\n"
        f"- sub-session: {args.subsession_id} ({args.subsession_index}/{args.subsession_count}, {args.subsession_mode}, {args.subsession_purpose})\n"
        "- stage_authority: 0; never publish the route-node completion marker\n"
        f"- phase brief: {args.phase_brief}\n"
        f"- persistent compact anchor: {args.state_ledger}\n"
        f"- required narrow verify: {args.narrow_verify}\n"
        "- also run any additional checks named in the phase brief\n"
        f"- final handoff: `artifact: {args.state_ledger}` (this ledger, never a source file you edited)\n"
        f"{report_line}"
        f"{predecessor_line}"
        "- fixed files (exhaustive; out-of-list work stops with a handoff):\n"
        f"{files}\n"
        f"{readonly_line}"
        f"- after at most three edits or one verify round trip, update: python3 {Path(__file__).resolve().parent / 'worker-state-ledger.py'} update --path {args.state_ledger} --attempt-id {args.attempt_id} ...\n"
        "- read the phase brief and assigned ledger; do not reload the full spec unless the brief names it\n\n"
    )
