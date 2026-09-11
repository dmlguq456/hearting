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
    ATTEMPT_DESCENDANT_RESIDUE_PROOF,
    GROUP_REAP_PROOF,
    DispatchContractError,
    annotate_attempt_row,
    launched_attempt_identity,
    annotate_attempt_row_if,
    attempt_scan_namespace_authority,
    attempt_process_quiescence,
    attempt_tagged_descendants,
    close_attempt_row_if,
    parent_completion_window,
    parse_registry_metadata,
    process_group_observation,
    process_namespace_identity,
    process_start_ticks,
    foreground_review_eligible,
    _foreground_outcome_values_from_pipe,
)
from codex_dispatch_terminal import terminal_envelope_observed
from dispatch_completion_join import (
    JoinContractError,
    _route_free_review_row,
    apply_exact_route_free_review_classification,
    classify_exact_route_free_review_outcome,
    close_finished_child,
    close_wrapper_pass,
    exact_attempt_row,
    materialize_after_terminal_close,
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


def exact_binding(
    metadata: dict[str, str], args: argparse.Namespace, raw_pipe: str | None = None
) -> bool:
    detached = bool(
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
    if detached:
        return True
    if not foreground_review_eligible(
        metadata,
        expected_attempt_id=args.attempt_id,
        expected_pid=args.pid,
        expected_pid_start=args.pid_start,
        expected_pgid=args.pgid,
    ):
        return False
    try:
        return _foreground_outcome_values_from_pipe(
            raw_pipe if raw_pipe is not None else ",".join(
                f"{key}={value}" for key, value in metadata.items()
            )
        ) is not None
    except DispatchContractError:
        return False


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


def residue_terminal_basis(fields: list[str], metadata: dict[str, str]) -> str:
    """Name the semantic terminal evidence that makes tagged survivors residue.

    A terminal registry status (someone already closed the row with evidence)
    or the worker's own final runtime envelope both mean the worker declared
    itself finished; anything still carrying its tag afterwards is a leftover.
    Without either, survivors keep their veto -- the worker may still be
    writing its output. Sealing additionally requires the governed process
    group to be empty or to hold only tagged survivors: an untagged live
    member of the governed group is not residue and keeps the drain waiting
    (at the backed-off interval).
    """

    if fields[1] not in {"open", "running"}:
        return "registry-terminal"
    if terminal_envelope_observed(metadata.get("log_file")):
        return "terminal-envelope"
    return ""


def watch(args: argparse.Namespace) -> int:
    initial = attempt_record(args.jobs, args.attempt_id)
    metadata = initial[1] if initial is not None else {}
    if not exact_binding(metadata, args, initial[0][5] if initial else None):
        return 65
    if (
        process_namespace_identity() != metadata.get("pid_observer_ns")
        or not attempt_scan_namespace_authority(metadata)
    ):
        return 69

    while process_start_ticks(args.pid) == args.pid_start:
        time.sleep(args.interval)

    # SD-OPEN-47 (H7-a): the post-exit drain loop used to rescan every
    # `/proc/<pid>/environ` at the launch interval (0.2 s) for as long as any
    # tagged process lived, which for a worker-spawned background shell meant
    # forever at ~25% CPU. The scan interval now backs off, and tagged residue
    # that outlives the grace after the attempt already holds semantic
    # terminal evidence is sealed as a typed residue receipt instead of being
    # waited on without bound.
    drain_started = time.monotonic()
    drain_interval = args.interval
    descendant_proof: dict[str, str] = {
        "attempt_descendant_proof": ATTEMPT_DESCENDANT_PROOF,
    }
    while True:
        record = attempt_record(args.jobs, args.attempt_id)
        if record is None:
            return 65
        fields, metadata = record
        if not exact_binding(metadata, args, fields[5]):
            return 65
        group = process_group_observation(args.pgid)
        descendants = attempt_tagged_descendants(metadata)
        if group.state == "unverifiable" or descendants.state == "unverifiable":
            return 69
        if group.state == "empty" and descendants.state == "empty":
            break
        tagged_pids = {pid for pid, _start, _state in descendants.members}
        group_live = {pid for pid, _start, state in group.members if state != "Z"}
        # Residue may also sit inside the governed group (`nohup cmd &` without
        # setsid); it is residue all the same when every live group member
        # carries the tag (review finding 11).
        group_is_residue = group.state == "empty" or (
            group.state == "populated" and group_live <= tagged_pids
        )
        if group_is_residue and descendants.state == "populated":
            basis = residue_terminal_basis(fields, metadata)
            if basis and time.monotonic() - drain_started >= args.residue_grace:
                members = list(descendants.members)
                descendant_proof = {
                    "attempt_descendant_proof": ATTEMPT_DESCENDANT_RESIDUE_PROOF,
                    # Bounded: the row is re-read by every registry parser
                    # (review finding 10); the count carries the rest.
                    "attempt_descendant_residue": ";".join(
                        f"{pid}:{start}" for pid, start, _state in members[:16]
                    ),
                    "attempt_descendant_residue_count": str(len(members)),
                    "attempt_descendant_residue_basis": basis,
                    "attempt_descendant_residue_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
                break
        time.sleep(drain_interval)
        drain_interval = min(args.drain_interval_max, drain_interval * 2)

    annotated = annotate_attempt_row(
        args.jobs,
        args.attempt_id,
        {
            "launch_outcome": "governed-process-group-drained",
            "group_reap_proof": GROUP_REAP_PROOF,
            "group_reap_pgid": str(args.pgid),
            **descendant_proof,
            "attempt_descendant_observer_ns": metadata["pid_observer_ns"],
        },
    )
    if not annotated:
        return 65
    row = exact_attempt_row(args.jobs, args.attempt_id)
    if _route_free_review_row(row):
        try:
            quiescence = attempt_process_quiescence(row.metadata)
            classification = classify_exact_route_free_review_outcome(
                row,
                jobs=args.jobs,
                expected_attempt_id=args.attempt_id,
                expected_pid=args.pid,
                expected_pid_start=args.pid_start,
                expected_pgid=args.pgid,
                quiescence=quiescence,
            )
            failure = apply_exact_route_free_review_classification(
                row, jobs=args.jobs, classification=classification
            )
            # A successful process drain is not a committed terminal row.
            # Re-read even after success, allowing an exact concurrent close
            # but never treating a CAS or I/O refusal as watcher completion.
            current = exact_attempt_row(args.jobs, args.attempt_id)
            if (current.status not in {"done", "killed", "cancelled"}
                    or launched_attempt_identity(current.raw.split("\t"))
                    != launched_attempt_identity(row.raw.split("\t"))):
                print("review-completion-apply-failed: " + (failure or "terminal-row-unverified"), file=sys.stderr)
                return 65
            if failure:
                materialize_after_terminal_close(args.jobs, args.attempt_id)
        except (DispatchContractError, JoinContractError, OSError):
            return 65
        return 0
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
            if not exact_binding(metadata, args, fields[5]):
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
        materialize_after_terminal_close(args.jobs, args.attempt_id)
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
    parser.add_argument(
        "--drain-interval-max",
        type=float,
        default=2.0,
        help="ceiling for the post-exit drain rescan backoff (seconds)",
    )
    parser.add_argument(
        "--residue-grace",
        type=float,
        default=30.0,
        help=(
            "seconds after leader exit before tagged survivors of an attempt "
            "that already holds terminal evidence are sealed as residue"
        ),
    )
    args = parser.parse_args(argv)
    if args.pid <= 0 or args.pgid <= 0 or args.interval <= 0:
        parser.error("--pid, --pgid, and --interval must be positive")
    if args.parent_recheck_interval <= 0:
        parser.error("--parent-recheck-interval must be positive")
    if args.drain_interval_max < args.interval or args.residue_grace < 0:
        parser.error(
            "--drain-interval-max must be >= --interval and --residue-grace >= 0"
        )
    args.jobs = args.jobs.resolve()
    return watch(args)


if __name__ == "__main__":
    raise SystemExit(main())
