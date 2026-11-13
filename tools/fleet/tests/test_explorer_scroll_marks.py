"""Sidebar section rules and the pinned OVERVIEW ROUTES/RECENT block with its overflow mark."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from pathlib import Path

from tools.fleet.explorer.frame import draw, measure_pane_max
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.view_model import load_fixture

FIXTURE = Path(__file__).parent / "fixtures" / "explorer" / "view_model.json"
NAV = 32
PANE_X = 34


def _text(frame, y, x0=0, x1=None):
    return "".join(cell[0] for cell in frame.cells[y][x0:x1])


def _state(**scroll):
    return NavState(scroll=tuple(sorted(scroll.items())))


class SidebarRules(unittest.TestCase):
    def setUp(self):
        self.vm = load_fixture(FIXTURE)

    def test_rules_frame_the_session_tree_and_carry_no_hit(self):
        for rows in (40, 60):
            with self.subTest(rows=rows):
                frame = draw(self.vm, NavState(), 140, rows)
                _x0, top, _width, bottom, _offset, _maximum, _total, _visible = frame.tree_scroll[:8]
                compute_y = min(hit.y for hit in frame.hits if hit.action == ("root", "COMPUTE"))
                self.assertEqual(bottom, compute_y - 1)
                self.assertTrue(_text(frame, top - 1, 0, NAV).startswith(" SESSIONS"))
                for rule_y in (top - 2, bottom):
                    self.assertTrue(_text(frame, rule_y, 1, NAV - 1).startswith("┈┈┈┈"), rule_y)
                    self.assertFalse([hit for hit in frame.hits if hit.y == rule_y and hit.x0 < NAV])
                tree_hits = [hit for hit in frame.hits if hit.action[:1] in (("scope",), ("route",))
                             and hit.x1 <= NAV]
                self.assertTrue(all(top <= hit.y < bottom for hit in tree_hits))

    def test_more_mark_follows_the_tree_overflow(self):
        frame = draw(self.vm, NavState(), 140, 40)
        bottom, maximum = frame.tree_scroll[3], frame.tree_scroll[5]
        self.assertGreater(maximum, 0)
        self.assertIn("↓ more", _text(frame, bottom, 0, NAV))
        end = draw(self.vm, _state(tree=maximum), 140, 40)
        self.assertNotIn("↓", _text(end, bottom, 0, NAV))
        roomy = draw(self.vm, NavState(), 140, 60)
        self.assertEqual(roomy.tree_scroll[5], 0)
        self.assertNotIn("↓", _text(roomy, roomy.tree_scroll[3], 0, NAV))

    def test_wheel_on_a_rule_row_does_not_scroll_the_tree(self):
        frame = draw(self.vm, NavState(), 140, 40)
        bottom = frame.tree_scroll[3]
        state, _effects = reduce(NavState(), {"type": "wheel", "direction": -1, "x": 5, "y": bottom}, frame)
        self.assertEqual(dict(state.scroll).get("tree", 0), 0)
        inside, _effects = reduce(NavState(), {"type": "wheel", "direction": -1, "x": 5, "y": bottom - 1},
                                  frame)
        self.assertGreater(dict(inside.scroll).get("tree", 0), 0)


class PinnedOverviewTail(unittest.TestCase):
    def setUp(self):
        self.vm = load_fixture(FIXTURE)

    def _rows(self, frame):
        return [_text(frame, y, PANE_X) for y in range(frame.rows)]

    def _check_pinned(self, frame, rows):
        body_end = rows - 4
        top = frame.pane_scroll[3]
        text = self._rows(frame)
        self.assertLess(top, body_end, "the tail is pinned")
        self.assertTrue(text[top + 1].startswith("── ROUTES"), text[top + 1])
        self.assertTrue(any(line.startswith("── RECENT") for line in text[top:body_end]))
        recent = next(y for y in range(top, body_end) if text[y].startswith("── RECENT"))
        self.assertLess(recent + 1, body_end, "a RECENT row stays visible")
        self.assertIn("✓", text[recent + 1])
        route_hits = [hit for hit in frame.hits if hit.action[:2] == ("root", "ROUTES") and hit.x0 >= PANE_X]
        self.assertTrue(route_hits)
        self.assertTrue(all(top + 2 <= hit.y < recent for hit in route_hits))
        session_hits = [hit for hit in frame.hits if hit.action[:2] == ("fold", "sopen:OVERVIEW")]
        self.assertTrue(all(frame.pane_scroll[1] <= hit.y < top for hit in session_hits))
        return top, text

    def test_short_screens_pin_routes_and_mark_only_real_overflow(self):
        for rows in (24, 30, 40):
            with self.subTest(rows=rows):
                frame = draw(self.vm, NavState(), 140, rows)
                top, text = self._check_pinned(frame, rows)
                effective, maximum = frame.pane_scroll[4], frame.pane_scroll[5]
                self.assertLess(effective, maximum)
                self.assertIn("↓ more below", text[top])
                end_max = measure_pane_max(self.vm, NavState(), 140, rows)
                end = draw(self.vm, _state(pane=end_max), 140, rows)
                end_top, end_text = self._check_pinned(end, rows)
                self.assertEqual(end.pane_scroll[4], end.pane_scroll[5])
                self.assertNotIn("↓", end_text[end_top])
                self.assertIn("10 sessions", end_text[end_top - 1], "the list ends right above the tail")

    def test_page_keys_step_by_the_rows_above_the_tail(self):
        frame = draw(self.vm, NavState(), 140, 40)
        page = frame.pane_scroll[6]
        self.assertEqual(page, frame.pane_scroll[3] - frame.pane_scroll[1])
        down, _effects = reduce(NavState(), {"type": "key", "key": "pagedown"}, frame)
        self.assertEqual(dict(down.scroll)["pane"], min(frame.pane_scroll[5], page))
        moved = draw(self.vm, down, 140, 40)
        self._check_pinned(moved, 40)
        home, _effects = reduce(down, {"type": "key", "key": "home"}, moved)
        self.assertEqual(dict(home.scroll)["pane"], 0)

    def test_resize_keeps_the_tail_whole_and_the_list_scrolled(self):
        state = _state(pane=3)
        for rows in (40, 24, 40):
            with self.subTest(rows=rows):
                frame = draw(self.vm, state, 140, rows)
                top, text = self._check_pinned(frame, rows)
                last = max(y for y in range(top, rows - 4) if text[y].strip())
                self.assertTrue(text[last].lstrip().startswith("✓"), text[last])

    def test_a_tall_screen_keeps_the_tail_under_the_list_without_a_mark(self):
        frame = draw(self.vm, NavState(), 140, 60)
        text = self._rows(frame)
        self.assertEqual(frame.pane_scroll[3], 60 - 4, "nothing is pinned when everything fits")
        summary = next(y for y, line in enumerate(text) if "10 sessions" in line)
        self.assertTrue(text[summary + 2].startswith("── ROUTES"))
        self.assertNotIn("↓ more", "\n".join(text))

    def test_scoped_pages_keep_their_own_flow(self):
        repo = str(self.vm.repos[0].get("id"))
        frame = draw(self.vm, NavState(scope="repo:" + repo, title=repo), 140, 40)
        self.assertEqual(frame.pane_scroll[3], 40 - 4)


if __name__ == "__main__":
    unittest.main()
