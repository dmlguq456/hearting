import unittest
import os
import sys
import json
import re

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render
from fleet.model import ContextProjection, Session


FULL, EMPTY = render._BAR_FULL, render._BAR_EMPTY


class F51GaugeTest(unittest.TestCase):
    def test_fixed_width_and_half_up(self):
        """The legacy `width` argument never sizes the meter — `_GAUGE_W` cells regardless
        (F-52b keeps the usage side fixed and sizes only the context track, via `track=`).
        F-59 widened that constant 6→12 without touching this contract."""
        W = render._GAUGE_W
        self.assertEqual("".join(x[0] for x in render._gauge_segs(75, 99)),
                         FULL * 9 + EMPTY * 3)
        for pct in (None, 0, -1):
            self.assertEqual("".join(x[0] for x in render._gauge_segs(pct, 1)), EMPTY * W)
        self.assertEqual("".join(x[0] for x in render._gauge_segs(100, 1)), FULL * W)

    def test_acceptance_quantization_table_and_pct_boundaries(self):
        """F-59 acceptance table on the 12-cell meter. Same half-up rule as the 6-cell one:
        `None`/<=0 → 0, `>=100` → 12, otherwise `clamp(half_up(pct*12/100), 1, 11)` — so 0
        and 12 stay reserved for exactly "none" and "full". 12/13 and 87/88 are the first two
        rung boundaries the wider meter newly resolves (both were a single step at 6)."""
        expected = {None: 0, 0: 0, 1: 1, 8: 1, 12: 1, 13: 2, 16: 2, 50: 6,
                    75: 9, 84: 10, 87: 10, 88: 11, 92: 11, 99: 11, 100: 12, 150: 12}
        for pct, filled in expected.items():
            with self.subTest(pct=pct):
                segs = render._gauge_segs(pct, 6)
                self.assertEqual(sum(len(value) for value, _key in segs), render._GAUGE_W)
                self.assertEqual(sum(len(value) for value, _key in segs
                                     if FULL in value), filled)
        self.assertEqual(render._pct_key(50), render._pct_key(50.0))
        self.assertEqual(render._pct_key(80), render._pct_key(80.0))

    def test_width_shim_is_constant(self):
        self.assertEqual({render._compact_context_gauge_width(w, depth=d)
                          for w in (20, 40, 60, 100, 138, 168, 200, 400)
                          for d in (0, 1, 2)}, {render._GAUGE_W})

    def test_unknown_and_zero_values_are_distinct(self):
        text = lambda pct: "".join(x[0] for x in render._context_detail_row(
            type("E", (), {"liveness": "working", "ctx_pct": pct, "summary": None})())[0])
        self.assertIn("—", text(None))
        self.assertIn("0%", text(0))

    def test_context_now_anchor_and_widths(self):
        session = Session(harness="claude", pid=1, cwd="/x", liveness="working",
                          context=ContextProjection(50, "normal", "x"), summary="NOW",
                          rl_5h=50, rl_7d=50)
        for width, layout in ((168, "wide"), (138, "wide"), (100, "narrow"),
                              (60, "stack")):
            with self.subTest(width=width, layout=layout):
                row = render._context_detail_row(session, term_width=width)
                visible = "".join(value for value, _key in row[0])
                self.assertEqual(render._dw(visible[:visible.index("NOW")]), render._NAME_COL)
                self.assertLessEqual(render._dw(visible), width)
                headers = render._usage_header_rows([session], layout=layout)
                if headers:
                    self.assertTrue(any(FULL in value or EMPTY in value or "·" in value
                                        for value, _key in headers[0]))

    def test_header_row_is_layout_independent_and_costs_six_more_cells_per_meter(self):
        """A3, re-stated for F-59. The old form asserted "never wider than the pre-F51 8/12
        cell gauge"; at `_GAUGE_W`=12 that bound holds only with equality, so it no longer
        asserts anything and is retired.

        What is worth locking in instead is the shape of the surface. `_usage_header_rows`
        accepts `layout` but never reads it and is never handed a terminal width, so there is
        no drop or abbreviate ladder here at all: the row is byte-identical in wide, narrow
        and stack, and each meter simply costs six more cells than it did at `_GAUGE_W`=6.
        A two-window claude row measures 69 cells (was 57) — inside every wide/narrow
        terminal, past the 60-column stack layout, where `_addline` clips it at the right
        edge. F-59 deliberately adds no rule for that (see the cycle report)."""
        session = Session(harness="claude", pid=1, cwd="/x", liveness="idle",
                          rl_5h=92, rl_7d=30, mtime=1000)
        texts = set()
        for layout in ("wide", "narrow", "stack"):
            rows = render._usage_header_rows([session], layout=layout)
            self.assertTrue(rows)
            texts.add("".join(v for row in rows for v, _k in row))
        # plain (--once) path: no term_width constraint and no layout at all — same row.
        texts.add("".join(v for row in render._usage_header_rows([session]) for v, _k in row))
        self.assertEqual(len(texts), 1, "usage header must not vary by layout")
        text = texts.pop()
        n_meters = text.count("[")
        self.assertEqual(n_meters, 2)
        self.assertEqual(render._dw(text), 45 + n_meters * render._GAUGE_W)
        self.assertIn("[" + render._BAR_FULL * 11 + render._BAR_EMPTY, text)

    def test_stale_window_keeps_dotted_track_but_preserves_fill_and_pct_colors(self):
        """A5: a stale usage window shows an empty `·` track while the fill segment and the
        percent number both keep their `_pct_key` LEVEL (92% red, 30% green) — stale means
        "don't trust the freshness", not "hide the level".

        F-61 renders the header's red without the alarm bold, so the key is `lvl_r_flat`; the
        threshold that chose red is unchanged and every non-header surface still gets `lvl_r`.
        """
        session = Session(harness="claude", pid=1, cwd="/x", liveness="idle",
                          rl_5h=92, rl_7d=30, mtime=1000)
        session._usage_freshness = "stale"
        row = render._usage_header_rows([session], layout="wide")[0]
        visible = "".join(v for v, _k in row)
        self.assertTrue(all(len(track) == render._GAUGE_W
                            for track in re.findall(r"[%s·]+" % re.escape(FULL), visible)))
        fill_keys = {k for v, k in row if render._BAR_FULL in v}
        self.assertIn("lvl_r_flat", fill_keys)
        self.assertIn("lvl_g", fill_keys)
        dotted_tracks = [v for v, k in row if k == "dim" and v and set(v) == {"·"}]
        self.assertTrue(dotted_tracks)
        # 92% → 11 filled + 1 dotted, 30% → 4 filled + 8 dotted (was [1, 4] at _GAUGE_W=6).
        self.assertEqual(sorted(map(len, dotted_tracks)), [1, 8])
        pct_keys = {k for v, k in row if "%" in v}
        self.assertIn("lvl_r_flat", pct_keys)
        self.assertIn("lvl_g", pct_keys)

    def test_unknown_window_is_dotted_track_and_em_dash(self):
        """A5: an unknown usage window (no cached value at all) shows `·`×6 track plus a bare
        `—`, distinct from both the stale window above and the 0% filled-track case."""
        session = Session(harness="claude", pid=1, cwd="/x", liveness="idle",
                          rl_5h=None, rl_7d=None, mtime=1000)
        session._usage_freshness = "unknown"
        row = render._usage_header_rows([session], layout="wide")[0]
        joined = "".join(v for v, _k in row)
        self.assertIn("·" * render._GAUGE_W, joined)
        self.assertIn("—", joined)
        self.assertNotIn(render._BAR_FULL, joined)

    def test_account_cache_renders_without_a_session_row(self):
        snapshots = {
            "claude": {
                "payload": {"rl_5h": 31, "rl_7d": 81},
                "freshness": "fresh",
                "observed_at": 1000,
            },
        }
        rows = render._usage_header_rows([], now=1000, usage_snapshots=snapshots)
        visible = "\n".join("".join(value for value, _key in row) for row in rows)
        self.assertIn("claude code", visible)
        self.assertIn("31%", visible)
        self.assertIn("81%", visible)
        self.assertNotIn("no usage api", visible)

    def test_empty_account_cache_does_not_invent_a_usage_row(self):
        snapshots = {
            "claude": {
                "payload": {},
                "freshness": "unknown",
                "observed_at": None,
            },
        }
        self.assertEqual([], render._usage_header_rows([], usage_snapshots=snapshots))

    def test_codex_account_window_keeps_its_runtime_duration_label(self):
        snapshots = {
            "codex": {
                "payload": {
                    "rl_5h": None,
                    "rl_7d": 48,
                    "windows": [["7d", 48, 2000]],
                },
                "freshness": "fresh",
                "observed_at": 1000,
            },
        }
        rows = render._usage_header_rows([], now=1000, usage_snapshots=snapshots)
        visible = "\n".join("".join(value for value, _key in row) for row in rows)
        self.assertIn("codex", visible)
        self.assertIn("7d", visible)
        self.assertIn("48%", visible)
        self.assertNotIn("5h", visible)

    def test_wide_ledger_matches_the_frozen_fixture_exactly(self):
        """A8: `f51_wide_ledger_v49.json` records `_wide_slack`/`_wide_name_width` for every
        terminal width 60..400 — recompute both and diff against the frozen ledger so a
        future edit to the wide slack ladder cannot silently regress without this fixture
        failing.

        Re-frozen for F-65 (v51; filename retained for compatibility; originally renamed
        for F-64 from `..._v44.json` ← `..._v41.json` ←
        `..._v40.json` ← `..._v38.json` ← `..._v35.json`; frozen twice within v49 as the
        user widened `_HMW` 32→35→38, then F-65 widened 38→40). Like F-58, each
        step moves ONE constant, so each freeze is a single exact TRANSLATION of the
        previous ledger, proved before the values were written (net vs v44 below):

          * `wide_slack` — `_HMW` only enters through `_NAME_COL` in `fixed_row`, which
            grows by exactly 8 net, so every entry loses 8 cells: `v51[w] == v44[w] - 8`
            for all w in 60..400 (each 3-cell step verified 341/341 at its re-freeze).
            A VERTICAL shift.
          * `wide_name_width` — the same 8 cells reach the title 8 columns later:
            `v51[w] == v44[w-8]` for all w in 68..400, and
            the low end sits on the `_NW_S` floor either way. A HORIZONTAL shift, so the
            ladder's SHAPE is unchanged — same `_NW_S` floor (28), same `_NAME_WIDE_MAX`
            cap (40), still monotonic. Only the width at which each rung is reached moved:
            the 40-col name cap is now first reached at 138 cols (was 130 at v44)."""
        path = os.path.join(os.path.dirname(__file__), "fixtures", "f51_wide_ledger_v49.json")
        with open(path, encoding="utf-8") as fh:
            ledger = json.load(fh)
        self.assertEqual(len(ledger), 341)
        for w in range(60, 401):
            expected = ledger[str(w)]
            with self.subTest(w=w):
                self.assertEqual(render._wide_name_width(w), expected["wide_name_width"])
                self.assertEqual(render._wide_slack(w), expected["wide_slack"])

    def test_gauge_surfaces_never_emit_tilde(self):
        session = Session(harness="claude", pid=1, cwd="/x", liveness="working",
                          ctx_pct=50, summary="NOW", branch="main")
        surfaces = [render._gauge_segs(50, 99),
                    render._context_detail_row(session),
                    render._usage_header_rows([session]),
                    render._branch_suffix_segs("/tmp", "main")]
        for surface in surfaces:
            with self.subTest(surface=surface):
                self.assertNotIn("~", repr(surface))


