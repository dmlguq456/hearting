#!/usr/bin/env python3
"""Hermetic unit tests — F-30 process view (prd.md:304-310).

Two harnesses (plan §5.6 Y2):
  - render content (T3-1..T3-7, T3-12..T3-14): `render._build_lines(...)` directly.
  - mouse/maps (T3-8..T3-11): `_draw` against a FakeScreen with `curses.doupdate` patched out
    (test_f27_mouse.py:20-37 precedent) — `_build_lines` alone never populates
    `_FOLD_ROWS`/`_CLICK_ROWS`/`_TOGGLE_ROWS`.
"""
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import projection, render         # noqa: E402
from fleet import route                     # noqa: E402
from fleet import demo                      # noqa: E402
from fleet.collectors import dispatch       # noqa: E402
from fleet.model import DispatchJob, Session, SubAgent  # noqa: E402

_FIXDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "route")
_REAL_CLAUDE = os.path.join(_FIXDIR, "real_claude_staged.json")
_REAL_RID = "rt-9fa0fed86699b8f5"
_LAB = os.path.join(_FIXDIR, "synth_parallel_lab.json")
_LAB_RID = "rt-6f5423d05eaf3189"
_COMPOSED = os.path.join(_FIXDIR, "synth_composed_survey.json")


def _joined(lines):
    return "\n".join("".join(t for t, _k in ln) for ln in lines if ln)


class FakeScreen:
    """test_f27_mouse.py:20-37 precedent — the minimal stdscr surface `_draw` touches."""

    def __init__(self, h=40, w=168):
        self.h, self.w = h, w

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        pass

    def addstr(self, row, col, text, attr=0):
        pass

    def chgat(self, y, x, n, attr):
        pass

    def noutrefresh(self):
        pass


class ProcessViewEnv(unittest.TestCase):
    def setUp(self):
        route.clear_cache()
        self._saved_evidence = dispatch.collect.last_route_nodes
        dispatch.collect.last_route_nodes = {}
        render.set_process_view(False)
        render.reset_selection()
        render._ROUTE_FOLD = {}
        render._FOLDABLE = []
        render._TOGGLE_ROWS = {}
        render._CLICK_ROWS = {}
        render._FOLD_ROWS = {}
        render._PROMPT_HITS = []
        render._OFFSET = 0
        render.set_show_all(False)

    def tearDown(self):
        dispatch.collect.last_route_nodes = self._saved_evidence
        route.clear_cache()
        render.set_process_view(False)
        render.reset_selection()
        render._ROUTE_FOLD = {}
        render._FOLDABLE = []
        render._TOGGLE_ROWS = {}
        render._CLICK_ROWS = {}
        render._FOLD_ROWS = {}
        render.set_show_all(False)

    def _lab_route_jobs(self, sep_liveness="stale", asr_liveness="working"):
        conductor = DispatchJob(key="lab", slug="lab-conductor", cwd="/x", depth=1,
                                liveness="working")
        asr = DispatchJob(key="lab-eval", slug="lab-conductor-eval-asr", cwd="/x",
                          parent_slug="lab-conductor", depth=2, liveness=asr_liveness,
                          route_id=_LAB_RID, route_file=_LAB, route_node="eval-asr",
                          model="sonnet", effort="medium", elapsed_min=9)
        sep = DispatchJob(key="lab-eval", slug="lab-conductor-eval-sep", cwd="/x",
                          parent_slug="lab-conductor", depth=2, liveness=sep_liveness,
                          route_id=_LAB_RID, route_file=_LAB, route_node="eval-sep",
                          elapsed_min=3)
        return [conductor, asr, sep]


