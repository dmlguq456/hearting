#!/usr/bin/env python3
"""Both session supervisors make their continuation and stage decisions through one module."""
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import session_supervisor_decisions as D  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SUPERVISORS = {"claude": load("claude-session-supervisor"), "codex": load("codex-app-server-supervisor")}


class SharedDecisionsTest(unittest.TestCase):
    def test_owner_default_idle_window_remains_7200_seconds(self):
        clock = SimpleNamespace(now=0.0)
        quiet = SimpleNamespace(active=False, uncertain=False)
        activity = D.OwnerTurnActivity(lambda: quiet, clock=lambda: clock.now)
        self.assertEqual(activity.window_seconds, 7200.0)
        self.assertFalse(activity.expired(now=0))
        clock.now = 7199
        self.assertFalse(activity.expired(now=7199))
        clock.now = 7200
        self.assertTrue(activity.expired(now=7200))

    def test_owner_idle_window_renews_on_payload_or_attributed_activity(self):
        clock = SimpleNamespace(now=0.0)
        observation = SimpleNamespace(active=False, uncertain=False)
        activity = D.OwnerTurnActivity(
            lambda: observation, window_seconds=10, sample_seconds=1,
            clock=lambda: clock.now,
        )
        self.assertFalse(activity.expired(now=0))
        clock.now = 9
        self.assertFalse(activity.expired(now=9))
        clock.now = 12
        self.assertFalse(activity.expired(payload_progress=True, now=12))
        clock.now = 21
        self.assertFalse(activity.expired(now=21))
        observation.active = True
        clock.now = 22
        self.assertFalse(activity.expired(now=22))
        observation.active = False
        clock.now = 31
        self.assertFalse(activity.expired(now=31))
        clock.now = 32
        self.assertTrue(activity.expired(now=32))

    def test_owner_unknown_never_expires_and_restarts_a_full_confirmed_quiet_window(self):
        clock = SimpleNamespace(now=0.0)
        observation = SimpleNamespace(active=False, uncertain=False)
        activity = D.OwnerTurnActivity(
            lambda: observation, window_seconds=10, sample_seconds=1,
            clock=lambda: clock.now,
        )
        self.assertFalse(activity.expired(now=0))
        clock.now = 7
        observation.uncertain = True
        self.assertFalse(activity.expired(now=7))
        clock.now = 100
        self.assertFalse(activity.expired(now=100))
        observation.uncertain = False
        clock.now = 101
        self.assertFalse(activity.expired(now=101))
        clock.now = 110
        self.assertFalse(activity.expired(now=110))
        clock.now = 111
        self.assertTrue(activity.expired(now=111))

    def test_both_supervisors_use_the_shared_error_and_notice(self):
        for name, module in SUPERVISORS.items():
            with self.subTest(supervisor=name):
                self.assertIs(module.SupervisorError, D.SupervisorError)
                self.assertIs(module._apply_notice, D.apply_notice)

    def test_an_exhausted_admit_warns_through_the_callers_emit_on_both(self):
        ledger = SimpleNamespace(gross_remaining=0, stall_remaining=0, reserved_remaining=0,
                                 admit=lambda **kw: SimpleNamespace(admitted=False, gross_remaining=0,
                                                                    stall_remaining=0, reserved_remaining=0))
        for name, module in SUPERVISORS.items():
            with self.subTest(supervisor=name):
                events = []
                with mock.patch.object(D.budget_record, "reserve", return_value=(False, "")), \
                     mock.patch.object(D.budget_record, "record_warning", side_effect=OSError("disk")), \
                     mock.patch.object(module, "emit", events.append):
                    verdict, notice = module._admit_continuation(
                        ledger, "/state", parent_attempt_id="att-1", route_id="rt-1", route_hash="sha256:1",
                        ordinal=1, purpose="ordinary", stalled=False)
                self.assertFalse(verdict.admitted)
                self.assertEqual(notice, "")
                self.assertEqual([event["type"] for event in events],
                                 ["dispatch.supervisor.continuation-budget-warning-unrecorded"])

    def test_a_spent_reserve_refuses_the_terminal_handoff_alike(self):
        ledger = SimpleNamespace(reserved_remaining=0)
        for name, module in SUPERVISORS.items():
            with self.subTest(supervisor=name), self.assertRaises(D.SupervisorError):
                module._seal_terminal_handoff_or_raise(
                    ledger, "/state", args=SimpleNamespace(), ordinal=1, failure_reason="budget",
                    terminal_handoff_issued=[False])

    def test_stage_advance_is_off_unless_enabled_and_names_each_runtime(self):
        for name, module in SUPERVISORS.items():
            with self.subTest(supervisor=name):
                self.assertIsNone(module.attempt_stage_advance(SimpleNamespace(enable_stage_advance=False), [], set()))
        source = {name: (HERE / f"{file}.py").read_text() for name, file in
                  (("claude", "claude-session-supervisor"), ("codex", "codex-app-server-supervisor"))}
        self.assertIn('default_harness="claude"', source["claude"])
        self.assertIn('default_harness="codex"', source["codex"])


if __name__ == "__main__":
    unittest.main()
