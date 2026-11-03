"""The explorer paints a frame in runs of one attribute, cell for cell as before.

The user's explorer spent about half a core on its main thread while idle (2026-11-04):
every 100 ms tick wrote each of the screen's ~10k cells with its own ``addstr`` and asked the
cell width of each character through a per-call import. Painting is now one ``addstr`` per
run of cells that share an attribute, and character widths are remembered; the cells that
reach the terminal must not change.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render                                             # noqa: E402
from tools.fleet.explorer import grid, output                        # noqa: E402
from tools.fleet.explorer.frame import draw                          # noqa: E402
from tools.fleet.explorer.state import NavState                      # noqa: E402
from tools.fleet.tests import explorer_fixture as ef                 # noqa: E402


class Screen:
    """A curses window stand-in that keeps the cells it was given."""

    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.cells = {}
        self.calls = 0

    def getmaxyx(self):
        return self.rows, self.cols

    def erase(self):
        self.cells.clear()

    def addstr(self, y, x, text, attr=0):
        self.calls += 1
        for char in text:
            self.cells[(y, x)] = (char, attr)
            x += max(1, output.cell_width(char))

    def refresh(self):
        pass


def per_cell(screen, frame, stale):
    """The painting before this change, one addstr per cell, as the reference."""
    spin = render._SPIN[0]
    for y, row in enumerate(frame.cells[:screen.rows]):
        for x, (glyph, key, tint) in enumerate(row[:screen.cols]):
            if glyph == "":
                continue
            if not stale and glyph == "⠏" and key in render._EXPLORER_SPIN_KEYS:
                glyph = spin
            attr = render._key_attr(key, tint)
            if stale:
                attr |= getattr(render.curses, "A_DIM", 0)
            screen.addstr(y, x, glyph, attr)


class DrawRunsTest(unittest.TestCase):
    def test_runs_paint_the_same_cells_with_far_fewer_writes(self):
        model = ef.demo_model(detail=True)
        states = (NavState(), NavState(view="ROUTES", title="ROUTES"),
                  NavState(view="PEERS", title="PEERS"), NavState(view="HISTORY", title="HISTORY"))
        with mock.patch.object(render, "_explorer_hit_geometry"), \
             mock.patch.object(render.time, "time", return_value=0.0), \
             mock.patch.object(render, "_key_attr", side_effect=lambda key, tint: hash((key, tint)) & 0xffff):
            for state in states:
                for cols, rows in ((140, 45), (200, 60)):
                    for stale in (False, True):
                        with self.subTest(view=state.view, cols=cols, stale=stale):
                            frame = draw(model, state, cols, rows, beat=1)
                            reference, runs = Screen(rows, cols), Screen(rows, cols)
                            per_cell(reference, frame, stale)
                            render._draw_explorer_frame(runs, frame, None, stale=stale)
                            self.assertEqual(runs.cells, reference.cells)
                            self.assertLess(runs.calls * 4, reference.calls)

    def test_character_widths_are_unchanged_and_remembered(self):
        for text in ("a", "가", "⏳", "⠏", "é", "​", "abc가나"):
            self.assertEqual(grid.cell_width(text), output.cell_width(text), text)
        with mock.patch.object(grid, "_text_width", side_effect=AssertionError("asked again")):
            self.assertEqual(grid.cell_width("가"), 2)


if __name__ == "__main__":
    unittest.main()
