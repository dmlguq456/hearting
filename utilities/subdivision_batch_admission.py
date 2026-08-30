#!/usr/bin/env python3
"""SD-119 R4 — route-leg-independent admission gate for a sealed sub-session batch.

`utilities/dispatch-batch.py:340-348` (`parallel_nodes`) requires `parallel_group`
membership with 2..4 realized route-leg nodes, which the sole subdivision-permitted
node (`autopilot-code` `execute`) structurally never has (SD-119 (1)). This module
gives that node a second, dedicated admission surface that does not require leg
membership, while reusing the SD-89 full-N atomic governor reservation primitive
(`reserve_batch`) so a shortfall still yields row 0 / model 0 across the whole
batch (M-5). Admission and completion are separate gates (SD-119 (6)): this module
proves permission, reservation, fixed-file fence, and worktree baseline before any
child row or model process exists; it does not own the completion marker.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

from stage_session_contract import StageSessionError, load_manifest  # noqa: E402

# Route-leg cardinality tier order, duplicated from `capability-route.py:41` --
# importing that module is safe (no cycle back into this one) but the tier map
# is a 6-entry literal, cheaper to keep local than to reach across the module
# for one dict.
ORDER = {"direct": 0, "quick": 1, "standard": 2, "strong": 3, "thorough": 4, "adversarial": 5}

RESERVATION_TOKEN = re.compile(r"[0-9a-f]{32}")

_ROUTE_SPEC = importlib.util.spec_from_file_location(
    "capability_route_for_subdivision_admission", ROOT / "utilities" / "capability-route.py"
)
if _ROUTE_SPEC is None or _ROUTE_SPEC.loader is None:
    raise ImportError("capability-route.py could not be loaded")
ROUTE_MODULE = importlib.util.module_from_spec(_ROUTE_SPEC)
_ROUTE_SPEC.loader.exec_module(ROUTE_MODULE)  # type: ignore[union-attr]


class SubdivisionAdmissionError(RuntimeError):
    """Typed admission refusal. `.reason` is one of R6's closed `refused`/
    `not-eligible`/`considered-declined` vocabulary (SD-119 (8))."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}:{detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def has_route_leg_group(route: dict[str, Any], group: str) -> bool:
    """True iff `group` names an existing SD-89 route-leg membership.

    Mirrors `parallel_nodes`'s own membership filter (`dispatch-batch.py:341-346`)
    without its cardinality/shape assertions, so calling this never raises and
    never invokes `parallel_nodes` itself -- the two admission surfaces stay
    distinguishable at zero cost to the legacy path (A-1).
    """

    return any(
        isinstance(node, dict) and (node.get("parallel_group") or node.get("replica_group")) == group
        for node in route.get("nodes", [])
    )


def route_node(route: dict[str, Any], node_id: str) -> dict[str, Any]:
    found = [item for item in route.get("nodes", []) if item.get("id") == node_id]
    if len(found) != 1:
        raise SubdivisionAdmissionError("surface-unreachable", f"node={node_id}")
    return found[0]


