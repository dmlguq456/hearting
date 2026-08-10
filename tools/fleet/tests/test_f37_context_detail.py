"""Focused F-37 context/NOW subordinate-row and child-association checks."""
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import collectors as fleet_collectors  # noqa: E402
from fleet import projection, render, route  # noqa: E402
from fleet.collectors import dispatch as dispatch_collector  # noqa: E402
from fleet.model import (  # noqa: E402
    ContextEvidence, ContextProjection, DispatchJob, ProgressProjection, Session,
    SubAgent, WorkProjection,
)


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "route")
REAL = os.path.join(FIXTURES, "real_claude_staged.json")
COMPOSED = os.path.join(FIXTURES, "synth_composed_survey.json")


def text(lines):
    return "\n".join("".join(part for part, _ in line) for line in lines if line)


FULL, EMPTY = render._BAR_FULL, render._BAR_EMPTY
# F-52c: the row leads with the session's own liveness mark, not the old 📚 icon. `idle` is the
# one state whose glyph is stable across calls (`working` animates the spinner by wall clock).
# F-55 (v39): the lead cell is the padded state WORD, not the glyph. Taken from the producer so
# this file tracks the F-55a padding ledger instead of re-deriving it.
LEAD = render._context_lead_cell("idle")[0]
# F-52b: no measured `context_window_tokens` on these fixtures → the 16-cell baseline track.
BASE = render._CTX_TRACK_MAX
# A context gauge is the only place a bar track is immediately followed by its right-justified
# value cell (`—` or `NN%`); panel separators are bare `─` runs, so this never matches them.
CTX_GAUGE_RE = re.compile(r"[%s%s]{2,} *(?:—|\d+%%)" % (re.escape(FULL), re.escape(EMPTY)))


