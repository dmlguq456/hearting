"""F-49 exact Codex App Server interaction semantics."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from fleet import interaction
from fleet.collectors import interaction as collector


class F49InteractionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ, {"FLEET_INTERACTION_STATE_DIR": self.tmp.name}, clear=False
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_exact_source_survives_activity_and_beats_later_rollout(self):
        self.assertTrue(interaction.set_wait(
            "thread-1", "codex", "decision", "codex-appserver", now=100
        ))
        self.assertIsNotNone(interaction.pending_wait(
            "thread-1", "codex", activity_since=200, now=210
        ))
        sess = SimpleNamespace(
            session_id="thread-1", harness="codex", started_at=1,
            _interaction_activity=200, interaction_state={
                "kind": "decision", "source": "codex-rollout", "waiting_since": 300,
            },
        )
        collector.enrich(sess, now=210)
        self.assertEqual(sess.interaction_state["source"], "codex-appserver")

    def test_exact_source_keeps_session_and_future_clock_guards(self):
        self.assertTrue(interaction.set_wait(
            "thread-1", "codex", "decision", "codex-appserver", now=100
        ))
        self.assertIsNone(interaction.pending_wait(
            "thread-1", "codex", session_start=101, now=110
        ))
        self.assertIsNone(interaction.pending_wait(
            "thread-1", "codex", now=1
        ))

    def test_unmanaged_source_is_rejected_without_replacing_exact(self):
        self.assertTrue(interaction.set_wait(
            "thread-1", "codex", "decision", "codex-appserver", now=100
        ))
        self.assertFalse(interaction.set_wait(
            "thread-1", "codex", "decision", "codex-unmanaged", now=101
        ))
        self.assertEqual(interaction.read_wait("thread-1", "codex")["source"], "codex-appserver")

    def test_legacy_sources_still_invalidate_on_later_activity(self):
        self.assertTrue(interaction.set_wait(
            "thread-1", "codex", "decision", "codex-rollout", now=100
        ))
        self.assertIsNone(interaction.pending_wait(
            "thread-1", "codex", activity_since=101, now=110
        ))

    def test_absent_exact_marker_preserves_rollout_fallback(self):
        sess = SimpleNamespace(
            session_id="thread-1", harness="codex", started_at=1,
            _interaction_activity=100, interaction_state={
                "kind": "decision", "source": "codex-rollout", "waiting_since": 100,
            },
        )
        collector.enrich(sess, now=110)
        self.assertEqual(sess.interaction_state["source"], "codex-rollout")


if __name__ == "__main__":
    unittest.main()
