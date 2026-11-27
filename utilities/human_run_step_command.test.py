#!/usr/bin/env python3
"""A step the owner or its parent session runs itself is printed with its exact start or completion command only
when the route's own sealed context (its bound cycle, declared inputs, sealed config manifest) fixes every
argument; otherwise one line says what is missing. Closing authority stays where it was.

Fabricated staged routes of the real recipes, no model: a measurement route (`smoke` then a `resource-runner`
node), lab eval graphs, a ship route whose owner-run setup stage is followed by dispatched reviews, and the parent
session closing stages inline. Expected paths are computed from the route and its cycle record, never read back
from the printed text. The resource payload is a one-line Python print under a fake `nvidia-smi`.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import shlex
import shutil
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
import workflow_state as WS  # noqa: E402
import worker_bootstrap as W  # noqa: E402

PAYLOAD = [sys.executable, "-c", "print('fixture payload')"]
REPLACE = "replace only `<approved command>` with the approved payload command"
ROUTE_TOOL = str(ROOT / "utilities/capability-route.py")


class HumanRunStepCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = self.base = Path(self.tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        for argv in (["init", "-q"], ["config", "user.email", "fixture@example.test"], ["config", "user.name", "F"]):
            subprocess.run(["git", "-C", str(self.repo), *argv], check=True)
        (self.repo / "README").write_text("fixture\n")
        (self.repo / "configs").mkdir()
        (self.repo / "configs" / "train.yaml").write_text("config:\n  lr: 0.001\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "README", "configs"], check=True)
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

    # -- fixtures ------------------------------------------------------------------------------------------

    def evidence(self, parent="codex"):
        def legs(child):
            return {"parent_harness": parent, "parent_transport": "headless",
                    "parent_sandbox": R.WRAPPER_PARENT_SANDBOXES.get(parent, ["workspace-write"])[0],
                    "child_harness": child, "launch_authority": "conductor", "status": "supported",
                    "probe_source": "fixture", "probe_time": "2026-07-16T00:00:00Z", "failure_class": "",
                    "checked_worktree": str(self.repo.resolve()), "failure_scope": "none",
                    "codex_command": "ok" if child == "codex" else "not-applicable", "retry_on_isolated_worktree": 0}
        return {"tuples": [legs("codex"), legs("claude")], "native_subagent": [{
            "harness": "codex", "transport": "headless", "execution_surface": "codex-native-subagent",
            "registered_worker": False, "status": "unsupported", "check_source": "fixture"}]}

    def publish(self, route, bind):
        path = R.canonical_route_path(self.art, route["route_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(route))
        if bind:
            self.cycle_env = artifact_producer.prepare_route_artifact_env(path, start=True, jobs=self.jobs)
            self.output = self.cycle_env["AGENT_ARTIFACT_OUTPUT_DIR"]
        return route, path

    def compose(self, capability, mode, *, bind=True, key="human-step-fixture"):
        """A sealed staged route of the real recipe at its canonical path, its cycle bound unless `bind=False`."""
        gate = {"spec_read": {"satisfied": True, "source": "fixture"}, "drift_verdict": "within-spec",
                "workflow_mode": "tracked", "artifact_guard": {"satisfied": True, "source": "fixture"}}
        route = R.compile_route(capability, mode, "standard", self.repo, self.art, transport="headless",
                                tracking="tracked", tracked_gate_evidence=gate, dispatch_evidence=self.evidence(),
                                frameless=True, campaign_key=key)
        return self.publish(route, bind)

    def compose_graph(self, graph, key):
        route = R.compose_route(
            capability="autopilot-lab", capability_mode="eval", shape="staged", graph=graph, slug="human-step",
            cwd=self.repo, artifact_root=self.art, spec_read="fixture", campaign_key=key, frameless=True,
            dispatch_evidence=self.evidence("claude"), registered_headless_evidence={"candidates": [
                {"harness": h, "transport": "headless", "surface": "registered-headless", "status": "supported",
                 "probe_source": "fixture", "probe_time": "2026-07-20T00:00:00Z"} for h in ("codex", "claude")]})
        return self.publish(route, True)

    def cycle_output(self, route):
        """The route's cycle output directory from its cycle record (independent of anything printed)."""
        record = artifact_producer.route_cycle_for(self.art, route)
        return artifact_producer.cycle_dir(self.art, record["campaign_id"], record["cycle_id"], record) / "artifacts"

    def node(self, route, node_id):
        return next(n for n in route["nodes"] if n["id"] == node_id)

    def owner_env(self, route, path, output=None):
        """The registered owner's environment: its own attempt and route binding (no node), its bound cycle."""
        return {**self.env, "AGENT_DISPATCH_ATTEMPT_ID": unique_attempt_id("att-human-step-owner"),
                "AGENT_ROUTE_FILE": str(path), "AGENT_ROUTE_ID": route["route_id"], "AGENT_ROUTE_NODE": "",
                "AGENT_ARTIFACT_OUTPUT_DIR": output or self.output}

    def parent_env(self):
        """The parent session's environment: no dispatch attempt, only its caller harness."""
        env = {k: v for k, v in self.env.items() if not k.startswith("AGENT_DISPATCH_")}
        return {**env, "AGENT_DISPATCH_CALLER_HARNESS": "claude"}

    def route_steps(self, path, output=None):
        args = SimpleNamespace(worker_type="owner", route_file=str(path), route_node=None)
        return W.owner_route_steps_prompt(args, {"AGENT_ARTIFACT_OUTPUT_DIR": output or self.output})

    def step_line(self, text, node_id):
        """The line printed under node `node_id`'s Route steps line, or None."""
        found = re.search(rf"^\d+\. {re.escape(node_id)}\b.*\n   ((?:start|complete|{re.escape(node_id)})\b.*)$",
                          text, re.M)
        return found.group(1) if found else None

    def command_in(self, line):
        found = re.search(r"yourself: `([^`]+)`", line or "")
        self.assertIsNotNone(found, line)
        return found.group(1)

    def next_ready(self, route, path, finished, successor):
        meta = {"dispatch_depth": "2", "worker_type": "review", "route_file": str(path), "route_id": route["route_id"],
                "route_hash": route["route_hash"], "note": "completed-marker", "route_node": finished,
                "attempt_id": "att-finished"}
        with mock.patch.object(JOIN, "exact_attempt_row", return_value=SimpleNamespace(metadata=meta)), \
             mock.patch.object(ADVANCE, "completed_nodes", return_value=set()), \
             mock.patch.object(ADVANCE, "started_nodes", return_value=set()), \
             mock.patch.object(ADVANCE, "runnable_successors", return_value=[self.node(route, successor)]):
            return JOIN._next_ready_start_lines({"children": [{"attempt_id": "att-finished"}]}, str(self.jobs))

    def run_printed(self, command, env, **replace):
        for placeholder, value in replace.items():
            command = command.replace(placeholder, value)
        return subprocess.run(shlex.split(command), text=True, capture_output=True, env=env, cwd=self.repo)

    def complete(self, *argv, env):
        return subprocess.run([sys.executable, ROUTE_TOOL, "complete", *argv], text=True, capture_output=True,
                              env=env, cwd=self.repo)

    def attest(self, output, manifest=None):
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, str(ROOT / "tools/smoke-attestation.py"), "attest", "--input",
                        str(self.repo / "README"), "--cwd", str(self.repo), "--output", str(output),
                        *(["--config-manifest", str(manifest)] if manifest else []), "--", *PAYLOAD],
                       check=True, capture_output=True, env=self.env)

    def run_to_end(self, command, env, registry, run_id, log):
        started = self.run_printed(command, env, **{"<approved command>": shlex.join(PAYLOAD)})
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        deadline = time.monotonic() + 20
        while not Path(str(log) + ".exit").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        reaped = subprocess.run([sys.executable, str(ROOT / "utilities/resource-runner.py"), "--registry",
                                 str(registry), "reap", "--run-id", run_id], text=True, capture_output=True, env=self.env)
        self.assertEqual(reaped.returncode, 0, reaped.stderr)
        self.assertEqual((json.loads(reaped.stdout)["status"], json.loads(reaped.stdout)["exit_code"]), ("succeeded", 0))
        self.assertIn("fixture payload", Path(log).read_text())
        return json.loads(Path(registry).read_text())["runs"][run_id]

    def status(self, path, *extra):
        result = subprocess.run([sys.executable, str(ROOT / "utilities/workflow-supervisor.py"), "status", "--route",
                                 str(path), "--jobs", str(self.jobs), *extra], text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def expected_argv(self, route, path, out, run_id, attestation):
        return {"--registry": str(out / "resource-runs.json"), "--run-id": run_id, "--cwd": str(self.repo.resolve()),
                "--log": str(out / "logs" / f"{run_id}.log"), "--route": str(path), "--node": "full-run",
                "--smoke-attestation": str(attestation)}

    def assert_argv(self, command, expected, manifest=None):
        argv = shlex.split(command)
        self.assertEqual(Path(argv[1]).name, "resource-runner.py")
        self.assertEqual({flag: argv[argv.index(flag) + 1] for flag in expected}, expected)
        self.assertEqual(argv[argv.index("--config-manifest") + 1] if "--config-manifest" in argv else None, manifest)
        self.assertTrue(command.endswith(" -- <approved command>"), command)

    # -- resource start ------------------------------------------------------------------------------------

    def test_a_measurement_route_prints_one_resource_start_everywhere_and_it_runs_as_printed(self):
        route, path = self.compose("autopilot-lab", "setup")
        out = self.cycle_output(route)
        attestation = out / "reviews" / "smoke-attestation.json"
        self.assertEqual(self.step_line(self.route_steps(path), "full-run"),
                         f"full-run has no start command yet: start it after {attestation} is written: its run id "
                         "and any sealed config manifest follow from it")
        self.attest(attestation)
        line = self.step_line(self.route_steps(path), "full-run")
        command = self.command_in(line)
        self.assertEqual(line, f"start full-run yourself: `{command}` ({REPLACE})")
        run_id = f"{route['route_id']}-full-run"
        self.assert_argv(command, self.expected_argv(route, path, out, run_id, attestation))
        # The receipt after smoke, the read-only check, the stage-start refusal, the run-verify gate and the
        # supervisor status print the same line.
        self.assertEqual(self.next_ready(route, path, "smoke", "full-run"), [f"Next ready: {line}"])
        evidence = out / "check.md"
        evidence.write_text("fixture\n")
        checked = self.complete("--route", str(path), "--node", "full-run", "--evidence", str(evidence), "--check",
                                env=self.owner_env(route, path))
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertTrue(json.loads(checked.stdout)["next_action"].startswith(line + "; "), checked.stdout)
        with self.assertRaises(ValueError) as refused:
            F.load_node(path, "full-run", "start")
        self.assertEqual(str(refused.exception), f"route node full-run is not a dispatched stage; {line}")
        with self.assertRaises(GATES.DispatchContractError) as missing:
            GATES.completion_marker_gate(str(path), "run-verify", "dry-run", ROOT, self.jobs)
        self.assertEqual(missing.exception.reason, "completion-marker-missing")
        self.assertTrue(missing.exception.next_action.endswith("; " + line), missing.exception.next_action)
        ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=str(self.jobs))
        for node_id in ("scaffold", "smoke"):
            ledger.record(node_id, "RUNNING")
            ledger.record(node_id, "STAGE_SUCCEEDED")
        self.assertIn(f"\nnext step  {line}\n", self.status(path))
        self.assertEqual(json.loads(self.status(path, "--json"))["next_steps"], {"full-run": line})
        # Run it as printed in the owner's environment, the approved payload in place of the placeholder.
        self.run_to_end(command, self.owner_env(route, path), out / "resource-runs.json", run_id,
                        out / "logs" / f"{run_id}.log")
        held = W.human_run_step(route, path, self.node(route, "full-run"))
        self.assertTrue(held.startswith(f"full-run has no start command yet: run {run_id} is already succeeded"), held)
        self.assertNotIn("<approved command>", self.route_steps(path))

    def test_a_held_run_id_is_reported_with_its_state_and_a_failed_one_retries_under_its_next_id(self):
        route, path = self.compose("autopilot-lab", "setup")
        out = self.cycle_output(route)
        self.attest(out / "reviews" / "smoke-attestation.json")
        registry, base = out / "resource-runs.json", f"{route['route_id']}-full-run"
        node = self.node(route, "full-run")
        status = shlex.join(["python3", str(ROOT / "utilities/resource-runner.py"), "--registry", str(registry),
                             "status", "--run-id", base])
        for state, word in (("launching", "waiting to start"), ("running", "running"), ("succeeded", "succeeded")):
            with self.subTest(state=state):
                registry.write_text(json.dumps({"schema_version": 1, "runs": {base: {"status": state}}}))
                self.assertEqual(W.human_run_step(route, path, node),
                                 f"full-run has no start command yet: run {base} is already {word} (`{status}`); "
                                 "start nothing new")
        for held, retry in (({base: "failed"}, f"{base}__a1"), ({base: "failed", f"{base}__a1": "failed"}, f"{base}__a2")):
            with self.subTest(retry=retry):
                registry.write_text(json.dumps({"schema_version": 1, "runs": {k: {"status": v} for k, v in held.items()}}))
                line = W.human_run_step(route, path, node)
                self.assertTrue(line.endswith(f"; run {sorted(held)[-1]} failed, so this retries it as {retry})"), line)
                self.assert_argv(self.command_in(line), self.expected_argv(
                    route, path, out, retry, out / "reviews" / "smoke-attestation.json"))
        registry.write_text("{not json")
        self.assertEqual(W.human_run_step(route, path, node),
                         f"full-run has no start command yet: {registry} cannot be read: read the run state with "
                         f"`{status}`")

    def test_the_cycle_comes_from_the_route_itself_never_from_an_inherited_cycle(self):
        bound, bound_path = self.compose("autopilot-lab", "setup", key="bound-a")
        self.attest(self.cycle_output(bound) / "reviews" / "smoke-attestation.json")
        other, other_path = self.compose("autopilot-lab", "setup", bind=False, key="unbound-b")
        owner_a = self.owner_env(bound, bound_path)
        with mock.patch.dict(os.environ, owner_a):
            line = W.human_run_step(other, other_path, self.node(other, "full-run"))
            steps = self.route_steps(other_path, output=self.output)
        unbound = ("full-run has no start command yet: its resource-runner.py start needs this route's bound cycle "
                   "(run registry and log path)")
        self.assertEqual(line, unbound)
        self.assertEqual(self.step_line(steps, "full-run"), unbound)
        evidence = self.base / "check.md"
        evidence.write_text("fixture\n")
        checked = self.complete("--route", str(other_path), "--node", "full-run", "--evidence", str(evidence),
                                "--check", env=owner_a)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertTrue(json.loads(checked.stdout)["next_action"].startswith(unbound + "; "), checked.stdout)
        self.assertNotIn(self.output, checked.stdout)
        # A ship route with no bound cycle: its setup completion would be refused (`inline-evidence-no-bound-cycle`),
        # so no command is printed.
        ship, ship_path = self.compose("autopilot-ship", "default", bind=False, key="unbound-ship")
        self.assertEqual(self.step_line(self.route_steps(ship_path, output="/nonexistent"), "release-setup"),
                         "release-setup has no completion command yet: its evidence needs this route's bound cycle")

    def test_the_smoke_attestation_comes_from_the_declared_input(self):
        route, path = self.compose("autopilot-lab", "eval", key="eval-default")
        self.assertEqual(W.human_run_step(route, path, self.node(route, "eval-run")),
                         "eval-run has no start command yet: its `smoke-attestation` input is not produced by this "
                         "route: give `resource-runner.py start` your hash-bound smoke attestation as "
                         "`--smoke-attestation <file>`")
        # A graph with eval-smoke reads the attestation that stage writes in this cycle (`part_io`).
        route, path = self.compose_graph("eval-smoke,eval-run,metrics,report", "eval-smoke")
        attestation = self.cycle_output(route) / "reviews" / "smoke-attestation.json"
        self.assertIn(f"start it after {attestation} is written", W.human_run_step(route, path, self.node(route, "eval-run")))
        self.attest(attestation)
        argv = shlex.split(self.command_in(W.human_run_step(route, path, self.node(route, "eval-run"))))
        self.assertEqual(argv[argv.index("--smoke-attestation") + 1], str(attestation))
        # A graph without it reads the prior cycle's attestation compose sealed as an input source.
        prior, _ = self.compose("autopilot-lab", "eval", key="eval-fill")
        earlier = self.cycle_output(prior) / "reviews" / "smoke-attestation.json"
        self.attest(earlier)
        (earlier.parent.parent / "eval-spec.md").write_text("fixture\n")
        route, path = self.compose_graph("eval-run,metrics,report", "eval-fill")
        self.assertIn("smoke-attestation", self.node(route, "eval-run")["input_sources"])
        argv = shlex.split(self.command_in(W.human_run_step(route, path, self.node(route, "eval-run"))))
        self.assertEqual(Path(argv[argv.index("--smoke-attestation") + 1]).resolve(), earlier.resolve())

    def test_a_sealed_config_prints_its_run_id_and_manifest_and_the_run_keeps_its_provenance(self):
        route, path = self.compose("autopilot-lab", "setup")
        out = self.cycle_output(route)
        sealed = subprocess.run([sys.executable, str(ROOT / "tools/lab-config-provenance.py"), "seal", "--repo",
                                 str(self.repo), "--config", "configs/train.yaml", "--slug", "fixture-run", "--artifact-root",
                                 str(self.art)], text=True, capture_output=True, cwd=self.repo,
                                env={**self.env, **self.cycle_env, "AGENT_ROUTE_FILE": str(path),
                                     "AGENT_ROUTE_ID": route["route_id"], "AGENT_ROUTE_NODE": "smoke"})
        self.assertEqual(sealed.returncode, 0, sealed.stderr)
        manifest = json.loads(sealed.stdout)
        manifest_path = Path(manifest["snapshot_path"]).parent / f"{manifest['run_id']}.manifest.json"
        attestation = out / "reviews" / "smoke-attestation.json"
        self.attest(attestation, manifest_path)
        line = W.human_run_step(route, path, self.node(route, "full-run"))
        command = self.command_in(line)
        self.assert_argv(command, self.expected_argv(route, path, out, manifest["run_id"], attestation),
                         manifest=str(manifest_path))
        row = self.run_to_end(command, self.owner_env(route, path), out / "resource-runs.json", manifest["run_id"],
                              out / "logs" / f"{manifest['run_id']}.log")
        self.assertEqual({key: row.get(key) for key in ("config_ref", "config_sha256", "source_commit", "source_dirty",
                                                        "config_layout")},
                         {"config_ref": manifest["config_ref"], "config_sha256": manifest["snapshot_sha256"],
                          "source_commit": manifest["source_commit"], "source_dirty": manifest["source_dirty"],
                          "config_layout": manifest["config_layout"]})
        # Two sealed manifests for the attested snapshot: which one is not settled, so no command is printed.
        twin = manifest_path.with_name("fixture-twin.manifest.json")
        twin.write_text(json.dumps({**manifest, "run_id": "fixture-twin"}))
        (out / "resource-runs.json").unlink()
        self.assertEqual(W.human_run_step(route, path, self.node(route, "full-run")),
                         f"full-run has no start command yet: {attestation} is bound to a sealed config manifest "
                         "(S3-2b): start it with that manifest's run id as `--run-id` and add "
                         "`--config-manifest <its manifest>`")

    def test_the_read_only_check_keeps_its_answer_when_the_step_text_cannot_be_imported(self):
        route, path = self.compose("autopilot-lab", "setup")
        evidence = self.cycle_output(route) / "check.md"
        evidence.write_text("fixture\n")
        argv = ["capability-route.py", "complete", "--route", str(path), "--node", "full-run", "--evidence",
                str(evidence), "--check"]
        printed = io.StringIO()
        with mock.patch.dict(sys.modules, {"worker_bootstrap": None}), mock.patch.object(sys, "argv", argv), \
             contextlib.redirect_stdout(printed):
            R.main()
        answer = json.loads(printed.getvalue())
        self.assertEqual((answer["result"], answer["reason"]), ("not-applicable", "owner-closure-review-nodes-only"))
        self.assertTrue(answer["next_action"].startswith("use the resource node's existing start path; "), answer)

    # -- owner-run stages and the parent session -------------------------------------------------------------

    def test_an_owner_run_stage_prints_its_completion_that_runs_as_printed_and_releases_its_successors(self):
        route, path = self.compose("autopilot-ship", "default")
        text = self.route_steps(path)
        line = self.step_line(text, "release-setup")
        command = self.command_in(line)
        self.assertEqual(command, W.inline_complete_command("release-setup", None, path))
        self.assertEqual(shlex.split(command)[2:6], ["complete", "--route", str(path), "--inline"])
        # Deploy: with no parent deploy request the owner completes it with the cycle's deploy record; with one, the
        # parent deploy handback prints its completion.
        out = self.cycle_output(route)
        self.assertEqual(self.command_in(self.step_line(text, "deploy")),
                         W.inline_complete_command("deploy", str(out / "deploy-record.json"), path))
        request = self.jobs.resolve().parent / "ship-parent-deploy" / f"{route['route_id']}.json"
        request.parent.mkdir()
        request.write_text("{}")
        self.assertEqual(self.step_line(self.route_steps(path), "deploy"),
                         f"complete deploy with the command its parent deploy handback prints (beside {request})")
        request.unlink()
        with self.assertRaises(ValueError) as refused:
            F.load_node(path, "release-setup", "start")
        self.assertEqual(str(refused.exception), f"route node release-setup is not a dispatched stage; {line}")
        with self.assertRaises(GATES.DispatchContractError) as missing:
            GATES.completion_marker_gate(str(path), "security-review", "dry-run", ROOT, self.jobs)
        self.assertEqual(missing.exception.reason, "completion-marker-missing")
        self.assertIn(f"release-setup is run by the route owner itself: {line}", missing.exception.next_action)
        self.assertIn(f"\nnext step  {line}\n", self.status(path))
        self.assertEqual(json.loads(self.status(path, "--json"))["next_steps"], {"release-setup": line})
        done = self.run_printed(command, self.owner_env(route, path), **{"<one line>": "setup checklist done"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(json.loads(done.stdout.splitlines()[1])["status"], "unregistered-complete")
        GATES.completion_marker_gate(str(path), "security-review", "dry-run", ROOT, self.jobs)

    def test_the_receipt_after_a_dispatched_stage_names_the_owner_stage_completion(self):
        route, path = self.compose("autopilot-lab", "eval")
        evidence = self.cycle_output(route) / "bundle-publication.json"
        line = self.step_line(self.route_steps(path), "publish")
        self.assertEqual(line, f"complete publish yourself: `{W.inline_complete_command('publish', str(evidence), path)}` "
                               f"(after you have done this stage's work and written {evidence})")
        self.assertEqual(self.next_ready(route, path, "independent-verify", "publish"), [f"Next ready: {line}"])

    def test_the_parent_session_closes_only_owner_run_stages_without_an_attempt_id(self):
        ship, ship_path = self.compose("autopilot-ship", "default", key="parent-ship")
        ship_out = self.output
        setup, setup_path = self.compose("autopilot-lab", "setup", key="parent-setup")
        setup_out = self.output
        evaluation, eval_path = self.compose("autopilot-lab", "eval", key="parent-eval")
        eval_out = self.output
        # A registered reviewer already closed security-review: its marker is the current one.
        node = self.node(ship, "security-review")
        meta = ",".join(["attempt_schema_version=2", "dispatch_depth=2", "transport=headless",
                         "execution_surface=registered-headless", "registered_worker=1",
                         "fallback_hop=same-harness-headless", "attempt_id=att-human-step-reviewer",
                         "worker_type=review", f"route_id={ship['route_id']}", f"route_hash={ship['route_hash']}",
                         "route_node=security-review"])
        with self.jobs.open("a") as handle:
            handle.write("\t".join(["2026-11-27T00:00:00Z", "running", "repo", "worktree", "slug", meta]) + "\n")
        verdict = Path(ship_out) / "reviews" / "security-verdict.json"
        verdict.parent.mkdir(parents=True, exist_ok=True)
        verdict.write_text('{"verdict": "PASS"}\n')
        R.complete_node(ship, node, "security-review", verdict.resolve(), jobs=str(self.jobs),
                        attempt_id="att-human-step-reviewer",
                        review_claim={"kind": "registered-worker", "attempt_id": "att-human-step-reviewer"})
        current = R.completion_dir(ship["route_id"], jobs=self.jobs) / "security-review.json"
        before = current.read_bytes()
        self.assertEqual(json.loads(before)["review_independence"], "independent")
        for (route, path, out), node_id in (((ship, ship_path, ship_out), "security-review"),
                                            ((setup, setup_path, setup_out), "scaffold"),
                                            ((ship, ship_path, ship_out), "post-deploy-verify"),
                                            ((evaluation, eval_path, eval_out), "sync")):
            with self.subTest(node=node_id):
                evidence = Path(out) / f"{node_id}-parent.md"
                evidence.write_text("fixture\n")
                argv = ["--route", str(path), "--inline", "--node", node_id, "--evidence", str(evidence),
                        "--reason", "parent closes it"]
                implicit = self.complete(*argv, env=self.parent_env())
                self.assertNotEqual(implicit.returncode, 0, implicit.stdout)
                self.assertIn("explicit attempt metadata requires --attempt-id", implicit.stderr)
                if node_id == "security-review":
                    self.assertEqual(current.read_bytes(), before)
                    continue
                explicit = self.complete(*argv, "--attempt-id", f"att-parent-{node_id}", env=self.parent_env())
                self.assertEqual(explicit.returncode, 0, explicit.stderr)
                self.assertEqual(json.loads(explicit.stdout.splitlines()[1]),
                                 {"attempt_id": f"att-parent-{node_id}", "status": "unregistered-complete"})
        # The stage its Route steps tell the parent to close runs as printed, by file and by exact id.
        command = self.command_in(self.step_line(self.route_steps(ship_path, output=ship_out), "release-setup"))
        for label, printed in (("file", command), ("id", command.replace(shlex.quote(str(ship_path)), ship["route_id"]))):
            with self.subTest(route=label):
                done = self.run_printed(printed, self.parent_env(), **{"<one line>": "setup checklist done"})
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                marker, row = (json.loads(line) for line in done.stdout.splitlines()[:2])
                self.assertEqual(row, {"attempt_id": f"{ship['route_id']}-release-setup-inline",
                                       "status": "unregistered-complete"})
                self.assertEqual((marker["registered_worker"], marker["fallback_hop"]), (False, "inline"))

    def test_complete_takes_only_a_route_file_or_its_exact_id(self):
        route, path = self.compose("autopilot-ship", "default", key="id-a")
        other, other_path = self.compose("autopilot-ship", "default", key="id-b")
        other_out = self.cycle_output(other)
        base = ["--inline", "--node", "release-setup", "--reason", "setup checklist done"]
        alias = "att-human-step-alias"
        with self.jobs.open("a") as handle:
            handle.write("\t".join(["2026-11-27T00:00:00Z", "running", "repo", "worktree", "slug",
                                    f"attempt_id={alias},route_id={route['route_id']},route_file={path}"]) + "\n")
        swapped = R.canonical_route_path(self.art, "rt-" + "f" * 16)
        shutil.copyfile(other_path, swapped)
        for given, reason in ((str(self.base / "absent" / "route.json"), "route-argument-not-file-or-id"),
                              ("rt-" + "0" * 16, "route-id-unresolved"),
                              (swapped.name[:-5], "route-id-mismatch"),
                              (alias, "route-argument-not-file-or-id")):
            with self.subTest(route=given):
                refused = self.complete("--route", given, *base, env=self.parent_env())
                self.assertNotEqual(refused.returncode, 0, refused.stdout)
                self.assertIn(reason, refused.stderr)
        # An owner bound to route A cannot close route B's stage, by B's id or file, and leaves B no evidence.
        for given in (other["route_id"], str(other_path)):
            with self.subTest(owner_gives=given):
                refused = self.complete("--route", given, *base, env=self.owner_env(route, path))
                self.assertNotEqual(refused.returncode, 0, refused.stdout)
                self.assertIn("route-binding-mismatch", refused.stderr)
        self.assertEqual(sorted(p.name for p in other_out.glob("*.inline.md")), [])
        self.assertFalse((R.completion_dir(other["route_id"], jobs=self.jobs) / "release-setup.json").exists())


if __name__ == "__main__":
    unittest.main()
