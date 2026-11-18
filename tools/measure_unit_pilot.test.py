import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "measure-unit-pilot.py"
spec = importlib.util.spec_from_file_location("measure_unit_pilot", SCRIPT)
M = importlib.util.module_from_spec(spec)
sys.modules["measure_unit_pilot"] = M
spec.loader.exec_module(M)

REVIEW = ("## 📋 Code Review Results\n\n### 🔴 Must-fix issues\n\n"
          "- a.py:12 fails when the list is empty; fix by guarding it\n"
          "- the check `pytest x` could not run here, so this is blocked\n\n"
          "### 🟡 Suggested improvements\n\n- naming\n\nhanded-over: `pytest x` (reason: no network)\n")
HEADING_ITEMS = ("## 🔴 Must-fix issues\n\n### a.py:3 — crash\n- Why it matters: x\n\n### b.py:7 — leak\n- Why: y\n\n"
                 "## 🟡 Suggested\n\n- naming\n")
CLOSED = "### Prior\n\n- r1 🔴 1: closed (evidence)\n\n### 🔴 Must-fix issues\n\n- new.py:3 breaks when x is None\n"


def row(when, unit, note, route="rt-1", node="impl-review", attempt="att-1", log="", depth="2"):
    meta = (f"unit={unit},route_id={route},route_node={node},note={note},attempt_id={attempt},"
            f"dispatch_depth={depth},log_file={log}")
    return f"{when}\tdone\t/w\t/w\tname\t{meta}\n"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def report(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def log(self, name, report):
        path = self.root / name
        path.write_text(f'{{"result":"artifact: {report}\\nverdict: FAIL\\nblocker: x"}}\n', encoding="utf-8")
        return path


class ReportReadingTest(unittest.TestCase):
    def test_red_items_place_cue_unrun_and_handed_over(self):
        out = M.analyse_report(REVIEW, 0, True)
        self.assertEqual((out["red"], out["place"], out["unrun"], out["handed"]), (2, 1, 1, 1))
        self.assertGreaterEqual(out["repro"], 1)

    def test_a_passing_report_has_no_red_items(self):
        self.assertEqual(M.analyse_report(REVIEW, 0, False)["red"], 0)

    def test_closed_priors_and_a_new_finding_is_counted_only_from_round_two(self):
        self.assertTrue(M.analyse_report(CLOSED, 1, True)["closed_fail"])
        self.assertFalse(M.analyse_report(CLOSED, 0, True)["closed_fail"])
        self.assertFalse(M.analyse_report(CLOSED.replace("### 🔴", "- r1 🔴 2: open\n\n### 🔴"), 1, True)["closed_fail"])


    def test_heading_items_are_counted_and_a_shallower_heading_ends_the_section(self):
        out = M.analyse_report(HEADING_ITEMS, 0, True)
        self.assertEqual((out["red"], out["place"]), (2, 2))
        deeper = "### 🔴 Must-fix\n\n#### a.py:3 — crash\n- Why\n\n#### b.py:7 — leak\n- Why\n\n## 📋 Other\n\n- not red\n"
        self.assertEqual(M.analyse_report(deeper, 0, True)["red"], 2)

    def test_status_words_count_only_next_to_an_earlier_item_id(self):
        base = "- r1 🔴 1: closed (evidence)\n\n### 🔴 Must-fix issues\n\n- new.py:3 breaks when x is None\n"
        self.assertTrue(M.analyse_report(base + "\nNo prior item remains open.\n", 1, True)["closed_fail"])
        unresolved = "- r1 🔴 1: closed\n- r1 🔴 2: 해소되지 않음\n\n### 🔴 Must-fix issues\n\n- new.py:3 breaks when x is None\n"
        self.assertFalse(M.analyse_report(unresolved, 1, True)["closed_fail"])
        self.assertTrue(M.analyse_report(unresolved.replace("해소되지 않음", "해소됨"), 1, True)["closed_fail"])


class MeasureTest(Fixture):
    def test_roundtrips_leave_plan_review_out_count_stage_fail_and_split_by_unit(self):
        lines = [
            row("2026-11-01T00:00:00Z", "qa/plan-review", "completed-review-blocking", attempt="p1"),
            row("2026-11-01T01:00:00Z", "qa/code-review", "completed-review-blocking", attempt="c1"),
            row("2026-11-01T02:00:00Z", "qa/test", "dead-worker-fail", attempt="t1").replace("note=", "failure_class=fail,note="),
            row("2026-11-01T03:00:00Z", "qa/test", "dead-worker-blocked", attempt="t2").replace("note=", "failure_class=blocked,note="),
            row("2026-11-01T04:00:00Z", "dev/backend", "completed-marker", attempt="b1"),
        ]
        (self.root / "jobs.log").write_text("".join(lines), encoding="utf-8")
        rows = M.read_rows(self.root / "jobs.log")
        result = M.measure(rows, self.root, M.parse_ts("2026-11-05T00:00:00Z"), "before", {})
        self.assertEqual(result["dev/backend"]["roundtrips_median"], 2)
        self.assertEqual(result["dev/backend"]["roundtrips_by_unit"], {"qa/code-review": 1, "qa/test": 1})

    def test_fail_and_blocked_reports_are_read_and_unmeasurable_items_are_none(self):
        stage = "verdict: FAIL\n\n- the check could not run\nhanded-over: `pytest x` (reason: no network)\n"
        lines = [row("2026-11-01T00:00:00Z", "qa/test", "dead-worker-fail", attempt="t1", log=str(self.log("l1", self.report("r1.md", stage)))),
                 row("2026-11-01T01:00:00Z", "qa/test", "dead-worker-blocked", attempt="t2", log=str(self.log("l2", self.report("r2.md", stage))))]
        (self.root / "jobs.log").write_text("".join(l.replace("note=", "failure_class=fail,note=") for l in lines), encoding="utf-8")
        rows = M.read_rows(self.root / "jobs.log")
        test = M.measure(rows, self.root, M.parse_ts("2026-11-05T00:00:00Z"), "before", {})["qa/test"]
        self.assertEqual((test["reports_read"], test["handed_over_lines"]), (2, 2))
        self.assertIsNone(test["red_items"])
        self.assertIsNone(test["unrun_as_red"])
        self.assertTrue(test["notes"])


    def test_windows_roundtrips_invalid_envelope_and_missing_reports(self):
        rep = self.report("r1.md", REVIEW)
        lines = [
            row("2026-11-01T00:00:00Z", "qa/code-review", "completed-review-blocking", attempt="a1", log=str(self.log("l1", rep))),
            row("2026-11-02T00:00:00Z", "qa/code-review", "completed-marker", attempt="a2", log=str(self.root / "none")),
            row("2026-11-03T00:00:00Z", "qa/code-review", "dead-invalid-envelope", route="rt-2", attempt="a3"),
            row("2026-10-01T00:00:00Z", "qa/code-review", "completed-marker", route="rt-9", attempt="a9"),  # outside 14 days
            row("2026-11-04T00:00:00Z", "dev/backend", "completed-marker", route="rt-1", attempt="b1"),
        ]
        (self.root / "jobs.log").write_text("".join(lines), encoding="utf-8")
        rows = M.read_rows(self.root / "jobs.log")
        cut = M.parse_ts("2026-11-05T00:00:00Z")
        result = M.measure(rows, self.root, cut, "before", {"qa/code-review": 123})
        review = result["qa/code-review"]
        self.assertEqual((review["attempts"], review["routes"], review["invalid_envelope"]), (3, 2, 1))
        self.assertEqual((review["roundtrips_median"], review["roundtrips_p90"]), (0.5, 1))
        self.assertEqual((review["red_items"], review["red_with_place"], review["unrun_as_red"], review["handed_over_lines"]),
                         (2, 1, 1, 1))
        self.assertEqual((review["reports_read"], review["reports_missing"], review["render_bytes"]), (1, 2, 123))
        self.assertIsNone(result["dev/backend"]["red_items"])
        self.assertEqual(result["qa/test"]["attempts"], 0)

    def test_after_takes_the_earliest_attempts_and_the_cap_applies(self):
        (self.root / "jobs.log").write_text("".join(
            row(f"2026-11-0{n}T00:00:00Z", "qa/test", "completed-marker", route=f"rt-{n}", attempt=f"t{n}")
            for n in range(1, 6)), encoding="utf-8")
        rows = M.read_rows(self.root / "jobs.log")
        cut = M.parse_ts("2026-11-02T00:00:00Z")
        self.assertEqual(M.measure(rows, self.root, cut, "after", {}, 2)["qa/test"]["attempts"], 2)
        self.assertEqual(M.measure(rows, self.root, cut, "after", {})["qa/test"]["attempts"], 4)


# A line the form review marked as exposed when moved below must stay in the musts section.
MUSTS = {
    "qa/code-review": ("Read-only", "spec/pipeline_state.yaml", "Round 1 — front-load", "Round ≥ 2 — closure check",
                       "Never fail a round on a", "Verdict tokens:", "they are not caps", "Unchanged code is NOT a review target",
                       "A PASS remains attached to the content it reviewed", "reusing command evidence never substitutes"),
    "qa/test": ("stop-on-failure", "Handed-over checks", "Never remove or relocate an existing reviewer", "runtime observation",
                "tools/figure-semantic-verify.py", "failure, not a skip", "hangs beyond ~60 s", "Verdict tokens:"),
    "dev/backend": ("DB migrations, core auth logic, and deployment infrastructure require explicit", "never silently diverge from spec",
                    "search every caller", "Preserve inputs and outputs"),
    "editorial/report": ("Carry the source description and distinguishing comparison condition", "No new QA", "Remaining risk is part of the report",
                         "Write the report to the assigned artifact path"),
}


class TwoLayerRenderTest(unittest.TestCase):
    """What the runtime carries to the worker: both headings once, musts first, the musts intact, the end-of-work form unchanged."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "utilities"))
        import worker_bootstrap
        cls.wb = worker_bootstrap

    def render(self, unit):
        return self.wb.render_worker_bootstrap(ROOT, M.WORKER_TYPE[unit], unit=unit)

    def test_both_headings_once_and_the_musts_come_first(self):
        for unit in M.UNITS:
            with self.subTest(unit=unit):
                text = self.render(unit)
                self.assertEqual((text.count("\n## 지킬 것\n"), text.count("\n## 참고\n")), (1, 1))
                self.assertLess(text.index("\n## 지킬 것\n"), text.index("\n## 참고\n"))

    def test_the_exposed_lines_sit_in_the_musts_section(self):
        for unit, anchors in MUSTS.items():
            with self.subTest(unit=unit):
                text = self.render(unit)
                musts = text[text.index("\n## 지킬 것\n"):text.index("\n## 참고\n")]
                for anchor in anchors:
                    self.assertIn(anchor, musts, anchor)

    def test_review_units_carry_the_stance_once_and_the_closing_form_is_untouched(self):
        for unit in M.UNITS:
            with self.subTest(unit=unit):
                text = self.render(unit)
                self.assertEqual(text.count("Refute first"), 1 if unit in M.REVIEW_UNITS else 0)
                self.assertIn("artifact: <canonical path | ->\nverdict: PASS | FAIL | BLOCKED\nblocker: none | <one line>", text)

    def test_a_unit_that_is_not_in_the_pilot_keeps_its_single_body(self):
        text = self.wb.render_worker_bootstrap(ROOT, "stage", unit="dev/frontend")
        self.assertNotIn("\n## 지킬 것\n", text)


class RenderAndCliTest(Fixture):
    def test_render_bytes_cover_the_four_units_and_a_ref_renders_the_same(self):
        here = M.render_bytes(ROOT)
        self.assertEqual(set(here), set(M.UNITS) | {"qa/test@stage"})
        self.assertTrue(all(isinstance(v, int) and v > 0 for v in here.values()))

    def test_cli_prints_the_table_and_json_for_an_empty_registry(self):
        (self.root / "jobs.log").write_text("", encoding="utf-8")
        base = [sys.executable, str(SCRIPT), "--state-root", str(self.root), "--before", "2026-11-05T00:00:00Z"]
        table = subprocess.run(base, capture_output=True, text=True, check=True).stdout
        self.assertIn("roundtrips_median", table)
        data = json.loads(subprocess.run([*base, "--json"], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(set(data), set(M.UNITS))
        self.assertEqual(data["dev/backend"]["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
