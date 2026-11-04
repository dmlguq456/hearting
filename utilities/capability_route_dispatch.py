"""Capability route: dispatch evidence, fallback chains and parallel groups.

Split out of `capability-route.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import json
from pathlib import Path

from dispatch_contract import CANONICAL_PARENT_TRANSPORTS

from capability_route_base import (
    BROKER_FIELDS, CODEX_COMMAND_STATES, DISPATCH_CONTRACT_VERSION,
    EVIDENCE_CONSUMER_DISPATCH_DEPTH, FALLBACK_ORDER, NATIVE_EVIDENCE_FIELDS, NATIVE_SURFACES,
    NESTED_FAILURE_SCOPES, NESTED_FIELDS, NESTED_SCOPE_FIELDS, NESTED_STATUSES, ORDER, TOPO,
)

from capability_route_continuation import (
    _validate_tuple_parent_identity,
)


def _validate_dispatch_evidence(
    evidence,
    contract_version=DISPATCH_CONTRACT_VERSION,
    parent_dispatch_depth=EVIDENCE_CONSUMER_DISPATCH_DEPTH - 1,
    *,
    expected_worktree=None,
    require_scope=False,
):
    contract_version = contract_version or 1
    if contract_version not in {1, 2, DISPATCH_CONTRACT_VERSION}:
        raise ValueError(f"unsupported dispatch contract version: {contract_version}")
    if not isinstance(evidence,dict): raise ValueError("checked dispatch evidence required")
    tuples=evidence.get("tuples")
    if not isinstance(tuples,list) or not tuples: raise ValueError("nested eligibility tuples required")
    normalized=[]
    for row in tuples:
        if not isinstance(row,dict) or not NESTED_FIELDS.issubset(row):
            raise ValueError("nested eligibility tuple fields missing")
        if row["status"] not in NESTED_STATUSES: raise ValueError("invalid nested eligibility status")
        if row["launch_authority"] not in ("conductor","ancestor-broker"):
            raise ValueError("invalid nested launch authority")
        if not row["probe_source"] or not row["probe_time"]:
            raise ValueError("nested eligibility checked source/time required")
        normalized_row={key:row[key] for key in sorted(NESTED_FIELDS)}
        present_scope=NESTED_SCOPE_FIELDS.intersection(row)
        has_scope=NESTED_SCOPE_FIELDS.issubset(row)
        if present_scope and not has_scope:
            raise ValueError("nested eligibility scope fields incomplete")
        if require_scope and not has_scope:
            raise ValueError("nested eligibility exact-worktree scope required")
        if has_scope:
            checked=Path(row["checked_worktree"])
            if not checked.is_absolute():
                raise ValueError("nested eligibility checked_worktree must be absolute")
            checked=checked.resolve()
            if expected_worktree is not None and checked != Path(expected_worktree).resolve():
                raise ValueError(
                    "dispatch-evidence-worktree-mismatch: "
                    f"route cwd {Path(expected_worktree).resolve()} != checked {checked}"
                )
            scope=row["failure_scope"]
            if scope not in NESTED_FAILURE_SCOPES:
                raise ValueError("invalid nested eligibility failure_scope")
            if row["codex_command"] not in CODEX_COMMAND_STATES:
                raise ValueError("invalid nested eligibility codex_command")
            retry=row["retry_on_isolated_worktree"]
            if type(retry) is not int or retry not in (0, 1):
                raise ValueError("retry_on_isolated_worktree must be 0 or 1")
            if row["status"] == "supported" and (scope != "none" or retry != 0):
                raise ValueError("supported nested eligibility cannot carry failure scope")
            if scope == "exact-worktree":
                if row["status"] == "supported" or retry != 1:
                    raise ValueError("exact-worktree failure requires unsupported retry evidence")
                if row["child_harness"] == "codex" and row["codex_command"] != "ok":
                    raise ValueError("Codex exact-worktree failure requires codex_command=ok")
            elif retry != 0:
                raise ValueError("isolated-worktree retry requires exact-worktree failure_scope")
            normalized_row.update({
                "checked_worktree": str(checked),
                "codex_command": row["codex_command"],
                "failure_scope": scope,
                "retry_on_isolated_worktree": retry,
            })
        if contract_version==DISPATCH_CONTRACT_VERSION:
            if row["launch_authority"] != "conductor":
                raise ValueError("v3 dispatch evidence requires conductor launch authority")
            if row["parent_transport"] not in CANONICAL_PARENT_TRANSPORTS:
                raise ValueError("v3 dispatch evidence requires canonical parent transport")
            _validate_tuple_parent_identity(row, parent_dispatch_depth)
            if any(row.get(key) for key in BROKER_FIELDS):
                raise ValueError("v3 dispatch evidence must not carry broker fields")
        elif contract_version==2:
            if row.get("launch_authority")=="ancestor-broker" and row.get("status")=="supported" and not row.get("broker_root"):
                raise ValueError("v2 dispatch evidence requires broker_root")
            if row.get("broker_root"):
                normalized_row["broker_root"]=row["broker_root"]
            # broker_instance is mutable rollover identity -- v2 strips it
            # even if the caller's probe output still carries one.
        else:
            for key in BROKER_FIELDS:
                if key in row:
                    normalized_row[key]=row[key]
        normalized.append(normalized_row)
    native=evidence.get("native_subagent",[])
    if not isinstance(native,list): raise ValueError("native_subagent evidence must be a list")
    normalized_native=[]
    for row in native:
        if not isinstance(row,dict) or not NATIVE_EVIDENCE_FIELDS.issubset(row):
            raise ValueError("invalid native subagent evidence")
        harness=row.get("harness")
        if (
            row.get("status") not in NESTED_STATUSES
            or harness not in NATIVE_SURFACES
            or row.get("execution_surface") != NATIVE_SURFACES[harness]
            or row.get("transport") != "headless"
            or row.get("registered_worker") is not False
            or not row.get("check_source")
        ):
            raise ValueError("invalid native subagent evidence")
        normalized_native.append({key:row[key] for key in sorted(NATIVE_EVIDENCE_FIELDS)})
    return {"tuples":normalized,"native_subagent":normalized_native}

def _fallback_chain(
    evidence,
    contract_version=DISPATCH_CONTRACT_VERSION,
    parent_dispatch_depth=EVIDENCE_CONSUMER_DISPATCH_DEPTH - 1,
    *,
    expected_worktree=None,
    require_scope=False,
):
    contract_version = contract_version or 1
    evidence=_validate_dispatch_evidence(
        evidence, contract_version, parent_dispatch_depth,
        expected_worktree=expected_worktree, require_scope=require_scope,
    )
    tuples=evidence["tuples"]
    reprobes = sorted((row for row in tuples
        if row.get("status") == "unsupported"
        and row.get("failure_scope") == "exact-worktree"
        and row.get("retry_on_isolated_worktree") == 1),
        key=lambda row: (str(row.get("child_harness", "")), str(row.get("checked_worktree", ""))))
    if reprobes:
        detail = "; ".join(
            f"{row.get('child_harness')}:{row.get('reason') or row.get('failure_class') or 'unknown'}"
            + (f" worktree={row.get('checked_worktree')}" if row.get("checked_worktree") else "")
            for row in reprobes)
        raise ValueError("dispatch-evidence-exact-worktree-reprobe-required: " + detail)
    same=[row for row in tuples if row["child_harness"]==row["parent_harness"]]
    cross=[row for row in tuples if row["child_harness"]!=row["parent_harness"]]
    if contract_version==DISPATCH_CONTRACT_VERSION:
        same=[row for row in same if row["launch_authority"]=="conductor"]
        cross=[row for row in cross if row["launch_authority"]=="conductor"]
        has_direct=any(row["status"]=="supported" for row in same+cross)
        if not has_direct:
            raise ValueError("no supported direct headless tuple")
    elif contract_version==2:
        has_broker=any(
            row["status"]=="supported" and row["launch_authority"]=="ancestor-broker" and row.get("broker_root")
            for row in same+cross
        )
        if not has_broker:
            raise ValueError("no supported registered-headless launch tuple")
    else:
        has_broker=any(
            row["status"]=="supported" and row["launch_authority"]=="ancestor-broker"
            and row.get("broker_root") and row.get("broker_instance")
            for row in same+cross
        )
        if not has_broker:
            raise ValueError("no supported registered-headless launch tuple")
    return [
        {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":same},
        {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":cross},
        {"ordinal":3,"fallback_hop":"native-subagent","candidates":evidence["native_subagent"],"fleet_visibility":"degraded"},
        {"ordinal":4,"fallback_hop":"inline","status":"eligible-after-prior-hop-exhaustion","reason_enum":"runtime-unavailable","fleet_visibility":"none"},
    ]

def _verify_fallback_chain(node, contract_version=None):
    contract_version = contract_version or 1
    chain=node.get("fallback_hops")
    if not isinstance(chain,list) or [row.get("fallback_hop") for row in chain] != FALLBACK_ORDER:
        raise ValueError(f"dispatch-depth-2 node {node.get('id')} missing ordered dispatch fallback")
    candidates=[candidate for row in chain[:2] for candidate in row.get("candidates",[])]
    if contract_version==DISPATCH_CONTRACT_VERSION:
        supported=[c for c in candidates if c.get("status")=="supported" and c.get("launch_authority")=="conductor"]
        if not supported:
            raise ValueError(f"dispatch-depth-2 node {node.get('id')} lacks supported direct headless tuple")
        if any(c.get("broker_root") or c.get("broker_instance") for c in candidates):
            raise ValueError(f"dispatch-depth-2 node {node.get('id')} v3 candidate carries retired broker fields")
    elif contract_version==1:
        supported=[c for c in candidates if c.get("status")=="supported" and c.get("launch_authority")=="ancestor-broker"]
        if not any(c.get("broker_root") and c.get("broker_instance") for c in supported):
            raise ValueError(f"dispatch-depth-2 node {node.get('id')} lacks supported dispatch-depth-0 broker tuple")
    elif contract_version==2:
        supported=[c for c in candidates if c.get("status")=="supported" and c.get("launch_authority")=="ancestor-broker"]
        if not any(c.get("broker_root") for c in supported):
            raise ValueError(f"dispatch-depth-2 node {node.get('id')} lacks supported dispatch-depth-0 broker tuple")
        if any(c.get("broker_instance") for c in supported):
            raise ValueError(f"dispatch-depth-2 node {node.get('id')} v2 candidate must not carry broker_instance")
    else:
        if not supported:
            raise ValueError(f"dispatch-depth-2 node {node.get('id')} lacks supported dispatch-depth-0 broker tuple")
    return chain

def _parallel_path(path, suffix):
    """Use the topology validator's single parallel artifact-path rule."""
    return TOPO._parallel_path(path, suffix)

