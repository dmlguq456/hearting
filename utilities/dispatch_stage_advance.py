#!/usr/bin/env python3
"""SD-110: runtime-owned deterministic stage advance.

Shared pure core used by both per-process session supervisors
(`claude-session-supervisor.py`, `codex-app-server-supervisor.py`). Census and
every eligibility predicate are pure functions of `(route, completed, started,
rows, phase)`; all side effects live behind `StageAdvanceServices`. This module
never runs `git`, never merges, never pushes, and never authors brief prose.

Modelled directly on `utilities/dispatch-recovery.py:789 coordinate_recovery`
and its injected-services + named-checkpoint transaction shape (`_atomic_json`,
`_commit_phase`, `_checkpoint`) so that A-16's five crash windows are a unit
cost rather than requiring a real process kill.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Callable, FrozenSet, Protocol

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))


def _load_capability_route():
    source_path = ROOT / "utilities" / "capability-route.py"
    spec = importlib.util.spec_from_file_location(
        "dispatch_stage_advance_capability_route", source_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_stage_dispatch_fallback():
    source_path = ROOT / "utilities" / "stage-dispatch-fallback.py"
    spec = importlib.util.spec_from_file_location(
        "dispatch_stage_advance_stage_dispatch_fallback", source_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROUTE = _load_capability_route()
FALLBACK = _load_stage_dispatch_fallback()

import dispatch_completion_join as JOIN  # noqa: E402
import dispatch_contract as DC  # noqa: E402
from dispatch_contract import DispatchContractError  # noqa: E402


STAGE_ADVANCE_RECORD_SCHEMA_VERSION = 1
BRIEF_TEMPLATE_ID = "stage_brief_template_v1"

# §13.32.1-(4): closed 16-value refusal vocabulary. Order is the enum's
# authored priority; it is NOT the sequential check order `classify_boundary`
# and `coordinate_stage_advance` execute in (recorded deviation, A-8: a
# terminal successor is classified before tuple-unsealed is even evaluated,
# because terminal-node is a pure structural fact that makes every later
# precondition moot).
REFUSAL_REASONS = (
    "stage-advance-phase-ineligible",
    "stage-advance-receipt-schema-unsupported",
    "stage-advance-tuple-unsealed",
    "stage-advance-launch-compatibility-mismatch",
    "stage-advance-evidence-unreadable",
    "stage-advance-gate-unproven",
    # ---- above this point gate close is always 0 ----
    "stage-advance-commit-expected",  # gate close 1, start 0
    "stage-advance-successor-blocked",
    "stage-advance-successor-ambiguous",
    "stage-advance-parallel-group-boundary",
    "stage-advance-arbiter-declared",
    "stage-advance-terminal-node",
    "stage-advance-claim-conflict",
    "stage-advance-lifecycle-unsupported",
    "stage-advance-harness-unavailable",
    "stage-advance-successor-start-failed",
)

# The five crash-checkpoint names, 1:1 with A-16's CRASH_MATRIX
# (`dispatch_stage_advance_crash.test.py`). Fixed here since checkpoint
# placement in `coordinate_stage_advance` binds these names structurally.
CHECKPOINTS = (
    "before-gate-close",
    "before-intent",
    "before-register",
    "before-start",
    "after-start",
)

# Structural refusals produced by `classify_boundary` alone (no gate evidence,
# no registry mutation). `None` means "eligible-linear, proceed".
_STRUCTURAL_REASONS = frozenset(
    {
        "stage-advance-terminal-node",
        "stage-advance-tuple-unsealed",
        "stage-advance-commit-expected",
        "stage-advance-successor-blocked",
        "stage-advance-successor-ambiguous",
        "stage-advance-parallel-group-boundary",
        "stage-advance-arbiter-declared",
    }
)


class StageAdvanceError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class StageAdvanceRequest:
    jobs: Path
    route_file: Path
    predecessor_node: str
    predecessor_terminal_attempt_id: str
    parent_attempt_id: str
    supervisor_phase: str
    delivered_open_attempt_ids: FrozenSet[str]
    receipt_schema_negotiated: int
    harness: str
    worktree: str


@dataclass(frozen=True)
class StageAdvanceClaim:
    stage_advance_id: str
    claim_key: tuple
    successor_attempt_id: str
    replayed: bool


@dataclass(frozen=True)
class StageAdvanceResult:
    outcome: str
    reason: str
    stage_advance_id: str
    successor_node: str | None
    successor_attempt_id: str | None
    claim_key: tuple | None
    brief_template_digest: str
    gate_closed: bool
    registered: bool
    started: bool
    child_spawned: bool
    record_path: Path | None


class StageAdvanceServices(Protocol):
    """Checked mutation/observation boundary. Tests inject a deterministic impl."""

    def close_gate(
        self,
        request: StageAdvanceRequest,
        *,
        node: str,
        terminal_attempt_id: str,
        artifact: str,
    ) -> dict: ...

    def claim(
        self,
        request: StageAdvanceRequest,
        *,
        stage_advance_id: str,
        claim_key: tuple,
        successor_node: str,
    ) -> StageAdvanceClaim: ...

    def start_successor(
        self,
        request: StageAdvanceRequest,
        *,
        claim: StageAdvanceClaim,
        successor: dict,
        slug: str,
        prompt_file: Path,
    ) -> dict: ...

    def observe_record(
        self, request: StageAdvanceRequest, *, stage_advance_id: str
    ) -> dict | None: ...

    def process_quiescence(
        self, request: StageAdvanceRequest, *, attempt_id: str
    ) -> bool: ...


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _atomic_json(path: Path, payload: dict) -> None:
    """Same tempfile+fsync+os.replace shape as
    `dispatch_completion_join.py`'s supervisor-state writer and
    `dispatch-recovery.py:_atomic_json`."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_canonical_bytes(payload) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def stage_advance_record_path(jobs: Path, stage_advance_id: str) -> Path:
    if not stage_advance_id.startswith("sadv-"):
        raise StageAdvanceError("stage-advance-record-identity-invalid")
    return jobs.parent / "stage_advance" / f"{stage_advance_id}.json"


