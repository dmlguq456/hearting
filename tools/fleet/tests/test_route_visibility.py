"""Route visibility reads only existing route records and labels them (display only)."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from tools.fleet import route_visibility as rv


def _record(root, rid, shape, nodes, *, intensity="quick", source="shape-default",
            gates=None, human_gates=(), route_plan=None, topology="one-shot-owner", **extra):
    record = {
        "schema_version": 2, "route_id": rid, "route_hash": "sha256:" + rid,
        "artifact_root": str(root), "capability": "autopilot-code",
        "requested_intensity": intensity, "effective_intensity": intensity,
        "execution_topology": topology, "tracking": "untracked",
        "selection": {"shape": shape, "route_origin": "compose", "direct_predicates": [],
                      "escalation_basis": [], "inline_reason": None,
                      "selection_basis": [{"axis": "compose-intensity", "signal": intensity,
                                           "source": source}]},
        "nodes": nodes, "completion_gates": gates or [n["completion_gate"] for n in nodes],
        "human_gates": list(human_gates),
        "workflow_contract": {"terminal_nodes": [n["id"] for n in nodes if n.get("terminal")]},
    }
    if route_plan is not None:
        record["route_plan"] = route_plan
    record.update(extra)
    return record


def _write_outcome(root, record, **fields):
    routes = Path(root) / ".runtime" / "routes"
    routes.mkdir(parents=True, exist_ok=True)
    data = {"route_id": record["route_id"], "route_hash": record["route_hash"], **fields}
    (routes / (record["route_id"] + ".outcome.json")).write_text(json.dumps(data), encoding="utf-8")


class RouteVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".agent_reports"
        (self.root / "research" / "inputs").mkdir(parents=True)
        rv._OUTCOME_CACHE.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_closed_direct_route_shows_tests_ci_same_head_and_worker_records(self):
        (self.root / "research" / "inputs" / "RV-BC-FIXTURE.md").write_text("review", encoding="utf-8")
        record = _record(self.root, "rt-direct", "direct", [
            {"id": "inline", "completion_gate": "inline-complete", "terminal": True}],
            intensity="direct", topology="inline")
        record["selection"]["direct_predicates"] = ["known-scope"]
        head = "f0086bdd4f418375f54d94052a6daab567c24281"
        _write_outcome(self.root, record, closed_at="2026-11-09T08:32:15Z", head_commit=head,
                       terminal_gate_proven=True, inline_finish_id="x",
                       terminal_gates={"inline": {"passed": True, "reason": "completion-marker-verified",
                                                  "evidence": "/fixture/journal.md#a"}},
                       summary="Three focused suites pass.\nExact CI42 attempt1: all13 jobs SUCCESS for "
                               + head + ".\nReviewer report: .agent_reports/research/inputs/RV-BC-FIXTURE.md\n"
                               "api_key=hidden-value must never show.")
        desc = rv.describe(record)
        self.assertEqual(desc["shape"], "direct")
        self.assertEqual(desc["basis"]["kind"], "default")
        self.assertEqual(desc["stages"][0]["state"], "done")
        ver = desc["verification"]
        self.assertTrue(ver["ci"][0]["same_head"])
        self.assertEqual(ver["ci"][0]["tone"], "good")
        self.assertIn("Three focused suites pass.", [row["text"] for row in ver["tests"]])
        self.assertEqual(ver["worker_records"][0]["kind"], "RV")
        self.assertTrue(ver["worker_records"][0]["path"].endswith("RV-BC-FIXTURE.md"))
        self.assertEqual(desc["next"]["kind"], "closed")
        self.assertEqual(ver["result"], "Three focused suites pass.")
        self.assertIn(("result", "Three focused suites pass.", "info"), rv.detail_lines(desc))
        text = json.dumps(desc)
        self.assertNotIn("hidden-value", text)
        self.assertEqual(rv.summary_text(desc), "direct·default · RV record · CI ✓ same-head · gate ✓")

    def test_open_solo_route_without_reviewer_says_so_and_names_next_step(self):
        record = _record(self.root, "rt-solo", "solo", [
            {"id": "one-shot", "completion_gate": "quick-complete", "terminal": True,
             "unit": "_kernel/owner"}])
        view = {"nodes": [{"id": "one-shot", "state": "active", "harness": "claude",
                           "model": "opus", "gate_passed": None}]}
        desc = rv.describe(record, view)
        self.assertEqual(desc["current"], "one-shot")
        self.assertFalse(desc["verification"]["independent_review"])
        self.assertEqual(desc["next"], {"kind": "wait", "text": "wait · one-shot (claude) running"})
        labels = [label for label, _text, _tone in rv.detail_lines(desc)]
        self.assertEqual(labels[:3], ["route", "why", "stages"])
        self.assertIn(("review", "no independent reviewer stage in this route", "dim"), rv.detail_lines(desc))

    def test_framed_route_waits_for_the_person_after_both_legs(self):
        nodes = [{"id": "frame", "unit": "plan/frame", "completion_gate": "route-frame"},
                 {"id": "frame-alternative", "unit": "plan/frame", "completion_gate": "route-frame"},
                 {"id": "route-decision", "kind": "runtime-terminal", "completion_gate": "route-decision",
                  "depends_on": ["frame", "frame-alternative"], "terminal": True}]
        record = _record(self.root, "rt-frame", "framed", nodes, intensity="standard",
                         human_gates=["frame-review"], topology="staged")
        view = {"nodes": [{"id": "frame", "state": "done", "gate_passed": True},
                          {"id": "frame-alternative", "state": "done", "gate_passed": True},
                          {"id": "route-decision", "state": "pending"}]}
        desc = rv.describe(record, view)
        self.assertEqual(desc["next"], {"kind": "person", "text": "answer · frame-review"})
        self.assertEqual([g["node"] for g in desc["verification"]["completion_gates"]], ["route-decision"])
        self.assertIn("frame 2-way", rv.summary_text(desc))
        self.assertNotIn("no reviewer", rv.summary_text(desc))

    def test_staged_route_from_frame_proposal_lists_reviewers_and_explicit_basis(self):
        nodes = [{"id": "execute", "unit": "dev/backend", "completion_gate": "code-execute"},
                 {"id": "impl-review", "unit": "qa/code-review", "kind": "review-worker",
                  "completion_gate": "code-impl-review", "depends_on": ["execute"]},
                 {"id": "test", "unit": "qa/test", "completion_gate": "code-test",
                  "depends_on": ["impl-review"], "terminal": True}]
        record = _record(self.root, "rt-staged", "staged", nodes, intensity="standard",
                         source="explicit", topology="staged", route_plan={"decision": "d", "index": 0})
        view = {"nodes": [{"id": "execute", "state": "done", "gate_passed": True},
                          {"id": "impl-review", "state": "failed", "harness": "codex"},
                          {"id": "test", "state": "pending"}]}
        desc = rv.describe(record, view)
        self.assertEqual(desc["basis"]["kind"], "frame-proposal")
        self.assertEqual(desc["verification"]["reviewers"][0]["harness"], "codex")
        self.assertEqual(desc["next"]["kind"], "resume")
        self.assertIn("review 0/1 ✕", rv.summary_text(desc))
        record.pop("route_plan")
        self.assertEqual(rv.describe(record, view)["basis"]["kind"], "user")

    def test_closed_route_without_markers_is_no_claim_not_pending(self):
        nodes = [{"id": "plan", "completion_gate": "code-plan"},
                 {"id": "report", "completion_gate": "code-report", "terminal": True,
                  "depends_on": ["plan"]}]
        record = _record(self.root, "rt-old", "staged", nodes, topology="staged")
        _write_outcome(self.root, record, closed_at="2026-10-27T21:10:39Z", terminal_gate_proven=True,
                       terminal_gates={"report": {"passed": True}})
        desc = rv.describe(record)
        self.assertEqual([s["state"] for s in desc["stages"]], ["unrecorded", "done"])
        self.assertIsNone(desc["current"])

    def test_foreign_outcome_and_junk_never_break_the_screen(self):
        record = _record(self.root, "rt-x", "solo", [{"id": "one-shot", "completion_gate": "g"}])
        _write_outcome(self.root, dict(record, route_hash="sha256:other"), closed_at="2026-01-01T00:00:00Z")
        self.assertIsNone(rv.read_outcome(record))
        self.assertEqual(rv.describe(None), {})
        self.assertEqual(rv.describe({"route_id": "rt-y", "nodes": "junk"})["stages"], [])
        self.assertEqual(rv.bounded({}), {})

    def test_bounded_projection_is_string_only_and_clipped(self):
        record = _record(self.root, "rt-b", "solo", [{"id": "one-shot", "completion_gate": "g", "terminal": True}])
        out = rv.bounded(rv.describe(record))
        self.assertEqual(out["schema"], rv.SCHEMA)
        for label, text, tone in out["lines"]:
            self.assertTrue(all(isinstance(v, str) for v in (label, text, tone)))
            self.assertLessEqual(len(text), 200)
        self.assertNotIn("one-shot", out["card"])   # the card leaves the stage rail to the row

    def test_module_never_writes(self):
        source = Path(rv.__file__).read_text(encoding="utf-8")
        for pattern in ('open(path, "w', ".write(", "os.replace(", "write_text(", ".unlink("):
            self.assertNotIn(pattern, source)


class RouteVisibilityRenderTests(unittest.TestCase):
    VIS = {"schema": rv.SCHEMA, "route_id": "rt-1", "summary": "s", "card": "solo·default  no reviewer",
           "shape": "solo", "basis": "default", "next": "start · one-shot",
           "lines": [["route", "solo · default · autopilot-code quick", "info"],
                     ["review", "no independent reviewer stage in this route", "dim"],
                     ["gate", "quick-complete ✓", "good"], ["next", "closed · nothing pending", "good"]]}

    def test_route_row_appends_the_card_only_when_it_fits(self):
        from tools.fleet.explorer import tables
        from tools.fleet.explorer.grid import CellGrid
        route = dict(legs=[["code", "active", "0/1"]], stages=[dict(name="one-shot", st="active")],
                     tree_label="code", visibility=self.VIS)
        wide = CellGrid(120, 1)
        tables._draw_route_progress(wide, 0, 120, 0, route)
        self.assertIn("solo·default  no reviewer", wide.rows_text()[0])
        narrow = CellGrid(24, 1)
        tables._draw_route_progress(narrow, 0, 24, 0, route)
        self.assertNotIn("solo·default", narrow.rows_text()[0])
        plain = CellGrid(120, 1)
        tables._draw_route_progress(plain, 0, 120, 0, dict(route, visibility=None))
        self.assertNotIn("reviewer", plain.rows_text()[0])

    def test_route_page_prints_the_visibility_lines_and_folds_extra(self):
        from tools.fleet.explorer import pages
        from tools.fleet.explorer.grid import CellGrid
        grid = CellGrid(140, 40)
        y = pages._route_visibility_lines(grid, self.VIS, 0, 138, 3, 38)
        text = "\n".join(grid.rows_text())
        self.assertEqual(y, 7)
        for word in ("route:", "review:", "no independent reviewer", "gate:", "next:", "closed · nothing pending"):
            self.assertIn(word, text)
        short = CellGrid(140, 22)
        y = pages._route_visibility_lines(short, self.VIS, 0, 138, 3, 20)
        self.assertEqual(y, 3)          # no room: the route frame keeps its rows
        tight = CellGrid(140, 24)
        y = pages._route_visibility_lines(tight, self.VIS, 0, 138, 3, 22)
        self.assertIn("+1 more", "\n".join(tight.rows_text()))
        self.assertEqual(pages._route_visibility_lines(grid, None, 0, 138, 3, 38), 3)


if __name__ == "__main__":
    unittest.main()
