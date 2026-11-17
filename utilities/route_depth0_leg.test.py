#!/usr/bin/env python3
"""`route_plan.is_depth0_leg`: the one judgment for a depth-1 worker launched without an owner.

Pins the behaviour-preserving claim of that judgment: over every recipe the registry ships, exactly
the frame legs read true. Owners, depth-2 stage nodes, unsealed nodes and broken input read false.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import route_plan as RP  # noqa: E402

REGISTRY = json.loads((HERE.parent / "capabilities" / "topologies.json").read_text(encoding="utf-8"))


def _shipped_nodes():
    """Every node of every shipped recipe, in any graph the recipe declares."""
    stack = [REGISTRY["recipes"]]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if "id" in item and "kind" in item and "dispatch_depth" in item:
                yield item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


class Depth0LegTest(unittest.TestCase):
    def test_frame_leg_is_a_depth0_leg(self):
        leg = {"id": "frame", "kind": "map-worker", "worker_type": "frame", "unit": "plan/frame",
               "dispatch_depth": 1, "launch_authority": "depth-0"}
        self.assertTrue(RP.is_depth0_leg(leg))
        self.assertTrue(RP.is_depth0_leg(dict(leg, id="frame-alternative")))

    def test_owner_and_stage_workers_are_not(self):
        owner = {"id": "one-shot", "kind": "capability-owner", "worker_type": "owner",
                 "unit": "_kernel/owner", "dispatch_depth": 1}
        stage = {"id": "plan-check", "kind": "map-worker", "worker_type": "stage",
                 "unit": "qa/plan-review", "dispatch_depth": 2}
        self.assertFalse(RP.is_depth0_leg(owner))
        self.assertFalse(RP.is_depth0_leg(stage))
        # A different launch authority never reads as a depth-0 leg.
        self.assertFalse(RP.is_depth0_leg(dict(owner, launch_authority="conductor")))

    def test_unsealed_or_broken_nodes_are_not(self):
        leg = {"id": "frame", "worker_type": "frame", "unit": "plan/frame",
               "dispatch_depth": 1, "launch_authority": "depth-0"}
        unsealed = {k: v for k, v in leg.items() if k != "launch_authority"}
        self.assertFalse(RP.is_depth0_leg(unsealed))
        for broken in (dict(leg, launch_authority=None), dict(leg, launch_authority="Depth-0"),
                       dict(leg, dispatch_depth=2), dict(leg, dispatch_depth=0),
                       dict(leg, dispatch_depth=True), dict(leg, dispatch_depth="1"),
                       {k: v for k, v in leg.items() if k != "dispatch_depth"},
                       {}, None, "frame", ["frame"], 1):
            with self.subTest(broken=broken):
                self.assertFalse(RP.is_depth0_leg(broken))

    def test_shipped_recipes_seal_it_on_frame_legs_only(self):
        nodes = list(_shipped_nodes())
        legs = [n for n in nodes if RP.is_depth0_leg(n)]
        self.assertTrue(legs)
        # The frame legs, and the `unit` recipe's one template leg (no worker type or unit of its own:
        # a route fills them from the unit head, so a shipped recipe never names one).
        frame_legs = [n for n in legs if n.get("worker_type") == "frame"]
        template = [n for n in legs if n.get("worker_type") is None]
        self.assertEqual(len(frame_legs) + len(template), len(legs))
        self.assertEqual([n["id"] for n in template], ["unit"])
        self.assertEqual({n.get("unit") for n in frame_legs}, {"plan/frame"})
        self.assertEqual([n["id"] for n in nodes if n.get("worker_type") == "frame" and not RP.is_depth0_leg(n)], [])

    def test_frame_legs_still_reads_the_pair(self):
        nodes = [{"id": "frame", "worker_type": "frame", "dispatch_depth": 1, "launch_authority": "depth-0"},
                 {"id": "frame-alternative", "worker_type": "frame", "dispatch_depth": 1, "launch_authority": "depth-0"},
                 {"id": "one-shot", "worker_type": "owner", "dispatch_depth": 1}]
        self.assertEqual(RP.frame_legs({"nodes": nodes}), ("frame", "frame-alternative"))
        self.assertEqual(RP.frame_legs({"nodes": nodes[2:]}), ())


if __name__ == "__main__":
    unittest.main()
