#!/usr/bin/env python3
"""OpenCode native turn record contract; no terminal fixture is required."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
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

class OpenCodeIdleWithoutHerdrTest(unittest.TestCase):
    """The watch reads an OpenCode target outside Herdr through the one turn record `native_turn` reads."""

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

    def test_the_native_agent_a_watch_reads_carries_that_idle(self):
        """A watch reads OpenCode's own turn record (the one `native_turn` reads), idle or not."""
        import peer_address
        import peer_steward_native as native
        import session_shell
        rows = [{"pid": self.pid, "harness": "opencode", "sid": self.SID, "env": {"TMUX_PANE": "%0"}}]
        with mock.patch.object(session_shell, "exact_binding", return_value=(None, "attachment-unavailable")), \
                mock.patch.object(peer_address, "native_sessions", return_value=rows):
            for turn in ("unknown", "working", "idle"):
                with mock.patch.object(ts, "native_turn", return_value=turn):
                    self.assertEqual(native.native_agent("opencode", self.SID)["result"]["agent"]["agent_status"],
                                     turn)

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
