#!/usr/bin/env python3
"""Exercise the OpenCode start path through native context lookup to spawn."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
import dispatch_contract as DC

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
SPEC = importlib.util.spec_from_file_location(
    "opencode_dispatch_context_fixture", ROOT / "adapters/opencode/bin/dispatch-headless.py",
)
DISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DISPATCH)
CONTEXT = DISPATCH.opencode_context


class OpenCodeDispatchContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opencode-dispatch-context-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(self.worktree)], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "config", "user.email",
                        "fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "config", "user.name",
                        "Fixture"], check=True)
        (self.worktree / "README").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.worktree), "add", "README"], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "commit", "-qm", "fixture"], check=True)
        self.artifacts = self.root / ".agent_reports"
        self.artifacts.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake_opencode = self.bin / "opencode"
        fake_opencode.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        fake_opencode.chmod(0o755)
        for name in ("config", "data", "cache"):
            (self.root / name).mkdir()
        self.jobs = self.root / "jobs.log"
        self.environment = {
            "PATH": os.pathsep.join((str(self.bin), os.environ.get("PATH", "/usr/bin:/bin"))),
            "HOME": str(self.root),
            "AGENT_HOME": str(ROOT),
            "AGENT_ARTIFACT_ROOT": str(self.artifacts),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "HARNESS_STATE_ROOT": str(self.state),
            "FLEET_USAGE_STATE_DIR": str(self.state / "usage"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_STATE_HOME": str(self.state),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "OPENCODE_RUNTIME_VERSION": "2.0.32",
            "OPENCODE_CONFIG_CONTENT": json.dumps({"model": "fixture/model"}),
            "AGENT_DISPATCH_CURRENT_HARNESS": "claude",
            "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
            "AGENT_DISPATCH_CURRENT_SANDBOX": "fixture",
            "AGENT_DISPATCH_CALLER_HARNESS": "claude",
            "AGENT_DISPATCH_OWNER_HARNESS": "claude",
            "AGENT_DISPATCH_PARENT_SESSION_ID": "session-test",
        }
        self.before = self._model(1000000, 500000, 16000)

    def _model(self, context, input_limit, output):
        return {"providerID": "fixture", "id": "model", "modelID": "provider-model",
                "name": "Fixture", "status": "active", "enabled": True,
                "limit": {"context": context, "input": input_limit, "output": output}}

    def _invoke(self, *, wrong_location=False, shared_stale=False, environment=None):
        calls = []

        def native_api(command, *, cwd, env):
            calls.append((command, cwd, dict(env)))
            location = str(self.root if wrong_location else self.worktree)
            if shared_stale:
                response = {"location": {"directory": location}, "data": [self.before]}
                return json.dumps(response)
            content = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}")
            providers = content.get("providers")
            provider = providers.get("fixture") if isinstance(providers, dict) else None
            models = provider.get("models") if isinstance(provider, dict) else None
            model = models.get("model") if isinstance(models, dict) else None
            limits = model.get("limit") if isinstance(model, dict) else None
            if "--standalone" in command and isinstance(limits, dict):
                response_model = self._model(
                    limits.get("context", self.before["limit"]["context"]),
                    limits.get("input", self.before["limit"]["input"]),
                    limits.get("output", self.before["limit"]["output"]),
                )
                response = {"location": {"directory": location}, "data": [response_model]}
            else:
                response = {"location": {"directory": location}, "data": [self.before]}
            return json.dumps(response)

        argv = [
            "dispatch-headless.py", "--start", "--worktree", str(self.worktree),
            "--jobs", str(self.jobs), "--slug", "context-fixture",
            "--capability", "autopilot-code", "--capability-mode", "debug",
            "--worker-mode", "dev/backend", "--worker-type", "review",
            "--launch-lifecycle", "foreground-scoped", "--model", "fixture/model",
            "--variant", "fast", "--attempt-id", "att-context-fixture",
        ]
        stream = __import__("io").StringIO()
        real_popen = subprocess.Popen
        real_shell_command = DISPATCH.shell_command
        spawned = []
        worker_commands = []

        def tracked_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            spawned.append(process)
            return process

        spawn = mock.Mock(side_effect=tracked_popen)

        def wait(process, *_args, **_kwargs):
            process.wait(timeout=10)
            return types.SimpleNamespace(exit_code=0, failure="", group_empty=True)

        def no_sidecar(args, *_args, **_kwargs):
            args.managed_sidecar_state = "not-selected"
            args.managed_sidecar_reason = "-"
            args.managed_sidecar_pid = "-"
            args.managed_sealed_batch_id = "-"
            args.managed_sidecar_log = "-"

        def capture_worker_command(args, prompt_path, log_path):
            command = real_shell_command(args, prompt_path, log_path)
            worker_commands.append(command)
            return "true"

        patches = [
            mock.patch.object(DISPATCH, "resolve_artifact_root", return_value=str(self.artifacts)),
            mock.patch.object(DISPATCH, "check_runtime_projection", return_value=0),
            mock.patch.object(DISPATCH, "shell_command", side_effect=capture_worker_command),
            mock.patch.object(DISPATCH, "wait_governor_reservation_claim", return_value={}),
            mock.patch.object(DISPATCH, "launch_parent_completion_sidecar", side_effect=no_sidecar),
            mock.patch.object(DISPATCH, "wait_foreground", side_effect=wait),
            mock.patch.object(DISPATCH, "seal_foreground_result", return_value=object()),
            mock.patch.object(DISPATCH, "launch_reap_watch", return_value=4243),
            mock.patch.object(CONTEXT, "_installed_version", return_value="2.0.32"),
            mock.patch.object(CONTEXT, "_run", side_effect=native_api),
            mock.patch.object(DISPATCH.subprocess, "Popen", new=spawn),
        ]
        from contextlib import redirect_stdout
        with mock.patch.dict(os.environ, environment or self.environment, clear=True), \
                redirect_stdout(stream):
            for patch in patches:
                patch.start()
            try:
                result = DISPATCH.main(argv)
            finally:
                for patch in reversed(patches):
                    patch.stop()
        for process in spawned:
            if process.poll() is None:
                process.wait(timeout=10)
        return result, stream.getvalue(), calls, spawn, worker_commands

    def test_v2_cap_is_applied_to_worker_home_and_dispatch_reaches_spawn(self):
        real_observer = DISPATCH.opencode_runtime.observe_launch
        with mock.patch.object(DISPATCH.opencode_runtime, "observe_launch",
                               wraps=real_observer) as observe:
            result, receipt, calls, spawn, worker_commands = self._invoke()
        self.assertEqual(result, 0, receipt)
        self.assertEqual(observe.call_count, 1)
        self.assertIn("child_spawned=1", receipt)
        self.assertEqual([call[0][1:] for call in calls], [
            ["api", "--standalone", "GET", "/api/model"],
            ["api", "--standalone", "GET", "/api/model"],
        ])
        before_config = json.loads(calls[0][2]["OPENCODE_CONFIG_CONTENT"])
        final_config = json.loads(calls[1][2]["OPENCODE_CONFIG_CONTENT"])
        self.assertFalse(before_config["snapshot"])
        self.assertEqual(final_config["providers"]["fixture"]["models"]["model"]["limit"], {
            "context": 800000, "input": 500000, "output": 16000,
        })
        launch_calls = [call for call in spawn.call_args_list
                        if any("launch-fence.py" in str(arg) for arg in call.args[0])]
        self.assertEqual(len(launch_calls), 1)
        child_env = launch_calls[0].kwargs["env"]
        self.assertEqual(child_env["OPENCODE_CONFIG_CONTENT"], calls[1][2]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(child_env["OPENCODE_RUNTIME_VERSION"], "2.0.32")
        self.assertEqual(child_env["XDG_DATA_HOME"], calls[0][2]["XDG_DATA_HOME"])
        self.assertIn(calls[0][0][0], worker_commands[0])
        self.assertNotIn("OPENCODE_DB", child_env)
        self.assertNotIn("OPENCODE_AUTH", child_env)
        self.assertEqual(len(worker_commands), 1)
        self.assertIn("opencode run --standalone --format json", worker_commands[0])
        self.assertIn("fixture/model#fast", worker_commands[0])
        self.assertIn("context_window_native_application=standalone-api:OPENCODE_CONFIG_CONTENT:providers-model-limit",
                      receipt)
        self.assertIn("context_window_execution_mode=standalone", receipt)
        self.assertIn("context_window_runtime_target=standalone-per-command", receipt)
        self.assertIn("context_window_server_identity=unavailable", receipt)

    def test_v2_spawns_when_the_user_environment_sets_no_xdg_homes(self):
        # A real user shell often leaves XDG_*_HOME unset; the V2 worker keeps the
        # shared data home and records no attempt-private runtime directory.
        environment = {key: value for key, value in self.environment.items()
                       if not key.startswith("XDG_")}
        result, receipt, _calls, _spawn, _worker_commands = self._invoke(environment=environment)
        self.assertEqual(result, 0, receipt)
        self.assertIn("child_spawned=1", receipt)
        rows = self.jobs.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("opencode_runtime_dir=", rows[0])

    def test_v2_worker_command_runs_in_the_worktree_not_the_caller_directory(self):
        # V2 has no `--dir`: a worker left in the caller's cwd works there and its
        # external_directory boundary is measured from the wrong Location.
        result, receipt, _calls, _spawn, worker_commands = self._invoke()
        self.assertEqual(result, 0, receipt)
        self.assertNotIn("--dir", worker_commands[0])
        seen = self.root / "worker-cwd"
        (self.bin / "opencode").write_text(f'#!/bin/sh\npwd -P > "{seen}"\n', encoding="utf-8")
        caller = self.root / "caller"
        caller.mkdir()
        subprocess.run(["sh", "-c", worker_commands[0]], cwd=caller, check=True,
                       env={**os.environ, "PATH": self.environment["PATH"]})
        self.assertEqual(seen.read_text(encoding="utf-8").strip(), str(self.worktree.resolve()))

    def test_v2_wrong_location_fails_before_spawn_and_closes_attempt(self):
        result, receipt, calls, spawn, worker_commands = self._invoke(wrong_location=True)
        self.assertEqual(result, 69, receipt)
        self.assertIn("reason=opencode-context-limit-unavailable", receipt)
        self.assertIn("child_spawned=0", receipt)
        self.assertEqual(len(calls), 1)
        self.assertIn("--standalone", calls[0][0])
        self.assertEqual(len(worker_commands), 1)
        self.assertIn("--standalone", worker_commands[0])
        self.assertFalse(any(any("launch-fence.py" in str(arg) for arg in call.args[0])
                             for call in spawn.call_args_list))
        rows = self.jobs.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 1)
        fields = rows[0].split("\t")
        self.assertEqual(fields[1], "done")
        metadata = DC.parse_registry_metadata(fields[5])
        self.assertEqual(metadata["note"], "dead-candidate-preparation")
        self.assertEqual(metadata["failure_class"], "launch-tuple")
        self.assertEqual(metadata["reconcile_reason"], "opencode-context-limit-unavailable")
        self.assertEqual(metadata["launch_outcome"], "never-launched")

    def test_reused_shared_server_with_unchanged_model_fails_before_worker_spawn(self):
        result, receipt, calls, spawn, worker_commands = self._invoke(shared_stale=True)
        self.assertEqual(result, 69, receipt)
        self.assertIn("reason=opencode-context-limit-unavailable", receipt)
        self.assertIn("child_spawned=0", receipt)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0][2]["OPENCODE_CONFIG_CONTENT"],
                            calls[1][2]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(len(worker_commands), 1)
        self.assertIn("--standalone", worker_commands[0])
        self.assertFalse(any(any("launch-fence.py" in str(arg) for arg in call.args[0])
                             for call in spawn.call_args_list))

    def test_supervised_owner_command_carries_standalone_mode_and_selected_model(self):
        args = types.SimpleNamespace(
            jobs_path=str(self.jobs), attempt_id="att-owner-command",
            worktree=str(self.worktree), agent="build", route_file=None,
            route_id=None, route_hash=None, route_node=None, owner_route_binding=None,
            dispatch_depth=1, worker_type="owner", intensity="quick",
            resolved_model_settings={"source": "explicit", "model": "fixture/model",
                                     "variant": "fast",
                                     "context_window_execution_mode": "standalone"},
        )
        command = DISPATCH.shell_command(args, self.root / "prompt.txt", self.root / "log.txt")
        self.assertIn("--opencode-standalone", command)
        self.assertIn("--model fixture/model", command)
        self.assertIn("--variant fast", command)


if __name__ == "__main__":
    unittest.main()
