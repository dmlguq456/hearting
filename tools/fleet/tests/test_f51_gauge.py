import unittest
import os
import sys
import json
import re
from unittest import mock

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
                          context=ContextProjection(50, "normal", "x"), summary="NOW")
        snapshots = {"claude": {"payload": {"rl_5h": 50, "rl_7d": 50, "rl_ms": []},
                                "freshness": "fresh", "observed_at": 1000}}
        for width, layout in ((168, "wide"), (138, "wide"), (100, "narrow"),
                              (60, "stack")):
            with self.subTest(width=width, layout=layout):
                row = render._context_detail_row(session, term_width=width)
                visible = "".join(value for value, _key in row[0])
                self.assertEqual(render._dw(visible[:re.search(r"(?:\d+%|—) +(\S)", visible).start(1)]), render._NAME_COL)
                self.assertLessEqual(render._dw(visible), width)
                headers = render._usage_header_rows([session], layout=layout, now=1000,
                                                    usage_snapshots=snapshots)
                self.assertTrue(headers)
                self.assertTrue(any(FULL in value or EMPTY in value
                                    for value, _key in headers[0]))

    def test_header_row_is_layout_independent_and_keeps_the_meter_shape(self):
        """Without an explicit viewport, layouts share the same aligned header.

        Normalizing 7d to week adds two cells to the historical 69-cell row;
        the twelve-cell gauge and its quantization remain unchanged.
        """
        session = Session(harness="claude", pid=1, cwd="/x", liveness="idle")
        snapshots = {"claude": {"payload": {"rl_5h": 92, "rl_7d": 30, "rl_ms": []},
                                "freshness": "fresh", "observed_at": 1000}}
        texts = set()
        for layout in ("wide", "narrow", "stack"):
            rows = render._usage_header_rows([session], layout=layout, now=1000,
                                             usage_snapshots=snapshots)
            self.assertTrue(rows)
            texts.add("".join(v for row in rows for v, _k in row))
        # plain (--once) path: no term_width constraint and no layout at all — same row.
        texts.add("".join(v for row in render._usage_header_rows(
            [session], now=1000, usage_snapshots=snapshots) for v, _k in row))
        self.assertEqual(len(texts), 1, "usage header must not vary by layout")
        text = texts.pop()
        n_meters = text.count("[")
        self.assertEqual(n_meters, 2)
        self.assertEqual(render._dw(text), 47 + n_meters * render._GAUGE_W)
        self.assertIn("[" + render._BAR_FULL * 11 + render._BAR_EMPTY, text)

    def test_usage_slots_align_across_accounts_in_plain_and_curses(self):
        now = 1000
        snapshots = {
            "claude": {"payload": {
                "rl_5h": 12, "rl_7d": 59, "rs_5h": now + 175 * 60,
                "rs_7d": now + 8639 * 60, "rl_ms": [["fable", 0]]},
                "freshness": "fresh", "observed_at": now},
            "codex": {"payload": {
                "windows": [["7d", 12, now + 9738 * 60]]}},
            "opencode": {"payload": {
                "windows": [["5h", 0, now + 281 * 60],
                            ["wk", 74, now + 6675 * 60],
                            ["mo", 70, now + 29959 * 60]]}},
        }

        class Screen:
            def __init__(self):
                self.calls = []

            def addstr(self, row, col, text, attr):
                self.calls.append((col, text))

        for width in (168, 100, 60):
            with self.subTest(width=width), mock.patch.object(render.time, "time", return_value=now):
                lines = render._build_lines([], [], "fleet", False, 0,
                                            layout=render._layout_mode(width), term_width=width,
                                            usage_snapshots=snapshots)
                rows = [line for line in lines if line and
                        any(key in ("hb_claude", "hb_codex", "hb_opencode") for _t, key in line)]
                self.assertEqual(len(rows), 3)
                visible = [render._snapshot_line(row) for row in rows]
                self.assertTrue(all(render._dw(row) <= width for row in visible))
                self.assertEqual(visible[0].index("5h "), visible[2].index("5h "))
                self.assertNotIn("5h", visible[1])
                week_cols = [render._dw(row[:row.index("week ")])
                             for row in visible if "week " in row]
                self.assertLessEqual(len(set(week_cols)), 1)
                if width >= 100:
                    self.assertEqual(len(week_cols), 3)
                    self.assertIn("↻ 2h 55m", visible[0])
                    self.assertIn("↻ 6d 18h", visible[1])
                if width == 168:
                    self.assertEqual(render._dw(visible[0].split("fable ", 1)[0]),
                                     render._dw(visible[2].split("month ", 1)[0]))
                    self.assertIn("↻ 20d 19h", visible[2])
                    self.assertIn("[" + FULL + EMPTY * 11 + "  12%]", visible[0])
                self.assertNotRegex("\n".join(visible), r"\b(?:7d|wk|mo) \[")
                for row, plain in zip(rows, visible):
                    screen = Screen()
                    render._addline(screen, 0, row, width)
                    self.assertEqual("".join(part for _col, part in screen.calls),
                                     render._clip_w(plain, width - 1, ellipsis=""))
                    self.assertEqual(render._snapshot_line(row, colored=True), plain)

    def test_stale_window_keeps_dotted_track_but_preserves_fill_and_pct_colors(self):
        """A5: a stale usage window shows an empty `·` track while the fill segment and the
        percent number both keep their `_pct_key` LEVEL (92% red, 30% green) — stale means
        "don't trust the freshness", not "hide the level".

        F-61 renders the header's red without the alarm bold, so the key is `lvl_r_flat`; the
        threshold that chose red is unchanged and every non-header surface still gets `lvl_r`.
        """
        snapshots = {"claude": {"payload": {"rl_5h": 92, "rl_7d": 30, "rl_ms": []},
                                "freshness": "stale", "observed_at": 1000}}
        row = render._usage_header_rows([], layout="wide", now=1200,
                                       usage_snapshots=snapshots)[0]
        visible = "".join(v for v, _k in row)
        tracks = re.findall(r"\[([%s·]+)" % re.escape(FULL), visible)
        self.assertEqual([len(track) for track in tracks], [render._GAUGE_W] * 2)
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

    def test_unknown_window_is_status_without_an_invented_gauge(self):
        """Unknown account quota stays distinct from an admitted, measured zero."""
        session = Session(harness="claude", pid=1, cwd="/x", liveness="idle",
                          rl_5h=92, rl_7d=30, mtime=1000)
        snapshots = {"claude": {"payload": None, "freshness": "unknown",
                                "observed_at": None, "error_observation": None}}
        row = render._usage_header_rows([session], layout="wide", now=1000,
                                       usage_snapshots=snapshots)[0]
        joined = "".join(v for v, _k in row)
        self.assertIn("claude code", joined)
        self.assertIn("unavailable", joined)
        self.assertNotIn("[", joined)
        self.assertNotIn("%", joined)
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

    def test_empty_account_cache_shows_status_without_inventing_quota(self):
        snapshots = {
            "claude": {
                "payload": {"rl_5h": None, "rl_7d": None, "rl_ms": []},
                "freshness": "unknown",
                "observed_at": None,
            },
        }
        rows = render._usage_header_rows([], now=1000, usage_snapshots=snapshots)
        self.assertEqual(len(rows), 1)
        self.assertEqual("".join(v for v, _key in rows[0]), "  usage claude code   unavailable")

    def test_codex_week_label_keeps_its_runtime_window_and_blank_5h_slot(self):
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
        self.assertIn("week", visible)
        self.assertNotIn("7d", visible)
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
        snapshots = {"claude": {"payload": {"rl_5h": pct, "rl_7d": pct, "rl_ms": []},
                                "freshness": "fresh", "observed_at": 1000}}
        return render._usage_header_rows([], layout="wide", now=1000,
                                        usage_snapshots=snapshots)[0]

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
