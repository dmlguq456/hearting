#!/usr/bin/env python3
"""Isolated exact-batch regression for settled completion and late cleanup notices."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_completion_join as join
import dispatch_contract
import dispatch_pending_delivery as pending
import dispatch_session_sweep as sweep
import dispatch_supervision as supervision


class ExactCompletionSequenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.log"
        self.recipient = "codex-isolated-parent"
        self.attempts = ["att-code", "att-ship", "att-spec"]
        parent = "parent-session-" + hashlib.sha256(self.recipient.encode()).hexdigest()
        self.receipt = {
            "schema_version": 2, "state": "ready", "parent_attempt_id": parent,
            "job_registry": str(self.jobs), "delivery_classification": "success",
            "children": [{"attempt_id": attempt, "status": "done", "readiness": "ready",
                "reason": "registry-closed", "required_action": "advance-completed",
                "harness": "codex", "delivery_classification": "success"}
                for attempt in self.attempts],
        }
        digest = pending._canonical_receipt_digest(self.receipt)
        lines = []
        self.delivery_ids = {}
        for index, (attempt, node) in enumerate(zip(self.attempts, ("code", "ship", "spec")), start=1):
            delivery_id = "delivery-sequence-" + str(index)
            self.delivery_ids[attempt] = delivery_id
            revision = "revision-" + str(index)
            metadata = {
                "attempt_schema_version": "2", "dispatch_depth": "1", "transport": "headless",
                "execution_surface": "registered-headless", "registered_worker": "1",
                "launch_claimed": "1", "launch_started": "1", "worker_type": "stage",
                "attempt_id": attempt, "parent_attempt_id": "-", "route_id": "rt-sequence",
                "route_node": node, "route_hash": "sha256:sequence", "harness": "codex",
                "parent_sid": self.recipient, "parent_completion_delivery": "codex-native-queue",
                "delivery_intent": "1", "delivery_id": delivery_id,
                "delivery_receipt_digest": digest, "delivery_row_revision": revision,
                "delivery_recipient_kind": "codex-native-queue", "note": "completed-marker",
                "launch_outcome": "completed",
            }
            lines.append("2026-10-19T00:00:00Z\tdone\t/repo\t/wt\t" + node + "\t"
                         + ",".join(f"{key}={value}" for key, value in metadata.items()))
        self.jobs.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.completion_times = {}
        for index, attempt in enumerate(self.attempts, start=1):
            delivery_id = self.delivery_ids[attempt]
            revision = "revision-" + str(index)
            record = pending.create(self.root, recipient_kind="codex-native-queue",
                recipient_key=self.recipient, delivery_id=delivery_id,
                session_generation="", session_generation_supported="0", attempt_ids=[attempt],
                parent_attempt_id="-", route_id="rt-sequence",
                route_node=("code", "ship", "spec")[index - 1], receipt=self.receipt,
                receipt_digest=digest, row_revisions={attempt: revision})
            pending.claim(self.root, self.recipient, delivery_id,
                          claim_owner="isolated-sequence", lease_seconds=30)
            pending.record_send(self.root, self.recipient, delivery_id,
                                source="codex-prompt-context-rendered", event_id="turn-1")
            received = pending.ack(self.root, self.recipient, delivery_id,
                                   acked_by="codex-native-prompt:" + self.recipient)
            self.completion_times[attempt] = received["timestamp_provenance"]

    def test_late_cleanup_and_join_deadline_do_not_reopen_an_exact_received_batch(self):
        original_jobs = self.jobs.read_bytes()
        chronology = [
            {"stage": "code-original", "verdict": "FAIL", "skipped": ["independent review"]},
            {"stage": "code-correction", "verdict": "PASS", "skipped": []},
            {"stage": "deployment-fixture", "verdict": "PASS", "operating_deployment": "unmeasured"},
            {"stage": "independent-review-fixture", "verdict": "PASS"},
            {"stage": "documentation", "verdict": "complete"},
            {"stage": "cleanup", "state": "unknown"},
            {"stage": "late-join-deadline", "state": "observed"},
            {"stage": "same-completion-redelivery", "state": "duplicate"},
        ]
        history = self.root / "pipeline-history.json"
        history.write_text(json.dumps(chronology, sort_keys=True) + "\n", encoding="utf-8")
        original_history = history.read_bytes()
        recovered = join.recover_acknowledged_batch_outcome(
            self.jobs, set(self.attempts), recipient_sid=self.recipient,
            parent_attempt_id=self.receipt["parent_attempt_id"],
            parent_completion_delivery="codex-native-queue")
        self.assertEqual(recovered, self.receipt)
        notice = supervision.materialize(self.jobs, {self.attempts[0]}, reason="join-deadline")[0]
        delivered_text = sweep._bounded_receipt_text(notice)
        self.assertIn("retains cleanup observation only", delivered_text)
        self.assertIn("Do not restart the goal", delivered_text)
        self.assertIn("ask the user whether to wait or cancel", delivered_text)
        self.assertIn("first observed send=", delivered_text)
        self.assertIn("exact receipt=", delivered_text)
        self.assertIn("notice record materialized", delivered_text)
        self.assertNotIn("run the route's start command", delivered_text)
        self.assertEqual(self.jobs.read_bytes(), original_jobs)

        # The native queue courier must retire the same notice before enqueue,
        # with no gateway/queue RPC at all for an already received exact result.
        spec = importlib.util.spec_from_file_location("native_cleanup_courier",
                                                     ROOT / "utilities/codex-managed-completion.py")
        courier = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = courier
        spec.loader.exec_module(courier)
        native_notice = supervision.materialize(self.jobs, {self.attempts[0]}, reason="process-unverifiable")[0]
        watcher = courier.NoticeWatcher(SimpleNamespace(jobs=self.jobs, parent_session_id=self.recipient,
            queue_socket=Path("/isolated-never-contact.sock"), sealed_batch_id="", interval=0.1),
            {self.attempts[0]}, None)
        with mock.patch.object(courier.codex_queue_delivery, "send_at_least_once") as send:
            watcher._one(pending.record_path(self.root, self.recipient, native_notice["delivery_id"]))
        send.assert_not_called()
        self.assertEqual(pending.read(self.root, self.recipient, native_notice["delivery_id"])["state"], "rejected")
        self.assertEqual(self.jobs.read_bytes(), original_jobs)
        self.assertEqual(json.loads(history.read_text(encoding="utf-8")), chronology)
        self.assertEqual(history.read_bytes(), original_history)

        # The exact receipt is idempotent; a duplicate materialization does not
        # change its first-send/receipt timestamps or create another message.
        original_record = pending.read(self.root, self.recipient,
                                       self.delivery_ids[self.attempts[0]])
        duplicate = pending.create(self.root, recipient_kind="codex-native-queue",
            recipient_key=self.recipient, delivery_id=original_record["delivery_id"],
            session_generation="", session_generation_supported="0", attempt_ids=[self.attempts[0]],
            parent_attempt_id="-", route_id="rt-sequence", route_node="code", receipt=self.receipt,
            receipt_digest=original_record["receipt_digest"],
            row_revisions={self.attempts[0]: "revision-1"})
        self.assertEqual(duplicate["timestamp_provenance"], original_record["timestamp_provenance"])

        # Run the actual shared consumer: rendering/empty queue inspection alone
        # cannot prove that a pending cleanup notice would not wake the parent.
        self.assertFalse(supervision.notice_is_current(notice))
        records, _ = sweep.sweep_deliver(self.root, "codex-native-queue", self.recipient)
        self.assertEqual(records, [])
        retired = pending.read(self.root, self.recipient, notice["delivery_id"])
        self.assertEqual(retired["state"], "rejected")
        self.assertEqual(self.jobs.read_bytes(), original_jobs)

    def test_unacked_child_or_new_terminal_conflict_keeps_recovery_visible(self):
        record = pending.read(self.root, self.recipient, self.delivery_ids[self.attempts[-1]])
        original_read = pending.read

        def partial_read(root, recipient, delivery_id):
            value = original_read(root, recipient, delivery_id)
            if delivery_id == self.delivery_ids[self.attempts[-1]]:
                return dict(value, state="pending")
            return value

        with mock.patch.object(pending, "read", side_effect=partial_read):
            self.assertIsNone(join.recover_acknowledged_batch_outcome(
                self.jobs, set(self.attempts), recipient_sid=self.recipient,
                parent_attempt_id=self.receipt["parent_attempt_id"],
                parent_completion_delivery="codex-native-queue"))
            notice = supervision.materialize(self.jobs, {self.attempts[0]}, reason="join-deadline")[0]
            self.assertIsNone(supervision._cleanup_only_result(notice["receipt"]))
        with mock.patch("dispatch_contract.terminal_conflict_pending", return_value=True):
            self.assertIsNone(join.recover_acknowledged_batch_outcome(
                self.jobs, set(self.attempts), recipient_sid=self.recipient,
                parent_attempt_id=self.receipt["parent_attempt_id"],
                parent_completion_delivery="codex-native-queue"))

    def test_one_acknowledged_batch_does_not_cover_a_different_unreceived_attempt(self):
        metadata = {"attempt_schema_version": "2", "attempt_id": "att-unreceived",
            "dispatch_depth": "1", "worker_type": "stage", "harness": "codex",
            "parent_attempt_id": "-", "parent_sid": self.recipient,
            "parent_completion_delivery": "codex-native-queue", "delivery_id": "delivery-unreceived"}
        with self.jobs.open("a") as stream:
            stream.write("2026-10-19T00:00:00Z\tdone\t/r\t/w\tother\t" +
                         ",".join(f"{k}={v}" for k,v in metadata.items()) + "\n")
        receipt = {"job_registry": str(self.jobs), "recipient_thread_id": self.recipient,
                   "monitored_attempt_ids": [self.attempts[0], "att-unreceived"], "reason": "join-deadline"}
        self.assertIsNone(supervision._cleanup_only_result(receipt))

    def test_legacy_delivered_peer_ack_recovers_only_with_exact_durable_peer_binding(self):
        # Shape observed read-only in the reported operating duty: the old body
        # omitted jobs/parent, while the pending identity held their scope.
        import peer_obligations as obligations
        legacy_root = self.root / "legacy"
        legacy_root.mkdir()
        jobs = legacy_root / "jobs.log"
        aid, sid, did, ref = "att-legacy", "codex-legacy-parent", "delivery-legacy", "a" * 32
        receipt = {"schema_version": 2, "state": "delivered", "job_registry": "",
            "parent_attempt_id": "", "delivery_classification": "success", "children": [{
                "attempt_id": aid, "status": "done", "readiness": "ready", "harness": "claude",
                "required_action": "advance-completed", "delivery_classification": "success"}]}
        digest = pending._canonical_receipt_digest(receipt)
        metadata = {"attempt_schema_version": "2", "attempt_id": aid, "dispatch_depth": "1",
            "worker_type": "owner", "parent_sid": sid, "parent_attempt_id": "-",
            "parent_completion_delivery": "codex-native-queue", "delivery_intent": "1",
            "delivery_id": did, "delivery_receipt_digest": digest, "delivery_row_revision": "legacy-rev",
            "route_id": "rt-legacy", "route_node": "_owner", "note": "completed-supervisor"}
        jobs.write_text("t\tdone\t/r\t/w\towner\t" + ",".join(f"{k}={v}" for k,v in metadata.items()) + "\n")
        record = pending.create(legacy_root, recipient_kind="codex-native-queue", recipient_key=sid,
            delivery_id=did, session_generation="", session_generation_supported="0", attempt_ids=[aid],
            parent_attempt_id="-", route_id="rt-legacy", route_node="_owner", receipt=receipt,
            receipt_digest=digest, row_revisions={aid: "legacy-rev"})
        pending.claim(legacy_root, sid, did, claim_owner="fixture", lease_seconds=30)
        record = pending.ack(legacy_root, sid, did, acked_by="peer-received:" + ref)
        store = obligations.ObligationStore(legacy_root)
        duty = store.create("registered-batch-legacy", "registered-batch", {"jobs": str(jobs),
            "attempt_id": aid, "session_id": sid, "harness": "codex", "server": "default"},
            {"carrier": "codex-native-queue"})
        store.update(duty["id"], state="complete", delivery="acknowledged")
        to = {"harness": "codex", "session_id": sid, "name": None}
        sender = {"harness": "claude", "session_id": "fixture-source-parent", "name": None}
        message = {"schema_version": 1, "ref": ref, "state": "received", "receipt": "exact-peer-ref",
            "created": 1.0, "from": sender, "to": to, "refs": [duty["id"]], "body_sha256": "b" * 64,
            "body_ref": None, "dispatch_notice": {"duty_id": duty["id"], "jobs": str(jobs),
                "attempt_id": aid, "carrier": "codex-native-queue", "recipient_sid": sid,
                "storage_recipient": sid, "delivery_id": did,
                "claim_owner": "retained-rewake:" + duty["id"]}}
        for bucket, body in (("pending", message), ("transfers", {"schema_version": 1, "message_id": ref,
                "from": sender, "to": to, "body_sha256": "b" * 64, "body_ref": None})):
            directory = legacy_root / "peer-messages" / bucket
            directory.mkdir(mode=0o700, parents=True)
            path = directory / (ref + ".json")
            path.write_text(json.dumps(body)); path.chmod(0o600)
        before = pending.record_path(legacy_root, sid, did).read_bytes()
        recovered = join.recover_acknowledged_batch_outcome(jobs, {aid}, recipient_sid=sid,
            parent_attempt_id="parent-session-legacy", parent_completion_delivery="codex-native-queue")
        self.assertIsNotNone(recovered)
        self.assertEqual((recovered["state"], recovered["job_registry"], recovered["parent_attempt_id"]),
                         ("ready", str(jobs), "parent-session-legacy"))
        self.assertEqual(recovered["children"], receipt["children"])
        self.assertEqual(pending.record_path(legacy_root, sid, did).read_bytes(), before)
        # Historical verification is read-only even when current lineage is
        # unavailable. It cannot authorize another send or manufacture an ACK.
        from unittest import mock
        with mock.patch("dispatch_seat_handover._effective", side_effect=OSError("current lineage unavailable")), \
                mock.patch.object(pending, "ack_received") as ack:
            self.assertTrue(obligations.received_registered_ack(jobs, record, sid))
            self.assertEqual(list(obligations._registered_delivery_bindings(
                message, roots=[legacy_root], require_current_recipient=True)), [])
            ack.assert_not_called()
        peer_path = legacy_root / "peer-messages/pending" / (ref + ".json")
        for change in ({"receipt": "unverified"}, {"dispatch_notice": {
                **message["dispatch_notice"], "delivery_id": "delivery-other"}}):
            peer_path.write_text(json.dumps({**message, **change}))
            self.assertIsNone(join.recover_acknowledged_batch_outcome(jobs, {aid}, recipient_sid=sid,
                parent_attempt_id="parent-session-legacy", parent_completion_delivery="codex-native-queue"))
        peer_path.unlink()  # A peer-received prefix without its evidence proves nothing.
        self.assertIsNone(join.recover_acknowledged_batch_outcome(jobs, {aid}, recipient_sid=sid,
            parent_attempt_id="parent-session-legacy", parent_completion_delivery="codex-native-queue"))


if __name__ == "__main__":
    unittest.main()
