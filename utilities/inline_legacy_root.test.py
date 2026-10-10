#!/usr/bin/env python3
"""Direct routes in an artifact root whose cutover is inactive with legacy content."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import artifact_producer

CLI = [sys.executable, str(ROOT / "utilities/capability-route.py")]


class LegacyRootDirectRouteTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="legacy-root-direct-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.repo, self.root, self.jobs = base / "repo", base / "repo/.agent_reports", base / "jobs.log"
        self.repo.mkdir()
        self.jobs.write_text("", encoding="utf-8")
        git = ["git", "-C", str(self.repo)]
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(git + ["config", "user.email", "t@example.com"], check=True)
        subprocess.run(git + ["config", "user.name", "t"], check=True)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(git + ["add", "README.md"], check=True)
        subprocess.run(git + ["commit", "-qm", "base"], check=True)
        legacy = self.root / "analysis_project/summary.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("legacy\n", encoding="utf-8")
        self.assertEqual(artifact_producer.classify_root(self.root)["state"], "inactive-with-legacy")
        self.env = {**os.environ, "AGENT_HOME": str(ROOT), "AGENT_DISPATCH_JOBS": str(self.jobs),
                    "AGENT_DISPATCH_DEPTH": "0", "CODEX_THREAD_ID": "legacy-root-test-session",
                    "XDG_STATE_HOME": str(base / "state"), "AGENT_ARTIFACT_CHECKPOINT": "off",
                    "HEARTING_WORKFLOW_GROUP_REVIEW": "off"}
        for key in ("AGENT_DISPATCH_ATTEMPT_ID", "CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID",
                    "AGENT_DISPATCH_CALLER_HARNESS", "AGENT_ARTIFACT_ROOT"):
            self.env.pop(key, None)
        prompt = base / "task.md"
        prompt.write_text("Fix the README.\n", encoding="utf-8")
        self.compose = CLI + ["compose", "--slug", "legacy-direct", "--campaign-key", "legacy-direct",
                              "--shape", "direct", "--cwd", str(self.repo), "--artifact-root", str(self.root),
                              "--prompt-file", str(prompt), "--spec-read", "fixture",
                              "--drift-verdict", "within-spec", "--artifact-guard", "fixture",
                              "--parent-harness", "codex"]

    def run_cli(self, argv):
        return subprocess.run(argv, cwd=self.repo, env=self.env, capture_output=True, text=True)

    def test_compose_start_returns_legacy_compat_env_not_a_traceback(self):
        started = self.run_cli(self.compose + ["--start"])
        self.assertNotIn("Traceback", started.stderr)
        self.assertEqual(started.returncode, 0, started.stderr[-2000:])
        receipt = json.loads(started.stdout.strip().splitlines()[-1])
        self.assertEqual(receipt["state"], "inline", receipt)
        self.assertEqual(receipt["artifact_env"], {"AGENT_ARTIFACT_ROOT": str(self.root.resolve())})
        self.assertFalse((self.root / "campaigns").exists())   # nothing activated or issued

    def test_finish_closes_a_legacy_direct_route_with_a_proven_gate(self):
        composed = self.run_cli(self.compose)
        self.assertEqual(composed.returncode, 0, composed.stderr[-2000:])
        route_file = json.loads(composed.stdout)["route_file"]
        (self.repo / "README.md").write_text("fixed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "fix"], check=True)
        evidence = self.root / "analysis_project/evidence.md"
        evidence.write_text("done\n", encoding="utf-8")
        summary = Path(self.temp.name) / "summary.md"
        summary.write_text("Fixed the README.\n", encoding="utf-8")
        finished = self.run_cli(CLI + ["finish", "--route", route_file, "--evidence", str(evidence),
                                       "--summary-file", str(summary)])
        self.assertEqual(finished.returncode, 0, finished.stderr[-2000:])
        receipt = json.loads(finished.stdout)
        self.assertIs(receipt["outcome"]["terminal_gate_proven"], True, receipt)


if __name__ == "__main__":
    unittest.main()
