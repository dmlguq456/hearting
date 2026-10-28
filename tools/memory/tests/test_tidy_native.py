#!/usr/bin/env python3
"""Exact native background tidy transport, with all state in isolated fixtures."""
import contextlib
import importlib.util
import json
import os
import shutil
from pathlib import Path
import sys
import textwrap
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

    def reset_tidy(self):
        root = st.state_root()
        self.assertIn(self.iso.root, root.parents)
        shutil.rmtree(root, ignore_errors=True)

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

    def test_continue_uses_one_strict_card_observation_and_recovers_before_or_after_claim(self):
        for boundary in ("before-claim", "after-claim"):
            with self.subTest(boundary=boundary):
                self.reset_tidy()
                self.publish(sessionId="session-A")
                req, path = self.book(continuing=True)
                self.prepare_transport()
                card_path = st.card_json_path(st.session_seat(self.seat, "claude", "session-A"))
                original = card_path.read_bytes()
                real_read = st.read_latest_card
                lost, waits, claims = [], [], []
                def read(seat):
                    card = real_read(seat)
                    held = clear.read_reservation(self.seat.key)
                    sending = (held.get("continued") or {}).get("state") == "sending"
                    if card and not lost and sending == (boundary == "after-claim"):
                        lost.append(1)
                        claims.append(held.get("continued"))
                        card_path.write_bytes(b'\xff')
                    return card
                def restore(_delay):
                    waits.append(1)
                    self.write.assert_not_called()
                    self.assertEqual(clear.read_reservation(self.seat.key)["nonce"], req["nonce"])
                    if boundary == "after-claim":
                        self.assertEqual(clear.read_reservation(self.seat.key)["continued"], claims[0])
                    card_path.write_bytes(original)
                def sent(_fd, data):
                    st.run_hook("claude", "prompt", "session-B", cwd=self.cwd)
                    return len(data)
                self.write.side_effect = sent
                def continuer(path, nonce, _pane):
                    held, reason = clear.validate_continue(path, nonce)
                    if not held:
                        return {"continued": "skipped", "reason": reason}
                    return native.command(held, path, nonce, continuing=True, screen_ready=lambda *_: None)
                with mock.patch.object(st, "read_latest_card", side_effect=read), \
                     mock.patch.object(native.time, "sleep", side_effect=restore):
                    self.assertEqual(clear._continue(self.seat.key, req["nonce"], continuer, sleep=restore), "continued")
                self.assertEqual(len(waits), 1)
                self.write.assert_called_once()
                held = clear.read_reservation(self.seat.key)
                self.assertEqual(held["continued"]["state"], "sent")
                if boundary == "after-claim":
                    self.assertEqual(held["continued"]["claim_id"], claims[0]["claim_id"])
                self.assertEqual(held["nonce"], req["nonce"])

    def test_continue_strict_observation_preserves_absence_replacement_handoff_and_input_protections(self):
        for change in ("absent", "replaced", "handed-off", "new-input", "unreadable-expiry"):
            with self.subTest(change=change):
                self.reset_tidy()
                self.publish(sessionId="session-A")
                req, path = self.book(continuing=True)
                self.prepare_transport()
                booked = clear._booked_seat(req, self.seat.key)
                card_path = st.card_json_path(booked)
                if change == "absent":
                    card_path.unlink()
                    reason = "card-changed"
                elif change in {"replaced", "handed-off"}:
                    card = st.read_latest_card(booked)
                    if change == "replaced": card["generation"] += 1
                    else: card["handed_off"] = True
                    st.atomic_write_json(card_path, card)
                    reason = "card-changed" if change == "replaced" else "handed-off"
                elif change == "new-input":
                    st.bump_prompt_seq(booked, "claude", "session-B", st.now_epoch())
                    reason = "new-input"
                else:
                    card_path.write_bytes(b'\xff')
                    reason = "expired"
                now = st.now_epoch()
                clock = [now]
                def advance(delay): clock[0] += delay
                with mock.patch.object(st, "now_epoch", side_effect=lambda: clock[0]):
                    self.assertEqual(clear._continue(self.seat.key, req["nonce"], mock.Mock(), sleep=advance), "cleared")
                held = clear.read_reservation(self.seat.key)
                self.assertEqual((held["continued"]["state"], held["continued"]["reason"]), ("skipped", reason))
                self.assertEqual(held["continued"]["deadline"], req["continued"]["deadline"])
                self.write.assert_not_called()

    def test_continue_verified_absence_is_no_card_when_no_generation_was_booked(self):
        req, path = self.book(continuing=True)
        card_path = st.card_json_path(clear._booked_seat(req, self.seat.key))
        card_path.unlink()
        req["card_generation"] = 0
        clear._write_reservation(req)
        self.assertEqual(clear.validate_continue(path, req["nonce"])[1], "no-card")

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


