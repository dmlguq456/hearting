"""F-60 (v44) — `blocked` is red, on its own key, and its badge is a reverse-video chip.

The decision is a DISPLAY-layer one: `_LIVE_RANK`, the sort, the state classifier and the
F-48/F-49 evidence that decides a session IS blocked are all untouched, and this suite asserts
that separation as much as it asserts the new colors.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render                                       # noqa: E402
from fleet.model import Session                                # noqa: E402


def _blocked(kind="decision", **kw):
    return Session(harness="codex", pid=10, cwd="/work/project", session_id="sid",
                   slug="project", liveness="blocked", elapsed_min=3, branch="main",
                   interaction_state={"kind": kind, "source": "codex-rollout",
                                      "waiting_since": 100.0}, **kw)


class BlockedKeyTest(unittest.TestCase):
    def test_blocked_has_its_own_key_shared_by_every_surface(self):
        """One key, three places: the harness-row glyph, the legend and (in its reverse
        variant) the interaction badge. (The context lead word was the fourth until F-100b
        retired the state word from that row.)"""
        self.assertEqual(render._GLYPH_KEY["blocked"], "g_blocked")
        self.assertEqual(render._state_key("blocked"), "g_blocked")
        self.assertEqual(render._glyph("blocked")[1], "g_blocked")

    def test_the_key_is_separate_from_both_idle_and_dead(self):
        """Red is shared with `dead`; the KEY is not, so either can be retuned alone. And it
        is off `g_idle` entirely — that was the whole complaint F-60 answers. v47 retuned
        exactly that way: same red hue, but blocked dropped its bold while dead kept it."""
        self.assertNotIn(render._GLYPH_KEY["blocked"], ("g_idle", "g_dead"))
        self.assertEqual(render._HUE_OF["g_blocked"][0], "r")
        self.assertEqual(render._HUE_OF["g_blocked"][0], render._HUE_OF["g_dead"][0])
        self.assertNotEqual(render._GLYPH_KEY["blocked"], render._GLYPH_KEY["dead"])

    def test_blocked_carries_no_bold_anywhere(self):
        """v47 (user 2026-08-05, "blocked가 볼드더라"): the chip owns the emphasis, so the
        glyph, the lead word and even the chip itself stay off A_BOLD."""
        for key in ("g_blocked", "g_blocked_chip"):
            self.assertFalse(render._HUE_OF[key][1] & render._A_BOLD, key)

    def test_the_chip_reverses_and_the_glyph_and_word_do_not(self):
        """Emphasis lives in exactly one cell run. Two reversed runs on a row cancel out."""
        self.assertTrue(render._HUE_OF["g_blocked_chip"][1] & render._A_REVERSE)
        for key in ("g_blocked", "g_dead", "g_idle"):
            self.assertFalse(render._HUE_OF[key][1] & render._A_REVERSE, key)

    def test_no_blink_anywhere_on_the_blocked_path(self):
        """The working dot's manual 2 Hz toggle already owns "in progress"; blocked is a
        static wait for input, so it must not animate."""
        blink = getattr(render.curses, "A_BLINK", None) if render.curses else None
        if blink:
            for key in ("g_blocked", "g_blocked_chip"):
                self.assertFalse(render._HUE_OF[key][1] & blink, key)
        segs = render._session_row(_blocked(), narrow=False, name_width=40)
        self.assertNotIn("_BLINK", repr(segs))


class BlockedRowTest(unittest.TestCase):
    def _keys(self, segs):
        return [k for _v, k in segs]

    def test_wide_row_carries_one_chip_and_a_red_glyph(self):
        segs = render._session_row(_blocked("approval"), narrow=False, name_width=40)
        self.assertEqual(self._keys(segs).count("g_blocked_chip"), 1)
        chip = [v for v, k in segs if k == "g_blocked_chip"][0]
        self.assertEqual(chip, " approval ")            # padded INSIDE the reversed run
        glyph_keys = [k for v, k in segs if v == render._LIVE_GLYPH["blocked"]]
        self.assertEqual(glyph_keys, ["g_blocked"])

    def test_narrow_and_stack_rows_carry_the_same_chip(self):
        for row in (render._session_row_2line(_blocked("permission"), term_width=100)[0],
                    render._session_row_stack(_blocked("permission"), term_width=60)[0]):
            self.assertIn(" approval ", [v for v, k in row if k == "g_blocked_chip"])

    def test_the_separator_space_stays_outside_the_reversed_run(self):
        """Otherwise the inverted block runs straight into the title with no gutter."""
        segs = render._interaction_chip(_blocked())[0]
        self.assertEqual(segs[0], (" ", None))
        self.assertEqual(segs[1][1], "g_blocked_chip")

    def test_chip_width_is_reported_exactly(self):
        segs, width = render._interaction_chip(_blocked("elicitation"))
        self.assertEqual(width, sum(render._dw(v) for v, _k in segs))

    def test_a_session_with_no_interaction_kind_gets_no_chip(self):
        session = Session(harness="codex", pid=10, cwd="/x", liveness="blocked", branch="main")
        self.assertEqual(render._interaction_chip(session), ([], 0))
        segs = render._session_row(session, narrow=False, name_width=40)
        self.assertNotIn("g_blocked_chip", self._keys(segs))
        self.assertIn("g_blocked", self._keys(segs))          # the glyph is still red

    def test_context_row_carries_no_second_blocked_mark(self):
        """F-100b retired the context row's state word: the L1 glyph (+ chip) is the one
        place `blocked` is named, so the context row shows neither the word nor a second
        red chip — its lead slot is the WHERE chip or blank."""
        row = render._context_detail_row(_blocked(ctx_pct=40), term_width=168)[0]
        visible = "".join(v for v, _k in row)
        self.assertNotIn("blocked", visible)
        self.assertNotIn("g_blocked_chip", [k for _v, k in row])
        self.assertNotIn("g_blocked", [k for _v, k in row])
        self.assertEqual(row[1], (" " * render._CTX_LABEL_W, None))


class BlockedVersusDeadTest(unittest.TestCase):
    """Red is now shared, so the two states must separate on shape and emphasis as well —
    including on a terminal with no color at all, where every key collapses to attributes."""

    def test_the_glyphs_differ(self):
        self.assertNotEqual(render._LIVE_GLYPH["blocked"], render._LIVE_GLYPH["dead"])

    def test_only_blocked_gets_a_reverse_chip(self):
        blocked = render._session_row(_blocked(), narrow=False, name_width=40)
        dead = render._session_row(
            Session(harness="codex", pid=11, cwd="/x", liveness="dead", branch="main"),
            narrow=False, name_width=40)
        self.assertIn("g_blocked_chip", [k for _v, k in blocked])
        self.assertNotIn("g_blocked_chip", [k for _v, k in dead])

    def test_without_color_the_chip_is_still_the_only_reversed_run(self):
        """v47: with blocked's bold gone, the two glyph keys separate on attributes alone —
        blocked reads plain, dead reads bold — and the chip still carries REVERSE as the one
        emphasis. This reads the decomposition `_HUE_OF` publishes rather than restating the
        composition."""
        no_color = lambda key: render._HUE_OF[key][1]      # hue dropped = no color available
        self.assertNotEqual(no_color("g_blocked"), no_color("g_dead"))
        self.assertTrue(no_color("g_dead") & render._A_BOLD)
        self.assertFalse(no_color("g_blocked") & render._A_BOLD)
        self.assertTrue(no_color("g_blocked_chip") & render._A_REVERSE)

    def test_legend_uses_the_new_key(self):
        path = os.path.join(os.path.dirname(__file__), "..", "render.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('legend += [("◑", "g_blocked")', source)
        self.assertNotIn('"g_idle"), (" blocked', source)


class BlockedClassifierIsUntouchedTest(unittest.TestCase):
    def test_rank_and_ordering_vocabulary_are_display_independent(self):
        """F-60 is a display decision — it must not have moved blocked in the live order.
        Colour went red; the RANK stays where F-48/F-49 put it, between idle and done."""
        self.assertEqual(render._LIVE_RANK["blocked"], 2)
        self.assertLess(render._LIVE_RANK["idle"], render._LIVE_RANK["blocked"])
        self.assertLess(render._LIVE_RANK["blocked"], render._LIVE_RANK["done"])
        self.assertLess(render._LIVE_RANK["blocked"], render._LIVE_RANK["dead"])


if __name__ == "__main__":
    unittest.main()
