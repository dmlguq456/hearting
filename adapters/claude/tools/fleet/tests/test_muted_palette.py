"""Muted Fleet foreground and panel palette stays calm and semantic."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render  # noqa: E402


class MutedPaletteTest(unittest.TestCase):
    def test_256_color_palette_is_low_chroma(self):
        self.assertEqual(render._MUTED_256, {
            "soft": 253,
            "green": 150,
            "yellow": 186,
            "red": 217,
            "cyan": 116,
            "magenta": 176,
            "blue": 147,
            "vanilla": 230,
            "chrome": 252,
            "warning": 131,
        })
        with mock.patch.object(render.curses, "COLORS", 256, create=True):
            self.assertEqual(render._palette_fg("green", 2), 150)
            self.assertEqual(render._palette_fg("blue", 4), 147)

    def test_low_color_terminal_keeps_native_fallback(self):
        with mock.patch.object(render.curses, "COLORS", 8, create=True):
            self.assertEqual(render._palette_fg("green", 2), 2)
            self.assertEqual(render._palette_fg("soft", 7), 7)

    def test_panel_tints_keep_brightness_and_reduce_chroma(self):
        self.assertEqual(render._TINT_LVL, {
            "b": 233, "c": 236, "B": 234, "C": 234, "k": 233, "i": 233,
            # card interior (user 2026-08-14 "박스 테두리 내부는 틴트를 어둡게"):
            # one step BELOW every body level, so a boxed unit reads as a recess.
            "x": 232,
        })
        self.assertLess(render._TINT_LVL["x"], min(
            render._TINT_LVL[k] for k in ("b", "B", "k", "i")))
        self.assertEqual(render._TINT_LVL["b"], render._TINT_LVL["i"])
        self.assertNotIn(60, render._TINT_LVL.values())
        self.assertNotIn(95, render._TINT_LVL.values())

    def test_changeable_palette_nudges_foreground_saturation_and_panel_depth(self):
        self.assertEqual(render._RICHER_RGB_1000, {
            "green": (678, 859, 506),
            "yellow": (859, 859, 506),
            "red": (1000, 663, 663),
            "cyan": (506, 859, 859),
            "magenta": (859, 506, 859),
            "blue": (663, 663, 1000),
        })
        self.assertEqual(render._TINT_RGB_1000, {
            232: (38, 43, 61),
            233: (63, 71, 102),
            234: (108, 127, 188),
            236: (169, 182, 235),
        })
        # The card level continues the SAME slate ramp downward — it must not fall
        # back to xterm's neutral grey, which would read as a different material.
        for chan in range(3):
            self.assertLess(render._TINT_RGB_1000[232][chan],
                            render._TINT_RGB_1000[233][chan])
        init_color = mock.Mock()
        old_tint = render._TINT_OK
        try:
            with mock.patch.multiple(
                render.curses,
                create=True,
                COLORS=256,
                start_color=mock.Mock(),
                use_default_colors=mock.Mock(),
                init_pair=mock.Mock(),
                color_pair=mock.Mock(return_value=0),
                can_change_color=mock.Mock(return_value=True),
                init_color=init_color,
            ):
                render._init_colors()
        finally:
            render._TINT_OK = old_tint
        expected = [
            mock.call(render._MUTED_256[name], *rgb)
            for name, rgb in render._RICHER_RGB_1000.items()
        ]
        expected.extend(
            mock.call(index, *rgb)
            for index, rgb in render._TINT_RGB_1000.items()
        )
        self.assertEqual(init_color.call_args_list, expected)

    def test_semantic_hues_and_soft_focal_text_are_preserved(self):
        self.assertEqual(render._HUE_OF["g_work"][0], "g")
        self.assertEqual(render._HUE_OF["g_idle"][0], "y")
        self.assertEqual(render._HUE_OF["g_dead"][0], "r")
        badge_hues = [render._HUE_OF[key][0] for key in render._BADGE_KEY.values()]
        self.assertEqual(badge_hues, ["c", "m", "l"])
        self.assertEqual(render._HUE_OF["name_work"][0], "w")


if __name__ == "__main__":
    unittest.main()