if __name__ == "__main__":
    unittest.main()


class F61UsageBoldTest(unittest.TestCase):
    """F-61: the usage header drops the alarm bold; no other `lvl_r` surface does."""

    def _header(self, pct):
        session = Session(harness="claude", pid=1, cwd="/x", liveness="idle",
                          rl_5h=pct, rl_7d=pct, mtime=1000)
        return render._usage_header_rows([session], layout="wide")[0]

    def test_header_never_emits_the_bold_level_key(self):
        for pct in (0, 30, 49, 50, 79, 80, 92, 100):
            with self.subTest(pct=pct):
                self.assertNotIn("lvl_r", [k for _v, k in self._header(pct)])

    def test_header_red_stays_red_at_the_same_threshold(self):
        self.assertNotIn("lvl_r_flat", [k for _v, k in self._header(79)])
        self.assertIn("lvl_r_flat", [k for _v, k in self._header(80)])

    def test_flat_key_is_the_same_hue_without_the_weight(self):
        hue, attr = render._HUE_OF["lvl_r_flat"]
        self.assertEqual(hue, render._HUE_OF["lvl_r"][0])
        self.assertEqual(attr, 0)
        self.assertNotEqual(render._HUE_OF["lvl_r"][1], 0)

    def test_other_surfaces_keep_the_alarm_weight(self):
        """The context gauge shares `_pct_key`; only the header maps onto the flat key."""
        session = Session(harness="claude", pid=1, cwd="/x", liveness="idle", ctx_pct=92,
                          context_window_tokens=1000000)
        keys = [k for _v, k in render._context_detail_row(session, term_width=168)[0]]
        self.assertIn("lvl_r", keys)
        self.assertNotIn("lvl_r_flat", keys)

    def test_flat_level_only_renames_the_red(self):
        self.assertEqual(render._flat_level("lvl_r"), "lvl_r_flat")
        for key in ("lvl_y", "lvl_g", "dim"):
            self.assertEqual(render._flat_level(key), key)
