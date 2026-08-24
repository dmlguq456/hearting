"""F-27 v9 — mouse as a first-class control path (prd.md:279-284).

Hermetic by construction, per plan.md §0: curses.getmouse() is stubbed by calling
_handle_mouse(mx, my) directly (a curses-free pure function) or by driving _draw() against a
FakeScreen with curses.doupdate() patched out. No test here signals a real session — kill
paths are verified with mock spies on control.kill_target / render._do_kill, and every action
log lands in a temp FLEET_ACTION_STATE_DIR (ActionLogEnv, shared with test_f27_control.py).
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import control, render                                # noqa: E402
from fleet.model import Session                                  # noqa: E402


class FakeScreen:
    """The minimal stdscr surface _draw touches. No real curses window is ever created —
    _draw's only curses-module call (doupdate) is patched out at every call site below."""

    def __init__(self, h=24, w=168):
        self.h, self.w = h, w
        self.erase_count = 0

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.erase_count += 1

    def addstr(self, row, col, text, attr=0):
        pass

    def chgat(self, y, x, n, attr):
        pass

    def noutrefresh(self):
        pass


class MouseEnv(unittest.TestCase):
    """Shared setup: isolated action log dir + full render-module state reset every test."""

    def setUp(self):
        self.state = tempfile.mkdtemp()
        self._prev = os.environ.get("FLEET_ACTION_STATE_DIR")
        os.environ["FLEET_ACTION_STATE_DIR"] = self.state
        render.reset_selection()
        render._TOGGLE_ROWS = {}
        render._CLICK_ROWS = {}
        render._PROMPT_HITS = []
        render._OFFSET = 0

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("FLEET_ACTION_STATE_DIR", None)
        else:
            os.environ["FLEET_ACTION_STATE_DIR"] = self._prev
        render.reset_selection()
        render._TOGGLE_ROWS = {}
        render._CLICK_ROWS = {}
        render._PROMPT_HITS = []

    def _draw_once(self, sessions, jobs=None, w=168, h=24):
        scr = FakeScreen(h, w)
        with mock.patch.object(render.curses, "doupdate"):
            render._draw(scr, sessions, jobs or [], "fleet", 0)
        return scr

    def _row_for(self, pid):
        for row, entry in render._CLICK_ROWS.items():
            if entry["pid"] == pid:
                return row
        raise AssertionError("pid %s never entered _CLICK_ROWS: %s" % (pid, render._CLICK_ROWS))


