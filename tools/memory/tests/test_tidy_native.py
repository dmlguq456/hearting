#!/usr/bin/env python3
"""Exact native background tidy transport, with all state in isolated fixtures."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT / "utilities")]
from tidy_isolation import isolated_env
import session_tidy as st
import session_tidy_clear as clear
import session_tidy_native as native
import pane_ownership

spec = importlib.util.spec_from_file_location("native_test_steward", ROOT / "utilities/peer-steward.py")
peer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer)


class NativeCase(unittest.TestCase):
    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        self.env = self.iso.patched_environ()
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        self.own = pane_ownership._process(os.getpid())
        self.target = {"pid": self.own[0], "start": self.own[5], "job": "1234abcd",
                       "home": str(self.iso.claude_dir), "binary": os.readlink(f"/proc/{self.own[0]}/exe")}
        self.registry = self.iso.claude_dir / "sessions" / f"{self.own[0]}.json"
        self.registry.parent.mkdir()
        self.record = {"pid": self.own[0], "procStart": self.own[5], "jobId": "1234abcd",
                       "kind": "bg", "sessionId": "session-A", "status": "idle"}
        self.publish()
        self.seat = st.Seat("native", st._digest("native", "claude", self.target["home"],
                                               self.target["job"], self.target["start"]),
                            harness="claude", native=self.target)
        self.cwd = str(self.iso.home)
        self.patch(native, "_caller_runtime", return_value=(self.own, False))
        self.patch(pane_ownership, "verified_pane", return_value="")

    def patch(self, obj, name, **kwargs):
        p = mock.patch.object(obj, name, **kwargs)
        result = p.start()
        self.addCleanup(p.stop)
        return result

    def publish(self, **fields):
        self.record.update(fields)
        self.registry.write_text(json.dumps(self.record))

    def book(self, *, continuing=False):
        st.record_event(self.seat, "claude", "session-A", "start", source="startup", cwd=self.cwd)
        st.write_card(self.seat, "claude", "session-A", "진행 중인 일: 임시 정리 시험")
        with mock.patch.object(clear, "_start_helper", return_value=os.getpid()), \
             mock.patch.object(clear, "herdr_command", return_value=None):
            self.assertEqual(clear.schedule_for_enqueue(self.seat, "claude", "session-A", self.cwd), "clear=scheduled")
        req = clear.read_reservation(self.seat.key)
        with st.seat_lock(self.seat.key):
            req["snapshot_state"] = "ready"  # the command tests start after helper snapshot preparation
            clear._write_reservation(req)
        path = clear.reservation_path(self.seat.key)
        if continuing:
            clear._finish(self.seat.key, req["nonce"], "cleared", observed="session-B")
            self.publish(sessionId="session-B")
            # The real start hook consumes the native seat's card.
            self.assertIn("진행 중인 일", st.run_hook("claude", "start", "session-B", source="clear", cwd=self.cwd))
            req = clear.read_reservation(self.seat.key)
        return req, path

    def book_compact(self):
        transcript = self.iso.root / "session-A.jsonl"
        rows = [
            {"type": "assistant", "uuid": "a-tool", "message": {"stop_reason": "tool_use",
             "content": [{"type": "text", "text": "tool call"}]}},
            {"type": "user", "uuid": "u-tool", "message": {"content": [
             {"type": "tool_result", "tool_use_id": "t", "content": "result"}]}},
        ]
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        info = transcript.stat()
        context = {"trigger": "auto", "identity": "native-compact", "transcript": str(transcript),
                   "boundary_offset": info.st_size,
                   "transcript_identity": f"{info.st_dev}:{info.st_ino}"}
        with st.seat_lock(self.seat.key):
            st.write_card(self.seat, "claude", "session-A", "complete compact summary", cwd=self.cwd,
                          prompt_seq=st.read_prompt_seq(self.seat), origin="runtime-compact",
                          compact_context=context)
        with mock.patch.object(clear, "_start_helper", return_value=os.getpid()):
            clear.schedule_for_enqueue(self.seat, "claude", "session-A", self.cwd, compact_context=context)
        req = clear.read_reservation(self.seat.key)
        with st.seat_lock(self.seat.key):
            req["snapshot_state"] = "ready"  # these are peer-command checks after compact evidence is ready
            clear._write_reservation(req)
        return req, clear.reservation_path(self.seat.key), transcript


class NativeIdentityTest(NativeCase):
    def test_inherited_pane_does_not_borrow_its_card(self):
        os.environ["HERDR_PANE_ID"] = "another:pane"
        seat = st.resolve_seat("claude", self.cwd, sid="session-A")
        self.assertEqual(seat, self.seat)
        self.assertEqual(seat.sid, "")
        self.assertEqual(st.session_identity(seat), ("claude", "session-A"))
        self.assertEqual(seat.pane, "")
        self.assertNotEqual(seat.key, st._digest("pane", "another:pane"))

    def test_other_jobs_and_restart_have_distinct_seats(self):
        a = st.resolve_seat("claude", self.cwd)
        self.publish(jobId="8765abcd")
        b = st.resolve_seat("claude", self.cwd)
        self.assertNotEqual(a.key, b.key)
        self.publish(procStart="different-start")
        self.assertEqual(st.resolve_seat("claude", self.cwd).kind, "project")

    def test_shared_daemon_and_other_harnesses_stay_project(self):
        with mock.patch.object(native, "_caller_runtime", return_value=(self.own, True)):
            self.assertIsNone(native.caller_target("claude"))
        for harness in ("codex", "opencode"):
            self.assertIsNone(native.caller_target(harness))
            self.assertEqual(st.resolve_seat(harness, self.cwd).kind, "project")

    def test_promoted_spare_needs_a_live_exact_background_declaration(self):
        with mock.patch.object(native, "_caller_runtime", return_value=(self.own, True)), \
             mock.patch.object(Path, "read_bytes", return_value=b"claude bg-spare\0--bg-spare\0claim.sock\0"):
            self.assertEqual(native.caller_target("claude"), self.target)
            self.publish(kind="spare")
            self.assertIsNone(native.caller_target("claude"))

    def test_native_process_titles_are_services_not_physical_pane_owners(self):
        for title in (b"claude bg-spare", b"claude bg-pty-host"):
            with mock.patch.object(Path, "read_bytes", return_value=title + b"\0--internal\0"):
                self.assertEqual(pane_ownership._runtime(self.own[0]), ("claude", True))

    def test_live_requires_pid_start_job_kind_and_no_symlink(self):
        for fields in ({"pid": 0}, {"procStart": "wrong"}, {"jobId": "wrong"},
                       {"kind": "foreground"}, {"sessionId": ""}):
            original = dict(self.record)
            self.publish(**fields)
            self.assertIsNone(native.live(self.target), fields)
            self.record = original
            self.publish()
        other = self.registry.with_suffix(".other")
        self.registry.rename(other)
        self.registry.symlink_to(other)
        self.assertIsNone(native.live(self.target))

    def test_pid_identity_is_rechecked_after_registry_read(self):
        with mock.patch.object(native, "_process", side_effect=[self.own, None]):
            self.assertIsNone(native.live(self.target))

    def test_ambient_old_id_is_replaced_but_explicit_foreign_id_is_refused(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "old-id"
        self.assertEqual(st.resolve_caller("claude", None, self.cwd)[2], "session-A")
        self.assertIsNone(st.resolve_caller("claude", "other-session", self.cwd))

    def test_worker_hooks_still_write_nothing(self):
        os.environ["AGENT_DISPATCH_DEPTH"] = "1"
        self.assertEqual(st.run_hook("claude", "start", "session-A", cwd=self.cwd), "")
        self.assertFalse(st._ledger_path(self.seat).exists())

    def test_native_card_survives_clear_and_is_delivered_once(self):
        req, _ = self.book()
        self.publish(sessionId="session-B")
        first = st.run_hook("claude", "start", "session-B", source="clear", cwd=self.cwd)
        self.assertIn("진행 중인 일", first)
        self.assertEqual(clear.read_reservation(self.seat.key)["observed"]["sid"], "session-B")
        self.assertNotIn("진행 중인 일", st.run_hook("claude", "prompt", "session-B", cwd=self.cwd))
        self.assertEqual(st.resolve_seat("claude", self.cwd).key, self.seat.key)


class NativeScreenTest(unittest.TestCase):
    def screen(self, body):
        snap = native.Snapshot()
        snap.feed("\x1b[2J\x1b[H" + body + "\r\n────────────────\r\n")
        return snap.lines()

    def test_faint_placeholder_is_empty_but_rgb_text_is_a_draft(self):
        self.assertIsNone(peer._screen_ready("claude", self.screen("❯ \x1b[2mSuggestion\x1b[22m")))
        self.assertEqual(peer._screen_ready("claude", self.screen("❯ \x1b[38;2;2;136;136mTyped draft")), "draft")
        self.assertEqual(peer._screen_ready("claude", self.screen("❯ \x1b[38;5;2mTyped draft")), "draft")

    def test_cursor_repaints_and_multiline_draft(self):
        self.assertEqual(peer._screen_ready("claude", self.screen("❯\r\n  second line")), "draft")
        lines = self.screen("❯ stale\x1b[3G\x1b[K")
        self.assertIsNone(peer._screen_ready("claude", lines))

    def test_unknown_controls_or_missing_repaint_are_refused(self):
        snap = native.Snapshot(); snap.feed("❯ ")
        self.assertIsNone(snap.lines())
        self.assertIsNone(self.screen("❯ \x1b[2S"))
        self.assertIsNone(self.screen("❯ \x1b[38:2:1:2:3m"))

    def test_form_guard_remains_shared(self):
        self.assertEqual(peer._screen_ready("claude", self.screen("Enter to confirm\r\n❯")), "form-open")

    def test_incomplete_new_frame_is_not_certified_by_an_older_finished_frame(self):
        fd, writer = os.pipe()
        self.addCleanup(os.close, fd)
        self.addCleanup(os.close, writer)
        os.write(writer, "\x1b[2J\x1b[H\x1b[?2026hAttaching…\x1b[?2026l\x1b[?2026h\x1b[H❯".encode())
        self.assertIsNone(native.View(fd).read(timeout=0.3))

    def test_same_view_reads_a_draft_arriving_after_initial_repaint(self):
        fd, writer = os.pipe()
        self.addCleanup(os.close, fd)
        self.addCleanup(os.close, writer)
        os.write(writer, "\x1b[?2026h\x1b[2J\x1b[H❯ \r\n────────────────\x1b[?2026l".encode())
        view = native.View(fd)
        self.assertIsNone(peer._screen_ready("claude", view.read(timeout=1)))
        os.write(writer, b"\x1b[?2026h\x1b[1;3Htyped\x1b[?2026l")
        self.assertEqual(peer._screen_ready("claude", view.read(timeout=1)), "draft")


class NativeCommandTest(NativeCase):
    def prepare_transport(self):
        proc = mock.Mock(); proc.poll.return_value = None
        @contextlib.contextmanager
        def attached(_target):
            yield 99, proc
        self.attach = self.patch(native, "attachment", side_effect=attached)
        self.view = self.patch(native.View, "read", return_value=[[("❯", False)], [("─", False)]])
        proxy = SimpleNamespace(**vars(os))
        self.write = proxy.write = mock.Mock()
        self.patch(native, "os", new=proxy)
        self.patch(native, "idle_reason", return_value="")
        return proc

    def run_command(self, req, path, *, continuing=False, ready=None):
        return native.command(req, path, req["nonce"], continuing=continuing,
                              screen_ready=ready or (lambda *_: None))

    def test_snapshot_pending_is_unsent_and_the_same_booking_can_clear_after_recovery(self):
        req, path = self.book(); self.prepare_transport()
        held = clear.read_reservation(self.seat.key)
        held["snapshot_state"] = "pending"
        clear._write_reservation(held)
        result = self.run_command(held, path)
        self.assertEqual((result["cleared"], result["reason"]), ("queued", "snapshot-pending"))
        self.write.assert_not_called()
        self.assertNotIn("clear_claim", clear.read_reservation(self.seat.key))
        held["snapshot_state"] = "ready"
        clear._write_reservation(held)
        def sent(fd, text):
            self.publish(sessionId="session-B")
            return len(text)
        self.write.side_effect = sent
        self.assertEqual(self.run_command(held, path)["cleared"], "true")
        self.write.assert_called_once()

    def test_clear_once_and_observe_exact_new_session(self):
        req, path = self.book(); self.prepare_transport()
        def sent(fd, text):
            self.assertEqual(text, b"/clear\r")
            self.publish(sessionId="session-B")
            return len(text)
        self.write.side_effect = sent
        result = self.run_command(req, path)
        self.assertEqual((result["cleared"], result["new_session"]), ("true", "session-B"))
        self.write.assert_called_once()

    def test_unreadable_source_after_clear_claim_keeps_claim_and_rechecks_screen_before_one_write(self):
        for source in ("card", "frontier"):
            with self.subTest(source=source):
                self.publish(sessionId="session-A")
                req, path = self.book(); self.prepare_transport()
                logical = st.session_seat(self.seat, "claude", "session-A")
                source_path = st.card_json_path(logical) if source == "card" else st._prompt_seq_path(logical)
                original = source_path.read_bytes() if source_path.exists() else None
                source_path.parent.mkdir(parents=True, exist_ok=True)
                reads, claims = [], []
                lines = [[("❯", False)], [("─", False)]]
                def read(*_args, **_kwargs):
                    reads.append(1)
                    if len(reads) == 2:
                        claims.append(clear.read_reservation(self.seat.key)["clear_claim"])
                        source_path.write_bytes(b'\xff')
                    return lines
                def restore(_delay):
                    self.write.assert_not_called()
                    held = clear.read_reservation(self.seat.key)
                    self.assertEqual(held["clear_claim"], claims[0])
                    self.assertEqual(held["nonce"], req["nonce"])
                    if original is None:
                        source_path.unlink()
                    else:
                        source_path.write_bytes(original)
                self.view.side_effect = read
                self.write.side_effect = lambda fd, text: (self.publish(sessionId="session-B"), len(text))[1]
                with mock.patch.object(native.time, "sleep", side_effect=restore):
                    result = self.run_command(req, path)
                self.assertEqual(result["cleared"], "true")
                self.write.assert_called_once_with(99, b"/clear\r")
                self.assertEqual(len(reads), 3)
                self.assertEqual(clear.read_reservation(self.seat.key)["clear_claim"], claims[0])

    def test_unreadable_card_after_continue_claim_retains_it_until_one_native_write(self):
        req, path = self.book(continuing=True); self.prepare_transport()
        logical = st.session_seat(self.seat, "claude", "session-A")
        card_path = st.card_json_path(logical)
        original = card_path.read_bytes()
        lines = [[("❯", False)], [("─", False)]]
        reads, claims = [], []
        def read(*_args, **_kwargs):
            reads.append(1)
            if len(reads) == 2:
                claims.append(clear.read_reservation(self.seat.key)["continued"])
                card_path.write_bytes(b'\xff')
            return lines
        def restore(_delay):
            self.write.assert_not_called()
            self.assertEqual(clear.read_reservation(self.seat.key)["continued"], claims[0])
            card_path.write_bytes(original)
        def sent(fd, text):
            st.run_hook("claude", "prompt", "session-B", cwd=self.cwd)
            return len(text)
        self.view.side_effect = read
        self.write.side_effect = sent
        with mock.patch.object(native.time, "sleep", side_effect=restore):
            result = self.run_command(req, path, continuing=True)
        self.assertEqual(result["continued"], "true")
        self.write.assert_called_once_with(99, (clear.CONTINUE_TEXT + "\r").encode())
        self.assertEqual(len(reads), 3)
        self.assertEqual(clear.read_reservation(self.seat.key)["continued"]["claim_id"], claims[0]["claim_id"])

    def test_unavailable_zero_frontier_and_io_errors_never_authorize_clear(self):
        req, path = self.book()
        logical = st.session_seat(self.seat, "claude", "session-A")
        frontier = st._prompt_seq_path(logical)
        frontier.parent.mkdir(parents=True, exist_ok=True)
        frontier.symlink_to(frontier.parent / "unavailable-target")
        self.assertEqual(clear.validate_request(path, req["nonce"])[1], "idle-card-observation-unavailable")
        frontier.unlink()
        with mock.patch.object(st, "read_latest_card", side_effect=PermissionError("unreadable")):
            self.assertEqual(clear.validate_request(path, req["nonce"])[1], "idle-card-observation-unavailable")
        self.assertIsNotNone(clear.validate_request(path, req["nonce"])[0])
        self.assertEqual(clear.read_reservation(self.seat.key)["status"], "reserved")

    def test_idle_card_request_uses_the_exact_attachment_and_claims_one_prompt(self):
        req, path = self.book()
        now = time.time()
        with st.seat_lock(self.seat.key):
            held = clear.read_reservation(self.seat.key)
            held.update(origin="idle", followup_kind="status-only", snapshot_state="ready",
                        idle={"identity": "idle-event", "due_at": now - 1, "tokens": 200000,
                              "min_tokens": 200000})
            clear._write_reservation(held)
        req = clear.read_reservation(self.seat.key)
        self.prepare_transport()

        def submitted(fd, data):
            self.assertEqual(fd, 99)
            self.assertIn("자동 유휴 정리 요청".encode("utf-8"), data)
            current = clear.read_reservation(self.seat.key)
            with st.seat_lock(self.seat.key):
                current["idle_request"] = {**current["idle_request"], "state": "arrived",
                                           "arrival": {"harness": "claude", "sid": "session-A",
                                                       "at": time.time(), "prompt_seq": 0}}
                clear._write_reservation(current)
            return len(data)

        self.write.side_effect = submitted
        result = native.idle_card_request(req, path, req["nonce"], screen_ready=lambda *_: None)
        self.assertEqual(result["accepted"], "true")
        self.write.assert_called_once()
        held = clear.read_reservation(self.seat.key)
        self.assertEqual(held["idle_request"]["state"], "sent")

    def test_idle_card_request_native_transport_uses_the_bound_job_once(self):
        req, path = self.book()
        now = time.time()
        with st.seat_lock(self.seat.key):
            held = clear.read_reservation(self.seat.key)
            held.update(origin="idle", followup_kind="status-only", snapshot_state="ready",
                        idle={"identity": "idle-native", "due_at": now - 1, "tokens": 200000,
                              "min_tokens": 200000})
            clear._write_reservation(held)
        req = clear.read_reservation(self.seat.key)
        self.prepare_transport()

        def submitted(fd, data):
            self.assertEqual(fd, 99)
            self.assertIn("자동 유휴 정리 요청".encode("utf-8"), data)
            current = clear.read_reservation(self.seat.key)
            with st.seat_lock(self.seat.key):
                current["idle_request"] = {**current["idle_request"], "state": "arrived",
                                           "arrival": {"harness": "claude", "sid": "session-A",
                                                       "at": time.time(), "prompt_seq": 0}}
                clear._write_reservation(current)
            return len(data)

        self.write.side_effect = submitted
        result = native.idle_card_request(req, path, req["nonce"], screen_ready=lambda *_: None)
        self.assertEqual(result["accepted"], "true")
        self.write.assert_called_once()
        self.assertEqual(clear.read_reservation(self.seat.key)["idle_request"]["state"], "sent")

    def test_draft_and_form_never_send(self):
        req, path = self.book(); self.prepare_transport()
        for reason in ("draft", "form-open", "draft-unknown"):
            self.assertEqual(self.run_command(req, path, ready=lambda *_, r=reason: r)["reason"], reason)
        self.write.assert_not_called()

    def test_new_prompt_or_superseded_card_never_sends(self):
        req, path = self.book(); self.prepare_transport()
        st.run_hook("claude", "prompt", "session-A", cwd=self.cwd)
        self.assertEqual(self.run_command(req, path)["reason"], "new-input")
        self.write.assert_not_called()

    def test_native_compact_never_sends_on_tool_result_then_sends_after_end_turn(self):
        req, path, transcript = self.book_compact()
        self.prepare_transport()
        pending = self.run_command(req, path)
        self.assertEqual(pending["reason"], "compact-pending")
        self.write.assert_not_called()
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "assistant", "uuid": "a-final",
                                     "message": {"stop_reason": "end_turn", "content": [
                                         {"type": "text", "text": "최종 응답"}]}}) + "\n")
        self.write.side_effect = lambda fd, text: (self.publish(sessionId="session-B"), len(text))[1]
        result = self.run_command(req, path)
        self.assertEqual((result["cleared"], result["new_session"]), ("true", "session-B"))
        self.write.assert_called_once()

    def test_native_compact_rechecks_transcript_after_claim_before_send(self):
        req, path, transcript = self.book_compact()
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "assistant", "uuid": "a-final",
                                     "message": {"stop_reason": "end_turn", "content": [
                                         {"type": "text", "text": "완료"}]}}) + "\n")
        self.prepare_transport()
        reads = [0]

        def screen(*_args, **_kwargs):
            reads[0] += 1
            if reads[0] == 2:
                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"type": "user", "uuid": "u-late", "message": {
                        "content": [{"type": "text", "text": "새 입력"}]}}) + "\n")
            return [[("❯", False)], [("─", False)]]

        self.view.side_effect = screen
        result = self.run_command(req, path)
        self.assertEqual(result["reason"], "compact-pending")
        self.write.assert_not_called()

    def test_changed_target_is_refused_before_attach(self):
        req, path = self.book()
        self.publish(jobId="another-job")
        with mock.patch.object(native, "attachment") as attach:
            self.assertEqual(self.run_command(req, path)["reason"], "target-changed")
            attach.assert_not_called()

    def test_partial_send_is_unverified_and_never_retried(self):
        req, path = self.book(); self.prepare_transport()
        self.write.return_value = 1
        self.assertEqual(self.run_command(req, path)["cleared"], "unverified")
        self.write.assert_called_once()

    def test_continue_once_after_real_card_receipt(self):
        req, path = self.book(continuing=True); self.prepare_transport()
        def sent(fd, text):
            self.assertEqual(text, (clear.CONTINUE_TEXT + "\r").encode())
            st.run_hook("claude", "prompt", "session-B", cwd=self.cwd)
            return len(text)
        self.write.side_effect = sent
        self.assertEqual(self.run_command(req, path, continuing=True)["continued"], "true")
        self.assertEqual(self.run_command(req, path, continuing=True)["reason"], "superseded")
        self.write.assert_called_once()

    def test_draft_after_continue_claim_releases_only_unsent_claim(self):
        req, path = self.book(continuing=True); self.prepare_transport()
        ready = mock.Mock(side_effect=[None, "draft"])
        self.assertEqual(self.run_command(req, path, continuing=True, ready=ready)["reason"], "draft")
        self.assertEqual(clear.read_reservation(self.seat.key)["continued"]["state"], "pending")
        self.write.assert_not_called()

    def test_another_clear_after_send_cannot_confirm_the_continue_prompt(self):
        req, path = self.book(continuing=True); self.prepare_transport()
        def sent(fd, text):
            self.publish(sessionId="session-C")
            st.run_hook("claude", "prompt", "session-C", cwd=self.cwd)
            return len(text)
        self.write.side_effect = sent
        self.assertEqual(self.run_command(req, path, continuing=True)["continued"], "unverified")
        self.write.assert_called_once()

    def test_prompt_during_final_screen_read_cancels_clear_and_continue(self):
        for continuing in (False, True):
            with self.subTest(continuing=continuing):
                req, path = self.book(continuing=continuing)
                self.prepare_transport()
                lines = [[("❯", False)], [("─", False)]]
                count = 0
                def read(*args, **kwargs):
                    nonlocal count
                    count += 1
                    if count == 2:
                        st.run_hook("claude", "prompt", "session-B" if continuing else "session-A", cwd=self.cwd)
                    return lines
                self.view.side_effect = read
                result = self.run_command(req, path, continuing=continuing)
                self.assertEqual(result["reason"], "new-input")
                self.write.assert_not_called()
                if continuing:
                    self.assertEqual(clear.read_reservation(self.seat.key)["continued"]["state"], "pending")

    def test_native_logical_handoff_does_not_acquire_physical_authority(self):
        import dispatch_seat_handover as handover
        with mock.patch.object(native, "attachment") as physical:
            row = handover.record_retire_handover("before", "claude", "session-A", "claude")
        self.assertEqual((row["from"], row["sid"], row["bindings"]), ("before", "session-A", []))
        physical.assert_not_called()


if __name__ == "__main__":
    unittest.main()
