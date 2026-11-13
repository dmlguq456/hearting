#!/usr/bin/env python3
"""Responsibility contract falsifiers: no model/network credentials used."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
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
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch_attempt_policy as policy
import dispatch_contract as contract
import dispatch_supervision as supervision
import dispatch_pending_delivery as pending
import dispatch_session_sweep as sweep
from split_module_fixture import patch_split

CURRENT = {"attempt_schema_version": "2", "dispatch_depth": "2", "transport": "headless",
           "execution_surface": "registered-headless", "registered_worker": "1",
           "fallback_hop": "same-harness-headless", "route_id": "rt-policy", "route_node": "test",
           "parent_attempt_id": "att-owner", "pid_observer_ns": contract.process_namespace_identity()}


def row(attempt, *, status="open", **metadata):
    meta = {**CURRENT, "attempt_id": attempt, **metadata}
    return f"2026-09-11T00:00:00Z\t{status}\t/r\t/w\ttest\t" + ",".join(f"{k}={v}" for k,v in meta.items()) + "\n"


class SupervisorExitedSettlementTest(unittest.TestCase):
    """2026-11-10: six PASS owners whose supervisor exit reached the parent as "needs attention"
    minutes before their own completion (att-37f6788f, att-dc522c7d, att-6070b0f5, att-466d33ea)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name) / "jobs.log"

    def receipt(self, *attempts):
        return {"schema_version": 1, "kind": supervision.KIND, "state": "attention",
                "owner_attempt_id": attempts[0], "monitored_attempt_ids": list(attempts),
                "recipient_thread_id": "parent", "sealed_batch_id": "", "job_registry": str(self.jobs),
                "reason": "supervisor-exited", "obligation_revision": "",
                "responsible": "supervision-controller", "required_action": "inspect-recovery",
                "pending_delivery_id": "delivery-x"}

    def render(self, rows, process_state, reason="supervisor-exited", process_reason="attempt-descendant-live"):
        from types import SimpleNamespace
        self.jobs.write_text("".join(row(aid, status=status, **{k: v for k, v in meta.items() if k != "attempt_id"})
                                     for aid, (status, meta) in rows.items()))
        receipt = {**self.receipt(*sorted(rows)), "reason": reason, "owner_attempt_id": next(iter(rows))}
        with patch_split(contract, "observed_attempt_liveness",
                         return_value=SimpleNamespace(process_state=process_state, process_reason=process_reason)), \
             mock.patch("codex_dispatch_terminal.terminal_envelope_observed", return_value=False):
            return supervision.render_text(receipt)

    def owner(self, harness, **extra):
        return {**CURRENT, "parent_attempt_id": "-", "worker_type": "owner", "harness": harness,
                "workflow_completion": "runtime-v1", "failure_class": "pass", "note": "completed-supervisor",
                "parent_sid": "parent", **extra}

    def test_a_settlement_past_the_ceiling_is_the_attention_notice_in_any_harness(self):
        """RV-PR1559-1112: the watcher records a PASS owner's settlement-only exit only past
        SETTLING_NOTICE_SECONDS, so that one notice -- an undelivered completion, an incomplete
        cleanup proof, or a live descendant whose environment cannot be read -- is attention,
        never "not a blockage"."""
        for harness in ("claude", "codex", "opencode"):
            for process_state, process_reason in [("unverifiable", "post-exit-receipt-incomplete"),
                                                  ("unverifiable", "attempt-descendant-unverifiable"),
                                                  ("quiescent", "governed-process-group-drained")]:
                with self.subTest(harness=harness, process=process_reason):
                    text = self.render({"att-owner": ("done", self.owner(harness, attempt_id="att-owner"))},
                                       process_state, process_reason=process_reason)
                    self.assertTrue(text.startswith("Hearting supervision needs attention."), text)
                    self.assertIn("Explain the blockage", text)
                    self.assertIn("dispatch-registry.py reconcile --jobs", text)
                    for claim in ("not a blockage", "Do not report this to the user as blocked"):
                        self.assertNotIn(claim, text)
        for status, extra in (("open", {}), ("done", {"failure_class": "fail", "note": "dead-runtime"})):
            with self.subTest(status=status, **extra):   # no committed PASS: the attention text alone
                text = self.render({"att-owner": (status, self.owner("claude", attempt_id="att-owner", **extra))},
                                   "unverifiable", process_reason="post-exit-receipt-incomplete")
                self.assertTrue(text.startswith("Hearting supervision needs attention."), text)
                self.assertNotIn("not a blockage", text)

    def test_a_pass_owner_with_a_running_process_is_neither_blocked_nor_called_settlement(self):
        """RV-PR1444-1110 F1: a live tagged descendant may be work the worker started, not a helper."""
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                text = self.render({"att-owner": ("done", self.owner(harness, attempt_id="att-owner"))}, "live")
                self.assertIn("not a blockage, and not finished either", text)
                self.assertIn("Do not report the work as finished while it runs", text)
                for claim in ("only Hearting's own settlement", "do not restart, retry, cancel",
                              "Explain the blockage"):
                    self.assertNotIn(claim, text)

    def test_a_real_running_descendant_is_named_by_pid_before_any_residue_seal(self):
        """No liveness mock: a real process tagged with the attempt keeps the notice from settling."""
        self.jobs.write_text(row("att-owner", status="done", **{k: v for k, v in self.owner("claude").items()}))
        receipt = self.receipt("att-owner")
        work = subprocess.Popen(["sleep", "60"], env={**os.environ, "AGENT_DISPATCH_ATTEMPT_ID": "att-owner"},
                                start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while supervision.settling_after_pass(receipt) != [("att-owner", "wait")] and time.monotonic() < deadline:
                time.sleep(0.05)
            text = supervision.render_text(receipt)
            self.assertIn("not a blockage, and not finished either", text)
            self.assertIn(f"pid {work.pid}", text)
            self.assertNotIn("only Hearting's own settlement", text)
            self.assertNotIn("Explain the blockage", text)
        finally:
            work.kill()
            work.wait()

    def test_a_real_stop_or_unknown_evidence_keeps_the_attention_notice(self):
        cases = {
            "blocked-owner": ("done", {"failure_class": "blocked", "note": "dead-worker-blocked"}, "quiescent"),
            "died-without-result": ("done", {"failure_class": "fail", "note": "dead-runtime"}, "quiescent"),
            "no-verdict": ("done", {"failure_class": "", "note": "dead-silent"}, "quiescent"),
            "row-still-open": ("open", {}, "quiescent"),
            "process-unverifiable": ("done", {}, "unverifiable"),
            "evidence-conflict": ("done", {"terminal_conflict": "1", "conflicting_failure_class": "fail"}, "quiescent"),
        }
        for name, (status, extra, process) in cases.items():
            with self.subTest(case=name):
                text = self.render({"att-owner": (status, self.owner("codex", attempt_id="att-owner", **extra))}, process)
                self.assertTrue(text.startswith("Hearting supervision needs attention."), text)
                self.assertIn("Explain the blockage", text)

    def test_one_unsettled_attempt_or_another_reason_keeps_the_attention_notice(self):
        rows = {"att-owner": ("done", self.owner("claude", attempt_id="att-owner")),
                "att-child": ("done", {**self.owner("claude", attempt_id="att-child", failure_class="fail",
                                                   note="dead-runtime"), "parent_attempt_id": "att-owner"})}
        self.assertIn("Explain the blockage", self.render(rows, "quiescent"))
        only = {"att-owner": ("done", self.owner("claude", attempt_id="att-owner"))}
        closure = self.render(only, "quiescent", reason="closure-blocked")   # keeps its own wording
        self.assertNotIn("settlement in progress", closure)
        self.assertIn("Explain this to the user", closure)
        self.assertEqual(supervision.settling_after_pass({**self.receipt("att-gone")}), [])   # unreadable row


class ResponsibilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.log"

    def test_transient_capacity_wait_is_exact_attempt_bounded_and_superseded_by_next_turn(self):
        log = self.root / "owner.jsonl"
        start = {"type": "dispatch.supervisor.transient-wait.started",
                 "parent_attempt_id": "att-owner", "thread_id": "thread-a",
                 "turn_id": "turn-a", "reason_code": "transient-capacity",
                 "ordinal": 1, "deadline_monotonic": 160.0}
        log.write_text(json.dumps(start) + "\n")
        metadata = {"harness": "codex", "log_file": str(log)}
        with mock.patch.object(supervision.time, "monotonic", return_value=100.0):
            self.assertTrue(supervision._transient_retry_wait_active(metadata, "att-owner"))
            self.assertFalse(supervision._transient_retry_wait_active(metadata, "att-other"))
        with mock.patch.object(supervision.time, "monotonic", return_value=161.0):
            self.assertFalse(supervision._transient_retry_wait_active(metadata, "att-owner"))
        log.write_text(json.dumps(start) + "\n" + json.dumps({
            "type": "dispatch.supervisor.turn.started", "parent_attempt_id": "att-owner",
            "thread_id": "thread-a", "turn_id": "turn-b"}) + "\n")
        with mock.patch.object(supervision.time, "monotonic", return_value=100.0):
            self.assertFalse(supervision._transient_retry_wait_active(metadata, "att-owner"))

    def test_claude_and_opencode_retry_waits_are_read_by_their_turn_ordinal(self):
        log = self.root / "owner.jsonl"
        start = {"type": "dispatch.supervisor.transient-wait.started",
                 "parent_attempt_id": "att-owner", "turn_ordinal": 3,
                 "reason_code": "transient-edge-block", "ordinal": 1, "deadline_monotonic": 160.0}
        for harness in ("claude", "opencode"):
            with self.subTest(harness=harness):
                metadata = {"harness": harness, "log_file": str(log)}
                with mock.patch.object(supervision.time, "monotonic", return_value=100.0):
                    log.write_text(json.dumps(start) + "\n")
                    self.assertTrue(supervision._transient_retry_wait_active(metadata, "att-owner"))
                    self.assertFalse(supervision._transient_retry_wait_active(metadata, "att-other"))
                    for changed in ({"turn_ordinal": None}, {"reason_code": "transient-capacity"},
                                    {"turn_ordinal": None, "thread_id": "t", "turn_id": "u"}):
                        log.write_text(json.dumps({**start, **changed}) + "\n")
                        self.assertFalse(supervision._transient_retry_wait_active(metadata, "att-owner"))
                    for after in ({"type": "dispatch.supervisor.transient-wait.ended",
                                   "parent_attempt_id": "att-owner", "turn_ordinal": 3},
                                  {"type": "dispatch.supervisor.turn-started",
                                   "parent_attempt_id": "att-owner", "turn_ordinal": 4}):
                        log.write_text(json.dumps(start) + "\n" + json.dumps(after) + "\n")
                        self.assertFalse(supervision._transient_retry_wait_active(metadata, "att-owner"))
                    # Another turn's end does not close this wait.
                    log.write_text(json.dumps(start) + "\n" + json.dumps({
                        "type": "dispatch.supervisor.transient-wait.ended",
                        "parent_attempt_id": "att-owner", "turn_ordinal": 2}) + "\n")
                    self.assertTrue(supervision._transient_retry_wait_active(metadata, "att-owner"))
                with mock.patch.object(supervision.time, "monotonic", return_value=161.0):
                    self.assertFalse(supervision._transient_retry_wait_active(metadata, "att-owner"))

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
             patch_split(contract, "observed_attempt_liveness",
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

    def test_attempt_stall_is_not_a_parent_start_or_retry_authorization(self):
        record = {"receipt": {"kind": supervision.KIND, "reason": "attempt-stalled",
                               "owner_attempt_id": "att-owner",
                               "job_registry": str(self.jobs)}}
        arm = mock.Mock(side_effect=AssertionError("stall advisory must not arm"))
        self.assertIsNone(supervision.continue_for_parent(
            record, session_id="parent", recipient_kind="codex-managed-gateway", arm=arm))
        arm.assert_not_called()

    def test_stall_notice_explains_the_observation_with_read_only_next_command(self):
        metadata = {**CURRENT, "attempt_id": "att-worker", "parent_sid": "parent",
                    "parent_completion_delivery": "codex-managed-gateway"}
        self.jobs.write_text(row("att-worker", **metadata))
        receipt = {"schema_version": 1, "kind": supervision.KIND, "state": "attention",
            "owner_attempt_id": "att-worker", "monitored_attempt_ids": ["att-worker"],
            "recipient_thread_id": "parent", "sealed_batch_id": "",
            "job_registry": str(self.jobs), "reason": "attempt-stalled",
            "obligation_revision": "revision", "responsible": "supervision-controller",
            "required_action": "inspect-recovery", "pending_delivery_id": "delivery-stall"}
        observation = {"state": "stalled", "reason": "no-comparable-progress",
                       "quiet_windows": 2, "last_progress_at": 10.0}
        with mock.patch.object(supervision, "_rows", return_value={
                "att-worker": ("open", metadata)}), \
             mock.patch.object(supervision, "update_attempt_stall_observation",
                               return_value=observation), \
             mock.patch("dispatch_replacement._replacement_in_flight", return_value=False), \
             mock.patch("dispatch_replacement._plain_retry_successor", return_value=False), \
             mock.patch("route_parent_close.row_requested", return_value=False):
            text = supervision.render_text(receipt)
        self.assertIn("no comparable progress for 2 watchdog windows", text)
        self.assertIn("dispatch-registry.py reconcile --jobs", text)
        self.assertNotIn("start --route", text)
        self.assertIn("did not start, retry, signal, approve", text)

    def test_current_marker_or_successor_retires_an_older_stall_episode(self):
        route_file = self.root / "route.json"
        marker = self.root / "execute.json"
        route_file.write_text(json.dumps({"route_id": "rt-policy", "route_hash": "route-hash",
                                          "nodes": [{"id": "test"}]}))
        marker.write_text(json.dumps({"attempt_id": "att-worker"}))
        metadata = {**CURRENT, "attempt_id": "att-worker", "route_file": str(route_file),
                    "route_hash": "route-hash", "completion_marker": str(marker)}
        self.jobs.write_text(row("att-worker", **metadata))
        with mock.patch("dispatch_replacement._replacement_in_flight", return_value=False), \
             mock.patch("dispatch_replacement._plain_retry_successor", return_value=False), \
             patch_split(contract, "completion_marker_is_current", return_value=True):
            self.assertTrue(supervision._attempt_stall_superseded(self.jobs, "att-worker", metadata))
        with mock.patch("dispatch_replacement._replacement_in_flight", return_value=True), \
             mock.patch("dispatch_replacement._plain_retry_successor", return_value=False):
            self.assertTrue(supervision._attempt_stall_superseded(self.jobs, "att-worker", metadata))

    def test_effective_parent_receives_actual_needs_attention_reason_and_existing_command(self):
        import route_identity
        route = {"route_id": "rt-attention", "artifact_root": str(self.root),
                 "nodes": [{"id": "execute", "depends_on": []}]}
        route["route_hash"] = route_identity.route_hash(route)
        route_file = self.root / "route.json"
        route_file.write_text(json.dumps(route), encoding="utf-8")
        metadata = {**CURRENT, "attempt_id": "att-worker", "route_id": route["route_id"],
            "route_hash": route["route_hash"], "route_node": "execute",
            "route_file": str(route_file), "parent_sid": "parent",
            "parent_harness": "codex", "parent_completion_delivery": "codex-managed-gateway",
            "parent_attempt_id": "-"}
        self.jobs.write_text(row("att-worker", **metadata), encoding="utf-8")
        receipt_path = self.root / "start-receipts"
        receipt_path.mkdir()
        key = hashlib.sha256(b"codex\0parent").hexdigest()
        command = "python3 utilities/capability-route.py start --route /safe/route.json"
        (receipt_path / (key + ".json")).write_text(json.dumps({
            "schema": "start_receipt_v1", "id": "receipt", "saved_at": time.time(),
            "harness": "codex", "session_id": "parent",
            "receipt": {"state": "needs-attention", "route_id": route["route_id"],
                        "reason": "work-parent-recovery-required",
                        "required_action": "resume-route", "resume_command": command},
        }), encoding="utf-8")
        execution = contract.AttemptExecutionObservation("active", "runtime-command-live")
        with mock.patch.object(contract, "attempt_execution_observation", return_value=execution), \
             mock.patch("peer_obligations.ensure_normal_runner"):
            observation = supervision.update_attempt_stall_observation(
                self.jobs, "att-worker", now=time.time(),
                row=("open", metadata), worktree=str(self.root))
            notices = supervision.materialize(self.jobs, {"att-worker"}, reason="attempt-stalled")
        self.assertEqual(observation["reason"], "route-needs-attention")
        self.assertEqual(observation["route_attention"]["reason"],
                         "work-parent-recovery-required")
        text = supervision.render_text(notices[0]["receipt"])
        self.assertIn("reason=work-parent-recovery-required", text)
        self.assertIn("required_action=resume-route", text)
        self.assertIn(command, text)
        self.assertIn("not human approval", text)

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

    def test_output_invalid_notice_does_not_promise_automatic_retry(self):
        # (also: renders the exact refusal late, without a finish requirement)
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
        with mock.patch.object(dispatch_terminal_commit, "recorded_owner_completion_notice", return_value=diagnosis), \
             mock.patch.object(dispatch_terminal_commit, "inspect_owner_completion", side_effect=AssertionError("heavy inspect")):
            text = supervision.render_text(receipt)
        self.assertIn("output-invalid", text)
        self.assertIn("artifacts/bad|name.txt", text)
        self.assertNotIn("ignored", text)
        self.assertIn("event.json", text)
        self.assertIn("Correct the reported locator", text)
        self.assertIn("The next completion join judges closure again", text)
        self.assertNotIn("retries closure automatically", text)   # no promise the runtime does not keep
        self.assertIn("ead-only diagnosis", text)
        self.assertNotIn(" finish ", text)
        self.assertEqual(supervision.receipt_digest(receipt), digest)

    def test_completion_pending_notice_never_promises_a_retry_for_a_held_refusal(self):
        import dispatch_terminal_commit
        self._notice_rows()
        receipt = supervision.materialize(
            self.jobs, {"att-child"}, reason="workflow-completion-pending")[0]["receipt"]
        diagnosis = {"state": "closure-pending", "checkpoint": "route-closed",
                     "last_refusal": {
                         "reason": "producer-finalize-failed",
                         "detail": "cycle-route-binding-mismatch:superseded-route: rt-a",
                         "evidence": "sha256:" + "a" * 64,
                     }}
        with mock.patch.object(dispatch_terminal_commit,
                               "recorded_owner_completion_notice", return_value=diagnosis):
            text = supervision.render_text(receipt)
        self.assertIn("not retried automatically on the same evidence", text)
        self.assertIn("a later join or finish judges it again only after the route lineage "
                      "or the installed runtime changes", text)
        self.assertNotIn("retries when possible", text)
        self.assertNotIn("retries closure automatically", text)

    def test_completion_pending_notice_keeps_the_retry_wording_for_an_old_refusal_record(self):
        import dispatch_terminal_commit
        self._notice_rows()
        receipt = supervision.materialize(
            self.jobs, {"att-child"}, reason="workflow-completion-pending")[0]["receipt"]
        # The same deterministic prefix without evidence is an old record: the
        # fixed code judges it again, so the notice must not promise otherwise.
        old = {"state": "closure-pending", "checkpoint": "route-closed",
               "last_refusal": {"reason": "producer-finalize-failed",
                                "detail": "cycle-route-binding-mismatch:lineage-fork: rt-a:rt-b,rt-c"}}
        with mock.patch.object(dispatch_terminal_commit,
                               "recorded_owner_completion_notice", return_value=old):
            text = supervision.render_text(receipt)
        self.assertIn("retries when possible", text)
        self.assertNotIn("not retried automatically on the same evidence", text)

    def test_closure_blocked_notice_names_the_held_refusal_without_marker_repair_advice(self):
        import dispatch_terminal_commit
        self._notice_rows()
        receipt = supervision.materialize(
            self.jobs, {"att-child"}, reason="closure-blocked")[0]["receipt"]
        held = {"state": "closure-pending", "checkpoint": "route-closed",
                "last_refusal": {
                    "reason": "producer-finalize-failed",
                    "detail": "completion-route-hash-mismatch: rt-a",
                    "evidence": "sha256:" + "b" * 64,
                }}
        with mock.patch.object(dispatch_terminal_commit,
                               "recorded_owner_completion_notice", return_value=held):
            text = supervision.render_text(receipt)
        self.assertIn("holds a deterministic producer finalize refusal", text)
        self.assertIn("not retried automatically on the same evidence", text)
        self.assertNotIn("fresh dispatch of the same route node", text)
        self.assertNotIn("manual repair of the marker/evidence", text)
        # The existing gate-blocked wording is unchanged for a non-held reason.
        gate = {"state": "closure-pending", "checkpoint": "route-closed",
                "last_refusal": {"reason": "completion-attempt-not-current", "detail": ""}}
        with mock.patch.object(dispatch_terminal_commit,
                               "recorded_owner_completion_notice", return_value=gate):
            gate_text = supervision.render_text(receipt)
        self.assertIn("a later attempt already claimed this route node", gate_text)
        self.assertIn("fresh dispatch of the same route node", gate_text)

    def test_a_mixed_closure_blocked_batch_keeps_the_gate_blocked_wording(self):
        held = {"attempt_id": "att-held", "reason": "closure-blocked:producer-finalize-refused"}
        gate = {"attempt_id": "att-gate", "reason": "closure-blocked:completion-attempt-not-current"}
        # `render_text` chooses the gate-blocked wording exactly when this
        # predicate is false, so a mixed batch keeps the gate child's advice.
        self.assertFalse(supervision._held_finalize_refusal({"children": [held, gate]}))
        self.assertTrue(supervision._held_finalize_refusal({"children": [held]}))

    def test_notice_observation_leaves_full_controller_pending_observation_intact(self):
        import dispatch_notice_state
        from types import SimpleNamespace
        meta = {**CURRENT, "attempt_id": "att-owner", "workflow_completion": "runtime-v1"}
        rows = {"att-owner": ("done", meta)}
        with mock.patch.object(supervision, "_settlement_decision", return_value=(None, SimpleNamespace(action="advance"))), \
             mock.patch("route_parent_close.row_requested", return_value=False), \
             mock.patch("dispatch_terminal_commit.recorded_owner_completion_notice", return_value={"state": "unknown"}), \
             mock.patch("dispatch_terminal_commit.owner_completion_pending", return_value=True) as full:
            self.jobs.write_text("")
            self.assertTrue(supervision._pending(rows, ["att-owner"], self.jobs))
            full.assert_called_once()
            full.reset_mock()
            self.assertTrue(supervision._pending(rows, ["att-owner"], self.jobs,
                notice_context=dispatch_notice_state.NoticeReadContext()))
            full.assert_not_called()

    def test_reused_receipt_never_reuses_an_unmonitored_siblings_recipient_authority(self):
        from dispatch_notice_state import NoticeReadContext
        from types import SimpleNamespace
        self.jobs.write_text("")
        metadata = {aid: {"attempt_id": aid, "parent_sid": "parent", "delivery_id": "delivery-result",
                         "parent_completion_delivery": "claude-parent-runtime"} for aid in ("att-owner", "att-sibling")}
        completed = {"parent_attempt_id": "-", "children": [
            {"attempt_id": aid, "status": "done", "readiness": "ready", "required_action": "advance-completed"}
            for aid in metadata]}
        receipt = {"job_registry": str(self.jobs), "monitored_attempt_ids": ["att-owner"], "recipient_thread_id": "parent"}
        targets = {aid: "parent" for aid in metadata}
        context = NoticeReadContext()
        with mock.patch("dispatch_completion_join.current_attempt_row", side_effect=lambda _jobs, aid: SimpleNamespace(metadata=metadata[aid])), \
             mock.patch("dispatch_seat_handover.effective_parent", side_effect=lambda meta, _jobs: targets[meta["attempt_id"]]), \
             mock.patch.object(pending, "read", return_value={"receipt": completed}), \
             mock.patch("dispatch_completion_join.current_batch_receipt_projection", return_value=completed) as projection:
            self.assertIsNotNone(supervision._recorded_completion_result(receipt, require_ack=False, notice_context=context))
            self.assertIsNotNone(supervision._recorded_completion_result(receipt, require_ack=False, notice_context=context))
            projection.assert_called_once()
            targets["att-sibling"] = "other-parent"
            self.assertIsNone(supervision._recorded_completion_result(receipt, require_ack=False, notice_context=context))
            projection.assert_called_once()

    def test_display_recipients_share_only_the_current_pass_lineage_context(self):
        from types import SimpleNamespace
        self.jobs.write_text('')
        receipt = dict(job_registry=str(self.jobs), monitored_attempt_ids=['one', 'two'],
                       recipient_thread_id='parent')
        def row(_jobs, attempt):
            return SimpleNamespace(metadata=dict(attempt_id=attempt, parent_sid='parent',
                                                 delivery_id='delivery'))
        with mock.patch('dispatch_completion_join.current_attempt_row', side_effect=row), \
                mock.patch('dispatch_seat_handover.effective_parent', return_value='parent') as recipient, \
                mock.patch.object(pending, 'read', return_value=None):
            with supervision.route_attention_read_scope():
                first = supervision.passive_read_context()
                supervision._recorded_completion_result(receipt, require_ack=False)
                self.assertEqual([call.kwargs for call in recipient.call_args_list],
                                 [{'read_context': first}] * 2)
            recipient.reset_mock()
            with supervision.route_attention_read_scope():
                second = supervision.passive_read_context()
                self.assertIsNot(first, second)
                supervision._recorded_completion_result(receipt, require_ack=False)
                self.assertEqual([call.kwargs for call in recipient.call_args_list],
                                 [{'read_context': second}] * 2)
            recipient.reset_mock()
            supervision._recorded_completion_result(receipt, require_ack=False)
            self.assertEqual([call.kwargs for call in recipient.call_args_list], [{}, {}])

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

    def _conflicted_child(self, attempt="att-child", **metadata):
        """A closed child holding a pass while a later failure contradicts it."""
        lines = self.jobs.read_text().splitlines()
        index = next(i for i, line in enumerate(lines) if f"attempt_id={attempt}," in line + ",")
        lines[index] = row(attempt, status="done", note="completed-marker", failure_class="pass",
                           launch_outcome="never-launched", **metadata).strip()
        self.jobs.write_text("\n".join(lines) + "\n")
        self.assertEqual(contract.reconcile_attempt_terminal(self.jobs, attempt, "dead-worker-fail",
            evidence={"failure_class": "fail", "classifier_source": "supervisor-terminal-v1"}),
            "terminal-conflict")

    def _settle(self, attempts, rounds):
        """Run the final settlement wait; a wait still held after `rounds` joins stops the test."""
        calls = []

        def join(_monitored):
            calls.append(1)
            if len(calls) > rounds:
                raise supervision.StopWaiting("still-waiting")
            return {"state": "timeout"}

        with mock.patch.object(supervision.time, "sleep"):
            try:
                supervision.wait_for_child_settlement(
                    jobs=self.jobs, attempts=set(attempts), parent_attempt_id="att-owner",
                    join=join, reconcile=lambda _monitored: None, emit=lambda _event: None)
            except supervision.StopWaiting:
                return False, len(calls)
        return True, len(calls)

    def test_final_settlement_ends_when_only_closed_quiescent_conflicts_remain(self):
        self._notice_rows()
        self._conflicted_child()
        finished, joins = self._settle({"att-child"}, rounds=20)
        self.assertTrue(finished)
        self.assertEqual(joins, 1)
        records = list(pending.record_directory(self.root, "parent-test").glob("delivery-*.json"))
        self.assertEqual([json.loads(record.read_text())["receipt"]["reason"] for record in records],
                         ["terminal-evidence-conflict"])

    def test_final_settlement_keeps_waiting_for_open_or_live_children(self):
        self._notice_rows()
        finished, _joins = self._settle({"att-child"}, rounds=5)
        self.assertFalse(finished, "an open child keeps the wait")
        live = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (live.kill(), live.wait()))
        raw = Path(f"/proc/{live.pid}/stat").read_text()
        start = raw[raw.rfind(")") + 2:].split()[19]
        self.jobs.write_text(row("att-owner", dispatch_depth="1", parent_attempt_id="", parent_sid="parent-test",
            parent_completion_delivery="codex-managed-gateway", route_node="__owner__",
            managed_sealed_batch_id="batch-test", pid="99999999", pid_start="1")
            + row("att-child", pid=str(live.pid), pid_start=start, pgid=str(live.pid)))
        self._conflicted_child(pid=str(live.pid), pid_start=start, pgid=str(live.pid))
        finished, _joins = self._settle({"att-child"}, rounds=5)
        self.assertFalse(finished, "a live process keeps the wait")

    def test_final_settlement_waits_while_any_child_is_still_open(self):
        self._notice_rows()
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write(row("att-other", pid="99999997", pid_start="1"))
        self._conflicted_child()
        finished, _joins = self._settle({"att-child", "att-other"}, rounds=5)
        self.assertFalse(finished)

    def test_final_settlement_ends_at_once_for_a_reviewed_conflict(self):
        self._notice_rows()
        self._conflicted_child()
        review = self.root / "review.md"
        review.write_text("Compared the recorded PASS and the late failure; retain the recorded result.\n")
        raw = self.jobs.read_text().splitlines()[1]
        contract.resolve_terminal_conflict(self.jobs, "att-child",
            expected_row_sha256=hashlib.sha256(raw.encode()).hexdigest(), review=review)
        with mock.patch.object(supervision, "materialize") as materialize:
            finished, joins = self._settle({"att-child"}, rounds=5)
        self.assertTrue(finished)
        self.assertEqual(joins, 0)
        materialize.assert_not_called()

    def test_owned_rows_select_owner_and_children_and_keep_the_duplicate_refusal(self):
        self._notice_rows()
        with self.jobs.open("a", encoding="utf-8") as handle:
            for index in range(20):
                handle.write(row(f"att-unrelated-{index}", parent_attempt_id="att-other"))
        rows = supervision._owned_rows(self.jobs, "att-owner")
        self.assertEqual(set(rows), {"att-owner", "att-child"})
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write(row("att-unrelated-3", parent_attempt_id="att-other"))
        with self.assertRaisesRegex(supervision.SupervisionError, "supervision-attempt-ambiguous"):
            supervision._owned_rows(self.jobs, "att-owner")
        with self.assertRaisesRegex(supervision.SupervisionError, "supervision-attempt-ambiguous"):
            supervision._rows(self.jobs, {"att-owner"}, require_unique=True)
        self.assertEqual(set(supervision._rows(self.jobs, {"att-owner"})), {"att-owner"})

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

        with patch_split(contract, "namespace_gone", return_value="extinct"), \
                patch_split(contract, "attempt_tagged_descendants", side_effect=probe):
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


class ConfirmedExecutionNoticeTest(unittest.TestCase):
    """C8: 대기(확인된 실행) is observed by the existing controller, never escalated."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.log"
        import route_identity
        route = {"route_id": "rt-confirmed", "artifact_root": str(self.root),
                 "nodes": [{"id": "execute", "depends_on": []}]}
        route["route_hash"] = route_identity.route_hash(route)
        route_file = self.root / "route.json"
        route_file.write_text(json.dumps(route), encoding="utf-8")
        binding = dict(route_file=str(route_file), route_hash=route["route_hash"], route_id=route["route_id"])
        self.owner_meta = dict(dispatch_depth="1", parent_attempt_id="", parent_sid="parent-test",
            parent_completion_delivery="claude-parent-runtime", worker_type="owner",
            owner_route_id=route["route_id"], owner_route_file=str(route_file),
            owner_route_hash=route["route_hash"], route_node="_owner",
            managed_sealed_batch_id="batch-test", pid="99999999", pid_start="1", route_id="")
        self.child_meta = dict(binding, route_node="execute", pid="99999998", pid_start="1")
        self.executions = {}

    def _rows(self, child_status="open", owner_status="open"):
        self.jobs.write_text(row("att-owner", status=owner_status, **self.owner_meta)
                             + row("att-child", status=child_status, **self.child_meta))

    def _execution(self, metadata):
        return self.executions[metadata["attempt_id"]]

    def _patched(self, decision=None):
        from types import SimpleNamespace
        stack = [mock.patch.object(contract, "attempt_execution_observation", side_effect=self._execution)]
        if decision is not None:
            stack.append(mock.patch.object(supervision, "_settlement_decision",
                return_value=(SimpleNamespace(process_state="live"), decision)))
        return stack

    def _live(self):
        from types import SimpleNamespace
        return SimpleNamespace(action="wait", reason="process-alive", outcome="pending")

    def _enter(self, stack):
        for item in stack:
            item.start()
            self.addCleanup(item.stop)

    def _pending(self, reason, aid="att-child", rows=None):
        rows = rows or supervision._rows(self.jobs, {aid})
        return supervision._pending(rows, [aid], self.jobs, reason)

    def test_live_child_with_recent_progress_is_not_a_join_deadline_attention(self):
        self._rows()
        self.executions["att-child"] = contract.AttemptExecutionObservation("active", "runtime-command-live")
        self._enter(self._patched(self._live()))
        self.assertFalse(self._pending("join-deadline"))
        self.assertFalse(self._pending("process-unverifiable"))
        # The watch itself records no join-deadline notice while the member is confirmed executing.
        join = mock.Mock(side_effect=[{"state": "timeout"}, {"state": "ready", "children": []}])
        with mock.patch.object(supervision.time, "sleep"), \
             mock.patch.object(supervision, "materialize", return_value=[]) as materialize:
            supervision.wait_for_batch(join=join, attempts={"att-child"}, jobs=self.jobs)
        materialize.assert_not_called()

    def test_live_process_without_progress_past_the_window_still_raises_attention(self):
        self._rows()
        self.executions["att-child"] = contract.AttemptExecutionObservation("active", "runtime-command-live")
        self._enter(self._patched(self._live()))
        start = time.time()
        supervision.update_attempt_stall_observation(self.jobs, "att-child", now=start)
        later = start + 2 * 300.0 + 1
        with mock.patch.object(supervision.time, "time", return_value=later):
            observation = supervision.update_attempt_stall_observation(
                self.jobs, "att-child", persist=False, now=later)
            self.assertEqual(observation["state"], "stalled")
            self.assertTrue(self._pending("join-deadline"))
            self.assertTrue(self._pending("process-unverifiable"))
        join = mock.Mock(side_effect=[{"state": "timeout"}, {"state": "ready", "children": []}])
        with mock.patch.object(supervision.time, "time", return_value=later), \
             mock.patch.object(supervision.time, "sleep"), \
             mock.patch.object(supervision, "materialize", return_value=[]) as materialize:
            supervision.wait_for_batch(join=join, attempts={"att-child"}, jobs=self.jobs)
        materialize.assert_called_once()

    def test_dead_process_without_terminal_evidence_still_raises_attention(self):
        from types import SimpleNamespace
        self._rows()
        self.executions["att-child"] = contract.AttemptExecutionObservation("absent", "attempt-tag-census-empty")
        recover = SimpleNamespace(action="recover", reason="process-unverifiable", outcome="pending")
        self._enter(self._patched(recover))
        self.assertTrue(self._pending("join-deadline"))
        self.assertTrue(self._pending("process-unverifiable"))

    def test_unobservable_execution_stays_pending_under_the_existing_controller(self):
        from types import SimpleNamespace
        self._rows()
        self.executions["att-child"] = contract.AttemptExecutionObservation(
            "unknown", "attempt-process-role-unclassified")
        recover = SimpleNamespace(action="recover", reason="process-unverifiable", outcome="pending")
        self._enter(self._patched(recover))
        self.assertTrue(self._pending("process-unverifiable"))

    def test_normal_end_with_the_process_still_draining_is_not_attention(self):
        from types import SimpleNamespace
        self._rows(child_status="done")
        draining = SimpleNamespace(action="wait", reason="registry-terminal-process-live", outcome="succeeded")
        self._enter(self._patched(draining))
        self.assertFalse(self._pending("join-deadline"))
        self.assertEqual(supervision.confirmed_execution_attempts(self.jobs, {"att-child"}), {"att-child"})
        # A finished row whose process cannot be proven is not claimed as draining.
        unknown = SimpleNamespace(action="recover", reason="process-unverifiable", outcome="succeeded")
        with mock.patch.object(supervision, "_settlement_decision",
                               return_value=(SimpleNamespace(process_state="unknown"), unknown)):
            self.assertTrue(self._pending("process-unverifiable"))
            self.assertEqual(supervision.confirmed_execution_attempts(self.jobs, {"att-child"}), set())

    def test_other_reasons_keep_their_existing_meaning_for_a_confirmed_member(self):
        self._rows()
        self.executions["att-child"] = contract.AttemptExecutionObservation("active", "runtime-command-live")
        self._enter(self._patched(self._live()))
        self.assertTrue(self._pending("supervisor-exited"))
        self.assertTrue(self._pending("closure-blocked"))

    def test_owner_waiting_on_a_live_child_counts_the_childs_execution_as_its_progress(self):
        self._rows()
        self.executions["att-owner"] = contract.AttemptExecutionObservation(
            "unknown", "attempt-process-role-unclassified")
        self.executions["att-child"] = contract.AttemptExecutionObservation("active", "runtime-command-live")
        self._enter(self._patched())
        observation = supervision.update_attempt_stall_observation(
            self.jobs, "att-owner", persist=False)
        self.assertEqual((observation["state"], observation["reason"]), ("active", "owned-child-executing"))
        self.assertFalse(supervision._pending(supervision._rows(self.jobs, {"att-owner"}),
                                              ["att-owner"], self.jobs, "attempt-stalled"))

    def test_owner_without_a_confirmed_child_keeps_its_unverified_classification(self):
        self.executions["att-owner"] = contract.AttemptExecutionObservation(
            "unknown", "attempt-process-role-unclassified")
        self._enter(self._patched())
        for case, child_status, child_execution in (
                ("child-ended", "done", contract.AttemptExecutionObservation("absent", "attempt-tag-census-empty")),
                ("child-unobservable", "open", contract.AttemptExecutionObservation("unknown", "attempt-process-role-unclassified")),
                ("child-absent", "open", contract.AttemptExecutionObservation("absent", "attempt-tag-census-empty"))):
            with self.subTest(case=case):
                self._rows(child_status=child_status)
                self.executions["att-child"] = child_execution
                observation = supervision.update_attempt_stall_observation(
                    self.jobs, "att-owner", persist=False)
                self.assertNotEqual(observation["reason"], "owned-child-executing")
                self.assertIn(observation["state"], {"observing", "unknown"})

    def test_absent_owner_with_a_live_child_is_still_a_stall(self):
        self._rows()
        self.executions["att-owner"] = contract.AttemptExecutionObservation("absent", "attempt-tag-census-empty")
        self.executions["att-child"] = contract.AttemptExecutionObservation("active", "runtime-command-live")
        self._enter(self._patched())
        observation = supervision.update_attempt_stall_observation(
            self.jobs, "att-owner", persist=False)
        self.assertEqual((observation["state"], observation["reason"]), ("stalled", "execution-absent"))


class OwnerProgressAlarmTest(unittest.TestCase):
    """2026-11-13: three live owners got a stall notice while their work moved.

    08:05 an owner parked two hours on its own execute stage (commits, cycle writes)
    resumed into 24 quiet windows; 08:34 an owner parked on its own host-run resource
    read as unverified execution; a one-turn owner's long call hid behind a parallel
    sibling's result. A live owner with no trace and a dead owner still raise it.
    """

    DELIVERY = {"claude": "session-resume-supervised", "codex": "app-server-supervised",
                "opencode": "session-resume-supervised"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.executions = {}
        patcher = mock.patch.object(contract, "attempt_execution_observation",
                                    side_effect=lambda metadata: self.executions[metadata["attempt_id"]])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.t0 = 1_800_000_000.0

    def owner(self, harness, case=""):
        import route_identity
        root = Path(self.temp.name) / (harness + case)
        root.mkdir()
        self.root, self.jobs, self.log = root, root / "jobs.log", root / f"owner.{harness}.jsonl"
        self.log.write_text("", encoding="utf-8")
        route = {"route_id": "rt-alarm-" + harness, "artifact_root": str(root),
                 "nodes": [{"id": "execute", "depends_on": []}]}
        route["route_hash"] = route_identity.route_hash(route)
        route_file = root / "route.json"
        route_file.write_text(json.dumps(route), encoding="utf-8")
        self.aid = "att-alarm-" + harness + case
        self.owner_meta = dict(
            dispatch_depth="1", parent_attempt_id="", route_id="", route_node="_owner",
            parent_sid="parent-test", parent_completion_delivery="claude-parent-runtime",
            worker_type="owner", harness=harness, completion_delivery=self.DELIVERY[harness],
            owner_route_id=route["route_id"], owner_route_file=str(route_file),
            owner_route_hash=route["route_hash"], log_file=str(self.log),
            supervisor_lease="flock-v1", supervisor_lease_nonce="a" * 64,
            supervisor_lease_file=str(contract.supervisor_lease_path(self.jobs, self.aid)),
            pid="99999999", pid_start="1")
        self.child_meta = dict(route_file=str(route_file), route_id=route["route_id"],
                               route_hash=route["route_hash"], route_node="execute",
                               parent_attempt_id=self.aid, pid="99999998", pid_start="1")
        self.rows()

    def rows(self, child_status=None):
        text = row(self.aid, **self.owner_meta)
        if child_status:
            text += row("att-alarm-child", status=child_status, **self.child_meta)
        temporary = self.jobs.with_name(".jobs.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, self.jobs)

    def phase(self, phase):
        from dispatch_completion_join import write_supervisor_state
        write_supervisor_state(contract.dispatch_state_root(self.jobs) / "supervisor-state"
                               / f"{self.aid}.json", self.aid, set(), phase=phase)

    def runtime(self, state, reason, pid="4242"):
        identities = ((pid, "1", "claude"),) if state == "active" else ()
        self.executions[self.aid] = contract.AttemptExecutionObservation(state, reason, identities)

    def lease(self):
        return contract.hold_supervisor_lease(self.jobs, self.aid, self.owner_meta["supervisor_lease_file"])

    def tool_rows(self, harness, events):
        """One native row per (call id, phase), in each runtime's own event shape."""
        lines = []
        for identity, phase, message in events:
            if harness == "claude":
                kind = "assistant" if phase == "start" else "user"
                block = ({"type": "tool_use", "id": identity} if phase == "start"
                         else {"type": "tool_result", "tool_use_id": identity})
                lines.append({"type": kind, "message": {"id": message if kind == "assistant" else None,
                                                        "content": [block]}})
            elif harness == "codex":
                lines.append({"type": "item.started" if phase == "start" else "item.completed",
                              "item": {"type": "command_execution", "id": identity}})
            else:
                lines.append({"type": "tool_use", "part": {"type": "tool", "callID": identity,
                              "state": {"status": "running" if phase == "start" else "completed"}}})
        with self.log.open("a", encoding="utf-8") as handle:
            handle.writelines(json.dumps(line) + "\n" for line in lines)

    def alarms(self, now):
        """Run the existing observer pass at ``now``; return its attempt-stalled notices."""
        calls = []
        with mock.patch.object(supervision.time, "time", return_value=now), \
             mock.patch.object(supervision, "materialize",
                               side_effect=lambda jobs, ids, reason: calls.append(reason) or []):
            supervision.update_open_attempt_stall_observations(self.jobs)
        return calls.count("attempt-stalled")

    def state(self, now):
        return supervision.update_attempt_stall_observation(self.jobs, self.aid, persist=False, now=now)

    def test_a_owner_resuming_after_its_stage_worked_two_hours_gets_no_alarm(self):
        for harness in self.DELIVERY:
            with self.subTest(harness=harness):
                self.owner(harness)
                with self.lease():
                    self.phase("running-turn")
                    self.runtime("active", "runtime-command-live")
                    self.tool_rows(harness, [("call-plan", "start", "m1"), ("call-plan", "end", "m1")])
                    count = self.alarms(self.t0)
                    # The owner parks on its execute stage; the stage commits and writes the cycle.
                    self.phase("parked")
                    self.rows(child_status="open")
                    self.runtime("unknown", "attempt-process-role-unclassified")
                    self.executions["att-alarm-child"] = contract.AttemptExecutionObservation(
                        "active", "runtime-command-live", (("4343", "1", "claude"),))
                    for window in range(1, 25):
                        count += self.alarms(self.t0 + window * 300)
                    # The stage ends; the owner's next turn starts before its first tool row.
                    self.rows(child_status="done")
                    self.phase("running-turn")
                    self.runtime("active", "runtime-command-live", pid="4444")
                    count += self.alarms(self.t0 + 7472)
                    count += self.alarms(self.t0 + 7472 + 299)
                self.assertEqual(count, 0)

    def host_run(self, *, running=True, own_process=True, owner=None):
        """Arm the owner route's resource node on one run its runner records."""
        import resource_run_registry as RR
        from workflow_state import WorkflowLedger
        identity = RR.proc_identity(os.getpid()) if own_process else {
            "pid": 99999997, "starttime": "1", "command_hash": "0" * 64}
        registry = self.root / "cycle" / "resource-runs.json"
        registry.parent.mkdir(exist_ok=True)
        registry.write_text(json.dumps({"runs": {"host-run-r1": {
            "run_id": "host-run-r1", "resource_policy": "supervised-owner",
            "status": "running" if running else "succeeded",
            "pid": identity["pid"], "starttime": identity["starttime"],
            "command_hash": identity["command_hash"], "jobs": str(self.jobs.resolve()),
            "route": self.owner_meta["owner_route_file"], "node": "host-run",
            "parent_attempt_id": owner or self.aid,
            "owner_wait": {"parent_attempt_id": owner or self.aid, **({
                # Another owner's complete record on the same route: only a handoff could move it.
                "route_id": self.owner_meta["owner_route_id"],
                "route_hash": self.owner_meta["owner_route_hash"], "jobs": str(self.jobs.resolve()),
                "session_id": "sid-" + owner, "owner_pid": 99999996, "owner_start": "1"}
                if owner else {})}}}}), encoding="utf-8")
        if owner:
            with self.jobs.open("a", encoding="utf-8") as handle:
                handle.write(row(owner, status="done", **{**self.owner_meta, "pid": "99999996"}))
        armed = WorkflowLedger(self.owner_meta["owner_route_id"], self.owner_meta["owner_route_hash"],
                               jobs=self.jobs).root / "armed"
        armed.mkdir(parents=True, exist_ok=True)
        (armed / "host-run.json").write_text(json.dumps({
            "node": "host-run", "predecessor_kind": "resource", "predecessor_id": "host-run-r1",
            "resource_registry": str(registry), "route_file": self.owner_meta["owner_route_file"],
            "jobs": str(self.jobs.resolve())}), encoding="utf-8")

    def test_a_one_turn_owner_whose_long_call_hides_behind_a_sibling_result_gets_no_alarm(self):
        # The long call's own tool command, started for it, still runs: exact execution.
        for harness in self.DELIVERY:
            with self.subTest(harness=harness):
                self.owner(harness)
                with self.lease(), mock.patch.object(supervision, "_running_tool_command",
                                                     return_value=(self.t0 + 1, "sleep")):
                    self.phase("running-turn")
                    self.runtime("active", "runtime-command-live")
                    # Two parallel calls of one model message; the quick one returns first.
                    self.tool_rows(harness, [("call-long", "start", "m1"), ("call-quick", "start", "m1"),
                                             ("call-quick", "end", "m1")])
                    count = sum(self.alarms(self.t0 + window * 300) for window in range(0, 25))
                self.assertEqual(count, 0)

    def test_r2_a_call_whose_result_never_comes_alarms_after_the_quiet_windows(self):
        # RV-PR1592 R2: the long call's result never arrives and nothing else moves; the
        # runtime alone stays live. Before the fix the stale start record exempted it for days.
        for harness in self.DELIVERY:
            for running in (None, (self.t0 - 3600, "sleep")):
                with self.subTest(harness=harness, resident_shell=bool(running)):
                    self.owner(harness, "-resident" if running else "")
                    # A resident shell older than the call does not back the call either.
                    with self.lease(), mock.patch.object(supervision, "_running_tool_command",
                                                         return_value=running):
                        self.phase("running-turn")
                        self.runtime("active", "runtime-command-live")
                        self.tool_rows(harness, [("call-long", "start", "m1"), ("call-quick", "start", "m1"),
                                                 ("call-quick", "end", "m1")])
                        counts = [self.alarms(self.t0 + offset) for offset in (0, 601, 3600, 86400, 604800)]
                        observation = self.state(self.t0 + 604800)
                    self.assertEqual(counts, [0, 1, 1, 1, 1])
                    self.assertEqual((observation["state"], observation["reason"]),
                                     ("stalled", "no-comparable-progress"))
            # The runtime unseen and only an unclassified process (a supervisor wrapper) left:
            # the pinned record backs nothing past the windows; the existing unknown alarm follows.
            with self.subTest(harness=harness, execution="unclassified-only"):
                self.owner(harness, "-unclassified")
                with self.lease():
                    self.phase("running-turn")
                    self.executions[self.aid] = contract.AttemptExecutionObservation(
                        "unknown", "attempt-process-role-unclassified", (("4242", "1", "unclassified:sh"),))
                    self.tool_rows(harness, [("call-long", "start", "m1"), ("call-quick", "start", "m1"),
                                             ("call-quick", "end", "m1")])
                    counts = [self.alarms(self.t0 + offset) for offset in (0, 601, 3600, 86400, 604800)]
                    observation = self.state(self.t0 + 604800)
                self.assertEqual(counts, [0, 0, 1, 1, 1])
                self.assertEqual((observation["state"], observation["reason"]),
                                 ("unknown", "attempt-process-role-unclassified"))

    def test_b_owner_waiting_on_its_own_host_run_gets_no_alarm(self):
        for harness in self.DELIVERY:
            for execution in (("unknown", "attempt-process-role-unclassified"), ("absent", "carrier-only")):
                with self.subTest(harness=harness, execution=execution[1]):
                    self.owner(harness, "-" + execution[1])
                    with self.lease():
                        self.phase("running-turn")
                        self.runtime("active", "runtime-command-live")
                        self.tool_rows(harness, [("call-launch", "start", "m1"), ("call-launch", "end", "m1")])
                        count = self.alarms(self.t0)
                        # The resource runner owns the 2,400 s host run; the owner is parked on it.
                        self.host_run()
                        self.phase("parked")
                        self.runtime(*execution)
                        count += sum(self.alarms(self.t0 + window * 300) for window in range(1, 9))
                        observation = self.state(self.t0 + 2400)
                    self.assertEqual(count, 0)
                    self.assertEqual((observation["state"], observation["reason"]),
                                     ("active", "supervised-owner-parked"))

    def test_r1_parked_owner_with_nothing_running_and_a_stuck_supervisor_alarms(self):
        # RV-PR1592 R1: an old parked phase file and an exactly held lease, but the supervisor
        # sleeps and nothing it could wait on runs. Before the fix: no alarm, ever.
        cases = [("parked", "none"), ("deliverable", "none"), ("recovery", "none"),
                 ("parked", "child-not-executing"), ("parked", "run-process-gone"),
                 ("parked", "run-finished"), ("parked", "run-of-another-owner")]
        for harness in self.DELIVERY:
            for phase, target in cases:
                for execution in (("unknown", "attempt-process-role-unclassified"), ("absent", "carrier-only")):
                    with self.subTest(harness=harness, phase=phase, target=target, execution=execution[1]):
                        self.owner(harness, f"-{phase}-{target}-{execution[1]}")
                        if target == "child-not-executing":
                            self.rows(child_status="open")
                            self.executions["att-alarm-child"] = contract.AttemptExecutionObservation(
                                "absent", "attempt-tag-census-empty", ())
                        elif target.startswith("run-"):
                            self.host_run(own_process=target != "run-process-gone",
                                          running=target != "run-finished",
                                          owner="att-someone-else" if target == "run-of-another-owner" else None)
                        with self.lease():
                            self.phase(phase)
                            path = contract.dispatch_state_root(self.jobs) / "supervisor-state" / f"{self.aid}.json"
                            os.utime(path, (1, 1))
                            self.runtime(*execution)
                            counts = [self.alarms(self.t0 + offset) for offset in (0, 601, 3600, 86400, 604800)]
                            observation = self.state(self.t0 + 604800)
                        self.assertEqual(counts, [0, 1, 1, 1, 1])
                        self.assertIn(observation["state"], {"stalled", "unknown"})
                        self.assertNotEqual(observation["reason"], "supervised-owner-parked")

    def test_c_live_owner_without_any_trace_still_raises_the_alarm(self):
        for harness in self.DELIVERY:
            with self.subTest(harness=harness):
                self.owner(harness)
                with self.lease():
                    self.phase("running-turn")
                    self.runtime("active", "runtime-command-live")
                    self.tool_rows(harness, [("call-last", "start", "m1"), ("call-last", "end", "m1")])
                    self.assertEqual(self.alarms(self.t0), 0)
                    self.assertEqual(self.alarms(self.t0 + 2 * 300 + 1), 1)
                    observation = self.state(self.t0 + 2 * 300 + 1)
                self.assertEqual((observation["state"], observation["reason"]),
                                 ("stalled", "no-comparable-progress"))

    def test_d_dead_owner_still_raises_the_alarm(self):
        for harness in self.DELIVERY:
            with self.subTest(harness=harness):
                self.owner(harness)
                # Its last phase was parked, but no live supervisor holds the lease any more.
                self.phase("parked")
                self.runtime("absent", "attempt-tag-census-empty")
                self.assertEqual(self.alarms(self.t0), 1)
                observation = self.state(self.t0)
                self.assertEqual((observation["state"], observation["reason"]),
                                 ("stalled", "execution-absent"))

    def test_a_claude_owner_whose_receipt_log_caught_a_cli_warning_still_shows_its_tools(self):
        # 12:44/13:05/13:53: each turn's "Ignoring 7 permissions.allow ..." stderr line landed in
        # the receipt log, and the owner's own transcript stopped being the tool-progress source.
        from claude_project_dir import encode_project_dir
        self.owner("claude")
        cwd, home = self.root / "work", self.root / "home"
        cwd.mkdir()
        self.owner_meta["runtime_home"] = str(home)
        self.rows()
        transcript = home / "projects" / encode_project_dir(str(cwd)) / "sid-owner.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("", encoding="utf-8")
        warning = "Ignoring 7 permissions.allow entries from project settings\n"
        self.log.write_text(json.dumps({"type": "dispatch.supervisor.session", "session_id": "sid-owner",
                                        "cwd": str(cwd)}) + "\n"
                            + json.dumps({"type": "dispatch.supervisor.turn-started"}) + "\n" + warning,
                            encoding="utf-8")
        count = 0
        with self.lease():
            self.phase("running-turn")
            self.runtime("active", "runtime-command-live")
            for window in range(0, 12):
                with transcript.open("a", encoding="utf-8") as handle:
                    for phase in ("start", "end"):
                        kind = "assistant" if phase == "start" else "user"
                        block = ({"type": "tool_use", "id": f"call-{window}"} if phase == "start"
                                 else {"type": "tool_result", "tool_use_id": f"call-{window}"})
                        handle.write(json.dumps({"type": kind, "message": {
                            "id": f"m{window}" if kind == "assistant" else None, "content": [block]}}) + "\n")
                count += self.alarms(self.t0 + window * 300)
        self.assertEqual(count, 0)

    def test_r3_a_damaged_announcement_never_reads_another_sessions_tools(self):
        # RV-PR1592 R3: another process's valid announcement B landed first, then this owner's
        # own announcement A arrived damaged. Before the fix B's transcript became the source.
        from claude_project_dir import encode_project_dir
        self.owner("claude")
        cwd, home = self.root / "work", self.root / "home"
        cwd.mkdir()
        self.owner_meta["runtime_home"] = str(home)
        folder = home / "projects" / encode_project_dir(str(cwd))
        folder.mkdir(parents=True)
        for sid in ("sid-a", "sid-b"):
            (folder / f"{sid}.jsonl").write_text(json.dumps({"type": "assistant", "message": {
                "id": "m-" + sid, "content": [{"type": "tool_use", "id": "call-" + sid}]}}) + "\n",
                encoding="utf-8")
        other = json.dumps({"type": "dispatch.supervisor.session", "session_id": "sid-b", "cwd": str(cwd)})
        for damaged in ('stderr {"type":"dispatch.supervisor.session","session_id":"sid-a"}',
                        "dispatch.supervisor.session sid-a " + str(cwd),
                        '"session_id":"sid-a","cwd":"' + str(cwd) + '"'):
            with self.subTest(damaged=damaged):
                self.log.write_text(other + "\n" + damaged + "\n", encoding="utf-8")
                metadata = {**self.owner_meta, "log_file": str(self.log)}
                self.assertEqual(supervision._tool_event_source(metadata, self.log), self.log)
                self.assertIsNone(supervision.runtime_tool_progress(metadata))
        # A plain CLI warning after the exact announcement still keeps its own transcript.
        warning = ('Ignoring 7 permissions.allow entries from .claude/settings.json: this workspace has '
                   'not been trusted. Run Claude Code interactively here once and accept the trust '
                   'dialog, or set projects["/x"].hasTrustDialogAccepted: true in /h/.claude.json.')
        self.log.write_text(other + "\n" + warning + "\n", encoding="utf-8")
        self.assertEqual(supervision.runtime_tool_progress({**self.owner_meta, "log_file": str(self.log)}),
                         ["claude", "call-sid-b", "tool_use"])

    def test_a_new_model_message_settles_the_calls_of_the_previous_one(self):
        self.owner("claude")
        self.tool_rows("claude", [("call-a", "start", "m1"), ("call-b", "start", "m1"),
                                  ("call-b", "end", "m1")])
        metadata = {"log_file": str(self.log)}
        self.assertEqual(supervision.runtime_tool_progress(metadata), ["claude", "call-a", "tool_use"])
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "assistant", "message": {"id": "m2", "content": [
                {"type": "text", "text": "x"}]}}) + "\n")
        self.assertEqual(supervision.runtime_tool_progress(metadata), ["claude", "call-b", "tool_result"])
        self.owner("codex")
        self.tool_rows("codex", [("item-a", "start", ""), ("item-b", "start", ""), ("item-b", "end", "")])
        metadata = {"log_file": str(self.log)}
        self.assertEqual(supervision.runtime_tool_progress(metadata), ["codex", "item-a", "item.started"])
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "turn.completed"}) + "\n")
        self.assertEqual(supervision.runtime_tool_progress(metadata), ["codex", "item-b", "item.completed"])


    def retry_wait(self, harness, reason="transient-edge-block", attempt_id=None, deadline=1960.0):
        """The retry wait each supervisor writes before it resumes the same session."""
        event = {"type": "dispatch.supervisor.transient-wait.started",
                 "parent_attempt_id": attempt_id or self.aid, "reason_code": reason,
                 "ordinal": 5, "deadline_monotonic": deadline}
        event.update({"thread_id": "thread-rv", "turn_id": "turn-rv"} if harness == "codex"
                     else {"turn_ordinal": 6})
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    def test_e_owner_waiting_out_a_retry_reads_waiting_on_every_harness(self):
        # RV-PR1615: a live owner retrying a 403 edge page, and Codex's capacity wait, read
        # `stalled / no-comparable-progress` with one attempt-stalled notice after 601 s.
        cases = [("codex", "transient-capacity", "active"), ("codex", "transient-edge-block", "active"),
                 ("claude", "transient-edge-block", "active"), ("opencode", "transient-edge-block", "active"),
                 # Between turns only the supervisor carrier remains, and it reads unclassified.
                 ("claude", "transient-edge-block", "unknown"),
                 ("opencode", "transient-edge-block", "unknown")]
        for index, (harness, reason, runtime) in enumerate(cases):
            with self.subTest(harness=harness, reason=reason, runtime=runtime):
                self.owner(harness, case=f"-wait{index}")
                self.runtime(runtime, "runtime-command-live" if runtime == "active"
                             else "attempt-process-role-unclassified")
                self.retry_wait(harness, reason)
                with self.lease(), mock.patch.object(supervision.time, "monotonic", return_value=1000.0):
                    self.phase("running-turn")
                    self.assertTrue(supervision._transient_retry_wait_active(self.owner_meta, self.aid))
                    count = self.alarms(self.t0) + self.alarms(self.t0 + 601)
                    observation = self.state(self.t0 + 601)
                self.assertEqual(count, 0)
                self.assertEqual((observation["state"], observation["reason"]),
                                 ("waiting", "registered-transient-retry-backoff"))

    def test_e_a_retry_wait_never_hides_a_real_stop(self):
        for harness in self.DELIVERY:
            with self.subTest(harness=harness, case="deadline-passed"):
                # Observed through the whole wait, then no next turn: the windows count again.
                self.owner(harness, case="-late")
                self.runtime("active", "runtime-command-live")
                self.retry_wait(harness)
                counts = []
                with self.lease():
                    self.phase("running-turn")
                    for offset in (0, 300, 600, 900, 961, 1501):
                        with mock.patch.object(supervision.time, "monotonic",
                                               return_value=1000.0 + offset):
                            counts.append(self.alarms(self.t0 + offset))
                    with mock.patch.object(supervision.time, "monotonic", return_value=2501.0):
                        observation = self.state(self.t0 + 1501)
                self.assertEqual(counts, [0, 0, 0, 0, 0, 1])
                self.assertEqual((observation["state"], observation["reason"]),
                                 ("stalled", "no-comparable-progress"))
            for case in ("other-attempt", "dead-lease"):
                with self.subTest(harness=harness, case=case):
                    self.owner(harness, case="-" + case)
                    self.runtime("active", "runtime-command-live")
                    self.retry_wait(harness, attempt_id="att-alarm-other" if case == "other-attempt" else None)
                    with contextlib.ExitStack() as stack:
                        if case == "other-attempt":
                            stack.enter_context(self.lease())
                        stack.enter_context(mock.patch.object(supervision.time, "monotonic",
                                                              return_value=1000.0))
                        self.phase("running-turn")
                        count = self.alarms(self.t0) + self.alarms(self.t0 + 601)
                        observation = self.state(self.t0 + 601)
                    self.assertEqual(count, 1)
                    self.assertEqual((observation["state"], observation["reason"]),
                                     ("stalled", "no-comparable-progress"))

