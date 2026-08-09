#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


def run(argv: list[str], cwd: Path, env: dict[str, str] | None = None):
    result = subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"{argv}:\n{result.stdout}\n{result.stderr}")
    return result


class DispatchArtifactRootTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "project"
        self.worktree = self.base / "project-wt" / "topic"
        self.logs = self.base / "logs"
        self.jobs = self.base / "jobs.log"
        self.repo.mkdir()
        run(["git", "init", "-q"], self.repo)
        run(["git", "config", "user.name", "test"], self.repo)
        run(["git", "config", "user.email", "test@example.com"], self.repo)
        (self.repo / ".agent_reports").mkdir()
        (self.repo / ".agent_reports" / "marker").write_text("canonical\n", encoding="utf-8")
        run(["git", "add", "."], self.repo)
        run(["git", "commit", "-qm", "base"], self.repo)
        self.worktree.parent.mkdir(parents=True)
        run(
            ["git", "worktree", "add", "-q", "-b", "topic", str(self.worktree)],
            self.repo,
        )
        self.canonical = str(self.repo / ".agent_reports")
        evidence = self.base / "quick-evidence.json"
        evidence.write_text(
            json.dumps(
                {
                    "candidates": [
                        {
                            "harness": adapter,
                            "transport": "headless",
                            "surface": "registered-headless",
                            "status": "supported",
                            "probe_source": "artifact-root-fixture",
                            "probe_time": "2026-07-24T00:00:00Z",
                        }
                        for adapter in ("claude", "codex", "opencode")
                    ]
                }
            ),
            encoding="utf-8",
        )
        route_dir = Path(self.canonical) / ".runtime" / "routes"
        route_dir.mkdir(parents=True)
        self.route_file = route_dir / "quick-route.json"
        run(
            [
                sys.executable,
                str(ROOT / "utilities" / "capability-route.py"),
                "compile",
                "--capability",
                "autopilot-code",
                "--capability-mode",
                "dev",
                "--intensity",
                "quick",
                "--cwd",
                str(self.worktree),
                "--artifact-root",
                self.canonical,
                "--registered-headless-evidence",
                str(evidence),
                "--tracking",
                "tracked",
                "--spec-read",
                "artifact-root-fixture",
                "--drift-verdict",
                "within-spec",
                "--workflow-mode",
                "tracked",
                "--artifact-guard",
                "artifact-root-fixture",
                "--output",
                str(self.route_file),
            ],
            ROOT,
        )
        self.route = json.loads(self.route_file.read_text(encoding="utf-8"))
        self.route_node = self.route["nodes"][0]

    def tearDown(self):
        self.temp.cleanup()

    def wrapper_args(self, adapter: str) -> list[str]:
        path = ROOT / "adapters" / adapter / "bin" / "dispatch-headless.py"
        args = [
            str(path),
            "--register",
            "--worktree",
            str(self.worktree),
            "--slug",
            f"{adapter}-artifact-root",
            "--capability",
            "autopilot-code",
            "--capability-mode",
            self.route["capability_mode"],
            "--intensity",
            "quick",
            "--route-file",
            str(self.route_file),
            "--route-id",
            self.route["route_id"],
            "--route-hash",
            self.route["route_hash"],
            "--route-node",
            "one-shot",
            "--registry-digest",
            self.route["registry_digest"],
            "--write-scope",
            ";".join(self.route_node["write_scope"]),
            "--completion-gate",
            self.route_node["completion_gate"],
            "--attempt-id",
            f"att-{adapter}-artifact-root",
            "--jobs",
            str(self.jobs),
            "--log-dir",
            str(self.logs),
            "--model-profile",
            self.route_node["model_profile"],
        ]
        return args

    def assert_registered(self, adapter: str, extra_command: str | None = None):
        result = run(self.wrapper_args(adapter), ROOT)
        self.assertIn(f"artifact_root={self.canonical}", result.stdout)
        self.assertIn("artifact_write_scope=canonical-only", result.stdout)
        if extra_command:
            self.assertIn(extra_command, result.stdout)
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn(f"artifact_root={self.canonical}", registry)
        prompt = next(
            self.logs.glob(f"{adapter}-artifact-root.*.{adapter}.prompt.txt")
        )
        body = prompt.read_text(encoding="utf-8")
        self.assertIn(self.canonical, body)
        self.assertIn("read-only shadow state", body)

    def test_claude_scopes_add_dir_and_metadata(self):
        self.assert_registered("claude", f"--add-dir {self.canonical}")

    def test_codex_scopes_add_dir_and_metadata(self):
        self.assert_registered("codex", f"--add-dir {self.canonical}")

    def test_opencode_scopes_external_directory_and_preserves_config(self):
        existing = {
            "permission": {
                "bash": "deny",
                "external_directory": {"*": "ask", "/keep/**": "deny"},
            },
            "theme": "system",
        }
        env = {**os.environ, "OPENCODE_CONFIG_CONTENT": json.dumps(existing)}
        result = run(self.wrapper_args("opencode"), ROOT, env=env)
        self.assertIn(f"artifact_root={self.canonical}", result.stdout)
        self.assertIn("external_directory_permission=scoped-allow", result.stdout)

        module_path = ROOT / "adapters" / "opencode" / "bin" / "dispatch-headless.py"
        spec = importlib.util.spec_from_file_location("opencode_dispatch", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        old = os.environ.get("OPENCODE_CONFIG_CONTENT")
        os.environ["OPENCODE_CONFIG_CONTENT"] = json.dumps(existing)
        try:
            merged = json.loads(module.scoped_external_directory_config(self.canonical))
        finally:
            if old is None:
                os.environ.pop("OPENCODE_CONFIG_CONTENT", None)
            else:
                os.environ["OPENCODE_CONFIG_CONTENT"] = old
        self.assertEqual(merged["theme"], "system")
        self.assertEqual(merged["permission"]["bash"], "deny")
        rules = merged["permission"]["external_directory"]
        self.assertEqual(rules["*"], "ask")
        self.assertEqual(rules["/keep/**"], "deny")
        self.assertEqual(rules[self.canonical], "allow")
        self.assertEqual(rules[f"{self.canonical}/**"], "allow")


if __name__ == "__main__":
    unittest.main()