@contextlib.contextmanager
def _stage_advance_transaction_lock(jobs: Path, stage_advance_id: str):
    """Serialize the ENTIRE gate-close -> intent -> register -> start
    transition for one `stage_advance_id` across concurrent coordinator
    processes (T3 correction, round-1 blocking finding 2).

    `dispatch_contract.claim_stage_advance` already CASes the *claim* itself
    under `<jobs>.lock`, but the record load that decides whether this
    process even attempts gate-close/claim/start
    (`coordinate_stage_advance`'s `_load_record` above) happened outside any
    lock. Two coordinators racing the same just-closed predecessor could both
    load a record with no `started` phase from stale snapshots and both call
    `start_successor`, because each held its own in-memory `record` dict and
    never re-read the other's write. A distinct exclusive flock, keyed by the
    deterministic `stage_advance_id` (not the shared `<jobs>.lock`, which
    would serialize unrelated advances against each other), makes the second
    racer block until the first has fully committed or refused, then observe
    that committed record via the ordinary `_load_record` replay path instead
    of independently reaching `start_successor`."""

    lock_dir = jobs.parent / "stage_advance" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{stage_advance_id}.lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def completed_nodes(jobs: Path, route_id: str) -> FrozenSet[str]:
    """Canonical completion authority ONLY — the marker directory
    (`capability-route.py:completion_dir`). Never a supervisor-local ledger."""

    directory = ROUTE.completion_dir(route_id, jobs=jobs)
    if not directory.is_dir():
        return frozenset()
    out = set()
    for path in directory.glob("*.json"):
        name = path.name
        if name.endswith(".attempt.json"):
            continue
        stem = name[: -len(".json")]
        # history sidecars are `<node>.<sequence>.json` — only the bare
        # `<node>.json` canonical marker counts.
        if "." in stem:
            continue
        out.add(stem)
    return frozenset(out)


def started_nodes(jobs: Path, route_id: str, route_hash: str) -> FrozenSet[str]:
    """Every node id with ANY registry row bound to this exact
    `(route_id, route_hash)` pair (cancelled rows included — their recovery
    is SD-105/106's `claim_recovery_retry` path, not stage advance).

    Scoping by `route_hash` — rather than `route_id` alone — is what actually
    pins `advance_generation` (§4.2): a distinct generation always compiles
    to a distinct `route_hash` (`advance_generation` is hashed into the route
    payload, `capability-route.py` continuation compile), and `route_hash` is
    already recorded on every registry row via the existing `--route-hash`
    start-wrapper plumbing, so no new metadata is required. Fail-closed
    direction (§4.1): a row that matches `route_id` but was bound to a
    different generation's `route_hash` is excluded here so the *current*
    generation can re-advance a node its predecessor generation already
    started — filtering on `route_id` alone silently trusted that coupling
    without checking it."""

    if not jobs.is_file():
        return frozenset()
    out = set()
    for line in jobs.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
        if metadata.get("route_id") != route_id:
            continue
        if metadata.get("route_hash") != route_hash:
            continue
        node = metadata.get("route_node")
        if node:
            out.add(node)
    return frozenset(out)


def advance_generation(route: dict) -> int:
    return int(route.get("advance_generation") or 0)


def advance_slug(route_id: str, node_id: str, generation: int) -> str:
    digest = hashlib.sha256(
        f"{route_id}\0{node_id}\0{generation}".encode("utf-8")
    ).hexdigest()[:24]
    return f"stage-advance-{node_id}-{digest}"


def canonical_stage_advance_id(
    *,
    source_route_id: str,
    source_route_hash: str,
    predecessor_node: str,
    predecessor_terminal_attempt_id: str,
    successor_node: str,
    gate_evidence_digest: str,
) -> str:
    identity = {
        "source_route_id": source_route_id,
        "source_route_hash": source_route_hash,
        "predecessor_node": predecessor_node,
        "predecessor_terminal_attempt_id": predecessor_terminal_attempt_id,
        "successor_node": successor_node,
        "gate_evidence_digest": gate_evidence_digest,
    }
    if any(not value for value in identity.values()):
        raise StageAdvanceError("stage-advance-identity-incomplete")
    return "sadv-" + hashlib.sha256(_canonical_bytes(identity)).hexdigest()


def _node_by_id(route: dict, node_id: str) -> dict | None:
    for node in route.get("nodes") or []:
        if node.get("id") == node_id:
            return node
    return None


def runnable_successors(route: dict, completed: FrozenSet[str], started: FrozenSet[str]) -> list:
    """§13.32.1-(2)2 corrected for the `started` exclusion (§4.1): without it,
    an already-running sibling in a fan-out reappears as the unique runnable
    node the instant its sibling finishes, producing a duplicate start."""

    nodes = route.get("nodes") or []
    out = []
    for node in nodes:
        node_id = node.get("id")
        if node_id in completed or node_id in started:
            continue
        depends_on = set(node.get("depends_on") or [])
        if depends_on <= completed:
            out.append(node)
    return out


