#!/usr/bin/env python3
"""Public start orchestration; outcome authority remains the production join.

Admission and observation are controlled here to exercise crash/replay and
refusal boundaries. Actual claim/process/closure tests live in their owners.
"""
import json
import contextlib
import io
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace

import work_start as W
from dispatch_completion_join import CurrentDeliveryState


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class WorkStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        self.route = {"route_id": "rt-probe", "route_hash": "sha256:probe", "slug": "task",
            "effective_intensity": "standard", "work_request": {"text": "Run the two commands", "owner_harness": "codex"},
            "nodes": [{"id": n, "worker_type": "frame", "dispatch_depth": 1} for n in ("frame", "frame-alternative")]
                     + [{"id": "test", "depends_on": ["frame", "frame-alternative"]}],
            "dispatch_evidence": {"tuples": [{"child_harness": h, "status": "supported"} for h in ("codex", "opencode")]}}
        self.path = Path(self.tmp.name) / "route.json"
        self.path.write_text(json.dumps(self.route))
        self.calls = []
        self.ready = False
        self.released = False
        self.statuses = {}
        self.parent = mock.patch.object(W, "default_parent_session_id", return_value="parent")
        self.parent.start(); self.addCleanup(self.parent.stop)
        self.environment = mock.patch.dict(os.environ, {"AGENT_CODEX_MANAGED_GATEWAY": "0"})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.join = mock.patch.object(W, "join_selected_attempts", side_effect=self.observe)
        self.join.start(); self.addCleanup(self.join.stop)
        self.gate = mock.patch.object(W, "owner_frame_launch_gate", side_effect=self.owner_gate)
        self.gate.start(); self.addCleanup(self.gate.stop)
        self.pair = mock.patch.object(W, "completion_marker_gate")
        self.pair.start(); self.addCleanup(self.pair.stop)
        self.current = mock.patch.object(W, "current_delivery_state", side_effect=self.delivery)
        self.current.start(); self.addCleanup(self.current.stop)

    def observe(self, **kw):
        return {"state": "ready" if self.ready else "timeout", "children": []}

    def delivery(self, jobs, aid, **kw):
        fields = dict(marker={"artifact": "/exact/report.md"}, marker_digest="sha256:marker",
            row_revision="1", row_digest="sha256:row", status="done", verdict="PASS", quiescent=True,
            owned_children=0, advanced=False, completion_proven=True)
        fields.update(self.statuses.get(aid, {}))
        return CurrentDeliveryState(**fields)

    def owner_gate(self, *args, **kw):
        if not self.released:
            raise W.DispatchContractError("human-gate-unreleased", "frame-review")

    def admit(self, command, **kwargs):
        self.calls.append(command)
        def value(flag, fallback=""):
            return command[command.index(flag) + 1] if flag in command else fallback
        aid = value("--attempt-id")
        node = value("--route-node", "owner")
        meta = {"attempt_id": aid, "parent_sid": "parent", "launch_started": "1",
            "worker_type": "owner" if node == "owner" else "frame", "route_node": node,
            "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
            "owner_route_hash": self.route["route_hash"] if node == "owner" else "",
            "parent_completion_delivery": "codex-managed-gateway"}
        with self.jobs.open("a") as stream:
            stream.write("now\topen\t12\tparent\ttask\t" + ",".join(k+"="+v for k,v in meta.items()) + "\n")
        return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")

    def start(self, **kwargs):
        return W.start_work(self.route, self.path, self.jobs, run=self.admit, **kwargs)

    def refusal_receipt(self, *, worker_class="dispatch", retry_after_seconds=1,
                         frees_at=None, retryable="1", reason="model-worker-governor-denied",
                         child_spawned="0"):
        if frees_at is None:
            frees_at = int(time.time()) + (retry_after_seconds or 60)
        lines = [
            "check=failed", f"reason={reason}",
            f"detail=rolling model-worker start budget reached: class={worker_class} "
            f"used=20 limit=20 retry_after_seconds={retry_after_seconds}",
            f"child_spawned={child_spawned}",
        ]
        if retryable is not None:
            lines += [
                f"retryable={retryable}", "refusal=start-budget", f"worker_class={worker_class}",
                f"retry_after_seconds={retry_after_seconds}", f"frees_at={frees_at}",
            ]
        return "\n".join(lines) + "\n"

    def make_run(self, *, refuse_node, refuse_times=1, receipt=None):
        """A `run` fixture that refuses one node's launch a bounded number of
        times (typed refusal receipt, no registered row), then falls through
        to the normal admitting fixture."""
        counts = {"n": 0}

        def run(command, **kwargs):
            node = command[command.index("--route-node") + 1] if "--route-node" in command else "owner"
            if node == refuse_node and counts["n"] < refuse_times:
                counts["n"] += 1
                return subprocess.CompletedProcess(command, 75, receipt or self.refusal_receipt(), "")
            return self.admit(command, **kwargs)

        run.counts = counts
        return run

    def test_repeated_start_and_restart_keep_both_frames_then_one_owner(self):
        for _ in range(3):
            result = self.start()
            self.assertEqual(result["state"], "preparing", result)
            self.assertEqual(result["parent_next"], "end-turn")
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all("--adapter" not in c for c in self.calls))
        self.ready = True
        result = self.start()
        self.assertEqual(result["state"], "needs-interview", result)
        self.assertEqual(len(self.calls), 2)
        self.released = True
        result = self.start()
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(result["result"]["recovery_command"], "")
        self.assertEqual(result["result"]["marker"]["artifact"], "/exact/report.md")
        self.start(); self.assertEqual(len(self.calls), 3)

    def test_partial_admission_retains_the_started_child_and_exact_resume(self):
        def partial(command, **kwargs):
            if "frame-alternative" in command:
                return subprocess.CompletedProcess(command, 75, "started=0", "capacity temporarily full")
            return self.admit(command, **kwargs)
        result = W.start_work(self.route, self.path, self.jobs, run=partial)
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(len(result["frame_attempts"]), 1)
        self.assertEqual(result["parent_next"], "end-turn")
        self.assertIn("capacity temporarily full", result["launches"][1]["diagnostic"])
        self.assertEqual(self.start()["state"], "preparing")
        self.assertEqual(len(self.calls), 2)

    def test_typed_budget_refusal_is_waiting_capacity_not_failure(self):
        run = self.make_run(refuse_node="frame-alternative", refuse_times=99,
                             receipt=self.refusal_receipt(retry_after_seconds=1))
        result = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(result["state"], "waiting-capacity", result)
        self.assertEqual(result["refused_attempt_id"], W.attempt_id(self.route, "frame-alternative"))
        self.assertEqual(result["refused_node"], "frame-alternative")
        self.assertFalse(result["spawned"])
        self.assertIn("retry_at", result)
        self.assertEqual(len(result["frame_attempts"]), 1)
        self.assertEqual(result["parent_next"], "bounded-wait")
        self.assertTrue(result["parent_next_command"].endswith("--wait"))
        self.assertNotIn("failed attempt", result["next_step"])

    def test_resume_after_capacity_relaunches_refused_attempt_exactly_once(self):
        run = self.make_run(refuse_node="frame-alternative", refuse_times=1,
                             receipt=self.refusal_receipt(retry_after_seconds=1))
        first = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(first["state"], "waiting-capacity", first)
        self.assertEqual(len(self.calls), 1)  # only "frame" admitted so far

        second = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(second["state"], "preparing", second)
        self.assertEqual(len(second["frame_attempts"]), 2)
        self.assertEqual(len(self.calls), 2)  # frame-alternative admitted exactly once

        W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(len(self.calls), 2)  # both rows exist; no further launch call

    def test_resume_wait_sleeps_until_retry_then_launches_once(self):
        run = self.make_run(refuse_node="frame-alternative", refuse_times=1,
                             receipt=self.refusal_receipt(retry_after_seconds=7))
        sleeps = []
        result = W.start_work(self.route, self.path, self.jobs, run=run, wait=True,
                               sleep=sleeps.append, clock=lambda: 1_800_000_000.0)
        self.assertEqual(sleeps, [7])
        self.assertNotEqual(result["state"], "waiting-capacity", result)
        self.assertEqual(len(result["frame_attempts"]), 2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result["capacity_waited_seconds"], 7)
        self.assertEqual(W.join_selected_attempts.call_args.kwargs["timeout"], 600 - 7)

    def test_resume_wait_refused_again_hands_back_without_another_wait(self):
        run = self.make_run(refuse_node="frame-alternative", refuse_times=99,
                             receipt=self.refusal_receipt(retry_after_seconds=3))
        sleeps = []
        result = W.start_work(self.route, self.path, self.jobs, run=run, wait=True,
                               sleep=sleeps.append, clock=lambda: 1_800_000_000.0)
        self.assertEqual(sleeps, [3])  # exactly one wait, never a retry loop
        self.assertEqual(result["state"], "waiting-capacity", result)
        self.assertNotIn("parent_next", result)
        self.assertNotIn("parent_next_command", result)
        self.assertEqual(result["required_action"], "report-capacity-wait")
        self.assertEqual(result["capacity_waited_seconds"], 3)

    def test_owner_budget_refusal_is_waiting_capacity(self):
        run = self.make_run(refuse_node="owner", refuse_times=99,
                             receipt=self.refusal_receipt(retry_after_seconds=5))
        self.ready = True
        self.released = True
        result = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(result["state"], "waiting-capacity", result)
        self.assertEqual(result["refused_attempt_id"], W.attempt_id(self.route, "owner"))
        self.assertEqual(result["refused_node"], "owner")
        self.assertFalse(result["spawned"])

    def test_spawned_then_exit_75_is_never_relaunched(self):
        def crashed_after_claim(command, **kwargs):
            registered = self.admit(command, **kwargs)
            receipt = registered.stdout + self.refusal_receipt(retry_after_seconds=5, child_spawned="1")
            return subprocess.CompletedProcess(command, 75, receipt, "worker crashed after claim")
        result = W.start_work(self.route, self.path, self.jobs, run=crashed_after_claim)
        self.assertNotEqual(result["state"], "waiting-capacity", result)
        calls_after_first = len(self.calls)
        W.start_work(self.route, self.path, self.jobs, run=crashed_after_claim)
        self.assertEqual(len(self.calls), calls_after_first)  # rows exist; no relaunch

    def test_receipt_refusal_with_existing_row_trusts_registry(self):
        def raced(command, **kwargs):
            node = command[command.index("--route-node") + 1] if "--route-node" in command else "owner"
            if node == "frame-alternative":
                self.admit(command, **kwargs)  # a concurrent resume already admitted this exact aid
                return subprocess.CompletedProcess(command, 75, self.refusal_receipt(), "")
            return self.admit(command, **kwargs)
        result = W.start_work(self.route, self.path, self.jobs, run=raced)
        self.assertNotEqual(result["state"], "waiting-capacity", result)
        self.assertEqual(len(result["frame_attempts"]), 2)

    def test_untyped_or_kill_switch_refusal_stays_needs_attention(self):
        def kill_switch(command, **kwargs):
            node = command[command.index("--route-node") + 1] if "--route-node" in command else "owner"
            if node == "frame-alternative":
                receipt = self.refusal_receipt(retryable=None, reason="model-worker-governor-denied")
                return subprocess.CompletedProcess(command, 75, receipt, "kill switch")
            return self.admit(command, **kwargs)
        result = W.start_work(self.route, self.path, self.jobs, run=kill_switch)
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(result["reason"], "frame-launch-not-admitted")

    def test_automatic_frames_use_real_selector_usage_gate_before_wrapper_launch(self):
        owner = load("work_start_capacity_owner", W.ROOT / "utilities/dispatch-owner.py")
        self.route.update(cwd=self.tmp.name, capability="autopilot-code", capability_mode="debug",
                          owner_model_profile="deep")
        for node in self.route["nodes"][:2]:
            node.update(model_profile="deep", role="deep maker", unit="plan/frame")
        self.route["dispatch_evidence"]["tuples"] = [
            {"child_harness": h, "status": "supported"} for h in ("claude", "codex")]
        self.path.write_text(json.dumps(self.route))
        context = {"harnesses": ["claude", "codex"],
            "policy": {"primary": ["claude", "codex"], "relief": [], "last_resort": [],
                       "promote_relief_below": 0},
            "allocation": {"strategy": "balanced", "window": 30,
                "harness_order": ["claude", "codex", "opencode"], "usage_gate_used_percent": 85}}
        binding = SimpleNamespace(route_file=str(self.path), route_id=self.route["route_id"],
            route_hash=self.route["route_hash"], route_node="frame", registry_digest="sha256:fixture",
            write_scope="shards/frame/**", completion_gate="code-frame")
        launched = []
        def wrapper(command, **kwargs):
            launched.append(Path(command[0]).parents[1].name)
            return subprocess.CompletedProcess(command, 0)
        def select(command, **kwargs):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = owner.main(command[2:])
            if rc == 0:
                self.admit(command)
            return subprocess.CompletedProcess(command, rc, output.getvalue(), "")
        import artifact_producer
        for headroom in (3, 1):
            with self.subTest(headroom=headroom), contextlib.ExitStack() as stack:
                self.jobs.unlink(missing_ok=True); self.calls.clear(); launched.clear()
                stack.enter_context(mock.patch.dict(os.environ, {}, clear=True))
                stack.enter_context(mock.patch.object(owner, "_authoritative_jobs", return_value=str(self.jobs)))
                stack.enter_context(mock.patch.object(owner, "_sealed_owner_context", return_value=context))
                stack.enter_context(mock.patch.object(owner, "_usage", return_value=dict.fromkeys(("claude", "codex", "opencode"), "ok")))
                stack.enter_context(mock.patch.object(owner._capacity, "capacity_report", return_value={
                    "scores": {"claude": headroom, "codex": 50, "opencode": 0}, "sources": {}}))
                stack.enter_context(mock.patch.object(owner, "derive_frame_route_binding", return_value=binding))
                stack.enter_context(mock.patch.object(artifact_producer, "prepare_route_artifact_env", return_value={}))
                stack.enter_context(mock.patch.object(owner.subprocess, "run", side_effect=wrapper))
                result = W.start_work(self.route, self.path, self.jobs, run=select)
                self.assertEqual(result["state"], "preparing", result)
                self.assertEqual(launched, ["codex", "codex"])
                self.assertTrue(all("selection_source=configured-balanced" in r["receipt"] for r in result["launches"]))
                # No authorized capacity is a refusal before any model wrapper.
                self.jobs.unlink(); self.calls.clear(); launched.clear()
                with mock.patch.object(owner, "_usage", return_value=dict.fromkeys(("claude", "codex", "opencode"), "limited(reset)")):
                    refused = W.start_work(self.route, self.path, self.jobs, run=select)
                self.assertEqual(refused["state"], "needs-attention", refused)
                self.assertEqual(launched, [])
                self.assertFalse(self.jobs.exists())

    def test_public_answer_submission_releases_then_starts_only_one_owner(self):
        self.start(); self.ready = True
        def release(*args, **kwargs):
            self.assertEqual(kwargs["answers"], "actual-answers.json")
            self.released = True
            return {"state": "released", "decision": "proceed"}
        with mock.patch.object(W, "frame_interview_step", side_effect=release):
            result = self.start(interview="question.json", answers="actual-answers.json")
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(len(self.calls), 3)

    def test_question_failure_after_frame_completion_does_not_promise_another_wake(self):
        self.start(); self.ready = True
        self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t"))
        with mock.patch.object(W, "frame_interview_step", side_effect=ValueError("bad-question")):
            result = self.start(interview="question.json")
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertNotIn("parent_next", result)
        self.assertEqual(len(self.calls), 2)

    def test_exited_owner_with_missing_work_reports_without_waiting_or_replacement(self):
        import dispatch_terminal_commit as T
        self.start(); self.ready = self.released = True; self.start()
        self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t").replace(
            "worker_type=owner", "workflow_completion=runtime-v1,failure_class=pass,worker_type=owner"))
        with mock.patch.object(T, "owner_workflow_gaps", return_value={"report":"completion-marker-absent"}), \
             mock.patch.object(W, "join_selected_attempts", side_effect=AssertionError("cannot wait for an absent executor")):
            result = self.start(wait=True)
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(result["reason"], "workflow-executor-exited")
        self.assertNotIn("parent_next", result)
        self.assertEqual(len(self.calls), 3)

    def test_completed_deferred_owner_still_reports_workflow_gaps(self):
        """C15 (S3a): `verdict_pass` recognizes a marker-bound deferred owner
        row (failure_class stays infrastructure) the same way a plain
        failure_class=pass row already does -- the gap check must still run,
        not be skipped because the literal failure_class isn't "pass"."""
        import dispatch_terminal_commit as T
        self.start(); self.ready = self.released = True; self.start()
        self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t").replace(
            "worker_type=owner",
            "workflow_completion=runtime-v1,failure_class=infrastructure,"
            "classifier_source=registered-wrapper-completion-transient-v1,"
            "completion_marker=/artifacts/.runtime/completions/one-shot.json,"
            "note=completed-marker,worker_type=owner"))
        with mock.patch.object(T, "owner_workflow_gaps", return_value={"report": "completion-marker-absent"}), \
             mock.patch.object(W, "join_selected_attempts", side_effect=AssertionError("cannot wait for an absent executor")):
            result = self.start(wait=True)
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(result["reason"], "workflow-executor-exited")

    def test_failure_conflict_or_unsealed_workflow_never_authorizes_success(self):
        self.start(); self.ready = True
        for fields in ({"verdict":"FAIL"}, {"terminal_conflict":True}, {"workflow_complete":False}):
            with self.subTest(fields=fields):
                self.statuses[W.attempt_id(self.route,"frame")] = fields
                result = self.start()
                self.assertEqual(result["state"], "needs-attention", result)
                self.assertEqual(len(self.calls), 2)
                outcome = next(r for r in result["frame_results"] if r["classification"] == "attention")
                command = outcome["recovery_command"]
                self.assertIn(W.attempt_id(self.route,"frame"), command)
                self.assertNotIn("retry", command)

    def test_exited_owner_with_pending_closure_never_promises_running_or_a_new_turn(self):
        import dispatch_terminal_commit as T
        self.start(); self.ready = self.released = True; self.start()
        self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t").replace(
            "worker_type=owner", "workflow_completion=runtime-v1,failure_class=pass,worker_type=owner"))
        self.ready = False
        self.statuses[W.attempt_id(self.route,"owner")] = {"workflow_complete":False}
        with mock.patch.object(T,"owner_workflow_gaps",return_value={}):
            for wait in (False,True):
                result=self.start(wait=wait)
                self.assertEqual(result["state"],"needs-attention",result)
                self.assertEqual(result["reason"],"owner-settlement-pending")
                self.assertIn(" finish ",result["result"]["recovery_command"])
                self.assertNotIn("parent_next",result)
                self.assertEqual(len(self.calls),3)

    def test_owner_exits_during_join_before_the_public_receipt(self):
        self.start(); self.ready = self.released = True; self.start()
        def close_during_join(**kwargs):
            self.jobs.write_text(self.jobs.read_text().replace("\topen\t","\tdone\t"))
            return {"state":"timeout","children":[]}
        self.statuses[W.attempt_id(self.route,"owner")] = {"workflow_complete":False}
        with mock.patch.object(W,"join_selected_attempts",side_effect=close_during_join):
            result=self.start()
            self.assertEqual(result["state"],"needs-attention",result)
            self.assertNotIn("parent_next",result)
            self.statuses[W.attempt_id(self.route,"owner")] = {}
            self.assertEqual(self.start()["state"],"completed")
        self.assertEqual(len(self.calls),3)

    def test_unknown_process_retains_runtime_wait_without_replacement(self):
        self.start()
        for _ in range(3):
            result = self.start()
            self.assertEqual(result["state"], "preparing")
        self.assertEqual(len(self.calls), 2)

    def test_bounded_wait_deadline_hands_back_without_another_wait_or_retry(self):
        for owner in (False, True):
            with self.subTest(owner=owner):
                self.jobs.unlink(missing_ok=True); self.calls.clear()
                if owner:
                    self.route["nodes"] = []
                result = self.start(wait=True)
                self.assertEqual(result["state"],"needs-attention",result)
                self.assertEqual(result["reason"],"parent-wait-deadline")
                self.assertEqual(result["required_action"],"report-pending-work")
                self.assertNotIn("parent_next_command",result)
                self.assertIn("runtime watchers retain",result["next_step"])
                before = len(self.calls)
                self.start()
                self.assertEqual(len(self.calls),before)

    def test_foreign_parent_is_checked_before_any_new_sibling(self):
        self.admit(["fixture", "--route-node", "frame-alternative", "--attempt-id", W.attempt_id(self.route,"frame-alternative")])
        self.jobs.write_text(self.jobs.read_text().replace("parent_sid=parent", "parent_sid=other"))
        self.calls.clear()
        result = self.start()
        self.assertEqual(result["reason"], "work-parent-recovery-required", result)
        self.assertEqual(self.calls, [])

    def test_resume_reuses_witnessed_parent_and_refuses_sibling_or_missing_proof(self):
        self.start()
        W.default_parent_session_id.return_value = "inherited"
        with mock.patch.dict(os.environ, {"AGENT_CODEX_MANAGED_GATEWAY": "1", "AGENT_DISPATCH_CHILD": "0"}), \
             mock.patch.object(W, "interactive_parent_identity", return_value=("codex", "inherited")), \
             mock.patch.object(W, "probe_managed_codex_parent", return_value=SimpleNamespace(thread_id="parent")) as probe:
            self.assertEqual(self.start()["state"], "preparing")
            self.assertEqual(len(self.calls), 2)
            probe.assert_called_with(parent_harness="codex", parent_session_id="inherited")
            probe.return_value = SimpleNamespace(thread_id="sibling")
            self.assertEqual(self.start()["reason"], "work-parent-recovery-required")
            probe.side_effect = W.ManagedDispatchError("managed-gateway-not-ready")
            self.assertEqual(self.start()["reason"], "work-parent-recovery-required")
            self.assertEqual(len(self.calls), 2)

    def test_route_hash_collision_is_never_adopted(self):
        self.start()
        self.jobs.write_text(self.jobs.read_text().replace("route_hash=sha256:probe", "route_hash=sha256:other"))
        self.assertEqual(self.start()["reason"], "work-attempt-identity-conflict")
        self.assertEqual(len(self.calls), 2)

    def test_quick_uses_the_sealed_candidate_pool(self):
        self.route["effective_intensity"] = "quick"
        self.route["registered_headless_candidates"] = [{"harness":"opencode","status":"supported"}]
        self.start()
        self.assertTrue(all("--adapter" not in c for c in self.calls))
        self.assertTrue(all(c[c.index("--route-evidence") + 1] == str(self.path) for c in self.calls))

    def test_owner_completion_waits_for_runtime_closure_and_reuses_attempt(self):
        self.route["nodes"] = []
        self.ready = True
        aid = W.attempt_id(self.route, "owner")
        self.statuses[aid] = {"workflow_complete":False}
        result = self.start()
        self.assertEqual(result["result"]["required_action"], "finish-workflow", result)
        self.statuses.clear()
        self.assertEqual(self.start()["state"], "completed")
        self.assertEqual(len(self.calls), 1)

    def test_claim_interruption_leaves_admitted_attempt_recoverable(self):
        def interrupted(command, **kwargs):
            result = self.admit(command, **kwargs)
            raise OSError("caller disconnected after admission")
        result = W.start_work(self.route, self.path, self.jobs, run=interrupted)
        self.assertEqual(result["state"], "needs-attention")
        self.assertEqual(result["registered_attempts"], [W.attempt_id(self.route,"frame")])
        self.assertEqual(result["parent_next"], "end-turn")
        self.assertEqual(self.start()["state"], "preparing")
        self.assertEqual(len(self.calls), 2)

    def test_direct_never_spawns(self):
        self.route["effective_intensity"] = "direct"
        self.assertEqual(self.start()["state"], "inline")
        self.assertEqual(self.calls, [])

    def test_production_selector_and_three_adapter_parsers_accept_the_public_request(self):
        router = load("work_start_router_test", W.ROOT / "utilities/capability-route.py")
        selector = load("work_start_selector_test", W.ROOT / "utilities/dispatch-owner.py")
        evidence = {"tuples": [{"parent_harness":"codex", "child_harness":h,
            "parent_transport":"headless", "parent_sandbox":"workspace-write", "launch_authority":"conductor",
            "status":"supported", "probe_source":"fixture-check", "probe_time":"2026-09-12T00:00:00Z",
            "failure_class":"", "checked_worktree":str(W.ROOT), "failure_scope":"none",
            "codex_command":"ok", "retry_on_isolated_worktree":0} for h in ("codex","opencode")]}
        env = {k:v for k,v in os.environ.items() if not k.startswith(("AGENT_DISPATCH_","AGENT_ROUTE_","AGENT_OWNER_ROUTE_","AGENT_ARTIFACT_"))}
        env["AGENT_HOME"] = str(W.ROOT)
        with mock.patch.dict(os.environ, env, clear=True):
            route = router.compose_route(capability="autopilot-code", capability_mode="dev", shape="staged",
                graph="frame,frame-alternative,test,report", slug="work-start", cwd=W.ROOT,
                artifact_root=self.tmp.name, dispatch_evidence=evidence, parent_harness="codex", profile="light",
                work_request={"text":"Run both commands and record exit 7 and exit 0.","owner_harness":"codex"},
                unassigned=True)
            router.verify_route(route, W.ROOT)
            self.path.write_text(json.dumps(route))
            for node in ("frame", "frame-alternative", "owner"):
                for harness in ("claude", "codex", "opencode"):
                    with self.subTest(node=node,harness=harness):
                        def run(command, **kwargs):
                            _, values, forwarded, _, _ = selector._parse(command[2:])
                            adapter = load("work_start_adapter_"+harness, W.ROOT / "adapters" / harness / "bin/dispatch-headless.py")
                            args = adapter.parser().parse_args(forwarded)
                            self.assertEqual(args.worker_type,"owner" if node=="owner" else "frame")
                            self.assertEqual(args.dispatch_depth,1)
                            self.assertEqual(adapter.resolve_model_settings(args)["profile"],"light")
                            self.assertEqual(args.prompt_text,route["work_request"]["text"])
                            self.assertEqual(args.attempt_id,W.attempt_id(route,node))
                            return subprocess.CompletedProcess(command,0,"validated","")
                        W._start(route,self.path,self.jobs,node,harness,run)