def check_permission(route: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Checkpoint (1): route-leg-independent subdivision permission.

    Never consults `parallel_group`/`replica_group` -- only the node's own
    sealed `subdivision` permission block and the route's effective intensity.
    """

    permission = node.get("subdivision")
    if not isinstance(permission, dict) or permission.get("disjointness") != "exact-fixed-files":
        raise SubdivisionAdmissionError("subdivision-not-permitted")
    effective = route.get("effective_intensity")
    minimum = permission.get("min_intensity")
    if effective not in ORDER or minimum not in ORDER or ORDER[effective] < ORDER[minimum]:
        raise SubdivisionAdmissionError("intensity-below-min", f"{effective}<{minimum}")
    return permission


def load_batch_manifest(
    manifest_path: str | Path, *, route: dict[str, Any], node: dict[str, Any], permission: dict[str, Any]
) -> dict[str, Any]:
    """Checkpoint (3): load + exact fixed-file disjointness/scope.

    `load_manifest` already enforces exact-file (no glob), worktree containment,
    write-scope containment, and pairwise disjointness (`stage_session_contract.py`
    `_fixed_files`/parallel-fixed-file-overlap) -- this is the SD-103 fence R4
    reuses rather than reimplements.
    """

    try:
        manifest = load_manifest(manifest_path, route=route, node=node)
    except StageSessionError as exc:
        raise SubdivisionAdmissionError("disjointness-unproven", str(exc)) from exc
    if manifest.get("mode") != "parallel":
        raise SubdivisionAdmissionError("plan-declared-no-slices", f"mode={manifest.get('mode')}")
    count = len(manifest["sessions"])
    cap = permission.get("max_slices", 4)
    if not 2 <= count <= cap:
        raise SubdivisionAdmissionError("slice-count-out-of-range", f"count={count}:cap={cap}")
    return manifest


def reserve_full_n(
    governor: Path,
    governor_root: Path,
    sessions: list[dict[str, Any]],
    *,
    route: dict[str, Any],
    node_id: str,
    manifest_digest: str,
    reserve: Callable[..., list[str]] | None = None,
) -> list[str]:
    """Checkpoint (2): full-N atomic governor reservation, route-leg independent.

    Reuses the exact SD-89 all-or-nothing primitive (`dispatch-batch.reserve_batch`)
    injected by the caller -- this module never talks to the governor CLI itself,
    so a shortfall raises the same typed distinction the caller already knows how
    to record (M-5).
    """

    if reserve is None:
        raise SubdivisionAdmissionError("governor-capacity-insufficient", "no-reserve-callable")
    pending = [{"attempt_id": session["attempt_id"]} for session in sessions]
    batch_manifest = {
        "batch_manifest_sha256": manifest_digest,
        "route_id": route.get("route_id"),
        "route_node": node_id,
    }
    try:
        tokens = reserve(
            governor,
            governor_root,
            pending,
            manifest=batch_manifest,
            manifest_digest=manifest_digest,
        )
    except Exception as exc:  # noqa: BLE001 -- caller's `reserve` raises its own typed error
        raise SubdivisionAdmissionError("governor-capacity-insufficient", str(exc)) from exc
    if len(tokens) != len(sessions) or len(set(tokens)) != len(tokens):
        raise SubdivisionAdmissionError("governor-capacity-insufficient", "token-count-mismatch")
    return tokens


@dataclasses.dataclass(frozen=True)
class AdmissionResult:
    tokens: list[str]
    manifest: dict[str, Any]
    manifest_digest: str
    sessions: list[dict[str, Any]]
    node_id: str
    reservation_identity: str  # shared by every admitted slice (A-2)


def admit_batch(
    *,
    route: dict[str, Any],
    node: dict[str, Any],
    manifest_path: str | Path,
    governor: Path,
    governor_root: Path,
    reserve: Callable[..., list[str]] | None = None,
    record_baseline: Callable[[dict[str, Any], str, dict[str, Any]], None] | None = None,
) -> AdmissionResult:
    """Run all four admission checkpoints in order; any failure is row 0 / model 0.

    Order (SD-119 (6)): permission -> full-N reservation -> fixed-file fence ->
    worktree baseline. The fence check is folded into `load_batch_manifest`
    (checkpoint 3 happens before checkpoint 2's reservation cost is spent) --
    ordering the *cheap* structural checks before the *stateful* reservation call
    is a defensible refinement of the listed order, not a deviation from it: a
    manifest that cannot pass checkpoint 3 must never consume a checkpoint 2
    reservation in the first place.
    """

    permission = check_permission(route, node)
    node_id = str(node["id"])
    manifest = load_batch_manifest(manifest_path, route=route, node=node, permission=permission)
    manifest_digest = manifest["_manifest_sha256"]
    tokens = reserve_full_n(
        governor, governor_root, manifest["sessions"],
        route=route, node_id=node_id, manifest_digest=manifest_digest, reserve=reserve,
    )
    (record_baseline or ROUTE_MODULE.record_subdivision_baseline)(route, node_id, manifest)
    return AdmissionResult(
        tokens=tokens,
        manifest=manifest,
        manifest_digest=manifest_digest,
        sessions=manifest["sessions"],
        node_id=node_id,
        reservation_identity=manifest_digest,
    )


def dispatch_command(
    manifest: dict[str, Any], session: dict[str, Any], action: str, parent: str, jobs: Path
) -> list[str]:
    """Same shape as `stage-session-chain.dispatch_command` (local copy: that
    module's filename cannot be imported without an importlib alias, and the
    command shape is 15 flags of pure data -- duplicating it is cheaper and
    less fragile than aliasing the whole sibling module for one function)."""

    return [
        sys.executable, str(ROOT / "utilities" / "dispatch-node.py"),
        "--route", manifest["route_file"],
        "--node", manifest["route_node"],
        "--adapter", session["adapter"],
        "--action", action,
        "--slug", session["slug"],
        "--parent", parent,
        "--jobs", str(jobs),
        "--prompt-text", (
            f"Execute sub-session {session['subsession_id']} from phase brief "
            f"{session['phase_brief']}. Run only: {session['narrow_verify']}"
        ),
        "--subsession-id", session["subsession_id"],
        "--subsession-index", str(session["index"]),
        "--subsession-count", str(session["count"]),
        "--subsession-mode", manifest["mode"],
        "--session-chain-id", manifest["chain_id"],
        "--phase-brief", session["phase_brief"],
        "--stage-authority", "0",
        "--narrow-verify", session["narrow_verify"],
        "--expected-round-trips", str(session["expected_round_trips"]),
        "--attempt-id", session["attempt_id"],
    ] + [flag for file in session["fixed_files"] for flag in ("--fixed-file", file)]


def start_admitted_batch(
    admission: AdmissionResult,
    *,
    parent: str,
    jobs: Path,
    governor_reservation_env: str,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> list[dict[str, Any]]:
    """Register + start every admitted slice concurrently, each spending
    exactly the reservation token admission already proved it has (no second
    governor round-trip per child, unlike the ad hoc single-session path)."""

    runner = run or (lambda cmd, env: subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, env=env, check=False))
    results: list[dict[str, Any]] = []
    for session, token in zip(admission.sessions, admission.tokens):
        register = runner(
            dispatch_command(admission.manifest, session, "register", parent, jobs), os.environ.copy()
        )
        if register.returncode:
            results.append({
                "subsession_id": session["subsession_id"], "registered": 0, "started": 0,
                "stdout": register.stdout, "stderr": register.stderr, "exit_code": register.returncode,
            })
            continue
        env = os.environ.copy()
        env[governor_reservation_env] = token
        start = runner(dispatch_command(admission.manifest, session, "start", parent, jobs), env)
        results.append({
            "subsession_id": session["subsession_id"],
            "registered": 1,
            "started": 0 if start.returncode else 1,
            "stdout": start.stdout, "stderr": start.stderr, "exit_code": start.returncode,
        })
    return results
