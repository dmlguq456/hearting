#!/usr/bin/env python3

import os
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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

    def test_route_bound_stage_prompts_name_deterministic_heartbeat_consumer(self):
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
                self.assertIn("Stage progress contract (SD-58)",prompt)
                self.assertIn("att-promptheartbeat01",prompt)
                self.assertIn("rt-prompt",prompt)
                self.assertIn("--phase analysis",prompt)
                self.assertIn("unchanged phase/evidence pair is not progress",prompt)
                heartbeat_path=(
                    ROOT/"utilities/dispatch-progress.py"
                    if harness=="claude"
                    else ROOT/f"adapters/{harness}/bin/preflight.sh"
                )
                self.assertIn(str(heartbeat_path),prompt)


if __name__ == "__main__":
    unittest.main()
