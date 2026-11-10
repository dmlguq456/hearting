#!/usr/bin/env python3
"""Codex in-turn carrier: owed records at PostToolUse/Stop and one follow-through pass."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import codex_turn_carrier as carrier  # noqa: E402

HOOKS = ROOT / "adapters" / "codex" / "hooks"
WORKER_ENV = ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
              "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "MEM_DISTILL")


def completion(delivery_id="delivery-a", action="complete-open"):
    return {"delivery_id": delivery_id,
            "receipt": {"children": [{"attempt_id": "att-1", "required_action": action}]}}


class CarrierCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in WORKER_ENV:
            os.environ.pop(key, None)
        self.roots = mock.patch.object(carrier, "_roots", return_value=[self.root])
        self.roots.start()
        self.watches = mock.patch.object(carrier, "watch_receipts", return_value=(None, []))
        self.watches.start()
        self.acked = []
        def acknowledge(sid, batches):
            self.acked.append(batches)
            return [record["delivery_id"] for _root, records in batches for record in records]
        mock.patch.object(carrier, "acknowledge", side_effect=acknowledge).start()

    def tearDown(self):
        mock.patch.stopall()
        self.env.stop()
        self.tmp.cleanup()

    def payload(self, **extra):
        return {"session_id": "thread-1", "turn_id": "turn-1", "hook_event_name": "Stop", **extra}

    def run_stop(self, **extra):
        out = io.StringIO()
        carrier.stop(self.payload(**extra), out)
        return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    def run_post(self):
        out = io.StringIO()
        carrier.post_tool(self.payload(hook_event_name="PostToolUse", tool_name="Bash"), out)
        return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


class StopTest(CarrierCase):
    def test_silent_without_records_or_owed_action(self):
        with mock.patch.object(carrier, "collect", return_value=("", [])):
            self.assertEqual(self.run_stop(), [])

    def test_owed_records_continue_the_turn_and_are_acked(self):
        batch = [(self.root, [completion()])]
        with mock.patch.object(carrier, "collect", return_value=("RECEIPT TEXT", batch)):
            output = self.run_stop()
        self.assertEqual(output[0]["decision"], "block")
        self.assertIn("RECEIPT TEXT", output[0]["reason"])
        self.assertEqual(len(self.acked), 1)
        state = carrier.read_state("thread-1", "turn-1", self.root)
        self.assertIn("delivery-a", state["rendered"])
        self.assertEqual(state["owed"][0]["required_action"], "complete-open")

    def test_rendered_records_are_skipped_on_the_next_stop(self):
        carrier.note_received("thread-1", "turn-1", [], rendered=["delivery-a"], root=self.root)
        seen = {}

        def collect(sid, tid, *, budget_seconds, skip=frozenset(), request_context=None):
            seen["skip"] = set(skip)
            return "", []
        with mock.patch.object(carrier, "collect", side_effect=collect):
            self.run_stop(stop_hook_active=True)
        self.assertIn("delivery-a", seen["skip"])

    def test_unmet_next_action_continues_exactly_once(self):
        carrier.note_received("thread-1", "turn-1", carrier.owed_items([completion()]),
                              root=self.root)
        with mock.patch.object(carrier, "collect", return_value=("", [])):
            first = self.run_stop()
            second = self.run_stop(stop_hook_active=True)
        self.assertEqual(first[0]["decision"], "block")
        self.assertIn("required_action=complete-open", first[0]["reason"])
        self.assertEqual(second, [])

    def test_a_tool_after_the_receipt_means_no_continuation(self):
        carrier.note_received("thread-1", "turn-1", carrier.owed_items([completion()]),
                              root=self.root)
        carrier.note_tool("thread-1", "turn-1", root=self.root)
        with mock.patch.object(carrier, "collect", return_value=("", [])):
            self.assertEqual(self.run_stop(), [])

    def test_reading_only_records_owe_nothing(self):
        items = carrier.owed_items([
            completion(action="advance-completed"),
            {"delivery_id": "d2", "receipt": {"kind": "notice", "required_action": "read-notice"}}])
        self.assertEqual(items, [])

    def test_steward_check_notice_is_owed(self):
        items = carrier.owed_items([{"delivery_id": "d3", "receipt": {
            "kind": "notice", "subject": "steward-check", "required_action": "run-steward-check"}}])
        self.assertEqual(items[0]["kind"], "steward-check")

    def test_peer_report_prompt_is_owed(self):
        self.assertIsNotNone(carrier.peer_report_item("done\n(peer-from: codex abc [7b])"))
        self.assertIsNone(carrier.peer_report_item("ordinary user prompt"))

    def test_new_turn_starts_fresh_state(self):
        carrier.note_received("thread-1", "turn-0", carrier.owed_items([completion()]),
                              root=self.root)
        with mock.patch.object(carrier, "collect", return_value=("", [])):
            self.assertEqual(self.run_stop(), [])

    def test_worker_sessions_are_silent(self):
        os.environ["AGENT_DISPATCH_DEPTH"] = "1"
        with mock.patch.object(carrier, "collect", return_value=("RECEIPT", [(self.root, [completion()])])):
            self.assertEqual(self.run_stop(), [])

    def test_watch_receipts_continue_the_turn(self):
        module = mock.Mock()
        row = {"watch_id": "w1", "state": "receipt", "receipt_state": "done", "agent": {}}
        with mock.patch.object(carrier, "collect", return_value=("", [])), \
                mock.patch.object(carrier, "watch_receipts", return_value=(module, [row])), \
                mock.patch.object(carrier, "ack_watches") as ack_watches:
            output = self.run_stop()
        self.assertIn("watch_id=w1", output[0]["reason"])
        ack_watches.assert_called_once()


class PostToolTest(CarrierCase):
    def test_owed_records_become_additional_context(self):
        batch = [(self.root, [completion()])]
        with mock.patch.object(carrier, "collect", return_value=("RECEIPT TEXT", batch)):
            output = self.run_post()
        self.assertEqual(output[0]["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("RECEIPT TEXT", output[0]["hookSpecificOutput"]["additionalContext"])
        state = carrier.read_state("thread-1", "turn-1", self.root)
        self.assertEqual(state["tools"], 1)
        self.assertEqual(state["tools_after_receipt"], 0)

    def test_tool_without_records_is_counted_and_silent(self):
        with mock.patch.object(carrier, "collect", return_value=("", [])):
            self.assertEqual(self.run_post(), [])
        self.assertEqual(carrier.read_state("thread-1", "turn-1", self.root)["tools"], 1)


class CollectTest(CarrierCase):
    def test_unackable_completion_is_left_for_the_queue(self):
        import dispatch_session_sweep
        record = completion()
        with mock.patch.object(dispatch_session_sweep, "addressed_records", return_value=([record], 1)), \
                mock.patch.object(dispatch_session_sweep, "sweep_deliver") as sweep, \
                mock.patch.object(carrier, "native_receiving_turn", return_value=None):
            text, batches = carrier.collect("thread-1", "turn-1", budget_seconds=1.0)
        self.assertEqual((text, batches), ("", []))
        sweep.assert_not_called()  # never claimed, so the queue courier is not held off

    def test_ackable_records_are_rendered_with_the_shared_context(self):
        import dispatch_session_sweep
        record = {"delivery_id": "d1", "receipt": {"kind": "notice"}}
        with mock.patch.object(dispatch_session_sweep, "addressed_records", return_value=([record], 1)), \
                mock.patch.object(dispatch_session_sweep, "sweep_deliver", return_value=([record], 1)), \
                mock.patch.object(dispatch_session_sweep, "retain_receiving_turn",
                                  return_value={(str(self.root), "d1")}), \
                mock.patch.object(dispatch_session_sweep, "delivery_context", return_value="CTX"), \
                mock.patch.object(carrier, "native_receiving_turn", return_value=None):
            text, batches = carrier.collect("thread-1", "turn-1", budget_seconds=1.0)
        self.assertEqual(text, "CTX")
        self.assertEqual(batches, [(self.root, [record])])


class RealRecordTest(unittest.TestCase):
    """The shared sweep, ACK and release paths on one real pending notice."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "dispatch"
        self.state.mkdir(mode=0o700)
        self.sid = "019a0000-0000-7000-8000-0000000000aa"
        import session_notice
        with mock.patch.object(session_notice, "_retain", return_value=None):
            self.record = session_notice.notify(
                "codex", self.sid, key="carrier-test", subject="steward-check",
                text="steward check is due", required_action="run-steward-check",
                jobs=self.state / "jobs.log")
        self.assertIsNotNone(self.record)
        mock.patch.object(carrier, "_roots", return_value=[self.state]).start()
        mock.patch.object(carrier, "native_receiving_turn", return_value=None).start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def state_of(self):
        import dispatch_pending_delivery as pending
        return pending.read(self.state, self.sid, self.record["delivery_id"])["state"]

    def test_unacked_claim_is_returned_to_pending(self):
        import dispatch_session_sweep
        with mock.patch.object(dispatch_session_sweep, "retain_receiving_turn", return_value=set()):
            text, batches = carrier.collect(self.sid, "turn-x", budget_seconds=5.0)
        self.assertEqual((text, batches), ("", []))
        self.assertEqual(self.state_of(), "pending")

    def test_rendered_notice_is_acked_once(self):
        text, batches = carrier.collect(self.sid, "turn-x", budget_seconds=5.0)
        self.assertIn("steward check is due", text)
        carrier.acknowledge(self.sid, batches)
        self.assertEqual(self.state_of(), "acked")
        again, _ = carrier.collect(self.sid, "turn-x", budget_seconds=5.0)
        self.assertEqual(again, "")


class ManifestTest(unittest.TestCase):
    def test_hooks_json_registers_the_carrier_on_stop_and_post_tool(self):
        config = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
        stop = [h["command"] for m in config["hooks"]["Stop"] for h in m["hooks"]]
        post = [h["command"] for m in config["hooks"]["PostToolUse"] for h in m["hooks"]]
        self.assertTrue(any('run-hook.sh" turn-carrier.py stop' in c for c in stop))
        self.assertTrue(any('run-hook.sh" turn-carrier.py post-tool' in c for c in post))
        self.assertIn("turn-carrier.py", (HOOKS / "run-hook.sh").read_text(encoding="utf-8"))
        self.assertTrue(os.access(HOOKS / "turn-carrier.py", os.X_OK))

    def test_bridge_is_fail_open_on_bad_input(self):
        spec = importlib.util.spec_from_file_location("turn_carrier_bridge", HOOKS / "turn-carrier.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(sys, "argv", ["turn-carrier.py", "stop"]), \
                mock.patch.object(sys, "stdin", io.StringIO("not json")):
            self.assertEqual(module.main(), 0)


if __name__ == "__main__":
    unittest.main()
