import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet.refresh import RefreshPump  # noqa: E402
from fleet import render  # noqa: E402


class RefreshPumpTest(unittest.TestCase):
    def _wait(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("timed out waiting for refresh pump state")

    def test_requests_and_polls_stay_fast_while_collector_is_blocked(self):
        entered = threading.Event()
        release = threading.Event()

        def producer():
            entered.set()
            release.wait()
            return "done"

        pump = RefreshPump(producer, 2.0)
        self.assertTrue(pump.start())
        self.assertTrue(entered.wait(1.0))
        samples = []
        for _ in range(200):
            started = time.perf_counter()
            pump.request(force=True)
            pump.poll(0)
            samples.append(time.perf_counter() - started)
        # The slow producer is still blocked; only lock-protected scheduling ran here.
        self.assertLess(sorted(samples)[int(len(samples) * 0.95)], 0.020)
        self.assertEqual(pump.generation, 0)
        release.set()
        self._wait(lambda: pump.generation >= 1)
        pump.stop()

    def test_eight_requests_coalesce_to_one_follow_up_with_no_overlap(self):
        started = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        lock = threading.Lock()
        calls = 0
        active = 0
        max_active = 0

        def producer():
            nonlocal calls, active, max_active
            with lock:
                index = calls
                calls += 1
                active += 1
                max_active = max(max_active, active)
            started[index].set()
            release[index].wait()
            with lock:
                active -= 1
            return index

        pump = RefreshPump(producer, 2.0)
        pump.start()
        self.assertTrue(started[0].wait(1.0))
        for _ in range(8):
            self.assertFalse(pump.request(force=True))
        release[0].set()
        self.assertTrue(started[1].wait(1.0))
        release[1].set()
        self._wait(lambda: pump.generation == 2 and not pump.running)
        self.assertEqual(calls, 2)
        self.assertEqual(max_active, 1)
        pump.stop()

    def test_failure_preserves_last_good_and_next_success_advances_generation(self):
        values = iter(("old", RuntimeError("boom"), "new"))

        def producer():
            value = next(values)
            if isinstance(value, Exception):
                raise value
            return value

        pump = RefreshPump(producer, 2.0)
        pump.start()
        self._wait(lambda: pump.generation == 1)
        self.assertEqual(pump.poll(0), (1, "old"))

        pump.request(force=True)
        self._wait(lambda: pump.last_error is not None and not pump.running)
        self.assertEqual(pump.generation, 1)
        self.assertEqual(pump.poll(0), (1, "old"))

        pump.request(force=True)
        self._wait(lambda: pump.generation == 2)
        self.assertEqual(pump.poll(1), (2, "new"))
        pump.stop()

    def test_stop_has_bounded_join_for_stuck_daemon_worker(self):
        entered = threading.Event()
        release = threading.Event()

        def producer():
            entered.set()
            release.wait()

        pump = RefreshPump(producer, 2.0)
        pump.start()
        self.assertTrue(entered.wait(1.0))
        started = time.perf_counter()
        pump.stop(join_timeout=0.02)
        self.assertLess(time.perf_counter() - started, 0.15)
        release.set()

    def test_curses_loop_reaches_key_input_while_first_snapshot_is_blocked(self):
        self.addCleanup(setattr, render, "_BLINK_ON", render._BLINK_ON)
        collector_entered = threading.Event()
        release = threading.Event()
        getch_called = threading.Event()
        collector_threads = []

        def collector(harness_filter=None):
            collector_threads.append(threading.get_ident())
            collector_entered.set()
            release.wait()
            return [], []

        collector.last_resource_jobs = []
        collector.last_usage_snapshots = {}

        class Screen:
            def timeout(self, _value):
                pass

            def getch(self):
                getch_called.set()
                return ord("q")

        result = []

        def run_loop():
            result.append(render._loop(Screen(), collector, None, "both", 2.0))

        with mock.patch.object(render, "_init_colors"), \
             mock.patch.object(render, "_draw"), \
             mock.patch.object(render.curses, "curs_set"), \
             mock.patch.dict(render.os.environ, {"HERDR_ENV": "1"}):
            thread = threading.Thread(target=run_loop)
            thread.start()
            self.assertTrue(collector_entered.wait(1.0))
            self.assertTrue(getch_called.wait(0.2))
            release.set()
            thread.join(1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [0])
        self.assertNotEqual(collector_threads, [thread.ident])


if __name__ == "__main__":
    unittest.main()