def _has_partial_dependency(route: dict, completed: FrozenSet[str], started: FrozenSet[str]) -> bool:
    for node in route.get("nodes") or []:
        node_id = node.get("id")
        if node_id in completed or node_id in started:
            continue
        depends_on = set(node.get("depends_on") or [])
        if depends_on and depends_on & completed and not depends_on <= completed:
            return True
    return False


def render_stage_brief(route: dict, node: dict) -> tuple:
    """`stage_brief_template_v1` — reads ONLY route capability/mode/intensity
    and the node's unit/inputs/outputs/write_scope/completion_gate. Never
    creative prose. Returns (text, digest)."""

    template = (
        "capability: {capability}\n"
        "capability_mode: {capability_mode}\n"
        "intensity: {intensity}\n"
        "unit: {unit}\n"
        "inputs: {inputs}\n"
        "outputs: {outputs}\n"
        "write_scope: {write_scope}\n"
        "completion_gate: {completion_gate}\n"
    ).format(
        capability=route.get("capability"),
        capability_mode=route.get("capability_mode"),
        intensity=route.get("effective_intensity"),
        unit=node.get("unit"),
        inputs=",".join(node.get("inputs") or []),
        outputs=",".join(node.get("outputs") or []),
        write_scope=",".join(node.get("write_scope") or []),
        completion_gate=node.get("completion_gate"),
    )
    digest = "sha256:" + hashlib.sha256(
        (BRIEF_TEMPLATE_ID + "\n" + template).encode("utf-8")
    ).hexdigest()
    return template, digest


def classify_boundary(
    route: dict,
    request: StageAdvanceRequest,
    rows: list,
    completed: FrozenSet[str],
    started: FrozenSet[str],
) -> str | None:
    """Pure structural classification — no gate evidence, no side effects.

    Returns one of the structural `REFUSAL_REASONS` members, or `None` for an
    eligible-linear boundary that may proceed to gate-evidence checks.
    """

    predecessor = _node_by_id(route, request.predecessor_node)
    if predecessor is None:
        return "stage-advance-successor-ambiguous"

    completed_after = frozenset(completed | {request.predecessor_node})
    runnable = runnable_successors(route, completed_after, started)

    if len(runnable) == 0:
        if _has_partial_dependency(route, completed_after, started):
            return "stage-advance-successor-blocked"
        return "stage-advance-successor-ambiguous"
    if len(runnable) >= 2:
        return "stage-advance-successor-ambiguous"

    successor = runnable[0]

    # A-8: terminal-node is a pure fact about the successor and is decided
    # before anything else structural, including tuple-unsealed — a terminal
    # successor can never be runtime-advanced regardless of its own sealed
    # fields (recorded priority deviation from REFUSAL_REASONS array order).
    if successor.get("terminal") is True:
        return "stage-advance-terminal-node"

    if predecessor.get("parallel_group") or successor.get("parallel_group"):
        return "stage-advance-parallel-group-boundary"

    # A-7: an auxiliary-bearing group's arbiter (node arbiter or owner-merge)
    # is never a plain linear successor -- `owner_merge_auxiliary_groups`
    # (`capability-route.py:2513`) and `_auxiliary_groups_arbitrated_by`
    # (`:2534`) are the SAME predicates the completion gate itself uses, reused
    # rather than re-derived (checklist A-7). A fan-in successor that is not
    # such an arbiter (the synthetic A-2 join) is deliberately NOT refused
    # here -- `runnable_successors`'s `depends_on <= completed` is the whole
    # eligibility test for an ordinary multi-dependency join.
    if successor.get("leg_class") == "auxiliary":
        return "stage-advance-arbiter-declared"
    node_groups, _required = ROUTE._auxiliary_groups_arbitrated_by(route, successor.get("id"))
    if node_groups:
        return "stage-advance-arbiter-declared"

    if successor.get("advance_class") != "runtime-eligible":
        return "stage-advance-tuple-unsealed"

    if predecessor.get("commit_expected") is True:
        return "stage-advance-commit-expected"

    # `claim-conflict` is NOT decided here: at this point `stage_advance_id`
    # (which needs the gate-evidence digest) is not known yet, so a
    # pre-check on `claim_key` alone cannot distinguish "my own replay" from
    # "someone else's conflicting claim". `dispatch_contract.claim_stage_advance`
    # already makes that distinction correctly inside the registry lock;
    # `coordinate_stage_advance` maps its conflict exception to this reason.
    del rows

    return None


def stage_advance_record(
    *,
    stage_advance_id: str,
    route: dict,
    request: StageAdvanceRequest,
    successor_node: str | None,
) -> dict:
    return {
        "schema_version": STAGE_ADVANCE_RECORD_SCHEMA_VERSION,
        "stage_advance_id": stage_advance_id,
        "route_id": route.get("route_id"),
        "route_hash": route.get("route_hash"),
        "predecessor_node": request.predecessor_node,
        "predecessor_terminal_attempt_id": request.predecessor_terminal_attempt_id,
        "successor_node": successor_node,
        "successor_attempt_id": None,
        "claim_key": None,
        "brief_template_digest": "",
        "registered": False,
        "started": False,
        "child_spawned": False,
        "phases": {},
        "outcome": None,
        "reason": "",
    }


