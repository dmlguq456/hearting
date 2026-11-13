"""A stage a plan added (`plan-<id>`) shows like any other sealed route node."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fleet import render, route  # noqa: E402
from fleet.model import DispatchJob  # noqa: E402

STAGE = {"unit": "qa/test", "kind": "pipeline-stage", "completion_gate": "code-test",
         "dispatch_depth": 2, "model_profile": "balanced"}
RECORD = {"route_id": "rt-00000000000000a1", "route_hash": "sha256:fixture", "nodes": [
    {**STAGE, "id": "test", "depends_on": []},
    {**STAGE, "id": "plan-measure", "depends_on": ["test"], "part": "autopilot-code:test",
     "plan_stage": {"id": "measure", "template": "test"}},
    {**STAGE, "id": "lab-smoke", "depends_on": ["plan-measure"], "part": "autopilot-lab:smoke"},
]}


class PlanStageNodeTest(unittest.TestCase):
    def test_a_plan_stage_has_the_same_row_and_label_as_a_borrowed_stage(self):
        route.clear_cache()
        nodes = {n["id"]: n for n in route._record_view(RECORD, RECORD["route_id"], [], {}, 100.0)["nodes"]}
        drop = lambda node: {k: v for k, v in node.items() if k not in ("id", "depends_on", "level")}
        self.assertEqual(drop(nodes["plan-measure"]), drop(nodes["lab-smoke"]))
        self.assertEqual((nodes["plan-measure"]["unit"], nodes["plan-measure"]["gate"]), ("qa/test", "code-test"))
        for node in ("plan-measure", "lab-smoke"):
            job = DispatchJob(key=node, slug="x-" + node, route_id=RECORD["route_id"], route_node=node,
                              depth=2, liveness="working")
            self.assertEqual((render._dispatch_stage_label(job), render._entry_skill(job)), (node, node))


if __name__ == "__main__":
    unittest.main()
