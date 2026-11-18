#!/usr/bin/env python3
"""11-18 (F4): a session Claude's background daemon started takes no window hint from its environment.

Such a session carries the ``HERDR_PANE_ID`` the daemon inherited from whichever window first
started it, not one a window gave it. ``peer-steward start`` then does not locate a workspace from
that value, and ``native_agent`` does not report it as the session's window; a session a window
started keeps both, as before.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "tools")]
import peer_address  # noqa: E402
import peer_steward_native as native  # noqa: E402

spec = importlib.util.spec_from_file_location("peer_steward_hint_1118", HERE / "peer-steward.py")
steward = importlib.util.module_from_spec(spec)
sys.modules["peer_steward_hint_1118"] = steward
spec.loader.exec_module(steward)

PANE = "w18:pB"


class ServiceHint(unittest.TestCase):
    def setUp(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ["HERDR_PANE_ID"] = PANE
        self.cwd = tempfile.TemporaryDirectory(prefix="hint-")
        self.addCleanup(self.cwd.cleanup)
        self.agent = {"agent": {"pane_id": PANE, "workspace_id": "w18", "foreground_cwd": self.cwd.name}}

    def workspace(self, hosted):
        with mock.patch.object(peer_address, "_service_hosted", return_value=hosted), \
                mock.patch.object(steward, "_placement_result", return_value=self.agent) as asked, \
                mock.patch.object(steward.INSTALL_PATHS, "primary_checkout", side_effect=lambda path: Path(self.cwd.name)):
            return steward._project_start_workspace(), asked.called, steward._project_start_hint()

    def test_a_window_started_session_keeps_its_hint(self):
        self.assertEqual(self.workspace(False), ("w18", True, PANE))

    def test_a_daemon_hosted_session_uses_no_hint(self):
        self.assertEqual(self.workspace(True), (None, False, ""))

    def test_an_unreadable_process_tree_uses_no_hint(self):
        with mock.patch.object(peer_address, "_service_hosted", side_effect=OSError("no /proc")):
            self.assertEqual(steward._project_start_hint(), "")

    def row(self):
        return {"harness": "claude", "sid": "s1", "pid": 4242, "env": {"HERDR_PANE_ID": PANE}}

    def test_native_agent_window_value(self):
        for hosted, want in ((False, PANE), (True, None)):
            with self.subTest(hosted=hosted), \
                    mock.patch.object(peer_address, "_service_hosted", return_value=hosted):
                self.assertEqual(native._own_window_value(peer_address, self.row(), 4242), want)
        with mock.patch.object(peer_address, "_service_hosted", return_value=False):
            self.assertEqual(native._own_window_value(peer_address, {"env": {"TMUX_PANE": "%3"}}, 1), "%3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
