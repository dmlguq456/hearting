#!/usr/bin/env python3
"""Recompute the per-field harness rows of `capabilities/topologies.json` `field_harness_weights`.

A person runs this when the shipped model table changes and when `checked_on` is three
months old (core/OPERATIONS.md §5.10). It reads one JSON input file, writes the
`weights` block to stdout, and changes nothing else: the person copies the rows into the
registry and updates `checked_on` and `basis` by hand. Nothing here collects measurements,
polls, or blocks on a date.

Input (field → depth → harness → measured values, in the order the file gives them)::

    {"fields": {"<field>": {"strength": 0.5,        # optional; default below
                            "depths": {"<depth>": {"<harness>": [
                                {"kind": "accuracy", "value": 0.61},
                                {"kind": "elo", "value": 1420}, ...]}}}},
     "depth_mix": {"<depth>": 0.36, ...}}           # optional; default below

The i-th value of every harness at one depth is the same measurement, so a measurement
is compared across harnesses only. A value may be `null` (not measured). Which sources
the numbers came from stays in the work record outside the repository, not in this file.

Accepted input (anything else is refused with exit 2, naming the field, depth and harness):
`accuracy` is a finite number >= 0, but a harness whose accuracy is 0 beside a larger one in every
measurement it was compared in for a field is refused (its score would be 0 and the general level
undefined; no small stand-in is made up); `elo` is a finite number;
`strength` is a number from 0 to 1; `depth_mix` values are numbers >= 0, and a harness compared at
depths that all weigh 0 is refused.

One difference from the design's original calculation: a harness with no value to compare at a
depth is left out of that depth here, where the original counted it as 0 there, so a field with
such a gap can come out differently from the original.

The rule ("keep some", core/OPERATIONS.md §5.10):

1. Compare only inside one depth, among the harnesses that have a value for that measurement.
2. Relative score r: accuracy ÷ the best accuracy; elo → expected score against the best entry ÷ 0.5.
3. A harness's field score R at a depth is the mean of its r; across depths it is the mean
   weighted by `depth_mix` over the depths where it was compared; the row's best becomes 1.
4. Remove general level: divide each R by the harness's geometric mean over every field,
   then make the row's best 1 (comparative advantage only).
5. Shrink by evidence strength k: w = 1 − k × min(1 − floor, 1 − T), rounded to 0.05, at least `floor`.
6. A field without data gets no row (its cells are 1.0 in the registry).
"""
from __future__ import annotations

import argparse
import json
import math
import sys

HARNESS_ORDER = ("claude", "codex", "opencode")
FLOOR = 0.6
DEPTH_MIX = {"deep": 0.25, "balanced-deep": 0.24, "balanced": 0.36, "light": 0.13, "mini": 0.02}
STRENGTH = {"code": 1.0, "design": 0.75, "research": 0.5, "lab": 0.5, "plan": 0.5, "docs": 0.25,
            "admin": 0.0, "other": 0.0}
KINDS = ("accuracy", "elo")


class InputError(ValueError):
    pass


def relative(values, kind):
    """`{harness: r}` for one measurement; empty when fewer than two harnesses have a value."""
    have = {harness: value for harness, value in values.items() if value is not None}
    if len(have) < 2:
        return {}
    best = max(have.values())
    if kind == "elo":
        return {harness: (1 / (1 + 10 ** ((best - value) / 400))) / 0.5 for harness, value in have.items()}
    return {harness: (value / best if best > 0 else 1.0) for harness, value in have.items()}


def depth_scores(harnesses):
    """`{harness: R}` at one depth: the mean relative score over the measurements it appears in."""
    width = max((len(items) for items in harnesses.values()), default=0)
    sums, counts = {}, {}
    for position in range(width):
        kinds = {items[position]["kind"] for items in harnesses.values() if position < len(items)}
        if len(kinds) != 1:
            raise InputError(f"measurement {position + 1} gives different kinds across harnesses: {sorted(kinds)}")
        values = {harness: (items[position]["value"] if position < len(items) else None)
                  for harness, items in harnesses.items()}
        for harness, score in relative(values, kinds.pop()).items():
            sums[harness] = sums.get(harness, 0.0) + score
            counts[harness] = counts.get(harness, 0) + 1
    return {harness: sums[harness] / counts[harness] for harness in sums}


def field_scores(depths, mix):
    """`{harness: R}` for a field, best = 1, mixed over the depths where the harness was compared."""
    total, weight, seen = {}, {}, {}
    for depth, harnesses in depths.items():
        if len(harnesses) < 2:
            continue
        try:
            scores = depth_scores(harnesses)
        except InputError as exc:
            raise InputError(f"depth {depth}: {exc}") from None
        for harness, score in scores.items():
            total[harness] = total.get(harness, 0.0) + mix[depth] * score
            weight[harness] = weight.get(harness, 0.0) + mix[depth]
            seen.setdefault(harness, []).append(depth)
    for harness, amount in weight.items():
        if amount <= 0:
            raise InputError(f"{harness}: `depth_mix` is 0 at every depth it was compared ({', '.join(seen[harness])})")
    mixed = {harness: total[harness] / weight[harness] for harness in total}
    best = max(mixed.values(), default=0.0)
    for harness, value in mixed.items():
        if best > 0 and value == 0:
            raise InputError(f"{harness}: accuracy 0 beside a larger one in every measurement it was compared "
                             f"in ({', '.join(seen[harness])}), so its score is 0 and its general level is "
                             "undefined; the rule refuses a 0 and does not stand in a small number")
    return {harness: value / best for harness, value in mixed.items()} if best > 0 else {}