class ContextDetailTruthTableTest(unittest.TestCase):
    def _session(self, **kwargs):
        base = dict(harness="claude", pid=1, cwd="/x", liveness="working")
        base.update(kwargs)
        return Session(**base)

    def _idle(self, **kwargs):
        kwargs.setdefault("liveness", "idle")
        return self._session(**kwargs)

    def test_context_now_truth_table(self):
        # 63% over the F-57b 16-cell baseline track: half_up(63 * 16 / 100) = half_up(10.08)
        # = 10 filled, 6 empty (was 13/7 while the base track was 20).
        cases = [
            (ContextProjection(63, "normal", "claude"), "Doing work",
             LEAD + FULL * 10 + EMPTY * 6 + " 63%"),
            (ContextProjection(63, "normal", "claude"), None,
             LEAD + FULL * 10 + EMPTY * 6 + " 63%"),
            (ContextProjection(None, "unknown", "claude"), "Doing work",
             LEAD + EMPTY * BASE + "   —"),
            (None, None, LEAD + EMPTY * BASE + "   —"),
            (ContextProjection(0, "normal", "claude"), None,
             LEAD + EMPTY * BASE + "  0%"),
        ]
        for context, now, context_text in cases:
            row = render._context_detail_row(self._idle(context=context, summary=now), term_width=168)
            expected = " " * render._CONTEXT_INDENT_W + context_text
            if now:
                expected += " " * (render._NAME_COL - render._dw(expected)) + now
            self.assertEqual(text(row), expected)

    def test_stale_and_dead_rows_suppress_cached_detail(self):
        for state in ("stale", "dead"):
            row = render._context_detail_row(
                self._session(liveness=state, context=ContextProjection(85, "critical", "x"),
                              summary="cached now"), term_width=168)
            self.assertEqual(row, [])

    def test_dispatch_uses_main_context_row_even_when_now_is_missing(self):
        job = DispatchJob(
            key="code", slug="worker", harness="claude", depth=1,
            liveness="working", summary="NOW", ctx_pct=85,
            context=ContextProjection(85, "critical", "legacy"),
        )
        job._dispatch_context_owned = True
        job._context_evidence = ContextEvidence(
            used_pct=85, source="claude-attempt-stream", sequence=(1, 1),
            source_head_sequence=(1, 1), observed_at=1, fresh_until=1000)
        for width, layout in ((168, "wide"), (120, "wide"),
                              (100, "narrow"), (60, "stack")):
            with self.subTest(width=width):
                row = render._dispatch_summary_detail_row(
                    job, depth=1, term_width=width)
                visible = text(row)
                self.assertIsNotNone(CTX_GAUGE_RE.search(visible))
                self.assertIn("85%", visible)
                self.assertEqual(visible.index("NOW"), render._NAME_COL)
                self.assertLessEqual(render._dw(visible), width)

                rendered = text(render._build_lines(
                    [], [job], "both", False, 0,
                    layout=layout, term_width=width))
                self.assertIsNotNone(CTX_GAUGE_RE.search(rendered))
                self.assertIn("NOW", rendered)

        job.summary = None
        visible = text(render._dispatch_summary_detail_row(job))
        self.assertIsNotNone(CTX_GAUGE_RE.search(visible))
        self.assertNotIn("NOW", visible)

    def test_dispatch_detail_sits_after_indicator_and_is_quieter_than_main(self):
        for depth in (1, 2):
            with self.subTest(depth=depth):
                job = DispatchJob(
                    key="code", slug="worker", harness="codex", depth=depth,
                    liveness="working", summary="NOW", ctx_pct=85,
                    context=ContextProjection(85, "critical", "codex"),
                    exec_tool={"name": "python3"},
                )
                primary = render._dispatch_row(job)
                indicator_col = sum(render._dw(part) for part, _key in primary[:2])
                detail = render._dispatch_summary_detail_row(
                    job, depth=depth, term_width=168)
                visible = text(detail)

                self.assertEqual(visible.index("working"), indicator_col + 2)
                self.assertEqual(
                    {key for line in detail for _part, key in line if key is not None},
                    {"dim"},
                )

        main = render._context_detail_row(
            self._session(context=ContextProjection(85, "critical", "claude")),
            term_width=168)
        self.assertNotEqual(
            {key for line in main for _part, key in line if key is not None},
            {"dim"},
        )

    def test_malformed_legacy_percentage_is_unavailable_not_clamped(self):
        for malformed in (-1, 101, True, "63"):
            with self.subTest(malformed=malformed):
                row = render._context_detail_row(
                    self._idle(ctx_pct=malformed), term_width=168)
                self.assertEqual(text(row), " " * render._CONTEXT_INDENT_W +
                                 LEAD + EMPTY * BASE + "   —")

    def test_hot_context_stays_visible_without_integrated_alert(self):
        session = self._session(slug="hot", ctx_pct=85)
        visible = text(render._build_lines([session], [], "fleet", False, 0,
                                           layout="wide", term_width=168))
        self.assertNotIn("⚠ context", visible)
        self.assertNotIn("⚠ ctx ", visible)
        self.assertIn("85%", visible)
        self.assertIn("hot", visible)

    def test_context_row_is_cell_safe_at_all_required_widths(self):
        for width in (168, 120, 100, 60):
            row = render._context_detail_row(
                self._idle(context=ContextProjection(63, "normal", "x"),
                           summary="한글 상태 설명이 아주 길게 이어지는 중"), term_width=width)
            self.assertLessEqual(render._dw(text(row)), width)
            track = re.search(r"[%s%s]+" % (re.escape(FULL), re.escape(EMPTY)),
                              text(row)).group(0)
            self.assertEqual(len(track), BASE)
            self.assertIn(LEAD, text(row))
            self.assertLess(text(row).index(LEAD), text(row).index("한글"))
            self.assertNotIn(": ", text(row))
            self.assertIn("   한글", text(row))
            row_text = text(row)
            # The context row starts under the harness name in every layout (2026-07-24).
            self.assertEqual(render._dw(row_text[:row_text.index(LEAD)]), render._CONTEXT_INDENT_W)

    def test_description_column_is_stable_for_value_width_and_depth(self):
        """The description column never moves with the VALUE width (`—`/`0%`/`63%`/`100%` all
        occupy `_CONTEXT_VALUE_W`), and it anchors to `_NAME_COL` whenever the row's prefix
        leaves at least `_CONTEXT_NOW_GAP` cells to get there.

        F-58 narrowed `_NAME_COL` 46→36, which is only 4 cells past a depth-0 prefix
        (4 indent + 8 word + 16 track + 4 value = 32). Each depth level adds 2 cells of
        indent, so at depth ≥1 the `max(_CONTEXT_NOW_GAP, _NAME_COL - prefix)` floor takes
        over and the nested row sits 1 (depth 1) / 3 (depth 2) cells right of the session
        column. That is the F-42c rule working as written — legibility gap first, alignment
        second — not a new behavior, so it is asserted here rather than pinned to a constant.
        """
        for pct in (None, 0, 63, 100):
            context = ContextProjection(pct, "unknown", "x")
            for depth in (0, 1, 2):
                with self.subTest(pct=pct, depth=depth):
                    row = render._context_detail_row(
                        self._idle(context=context, summary="Doing work"),
                        depth=depth, term_width=168)
                    visible = text(row)
                    prefix = render._CONTEXT_INDENT_W + 2 * depth + render._CTX_LABEL_W \
                        + render._CTX_TRACK_MAX + render._CONTEXT_VALUE_W
                    self.assertEqual(
                        render._dw(visible[:visible.index("Doing work")]),
                        max(prefix + render._CONTEXT_NOW_GAP, render._NAME_COL))
                    track = re.search(r"[%s%s]+" % (re.escape(FULL), re.escape(EMPTY)),
                                      visible).group(0)
                    self.assertEqual(len(track), BASE)

    def test_context_band_is_not_rendered(self):
        for band in ("normal", "tight", "critical"):
            with self.subTest(band=band):
                row = render._context_detail_row(
                    self._session(context=ContextProjection(85, band, "x"),
                                  summary="Doing work"), term_width=168)
                visible = text(row)
                self.assertNotIn(band, visible)
                self.assertEqual(render._dw(visible[:visible.index("Doing work")]),
                                 render._NAME_COL)
                self.assertNotIn(": ", visible)

    def test_percentage_is_dim_while_gauge_keeps_level_color(self):
        row = render._context_detail_row(
            self._session(context=ContextProjection(85, "critical", "x")),
            term_width=168)[0]
        self.assertEqual([key for value, key in row if value == " 85%"], ["dim"])
        self.assertIn("lvl_r", [key for value, key in row if FULL in value])

    def test_linear_dispatch_owner_owns_projection_stage_once_at_all_widths(self):
        rid = route.load(REAL)["route_id"]
        session = Session(harness="claude", pid=200, proc_start="root", cwd="/root",
                          session_id="sid-root", slug="root", liveness="working")
        owner = DispatchJob(key="code", slug="owner", parent_sid="sid-root", depth=1,
                            cwd="/root", harness="claude", is_child=True,
                            liveness="working")
        leaf = DispatchJob(key="code-execute", slug="leaf", parent_slug="owner", depth=2,
                           pid=201, proc_start="leaf", route_id=rid, route_file=REAL,
                           route_node="execute", liveness="working")
        projection.attach_projections([session], [owner, leaf], now=100.0)
        for width, layout in ((168, "wide"), (120, "wide"), (100, "narrow"), (60, "stack")):
            lines = render._build_lines([session], [owner, leaf], "both", False, 0,
                                        layout=layout, term_width=width)
            visible = "\n".join("".join(part for part, _ in line) for line in lines if line)
            self.assertNotIn("stage execute", visible)
            self.assertNotIn("←{", visible)
            self.assertIn("plan › execute › test › report", visible)

    def test_replica_route_collapses_and_second_line_stays_log_summary(self):
        # 2026-07-24: the conductor breadcrumb names a replica group ONCE
        # (`impl-review(2-way)`), never leg-by-leg, and a dispatch card's second
        # line is its live log summary — no dedicated stage row even for a
        # non-linear (replica-branched) route.
        nodes = [
            {"id": "plan", "state": "done", "level": 0, "depends_on": []},
            {"id": "execute", "state": "done", "level": 1, "depends_on": ["plan"]},
            {"id": "impl-review", "state": "active", "level": 2,
             "depends_on": ["execute"], "replica_group": "impl-review"},
            {"id": "impl-review-replica", "state": "active", "level": 2,
             "depends_on": ["execute"], "replica_group": "impl-review"},
            {"id": "test", "state": "pending", "level": 3,
             "depends_on": ["impl-review", "impl-review-replica"]},
        ]
        work = WorkProjection(
            source="route-exact", route_id="rt-replica",
            stage_label="impl-review(2-way)", node_state="active",
            progress=ProgressProjection(2, 5),
            _route_view={"view": {"nodes": nodes}},
        )
        session = Session(harness="claude", pid=230, proc_start="root", cwd="/root",
                          session_id="sid-rep", slug="root", liveness="working")
        owner = DispatchJob(key="code", slug="conductor", parent_sid="sid-rep",
                            depth=1, cwd="/root", harness="claude", is_child=True,
                            liveness="working", work_projection=work,
                            summary="review merge")
        for width, layout in ((256, "wide"), (168, "wide"), (100, "narrow"),
                              (60, "stack")):
            visible = text(render._build_lines([session], [owner], "both", False, 0,
                                               layout=layout, term_width=width))
            with self.subTest(width=width):
                self.assertIn("impl-review(2-way)", visible)
                self.assertNotIn("impl-review-replica", visible)
                self.assertNotIn("←{", visible)
                self.assertIn("review merge", visible)
        # A genuinely wide terminal lends its slack to the breadcrumb: the whole
        # collapsed route stays visible instead of folding its early stages.
        wide = text(render._build_lines([session], [owner], "both", False, 0,
                                        layout="wide", term_width=256))
        self.assertIn("plan✓ › execute✓ › impl-review(2-way) › test", wide)

    def test_quick_one_shot_is_rendered_once_on_owner_not_parent_or_detail(self):
        node = {"id": "one-shot", "state": "active", "level": 0, "depends_on": []}
        work = WorkProjection(
            source="route-exact", route_id="rt-one", route_node="one-shot",
            stage_label="one-shot", node_state="active",
            progress=ProgressProjection(0, 1),
            _route_view={"view": {"nodes": [node]}},
        )
        session = Session(
            harness="claude", pid=220, proc_start="root", cwd="/tmp/fleet-one",
            session_id="sid-one", slug="parent", liveness="working",
            work_projection=work,
        )
        owner = DispatchJob(
            key="code", slug="quick-worker", parent_sid="sid-one", is_child=True,
            cwd="/tmp/fleet-one", harness="claude", depth=1, intensity="quick",
            liveness="working", work_projection=work,
        )
        for width, layout in ((168, "wide"), (120, "wide"), (100, "narrow"), (60, "stack")):
            visible = text(render._build_lines(
                [session], [owner], "both", False, 0, layout=layout, term_width=width))
            with self.subTest(width=width):
                self.assertEqual(visible.count("one-shot"), 1)
                self.assertNotIn("quick/exec", visible)
                self.assertNotIn("stage one-shot", visible)

    def test_inline_main_session_uses_artifact_stage_without_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = os.path.join(tmp, "plans", "2026-07-22_inline-main")
            os.makedirs(plan)
            with open(os.path.join(plan, "execute.md"), "w", encoding="utf-8") as stream:
                stream.write("inline implementation evidence\n")
            session = Session(harness="codex", pid=205, proc_start="inline", cwd=tmp,
                              session_id="sid-inline", slug="inline-main",
                              liveness="working")
            projection.attach_projections([session], [], artifact_root=tmp, now=100.0)
            self.assertEqual(session.work_projection.source, "artifact-inferred")
            self.assertEqual(session.work_projection.stage_label, "exec")
            for width in (168, 120, 100, 60):
                lines = render._build_lines([session], [], "fleet", width < 70, 0,
                                            layout=render._layout_mode(width),
                                            term_width=width)
                visible = text(lines)
                with self.subTest(width=width):
                    # F-52c: the context row leads with the session's liveness mark; its
                    # 16-cell baseline track is the stable marker that the row rendered.
                    self.assertIn(EMPTY * render._CTX_TRACK_MAX, visible)
                    self.assertIn("stage exec", visible)      # stage rides the row's own column
                    # An INFERRED inline stage carries NO dedicated detail row (2026-07-24): a
                    # main session must not show the `plan › exec › test` breadcrumb line.
                    self.assertNotIn("exec ● ←{plan}", visible)
                    self.assertEqual(
                        render._projection_stage_detail_rows(session, term_width=width), [])

    def test_composed_pipeline_keeps_parallel_and_fanin_at_all_widths(self):
        rid = route.load(COMPOSED)["route_id"]
        owner = Session(harness="claude", pid=210, proc_start="root", cwd="/root",
                        session_id="sid-composed", slug="root", liveness="working")
        jobs = [
            DispatchJob(key="claim", slug="claim-b", parent_sid="sid-composed", depth=2,
                        route_id=rid, route_file=COMPOSED, route_node="claim-b",
                        liveness="working"),
            DispatchJob(key="claim", slug="claim-a", parent_sid="sid-composed", depth=2,
                        route_id=rid, route_file=COMPOSED, route_node="claim-a",
                        liveness="working"),
        ]
        projection.attach_projections([owner], jobs, now=100.0)
        view = owner.work_projection._route_view["view"]
        for width in (168, 120, 100, 60):
            stage_rows = render._stage_detail_rows(view["nodes"], term_width=width)
            rendered = text(stage_rows)
            with self.subTest(width=width):
                self.assertTrue(all(render._dw(text([row])) <= width for row in stage_rows))
                for node_id in ("survey", "claim-a", "claim-b", "synth"):
                    self.assertEqual(
                        len(re.findall(r"\b%s [✓●…○✕]" % re.escape(node_id), rendered)), 1)
                self.assertIn("claim-a ● ←{survey}", rendered)
                self.assertIn("claim-b ● ←{survey}", rendered)
                self.assertIn("synth ○ ←{claim-a,claim-b}", rendered)
                self.assertIn("| claim-b", rendered)

    def test_arbitrary_dag_keeps_multiple_roots_partial_join_and_exact_edges(self):
        nodes = [
            {"id": "root-a", "state": "done", "level": 0, "depends_on": []},
            {"id": "root-b", "state": "done", "level": 0, "depends_on": []},
            {"id": "a1", "state": "active", "level": 1, "depends_on": ["root-a"]},
            {"id": "a2", "state": "pending", "level": 2, "depends_on": ["a1"]},
            {"id": "partial", "state": "pending", "level": 2,
             "depends_on": ["a1", "root-b"]},
            {"id": "final", "state": "pending", "level": 3,
             "depends_on": ["a2", "partial"]},
        ]
        for width in (168, 120, 100, 60):
            rows = render._stage_detail_rows(nodes, term_width=width)
            rendered = text(rows)
            with self.subTest(width=width):
                self.assertTrue(all(render._dw(text([row])) <= width for row in rows))
                for node in nodes:
                    primary = r"\b%s [✓●…○✕]" % re.escape(node["id"])
                    self.assertEqual(len(re.findall(primary, rendered)), 1)
                for relation in ("a1 ● ←{root-a}", "a2 ○ ←{a1}",
                                 "partial ○ ←{a1,root-b}",
                                 "final ○ ←{a2,partial}"):
                    self.assertIn(relation, rendered)
                self.assertIn("| root-b", rendered)
                self.assertIn("| partial", rendered)

    def test_completed_route_suppresses_session_detail_row(self):
        # 2026-07-24 (user "stage 설명 여전히 뜨는데 이거 없앴다매?"): a fully-done route draws
        # no stage detail row on the owning session — a finished (often dead-conductor) pipeline's
        # whole DAG lingering under the live dispatcher session is noise. Any non-done node still
        # renders so a real failure stays visible.
        def _session(nodes):
            return Session(harness="claude", pid=1, proc_start="p", cwd="/x",
                           session_id="sid-x", slug="root", liveness="working",
                           work_projection=WorkProjection(
                               source="route-exact", route_id="rt-done",
                               _route_view={"view": {"nodes": nodes}}))
        done_nodes = [
            {"id": "plan", "state": "done", "level": 0, "depends_on": []},
            {"id": "execute", "state": "done", "level": 1, "depends_on": ["plan"]},
        ]
        for width in (168, 120, 100, 60):
            with self.subTest(width=width):
                self.assertEqual(
                    render._projection_stage_detail_rows(_session(done_nodes), term_width=width),
                    [])
        live_nodes = [
            {"id": "plan", "state": "done", "level": 0, "depends_on": []},
            {"id": "execute", "state": "active", "level": 1, "depends_on": ["plan"]},
        ]
        rows = render._projection_stage_detail_rows(_session(live_nodes), term_width=168)
        self.assertTrue(rows)
        self.assertIn("execute", text(rows))

    def test_replica_completed_route_collapses_and_suppresses(self):
        # The replica legs collapse to `impl-review(2-way)`; when the whole (collapsed) route
        # is done, still no detail row.
        nodes = [
            {"id": "execute", "state": "done", "level": 0, "depends_on": []},
            {"id": "impl-review", "state": "done", "level": 1, "depends_on": ["execute"],
             "replica_group": "impl-review"},
            {"id": "impl-review-replica", "state": "done", "level": 1,
             "depends_on": ["execute"], "replica_group": "impl-review"},
            {"id": "test", "state": "done", "level": 2,
             "depends_on": ["impl-review", "impl-review-replica"]},
        ]
        session = Session(harness="claude", pid=2, proc_start="p", cwd="/y",
                          session_id="sid-y", slug="root", liveness="working",
                          work_projection=WorkProjection(
                              source="route-exact", route_id="rt-rep-done",
                              _route_view={"view": {"nodes": nodes}}))
        self.assertEqual(render._projection_stage_detail_rows(session, term_width=168), [])


class ClaudeStreamSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = os.path.join(self.tmp.name, ".dispatch", "logs")
        os.makedirs(self.logs)
        self.env = mock.patch.dict(os.environ, {"AGENT_HOME": self.tmp.name})
        self.env.start()
        dispatch_collector._CLAUDE_STREAM_CACHE.clear()

    def tearDown(self):
        dispatch_collector._CLAUDE_STREAM_CACHE.clear()
        self.env.stop()
        self.tmp.cleanup()

    def _job(self, attempt="att-stream-exact"):
        job = DispatchJob(
            key="code", slug="owner", pid=91, proc_start="wrapper", harness="claude",
            attempt_id=attempt, is_child=True, liveness="working",
        )
        job._log_file = os.path.join(self.logs, "owner.%s.claude.jsonl" % attempt)
        return job

    def _write_log(self, job, rows):
        with open(job._log_file, "w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    @staticmethod
    def _assistant(sid, model_id, active):
        return {
            "type": "assistant", "session_id": sid,
            "message": {"model": model_id, "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": active - 30,
            }},
        }

    @staticmethod
    def _agent_use(sid, agent_type="explore"):
        return {
            "type": "assistant", "session_id": sid,
            "timestamp": "2026-07-23T05:00:00Z",
            "message": {"content": [{
                "type": "tool_use", "name": "Agent", "id": "toolu-agent-1",
                "input": {"subagent_type": agent_type},
            }]},
        }

    def test_attempt_stream_uses_one_million_window_default_while_live(self):
        job = self._job()
        self._write_log(job, [
            {"type": "system", "session_id": "sid-child"},
            self._assistant("sid-child", "claude-fable-5", 100),
            self._assistant("sid-child", "claude-fable-5", 160),
        ])
        dispatch_collector._enrich_claude_stream_session(job)
        self.assertEqual(job._runtime_session_id, "sid-child")
        self.assertEqual(job.active_context_tokens, 160)
        self.assertEqual(job.context_window_tokens, 1_000_000)
        self.assertEqual(job.ctx_pct, 0)
        self.assertTrue(job._dispatch_context_owned)
        self.assertIsNone(job.context)
        self.assertIsNotNone(job._context_evidence)
        visible = text(render._dispatch_summary_detail_row(job, depth=2, term_width=168))
        self.assertIn("0%", visible)

    def test_explicit_runtime_window_overrides_one_million_default(self):
        job = self._job()
        self._write_log(job, [
            self._assistant("sid-child", "claude-fable-5", 160),
            {"type": "result", "session_id": "sid-child",
             "modelUsage": {"claude-fable-5": {"contextWindow": 2000}}},
        ])
        dispatch_collector._enrich_claude_stream_session(job)
        self.assertEqual(job.context_window_tokens, 2000)
        self.assertEqual(job.ctx_pct, 8)

    def test_stream_usage_and_same_attempt_model_window_create_context(self):
        job = self._job()
        self._write_log(job, [
            self._assistant("sid-child", "claude-fable-5", 160),
            {"type": "result", "session_id": "sid-child",
             "usage": {"input_tokens": 10, "cache_read_input_tokens": 300,
                       "output_tokens": 40},
             "modelUsage": {"claude-fable-5": {"contextWindow": 1000}}},
        ])
        dispatch_collector._enrich_claude_stream_session(job)
        projection.attach_projections([], [job], now=100.0)
        self.assertEqual(job._runtime_session_id, "sid-child")
        self.assertIsNone(job.model)
        self.assertEqual(job.ctx_pct, 16)
        self.assertEqual(job.context.used_pct, 16)
        self.assertEqual(job.context_window_tokens, 1000)
        self.assertEqual(job.session_output_tokens, 40)

    def test_attempt_stream_attaches_native_subagents_without_inventing_window(self):
        job = self._job()
        self._write_log(job, [
            {"type": "system", "session_id": "sid-child"},
            self._agent_use("sid-child", "fact-check"),
        ])
        dispatch_collector._enrich_claude_stream_session(job)
        self.assertEqual(job._runtime_session_id, "sid-child")
        self.assertEqual(len(job.subagents), 1)
        self.assertEqual(job.subagents[0].agent_type, "fact-check")
        self.assertTrue(job.subagents[0].active)
        self.assertEqual(job.subagents[0].source, "claude-attempt-stream")
        self.assertIsNone(job.context)
        self.assertIsNone(job._context_evidence)
        self.assertEqual(job.to_dict()["subagents"][0]["agent_type"], "fact-check")

    def test_attempt_stream_projects_only_open_tool_executable_label(self):
        job = self._job()
        row = self._assistant("sid-child", "claude-fable-5", 160)
        row["message"]["content"] = [{
            "type": "tool_use", "name": "Bash", "id": "toolu-bash-1",
            "input": {"command": "python3 train.py --token SECRET"},
        }]
        self._write_log(job, [row])
        dispatch_collector._enrich_claude_stream_session(job)
        self.assertEqual(job.exec_tool, {"name": "python3"})
        self.assertNotIn("SECRET", json.dumps(job.to_dict()))

        dispatch_collector._CLAUDE_STREAM_CACHE.clear()
        self._write_log(job, [row, {
            "type": "user", "session_id": "sid-child",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": "toolu-bash-1",
            }]},
        }])
        dispatch_collector._enrich_claude_stream_session(job)
        self.assertIsNone(job.exec_tool)

    def test_multiple_stream_session_ids_and_foreign_path_fail_closed(self):
        job = self._job()
        self._write_log(job, [
            self._assistant("sid-a", "claude-fable-5", 100),
            self._assistant("sid-b", "claude-fable-5", 160),
        ])
        dispatch_collector._enrich_claude_stream_session(job)
        self.assertFalse(hasattr(job, "_runtime_session_id"))
        self.assertEqual(job.association_ambiguity, "multiple-stream-session-ids")
        self.assertIsNone(job.subagents)

        foreign = self._job("att-foreign")
        foreign._log_file = os.path.join(self.tmp.name, "foreign.att-foreign.claude.jsonl")
        with open(foreign._log_file, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(self._assistant("sid-x", "claude-fable-5", 160)) + "\n")
        dispatch_collector._enrich_claude_stream_session(foreign)
        self.assertFalse(hasattr(foreign, "_runtime_session_id"))


