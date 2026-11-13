"""The basic collection starts early when the registry or the producer's projection changes.

The pump keeps the rest that protects the CPU; Fleet writes neither watched file; without
kernel notification, or on a network file, the 2 s tick alone remains.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet.refresh import RefreshPump  # noqa: E402
from fleet import render  # noqa: E402


class FakePump:
    """A pump on a fake clock whose runs are synchronous: duration and CPU time are given."""

    def __init__(self, *, adaptive, duration, cpu, interval=2.0):
        self.clock, self.cpu_now, self.runs = [0.0], [0.0], []
        self.duration, self.cpu = duration, cpu

        def produce():
            self.runs.append(self.clock[0])
            self.clock[0] += self.duration
            self.cpu_now[0] += self.cpu
            return "ok"

        def thread(**options):
            return SimpleNamespace(start=options["target"], is_alive=lambda: False, join=lambda _timeout: None)

        self.patch = mock.patch("fleet.refresh.time.thread_time", side_effect=lambda: self.cpu_now[0])
        self.patch.start()
        self.pump = RefreshPump(produce, interval, clock=lambda: self.clock[0], thread_factory=thread,
                                adaptive_cadence=adaptive)
        self.pump.start()
        self.pump.poll()

    def close(self):
        self.pump.stop()
        self.patch.stop()


class RequestSoonTest(unittest.TestCase):
    def test_a_change_starts_the_next_run_at_the_end_of_the_adaptive_rest_not_the_tick(self):
        fake = FakePump(adaptive=True, duration=0.3, cpu=0.1)     # rest = max(0.3, 4 * 0.1) = 0.4 s
        self.addCleanup(fake.close)
        self.assertFalse(fake.pump.request_due(now=1.0))           # the 2 s tick is not due
        self.assertFalse(fake.pump.request_soon(now=0.5))          # still inside the rest (0.3 + 0.4)
        self.assertTrue(fake.pump.request_soon(now=0.8))           # rest over: starts, tick not waited for
        self.assertEqual(len(fake.runs), 2)

    def test_a_source_that_keeps_changing_never_beats_the_rest(self):
        fake = FakePump(adaptive=True, duration=0.3, cpu=0.3)     # rest = 1.2 s
        self.addCleanup(fake.close)
        for step in range(1, 301):                                 # a change every 0.05 s for 15 s
            fake.clock[0] = max(fake.clock[0], step * 0.05)       # time passes outside the runs too
            fake.pump.request_soon()
        gaps = [b - a for a, b in zip(fake.runs, fake.runs[1:])]
        self.assertGreater(len(fake.runs), 3)
        self.assertGreaterEqual(min(gaps), 0.3 + 1.2 - 1e-9)       # each run began after its full rest
        self.assertLess(len(fake.runs), 15 / 1.5 + 2)              # at most one run per run + rest

    def test_the_failure_backoff_is_kept(self):
        fake = FakePump(adaptive=True, duration=0.3, cpu=0.0)
        self.addCleanup(fake.close)
        fake.pump._failure_streak = 2
        fake.pump._soonest = fake.clock[0] + 4.0
        self.assertFalse(fake.pump.request_soon(now=fake.clock[0] + 1.0))

    def test_a_pump_that_is_not_adaptive_keeps_its_tick(self):
        fake = FakePump(adaptive=False, duration=0.3, cpu=0.1)
        self.addCleanup(fake.close)
        self.assertFalse(fake.pump.request_soon(now=1.0))
        self.assertTrue(fake.pump.request_soon(now=2.4))           # only once the tick is due

    def test_a_stopped_pump_is_never_started(self):
        fake = FakePump(adaptive=True, duration=0.3, cpu=0.1)
        self.addCleanup(fake.patch.stop)
        fake.pump.stop()
        self.assertFalse(fake.pump.request_soon(now=10.0))


class BasicSourceEventsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = self.root / "reg" / "jobs.log"
        self.registry.parent.mkdir()
        self.registry.write_text("row\n", encoding="utf-8")
        self.state = self.root / "state"
        self.waits = self.state / "hearting" / "human-waits"
        self.waits.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        from fleet.collectors import dispatch
        patch = mock.patch.object(dispatch, "human_gate_registry_paths", return_value=[str(self.registry)])
        patch.start()
        self.addCleanup(patch.stop)

    def events(self):
        events = render._basic_source_events()
        self.assertIsNotNone(events)
        self.addCleanup(events.stop)
        self.assertTrue(events.ready.wait(2))
        events.take_changed()
        return events

    def test_a_registry_write_and_a_projection_publish_wake_it(self):
        events = self.events()
        self.assertFalse(events.wait_changed(0.3))
        with self.registry.open("a", encoding="utf-8") as handle:
            handle.write("more\n")
        self.assertTrue(events.wait_changed(2.0))
        (self.waits / "projection.json").write_text("{}", encoding="utf-8")
        self.assertTrue(events.wait_changed(2.0))

    def test_fleets_own_writes_in_the_state_directory_do_not_wake_it(self):
        events = self.events()
        for name in (".demand", ".demand.lock", ".projection.lock", "fleet.json", "notes.json"):
            (self.waits / name).write_text("{}", encoding="utf-8")
        (self.root / "reg" / "other.log").write_text("x", encoding="utf-8")
        self.assertFalse(events.wait_changed(0.8))

    def test_a_network_source_or_no_notification_leaves_the_tick(self):
        import claude_process_identity as identity
        with mock.patch.object(identity, "is_local_path", return_value=False):
            self.assertIsNone(render._basic_source_events())
        from fleet import native_events
        with mock.patch.object(native_events, "NativeDisplayEvents", side_effect=OSError("none")):
            self.assertIsNone(render._basic_source_events())


if __name__ == "__main__":
    unittest.main()
