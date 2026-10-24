#!/usr/bin/env python3
"""Regression tests for the silent, exact-cleanup-only Codex Stop bridge."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
STOP_PATH = ROOT / "adapters" / "codex" / "hooks" / "stop-lifecycle.py"
SESSIONEND_PATH = ROOT / "adapters" / "codex" / "hooks" / "sessionend-lifecycle.py"
HOOKS_PATH = ROOT / "adapters" / "codex" / "hooks" / "hooks.json"


def load_stop():
    spec = importlib.util.spec_from_file_location("codex_stop_lifecycle", STOP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class StopLifecycleTest(unittest.TestCase):
    def test_manifest_uses_short_dedicated_stop_bridge(self):
        config = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
        definition = config["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(definition["timeout"], 30)
        self.assertIn("stop-lifecycle.py", definition["command"])
        self.assertNotIn("sessionend-lifecycle.py", definition["command"])

    def test_stop_has_no_completion_or_continuation_authority(self):
        module = load_stop()
        self.assertEqual(module.main(), 0)
        source = STOP_PATH.read_text(encoding="utf-8")
        for retired in (
            "subprocess",
            "session-end",
            "join_session_batch",
            "decision",
            "parent_session_state",
        ):
            self.assertNotIn(retired, source)

    def test_stop_source_is_limited_to_exact_interaction_cleanup(self):
        source = STOP_PATH.read_text(encoding="utf-8")
        self.assertIn("sys.stdin", source)
        self.assertIn("os.environ", source)
        self.assertIn("interaction.clear_wait", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("dispatch-wait", source)
        self.assertNotIn("join_session_batch", source)

    def test_held_idle_observer_does_not_delay_stop_consumers(self):
        module = load_stop()
        sys.path.insert(0, str(ROOT / "utilities"))
        import session_tidy_idle
        from fleet import interaction
        import session_summary_trigger
        import artifact_checkpoint_trigger

        events = []

        class InputSink:
            def write(self, _data):
                return None

            def close(self):
                return None

        class HeldChild:
            stdin = InputSink()

        child = HeldChild()
        payload = json.dumps({"thread_id": "thread-held", "cwd": str(ROOT), "transcript_path": ""})
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"HOME": home,
                                             "XDG_STATE_HOME": str(Path(home) / "state")}, clear=True), \
                mock.patch("sys.stdin", io.StringIO(payload)), \
                mock.patch.object(session_tidy_idle.subprocess, "Popen", return_value=child), \
                mock.patch.object(interaction, "clear_wait", side_effect=lambda *_: events.append("clear_wait")), \
                mock.patch.object(session_summary_trigger, "launch_trigger",
                                  side_effect=lambda *_: events.append("summary")), \
                mock.patch.object(artifact_checkpoint_trigger, "launch_for_session",
                                  side_effect=lambda *_: events.append("checkpoint")):
            self.assertEqual(module.main(), 0)

        # The fake child remains held; Stop has already progressed through its consumers.
        self.assertEqual(events, ["clear_wait", "summary", "checkpoint"])

    def test_sessionend_has_no_stop_branch(self):
        source = SESSIONEND_PATH.read_text(encoding="utf-8")
        self.assertNotIn("session-end", source)
        self.assertNotIn("run_preflight", source)
        self.assertIn("clear_wait", source)
        self.assertNotIn("join_session_batch", source)
        self.assertNotIn('event == "stop"', source)


if __name__ == "__main__":
    unittest.main()
