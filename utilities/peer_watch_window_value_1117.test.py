#!/usr/bin/env python3
"""A supervisor can watch a live session however its window was opened (2026-11-17).

The session's window value is the ``HERDR_PANE_ID``/``TMUX_PANE`` its own native
process received from its window. A session the Hearting session shell attaches was
read without its Herdr value, so ``watch harness:sid`` (and a Fleet number or a name)
refused it as ``watch-identity-unverified`` and no supervision was recorded. A session a
native service process hosts (Claude's background daemon) carries the value that
service inherited from whichever window started it; it never makes a window address
ambiguous. Real child processes, a private Hearting state root, no Herdr, no tmux.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import select
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
import pane_ownership  # noqa: E402
import peer_address  # noqa: E402
import peer_steward_native  # noqa: E402
import session_shell  # noqa: E402
import terminal_surface  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("peer_watch_window_value", ROOT / "utilities/peer-steward.py")
peer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(peer)

_WORKER_MARKERS = ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                   "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "MEM_DISTILL")
_SERVER = "t1117"
_PANE = "wT:pT1"
_SID = "01000000-0000-7000-8000-000000001117"
_HOLD = "import os,sys;print(os.getpid(),flush=True);sys.stdin.buffer.read()"
# A stand-in for Claude's background daemon: its title marks it a service; the session it
# hosts is an ordinary `claude` process that inherited the daemon's environment.
_DAEMON = """\
import subprocess
import sys

with subprocess.Popen(["claude", "-c", sys.argv[2]], executable=sys.argv[1],
                      stdin=subprocess.PIPE, stdout=subprocess.PIPE) as child:
    sys.stdout.buffer.write(child.stdout.readline())
    sys.stdout.buffer.flush()
    sys.stdin.buffer.read()
    child.stdin.close()
