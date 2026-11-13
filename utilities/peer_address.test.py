#!/usr/bin/env python3
"""peer_address resolves peer addresses from Hearting records only, never a multiplexer."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "tools"))
import peer_address  # noqa: E402


def _row(harness, sid, pane="", *, server=None, socket=None, tmux_pane=None, tmux=None,
         config=None):
    # A native process of this user carries its own HOME and configuration folders.
    env = {key: os.environ[key] for key in ("HOME", "XDG_CONFIG_HOME") if os.environ.get(key)}
    if config:
        env["XDG_CONFIG_HOME"] = config
    if pane:
        env["HERDR_PANE_ID"] = pane
    if server:
        env["HERDR_SESSION"] = server
    if socket:
        env["HERDR_SOCKET_PATH"] = socket
    if tmux_pane:
        env["TMUX_PANE"] = tmux_pane
    if tmux:
        env["TMUX"] = tmux + ",1,0"
    return {"harness": harness, "sid": sid, "pid": 0, "env": env, "binding": None}


class PeerAddressTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in ("AGENT_HERDR_SESSION", "HERDR_SESSION", "HERDR_SOCKET_PATH", "TMUX"):
            os.environ.pop(key, None)
        self.sessions = []
        patch = mock.patch.object(peer_address, "native_sessions", side_effect=lambda: list(self.sessions))
        patch.start()
        self.addCleanup(patch.stop)
        names = {}
        self.names = names
        name_patch = mock.patch.object(peer_address, "_registry_name",
                                       side_effect=lambda harness, sid: names.get((harness, sid)))
        name_patch.start()
        self.addCleanup(name_patch.stop)
        # Resolution never runs a terminal multiplexer or any other program.
        run = mock.patch.object(subprocess, "run", side_effect=AssertionError("no subprocess"))
        popen = mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess"))
        run.start(); popen.start()
        self.addCleanup(run.stop); self.addCleanup(popen.stop)

    def tags(self, rows):
        path = self.root / "state" / "agent-fleet" / "tag-assignments.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "assignments": [
            {"harness": h, "session_id": s, "tag": t, "first_seen": 1.0, "started_at": 1.0}
            for h, s, t in rows]}))

    def test_full_native_address_is_itself(self):
        self.assertEqual(peer_address.resolve("codex:thread-1"),
                         ({"harness": "codex", "session_id": "thread-1"}, None))
        self.assertEqual(peer_address.resolve("claude:-"), (None, "target-not-found"))

    def test_fleet_number_uses_the_confirmed_assignment(self):
        self.tags([("claude", "sid-a", "e9"), ("codex", "thread-b", "e7")])
        self.assertEqual(peer_address.resolve("[e7]")[0], {"harness": "codex", "session_id": "thread-b"})
        self.assertEqual(peer_address.resolve("e9")[0], {"harness": "claude", "session_id": "sid-a"})
        self.assertEqual(peer_address.resolve("[52]"), (None, "fleet-tag-not-found"))

    def test_registry_name_must_be_unique_among_live_sessions(self):
        self.sessions = [_row("claude", "sid-a"), _row("codex", "thread-b"), _row("opencode", "")]
        self.names.update({("claude", "sid-a"): "hearting-46", ("codex", "thread-b"): "reviewer"})
        self.assertEqual(peer_address.resolve("reviewer")[0], {"harness": "codex", "session_id": "thread-b"})
        self.assertEqual(peer_address.resolve("nobody"), (None, "target-name-not-found"))
        self.names[("claude", "sid-a")] = "reviewer"
        self.assertEqual(peer_address.resolve("reviewer"), (None, "target-name-ambiguous"))

    def test_window_address_uses_the_sessions_own_pane_value_on_the_same_server_only(self):
        self.sessions = [_row("claude", "sid-default", "wB:pB3"),
                         _row("codex", "thread-other", "wB:pB3", server="other")]
        self.assertEqual(peer_address.resolve("wB:pB3")[0],
                         {"harness": "claude", "session_id": "sid-default", "pane": "wB:pB3"})
        # The recorded server of a retained message selects the other server's window.
        self.assertEqual(peer_address.resolve("wB:pB3", server="other")[0]["session_id"], "thread-other")
        # The same number on a server nobody names is never borrowed.
        self.assertEqual(peer_address.resolve("wB:pB3", server="third"), (None, "target-pane-unresolved"))
        self.assertEqual(peer_address.resolve("wB:pB9"), (None, "target-pane-unresolved"))

    def test_window_address_socket_mismatch_ambiguity_and_missing_session_are_refused(self):
        os.environ["HERDR_SOCKET_PATH"] = str(self.root / "mine.sock")
        self.sessions = [_row("claude", "sid-a", "w1:p1", socket=str(self.root / "theirs.sock"))]
        self.assertEqual(peer_address.resolve("w1:p1"), (None, "target-pane-unresolved"))
        mine = str(self.root / "mine.sock")
        self.sessions = [_row("claude", "sid-a", "w1:p1", socket=mine),
                         _row("claude", "sid-b", "w1:p1", socket=mine)]
        self.assertEqual(peer_address.resolve("w1:p1"), (None, "target-pane-ambiguous"))
        self.sessions = [_row("opencode", "", "w1:p1", socket=mine)]
        self.assertEqual(peer_address.resolve("w1:p1"), (None, "target-session-unresolved"))

    def test_unknown_socket_never_borrows_another_namespace_with_the_same_server_name(self):
        # Two Herdr servers named alike, each under its own configuration folder.
        a, b = str(self.root / "a"), str(self.root / "b")
        sock_a = os.path.join(a, "herdr", "sessions", "hb", "herdr.sock")
        sock_b = os.path.join(b, "herdr", "sessions", "hb", "herdr.sock")
        native_b = _row("claude", "sid-b", "w1:p1", server="hb", socket=sock_b, config=b)
        os.environ.update(AGENT_HERDR_SESSION="hb", XDG_CONFIG_HOME=a)
        self.sessions = [native_b]
        # A's caller without a socket value is on A's namespace: B's same window is refused.
        self.assertEqual(peer_address.resolve("w1:p1"), (None, "target-pane-unresolved"))
        os.environ["HERDR_SOCKET_PATH"] = sock_a
        self.assertEqual(peer_address.resolve("w1:p1"), (None, "target-pane-unresolved"))
        os.environ.pop("HERDR_SOCKET_PATH")
        # A's own native in the same window number still resolves without a socket value.
        self.sessions = [native_b, _row("codex", "thread-a", "w1:p1", server="hb", socket=sock_a, config=a)]
        self.assertEqual(peer_address.resolve("w1:p1")[0]["session_id"], "thread-a")
        # A retained message keeps the namespace it was first read on, whoever re-reads it.
        os.environ["XDG_CONFIG_HOME"] = b
        self.assertEqual(peer_address.resolve("w1:p1")[0]["session_id"], "sid-b")
        self.assertEqual(peer_address.resolve("w1:p1", server="hb", namespace=sock_a)[0]["session_id"],
                         "thread-a")
        self.sessions = [native_b]
        self.assertEqual(peer_address.resolve("w1:p1", server="hb", namespace=sock_a),
                         (None, "target-pane-unresolved"))

    def test_tmux_window_address_matches_its_own_server_socket(self):
        mine, other = str(self.root / "tmux-mine"), str(self.root / "tmux-other")
        self.sessions = [_row("claude", "sid-mine", tmux_pane="%0", tmux=mine),
                         _row("claude", "sid-other", tmux_pane="%0", tmux=other)]
        self.assertEqual(peer_address.resolve("%0@" + other)[0]["session_id"], "sid-other")
        os.environ["TMUX"] = mine + ",1,0"
        self.assertEqual(peer_address.resolve("%0")[0]["session_id"], "sid-mine")

    def test_presence_reads_shell_attachments_and_live_sessions(self):
        import session_shell
        self.sessions = [_row("claude", "sid-live")]
        with mock.patch.object(session_shell, "exact_binding",
                               side_effect=lambda h, s: ({"sid": s}, "") if s == "sid-shell" else (None, "attachment-unavailable")):
            self.assertEqual(peer_address.presence("opencode", "sid-shell"), "present")
            self.assertEqual(peer_address.presence("claude", "sid-live"), "present")
            self.assertEqual(peer_address.presence("claude", "sid-gone"), "absent")
            self.assertEqual(peer_address.presence("claude", "not a sid"), "unknown")
        with mock.patch.object(session_shell, "exact_binding", return_value=(None, "attachment-ambiguous")):
            self.assertEqual(peer_address.presence("claude", "sid-live"), "unknown")


if __name__ == "__main__":
    unittest.main()