def compute(data):
    """`(weights, detail)`: the rows and the numbers behind them."""
    mix = dict(DEPTH_MIX, **(data.get("depth_mix") or {}))
    scored, strength = {}, {}
    for field, body in (data.get("fields") or {}).items():
        for depth in body.get("depths", {}):
            if depth not in mix:
                raise InputError(f"{field}: unknown depth {depth!r} (known: {', '.join(mix)})")
        try:
            scores = field_scores(body.get("depths", {}), mix)
        except InputError as exc:
            raise InputError(f"{field}: {exc}") from None
        if scores:
            scored[field] = scores
            strength[field] = body["strength"] if body.get("strength") is not None else STRENGTH.get(field)
            if strength[field] is None:
                raise InputError(f"{field}: no default strength; give `strength`")
    general = {}
    for harness in {h for scores in scored.values() for h in scores}:
        values = [scores[harness] for scores in scored.values() if harness in scores]
        general[harness] = math.exp(sum(math.log(value) for value in values) / len(values))
    weights, detail = {}, {"general": general, "fields": {}}
    for field, scores in scored.items():
        tilt = {harness: score / general[harness] for harness, score in scores.items()}
        best = max(tilt.values())
        tilt = {harness: value / best for harness, value in tilt.items()}
        row = {harness: max(FLOOR, round((1 - strength[field] * min(1 - FLOOR, 1 - value)) * 20) / 20)
               for harness, value in tilt.items()}
        order = sorted(row, key=lambda h: (HARNESS_ORDER.index(h) if h in HARNESS_ORDER else len(HARNESS_ORDER), h))
        weights[field] = {harness: row[harness] for harness in order}
        detail["fields"][field] = {"R": scores, "T": tilt, "strength": strength[field]}
    return weights, detail


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate(data):
    if not isinstance(data, dict) or not isinstance(data.get("fields"), dict):
        raise InputError("the input needs a `fields` object")
    mix = data.get("depth_mix")
    if mix is not None:
        if not isinstance(mix, dict):
            raise InputError("`depth_mix` is an object of depth -> number >= 0")
        for depth, value in mix.items():
            if not (_number(value) and value >= 0):
                raise InputError(f"`depth_mix`.{depth}: a number >= 0")
        if sum(dict(DEPTH_MIX, **mix).values()) <= 0:
            raise InputError("`depth_mix` gives every depth the weight 0")
    for field, body in data["fields"].items():
        if not isinstance(body, dict) or not isinstance(body.get("depths"), dict):
            raise InputError(f"{field}: needs a `depths` object")
        if body.get("strength") is not None and not (_number(body["strength"]) and 0 <= body["strength"] <= 1):
            raise InputError(f"{field}: `strength` is a number from 0 to 1")
        for depth, harnesses in body["depths"].items():
            if not isinstance(harnesses, dict):
                raise InputError(f"{field}.{depth}: needs a harness object")
            for harness, items in harnesses.items():
                if not isinstance(items, list):
                    raise InputError(f"{field}.{depth}.{harness}: needs a list of values")
                for item in items:
                    value = item.get("value") if isinstance(item, dict) else None
                    if not isinstance(item, dict) or item.get("kind") not in KINDS or (
                            value is not None and not _number(value)):
                        raise InputError(f"{field}.{depth}.{harness}: each value is "
                                         f"{{\"kind\": accuracy|elo, \"value\": finite number|null}}")
                    if item["kind"] == "accuracy" and value is not None and value < 0:
                        raise InputError(f"{field}.{depth}.{harness}: an accuracy is a number >= 0")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("input", help="JSON input file (see the module docstring)")
    parser.add_argument("--explain", action="store_true", help="print R, T and strength per field to stderr")
    args = parser.parse_args(argv)
    try:
        with open(args.input, encoding="utf-8") as handle:
            data = json.load(handle)
        validate(data)
        weights, detail = compute(data)
    except (OSError, ValueError, ArithmeticError) as exc:
        print(f"field-weights-table: {exc}", file=sys.stderr)
        return 2
    if args.explain:
        for field, numbers in detail["fields"].items():
            print(f"{field} k={numbers['strength']} R={_round(numbers['R'])} T={_round(numbers['T'])}", file=sys.stderr)
        print(f"general level {_round(detail['general'])}", file=sys.stderr)
    print(json.dumps({"weights": weights}, indent=2))
    return 0


def _round(values):
    return {key: round(value, 3) for key, value in values.items()}


if __name__ == "__main__":
    sys.exit(main())
