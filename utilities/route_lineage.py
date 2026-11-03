#!/usr/bin/env python3
"""SD-155: the one route-lineage walk, and the canonical route file path.

`review_lineage_routes` (capability-route.py) used to walk hash-verified
ancestry for review disposition, while `continuation_lineage_route_ids`
(capability-route.py, deleted) listed the same ancestry for the write guard
without verifying a single hash (SD-133 v74 admits this). D-120 cycle
admission needs the verified walk too. This leaf gives all three one
definition: `verified_route_lineage`.

Kept a leaf (no `capability-route` import) on purpose: `artifact_producer.py`
-- and `hooks/material-route-guard.py`, which imports `artifact_producer` --
would otherwise pull in capability-route's full import weight on every hook
invocation. `capability-route.py` re-exports the same objects so there is
still exactly one definition (`ROUTE.verified_route_lineage is
route_lineage.verified_route_lineage`). This module makes no git calls and no
subprocess calls.
"""
from __future__ import annotations

from hearting_gates import note_same_work_drift

import json
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

import route_identity


class RouteLineageError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def canonical_route_path(artifact_root: Any, route_id: str) -> Path:
    return Path(artifact_root).resolve() / ".runtime" / "routes" / f"{route_id}.json"


# The lineage-context fields that must be identical at every step. `capability`
# is included so a continuation that changed capability is refused here, before
# admission's material-input step ever sees it (D-120 step 1 note).
_LINEAGE_CONTEXT_KEYS = ("artifact_root", "cwd", "capability")


def verified_route_lineage(
    route: Dict[str, Any], *, artifact_root: Optional[Any] = None, observe: bool = False,
) -> List[Dict[str, Any]]:
    """``route`` and every verified ancestor, nearest first.

    ``observe=True`` retains unverified lineage as a read error even when
    execution compatibility permits it.

    Each step recomputes the parent's `route_identity.route_hash`, checks it
    against the child's sealed `source_route_hash`, requires
    `artifact_root`/`cwd`/`capability` to match, and refuses a cycle by
    seen-set before the next parent is even read. Name/campaign/slug sharing
    is never lineage -- only this hash chain is (SD-155).
    """
    root = artifact_root if artifact_root is not None else route.get("artifact_root")
    if route_identity.route_hash(route) != route.get("route_hash"):
        if observe:
            raise RouteLineageError("route-lineage-unverified", f"hash-mismatch:{route.get('route_id')}")
        note_same_work_drift("route-lineage-unverified", f"hash-mismatch:{route.get('route_id')}")
    lineage: List[Dict[str, Any]] = [route]
    seen = {route.get("route_id")}
    current = route
    while current.get("continuation_contract_version") == 1:
        parent_id = current.get("source_route_id")
        if not isinstance(parent_id, str) or not parent_id or parent_id in seen:
            raise RouteLineageError("route-lineage-unverified", f"cycle-or-missing-parent:{parent_id}")
        parent_path = canonical_route_path(root, parent_id)
        try:
            parent_stat = parent_path.lstat()
        except OSError as exc:
            raise RouteLineageError("route-lineage-unverified", f"parent-unreadable:{parent_id}") from exc
        if (not stat.S_ISREG(parent_stat.st_mode) or parent_path.is_symlink()
                or parent_path.resolve() != parent_path):
            raise RouteLineageError("route-lineage-unverified", f"parent-kind-or-path:{parent_id}")
        try:
            raw = parent_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RouteLineageError("route-lineage-unverified", f"parent-unreadable:{parent_id}") from exc
        try:
            parent = json.loads(raw)
        except ValueError as exc:
            raise RouteLineageError("route-lineage-unverified", f"parent-malformed:{parent_id}") from exc
        if not isinstance(parent, dict) or parent.get("route_id") != parent_id:
            raise RouteLineageError("route-lineage-unverified", f"parent-identity-mismatch:{parent_id}")
        if route_identity.route_hash(parent) != parent.get("route_hash"):
            if observe:
                raise RouteLineageError("route-lineage-unverified", f"parent-hash-mismatch:{parent_id}")
            note_same_work_drift("route-lineage-unverified", f"parent-hash-mismatch:{parent_id}")
        if parent.get("route_hash") != current.get("source_route_hash"):
            if observe:
                raise RouteLineageError("route-lineage-unverified", f"source-route-hash-mismatch:{parent_id}")
            note_same_work_drift("route-lineage-unverified", f"source-route-hash-mismatch:{parent_id}")
        for key in _LINEAGE_CONTEXT_KEYS:
            if parent.get(key) != current.get(key):
                if observe:
                    raise RouteLineageError("route-lineage-unverified", f"context-mismatch:{key}")
                note_same_work_drift("route-lineage-unverified", f"context-mismatch:{key}")
        lineage.append(parent)
        seen.add(parent_id)
        current = parent
    return lineage
