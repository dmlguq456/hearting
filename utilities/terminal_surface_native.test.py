#!/usr/bin/env python3
"""OpenCode native turn record contract; no terminal fixture is required."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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

class OpenCodeIdleWithoutHerdrTest(unittest.TestCase):
    """An OpenCode target outside Herdr still shows its work and its idle to a watch."""

    SID = "ses-idle-without-herdr"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opencode-native-turn-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("HERDR_", "AGENT_HERDR", "AGENT_PEER", "TMUX"))}
        env.update(HOME=self.tmp, XDG_STATE_HOME=self.tmp + "/state")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        import herdr_observation
        self.observation = herdr_observation
        self.pid = os.getpid()
        self.birth = herdr_observation._process_start_ticks(self.pid)

    def plugin_record(self, status, *, age=0.0, pane="%0"):
        """What `recordNativeTurnStatus` sends: the tmux pane id when there is no Herdr pane."""
        self.assertTrue(self.observation.record_native_status({
            "endpoint": "default", "pane": pane, "harness": "opencode", "session_id": self.SID,
            "pid": self.pid, "pid_start": self.birth, "status": status,
            "observed_at": time.time() - age}))

    def test_the_record_a_plugin_writes_without_herdr_is_the_one_the_watch_reads(self):
        address = "%0@/tmp/hbt-x/s"
        self.assertEqual(ts.native_turn("opencode", self.pid, self.SID, pane=address), "unknown")
        self.plugin_record("working")
        self.assertEqual(ts.native_turn("opencode", self.pid, self.SID, pane=address), "working")
        self.plugin_record("idle")
        self.assertEqual(ts.native_turn("opencode", self.pid, self.SID, pane=address), "idle")

    def test_an_idle_that_began_long_ago_stays_idle_for_the_watch_but_not_for_retire(self):
        address = "%0@/tmp/hbt-x/s"
        self.plugin_record("idle", age=600)
        self.assertEqual(ts.native_turn("opencode", self.pid, self.SID, pane=address), "unknown")
        self.assertEqual(ts.native_turn("opencode", self.pid, self.SID, pane=address, max_age=None), "idle")
        # Exactness still decides: another session, pane or process birth reads nothing.
        self.assertEqual(ts.native_turn("opencode", self.pid, "ses-other", pane=address, max_age=None), "unknown")
        self.assertEqual(ts.native_turn("opencode", self.pid, self.SID, pane="%1", max_age=None), "unknown")

    def test_the_native_agent_a_watch_reads_carries_that_idle(self):
        import peer_address
        import peer_steward_native as native
        import session_shell
        rows = [{"pid": self.pid, "harness": "opencode", "sid": self.SID, "env": {"TMUX_PANE": "%0"}}]
        with mock.patch.object(session_shell, "exact_binding", return_value=(None, "attachment-unavailable")), \
                mock.patch.object(peer_address, "native_sessions", return_value=rows):
            self.assertEqual(native.native_agent("opencode", self.SID)["result"]["agent"]["agent_status"],
                             "unknown")
            self.plugin_record("working", age=300)
            self.assertEqual(native.native_agent("opencode", self.SID)["result"]["agent"]["agent_status"],
                             "working")
            self.plugin_record("idle", age=120)
            self.assertEqual(native.native_agent("opencode", self.SID)["result"]["agent"]["agent_status"],
                             "idle")

    def test_the_plugin_records_under_the_pane_hint_outside_herdr(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        source = (ROOT / "adapters/opencode/plugins/hearting-guards.js").read_text(encoding="utf-8")
        start = source.index("function recordNativeTurnStatus(")
        body = source[start:source.index("\n}\n", start)]
        self.assertIn("const pane = paneHint()", body)
        self.assertNotIn("process.env.HERDR_PANE_ID", body)
        hint = source[source.index("function paneHint()"):]
        hint = hint[:hint.index("\n}\n") + 2]
        script = hint + 'process.stdout.write(JSON.stringify(paneHint()))'
        for env, expected in (({"TMUX": "/tmp/x/s,1,0", "TMUX_PANE": "%4"}, "%4"),
                              ({"TMUX_PANE": "%4"}, ""),
                              ({"HERDR_PANE_ID": "w1:p2", "TMUX": "/tmp/x/s,1,0", "TMUX_PANE": "%4"}, "w1:p2"),
                              ({}, "")):
            done = subprocess.run([node, "-e", script], env={"PATH": os.environ["PATH"], **env},
                                  capture_output=True, text=True, timeout=15)
            self.assertEqual(json.loads(done.stdout), expected, (env, done.stderr))


if __name__ == "__main__":
    unittest.main()
