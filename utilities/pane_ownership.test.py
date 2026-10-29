#!/usr/bin/env python3
"""Inherited daemon labels must not claim another terminal's seat or card."""
import fcntl
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import pty
import subprocess
import sys
import tempfile
import termios
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools"))
import pane_ownership as po
import session_tidy as st
import session_tidy_native as native


class PaneOwnershipTest(unittest.TestCase):
    def test_codex_runtime_skips_config_values_before_service_command(self):
        cases = (
            (["codex", "-c", "foo=app-server", "app-server"], True),
            (["codex", "--config", "foo=app-server", "tui"], False),
            (["codex", "--config=foo=app-server", "resume", "sid"], False),
            (["codex", "--model", "app-server", "tui"], False),
            (["codex", "--model", "o3", "app-server"], True),
            (["codex", "tui", "prompt", "app-server"], False),
        )
        for argv, service in cases:
            with self.subTest(argv=argv), mock.patch.object(
                    Path, "read_bytes",
                    return_value=b"\0".join(part.encode() for part in argv) + b"\0"):
                self.assertEqual(po._runtime(123), ("codex", service))

    def test_codex_daemon_binding_ignores_inherited_pane_and_fails_closed(self):
        sid = "019facc9-fe83-7480-89f6-9e64fbebf0ca"
        binding = {"pid": 1234, "pid_start": "77", "session_id": sid,
                   "pane": "w:p1", "socket_path": "/tmp/herdr.sock"}
        with mock.patch.object(po, "_registered_worker", return_value=False), \
                mock.patch.object(po, "_caller_runtime", return_value=((99, 1, 1, 0, 0, "1"), True)), \
                mock.patch.object(po, "codex_tui_process_bindings", return_value=[binding]), \
                mock.patch.object(po, "_process", return_value=(1234, 1, 1, 0, 0, "77")):
            got = po.codex_caller_binding(sid, pid=99, environ={
                "CODEX_THREAD_ID": sid, "HERDR_PANE_ID": "foreign", "HERDR_SOCKET_PATH": "/tmp/foreign"})
        self.assertEqual(got, binding)
        with mock.patch.object(po, "_registered_worker", return_value=False), \
                mock.patch.object(po, "_caller_runtime", return_value=((99, 1, 1, 0, 0, "1"), True)), \
                mock.patch.object(po, "codex_tui_process_bindings", return_value=[binding, dict(binding, pane="w:p2")]):
            self.assertIsNone(po.codex_caller_binding(
                sid, pid=99, environ={"CODEX_THREAD_ID": sid, "HERDR_PANE_ID": "w:p1"}))

    def test_pane_seat_from_binding_uses_the_bound_socket_not_inherited_environment(self):
        binding = {"pid": 1, "pid_start": "2", "session_id": "t-A",
                   "pane": "w:p1", "socket_path": "/run/herdr/one.sock"}
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/run/herdr/other.sock"}):
            seat = st.pane_seat_from_binding(binding)
        expected_endpoint = st._endpoint_scope({"HERDR_SOCKET_PATH": binding["socket_path"]})
        self.assertEqual((seat.pane, seat.endpoint, seat.socket_path),
                         ("w:p1", expected_endpoint, binding["socket_path"]))
        self.assertNotEqual(seat.key, st._pane_seat_of("w:p1").key)

    def test_daemon_enqueue_and_start_hook_share_bound_endpoint(self):
        import session_tidy_clear as clear
        current = "019facc9-fe83-7480-89f6-9e64fbebf0ca"
        successor = "019facc9-fe83-7480-89f6-9e64fbebf0cb"
        bindings = {
            current: {"pid": 1234, "pid_start": "77", "session_id": current,
                      "pane": "w:p1", "socket_path": "/run/herdr/actual.sock"},
            successor: {"pid": 1234, "pid_start": "77", "session_id": successor,
                        "pane": "w:p1", "socket_path": "/run/herdr/actual.sock"},
        }
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(temp) / "state"),
                                             "XDG_CONFIG_HOME": str(Path(temp) / "config"),
                                             "CODEX_THREAD_ID": current, "HERDR_PANE_ID": "foreign-pane",
                                             "HERDR_SOCKET_PATH": "/run/herdr/foreign.sock"}, clear=True), \
                mock.patch.object(po, "_caller_runtime",
                                  return_value=((99, 1, 1, 0, 0, "1"), True)), \
                mock.patch.object(po, "codex_caller_binding", side_effect=lambda sid, **_kw: bindings.get(sid)), \
                mock.patch.object(clear, "herdr_command", return_value="/bin/true"), \
                mock.patch.object(clear, "_start_helper", return_value=4242):
            seat = st.resolve_seat("codex", temp, sid=current)
            self.assertEqual((seat.kind, seat.pane, seat.socket_path),
                             ("pane", "w:p1", "/run/herdr/actual.sock"))
            with st.seat_lock(seat.key):
                st.write_card(seat, "codex", current, "앞 세대 카드", cwd=temp)
            self.assertEqual(clear.schedule_for_enqueue(seat, "codex", current, temp), "clear=scheduled")
            request = clear.read_reservation(seat.key)
            self.assertEqual(request["herdr_transport"], {"socket_path": "/run/herdr/actual.sock"})
            os.environ["CODEX_THREAD_ID"] = successor
            shown = []
            text = st.run_hook("codex", "start", successor, source="clear", cwd=temp, emit=shown.append)
            self.assertIn("앞 세대 카드", text)
            self.assertEqual(st.resolve_seat("codex", temp, sid=successor).key, seat.key)

    def test_confirmed_clear_lookup_uses_the_booking_endpoint(self):
        import session_tidy_clear as clear
        binding = {"pid": 1234, "pid_start": "77", "session_id": "thread-A",
                   "pane": "w:p1", "socket_path": "/run/herdr/booked.sock"}
        seat = st.pane_seat_from_binding(binding)
        booking = {"harness": "codex", "status": "cleared", "new_session": "thread-B",
                   "seat": seat.as_dict()}
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(temp) / "state"),
                                             "HERDR_SOCKET_PATH": "/run/herdr/inherited.sock"}, clear=True), \
                mock.patch.object(clear, "clear_dir", return_value=Path(temp)), \
                mock.patch.object(clear, "read_reservation", return_value=booking):
            (Path(temp) / f"{seat.key}.json").write_text("{}")
            self.assertEqual(st._cleared_into("thread-B"), ["w:p1"])
            self.assertEqual(st._pane_seat_of("w:p1", endpoint=seat.endpoint).key, seat.key)

    def test_import_adds_tools_path_for_isolated_utilities_entrypoint(self):
        utilities = str(ROOT / "utilities")
        script = (
            "import sys; "
            f"sys.path.insert(0, {utilities!r}); "
            "import pane_ownership; "
            "from fleet.collectors import procscan; "
            "assert procscan.__name__ == 'fleet.collectors.procscan'"
        )
        result = subprocess.run([sys.executable, "-I", "-c", script],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pane-ownership-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.info = self.root / "process-info.json"
        self.herdr = self.root / "herdr"
        self.herdr.write_text("#!/usr/bin/env python3\nfrom pathlib import Path\n"
                              f"print(Path({str(self.info)!r}).read_text())\n")
        self.herdr.chmod(0o755)
        self.thread_env = mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "",
                                                       "HERDR_SOCKET_PATH": ""})
        self.thread_env.start()
        self.addCleanup(self.thread_env.stop)

    def runtime(self, harness="claude", *extra_argv):
        master, slave = pty.openpty()
        env = os.environ.copy()
        for name in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                     "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "MEM_DISTILL"):
            env.pop(name, None)
        if harness == "codex":
            env.update({"HERDR_PANE_ID": "wB:p3N", "HERDR_SOCKET_PATH": ""})

        def terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        proc = subprocess.Popen([harness, "-c", "import time; time.sleep(60)", *extra_argv],
                                executable=sys.executable, stdin=slave,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                                preexec_fn=terminal)
        os.close(slave)
        self.addCleanup(os.close, master)
        def stop():
            proc.terminate()
            proc.wait(timeout=5)
        self.addCleanup(stop)
        return proc

    def foreground(self, proc, pane="wB:p3N"):
        self.info.write_text(json.dumps({"result": {"process_info": {
            "pane_id": pane, "foreground_process_group_id": proc.pid,
            "foreground_processes": [{"pid": proc.pid}]}}}))

    def verify(self, pid, harness="claude", sid=None):
        return po.verified_pane("wB:p3N", harness, sid, pid=pid, executable=str(self.herdr))

    def test_real_foreground_terminals_keep_their_seat_on_every_harness(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                foreground = self.runtime(harness)
                self.foreground(foreground)
                sid = "019facc9-fe83-7480-89f6-9e64fbebf0ca" if harness == "codex" else None
                if harness == "codex":
                    with mock.patch.object(po, "_native_session", return_value=sid), \
                            mock.patch.object(po, "_read_codex_footer_threads", return_value={sid}), \
                            mock.patch.object(po, "_registered_worker", return_value=False):
                        self.assertEqual(self.verify(foreground.pid, harness, sid), "wB:p3N")
                else:
                    self.assertEqual(self.verify(foreground.pid, harness, sid), "wB:p3N")

    def test_claude_bg_job_exact_attach_owns_only_its_foreground_pane(self):
        from fleet import herdr_projection as hp
        job, sid = "deadbeef", "bg-session-one"
        bg, attach = self.runtime(), self.runtime()
        self.foreground(attach)
        bg_record = po._process(bg.pid)
        native_record = {"pid": bg.pid, "procStart": bg_record[5], "kind": "bg",
                         "jobId": job, "sessionId": sid}
        sessions = self.root / "sessions"
        sessions.mkdir()
        declaration = sessions / f"{bg.pid}.json"
        declaration.write_text(json.dumps(native_record))
        original_read = Path.read_bytes
        attach_argv = [b"claude\0attach\0deadbeef\0"]

        def cmdline(path):
            if str(path) == f"/proc/{attach.pid}/cmdline":
                return attach_argv[0]
            if str(path) == f"/proc/{bg.pid}/cmdline":
                return b"claude bg-spare\0"
            return original_read(path)

        with mock.patch.object(po, "_caller_runtime", return_value=(bg_record, True)), \
                mock.patch.object(native, "_caller_runtime", return_value=(bg_record, True)), \
                mock.patch.object(po.os, "getpid", return_value=bg.pid), \
                mock.patch.object(Path, "read_bytes", cmdline), \
                mock.patch.object(po.shutil, "which", return_value=str(self.herdr)), \
                mock.patch.object(hp, "runtime_identity", return_value=("claude", sid)), \
                mock.patch.dict(os.environ, {"HERDR_PANE_ID": "wB:p3N", "CLAUDE_CONFIG_DIR": str(self.root)}):
            for executable in (b"claude", b"/home/u/.local/share/claude/versions/2.1.30"):
                with self.subTest(executable=executable):
                    attach_argv[0] = executable + b"\0attach\0deadbeef\0"
                    self.assertEqual(po._runtime(attach.pid), ("claude", False))
                    self.assertEqual(po.verified_pane("wB:p3N", "claude", sid,
                                                      executable=str(self.herdr)), "wB:p3N")
                    self.assertTrue(hp.may_report("claude", sid, worker=False))
            for argv in (b"/opt/unrelated/2.1.30\0attach\0deadbeef\0",
                         b"/home/u/.local/share/claude/versions/2.1.30\0attach\0badc0ffe\0"):
                with self.subTest(argv=argv):
                    attach_argv[0] = argv
                    self.assertEqual(po.verified_pane("wB:p3N", "claude", sid,
                                                      executable=str(self.herdr)), "")
                    self.assertFalse(hp.may_report("claude", sid, worker=False))
            attach_argv[0] = b"/home/u/.local/share/claude/versions/2.1.30\0attach\0deadbeef\0"
            self.assertEqual(po.verified_pane("wB:p3N", "claude", "wrong", executable=str(self.herdr)), "")
            self.assertFalse(hp.may_report("claude", "wrong", worker=False))
            self.foreground(attach, "other-pane")
            self.assertEqual(po.verified_pane("wB:p3N", "claude", sid, executable=str(self.herdr)), "")
            self.assertFalse(hp.may_report("claude", sid, worker=False))
            self.foreground(attach)
            self.assertEqual(po.verified_pane("wB:p3N", "claude", sid, pid=attach.pid, executable=str(self.herdr)), "")
            for key, value in (("jobId", "other"), ("procStart", "old"),
                               ("sessionId", "other-session"), ("kind", "fg")):
                declaration.write_text(json.dumps(dict(native_record, **{key: value})))
                self.assertEqual(po.verified_pane("wB:p3N", "claude", sid, executable=str(self.herdr)), "")
                self.assertFalse(hp.may_report("claude", sid, worker=False))
            declaration.write_text(json.dumps(native_record))
            declaration.unlink()
            declaration.symlink_to(self.info)
            self.assertEqual(po.verified_pane("wB:p3N", "claude", sid, executable=str(self.herdr)), "")

    def test_inherited_label_different_pty_cannot_overwrite_or_consume_foreground_card(self):
        foreground, unrelated = self.runtime(), self.runtime()
        self.foreground(foreground)
        self.assertNotEqual(po._process(foreground.pid)[3], po._process(unrelated.pid)[3])
        env = {"HERDR_PANE_ID": "wB:p3N", "XDG_STATE_HOME": str(self.root / "state")}
        with mock.patch.dict(os.environ, env), mock.patch.object(st, "project_key_for", return_value="home-os"), \
                mock.patch.object(st, "_herdr_executable", return_value=str(self.herdr)), \
                mock.patch.object(po.os, "getpid", return_value=unrelated.pid):
            seat = st.resolve_seat("claude", str(self.root), sid="home-os-sid")
            # Before the fix resolve_seat returned this pane solely from HERDR_PANE_ID.
            self.assertEqual(seat.kind, "project")
            pane_seat = st.Seat("pane", st._digest("pane", "wB:p3N"), "wB:p3N", "claude", "")
            st.write_card(pane_seat, "claude", "supervisor-sid", "감독 카드", cwd=str(self.root))
            previous = st.read_latest_card(pane_seat)
            st.write_card(seat, "claude", "home-os-sid", "home-os 카드", cwd=str(self.root))
            self.assertEqual(st.read_latest_card(pane_seat), previous)
            self.assertEqual(st.read_latest_card(seat)["body"], "home-os 카드")
            consumed = st._consumed_path(pane_seat)
            before = consumed.read_bytes() if consumed.exists() else None
            self.assertEqual(st.run_hook("claude", "start", "home-os-successor",
                                         cwd=str(self.root), env=env), "")
            successor = st.session_seat(seat, "claude", "home-os-successor")
            now = st.now_epoch()
            with st.session_state_lock(successor):
                st.record_event(successor, "claude", "home-os-successor", "start",
                                source="clear", cwd=str(self.root), now=now)
                st._append_ledger_entry(successor, {
                    "ts": now + 0.001, "harness": "claude", "sid": "home-os-successor",
                    "event": "handover", "from": {"harness": "claude", "sid": "home-os-sid"},
                    "source": "clear",
                })
            injected = st.run_hook("claude", "start", "home-os-successor",
                                   cwd=str(self.root), env=env)
            self.assertIn("home-os 카드", injected)
            self.assertNotIn("감독 카드", injected)
            self.assertEqual(consumed.read_bytes() if consumed.exists() else None, before)

    def test_parked_frontend_does_not_prove_current_attached_session(self):
        from fleet.collectors import claude
        foreground = self.runtime()
        self.foreground(foreground)
        record = po._process(foreground.pid)
        with mock.patch.object(claude, "read_registry", return_value={
                "procStart": record[5], "sessionId": "original-sid", "parkedJobId": "original-job"}):
            self.assertEqual(po._native_session(foreground.pid, "claude"), "")

    def test_unrelated_inheritance_is_rejected_on_codex_and_opencode_too(self):
        for harness in ("codex", "opencode"):
            foreground, unrelated = self.runtime(harness), self.runtime(harness)
            self.foreground(foreground)
            self.assertEqual(self.verify(unrelated.pid, harness, "unrelated-session"), "")

    def test_daemon_or_spare_with_no_controlling_terminal_is_not_foreground(self):
        foreground = self.runtime()
        self.foreground(foreground)
        proc = subprocess.Popen(["claude", "-c", "import time; time.sleep(60)"],
                                executable=sys.executable, start_new_session=True)
        self.addCleanup(lambda: (proc.terminate(), proc.wait(timeout=5)))
        self.assertEqual(self.verify(proc.pid), "")

    def test_stale_foreground_pid_foreign_pane_and_changed_process_start_fail_closed(self):
        foreground = self.runtime()
        self.foreground(foreground, "another-pane")
        self.assertEqual(self.verify(foreground.pid), "")
        self.foreground(foreground)
        record = po._process(foreground.pid)
        recycled = (*record[:5], "another-start")
        with mock.patch.object(po, "_process", side_effect=[record, record, recycled]):
            self.assertEqual(self.verify(foreground.pid), "")
        with mock.patch.object(po, "_process", return_value=None):
            self.assertEqual(self.verify(foreground.pid), "")

    def test_process_identity_works_without_footer_and_visible_difference_refuses(self):
        from fleet.collectors import procscan
        foreground = self.runtime("codex")
        self.foreground(foreground)
        current = "019facc9-fe83-7480-89f6-9e64fbebf0ca"
        owner_env = {"HERDR_PANE_ID": "wB:p3N", "HERDR_SOCKET_PATH": ""}
        with mock.patch.object(procscan, "read_environ", return_value=owner_env), \
                mock.patch.object(po, "_native_session", return_value=current), \
                mock.patch.object(po, "_read_codex_footer_threads", return_value=set()), \
                mock.patch.object(po, "_registered_worker", return_value=False):
            self.assertEqual(self.verify(foreground.pid, "codex", current), "wB:p3N")
        with mock.patch.object(procscan, "read_environ", return_value=owner_env), \
                mock.patch.object(po, "_native_session", return_value=current), \
                mock.patch.object(po, "_read_codex_footer_threads", return_value={"different-thread"}), \
                mock.patch.object(po, "_registered_worker", return_value=False):
            self.assertEqual(self.verify(foreground.pid, "codex", current), "")

    def test_current_codex_thread_rechecks_native_claim_and_process_birth(self):
        foreground = self.runtime("codex")
        self.foreground(foreground)
        current = "019facc9-fe83-7480-89f6-9e64fbebf0ca"
        replacement = "019facc9-fe83-7480-89f6-9e64fbebf0cb"
        record = po._process(foreground.pid)
        with mock.patch.object(po, "_runtime", return_value=("codex", False)), \
                mock.patch.object(po, "_native_session", side_effect=(current, replacement)) as native, \
                mock.patch.object(po, "_read_codex_footer_threads", return_value=set()), \
                mock.patch.object(po, "_registered_worker", return_value=False):
            self.assertEqual(po.current_codex_thread(
                "wB:p3N", foreground.pid, executable=str(self.herdr)), "")
        self.assertEqual(native.call_count, 2)

        recycled = (*record[:5], "different-start")
        with mock.patch.object(po, "_runtime", return_value=("codex", False)), \
                mock.patch.object(po, "_process", side_effect=(record, recycled)), \
                mock.patch.object(po, "_native_session", return_value=current) as native, \
                mock.patch.object(po, "_read_codex_footer_threads", return_value=set()), \
                mock.patch.object(po, "_registered_worker", return_value=False):
            self.assertEqual(po.current_codex_thread(
                "wB:p3N", foreground.pid, executable=str(self.herdr)), "")
        native.assert_called_once()

    def test_current_codex_thread_reuses_the_collection_daemon_observation(self):
        from fleet.collectors import codex

        pid = 43123
        sid = "019facc9-fe83-7480-89f6-9e64fbebf0ca"
        record = (pid, 1, 77, 4, 77, "778899")
        info = {"pane_id": "w:p1", "foreground_process_group_id": 77,
                "foreground_processes": [{"pid": pid}]}
        with codex.daemon_identity_read_scope() as observation, \
                mock.patch.object(po, "_pane_process_info", return_value=info), \
                mock.patch.object(po, "_process", return_value=record), \
                mock.patch.object(po, "_runtime", return_value=("codex", False)), \
                mock.patch.object(po, "_registered_worker", return_value=False), \
                mock.patch.object(po, "_native_session", return_value=sid) as native, \
                mock.patch.object(po, "_read_codex_footer_threads", return_value=set()):
            self.assertEqual(po.current_codex_thread("w:p1", pid, executable="/fake/herdr"), sid)
        self.assertEqual(native.call_count, 2)
        self.assertTrue(all(call.kwargs["codex_observation"] is observation
                            for call in native.call_args_list))

    def test_yolo_tui_proves_its_thread_and_refuses_a_foreign_one(self):
        from fleet.collectors import codex, procscan
        foreground = self.runtime("codex", "--yolo")
        self.foreground(foreground)
        current = "019facc9-fe83-7480-89f6-9e64fbebf0ca"
        foreign = "019facc9-fe83-7480-89f6-9e64fbebf0cb"
        owner_env = {"HERDR_PANE_ID": "wB:p3N", "HERDR_SOCKET_PATH": ""}
        # Only the daemon's origin/listener facts are faked; the foreground
        # argv gate (procscan.invocation_mode) runs for real.
        with mock.patch.object(procscan, "read_environ", return_value=owner_env), \
                mock.patch.object(procscan, "_comm_of", return_value="codex"), \
                mock.patch.object(codex, "_proc_rollout", return_value=None), \
                mock.patch.object(codex, "_daemon_thread_origins",
                                  return_value={current: (41301, "user")}), \
                mock.patch.object(codex, "_tcp_listener_snapshot",
                                  return_value=(frozenset({"1"}), frozenset({foreground.pid}))), \
                mock.patch.object(codex, "_daemon_endpoint_current", return_value=True), \
                mock.patch.object(codex, "_listener_unchanged", return_value=True), \
                mock.patch.object(po, "_read_codex_footer_threads", return_value=set()), \
                mock.patch.object(po, "_registered_worker", return_value=False):
            self.assertEqual(self.verify(foreground.pid, "codex", current), "wB:p3N")
            self.assertEqual(self.verify(foreground.pid, "codex", foreign), "")

    def test_loader_pane_and_endpoint_stay_paired_for_later_actions(self):
        from fleet.collectors import procscan
        foreground = self.runtime("codex")
        self.foreground(foreground)
        current = "019facc9-fe83-7480-89f6-9e64fbebf0ca"
        owner_env = {"HERDR_PANE_ID": "wB:p3N", "HERDR_SOCKET_PATH": "/owner.sock"}
        with mock.patch.object(po.os, "listdir", return_value=[str(foreground.pid)]), \
                mock.patch.object(procscan, "read_environ", return_value=owner_env), \
                mock.patch.object(po, "_native_session", return_value=current), \
                mock.patch.object(po, "_read_codex_footer_threads", return_value=set()), \
                mock.patch.object(po, "_registered_worker", return_value=False):
            self.assertEqual(po.codex_tui_pane_endpoint(current, executable=str(self.herdr)),
                             ("wB:p3N", "/owner.sock"))
            binding = po.codex_tui_process_bindings(current, executable=str(self.herdr))
            self.assertEqual(binding, [{
                "pid": foreground.pid,
                "pid_start": str(po._process(foreground.pid)[5]),
                "session_id": current,
                "pane": "wB:p3N",
                "socket_path": "/owner.sock",
            }])
            native_process = po.codex_tui_processes_for_session(current)
            self.assertEqual(native_process, [{
                "pid": foreground.pid,
                "pid_start": str(po._process(foreground.pid)[5]),
                "session_id": current,
            }])
            with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/owner.sock"}, clear=True):
                self.assertEqual(self.verify(foreground.pid, "codex", current), "wB:p3N")
            with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/caller.sock"}, clear=True):
                self.assertEqual(self.verify(foreground.pid, "codex", current), "")

        named_server = {"HERDR_PANE_ID": "wB:p3N", "AGENT_HERDR_SESSION": "secondary"}
        with mock.patch.object(po.os, "listdir", return_value=[str(foreground.pid)]), \
                mock.patch.object(procscan, "read_environ", return_value=named_server), \
                mock.patch.object(po, "_native_session", return_value=current), \
                mock.patch.object(po, "_registered_worker", return_value=False):
            self.assertIsNone(po.codex_tui_pane_endpoint(current, executable=str(self.herdr)))

    def test_endpoint_reads_the_pane_hint_before_the_session_proof(self):
        from fleet.collectors import procscan
        foreground = self.runtime("codex")
        self.foreground(foreground)
        current = "019facc9-fe83-7480-89f6-9e64fbebf0ca"
        owner_env = {"HERDR_PANE_ID": "wB:p3N", "HERDR_SOCKET_PATH": "/owner.sock"}
        with mock.patch.object(po.os, "listdir", return_value=[str(foreground.pid)]), \
                mock.patch.object(procscan, "read_environ", return_value=owner_env), \
                mock.patch.object(po, "_native_session", return_value=current) as proof, \
                mock.patch.object(po, "_read_codex_footer_threads", return_value=set()), \
                mock.patch.object(po, "_registered_worker", return_value=False):
            self.assertIsNone(po.codex_tui_pane_endpoint(
                current, pane_hint="wB:p9Z", executable=str(self.herdr)))
            proof.assert_not_called()
            self.assertEqual(po.codex_tui_pane_endpoint(
                current, pane_hint="wB:p3N", executable=str(self.herdr)),
                ("wB:p3N", "/owner.sock"))
            # The loader's own before/after reads are the whole proof.
            self.assertEqual(proof.call_count, 2)
            observation = proof.call_args_list[0].kwargs["codex_observation"]
            self.assertIsNotNone(observation)
            self.assertTrue(all(call.kwargs["codex_observation"] is observation
                                for call in proof.call_args_list))

    def test_multi_candidate_endpoint_uses_one_collector_observation(self):
        from fleet.collectors import procscan

        current = "019facc9-fe83-7480-89f6-9e64fbebf0ca"
        pids = [43101, 43102, 43103, 43104, 43105]
        records = {pid: (pid, 1, 77, 4, 77, str(1000 + pid)) for pid in pids}
        envs = {pid: {"HERDR_PANE_ID": f"pane-{pid}", "HERDR_SOCKET_PATH": "/owner.sock"}
                for pid in pids}

        def pane_info(pane, **_kwargs):
            pid = int(pane.removeprefix("pane-"))
            return {"pane_id": pane, "foreground_process_group_id": 77,
                    "foreground_processes": [{"pid": pid}]}

        with mock.patch.object(po.os, "listdir", return_value=[str(pid) for pid in pids]), \
                mock.patch.object(po, "_runtime", return_value=("codex", False)), \
                mock.patch.object(po, "_process", side_effect=lambda pid: records.get(int(pid))), \
                mock.patch.object(po, "_registered_worker", return_value=False), \
                mock.patch.object(procscan, "read_environ", side_effect=lambda pid: envs[int(pid)]), \
                mock.patch.object(po, "_pane_process_info", side_effect=pane_info), \
                mock.patch.object(po, "_read_codex_footer_threads", return_value=set()), \
                mock.patch.object(po, "_native_session", return_value=current) as proof:
            bindings = po.codex_tui_process_bindings(current, executable=str(self.herdr))

        self.assertEqual(len(bindings), len(pids), bindings)
        self.assertEqual(proof.call_count, 2 * len(pids))
        observation = proof.call_args_list[0].kwargs["codex_observation"]
        self.assertTrue(all(call.kwargs["codex_observation"] is observation
                            for call in proof.call_args_list))

    def test_codex_hook_can_report_new_thread_only_after_current_footer_proof(self):
        from fleet import herdr_projection as hp
        from fleet.collectors import procscan
        foreground = self.runtime("codex")
        self.foreground(foreground)
        current = "019facc9-fe83-7480-89f6-9e64fbebf0ca"
        env = {"HERDR_PANE_ID": "wB:p3N", "CODEX_THREAD_ID": current}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(hp, "runtime_identity", return_value=("codex", "old-thread")), \
                mock.patch.object(hp, "_nearest_codex_pid", return_value=foreground.pid), \
                mock.patch.object(po.shutil, "which", return_value=str(self.herdr)), \
                mock.patch.object(procscan, "read_environ", return_value={
                    "HERDR_PANE_ID": "wB:p3N", "HERDR_SOCKET_PATH": ""}), \
                mock.patch.object(po, "_native_session", return_value=current), \
                mock.patch.object(po, "_read_codex_footer_threads", return_value={current}), \
                mock.patch.object(po, "_registered_worker", return_value=False):
            self.assertTrue(hp.may_report("codex", current, worker=False))
            self.assertFalse(hp.may_report("codex", "old-thread", worker=False))

    def test_session_tidy_uses_callers_exact_codex_thread_id(self):
        foreground = self.runtime("codex")
        self.foreground(foreground)
        current = "019facc9-fe83-7480-89f6-9e64fbebf0ca"
        process = po._process(foreground.pid)
        env = {"CODEX_THREAD_ID": current, "HERDR_PANE_ID": "wB:p3N"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(po, "_caller_runtime", return_value=(process, False)), \
                mock.patch.object(po.shutil, "which", return_value=str(self.herdr)), \
                mock.patch.object(po, "_read_codex_footer_threads", return_value={current}), \
                mock.patch.object(po, "_registered_worker", return_value=False):
            self.assertEqual(st.session_from_env(), ("codex", current))

    def test_dual_root_transition_keeps_caller_thread_but_refuses_pane(self):
        caller = (123, 1, 123, 0, -1, "server-start")
        caller_thread = "019facc9-fe83-7480-89f6-9e64fbebf0ca"
        visible_new_thread = "019facc9-fe83-7480-89f6-9e64fbebf0cb"
        with mock.patch.object(po, "_caller_runtime", return_value=(caller, True)), \
                mock.patch.object(po, "_native_session", return_value=""), \
                mock.patch.object(po, "_read_codex_footer_threads", return_value=set()), \
                mock.patch.object(po, "_registered_worker", return_value=False), \
                mock.patch.dict(os.environ, {"CODEX_THREAD_ID": caller_thread}, clear=True):
            self.assertEqual(po.current_codex_thread_for_caller("wB:p3N"), caller_thread)
            self.assertEqual(self.verify(123, "codex", caller_thread), "")
            with mock.patch.object(po, "_read_codex_footer_threads",
                                   return_value={visible_new_thread}):
                self.assertEqual(self.verify(123, "codex", caller_thread), "")

    def test_pane_hook_and_peer_use_the_same_refusal(self):
        from fleet import herdr_projection as hp
        with mock.patch.object(hp, "runtime_identity", return_value=("claude", "home-os-sid")), \
                mock.patch.object(po, "verified_pane", return_value=""), \
                mock.patch.dict(os.environ, {"HERDR_PANE_ID": "wB:p3N"}):
            self.assertFalse(hp.may_report("claude", "home-os-sid", worker=False))
            spec = importlib.util.spec_from_file_location("pane_test_steward", ROOT / "utilities/peer-steward.py")
            peer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(peer)
            with mock.patch.object(peer, "_current_session_identity", return_value=("home-os-sid", "claude")):
                self.assertEqual(peer._caller_pane(), "")

    def test_fake_inherited_codex_thread_and_registered_worker_cannot_report(self):
        from fleet import herdr_projection as hp
        env = {"CODEX_THREAD_ID": "inherited-fake-thread", "HERDR_PANE_ID": "wB:p3N"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(hp, "runtime_identity", return_value=("codex", "selected-thread")), \
                mock.patch.object(hp, "_nearest_codex_pid", return_value=os.getpid()), \
                mock.patch.object(hp, "verified_pane",
                                  side_effect=lambda pane, harness, sid, **kw:
                                  "wB:p3N" if sid == "selected-thread" else ""):
            self.assertFalse(hp.may_report("codex", "inherited-fake-thread"))
            self.assertTrue(hp.may_report("codex", "selected-thread"))
            with mock.patch.dict(os.environ, {"AGENT_SESSION_ROLE": "worker"}):
                self.assertFalse(hp.may_report("codex", "selected-thread"))

    def test_inherited_pane_cannot_receive_or_retire_another_seats_dispatch(self):
        import dispatch_seat_handover as handover
        foreground, unrelated = self.runtime(), self.runtime()
        self.foreground(foreground)
        env = {"HERDR_PANE_ID": "wB:p3N", "XDG_STATE_HOME": str(self.root / "state")}
        with mock.patch.dict(os.environ, env), mock.patch.object(st, "project_key_for", return_value="home-os"), \
                mock.patch.object(st, "_herdr_executable", return_value=str(self.herdr)), \
                mock.patch.object(po.os, "getpid", return_value=unrelated.pid), \
                mock.patch.object(handover, "handover_rows") as rows, \
                mock.patch.object(handover, "current_bindings") as bindings:
            self.assertIsNone(handover.pane_seat(env, "claude", "home-os-sid"))
            self.assertEqual(handover.storage_recipients("home-os-sid", env, "claude"), [("home-os-sid", None)])
            self.assertIsNone(handover.record_retire_handover("supervisor-sid", "claude", "home-os-sid", "claude", env=env))
            rows.assert_not_called()
            bindings.assert_not_called()

    def test_deferred_report_cannot_promote_a_guessed_foreground_session(self):
        from fleet import herdr_projection as hp
        from fleet.collectors import procscan
        foreground = self.runtime("codex")
        self.foreground(foreground)
        with mock.patch.object(po.shutil, "which", return_value=str(self.herdr)), \
                mock.patch.object(hp.os, "listdir", return_value=[str(foreground.pid)]), \
                mock.patch.object(hp, "_comm", return_value="codex"), \
                mock.patch.object(procscan, "read_environ", return_value={"HERDR_PANE_ID": "wB:p3N"}), \
                mock.patch.object(po, "_native_session", return_value=""):
            self.assertIsNone(hp._codex_tui_pane("guessed-session"))
            actual = "019facc9-fe83-7480-89f6-9e64fbebf0ca"
            with mock.patch.object(po, "_native_session", return_value=actual):
                self.assertIsNone(hp._codex_tui_pane("guessed-session"))
                with mock.patch.object(po, "_read_codex_footer_threads",
                                       return_value={actual}):
                    self.assertEqual(hp._codex_tui_pane(actual), "wB:p3N")

    def test_recovery_default_does_not_use_an_inherited_pane(self):
        foreground, unrelated = self.runtime(), self.runtime()
        self.foreground(foreground)
        spec = importlib.util.spec_from_file_location("pane_test_recovery", ROOT / "utilities/interactive-main-recovery.py")
        recovery = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(recovery)
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "wB:p3N"}), \
                mock.patch.object(st, "session_from_env", return_value=("claude", "home-os-sid")), \
                mock.patch.object(po.shutil, "which", return_value=str(self.herdr)), \
                mock.patch.object(po.os, "getpid", return_value=unrelated.pid), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(recovery.main(["--check"]), recovery.EXIT_INVALID)
            self.assertEqual(json.loads(output.getvalue())["reason"], "pane-id-required")

    def test_recovery_socket_override_cannot_borrow_another_servers_proof(self):
        foreground, unrelated = self.runtime(), self.runtime()
        self.foreground(unrelated)
        foreign_info = self.info.read_text()
        self.foreground(foreground)
        self.herdr.write_text("#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\n"
                              f"print({foreign_info!r} if os.getenv('HERDR_SOCKET_PATH') == '/other.sock' "
                              f"else Path({str(self.info)!r}).read_text())\n")
        spec = importlib.util.spec_from_file_location("pane_test_recovery_socket", ROOT / "utilities/interactive-main-recovery.py")
        recovery = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(recovery)
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "wB:p3N", "HERDR_SOCKET_PATH": "/own.sock"}), \
                mock.patch.object(st, "session_from_env", return_value=("claude", "own-sid")), \
                mock.patch.object(po.shutil, "which", return_value=str(self.herdr)), \
                mock.patch.object(po.os, "getpid", return_value=foreground.pid), \
                mock.patch.object(recovery, "checked_pane") as checked, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(po.verified_pane("wB:p3N", "claude"), "wB:p3N")
            self.assertEqual(recovery.main(["--check", "--socket", "/other.sock"]), recovery.EXIT_INVALID)
            checked.assert_not_called()


