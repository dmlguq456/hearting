"""F-52a/b/c — thin gauge glyphs, window-proportional context track, liveness lead cell.

v36 (2026-08-04) replaced only F-51a's GLYPH and context-WIDTH clauses: the usage header meter
stays a fixed six cells, and every quantization / color / unknown-vs-0% rule is untouched.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render                                       # noqa: E402
from fleet.model import ContextProjection, DispatchJob, Session  # noqa: E402


FULL, EMPTY = render._BAR_FULL, render._BAR_EMPTY


def track_of(row):
    """The gauge track text of a context detail row (the two segments after the lead cell)."""
    return "".join(value for value, _key in row[0][2:4])


class F52aGlyphTest(unittest.TestCase):
    def test_gauge_glyphs_are_the_thin_mid_height_bars(self):
        self.assertEqual((FULL, EMPTY), ("━", "─"))

    def test_both_surfaces_still_share_one_producer(self):
        session = Session(harness="claude", pid=1, cwd="/x", liveness="idle",
                          rl_5h=75, rl_7d=30, mtime=1000)
        header = "".join(v for row in render._usage_header_rows([session]) for v, _k in row)
        detail = "".join(v for v, _k in render._context_detail_row(session)[0])
        self.assertIn(FULL, header)
        self.assertNotIn("█", header + detail)   # no battery block anywhere
        self.assertNotIn("░", header + detail)


class F52bTrackLengthTest(unittest.TestCase):
    def test_reference_windows_map_to_the_documented_track_lengths(self):
        # F-57b (v41) restored the 1M reference track to 16 cells (F-54 had widened it to 20);
        # every entry is `clamp(half_up(16 * window / 1M), 1, 16)` against that new base.
        cases = {1000000: 16, 256000: 4, 262144: 4, 200000: 3, 500000: 8, 2000000: 16}
        for window, cells in cases.items():
            with self.subTest(window=window):
                self.assertEqual(render._context_gauge_track(window), cells)

    def test_half_up_at_the_boundary_and_clamped_to_at_least_one_cell(self):
        # 16 * w / 1M == 2.5 exactly → half-up lands on 3, not banker's 2.
        self.assertEqual(render._context_gauge_track(156250), 3)
        for tiny in (1, 1000, 31249):
            self.assertEqual(render._context_gauge_track(tiny), 1)

    def test_unmeasured_window_falls_back_to_the_baseline_track(self):
        for missing in (None, 0, -1, True, "1000000", float("nan"), float("inf")):
            with self.subTest(missing=missing):
                self.assertEqual(render._context_gauge_track(missing), render._CTX_TRACK_MAX)

    def _row(self, window, pct):
        session = Session(harness="claude", pid=1, cwd="/x", liveness="idle",
                          ctx_pct=pct, context_window_tokens=window)
        return render._context_detail_row(session, term_width=168)

    def test_row_track_follows_the_measured_window(self):
        self.assertEqual(len(track_of(self._row(1000000, 50))), 16)
        self.assertEqual(len(track_of(self._row(256000, 50))), 4)
        self.assertEqual(len(track_of(self._row(None, 50))), 16)   # unknown → baseline, row stays
        self.assertIn("50%", "".join(v for v, _k in self._row(None, 50)[0]))

    def test_no_depth_dependent_shrink_survives(self):
        """F-51a abolished the per-depth narrowing; length depends on the window only."""
        session = Session(harness="claude", pid=1, cwd="/x", liveness="idle", ctx_pct=50,
                          context_window_tokens=1000000)
        for depth in (0, 1, 2, 3):
            row = render._context_detail_row(session, depth=depth, term_width=200)
            self.assertEqual(len(track_of(row)), 16)

    def test_fill_is_half_up_over_the_track_with_reserved_ends(self):
        for pct, filled in ((None, 0), (0, 0), (1, 1), (2, 1), (3, 1), (50, 10),
                            (97, 19), (99, 19), (100, 20), (150, 20)):
            with self.subTest(pct=pct):
                segs = render._gauge_segs(pct, 99, track=20)
                self.assertEqual(sum(len(v) for v, _k in segs), 20)
                self.assertEqual(len(segs[0][0]), filled)

    def test_one_cell_track_never_over_reports(self):
        """A 1-cell track has no in-between cell — lighting it below 100% would read as full."""
        for pct in (None, 0, 1, 50, 99, 99.9):
            with self.subTest(pct=pct):
                self.assertEqual(render._gauge_segs(pct, 6, track=1)[0][0], "")
        self.assertEqual(render._gauge_segs(100, 6, track=1)[0][0], FULL)
        row = self._row(50000, 99)                      # 50K window → a single cell
        self.assertEqual(track_of(row), EMPTY)
        self.assertEqual(track_of(self._row(50000, 100)), FULL)

    def test_usage_header_meter_is_fixed_whatever_the_session_window(self):
        """The two axes stay separate: F-52b's context track is window-proportional, the
        usage meter is a constant. F-59 moved that constant 6→12 and this test reads it from
        `_GAUGE_W` so the separation, not the number, is what is asserted."""
        for window in (None, 256000, 1000000):
            with self.subTest(window=window):
                session = Session(harness="claude", pid=1, cwd="/x", liveness="idle",
                                  rl_5h=75, rl_7d=30, mtime=1000,
                                  context_window_tokens=window)
                joined = "".join(v for row in render._usage_header_rows([session])
                                 for v, _k in row)
                self.assertIn("[" + FULL * 9 + EMPTY, joined)   # 75% → half_up(9.0) = 9/12
                for chunk in joined.split("[")[1:]:
                    meter = chunk.split("]")[0]
                    self.assertEqual(len([c for c in meter if c in (FULL, EMPTY)]),
                                     render._GAUGE_W)

    def test_default_track_argument_is_the_usage_meter_width(self):
        self.assertEqual(render._gauge_segs(50, 99),
                         render._gauge_segs(50, 99, track=render._GAUGE_W))


class ContextDisplayRestraintTest(unittest.TestCase):
    def test_wide_detail_keeps_percent_but_hides_absolute_tokens(self):
        session = Session(harness="claude", pid=1, cwd="/x", liveness="working",
                          ctx_pct=38, active_context_tokens=380000,
                          context_window_tokens=1000000, summary="working now")
        row = render._context_detail_row(session, term_width=168)[0]
        text = "".join(value for value, _key in row)
        self.assertIn("38%", text)
        self.assertNotIn("380K", text)
        self.assertNotIn("1M", text)
        self.assertLessEqual(render._dw(text), 168)

    def test_narrow_detail_also_hides_absolute_tokens(self):
        session = Session(harness="claude", pid=1, cwd="/x", liveness="working",
                          ctx_pct=47, active_context_tokens=100000,
                          context_window_tokens=200000, summary="working now")
        for width in (20, 22, 24, 60):
            with self.subTest(width=width):
                text = "".join(value for value, _key in
                               render._context_detail_row(session, term_width=width)[0])
                self.assertNotIn("100K/200K", text)
                self.assertLessEqual(render._dw(text), width)

    def test_telemetry_remains_available_for_json_and_gauge_normalization(self):
        session = Session(harness="claude", pid=1, cwd="/x", liveness="idle",
                          ctx_pct=38, active_context_tokens=380000,
                          context_window_tokens=1000000)
        self.assertEqual(session.active_context_tokens, 380000)
        self.assertEqual(session.context_window_tokens, 1000000)
        self.assertEqual(len(track_of(render._context_detail_row(session, term_width=168))), 16)


class MainOwnershipWeightTest(unittest.TestCase):
    def test_child_count_uses_the_main_session_name_hue_in_wide_and_narrow(self):
        session = Session(harness="claude", pid=1, cwd="/x", liveness="working",
                          slug="main")
        expected = render._NAME_KEY["claude"]
        wide = render._session_row(session, narrow=False, is_parent=True, child_count=3)
        narrow = render._session_row_2line(
            session, is_parent=True, child_count=3, term_width=80)[0]
        for row in (wide, narrow):
            self.assertIn((" ▾3", expected), row)
            self.assertNotIn((" ▾3", "dim"), row)


class F100bLeadChipTest(unittest.TestCase):
    """F-52c's lead-cell SLOT, as re-purposed by F-100b (user 2026-09-03): the cell holds
    the WHERE chip, not the state word — the L1 glyph is the one status indicator. Its
    position and width are still F-52c/F-55's, so the gauge column never moved."""

    def _lead(self, row):
        return row[0][1]

    def _session(self, state="idle", **over):
        base = dict(harness="claude", pid=1, cwd="/x", liveness=state, ctx_pct=40,
                    slug="s", elapsed_min=1)
        base.update(over)
        return Session(**base)

    def test_attached_session_leads_with_the_reversed_herdr_chip_in_every_state(self):
        for state in ("working", "idle", "blocked", "unused", "queued", "done", "unknown"):
            with self.subTest(state=state):
                row = render._context_detail_row(self._session(state, herdr_attached=True),
                                                 term_width=168)[0]
                self.assertEqual(row[1], (render._CTX_CHIP_TEXT, "herdr_chip"))
                self.assertEqual(row[2], (" ", None))
                self.assertEqual(render._dw(row[1][0] + row[2][0]), render._CTX_LABEL_W)
                self.assertTrue(render._HUE_OF["herdr_chip"][1] & render._A_REVERSE)

    def test_plain_terminal_leads_with_dim_tty_in_the_same_slot(self):
        text, key = self._lead(render._context_detail_row(
            self._session(herdr_attached=False), term_width=168))
        self.assertEqual(text, render._CTX_OFF_TEXT.ljust(render._CTX_CHIP_W) + " ")
        self.assertEqual(key, "dim")
        self.assertEqual(render._dw(text), render._CTX_LABEL_W)

    def test_unknown_attachment_leaves_the_slot_blank_never_a_guess(self):
        text, key = self._lead(render._context_detail_row(self._session(), term_width=168))
        self.assertEqual(text, " " * render._CTX_LABEL_W)
        self.assertIsNone(key)

    def test_the_state_word_is_gone_from_the_row(self):
        """The whole point: `working`/`idle` duplicated the L1 glyph one line down."""
        for state in ("working", "idle", "blocked", "unused", "queued", "done", "unknown"):
            for attached in (True, False, None):
                with self.subTest(state=state, attached=attached):
                    row = render._context_detail_row(
                        self._session(state, herdr_attached=attached), term_width=168)[0]
                    visible = "".join(v for v, _k in row)
                    self.assertNotIn(state, visible)
                    self.assertFalse(set(visible) & set(render._SPIN))

    def test_plugin_agent_has_no_context_detail_row(self):
        """F-73: plugin-queue telemetry stays JSON-only; the subagent row has no gauge."""
        job = DispatchJob(key="code", slug="w", harness="codex", depth=1, source="plugin-queue",
                          liveness="working", context=ContextProjection(40, "normal", "x"))
        row = render._dispatch_summary_detail_row(job, depth=1, term_width=168)
        self.assertEqual(row, [])

    def test_no_book_icon_and_no_new_state_vocabulary(self):
        session = Session(harness="claude", pid=1, cwd="/x", liveness="idle", ctx_pct=40,
                          herdr_attached=True)
        visible = "".join(v for v, _k in render._context_detail_row(session)[0])
        self.assertNotIn("\U0001f4da", visible)
        self.assertFalse(hasattr(render, "_CTX_LABEL"))
        self.assertFalse(hasattr(render, "_context_lead_cell"))
        keys = {k for _v, k in render._context_detail_row(session)[0]}
        self.assertTrue(keys <= {None, "dim", "lvl_g", "lvl_y", "lvl_r", "herdr_chip"})

    def test_label_ledger_matches_the_real_display_cells(self):
        """`_CTX_LABEL_W` is computed with len() (module load runs before `_dw` exists) — pin
        it against the actual display width of both lead shapes, and pin the slot to the
        F-55 width so the gauge/NOW anchors provably did not move."""
        self.assertEqual(render._dw(render._CTX_CHIP_TEXT + " "), render._CTX_LABEL_W)
        self.assertEqual(render._dw(render._CTX_OFF_TEXT.ljust(render._CTX_CHIP_W) + " "),
                         render._CTX_LABEL_W)
        self.assertEqual(render._CTX_CHIP_W, 7)          # ` herdr ` == len("working")
        self.assertEqual(render._CTX_LABEL_W, 8)         # + one trailing space, as F-55

    def test_stale_and_dead_never_reach_the_lead_cell(self):
        for state in ("stale", "dead"):
            with self.subTest(state=state):
                self.assertEqual(render._context_detail_row(
                    self._session(state, herdr_attached=True), term_width=168), [])


