#!/usr/bin/env python3

import os
import json
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import frame_interview as FI
import workflow_state as WS
import worker_bootstrap as WB

ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = {
    "codex": (
        ROOT / "adapters/codex/bin/dispatch-headless.py",
        ["--model", "gpt-test", "--reasoning", "low"],
        "codex.prompt.txt",
    ),
    "claude": (
        ROOT / "adapters/claude/bin/dispatch-headless.py",
        ["--model", "claude-test", "--effort", "low"],
        "claude.prompt.txt",
    ),
    "opencode": (
        ROOT / "adapters/opencode/bin/dispatch-headless.py",
        ["--model", "provider/test", "--variant", "low"],
        "opencode.prompt.txt",
    ),
}


class WorkerDispatchPromptTest(unittest.TestCase):
    def setUp(self):
        self.parents = []

    def tearDown(self):
        for proc in self.parents:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def test_custom_assignment_is_wrapped_for_every_adapter(self):
        for harness, (wrapper, model, suffix) in ADAPTERS.items():
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                repo.mkdir()
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
                subprocess.run(
                    ["git", "-C", str(repo), "config", "user.email", "fixture@example.com"],
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", str(repo), "config", "user.name", "Fixture"],
                    check=True,
                )
                (repo / "x").write_text("x", encoding="utf-8")
                subprocess.run(["git", "-C", str(repo), "add", "x"], check=True)
                subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
                artifact_root = root / ".agent_reports"
                artifact_root.mkdir()
                logs = root / "logs"
                jobs = root / "jobs.log"
                if harness in {"codex", "claude"}:
                    parent = subprocess.Popen(["sleep", "60"])
                    self.parents.append(parent)
                    start = (Path("/proc") / str(parent.pid) / "stat").read_text().split()[21]
                    jobs.write_text(
                        f"2026-07-23T00:00:00Z\topen\t{repo}\t{repo}\towner\t"
                        "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                        "execution_surface=registered-headless,registered_worker=1,"
                        "fallback_hop=same-harness-headless,worker_type=owner,"
                        f"harness={harness},runtime_sandbox=workspace-write,"
                        f"attempt_id=att-prompt-parent,pid={parent.pid},pid_start={start}\n"
                    )
                slug = f"{harness}-typed"
                if harness == "opencode":
                    topology = [
                        "--dispatch-depth", "1",
                        "--worker-type", "owner",
                        "--unit", "_kernel/owner",
                        "--assigned-contract", "autopilot-code",
                    ]
                    mode_axes = ["--capability-mode", "dev"]
                    expected_worker_type = "Owner"
                    expected_contract = "autopilot-code"
                else:
                    topology = [
                        "--dispatch-depth", "2",
                        "--parent", "owner",
                        "--worker-type", "stage",
                        "--assigned-contract", "code-test",
                    ]
                    mode_axes = [
                        "--capability-mode", "dev",
                        "--worker-mode", "dev/backend",
                    ]
                    expected_worker_type = "Stage"
                    expected_contract = "code-test"
                command = [
                    sys.executable,
                    str(wrapper),
                    "--register",
                    "--worktree",
                    str(repo),
                    "--slug",
                    slug,
                    "--capability",
                    "autopilot-code",
                    *mode_axes,
                    "--intensity",
                    "standard",
                    *topology,
                    *(
                        [
                            "--parent-harness", harness,
                            "--parent-transport", "headless",
                            "--parent-sandbox", "workspace-write",
                            "--nested-eligibility", "supported",
                            "--eligibility-source", "fixture",
                        ]
                        if harness in {"codex", "claude"}
                        else []
                    ),
                    "--prompt-text",
                    "CUSTOM ASSIGNMENT",
                    "--jobs",
                    str(jobs),
                    "--log-dir",
                    str(logs),
                    *model,
                ]
                env = {
                    **os.environ,
                    "AGENT_HOME": str(ROOT),
                    "AGENT_ARTIFACT_ROOT": str(artifact_root),
                    "AGENT_ARTIFACT_CYCLE_ID": "cyc-concrete-prompt",
                    "AGENT_ARTIFACT_CYCLE_DIR": str(artifact_root / "campaigns" / "stream" / "current"),
                    "AGENT_ARTIFACT_OUTPUT_DIR": "",
                    "OPENCODE_CONFIG_CONTENT": "{}",
                }
                env.pop("AGENT_DISPATCH_JOBS", None)
                if harness in {"codex", "claude"}:
                    env["AGENT_DISPATCH_ATTEMPT_ID"] = "att-prompt-parent"
                result = subprocess.run(command, text=True, capture_output=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                prompts = list(logs.glob(f"{slug}.*.{suffix}"))
                self.assertEqual(len(prompts), 1, prompts)
                prompt = prompts[0].read_text(encoding="utf-8")
                self.assertEqual(prompt.count("# Portable Worker Kernel"), 1)
                self.assertEqual(prompt.count("# Worker Type:"), 1)
                self.assertIn(f"# Worker Type: {expected_worker_type}", prompt)
                self.assertIn(f"- assigned_contract: {expected_contract}", prompt)
                self.assertNotIn("- worker_role:", prompt)
                self.assertIn("CUSTOM ASSIGNMENT", prompt)
                self.assertIn("- artifact_cycle_id: cyc-concrete-prompt", prompt)
                self.assertIn(f"- artifact_output_dir: {artifact_root}/campaigns/stream/current/artifacts", prompt)
                self.assertIn("artifact: <canonical path | ->", prompt)
                self.assertIn("verdict: PASS | FAIL | BLOCKED", prompt)
                self.assertIn("blocker: none | <one line>", prompt)
                self.assertNotIn("Read $AGENT_HOME/adapters/codex/AGENTS.md first", prompt)
                self.assertNotIn("Return a concise report with changed files", prompt)
                if harness in {"codex", "claude"}:
                    # SD-45 guard-identity parity (round_1 finding 4): the
                    # dispatch metadata's guard_session_id must equal the
                    # exact attempt id the wrapper minted for this row — the
                    # same identity embedded in the prompt filename itself —
                    # and the field name/position must match across adapters.
                    prompt_attempt_id = prompts[0].name.split(".")[1]
                    self.assertIn(f"- guard_session_id: {prompt_attempt_id}\n", prompt)
                    self.assertNotIn("- guard_session_id: codex-headless", prompt)
                    self.assertNotIn("codex-headless", prompt)

    def test_three_adapters_leave_progress_bookkeeping_to_runtime(self):
        for harness, (wrapper, model, _suffix) in ADAPTERS.items():
            with self.subTest(harness=harness):
                spec=importlib.util.spec_from_file_location(f"dispatch_{harness}",wrapper)
                module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                args=module.parser().parse_args([
                    "--worktree","/work/repo","--slug","stage","--capability","autopilot-code",
                    "--capability-mode","dev","--worker-mode","dev/backend",
                    "--unit","dev/backend","--intensity","standard","--dispatch-depth","2",
                    "--parent","owner","--worker-type","stage",
                    "--assigned-contract","code-test","--prompt-text","TASK",*model,
                ])
                args.attempt_id="att-promptheartbeat01";args.route_id="rt-prompt";args.route_node="test"
                args.artifact_root="/artifacts"
                render=module.prompt if harness=="opencode" else module.dispatch_prompt
                prompt,_=render(args)
                self.assertIn("The runtime observes tool progress",prompt)
                self.assertIn("No per-tool heartbeat command is required",prompt)
                self.assertNotIn("--phase analysis",prompt)
                self.assertNotIn("Stage progress contract",prompt)
                self.assertIn("TASK",prompt)


class ReleasedTaskPromptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "canonical" / "jobs.log"
        self.jobs.parent.mkdir()
        self.jobs.touch()
        self.route_id = "rt-released-context"
        self.interview = self.root / "issued-cycle" / "interview.json"
        self.interview.parent.mkdir()
        self.value = {
            "schema": FI.SCHEMA, "route_id": self.route_id, "round": 1,
            "understanding": "Run only sum([1,2]) and record the observed result.",
            "brief": {"constraints": "No source edits or repository audit."},
            "questions": [{"id": "record-failure", "topic": "Evidence",
                "question": "Which observations should be recorded?",
                "options": [{"label": "Both", "means": "Record the alias failure and python3 success."},
                            {"label": "Success", "means": "Record the successful command."}],
                "recommended": 0}],
        }
        self.interview.write_text(json.dumps(self.value))
        self.answers = FI.answers_template(self.value)
        self.answers["understanding_confirmed"] = True
        self.answers["answers"]["record-failure"] = {"choice": 0, "note": "Keep the failure."}
        self.ledger = WS.WorkflowLedger(self.route_id, jobs=self.jobs)
        self.ledger.root.mkdir(parents=True)
        self.raised = {"at": "2026-09-11T11:00:00Z", "workflow_state": "BLOCKED_HUMAN_GATE",
            "evidence": {"gate": "frame-review", "artifact": str(self.interview),
                         "interview": True, "questions": 1}}
        self.released = {"at": "2026-09-11T11:01:00Z", "workflow_state": "READY",
            "evidence": {"released_gate": "frame-review", "decision": "proceed",
                         "answers": self.answers}}
        self.write_journal(self.raised, self.released)

    def write_journal(self, *entries):
        self.ledger.journal_path.write_text("".join(json.dumps(e) + "\n" for e in entries))

    def args(self, **overrides):
        return SimpleNamespace(**{"worker_type": "stage", "route_id": self.route_id,
                                  "jobs": self.jobs, **overrides})

    def test_no_plan_stage_or_intent_copy_needed_in_all_three_adapters(self):
        for harness, (wrapper, model, _suffix) in ADAPTERS.items():
            spec = importlib.util.spec_from_file_location("context_" + harness, wrapper)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for worker_type, unit, depth in (("owner", "_kernel/owner", "1"),
                                             ("stage", "qa/test", "2"),
                                             ("review", "qa/code-review", "2")):
                with self.subTest(harness=harness, worker_type=worker_type):
                    args = module.parser().parse_args([
                        "--worktree", str(self.root), "--slug", "context", "--capability", "autopilot-code",
                        "--capability-mode", "dev", "--intensity", "standard", "--dispatch-depth", depth,
                        "--worker-type", worker_type, "--unit", unit, "--jobs", str(self.jobs), *model])
                    args.attempt_id = "att-context"
                    args.route_id = None if worker_type == "owner" else self.route_id
                    if worker_type == "owner":
                        args.owner_route_binding = SimpleNamespace(route_id=self.route_id, route_file="/issued/route.json")
                    args.artifact_root = str(self.root)
                    render = module.prompt if harness == "opencode" else module.dispatch_prompt
                    for custom in (None, "Run the assigned stage with this extra detail."):
                        args.prompt_text = custom
                        # A different ambient cycle must never supply the task.
                        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_OUTPUT_DIR": "/wrong-cycle/artifacts"}):
                            prompt, _ = render(args)
                        self.assertIn("Run only sum([1,2])", prompt)
                        self.assertIn("Record the alias failure and python3 success.", prompt)
                        self.assertIn("record-failure", prompt)
                        self.assertIn("No source edits or repository audit.", prompt)
                        if custom:
                            self.assertIn(custom, prompt)
                        self.assertFalse((self.interview.parent / "intent.md").exists())

    def test_latest_raise_drops_old_answers_and_frame_remains_independent(self):
        self.assertEqual(WB.released_task_prompt(self.args(worker_type="frame")), "")
        self.write_journal(self.raised, self.released, self.raised)
        self.assertEqual(WB.released_task_prompt(self.args()), "")
        self.write_journal(self.raised)
        self.assertEqual(WB.released_task_prompt(self.args()), "")

    def test_legacy_route_does_not_inherit_another_routes_context(self):
        self.assertEqual(WB.released_task_prompt(self.args(route_id="rt-unrelated")), "")
        self.assertEqual(WB.released_task_prompt(self.args(route_id=None)), "")
        fresh_jobs = self.root / "fresh-preview" / "jobs.log"
        self.assertEqual(WB.released_task_prompt(self.args(jobs=fresh_jobs)), "")
        self.assertFalse(fresh_jobs.parent.exists())

    def test_missing_or_foreign_input_returns_the_exact_recovery_path(self):
        self.interview.unlink()
        with self.assertRaisesRegex(ValueError, "Restore the recorded interview"):
            WB.released_task_prompt(self.args())
        self.interview.write_text(json.dumps({**self.value, "route_id": "rt-other"}))
        with self.assertRaisesRegex(ValueError, "different route"):
            WB.released_task_prompt(self.args())
        self.interview.write_text(json.dumps(self.value))
        self.released["evidence"]["answers"] = {**self.answers, "route_id": "rt-other"}
        self.write_journal(self.raised, self.released)
        with self.assertRaisesRegex(ValueError, "route_id"):
            WB.released_task_prompt(self.args())


if __name__ == "__main__":
    unittest.main()