class NativeCurrentSelectionTest(unittest.TestCase):
    """Current Codex selection requires native footer and physical pane proof."""

    pane = "w:p1"
    current = "019facc9-fe83-7480-89f6-9e64fbebf0ca"
    previous = "019facc9-fe83-7480-89f6-9e64fbebf0cb"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-current-selection-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "herdr-state.json"
        self.read_count = self.root / "visible-read-count"
        self.herdr = self.root / "herdr"
        self.herdr.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"state_path = {str(self.state)!r}\n"
            f"count_path = {str(self.read_count)!r}\n"
            "state = json.loads(open(state_path).read())\n"
            "args = sys.argv[1:]\n"
            "if args and args[0] == 'pane':\n"
            "    print(json.dumps(state['pane']))\n"
            "elif args and args[0] == 'agent':\n"
            "    try: count = int(open(count_path).read())\n"
            "    except (OSError, ValueError): count = 0\n"
            "    open(count_path, 'w').write(str(count + 1))\n"
            "    screens = state.get('screens', [[]])\n"
            "    lines = screens[min(count, len(screens) - 1)] if screens else []\n"
            "    print('\\n'.join(lines))\n"
        )
        self.herdr.chmod(0o755)
        self.codex = self.root / "codex"
        self.codex.write_text(
            "#!/bin/bash\n"
            "exec -a codex /usr/bin/python3 -c 'import time; time.sleep(60)'\n"
        )
        self.codex.chmod(0o755)
        self.proc = self._spawn_codex()
        self.record = po._process(self.proc.pid)
        self.assertIsNotNone(self.record)
        self.assertTrue(self.record[3])
        self.assertEqual(self.record[2], self.record[4])
        self._set_screens([[]])

    def _spawn_codex(self, *, tty=True, extra_env=None):
        env = os.environ.copy()
        for name in ("AGENT_SESSION_ROLE", *po._WORKER_ENV):
            env.pop(name, None)
        env.update(extra_env or {})
        master = slave = None
        if tty:
            master, slave = pty.openpty()

            def terminal():
                os.setsid()
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(
            [str(self.codex)], stdin=slave if tty else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
            preexec_fn=terminal if tty else None,
        )
        if slave is not None:
            os.close(slave)
        if master is not None:
            self.addCleanup(os.close, master)

        def stop():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

        self.addCleanup(stop)
        return process

    def _set_screens(self, screens, *, group=None, foreground_pid=None):
        pane_info = {
            "result": {"process_info": {
                "pane_id": self.pane,
                "foreground_process_group_id": self.record[2] if group is None else group,
                "foreground_processes": [{"pid": self.proc.pid if foreground_pid is None
                                           else foreground_pid}],
            }}
        }
        self.state.write_text(json.dumps({"pane": pane_info, "screens": screens}))
        self.read_count.write_text("0")

    @staticmethod
    def _footer(session_id):
        return ["› Ask Codex", f"gpt-5 · {session_id}", "? for shortcuts"]

    def _current(self, *, pid=None, executable=None):
        return po.current_codex_thread(self.pane, self.proc.pid if pid is None else pid,
                                       executable=str(executable or self.herdr))

    def test_missing_or_stale_process_sid_uses_current_footer_in_both_readers(self):
        self._set_screens([self._footer(self.current)] * 3)
        with mock.patch.object(po, "_native_session", return_value="") as native:
            self.assertEqual(self._current(), self.current)
        native.assert_called_once()

        self._set_screens([self._footer(self.current)] * 3)
        with mock.patch.object(po, "_native_session", return_value=self.previous):
            self.assertEqual(self._current(), self.current)

        self._set_screens([self._footer(self.current)] * 3)
        with mock.patch.object(po, "_native_session", return_value=self.previous):
            self.assertEqual(po._loader_verified_pane(
                self.pane, self.previous, self.proc.pid, self.record[5],
                executable=str(self.herdr)), "")

        self._set_screens([self._footer(self.current)] * 3)
        with mock.patch.object(po, "_native_session", return_value=self.previous):
            self.assertEqual(po._loader_verified_pane(
                self.pane, self.current, self.proc.pid, self.record[5],
                executable=str(self.herdr)), self.pane)

    def test_disagreeing_footer_refuses_and_unproved_footer_supplies_no_identity(self):
        disagreeing = [self._footer(self.current), self._footer(self.current),
                       self._footer(self.previous)]
        self._set_screens(disagreeing)
        with mock.patch.object(po, "_native_session", return_value=self.previous):
            self.assertEqual(self._current(), "")

        malformed = (
            ["› Ask Codex", f"gpt-5 · {self.current[:-3]}…", "? for shortcuts"],
            ["› Ask Codex", f"Where should the new conversation run? {self.current}",
             "? for shortcuts"],
            ["› Ask Codex", f"title: {self.current}", "? for shortcuts"],
            [],  # hidden footer
        )
        for lines in malformed:
            with self.subTest(lines=lines):
                self.assertEqual(po.codex_footer_threads(lines, current_id_only=True), set())
                self._set_screens([lines] * 3)
                with mock.patch.object(po, "_native_session", return_value=""):
                    self.assertEqual(self._current(), "")

    def test_process_fallback_and_same_pass_daemon_observation_are_preserved(self):
        from fleet.collectors import codex

        self._set_screens([[]])
        with codex.daemon_identity_read_scope() as observation, \
                mock.patch.object(po, "_native_session", return_value=self.current) as native:
            self.assertEqual(self._current(), self.current)
        self.assertEqual(native.call_count, 2)
        self.assertTrue(all(call.kwargs["codex_observation"] is observation
                            for call in native.call_args_list))

    def test_clipped_or_unconfigured_footer_keeps_exact_process_native_proof(self):
        for field in ("01a…", self.current[:-3] + "…", "100% context left"):
            with self.subTest(field=field):
                lines = ["› Ask Codex", "gpt-5 · " + field, "? for shortcuts"]
                self._set_screens([lines] * 3)
                with mock.patch.object(po, "_native_session", return_value=self.current):
                    self.assertEqual(self._current(), self.current)
                self._set_screens([lines] * 3)
                with mock.patch.object(po, "_native_session", return_value=""):
                    self.assertEqual(self._current(), "")

    def test_pid_birth_group_tty_service_and_worker_mismatches_refuse(self):
        self._set_screens([self._footer(self.current)] * 3)
        with mock.patch.object(po, "_native_session", return_value=""):
            self.assertEqual(po._loader_verified_pane(
                self.pane, self.current, self.proc.pid, "wrong-start",
                executable=str(self.herdr)), "")

        self._set_screens([self._footer(self.current)] * 3, group=self.record[2] + 1)
        with mock.patch.object(po, "_native_session", return_value=""):
            self.assertEqual(self._current(), "")

        no_tty = self._spawn_codex(tty=False)
        no_tty_record = po._process(no_tty.pid)
        self.assertEqual(no_tty_record[3], 0)
        self._set_screens([self._footer(self.current)] * 3,
                          group=no_tty_record[2], foreground_pid=no_tty.pid)
        with mock.patch.object(po, "_native_session", return_value=""):
            self.assertEqual(self._current(pid=no_tty.pid), "")

        self._set_screens([self._footer(self.current)] * 3)
        with mock.patch.object(po, "_runtime", return_value=("codex", True)), \
                mock.patch.object(po, "_native_session", return_value=""):
            self.assertEqual(self._current(), "")

        worker = self._spawn_codex(extra_env={"AGENT_SESSION_ROLE": "worker"})
        worker_record = po._process(worker.pid)
        self._set_screens([self._footer(self.current)] * 3,
                          group=worker_record[2], foreground_pid=worker.pid)
        self.assertEqual(self._current(pid=worker.pid), "")


