"""Keep Fleet's frame loop running when its terminal stops taking the screen.

A terminal that stops reading (a VS Code remote terminal whose window went away, a
stopped tty, a stalled link) fills the pty output queue (about 20 KB on Linux) and the
next curses write blocks. Fleet's whole loop used to stop on that one write: no input,
no collection, no install follow, until someone read the terminal again.

``ScreenGuard`` stands between render's loop and the curses screen:

* a frame's write runs on one helper thread and the loop waits at most ``WRITE_BUDGET``
  for it; a write still blocked after that finishes by itself once the terminal reads;
* while that write is pending, or the terminal says it cannot take a byte, frames are
  skipped without touching curses (curses is not thread-safe) and input reads wait;
* when output comes back after a stall of ``REPAINT_AFTER`` or more, the next frame
  repaints the whole screen;
* ``hold_reason`` lets install follow wait instead of exec'ing a new release into a
  terminal that cannot take its first frame.

Only ``window.refresh()`` gets this protection: CPython releases the GIL around it, while
``curses.doupdate()`` holds the GIL for as long as it blocks. The explorer draws through
``refresh()``; the group/process screen still flushes with ``doupdate()`` and only gets
the skip-when-unwritable part.

Nothing here closes the window, signals a process, or drops terminal output. A hangup
still ends Fleet exactly as before.
"""
from contextlib import contextmanager
import functools
import select
import sys
import threading
import time
import unicodedata

try:
    import curses
except ImportError:  # pragma: no cover - platforms without curses never run the live loop
    curses = None

WRITE_BUDGET = 0.25      # a pty write with room returns in microseconds
REPAINT_AFTER = 5.0      # a stall this long means the terminal went away; repaint everything
RECOVERED_NOTE_SEC = 60.0
STALLED = "화면 출력이 막혀 있음"


def _cells(text):
    return sum(0 if unicodedata.combining(char)
               else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in text)


def _span(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}초"
    if seconds < 3600:
        return f"{seconds // 60}분"
    return f"{seconds // 3600}시간"


