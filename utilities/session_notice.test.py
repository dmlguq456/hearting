#!/usr/bin/env python3
"""A notice about non-attempt work reaches its session through the shared store and renderer."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import sys
import json
import hashlib
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from unittest import mock
from concurrent.futures import ThreadPoolExecutor

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import dispatch_session_sweep as sweep  # noqa: E402
import session_notice as N  # noqa: E402


class SessionNoticeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.jobs = Path(self._tmp.name) / "jobs.log"
        self.jobs.write_text("")

    def notify(self, harness="claude", key="run:host-1"):
        with mock.patch("peer_obligations.ensure_normal_runner", return_value=False), \
             mock.patch("peer_obligations.request_dispatch_reconciliation", return_value=False):
            return N.notify(harness, "sid-1", key=key, subject="compute run host-1",
                            text="finished with exit 0; read it with compute-hosts tail host-1",
                            jobs=self.jobs)

    def test_one_notice_is_delivered_once_and_rendered_in_its_own_block(self):
        first = self.notify()
        self.assertEqual(first["recipient_kind"], "claude-parent-runtime")
        self.assertEqual(self.notify()["delivery_id"], first["delivery_id"])  # same key, same record
        claimed, _count = sweep.sweep_deliver(self.jobs.parent, "claude-parent-runtime", "sid-1")
        self.assertEqual([record["delivery_id"] for record in claimed], [first["delivery_id"]])
        text = sweep.delivery_context([(self.jobs.parent, claimed)])
        self.assertTrue(text.startswith(sweep.NOTICE_DELIVERY_HEADER))
        self.assertIn("compute run host-1: finished with exit 0", text)
        self.assertNotIn(sweep.COMPLETION_DELIVERY_HEADER, text)
        self.assertEqual(sweep.ack_delivered(self.jobs.parent, "sid-1", claimed, acked_by="test"), 1)
        self.assertEqual(sweep.sweep_deliver(self.jobs.parent, "claude-parent-runtime", "sid-1")[0], [])

    def test_each_harness_uses_its_declared_carrier(self):
        self.assertEqual(self.notify("codex", "k-codex")["recipient_kind"], "codex-native-queue")
        self.assertEqual(self.notify("opencode", "k-oc")["recipient_kind"], "opencode-turn")
        self.assertIsNone(self.notify("other", "k-other"))
        self.assertIsNone(N.notify("claude", "", key="k", subject="s", text="t", jobs=self.jobs))

    def test_writer_retains_idempotent_notice_duty_and_pending_survives_runner_failure(self):
        import peer_obligations
        env = {"HOME": str(self.jobs.parent / "home"),
               "XDG_STATE_HOME": str(self.jobs.parent / "state"),
               "AGENT_DISPATCH_JOBS": str(self.jobs)}
        with mock.patch.dict("os.environ", env), \
             mock.patch.object(peer_obligations, "ensure_normal_runner", return_value=False) as ensure, \
             mock.patch.object(peer_obligations, "request_dispatch_reconciliation", return_value=False):
            first = N.notify("claude", "sid-1", key="writer-idempotent", subject="compute run",
                             text="finished", jobs=self.jobs)
            second = N.notify("claude", "sid-1", key="writer-idempotent", subject="compute run",
                              text="finished", jobs=self.jobs)
            duties = peer_obligations.ObligationStore(self.jobs.parent).list(
                kinds={"run-notice"})
        self.assertEqual(first["delivery_id"], second["delivery_id"])
        self.assertEqual(len(duties), 1)
        identity = duties[0]["intent"]["identity"]
        self.assertEqual(identity["storage_recipient"], "sid-1")
        self.assertEqual(identity["recipient_harness"], "claude")
        self.assertEqual(duties[0]["intent"]["delivery_id"], first["delivery_id"])
        self.assertEqual(duties[0]["intent"]["receipt_digest"], first["receipt_digest"])
        self.assertNotIn("recipient_sid", identity)
        self.assertEqual(ensure.call_count, 2)

    def test_runner_failure_does_not_remove_the_pending_notice(self):
        import dispatch_pending_delivery as pending
        import peer_obligations
        env = {"HOME": str(self.jobs.parent / "home"),
               "XDG_STATE_HOME": str(self.jobs.parent / "state"),
               "AGENT_DISPATCH_JOBS": str(self.jobs)}
        with mock.patch.dict("os.environ", env), \
             mock.patch.object(peer_obligations, "ensure_normal_runner",
                               side_effect=OSError("runner unavailable")) as ensure:
            receipt = {"schema_version": 1, "kind": "notice", "key": "run:durable",
                       "subject": "compute run", "text": "finished", "required_action": "read-notice"}
            with self.assertRaisesRegex(OSError, "runner unavailable"):
                N.notify("claude", "sid-1", key=receipt["key"], subject=receipt["subject"],
                         text=receipt["text"], jobs=self.jobs)
            stored = pending.read(self.jobs.parent, "sid-1", N.gateway_delivery_id(receipt))
            duties = peer_obligations.ObligationStore(self.jobs.parent).list(kinds={"run-notice"})
        self.assertEqual(ensure.call_count, 1)
        self.assertEqual(len(duties), 1)
        self.assertEqual(stored["state"], "pending")
        self.assertEqual(stored["receipt_digest"], N.receipt_digest(receipt))

    def test_a_malformed_notice_is_never_shown(self):
        with self.assertRaises(ValueError):
            N.validate({"kind": "notice"})
        self.assertEqual(N.render_text({"kind": "notice"}), "notice=unreadable")

    def test_notice_uses_no_registered_attempt_and_retains_one_exact_duty(self):
        from peer_obligations import ObligationStore
        notice = self.notify()
        self.assertEqual(notice["attempt_ids"], [])
        self.assertEqual(notice["parent_attempt_id"], "")
        self.assertEqual(notice["row_revisions"], {})
        self.assertEqual(self.notify()["delivery_id"], notice["delivery_id"])
        duties = ObligationStore(self.jobs.parent).list(kinds={"run-notice"})
        self.assertEqual(len(duties), 1)
        self.assertEqual(duties[0]["intent"]["delivery_id"], notice["delivery_id"])
        self.assertEqual(duties[0]["intent"]["identity"]["lineage"], {
            "kind": "provenance", "harness": "claude", "session_id": "sid-1"})

    def test_concurrent_watchers_converge_on_one_pending_key_digest_and_duty(self):
        from peer_obligations import ObligationStore
        with mock.patch("peer_obligations.ensure_normal_runner", return_value=False), \
                mock.patch("peer_obligations.request_dispatch_reconciliation", return_value=False):
            with ThreadPoolExecutor(max_workers=6) as pool:
                records = list(pool.map(lambda _i: N.notify(
                    "codex", "sid-1", key="attention:run-2:nan", subject="attention",
                    text="run=run-2; NaN observed", jobs=self.jobs), range(12)))
        self.assertEqual(len({row["delivery_id"] for row in records}), 1)
        self.assertEqual(len({row["receipt_digest"] for row in records}), 1)
        self.assertEqual(len({row["recipient_digest"] for row in records}), 1)
        duties = ObligationStore(self.jobs.parent).list(kinds={"run-notice"})
        self.assertEqual(len(duties), 1)


class MessageNoticeBoundaryTests(unittest.TestCase):
    def test_context_sweep_cannot_claim_or_ack_the_peer_couriers_message_answer(self):
        from fixtures.message_questions import MessageQuestionFixture
        with MessageQuestionFixture() as fixture:
            for harness in ("claude", "codex", "opencode"):
                signal, envelope = fixture.ask(harness)
                fixture.submit(envelope)
                record = fixture.record(signal)
                claimed, _ = sweep.sweep_deliver(fixture.root, record["recipient_kind"], signal["session_id"])
                self.assertEqual(claimed, [])
                self.assertEqual(fixture.record(signal)["state"], "pending")

    def test_receipt_serialization_keeps_the_existing_2048_byte_limit(self):
        import dispatch_pending_delivery as pending
        receipt = {"schema_version": 1, "kind": "notice", "subject": "s", "text": "t",
                   "required_action": "read-notice", "key": "k"}
        base = len(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode())
        with tempfile.TemporaryDirectory() as directory:
            for size in (2047, 2048, 2049):
                candidate = {**receipt, "subject": "s" * (size - base + 1)}
                raw = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()
                self.assertEqual(len(raw), size)
                if size <= 2048:
                    self.assertEqual(N.validate(candidate), candidate)
                    pending.create(Path(directory), recipient_kind="claude-parent-runtime", recipient_key="sid",
                        delivery_id="delivery-" + hashlib.sha256(str(size).encode()).hexdigest(), session_generation="",
                        session_generation_supported="0", attempt_ids=[], parent_attempt_id="",
                        route_id="route-free", route_node="notice", receipt=candidate,
                        receipt_digest=N.receipt_digest(candidate), row_revisions={})
                else:
                    with self.assertRaisesRegex(ValueError, "too-large"):
                        N.validate(candidate)
                    with self.assertRaises(pending.PendingDeliveryError):
                        pending.create(Path(directory), recipient_kind="claude-parent-runtime", recipient_key="sid",
                            delivery_id="delivery-" + hashlib.sha256(str(size).encode()).hexdigest(), session_generation="",
                            session_generation_supported="0", attempt_ids=[], parent_attempt_id="",
                            route_id="route-free", route_node="notice", receipt=candidate,
                            receipt_digest=N.receipt_digest(candidate), row_revisions={})

    def test_question_key_alone_keeps_ordinary_notice_semantics(self):
        from fixtures.message_questions import MessageQuestionFixture
        with MessageQuestionFixture() as fixture:
            signal, _ = fixture.ask()
            record = N.notify("claude", signal["session_id"], key="question-answer:pretend",
                              subject="ordinary", text="  ordinary\n  text  ", jobs=fixture.jobs)
            self.assertEqual(record["receipt"]["text"], "ordinary text")
            self.assertNotIn("answer_binding", record["receipt"])
            self.assertEqual(N.consumer_text(record["receipt"]), N.render_text(record["receipt"]))
            import human_wait_state
            self.assertEqual(human_wait_state.read_signals()[0]["id"], signal["id"])


if __name__ == "__main__":
    unittest.main()
