#!/usr/bin/env python3
"""The watch's own seat proof reaches Herdr only for the caller's own window.

A logging `herdr` first on PATH records every call; each case ends by asserting what it saw.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "tools")]
import pane_ownership  # noqa: E402
import peer_steward_watch as watch  # noqa: E402
import session_shell  # noqa: E402
import session_tidy  # noqa: E402
from fleet.collectors import procscan  # noqa: E402

SID = "019a0000-0000-7000-8000-0000000000dd"
LOGGER = "#!/bin/sh\necho \"$@\" >> \"$FAKE_HERDR_CALLLOG\"\nexit 1\n"


class StewardSeatReachTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="seat-reach-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        (self.tmp / "bin").mkdir()
        shim = self.tmp / "bin" / "herdr"
        shim.write_text(LOGGER)
        shim.chmod(0o755)
        self.log = self.tmp / "calls.log"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("HERDR_", "AGENT_HERDR", "TMUX", "CODEX_"))}
        env.update(PATH=f"{self.tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
                   FAKE_HERDR_CALLLOG=str(self.log), HOME=str(self.tmp))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("_registered_worker", False),):
            other = mock.patch.object(pane_ownership, name, return_value=value)
            other.start()
            self.addCleanup(other.stop)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def codex_service(self, walk=True):
        """A Codex tool command running in the shared service, thread `SID`.

        `walk=False` leaves the real `codex_tui_process_bindings`, which the real pane proof
        itself uses with the caller's own pane as the hint."""
        os.environ["CODEX_THREAD_ID"] = SID
        patches = [mock.patch.object(pane_ownership, "_caller_runtime",
                                     return_value=((1, 1, 1, 1, 1, "1"), True))]
        if walk:
            guard = mock.patch.object(pane_ownership, "codex_tui_process_bindings", return_value=[])
            self.walk = guard.start()  # the host-wide Codex pane walk: it must stay uncalled
            self.addCleanup(guard.stop)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_a_tmux_caller_of_any_harness_never_asks_herdr(self):
        for harness in ("claude", "opencode", "codex"):
            with self.subTest(harness):
                with mock.patch.dict(os.environ, {"TMUX": f"{self.tmp}/none,1,0", "TMUX_PANE": "%3"}), \
                        mock.patch.object(pane_ownership, "_caller_runtime",
                                          return_value=(None, False)):
                    self.assertIsNone(watch._steward_seat(harness, SID))
        self.assertEqual(self.calls(), [])

    def test_a_caller_with_no_window_is_not_searched_for(self):
        with mock.patch.object(session_tidy, "resolve_seat") as resolve, \
                mock.patch.object(pane_ownership, "codex_tui_process_bindings") as walk, \
                mock.patch.object(pane_ownership, "_caller_runtime", return_value=((1, 1, 1, 1, 1, "1"), False)):
            self.assertIsNone(watch._steward_seat("codex", SID))
        resolve.assert_not_called()
        walk.assert_not_called()
        self.assertEqual(self.calls(), [])

    def test_a_caller_in_a_herdr_window_asks_about_its_own_pane_only(self):
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p9"}):
            self.assertIsNone(watch._steward_seat("claude", SID))
        self.assertEqual(self.calls(), ["pane process-info --pane w1:p9"])

    def test_a_codex_service_caller_finds_its_tui_pane_through_tmux_not_herdr(self):
        self.codex_service()
        socket = self.tmp / "tmux.sock"
        socket.touch()
        tui = {"pid": 4242, "pid_start": "77", "session_id": SID}
        proven = []

        def verified(pane, harness, sid, **kw):
            proven.append((pane, harness, sid, kw.get("executable")))
            return pane

        with mock.patch.object(pane_ownership, "codex_tui_processes_for_session", return_value=[tui]), \
                mock.patch.object(procscan, "read_environ",
                                  return_value={"TMUX": f"{socket},1,0", "TMUX_PANE": "%3",
                                                "HERDR_PANE_ID": "w9:p9"}), \
                mock.patch.object(pane_ownership, "verified_pane", side_effect=verified):
            seat = watch._steward_seat("codex", SID)
        pane = f"%3@{os.path.realpath(socket)}"
        self.assertEqual((seat or {}).get("kind"), "pane", seat)
        self.assertEqual(seat["pane"], pane)
        self.assertEqual(proven, [(pane, "codex", SID, "tmux")])
        self.walk.assert_not_called()
        self.assertEqual(self.calls(), [])

    def test_a_codex_service_seat_stays_unknown_when_nothing_proves_it(self):
        socket = self.tmp / "tmux.sock"
        socket.touch()
        env = {"TMUX": f"{socket},1,0", "TMUX_PANE": "%3"}
        tui = {"pid": 4242, "pid_start": "77", "session_id": SID}
        cases = {
            "no TUI process for the thread": dict(processes=[], environ=env, proof="%3"),
            "two TUI processes": dict(processes=[tui, dict(tui, pid=4243)], environ=env, proof="%3"),
            "TUI with no window at all": dict(processes=[tui], environ={}, proof="%3"),
            "Herdr-hosted TUI whose pane proof is refused": dict(
                processes=[tui], environ={"HERDR_PANE_ID": "w9:p9"}, proof=""),
            "Herdr-hosted TUI proved for another pane": dict(
                processes=[tui], environ={"HERDR_PANE_ID": "w9:p9"}, proof="w9:p1"),
            "Herdr-hosted TUI of a named server without its socket": dict(
                processes=[tui], environ={"HERDR_PANE_ID": "w9:p9", "AGENT_HERDR_SESSION": "x"},
                proof="w9:p9"),
            "pane proof refused": dict(processes=[tui], environ=env, proof=""),
        }
        for label, case in cases.items():
            with self.subTest(label):
                self.codex_service()
                with mock.patch.object(pane_ownership, "codex_tui_processes_for_session",
                                       return_value=case["processes"]), \
                        mock.patch.object(procscan, "read_environ", return_value=case["environ"]), \
                        mock.patch.object(pane_ownership, "verified_pane", return_value=case["proof"]):
                    self.assertIsNone(watch._steward_seat("codex", SID))
                self.walk.assert_not_called()
        self.assertEqual(self.calls(), [])

    def test_a_herdr_hosted_codex_tui_gives_its_own_pane_seat(self):
        self.codex_service()
        tui = {"pid": 4242, "pid_start": "77", "session_id": SID}
        proven = []

        def verified(pane, harness, sid, **kw):
            proven.append((pane, harness, sid, kw.get("socket_path")))
            return pane

        with mock.patch.object(pane_ownership, "codex_tui_processes_for_session", return_value=[tui]), \
                mock.patch.object(procscan, "read_environ",
                                  return_value={"HERDR_PANE_ID": "w9:p9",
                                                "HERDR_SOCKET_PATH": "/run/h.sock"}), \
                mock.patch.object(pane_ownership, "verified_pane", side_effect=verified):
            seat = watch._steward_seat("codex", SID)
        self.assertEqual((seat or {}).get("kind"), "pane", seat)
        self.assertEqual(seat["pane"], "w9:p9")
        self.assertEqual(proven, [("w9:p9", "codex", SID, "/run/h.sock")])
        self.walk.assert_not_called()

    def test_a_herdr_hosted_codex_tui_costs_one_process_info_for_its_own_pane_only(self):
        """Real proof path over a fake process table: the other TUI's pane is never asked."""
        self.codex_service(walk=False)
        records = {11: (11, 1, 50, "/dev/pts/9", 50, "77"), 12: (12, 1, 60, "/dev/pts/8", 60, "88")}
        envs = {11: {"HERDR_PANE_ID": "w9:p9"}, 12: {"HERDR_PANE_ID": "w9:p8"}}
        real_listdir = os.listdir
        tui = {"pid": 11, "pid_start": "77", "session_id": SID}
        with mock.patch.object(pane_ownership.os, "listdir",
                               side_effect=lambda path: ["11", "12"] if path == "/proc" else real_listdir(path)), \
                mock.patch.object(pane_ownership, "codex_tui_processes_for_session", return_value=[tui]), \
                mock.patch.object(pane_ownership, "_runtime", return_value=("codex", False)), \
                mock.patch.object(pane_ownership, "_process", side_effect=lambda pid: records.get(int(pid))), \
                mock.patch.object(pane_ownership, "_native_session", return_value=SID), \
                mock.patch.object(pane_ownership, "_codex_identity_observation", return_value=None), \
                mock.patch.object(pane_ownership, "_shell_seat_pane", return_value=None), \
                mock.patch.object(procscan, "read_environ", side_effect=lambda pid: envs.get(int(pid), {})):
            self.assertIsNone(watch._steward_seat("codex", SID))  # the logging herdr refuses it
        self.assertEqual(self.calls(), ["pane process-info --pane w9:p9"])

    def test_another_threads_service_caller_has_no_seat(self):
        self.codex_service()
        os.environ["CODEX_THREAD_ID"] = "019a0000-0000-7000-8000-0000000000ee"
        with mock.patch.object(pane_ownership, "codex_tui_processes_for_session") as lookup:
            self.assertIsNone(watch._steward_seat("codex", SID))
        lookup.assert_not_called()


    # A Codex TUI behind the session shell is proven by its attachment, not by an environment.
    BINDING = {"harness": "codex", "sid": SID, "host_pid": 500, "host_start": "5",
               "broker_pid": 400, "broker_start": "4", "native_pid": 600, "native_start": "6"}

    def attached(self, *, tui_env=None, broker_env=None, joined="same", thread=SID,
                 current=SID, caller_descends=False, binding="one", user_tui=True):
        """Run `_steward_seat` for a non-service caller with only the attachment to go on."""
        socket = self.tmp / "tmux.sock"
        socket.touch()
        tmux = {"TMUX": f"{socket},1,0", "TMUX_PANE": "%7"}
        environs = {400: tmux if broker_env is None else broker_env, 500: {},
                    600: {} if tui_env is None else tui_env}
        if thread:
            os.environ["CODEX_THREAD_ID"] = thread
        stack = [
            mock.patch.object(session_shell, "exact_binding",
                              return_value=(dict(self.BINDING), "") if binding == "one"
                              else (None, "attachment-unavailable")),
            mock.patch.object(session_shell, "process",
                              side_effect=lambda pid: {"pid": pid, "start": "6", "parent": 1, "uid": 1}),
            mock.patch.object(session_shell, "_descends", return_value=caller_descends),
            mock.patch.object(session_shell, "pane_seat",
                              return_value=(dict(self.BINDING) if joined == "same"
                                            else dict(self.BINDING, native_pid=601) if joined == "other"
                                            else None)),
            mock.patch.object(pane_ownership, "_native_user_tui", return_value=user_tui),
            mock.patch.object(pane_ownership, "_pane_process_info",
                              return_value={"foreground_process_group_id": 400,
                                            "foreground_processes": [{"pid": 400}]}),
            mock.patch.object(pane_ownership, "_caller_runtime", return_value=(None, False)),
            mock.patch.object(pane_ownership, "codex_tui_processes_for_session", return_value=[]),
            mock.patch.object(pane_ownership, "codex_tui_process_bindings", return_value=[]),
            mock.patch.object(procscan, "read_environ", side_effect=lambda pid: environs.get(pid, {})),
            mock.patch("fleet.collectors.codex.session_id_of_process", return_value=current),
        ]
        for item in stack:
            item.start()
            self.addCleanup(item.stop)
        return watch._steward_seat("codex", SID), os.path.realpath(socket)

    def test_the_attachment_proves_the_seat_when_neither_caller_nor_tui_env_names_a_window(self):
        seat, socket = self.attached()
        self.assertEqual((seat or {}).get("kind"), "pane", seat)
        self.assertEqual(seat["pane"], f"%7@{socket}")
        self.assertEqual(self.calls(), [])

    def test_the_attachment_proves_a_caller_that_descends_from_its_tui(self):
        seat, socket = self.attached(thread="", caller_descends=True, tui_env={"TMUX_PANE": "%1"})
        self.assertEqual((seat or {}).get("pane"), f"%7@{socket}", seat)

    def test_an_unproven_attachment_gives_no_seat(self):
        for label, case in {
            "no attachment": dict(binding="none"),
            "pane foreground is another attachment": dict(joined="other"),
            "pane foreground is nobody's attachment": dict(joined="none"),
            "no process of the attachment names a tmux pane": dict(broker_env={}),
            "the TUI proves another thread now": dict(current="019a0000-0000-7000-8000-0000000000ee"),
            "the caller is another thread outside the TUI": dict(
                thread="019a0000-0000-7000-8000-0000000000ee"),
            "the attachment's process is a service": dict(user_tui=False),
        }.items():
            with self.subTest(label):
                with mock.patch.dict(os.environ, {}):
                    seat, _socket = self.attached(**case)
                self.assertIsNone(seat)
        self.assertEqual(self.calls(), [])


