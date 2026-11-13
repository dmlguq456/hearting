"""An exact attachment reads Fleet's native parsers, never neighboring sessions."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
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
        self.git_patch = patch.object(view.gitinfo, "cached_display", return_value=None)
        self.git = self.git_patch.start(); self.addCleanup(self.git_patch.stop)
        self.start = procscan.read_proc_start(os.getpid())

    def write(self, path, *rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def test_claude_prefers_the_exact_native_callback_over_transcript_telemetry(self):
        transcript = self.root / "claude/projects/test" / (self.sid + ".jsonl")
        self.write(transcript, {"type": "assistant", "sessionId": self.sid, "effort": "high",
                   "message": {"model": "claude-opus", "usage": {"input_tokens": 123}}})
        self.write(self.root / "claude/.statusline" / (self.sid + ".json"),
                   {"session_id": self.sid, "pid": os.getpid(), "proc_start": self.start,
                    "model": {"id": "native-current-model"}, "effort": {"level": "xhigh"},
                    "context_window": {"used_percentage": 37}})
        self.write(Path(titles.sidecar_path(self.sid, "claude")), {"title": "Shared title"})
        values, watched = view.observe("claude", os.getpid(), self.start, self.sid)
        self.assertEqual(values["model"], "native-current-model")
        self.assertEqual(values["effort"], "xhigh")
        self.assertEqual(values["title"], "Shared title")
        self.assertEqual(values["weekly_pct"], 60)
        self.assertEqual(values["ctx_pct"], 37)
        self.assertIn(str(transcript), watched)
        self.assertIn(str(self.root / "claude/.statusline" / (self.sid + ".json")), watched)

    def test_native_context_zero_and_exact_callback_window_are_not_unknown(self):
        path = self.root / "claude/.statusline" / (self.sid + ".json")
        for window, expected in (({"used_percentage": 0}, 0),
                ({"context_window_size": 100000, "current_usage": {
                    "input_tokens": 10000, "cache_creation_input_tokens": 5000,
                    "cache_read_input_tokens": 2000}}, 17)):
            with self.subTest(window=window):
                self.write(path, {"session_id": self.sid, "model": {"id": "native"},
                                  "context_window": window})
                values, _ = view.observe("claude", os.getpid(), self.start, self.sid)
                self.assertEqual(values["ctx_pct"], expected)

    def test_another_sid_or_process_birth_cannot_supply_native_display_values(self):
        path = self.root / "claude/.statusline" / (self.sid + ".json")
        for extra in ({"session_id": "another-session"},
                      {"pid": os.getpid(), "proc_start": self.start + "0"}):
            with self.subTest(extra=extra):
                self.write(path, {"session_id": self.sid, "model": {"id": "wrong"},
                                  "context_window": {"used_percentage": 99}, **extra})
                values, _ = view.observe("claude", os.getpid(), self.start, self.sid)
                self.assertIsNone(values["model"])
                self.assertIsNone(values["ctx_pct"])

    def test_observed_non_git_is_distinct_from_unavailable_git(self):
        with patch.object(view.gitinfo, "branch", return_value=None), patch.object(view.gitinfo, "resolve_gitdir", return_value=(None, None)):
            self.git.return_value = {"git": {"repo": 0}}
            values, _ = view.observe("claude", os.getpid(), self.start, self.sid)
            self.assertEqual(values["branch"], "no-git")
            self.git.return_value = {"git": {"repo": 1, "branch": "observed-branch"}}
            values, _ = view.observe("claude", os.getpid(), self.start, self.sid)
            self.assertEqual(values["branch"], "observed-branch")
            self.git.return_value = {"git": {"repo": None}}
            values, _ = view.observe("claude", os.getpid(), self.start, self.sid)
            self.assertIsNone(values["branch"])

    def test_unavailable_cwd_does_not_borrow_the_backend_git_observation(self):
        readlink = os.readlink
        def observed(path, *args, **kwargs):
            if str(path) == f"/proc/{os.getpid()}/cwd":
                raise PermissionError("synthetic unavailable cwd")
            return readlink(path, *args, **kwargs)
        self.git.return_value = {"git": {"repo": 0}}
        with patch.object(view.os, "readlink", side_effect=observed):
            values, _ = view.observe("claude", os.getpid(), self.start, self.sid)
        self.assertIsNone(values["branch"])
        self.git.assert_not_called()

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

    def test_codex_native_usage_is_not_hidden_by_an_unknown_compaction_policy(self):
        exact = self.root / "codex/sessions" / ("rollout-2026-11-07-" + self.sid + ".jsonl")
        self.write(exact, {"type": "turn_context", "payload": {"model": "fixture-current", "effort": "high"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"total_tokens": 82000}, "model_context_window": 112000}}})
        values, watched = view.observe("codex", os.getpid(), self.start, self.sid)
        self.assertEqual(values["ctx_pct"], 70)
        self.assertIn(str(exact), watched)

    def test_opencode_uses_observed_native_capacity_without_guessing_one(self):
        def enrich(sess, **_options):
            sess.model = "native-model"
            sess.active_context_tokens = 20000
            sess.context_window_tokens = 200000
        with patch.object(view.opencode, "enrich", side_effect=enrich):
            values, _ = view.observe("opencode", os.getpid(), self.start, self.sid)
            self.assertEqual((values["model"], values["ctx_pct"]), ("native-model", 10))
        with patch.object(view.opencode, "enrich"):
            values, _ = view.observe("opencode", os.getpid(), self.start, self.sid)
            self.assertIsNone(values["ctx_pct"])

    def test_other_dynamic_quota_windows_never_take_the_seven_day_label(self):
        self.assertIsNone(view.weekly_quota({"rl_windows": [["24h", 80, 0]], "rl_7d": 30}))
        self.assertEqual(view.weekly_quota({"rl_windows": [["7d", 0, 0]], "rl_7d": 30}), 0)

    def test_exact_native_rate_windows_keep_resets_and_model_buckets(self):
        path=self.root / "claude/.statusline" / (self.sid + ".json")
        self.write(path, {"session_id":self.sid,"rate_limits":{
            "five_hour":{"used_percentage":31,"resets_at":1800000000},
            "seven_day":{"used_percentage":62,"resets_at":1800200000},
            "seven_day_opus":{"used_percentage":57},
            "model_scoped":[{"display_name":"Fable 5","utilization":.42}]}})
        values,_=view.observe("claude",os.getpid(),self.start,self.sid)
        self.assertEqual(values["quota_windows"],[("5h",31,1800000000),("7d",62,1800200000)])
        self.assertEqual(values["model_quotas"],[("opus",57),("fable",42)])
        self.assertEqual(values["weekly_pct"],62)

    def test_changed_process_birth_yields_no_other_session_values(self):
        self.assertEqual(view.observe("claude", os.getpid(), self.start + "0", self.sid), ({}, []))


if __name__ == "__main__": unittest.main()
