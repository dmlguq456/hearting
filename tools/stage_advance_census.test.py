#!/usr/bin/env python3
"""Fixtures for the SD-110 stage-advance census.

Pins the two separate axes (static recipe vs live route), the node
aggregation, and the fail-closed paths -- including the fact that
``--topologies`` is really read, not just accepted.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CENSUS = ROOT / "tools" / "stage-advance-census.py"
REAL_TOPOLOGIES = ROOT / "capabilities" / "topologies.json"


def node(node_id, advance=None, continuation=None, terminal=False):
    payload = {"id": node_id}
    if advance:
        payload["advance_class"] = advance
    if continuation:
        payload["continuation"] = {"kind": continuation}
    if terminal:
        payload["terminal"] = True
    return payload


def run_census(routes, topologies, *extra):
    return subprocess.run(
        [sys.executable, str(CENSUS), "--routes", str(routes),
         "--topologies", str(topologies), *extra],
        capture_output=True, text=True,
    )


class CensusFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.routes = self.root / "routes"
        self.routes.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_topology(self, recipes, path_name="topologies.json"):
        path = self.root / path_name
        path.write_text(json.dumps({"schema_version": 10, "recipes": recipes}), encoding="utf-8")
        return path

    def write_route(self, name, nodes):
        path = self.routes / name
        path.write_text(json.dumps({"route_id": name, "nodes": nodes}), encoding="utf-8")
        return path

    def default_topology(self):
        return self.write_topology([
            {"capability": "autopilot-code", "standard_plus": {"nodes": [
                node("plan", "runtime-eligible", "inline-next"),
                node("execute", "runtime-eligible", "inline-next"),
                node("report", "model-required", terminal=True),
            ]}},
            {"capability": "autopilot-spec", "standard_plus": {"nodes": [
                node("draft", "runtime-eligible", "human-gate"),
            ]}},
            {"capability": "post-it"},  # no staged graph at all
        ])

    def json_census(self, topologies=None):
        result = run_census(self.routes, topologies or self.default_topology(), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    # --- the two axes are computed and never share a denominator ----------

    def test_recipe_axis_and_route_axis_are_counted_separately(self):
        self.write_route("rt-a.json", [
            node("plan", "runtime-eligible", "inline-next"),
            node("execute", None, "inline-next"),
            node("report", None, terminal=True),
        ])
        self.write_route("rt-b.json", [node("solo", None, terminal=True)])
        payload = self.json_census()

        recipe, route = payload["recipe_axis"], payload["route_axis"]
        self.assertEqual(recipe["nodes_total"], 4)
        self.assertEqual(recipe["nodes_with_continuation"], 3)
        self.assertEqual(recipe["terminal_nodes"], 1)
        self.assertEqual(recipe["advance_class"],
                         {"model-required": 1, "runtime-eligible": 3, "unsealed": 0})
        self.assertEqual(recipe["capabilities_total"], 3)
        self.assertEqual(recipe["capabilities_with_staged_nodes"], 2)

        self.assertEqual(route["nodes_total"], 4)
        self.assertEqual(route["advance_class"],
                         {"model-required": 0, "runtime-eligible": 1, "unsealed": 3})
        self.assertEqual(route["sealed_nodes"], 1)
        self.assertEqual(route["sealed_routes"], 1)
        self.assertEqual(route["routes_with_nodes"], 2)
        self.assertEqual(route["sealed_route_percent"], 50.0)

        # identical node totals here, but the populations stay distinct.
        self.assertFalse(payload["axis_denominators"]["shared_denominator"])
        self.assertEqual(payload["axis_denominators"]["recipe_nodes"], 4)
        self.assertEqual(payload["axis_denominators"]["live_nodes"], 4)

    def test_outcome_receipts_are_not_counted_as_routes(self):
        self.write_route("rt-a.json", [node("plan", "runtime-eligible")])
        (self.routes / "rt-a.outcome.json").write_text(json.dumps({"nodes": [node("plan")]}),
                                                       encoding="utf-8")
        route = self.json_census()["route_axis"]
        self.assertEqual(route["route_files"], 1)
        self.assertEqual(route["nodes_total"], 1)

    def test_repo_topology_pins_the_documented_recipe_axis(self):
        # The static numbers quoted in docs/stage-advance-canary.md come from
        # this file and nowhere else.
        self.write_route("rt-a.json", [node("plan", "runtime-eligible")])
        recipe = self.json_census(REAL_TOPOLOGIES)["recipe_axis"]
        self.assertEqual(recipe["nodes_total"], 53)
        self.assertEqual(recipe["nodes_with_continuation"], 40)
        self.assertEqual(recipe["terminal_nodes"], 13)
        self.assertEqual(recipe["advance_class"]["runtime-eligible"], 35)
        self.assertEqual(recipe["advance_class"]["model-required"], 18)
        self.assertEqual(recipe["capabilities_with_staged_nodes"], 12)
        self.assertEqual(recipe["capabilities_total"], 13)

    # --- --topologies is really consumed ----------------------------------

    def test_topology_drives_the_advance_class_vocabulary(self):
        self.write_route("rt-a.json", [node("plan", "eventually-maybe")])
        result = run_census(self.routes, self.default_topology(), "--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("undeclared advance_class", result.stderr)

    def test_missing_or_recipeless_topology_fails_closed(self):
        self.write_route("rt-a.json", [node("plan", "runtime-eligible")])
        absent = self.root / "no-such-topologies.json"
        empty = self.write_topology([], "empty.json")
        bare = self.root / "bare.json"
        bare.write_text(json.dumps({"schema_version": 10}), encoding="utf-8")
        for topologies in (absent, empty, bare):
            with self.subTest(topologies=topologies.name):
                result = run_census(self.routes, topologies, "--json")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("census=fail-closed", result.stderr)

    # --- no corpus means no report ----------------------------------------

    def test_absent_or_empty_route_corpus_produces_no_report(self):
        topologies = self.default_topology()
        for routes in (self.root / "missing-routes", self.routes):
            with self.subTest(routes=routes.name):
                result = run_census(routes, topologies, "--json")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("census=fail-closed", result.stderr)

    def test_unparsable_route_is_reported_and_exits_nonzero(self):
        self.write_route("rt-a.json", [node("plan", "runtime-eligible")])
        (self.routes / "rt-bad.json").write_text("{not json", encoding="utf-8")
        result = run_census(self.routes, self.default_topology(), "--json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["route_axis"]["invalid_routes"], ["rt-bad.json"])


if __name__ == "__main__":
    unittest.main()
