"""Private, real-process identity and local-control checks; no operating TUI."""
from pathlib import Path
from contextlib import ExitStack
import importlib.util
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parent)]
import session_shell as shell


class SessionShellTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hearting-shell-", dir="/var/tmp")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"), "FLEET_SESSION_REGISTRY_DIR": ""})
        self.env.start(); self.addCleanup(self.env.stop)

    def child(self):
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                                 stdin=subprocess.PIPE)
        self.addCleanup(self.close_child, child)
        return child

    @staticmethod
    def close_child(child):
        # EOF normally ends the test's own direct child. Any fallback targets
        # the same checked process through a pidfd, never a numeric group.
        child.stdin.close()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            current = shell.process(child.pid)
            if current is None:
                child.wait(timeout=2); return
            fd = os.pidfd_open(child.pid)
            try:
                if shell.process(child.pid) == current:
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
            finally:
                os.close(fd)
            child.wait(timeout=2)

    def binding(self):
        native = self.child()
        return {"schema": shell.SCHEMA, "harness": "claude", "sid": "test-native-session",
                "incarnation": "a" * 32, "host_pid": os.getpid(),
                "host_start": shell.process(os.getpid())["start"], "native_pid": native.pid,
                "native_start": shell.process(native.pid)["start"], "socket": str(self.root / "control.sock")}

    def save(self, row):
        path = shell.binding_path(row["harness"], row["host_pid"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row)); path.chmod(0o600)
        return path

    def test_absent_policy_is_disabled_without_creating_any_file(self):
        self.assertEqual(shell.policy(), (False, "disabled"))
        self.assertFalse(shell.policy_path().exists())

    def test_policy_keeps_explicit_off_and_refuses_invalid_or_symlinked_input(self):
        path = shell.policy_path(); path.parent.mkdir(parents=True)
        for value, expected in [({"enabled": False}, False), ({"enabled": True}, True),
                                ({"enabled": "true"}, False), ({"other": True}, False)]:
            path.write_text(json.dumps(value)); path.chmod(0o600)
            self.assertEqual(shell.policy()[0], expected)
        path.unlink(); target = path.with_name("target.json")
        target.write_text('{"enabled":true}'); path.symlink_to(target)
        self.assertFalse(shell.policy()[0])

    def test_exact_live_birth_bound_native_attachment_is_found(self):
        row = self.binding(); path = self.save(row)
        self.assertEqual(shell.read_binding(path), row)
        self.assertEqual(shell.exact_binding("claude", row["sid"]), (row, ""))
        self.assertEqual(shell.bindings(harness="codex", sid=row["sid"]), [])

    def test_wrong_identity_birth_and_self_as_native_are_refused(self):
        row = self.binding(); self.save(row)
        self.assertFalse(shell.valid_binding(row, harness="codex"))
        self.assertFalse(shell.valid_binding(row, sid="different-session"))
        self.assertFalse(shell.valid_binding({**row, "native_start": "different-birth"}))
        self.assertFalse(shell.valid_binding({**row, "native_pid": os.getpid(),
                                              "native_start": row["host_start"]}))

    def test_mutable_or_symlinked_record_cannot_select_a_session(self):
        row = self.binding(); path = self.save(row)
        path.chmod(0o666); self.assertIsNone(shell.read_binding(path))
        target = path.with_suffix(".saved"); path.rename(target); target.chmod(0o600)
        path.symlink_to(target); self.assertIsNone(shell.read_binding(path))

    def serve(self, row, *, change_reply=False):
        listener = socket.socket(socket.AF_UNIX); listener.bind(row["socket"])
        os.chmod(row["socket"], 0o600); listener.listen(1); listener.settimeout(3)
        received = []
        def worker():
            try:
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(2); raw = bytearray()
                    while b"\n" not in raw:
                        raw.extend(conn.recv(4096))
                    req = json.loads(raw.partition(b"\n")[0]); received.append(req)
                    identity = {k: row[k] for k in ("harness", "sid", "incarnation", "host_pid",
                                "host_start", "native_pid", "native_start")}
                    if change_reply: identity["sid"] = "a-different-session"
                    conn.sendall((json.dumps({**identity, "ok": True, "state": "idle"}) + "\n").encode())
            finally:
                listener.close()
        thread = threading.Thread(target=worker); thread.start()
        self.addCleanup(thread.join, 4)
        return received

    def test_control_checks_the_real_socket_peer_and_exact_reply(self):
        row = self.binding(); received = self.serve(row)
        reply = shell.control(row, "panel", expanded=True)
        self.assertTrue(reply["ok"]); self.assertEqual(reply["sid"], row["sid"])
        self.assertEqual(received[0]["op"], "panel")
        self.assertEqual(received[0]["native_pid"], row["native_pid"])

    def test_changed_reply_is_ambiguous_and_never_claimed_not_started(self):
        row = self.binding(); self.serve(row, change_reply=True)
        reply = shell.control(row, "panel", expanded=True)
        self.assertFalse(reply["ok"]); self.assertFalse(reply["not_started"])

    def test_native_start_registers_claude_and_codex_display_identity(self):
        import session_tidy
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness), mock.patch.object(shell, "lifecycle") as report:
                sid = "00000000-0000-4000-8000-000000000052"
                env = dict(os.environ, HEARTING_SHELL_SOCKET=str(self.root / "native.sock"),
                           HEARTING_SHELL_TOKEN="synthetic-private-control-token")
                session_tidy.run_hook(harness, "start", sid, source="resume",
                                      cwd=str(self.root), env=env)
                report.assert_called_once_with(harness, "start", sid, env=env)

    def test_hook_reports_proven_native_owner_instead_of_its_helper_process(self):
        row = self.binding(); received = self.serve(row)
        owner = {"pid": row["native_pid"], "proc_start": row["native_start"], "argv": ["claude"]}
        env = {"HEARTING_SHELL_SOCKET": row["socket"], "HEARTING_SHELL_TOKEN": "private-fixture"}
        with mock.patch("fleet.process_identity.current_launch_context", return_value=owner):
            shell.lifecycle("claude", "start", row["sid"], env=env)
        self.assertEqual(received[0]["pid"], row["native_pid"])
        self.assertNotEqual(received[0]["pid"], os.getpid())
        self.assertEqual(received[0]["start"], row["native_start"])

    def test_unknown_command_and_worker_callbacks_do_not_contact_the_host(self):
        row = self.binding()
        env = {"HEARTING_SHELL_SOCKET": row["socket"], "HEARTING_SHELL_TOKEN": "private-fixture"}
        owner = {"pid": row["native_pid"], "proc_start": row["native_start"], "argv": ["claude", "mcp", "list"]}
        for value, extra in ((None, {}), (owner, {}), (owner, {"AGENT_SESSION_ROLE": "worker"})):
            with self.subTest(owner=value, env=extra), \
                    mock.patch("fleet.process_identity.current_launch_context", return_value=value), \
                    mock.patch.object(shell.socket, "socket") as contact:
                shell.lifecycle("claude", "stop", row["sid"], env={**env, **extra})
                contact.assert_not_called()

    def test_codex_stop_reports_completion_to_its_wrapped_display(self):
        path = Path(__file__).resolve().parents[1] / "adapters/codex/hooks/stop-lifecycle.py"
        spec = importlib.util.spec_from_file_location("shell_codex_stop_fixture", path)
        stop = importlib.util.module_from_spec(spec); spec.loader.exec_module(stop)
        sid = "00000000-0000-4000-8000-000000000052"
        with ExitStack() as patches:
            patches.enter_context(mock.patch.dict(os.environ, {"HEARTING_SHELL_SOCKET": str(self.root / "native.sock")}))
            patches.enter_context(mock.patch.object(stop, "_worker", return_value=False))
            patches.enter_context(mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": sid}))))
            for target in ("session_tidy_idle.launch_observer", "session_summary_trigger.launch_trigger",
                           "turn_change_record.launch", "artifact_checkpoint_trigger.launch_for_session",
                           "fleet.interaction.clear_wait"):
                patches.enter_context(mock.patch(target))
            report = patches.enter_context(mock.patch.object(shell, "lifecycle"))
            self.assertEqual(stop.main(), 0)
            report.assert_called_once_with("codex", "stop", sid)


if __name__ == "__main__":
    unittest.main()
