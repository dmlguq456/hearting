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


class PaneOwnershipTest(unittest.TestCase):
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
            return {"foreground_process_group_id": 77,
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


if __name__ == "__main__":
    unittest.main()
