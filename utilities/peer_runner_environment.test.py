#!/usr/bin/env python3
"""The shared runner a worker starts runs as the user's own service (#1576 review follow-ups).

A worker's masked harness homes lead back to the user's home through each worker
home's manifest, including a user who chose a home that is not the default and a
worker started under another worker or under another harness's worker. The
runner is not marked as a worker either.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import tempfile
import unittest
from pathlib import Path

import peer_obligations as obligations
import session_tidy


class RunnerEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.user_home = self.root / "home"
        self.alt_claude = self.root / "alt" / "claude-profile"
        self.alt_codex = self.root / "alt" / "codex-profile"
        for path in (self.user_home, self.alt_claude, self.alt_codex):
            path.mkdir(parents=True)

    def worker_home(self, name, replaced, harness):
        home = self.root / "workers" / name
        home.mkdir(parents=True)
        (home / "worker-home.json").write_text(json.dumps(
            {"harness": harness, "schema": 2, "model_config_home": str(replaced)}), encoding="utf-8")
        return home

    def env(self, **values):
        base = {"HOME": str(self.user_home), "PATH": "/usr/bin"}
        base.update({key: str(value) for key, value in values.items()})
        return base

    def test_depth_two_claude_worker_leads_back_to_the_users_alternate_home(self):
        first = self.worker_home("claude-1", self.alt_claude, "claude")
        second = self.worker_home("claude-2", first, "claude")
        result = obligations._user_service_environment(self.env(
            CLAUDE_CONFIG_DIR=second, HEARTING_WORKER_HOME=second, HEARTING_MODEL_CONFIG_HOME=first))
        self.assertEqual(result["CLAUDE_CONFIG_DIR"], str(self.alt_claude))
        self.assertNotIn("HEARTING_WORKER_HOME", result)
        self.assertNotIn("HEARTING_MODEL_CONFIG_HOME", result)

    def test_codex_worker_under_a_claude_worker_keeps_both_alternate_homes(self):
        claude_worker = self.worker_home("claude-1", self.alt_claude, "claude")
        codex_worker = self.worker_home("codex-1", self.alt_codex, "codex")
        result = obligations._user_service_environment(self.env(
            CLAUDE_CONFIG_DIR=claude_worker, CODEX_HOME=codex_worker,
            HEARTING_WORKER_HOME=codex_worker, HEARTING_MODEL_CONFIG_HOME=self.alt_codex))
        self.assertEqual(result["CLAUDE_CONFIG_DIR"], str(self.alt_claude))
        self.assertEqual(result["CODEX_HOME"], str(self.alt_codex))

    def test_claude_worker_under_a_codex_worker_keeps_both_alternate_homes(self):
        codex_worker = self.worker_home("codex-1", self.alt_codex, "codex")
        claude_worker = self.worker_home("claude-1", self.alt_claude, "claude")
        result = obligations._user_service_environment(self.env(
            CLAUDE_CONFIG_DIR=claude_worker, CODEX_HOME=codex_worker,
            HEARTING_WORKER_HOME=claude_worker, HEARTING_MODEL_CONFIG_HOME=self.alt_claude))
        self.assertEqual(result["CLAUDE_CONFIG_DIR"], str(self.alt_claude))
        self.assertEqual(result["CODEX_HOME"], str(self.alt_codex))

    def test_default_homes_fall_back_to_the_native_default(self):
        worker = self.worker_home("claude-1", self.user_home / ".claude", "claude")
        result = obligations._user_service_environment(self.env(
            CLAUDE_CONFIG_DIR=worker, HEARTING_WORKER_HOME=worker,
            HEARTING_MODEL_CONFIG_HOME=self.user_home / ".claude"))
        self.assertNotIn("CLAUDE_CONFIG_DIR", result)

    def test_a_home_without_the_manifest_field_uses_the_pinned_home(self):
        worker = self.root / "workers" / "old"
        worker.mkdir(parents=True)
        (worker / "worker-home.json").write_text("{}", encoding="utf-8")
        result = obligations._user_service_environment(self.env(
            CLAUDE_CONFIG_DIR=worker, HEARTING_WORKER_HOME=worker,
            HEARTING_MODEL_CONFIG_HOME=self.alt_claude))
        self.assertEqual(result["CLAUDE_CONFIG_DIR"], str(self.alt_claude))

    def test_a_pinned_home_that_no_longer_exists_means_the_default(self):
        worker = self.worker_home("claude-1", self.root / "gone", "claude")
        result = obligations._user_service_environment(self.env(
            CLAUDE_CONFIG_DIR=worker, HEARTING_WORKER_HOME=worker,
            HEARTING_MODEL_CONFIG_HOME=self.root / "gone"))
        self.assertNotIn("CLAUDE_CONFIG_DIR", result)

    def test_a_session_that_chose_its_own_home_is_unchanged(self):
        environ = self.env(CLAUDE_CONFIG_DIR=self.alt_claude)
        self.assertIs(obligations._user_service_environment(environ), environ)

    def test_the_runner_environment_carries_no_worker_identity(self):
        worker = self.worker_home("claude-1", self.alt_claude, "claude")
        result = obligations._user_service_environment(self.env(
            CLAUDE_CONFIG_DIR=worker, HEARTING_WORKER_HOME=worker, HEARTING_MODEL_CONFIG_HOME=self.alt_claude,
            AGENT_SESSION_ROLE="worker", AGENT_DISPATCH_CHILD="1", AGENT_DISPATCH_DEPTH="2",
            CLAUDE_CODE_SESSION_ID="sid-worker", CLAUDE_PID="123", CLAUDE_SECURESTORAGE_CONFIG_DIR="/x",
            AGENT_CODEX_MANAGED_CONTROL_SOCKET="/tmp/socket", OPENCODE_DISPATCH_PARENT="p",
            AGENT_DISPATCH_JOBS=self.root / "jobs.log"))
        for key in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                    "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
                    "AGENT_CODEX_MANAGED_CONTROL_SOCKET", "OPENCODE_DISPATCH_PARENT"):
            self.assertNotIn(key, result)
        self.assertFalse(session_tidy.is_worker(result))
        # The state root pointer is not an identity; the runner still finds its store.
        self.assertEqual(result["AGENT_DISPATCH_JOBS"], str(self.root / "jobs.log"))


if __name__ == "__main__":
    unittest.main()
