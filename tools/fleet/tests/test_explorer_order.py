"""Stable explorer ordering and drag-reducer contracts."""
import json
import tempfile
import unittest
from pathlib import Path

from tools.fleet.explorer import order
from tools.fleet.explorer.frame import CellFrame
from tools.fleet.explorer.grid import Hit
from tools.fleet.explorer.state import NavState, reduce


class ExplorerOrderTests(unittest.TestCase):
    def test_atomic_order_round_trip_and_unreadable_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "fleet2-order.json"
            self.assertEqual(order.load_order(path), {"repos": [], "sessions": {}})
            state = order.move_repo({"repos": ["a", "b"], "sessions": {}}, "b", "a")
            state = order.move_session(state, "b", "sid:b1", None)
            order.save_order(state, path)
            self.assertEqual(order.load_order(path), state)
            path.write_text("{invalid", encoding="utf-8")
            self.assertEqual(order.load_order(path), {"repos": [], "sessions": {}})
            path.write_text(json.dumps({"schema": 1, "repos": "not-a-list", "sessions": []}),
                            encoding="utf-8")
            self.assertEqual(order.load_order(path), {"repos": [], "sessions": {}})

    def test_reconcile_prunes_ended_and_appends_new_in_natural_order(self):
        saved = {"repos": ["b", "ended", "a"],
                 "sessions": {"b": ["sid:b2", "sid:ended"], "a": ["sid:a1"]}}
        repos = [{"id": "a"}, {"id": "b"}, {"id": "new"}]
        sessions = [
            {"repo_id": "a", "order_key": "sid:a1"},
            {"repo_id": "a", "order_key": "sid:a2"},
            {"repo_id": "b", "order_key": "sid:b1"},
            {"repo_id": "b", "order_key": "sid:b2"},
        ]
        result = order.reconcile_order(saved, repos, sessions)
        self.assertEqual(result["repo_order"], ["b", "a", "new"])
        self.assertEqual(result["session_order"],
                         {"b": ["sid:b2", "sid:b1"], "a": ["sid:a1", "sid:a2"]})

    def test_tree_press_without_motion_remains_a_click(self):
        frame = CellFrame((), (Hit(4, 0, 32, ("scope", "repo:a")),), 40, 20)
        pressed, effects = reduce(NavState(), {"type": "mouse", "phase": "press", "x": 2, "y": 4,
                                                "ordering": {}}, frame)
        self.assertTrue(pressed.drag)
        self.assertEqual(effects, ())
        clicked, effects = reduce(pressed, {"type": "mouse", "phase": "release", "x": 2, "y": 4,
                                             "ordering": {}}, frame)
        self.assertEqual(clicked.scope, "repo:a")
        self.assertFalse(clicked.drag)
        self.assertEqual(effects, ())

    def test_tree_drag_highlight_begins_on_first_motion(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.view_model import load_fixture
        fixture = Path(__file__).parent / "fixtures" / "explorer" / "view_model.json"
        vm = load_fixture(fixture)
        initial = draw(vm, NavState(), 140, 45)
        repo_hit = next(hit for hit in initial.hits if hit.action[:1] == ("scope",)
                        and str(hit.action[1]).startswith("repo:"))
        pressed, _ = reduce(NavState(), {"type": "mouse", "phase": "press", "x": 2,
                                         "y": repo_hit.y}, initial)
        pressed_frame = draw(vm, pressed, 140, 45)
        self.assertEqual(pressed_frame.cells, initial.cells)
        moved, _ = reduce(pressed, {"type": "mouse", "phase": "press", "motion": True,
                                    "x": 4, "y": repo_hit.y}, initial)
        moved_frame = draw(vm, moved, 140, 45)
        grabbed = next(hit for hit in moved_frame.hits if hit.action == repo_hit.action)
        self.assertTrue(all(cell[2] == 236 for cell in moved_frame.cells[grabbed.y][:32]))

    def test_repo_drag_commits_and_cross_repo_session_drop_is_refused(self):
        frame = CellFrame((), (Hit(4, 0, 32, ("scope", "repo:a")),
                               Hit(5, 0, 32, ("scope", "repo:b")),
                               Hit(6, 0, 32, ("scope", "ta")),
                               Hit(7, 0, 32, ("scope", "tb"))), 40, 20)
        ordering = {"tag_keys": {"ta": "sid:a", "tb": "sid:b"},
                    "repo_by_tag": {"ta": "a", "tb": "b"}}
        state, _ = reduce(NavState(), {"type": "mouse", "phase": "press", "x": 2, "y": 4,
                                       "ordering": ordering}, frame)
        state, _ = reduce(state, {"type": "mouse", "phase": "press", "motion": True,
                                  "x": 2, "y": 5, "ordering": ordering}, frame)
        self.assertEqual(state.drag[6:8], ("b", True))
        state, effects = reduce(state, {"type": "mouse", "phase": "release", "x": 2, "y": 5,
                                        "ordering": ordering}, frame)
        self.assertEqual(effects, (("reorder", "repo", "a", "a", "b"),))
        ended_target_state, _ = reduce(NavState(), {"type": "mouse", "phase": "press", "x": 2,
                                                    "y": 4, "ordering": ordering}, frame)
        ended_target_state, _ = reduce(ended_target_state,
                                      {"type": "mouse", "phase": "press", "motion": True,
                                       "x": 2, "y": 5, "ordering": ordering}, frame)
        _ended_target_state, ended_effects = reduce(
            ended_target_state, {"type": "mouse", "phase": "release", "x": 2, "y": 5,
                                 "ordering": {"repo_by_tag": {}, "tag_keys": {}}}, frame)
        self.assertEqual(ended_effects, (("reorder", "repo", "a", "a", "b"),))
        self.assertEqual(order.move_repo({"repos": ["a", "c"], "sessions": {}},
                                         ended_effects[0][3], ended_effects[0][4])["repos"],
                         ["c", "a"])
        state, _ = reduce(NavState(), {"type": "mouse", "phase": "press", "x": 2, "y": 6,
                                       "ordering": ordering}, frame)
        state, _ = reduce(state, {"type": "mouse", "phase": "press", "motion": True,
                                  "x": 2, "y": 7, "ordering": ordering}, frame)
        self.assertFalse(state.drag[7])
        state, effects = reduce(state, {"type": "mouse", "phase": "release", "x": 2, "y": 7,
                                        "ordering": ordering}, frame)
        self.assertEqual(effects, ())

    def test_empty_rail_below_tree_commits_last_insertion_without_hit(self):
        frame = CellFrame((), (Hit(4, 0, 32, ("scope", "repo:a")),), 40, 20)
        state, _ = reduce(NavState(), {"type": "mouse", "phase": "press", "x": 2, "y": 4,
                                       "ordering": {}}, frame)
        state, _ = reduce(state, {"type": "mouse", "phase": "press", "motion": True,
                                  "x": 2, "y": 8, "ordering": {}}, frame)
        self.assertEqual(state.drag[6:8], ("end", True))
        self.assertFalse(any(hit.action[:1] == ("order-end",) for hit in frame.hits))
        _state, effects = reduce(state, {"type": "mouse", "phase": "release", "x": 2, "y": 8,
                                         "ordering": {}}, frame)
        self.assertEqual(effects, (("reorder", "repo", "a", "a", "end"),))
        moved = order.move_repo({"repos": ["a", "b"], "sessions": {}}, "a", None)
        self.assertEqual(moved["repos"], ["b", "a"])


if __name__ == "__main__":
    unittest.main()
