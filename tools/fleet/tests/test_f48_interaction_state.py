"""F-48 neutral interaction sidecar: privacy, ownership, freshness, reuse."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import inspect
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import interaction  # noqa: E402


class InteractionStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ, {"FLEET_INTERACTION_STATE_DIR": self.tmp.name}, clear=False
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_round_trip_permissions_and_clear(self):
        self.assertTrue(
            interaction.set_wait(
                "sid-a", "claude", "decision", "claude-asktool", now=100.0
            )
        )
        record = interaction.read_wait("sid-a", "claude")
        self.assertEqual(set(record), interaction._BASE_KEYS)
        self.assertEqual(record["kind"], "decision")
        path = interaction.sidecar_path("sid-a", "claude")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        self.assertTrue(interaction.clear_wait("sid-a", "claude"))
        self.assertIsNone(interaction.read_wait("sid-a", "claude"))
        self.assertTrue(interaction.clear_wait("sid-a", "claude"))

    def test_opencode_question_uses_the_exact_source_and_session(self):
        self.assertIn("opencode-question", interaction.WAIT_SOURCES)
        self.assertTrue(interaction.set_wait("oc-sid", "opencode", "decision",
                                             "opencode-question", now=200.0))
        record = interaction.read_wait("oc-sid", "opencode")
        self.assertEqual((record["harness"], record["session_id"], record["source"]),
                         ("opencode", "oc-sid", "opencode-question"))
        self.assertTrue(interaction.clear_wait("oc-sid", "opencode"))

    def test_optional_native_refs_are_allowlisted_and_backward_compatible(self):
        self.assertTrue(interaction.set_wait(
            "sid-native", "codex", "decision", "codex-appserver", now=201.0,
            request_id="42", turn_id="turn_7", item_id="item_9"))
        record = interaction.read_wait("sid-native", "codex")
        self.assertEqual((record["request_id"], record["turn_id"], record["item_id"]),
                         ("42", "turn_7", "item_9"))
        self.assertNotIn("question", record)
        self.assertNotIn("choices", record)
        self.assertTrue(interaction.set_wait(
            "sid-native", "codex", "decision", "codex-appserver", now=202.0))
        self.assertEqual(set(interaction.read_wait("sid-native", "codex")),
                         interaction._BASE_KEYS)
        self.assertFalse(interaction.set_wait(
            "sid-native", "codex", "decision", "codex-appserver", now=203.0,
            request_id="../foreign"))

    def test_schema_has_no_content_channel(self):
        signature = inspect.signature(interaction.set_wait)
        self.assertFalse(
            any(
                item.kind in (item.VAR_KEYWORD, item.VAR_POSITIONAL)
                for item in signature.parameters.values()
            )
        )
        question = "실제 질문 본문과 rm -rf /"
        self.assertFalse(
            interaction.set_wait("sid", "claude", question, "claude-asktool", now=1)
        )
        self.assertFalse(
            interaction.set_wait("sid", "claude", "decision", question, now=1)
        )
        self.assertFalse(os.path.exists(interaction.sidecar_path("sid", "claude")))
        good = interaction._encode("claude", "sid", "decision", "claude-asktool", 1)
        with self.assertRaises(ValueError):
            interaction._atomic_write(
                interaction.sidecar_path("sid", "claude"), dict(good, prompt=question)
            )

    def test_traversal_foreign_owner_and_malformed_are_silent(self):
        for value in ("../sid", "a/b", "", None):
            self.assertFalse(
                interaction.set_wait(value, "claude", "decision", "claude-asktool", now=1)
            )
        self.assertTrue(
            interaction.set_wait("sid", "claude", "decision", "claude-asktool", now=2)
        )
        with mock.patch.object(interaction.os, "getuid", return_value=os.getuid() + 1):
            self.assertIsNone(interaction.read_wait("sid", "claude"))
        path = interaction.sidecar_path("sid", "claude")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        self.assertIsNone(interaction.read_wait("sid", "claude"))

    def test_world_readable_and_symlink_directory_are_rejected(self):
        self.assertTrue(
            interaction.set_wait("sid", "claude", "decision", "claude-asktool", now=2)
        )
        path = interaction.sidecar_path("sid", "claude")
        os.chmod(path, 0o644)
        self.assertIsNone(interaction.read_wait("sid", "claude"))

        linked_root = os.path.join(self.tmp.name, "linked")
        os.makedirs(linked_root)
        os.symlink(linked_root, interaction.interactions_dir("codex"))
        self.assertFalse(
            interaction.set_wait(
                "sid", "codex", "approval", "codex-permissionrequest", now=3
            )
        )
        self.assertFalse(os.path.exists(os.path.join(linked_root, "sid.json")))

    def test_session_reuse_activity_future_and_extra_keys_invalidate(self):
        self.assertTrue(
            interaction.set_wait("sid", "codex", "approval", "codex-permissionrequest", now=100)
        )
        self.assertIsNotNone(interaction.pending_wait("sid", "codex", now=110))
        self.assertIsNone(
            interaction.pending_wait("sid", "codex", session_start=101, now=110)
        )
        self.assertIsNone(
            interaction.pending_wait("sid", "codex", activity_since=101, now=110)
        )
        self.assertIsNone(interaction.pending_wait("sid", "codex", now=1))
        path = interaction.sidecar_path("sid", "codex")
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
        record["prompt"] = "secret"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        self.assertIsNone(interaction.read_wait("sid", "codex"))

    def test_sweep_is_cleanup_only(self):
        self.assertTrue(
            interaction.set_wait("sid", "claude", "decision", "claude-asktool", now=10)
        )
        path = interaction.sidecar_path("sid", "claude")
        os.utime(path, (10, 10))
        self.assertIsNotNone(interaction.pending_wait("sid", "claude", now=1000))
        directory = os.path.dirname(path)
        malformed = os.path.join(directory, "malformed.json")
        with open(malformed, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        os.utime(malformed, (10, 10))
        temporary = os.path.join(directory, "abandoned.tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write("partial")
        os.utime(temporary, (10, 10))
        with tempfile.TemporaryDirectory() as external:
            target = os.path.join(external, "keep.json")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("{}")
            os.utime(target, (10, 10))
            linked = os.path.join(directory, "linked.json")
            os.symlink(target, linked)

            self.assertEqual(interaction.sweep(now=1000, max_age=100), 2)
            self.assertIsNotNone(interaction.read_wait("sid", "claude"))
            self.assertFalse(os.path.exists(malformed))
            self.assertFalse(os.path.exists(temporary))
            self.assertTrue(os.path.islink(linked))
            self.assertTrue(os.path.exists(target))

        self.assertIsNone(interaction.pending_wait(
            "sid", "claude", activity_since=11, now=1000))
        self.assertTrue(interaction.clear_wait("sid", "claude"))
        self.assertIsNone(interaction.read_wait("sid", "claude"))

    def test_sweep_does_not_follow_harness_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as external:
            target = os.path.join(external, "keep.json")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("{}")
            os.utime(target, (1, 1))
            os.symlink(external, interaction.interactions_dir("claude"))
            self.assertEqual(interaction.sweep(now=1000, max_age=10), 0)
            self.assertTrue(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
