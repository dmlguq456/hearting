"""Nothing the terminal sends may hold the frame loop.

The dashboard froze twice from terminal input, in two different terminals: under herdr
(2026-07-01) and in a VS Code terminal (2026-09-09). The second was measured while stuck —
0 CPU, zero read and write syscalls, one thread, sleeping on the tty with the terminal's
output queue empty — and any keypress advanced it exactly one frame, which is what a
half-assembled mouse or focus escape sequence does.

The freeze needs a real terminal and a real click, so it is not reproducible here. What is
testable is the configuration that prevents it, which is why it lives in its own function.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import sys
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet import render  # noqa: E402


class FakeCurses:
    """Records what `_configure_input` asks the terminal to do."""

    ERR = -1
    KEY_MOUSE = 409
    KEY_UP = 259
    KEY_DOWN = 258
    KEY_LEFT = 260
    KEY_RIGHT = 261
    KEY_PPAGE = 339
    KEY_NPAGE = 338
    KEY_HOME = 262
    KEY_END = 360
    KEY_RESIZE = 410
    BUTTON1_PRESSED = 0x1
    BUTTON1_RELEASED = 0x2
    BUTTON1_CLICKED = 0x4
    BUTTON2_PRESSED = 0x8
    BUTTON3_PRESSED = 0x10
    BUTTON4_PRESSED = 0x100
    BUTTON5_PRESSED = 0x200
    BUTTON_SHIFT = 0x400
    BUTTON_ALT = 0x800
    BUTTON_CTRL = 0x1000

    def __init__(self, *, escdelay_raises=False, mouse_raises=False, interval_raises=False):
        self.calls = []
        self._escdelay_raises = escdelay_raises
        self._mouse_raises = mouse_raises
        self._interval_raises = interval_raises

    def set_escdelay(self, ms):
        self.calls.append(("set_escdelay", ms))
        if self._escdelay_raises:
            raise Exception("terminal refused")

    def mousemask(self, mask):
        self.calls.append(("mousemask", mask))
        if self._mouse_raises:
            raise Exception("terminal refused")

    def mouseinterval(self, interval):
        self.calls.append(("mouseinterval", interval))
        if self._interval_raises:
            raise Exception("terminal refused")

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class OlderCurses:
    """A curses build predating `set_escdelay` (older Python / ncurses).

    Written out rather than subclassed: `hasattr` has to report False, and removing an
    inherited method from one instance is not something a subclass can do cleanly.
    """

    BUTTON1_PRESSED = FakeCurses.BUTTON1_PRESSED
    BUTTON4_PRESSED = FakeCurses.BUTTON4_PRESSED
    BUTTON5_PRESSED = FakeCurses.BUTTON5_PRESSED

    def __init__(self):
        self.calls = []

    def mousemask(self, mask):
        self.calls.append(("mousemask", mask))

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class ClickOnlyCurses:
    """Legacy input surface without BUTTON1_PRESSED or mouseinterval()."""
    BUTTON1_CLICKED = 0x4
    BUTTON4_PRESSED = 0x100
    BUTTON5_PRESSED = 0x200

    def __init__(self):
        self.calls = []

    def set_escdelay(self, ms):
        self.calls.append(("set_escdelay", ms))

    def mousemask(self, mask):
        self.calls.append(("mousemask", mask))

    def named(self, name):
        return [call for call in self.calls if call[0] == name]


class FakeScreen:
    def __init__(self, values):
        self.values = list(values)
        self.delay = 100
        self.read_delays = []

    def timeout(self, delay):
        self.delay = delay

    def getch(self):
        self.read_delays.append(self.delay)
        return self.values.pop(0) if self.values else FakeCurses.ERR


class InputConfigurationTest(unittest.TestCase):
    def test_mouse_reporting_is_on_by_default_with_press_and_wheel_bits(self):
        fake = FakeCurses()
        render._configure_input(fake, {})
        expected = (FakeCurses.BUTTON1_PRESSED | FakeCurses.BUTTON4_PRESSED
                    | FakeCurses.BUTTON5_PRESSED)
        self.assertEqual(fake.named("mousemask"), [("mousemask", expected)])
        self.assertEqual(fake.named("mouseinterval"), [("mouseinterval", 0)])

    def test_exact_one_keeps_mouse_enabled(self):
        fake = FakeCurses()
        render._configure_input(fake, {"FLEET_MOUSE": "1"})
        expected = (FakeCurses.BUTTON1_PRESSED | FakeCurses.BUTTON4_PRESSED
                    | FakeCurses.BUTTON5_PRESSED)
        self.assertEqual(fake.named("mousemask"), [("mousemask", expected)])

    def test_every_explicit_non_one_value_preserves_opt_out(self):
        for value in ("0", "", "true", "yes", "2", " 1"):
            with self.subTest(value=value):
                fake = FakeCurses()
                render._configure_input(fake, {"FLEET_MOUSE": value})
                self.assertEqual(fake.named("mousemask"), [])

    def test_escape_delay_is_shortened_so_a_stray_esc_costs_one_frame(self):
        fake = FakeCurses()
        render._configure_input(fake, {})
        calls = fake.named("set_escdelay")
        self.assertEqual(len(calls), 1)
        # The frame cadence is ~100ms; the delay has to sit well under it.
        self.assertLessEqual(calls[0][1], 50)
        self.assertGreater(calls[0][1], 0)

    def test_a_terminal_that_refuses_input_settings_does_not_crash_the_tui(self):
        # These run before the first frame; an exception here would take the whole
        # dashboard down over a cosmetic setting.
        render._configure_input(FakeCurses(escdelay_raises=True), {})
        render._configure_input(FakeCurses(mouse_raises=True), {"FLEET_MOUSE": "1"})
        render._configure_input(FakeCurses(interval_raises=True), {})
        render._configure_input(OlderCurses(), {})

    def test_an_older_curses_without_set_escdelay_still_configures_mouse(self):
        fake = OlderCurses()
        render._configure_input(fake, {"FLEET_MOUSE": "1"})
        self.assertEqual(fake.named("set_escdelay"), [])
        self.assertEqual(len(fake.named("mousemask")), 1)

    def test_click_only_fallback_does_not_disable_ncurses_click_synthesis(self):
        fake = ClickOnlyCurses()
        render._configure_input(fake, {})
        mask = (ClickOnlyCurses.BUTTON1_CLICKED | ClickOnlyCurses.BUTTON4_PRESSED
                | ClickOnlyCurses.BUTTON5_PRESSED)
        self.assertEqual(fake.named("mousemask"), [("mousemask", mask)])

    def test_mousemask_is_still_requested_when_zero_interval_is_refused(self):
        fake = FakeCurses(interval_raises=True)
        render._configure_input(fake, {})
        expected = (FakeCurses.BUTTON1_PRESSED | FakeCurses.BUTTON4_PRESSED
                    | FakeCurses.BUTTON5_PRESSED)
        self.assertEqual(fake.named("mousemask"), [("mousemask", expected)])


class IncrementalCursesInputTest(unittest.TestCase):
    def test_buffered_arrow_decodes_once_without_consuming_the_next_action(self):
        fake = FakeCurses()
        reader = render._CursesInput(fake)
        screen = FakeScreen([*b"\x1b[B", ord("x")])
        screen.timeout(37)
        self.assertEqual(reader.read(screen, timeout_ms=37), (fake.KEY_DOWN, None))
        self.assertEqual(screen.read_delays, [37, 0, 0])
        self.assertEqual(screen.delay, 37)
        self.assertEqual(screen.values, [ord("x")])
        self.assertEqual(reader.read(screen), (ord("x"), None))

    def test_buffered_mouse_dispatches_one_event_and_preserves_confirmation_key(self):
        fake = FakeCurses()
        reader = render._CursesInput(fake)
        screen = FakeScreen([*b"\x1b[<0;12;4M", ord("y")])
        self.assertEqual(reader.read(screen),
                         (fake.KEY_MOUSE, (11, 3, fake.BUTTON1_PRESSED)))
        self.assertEqual(screen.values, [ord("y")])
        self.assertEqual(screen.delay, 100)

    def test_missing_tail_yields_and_malformed_stream_has_a_bounded_drain(self):
        fake = FakeCurses()
        for sequence in (b"\x1b[", b"\x1b[<" + b"1" * 1000):
            with self.subTest(sequence_length=len(sequence)):
                reader = render._CursesInput(fake)
                screen = FakeScreen(sequence)
                self.assertEqual(reader.read(screen), (fake.ERR, None))
                self.assertLessEqual(len(screen.read_delays), reader._MAX_SEQUENCE + 1)
                self.assertEqual(screen.delay, 100)
                self.assertTrue(reader.has_buffered_input())

    def _read(self, reader, sequence):
        result = (FakeCurses.ERR, None)
        for value in sequence:
            result = reader.read(FakeScreen([value]))
        return result

    def _read_all(self, reader, sequence):
        results = []
        for value in sequence:
            results.append(reader.read(FakeScreen([value])))
            while reader.ready:
                results.append(reader.read(FakeScreen([])))
        return results

    def test_incomplete_sgr_coordinates_remain_a_nonblocking_prefix(self):
        reader = render._CursesInput(FakeCurses())
        prefix = bytes.fromhex("1b5b3c303b31323b")
        for value in prefix:
            ch, event = reader.read(FakeScreen([value]))
            self.assertEqual(ch, FakeCurses.ERR)
            self.assertIsNone(event)
        self.assertEqual(reader.read(FakeScreen([])), (FakeCurses.ERR, None))
        self.assertEqual(bytes(reader.pending), prefix)

    def test_complete_sgr_press_release_and_wheel_decode_to_curses_bstate(self):
        cases = (
            (b"\x1b[<0;12;4M", (11, 3, FakeCurses.BUTTON1_PRESSED)),
            (b"\x1b[<0;12;4m", (11, 3, FakeCurses.BUTTON1_RELEASED)),
            (b"\x1b[<64;12;4M", (11, 3, FakeCurses.BUTTON4_PRESSED)),
            (b"\x1b[<65;12;4M", (11, 3, FakeCurses.BUTTON5_PRESSED)),
        )
        for sequence, expected in cases:
            with self.subTest(sequence=sequence):
                reader = render._CursesInput(FakeCurses())
                ch, event = self._read(reader, sequence)
                self.assertEqual(ch, FakeCurses.KEY_MOUSE)
                self.assertEqual(event, expected)

    def test_supported_modifier_combinations_are_preserved(self):
        fake = FakeCurses()
        reader = render._CursesInput(fake)
        for bit, name in ((4, "BUTTON_SHIFT"), (8, "BUTTON_ALT"), (16, "BUTTON_CTRL")):
            with self.subTest(modifier=name):
                ch, event = self._read(reader, ("\x1b[<%d;12;4M" % bit).encode("ascii"))
                self.assertEqual(ch, fake.KEY_MOUSE)
                self.assertEqual(event, (11, 3, fake.BUTTON1_PRESSED | getattr(fake, name)))

    def test_unsupported_button_modifier_motion_and_wheel_forms_are_inert(self):
        fake = FakeCurses()
        for button, suffix in ((128, "M"), (132, "M"), (32, "M"), (33, "M"),
                               (66, "M"), (67, "M"), (96, "M"), (64, "m"),
                               (65, "m"), (3, "M"), (-1, "M")):
            with self.subTest(button=button, suffix=suffix):
                reader = render._CursesInput(fake)
                sequence = ("\x1b[<%d;12;4%s" % (button, suffix)).encode("ascii")
                results = self._read_all(reader, sequence)
                self.assertFalse([value for value in results if value[0] != fake.ERR], results)

    def test_malformed_sgr_packets_are_consumed_and_the_next_key_and_packet_work(self):
        fake = FakeCurses()
        malformed = (
            b"\x1b[<qM", b"\x1b[<sM", b"\x1b[<xM",
            b"\x1b[<1;bad;4M", b"\x1b[<0;0;4M", b"\x1b[<0;65536;4M",
            b"\x1b[<0;1;2;3M", b"\x1b[<" + b"1" * 70 + b"M",
        )
        for sequence in malformed:
            with self.subTest(sequence=sequence):
                reader = render._CursesInput(fake)
                results = self._read_all(reader, sequence)
                self.assertFalse([value for value in results if value[0] != fake.ERR], results)
                self.assertFalse(reader.has_buffered_input())
                self.assertEqual(reader.read(FakeScreen([ord("s")])), (ord("s"), None))
                self.assertEqual(self._read(reader, b"\x1b[<0;12;4M"),
                                 (fake.KEY_MOUSE, (11, 3, fake.BUTTON1_PRESSED)))

    def test_incomplete_control_and_discard_state_expire_to_idle_without_replay(self):
        fake = FakeCurses()
        for sequence in (b"\x1b[<0;12;", b"\x1b[<q", b"\x1b["):
            with self.subTest(sequence=sequence):
                reader = render._CursesInput(fake)
                with mock.patch.object(render.time, "monotonic", return_value=0.0):
                    self._read_all(reader, sequence)
                self.assertTrue(reader.has_buffered_input())
                with mock.patch.object(render.time, "monotonic", return_value=1.0):
                    self.assertEqual(reader.read(FakeScreen([])), (fake.ERR, None))
                self.assertFalse(reader.has_buffered_input())
                self.assertEqual(reader.read(FakeScreen([ord("q")])), (ord("q"), None))

    def test_new_escape_boundary_resynchronizes_and_native_key_is_not_packet_tail(self):
        fake = FakeCurses()
        reader = render._CursesInput(fake)
        self._read_all(reader, b"\x1b[<q")
        self.assertTrue(reader.has_buffered_input())
        self.assertEqual(self._read(reader, b"\x1b[<0;12;4M"),
                         (fake.KEY_MOUSE, (11, 3, fake.BUTTON1_PRESSED)))

        reader = render._CursesInput(fake)
        self._read_all(reader, b"\x1b[<0;12;")
        self.assertEqual(reader.read(FakeScreen([fake.KEY_RESIZE])),
                         (fake.KEY_RESIZE, None))
        self.assertTrue(reader.has_buffered_input())
        self.assertFalse([ch for ch, _ in self._read_all(reader, b"qM")
                          if ch != fake.ERR])
        self.assertFalse(reader.has_buffered_input())

    def test_native_resize_does_not_leak_mouse_tail_as_commands(self):
        fake = FakeCurses()
        for enabled in (True, False):
            for tail in (b"qM", b"Qm", b"sM", b"xM"):
                with self.subTest(mouse_enabled=enabled, tail=tail):
                    reader = render._CursesInput(fake, mouse_enabled=enabled)
                    with mock.patch.object(render.time, "monotonic", return_value=0.0):
                        results = self._read_all(
                            reader, [*b"\x1b[<", fake.KEY_RESIZE, *tail])
                    self.assertEqual([ch for ch, _ in results if ch != fake.ERR],
                                     [fake.KEY_RESIZE])
                    self.assertFalse(reader.has_buffered_input())
                    self.assertEqual(reader.read(FakeScreen([ord("s")])),
                                     (ord("s"), None))

    def test_repeated_native_keys_do_not_extend_mouse_tail_expiry(self):
        fake = FakeCurses()
        for prefix in (b"\x1b[<", b"\x1b[<q"):
            with self.subTest(prefix=prefix):
                reader = render._CursesInput(fake)
                with mock.patch.object(render.time, "monotonic", return_value=0.0):
                    self._read_all(reader, prefix)
                for tick in (0.05, 0.10, 0.19):
                    with mock.patch.object(render.time, "monotonic", return_value=tick):
                        self.assertEqual(reader.read(FakeScreen([fake.KEY_RESIZE])),
                                         (fake.KEY_RESIZE, None))
                    self.assertTrue(reader.has_buffered_input())
                with mock.patch.object(render.time, "monotonic", return_value=0.201):
                    self.assertEqual(reader.read(FakeScreen([])), (fake.ERR, None))
                self.assertFalse(reader.has_buffered_input())
                self.assertEqual(reader.read(FakeScreen([ord("q")])), (ord("q"), None))

    def test_buffered_input_reports_pending_and_ready_data(self):
        reader = render._CursesInput(FakeCurses())
        self.assertFalse(reader.has_buffered_input())
        self._read_all(reader, b"\x1b[<0;12;")
        self.assertTrue(reader.has_buffered_input())
        reader._clear()
        reader.ready.append((ord("z"), None))
        self.assertTrue(reader.has_buffered_input())
        self.assertEqual(reader.read(FakeScreen([])), (ord("z"), None))
        self.assertFalse(reader.has_buffered_input())

    def test_focus_reports_are_ignored_and_existing_navigation_keys_decode(self):
        fake = FakeCurses()
        reader = render._CursesInput(fake)
        for sequence in (b"\x1b[I", b"\x1b[O"):
            self.assertEqual(self._read(reader, sequence), (fake.ERR, None))
        for sequence, expected in (
                (b"\x1b[A", fake.KEY_UP), (b"\x1b[B", fake.KEY_DOWN),
                (b"\x1b[5~", fake.KEY_PPAGE), (b"\x1b[6~", fake.KEY_NPAGE),
                (b"\x1bOH", fake.KEY_HOME), (b"\x1bOF", fake.KEY_END)):
            with self.subTest(sequence=sequence):
                self.assertEqual(self._read(reader, sequence), (expected, None))

    def test_ordinary_key_after_a_malformed_mouse_packet_is_not_swallowed(self):
        fake = FakeCurses()
        reader = render._CursesInput(fake)
        self._read(reader, bytes.fromhex("1b5b3c303b31323b"))
        results = self._read_all(reader, b"qM")
        self.assertFalse([value for value in results if value[0] != fake.ERR], results)
        self.assertEqual(reader.read(FakeScreen([ord("q")])), (ord("q"), None))

    def test_new_mouse_packet_restarts_after_an_abandoned_partial_packet(self):
        fake = FakeCurses()
        reader = render._CursesInput(fake)
        self._read(reader, bytes.fromhex("1b5b3c303b31323b"))
        ch, event = self._read(reader, b"\x1b[<0;12;4M")
        self.assertEqual(ch, fake.KEY_MOUSE)
        self.assertEqual(event, (11, 3, fake.BUTTON1_PRESSED))


class StallDumpTest(unittest.TestCase):
    """`kill -USR1 <fleet pid>` must be able to answer "where is it stuck?".

    This host sets `ptrace_scope=1`, so strace and gdb cannot attach to a process that is
    not their own child. Both freezes were diagnosed by elimination because fleet could not
    answer for itself.
    """

    def test_the_entry_point_arms_a_stack_dump(self):
        import importlib
        fleet = importlib.import_module("fleet.fleet")
        self.assertTrue(hasattr(fleet, "_arm_stall_dump"))

    def test_arming_writes_a_dump_the_operator_can_read(self):
        import faulthandler
        import os
        import signal
        import tempfile
        import importlib
        fleet = importlib.import_module("fleet.fleet")
        if not hasattr(signal, "SIGUSR1"):
            self.skipTest("platform has no SIGUSR1")
        previous = faulthandler.is_enabled()
        fleet._arm_stall_dump()
        path = os.path.join(tempfile.gettempdir(), "fleet-stall-%d.txt" % os.getpid())
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        try:
            os.kill(os.getpid(), signal.SIGUSR1)
            self.assertTrue(os.path.exists(path), "no dump file was written")
            dump = Path(path).read_text(encoding="utf-8", errors="replace")
            self.assertIn("most recent call first", dump)
            self.assertIn("test_input_freeze.py", dump)
        finally:
            faulthandler.unregister(signal.SIGUSR1)
            if not previous:
                faulthandler.disable()


    def test_an_unused_dump_file_does_not_outlive_the_process(self):
        import os
        import signal
        import subprocess
        import tempfile
        if not hasattr(signal, "SIGUSR1"):
            self.skipTest("platform has no SIGUSR1")
        script = (
            "import os, signal, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from fleet import fleet\n"
            "fleet._arm_stall_dump()\n"
            "if sys.argv[2] == 'dump':\n"
            "    os.kill(os.getpid(), signal.SIGUSR1)\n"
            "print(os.getpid())\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            env = dict(os.environ, TMPDIR=temporary)
            outcomes = {}
            for mode in ("quiet", "dump"):
                done = subprocess.run(
                    [sys.executable, "-c", script, str(TOOLS), mode],
                    env=env, capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(done.returncode, 0, done.stderr)
                path = os.path.join(temporary, "fleet-stall-%s.txt" % done.stdout.strip())
                outcomes[mode] = os.path.exists(path)
            # Armed and never used: gone. Actually dumped into: evidence, kept.
            self.assertEqual(outcomes, {"quiet": False, "dump": True})


if __name__ == "__main__":
    unittest.main()
