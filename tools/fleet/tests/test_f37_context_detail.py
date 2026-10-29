"""Focused F-37 context/NOW subordinate-row and child-association checks."""
import importlib.util
import json
import os
import re
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
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
# F-55 (v39): the lead cell was the padded state WORD. F-100b (2026-09-03): it is the WHERE
# chip — ` herdr ` reversed for a session herdr lists — in a slot of the same width. The
# fixtures below are herdr-attached so the lead stays a distinctive marker in the text.
LEAD = render._CTX_ON_TEXT.ljust(4 + render._HW - render._CONTEXT_INDENT_W)
# F-52b: no measured `context_window_tokens` on these fixtures → the 16-cell baseline track.
BASE = render._CTX_TRACK_MAX
# A context gauge is the only place a bar track is immediately followed by its right-justified
# value cell (`—` or `NN%`); panel separators are bare `─` runs, so this never matches them.
CTX_GAUGE_RE = re.compile(r"[%s%s]{2,} *(?:—|\d+%%)" % (re.escape(FULL), re.escape(EMPTY)))


class ContextDetailTruthTableTest(unittest.TestCase):
    def _session(self, **kwargs):
        base = dict(harness="claude", pid=1, cwd="/x", liveness="working",
                    herdr_attached=True)
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
                expected += " " * (render._NAME_COL - render._dw(expected)) + now + " · age unknown"
            self.assertEqual(text(row), expected)

    def test_successful_empty_summary_is_explicitly_dim(self):
        row = self._session(summary=None)
        row._details_pending = False
        row._details_error = False
        segments = render._context_detail_row(row, term_width=168, now_key="now_main")[0]
        self.assertIn("no summary yet", text([segments]))
        self.assertIn(("no summary yet", "dim"), segments)

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
                self.assertEqual(render._dw(visible[:re.search(r"(?:\d+%|—) +(\S)", visible).start(1)]), render._NAME_COL)
                self.assertLessEqual(render._dw(visible), width)

                rendered = text(render._build_lines(
                    [], [job], "both", False, 0,
                    layout=layout, term_width=width))
                self.assertIsNotNone(CTX_GAUGE_RE.search(rendered))
                if width >= 100:
                    self.assertIn("age unknown", rendered)
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

                # F-100b: a worker's lead slot is blank (never a herdr pane), so the
                # detail row's first ink is the gauge, one full slot past the indent.
                leading = render._dw(visible[:next(i for i, ch in enumerate(visible) if ch in (FULL, EMPTY))])
                self.assertEqual(leading, indicator_col + 2 + render._CTX_LABEL_W)
                self.assertNotIn("working", visible)
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

    def test_card_worker_gauge_shares_the_owner_gauge_column(self):
        def gauge_col(job, depth):
            visible = text(render._dispatch_summary_detail_row(
                job, depth=depth, term_width=200, in_card=True))
            return render._dw(visible[:next(i for i, ch in enumerate(visible) if ch in (FULL, EMPTY))])

        owner = DispatchJob(
            key="code", slug="owner", harness="codex", depth=1, worker_type="owner",
            liveness="working", summary="NOW", ctx_pct=13,
            context=ContextProjection(13, "ok", "codex"))
        for depth in (2, 3):
            with self.subTest(depth=depth):
                worker = DispatchJob(
                    key="code", slug="worker", harness="claude", depth=depth,
                    worker_type="worker", liveness="working", summary="NOW", ctx_pct=20,
                    context=ContextProjection(20, "ok", "claude"))
                self.assertEqual(gauge_col(worker, depth), gauge_col(owner, 1))

    def test_unapplied_policy_keeps_the_native_gauge(self):
        # An unverified runtime has no applied trigger, but its measured window and usage
        # still draw the gauge they drew before applied triggers existed.
        claude_row = render._context_detail_row(self._idle(
            context_window_tokens=1_000_000, active_context_tokens=262_312,
            context_policy={"state": "unknown", "raw_percentage": 26}), term_width=168)
        self.assertIn("26%", text(claude_row))
        self.assertIn(FULL * 4 + EMPTY * 12, text(claude_row))
        codex_row = render._context_detail_row(self._idle(
            harness="codex", context_window_tokens=475_000, active_context_tokens=317_769,
            context_policy={"state": "unknown", "raw_percentage": None}), term_width=168)
        self.assertIn(FULL * 5 + EMPTY * 3 + " 66%", text(codex_row))  # 12k baseline, 8-cell track
        applied_row = render._context_detail_row(self._idle(
            context_window_tokens=1_000_000, active_context_tokens=262_312, ctx_pct=34,
            context_display_window_tokens=767_000,
            context_policy={"state": "applied", "raw_percentage": 26}), term_width=168)
        self.assertIn("34%", text(applied_row))

    def test_malformed_legacy_percentage_is_unavailable_not_clamped(self):
        for malformed in (-1, 101, True, "63"):
            with self.subTest(malformed=malformed):
                row = render._context_detail_row(
                    self._idle(ctx_pct=malformed), term_width=168)
                self.assertEqual(text(row), " " * render._CONTEXT_INDENT_W +
                                 LEAD + EMPTY * BASE + "   —")

    def test_hot_context_stays_visible_without_integrated_alert(self):
        session = self._session(slug="hot", title="hot", ctx_pct=85)
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
            self.assertLess(text(row).index(LEAD), re.search(r"(?:\d+%|—) +(\S)", text(row)).start(1))
            self.assertNotIn(": ", text(row))
            self.assertTrue(text(row).endswith("age unknown"))
            if width >= 120:
                self.assertIn("한글", text(row))
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
                if width >= 120:
                    self.assertIn("review merge", visible)
                else:
                    self.assertTrue("age unknown" in visible or "age unk" in visible)
        # A genuinely wide terminal lends its slack to the breadcrumb: the whole
        # collapsed route stays visible instead of folding its early stages.
        wide = text(render._build_lines([session], [owner], "both", False, 0,
                                        layout="wide", term_width=256))
        self.assertIn("plan ✓ › execute ✓ › impl-review(2-way) › test", wide)

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

    def test_inline_main_session_without_work_binding_has_no_invented_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = os.path.join(tmp, "plans", "2026-07-22_inline-main")
            os.makedirs(plan)
            with open(os.path.join(plan, "execute.md"), "w", encoding="utf-8") as stream:
                stream.write("inline implementation evidence\n")
            session = Session(harness="codex", pid=205, proc_start="inline", cwd=tmp,
                              session_id="sid-inline", slug="inline-main",
                              liveness="working")
            projection.attach_projections([session], [], artifact_root=tmp, now=100.0)
            self.assertEqual(session.work_projection.source, "none")
            self.assertIsNone(session.work_projection.stage_label)
            for width in (168, 120, 100, 60):
                lines = render._build_lines([session], [], "fleet", width < 70, 0,
                                            layout=render._layout_mode(width),
                                            term_width=width)
                visible = text(lines)
                with self.subTest(width=width):
                    # F-52c: the context row leads with the session's liveness mark; its
                    # 16-cell baseline track is the stable marker that the row rendered.
                    self.assertIn(EMPTY * render._CTX_TRACK_MAX, visible)
                    self.assertNotIn("단계 구현", visible)
                    # An INFERRED inline stage carries NO dedicated detail row (2026-07-24): a
                    # main session must not show the `plan › exec › test` breadcrumb line.
                    self.assertNotIn("exec ● ←{plan}", visible)

    def test_session_never_revives_retired_stage_rows_without_owner(self):
        # Live regression: an idle Codex session retained an open route after
        # its owner card disappeared. Pending nodes resurrected the old strip.
        for harness in ("claude", "codex", "opencode"):
            for state in ("pending", "active", "reconciling", "failed", "done"):
                nodes = [
                    {"id": "execute", "state": "done", "level": 0, "depends_on": []},
                    {"id": "impl-review", "state": state, "level": 1,
                     "depends_on": ["execute"], "parallel_group": "impl-review"},
                    {"id": "impl-review-alt", "state": state, "level": 1,
                     "depends_on": ["execute"], "parallel_group": "impl-review"},
                    {"id": "test", "state": state, "level": 2,
                     "depends_on": ["impl-review", "impl-review-alt"]},
                ]
                session = Session(harness=harness, pid=1, proc_start="p", cwd="/x",
                    session_id="sid-retired", slug="root", title="Session retained",
                    liveness="idle", work_projection=WorkProjection(
                        source="route-exact", route_id="rt-retired",
                        _route_view={"view": {"nodes": nodes}}))
                for width in (168, 120, 100, 60):
                    with self.subTest(harness=harness, state=state, width=width):
                        visible = text(render._build_lines(
                            [session], [], "fleet", False, 0,
                            layout=render._layout_mode(width), term_width=width))
                        self.assertIn("Session retained", visible)
                        self.assertNotRegex(visible, r"(?m)^.*\bstage execute")
                        self.assertNotIn("←{", visible)
                        self.assertNotIn("impl-review", visible)


class ClaudeStreamSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = os.path.join(self.tmp.name, ".dispatch", "logs")
        os.makedirs(self.logs)
        self.env = mock.patch.dict(os.environ, {
            "AGENT_HOME": self.tmp.name,
            "CLAUDE_CONFIG_DIR": self.tmp.name,
        })
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

    def _write_transcript(self, cwd, sid, rows, home=None):
        encoded = "".join("-" if ch in "/._" else ch for ch in cwd)
        project = os.path.join(home or self.tmp.name, "projects", encoded)
        os.makedirs(project, exist_ok=True)
        path = os.path.join(project, sid + ".jsonl")
        with open(path, "w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        return path

    @staticmethod
    def _supervisor(sid, cwd):
        return {
            "type": "dispatch.supervisor.session",
            "parent_attempt_id": "att-parent",
            "session_id": sid,
            "cwd": cwd,
        }

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
    def _init(sid, model_id="claude-fable-5", version="2.1.299"):
        return {"type": "system", "subtype": "init", "session_id": sid,
                "claude_code_version": version, "model": model_id}

    def _launch_metadata(self, env=None, settings_env=None, **extra):
        """Metadata produced by the real Claude wrapper function for this launch env."""
        home = os.path.join(self.tmp.name, "launch-home-%d" % len(os.listdir(self.tmp.name)))
        os.makedirs(home)
        with open(os.path.join(home, "settings.json"), "w", encoding="utf-8") as stream:
            json.dump({"env": settings_env or {}}, stream)
        import importlib.util
        root = Path(__file__).resolve().parents[3]
        for entry in (root / "utilities", root / "adapters" / "claude" / "bin"):
            if str(entry) not in sys.path:
                sys.path.insert(0, str(entry))
        spec = importlib.util.spec_from_file_location(
            "claude_wrapper_for_policy_test", root / "adapters" / "claude" / "bin" / "dispatch-headless.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        launch_env = {"CLAUDE_CONFIG_DIR": home, **(env or {})}
        return {**module.claude_compaction_inputs(launch_env), **extra}

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

    def test_attempt_stream_without_native_policy_keeps_window_and_percent_unknown(self):
        job = self._job()
        self._write_log(job, [
            {"type": "system", "session_id": "sid-child"},
            self._assistant("sid-child", "claude-fable-5", 100),
            self._assistant("sid-child", "claude-fable-5", 160),
        ])
        dispatch_collector._enrich_claude_stream_session(job)
        self.assertEqual(job._runtime_session_id, "sid-child")
        self.assertEqual(job.active_context_tokens, 160)
        self.assertIsNone(job.context_window_tokens)
        self.assertIsNone(job.context_display_window_tokens)
        self.assertEqual(job.context_policy["state"], "unknown")
        self.assertIsNone(job.ctx_pct)
        self.assertTrue(job._dispatch_context_owned)
        self.assertIsNone(job.context)
        self.assertIsNone(job._context_evidence)
        visible = text(render._dispatch_summary_detail_row(job, depth=2, term_width=168))
        self.assertNotIn("0%", visible)

    def test_launch_window_and_native_consumer_give_the_applied_trigger(self):
        job = self._job()
        job._registry_metadata = self._launch_metadata(
            {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"})
        self._write_log(job, [
            self._init("sid-child"),
            self._assistant("sid-child", "claude-fable-5", 183500),
            {"type": "result", "session_id": "sid-child",
             "modelUsage": {"claude-fable-5": {"contextWindow": 1000000,
                                                 "maxOutputTokens": 128000}}},
        ])
        dispatch_collector._enrich_claude_stream_session(job)
        self.assertEqual(job.context_window_tokens, 1000000)
        # 400000 window - min(128000, 20000) output reserve - 13000 buffer.
        self.assertEqual(job.context_display_window_tokens, 367000)
        self.assertEqual(job.context_policy["state"], "applied")
        self.assertEqual(job.context_policy["runtime_version"], "2.1.299")
        self.assertEqual(job.ctx_pct, 50)

    def test_registered_policy_stays_unknown_until_the_raw_window_is_observed(self):
        job = self._job()
        job._registry_metadata = self._launch_metadata(
            {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"})
        self._write_log(job, [self._init("sid-child"),
                              self._assistant("sid-child", "claude-fable-5", 400000)])
        dispatch_collector._enrich_claude_stream_session(job)
        self.assertEqual(job.active_context_tokens, 400000)
        self.assertEqual(job.context_policy["state"], "unknown")
        self.assertEqual(job.context_policy["reason"], "raw-model-window-not-observed")
        self.assertIsNone(job.ctx_pct)
        self.assertIsNone(job.context_display_window_tokens)

    def test_small_configured_window_applies_without_a_terminal_window(self):
        job = self._job()
        job._registry_metadata = self._launch_metadata(
            {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000"})
        self._write_log(job, [self._init("sid-child"),
                              self._assistant("sid-child", "claude-fable-5", 58500)])
        dispatch_collector._enrich_claude_stream_session(job)
        self.assertEqual(job.context_display_window_tokens, 117000)
        self.assertEqual(job.ctx_pct, 50)

    def test_disabled_missing_and_conflicting_launch_inputs_are_unknown(self):
        cases = (
            ("disabled", {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000",
                          "DISABLE_AUTO_COMPACT": "1"}, None, "native-auto-compaction-disabled"),
            ("no-env-window", {}, None, "native-window-source-not-env"),
            ("conflict", {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"},
             {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "300000"},
             "launch-env-and-settings-env-disagree"),
            ("unverified-version", {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000"}, None,
             "native-consumer-version-unverified"),
        )
        for name, env, settings_env, reason in cases:
            with self.subTest(name):
                job = self._job("att-" + name)
                job._registry_metadata = self._launch_metadata(env, settings_env)
                version = "9.9.9" if name == "unverified-version" else "2.1.299"
                self._write_log(job, [self._init("sid-child", version=version),
                                      self._assistant("sid-child", "claude-fable-5", 1000),
                                      {"type": "result", "session_id": "sid-child",
                                       "modelUsage": {"claude-fable-5": {"contextWindow": 1000000}}}])
                dispatch_collector._enrich_claude_stream_session(job)
                self.assertEqual(job.active_context_tokens, 1000)
                self.assertEqual(job.context_policy["reason"], reason)
                self.assertIsNone(job.ctx_pct)
                self.assertIsNone(job.context_display_window_tokens)

    def test_known_zero_active_is_zero_percent(self):
        job = self._job()
        job._registry_metadata = self._launch_metadata(
            {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000"})
        row = self._assistant("sid-child", "claude-fable-5", 30)
        row["message"]["usage"].update(input_tokens=0, cache_creation_input_tokens=0,
                                       cache_read_input_tokens=0)
        self._write_log(job, [self._init("sid-child"), row])
        dispatch_collector._enrich_claude_stream_session(job)
        self.assertEqual(job.active_context_tokens, 0)
        self.assertEqual(job.ctx_pct, 0)

    def test_owner_and_worker_show_the_model_recorded_in_the_exact_attempt(self):
        for depth, alias, model, shown in (
                (1, "opus", "claude-opus-5-5", "Opus 5.5"),
                (2, "sonnet", "claude-sonnet-5-5", "Sonnet 5.5"),
                (2, "opus", "claude-opus-4-8", "Opus 4.8")):
            with self.subTest(depth=depth, model=model):
                job = self._job("att-model-%s-%s" % (depth, alias))
                job.model, job.depth, job.effort = alias, depth, "xhigh"
                job.resolved_model = "claude-opus-5"
                self._write_log(job, [self._assistant("sid-child", model, 160)])
                dispatch_collector._enrich_claude_stream_session(job)
                self.assertEqual(job.resolved_model, model)
                self.assertEqual(job.model, alias)
                self.assertEqual(render._job_display_model(job), shown)
                row = render._dispatch_row(job, name_width=40)
                self.assertIn(shown, "".join(t for t, _k in row))
                self.assertIn(shown, "".join(t for t, _k in render._route_job_row(job)))

    def test_model_alias_is_kept_until_a_real_model_is_logged(self):
        job = self._job("att-model-startup")
        job.model = "opus"
        self._write_log(job, [{"type": "system", "session_id": "sid-child"},
                              self._assistant("sid-child", "<synthetic>", 160)])
        dispatch_collector._enrich_claude_stream_session(job)
        self.assertIsNone(job.resolved_model)
        with mock.patch.object(render, "_resolved_alias_models", return_value={}):
            self.assertEqual(render._job_display_model(job), "Opus")
        self._write_log(job, [{"type": "system", "subtype": "init",
                              "session_id": "sid-child", "model": "claude-opus-5-5"},
                             self._assistant("sid-child", "<synthetic>", 160)])
        dispatch_collector._enrich_claude_stream_session(job, fast_first=True)
        self.assertEqual(render._job_display_model(job), "Opus 5.5")

    def test_stream_usage_and_same_attempt_model_window_create_context(self):
        job = self._job()
        job._registry_metadata = self._launch_metadata(
            {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"})
        self._write_log(job, [
            self._init("sid-child"),
            self._assistant("sid-child", "claude-fable-5", 73400),
            {"type": "result", "session_id": "sid-child",
             "usage": {"input_tokens": 10, "cache_read_input_tokens": 300,
                       "output_tokens": 40},
             "modelUsage": {"claude-fable-5": {"contextWindow": 1000000}}},
        ])
        dispatch_collector._enrich_claude_stream_session(job)
        projection.attach_projections([], [job], now=100.0)
        self.assertEqual(job._runtime_session_id, "sid-child")
        self.assertIsNone(job.model)
        self.assertEqual(job.ctx_pct, 20)
        self.assertEqual(job.context.used_pct, 20)
        self.assertEqual(job.context_window_tokens, 1000000)
        self.assertEqual(job.context_display_window_tokens, 367000)
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
        self.assertIsNone(job.resolved_model)

        foreign = self._job("att-foreign")
        foreign._log_file = os.path.join(self.tmp.name, "foreign.att-foreign.claude.jsonl")
        with open(foreign._log_file, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(self._assistant("sid-x", "claude-fable-5", 160)) + "\n")
        dispatch_collector._enrich_claude_stream_session(foreign)
        self.assertFalse(hasattr(foreign, "_runtime_session_id"))
        self.assertIsNone(foreign.resolved_model)

    def test_supervisor_receipt_resolves_exact_worktree_session_transcript(self):
        cwd = os.path.join(self.tmp.name, "worktree")
        os.makedirs(cwd)
        sid = "11111111-2222-3333-4444-555555555555"
        job = self._job("att-supervised")
        job.cwd = cwd
        self._write_log(job, [self._supervisor(sid, cwd)])
        job._registry_metadata = self._launch_metadata(
            {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"})
        assistant = self._assistant(sid, "claude-fable-5", 91750)
        assistant["sessionId"] = assistant.pop("session_id")
        assistant["message"]["content"] = [{
            "type": "tool_use", "name": "Bash", "id": "toolu-exact",
            "input": {"command": "python3 exact.py"},
        }]
        self._write_transcript(cwd, sid, [self._init(sid), assistant, {
            "type": "result", "sessionId": sid,
            "modelUsage": {"claude-fable-5": {"contextWindow": 1000000}},
        }])

        dispatch_collector._enrich_claude_stream_session(job)

        self.assertEqual(job._runtime_session_id, sid)
        self.assertEqual(job.active_context_tokens, 91750)
        self.assertEqual(job.context_window_tokens, 1000000)
        self.assertEqual(job.ctx_pct, 25)
        self.assertEqual(job.context_display_window_tokens, 367000)
        self.assertEqual(job.exec_tool, {"name": "python3"})
        self.assertEqual(job._context_evidence.source, "claude-session-transcript")
        self.assertEqual(job.resolved_model, "claude-fable-5")

    def test_supervisor_missing_transcript_does_not_borrow_neighbor(self):
        cwd = os.path.join(self.tmp.name, "worktree")
        os.makedirs(cwd)
        sid = "11111111-2222-3333-4444-555555555555"
        job = self._job("att-supervised-missing")
        job.cwd = cwd
        self._write_log(job, [self._supervisor(sid, cwd)])
        self._write_transcript(cwd, "neighbor-session", [
            self._assistant("neighbor-session", "claude-fable-5", 999),
        ])

        dispatch_collector._enrich_claude_stream_session(job)

        self.assertIsNone(job.active_context_tokens)
        self.assertIsNone(job.context_window_tokens)
        self.assertIsNone(job.exec_tool)

    def test_supervisor_uses_recorded_worker_home_over_global_same_session(self):
        cwd = os.path.join(self.tmp.name, "worktree")
        os.makedirs(cwd)
        sid = "11111111-2222-3333-4444-555555555555"
        job = self._job("att-isolated-owner")
        job.cwd = cwd
        worker_home = os.path.join(self.tmp.name, "isolated-worker")
        job._registry_metadata = {
            **self._launch_metadata({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"}),
            "runtime_home": worker_home}
        self._write_log(job, [self._supervisor(sid, cwd)])
        self._write_transcript(cwd, sid, [
            self._assistant(sid, "claude-fable-5", 999),
        ])
        self._write_transcript(cwd, sid, [
            self._init(sid, "claude-opus-5-5"),
            self._assistant(sid, "claude-opus-5-5", 91750),
            {"type": "result", "sessionId": sid,
             "modelUsage": {"claude-opus-5-5": {"contextWindow": 1000000}}},
        ], home=worker_home)

        with mock.patch.object(dispatch_collector, "_native_home_admitted", return_value=True):
            dispatch_collector._enrich_claude_stream_session(job)

        self.assertEqual(job._runtime_session_id, sid)
        self.assertEqual(job.resolved_model, "claude-opus-5-5")
        self.assertEqual(job.active_context_tokens, 91750)
        self.assertEqual(job.context_window_tokens, 1000000)
        self.assertEqual(job.ctx_pct, 25)
        self.assertEqual(job._context_evidence.source, "claude-session-transcript")

    def test_supervisor_missing_recorded_home_does_not_borrow_global_transcript(self):
        cwd = os.path.join(self.tmp.name, "worktree")
        os.makedirs(cwd)
        sid = "11111111-2222-3333-4444-555555555555"
        job = self._job("att-missing-isolated-owner")
        job.cwd = cwd
        job._registry_metadata = {"runtime_home": os.path.join(self.tmp.name, "missing-worker")}
        self._write_log(job, [self._supervisor(sid, cwd)])
        self._write_transcript(cwd, sid, [
            self._assistant(sid, "claude-fable-5", 999),
        ])

        dispatch_collector._enrich_claude_stream_session(job)

        self.assertIsNone(job.active_context_tokens)
        self.assertIsNone(job.resolved_model)

    def test_supervisor_malformed_ambiguous_and_mismatched_evidence_is_denied(self):
        cwd = os.path.join(self.tmp.name, "worktree")
        other = os.path.join(self.tmp.name, "other")
        os.makedirs(cwd)
        os.makedirs(other)
        sid = "11111111-2222-3333-4444-555555555555"

        malformed = self._job("att-supervised-malformed")
        malformed.cwd = cwd
        self._write_log(malformed, [self._supervisor("../bad", cwd)])
        dispatch_collector._enrich_claude_stream_session(malformed)
        self.assertEqual(malformed.association_ambiguity,
                         "invalid-supervisor-session-event")
        self.assertFalse(hasattr(malformed, "_runtime_session_id"))

        ambiguous = self._job("att-supervised-ambiguous")
        ambiguous.cwd = cwd
        self._write_log(ambiguous, [self._supervisor(sid, cwd),
                                    self._supervisor(sid, cwd)])
        dispatch_collector._enrich_claude_stream_session(ambiguous)
        self.assertEqual(ambiguous.association_ambiguity,
                         "multiple-supervisor-session-events")

        mismatched = self._job("att-supervised-mismatch")
        mismatched.cwd = cwd
        self._write_log(mismatched, [self._supervisor(sid, other)])
        self._write_transcript(other, sid, [self._assistant(
            sid, "claude-fable-5", 999)])
        dispatch_collector._enrich_claude_stream_session(mismatched)
        self.assertEqual(mismatched.association_ambiguity,
                         "supervisor-worktree-mismatch")
        self.assertIsNone(mismatched.active_context_tokens)


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
        self.assertLess(re.search(r"(?:\d+%|—) +(\S)", visible).start(1), visible.index("⚡tool"))
        now_line = next(line for line in visible.splitlines() if "NOW" in line)
        self.assertEqual(render._dw(now_line[:now_line.index("NOW")]), render._NAME_COL)


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

    @staticmethod
    def _producer_usage(wire):
        path = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                            "utilities", "codex-app-server-supervisor.py")
        spec = importlib.util.spec_from_file_location("f37_codex_producer", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.normalize_token_usage(wire)

    def _usage_wire(self):
        return {
            "last": {"totalTokens": 100000},
            "total": {"inputTokens": 120000, "cachedInputTokens": 30000,
                      "outputTokens": 5000, "reasoningOutputTokens": 1000,
                      "totalTokens": 156000},
            "modelContextWindow": 200000,
        }

    def _write_rows(self, job, rows):
        with open(job._log_file, "w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def test_exact_token_usage_and_open_command_are_projected(self):
        job = self._job()
        thread_id, turn_id = "thread-exact-preview", "turn-exact-preview"
        rows = [
            {"type": "dispatch.supervisor.turn.started", "thread_id": thread_id,
             "turn_id": turn_id},
            {"type": "dispatch.supervisor.token_usage", "thread_id": thread_id,
             "turn_id": turn_id, "token_usage": self._producer_usage(self._usage_wire())},
            {"type": "item.started", "item": {
                "type": "command_execution", "id": "cmd-1",
                "command": "python3 train.py --secret omitted"}},
        ]
        self._write_rows(job, rows)
        self.assertNotIn("timestamp", rows[1])
        self.assertEqual(rows[1]["token_usage"]["last"]["total_tokens"], 100000)
        self.assertEqual(rows[1]["token_usage"]["total"]["total_tokens"], 156000)
        dispatch_collector._enrich_codex_attempt_session(job)
        projection.attach_projections([], [job], now=100.0)
        self.assertIsNone(job.context)  # app-server tokenUsage omits the resolved native trigger
        self.assertEqual(job.context_policy["state"], "unknown")
        self.assertEqual(job.active_context_tokens, 100000)
        self.assertEqual(job.session_total_tokens, 156000)
        self.assertEqual(job.context_window_tokens, 200000)
        self.assertEqual(job._runtime_session_id, thread_id)
        self.assertEqual(job.exec_tool, {"name": "python3"})
        self.assertNotIn("secret", json.dumps(job.to_dict()))
        visible = text(render._dispatch_summary_detail_row(job, term_width=168))
        self.assertIn("⚙ python3", visible)

    def test_fake_stage_producer_output_flows_through_exact_attempt_detail(self):
        fake = os.path.join(self.tmp.name, "app_server.py")
        with open(fake, "w", encoding="utf-8") as stream:
            stream.write('''import json, sys
def send(value): print(json.dumps(value), flush=True)
for line in sys.stdin:
 value=json.loads(line); method=value.get("method")
 if method == "initialize": send({"jsonrpc":"2.0","id":value["id"],"result":{}})
 elif method == "thread/start": send({"jsonrpc":"2.0","id":value["id"],"result":{"thread":{"id":"thread-produced"}}})
 elif method == "turn/start":
  send({"jsonrpc":"2.0","id":value["id"],"result":{"turn":{"id":"turn-produced"}}})
  send({"jsonrpc":"2.0","method":"thread/tokenUsage/updated","params":{"threadId":"thread-produced","turnId":"turn-produced","tokenUsage":{"last":{"totalTokens":50000},"total":{"totalTokens":90000},"modelContextWindow":100000,"prompt":"PRIVATE_SENTINEL","extra":{"secret":"PRIVATE_SENTINEL"}}}})
  send({"jsonrpc":"2.0","method":"item/completed","params":{"turnId":"turn-produced","item":{"type":"agentMessage","id":"final","text":"artifact: -\\nverdict: PASS\\nblocker: none"}}})
  send({"jsonrpc":"2.0","method":"turn/completed","params":{"turn":{"id":"turn-produced","status":"completed"}}})
''')
        supervisor = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                  "utilities", "codex-app-server-supervisor.py")
        result = subprocess.run(
            [sys.executable, supervisor, "--one-turn", "--worktree", self.worktree,
             "--sandbox", "read-only", "--app-server-command", f"{sys.executable} {fake}"],
            input="bounded stage prompt", text=True, capture_output=True, timeout=10,
            # This private fake stage is not the enclosing route owner.
            env={key: value for key, value in os.environ.items()
                 if key != "AGENT_OWNER_RUNTIME_PERMISSION_CONTEXT"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        produced = [json.loads(line) for line in result.stdout.splitlines()]
        telemetry = [row for row in produced
                     if row.get("type") == "dispatch.supervisor.token_usage"]
        self.assertEqual(len(telemetry), 1)
        self.assertEqual(telemetry[0]["thread_id"], "thread-produced")
        self.assertEqual(telemetry[0]["turn_id"], "turn-produced")
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(telemetry))
        job = self._job()
        self._write_rows(job, produced)
        dispatch_collector._enrich_codex_attempt_session(job)
        projection.attach_projections([], [job], now=100.0)
        self.assertEqual(job.active_context_tokens, 50000)
        self.assertEqual(job.context_window_tokens, 100000)
        self.assertIsNone(job.context)
        self.assertEqual(job.context_policy["state"], "unknown")
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(job.to_dict()))

    def test_cumulative_only_and_invalid_window_stay_unknown(self):
        job = self._job()
        self._write_rows(job, [{
            "type": "dispatch.supervisor.token_usage", "thread_id": "thread-unknown",
            "turn_id": "turn-unknown", "token_usage": {
                "last": {}, "total": {"total_tokens": 90000},
                "model_context_window": "100000"},
        }])
        dispatch_collector._enrich_codex_attempt_session(job)
        projection.attach_projections([], [job], now=100.0)
        self.assertIsNone(job.context)
        self.assertIsNone(job.active_context_tokens)
        self.assertIsNone(job.context_window_tokens)

    def test_exact_attempt_usage_without_thread_id_keeps_legacy_compatibility(self):
        job = self._job()
        self._write_rows(job, [{
            "type": "dispatch.supervisor.token_usage", "turn_id": "legacy-turn",
            "token_usage": self._producer_usage(self._usage_wire()),
        }])
        dispatch_collector._enrich_codex_attempt_session(job)
        projection.attach_projections([], [job], now=100.0)
        self.assertIsNone(job.association_ambiguity)
        self.assertIsNone(job.context)
        self.assertEqual(job.context_policy["state"], "unknown")
        self.assertEqual(job.session_total_tokens, 156000)

    def test_mixed_thread_usage_activity_and_exec_are_all_unknown(self):
        job = self._job()
        a, b, turn = "thread-a", "thread-b", "turn-a"
        job.active_context_tokens = 777
        job.session_total_tokens = 888
        job.context = ContextProjection(47, "normal", "codex")
        job._context_evidence = ContextEvidence(
            used_pct=47, source="old", sequence=(1, 1), source_head_sequence=(1, 1),
            observed_at=1, fresh_until=1000)
        self._write_rows(job, [
            {"type": "dispatch.supervisor.turn.started", "thread_id": a, "turn_id": turn},
            {"type": "dispatch.supervisor.token_usage", "thread_id": a, "turn_id": turn,
             "timestamp": "2026-10-03T10:00:00+00:00",
             "token_usage": self._producer_usage(self._usage_wire())},
            {"type": "item.started", "item": {"type": "command_execution", "id": "cmd-a",
                                                  "command": "python train.py --sensitive"}},
            {"type": "thread.started", "thread_id": b},
        ])
        dispatch_collector._enrich_codex_attempt_session(job)
        projection.attach_projections([], [job], now=100.0)
        self.assertEqual(job.association_ambiguity, "multiple-attempt-thread-ids")
        self.assertIsNone(job._runtime_session_id)
        self.assertIsNone(job._runtime_activity)
        self.assertIsNone(job.context)
        self.assertIsNone(job.ctx_pct)
        self.assertIsNone(job.active_context_tokens)
        self.assertIsNone(job.context_window_tokens)
        self.assertIsNone(job.session_total_tokens)
        self.assertIsNone(job._context_evidence)
        self.assertIsNone(job.exec_tool)
        visible = text(render._dispatch_summary_detail_row(job, term_width=168))
        self.assertIn("—", visible)
        self.assertNotIn("47%", visible)
        self.assertNotIn("python", visible)

    def test_head_tail_thread_ambiguity_does_not_reuse_tail_usage(self):
        job = self._job()
        head_thread, tail_thread = "head-thread", "tail-thread"
        filler = "x" * (dispatch_collector._CLAUDE_SUPERVISOR_HEAD_BYTES
                        + dispatch_collector._CLAUDE_STREAM_TAIL_BYTES + 1024)
        with open(job._log_file, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "thread.started", "thread_id": head_thread}) + "\n")
            stream.write(filler + "\n")
            stream.write(json.dumps({"type": "thread.started", "thread_id": tail_thread}) + "\n")
            stream.write(json.dumps({"type": "dispatch.supervisor.token_usage",
                                     "thread_id": tail_thread, "turn_id": "tail-turn",
                                     "token_usage": self._producer_usage(self._usage_wire())}) + "\n")
        parsed = dispatch_collector._parse_codex_attempt_tail(job._log_file)
        self.assertTrue(parsed["thread_ambiguity"])
        dispatch_collector._enrich_codex_attempt_session(job)
        projection.attach_projections([], [job], now=100.0)
        self.assertIsNone(job.context)
        self.assertIsNone(job._context_evidence)
        self.assertIsNone(job.exec_tool)

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
        self.assertIsNone(job.context)
        self.assertEqual(job.context_policy["state"], "unknown")
        self.assertIsNone(job._context_evidence)

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
