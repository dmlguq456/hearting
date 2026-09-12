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
import unittest
from unittest import mock

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

    def test_repeated_start_and_restart_keep_both_frames_then_one_owner(self):
        for _ in range(3):
            result = self.start()
            self.assertEqual(result["state"], "preparing", result)
            self.assertEqual(result["parent_next"], "end-turn")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([c[c.index("--adapter")+1] for c in self.calls], ["codex", "opencode"])
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

    def test_route_hash_collision_is_never_adopted(self):
        self.start()
        self.jobs.write_text(self.jobs.read_text().replace("route_hash=sha256:probe", "route_hash=sha256:other"))
        self.assertEqual(self.start()["reason"], "work-attempt-identity-conflict")
        self.assertEqual(len(self.calls), 2)

    def test_quick_uses_the_sealed_candidate_pool(self):
        self.route["effective_intensity"] = "quick"
        self.route["registered_headless_candidates"] = [{"harness":"opencode","status":"supported"}]
        self.start()
        self.assertEqual([c[c.index("--adapter")+1] for c in self.calls], ["opencode", "opencode"])

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
                work_request={"text":"Run both commands and record exit 7 and exit 0.","owner_harness":"codex"})
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
