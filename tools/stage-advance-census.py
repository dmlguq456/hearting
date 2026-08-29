#!/usr/bin/env python3
"""SD-110 stage-advance census (read-only).

Two axes are counted separately and never share a denominator:

* ``recipe_axis``  -- the static ``standard_plus`` node graphs declared in
  ``capabilities/topologies.json`` (what *could* ever be sealed).
* ``route_axis``   -- the compiled live routes under a runtime routes
  directory (what *is* sealed today).

The run is fail-closed: with no topology recipes, no route corpus, or an
``advance_class`` value the topology does not declare, no report is printed
and the exit code is non-zero. Documentation may only quote numbers this
script printed.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

EX_DATAERR = 65
EX_NOINPUT = 66
UNSEALED = "unsealed"


class CensusError(Exception):
    """Fail-closed: the corpus cannot support a report."""


def load_topology(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CensusError(f"topologies unreadable: {exc}") from exc
    except ValueError as exc:
        raise CensusError(f"topologies malformed: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("recipes"), list):
        raise CensusError("topologies has no recipes list")
    return payload


def recipe_nodes(topology: dict) -> list[tuple[str, dict]]:
    nodes = []
    for recipe in topology["recipes"]:
        if not isinstance(recipe, dict):
            raise CensusError("malformed recipe entry")
        capability = recipe.get("capability", "?")
        staged = recipe.get("standard_plus") or {}
        for node in staged.get("nodes", []) or []:
            if not isinstance(node, dict):
                raise CensusError(f"malformed node in recipe {capability}")
            nodes.append((capability, node))
    if not nodes:
        raise CensusError("no standard_plus recipe nodes to census")
    return nodes


def declared_advance_classes(nodes: list[tuple[str, dict]]) -> set[str]:
    """The closed advance_class vocabulary, taken from the topology itself."""
    classes = {node["advance_class"] for _cap, node in nodes if node.get("advance_class")}
    if not classes:
        raise CensusError("topology declares no advance_class value")
    return classes


def continuation_kind(node: dict) -> str | None:
    continuation = node.get("continuation")
    if isinstance(continuation, dict):
        return continuation.get("kind")
    if isinstance(continuation, str):
        return continuation
    return None


def tally(nodes, known_classes, where):
    counts = {name: 0 for name in sorted(known_classes)}
    counts[UNSEALED] = 0
    continuations: dict[str, int] = {}
    terminal = with_continuation = 0
    for node in nodes:
        advance = node.get("advance_class")
        if advance is None:
            counts[UNSEALED] += 1
        elif advance in counts:
            counts[advance] += 1
        else:
            raise CensusError(f"undeclared advance_class {advance!r} in {where}")
        kind = continuation_kind(node)
        if kind:
            with_continuation += 1
            continuations[kind] = continuations.get(kind, 0) + 1
        if node.get("terminal"):
            terminal += 1
    return {
        "nodes_total": len(nodes),
        "advance_class": counts,
        "sealed_nodes": len(nodes) - counts[UNSEALED],
        "nodes_with_continuation": with_continuation,
        "continuation_kinds": dict(sorted(continuations.items())),
        "terminal_nodes": terminal,
    }


def build_recipe_axis(topology: dict, known_classes: set[str]) -> dict:
    pairs = recipe_nodes(topology)
    axis = tally([node for _cap, node in pairs], known_classes, "topologies.json")
    axis["capabilities_total"] = len(topology["recipes"])
    axis["capabilities_with_staged_nodes"] = len({cap for cap, _node in pairs})
    return axis


def route_files(routes_dir: Path) -> list[Path]:
    if not routes_dir.is_dir():
        raise CensusError(f"routes directory not found: {routes_dir}")
    # `<route>.outcome.json` is a sibling receipt, not a route: counting it
    # doubles the denominator.
    return sorted(p for p in routes_dir.glob("*.json") if not p.name.endswith(".outcome.json"))


def build_route_axis(routes_dir: Path, known_classes: set[str]) -> dict:
    paths = route_files(routes_dir)
    if not paths:
        raise CensusError(f"no route files in {routes_dir}")
    routes, invalid = [], []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            invalid.append(path.name)
            continue
        if not isinstance(payload, dict):
            invalid.append(path.name)
            continue
        routes.append((path, payload))
    if not routes:
        raise CensusError(f"no parsable route files in {routes_dir}")

    nodes, sealed_routes, staged_routes, sealed_mtimes = [], 0, 0, []
    for path, payload in routes:
        route_nodes = payload.get("nodes")
        if not isinstance(route_nodes, list):
            continue
        route_nodes = [n for n in route_nodes if isinstance(n, dict)]
        staged_routes += 1
        nodes.extend(route_nodes)
        if any(n.get("advance_class") for n in route_nodes):
            sealed_routes += 1
            sealed_mtimes.append(path.stat().st_mtime)

    axis = tally(nodes, known_classes, str(routes_dir)) if nodes else tally([], known_classes, "")
    axis.update({
        "route_files": len(paths),
        "routes_parsed": len(routes),
        "routes_with_nodes": staged_routes,
        "sealed_routes": sealed_routes,
        "sealed_route_percent": round(100 * sealed_routes / staged_routes, 2) if staged_routes else 0.0,
        "invalid_routes": invalid,
        "sealed_window": sealed_window(sealed_mtimes),
    })
    return axis


def sealed_window(mtimes: list[float]) -> dict:
    """Arrival window of sealed routes, measured from file mtime (compiled
    routes carry no seal timestamp of their own)."""
    if not mtimes:
        return {"basis": "file-mtime", "first": None, "last": None, "span_days": 0.0,
                "sealed_routes_per_day": 0.0}
    first, last = min(mtimes), max(mtimes)
    span_days = (last - first) / 86400
    return {
        "basis": "file-mtime",
        "first": datetime.datetime.fromtimestamp(first, datetime.timezone.utc).isoformat(),
        "last": datetime.datetime.fromtimestamp(last, datetime.timezone.utc).isoformat(),
        "span_days": round(span_days, 3),
        "sealed_routes_per_day": round(len(mtimes) / max(span_days, 1.0), 2),
    }


def census(routes_dir: Path, topologies: Path) -> dict:
    topology = load_topology(topologies)
    pairs = recipe_nodes(topology)
    known_classes = declared_advance_classes(pairs)
    recipe_axis = build_recipe_axis(topology, known_classes)
    route_axis = build_route_axis(routes_dir, known_classes)
    return {
        "schema_version": 2,
        "inputs": {"routes": str(routes_dir), "topologies": str(topologies),
                   "topology_schema_version": topology.get("schema_version")},
        "advance_classes_declared": sorted(known_classes),
        "recipe_axis": recipe_axis,
        "route_axis": route_axis,
        # The two axes count different populations; a shared percentage would
        # be meaningless. Kept explicit so no reader silently merges them.
        "axis_denominators": {
            "recipe_nodes": recipe_axis["nodes_total"],
            "live_nodes": route_axis["nodes_total"],
            "shared_denominator": False,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--routes", required=True, type=Path)
    ap.add_argument("--topologies", required=True, type=Path)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        payload = census(args.routes, args.topologies)
    except CensusError as exc:
        print(f"census=fail-closed detail={exc}", file=sys.stderr)
        return EX_NOINPUT if "not found" in str(exc) or "no " in str(exc) else EX_DATAERR
    if args.json:
        print(json.dumps(payload, sort_keys=True, indent=1))
    else:
        recipe, route = payload["recipe_axis"], payload["route_axis"]
        print(f"recipe_axis nodes={recipe['nodes_total']} continuation={recipe['nodes_with_continuation']}"
              f" sealed={recipe['sealed_nodes']} terminal={recipe['terminal_nodes']}"
              f" capabilities={recipe['capabilities_with_staged_nodes']}/{recipe['capabilities_total']}")
        print(f"route_axis routes={route['routes_with_nodes']}/{route['route_files']}"
              f" sealed_routes={route['sealed_routes']} ({route['sealed_route_percent']}%)"
              f" nodes={route['nodes_total']} " +
              " ".join(f"{k}={v}" for k, v in route["advance_class"].items()) +
              f" invalid={len(route['invalid_routes'])}")
    return 1 if payload["route_axis"]["invalid_routes"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
