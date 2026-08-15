#!/usr/bin/env python3
"""Derive the quality-peer harness family set shared by both cross-harness
selectors (plan.md D10/§2.5, AC 10).

Both `assign_harnesses` (parallel batch) and `ordered_fallback_hops` (single
checker) use the SAME derived quality-peer set, so the two enforcement points
cannot drift apart; the derivation itself stays here with no hardcoded family
constants. A `None` policy input returns `None` so callers branch to the
not-applicable path (D8-①) instead of guessing.
"""
from __future__ import annotations


def quality_peer_families(policy_by_profile):
    """Return frozenset(deep.primary) & frozenset(balanced_deep.primary).

    `policy_by_profile` maps a profile name to a band dict carrying a `primary`
    list (the shape produced by `dispatch_defaults.query_profile_policy` or the
    sealed `harness_policy` node field). Missing profiles or missing `primary`
    bands degrade to the empty set; a `None` input returns `None` so the caller
    can mark the gate not-applicable rather than derive a wrong set.
    """
    if policy_by_profile is None:
        return None
    deep = (policy_by_profile.get("deep") or {}).get("primary") or []
    balanced_deep = (
        (policy_by_profile.get("balanced-deep") or {}).get("primary") or []
    )
    return frozenset(deep) & frozenset(balanced_deep)