class ScreenGuard:
    """A curses screen stand-in whose terminal writes cannot hold the frame loop."""

    def __init__(self, *, budget=WRITE_BUDGET, repaint_after=REPAINT_AFTER, note=None,
                 writable=None, clock=time.monotonic):
        self.note = note                    # optional callable -> short status text or None
        self.skipped = 0                    # frames not written (diagnostics and tests)
        self._budget = float(budget)
        self._repaint_after = float(repaint_after)
        self._probe = writable
        self._clock = clock
        self._screen = None
        self._fd = None
        self._request = threading.Event()
        self._done = threading.Event()
        self._done.set()
        self._closing = False
        self._worker = None
        self._error = None
        self._stalled_since = None
        self._recovered = None              # (when, seconds) of the last long stall
        self._repaint = False
        self._skip = False
        self._delay_ms = -1
        self._rows = self._cols = 0
        self._frame_note = None
        self._last_row_end = 0

    # -- wiring ---------------------------------------------------------------------------------

    def attach(self, screen, fd=None):
        self._screen = screen
        if fd is None:
            try:
                fd = sys.stdout.fileno()
            except (AttributeError, OSError, ValueError):
                fd = None
        self._fd = fd
        return self

    @contextmanager
    def around(self, render):
        """Give ``render._loop`` this guard as its screen while the live view runs.

        ``render.run_live`` owns terminal setup and calls ``curses.wrapper(_loop, ...)``;
        wrapping ``_loop`` keeps that setup in one place.
        """
        loop = render._loop

        @functools.wraps(loop)
        def guarded(stdscr, *args, **kwargs):
            screen = self.attach(stdscr)
            try:
                return loop(screen, *args, **kwargs)
            finally:
                self.close()

        render._loop = guarded
        try:
            yield self
        finally:
            render._loop = loop

    def close(self):
        """Let a pending write finish before curses restores the terminal, then stop the writer."""
        try:
            self._done.wait()
        finally:
            self._closing = True
            self._request.set()

    def hold_reason(self):
        """Why a new release should not take this terminal now, or None."""
        if self._screen is None:
            return None
        if not self._done.is_set() or not self._takes_bytes():
            return STALLED
        return None

    # -- state ----------------------------------------------------------------------------------

    def _takes_bytes(self):
        if self._probe is not None:
            return bool(self._probe())
        if self._fd is None:
            return True
        try:
            return bool(select.select((), (self._fd,), (), 0)[1])
        except (OSError, ValueError, TypeError):
            return True                      # unknown: behave as before

    def _stalled(self):
        if self._stalled_since is None:
            self._stalled_since = self._clock()

    def _ready(self):
        """Whether this frame may touch curses and write; keeps the stall bookkeeping."""
        if not self._done.is_set():
            self._stalled()
            return False
        error, self._error = self._error, None
        if error is not None:
            raise error                      # a late write failure surfaces as before
        if not self._takes_bytes():
            self._stalled()
            return False
        if self._stalled_since is not None:
            lasted = self._clock() - self._stalled_since
            self._stalled_since = None
            if lasted >= self._repaint_after:
                self._repaint = True
                self._recovered = (self._clock(), lasted)
        return True

    def _work(self):
        while True:
            self._request.wait()
            self._request.clear()
            if self._closing:
                return
            try:
                self._screen.refresh()
            except Exception as exc:         # handed to the loop thread
                self._error = exc
            finally:
                self._done.set()

    def _write(self):
        if self._worker is None:
            self._worker = threading.Thread(target=self._work, name="fleet-screen-write",
                                            daemon=True)
            self._worker.start()
        self._done.clear()
        self._request.set()
        if self._done.wait(self._budget):
            error, self._error = self._error, None
            if error is not None:
                raise error
            return
        self._stalled()

    # -- the screen surface render uses --------------------------------------------------------

    def erase(self):
        self._skip = not self._ready()
        if self._skip:
            self.skipped += 1
            return None
        self._rows, self._cols = self._screen.getmaxyx()
        self._frame_note = self._note_text()
        self._last_row_end = 0
        return self._screen.erase()

    def addstr(self, *args):
        if self._skip or not self._done.is_set():
            return None
        if self._frame_note and len(args) >= 3 and args[0] == self._rows - 1:
            body = str(args[2]).rstrip()
            if body and isinstance(args[1], int):
                self._last_row_end = max(self._last_row_end, args[1] + _cells(body))
        return self._screen.addstr(*args)

    def chgat(self, *args):
        if self._skip or not self._done.is_set():
            return None
        return self._screen.chgat(*args)

    def noutrefresh(self):
        if self._skip or not self._done.is_set():
            return None
        self._draw_note()
        return self._screen.noutrefresh()

    def refresh(self):
        if self._skip or not self._done.is_set():
            return None
        self._draw_note()
        if not self._takes_bytes():
            # Settle the frame into curses' own buffer so the next getch has nothing to flush.
            self._stalled()
            self.skipped += 1
            return self._screen.noutrefresh()
        if self._repaint:
            self._repaint = False
            self._screen.clearok(True)
        self._write()
        return None

    def getch(self, *args):
        if not self._done.is_set():
            delay = self._delay_ms
            self._done.wait(self._budget if delay is None or delay < 0 else delay / 1000.0)
            return -1
        return self._screen.getch(*args)

    def timeout(self, delay):
        self._delay_ms = delay
        return self._screen.timeout(delay)

    def getmaxyx(self):
        return self._screen.getmaxyx()

    def __getattr__(self, name):
        screen = self.__dict__.get("_screen")
        if screen is None:
            raise AttributeError(name)
        return getattr(screen, name)

    # -- the one-line note ----------------------------------------------------------------------

    def _note_text(self):
        parts = []
        recovered = self._recovered
        if recovered is not None:
            when, lasted = recovered
            if self._clock() - when < RECOVERED_NOTE_SEC:
                parts.append(f"화면 출력이 {_span(lasted)} 막혔다가 다시 그림")
            else:
                self._recovered = None
        if callable(self.note):
            try:
                extra = self.note()
            except Exception:
                extra = None
            if extra:
                parts.append(str(extra))
        return " · ".join(parts) or None

    def _draw_note(self):
        """Put the note in the blank right end of the bottom row; never over drawn text."""
        text = self._frame_note
        if not text or self._rows <= 0:
            return
        x = self._cols - 1 - _cells(text)    # the bottom-right cell stays unwritten
        if x < self._last_row_end + 2:
            return
        try:
            self._screen.addstr(self._rows - 1, x, text, getattr(curses, "A_DIM", 0))
        except Exception:
            pass