class ChildAssociationTest(unittest.TestCase):
    def _child(self, pid, start, cwd, harness="claude"):
        child = Session(harness=harness, pid=pid, proc_start=start, cwd=cwd,
                        session_id="sid-%s" % pid, is_child=True, liveness="working",
                        title="Child title", summary="Child now", ctx_pct=70,
                        active_context_tokens=700, context_window_tokens=1000,
                        context=ContextProjection(70, "tight", "child"))
        child._context_evidence = ContextEvidence(
            used_pct=70, source="child", sequence=(1, 1),
            source_head_sequence=(1, 1), observed_at=1, fresh_until=1000)
        return child

    def test_exact_identity_copies_title_now_and_context(self):
        child = self._child(7, "new", "/child")
        child.subagents = [SubAgent(agent_type="exact-child", active=True)]
        job = DispatchJob(key="code", slug="job", pid=7, proc_start="new", cwd="/other",
                          harness="claude", liveness="working", is_child=True)
        fleet_collectors._adopt_child_titles([child], [job])
        projection.attach_projections([], [job], now=100.0)
        self.assertEqual((job.title, job.summary, job.context.used_pct),
                         ("Child title", "Child now", 70))
        self.assertEqual(job.active_context_tokens, 700)
        self.assertEqual(job.subagents[0].agent_type, "exact-child")

    def test_wrapper_pid_uses_attempt_stream_session_id_for_title_and_now(self):
        child = self._child(7, "runtime", "/child")
        child.subagents = [SubAgent(agent_type="sid-child", active=True)]
        job = DispatchJob(
            key="code", slug="job", pid=99, proc_start="wrapper", cwd="/child",
            harness="claude", liveness="working", is_child=True,
        )
        job._runtime_session_id = child.session_id
        fleet_collectors._adopt_child_titles([child], [job])
        projection.attach_projections([], [job], now=100.0)
        self.assertEqual((job.title, job.summary, job.context.used_pct),
                         ("Child title", "Child now", 70))
        self.assertEqual(job.subagents[0].agent_type, "sid-child")
        self.assertEqual(child.context.used_pct, 70)

    def test_attempt_stream_subagents_win_over_associated_session(self):
        child = self._child(7, "runtime", "/child")
        child.subagents = [SubAgent(agent_type="persistent-child", active=True)]
        job = DispatchJob(
            key="code", slug="job", pid=99, proc_start="wrapper", cwd="/child",
            harness="claude", liveness="working", is_child=True,
            subagents=[SubAgent(agent_type="attempt-stream", active=True)],
        )
        job._runtime_session_id = child.session_id
        fleet_collectors._adopt_child_titles([child], [job])
        self.assertEqual(job.subagents[0].agent_type, "attempt-stream")

    def test_duplicate_stream_session_id_candidates_fail_closed(self):
        first = self._child(7, "a", "/first")
        second = self._child(8, "b", "/second")
        second.session_id = first.session_id
        job = DispatchJob(
            key="code", slug="job", harness="claude", is_child=True,
        )
        job._runtime_session_id = first.session_id
        fleet_collectors._adopt_child_titles([first, second], [job])
        self.assertIsNone(job.title)
        self.assertIsNone(job.summary)
        self.assertIsNone(job.context)
        self.assertEqual(job.association_ambiguity,
                         "multiple-child-session-id-candidates")

    def test_pid_reuse_cwd_ambiguity_and_cross_harness_are_fail_closed(self):
        old = self._child(7, "old", "/shared")
        new = self._child(7, "new", "/shared")
        reused = DispatchJob(key="code", slug="reuse", pid=7, proc_start="missing",
                             cwd="/shared", harness="claude", is_child=True)
        fleet_collectors._adopt_child_titles([old, new], [reused])
        self.assertIsNone(reused.title)
        self.assertIsNone(reused.association_ambiguity)  # exact identity mismatch never falls through
        cwd_ambiguous = DispatchJob(key="code", slug="cwd", cwd="/shared", harness="claude",
                                    is_child=True)
        fleet_collectors._adopt_child_titles([old, new], [cwd_ambiguous])
        self.assertEqual(cwd_ambiguous.association_ambiguity, "multiple-child-cwd-candidates")
        foreign = self._child(8, "x", "/other", harness="codex")
        cross = DispatchJob(key="code", slug="cross", pid=8, proc_start="x", cwd="/other",
                            harness="claude", is_child=True)
        fleet_collectors._adopt_child_titles([foreign], [cross])
        self.assertIsNone(cross.title)

    def test_parent_context_is_not_inherited(self):
        parent = Session(harness="claude", pid=1, cwd="/x", context=ContextProjection(85, "critical", "parent"))
        child = self._child(2, "x", "/child")
        job = DispatchJob(key="code", slug="job", pid=99, proc_start="wrong", cwd="/job",
                          harness="claude", is_child=True)
        fleet_collectors._adopt_child_titles([parent, child], [job])
        self.assertIsNone(job.context)

    def test_group_dispatch_row_uses_job_subagents_without_runtime_session(self):
        parent = Session(
            harness="claude", pid=300, proc_start="parent", cwd="/x",
            session_id="sid-parent", slug="parent", liveness="working",
        )
        job = DispatchJob(
            key="code", slug="wrapper-child", pid=999, proc_start="wrapper",
            cwd="/x", parent_sid="sid-parent", harness="claude", is_child=True,
            liveness="working", subagents=[SubAgent(agent_type="stream-tool", active=True)],
        )
        for width, layout in ((168, "wide"), (120, "wide"),
                              (100, "narrow"), (60, "stack")):
            visible = text(render._build_lines(
                [parent], [job], "both", False, 0, layout=layout, term_width=width))
            with self.subTest(width=width):
                self.assertEqual(visible.count("⚡stream-tool"), 1)
                self.assertNotIn("🧩", next(
                    line for line in visible.splitlines() if "stream-tool" in line))

    def test_process_route_chunk_orders_job_now_then_attempt_subagents(self):
        rid = route.load(REAL)["route_id"]
        job = DispatchJob(key="code", slug="process-leaf", pid=999, proc_start="wrapper",
                          route_id=rid, route_file=REAL, route_node="execute",
                          harness="claude", liveness="working", summary="NOW",
                          subagents=[SubAgent(agent_type="tool", active=True)])
        projection.attach_projections([], [job], now=100.0)
        render.set_process_view(True)
        try:
            lines = render._build_lines([], [job], "both", False, 0,
                                        layout="wide", term_width=168)
        finally:
            render.set_process_view(False)
        visible = "\n".join("".join(part for part, _ in line) for line in lines if line)
        self.assertIsNotNone(CTX_GAUGE_RE.search(visible))
        self.assertLess(visible.index("└▸🚀"), visible.index("NOW"))
        self.assertLess(visible.index("NOW"), visible.index("⚡tool"))
        now_line = next(line for line in visible.splitlines() if "NOW" in line)
        self.assertEqual(now_line.index("NOW"), render._NAME_COL)


class CodexAttemptTelemetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = os.path.join(self.tmp.name, ".dispatch", "logs")
        os.makedirs(self.logs)
        self.worktree = os.path.join(self.tmp.name, "worktree")
        self.nested_home = os.path.join(
            self.worktree, ".dispatch", "nested-codex-home")
        os.makedirs(self.nested_home)
        self.env = mock.patch.dict(os.environ, {"AGENT_HOME": self.tmp.name})
        self.env.start()
        dispatch_collector._CODEX_ATTEMPT_CACHE.clear()

    def tearDown(self):
        dispatch_collector._CODEX_ATTEMPT_CACHE.clear()
        self.env.stop()
        self.tmp.cleanup()

    def _job(self):
        job = DispatchJob(key="code", slug="owner", harness="codex",
                          attempt_id="att-codex-exact", liveness="working",
                          depth=2, cwd=self.worktree)
        job._log_file = os.path.join(
            self.logs, "owner.att-codex-exact.codex.jsonl")
        return job

    def _write_rollout(self, thread_id, home=None):
        sessions = os.path.join(home or self.nested_home, "sessions", "2026", "08", "06")
        os.makedirs(sessions, exist_ok=True)
        path = os.path.join(sessions, "rollout-2026-08-06T00-00-00-%s.jsonl" % thread_id)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "last_token_usage": {"total_tokens": 100000},
                    "total_token_usage": {
                        "input_tokens": 120000, "cached_input_tokens": 30000,
                        "output_tokens": 5000, "reasoning_output_tokens": 1000,
                        "total_tokens": 156000,
                    },
                    "model_context_window": 200000,
                }},
            }) + "\n")
        return path

    def test_exact_token_usage_and_open_command_are_projected(self):
        job = self._job()
        rows = [
            {"type": "dispatch.supervisor.token_usage", "token_usage": {
                "last": {"total_tokens": 100000},
                "total": {"input_tokens": 120000, "cached_input_tokens": 30000,
                          "output_tokens": 5000, "reasoning_output_tokens": 1000,
                          "total_tokens": 156000},
                "model_context_window": 200000}},
            {"type": "item.started", "item": {
                "type": "command_execution", "id": "cmd-1",
                "command": "python3 train.py --secret omitted"}},
        ]
        with open(job._log_file, "w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        dispatch_collector._enrich_codex_attempt_session(job)
        projection.attach_projections([], [job], now=100.0)
        self.assertEqual(job.context.used_pct, 47)  # Codex 12k reserve formula
        self.assertEqual(job.context_window_tokens, 200000)
        self.assertEqual(job.exec_tool, {"name": "python3"})
        self.assertNotIn("secret", json.dumps(job.to_dict()))
        visible = text(render._dispatch_summary_detail_row(job, term_width=168))
        self.assertIn("⚙ python3", visible)

    def test_completed_command_is_not_reported_as_running(self):
        job = self._job()
        with open(job._log_file, "w", encoding="utf-8") as stream:
            for row in (
                    {"type": "item.started", "item": {
                        "type": "command_execution", "id": "cmd-1", "command": "rg x"}},
                    {"type": "item.completed", "item": {
                        "type": "command_execution", "id": "cmd-1", "status": "completed"}}):
                stream.write(json.dumps(row) + "\n")
        dispatch_collector._enrich_codex_attempt_session(job)
        self.assertIsNone(job.exec_tool)

    def test_depth2_raw_exec_joins_exact_projected_rollout_context(self):
        thread_id = "019fd56f-1e8a-77f0-9964-dc8cba43d344"
        self._write_rollout(thread_id)
        job = self._job()
        with open(job._log_file, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n")
            stream.write(json.dumps({"type": "turn.started"}) + "\n")

        dispatch_collector._enrich_codex_attempt_session(job)
        projection.attach_projections([], [job], now=100.0)

        self.assertEqual(job._runtime_session_id, thread_id)
        self.assertEqual(job.context_window_tokens, 200000)
        self.assertEqual(job.active_context_tokens, 100000)
        self.assertEqual(job.context.used_pct, 47)
        self.assertEqual(job._context_evidence.source, "codex-attempt-rollout")

    def test_depth2_duplicate_projected_rollouts_fail_closed(self):
        thread_id = "019fd56f-1e8a-77f0-9964-dc8cba43d344"
        self._write_rollout(thread_id)
        second = os.path.join(self.worktree, ".dispatch", "codex-home")
        self._write_rollout(thread_id, home=second)
        job = self._job()
        with open(job._log_file, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n")

        dispatch_collector._enrich_codex_attempt_session(job)

        self.assertIsNone(job.context_window_tokens)
        self.assertIsNone(job.ctx_pct)
        self.assertIsNone(job._context_evidence)


if __name__ == "__main__":
    unittest.main()
