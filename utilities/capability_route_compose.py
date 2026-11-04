"""Capability route: compose recipes.

Intensity shapes, graph specs, `compose_subgraph_recipe`, compose readiness,
spec reads, peer sources, and selection pins.

Split out of `capability-route.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path

import harness_state_roots as HARNESS_STATE_ROOTS
import route_identity as ROUTE_IDENTITY
import route_plan as ROUTE_PLAN
import route_authority as ROUTE_AUTHORITY
import gpu_execution_sandbox as GPU_SANDBOX
from dispatch_contract import DispatchContractError, stable_state_root

from capability_route_base import (
    _git, _route_chain_identity, _verify_selection_pins, DEFAULTS, ORDER, ROOT, route_hash,
    SELECTION_PIN_CONTRACT_VERSION, SELECTION_PIN_EFFORT, SELECTION_PIN_MODEL,
    SELECTION_PIN_TARGETS, TOPO, UI_TEXT,
)

from capability_route_continuation import (
    worktree_mutating_scope,
)


# ---------------------------------------------------------------------------
# compose: the preset-free work route (SD-135).
#
# `compile` asks the caller to pick a registry recipe by entry-router trigger
# and to restate ~15 flags plus evidence files by hand. `compose` inverts that:
# the caller names the *shape* of the work (direct / solo / staged) and, for a
# staged shape, the stage subgraph it actually wants; every other flag and both
# evidence probes default from the checkout and the registry. The result goes
# through the SAME validator, sealer, verifier and guards as a preset route --
# composition changes route shape only (WORKFLOW §7 compose-on-demand).
# ---------------------------------------------------------------------------
COMPOSE_SHAPES = ("direct", "solo", "staged", "framed")
SHAPE_INTENSITY = ROUTE_PLAN.SHAPE_INTENSITY
# The compiler-internal capability behind `--shape framed`. No person and no model names it; a
# route of any other shape can never seal it (`compose-shape-invalid`).
ROUTE_FRAME_CAPABILITY = TOPO.ROUTE_FRAME_CAPABILITY
INTENSITY_SHAPE = {"direct": "direct", "quick": "solo"}
ROUTE_ORIGINS = ("preset", "compose")
COMPOSE_DEFAULT_CAPABILITY = "autopilot-code"
# Fallback when the user's policy names no enabled harnesses (no file, unreadable):
# the shipped default enables all three.
COMPOSE_DEFAULT_CHILDREN = ("claude", "codex", "opencode")


def shape_for_intensity(effective):
    return INTENSITY_SHAPE.get(effective, "staged")


def parse_graph_spec(text, capabilities=None):
    """`execute,test,report` or `execute:dev/refactor,test` -> [(id, unit|None)].

    SD-165: when the first segment names a registered capability the token is a
    part id, `autopilot-lab:diagnose` or `autopilot-lab:metrics:qa/ml-debug`,
    and the returned key is `capability:stage`. Every other token parses as
    before.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("compose-graph-empty")
    rows = []
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            raise ValueError("compose-graph-empty-node")
        node_id, _, unit = item.partition(":")
        node_id = node_id.strip()
        if not re.fullmatch(r"[a-z][a-z0-9-]*", node_id):
            raise ValueError(f"compose-graph-node-invalid:{node_id}")
        if unit and capabilities and node_id in capabilities:
            stage, _, unit = unit.partition(":")
            stage = stage.strip()
            if not re.fullmatch(r"[a-z][a-z0-9-]*", stage):
                raise ValueError(f"compose-graph-node-invalid:{node_id}:{stage}")
            node_id = f"{node_id}:{stage}"
        rows.append((node_id, unit.strip() or None))
    ids = [row[0] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("compose-graph-duplicate-node")
    return rows


def _compose_inputs(base_nodes, base_node, kept, find_input=None, alternates=None):
    """Keep the base node's declared inputs wherever they can still exist.

    An input stays when it is not produced by any recipe node (an external
    literal such as `task`/`spec`/`source`), when its producer is a kept
    node, or when it is a semantic token of the tree (`source-diff`, …) that
    no node has to author. An input whose only producer was dropped is
    removed -- the stage brief (`dispatch_stage_advance.render_stage_brief`)
    prints `inputs` verbatim, so a dropped node's file must not be promised.
    Nothing is added: the chain edge lives in `depends_on`, and a full-graph
    compose must yield exactly the preset's inputs. (Canary review round 1
    B1, round 2 M2.) SD-163: when `find_input(name)` finds a same-named prior
    output, the input stays and its source is returned in the second value.
    SD-165: `alternates(name, dropped_producers | None)` names further prior
    outputs to look for -- the relocated name of a borrowed part, or the part
    output the catalog maps an external input to. The input's own name is
    always tried first, so a sealed source replays through `sealed.get`.
    """
    producers = {}
    for candidate in base_nodes.values():
        for output in candidate.get("outputs") or []:
            producers.setdefault(output, set()).add(candidate["id"])
    inputs, sources = [], {}
    for item in base_node.get("inputs") or []:
        owners = producers.get(item)
        if owners is None or owners & kept or TOPO._is_semantic_output(item):
            if item not in inputs:
                inputs.append(item)
            names = alternates(item, None) if alternates is not None and owners is None else ()
        else:
            names = (item, *(alternates(item, owners) if alternates is not None else ()))
        if find_input is None:
            continue
        source = next((found for name in names if (found := find_input(name)) is not None), None)
        if source is not None:
            if item not in inputs:
                inputs.append(item)
            sources[item] = source
    return inputs or ["task"], sources


def _compose_graph_order_violation(base_nodes, ids):
    """First caller-order violation in a `--graph` selection (D9): a consumer
    named before a producer it transitively `depends_on` in the base recipe.
    Two nodes with no ancestor relationship either way (parallel/alternative
    siblings, e.g. `frame`/`frame-alternative`) may appear in either order --
    only `compose_subgraph_recipe` re-links edges in the CALLER's order, so
    nothing before this check ever refused a graph that silently reordered a
    real dependency (e.g. `test,execute` sealed with `test` running before the
    `execute` output it reads).

    Returns `(consumer, producer)` for the first violation found scanning
    `ids` left to right, or `None` when the order is consistent.
    """
    base_order = list(base_nodes)
    ancestor_cache: dict[str, set[str]] = {}

    def ancestors(node_id):
        cached = ancestor_cache.get(node_id)
        if cached is not None:
            return cached
        ancestor_cache[node_id] = set()  # defend against a cyclic recipe
        result: set[str] = set()
        for parent in base_nodes[node_id].get("depends_on") or []:
            result.add(parent)
            result |= ancestors(parent)
        ancestor_cache[node_id] = result
        return result

    position = {node_id: index for index, node_id in enumerate(ids)}
    for index, node_id in enumerate(ids):
        selected_ancestors = ancestors(node_id) & position.keys()
        for ancestor in sorted(selected_ancestors, key=base_order.index):
            if position[ancestor] > index:
                return node_id, ancestor
    return None


def _unarbitrated_auxiliary_legs(registry, group, anchor, consumers):
    """Suffixes of a borrowed group's auxiliary legs that no selected node arbitrates.

    Same rule `_validate_gate_contracts` applies to a recipe: a review anchor is
    merged by the conductor; a map or pipeline anchor needs exactly one direct
    consumer whose gate declares `auxiliary_arbiter`.
    """
    auxiliary = [leg["suffix"] for leg in group["legs"] if leg.get("leg_class") == "auxiliary"]
    if not auxiliary or anchor["kind"] == "review-worker":
        return []
    if anchor["kind"] == "pipeline-stage":
        consumers = [node for node in consumers if node["kind"] == "review-worker"]
    contracts = registry.get("completion_gate_contracts") or {}
    if len(consumers) == 1 and (contracts.get(consumers[0]["completion_gate"]) or {}).get(
            "auxiliary_arbiter") is True:
        return []
    return auxiliary


def _compose_unarbitrated_groups(registry, recipe):
    nodes = recipe["standard_plus"]["nodes"]
    for group in recipe["standard_plus"].get("parallel_groups", []):
        anchor = next(node for node in nodes if node["id"] == group["node"])
        consumers = [node for node in nodes if anchor["id"] in node.get("depends_on", [])]
        missing = _unarbitrated_auxiliary_legs(registry, group, anchor, consumers)
        if missing:
            yield {"id": group["id"], "reason": "auxiliary-arbiter-not-selected", "legs": missing}


def _complete_compose_group_consumers(registry, base, graph, find_input, **kwargs):
    """Restore missing safe registry consumers through the ordinary part assembler.

    Replaying the original selection remains possible: historical recipes do not
    ask for this completion, and new recipes record the inserted nodes rather
    than treating them as caller-selected stages.
    """
    recipe = compose_subgraph_recipe(registry, base, graph, find_input, **kwargs)
    original = json.loads(json.dumps(recipe["compose"]))
    additions = []
    contracts = registry.get("completion_gate_contracts") or {}
    while True:
        nodes = {node["id"]: node for node in recipe["standard_plus"]["nodes"]}
        completed = False
        pending = recipe["compose"].get("omitted_parallel_presets", []) + list(
            _compose_unarbitrated_groups(registry, recipe))
        for omission in pending:
            anchor = nodes.get(omission["id"])
            if anchor is None:
                declared = next((row for row in base["standard_plus"].get("parallel_groups", [])
                                 if row["id"] == omission["id"]), {})
                anchor = nodes.get(declared.get("node"))
            if anchor is None:
                continue
            part_id = anchor.get("part")
            found = TOPO.part_recipe(registry, part_id) if part_id else (base, anchor)
            if found is None:
                continue
            origin, source = found
            group = next((row for row in origin["standard_plus"].get("parallel_groups", [])
                          if row["node"] == source["id"]), None)
            if group is None or ORDER[kwargs["group_intensity"]] < ORDER[group["min_intensity"]]:
                continue
            reason = omission["reason"]
            if reason == "terminal-anchor" and anchor["kind"] == "pipeline-stage":
                reason = "review-consumer-not-selected"
            if reason not in ("review-consumer-not-selected", "auxiliary-arbiter-not-selected"):
                continue
            consumers = [node for node in origin["standard_plus"]["nodes"]
                         if source["id"] in node.get("depends_on", [])]
            if anchor["kind"] == "pipeline-stage":
                consumers = [node for node in consumers if node["kind"] == "review-worker"]
            if any(leg.get("leg_class") == "auxiliary" for leg in group["legs"]):
                consumers = [node for node in consumers
                             if contracts[node["completion_gate"]].get("auxiliary_arbiter") is True]
            if len(consumers) != 1:
                continue
            consumer = consumers[0]
            if (consumer["kind"] not in ("review-worker", "pipeline-stage")
                    or consumer.get("resource_class") != "normal"
                    or consumer.get("dispatch_depth") != 2 or consumer.get("commit_expected")
                    or any(worktree_mutating_scope(scope) for scope in consumer["write_scope"])
                    or (consumer.get("continuation") or {}).get("kind") != "inline-next"):
                continue
            key = f"{origin['capability']}:{consumer['id']}" if part_id else consumer["id"]
            consumer_id = key.replace(":", "-") if part_id else key
            if consumer_id in nodes or part_id and TOPO.resolve_shared_part(registry, base, key) is None:
                continue  # Never duplicate or move a caller-selected stage.
            selected = recipe["compose"]["graph"]
            anchor_key = part_id or anchor["id"]
            if anchor_key not in selected:
                continue
            augmented = list(selected)
            augmented.insert(augmented.index(anchor_key) + 1, key)
            overrides = recipe["compose"]["unit_overrides"]
            try:
                candidate = compose_subgraph_recipe(registry, base,
                    [(stage, overrides.get(stage)) for stage in augmented], find_input, **kwargs)
            except ValueError:
                continue  # A declared consumer may conflict with a selected dependency.
            if any(row["id"] == omission["id"]
                   for row in candidate["compose"].get("omitted_parallel_presets", []) + list(
                       _compose_unarbitrated_groups(registry, candidate))):
                continue  # Declared ordering/extra stages may make completion unsafe.
            additions.append({"id": consumer_id, "reason": reason, "anchors": [anchor["id"]],
                              "intensity": kwargs["group_intensity"]})
            recipe, completed = candidate, True
            break
        if not completed:
            break
    # Host groups historically kept auxiliary declarations even without their
    # arbiter and then failed topology validation. Strong+ uses the same safe
    # omission as borrowed groups when the declared consumer cannot be restored.
    missing = list(_compose_unarbitrated_groups(registry, recipe))
    for omission in missing:
        group = next(row for row in recipe["standard_plus"]["parallel_groups"] if row["id"] == omission["id"])
        group["legs"] = [leg for leg in group["legs"] if leg["suffix"] not in omission["legs"]]
        group["width_by_intensity"] = {
            tier: min(width, len(group["legs"])) for tier, width in group["width_by_intensity"].items()}
        recipe["compose"].setdefault("omitted_parallel_presets", []).append(omission)
    if missing:
        # Assembly metadata only, so this new omission can replay without
        # reinterpreting historical graphs that never requested completion.
        recipe["compose"]["group_completion_intensity"] = kwargs["group_intensity"]
    if additions:
        recipe["compose"]["graph"] = original["graph"]
        recipe["compose"]["unit_overrides"] = original["unit_overrides"]
        recipe["compose"]["auto_completed_nodes"] = additions + recipe["compose"].get("auto_completed_nodes", [])
    if (additions or missing) and kwargs.get("preserve_base_dependencies"):
        recipe["compose"]["preserve_base_dependencies"] = True
    return recipe


def _kept_dependencies(order, declared, kept):
    """`declared` with each stage left out of the graph replaced by the kept stages it waited on,
    transitively, so dropping a stage never makes its follower concurrent with its producer."""
    found, seen, pending = [], set(), list(declared)
    while pending:
        dep = pending.pop(0)
        if dep in seen:
            continue
        seen.add(dep)
        if dep in kept:
            found.append(dep)
        else:
            pending.extend(order.get(dep, {}).get("depends_on") or [])
    return found


def compose_subgraph_recipe(registry, base_recipe, graph_spec, find_input=None, *, preserve_base_dependencies=False,
                            extra_stages=None, group_intensity=None, complete_group_consumers=False):
    """Cut the caller's stage subgraph out of the capability's own recipe.

    The nodes keep their unit, kind, gate, write scope, profile and permissions;
    only the edges change: the subgraph is re-linked in the caller's order, the
    last node becomes the terminal, a human gate a kept node raised is rebound
    to the entry of the node that now follows it (dropped when nothing
    follows), and a parallel group survives only when its anchor is kept and is
    not the new terminal (G6). Strong+ may append a side-effect-free owner close
    to keep eligible terminal review/map groups. An absent group_intensity
    replays the historical assembly. Validation stays with `_validate_recipe` --
    this function never re-implements a rule, it only assembles.

    SD-165: a graph key is a host stage id, an optional catalog part of the host
    recipe, or a registered shareable `capability:stage`. A borrowed part keeps
    its unit, kind and gate and is relocated under `parts/<capability>/<stage>/`
    inside the host's own artifact scope; the name mapping is sealed as
    `part_io`. A graph that names none of these assembles exactly as before.
    """
    if complete_group_consumers and group_intensity is not None and ORDER[group_intensity] >= ORDER["strong"]:
        return _complete_compose_group_consumers(registry, base_recipe, graph_spec, find_input,
            preserve_base_dependencies=preserve_base_dependencies,
            extra_stages=extra_stages, group_intensity=group_intensity)
    host = base_recipe["capability"]
    base_nodes = {node["id"]: node for node in base_recipe["standard_plus"]["nodes"]}
    optional = dict(TOPO.recipe_optional_parts(registry, base_recipe))
    # `view` is what a key may resolve to locally; `order` carries the
    # precedence edges used only by the caller-order check (sealed `depends_on`
    # is always rewritten in the caller's order below).
    view = dict(base_nodes)
    view.update({stage: row["optional"]["node"] for stage, row in optional.items()})
    order = {node_id: {"depends_on": list(node.get("depends_on") or [])} for node_id, node in view.items()}
    for stage, row in optional.items():
        order[stage]["depends_on"] = list(row["optional"]["after"])
        for follower in row["optional"]["before"]:
            order[follower]["depends_on"].append(stage)
    rows, unknown = [], []
    for key, unit in graph_spec:
        capability, separator, stage = key.partition(":")
        if separator and capability == host and stage in view:
            key, separator = stage, ""
        if not separator:
            if key not in view:
                unknown.append(key)
                continue
            rows.append((key, key, unit, None))
            continue
        part = TOPO.resolve_shared_part(registry, base_recipe, key)
        if part is None:
            unknown.append(key)
            continue
        rows.append((key, f"{capability}-{stage}", unit, part))
    # A plan's extra stages (`extra_stages`): each is shaped on a catalog stage of the same unit and
    # runs right after the stage it names; one that cannot be shaped or placed is left to the owner.
    placed = []
    for stage in extra_stages or ():
        part = TOPO.plan_stage_part(registry, base_recipe, stage)
        node_id = f"plan-{stage.get('id')}"
        after = [index for index, row in enumerate(rows) if row[1] == stage.get("after")]
        if part is None or not after or after[0] == len(rows) - 1 or node_id in {row[1] for row in rows}:
            continue
        rows.insert(after[0] + 1, (part["part"], node_id, None, part))
        placed.append({key: stage[key] for key in ("id", "unit", "after", "verify") if key in stage})
        view[node_id] = part["node"]
        order[node_id] = {"depends_on": [stage["after"]]}
        follower = rows[after[0] + 2][1]
        order[follower] = {"depends_on": list(order.get(follower, {}).get("depends_on") or []) + [node_id]}
    if unknown:
        raise ValueError(
            "compose-graph-unknown-node:" + ",".join(unknown)
            + " (available: " + ",".join(view) + ")"
            + " (run `capability-route.py stages --capability "
            + base_recipe["capability"] + "` to list valid stage ids)"
        )
    ids = [row[1] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("compose-graph-duplicate-node")
    source_of = {node_id: (part["node"] if part else view[node_id]) for _, node_id, _, part in rows}
    for _, node_id, _, part in rows:
        if part and not part.get("plan_stage"):
            borrowed_ids = {p["stage"]: other for _, other, _, p in rows
                            if p and p["recipe"] is part["recipe"]}
            order[node_id] = {"depends_on": [borrowed_ids[dep] for dep in part["node"].get("depends_on") or []
                                             if dep in borrowed_ids]}
    violation = _compose_graph_order_violation(order, ids)
    if violation:
        consumer, producer = violation
        raise ValueError(
            f"compose-graph-order:{consumer}-before-{producer} "
            "(recipe order: " + ",".join(view) + ")"
        )
    def gate_group(node_id):
        base = source_of[node_id]
        continuation = base.get("continuation") or {}
        if continuation.get("kind") == "human-gate":
            return continuation["gate"], tuple(base.get("depends_on", []))
        return None

    host_kept = {node_id for _, node_id, _, part in rows if part is None}
    # The catalog's input-name lookup fills a SUBGRAPH; a full recipe graph and a
    # preset never ask (stage-dispatch 13.64.3: "in a partial graph").
    strict = len(host_kept & set(base_nodes)) < len(base_nodes)
    selected_outputs = {out for node_id in ids for out in source_of[node_id].get("outputs") or []}

    def producers_in(capability, name):
        stages = [node["id"] for recipe in registry["recipes"] if recipe["capability"] == capability
                  for node in recipe["standard_plus"]["nodes"] if name in (node.get("outputs") or [])]
        stages += [part_id.partition(":")[2] for part_id, row in (TOPO.part_catalog(registry).get("parts") or {}).items()
                   if part_id.partition(":")[0] == capability and isinstance(row.get("optional"), dict)
                   and name in (row["optional"]["node"].get("outputs") or [])]
        return sorted(set(stages))

    scope = json.loads(json.dumps(base_recipe["artifact_scope"]))
    nodes = []
    overrides = {}
    used_parts = set()
    meta = {}
    for index, (key, node_id, unit, part) in enumerate(rows):
        node = json.loads(json.dumps(source_of[node_id]))
        part_id = key if part else f"{host}:{node_id}"
        catalog_row = TOPO.part_row(registry, part_id)
        capability = part["capability"] if part else host
        if unit:
            choices = node.get("unit_choices")
            widened = catalog_row.get("unit_choices")
            if choices is None and widened and unit != node.get("unit") and unit in widened:
                # A widened choice is merged only when the graph actually picks it.
                choices = node["unit_choices"] = list(widened)
                used_parts.add(part_id)
            if choices is not None and unit not in choices:
                raise ValueError(
                    f"compose-unit-not-in-choices:{node_id}:{unit} (choices: {','.join(choices)})"
                )
            if node.get("kind") in ("capability-owner", "resource-runner"):
                raise ValueError(f"compose-unit-override-reserved:{node_id}")
            node["unit"] = unit
            node["role"] = TOPO._unit_frontmatter(unit)["role"]
            overrides[key] = unit
        originals = list(node.get("outputs") or [])
        moved = {}
        if part:
            kind = node["kind"]
            def relocate(path, _kind=kind, _part=part):
                return TOPO.relocate_part_path(_part["scope"], _kind, _part["capability"], _part["stage"], path)
            node["id"] = node_id
            node["write_scope"] = [target for path in node["write_scope"] for target in relocate(path)]
            for out in originals:
                if not TOPO._is_semantic_output(out):
                    moved[out] = relocate(out)[0]
            node["outputs"] = [moved.get(out, out) for out in originals]
            node["part"] = key
            if part.get("plan_stage"):
                node["plan_stage"] = dict(part["plan_stage"])
            if catalog_row.get("start_approval"):
                node["start_approval"] = catalog_row["start_approval"]
            if part["merged_anchor"]:
                scope[part["merged_anchor"]] = part["scope"][part["merged_anchor"]]
            used_parts.add(key)
        elif node_id in optional:
            used_parts.add(part_id)
        previous = nodes[-1] if nodes else None
        if preserve_base_dependencies:
            if part:
                borrowed_ids = {p["stage"]: other for _, other, _, p in rows
                                if p and p["recipe"] is part["recipe"]}
                declared_dependencies = (part["node"].get("depends_on") or [])
                node["depends_on"] = [borrowed_ids[dep] for dep in declared_dependencies if dep in borrowed_ids]
            else:
                declared_dependencies = order.get(node_id, {}).get("depends_on") or []
                node["depends_on"] = _kept_dependencies(order, declared_dependencies, set(ids))
        else:
            node["depends_on"] = [previous["id"]] if previous else []
        if not preserve_base_dependencies and previous and gate_group(previous["id"]):
            group = gate_group(previous["id"])
            if gate_group(node_id) == group:
                # Independent raisers share their predecessor. Serializing
                # them would make the first gate block its own second raiser.
                node["depends_on"] = list(previous["depends_on"])
            else:
                raisers = []
                for prior in reversed(nodes):
                    if gate_group(prior["id"]) != group:
                        break
                    raisers.append(prior["id"])
                node["depends_on"] = list(reversed(raisers))
        input_names = catalog_row.get("input_names") or {}
        if part:
            origin_nodes = {n["id"]: n for n in part["recipe"]["standard_plus"]["nodes"]}
            kept = {p["stage"] for _, _, _, p in rows if p and p["recipe"] is part["recipe"]}
        else:
            origin_nodes, kept = base_nodes, set(ids)

        def alternates(name, owners, _capability=capability, _names=input_names):
            if owners is not None:  # every recipe producer was dropped: also look under the borrowed name
                return tuple(f"parts/{_capability}/{stage}/{name}" for stage in sorted(owners))
            target = _names.get(name)
            if not target or not strict or target in selected_outputs:
                return ()
            return (name, target, *(f"parts/{_capability}/{stage}/{target}"
                                    for stage in producers_in(_capability, target)))

        if part and part.get("plan_stage"):
            # A plan's stage reads what the stage before it wrote.
            node["inputs"], sources = [out for out in (previous or {}).get("outputs") or []
                                       if not TOPO._is_semantic_output(out)], {}
        else:
            node["inputs"], sources = _compose_inputs(origin_nodes, source_of[node_id], kept, find_input, alternates)
        if sources:
            node["input_sources"] = sources
            if set(sources) & set(input_names):
                used_parts.add(part_id)
        node.pop("terminal", None)
        node.pop("terminal_gate", None)
        node.pop("continuation", None)
        node.pop("parallel_group", None)
        nodes.append(node)
        meta[node_id] = {"part_id": part_id, "originals": originals, "moved": moved,
                         "input_names": {} if part and part.get("plan_stage") else input_names,
                         "catalog": bool(part) or node_id in optional}
    # SD-165 name mapping, sealed as `part_io` (absent on a route that uses no catalog part).
    produced = {}
    for index, node in enumerate(nodes):
        info = meta[node["id"]]
        io = {}
        inputs = []
        for name in node["inputs"]:
            target = produced.get(name, name)
            if target != name:  # the nearest producer is a borrowed part: read its relocated path
                io[name] = target
            if target not in inputs:
                inputs.append(target)
        for name, target in info["input_names"].items():
            if name in inputs and target in produced:  # catalog name mapping to a selected producer
                io[name] = produced[target]
                used_parts.add(info["part_id"])
        if index and meta[nodes[index - 1]["id"]]["catalog"]:
            # A catalog part's result reaches the next stage brief even when that
            # stage's recipe inputs never named it.
            handed = [out for out in nodes[index - 1]["outputs"] if not TOPO._is_semantic_output(out)]
            if handed and not set(handed) & (set(inputs) | set(io.values())):
                inputs.extend(handed)
        node["inputs"] = inputs
        io.update(info["moved"])
        if io:
            node["part_io"] = io
        for original, sealed in zip(info["originals"], node["outputs"]):
            if not TOPO._is_semantic_output(original):
                produced[original] = sealed
    declared_groups = [(group, False) for group in base_recipe["standard_plus"].get("parallel_groups") or []]
    for _, node_id, _, part in rows:
        if part:  # a borrowed anchor brings its own group, re-keyed to the borrowed node id
            declared_groups.extend(
                (dict(json.loads(json.dumps(group)), id=node_id, node=node_id), True)
                for group in part["recipe"]["standard_plus"].get("parallel_groups") or []
                if group["node"] == part["stage"])
    if preserve_base_dependencies:
        terminal_ids = {node["id"] for node in nodes
                        if not any(node["id"] in (other.get("depends_on") or []) for other in nodes)}
    else:
        terminal_ids = {nodes[-1]["id"]}
    auto_completed = []
    closing_boundaries = set()
    if group_intensity is not None and ORDER[group_intensity] >= ORDER["strong"]:
        eligible = {group["node"] for group, _ in declared_groups
                    if ORDER[group_intensity] >= ORDER[group["min_intensity"]]}
        anchors = [node for node in nodes if node["id"] in terminal_ids & eligible
                   and node["kind"] in ("review-worker", "map-worker")]
        if anchors:
            closing_boundaries = set(terminal_ids)
            owner_id, suffix = "owner-close", 1
            occupied = {node["id"] for node in nodes} | {out for node in nodes for out in node["outputs"]}
            while owner_id in occupied or owner_id + ".md" in occupied:
                suffix += 1
                owner_id = f"owner-close-{suffix}"
            terminals = [node for node in nodes if node["id"] in terminal_ids]
            owner = {
                "id": owner_id, "kind": "capability-owner", "unit": "_kernel/owner",
                "depends_on": [node["id"] for node in terminals], "role": "deep orchestrator",
                "inputs": list(dict.fromkeys(item for node in terminals
                                             for item in node["inputs"] + node["outputs"])),
                "outputs": [owner_id + ".md"], "write_scope": [owner_id + ".md"],
                "resource_class": "normal", "completion_gate": "compose-owner-close",
                "dispatch_depth": 1, "model_profile": registry["owner_profile_by_intensity"]["standard"],
                "fallback_hops": ["same-harness-headless", "cross-harness-headless", "inline"],
                "advance_class": "model-required", "model_required_reason": "terminal-report",
                "commit_expected": False,
            }
            nodes.append(owner)
            source_of[owner_id] = owner
            terminal_ids = {owner_id}
            auto_completed.append({"id": owner_id, "reason": "terminal-anchor",
                                   "anchors": [node["id"] for node in anchors],
                                   "intensity": group_intensity})
    for terminal in nodes:
        if terminal["id"] not in terminal_ids:
            continue
        terminal["terminal"] = True
        terminal["terminal_gate"] = terminal["completion_gate"]
        if terminal.get("advance_class") != "model-required":
            terminal["advance_class"] = "model-required"
            terminal["model_required_reason"] = "terminal-report"
    # Human gates: a base node whose continuation was `human-gate G` keeps G
    # only when a graph node follows it; G is rebound to that node's entry.
    # One binding per DISTINCT gate, not one per raiser. Sibling nodes can raise
    # the same gate -- the frame pair both continue on `frame-review` -- and a
    # second binding for the same gate is refused by
    # `capability_topology.py:734-737` ("every declared human gate must bind to
    # exactly one node"), which made a compose graph naming both frame legs
    # impossible at any capability. The anchor is the node following the LAST
    # raiser, so the gate opens only after every raiser has run.
    bindings, gates = [], []
    gate_anchor: dict[str, str] = {}
    for index, node in enumerate(nodes):
        if node["id"] in terminal_ids:
            continue
        base = source_of[node["id"]]
        continuation = base.get("continuation") or {}
        # A closing summary is not the omitted transaction: the old terminal
        # boundary still drops its approval rather than rebinding it to the close.
        if continuation.get("kind") == "human-gate" and node["id"] not in closing_boundaries:
            gate = continuation["gate"]
            if gate not in gate_anchor:
                gates.append(gate)
            gate_anchor[gate] = nodes[index + 1]["id"]
            node["continuation"] = {"kind": "human-gate", "gate": gate}
        elif node.get("kind") == "resource-runner":
            node["continuation"] = {"kind": "supervised"}
        else:
            node["continuation"] = {"kind": "inline-next"}
    bindings.extend(
        {"gate": gate, "node": gate_anchor[gate], "position": "entry"} for gate in gates
    )
    # A gate declared on the entry of a base SOURCE node (no predecessor raises
    # it, e.g. autopilot-spec `intent-confirmation` on `research`) is kept
    # verbatim when that node is kept: it is parent-owned, not a continuation.
    kept_ids = set(ids)
    for row in base_recipe.get("human_gate_bindings") or []:
        node_id = row.get("node")
        if (row.get("position") == "entry" and node_id in kept_ids and node_id in base_nodes
                and not (base_nodes[node_id].get("depends_on") or [])
                and row.get("gate") not in gates):
            bindings.append({"gate": row["gate"], "node": node_id, "position": "entry"})
            gates.append(row["gate"])
    groups, omitted_groups = [], []
    for group, borrowed in declared_groups:
        anchor = next((n for n in nodes if n["id"] == group["node"]), None)
        if anchor is None:
            continue
        if anchor.get("terminal") is True:
            omitted_groups.append({"id": group["id"], "reason": "terminal-anchor"})
            continue
        consumers = [n for n in nodes if anchor["id"] in n.get("depends_on", [])]
        if anchor["kind"] == "pipeline-stage" and not any(
                n["kind"] == "review-worker" for n in consumers):
            # The preset's fan-out requires a review consumer. A caller who
            # selected a smaller graph did not select that fan-out obligation.
            omitted_groups.append({"id": group["id"], "reason": "review-consumer-not-selected"})
            continue
        group = json.loads(json.dumps(group))
        dropped = _unarbitrated_auxiliary_legs(registry, group, anchor, consumers) if borrowed else []
        if dropped:
            # Peer legs stay; an auxiliary leg nobody arbitrates is left out.
            group["legs"] = [leg for leg in group["legs"] if leg["suffix"] not in dropped]
            group["width_by_intensity"] = {
                tier: min(width, len(group["legs"])) for tier, width in group["width_by_intensity"].items()}
            omitted_groups.append({"id": group["id"], "reason": "auxiliary-arbiter-not-selected",
                                   "legs": dropped})
        groups.append(group)
    extensions = [
        json.loads(json.dumps(row))
        for row in base_recipe.get("conditional_extensions") or []
        if set(row.get("after") or []) <= kept_ids
        and {ref.get("node") for ref in row.get("source_outputs") or []} <= kept_ids
    ]
    # A kept group anchor or an appended owner close can follow the node an extension
    # names, so the extension runs after what now ends the route.
    terminals = sorted({node["id"] for node in nodes}
                       - {dep for node in nodes for dep in node.get("depends_on") or []})
    for row in extensions:
        if not set(row.get("after") or []) <= set(terminals):
            row["after"] = terminals
    recipe = {
        "capability": base_recipe["capability"],
        "modes": list(base_recipe["modes"]),
        "topology_class": base_recipe["topology_class"],
        "direct_predicates": list(base_recipe["direct_predicates"]),
        "promotion_signals": list(base_recipe["promotion_signals"]),
        "artifact_scope": scope,
        "quick": json.loads(json.dumps(base_recipe["quick"])),
        "standard_plus": {
            "topology": base_recipe["standard_plus"].get("topology", base_recipe["topology_class"]),
            "owner_dispatch_depth": base_recipe["standard_plus"]["owner_dispatch_depth"],
            "max_dispatch_depth": max(
                (node.get("dispatch_depth", 0) for node in nodes if node.get("kind") != "resource-runner"),
                default=0,
            ),
            "nodes": nodes,
        },
        "conditional_extensions": extensions,
        "completion_gates": sorted({node["completion_gate"] for node in nodes}),
        "human_gates": sorted(set(gates)),
        "human_gate_bindings": bindings,
        "resume_retry_boundaries": list(ids) + [row["id"] for row in auto_completed],
        "compose": {"origin": "compose", "shape": "staged",
                    "graph": [row[0] for row in rows if not (row[3] and row[3].get("plan_stage"))],
                    "unit_overrides": overrides, "base_capability": base_recipe["capability"]},
    }
    if placed:
        recipe["compose"]["extra_stages"] = placed
    if used_parts:
        # Catalog rows this recipe derives from; `capability_registry_digest` reads it.
        recipe["compose"]["parts"] = sorted(used_parts)
    if groups:
        recipe["standard_plus"]["parallel_groups"] = groups
    if omitted_groups:
        recipe["compose"]["omitted_parallel_presets"] = omitted_groups
    if auto_completed:
        recipe["compose"]["auto_completed_nodes"] = auto_completed
        if preserve_base_dependencies:
            recipe["compose"]["preserve_base_dependencies"] = True
    return recipe


def _versioned_subgraph(registry, recipe):
    """Only an exact registry-derived subgraph may inherit legacy demands."""
    meta = recipe.get("compose") or {}
    if not isinstance(meta, dict) or not meta.get("base_capability"):
        return False
    try:
        base = TOPO.resolve_recipe(registry, meta["base_capability"], recipe["modes"][0])
        overrides = meta.get("unit_overrides", {})
        graph = [(node_id, overrides.get(node_id)) for node_id in meta["graph"]]
        sealed = {}
        for node in recipe["standard_plus"]["nodes"]:
            if isinstance(node.get("input_sources"), dict):
                for name, source in node["input_sources"].items():
                    sealed.setdefault(name, source)
        auto = meta.get("auto_completed_nodes") or []
        expected = compose_subgraph_recipe(registry, base, graph, sealed.get if sealed else None,
                                           extra_stages=meta.get("extra_stages"),
                                           group_intensity=meta.get("group_completion_intensity") or (auto[0]["intensity"] if auto else None),
                                           complete_group_consumers=bool(meta.get("group_completion_intensity")) or any(
                                               row["reason"] != "terminal-anchor" for row in auto),
                                           preserve_base_dependencies=meta.get("preserve_base_dependencies", False))
        if expected == recipe:
            return True
        # Pre-notice routes already had this identical graph, but did not
        # record terminal omissions. Preserve their original bytes/profiles.
        omissions = expected["compose"].get("omitted_parallel_presets", [])
        remaining = [row for row in omissions if row["reason"] != "terminal-anchor"]
        if remaining:
            expected["compose"]["omitted_parallel_presets"] = remaining
        else:
            expected["compose"].pop("omitted_parallel_presets", None)
        return expected == recipe
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return False


def _validate_compose_owner_close(registry, recipe, intensity=None):
    """Automatic closing/consumer nodes must be the exact registry-derived assembly."""
    carries = any(node.get("completion_gate") == "compose-owner-close"
                  for node in recipe["standard_plus"]["nodes"])
    meta = recipe.get("compose") or {}
    auto = meta.get("auto_completed_nodes")
    selected = {key.replace(":", "-") for key in meta.get("graph", [])}
    # Consumer completion is strong+ only; lower intensities retain the
    # existing profile-demand validation of an inexact subgraph marker.
    extra = intensity not in ("direct", "quick", "standard") and bool(meta.get("graph")) and any(
        node["id"] not in selected and not node.get("plan_stage")
        for node in recipe["standard_plus"]["nodes"])
    completion_intensity = meta.get("group_completion_intensity")
    nodes = {node["id"]: node for node in recipe["standard_plus"]["nodes"]}
    host_omission = any(row["reason"] == "auxiliary-arbiter-not-selected"
                        and row["id"] in nodes and not nodes[row["id"]].get("part")
                        for row in meta.get("omitted_parallel_presets", []))
    if carries or auto or extra or completion_intensity or host_omission:
        if ((carries or auto or extra) and not auto
                or carries != any(row["reason"] == "terminal-anchor" for row in auto or [])
                or not _versioned_subgraph(registry, recipe)
                or intensity is not None and (any(row["intensity"] != intensity for row in auto or [])
                                             or completion_intensity and completion_intensity != intensity)):
            raise ValueError("compose-owner-close-differs-from-assembly")


def _compose_default_jobs():
    inherited = os.environ.get("AGENT_DISPATCH_JOBS")
    if inherited:
        return Path(inherited)
    return stable_state_root(os.environ) / "jobs.log"


def _compose_readiness(cwd, jobs, parent_harness, children, *, gpu_route=None,
                       owner_harnesses=None):
    spec = importlib.util.spec_from_file_location(
        "hearting_dispatch_readiness", ROOT / "utilities" / "dispatch-readiness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Compose drafts have no historical seal; pin probes pass the existing
    # route, whose owner selection survives completed compute-node omission.
    selection = GPU_SANDBOX.select(gpu_route) if gpu_route else None
    try:
        return module.generate(
            worktree=Path(cwd), jobs=Path(jobs),
            owner_harnesses=list(owner_harnesses) if owner_harnesses is not None else [parent_harness],
            child_harnesses=list(children),
            **({"codex_execution_selection": selection}
               if selection and selection["gpu_scope"] else {}),
        )
    except module.ReadinessError as exc:
        raise ValueError(f"compose-readiness-unavailable:{exc}") from exc


SPEC_READ_SHARED_PREFIX = "compose-auto: shared spec present, read "
# compose and compile record the same tracked-gate defaults when the caller states none.
DEFAULT_DRIFT_VERDICT = "no-spec-impact: compose default (caller asserted no spec-significant change)"
DEFAULT_ARTIFACT_GUARD = "compose-prechecked"


def compose_spec_read(cwd, artifact_root, explicit):
    """Require this session's unchanged read only for the artifact root's governing
    legacy `spec/prd.md`. Resolve that decision with the same `prd_candidates`
    function as the read hook; shared revisions are informational, and a checkout
    PRD outside the artifact root cannot satisfy or block the hook's marker gate."""
    if explicit not in (None, "", "auto"):
        return {"satisfied": explicit.lower() not in ("0", "false", "no"), "source": explicit}
    import artifact_cutover as ARTIFACT_CUTOVER

    root = Path(artifact_root).resolve()
    candidates = ARTIFACT_CUTOVER.prd_candidates(root)
    present = [str(root / "spec" / "prd.md")] if str(root / "spec" / "prd.md") in candidates else []
    shared = _compose_shared_spec_prds(root)
    unread = sorted(set(present) - set(_spec_reads_recorded(present, artifact_root) if present else ()))
    if unread:
        raise ValueError("compose-spec-read-required:" + ",".join(unread))
    if present:
        return {"satisfied": True, "source": SPEC_READ_MARKER_PREFIX + ",".join(sorted(set(present)))}
    if shared:
        return {"satisfied": True, "source": SPEC_READ_SHARED_PREFIX + ",".join(sorted(set(shared)))}
    if (Path(cwd) / "spec" / "prd.md").is_file():
        return {"satisfied": True, "source": "compose-auto: no governing artifact-root PRD; checkout spec/prd.md is informational"}
    return {"satisfied": True, "source": "compose-auto: no spec/prd.md under cwd or artifact root"}


SPEC_READ_MARKER_PREFIX = "spec-read-marker: "


def _spec_reads_recorded(paths, artifact_root):
    """The `paths` this session has read unchanged, as the read hook recorded them
    (`hooks/spec-read-marker.sh`: `<state_root>/spec-grounding/<session>__<root key>`
    holding the file's mtime at the read). Only a root-level `spec/prd.md` of the
    artifact root has such a key; anything unreadable counts as not read."""
    try:
        session = ROUTE_AUTHORITY.default_parent_session_id()
    except ValueError:                      # an ambiguous caller has no reads of its own
        return []
    if not session:
        return []
    root = Path(artifact_root)
    key = str(root.parent).replace("/", "_").replace(" ", "_")
    marker = HARNESS_STATE_ROOTS.spec_grounding_dir(None) / f"{session}__{key}"
    try:
        recorded = int(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return []
    read = []
    for path in paths:
        try:
            if Path(path) == root / "spec" / "prd.md" and int(Path(path).stat().st_mtime) == recorded:
                read.append(path)
        except OSError:
            continue
    return read


def _compose_spec_read_notice(route, t=None):
    """One `[경로]` card line when compose found a shared spec it did not ask the caller to read."""
    t = t or UI_TEXT.translator()
    source = ((route.get("tracked_gate_evidence") or {}).get("spec_read") or {}).get("source")
    if isinstance(source, str) and source.startswith(SPEC_READ_SHARED_PREFIX):
        return "  " + t("route.spec.shared", path=source[len(SPEC_READ_SHARED_PREFIX):])
    return None


def _compose_shared_spec_prds(root):
    """The shared governing PRDs selected by artifact_cutover.prd_candidates.

    Older revisions and other shared refs are history, not independent compose
    requirements; the same candidate list controls both the hook and this notice.
    """
    import artifact_cutover as ARTIFACT_CUTOVER

    root = Path(root).resolve()
    shared_root = root / "shared" / "spec"
    found = []
    for candidate in ARTIFACT_CUTOVER.prd_candidates(root):
        try:
            Path(candidate).relative_to(shared_root)
        except ValueError:
            continue
        found.append(str(candidate))
    return found


def _parse_selection_pins(values, owner=None):
    """Parse repeated `--pin <target>=<harness>[:<model>[@<effort>]]` values.

    Returns `{target: {"harness", "model", "effort"}}` for the targets that were
    given. `--owner H` is the same statement as `--pin owner=H`; naming two
    different harnesses for the owner is a contradiction in the input.  Only a
    value that cannot be read at all is refused -- a route without pins never
    reaches this function with anything to refuse.
    """
    pins = {}
    for raw in values or ():
        target, sep, rest = str(raw).partition("=")
        if not sep or target not in SELECTION_PIN_TARGETS:
            raise ValueError(
                f"compose-pin-invalid:{raw!r} (use <target>=<harness>[:<model>[@<effort>]], "
                f"target one of {','.join(SELECTION_PIN_TARGETS)})")
        if target in pins:
            raise ValueError(f"compose-pin-invalid:{target} given twice")
        harness, colon, remainder = rest.partition(":")
        if harness not in DEFAULTS.DISPATCHABLE_HARNESSES:
            raise ValueError(
                f"compose-pin-invalid:{raw!r} unknown harness {harness!r} "
                f"(one of {','.join(sorted(DEFAULTS.DISPATCHABLE_HARNESSES))})")
        model = effort = None
        if colon:
            if not remainder:
                raise ValueError(f"compose-pin-invalid:{raw!r} empty model")
            model, at, tail = remainder.rpartition("@")
            if not at:
                model, effort = remainder, None
            elif not model or not tail:
                raise ValueError(f"compose-pin-invalid:{raw!r} empty model or effort around '@'")
            else:
                effort = tail
            if not SELECTION_PIN_MODEL.fullmatch(model):
                raise ValueError(
                    f"compose-pin-invalid:{raw!r} model must start with a letter or digit and contain only letters, digits and ._/:-")
            if effort is not None and not SELECTION_PIN_EFFORT.fullmatch(effort):
                raise ValueError(f"compose-pin-invalid:{raw!r} effort must be lowercase letters")
        pins[target] = {"harness": harness, "model": model, "effort": effort}
    if owner:
        if "owner" in pins and pins["owner"]["harness"] != owner:
            raise ValueError(
                f"compose-pin-owner-conflict:--owner {owner} vs --pin owner={pins['owner']['harness']}")
        pins.setdefault("owner", {"harness": owner, "model": None, "effort": None})
    return pins


def _turn_peer_source(harness, session, cwd):
    """Return only provenance bound to this exact prompt marker and transfer."""
    try:
        import session_tidy
        seat = session_tidy.resolve_seat(harness, cwd or None, os.environ, session, "")
        turn = session_tidy.read_json(session_tidy._prompt_seq_path(seat))
        if (not isinstance(turn, dict) or turn.get("sid") != session
                or turn.get("harness") != harness or type(turn.get("seq")) is not int):
            return "unattributed"
        origin = turn.get("request_origin")
        if (not isinstance(origin, dict) or origin.get("harness") != harness
                or origin.get("session_id") != session
                or origin.get("request_digest") is None
                or not re.fullmatch(r"[0-9a-f]{64}", str(origin.get("request_digest")))
                or not isinstance(origin.get("transfer_ref"), str)
                or not isinstance(origin.get("name"), str)):
            return "unattributed"
        if origin.get("kind") == "peer" and origin.get("transfer_ref") == "native-envelope":
            if (origin.get("sender_harness") != "claude"
                    or not isinstance(origin.get("sender_session_id"), str)
                    or not origin["sender_session_id"]):
                return "unattributed"
            return f"peer:{origin['name']}·native-envelope"
        if origin.get("kind") not in {"peer", "runtime-completion"}:
            return "unattributed"
        ref = origin["transfer_ref"]
        spec = importlib.util.spec_from_file_location("pin_change_peer_message", ROOT / "utilities/peer-message.py")
        peer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(peer)
        transfer = peer._read_transfer_record(ref)
        if (not isinstance(transfer, dict)
                or transfer.get("body_sha256") != origin.get("transfer_body_sha256")
                or not peer._reference_target_matches(transfer, {"harness": harness,
                                                                  "session_id": session}, ref)):
            return "unattributed"
        sender = transfer.get("from") or {}
        producer = transfer.get("producer_binding")
        if origin.get("kind") == "runtime-completion":
            if origin.get("legacy") is True:
                pending = peer._read_pending(ref)
                legacy = peer._legacy_registered_producer_binding(
                    pending, {"harness": harness, "session_id": session})
                if (legacy is None or legacy != origin.get("producer_binding")
                        or producer is not None):
                    return "unattributed"
                producer = legacy
            elif (sender.get("harness") != "runtime"
                  or producer != origin.get("producer_binding")
                  or not peer._valid_runtime_producer_binding(
                      producer, sender, {"harness": harness, "session_id": session},
                      transfer.get("refs"))):
                return "unattributed"
            return f"runtime:completion·{producer['attempt_id']}/{producer['delivery_id']}"
        if (producer is not None or sender.get("harness") != origin.get("sender_harness")
                or sender.get("session_id") != origin.get("sender_session_id")
                or not peer._valid_transfer_sender(sender, producer, transfer.get("to"),
                                                   transfer.get("refs"))):
            return "unattributed"
        name = sender.get("name") or sender.get("session_id") or sender.get("harness") or "?"
        return f"peer:{name}·{ref}"
    except Exception:  # noqa: BLE001 -- an unknown source is recorded as unknown
        return "unattributed"


def _pin_change_probe(route, jobs, target, harness, caller):
    """`(tuples, candidates)` a pin moved to `harness` launches with, probed by the runtime now.

    owner: from the new owner harness to the route's children. worker: from the owner
    harness in force to the new worker harness. frame: from this depth-0 session to the new
    frame harness. A quick route (one owner, its registered-headless candidates) is probed for
    candidates instead; it starts no stage, so a worker change there needs nothing probed.
    Nothing supported for the new pin is refused before anything is recorded."""
    if harness not in DEFAULTS.current_enabled_harnesses():
        raise ValueError(f"explicit-adapter-disabled-by-user-policy:{target}={harness}")
    import route_authority as RA
    view = RA.route_in_force(route)
    quick = route.get("effective_intensity") == "quick" or route.get("registered_headless_candidates") is not None
    rows = [row for row in (view.get("dispatch_evidence") or {}).get("tuples") or [] if isinstance(row, dict)]
    if quick and target == "worker":
        return [], []
    if target == "owner" or quick:
        children = sorted({row.get("child_harness") for row in rows if row.get("child_harness")}) \
            or list(_compose_default_children())
        probe = _compose_readiness(route["cwd"], jobs, harness, children, gpu_route=route)
        tuples = [] if quick else [row for row in probe.get("tuples") or [] if row.get("parent_harness") == harness]
        candidates = [row for row in probe.get("candidates") or [] if row.get("harness") == harness] if quick else []
        ready = (any(row.get("status") == "supported" for row in candidates) if quick else
                 any(row.get("status") == "supported" and row.get("launch_authority") == "conductor" for row in tuples))
    else:
        if target == "worker":
            owner = ((view.get("selection_pins") or {}).get("owner") or {}).get("harness")
            parents = [owner] if owner else sorted({row.get("parent_harness") for row in rows
                                                    if row.get("launch_authority") == "conductor"
                                                    and row.get("parent_harness")})
        else:
            parents = [caller] if caller else sorted({row.get("parent_harness") for row in rows
                                                      if row.get("parent_harness")})
        tuples, candidates = [], []
        for parent in parents:
            probe = _compose_readiness(route["cwd"], jobs, parent, [harness], gpu_route=route)
            tuples += [row for row in probe.get("tuples") or []
                       if row.get("parent_harness") == parent and row.get("child_harness") == harness]
        ready = any(row.get("status") == "supported"
                    and (target == "frame" or row.get("launch_authority") == "conductor") for row in tuples)
    if not ready:
        raise ValueError(f"start-pin-{target}-unready:{harness} (the runtime probed no supported launch "
                         f"for that {target} harness in this worktree; the {target} pin is unchanged)")
    return tuples, candidates


def _change_pins(route, jobs, values):
    """`start --pin owner|frame|worker=<harness>[:<model>[@<effort>]]`: the route's parent moves
    pins of a sealed route.

    Each pin's checked launch tuples are probed now (`_pin_change_probe`), all of them before
    anything is recorded, and each change is one row beside the route
    (`route_authority.record_pin_change`); every launch decision reads the route through
    `route_authority.route_in_force`, so the next launch of that target uses the new pin and an
    attempt that already launched keeps what it launched with."""
    import route_authority as RA
    from work_start import _rows
    pins, warnings = _filter_top_pins(_parse_selection_pins(values))
    harness, session = RA.caller_identity()
    owner = RA.current_route_owner(route, jobs, _rows(jobs))
    if owner:
        RA.require_owner(owner, session, jobs, "start-pin-parent-only",
                         "start-pin-parent-only: only this route's parent session moves its pins")
    targets = [target for target in RA.PIN_CHANGE_TARGETS if target in pins]
    probed = {target: _pin_change_probe(route, jobs, target, pins[target]["harness"], harness)
              for target in targets}
    source = _turn_peer_source(harness, session, route.get("cwd"))
    result = {"changed": False, "warnings": warnings, "changes": {}}
    for target in targets:
        tuples, candidates = probed[target]
        row = RA.record_pin_change(route, target=target, pin=pins[target],
                                   by={"harness": harness, "session_id": session},
                                   source=source, tuples=tuples, candidates=candidates)
        current = (RA.route_in_force(route).get("selection_pins") or {}).get(target)
        result["changes"][target] = {"changed": row is not None, "pin": current,
                                     "previous": (row or {}).get("previous")}
        result.update({target: current, "previous": (row or {}).get("previous"),
                       "source": (row or {}).get("source"),
                       "changed": result["changed"] or row is not None})
    return result


def _record_access_change(route, jobs):
    """The route's parent hands its next owner the execution access request in its environment
    (`AGENT_DISPATCH_EXECUTION_ACCESS_FILE`): one `access` row beside the route
    (`route_authority.record_access_change`), which the next owner and a replacement owner
    launch with. Only the route's parent records it; a request that does not validate is not
    recorded, and the launch meets it as it does today."""
    import route_authority as RA
    from execution_access import (AccessContext, ExecutionAccessError, load_request,
                                  normalized_request, request_path)
    from work_start import _rows
    given = request_path(None)
    if given is None:
        return None
    harness, session = RA.caller_identity()
    owner = RA.current_route_owner(route, jobs, _rows(jobs))
    if owner:
        try:
            RA.require_owner(owner, session, jobs, "access-change-parent-only")
        except DispatchContractError as exc:  # not recorded; an unreadable lineage waits for the same start
            return {"changed": False, "reason": exc.reason,
                    **{key: getattr(exc, key) for key in ("next_action", "retry_when") if getattr(exc, key, None)}}
    try:
        context = AccessContext.build(worktree=str(route.get("cwd") or ""),
                                      artifact_root=str(route.get("artifact_root") or ""),
                                      dispatch_state_root=Path(jobs).expanduser().resolve(strict=False).parent,
                                      agent_home=ROOT)
        request = load_request(given, context=context)
        row = RA.record_access_change(route, request=normalized_request(request),
                                      request_sha256=request.request_sha256,
                                      by={"harness": harness, "session_id": session},
                                      source=_turn_peer_source(harness, session, route.get("cwd")))
    except (ExecutionAccessError, OSError, ValueError) as exc:
        return {"changed": False, "reason": getattr(exc, "reason", None) or str(exc)[:200]}
    current = RA.access_in_force(route) or {}
    return {"changed": row is not None, "request_sha256": current.get("request_sha256"),
            "source": current.get("source")}


def _main_session_only_models(harness):
    """The harness's `CFG_MAIN_SESSION_ONLY_MODELS`, or "" when it declares none
    or its model config cannot be read (compose then leaves the pin alone; the
    wrapper still refuses a restricted model at launch)."""
    try:
        import model_config
        values, _receipt = model_config.resolve_config(harness)
    except Exception:
        return ""
    return values.get("CFG_MAIN_SESSION_ONLY_MODELS", "")


def _filter_top_pins(pins):
    """`top-model-pin`: a main-session-only (top) model runs only in the main
    session and in frame.  For the owner and worker targets compose drops just
    the model part of the pin (the harness pin stays) and says so; frame keeps
    its model.  Returns `(pins, warnings)`."""
    kept, warnings = {}, []
    for target, pin in pins.items():
        pin = dict(pin)
        if target != "frame" and pin["model"]:
            import model_config
            restricted = _main_session_only_models(pin["harness"])
            if restricted and model_config.restricted_model(pin["model"], restricted):
                warnings.append(
                    f"warning=pin-dropped:{target}:{pin['model']}:main-session-only "
                    "(top models run only in the main session and frame)")
                pin["model"] = pin["effort"] = None
        kept[target] = pin
    return kept, warnings


def _compose_default_children(pins=None):
    """Probe current enabled children; pins cannot widen policy or unreadable defaults.

    A missing user file still resolves to the existing shipped fallback through
    default_config_path. A selected but unreadable/malformed file is an error.
    """
    config = DEFAULTS.load_and_validate(DEFAULTS.default_config_path(), DEFAULTS.default_topology_path())
    enabled = DEFAULTS.current_enabled_harnesses(config)
    children = list(DEFAULTS.query_owners(config))
    for pin in (pins or {}).values():
        if pin["harness"] not in enabled:
            raise ValueError("explicit-adapter-disabled-by-user-policy:" + pin["harness"])
        if pin["harness"] not in children:
            children.append(pin["harness"])  # legacy schemas declare ordering, not a prohibition
    return tuple(children)


def _compose_default_owners(pins=None):
    """Return enabled prospective owners; exact pinned-owner probes stay separate.

    Reuse the existing enabled-pool loader so owner discovery has the same fallback
    and policy source as child discovery. A disabled owner pin is not an automatic
    candidate; its dedicated pin probe and refusal path remain authoritative.
    """
    # Enabled pins are already present in this policy pool. Do not pass the
    # whole pin map to the child helper: worker/frame pins are not owner inputs,
    # and disabled owner pins are handled by the exact-target probe below.
    return _compose_default_children()


def _apply_selection_pins(route, pins):
    """Seal `pins` into `route` and stamp the worker pin on every depth-2 node.

    A worker pin H becomes each depth-2 node's `harness_affinity` and moves H to
    the front of that node's sealed `primary` band (removing it from the other
    bands), so the ordinary affinity consumers pick H first.  The pin leads the
    selection order (CONVENTIONS §2.1): it is ahead of the usage gate, and only
    hard eligibility (policy, checked tuple) and an active usage limit keep H from
    running.  A route with no pin gets no key.
    """
    if not pins:
        return
    enabled = DEFAULTS.current_enabled_harnesses()
    for pin in pins.values():
        if pin["harness"] not in enabled:
            raise ValueError("explicit-adapter-disabled-by-user-policy:" + pin["harness"])
    route["selection_pins"] = {
        "contract_version": SELECTION_PIN_CONTRACT_VERSION,
        **{target: dict(pins[target]) for target in SELECTION_PIN_TARGETS if target in pins},
    }
    worker = pins.get("worker")
    if worker is None:
        return
    harness = worker["harness"]
    for node in route.get("nodes", []):
        if node.get("dispatch_depth") != 2:
            continue
        node["harness_affinity"] = harness
        policy = node.get("harness_policy")
        if isinstance(policy, dict):
            for band in ("relief", "last_resort"):
                policy[band] = [h for h in policy.get(band, []) if h != harness]
            policy["primary"] = [harness] + [h for h in policy.get("primary", []) if h != harness]


def _inherited_selection_pins(binding, artifact_root):
    """The structurally valid pins of the frame a `--route-plan` binding names, else `{}`."""
    import route_plan as RP
    pins = RP.frame_selection_pins(binding["record"]["decision"], artifact_root)
    try:
        _verify_selection_pins({"selection_pins": {"contract_version": SELECTION_PIN_CONTRACT_VERSION, **pins}})
    except ValueError:
        return {}
    return pins


# Shapes a session picks for work that is already decided (the person named the capability and
# its stages, a stopped route continues, or an approved proposal's next leg): they compile without
# the recipe's frame pair. Routes sealed before this rule keep the frame nodes they were sealed with.
DECIDED_SHAPES = ("solo", "staged")


def _without_implicit_frame_briefs(registry, finder):
    """Keep campaign lookup for ordinary results, but do not infer frame lineage from it.

    The blocked names come from the frame contract, including capability-specific consumer names
    and the borrowed-part relocation form used by `_compose_inputs`. An explicit parent or route
    proposal bypasses this wrapper at the compose call site and keeps the existing mapping.
    """
    catalog = TOPO.part_catalog(registry)
    frame = catalog.get("frame") or {}
    names = set(frame.get("brief_outputs") or [])
    names.update(name for brief in (frame.get("briefs") or {}).values()
                 for name in brief.get("inputs", []))
    blocked = set(names)
    aliases = frame.get("aliases") or []
    for part_id in (catalog.get("parts") or {}):
        part_capability = part_id.partition(":")[0]
        blocked.update(f"parts/{part_capability}/{producer}/{name}"
                       for producer in aliases for name in names)

    def find(name):
        return None if name in blocked else finder(name)
    return find


def _frame_brief_finder(registry, capability, finder):
    """Let a leg of an approved plan find the frame cycle's two briefs under the names its recipe reads.

    A recipe that keeps its frame briefs under its own names (design, spec) maps them, through the
    part catalogue's brief-name table, to the canonical `shards/<frame>/direction-brief.md` outputs
    the framed route's cycle holds. Names that already match (code, draft, refine) need no mapping.
    """
    names = TOPO.frame_brief_inputs(registry, capability)
    outputs = (TOPO.part_catalog(registry).get("frame") or {}).get("brief_outputs") or []
    table = dict(zip(names, outputs)) if len(names) == len(outputs) else {}

    def find(name):
        return finder(name) or (finder(table[name]) if name in table else None)
    return find


def _frame_pin_rows(frame_route):
    """Compatibility name; all descendants read the same pins in force."""
    return ROUTE_AUTHORITY.selection_pin_rows(frame_route)


def proposal_readiness(frame_route, jobs):
    """One read-only readiness probe shared by every proposal leg's memory compile."""
    from dispatch_parent_completion import default_parent_harness
    return _compose_readiness(frame_route["cwd"], jobs, default_parent_harness("claude"),
                              _compose_default_children(_frame_pin_rows(frame_route) or None),
                              owner_harnesses=_compose_default_owners(_frame_pin_rows(frame_route) or None))


def _leg_compose_kwargs(leg_args, *, frame_route, frame_cycle_id, slug):
    import route_plan as RP
    spec = ((frame_route.get("tracked_gate_evidence") or {}).get("spec_read") or {}).get("source") or "auto"
    return dict(
        capability=leg_args["capability"], capability_mode=leg_args["capability_mode"], shape=leg_args["shape"],
        graph=leg_args["graph"], intensity=leg_args["intensity"], slug=slug,
        cwd=RP.leg_cwd(leg_args, frame_route["cwd"]),
        artifact_root=frame_route["artifact_root"],
        spec_read="auto" if str(spec).startswith("compose-auto:") else spec,
        campaign_key=frame_route.get("campaign_key"), parent_cycle_id=frame_cycle_id,
        selection_pins=_frame_pin_rows(frame_route) or None)


def _leg_evidence(leg, readiness):
    if leg["shape"] == "direct":
        return {}
    probe = readiness()
    return {"dispatch_evidence": {"tuples": probe["tuples"], "native_subagent": []},
            "registered_headless_evidence": {"candidates": probe["candidates"]}}


def _route_plan_preparation_key(binding):
    decision = ((binding.get("record") or {}).get("decision") or {}) if isinstance(binding, dict) else {}
    frame_route = decision.get("frame_route") or {}
    return {"kind": "approved-route-plan", "digest": binding.get("digest"),
            "frame_route_id": frame_route.get("route_id")}


def _compose_preparation_key(compose_args, route_plan_binding, prompt_file=None):
    if route_plan_binding is not None:
        return _route_plan_preparation_key(route_plan_binding)
    request = compose_args.get("work_request") or {}
    text = request.get("text") if isinstance(request, dict) else ""
    graph = compose_args.get("graph")
    graph = ([part.strip() for part in graph.split(",") if part.strip()]
             if isinstance(graph, str) else list(graph or []))
    caller = _route_chain_identity("compose", None)
    # Probe receipts can acquire new timestamps between a failed preparation
    # and its ordinary retry. They describe current launch eligibility, not the
    # identity of the user's prepared worktree allocation.
    return {"kind": "compose-start", "caller": caller,
            "request_path": str(Path(prompt_file).resolve()) if prompt_file else None,
            "request_sha256": hashlib.sha256(str(text or "").encode("utf-8")).hexdigest(),
            "capability": compose_args.get("capability"),
            "mode": compose_args.get("capability_mode"), "shape": compose_args.get("shape"),
            "graph": graph, "slug": compose_args.get("slug"),
            "campaign_key": compose_args.get("campaign_key"),
            "parent_cycle_id": compose_args.get("parent_cycle_id"),
            "intensity": compose_args.get("intensity"), "signals": sorted(compose_args.get("signals") or []),
            "parent_harness": compose_args.get("parent_harness"),
            "children": list(compose_args.get("children") or []),
            "selection_pins": compose_args.get("selection_pins"),
            "execution_scope": compose_args.get("execution_scope"), "profile": compose_args.get("profile"),
            "profile_demands": compose_args.get("profile_demands"),
            "explicit_profiles": compose_args.get("explicit_profiles"),
            "tracking": compose_args.get("tracking"), "artifact_guard": compose_args.get("artifact_guard"),
            "transport_evidence": compose_args.get("transport_evidence"),
            "cwd": str(Path(compose_args["cwd"]).resolve())}


def _dispatch_evidence_matches_cwd(evidence, cwd):
    rows = evidence.get("tuples") if isinstance(evidence, dict) else None
    if not isinstance(rows, list) or not rows:
        return False
    expected = os.path.realpath(cwd)
    return all(isinstance(row, dict) and isinstance(row.get("checked_worktree"), str)
               and os.path.realpath(row["checked_worktree"]) == expected for row in rows)


def _seal_framed_worktree(route, worktree):
    """Seal how a framed start prepared its read cwd into the route identity."""
    route["framed_worktree"] = dict(worktree)
    route["route_hash"] = route_hash(route)
    route["route_id"] = ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
    return route


def _inherits_frame_worktree(frame_route, leg, index, cwd):
    """Whether the first leg runs in the allocation its frame's own start made, without preparing again.

    Only that allocation qualifies, and only while it is still the same checkout on the same branch.
    An explicitly approved or retained linked checkout keeps the proposed-`cwd` approval path.
    """
    prepared = frame_route.get("framed_worktree") or {}
    frame_cwd = frame_route.get("cwd") or ""
    path, branch = prepared.get("path"), prepared.get("branch")
    if not (index == 0 and not leg.get("cwd")
            and prepared.get("state") in {"created", "reused"}
            and prepared.get("reason") != "approved-linked-task"
            and path and branch
            and os.path.realpath(cwd) == os.path.realpath(frame_cwd)
            and os.path.realpath(prepared.get("cwd") or "") == os.path.realpath(frame_cwd)
            and os.path.isdir(path)):
        return False
    top = _git(path, "rev-parse", "--show-toplevel")
    return (bool(top) and os.path.realpath(top) == os.path.realpath(path)
            and _git(path, "symbolic-ref", "-q", "HEAD") == f"refs/heads/{branch}")
