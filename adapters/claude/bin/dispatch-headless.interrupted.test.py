#!/usr/bin/env python3
"""A foreground wrapper that is itself stopped closes its row the same way on every tool.

The wrapper receives SIGINT while its worker runs: it stops the worker and
closes the row `dead-interrupted` with `failure_class=runtime` and
`reconcile_reason=interrupted` -- the Claude, Codex and OpenCode wrappers alike.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import io
import os
from contextlib import redirect_stdout
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import parse_registry_metadata  # noqa: E402

MODEL_ARGS = {
    "codex": ["--model", "gpt-test", "--reasoning", "low"],
    "claude": ["--model", "claude-test", "--effort", "low"],
    "opencode": ["--model", "provider/test", "--variant", "low"],
}


class ForegroundInterruptedCloseTest(unittest.TestCase):
    def run_interrupted(self, harness):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            art = root / ".agent_reports"
            art.mkdir()
            jobs = root / "jobs.log"
            fakebin = root / "bin"
            fakebin.mkdir()
            fake = fakebin / harness
            version = ('[ "$1" = --version ] && { echo 1.19.0; exit 0; }\n'
                       if harness == "opencode" else "")
            fake.write_text("#!/bin/sh\n" + version + "exec sleep 60\n", encoding="utf-8")
            fake.chmod(0o755)
            parent = subprocess.Popen(["sleep", "60"])
            self.addCleanup(lambda: (parent.kill(), parent.wait()))
            start = (Path("/proc") / str(parent.pid) / "stat").read_text().split()[21]
            jobs.write_text(
                f"2026-07-23T00:00:00Z\topen\t{repo}\t{repo}\towner\t"
                "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                f"fallback_hop=same-harness-headless,worker_type=owner,harness={harness},"
                f"runtime_sandbox=fixture,attempt_id=att-parent-fixture,pid={parent.pid},pid_start={start}\n")
            spec = importlib.util.spec_from_file_location(
                f"{harness}_interrupted_fixture", ROOT / f"adapters/{harness}/bin/dispatch-headless.py")
            wrapper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(wrapper)
            attempt = f"att-{harness}-interrupted-0001"
            argv = ["dispatch-headless.py", "--start", "--worktree", str(repo), "--slug", f"{harness}-stop",
                    "--capability", "autopilot-code", "--capability-mode", "dev", "--worker-mode", "dev/backend",
                    "--intensity", "standard", "--dispatch-depth", "2", "--parent", "owner",
                    "--worker-role", "code-plan", "--owner", "autopilot-code", "--jobs", str(jobs),
                    "--log-dir", str(root / "logs"), "--attempt-id", attempt,
                    "--parent-harness", harness, "--parent-transport", "headless", "--parent-sandbox", "fixture",
                    "--launch-authority", "conductor", "--nested-eligibility", "supported",
                    "--eligibility-source", f"{harness}-fixture", "--fallback-ordinal", "1",
                    "--foreground-timeout", "30", *MODEL_ARGS[harness]]
            # This test may itself run inside a dispatched worker: none of that
            # session's route or dispatch bindings may reach the fixture.
            ambient = {key: value for key, value in os.environ.items()
                       if not key.startswith(("AGENT_DISPATCH_", "AGENT_OWNER_ROUTE_", "AGENT_ROUTE_",
                                              "AGENT_ARTIFACT_", "AGENT_MODEL_GOVERNOR_"))}
            env = {**ambient, "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""),
                   "AGENT_HOME": str(ROOT), "AGENT_ARTIFACT_ROOT": str(art),
                   "AGENT_DISPATCH_JOBS": str(jobs), "AGENT_DISPATCH_CHILD": "1",
                   "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                   "OPENCODE_CONFIG_CONTENT": "{}", "XDG_STATE_HOME": str(root / "state")}
            env.pop("AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN", None)
            resolution = wrapper.reconcile_launch_lifecycle(
                wrapper.DETACHED, {}, evidence={
                    "lifecycle_selector_source": "nspid-vector",
                    "lifecycle_nspid_width": "2",
                    "lifecycle_pid1_class": "non-system-init",
                })
            waiting = threading.Event()
            real_wait = wrapper.wait_foreground

            def wait_and_mark(*args, **kwargs):
                waiting.set()
                return real_wait(*args, **kwargs)

            def stop_the_wrapper():
                if waiting.wait(20):
                    time.sleep(0.3)
                    os.kill(os.getpid(), signal.SIGINT)

            patches = [
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(wrapper, "parent_attempt_binding_is_live", lambda *_a: True),
                mock.patch.object(wrapper, "reconcile_launch_lifecycle", return_value=resolution),
                mock.patch.object(wrapper, "wait_foreground", side_effect=wait_and_mark),
            ]
            if harness == "opencode":
                from fixtures.opencode_native_context import launch_context
                patches.append(mock.patch.object(wrapper.opencode_context,
                    "prepare_launch_context", side_effect=launch_context))
            if hasattr(wrapper, "check_runtime_projection"):
                patches.append(mock.patch.object(wrapper, "check_runtime_projection", return_value=0))
            if hasattr(wrapper, "ensure_runtime_home_projection"):
                patches.append(mock.patch.object(wrapper, "ensure_runtime_home_projection", return_value=None))
            if hasattr(wrapper, "launch_summary_owner"):
                patches.append(mock.patch.object(
                    wrapper, "launch_summary_owner", return_value={"summary_owner": "test-fixture"}))
            stopper = threading.Thread(target=stop_the_wrapper, daemon=True)
            stream = io.StringIO()
            for patch in patches:
                patch.start()
            try:
                stopper.start()
                with redirect_stdout(stream):
                    code = wrapper.main(["dispatch-headless.py", *argv[1:]])
            finally:
                for patch in reversed(patches):
                    patch.stop()
                stopper.join(5)
            rows = [line.split("\t") for line in jobs.read_text(encoding="utf-8").splitlines()]
            row = next(fields for fields in rows
                       if len(fields) == 6 and parse_registry_metadata(fields[5]).get("attempt_id") == attempt)
            return code, stream.getvalue(), row

    def test_every_wrapper_closes_an_interrupted_foreground_worker_alike(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                code, output, row = self.run_interrupted(harness)
                metadata = parse_registry_metadata(row[5])
                self.assertEqual(code, 0, output)
                self.assertIn("worker_failure=interrupted", output.splitlines(), output)
                self.assertEqual(row[1], "done", row)
                self.assertEqual(metadata.get("note"), "dead-interrupted", row)
                self.assertEqual(metadata.get("failure_class"), "runtime", row)
                self.assertEqual(metadata.get("reconcile_reason"), "interrupted", row)
                self.assertEqual(metadata.get("detected_by"), "foreground-process-exit", row)


if __name__ == "__main__":
    unittest.main()