def _sync_flat_fields(record: dict) -> None:
    """checklist 2.13: mirror the plan's flat `stage_advance_record_v1` field
    set onto the phase-keyed durable record on every commit, so a reader that
    only knows the sealed flat shape (block 3/4/6) never has to parse
    `phases`."""

    phases = record.get("phases") or {}
    intent = phases.get("intent") or {}
    registered = phases.get("registered") or {}
    started_phase = phases.get("started") or {}
    claim = registered.get("claim") or {}
    started_result = started_phase.get("result") or {}
    record["brief_template_digest"] = intent.get("brief_template_digest", "")
    record["claim_key"] = claim.get("claim_key") or intent.get("claim_key")
    record["successor_attempt_id"] = claim.get("successor_attempt_id")
    record["registered"] = "registered" in phases
    # `started` mirrors `child_spawned`, not phase presence -- a sealed
    # `stage-advance-successor-start-failed` record has a `started` phase key
    # (the attempt was made) but the closed-reason table requires `start 0`.
    record["child_spawned"] = bool(started_result.get("child_spawned"))
    record["started"] = record["child_spawned"]


# --------------------------------------------------------------------------
# census — committed pure function over the recipe registry (§3)
# --------------------------------------------------------------------------


def _group_realized(group: dict, intensity: str, order: dict) -> bool:
    min_intensity = group.get("min_intensity")
    width_by = group.get("width_by_intensity") or {}
    if min_intensity is None or min_intensity not in order:
        return False
    if order.get(intensity, -1) < order[min_intensity]:
        return False
    return int(width_by.get(intensity, 1)) >= 2


def census(registry: dict, intensity: str) -> dict:
    """§3.1: pure function of `capabilities/topologies.json` and an intensity.
    Filter order is sealed: base -> continuation-kind exclusion -> realized
    parallel-group-anchor exclusion -> fan-out/fan-in exclusion -> non-terminal
    -> commit-expected-excluded -> runtime-advanced."""

    order = {name: index for index, name in enumerate(registry["intensities"])}
    base = 0
    eligible = 0
    non_terminal = 0
    commit_excluded = 0
    runtime_advanced = 0
    boundaries = []

    for recipe in registry["recipes"]:
        nodes = recipe["standard_plus"]["nodes"]
        groups = recipe["standard_plus"].get("parallel_groups") or []
        realized_group_nodes = {
            group["node"] for group in groups if _group_realized(group, intensity, order)
        }
        successor_counts: dict = {}
        for node in nodes:
            for dep in node.get("depends_on") or []:
                successor_counts[dep] = successor_counts.get(dep, 0) + 1

        for i in range(len(nodes) - 1):
            predecessor = nodes[i]
            successor = nodes[i + 1]
            base += 1

            continuation_kind = (predecessor.get("continuation") or {}).get("kind")
            if continuation_kind in ("human-gate", "supervised", "monitor"):
                continue
            if predecessor["id"] in realized_group_nodes or successor["id"] in realized_group_nodes:
                continue
            if len(successor.get("depends_on") or []) > 1:
                continue
            if successor_counts.get(predecessor["id"], 0) >= 2:
                continue
            eligible += 1

            if successor.get("terminal") is True:
                continue
            non_terminal += 1

            if predecessor.get("commit_expected") is True:
                continue
            commit_excluded += 1

            if successor.get("advance_class") != "runtime-eligible":
                continue
            runtime_advanced += 1
            boundaries.append(
                {
                    "recipe": recipe["capability"],
                    "predecessor": predecessor["id"],
                    "successor": successor["id"],
                }
            )

    return {
        "base": base,
        "eligible": eligible,
        "non_terminal": non_terminal,
        "commit_expected_excluded": commit_excluded,
        "runtime_advanced": runtime_advanced,
        "boundaries": boundaries,
    }


# --------------------------------------------------------------------------
# Transaction core
# --------------------------------------------------------------------------


def _load_record(path: Path, seed: dict) -> dict:
    if not path.is_file():
        return seed
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StageAdvanceError("stage-advance-record-invalid", str(exc)) from exc
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != STAGE_ADVANCE_RECORD_SCHEMA_VERSION
        or record.get("stage_advance_id") != seed["stage_advance_id"]
    ):
        raise StageAdvanceError("stage-advance-record-binding-mismatch")
    return record


def _refused(
    reason: str,
    *,
    stage_advance_id: str = "",
    successor_node: str | None = None,
    gate_closed: bool = False,
    registered: bool = False,
    record_path: Path | None = None,
) -> StageAdvanceResult:
    return StageAdvanceResult(
        outcome="refused",
        reason=reason,
        stage_advance_id=stage_advance_id,
        successor_node=successor_node,
        successor_attempt_id=None,
        claim_key=None,
        brief_template_digest="",
        gate_closed=gate_closed,
        registered=registered,
        started=False,
        child_spawned=False,
        record_path=record_path,
    )


