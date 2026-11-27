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


def solo_up(intensity, at):
    names = (["impl-review", "impl-review-alternative", "impl-review-failure-mode"][:2 if intensity == "strong" else 3]
             + ["one-shot"])
    return {"state": "working", "shape": "one-shot-owner", "capability": "code", "intensity": intensity, "field": "code",
            "stages": [{"name": n, "st": "done" if i < at else "active" if i == at else "todo"}
                       for i, n in enumerate(names)],
            "legs": (("code", "active", f"{at + 1}/{len(names)}"),)}


class SoloStrongAndThoroughCell(unittest.TestCase):
    """S2: `solo·strong`, `solo·thorough` and `solo·adversarial` are drawn by the words the cell already has."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})
        env.start()
        self.addCleanup(env.stop)

    def test_the_cell_reads_the_form_and_the_intensity_and_the_node_order_and_never_staged(self):
        for intensity in ("strong", "thorough", "adversarial"):
            for at in (0, 1, 2):
                for width in (100, 74, 44):
                    with self.subTest(intensity=intensity, at=at, width=width):
                        text = cell(solo_up(intensity, at), width)
                        self.assertTrue(text.startswith(f"code(solo·{intensity}) · {at + 1}/"), text)
                        self.assertNotIn("staged", text)
                        self.assertNotIn("team", text)
                for width in (28, 20):
                    text = cell(solo_up(intensity, 0), width)
                    self.assertTrue(text and "staged" not in text, text)
        self.assertEqual(cell(solo_up("strong", 0), 100), "code(solo·strong) · 1/3: impl-review")
        self.assertEqual(cell(solo_up("thorough", 3), 100), "code(solo·thorough) · 4/4: solo")
        # the lighter cells read as they did
        self.assertEqual(cell(solo_standard(), 100), "code(solo·standard) · 1/2: impl-review")

    def test_the_kind_cell_wears_the_bold_key_from_strong_up_and_the_plain_key_at_standard(self):
        row = lambda intensity: {"capability": "code", "field": "code", "shape": "one-shot-owner", "intensity": intensity}  # noqa: E731
        self.assertEqual(tables._kind_cell(row("standard"))[1], ("(solo·standard)", "kinds_code"))
        for intensity in ("strong", "thorough", "adversarial"):
            self.assertEqual(tables._kind_cell(row(intensity)), [("code", "kind_code"), (f"(solo·{intensity})", "kindb_code")])