# G6 (AC 21 declaration-level gate): a parallel_group's non-anchor legs always
# have `terminal`/`terminal_gate` stripped during expansion (D3), which makes
# the downstream "2+ realized nodes share one terminal_gate" check in
# `_workflow_contract` structurally unreachable for any group declared on a
# terminal node -- it can never fire, so it silently permits peer expansion of
# ANY terminal node. The real gate has to run at declaration time, before that
# stripping happens. `autopilot-research claim-verify` already ships this
# pattern (D3′, `plan.md` D3/D3-a) and is preserved as a recorded, non-silent
# grandfather rather than a silent exception; no other recipe may add this
# pattern going forward.
_TERMINAL_PARALLEL_GROUP_GRANDFATHER = {("autopilot-research", "claim-verify")}


def _expand_parallel_groups(nodes, parallel_groups, effective_intensity,
                            capability, *, auxiliary_check_units=None, persona_policy=True):
    """Expand registry-v6 groups into ordered 2..4-way sibling nodes.

    `capability` is required (N2). It was an optional kwarg defaulting to
    `None`, and the grandfather lookup is keyed on `(capability, group id)` --
    so a caller that simply forgot the argument silently rejected the shipped
    `autopilot-research claim-verify` group instead of failing at the call.
    A required parameter turns that into a TypeError at the call site.

    The first leg keeps the anchor id for stable downstream references. Extra
    legs get suffix-specific ids, outputs, and write scopes. Direct consumers
    depend on every realized leg; non-review consumers also receive every leg's
    output. `replica_group`/`independence_axis` remain one-window read aliases,
    while `parallel_group` and the plural axes are canonical.

    A group declared on a node whose recipe declaration carries `terminal:
    true` is rejected (G6/AC 21) unless the (capability, group id) pair is
    named in `_TERMINAL_PARALLEL_GROUP_GRANDFATHER`.
    """
    if not parallel_groups:
        return nodes
    if effective_intensity not in ORDER:
        raise ValueError("invalid intensity")
    for group in parallel_groups:
        if ORDER[effective_intensity] < ORDER[group["min_intensity"]]:
            continue
        width = group["width_by_intensity"][effective_intensity]
        base = next(n for n in nodes if n["id"] == group["node"])
        if base.get("terminal") is True and (capability, group["id"]) not in _TERMINAL_PARALLEL_GROUP_GRANDFATHER:
            raise ValueError(
                f"parallel group {group['id']!r} is declared on terminal node "
                f"{base['id']!r}; peer expansion of a terminal node is rejected "
                "at declaration (G6/AC 21) unless explicitly grandfathered"
            )
        members = []
        for index, leg_spec in enumerate(group["legs"][:width]):
            leg = base if index == 0 else json.loads(json.dumps(base))
            suffix = leg_spec["suffix"]
            if index:
                leg["id"] = f"{base['id']}-{suffix}"
                leg["outputs"] = [_parallel_path(path, suffix) for path in base["outputs"]]
                leg["write_scope"] = [
                    _parallel_path(path, suffix) for path in base["write_scope"]
                ]
                if "part_io" in leg:  # SD-165: a borrowed leg's own outputs moved with it
                    leg["part_io"] = {
                        name: _parallel_path(path, suffix) if path in base["outputs"] else path
                        for name, path in base["part_io"].items()}
            leg["model_profile"] = leg_spec["model_profile"]
            leg.pop("profile_demand", None)
            if "profile_demand" in leg_spec:
                leg["profile_demand"] = json.loads(json.dumps(leg_spec["profile_demand"]))
            leg["perspective"] = leg_spec["perspective"]
            leg["leg_class"] = leg_spec["leg_class"]
            if index:
                # D3: only the anchor holds the workflow terminal gate; a
                # realized sibling is a continuation leg, never a terminal.
                leg.pop("terminal", None)
                leg.pop("terminal_gate", None)
                if "continuation" not in leg:
                    leg["continuation"] = {"kind": "inline-next"}
            if leg_spec["leg_class"] == "auxiliary":
                leg["auxiliary_check"] = leg_spec["auxiliary_check"]
                unit = (auxiliary_check_units or {}).get(leg_spec["auxiliary_check"])
                if unit:
                    leg["unit"] = unit
                    leg["role"] = TOPO._unit_frontmatter(unit)["role"]
            leg["parallel_group"] = group["id"]
            leg["parallel_group_kind"] = group["kind"]
            leg["parallel_join_policy"] = group["join_policy"]
            leg["parallel_independence_axes"] = list(group["independence_axes"])
            leg["parallel_leg_index"] = index
            leg["parallel_leg_count"] = width
            leg["parallel_leg_suffix"] = suffix
            leg["parallel_anchor"] = base["id"]
            # One-window compatibility fields for jobs/Fleet and old receipts.
            leg["replica_group"] = group["id"]
            leg["independence_axis"] = "perspective" if persona_policy else "cross-harness"
            members.append(leg)
        for node in nodes:
            if node is not base and base["id"] in node.get("depends_on", []):
                node["depends_on"] = list(node["depends_on"]) + [
                    member["id"] for member in members[1:]
                ]
                if (base.get("kind") != "review-worker"
                        or node.get("completion_gate") == "compose-owner-close"):
                    node["inputs"] = list(node.get("inputs", [])) + [
                        output for member in members[1:] for output in member["outputs"]
                    ]
        expanded = []
        for node in nodes:
            expanded.append(node)
            if node is base:
                expanded.extend(members[1:])
        nodes = expanded
    return nodes


