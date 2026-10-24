#!/usr/bin/env python3
"""Focused tests for native receipt identity and exact settlement."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dispatch_pending_delivery as pending
import dispatch_recipient_receipt as receipt


class RecipientReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="recipient-receipt-")
        self.root = Path(self.tmp.name) / "dispatch"
        self.storage = "session-original"
        self.session = "session-recipient"
        self.delivery_id = "delivery-" + "a" * 32
        body = {"kind": "supervision", "owner_attempt_id": "att-" + "b" * 28,
                "reason": "waiting", "target_attempts": ["att-" + "c" * 28]}
        self.record = pending.create(
            self.root, recipient_kind="claude-parent-runtime", recipient_key=self.storage,
            delivery_id=self.delivery_id, session_generation="g1",
            session_generation_supported="1", attempt_ids=["att-" + "c" * 28],
            parent_attempt_id="att-" + "b" * 28, route_id="route-test", route_node="execute",
            receipt=body, receipt_digest=pending._canonical_receipt_digest(body),
            row_revisions={"att-" + "c" * 28: "rev-1"})

    def tearDown(self):
        self.tmp.cleanup()

    def _mark_sent_ambiguous(self):
        pending.claim(self.root, self.storage, self.delivery_id,
                      claim_owner="test-carrier", lease_seconds=30,
                      require_generation_proof=False)
        pending.mark_sent_ambiguous(self.root, self.storage, self.delivery_id,
                                    claim_owner="test-carrier")
        self.record = pending.read(self.root, self.storage, self.delivery_id)

    def test_binding_is_exact_to_immutable_identity(self):
        marker = receipt.receipt_marker(self.record, self.storage)
        self.assertEqual(marker, receipt.receipt_marker(self.record, self.storage))
        changed = dict(self.record, route_node="other")
        self.assertNotEqual(marker, receipt.receipt_marker(changed, self.storage))

    def test_opencode_receipts_require_the_exact_expected_carrier_body(self):
        marker = receipt.receipt_marker(self.record, self.storage)
        body = "Hearting delivery context\n" + marker
        expected = [{"text": body, "bindings": [
            (self.delivery_id, receipt.binding_digest(self.record, self.storage))]}]
        messages = [
            {"info": {"sessionID": self.session, "role": "assistant"},
             "parts": [{"type": "text", "text": body}]},
            {"info": {"sessionID": "foreign", "role": "user"},
             "parts": [{"type": "text", "text": body}]},
            {"info": {"sessionID": self.session, "role": "user"},
             "parts": [{"type": "text", "text": "I received this marker: " + marker}]},
            {"info": {"sessionID": self.session, "role": "user"},
             "parts": [{"type": "text", "text": "prefix\n" + body}]},
            {"info": {"sessionID": self.session, "role": "user"},
             "parts": [{"type": "text", "text": body[:-8]}]},
            {"info": {"sessionID": self.session, "role": "user"},
             "parts": [{"type": "text", "text": body}]},
        ]
        evidence = receipt.opencode_messages_evidence(messages, self.session, expected)
        self.assertEqual(evidence["bindings"], [(self.delivery_id, marker.split()[-1])])
        self.assertEqual(evidence["source"], "opencode-session-messages")
        peer_shape = receipt.opencode_messages_evidence([{
            "info": {"sessionID": self.session, "role": "user"},
            "message": {"content": body},
        }], self.session, expected)
        self.assertEqual(peer_shape["bindings"], evidence["bindings"])
        claude = receipt.claude_feedback_evidence([
            {"type": "assistant", "sessionId": self.session,
             "summary": "Stop hook feedback", "message": {"content": marker}},
            {"type": "user", "sessionId": "foreign",
             "summary": "Stop hook feedback", "message": {"content": marker}},
            {"type": "user", "sessionId": self.session, "summary": "user prompt",
             "message": {"content": marker}},
            {"type": "user", "sessionId": self.session,
             "summary": "Stop hook feedback", "message": {"content": marker}},
        ], self.session)
        self.assertEqual(claude["bindings"], [(self.delivery_id, marker.split()[-1])])

    def test_claude_21306_native_task_notification_is_session_bound(self):
        marker = receipt.receipt_marker(self.record, self.storage)
        native = {
            "type": "user", "sessionId": self.session,
            "origin": {"kind": "task-notification", "producer": "session-task"},
            "promptSource": "system", "turnOrigin": "task_notification",
            "message": {"content": (
                "<task-notification><task-id>fixture</task-id>"
                "<summary>Stop hook feedback</summary>" + marker +
                "</task-notification>")},
        }
        evidence = receipt.claude_feedback_evidence([native], self.session)
        self.assertEqual(evidence["bindings"], [(self.delivery_id, marker.split()[-1])])
        for invalid in (
            {**native, "sessionId": "foreign"},
            {**native, "promptSource": "user"},
            {**native, "message": {"content": "quoted " + marker}},
            {**native, "message": {"content": "<task-notification>partial" + marker}},
        ):
            observed = receipt.claude_feedback_evidence([invalid], self.session)
            self.assertFalse(observed["bindings"])

    def test_exact_receipt_settles_delivery_only(self):
        self._mark_sent_ambiguous()
        marker = receipt.receipt_marker(self.record, self.storage)
        body = "Hearting delivery context\n" + marker
        evidence = receipt.opencode_messages_evidence([
            {"info": {"sessionID": self.session, "role": "user"},
             "parts": [{"type": "text", "text": body}]},
        ], self.session, [{"text": body, "bindings": [
            (self.delivery_id, receipt.binding_digest(self.record, self.storage))]}])
        result = receipt.settle_native_receipt(
            self.root, self.storage, "opencode", self.session, self.record,
            evidence, acked_by="opencode-session:" + self.session)
        self.assertEqual(result, {"received": True, "settled": True,
                                  "reason": "native-receipt-settled"})
        self.assertEqual(pending.read(self.root, self.storage, self.delivery_id)["state"], "acked")
        # Settlement is only a pending-delivery transition. The receipt schema
        # and its unresolved owner state are untouched.
        current = pending.read(self.root, self.storage, self.delivery_id)
        self.assertEqual(current["receipt"]["reason"], "waiting")
        self.assertEqual(current["receipt"]["owner_attempt_id"], self.record["receipt"]["owner_attempt_id"])

    def test_ack_write_failure_still_reports_received_and_does_not_resend(self):
        self._mark_sent_ambiguous()
        marker = receipt.receipt_marker(self.record, self.storage)
        body = "Hearting delivery context\n" + marker
        evidence = receipt.opencode_messages_evidence([
            {"info": {"sessionID": self.session, "role": "user"},
             "parts": [{"type": "text", "text": body}]},
        ], self.session, [{"text": body, "bindings": [
            (self.delivery_id, receipt.binding_digest(self.record, self.storage))]}])
        with mock.patch.object(pending, "ack_received", side_effect=OSError("simulated write failure")):
            first = receipt.settle_native_receipt(
                self.root, self.storage, "opencode", self.session, self.record,
                evidence, acked_by="opencode-session:" + self.session)
        self.assertTrue(first["received"])
        self.assertFalse(first["settled"])
        self.assertEqual(first["reason"], "native-receipt-ack-write-failed")
        self.assertEqual(pending.read(self.root, self.storage, self.delivery_id)["state"],
                         "sent-ambiguous")
        second = receipt.settle_native_receipt(
            self.root, self.storage, "opencode", self.session, self.record,
            evidence, acked_by="opencode-session:" + self.session)
        self.assertTrue(second["received"])
        self.assertTrue(second["settled"])

    def test_foreign_or_stale_identity_cannot_suppress_delivery(self):
        marker = receipt.receipt_marker(self.record, self.storage)
        body = "Hearting delivery context\n" + marker
        foreign = receipt.opencode_messages_evidence([
            {"info": {"sessionID": "other", "role": "user"},
             "parts": [{"type": "text", "text": body}]},
        ], self.session, [{"text": body, "bindings": [
            (self.delivery_id, receipt.binding_digest(self.record, self.storage))]}])
        result = receipt.settle_native_receipt(
            self.root, self.storage, "opencode", self.session, self.record,
            foreign, acked_by="test")
        self.assertFalse(result["received"])
        mismatched = {**self.record, "attempt_ids": ["att-" + "d" * 28]}
        stale = receipt.settle_native_receipt(
            self.root, self.storage, "opencode", self.session, mismatched,
            receipt.opencode_messages_evidence([
                {"info": {"sessionID": self.session, "role": "user"},
                 "parts": [{"type": "text", "text": body}]},
            ], self.session, [{"text": body, "bindings": [
                (self.delivery_id, receipt.binding_digest(self.record, self.storage))]}]), acked_by="test")
        self.assertFalse(stale["received"])
        self.assertEqual(pending.read(self.root, self.storage, self.delivery_id)["state"], "pending")

    def test_markerless_supervision_feedback_requires_a_unique_rendered_receipt(self):
        import dispatch_session_sweep as sweep
        owner = "att-" + "a" * 28
        monitored = "att-" + "b" * 28
        did = "delivery-" + "e" * 32
        body = {"schema_version": 1, "kind": "supervision", "state": "attention",
                "owner_attempt_id": owner, "monitored_attempt_ids": [monitored],
                "recipient_thread_id": self.storage, "sealed_batch_id": "batch-legacy",
                "job_registry": "/tmp/legacy-jobs.log", "reason": "join-deadline",
                "responsible": "supervision-controller", "required_action": "inspect-recovery",
                "pending_delivery_id": did, "obligation_revision": "revision-1"}
        record = pending.create(
            self.root, recipient_kind="claude-parent-runtime", recipient_key=self.storage,
            delivery_id=did, session_generation="", session_generation_supported="0",
            attempt_ids=[owner], parent_attempt_id=owner, route_id="route-legacy",
            route_node="execute", receipt=body,
            receipt_digest=pending._canonical_receipt_digest(body),
            row_revisions={owner: "row-revision"})
        pending.claim(self.root, self.storage, did, claim_owner="async-rewake",
                      lease_seconds=30, require_generation_proof=False)
        pending.mark_sent_ambiguous(self.root, self.storage, did, claim_owner="async-rewake")
        record = {**pending.read(self.root, self.storage, did),
                  "_storage_key": self.storage}
        text = sweep._bounded_receipt_text(record)
        evidence = receipt.claude_feedback_evidence([{
            "type": "user", "sessionId": self.storage,
            "summary": "Stop hook feedback", "message": {"content": text},
        }], self.storage)
        unique = receipt.legacy_claude_receipt_bindings(
            self.root, "claude-parent-runtime", self.storage, [record], evidence)
        binding = unique[(self.storage, did)]
        duplicate = {**record, "delivery_id": "delivery-" + "f" * 32}
        ambiguous = receipt.legacy_claude_receipt_bindings(
            self.root, "claude-parent-runtime", self.storage,
            [record, duplicate], evidence)
        self.assertEqual(ambiguous, {})
        exact = {**evidence, "bindings": [binding]}
        result = receipt.settle_native_receipt(
            self.root, self.storage, "claude", self.storage, record, exact,
            acked_by="native-receipt:claude:" + self.storage)
        self.assertTrue(result["received"])
        self.assertTrue(result["settled"])

    def test_markerless_completion_feedback_binds_the_full_current_receipt(self):
        import dispatch_completion_join as join
        from types import SimpleNamespace

        jobs = self.root / "jobs.log"
        self.root.mkdir(parents=True, exist_ok=True)
        jobs.touch()
        attempt = "att-legacy-completion"
        revision_text = "registry-row-legacy-completion"
        revision = __import__("hashlib").sha256(revision_text.encode()).hexdigest()
        receipt_body = {
            "schema_version": 2, "state": "delivered", "parent_attempt_id": "att-parent",
            "job_registry": str(jobs), "delivery_classification": "success",
            "children": [{"attempt_id": attempt, "status": "done",
                          "required_action": "advance-completed",
                          "delivery_classification": "success"}],
        }
        digest = pending._canonical_receipt_digest(receipt_body)
        did = "delivery-" + "f" * 32
        record = pending.create(
            self.root, recipient_kind="claude-parent-runtime", recipient_key=self.storage,
            delivery_id=did, session_generation="", session_generation_supported="0",
            attempt_ids=[attempt], parent_attempt_id="att-parent", route_id="route-legacy",
            route_node="execute", receipt=receipt_body, receipt_digest=digest,
            row_revisions={attempt: revision})
        pending.claim(self.root, self.storage, did, claim_owner="async-rewake",
                      lease_seconds=30, require_generation_proof=False)
        pending.mark_sent_ambiguous(self.root, self.storage, did, claim_owner="async-rewake")
        record = pending.read(self.root, self.storage, did)
        row = SimpleNamespace(status="done", raw=revision_text, metadata={
            "parent_sid": self.storage, "parent_harness": "claude",
            "parent_completion_delivery": "claude-parent-runtime", "parent_attempt_id": "att-parent",
            "dispatch_depth": "2", "worker_type": "stage", "route_id": "route-legacy",
            "route_node": "execute", "delivery_intent": "1", "delivery_id": did,
            "delivery_receipt_digest": digest, "delivery_row_revision": revision,
            "delivery_receipt_b64": join.seal_delivery_receipt(receipt_body),
        })
        text = ("Hearting dispatch completed. Runtime owner completion receipt schema=2 "
                f"state=success attempt_id={attempt} armed=stdout status=done "
                f"row_revision={revision} row_digest={revision} marker_current=1 "
                "quiescent=1 owned_children=0 advanced=0 reason=terminal-complete "
                "required_action=advance-completed. No harvest command is required.")
        evidence = receipt.claude_feedback_evidence([{
            "type": "user", "sessionId": self.session,
            "summary": "Stop hook feedback", "message": {"content": text},
        }], self.session)
        with mock.patch("dispatch_completion_join.current_attempt_row", return_value=row), \
             mock.patch("dispatch_seat_handover.effective_parent", return_value=self.session), \
             mock.patch("dispatch_seat_handover.effective_parent_harness", return_value="claude"):
            bindings = receipt.legacy_claude_receipt_bindings(
                self.root, "claude-parent-runtime", self.session,
                [{**record, "_storage_key": self.storage}], evidence)
        self.assertIn((self.storage, did), bindings)
        exact = {**evidence, "bindings": [bindings[(self.storage, did)] ]}
        result = receipt.settle_native_receipt(
            self.root, self.storage, "claude", self.session, record, exact,
            acked_by="native-receipt:claude:" + self.session)
        self.assertTrue(result["received"])
        self.assertTrue(result["settled"])


if __name__ == "__main__":
    unittest.main()
