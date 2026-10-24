#!/usr/bin/env python3
"""A verification lease ends everything its command started, and caps its memory."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
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
            f"subprocess.Popen(['setsid', 'env', '-i', 'sleep', '{marker}'], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "time.sleep(.3)\n" + then]


def forked_sharing(hold=3):
    code = (
        "import os, sys, time\n"
        "buffer = bytearray(8 * 1024 * 1024)\n"
        "for offset in range(0, len(buffer), 4096): buffer[offset] = 1\n"
        "children = []\n"
        "for _ in range(3):\n"
        " child = os.fork()\n"
        " if child == 0: time.sleep(float(sys.argv[1])); os._exit(0)\n"
        " children.append(child)\n"
        "print('ready', flush=True)\n"
        "for child in children: os.waitpid(child, 0)\n"
    )
    return [sys.executable, "-c", code, str(hold)]


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
        self.assertIn("verification-memory-peak", done.stderr)

    def test_a_stopped_lease_ends_its_scope(self):
        with tempfile.TemporaryFile(mode="w+t") as stderr_file:
            lease = subprocess.Popen([sys.executable, str(LEASE), "--timeout", "60", "--",
                                      *detaching(self.marker, then="time.sleep(60)")],
                                     stdout=subprocess.DEVNULL, stderr=stderr_file)
            deadline = time.monotonic() + 10
            while not marked(self.marker) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(marked(self.marker))
            lease.send_signal(signal.SIGTERM)
            self.assertEqual(lease.wait(timeout=20), 128 + signal.SIGTERM)
            stderr_file.seek(0)
            stderr = stderr_file.read()
        self.assertIn("verification-memory-peak", stderr)
        self.assertEqual(marked(self.marker), [])

    def test_sigint_ends_scope_and_prints_peak(self):
        with tempfile.TemporaryFile(mode="w+t") as stderr_file:
            lease = subprocess.Popen([sys.executable, str(LEASE), "--timeout", "60", "--",
                                      *detaching(self.marker, then="time.sleep(60)")],
                                     stdout=subprocess.DEVNULL, stderr=stderr_file)
            deadline = time.monotonic() + 10
            while not marked(self.marker) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(marked(self.marker))
            lease.send_signal(signal.SIGINT)
            lease.wait(timeout=20)
            stderr_file.seek(0)
            stderr = stderr_file.read()
        self.assertIn(lease.returncode, (-signal.SIGINT, 128 + signal.SIGINT))
        self.assertIn("verification-memory-peak", stderr)
        self.assertEqual(marked(self.marker), [])

    def test_a_command_with_nothing_left_reports_nothing(self):
        done = self.run_lease([sys.executable, "-c", "print('ok')"])
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"))
        self.assertNotIn("verification-scope-ended", done.stderr)
        self.assertRegex(done.stderr, r"verification-memory-peak bytes=none measure=none .*scope=tree-aggregate samples=0")

    def test_peak_line_on_sampled_success(self):
        if L.pss_bytes(os.getpid()) is None:
            self.skipTest("/proc smaps_rollup PSS is unavailable")
        done = self.run_lease([sys.executable, "-c", "import time; time.sleep(1.5)"])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertRegex(done.stderr, r"verification-memory-peak bytes=\d+ measure=pss-sum")
        self.assertRegex(done.stderr, r"samples=[1-9]\d* .*budget_bytes=" +
                         str(L.memory_budget_bytes()) + r" budget_source=default")

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
        self.assertRegex(done.stderr, r"measure=(?:pss-sum|rss-sum|mixed)")
        self.assertIn("verification-memory-peak", done.stderr)
        self.assertIn("budget_source=option", done.stderr.split("verification-memory-peak", 1)[1])
        self.assertEqual(marked(self.marker), [])

    def test_fork_shared_buffer_is_counted_once(self):
        if L.pss_bytes(os.getpid()) is None:
            self.skipTest("/proc smaps_rollup PSS is unavailable")
        process = subprocess.Popen(forked_sharing(), stdout=subprocess.PIPE, text=True,
                                   start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertEqual(process.stdout.readline().strip(), "ready")
        process.stdout.close()
        table = L.process_table()
        pids = {pid for pid, (ppid, _rss) in table.items()
                if pid == process.pid or ppid == process.pid}
        rss = sum(table[pid][1] for pid in pids)
        measured, measure = L.measure_scope(table, pids)
        self.assertEqual(measure, "pss-sum")
        self.assertGreaterEqual(rss - measured, 12 * 1024**2)
        process.wait()

    def test_budget_between_pss_and_rss_passes_but_rss_fallback_refuses(self):
        if L.pss_bytes(os.getpid()) is None:
            self.skipTest("/proc smaps_rollup PSS is unavailable")
        process = subprocess.Popen(forked_sharing(), stdout=subprocess.PIPE, text=True,
                                   start_new_session=True)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        process.stdout.close()
        table = L.process_table()
        pids = {pid for pid, (ppid, _rss) in table.items()
                if pid == process.pid or ppid == process.pid}
        rss = sum(table[pid][1] for pid in pids)
        pss, _measure = L.measure_scope(table, pids)
        budget = (rss + pss) // 2
        self.assertEqual(L.wait_within_budget(process, 10, "unused-scope", budget=budget), 0)

        fallback = subprocess.Popen(forked_sharing(hold=10), stdout=subprocess.PIPE, text=True,
                                    start_new_session=True)
        self.assertEqual(fallback.stdout.readline().strip(), "ready")
        fallback.stdout.close()
        try:
            with mock.patch.object(L, "pss_bytes", return_value=None), \
                    self.assertRaisesRegex(MemoryError, "budget_source=default measure=rss-sum"):
                L.wait_within_budget(fallback, 30, "unused-scope", budget=budget)
        finally:
            os.killpg(fallback.pid, signal.SIGKILL)
            fallback.wait()

    def test_unreadable_pss_falls_back_per_pid(self):
        processes = [subprocess.Popen(["sleep", "10"]) for _ in range(2)]
        for process in processes:
            self.addCleanup(process.kill)
            self.addCleanup(process.wait)
        table = L.process_table()
        pids = {process.pid for process in processes}
        with mock.patch.object(L, "pss_bytes", side_effect=lambda pid: None if pid == processes[0].pid else 100):
            _value, measure = L.measure_scope(table, pids)
            self.assertEqual(measure, "mixed")
        with mock.patch.object(L, "pss_bytes", return_value=None):
            _value, measure = L.measure_scope(table, pids)
            self.assertEqual(measure, "rss-sum")
            missing = max(table) + 100000
            value, measure = L.measure_scope({missing: (1, 999)}, {missing})
            self.assertEqual((value, measure), (0, "pss-sum"))

    def test_pss_read_permission_error_is_none(self):
        if L.pss_bytes(os.getpid()) is None:
            self.skipTest("/proc smaps_rollup PSS is unavailable")
        with mock.patch("builtins.open", side_effect=PermissionError):
            self.assertIsNone(L.pss_bytes(os.getpid()))

    def test_running_peak_is_recorded_only_when_it_grows(self):
        peak = L.MemoryPeak()
        peak.record(100, "pss-sum", 1.0)
        self.assertTrue(peak.json_due(1600))
        peak.written = peak.bytes
        self.assertFalse(peak.json_due(1600))
        peak.record(199, "pss-sum", 1.0)
        self.assertFalse(peak.json_due(1600))
        peak.record(200, "mixed", 2.0)
        self.assertTrue(peak.json_due(1600))

    def test_the_budget_is_a_quarter_of_a_small_host(self):
        with mock.patch.object(L.os, "sysconf", side_effect=lambda name: {
                "SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": (8 * 1024 ** 3) // 4096}[name]):
            self.assertEqual(L.memory_budget_bytes(), 2 * 1024 ** 3)
        self.assertLessEqual(L.memory_budget_bytes(), 16 * 1024 ** 3)


if __name__ == "__main__":
    unittest.main()
