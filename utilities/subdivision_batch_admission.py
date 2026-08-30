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
from dispatch_contract import DispatchContractError, close_attempt_row  # noqa: E402

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


# SD-119 impl-review round 1 (F-3): a TEMPORARY fail-closed gate for the two
# LIVE parallel entry points -- `stage-session-chain.py`'s `mode == "parallel"`
# branch and `dispatch-batch.py`'s subdivision-manifest-without-route-leg
# branch. `admit_batch()` below proves permission, full-N reservation, and
# exact-fixed-file scope (A-1/A-2), but not the R5 artifact-base fence: a
# per-slice `{"base": "worktree"|"artifact", "path": ...}` declaration, the
# producer-output ∩ write_scope intersection, a content-digest baseline/scan
# root, and an ownership receipt (SD §13.35.1-(7)/(8)). None of that exists
# yet (R5 is not landed), so a live call reaching `admit_batch()` today would
# reserve full-N governor slots and start real children with only the
# worktree fence proven -- not what SD §13.35.5's rollout note calls safe:
# "R4의 fail-closed 게이트가 열리기 전에는 parallel spawn 0이므로 R1~R3만
# 착지해도 회귀 위험 없이 serial 사장 원인이 닫힌다". `admit_batch()` itself
# and its 8 existing unit tests are deliberately NOT gated here -- A-1/A-2
# keep proving admission safety at unit level (impl-review round 1 explicit
# instruction: do not delete or gate `admit_batch()`). Delete this function
# and its two call sites, and only those, once R5 (artifact-base fence +
# baseline/delta audit + ownership receipt) lands and the two live entry
# points can prove it before calling `admit_batch()`.
def raise_if_parallel_entry_fail_closed() -> None:
    raise SubdivisionAdmissionError(
        "scope-unproven",
        "R5 artifact-base fence/baseline/ownership-receipt not yet landed; "
        "parallel sub-session batch admission stays fail-closed (SD-119 R4)",
    )


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


BATCH_REGISTRATION_INCOMPLETE = "subsession-batch-registration-incomplete"


def _cancel_registered_row(jobs: Path, attempt_id: str) -> int:
    """Mark one already-registered, never-started child row terminal so the
    all-or-nothing receipt below is not contradicted by a live registry row.
    A row that cannot be closed (already terminal, teardown-claimed, or a
    registry the caller cannot write) reports 0 rather than raising -- the
    batch refusal is the caller's answer either way."""

    try:
        return 1 if close_attempt_row(jobs, attempt_id, BATCH_REGISTRATION_INCOMPLETE) else 0
    except (DispatchContractError, OSError):
        return 0


def start_admitted_batch(
    admission: AdmissionResult,
    *,
    parent: str,
    jobs: Path,
    governor_reservation_env: str,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    cancel_row: Callable[[Path, str], int] | None = None,
) -> list[dict[str, Any]]:
    """Register every admitted slice first; only start ANY slice once EVERY
    registration has succeeded (F-4, impl-review round 1). Registration and
    start each run sequentially -- this fixes the all-or-nothing invariant a
    prior register-then-immediately-start loop violated (a mid-batch
    registration failure used to leave earlier slices already started while
    later ones never registered), not the sequencing itself. Typed
    partial-start recovery: a registration failure anywhere in the batch
    means zero slices start, including ones that themselves registered
    cleanly -- their reservation token is simply never consumed by a start
    call, and their row is cancel-marked so the returned receipt's
    `registered: 0` is true of the registry too (F-4, round 2). This
    function is presently unreachable from either live entry
    point (F-3's fail-closed gate), so no test exercises it against a real
    governor reservation lifecycle; a future R5 landing that reopens the
    live path should also decide whether an unconsumed token needs explicit
    `model-worker-governor.release` (not implemented here -- no existing
    caller of this function threads a `governor_root` through to release
    with)."""

    runner = run or (lambda cmd, env: subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, env=env, check=False))
    registrations: list[tuple[dict[str, Any], subprocess.CompletedProcess[str]]] = []
    for session in admission.sessions:
        register = runner(
            dispatch_command(admission.manifest, session, "register", parent, jobs), os.environ.copy()
        )
        registrations.append((session, register))
        if register.returncode:
            break

    all_registered = (
        len(registrations) == len(admission.sessions)
        and all(register.returncode == 0 for _session, register in registrations)
    )
    if not all_registered:
        # F-4 (impl-review round 2): the contract is all-or-nothing on BOTH
        # counters, not just `started`. A slice that registered before the
        # failure is cancel-marked here and then reported `registered: 0`, so
        # the receipt and the registry agree that this batch admitted nothing.
        # `cancelled` carries the fact that a row briefly existed -- the
        # receipt states it explicitly instead of hiding it behind a 0.
        canceller = cancel_row or _cancel_registered_row
        results: list[dict[str, Any]] = []
        for session, register in registrations:
            cancelled = 0
            if register.returncode == 0:
                cancelled = canceller(jobs, session["attempt_id"])
            results.append({
                "subsession_id": session["subsession_id"],
                "registered": 0,
                "started": 0,
                "cancelled": cancelled,
                "refusal_reason": BATCH_REGISTRATION_INCOMPLETE,
                "stdout": register.stdout, "stderr": register.stderr, "exit_code": register.returncode,
            })
        for session in admission.sessions[len(registrations):]:
            results.append({
                "subsession_id": session["subsession_id"],
                "registered": 0, "started": 0, "cancelled": 0,
                "refusal_reason": BATCH_REGISTRATION_INCOMPLETE,
                "stdout": "", "stderr": "", "exit_code": None,
            })
        return results

    results = []
    for session, token in zip(admission.sessions, admission.tokens):
        env = os.environ.copy()
        env[governor_reservation_env] = token
        start = runner(dispatch_command(admission.manifest, session, "start", parent, jobs), env)
        results.append({
            "subsession_id": session["subsession_id"],
            "registered": 1,
            "started": 0 if start.returncode else 1,
            "cancelled": 0,
            "refusal_reason": "",
            "stdout": start.stdout, "stderr": start.stderr, "exit_code": start.returncode,
        })
    return results
