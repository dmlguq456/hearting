#!/usr/bin/env python3
"""Responsibility contract falsifiers: no model/network credentials used."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch_attempt_policy as policy
import dispatch_contract as contract
import dispatch_supervision as supervision
import dispatch_pending_delivery as pending
import dispatch_session_sweep as sweep

CURRENT = {"attempt_schema_version": "2", "dispatch_depth": "2", "transport": "headless",
           "execution_surface": "registered-headless", "registered_worker": "1",
           "fallback_hop": "same-harness-headless", "route_id": "rt-policy", "route_node": "test",
           "parent_attempt_id": "att-owner", "pid_observer_ns": contract.process_namespace_identity()}


def row(attempt, *, status="open", **metadata):
    meta = {**CURRENT, "attempt_id": attempt, **metadata}
    return f"2026-09-11T00:00:00Z\t{status}\t/r\t/w\ttest\t" + ",".join(f"{k}={v}" for k,v in meta.items()) + "\n"


class ResponsibilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.log"

    def test_committed_success_survives_delays_and_unknown_cleanup_for_all_harnesses(self):
        for harness in ("claude", "codex", "opencode"):
            for state, action in (("live", "wait"), ("unverifiable", "recover"), ("quiescent", "advance")):
                with self.subTest(harness=harness, process=state):
                    decision = policy.decide_attempt("done", {"harness": harness, "note": "completed-marker"}, process_state=state)
                    self.assertEqual((decision.outcome, decision.action, decision.retry_allowed), ("succeeded", action, False))

    def test_unverified_admission_cleanup_cannot_retry_from_terminal_word(self):
        for harness in ("claude", "codex", "opencode"):
            for state in ("live", "unverifiable"):
                decision = policy.decide_attempt("done", {"harness": harness, "note": "dead-launch-error",
                    "review_admission_cleanup": "unverified"}, process_state=state)
                self.assertEqual(decision.outcome, "failed")
                self.assertFalse(decision.retry_allowed)
                self.assertIn(decision.action, {"wait", "recover"})

    def test_receipt_identity_has_one_definition_for_writer_storage_and_carrier(self):
        import dispatch_completion_join as join
        import dispatch_receipt_identity as identity
        receipt = {"schema_version": 2, "state": "ready", "children": [{"attempt_id": "att-x", "slug": "ignored"}], "delivery_timing": {"arbitrary": 1}}
        self.assertEqual(join.canonical_receipt_digest(receipt), pending._canonical_receipt_digest(receipt))
        self.assertEqual(join.canonical_receipt_digest(receipt), identity.receipt_digest(receipt))
        changed = dict(receipt, delivery_timing={"arbitrary": 2})
        self.assertEqual(identity.receipt_digest(receipt), identity.receipt_digest(changed))

    def test_exact_retry_claim_converges_across_competing_processes(self):
        self.jobs.write_text(row("att-failed", status="done", note="dead-launch-error", launch_outcome="never-launched"))
        script = """import sys,json; from pathlib import Path
sys.path.insert(0,sys.argv[1]); import dispatch_contract as d
try:
 ok=d.claim_attempt_row(Path(sys.argv[2]),sys.argv[3],sys.argv[4],launch=True)
 print(json.dumps({'claimed':ok}))