def coordinate_stage_advance(
    request: StageAdvanceRequest,
    services: StageAdvanceServices,
    *,
    checkpoint: Callable[[str], None] | None = None,
) -> StageAdvanceResult:
    def cp(name: str) -> None:
        if checkpoint is not None:
            checkpoint(name)

    # §13.32.1-(2)7: advance runs only in `parked` phase, owned open children
    # 0. `running-turn` is refused unconditionally -- "running-turn에서는
    # 절대 실행하지 않는다" -- never as a function of the (caller-supplied)
    # delivered-open set.
    if request.supervisor_phase != "parked":
        return _refused("stage-advance-phase-ineligible")
    if request.delivered_open_attempt_ids:
        return _refused("stage-advance-phase-ineligible")
    if request.receipt_schema_negotiated != 3:
        # §13.32.1-(2)6: advance requires an ACTUAL v3 negotiation, not merely a
        # legal schema value. `2` is the ordinary un-negotiated case (block 3's
        # default) and refuses exactly like any other unsupported value -- the
        # caller never learns "close but no cigar", it is simply refused
        # (checklist 4's flag-default requirement, block 4).
        return _refused("stage-advance-receipt-schema-unsupported")

    try:
        route = _load_and_verify_route(request.route_file)
    except StageAdvanceError as exc:
        return _refused(exc.reason)

    route_id = route.get("route_id")
    completed = completed_nodes(request.jobs, route_id)
    generation = advance_generation(route)
    started = started_nodes(request.jobs, route_id, route.get("route_hash"))
    rows = _stage_advance_rows(request.jobs)

    structural = classify_boundary(route, request, rows, completed, started)
    if structural is not None and structural != "stage-advance-commit-expected":
        return _refused(structural)

    completed_after = frozenset(completed | {request.predecessor_node})
    successor_node_dict = runnable_successors(route, completed_after, started)[0]
    successor_id = successor_node_dict["id"]

    evidence, evidence_reason = gate_evidence(request)
    if evidence is None:
        mapped = (
            "stage-advance-evidence-unreadable"
            if evidence_reason in _EVIDENCE_UNREADABLE_REASONS
            else "stage-advance-gate-unproven"
        )
        return _refused(mapped, successor_node=successor_id)

    gate_evidence_digest = "sha256:" + hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    stage_advance_id = canonical_stage_advance_id(
        source_route_id=route_id,
        source_route_hash=route.get("route_hash"),
        predecessor_node=request.predecessor_node,
        predecessor_terminal_attempt_id=request.predecessor_terminal_attempt_id,
        successor_node=successor_id,
        gate_evidence_digest=gate_evidence_digest,
    )
    with _stage_advance_transaction_lock(request.jobs, stage_advance_id):
        record_path = stage_advance_record_path(request.jobs, stage_advance_id)
        seed = stage_advance_record(
            stage_advance_id=stage_advance_id,
            route=route,
            request=request,
            successor_node=successor_id,
        )
        record = _load_record(record_path, seed)

        if record.get("outcome") is not None:
            return _result_from_record(record, record_path)

        template, brief_digest = render_stage_brief(route, successor_node_dict)
        generation = advance_generation(route)
        claim_key = (route.get("route_hash"), successor_id, generation)

        if "gate_closed" not in record["phases"]:
            cp("before-gate-close")
            gate_result = services.close_gate(
                request,
                node=request.predecessor_node,
                terminal_attempt_id=request.predecessor_terminal_attempt_id,
                artifact=evidence,
            )
            record["phases"]["gate_closed"] = {
                "committed_at_ns": time.time_ns(),
                "evidence": gate_result,
            }
            _sync_flat_fields(record)
            _atomic_json(record_path, record)

        if structural == "stage-advance-commit-expected":
            record["outcome"] = "refused"
            record["reason"] = "stage-advance-commit-expected"
            _sync_flat_fields(record)
            _atomic_json(record_path, record)
            return _refused(
                "stage-advance-commit-expected",
                stage_advance_id=stage_advance_id,
                successor_node=successor_id,
                gate_closed=True,
                record_path=record_path,
            )

        if "intent" not in record["phases"]:
            cp("before-intent")
            record["phases"]["intent"] = {
                "committed_at_ns": time.time_ns(),
                "claim_key": list(claim_key),
                "successor_node": successor_id,
                "brief_template_digest": brief_digest,
            }
            _sync_flat_fields(record)
            _atomic_json(record_path, record)

        if "registered" not in record["phases"]:
            cp("before-register")
            try:
                claim = services.claim(
                    request,
                    stage_advance_id=stage_advance_id,
                    claim_key=claim_key,
                    successor_node=successor_id,
                )
            except DispatchContractError as exc:
                if exc.reason == "stage-advance-claim-conflict":
                    return _refused(
                        "stage-advance-claim-conflict",
                        stage_advance_id=stage_advance_id,
                        successor_node=successor_id,
                        gate_closed=True,
                        record_path=record_path,
                    )
                raise
            record["phases"]["registered"] = {
                "committed_at_ns": time.time_ns(),
                "claim": {
                    "stage_advance_id": claim.stage_advance_id,
                    "claim_key": list(claim.claim_key),
                    "successor_attempt_id": claim.successor_attempt_id,
                    "replayed": claim.replayed,
                },
            }
            _sync_flat_fields(record)
            _atomic_json(record_path, record)
        else:
            claim_evidence = record["phases"]["registered"]["claim"]
            claim = StageAdvanceClaim(
                stage_advance_id=claim_evidence["stage_advance_id"],
                claim_key=tuple(claim_evidence["claim_key"]),
                successor_attempt_id=claim_evidence["successor_attempt_id"],
                replayed=True,
            )

        if "started" not in record["phases"]:
            cp("before-start")
            prompt_dir = record_path.parent / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            prompt_file = prompt_dir / f"{stage_advance_id}.md"
            prompt_file.write_text(template, encoding="utf-8")
            slug = advance_slug(route_id, successor_id, generation)
            try:
                start_result = services.start_successor(
                    request,
                    claim=claim,
                    successor=successor_node_dict,
                    slug=slug,
                    prompt_file=prompt_file,
                )
            except StageAdvanceError as exc:
                # §13.32.1-(3)D: gate close succeeded, successor start failed is a
                # normal typed re-park point -- marker 1, start 0 -- never an
                # uncaught crash that would deny the model its resume turn.
                #
                # T3 correction (round-1 blocking finding 3): every prior
                # nonzero `start_successor` outcome collapsed to the single
                # generic `stage-advance-successor-start-failed` reason here,
                # regardless of the typed reason the exception actually
                # carried -- erasing the three distinct refusal-table rows
                # (`launch-compatibility-mismatch`, `harness-unavailable`,
                # `lifecycle-unsupported`) `RealStageAdvanceServices.
                # start_successor` (or a test-injected services boundary) had
                # already classified. Use the exception's own typed reason;
                # only an unrecognized reason (a services boundary bug, not a
                # real wrapper failure) falls back to the generic one.
                reason = exc.reason if exc.reason in REFUSAL_REASONS else (
                    "stage-advance-successor-start-failed"
                )
                record["phases"]["started"] = {
                    "committed_at_ns": time.time_ns(),
                    "result": {"child_spawned": False, "reason": reason},
                }
                record["outcome"] = "refused"
                record["reason"] = reason
                _sync_flat_fields(record)
                _atomic_json(record_path, record)
                cp("after-start")
                return _refused(
                    reason,
                    stage_advance_id=stage_advance_id,
                    successor_node=successor_id,
                    gate_closed=True,
                    registered=True,
                    record_path=record_path,
                )
            record["phases"]["started"] = {
                "committed_at_ns": time.time_ns(),
                "result": start_result,
            }
            record["outcome"] = "advanced"
            record["reason"] = ""
            _sync_flat_fields(record)
            _atomic_json(record_path, record)
            # F-2 / A-17: this is the one place a runtime-eligible advance's
            # success actually becomes true. The predecessor row already
            # carries an ordinary delivery intent (stamped before this was
            # known) -- supersede it now so §13.33.1-(8)'s "no model
            # delivery" holds for the eligible-linear success path. The
            # advance result above is already committed to disk by this
            # point; F-6: `supersede_pending_delivery_for_advance` is itself
            # total (never raises), so this call can only ever add a
            # best-effort cleanup outcome, never take back the advance.
            JOIN.supersede_pending_delivery_for_advance(
                request.jobs, request.predecessor_terminal_attempt_id
            )
            cp("after-start")

        return _result_from_record(record, record_path)