class RenderContentTest(ProcessViewEnv):
    @staticmethod
    def _retry_view(review_history=None):
        record = {
            "route_id": "rt-rounds",
            "route_hash": "sha256:rounds",
            "capability": "autopilot-code",
            "nodes": [
                {"id": "plan", "depends_on": []},
                {"id": "plan-check", "depends_on": ["plan"]},
                {"id": "execute", "depends_on": ["plan-check"]},
                {"id": "impl-review", "depends_on": ["execute"]},
            ],
        }
        execute = DispatchJob(
            key="code-execute", slug="rounds-execute-r2", cwd="/x", depth=2,
            liveness="working", route_id="rt-rounds", route_node="execute",
        )
        evidence = {
            "plan": {"attempt_history": [
                {"attempt_id": "att-plan-%d" % index,
                 "contract_status": "current", "status": "done"}
                for index in range(1, 5)
            ]},
            "plan-check": {"attempt_history": [
                {"attempt_id": "att-plan-check-1", "contract_status": "current", "status": "done"},
                {"attempt_id": "att-plan-check-2", "contract_status": "current", "status": "running"},
            ]},
            "execute": {"attempt_history": [
                {"attempt_id": "att-execute-1", "contract_status": "current", "status": "done"},
                {"attempt_id": "att-execute-1", "contract_status": "current", "status": "done"},
                {"attempt_id": "att-execute-2", "contract_status": "current", "status": "running"},
            ]},
            "impl-review": {
                "status": "done", "ts": None,
                "attempt_history": review_history or [
                    {"attempt_id": "att-review-1", "contract_status": "current", "status": "done"},
                ],
            },
        }
        return route._record_view(record, "rt-rounds", [execute], evidence, 0.0)

    def test_retry_rounds_are_independent_and_keep_one_fixed_dag_line(self):
        view = self._retry_view()
        rows = render._route_card_l2(view, max_width=160)
        self.assertEqual(len(rows), 1)
        text = _joined(rows)
        self.assertIn("plan(R4)", text)
        self.assertIn("plan-check(R2)", text)
        self.assertIn("execute(R2)", text)
        self.assertIn("impl-review(R1)", text)
        self.assertEqual(text.count("execute(R2)"), 1)
        self.assertEqual(text.count("impl-review(R1)"), 1)
        self.assertEqual(text.count("plan(R4)"), 1)
        self.assertEqual(text.count("plan-check(R2)"), 1)

        projection_holder = SimpleNamespace(
            work_projection=SimpleNamespace(
                source="route-exact", _route_view={"view": view}))
        breadcrumb = "".join(
            text for text, _key in render._route_stage_segs(
                render._projection_route_seq(projection_holder), True, 160))
        self.assertIn("execute(R2)", breadcrumb)
        self.assertIn("impl-review(R1)", breadcrumb)
        self.assertIn("plan(R4)", breadcrumb)
        self.assertIn("plan-check(R2)", breadcrumb)

    def test_retry_round_omits_unverified_evidence_and_advances_on_actual_start(self):
        unverified = self._retry_view(review_history=[
            {"attempt_id": "att-review-1", "contract_status": "legacy-read-only", "status": "done"},
            {"attempt_id": None, "contract_status": "current", "status": "done"},
        ])
        self.assertNotIn("impl-review(R", _joined(render._route_card_l2(unverified, 160)))

        second_started = self._retry_view(review_history=[
            {"attempt_id": "att-review-1", "contract_status": "current", "status": "done"},
            {"attempt_id": "att-review-2", "contract_status": "current", "status": "running"},
        ])
        text = _joined(render._route_card_l2(second_started, 160))
        self.assertIn("execute(R2)", text)
        self.assertIn("impl-review(R2)", text)

        payload = route.summary([second_started])[0]
        rounds = {node["id"]: node.get("attempt_round") for node in payload["nodes"]}
        self.assertEqual(rounds, {"plan": 4, "plan-check": 2,
                                  "execute": 2, "impl-review": 2})

    def test_retry_round_waits_for_start_evidence(self):
        registered_only = self._retry_view(review_history=[
            {"attempt_id": "att-review-1", "contract_status": "current", "status": "done"},
            {"attempt_id": "att-review-2", "contract_status": "current", "status": "open"},
        ])
        self.assertIn("impl-review(R1)", _joined(render._route_card_l2(registered_only, 160)))

        spawned = self._retry_view(review_history=[
            {"attempt_id": "att-review-1", "contract_status": "current", "status": "done"},
            {"attempt_id": "att-review-2", "contract_status": "current", "status": "open", "pid": 42},
        ])
        self.assertIn("impl-review(R2)", _joined(render._route_card_l2(spawned, 160)))

    def test_parallel_review_keeps_round_and_width_in_one_marker(self):
        view = self._retry_view(review_history=[
            {"attempt_id": "att-review-1", "contract_status": "current", "status": "done"},
            {"attempt_id": "att-review-2", "contract_status": "current", "status": "running"},
        ])
        anchor = next(node for node in view["nodes"] if node["id"] == "impl-review")
        anchor["parallel_group"] = "impl-review"
        alternative = dict(anchor)
        alternative.update({
            "id": "impl-review-alternative",
            "parallel_group": "impl-review",
            "attempt_round": None,
        })
        view["nodes"].append(alternative)

        collapsed = {"nodes": render._collapse_parallel_nodes(view["nodes"])}
        text = _joined(render._route_card_l2(collapsed, 160))
        self.assertIn("impl-review(R2·2-way)", text)
        self.assertNotIn("impl-review(2-way)(R2)", text)

    def test_t3_1_process_view_off_matches_group_view(self):
        job = DispatchJob(key="code", slug="plain-job", cwd="/x", liveness="working", depth=1)
        render.set_process_view(False)
        lines = render._build_lines([], [job], section="dispatch", narrow=False,
                                    malformed=0, layout="wide")
        text = _joined(lines)
        self.assertNotIn("PROCESS VIEW", text)
        # F-42a: record-less capability identity is not a concrete route. The group view
        # renders one preparation state and does not preview `_PIPE_STAGES`.
        self.assertIn("preparing…", text)
        self.assertNotIn("plan › exec › test", text)

    def test_t3_2_real_record_card_shows_short_route_id_and_progress(self):
        dispatch.collect.last_route_nodes = {
            _REAL_RID: {"plan": {"status": "done", "slug": "x", "ts": None, "note": None}},
        }
        conductor = DispatchJob(key="code", slug="v10-conductor", cwd="/x", depth=1,
                                liveness="working")
        child = DispatchJob(key="code-execute", slug="v10-conductor-execute", cwd="/x",
                            parent_slug="v10-conductor", depth=2, liveness="working",
                            route_id=_REAL_RID, route_file=_REAL_CLAUDE, route_node="execute")
        render.set_process_view(True)
        lines = render._build_lines([], [conductor, child], section="both", narrow=False,
                                    malformed=0, layout="wide", term_width=168)
        text = _joined(lines)
        self.assertIn("rt-9fa0fed8", text)
        self.assertIn("1/4 nodes", text)

    def test_t3_3_parallel_nodes_render_as_indented_siblings(self):
        jobs = self._lab_route_jobs()
        render.set_process_view(True)
        lines = render._build_lines([], jobs, section="both", narrow=False, malformed=0,
                                    layout="wide", term_width=168)
        text = _joined(lines)
        for nid in ("eval-asr", "eval-sep", "eval-vad"):
            self.assertIn(nid, text)
        self.assertIn("├", text)
        self.assertIn("└", text)

    def test_t3_3b_composed_survey_process_card_preserves_fanin_metadata(self):
        record = route.load(_COMPOSED)
        rid = record["route_id"]
        owner = DispatchJob(key="survey", slug="composed-owner", cwd="/composed",
                            depth=1, liveness="working")
        child_a = DispatchJob(
            key="claim", slug="claim-a", parent_slug="composed-owner", depth=2,
            cwd="/composed/a", liveness="working", route_id=rid,
            route_file=_COMPOSED, route_node="claim-a", assigned_contract="autopilot-code")
        child_b = DispatchJob(
            key="claim", slug="claim-b", parent_slug="composed-owner", depth=2,
            cwd="/composed/b", liveness="working", route_id=rid,
            route_file=_COMPOSED, route_node="claim-b", assigned_contract="autopilot-code")
        # Feed the process renderer a projection-attached reversed collector order.
        projection.attach_projections([], [owner, child_b, child_a], now=100.0)
        render.set_process_view(True)
        text = _joined(render._build_lines([], [owner, child_b, child_a], section="both",
                                            narrow=False, malformed=0, layout="wide",
                                            term_width=168))
        self.assertIn("survey[research/research-survey]", text)
        self.assertIn("claim-a[research/claim-verify]", text)
        self.assertIn("claim-b[research/claim-verify]", text)
        self.assertIn("synth[research/research-survey]", text)
        self.assertIn("rt-336a0adf", text)
        self.assertNotIn("plan › exec › test › report", text)

    def test_t3_4_failed_node_auto_expands_with_red_key(self):
        jobs = self._lab_route_jobs(sep_liveness="stale")
        render.set_process_view(True)
        lines = render._build_lines([], jobs, section="both", narrow=False, malformed=0,
                                    layout="wide", term_width=168)
        text = _joined(lines)
        self.assertIn("✕", text)
        self.assertIn("⚠ failed node", text)
        # auto-expanded — the DAG line (not just the 1-line header) must be present
        self.assertIn("setup", text)

    def test_exact_reconcile_needed_uses_gate_pending_not_failed(self):
        jobs = self._lab_route_jobs(sep_liveness="stale")
        sep = jobs[2]
        sep.attempt_id = "att-reconcile-sep"
        sep.note = "reconcile-needed"
        sep.state_evidence = {"attempt": {
            "state": "stale", "source": "shared-observer",
            "rule": "terminal-observed/reconcile-needed",
            "attempt_id": sep.attempt_id, "route_id": sep.route_id,
            "route_node": sep.route_node,
            "observed_liveness": {
                "state": "reconcile-needed", "reason": "terminal-observed",
                "process_state": "quiescent",
            },
        }}
        render.set_process_view(True)
        text = _joined(render._build_lines([], jobs, section="both", narrow=False,
                                            malformed=0, layout="wide", term_width=168))
        self.assertIn("…gate", text)
        self.assertNotIn("⚠ failed node", text)

    def test_t3_5_all_done_route_defaults_to_one_line_fold(self):
        # code-test verification.md §10 — a job whose registry row is already `done` NEVER
        # becomes a live DispatchJob (`_scan_jobs_log` drops terminal rows BEFORE
        # classification, dispatch.py ~845): the earlier version of this test injected a
        # `liveness="idle"` depth-2 job to represent a finished stage, which is a state
        # production can never actually produce. The real production shape of "every node
        # done" is `jobs=[]` — the route survives ONLY through `node_evidence` (each entry
        # carries `route_file`, per the code-test-driven `_scan_route_nodes` fix), exactly like
        # this cycle's own record once every stage worker's registry row went terminal.
        dispatch.collect.last_route_nodes = {
            _REAL_RID: {n: {"status": "done", "slug": "x", "ts": None, "note": None,
                            "route_file": _REAL_CLAUDE, "route_hash": None}
                       for n in ("plan", "execute", "test", "report")},
        }
        render.set_process_view(True)
        lines = render._build_lines([], [], section="both", narrow=False,
                                    malformed=0, layout="wide", term_width=168)
        text = _joined(lines)
        self.assertIn("4/4 nodes", text)
        self.assertNotIn("plan ✓", text)   # L2 line never emitted — folded to 1 line
        self.assertIn("▸", text)           # collapsed glyph, never the word "folded"/"hidden"
        self.assertNotIn("folded", text)
        self.assertNotIn("hidden", text)
        self.assertNotIn("no route record", text)  # ★ the regression this defect was about:
                                                     # a valid, fully-resolved record must NOT
                                                     # degrade just because no job is live.

    def test_t3_6_record_less_job_becomes_degrade_card(self):
        job = DispatchJob(key="code", slug="no-record-job", cwd="/x", liveness="working",
                          depth=1, mode="dev")
        render.set_process_view(True)
        lines = render._build_lines([], [job], section="both", narrow=False, malformed=0,
                                    layout="wide", term_width=168)
        text = _joined(lines)
        self.assertIn("no route record", text)
        self.assertIn("no-record-job", text)

    def test_conductor_not_duplicated_as_degrade_card_when_route_child_is_terminal(self):
        """code-test verification_round_2.md §10 — same bug class as defect 1, second location.
        Production-realistic (M2) shape: the route-carrying depth-2 child (`fleet-v10-plan`
        style) has already gone TERMINAL (done/killed/cancelled) — `_scan_jobs_log` drops it
        BEFORE a live DispatchJob is ever built for it, so `jobs` contains ONLY the bare depth-1
        conductor (`route_id=None`, exactly like a real conductor row). Before the fix,
        `covered_slugs` only ever looked at LIVE jobs and stayed empty here, so the conductor's
        bare slug re-entered the degrade pool and rendered a SECOND, contradicting card
        ("no route record") right next to its own real record card. `parent` in the terminal
        node_evidence (dispatch.py's `_scan_route_nodes`, same shape defect 1 added `route_file`
        to) is what lets `covered_slugs` find it without a live child."""
        dispatch.collect.last_route_nodes = {
            _REAL_RID: {"plan": {"status": "done", "slug": "v10-conductor-plan", "ts": None,
                                 "note": "dead-plan-done", "route_file": _REAL_CLAUDE,
                                 "route_hash": None, "parent": "v10-conductor"}},
        }
        conductor = DispatchJob(key="code", slug="v10-conductor", cwd="/x", depth=1,
                                liveness="idle")   # the ONLY job — its route-carrying child is gone
        render.set_process_view(True)
        lines = render._build_lines([], [conductor], section="both", narrow=False,
                                    malformed=0, layout="wide", term_width=168)
        text = _joined(lines)
        self.assertIn("rt-9fa0fed8", text)          # the real record card IS present
        self.assertNotIn("no route record", text)   # the conductor must NOT re-appear as degrade
        self.assertNotIn("v10-conductor", text)     # its bare slug never rendered a 2nd card

    def test_t3_7_active_node_subagent_row_via_pid_join(self):
        conductor = DispatchJob(key="code", slug="v10-sub-conductor", cwd="/x", depth=1,
                                liveness="working")
        child = DispatchJob(key="code-execute", slug="v10-sub-conductor-execute", cwd="/x",
                            parent_slug="v10-sub-conductor", depth=2, liveness="working",
                            route_id=_REAL_RID, route_file=_REAL_CLAUDE, route_node="execute",
                            pid=77001, proc_start="77001-start")
        sess = Session(harness="claude", pid=77001, cwd="/x", slug="v10-sub-conductor-execute",
                       proc_start="77001-start", is_child=True, liveness="working",
                       subagents=[SubAgent(agent_type="explore", active=True)])
        render.set_process_view(True)
        lines = render._build_lines([sess], [conductor, child], section="both", narrow=False,
                                    malformed=0, layout="wide", term_width=168)
        text = _joined(lines)
        self.assertIn(render._ICON_SUBAGENT, text)
        self.assertIn("explore", text)

    def test_t3_12_gate_names_dim_only_behind_a_toggle(self):
        conductor = DispatchJob(key="code", slug="v10-gate-conductor", cwd="/x", depth=1,
                                liveness="working")
        child = DispatchJob(key="code-execute", slug="v10-gate-conductor-execute", cwd="/x",
                            parent_slug="v10-gate-conductor", depth=2, liveness="working",
                            route_id=_REAL_RID, route_file=_REAL_CLAUDE, route_node="execute")
        render.set_process_view(True)
        base = _joined(render._build_lines([], [conductor, child], section="both", narrow=False,
                                           malformed=0, layout="wide", term_width=168))
        self.assertNotIn("code-plan", base)
        render.set_show_all(True)
        toggled = _joined(render._build_lines([], [conductor, child], section="both",
                                              narrow=False, malformed=0, layout="wide",
                                              term_width=168))
        self.assertIn("code-plan", toggled)

    def test_t3_13_no_overflow_at_60_120_168(self):
        jobs = self._lab_route_jobs()
        render.set_process_view(True)
        for w in (60, 120, 168):
            lines = render._build_lines([], jobs, section="both", narrow=(w < 70),
                                        malformed=0, layout=render._layout_mode(w),
                                        term_width=w)
            for ln in lines[6:]:   # skip the shared pulse/mem header block (pre-existing,
                                   # unrelated to F-30 — see dev_log)
                if not ln:
                    continue
                width = sum(render._dw(t) for t, _k in ln)
                self.assertLessEqual(width, w, "overflow at %d: %r" % (w, ln))

    def test_t3_14_no_jobs_shows_honest_empty_state(self):
        render.set_process_view(True)
        lines = render._build_lines([], [], section="both", narrow=False, malformed=0,
                                    layout="wide", term_width=168)
        text = _joined(lines)
        self.assertIn("no active route", text)