class F100bNarrowDegradeTest(unittest.TestCase):
    """F-55b's drop order, kept for the chip: NOW yields first, the track is a measurement
    (F-52b) that never shrinks, and the chip is the LAST thing to yield — whole, never
    clipped to `herd…`."""

    def _row(self, width, window=1000000, summary="NOW text here"):
        session = Session(harness="claude", pid=1, cwd="/x", liveness="working", ctx_pct=63,
                          context_window_tokens=window, summary=summary, herdr_attached=True)
        return render._context_detail_row(session, term_width=width)[0]

    def _lead_text(self, row):
        return row[1][0]

    def test_now_yields_before_the_chip(self):
        wide = "".join(v for v, _k in self._row(168))
        tight = "".join(v for v, _k in self._row(36))
        self.assertIn("NOW", wide)
        self.assertNotIn("NOW", tight)
        self.assertEqual(self._lead_text(self._row(36)), render._CTX_CHIP_TEXT)

    def test_the_chip_yields_whole_only_when_it_cannot_share_the_row(self):
        # 4 indent + 8 slot + 16 track + 4 value = 32 cells is the last width that fits.
        self.assertEqual(self._lead_text(self._row(32)), render._CTX_CHIP_TEXT)
        degraded = self._row(31)
        visible = "".join(v for v, _k in degraded)
        self.assertNotIn("herdr", visible)
        self.assertNotIn("herd", visible)
        # the gauge now follows the indent directly — no partial slot is left behind
        self.assertEqual(render._dw(degraded[0][0]), render._CONTEXT_INDENT_W)
        self.assertIn(degraded[1][0][0], (FULL, EMPTY))

    def test_a_short_track_keeps_the_chip_at_widths_a_full_track_could_not(self):
        self.assertEqual(self._lead_text(self._row(24, window=256000)), render._CTX_CHIP_TEXT)

    def test_the_chip_is_whole_or_absent_never_clipped(self):
        # The 16-cell track is a measurement that never shrinks (F-52b), so below 24
        # cells the row itself overflows — the contract under test is only that the chip
        # is whole or gone, never a clipped `herd…`.
        for width in range(10, 40):
            with self.subTest(width=width):
                visible = "".join(v for v, _k in self._row(width))
                self.assertNotIn("…", visible)
                if "herd" in visible:
                    self.assertIn(render._CTX_CHIP_TEXT, visible)
                if width >= 24:
                    self.assertLessEqual(render._dw(visible), width)


