#!/usr/bin/env python3
"""Tier-derived review-round cap leaf (CONVENTIONS §1.1 retry budget).

Moved out of `dispatch-node.py` so `capability-route.py` (SD-116 WP4) can
reuse the same derivation for its sealed `continuation_budget.review_round_cap`
without creating an import cycle: `dispatch-node.py` already loads
`capability-route.py`, so `capability-route.py` importing `dispatch-node.py`
back would cycle. Both now import this leaf instead (route_identity.py /
dispatch_launch_tuple.py precedent -- one definition, no duplication)."""

from __future__ import annotations


def max_review_rounds(effective_intensity):
    """Tier-derived max round count for a capped anchor.

    `direct`/`quick` run no automatic correction round (max 1: the first pass
    only, matching the table's "One pass"/"None automatically"). `standard`/`strong`
    get one correction (max 2). `thorough`/`adversarial` get two, including the
    adversary pass that is not itself a correction (max 3). This is deliberately a
    per-tier derivation, not a hardcoded `cap=2` -- a tier change moves the cap with it.
    """
    if effective_intensity in ("direct", "quick"):
        return 1
    if effective_intensity in ("standard", "strong"):
        return 2
    if effective_intensity in ("thorough", "adversarial"):
        return 3
    raise ValueError(f"unknown effective_intensity for review round cap: {effective_intensity}")