class MouseSelectionTest(MouseEnv):
    """Row clicks (§4.2/§4.3) — precedence rungs 2-4, plus the §4.2.1 click-map contract."""

    def _rows(self):
        unused = Session(harness="claude", pid=90001, cwd="/x", slug="a",
                         liveness="unused", elapsed_min=10)
        unused.proc_start = "111"
        working = Session(harness="claude", pid=90002, cwd="/y", slug="b",
                          liveness="working", title="live")
        working.proc_start = "222"
        return [unused, working]

    def test_each_refresh_erases_the_complete_previous_frame(self):
        """A terminal row that disappears cannot leave an old-format or input fragment."""
        scr = FakeScreen()
        with mock.patch.object(render.curses, "doupdate"):
            render._draw(scr, self._rows(), [], "fleet", 0)
            render._draw(scr, [], [], "fleet", 0)
        self.assertEqual(scr.erase_count, 2)

    def test_row_click_selects_and_highlights(self):
        self._draw_once(self._rows())
        row = self._row_for(90001)
        self.assertTrue(render._handle_mouse(5, row))
        self.assertTrue(render._SELECT_MODE)
        self.assertEqual(render._CURSOR_ID, (90001, "111"))
        self.assertIsNone(render._PROMPT, "첫 클릭은 선택이지 kill 요청이 아니다")

    def test_first_click_works_from_base_mode(self):
        """★ §4.2.1 — _CLICK_ROWS is keyed off _SELECTABLE, not _live_targets(), so the very
        first click (base mode, _SELECT_MODE False) must already see a target."""
        self._draw_once(self._rows())
        self.assertFalse(render._SELECT_MODE)
        row = self._row_for(90002)
        self.assertTrue(render._handle_mouse(0, row))
        self.assertTrue(render._SELECT_MODE)
        self.assertEqual(render._CURSOR_ID, (90002, "222"))

    def test_second_click_same_row_raises_kill_prompt(self):
        self._draw_once(self._rows())
        row = self._row_for(90001)
        render._handle_mouse(0, row)
        self._draw_once(self._rows())               # §4.4.1 — redraw between clicks
        row = self._row_for(90001)
        render._handle_mouse(0, row)
        self.assertIsNotNone(render._PROMPT)
        self.assertEqual(render._PROMPT["stage"], "confirm")
        self.assertEqual(render._PROMPT["entry"]["pid"], 90001)

    def test_second_click_does_not_reach_kill_target(self):
        self._draw_once(self._rows())
        row = self._row_for(90001)
        render._handle_mouse(0, row)
        self._draw_once(self._rows())
        row = self._row_for(90001)
        with mock.patch.object(control, "kill_target") as k:
            render._handle_mouse(0, row)
            k.assert_not_called()                    # a kill REQUEST is not a kill

    def test_click_other_row_moves_selection(self):
        self._draw_once(self._rows())
        render._handle_mouse(0, self._row_for(90001))
        self.assertEqual(render._CURSOR_ID, (90001, "111"))
        render._handle_mouse(0, self._row_for(90002))
        self.assertEqual(render._CURSOR_ID, (90002, "222"))
        self.assertIsNone(render._PROMPT)

    def test_click_outside_any_row_deselects(self):
        self._draw_once(self._rows())
        render._handle_mouse(0, self._row_for(90001))
        self.assertTrue(render._SELECT_MODE)
        render._handle_mouse(0, 999)                  # far outside any drawn row
        self.assertFalse(render._SELECT_MODE)

    def test_toggle_row_click_still_toggles_show_all(self):
        """rung 2 — checked ahead of the row map (§4.3): a toggle row is never a selectable
        row, so the two maps never overlap. Fixture-injects _TOGGLE_ROWS directly (the exact
        real-board condition that populates it is out of this step's scope)."""
        before = render._SHOW_ALL
        render._TOGGLE_ROWS = {5: True}
        try:
            self.assertTrue(render._handle_mouse(2, 5))
            self.assertEqual(render._SHOW_ALL, not before)
        finally:
            render.set_show_all(before)

    def test_excluded_pid_click_does_not_select(self):
        """★ §4.2.1 — is_excluded() is applied at CLICK time, once; fleet's own pid must
        never become selectable via mouse either."""
        me = Session(harness="claude", pid=os.getpid(), cwd="/z", slug="self",
                    liveness="unused", elapsed_min=1)
        self._draw_once([me])
        row = self._row_for(os.getpid())
        self.assertTrue(render._handle_mouse(0, row))
        self.assertFalse(render._SELECT_MODE)
        self.assertIsNone(render._CURSOR_ID)

    def test_click_map_costs_nothing_per_tick(self):
        """★ the perf guard behind §4.2.1's decision: as long as nothing is selected yet,
        _draw must never call control.is_excluded() — that path is reserved for click time."""
        with mock.patch.object(control, "is_excluded") as spy:
            self._draw_once(self._rows())
            spy.assert_not_called()

    def test_getmouse_exception_does_not_crash(self):
        """★ 🟡5 — a bare `except: my = None` still referenced a possibly-unbound `mx`.
        _getmouse_xy() collapses both into a matched (None, None) pair."""
        with mock.patch.object(render.curses, "getmouse", side_effect=Exception("no event")):
            mx, my = render._getmouse_xy()
        self.assertIsNone(mx)
        self.assertIsNone(my)


