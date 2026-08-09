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
    parse_registry_metadata,
    process_group_observation,
    process_namespace_identity,
    process_start_ticks,
)


def attempt_metadata(jobs: Path, attempt_id: str) -> dict[str, str]:
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    matches = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") == attempt_id:
            matches.append(metadata)
    return matches[0] if len(matches) == 1 else {}


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
        return 0 if annotated else 65


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
