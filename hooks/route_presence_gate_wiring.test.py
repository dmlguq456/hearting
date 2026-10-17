#!/usr/bin/env python3
"""Each adapter reaches the one route presence gate through its own native surface."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SID = "11111111-2222-3333-4444-555555555555"


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class WiringFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-gate-wiring-")))
        self.repo = self.tmp / "proj"
        (self.repo / "src").mkdir(parents=True)
        git(self.repo, "init", "-q")
        (self.repo / "src" / "engine.py").write_text("x = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        (self.tmp / "scratch").mkdir()
        (self.tmp / "home").mkdir()
        self.ledgers = self.tmp / "ledgers"
        self.root = self.repo / ".agent_reports"
        self.env = {
            "HOME": str(self.tmp / "home"), "PATH": os.environ.get("PATH", ""),
            "TMPDIR": str(self.tmp / "scratch"), "FLEET_ROUTE_CHAIN_DIR": str(self.ledgers),
            "XDG_DATA_HOME": str(self.tmp / "home" / ".local" / "share"),
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ledger_line(self, harness, sid):
        directory = self.ledgers / harness
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{sid}.jsonl").write_text(json.dumps(
            {"v": 1, "event": "compose", "harness": harness, "session_id": sid,
             "artifact_root": str(self.root)}) + "\n")


class HookReleasePinningTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="hook-release-pin-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.projection = self.root / "runtime-hooks"
        self.projection.mkdir()
        self.runner = self.projection / "run-hook.sh"
        self.runner.write_bytes((ROOT / "hooks/run-hook.sh").read_bytes())
        self.hook = "pin-probe.sh"
        projected = self.projection / self.hook
        projected.write_text("#!/bin/sh\necho CURRENT_PROJECTION_RAN\nexit 17\n")
        projected.chmod(0o755)
        self.pinned = self.root / "release"
        (self.pinned / "core").mkdir(parents=True)
        (self.pinned / "core/CORE.md").write_text("fixture harness root\n")
        (self.pinned / "hooks").mkdir()

    def run_hook(self, interpreter, agent_home):
        env = dict(os.environ)
        env.pop("AGENT_HOME", None)
        if agent_home is not None:
            env["AGENT_HOME"] = str(agent_home)
        return subprocess.run(["sh", str(self.runner), interpreter, self.hook],
                              text=True, capture_output=True, env=env)

    def test_missing_pinned_hook_keeps_its_release_and_native_missing_file_result(self):
        for interpreter in ("sh", "exec"):
            with self.subTest(interpreter=interpreter):
                result = self.run_hook(interpreter, self.pinned)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("CURRENT_PROJECTION_RAN", result.stdout)
                self.assertIn(str(self.pinned / "hooks" / self.hook), result.stderr)

    def test_existing_pinned_hook_and_interactive_fallback_keep_the_hook_exit(self):
        pinned_hook = self.pinned / "hooks" / self.hook
        pinned_hook.write_text("#!/bin/sh\necho PINNED_RELEASE_RAN\nexit 13\n")
        pinned_hook.chmod(0o755)
        for interpreter in ("sh", "exec"):
            for root, text, code in ((self.pinned, "PINNED_RELEASE_RAN", 13),
                                     (None, "CURRENT_PROJECTION_RAN", 17),
                                     (self.root / "not-a-harness", "CURRENT_PROJECTION_RAN", 17)):
                with self.subTest(interpreter=interpreter, root=root):
                    result = self.run_hook(interpreter, root)
                    self.assertEqual((result.returncode, result.stdout.strip()), (code, text))


class CodexHookReleasePinningTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="codex-hook-release-pin-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.pinned = self.root / "pinned"
        self.current = self.root / "home/.codex/hearting"
        self.script = "stop-lifecycle.py"
        for root in (self.pinned, self.current):
            (root / "core").mkdir(parents=True)
            (root / "core/CORE.md").write_text("fixture\n")
            (root / "adapters/codex/hooks").mkdir(parents=True)
        self.runner = self.pinned / "adapters/codex/hooks/run-hook.sh"
        self.runner.write_bytes((ROOT / "adapters/codex/hooks/run-hook.sh").read_bytes())
        bridge = self.current / "adapters/codex/hooks" / self.script
        bridge.write_text("print('CURRENT_CODEX_BRIDGE_RAN')\nraise SystemExit(17)\n")
        bridge.chmod(0o755)

    def run_hook(self, root):
        env = {**os.environ, "HOME": str(self.root / "home")}
        env.pop("AGENT_HOME", None)
        if root is not None:
            env["AGENT_HOME"] = str(root)
        return subprocess.run(["sh", str(self.runner), self.script], capture_output=True, text=True, env=env)

    def test_valid_pinned_root_never_replaces_a_missing_bridge(self):
        result = self.run_hook(self.pinned)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("CURRENT_CODEX_BRIDGE_RAN", result.stdout)
        self.assertIn(str(self.pinned / "adapters/codex/hooks" / self.script), result.stderr)

    def test_existing_bridge_and_unpinned_fallback_preserve_exit_codes(self):
        for root in (None, self.root / "invalid"):
            result = self.run_hook(root)
            self.assertEqual((result.returncode, result.stdout.strip()), (17, "CURRENT_CODEX_BRIDGE_RAN"))
        bridge = self.pinned / "adapters/codex/hooks" / self.script
        bridge.write_text("print('PINNED_CODEX_BRIDGE_RAN')\nraise SystemExit(13)\n")
        bridge.chmod(0o755)
        result = self.run_hook(self.pinned)
        self.assertEqual((result.returncode, result.stdout.strip()), (13, "PINNED_CODEX_BRIDGE_RAN"))


class ClaudeWiringTest(WiringFixture):
    def test_settings_register_the_bridge_for_edits_and_bash(self):
        settings = json.loads((ROOT / "adapters/claude/settings.json").read_text())
        entries = [entry for entry in settings["hooks"]["PreToolUse"]
                   if any("route-presence-gate.py" in hook["command"] for hook in entry["hooks"])]
        self.assertEqual(len(entries), 1)
        self.assertEqual(set(entries[0]["matcher"].split("|")),
                         {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"})
        self.assertIn('"$HOME/.claude/hooks/run-hook.sh" python3 route-presence-gate.py',
                      entries[0]["hooks"][0]["command"])
        link = ROOT / "adapters/claude/hooks/route-presence-gate.py"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (ROOT / "hooks/route-presence-gate.py").resolve())

    def test_bridge_refuses_then_passes(self):
        hook = ROOT / "adapters/claude/hooks/route-presence-gate.py"
        payload = json.dumps({"tool_name": "Edit", "session_id": SID, "cwd": str(self.repo),
                              "tool_input": {"file_path": str(self.repo / "src/engine.py")}})
        first = subprocess.run(["python3", str(hook)], input=payload, capture_output=True,
                               text=True, env=self.env)
        self.assertEqual(first.returncode, 2)
        self.assertIn("capability-route.py compose --start --shape direct", first.stderr)
        self.ledger_line("claude", SID)
        second = subprocess.run(["python3", str(hook)], input=payload, capture_output=True,
                                text=True, env=self.env)
        self.assertEqual((second.returncode, second.stderr), (0, ""))


class CodexWiringTest(WiringFixture):
    def command(self):
        hooks = json.loads((ROOT / "adapters/codex/hooks/hooks.json").read_text())["hooks"]
        entries = [entry for entry in hooks["PreToolUse"]
                   if any("route-presence-gate.py" in hook["command"] for hook in entry["hooks"])]
        self.assertEqual(len(entries), 1)
        for tool in ("Write", "Edit", "apply_patch", "Bash", "Shell", "functions\\.exec_command"):
            self.assertIn(tool, entries[0]["matcher"].split("|"))
        return entries[0]["hooks"][0]["command"]

    def run_hook(self, payload):
        return subprocess.run(["sh", "-c", self.command()], input=json.dumps(payload),
                              capture_output=True, text=True,
                              env={**self.env, "AGENT_HOME": str(ROOT)})

    def test_run_hook_allows_the_bridge_and_blocks_with_codex_json(self):
        (self.repo / "src/engine.py").write_text("x = 2\n")
        payload = {"tool_name": "Bash", "session_id": "thread-1", "cwd": str(self.repo),
                   "tool_input": {"command": "git commit -am fix"}}
        first = self.run_hook(payload)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["decision"], "block")
        self.ledger_line("codex", "thread-1")
        second = self.run_hook(payload)
        self.assertEqual((second.returncode, second.stdout), (0, ""))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class OpenCodeWiringTest(WiringFixture):
    def call(self, tool, args, sid="ses_1"):
        script = f"""
