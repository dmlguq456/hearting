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


if __name__ == "__main__":
    unittest.main()
