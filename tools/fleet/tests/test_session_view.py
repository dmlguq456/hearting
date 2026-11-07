"""An exact attachment reads Fleet's native parsers, never neighboring sessions."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.fleet import session_view as view, titles
from tools.fleet.collectors import procscan, usage_cache


class SessionViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shell-view-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sid = "00000000-0000-4000-8000-000000000052"
        self.env = patch.dict(os.environ, {"HOME": str(self.root), "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                    "CODEX_HOME": str(self.root / "codex"), "XDG_STATE_HOME": str(self.root / "state"),
                    "FLEET_TITLE_STATE_DIR": str(self.root / "titles")})
        self.env.start(); self.addCleanup(self.env.stop)
        self.quota = patch.object(usage_cache, "read", return_value={"payload": {"rl_7d": 60}})
        self.quota.start(); self.addCleanup(self.quota.stop)
        self.start = procscan.read_proc_start(os.getpid())

    def write(self, path, *rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def test_claude_uses_native_transcript_and_keeps_unobserved_context_unknown(self):
        transcript = self.root / "claude/projects/test" / (self.sid + ".jsonl")
        self.write(transcript, {"type": "assistant", "sessionId": self.sid, "effort": "high",
                   "message": {"model": "claude-opus", "usage": {"input_tokens": 123}}})
        self.write(self.root / "claude/.statusline" / (self.sid + ".json"),
                   {"session_id": self.sid, "model": {"id": "unrelated-statusline-model"}, "context_window": {"used_percentage": 99}})
        self.write(Path(titles.sidecar_path(self.sid, "claude")), {"title": "Shared title"})
        values, watched = view.observe("claude", os.getpid(), self.start, self.sid)
        self.assertEqual(values["model"], "claude-opus")
        self.assertEqual(values["effort"], "high")
        self.assertEqual(values["title"], "Shared title")
        self.assertEqual(values["weekly_pct"], 60)
        self.assertIsNone(values["ctx_pct"])
        self.assertIn(str(transcript), watched)
        self.assertFalse(any(".statusline" in path for path in watched))

    def test_codex_selects_only_the_addressed_rollout_in_a_shared_directory(self):
        sessions = self.root / "codex/sessions"
        exact = sessions / ("rollout-2026-11-06-" + self.sid + ".jsonl")
        neighbor = sessions / "rollout-2026-11-06-00000000-0000-4000-8000-000000000073.jsonl"
        self.write(exact, {"type": "turn_context", "payload": {"model": "fixture-current", "effort": "high"}})
        self.write(neighbor, {"type": "turn_context", "payload": {"model": "fixture-neighbor", "effort": "low"}})
        values, watched = view.observe("codex", os.getpid(), self.start, self.sid)
        self.assertEqual((values["model"], values["effort"]), ("fixture-current", "high"))
        self.assertIn(str(exact), watched)
        self.assertNotIn(str(neighbor), watched)
        self.assertIsNone(values["ctx_pct"])

    def test_other_dynamic_quota_windows_never_take_the_seven_day_label(self):
        self.assertIsNone(view.weekly_quota({"rl_windows": [["24h", 80, 0]], "rl_7d": 30}))
        self.assertEqual(view.weekly_quota({"rl_windows": [["7d", 0, 0]], "rl_7d": 30}), 0)

    def test_changed_process_birth_yields_no_other_session_values(self):
        self.assertEqual(view.observe("claude", os.getpid(), self.start + "0", self.sid), ({}, []))


if __name__ == "__main__": unittest.main()