def _realized_parallel_groups(nodes):
    groups = {}
    for node in nodes:
        group_id = node.get("parallel_group")
        if not group_id:
            continue
        row = groups.setdefault(group_id, {
            "id": group_id,
            "kind": node["parallel_group_kind"],
            "join_policy": node["parallel_join_policy"],
            "independence_axes": list(node["parallel_independence_axes"]),
            "width": node["parallel_leg_count"],
            "members": [],
        })
        row["members"].append(node["id"])
    return list(groups.values())


WORKFLOW_CONTRACT_VERSION = 1


def _workflow_contract(registry, nodes, human_gate_bindings):
    """Seal the tracked-workflow shape the route commits to (`WORKFLOW §0.6`).

    Sealing terminal nodes and continuation kinds beside the graph is what lets a
    supervisor, a status surface, or a later session answer "is this finished?" from
    the route alone, instead of inferring completion from a process that exited.
    """
    ids = [node["id"] for node in nodes]
    terminal, continuations = [], {}
    terminal_gates: dict[str, str] = {}
    for node in nodes:
        node_id = node["id"]
        if node.get("terminal") is True:
            # D3-a: terminal classification is by the `terminal: true` flag, not
            # by "has no downstream dependents". A realized parallel sibling that
            # carries no flag is a continuation leg even when nothing depends on it.
            if not node.get("terminal_gate"):
                raise ValueError(f"terminal node {node_id} lacks a sealed terminal gate")
            if node.get("kind") == "resource-runner":
                raise ValueError(
                    f"terminal node {node_id} is a detached resource run; a workflow cannot "
                    "end on a process exit"
                )
            gate = node["terminal_gate"]
            # AC 21 (D3 retyping): one terminal_gate may be held by at most one
            # realized node — a second holder would duplicate the workflow end.
            if gate in terminal_gates:
                raise ValueError(
                    f"terminal gate {gate} held by both {terminal_gates[gate]} and {node_id}"
                )
            terminal_gates[gate] = node_id
            terminal.append(node_id)
            continue
        continuation = node.get("continuation")
        if not isinstance(continuation, dict) or continuation.get("kind") not in registry[
            "continuation_kinds"
        ]:
            raise ValueError(f"non-terminal node {node_id} declares no valid continuation")
        continuations[node_id] = continuation["kind"]
    if not terminal:
        raise ValueError("route declares no terminal node")
    return {
        "schema_version": WORKFLOW_CONTRACT_VERSION,
        "states": list(registry["workflow_states"]),
        "failure_states": list(registry["workflow_failure_states"]),
        "terminal_nodes": sorted(terminal),
        "continuations": continuations,
        "human_gate_bindings": json.loads(json.dumps(human_gate_bindings or [])),
    }


def _realize_conditional_extensions(recipe, effective_intensity):
    """Seal owner postconditions without turning them into dispatch nodes."""
    rows = json.loads(json.dumps(recipe.get("conditional_extensions", [])))
    terminal = "inline" if effective_intensity == "direct" else (
        "one-shot" if effective_intensity == "quick" else None
    )
    if terminal is not None:
        for row in rows:
            row["after"] = [terminal]
    return rows
