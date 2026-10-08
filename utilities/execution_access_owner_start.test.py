#!/usr/bin/env python3
"""Exercise effective grants through real, isolated wrapper starts.

The model payload, runtime bootstrap/observations and frame readiness are substituted.
Route/request validation, registration, fencing, spawning and grant I/O are real.
"""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout, redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from execution_access import (
    AccessContext, ExecutionAccessError, assert_within_parent, load_request,
    load_parent_effective_grant,
)

ROOT = Path(__file__).resolve().parents[1]
OWNER = "att-" + "a" * 32
CHILD = "att-" + "b" * 32
DEFAULT = "att-" + "c" * 32


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class OwnerGrantStartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.route_api = load_module("owner_grant_route", ROOT / "utilities/capability-route.py")
        cls.wrappers = {
            adapter: load_module("owner_grant_" + adapter,
                                 ROOT / "adapters" / adapter / "bin/dispatch-headless.py")
            for adapter in ("codex", "claude", "opencode")
        }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / "worktree"
        self.artifacts = self.root / "artifacts"
        self.home = self.root / "home"
        self.state = self.root / "state/dispatch"
        self.data = self.root / "data/output"
        for path in (self.worktree, self.artifacts, self.home, self.state, self.data):
            path.mkdir(parents=True)
        (self.home / ".codex").mkdir()
        self.jobs = self.state / "jobs.log"
        self.jobs.touch()
        for args in (("init", "-q"), ("config", "user.email", "fixture@example.com"),
                     ("config", "user.name", "Fixture")):
            subprocess.run(["git", "-C", str(self.worktree), *args], check=True)
        (self.worktree / "README").write_text("fixture\n")
        subprocess.run(["git", "-C", str(self.worktree), "add", "README"], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "commit", "-qm", "fixture"], check=True)
        self.env = {
            "PATH": os.environ["PATH"], "HOME": str(self.home), "AGENT_HOME": str(ROOT),
            "CODEX_HOME": str(self.home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "share"),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_DISPATCH_PARENT_SESSION_ID": "fixture-session",
            "AGENT_DISPATCH_CURRENT_HARNESS": "claude",
            "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
            "AGENT_DISPATCH_CURRENT_SANDBOX": "default",
            "AGENT_DISPATCH_CALLER_HARNESS": "claude",
            "HEARTING_WORKFLOW_GROUP_REVIEW": "off",
        }
        self.context = AccessContext.build(
            worktree=self.worktree, artifact_root=self.artifacts,
            dispatch_state_root=self.state, agent_home=ROOT, environ=self.env,
        )
        self.worker = self.root / "worker.py"
        self.worker.write_text(
            "import os,pathlib,sys,time\n"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
            "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)\n"
        )
        self.processes = []
        self.addCleanup(self.stop_workers)

    def stop_workers(self):
        for pid in self.processes:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass

    def request(self, name, root=None, network=False):
        root = root or self.data
        path = self.root / (name + ".json")
        path.write_text(json.dumps({
            "schema_version": 1, "writable_roots": [str(root)], "read_roots": [],
            "network": {"required": network, "reason": "fixture" if network else "", "hosts": []},
            "enforcement_required": "any", "justification": {str(root): "fixture output"},
        }))
        return path

    def route(self, adapter, child_adapter="codex"):
        gate = {"spec_read": {"satisfied": True, "source": "isolated-fixture"},
                "drift_verdict": "within-spec", "workflow_mode": "tracked",
                "artifact_guard": {"satisfied": True, "source": "isolated-fixture"}}
        dispatch = {"tuples": [{
            "parent_harness": adapter, "parent_transport": "headless",
            "parent_sandbox": "workspace-write" if adapter == "codex" else "adapter-default",
            "child_harness": child_adapter, "launch_authority": "conductor",
            "status": "supported", "probe_source": "isolated-fixture",
            "probe_time": "2026-10-04T00:00:00Z", "failure_class": "",
            "checked_worktree": str(self.worktree), "failure_scope": "none",
            "codex_command": "ok", "retry_on_isolated_worktree": 0,
        }], "native_subagent": []}
        with mock.patch.dict(os.environ, self.env, clear=True):
            route = self.route_api.compile_route(
                "autopilot-code", "debug", "standard", self.worktree, self.artifacts,
                signals=["shared-contract"], transport="headless", tracking="tracked",
                tracked_gate_evidence=gate, dispatch_evidence=dispatch,
                slug="owner-grant", campaign_key="isolated-owner-grant",
            )
        path = self.artifacts / ".runtime/routes" / (route["route_id"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(route))
        return path, route

    def row(self, attempt):
        for line in self.jobs.read_text().splitlines():
            fields = line.split("\t", 5)
            metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
            if metadata.get("attempt_id") == attempt:
                return fields, metadata
        self.fail("attempt has no row: " + attempt)

    def start(self, adapter, attempt, request, *, route=None, owner=None,
              action="start", parent_adapter="codex"):
        wrapper = self.wrappers[adapter]
        marker = self.root / (attempt + ".started")
        release = self.root / (attempt + ".release")
        command = shlex.join([sys.executable, str(self.worker), str(marker), str(release)])
        env = dict(self.env)
        argv = ["dispatch-headless.py", "--" + action, "--worktree", str(self.worktree),
                "--jobs", str(self.jobs), "--log-dir", str(self.state / "logs"),
                "--slug", attempt, "--attempt-id", attempt, "--capability", "autopilot-code",
                "--capability-mode", "debug", "--intensity", "standard", "--qa", "standard",
                "--owner-harness", adapter]
        if request is not None:
            argv += ["--execution-access-file", str(request)]
        if adapter != "opencode":
            argv += ["--completion-delivery", "poll"]
        path, record = route
        if owner is None:
            argv += ["--dispatch-depth", "1", "--worker-type", "owner", "--unit", "_kernel/owner",
                     "--model-role", "_kernel/owner", "--model-profile", "deep"]
            env.update(AGENT_OWNER_ROUTE_FILE=str(path), AGENT_OWNER_ROUTE_ID=record["route_id"],
                       AGENT_OWNER_ROUTE_HASH=record["route_hash"])
        else:
            node = next(node for node in record["nodes"] if node["id"] == "plan")
            parent_sandbox = "workspace-write" if parent_adapter == "codex" else "adapter-default"
            argv += ["--dispatch-depth", "2", "--worker-type", "stage", "--parent", owner,
                     "--parent-attempt-id", owner, "--parent-harness", parent_adapter,
                     "--parent-transport", "headless", "--parent-sandbox", parent_sandbox,
                     "--nested-eligibility", "supported", "--eligibility-source", "isolated-fixture",
                     "--route-file", str(path), "--route-id", record["route_id"],
                     "--route-hash", record["route_hash"], "--route-node", node["id"],
                     "--unit", node["unit"], "--worker-mode", node["unit"],
                     "--registry-digest", record["registry_digest"],
                     "--write-scope", ";".join(node["write_scope"]),
                     "--completion-gate", node["completion_gate"]]
            argv += ["--model-role", node["role"], "--model-profile", node["model_profile"]]
            env.update(AGENT_DISPATCH_ATTEMPT_ID=owner,
                       AGENT_DISPATCH_CURRENT_HARNESS=parent_adapter,
                       AGENT_DISPATCH_CALLER_HARNESS=parent_adapter,
                       AGENT_DISPATCH_CURRENT_SANDBOX=parent_sandbox,
                       AGENT_NESTED_HEADLESS_NETWORK="1")
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            # The grant fixture needs a live parent after start returns. Keep
            # its fake payload detached even when the test runner is sandboxed;
            # lifecycle selection is covered by dispatch_lifecycle.test.py.
            stack.enter_context(mock.patch("dispatch_lifecycle.pid_namespace_evidence", return_value={
                "lifecycle_selector_source": "host-like", "lifecycle_nspid_width": "1",
                "lifecycle_pid1_class": "system-init",
            }))
            if hasattr(wrapper, "check_runtime_projection"):
                stack.enter_context(mock.patch.object(wrapper, "check_runtime_projection", return_value=0))
            if hasattr(wrapper, "prepare_nested_codex_home"):
                stack.enter_context(mock.patch.object(wrapper, "prepare_nested_codex_home",
                                                      return_value=self.home / ".codex"))
                stack.enter_context(mock.patch.object(wrapper, "_core_grounding_dir",
                                                      return_value=self.root / "bootstrap/core"))
            stack.enter_context(mock.patch.object(wrapper.shutil, "which", return_value=sys.executable))
            stack.enter_context(mock.patch.object(wrapper, "resolve_artifact_root", return_value=str(self.artifacts)))
            stack.enter_context(mock.patch.object(wrapper, "shell_command", return_value=command))
            stack.enter_context(mock.patch.object(wrapper, "attach_summary_owner", return_value={}))
            # Frame/review readiness is covered separately; this fixture starts
            # at the approved owner's execution-access boundary.
            stack.enter_context(mock.patch.object(wrapper, "owner_frame_launch_gate"))
            stack.enter_context(mock.patch.object(wrapper, "completion_marker_gate"))
            for name in ("launch_orphan_watch", "launch_reap_watch"):
                if hasattr(wrapper, name):
                    stack.enter_context(mock.patch.object(wrapper, name, return_value=os.getpid()))
            def sidecar(args, _jobs):
                args.managed_sidecar_state = "not-started"
                args.managed_sidecar_reason = "-"
                args.managed_sidecar_pid = args.managed_sealed_batch_id = args.managed_sidecar_log = "-"
            stack.enter_context(mock.patch.object(wrapper, "launch_parent_completion_sidecar", side_effect=sidecar))
            access = stack.enter_context(mock.patch.object(
                wrapper.route_authority, "bind_launch_access",
                wraps=wrapper.route_authority.bind_launch_access))
            result = wrapper.main(argv)
            self.last_access_args = access.call_args.args[0] if access.call_args else None
        try:
            _, metadata = self.row(attempt)
            if metadata.get("pid"):
                self.processes.append(int(metadata["pid"]))
        except AssertionError:
            pass
        output = stdout.getvalue() + stderr.getvalue()
        if result == 0 and "started=1" in output:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists(), output)
        return result, output

    def test_child_dry_run_uses_the_same_live_parent_grant_without_launching(self):
        for adapter in ("codex", "claude", "opencode"):
            with self.subTest(adapter=adapter):
                route = self.route("claude", child_adapter=adapter)
                owner = "att-" + hashlib.sha256(("parent-" + adapter).encode()).hexdigest()[:32]
                child = "att-" + hashlib.sha256(("preview-" + adapter).encode()).hexdigest()[:32]
                result, output = self.start("claude", owner, self.request(owner), route=route)
                self.assertEqual(0, result, output)
                request = self.request(child, self.data / adapter)
                before = self.jobs.read_bytes()
                result, output = self.start(adapter, child, request, route=route, owner=owner,
                                            action="dry-run", parent_adapter="claude")
                self.assertEqual(0, result, output)
                self.assertNotIn("parent-grant-unknown", output)
                self.assertEqual(before, self.jobs.read_bytes())
                self.assertFalse((self.root / (child + ".started")).exists())
                self.assertFalse((self.state / "execution-access/attempts" / child).exists())
                preview = self.last_access_args.execution_access_grant
                self.assertEqual(owner, self.last_access_args.parent_binding.attempt_id)
                result, output = self.start(adapter, child, request, route=route, owner=owner,
                                            parent_adapter="claude")
                self.assertEqual(0, result, output)
                actual = self.last_access_args.execution_access_grant
                self.assertEqual(preview.writable_roots, actual.writable_roots)
                self.assertEqual(preview.network, actual.network)
                for name, root, network in (("expanded", self.root / "foreign/output", False),
                                             ("network", self.data / adapter, True)):
                    expanded = self.request(child + "-" + name, root, network)
                    before = self.jobs.read_bytes()
                    result, output = self.start(adapter, "att-" + "d" * 32, expanded,
                        route=route, owner=owner, action="dry-run", parent_adapter="claude")
                    self.assertNotEqual(0, result, output)
                    self.assertIn("execution-access-exceeds-parent", output)
                    self.assertEqual(before, self.jobs.read_bytes())

    def test_codex_owner_to_child_actual_start_and_parent_boundary(self):
        route = self.route("codex")
        parent_request = self.request("parent")
        result, output = self.start("codex", OWNER, parent_request, route=route)
        self.assertEqual(0, result, output)
        self.assertIn("started=1", output)
        _, row = self.row(OWNER)
        self.assertNotIn("route_id", row)
        self.assertEqual(route[1]["route_id"], row["owner_route_id"])
        effective = Path(row["execution_access_effective_file"])
        self.assertEqual(effective, self.state / "execution-access/attempts" / OWNER / "effective.json")
        self.assertEqual(hashlib.sha256(effective.read_bytes()).hexdigest(),
                         row["execution_access_effective_sha256"])
        parent = load_parent_effective_grant(jobs=self.jobs, parent_attempt_id=OWNER, context=self.context)
        original_rows = self.jobs.read_text()
        fields, original = self.row(OWNER)
        for changes in ({"owner_route_id": "rt-foreign"},
                        {"owner_route_hash": "sha256:" + "0" * 64},
                        {"owner_route_file": ""}, {"dispatch_depth": "2"},
                        {"worker_type": "stage"}, {"unit": "_kernel/resource"},
                        {"route_id": original["owner_route_id"]},
                        {"owner_route_hash": ""}):
            with self.subTest(identity=changes):
                metadata = {**original, **changes}
                self.jobs.write_text("\t".join(fields[:5]) + "\t" +
                                     ",".join(f"{key}={value}" for key, value in metadata.items()) + "\n")
                with self.assertRaises(ExecutionAccessError) as caught:
                    load_parent_effective_grant(jobs=self.jobs, parent_attempt_id=OWNER, context=self.context)
                self.assertEqual("execution-access-parent-record-identity-mismatch", caught.exception.reason)
        self.jobs.write_text(original_rows)
        with self.assertRaises(ExecutionAccessError) as caught:
            load_parent_effective_grant(jobs=self.jobs, parent_attempt_id="att-unknown", context=self.context)
        self.assertEqual("execution-access-parent-row-invalid", caught.exception.reason)
        child_request = self.request("child", self.data / "child")
        result, output = self.start("codex", CHILD, child_request, route=route, owner=OWNER)
        self.assertEqual(0, result, output)
        self.assertIn("started=1", output)
        _, child_row = self.row(CHILD)
        child_effective = json.loads(Path(child_row["execution_access_effective_file"]).read_text())
        for root in child_effective["writable_roots"]:
            self.assertTrue(any(Path(root).is_relative_to(bound) for bound in parent.writable_roots), root)
        self.assertLessEqual(child_effective["network_allowed"], parent.network_allowed)
        assert_within_parent(load_request(child_request, context=self.context), parent, is_child=True)
        for name, root, network in (("expanded", self.root / "foreign/output", False),
                                    ("network", self.data / "child", True)):
            request = self.request(name, root, network)
            # Owner default networking is permitted; only test an expansion when absent.
            if network and parent.network_allowed:
                continue
            attempt = "att-" + hashlib.sha256(name.encode()).hexdigest()[:32]
            result, output = self.start("codex", attempt, request, route=route, owner=OWNER)
            self.assertNotEqual(0, result, output)
            self.assertIn("execution-access-exceeds-parent", output)
            self.assertFalse((self.root / (attempt + ".started")).exists())
            self.assertNotIn("attempt_id=" + attempt + ",", self.jobs.read_text())

    def test_other_adapters_publish_the_same_validated_owner_identity(self):
        for adapter in ("claude", "opencode"):
            with self.subTest(adapter=adapter):
                route = self.route(adapter)
                attempt = "att-" + hashlib.sha256(adapter.encode()).hexdigest()[:32]
                result, output = self.start(adapter, attempt, self.request(adapter), route=route)
                self.assertEqual(0, result, output)
                self.assertIn("started=1", output)
                _, row = self.row(attempt)
                self.assertNotIn("route_id", row)
                record = json.loads(Path(row["execution_access_effective_file"]).read_text())
                self.assertEqual(route[1]["route_id"], record["route_id"])
                self.assertEqual(route[1]["route_hash"], record["route_hash"])
                parent = load_parent_effective_grant(jobs=self.jobs, parent_attempt_id=attempt, context=self.context)
                self.assertIn(self.data, parent.writable_roots)

    def test_each_owner_tree_runs_on_the_release_its_launch_resolved(self):
        # Launched through a moving pointer (`<share>/hearting/current`), the owner gets the
        # release the pointer named at launch, as the row records it (OPERATIONS §5.9a), so a
        # later install moves neither. Before, the child inherited the pointer itself.
        pointer = self.root / "current"
        pointer.symlink_to(ROOT)
        self.env["AGENT_HOME"] = str(pointer)
        self.worker.write_text(
            "import os,pathlib,sys,time\n"
            "pathlib.Path(sys.argv[1] + '.home').write_text(os.environ.get('AGENT_HOME', ''))\n"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
            "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)\n")
        for adapter in ("claude", "codex", "opencode"):
            with self.subTest(adapter=adapter):
                attempt = "att-" + hashlib.sha256(("pin-" + adapter).encode()).hexdigest()[:32]
                result, output = self.start(adapter, attempt, None, route=self.route(adapter))
                self.assertEqual(0, result, output)
                _, row = self.row(attempt)
                self.assertEqual(str(ROOT.resolve()), row["launch_home"])
                self.assertEqual(str(ROOT.resolve()), (self.root / (attempt + ".started.home")).read_text())

    def test_owner_without_request_keeps_default_grants(self):
        route = self.route("codex")
        result, output = self.start("codex", DEFAULT, None, route=route)
        self.assertEqual(0, result, output)
        _, row = self.row(DEFAULT)
        record = json.loads(Path(row["execution_access_effective_file"]).read_text())
        self.assertIsNone(record["request_path"])
        self.assertIsNone(record["request_sha256"])
        self.assertNotIn(str(self.data), record["writable_roots"])
        self.assertEqual("os-sandbox", record["file_enforcement"])


if __name__ == "__main__":
    unittest.main()
