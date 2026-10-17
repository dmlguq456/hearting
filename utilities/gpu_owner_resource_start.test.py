#!/usr/bin/env python3
"""Normal GPU compose/start with real isolated grants, claims and resource exits.

Model payloads, runtime bootstrap observations and readiness command observations
are substituted. The native GPU query is a separate, optional validation caller;
CI does not assume that its host has NVIDIA devices.
"""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import unittest
from unittest import mock

from execution_access import load_parent_effective_grant
import gpu_execution_sandbox as G
import work_start as W

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


O = load("gpu_owner_fixture", "utilities/execution_access_owner_start.test.py")
U = load("gpu_owner_selector", "utilities/dispatch-owner.py")
D = load("gpu_start_node", "utilities/dispatch-node.py")
P = load("gpu_start_readiness", "utilities/dispatch-readiness.py")


class GpuOwnerResourceStartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        O.OwnerGrantStartTest.setUpClass()

    def setUp(self):
        self.fixture = O.OwnerGrantStartTest("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture
        self.c = self.f.wrappers["codex"]
        self.r = self.f.route_api
        self.launches = []
        fakebin = self.f.root / "fake-bin"
        fakebin.mkdir()
        smi = fakebin / "nvidia-smi"
        smi.write_text("#!/bin/sh\nprintf '0, GPU-TEST, NVIDIA Test GPU, 0, 40960, 0\\n'\n")
        smi.chmod(0o755)
        self.f.env["PATH"] = str(fakebin) + os.pathsep + self.f.env.get("PATH", "")
        inventory = self.f.root / "compute-hosts.yaml"
        inventory.write_text(f"schema_version: 1\nrun_root: {self.f.data}\nhosts:\n  fixture:\n    ssh_host: local\n")
        self.f.env["COMPUTE_HOSTS_CONFIG"] = str(inventory)
        self.f.env["AGENT_RESOURCE_RUN_INDEX"] = str(self.f.root / "resource-index.json")

    def readiness(self, cwd, jobs, parent, children, *, gpu_route=None, owner_harnesses=None):
        with mock.patch.object(P.NESTED, "command_check", return_value=("supported", "fixture", "")), \
             mock.patch.object(P.NESTED, "prospective_owner_registry_check", return_value=(True, "")):
            return P.generate(worktree=Path(cwd), jobs=Path(jobs),
                owner_harnesses=list(owner_harnesses or [parent]), child_harnesses=children,
                codex_execution_selection=G.select(gpu_route))

    def compose(self):
        with mock.patch.dict(os.environ, self.f.env, clear=True), \
             mock.patch.object(self.r, "_compose_readiness", side_effect=self.readiness):
            route = self.r.compose_route(capability="autopilot-lab", capability_mode="setup",
                shape="staged", graph=None, slug="gpu-normal-start", cwd=str(self.f.worktree),
                artifact_root=str(self.f.artifacts), signals=["gpu"], spec_read="fixture",
                campaign_key="gpu-normal-start", parent_harness="codex", children=["codex"],
                work_request={"text": "Read-only GPU query; no training", "owner_harness": "codex"}, jobs=self.f.jobs)
        path = self.r.canonical_route_path(self.f.artifacts, route["route_id"])
        self.r.write_once(path, route)
        return path, route

    def wrapper(self, argv, env):
        c, f = self.c, self.f
        attempt = argv[argv.index("--attempt-id") + 1]
        marker, release = f.root / (attempt + ".started"), f.root / (attempt + ".release")
        command = shlex.join([sys.executable, str(f.worker), str(marker), str(release)])
        stdout, stderr = io.StringIO(), io.StringIO()
        def payload(args, *_):
            self.launches.append({"attempt_id": attempt, "sandbox": c.effective_runtime_sandbox(args),
                "gpu_scope": args.gpu_execution_scope, "argv": list(argv)})
            return command
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(redirect_stdout(stdout)); stack.enter_context(redirect_stderr(stderr))
            stack.enter_context(mock.patch.object(c, "check_runtime_projection", return_value=0))
            stack.enter_context(mock.patch.object(c, "prepare_nested_codex_home", return_value=f.home / ".codex"))
            stack.enter_context(mock.patch.object(c, "_core_grounding_dir", return_value=f.root / "bootstrap/core"))
            stack.enter_context(mock.patch.object(c.shutil, "which", return_value=sys.executable))
            stack.enter_context(mock.patch.object(c, "resolve_artifact_root", return_value=str(f.artifacts)))
            stack.enter_context(mock.patch.object(c, "shell_command", side_effect=payload))
            stack.enter_context(mock.patch.object(c, "attach_summary_owner", return_value={}))
            stack.enter_context(mock.patch.object(c, "owner_frame_launch_gate"))
            stack.enter_context(mock.patch.object(c, "completion_marker_gate"))
            stack.enter_context(mock.patch("dispatch_lifecycle.pid_namespace_evidence", return_value={
                "lifecycle_selector_source": "host-like", "lifecycle_nspid_width": "1",
                "lifecycle_pid1_class": "system-init",
            }))
            for name in ("launch_orphan_watch", "launch_reap_watch"):
                stack.enter_context(mock.patch.object(c, name, return_value=os.getpid()))
            def sidecar(args, _):
                args.managed_sidecar_state, args.managed_sidecar_reason = "not-started", "-"
                args.managed_sidecar_pid = args.managed_sealed_batch_id = args.managed_sidecar_log = "-"
            stack.enter_context(mock.patch.object(c, "launch_parent_completion_sidecar", side_effect=sidecar))
            result = c.main(["dispatch-headless.py", *argv])
        output = stdout.getvalue() + stderr.getvalue()
        if result == 0:
            _, row = f.row(attempt)
            f.processes.append(int(row["pid"]))
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists(), output)
        return subprocess.CompletedProcess(argv, result, stdout.getvalue(), stderr.getvalue())

    def owner(self, path, route):
        def run(command, **_):
            stdout, stderr = io.StringIO(), io.StringIO()
            def launch(argv, env):
                result = self.wrapper(argv[1:], env)
                print(result.stdout, end="")
                print(result.stderr, end="", file=sys.stderr)
                return result.returncode
            with redirect_stdout(stdout), redirect_stderr(stderr), \
                 mock.patch.object(U, "_usage", return_value={h: "ok" for h in U._defaults.DISPATCHABLE_HARNESSES}), \
                 mock.patch.object(U._capacity, "capacity_report", return_value={
                     "scores": {h: 100 for h in U._defaults.DISPATCHABLE_HARNESSES}, "sources": {}}), \
                 mock.patch.object(U, "run_owner_wrapper", side_effect=launch):
                result = U.main(command[2:])
            return subprocess.CompletedProcess(command, result, stdout.getvalue(), stderr.getvalue())
        with mock.patch.dict(os.environ, self.f.env, clear=True):
            return W._start(route, path, self.f.jobs, "owner", "codex", run)

    def child(self, path, route, owner, request):
        node = next(n for n in route["nodes"] if n["id"] == "scaffold")
        fields, _row = self.f.row(owner)
        parent = {"parent_harness": "codex", "parent_transport": "headless", "parent_sandbox": "danger-full-access"}
        checked = D.resolve_checked_tuple(route, node, "codex", parent).tuple_row
        attempt = "att-" + "d" * 32
        argv = ["--start", "--worktree", str(self.f.worktree), "--jobs", str(self.f.jobs),
            "--log-dir", str(self.f.state / "logs"), "--slug", attempt, "--attempt-id", attempt,
            "--capability", "autopilot-lab", "--capability-mode", "setup", "--intensity", "standard", "--qa", "standard",
            "--owner-harness", "codex", "--completion-delivery", "poll", "--execution-access-file", str(request),
            "--dispatch-depth", "2", "--worker-type", "stage", "--parent", fields[4], "--parent-attempt-id", owner,
            "--parent-harness", "codex", "--parent-transport", "headless", "--parent-sandbox", checked["parent_sandbox"],
            "--nested-eligibility", "supported", "--eligibility-source", "isolated-fixture",
            "--route-file", str(path), "--route-id", route["route_id"], "--route-hash", route["route_hash"],
            "--route-node", node["id"], "--unit", node["unit"], "--worker-mode", node["unit"],
            "--registry-digest", route["registry_digest"], "--write-scope", ";".join(node["write_scope"]),
            "--completion-gate", node["completion_gate"], "--model-role", node["role"], "--model-profile", node["model_profile"]]
        env = {**self.f.env, "AGENT_DISPATCH_ATTEMPT_ID": owner, "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
            "AGENT_DISPATCH_CALLER_HARNESS": "codex", "AGENT_DISPATCH_CURRENT_SANDBOX": "danger-full-access",
            "AGENT_NESTED_HEADLESS_NETWORK": "1"}
        return attempt, self.wrapper(argv, env)

    def exercise(self, query_command=None):
        path, route = self.compose()
        original = path.read_bytes()
        launched = self.owner(path, route)
        self.assertEqual(0, launched["exit_code"], launched)
        self.assertIn("started=1", launched["receipt"])
        owner = launched["attempt_id"]
        _, row = self.f.row(owner)
        record = json.loads(Path(row["execution_access_effective_file"]).read_text())
        self.assertEqual(route["route_id"], record["route_id"])
        self.assertEqual("danger-full-access", record["sandbox"])
        self.assertEqual("logical-request", record["boundary"])
        self.assertEqual(route["codex_execution_sandbox"], record["execution_sandbox_selection"])
        self.assertFalse(record["os_filesystem_enforced"])
        self.assertFalse(record["os_network_enforced"])
        self.assertIn(str(self.f.data), record["writable_roots"])
        parent = load_parent_effective_grant(jobs=self.f.jobs, parent_attempt_id=owner, context=self.f.context)
        self.assertIn(self.f.data, parent.writable_roots)
        child_request = self.f.request("child", self.f.data / "child")
        child, result = self.child(path, route, owner, child_request)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(["danger-full-access", "workspace-write"], [r["sandbox"] for r in self.launches])
        # A normal scaffold child keeps its original sandbox; the typed GPU
        # resource is launched by its full-access owner after scaffold work.
        registry, log, attestation = self.f.root / "resource.json", self.f.root / "query.log", self.f.root / "smoke.json"
        env = dict(self.f.env)
        subprocess.run([sys.executable, str(ROOT / "tools/smoke-attestation.py"), "attest",
            "--input", str(self.f.worktree / "README"), "--cwd", str(self.f.worktree), "--output", str(attestation),
            "--", sys.executable, "-c", "pass"], check=True, capture_output=True, env=env)
        query = (query_command(G.select(route, owner=False, node="full-run")) if callable(query_command)
                 else query_command) or [sys.executable, "-c", "print('read-only resource fixture')"]
        argv = [sys.executable, str(ROOT / "utilities/resource-runner.py"), "--registry", str(registry), "start",
            "--run-id", "gpu-query", "--cwd", str(self.f.worktree), "--log", str(log), "--route", str(path),
            "--node", "full-run", "--parent-attempt-id", owner, "--smoke-attestation", str(attestation), "--", *query]
        started = subprocess.run(argv, capture_output=True, text=True, env=env)
        self.assertEqual(0, started.returncode, started.stderr)
        deadline = time.monotonic() + 10
        while not Path(str(log) + ".exit").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        settled = subprocess.run([sys.executable, str(ROOT / "utilities/resource-runner.py"), "--registry", str(registry),
            "reap", "--run-id", "gpu-query"], capture_output=True, text=True, env=env)
        self.assertEqual(0, settled.returncode, settled.stderr)
        resource = json.loads(settled.stdout)
        self.assertEqual(("succeeded", 0, owner), (resource["status"], resource["exit_code"], resource["parent_attempt_id"]))
        self.assertEqual(original, path.read_bytes())
        return {"route": route, "owner_receipt": launched, "owner_effective": record,
            "child_attempt": child, "child_receipt": result.stdout, "launches": self.launches,
            "resource": resource, "resource_stdout": log.read_text(), "resource_argv": argv}

    def test_normal_gpu_compose_owner_child_resource_without_access_file(self):
        self.exercise()

    def test_explicit_full_owner_record_matches_and_normal_child_still_refuses_full_grant(self):
        self.f.env["CODEX_DISPATCH_SANDBOX"] = "danger-full-access"
        path, route = self.compose()
        self.assertEqual("caller-env", route["codex_execution_sandbox"]["source"])
        launched = self.owner(path, route)
        self.assertEqual(0, launched["exit_code"], launched)
        _, row = self.f.row(launched["attempt_id"])
        effective = json.loads(Path(row["execution_access_effective_file"]).read_text())
        self.assertEqual("danger-full-access", effective["sandbox"])
        self.assertEqual("none", effective["file_enforcement"])
        self.assertEqual("caller-env", effective["execution_sandbox_selection"]["source"])
        self.assertEqual("danger-full-access", self.launches[0]["sandbox"])
        child, result = self.child(path, route, launched["attempt_id"], self.f.request("normal-child"))
        self.assertEqual(64, result.returncode, result.stdout + result.stderr)
        self.assertIn("execution-access-enforcement-unavailable:codex-file-sandbox", result.stdout)
        self.assertNotIn("attempt_id=" + child + ",", self.f.jobs.read_text())

    def test_forced_workspace_choice_reaches_normal_owner_grant_and_launch(self):
        self.f.env["CODEX_DISPATCH_SANDBOX_FORCE"] = "workspace-write"
        path, route = self.compose()
        tuples = route["dispatch_evidence"]["tuples"]
        codex_rows = [row for row in tuples if row["parent_harness"] == "codex"]
        other_rows = [row for row in tuples if row["parent_harness"] != "codex"]
        self.assertTrue(codex_rows)
        self.assertTrue(all(row["parent_sandbox"] == "workspace-write" for row in codex_rows))
        self.assertTrue(all(row["parent_sandbox"] ==
                            self.r.WRAPPER_PARENT_SANDBOXES[row["parent_harness"]][0]
                            for row in other_rows))
        launched = self.owner(path, route)
        self.assertEqual(0, launched["exit_code"], launched)
        _, row = self.f.row(launched["attempt_id"])
        effective = json.loads(Path(row["execution_access_effective_file"]).read_text())
        self.assertEqual("workspace-write", effective["sandbox"])
        self.assertEqual("os-sandbox", effective["file_enforcement"])
        self.assertEqual("forced-env", effective["execution_sandbox_selection"]["source"])
        self.assertEqual("workspace-write", self.launches[0]["sandbox"])

    def test_explicit_strict_request_is_not_lowered_by_implicit_lab_storage(self):
        request = self.f.request("strict")
        body = json.loads(request.read_text())
        body["enforcement_required"] = "os-sandbox"
        request.write_text(json.dumps(body))
        original = request.read_bytes()
        self.f.env["AGENT_DISPATCH_EXECUTION_ACCESS_FILE"] = str(request)
        path, route = self.compose()
        launched = self.owner(path, route)
        self.assertEqual(64, launched["exit_code"], launched)
        self.assertIn("execution-access-enforcement-unavailable:codex-file-sandbox", launched["receipt"])
        self.assertIn("child_spawned=0", launched["receipt"])
        self.assertEqual([], self.launches)
        self.assertEqual(original, request.read_bytes())
        self.assertEqual("", self.f.jobs.read_text())


if __name__ == "__main__":
    unittest.main()
