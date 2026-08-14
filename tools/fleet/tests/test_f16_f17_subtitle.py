#!/usr/bin/env python3
"""F-16/F-17 merge (사용자 확정 2026-07-19) — the live-title refresher's single haiku call
now returns both a shrunk title AND a one-sentence "what is this session doing right now"
subtitle. This file covers the parts F-17's own test file (test_f17_title_refresh.py) does
not: the additive Session/DispatchJob field, the child-adoption join, and the render.py
subtitle row (presence/silence/dead-stale gating/clip/ordering vs the sub-agent strip).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import collectors as fleet_collectors   # noqa: E402
from fleet import render                           # noqa: E402
from fleet.model import DispatchJob, Session        # noqa: E402


class ModelAdditiveTest(unittest.TestCase):
    def test_session_summary_defaults_none_and_is_json_additive(self):
        s = Session(harness="claude", pid=1, cwd="/x")
        self.assertIsNone(s.summary)
        d = s.to_dict()
        self.assertIn("summary", d)
        self.assertIsNone(d["summary"])
        self.assertIn("title", d)   # unrelated existing field still present — additive, not replaced

    def test_dispatch_job_summary_defaults_none_and_is_json_additive(self):
        j = DispatchJob(key="autopilot-code", slug="j1")
        self.assertIsNone(j.summary)
        d = j.to_dict()
        self.assertIn("summary", d)
        self.assertIsNone(d["summary"])


def _child(pid, cwd, title=None, summary=None, harness="claude", sid=None, proc_start=None):
    return Session(harness=harness, pid=pid, cwd=cwd, session_id=sid or "sid-%d" % pid,
                   slug=os.path.basename(cwd), liveness="working", is_child=True,
                   title=title, summary=summary, proc_start=proc_start)


class AdoptSummaryTest(unittest.TestCase):
    def test_pid_join_adopts_summary_alongside_title(self):
        # v16: exact identity requires (pid, proc_start) on both sides (plan Step 2.4.2).
        child = _child(42, "/work/agent_setting-wt/fix-x", title="Fix flaky tests",
                       summary="지금 실패하는 테스트를 재현하는 중", proc_start="777")
        job = DispatchJob(key="autopilot-code", slug="fix-x", cwd="/tmp/elsewhere",
                          harness="claude", pid=42, proc_start="777",
                          is_child=True, liveness="working")
        fleet_collectors._adopt_child_titles([child], [job])
        self.assertEqual(job.title, "Fix flaky tests")
        self.assertEqual(job.summary, "지금 실패하는 테스트를 재현하는 중")

    def test_summary_adopted_even_without_a_title(self):
        # A child can have a fresh live summary while its title sidecar is still stale/absent
        # (the two fields have independent freshness windows) — the join must not require both.
        child = _child(42, "/work/agent_setting-wt/fix-x", title=None,
                       summary="지금 빌드 로그를 읽는 중", proc_start="777")
        job = DispatchJob(key="autopilot-code", slug="fix-x", pid=42, proc_start="777",
                          cwd="/tmp/elsewhere",
                          harness="claude", is_child=True, liveness="working")
        fleet_collectors._adopt_child_titles([child], [job])
        self.assertIsNone(job.title)
        self.assertEqual(job.summary, "지금 빌드 로그를 읽는 중")

    def test_cwd_join_adopts_summary_when_pid_is_unknown(self):
        child = _child(42, "/work/agent_setting-wt/fix-x", title="Port memory rows",
                       summary="지금 렌더 테스트를 도는 중")
        job = DispatchJob(key="autopilot-code", slug="fix-x",
                          cwd="/work/agent_setting-wt/fix-x", harness="claude",
                          is_child=True, liveness="working")
        fleet_collectors._adopt_child_titles([child], [job])
        self.assertEqual(job.summary, "지금 렌더 테스트를 도는 중")

    def test_ambiguous_cwd_refuses_summary_too(self):
        kids = [_child(1, "/work/shared", title="First", summary="doing first"),
                _child(2, "/work/shared", title="Second", summary="doing second")]
        job = DispatchJob(key="autopilot-code", slug="shared", cwd="/work/shared",
                          harness="claude", is_child=True, liveness="working")
        fleet_collectors._adopt_child_titles(kids, [job])
        self.assertIsNone(job.title)
        self.assertIsNone(job.summary)

    def test_untitled_and_unsummarized_child_leaves_the_job_untouched(self):
        child = _child(42, "/work/agent_setting-wt/fix-x")
        job = DispatchJob(key="autopilot-code", slug="fix-x", pid=42,
                          cwd="/work/agent_setting-wt/fix-x", harness="claude",
                          is_child=True, liveness="working")
        fleet_collectors._adopt_child_titles([child], [job])
        self.assertIsNone(job.title)
        self.assertIsNone(job.summary)


class SummaryRowRenderTest(unittest.TestCase):
    """Uses render._build_lines the same way test_f29_subagents.py's NoRegressionTest does:
    the real assembly path, not a hand-rolled call to the row helper alone."""

    def setUp(self):
        render.reset_selection()

    def _session(self, **over):
        base = dict(harness="claude", pid=90001, cwd="/x", slug="a", title="live session",
                   liveness="working", elapsed_min=5)
        base.update(over)
        return Session(**base)

    def _lines_for(self, sessions, jobs=None, term_width=168):
        return render._build_lines(sessions, jobs or [], section="fleet", narrow=False,
                                   malformed=0, term_width=term_width)

    def test_summary_renders_as_its_own_main_session_row(self):
        summary = "지금 render.py 그룹 루프의 틴트 적용 경로를 분석 중"
        s = self._session(summary=summary)
        lines = self._lines_for([s])
        hits = [ln for ln in lines if ln and summary in "".join(t for t, _k in ln)]
        self.assertEqual(len(hits), 1)
        # F-69 (user 2026-08-14): a MAIN session's NOW sentence is the board's
        # most-read line — it carries the brighter bold `now_main` key while
        # dispatch subtitles keep the dim supporting weight. Leading segments
        # (tint fill / rail marker / indent) are excluded as before.
        text_seg = next((t, k) for t, k in hits[0] if summary in t)
        self.assertEqual(text_seg[1], "now_main")

    def test_summary_absent_still_renders_one_context_dash_row(self):
        # The combined subtitle/context detail row is mandatory on every live identity card —
        # a summary-less/context-less session renders an empty gauge plus `—` rather than
        # omitting the row
        # (plan Step 3.3.2's "neither" case), so row COUNT is unchanged either way; only the
        # row's own TEXT differs.
        with_summary = self._session(pid=1, summary="지금 무언가 하는 중")
        without_summary = self._session(pid=2, summary=None)
        lines_with = self._lines_for([with_summary])
        lines_without = self._lines_for([without_summary])
        self.assertEqual(len(lines_with), len(lines_without))
        joined_without = "\n".join("".join(t for t, _k in ln) for ln in lines_without if ln)
        self.assertNotIn("지금 무언가 하는 중", joined_without)
        # F-52b/c: the row now leads with the session's liveness mark (a `working` session's
        # spinner frame moves, so it is not part of this literal) and carries the 16-cell
        # baseline track — no measured window on this fixture.
        self.assertIn(render._BAR_EMPTY * render._CTX_TRACK_MAX + "   —", joined_without)

    def test_dead_row_omits_summary(self):
        s = self._session(liveness="dead", mtime=None, summary="지금 뭔가 하는 중")
        lines = self._lines_for([s])
        joined = "\n".join("".join(t for t, _k in ln) for ln in lines if ln)
        self.assertNotIn("지금 뭔가 하는 중", joined, "F-13: dead rows show no live telemetry")

    def test_stale_row_omits_summary(self):
        s = self._session(liveness="stale", summary="지금 뭔가 하는 중")
        lines = self._lines_for([s])
        joined = "\n".join("".join(t for t, _k in ln) for ln in lines if ln)
        self.assertNotIn("지금 뭔가 하는 중", joined)

    def test_summary_row_precedes_the_subagent_strip(self):
        from fleet.model import SubAgent
        s = self._session(summary="지금 서브에이전트를 확인하는 중")
        s.subagents = [SubAgent(agent_type="explore", active=True)]
        lines = self._lines_for([s])
        summary_idx = next(i for i, ln in enumerate(lines)
                           if ln and "지금 서브에이전트를 확인하는 중" in "".join(t for t, _k in ln))
        strip_idx = next(i for i, ln in enumerate(lines)
                         if ln and render._ICON_SUBAGENT in "".join(t for t, _k in ln))
        self.assertLess(summary_idx, strip_idx)

    def test_summary_row_indent_is_the_subagent_strip_inset(self):
        segs = render._summary_row("hello", depth=0, term_width=168)[0]
        indent = segs[0][0]
        self.assertEqual(indent.strip(), "")
        self.assertEqual(indent, render._SUBAGENT_IND)

    def test_summary_row_depth_matches_strip_depth_ladder(self):
        d0 = render._summary_row("x", depth=0)[0][0][0]
        d1 = render._summary_row("x", depth=1)[0][0][0]
        d2 = render._summary_row("x", depth=2)[0][0][0]
        self.assertEqual(len(d1), len(d0) + 2)
        self.assertEqual(len(d2), len(d0) + 4)

    def test_long_summary_clips_within_terminal_width(self):
        long_summary = "지금 " + ("아주 긴 상태 설명을 " * 20) + "계속 쓰는 중"
        segs = render._summary_row(long_summary, depth=0, term_width=80)[0]
        text = "".join(t for t, _k in segs)
        self.assertLessEqual(render._dw(text), 80)
        self.assertIn("…", text)

    def test_dispatch_job_summary_row_uses_its_own_depth(self):
        job = DispatchJob(key="autopilot-code", slug="wt", cwd="/x/wt", harness="claude",
                          pid=42, is_child=True, liveness="working", depth=1,
                          parent_sid="parent-sid",
                          summary="지금 스테이지 워커가 테스트를 돌리는 중")
        parent = Session(harness="claude", pid=1, cwd="/x", session_id="parent-sid",
                         slug="x", liveness="working", title="parent")
        child = Session(harness="claude", pid=42, cwd="/x/wt", slug="wt",
                        liveness="working", is_child=True)
        lines = render._build_lines([parent, child], [job], section="both", narrow=False,
                                    malformed=0, term_width=168)
        hit = next(ln for ln in lines
                  if ln and "지금 스테이지 워커가 테스트를 돌리는 중" in "".join(t for t, _k in ln))
        text = "".join(t for t, _k in hit)
        indent = text[:text.index("지금")].lstrip("▍")
        self.assertGreaterEqual(len(indent), len(render._SUBAGENT_IND) + 1)


if __name__ == "__main__":
    unittest.main()
