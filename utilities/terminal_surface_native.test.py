#!/usr/bin/env python3
"""OpenCode native turn record contract; no terminal fixture is required."""
from __future__ import annotations
import sys
import unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parent.parent
sys.path[:0]=[str(ROOT / "utilities"),str(ROOT / "tools")]
import terminal_surface as ts

class OpenCodeNativeTurnTest(unittest.TestCase):
    def test_plugin_record_is_bound_to_exact_pane_sid_and_pid_birth(self):
        import pane_ownership
        import herdr_observation
        pid, sid, pane = 43210, "ses-native-turn", "%9@/tmp/tmux-native.sock"
        process = (pid, 100, 100, 0, 100, "123456")
        record = {"status": "blocked"}
        with mock.patch.object(pane_ownership, "_process", return_value=process), \
             mock.patch.object(herdr_observation, "read_native_status", return_value=record) as read:
            self.assertEqual(ts.native_turn("opencode", pid, sid, pane=pane,
                                            server="fixture-server"), "blocked")
        read.assert_called_once_with("fixture-server", "%9", "opencode", sid, pid, "123456")

    def test_unreadable_plugin_record_stays_unknown(self):
        import pane_ownership
        import herdr_observation
        pid, sid = 43211, "ses-native-unknown"
        process = (pid, 100, 100, 0, 100, "123457")
        with mock.patch.object(pane_ownership, "_process", return_value=process), \
             mock.patch.object(herdr_observation, "read_native_status", return_value=None):
            self.assertEqual(ts.native_turn("opencode", pid, sid, pane="%10", server="default"),
                             "unknown")


