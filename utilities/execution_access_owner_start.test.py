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
import shutil
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
from split_module_fixture import patch_split

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
        import opencode_context_window
        from fixtures.opencode_native_context import launch_context
        native = mock.patch.object(opencode_context_window, "prepare_launch_context", side_effect=launch_context)
        native.start()
        self.addCleanup(native.stop)
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
            # A backstop at the suite timeout: a worker orphaned by a timed-out
            # run must not poll forever, but it must outlive any one test.
            "deadline=time.monotonic()+600\n"
            "while not pathlib.Path(sys.argv[2]).exists() and time.monotonic()<deadline: time.sleep(.05)\n"
        )
        self.processes = []
        self.access_grants = {}
        self.native_settings = {}
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

    def request(self, name, root=None, network=False, read_roots=()):
        root = root or self.data
        path = self.root / (name + ".json")
        path.write_text(json.dumps({
            "schema_version": 1, "writable_roots": [str(root)],
            "read_roots": [str(value) for value in read_roots],
            "network": {"required": network, "reason": "fixture" if network else "", "hosts": []},
            "enforcement_required": "any",
            "justification": {str(root): "fixture output",
                              **{str(value): "fixture input" for value in read_roots}},
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

    def make_release_fixture(self, release):
        release.mkdir(parents=True)
        (release / ".hearting-release.json").write_text(
            '{"schema":1,"version":"fixture"}\n', encoding="utf-8")
        (release / "harness-manifest.json").symlink_to(ROOT / "harness-manifest.json")
        for name in ("capabilities", "core", "profiles", "roles", "tools", "utilities"):
            (release / name).symlink_to(ROOT / name, target_is_directory=True)
        adapters = release / "adapters"
        adapters.mkdir()
        for harness in ("claude", "codex", "opencode"):
            source = ROOT / "adapters" / harness
            target = adapters / harness
            target.mkdir()
            for entry in source.iterdir():
                if entry.name != "bin":
                    (target / entry.name).symlink_to(
                        entry, target_is_directory=entry.is_dir())
                    continue
                target_bin = target / "bin"
                target_bin.mkdir()
                for executable in entry.iterdir():
                    destination = target_bin / executable.name
                    if executable.name == "dispatch-headless.py":
                        shutil.copy2(executable, destination)
                    else:
                        destination.symlink_to(
                            executable, target_is_directory=executable.is_dir())

    def start(self, adapter, attempt, request, *, route=None, owner=None,
              action="start", parent_adapter="codex", automatic_retry_of=None,
              prompt_file=None, runtime_env=None, slug=None, capture_native=False,
              release_pair=None):
        wrapper = self.wrappers[adapter]
        marker = self.root / (attempt + ".started")
        release = self.root / (attempt + ".release")
        command = shlex.join([sys.executable, str(self.worker), str(marker), str(release)])
        env = dict(self.env)
        if runtime_env:
            env.update(runtime_env)
        argv = ["dispatch-headless.py", "--" + action, "--worktree", str(self.worktree),
                "--jobs", str(self.jobs), "--log-dir", str(self.state / "logs"),
                "--slug", slug or attempt, "--attempt-id", attempt, "--capability", "autopilot-code",
                "--capability-mode", "debug", "--intensity", "standard", "--qa", "standard",
                "--owner-harness", adapter]
        if request is not None:
            argv += ["--execution-access-file", str(request)]
        if automatic_retry_of is not None:
            argv += ["--automatic-retry-of", automatic_retry_of]
        if prompt_file is not None:
            argv += ["--prompt-file", str(prompt_file)]
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
        exec_calls = []

        class SimulatedExec(BaseException):
            def __init__(self, result):
                self.result = result

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
                if capture_native:
                    nested_codex_home = self.home / (attempt + ".codex")
                elif release_pair is not None:
                    nested_codex_home = self.home / (".codex-" + attempt)
                else:
                    nested_codex_home = self.home / ".codex"
                nested_codex_home.mkdir(parents=True, exist_ok=True)
                stack.enter_context(mock.patch.object(wrapper, "prepare_nested_codex_home",
                                                      return_value=nested_codex_home))
                stack.enter_context(mock.patch.object(wrapper, "_core_grounding_dir",
                                                      return_value=self.root / "bootstrap/core"))
            stack.enter_context(mock.patch.object(wrapper.shutil, "which", return_value=sys.executable))
            stack.enter_context(mock.patch.object(wrapper, "resolve_artifact_root", return_value=str(self.artifacts)))
            build_native_command = wrapper.shell_command
            def fixture_runner(args, prompt_path, log_path):
                if capture_native:
                    from dataclasses import asdict
                    native_command = build_native_command(args, prompt_path, log_path)
                    grant = args.execution_access_grant
                    snapshot = {
                        "command": native_command,
                        "access": json.loads(json.dumps(asdict(grant), default=str)),
                    }
                    if adapter == "codex":
                        snapshot.update(
                            runtime_sandbox=wrapper.effective_runtime_sandbox(args),
                            nested_headless_network=bool(args.nested_headless_network),
                        )
                    elif adapter == "claude":
                        snapshot["claude"] = {
                            key: args.resolved_permission_posture.get(key)
                            for key in ("mode", "mode_flag", "allowed_tools", "inherited_default_mode")
                        }
                    else:
                        import dispatch_replacement
                        snapshot["opencode_permission"] = dispatch_replacement._applied_permissions(
                            args).get("opencode_permission")
                    self.native_settings[attempt] = snapshot
                return command
            stack.enter_context(mock.patch.object(wrapper, "shell_command", side_effect=fixture_runner))
            stack.enter_context(mock.patch.object(wrapper, "attach_summary_owner", return_value={}))
            # Frame/review readiness is covered separately; this fixture starts
            # at the approved owner's execution-access boundary.
            stack.enter_context(mock.patch.object(wrapper, "owner_frame_launch_gate"))
            stack.enter_context(patch_split(wrapper, "completion_marker_gate"))
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
            if release_pair is not None:
                source_root, sealed_root = release_pair
                stack.enter_context(patch_split(wrapper, "ROOT", source_root))

                def simulate_exec(executable, vector):
                    exec_calls.append((executable, list(vector)))
                    target = Path(vector[1])
                    if not target.is_file():
                        raise AssertionError("sealed release wrapper is missing")
                    with patch_split(wrapper, "ROOT", sealed_root):
                        nested_result = wrapper.main([str(target), *vector[2:]])
                    raise SimulatedExec(nested_result)

                stack.enter_context(mock.patch.object(
                    wrapper.WRAPPER_COMMON.os, "execv", side_effect=simulate_exec))
            try:
                result = wrapper.main(argv)
            except SimulatedExec as completed:
                result = completed.result
            self.last_access_args = access.call_args.args[0] if access.call_args else None
            grant = getattr(self.last_access_args, "execution_access_grant", None)
            if grant is not None:
                self.access_grants[attempt] = grant
        self.last_exec_calls = exec_calls
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

    def test_codex_workspace_write_keeps_an_external_read_root_in_the_effective_grant(self):
        route = self.route("codex")
        external = self.root / "external-read-only"
        external.mkdir()
        request = self.request("codex-external-read", read_roots=(external,))
        result, output = self.start("codex", OWNER, request, route=route)
        self.assertEqual(0, result, output)
        self.assertIn("started=1", output)
        _, row = self.row(OWNER)
        effective_path = Path(row["execution_access_effective_file"])
        record = json.loads(effective_path.read_text(encoding="utf-8"))
        self.assertEqual(row["runtime_sandbox"], "workspace-write")
        self.assertIn(str(external.resolve()), record["read_roots"])
        self.assertEqual(record["read_enforcement"], "os-sandbox")
        self.assertEqual((record["file_enforcement"], record["network_enforcement"]),
                         ("os-sandbox", "os-sandbox"))
        self.assertEqual(row["execution_access_enforcement"], "os-sandbox")
        self.assertEqual(hashlib.sha256(effective_path.read_bytes()).hexdigest(),
                         row["execution_access_effective_sha256"])

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

    def test_exact_attempt_reentry_spawns_one_real_child_on_each_adapter(self):
        for adapter in ("codex", "claude", "opencode"):
            with self.subTest(adapter=adapter):
                attempt = "att-" + hashlib.sha256(("single-spawn-" + adapter).encode()).hexdigest()[:32]
                route = self.route(adapter)
                first, output = self.start(adapter, attempt, self.request(attempt), route=route)
                self.assertEqual(first, 0, output)
                marker = self.root / (attempt + ".started")
                first_pid = marker.read_text(encoding="utf-8")
                fields, first_row = self.row(attempt)
                second, duplicate = self.start(adapter, attempt, self.request(attempt), route=route)
                self.assertNotEqual(second, 0, duplicate)
                self.assertIn("reason=attempt-identity-conflict", duplicate)
                self.assertIn("child_spawned=0", duplicate)
                self.assertEqual(marker.read_text(encoding="utf-8"), first_pid)
                rows = [line for line in self.jobs.read_text().splitlines()
                        if f"attempt_id={attempt}" in line]
                self.assertEqual(len(rows), 1)
                self.assertIn(f"pid={first_row['pid']}", rows[0])
                self.assertTrue(fields[1] in {"open", "running"})

    def _assert_replacement_launch_evidence(self, adapter, attempt, read_root):
        from dataclasses import asdict
        import dispatch_replacement as replacement
        import execution_access as access

        _fields, row = self.row(attempt)
        sealed = replacement.launch_input(self.jobs, attempt, row)
        applied = sealed["applied_permissions"]
        grant = self.access_grants[attempt]
        grant_json = json.loads(json.dumps(asdict(grant), default=str))
        self.assertEqual(applied["execution_access"], grant_json)
        self.assertEqual(self.native_settings[attempt]["access"], grant_json)
        for key, value in access.receipt_fields(grant).items():
            self.assertEqual(row.get(key), value, f"{attempt} registry {key}")

        effective_path = Path(row["execution_access_effective_file"])
        effective_bytes = effective_path.read_bytes()
        effective = json.loads(effective_bytes)
        self.assertEqual(hashlib.sha256(effective_bytes).hexdigest(),
                         row["execution_access_effective_sha256"])
        self.assertEqual(effective["attempt_id"], attempt)
        self.assertEqual(effective["sandbox"], row["runtime_sandbox"])
        self.assertEqual(effective["file_enforcement"], grant.file_enforcement)
        self.assertEqual(effective["read_enforcement"], grant.read_enforcement)
        self.assertEqual(effective["network_enforcement"], grant.network_enforcement)
        self.assertEqual(set(map(str, grant.read_roots)), set(effective["read_roots"]))
        self.assertIn(str(Path(read_root).resolve()), effective["read_roots"])
        for root in grant.additional_writable_roots:
            self.assertIn(str(root), effective["writable_roots"])

        native = self.native_settings[attempt]
        if adapter == "codex":
            self.assertEqual(applied["runtime_sandbox"], native["runtime_sandbox"])
            self.assertEqual(native["runtime_sandbox"], row["runtime_sandbox"])
            self.assertEqual(applied["nested_headless_network"], native["nested_headless_network"])
            self.assertEqual(effective["network_allowed"], native["nested_headless_network"])
            native_argv = shlex.split(native["command"])
            native_config = [native_argv[index + 1] for index, flag in enumerate(native_argv[:-1])
                             if flag == "-c"]
            self.assertTrue(
                f"--sandbox {native['runtime_sandbox']}" in native["command"] or
                (any("default_permissions" in value for value in native_config) and
                 any('":workspace"' in value for value in native_config)), native["command"])
            if native["nested_headless_network"]:
                self.assertTrue("--network-access" in native["command"] or any(
                    "network" in value and "enabled" in value and "true" in value
                    for value in native_config) or
                    any("sandbox_workspace_write.network_access=true" in value
                        for value in native_config), native["command"])
            else:
                self.assertNotIn("--network-access", native["command"])
        elif adapter == "claude":
            sealed_posture = dict(applied["claude"])
            native_posture = dict(native["claude"])
            sealed_posture["allowed_tools"] = sorted(sealed_posture["allowed_tools"])
            native_posture["allowed_tools"] = sorted(native_posture["allowed_tools"])
            self.assertEqual(sealed_posture, native_posture)
            self.assertIn(applied["claude"]["mode_flag"], native["command"])
        else:
            self.assertEqual(applied["opencode_permission"], native["opencode_permission"])

        network_signature = {
            "request": grant.network,
            "network_allowed": effective["network_allowed"],
            "network_enforcement": effective["network_enforcement"],
            "file_enforcement": effective["file_enforcement"],
            "read_enforcement": effective["read_enforcement"],
            "read_roots": sorted(effective["read_roots"]),
            "runtime_sandbox": row["runtime_sandbox"],
            "adapter_native": {key: value for key, value in native.items() if key != "command"},
            "sealed": applied,
        }
        return network_signature

    def _end_blocked_and_correct(self, adapter, attempt, answer, request_id):
        import dispatch_contract as contract
        import dispatch_owner_input as owner_input
        import dispatch_replacement as replacement

        transport = {"codex": "codex-active-turn", "claude": "claude-next-turn",
                     "opencode": "opencode-next-turn"}[adapter]
        owner_input.initialize_owner_input(self.jobs, attempt, transport)
        _fields, row = self.row(attempt)
        (self.root / (attempt + ".release")).touch()
        pid = int(row["pid"])
        try:
            waited, _status = os.waitpid(pid, 0)
            self.assertEqual(waited, pid)
        except ChildProcessError:
            # The wrapper may have reaped the short lived fixture child already.
            pass
        self.assertTrue(contract.close_attempt_row(
            self.jobs, attempt, "dead-worker-blocked", evidence={"failure_class": "blocked"}))
        submitted = owner_input.submit(self.jobs, attempt, answer, request_id)
        self.assertTrue(submitted.get("retained"), submitted)
        fields, row = replacement._rows(self.jobs.read_text().splitlines())[attempt]
        self.assertEqual(fields[1], "done")
        self.assertEqual(replacement.death_kind(fields, row, jobs=self.jobs), replacement.CORRECTED)
        return submitted

    def test_two_blocked_corrections_spawn_the_exact_final_replacement_once_on_each_adapter(self):
        import dispatch_owner_input as owner_input
        import dispatch_replacement as replacement

        self.env["CLAUDE_SESSION_ID"] = "fixture-parent-session"
        self.worker.write_text(
            "import os,pathlib,sys,time\n"
            "marker=pathlib.Path(sys.argv[1]); release=pathlib.Path(sys.argv[2])\n"
            "with (marker.parent/'spawn-ledger').open('a') as ledger:\n"
            " ledger.write(marker.name.removesuffix('.started')+'\\t'+str(os.getpid())+'\\n')\n"
            "marker.write_text(str(os.getpid()))\n"
            "while not release.exists(): time.sleep(.01)\n")

        for adapter in ("codex", "claude", "opencode"):
            with self.subTest(adapter=adapter):
                route = self.route(adapter)
                external_read = self.root / (adapter + "-external-read")
                external_read.mkdir()
                request = self.request(adapter + "-lineage", read_roots=(external_read,))
                origin = "att-" + hashlib.sha256((adapter + "-blocked-origin").encode()).hexdigest()[:32]
                corrections = {}
                starts = []

                first_start, first_output = self.start(
                    adapter, origin, request, route=route, capture_native=True)
                self.assertEqual(first_start, 0, first_output)
                self.assertIn("started=1", first_output)
                _origin_fields, origin_row = self.row(origin)
                origin_pid = (self.root / (origin + ".started")).read_text()
                self.assertTrue(origin_row["pid"])
                self.assertEqual(sum(line.startswith(origin + "\t") for line in
                                     (self.root / "spawn-ledger").read_text().splitlines()), 1)
                self.assertIn(f"{origin}\t{origin_pid}",
                              (self.root / "spawn-ledger").read_text().splitlines())
                signatures = [self._assert_replacement_launch_evidence(adapter, origin, external_read)]

                parent_env = dict(self.env)
                def launch_replacement(command, *, env=None, **_kwargs):
                    self.assertEqual(Path(command[1]), ROOT / "utilities/dispatch-owner.py")
                    def option(name):
                        return command[command.index(name) + 1]
                    predecessor = option("--automatic-retry-of")
                    attempt = option("--attempt-id")
                    prompt_file = Path(option("--prompt-file"))
                    self.assertEqual(Path(option("--route-evidence")), route[0])
                    self.assertEqual(Path(option("--jobs")), self.jobs)
                    self.assertEqual(Path(option("--execution-access-file")), request)
                    result, output = self.start(
                        adapter, attempt, request, route=route,
                        automatic_retry_of=predecessor, prompt_file=prompt_file,
                        runtime_env=env, slug=option("--slug"), capture_native=True)
                    self.assertEqual(result, 0, output)
                    self.assertIn("started=1", output)
                    rendered_prompt = next(
                        Path(line.split("=", 1)[1]) for line in output.splitlines()
                        if line.startswith("prompt_file=")
                    )
                    self.assertIn(corrections[predecessor],
                                  rendered_prompt.read_text(encoding="utf-8"))
                    _fields, row = self.row(attempt)
                    self.assertEqual(row["automatic_retry_of"], predecessor)
                    self.assertEqual(row["harness"], adapter)
                    self.assertTrue(row["pid"])
                    ledger_rows = (self.root / "spawn-ledger").read_text().splitlines()
                    self.assertIn(f"{attempt}\t{(self.root / (attempt + '.started')).read_text()}",
                                  ledger_rows)
                    starts.append((attempt, predecessor, prompt_file))
                    signatures.append(self._assert_replacement_launch_evidence(
                        adapter, attempt, external_read))
                    return subprocess.CompletedProcess(command, result, output, "")

                def advance(attempt):
                    with mock.patch.dict(os.environ, parent_env, clear=True):
                        with mock.patch("dispatch_capacity_evidence.harness_hold", return_value=None):
                            with mock.patch.object(replacement, "_reuse_snapshot", return_value={
                                    "completed": [], "cycle_id": "cyc-fixture",
                                    "producer_id": "prod-fixture", "gate_releases": []}):
                                return replacement.advance(
                                    self.jobs, attempt, run=launch_replacement, resume_capacity=True)

                self._end_blocked_and_correct(
                    adapter, origin, "approved: start full run", "answer-origin")
                corrections[origin] = "approved: start full run"
                first_result = advance(origin)
                self.assertEqual(len(starts), 1, first_result)
                self.assertIn("record", first_result, first_result)
                first_record = first_result["record"]
                first = first_record["replacement_attempt_id"]
                self.assertEqual(first_result["attempt_id"], first, first_result)
                self.assertEqual(first_record["original_attempt_id"], origin)
                self.assertEqual(first_record["proof"]["death_kind"], replacement.CORRECTED)
                self.assertEqual(len(starts), 1)
                self._end_blocked_and_correct(
                    adapter, first, "approved: continue phase two", "answer-first")
                corrections[first] = "approved: continue phase two"
                second_result = advance(first)
                self.assertEqual(len(starts), 2, second_result)
                self.assertIn("record", second_result, second_result)
                second_record = second_result["record"]
                second = second_record["replacement_attempt_id"]
                self.assertEqual(second_result["attempt_id"], second, second_result)
                self.assertEqual(second_record["original_attempt_id"], first)
                self.assertEqual(second_record["proof"]["death_kind"], replacement.CORRECTED)
                self.assertEqual(len(starts), 2)
                self.assertEqual(starts[0][1], origin)
                self.assertEqual(starts[1][1], first)

                for predecessor, record in ((origin, first_record), (first, second_record)):
                    _fields, source = self.row(predecessor)
                    self.assertEqual(replacement.source_binding(self.jobs, source),
                                     (record["family_id"], record["replacement_attempt_id"],
                                      replacement._digest(record)))
                    _child_fields, child = self.row(record["replacement_attempt_id"])
                    self.assertEqual(child["replacement_original_attempt_id"], predecessor)
                    self.assertEqual(child["replacement_family_id"], record["family_id"])
                    self.assertEqual(child["replacement_claim_digest"], replacement._digest(record))
                    self.assertEqual(child["automatic_retry_of"], predecessor)
                    self.assertEqual(replacement.claim(self.jobs, predecessor), record)

                effective, lineage = replacement.effective_attempts(self.jobs, {origin})
                self.assertEqual(effective, {second})
                self.assertEqual(len(lineage), 1)
                self.assertEqual(lineage[0]["original_attempt_id"], origin)
                self.assertEqual(lineage[0]["replacement_attempt_id"], second)
                self.assertEqual(lineage[0]["family_id"], first_record["family_id"])
                self.assertEqual(signatures[0], signatures[1])
                self.assertEqual(signatures[1], signatures[2])

                ledger = self.root / "spawn-ledger"
                before_duplicate = ledger.read_text(encoding="utf-8").splitlines()
                counts = {attempt: sum(line.split("\t", 1)[0] == attempt
                                       for line in before_duplicate)
                          for attempt in (origin, first, second)}
                self.assertEqual(counts, {origin: 1, first: 1, second: 1})
                final_pid = (self.root / (second + ".started")).read_text()
                self.assertTrue(self.row(second)[1]["pid"])
                self.assertIn(f"{second}\t{final_pid}", before_duplicate)

                calls_before = len(starts)
                replayed = advance(origin)
                self.assertIn(replayed["state"], {"running", "reused"}, replayed)
                self.assertEqual(replayed["attempt_id"], second)
                self.assertEqual(len(starts), calls_before)
                with mock.patch.object(replacement, "_reuse_snapshot", return_value={
                        "completed": [], "cycle_id": "cyc-fixture",
                        "producer_id": "prod-fixture", "gate_releases": []}):
                    duplicate, duplicate_output = self.start(
                        adapter, second, request, route=route, automatic_retry_of=first,
                        prompt_file=starts[1][2], capture_native=True)
                self.assertNotEqual(duplicate, 0, duplicate_output)
                self.assertIn("child_spawned=0", duplicate_output)
                self.assertEqual(ledger.read_text(encoding="utf-8").splitlines(), before_duplicate)
                self.assertEqual((self.root / (second + ".started")).read_text(), final_pid)
                after_duplicate = replacement.effective_attempts(self.jobs, {origin})
                self.assertEqual(after_duplicate, (effective, lineage))

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
        wrapper_common = self.wrappers["claude"].WRAPPER_COMMON
        with mock.patch.object(
            wrapper_common, "select_execution_root",
            wraps=wrapper_common.select_execution_root,
        ) as reexec:
            for adapter in ("claude", "codex", "opencode"):
                with self.subTest(adapter=adapter):
                    attempt = "att-" + hashlib.sha256(("pin-" + adapter).encode()).hexdigest()[:32]
                    result, output = self.start(adapter, attempt, None, route=self.route(adapter))
                    self.assertEqual(0, result, output)
                    _, row = self.row(attempt)
                    self.assertEqual(str(ROOT.resolve()), row["launch_home"])
                    self.assertEqual(str(ROOT.resolve()), (self.root / (attempt + ".started.home")).read_text())
        self.assertEqual(reexec.call_count, 3)
        self.assertEqual([call.args[2] for call in reexec.call_args_list],
                         ["claude", "codex", "opencode"])
        self.assertTrue(all(Path(call.args[0]).resolve() == ROOT.resolve()
                            and Path(call.args[1]).resolve() == ROOT.resolve()
                            and call.args[3][0] == "--start"
                            for call in reexec.call_args_list))

    def test_sibling_release_reexec_keeps_one_registration_and_pins_child_home(self):
        releases = self.root / "managed" / "releases"
        releases.mkdir(parents=True)
        release_a, release_b = releases / "vA", releases / "vB"
        self.make_release_fixture(release_a)
        self.make_release_fixture(release_b)
        self.worker.write_text(
            "import os,pathlib,sys,time\n"
            "pathlib.Path(sys.argv[1] + '.home').write_text(os.environ.get('AGENT_HOME', ''))\n"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
            "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)\n")
        self.env["AGENT_HOME"] = str(ROOT)

        for adapter in ("claude", "codex", "opencode"):
            for source, sealed in ((release_a, release_b), (release_b, release_a)):
                with self.subTest(adapter=adapter, source=source.name, sealed=sealed.name):
                    route = self.route(adapter)
                    self.env["AGENT_HOME"] = str(sealed)
                    attempt = "att-" + hashlib.sha256(
                        (adapter + source.name + sealed.name).encode()).hexdigest()[:32]
                    result, output = self.start(
                        adapter, attempt, None, route=route,
                        release_pair=(source, sealed),
                    )
                    self.assertEqual(result, 0, output)
                    self.assertIn("started=1", output)
                    _fields, row = self.row(attempt)
                    self.assertEqual(row["launch_home"], str(sealed.resolve()))
                    self.assertEqual((self.root / (attempt + ".started.home")).read_text(),
                                     str(sealed.resolve()))
                    self.assertEqual(len(self.last_exec_calls), 1)
                    executable, vector = self.last_exec_calls[0]
                    self.assertEqual(executable, sys.executable)
                    self.assertEqual(Path(vector[1]).resolve(),
                                     (sealed / "adapters" / adapter / "bin" /
                                      "dispatch-headless.py").resolve())
                    registered = [line for line in self.jobs.read_text(encoding="utf-8").splitlines()
                                  if f"attempt_id={attempt}" in line]
                    self.assertEqual(len(registered), 1)

    def test_installed_release_is_the_one_root_of_guard_row_and_child_when_the_tree_inherited_an_older_home(self):
        # T4: the wrapper runs the installed release B, the tree inherited A. Without any
        # re-exec, the row's launch_home and the child's AGENT_HOME are B, the root that ran admission.
        import peer_obligations
        releases = self.root / "managed" / "releases"
        releases.mkdir(parents=True)
        release_a, release_b = releases / "vA", releases / "vB"
        self.make_release_fixture(release_a)
        self.make_release_fixture(release_b)
        self.worker.write_text(
            "import os,pathlib,sys,time\n"
            "pathlib.Path(sys.argv[1] + '.home').write_text(os.environ.get('AGENT_HOME', ''))\n"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
            "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)\n")
        for adapter in ("claude", "codex", "opencode"):
            with self.subTest(adapter=adapter):
                self.env["AGENT_HOME"] = str(release_a)
                attempt = "att-" + hashlib.sha256(("installed-" + adapter).encode()).hexdigest()[:32]
                with mock.patch.object(peer_obligations, "installed_release",
                                       return_value=release_b.resolve()):
                    result, output = self.start(adapter, attempt, None, route=self.route(adapter),
                                                release_pair=(release_b, release_a))
                self.assertEqual(result, 0, output)
                self.assertEqual(self.last_exec_calls, [])
                _fields, row = self.row(attempt)
                self.assertEqual(row["launch_home"], str(release_b.resolve()))
                self.assertEqual((self.root / (attempt + ".started.home")).read_text(),
                                 str(release_b.resolve()))

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
