"""Regressions for the registered-supervisor transient retry seam."""
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


def source_root():
    configured = os.environ.get("AGENT_HOME")
    return Path(configured) if configured else Path(__file__).resolve().parents[1]


def load(name, path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TransientRetryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = source_root()
        cls.decisions = load("transient_retry_decisions", root / "utilities/session_supervisor_decisions.py")
        cls.supervisor = load("transient_retry_supervisor", root / "utilities/codex-app-server-supervisor.py")

    def test_registered_policy_allows_only_typed_overload_and_shared_schedule(self):
        policy = getattr(self.decisions, "transient_retry_delay", None)
        self.assertTrue(callable(policy), "registered structured transient retry policy is missing")
        self.assertEqual([policy("serverOverloaded", n) for n in range(5)], [60, 120, 240, 480, 960])
        self.assertIsNone(policy({"kind": "responseTooManyFailedAttempts", "http_status": 429}, 0))
        self.assertIsNone(policy({"kind": "responseTooManyFailedAttempts", "http_status": 503}, 5))
        self.assertIsNone(policy({"kind": "unauthorized"}, 0))
        self.assertIsNone(policy({"kind": "serverOverloaded", "http_status": True}, 0))

    def test_transient_failure_retries_same_thread_with_virtual_time(self):
        self.assertTrue(callable(getattr(self.supervisor, "_run_turn_once", None)),
                        "supervisor transport retry seam is missing")
        clock = SimpleNamespace(now=10.0, slept=[])
        events = []
        payload = {"turn_id": "turn-1", "codex_error_info": "serverOverloaded"}
        calls = []

        def once(server, *, thread_id, prompt, args, activity=None):
            calls.append((thread_id, prompt))
            if len(calls) == 1:
                raise self.supervisor.TurnFailed(payload)
            return "done", {"type": "agent_message", "text": "ok"}

        def sleep(seconds):
            clock.slept.append(seconds)
            clock.now += max(seconds, 60)

        server = SimpleNamespace()
        with mock.patch.object(self.supervisor, "_run_turn_once", side_effect=once), \
             mock.patch.object(self.supervisor, "emit", side_effect=events.append):
            result = self.supervisor.run_turn(server, thread_id="thread-1", prompt="prompt",
                args=SimpleNamespace(owner_input=None), clock=lambda: clock.now, sleep=sleep)
        self.assertEqual(result[0], "done")
        self.assertEqual([item[0] for item in calls], ["thread-1", "thread-1"])
        self.assertGreaterEqual(clock.now, 70)
        self.assertEqual([e["type"] for e in events], [
            "dispatch.supervisor.transient-wait.started",
            "dispatch.supervisor.transient-wait.ended",
        ])

    def test_real_work_resets_delay_chain_and_sixth_failure_stops(self):
        self.assertTrue(callable(getattr(self.supervisor, "_run_turn_once", None)),
                        "supervisor transport retry seam is missing")
        clock = SimpleNamespace(now=0.0)
        events = []
        prompts = []
        failures = [
            self.supervisor.TurnFailed({"turn_id": f"turn-{n}",
                "codex_error_info": "serverOverloaded"}, progressed=(n == 1))
            for n in range(1, 8)
        ]

        def once(_server, *, thread_id, prompt, args, activity=None):
            prompts.append(prompt)
            if failures:
                raise failures.pop(0)
            return "done", None

        def sleep(seconds):
            clock.now += max(seconds, 60)

        with mock.patch.object(self.supervisor, "_run_turn_once", side_effect=once), \
             mock.patch.object(self.supervisor, "emit", side_effect=events.append):
            with self.assertRaises(self.supervisor.TurnFailed):
                self.supervisor.run_turn(SimpleNamespace(), thread_id="thread-3", prompt="p",
                    args=SimpleNamespace(owner_input=None), clock=lambda: clock.now, sleep=sleep)
        starts = [e for e in events if e["type"].endswith("transient-wait.started")]
        self.assertEqual(len(starts), 6)  # one after real progress, then five no-progress retries
        self.assertEqual([e["ordinal"] for e in starts], [1, 1, 2, 3, 4, 5])
        self.assertIn("completed real work", prompts[1])

    def test_non_transient_and_account_limit_keep_existing_failure(self):
        usage = self.supervisor.codex_turn_failure_terminal({
            "codex_error_info": {"kind": "usageLimitExceeded"}})
        rate = self.supervisor.codex_turn_failure_terminal({
            "codex_error_info": {"kind": "responseTooManyFailedAttempts", "http_status": 429}})
        self.assertEqual((usage.failure_class, rate.failure_class), ("capacity", "capacity"))


if __name__ == "__main__":
    unittest.main()
