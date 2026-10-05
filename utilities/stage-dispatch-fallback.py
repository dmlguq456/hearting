#!/usr/bin/env python3
"""Execute a checked dispatch-contract-v3 fallback for one route node."""

from __future__ import annotations


import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from model_config import ModelConfigError, resolve_config  # noqa: E402
from model_profile import sealed_pin_harness  # noqa: E402


def _unit_role(unit):
    """Model-role authority follows the CHOSEN unit's frontmatter (2026-07-22 verify
    finding: with unit_choices, the node role is only the default — e.g. a
    fast-fact-checker claim-verify choice under a fast-reviewer node must resolve
    its own role). Stdlib-only; returns None for absent/reserved/malformed units."""
    if not unit or unit.startswith("_kernel/"):
        return None
    path = ROOT / "roles" / "units" / f"{unit}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    import re as _re
    m = _re.search(r"^role:\s*(.+?)\s*$", text.split("---", 2)[1], _re.MULTILINE) if text.startswith("---") else None
    return m.group(1) if m else None

from dispatch_lifecycle import (  # noqa: E402
    DETACHED,
    FOREGROUND_INTERRUPTED,
    FOREGROUND_NOTICE,
    FOREGROUND_SCOPED,
    FOREGROUND_TIMEOUT_DEFAULT,
    bounded_foreground_timeout,
    run_forwarding_termination,
    select_launch_lifecycle,
)
# Spawn-confirm window: how long a `--start` call may synchronously watch a
# healthy child before it owes its caller a launch receipt.
DIRECT_TIMEOUT_DEFAULT = 45.0

from dispatch_contract import (  # noqa: E402
    PRELAUNCH_PROCESS_BLOCK_REASONS,
    ROUTE_STATE_REFUSAL_REASONS,
    DispatchContractError,
    attempt_process_quiescence,
    parse_registry_metadata,
    resolve_agent_home,
    resolve_dispatch_state_root,
    resolve_global_registry,
    resolve_live_parent_attempt,
    route_state_next_action,
    validate_attempt_metadata,
)

_NODE_SPEC = importlib.util.spec_from_file_location(
    "dispatch_node", ROOT / "utilities" / "dispatch-node.py"
)
if _NODE_SPEC is None or _NODE_SPEC.loader is None:  # pragma: no cover - install corruption
    raise RuntimeError("dispatch-node loader unavailable")
DISPATCH_NODE = importlib.util.module_from_spec(_NODE_SPEC)
_NODE_SPEC.loader.exec_module(DISPATCH_NODE)
from dispatch_mode_contract import (  # noqa: E402
    DispatchModeContractError,
    normalize_dispatch_modes,
    validate_route_mode_axes,
)
from worker_bootstrap import assigned_contract, worker_type_for_kind  # noqa: E402
from dispatch_attempt_policy import decide_attempt, committed_outcome
from codex_dispatch_terminal import REVIEW_BLOCKING_NOTE  # noqa: E402
from review_round_cap import classify_round_row  # noqa: E402
from dispatch_degradation import record_degradation  # noqa: E402
from dispatch_allocation_receipt import record_allocation_receipt  # noqa: E402
from dispatch_allocation import inert_allocation_keys  # noqa: E402
import dispatch_launch_tuple as LAUNCH_TUPLE  # noqa: E402
from dispatch_quality_peer import quality_peer_families  # noqa: E402
from dispatch_allocation import (  # noqa: E402
    HARNESSES as ALLOCATION_HARNESSES,
    STRATEGY as ALLOCATION_STRATEGY,
    attempt_counts,
    rank_harnesses,
)

_CAPACITY_SPEC = importlib.util.spec_from_file_location(
    "harness_capacity", ROOT / "utilities" / "harness-capacity.py"
)
if _CAPACITY_SPEC is None or _CAPACITY_SPEC.loader is None:
    raise RuntimeError("cannot load harness-capacity.py")
CAPACITY = importlib.util.module_from_spec(_CAPACITY_SPEC)
_CAPACITY_SPEC.loader.exec_module(CAPACITY)

_EXCLUSION_SPEC = importlib.util.spec_from_file_location(
    "dispatch_harness_exclusion", ROOT / "utilities" / "dispatch_harness_exclusion.py"
)
if _EXCLUSION_SPEC is None or _EXCLUSION_SPEC.loader is None:
    raise RuntimeError("cannot load dispatch_harness_exclusion.py")
EXCLUSION = importlib.util.module_from_spec(_EXCLUSION_SPEC)
_EXCLUSION_SPEC.loader.exec_module(EXCLUSION)

ORDER = ["same-harness-headless", "cross-harness-headless", "native-subagent", "inline"]


def outer_subprocess_timeout(
    foreground_timeout: float, lifecycle: str, direct_timeout: float
) -> float | None:
    """Wall-clock deadline for the OUTER subprocess.run hosting a wrapper launch.

    Detached launches return immediately, so the outer call only needs the short
    spawn-confirm window (``direct_timeout``). A foreground-scoped wrapper instead
    stays attached and self-bounds its child via
    ``dispatch_lifecycle.wait_foreground``; the outer deadline is a small grace
    margin ABOVE that internal deadline so the wrapper can run its own
    SIGTERM->SIGKILL cleanup and record a clean terminal row.

    Boundary hazard this centralizes: ``foreground_timeout <= 0`` was the wrapper's
    "disable timeout / wait indefinitely" sentinel, and a flat ``+ 10`` collapsed
    the outer wall to 10s — the SHORTEST deadline, the inverse of "indefinite" —
    abandoning a child that had only just started. Because a foreground-scoped
    parent blocks on its child, indefinite is never safe here, so the non-positive
    sentinel is clamped to a finite ceiling via ``bounded_foreground_timeout``
    (mirrored inside ``wait_foreground`` so the wrapper self-bounds too). A
    no-progress watchdog that tells slow-but-progressing from wedged is the planned
    follow-up. Boundary coverage: utilities/dispatch_liveness_matrix.test.py.
    """
    if lifecycle != FOREGROUND_SCOPED:
        return direct_timeout
    return bounded_foreground_timeout(foreground_timeout) + 10.0


def run_wrapper(args: argparse.Namespace, command: list[str]):
    """One adapter-wrapper launch for this chain.

    A foreground-scoped ``--start`` hosts the worker inside this call, so a
    stop request (SIGINT/SIGTERM/SIGHUP) is handed to the wrapper and the
    chain waits for it to stop the worker and close the row
    (``run_forwarding_termination``). Every other launch returns quickly and
    keeps the plain ``subprocess.run``.
    """
    lifecycle = getattr(args, "launch_lifecycle", DETACHED)
    timeout = outer_subprocess_timeout(
        getattr(args, "foreground_timeout", FOREGROUND_TIMEOUT_DEFAULT),
        lifecycle,
        args.direct_timeout,
    )
    if getattr(args, "action", None) == "start" and lifecycle == FOREGROUND_SCOPED:
        return run_forwarding_termination(
            command, cwd=ROOT, env=direct_env(), capture=True, timeout=timeout,
        )
    return subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False,
        timeout=timeout, env=direct_env(),
    )


def wrapper_interrupted(result, fields: dict[str, str]) -> bool:
    """The caller stopped this call, or the wrapper reports it was stopped."""
    return (
        getattr(result, "received_signal", None) is not None
        or fields.get("worker_failure") == FOREGROUND_INTERRUPTED
    )


def report_interrupted(result, output: str, attempt_id: str, attempts: list[str]) -> int:
    """Stop the chain where the caller stopped it: no next hop is tried.

    The interrupted attempt keeps its own row (``dead-interrupted``); a later
    ``--start`` retries the same tuple through ``automatic_retry_of``.
    """
    signum = getattr(result, "received_signal", None)
    extra = {"cleanup": "incomplete"} if getattr(result, "cleanup_incomplete", False) else {}
    code = fail(
        "interrupted", 128 + int(signum) if signum else 130,
        interrupted="1", **extra, attempt_id=attempt_id,
        detail="the call was stopped, so the worker was stopped with it",
        attempt_trace="|".join(attempts),
    )
    if output:
        print(output)
    return code


def fail(reason: str, code: int, **fields: str) -> int:
    print("check=failed")
    print(f"reason={reason}")
    for key, value in fields.items():
        print(f"{key}={value}")
    return code


