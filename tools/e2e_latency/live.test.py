#!/usr/bin/env python3
"""tools/e2e_latency/live.py — the read-only sampler, on synthetic files only.

No test sleeps, none asserts a duration, and none opens an operator path: registries
and the cache directory are injected tmp files, mtimes are set with ``os.utime``.
"""
import contextlib
import fcntl
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("e2e_latency_live", HERE / "live.py")
live = importlib.util.module_from_spec(_spec)
sys.modules["e2e_latency_live"] = live
_spec.loader.exec_module(live)


def put(path, data, mtime, mode="wb"):
    with open(path, mode) as fh:
        fh.write(data)
    os.utime(path, (mtime, mtime))


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="e2e-live-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.registry = self.root / "jobs.log"
        self.cache = self.root / "human-waits"
        self.cache.mkdir()
        put(self.registry, b"row-1\n", 1000.0)
        self.publish("projection", 1000.0, 1000.0)
        self.publish("fleet", 1000.0, 1000.0)

    def publish(self, name, observed_at, mtime, extra=None):
        body = {"schema_version": 3, "observed_at": observed_at, "items": extra or []}
        put(self.cache / (name + ".json"), json.dumps(body).encode(), mtime)

    def sampler(self):
        sampler = live.Sampler([self.registry], self.cache)
        sampler.poll(1000.5)                      # baseline: nothing is a change yet
        return sampler


class SamplerTest(Case):
    def test_append_then_replace_are_told_apart(self):
        s = self.sampler()
        put(self.registry, b"row-2\n", 1005.0, "ab")
        s.poll(1005.2)
        replacement = self.root / "new.log"
        put(replacement, b"x\n", 1010.0)
        os.replace(replacement, self.registry)    # new inode, smaller: a wholesale replace
        s.poll(1010.2)
        self.assertEqual([c["kind"] for c in s.changes], ["append", "replace"])
        self.assertEqual([round(c["t"]) for c in s.changes], [1005, 1010])

    def test_no_change_no_event_and_first_poll_is_baseline(self):
        s = self.sampler()
        for now in (1001.0, 1001.1, 1001.2):
            s.poll(now)
        self.assertEqual(s.changes, [])
        self.assertEqual(s.publications["projection"], [])

    def test_only_a_publication_observing_the_change_pairs(self):
        s = self.sampler()
        put(self.registry, b"row-2\n", 1005.0, "ab")
        s.poll(1005.1)
        self.publish("projection", 1004.5, 1006.0)   # started before the change: no pair
        s.poll(1006.1)
        paired, unpaired = s.lags("projection")
        self.assertEqual((paired, unpaired), ([], 1))
        self.publish("projection", 1005.5, 1008.0)
        s.poll(1008.1)
        paired, unpaired = s.lags("projection")
        self.assertEqual(unpaired, 0)
        self.assertAlmostEqual(paired[0]["lag_s"], 3.0, places=3)
        self.assertAlmostEqual(paired[0]["observed_lag_s"], 0.5, places=3)

    def test_two_changes_one_publication_each_has_its_own_lag(self):
        s = self.sampler()
        put(self.registry, b"a\n", 1005.0, "ab")
        s.poll(1005.1)
        put(self.registry, b"b\n", 1007.0, "ab")
        s.poll(1007.1)
        self.publish("fleet", 1007.5, 1009.0)
        s.poll(1009.1)
        paired, _ = s.lags("fleet")
        self.assertEqual([round(p["lag_s"], 1) for p in paired], [4.0, 2.0])

    def test_replace_when_the_file_vanishes_and_returns(self):
        s = self.sampler()
        self.registry.unlink()
        s.poll(1002.0)
        self.assertEqual(s.changes, [])             # a missing file is not a change
        put(self.registry, b"fresh\n", 1003.0)
        s.poll(1003.1)
        self.assertEqual([c["kind"] for c in s.changes], ["replace"])

    def test_report_carries_counts_and_times_but_no_content(self):
        s = self.sampler()
        put(self.registry, b"SECRET-ROW-TEXT\n", 1005.0, "ab")
        s.poll(1005.1)
        self.publish("projection", 1005.5, 1006.0, extra=[{"question": "PRIVATE-QUESTION"}])
        s.poll(1006.1)
        text = json.dumps(s.report())
        for leak in ("SECRET-ROW-TEXT", "PRIVATE-QUESTION", str(self.root), "jobs.log"):
            self.assertNotIn(leak, text)
        report = s.report()
        self.assertEqual(report["registry"]["changes"], {"append": 1, "replace": 0})
        self.assertEqual(report["latency"]["projection"]["all"]["n"], 1)
        self.assertGreater(report["cache_age"]["projection"]["n"], 0)

    def test_summarize_is_nearest_rank(self):
        out = live.summarize(range(1, 21))
        self.assertEqual((out["n"], out["median_s"], out["p95_s"], out["max_s"]), (20, 10.5, 19, 20))
        self.assertNotIn("p95_basis", out)
        self.assertEqual(live.summarize([1, 2, 3])["p95_basis"], "max-of-n")
        self.assertEqual(live.summarize([])["n"], 0)

    def test_producer_lock_held_follows_a_real_flock(self):
        lock = self.cache / ".projection.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                with open("/proc/locks"):
                    pass
            except OSError:
                self.skipTest("/proc/locks unreadable here")
            self.assertTrue(live.producer_lock_held(str(lock)))
        finally:
            os.close(fd)
        self.assertFalse(live.producer_lock_held(str(lock)))
        self.assertFalse(live.producer_lock_held(str(self.cache / "absent.lock")))


