"""Private, real-process identity and local-control checks; no operating TUI."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
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

    def test_a_broker_can_own_the_native_and_display_as_separate_children(self):
        row=self.binding();display=self.child()
        row.update(host_pid=display.pid,host_start=shell.process(display.pid)["start"],
                   broker_pid=os.getpid(),broker_start=shell.process(os.getpid())["start"])
        self.assertTrue(shell.valid_binding(row))
        self.assertFalse(shell.valid_binding({**row,"broker_start":row["broker_start"]+"0"}))
        self.assertFalse(shell.valid_binding({**row,"broker_pid":display.pid,
                                             "broker_start":row["host_start"]}))

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

    def test_optional_side_geometry_uses_the_existing_policy_and_validates_only_present_values(self):
        path=shell.policy_path();path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'enabled':True}));path.chmod(0o600)
        self.assertEqual(shell.display_policy(),{'layout':'side','side_width':40,'side_min_columns':100,'narrow':'bottom','auxiliary':'summary','bottom_rows':14,'position':'right','theme':'terminal','shade':'none','route_line':'time','rule_motion':'on'})
        path.write_text(json.dumps({'enabled':True,'layout':'side','side_width':48,'side_min_columns':150}))
        self.assertTrue(shell.policy()[0]);self.assertEqual(shell.display_policy()['side_width'],48)
        for field,value in (('layout','other'),('side_width',True),('side_width',-1),('side_min_columns','132')):
            path.write_text(json.dumps({'enabled':True,field:value}))
            self.assertFalse(shell.policy()[0]);self.assertEqual(shell.display_policy()['layout'],'side')

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

    def test_panel_request_uses_a_frozen_native_identity(self):
        row = self.binding(); self.save(row)
        received = self.serve(row) if hasattr(shell, "open_panel") else []
        open_panel = getattr(shell, "open_panel", lambda *_: {"ok": False})
        result = open_panel("claude", row["sid"])
        self.assertTrue(result["ok"])
        self.assertEqual(received[0]["op"], "panel")
        self.assertTrue(received[0]["expanded"])
        self.assertEqual(received[0]["sid"], row["sid"])

    def test_changed_reply_is_ambiguous_and_never_claimed_not_started(self):
        row = self.binding(); self.serve(row, change_reply=True)
        reply = shell.control(row, "panel", expanded=True)
        self.assertFalse(reply["ok"]); self.assertFalse(reply["not_started"])

    def test_native_start_registers_claude_and_codex_display_identity(self):
        import session_tidy
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness), mock.patch.object(shell, "lifecycle") as report:
                sid = "00000000-0000-4000-8000-000000000052"
                # Model a main TUI callback; the runner may itself be a worker.
                env = {"HEARTING_SHELL_SOCKET": str(self.root / "native.sock"),
                       "HEARTING_SHELL_TOKEN": "synthetic-private-control-token"}
                session_tidy.run_hook(harness, "start", sid, source="resume",
                                      cwd=str(self.root), env=env)
                report.assert_called_once_with(harness, "start", sid, env=env, source="resume")

    def test_hook_reports_proven_native_owner_instead_of_its_helper_process(self):
        row = self.binding(); received = self.serve(row)
        from pane_ownership import _process
        owner = _process(row["native_pid"])
        env = {"HEARTING_SHELL_SOCKET": row["socket"], "HEARTING_SHELL_TOKEN": "private-fixture",
               "HEARTING_SHELL_NATIVE_MODEL": "native-selected-model"}
        with mock.patch("pane_ownership._caller_runtime", return_value=(owner, False)):
            shell.lifecycle("claude", "start", row["sid"], env=env)
        self.assertEqual(received[0]["pid"], row["native_pid"])
        self.assertNotEqual(received[0]["pid"], os.getpid())
        self.assertEqual(received[0]["start"], row["native_start"])
        self.assertEqual(received[0]["native_model"], "native-selected-model")
        self.assertEqual(received[0]["sid"], row["sid"])
        self.assertNotIn("source", received[0])
        # Spec E9 rule 34 (f): a start carries the native's own start reason to the host.
        for event, want in (("start", "clear"), ("prompt", None)):
            os.unlink(row["socket"]); received = self.serve(row)
            with mock.patch("pane_ownership._caller_runtime", return_value=(owner, False)):
                shell.lifecycle("claude", event, row["sid"], env=env, source="clear")
            self.assertEqual(received[0].get("source"), want, event)

    def test_native_callback_can_register_before_the_passive_session_registry_exists(self):
        row = self.binding()
        from pane_ownership import _process
        owner = _process(row["native_pid"])
        env = {"HEARTING_SHELL_SOCKET": row["socket"], "HEARTING_SHELL_TOKEN": "private-fixture"}
        with mock.patch("pane_ownership._caller_runtime", return_value=(owner, False)), \
                mock.patch.object(shell.socket, "socket") as contact:
            shell.lifecycle("claude", "start", row["sid"], env=env)
            self.assertEqual(contact.call_count, 1)

    def test_unknown_service_and_worker_callbacks_do_not_contact_the_host(self):
        row = self.binding()
        env = {"HEARTING_SHELL_SOCKET": row["socket"], "HEARTING_SHELL_TOKEN": "private-fixture"}
        from pane_ownership import _process
        owner = _process(row["native_pid"])
        for value, extra in (((None, False), {}), ((owner, True), {}), ((owner, False), {"AGENT_SESSION_ROLE": "worker"})):
            with self.subTest(owner=value, env=extra), \
                    mock.patch("pane_ownership._caller_runtime", return_value=value), \
                    mock.patch.object(shell.socket, "socket") as contact:
                shell.lifecycle("claude", "stop", row["sid"], env={**env, **extra})
                contact.assert_not_called()

    def test_panel_and_collapsed_line_share_values_without_exposing_human_text_in_the_line(self):
        import human_wait_state, session_tidy
        from fleet import session_tags, session_view
        row = self.binding()
        observed = {"harness": "claude", "cwd": "/synthetic/hearting", "title": "Common title",
                    "branch": "feature", "ctx_pct": 37, "model": "claude-opus", "effort": "high", "weekly_pct": 60}
        with ExitStack() as patches:
            patches.enter_context(mock.patch.object(human_wait_state, "cached_statusline", return_value=((2, 1), "route 2")))
            patches.enter_context(mock.patch.object(human_wait_state, "cached_view", return_value={"items": [], "by_session": {("claude", row["sid"]): ["wait-one"]}}))
            # The panel counts the questions it can answer itself (11-14): two here.
            patches.enter_context(mock.patch.object(shell, "session_questions", return_value=[
                {"id": "S4:one", "answerable": True, "questions": []},
                {"id": "S4:two", "answerable": True, "questions": []}]))
            patches.enter_context(mock.patch.object(session_tidy, "prompt_display", return_value={"human_state": "known", "human": {"at": 123, "text": "A real person's private draft"}, "received": []}))
            patches.enter_context(mock.patch.object(session_tags, "assigned_tag", return_value="52"))
            patches.enter_context(mock.patch.object(session_view, "observe", return_value=(observed, [])))
            with mock.patch.object(shell.os, "getppid", return_value=os.getpid()):
                result = shell._backend_operation({"op": "projection", "binding": row, "columns": 240, "state": "idle"})
        text = "".join(segment["text"] for segment in result.get("segments", [{"text": result["footer"]}]))
        # Spec 2026-11-11 rule 10: the session title is the panel's, not the line's.
        from fleet.session_line import answer_wait_word   # the screen language's words
        fields = ("[52] hearting", "(feature)", "claude-opus high", "context ━━━━────── 37%", answer_wait_word() + " 2", "route 2", "7d 60%")
        for field in fields:
            self.assertIn(field, text)
        self.assertEqual([text.index(field) for field in fields], sorted(text.index(field) for field in fields))
        self.assertNotIn("private draft", text)
        self.assertTrue(any("private draft" in "".join(part["text"] for part in line) for line in result["lines"]))

    def test_a_broken_byte_in_the_memory_journal_does_not_stop_the_panel(self):
        # Every panel reads the one shared memory journal; a half-written line there costs
        # only that line: the projection answers and MEM shows the newest whole row.
        shell.registry_root()   # puts `tools` on the path
        import human_wait_state, session_tidy
        from fleet import session_tags, session_view
        from fleet.collectors import memory
        row = self.binding()
        observed = {"harness": "claude", "cwd": "/synthetic/hearting", "title": "Common title"}
        line = lambda ts, text, rid: json.dumps({"ts": ts, "action": "add", "id": rid, "type": "decision",
                                                 "snippet": text, "cwd": "/synthetic/hearting"}, ensure_ascii=False).encode()
        older, newer = line("2026-11-12T09:00:00", "앞 기록", "a"), line("2026-11-12T10:00:00", "뒤 기록", "b")
        cut = newer[:newer.index("뒤".encode()) + 1]
        journal = self.root / "memory" / "write-events.jsonl"
        journal.parent.mkdir()
        with ExitStack() as patches:
            patches.enter_context(mock.patch.dict(os.environ, {"MEM_STORE": str(journal.parent), "MEM_WRITE_EVENTS": str(journal)}))
            patches.enter_context(mock.patch.object(human_wait_state, "cached_statusline", return_value=(None, "")))
            patches.enter_context(mock.patch.object(human_wait_state, "cached_view", return_value=None))
            patches.enter_context(mock.patch.object(session_tidy, "prompt_display", return_value={"human_state": "none"}))
            patches.enter_context(mock.patch.object(session_tags, "assigned_tag", return_value="52"))
            patches.enter_context(mock.patch.object(session_view, "observe", return_value=(observed, [])))
            patches.enter_context(mock.patch.object(shell.os, "getppid", return_value=os.getpid()))
            for name, data, shown in (("the last line cut inside a character", older + b"\n" + cut, "앞 기록"),
                                      ("a broken byte mid-journal", older + b"\n\xff" + cut + b"\n" + newer + b"\n", "뒤 기록")):
                with self.subTest(name):
                    journal.write_bytes(data)
                    memory._latest_cache.clear()
                    result = shell._backend_operation({"op": "projection", "binding": row, "columns": 100, "state": "idle"})
                    self.assertTrue(result.get("ok"), result)
                    self.assertIn(str(journal), result["watch"])
                    self.assertIn(shown, "".join(part["text"] for part in result["panel"]["memory"]))

    def test_an_unobserved_question_projection_does_not_become_zero_waits(self):
        import human_wait_state, session_tidy
        with mock.patch.object(human_wait_state, "cached_statusline", return_value=(None, "")), \
                mock.patch.object(human_wait_state, "cached_view", return_value=None), \
                mock.patch.object(session_tidy, "prompt_display", return_value={"human_state": "none"}):
            result = shell.panel_projection("claude", "test-native-session")
        text = "".join(segment["text"] for segment in result.get("segments", [{"text": result["footer"]}]))
        from fleet.session_line import answer_wait_word
        self.assertIn(answer_wait_word() + " ?", text)
        self.assertNotIn(answer_wait_word() + " 0", result["footer"])

    def test_readiness_requires_the_exact_native_idle_record_and_known_empty_screen(self):
        from fleet import session_registry
        row = self.binding()
        native = {"pid": row["native_pid"], "procStart": row["native_start"], "sessionId": row["sid"], "status": "idle"}
        screen = [[["❯ ", False]], [["──────────────────", False]]]
        with mock.patch.object(session_registry, "read", return_value=native):
            self.assertTrue(shell._input_readiness(row, {"screen": screen})["ready"])
            self.assertFalse(shell._input_readiness(row, {"screen": [[["unrecognized UI", False]]]})["ready"])
        for observed in (None, {**native, "procStart": native["procStart"] + "0"},
                         {**native, "sessionId": "another-native-session"}, {**native, "status": "busy"}):
            with self.subTest(native=observed), mock.patch.object(session_registry, "read", return_value=observed):
                self.assertFalse(shell._input_readiness(row, {"screen": screen, "state": "idle"})["ready"])

    def test_the_backend_begins_the_existing_pending_write_at_its_final_admission(self):
        peer = shell._peer_message(); binding = self.binding(); root = self.root / "peer-state"
        recipient = {"harness": "claude", "session_id": binding["sid"]}
        with mock.patch.object(peer, "peer_state_root", return_value=root), \
                mock.patch.object(peer, "steward_marker_roots", return_value=([root], "fixture")), \
                mock.patch.object(shell.os, "getppid", return_value=os.getpid()), \
                mock.patch.object(shell, "_input_readiness", return_value={"ok": True, "ready": True}):
            _text, ref = peer.prepare_peer_message("queued-contract-fixture",
                {"harness": "codex", "session_id": "synthetic-sender"}, recipient, defer=True)
            reserved = peer.reserve_pending_herdr(ref, recipient, receipt="session-shell-inflight")
            self.assertIsNotNone(reserved)
            self.assertEqual(peer._read_pending(ref)["state"], "pending")
            result = shell._backend_operation({"op": "payload", "binding": binding,
                                               "ref": ref, "claim": reserved["rpc_claim"]})
            self.assertTrue(result["ok"], result)
            self.assertEqual(peer._read_pending(ref)["state"], "unverified")

    def test_the_sender_keeps_a_reservation_pending_until_native_admission(self):
        peer = shell._peer_message(); binding = self.binding(); root = self.root / "peer-state"
        recipient = {"harness": "claude", "session_id": binding["sid"]}; observed = []
        with mock.patch.object(peer, "peer_state_root", return_value=root), \
                mock.patch.object(peer, "steward_marker_roots", return_value=([root], "fixture")), \
                mock.patch.object(shell, "exact_binding", return_value=(binding, "")):
            _text, ref = peer.prepare_peer_message("sender-contract-fixture",
                {"harness": "codex", "session_id": "synthetic-sender"}, recipient, defer=True)
            def native_control(_binding, operation, **_fields):
                if operation == "status":return {"ok": True, "state": "idle", "empty": True, "epoch": 0}
                observed.append(peer._read_pending(ref)["state"])
                return {"ok": False, "not_started": True, "reason": "input-changed"}
            with mock.patch.object(shell, "control", side_effect=native_control):
                result = shell.deliver_pending(ref, "claude", binding["sid"])
            self.assertTrue(result["not_started"])
            self.assertEqual(observed, ["pending"])
            row = peer._read_pending(ref)
            self.assertEqual(row["state"], "pending");self.assertIsNone(row["rpc_claim"])

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


class FableQuotaTimelineTest(unittest.TestCase):
    """Review 1597-r2 (11-13): Fable's model quota beside Claude's callback is shown only while
    the account cache is fresh (180 s). Fleet refreshed that cache only once it was stale, so
    the value went missing until the reply landed, and without Fleet nothing refreshed it at
    all. The panel's own projection (what the backend computes, `_observe_session`) is followed
    for 600 s on a fake clock with a fake fetcher; no account and no network."""

    BASE = 1_800_000_000.0

    def setUp(self):
        import queue
        shell.registry_root()  # the backend's own import path for Fleet's readers
        from fleet import session_view, gitinfo
        from fleet.collectors import procscan, usage_api
        self.view, self.queue = session_view, queue
        self.cache = session_view.usage_cache
        tmp = tempfile.TemporaryDirectory(prefix="fable-timeline-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"HOME": str(root), "CLAUDE_CONFIG_DIR": str(root / "claude"),
            "XDG_STATE_HOME": str(root / "state"), "XDG_CACHE_HOME": str(root / "cache"),
            "FLEET_USAGE_STATE_DIR": str(root / "usage"), "FLEET_TITLE_STATE_DIR": str(root / "titles")})
        env.start(); self.addCleanup(env.stop)
        git = mock.patch.object(gitinfo, "cached_display", return_value=None)
        git.start(); self.addCleanup(git.stop)
        (root / "claude").mkdir()
        (root / "claude/.credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "synthetic-timeline"}}))
        self.sid, self.pid = "00000000-0000-4000-8000-000000001597", os.getpid()
        self.start = procscan.read_proc_start(self.pid)
        tap = root / "claude/.statusline" / (self.sid + ".json")
        tap.parent.mkdir(parents=True)
        tap.write_text(json.dumps({"session_id": self.sid, "pid": self.pid, "proc_start": self.start,
            "rate_limits": {"five_hour": {"used_percentage": 11}, "seven_day": {"used_percentage": 22}}}))
        self.generation = usage_api._credential().generation
        self.assertTrue(self.generation)
        shell._LAST_OBSERVED.clear(); self.addCleanup(shell._LAST_OBSERVED.clear)

    def timeline(self, *, fleet, cadence, phase=0, fail=False, latency=1, horizon=600):
        """Each projection's last row and freshness, and each fetch's start, in seconds."""
        from fleet import session_panel
        clock, fetches, entered, active = [self.BASE], [], self.queue.Queue(), []
        for path in Path(self.cache.state_dir()).iterdir() if Path(self.cache.state_dir()).is_dir() else ():
            path.unlink()
        def fetch(_credential):
            fetches.append(clock[0] - self.BASE)
            active.append(1); self.peak = max(getattr(self, "peak", 0), len(active))
            done = threading.Event(); entered.put(done)
            done.wait(10); active.pop()
            if fail:
                return {"error": "timeout", "reason": "usage-timeout"}
            return {"rl_5h": 99, "rl_7d": 99, "rl_ms": [["fable", 70 + len(fetches)]]}
        self.cache._write("claude", {"rl_5h": 99, "rl_7d": 99, "rl_ms": [["fable", 70]]},
                          self.BASE, self.BASE, credential_generation=self.generation)
        rows, flight = [], None
        def started(t):
            thread = self.cache._THREADS.get("claude")
            if thread is not None and flight is None:
                return (t, thread, entered.get(timeout=10))
            return flight
        with mock.patch("time.time", lambda: clock[0]), \
             mock.patch.dict(self.cache.FETCHERS, {"claude": fetch}):
            try:
                for t in range(horizon + 1):
                    clock[0] = self.BASE + t
                    landed = False
                    if flight is not None and t >= flight[0] + latency:
                        flight[2].set(); flight[1].join(10)
                        self.assertFalse(flight[1].is_alive())
                        flight, landed = None, True
                    if fleet and t % 2 == 0:
                        # Fleet's collection, every two seconds.
                        self.cache.account_usage("claude", usage="refresh")
                        flight = started(t)
                    # The panel projects on its cadence and when the watched cache is rewritten.
                    if t % cadence == phase or landed:
                        values, _ = shell._observe_session(self.view.observe, "claude", self.sid, self.pid, self.start)
                        flight = started(t)
                        text = "".join(part for part, _key in session_panel.quota_row(values))
                        rows.append((t, text, self.cache.read("claude")["freshness"]))
            finally:
                while not entered.empty():
                    entered.get().set()
                if flight is not None:
                    flight[2].set()
                for thread in list(self.cache._THREADS.values()):
                    thread.join(10)
        self.assertLessEqual(getattr(self, "peak", 0), 1, "a second fetch ran beside the first")
        return rows, fetches

    def assert_shown_only_fresh(self, rows):
        for t, text, freshness in rows:
            if "fable" in text:
                self.assertEqual(freshness, "fresh", (t, text))

    def assert_spacing(self, fetches, least):
        self.assertEqual(fetches, sorted(fetches))
        for earlier, later in zip(fetches, fetches[1:]):
            self.assertGreaterEqual(later - earlier, least, fetches)

    def test_with_fleet_running_fable_never_leaves_the_last_row_in_600_seconds(self):
        rows, fetches = self.timeline(fleet=True, cadence=2)
        missing = [(t, text) for t, text, _ in rows if "fable" not in text]
        self.assertEqual(missing, [])
        self.assert_shown_only_fresh(rows)
        # The existing single flight and spacing: one fetch at a time, never more often than
        # two thirds of the fresh window, so at most 600 / 120 = 5 in 600 s.
        self.assert_spacing(fetches, self.cache.FRESH_WINDOWS["claude"] * 2 / 3)
        self.assertLessEqual(len(fetches), 5)

    def test_with_only_the_shell_its_projection_wakes_the_refresh_and_fable_stays(self):
        # Without Fleet nothing else refreshes this cache. The statusline callback repaints the
        # panel at least every 60 s; the projection between repaints also follows cache writes.
        for cadence, phase in ((60, 0), (60, 30), (60, 59), (2, 0)):
            with self.subTest(cadence=cadence, phase=phase):
                rows, fetches = self.timeline(fleet=False, cadence=cadence, phase=phase)
                self.assertEqual([(t, text) for t, text, _ in rows if "fable" not in text], [])
                self.assertGreater(rows[-1][0], 540)
                self.assert_shown_only_fresh(rows)
                self.assert_spacing(fetches, self.cache.FRESH_WINDOWS["claude"] * 2 / 3)
                self.assertLessEqual(len(fetches), 5)

    def test_a_refresh_that_keeps_failing_hides_fable_once_the_window_ends(self):
        # The old value is never shown as now: past 180 s it leaves, with or without Fleet, and
        # the failed attempts keep the existing retry spacing (60 s, at most 10 in 600 s).
        for fleet, cadence in ((True, 2), (False, 60), (False, 2)):
            with self.subTest(fleet=fleet, cadence=cadence):
                rows, fetches = self.timeline(fleet=fleet, cadence=cadence, fail=True)
                self.assert_shown_only_fresh(rows)
                self.assertTrue(all("fable" in text for t, text, _ in rows if t <= 180), rows)
                self.assertTrue(all("fable" not in text and text.startswith("5h 11% │ 7d 22%")
                                    for t, text, _ in rows if t > 180), rows)
                self.assert_spacing(fetches, self.cache.RETRY_AFTER)
                self.assertLessEqual(len(fetches), 600 // self.cache.RETRY_AFTER)
                self.assertTrue(fetches)


if __name__ == "__main__":
    unittest.main()
