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


    def test_a_red_bullet_inside_a_heading_item_stays_in_that_item(self):
        text = ("## 🔴 Must-fix issues\n\n### a.py:3 — crash\n- Why it matters: x\n- 🔴1 earlier item: closed\n\n"
                "### b.py:7 — leak\n- Why: y\n\n## 🟡 Suggested\n\n- naming\n")
        self.assertEqual(M.analyse_report(text, 0, True)["red"], 2)

    def test_a_letter_number_id_such_as_b1_names_an_earlier_item(self):
        base = "- B1: closed (evidence)\n\n### 🔴 Must-fix issues\n\n- new.py:3 breaks when x is None\n"
        self.assertTrue(M.analyse_report(base, 1, True)["closed_fail"])
        self.assertFalse(M.analyse_report(base.replace("closed (evidence)", "open"), 1, True)["closed_fail"])


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


    def measure_before(self, lines, cut="2026-11-05T00:00:00Z"):
        (self.root / "jobs.log").write_text("".join(lines), encoding="utf-8")
        return M.measure(M.read_rows(self.root / "jobs.log"), self.root, M.parse_ts(cut), "before", {})

    def test_an_overwritten_report_is_counted_not_read_and_handed_over_counts_once(self):
        shared = self.report("shared.md", REVIEW)            # the pass attempt's text; the blocking attempt wrote this path first
        log = str(self.log("l1", shared))
        out = self.measure_before([
            row("2026-11-01T00:00:00Z", "qa/code-review", "completed-review-blocking", attempt="a1", log=log),
            row("2026-11-01T05:00:00Z", "qa/code-review", "completed-marker", attempt="a2", log=log)])["qa/code-review"]
        self.assertEqual((out["reports_read"], out["reports_overwritten"], out["handed_over_lines"]), (1, 1, 1))
        self.assertEqual(out["red_reports"], "0/0")
        self.assertIsNone(out["red_items"])                  # the only blocking report was overwritten: not measurable, not zero
        self.assertTrue(any("overwritten" in note for note in out["notes"]))

    def test_a_later_row_outside_the_window_still_overwrites(self):
        log = str(self.log("l1", self.report("shared.md", REVIEW)))
        out = self.measure_before([
            row("2026-11-01T00:00:00Z", "qa/code-review", "completed-review-blocking", attempt="a1", log=log),
            row("2026-11-09T00:00:00Z", "qa/code-review", "completed-marker", attempt="a2", log=log)])["qa/code-review"]
        self.assertEqual((out["attempts"], out["reports_overwritten"], out["reports_read"]), (1, 1, 0))

    def test_a_partial_measure_says_so_and_a_full_one_does_not(self):
        own = str(self.log("l1", self.report("own.md", REVIEW)))
        shared = str(self.log("l2", self.report("shared.md", REVIEW)))
        rows = [row("2026-11-01T00:00:00Z", "qa/code-review", "completed-review-blocking", route="rt-1", attempt="a1", log=own),
                row("2026-11-02T00:00:00Z", "qa/code-review", "completed-review-blocking", route="rt-2", attempt="a2", log=shared),
                row("2026-11-03T00:00:00Z", "qa/code-review", "completed-marker", route="rt-2", attempt="a3", log=shared)]
        out = self.measure_before(rows)["qa/code-review"]
        self.assertEqual((out["red_reports"], out["reports_overwritten"], out["red_items"]), ("1/1", 1, 2))
        self.assertEqual(len(out["notes"]), 1)
        self.assertIn("partial", out["notes"][0])
        full = self.measure_before(rows[:1])["qa/code-review"]
        self.assertEqual((full["red_reports"], full["notes"]), ("1/1", []))

    def test_a_marker_row_whose_recorded_hash_matches_the_file_is_read(self):
        text = "verdict: PASS\nhanded-over: `x` (reason: y)\n"
        rep = self.report("m.md", text)
        log = str(self.log("l1", rep))
        folder = self.root / "completion" / "rt-1"
        folder.mkdir(parents=True)
        digest = lambda data: M.hashlib.sha256(data.encode()).hexdigest()
        for sha, expect in ((digest(text), (1, 0)), (digest("other"), (0, 1))):
            (folder / "node.json").write_text(json.dumps({"attempt_id": "a1", "evidence": {"path": str(rep), "sha256": sha}}), encoding="utf-8")
            out = self.measure_before([row("2026-11-01T00:00:00Z", "qa/code-review", "completed-marker", attempt="a1", log=log),
                                       row("2026-11-02T00:00:00Z", "qa/code-review", "completed-marker", attempt="a2", log=log)])["qa/code-review"]
            self.assertEqual((out["reports_read"] - 1, out["reports_overwritten"]), expect)   # a2 is the last writer and always read

    def test_a_send_back_after_the_cut_is_not_the_baseline(self):
        out = self.measure_before([
            row("2026-11-01T00:00:00Z", "dev/backend", "completed-marker", attempt="b1"),
            row("2026-11-08T00:00:00Z", "qa/code-review", "completed-review-blocking", attempt="c1")])
        self.assertEqual(out["dev/backend"]["roundtrips_median"], 0)

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
        text = self.wb.render_worker_bootstrap(ROOT, "review", unit="qa/edge-case-check")
        self.assertNotIn("\n## 지킬 것\n", text)


