#!/usr/bin/env python3
"""OpenCode dispatch registry harvest/status wrapper."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import (DispatchContractError, close_attempt_row,
                               parse_registry_metadata, reconcile_local_registry,
                               resolve_agent_home as _resolve_agent_home,
                               resolve_dispatch_state_root,
                               validate_attempt_metadata)  # noqa: E402
from dispatch_completion_join import (  # noqa: E402
    materialize_after_terminal_close,
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
    return p


def emit_header(args: argparse.Namespace, jobs: Path, matched: int, marked_done: int, malformed: int) -> None:
    print("adapter=opencode")
    print("runtime_surface=opencode-dispatch-harvest")
    print("status=harvest")
    print(f"job_registry={jobs}")
    print(f"selector_slug={args.slug or '*'}")
    print(f"selector_attempt_id={args.attempt_id or '*'}")
    print(f"selector_worktree={args.worktree or '*'}")
    print(f"status_filter={args.status}")
    print(f"matched={matched}")
    print(f"marked_done={marked_done}")
    print(f"malformed={malformed}")
    print(f"reconciled={getattr(args, 'reconciled', 0)}")
    print("merge_action=unsupported")
    print("cleanup_action=guarded-separate-step")
    print("cleanup_command=adapters/opencode/bin/preflight.sh worktree-cleanup --check --worktree <path>")
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
    return _resolve_agent_home(
        runtime_pointer=Path.home() / ".config" / "opencode" / "hearting"
    )


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
    """Isomorphic to adapters/codex/bin/dispatch-harvest.py (round_1 finding
    5, SD-70/78): derive completion evidence from the row's own terminal
    envelope when no explicit ``--completion`` marker was named. The exact
    route/hash/node/attempt binding is enforced by `ROUTE.complete_node`
    itself against the live registry row and the real route file.
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


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv[1:])
    if args.mark_done and not (args.slug or args.attempt_id or args.worktree):
        print("check=failed")
        print("reason=selector-required")
        print("hint=pass --slug, --attempt-id, or --worktree before --mark-done")
        return 64

    agent_home = resolve_agent_home()
    jobs_override = args.jobs or os.environ.get("AGENT_DISPATCH_JOBS")
    jobs = Path(jobs_override) if jobs_override else resolve_dispatch_state_root(agent_home) / "jobs.log"
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


    emit_header(args, jobs, len(rows), marked_done, malformed)
    for fields in rows:
        _, state, repo, worktree, slug, pipe = fields
        print(f"job_status={state}")
        print(f"job_repo={repo}")
        print(f"job_worktree={worktree}")
        print(f"job_slug={slug}")
        print(f"job_pipe={pipe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
