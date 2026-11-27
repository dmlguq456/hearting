#!/usr/bin/env python3
"""A step the owner or its parent session runs itself is printed with its exact start or completion command,
and that command runs as printed where it is printed.

Three fabricated staged routes, no model: (a) a measurement route whose dispatched smoke stage is followed by a
`resource-runner` node, (b) a ship route whose owner-run setup stage is followed by dispatched reviews, (c) the
parent session closing (b)'s setup stage inline with `--route` given as a file and as an id. The resource payload
is a one-line Python print under a fake `nvidia-smi`; no GPU, account or live state is touched.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from fixture_processes import unique_attempt_id  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = load("human_step_route", "utilities/capability-route.py")
F = load("human_step_fallback", "utilities/stage-dispatch-fallback.py")
import artifact_producer  # noqa: E402
import dispatch_completion_join as JOIN  # noqa: E402
import dispatch_contract_gates as GATES  # noqa: E402
import dispatch_stage_advance as ADVANCE  # noqa: E402
import worker_bootstrap as W  # noqa: E402

PAYLOAD = [sys.executable, "-c", "print('fixture payload')"]


class HumanRunStepCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        for argv in (["init", "-q"], ["config", "user.email", "fixture@example.test"], ["config", "user.name", "F"]):
            subprocess.run(["git", "-C", str(self.repo), *argv], check=True)
        (self.repo / "README").write_text("fixture\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "README"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        self.art = base / ".agent_reports"
        self.art.mkdir()
        self.jobs = base / "dispatch" / "jobs.log"
        self.jobs.parent.mkdir()
        self.jobs.write_text("")
        fakebin = base / "fake-bin"
        fakebin.mkdir()
        smi = fakebin / "nvidia-smi"
        smi.write_text("#!/bin/sh\nprintf '0, GPU-TEST, NVIDIA Test GPU, 0, 40960, 0\\n'\n")
        smi.chmod(0o755)
        kept = {k: v for k, v in os.environ.items()
                if not k.startswith(("AGENT_", "HEARTING_DISPATCH", "CODEX_DISPATCH"))}
        self.env = {**kept, "AGENT_HOME": str(ROOT), "AGENT_ARTIFACT_ROOT": str(self.art),
                    "AGENT_MODEL_GOVERNOR_ROOT": str(self.art / ".runtime/model-worker-governor"),
                    "AGENT_DISPATCH_JOBS": str(self.jobs),
                    "AGENT_RESOURCE_RUN_INDEX": str(base / "resource-index.json"),
                    "DISPATCH_DEFAULTS_CONFIG": str(ROOT / "profiles/dispatch-defaults.yaml"),
                    "PATH": str(fakebin) + os.pathsep + kept.get("PATH", "")}
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def compose(self, capability, mode):
        """A sealed staged route of the real recipe, at its canonical path, with its cycle bound."""
        def legs(child):
            return {"parent_harness": "codex", "parent_transport": "headless", "parent_sandbox": "workspace-write",
                    "child_harness": child, "launch_authority": "conductor", "status": "supported",
                    "probe_source": "fixture", "probe_time": "2026-07-16T00:00:00Z", "failure_class": "",
                    "checked_worktree": str(self.repo.resolve()), "failure_scope": "none",
                    "codex_command": "ok" if child == "codex" else "not-applicable", "retry_on_isolated_worktree": 0}
        evidence = {"tuples": [legs("codex"), legs("claude")], "native_subagent": [{
            "harness": "codex", "transport": "headless", "execution_surface": "codex-native-subagent",
            "registered_worker": False, "status": "unsupported", "check_source": "fixture"}]}
        gate = {"spec_read": {"satisfied": True, "source": "fixture"}, "drift_verdict": "within-spec",
                "workflow_mode": "tracked", "artifact_guard": {"satisfied": True, "source": "fixture"}}
        route = R.compile_route(capability, mode, "standard", self.repo, self.art, transport="headless",
                                tracking="tracked", tracked_gate_evidence=gate, dispatch_evidence=evidence,
                                frameless=True, campaign_key="human-step-fixture")
        path = R.canonical_route_path(self.art, route["route_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(route))
        cycle = artifact_producer.prepare_route_artifact_env(path, start=True, jobs=self.jobs)
        self.output = cycle["AGENT_ARTIFACT_OUTPUT_DIR"]
        return route, path

    def node(self, route, node_id):
        return next(n for n in route["nodes"] if n["id"] == node_id)

    def owner_env(self, route, path):
        """The registered owner's environment: its own attempt and route binding (no node), its bound cycle."""
        return {**self.env, "AGENT_DISPATCH_ATTEMPT_ID": unique_attempt_id("att-human-step-owner"),
                "AGENT_ROUTE_FILE": str(path), "AGENT_ROUTE_ID": route["route_id"], "AGENT_ROUTE_NODE": "",
                "AGENT_ARTIFACT_OUTPUT_DIR": self.output}

    def parent_env(self):
        """The parent session's environment: no dispatch attempt, only its caller harness."""
        env = {k: v for k, v in self.env.items() if not k.startswith("AGENT_DISPATCH_")}
        return {**env, "AGENT_DISPATCH_CALLER_HARNESS": "claude"}

    def route_steps(self, path):
        args = SimpleNamespace(worker_type="owner", route_file=str(path), route_node=None)
        return W.owner_route_steps_prompt(args, {"AGENT_ARTIFACT_OUTPUT_DIR": self.output})

    def step_command(self, text, node_id, verb):
        """The command printed under node `node_id`'s Route steps line."""
        block = re.search(rf"^\d+\. {re.escape(node_id)}\b.*\n   {verb}: `([^`]+)`", text, re.M)
        self.assertIsNotNone(block, text)
        return block.group(1)

    def next_ready(self, route, path, finished, successor):
        meta = {"dispatch_depth": "2", "worker_type": "review", "route_file": str(path), "route_id": route["route_id"],
                "route_hash": route["route_hash"], "note": "completed-marker", "route_node": finished,
                "attempt_id": "att-finished"}
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_OUTPUT_DIR": self.output}), \
             mock.patch.object(JOIN, "exact_attempt_row", return_value=SimpleNamespace(metadata=meta)), \
             mock.patch.object(ADVANCE, "completed_nodes", return_value=set()), \
             mock.patch.object(ADVANCE, "started_nodes", return_value=set()), \
             mock.patch.object(ADVANCE, "runnable_successors", return_value=[self.node(route, successor)]):
            return JOIN._next_ready_start_lines({"children": [{"attempt_id": "att-finished"}]}, str(self.jobs))

    def run_printed(self, command, env, **replace):
        for placeholder, value in replace.items():
            command = command.replace(placeholder, value)
        return subprocess.run(shlex.split(command), text=True, capture_output=True, env=env, cwd=self.repo)

    def test_a_measurement_route_prints_the_resource_start_that_runs_as_printed(self):
        route, path = self.compose("autopilot-lab", "setup")
        command = self.step_command(self.route_steps(path), "full-run", "Start")
        argv = shlex.split(command)
        self.assertEqual(Path(argv[1]).name, "resource-runner.py")
        self.assertEqual(argv[argv.index("--route") + 1], str(path))
        self.assertEqual(argv[argv.index("--node") + 1], "full-run")
        self.assertTrue(command.endswith(" -- <approved command>"), command)
        # The receipt after smoke, the read-only check and the stage-start refusal print the same command.
        self.assertEqual(self.next_ready(route, path, "smoke", "full-run"),
                         [f"Next ready: {command}  # start full-run yourself: replace only `<approved command>` "
                          "with the approved payload command"])
        evidence = Path(self.output) / "check.md"
        evidence.write_text("fixture\n")
        checked = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"), "complete",
                                  "--route", str(path), "--node", "full-run", "--evidence", str(evidence), "--check"],
                                 text=True, capture_output=True, env=self.owner_env(route, path), cwd=self.repo)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn(f"start it with `{command}`", json.loads(checked.stdout)["next_action"])
        with self.assertRaises(ValueError) as refused:
            F.load_node(path, "full-run", "start")
        self.assertIn(f"start it yourself: `{command}`", str(refused.exception))
        self.assertNotIn("lacks checked ordered fallback", str(refused.exception))
        # Run it as printed in the owner's environment, the approved payload in place of the placeholder.
        attestation = Path(argv[argv.index("--smoke-attestation") + 1])
        attestation.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, str(ROOT / "tools/smoke-attestation.py"), "attest", "--input",
                        str(self.repo / "README"), "--cwd", str(self.repo), "--output", str(attestation),
                        "--", *PAYLOAD], check=True, capture_output=True, env=self.env)
        started = self.run_printed(command, self.owner_env(route, path), **{"<approved command>": shlex.join(PAYLOAD)})
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        log = Path(argv[argv.index("--log") + 1])
        deadline = time.monotonic() + 20
        while not Path(str(log) + ".exit").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        registry, run_id = argv[argv.index("--registry") + 1], argv[argv.index("--run-id") + 1]
        reaped = subprocess.run([sys.executable, str(ROOT / "utilities/resource-runner.py"), "--registry", registry,
                                 "reap", "--run-id", run_id], text=True, capture_output=True, env=self.env)
        self.assertEqual(reaped.returncode, 0, reaped.stderr)
        self.assertEqual((json.loads(reaped.stdout)["status"], json.loads(reaped.stdout)["exit_code"]), ("succeeded", 0))
        self.assertIn("fixture payload", log.read_text())

    def test_an_unbound_cycle_says_what_the_resource_start_lacks(self):
        route, path = self.compose("autopilot-lab", "setup")
        self.output = ""
        with mock.patch.object(W, "cycle_output_dir", return_value=None):
            lines = self.next_ready(route, path, "smoke", "full-run")
        self.assertEqual(lines, ["Next ready: start full-run yourself; its exact resource-runner.py start needs "
                                 "this route's bound cycle (run registry and log path)."])

    def test_an_owner_run_stage_prints_its_completion_that_runs_as_printed_and_releases_its_successors(self):
        route, path = self.compose("autopilot-ship", "default")
        text = self.route_steps(path)
        command = self.step_command(text, "release-setup", "Complete")
        self.assertEqual(command, W.inline_complete_command("release-setup", None, path))
        self.assertEqual(shlex.split(command)[2:6], ["complete", "--route", str(path), "--inline"])
        # A ship deploy keeps the completion its parent deploy handback prints with the validated record.
        self.assertIn("deploy after security-review, release-review [you run it yourself] → deploy-record.json\n"
                      "5. post-deploy-verify", text)
        with self.assertRaises(ValueError) as refused:
            F.load_node(path, "release-setup", "start")
        self.assertIn(f"complete it yourself: `{command}`", str(refused.exception))
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_OUTPUT_DIR": self.output}), \
             self.assertRaises(GATES.DispatchContractError) as missing:
            GATES.completion_marker_gate(str(path), "security-review", "dry-run", ROOT, self.jobs)
        self.assertEqual(missing.exception.reason, "completion-marker-missing")
        self.assertIn(f"run `{command}`", missing.exception.next_action)
        done = self.run_printed(command, self.owner_env(route, path), **{"<one line>": "setup checklist done"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(json.loads(done.stdout.splitlines()[1])["status"], "unregistered-complete")
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_OUTPUT_DIR": self.output}):
            GATES.completion_marker_gate(str(path), "security-review", "dry-run", ROOT, self.jobs)

    def test_the_receipt_after_a_dispatched_stage_names_the_owner_stage_completion(self):
        route, path = self.compose("autopilot-lab", "eval")
        command = self.step_command(self.route_steps(path), "publish", "Complete")
        evidence = Path(self.output) / "bundle-publication.json"
        self.assertEqual(command, W.inline_complete_command("publish", str(evidence), path))
        self.assertEqual(self.next_ready(route, path, "independent-verify", "publish"),
                         [f"Next ready: {command}  # complete publish yourself: after you have done this stage's "
                          f"work and written {evidence}"])

    def test_the_parent_session_runs_the_same_completion_with_a_route_file_or_id(self):
        route, path = self.compose("autopilot-ship", "default")
        command = self.step_command(self.route_steps(path), "release-setup", "Complete")
        for label, printed in (("file", command), ("id", command.replace(shlex.quote(str(path)), route["route_id"]))):
            with self.subTest(route=label):
                done = self.run_printed(printed, self.parent_env(), **{"<one line>": "setup checklist done"})
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                marker, row = (json.loads(line) for line in done.stdout.splitlines()[:2])
                self.assertEqual(row, {"attempt_id": f"{route['route_id']}-release-setup-inline",
                                       "status": "unregistered-complete"})
                self.assertEqual((marker["registered_worker"], marker["fallback_hop"]), (False, "inline"))
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_OUTPUT_DIR": self.output}):
            GATES.completion_marker_gate(str(path), "security-review", "dry-run", ROOT, self.jobs)


if __name__ == "__main__":
    unittest.main()