class StallNoticeRunningToolTest(unittest.TestCase):
    """2026-11-11 00:02: the notice for att-a9d2c7d7 read like a hang while its owner waited
    on its own `sleep 1500; grep ITER ...` measurement window."""

    CLAUDE = ("source /dev/null 2>/dev/null || true && eval 'sleep 60; grep ITER '\"'\"'log'\"'\"''"
              " < /dev/null && pwd -P >| /dev/null")

    def runtime(self, harness, script):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = Path(tmp.name) / harness
        runtime.write_text("#!" + sys.executable + "\nimport subprocess,sys,time\n"
                           "subprocess.Popen(['sleep','60'])\n"  # a long-lived non-shell child
                           + (f"subprocess.Popen(['bash','-c',{script!r}])\n" if script else "")
                           + "time.sleep(0.3); print('ready',flush=True); time.sleep(60)\n")
        runtime.chmod(0o755)
        aid = "att-tool-" + harness
        process = subprocess.Popen([str(runtime)], stdout=subprocess.PIPE, text=True, start_new_session=True,
                                   env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                        "AGENT_DISPATCH_ATTEMPT_ID": aid})
        def reap():
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            process.stdout.close()
        self.addCleanup(reap)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return {**CURRENT, "attempt_id": aid, "harness": harness, "worker_type": "owner",
                **contract.process_launch_identity(process.pid)}

    def notice(self, metadata, wire=False):
        receipt = {"schema_version": 1, "kind": supervision.KIND, "state": "attention",
            "owner_attempt_id": metadata["attempt_id"], "monitored_attempt_ids": [metadata["attempt_id"]],
            "recipient_thread_id": "parent", "sealed_batch_id": "", "job_registry": "/dev/null",
            "reason": "attempt-stalled", "obligation_revision": "revision",
            "responsible": "supervision-controller", "required_action": "inspect-recovery",
            "pending_delivery_id": "delivery-stall"}
        observation = {"state": "stalled", "reason": "no-comparable-progress",
                       "quiet_windows": 5, "last_progress_at": time.time() - 1544}
        with mock.patch.object(supervision, "_rows", return_value={metadata["attempt_id"]: ("open", metadata)}), \
             mock.patch.object(supervision, "update_attempt_stall_observation", return_value=observation), \
             mock.patch("dispatch_replacement._replacement_in_flight", return_value=False), \
             mock.patch("dispatch_replacement._plain_retry_successor", return_value=False), \
             mock.patch("route_parent_close.row_requested", return_value=False):
            if wire:
                return supervision.context(receipt, "delivery-stall")["additionalContext"][
                    "hearting-supervision"]["value"]
            return supervision.render_text(receipt)

    def test_live_tool_command_is_named_with_its_start_time_on_every_harness(self):
        for harness, script in (("claude", self.CLAUDE), ("codex", "sleep 60; grep ITER log"),
                                ("opencode", "sleep 60; grep ITER log")):
            with self.subTest(harness=harness):
                text = self.notice(self.runtime(harness, script))
                self.assertIn("no comparable progress for 5 watchdog windows", text)
                self.assertRegex(text, r"; a tool command started \d\d:\d\d is still running: sleep\.")
                self.assertNotIn("ITER", text)

    def test_only_the_program_name_reaches_the_parent_never_arguments(self):
        # The name is the kernel's for the shell's live child; the command text is never read.
        secret = "RV1492_FAKE_API_TOKEN_7b"
        for script in ("sleep 60; : --api-key " + secret,
                       "API_TOKEN=" + secret + " sleep 60; :",
                       "sleep 60 && : " + secret + "; : " + secret,
                       "sleep 60; : ' eval " + secret + " '",  # PR #1492 R2: eval inside a quoted argument
                       "cd / && sleep 60; : " + secret):
            with self.subTest(script=script):
                metadata = self.runtime("codex", script)
                for text in (self.notice(metadata), self.notice(metadata, wire=True)):
                    self.assertNotIn(secret, text)
                    self.assertRegex(text, r"is still running: sleep\.")
        # A single command the shell execs leaves no shell to read: the existing text, no value.
        metadata = self.runtime("codex", "API_TOKEN=" + secret + " sleep 60")
        for text in (self.notice(metadata), self.notice(metadata, wire=True)):
            self.assertNotIn(secret, text)
        self.assertFalse(hasattr(supervision, "_program_name"))
        self.assertFalse(hasattr(supervision, "_shell_command_text"))

    def test_a_shell_running_only_builtins_keeps_the_existing_text(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fifo = Path(tmp.name) / "wait"
        os.mkfifo(fifo)
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                text = self.notice(self.runtime(harness, f"read -t 60 x <> {fifo}; :"))
                self.assertIn("watchdog windows; last comparable progress", text)
                self.assertNotIn("tool command", text)

    def test_no_running_tool_or_no_runtime_keeps_the_existing_text(self):
        text = self.notice(self.runtime("claude", None))
        self.assertIn("watchdog windows; last comparable progress", text)
        self.assertNotIn("tool command", text)
        gone = {**CURRENT, "attempt_id": "att-gone", "harness": "codex", "pid": "999999999", "pid_start": "1"}
        self.assertNotIn("tool command", self.notice(gone))


if __name__ == "__main__":
    unittest.main()