class F52WidthLedgerTest(unittest.TestCase):
    def test_left_anchor_is_untouched_and_the_wide_slack_ledger_holds(self):
        # F-52's left anchor is independent of every wide-row width edit and never moves.
        self.assertEqual(render._CONTEXT_INDENT_W, 4)
        # F-57 (v41) removed the dead `_CTX_W` (24) term from `fixed_row` and the `_CTX_BOOST`
        # (12) skim from the allocator; F-58 (v44) then narrowed `_HMW` 42→32, which shrinks
        # `_NAME_COL` — and therefore `fixed_row` — by another 10; F-64 (v49) widened `_HMW`
        # 32→35→38, taking 6 back; F-65 (v51) widened it once more to 40. Every slack
        # entry now loses 8 vs v44 and the name ladder shifts 8 widths right: 126 is the
        # last `_NW_S` floor width and the cap is first reached at 138 (was 130 at v44,
        # 140 at v41, 176 at v40).
        self.assertEqual([render._wide_slack(w) for w in (60, 120, 168, 200, 400)],
                         [-38, 22, 70, 102, 302])
        self.assertEqual([render._wide_name_width(w) for w in (126, 138, 168, 400)],
                         [28, 40, 40, 40])

    def test_row_starts_at_the_harness_name_column_and_fits_every_layout(self):
        session = Session(harness="claude", pid=1, cwd="/x", liveness="idle", ctx_pct=63,
                          context_window_tokens=1000000, summary="NOW", herdr_attached=True)
        for width in (168, 138, 120, 100, 60):
            with self.subTest(width=width):
                visible = "".join(v for v, _k in
                                  render._context_detail_row(session, term_width=width)[0])
                self.assertLessEqual(render._dw(visible), width)
                self.assertEqual(render._dw(visible[:visible.index(render._CTX_CHIP_TEXT)]),
                                 render._CONTEXT_INDENT_W)
                self.assertEqual(render._dw(visible[:visible.index("NOW")]), render._NAME_COL)

    def test_legend_gained_no_new_entry(self):
        """F-12(c): the lead cell is a STATE mark, already covered by the state legend — the
        legend line (last rendered row) gains no gauge item of its own."""
        session = Session(harness="claude", pid=1, cwd="/x", liveness="idle", ctx_pct=63,
                          slug="s", elapsed_min=1)
        lines = render._build_lines([session], [], "fleet", False, 0,
                                    layout="wide", term_width=168)
        legend = "".join(v for v, _k in [ln for ln in lines if ln][-1])
        self.assertIn("idle", legend)
        self.assertNotIn("context", legend)
        self.assertNotIn(FULL, legend)
        self.assertNotIn(EMPTY, legend)


if __name__ == "__main__":
    unittest.main()
