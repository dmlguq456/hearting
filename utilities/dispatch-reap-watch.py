#!/usr/bin/env python3
"""Seal a namespace-portable drain receipt for one detached dispatch attempt."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import time

from dispatch_contract import (
    ATTEMPT_DESCENDANT_PROOF,
    GROUP_REAP_PROOF,
    annotate_attempt_row,
    annotate_attempt_row_if,
    attempt_scan_namespace_authority,
    attempt_tagged_descendants,
    close_attempt_row_if,
    parent_completion_window,
    parse_registry_metadata,
    process_group_observation,
    process_namespace_identity,
    process_start_ticks,
)
from codex_dispatch_terminal import terminal_envelope_observed
from dispatch_completion_join import (
    JoinContractError,
    close_finished_child,
    close_wrapper_pass,
    exact_attempt_row,
)
from dispatch_degradation import record_degradation


def attempt_record(
    jobs: Path, attempt_id: str
) -> tuple[list[str], dict[str, str]] | None:
    found = None
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") == attempt_id:
            found = (fields, metadata)
    return found


def attempt_metadata(jobs: Path, attempt_id: str) -> dict[str, str]:
    record = attempt_record(jobs, attempt_id)
    return record[1] if record is not None else {}


def exact_binding(metadata: dict[str, str], args: argparse.Namespace) -> bool:
    return bool(
        metadata
        and metadata.get("attempt_id") == args.attempt_id
        and metadata.get("pid") == str(args.pid)
        and metadata.get("pid_start") == args.pid_start
        and metadata.get("pgid") == str(args.pgid)
        and args.pgid == args.pid
        and metadata.get("launch_lifecycle") == "detached"
        and metadata.get("pid_observer_ns")
        and metadata.get("pid_ns") == metadata.get("pid_observer_ns")
    )


def record_missing_result_degradation(
    metadata: dict[str, str], args: argparse.Namespace
) -> None:
    """Best-effort SD-93 evidence for one reaper-owned leg close."""

    try:
        record_degradation(
            route_id=metadata.get("route_id") or metadata.get("batch_route_id"),
            route_node=metadata.get("route_node")
            or metadata.get("batch_route_node"),
            route_hash=metadata.get("route_hash"),
            dispatch_depth=2,
            fallback_hop=metadata.get("fallback_hop")
            or metadata.get("batch_fallback_hop"),
            execution_surface="registered-headless",
            writer="dispatch-reap-watch.py",
            kind="leg-failure",
            jobs=args.jobs,
            parallel_group=metadata.get("parallel_group")
            or metadata.get("batch_group"),
            parallel_leg_index=metadata.get("parallel_leg_index")
            or metadata.get("batch_parallel_leg_index"),
            parallel_leg_count=metadata.get("parallel_leg_count")
            or metadata.get("batch_declared_size"),
            attempt_id=metadata.get("attempt_id") or args.attempt_id,
            fallback_ordinal=metadata.get("fallback_ordinal")
            or metadata.get("batch_fallback_ordinal"),
            harness=metadata.get("harness") or metadata.get("batch_harness"),
            leg_class=metadata.get("leg_class"),
            reason="dead-missing-result",
        )
    except BaseException:
        # SD-93d: observability must never change dispatch state or outcome.
        return


def watch(args: argparse.Namespace) -> int:
    metadata = attempt_metadata(args.jobs, args.attempt_id)
    if not exact_binding(metadata, args):
        return 65
    if (
        process_namespace_identity() != metadata.get("pid_observer_ns")
        or not attempt_scan_namespace_authority(metadata)
    ):
        return 69

    while process_start_ticks(args.pid) == args.pid_start:
        time.sleep(args.interval)

    while True:
        metadata = attempt_metadata(args.jobs, args.attempt_id)
        if not exact_binding(metadata, args):
            return 65
        group = process_group_observation(args.pgid)
        descendants = attempt_tagged_descendants(metadata)
        if group.state == "unverifiable" or descendants.state == "unverifiable":
            return 69
        if group.state == "populated" or descendants.state == "populated":
            time.sleep(args.interval)
            continue
        annotated = annotate_attempt_row(
            args.jobs,
            args.attempt_id,
            {
                "launch_outcome": "governed-process-group-drained",
                "group_reap_proof": GROUP_REAP_PROOF,
                "group_reap_pgid": str(args.pgid),
                "attempt_descendant_proof": ATTEMPT_DESCENDANT_PROOF,
                "attempt_descendant_observer_ns": metadata["pid_observer_ns"],
            },
        )
        if not annotated:
            return 65

        # The drain proof is the last moment at which the detached watcher has
        # exact process authority.  If no semantic terminal envelope exists and
        # the row is still open, close the residue as typed missing-result.
        # A concurrently written result always wins this fallback, and F-1
        # additionally defers the close while an exact live parent conductor
        # still owns delivery of `capability-route.py complete` for this row.
        record = attempt_record(args.jobs, args.attempt_id)
        if record is None or record[0][1] not in {"open", "running"}:
            return 0
        if terminal_envelope_observed(record[1].get("log_file")):
            # Detached registered workers cannot write their own marker after
            # exit.  This wrapper-launched watcher owns the durable drain
            # receipt, so it also performs the exact evidence-backed closure
            # before any supervisor may resume the parent.
            if record[1].get("route_file") and record[1].get("route_node"):
                try:
                    row = exact_attempt_row(args.jobs, args.attempt_id)
                    reason = close_finished_child(row, jobs=args.jobs)
                    if reason.startswith("completion-"):
                        close_wrapper_pass(row, jobs=args.jobs)
                except JoinContractError:
                    return 65
            return 0

        fields, metadata = record
        expected = dict(metadata)
        window = parent_completion_window(args.jobs, fields, metadata)
        if window.deferred:
            annotate_attempt_row_if(
                args.jobs,
                args.attempt_id,
                {
                    "reap_close_deferred": window.source,
                    "reap_close_deferred_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
                lambda current_fields: exact_binding(
                    parse_registry_metadata(current_fields[5]), args
                ),
            )
            while True:
                time.sleep(args.parent_recheck_interval)
                record = attempt_record(args.jobs, args.attempt_id)
                if record is None:
                    return 65
                fields, metadata = record
                if not exact_binding(metadata, args):
                    return 65
                if fields[1] not in {"open", "running"}:
                    return 0  # the conductor's complete won the race
                if terminal_envelope_observed(metadata.get("log_file")):
                    return 0  # semantic terminal evidence takes precedence
                if not parent_completion_window(args.jobs, fields, metadata).deferred:
                    break

        def still_missing_result(current_fields):
            current = parse_registry_metadata(current_fields[5])
            return bool(
                current_fields[1] in {"open", "running"}
                and current.get("pid") == expected.get("pid")
                and current.get("pid_start") == expected.get("pid_start")
                and current.get("pgid") == expected.get("pgid")
                and not terminal_envelope_observed(current.get("log_file"))
                and not parent_completion_window(
                    args.jobs, current_fields, current
                ).deferred
            )

        closed = close_attempt_row_if(
            args.jobs,
            args.attempt_id,
            "dead-missing-result",
            still_missing_result,
            evidence={
                "classifier_source": "dispatch-reap-missing-result-v1",
                "reconcile_reason": "governed-process-group-drained",
            },
        )
        if closed:
            record_missing_result_degradation(metadata, args)
        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--pid-start", required=True)
    parser.add_argument("--pgid", type=int, required=True)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--parent-recheck-interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.pid <= 0 or args.pgid <= 0 or args.interval <= 0:
        parser.error("--pid, --pgid, and --interval must be positive")
    if args.parent_recheck_interval <= 0:
        parser.error("--parent-recheck-interval must be positive")
    args.jobs = args.jobs.resolve()
    return watch(args)


if __name__ == "__main__":
    raise SystemExit(main())
