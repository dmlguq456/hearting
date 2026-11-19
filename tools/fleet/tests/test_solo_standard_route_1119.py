"""11-19 (route form 3b-1): Fleet draws the middle cell `solo·standard` (one owner makes the work and starts its review).

The ROUTES cell reads the form and the intensity (`code(solo·standard)`), never `staged`, at the widths it is drawn in; the
collector's `quick-shape` check holds the cell's owner row to the serial owner shape and leaves its review row alone.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

from tools.fleet.explorer import tables
from tools.fleet.explorer.grid import CellGrid


def cell(route, width):
    grid = CellGrid(width, 1)
    grid.beat = 1
    tables._draw_route_progress(grid, 0, width, 0, route, card=False)
    return "".join(c[0] for c in grid.cells[0]).rstrip()


def solo_standard(reviewing=True):
    names = ["impl-review", "one-shot"]
    at = 0 if reviewing else 1
    return {"state": "working", "shape": "one-shot-owner", "capability": "code", "intensity": "standard",
            "stages": [{"name": n, "st": "done" if i < at else "active" if i == at else "todo"}
                       for i, n in enumerate(names)],
            "legs": (("code", "active", f"{at + 1}/2"),)}


class SoloStandardCell(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})
        env.start()
        self.addCleanup(env.stop)

    def test_the_cell_reads_the_form_and_the_intensity_at_every_width_and_never_staged(self):
        for reviewing in (True, False):
            for width in (100, 74, 44, 36, 28, 20):
                with self.subTest(reviewing=reviewing, width=width):
                    text = cell(solo_standard(reviewing), width)
                    self.assertTrue(text, "an empty cell")
                    self.assertNotIn("staged", text)
                    self.assertNotIn("team", text)
                    self.assertTrue(text.startswith("code(solo") or "code(solo" in text or width < 28, text)
        self.assertTrue(cell(solo_standard(), 100).startswith("code(solo·standard)"), cell(solo_standard(), 100))
        self.assertTrue(cell(solo_standard(False), 74).startswith("code(solo·standard)"), cell(solo_standard(False), 74))
