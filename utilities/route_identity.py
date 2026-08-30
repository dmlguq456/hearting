#!/usr/bin/env python3
"""Single-source route hash/id derivation (SD-116 WP1).

`capability-route.py` (compiler) and `dispatch_continuation_budget.py`
(supervisor-side resolver) each recomputed `route_hash` independently before
this module existed. The two recomputations diverged after SD-118 added
`owner_attempt_id`/`route_family_key` to the compiled payload -- the compiler
excluded both from the hash, the resolver excluded neither, so every route
compiled after SD-118 failed the resolver's hash check and fell to the
compatibility floor (12-turn budget) instead of its declared, larger budget.

This leaf holds the one definition both call sites import."""

from __future__ import annotations

import hashlib
import json

ROUTE_HASH_EXCLUDED_KEYS = frozenset(
    {"route_hash", "route_id", "owner_attempt_id", "route_family_key"}
)


def canonical(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def route_hash(payload: dict) -> str:
    bare = {
        key: value
        for key, value in payload.items()
        if key not in ROUTE_HASH_EXCLUDED_KEYS
    }
    return "sha256:" + hashlib.sha256(canonical(bare)).hexdigest()


def route_id_from_hash(digest: str) -> str:
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ValueError("route hash must be a sha256: digest")
    return "rt-" + digest.split(":", 1)[1][:16]