def _result_from_record(record: dict, record_path: Path) -> StageAdvanceResult:
    phases = record.get("phases") or {}
    gate_closed = "gate_closed" in phases
    registered = "registered" in phases
    started_phase = phases.get("started")
    started_result = (started_phase or {}).get("result") or {}
    # `started` mirrors `child_spawned`, not phase presence: a replayed
    # `stage-advance-successor-start-failed` record still has a `started`
    # phase key (the attempt was made) but no process (checklist A-13/D).
    started = bool(started_result.get("child_spawned"))
    claim_evidence = (phases.get("registered") or {}).get("claim") or {}
    intent = phases.get("intent") or {}
    return StageAdvanceResult(
        outcome=record.get("outcome") or "refused",
        reason=record.get("reason") or "",
        stage_advance_id=record.get("stage_advance_id"),
        successor_node=record.get("successor_node"),
        successor_attempt_id=claim_evidence.get("successor_attempt_id"),
        claim_key=tuple(claim_evidence["claim_key"]) if claim_evidence.get("claim_key") else (
            tuple(intent["claim_key"]) if intent.get("claim_key") else None
        ),
        brief_template_digest=intent.get("brief_template_digest", ""),
        gate_closed=gate_closed,
        registered=registered,
        started=started,
        child_spawned=bool(started_result.get("child_spawned")),
        record_path=record_path,
    )


