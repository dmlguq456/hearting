"""The explorer paints a frame in runs of one attribute, cell for cell as before.

The user's explorer spent about half a core on its main thread while idle (2026-11-04):
every 100 ms tick wrote each of the screen's ~10k cells with its own ``addstr`` and asked the
cell width of each character through a per-call import. Painting is now one ``addstr`` per
run of cells that share an attribute, and character widths are remembered; the cells that
reach the terminal must not change.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render                                             # noqa: E402
from tools.fleet.explorer import grid, output                        # noqa: E402
from tools.fleet.explorer.frame import draw                          # noqa: E402
from tools.fleet.explorer.state import NavState                      # noqa: E402
from tools.fleet.tests import explorer_fixture as ef                 # noqa: E402


class Screen:
    """A curses window stand-in that keeps the cells it was given."""

    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.cells = {}
        self.calls = 0

    def getmaxyx(self):
        return self.rows, self.cols

    def erase(self):
        self.cells.clear()

    def addstr(self, y, x, text, attr=0):
        self.calls += 1
        for char in text:
            self.cells[(y, x)] = (char, attr)
            x += max(1, output.cell_width(char))

    def refresh(self):
        pass


def per_cell(screen, frame, stale):
    """The painting before this change, one addstr per cell, as the reference."""
    spin = render._SPIN[int(render.time.time() * 10) % len(render._SPIN)]
    for y, row in enumerate(frame.cells[:screen.rows]):
        for x, (glyph, key, tint) in enumerate(row[:screen.cols]):
            if glyph == "":
                continue
            if not stale and glyph == "⠏" and key in render._EXPLORER_SPIN_KEYS:
                glyph = spin
            attr = render._key_attr(key, tint)
            if stale:
                attr |= getattr(render.curses, "A_DIM", 0)
            screen.addstr(y, x, glyph, attr)


class DrawRunsTest(unittest.TestCase):
    def test_new_frames_reuse_unchanged_unicode_rows(self):
        from types import SimpleNamespace
        from importlib import import_module
        paint_grid = import_module(render.__package__ + '.explorer.grid')
        stationary = (('한','soft',None),('', 'soft',None),(' ',None,None),(' ',None,None))
        before=SimpleNamespace(cells=(stationary,(('a','soft',None),(' ',None,None))))
        after=SimpleNamespace(cells=(tuple(list(stationary)),(('b','soft',None),(' ',None,None))))
        with mock.patch.object(render,'_EXPLORER_PAINT_CACHE',None):
            original=render._explorer_paint_runs(before,2,4)
            with mock.patch.object(paint_grid,'run_safe',side_effect=AssertionError('recompiled unchanged Unicode row')):
                reused=render._explorer_paint_runs(after,2,4)
            self.assertEqual(tuple(run for run in original if run[0]==0),
                             tuple(run for run in reused if run[0]==0))
            self.assertTrue(any(run[0]==1 and run[2].startswith('b') for run in reused))
            self.assertLessEqual(render._EXPLORER_PAINT_CACHE[5],64*1024)

    def test_same_window_repaints_only_animation_and_still_refreshes_input_geometry(self):
        frame = draw(ef.demo_model(detail=True), NavState(), 140, 45)
        screen = Screen(45, 140)
        with mock.patch.object(render, "_EXPLORER_DRAW_CACHE", None), \
                mock.patch.object(render, "_explorer_hit_geometry") as hit, \
                mock.patch.object(render, "_key_attr", return_value=0), \
                mock.patch.object(render.time, "time", return_value=0.0):
            render._draw_explorer_frame(screen, frame, None)
            full = screen.calls
            with mock.patch.object(render.time, "time", return_value=0.1):
                reference = Screen(45, 140)
                per_cell(reference, frame, False)
                render._draw_explorer_frame(screen, frame, None)
            self.assertEqual(screen.cells, reference.cells)
            self.assertLess(screen.calls - full, full // 4)
            self.assertEqual(hit.call_count, 2)
            with mock.patch.object(render, "_key_attr", return_value=1):
                before = screen.calls
                render._draw_explorer_frame(screen, frame, None)
                self.assertEqual(screen.calls - before, full)
            before = screen.calls
            render._draw_explorer_frame(screen, frame, None, stale=True)
            self.assertEqual(screen.calls - before, full)

    def test_unchanged_frame_reuses_runs_but_spinner_dim_and_resize_stay_live(self):
        frame = draw(ef.demo_model(detail=True), NavState(), 140, 45)
        with mock.patch.object(render, "_EXPLORER_PAINT_CACHE", None), \
                mock.patch.object(render, "_explorer_hit_geometry"), \
                mock.patch.object(render, "_key_attr", return_value=0):
            first = Screen(45, 140)
            render._draw_explorer_frame(first, frame, None)
            compiled = render._EXPLORER_PAINT_CACHE
            with mock.patch.object(grid, "run_safe", side_effect=AssertionError("recompiled")), \
                    mock.patch.object(render.time, "time", return_value=0.5):
                live, stale = Screen(45, 140), Screen(45, 140)
                render._draw_explorer_frame(live, frame, None)
                render._draw_explorer_frame(stale, frame, None, stale=True)
                self.assertIs(render._EXPLORER_PAINT_CACHE, compiled)
            reference = Screen(45, 140)
            with mock.patch.object(render.time, "time", return_value=0.0):
                per_cell(reference, frame, True)
            self.assertEqual(stale.cells, reference.cells)
            render._draw_explorer_frame(Screen(24, 80), frame, None)
            self.assertIsNot(render._EXPLORER_PAINT_CACHE, compiled)

    def test_runs_paint_the_same_cells_with_far_fewer_writes(self):
        model = ef.demo_model(detail=True)
        states = (NavState(), NavState(view="ROUTES", title="ROUTES"),
                  NavState(view="PEERS", title="PEERS"), NavState(view="HISTORY", title="HISTORY"))
        with mock.patch.object(render, "_explorer_hit_geometry"), \
             mock.patch.object(render.time, "time", return_value=0.0), \
             mock.patch.object(render, "_key_attr", side_effect=lambda key, tint: hash((key, tint)) & 0xffff):
            for state in states:
                for cols, rows in ((140, 45), (200, 60)):
                    for stale in (False, True):
                        with self.subTest(view=state.view, cols=cols, stale=stale):
                            frame = draw(model, state, cols, rows, beat=1)
                            reference, runs = Screen(rows, cols), Screen(rows, cols)
                            per_cell(reference, frame, stale)
                            render._draw_explorer_frame(runs, frame, None, stale=stale)
                            self.assertEqual(runs.cells, reference.cells)
                            self.assertLess(runs.calls * 4, reference.calls)

    def test_character_widths_are_unchanged_and_remembered(self):
        for text in ("a", "가", "⏳", "⠏", "é", "​", "abc가나"):
            self.assertEqual(grid.cell_width(text), output.cell_width(text), text)
        with mock.patch.object(grid, "_text_width", side_effect=AssertionError("asked again")):
            self.assertEqual(grid.cell_width("가"), 2)


RISKY = ("A" + "\u1100\u1161\u11a8" + "X|", "B" + "\u3248" + "Y|", "C" + "\u200e" + "Z|",
         "D" + "\x7f" + "W|", "E" + "가🤖●…⏳⠏─│" + "V|")


def _ncurses_child(out):
    """In a private pty: paint each frame both ways with real ncurses and keep every cell."""
    import curses
    import fcntl
    import json
    import locale
    import struct
    import termios
    locale.setlocale(locale.LC_ALL, "")
    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 200, 0, 0))
    from tools.fleet.explorer.grid import Grid

    class Frame:
        def __init__(self, cells):
            self.cells, self.lines = cells, []

    model = ef.demo_model(detail=True)
    frames = [draw(model, state, 120, 40, beat=1)
              for state in (NavState(), NavState(view="PEERS", title="PEERS"),
                            NavState(view="HISTORY", title="HISTORY"))]
    risky = Grid(40, len(RISKY))
    for y, text in enumerate(RISKY):
        risky.put(0, y, text)
    frames.append(Frame(risky.cells))

    def cells(win, rows, cols):
        return [[win.inch(y, x) for x in range(cols)] for y in range(rows)]

    def main(stdscr):
        result = []
        for frame in frames:
            rows, cols = len(frame.cells), len(frame.cells[0])
            win = curses.newwin(rows, cols, 0, 0)
            win.erase()
            for y, row in enumerate(frame.cells):          # the painting before runs
                for x, (glyph, key, tint) in enumerate(row):
                    if glyph == "":
                        continue
                    if glyph == "⠏" and key in render._EXPLORER_SPIN_KEYS:
                        glyph = render._SPIN[0]
                    try:
                        win.addstr(y, x, glyph, render._key_attr(key, tint))
                    except curses.error:
                        pass
            before = cells(win, rows, cols)
            render._draw_explorer_frame(win, frame, None)
            result.append(before == cells(win, rows, cols))
        with open(out, "w") as stream:
            json.dump(result, stream)

    with mock.patch.object(render, "_explorer_hit_geometry"), \
         mock.patch.object(render.time, "time", return_value=0.0), \
         mock.patch.object(render, "_key_attr", side_effect=lambda key, tint: (
             render.curses.A_BOLD if hash((key, tint)) % 2 else 0)):
        curses.wrapper(main)


class NcursesRunsTest(unittest.TestCase):
    def test_real_ncurses_puts_every_character_where_per_cell_painting_did(self):
        """Fleet's width and the terminal's can disagree (decomposed jamo, U+3248, LRM, DEL)."""
        import json
        import pty
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as temp:
            out = os.path.join(temp, "cells.json")
            pid, fd = pty.fork()
            if pid == 0:
                os.environ.update(TERM="xterm-256color", LC_ALL="C.UTF-8")
                try:
                    _ncurses_child(out)
                except BaseException as error:  # noqa: BLE001 - reported through the file
                    with open(out + ".error", "w") as stream:
                        stream.write(repr(error))
                finally:
                    os._exit(0)
            deadline = time.time() + 60
            while time.time() < deadline:
                try:
                    if not os.read(fd, 65536):
                        break
                except OSError:
                    break
            os.waitpid(pid, 0)
            os.close(fd)
            if not os.path.exists(out):
                error = out + ".error"
                reason = open(error).read() if os.path.exists(error) else "no result"
                if "locale" in reason:
                    self.skipTest("no UTF-8 locale here: " + reason)
                self.fail(reason)
            with open(out) as stream:
                self.assertEqual(json.load(stream), [True, True, True, True])


if __name__ == "__main__":
    unittest.main()