class TerminalCallerTest(unittest.TestCase):
    """Private controlling PTYs, with a foreground job distinct from its shell."""

    def tty_variants(self, info):
        return (info, {k: v for k, v in info.items() if k != "tty"}, {**info, "tty": None})

    def terminal(self, foreground=True):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        env = {k: v for k, v in os.environ.items() if k not in
               (*po._WORKER_ENV, "AGENT_SESSION_ROLE") and not k.startswith("OPENCODE_DISPATCH")}
        script = '''
import os, subprocess, sys, json, signal, time
signal.signal(signal.SIGTTOU, signal.SIG_IGN)
def stop(*_): raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
child = subprocess.Popen([sys.executable, '-u', '-c', "import time; print('ready', flush=True); time.sleep(60)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    preexec_fn=lambda: os.setpgid(0, 0))
try:
    assert child.stdout.readline() == b'ready\\n'
    os.tcsetpgrp(0, child.pid if sys.argv[1] == '1' else os.getpgrp())
    print(json.dumps({'shell_pid':os.getpid(), 'pid':child.pid, 'tty':os.ttyname(0),
        'foreground_process_group_id':os.tcgetpgrp(0),
        'foreground_processes':[{'pid':os.tcgetpgrp(0)}]}), flush=True)
    time.sleep(60)
finally:
    child.terminate()
    child.wait(timeout=5)
    child.stdout.close()
'''
        def terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        proc = subprocess.Popen([sys.executable, "-u", "-c", script, "1" if foreground else "0"],
                                stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                preexec_fn=terminal, env=env)
        os.close(slave)
        def settle():
            proc.terminate()
            _out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, err.decode())
        self.addCleanup(settle)
        line = proc.stdout.readline()
        self.assertTrue(line, "private PTY driver did not report readiness")
        info = json.loads(line)
        return info, info["pid"]

    def test_foreground_job_with_redirected_stdio_is_proved(self):
        info, pid = self.terminal()
        self.assertNotEqual(info["shell_pid"], info["foreground_process_group_id"])
        self.assertEqual(os.readlink(f"/proc/{pid}/fd/0"), "/dev/null")
        self.assertTrue(po.verified_terminal_caller(info, pid=pid))

    def test_optional_tty_with_redirected_stdio_is_proved_from_proc(self):
        info, pid = self.terminal()
        self.assertNotEqual(po._process(pid)[3], 0)
        self.assertEqual(os.readlink(f"/proc/{pid}/fd/0"), "/dev/null")
        for variant in self.tty_variants(info):
            with self.subTest(info=variant):
                self.assertTrue(po.verified_terminal_caller(variant, pid=pid))

    def test_background_job_and_other_terminal_are_refused(self):
        background, pid = self.terminal(foreground=False)
        for variant in self.tty_variants(background):
            self.assertFalse(po.verified_terminal_caller(variant, pid=pid))
        first, pid = self.terminal()
        other, other_pid = self.terminal()
        for variant in self.tty_variants(other):
            self.assertFalse(po.verified_terminal_caller(variant, pid=pid))
        for variant in self.tty_variants(first):
            self.assertFalse(po.verified_terminal_caller(variant, pid=other_pid))

    def test_stale_start_inaccessible_uid_namespace_and_worker_are_refused(self):
        info, pid = self.terminal()
        read = po._process
        calls = {"n": 0}
        def stale(value):
            record = read(value)
            if int(value) == pid:
                calls["n"] += 1
                if calls["n"] > 1:
                    return (*record[:5], str(int(record[5]) + 1))
            return record
        for variant in self.tty_variants(info):
            calls["n"] = 0
            with mock.patch.object(po, "_process", side_effect=stale):
                self.assertFalse(po.verified_terminal_caller(variant, pid=pid))
            with mock.patch.object(po, "_process", side_effect=lambda p: None if p == info["shell_pid"] else read(p)):
                self.assertFalse(po.verified_terminal_caller(variant, pid=pid))
            with mock.patch.object(po.os, "getuid", return_value=os.getuid() + 1):
                self.assertFalse(po.verified_terminal_caller(variant, pid=pid))
            with mock.patch.object(po, "_registered_worker", return_value=True):
                self.assertFalse(po.verified_terminal_caller(variant, pid=pid))
            with mock.patch.object(po, "_runtime", return_value=("codex", True)):
                self.assertFalse(po.verified_terminal_caller(variant, pid=pid))

    def test_missing_proc_tty_and_wrong_foreground_evidence_are_refused(self):
        info, pid = self.terminal()
        read = po._process
        for variant in self.tty_variants(info):
            for altered in ({**variant, "foreground_process_group_id": info["shell_pid"]},
                            {**variant, "foreground_processes": []}, {**variant, "shell_pid": 999999999}):
                self.assertFalse(po.verified_terminal_caller(altered, pid=pid))
            def no_tty(value):
                record = read(value)
                return (*record[:3], 0, *record[4:]) if record else None
            with mock.patch.object(po, "_process", side_effect=no_tty):
                self.assertFalse(po.verified_terminal_caller(variant, pid=pid))

    def test_present_invalid_or_other_tty_is_refused(self):
        info, pid = self.terminal()
        other, _ = self.terminal()
        for tty in (other["tty"], "/dev/null", "/dev/does-not-exist", "", "pts/0", 0, []):
            with self.subTest(tty=tty):
                self.assertFalse(po.verified_terminal_caller({**info, "tty": tty}, pid=pid))


if __name__ == "__main__":
    unittest.main()
