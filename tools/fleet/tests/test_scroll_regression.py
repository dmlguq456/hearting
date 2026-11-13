"""Scroll regression budget = 0 (F-27, plan.md §7 Step 5).

Proves render.py:2610's contract executably: `_handle_base_key` was extracted from `_loop`
purely so this suite could exist without curses.wrapper (plan.md §2.2/§3). Hermetic — no
curses screen, no real session, no signal.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import curses
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render                                     # noqa: E402


class ScrollEnv(unittest.TestCase):
    def setUp(self):
        render.reset_selection()
        render._OFFSET = 0
        render.set_show_all(False)
        render._LAYOUT = "auto"

    def tearDown(self):
        render.reset_selection()
        render._OFFSET = 0
        render.set_show_all(False)
        render._LAYOUT = "auto"


class BaseModeScrollTest(ScrollEnv):
    """★ 태스크 명시 요구 — 6개 base 키 전부, curses.wrapper 없이 실행(hermetic)."""

    def test_arrow_keys_still_scroll_in_base_mode(self):
        render._OFFSET = 5
        self.assertTrue(render._handle_base_key(curses.KEY_UP, body_h=20))
        self.assertEqual(render._OFFSET, 4)
        self.assertTrue(render._handle_base_key(curses.KEY_DOWN, body_h=20))
        self.assertEqual(render._OFFSET, 5)

    def test_jk_still_scroll_in_base_mode(self):
        render._OFFSET = 5
        self.assertTrue(render._handle_base_key(ord("k"), body_h=20))
        self.assertEqual(render._OFFSET, 4)
        self.assertTrue(render._handle_base_key(ord("j"), body_h=20))
        self.assertEqual(render._OFFSET, 5)

    def test_pgup_pgdn_page_scroll(self):
        render._OFFSET = 50
        self.assertTrue(render._handle_base_key(curses.KEY_PPAGE, body_h=20))
        self.assertEqual(render._OFFSET, 30)
        self.assertTrue(render._handle_base_key(curses.KEY_NPAGE, body_h=20))
        self.assertEqual(render._OFFSET, 50)

    def test_home_end_jump_to_top_and_bottom(self):
        render._OFFSET = 50
        self.assertTrue(render._handle_base_key(curses.KEY_HOME, body_h=20))
        self.assertEqual(render._OFFSET, 0)
        self.assertTrue(render._handle_base_key(ord("g"), body_h=20))
        self.assertEqual(render._OFFSET, 0)
        self.assertTrue(render._handle_base_key(curses.KEY_END, body_h=20))
        self.assertEqual(render._OFFSET, 1 << 30)     # _draw clamps this to the real max
        render._OFFSET = 0
        self.assertTrue(render._handle_base_key(ord("G"), body_h=20))
        self.assertEqual(render._OFFSET, 1 << 30)

    def test_a_still_toggles_show_all(self):
        self.assertFalse(render._SHOW_ALL)
        self.assertTrue(render._handle_base_key(ord("a"), body_h=20))
        self.assertTrue(render._SHOW_ALL)
        self.assertTrue(render._handle_base_key(ord("A"), body_h=20))
        self.assertFalse(render._SHOW_ALL)

    def test_w_still_cycles_layout(self):
        self.assertEqual(render._LAYOUT, "auto")
        self.assertTrue(render._handle_base_key(ord("w"), body_h=20))
        self.assertEqual(render._LAYOUT, "wide")
        self.assertTrue(render._handle_base_key(ord("w"), body_h=20))
        self.assertEqual(render._LAYOUT, "narrow")


class ScrollIsolationTest(ScrollEnv):
    """§7.1의 결정된 경계 — 4개가 무엇이 지켜지고 무엇이 의도적으로 바뀌는지를 고정한다."""

    def test_base_mode_keeps_arrow_keys_when_nothing_is_selected(self):
        """★ 회귀 예산 0의 실제 범위 — 아무것도 선택하지 않은 사용자(kill을 쓰지 않는 전
        사용자)는 방향키를 100% 그대로 유지한다."""
        self.assertFalse(render._SELECT_MODE)
        render._OFFSET = 10
        self.assertTrue(render._handle_base_key(curses.KEY_UP, body_h=20))
        self.assertEqual(render._OFFSET, 9, "base 모드 방향키가 스크롤에 도달하지 않았다")

    def test_select_mode_intentionally_takes_arrow_keys(self):
        """§7.1 — 선택 모드의 방향키 커서는 v8부터의 기존 계약이며 회귀가 아니다. 클릭이
        선택 모드를 켜면(v9) 방향키가 커서로 가는 것도 같은 계약의 연장."""
        render._SELECT_MODE = True
        render._CURSOR_ID = None
        render._OFFSET = 10
        render._handle_select_key(curses.KEY_UP)         # select-mode handler owns this key
        self.assertEqual(render._OFFSET, 10, "선택 모드에서 방향키가 스크롤도 움직였다 — 이중 반응")

    def test_mouse_click_does_not_disturb_scroll_offset(self):
        """★ Step 2 회귀 0 — 마우스 클릭(선택 이동/해제 어느 쪽이든)이 _OFFSET을 흔들지 않는다."""
        render._OFFSET = 7
        render._TOGGLE_ROWS = {}
        render._CLICK_ROWS = {}
        render._handle_mouse(0, 0)          # click outside any row → deselect path
        self.assertEqual(render._OFFSET, 7)

    def test_prompt_does_not_leak_keys_to_scroll(self):
        """render.py:2592 — a pending confirmation swallows ALL keys; a stray arrow key must
        never reach _handle_base_key while _PROMPT is up (that dispatch is _loop's job, but
        the invariant this test pins is that _handle_prompt_key itself never touches _OFFSET)."""
        render._PROMPT = {"stage": "confirm",
                          "entry": {"pid": 1, "proc_start": "1", "state": "unused",
                                   "status": None, "label": "x", "kind": "session"}}
        render._OFFSET = 3
        from unittest import mock
        with mock.patch.object(render, "_do_kill"):
            render._handle_prompt_key(curses.KEY_UP)      # not a recognized prompt key
        self.assertEqual(render._OFFSET, 3, "프롬프트 처리 경로가 스크롤 오프셋을 건드렸다")
        render._PROMPT = None


if __name__ == "__main__":
    unittest.main()