def _stage_advance_rows(jobs: Path) -> list:
    directory = jobs.parent / "stage_advance"
    if not directory.is_dir():
        return []
    rows = []
    for path in directory.glob("sadv-*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        intent = (record.get("phases") or {}).get("intent") or {}
        claim_key = intent.get("claim_key")
        rows.append(
            {
                "stage_advance_id": record.get("stage_advance_id"),
                "claim_key": tuple(claim_key) if claim_key else None,
            }
        )
    return rows


_EVIDENCE_UNREADABLE_REASONS = frozenset(
    {"evidence-not-readable", "evidence-undecodable", "evidence-absent"}
)


def process_quiescence(request: StageAdvanceRequest, *, attempt_id: str) -> bool:
    """Reference `StageAdvanceServices.process_quiescence` realization:
    delegates to `dispatch_contract.attempt_process_quiescence` (`:1884`).
    Never pid+starttime liveness (checklist 2.8) -- a real service
    implementation (block 4) calls this, tests inject their own fake."""

    import dispatch_contract as DC

    metadata = _predecessor_metadata(request.jobs, attempt_id)
    if not metadata:
        return False
    return DC.attempt_process_quiescence(metadata).state == "quiescent"


def _predecessor_metadata(jobs: Path, attempt_id: str) -> dict:
    if not jobs.is_file():
        return {}
    for line in jobs.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
        if metadata.get("attempt_id") == attempt_id:
            return metadata
    return {}


def gate_evidence(request: StageAdvanceRequest) -> tuple:
    """Shared with `close_finished_child`/`dispatch-harvest --mark-done`
    (checklist 2.7): the identical fail-closed predicate, never a second one.
    A thin, monkeypatch-friendly seam so P-tier fixtures can inject
    `route_completion_evidence`'s return value without a real registry file.
    """

    metadata = _predecessor_metadata(request.jobs, request.predecessor_terminal_attempt_id)
    if not metadata:
        return None, "evidence-not-valid"
    artifact, reason = JOIN.route_completion_evidence(metadata, worktree=request.worktree)
    return artifact, reason


def _load_and_verify_route(route_file: Path) -> dict:
    """Load the route and prove its own tamper-evidence (route hash / id).

    Full registry-currentness verification (`ROUTE.verify_route` without
    `allow_stale_registry`) is the launching supervisor's concern before it
    ever calls into this module; here we only refuse a route whose own hash
    no longer matches its payload (A-12: route hash / marker digest / row
    revision / SD-107 tuple tampering all typed-refuse, never silently
    proceed)."""

    route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    if route.get("route_hash") != ROUTE.route_hash(route):
        raise StageAdvanceError("stage-advance-launch-compatibility-mismatch")
    if route.get("route_id") != "rt-" + str(route.get("route_hash", "")).split(":", 1)[-1][:16]:
        raise StageAdvanceError("stage-advance-launch-compatibility-mismatch")
    return route


# --------------------------------------------------------------------------
# Block 4 -- real subprocess-backed services (the ONLY implementation that
# mutates the registry or spawns a successor; the block 2/3 test suites use
# only `FakeServices`). Never `git`. Never a precomputed `--adapter`
# (R-4, §13.32.1-(3)A) -- `start_successor` execs
# `utilities/stage-dispatch-fallback.py --start` and lets ITS OWN checked
# wrapper selector, launch fence, governor reservation, and SD-107
# revalidation run unmodified underneath.
# --------------------------------------------------------------------------


# T3 correction (round-1 blocking finding 3): `stage-dispatch-fallback.py
# --start`'s own typed `reason=` vocabulary, not a parallel one invented
# here. `launch-runtime-root-mismatch` / `launch-compatibility-tuple-required`
# are its SD-107 launch-compatibility-tuple revalidation failures (the ONLY
# other source was the local self-hash check in `_load_and_verify_route`,
# which never observes a REAL wrapper failure). `fallback-chain-exhausted` is
# what the wrapper emits once every candidate hop has been skipped as
# unsupported/failed/usage-limited -- "harness-unavailable" in the refusal
# table's words. `unsupported-native-execution-surface` is the wrapper
# refusing to service a launch lifecycle its own launch fence cannot host.
_WRAPPER_LAUNCH_COMPATIBILITY_MISMATCH_REASONS = frozenset({
    "launch-runtime-root-mismatch",
    "launch-compatibility-tuple-required",
})
_WRAPPER_HARNESS_UNAVAILABLE_REASONS = frozenset({
    "fallback-chain-exhausted",
})
_WRAPPER_LIFECYCLE_UNSUPPORTED_REASONS = frozenset({
    "unsupported-native-execution-surface",
})


def _classify_wrapper_start_failure(reason: str) -> str:
    """Map one `stage-dispatch-fallback.py --start` typed `reason=` value to
    its §13.32.1-(4) refusal row. Anything not in the closed sets above falls
    back to the generic `stage-advance-successor-start-failed` -- the same
    outcome every wrapper failure used to collapse to unconditionally."""

    if reason in _WRAPPER_LAUNCH_COMPATIBILITY_MISMATCH_REASONS:
        return "stage-advance-launch-compatibility-mismatch"
    if reason in _WRAPPER_HARNESS_UNAVAILABLE_REASONS:
        return "stage-advance-harness-unavailable"
    if reason in _WRAPPER_LIFECYCLE_UNSUPPORTED_REASONS:
        return "stage-advance-lifecycle-unsupported"
    return "stage-advance-successor-start-failed"


class RealStageAdvanceServices:
    """Checked subprocess boundary. `close_gate` calls
    `capability-route.py complete` (the identical command
    `dispatch_completion_join.close_finished_child` already issues for the
    existing SD-78 non-model gate-close path, reusing its bounded-retry seam
    `run_route_completion`). `claim` delegates to
    `dispatch_contract.claim_stage_advance`. `start_successor` execs
    `stage-dispatch-fallback.py --start` in the same argument shape the model
    uses."""

    def __init__(self) -> None:
        self._capability_route_script = ROOT / "utilities" / "capability-route.py"
        self._fallback_script = ROOT / "utilities" / "stage-dispatch-fallback.py"

    def close_gate(
        self,
        request: StageAdvanceRequest,
        *,
        node: str,
        terminal_attempt_id: str,
        artifact: str,
    ) -> dict:
        metadata = _predecessor_metadata(request.jobs, terminal_attempt_id)
        command = [
            sys.executable,
            str(self._capability_route_script),
            "complete",
            "--route", str(request.route_file),
            "--node", node,
            "--evidence", artifact,
            "--jobs", str(request.jobs),
            "--attempt-id", terminal_attempt_id,
        ]
        for flag, key in (
            ("--dispatch-depth", "dispatch_depth"),
            ("--transport", "transport"),
            ("--execution-surface", "execution_surface"),
            ("--registered-worker", "registered_worker"),
            ("--fallback-hop", "fallback_hop"),
        ):
            value = metadata.get(key)
            if value:
                command += [flag, str(value)]
        reason = JOIN.run_route_completion(command)
        if reason:
            # `complete` is exact-attempt idempotent -- the same one bounded
            # retry `close_finished_child` uses recovers the
            # marker-written/row-not-yet-closed publication window
            # (dispatch_completion_join.py close_finished_child).
            reason = JOIN.run_route_completion(command)
        if reason:
            raise StageAdvanceError("stage-advance-gate-unproven", reason)
        return {"reason": ""}

    def claim(
        self,
        request: StageAdvanceRequest,
        *,
        stage_advance_id: str,
        claim_key: tuple,
        successor_node: str,
    ) -> StageAdvanceClaim:
        route = json.loads(Path(request.route_file).read_text(encoding="utf-8"))
        route_hash, _successor, generation = claim_key
        registry_claim = DC.claim_stage_advance(
            request.jobs,
            stage_advance_id=stage_advance_id,
            route_hash=route_hash,
            successor_node=successor_node,
            advance_generation=generation,
            source_route_id=route.get("route_id"),
            predecessor_attempt_id=request.predecessor_terminal_attempt_id,
        )
        return StageAdvanceClaim(
            stage_advance_id=registry_claim.stage_advance_id,
            claim_key=registry_claim.claim_key,
            successor_attempt_id=registry_claim.successor_attempt_id,
            replayed=registry_claim.replayed,
        )

    def start_successor(
        self,
        request: StageAdvanceRequest,
        *,
        claim: StageAdvanceClaim,
        successor: dict,
        slug: str,
        prompt_file: Path,
    ) -> dict:
        route = json.loads(Path(request.route_file).read_text(encoding="utf-8"))
        unit = successor.get("unit") or ""
        model_role = FALLBACK._unit_role(unit) or successor.get("role", "fast implementer")
        jobs_path = str(Path(request.jobs).expanduser().resolve())
        command = [
            sys.executable,
            str(self._fallback_script),
            "--route", str(request.route_file),
            "--node", successor["id"],
            "--slug", slug,
            "--parent", request.parent_attempt_id,
            "--capability-mode", route.get("capability_mode") or "",
            "--worker-mode", unit,
            "--qa", "standard",
            "--model-role", model_role,
            "--prompt-file", str(prompt_file),
            "--jobs", jobs_path,
            "--start",
        ]
        try:
            completed = subprocess.run(
                command, text=True, capture_output=True, timeout=180.0, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StageAdvanceError(
                "stage-advance-successor-start-failed", f"{type(exc).__name__}"
            ) from exc
        fields = FALLBACK.output_fields((completed.stdout or "") + (completed.stderr or ""))
        if completed.returncode != 0 or fields.get("check") == "failed":
            wrapper_reason = fields.get("reason") or ""
            raise StageAdvanceError(
                _classify_wrapper_start_failure(wrapper_reason),
                wrapper_reason or (completed.stderr or completed.stdout or "")[:200],
            )
        return {
            "child_spawned": fields.get("child_spawned", "1") != "0",
            "attempt_id": fields.get("attempt_id") or claim.successor_attempt_id,
            "raw": fields,
        }

    def observe_record(
        self, request: StageAdvanceRequest, *, stage_advance_id: str
    ) -> dict | None:
        path = stage_advance_record_path(request.jobs, stage_advance_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def process_quiescence(
        self, request: StageAdvanceRequest, *, attempt_id: str
    ) -> bool:
        return process_quiescence(request, attempt_id=attempt_id)


def stage_advance_event_fields(
    *,
    route_hash: str,
    predecessor_node: str,
    result: "StageAdvanceResult",
    last_child_terminal_ns: int | None = None,
    join_completed_ns: int | None = None,
    next_stage_start_ns: int | None = None,
) -> dict:
    """Pure projection shared by both supervisors for
    `dispatch.supervisor.stage-advance{,-refused}` (§13.32.1-(5), block 6).
    `event_id` is the `stage_advance_id` when one was computed; the earliest
    structural refusals (phase-ineligible, receipt-schema-unsupported, a
    route that fails its own tamper check) never reach that point, so a
    stable fallback digest over the boundary identity keeps every refusal
    reason emittable with a stable id.

    On the advance path there is no model turn, so `same_thread_resume_ns`
    and `exact_harvest_ns` stay explicitly `null` -- only
    `last_child_terminal_ns -> join_completed_ns -> next_stage_start_ns` are
    filled (§13.32.1-(5), checklist 6.1). `delivery_timing` schema v1 does not
    change: `delivery_timing_fields` already accepts a partial point set and
    the existing monotonicity check already skips `None`. Timing is only
    attached when the caller supplies a `next_stage_start_ns` (i.e. an
    advanced outcome) -- a refusal never had a "next stage" to time."""

    event_id = result.stage_advance_id or (
        "sadv-refused-"
        + hashlib.sha256(
            f"{route_hash}\0{predecessor_node}\0{result.reason}".encode("utf-8")
        ).hexdigest()[:32]
    )
    event = {
        "type": (
            "dispatch.supervisor.stage-advance"
            if result.outcome == "advanced"
            else "dispatch.supervisor.stage-advance-refused"
        ),
        "event_id": event_id,
        "advance_mode": "runtime-deterministic",
        "route_hash": route_hash,
        "predecessor_node": predecessor_node,
        "successor_node": result.successor_node,
        "outcome": result.outcome,
        "reason": result.reason,
    }
    if next_stage_start_ns is not None:
        event["delivery_timing"] = JOIN.delivery_timing_fields(
            last_child_terminal_ns=last_child_terminal_ns,
            join_completed_ns=join_completed_ns,
            next_stage_start_ns=next_stage_start_ns,
        )
    return event
