#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dispatch_attempt_ready", ROOT / "utilities" / "dispatch-attempt-ready.py"
)
READY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(READY)
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_contract as D  # noqa: E402


def sealed_cancellation_metadata(attempt: str) -> dict[str, str]:
    observer = os.readlink("/proc/self/ns/pid")
    return {
        "attempt_schema_version": "2",
        "dispatch_depth": "2",
        "transport": "headless",
        "execution_surface": "registered-headless",
        "registered_worker": "1",
        "fallback_hop": "same-harness-headless",
        "attempt_id": attempt,
        "pid_scope": "namespace-local",
        "pid": "99999996", "pid_start": "1", "pgid": "99999996",
        "pid_observer_ns": observer, "pid_ns": observer,
        "cancellation_quiescence_receipt": D.ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT,
        "cancellation_receipt_digest": "sha256:" + "e" * 64,
        "quiescence_pgid_proof": D.GROUP_REAP_PROOF,
        "quiescence_descendant_proof": D.ATTEMPT_DESCENDANT_PROOF,
        "failure_class": "cancelled",
        "note": "cancelled-receipt-unavailable",
    }


class DispatchAttemptReadyTest(unittest.TestCase):
    def classify(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.log"
            jobs.write_text("".join("\t".join([*fields[:5], ",".join(k + "=" + v for k, v in meta.items())]) + "\n"
                                    for fields, meta in rows))
            before = jobs.read_bytes()
            result = READY.classify_selection(jobs, rows)
            self.assertEqual(jobs.read_bytes(), before)
            return result

    def test_supervised_owner_pass_is_ready_without_a_stage_marker(self):
        fields = [
            "2026-08-11T00:00:00Z", "done", "/r", "/w", "owner", "",
        ]
        metadata = {
            "attempt_schema_version": "2",
            "dispatch_depth": "1",
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": "1",
            "fallback_hop": "same-harness-headless",
            "attempt_id": "att-ready-owner-pass",
            "worker_type": "owner",
            "note": "completed-supervisor",
            "failure_class": "pass",
            "pid": "999999",
            "pid_start": "1",
            "pgid": "999999",
            "pid_observer_ns": "pid:[test]",
            "pid_ns": "pid:[test]",
            "launch_lifecycle": "detached",
            "launch_outcome": "governed-process-group-drained",
            "group_reap_proof": "pgid-empty-v1",
            "group_reap_pgid": "999999",
            "attempt_descendant_proof": "attempt-tagged-empty-v1",
            "attempt_descendant_observer_ns": "pid:[test]",
        }
        receipt = self.classify([(fields, metadata)])
        self.assertEqual(receipt["state"], "ready")
        self.assertEqual(receipt["children"][0]["readiness"], "ready")

    def _slice_metadata(self, note, **overrides):
        metadata = {
            "attempt_schema_version": "2",
            "dispatch_depth": "2",
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": "1",
            "fallback_hop": "same-harness-headless",
            "attempt_id": "att-ready-slice",
            "subsession_id": "ss-fixture",
            "stage_authority": "0",
            "session_chain_id": "ssc-fixture",
            "subsession_index": "1",
            "subsession_count": "3",
            "subsession_mode": "serial",
            "subsession_purpose": "planned",
            "completion_delivery": "one-shot",
            "note": note,
            "failure_class": "pass",
            "pid": "999998",
            "pid_start": "1",
            "pgid": "999998",
            "pid_observer_ns": "pid:[test]",
            "pid_ns": "pid:[test]",
            "launch_lifecycle": "detached",
            "launch_outcome": "governed-process-group-drained",
            "group_reap_proof": "pgid-empty-v1",
            "group_reap_pgid": "999998",
            "attempt_descendant_proof": "attempt-tagged-empty-v1",
            "attempt_descendant_observer_ns": "pid:[test]",
        }
        metadata.update({"classifier_source": D.SUBSESSION_TERMINAL_CLASSIFIER, **overrides})
        return ["2026-09-06T00:00:00Z", "done", "/r", "/w", "slice", ""], metadata

    def test_subsession_terminal_is_ready(self):
        # SD-130. This readiness arm existed before the note did -- it required
        # `completed-supervisor`, which a slice can never carry (one-shot
        # delivery, so no supervisor). It was a reader waiting for a writer that
        # did not exist, and this is the first test that reaches it.
        fields, metadata = self._slice_metadata("completed-subsession")
        receipt = self.classify([(fields, metadata)])
        self.assertEqual(receipt["state"], "ready")
        self.assertEqual(receipt["children"][0]["readiness"], "ready")

    def test_subsession_terminal_without_a_pass_verdict_is_not_ready(self):
        fields, metadata = self._slice_metadata(
            "completed-subsession", failure_class="contract"
        )
        receipt = self.classify([(fields, metadata)])
        self.assertNotEqual(receipt["children"][0]["readiness"], "ready")

    def test_subsession_note_on_a_stage_owner_row_is_not_ready(self):
        # The note only means "ready" for a row that actually is a slice; a
        # forged note on a stage-authoritative row must not buy readiness.
        fields, metadata = self._slice_metadata("completed-subsession")
        for key in ("subsession_id", "stage_authority", "session_chain_id",
                    "subsession_index", "subsession_count", "subsession_mode",
                    "subsession_purpose"):
            metadata.pop(key, None)
        receipt = self.classify([(fields, metadata)])
        self.assertNotEqual(receipt["children"][0]["readiness"], "ready")

    def test_slice_carrying_the_supervisor_note_is_not_ready(self):
        # The arm this replaced. A slice cannot be closed by a supervisor, so a
        # row claiming both is malformed and buys nothing.
        fields, metadata = self._slice_metadata("completed-supervisor")
        receipt = self.classify([(fields, metadata)])
        self.assertNotEqual(receipt["children"][0]["readiness"], "ready")

    def test_supervised_stage_pass_still_requires_a_completion_marker(self):
        fields = [
            "2026-08-11T00:00:00Z", "done", "/r", "/w", "stage", "",
        ]
        metadata = {
            "attempt_schema_version": "2",
            "dispatch_depth": "2",
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": "1",
            "fallback_hop": "same-harness-headless",
            "attempt_id": "att-ready-stage-pass",
            "worker_type": "review",
            "note": "completed-supervisor",
            "failure_class": "pass",
            "pid": "999999",
            "pid_start": "1",
            "pgid": "999999",
            "pid_observer_ns": "pid:[test]",
            "pid_ns": "pid:[test]",
            "launch_lifecycle": "detached",
            "launch_outcome": "governed-process-group-drained",
            "group_reap_proof": "pgid-empty-v1",
            "group_reap_pgid": "999999",
            "attempt_descendant_proof": "attempt-tagged-empty-v1",
            "attempt_descendant_observer_ns": "pid:[test]",
        }
        receipt = self.classify([(fields, metadata)])
        self.assertEqual(receipt["state"], "terminal")
        self.assertEqual(
            receipt["children"][0]["readiness"], "terminal-failure"
        )

    def test_open_quiescent_attempt_waits_for_terminal_commit(self):
        fields = [
            "2026-07-24T00:00:00Z", "open", "/r", "/w", "owner", "",
        ]
        metadata = {
            "attempt_schema_version": "2",
            "dispatch_depth": "1",
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": "1",
            "fallback_hop": "same-harness-headless",
            "attempt_id": "att-ready-stale",
            "launch_outcome": "reaped-before-publish",
        }
        receipt = self.classify([(fields, metadata)])
        self.assertEqual(receipt["state"], "pending")
        self.assertEqual(receipt["children"][0]["reason"], "terminal-commit-pending")

    def test_exact_terminal_envelope_is_reported_without_registry_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "attempt.claude.jsonl"
            log.write_text(
                '{"type":"result","is_error":true,"api_error_status":429}\n',
                encoding="utf-8",
            )
            fields = [
                "2026-07-24T00:00:00Z", "open", "/r", "/w", "owner", "",
            ]
            metadata = {
                "attempt_schema_version": "2",
                "dispatch_depth": "1",
                "transport": "headless",
                "execution_surface": "registered-headless",
                "registered_worker": "1",
                "fallback_hop": "same-harness-headless",
                "attempt_id": "att-ready-envelope",
                "launch_outcome": "reaped-before-publish",
                "log_file": str(log),
            }
            receipt = self.classify([(fields, metadata)])
        child = receipt["children"][0]
        self.assertEqual(receipt["state"], "pending")
        self.assertEqual(child["reason"], "terminal-commit-pending")

    def test_automatically_cancelled_row_is_no_longer_pending(self):
        # J-4
        fields = ["2026-08-26T00:00:00Z", "done", "/r", "/w", "cancelled-auto", ""]
        metadata = sealed_cancellation_metadata("att-j4-automatic")
        metadata["classifier_source"] = "automatic-receipt-unavailable-v1"
        receipt = self.classify([(fields, metadata)])
        child = receipt["children"][0]
        self.assertNotEqual(child["readiness"], "pending")
        self.assertNotEqual(child.get("process_reason"), "post-exit-receipt-incomplete")

    def test_manually_cancelled_row_is_no_longer_pending(self):
        # J-5: "manually-cancelled row no longer readiness=pending"
        fields = ["2026-08-26T00:00:00Z", "done", "/r", "/w", "cancelled-manual", ""]
        metadata = sealed_cancellation_metadata("att-j5-manual")
        metadata["classifier_source"] = "operator-receiptless-cancel-v1"
        receipt = self.classify([(fields, metadata)])
        child = receipt["children"][0]
        self.assertNotEqual(child["readiness"], "pending")
        self.assertNotEqual(child.get("process_reason"), "post-exit-receipt-incomplete")


class RuntimeWaitSharedJoinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs.log"
        self.attempt = "att-wait-shared"
        self.meta = {"attempt_id": self.attempt, "attempt_schema_version": "2",
                     "registered_worker": "1", "execution_surface": "registered-headless",
                     "launch_outcome": "reaped-before-publish", "harness": "opencode",
                     "worker_type": "review", "dispatch_depth": "1"}
        self.write_row("open", self.meta)

    def write_row(self, status, metadata):
        self.jobs.write_text("\t".join(["2026-09-11T00:00:00Z", status, str(self.root),
            str(self.root), "selected", ",".join(k + "=" + v for k, v in metadata.items())]) + "\n")

    def selected(self):
        return READY.selected_rows(self.jobs, attempt_id=self.attempt)

    def test_terminal_commit_must_land_before_success_is_consumed(self):
        def commit(jobs, row):
            self.assertEqual(row.attempt_id, self.attempt)
            self.write_row("done", {**self.meta, "note": "completed-review", "failure_class": "pass"})
            return {"closed": True}
        def proof(jobs, attempt, **kwargs):
            self.assertEqual(READY.JOIN.exact_attempt_row(jobs, attempt).status, "done")
            return READY.JOIN.CurrentDeliveryState(None, "", "", "", "done", "PASS", True, 0,
                                                    False, completion_proven=True)
        with mock.patch.object(READY.JOIN, "settle_finished_attempt", side_effect=commit) as settle, \
                mock.patch.object(READY.JOIN, "current_delivery_state", side_effect=proof):
            result = READY.classify_selection(self.jobs, self.selected(), settle=True)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["children"][0]["status"], "done")
        self.assertEqual(result["children"][0]["required_action"], "advance-completed")
        settle.assert_called_once()

    def test_writer_failure_or_false_success_stays_pending_without_relabeling(self):
        before = self.jobs.read_bytes()
        for closed in (False, True):
            with self.subTest(closed=closed), \
                    mock.patch.object(READY.JOIN, "settle_finished_attempt", return_value={
                        "closed": closed, "reason": "write-unconfirmed"}), \
                    mock.patch.object(READY.JOIN, "current_delivery_state") as consume:
                result = READY.classify_selection(self.jobs, self.selected(), settle=True)
                self.assertEqual(result["state"], "pending")
                self.assertEqual(result["children"][0]["reason"], "terminal-commit-pending")
                consume.assert_not_called()
                self.assertEqual(self.jobs.read_bytes(), before)

    def test_conflict_uses_shared_current_consumption_proof(self):
        self.write_row("done", {**self.meta, "note": "completed-review", "failure_class": "pass"})
        proof = READY.JOIN.CurrentDeliveryState(None, "", "", "", "done", "PASS", True, 0,
                                               False, completion_proven=True, terminal_conflict=True)
        with mock.patch.object(READY.JOIN, "current_delivery_state", return_value=proof), \
                mock.patch.object(READY.JOIN, "settle_finished_attempt") as settle:
            result = READY.classify_selection(self.jobs, self.selected(), settle=True)
        self.assertEqual(result["state"], "terminal")
        self.assertEqual(result["children"][0]["required_action"], "inspect-done-failure")
        settle.assert_not_called()

    def test_unobservable_attempt_retains_recovery_obligation(self):
        metadata = {**self.meta, "pid_scope": "namespace-local", "pid": "99999999", "pid_start": "1"}
        metadata.pop("launch_outcome")
        self.write_row("done", {**metadata, "note": "completed-review", "failure_class": "pass"})
        before = self.jobs.read_bytes()
        with mock.patch.object(READY.JOIN, "recover_receiptless_attempt", return_value={"closed": False}) as recover:
            result = READY.classify_selection(self.jobs, self.selected(), settle=True)
        self.assertEqual(result["state"], "pending")
        recover.assert_called_once()
        self.assertEqual(self.jobs.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
