#!/usr/bin/env python3
"""Compile, verify, and complete immutable capability routes."""
from __future__ import annotations
import argparse, contextlib, fcntl, hashlib, importlib.util, json, os, re, subprocess, sys, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("capability_topology", ROOT/"tools/capability_topology.py")
TOPO = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(TOPO)
DEFAULTS_SPEC = importlib.util.spec_from_file_location("dispatch_defaults", ROOT/"utilities/dispatch-defaults.py")
DEFAULTS = importlib.util.module_from_spec(DEFAULTS_SPEC); DEFAULTS_SPEC.loader.exec_module(DEFAULTS)
VALID_AFFINITY = DEFAULTS.AFFINITY_VALUES | {"unspecified"}
sys.path.insert(0, str(ROOT/"utilities"))
from dispatch_contract import (
    CANONICAL_PARENT_TRANSPORTS,
    DispatchContractError,
    EXECUTION_SURFACES,
    FALLBACK_HOPS,
    PARENT_TRANSPORT_BY_DISPATCH_DEPTH,
    WRAPPER_PARENT_SANDBOXES,
    WRAPPER_TRANSPORTS,
    _atomic_registry_replace,
    attempt_process_quiescence,
    ensure_global_registry_writable,
    parse_registry_metadata,
    resolve_agent_home,
    validate_attempt_metadata,
)
from stage_session_contract import load_manifest
ORDER = {"direct":0,"quick":1,"standard":2,"strong":3,"thorough":4,"adversarial":5}
TRACKING = {"tracked", "untracked"}
GATE_FIELDS = {"spec_read", "drift_verdict", "workflow_mode", "artifact_guard"}
NESTED_STATUSES = {"supported", "unsupported", "unknown"}
NESTED_FIELDS = {
    "parent_harness", "parent_transport", "parent_sandbox", "child_harness",
    "launch_authority", "status", "probe_source", "probe_time", "failure_class",
}
NESTED_SCOPE_FIELDS = {
    "checked_worktree", "failure_scope", "codex_command",
    "retry_on_isolated_worktree",
}
NESTED_FAILURE_SCOPES = {
    "none", "exact-worktree", "runtime-global", "parent-runtime", "tuple-contract",
}
CODEX_COMMAND_STATES = {"ok", "unavailable", "unchecked", "not-applicable"}
DISPATCH_EVIDENCE_SCOPE_VERSION = 1
BROKER_FIELDS = {"broker_root", "broker_instance"}  # historical v1
BROKER_FIELDS_V2 = {"broker_root"}                   # historical v2
DISPATCH_CONTRACT_VERSION = 3
FALLBACK_ORDER = ["same-harness-headless", "cross-harness-headless", "native-subagent", "inline"]
ROUTE_SCHEMA_VERSION = 2
# Only dispatch-depth-2 nodes receive a checked `fallback_hops` chain, so they are
# the sole consumers of `dispatch_evidence.tuples`.
EVIDENCE_CONSUMER_DISPATCH_DEPTH = 2
REGISTERED_HEADLESS_EVIDENCE_FIELDS = {
    "harness", "transport", "surface", "status", "probe_source", "probe_time",
}
REGISTERED_HEADLESS_STATUSES = {"supported", "unsupported", "unknown"}
REGISTERED_HEADLESS_HARNESSES = {"claude", "codex", "opencode"}
NATIVE_SURFACES = {
    "codex": "codex-native-subagent",
    "claude": "claude-subagent",
}
NATIVE_EVIDENCE_FIELDS = {
    "harness",
    "transport",
    "execution_surface",
    "registered_worker",
    "status",
    "check_source",
}


def _validate_registered_headless_evidence(evidence):
    """Normalize quick eligibility; every invalid/empty case has one failure enum."""

    if not isinstance(evidence, dict):
        raise ValueError("quick-headless-unavailable")
    candidates = evidence.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("quick-headless-unavailable")
    normalized = []
    seen_harnesses = set()
    for row in candidates:
        if not isinstance(row, dict) or not REGISTERED_HEADLESS_EVIDENCE_FIELDS.issubset(row):
            raise ValueError("quick-headless-unavailable")
        if row["status"] not in REGISTERED_HEADLESS_STATUSES:
            raise ValueError("quick-headless-unavailable")
        if row["transport"] != "headless" or row["surface"] != "registered-headless":
            raise ValueError("quick-headless-unavailable")
        if row["harness"] not in REGISTERED_HEADLESS_HARNESSES:
            raise ValueError("quick-headless-unavailable")
        if row["harness"] in seen_harnesses:
            raise ValueError("quick-headless-unavailable")
        if not row["probe_source"] or not row["probe_time"]:
            raise ValueError("quick-headless-unavailable")
        seen_harnesses.add(row["harness"])
        normalized.append({key: row[key] for key in sorted(REGISTERED_HEADLESS_EVIDENCE_FIELDS)})
    if not any(row["status"] == "supported" for row in normalized):
        raise ValueError("quick-headless-unavailable")
    return sorted(normalized, key=lambda row: row["harness"])

def canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",",":")).encode()

def route_hash(payload):
    bare={k:v for k,v in payload.items() if k not in ("route_hash","route_id")}
    return "sha256:"+hashlib.sha256(canonical(bare)).hexdigest()

def _git_commit(cwd):
    p=subprocess.run(["git","-C",str(cwd),"rev-parse","HEAD"],text=True,capture_output=True)
    return p.stdout.strip() if p.returncode == 0 else "unversioned"

def _validate_tracking_evidence(tracking, evidence):
    if tracking not in TRACKING: raise ValueError("invalid tracking value")
    if not isinstance(evidence, dict) or set(evidence) != GATE_FIELDS:
        raise ValueError("tracked gate evidence requires spec_read, drift_verdict, workflow_mode, artifact_guard")
    if evidence["workflow_mode"] != tracking:
        raise ValueError("tracked gate workflow_mode mismatch")
    if not isinstance(evidence["drift_verdict"], str) or not evidence["drift_verdict"]:
        raise ValueError("tracked gate drift_verdict required")
    for name in ("spec_read", "artifact_guard"):
        row=evidence[name]
        if not isinstance(row, dict) or not isinstance(row.get("satisfied"), bool) or not row.get("source"):
            raise ValueError(f"tracked gate {name} requires satisfied boolean and source")
        if tracking=="tracked" and not row["satisfied"]:
            raise ValueError(f"tracked gate {name} must be satisfied")
    return evidence

def _scope_touches_spec(scope):
    root=scope[:-3] if scope.endswith("/**") else scope
    return root=="spec" or root.startswith("spec/")

def _evidence_parent_dispatch_depth(nodes, owner_dispatch_depth):
    """Derive whose runtime the checked tuples must describe, from the route itself.

    `dispatch_evidence.tuples` are consumed by exactly the nodes that receive a
    `fallback_hops` chain, and only dispatch-depth-2 nodes do. The parent of a
    dispatch-depth-N node is the depth-(N-1) runtime, which for those nodes is
    the route's own registered-headless capability owner. Deriving the value
    here -- instead of hardcoding it at each call site -- keeps the check honest
    if a recipe ever seals evidence at another depth, and cross-checks the two
    structural facts the route already states about itself.
    """
    if not any(node.get("dispatch_depth") == EVIDENCE_CONSUMER_DISPATCH_DEPTH for node in nodes):
        raise ValueError(
            "dispatch-evidence-without-consumer-node: checked tuples were sealed but no "
            f"dispatch-depth-{EVIDENCE_CONSUMER_DISPATCH_DEPTH} node consumes them"
        )
    parent_dispatch_depth = EVIDENCE_CONSUMER_DISPATCH_DEPTH - 1
    if owner_dispatch_depth != parent_dispatch_depth:
        raise ValueError(
            "dispatch-evidence-parent-depth-mismatch: "
            f"owner_dispatch_depth={owner_dispatch_depth} cannot parent a "
            f"dispatch-depth-{EVIDENCE_CONSUMER_DISPATCH_DEPTH} node"
        )
    return parent_dispatch_depth

