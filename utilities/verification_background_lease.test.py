#!/usr/bin/env python3
"""A verification lease ends everything its command started, and caps its memory."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
LEASE = HERE / "verification-background-lease.py"
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("verification_background_lease", LEASE)
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)


def marked(marker):
    """Live `sleep <marker>` processes (not the lease, whose argv also names it)."""
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            argv = Path(f"/proc/{name}/cmdline").read_bytes().split(b"\0")
            if argv[:2] == [b"sleep", marker.encode()]:
                state = Path(f"/proc/{name}/stat").read_bytes().rsplit(b")", 1)[1].split()[0]
                if state not in (b"Z", b"X"):
                    found.append(int(name))
        except (OSError, IndexError):
            continue
    return found


def detaching(marker, *, then="pass"):
    """A command that leaves a setsid, environment-stripped sleeper behind."""
    return [sys.executable, "-c",
            "import subprocess, time\n"
            f"subprocess.Popen(['setsid', 'env', '-i', 'sleep', '{marker}'])\n"
            "time.sleep(.3)\n" + then]


class LeaseScopeTest(unittest.TestCase):
    def setUp(self):
        self.marker = f"{300 + os.getpid() % 1000}.{time.monotonic_ns() % 10**6}"
        self.addCleanup(self.kill_marked)

    def kill_marked(self):
        for pid in marked(self.marker):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def run_lease(self, command, timeout=30, options=()):
        return subprocess.run([sys.executable, str(LEASE), "--timeout", str(timeout), *options, "--", *command],
                              capture_output=True, text=True, timeout=60)

    def test_a_detached_descendant_ends_with_its_command(self):
        done = self.run_lease(detaching(self.marker))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(marked(self.marker), [])
        self.assertIn("verification-scope-ended", done.stderr)

    def test_expiry_returns_124_and_ends_the_detached_descendant(self):
        done = self.run_lease(detaching(self.marker, then="time.sleep(60)"), timeout=2)
        self.assertEqual(done.returncode, 124, done.stderr)
        self.assertEqual(marked(self.marker), [])

    def test_a_stopped_lease_ends_its_scope(self):
        lease = subprocess.Popen([sys.executable, str(LEASE), "--timeout", "60", "--",
                                  *detaching(self.marker, then="time.sleep(60)")],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 10
        while not marked(self.marker) and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(marked(self.marker))
        lease.send_signal(signal.SIGTERM)
        self.assertEqual(lease.wait(timeout=20), 128 + signal.SIGTERM)
        lease.stderr.close()
        self.assertEqual(marked(self.marker), [])

    def test_a_command_with_nothing_left_reports_nothing(self):
        done = self.run_lease([sys.executable, "-c", "print('ok')"])
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"))
        self.assertNotIn("verification-scope-ended", done.stderr)

    def test_the_scope_past_its_memory_budget_is_refused(self):
        process = subprocess.Popen(["sleep", self.marker], start_new_session=True)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        with mock.patch.object(L, "memory_budget_bytes", return_value=1), \
                self.assertRaisesRegex(MemoryError, "verification-memory-budget-exceeded.*budget_source=default"):
            L.wait_within_budget(process, 30, "unused-scope")
        self.assertIsNone(process.poll())  # the caller, not the check, ends the scope

    def test_default_budget_is_unchanged(self):
        self.assertEqual(L.resolve_memory_budget(None), (L.memory_budget_bytes(), "default"))

    def test_size_grammar(self):
        expected = {"1048576": 1048576, "1K": 1024, "1KiB": 1024, "1k": 1024,
                    "1KB": 1000, "140GB": 140 * 10**9, "8GiB": 8 * 1024**3,
                    "1.5GiB": 1610612736, "2T": 2 * 1024**4}
        for raw, value in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(L.parse_size(raw), value)
        for raw in ("", "abc", "0", "-1", "1.5", "1XB", "nan", "inf", "0.0001K"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                L.parse_size(raw)

    def test_budget_above_host_memory_is_refused(self):
        with mock.patch.object(L, "host_memory_bytes", return_value=8 * 1024**3):
            with self.assertRaisesRegex(ValueError, "9GiB"):
                L.resolve_memory_budget("9GiB")
            self.assertEqual(L.resolve_memory_budget("8GiB"), (8 * 1024**3, "option"))
        with mock.patch.object(L, "host_memory_bytes", return_value=0):
            self.assertEqual(L.resolve_memory_budget("9GiB"), (9 * 1024**3, "option"))

    def test_bad_budget_is_a_usage_error_before_the_command_runs(self):
        host = L.host_memory_bytes()
        for option in ("0", "-5", "lots", f"{host * 2}B"):
            with self.subTest(option=option):
                done = self.run_lease([sys.executable, "-c", "print('ran')"],
                                      options=("--memory-budget", option))
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn("--memory-budget", done.stderr)
                self.assertNotIn("ran", done.stdout)

    def test_option_applies_and_the_message_names_its_source(self):
        done = self.run_lease(["sleep", self.marker], options=("--memory-budget", "1"))
        self.assertEqual(done.returncode, 125, done.stderr)
        self.assertIn("verification-memory-budget-exceeded", done.stderr)
        self.assertIn("budget_bytes=1", done.stderr)
        self.assertIn("budget_source=option", done.stderr)
        self.assertEqual(marked(self.marker), [])

    def test_the_budget_is_a_quarter_of_a_small_host(self):
        with mock.patch.object(L.os, "sysconf", side_effect=lambda name: {
                "SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": (8 * 1024 ** 3) // 4096}[name]):
            self.assertEqual(L.memory_budget_bytes(), 2 * 1024 ** 3)
        self.assertLessEqual(L.memory_budget_bytes(), 16 * 1024 ** 3)


if __name__ == "__main__":
    unittest.main()
