#!/usr/bin/env python3
"""Which harness state a dispatched launch writes, decided once for every harness.

Besides its own scope, a registered attempt writes harness state: progress
files, the read-guard markers its portable hooks record, the spec-read marker,
and -- when it closes its own registry row -- the dispatch state root. A
committing linked-worktree run also writes Git metadata (`commit_policy`).

Whether a launch needs each of these is a rule about the launch, not about a
harness. It lived in the Codex wrapper because Codex is the adapter whose OS
sandbox must open these directories (audit §4 #8, A7). An adapter whose
declared access enforcement confines writes (`harness_capabilities` `access`)
projects the answer into its sandbox; one that does not confine a worker's
shell writes has nothing to open.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

from dispatch_contract import dispatch_state_root, state_root


def spec_grounding_dir(args) -> Path:
    """The release-independent spec-read marker directory shared by every adapter."""
    return state_root(os.environ) / "spec-grounding"


def core_grounding_dir(args) -> Path:
    return Path(args.agent_home) / ".core-grounding"


def spec_read_marker_required(args) -> bool:
    """The portable worker kernel requires a witnessed governing-PRD read before
    spec-backed output, including workers without a route binding. Registration
    carries that obligation; a review persona or network grant does not."""
    return bool(
        getattr(args, "route_id", None)
        or getattr(args, "nested_headless_network", False)
        or (
            getattr(args, "execution_surface", None) == "registered-headless"
            and getattr(args, "registered_worker", 0) == 1
        )
    )


def route_bound_worker_writable_dirs(args) -> tuple[Path, ...]:
    """The portable read-guard state directory, for a route-bound launch or an
    owner that dispatches children from inside its own sandbox."""
    if not (getattr(args, "route_id", None) or getattr(args, "nested_headless_network", False)):
        return ()
    paths = [path.resolve() for path in (core_grounding_dir(args),) if path.is_dir()]
    if getattr(args, "nested_headless_network", False):
        # Owners that publish continuations also append their parent session's
        # route chain. Use the ledger's own path resolver, including overrides.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from fleet.route_chain import state_root
        paths.append(Path(state_root()).resolve())
    return tuple(paths)


def progress_writable_dirs(args) -> tuple[Path, ...]:
    """The two dispatch-state subdirectories an attempt writes progress into.

    Only `heartbeats/` and `watchdog/`, not the state root: recording progress
    does not need the registry, logs, or supervisor state.
    """
    if not getattr(args, "jobs_path", None):
        return ()
    root = dispatch_state_root(args.jobs_path)
    return (root / "heartbeats", root / "watchdog")


def registry_writable_launch(args) -> bool:
    """Whether the launch needs the whole dispatch state root (registry,
    `jobs.log`, `completion/<route_id>`): any route-bound attempt that closes
    its own row, at any depth, or an owner that registers its children."""
    return bool(
        getattr(args, "nested_headless_network", False)
        or (getattr(args, "route_id", None) and getattr(args, "command_attempt_id", None))
    )
