#!/usr/bin/env python3
"""Owned-process and isolation fixtures for the bounded OpenCode live runner."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "utilities" / "herdr_opencode_live.py"
SPEC = importlib.util.spec_from_file_location("herdr_opencode_live_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


class LiveRunnerIsolationTest(unittest.TestCase):
    def test_environment_is_named_and_drops_inherited_secrets_and_endpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "project"
            project.mkdir()
            with mock.patch.dict(os.environ, {
                "OPENAI_API_KEY": "must-not-copy",
                "HERDR_SOCKET_PATH": "/run/default-herdr.sock",
                "AGENT_HERDR_SESSION": "default",
                "CODEX_THREAD_ID": "foreign-session",
                "PATH": os.environ.get("PATH", ""),
            }, clear=True):
                env = runner.isolated_environment(base / "out", project)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("HERDR_SOCKET_PATH", env)
            self.assertNotIn("AGENT_HERDR_SESSION", env)
            self.assertNotIn("CODEX_THREAD_ID", env)
            self.assertEqual(Path(env["HOME"]).parent, base / "out" / "isolated")
            self.assertEqual(Path(env["HERDR_CONFIG_PATH"]).parent.parent,
                             base / "out" / "isolated" / "config")

    def test_attach_url_redaction_keeps_host_and_discards_credentials_and_query(self):
        argv = ["opencode", "attach", "https://user:secret@example.test/path?token=private", "--dir", "/sandbox"]
        safe = runner._safe_argv(argv)
        rendered = " ".join(safe)
        self.assertIn("https://example.test/[redacted]", rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("private", rendered)

    def test_owned_process_group_is_terminated_and_reaped(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        result = runner.stop_owned_process(child, timeout=0.1)
        self.assertIn(result, {"terminated", "killed-after-timeout", "already-exited"})
        self.assertIsNotNone(child.poll())


if __name__ == "__main__":
    unittest.main()
