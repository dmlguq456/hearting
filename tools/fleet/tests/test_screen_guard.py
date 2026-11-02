"""A terminal that stops reading pauses Fleet's picture, never its loop or install follow."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import types
import unittest

TOOLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOLS))
from fleet import install_follow, screen_guard  # noqa: E402

CLEAR = b"\x1b[H\x1b[2J"          # xterm-256color clear: a full repaint starts with it


class FakeScreen:
    def __init__(self, rows=10, cols=40):
        self.rows, self.cols = rows, cols
        self.calls = []
        self.gate = None              # an Event the next refresh waits on (a terminal not reading)
        self.refresh_error = None
        self.writer_threads = []

    def getmaxyx(self):
        return self.rows, self.cols

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            if name == "refresh":
                self.writer_threads.append(threading.current_thread().name)
                if self.gate is not None:
                    self.gate.wait(10)
                if self.refresh_error is not None:
                    raise self.refresh_error
            if name == "getch":
                return ord("k")
            return None
        return call

    def names(self):
        return [name for name, _args in self.calls]


def frame(guard, text="hello"):
    guard.erase()
    guard.addstr(0, 0, text)
    guard.refresh()


class ScreenGuardTest(unittest.TestCase):
    def guard(self, screen, **kwargs):
        kwargs.setdefault("budget", 0.05)
        guard = screen_guard.ScreenGuard(**kwargs).attach(screen, fd=-1)
        self.addCleanup(guard.close)
        return guard

    def test_healthy_frame_is_written_by_the_writer_thread(self):
        screen = FakeScreen()
        guard = self.guard(screen, writable=lambda: True)
        frame(guard)
        self.assertEqual(screen.names(), ["erase", "addstr", "refresh"])
        self.assertEqual(screen.writer_threads, ["fleet-screen-write"])
        self.assertIsNone(guard.hold_reason())
        self.assertEqual(guard.getch(), ord("k"))

    def test_blocked_write_returns_and_later_frames_leave_curses_alone(self):
        screen = FakeScreen()
        screen.gate = threading.Event()
        guard = self.guard(screen, writable=lambda: True, repaint_after=0.0)
        guard.timeout(30)
        started = time.monotonic()
        frame(guard)
        self.assertLess(time.monotonic() - started, 1.0)
        before = list(screen.calls)
        for _ in range(3):
            frame(guard, "next")
            self.assertEqual(guard.getch(), -1)
            self.assertEqual(guard.hold_reason(), screen_guard.STALLED)
        self.assertEqual(screen.calls, before, "no curses call may race the pending write")
        self.assertEqual(guard.skipped, 3)
        screen.gate.set()
        screen.gate = None
        deadline = time.monotonic() + 5
        while guard.hold_reason() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNone(guard.hold_reason())
        frame(guard, "back")
        tail = screen.calls[len(before):]
        self.assertEqual([name for name, _args in tail],
                         ["erase", "addstr", "addstr", "clearok", "refresh"])
        self.assertEqual(tail[1], ("addstr", (0, 0, "back")))
        self.assertIn("막혔다가 다시 그림", tail[2][1][2])      # the bottom-row note
        self.assertEqual(tail[3], ("clearok", (True,)))

    def test_short_stall_does_not_repaint_everything(self):
        screen = FakeScreen()
        writable = [False]
        guard = self.guard(screen, writable=lambda: writable[0], repaint_after=60.0)
        frame(guard)
        writable[0] = True
        frame(guard)
        self.assertNotIn("clearok", screen.names())

    def test_unwritable_terminal_skips_the_frame_without_a_write(self):
        screen = FakeScreen()
        guard = self.guard(screen, writable=lambda: False)
        frame(guard)
        self.assertEqual(screen.calls, [])
        self.assertIsNone(guard._worker)
        self.assertEqual(guard.hold_reason(), screen_guard.STALLED)
        self.assertEqual(guard.getch(), ord("k"), "input still reads when no write is pending")

    def test_frame_drawn_before_output_stopped_is_settled_not_flushed(self):
        screen = FakeScreen()
        answers = iter([True, False])
        guard = self.guard(screen, writable=lambda: next(answers))
        frame(guard)
        self.assertEqual(screen.names(), ["erase", "addstr", "noutrefresh"])

    def test_late_write_failure_surfaces_on_the_loop_thread(self):
        screen = FakeScreen()
        screen.gate = threading.Event()
        screen.refresh_error = RuntimeError("fixture write failure")
        guard = self.guard(screen, writable=lambda: True)
        frame(guard)
        screen.gate.set()
        guard._done.wait(5)
        with self.assertRaisesRegex(RuntimeError, "fixture write failure"):
            guard.erase()

    def test_note_uses_only_the_blank_end_of_the_bottom_row(self):
        screen = FakeScreen(rows=5, cols=60)
        guard = self.guard(screen, writable=lambda: True, note=lambda: "v9.9.9로 넘어가기 대기")
        guard.erase()
        guard.addstr(4, 1, "hearting › OVERVIEW")
        guard.refresh()
        notes = [args for name, args in screen.calls if name == "addstr" and args[0] == 4
                 and "대기" in args[2]]
        self.assertEqual(len(notes), 1)
        y, x, text, _attr = notes[0]
        self.assertEqual(x + screen_guard._cells(text), 59)
        screen.calls.clear()
        guard.erase()
        guard.addstr(4, 1, "x" * 50)
        guard.refresh()
        self.assertFalse([args for name, args in screen.calls if name == "addstr" and "대기" in str(args)])

    def test_recovered_stall_is_named_for_a_while(self):
        clock = [100.0]
        screen = FakeScreen(rows=5, cols=80)
        writable = [False]
        guard = self.guard(screen, writable=lambda: writable[0], repaint_after=5.0,
                           clock=lambda: clock[0])
        frame(guard)
        clock[0] += 720
        writable[0] = True
        frame(guard)
        shown = [args[2] for name, args in screen.calls if name == "addstr" and args[0] == 4]
        self.assertEqual(shown, ["화면 출력이 12분 막혔다가 다시 그림"])
        clock[0] += screen_guard.RECOVERED_NOTE_SEC + 1
        screen.calls.clear()
        frame(guard)
        self.assertFalse([args for name, args in screen.calls if name == "addstr" and args[0] == 4])

    def test_close_waits_for_the_pending_write(self):
        screen = FakeScreen()
        screen.gate = threading.Event()
        guard = screen_guard.ScreenGuard(budget=0.05, writable=lambda: True).attach(screen, fd=-1)
        frame(guard)
        closed = threading.Event()
        threading.Thread(target=lambda: (guard.close(), closed.set()), daemon=True).start()
        self.assertFalse(closed.wait(0.2))
        screen.gate.set()
        self.assertTrue(closed.wait(5))

    def test_around_hands_render_loop_the_guard_only_while_it_runs(self):
        seen = []
        original = lambda stdscr, *args: seen.append((stdscr, args)) or 7  # noqa: E731
        render = types.SimpleNamespace(_loop=original)
        guard = screen_guard.ScreenGuard()
        screen = FakeScreen()
        with guard.around(render):
            self.assertIsNot(render._loop, original)
            self.assertEqual(render._loop(screen, "collector", 2), 7)
        self.assertIs(render._loop, original)
        self.assertIs(seen[0][0], guard)
        self.assertEqual(seen[0][1], ("collector", 2))
        self.assertIs(guard._screen, screen)

    def test_fleet_wires_the_guard_and_the_follower_together(self):
        from fleet import fleet as fleet_main
        observed = {}

        def run_live(collector, *_args):
            observed["loop"] = render._loop
            observed["follower"] = collector.install_follower
            return 0

        render = types.SimpleNamespace(_loop=lambda *_args: None, run_live=run_live)
        original = render._loop
        follower = install_follow.InstallFollower(Path(tempfile.gettempdir()), env={})
        guard = screen_guard.ScreenGuard()
        collector = types.SimpleNamespace()
        self.assertEqual(fleet_main._run_live(render, collector, None, "both", 2,
                                              follower=follower, guard=guard), 0)
        self.assertIsNot(observed["loop"], original)
        self.assertIs(observed["follower"], follower)
        self.assertEqual(follower.hold, guard.hold_reason)
        self.assertEqual(guard.note, follower.waiting_note)
        self.assertIs(render._loop, original)


# -- real terminals ---------------------------------------------------------------------------

def _write_progress(directory, **values):
    path = Path(directory) / "progress.json"
    temp = path.with_name("progress.tmp.%d" % threading.get_ident())
    temp.write_text(json.dumps(values))
    os.replace(temp, path)


def _append_event(directory, **values):
    with open(Path(directory) / "events.jsonl", "a", encoding="utf-8") as stream:
        stream.write(json.dumps(values, ensure_ascii=False) + "\n")


def _frames_child(directory, guarded):
    """Full-screen frames every tick: a terminal that stops reading fills within a few.

    Random cells defeat curses' scroll and repeat-character shortcuts, so every frame
    really rewrites the screen.
    """
    import curses
    import random
    guard = screen_guard.ScreenGuard(repaint_after=0.5)

    def main(stdscr):
        screen = guard.attach(stdscr) if guarded else stdscr
        curses.curs_set(0)
        screen.timeout(50)
        tick = 0
        cells = random.Random(0)
        while True:
            rows, cols = screen.getmaxyx()
            screen.erase()
            for y in range(rows - 1):
                screen.addstr(y, 0, "".join(cells.choice("abcdefghijklmnopqrstuvwxyz")
                                            for _ in range(cols - 1)))
            screen.refresh()
            key = screen.getch()
            tick += 1
            _write_progress(directory, tick=tick, skipped=guard.skipped,
                            hold=guard.hold_reason() if guarded else None)
            if key == ord("q"):
                break
        if guarded:
            guard.close()

    curses.wrapper(main)


def _explorer_child(directory, old, data, state):
    """The real explorer loop through fleet's own wiring, with a fixture collector."""
    from fleet import fleet as fleet_main, render
    from fleet.refresh import LiveSnapshot

    render._collect_memory_observed = lambda include_backlog=False: (None, "absent")
    render._collect_governor = lambda: None
    render.set_explorer_mode(True)
    collects = [0]

    def collect(harness_filter=None, **_options):
        collects[0] += 1
        _write_progress(directory, collects=collects[0])
        return LiveSnapshot()

    collect.restart_argv = ["--explorer"]

    class Follower(install_follow.InstallFollower):
        def restart(self, target, argv, viewer_state, *, stderr_fd=None):
            _append_event(directory, event="restart", target=str(target), hold=self.hold())
            return super().restart(target, argv, viewer_state, stderr_fd=stderr_fd)

    follower = Follower(Path(old), env={"HARNESS_DATA_ROOT": data, "HARNESS_STATE_ROOT": state})
    guard = screen_guard.ScreenGuard(repaint_after=0.5)
    return fleet_main._run_live(render, collect, None, "both", 0.2, follower=follower, guard=guard)