except d.DispatchContractError as e: print(json.dumps({'reason':e.reason}))
"""
        processes = [subprocess.Popen([sys.executable, "-c", script, str(Path(__file__).parent.resolve()), str(self.jobs), aid,
                         row(aid, automatic_retry_of="att-failed")], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for aid in ("att-retry-a", "att-retry-b")]
        results = []
        for process in processes:
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, err)
            results.append(json.loads(out))
        self.assertEqual(sum(r.get("claimed") is True for r in results), 1, results)
        self.assertEqual(sum(r.get("reason") == "retry-already-claimed" for r in results), 1, results)
        self.assertEqual(len(self.jobs.read_text().splitlines()), 2)

    def test_pre_registered_retries_do_not_deadlock_each_others_first_start(self):
        self.jobs.write_text(row("att-failed", status="done", note="dead-launch-error", launch_outcome="never-launched"))
        for aid in ("att-first", "att-second"):
            contract.claim_attempt_row(self.jobs, aid, row(aid, automatic_retry_of="att-failed"))
        self.assertTrue(contract.claim_attempt_row(self.jobs, "att-first", row("att-first", automatic_retry_of="att-failed"), launch=True))
        with self.assertRaises(contract.DispatchContractError) as caught:
            contract.claim_attempt_row(self.jobs, "att-second", row("att-second", automatic_retry_of="att-failed"), launch=True)
        self.assertEqual(caught.exception.reason, "retry-already-claimed")

    def test_success_published_between_retry_proposal_and_claim_wins(self):
        self.jobs.write_text(row("att-failed", status="done", note="completed-marker", launch_outcome="never-launched"))
        with self.assertRaises(contract.DispatchContractError) as caught:
            contract.claim_attempt_row(self.jobs, "att-retry", row("att-retry", automatic_retry_of="att-failed"), launch=True)
        self.assertEqual(caught.exception.reason, "retry-predecessor-not-retryable")
        self.assertEqual(len(self.jobs.read_text().splitlines()), 1)

    def _notice_rows(self, kind="codex-managed-gateway"):
        self.jobs.write_text(row("att-owner", dispatch_depth="1", parent_attempt_id="", parent_sid="parent-test",
            parent_completion_delivery=kind,
            managed_sealed_batch_id="batch-test", pid="99999999", pid_start="1")
            + row("att-child", pid="99999998", pid_start="1"))

    def test_notice_is_idempotent_after_controller_restart_and_never_closes_rows(self):
        self._notice_rows()
        before = self.jobs.read_bytes()
        first = supervision.materialize(self.jobs, {"att-child"}, reason="process-unverifiable")[0]
        second = supervision.materialize(self.jobs, {"att-child"}, reason="process-unverifiable")[0]
        self.assertEqual(first, second)
        self.assertEqual(first["session_generation_supported"], "0")
        self.assertNotIn("recipient_epoch", first["receipt"])
        self.assertEqual(self.jobs.read_bytes(), before)
        supervision.validate_pending_record(first, jobs=self.jobs, expected_thread_id="parent-test",
            expected_epoch=1, expected_attempts={"att-owner"}, expected_sealed_batch_id="batch-test")
        self.assertIn("not workflow completion", supervision.render_text(first["receipt"]))

    def test_claim_binds_live_generation_without_changing_the_obligation(self):
        self._notice_rows()
        record = supervision.materialize(self.jobs, {"att-child"}, reason="join-deadline")[0]
        with self.assertRaisesRegex(pending.PendingDeliveryError, "generation-unproven"):
            pending.claim(self.root, "parent-test", record["delivery_id"], claim_owner="courier",
                          lease_seconds=1, require_generation_proof=True)
        with self.assertRaisesRegex(pending.PendingDeliveryError, "generation-unproven"):
            pending.claim(self.root, "parent-test", record["delivery_id"], claim_owner="courier",
                          lease_seconds=1, require_generation_proof=True,
                          live_recipient_generation=("another-parent", "2"))
        claimed = pending.claim(self.root, "parent-test", record["delivery_id"], claim_owner="courier",
                                lease_seconds=1, require_generation_proof=True,
                                live_recipient_generation=("parent-test", "2"))
        self.assertEqual((claimed["session_generation"], claimed["claim_authority"]), ("2", "generation-proven"))
        self.assertEqual(claimed["receipt"], record["receipt"])
        self.assertEqual(supervision.materialize(self.jobs, {"att-child"}, reason="join-deadline")[0], claimed)

    def test_recovered_work_retires_stale_notice_in_native_carriers(self):
        for kind in ("claude-parent-runtime", "opencode-turn"):
            with self.subTest(kind=kind):
                self._notice_rows(kind)
                record = supervision.materialize(self.jobs, {"att-child"}, reason="join-deadline")[0]
                lines = self.jobs.read_text().splitlines()
                fields = lines[1].split("\t")
                fields[1] = "done"
                meta = contract.parse_registry_metadata(fields[5])
                meta.pop("pid"); meta.pop("pid_start")
                meta.update(launch_outcome="never-launched", note="completed-marker")
                fields[5] = ",".join(f"{key}={value}" for key, value in meta.items())
                lines[1] = "\t".join(fields)
                self.jobs.write_text("\n".join(lines)+"\n")
                claimed, _ = sweep.sweep_deliver(self.root, kind, "parent-test")
                self.assertEqual(claimed, [])
                stored = pending.read(self.root, "parent-test", record["delivery_id"])
                self.assertEqual(stored["expiry_reason"], "supervision-resolved")
                # Separate recipient queue for the other carrier's identical scenario.
                import shutil
                shutil.rmtree(pending.record_directory(self.root, "parent-test"))

    def test_failed_join_observer_preserves_work_and_hands_back_then_recovers(self):
        self._notice_rows()
        before = self.jobs.read_bytes()
        join = mock.Mock(side_effect=[ValueError("join-process-failed"), {"state": "ready"}])
        with mock.patch.object(supervision.time, "sleep") as delay:
            receipt = supervision.wait_for_batch(join=join, attempts={"att-child"}, jobs=self.jobs)
        self.assertEqual(receipt["state"], "ready")
        self.assertEqual(self.jobs.read_bytes(), before)
        delay.assert_called_once_with(30.0)
        records = list(pending.record_directory(self.root, "parent-test").glob("delivery-*.json"))
        self.assertEqual(json.loads(records[0].read_text())["receipt"]["reason"], "join-observer-failed")

    def test_wait_owns_more_than_old_seven_checkpoints_without_model_resume(self):
        self._notice_rows()
        join = mock.Mock(side_effect=[{"state": "timeout"} for _ in range(9)] + [{"state": "ready"}])
        events=[]
        with mock.patch.object(supervision.time, "sleep"):
            result = supervision.wait_for_batch(join=join, attempts={"att-child"}, jobs=self.jobs,
                                                 parent_attempt_id="att-owner", emit=events.append)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(len(events), 9)
        self.assertEqual(len(list(pending.record_directory(self.root,"parent-test").glob("delivery-*.json"))), 1)
        self.assertTrue(all(event["responsible"] == "supervision-controller" for event in events))


if __name__ == "__main__":
    unittest.main()