class MouseFoldTest(ProcessViewEnv):
    def _draw_once(self, sessions, jobs, w=168, h=40):
        scr = FakeScreen(h, w)
        with mock.patch.object(render.curses, "doupdate"):
            render._draw(scr, sessions, jobs, "both", 0)
        return scr

    def _fold_row(self, card_key):
        for row, entry in render._FOLD_ROWS.items():
            if entry["card_key"] == card_key:
                return row
        raise AssertionError("card_key %s never entered _FOLD_ROWS: %s"
                             % (card_key, render._FOLD_ROWS))

    def test_t3_8_card_click_toggles_fold_no_prompt(self):
        render.set_process_view(True)
        jobs = self._lab_route_jobs()
        self._draw_once([], jobs)
        row = self._fold_row(_LAB_RID)
        self.assertTrue(render._handle_mouse(2, row))
        self.assertIsNone(render._PROMPT)
        self.assertIn(_LAB_RID, render._ROUTE_FOLD)

    def test_t3_9_fold_rows_disjoint_from_click_and_toggle_rows(self):
        render.set_process_view(True)
        conductor = DispatchJob(key="code", slug="v10-conductor", cwd="/x", depth=1,
                                liveness="working")
        child = DispatchJob(key="code-execute", slug="v10-conductor-execute", cwd="/x",
                            parent_slug="v10-conductor", depth=2, liveness="working",
                            route_id=_REAL_RID, route_file=_REAL_CLAUDE, route_node="execute",
                            pid=88001)
        self._draw_once([], [conductor, child] + self._lab_route_jobs())
        fold_set = set(render._FOLD_ROWS)
        click_set = set(render._CLICK_ROWS)
        toggle_set = set(render._TOGGLE_ROWS)
        self.assertEqual(fold_set & (click_set | toggle_set), set())

    def test_t3_9b_folded_completed_card_not_in_toggle_rows(self):
        # Same production-shape correction as T3-5 above — `jobs=[]`, route_file carried by
        # node_evidence only (no `liveness="idle"` fabricated job).
        dispatch.collect.last_route_nodes = {
            _REAL_RID: {n: {"status": "done", "slug": "x", "ts": None, "note": None,
                            "route_file": _REAL_CLAUDE, "route_hash": None}
                       for n in ("plan", "execute", "test", "report")},
        }
        render.set_process_view(True)
        self._draw_once([], [])
        row = self._fold_row(_REAL_RID)
        self.assertNotIn(row, render._TOGGLE_ROWS)
        show_all_before = render._SHOW_ALL
        self.assertTrue(render._handle_mouse(2, row))
        self.assertEqual(render._SHOW_ALL, show_all_before)   # `a` toggle untouched
        self.assertIn(_REAL_RID, render._ROUTE_FOLD)

    def test_t3_10_session_row_click_then_reclick_confirms(self):
        render.set_process_view(True)
        conductor = DispatchJob(key="code", slug="v10-kill-conductor", cwd="/x", depth=1,
                                liveness="working")
        child = DispatchJob(key="code-execute", slug="v10-kill-conductor-execute", cwd="/x",
                            parent_slug="v10-kill-conductor", depth=2, liveness="working",
                            route_id=_REAL_RID, route_file=_REAL_CLAUDE, route_node="execute",
                            pid=88002, proc_start="123")
        self._draw_once([], [conductor, child])
        row = next(r for r, e in render._CLICK_ROWS.items() if e.get("pid") == 88002)
        self.assertTrue(render._handle_mouse(2, row))
        self.assertEqual(render._CURSOR_ID, (88002, "123"))
        self._draw_once([], [conductor, child])
        row2 = next(r for r, e in render._CLICK_ROWS.items() if e.get("pid") == 88002)
        self.assertTrue(render._handle_mouse(2, row2))
        self.assertIsNotNone(render._PROMPT)
        self.assertEqual(render._PROMPT["stage"], "confirm")

    def test_t3_11_prompt_swallows_fold_click(self):
        render.set_process_view(True)
        jobs = self._lab_route_jobs()
        self._draw_once([], jobs)
        row = self._fold_row(_LAB_RID)
        render._PROMPT = {"stage": "confirm", "entry": {"pid": 1, "label": "x", "state": "working"}}
        before = dict(render._ROUTE_FOLD)
        self.assertTrue(render._handle_mouse(2, row))
        self.assertEqual(render._ROUTE_FOLD, before)   # swallowed — fold state untouched
        render._PROMPT = None