class Terminal:
    """A pty whose reader the test controls; the child on it is the test's own."""

    def __init__(self, argv, env, rows=40, cols=160):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.slave_name = os.ttyname(slave)
        self.child = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env,
                                      close_fds=True, start_new_session=True)
        os.close(slave)
        os.set_blocking(master, False)
        self.master = master
        self.received = bytearray()

    def read_for(self, seconds, until=None):
        deadline = time.monotonic() + seconds
        open_ = True
        while time.monotonic() < deadline:
            if until is not None and until():
                return True
            if not open_:
                time.sleep(0.05)           # the terminal closed; the child may not be reaped yet
                continue
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if not ready:
                continue
            try:
                data = os.read(self.master, 65536)
            except BlockingIOError:
                continue
            except OSError:
                data = b""                 # EIO: the child closed the terminal
            if not data:
                open_ = False
                continue
            self.received.extend(data)
        return until() if until is not None else True

    def fill(self):
        """Leave the output queue as full as a terminal that stopped reading would.

        The kernel moves queued bytes to the reader's side in the background, so one
        refused write is not proof of a full queue: stop only after refusals in a row.
        """
        fd = os.open(self.slave_name, os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)
        refused = 0
        try:
            while refused < 4:
                try:
                    os.write(fd, b"~" * 256)
                    refused = 0
                except BlockingIOError:
                    refused += 1
                    time.sleep(0.05)
        finally:
            os.close(fd)

    def close(self):
        if self.child.poll() is None:
            os.killpg(self.child.pid, signal.SIGKILL)
            self.child.wait(timeout=5)
        os.close(self.master)