def output_fields(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def compact_diagnostic(output: str, limit: int = 1000) -> str:
    """Keep a bounded wrapper diagnostic on one machine-readable output line."""

    value = "\\n".join(line.strip() for line in output.splitlines() if line.strip())
    return value[:limit] or "-"


def load_node(route_path: Path, node_id: str, launch_phase: str) -> tuple[dict, dict]:
    route = json.loads(route_path.read_text(encoding="utf-8"))
    verify = subprocess.run(
        [sys.executable, str(ROOT / "utilities/capability-route.py"), "verify", "--route", str(route_path),
         "--launch-phase", launch_phase],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if verify.returncode:
        raise ValueError((verify.stderr or verify.stdout).strip())
    node = next((row for row in route.get("nodes", []) if row.get("id") == node_id), None)
    if not node:
        raise ValueError(f"unknown route node: {node_id}")
    chain = node.get("fallback_hops")
    if not isinstance(chain, list) or [row.get("fallback_hop") for row in chain] != ORDER:
        raise ValueError("route node lacks checked ordered fallback")
    return route, node


def tuple_key(row: dict) -> str:
    return "/".join(str(row[key]) for key in (
        "parent_harness", "parent_transport", "parent_sandbox", "child_harness", "launch_authority"
    ))


def _usage_states(jobs: Path, profile=None) -> dict[str, str]:
    result = subprocess.run(
        [str(ROOT / "utilities/usage-check.sh"), "--harness", "all", "--jobs", str(jobs),
         "--model-profile", profile or ""],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    states = {}
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0] in ALLOCATION_HARNESSES:
                states[fields[0]] = fields[1]
    return {
        harness: states.get(harness, "unknown")
        for harness in ALLOCATION_HARNESSES
    }


def _usage_eligible(state: str) -> bool:
    return state != "limited" and not state.startswith("limited(")


def _policy_by_profile(route, node):
    """Collect sealed per-profile harness policies for the quality-peer derivation.

    For the single-checker axis the owner policy -- keyed under `deep`: the
    standard+ owner profile is deep, and a `top` owner borrows deep's bands
    (`dispatch-defaults.EXCEPTION_PROFILE_POLICY`), so the key names the
    band, not the profile -- plus every depth-2 node's sealed `harness_policy` keyed by its
    model_profile reconstructs the config surface the quality-peer set is
    derived from (spec 13.30.2). Empty dict means no config-derived policy is
    present, which callers treat as not-applicable (D8-①).
    """
    by_profile: dict[str, object] = {}
    owner = route.get("owner_harness_policy")
    if isinstance(owner, dict):
        by_profile.setdefault("deep", owner)
    for candidate in route.get("nodes", []):
        if not isinstance(candidate, dict):
            continue
        policy = candidate.get("harness_policy")
        profile = candidate.get("model_profile")
        if isinstance(policy, dict) and isinstance(profile, str) and profile:
            by_profile.setdefault(profile, policy)
    policy = node.get("harness_policy")
    profile = node.get("model_profile")
    if isinstance(policy, dict) and isinstance(profile, str) and profile:
        by_profile.setdefault(profile, policy)
    return by_profile


HEADLESS_HOPS = frozenset({"same-harness-headless", "cross-harness-headless"})


def _owner_harness(hops: list[dict], parent_identity: dict | None) -> str | None:
    """The harness the native-subagent and inline hops would run on."""
    if parent_identity and parent_identity.get("parent_harness"):
        return parent_identity["parent_harness"]
    sealed = {
        row.get("parent_harness")
        for hop in hops if hop.get("fallback_hop") in HEADLESS_HOPS
        for row in hop.get("candidates", []) if row.get("parent_harness")
    }
    if len(sealed) == 1:
        return next(iter(sealed))
    native = {
        row.get("harness")
        for hop in hops if hop.get("fallback_hop") == "native-subagent"
        for row in hop.get("candidates", []) if row.get("harness")
    }
    return next(iter(native)) if len(native) == 1 else None


def apply_worker_pin(route: dict, hops: list[dict], parent_identity: dict | None) -> list[dict]:
    """Keep a sealed `--pin worker=<harness>` step on that harness.

    A headless candidate on another harness is marked ``_worker_pin_skip`` so
    the loop records ``skipped-worker-pin`` instead of launching it; the
    native-subagent and inline hops run on the owner's harness and stay only
    when that is the pinned one. Without a pin the hops are returned as they
    are.
    """
    pin = sealed_pin_harness(route, worker_type="stage")
    if not pin:
        return hops
    owner = _owner_harness(hops, parent_identity)
    pinned = []
    for hop in hops:
        if hop.get("fallback_hop") in HEADLESS_HOPS:
            pinned.append({**hop, "candidates": [
                row if row.get("child_harness") == pin else {**row, "_worker_pin_skip": pin}
                for row in hop.get("candidates", [])
            ]})
        elif owner == pin:
            pinned.append(hop)
        else:
            pinned.append({**hop, "_worker_pin_skip": pin})
    return pinned


def ordered_fallback_hops(
    route: dict, node: dict, jobs: Path, *, parent_identity: dict | None = None
) -> tuple[list[dict], dict | None]:
    """Rank the checked direct-headless band from the sealed allocation policy.

    Both paths end in ``apply_worker_pin``: a pinned step is never moved to
    another tool.
    """

    allocation = route.get("dispatch_allocation")
    if not isinstance(allocation, dict) or allocation.get("strategy") not in {
        ALLOCATION_STRATEGY, "capacity-aware", "balanced"
    }:
        # Without an allocation policy the chain is the compiled order and
        # every candidate at every hop is visited, so parent_runtime_failure
        # below skips a foreign row and the correct row is still reached.
        # Only the allocation path collapses the chain to one row per
        # harness (via headless.setdefault below) and therefore needs the
        # parent filter.
        return apply_worker_pin(route, list(node["fallback_hops"]), parent_identity), None
    counts = attempt_counts(jobs, window=int(allocation["window"]))
    states = _usage_states(jobs, node.get("model_profile"))
    headless: dict[str, tuple[dict, dict]] = {}
    trailing_rows: list[tuple[dict, dict]] = []
    tail_hops = []
    for hop in node["fallback_hops"]:
        if hop["fallback_hop"] not in {"same-harness-headless", "cross-harness-headless"}:
            tail_hops.append(hop)
            continue
        for row in hop.get("candidates", []):
            harness = row.get("child_harness")
            if not DISPATCH_NODE.candidate_matches_parent(row, parent_identity):
                # A foreign parent's row must not claim this harness slot. It
                # is kept in the trailing band rather than dropped, so the
                # attempt trace still records
                # skipped-dispatch-evidence-parent-runtime-mismatch exactly
                # as today.
                trailing_rows.append((hop, row))
                continue
            if row.get("status") == "supported" and harness in ALLOCATION_HARNESSES:
                # After parent filtering a child adapter appears at exactly
                # one ordinal, so setdefault's first-wins is a no-op here
                # rather than the silent deletion it used to be.
                headless.setdefault(harness, (hop, row))
            else:
                trailing_rows.append((hop, row))
    candidates = list(headless)
    eligible = [harness for harness in candidates if _usage_eligible(states[harness])]
    limited = [harness for harness in candidates if harness not in eligible]
    scores = None
    quality_band = None
    relief_promoted = False
    if allocation["strategy"] in {"capacity-aware", "balanced"} and isinstance(node.get("harness_policy"), dict):
        scores = CAPACITY.capacity_scores()
        available = set(candidates)
        policy = {
            **node["harness_policy"],
            **{
                band: [h for h in node["harness_policy"][band] if h in available]
                for band in ("primary", "relief", "last_resort")
            },
        }
        _selected, quality_band, ranks, relief_promoted = CAPACITY.select(
            policy,
            states,
            counts,
            allocation.get("harness_order") or ALLOCATION_HARNESSES,
            scores,
            strategy=allocation["strategy"],
            usage_gate_used_percent=allocation.get("usage_gate_used_percent", 90),
            preferred=CAPACITY.preferred_for_depth(allocation, int(node.get("dispatch_depth", 2))),
            affinity_weight=allocation.get("depth_affinity_weight", 0.5),
            headroom_exponent=allocation.get("usage_headroom_exponent", 1),
            harness_weights=allocation.get("harness_weights"),
        )
        band_order = ("relief", "primary", "last_resort") if relief_promoted else (
            "primary", "relief", "last_resort"
        )
        ranked = [
            name
            for _band, name in CAPACITY.ordered_candidates(
                ranks, band_order, scores,
                strategy=allocation["strategy"],
                usage_gate_used_percent=allocation.get("usage_gate_used_percent", 90),
            )
        ]
    else:
        ranked = rank_harnesses(
            eligible,
            counts,
            declared_order=allocation.get("harness_order") or ALLOCATION_HARNESSES,
        )
    affinity = node.get("harness_affinity")
    # A sealed `--pin worker=<harness>` leads the order (CONVENTIONS §2.1) while that harness is ranked,
    # i.e. outside a usage limit and the node's policy. The soft usage gate does not move it; a limited
    # pin is in `limited` and goes to the tail below, as before.
    worker_pin = sealed_pin_harness(route, worker_type="stage")
    pinned_head = worker_pin in ranked
    if pinned_head:
        ranked = [worker_pin] + [harness for harness in ranked if harness != worker_pin]
    elif affinity in ranked:
        # B-1: mirrors the HARNESS_CAPACITY_BIAS gate-class constraint in
        # `rank_band` — a sealed affinity may reorder within its own gate
        # class but must never lift a gated harness above an ungated one.
        if allocation["strategy"] == "balanced" and scores is not None:
            gated_of = lambda name: CAPACITY.is_gated(
                scores, name, usage_gate_used_percent=allocation.get("usage_gate_used_percent", 90),
            )
            affinity_gated = gated_of(affinity)
            all_gated = bool(ranked) and all(gated_of(h) for h in ranked)
            if all_gated:
                # Scarcity fallback is global maximum-headroom first; affinity
                # may only break a tie already preserved by the stable order.
                pass
            elif any(gated_of(h) != affinity_gated for h in ranked):
                own_class = [h for h in ranked if gated_of(h) == affinity_gated]
                other_class = [h for h in ranked if gated_of(h) != affinity_gated]
                reordered_own_class = [affinity] + [h for h in own_class if h != affinity]
                ranked = (
                    (other_class + reordered_own_class)
                    if affinity_gated
                    else (reordered_own_class + other_class)
                )
            else:
                ranked = [affinity] + [harness for harness in ranked if harness != affinity]
        else:
            ranked = [affinity] + [harness for harness in ranked if harness != affinity]
    # SD-160 supersedes the SD-101 cross-family preference, including old
    # sealed parent_cross_preference fields. A separate reviewer persona is
    # independent on the owner's harness too; placement keeps the capacity
    # order. SD-100's quality-peer sole-gate protection remains in force.
    parent_cross = "not-applicable"
    parent_cross_cause = "-"
    sole_gate = "not-applicable"
    quality_peer = None
    owner_family = (parent_identity or {}).get("parent_harness")
    if node.get("kind") == "review-worker" or node.get("parent_cross_preference") is True:
        quality_peer = quality_peer_families(_policy_by_profile(route, node))
        sole_gate = "ok"
        if quality_peer is not None and owner_family:
            head = ranked[0] if ranked else None
            if head is not None and head not in quality_peer:
                affinity_pinned_head = pinned_head or (affinity in ranked and affinity == head)
                if affinity_pinned_head:
                    sole_gate = "degraded"
                else:
                    qp_eligible = [h for h in ranked if h in quality_peer]
                    if qp_eligible:
                        ranked = qp_eligible + [h for h in ranked if h not in qp_eligible]
                    else:
                        sole_gate = "degraded"
    ranked += limited
    ordered = []
    for harness in ranked:
        hop, row = headless[harness]
        candidate = dict(row)
        if harness in limited:
            candidate["_allocation_skip"] = f"usage-{states[harness]}"
        ordered.append({**hop, "candidates": [candidate]})
    ordered.extend({**hop, "candidates": [dict(row)]} for hop, row in trailing_rows)
    ordered.extend(tail_hops)
    return apply_worker_pin(route, ordered, parent_identity), {
        "strategy": allocation["strategy"],
        "window": allocation["window"],
        "allocation": allocation,
        "preferred": CAPACITY.preferred_for_depth(allocation, int(node.get("dispatch_depth", 2))),
        "usage_gate_used_percent": allocation.get("usage_gate_used_percent", 90),
        "counts": counts,
        "states": states,
        "rank": ranked,
        "capacity": scores,
        "quality_band": quality_band,
        "relief_promoted": relief_promoted,
        "parent_cross": parent_cross,
        "parent_cross_cause": parent_cross_cause,
        "sole_gate": sole_gate,
        "quality_peer_families": sorted(quality_peer) if quality_peer is not None else None,
        "eligible": list(eligible),
        "limited": list(limited),
        "affinity": affinity,
        "owner_family": owner_family,
        "quality_peer_set": quality_peer,
    }


def _recompute_verdicts_for_child(context, child_harness):
    """Recompute the receipt verdicts for the actually launched child (G3).

    The pre-loop context describes the ranked head; the fallback cascade may
    launch a later hop instead, so `parent_cross`/`sole_gate` are recomputed
    from the real `child_harness` before the receipt is emitted and the ledger
    is written. Returns a shallow copy of the context with updated verdicts,
    or the context unchanged when the gate does not apply.
    """
    if context is None:
        return context
    updated = dict(context)
    # Never carry an obsolete same-family degradation into a fallback receipt.
    updated["parent_cross"] = "not-applicable"
    updated["parent_cross_cause"] = "-"
    quality_peer = context.get("quality_peer_set")
    if context.get("owner_family") is None or quality_peer is None:
        return updated
    updated["sole_gate"] = (
        "degraded" if child_harness not in quality_peer else "ok"
    )
    return updated


def _emit_child_success(args, route, node, context, row, *, attempt_id=None, fallback_hop=None):
    """Emit the allocation receipt and persist degradations for the actual child.

    Recomputes `parent_cross`/`sole_gate` from the launched
    `row['child_harness']` (G3): the fallback cascade can win with a later hop,
    and the pre-loop context would then mislabel the receipt and skip the
    ledger row entirely.
    """
    context = _recompute_verdicts_for_child(context, row.get("child_harness"))
    if context is not None:
        os.environ["AGENT_DISPATCH_PARENT_CROSS"] = str(
            context.get("parent_cross") or "not-applicable"
        )
        os.environ["AGENT_DISPATCH_SOLE_GATE"] = str(
            context.get("sole_gate") or "ok"
        )
    emit_allocation(context)
    _persist_parent_cross_ledger(args, route, node, context)
    receipt = _persist_allocation_receipt(
        args, route, node, context, row, attempt_id=attempt_id, fallback_hop=fallback_hop,
    )
    if receipt is not None:
        print(f"allocation_receipt={receipt['event_id']}")
        print(f"allocation_ledger={receipt['path']}")


def _persist_allocation_receipt(args, route, node, context, row, *, attempt_id=None, fallback_hop=None):
    """Durable twin of the stdout allocation receipt (2026-08-29).

    Until now the rank/headroom verdict existed only on the conductor's stdout,
    so nobody could later tell whether a configured policy had fired. Best
    effort: a ledger failure changes neither the launch nor the exit code.
    """
    context = context or {}
    return record_allocation_receipt(
        route_id=route.get("route_id"), route_node=node.get("id"),
        route_hash=route.get("route_hash"), dispatch_depth=node.get("dispatch_depth", 2),
        writer="stage-dispatch-fallback.py", action=getattr(args, "action", None),
        attempt_id=attempt_id, slug=getattr(args, "slug", None), unit=node.get("unit"),
        child_harness=row.get("child_harness"), fallback_hop=fallback_hop,
        allocation=context.get("allocation"), preferred=context.get("preferred"),
        rank=context.get("rank"), capacity=context.get("capacity"),
        counts=context.get("counts"), states=context.get("states"),
        quality_band=context.get("quality_band"), relief_promoted=context.get("relief_promoted"),
        parent_cross=context.get("parent_cross"), sole_gate=context.get("sole_gate"),
        affinity=context.get("affinity"), owner_family=context.get("owner_family"),
        jobs=getattr(args, "jobs", None),
    )


def emit_allocation(context: dict | None) -> None:
    if context is None:
        return
    print(f"allocation_strategy={context['strategy']}")
    print(f"allocation_window={context['window']}")
    for harness in ALLOCATION_HARNESSES:
        print(f"attempt_count.{harness}={context['counts'][harness]}")
    print("allocation_rank=" + ",".join(context["rank"]))
    if context.get("capacity") is not None:
        for harness in ALLOCATION_HARNESSES:
            value = context["capacity"].get(harness)
            print(
                f"capacity_headroom.{harness}="
                + ("unknown" if value is None else str(round(value, 1)))
            )
        print(f"quality_band={context.get('quality_band') or 'none'}")
        print(f"relief_promoted={int(bool(context.get('relief_promoted')))}")
    print(f"allocation_preferred={context.get('preferred') or '-'}")
    inert = inert_allocation_keys(context.get("allocation"))
    print("allocation_inert_keys=" + (",".join(sorted(inert)) or "-"))
    print(f"parent_cross={context.get('parent_cross') or 'not-applicable'}")
    print(f"parent_cross_cause={context.get('parent_cross_cause') or '-'}")
    print(f"sole_gate={context.get('sole_gate') or 'ok'}")


TUPLE_FAILURE_CLASS = "launch-tuple"


def _persist_parent_cross_ledger(args, route, node, context):
    """Record SD-100 quality degradation after a realized child start.

    Best-effort (record_degradation swallows failures by design); the stdout
    receipt fields already carry the verdicts, so a ledger write failure cannot
    change the exit code or the child (AC 17 / R5).
    """
    if context is None:
        return
    common = dict(
        route_id=route.get("route_id"), route_node=node.get("id"),
        route_hash=route.get("route_hash"), dispatch_depth=node.get("dispatch_depth", 2),
        fallback_hop=None, execution_surface="registered-headless",
        writer="stage-dispatch-fallback.py", kind="degradation",
        route_file=getattr(args, "route", None) and str(getattr(args, "route")),
        completion_gate=node.get("completion_gate"),
    )
    if context.get("sole_gate") == "degraded":
        record_degradation(
            **common,
            reason="sole-gate-non-peer-harness",
            sole_gate="degraded",
            leg_class="peer",
        )


def registry_failures(jobs: Path, route_id: str, node_id: str) -> dict[str, list[str]]:
    """Return only failures explicitly classified as launch-tuple failures.

    A terminal ``dead-*`` note describes one attempt, not the health of the
    sealed parent/child harness tuple. Worker verdicts, liveness reconciliation,
    watchdog expiry, and capacity handling therefore cannot spend a tuple by
    themselves. Producers that have exact pre-launch tuple evidence must record
    ``failure_class=launch-tuple``; current-invocation failures can still use the
    explicit ``--failed-tuple`` input without persisting that inference.
    """
    failures: dict[str, list[str]] = {}
    if not jobs.is_file():
        return failures
    for line in jobs.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split("\t")
        if len(fields) != 6 or fields[1] != "done":
            continue
        metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
        if metadata.get("route_id") != route_id or metadata.get("route_node") != node_id:
            continue
        if (not metadata.get("note", "").startswith("dead-")
                or metadata.get("note") == "dead-capacity"
                or metadata.get("failure_class") != TUPLE_FAILURE_CLASS):
            continue
        required = ("parent_harness", "parent_transport", "parent_sandbox", "child_harness", "launch_authority")
        if any(not metadata.get(key) for key in required):
            continue
        key = "/".join(metadata[name] for name in required)
        failures.setdefault(key, []).append(metadata.get("attempt_id", "legacy-attempt"))
    return failures


def registry_route_rows(jobs: Path, route_ids) -> list[dict[str, str]]:
    """Every row whose `route_id` is one of `route_ids`, in one pass.

    One definition of the row parse; `registry_rows` narrows this by node.
    A continuation asks the lineage form of the question (SD-128): a declined
    continuation writes no rows of its own, so asking only about the immediate
    predecessor finds a clean registry one generation later.
    """
    wanted = {route_ids} if isinstance(route_ids, str) else set(route_ids)
    rows: list[dict[str, str]] = []
    if not jobs.is_file():
        return rows
    for order, line in enumerate(jobs.read_text(encoding="utf-8", errors="replace").splitlines()):
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
        if metadata.get("route_id") not in wanted:
            continue
        rows.append({**metadata, "_status": fields[1], "_slug": fields[4], "_order": str(order)})
    return rows


def registry_rows(jobs: Path, route_id: str, node_id: str) -> list[dict[str, str]]:
    return [row for row in registry_route_rows(jobs, route_id)
            if row.get("route_node") == node_id]


def own_registered_attempt(args: argparse.Namespace, route: dict, node: dict) -> str | None:
    """The one open row this command registered and never launched, which its `--start` claims.

    Nothing runs for it yet, so it is not a live round: counting it refused the very start that
    launches it (`prior-attempt-still-live`).
    """
    if args.action != "start":
        return None
    rows = [row for row in registry_rows(args.jobs, route["route_id"], node["id"])
            if row["_status"] == "open" and row.get("launch_claimed") == "0" and not row.get("pid")
            and row["_slug"] == args.slug and row.get("parent_attempt_id", "") == (args.parent_attempt_id or "")]
    return rows[0].get("attempt_id") if len(rows) == 1 else None


def metadata_tuple_key(metadata: dict[str, str]) -> str:
    required = ("parent_harness", "parent_transport", "parent_sandbox",
                "child_harness", "launch_authority")
    return "/".join(metadata.get(key, "") for key in required)


def capacity_context(jobs: Path, route_id: str, node_id: str) -> dict:
    rows = registry_rows(jobs, route_id, node_id)
    capacity = [row for row in rows if row.get("note") == "dead-capacity"]
    retries = [row for row in rows if row.get("capacity_retry") == "1"]
    cooled = {row.get("model") for row in capacity if row.get("model") not in (None, "", "inherit")}
    cooled.update(row.get("cooled_model") for row in retries
                  if row.get("cooled_model") not in (None, "", "unknown", "inherit"))
    return {"capacity": capacity, "retries": retries, "cooled": cooled}


def registry_has_attempt(jobs: Path, attempt_id: str) -> bool:
    if not jobs.is_file():
        return False
    for line in jobs.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
        if metadata.get("attempt_id") == attempt_id:
            return True
    return False


def _report_launched(args, route, node, allocation_context, row, hop, ordinal, attempt_id,
                     attempts, prior_failures, output, *, terminal_note=None,
                     review_verdict=None, watch_fields=None) -> int:
    """The one success receipt of a launched attempt -- a plain start, or a
    worker that already finished with a verdict."""
    watch_fields = watch_fields or {}
    print("check=ok")
    _emit_child_success(
        args, route, node, allocation_context, row,
        attempt_id=attempt_id, fallback_hop=hop["fallback_hop"],
    )
    print(f"selected_hop={hop['fallback_hop']}")
    print(f"fallback_ordinal={ordinal}")
    print(f"child_harness={row['child_harness']}")
    print(f"excluded_harnesses={EXCLUSION.format_excluded(getattr(args, 'excluded_harnesses', frozenset()))}")
    print("launch_authority=conductor")
    print("broker_lifecycle=retired")
    print(f"attempt_id={attempt_id}")
    if review_verdict:
        print(f"terminal_note={terminal_note or REVIEW_BLOCKING_NOTE}")
        print(f"review_verdict={review_verdict}")
    if watch_fields.get("watchdog_verdict") == "advisory":
        # SD-OPEN-38 (#9): the progress tool failed while the
        # child was verified alive -- advisory, not a verdict.
        for key in ("watchdog_verdict", "watchdog_advisory_tool",
                    "watchdog_advisory_reason", "watchdog_advisory_detail"):
            print(f"{key}={watch_fields.get(key, '-')}")
    print(f"job_registry={args.jobs}")
    print("attempt_trace=" + "|".join(attempts))
    print("prior_attempt_ids=" + ",".join(x for values in prior_failures.values() for x in values))
    if output:
        print(output)
    return 0


def finished_verdict_row(jobs: Path, route_id: str, node_id: str, attempt_id: str):
    """The launched attempt's own final row when it already carries a verdict.

    A foreground reviewer that finished FAIL comes back from the wrapper as
    `worker_failure=completed-review-blocking`. That is this round's result,
    not a failed launch: retrying it on the next hop reran the same unchanged
    artifact and spent another review round before the owner could correct it
    (home-os rt-96dd5b62, 2026-09-27). `classify_round_row` is the one
    definition of "this row is a verdict" the round budget also counts.
    """
    for row in reversed(registry_rows(jobs, route_id, node_id)):
        if row.get("attempt_id") != attempt_id or row.get("_status") != "done":
            continue
        worker_type = row.get("worker_type", "")
        return row if classify_round_row("done", row, worker_type=worker_type) == "verdict" else None
    return None


def terminal_attempt_state(
    jobs: Path, route_id: str, node_id: str, attempt_id: str
) -> tuple[str, dict[str, str]] | None:
    """Classify an exact terminal row that may win the launch-heartbeat race."""

    row = next(
        (
            item for item in registry_rows(jobs, route_id, node_id)
            if item.get("attempt_id") == attempt_id and item.get("_status") == "done"
        ),
        None,
    )
    if row is None:
        return None
    note = row.get("note", "")
    fields = {
        "action": "registry-terminal",
        "terminal_action": "registry-terminal",
        "note": note or "unknown",
    }
    # The exact terminal row is the gate that may consume a portable drain
    # receipt after its observer namespace has gone away.
    process = attempt_process_quiescence(row, terminal_receipt=True)
    fields.update(process_state=process.state, process_reason=process.reason)
    decision = decide_attempt("done", row, process_state=process.state,
                              process_reason=process.reason)
    if decision.action == "wait":
        return "draining", fields
    if decision.action == "recover":
        return "fail-closed", fields
    if decision.action == "advance":
        return "terminal", fields
    if decision.action == "review":
        return "terminal", {**fields, "review_verdict": "FAIL"}
    if decision.retry_kind == "capacity":
        return "capacity", {**fields, "failure_class": "capacity"}
    if decision.retry_allowed:
        return "fallback", fields
    return "fail-closed", fields


def native_child_proof(args: argparse.Namespace, route: dict, node: dict) -> str:
    """Return the proof source for a real route-owned native child, else ''."""

    def valid_native_axes(meta):
        expected={
            "codex":"codex-native-subagent",
            "claude":"claude-subagent",
        }.get(meta.get("harness"))
        if not expected or meta.get("execution_surface") != expected:
            return False
        try:
            validate_attempt_metadata(meta)
        except DispatchContractError:
            return False
        return (
            meta.get("dispatch_depth")==str(node["dispatch_depth"])
            and meta.get("registered_worker") in {"0","false"}
            and meta.get("fallback_hop")=="native-subagent"
        )

    if args.native_attempt_id and registry_has_attempt(args.jobs, args.native_attempt_id):
        for line in args.jobs.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            meta = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
            if (meta.get("attempt_id") == args.native_attempt_id
                    and meta.get("route_id") == route["route_id"]
                    and meta.get("route_node") == node["id"]
                    and valid_native_axes(meta)
                    and meta.get("pid", "").isdigit() and meta.get("pid_start")):
                pid = int(meta["pid"])
                try:
                    actual = (Path("/proc") / str(pid) / "stat").read_text().split()[21]
                except (OSError, IndexError):
                    continue
                if actual == meta["pid_start"]:
                    return "registry-exact-pid"
    if args.native_artifact:
        path = args.native_artifact.resolve()
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
        producer = record.get("producer_attempt_id")
        if (record.get("route_id") == route["route_id"]
                and record.get("route_hash") == route["route_hash"]
                and record.get("route_node") == node["id"] and producer):
            for item in registry_rows(args.jobs, route["route_id"], node["id"]):
                if item.get("attempt_id") == producer and valid_native_axes(item):
                    return "route-owned-artifact"
    return ""


# Parent-resolution reasons that disqualify one candidate. Anything else the
# resolver can raise is an infrastructure failure of the registry itself, which
# `--start` reports as a hard stop; descending to the inline hop on it would
# reintroduce the dry-run/start divergence this function exists to remove.
CANDIDATE_SCOPED_PARENT_FAILURES = frozenset({
    "parent-attempt-not-found",
    "live-parent-not-found",
    "parent-attempt-not-live",
    "parent-attempt-ambiguous",
    "parent-process-identity-missing",
    "parent-repo-unreadable",
    "dispatch-evidence-parent-runtime-mismatch",
})


def parent_runtime_failure(args, route: dict, row: dict, parent_identity) -> str:
    """Return the typed reason this candidate's sealed parent cannot be the real one.

    Of the three dispatch-depth-2 launchers this one was the only surface with no
    parent-identity check at all: `dispatch-node.py` compares the sealed tuple
    against the launching wrapper's `AGENT_DISPATCH_CURRENT_*` export in every
    action, and `dispatch-batch.py` additionally resolves the live depth-1 owner
    attempt in its `dry-run` too. So on 2026-08-04 the same route, at the same
    moment, was reported `blocked` by the batch dry-run and `check=ok,
    selected_hop=same-harness-headless` here -- and the following `--start` spent
    two real wrapper launches to reach `parent-attempt-not-found` and descend to
    the inline hop.

    Both halves of that gap close here. The identity comparison is static and
    needs no registry, so it runs in every action and short-circuits a launch
    that could not have succeeded. The live-attempt resolution is time-varying --
    a parent alive at `dry-run` may be gone at `--start`, so exact parity is not
    achievable in principle -- and it only runs in `dry-run`, where `--register`
    and `--start` would otherwise get it from the wrapper itself. A failure is
    not fatal to the chain: the candidate is skipped exactly as a failed wrapper
    launch would be, so a genuinely unavailable runtime still descends to the
    inline hop.
    """

    if parent_identity is not None:
        try:
            DISPATCH_NODE.validate_parent_identity(row, parent_identity)
        except DISPATCH_NODE.DispatchNodeError as exc:
            return exc.reason
    if args.action != "dry-run" or not args.inherited_jobs:
        return ""
    try:
        repo = subprocess.check_output(
            ["git", "-C", str(route["cwd"]), "rev-parse", "--show-toplevel"], text=True
        ).strip()
        resolve_live_parent_attempt(
            args.jobs,
            parent_slug=args.parent,
            repo=repo,
            worktree=str(route["cwd"]),
            expected_attempt_id=os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") or None,
            expected_harness=row["parent_harness"],
            expected_transport=row["parent_transport"],
            expected_sandbox=row["parent_sandbox"],
        )
    except DispatchContractError as exc:
        return exc.reason
    except (OSError, subprocess.SubprocessError):
        return "parent-repo-unreadable"
    return ""


def attempt_identity(args: argparse.Namespace, route: dict, node: dict, row: dict, ordinal: int,
                     round_number: int = 1) -> str:
    """Stable across dry-run/register/start and concurrent conductor retries.

    A later round of a round-capped node names its round, so it gets its own identity on the
    same (pinned) harness instead of colliding with the finished first round and falling through
    to the next hop's harness. The first round's identity is unchanged.
    """

    payload = {
        "route_id": route["route_id"],
        "route_node": node["id"],
        "slug": args.slug,
        "parent": args.parent,
        "parent_attempt_id": args.parent_attempt_id,
        "target_harness": row["child_harness"],
        "fallback_ordinal": ordinal,
    }
    if round_number > 1:
        payload["round"] = round_number
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return "att-" + digest[:48]


def retry_attempt_identity(failed_attempt_id: str) -> str:
    """The one same-tuple successor of a failed attempt.

    `attempt_identity` is deterministic, so a retry on the tuple that just
    failed would name the failed row again, and the wrapper refuses a second
    launch of an existing attempt. The successor is derived from the failed
    attempt alone: dry-run, register and start (and concurrent conductors) all
    name the same one, and the claim's `automatic_retry_of` admission keeps it
    to one.
    """
    payload = {"automatic_retry_of": failed_attempt_id}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return "att-" + digest[:48]


def capacity_attempt_identity(args, route, node, row, ordinal, model):
    payload = {
        "route_id": route["route_id"], "route_node": node["id"], "slug": args.slug,
        "parent": args.parent, "target_harness": row["child_harness"],
        "parent_attempt_id": args.parent_attempt_id,
        "fallback_ordinal": ordinal, "capacity_retry": 1, "model": model,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return "att-" + digest[:48]


def legacy_attempt_identity(
    args: argparse.Namespace, route: dict, node: dict, row: dict, ordinal: int
) -> str:
    """Return the pre-D-5 identity only for typed migration diagnostics."""

    payload = {
        "route_id": route["route_id"],
        "route_node": node["id"],
        "slug": args.slug,
        "parent": args.parent,
        "target_harness": row["child_harness"],
        "fallback_ordinal": ordinal,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return "att-" + digest[:48]


def legacy_parent_generation_conflict(
    jobs: Path, legacy_attempt_id: str, parent_attempt_id: str
) -> str:
    """Name a legacy collision without treating it as a duplicate new start."""

    latest = None
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") == legacy_attempt_id:
            latest = metadata
    if latest is not None and latest.get("parent_attempt_id") not in {
        "",
        parent_attempt_id,
    }:
        return "attempt-identity-parent-generation-conflict"
    return ""


def parent_attempt_generation(
    jobs: Path, parent_slug: str, route: dict
) -> str:
    """Resolve only the exact parent generation; candidate axes stay separate."""

    expected = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
    latest: dict[str, tuple[str, str, str, dict[str, str]]] = {}
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise DispatchContractError("parent-attempt-not-found", str(exc)) from exc
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        attempt = metadata.get("attempt_id", "")
        if attempt:
            latest[attempt] = (fields[1], fields[3], fields[4], metadata)
    candidates = []
    for attempt, (status, worktree, slug, metadata) in latest.items():
        if (
            status in {"open", "running"}
            and slug == parent_slug
            and metadata.get("worker_type") == "owner"
            and Path(worktree).resolve(strict=False)
            == Path(str(route["cwd"])).resolve(strict=False)
            and (not expected or attempt == expected)
        ):
            candidates.append(attempt)
    if len(candidates) != 1:
        raise DispatchContractError(
            "parent-attempt-not-found" if not candidates else "parent-attempt-ambiguous",
            parent_slug,
        )
    return candidates[0]


def _adapter_models_conf(harness: str) -> dict[str, str]:
    try:
        config, _receipt = resolve_config(harness, source_root=ROOT)
        return config
    except ModelConfigError:
        return {}


def capacity_cascade(harness: str) -> list[tuple[str, str]]:
    """Ordered (model, paired) capacity-failover candidates from the adapter config."""
    config = _adapter_models_conf(harness)
    raw = config.get("CFG_TIER_DEEP_FAILOVER_CASCADE", "")
    restricted = config.get("CFG_MAIN_SESSION_ONLY_MODELS", "").split()
    out: list[tuple[str, str]] = []
    for entry in raw.split():
        model, sep, paired = entry.partition(":")
        if model and sep and paired and not _restricted_model(model, restricted):
            out.append((model, paired))
    return out


def capacity_cascade_next(harness: str, failed_model: str) -> tuple[str, str] | None:
    """Next (model, paired) after failed_model in the config cascade, else None.

    Capacity failover switches MODEL (a rate-limited model does not recover by
    lowering effort), so the cascade is model-granularity: e.g. opus -> sonnet.
    """
    cascade = capacity_cascade(harness)
    restricted = _adapter_models_conf(harness).get(
        "CFG_MAIN_SESSION_ONLY_MODELS", ""
    ).split()
    # Migration-only recovery: an already-running legacy job may have recorded
    # a model that is no longer delegation-eligible.  Resume at the first
    # eligible candidate without ever admitting that model into the cascade.
    if _restricted_model(failed_model, restricted):
        return cascade[0] if cascade else None
    for i, (model, _paired) in enumerate(cascade):
        if _declared_model_matches(model, failed_model) and i + 1 < len(cascade):
            return cascade[i + 1]
    return None


def _restricted_model(model: str, restricted: list[str]) -> bool:
    from model_config import restricted_model  # one matcher for every consumer

    return restricted_model(model, restricted)


def _declared_model_matches(declared: str, actual: str) -> bool:
    declared_lower = declared.lower()
    if declared_lower == actual.lower():
        return True
    return bool(
        re.fullmatch(r"[a-z0-9]+", declared_lower)
        and declared_lower in set(re.split(r"[^a-z0-9]+", actual.lower()))
    )


def allowed_capacity_settings(harness: str, model: str, paired: str) -> bool:
    restricted = (
        _adapter_models_conf(harness).get(
            "CFG_MAIN_SESSION_ONLY_MODELS", ""
        ).split()
    )
    if _restricted_model(model, restricted):
        return False
    # A model declared in the adapter capacity cascade is proved by declaration
    # (failover-only models such as Opus are intentionally not primary tiers).
    if (model, paired) in capacity_cascade(harness):
        return True
    roles = ("deep maker", "deep reviewer", "deep editor", "deep orchestrator",
             "fast implementer", "fast reviewer", "fast fact checker", "fast writer",
             "fast tool worker", "orchestrator", "external adversary")
    mapper = ROOT / f"adapters/{harness}/bin/model-map.sh"
    if not mapper.is_file():
        return False
    for role in roles:
        result = subprocess.run([str(mapper), role], cwd=ROOT, text=True, capture_output=True)
        fields = output_fields(result.stdout)
        if (result.returncode == 0 and fields.get("status", "supported") != "unknown"
                and fields.get("exact_model_id") == model
                and fields.get("reasoning") == paired):
            return True
    return False


def wrapper_command(
    args: argparse.Namespace,
    route: dict,
    node: dict,
    row: dict,
    ordinal: int,
    attempt_id: str,
    capacity_settings: tuple[str, str] | None = None,
    capacity_prior: dict[str, str] | None = None,
) -> list[str]:
    harness = row["child_harness"]
    wrapper = ROOT / f"adapters/{harness}/bin/dispatch-headless.py"
    if harness not in {"codex", "claude", "opencode"} or not wrapper.is_file():
        raise ValueError(f"unsupported child harness: {harness}")
    lifecycle = getattr(args, "launch_lifecycle", DETACHED)
    worker_type = worker_type_for_kind(node["kind"])
    contract = assigned_contract(
        # SD-165: a borrowed part reads its origin capability's contract.
        capability=(node.get("part") or "").partition(":")[0] or route["capability"],
        worker_type=worker_type,
        route_node=node["id"],
        completion_gate=node.get("completion_gate"),
        root=ROOT,
    )
    command = [
        sys.executable,
        str(wrapper),
        f"--{args.action}",
        "--worktree", route["cwd"],
        "--slug", args.slug,
        "--capability", route["capability"],
        "--capability-mode", route["capability_mode"],
        "--intensity", route["effective_intensity"],
        "--dispatch-depth", "2",
        "--parent", args.parent,
        "--worker-type", worker_type,
        "--unit", node.get("unit", ""),
        "--assigned-contract", contract,
        "--owner", route["capability"],
        "--owner-harness", row["parent_harness"],
        "--route-file", str(args.route),
        "--route-id", route["route_id"],
        "--route-hash", route["route_hash"],
        "--route-node", node["id"],
        "--registry-digest", route["registry_digest"],
        "--write-scope", ";".join(node.get("write_scope", [])),
        "--completion-gate", node["completion_gate"],
        "--jobs", str(args.jobs),
        "--attempt-id", attempt_id,
        "--parent-harness", row["parent_harness"],
        "--parent-transport", row["parent_transport"],
        "--parent-sandbox", row["parent_sandbox"],
        "--launch-authority", "conductor",
        "--nested-eligibility", "supported",
        "--eligibility-source", row["probe_source"],
        "--eligibility-failure-class", row.get("failure_class") or "-",
        "--fallback-ordinal", str(ordinal),
        "--fallback-hop", ORDER[ordinal - 1],
        "--execution-surface", "registered-headless",
        "--registered-worker", "1",
    ]
    retry_of = (capacity_prior or {}).get("attempt_id") or getattr(args, "automatic_retry_of", "")
    if retry_of:
        command += ["--automatic-retry-of", retry_of]
    if args.qa:
        # Omitted when unset: the wrapper derives it from --intensity
        # (dispatch_mode_contract.resolve_qa, CONVENTIONS §1.1, single SoT).
        command += ["--qa", args.qa]
    unit = node.get("unit") or ""
    if unit and not unit.startswith("_kernel/"):
        command += ["--worker-mode", unit]
    # Backward-compatible explicit metadata only. Canonical route dispatch
    # never synthesizes a worker_role from topology kind, node, or model role.
    if args.worker_role:
        command += ["--worker-role", args.worker_role]
    if harness in {"codex", "claude"}:
        command += ["--launch-lifecycle", lifecycle]
        if lifecycle == FOREGROUND_SCOPED:
            command += ["--foreground-timeout", str(args.foreground_timeout)]
    command += ["--model-role", _unit_role(node.get("unit")) or node.get("role", "fast implementer")]
    command += ["--model-profile", node["model_profile"]]
    if capacity_settings:
        model, paired = capacity_settings
        command += ["--model", model]
        command += [{"codex": "--reasoning", "claude": "--effort", "opencode": "--variant"}[harness], paired]
        if capacity_prior:
            command += [
                "--capacity-retry", "1",
                "--prior-attempt-id", capacity_prior.get("attempt_id", "unknown"),
                "--cooled-model", capacity_prior.get("model", "unknown"),
                "--selection-source", "orchestrator-explicit",
            ]
    optional = (
        (getattr(args, "reviewed_evidence", None), "--reviewed-evidence"),
        (args.prompt_file, "--prompt-file"),
        (os.environ.get("AGENT_DISPATCH_PARENT_SESSION_ID"), "--parent-session-id"),
        (os.environ.get("AGENT_DISPATCH_PARENT_CWD"), "--parent-cwd"),
    )
    for value, flag in optional:
        if value:
            command += [flag, str(value)]
    return command


def direct_env() -> dict[str, str]:
    """Never project a retired broker binding or an owner's own route binding
    into a direct adapter call. A node launch always supplies its own
    ``--route-file``, so an inherited ``AGENT_OWNER_ROUTE_*`` triple (set by
    ``dispatch-owner.py`` for the owner's own identity) makes the wrapper's
    owner/node tuple check reject the launch outright."""

    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENT_DISPATCH_BROKER_")
        and not key.startswith("AGENT_OWNER_ROUTE_")
        # Defense in depth only, not the fix: blocks accidental inheritance
        # into the child's own environment. The override gate reads the
        # launcher's own environment, not the child's, so this alone does not
        # address the incident.
        and key != "AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN"
    }


def launch_confirm_deadline_seconds(args) -> float:
    """Ceiling for the synchronous post-launch observation of a healthy child.

    `--start` must return a launch receipt, not babysit the worker. The window
    below used to be the FULL no-progress budget
    (`progress_window_seconds * watchdog_max_windows`, 300*12 = 1h by default),
    so a detached child that stayed healthy kept the launcher polling for an
    hour and the owner's foreground call died before ever seeing
    `registered=1/started=1/child_spawned=1`. Bound it by the spawn-confirm
    window (`--direct-timeout`) instead: early death and capacity death still
    surface here, while everything after launch belongs to the watchdog,
    orphan watch, and the completion supervisor.
    """

    budget = max(0.1, float(args.progress_window_seconds)) * max(
        1, args.watchdog_max_windows
    )
    confirm = float(getattr(args, "direct_timeout", None) or DIRECT_TIMEOUT_DEFAULT)
    if confirm <= 0:
        return budget
    return min(budget, confirm)


def live_attempt_row(jobs: Path, route_id: str, node_id: str, attempt_id: str):
    """The exact open/running row of this attempt whose governed process is
    live right now, else None."""

    row = next(
        (
            item for item in registry_rows(jobs, route_id, node_id)
            if item.get("attempt_id") == attempt_id
            and item.get("_status") in {"open", "running"}
        ),
        None,
    )
    if row is None:
        return None
    return row if attempt_process_quiescence(row).state == "live" else None


def settle_dead_latest_attempt(jobs: Path, route_id: str, node_id: str) -> str:
    """Close this node's newest launched row when its process is provably gone.

    A foreground worker whose call was killed leaves its row open; the next
    ``--start`` would otherwise hit that claimed row and start nothing. Only
    the registry's exact-dead reconcile closes it (the row is reclassified
    under the jobs lock, so a row that turned live is left alone), then the
    post-exit cleanup is settled. Anything short of that changes nothing and
    the start goes on as before. Returns the closed attempt id, else ''.
    """
    rows = registry_rows(jobs, route_id, node_id)
    if not rows:
        return ""
    latest = rows[-1]
    attempt_id = latest.get("attempt_id", "")
    if (not attempt_id or latest.get("_status") not in {"open", "running"}
            or latest.get("launch_claimed") != "1"):
        return ""
    try:
        if attempt_process_quiescence(latest).state != "quiescent":
            return ""
        import dispatch_completion_join as JOIN

        settled = JOIN.reconcile_exact_dead_attempt(jobs, JOIN.exact_attempt_row(jobs, attempt_id))
        if not settled.get("closed"):
            return ""
        JOIN.resolve_attempt_cleanup(jobs, attempt_id, apply=True)
    except (DispatchContractError, OSError, ValueError, TypeError, RuntimeError):
        return ""
    return attempt_id


def pin_last_reason(jobs: Path, route_id: str, node_id: str, pin: str,
                    attempts: list[str], direct_failures: list[dict[str, str]]) -> str:
    """Why the pinned harness did not take this step, from this run's trace."""
    failures = {item["attempt_id"]: item["reason"] for item in direct_failures}
    for entry in reversed(attempts):
        parts = entry.split(":", 2)
        if len(parts) != 3 or parts[1].split("/")[3:4] != [pin]:
            continue
        rest = parts[2]
        attempt_id = rest.rsplit("attempt-", 1)[1] if "attempt-" in rest else ""
        if attempt_id in failures:
            return failures[attempt_id]
        if attempt_id:
            note = next((row.get("note") for row in reversed(registry_rows(jobs, route_id, node_id))
                         if row.get("attempt_id") == attempt_id and row.get("note")), "")
            return note or rest.split(":", 1)[0]
        if rest.startswith("watchdog-"):
            continue
        return rest[len("skipped-"):] if rest.startswith("skipped-") else rest
    return f"no checked {pin} candidate"


def progress_tool_failure_verdict(args, route, node, attempt_id, tool, fields):
    """SD-OPEN-38 (#9): a progress TOOL failure is evidence about the tool,
    not about the child.

    Measured (cairn W15a/W15b, 2026-09-07, six stages): `--start` returned
    `progress-watchdog-fail-closed` / `watchdog_action=unknown` while the
    registry row stayed open and the child finished normally. The verdict is
    therefore cross-checked against the child itself before it may fail the
    launch: an authoritative terminal row wins as before; a live exact process
    on an open row demotes the tool failure to an advisory carried on the
    launch receipt (`watchdog_verdict=advisory`); only a child that is neither
    provably alive nor terminal keeps the fail-closed verdict.
    """

    terminal = terminal_attempt_state(args.jobs, route["route_id"], node["id"], attempt_id)
    if terminal is not None and terminal[0] != "fail-closed":
        return terminal
    if live_attempt_row(args.jobs, route["route_id"], node["id"], attempt_id) is not None:
        reason = fields.get("reason") or f"{tool}-exit"
        return "observed", {
            **{key: value for key, value in fields.items() if key not in {"check"}},
            "action": "observed",
            "watchdog_verdict": "advisory",
            "watchdog_advisory_tool": tool,
            "watchdog_advisory_reason": reason,
            "watchdog_advisory_detail": fields.get("detail", "-"),
        }
    return "fail-closed", fields


def watch_launched_attempt(args, route, node, attempt_id, launch_fields):
    """Synchronously observe one exact attempt for a bounded launch-confirm window."""
    progress = ROOT / "utilities/dispatch-progress.py"
    common = [sys.executable, str(progress), "--attempt-id", attempt_id,
              "--route-id", route["route_id"], "--route-node", node["id"],
              "--jobs", str(args.jobs)]
    # `--if-absent`: the wrapper already seeded `launch` at spawn and the worker
    # may have heartbeated past it by now; this seed is a floor, never a
    # regression (SD-OPEN-38 root cause: `progress-phase-regression` from this
    # very call was reported as `progress-watchdog-fail-closed`).
    advisory: dict[str, str] = {}
    seed = subprocess.run(common[:2] + ["heartbeat"] + common[2:] +
        ["--phase", "launch", "--kind", "registry", "--if-absent",
         "--evidence", f"pid={launch_fields.get('child_pid', '-')};start={launch_fields.get('child_pid_start', '-')}"],
        cwd=ROOT, text=True, capture_output=True, check=False, env=direct_env())
    if seed.returncode:
        seed_fields = output_fields(seed.stdout + seed.stderr)
        if seed_fields.get("reason") == "progress-phase-regression":
            # Cannot happen with --if-absent unless an older progress tool is
            # installed; still not a launch failure, but it rides on the
            # receipt as an advisory (review finding 9).
            advisory.update({
                "watchdog_verdict": "advisory",
                "watchdog_advisory_tool": "heartbeat-seed",
                "watchdog_advisory_reason": "progress-phase-regression",
                "watchdog_advisory_detail": seed_fields.get("detail", "-"),
            })
        else:
            # A foreground-scoped wrapper returns only after the worker exits.
            # The worker can therefore close its exact row before this late
            # launch heartbeat is attempted; the authoritative terminal row
            # wins that race, a live child demotes the tool failure to an
            # advisory, and anything else stays fail-closed.
            verdict = progress_tool_failure_verdict(
                args, route, node, attempt_id, "heartbeat-seed", seed_fields)
            if verdict[0] != "observed":
                return verdict
            advisory.update(verdict[1])
    def observe():
        result = subprocess.run(common[:2] + ["watchdog"] + common[2:] +
            ["--progress-window-seconds", str(args.progress_window_seconds),
             "--watchdog-max-windows", "2", "--apply"], cwd=ROOT, text=True,
            capture_output=True, check=False, env=direct_env())
        last = output_fields(result.stdout + result.stderr)
        if result.returncode:
            verdict = progress_tool_failure_verdict(
                args, route, node, attempt_id, "watchdog", last)
            if verdict[0] == "observed":
                advisory.update(verdict[1])
            return verdict
        # Observe progress here; derive retry permission only from the exact
        # settled attempt row. Process exit can precede marker publication.
        terminal = terminal_attempt_state(
            args.jobs, route["route_id"], node["id"], attempt_id
        )
        if terminal is not None:
            if terminal[0] == "draining":
                return "observed", terminal[1]
            return terminal
        if last.get("action", "").startswith("fail-closed"):
            return "fail-closed", last
        action = last.get("terminal_action")
        if action == "process-exited":
            # Keep the existing bounded observation window. If publication
            # is still pending at its end, return the original launch receipt
            # to the owner without launching another attempt.
            return "observed", last
        if action in {"dead-capacity", "dead-no-progress", "registry-terminal"}:
            # The cached claim has no matching durable terminal record.
            return "fail-closed", last
        return "observed", last

    # Establish a file/heartbeat fingerprint before the first deadline. This
    # prevents a write made during the first window from being mistaken for the
    # baseline, and also catches a capacity death that happened just after the
    # wrapper's short early-exit watch.
    state, last = observe()
    if state != "observed":
        return state, last

    window = max(0.1, float(args.progress_window_seconds))
    deadline = time.monotonic() + launch_confirm_deadline_seconds(args)
    poll = min(1.0, max(0.1, window / 10.0))
    while time.monotonic() < deadline:
        time.sleep(poll)
        state, last = observe()
        if state != "observed":
            return state, last
    if advisory:
        # A tool failure seen during the window rides on the receipt even when
        # a later observe succeeded: the owner reads it as advisory, not verdict.
        last = {**last, **{key: value for key, value in advisory.items()
                           if key.startswith("watchdog_")}}
    return "observed", last


def capacity_pair(args, harness: str) -> str | None:
    return {
        "codex": args.capacity_reasoning,
        "claude": args.capacity_effort,
        "opencode": args.capacity_variant,
    }[harness]


def capacity_retry(
    args: argparse.Namespace,
    route: dict,
    node: dict,
    row: dict,
    ordinal: int,
    failed: dict[str, str],
    attempts: list[str],
) -> tuple[str, dict[str, str], str]:
    """Run or reuse the single canonical SD-59 retry for this route node."""
    context = capacity_context(args.jobs, route["route_id"], node["id"])
    existing = context["retries"]
    if existing:
        retry = existing[-1]
        attempts.append(
            f"{ordinal}:{tuple_key(row)}:capacity-retry-existing:attempt-{retry.get('attempt_id', 'unknown')}"
        )
        if retry.get("_status") in {"open", "running"} or not retry.get("note", "").startswith("dead-"):
            return "existing", retry, ""
        return "descend", retry, "capacity-retry-terminal"

    harness = row["child_harness"]
    failed_model = failed.get("model", "")
    if node.get("model_profile") == "top":
        # The top exception profile has no cascade in either direction: the
        # person who asked for the top model decides what happens when it is
        # rate-limited. A silent step down to the deep tier would hand them a
        # review they did not ask for under the name they did.
        attempts.append(f"{ordinal}:{tuple_key(row)}:capacity-alternative-top-profile")
        return "descend", {}, "capacity-alternative-top-profile"
    # The alternative comes from an explicit --capacity-model, else the adapter
    # config capacity cascade (SD-59): a rate-limited model is switched, not
    # re-tried at lower effort. Opus exhausted -> sonnet, SOL -> LUNA, etc.
    alt_model = args.capacity_model
    alt_paired = capacity_pair(args, harness)
    if not alt_model:
        derived = capacity_cascade_next(harness, failed_model)
        if derived:
            alt_model, alt_paired = derived
    rejected = ""
    if not alt_model or not alt_paired:
        rejected = "capacity-alternative-unpaired"
    elif alt_model in context["cooled"] or alt_model == failed_model:
        rejected = "capacity-alternative-cooled"
    elif not allowed_capacity_settings(harness, alt_model, alt_paired):
        rejected = "capacity-alternative-unproved"
    if rejected:
        attempts.append(f"{ordinal}:{tuple_key(row)}:{rejected}")
        return "descend", {}, rejected

    from dispatch_capacity_evidence import active_limits
    quota = active_limits(args.jobs, models={harness: alt_model}).get(harness)
    if quota:
        reason = f"capacity-quota-until-{quota['reset_epoch']}"
        attempts.append(f"{ordinal}:{tuple_key(row)}:{reason}")
        return "descend", {"quota_reset_epoch": str(quota["reset_epoch"])}, reason

    retry_id = capacity_attempt_identity(
        args, route, node, row, ordinal, f"{alt_model}/{alt_paired}"
    )
    retry_command = wrapper_command(
        args, route, node, row, ordinal, retry_id,
        (alt_model, alt_paired), failed,
    )
    try:
        retry = run_wrapper(args, retry_command)
    except subprocess.TimeoutExpired:
        if registry_has_attempt(args.jobs, retry_id):
            return "fail-closed", {"attempt_id": retry_id}, "capacity-launch-outcome-unknown"
        return "descend", {}, "capacity-launch-timeout"
    retry_output = (retry.stdout + retry.stderr).strip()
    retry_fields = output_fields(retry_output)
    attempts.append(
        f"{ordinal}:{tuple_key(row)}:capacity-retry:exit-{retry.returncode}:attempt-{retry_id}"
    )
    if wrapper_interrupted(retry, retry_fields):
        return "interrupted", {"attempt_id": retry_id, "_run": retry}, retry_output
    if retry_fields.get("duplicate_attempt") == "1":
        refreshed = capacity_context(args.jobs, route["route_id"], node["id"])["retries"]
        if refreshed:
            return "existing", refreshed[-1], retry_output
        return "fail-closed", retry_fields, "capacity-exclusive-claim-lost"
    if (retry.returncode == 0 and retry_fields.get("early_death", "-") == "-"
            and retry_fields.get("check") != "failed"
            and retry_fields.get("worker_failure", "-") == "-"):
        if args.action == "start":
            watch_state, watch_fields = watch_launched_attempt(
                args, route, node, retry_id, retry_fields
            )
            attempts.append(f"{ordinal}:{tuple_key(row)}:capacity-watchdog-{watch_state}")
            if watch_state == "fallback":
                return "descend", watch_fields, retry_output
            if watch_state == "capacity":
                return "descend", watch_fields, retry_output
            if watch_state == "fail-closed":
                return "fail-closed", watch_fields, "capacity-watchdog-fail-closed"
        return "success", {**retry_fields, "attempt_id": retry_id}, retry_output
    # The wrapper already closed a second capacity death. Exactly one retry
    # has now been consumed; ordinary SD-50 descent owns the next action.
    return "descend", retry_fields, retry_output


def _dispatch(observation: "LAUNCH_TUPLE.ReportOnlyObservation") -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--route", type=Path, required=True)
    p.add_argument("--node", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--parent")
    p.add_argument("--capability-mode")
    p.add_argument("--worker-mode")
    p.add_argument("--mode", help="legacy scalar capability mode or family/mode worker projection")
    p.add_argument("--qa", default=None)
    p.add_argument("--worker-role")
    p.add_argument("--model-role")
    p.add_argument("--prompt-file", type=Path)
    from review_input import add_arguments, drop_inapplicable, resolve_input
    add_arguments(p)
    p.add_argument("--jobs", type=Path)
    p.add_argument("--broker-root", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--broker-timeout", type=float, help=argparse.SUPPRESS)
    p.add_argument("--direct-timeout", type=float, default=DIRECT_TIMEOUT_DEFAULT)
    p.add_argument("--foreground-timeout", type=float, default=3600.0)
    p.add_argument("--progress-window-seconds", type=float, default=300.0)
    p.add_argument("--watchdog-max-windows", type=int, default=12)
    p.add_argument("--native-attempt-id")
    p.add_argument("--native-artifact", type=Path)
    p.add_argument("--capacity-model")
    p.add_argument("--capacity-reasoning")
    p.add_argument("--capacity-effort")
    p.add_argument("--capacity-variant")
    p.add_argument("--failed-tuple", action="append", default=[], help="tuple key already failed without evidence change")
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", dest="action", action="store_const", const="dry-run")
    action.add_argument("--register", dest="action", action="store_const", const="register")
    action.add_argument("--start", dest="action", action="store_const", const="start")
    args = p.parse_args()
    # The wrappers run with cwd=ROOT, so a relative prompt path would be read
    # against the harness tree instead of the caller's directory (home-os,
    # 2026-09-27: three launches fell through to "inline, runtime-unavailable").
    if args.prompt_file is not None:
        args.prompt_file = args.prompt_file.expanduser().resolve()
    args.launch_lifecycle = select_launch_lifecycle()
    if args.action == "start" and args.launch_lifecycle == FOREGROUND_SCOPED:
        print(FOREGROUND_NOTICE, file=sys.stderr, flush=True)
    self_slug = os.environ.get("AGENT_DISPATCH_SELF_SLUG")
    if args.parent and self_slug and args.parent != self_slug:
        # The session's own name is the parent; a different --parent is a
        # typo or a stale name, not a reason to stop. Fixed before any
        # identity or registration is derived from it.
        print(
            f"notice: --parent {args.parent} is not this session's name; "
            f"using {self_slug} (from AGENT_DISPATCH_SELF_SLUG).",
            file=sys.stderr, flush=True,
        )
        args.parent = self_slug
    args.parent = args.parent or self_slug
    if not args.parent:
        return fail("parent-identity-missing", 73, child_spawned="0")

    try:
        args.route = args.route.resolve()
        raw_route = json.loads(args.route.read_text(encoding="utf-8"))
        raw_contract = raw_route.get("dispatch_contract_version") or raw_route.get("broker_contract_version")
        if raw_contract != 3:
            return fail(
                "legacy-broker-route-read-only",76,
                contract_version=str(raw_contract or 1),child_spawned="0",
            )
        route, node = load_node(args.route, args.node, args.action)
        args.reviewed_evidence = drop_inapplicable(node, args.reviewed_evidence)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        detail = str(exc)
        if "launch-runtime-root-mismatch" in detail:
            return fail(
                "launch-runtime-root-mismatch", 65, detail=detail,
                registered="0", started="0", child_spawned="0",
            )
        if "launch-compatibility-tuple-required" in detail:
            return fail(
                "launch-compatibility-tuple-required", 65, detail=detail,
                registered="0", started="0", child_spawned="0",
            )
        return fail("invalid-fallback-route", 65, detail=detail)
    contract = route.get("dispatch_contract_version") or route.get("broker_contract_version")
    if contract != 3:
        return fail("legacy-broker-route-read-only", 76, contract_version=str(contract or 1), child_spawned="0")
    mode_args = argparse.Namespace(
        capability=route.get("capability") or "",
        capability_mode=args.capability_mode,
        worker_mode=args.worker_mode,
        mode=args.mode,
        worker_type=worker_type_for_kind(node["kind"]),
        unit=node.get("unit") or "",
        assigned_contract=None,
        dispatch_depth=2,
        route_node=node["id"],
    )
    try:
        normalize_dispatch_modes(
            mode_args,
            default_capability_mode=route.get("capability_mode"),
        )
        validate_route_mode_axes(mode_args, route)
    except (DispatchModeContractError, ValueError) as exc:
        if isinstance(exc, DispatchModeContractError):
            return fail(exc.reason, 64, **exc.fields, child_spawned="0")
        return fail("invalid-dispatch-worker-type", 64, detail=str(exc), child_spawned="0")
    args.capability_mode = mode_args.capability_mode
    args.worker_mode = mode_args.worker_mode
    sealed_role = _unit_role(node.get("unit")) or node.get("role", "fast implementer")
    if args.model_role and args.model_role != sealed_role:
        return fail(
            "route-model-role-override", 64,
            expected=sealed_role, explicit=args.model_role, child_spawned="0",
        )
    if args.broker_root is not None or args.broker_timeout is not None:
        return fail("retired-broker-option", 64, child_spawned="0")
    group = node.get("parallel_group") or node.get("replica_group")
    if group and args.action in {"register", "start"}:
        return fail(
            "parallel-group-batch-required",
            65,
            parallel_group=str(group),
            child_spawned="0",
        )

    inherited_jobs = os.environ.get("AGENT_DISPATCH_JOBS")
    args.inherited_jobs = inherited_jobs
    try:
        args.jobs = resolve_global_registry(
            ROOT,
            str(args.jobs) if args.jobs else None,
            int(node.get("dispatch_depth", 2)),
            args.action,
        ).path
    except DispatchContractError as exc:
        return fail(exc.reason, 73, detail=exc.detail, child_spawned="0")

    try:
        parent_identity = DISPATCH_NODE.current_parent_identity()
    except DISPATCH_NODE.DispatchNodeError as exc:
        return fail(exc.reason, 73, child_spawned="0", **exc.fields)
    try:
        args.parent_attempt_id = parent_attempt_generation(
            args.jobs, args.parent, route
        )
    except DispatchContractError as exc:
        reason = exc.reason
        return fail(reason, 73, child_spawned="0")

    # Launch-time hard exclusion for a user prohibition (no-Claude, ...).
    # Sealed `user-disabled` unsupported rows already exclude a harness when
    # the route was composed after the prohibition; this covers a prohibition
    # that arrived after sealing. It applies identically to --dry-run,
    # --register and --start, so the dry-run receipt predicts the start
    # fallback chain instead of promising a head the start would abandon
    # for a prohibited fallback (2026-10-04 owner-handoff: dry-run Codex
    # exit 0, start Codex exit 1 -> fresh Claude worker).
    try:
        args.excluded_harnesses = EXCLUSION.excluded_harnesses(os.environ)
    except ValueError as exc:
        detail = str(exc)
        return fail("excluded-harness-unknown", 65, detail=detail, child_spawned="0",
                    excluded_harnesses="unknown")

    if args.action == "start":
        # A provably dead claimed row is closed first, so neither the round
        # admission nor the retry below reads it as a running attempt.
        settle_dead_latest_attempt(args.jobs, route["route_id"], node["id"])

    # C-14: dispatch-node.py and dispatch-batch.py both cap review rounds, but
    # ordinary standard+ depth-2 work goes through this wrapper, which had no
    # such check -- so the cap was unreachable on the path most dispatches take.
    # Reuse the already-loaded definitions rather than duplicating the cap.
    # SD-153/SD-154: `admit_round` (budget + rule-8 auto-revision) is the one
    # admission decision every registered launch surface reads -- no surface
    # keeps its own `len(prior)+1 > max_round` comparison, or its own
    # auto-revision copy, any more.
    node_round_admission = None
    if DISPATCH_NODE.REVIEW_ROUND_CAP.is_round_capped_node(node):
        own_registration = own_registered_attempt(args, route, node)
        round_rows = DISPATCH_NODE.prior_round_attempts(
            args.jobs, route["route_id"], node["id"], exclude_attempt=own_registration,
            route=route if node.get("kind") == "review-worker" else None,
        )
        admission_options = {"record_auto_revisions": False} if args.action == "dry-run" else {}
        try:
            node_round_admission = DISPATCH_NODE.admit_round(
                route, node, args.jobs, owner_attempt_id=args.parent_attempt_id,
                exclude_attempt=own_registration,
                reviewed_evidence=args.reviewed_evidence,
                **admission_options,
            )
            node_round_budget = node_round_admission.budget
        except (DispatchContractError, ValueError) as exc:
            return fail(getattr(exc, "reason", str(exc)), 65, child_spawned="0")
        if node_round_budget.state == "blocked-live":
            return fail(
                "prior-attempt-still-live", 78,
                route_id=route["route_id"], route_node=node["id"],
                child_spawned="0",
            )
        if node_round_budget.state == "blocked-unsettled":
            return fail(
                "round-unsettled", 65,
                route_id=route["route_id"], route_node=node["id"],
                child_spawned="0",
                next_action=route_state_next_action("round-unsettled", node["id"], str(args.route), node),
            )
        if node_round_budget.state == "exhausted":
            return fail(
                "review-round-budget-exhausted", 65,
                route_id=route["route_id"], route_node=node["id"],
                effective_intensity=route["effective_intensity"],
                round=str(node_round_budget.next_round), max_round=str(node_round_budget.cap),
                child_spawned="0",
                next_action=route_state_next_action("review-round-budget-exhausted", node["id"], str(args.route), node),
                **DISPATCH_NODE.review_budget_recovery_fields(node.get("kind"), route=route, node=node, jobs=args.jobs, route_file=args.route),
            )
        if node_round_budget.state == "verdictless-bound":
            # `review_budget_recovery_fields` already supplies its own
            # `next_action` (native-subagent/owner-inline routing, SD-153) --
            # that satisfies the SD-154 route-state-refusal `next_action`
            # field too, so it is not duplicated here.
            return fail(
                "review-verdictless-bound", 65,
                route_id=route["route_id"], route_node=node["id"],
                effective_intensity=route["effective_intensity"],
                round=str(node_round_budget.next_round), max_round=str(node_round_budget.cap),
                child_spawned="0",
                **DISPATCH_NODE.review_budget_recovery_fields(node.get("kind"), "verdictless-bound", route=route, node=node, jobs=args.jobs, route_file=args.route),
            )
        # B2: unlike dispatch-batch.py (which re-invokes dispatch-node.py for
        # the actual leg and gets the round protocol block for free), this
        # wrapper calls the adapter directly -- attach the same block here so
        # a correction round dispatched through this path is not silently
        # blind to its prior rounds.
        round_block = DISPATCH_NODE.round_protocol_block(
            node_round_budget, round_rows, worker_type_for_kind(node["kind"]), node["id"],
        )
        if round_block and args.prompt_file:
            base_prompt = Path(args.prompt_file).read_text(encoding="utf-8")
            with tempfile.NamedTemporaryFile(
                mode="w", delete=False, suffix=".md",
                prefix=f"round-protocol-{node['id']}-", dir=str(args.jobs.parent), encoding="utf-8",
            ) as handle:
                handle.write(base_prompt + round_block)
                augmented_prompt_path = handle.name
            args.prompt_file = Path(augmented_prompt_path)

    prior_failures = registry_failures(args.jobs, route["route_id"], node["id"])
    prior_rows = registry_rows(args.jobs, route["route_id"], node["id"])
    args.automatic_retry_of = (
        prior_rows[-1].get("attempt_id", "") if prior_rows
        and committed_outcome(prior_rows[-1]["_status"], prior_rows[-1]) == "failed" else ""
    )
    if (not args.automatic_retry_of and prior_rows
            and prior_rows[-1]["_status"] == "open"
            and prior_rows[-1].get("launch_claimed") == "0"):
        # A retry `--register`ed but not yet started keeps its predecessor, so
        # its `--start` names the same successor attempt.
        args.automatic_retry_of = prior_rows[-1].get("automatic_retry_of", "")
    failed_tuples = set(args.failed_tuple) | set(prior_failures)
    attempts: list[str] = []
    direct_failures: list[dict[str, str]] = []
    fallback_hops, allocation_context = ordered_fallback_hops(
        route, node, args.jobs, parent_identity=parent_identity
    )
    if allocation_context is not None:
        os.environ["AGENT_DISPATCH_PARENT_CROSS"] = str(
            allocation_context.get("parent_cross") or "not-applicable"
        )
        os.environ["AGENT_DISPATCH_SOLE_GATE"] = str(
            allocation_context.get("sole_gate") or "ok"
        )
    # §6.1-(2)/§4 (2)-C: arm and confirm the report-only counters *before*
    # the candidate loop -- there is no common post-loop point (E1-E12), so
    # anything computed after the loop would miss the success/native/inline/
    # chain-exhausted exits. `main()`'s `finally` reads this via `observation`
    # however `_dispatch()` returns.
    state_root = resolve_dispatch_state_root(resolve_agent_home(), args.jobs)
    observation.arm(state_root, route, node, args.parent_attempt_id)
    observation.universe = [
        tuple_key(candidate_row)
        for candidate_hop in fallback_hops
        if candidate_hop["fallback_hop"] in {"same-harness-headless", "cross-harness-headless"}
        for candidate_row in candidate_hop.get("candidates", [])
    ]
    observation.spent = LAUNCH_TUPLE.spent_tuples(
        state_root, route["route_id"], node["id"], route_hash=route.get("route_hash")
    )
    observation.failed_tuples = frozenset(failed_tuples)
    recorded_prior_skips = set()
    for ordered_hop in fallback_hops:
        if ordered_hop.get("fallback_hop") not in {
            "same-harness-headless", "cross-harness-headless"
        }:
            continue
        for ordered_row in ordered_hop.get("candidates", []):
            key = tuple_key(ordered_row)
            if ordered_row.get("_worker_pin_skip"):
                continue
            if key in failed_tuples and key not in recorded_prior_skips:
                attempts.append(
                    f"{ordered_hop['ordinal']}:{key}:skipped-prior-unchanged-failure"
                )
                recorded_prior_skips.add(key)
    pin_skipped = False
    for hop in fallback_hops:
        ordinal = int(hop["ordinal"])
        if hop.get("_worker_pin_skip"):
            # native-subagent/inline run on the owner's harness, not the pin's
            attempts.append(f"{ordinal}:{hop['fallback_hop']}:skipped-worker-pin")
            pin_skipped = True
            continue
        if hop["fallback_hop"] in {"same-harness-headless", "cross-harness-headless"}:
            for row in hop.get("candidates", []):
                key = tuple_key(row)
                if row.get("_worker_pin_skip"):
                    attempts.append(f"{ordinal}:{key}:skipped-worker-pin")
                    pin_skipped = True
                    continue
                if row.get("child_harness") in args.excluded_harnesses:
                    # Launch-time hard exclusion: a post-seal user prohibition
                    # (no-Claude, ...) skips the prohibited harness on every
                    # fallback hop, in dry-run as in start. The attempt trace
                    # keeps the skip visible so a dry-run receipt predicts the
                    # start chain instead of hiding the prohibited fallback.
                    # Reuses the closed candidate-unsupported discriminator
                    # (evidence names the prohibition) so the launch-tuple
                    # ledger stays spent without a schema change.
                    attempts.append(f"{ordinal}:{key}:skipped-excluded-harness")
                    p_excl = LAUNCH_TUPLE.record_rejection(
                        state_root, route=route, node=node, tuple_key=key,
                        rejection_class="candidate-unsupported",
                        evidence_ref=f"excluded-harness:{row.get('child_harness')}",
                        owner_attempt_id=args.parent_attempt_id,
                    )
                    if isinstance(p_excl, tuple):
                        observation.note_unrecorded(p_excl[1])
                    continue
                # Re-read after an early failure too: a whole-account quota
                # cannot be cured by changing models later in this same chain.
                from dispatch_capacity_evidence import active_limits
                quota = active_limits(args.jobs, profile=node.get("model_profile")).get(row.get("child_harness"))
                if quota:
                    row = {**row, "_allocation_skip": f"quota-until-{quota['reset_epoch']}"}
                if row.get("_allocation_skip"):
                    attempts.append(
                        f"{ordinal}:{key}:skipped-{row['_allocation_skip']}"
                    )
                    # P1 (B47-1/2): allocation-skip has exact pre-launch
                    # tuple evidence at this point -- record it.
                    p1_result = LAUNCH_TUPLE.record_rejection(
                        state_root, route=route, node=node, tuple_key=key,
                        rejection_class="allocation-skip",
                        evidence_ref=str(row["_allocation_skip"]),
                        owner_attempt_id=args.parent_attempt_id,
                    )
                    if isinstance(p1_result, tuple):
                        observation.note_unrecorded(p1_result[1])
                    continue
                unsupported = row.get("status") != "supported" or row.get("launch_authority") != "conductor"
                if unsupported or key in failed_tuples:
                    reason = "prior-unchanged-failure" if key in failed_tuples else row.get("failure_class") or row.get("status")
                    if key not in recorded_prior_skips:
                        attempts.append(f"{ordinal}:{key}:skipped-{reason}")
                    if unsupported:
                        # P2 (B47-1/2/10): only the `unsupported` branch has
                        # exact evidence -- a pure `key in failed_tuples`
                        # consumer-only skip produces no evidence (B47-10).
                        p2_evidence = str(row.get("failure_class") or row.get("status") or "unsupported")
                        p2_result = LAUNCH_TUPLE.record_rejection(
                            state_root, route=route, node=node, tuple_key=key,
                            rejection_class="candidate-unsupported",
                            evidence_ref=p2_evidence,
                            owner_attempt_id=args.parent_attempt_id,
                        )
                        if isinstance(p2_result, tuple):
                            observation.note_unrecorded(p2_result[1])
                    continue
                parent_failure = parent_runtime_failure(args, route, row, parent_identity)
                if parent_failure and parent_failure not in CANDIDATE_SCOPED_PARENT_FAILURES:
                    return fail(parent_failure, 73, child_spawned="0",
                                attempt_trace="|".join(attempts))
                if parent_failure:
                    attempts.append(f"{ordinal}:{key}:skipped-{parent_failure}")
                    direct_failures.append({
                        "attempt_id": "-",
                        "exit": "73",
                        "reason": parent_failure,
                        "detail": "sealed parent identity is not the live dispatch-depth-1 owner",
                    })
                    # P3 (B47-1/2): only the CANDIDATE_SCOPED_PARENT_FAILURES
                    # branch reaches here -- the non-scoped case already
                    # returned via fail() above.
                    p3_result = LAUNCH_TUPLE.record_rejection(
                        state_root, route=route, node=node, tuple_key=key,
                        rejection_class="sealed-parent-not-live",
                        evidence_ref=parent_failure,
                        owner_attempt_id=args.parent_attempt_id,
                    )
                    if isinstance(p3_result, tuple):
                        observation.note_unrecorded(p3_result[1])
                    continue
                try:
                    candidate = (node_round_admission.reviewed_input
                                 if args.action == "dry-run" and node_round_admission is not None
                                 else None)
                    if candidate is None:
                        candidate = resolve_input(route, node, args.jobs, args.reviewed_evidence)
                    if candidate is not None:
                        args.reviewed_evidence = candidate["path"]
                except DispatchContractError as exc:
                    return fail(exc.reason, 65, detail=exc.detail, child_spawned="0")

                pending_capacity = [
                    item for item in capacity_context(
                        args.jobs, route["route_id"], node["id"]
                    )["capacity"]
                    if item.get("capacity_retry") != "1"
                    and metadata_tuple_key(item) == key
                ]
                if pending_capacity:
                    retry_state, retry_fields, retry_output = capacity_retry(
                        args, route, node, row, ordinal, pending_capacity[-1], attempts
                    )
                    if retry_state in {"success", "existing"}:
                        print("check=ok")
                        _emit_child_success(
                            args, route, node, allocation_context, row,
                            attempt_id=retry_fields.get("attempt_id", "existing"),
                            fallback_hop=hop["fallback_hop"],
                        )
                        print(f"selected_hop={hop['fallback_hop']}")
                        print(f"fallback_ordinal={ordinal}")
                        print(f"child_harness={row['child_harness']}")
                        print(f"excluded_harnesses={EXCLUSION.format_excluded(args.excluded_harnesses)}")
                        print("capacity_retry=1")
                        print(f"cooled_model={pending_capacity[-1].get('model', 'unknown')}")
                        print(f"selected_model={retry_fields.get('model', args.capacity_model or 'existing')}")
                        print(f"attempt_id={retry_fields.get('attempt_id', 'existing')}")
                        print("attempt_trace=" + "|".join(attempts))
                        if retry_output:
                            print(retry_output)
                        return 0
                    if retry_state == "interrupted":
                        return report_interrupted(
                            retry_fields["_run"], retry_output, retry_fields["attempt_id"], attempts)
                    if retry_state == "fail-closed":
                        return fail(
                            retry_output or "capacity-retry-fail-closed", 76,
                            attempt_trace="|".join(attempts),
                        )
                    failed_tuples.add(key)
                    continue
                attempt_id = attempt_identity(
                    args, route, node, row, ordinal,
                    node_round_admission.budget.next_round if node_round_admission is not None else 1)
                if args.automatic_retry_of and attempt_id == args.automatic_retry_of:
                    # This very tuple just failed: its successor is a new attempt.
                    attempt_id = retry_attempt_identity(attempt_id)
                legacy_reason = legacy_parent_generation_conflict(
                    args.jobs,
                    legacy_attempt_identity(args, route, node, row, ordinal),
                    args.parent_attempt_id,
                )
                if legacy_reason:
                    attempts.append(
                        f"{ordinal}:{key}:legacy:{legacy_reason}"
                    )
                try:
                    command = wrapper_command(args, route, node, row, ordinal, attempt_id)
                    result = run_wrapper(args, command)
                    output = (result.stdout + result.stderr).strip()
                    fields = output_fields(output)
                    early = fields.get("early_death", "-")
                    worker_failure = fields.get("worker_failure", "-")
                    attempts.append(f"{ordinal}:{key}:direct:exit-{result.returncode}:attempt-{attempt_id}")
                    if wrapper_interrupted(result, fields):
                        return report_interrupted(result, output, attempt_id, attempts)
                    verdict_row = (finished_verdict_row(args.jobs, route["route_id"], node["id"], attempt_id)
                                   if worker_failure != "-" else None)
                    if verdict_row is not None:
                        note = verdict_row.get("note", worker_failure)
                        return _report_launched(
                            args, route, node, allocation_context, row, hop, ordinal, attempt_id,
                            attempts, prior_failures, output,
                            terminal_note=note,
                            review_verdict=("PASS" if note not in (REVIEW_BLOCKING_NOTE, "dead-worker-fail")
                                            else "FAIL"),
                        )
                    if (result.returncode != 0 or fields.get("check") == "failed"
                            or worker_failure != "-"):
                        failure_reason = (
                            fields.get("reason")
                            or (worker_failure if worker_failure != "-" else "wrapper-exit")
                        )
                        if failure_reason in PRELAUNCH_PROCESS_BLOCK_REASONS or failure_reason.startswith("retry-"):
                            return fail(
                                failure_reason,
                                78,
                                attempt_id=attempt_id,
                                child_spawned="0",
                                detail=fields.get("detail", "-"),
                                attempt_trace="|".join(attempts),
                            )
                        if failure_reason in ROUTE_STATE_REFUSAL_REASONS:
                            # SD-154/B-2 (defect #2): the wrapper's own
                            # `completion_marker_gate` (or round admission)
                            # already proved this is the ROUTE's recorded
                            # state, not a runtime that is merely unavailable
                            # right now -- stop here, never fall through to
                            # the candidate loop's eventual inline hop.
                            detail = fields.get("detail", "-")
                            next_action = fields.get("next_action") or route_state_next_action(
                                failure_reason, detail, str(args.route), node,
                            )
                            return fail(
                                failure_reason,
                                65,
                                attempt_id=attempt_id,
                                child_spawned="0",
                                detail=detail,
                                next_action=next_action,
                                attempt_trace="|".join(attempts),
                            )
                        direct_failures.append({
                            "attempt_id": attempt_id,
                            "exit": str(result.returncode),
                            "reason": failure_reason,
                            "detail": fields.get("detail") or compact_diagnostic(output),
                        })
                except subprocess.TimeoutExpired as exc:
                    attempts.append(f"{ordinal}:{key}:direct-timeout:attempt-{attempt_id}")
                    if registry_has_attempt(args.jobs, attempt_id):
                        return fail(
                            "direct-launch-outcome-unknown", 76,
                            attempt_id=attempt_id, child_spawned="unknown",
                            attempt_trace="|".join(attempts),
                        )
                    continue
                except (OSError, ValueError) as exc:
                    attempts.append(f"{ordinal}:{key}:direct-error-{type(exc).__name__}:attempt-{attempt_id}")
                    continue
                if (result.returncode == 0 and early == "-"
                        and fields.get("check") != "failed" and worker_failure == "-"):
                    watch_fields = {}
                    if args.action == "start":
                        watch_state, watch_fields = watch_launched_attempt(
                            args, route, node, attempt_id, fields)
                        attempts.append(f"{ordinal}:{key}:watchdog-{watch_state}")
                        if watch_state == "fallback":
                            failed_tuples.add(key)
                            continue
                        if watch_state == "capacity":
                            early = "capacity"
                            fields = {**fields, **watch_fields, "early_death": "capacity"}
                        if watch_state == "fail-closed":
                            return fail("progress-watchdog-fail-closed", 76,
                                        attempt_id=attempt_id,
                                        watchdog_action=watch_fields.get("action", "unknown"),
                                        attempt_trace="|".join(attempts))
                    if early != "capacity":
                        return _report_launched(
                            args, route, node, allocation_context, row, hop, ordinal, attempt_id,
                            attempts, prior_failures, output,
                            # OPERATIONS §5.10: a reviewer that finished with
                            # blocking findings inside the launch-confirm window
                            # is reported as such, not swallowed as a plain start.
                            terminal_note=(watch_fields.get("note", REVIEW_BLOCKING_NOTE)
                                           if watch_fields.get("review_verdict") else None),
                            review_verdict=watch_fields.get("review_verdict"),
                            watch_fields=watch_fields,
                        )
                if registry_has_attempt(args.jobs, attempt_id):
                    args.automatic_retry_of = attempt_id
                if early == "capacity":
                    failed = {
                        **fields, "attempt_id": attempt_id,
                        "model": fields.get("model", "unknown"),
                    }
                    retry_state, retry_fields, retry_output = capacity_retry(
                        args, route, node, row, ordinal, failed, attempts
                    )
                    if retry_state == "interrupted":
                        return report_interrupted(
                            retry_fields["_run"], retry_output, retry_fields["attempt_id"], attempts)
                    if retry_state in {"success", "existing"}:
                        print("check=ok")
                        _emit_child_success(
                            args, route, node, allocation_context, row,
                            attempt_id=retry_fields.get("attempt_id", "existing"),
                            fallback_hop=hop["fallback_hop"],
                        )
                        print(f"selected_hop={hop['fallback_hop']}")
                        print(f"fallback_ordinal={ordinal}")
                        print(f"child_harness={row['child_harness']}")
                        print(f"excluded_harnesses={EXCLUSION.format_excluded(args.excluded_harnesses)}")
                        print("capacity_retry=1")
                        print(f"cooled_model={failed['model']}")
                        print(f"selected_model={retry_fields.get('model', args.capacity_model or 'existing')}")
                        print(f"attempt_id={retry_fields.get('attempt_id', 'existing')}")
                        print("attempt_trace=" + "|".join(attempts))
                        if retry_output:
                            print(retry_output)
                        return 0
                    if retry_state == "fail-closed":
                        return fail(
                            retry_output or "capacity-retry-fail-closed", 76,
                            attempt_trace="|".join(attempts),
                        )
                    failed_tuples.add(key)
                    # Exactly one retry. A second capacity death descends through SD-50.
        elif hop["fallback_hop"] == "native-subagent":
            candidate = next((row for row in hop.get("candidates", []) if row.get("status") == "supported"), None)
            if candidate and candidate.get("harness") in args.excluded_harnesses:
                attempts.append(f"{ordinal}:native-subagent:skipped-excluded-harness")
                candidate = None
            if candidate:
                proof = native_child_proof(args, route, node)
                if proof:
                    print("check=degraded")
                    print("selected_hop=native-subagent")
                    surface={"codex":"codex-native-subagent","claude":"claude-subagent"}.get(candidate["harness"])
                    if not surface:
                        return fail("unsupported-native-execution-surface",76,child_spawned="0")
                    print(f"execution_surface={surface}")
                    print("registered_worker=0")
                    print("fallback_hop=native-subagent")
                    print("fleet_visibility=degraded")
                    print(f"native_harness={candidate['harness']}")
                    print(f"child_proof={proof}")
                    print("attempt_trace=" + "|".join(attempts))
                    print("prior_attempt_ids=" + ",".join(x for values in prior_failures.values() for x in values))
                    ledger = record_degradation(
                        route_id=route.get("route_id"), route_node=node.get("id"),
                        route_hash=route.get("route_hash"), dispatch_depth=node.get("dispatch_depth", 2),
                        fallback_hop="native-subagent", execution_surface=surface,
                        writer="stage-dispatch-fallback.py", reason="native-subagent-degraded",
                        detail=proof, attempt_trace="|".join(attempts),
                        fallback_ordinal=ordinal, fleet_visibility="degraded",
                        registered_worker=0, route_file=str(args.route),
                        completion_gate=node.get("completion_gate"),
                    )
                    print("degradation_ledger=" + (str(ledger) if ledger else "-"))
                    return 78
                attempts.append(f"{ordinal}:native-subagent:skipped-child-proof-missing")
        else:
            print("check=degraded")
            print("selected_hop=inline")
            print("execution_surface=inline")
            print("registered_worker=0")
            print("fallback_hop=inline")
            print(f"reason={hop['reason_enum']}")
            print("fleet_visibility=none")
            print("route_reuse=required")
            print(f"route_id={route['route_id']}")
            print(f"route_node={node['id']}")
            print(f"route_file={args.route}")
            print(f"completion_gate={node['completion_gate']}")
            print("attempt_trace=" + "|".join(attempts))
            print("prior_attempt_ids=" + ",".join(x for values in prior_failures.values() for x in values))
            if direct_failures:
                last = direct_failures[-1]
                print(f"last_direct_failure_attempt_id={last['attempt_id']}")
                print(f"last_direct_failure_exit={last['exit']}")
                print(f"last_direct_failure_reason={last['reason']}")
                print(f"last_direct_failure_detail={last['detail']}")
            # `reason_enum` is a compile-time constant on the inline hop, so it
            # says `runtime-unavailable` whatever actually exhausted the chain.
            # On 2026-08-04 that laundered a misconfigured route -- the runtime
            # was fine, the sealed evidence was not -- into a ledger entry
            # blaming the runtime. Carry the real last direct failure alongside
            # it; the schema already reserves the field.
            ledger = record_degradation(
                route_id=route.get("route_id"), route_node=node.get("id"),
                route_hash=route.get("route_hash"), dispatch_depth=node.get("dispatch_depth", 2),
                fallback_hop="inline", execution_surface="inline",
                writer="stage-dispatch-fallback.py", reason=hop.get("reason_enum") or "inline-degraded",
                attempt_trace="|".join(attempts), fallback_ordinal=ordinal,
                fleet_visibility="none", registered_worker=0, route_file=str(args.route),
                completion_gate=node.get("completion_gate"), parent=args.parent,
                last_direct_failure=(
                    f"{direct_failures[-1]['reason']}:exit-{direct_failures[-1]['exit']}"
                    if direct_failures else None
                ),
            )
            print("degradation_ledger=" + (str(ledger) if ledger else "-"))
            return 79
    worker_pin = sealed_pin_harness(route, worker_type="stage") if pin_skipped else None
    if worker_pin:
        # The pinned harness cannot take this step now, and the step was not
        # moved to another tool: say so in one line instead of "exhausted".
        last = pin_last_reason(args.jobs, route["route_id"], node["id"], worker_pin,
                               attempts, direct_failures)
        detail = (f"the worker is pinned to {worker_pin}; {worker_pin} cannot take this step "
                  f"now ({last}), and a pinned step is not moved to another tool")
        ledger = record_degradation(
            route_id=route.get("route_id"), route_node=node.get("id"),
            route_hash=route.get("route_hash"), dispatch_depth=node.get("dispatch_depth", 2),
            writer="stage-dispatch-fallback.py", kind="chain-exhausted",
            reason="worker-pin-unavailable", detail=detail, attempt_trace="|".join(attempts),
            route_file=str(args.route), completion_gate=node.get("completion_gate"), parent=args.parent,
        )
        return fail("worker-pin-unavailable", 79, detail=detail, worker_pin=worker_pin,
                    attempt_trace="|".join(attempts),
                    degradation_ledger=str(ledger) if ledger else "-")
    ledger = record_degradation(
        route_id=route.get("route_id"), route_node=node.get("id"),
        route_hash=route.get("route_hash"), dispatch_depth=node.get("dispatch_depth", 2),
        writer="stage-dispatch-fallback.py", kind="chain-exhausted",
        reason="fallback-chain-exhausted", attempt_trace="|".join(attempts),
        route_file=str(args.route), completion_gate=node.get("completion_gate"), parent=args.parent,
    )
    return fail("fallback-chain-exhausted", 79, attempt_trace="|".join(attempts),
                excluded_harnesses=EXCLUSION.format_excluded(args.excluded_harnesses),
                degradation_ledger=str(ledger) if ledger else "-")


def main() -> int:
    """Single exit wrapper (§4 (2)-C round_1 finding R3): every `_dispatch()`
    return -- E1-E12 and any propagated exception -- passes through this
    `finally`, so the report-only finalizer runs exactly once per invocation
    regardless of which of the 12 exit points fired. `armed=False` (any
    preprocessing failure before the candidate loop arms the observation)
    makes `write_report()` a no-op."""

    observation = LAUNCH_TUPLE.ReportOnlyObservation()
    try:
        return _dispatch(observation)
    finally:
        LAUNCH_TUPLE.write_report(observation)


if __name__ == "__main__":
    raise SystemExit(main())