class MousePromptTest(MouseEnv):
    """Footer button clicks (§4.4/§4.5) — the coordinate-inversion safety design."""

    E_UNUSED = {"line": 0, "kind": "session", "pid": 1168514, "proc_start": "3918896",
                "sid": "s", "state": "unused", "status": "idle", "cwd": "/x", "slug": "a",
                "label": "agent-setting-17", "harness": "claude", "source": None}
    E_WORKING = dict(E_UNUSED, state="working", status="busy", pid=2119125,
                     label="Fix breadcrumb cutoff in wide layout")

    def _prompt_draw(self, prompt, w=168, h=24):
        render._PROMPT = prompt
        scr = FakeScreen(h, w)
        with mock.patch.object(render.curses, "doupdate"):
            render._draw(scr, [], [], "fleet", 0)
        return scr

    def _hit(self, action):
        for row, x0, x1, act in render._PROMPT_HITS:
            if act == action:
                return row, x0, x1
        raise AssertionError("no %r hitbox in %s" % (action, render._PROMPT_HITS))

    def _maybe_hit(self, action):
        """None when this rung dropped its buttons (§4.5 — width too tight, keyboard hint
        kept instead). A dropped button cannot overlap anything, so callers treat None as
        vacuously safe rather than a failure."""
        for row, x0, x1, act in render._PROMPT_HITS:
            if act == action:
                return row, x0, x1
        return None

    def test_kill_hitbox_confirms(self):
        self._prompt_draw({"stage": "confirm", "entry": self.E_UNUSED})
        with mock.patch.object(render, "_do_kill") as d:
            row, x0, _x1 = self._hit("kill")
            render._handle_mouse(x0, row)
            d.assert_called_once()
            self.assertEqual(d.call_args[0][1], "single")
        self.assertIsNone(render._PROMPT)

    def test_cancel_hitbox_cancels(self):
        self._prompt_draw({"stage": "confirm", "entry": self.E_UNUSED})
        with mock.patch.object(render, "_do_kill") as d:
            row, x0, _x1 = self._hit("cancel")
            render._handle_mouse(x0, row)
            d.assert_not_called()
        self.assertIsNone(render._PROMPT)

    def test_click_elsewhere_while_prompt_is_swallowed(self):
        """★ rung 1 — a stray click while prompted must resolve NEITHER decision."""
        self._prompt_draw({"stage": "confirm", "entry": self.E_UNUSED})
        with mock.patch.object(render, "_do_kill") as d:
            self.assertTrue(render._handle_mouse(0, 0))   # far from any hitbox
            d.assert_not_called()
        self.assertIsNotNone(render._PROMPT, "빗나간 클릭이 프롬프트를 지웠다")

    def test_working_session_click_needs_second_confirm(self):
        """working/busy sessions must double-confirm through the mouse path too (prd.md:281)."""
        self._prompt_draw({"stage": "confirm", "entry": self.E_WORKING})
        with mock.patch.object(render, "_do_kill") as d:
            row, x0, _x1 = self._hit("kill")
            render._handle_mouse(x0, row)
            d.assert_not_called()                          # first click only escalates
            self.assertEqual(render._PROMPT["stage"], "confirm2")

    def test_confirm_to_confirm2_transition_repopulates_hits(self):
        """★★ §4.4.1 — the staleness guard. Clicking confirm's [kill] escalates to confirm2;
        a redraw (which _loop always performs before the next getch, §4.4.1's regime
        invariant) must repopulate _PROMPT_HITS for confirm2 before a second click can land —
        so the exact same coordinate, clicked again, can never reach a kill."""
        self._prompt_draw({"stage": "confirm", "entry": self.E_WORKING})
        kill_row, kill_x0, _ = self._hit("kill")
        with mock.patch.object(render, "_do_kill") as d:
            render._handle_mouse(kill_x0, kill_row)        # click 1: confirm -> confirm2
            self.assertEqual(render._PROMPT["stage"], "confirm2")
            self._prompt_draw(render._PROMPT)              # the redraw _loop always does
            render._handle_mouse(kill_x0, kill_row)        # click 2, same spot as click 1
            d.assert_not_called()
        # same-spot double-click never reaches a kill, whether it swallows or cancels — the
        # controlling assertion is d.assert_not_called() above.

    def test_kill_hitboxes_do_not_overlap_across_stages(self):
        """★★ §4.4 — parameterized over width x rung combination. A rung that dropped its
        buttons entirely (§4.5, width too tight) is vacuously non-overlapping."""
        checked = 0
        for w in (60, 120, 168):
            self._prompt_draw({"stage": "confirm", "entry": self.E_WORKING}, w=w)
            hit1 = self._maybe_hit("kill")
            self._prompt_draw({"stage": "confirm2", "entry": self.E_WORKING}, w=w)
            hit2 = self._maybe_hit("kill")
            if hit1 is None or hit2 is None:
                continue
            _row1, k0, k1 = hit1
            _row2, k2, k3 = hit2
            checked += 1
            overlap = max(k0, k2) < min(k1, k3)
            self.assertFalse(overlap, "width=%d: confirm/confirm2 kill hitboxes overlap "
                                      "(%s vs %s)" % (w, (k0, k1), (k2, k3)))
        self.assertGreater(checked, 0, "no width had buttons in both stages to compare")

    def test_escalate_hitbox_does_not_overlap_confirm(self):
        checked = 0
        for w in (60, 120, 168):
            self._prompt_draw({"stage": "confirm", "entry": self.E_WORKING}, w=w)
            hit1 = self._maybe_hit("kill")
            self._prompt_draw({"stage": "escalate", "entry": self.E_UNUSED}, w=w)
            hit2 = self._maybe_hit("kill")
            if hit1 is None or hit2 is None:
                continue
            _row1, k0, k1 = hit1
            _row2, k2, k3 = hit2
            checked += 1
            overlap = max(k0, k2) < min(k1, k3)
            self.assertFalse(overlap, "width=%d: confirm/escalate kill hitboxes overlap"
                                      % w)
        self.assertGreater(checked, 0, "no width had buttons in both stages to compare")

    def test_hits_are_only_populated_for_the_drawn_rung(self):
        self._prompt_draw({"stage": "confirm", "entry": self.E_UNUSED})
        for _row, _x0, _x1, action in render._PROMPT_HITS:
            self.assertIn(action, ("kill", "cancel"))
        self.assertGreaterEqual(len(render._PROMPT_HITS), 2)

    def test_clipped_button_registers_no_hitbox(self):
        """★ pick()'s final rung can still overshoot an absurdly narrow width (:2286's
        `return variants[-1]`) — a partially-clipped button must never keep a live hitbox."""
        self._prompt_draw({"stage": "confirm", "entry": self.E_UNUSED}, w=8)
        for row, x0, x1, _action in render._PROMPT_HITS:
            self.assertLessEqual(x1, 7, "clipped button kept a hitbox past the edge")

    def test_prompt_fits_at_60_columns(self):
        for stage, entry in (("confirm", self.E_UNUSED), ("confirm", self.E_WORKING),
                             ("confirm2", self.E_WORKING), ("escalate", self.E_UNUSED)):
            segs = render._prompt_segs({"stage": stage, "entry": entry}, 60)
            got = sum(render._dw(t) for t, _k in segs)
            self.assertLessEqual(got, 60, "%s prompt is %d cells at width 60" % (stage, got))


class NoAutomaticControlTest(unittest.TestCase):
    """prd.md:278·452 — zero automatic control, re-checked for the mouse handler specifically.
    _handle_mouse only ever reaches control.kill_target by replaying a keystroke through
    _handle_prompt_key (the one path, §4.1) — never directly."""

    def test_handle_mouse_never_calls_kill_target_directly(self):
        import inspect
        src = inspect.getsource(render._handle_mouse)
        self.assertNotIn("kill_target(", src)


if __name__ == "__main__":
    unittest.main()
