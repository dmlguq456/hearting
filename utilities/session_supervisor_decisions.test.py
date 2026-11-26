#!/usr/bin/env python3
"""Both session supervisors make their continuation and stage decisions through one module."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import session_supervisor_decisions as D  # noqa: E402
import dispatch_budget_record as BR  # noqa: E402
from dispatch_continuation_budget import ContinuationBudget, ContinuationLedger  # noqa: E402


class ContinuationLedgerInheritanceTest(unittest.TestCase):
    def test_restores_next_ordinal_remaining_and_terminal_handoff_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            owner = "att-ledger-inherit"
            remaining = {"gross_remaining": 3, "stall_remaining": 2,
                         "reserved_remaining": 1}
            self.assertTrue(BR.reserve(root, parent_attempt_id=owner, route_id="rt-test",
                route_hash="sha256:test", ordinal=0, purpose="ordinary", klass="gross",
                remaining=remaining)[0])
            self.assertTrue(BR.reserve(root, parent_attempt_id=owner, route_id="rt-test",
                route_hash="sha256:test", ordinal=1, purpose="ordinary", klass="stall",
                remaining=remaining)[0])
            self.assertTrue(BR.reserve(root, parent_attempt_id=owner, route_id="rt-test",
                route_hash="sha256:test", ordinal=1, purpose="terminal-handoff", klass="reserved",
                remaining=remaining)[0])
            before = BR._ledger_path(root, owner).read_bytes()
            budget = ContinuationBudget(limit=3, source="fixture", reserved=1, stall=2)
            ledger = ContinuationLedger(budget)
            next_ordinal, handoff = D.inherit_continuation_ledger(ledger, root, owner)
            self.assertEqual(next_ordinal, 2)
            self.assertTrue(handoff)
            # Phase F: replaying the progress-carrying `gross` reservation no
            # longer draws down the gross remainder; the stall row and the
            # reserved terminal-handoff row still replay their own spend.
            self.assertEqual((ledger.gross_remaining, ledger.stall_remaining,
                              ledger.reserved_remaining),
                             (budget.ordinary, budget.stall - 1, budget.reserved - 1))
            self.assertEqual(BR._ledger_path(root, owner).read_bytes(), before)


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


class ReparkClaimTest(unittest.TestCase):
    def test_parking_the_same_children_again_after_a_resource_turn_claims_nothing_new(self):
        with tempfile.TemporaryDirectory() as temp:
            def park():
                return BR.claim_terminal_handoff(Path(temp), owner_attempt_id="att-owner",
                                                 route_hash="sha256:r", child_attempt_ids=["att-b", "att-a"])
            first, again = park(), park()
            self.assertEqual(first, again)
            self.assertEqual(len(list(Path(temp).rglob("claim.json"))), 1)


class JoinWithResourceAttentionTest(unittest.TestCase):
    """A supervisor parked on model children is not deaf to the resources its owner holds."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state.json"
        D.completion_join.write_supervisor_state(self.state, "att-parent", set(), phase="parked")
        self.args = SimpleNamespace(parent_attempt_id="att-parent", join_timeout=100.0)
        self.control = SimpleNamespace(thread_id="native", pending=lambda: False)
        self.now = [0.0]
        self.calls = []

    def join(self, args, attempts, *, state="timeout"):
        self.calls.append(args.join_timeout)
        self.now[0] += args.join_timeout
        return {"state": state, "children": []}

    def wrapped(self, **kw):
        return D.join_with_resource_attention(self.args, self.state, self.control, set(), self.join,
                                              clock=lambda: self.now[0], **kw)

    def test_an_owner_holding_nothing_waits_the_same_total_in_pieces_without_a_look(self):
        with mock.patch.object(D.resource_wait, "context", return_value=None) as context, \
             mock.patch.object(D.resource_wait, "observe_once") as look:
            receipt = self.wrapped()({"att-child"})
        self.assertEqual(self.calls, [30.0, 30.0, 30.0, 10.0])
        self.assertEqual(receipt["state"], "timeout")
        self.assertEqual(context.call_count, 1)
        look.assert_not_called()
        self.calls.clear()
        with mock.patch.object(D.resource_wait, "context", side_effect=ValueError("unreadable")):
            self.wrapped()({"att-child"})
        self.assertEqual(sum(self.calls), 100.0)
        # A wait with no deadline of its own is still the one join.
        self.calls.clear()
        self.args.join_timeout = 0
        self.wrapped()({"att-child"})
        self.assertEqual(self.calls, [0])

    def test_a_pending_correction_ends_the_wait_at_the_next_checkpoint(self):
        """Observation 5: the correction is not held behind the model children."""
        arrived = []
        self.control = SimpleNamespace(thread_id="native", pending=lambda: bool(arrived))
        def join(args, attempts):
            self.calls.append(args.join_timeout)
            if len(self.calls) == 2:
                arrived.append(True)  # the supervisor's `correct` lands during the second piece
            return {"state": "timeout", "children": []}
        for holds in (None, object()):
            with self.subTest(holds=holds is not None):
                self.calls.clear()
                arrived.clear()
                with mock.patch.object(D.resource_wait, "context", return_value=holds), \
                     mock.patch.object(D.resource_wait, "observe_once",
                                       return_value=D.resource_wait.WAITING) as look:
                    with self.assertRaises(D.CorrectionReady) as caught:
                        D.join_with_resource_attention(self.args, self.state, self.control, set(), join,
                                                       clock=lambda: 0.0, check_seconds=30.0)({"att-child"})
                self.assertEqual(self.calls, [30.0, 30.0])
                self.assertEqual(look.call_count, 1 if holds is not None else 0)
                self.assertIn("still running", caught.exception.prompt)
                # caught where a resource receipt is, and never a delivery of the children
                self.assertIsInstance(caught.exception, D.ResourceAttentionReady)
                self.assertIsInstance(caught.exception, D.StopWaiting)
        # A ready join wins over a correction that arrived meanwhile: the children are delivered.
        arrived.append(True)
        receipt = D.join_with_resource_attention(
            self.args, self.state, self.control, set(),
            lambda args, attempts: {"state": "ready", "children": []})({"att-child"})
        self.assertEqual(receipt["state"], "ready")
        # An unreadable channel is not a correction.
        def broken():
            raise OSError("input state unreadable")
        self.control = SimpleNamespace(thread_id="native", pending=broken)
        self.calls.clear()
        with mock.patch.object(D.resource_wait, "context", return_value=None):
            self.assertEqual(self.wrapped()({"att-child"})["state"], "timeout")

    def test_a_holder_not_read_yet_keeps_the_short_pieces_until_it_is_read(self):
        """RV-PR1602 finding 2: one transient read error is "not read yet", never "holds nothing"."""
        unread = D.resource_wait.HolderUnobserved("registry-unobserved:JSONDecodeError")
        self.args.join_timeout = 3600.0
        with mock.patch.object(D.resource_wait, "context", side_effect=[unread, unread, object()]) as context, \
             mock.patch.object(D.resource_wait, "observe_once", return_value=D.resource_wait.WAITING) as look:
            receipt = self.wrapped(check_seconds=30.0)({"att-child"})
        self.assertEqual(receipt["state"], "timeout")
        self.assertLessEqual(max(self.calls), 30.0)
        self.assertEqual(sum(self.calls), 3600.0)
        # Read again at each checkpoint until it reads; then the pieces look at the resources.
        self.assertEqual(context.call_count, 3)
        self.assertEqual(look.call_count, len(self.calls) - 1)

    def test_a_holder_read_later_as_holding_nothing_is_not_looked_at(self):
        unread = D.resource_wait.HolderUnobserved("registry-unobserved:OSError")
        with mock.patch.object(D.resource_wait, "context", side_effect=[unread, None]) as context, \
             mock.patch.object(D.resource_wait, "observe_once") as look:
            receipt = self.wrapped(check_seconds=30.0)({"att-child"})
        self.assertEqual(receipt["state"], "timeout")
        self.assertEqual(self.calls, [30.0, 30.0, 30.0, 10.0])
        self.assertEqual(context.call_count, 2)
        look.assert_not_called()

    def test_held_resources_are_looked_at_between_pieces_of_the_same_total_wait(self):
        with mock.patch.object(D.resource_wait, "context", return_value=object()), \
             mock.patch.object(D.resource_wait, "observe_once", return_value=D.resource_wait.WAITING) as look:
            receipt = self.wrapped(check_seconds=30.0)({"att-child"})
        # The wait is the same 100s, cut at 30s; the model children stay joined, never delivered.
        self.assertEqual(self.calls, [30.0, 30.0, 30.0, 10.0])
        self.assertEqual(sum(self.calls), 100.0)
        self.assertEqual(receipt["state"], "timeout")
        self.assertEqual(look.call_count, 4)  # after every piece, the last one too

    def test_a_ready_join_receipt_ends_the_wait_without_a_look(self):
        def ready(args, attempts):
            return {"state": "ready", "children": []}
        with mock.patch.object(D.resource_wait, "context", return_value=object()), \
             mock.patch.object(D.resource_wait, "observe_once") as look:
            receipt = D.join_with_resource_attention(self.args, self.state, self.control, set(), ready)({"a"})
        self.assertEqual(receipt["state"], "ready")
        look.assert_not_called()

    def test_a_resource_receipt_stops_the_wait_and_a_bare_correction_does_not(self):
        def receipt_written(*_):
            import hashlib, json
            receipt = {"type": "resource-completion", "parent_attempt_id": "att-parent",
                       "session_id": "native", "route_id": "rt", "route_hash": "h", "jobs": "/j",
                       "node": "n", "run_id": "r", "resource_key": "b" * 64,
                       "resource_sha256": "c" * 64, "state": "needs-attention", "exit_code": 3,
                       "reason": "x", "verification_pass": False, "workflow_complete": False,
                       "successors": []}
            digest = hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            resource = {"session_id": "native", "delivered": [], "outbox": {
                "receipt_id": "resource-" + digest[:32], "digest": digest, "key": "b" * 64, "receipt": receipt}}
            D.completion_join._write_supervisor_state_unlocked(
                self.state, "att-parent", set(), phase="deliverable", resource=resource)
            return "Runtime resource receipt"
        with mock.patch.object(D.resource_wait, "context", return_value=object()), \
             mock.patch.object(D.resource_wait, "observe_once", side_effect=receipt_written):
            with self.assertRaises(D.ResourceAttentionReady) as caught:
                self.wrapped(check_seconds=30.0)({"att-child"})
        self.assertEqual(caught.exception.prompt, "Runtime resource receipt")
        self.assertEqual(self.calls, [30.0])
        self.assertTrue(issubclass(D.ResourceAttentionReady, D.StopWaiting))
        # A pending user correction is not a resource receipt: the children keep waiting.
        self.calls.clear()
        self.now[0] = 0.0
        D.completion_join.write_supervisor_state(self.state, "att-parent", set(), phase="parked")
        self.state.write_text(self.state.read_text())
        with mock.patch.object(D.resource_wait, "context", return_value=object()), \
             mock.patch.object(D.resource_wait, "observe_once", return_value="Continue ... correction"):
            receipt = self.wrapped(check_seconds=50.0)({"att-child"})
        self.assertEqual(receipt["state"], "timeout")


if __name__ == "__main__":
    unittest.main()
