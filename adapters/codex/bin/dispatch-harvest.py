#!/usr/bin/env python3
"""Codex dispatch registry harvest/status wrapper."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    annotate_attempt_row,
    close_attempt_row,
    parse_registry_metadata,
    reconcile_local_registry,
    resolve_agent_home as _resolve_agent_home,
    resolve_dispatch_state_root,
    validate_attempt_metadata,
)
from codex_dispatch_terminal import inspect_terminal_attempt  # noqa: E402
from dispatch_completion_join import (  # noqa: E402
    consume_supervisor_outbox_attempts,
    consume_parent_session_attempt,
    JoinContractError,
    materialize_after_terminal_close,
    parent_session_state_path,
    read_supervisor_phase_state,
    required_action_for_attempt,
    route_completion_evidence,
)
_route_spec = importlib.util.spec_from_file_location(
    "capability_route", ROOT / "utilities" / "capability-route.py"
)
ROUTE = importlib.util.module_from_spec(_route_spec)
_route_spec.loader.exec_module(ROUTE)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--jobs")
    p.add_argument("--reconcile-local", help="legacy cycle-local registry to reconcile first")
    p.add_argument("--slug")
    p.add_argument("--attempt-id")
    p.add_argument("--worktree")
    p.add_argument("--status", choices=("open", "done", "all"), default="open")
    p.add_argument("--mark-done", action="store_true")
    p.add_argument("--completion", help="hash-bound completion marker for routed rows")
    p.add_argument("--keep-home", action="store_true")
    p.add_argument("--failure-detail", action="store_true")
    return p


def emit_header(args: argparse.Namespace, jobs: Path, matched: int, marked_done: int, malformed: int) -> None:
    print("adapter=codex")
    print("runtime_surface=codex-dispatch-harvest")
    print("status=harvest")
    print(f"job_registry={jobs}")
    print(f"registry_lock={jobs}.lock")
    print(f"selector_slug={args.slug or '*'}")
    print(f"selector_attempt_id={args.attempt_id or '*'}")
    print(f"selector_worktree={args.worktree or '*'}")
    print(f"status_filter={args.status}")
    print(f"matched={matched}")
    print(f"marked_done={marked_done}")
    print(f"malformed={malformed}")
    print(f"reconciled={getattr(args, 'reconciled', 0)}")
    print(f"failure_detail={int(args.failure_detail)}")
    print("merge_action=unsupported")
    print("cleanup_action=guarded-separate-step")
    print("cleanup_command=adapters/codex/bin/preflight.sh worktree-cleanup --check --worktree <path>")
    print("note=registry-only; merge remains main/orchestrator; apply cleanup only after merge, integrated verification, and push")


def matches(args: argparse.Namespace, fields: list[str]) -> bool:
    if len(fields) != 6:
        return False
    _, state, _, worktree, slug, _ = fields
    if args.status != "all" and state != args.status:
        return False
    if args.slug and slug != args.slug:
        return False
    if args.attempt_id:
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") != args.attempt_id:
            return False
    if args.worktree and worktree != args.worktree:
        return False
    return True


def resolve_agent_home() -> Path:
    return _resolve_agent_home(runtime_pointer=Path.home() / ".codex" / "hearting")


def _complete_exact_routed_attempt(jobs: Path, metadata: dict[str, str], completion: Path) -> None:
    marker = json.loads(completion.read_text(encoding="utf-8"))
    registered = str(metadata.get("registered_worker", "")).lower() in {"1", "true"}
    expected = {
        "schema_version": 2,
        "route_id": metadata.get("route_id"),
        "route_hash": metadata.get("route_hash"),
        "node_id": metadata.get("route_node"),
        "attempt_id": metadata.get("attempt_id"),
        "dispatch_depth": int(metadata["dispatch_depth"]),
        "transport": metadata.get("transport"),
        "execution_surface": metadata.get("execution_surface"),
        "registered_worker": registered,
        "fallback_hop": metadata.get("fallback_hop") or None,
    }
    if any(marker.get(key) != value for key, value in expected.items()):
        raise ValueError("stale-route-completion")
    route_file = Path(metadata.get("route_file", ""))
    if not route_file.is_file():
        raise ValueError("route-record-unreadable")
    route = ROUTE.verify_route(json.loads(route_file.read_text(encoding="utf-8")))
    node = next(
        (row for row in route["nodes"] if row.get("id") == metadata["route_node"]),
        None,
    )
    if node is None:
        raise ValueError("route-node-unknown")
    evidence = Path(str(marker.get("evidence", {}).get("path", "")))
    # A deliverable is one file or one directory of them; this was the last gate
    # that still said "file", so a directory-artifact marker re-driven here was
    # refused. `complete_node` re-validates the shape anyway.
    if not evidence.is_absolute() or not (evidence.is_file() or evidence.is_dir()):
        raise ValueError("completion-evidence-missing")
    ROUTE.complete_node(
        route,
        node,
        metadata["route_node"],
        evidence,
        jobs=jobs,
        attempt_id=metadata["attempt_id"],
    )


def _complete_routed_attempt_from_terminal_evidence(
    jobs: Path, metadata: dict[str, str], worktree: str
) -> None:
    """Derive completion evidence from the row's own terminal envelope when
    no explicit ``--completion`` marker was named (SD-70/78, round_1 finding
    5: `preflight.sh harvest --attempt-id ... --mark-done` is the only
    admitted delivered-phase command, and it was unsatisfiable for a
    route-bound row because no production caller ever supplied
    ``--completion``).

    Uses the identical shared predicate `route_completion_evidence` the
    supervisor path already uses (valid envelope, PASS verdict, readable
    in-root artifact) and then the same `ROUTE.complete_node` entry point
    `_complete_exact_routed_attempt` calls. The exact route/hash/node/attempt
    binding is enforced by `complete_node` itself, which re-reads the live
    registry row and the real route file rather than trusting this call's
    arguments — a wrong route file, wrong route hash, wrong node id, or
    wrong attempt id in the metadata this function was given is caught there,
    not here.
    """

    artifact, _evidence_reason = route_completion_evidence(metadata, worktree=worktree)
    if artifact is None:
        raise ValueError("route-completion-required")
    route_file = Path(metadata.get("route_file", ""))
    if not route_file.is_file():
        raise ValueError("route-record-unreadable")
    route = ROUTE.verify_route(json.loads(route_file.read_text(encoding="utf-8")))
    node = next(
        (row for row in route["nodes"] if row.get("id") == metadata["route_node"]),
        None,
    )
    if node is None:
        raise ValueError("route-node-unknown")
    ROUTE.complete_node(
        route,
        node,
        metadata["route_node"],
        Path(artifact),
        jobs=jobs,
        attempt_id=metadata["attempt_id"],
    )


def mark_native_stop_harvest(jobs: Path, attempt_id: str) -> bool:
    try:
        return annotate_attempt_row(
            jobs,
            attempt_id,
            {"parent_completion_harvested": "1"},
        )
    except (DispatchContractError, OSError):
        return False


def default_runtime_jobs(environ: dict[str, str] | os._Environ[str]) -> Path:
    """Return the Codex runtime registry, never the packaged source root."""

    codex_home = Path(environ.get("CODEX_HOME") or Path.home() / ".codex")
    return codex_home.expanduser() / ".harness" / "dispatch" / "jobs.log"


def consume_supervised_harvest(
    args: argparse.Namespace,
    *,
    matched: int,
    marked_done: int,
) -> bool:
    """Acknowledge one outbox action only after its harvest succeeded."""

    state_file = os.environ.get("AGENT_DISPATCH_COMPLETION_STATE_FILE", "")
    parent_attempt = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
    if not state_file or not parent_attempt or not args.attempt_id:
        return True
    state = read_supervisor_phase_state(Path(state_file), parent_attempt)
    if (
        state is None
        or state.outbox is None
        or args.attempt_id not in state.outbox.attempt_ids
        or args.attempt_id in state.outbox.consumed_attempt_ids
    ):
        return True
    succeeded = (args.mark_done and marked_done == 1) or (
        args.failure_detail and matched == 1
    )
    if not succeeded:
        return False
    try:
        return consume_supervisor_outbox_attempts(
            Path(state_file), parent_attempt, {args.attempt_id}
        )
    except JoinContractError:
        return False


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv[1:])
    if args.mark_done and not (args.slug or args.attempt_id or args.worktree):
        print("check=failed")
        print("reason=selector-required")
        print("hint=pass --slug or --worktree before --mark-done")
        return 64

    agent_home = resolve_agent_home()
    jobs_override = args.jobs or os.environ.get("AGENT_DISPATCH_JOBS")
    jobs = Path(jobs_override) if jobs_override else default_runtime_jobs(os.environ)
    args.reconciled = 0
    if args.reconcile_local:
        try:
            args.reconciled, _ = reconcile_local_registry(
                jobs.resolve(), Path(args.reconcile_local).resolve()
            )
        except DispatchContractError as exc:
            print(f"check=failed\nreason={exc.reason}\ndetail={exc.detail}")
            return 73
    if not jobs.exists():
        emit_header(args, jobs, 0, 0, 0)
        return 0

    rows = []
    malformed = 0
    for line in jobs.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            malformed += 1
        elif matches(args, fields):
            rows.append(fields)

    terminal_results: dict[str, dict[str, object]] = {}
    registry_failure_details: dict[str, dict[str, str]] = {}
    for fields in rows:
        metadata = parse_registry_metadata(fields[5])
        attempt_id = metadata.get("attempt_id", f"row-{len(terminal_results)}")
        if metadata.get("harness") not in (None, "", "codex", "claude"):
            continue
        try:
            required_action = required_action_for_attempt(fields[1], metadata)
        except JoinContractError as exc:
            print("check=failed")
            print(f"reason={exc}")
            return 64
        if args.failure_detail and required_action != "inspect-done-failure":
            print("check=failed")
            print("reason=failure-detail-requires-terminal-failure")
            return 64
        result = inspect_terminal_attempt(
            metadata.get("log_file"),
            worktree=fields[3],
            artifact_root_metadata=metadata.get("artifact_root"),
            include_failure_detail=args.failure_detail,
        )
        terminal_results[attempt_id] = result
        if args.failure_detail and not (
            result.get("state") == "valid"
            and result.get("verdict") in {"FAIL", "BLOCKED"}
        ):
            registry_failure_details[attempt_id] = {
                "failure_class": metadata.get("failure_class") or "unknown",
                "note": metadata.get("note") or "unknown",
                "reconcile_reason": metadata.get("reconcile_reason") or "unknown",
            }

    marked_done = 0
    if args.mark_done:
        live = [fields for fields in rows if fields[1] in {"open", "running"}]
        if len(live) > 1:
            print("check=failed\nreason=ambiguous-selector")
            print(f"matched_live={len(live)}")
            return 64
        if live:
            target = live[0]
            metadata = parse_registry_metadata(target[5])
            try:
                validate_attempt_metadata(metadata)
            except DispatchContractError as exc:
                print(f"check=failed\nreason={exc.reason}\ndetail={exc.detail}")
                return 65
            attempt_id = metadata.get("attempt_id")
            if not attempt_id:
                print("check=failed\nreason=attempt-id-required")
                return 65
            try:
                if metadata.get("route_id"):
                    if args.completion:
                        if not Path(args.completion).is_file():
                            raise ValueError("route-completion-required")
                        _complete_exact_routed_attempt(jobs, metadata, Path(args.completion))
                    else:
                        _complete_routed_attempt_from_terminal_evidence(
                            jobs, metadata, target[3]
                        )
                elif close_attempt_row(jobs, attempt_id, "harvest-complete"):
                    materialize_after_terminal_close(jobs, attempt_id)
                else:
                    raise ValueError("attempt-row-not-open")
            except (KeyError, OSError, TypeError, ValueError) as exc:
                print(f"check=failed\nreason={exc}")
                return 65
            marked_done = 1

        if live and not args.keep_home:
            profile_name = metadata.get("profile")
            if profile_name:
                home = resolve_dispatch_state_root(resolve_agent_home()) / "homes" / (
                    f"{target[4]}.{profile_name}"
                )
                if home.exists():
                    shutil.rmtree(home, ignore_errors=True)


    if not consume_supervised_harvest(
        args, matched=len(rows), marked_done=marked_done
    ):
        print("check=failed")
        print("reason=supervisor-outbox-consume-failed")
        return 70

    emit_header(args, jobs, len(rows), marked_done, malformed)
    for fields in rows:
        _, state, repo, worktree, slug, pipe = fields
        metadata = parse_registry_metadata(pipe)
        print(f"job_status={state}")
        print(f"job_repo={repo}")
        print(f"job_worktree={worktree}")
        print(f"job_slug={slug}")
        print(f"job_pipe={pipe}")
        terminal = terminal_results.get(metadata.get("attempt_id", ""))
        if terminal is not None:
            print(f"handoff_state={terminal['state']}")
            print(f"handoff_source={terminal['source']}")
            print(f"terminal_verdict={terminal['verdict']}")
            print(f"artifact_state={terminal['artifact_state']}")
            print(f"artifact_readable={1 if terminal['artifact_state'] == 'readable' else 0}")
            print(f"artifact_path_b64={terminal.get('artifact_path_b64', '-')}")
            print(f"blocker_reason={terminal['blocker_reason']}")
            for key in (
                "blocker_detail_excerpt",
                "blocker_detail_truncated",
                "failure_diagnostic_excerpt",
                "failure_diagnostic_truncated",
            ):
                if key in terminal:
                    print(f"{key}={terminal[key]}")
            registry_failure = registry_failure_details.get(metadata.get("attempt_id", ""))
            if registry_failure is not None:
                print("failure_source=registry-terminal")
                print(f"registry_failure_class={registry_failure['failure_class']}")
                print(f"registry_failure_note={registry_failure['note']}")
                print(f"registry_reconcile_reason={registry_failure['reconcile_reason']}")

    native_rows = []
    for fields in rows:
        metadata = parse_registry_metadata(fields[5])
        if (
            metadata.get("parent_completion_delivery") == "codex-stop-hook"
            and metadata.get("attempt_id") == args.attempt_id
            and metadata.get("parent_sid")
        ):
            native_rows.append((fields, metadata))
    if len(native_rows) == 1 and args.status == "all":
        native_fields, metadata = native_rows[0]
        # Stop delivery means the exact batch reached a typed terminal or
        # recovery boundary, not that the worker produced a valid PASS/FAIL
        # envelope. Exact --status all harvest must consume runtime-error and
        # malformed/absent handoffs too, after reporting their bounded state.
        try:
            consumed = consume_parent_session_attempt(
                parent_session_state_path(
                    jobs.resolve(), metadata["parent_sid"]
                ),
                metadata["parent_sid"],
                metadata["attempt_id"],
                before_consume=lambda: mark_native_stop_harvest(
                    jobs, metadata["attempt_id"]
                ),
                # SD-91 migration: the old Stop bridge may have crashed before
                # publishing delivered state. Exact done-row harvest is the
                # only path allowed to consume that pending receipt.
                allow_pending=native_fields[1] == "done",
            )
        except JoinContractError as exc:
            print("check=failed")
            print(f"reason={exc}")
            return 70
        if not consumed:
            print("check=failed")
            print("reason=native-stop-receipt-not-delivered")
            return 70
        print("parent_completion_receipt=consumed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
