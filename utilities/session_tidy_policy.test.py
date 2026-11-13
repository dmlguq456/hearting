from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
from pathlib import Path
import tempfile
import unittest

import session_tidy_policy as policy


class SessionTidyPolicyTests(unittest.TestCase):
    def test_default_context_floor_matches_the_500k_recommendation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "absent-config.json"
            for harness in ("claude", "codex", "opencode"):
                selected, reason = policy.idle_policy(harness, path=path)
                self.assertEqual(reason, "")
                self.assertEqual(selected["min_tokens"], 500000)
            self.assertFalse(path.exists())

    def test_default_idle_wait_is_six_hours_for_every_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "absent-config.json"
            for harness in ("claude", "codex", "opencode"):
                selected, reason = policy.idle_policy(harness, path=path)
                self.assertEqual(reason, "")
                self.assertEqual(selected["wait_seconds"], 21600)
                self.assertEqual(selected["cache_margin_seconds"], 60)
            self.assertFalse(path.exists())

    def test_absent_uses_defaults_without_creating_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hearting" / "session-tidy.json"
            value, reason = policy.idle_policy("claude", path=path)
            self.assertEqual(reason, "")
            self.assertEqual(value, policy.DEFAULT_IDLE)
            self.assertFalse(path.exists())

    def test_harness_provider_override_only_changes_selected_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session-tidy.json"
            path.write_text(json.dumps({"idle": {"wait_seconds": 100}, "overrides": {
                "claude": {"anthropic": {"wait_seconds": 200, "min_tokens": 300000}}}}),
                encoding="utf-8")
            selected, reason = policy.idle_policy("claude", "anthropic", path=path)
            other, other_reason = policy.idle_policy("claude", "bedrock", path=path)
            self.assertEqual(reason, other_reason, "")
            self.assertEqual((selected["wait_seconds"], selected["min_tokens"]), (200, 300000))
            self.assertEqual((other["wait_seconds"], other["min_tokens"]), (100, 500000))

    def test_disabled_only_disables_idle_policy(self):
        parsed = policy.validate_document({"idle": {"enabled": False}})
        self.assertFalse(parsed["idle"]["enabled"])
        self.assertEqual(parsed["idle"]["min_tokens"], 500000)

    def test_rejects_bool_nan_bad_structures_and_nonpositive_wait(self):
        documents = (
            {"idle": {"min_tokens": True}},
            {"idle": {"wait_seconds": float("nan")}},
            {"idle": {"wait_seconds": 0}},
            {"overrides": {"claude": []}},
            {"overrides": {"claude": {"anthropic": {"enabled": 1}}}},
        )
        for document in documents:
            with self.subTest(document=document), self.assertRaises(policy.PolicyError):
                policy.validate_document(document)

    def test_invalid_file_safely_withholds_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session-tidy.json"
            path.write_text("{bad", encoding="utf-8")
            value, reason = policy.idle_policy("codex", path=path)
            self.assertIsNone(value)
            self.assertTrue(reason)

    def test_user_config_validation_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session-tidy.json"
            before = policy.validate_user_config(path)
            self.assertEqual(before["status"], "absent")
            self.assertFalse(path.exists())
            path.write_text(json.dumps({"idle": {"enabled": False}}), encoding="utf-8")
            content, mtime = path.read_bytes(), path.stat().st_mtime_ns
            result = policy.validate_user_config(path)
            self.assertEqual(result["status"], "valid")
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (content, mtime))


if __name__ == "__main__":
    unittest.main()