def _wait(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class RealTerminalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("AGENT_", "HERDR_", "CODEX_", "CLAUDE_", "OPENCODE_",
                                           "FLEET_"))}
        for name in ("home", "xdg-state", "xdg-data", "xdg-cache", "xdg-config", "tmp"):
            (self.base / name).mkdir()
        self.env.update(TERM="xterm-256color", PYTHONDONTWRITEBYTECODE="1",
                        HOME=str(self.base / "home"), XDG_STATE_HOME=str(self.base / "xdg-state"),
                        XDG_DATA_HOME=str(self.base / "xdg-data"),
                        XDG_CACHE_HOME=str(self.base / "xdg-cache"),
                        XDG_CONFIG_HOME=str(self.base / "xdg-config"), TMPDIR=str(self.base / "tmp"))

    def start(self, *args):
        terminal = Terminal([sys.executable, str(Path(__file__).resolve()), "--child", *args],
                            self.env)
        self.addCleanup(terminal.close)
        return terminal

    def progress(self):
        try:
            return json.loads((self.base / "progress.json").read_text())
        except (OSError, ValueError):
            return {}

    def events(self):
        try:
            lines = (self.base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [json.loads(line) for line in lines if line.strip()]

    def stderr_log(self):
        path = self.base / "xdg-state" / "agent-fleet" / "fleet-stderr.log"
        return path.read_text(errors="replace") if path.exists() else ""

    def test_without_the_guard_the_same_frames_stop(self):
        """The fixture reproduces the freeze: an unguarded loop stops on its first full write."""
        terminal = self.start("frames", str(self.base), "unguarded")
        self.assertTrue(terminal.read_for(15, lambda: self.progress().get("tick", 0) >= 5))
        time.sleep(1.0)                    # stop reading
        before = self.progress()["tick"]
        time.sleep(1.0)
        self.assertLessEqual(self.progress()["tick"] - before, 1)
        self.assertIsNone(terminal.child.poll())

    def test_guarded_frames_keep_looping_and_repaint_when_read_again(self):
        terminal = self.start("frames", str(self.base), "guarded")
        self.assertTrue(terminal.read_for(15, lambda: self.progress().get("tick", 0) >= 5))
        time.sleep(1.0)                    # stop reading; two frames fill the queue
        stalled = self.progress()
        time.sleep(1.0)
        later = self.progress()
        self.assertGreaterEqual(later["tick"] - stalled["tick"], 8, (stalled, later))
        self.assertEqual(later["hold"], screen_guard.STALLED)
        self.assertGreater(later["skipped"], stalled.get("skipped", 0))
        mark = len(terminal.received)
        self.assertTrue(terminal.read_for(10, lambda: CLEAR in terminal.received[mark:]
                                          and self.progress().get("hold") is None))
        resumed = self.progress()["tick"]
        terminal.read_for(0.5)
        self.assertGreater(self.progress()["tick"], resumed)
        os.write(terminal.master, b"q")
        self.assertTrue(terminal.read_for(10, lambda: terminal.child.poll() is not None))
        self.assertEqual(terminal.child.returncode, 0)

    def release(self, data, version):
        root = data / "releases" / version
        (root / "tools/fleet").mkdir(parents=True)
        (root / "RELEASE_VERSION").write_text(version + "\n")
        (root / ".hearting-release.json").write_text(json.dumps(
            {"schema": 1, "version": version, "archive_sha256": "a" * 64}))
        (root / "tools/fleet/fleet.py").write_text(
            "import json, os, sys\n"
            f"sys.path.insert(0, {str(TOOLS)!r})\n"
            "from fleet.install_follow import read_handoff\n"
            "state = read_handoff()\n"
            "with open(os.environ['FLEET_GUARD_MARKER'], 'w') as stream:\n"
            "    json.dump({'pid': os.getpid(), 'handoff': isinstance(state, dict),\n"
            "               'argv': sys.argv[1:]}, stream)\n")
        return root

    def activate(self, data, state, root):
        current = data / "current"
        if current.is_symlink():
            current.unlink()
        current.symlink_to(root)
        (state / "distribution.json").write_text(json.dumps(
            {"schema": 1, "release_root": str(root), "version": root.name,
             "archive_sha256": "a" * 64}))

    def test_explorer_collects_and_follows_through_a_stalled_terminal(self):
        data, state = self.base / "data", self.base / "state"
        state.mkdir()
        (state / "distribution.lock").touch()
        old, new = self.release(data, "v3.25.18"), self.release(data, "v3.25.19")
        self.activate(data, state, old)
        marker = self.base / "exec-marker.json"
        self.env["FLEET_GUARD_MARKER"] = str(marker)
        terminal = self.start("explorer", str(self.base), str(old), str(data), str(state))
        started = terminal.read_for(45, lambda: self.progress().get("collects", 0) >= 3
                                    and len(terminal.received) > 512)
        self.assertTrue(started, self.stderr_log())

        # Stall 1: nobody reads; collection goes on, and output returns as a full repaint.
        terminal.fill()
        first = self.progress()["collects"]
        self.assertTrue(_wait(lambda: self.progress()["collects"] >= first + 3, 10),
                        self.stderr_log())
        time.sleep(0.6)                    # longer than the fixture's repaint threshold
        mark = len(terminal.received)
        self.assertTrue(terminal.read_for(10, lambda: CLEAR in terminal.received[mark:].rsplit(b"~", 1)[-1]),
                        self.stderr_log())

        # Stall 2: a newer release appears; the viewer waits instead of exec'ing into it.
        terminal.read_for(0.5)
        terminal.fill()
        self.activate(data, state, new)
        held = lambda: any(event.get("hold") == screen_guard.STALLED  # noqa: E731
                           for event in self.events())
        self.assertTrue(_wait(held, 10), (self.events(), self.stderr_log()))
        second = self.progress()["collects"]
        self.assertTrue(_wait(lambda: self.progress()["collects"] > second, 10))
        self.assertFalse(marker.exists())
        self.assertIsNone(terminal.child.poll())

        # Output returns: the same process becomes the new release.
        self.assertTrue(terminal.read_for(20, marker.exists), (self.events(), self.stderr_log()))
        value = json.loads(marker.read_text())
        self.assertEqual(value, {"pid": terminal.child.pid, "handoff": True, "argv": ["--explorer"]})
        self.assertTrue(terminal.read_for(10, lambda: terminal.child.poll() is not None))
        self.assertEqual(terminal.child.returncode, 0)


def _child(argv):
    mode, directory = argv[0], argv[1]
    if mode == "frames":
        _frames_child(directory, argv[2] == "guarded")
        return 0
    return _explorer_child(directory, *argv[2:5]) or 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        sys.exit(_child(sys.argv[2:]))
    unittest.main()
