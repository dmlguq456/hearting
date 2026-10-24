"""Bounds checks for the list views at supported terminal widths.

At 80 and 100 columns the views draw what the approved reference draws; where that drawing
overlaps itself the views stay clean instead. ``parity_matrix.py`` compares every reachable
state with the reference; these checks only keep every frame inside the screen.
"""
import unittest

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.grid import cell_width
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.view_model import load_fixture
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures" / "explorer"


class ListSizes(unittest.TestCase):
    def test_list_views_fit_and_hits_stay_inside_the_body(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (80, 100, 140, 168):
            for view in ("ROUTES", "QUESTIONS", "HISTORY", "PEERS"):
                variants = [NavState(view=view, title=view)]
                fold_key, ident = {"ROUTES": ("ropen:ROUTES", "f5"),
                                   "QUESTIONS": ("qshut:QUESTIONS", "bb"),
                                   "HISTORY": ("hopen:HISTORY", "0c"),
                                   "PEERS": ("pfocus:PEERS", "f5")}[view]
                variants.append(NavState(view=view, title=view, folds=((fold_key, ident),)))
                for state in variants:
                    with self.subTest(cols=cols, view=view, folds=state.folds):
                        frame = draw(vm, state, cols, 45, beat=1)
                        self.assertEqual(len(frame.cells), 45)
                        self.assertTrue(all(len(row) == cols for row in frame.cells))
                        self.assertTrue(all(hit.y < 41 for hit in frame.hits if hit.y >= 0))
                        self.assertTrue(all((hit.y == -1 and hit.action[0] == "parent") or
                                            (0 <= hit.x0 < hit.x1 <= cols)
                                            for hit in frame.hits))
                        self.assertTrue(all(sum(cell_width(cell[0]) for cell in row) == cols
                                            for row in frame.cells))
                        self.assertEqual(len(frame.text_rows()[42]), cols)
                        self.assertEqual(len(frame.text_rows()[43]), cols)
                        self.assertEqual(len(frame.text_rows()[44]), cols)


class QuietSizes(unittest.TestCase):
    def test_quiet_views_fit_and_hits_stay_in_bounds_at_all_widths(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (80, 100, 140, 168):
            for view in ("COMPUTE", "MEMORY", "MODELS", "SYSTEM"):
                state = NavState(view=view, title=view)
                if view == "MODELS":
                    state = NavState(view=view, title=view,
                                     folds=(("ui:sel", "light/codex"),
                                            ("ui:model", "gpt-6-luna"),
                                            ("ui:effort", "high")))
                with self.subTest(cols=cols, view=view):
                    frame = draw(vm, state, cols, 45, beat=1)
                    self.assertTrue(all(len(row) == cols for row in frame.cells))
                    self.assertTrue(all((hit.y == -1 and hit.action[0] == "parent") or
                                        (0 <= hit.y < 41 and 0 <= hit.x0 < hit.x1 <= cols)
                                        for hit in frame.hits))
                    self.assertEqual(len(frame.text_rows()[42]), cols)
                    self.assertEqual(len(frame.text_rows()[43]), cols)
                    self.assertEqual(len(frame.text_rows()[44]), cols)
