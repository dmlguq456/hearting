"""Route visibility reads only existing route records and labels them (display only)."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
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
        self.assertEqual(ver["ci"][0]["tone"], "info")      # shown as recorded, never judged
        self.assertIn("Three focused suites pass.", [row["text"] for row in ver["tests"]])
        self.assertEqual(ver["worker_records"][0]["kind"], "RV")
        self.assertTrue(ver["worker_records"][0]["path"].endswith("RV-BC-FIXTURE.md"))
        self.assertEqual(desc["next"]["kind"], "closed")
        self.assertIsNone(ver["result"])           # already shown as a test line, not repeated
        self.assertIn(("test", "Three focused suites pass.", "info"), rv.detail_lines(desc))
        text = json.dumps(desc)
        self.assertNotIn("hidden-value", text)
        self.assertEqual(rv.summary_text(desc), "direct·default · RV record · CI same-head · gate ✓")

    def test_open_solo_route_without_reviewer_says_so_and_names_next_step(self):
        record = _record(self.root, "rt-solo", "solo", [
            {"id": "one-shot", "completion_gate": "quick-complete", "terminal": True,
             "unit": "_kernel/owner"}])
        view = {"nodes": [{"id": "one-shot", "state": "active", "harness": "claude",
                           "model": "opus", "gate_passed": None}]}
        desc = rv.describe(record, view)
        self.assertEqual(desc["current"], "one-shot")
        self.assertFalse(desc["verification"]["independent_review"])
        self.assertEqual(desc["next"], {"kind": "wait", "node": "one-shot", "harness": "claude",
                                        "text": "wait · one-shot (claude) running"})
        labels = [label for label, _text, _tone in rv.detail_lines(desc)]
        self.assertEqual(labels[:4], ["route", "why", "owner", "stages"])
        self.assertIn(("review", "no independent reviewer stage in this route", "dim"), rv.detail_lines(desc))

    def test_solo_route_read_without_fleet_shows_the_registry_owner(self):
        record = _record(self.root, "rt-solo2", "solo", [
            {"id": "one-shot", "completion_gate": "quick-complete", "terminal": True}])
        job = {"attempt_id": "att-1", "liveness": "working", "status": "open", "harness": "claude",
               "model": "opus", "route_node": "one-shot", "_context_execution_started_at": 1794277391.0,
               "elapsed_min": 52, "attempt_decision": {"action": "wait", "reason": "local-pid-live"}}
        desc = rv.describe(record, None, owner=rv.owner_from_job(job))
        self.assertEqual(desc["stages"][0]["state"], "active")
        self.assertEqual(desc["owner"]["started_at"], 1794277391.0)
        self.assertTrue(desc["next"]["text"].startswith("wait · one-shot"))
        owner_line = next(text for label, text, _tone in rv.detail_lines(desc) if label == "owner")
        self.assertIn("working since", owner_line)
        self.assertIn("registry: wait (local-pid-live)", owner_line)
        ended = rv.describe(record, None, owner=rv.owner_from_job(dict(job, liveness="dead", note="exit-1")))
        self.assertEqual(ended["next"]["kind"], "attention")
        self.assertIn("route not closed", ended["next"]["text"])
        alone = rv.describe(record)
        self.assertIn(("owner", "no owner attempt in the dispatch registry", "dim"), rv.detail_lines(alone))

    def test_observe_reads_owner_and_view_from_given_registry_rows(self):
        from tools.fleet.model import DispatchJob
        record = _record(self.root, "rt-obs", "solo", [
            {"id": "one-shot", "completion_gate": "quick-complete", "terminal": True, "dispatch_depth": 1}],
            owner_attempt_id="att-obs")
        job = DispatchJob(key="owner", slug="rt-obs", harness="codex", depth=1, route_id="rt-obs",
                          route_node="one-shot", attempt_id="att-obs", liveness="working")
        view, owner = rv.observe(record, jobs=[job], node_evidence={}, now=1000.0)
        self.assertEqual(owner["attempt_id"], "att-obs")
        self.assertEqual(owner["harness"], "codex")
        self.assertEqual(view["nodes"][0]["state"], "active")

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
        self.assertEqual(desc["next"], {"kind": "person", "node": "route-decision", "harness": None,
                                        "text": "answer · frame-review"})
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
        reviewer = next(text for label, text, _tone in rv.detail_lines(desc) if label == "review")
        self.assertEqual(reviewer, "impl-review · codex · failed")
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

    # --- PR #1429 review (RV-PR1429-1110) regressions: each uses the recorded counterexample ---

    def test_p1_1_card_marks_ci_only_for_a_same_head_record(self):
        record = _record(self.root, "rt-ci", "direct", [
            {"id": "inline", "completion_gate": "inline-complete", "terminal": True}],
            intensity="direct", topology="inline")
        head = "a0e73fd49c88e15c6bd4c891ea9b36c301e159be"
        for summary in ("CI42465679913 is not green: two jobs failed.",
                        "외부push/PR/CI·설치·pending수정0.",
                        "CI and independent review evidence are recorded in the author report."):
            with self.subTest(summary=summary):
                rv._OUTCOME_CACHE.clear()
                _write_outcome(self.root, record, closed_at="2026-11-10T01:00:00Z", head_commit=head,
                               terminal_gate_proven=True, terminal_gates={"inline": {"passed": True}},
                               summary=summary)
                desc = rv.describe(record)
                self.assertNotIn("CI", rv.summary_text(desc, current=False))
                self.assertNotIn("CI", rv.summary_text(desc))
                self.assertEqual(rv.bounded(desc)["codes"]["verification"]["ci"], "recorded")
                # the detail line still shows the sentence as written
                self.assertIn(("CI", summary, "info"), rv.detail_lines(desc))
        rv._OUTCOME_CACHE.clear()
        _write_outcome(self.root, record, closed_at="2026-11-10T01:00:00Z", head_commit=head,
                       terminal_gate_proven=True, terminal_gates={"inline": {"passed": True}},
                       summary="CI 77 all jobs SUCCESS for " + head + ".")
        desc = rv.describe(record)
        self.assertIn("CI same-head", rv.summary_text(desc))
        codes = rv.bounded(desc)["codes"]["verification"]
        self.assertEqual(codes["ci"], "same-head")
        self.assertEqual(codes["head"], head[:7])
        self.assertEqual(codes["head_full"], head)
        self.assertEqual(codes["ci_text_short"], "CI 77 all jobs SUCCESS for a0e73fd.")

    def test_p1_2_route_fleet_reads_as_no_response_is_not_shown_running(self):
        nodes = [{"id": "execute", "completion_gate": "code-execute"},
                 {"id": "test", "unit": "qa/test", "completion_gate": "code-test",
                  "depends_on": ["execute"], "terminal": True}]
        record = _record(self.root, "rt-stale", "staged", nodes, intensity="standard", topology="staged")
        view = {"liveness": "unknown",
                "nodes": [{"id": "execute", "state": "done", "gate_passed": True},
                          {"id": "test", "state": "reconciling", "harness": "codex"}]}
        desc = rv.describe(record, view)
        self.assertEqual(desc["next"]["kind"], "no-response")
        self.assertNotIn("running", desc["next"]["text"])
        self.assertIn("test last reconciling", desc["next"]["text"])
        self.assertNotIn("running", rv.summary_text(desc))
        self.assertEqual(rv.bounded(desc)["codes"]["next"], {"kind": "no-response", "node": "test", "harness": "codex"})
        live = rv.describe(record, dict(view, liveness="working"))
        self.assertEqual(live["next"]["text"], "wait · test (codex) reconciling")

    def test_p1_3_bounded_carries_structured_codes_beside_the_sentences(self):
        nodes = [{"id": "execute", "completion_gate": "code-execute"},
                 {"id": "impl-review", "unit": "qa/code-review", "kind": "review-worker",
                  "completion_gate": "code-impl-review", "depends_on": ["execute"]},
                 {"id": "report", "completion_gate": "code-report", "depends_on": ["impl-review"],
                  "terminal": True}]
        record = _record(self.root, "rt-codes", "staged", nodes, intensity="standard", topology="staged")
        record["selection"]["selection_basis"] = []          # no basis recorded at all
        view = {"liveness": "working", "nodes": [
            {"id": "execute", "state": "done", "gate_passed": True},
            {"id": "impl-review", "state": "active", "harness": "codex"},
            {"id": "report", "state": "pending"}]}
        job = {"attempt_id": "att-c", "liveness": "working", "harness": "claude", "route_node": "execute",
               "_context_execution_started_at": 1794277391.0, "elapsed_min": 7}
        out = rv.bounded(rv.describe(record, view, owner=rv.owner_from_job(job)))
        codes = out["codes"]
        self.assertEqual(codes["schema"], "route_visibility_codes_v1")
        self.assertEqual(codes["stages"], [["execute", "work", "done", "1"], ["impl-review", "review", "active", "0"],
                                           ["report", "work", "pending", "0"]])
        self.assertEqual(codes["basis"], {"kind": "unknown", "codes": []})
        self.assertEqual(codes["next"], {"kind": "wait", "node": "impl-review", "harness": "codex"})
        self.assertEqual(codes["owner"], {"liveness": "working", "started_at": "1794277391",
                                          "elapsed_min": "7", "harness": "claude", "attempt_id": "att-c"})
        self.assertEqual(codes["verification"]["review"], "stages")
        self.assertEqual(codes["verification"]["review_total"], "1")
        self.assertEqual(codes["verification"]["gate"], "unproven")
        self.assertNotIn("?", json.dumps(out, ensure_ascii=False))

        def leaves(value):
            if isinstance(value, dict):
                return [leaf for item in value.values() for leaf in leaves(item)]
            if isinstance(value, list):
                return [leaf for item in value for leaf in leaves(item)]
            return [value]
        self.assertTrue(all(isinstance(leaf, str) for leaf in leaves(codes)))
        from tools.fleet.tests.test_explorer_live_shape import route_extension
        self.assertTrue(route_extension("visibility", out))

    def test_contract_enums_are_closed_and_no_record_differs_from_failure(self):
        nodes = [{"id": "plan", "completion_gate": "code-plan"},
                 {"id": "report", "completion_gate": "code-report", "depends_on": ["plan"], "terminal": True}]
        record = _record(self.root, "rt-enum", "staged", nodes, intensity="standard", topology="staged")
        view = {"nodes": [{"id": "plan", "state": "someday-new-state"}, {"id": "report", "state": "pending"}]}
        codes = rv.bounded(rv.describe(record, view))["codes"]
        self.assertEqual(codes["shape"], "staged")
        self.assertEqual([row[2] for row in codes["stages"]], ["unknown", "pending"])
        self.assertEqual(codes["verification"]["gates"], [["report", "code-report", "", ""]])  # no record
        self.assertEqual(codes["verification"]["gate"], "unproven")
        record["selection"]["shape"] = "weird"
        record["execution_topology"] = "weird"
        self.assertEqual(rv.bounded(rv.describe(record, view))["codes"]["shape"], "unknown")

    def test_p2_1_why_names_the_direct_predicates_that_did_not_hold(self):
        record = _record(self.root, "rt-gap", "solo", [{"id": "one-shot", "completion_gate": "g", "terminal": True}])
        record["selection"]["selection_basis"] += [
            {"axis": "direct-predicate-gap", "signal": signal, "source": "compiler"}
            for signal in ("atomic-outcome", "focused-verification", "known-scope", "no-artifact-handoff")]
        desc = rv.describe(record)
        why = next(text for label, text, _tone in rv.detail_lines(desc) if label == "why")
        self.assertIn("not direct: atomic-outcome, focused-verification, known-scope +1", why)
        self.assertIn("not-direct:no-artifact-handoff", rv.bounded(desc)["codes"]["basis"]["codes"])
        self.assertIn("intensity-default", rv.bounded(desc)["codes"]["basis"]["codes"])

    def test_p2_2_review_independence_and_failed_terminal_gate_are_shown_as_recorded(self):
        nodes = [{"id": "impl-review", "unit": "qa/code-review", "kind": "review-worker",
                  "completion_gate": "code-impl-review"},
                 {"id": "report", "completion_gate": "code-report", "depends_on": ["impl-review"],
                  "terminal": True}]
        record = _record(self.root, "rt-ind", "staged", nodes, intensity="standard", topology="staged")
        _write_outcome(self.root, record, closed_at="2026-11-07T02:03:18Z", terminal_gate_proven=False,
                       terminal_gates={"report": {"passed": False, "reason": "completion-marker-absent"}},
                       review_independence={"impl-review": {"review_independence": "independent",
                                                            "reviewer_kind": "registered-worker"}})
        view = {"nodes": [{"id": "impl-review", "state": "done", "gate_passed": True, "harness": "codex"},
                          {"id": "report", "state": "pending"}]}
        desc = rv.describe(record, view)
        lines = rv.detail_lines(desc)
        review = next(text for label, text, _tone in lines if label == "review")
        self.assertIn("independent (registered-worker)", review)
        gate = next(text for label, text, _tone in lines if label == "gate")
        self.assertIn("code-report ✕ completion-marker-absent", gate)
        self.assertIn("gate ✕", rv.summary_text(desc))
        codes = rv.bounded(desc)["codes"]["verification"]
        self.assertEqual(codes["gates"], [["report", "code-report", "0", "completion-marker-absent"]])
        self.assertEqual(codes["gate"], "failed")
        self.assertEqual(codes["gate_reasons"], ["completion-marker-absent"])
        self.assertEqual(codes["independence"], "independent")

    def test_p3_ended_owner_heading_unfound_rv_and_date_noise(self):
        record = _record(self.root, "rt-p3", "solo", [{"id": "one-shot", "completion_gate": "g", "terminal": True}])
        job = {"attempt_id": "att-d", "liveness": "done", "harness": "claude",
               "_context_execution_started_at": 1794277391.0}
        owner_line = next(text for label, text, _tone in rv.detail_lines(
            rv.describe(record, owner=rv.owner_from_job(job))) if label == "owner")
        self.assertIn("done · started", owner_line)
        self.assertNotIn("since", owner_line)
        _write_outcome(self.root, record, closed_at="2026-11-10T01:00:00Z", terminal_gate_proven=True,
                       terminal_gates={"one-shot": {"passed": True}},
                       summary="# Wrapped foreground and retirement settlement\n"
                               "검토 반영 완료, 11/10 배포 준비.\n"
                               "Reviewer record: inputs/RV-MISSING-1110.md\n"
                               "#1403 CI42 is not green: download HTTP504.")
        desc = rv.describe(record)
        ver = desc["verification"]
        self.assertEqual(ver["result"], None)                # the first sentence already shows as review
        self.assertNotIn("Wrapped foreground", json.dumps(desc["verification"], ensure_ascii=False))
        self.assertFalse(ver["independent_review"])         # a named but missing RV is not a review
        self.assertEqual(ver["tests"], [])                   # a date is not a test count
        self.assertEqual([r["text"] for r in ver["review_notes"]], ["검토 반영 완료, 11/10 배포 준비."])
        self.assertIn("no reviewer", rv.summary_text(desc))   # a named but missing RV is no card mark
        self.assertNotIn("RV record", rv.summary_text(desc))
        self.assertEqual(rv.bounded(desc)["codes"]["verification"]["review"], "none")
        # ``#1403`` is a PR reference, not a heading: its sentence still shows
        self.assertEqual([r["text"] for r in ver["ci"]], ["#1403 CI42 is not green: download HTTP504."])

    def test_module_never_writes(self):
        source = Path(rv.__file__).read_text(encoding="utf-8")
        for pattern in ('open(path, "w', ".write(", "os.replace(", "write_text(", ".unlink("):
            self.assertNotIn(pattern, source)


class RouteVisibilityLiveTests(unittest.TestCase):
    def test_fleet_route_row_carries_the_solo_owner_from_its_node_attempt(self):
        from tools.fleet.explorer.live import build_view_model
        from tools.fleet.model import DispatchJob, Session, WorkProjection
        record = _record("/nonexistent-root", "rt-live", "solo", [
            {"id": "one-shot", "completion_gate": "quick-complete", "terminal": True}],
            owner_attempt_id="att-live")
        view = {"nodes": [{"id": "one-shot", "state": "active", "harness": "claude"}], "liveness": "working"}
        work = WorkProjection(source="route-exact", route_id="rt-live",
                              _route_view={"record": record, "view": view})
        session = Session(harness="claude", pid=os.getpid(), session_id="sid-main", session_tag="s1",
                          cwd="/fixture", liveness="idle")
        job = DispatchJob(key="owner", slug="rt-live", harness="claude", depth=1, route_id="rt-live",
                          route_node="one-shot", attempt_id="att-live", parent_sid="sid-main",
                          liveness="working", elapsed_min=5, work_projection=work)
        vm = build_view_model({"sessions": [session], "jobs": [job], "resources": []}, now=2000.0).to_mapping()
        route = next(r for r in vm["routes"] if r["id"] == "rt-live")
        visibility = route["visibility"]
        self.assertEqual(visibility["shape"], "solo")
        owner = next(text for label, text, _tone in visibility["lines"] if label == "owner")
        self.assertIn("att-live", owner)
        self.assertIn("working", owner)


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