process.env.AGENT_HOME = {json.dumps(str(ROOT))}
const mod = await import({json.dumps(str(ROOT / "adapters/opencode/plugins/hearting-guards.js"))})
const plugin = await mod.AgentHarnessGuards({{ directory: {json.dumps(str(self.repo))}, worktree: {json.dumps(str(self.repo))} }})
try {{
  await plugin["tool.execute.before"]({{ tool: {json.dumps(tool)}, sessionID: {json.dumps(sid)} }}, {{ args: {json.dumps(args)} }})
  console.log("PASS")
}} catch (error) {{
  console.log("THROW " + error.message)
}}
"""
        result = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True,
                                text=True, env=self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_plugin_gates_edits_commits_and_passes_artifacts(self):
        edit = {"filePath": str(self.repo / "src/engine.py"), "oldString": "1", "newString": "2"}
        self.assertTrue(self.call("edit", edit).startswith("THROW hearting: this session has no route"))
        self.assertEqual(self.call("write", {"filePath": str(self.root / "notes.md")}), "PASS")
        (self.repo / "src/engine.py").write_text("x = 2\n")
        self.assertTrue(self.call("bash", {"command": "git commit -am x"}).startswith("THROW"))
        self.assertEqual(self.call("bash", {"command": "git status"}), "PASS")
        self.ledger_line("opencode", "ses_1")
        self.assertEqual(self.call("edit", edit), "PASS")
        self.assertEqual(self.call("bash", {"command": "git commit -am x"}), "PASS")


if __name__ == "__main__":
    unittest.main()
