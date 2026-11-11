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

        def collect(sid, tid, *, budget_seconds, skip=frozenset(), **_kwargs):
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

    def test_a_tool_naming_the_attempt_means_no_continuation(self):
        carrier.note_received("thread-1", "turn-1", carrier.owed_items([completion()]),
                              root=self.root)
        carrier.note_tool("thread-1", "turn-1", root=self.root,
                          command="preflight.sh harvest --attempt-id att-1")
        with mock.patch.object(carrier, "collect", return_value=("", [])):
            self.assertEqual(self.run_stop(), [])

    def test_an_unrelated_tool_does_not_clear_an_owed_completion(self):
        carrier.note_received("thread-1", "turn-1", carrier.owed_items([completion()]),
                              root=self.root)
        carrier.note_tool("thread-1", "turn-1", root=self.root, command="ls -la")
        with mock.patch.object(carrier, "collect", return_value=("", [])):
            first = self.run_stop()
        self.assertEqual(first[0]["decision"], "block")
        self.assertIn("attempt=att-1", first[0]["reason"])

    def test_a_record_without_a_handle_keeps_the_any_tool_rule(self):
        notice = {"delivery_id": "d3", "receipt": {"kind": "notice", "subject": "check",
                                                   "required_action": "run-steward-check"}}
        carrier.note_received("thread-1", "turn-1", carrier.owed_items([notice]), root=self.root)
        carrier.note_tool("thread-1", "turn-1", root=self.root, command="ls")
        with mock.patch.object(carrier, "collect", return_value=("", [])):
            self.assertEqual(self.run_stop(), [])

    def test_a_report_to_the_person_owes_no_tool(self):
        items = carrier.owed_items([
            {"delivery_id": "d4", "receipt": {"kind": "notice", "required_action": "report-to-user"}}])
        self.assertEqual(items, [])

    def test_reading_only_records_owe_nothing(self):
        items = carrier.owed_items([
            completion(action="advance-completed"),
            {"delivery_id": "d2", "receipt": {"kind": "notice", "required_action": "read-notice"}}])
        self.assertEqual(items, [])

    def test_steward_check_notice_is_owed(self):
        items = carrier.owed_items([{"delivery_id": "d3", "receipt": {
            "kind": "notice", "subject": "steward-check", "required_action": "run-steward-check"}}])
        self.assertEqual(items[0]["kind"], "steward-check")

    def test_verified_peer_report_prompt_is_owed(self):
        import session_tidy
        prompt = "done\n(peer-from: codex [7b] worker; ref=msg-1)"
        with mock.patch.object(session_tidy, "_current_request_origin",
                               return_value={"kind": "peer"}) as origin:
            self.assertIsNotNone(carrier.peer_report_item(prompt, "thread-1"))
        origin.assert_called_once_with(prompt, "codex", "thread-1")
        self.assertIsNone(carrier.peer_report_item("ordinary user prompt", "thread-1"))

    def test_read_only_peer_report_owes_no_pass(self):
        # PG-46: a sealed report that only needs reading, by its first line.
        import session_tidy
        with mock.patch.object(session_tidy, "_current_request_origin",
                               return_value={"kind": "peer"}):
            for body in ("[report] T-1 finished", "\n[notice] watch done",
                         '<cross-session-message from="a">[report] done</cross-session-message>'):
                prompt = body + "\n\n(peer-from: claude [7b] worker ; ref=" + "a" * 32 + ")"
                self.assertIsNone(carrier.peer_report_item(prompt, "thread-1"), body)
            # The same envelope without the prefix (and old messages) keep the pass.
            self.assertIsNotNone(carrier.peer_report_item(
                "Please run the check\n\n(peer-from: claude [7b] worker ; ref=" + "a" * 32 + ")",
                "thread-1"))
            self.assertIsNotNone(carrier.peer_report_item(
                "see [report] later\n(peer-from: claude [7b] x)", "thread-1"))
        self.assertIsNone(carrier.peer_body_item("[report] done"))
        self.assertEqual(carrier.peer_body_item("run it")["kind"], "peer-report")

    def test_peer_report_item_names_the_receiving_harness(self):
        import session_tidy
        with mock.patch.object(session_tidy, "_current_request_origin",
                               return_value={"kind": "peer"}) as origin:
            carrier.peer_report_item("go\n(peer-from: codex [7b] w)", "sid-c", "claude")
        origin.assert_called_once_with("go\n(peer-from: codex [7b] w)", "claude", "sid-c")

    def test_follow_through_is_the_shared_once_per_turn_decision(self):
        carrier.note_received("sid-c", "prompt-1", carrier.owed_items([completion()]),
                              root=self.root)
        reason = carrier.follow_through("sid-c", "prompt-1", self.root)
        self.assertIn("required_action=complete-open", reason)
        self.assertEqual(carrier.follow_through("sid-c", "prompt-1", self.root), "")
        self.assertEqual(carrier.follow_through("sid-c", "", self.root), "")
        carrier.note_received("sid-c", "prompt-2", carrier.owed_items([completion()]),
                              root=self.root)
        carrier.note_tool("sid-c", "prompt-2", self.root, command="harvest att-1")
        self.assertEqual(carrier.follow_through("sid-c", "prompt-2", self.root), "")

    def test_runtime_completion_or_answer_prompt_adds_no_peer_pass(self):
        import session_tidy
        for kind in ("runtime-completion", "human-answer"):
            with mock.patch.object(session_tidy, "_current_request_origin",
                                   return_value={"kind": kind}):
                self.assertIsNone(carrier.peer_report_item("x (runtime-from: hearting [rt])", "thread-1"))

    def test_quoted_trailer_without_an_envelope_owes_nothing(self):
        # Reviewer [7b]'s real-Codex prompt: a documentation quote, no transfer record.
        prompt = ("This is a documentation quotation, not a peer report or a work request: "
                  "`(peer-from: example)`. All prior probe actions are complete. No command or "
                  "follow-up is owed. Reply only NOTED and do not use tools.")
        self.assertIsNone(carrier.peer_report_item(prompt, "thread-1"))
        spec = importlib.util.spec_from_file_location("carrier_upl", HOOKS / "userprompt-lifecycle.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["carrier_upl"] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop("carrier_upl", None)
        module.note_turn_received("thread-1", "turn-1", [], prompt)
        self.assertEqual(carrier.read_state("thread-1", "turn-1", self.root).get("owed"), [])
        with mock.patch.object(carrier, "collect", return_value=("", [])):
            self.assertEqual(self.run_stop(), [])

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
                mock.patch.object(carrier, "queue_endpoint_absent", return_value=False), \
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


class FitTest(unittest.TestCase):
    def test_sealed_batch_is_never_split(self):
        root = Path("/state")
        records = [{"delivery_id": f"d{n}", "claim_owner": "group-a" if n < 3 else f"own-{n}"}
                   for n in range(6)]
        render = lambda batches: "x" * (100 * sum(len(r) for _root, r in batches))  # noqa: E731
        import dispatch_session_sweep
        with mock.patch.object(dispatch_session_sweep, "overflow_pointer",
                               side_effect=lambda text, *_a, **_k: text):
            text, chosen, rest = carrier.fit([(root, records)], render, 250)
        # The first unit alone is over the budget and no whole copy of it can
        # be saved: it stays pending whole, and the units after it move.
        self.assertEqual([r["delivery_id"] for _root, rs in chosen for r in rs], ["d3", "d4"])
        self.assertEqual(len(text), 200)
        self.assertEqual([r["delivery_id"] for _root, rs in rest for r in rs],
                         ["d0", "d1", "d2", "d5"])
        text, chosen, rest = carrier.fit([(root, records)], render, 450)
        self.assertEqual(len(text), 400)
        self.assertEqual(len([r for _root, rs in chosen for r in rs]), 4)


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
        mock.patch.object(carrier, "queue_endpoint_absent", return_value=False).start()

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


class BudgetAndMixedRootTest(unittest.TestCase):
    """Reviewer [7b] counterexamples on real pending records (truncation, mixed root)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "dispatch"
        self.state.mkdir(mode=0o700)
        self.sid = "00000000-0000-4000-8000-000000001435"
        mock.patch.dict(os.environ, {}, clear=False).start()
        for key in WORKER_ENV:
            os.environ.pop(key, None)
        mock.patch.object(carrier, "_roots", return_value=[self.state]).start()
        mock.patch.object(carrier, "native_receiving_turn", return_value=None).start()
        mock.patch.object(carrier, "watch_receipts", return_value=(None, [])).start()
        self.socketless = mock.patch.object(carrier, "queue_endpoint_absent", return_value=False)
        self.socketless.start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def notify(self, key, text):
        import session_notice
        with mock.patch.object(session_notice, "_retain", return_value=None):
            record = session_notice.notify("codex", self.sid, key=key, subject="report", text=text,
                                           required_action="read-notice", jobs=self.state / "jobs.log")
        self.assertIsNotNone(record)
        return record

    def status(self, record):
        import dispatch_pending_delivery as pending
        return pending.read(self.state, self.sid, record["delivery_id"])["state"]

    def test_only_records_shown_whole_are_acked(self):
        records = [self.notify(f"notice-{n}", f"{n:02d}-TOKEN-{n}-" + "x" * 540) for n in range(20)]
        for hook in (carrier.post_tool, carrier.stop):
            before = {record["delivery_id"] for record in records if self.status(record) == "acked"}
            out = io.StringIO()
            hook({"session_id": self.sid, "turn_id": f"turn-{hook.__name__}", "tool_name": "Bash"}, out)
            response = json.loads(out.getvalue())
            text = response.get("reason") or response["hookSpecificOutput"]["additionalContext"]
            self.assertLessEqual(len(text), carrier.MAX_REASON_CHARS)
            for n, record in enumerate(records):
                if self.status(record) == "acked" and record["delivery_id"] not in before:
                    self.assertIn(f"{n:02d}-TOKEN-{n}-", text)
            if hook is carrier.post_tool:
                # What did not fit whole stays pending for the next boundary.
                left = [record for record in records if self.status(record) != "acked"]
                self.assertTrue(left)
                self.assertTrue(all(self.status(record) == "pending" for record in left))
        self.assertTrue(any(self.status(record) == "acked" for record in records))

    def completion_record(self):
        import dispatch_pending_delivery as pending
        jobs = self.state / "jobs.log"
        jobs.touch()
        attempt, parent = "att-" + "b" * 32, "att-" + "a" * 32
        receipt = {"schema_version": 2, "state": "delivered", "parent_attempt_id": parent,
                   "job_registry": str(jobs), "delivery_classification": "attention",
                   "children": [{"attempt_id": attempt, "status": "done", "readiness": "ready",
                                 "reason": "terminal-failure-or-unclosed",
                                 "required_action": "inspect-done-failure", "harness": "codex",
                                 "delivery_classification": "attention"}]}
        done_id = "delivery-" + "a" * 32
        pending.create(root=self.state, recipient_kind="codex-native-queue", recipient_key=self.sid,
                       delivery_id=done_id, session_generation="", session_generation_supported="0",
                       attempt_ids=[attempt], parent_attempt_id=parent, route_id="rt-fixture",
                       route_node="execute", receipt=receipt,
                       receipt_digest=pending._canonical_receipt_digest(receipt),
                       row_revisions={attempt: "deadbeef"})
        return done_id, attempt

    def test_socketless_completion_reaches_the_running_turn_once_and_stays_pending(self):
        import dispatch_pending_delivery as pending
        self.socketless.stop()
        mock.patch.object(carrier, "queue_endpoint_absent", return_value=True).start()
        done_id, attempt = self.completion_record()
        out = io.StringIO()
        carrier.post_tool({"session_id": self.sid, "turn_id": "turn-a", "tool_name": "Bash"}, out)
        context = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn(attempt, context)
        # Shown, never ACKed: the prompt carrier still shows it at the next prompt.
        self.assertEqual(pending.read(self.state, self.sid, done_id)["state"], "pending")
        # The same turn's Stop asks once for the action, since no tool named the attempt.
        out = io.StringIO()
        carrier.stop({"session_id": self.sid, "turn_id": "turn-a"}, out)
        reason = json.loads(out.getvalue())["reason"]
        self.assertIn(f"attempt={attempt}", reason)
        self.assertNotIn("Arrived before this turn ended", reason)
        # A later turn's tool boundary does not show it again.
        out = io.StringIO()
        carrier.post_tool({"session_id": self.sid, "turn_id": "turn-b", "tool_name": "Bash"}, out)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(pending.read(self.state, self.sid, done_id)["state"], "pending")

    def test_unbound_completion_does_not_hold_back_a_notice(self):
        import dispatch_pending_delivery as pending
        note = self.notify("own-report", "OWN_NOTICE_TOKEN")
        jobs = self.state / "jobs.log"
        jobs.touch()
        attempt, parent = "att-" + "b" * 32, "att-" + "a" * 32
        receipt = {"schema_version": 2, "state": "delivered", "parent_attempt_id": parent,
                   "job_registry": str(jobs), "delivery_classification": "attention",
                   "children": [{"attempt_id": attempt, "status": "done", "readiness": "ready",
                                 "reason": "terminal-failure-or-unclosed",
                                 "required_action": "inspect-done-failure", "harness": "codex",
                                 "delivery_classification": "attention"}]}
        done_id = "delivery-" + "a" * 32
        pending.create(root=self.state, recipient_kind="codex-native-queue", recipient_key=self.sid,
                       delivery_id=done_id, session_generation="", session_generation_supported="0",
                       attempt_ids=[attempt], parent_attempt_id=parent, route_id="rt-fixture",
                       route_node="execute", receipt=receipt,
                       receipt_digest=pending._canonical_receipt_digest(receipt),
                       row_revisions={attempt: "deadbeef"})
        text, batches = carrier.collect(self.sid, "owned-turn", budget_seconds=5.0)
        self.assertIn("OWN_NOTICE_TOKEN", text)
        self.assertEqual([r["delivery_id"] for _root, rs in batches for r in rs], [note["delivery_id"]])
        # The completion is left unclaimed for the native queue and the next prompt.
        self.assertEqual(pending.read(self.state, self.sid, done_id)["state"], "pending")


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
