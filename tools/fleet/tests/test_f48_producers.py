"""F-48 Claude/Codex hook producers are silent, exact and worker-safe."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import interaction  # noqa: E402


def _repo_root():
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "core" / "CORE.md").is_file():
            return candidate
    raise RuntimeError("repository root not found")


ROOT = _repo_root()
CLAUDE = ROOT / "adapters/claude/hooks/fleet-interaction-state.py"
CODEX_SET = ROOT / "adapters/codex/hooks/permissionrequest-lifecycle.py"
CODEX_CLEAR = ROOT / "adapters/codex/hooks/posttooluse-interaction-clear.py"
CODEX_STOP = ROOT / "adapters/codex/hooks/stop-lifecycle.py"
WORKER_KEYS = (
    "AGENT_SESSION_ROLE",
    "AGENT_DISPATCH_CHILD",
    "AGENT_DISPATCH_DEPTH",
    "CLAUDE_CODE_CHILD_SESSION",
    "OPENCODE_DISPATCH_SLUG",
    "FLEET_TITLE_REFRESH",
    "MEM_DISTILL",
)


class ProducerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = os.environ.copy()
        for key in WORKER_KEYS:
            self.env.pop(key, None)
        self.env["FLEET_INTERACTION_STATE_DIR"] = self.tmp.name
        os.environ["FLEET_INTERACTION_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("FLEET_INTERACTION_STATE_DIR", None)
        self.tmp.cleanup()

    def run_hook(self, script, payload, *args, env=None):
        result = subprocess.run(
            [sys.executable, str(script), *args],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env or self.env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        return result

    def test_claude_decision_permission_and_release(self):
        payload = {"session_id": "claude-sid", "hook_event_name": "PreToolUse"}
        self.run_hook(CLAUDE, payload, "set", "--kind", "decision")
        record = interaction.read_wait("claude-sid", "claude")
        self.assertEqual((record["kind"], record["source"]),
                         ("decision", "claude-asktool"))
        self.assertNotIn("hook_event_name", record)
        self.run_hook(CLAUDE, payload, "clear")
        self.assertIsNone(interaction.read_wait("claude-sid", "claude"))
        self.run_hook(CLAUDE, payload, "set", "--kind", "permission")
        self.assertEqual(interaction.read_wait("claude-sid", "claude")["kind"],
                         "permission")

    def test_claude_question_permission_keeps_the_decision_wait(self):
        # Claude raises PermissionRequest for AskUserQuestion after PreToolUse.
        self.run_hook(CLAUDE, {"session_id": "claude-q", "tool_name": "AskUserQuestion"},
                      "set", "--kind", "decision")
        self.run_hook(CLAUDE, {"session_id": "claude-q", "tool_name": "AskUserQuestion",
                               "hook_event_name": "PermissionRequest"},
                      "set", "--kind", "permission")
        record = interaction.read_wait("claude-q", "claude")
        self.assertEqual((record["kind"], record["source"]),
                         ("decision", "claude-asktool"))

    def test_claude_subagent_and_each_worker_gate_are_noops(self):
        payload = {"session_id": "sid", "agent_id": "subagent"}
        interaction.set_wait("sid", "claude", "decision", "claude-asktool")
        self.run_hook(CLAUDE, payload, "clear")
        self.assertIsNotNone(interaction.read_wait("sid", "claude"))
        interaction.clear_wait("sid", "claude")
        self.run_hook(CLAUDE, payload, "set", "--kind", "decision")
        self.assertIsNone(interaction.read_wait("sid", "claude"))
        values = {
            "AGENT_SESSION_ROLE": "worker", "AGENT_DISPATCH_CHILD": "1",
            "AGENT_DISPATCH_DEPTH": "1",
            "OPENCODE_DISPATCH_SLUG": "child", "FLEET_TITLE_REFRESH": "1",
            "MEM_DISTILL": "1",
        }
        for key, value in values.items():
            env = dict(self.env, **{key: value})
            interaction.set_wait("sid", "claude", "decision", "claude-asktool")
            self.run_hook(CLAUDE, {"session_id": "sid"}, "clear", env=env)
            self.assertIsNotNone(interaction.read_wait("sid", "claude"), key)
            interaction.clear_wait("sid", "claude")
            self.run_hook(CLAUDE, {"session_id": "sid"}, "set", "--kind", "decision", env=env)
            self.assertIsNone(interaction.read_wait("sid", "claude"), key)

    def test_runtime_child_session_marker_alone_is_not_a_worker(self):
        # core/OPERATIONS.md §5.10: a runtime injects CLAUDE_CODE_CHILD_SESSION into
        # every child process an ordinary interactive session spawns — hooks included —
        # so an agent-team teammate session carries it. Only harness-planted markers
        # gate the lifecycle; the teammate's waits must still reach Fleet.
        env = dict(self.env, CLAUDE_CODE_CHILD_SESSION="1")
        payload = {"session_id": "teammate-sid", "hook_event_name": "PreToolUse"}
        self.run_hook(CLAUDE, payload, "set", "--kind", "decision", env=env)
        record = interaction.read_wait("teammate-sid", "claude")
        self.assertIsNotNone(record)
        self.assertEqual(record["kind"], "decision")
        self.run_hook(CLAUDE, payload, "clear", env=env)
        self.assertIsNone(interaction.read_wait("teammate-sid", "claude"))
        # The same marker on the Codex clear producer is likewise not worker evidence.
        interaction.set_wait("teammate-sid", "codex", "approval",
                             "codex-permissionrequest")
        self.run_hook(CODEX_CLEAR, {"thread_id": "teammate-sid"}, env=env)
        self.assertIsNone(interaction.read_wait("teammate-sid", "codex"))

    def test_claude_missing_or_unsafe_sid_is_silent(self):
        self.run_hook(CLAUDE, {}, "set", "--kind", "decision")
        self.run_hook(CLAUDE, {"session_id": "../sid"}, "set", "--kind", "decision")
        self.assertFalse(list(Path(self.tmp.name).rglob("*.json")))

    def test_codex_approval_immediate_release_and_stop_backstop(self):
        payload = {"context": {"thread_id": "codex-sid"}, "tool_name": "exec"}
        self.run_hook(CODEX_SET, payload)
        record = interaction.read_wait("codex-sid", "codex")
        self.assertEqual((record["kind"], record["source"]),
                         ("approval", "codex-permissionrequest"))
        self.run_hook(CODEX_CLEAR, payload)
        self.assertIsNone(interaction.read_wait("codex-sid", "codex"))
        self.run_hook(CODEX_SET, payload)
        self.run_hook(CODEX_STOP, payload)
        self.assertIsNone(interaction.read_wait("codex-sid", "codex"))

    def test_codex_missing_sid_and_worker_are_noops(self):
        self.run_hook(CODEX_SET, {"tool_name": "exec"})
        self.assertFalse(list(Path(self.tmp.name).rglob("*.json")))
        env = dict(self.env, AGENT_DISPATCH_CHILD="1")
        self.run_hook(CODEX_SET, {"thread_id": "sid"}, env=env)
        self.assertIsNone(interaction.read_wait("sid", "codex"))
        interaction.set_wait("sid", "codex", "approval", "codex-permissionrequest")
        self.run_hook(CODEX_CLEAR, {"thread_id": "sid"}, env=env)
        self.run_hook(CODEX_STOP, {"thread_id": "sid"}, env=env)
        self.assertIsNotNone(interaction.read_wait("sid", "codex"))

    def shell_reports(self, script, payload, *args):
        """Run one hook in process; return the session-shell lifecycle reports it made."""
        import io
        import runpy
        import types
        from unittest import mock
        calls = []
        fake = types.ModuleType("session_shell")
        fake.lifecycle = lambda harness, event, sid, **_: calls.append((harness, event, sid))
        with mock.patch.dict(sys.modules, {"session_shell": fake}), \
                mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": self.tmp.name}), \
                mock.patch.object(sys, "argv", [str(script), *args]), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            for key in WORKER_KEYS:
                os.environ.pop(key, None)
            try:
                runpy.run_path(str(script), run_name="__main__")
            except SystemExit:
                pass
        return calls

    def test_a_wait_reaches_the_session_shell_border_from_the_same_hook(self):
        # The border shows `waiting` when the wait is recorded and leaves it when
        # the waited tool ran; a turn's end or a tool without a wait sends nothing.
        claude = {"session_id": "sid", "hook_event_name": "PermissionRequest", "tool_name": "Bash"}
        self.assertEqual(self.shell_reports(CLAUDE, claude, "set", "--kind", "permission"),
                         [("claude", "blocked", "sid")])
        after = dict(claude, hook_event_name="PostToolUse")
        self.assertEqual(self.shell_reports(CLAUDE, after, "clear"), [("claude", "working", "sid")])
        self.assertIsNone(interaction.read_wait("sid", "claude"))
        self.assertEqual(self.shell_reports(CLAUDE, after, "clear"), [])
        self.shell_reports(CLAUDE, claude, "set", "--kind", "permission")
        self.assertEqual(self.shell_reports(CLAUDE, dict(claude, hook_event_name="Stop"), "clear"), [])
        asked = dict(claude, tool_name="AskUserQuestion")
        self.assertEqual(self.shell_reports(CLAUDE, asked, "set", "--kind", "permission"), [])
        codex = {"context": {"thread_id": "codex-sid"}, "tool_name": "exec"}
        self.assertEqual(self.shell_reports(CODEX_SET, codex), [("codex", "blocked", "codex-sid")])
        self.assertEqual(self.shell_reports(CODEX_CLEAR, codex), [("codex", "working", "codex-sid")])
        self.assertEqual(self.shell_reports(CODEX_CLEAR, codex), [])


if __name__ == "__main__":
    unittest.main()
