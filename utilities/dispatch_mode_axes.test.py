#!/usr/bin/env python3
"""Cross-adapter regression for the typed dispatch mode axes."""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = {
    "codex": ["--model", "gpt-test", "--reasoning", "low"],
    "claude": ["--model", "claude-test", "--effort", "low"],
    "opencode": ["--model", "provider/test", "--variant", "low"],
}


class DispatchModeAxesWrapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.artifact_root = self.base / ".agent_reports"
        self.artifact_root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_wrapper(self, harness, *axes):
        # The claude wrapper probes `claude --help` for a standard+ owner and
        # refuses when the binary is absent. This suite covers mode axes, not
        # completion delivery, so ask for the explicit poll fallback instead of
        # depending on the host CLI. `opencode` has no such option.
        completion = ["--completion-delivery", "poll"] if harness != "opencode" else []
        command = [
            sys.executable,
            str(ROOT / f"adapters/{harness}/bin/dispatch-headless.py"),
            "--dry-run",
            "--worktree", str(self.repo),
            "--slug", f"mode-axes-{harness}",
            "--capability", "autopilot-code",
            "--intensity", "standard",
            *axes,
            *completion,
            *ADAPTERS[harness],
        ]
        env = {
            **os.environ,
            "AGENT_HOME": str(ROOT),
            "AGENT_ARTIFACT_ROOT": str(self.artifact_root),
            # Codex projects the canonical registry root into the owner only
            # when it already exists. Keep the dry-run registry inside this
            # fixture instead of inheriting the caller's XDG state root.
            "AGENT_DISPATCH_JOBS": str(self.base / "jobs.log"),
            "OPENCODE_CONFIG_CONTENT": "{}",
        }
        return subprocess.run(command, text=True, capture_output=True, env=env)

    def test_owner_accepts_capability_axis_without_worker_axis(self):
        for harness in ADAPTERS:
            with self.subTest(harness=harness):
                result = self.run_wrapper(
                    harness,
                    "--capability-mode", "dev",
                    "--dispatch-depth", "1",
                    "--worker-type", "owner",
                    "--unit", "_kernel/owner",
                    "--assigned-contract", "autopilot-code",
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("capability_mode=dev", result.stdout)
                self.assertIn("worker_mode=-", result.stdout)
                self.assertNotIn("\nmode=", "\n" + result.stdout)

    def test_owner_rejects_canonical_and_legacy_stage_modes(self):
        cases = (
            ("canonical", "--capability-mode", "dev", "--worker-mode", "plan/plan-author"),
            ("legacy", "--mode", "plan/plan-author"),
        )
        for harness in ADAPTERS:
            for case in cases:
                with self.subTest(harness=harness, form=case[0]):
                    result = self.run_wrapper(
                        harness,
                        *case[1:],
                        "--dispatch-depth", "1",
                        "--worker-type", "owner",
                        "--unit", "_kernel/owner",
                        "--assigned-contract", "autopilot-code",
                    )
                    self.assertEqual(64, result.returncode, result.stdout + result.stderr)
                    self.assertIn("reason=owner-worker-mode-forbidden", result.stdout)
                    self.assertIn("child_spawned=0", result.stdout)

    def test_legacy_scalar_remains_capability_compatibility_input(self):
        for harness in ADAPTERS:
            with self.subTest(harness=harness):
                result = self.run_wrapper(
                    harness,
                    "--mode", "dev",
                    "--dispatch-depth", "1",
                    "--worker-type", "owner",
                    "--unit", "_kernel/owner",
                    "--assigned-contract", "autopilot-code",
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("capability_mode=dev", result.stdout)
                self.assertIn("worker_mode=-", result.stdout)

    def test_stage_requires_exact_worker_mode_unit_projection(self):
        for harness in ADAPTERS:
            with self.subTest(harness=harness):
                result = self.run_wrapper(
                    harness,
                    "--capability-mode", "dev",
                    "--worker-mode", "plan/plan-author",
                    "--dispatch-depth", "2",
                    "--parent", "owner",
                    "--worker-type", "stage",
                    "--unit", "plan/plan-author",
                    "--assigned-contract", "code-plan",
                    "--owner", "autopilot-code",
                    "--parent-harness", harness,
                    "--parent-transport", "headless",
                    "--parent-sandbox", "workspace-write",
                    "--nested-eligibility", "supported",
                    "--eligibility-source", "fixture",
                    "--fallback-ordinal", "1",
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("capability_mode=dev", result.stdout)
                self.assertIn("worker_mode=plan/plan-author", result.stdout)


if __name__ == "__main__":
    unittest.main()
