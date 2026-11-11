"""A one-shot route's run is its depth-1 owner, in Fleet and in the session shell alike."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from tools.fleet import session_panel
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.live import build_view_model, session_route_status
from tools.fleet.explorer.state import NavState
from tools.fleet.model import DispatchJob, Session, WorkProjection

ROOT = Path(__file__).resolve().parents[3]
_ENGLISH = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})


def setUpModule():
    _ENGLISH.start()


def tearDownModule():
    _ENGLISH.stop()


def _record(rid, topology, nodes, shape):
    return {"schema_version": 2, "route_id": rid, "route_hash": "sha256:" + rid,
            "artifact_root": "/nonexistent-root", "capability": "autopilot-code", "capability_mode": "dev",
            "requested_intensity": "quick", "effective_intensity": "quick",
            "execution_topology": topology, "tracking": "untracked",
            "selection": {"shape": shape, "route_origin": "compose", "direct_predicates": [],
                          "escalation_basis": [], "inline_reason": None, "selection_basis": []},
            "nodes": nodes, "completion_gates": [n["completion_gate"] for n in nodes], "human_gates": [],
            "workflow_contract": {"terminal_nodes": [nodes[-1]["id"]]}, "owner_attempt_id": "att-" + rid,
            "display_title": "Fixture " + shape + " route"}


def _snapshot(harness="claude", worker_type="owner"):
    solo = _record("rt-solo", "one-shot-owner", [{"id": "one-shot", "completion_gate": "quick-complete",
                                                  "terminal": True, "unit": "_kernel/owner"}], "solo")
    solo_view = {"nodes": [{"id": "one-shot", "state": "active", "harness": "codex", "unit": "_kernel/owner",
                            "attempt_id": "att-rt-solo", "elapsed_min": 5}], "liveness": "working"}
    staged = _record("rt-staged", "staged", [{"id": "code-plan", "completion_gate": "p"},
                                             {"id": "code-execute", "completion_gate": "e", "unit": "executor/code"}],
                     "staged")
    staged_view = {"nodes": [{"id": "code-plan", "state": "done", "harness": "claude"},
                             {"id": "code-execute", "state": "active", "harness": "codex", "unit": "executor/code",
                              "attempt_id": "att-w2", "elapsed_min": 3}], "liveness": "working"}
    projection = lambda rid, record, view: WorkProjection(source="route-exact", route_id=rid,
                                                          _route_view={"record": record, "view": view})
    session = Session(harness=harness, pid=os.getpid(), session_id="sid-main", session_tag="s1",
                      cwd="/fixture/repo", liveness="idle")
    jobs = [
        DispatchJob(key="o1", slug="rt-solo", harness="codex", depth=1, dispatch_depth=1, worker_type=worker_type,
                    unit="_kernel/owner", route_id="rt-solo", route_node="one-shot", attempt_id="att-rt-solo",
                    parent_sid="sid-main", liveness="working", elapsed_min=5, model="claude-opus-5-5",
                    effort="high", work_projection=projection("rt-solo", solo, solo_view)),
        DispatchJob(key="o2", slug="rt-staged", harness="claude", depth=1, dispatch_depth=1, worker_type="owner",
                    unit="_kernel/owner", route_id="rt-staged", attempt_id="att-rt-staged", parent_sid="sid-main",
                    liveness="idle", elapsed_min=9, model="claude-opus-5-5", effort="high",
                    work_projection=projection("rt-staged", staged, staged_view)),
        DispatchJob(key="w2", slug="rt-staged-exec", harness="codex", depth=2, dispatch_depth=2, worker_type="stage",
                    worker_mode="executor/code", unit="executor/code", route_id="rt-staged",
                    route_node="code-execute", attempt_id="att-w2", parent_sid="sid-main", liveness="working",
                    elapsed_min=3),
    ]
    return {"sessions": [session], "jobs": jobs, "resources": []}


class OneShotOwnerTests(unittest.TestCase):
    def routes(self, **kw):
        vm = build_view_model(_snapshot(**kw), now=2000.0).to_mapping()
        return vm, {route["id"]: route for route in vm["routes"]}

    def test_one_shot_run_is_the_routes_owner_not_a_worker(self):
        for worker_type in ("owner", None):          # an older row without worker_type still says one-shot
            _vm, routes = self.routes(worker_type=worker_type)
            solo = routes["rt-solo"]
            self.assertEqual((solo["harness"], solo["model"][0], solo["effort"]), ("codex", "Opus 5.5", "high"))
            self.assertEqual(solo["session_tag"], "s1")          # the session that launched it
            self.assertEqual(solo["worker"][0], "owner")
            self.assertEqual(solo["worker_stage"], "one-shot")
            staged = routes["rt-staged"]
            self.assertEqual(staged["harness"], "claude")
            self.assertEqual(staged["worker"][0], "worker")      # a real depth-2 worker stays a worker

    def test_overview_and_routes_show_the_owner_harness_model_and_effort(self):
        vm, _routes = self.routes()
        overview = "\n".join(draw(vm, NavState(view="OVERVIEW", scope="-"), 150, 40).text_rows())
        line = next(row for row in overview.splitlines() if "Fixture solo route" in row)
        self.assertIn("[s1]", line)
        self.assertIn("codex (Opus 5.5·high)", line)
        self.assertIn("one-shot", line)
        page = draw(vm, NavState(view="ROUTES", scope="-"), 150, 40).text_rows()
        index = next(i for i, row in enumerate(page) if "Fixture solo route" in row)
        self.assertIn("codex", page[index])
        self.assertIn("└ owner", page[index + 1])
        self.assertNotIn("worker", page[index + 1])
        staged = next(i for i, row in enumerate(page) if "Fixture staged route" in row)
        self.assertIn("└ worker", page[staged + 1])

    def test_shell_panel_reads_the_same_owner_for_every_harness(self):
        # Spec 2026-11-11: one row per route with Fleet's own owner values, added
        # by the shared line helper from the same snapshot the producer reads.
        from tools.fleet.session_line import session_route_status as shell_route_status
        script = r'''
const v=require(process.argv[1]),m=JSON.parse(process.argv[2]);
const out=[];for(const side of [true,false]){const r=v.render({width:side?44:120,height:side?30:8,side,model:m,
 sid:'sid-main',questions:()=>[],configuration:{},expandedKeys:['route:rt-solo']});out.push(r.rows.map(row=>row.map(s=>s.text).join('')).join('\n'));}
console.log(JSON.stringify(out));'''
        for harness in ("claude", "codex", "opencode"):
            # Wall clock: the row keeps an absolute start time and the panel ages it.
            labels, panels = shell_route_status(_snapshot(harness), include_details=True)
            # Fleet's own judgment: the staged route's owner waits on its worker (●).
            self.assertEqual(set(labels.values()), {"⏳ route 2 ⠏1 ●1"})
            presented = session_panel.presentation({"model": "x", "effort": "high", "route_label": "⏳ route 2 ⠏1 ●1"},
                                                   {"route_panels": panels}, harness, "sid-main", 60)
            solo = next(route for route in presented["routes"] if route["id"] == "rt-solo")
            self.assertEqual(solo["owner"], "owner · codex (Opus 5.5·high)")
            self.assertEqual((solo["harness"], solo["model_text"], solo["effort"]), ("codex", "Opus 5.5", "high"))
            self.assertEqual(solo["elapsed"], "5m")
            staged = next(route for route in presented["routes"] if route["id"] == "rt-staged")
            self.assertEqual(staged["owner_line"], "owner · claude (Opus 5.5·high)")
            self.assertEqual(staged["harness"], "codex")              # the live worker of the shown stage
            self.assertIn("code-execute ●", staged["flow"])
            for route in presented["routes"]:
                route["paint"] = {name: {"text": route[name]} for name in ("glyph", "harness", "model_text", "effort", "ctx")
                                  if route.get(name)}
            result = subprocess.run(["node", "-e", script, str(ROOT / "utilities/session-shell/panel-view.cjs"),
                                     json.dumps(presented)], capture_output=True, text=True, check=True)
            side, bottom = json.loads(result.stdout)
            # The title row repeats the first route's title; its route row adds harness and time.
            rows = [line for line in side.splitlines() if "Fixture solo route" in line]
            self.assertTrue(any("codex" in line and "5m" in line for line in rows), side)
            self.assertIn("Opus 5.5 high", side)                      # the opened row
            self.assertIn("codex Opus 5.5 high", bottom)
            self.assertNotIn("hb_", side + bottom)
            self.assertEqual(len(bottom.splitlines()), 8)             # the bottom panel does not grow

    def test_one_shot_summary_names_its_completion_gate_instead_of_staying_unknown(self):
        codes = lambda gate: {"codes": {"schema": "route_visibility_codes_v1", "shape": "solo",
                                        "stages": [["one-shot", "work", "done", "1"]],
                                        "verification": {"review": "none", "ci": "none", "gate": gate}}}
        self.assertTrue(session_panel.route_evidence(codes("proven"))["summary"].endswith("gate✓"))
        self.assertTrue(session_panel.route_evidence(codes("failed"))["summary"].endswith("gate✕"))
        self.assertTrue(session_panel.route_evidence(codes("unproven"))["summary"].endswith("gate·"))
        self.assertNotIn("gate", session_panel.route_evidence(codes("none"))["summary"])


if __name__ == "__main__":
    unittest.main()