class SeatSharedWithTheGrantTest(unittest.TestCase):
    """`watch` and `wait` hand the seat they proved to `grant_steward` instead of proving it twice."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("ps_seat_under_test", HERE / "peer-steward.py")
        self.ps = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ps)

    def mark(self, **kw):
        with mock.patch.object(self.ps.peer_message, "grant_steward",
                               return_value={"status": "granted", "written": True}) as grant:
            self.ps._mark_observed("claude", "sid-target", "target", ("sid-steward", "codex"), **kw)
        return grant

    def test_the_seat_the_watch_proved_is_the_one_the_grant_uses(self):
        seat = {"kind": "pane", "key": "k1", "pane": "%1@/tmp/x/s"}
        with mock.patch.object(self.ps, "_steward_seat") as prove:
            grant = self.mark(steward_seat=seat)
        prove.assert_not_called()
        self.assertEqual(grant.call_args.kwargs["steward_seat"], seat)

    def test_wait_proves_it_through_the_same_local_proof(self):
        with mock.patch.object(self.ps, "_steward_seat", return_value=None) as prove:
            grant = self.mark()
        prove.assert_called_once_with("codex", "sid-steward")
        self.assertIsNone(grant.call_args.kwargs["steward_seat"])

    def test_a_given_seat_replaces_the_shared_resolver_in_the_grant(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="grant-seat-") as home, \
                mock.patch.dict(os.environ, {"HOME": home, "XDG_STATE_HOME": home + "/state"}), \
                mock.patch("dispatch_seat_handover.pane_seat") as shared, \
                mock.patch.object(self.ps.peer_message, "steward_marker_path",
                                  return_value=Path(home) / "marker.json"):
            self.ps.peer_message.grant_steward(
                "claude", "sid-steward", {"harness": "claude", "session_id": "sid-target"},
                "watch", "2026-11-12T00:00:00Z", source="watch", steward_seat=None)
            shared.assert_not_called()
            self.ps.peer_message.grant_steward(
                "claude", "sid-steward", {"harness": "claude", "session_id": "sid-target"},
                "watch", "2026-11-12T00:00:00Z", source="watch")
            shared.assert_called_once()


if __name__ == "__main__":
    unittest.main()
