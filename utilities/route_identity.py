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
    {
        "route_hash", "route_id", "owner_attempt_id", "route_family_key",
        # Invocation-only locators are attached by a few legacy helpers while
        # reading a sealed route.  They are not part of the route contract and
        # must never invalidate lineage proof when carried in memory.
        "route_file", "_route_file",
        # `verify_route` marks a route sealed under another release's catalog.
        # The mark is about the reader, so the route keeps its own hash.
        "_registry_current",
    }
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


def registered_node_identity(metadata: dict, node: dict) -> tuple[str, str, str]:
    """Resolve node-bound workers and node-less depth-1 owners without row edits.

    An owner binding authorizes only its terminal capability-owner node. Legacy
    node-bound rows retain their exact node; inherited owner context on depth-2
    rows never substitutes for that child's own binding.
    """
    owner_values = (metadata.get("owner_route_id"), metadata.get("owner_route_hash"))
    if str(metadata.get("dispatch_depth")) == "1" and any(owner_values):
        if (metadata.get("worker_type") != "owner" or metadata.get("unit") != "_kernel/owner"
                or not all(owner_values) or node.get("dispatch_depth") != 1
                or node.get("kind") != "capability-owner" or node.get("unit") != "_kernel/owner"
                or node.get("terminal") is not True):
            raise ValueError("attempt row route identity owner binding invalid")
        for key, expected in zip(("route_id", "route_hash"), owner_values):
            if metadata.get(key) and metadata[key] != expected:
                raise ValueError("attempt row route identity aliases conflict")
        if metadata.get("route_node") not in (None, "", "_owner", node.get("id")):
            raise ValueError("attempt row route identity node conflict")
        return owner_values[0], owner_values[1], node["id"]
    return tuple(str(metadata.get(key) or "") for key in ("route_id", "route_hash", "route_node"))
