#!/usr/bin/env python3
"""Responsibility contract falsifiers: no model/network credentials used."""
import importlib.util
import hashlib
import json
import os
import shlex
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

    def test_supervisor_exit_closed_route_does_not_hide_live_child_cleanup(self):
        from types import SimpleNamespace
        self.jobs.write_text(row("att-child", status="done", workflow_completion="runtime-v1"))
        with mock.patch("dispatch_notice_state.route_obligation_closed", return_value=True), \
             mock.patch.object(contract, "observed_attempt_liveness",
                               return_value=SimpleNamespace(process_state="live", process_reason="pid-live")), \
             mock.patch("codex_dispatch_terminal.terminal_envelope_observed", return_value=False):
            self.assertTrue(supervision._pending(
                {"att-child": ("done", {**CURRENT, "attempt_id": "att-child", "workflow_completion": "runtime-v1"})},
                ["att-child"], self.jobs, reason="supervisor-exited"))

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

    def test_wait_projects_multigeneration_lineage_to_one_original_final_edge(self):
        targets = {
            "att-a": ("att-b", {"original_attempt_id":"att-a", "replacement_attempt_id":"att-b",
                                 "family_id":"fam-a", "claim_digest":"digest-a"}),
            "att-b": ("att-c", {"original_attempt_id":"att-b", "replacement_attempt_id":"att-c",
                                 "family_id":"fam-b", "claim_digest":"digest-b"}),
            "att-c": ("att-d", {"original_attempt_id":"att-c", "replacement_attempt_id":"att-d",
                                 "family_id":"fam-c", "claim_digest":"digest-c"}),
            "att-d": ("att-d", None),
        }

        def checkpoint(selected):
            source=next(iter(selected))
            target,edge=targets[source]
            return {target}, [edge] if edge else [], []

        result=supervision.wait_for_batch(
            join=lambda selected: {"state":"ready", "children":[{"attempt_id":next(iter(selected))}]},
            attempts={"att-a"}, jobs=Path("/unused"), replacement_checkpoint=checkpoint)
        self.assertEqual(result["children"], [{"attempt_id":"att-d"}])
        self.assertEqual(result["replacement_lineage"], [{
            "original_attempt_id":"att-a", "replacement_attempt_id":"att-d",
            "family_id":"fam-a", "claim_digest":"digest-a",
        }])

    def test_no_progress_parent_consumer_uses_the_existing_route_start_contract(self):
        route_file = self.root / "route.json"
        jobs = self.root / "jobs.log"
        jobs.write_text("")
        metadata = {**CURRENT, "attempt_id": "att-owner", "worker_type": "owner",
                    "parent_sid": "parent", "owner_route_id": "rt-owner"}
        record = {"receipt": {"kind": supervision.KIND, "reason": "no-progress",
                               "job_registry": str(jobs), "owner_attempt_id": "att-owner"}}
        seen = []

        def arm(result, path, registry, *, environ=None):
            seen.append((result, path, registry, environ))
            return {"state": "armed", "cause": "replacement"}

        with mock.patch.object(supervision, "_rows", return_value={"att-owner": ("done", metadata)}), \
             mock.patch("dispatch_replacement._route", return_value=(route_file, {"route_id": "rt-owner"})):
            continued = supervision.continue_for_parent(record, session_id="parent",
                recipient_kind="codex-managed-gateway", environ={}, arm=arm)
        self.assertEqual(continued, {"state": "armed", "cause": "replacement"})
        self.assertEqual(seen[0][0], {"reason": "no-progress", "route_id": "rt-owner",
                                      "source_attempt_id": "att-owner"})
        self.assertEqual((seen[0][1], seen[0][2]), (route_file, jobs))
        self.assertEqual(seen[0][3]["CODEX_THREAD_ID"], "parent")
        with mock.patch("dispatch_replacement.parent_resume_obligation", return_value={}), \
             mock.patch("route_parent_close.row_requested", return_value=False):
            self.assertTrue(supervision._pending({"att-owner": ("done", metadata)},
                ["att-owner"], jobs, reason="no-progress"))
        with mock.patch("dispatch_replacement.parent_resume_obligation", return_value=None), \
             mock.patch("route_parent_close.row_requested", return_value=False):
            self.assertFalse(supervision._pending({"att-owner": ("done", metadata)},
                ["att-owner"], jobs, reason="no-progress"))

    def test_identity_refusal_hands_answerless_duty_to_parent_without_new_receipt_kind(self):
        refusal = {"state": "needs-attention", "reason": "replacement-parent-identity-unproven"}
        with mock.patch("dispatch_owner_input.retained", return_value=[]), \
             mock.patch.object(supervision, "materialize", return_value=[{}]) as materialize:
            handed = supervision.handover_identity_refusal(self.jobs, "att-owner", refusal)
        self.assertEqual(materialize.call_args.kwargs["reason"], "no-progress")
        self.assertTrue(handed["parent_notified"])
        self.assertEqual(set(handed), {"parent_notified", "next_step"})
        self.assertIn("does not claim that a worker launched", handed["next_step"])

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
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                self.jobs.write_text(row("att-failed", status="done", harness=harness,
                                         note="completed-marker", launch_outcome="never-launched"))
                with self.assertRaises(contract.DispatchContractError) as caught:
                    contract.claim_attempt_row(
                        self.jobs, "att-retry",
                        row("att-retry", harness=harness, automatic_retry_of="att-failed"),
                        launch=True)
                self.assertEqual(caught.exception.reason, "retry-predecessor-not-retryable")
                self.assertEqual(len(self.jobs.read_text().splitlines()), 1)

    def test_terminal_conflict_preserves_pass_blocks_consumption_and_has_exact_review_recovery(self):
        import dispatch_completion_join as join
        self._notice_rows()
        lines = self.jobs.read_text().splitlines()
        lines[1] = row("att-child", status="done", note="completed-marker", failure_class="pass",
                       launch_outcome="never-launched").strip()
        self.jobs.write_text("\n".join(lines)+"\n")
        route, node, marker = {"route_id": "rt-policy"}, {"id": "test"}, {"attempt_id": "att-child", "registered_worker": True}
        spec = importlib.util.spec_from_file_location("conflict_route", Path(contract.__file__).with_name("capability-route.py"))
        route_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(route_module)
        evidence = self.root / "passed.md"; evidence.write_text("PASS\n")
        marker_path = route_module.completion_dir(route["route_id"], jobs=self.jobs) / "test.json"
        marker_path.parent.mkdir(parents=True)
        # SD-154 A-2: `_marker_identity_row` now proves schema-v2 identity via
        # `evidence_currency` (single evidence-sha recompute site, A-SD154-8) --
        # the same schema/sequence/history-byte shape every real
        # `write_completion_marker` marker carries -- so the on-disk marker
        # needs those fields too. `marker` itself stays the small dict the rest
        # of this test passes straight to `completion_attempt_readiness`, which
        # never reads schema_version/sequence.
        disk_marker = {**marker, "schema_version": 2, "sequence": 1, "route_id": "rt-policy", "node_id": "test",
            "evidence": {"path": str(evidence), "sha256": route_module.evidence_digest(evidence)}}
        marker_path.write_text(json.dumps(disk_marker))
        (marker_path.parent / "test.1.json").write_text(json.dumps(disk_marker))
        marker_bytes = marker_path.read_bytes()
        observe = lambda: route_module._marker_identity_row(route, node, "test", None, jobs=self.jobs)
        self.assertTrue(observe()["passed"])
        self.assertEqual(contract.completion_attempt_readiness(route, node, marker, self.jobs).state, "ready")
        result = contract.reconcile_attempt_terminal(self.jobs, "att-child", "dead-worker-fail",
            evidence={"failure_class": "fail", "classifier_source": "supervisor-terminal-v1"})
        self.assertEqual(result, "terminal-conflict")
        raw = self.jobs.read_text().splitlines()[1]
        meta = contract.parse_registry_metadata(raw.split("\t")[5])
        self.assertEqual(policy.committed_outcome("done", meta), "succeeded")
        self.assertEqual(policy.required_action("done", meta), "inspect-done-failure")
        self.assertEqual(contract.completion_attempt_readiness(route, node, marker, self.jobs).reason, "terminal-evidence-conflict")
        self.assertFalse(observe()["passed"])
        self.assertEqual(marker_path.read_bytes(), marker_bytes)
        joined = join.join_batch(jobs=self.jobs, parent_attempt_id="att-owner", timeout=0, interval=0.05)
        self.assertEqual(joined["children"][0]["required_action"], "inspect-done-failure")
        notice = supervision.materialize(self.jobs, {"att-child"}, reason="terminal-evidence-conflict")[0]
        self.assertTrue(supervision.notice_is_current(notice))
        review = self.root / "disposition.md"
        review.write_text("Compared the recorded PASS and late failure; retain the recorded result.\n")
        with self.assertRaisesRegex(contract.DispatchContractError, "row-changed"):
            contract.resolve_terminal_conflict(self.jobs, "att-child", expected_row_sha256="0"*64, review=review)
        cli = [sys.executable, str(Path(contract.__file__).with_name("dispatch-registry.py")),
               "resolve-terminal-conflict", "--jobs", str(self.jobs), "--attempt", "att-child"]
        preview = subprocess.run(cli, capture_output=True, text=True, timeout=10)
        self.assertEqual(preview.returncode, 0, preview.stdout+preview.stderr)
        self.assertEqual(self.jobs.read_text().splitlines()[1], raw)
        # The preview prints the apply command with this row's digest; the review is the one input.
        apply_command = json.loads(preview.stdout.split("\n", 1)[1])["apply_command"]
        self.assertIn(hashlib.sha256(raw.encode()).hexdigest(), apply_command)
        applied = subprocess.run(shlex.split(apply_command.replace("<review report>", shlex.quote(str(review)))),
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(applied.returncode, 0, applied.stdout+applied.stderr)
        self.assertEqual(contract.completion_attempt_readiness(route, node, marker, self.jobs).state, "ready")
        self.assertTrue(observe()["passed"])
        self.assertFalse(supervision.notice_is_current(notice))

        # Replaying the same observation does not undo the recorded review.
        contract.reconcile_attempt_terminal(self.jobs, "att-child", "dead-worker-fail",
            evidence={"failure_class": "fail", "classifier_source": "supervisor-terminal-v1"})
        self.assertEqual(contract.completion_attempt_readiness(route, node, marker, self.jobs).state, "ready")
        # A different contradiction is a new obligation, never covered by the old review.
        contract.reconcile_attempt_terminal(self.jobs, "att-child", "dead-worker-blocked",
            evidence={"failure_class": "blocked", "classifier_source": "supervisor-terminal-v1"})
        fresh = supervision.materialize(self.jobs, {"att-child"}, reason="terminal-evidence-conflict")[0]
        self.assertNotEqual(fresh["delivery_id"], notice["delivery_id"])
        self.assertEqual(contract.completion_attempt_readiness(route, node, marker, self.jobs).reason, "terminal-evidence-conflict")
        self.assertFalse(supervision.notice_is_current(notice))

        # Re-observing reviewed A must not hide unresolved B.
        contract.reconcile_attempt_terminal(self.jobs, "att-child", "dead-worker-fail",
            evidence={"failure_class": "fail", "classifier_source": "supervisor-terminal-v1"})
        self.assertEqual(contract.completion_attempt_readiness(route, node, marker, self.jobs).reason, "terminal-evidence-conflict")
        review_b = self.root / "second-disposition.md"
        review_b.write_text("Inspected both contradictions, including the later BLOCKED evidence; retain PASS.\n")
        raw = self.jobs.read_text().splitlines()[1]
        contract.resolve_terminal_conflict(self.jobs, "att-child",
            expected_row_sha256=hashlib.sha256(raw.encode()).hexdigest(), review=review_b)
        for note, failure in (("dead-worker-fail", "fail"), ("dead-worker-blocked", "blocked")):
            self.assertEqual(contract.reconcile_attempt_terminal(self.jobs, "att-child", note,
                evidence={"failure_class": failure, "classifier_source": "supervisor-terminal-v1"}), "already-terminal")
            self.assertEqual(contract.completion_attempt_readiness(route, node, marker, self.jobs).state, "ready")
        history = policy.terminal_conflicts(contract.parse_registry_metadata(self.jobs.read_text().splitlines()[1].split("\t")[5]))
        self.assertEqual(len(history), 2)
        self.assertEqual({entry["review_sha256"] for entry in history.values()},
                         {hashlib.sha256(review.read_bytes()).hexdigest(), hashlib.sha256(review_b.read_bytes()).hexdigest()})

    def test_cancelled_work_never_becomes_an_automatic_retry_from_an_old_dead_note(self):
        for harness in ("claude", "codex", "opencode"):
            decision = policy.decide_attempt("cancelled", {"harness": harness, "note": "dead-runtime-exit"},
                                             process_state="quiescent")
            self.assertFalse(decision.retry_allowed)

    def test_chain_conflict_pauses_without_cancelling_and_is_rechecked_at_claim(self):
        from types import SimpleNamespace
        import dispatch_subsession_advance as chain
        common = dict(stage_authority="0", session_chain_id="ssc-conflict", subsession_count="2",
            subsession_mode="serial", subsession_purpose="planned", phase_brief=str(self.root/"brief"),
            state_ledger=str(self.root/"state"), phase_brief_sha256="1"*64,
            fixed_files_sha256="2"*64, narrow_verify_sha256="3"*64, expected_round_trips="1")
        predecessor = row("att-first", status="done", note="completed-subsession", failure_class="pass",
            launch_outcome="never-launched", subsession_id="ss-first", subsession_index="1", **common)
        successor = row("att-second", subsession_id="ss-second", subsession_index="2", **common)
        self.jobs.write_text(predecessor+successor)
        contract.reconcile_attempt_terminal(self.jobs, "att-first", "dead-worker-fail", evidence={"failure_class":"fail"})
        before = self.jobs.read_bytes()
        meta = contract.parse_registry_metadata(self.jobs.read_text().splitlines()[0].split("\t")[5])
        result = chain.advance_chain_step(self.jobs, "att-owner",
            {"att-first": SimpleNamespace(attempt_id="att-first", status="done", metadata=meta)})
        self.assertEqual((result.outcome, result.reason), ("unavailable", "terminal-evidence-conflict"))
        with self.assertRaises(contract.DispatchContractError) as caught:
            contract.claim_attempt_row(self.jobs, "att-second", successor, launch=True)
        self.assertEqual(caught.exception.reason, "terminal-evidence-conflict")
        self.assertEqual(self.jobs.read_bytes(), before)
        review = self.root/"chain-review.md"; review.write_text("Reviewed conflicting terminal evidence; keep the committed slice result.\n")
        raw = self.jobs.read_text().splitlines()[0]
        contract.resolve_terminal_conflict(self.jobs, "att-first",
            expected_row_sha256=hashlib.sha256(raw.encode()).hexdigest(), review=review)
        self.assertTrue(contract.claim_attempt_row(self.jobs, "att-second", successor, launch=True))

    def _notice_rows(self, kind="codex-managed-gateway"):
        self.jobs.write_text(row("att-owner", dispatch_depth="1", parent_attempt_id="", parent_sid="parent-test",
            parent_completion_delivery=kind, route_node="__owner__",
            managed_sealed_batch_id="batch-test", pid="99999999", pid_start="1")
            + row("att-child", pid="99999998", pid_start="1"))

    def test_parent_session_ended_notice_keeps_exact_attempts_without_manual_recovery(self):
        self._notice_rows()
        records = supervision.materialize(
            self.jobs, {"att-child"}, reason="parent-session-ended")
        receipt = records[0]["receipt"]
        self.assertEqual(receipt["monitored_attempt_ids"], ["att-child"])
        self.assertEqual(receipt["reason"], "parent-session-ended")
        text = supervision.render_text(receipt)
        self.assertIn("continue unchanged", text)
        self.assertIn("durable batch record", text)
        self.assertIn("confirmed same-seat successor", text)
        self.assertNotIn("run the route's start command", text)

    def test_an_answer_from_another_session_reaches_the_parent_as_a_notice(self):
        # RA-10: a non-parent's answer to a BLOCKED owner is not a dead end; the parent is told.
        for kind in ("claude-parent-runtime", "codex-native-queue", "opencode-turn"):
            with self.subTest(kind=kind):
                self.jobs.write_text(row("att-owner", status="done", dispatch_depth="1", parent_attempt_id="",
                    parent_sid="parent-test", parent_completion_delivery=kind, worker_type="owner",
                    note="dead-worker-blocked"))
                kept = [{"id": "input-1", "digest": "d1", "text": "use the smaller batch"}]
                with mock.patch("dispatch_owner_input.blocked_owner_answers", return_value=kept), \
                     mock.patch("dispatch_replacement._replacement_in_flight", return_value=False), \
                     mock.patch.object(supervision, "_resume_text", return_value="python3 capability-route.py start --route /r.json"):
                    record = supervision.materialize(self.jobs, {"att-owner"}, reason=supervision.ANSWER_AWAITING_PARENT)[0]
                    self.assertEqual(record["recipient_kind"], kind)
                    self.assertTrue(supervision.notice_is_current(record))
                    text = supervision.render_text(record["receipt"])
                    again = supervision.materialize(self.jobs, {"att-owner"}, reason=supervision.ANSWER_AWAITING_PARENT)[0]
                self.assertEqual(again, record)                       # one answer, one notice
                self.assertIn("answer is kept", text)
                self.assertIn("Do not ask for the answer again", text)
                self.assertIn("start --route /r.json", text)
                with mock.patch("dispatch_owner_input.blocked_owner_answers", return_value=kept), \
                     mock.patch("dispatch_replacement._replacement_in_flight", return_value=True):
                    self.assertFalse(supervision.notice_is_current(record))   # the parent already continued
                with mock.patch("dispatch_owner_input.blocked_owner_answers", return_value=[]):
                    self.assertFalse(supervision.notice_is_current(record))
                for path in (self.root / "pending-delivery").rglob("*.json"):
                    path.unlink()

    def test_the_parents_carrier_arms_the_answered_route_under_parent_identity(self):
        # The carrier records the parent's start; the later run owns execution and result delivery.
        from session_identity import session_env
        kept = [{"id": "input-1", "digest": "d1", "text": "use the smaller batch"}]
        cases = [("claude-parent-runtime", "claude", {"state": "armed", "cause": "answer"}),
                 ("codex-native-queue", "codex", {"state": "armed", "cause": "answer"}),
                 ("opencode-turn", "opencode", {"state": "armed", "cause": "answer"}),
                 ("claude-parent-runtime", "claude", None)]
        for kind, harness, outcome in cases:
            with self.subTest(kind=kind, armed=bool(outcome)):
                self.jobs.write_text(row("att-owner", status="done", dispatch_depth="1", parent_attempt_id="",
                    parent_sid="parent-test", parent_completion_delivery=kind, worker_type="owner",
                    note="dead-worker-blocked"))
                armed = []
                with mock.patch("dispatch_owner_input.blocked_owner_answers", return_value=kept), \
                     mock.patch("dispatch_replacement._replacement_in_flight", return_value=False), \
                     mock.patch("dispatch_replacement._route", return_value=(Path("/r/route.json"), {"route_id": "rt-answer"})), \
                     mock.patch.object(supervision, "_resume_text", return_value="python3 capability-route.py start --route /r/route.json"), \
                     mock.patch("capacity_auto_resume.arm",
                                side_effect=lambda result, route_file, jobs, environ: armed.append(
                                    (result, route_file, environ)) or outcome):
                    supervision.materialize(self.jobs, {"att-owner"}, reason=supervision.ANSWER_AWAITING_PARENT)
                    records, _ = sweep.sweep_deliver(self.jobs.parent, kind, "parent-test")
                    text = sweep.delivery_context([(self.jobs.parent, records)])
                self.assertEqual(len(armed), 1)
                result, route_file, env = armed[0]
                self.assertEqual((result["reason"], result["route_id"], route_file),
                                 (supervision.ANSWER_AWAITING_PARENT, "rt-answer", Path("/r/route.json")))
                self.assertEqual((env[session_env()[harness][0]], env["AGENT_DISPATCH_CALLER_HARNESS"]),
                                 ("parent-test", harness))
                if outcome:
                    self.assertIn("scheduled the route's start under the parent's identity", text)
                    self.assertIn("not completion", text)
                    self.assertNotIn("start --route", text)
                else:  # nothing could be armed: the notice keeps the start command
                    self.assertIn("start --route /r/route.json", text)
                for path in (self.root / "pending-delivery").rglob("*.json"):
                    path.unlink()
        other = {"receipt": {"kind": "supervision", "reason": "no-progress"}}
        self.assertIsNone(supervision.continue_for_parent(other, session_id="parent-test",
                                                          recipient_kind="claude-parent-runtime",
                                                          arm=lambda *a, **k: self.fail("armed")))

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

    def test_completion_pending_notice_late_renders_exact_refusal_without_finish_requirement(self):
        import dispatch_terminal_commit
        self._notice_rows()
        receipt = supervision.materialize(
            self.jobs, {"att-child"}, reason="workflow-completion-pending")[0]["receipt"]
        digest = supervision.receipt_digest(receipt)
        diagnosis = {"state": "closure-pending", "checkpoint": "route-closed",
                     "last_refusal": {
                         "reason": "output-invalid",
                         "detail": "locator-invalid-component:artifacts/bad|name.txt\nignored",
                         "record_path": str(self.root / "attempts" / "event.json"),
                         "remediation": "Correct the reported locator, mark the exact directory, or move it outside the cycle; the controller retries automatically.",
                     }}
        with mock.patch.object(dispatch_terminal_commit, "inspect_owner_completion", return_value=diagnosis):
            text = supervision.render_text(receipt)
        self.assertIn("output-invalid", text)
        self.assertIn("artifacts/bad|name.txt", text)
        self.assertNotIn("ignored", text)
        self.assertIn("event.json", text)
        self.assertIn("Correct the reported locator", text)
        self.assertIn("retries closure automatically", text)
        self.assertIn("read-only diagnosis", text)
        self.assertNotIn(" finish ", text)
        self.assertEqual(supervision.receipt_digest(receipt), digest)

    def test_residue_pid_is_named_in_the_notice_only_while_it_runs(self):
        self._notice_rows()
        receipt = supervision.materialize(self.jobs, {"att-child"}, reason="join-deadline")[0]["receipt"]
        with mock.patch.object(contract, "residue_live_pids", return_value=()):
            plain = supervision.render_text(receipt)
        self.assertNotIn("left process", plain)
        with mock.patch.object(contract, "residue_live_pids", return_value=(753216,)):
            named = supervision.render_text(receipt)
        self.assertTrue(named.startswith(plain))
        self.assertIn("att-child finished, but the worker left process pid 753216 running", named)
        self.assertIn("closes by itself once that process exits", named)

    def test_cleanup_notice_after_exact_received_result_keeps_observation_without_reopening_work(self):
        self._notice_rows()
        record = supervision.materialize(self.jobs, {"att-child"}, reason="join-deadline")[0]
        completed = {"children": [{"attempt_id": "att-child", "status": "done",
            "required_action": "advance-completed"}]}
        with mock.patch.object(supervision, "_acknowledged_completion_result", return_value=completed), \
             mock.patch.object(contract, "residue_live_pids", return_value=(753216,)):
            text = supervision.render_text(record["receipt"])
        self.assertIn("retains cleanup observation only", text)
        self.assertIn("cleanup residue", text)
        self.assertIn("753216", text)
        self.assertIn("Do not restart the goal", text)
        self.assertIn("or ask the user whether to wait or cancel", text)
        self.assertNotIn("run the route's start command", text)
        self.assertIn("grants no process signal", text)

    def test_cleanup_notice_without_exact_completion_ack_keeps_existing_recovery_guidance(self):
        self._notice_rows()
        receipt = supervision.materialize(self.jobs, {"att-child"}, reason="join-deadline")[0]["receipt"]
        with mock.patch.object(supervision, "_acknowledged_completion_result", return_value=None):
            text = supervision.render_text(receipt)
        self.assertNotIn("retains cleanup observation only", text)
        self.assertIn("inspect these exact attempts", text)
        self.assertIn("An inspection notice alone does not require a user decision", text)
        self.assertIn("actual unfinished work", text)
        self.assertNotIn("ask the user whether to keep waiting or cancel", text)

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

    def test_join_error_keeps_exact_typed_reason_but_rejects_foreign_or_raw_output(self):
        valid = dict(schema_version=2, state="contract-error", parent_attempt_id="att-owner",
                     children=[], reason="join-internal-error-PermissionError")
        self.assertIn("exit=69:reason=join-internal-error-PermissionError",
                      supervision.join_process_error(69, valid, "att-owner"))
        for change in ({"parent_attempt_id": "att-foreign"}, {"state": "ready"},
                       {"reason": "secret log: /private/path\nbody"}, {"reason": "x" * 129},
                       {"children": [{"attempt_id": "att-child"}]}):
            self.assertTrue(supervision.join_process_error(69, {**valid, **change}, "att-owner")
                            .endswith("reason=unverified-error-receipt"))

    def test_runtime_join_error_reaches_shared_wait_and_next_observation_recovers(self):
        from types import SimpleNamespace
        self._notice_rows()
        before = self.jobs.read_bytes()
        # Claude and OpenCode use the same session supervisor implementation.
        for filename in ("claude-session-supervisor.py", "codex-app-server-supervisor.py"):
            with self.subTest(supervisor=filename):
                source = Path(__file__).with_name(filename)
                spec = importlib.util.spec_from_file_location("join_error_" + filename.replace("-", "_"), source)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                script = self.root / "failed-observer.py"
                script.write_text('import json\nprint(json.dumps(dict(schema_version=2, '
                                  'state="contract-error", parent_attempt_id="att-owner", children=[], '
                                  'reason="join-internal-error-PermissionError")))\nraise SystemExit(69)\n')
                import shlex
                args = SimpleNamespace(join_command=shlex.join([sys.executable, str(script)]),
                                       jobs=str(self.jobs), parent_attempt_id="att-owner",
                                       join_interval=0.01, join_timeout=0)
                def first_join(attempts):
                    return module.run_join(args, attempts)
                count = 0
                def observer(attempts):
                    nonlocal count
                    count += 1
                    return first_join(attempts) if count == 1 else {"state": "ready"}
                events = []
                with mock.patch.object(supervision.time, "sleep"), mock.patch.object(module, "emit"):
                    result = supervision.wait_for_batch(join=observer, attempts={"att-child"},
                        jobs=self.jobs, parent_attempt_id="att-owner", emit=events.append)
                self.assertEqual(result["state"], "ready")
                self.assertIn("exit=69:reason=join-internal-error-PermissionError", events[0]["observer_error"])
                self.assertEqual(events[0]["responsible"], "supervision-controller")
                self.assertEqual(self.jobs.read_bytes(), before)

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

    def test_replacement_wait_joins_effective_once_and_retains_second_failure(self):
        edge = {"original_attempt_id": "att-child", "replacement_attempt_id": "att-retry"}
        attention = {"source_attempt_id": "att-retry", "reason": "automatic-replacement-exhausted"}
        seen = []
        def join(attempts):
            seen.append(attempts)
            return {"state": "ready", "children": [{"attempt_id": next(iter(attempts))}]}
        def checkpoint(attempts):
            return ({"att-retry"}, [edge], []) if attempts == {"att-child"} else (attempts, [], [attention])
        result = supervision.wait_for_batch(join=join, attempts={"att-child"}, jobs=self.jobs,
                                            replacement_checkpoint=checkpoint)
        self.assertEqual(seen, [{"att-child"}, {"att-retry"}])
        self.assertEqual(result["replacement_lineage"], [edge])
        self.assertEqual(result["replacement_attention"], [attention])
        self.assertEqual(result["children"], [{"attempt_id": "att-retry"}])

    def test_replacement_watch_expiry_keeps_effective_scope(self):
        edge = {"original_attempt_id": "att-child", "replacement_attempt_id": "att-retry"}
        with mock.patch.object(supervision, "materialize", return_value=[]):
            result = supervision.wait_for_batch(
                join=lambda attempts: {"state": "timeout"}, attempts={"att-child"}, jobs=self.jobs,
                deadline=0, replacement_checkpoint=lambda attempts:
                    ({"att-retry"}, [edge], []) if attempts == {"att-child"} else (attempts, [], []))
        self.assertEqual(result["state"], "watch-expired")
        self.assertEqual(result["replacement_lineage"], [edge])
        self.assertEqual(result["children"], [{"attempt_id": "att-retry"}])

    def test_wait_for_batch_deadline_records_once_and_returns(self):
        # plan.md item 8: a caller that supplies its own `deadline` (the
        # unfinishable-watch budget) stops after that deadline instead of
        # retrying forever -- exactly one `watch-deadline` notice, no more
        # join attempts after the deadline is reached.
        self._notice_rows()
        join = mock.Mock(return_value={"state": "timeout"})
        with mock.patch.object(supervision.time, "sleep"):
            result = supervision.wait_for_batch(
                join=join, attempts={"att-child"}, jobs=self.jobs,
                deadline=supervision.time.monotonic() - 1.0,
            )
        self.assertEqual(result["state"], "watch-expired")
        self.assertEqual(result["reason"], "watch-deadline")
        join.assert_called_once()
        records = list(pending.record_directory(self.root, "parent-test").glob("delivery-*.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text())["receipt"]["reason"], "watch-deadline")

    def test_wait_for_batch_without_deadline_keeps_retrying_like_before(self):
        # Pinning: `deadline=None` (every existing caller) must not change --
        # the loop keeps retrying past what would have been a deadline.
        self._notice_rows()
        join = mock.Mock(side_effect=[{"state": "timeout"} for _ in range(3)] + [{"state": "ready"}])
        with mock.patch.object(supervision.time, "sleep"):
            result = supervision.wait_for_batch(join=join, attempts={"att-child"}, jobs=self.jobs)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(join.call_count, 4)

    def test_wait_for_batch_stop_check_halts_under_its_own_reason(self):
        # The receiver-unavailable probe (codex-managed-completion.py) plugs
        # in here: a non-empty reason halts the same way a reached deadline
        # does, under whatever REASONS token the caller returns.
        self._notice_rows()
        join = mock.Mock(return_value={"state": "timeout"})
        with mock.patch.object(supervision.time, "sleep"):
            result = supervision.wait_for_batch(
                join=join, attempts={"att-child"}, jobs=self.jobs,
                stop_check=lambda: "receiver-unavailable",
            )
        self.assertEqual(result["state"], "watch-expired")
        self.assertEqual(result["reason"], "receiver-unavailable")
        join.assert_called_once()
        records = list(pending.record_directory(self.root, "parent-test").glob("delivery-*.json"))
        self.assertEqual(json.loads(records[0].read_text())["receipt"]["reason"], "receiver-unavailable")

    def test_wait_for_batch_stop_check_only_consulted_after_a_real_timeout(self):
        # An observer exception must not be treated as a stop signal -- the
        # existing join-observer-failed backoff still owns that path, and
        # `stop_check` never runs on that tick.
        self._notice_rows()
        join = mock.Mock(side_effect=[ValueError("boom"), {"state": "ready"}])
        stop_check = mock.Mock(return_value="")
        with mock.patch.object(supervision.time, "sleep"):
            result = supervision.wait_for_batch(join=join, attempts={"att-child"}, jobs=self.jobs,
                                                 stop_check=stop_check)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(stop_check.call_count, 0)

    def test_exact_materialization_uses_indexed_attempt_ancestry_on_large_history(self):
        from dispatch_registry_cache import RegistryRecord, clear_cache, metrics_snapshot, reset_metrics

        self._notice_rows()
        with self.jobs.open("a", encoding="utf-8") as handle:
            for index in range(4000):
                handle.write(row(f"att-unrelated-{index:05d}", parent_attempt_id="att-other"))
        clear_cache()
        reset_metrics()
        visited = []
        original = RegistryRecord.completion_metadata

        def observe(record):
            metadata = original(record)
            visited.append(metadata.get("attempt_id"))
            return metadata

        with mock.patch.object(RegistryRecord, "completion_metadata", observe):
            first = supervision.materialize(self.jobs, {"att-child"}, reason="join-deadline")
            second = supervision.materialize(self.jobs, {"att-child"}, reason="join-deadline")
        self.assertEqual(first[0]["delivery_id"], second[0]["delivery_id"])
        self.assertEqual(len(visited), 4, "each checkpoint should project only child and owner rows")
        self.assertEqual(set(visited), {"att-child", "att-owner"})
        metrics = metrics_snapshot()
        self.assertEqual(metrics["content_reads"], 1)
        self.assertEqual(metrics["full_metadata_parses"], 1)
        self.assertGreaterEqual(metrics["cache_hits"], 1)
        clear_cache()

    def test_stop_waiting_aborts_without_materializing_or_retrying(self):
        self._notice_rows()
        join = mock.Mock(return_value={"state": "timeout"})
        with mock.patch.object(supervision, "materialize") as materialize, \
             mock.patch.object(supervision.time, "sleep") as sleep:
            with self.assertRaises(supervision.StopWaiting):
                supervision.wait_for_batch(
                    join=join, attempts={"att-child"}, jobs=self.jobs,
                    stop_check=lambda: (_ for _ in ()).throw(
                        supervision.StopWaiting("observer-ownership-lost")))
        join.assert_called_once()
        materialize.assert_not_called()
        sleep.assert_not_called()



class NamespaceExtinctObligationTest(unittest.TestCase):
    """5th Codex run: an extinct namespace is a terminal-writer job, not an unverifiable process."""

    def test_open_row_is_a_reconcile_obligation_and_the_closed_row_ends_it(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        jobs = Path(temp.name) / "jobs.log"
        namespace = {"pid": "464", "pid_start": "537327887", "pgid": "464",
                     "pid_scope": "namespace-local", "pid_observer_ns": "pid:[4026534323]",
                     "pid_ns": "pid:[4026534323]", "launch_lifecycle": "foreground-scoped"}

        def probe(_metadata, *, host_complete=False):
            if host_complete:
                return contract.ProcessGroupObservation("empty")
            return contract.ProcessGroupObservation("unverifiable", (), "observer-namespace-mismatch")

        with mock.patch.object(contract, "namespace_gone", return_value="extinct"), \
                mock.patch.object(contract, "attempt_tagged_descendants", side_effect=probe):
            jobs.write_text(row("att-extinct", **namespace))
            rows = supervision._rows(jobs)
            meta = rows["att-extinct"][1]
            proof = contract.observed_attempt_liveness("open", meta, terminal_receipt_gate=True)
            self.assertEqual((proof.state, proof.process_reason),
                             ("reconcile-needed", contract.NAMESPACE_EXTINCT_REASON))
            self.assertEqual(policy.decide_attempt("open", meta, process_state=proof.process_state,
                                                   process_reason=proof.process_reason).action,
                             "reconcile")
            self.assertTrue(supervision._pending(rows, ["att-extinct"], jobs))
            jobs.write_text(row("att-extinct", status="done", note="dead-namespace-absent",
                                failure_class="runtime", **namespace))
            self.assertFalse(supervision._pending(supervision._rows(jobs), ["att-extinct"], jobs))


class DeferredCollectorBoundaryTest(unittest.TestCase):
    def test_deferred_projection_uses_deadline_backoff_and_never_replaces(self):
        from dispatch_completion_join import CompletionDeferred
        now = [100.0]
        def pause(seconds):
            now[0] += seconds
        join, replace = mock.Mock(side_effect=CompletionDeferred("termination-pending")), mock.Mock()
        with mock.patch.object(supervision.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(supervision.time, "sleep", side_effect=pause) as sleep, \
             mock.patch.object(supervision, "materialize", return_value=[]):
            result = supervision.wait_for_batch(join=join, attempts={"att-a", "att-b"}, jobs=Path("/unused"),
                deadline=105.0, replacement_checkpoint=replace)
        self.assertEqual(result["state"], "watch-expired")
        self.assertEqual(join.call_args_list, [mock.call({"att-a", "att-b"}), mock.call({"att-a", "att-b"})])
        sleep.assert_called_once_with(5.0)
        replace.assert_not_called()

    def test_deferred_projection_obeys_stop_and_replays_same_batch(self):
        from dispatch_completion_join import CompletionDeferred
        with mock.patch.object(supervision.time, "sleep"), mock.patch.object(supervision, "materialize", return_value=[]):
            stopped = supervision.wait_for_batch(join=mock.Mock(side_effect=CompletionDeferred("termination-pending")),
                attempts={"att-a"}, jobs=Path("/unused"), stop_check=lambda: "receiver-unavailable")
            join = mock.Mock(side_effect=[CompletionDeferred("termination-pending"), {"state": "ready", "children": [{"attempt_id": "att-a"}]}])
            settled = supervision.wait_for_batch(join=join, attempts={"att-a"}, jobs=Path("/unused"))
        self.assertEqual(stopped["reason"], "receiver-unavailable")
        self.assertEqual(settled["children"], [{"attempt_id": "att-a"}])
        self.assertEqual(join.call_count, 2)


if __name__ == "__main__":
    unittest.main()