class NativeRPCWaitTest(NativeCase):
    """Actual helper -> subprocess -> steward -> native command, with private I/O."""

    CHILD = r'''
import contextlib, importlib.util, json, os, signal, sys, time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import session_tidy as st
import session_tidy_clear as clear
import session_tidy_native as native
config = json.loads(Path(__file__).with_suffix('.json').read_text())
spec = importlib.util.spec_from_file_location('rpc_steward', config['steward'])
peer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer)
args = peer.build_parser().parse_args(sys.argv[1:])
req, seat, reason = clear._booking_at(Path(args.request))
assert req is not None, reason
continuing = sys.argv[1] == 'continue'
source = st.card_json_path(seat)
original = source.read_bytes()
events = Path(config['events'])
def emit(kind, **data):
    with events.open('a') as stream:
        stream.write(json.dumps({'event': kind, 'pid': os.getpid(), **data}) + '\n')
claim = None
reads = 0
started = None
restored = False
real_sleep = time.sleep
def held_claim():
    held = clear.read_reservation(req['seat']['key'])
    return held.get('continued' if continuing else 'clear_claim')
@contextlib.contextmanager
def attached(_target):
    try: yield 99, SimpleNamespace(poll=lambda: None)
    finally: emit('attachment-closed')
def read(*_args, **_kwargs):
    global reads, claim, started
    reads += 1
    if reads == 2:
        claim = held_claim()
        assert claim['state'] == 'sending'
        emit('claim', claim=claim, nonce=req['nonce'])
        source.write_bytes(b'\xff')
        started = time.monotonic()
    emit('screen', count=reads)
    if reads > 2 and config['scenario'] == 'draft':
        return [[('❯', False), ('x', False)], [('─', False)] * 16]
    if reads > 2 and config['scenario'] == 'form':
        return [[(c, False) for c in 'Enter to confirm'], [('❯', False)], [('─', False)] * 16]
    return [[('❯', False)], [('─', False)] * 16]
def sleep(delay):
    global restored
    assert held_claim() == claim
    real_sleep(min(delay, .04))
    if not restored and time.monotonic() - started >= config['outage_sec']:
        restored = True
        scenario = config['scenario']
        emit('wait-passed-fixed-timeout', seconds=time.monotonic()-started)
        if scenario == 'expiry': return
        source.write_bytes(original)
        if scenario == 'new-input':
            st.bump_prompt_seq(seat, 'claude', req.get('new_session') or req['sid'], st.now_epoch())
        elif scenario == 'supersede':
            held = clear.read_reservation(req['seat']['key'])
            held['nonce'] = 'replacement-nonce'
            clear._write_reservation(held)
        elif scenario == 'death-unsent':
            os.kill(os.getpid(), signal.SIGTERM)
        elif scenario in {'target-changed', 'process-changed'}:
            registry = Path(config['registry'])
            record = json.loads(registry.read_text())
            record['sessionId' if scenario == 'target-changed' else 'procStart'] = 'actual-other'
            registry.write_text(json.dumps(record))
        emit('restored', claim=held_claim())
def write(fd, data):
    assert held_claim() == claim
    emit('write', text=data.decode(), claim=claim)
    scenario = config['scenario']
    if scenario == 'death-after-write': os.kill(os.getpid(), signal.SIGTERM)
    if scenario == 'timeout-after-write': real_sleep(10)
    if scenario == 'partial': return len(data)-1
    if scenario == 'write-error': raise OSError('ambiguous-write')
    if continuing:
        st.bump_prompt_seq(st.seat_from_dict(req['seat']), 'claude', req['new_session'], st.now_epoch())
    else:
        registry = Path(config['registry'])
        record = json.loads(registry.read_text())
        record['sessionId'] = 'session-B'
        registry.write_text(json.dumps(record))
    return len(data)
proxy = SimpleNamespace(**vars(os)); proxy.write = write
with mock.patch.object(native, 'attachment', side_effect=attached), \
     mock.patch.object(native.View, 'read', side_effect=read), \
     mock.patch.object(native.time, 'sleep', side_effect=sleep), \
     mock.patch.object(native, 'os', proxy), \
     mock.patch.object(peer, '_record'), mock.patch.object(peer, '_project_of', return_value='fixture'):
    code = peer.cmd_continue(args) if continuing else peer.cmd_clear(args)
emit('finished', claim=held_claim())
raise SystemExit(code)
'''

    def probe(self, *, mode="ordinary", scenario="recovery"):
        continuing = mode == "continue"
        self.reset_tidy()
        self.publish(sessionId="session-A")
        req, path = self.book(continuing=continuing)
        if mode == "compact":
            req, path, transcript = self.book_compact()
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "assistant", "uuid": "a-final",
                    "message": {"stop_reason": "end_turn", "content": [
                        {"type": "text", "text": "complete"}]}}) + "\n")
        # Use the actual original reservation/window, scaled to keep the probe short.
        deadline = st.now_epoch() + 3.0
        if continuing:
            req["deadline"] = st.now_epoch() - 10  # Only continuation's own window applies.
            req["continued"]["deadline"] = deadline
        else:
            req["deadline"] = deadline
            req["continue_off"] = True
        clear._write_reservation(req)
        import dispatch_seat_handover as handover
        with st.seat_lock(self.seat.key):
            handover.write_snapshot_locked(self.seat, "claude", "session-A")
        tag = mode + "-" + scenario
        script = self.iso.root / (tag + ".py")
        events_path = self.iso.root / (tag + ".jsonl")
        script.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(ROOT / 'utilities')!r})\n"
                          + textwrap.dedent(self.CHILD))
        compile(script.read_text(), str(script), "exec")
        script.chmod(0o700)
        script.with_suffix(".json").write_text(json.dumps({"scenario": scenario,
            "steward": str(ROOT / "utilities/peer-steward.py"), "events": str(events_path),
            "registry": str(self.registry), "outage_sec": 1.0}))
        runs = []
        real_run = clear.subprocess.run
        def capture(*args, **kwargs):
            try:
                done = real_run(*args, **kwargs)
            except clear.subprocess.TimeoutExpired:
                runs.append({"timeout": kwargs.get("timeout"), "timed_out": True})
                raise
            runs.append({"code": done.returncode, "stdout": done.stdout, "stderr": done.stderr,
                         "timeout": kwargs.get("timeout")})
            return done
        with mock.patch.dict(os.environ, {"HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                                         "HEARTING_TIDY_PEER_STEWARD": str(script),
                                         "HEARTING_TIDY_CLEAR_OBSERVE": "0.01"}), \
             mock.patch.object(clear, "STEWARD_TIMEOUT_SEC", .75), \
             mock.patch.object(clear.subprocess, "run", side_effect=capture), \
             mock.patch.object(native, "wait_idle", return_value="idle"):
            if continuing:
                outcome = clear._continue(self.seat.key, req["nonce"], clear._run_continue)
            else:
                outcome = clear.run_helper(self.seat.key, req["nonce"])
        events = [json.loads(line) for line in events_path.read_text().splitlines()] if events_path.exists() else []
        self.assertEqual(len([e for e in events if e["event"] == "claim"]), 1, (events, runs, outcome, clear.read_reservation(self.seat.key)))
        self.assertEqual(len([e for e in events if e["event"] == "wait-passed-fixed-timeout"]), 1, events)
        for pid in {e["pid"] for e in events}:
            self.assertFalse(Path(f"/proc/{pid}").exists(), "RPC child must be reaped before fixture cleanup")
        held = clear.read_reservation(self.seat.key)
        window = held["continued"] if continuing else held
        self.assertEqual(window["deadline"], deadline)
        writes = [e for e in events if e["event"] == "write"]
        print("native-rpc-probe=" + json.dumps({"mode": mode, "scenario": scenario,
            "outcome": outcome, "nonce": req["nonce"], "deadline": deadline,
            "claim": next(e["claim"] for e in events if e["event"] == "claim"),
            "writes": writes, "events": [e["event"] for e in events], "rpc": runs,
            "owned_children_reaped": True}, ensure_ascii=False))
        return outcome, held, events, writes, req

    def test_rpc_outage_longer_than_fixed_timeout_recovers_on_original_clear_compact_and_continue_window(self):
        for mode in ("ordinary", "compact", "continue"):
            with self.subTest(mode=mode):
                outcome, held, events, writes, req = self.probe(mode=mode)
                self.assertEqual(outcome, "continued" if mode == "continue" else "cleared", events)
                self.assertEqual(len(writes), 1)
                self.assertEqual(held["nonce"], req["nonce"])
                claim = next(e["claim"] for e in events if e["event"] == "claim")
                final = held["continued"] if mode == "continue" else held["clear_claim"]
                self.assertEqual(final["token"], claim["token"])
                self.assertEqual(final["state"], "sent")
                self.assertGreaterEqual(len([e for e in events if e["event"] == "screen"]), 3)
                self.assertEqual(events[-2]["event"], "attachment-closed")

    def test_rpc_original_deadline_expiry_supersession_input_and_recovered_draft_never_send(self):
        for mode in ("ordinary", "compact", "continue"):
            for scenario in ("expiry", "supersede", "new-input", "draft"):
                with self.subTest(mode=mode, scenario=scenario):
                    outcome, held, events, writes, req = self.probe(mode=mode, scenario=scenario)
                    self.assertFalse(writes)
                    if scenario == "supersede":
                        self.assertEqual(held["nonce"], "replacement-nonce")
                    else:
                        final = held["continued"] if mode == "continue" else held
                        self.assertEqual(final["reason"], {"expiry": "expired", "new-input": "new-input", "draft": "draft"}[scenario])

    def test_rpc_process_death_and_ambiguous_writes_never_authorize_a_second_send(self):
        for mode in ("ordinary", "continue"):
            for scenario in ("death-unsent", "death-after-write", "timeout-after-write", "partial", "write-error"):
                with self.subTest(mode=mode, scenario=scenario):
                    outcome, held, events, writes, req = self.probe(mode=mode, scenario=scenario)
                    self.assertEqual(len(writes), 0 if scenario == "death-unsent" else 1)
                    final = held["continued"] if mode == "continue" else held
                    if scenario in ("partial", "write-error"):
                        self.assertEqual((final["state"] if mode == "continue" else final["status"], final["reason"]),
                                         ("unverified", "partial-send"))
                    with mock.patch.object(clear, "_ask_steward") as again:
                        if mode == "continue": clear._continue(self.seat.key, req["nonce"], clear._run_continue)
                        else: clear.run_helper(self.seat.key, req["nonce"])
                    again.assert_not_called()

    def test_rpc_recovery_rechecks_native_session_process_and_form_before_write(self):
        for mode in ("ordinary", "continue"):
            for scenario in ("target-changed", "process-changed", "form"):
                with self.subTest(mode=mode, scenario=scenario):
                    # Each case starts with the same exact private PID/start record.
                    self.publish(procStart=self.own[5])
                    outcome, held, events, writes, req = self.probe(mode=mode, scenario=scenario)
                    self.assertFalse(writes)
                    final = held["continued"] if mode == "continue" else held
                    self.assertEqual(final["reason"], "form-open" if scenario == "form" else "target-changed")
                    self.assertGreaterEqual(len([e for e in events if e["event"] == "screen"]), 3)


if __name__ == "__main__":
    unittest.main()