"""


class WindowValueTest(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory(prefix="pw1117-", dir="/tmp")
        self.addCleanup(home.cleanup)
        self.home = home.name
        self.socket = os.path.join(self.home, "herdr.sock")
        self.addCleanup(mock.patch.stopall)
        values = {"HOME": self.home, "XDG_STATE_HOME": self.home + "/state",
                  "XDG_CONFIG_HOME": self.home + "/config",
                  "CLAUDE_CONFIG_DIR": self.home + "/claude",
                  "AGENT_PEER_LEDGER_ROOT": self.home + "/peers",
                  "HERDR_SESSION": _SERVER, "HERDR_SOCKET_PATH": self.socket}
        mock.patch.dict(os.environ, values).start()
        for name in ("AGENT_HERDR_SESSION", "HERDR_PANE_ID", "TMUX", "TMUX_PANE", *_WORKER_MARKERS):
            os.environ.pop(name, None)
        self.children = []
        self.addCleanup(self.close_children)
        # Only the fixture's children are candidates.
        real_listdir = os.listdir
        mock.patch("os.listdir", side_effect=lambda path=".": [str(pid) for pid in self.pids()]
                   if str(path) == "/proc" else real_listdir(path)).start()
        # Keep the real daemon-to-session edge and stop the walk at the fixture root.
        read_process = pane_ownership._process
        test_pid = os.getpid()

        def fixture_process(pid):
            row = read_process(pid)
            if row and row[1] == test_pid:
                return (row[0], 1, *row[2:])
            return row

        mock.patch.object(pane_ownership, "_process", side_effect=fixture_process).start()
        mock.patch.object(terminal_surface, "native_turn", return_value="idle").start()

    def pids(self):
        found = []
        for child in self.children:
            found.extend(getattr(child, "family", [child.pid]))
        return found

    def spawn(self, argv, env):
        child = subprocess.Popen(argv, executable=sys.executable, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, env=env)
        self.children.append(child)
        # Popen can return while procfs still has no argv or environment for the new image.
        self.assertTrue(select.select([child.stdout], [], [], 10)[0], "fixture child did not start")
        ready_pid = child.stdout.readline().strip()
        self.assertTrue(ready_pid.isdigit(), "fixture child exited before announcing its PID")
        return child, int(ready_pid)

    def close_children(self):
        while self.children:
            child = self.children.pop()
            with contextlib.suppress(OSError):
                child.stdin.close()
            with child.stdout:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                    raise

    @contextlib.contextmanager
    def case(self, **params):
        """One subtest whose processes end with it, passed or not."""
        with self.subTest(**params):
            try:
                yield
            finally:
                self.close_children()
                root = peer._watch_root()
                # Only this test's private ledger root is ever emptied.
                if root.is_dir() and root.resolve().is_relative_to(Path(self.home).resolve()):
                    for path in root.glob("*"):
                        if path.is_file():
                            path.unlink()

    def window_env(self, **window):
        env = {key: value for key, value in os.environ.items() if key not in _WORKER_MARKERS}
        env.update(window)
        return env

    def attached_session(self, harness, **window):
        """A shell-attached native session: real host and native children, a valid record."""
        host, _ = self.spawn([sys.executable, "-c", _HOLD], self.window_env())
        native, _ = self.spawn([sys.executable, "-c", _HOLD], self.window_env(**window))
        broker = session_shell.process(os.getpid())
        host_row, native_row = session_shell.process(host.pid), session_shell.process(native.pid)
        value = {"schema": session_shell.SCHEMA, "harness": harness, "sid": _SID,
                 "incarnation": "b" * 32, "socket": self.home + "/s.sock",
                 "host_pid": host.pid, "host_start": host_row["start"],
                 "native_pid": native.pid, "native_start": native_row["start"],
                 "broker_pid": broker["pid"], "broker_start": broker["start"]}
        path = session_shell.binding_path(harness, host.pid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        self.addCleanup(path.unlink, missing_ok=True)
        return native

    def daemon_hosted_session(self, **window):
        daemon, hosted = self.spawn(["claude bg-pty-host", "-c", _DAEMON, sys.executable, _HOLD],
                                    self.window_env(**window))
        daemon.family = [daemon.pid, hosted]
        return hosted

    def herdr_window(self):
        return {"HERDR_PANE_ID": _PANE, "HERDR_SESSION": _SERVER, "HERDR_SOCKET_PATH": self.socket}

    def test_shell_attached_session_reports_its_own_window_value_on_all_harnesses(self):
        for harness in sorted(session_shell.HARNESSES):
            for window, expected in ((self.herdr_window(), _PANE), ({"TMUX_PANE": "%7"}, "%7")):
                with self.case(harness=harness, window=expected):
                    native = self.attached_session(harness, **window)
                    payload = peer_steward_native.native_agent(harness, _SID)
                    agent = payload["result"]["agent"]
                    self.assertEqual(agent["pid"], native.pid)
                    self.assertEqual(agent["pane_id"], expected)

    def test_watch_by_full_session_address_arms_and_records_supervision(self):
        self.watch_arms_and_records(lambda harness: f"{harness}:{_SID}")

    def test_watch_by_window_address_arms_and_records_supervision(self):
        # What a supervisor can run for a session already up, with or without this fix.
        self.watch_arms_and_records(lambda harness: _PANE)

    def watch_arms_and_records(self, address):
        for harness in sorted(session_shell.HARNESSES):
            with self.case(harness=harness):
                native = self.attached_session(harness, **self.herdr_window())
                args = peer.build_parser().parse_args(
                    ["watch", address(harness), "--wake", "none", "--timeout", "1000"])
                observer = SimpleNamespace(pid=native.pid)
                with mock.patch.object(peer, "_HERDR_SESSION", _SERVER), \
                     mock.patch.object(peer, "_current_session_identity",
                                       return_value=("steward-sid", "codex")), \
                     mock.patch.object(peer, "_steward_seat", return_value=None), \
                     mock.patch.object(peer, "_herdr_missing", side_effect=AssertionError("Herdr probe")), \
                     mock.patch.object(peer.subprocess, "Popen", return_value=observer), \
                     mock.patch.object(peer, "end_with_test", side_effect=lambda child: child), \
                     mock.patch.object(peer, "_mark_observed") as mark, \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    code = peer.cmd_watch(args)
                self.assertEqual(code, 0, output.getvalue())
                self.assertIn("state=armed", output.getvalue())
                self.assertNotIn("identity-unverified", output.getvalue())
                mark.assert_called_once()
                self.assertEqual(mark.call_args.args[:2], (harness, _SID))
                self.assertEqual(mark.call_args.kwargs["pane"], _PANE)

    def test_a_session_with_no_window_value_names_what_is_missing(self):
        for harness in sorted(session_shell.HARNESSES):
            with self.case(harness=harness):
                self.attached_session(harness)
                args = peer.build_parser().parse_args(
                    ["watch", f"{harness}:{_SID}", "--wake", "none", "--timeout", "1000"])
                with mock.patch.object(peer, "_current_session_identity",
                                       return_value=("steward-sid", "codex")), \
                     mock.patch.object(peer, "_steward_seat", return_value=None), \
                     mock.patch.object(peer, "_mark_observed") as mark, \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    code = peer.cmd_watch(args)
                self.assertEqual(code, 4)
                self.assertEqual(output.getvalue().strip(),
                                 "native-unavailable reason=watch-target-window-unknown fallback=poll-fallback")
                mark.assert_not_called()

    def test_window_address_skips_a_session_a_native_daemon_hosts(self):
        for harness in sorted(session_shell.HARNESSES):
            with self.case(harness=harness):
                self.attached_session(harness, **self.herdr_window())
                hosted = self.daemon_hosted_session(**self.herdr_window())
                # The hosted session is still a live native session, addressable by itself.
                self.assertIn(hosted, [row["pid"] for row in peer_address.native_sessions()])
                self.assertEqual(peer_address.resolve(_PANE, server=_SERVER),
                                 ({"harness": harness, "session_id": _SID, "pane": _PANE}, None))

    def test_two_window_sessions_with_one_value_stay_ambiguous(self):
        self.attached_session("claude", **self.herdr_window())
        self.spawn(["claude", "-c", _HOLD], self.window_env(**self.herdr_window()))
        self.assertEqual(peer_address.resolve(_PANE, server=_SERVER), (None, "target-pane-ambiguous"))


if __name__ == "__main__":
    unittest.main()
