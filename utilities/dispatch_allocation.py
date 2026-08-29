#!/usr/bin/env python3
"""Read-only rolling attempt counters and deterministic harness ranking."""

from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path


HARNESSES = ("claude", "codex", "opencode")
STRATEGY = "least-recent-attempts"
BALANCED_STRATEGY = "balanced"

# Which optional `allocation.*` keys each strategy actually reads. A key that
# is present in a user config but absent here is *inert*: the file validates,
# the route seals it, and nothing changes at dispatch time. That silent shape
# is exactly the 2026-08-13 -> 2026-08-29 drift (balanced-first decided, local
# file left on capacity-aware, depth-affinity keys appended on top), so the
# table is the single source for validator warnings, install drift reports,
# and the per-attempt allocation receipt.
_STRATEGY_READS = {
    "least-recent-attempts": frozenset(),
    "capacity-aware": frozenset({"depth_affinity", "depth_affinity_weight"}),
    "balanced": frozenset({
        "usage_gate_used_percent", "depth_affinity",
        "depth_affinity_weight", "usage_headroom_exponent",
    }),
}
_OPTIONAL_ALLOCATION_KEYS = (
    "usage_gate_used_percent", "depth_affinity",
    "depth_affinity_weight", "usage_headroom_exponent",
)
_DEGRADED_UNDER = {
    # capacity-aware only hoists the preferred harness inside a headroom
    # margin of (weight-0.5)*200 points; it never changes a target share.
    ("capacity-aware", "depth_affinity_weight"): "headroom-margin tie-break only (no share weighting)",
}


def inert_allocation_keys(allocation) -> dict[str, str]:
    """Map each explicitly configured allocation key to why it has no effect.

    Returns an empty mapping when every present key is read by the configured
    strategy. Values are short reasons: ``ignored under <strategy>`` for keys
    the strategy never reads, or the degraded-semantics note for keys it only
    partially honors. Unknown strategies report nothing (the validator owns
    that error).
    """
    if not isinstance(allocation, dict):
        return {}
    strategy = allocation.get("strategy")
    reads = _STRATEGY_READS.get(strategy)
    if reads is None:
        return {}
    inert: dict[str, str] = {}
    for key in _OPTIONAL_ALLOCATION_KEYS:
        if key not in allocation:
            continue
        if key not in reads:
            inert[key] = f"ignored under {strategy}"
            continue
        note = _DEGRADED_UNDER.get((strategy, key))
        if note:
            inert[key] = note
    return inert


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
