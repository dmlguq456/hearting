"""11-17 (the person): Fleet's route line reads ``code(staged 3/6): execute``.

The route's kind word leads the stage cell of a staged route in Fleet's ROUTES rows (the ROUTE popup, the session
page and OVERVIEW draw the same cell): the one bold word, then the shape in brackets, then the stage after a colon; the
stage keeps its hue but not its weight. Short of cells the kind goes, then the word ``staged``, then the position.
Everything is synthetic.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import inspect
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

from tools.fleet.explorer import tables
from tools.fleet.explorer.grid import CellGrid
from tools.fleet.palette import _A_B, _HUE_OF

NOW = 1_800_000_000.0
CAUSES = ("busy", "form", "draft", "paste", "unsettled", "unobserved", "old-shell", "no-shell")


def stages(at=1, mark="active", count=4):
    names = ["plan", "execute", "test", "report", "close", "ship"][:count]
    return [{"name": name, "st": "done" if i < at else mark if i == at else "todo"} for i, name in enumerate(names)]


def route(capability="code", at=1, mark="active", count=4):
    listed = stages(at, mark, count)
    return {"state": "working", "shape": "staged", "stages": listed,
            "legs": tuple((s["name"], s["st"], None) for s in listed),
            **({"capability": capability} if capability else {})}


def progress(value, width):
    grid = CellGrid(width, 1)
    tables._draw_route_progress(grid, 0, width, 0, value)
    cells = grid.cells[0]
    return "".join(cell[0] for cell in cells).rstrip(), cells


def weight(key):
    return bool(_HUE_OF.get(key, ("d", 0))[1] & _A_B)


class FleetRouteKindLine(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_the_cell_reads_kind_shape_colon_stage(self):
        text, _cells = progress(route(at=0), 60)
        self.assertIn("code(staged 1/4): plan ●", text)
        self.assertIn("code(staged 3/6): test ●", progress(route(at=2, count=6), 70)[0])
        self.assertIn("code(staged 2/4): execute ✕", progress(route(at=1, mark="failed"), 70)[0])
        for old in ("code · staged", "staged 2/4 execute", " · execute"):
            self.assertNotIn(old, progress(route(), 70)[0])

    def test_only_the_kind_word_is_bold_and_the_stage_keeps_its_hue(self):
        _text, cells = progress(route(), 60)
        row = "".join(cell[0] for cell in cells)
        start = row.index("code(")
        keys = {row[i]: cells[i][1] for i in range(start, start + len("code(staged 2/4): execute"))}
        self.assertTrue(weight(cells[start][1]), "the kind word is bold")
        self.assertNotIn(cells[start][1], ("dim", None))
        for offset, char in enumerate("code(staged 2/4): execute"):
            key = cells[start + offset][1]
            if offset < 4:
                continue
            self.assertFalse(weight(key), (char, key))      # brackets, shape, colon and stage: not bold
        name_key = cells[row.index("execute")][1]
        self.assertEqual(name_key, "hb_claude")             # index 1: its hue as before (here lit)
        # The stage that was bold (green, yellow, magenta) keeps its hue at plain weight.
        for index, plain in ((2, "grp_live"), (3, "g_idle"), (4, "hb_codex")):
            _text, cells3 = progress(route(at=index, count=6), 80)
            row3 = "".join(cell[0] for cell in cells3)
            at = row3.index(["plan", "execute", "test", "report", "close", "ship"][index] + " ")
            self.assertEqual(cells3[at][1], plain, index)
            self.assertEqual(_HUE_OF[plain][0], {2: "g", 3: "y", 4: "m"}[index])

    def test_the_cell_narrows_kind_then_staged_then_the_position_and_the_stage_stays(self):
        want = [(60, "code(staged 2/4): execute ●"), (34, "staged 2/4: execute ●"), (26, "2/4: execute ●")]
        for width, expect in want:
            text, _cells = progress(route(at=1), width)
            self.assertIn(expect, text, (width, text))
        # A route that names no kind reads the shape and the stage.
        self.assertIn("staged 2/4: execute ●", progress(route(capability=None), 60)[0])
        # The stage and its mark stay at every width.
        for width in range(24, 80):
            text, _cells = progress(route(at=1), width)
            self.assertIn("execute ●", text, (width, text))

    def test_every_fleet_place_draws_the_one_cell(self):
        # ROUTES rows (and the ROUTE popup's table), the session page's and OVERVIEW's route lines all call it.
        self.assertIn("_draw_route_progress(", inspect.getsource(tables.draw_route_row))
        self.assertIn("_draw_route_progress(", inspect.getsource(tables._draw_route_line))
        source = inspect.getsource(tables._draw_route_progress)
        self.assertIn('"(" + shape + "): "', source)


if __name__ == "__main__":
    unittest.main()
