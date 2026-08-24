#!/usr/bin/env python3
"""Watch one exact owner PID and reconcile its registry row after exit."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
import time

from dispatch_contract import (
    PARENT_EXTINCTION_TERMINAL_STATUSES,
    SUPERVISOR_LEASE_KIND,
    dispatch_state_root,
    observed_supervised_owner_liveness,
    parse_registry_metadata,
    process_namespace_identity,
    process_start_ticks,
    remove_supervisor_lease,
    supervisor_lease_path,
)
from dispatch_completion_join import (
    read_supervisor_phase_state,
    remove_supervisor_state,
)
from dispatch_supervisor_terminal import (
    classify_supervisor_log,
    reconcile_supervisor_terminal,
)


OPEN = {"open", "running"}


def process_start(pid: int) -> str | None:
    return process_start_ticks(pid)


def attempt_record(
    jobs: Path, attempt_id: str
) -> tuple[str | None, dict[str, str]]:
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, {}
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = parse_registry_metadata(fields[5])
        if meta.get("attempt_id") == attempt_id:
            return fields[1], meta
    return None, {}


def attempt_status(jobs: Path, attempt_id: str) -> str | None:
    return attempt_record(jobs, attempt_id)[0]


def _run_registry(operation: str, args) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("dispatch-registry.py")),
        operation,
        "--attempt", args.attempt_id,
        "--jobs", str(args.jobs),
        "--agent-home", str(args.agent_home),
        "--apply",
    ]
    if operation == "orphan-status":
        command.extend(("--pid", str(args.pid), "--pid-start", args.pid_start))
        if args.pid_observer_ns:
            command.extend(("--pid-observer-ns", args.pid_observer_ns))
    return subprocess.run(
        command,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _remove_supervisor_state(args) -> None:
    if re.fullmatch(r"att-[A-Za-z0-9._-]{1,240}", args.attempt_id):
        state_root = dispatch_state_root(args.jobs)
        remove_supervisor_state(
            state_root
            / "supervisor-state"
            / f"{args.attempt_id}.json"
        )
        _status, metadata = attempt_record(args.jobs, args.attempt_id)
        lease = supervisor_lease_path(args.jobs, args.attempt_id)
        if (
            metadata.get("supervisor_lease") == SUPERVISOR_LEASE_KIND
            and metadata.get("supervisor_lease_file") == str(lease)
        ):
            remove_supervisor_lease(lease)


def observed_owner_lifecycle(args):
    """Return the shared exact owner verdict plus its durable phase."""

    status, metadata = attempt_record(args.jobs, args.attempt_id)
    if status is None:
        return None, "", metadata
    state = read_supervisor_phase_state(
        dispatch_state_root(args.jobs)
        / "supervisor-state"
        / f"{args.attempt_id}.json",
        args.attempt_id,
    )
    phase = state.phase if state is not None else ""
    observed = observed_supervised_owner_liveness(
        args.jobs,
        status,
        metadata,
        supervisor_phase=phase,
    )
    return observed, phase, metadata


def reconcile_orphan_cascade(args) -> int:
    result = _run_registry("orphan-status", args)
    _remove_supervisor_state(args)
    return result.returncode


def reconcile_exact_exit(args) -> int:
    """Close any exact dead owner, even when it failed before child launch.

    Preserve orphan semantics first so unfinished children are cascaded. For a
    registered supervisor, classify its terminal envelope next so capacity,
    authentication, and protocol failures retain their typed reason. Finally,
    use the general exact-PID reconciler for legacy rows without a supervisor
    envelope. Every transition remains exact-attempt and conditionally atomic.
    """

    orphan_result = _run_registry("orphan-status", args)
    status, metadata = attempt_record(args.jobs, args.attempt_id)

    has_supervisor_envelope = bool(
        metadata.get("harness") or metadata.get("log_file")
    )
    if status in OPEN and has_supervisor_envelope:
        terminal = classify_supervisor_log(
            metadata.get("log_file"), metadata.get("harness", "unknown")
        )
        try:
            reconcile_supervisor_terminal(args.jobs, args.attempt_id, terminal)
        except Exception:
            _remove_supervisor_state(args)
            return 70
        status = attempt_status(args.jobs, args.attempt_id)

    exact_result = None
    if status in OPEN:
        exact_result = _run_registry("reconcile", args)
        status = attempt_status(args.jobs, args.attempt_id)

    _remove_supervisor_state(args)
    if status not in OPEN:
        return 0
    if exact_result is not None and exact_result.returncode:
        return exact_result.returncode
    return orphan_result.returncode or 70


def watch(args) -> int:
    while True:
        status = attempt_status(args.jobs, args.attempt_id)
        if status not in OPEN:
            # A terminal registry word can precede owner teardown (notably
            # Fleet kill). Keep the supervisor state and wait for the exact
            # recorded owner identity to disappear before cascading.
            if (
                status in PARENT_EXTINCTION_TERMINAL_STATUSES
                and process_start(args.pid) == args.pid_start
            ):
                time.sleep(args.interval)
                continue
            return (
                reconcile_orphan_cascade(args)
                if status in PARENT_EXTINCTION_TERMINAL_STATUSES
                else 0
            )
        if process_start(args.pid) != args.pid_start:
            observed, _phase, _metadata = observed_owner_lifecycle(args)
            if observed is not None and observed.state == "parked-supervised":
                time.sleep(args.interval)
                continue
            break
        time.sleep(args.interval)
    return reconcile_exact_exit(args)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--agent-home", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--pid-start", required=True)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args(argv)
    if args.pid <= 0 or args.interval <= 0:
        parser.error("--pid and --interval must be positive")
    args.jobs = args.jobs.resolve()
    args.agent_home = args.agent_home.resolve()
    # Bind the exact PID/start extinction observed by this watcher to the
    # namespace in which those numbers were meaningful. The registry resolver
    # independently compares this value with both its current namespace and
    # the parent's recorded launch observer before consuming the proof.
    args.pid_observer_ns = process_namespace_identity() or ""
    return watch(args)


if __name__ == "__main__":
    raise SystemExit(main())
