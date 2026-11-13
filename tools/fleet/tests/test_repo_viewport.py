"""Repo fold, pinned list viewport, and paint-derived hit regression tests."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import curses
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render  # noqa: E402
from fleet.model import Session  # noqa: E402


class FakeScreen:
    def __init__(self, h=24, w=120, fail_rows=()):
        self.h, self.w = h, w
        self.fail_rows = set(fail_rows)
        self.writes = []
        self.erased = 0

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.erased += 1
        self.writes = []

    def addstr(self, row, col, text, attr=0):
        if row in self.fail_rows:
            raise curses.error("fixture paint failure")
        self.writes.append((row, col, text, attr))

    def chgat(self, row, col, width, attr):
        pass

    def noutrefresh(self):
        pass

    def row_text(self, row):
        return "".join(text for y, _x, text, _attr in sorted(
            self.writes, key=lambda entry: (entry[0], entry[1])) if y == row)


def session(pid, cwd, slug):
    value = Session(harness="claude", pid=pid, proc_start="start-%d" % pid,
                    cwd=cwd, slug=slug, title=slug, liveness="working", elapsed_min=1)
    return value


class RepoViewportTest(unittest.TestCase):
    def setUp(self):
        render.reset_selection()
        render.set_process_view(False)
        render.set_show_all(False)
        render._ROUTE_FOLD = {"route:kept": True}
        render._REPO_FOLD = {}
        render._REPO_FOLDABLE = []
        render._SELECTABLE = []
        render._FOLDABLE = []
        render._OFFSET = 0
        render._LAYOUT = "auto"
        render._RELOAD_FRAME = None
        render._RELOAD_FRAME_AT = None
        render._RELOAD_FRAME_COMPUTE_AT = None
        render._RELOAD_LAYOUT = None
        render._HIT_VALID = False

    def tearDown(self):
        render.reset_selection()
        render.set_process_view(False)
        render.set_show_all(False)
        render._REPO_FOLD = {}
        render._ROUTE_FOLD = {}
        render._OFFSET = 0
        render._RELOAD_FRAME = None
        render._RELOAD_FRAME_AT = None
        render._RELOAD_FRAME_COMPUTE_AT = None
        render._RELOAD_LAYOUT = None
        render._HIT_VALID = False

    def draw(self, sessions, h=24, w=120, fail_rows=(), snapshot=None):
        screen = FakeScreen(h, w, fail_rows=fail_rows)
        with mock.patch.object(render.curses, "doupdate"):
            render._draw(screen, sessions, [], "fleet", 0, snapshot=snapshot)
        return screen

    @staticmethod
    def key_for(mapping, label):
        return next(key for key, entry in mapping.items()
                    if entry.get("repo_key") == label)

    @staticmethod
    def x_for(spans, row):
        x0, x1 = spans[row][0]
        return x0 + (x1 - x0 - 1) // 2

    def test_repo_titles_start_expanded_and_fold_only_their_own_body(self):
        rows = [session(81001, "/work/alpha", "alpha-only-task"),
                session(81002, "/work/beta", "beta-only-task")]
        self.draw(rows)
        alpha_row = self.key_for(render._REPO_FOLD_ROWS, "alpha")
        x = self.x_for(render._REPO_FOLD_SPANS, alpha_row)
        self.assertFalse(render._REPO_FOLD.get("alpha", False))
        self.assertTrue(render._handle_mouse(x, alpha_row))
        self.assertTrue(render._REPO_FOLD["alpha"])
        self.assertEqual(render._ROUTE_FOLD, {"route:kept": True})

        screen = self.draw(rows)
        self.assertTrue(render._REPO_FOLD_ROWS[alpha_row]["folded"])
        folded_text = "\n".join(render._plain(line) for line in render._RELOAD_FRAME)
        self.assertIn("alpha/", folded_text)
        self.assertNotIn("alpha-only-task", folded_text)
        self.assertIn("beta-only-task", folded_text)
        self.assertTrue(any("▸" in text for _row, _col, text, _attr in screen.writes))
        self.assertFalse(any(entry["pid"] == 81001 for entry in render._CLICK_ROWS.values()))

    def test_repo_title_hit_requires_painted_cells_and_failed_paint_has_no_hit(self):
        rows = [session(81101, "/work/alpha", "alpha-task")]
        self.draw(rows)
        row = self.key_for(render._REPO_FOLD_ROWS, "alpha")
        spans = render._REPO_FOLD_SPANS[row]
        self.assertFalse(any(x0 <= 119 < x1 for x0, x1 in spans))
        self.assertTrue(render._handle_mouse(119, row))
        self.assertFalse(render._REPO_FOLD.get("alpha", False))

        failed = self.draw(rows, fail_rows=(row,))
        self.assertNotIn(row, render._REPO_FOLD_ROWS)
        self.assertFalse(any(y == row for y, _x, _text, _attr in failed.writes))

    def test_folding_repo_clears_selection_owned_by_its_body(self):
        rows = [session(81111, "/work/alpha", "alpha-task"),
                session(81112, "/work/beta", "beta-task")]
        self.draw(rows)
        session_row, entry = next((row, entry) for row, entry in render._CLICK_ROWS.items()
                                  if entry.get("repo_key") == "alpha")
        x = self.x_for(render._CLICK_SPANS, session_row)
        self.assertTrue(render._handle_mouse(x, session_row))
        self.assertTrue(render._SELECT_MODE)
        self.assertEqual(render._CURSOR_ID, (entry["pid"], entry["proc_start"]))

        repo_row = self.key_for(render._REPO_FOLD_ROWS, "alpha")
        x = self.x_for(render._REPO_FOLD_SPANS, repo_row)
        self.assertTrue(render._handle_mouse(x, repo_row))
        self.assertFalse(render._SELECT_MODE)
        self.assertIsNone(render._CURSOR_ID)

        self.draw(rows)
        self.assertFalse(any(value.get("repo_key") == "alpha"
                             for value in render._CLICK_ROWS.values()))
        self.assertTrue(any(value.get("repo_key") == "beta"
                            for value in render._CLICK_ROWS.values()))

    @mock.patch.object(render.time, "time", return_value=1770000000.0)
    def test_flat_builder_ignores_offset_and_fixed_rows_stay_pinned(self, _clock):
        rows = [session(81200 + i, "/work/alpha", "task-%02d" % i) for i in range(14)]
        render._OFFSET = 0
        with mock.patch.object(render, "_BLINK_ON", False):
            first = render._build_lines(rows, [], "fleet", False, 0, term_width=120)
            render._OFFSET = 5
            second = render._build_lines(rows, [], "fleet", False, 0, term_width=120)
        self.assertEqual(first, second)
        self.assertEqual(render._OFFSET, 5)

        render._OFFSET = 0
        with mock.patch.object(render, "_BLINK_ON", False):
            screen0 = self.draw(rows, h=12)
        fixed_rows0 = [screen0.row_text(i) for i in range(render._CURRENT_BODY_TOP)]
        self.assertGreater(render._CURRENT_BODY_COUNT, render._CURRENT_BODY_H)
        render._OFFSET = 2
        with mock.patch.object(render, "_BLINK_ON", False):
            screen1 = self.draw(rows, h=12)
        fixed_rows1 = [screen1.row_text(i) for i in range(render._CURRENT_BODY_TOP)]
        self.assertEqual(fixed_rows0, fixed_rows1)
        self.assertEqual(render._OFFSET, 2)

    def test_page_home_end_and_wheel_use_body_only(self):
        rows = [session(81300 + i, "/work/alpha", "task-%02d" % i) for i in range(14)]
        self.draw(rows, h=12)
        body_h = render._CURRENT_BODY_H
        body_count = render._CURRENT_BODY_COUNT
        self.assertGreater(body_h, 0)
        self.assertTrue(render._handle_base_key(curses.KEY_NPAGE, body_h))
        self.assertEqual(render._OFFSET, body_h)
        self.assertTrue(render._handle_base_key(curses.KEY_HOME, body_h))
        self.assertEqual(render._OFFSET, 0)
        self.assertTrue(render._handle_base_key(curses.KEY_END, body_h))
        self.draw(rows, h=12)
        self.assertEqual(render._OFFSET, max(0, body_count - body_h))

        render._OFFSET = 0
        self.draw(rows, h=12)
        down = getattr(curses, "BUTTON5_PRESSED", 0)
        up = getattr(curses, "BUTTON4_PRESSED", 0)
        if down and up:
            y = render._CURRENT_BODY_TOP
            render._handle_mouse_event(0, y, down)
            self.assertEqual(render._OFFSET, 1)
            self.draw(rows, h=12)
            y = render._CURRENT_BODY_TOP
            render._handle_mouse_event(0, y, up)
            self.assertEqual(render._OFFSET, 0)
            self.draw(rows, h=12)
            y = render._CURRENT_BODY_TOP
            render._handle_mouse_event(0, y, down)
            self.assertEqual(render._OFFSET, 1)
            before = render._OFFSET
            release = getattr(curses, "BUTTON1_RELEASED", 0)
            render._handle_mouse_event(0, y, release)
            render._handle_mouse_event(0, y, 1 << 30)
            self.assertEqual(render._OFFSET, before)
            self.assertIsNone(render._PROMPT)

    def test_short_and_narrow_windows_have_safe_zero_body(self):
        rows = [session(81400 + i, "/work/alpha", "task-%02d" % i) for i in range(5)]
        for width in (20, 40, 60, 100, 120, 168):
            for height in (0, 1, 2, 3, 6, 12, 24):
                with self.subTest(height=height, width=width):
                    screen = self.draw(rows, h=height, w=width)
                    if height <= 0:
                        self.assertFalse(render._HIT_VALID)
                        self.assertFalse(render._REPO_FOLD_ROWS)
                    elif height == 1:
                        self.assertEqual(render._CURRENT_BODY_H, 0)
                        self.assertTrue(screen.writes)
                        self.assertTrue(all(row == 0 for row, _x, _text, _attr in screen.writes))
                    elif height == 2:
                        self.assertEqual(render._CURRENT_BODY_H, 0)
                        self.assertEqual(render._CURRENT_BODY_TOP, 1)
                    elif height == 3:
                        self.assertLessEqual(render._CURRENT_BODY_H, 1)
        self.draw(rows, h=12, w=1)
        self.assertFalse(render._HIT_VALID)
        self.assertFalse(render._CLICK_ROWS)
        self.assertFalse(render._REPO_FOLD_ROWS)

    def test_combining_and_wide_cells_use_display_width(self):
        self.assertEqual(render._cw("\u0301"), 0)
        self.assertEqual(render._dw("e\u0301"), 1)
        self.assertEqual(render._dw("한"), 2)
        screen = self.draw([session(81501, "/work/한", "cafe\u0301")], w=40)
        self.assertTrue(render._REPO_FOLD_ROWS)
        row = next(iter(render._REPO_FOLD_ROWS))
        self.assertTrue(render._REPO_FOLD_SPANS[row])
        self.assertTrue(screen.writes)

    def test_snapshot_change_invalidates_old_mouse_target_until_repaint(self):
        rows = [session(81601, "/work/alpha", "alpha-task")]
        snapshot = SimpleNamespace(node_evidence=None, route_entities=None, observations=None,
                                  resource_diagnostics=None, pending_delivery=None)
        self.draw(rows, snapshot=snapshot)
        row = next(iter(render._CLICK_ROWS))
        spans = render._CLICK_SPANS[row]
        x = self.x_for(render._CLICK_SPANS, row)
        render._HIT_VALID = False  # _loop performs this invalidation when a new snapshot lands.
        render._handle_mouse(x, row)
        self.assertFalse(render._SELECT_MODE)
        self.assertIsNone(render._PROMPT)
        self.assertEqual(render._CLICK_SPANS[row], spans)


if __name__ == "__main__":
    unittest.main()