def _validate_tuple_parent_identity(row, parent_dispatch_depth):
    """Reject a checked tuple sealed for a parent runtime the route cannot have.

    The tuple's parent fields are compared field-for-field against the launching
    wrapper's `AGENT_DISPATCH_CURRENT_*` export at dispatch time
    (`dispatch-node.validate_parent_identity`), so any value that no wrapper can
    export is dead evidence. Two production incidents proved each unchecked
    field costs a whole owner cycle -- 2026-07-31 on `parent_sandbox`, 2026-08-04
    on `parent_transport` -- so all three fields fail closed at compile instead.
    """
    harness = row["parent_harness"]
    if harness not in WRAPPER_PARENT_SANDBOXES:
        raise ValueError(
            f"dispatch-evidence-parent-harness-unknown: {harness!r} is not a wrapper harness"
        )
    if row["child_harness"] not in WRAPPER_PARENT_SANDBOXES:
        raise ValueError(
            f"dispatch-evidence-child-harness-unknown: {row['child_harness']!r} is not a wrapper harness"
        )
    expected_transport = PARENT_TRANSPORT_BY_DISPATCH_DEPTH[parent_dispatch_depth]
    if row["parent_transport"] != expected_transport:
        raise ValueError(
            "dispatch-evidence-parent-transport-mismatch: a "
            f"dispatch-depth-{parent_dispatch_depth} parent is {expected_transport}, "
            f"tuple sealed {row['parent_transport']!r} "
            "(probe the dispatch-depth-2 node's parent, not the calling session)"
        )
    if row["parent_sandbox"] not in WRAPPER_PARENT_SANDBOXES[harness]:
        raise ValueError(
            "dispatch-evidence-parent-sandbox-unknown: the "
            f"{harness} wrapper exports {sorted(WRAPPER_PARENT_SANDBOXES[harness])}, "
            f"tuple sealed {row['parent_sandbox']!r}"
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
    if any(
        row.get("status") == "unsupported"
        and row.get("failure_scope") == "exact-worktree"
        and row.get("retry_on_isolated_worktree") == 1
        for row in tuples
    ):
        raise ValueError("dispatch-evidence-exact-worktree-reprobe-required")
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


def _validate_output_scopes(nodes):
    """Reject realized nodes whose path outputs escape their write authority."""
    for node in nodes:
        uncovered = TOPO._uncovered_path_outputs(
            node.get("outputs", []), node.get("write_scope", [])
        )
        if uncovered:
            raise ValueError(
                f"node {node.get('id')} outputs outside write_scope {sorted(uncovered)}"
            )

def _expand_parallel_groups(nodes, parallel_groups, effective_intensity):
    """Expand registry-v6 groups into ordered 2..4-way sibling nodes.

    The first leg keeps the anchor id for stable downstream references. Extra
    legs get suffix-specific ids, outputs, and write scopes. Direct consumers
    depend on every realized leg; non-review consumers also receive every leg's
    output. `replica_group`/`independence_axis` remain one-window read aliases,
    while `parallel_group` and the plural axes are canonical.
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
            leg["model_profile"] = leg_spec["model_profile"]
            leg["perspective"] = leg_spec["perspective"]
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
            leg["independence_axis"] = "cross-harness"
            members.append(leg)
        for node in nodes:
            if node is not base and base["id"] in node.get("depends_on", []):
                node["depends_on"] = list(node["depends_on"]) + [
                    member["id"] for member in members[1:]
                ]
                if base.get("kind") != "review-worker":
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
    dependents = {node_id: [] for node_id in ids}
    for node in nodes:
        for dep in node.get("depends_on", []) or []:
            if dep in dependents:
                dependents[dep].append(node["id"])
    terminal, continuations = [], {}
    for node in nodes:
        node_id = node["id"]
        if not dependents[node_id]:
            if node.get("terminal") is not True or not node.get("terminal_gate"):
                raise ValueError(f"terminal node {node_id} lacks a sealed terminal gate")
            if node.get("kind") == "resource-runner":
                raise ValueError(
                    f"terminal node {node_id} is a detached resource run; a workflow cannot "
                    "end on a process exit"
                )
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

def _seal_dispatch_defaults(nodes, capability, owner_profile=None):
    """Return defaults digest/allocation and stamp each dispatch-depth-2 node's
    harness_affinity, BEFORE route_hash is computed. Absent config -> all
    'unspecified' + digest None. Corrupt config -> fail-loud (reused loader
    validator), surfaced as ValueError so main() exits 64. registry_digest is
    a separate field and is never touched here."""
    config_path = DEFAULTS.default_config_path()
    if not os.path.exists(config_path):
        for node in nodes:
            if node.get("dispatch_depth") == 2:
                node["harness_affinity"] = "unspecified"
                node["harness_policy"] = None
        return None, None, None
    try:
        cfg = DEFAULTS.load_and_validate(config_path, DEFAULTS.default_topology_path())
    except DEFAULTS.DefaultsConfigError as exc:
        raise ValueError(f"corrupt dispatch-defaults config: {exc}")
    for node in nodes:
        if node.get("dispatch_depth") == 2:
            node["harness_affinity"] = DEFAULTS.query_stage_affinity(
                cfg, capability, node.get("parallel_anchor", node["id"])
            )
            node["harness_policy"] = DEFAULTS.query_profile_policy(
                cfg, node["model_profile"]
            )
    return (
        "sha256:" + hashlib.sha256(canonical(cfg)).hexdigest(),
        DEFAULTS.query_allocation(cfg),
        DEFAULTS.query_profile_policy(cfg, owner_profile) if owner_profile else None,
    )

def unit_catalog_digest(units_root=None):
    """Digest of unit frontmatter blocks (machine contracts); unit BODY prose stays un-hashed."""
    units_root=Path(units_root) if units_root else ROOT/"roles"/"units"
    blocks=[]
    for path in sorted(units_root.glob("*/*.md")):
        if path.name.startswith("_"): continue
        match=re.match(r"\A---\n.*?\n---\n", path.read_text(encoding="utf-8"), re.DOTALL)
        if match: blocks.append(f"{path.relative_to(units_root)}\n{match.group(0)}")
    return "sha256:"+hashlib.sha256("\n".join(blocks).encode()).hexdigest()

def compile_route(capability, capability_mode, requested_intensity, cwd, artifact_root,
                  predicates=(), signals=(), transport=None,
                  transport_evidence="caller-selected", inline_reason=None,
                  tracking="tracked", tracked_gate_evidence=None, dispatch_evidence=None,
                  registered_headless_evidence=None):
    registry=TOPO.load_registry(); TOPO.validate_registry(registry)
    recipe=TOPO.resolve_recipe(registry, capability, capability_mode)
    return _compile_from_recipe(
        registry, recipe, capability, capability_mode, requested_intensity, cwd, artifact_root,
        predicates=predicates, signals=signals, transport=transport,
        transport_evidence=transport_evidence, inline_reason=inline_reason,
        tracking=tracking, tracked_gate_evidence=tracked_gate_evidence,
        dispatch_evidence=dispatch_evidence,
        registered_headless_evidence=registered_headless_evidence)

def compile_composed_route(composed_recipe, capability_mode, requested_intensity, cwd, artifact_root,
                           **kwargs):
    """Compile a compose-on-demand recipe through the SAME validate/seal path (composed: true)."""
    registry=TOPO.load_registry(); TOPO.validate_registry(registry)
    if not isinstance(composed_recipe, dict): raise ValueError("composed recipe must be an object")
    TOPO._validate_recipe(
        composed_recipe, registry,
        registry["owner_profile_by_intensity"]["standard"],
    )
    # SAME validator means gates too: without this, a composed recipe could carry a
    # forged completion gate that no registry contract backs (2026-07-22 verify finding).
    TOPO._validate_gate_contracts(composed_recipe, registry)
    if capability_mode not in composed_recipe.get("modes", []):
        raise ValueError("composed recipe does not declare the requested capability mode")
    return _compile_from_recipe(
        registry, composed_recipe, composed_recipe["capability"], capability_mode,
        requested_intensity, cwd, artifact_root, composed=True, **kwargs)

def _compile_from_recipe(registry, recipe, capability, capability_mode, requested_intensity,
                         cwd, artifact_root, predicates=(), signals=(), transport=None,
                         transport_evidence="caller-selected", inline_reason=None,
                         tracking="tracked", tracked_gate_evidence=None, dispatch_evidence=None,
                         registered_headless_evidence=None, composed=False):
    cwd=Path(cwd).resolve(strict=True); artifact=Path(artifact_root).resolve()
    if not cwd.is_absolute() or not artifact.is_absolute(): raise ValueError("cwd and artifact root must be absolute")
    known_pred=set(recipe["direct_predicates"]); predicates=sorted(set(predicates))
    unknown=set(predicates)-known_pred
    if unknown: raise ValueError("unknown predicates: "+",".join(sorted(unknown)))
    signals=sorted(set(signals))
    if set(signals) & TRACKING: raise ValueError("tracking cannot be an escalation signal")
    unknown=set(signals)-set(recipe["promotion_signals"])
    if unknown: raise ValueError("unknown promotion signals: "+",".join(sorted(unknown)))
    requested="standard" if requested_intensity=="auto" else requested_intensity
    if requested not in ORDER: raise ValueError("invalid intensity")
    if transport is not None and transport not in WRAPPER_TRANSPORTS:
        raise ValueError(f"invalid transport: {transport!r}")
    inferred="standard" if signals else ("direct" if set(predicates)==known_pred else "quick")
    effective=max((requested,inferred),key=ORDER.get)
    if composed and effective in ("direct","quick"):
        raise ValueError("composed routes require a standard+ effective intensity")
    registered_headless_candidates=None
    if effective=="direct":
        transport="interactive"
        if inline_reason is None: inline_reason="atomic-direct"
        owner_model_profile=None
        nodes=[{"id":"inline","kind":"capability-owner","dispatch_depth":0,"role":"orchestrator",
                "write_scope":recipe["quick"]["write_scope"],"resource_class":"normal",
                "execution_surface":"inline","registered_worker":False,
                "completion_gate":"inline-complete",
                "terminal":True,"terminal_gate":"inline-complete"}]
        gates=["inline-complete"]
        selection_basis=[{"axis":"direct-predicate","signal":p,"source":"caller"} for p in predicates]
    elif effective=="quick":
        if transport not in (None, "headless"):
            raise ValueError(f"invalid quick transport: {transport!r}")
        registered_headless_candidates=_validate_registered_headless_evidence(
            registered_headless_evidence
        )
        transport="headless"
        owner_model_profile=registry["owner_profile_by_intensity"]["quick"]
        nodes=[{"id":"one-shot","kind":recipe["quick"]["worker_kind"],"dispatch_depth":1,"role":"orchestrator",
                "unit":"_kernel/owner",
                "model_profile":owner_model_profile,
                "write_scope":recipe["quick"]["write_scope"],"resource_class":"normal",
                "execution_surface":"registered-headless","registered_worker":True,
                "completion_gate":"quick-complete",
                "terminal":True,"terminal_gate":"quick-complete"}]
        gates=["quick-complete"]
        selection_basis=[{"axis":"direct-predicate-gap","signal":p,"source":"compiler"} for p in sorted(known_pred-set(predicates))]
    else:
        if transport not in (None, "headless"):
            raise ValueError(f"invalid standard+ transport: {transport!r}")
        transport="headless"
        owner_model_profile=registry["owner_profile_by_intensity"][effective]
        nodes=json.loads(json.dumps(recipe["standard_plus"]["nodes"])); gates=recipe["completion_gates"]
        nodes=_expand_parallel_groups(
            nodes, recipe["standard_plus"].get("parallel_groups"), effective
        )
        for node in nodes:
            node.pop("fallback_hops", None)
        selection_basis=[{"axis":"promotion","signal":s,"source":"caller"} for s in signals]
    _validate_output_scopes(nodes)
    if effective != "direct" and inline_reason is not None:
        raise ValueError("inline_reason only applies to direct")
    if effective=="direct" and inline_reason not in registry["inline_reasons"]:
        raise ValueError("structured inline_reason required")
    evidence=_validate_tracking_evidence(tracking, tracked_gate_evidence)
    checked_dispatch=None
    if effective not in ("direct","quick"):
        parent_dispatch_depth=_evidence_parent_dispatch_depth(
            nodes, recipe["standard_plus"]["owner_dispatch_depth"])
        checked_dispatch=_validate_dispatch_evidence(
            dispatch_evidence, DISPATCH_CONTRACT_VERSION, parent_dispatch_depth,
            expected_worktree=cwd, require_scope=True)
        chain=_fallback_chain(
            checked_dispatch, DISPATCH_CONTRACT_VERSION, parent_dispatch_depth,
            expected_worktree=cwd, require_scope=True)
        for node in nodes:
            if node.get("dispatch_depth")==2:
                node["fallback_hops"]=json.loads(json.dumps(chain))
    dispatch_defaults_digest,dispatch_allocation,owner_harness_policy=_seal_dispatch_defaults(
        nodes, capability, owner_model_profile
    )
    spec_touch=any(_scope_touches_spec(scope) for node in nodes for scope in node["write_scope"])
    payload={
      "schema_version":ROUTE_SCHEMA_VERSION,"capability":capability,"capability_mode":capability_mode,
      "requested_intensity":requested_intensity,"effective_intensity":effective,
      "owner_model_profile":owner_model_profile,
      "execution_topology":("inline" if effective=="direct" else recipe["quick"]["topology"] if effective=="quick" else recipe["topology_class"]),
      "owner_dispatch_depth":0 if effective=="direct" else (recipe["quick"]["owner_dispatch_depth"] if effective=="quick" else recipe["standard_plus"]["owner_dispatch_depth"]),
      "max_dispatch_depth":recipe["quick"]["max_dispatch_depth"] if effective=="quick" else (0 if effective=="direct" else recipe["standard_plus"]["max_dispatch_depth"]),
      "tracking":tracking,"tracked_gate_evidence":evidence,"spec_touch":spec_touch,
      "cwd":str(cwd),"artifact_root":str(artifact),"source_commit":_git_commit(cwd),
      "registry_digest":TOPO.registry_digest(registry),
      "dispatch_defaults_digest":dispatch_defaults_digest,
      "dispatch_allocation":dispatch_allocation,
      "owner_harness_policy":owner_harness_policy,
      "selection":{"direct_predicates":predicates,"promotion_signals":[{"signal":s,"source":"caller"} for s in signals],
                   "selection_basis":selection_basis,
                   "escalation_basis":[{"signal":s,"source":"caller"} for s in signals],
                   "transport":transport,"transport_evidence":transport_evidence,"inline_reason":inline_reason},
      "nodes":nodes,"parallel_groups":_realized_parallel_groups(nodes),
      "conditional_extensions":_realize_conditional_extensions(recipe, effective),
      "completion_gates":gates,"human_gates":recipe["human_gates"],
      "human_gate_bindings":json.loads(json.dumps(
          recipe["human_gate_bindings"] if effective not in ("direct","quick") else [])),
      "workflow_contract":_workflow_contract(
          registry, nodes,
          recipe["human_gate_bindings"] if effective not in ("direct","quick") else []),
      "resume_retry_boundaries":recipe["resume_retry_boundaries"],
      "dispatch_evidence":checked_dispatch,
      "dispatch_contract_version":DISPATCH_CONTRACT_VERSION,
      "registered_headless_candidates":registered_headless_candidates,
      "registered_headless_policy":"serial-attempt" if effective=="quick" else None,
      "unit_catalog_digest":unit_catalog_digest()}
    if checked_dispatch is not None:
        payload["dispatch_evidence_scope_version"]=DISPATCH_EVIDENCE_SCOPE_VERSION
    if composed:
        payload["composed"]=True
        payload["composed_recipe"]=json.loads(json.dumps(recipe))
    digest=route_hash(payload); payload["route_hash"]=digest; payload["route_id"]="rt-"+digest.split(":",1)[1][:16]
    return payload

def verify_route(route, expected_cwd=None, *, allow_stale_registry=False):
    """Verify a route for mutating/resume use.

    `allow_stale_registry` exists for one caller: closing a route. A registry or unit
    edit inside the same cycle changes the digest and permanently invalidates the route
    compiled before it, so a strict `close` could never record the outcome of exactly
    the work that changed the registry — leaving an open route that `WORKFLOW §0.5`
    calls indistinguishable from abandoned work. Closure writes a sidecar and grants no
    authority, so the route's own integrity (hash, id, cwd) is the right gate there;
    digest currentness stays required for anything that launches, dispatches, or
    mutates, and the closure records which case it was.
    """
    if route.get("schema_version") != ROUTE_SCHEMA_VERSION:
        raise ValueError(
            f"legacy route schema_version={route.get('schema_version')!r} rejected for mutating/resume use"
        )
    if route.get("dispatch_contract_version") != DISPATCH_CONTRACT_VERSION:
        raise ValueError("legacy dispatch contract is read-only")
    scope_version=route.get("dispatch_evidence_scope_version")
    if scope_version not in (None, DISPATCH_EVIDENCE_SCOPE_VERSION):
        raise ValueError("unsupported dispatch evidence scope version")
    if route.get("route_hash") != route_hash(route): raise ValueError("stale or modified route hash")
    if route.get("route_id") != "rt-"+route["route_hash"].split(":",1)[1][:16]: raise ValueError("invalid route id")
    if expected_cwd and Path(expected_cwd).resolve()!=Path(route["cwd"]): raise ValueError("route cwd mismatch")
    registry=TOPO.load_registry()
    registry_current=route["registry_digest"]==TOPO.registry_digest(registry)
    units_current=(route.get("unit_catalog_digest") is None
                   or route["unit_catalog_digest"]==unit_catalog_digest())
    if not (registry_current and units_current):
        if not allow_stale_registry:
            raise ValueError(
                "stale registry digest" if not registry_current else "stale unit catalog digest"
            )
        # A stale sealed graph cannot be re-derived from the current registry, so every
        # check that compares against it is skipped rather than guessed at.
        return dict(route, _registry_current=False)
    _validate_output_scopes(route.get("nodes", []))
    if route.get("composed"):
        if route.get("effective_intensity") in ("direct","quick"):
            raise ValueError("composed routes require a standard+ effective intensity")
        composed_recipe=route.get("composed_recipe")
        if not isinstance(composed_recipe, dict):
            raise ValueError("composed route lacks embedded composed_recipe")
        TOPO._validate_recipe(
            composed_recipe, registry,
            registry["owner_profile_by_intensity"]["standard"],
        )
        def _node_identity(node):
            return {
                k: v for k, v in node.items()
                if k not in ("fallback_hops", "harness_affinity", "harness_policy")
            }
        expected_nodes=json.loads(json.dumps(composed_recipe["standard_plus"]["nodes"]))
        expected_nodes=_expand_parallel_groups(
            expected_nodes, composed_recipe["standard_plus"].get("parallel_groups"),
            route.get("effective_intensity"))
        if ([_node_identity(n) for n in route.get("nodes",[])]
                != [_node_identity(n) for n in expected_nodes]):
            raise ValueError("composed route nodes differ from embedded composed recipe")
        route_recipe=composed_recipe
    else:
        route_recipe=TOPO.resolve_recipe(
            registry, route.get("capability"), route.get("capability_mode")
        )
    expected_extensions=_realize_conditional_extensions(
        route_recipe, route.get("effective_intensity")
    )
    if route.get("conditional_extensions") != expected_extensions:
        raise ValueError("route conditional extensions differ from the sealed recipe")
    route_node_ids={node.get("id") for node in route.get("nodes", [])}
    if any(not set(row["after"]) <= route_node_ids for row in expected_extensions):
        raise ValueError("route conditional extension anchor is not realized")
    expected_bindings=json.loads(json.dumps(
        route_recipe["human_gate_bindings"]
        if route.get("effective_intensity") not in ("direct","quick") else []))
    if route.get("human_gate_bindings") != expected_bindings:
        raise ValueError("route human gate bindings differ from the sealed recipe")
    if route.get("workflow_contract") != _workflow_contract(
            registry, route.get("nodes",[]), expected_bindings):
        raise ValueError("route workflow contract differs from the realized stage graph")
    if {row["gate"] for row in expected_bindings} - set(route.get("human_gates") or []):
        raise ValueError("route binds an undeclared human gate")
    if route.get("owner_dispatch_depth") not in {0, 1} or route.get("max_dispatch_depth") not in {0, 1, 2}:
        raise ValueError("invalid qualified dispatch depth")
    if any(key in route for key in ("depth", "owner_depth", "max_depth")):
        raise ValueError("bare route dispatch-depth fields are forbidden")
    allocation = route.get("dispatch_allocation")
    if allocation is not None:
        if not isinstance(allocation, dict) or set(allocation) != {
            "strategy", "window", "harness_order"
        }:
            raise ValueError("invalid dispatch_allocation shape")
        if allocation.get("strategy") not in {
            "config-order", "least-recent-attempts", "capacity-aware"
        }:
            raise ValueError("invalid dispatch_allocation strategy")
        window = allocation.get("window")
        if (
            not isinstance(window, int)
            or window < 0
            or (allocation["strategy"] in {"least-recent-attempts", "capacity-aware"} and window < 3)
        ):
            raise ValueError("invalid dispatch_allocation window")
        order = allocation.get("harness_order")
        if (
            not isinstance(order, list)
            or not order
            or len(order) != len(set(order))
            or any(item not in DEFAULTS.DISPATCHABLE_HARNESSES for item in order)
        ):
            raise ValueError("invalid dispatch_allocation harness order")
    observed_dispatch_depths = [route["owner_dispatch_depth"]]
    effective=route.get("effective_intensity")
    expected_owner_profile=(
        None if effective=="direct"
        else registry["owner_profile_by_intensity"].get(effective)
    )
    if route.get("owner_model_profile") != expected_owner_profile:
        raise ValueError("owner_model_profile differs from the portable intensity policy")
    def validate_harness_policy(policy):
        if policy is None:
            return
        if not isinstance(policy, dict):
            raise ValueError("harness_policy must be a mapping or null")
        flattened = []
        for band in DEFAULTS.QUALITY_BANDS:
            values = policy.get(band)
            if not isinstance(values, list) or any(
                value not in DEFAULTS.DISPATCHABLE_HARNESSES for value in values
            ):
                raise ValueError(f"invalid harness_policy band: {band}")
            flattened.extend(values)
        if len(flattened) != len(set(flattened)):
            raise ValueError("harness_policy repeats a harness across bands")
        threshold = policy.get("promote_relief_below")
        if not isinstance(threshold, int) or not 0 <= threshold <= 100:
            raise ValueError("invalid harness_policy promote_relief_below")
    owner_policy = route.get("owner_harness_policy")
    validate_harness_policy(owner_policy)
    if effective == "direct" and owner_policy is not None:
        raise ValueError("direct route cannot carry owner_harness_policy")
    if owner_policy is not None and allocation is not None:
        owner_set = {
            harness for band in DEFAULTS.QUALITY_BANDS for harness in owner_policy[band]
        }
        if owner_set != set(allocation["harness_order"]):
            raise ValueError("owner_harness_policy differs from dispatch allocation pool")
    realized_groups = {}
    for node in route.get("nodes", []):
        if node.get("kind") == "resource-runner":
            if any(
                key in node
                for key in (
                    "depth", "owner_depth", "max_depth", "dispatch_depth",
                    "transport", "fallback_hops",
                )
            ):
                raise ValueError(f"resource node {node.get('id')} has dispatch attempt fields")
            if node.get("resource_transport") != "detached-process":
                raise ValueError(f"resource node {node.get('id')} lacks detached lifecycle")
            continue
        if node.get("dispatch_depth") in {1, 2}:
            profile = node.get("model_profile")
            row = registry["model_profiles"].get(profile)
            if not isinstance(row, dict) or row.get("registered_topology") is not True:
                raise ValueError(f"node {node.get('id')} has invalid registered model_profile")
        if (
            effective not in ("direct", "quick")
            and node.get("kind") == "capability-owner"
            and node.get("unit") == "_kernel/owner"
            and (
                node.get("dispatch_depth") != 1
                or node.get("model_profile") != expected_owner_profile
            )
        ):
            raise ValueError(
                f"semantic capability owner {node.get('id')} differs from "
                "the portable standard+ owner policy"
            )
        if any(key in node for key in ("depth", "owner_depth", "max_depth")) or node.get("dispatch_depth") not in {0, 1, 2}:
            raise ValueError(f"node {node.get('id')} has invalid dispatch_depth")
        observed_dispatch_depths.append(node["dispatch_depth"])
        if "harness_affinity" in node and node["harness_affinity"] not in VALID_AFFINITY:
            raise ValueError(f"invalid harness_affinity vocabulary: {node['harness_affinity']!r}")
        policy = node.get("harness_policy")
        validate_harness_policy(policy)
        if "execution_surface" in node and node["execution_surface"] not in EXECUTION_SURFACES:
            raise ValueError(f"invalid execution_surface vocabulary: {node['execution_surface']!r}")
        if "registered_worker" in node and not isinstance(node["registered_worker"], bool):
            raise ValueError("registered_worker must be boolean")
        if "dispatch_fallback" in node:
            raise ValueError("legacy dispatch_fallback is read-only")
        for hop in node.get("fallback_hops", []):
            if not isinstance(hop, dict) or hop.get("fallback_hop") not in FALLBACK_HOPS:
                raise ValueError(f"invalid fallback_hop vocabulary: {hop!r}")
        group_id=node.get("parallel_group")
        if group_id:
            if node.get("replica_group") != group_id:
                raise ValueError(f"node {node.get('id')} has inconsistent parallel-group alias")
            realized_groups.setdefault(group_id,[]).append(node)
    expected_group_rows=[]
    for group_id, members in realized_groups.items():
        members.sort(key=lambda member: member.get("parallel_leg_index", -1))
        width=members[0].get("parallel_leg_count") if members else 0
        if width != len(members) or [member.get("parallel_leg_index") for member in members] != list(range(width)):
            raise ValueError(f"parallel group {group_id} has incomplete/duplicate leg indexes")
        invariant_fields=("parallel_group_kind","parallel_join_policy","parallel_independence_axes")
        if any(member.get("parallel_leg_count") != width for member in members):
            raise ValueError(f"parallel group {group_id} width metadata mismatch")
        if any(member.get(field) != members[0].get(field) for member in members for field in invariant_fields):
            raise ValueError(f"parallel group {group_id} invariant metadata mismatch")
        for index,left in enumerate(members):
            for right in members[index+1:]:
                if any(TOPO._overlap(a,b) for a in left.get("write_scope",[]) for b in right.get("write_scope",[])):
                    raise ValueError(f"parallel group {group_id} has overlapping write scopes")
        expected_group_rows.append({
            "id":group_id,
            "kind":members[0]["parallel_group_kind"],
            "join_policy":members[0]["parallel_join_policy"],
            "independence_axes":members[0]["parallel_independence_axes"],
            "width":width,
            "members":[member["id"] for member in members],
        })
    if route.get("parallel_groups") != expected_group_rows:
        raise ValueError("route parallel_groups summary differs from realized nodes")
    if route["max_dispatch_depth"] != max(observed_dispatch_depths):
        raise ValueError("max_dispatch_depth does not match the realized route")
    selection=route.get("selection",{})
    if effective=="direct":
        if (
            route.get("owner_dispatch_depth") != 0
            or route.get("max_dispatch_depth") != 0
            or selection.get("transport") != "interactive"
            or route.get("registered_headless_candidates") is not None
            or route.get("registered_headless_policy") is not None
            or len(route.get("nodes",[])) != 1
        ):
            raise ValueError("direct route shape mismatch")
        node=route["nodes"][0]
        if (
            node.get("id") != "inline"
            or node.get("dispatch_depth") != 0
            or node.get("execution_surface") != "inline"
            or node.get("registered_worker") is not False
            or node.get("fallback_hops")
        ):
            raise ValueError("direct node axes mismatch")
    elif effective=="quick":
        if (
            route.get("owner_dispatch_depth") != 1
            or route.get("max_dispatch_depth") != 1
            or selection.get("transport") != "headless"
            or selection.get("inline_reason") is not None
            or route.get("registered_headless_policy") != "serial-attempt"
            or len(route.get("nodes",[])) != 1
        ):
            raise ValueError("quick route shape mismatch")
        candidates=_validate_registered_headless_evidence({
            "candidates":route.get("registered_headless_candidates")
        })
        if candidates != route.get("registered_headless_candidates"):
            raise ValueError("quick registered-headless evidence is not canonical")
        node=route["nodes"][0]
        if (
            node.get("id") != "one-shot"
            or node.get("dispatch_depth") != 1
            or node.get("unit") != "_kernel/owner"
            or node.get("model_profile") != expected_owner_profile
            or node.get("execution_surface") != "registered-headless"
            or node.get("registered_worker") is not True
            or node.get("fallback_hops")
        ):
            raise ValueError("quick node axes mismatch")
    dd_digest=route.get("dispatch_defaults_digest")
    if dd_digest is not None and (not isinstance(dd_digest, str) or not dd_digest.startswith("sha256:")):
        raise ValueError("invalid dispatch_defaults_digest format")
    _validate_tracking_evidence(route.get("tracking"), route.get("tracked_gate_evidence"))
    escalation=route.get("selection",{}).get("escalation_basis")
    if not isinstance(escalation,list): raise ValueError("escalation_basis missing")
    if any(row.get("signal") in TRACKING for row in escalation if isinstance(row,dict)):
        raise ValueError("tracking cannot be an escalation basis")
    spec_touch=any(_scope_touches_spec(scope) for node in route.get("nodes",[]) for scope in node.get("write_scope",[]))
    if bool(route.get("spec_touch")) != spec_touch: raise ValueError("spec_touch declaration mismatch")
    if route.get("effective_intensity") not in ("direct","quick"):
        if route.get("selection",{}).get("transport") != "headless":
            raise ValueError("standard+ routes require checked headless transport")
        contract_version=route.get("dispatch_contract_version") or route.get("broker_contract_version") or 1
        parent_dispatch_depth=_evidence_parent_dispatch_depth(
            route.get("nodes",[]), route.get("owner_dispatch_depth"))
        checked_dispatch=_validate_dispatch_evidence(
            route.get("dispatch_evidence"), contract_version, parent_dispatch_depth,
            expected_worktree=route.get("cwd"),
            require_scope=scope_version == DISPATCH_EVIDENCE_SCOPE_VERSION)
        expected_chain=_fallback_chain(
            checked_dispatch, contract_version, parent_dispatch_depth,
            expected_worktree=route.get("cwd"),
            require_scope=scope_version == DISPATCH_EVIDENCE_SCOPE_VERSION)
        for node in route.get("nodes",[]):
            if node.get("dispatch_depth")==2:
                chain=_verify_fallback_chain(node, contract_version)
                if chain != expected_chain:
                    raise ValueError(f"dispatch-depth-2 node {node.get('id')} fallback differs from checked evidence")
    return route

def legacy_route_diagnostic(route):
    """Return read-only classification for historical Fleet display."""
    version=route.get("schema_version",1)
    return {
        "route_id":route.get("route_id"),
        "schema_version":version,
        "legacy":version != ROUTE_SCHEMA_VERSION,
        "classification":"version-tagged-read-only-bootstrap" if version != ROUTE_SCHEMA_VERSION else "current",
    }

def write_once(path, payload):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); data=json.dumps(payload,indent=2,ensure_ascii=False)+"\n"
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != data: raise ValueError("immutable route already exists with different content")
        return
    with os.fdopen(fd,"w",encoding="utf-8") as fh: fh.write(data); fh.flush(); os.fsync(fh.fileno())

def completion_dir(route_id):
    return resolve_agent_home()/".dispatch"/"completion"/route_id

# Shared read-only terminal-gate seam: `close_route()` and `workflow-supervisor.py`'s
# `status`/`complete` all need the same four-field marker-identity truth (route id,
# route hash, node id, terminal-gate name, evidence readability, evidence hash), so it
# lives once here and `workflow-supervisor.py` dynamically loads this module rather than
# re-deriving it -- the dependency stays one-way (supervisor -> capability-route).
def terminal_gate_observation(route):
    """Per declared-terminal-node completion-gate truth, verified fresh from disk."""
    nodes={node.get("id"):node for node in route.get("nodes",[])}
    terminal_ids=[node_id for node_id,node in nodes.items() if node.get("terminal") is True]
    rows={}
    for node_id in terminal_ids:
        node=nodes[node_id]
        path=completion_dir(route["route_id"])/f"{node_id}.json"
        try:
            marker=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            rows[node_id]={"passed":False,"reason":"completion-marker-absent"}
            continue
        if (marker.get("route_id") != route.get("route_id")
                or marker.get("route_hash") != route.get("route_hash")
                or marker.get("node_id") != node_id
                or marker.get("completion_gate") != node.get("terminal_gate")):
            rows[node_id]={"passed":False,"reason":"completion-marker-identity-mismatch"}
            continue
        evidence=marker.get("evidence") or {}
        try:
            digest=hashlib.sha256(Path(evidence["path"]).read_bytes()).hexdigest()
        except (OSError,KeyError,TypeError):
            rows[node_id]={"passed":False,"reason":"completion-evidence-unreadable"}
            continue
        if digest != evidence.get("sha256"):
            rows[node_id]={"passed":False,"reason":"completion-evidence-hash-mismatch"}
            continue
        rows[node_id]={"passed":True,"reason":"completion-marker-verified",
                       "evidence":evidence.get("path")}
    return rows

def terminal_gate_proven(gates):
    """Tri-state aggregate: True if every declared terminal gate passed, False if any
    declared terminal gate is unproven, None only when no terminal node is declared."""
    if not gates:
        return None
    return all(row["passed"] for row in gates.values())

# D-2: route lifecycle records have exactly one canonical write location. `.resolve()`
# follows symlinks for every existing path segment, so a `--output` whose parent is a
# symlink pointing outside the canonical directory is classified by its real target, not
# its apparent one.
def canonical_routes_dir(artifact_root):
    return Path(artifact_root).resolve()/".runtime"/"routes"

def classify_route_location(path, artifact_root):
    """canonical | legacy-root | legacy-routes | legacy-_routes | legacy-.routes | outside"""
    resolved=Path(path).resolve()
    root=Path(artifact_root).resolve()
    if resolved.parent == canonical_routes_dir(artifact_root): return "canonical"
    if resolved.parent == root: return "legacy-root"
    if resolved.parent == root/"routes": return "legacy-routes"
    if resolved.parent == root/"_routes": return "legacy-_routes"
    if resolved.parent == root/".routes": return "legacy-.routes"
    return "outside"

_LEGACY_LOCATIONS=("legacy-root","legacy-routes","legacy-_routes","legacy-.routes")
_LOCATION_SORT_PRIORITY={"canonical":0,"legacy-root":1,"legacy-routes":2,"legacy-_routes":3,"legacy-.routes":4,"outside":5}

def atomic_write(path, payload):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    data=json.dumps(payload,indent=2,ensure_ascii=False)+"\n"
    temp=path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as fh: fh.write(data); fh.flush(); os.fsync(fh.fileno())
    os.replace(temp,path)

# v2 adds `registry_current`: a closure recorded against a registry that has since
# changed is still a real closure, but it says so instead of implying currency.
# v3 adds `terminal_gate_proven`/`terminal_gates`: `close` previously validated only
# D-2 location and never consulted whether the workflow's terminal gate actually
# passed, so a route could be closed while `WORKFLOW §0.6`'s completion condition
# stayed false. Absence of these keys (v2 and earlier sidecars) has different
# semantics than an explicit `false` -- readers must not fold the two together.
OUTCOME_SCHEMA_VERSION=3

def outcome_path(route_file):
    path=Path(route_file); return path.with_name(path.stem+".outcome.json")

def _head_commit(cwd):
    probe=subprocess.run(["git","-C",str(cwd),"rev-parse","HEAD"],text=True,capture_output=True)
    return probe.stdout.strip() if probe.returncode==0 else None

# A compiled route says work started; nothing said it finished. `complete` closes a
# registered attempt in the jobs registry, so an inline/direct route — which never
# reaches that registry — left no closure anywhere, and a leftover route file was
# indistinguishable from abandoned work. The route record cannot carry the closure
# itself: `route_hash` covers every field but the hash and id, so any added key makes
# `verify_route` reject it. The closure lives in a sidecar and binds `route_hash`, so a
# recompiled route leaves a detectably stale one rather than a silently wrong one.
def close_route(route, route_file, commit=None, summary=None):
    from datetime import datetime, timezone
    # F7: D-2's single-storage-location contract has a compile-time entrance gate
    # (`route-output-outside-canonical`) but had no exit gate -- `close` would
    # happily write a sidecar next to a route file living anywhere at all. The
    # four legacy locations stay closeable read-only (that's how open records
    # left over from before D-2 get resolved); everywhere else is rejected.
    location=classify_route_location(route_file,route["artifact_root"])
    if location != "canonical" and location not in _LEGACY_LOCATIONS:
        raise ValueError("route-close-outside-canonical-or-legacy")
    target=outcome_path(route_file)
    if target.exists():
        return json.loads(target.read_text(encoding="utf-8")), False
    # Live, not stored: every close computes gate truth fresh from the completion
    # markers on disk. A route closed once is never retroactively reopened to
    # recompute this, so the sidecar's `terminal_gate_proven` reflects gate state at
    # the moment of THIS close, not at any later inspection.
    gates=terminal_gate_observation(route)
    outcome={"schema_version":OUTCOME_SCHEMA_VERSION,
             "route_id":route["route_id"],"route_hash":route["route_hash"],
             "route_file":str(Path(route_file).resolve()),"cwd":route["cwd"],
             "capability":route["capability"],"effective_intensity":route["effective_intensity"],
             "closed_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
             "head_commit":commit or _head_commit(route["cwd"]),"summary":summary,
             "registry_current":route.get("_registry_current",True),
             "route_location":classify_route_location(route_file,route["artifact_root"]),
             "terminal_gate_proven":terminal_gate_proven(gates),"terminal_gates":gates}
    atomic_write(target,outcome)
    return outcome, True

def route_status(artifact_root, *, diagnostics=None):
    """Report every compiled route under one artifact root and whether it is closed.

    Scans the canonical `.runtime/routes/` directory plus four legacy locations
    (root-level `*-route.json`, `routes/`, `_routes/`, `.routes/`) read-only —
    D-2 blocks new writes to the legacy locations but `status` still surfaces
    them so open routes there remain discoverable and closeable.

    `diagnostics` is opt-in: when given a list, every candidate that fails to parse as a
    route (unreadable file, non-dict payload, missing `route_id`/`nodes`) is appended to
    it as `{"path", "location", "reason"}` instead of being silently skipped -- ordinary
    `status` callers that omit `diagnostics` keep today's fail-soft `continue` behavior
    unchanged; the scan itself never terminates on a malformed candidate either way.
    """
    root=Path(artifact_root)
    search_dirs=[canonical_routes_dir(artifact_root),root,root/"routes",root/"_routes",root/".routes"]
    by_route_id={}
    rows=[]
    for search_dir in search_dirs:
        if not search_dir.is_dir(): continue
        for path in sorted(search_dir.glob("*.json")):
            if path.name.endswith(".outcome.json"): continue
            try: raw=json.loads(path.read_text(encoding="utf-8"))
            except (OSError,json.JSONDecodeError,UnicodeDecodeError) as exc:
                if diagnostics is not None:
                    diagnostics.append({"path":str(path),
                                         "location":classify_route_location(path,artifact_root),
                                         "reason":f"route-unreadable:{exc}"})
                continue
            if not isinstance(raw,dict) or "route_id" not in raw or "nodes" not in raw:
                if diagnostics is not None:
                    diagnostics.append({"path":str(path),
                                         "location":classify_route_location(path,artifact_root),
                                         "reason":"route-malformed-missing-required-keys"})
                continue
            location=classify_route_location(path,artifact_root)
            target=outcome_path(path)
            row={"route_file":str(path),"route_id":raw.get("route_id"),
                 "capability":raw.get("capability"),"effective_intensity":raw.get("effective_intensity"),
                 "source_commit":raw.get("source_commit"),"closed":target.is_file(),
                 "location":location,"drift":location != "canonical",
                 "read_only":location in _LEGACY_LOCATIONS}
            if row["closed"]:
                try: closure=json.loads(target.read_text(encoding="utf-8"))
                except (OSError,json.JSONDecodeError,UnicodeDecodeError): closure={}
                row["closed_at"]=closure.get("closed_at"); row["head_commit"]=closure.get("head_commit")
                row["stale_closure"]=closure.get("route_hash")!=raw.get("route_hash")
                row["registry_current"]=closure.get("registry_current",True)
            rows.append(row)
            by_route_id.setdefault(row["route_id"],[]).append(row["route_file"])
    for row in rows:
        locations=by_route_id.get(row["route_id"],[])
        if len(locations) > 1: row["duplicate_locations"]=sorted(locations)
    rows.sort(key=lambda row:(_LOCATION_SORT_PRIORITY.get(row["location"],9),row["route_file"]))
    return rows

def _marker_attempt_axes(node, attempt_id, attempt_metadata):
    if node.get("kind") == "resource-runner":
        if attempt_id or attempt_metadata:
            raise ValueError("resource completion cannot carry agent attempt axes")
        return {
            "attempt_id":None,
            "dispatch_depth":None,
            "transport":None,
            "execution_surface":None,
            "registered_worker":False,
            "fallback_hop":None,
        }
    if attempt_metadata is not None and attempt_metadata.get("stage_authority") == "owner-chain":
        if not attempt_id or not attempt_metadata.get("subsession_manifest"):
            raise ValueError("owner-chain completion identity incomplete")
        return {
            "attempt_id":attempt_id,
            "dispatch_depth":node.get("dispatch_depth"),
            "transport":"headless",
            "execution_surface":"inline",
            "registered_worker":False,
            "fallback_hop":"inline",
            "stage_authority":"owner-chain",
            "subsession_manifest":attempt_metadata["subsession_manifest"],
            "subsession_manifest_sha256":attempt_metadata["subsession_manifest_sha256"],
            "session_chain_id":attempt_metadata["session_chain_id"],
        }
    if attempt_metadata is None:
        if node.get("dispatch_depth") != 0 or node.get("execution_surface") != "inline":
            raise ValueError("current dispatched completion requires exact attempt metadata")
        attempt_metadata={
            "attempt_schema_version":2,
            "dispatch_depth":0,
            "transport":"interactive",
            "execution_surface":"inline",
            "registered_worker":False,
            "fallback_hop":"",
        }
    validate_attempt_metadata(attempt_metadata)
    dispatch_depth=int(attempt_metadata["dispatch_depth"])
    if dispatch_depth != node.get("dispatch_depth"):
        raise ValueError("completion attempt dispatch_depth does not match route node")
    registered=str(attempt_metadata["registered_worker"]).lower() in {"1","true"}
    return {
        "attempt_id":attempt_id,
        "dispatch_depth":dispatch_depth,
        "transport":str(attempt_metadata["transport"]),
        "execution_surface":str(attempt_metadata["execution_surface"]),
        "registered_worker":registered,
        "fallback_hop":str(attempt_metadata.get("fallback_hop") or "") or None,
    }

def _next_marker_sequence(directory, node_id):
    maximum=0
    if directory.is_dir():
        prefix=f"{node_id}."
        for path in directory.glob(f"{node_id}.*.json"):
            middle=path.name[len(prefix):-5]
            if middle.isdigit():
                maximum=max(maximum,int(middle))
    return maximum+1

def write_completion_marker(route, node, node_id, evidence, *, attempt_id=None, attempt_metadata=None):
    directory=completion_dir(route["route_id"])
    canonical_path=directory/f"{node_id}.json"
    sha=hashlib.sha256(evidence.read_bytes()).hexdigest()
    axes=_marker_attempt_axes(node, attempt_id, attempt_metadata)
    identity={
        "evidence_sha256":sha,
        **axes,
    }
    if canonical_path.is_file():
        existing=json.loads(canonical_path.read_text(encoding="utf-8"))
        existing_identity={
            "evidence_sha256":existing.get("evidence",{}).get("sha256"),
            **{key:existing.get(key) for key in axes},
        }
        if existing_identity==identity:
            static_identity={
                "schema_version":2,
                "route_id":route["route_id"],
                "route_hash":route["route_hash"],
                "registry_digest":route["registry_digest"],
                "node_id":node_id,
                "completion_gate":node["completion_gate"],
            }
            if any(existing.get(key)!=value for key,value in static_identity.items()):
                raise ValueError("canonical completion marker identity conflict")
            history_path=directory/f"{node_id}.{existing.get('sequence')}.json"
            if (
                not history_path.is_file()
                or json.loads(history_path.read_text(encoding="utf-8"))!=existing
            ):
                raise ValueError("canonical completion marker history conflict")
            return existing
    sequence=_next_marker_sequence(directory,node_id)
    marker={
        "schema_version":2,
        "route_id":route["route_id"],"route_hash":route["route_hash"],
        "registry_digest":route["registry_digest"],"node_id":node_id,
        **axes,
        "completion_gate":node["completion_gate"],
        "evidence":{"path":str(evidence),"sha256":sha},
        "sequence":sequence,
    }
    from datetime import datetime, timezone
    marker["completed_at"]=datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
    while True:
        history_path=directory/f"{node_id}.{sequence}.json"
        try:
            write_once(history_path, marker)
        except ValueError:
            sequence+=1; marker["sequence"]=sequence; continue
        break
    atomic_write(canonical_path, marker)
    return marker

def _find_attempt_row_status(jobs, attempt_id):
    """Return the row status ('open'|'running'|'done') for attempt_id, or None if absent."""
    if not jobs.is_file(): return None
    for line in jobs.read_text(encoding="utf-8", errors="replace").splitlines():
        fields=line.split("\t")
        if len(fields)!=6: continue
        metadata=dict(part.split("=",1) for part in fields[5].split(",") if "=" in part)
        if metadata.get("attempt_id")==attempt_id: return fields[1]
    return None

def _find_attempt_row_metadata(jobs, attempt_id):
    if not jobs.is_file(): return None
    for line in jobs.read_text(encoding="utf-8",errors="replace").splitlines():
        fields=line.split("\t")
        if len(fields)!=6: continue
        metadata=parse_registry_metadata(fields[5])
        if metadata.get("attempt_id")==attempt_id:
            metadata["_status"]=fields[1]
            return metadata
    return None

@contextlib.contextmanager
def _exclusive_lock(path):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("a",encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

def _attempt_completion_path(route, node_id, attempt_id):
    safe_attempt="".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in attempt_id
    )
    return completion_dir(route["route_id"])/f"{node_id}.{safe_attempt}.attempt.json"

def _publish_completion_locked(
    route,
    node,
    node_id,
    evidence,
    *,
    attempt_id,
    attempt_metadata,
    require_existing_link=False,
):
    """Publish marker history, exact-attempt link, and canonical marker under one node lock."""

    axes=_marker_attempt_axes(node,attempt_id,attempt_metadata)
    evidence_sha=hashlib.sha256(evidence.read_bytes()).hexdigest()
    attempt_path=(
        _attempt_completion_path(route,node_id,attempt_id)
        if attempt_id else None
    )
    marker=None
    if attempt_path and attempt_path.is_file():
        existing_link=json.loads(attempt_path.read_text(encoding="utf-8"))
        expected_link_identity={
            "schema_version":2,
            "route_id":route["route_id"],
            "node_id":node_id,
            "attempt_id":attempt_id,
            **axes,
            "evidence_sha256":evidence_sha,
        }
        actual_link_identity={
            key:existing_link.get(key) for key in expected_link_identity
        }
        if actual_link_identity != expected_link_identity:
            raise ValueError("immutable attempt completion differs from existing link")
        history_path=Path(existing_link.get("completion_marker_history",""))
        if not history_path.is_file():
            raise ValueError("immutable attempt completion history is missing")
        marker=json.loads(history_path.read_text(encoding="utf-8"))
        marker_identity={
            "schema_version":marker.get("schema_version"),
            "route_id":marker.get("route_id"),
            "node_id":marker.get("node_id"),
            "attempt_id":marker.get("attempt_id"),
            **{key:marker.get(key) for key in axes if key!="attempt_id"},
            "evidence_sha256":marker.get("evidence",{}).get("sha256"),
        }
        if marker_identity != expected_link_identity:
            raise ValueError("immutable attempt completion history differs from link")
        expected_history_path=completion_dir(route["route_id"])/f"{node_id}.{marker.get('sequence')}.json"
        if history_path!=expected_history_path:
            raise ValueError("immutable attempt completion history path differs from link")
        marker_static={
            "route_hash":route["route_hash"],
            "registry_digest":route["registry_digest"],
            "completion_gate":node["completion_gate"],
            "evidence_path":str(evidence),
        }
        actual_static={
            "route_hash":marker.get("route_hash"),
            "registry_digest":marker.get("registry_digest"),
            "completion_gate":marker.get("completion_gate"),
            "evidence_path":marker.get("evidence",{}).get("path"),
        }
        if actual_static!=marker_static:
            raise ValueError("immutable attempt completion route identity differs from link")
    elif require_existing_link:
        raise ValueError("completed attempt row lacks immutable completion link")

    if marker is None:
        marker=write_completion_marker(
            route,node,node_id,evidence,
            attempt_id=attempt_id,
            attempt_metadata=attempt_metadata,
        )
    if not attempt_id:
        return marker

    canonical_marker_path=completion_dir(route["route_id"])/f"{node_id}.json"
    history_marker_path=completion_dir(route["route_id"])/f"{node_id}.{marker['sequence']}.json"
    attempt_link={
        "schema_version":2,
        "route_id":route["route_id"],"node_id":node_id,"attempt_id":attempt_id,
        "dispatch_depth":marker["dispatch_depth"],
        "transport":marker["transport"],
        "execution_surface":marker["execution_surface"],
        "registered_worker":marker["registered_worker"],
        "fallback_hop":marker["fallback_hop"],
        "evidence_sha256":marker["evidence"]["sha256"],
        "completion_marker":str(canonical_marker_path),
        "completion_marker_history":str(history_marker_path),
    }
    write_once(attempt_path,attempt_link)
    current_marker=json.loads(canonical_marker_path.read_text(encoding="utf-8"))
    if current_marker==marker:
        atomic_write(
            completion_dir(route["route_id"])/f"{node_id}.attempt.json",
            attempt_link,
        )
    return marker

def complete_node(
    route,
    node,
    node_id,
    evidence,
    jobs=None,
    attempt_id=None,
    explicit_attempt_metadata=None,
):
    """Atomically publish one exact-attempt completion and close only its row."""
    if jobs and not attempt_id:
        raise ValueError("registered completion requires --attempt-id")
    if not jobs and attempt_id and explicit_attempt_metadata is None:
        raise ValueError("unregistered completion requires explicit attempt metadata")
    if not jobs and explicit_attempt_metadata is not None and not attempt_id:
        raise ValueError("explicit attempt metadata requires --attempt-id")

    jobs_path=Path(jobs) if jobs else None
    directory=completion_dir(route["route_id"])
    node_lock=directory/f".{node_id}.completion.lock"
    with _exclusive_lock(node_lock):
        if not jobs_path:
            marker=_publish_completion_locked(
                route,node,node_id,evidence,
                attempt_id=attempt_id,
                attempt_metadata=explicit_attempt_metadata,
            )
            status="unregistered-complete" if attempt_id else None
            return marker, ({"attempt_id":attempt_id,"status":status} if status else None)

        try:
            ensure_global_registry_writable(jobs_path)
        except DispatchContractError as exc:
            if explicit_attempt_metadata is not None:
                _publish_completion_locked(
                    route,node,node_id,evidence,
                    attempt_id=attempt_id,
                    attempt_metadata=explicit_attempt_metadata,
                )
            raise ValueError(f"row-close-failed:{exc.reason}") from exc
        with _exclusive_lock(Path(f"{jobs_path}.lock")):
            lines=jobs_path.read_text(encoding="utf-8",errors="replace").splitlines()
            row_index=None
            row_fields=None
            row_metadata=None
            for index,line in enumerate(lines):
                fields=line.split("\t")
                if len(fields)!=6:
                    continue
                metadata=parse_registry_metadata(fields[5])
                if metadata.get("attempt_id")==attempt_id:
                    row_index=index; row_fields=fields; row_metadata=metadata
                    break
            if row_fields is None or row_metadata is None:
                if explicit_attempt_metadata is not None:
                    _publish_completion_locked(
                        route,node,node_id,evidence,
                        attempt_id=attempt_id,
                        attempt_metadata=explicit_attempt_metadata,
                    )
                raise ValueError(
                    f"attempt-row-absent:{attempt_id}; exact fallback attempt metadata required"
                )
            try:
                validate_attempt_metadata(row_metadata)
            except DispatchContractError as exc:
                raise ValueError(f"row-contract-invalid:{exc.reason}") from exc
            if row_metadata.get("subsession_id") or str(row_metadata.get("stage_authority", "1")).lower() in {"0", "false"}:
                raise ValueError("subsession-has-no-stage-gate-authority")
            if (
                row_metadata.get("route_id") != route["route_id"]
                or row_metadata.get("route_hash") != route["route_hash"]
                or row_metadata.get("route_node") != node_id
            ):
                raise ValueError("attempt row route identity mismatch")
            if explicit_attempt_metadata is not None:
                axis_keys=(
                    "attempt_schema_version","dispatch_depth","transport",
                    "execution_surface","registered_worker","fallback_hop",
                )
                row_axes={key:str(row_metadata.get(key,"")).lower() for key in axis_keys}
                explicit_axes={key:str(explicit_attempt_metadata.get(key,"")).lower() for key in axis_keys}
                if row_axes != explicit_axes:
                    raise ValueError("explicit attempt metadata differs from canonical row")
            if row_fields[1] not in {"open","running","done"}:
                raise ValueError(f"attempt-row-terminal:{row_fields[1]}")
            already_closed=row_fields[1]=="done"
            row_note=row_metadata.get("note")
            # SD-94 — supervisor-delivered completion closes the exact row BEFORE `complete`
            # runs, so SD-70's "complete closes the row" order never happens on that path.
            # A checked supervisor terminal (note=completed-supervisor) carrying a success
            # verdict (failure_class=pass) is marker-eligible: publish the marker and append
            # its evidence to THIS row only, leaving the `done` status untouched. Every other
            # terminal note, and any non-pass verdict, keeps the fail-closed refusal.
            marker_eligible=(
                already_closed
                and row_note=="completed-supervisor"
                and row_metadata.get("failure_class")=="pass"
            )
            if already_closed and row_note!="completed-marker" and not marker_eligible:
                raise ValueError(
                    f"attempt-row-terminal-without-completion:{row_note or 'unknown'}"
                )
            attempt_metadata={
                key:value for key,value in row_metadata.items()
                if not key.startswith("_")
            }
            marker=_publish_completion_locked(
                route,node,node_id,evidence,
                attempt_id=attempt_id,
                attempt_metadata=attempt_metadata,
                require_existing_link=already_closed and not marker_eligible,
            )
            if already_closed and not marker_eligible:
                return marker, {"attempt_id":attempt_id,"status":"already-closed"}

            canonical_marker_path=directory/f"{node_id}.json"
            history_marker_path=directory/f"{node_id}.{marker['sequence']}.json"
            # SD-94: `done` for a marker-eligible row is a no-op re-assert, never a re-close —
            # the supervisor's own terminal stays the row's closing act, and the appended
            # note/marker evidence only records that the marker now exists. A duplicate
            # `complete` then reads note=completed-marker (last value wins) and returns the
            # idempotent already-closed path instead of appending twice.
            row_fields[1]="done"
            row_fields[5] += (
                f",note=completed-marker,completion_marker={canonical_marker_path}"
                f",completion_marker_history={history_marker_path}"
            )
            lines[row_index]="\t".join(row_fields)
            _atomic_registry_replace(jobs_path,lines)
            return marker, {
                "attempt_id":attempt_id,
                "status":"marker-appended" if marker_eligible else "closed",
            }

def complete_subsession_stage(route, node, node_id, evidence, manifest_path, jobs):
    """Aggregate exact PASS sub-sessions into the route node's one stage marker."""

    # The manifest itself binds the actual route file; compare id/hash/node/gate
    # here, then use its immutable digest as marker identity.
    manifest=load_manifest(manifest_path,node=node)
    if (
        manifest.get("route_id") != route.get("route_id")
        or manifest.get("route_hash") != route.get("route_hash")
        or Path(manifest["route_file"]).resolve() != Path(route.get("_route_file", manifest["route_file"])).resolve()
    ):
        raise ValueError("subsession manifest route identity mismatch")
    jobs_path=Path(jobs)
    if not jobs_path.is_file():
        raise ValueError("subsession registry missing")
    rows={}
    for line in jobs_path.read_text(encoding="utf-8",errors="replace").splitlines():
        fields=line.split("\t")
        if len(fields)!=6:
            continue
        metadata=parse_registry_metadata(fields[5])
        attempt_id=metadata.get("attempt_id")
        if attempt_id:
            rows.setdefault(attempt_id,[]).append((fields,metadata))
    for session in manifest["sessions"]:
        matches=rows.get(session["attempt_id"],[])
        if len(matches)!=1:
            raise ValueError(f"subsession attempt row count invalid:{session['attempt_id']}:{len(matches)}")
        fields,metadata=matches[0]
        validate_attempt_metadata(metadata)
        expected={
            "route_id":route["route_id"], "route_node":node_id,
            "subsession_id":session["subsession_id"],
            "session_chain_id":manifest["chain_id"], "stage_authority":"0",
        }
        if any(str(metadata.get(key,""))!=str(value) for key,value in expected.items()):
            raise ValueError(f"subsession attempt identity mismatch:{session['attempt_id']}")
        if (
            fields[1]!="done"
            or metadata.get("note") not in {"completed-supervisor", "completed-marker"}
            or metadata.get("failure_class")!="pass"
        ):
            raise ValueError(f"subsession not semantic PASS:{session['attempt_id']}")
        process=attempt_process_quiescence(metadata)
        if process.state!="quiescent":
            raise ValueError(f"subsession process not quiescent:{session['attempt_id']}:{process.reason}")
    digest=manifest["_manifest_sha256"]
    attempt_id="att-stage-"+digest[:32]
    metadata={
        "stage_authority":"owner-chain",
        "subsession_manifest":str(Path(manifest_path).resolve()),
        "subsession_manifest_sha256":digest,
        "session_chain_id":manifest["chain_id"],
    }
    directory=completion_dir(route["route_id"])
    with _exclusive_lock(directory/f".{node_id}.completion.lock"):
        marker=write_completion_marker(
            route,node,node_id,evidence,
            attempt_id=attempt_id,attempt_metadata=metadata,
        )
    return marker,{"status":"stage-gate-aggregated","sessions":len(manifest["sessions"])}

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="command",required=True)
    c=sub.add_parser("compile"); c.add_argument("--capability",required=True); c.add_argument("--capability-mode",default="default")
    c.add_argument("--intensity",default="auto"); c.add_argument("--cwd",required=True); c.add_argument("--artifact-root",required=True)
    c.add_argument("--predicate",action="append",default=[]); c.add_argument("--signal",action="append",default=[])
    c.add_argument("--transport",default=None); c.add_argument("--transport-evidence",default="caller-selected")
    c.add_argument("--inline-reason"); c.add_argument("--tracking",choices=sorted(TRACKING),required=True)
    c.add_argument("--dispatch-evidence",help="JSON file with checked nested tuples/native evidence")
    c.add_argument("--registered-headless-evidence",help="JSON file with checked quick candidates")
    c.add_argument("--composed-recipe",help="JSON file with a compose-on-demand recipe (sealed composed: true)")
    c.add_argument("--spec-read",required=True); c.add_argument("--drift-verdict",required=True)
    c.add_argument("--workflow-mode",choices=sorted(TRACKING),required=True); c.add_argument("--artifact-guard",required=True)
    c.add_argument("--output")
    v=sub.add_parser("verify"); v.add_argument("--route",required=True); v.add_argument("--cwd")
    n=sub.add_parser("node"); n.add_argument("--route",required=True); n.add_argument("--node",required=True)
    d=sub.add_parser("complete"); d.add_argument("--route",required=True); d.add_argument("--node",required=True); d.add_argument("--evidence",required=True); d.add_argument("--output")
    d.add_argument("--jobs",help="canonical registry path for a registered attempt")
    d.add_argument("--attempt-id",help="exact current attempt id")
    d.add_argument("--dispatch-depth",type=int)
    d.add_argument("--transport")
    d.add_argument("--execution-surface")
    d.add_argument("--registered-worker",choices=("0","1","false","true"))
    d.add_argument("--fallback-hop")
    d.add_argument("--subsession-manifest",help="aggregate declared sub-sessions into this one stage gate")
    cl=sub.add_parser("close"); cl.add_argument("--route",required=True)
    cl.add_argument("--commit",help="result commit; defaults to HEAD in the route cwd")
    cl.add_argument("--summary",help="one line naming what the route produced")
    st=sub.add_parser("status"); st.add_argument("--artifact-root",required=True)
    st.add_argument("--open-only",action="store_true",help="list only routes with no recorded outcome")
    a=p.parse_args()
    if a.command=="compile":
        gate={"spec_read":{"satisfied":a.spec_read.lower() not in ("0","false","no"),"source":a.spec_read},
              "drift_verdict":a.drift_verdict,"workflow_mode":a.workflow_mode,
              "artifact_guard":{"satisfied":a.artifact_guard.lower() not in ("0","false","no"),"source":a.artifact_guard}}
        dispatch_evidence=json.loads(Path(a.dispatch_evidence).read_text()) if a.dispatch_evidence else None
        registered_headless_evidence=(
            json.loads(Path(a.registered_headless_evidence).read_text())
            if a.registered_headless_evidence else None
        )
        if a.composed_recipe:
            composed_recipe=json.loads(Path(a.composed_recipe).read_text())
            if composed_recipe.get("capability") != a.capability:
                raise ValueError("composed recipe capability differs from --capability")
            route=compile_composed_route(
                composed_recipe,a.capability_mode,a.intensity,a.cwd,a.artifact_root,
                predicates=a.predicate,signals=a.signal,transport=a.transport,
                transport_evidence=a.transport_evidence,inline_reason=a.inline_reason,
                tracking=a.tracking,tracked_gate_evidence=gate,
                dispatch_evidence=dispatch_evidence,
                registered_headless_evidence=registered_headless_evidence,
            )
        else:
            route=compile_route(
                a.capability,a.capability_mode,a.intensity,a.cwd,a.artifact_root,
                a.predicate,a.signal,a.transport,a.transport_evidence,a.inline_reason,
                a.tracking,gate,dispatch_evidence,registered_headless_evidence,
            )
        if a.output:
            output_path=Path(a.output)
            if classify_route_location(output_path,a.artifact_root) != "canonical":
                raise ValueError("route-output-outside-canonical")
        else:
            output_path=canonical_routes_dir(a.artifact_root)/f"{route['route_id']}.json"
        write_once(output_path,route)
        print(f"route_file={output_path.resolve()}",file=sys.stderr)
        print(json.dumps(route,sort_keys=True))
    elif a.command=="status":
        rows=route_status(a.artifact_root)
        if a.open_only: rows=[row for row in rows if not row["closed"]]
        print(json.dumps(rows,sort_keys=True,indent=2))
    else:
        route=verify_route(
            json.loads(Path(a.route).read_text()), getattr(a,"cwd",None),
            allow_stale_registry=a.command=="close",
        )
        if a.command=="verify": print(f"route_id={route['route_id']}\nroute_hash={route['route_hash']}")
        elif a.command=="close":
            outcome,created=close_route(route,a.route,a.commit,a.summary)
            print(json.dumps(outcome,sort_keys=True))
            if not created: print("capability-route: route already closed",file=sys.stderr)
            if outcome.get("terminal_gate_proven") is False:
                reasons={node_id:row.get("reason") for node_id,row in
                         (outcome.get("terminal_gates") or {}).items() if not row.get("passed")}
                print(f"capability-route: terminal-gate-unproven route_id={outcome['route_id']} "
                      f"reasons={json.dumps(reasons,sort_keys=True)}",file=sys.stderr)
        else:
            node=next((x for x in route["nodes"] if x["id"]==a.node),None)
            if not node: raise SystemExit("unknown route node")
            if a.command=="node": print(json.dumps(node,sort_keys=True))
            else:
                evidence=Path(a.evidence).resolve()
                if not evidence.is_file(): raise SystemExit("completion evidence missing")
                raw_axes=(a.dispatch_depth,a.transport,a.execution_surface,a.registered_worker,a.fallback_hop)
                explicit_attempt_metadata=None
                if any(value is not None for value in raw_axes):
                    explicit_attempt_metadata={
                        "attempt_schema_version":2,
                        "dispatch_depth":a.dispatch_depth,
                        "transport":a.transport,
                        "execution_surface":a.execution_surface,
                        "registered_worker":a.registered_worker,
                        "fallback_hop":a.fallback_hop,
                    }
                if a.subsession_manifest:
                    if not a.jobs or a.attempt_id or explicit_attempt_metadata is not None:
                        raise ValueError("subsession completion requires --jobs and forbids attempt axes")
                    route["_route_file"]=str(Path(a.route).resolve())
                    marker,row=complete_subsession_stage(
                        route,node,a.node,evidence,a.subsession_manifest,a.jobs,
                    )
                else:
                    marker,row=complete_node(
                        route,node,a.node,evidence,
                        jobs=a.jobs,
                        attempt_id=a.attempt_id,
                        explicit_attempt_metadata=explicit_attempt_metadata,
                    )
                if a.output: atomic_write(a.output, marker)
                print(json.dumps(marker,sort_keys=True))
                if row: print(json.dumps(row,sort_keys=True))

if __name__=="__main__":
    try: main()
    except (ValueError,TOPO.TopologyError) as exc: print(f"capability-route: {exc}",file=sys.stderr); raise SystemExit(64)
