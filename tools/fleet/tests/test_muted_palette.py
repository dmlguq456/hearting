"""Muted Fleet foreground and panel palette stays calm and semantic."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render  # noqa: E402


class MutedPaletteTest(unittest.TestCase):
    def test_256_color_palette_is_low_chroma(self):
        self.assertEqual(render._MUTED_256, {
            "steward_pink": 219,
            "orange": 137,
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
            self.assertEqual(render._palette_fg("orange", 3), 137)
            self.assertEqual(render._palette_fg("steward_pink", 5), 219)
            self.assertNotEqual(render._palette_fg("steward_pink", 5), render._palette_fg("soft", 7))
            self.assertNotEqual(render._palette_fg("steward_pink", 5), render._palette_fg("yellow", 3))

    def test_low_color_terminal_keeps_native_fallback(self):
        with mock.patch.object(render.curses, "COLORS", 8, create=True):
            self.assertEqual(render._palette_fg("green", 2), 2)
            self.assertEqual(render._palette_fg("soft", 7), 7)

    def test_panel_tints_keep_brightness_and_reduce_chroma(self):
        self.assertEqual(render._TINT_LVL, {
            "b": 233, "c": 236, "B": 234, "C": 234, "k": 233, "i": 233,
        })
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
            233: (63, 71, 102),
            234: (108, 127, 188),
            236: (169, 182, 235),
        })
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

    def test_model_family_pairs_keep_default_background_after_all_pairs_init(self):
        """The header bars once re-initialized pairs 15-17, which by then held
        `fam_astra/luna/gpt`, so Astra text wore the chrome bar background.
        Check the final (fg, bg) of every family pair after the whole init,
        not the key names."""
        previous = dict(render._COLOR)
        old_tint = render._TINT_OK
        self.addCleanup(lambda: (render._COLOR.clear(), render._COLOR.update(previous)))
        self.addCleanup(setattr, render, "_TINT_OK", old_tint)
        families = {"opus": ("cyan", "COLOR_CYAN"), "sonnet": ("blue", "COLOR_BLUE"),
                    "haiku": ("green", "COLOR_GREEN"), "fable": ("magenta", "COLOR_MAGENTA"),
                    "sol": ("yellow", "COLOR_YELLOW"), "astra": ("orange", "COLOR_YELLOW"),
                    "luna": ("soft", "COLOR_WHITE"), "gpt": ("yellow", "COLOR_YELLOW")}
        for colors in (256, 8):
            with self.subTest(colors=colors):
                final = {}
                with mock.patch.multiple(
                    render.curses,
                    create=True,
                    COLORS=colors,
                    start_color=mock.Mock(),
                    use_default_colors=mock.Mock(),
                    can_change_color=mock.Mock(return_value=False),
                    init_pair=mock.Mock(side_effect=lambda n, fg, bg: final.__setitem__(n, (fg, bg))),
                    color_pair=mock.Mock(side_effect=lambda n: n << 8),
                ):
                    render._init_colors()
                    expected = {fam: render._palette_fg(name, getattr(render.curses, fallback))
                                for fam, (name, fallback) in families.items()}
                family_pairs = set()
                for fam, fg in expected.items():
                    pair = render._COLOR["fam_" + fam] >> 8
                    family_pairs.add(pair)
                    self.assertEqual(final[pair], (fg, -1), fam)
                    self.assertEqual(render._COLOR["famd_" + fam] >> 8 & 0xFF, pair, fam)
                self.assertEqual(len(family_pairs), len(families))
                structural = {render._COLOR[key] >> 8 & 0xFF
                              for key in ("hdr_bar", "hdr_warn", "hdr_blk", "tag_steward")}
                self.assertFalse(family_pairs & structural)
                # The bars keep their own background; only the family text lost it.
                self.assertEqual(final[render._COLOR["hdr_bar"] >> 8 & 0xFF][0],
                                 render.curses.COLOR_BLACK)
                self.assertNotEqual(final[render._COLOR["hdr_bar"] >> 8 & 0xFF][1], -1)
                self.assertTrue(all(pair < 64 for pair in family_pairs | structural))
                if colors == 256:
                    tint_pairs = {value >> 8 for value in render._TINT_PAIR.values()}
                    self.assertTrue(render._TINT_OK)
                    self.assertFalse(tint_pairs & (family_pairs | structural))

    def test_semantic_hues_and_soft_focal_text_are_preserved(self):
        self.assertEqual(render._HUE_OF["g_work"][0], "g")
        self.assertEqual(render._HUE_OF["g_idle"][0], "y")
        self.assertEqual(render._HUE_OF["g_dead"][0], "r")
        badge_hues = [render._HUE_OF[key][0] for key in render._BADGE_KEY.values()]
        self.assertEqual(badge_hues, ["c", "m", "l"])
        # F-76b: the NAME zone moved onto the harness hue (own `nm_*` keys, same colors
        # as the badges above) so a row reads as one color system; only an unknown
        # harness keeps the historical soft white.
        name_hues = [render._HUE_OF[render._NAME_KEY[h]][0]
                     for h in ("claude", "codex", "opencode")]
        self.assertEqual(name_hues, ["c", "m", "l"])
        self.assertEqual(render._HUE_OF["nm_other"][0], "w")


if __name__ == "__main__":
    unittest.main()
