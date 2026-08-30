#!/usr/bin/env python3
"""Thin conformance: preflight.sh `dispatch-owner` delegates to
utilities/dispatch-owner.py, and the low-level `dispatch` arm still reaches
the Codex wrapper directly, unchanged."""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
PREFLIGHT = ROOT / "adapters" / "codex" / "bin" / "preflight.sh"


class PreflightDispatchOwnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "core").mkdir(parents=True)
        (self.home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        self.jobs = self.home / "jobs.log"
        self.jobs.touch()
        self.config = self.home / "dispatch-defaults.yaml"
        self.config.write_text(
            "schema_version: 1\ndepth1_owner: [codex]\nopencode:\n  relief_only: true\ncapabilities:\n",
            encoding="utf-8",
        )
        self.git_config = self.home / "gitconfig"
        self.git_config.write_text(
            "[safe]\n\tdirectory = %s\n" % ROOT,
            encoding="utf-8",
        )
        # Strip every ambient dispatch/runtime identity variable by prefix
        # rather than an enumerated list: this test process itself may be a
        # real registered nested worker (AGENT_DISPATCH_CHILD=1, a live
        # managed gateway socket, real CODEX_THREAD_ID/CLAUDE_CODE_SESSION_ID,
        # a real parent attempt id, ...), and any one of those leaking into a
        # subprocess meant to simulate an *unmanaged* interactive parent would
        # silently borrow this session's real managed/parent identity instead
        # of exercising the fixture's simulated one.
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("AGENT_", "CODEX_", "CLAUDE_"))
            and k not in ("GIT_DIR", "GIT_WORK_TREE")
        }
        self.env.update({
            "AGENT_HOME": str(self.home),
            "HOME": str(self.home),
            "DISPATCH_DEFAULTS_CONFIG": str(self.config),
            "GIT_CONFIG_GLOBAL": str(self.git_config),
            # Deterministic capacity so adapter selection never depends on
            # this process's own real ambient Codex/Claude session history.
            "HARNESS_CAPACITY_SCORES": "claude:80,codex:80,opencode:80",
        })

    def test_dispatch_owner_arm_delegates_to_selector(self):
        result = subprocess.run(
            [str(PREFLIGHT), "dispatch-owner"],
            text=True, capture_output=True, env=self.env, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("usage: dispatch-owner", result.stdout)

    def test_dispatch_owner_arm_selects_configured_adapter_end_to_end(self):
        log_dir = self.home / "logs"
        args = [
            str(PREFLIGHT), "dispatch-owner", "--dry-run",
            "--worktree", str(ROOT), "--slug", "conformance-owner-test",
            "--capability", "autopilot-code", "--capability-mode", "debug",
            "--qa", "standard", "--intensity", "standard",
            "--dispatch-depth", "1", "--worker-type", "owner",
            "--assigned-contract", "autopilot-code", "--owner", "autopilot-code",
            "--model-profile", "deep", "--jobs", str(self.jobs), "--log-dir", str(log_dir),
        ]
        result = subprocess.run(args, text=True, capture_output=True, env=self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("adapter=codex", result.stdout)
        self.assertIn("selection_source=configured-normal", result.stdout)

    def test_unmanaged_codex_parent_fails_before_either_owner_registration(self):
        for adapter in ("codex", "claude"):
            with self.subTest(adapter=adapter):
                jobs = self.home / f"jobs-{adapter}.log"
                jobs.touch()
                log_dir = self.home / f"logs-{adapter}"
                args = [
                    str(PREFLIGHT), "dispatch-owner", "--adapter", adapter, "--start",
                    "--worktree", str(ROOT), "--slug", f"unmanaged-{adapter}-owner-test",
                    "--capability", "autopilot-code", "--capability-mode", "debug",
                    "--qa", "standard", "--intensity", "standard",
                    "--dispatch-depth", "1", "--worker-type", "owner",
                    "--assigned-contract", "autopilot-code", "--owner", "autopilot-code",
                    "--model-profile", "deep", "--jobs", str(jobs), "--log-dir", str(log_dir),
                ]
                env = {
                    **self.env,
                    "CODEX_THREAD_ID": "thread-unmanaged-test",
                    "AGENT_DISPATCH_CALLER_HARNESS": "codex",
                    "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                    "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                    "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
                }
                result = subprocess.run(
                    args, text=True, capture_output=True, env=env, timeout=20
                )
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 69, output)
                self.assertIn(f"adapter={adapter}", output)
                self.assertIn("managed-entry-required", output)
                self.assertIn("parent_completion_delivery=poll-fallback", output)
                self.assertIn("child_spawned=0", output)
                self.assertEqual(jobs.read_text(encoding="utf-8"), "")

    def test_low_level_dispatch_arm_still_reaches_codex_wrapper_directly(self):
        result = subprocess.run(
            [str(PREFLIGHT), "dispatch"],
            text=True, capture_output=True, env=self.env, timeout=20,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dispatch-headless.py", result.stdout + result.stderr)
        self.assertNotIn("usage: dispatch-owner", result.stdout + result.stderr)
        self.assertNotIn("adapter=", result.stdout)


if __name__ == "__main__":
    unittest.main()