class GuardTest(Case):
    def test_write_outside_allowed_and_signals_are_refused_and_recorded(self):
        out = self.root / "out.json"
        other = self.root / "other.txt"
        with live.ReadOnlyGuard(allowed_writes=[out]) as guard:
            out.write_text("{}")                      # the one allowed write
            self.registry.read_bytes()                # reads stay free
            with self.assertRaises(PermissionError):
                other.write_text("x")
            with self.assertRaises(PermissionError):
                os.kill(os.getpid(), 0)
            with self.assertRaises(PermissionError):
                os.utime(self.registry, (1, 1))
            with self.assertRaises(PermissionError):
                os.rename(self.registry, self.root / "moved")
            with self.assertRaises(PermissionError):
                os.mkdir(self.root / "sub")
            with self.assertRaises(PermissionError):
                os.remove(self.registry)
            with self.assertRaises(PermissionError):
                subprocess.Popen([sys.executable, "-c", "pass"])
        self.assertFalse(other.exists())
        self.assertTrue(self.registry.exists())
        self.assertEqual([v["kind"] for v in guard.violations],
                         ["write", "signal", "write", "write", "write", "write", "spawn"])

    def test_append_open_is_a_write(self):
        with live.ReadOnlyGuard() as guard:
            with self.assertRaises(PermissionError):
                open(self.registry, "ab")
            with self.assertRaises(PermissionError):
                os.open(self.registry, os.O_WRONLY | os.O_APPEND)
        self.assertEqual(self.registry.read_bytes(), b"row-1\n")
        self.assertEqual(len(guard.violations), 2)

    def test_guard_is_scoped_to_its_with_block(self):
        with live.ReadOnlyGuard():
            pass
        (self.root / "after.txt").write_text("fine")
        os.utime(self.registry, (5, 5))


class RunTest(Case):
    def fake_time(self):
        state = {"now": 2000.0}

        def clock():
            return state["now"]

        def sleep(seconds):
            state["now"] += seconds
            state["tick"] = state.get("tick", 0) + 1
            if self.on_tick:
                self.on_tick(state["tick"], state["now"])

        return clock, sleep, state

    on_tick = None

    def test_run_pairs_an_injected_change_without_violations(self):
        def tick(n, now):
            # The operator's writers are other processes: stand in for them with the guard
            # lifted for this one mutation only.
            armed = live.ReadOnlyGuard._guards[:]
            del live.ReadOnlyGuard._guards[:]
            try:
                if n == 2:
                    put(self.registry, b"row-2\n", 2000.15, "ab")
                if n == 5:
                    self.publish("projection", 2000.2, 2000.45)
            finally:
                live.ReadOnlyGuard._guards[:] = armed

        self.on_tick = tick
        clock, sleep, state = self.fake_time()
        with mock.patch.object(live.os, "stat", wraps=os.stat) as spy:
            result = live.run([self.registry], self.cache, duration=1.0, period=0.1,
                              clock=clock, sleep=sleep, mono=lambda: state["now"] - 2000.0)
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["registry"]["changes"]["append"], 1)
        lag = result["latency"]["projection"]["all"]
        self.assertEqual(lag["n"], 1)
        self.assertAlmostEqual(lag["median_s"], 0.3, places=2)
        self.assertEqual(result["latency"]["fleet"]["unpaired_changes"], 1)
        touched = {str(c.args[0]) for c in spy.call_args_list if c.args}
        self.assertTrue(any(p.startswith(str(self.root)) for p in touched))

    def test_a_write_attempt_during_the_run_is_a_violation(self):
        def tick(n, now):
            os.utime(self.registry, (n, n))     # a bug in the sampler's caller: touching the registry

        self.on_tick = tick
        clock, sleep, state = self.fake_time()
        result = live.run([self.registry], self.cache, duration=1.0, period=0.1,
                          clock=clock, sleep=sleep, mono=lambda: state["now"] - 2000.0)
        self.assertEqual([v["event"] for v in result["violations"]], ["os.utime"])
        self.assertEqual(self.registry.stat().st_mtime, 1000.0)

    def test_main_uses_only_injected_paths_and_writes_only_out(self):
        out = self.root / "report" / "live.json"
        md = self.root / "report" / "live.md"
        with mock.patch.object(live, "default_registries",
                               side_effect=AssertionError("operator registry resolved")), \
                mock.patch.object(live, "default_cache_dir",
                                  side_effect=AssertionError("operator cache resolved")):
            code = live.main(["--duration", "0.05", "--period", "0.01", "--registry",
                              str(self.registry), "--cache-dir", str(self.cache),
                              "--out", str(out), "--markdown", str(md)])
        self.assertEqual(code, 0)
        data = json.loads(out.read_text())
        self.assertTrue(data["read_only"])
        self.assertEqual(data["violations"], [])
        self.assertIn("registry 변화", md.read_text())
        self.assertEqual(sorted(p.name for p in (self.root / "report").iterdir()),
                         ["live.json", "live.md"])
        self.assertEqual(self.registry.read_bytes(), b"row-1\n")

    def test_main_without_out_prints_json_and_a_violation_exits_1(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = live.main(["--duration", "0.02", "--period", "0.01", "--registry",
                              str(self.registry), "--cache-dir", str(self.cache)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue())["tool"], "e2e_latency.live")
        bad = {"tool": "x", "violations": [{"event": "os.kill", "kind": "signal"}]}
        with mock.patch.object(live, "run", return_value=bad), \
                mock.patch.object(live, "to_markdown", return_value=""), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(live.main(["--registry", str(self.registry),
                                        "--cache-dir", str(self.cache)]), 1)

    def test_bad_arguments_are_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            live.main(["--duration", "0"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            live.main(["--dur", "5"])           # no prefix abbreviation


if __name__ == "__main__":
    unittest.main()