WF = load("work_start_workflow_fixture", W.ROOT / "utilities/workflow_supervisor.test.py")


class FrameInterviewStepTest(WF.WorkflowFixture):
    """Real ledger/gate/answer/intent code; transport and producer location are isolated."""
    def setUp(self):
        super().setUp()
        self.route, self.path = self.two_stage_route(human_gate="frame-review",
            continuation={"kind": "human-gate", "gate": "frame-review"})
        self.jobs = self.base / "jobs.log"
        self.jobs.write_text("")
        self.output = self.base / "artifacts"
        self.calls = []
        self.question = {"understanding": "Run the two commands and preserve their actual results.",
            "brief": {"problem": "We need the measured results.", "outcome": "One report with both results.",
                "affected": "The report only.", "constraints": "Preserve the expected exit code 7.", "open": ""},
            "questions": []}
        self.question_file = self.base / "question.json"
        self.question_file.write_text(json.dumps(self.question))
        import artifact_producer
        for patch in (
            mock.patch.object(artifact_producer, "prepare_route_artifact_env", return_value={"AGENT_ARTIFACT_OUTPUT_DIR": str(self.output)}),
            mock.patch.object(WF.SUP, "create_gate_delivery", return_value=(self.base / "gate-record.json", True)),
            mock.patch.object(WF.SUP, "retire_gate_delivery", return_value="acked"),
        ):
            patch.start(); self.addCleanup(patch.stop)

    def run_command(self, argv, **kwargs):
        self.calls.append(argv[2])
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = WF.SUP.main(argv[2:])
        except (WF.SUP.SupervisorError, WF.WS.WorkflowStateError) as exc:
            rc = 64; stderr.write(str(exc))
        return subprocess.CompletedProcess(argv, rc, stdout.getvalue(), stderr.getvalue())

    def step(self, **kwargs):
        return W.frame_interview_step(self.route, self.path, self.jobs, run=self.run_command, **kwargs)

    def answers(self):
        import frame_interview as FI
        a = FI.answers_template({**self.question, "route_id": self.route["route_id"]})
        a["understanding_confirmed"] = True
        path = self.base / "answers.json"
        path.write_text(json.dumps(a))
        return path

    def resolution(self):
        ledger = WF.WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs)
        return WF.WS.human_gate_resolution(ledger.journal(), "frame-review")

    def test_register_before_question_then_actual_answers_release_once(self):
        self.assertEqual(self.step()["state"], "needs-interview")
        self.assertEqual(self.step(interview=self.question_file)["state"], "needs-question")
        self.assertEqual(self.resolution()["status"], "blocked")
        self.assertEqual(self.step()["state"], "needs-question")
        self.assertEqual(self.resolution()["epoch"], 1)
        result = self.step(answers=self.answers())
        self.assertEqual(result["state"], "released")
        intent = Path(result["intent_file"]).read_bytes()
        self.assertIn(b"status: agreed", intent)
        self.assertEqual(self.resolution()["answers"]["understanding_confirmed"], True)
        self.assertEqual(self.step(answers=self.answers())["state"], "released")
        self.assertEqual(self.calls, ["gate", "release"])
        self.assertEqual(Path(result["intent_file"]).read_bytes(), intent)

    def test_already_received_answers_register_and_release_without_reasking(self):
        result = self.step(interview=self.question_file, answers=self.answers())
        self.assertEqual(result["state"], "released")
        self.assertEqual(self.calls, ["gate", "release"])

    def test_committed_answer_replay_does_not_reopen_or_write_a_sealed_cycle(self):
        import artifact_producer
        answer = self.answers()
        result = self.step(interview=self.question_file, answers=answer)
        with mock.patch.object(artifact_producer, "prepare_route_artifact_env", side_effect=AssertionError("sealed cycle")), \
             mock.patch.object(W, "_store_once", side_effect=AssertionError("sealed write")):
            self.assertEqual(self.step(answers=answer), result)
            self.assertEqual(self.step(interview=self.question_file), result)
        self.assertEqual(self.calls, ["gate", "release"])

    def test_lost_release_response_replays_exact_committed_answer(self):
        original = self.run_command
        def lose(argv, **kwargs):
            r = original(argv, **kwargs)
            if argv[2] == "release":
                return subprocess.CompletedProcess(argv, 70, "", "lost reply")
            return r
        with self.assertRaisesRegex(ValueError, "frame-release-pending"):
            W.frame_interview_step(self.route,self.path,self.jobs,interview=self.question_file,
                                  answers=self.answers(),run=lose)
        self.assertEqual(self.resolution()["status"], "proceed")
        self.assertEqual(self.step(answers=self.answers())["state"], "released")
        self.assertEqual(self.calls, ["gate", "release"])

    def test_changed_answer_cannot_replace_a_committed_decision(self):
        answer = self.answers()
        self.step(interview=self.question_file, answers=answer)
        before = self.resolution()
        changed = json.loads(answer.read_text()); changed["understanding_confirmed"] = False
        changed["correction"] = "Change the scope."
        answer.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "frame-input-conflict"):
            self.step(answers=answer)
        self.assertEqual(self.resolution(), before)

    def test_invalid_or_foreign_answers_do_not_raise_a_gate(self):
        answer = self.answers()
        invalid = json.loads(answer.read_text()); invalid["route_id"] = "rt-foreign"
        answer.write_text(json.dumps(invalid))
        with self.assertRaisesRegex(ValueError, "frame-input-invalid"):
            self.step(interview=self.question_file, answers=answer)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.resolution()["status"], "not-raised")

    def test_stop_does_not_render_an_agreed_intent_or_start_anything(self):
        result = self.step(interview=self.question_file,answers=self.answers(),decision="stop")
        self.assertEqual(result["state"], "cancelled")
        self.assertFalse((self.output / "shards/frame/intent.md").exists())
        self.assertEqual(self.step(answers=self.answers(),decision="stop")["state"], "cancelled")
        self.assertEqual(self.calls, ["gate", "release"])

    def test_stop_without_answer_and_plain_resume_preserve_cancellation(self):
        self.step(interview=self.question_file)
        self.assertEqual(self.step(decision="stop")["state"], "cancelled")
        self.assertEqual(self.step()["state"], "cancelled")
        self.assertEqual(self.calls, ["gate", "release"])

    def test_revision_registers_next_round_without_replacing_the_first(self):
        answer=self.answers()
        result=self.step(interview=self.question_file,answers=answer,decision="revise")
        before=Path(result["interview_file"]).read_bytes()
        self.assertEqual(self.step()["state"], "needs-revision")
        question=result["interview_template"]
        self.assertEqual(question["round"],2)
        question["understanding"]="Run the two commands and keep both outputs in the report."
        self.question_file.write_text(json.dumps(question))
        second=self.step(interview=self.question_file)
        self.assertEqual(second["state"], "needs-question")
        self.assertEqual(self.resolution()["epoch"],2)
        response=second["answers_template"];response["understanding_confirmed"]=True
        answer.write_text(json.dumps(response))
        self.assertEqual(self.step(answers=answer)["state"],"released")
        self.assertEqual(Path(result["interview_file"]).read_bytes(),before)
        self.assertEqual(self.calls,["gate","release","gate","release"])

    def test_revision_limit_hands_back_without_an_unusable_next_command(self):
        import frame_interview as FI
        question=self.question
        for number in range(1,FI.MAX_ROUNDS+1):
            self.question_file.write_text(json.dumps(question))
            response=FI.answers_template({**question,"route_id":self.route["route_id"]})
            response["understanding_confirmed"]=True
            answer=self.base/"received-answer.json";answer.write_text(json.dumps(response))
            result=self.step(interview=self.question_file,answers=answer,decision="revise")
            question=result.get("interview_template")
        self.assertEqual(result["reason"],"frame-revision-round-limit")
        self.assertIsNone(question)
        self.assertEqual(self.resolution()["status"],"revise")
        self.assertEqual(self.step()["state"],"needs-attention")

    def test_interrupted_input_publication_leaves_no_partial_question_and_replays(self):
        import artifact_receipt
        with mock.patch.object(artifact_receipt.os, "link", side_effect=OSError("publication interrupted")):
            with self.assertRaisesRegex(OSError, "publication interrupted"):
                self.step(interview=self.question_file)
        self.assertFalse((self.output / "shards/frame/round-1/interview.json").exists())
        self.assertEqual(self.resolution()["status"], "not-raised")
        self.assertEqual(self.step(interview=self.question_file)["state"], "needs-question")


if __name__ == "__main__":
    unittest.main()
