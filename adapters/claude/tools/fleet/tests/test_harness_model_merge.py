"""F-33 (v11, 사용자 확정 2026-07-16) — model column retired into the harness field as a
parenthetical on WIDE session/dispatch rows: 'claude code (Fable 5 · xhigh)'. narrow/stack
keep their own separate L2 model cell, unchanged (out of scope for this merge).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render                                     # noqa: E402
from fleet.model import DispatchJob, Session, fmt_min        # noqa: E402


class HarnessModelCellTest(unittest.TestCase):
    """Unit-level: `_harness_model_cell` always sums to exactly `width` cells."""

    def _sum(self, segs):
        return sum(render._dw(t) for t, _k in segs)

    def test_no_model_pads_bare_harness_to_width(self):
        segs = render._harness_model_cell("claude", None, None, 32, "hb_claude")
        self.assertEqual(self._sum(segs), 32)
        text = "".join(t for t, _k in segs)
        self.assertEqual(text.strip(), "claude code")

    def test_model_and_effort_render_as_parenthetical(self):
        segs = render._harness_model_cell("claude", "Fable 5", "xhigh", 32, "hb_claude")
        text = "".join(t for t, _k in segs)
        self.assertIn("claude code (Fable 5·xhigh)", text)
        self.assertEqual(self._sum(segs), 32)

    def test_model_without_effort_omits_the_separator(self):
        segs = render._harness_model_cell("claude", "Fable 5", None, 32, "hb_claude")
        text = "".join(t for t, _k in segs)
        self.assertIn("claude code (Fable 5)", text)
        self.assertNotIn(" · ", text)

    def test_long_model_name_clips_but_never_overflows_and_keeps_a_gap(self):
        for width in (20, 24, 30, 32, 40):
            segs = render._harness_model_cell(
                "codex", "gpt-5.1-codex-max-ultra-long-id", "xhigh", width, "hb_codex")
            self.assertEqual(self._sum(segs), width, "width=%d" % width)
            text = "".join(t for t, _k in segs)
            # a maxed-out clip must never run the closing ')' straight into whatever follows
            self.assertTrue(text.endswith(" ") or ")" not in text, repr(text))

    def test_unknown_harness_fallback_is_configurable(self):
        segs = render._harness_model_cell(None, None, None, 16, "dim", unknown="—")
        text = "".join(t for t, _k in segs)
        self.assertEqual(text.strip(), "—")

    def test_colors_reuse_existing_model_and_effort_keys(self):
        segs = render._harness_model_cell("claude", "Fable 5", "xhigh", 32, "hb_claude")
        keys = [k for _t, k in segs]
        self.assertIn("hb_claude", keys)                    # harness text keeps its badge color
        self.assertIn(render._model_key("Fable 5"), keys)   # model reuses _model_cell's family color
        self.assertIn(render._eff_key("xhigh", False), keys)  # effort reuses the heat-ramp color


class NoColumnHeaderTest(unittest.TestCase):
    """F-77 (user "아니 헤더 자체를 통째로 날려") — the WIDE board has no column header row.

    It named three columns and only two were still columns: F-68c retired the time column,
    and the third holds a unit/capability/node token no single word covers. What remained
    labelled a harness name and a session name — both self-evident — for a line per screen.
    """

    def test_wide_board_emits_no_column_header(self):
        lines = render._build_lines([], [], "fleet", False, 0,
                                    layout="wide", term_width=168)
        text = "\n".join("".join(t for t, _k in ln) for ln in lines if ln)
        self.assertNotIn("harness (model·effort)", text)
        self.assertNotIn("session (branch)", text)
        self.assertNotIn("stages", text)

    def test_narrow_keeps_its_layout_band(self):
        """That line is not a column header — it names the layout and how to change it."""
        lines = render._build_lines([], [], "fleet", True, 0,
                                    layout="narrow", term_width=100)
        text = "\n".join("".join(t for t, _k in ln) for ln in lines if ln)
        self.assertIn("SESSIONS", text)
        self.assertIn("press w to cycle", text)

    def test_col_head_helper_is_gone(self):
        self.assertFalse(hasattr(render, "_col_head"))


class SessionRowMergeTest(unittest.TestCase):
    def setUp(self):
        render.reset_selection()

    def _session(self, **over):
        base = dict(harness="claude", pid=1, cwd="/x", slug="s", title="t",
                   liveness="working", model="Fable 5", effort="xhigh", ctx_pct=10,
                   elapsed_min=5, branch="main")
        base.update(over)
        return Session(**base)

    def test_live_row_folds_model_effort_into_harness_field(self):
        s = self._session()
        segs = render._session_row(s, narrow=False, name_width=40)
        text = "".join(t for t, _k in segs)
        self.assertIn("claude code (Fable 5·xhigh)", text)
        self.assertIn("t (main)", text)

    def test_empty_stage_uses_a_dash_at_the_stages_column(self):
        segs = render._session_row(self._session(), narrow=False, name_width=40)
        stage_i = next(i for i, (value, _key) in enumerate(segs) if value == "-")
        stage_col = sum(render._dw(value) for value, _key in segs[:stage_i])
        self.assertEqual(
            stage_col,
            render._NAME_COL + 40 + render._BRANCH_SUFFIX_W + render._WIDE_STAGE_GAP,
        )
        self.assertEqual(segs[stage_i], ("-", "dim"))
        # F-68c: no flush sentinel — the row ends with its inline elapsed tag.
        self.assertNotIn(render._RFLUSH, [value for value, _key in segs])
        # F-76: that tag is the bare duration (no glyph) — asserting on `_ELAPSED_GLYPH`
        # alone would be vacuous now that it is empty, so pin the VALUE and its dim key.
        self.assertEqual(segs[-1], (render._ELAPSED_GLYPH + fmt_min(self._session().elapsed_min),
                                    "dim"))
        self.assertNotIn(render._WAIT_GLYPH, segs[-1][0])   # ⏳ is F-47's badge, not elapsed

    def test_narrow_empty_stage_also_uses_a_dash(self):
        _l1, l2 = render._session_row_2line(self._session())
        self.assertIn(("-", "dim"), l2)

    def test_no_separate_model_cell_survives_on_the_row(self):
        """The old bare `Fable 5 (xhigh)` model-cell phrasing (no leading harness text right
        before it) must not appear a second time outside the harness field."""
        s = self._session()
        segs = render._session_row(s, narrow=False, name_width=40)
        text = "".join(t for t, _k in segs)
        self.assertEqual(text.count("Fable 5"), 1)

    def test_no_model_value_omits_the_parenthetical(self):
        s = self._session(model=None, effort=None)
        segs = render._session_row(s, narrow=False, name_width=40)
        text = "".join(t for t, _k in segs)
        self.assertIn("claude code", text)
        self.assertNotIn("(", text.split("claude code", 1)[1][:5])

    def test_dead_row_shows_bare_harness_no_parenthetical(self):
        s = self._session(liveness="dead", mtime=None)
        segs = render._session_row(s, narrow=False, name_width=40)
        text = "".join(t for t, _k in segs)
        self.assertIn("claude code", text)
        self.assertNotIn("Fable 5", text, "dead row must not show stale model telemetry")

    def test_name_column_starts_at_the_shared_name_col(self):
        s = self._session()
        segs = render._session_row(s, narrow=False, name_width=40)
        consumed = 0
        i = 0
        while consumed < render._NAME_COL:
            consumed += render._dw(segs[i][0])
            i += 1
        self.assertEqual(consumed, render._NAME_COL)


class DispatchRowMergeTest(unittest.TestCase):
    def _job(self, **over):
        base = dict(harness="claude", key="code", slug="j1", liveness="working",
                   model="Opus 4.8", effort="high", elapsed_min=3, cwd="/x", branch="main")
        base.update(over)
        return DispatchJob(**base)

    def test_dispatch_row_shows_its_own_model_and_effort(self):
        j = self._job()
        segs = render._dispatch_row(j, name_width=40)
        text = "".join(t for t, _k in segs)
        self.assertIn("claude code (Opus 4.8·high)", text)
        self.assertIn("j1 (main)", text)

    def test_dispatch_row_falls_back_to_parent_effort_shown_plain(self):
        # user 2026-07-16: the `~` derived-value marker is retired — inherited effort
        # renders exactly like an owned one. 2026-07-20: the full model name outranks the
        # effort word in the narrowed dispatch cell (F-9(c) ladder), so 'xhigh' rides as
        # its 2-char form here rather than clipping 'Opus 4.8' down to 'Opus 4.'.
        j = self._job(effort=None)
        segs = render._dispatch_row(j, name_width=40, parent_effort="xhigh")
        text = "".join(t for t, _k in segs)
        self.assertIn("Opus 4.8·xh", text)
        self.assertNotIn("~xh", text)

    def test_dead_dispatch_row_shows_bare_harness_only(self):
        j = self._job(liveness="dead")
        segs = render._dispatch_row(j, name_width=40)
        text = "".join(t for t, _k in segs)
        self.assertIn("claude code", text)
        self.assertNotIn("Opus 4.8", text)

    def test_dispatch_row_name_lands_on_the_same_name_col_as_a_session_row(self):
        j = self._job()
        dsegs = render._dispatch_row(j, name_width=40)
        ssegs = render._session_row(self._job_as_session(), narrow=False, name_width=40)
        d_consumed, i = 0, 0
        while d_consumed < render._NAME_COL:
            d_consumed += render._dw(dsegs[i][0]); i += 1
        s_consumed, i = 0, 0
        while s_consumed < render._NAME_COL:
            s_consumed += render._dw(ssegs[i][0]); i += 1
        self.assertEqual(d_consumed, s_consumed)

    def _job_as_session(self):
        return Session(harness="claude", pid=1, cwd="/x", slug="s", title="t",
                       liveness="working", model="Opus 4.8", effort="high", ctx_pct=10,
                       elapsed_min=3, branch="main")


class NoOverflowTest(unittest.TestCase):
    """168/200/400 are where the board actually picks the wide layout — same guard the
    ctx-gauge merge already carries for its own knob (test_wide_ctx_gauge.py)."""

    def test_session_and_dispatch_rows_never_overflow_at_wide_widths(self):
        s = Session(harness="claude", pid=1, cwd="/home/u/proj", slug="proj",
                   title="a fairly long descriptive session title here", liveness="working",
                   model="claude-opus-4-1-20250805", effort="xhigh", ctx_pct=42,
                   elapsed_min=42, branch="a-long-branch-name")
        j = DispatchJob(harness="codex", key="code", slug="job-with-a-long-name",
                        liveness="working", model="gpt-5.1-codex-max", effort="high",
                        elapsed_min=7, cwd="/home/u/proj", branch="main")
        for term_width in (168, 200, 400):
            self.assertEqual(render._layout_mode(term_width), "wide")
            name_w = render._wide_name_width(term_width)
            s_text = "".join(t for t, k in render._session_row(s, narrow=False, name_width=name_w)
                             if t != render._RFLUSH)
            j_text = "".join(t for t, k in render._dispatch_row(j, name_width=name_w)
                             if t != render._RFLUSH)
            self.assertLessEqual(render._dw(s_text), term_width, "session overflow at %d" % term_width)
            self.assertLessEqual(render._dw(j_text), term_width, "dispatch overflow at %d" % term_width)


if __name__ == "__main__":
    unittest.main()
