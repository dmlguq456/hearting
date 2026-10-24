from __future__ import annotations

import io
import json
import os
import contextlib
from pathlib import Path
import runpy
import sqlite3
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools/memory/tests"))

from tidy_isolation import isolated_env
import pane_ownership
import session_tidy as st
import session_tidy_clear as clear
import session_tidy_idle as idle


class IdleObserverTests(unittest.TestCase):
    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        self.cwd = self.iso.root / "project"
        self.cwd.mkdir()
        self.pane = "idle:test-pane"
        self.extra = {"HERDR_PANE_ID": self.pane, "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                      "XDG_CONFIG_HOME": str(self.iso.xdg_config),
                      "HEARTING_TIDY_HERDR": str(self.iso.root / "fake-herdr")}
        fake = self.iso.root / "fake-herdr"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        self.pane_patch = mock.patch.object(pane_ownership, "verified_pane", side_effect=lambda pane, *a, **kw: pane or "")
        self.pane_patch.start()
        self.addCleanup(self.pane_patch.stop)
        self.started = []

    def transcript(self, sid: str, tokens: int | None):
        path = self.iso.root / f"{sid}.jsonl"
        usage = {} if tokens is None else {"input_tokens": tokens, "cache_creation_input_tokens": 0,
                                           "cache_read_input_tokens": 0, "output_tokens": 900000}
        path.write_text(json.dumps({"type": "assistant", "sessionId": sid,
                                    "message": {"usage": usage}}) + "\n", encoding="utf-8")
        return str(path)

    def observe_claude(self, sid="sid-A", tokens=200000, *, event_id="turn-A", now=1000, provider=None):
        payload = {"hook_event_name": "Stop", "session_id": sid, "cwd": str(self.cwd),
                   "transcript_path": self.transcript(sid, tokens), "turn_id": event_id}
        if provider:
            payload["provider"] = provider
        return idle.observe_payload("claude", payload, now=now)

    def test_stop_payload_uses_exact_turn_and_duplicate_does_not_extend_due_time(self):
        with self.iso.patched_environ(self.extra), \
             mock.patch.object(clear, "herdr_command", return_value=str(self.iso.root / "fake-herdr")), \
             mock.patch.object(clear, "_start_helper", side_effect=lambda key, nonce: self.started.append(nonce) or 12345):
            self.assertEqual(self.observe_claude(), "booking=scheduled")
            seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
            first = clear.read_reservation(seat.key)
            self.assertEqual(first["origin"], "idle")
            self.assertEqual(first["idle"]["due_at"], 4240)
            self.assertEqual(first["idle"]["tokens"], 200000)
            self.assertEqual(first["idle"]["cache_ttl"], "unknown")

            self.assertEqual(self.observe_claude(now=1100), "booking=scheduled")
            replay = clear.read_reservation(seat.key)
            self.assertEqual((replay["nonce"], replay["idle"]["due_at"]),
                             (first["nonce"], first["idle"]["due_at"]))
            self.assertEqual(self.started, [first["nonce"]])

    def test_threshold_unknown_and_worker_signals_fail_closed(self):
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "herdr_command", return_value="fake"), \
             mock.patch.object(clear, "_start_helper", return_value=12345):
            for tokens, expected in ((199999, "below-threshold"), (200000, "scheduled"), (200001, "scheduled")):
                result = self.observe_claude(tokens=tokens, event_id=f"turn-{tokens}")
                self.assertIn(expected, result)
                seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
                clear.cancel(seat)
            self.assertEqual(self.observe_claude(tokens=None, event_id="missing"),
                             "booking=skipped reason=usage-unknown")
            os.environ["AGENT_SESSION_ROLE"] = "worker"
            self.assertEqual(self.observe_claude(event_id="worker-turn"), "booking=skipped reason=worker")

    def test_provider_override_sets_wait_without_claiming_a_cache_ttl(self):
        config = self.iso.xdg_config / "hearting" / "session-tidy.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"idle": {"wait_seconds": 100}, "overrides": {
            "claude": {"anthropic": {"wait_seconds": 10, "min_tokens": 200000}}}}), encoding="utf-8")
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "herdr_command", return_value="fake"), \
             mock.patch.object(clear, "_start_helper", return_value=12345):
            self.assertEqual(self.observe_claude(provider="anthropic"), "booking=scheduled")
            seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
            booking = clear.read_reservation(seat.key)
            self.assertEqual(booking["idle"]["due_at"], 1010)
            self.assertEqual(booking["idle"]["cache_ttl"], "unknown")

    def test_marked_final_reply_opens_wait_even_when_usage_is_unknown(self):
        import human_wait_state
        sid = "sid-marked"
        payload = {"hook_event_name": "Stop", "session_id": sid, "cwd": str(self.cwd),
                   "last_assistant_message": "[답변 필요] 계속 진행할까요?",
                   "transcript_path": self.transcript(sid, None), "turn_id": "turn-marked"}
        with self.iso.patched_environ(self.extra):
            result = idle.observe_payload("claude", payload, now=2000)
            self.assertEqual(result, "booking=skipped reason=usage-unknown")
            waits = human_wait_state.read_signals(now=2000)
            self.assertEqual(len(waits), 1)
            self.assertEqual(waits[0]["question"], "계속 진행할까요?")

    def test_prompt_between_observation_and_signal_write_cannot_reopen_wait(self):
        import human_wait_state
        sid = "sid-racing-prompt"
        payload = {"hook_event_name": "Stop", "session_id": sid, "cwd": str(self.cwd),
                   "last_assistant_message": "[답변 필요] 계속 진행할까요?",
                   "transcript_path": self.transcript(sid, None), "turn_id": "turn-racing",
                   "_answer_wait_prompt_seq": 0}
        original_lock = st.seat_lock
        injected = [False]

        @contextlib.contextmanager
        def prompt_before_observer_lock(seat_key, *, wait=None):
            if not injected[0]:
                injected[0] = True
                st.run_hook("claude", "prompt", sid, cwd=str(self.cwd),
                            prompt_text="다음 입력", now=2001)
            with original_lock(seat_key, wait=wait):
                yield

        notifications = []
        with self.iso.patched_environ(self.extra), \
             mock.patch.object(st, "seat_lock", side_effect=prompt_before_observer_lock), \
             mock.patch.object(human_wait_state, "notify_signal", side_effect=notifications.append):
            seat = st.resolve_seat("claude", str(self.cwd), os.environ, sid)
            self.assertEqual(st.read_prompt_seq(seat), 0)
            idle.observe_payload("claude", payload, now=2000)
            self.assertTrue(injected[0])
            self.assertEqual(st.read_prompt_seq(seat), 1)
            self.assertEqual(human_wait_state.read_signals(now=2002), [])
            self.assertEqual(notifications, [])

    def test_lock_timeout_fallback_rechecks_after_optimistic_write(self):
        import human_wait_state
        sid = "sid-fallback-race"
        payload = {"hook_event_name": "Stop", "session_id": sid, "cwd": str(self.cwd),
                   "last_assistant_message": "[답변 필요] 계속 진행할까요?",
                   "transcript_path": self.transcript(sid, None), "turn_id": "turn-fallback",
                   "_answer_wait_prompt_seq": 0}
        original_lock = st.seat_lock
        original_write = human_wait_state.write_signal
        allow_prompt_lock = [False]
        injected = [False]

        @contextlib.contextmanager
        def busy_seat_lock(seat_key, *, wait=None):
            if allow_prompt_lock[0]:
                with original_lock(seat_key, wait=wait):
                    yield
            else:
                raise BlockingIOError("fixture lock contention")
                yield

        def write_then_prompt(value):
            written = original_write(value)
            if written and not injected[0]:
                injected[0] = True
                allow_prompt_lock[0] = True
                try:
                    st.run_hook("claude", "prompt", sid, cwd=str(self.cwd),
                                prompt_text="다음 입력", now=2001)
                finally:
                    allow_prompt_lock[0] = False
            return written

        notifications = []
        with self.iso.patched_environ(self.extra), \
             mock.patch.object(st, "seat_lock", side_effect=busy_seat_lock), \
             mock.patch.object(human_wait_state, "write_signal", side_effect=write_then_prompt), \
             mock.patch.object(human_wait_state, "notify_signal", side_effect=notifications.append):
            seat = st.resolve_seat("claude", str(self.cwd), os.environ, sid)
            self.assertEqual(st.read_prompt_seq(seat), 0)
            idle.observe_payload("claude", payload, now=2000)
            self.assertTrue(injected[0])
            self.assertEqual(st.read_prompt_seq(seat), 1)
            self.assertEqual(human_wait_state.read_signals(now=2002), [])
            self.assertEqual(notifications, [])

    def test_fallback_prompt_closure_after_recheck_skips_registration_notice(self):
        import human_wait_state
        sid = "sid-fallback-notify-race"
        payload = {"hook_event_name": "Stop", "session_id": sid, "cwd": str(self.cwd),
                   "last_assistant_message": "[답변 필요] 계속 진행할까요?",
                   "transcript_path": self.transcript(sid, None), "turn_id": "turn-fallback-notify",
                   "_answer_wait_prompt_seq": 0}
        original_lock = st.seat_lock
        original_is_current = human_wait_state.signal_is_current
        allow_prompt_lock = [False]
        injected = [False]

        @contextlib.contextmanager
        def busy_seat_lock(seat_key, *, wait=None):
            if allow_prompt_lock[0]:
                with original_lock(seat_key, wait=wait):
                    yield
            else:
                raise BlockingIOError("fixture lock contention")
                yield

        def close_prompt_before_final_authorization(signal):
            if not injected[0]:
                injected[0] = True
                allow_prompt_lock[0] = True
                try:
                    st.run_hook("claude", "prompt", sid, cwd=str(self.cwd),
                                prompt_text="다음 입력", now=2001)
                finally:
                    allow_prompt_lock[0] = False
            return original_is_current(signal)

        notifications = []
        with self.iso.patched_environ(self.extra), \
             mock.patch.object(st, "seat_lock", side_effect=busy_seat_lock), \
             mock.patch.object(human_wait_state, "signal_is_current",
                               side_effect=close_prompt_before_final_authorization), \
             mock.patch.object(human_wait_state, "notify_signal", side_effect=notifications.append):
            seat = st.resolve_seat("claude", str(self.cwd), os.environ, sid)
            self.assertEqual(st.read_prompt_seq(seat), 0)
            idle.observe_payload("claude", payload, now=2000)
            self.assertTrue(injected[0])
            self.assertEqual(st.read_prompt_seq(seat), 1)
            self.assertEqual(human_wait_state.read_signals(now=2002), [])
            self.assertEqual(notifications, [])
            self.assertEqual(list((human_wait_state.state_root() / "notifications").glob("*.sent")), [])

    def test_next_user_prompt_clears_marked_wait_for_each_harness(self):
        import human_wait_state
        with self.iso.patched_environ(self.extra):
            for harness in ("claude", "codex", "opencode"):
                sid = "sid-clear-" + harness
                seat = st.resolve_seat(harness, str(self.cwd), os.environ, sid)
                self.assertTrue(human_wait_state.open_signal(
                    harness, sid, seat.key, "[결정 필요] A 또는 B", sequence=0, now=3000))
                projected = __import__("human_gate_observation").read_answer_waits(
                    jobs_paths=(), sessions=[type("Session", (), {"harness": harness,
                        "session_id": sid, "started_at": None})()], now=3001)
                self.assertEqual(projected["total"], 1)
                reader = sys.modules["human_gate_observation"]
                observed = []
                original_reader = reader.read_answer_waits
                def capture(**kwargs):
                    result = original_reader(**kwargs)
                    observed.append(result)
                    return result
                with mock.patch.object(reader, "read_answer_waits", side_effect=capture):
                    card = st.run_hook(harness, "start", sid, source="clear", cwd=str(self.cwd), now=3001)
                self.assertIn("[답변 필요] A 또는 B", card,
                              (seat.key, observed, human_wait_state.read_signals(now=3001), card))
                self.assertEqual(len(human_wait_state.read_signals(now=3001)), 1)
                st.run_hook(harness, "prompt", sid, cwd=str(self.cwd), prompt_text="B로 진행",
                            now=3002)
                self.assertEqual(human_wait_state.read_signals(now=3002), [])

    def test_opencode_session_idle_reads_latest_completed_message_and_provider(self):
        con = sqlite3.connect(self.iso.opencode_db)
        con.execute("CREATE TABLE message (id TEXT, session_id TEXT, data TEXT, time_updated INTEGER)")
        con.execute("INSERT INTO message VALUES (?, ?, ?, ?)", (
            "assistant-1", "op-sid", json.dumps({"id": "assistant-1", "role": "assistant",
                "finish": "stop", "providerID": "provider-x", "modelID": "model-y",
                "tokens": {"input": 190000, "output": 999999, "cache": {"read": 10000, "write": 0}}}), 10))
        con.commit()
        con.close()
        payload = {"event": "session.idle", "session_id": "op-sid", "cwd": str(self.cwd)}
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "herdr_command", return_value="fake"), \
             mock.patch.object(clear, "_start_helper", return_value=12345):
            self.assertEqual(idle.observe_payload("opencode", payload, now=2000), "booking=scheduled")
            seat = st.resolve_seat("opencode", str(self.cwd), sid="op-sid")
            booking = clear.read_reservation(seat.key)
            self.assertEqual((booking["idle"]["tokens"], booking["idle"]["provider"],
                              booking["idle"]["model"]), (200000, "provider-x", "model-y"))

    def test_codex_stop_uses_only_the_exact_rollout_latest_input_usage(self):
        rollout = self.iso.codex_home / "sessions" / "rollout.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text("\n".join((
            json.dumps({"type": "session_meta", "payload": {"id": "cx-sid", "source": {}}}),
            json.dumps({"type": "event_msg", "payload": {"type": "token_count",
                         "info": {"last_token_usage": {"input_tokens": 200000,
                                                             "cached_input_tokens": 190000}}}}),
        )) + "\n", encoding="utf-8")
        payload = {"thread_id": "cx-sid", "cwd": str(self.cwd), "transcript_path": str(rollout),
                   "turn_id": "cx-turn"}
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "herdr_command", return_value="fake"), \
             mock.patch.object(clear, "_start_helper", return_value=12345):
            self.assertEqual(idle.observe_payload("codex", payload, now=3000), "booking=scheduled")
            seat = st.resolve_seat("codex", str(self.cwd), sid="cx-sid")
            booking = clear.read_reservation(seat.key)
            self.assertEqual(booking["idle"]["tokens"], 200000)
            self.assertEqual(booking["idle"]["provider"], "unknown")

    def test_transport_claim_and_prompt_arrival_are_required_for_idle_enqueue(self):
        import session_tidy_runner as runner
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "herdr_command", return_value="fake"), \
             mock.patch.object(clear, "_start_helper", return_value=12345):
            self.assertEqual(self.observe_claude(), "booking=scheduled")
            seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
            before = clear.read_reservation(seat.key)
            path = clear.reservation_path(seat.key)
            with mock.patch.object(st, "now_epoch", return_value=4241):
                claim, reason = clear.claim_idle_card_request(path, before["nonce"])
                self.assertEqual(reason, "")
                text = clear.idle_card_request_text(claim)
                sequence = st.read_prompt_seq(seat) + 1
                st.run_hook("claude", "prompt", "sid-A", cwd=str(self.cwd), prompt_text=text, now=4241)
                self.assertTrue(clear.finish_idle_card_request(claim, accepted=True))
                context = clear.idle_context_for_enqueue(seat, "claude", "sid-A")
                self.assertEqual(context["identity"], before["idle"]["identity"])
                with st.seat_lock(seat.key):
                    st.write_card(seat, "claude", "sid-A", "진행 중: 카드", cwd=str(self.cwd),
                                  prompt_seq=sequence, now=4242)
                queue = runner.enqueue_item(seat, "claude", "sid-A", str(self.cwd),
                                            operation_id=context["identity"], start=False)
                result = clear._accept_idle_enqueue(seat, "claude", "sid-A", str(self.cwd), context,
                                                   operation_id=context["identity"], queue_id=queue["id"],
                                                   lock_wait=None)
                self.assertEqual(result, "clear=scheduled")
                accepted = clear.read_reservation(seat.key)
                self.assertEqual((accepted["idle_request"]["state"], accepted["queue_id"],
                                  accepted["followup_kind"]), ("accepted", queue["id"], "status-only"))

    def _requested(self):
        """Book an idle tidy for sid-A and deliver its exact request; return (seat, claim, text)."""
        self.assertEqual(self.observe_claude(), "booking=scheduled")
        seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
        path = clear.reservation_path(seat.key)
        claim, reason = clear.claim_idle_card_request(path, clear.read_reservation(seat.key)["nonce"])
        self.assertEqual(reason, "")
        text = clear.idle_card_request_text(claim)
        st.run_hook("claude", "prompt", "sid-A", cwd=str(self.cwd), prompt_text=text)
        self.assertTrue(clear.finish_idle_card_request(claim, accepted=True))
        return seat, claim, text

    def _stop_with(self, reply):
        payload = {"hook_event_name": "Stop", "session_id": "sid-A", "cwd": str(self.cwd),
                   "transcript_path": self.transcript("sid-A", 200000), "turn_id": "turn-tidy",
                   "last_assistant_message": reply}
        return idle.observe_payload("claude", payload)

    def test_stop_takes_a_marked_reply_as_the_card_and_queues_once(self):
        import session_tidy_runner as runner
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "herdr_command", return_value="fake"), \
             mock.patch.object(clear, "_start_helper", return_value=12345), \
             mock.patch.object(runner, "start_detached", return_value=None), \
             mock.patch.object(st, "now_epoch", return_value=4241):
            seat, claim, text = self._requested()
            begin, end = st.reply_card_markers(claim["nonce"])
            self.assertNotIn("\n", text)
            self.assertIn(begin, text)
            self._stop_with("카드 없이 끝난 답")
            self.assertNotEqual(clear.read_reservation(seat.key)["idle_request"]["state"], "accepted")
            self._stop_with(f"{begin}\n진행 중: 답으로 쓴 카드\n다음 할 일: 기다림\n{end}")
            accepted = clear.read_reservation(seat.key)
            self.assertEqual((accepted["idle_request"]["state"], accepted["followup_kind"]),
                             ("accepted", "status-only"))
            card = st.read_latest_card(seat)
            self.assertEqual(card["body"], "진행 중: 답으로 쓴 카드\n다음 할 일: 기다림")
            self.assertEqual(runner.read_item(accepted["queue_id"])["operation_id"], accepted["operation_id"])

    def test_input_after_the_request_or_another_nonce_writes_no_card_and_books_no_clear(self):
        import session_tidy_runner as runner
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "herdr_command", return_value="fake"), \
             mock.patch.object(clear, "_start_helper", return_value=12345), \
             mock.patch.object(runner, "start_detached", return_value=None), \
             mock.patch.object(st, "now_epoch", return_value=4241):
            seat, claim, _text = self._requested()
            other_begin, other_end = st.reply_card_markers("not-this-request")
            self.assertEqual(st.accept_reply_card("claude", "sid-A", str(self.cwd),
                                                  f"{other_begin}\n진행 중: x\n{other_end}"),
                             "reply-card=none reason=unmarked")
            st.run_hook("claude", "prompt", "sid-A", cwd=str(self.cwd), prompt_text="사용자가 그사이 입력")
            begin, end = st.reply_card_markers(claim["nonce"])
            self._stop_with(f"{begin}\n진행 중: 늦은 카드\n{end}")
            self.assertIsNone(st.read_latest_card(seat))
            self.assertNotEqual((clear.read_reservation(seat.key) or {}).get("status"), "accepted")
            self.assertFalse(any(item.get("card_generation") for item in runner.list_items()))

    def test_last_reply_reads_each_harness_without_guessing(self):
        self.assertEqual(idle.last_reply("claude", {"last_assistant_message": "끝"}, "s"), "끝")
        self.assertEqual(idle.last_reply("codex", {"last_assistant_message": None}, "s"), "")
        database = self.iso.root / "opencode.db"
        con = sqlite3.connect(database)
        con.execute("CREATE TABLE message (id text, session_id text, time_created integer, time_updated integer, data text)")
        con.execute("CREATE TABLE part (id text, message_id text, session_id text, time_created integer, time_updated integer, data text)")

        def message(mid, created, **data):
            con.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                        (mid, "oc-1", created, created, json.dumps({"role": "assistant", **data})))

        def part(pid, mid, **data):
            con.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (pid, mid, "oc-1", 0, 0, json.dumps(data)))

        message("m1", 1, finish="stop", time={"created": 1, "completed": 2})
        part("p1", "m1", type="reasoning", text="생각")
        part("p2", "m1", type="text", text="첫 줄")
        part("p3", "m1", type="text", text="둘째 줄")
        part("p4", "m1", type="text", text="숨김", synthetic=True)
        con.commit()
        import tidy_transcripts
        with mock.patch.object(tidy_transcripts, "opencode_db_path", return_value=str(database)):
            self.assertEqual(idle.last_reply("opencode", {}, "oc-1"), "첫 줄\n둘째 줄")
            message("m2", 3, finish="length", time={"created": 3, "completed": 4})       # cut off
            part("p5", "m2", type="text", text="잘린 답")
            con.commit()
            self.assertEqual(idle.last_reply("opencode", {}, "oc-1"), "")
            message("m3", 5, time={"created": 5})                                        # still running
            con.commit()
            self.assertEqual(idle.last_reply("opencode", {}, "oc-1"), "")
        con.close()

    def test_last_reply_reads_opencode_2_session_messages(self):
        database = self.iso.root / "opencode2.db"
        con = sqlite3.connect(database)
        con.execute("CREATE TABLE session_message (id text, session_id text, type text, seq integer, "
                    "time_created integer, time_updated integer, data text)")

        def row(mid, kind, created, **data):
            con.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                        (mid, "oc-2", kind, 1, created, created, json.dumps(data)))
            con.commit()

        row("u1", "user", 1, text="요청")
        row("a1", "assistant", 2, finish="stop", time={"created": 2, "completed": 3},
            content=[{"type": "reasoning", "text": "생각"}, {"type": "text", "text": "카드 답"}])
        row("i1", "idle", 4, outcome="succeeded")
        import tidy_transcripts
        with mock.patch.object(tidy_transcripts, "opencode_db_path", return_value=str(database)):
            self.assertEqual(idle.last_reply("opencode", {}, "oc-2"), "카드 답")
            row("u2", "user", 5, text="그사이 입력")                     # a newer prompt: not this reply
            self.assertEqual(idle.last_reply("opencode", {}, "oc-2"), "")
            row("a2", "assistant", 6, finish="length", time={"created": 6, "completed": 7},
                content=[{"type": "text", "text": "잘림"}])
            self.assertEqual(idle.last_reply("opencode", {}, "oc-2"), "")
        con.close()

    def test_fake_clock_runs_one_idle_cycle_and_rearms_only_after_user_input(self):
        import dispatch_seat_handover as handover
        import session_tidy_runner as runner
        clock = [1000.0]
        sends = []
        queue_written = [False]
        prompt_text = "사용자가 새로 입력한 요청"
        payload = {"hook_event_name": "Stop", "session_id": "sid-A", "cwd": str(self.cwd),
                   "transcript_path": self.transcript("sid-A", 200000), "turn_id": "turn-A"}
        config = self.iso.xdg_config / "hearting" / "session-tidy.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"idle": {"wait_seconds": 10}}), encoding="utf-8")
        with self.iso.patched_environ(self.extra), \
             mock.patch.object(clear, "herdr_command", return_value=str(self.iso.root / "fake-herdr")), \
             mock.patch.object(clear, "_start_helper", side_effect=lambda *_: 12345), \
             mock.patch.object(st, "now_epoch", side_effect=lambda: clock[0]), \
             mock.patch.object(clear, "_run_idle_card_request") as request, \
             mock.patch.object(runner, "start_detached", return_value=12345), \
             mock.patch.object(handover, "collect_snapshot_bindings", return_value=[]), \
             mock.patch.object(st, "resolve_seat", side_effect=lambda harness, cwd=None, *a, **kw:
                               st.Seat("pane", st._digest("pane", self.pane), self.pane, harness, "")):
            request_text = {"text": ""}

            def send_once(path, nonce, pane):
                sends.append(("idle-request", nonce))
                claim, reason = clear.claim_idle_card_request(path, nonce)
                self.assertEqual(reason, "")
                request_text["text"] = clear.idle_card_request_text(claim)
                st.run_hook("claude", "prompt", "sid-A", cwd=str(self.cwd),
                            prompt_text=request_text["text"], now=clock[0])
                self.assertTrue(clear.finish_idle_card_request(claim, accepted=True))
                return {"accepted": "true"}

            request.side_effect = send_once
            self.assertEqual(idle.observe_payload("claude", payload, now=clock[0]), "booking=scheduled")
            seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
            nonce = clear.read_reservation(seat.key)["nonce"]

            def sleep(seconds):
                clock[0] += seconds
                req = clear.read_reservation(seat.key)
                idle_request = req.get("idle_request") if req else None
                if idle_request and idle_request.get("state") == "sent" and not queue_written[0]:
                    sequence = st.read_prompt_seq(seat)
                    with st.seat_lock(seat.key):
                        st.write_card(seat, "claude", "sid-A", "진행 중: idle test", cwd=str(self.cwd),
                                      prompt_seq=sequence, now=clock[0])
                    context = clear.idle_context_for_enqueue(seat, "claude", "sid-A")
                    item = runner.enqueue_item(seat, "claude", "sid-A", str(self.cwd),
                                               operation_id=context["identity"], start=False)
                    line = clear.schedule_for_enqueue(
                        seat, "claude", "sid-A", str(self.cwd), idle_context=context,
                        operation_id=context["identity"], queue_id=item["id"])
                    self.assertEqual(line, "clear=scheduled")
                    runner._start_queued(item)
                    queue_written[0] = True

            def continue_status(path, continued_nonce, _pane):
                claim, reason = clear.claim_continue(path, continued_nonce)
                self.assertEqual(reason, "")
                st.run_hook("claude", "prompt", "sid-B", cwd=str(self.cwd),
                            prompt_text=clear.STATUS_ONLY_TEXT, now=clock[0])
                return {"continued": "true"}

            result = clear.run_helper(
                seat.key, nonce, wait=lambda *_: "idle", sleep=sleep,
                steward=lambda *_: sends.append(("clear", nonce)) or
                    {"cleared": "true", "new_session": "sid-B"},
                continuer=continue_status)
            self.assertEqual(result, "continued")
            self.assertTrue(queue_written[0])
            self.assertEqual([kind for kind, _identity in sends], ["idle-request", "clear"])
            request.assert_called_once()
            settled = clear.read_reservation(seat.key)
            self.assertEqual(settled["continued"]["kind"], "status-only")
            self.assertEqual(settled["continued"]["status_prompt_seq"], 2)
            self.assertFalse(clear.idle_clear_armed(seat, "claude", "sid-B"))

            successor_payload = {"hook_event_name": "Stop", "session_id": "sid-B", "cwd": str(self.cwd),
                                 "transcript_path": self.transcript("sid-B", 200000), "turn_id": "status-turn"}
            self.assertEqual(idle.observe_payload("claude", successor_payload, now=clock[0]),
                             "booking=skipped reason=not-rearmed")
            st.run_hook("claude", "prompt", "sid-B", cwd=str(self.cwd), prompt_text=prompt_text,
                        now=clock[0] + 1)
            self.assertTrue(clear.idle_clear_armed(seat, "claude", "sid-B"))
            self.assertEqual(idle.observe_payload(
                "claude", {**successor_payload, "turn_id": "user-turn"}, now=clock[0] + 2), "booking=scheduled")

    def test_late_unverified_claim_keeps_idle_provenance_after_booking_terminal(self):
        clock = [1000.0]
        with self.iso.patched_environ(self.extra), \
             mock.patch.object(clear, "herdr_command", return_value=str(self.iso.root / "fake-herdr")), \
             mock.patch.object(clear, "_start_helper", return_value=12345), \
             mock.patch.object(st, "now_epoch", side_effect=lambda: clock[0]):
            self.assertEqual(self.observe_claude(now=clock[0]), "booking=scheduled")
            seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
            booking = clear.read_reservation(seat.key)
            clock[0] = 4241
            claim, reason = clear.claim_idle_card_request(clear.reservation_path(seat.key), booking["nonce"])
            self.assertEqual(reason, "")
            request = clear.idle_card_request_text(claim)
            self.assertFalse(clear.finish_idle_card_request(claim, accepted=True))
            self.assertTrue(clear._finish(seat.key, booking["nonce"], "unverified", "arrival-not-observed"))

            # The exact claimed request arrives after the helper has recorded uncertainty.
            clock[0] = 4242
            st.run_hook("claude", "prompt", "sid-A", cwd=str(self.cwd), prompt_text=request, now=clock[0])
            context = clear.idle_context_for_enqueue(seat, "claude", "sid-A")
            self.assertIsNotNone(context, "late self-request lost its idle provenance")
            self.assertEqual(context["origin"], "idle")
            self.assertFalse(context["eligible"], "late arrival must not restore clear permission")

            import session_tidy_runner as runner
            item = runner.enqueue_item(seat, "claude", "sid-A", str(self.cwd),
                                       operation_id=context["identity"], start=False)
            outcome = clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd),
                                                 idle_context=context, operation_id=context["identity"],
                                                 queue_id=item["id"])
            self.assertNotEqual(outcome, "clear=scheduled")
            self.assertEqual(clear.read_reservation(seat.key)["nonce"], booking["nonce"])

    def test_self_request_stop_after_unverified_terminal_does_not_book_a_new_idle_nonce(self):
        clock = [1000.0]
        with self.iso.patched_environ(self.extra), \
             mock.patch.object(clear, "herdr_command", return_value=str(self.iso.root / "fake-herdr")), \
             mock.patch.object(clear, "_start_helper", return_value=12345), \
             mock.patch.object(st, "now_epoch", side_effect=lambda: clock[0]):
            self.assertEqual(self.observe_claude(event_id="idle-A", now=clock[0]), "booking=scheduled")
            seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
            booking = clear.read_reservation(seat.key)
            clock[0] = 4241
            claim, reason = clear.claim_idle_card_request(clear.reservation_path(seat.key), booking["nonce"])
            self.assertEqual(reason, "")
            st.run_hook("claude", "prompt", "sid-A", cwd=str(self.cwd),
                        prompt_text=clear.idle_card_request_text(claim), now=clock[0])
            self.assertTrue(clear.finish_idle_card_request(claim, accepted=True))
            self.assertTrue(clear._finish(seat.key, booking["nonce"], "unverified", "helper-timeout"))

            clock[0] = 4242
            result = self.observe_claude(event_id="self-request-stop", now=clock[0])
            self.assertEqual(result, "booking=skipped reason=not-rearmed")
            self.assertEqual(clear.read_reservation(seat.key)["nonce"], booking["nonce"])
            notices = st.read_json(st._notices_path(seat))
            text = "\n".join(item.get("text", "") for item in notices.get("items", []))
            self.assertNotIn("/clear", text)
            self.assertNotIn("이어서해", text)

    def test_real_prompt_in_new_same_pane_session_rearms_after_status_only_clear(self):
        with self.iso.patched_environ(self.extra), \
             mock.patch.object(clear, "herdr_command", return_value=str(self.iso.root / "fake-herdr")), \
             mock.patch.object(clear, "_start_helper", return_value=12345):
            self.assertEqual(self.observe_claude(event_id="idle-A", now=1000), "booking=scheduled")
            seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
            booking = clear.read_reservation(seat.key)
            self.assertTrue(clear._finish(seat.key, booking["nonce"], "cleared", observed="sid-B"))
            with st.seat_lock(seat.key):
                current = clear.read_reservation(seat.key)
                current["continued"]["state"] = "sending"
                current["continued"]["claim_id"] = "claim-status-only"
                clear._write_reservation(current)

            st.run_hook("claude", "prompt", "sid-B", cwd=str(self.cwd),
                        prompt_text=clear.STATUS_ONLY_TEXT, now=1001)
            self.assertTrue(clear._finish_continue(seat.key, booking["nonce"], "true",
                                                   claim_id="claim-status-only"))
            self.assertEqual(self.observe_claude(sid="sid-B", event_id="status-stop", now=1002),
                             "booking=skipped reason=not-rearmed")

            st.run_hook("claude", "prompt", "sid-C", cwd=str(self.cwd),
                        prompt_text="사용자가 새 세션에서 입력", now=1003)
            self.assertEqual(self.observe_claude(sid="sid-C", event_id="sid-C-stop", now=1004),
                             "booking=scheduled")

    def test_forced_fold_keeps_late_duplicate_claim_and_real_prompt_frontier_distinct(self):
        clock = [1000.0]
        with self.iso.patched_environ(self.extra), \
             mock.patch.object(clear, "herdr_command", return_value=str(self.iso.root / "fake-herdr")), \
             mock.patch.object(clear, "_start_helper", return_value=12345), \
             mock.patch.object(st, "now_epoch", side_effect=lambda: clock[0]):
            self.assertEqual(self.observe_claude(event_id="fold-idle", now=clock[0]), "booking=scheduled")
            seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
            booking = clear.read_reservation(seat.key)
            clock[0] = 4241
            claim, reason = clear.claim_idle_card_request(clear.reservation_path(seat.key), booking["nonce"])
            self.assertEqual(reason, "")
            claim_id = claim["idle_request"]["token"]
            request = clear.idle_card_request_text(claim)
            self.assertTrue(clear._finish(seat.key, booking["nonce"], "unverified", "arrival-not-observed"))

            with st.seat_lock(seat.key):
                for index in range(st.LEDGER_FOLD_LINES + 8):
                    st.record_event(seat, "claude", "sid-A", "card", cwd=str(self.cwd), now=5000 + index)
                st._fold_ledger(seat)
            self.assertLess(len(st._read_ledger_lines(seat)), st.LEDGER_FOLD_LINES)

            clock[0] = 6000
            st.run_hook("claude", "prompt", "sid-A", cwd=str(self.cwd), prompt_text=request, now=clock[0])
            clock[0] += 1
            st.run_hook("claude", "prompt", "sid-A", cwd=str(self.cwd), prompt_text=request, now=clock[0])
            self.assertFalse(clear.idle_clear_armed(seat, "claude", "sid-A"))
            clock[0] += 1
            st.run_hook("claude", "prompt", "sid-A", cwd=str(self.cwd),
                        prompt_text="actual user input", now=clock[0])
            self.assertTrue(clear.idle_clear_armed(seat, "claude", "sid-A"))

            prompt_state = st.prompt_state(seat)
            with st.seat_lock(seat.key):
                st._fold_ledger(seat)
            saved_claim = st.tidy_claim_by_id(seat, claim_id)
            frontier = st.real_prompt_frontier(seat, "claude", "sid-A")
            self.assertEqual((saved_claim["state"], saved_claim["arrival_seq"]), ("arrived", 1))
            self.assertEqual((prompt_state["source"], frontier["sequence"]), ("real", prompt_state["seq"]))

    def test_detached_launcher_keeps_pane_hint_and_strips_worker_context(self):
        captured = {}

        def spawn(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            captured["payload"] = os.pread(kwargs["stdin"], 48 * 1024, 0)
            return object()

        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.iso.home),
               "XDG_CONFIG_HOME": str(self.iso.xdg_config), "XDG_STATE_HOME": str(self.iso.xdg_state),
               "HERDR_PANE_ID": self.pane, "HERDR_SOCKET_PATH": str(self.iso.root / "fake.sock"),
               "AGENT_SESSION_ROLE": "main", "AGENT_ARTIFACT_PRODUCER_ID": "parent"}
        payload = {"hook_event_name": "Stop", "session_id": "sid-A", "cwd": str(self.cwd)}
        self.assertTrue(idle.launch_observer("claude", payload, environ=env, popen=spawn))
        self.assertEqual(json.loads(captured["payload"]), payload)
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertIn(captured["kwargs"]["stdin"], captured["kwargs"]["pass_fds"])
        self.assertEqual(captured["kwargs"]["env"]["HERDR_PANE_ID"], self.pane)
        self.assertEqual(captured["kwargs"]["env"]["HERDR_SOCKET_PATH"], env["HERDR_SOCKET_PATH"])
        self.assertNotIn("AGENT_SESSION_ROLE", captured["kwargs"]["env"])
        self.assertNotIn("AGENT_ARTIFACT_PRODUCER_ID", captured["kwargs"]["env"])

    def test_detached_launcher_excludes_worker_owner_auxiliary_and_reviewer(self):
        spawn = mock.Mock()
        payload = {"hook_event_name": "Stop", "session_id": "sid-A", "cwd": str(self.cwd)}
        for role in ("worker", "owner", "auxiliary", "reviewer"):
            with self.subTest(role=role):
                env = {"AGENT_SESSION_ROLE": role}
                self.assertFalse(idle.launch_observer("claude", payload, environ=env, popen=spawn))
        spawn.assert_not_called()

    def test_claude_idle_bridge_is_registered_on_stop_not_session_start(self):
        settings = json.loads((ROOT / "adapters/claude/settings.json").read_text(encoding="utf-8"))
        plugin = json.loads((ROOT / "adapters/claude/plugin-marketplace/plugins/hearting-claude/hooks/hooks.json")
                            .read_text(encoding="utf-8"))

        def commands(config, event):
            return [hook.get("command", "") for group in config.get("hooks", {}).get(event, [])
                    for hook in group.get("hooks", [])]

        self.assertTrue(any("session-tidy-idle.py" in command for command in commands(settings, "Stop")))
        self.assertFalse(any("session-tidy-idle.py" in command for command in commands(settings, "SessionStart")))
        self.assertTrue(any("session-tidy-idle.py" in command for command in commands(plugin, "Stop")))
        self.assertFalse(any("session-tidy-idle.py" in command for command in commands(plugin, "SessionStart")))

    def test_claude_stop_bridge_only_launches_the_detached_observer(self):
        payload = {"hook_event_name": "Stop", "session_id": "sid-A", "cwd": str(self.cwd)}
        calls = []
        with mock.patch.dict(os.environ, {"AGENT_HOME": str(ROOT)}, clear=True), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(idle, "launch_observer", side_effect=lambda harness, value:
                                  calls.append((harness, value))):
            bridge = runpy.run_path(str(ROOT / "hooks/session-tidy-idle.py"), run_name="idle_bridge_test")
            bridge["main"]()
        self.assertEqual(calls, [("claude", payload)])


if __name__ == "__main__":
    unittest.main()