class MutationCoverageGapTest(ProcessViewEnv):
    """code-test verification_round_2.md §6 — M3 (`⏳` glyph, defect 4) and M4 (demo `_LAB_RID`
    `setup` seed, defect 3) both flagged as FIXED but UNPROTECTED (reverting either left the
    suite green). Both fixes are otherwise real and already live-verified — these two tests
    exist solely so a future silent revert fails the suite."""

    def test_l1_elapsed_matches_the_board_wide_convention(self):
        # M3 (rewritten by F-76) — the card's L1 elapsed must follow `_ELAPSED_GLYPH`, the
        # ONE board-wide convention, rather than a card-local icon. The original M3 pinned
        # "⏳" here because `_route_job_row` used it; F-76 removed the glyph from every
        # elapsed at once, so matching the board IS the invariant now — what must never
        # come back is a card that decides its own elapsed shape.
        segs = render._route_card_l1(["code", "dev"], "rt-abc12345", 1, 4, 15, False, "▾", None)
        text = "".join(t for t, _k in segs)
        self.assertIn(render._ELAPSED_GLYPH + "15m", text)
        # The v10 critic's real worry survives the glyph: the value must stay visibly
        # detached from "n/m nodes" rather than reading as a number glued onto it.
        self.assertIn("nodes  ", text)

    def test_demo_seeds_lab_setup_node_as_done(self):
        # M4 — demo._seed_route_evidence() must seed _LAB_RID's `setup` node (not just
        # _DEMO_CARD_RID's `plan`) — otherwise the marquee parallel-fan-out demo card draws a
        # logically impossible DAG (a `pending` parent with `active`/`failed` children).
        demo.collect()
        evidence = dispatch.collect.last_route_nodes.get(demo._LAB_RID, {})
        self.assertEqual(evidence.get("setup", {}).get("status"), "done")


if __name__ == "__main__":
    unittest.main()
