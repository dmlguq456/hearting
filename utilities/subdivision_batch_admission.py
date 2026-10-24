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
import hashlib
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

from hearting_gates import gates_on  # noqa: E402
from route_authority import pinned_launch_harness  # noqa: E402
from stage_session_contract import (  # noqa: E402
    ADAPTERS,
    StageSessionError,
    _git_path,
    load_manifest,
    same_repository_worktree,
)
from dispatch_subsession_advance import chain_manifest_pointer_path  # noqa: E402
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    close_attempt_row,
    close_attempt_row_if,
    parse_registry_metadata,
    resolve_global_registry,
    terminal_claim_observation,
)

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


# SD-119 impl-review round 1 (F-3) narrowed by SD-103 (routing-flex,
# 2026-09-07). The original gate refused EVERY live parallel entry
# unconditionally, "until R5's artifact-base fence lands". What R5 adds is the
# per-slice `{"base": "worktree"|"artifact", ...}` fence, the producer-output
# ∩ write_scope intersection, a content-digest baseline/scan root, and an
# ownership receipt -- all of which concern slices that write ARTIFACT-base
# paths. A slice whose files live under the worktree is fenced today by
# `stage_session_contract.load_manifest` (exact files, no globs, worktree
# containment, write-scope containment, pairwise disjointness) and audited at
# the gate by `capability-route.py complete --subsession-manifest`
# (baseline-subtracted diff-scope audit, `subdivision-scope-violation`).
# Measured effect of the blanket gate: 0 subdivision rows against 100 execute
# rows in the canonical registry (2026-08-28..09-07), i.e. SD-103 never fired.
# The gate now refuses exactly the case the unlanded fence is for: a slice that
# declares a non-worktree `base`. An unreadable manifest is left to
# `admit_batch`, which types that refusal itself.
def raise_if_parallel_entry_fail_closed(manifest_path: str | Path | None = None) -> None:
    if manifest_path is None:
        return
    try:
        raw = json.loads(Path(manifest_path).resolve().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    sessions = raw.get("sessions") if isinstance(raw, dict) else None
    for offset, session in enumerate(sessions if isinstance(sessions, list) else [], 1):
        if not isinstance(session, dict):
            continue
        base = session.get("base")
        if isinstance(base, dict):
            base = base.get("base")
        if base in (None, "worktree"):
            continue
        raise SubdivisionAdmissionError(
            "scope-unproven",
            f"slice {session.get('subsession_id') or offset} declares base={base!r}; "
            "R5 artifact-base fence/baseline/ownership-receipt not yet landed, only "
            "worktree-base slices are admitted (SD-119 R4, narrowed by SD-103)",
        )


def persist_chain_manifest(jobs: Path, manifest: dict[str, Any]) -> Path:
    """Seal the admitted manifest at the chain-id-keyed pointer BEFORE any
    slice starts: `dispatch-node.py` refuses a slice start whose chain has no
    sealed manifest (`subsession-chain-manifest-unsealed`, defect F3), and the
    supervisor advance reads the same pointer. Same location and bytes as
    `stage-session-chain.py`'s serial-path persist."""
    path = chain_manifest_pointer_path(Path(jobs), manifest["chain_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return path


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
    jobs: str | Path | None = None,
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
    if record_baseline is None:
        # SD-OPEN-53: the admission-time baseline lands in the state root of
        # the registry the caller holds (`jobs`), the same root the audit
        # later reads it back from -- never the inherited/default root.
        def record_baseline(route, node_id, manifest):
            return ROUTE_MODULE.record_subdivision_baseline(route, node_id, manifest, jobs=jobs)
    record_baseline(route, node_id, manifest)
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
    """The one slice launch command (`stage-session-chain.py` uses it too).

    `--subsession-worktree` pins the launch to the sealed manifest worktree. The
    owner's inherited attempt id, when present, rides as a trailing adapter
    argument so the slice binds to exactly that owner row."""

    command = [
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
            f"{session['phase_brief']} within the fixed files. "
            f"Then verify only with: {session['narrow_verify']}"
        ),
        "--subsession-id", session["subsession_id"],
        "--subsession-index", str(session["index"]),
        "--subsession-count", str(session["count"]),
        "--subsession-mode", manifest["mode"],
        "--subsession-purpose", session.get("subsession_purpose") or "planned",
        "--session-chain-id", manifest["chain_id"],
        "--phase-brief", session["phase_brief"],
        "--stage-authority", "0",
        "--narrow-verify", session["narrow_verify"],
        "--expected-round-trips", str(session["expected_round_trips"]),
        "--attempt-id", session["attempt_id"],
    ] + [flag for file in session["fixed_files"] for flag in ("--fixed-file", file)]
    # Plan slices pin their launch tree; a serial-chain manifest (SD-119) has none
    # and keeps launching in the route's sealed cwd.
    if manifest.get("worktree"):
        command += ["--subsession-worktree", str(manifest["worktree"])]
    owner_attempt_id = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
    if owner_attempt_id:
        command += ["--", "--parent-attempt-id", owner_attempt_id]
    return command


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


BATCH_START_FAILED = "subsession-batch-start-failed"


def _close_unclaimed_row(jobs: Path, attempt_id: str) -> int:
    """Close one slice row whose start failed before its launcher claimed it, so
    the owner's join never waits on a row that will never run. A claimed (launched)
    row is left to its own lifecycle; returns 1 only when this call closed it."""

    def never_claimed(fields: list[str]) -> bool:
        return fields[1] == "open" and parse_registry_metadata(fields[5]).get("launch_claimed") == "0"

    try:
        return 1 if close_attempt_row_if(jobs, attempt_id, BATCH_START_FAILED, never_claimed) else 0
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
    close_unclaimed: Callable[[Path, str], int] | None = None,
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
    `registered: 0` is true of the registry too (F-4, round 2). Since the
    SD-103 narrowing of F-3 this function IS reachable for worktree-base
    slices; no test yet exercises it against a real governor reservation
    lifecycle (first live parallel execute is an owed canary), and an
    unconsumed token still has no explicit `model-worker-governor.release`
    (no caller threads a `governor_root` through to release with)."""

    runner = run or (lambda cmd, env: subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, env=env, check=False))
    registrations: list[tuple[dict[str, Any], subprocess.CompletedProcess[str]]] = []
    for session in admission.sessions:
        # This is only a subprocess-boundary precheck.  The adapter's
        # claim_attempt_row() owns the real jobs.log.lock fence; this function
        # must not assume the lock survives the registration subprocess.
        route_id = str(admission.manifest.get("route_id", ""))
        owner_attempt_id = str(session.get("attempt_id", ""))
        if (
            re.fullmatch(r"[A-Za-z0-9._-]+", route_id)
            and re.fullmatch(r"[A-Za-z0-9._-]+", owner_attempt_id)
            and terminal_claim_observation(jobs, route_id, owner_attempt_id) is not None
        ):
            register = subprocess.CompletedProcess(
                [], 65, "", "terminal-claim-conflict"
            )
            registrations.append((session, register))
            break
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

    # Every slice registered: seal the manifest once, before the first start
    # (F3 precondition; see `persist_chain_manifest`).
    persist_chain_manifest(Path(jobs), admission.manifest)
    results = []
    for session, token in zip(admission.sessions, admission.tokens):
        env = os.environ.copy()
        env[governor_reservation_env] = token
        # Slices share one worktree: `git status` must not take index.lock.
        env["GIT_OPTIONAL_LOCKS"] = "0"
        start = runner(dispatch_command(admission.manifest, session, "start", parent, jobs), env)
        closed = (close_unclaimed or _close_unclaimed_row)(jobs, session["attempt_id"]) if start.returncode else 0
        results.append({
            "subsession_id": session["subsession_id"],
            "registered": 1,
            "started": 0 if start.returncode else 1,
            "cancelled": 0,
            "closed_unclaimed": closed,
            "refusal_reason": "",
            "stdout": start.stdout, "stderr": start.stderr, "exit_code": start.returncode,
        })
    return results


# ---------------------------------------------------------------------------
# SD-103 cheap path: the plan declares 2..4 independent parts in one fenced
# `slices` block; the machine mints the ids, writes the briefs, and proves the
# fence with the same `load_manifest` the admission re-proves. A typed
# `StageSessionError` is the answer "run this stage as one session".
# ---------------------------------------------------------------------------
_SLICE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")

SLICE_RULES = (
    "Edit only the fixed_files above. Never run git add, commit, checkout, restore, stash, "
    "reset or rollback; read git state with `GIT_OPTIONAL_LOCKS=0 git ...`. checklist.md and "
    "the dev log belong to the owner. If a file outside the list is needed, stop and hand "
    "off. Finish by reporting the changed files and the verify result; your final `artifact:` line is your state ledger's absolute path, never a source file."
)


def single_session_next_action(route_file: Any, node_id: str, slug: str, parent: str) -> str:
    """The one command that runs the stage as an ordinary single session (printed, never run)."""
    return (
        "run the stage as one session: python3 $AGENT_HOME/utilities/stage-dispatch-fallback.py "
        f"--route {route_file} --node {node_id} --slug {slug} --parent {parent} --start"
    )


def _slices_invalid(detail: str) -> StageSessionError:
    return StageSessionError(f"plan-slices-invalid:{detail}")


def read_slices(path: Path | str) -> list | None:
    """Read the slice list a plan declares: None when it declares none.

    A `.md` plan declares slices with exactly one top-level fenced block whose
    info string starts with `slices`; fences nested inside another fence (an
    example, a heredoc) are not counted. Any other path is a JSON file. Two
    blocks, bad JSON, or a non-list value is `plan-slices-invalid`."""

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _slices_invalid(f"unreadable:{path}") from exc
    if path.suffix.lower() == ".md":
        blocks: list[str] = []
        fence: tuple[str, int] | None = None
        depth = 0
        current: list[str] | None = None
        for line in text.splitlines():
            match = _FENCE_OPEN.match(line)
            if fence is None:
                if match:
                    fence, depth = (match.group(1)[0], len(match.group(1))), 1
                    info = match.group(2).strip().split()
                    current = [] if info and info[0] == "slices" and fence[0] == "`" else None
                continue
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= fence[1]:
                if match.group(2).strip():
                    # An example fence inside a fence (a heredoc writing a plan):
                    # counted as nesting so its bare closer does not end the outer one.
                    depth += 1
                else:
                    depth -= 1
                    if depth == 0:
                        if current is not None:
                            blocks.append("\n".join(current))
                        fence, current = None, None
                        continue
            if current is not None:
                current.append(line)
        if not blocks:
            return None
        if len(blocks) > 1:
            raise _slices_invalid("more-than-one-slices-block")
        text = blocks[0]
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise _slices_invalid("json-error") from exc
    slices = raw.get("slices") if isinstance(raw, dict) else raw
    if not isinstance(slices, list):
        raise _slices_invalid("not-a-list")
    return slices


def _node_for(route: dict, node_id: str) -> dict:
    found = [item for item in route.get("nodes", []) if item.get("id") == node_id]
    if len(found) != 1:
        raise StageSessionError("route-node-not-unique")
    return found[0]


def _pin_harness_available(route: dict, node: dict, harness: str, jobs) -> bool:
    """`dispatch-node`'s own hard availability test for a sealed worker pin (one helper, not a copy)."""
    module = _load_sibling("dispatch_node_for_stage_session_chain", "dispatch-node.py")
    try:
        jobs = jobs if jobs is not None else resolve_global_registry(ROOT, None, 2, "check").path
    except DispatchContractError:
        return False  # no registry to read the limits from: the request stands
    return module.pin_harness_available(route, node, harness, jobs)


def _load_sibling(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / filename)
    if spec is None or spec.loader is None:
        raise ImportError(f"{filename} could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def route_allocated_harness(route: dict, node: dict, jobs) -> str:
    """The harness the route's allocation ranks first for this node.

    Falls back to the owner's own harness, then `claude`, when the allocation
    cannot be read."""

    try:
        fallback = _load_sibling("stage_dispatch_fallback_for_slices", "stage-dispatch-fallback.py")
        jobs = jobs if jobs is not None else resolve_global_registry(ROOT, None, 2, "check").path
        hops, _ = fallback.ordered_fallback_hops(route, node, Path(jobs))
        for hop in hops:
            if hop.get("fallback_hop") not in {"same-harness-headless", "cross-harness-headless"}:
                continue
            for row in hop.get("candidates", []):
                if row.get("status") == "supported" and row.get("child_harness") in ADAPTERS:
                    return row["child_harness"]
    except Exception:  # noqa: BLE001 -- the allocation is a preference, never a gate
        pass
    current = os.environ.get("AGENT_DISPATCH_CURRENT_HARNESS", "")
    return current if current in ADAPTERS else "claude"


def _default_slice_worktree(sealed_cwd: Path) -> Path:
    """The caller's worktree when it is a usable worktree of the route's repository, else the route cwd."""
    top = _git_path(Path.cwd(), "--show-toplevel")
    if top is not None and top != sealed_cwd and not gates_on() and same_repository_worktree(top, sealed_cwd):
        return top
    return sealed_cwd


def slice_chain_id(route_id: str, node_id: str, slices: list[dict[str, Any]],
                   *, owner_attempt_id: str | None = None) -> str:
    """Return the stable chain identity for this owner and exact slice set.

    Replacement owners get their own attempt namespace so a later `--slices`
    launch cannot overwrite the prior owner's manifest or slice receipts.
    Lower-level callers without an owner retain the historical identity.
    """
    if owner_attempt_id is None:
        owner_attempt_id = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    fixed_files = [
        sorted(map(str, item.get("files", item.get("fixed_files")) or []))
        if isinstance(item, dict) else []
        for item in slices
    ]
    seed_parts = [route_id, node_id, fixed_files]
    if owner_attempt_id:
        seed_parts.append(owner_attempt_id)
    seed = json.dumps(
        seed_parts,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()[:12]
    return f"ssc-{node_id}-{digest}"


def plan_slices(
    *, route_path: Path, node_id: str, slices_path: Path,
    output_path: Path, worktree: Path | None = None, default_adapter: str | None = None,
    jobs: Path | None = None, plan_path: Path | None = None,
) -> dict:
    """Build and prove a parallel sub-session manifest from a slice list.

    `slices_path` is a plan `.md` with one `slices` block or a JSON file: a list
    (or `{"slices": [...]}`) of `{"id", "files", "verify", "brief"?,
    "expected_round_trips"?, "adapter"?}` (`fixed_files`/`narrow_verify` are
    accepted aliases). Ids are minted deterministically from the route, node and
    file sets, one phase-brief file is written per slice beside the manifest, and
    the manifest is written only after `load_manifest` has proven exact files,
    worktree and write-scope containment and pairwise disjointness.
    """
    route_path = Path(route_path).resolve()
    route = json.loads(route_path.read_text(encoding="utf-8"))
    route["_route_file"] = str(route_path)
    node = _node_for(route, node_id)
    permission = node.get("subdivision")
    if not isinstance(permission, dict) or permission.get("disjointness") != "exact-fixed-files":
        raise StageSessionError("parallel-subdivision-not-permitted")
    slices = read_slices(slices_path)
    if slices is None:
        raise StageSessionError("plan-declared-no-slices")
    if plan_path is None and Path(slices_path).suffix.lower() == ".md":
        plan_path = Path(slices_path)
    cap = permission.get("max_slices", 4)
    if not 2 <= len(slices) <= cap:
        raise StageSessionError(f"parallel-session-count-invalid:2:{cap}")
    sealed_cwd = route.get("cwd")
    if not isinstance(sealed_cwd, str) or not sealed_cwd:
        raise StageSessionError("plan-slices-route-cwd-missing")
    sealed_cwd = Path(sealed_cwd).resolve()
    worktree = Path(worktree).resolve() if worktree is not None else _default_slice_worktree(sealed_cwd)
    if worktree != sealed_cwd and (gates_on() or not same_repository_worktree(worktree, sealed_cwd)):
        # The manifest's `worktree` becomes every slice's launch tree; it must be
        # the route's sealed cwd or (gates off) a linked worktree of the same
        # repository, never a foreign tree whose identity fields still match
        # (canary review round 1, B2).
        raise StageSessionError(f"plan-slices-worktree-mismatch:{worktree}:{sealed_cwd}")
    output_path = Path(output_path).resolve()
    def fixed_of(item):
        return item.get("files", item.get("fixed_files")) if isinstance(item, dict) else None

    chain_id = slice_chain_id(str(route.get("route_id") or ""), node_id, slices)
    allocated = None
    sessions = []
    briefs = []
    seen = set()
    for index, item in enumerate(slices, 1):
        if not isinstance(item, dict):
            raise StageSessionError(f"plan-slice-invalid:{index}")
        slice_id = str(item.get("id") or f"s{index}")
        if not _SLICE_ID.fullmatch(slice_id) or slice_id in seen:
            raise StageSessionError(f"plan-slice-id-invalid:{slice_id}")
        seen.add(slice_id)
        brief_text = item.get("brief")
        if brief_text is not None and not isinstance(brief_text, str):
            raise StageSessionError(f"plan-slice-brief-invalid:{slice_id}")
        narrow_verify = item.get("verify", item.get("narrow_verify"))
        if not isinstance(narrow_verify, str) or not narrow_verify.strip() or "\n" in narrow_verify:
            raise StageSessionError(f"narrow-verify-invalid:{slice_id}")
        rounds = item.get("expected_round_trips", 2)
        requested = item.get("adapter") or default_adapter
        if requested is None:
            if allocated is None:
                allocated = route_allocated_harness(route, node, jobs)
            requested = allocated
        # The manifest is the authority `dispatch-node` follows for a slice, so the route's sealed
        # worker pin (CONVENTIONS §2.1) is applied here, once, when the manifest is written -- while
        # the pinned harness can run this node, as at a direct launch; otherwise the request stands.
        adapter, _requested = pinned_launch_harness(
            route, worker_type="stage", requested=requested,
            available=lambda harness: _pin_harness_available(route, node, harness, jobs))
        fixed = fixed_of(item)
        if not isinstance(fixed, list) or not fixed:
            raise StageSessionError(f"fixed-files-missing:{slice_id}")
        brief_path = output_path.parent / f"{chain_id}-{slice_id}.brief.md"
        briefs.append((brief_path, (
            f"# {node_id} slice {index}/{len(slices)} — {slice_id}\n\n"
            f"chain: {chain_id}\n"
            + (f"plan: {plan_path}\n" if plan_path else "")
            + f"worktree: {worktree}\n"
            f"fixed_files (exhaustive; touch nothing else, commit nothing):\n"
            + "".join(f"- {f}\n" for f in fixed)
            + f"narrow_verify: {narrow_verify.strip()}\n\n"
            f"rules: {SLICE_RULES}\n"
            + (f"\n{brief_text.strip()}\n" if brief_text and brief_text.strip() else "")
        )))
        sessions.append({
            "subsession_id": f"ss-{chain_id[4:]}-{slice_id}",
            "attempt_id": f"att-{chain_id[4:]}-{slice_id}",
            "adapter": adapter,
            "slug": f"{node_id}-{slice_id}",
            "phase_brief": str(brief_path),
            "fixed_files": [str(f) for f in fixed],
            "narrow_verify": narrow_verify.strip(),
            "expected_round_trips": rounds,
            "node": f"{node_id}-slice-{index}",
        })
    manifest = {
        "schema_version": 1,
        "kind": "stage-session-chain",
        "chain_id": chain_id,
        "mode": "parallel",
        "worktree": str(worktree),
        "route_file": str(route_path),
        "route_id": route.get("route_id"),
        "route_hash": route.get("route_hash"),
        "route_node": node_id,
        "completion_gate": node.get("completion_gate"),
        "sessions": sessions,
    }
    if plan_path:
        manifest["plan"] = str(Path(plan_path).resolve())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for brief_path, text in briefs:
        brief_path.write_text(text, encoding="utf-8")
    output_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        proven = load_manifest(output_path, route=route, node=node)
    except StageSessionError:
        output_path.unlink(missing_ok=True)
        for brief_path, _text in briefs:
            brief_path.unlink(missing_ok=True)
        raise
    return {
        "planned": "ok", "chain_id": chain_id, "manifest": str(output_path),
        "sessions": len(proven["sessions"]),
        "fixed_files": sum(len(s["fixed_files"]) for s in proven["sessions"]),
        "manifest_sha256": proven["_manifest_sha256"],
    }
