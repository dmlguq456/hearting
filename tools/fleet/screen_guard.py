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
  terminal that cannot take its first frame;
* a frame that never reached the terminal does not become the click map: render's
  painted-frame globals stay on the last frame the person could see.

Only ``window.refresh()`` gets this protection: CPython releases the GIL around it, while
``curses.doupdate()`` holds the GIL for as long as it blocks. The explorer draws through
``refresh()``; the group/process screen still flushes with ``doupdate()`` and only gets
the skip-when-unwritable part.

Quitting is the one exception to keeping every byte: ``settle`` waits for the frame in
flight before anything is written past curses or curses restores the terminal, and a
terminal that has not taken that frame within ``QUIT_GRACE`` (or a second Ctrl-C) gets the
unread frame dropped from its output queue, so q or Ctrl-C never waits on a terminal that
stopped reading. Nothing here closes the window or signals a process. A hangup still ends
Fleet exactly as before.
"""
from contextlib import contextmanager
import functools
import select
import sys
import threading
import time
import unicodedata

from .config import ui_text

try:
    import curses
except ImportError:  # pragma: no cover - platforms without curses never run the live loop
    curses = None
try:
    import termios
except ImportError:  # pragma: no cover - no tty control: quitting waits for the terminal
    termios = None

WRITE_BUDGET = 0.25      # a pty write with room returns in microseconds
REPAINT_AFTER = 5.0      # a stall this long means the terminal went away; repaint everything
QUIT_GRACE = 1.0         # quitting waits this long for a frame the terminal has not taken
RECOVERED_NOTE_SEC = 60.0
# render globals that describe the painted frame: what a click or a navigation key is
# resolved against (``_explorer_click_current``, ``reduce(..., _EXPLORER_FRAME)``, the
# group screen's row maps). ``_draw`` resets and refills them on every pass.
CLICK_BASIS = ("_EXPLORER_FRAME", "_CLICK_ROWS", "_CLICK_SPANS", "_PROMPT_HITS",
               "_HIT_VALID", "_HIT_SIZE", "_HIT_SNAPSHOT_ID",
               "_TOGGLE_ROWS", "_TOGGLE_SPANS", "_FOLD_ROWS", "_FOLD_SPANS",
               "_REPO_FOLD_ROWS", "_REPO_FOLD_SPANS",
               "_CURRENT_BODY_H", "_CURRENT_BODY_TOP", "_CURRENT_BODY_COUNT")


def _cells(text):
    return sum(0 if unicodedata.combining(char)
               else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in text)


def _span(seconds):
    seconds = max(0, int(seconds))
    count, unit = ((seconds, "second") if seconds < 60 else (seconds // 60, "minute") if seconds < 3600
                   else (seconds // 3600, "hour"))
    return ui_text(f"fleet.span.{unit}" if count == 1 else f"fleet.span.{unit}s", count=count)


class ScreenGuard:
    """A curses screen stand-in whose terminal writes cannot hold the frame loop."""

    def __init__(self, *, budget=WRITE_BUDGET, repaint_after=REPAINT_AFTER, note=None,
                 writable=None, clock=time.monotonic):
        self.note = note                    # optional callable -> short status text or None
        self.skipped = 0                    # frames not written (diagnostics and tests)
        self.frame_skipped = False          # the latest frame did not go to the terminal
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
        self._repaint_pending = False       # the write in progress is itself a full repaint
        self._written = 0
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
        wrapping ``_loop`` keeps that setup in one place. ``render._draw`` is wrapped too:
        when the guard skips a frame, render's click map goes back to the frame on screen.
        """
        loop, draw = render._loop, render._draw

        @functools.wraps(loop)
        def guarded(stdscr, *args, **kwargs):
            screen = self.attach(stdscr)
            try:
                return loop(screen, *args, **kwargs)
            finally:
                self.close()

        @functools.wraps(draw)
        def painted(*args, **kwargs):
            shown = {name: getattr(render, name) for name in CLICK_BASIS if hasattr(render, name)}
            self.frame_skipped = False
            try:
                return draw(*args, **kwargs)
            finally:
                if self.frame_skipped:
                    for name, value in shown.items():
                        setattr(render, name, value)

        render._loop, render._draw = guarded, painted
        try:
            yield self
        finally:
            render._loop, render._draw = loop, draw

    def settle(self, grace=QUIT_GRACE):
        """Finish the frame in flight so Fleet can write past curses or let curses end.

        The writer is never abandoned inside ``refresh``: ``endwin`` or a raw write racing
        it would interleave two writers on one terminal (two threads inside curses for
        ``endwin``). A terminal that has not taken the frame within ``grace`` is not
        reading; the frame nobody can see is dropped from its output queue until the
        writer finishes and the restore that follows has room. A Ctrl-C ends the grace.
        """
        deadline = time.monotonic() + max(0.0, float(grace))
        while True:
            try:
                done = self._done.wait(0.05)
                if done and self._takes_bytes():
                    return
                if time.monotonic() >= deadline:
                    self._discard_unread()
                    if done:
                        return
                elif done:
                    time.sleep(0.05)         # the frame is out; wait for room for the restore
            except KeyboardInterrupt:
                deadline = time.monotonic()

    def close(self):
        """Settle the pending write before curses restores the terminal, then stop the writer.

        A hangup or SIGTERM still ends the process.
        """
        self.settle()
        self._closing = True
        self._request.set()

    def hold_reason(self):
        """Why a new release should not take this terminal now, or None."""
        if self._screen is None:
            return None
        if not self._done.is_set() or not self._takes_bytes():
            return ui_text("fleet.screen.stalled")
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

    def _discard_unread(self):
        """Drop output the terminal has not read and wake the write blocked on it.

        A flush frees the queue but does not wake a writer asleep on it; restarting
        output does (and also ends an XOFF stop, which quitting should not keep).
        """
        if termios is None or self._fd is None:
            return
        try:
            termios.tcflush(self._fd, termios.TCOFLUSH)
            termios.tcflow(self._fd, termios.TCOOFF)
            termios.tcflow(self._fd, termios.TCOON)
        except (termios.error, OSError, ValueError, TypeError):
            pass

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
            # A slow repaint does not earn another one: on a link that needs longer than
            # ``REPAINT_AFTER`` for one full screen, repaints would otherwise never end.
            if lasted >= self._repaint_after and not self._repaint_pending:
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
        self._written += 1
        # The first frame is written in full, as before: render turns mouse reporting on
        # with a raw write right after it, and those bytes must not land inside the frame.
        if self._done.wait(None if self._written == 1 else self._budget):
            error, self._error = self._error, None
            if error is not None:
                raise error
            return
        self._stalled()

    # -- the screen surface render uses --------------------------------------------------------

    def erase(self):
        self._skip = not self._ready()
        self.frame_skipped = self._skip
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
            self.frame_skipped = True
            return None
        self._draw_note()
        return self._screen.noutrefresh()

    def refresh(self):
        if self._skip or not self._done.is_set():
            self.frame_skipped = True
            return None
        self._draw_note()
        if not self._takes_bytes():
            # Settle the frame into curses' own buffer so the next getch has nothing to flush.
            self._stalled()
            self.skipped += 1
            self.frame_skipped = True
            return self._screen.noutrefresh()
        self._repaint_pending = self._repaint
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
                parts.append(ui_text("fleet.screen.recovered", span=_span(lasted)))
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
