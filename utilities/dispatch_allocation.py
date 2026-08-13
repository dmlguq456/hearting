#!/usr/bin/env python3
"""Read-only rolling attempt counters and deterministic harness ranking."""

from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path


HARNESSES = ("claude", "codex", "opencode")
STRATEGY = "least-recent-attempts"
BALANCED_STRATEGY = "balanced"


def _metadata(value: str) -> dict[str, str]:
    return {
        key: item
        for part in value.split(",")
        if "=" in part
        for key, item in (part.split("=", 1),)
    }


def attempt_counts(jobs: str | Path, *, window: int) -> dict[str, int]:
    if not isinstance(window, int) or window <= 0:
        raise ValueError("allocation window must be a positive integer")
    counts = {harness: 0 for harness in HARNESSES}
    path = Path(jobs)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return counts
    recent: deque[str] = deque(maxlen=window)
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = _metadata(fields[5])
        harness = metadata.get("harness", "")
        if (
            harness not in counts
            or not metadata.get("attempt_id")
            or metadata.get("attempt_schema_version") != "2"
            or metadata.get("registered_worker") not in {"1", "true"}
        ):
            continue
        recent.append(harness)
    for harness in recent:
        counts[harness] += 1
    return counts


def rank_harnesses(
    candidates: list[str] | tuple[str, ...],
    counts: dict[str, int],
    *,
    declared_order: list[str] | tuple[str, ...] = HARNESSES,
) -> list[str]:
    unique = list(dict.fromkeys(candidates))
    if any(harness not in HARNESSES for harness in unique):
        raise ValueError("unknown allocation candidate")
    order = {harness: index for index, harness in enumerate(declared_order)}
    tail = len(order)
    return sorted(
        unique,
        key=lambda harness: (
            int(counts.get(harness, 0)),
            order.get(harness, tail + HARNESSES.index(harness)),
        ),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("counts", "rank"))
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--window", type=int, default=30)
    parser.add_argument("--candidates", default=",".join(HARNESSES))
    parser.add_argument("--declared-order", default=",".join(HARNESSES))
    args = parser.parse_args(argv)
    candidates = [item for item in args.candidates.split(",") if item]
    declared = [item for item in args.declared_order.split(",") if item]
    try:
        counts = attempt_counts(args.jobs, window=args.window)
        ranked = rank_harnesses(candidates, counts, declared_order=declared)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"allocation_strategy={STRATEGY}")
    print(f"allocation_window={args.window}")
    for harness in HARNESSES:
        print(f"attempt_count.{harness}={counts[harness]}")
    if args.operation == "rank":
        print("rank=" + ",".join(ranked))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
