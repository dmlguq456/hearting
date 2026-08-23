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
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from model_config import ModelConfigError, resolve_config  # noqa: E402


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
    FOREGROUND_SCOPED,
    FOREGROUND_TIMEOUT_DEFAULT,
    bounded_foreground_timeout,
    select_launch_lifecycle,
)
# Spawn-confirm window: how long a `--start` call may synchronously watch a
# healthy child before it owes its caller a launch receipt.
DIRECT_TIMEOUT_DEFAULT = 45.0

from dispatch_contract import (  # noqa: E402
    PRELAUNCH_PROCESS_BLOCK_REASONS,
    DispatchContractError,
    attempt_process_quiescence,
    parse_registry_metadata,
    resolve_global_registry,
    resolve_live_parent_attempt,
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
from dispatch_degradation import record_degradation  # noqa: E402
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


def load_node(route_path: Path, node_id: str) -> tuple[dict, dict]:
    route = json.loads(route_path.read_text(encoding="utf-8"))
    verify = subprocess.run(
        [sys.executable, str(ROOT / "utilities/capability-route.py"), "verify", "--route", str(route_path)],
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


def _usage_states(jobs: Path) -> dict[str, str]:
    result = subprocess.run(
        [str(ROOT / "utilities/usage-check.sh"), "--harness", "all", "--jobs", str(jobs)],
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

    For the single-checker axis the owner policy (always deep for standard+
    routes) plus every depth-2 node's sealed `harness_policy` keyed by its
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


def _parent_cross_cause(
    affinity, ranked, eligible, limited, owner_family, quality_peer
):
    """Closed four-word cause for a parent-cross-same-harness degradation.

    Precedence: an affinity pinned to the owner family wins; otherwise a
    cross-quality-peer family that is usage-limited explains the miss; a
    missing cross-quality-peer candidate is eligible-none; the residual case
    (cross candidates exist but none was usable) is owner-family-only-peer.
    """
    if affinity == owner_family and affinity in ranked:
        return "affinity-pinned"
    cross_limited = [
        harness for harness in limited
        if harness != owner_family and harness in quality_peer
    ]
    if cross_limited:
        return "cross-family-usage-limited"
    cross_eligible = [
        harness for harness in eligible
        if harness != owner_family and harness in quality_peer
    ]
    if not cross_eligible:
        return "cross-family-eligible-none"
    return "owner-family-only-peer"


def ordered_fallback_hops(
    route: dict, node: dict, jobs: Path, *, parent_identity: dict | None = None
) -> tuple[list[dict], dict | None]:
    """Rank the checked direct-headless band from the sealed allocation policy."""

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
        return list(node["fallback_hops"]), None
    counts = attempt_counts(jobs, window=int(allocation["window"]))
    states = _usage_states(jobs)
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
    if affinity in ranked:
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
    # SD-101 parent-cross stable partition (W2) + SD-100 ② sole-gate (W2),
    # placed exactly between the affinity head-hoist and `ranked += limited`
    # so usage-limited harnesses stay out of the partition (spec 13.30.3 ④).
    parent_cross = "not-applicable"
    parent_cross_cause = "-"
    sole_gate = "not-applicable"
    quality_peer = None
    owner_family = (parent_identity or {}).get("parent_harness")
    if node.get("kind") == "review-worker" or node.get("parent_cross_preference") is True:
        quality_peer = quality_peer_families(_policy_by_profile(route, node))
        sole_gate = "ok"
        if quality_peer is not None and owner_family:
            tail = ranked[1:] if affinity in ranked else ranked

            def _cross(harness):
                return (
                    harness != owner_family
                    and harness in quality_peer
                    and harness in eligible
                )

            tail = [h for h in tail if _cross(h)] + [h for h in tail if not _cross(h)]
            if affinity in ranked:
                ranked = [affinity] + tail
            else:
                ranked = tail
            head = ranked[0] if ranked else None
            # SD-100 ②: a gate-holding single checker must land on a quality-peer
            # family when one is hard-eligible; otherwise the assignment proceeds
            # but records sole-gate-non-peer-harness (13.30.2 proviso). A sealed
            # literal affinity naming the head is a user override that is honored
            # and degrades instead of being reordered (13.30.2). The reorder is
            # derived from the sealed `ranked` order, not `eligible`, so a
            # candidate outside the sealed band cannot be hoisted over it (G3).
            if head is not None and head not in quality_peer:
                affinity_pinned_head = affinity in ranked and affinity == head
                if affinity_pinned_head:
                    sole_gate = "degraded"
                else:
                    qp_eligible = [h for h in ranked if h in quality_peer]
                    if qp_eligible:
                        ranked = qp_eligible + [h for h in ranked if h not in qp_eligible]
                    else:
                        sole_gate = "degraded"
            # G3: the parent-cross verdict is taken from the FINAL head after the
            # SD-100 ② reorder -- the head before it can be replaced by a
            # quality-peer hoist, leaving a stale "ok" on an owner-family pick.
            head = ranked[0] if ranked else None
            if head is not None and head == owner_family:
                parent_cross = "degraded"
                parent_cross_cause = _parent_cross_cause(
                    affinity, ranked, eligible, limited, owner_family, quality_peer
                )
            else:
                parent_cross = "ok"
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
    return ordered, {
        "strategy": allocation["strategy"],
        "window": allocation["window"],
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
    owner_family = context.get("owner_family")
    quality_peer = context.get("quality_peer_set")
    if owner_family is None or quality_peer is None:
        return context
    updated = dict(context)
    if child_harness == owner_family:
        updated["parent_cross"] = "degraded"
        updated["parent_cross_cause"] = _parent_cross_cause(
            context.get("affinity"),
            context.get("rank"),
            context.get("eligible"),
            context.get("limited"),
            owner_family,
            quality_peer,
        )
    else:
        updated["parent_cross"] = "ok"
        updated["parent_cross_cause"] = "-"
    updated["sole_gate"] = (
        "degraded" if child_harness not in quality_peer else "ok"
    )
    return updated


def _emit_child_success(args, route, node, context, row):
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
    print(f"parent_cross={context.get('parent_cross') or 'not-applicable'}")
    print(f"parent_cross_cause={context.get('parent_cross_cause') or '-'}")
    print(f"sole_gate={context.get('sole_gate') or 'ok'}")


TUPLE_FAILURE_CLASS = "launch-tuple"


def _persist_parent_cross_ledger(args, route, node, context):
    """Record SD-101/SD-100 typed degradations after a realized child start.

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
    if context.get("parent_cross") == "degraded":
        record_degradation(
            **common,
            reason="parent-cross-same-harness",
            cause=context.get("parent_cross_cause") or "-",
            parent_cross="degraded",
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


def registry_rows(jobs: Path, route_id: str, node_id: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not jobs.is_file():
        return rows
    for order, line in enumerate(jobs.read_text(encoding="utf-8", errors="replace").splitlines()):
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
        if metadata.get("route_id") != route_id or metadata.get("route_node") != node_id:
            continue
        rows.append({**metadata, "_status": fields[1], "_slug": fields[4], "_order": str(order)})
    return rows


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
    process = attempt_process_quiescence(row)
    fields.update(process_state=process.state, process_reason=process.reason)
    if process.state == "live":
        return "draining", fields
    if process.state != "quiescent":
        return "fail-closed", fields
    if note == "completed-marker":
        return "terminal", fields
    if note == "dead-capacity":
        return "capacity", {**fields, "failure_class": "capacity"}
    if note.startswith("dead-"):
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


def attempt_identity(args: argparse.Namespace, route: dict, node: dict, row: dict, ordinal: int) -> str:
    """Stable across dry-run/register/start and concurrent conductor retries."""

    payload = {
        "route_id": route["route_id"],
        "route_node": node["id"],
        "slug": args.slug,
        "parent": args.parent,
        "parent_attempt_id": args.parent_attempt_id,
        "target_harness": row["child_harness"],
        "fallback_ordinal": ordinal,
    }
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
    tokens = set(re.split(r"[^a-z0-9]+", model.lower()))
    return any(alias.lower() in tokens for alias in restricted)


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
        capability=route["capability"],
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
        "--qa", args.qa,
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


def watch_launched_attempt(args, route, node, attempt_id, launch_fields):
    """Synchronously observe one exact attempt for a bounded launch-confirm window."""
    progress = ROOT / "utilities/dispatch-progress.py"
    common = [sys.executable, str(progress), "--attempt-id", attempt_id,
              "--route-id", route["route_id"], "--route-node", node["id"],
              "--jobs", str(args.jobs)]
    seed = subprocess.run(common[:2] + ["heartbeat"] + common[2:] +
        ["--phase", "launch", "--kind", "registry",
         "--evidence", f"pid={launch_fields.get('child_pid', '-')};start={launch_fields.get('child_pid_start', '-')}"],
        cwd=ROOT, text=True, capture_output=True, check=False, env=direct_env())
    if seed.returncode:
        # A foreground-scoped wrapper returns only after the worker exits. The
        # worker can therefore close its exact row before this late launch
        # heartbeat is attempted. Preserve fail-closed behavior for every
        # other seed failure, but let the authoritative terminal row win this
        # completion race.
        terminal = terminal_attempt_state(
            args.jobs, route["route_id"], node["id"], attempt_id
        )
        if terminal is not None and terminal[0] != "fail-closed":
            return terminal
        return "fail-closed", output_fields(seed.stdout + seed.stderr)
    def observe():
        result = subprocess.run(common[:2] + ["watchdog"] + common[2:] +
            ["--progress-window-seconds", str(args.progress_window_seconds),
             "--watchdog-max-windows", "2", "--apply"], cwd=ROOT, text=True,
            capture_output=True, check=False, env=direct_env())
        last = output_fields(result.stdout + result.stderr)
        if result.returncode or last.get("action", "").startswith("fail-closed"):
            return "fail-closed", last
        if last.get("terminal_action") == "dead-capacity":
            return "capacity", last
        if last.get("terminal_action") == "dead-no-progress":
            return "fallback", last
        if last.get("terminal_action") == "process-exited":
            return "fallback", last
        if last.get("terminal_action") == "registry-terminal":
            terminal = terminal_attempt_state(
                args.jobs, route["route_id"], node["id"], attempt_id
            )
            if terminal is None:
                return "fail-closed", last
            if terminal[0] == "draining":
                return "observed", terminal[1]
            return terminal
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

    retry_id = capacity_attempt_identity(
        args, route, node, row, ordinal, f"{alt_model}/{alt_paired}"
    )
    retry_command = wrapper_command(
        args, route, node, row, ordinal, retry_id,
        (alt_model, alt_paired), failed,
    )
    try:
        retry = subprocess.run(
            retry_command, cwd=ROOT, text=True, capture_output=True,
            check=False,
            timeout=outer_subprocess_timeout(
                getattr(args, "foreground_timeout", FOREGROUND_TIMEOUT_DEFAULT),
                getattr(args, "launch_lifecycle", DETACHED),
                args.direct_timeout,
            ),
            env=direct_env(),
        )
    except subprocess.TimeoutExpired:
        if registry_has_attempt(args.jobs, retry_id):
            return "fail-closed", {"attempt_id": retry_id}, "capacity-launch-outcome-unknown"
        return "descend", {}, "capacity-launch-timeout"
    retry_output = (retry.stdout + retry.stderr).strip()
    retry_fields = output_fields(retry_output)
    attempts.append(
        f"{ordinal}:{tuple_key(row)}:capacity-retry:exit-{retry.returncode}:attempt-{retry_id}"
    )
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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--route", type=Path, required=True)
    p.add_argument("--node", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--parent")
    p.add_argument("--capability-mode")
    p.add_argument("--worker-mode")
    p.add_argument("--mode", help="legacy scalar capability mode or family/mode worker projection")
    p.add_argument("--qa", default="standard")
    p.add_argument("--worker-role")
    p.add_argument("--model-role")
    p.add_argument("--prompt-file", type=Path)
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
    args.launch_lifecycle = select_launch_lifecycle()
    self_slug = os.environ.get("AGENT_DISPATCH_SELF_SLUG")
    if args.parent and self_slug and args.parent != self_slug:
        return fail(
            "parent-identity-mismatch", 73, explicit=args.parent,
            current=self_slug, child_spawned="0",
        )
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
        route, node = load_node(args.route, args.node)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail("invalid-fallback-route", 65, detail=str(exc))
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

    prior_failures = registry_failures(args.jobs, route["route_id"], node["id"])
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
    recorded_prior_skips = set()
    for ordered_hop in fallback_hops:
        if ordered_hop.get("fallback_hop") not in {
            "same-harness-headless", "cross-harness-headless"
        }:
            continue
        for ordered_row in ordered_hop.get("candidates", []):
            key = tuple_key(ordered_row)
            if key in failed_tuples and key not in recorded_prior_skips:
                attempts.append(
                    f"{ordered_hop['ordinal']}:{key}:skipped-prior-unchanged-failure"
                )
                recorded_prior_skips.add(key)
    for hop in fallback_hops:
        ordinal = int(hop["ordinal"])
        if hop["fallback_hop"] in {"same-harness-headless", "cross-harness-headless"}:
            for row in hop.get("candidates", []):
                key = tuple_key(row)
                if row.get("_allocation_skip"):
                    attempts.append(
                        f"{ordinal}:{key}:skipped-{row['_allocation_skip']}"
                    )
                    continue
                unsupported = row.get("status") != "supported" or row.get("launch_authority") != "conductor"
                if unsupported or key in failed_tuples:
                    reason = "prior-unchanged-failure" if key in failed_tuples else row.get("failure_class") or row.get("status")
                    if key not in recorded_prior_skips:
                        attempts.append(f"{ordinal}:{key}:skipped-{reason}")
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
                    continue
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
                        _emit_child_success(args, route, node, allocation_context, row)
                        print(f"selected_hop={hop['fallback_hop']}")
                        print(f"fallback_ordinal={ordinal}")
                        print(f"child_harness={row['child_harness']}")
                        print("capacity_retry=1")
                        print(f"cooled_model={pending_capacity[-1].get('model', 'unknown')}")
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
                    continue
                attempt_id = attempt_identity(args, route, node, row, ordinal)
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
                    result = subprocess.run(
                        command,
                        cwd=ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=outer_subprocess_timeout(
                            args.foreground_timeout,
                            args.launch_lifecycle,
                            args.direct_timeout,
                        ),
                        env=direct_env(),
                    )
                    output = (result.stdout + result.stderr).strip()
                    fields = output_fields(output)
                    early = fields.get("early_death", "-")
                    worker_failure = fields.get("worker_failure", "-")
                    attempts.append(f"{ordinal}:{key}:direct:exit-{result.returncode}:attempt-{attempt_id}")
                    if (result.returncode != 0 or fields.get("check") == "failed"
                            or worker_failure != "-"):
                        failure_reason = (
                            fields.get("reason")
                            or (worker_failure if worker_failure != "-" else "wrapper-exit")
                        )
                        if failure_reason in PRELAUNCH_PROCESS_BLOCK_REASONS:
                            return fail(
                                failure_reason,
                                78,
                                attempt_id=attempt_id,
                                child_spawned="0",
                                detail=fields.get("detail", "-"),
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
                        print("check=ok")
                        _emit_child_success(args, route, node, allocation_context, row)
                        print(f"selected_hop={hop['fallback_hop']}")
                        print(f"fallback_ordinal={ordinal}")
                        print(f"child_harness={row['child_harness']}")
                        print("launch_authority=conductor")
                        print("broker_lifecycle=retired")
                        print(f"attempt_id={attempt_id}")
                        print(f"job_registry={args.jobs}")
                        print("attempt_trace=" + "|".join(attempts))
                        print("prior_attempt_ids=" + ",".join(x for values in prior_failures.values() for x in values))
                        if output:
                            print(output)
                        return 0
                if early == "capacity":
                    failed = {
                        **fields, "attempt_id": attempt_id,
                        "model": fields.get("model", "unknown"),
                    }
                    retry_state, retry_fields, retry_output = capacity_retry(
                        args, route, node, row, ordinal, failed, attempts
                    )
                    if retry_state in {"success", "existing"}:
                        print("check=ok")
                        _emit_child_success(args, route, node, allocation_context, row)
                        print(f"selected_hop={hop['fallback_hop']}")
                        print(f"fallback_ordinal={ordinal}")
                        print(f"child_harness={row['child_harness']}")
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
    ledger = record_degradation(
        route_id=route.get("route_id"), route_node=node.get("id"),
        route_hash=route.get("route_hash"), dispatch_depth=node.get("dispatch_depth", 2),
        writer="stage-dispatch-fallback.py", kind="chain-exhausted",
        reason="fallback-chain-exhausted", attempt_trace="|".join(attempts),
        route_file=str(args.route), completion_gate=node.get("completion_gate"), parent=args.parent,
    )
    return fail("fallback-chain-exhausted", 79, attempt_trace="|".join(attempts),
                degradation_ledger=str(ledger) if ledger else "-")


if __name__ == "__main__":
    raise SystemExit(main())
