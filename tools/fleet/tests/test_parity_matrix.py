"""Synthetic tests for the matrix state walk, diff and defect bookkeeping; the committed fixture
view model also has to draw at 80 and 100 columns without overlapping or overflowing itself."""
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.view_model import load_fixture
from tools.fleet.tests.parity_compare import diff_frames
from tools.fleet.tests.parity_matrix import (detect_reference_defects, find_text_defects,
                                              own_trace_defects)
from tools.fleet.tests.parity_states import (apply_action, base_state, enumerate_states, nav_state,
                                             state_id, state_key, walk_next)

FIXTURE = Path(__file__).parent / "fixtures" / "explorer" / "view_model.json"


class MatrixParityTests(unittest.TestCase):
    def test_enumeration_includes_interaction_dimensions(self):
        rows = enumerate_states(vm={"repos": [{"id": "r"}],
                                    "sessions": [{"tag": "s"}],
                                    "routes": [{"id": "rt"}]})
        ids = {row["id"] for row in rows}
        self.assertTrue({"usage-open", "OVERVIEW-scroll:1", "repo:r", "session:s",
                         "route:rt", "HISTORY-generation:0"}.issubset(ids))

    def test_reference_defect_coordinates_can_be_excluded_from_cell_diff(self):
        cells = [[("x", "d", None), ("界", "d", None)]]
        reference = SimpleNamespace(cells=cells, hits=[[0, 1, 3, ["x"]]], cols=2, rows=1)
        fleet = SimpleNamespace(cells=[[('x', 'd', None), ('y', 'd', None)]],
                                hits=reference.hits, cols=2, rows=1)
        defects = detect_reference_defects(reference, 2, pane_x=1)
        self.assertTrue(defects)
        # The explicit region excludes only the reported edge cell.
        self.assertEqual(diff_frames(reference, fleet, ((0, 1, 2),)), [])

    def test_walk_follows_the_reference_hit_actions(self):
        state = apply_action(base_state(), ["root", "ROUTES", {"ropen:ROUTES": "f5"}])
        self.assertEqual((state["view"], state["scope"], state["ui"]),
                         ("ROUTES", None, {"ropen:ROUTES": "f5"}))
        self.assertEqual(dict(nav_state(state).folds), {"ropen:ROUTES": "f5"})
        again = apply_action(state, ["fold", "ropen:ROUTES", "f5"])
        self.assertEqual(again["ui"], {})              # a second click folds it again
        page = apply_action(state, ["scope", "f5"])
        self.assertEqual(nav_state(page).scope, "session:f5")
        picked = apply_action(apply_action(base_state(), ["tab", "HISTORY"]), ["ui", "gen:f5", 0])
        self.assertEqual(dict(nav_state(picked).folds), {"ui:gen:f5": "0"})
        self.assertIsNone(apply_action(base_state(), ["ui", "apply", "dry-run"]))
        self.assertNotEqual(state_key(state), state_key(again))
        self.assertTrue(state_id(page).startswith("root:ROUTES"))

    def test_walk_children_add_first_and_last_scroll_offsets(self):
        children = walk_next(base_state(), [["tab", "PEERS"], ["scroll", "rawoff", 9], ["usage"]])
        ids = {state_id(child) for child in children}
        self.assertEqual(ids, {"tab:PEERS", "scroll:rawoff=1", "scroll:rawoff=9", "usage"})
        scrolled = next(child for child in children if state_id(child) == "scroll:rawoff=9")
        self.assertEqual(nav_state(scrolled).scroll, (("rawoff", 9),))
        self.assertEqual(walk_next(scrolled, [["tab", "PEERS"]]), [])    # scroll states are leaves

    def test_text_defects_see_overlap_cut_text_and_stray_borders(self):
        events = [("cell", 1, 10, "a", 40, 1), ("cell", 2, 10, "b", 40, 1),      # text on text
                  ("cell", 3, 12, "│", 50, 1), ("cell", 4, 12, " ", 50, 1),      # blank on a border
                  ("cell", 5, 14, "─", 60, 1), ("cell", 6, 14, "x", 60, 1),      # text in a rule
                  ("cell", 7, 16, "│", 70, 1), ("cell", 8, 16, "│", 70, 1),      # two lines crossing
                  ("clip", 9, 18, "tail", True, 95)]
        found = {(item["kind"], item["row"]) for item in find_text_defects(events, 100)}
        self.assertEqual(found, {("text-overlap", 10), ("text-overlap", 12),
                                 ("text-beyond-width", 18)})

    def test_fixture_draws_without_overlap_or_overflow_at_narrow_widths(self):
        vm = load_fixture(FIXTURE)
        states = [NavState(view=view, title=view) for view in
                  ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE", "MEMORY",
                   "MODELS", "SYSTEM")]
        states += [NavState(scope="session:f5"), NavState(scope="route:f5"),
                   NavState(scope="repo:xxxxxxxxxx"), NavState(usage_open=True),
                   NavState(view="ROUTES", folds=(("ropen:ROUTES", "f5"),)),
                   NavState(view="HISTORY", folds=(("hopen:HISTORY", "0c"),))]
        for width in (80, 100):
            for state in states:
                with self.subTest(width=width, view=state.view, scope=state.scope):
                    _frame, defects = own_trace_defects(vm, state, width)
                    self.assertEqual(defects, [])


if __name__ == "__main__":
    unittest.main()
