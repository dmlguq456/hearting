#!/usr/bin/env python3
"""codex_tui_exit: the Codex session's Fleet wait clears when its proven TUI exits."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
import codex_tui_exit  # noqa: E402
from fleet import interaction  # noqa: E402
from fleet.collectors import codex, procscan  # noqa: E402

SID = "01a1c50b-d424-7090-ba92-6338a4dc9975"


class CodexTuiExitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("FLEET_INTERACTION_STATE_DIR", None)
        self.assertTrue(interaction.set_wait(SID, "codex", "permission", "codex-appserver"))

    def test_wait_clears_when_the_proven_tui_process_ends(self):
        tui = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
        self.addCleanup(tui.wait)
        start = procscan.read_proc_start(tui.pid)
        with mock.patch.object(codex, "process_for_thread", return_value=(tui.pid, start)):
            self.assertEqual(codex_tui_exit.watch(SID), 0)
        self.assertIsNone(interaction.read_wait(SID, "codex"))

    def test_unproven_tui_leaves_the_wait_alone(self):
        with mock.patch.object(codex, "process_for_thread", return_value=None), \
                mock.patch.object(codex_tui_exit, "FIND_SECONDS", 0):
            self.assertEqual(codex_tui_exit.watch(SID), 0)
        self.assertIsNotNone(interaction.read_wait(SID, "codex"))

    def test_launch_refuses_unsafe_ids_and_tests(self):
        self.assertFalse(codex_tui_exit.launch("../x"))
        self.assertFalse(codex_tui_exit.launch(SID))  # never detaches from a test


class CodexInterruptBridgeTest(unittest.TestCase):
    def test_interrupt_clears_the_wait_and_stays_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp, "XDG_STATE_HOME": tmp,
                   "HEARTING_AUTO_RECORD": "off"}
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertTrue(interaction.set_wait(SID, "codex", "permission", "codex-appserver"))
            payload = {"session_id": SID, "cwd": tmp, "hook_event_name": "Interrupt",
                       "turn_id": "turn-1", "model": "m", "permission_mode": "default",
                       "transcript_path": None}
            done = subprocess.run(
                [sys.executable, str(ROOT / "adapters/codex/hooks/interrupt-lifecycle.py")],
                input=json.dumps(payload), env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual((done.returncode, done.stdout), (0, ""))
            self.assertFalse((Path(tmp) / "agent-fleet/interactions/codex" / f"{SID}.json").exists())


if __name__ == "__main__":
    unittest.main()