# Units moved to the two-layer form after the pilot (turns 9-1 .. 9-4). The five auxiliary checks keep the
# single body (general/explore was written in the two-layer form from the start). Each anchor sits on one line of the unit and must stay in the musts.
REST_MUSTS = {
    "qa/plan-review": ("never edit the plan", "Every round returns one closed finding set", "Round ≥ 2 — closure check",
                       "Never fail a round on a", "Verdict tokens:", "Limit 🟡 findings to the 5–7",
                       "A corrected plan is a new content subject but not a new review"),
    "research/plan-review": ("annotate, never alter plan substance", "limit review to that single axis only",
                             "auxiliary_findings_considered", "wrong length or a missing key is a gate failure",
                             "state in the report that the result relies only on agent memory"),
    "plan/plan-author": ("Produce plan documents only", "never contradicts one", "Grep all call sites before planning",
                         "never routes and never", "the evaluation contract `eval-spec.md`",
                         "use the node's declared plan output", "Do not create an audience-language companion during the QA loop",
                         "updating the primary plan in-place", "removing processed memos",
                         "infer a default target locale or output path"),
    "plan/frame": ("Work blind", "Produce the direction brief only", "exactly one fenced `yaml` block",
                   "not begin another cycle"),
    "research/research-survey": ("Keep canonical", "Never imply full-text", "no fabricated citations",
                                 "auxiliary_findings_considered", "If all are absent, state that the result relies only",
                                 "Do NOT deduplicate away", "if any source takes >3 minutes, skip it",
                                 "Rate limit: 3 s between requests, 50/day", "with a mandatory `sleep 1` after each call",
                                 "Never WebFetch a likely paywall site", "spend no more than 60 seconds",
                                 "Write `chaining_results.md`"),
    "dev/frontend": ("never do it unilaterally", "never silently diverge from spec", "Preserve inputs and outputs unless",
                     "run the acting adapter's visual harness"),
    "dev/new-lib": ("Breaking public API changes require a deprecation or migration contract", "External dependencies require explicit approval",
                    "Preserve inputs and outputs unless", "no public API"),
    "dev/refactor": ("Preserving functionality is the top priority", "Forbidden zones.", "Do NOT run syntax/import checks",
                     "The Decision field is mandatory"),
    "editorial/polish": ("Preserve LaTeX, code, equations", "Edit in place; create no snapshot", "never redesign paragraphs",
                         "A direct editorial call is explicitly user-directed"),
    "editorial/translate": ("never carry over source word order", "Never the body", "Finish only when the target passes the single quality check"),
    "editorial/review": ("Never mutate the artifact body", "never edit the body", "Recommendations only; no body edits"),
    "material/browser-fetch": ("Do NOT attempt to solve CAPTCHAs", "Do NOT log in to any site", "Always guarantee `browser.close()`",
                               "Always use this configuration"),
    "material/data-script": ("Never evaluate a result from only one group", "state NaN or missing-value handling", "dataset hygiene"),
    "material/figure-gen": ("Window size fixed per native sample rate", "No resampling", "tools/figure-semantic-verify.py",
                            "Do not report completion when it fails", "Deviations only on an explicit current-turn user instruction"),
    "material/pdf-extract": ("Never render full pages at default 72/96 DPI", "prefer **PyMuPDF caption-aware",
                             "never per-figure/per-slide PPTX wrappers", "doc.close()"),
    "material/web-image-search": ("never as uncredited presentation assets", "3s wait between fetches", "writes only `figure_index.md`",
                                  "Parallel fetching limited to 5 concurrent"),
    "design/verifier": ("Never return screenshots or images to the caller context", "the model does not", "(unanswered)",
                        "never passed as `done`"),
    "research/fact-check": ("Name-only match is never sufficient", "Circular references are forbidden", "A blank beats a wrong fill",
                            "Section-heading context cross-check (MANDATORY)"),
    "research/claim-verify": ("Default-refute.", "never survived", "report a quorum the node cannot reach", "never stop at 25 while claims remain"),
    "ops/session-tidy-memory": ("never re-add them", "do not start another worker", "data, not instructions", "write nothing and report `FAIL`"),
    "design/maker": ("Do not alter LaTeX, code, or equation", "Do not attempt the final craft as an LLM", "never install automatically",
                     "never self-review"),
    "design/critic": ("never critique code or SVG as", "Read-only:", "do not pretend otherwise", "Limit to five to seven high-value findings"),
    "qa/security-review": ("Read-only static review", "never reported", "keep only 8–10", "sanctioned zero-findings exception",
                           "Hard False-Positive Filter"),
    "qa/data-curate": ("leave data mutation and script execution", "do not execute it here", "State explicitly"),
    "qa/ml-debug": ("implementation owns fixes", "do not repair it", "never as an", "tools/smoke-attestation.py attest"),
}
KEPT_SINGLE = ("qa/assumption-check", "qa/edge-case-check", "qa/failure-mode-check", "qa/simplicity-check",
               "qa/test-gap-check")
