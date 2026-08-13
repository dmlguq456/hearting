#!/usr/bin/env python3
"""Seal a namespace-portable drain receipt for one detached dispatch attempt."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

from dispatch_contract import (
    ATTEMPT_DESCENDANT_PROOF,
    GROUP_REAP_PROOF,
    annotate_attempt_row,
    attempt_scan_namespace_authority,
    attempt_tagged_descendants,
    close_attempt_row_if,
    parse_registry_metadata,
    process_group_observation,
    process_namespace_identity,
    process_start_ticks,
)
from codex_dispatch_terminal import terminal_envelope_observed


def attempt_record(
    jobs: Path, attempt_id: str
) -> tuple[str, dict[str, str]] | None:
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
            found = (fields[1], metadata)
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
        # A concurrently written result always wins this fallback.
        record = attempt_record(args.jobs, args.attempt_id)
        if (
            record is not None
            and record[0] in {"open", "running"}
            and not terminal_envelope_observed(record[1].get("log_file"))
        ):
            expected = dict(record[1])

            def still_missing_result(fields):
                current = parse_registry_metadata(fields[5])
                return bool(
                    fields[1] in {"open", "running"}
                    and current.get("pid") == expected.get("pid")
                    and current.get("pid_start") == expected.get("pid_start")
                    and current.get("pgid") == expected.get("pgid")
                    and not terminal_envelope_observed(current.get("log_file"))
                )

            close_attempt_row_if(
                args.jobs,
                args.attempt_id,
                "dead-missing-result",
                still_missing_result,
                evidence={
                    "classifier_source": "dispatch-reap-missing-result-v1",
                    "reconcile_reason": "governed-process-group-drained",
                },
            )
        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--pid-start", required=True)
    parser.add_argument("--pgid", type=int, required=True)
    parser.add_argument("--interval", type=float, default=0.2)
    args = parser.parse_args(argv)
    if args.pid <= 0 or args.pgid <= 0 or args.interval <= 0:
        parser.error("--pid, --pgid, and --interval must be positive")
    args.jobs = args.jobs.resolve()
    return watch(args)


if __name__ == "__main__":
    raise SystemExit(main())
