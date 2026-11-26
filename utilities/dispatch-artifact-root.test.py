#!/usr/bin/env python3
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
CLEAN_ENV = {key: value for key, value in os.environ.items() if not key.startswith((
    "AGENT_DISPATCH_", "AGENT_ROUTE_", "AGENT_REVIEW_", "AGENT_ARTIFACT_", "AGENT_MODEL_GOVERNOR_"))}


def run(argv: list[str], cwd: Path, env: dict[str, str] | None = None):
    result = subprocess.run(
        argv,
        cwd=str(cwd),
        env={**CLEAN_ENV, "AGENT_HOME": str(ROOT), **(env or {})},
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
        compiled = run(
            [
                sys.executable,
                str(ROOT / "utilities" / "capability-route.py"),
                "compile",
                "--slug",
                "artifact-root-fixture",
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
            ],
            ROOT,
        )
        self.route = json.loads(compiled.stdout)
        self.route_file = route_dir / f"{self.route['route_id']}.json"
        self.assertTrue(self.route_file.is_file())
        self.route_node = next(node for node in self.route["nodes"] if node["id"] == "one-shot")

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
        env={**CLEAN_ENV,"AGENT_ARTIFACT_ROOT":self.canonical,
             "AGENT_ROUTE_FILE":str(self.route_file),"AGENT_ROUTE_ID":self.route["route_id"],
             "AGENT_ROUTE_NODE":"one-shot","AGENT_DISPATCH_WORKTREE":str(self.worktree)}
        result = run(self.wrapper_args(adapter), ROOT, env=env)
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
        return result

    def test_claude_scopes_add_dir_and_metadata(self):
        self.assert_registered("claude", f"--add-dir {self.canonical}")

    def test_codex_scopes_add_dir_and_metadata(self):
        result = self.assert_registered("codex")
        # The app-server supervisor and plain `codex exec` spell the same scope differently;
        # which one launches depends on the Codex runtime available here.
        command = next(line for line in result.stdout.splitlines() if line.startswith("command="))
        flag = "--writable-root" if "codex-app-server-supervisor.py" in command else "--add-dir"
        self.assertIn(f"{flag} {self.canonical}", command)

    def test_opencode_scopes_external_directory_and_preserves_config(self):
        v1_existing = {
            "permission": {
                "bash": "deny",
                "external_directory": {"*": "ask", "/keep/**": "deny"},
            },
            "theme": "system",
        }
        v2_existing = {
            "permissions": [
                {"action": "shell", "resource": "*", "effect": "deny"},
                {"action": "external_directory", "resource": "*", "effect": "ask"},
                {"action": "external_directory", "resource": "/keep/**", "effect": "deny"},
            ],
            "agents": {},
            "theme": "system",
        }
        module_path = ROOT / "adapters" / "opencode" / "bin" / "dispatch-headless.py"
        spec = importlib.util.spec_from_file_location("opencode_dispatch", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        actual_v2 = module.opencode_runtime.is_v2()
        existing = v2_existing if actual_v2 else v1_existing
        env = {**CLEAN_ENV, "AGENT_ARTIFACT_ROOT":self.canonical,
               "AGENT_ROUTE_FILE":str(self.route_file),"AGENT_ROUTE_ID":self.route["route_id"],
               "AGENT_ROUTE_NODE":"one-shot","AGENT_DISPATCH_WORKTREE":str(self.worktree),
               "OPENCODE_CONFIG_CONTENT": json.dumps(existing)}
        result = run(self.wrapper_args("opencode"), ROOT, env=env)
        self.assertIn(f"artifact_root={self.canonical}", result.stdout)
        self.assertIn("external_directory_permission=scoped-allow", result.stdout)

        import dispatch_replacement
        original_version = os.environ.get("OPENCODE_RUNTIME_VERSION")
        original_config = os.environ.get("OPENCODE_CONFIG_CONTENT")
        try:
            for version, source in (("1.19.0", v1_existing), ("2.0.32", v2_existing)):
                os.environ["OPENCODE_RUNTIME_VERSION"] = version
                os.environ["OPENCODE_CONFIG_CONTENT"] = json.dumps(source)
                merged = json.loads(module.scoped_external_directory_config(self.canonical))
                if version.startswith("2."):
                    self.assertNotIn("permission", merged)
                    self.assertNotIn("agent", merged)
                    self.assertIn("permissions", merged)
                    self.assertIn("agents", merged)
                else:
                    self.assertIn("permission", merged)
                recorded = dispatch_replacement._applied_permissions(
                    SimpleNamespace(opencode_config_content=json.dumps(merged)))
                rules = recorded["opencode_permission"]
                self.assertTrue(rules)
                self.assertEqual(module.opencode_runtime.opencode_permission_rules_for_action(
                    rules, "bash"), [("*", "deny")])
                external = rules["external_directory"]
                self.assertEqual(external["*"], "ask")
                self.assertEqual(external["/keep/**"], "deny")
                self.assertEqual(external[self.canonical], "allow")
                self.assertEqual(external[f"{self.canonical}/**"], "allow")
                self.assertEqual(merged["theme"], "system")
        finally:
            if original_version is None:
                os.environ.pop("OPENCODE_RUNTIME_VERSION", None)
            else:
                os.environ["OPENCODE_RUNTIME_VERSION"] = original_version
            if original_config is None:
                os.environ.pop("OPENCODE_CONFIG_CONTENT", None)
            else:
                os.environ["OPENCODE_CONFIG_CONTENT"] = original_config


if __name__ == "__main__":
    unittest.main()