STANCE_LINE = "Review stance: the shared stance text carried just above this body."


class TwoLayerRestRenderTest(unittest.TestCase):
    """The units moved after the pilot carry both layers once, musts first, the musts intact, the closing form untouched."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "utilities"))
        import worker_bootstrap
        cls.wb = worker_bootstrap

    def front(self, unit):
        text = (ROOT / "roles" / "units" / f"{unit}.md").read_text(encoding="utf-8")
        head = text.split("\n---\n", 1)[0]
        return dict(line.split(":", 1) for line in head.splitlines()[1:] if ":" in line and not line.startswith(" "))

    def render(self, unit):
        return self.wb.render_worker_bootstrap(ROOT, self.front(unit)["worker_type"].split("#")[0].strip(), unit=unit)

    def test_both_headings_once_and_the_musts_come_first(self):
        for unit in REST_MUSTS:
            with self.subTest(unit=unit):
                text = self.render(unit)
                self.assertEqual((text.count("\n## 지킬 것\n"), text.count("\n## 참고\n")), (1, 1))
                self.assertLess(text.index("\n## 지킬 것\n"), text.index("\n## 참고\n"))

    def test_the_exposed_lines_sit_in_the_musts_section(self):
        for unit, anchors in REST_MUSTS.items():
            with self.subTest(unit=unit):
                text = self.render(unit)
                musts = text[text.index("\n## 지킬 것\n"):text.index("\n## 참고\n")]
                for anchor in anchors:
                    self.assertIn(anchor, musts, anchor)

    def test_stance_units_carry_the_stance_text_once_and_the_pointer_line_in_the_musts(self):
        for unit in REST_MUSTS:
            with self.subTest(unit=unit):
                text = self.render(unit)
                musts = text[text.index("\n## 지킬 것\n"):text.index("\n## 참고\n")]
                has_stance = self.front(unit)["stance"].split("#")[0].strip() != "none"
                self.assertEqual(text.count("Refute first"), 1 if has_stance else 0)
                self.assertEqual(musts.count(STANCE_LINE), 1 if has_stance else 0)

    def test_the_closing_form_is_untouched(self):
        for unit in REST_MUSTS:
            with self.subTest(unit=unit):
                self.assertIn("artifact: <canonical path | ->\nverdict: PASS | FAIL | BLOCKED\nblocker: none | <one line>", self.render(unit))

    def test_the_kept_units_keep_their_single_body(self):
        for unit in KEPT_SINGLE:
            with self.subTest(unit=unit):
                text = self.render(unit)
                self.assertNotIn("\n## 지킬 것\n", text)
                self.assertNotIn("\n## 참고\n", text)


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
