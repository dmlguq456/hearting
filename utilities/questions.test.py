#!/usr/bin/env python3
"""``hearting run questions``: one shared board, answers only through the existing path."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import human_wait_state as state  # noqa: E402
import questions  # noqa: E402


def _run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = questions.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class BuildResponseTest(unittest.TestCase):
    INTERVIEW = {"kind": "question", "detail": {"questions": [
        {"id": "_understanding", "question": "Restated", "recommended": 0,
         "choices": [{"value": "I understand", "label": "I understand"}]},
        {"id": "scope", "question": "Which scope?", "recommended": 1,
         "choices": [{"value": "Small", "label": "Small"}, {"value": "Both", "label": "Both"}]},
    ]}}

    def test_one_choice_per_question_maps_to_the_native_values(self):
        response, error = questions.build_response(self.INTERVIEW, choices=[1, 2])
        self.assertIsNone(error)
        self.assertEqual(response, {"answers": {"_understanding": ["I understand"],
                                                "scope": ["Both"]}})
        self.assertIsNone(questions.build_response(self.INTERVIEW, choices=[1])[0])
        self.assertIsNone(questions.build_response(self.INTERVIEW, choices=[1, 3])[0])
        self.assertIsNone(questions.build_response(self.INTERVIEW, text="note")[0])
        self.assertEqual(questions.questions_of(self.INTERVIEW)[1]["recommended"], 1)

    def test_render_reuses_the_shared_recommended_label(self):
        row = {"id": "q1", "kind": "question", "age": "now", "project": "",
               "owner": {}, "owner_state": "live", "state": "current", "answerable": False,
               "questions": [{"question": "Pick one", "choices": ["A", "B"], "recommended": 0}]}
        with mock.patch.object(questions.HWS, "wait_counts", return_value={"total": 1, "older": 0, "ownerless": 0}), \
             mock.patch.object(questions.HWS, "ui_translator", return_value=lambda _key: "추천"):
            out = questions.render({"items": [], "complete": True}, [row], [])
        self.assertIn("(추천)", out)

    def test_render_indents_continuation_lines_of_decision_questions(self):
        row = {"id": "q1", "kind": "question", "age": "now", "project": "",
               "owner": {}, "owner_state": "live", "state": "current", "answerable": False,
               "questions": [{"question": "[결정 필요] 보관 기간\n- 30일: 유지\n추천: 30일 — 복구 가능",
                              "choices": [], "recommended": None}]}
        with mock.patch.object(questions.HWS, "wait_counts", return_value={"total": 1, "older": 0, "ownerless": 0}):
            out = questions.render({"items": [], "complete": True}, [row], [])
        lines = out.splitlines()
        self.assertIn("  [결정 필요] 보관 기간", lines)
        self.assertIn("  - 30일: 유지", lines)
        self.assertIn("  추천: 30일 — 복구 가능", lines)

    def test_single_question_text_answers_file_and_approval_shapes(self):
        single = {"kind": "question", "question_id": "owner-correction",
                  "detail": {"question": "blocked", "choices": []}}
        self.assertEqual(questions.build_response(single, text="retry")[0],
                         {"answers": {"owner-correction": ["retry"]}})
        self.assertEqual(questions.build_response(self.INTERVIEW, answers={
            "_understanding": "I understand", "scope": ["Small"]})[0],
            {"answers": {"_understanding": ["I understand"], "scope": ["Small"]}})
        opencode = {"kind": "question", "detail": {"questions": [
            {"id": "enabled", "question": "Enable?", "field_type": "boolean", "choices": []}]}}
        self.assertEqual(questions.build_response(opencode, answers={"enabled": True})[0],
                         {"answers": {"enabled": [True]}})
        approval = {"kind": "approval"}
        self.assertEqual(questions.build_response(approval, decision="reject")[0],
                         {"decision": "reject"})
        self.assertIsNone(questions.build_response(approval, choices=[1])[0])
        self.assertIsNone(questions.build_response(single, decision="approve")[0])


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name,
                                             "PATH": str(Path(self.tmp.name) / "empty-bin")})
        patch.start()
        self.addCleanup(patch.stop)
        demand = mock.patch.object(state, "_request_producer")
        demand.start()
        self.addCleanup(demand.stop)

    def test_closed_native_questions_are_distinct_from_unknown_ids(self):
        from fleet import interaction
        for harness, source in (("claude", "claude-asktool"), ("codex", "codex-appserver"),
                                ("opencode", "opencode-question")):
            with self.subTest(harness=harness):
                interaction.set_wait("fixture-session", harness, "decision", source, now=1000)
                interaction.clear_wait("fixture-session", harness)
                with mock.patch.object(questions, "load_board", return_value={"items": []}):
                    code, _, err = _run("answer", f"S1:{harness}:fixture-session:1000000", "--text", "yes")
                    self.assertEqual(code, 2)
                    self.assertIn("closed", err)
                    self.assertIn("reason unavailable", err)
                    self.assertIn("UTC", err)
                    unknown = _run("answer", f"S1:{harness}:fixture-session:999", "--text", "yes")
                    self.assertIn("no recorded question", unknown[2])

    def test_native_closure_reasons_and_reopened_id_do_not_change_publication(self):
        from fleet import interaction
        for status in ("answered", "cancelled", "expired"):
            interaction.set_wait("known", "codex", "decision", "codex-appserver", now=1000)
            interaction.clear_wait("known", "codex", reason=status)
            item_id = "S1:codex:known:1000000"
            state.publish_cache({"complete": True, "items": []})
            with mock.patch.object(questions, "load_board", return_value={"items": []}):
                self.assertIn(status, _run("answer", item_id, "--text", "yes")[2])
            interaction.set_wait("known", "codex", "decision", "codex-appserver", now=1000)
            self.assertIsNone(state.closed_question(item_id))
            self.assertIsNotNone(interaction.read_wait("known", "codex"))
            interaction.set_wait("known", "codex", "decision", "codex-appserver", now=1001)
            self.assertEqual(state.closed_question(item_id)["status"], "replaced")

    def test_missing_observation_is_unknown_not_an_empty_board(self):
        with mock.patch.object(questions, "_CACHE_WAIT_SECONDS", 0.0):
            code, out, err = _run()
        self.assertEqual((code, out), (3, ""))
        self.assertIn("unknown", err)

    def test_list_counts_match_the_shared_projection_and_ids_resolve_exactly(self):
        import time
        now = time.time()
        self.assertTrue(state.publish_cache({"observed_at": now, "complete": True, "items": [
            {"id": "S1:codex:sid-a:1", "source": "S1", "kind": "decision", "since": now - 30,
             "stale": False, "owner": {"harness": "codex", "session_id": "sid-a"},
             "owner_state": "live", "attribution": "exact"},
            {"id": "S1:codex:sid-b:2", "source": "S1", "kind": "decision", "since": now - 99,
             "stale": True, "fold": "ownerless", "owner_state": "none", "attribution": "exact",
             "owner": {"harness": "codex", "session_id": "sid-b"}},
        ]}))
        envelope = {"state": "open", "kind": "question", "support": {"supported": True},
                    "detail": {"questions": [{"id": "q", "question": "Pick one", "choices": [
                        {"value": "A", "label": "A"}, {"value": "B", "label": "B"}]}]}}
        with mock.patch.object(state, "read_selected_request", return_value=envelope) as read:
            code, out, _err = _run()
        self.assertEqual(code, 0)
        self.assertIn("questions: 1 current · 1 older (1 ownerless)", out)
        self.assertIn("Pick one", out)
        self.assertIn("S1:codex:sid-b:2  native question", out)
        self.assertIn("owner ended, no successor", out)
        read.assert_called_once_with({"id": "S1:codex:sid-a:1", "harness": "codex",
                                      "session_id": "sid-a"})
        with mock.patch.object(state, "read_selected_request", return_value=envelope), \
             mock.patch.object(state, "submit_selected_response") as submit:
            for prefix in ("S1:codex:sid-", "S1:codex:sid-a"):
                code, _out, err = _run("answer", prefix, "--choice", "1")
                self.assertEqual(code, 2)
                self.assertIn("exactly this id", err)
        submit.assert_not_called()

        def own(ref, response, *, action_id):
            return {"state": "applied", "action_id": action_id}
        with mock.patch.object(state, "read_selected_request", return_value=envelope), \
             mock.patch.object(state, "submit_selected_response", side_effect=own) as submit:
            code, out, _err = _run("answer", "S1:codex:sid-a:1", "--choice", "2")
        self.assertEqual(code, 0)
        self.assertIn("applied", out)
        ref, response = submit.call_args.args
        self.assertEqual((ref["id"], ref["question_id"], response),
                         ("S1:codex:sid-a:1", "q", {"answers": {"q": ["B"]}}))
        self.assertRegex(submit.call_args.kwargs["action_id"], r"^[0-9a-f]{32}$")
        # Another caller (Fleet) claimed first: its state is not this command's success.
        for other in ({"state": "submitted", "action_id": "fleet-action"},
                      {"state": "applied"}):
            with mock.patch.object(state, "read_selected_request", return_value=envelope), \
                 mock.patch.object(state, "submit_selected_response", return_value=other):
                code, out, _err = _run("answer", "S1:codex:sid-a:1", "--choice", "2")
            self.assertEqual(code, 1)
            self.assertIn("answered-elsewhere", out)

    def test_defer_is_reported_as_deferred_and_sends_nothing(self):
        import time
        now = time.time()
        self.assertTrue(state.publish_cache({"observed_at": now, "complete": True, "items": [
            {"id": "S1:codex:sid-a:1", "source": "S1", "kind": "approval", "since": now - 30,
             "stale": False, "owner": {"harness": "codex", "session_id": "sid-a"},
             "attribution": "exact"}]}))
        envelope = {"state": "open", "kind": "approval", "support": {"supported": True}}
        deferred = {"state": "open", "result": {"decision": "defer",
                                                "status": "no-response-sent"}}
        with mock.patch.object(state, "read_selected_request", return_value=envelope), \
             mock.patch.object(state, "submit_selected_response", return_value=deferred):
            code, out, _err = _run("answer", "S1:codex:sid-a:1", "--decision", "defer")
        self.assertEqual(code, 0)
        self.assertIn("deferred", out)


class MessageQuestionTest(unittest.TestCase):
    def setUp(self):
        from fixtures.message_questions import MessageQuestionFixture
        self.fixture = MessageQuestionFixture()
        self.f = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)

    def test_closed_registered_question_names_answer_or_replacement_without_exposing_text(self):
        for harness in ("claude", "codex", "opencode"):
            signal, _ = self.f.ask(harness=harness)
            self.assertTrue(state.close_signal(signal["seat_key"], harness=harness,
                session_id=signal["session_id"], sequence=signal["sequence"] + 1, answer="PRIVATE ANSWER"))
            with mock.patch.object(questions, "load_board", return_value={"items": []}):
                code, _, err = _run("answer", signal["id"], "--text", "second answer")
            self.assertEqual(code, 2)
            self.assertIn("answered", err)
            self.assertIn("UTC", err)
            self.assertNotIn("PRIVATE", err)
            old, _ = self.f.ask(harness=harness, text="old question")
            self.f.ask(harness=harness, text="new question")
            with mock.patch.object(questions, "load_board", return_value={"items": []}):
                code, _, err = _run("answer", old["id"], "--text", "second answer")
            self.assertEqual(code, 2)
            self.assertIn("replaced", err)

    def test_ask_cli_uses_caller_seat_sequence_and_preserves_private_detail(self):
        import session_tidy as tidy
        from fleet import process_identity
        seat = self.f.seat()
        text = "한국어 질문\n두 번째 줄 🙂" * 9
        private = self.f.root / "question.txt"
        private.write_text(text)
        with mock.patch("dispatch_parent_completion.interactive_parent_identity", return_value=("claude", "native-question-session")), \
             mock.patch.object(tidy, "resolve_seat", return_value=seat), \
             mock.patch.object(process_identity, "current_launch_context", return_value=None):
            code, out, err = _run("ask", "--text-file", str(private), "--choice", "yes", "--choice", "no", "--recommend", "2", "--secret", "--json")
            self.assertEqual((code, err), (0, ""))
            item_id = json.loads(out)["id"]
            again = _run("ask", "--text-file", str(private), "--choice", "yes", "--choice", "no", "--recommend", "2", "--secret", "--json")
        self.assertEqual(item_id, json.loads(again[1])["id"])
        envelope = state.read_selected_request(item_id)
        question = questions.questions_of(envelope)[0]
        self.assertEqual((question["question"], question["recommended"], question["secret"]), (text, 1, True))
        self.assertNotIn(text, out)
        self.assertEqual(state._signal_path({"seat_key": seat.key}).stat().st_mode & 0o777, 0o600)
        self.assertEqual(state.state_root().stat().st_mode & 0o777, 0o700)

    def test_registration_notice_names_choice_answers_only_when_choices_exist(self):
        import session_tidy as tidy
        from fleet import process_identity
        with mock.patch("dispatch_parent_completion.interactive_parent_identity", return_value=("claude", "native-question-session")), \
             mock.patch.object(tidy, "resolve_seat", return_value=self.f.seat()), \
             mock.patch.object(process_identity, "current_launch_context", return_value=None), \
             mock.patch.dict(os.environ, {"HEARTING_LANG": "ko"}):
            plain = _run("ask", "free question")
            choice = _run("ask", "choice question", "--choice", "A", "--choice", "B")
        self.assertEqual((plain[0], choice[0]), (0, 0))
        self.assertIn(state.ui_translator()("questions.registered"), plain[1])
        self.assertIn(state.ui_translator()("questions.registered_choices"), choice[1])
        self.assertNotEqual(state.ui_translator()("questions.registered"),
                            state.ui_translator()("questions.registered_choices"))

    def test_one_line_cli_resolves_each_harness_without_a_supplied_sid_or_tag(self):
        import session_tidy as tidy
        from fleet import process_identity
        variables = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID",
                     "opencode": "OPENCODE_SESSION_ID"}
        for harness, variable in variables.items():
            sid = "0100000f-0000-7000-8000-000000000017" if harness == "codex" else "native-" + harness
            env = {name: "" for name in variables.values()}
            env.update({variable: sid, "AGENT_DISPATCH_CALLER_HARNESS": harness})
            with self.subTest(harness=harness), mock.patch.dict(os.environ, env), \
                 mock.patch.object(tidy, "resolve_seat", return_value=self.f.seat(harness, sid)) as resolve, \
                 mock.patch.object(process_identity, "current_launch_context", return_value=None):
                code, out, err = _run("ask", "one line", "--json")
            self.assertEqual((code, err), (0, ""))
            envelope = state.read_selected_request(json.loads(out)["id"])
            self.assertEqual((envelope["harness"], envelope["session_id"]), (harness, sid))
            self.assertEqual(resolve.call_args.args[0], harness)

    def test_cli_raw_file_answer_and_racing_claim_report_queue_separately(self):
        signal, envelope = self.f.ask()
        answer = "  한국어\n\nanswer 🙂\n"
        private = self.f.root / "answer.txt"
        private.write_text(answer)
        board = {"items": [{"id": signal["id"], "owner": {
            "harness": signal["harness"], "session_id": signal["session_id"]}}]}
        with mock.patch.object(questions, "load_board", return_value=board):
            code, out, _err = _run("answer", signal["id"], "--text-file", str(private))
        self.assertEqual(code, 0)
        self.assertIn("submitted", out)
        self.assertEqual(state.message_answer_body(self.f.record(signal)["receipt"]["answer_binding"]), answer)
        self.assertEqual(state.read_signals()[0]["lifecycle"], "open")

        signal, envelope = self.f.ask(text="racing CLI")
        board["items"][0]["id"] = signal["id"]
        submit = state.submit_selected_response
        def fleet_wins(ref, response, **kwargs):
            submit(envelope, {"answers": {"message-response": ["fleet"]}}, action_id="fleet-first")
            return submit(ref, response, **kwargs)
        with mock.patch.object(questions, "load_board", return_value=board), \
             mock.patch.object(state, "submit_selected_response", side_effect=fleet_wins):
            code, out, _err = _run("answer", signal["id"], "--text", "cli second")
        self.assertEqual(code, 1)
        self.assertIn("claimed-elsewhere", out)
        self.assertNotIn("already answered", out)

    def test_invalid_asks_write_nothing_and_worker_cannot_register(self):
        import session_tidy as tidy
        seat = self.f.seat()
        for text, opts in [("  ", {}), ("x" * 4097, {}), ("q", {"choices": ["x"] * 9}),
                           ("q", {"choices": ["A"], "recommended": 1})]:
            with self.subTest(opts=opts, size=len(text)), self.assertRaises(ValueError):
                state.register_message("claude", "native-question-session", seat, text, sequence=0, **opts)
        self.assertIsNone(state._signal_path({"seat_key": seat.key}))
        for env in ({"AGENT_SESSION_ROLE": "owner"}, {"AGENT_SESSION_ROLE": "reviewer"},
                    {"AGENT_DISPATCH_CHILD": "1"}, {"AGENT_DISPATCH_DEPTH": "2"}):
            with mock.patch.dict(os.environ, env):
                self.assertEqual(_run("ask", "q")[0], 2)
        with mock.patch("dispatch_parent_completion.interactive_parent_identity", return_value=("", "")):
            self.assertEqual(_run("ask", "q")[0], 2)
        tidy.bump_prompt_seq(seat, "claude", "native-question-session", 1)
        with self.assertRaisesRegex(ValueError, "prompt-changed"):
            state.register_message("claude", "native-question-session", seat, "q", sequence=0)

    def test_retry_stop_and_replacement_share_one_id_without_answering(self):
        s, e = self.f.ask()
        again, _ = self.f.ask()
        self.assertEqual((s["id"], s["since"]), (again["id"], again["since"]))
        state.write_selected_draft(e, "unresolved draft")
        marker = state.signal_value(s["harness"], s["session_id"], s["seat_key"], "[답변 필요] marker", sequence=7)
        self.assertFalse(state.write_signal(marker))
        replacement, _ = self.f.ask(text="new question")
        self.assertNotEqual(s["id"], replacement["id"])
        self.assertEqual(len(state.read_signals()), 1)
        old = state._selected_entry(e)
        self.assertEqual((old["state"], old["draft"], old["result"]["status"]),
                         ("cancelled", "unresolved draft", "replaced"))
        self.assertFalse(state.read_selected_request(e)["support"]["supported"])
        self.assertIsNone(self.f.record(s))

    def test_source_is_durable_and_public_projection_has_no_body(self):
        s, e = self.f.ask(choices=["PRIVATE-CHOICE"], recommended=0, secret=True)
        state.write_selected_draft(e, "PRIVATE-DRAFT")
        self.assertTrue(state.publish_cache({"observed_at": 1, "complete": True, "items": [{
            "id": s["id"], "source": "S4", "kind": "message", "since": s["since"],
            "question": s["question"], "choices": ["PRIVATE-CHOICE"], "draft": "PRIVATE-DRAFT",
            "owner": {"harness": s["harness"], "session_id": s["session_id"]}}]}))
        public = state._fleet_cache_path().read_text()
        for sentinel in (s["question"], "PRIVATE-CHOICE", "PRIVATE-DRAFT"):
            self.assertNotIn(sentinel, public)
        reopened = state.read_selected_request(s["id"])
        self.assertEqual(reopened["state"], "open")  # display TTL is not permission lifetime
        self.assertEqual(reopened["draft"], "PRIVATE-DRAFT")
        self.assertEqual(state.read_signals(now=s["since"] + 90 * 86400)[0]["id"], s["id"])

    def test_raw_answer_limits_private_receipt_and_tamper_rejection(self):
        import session_notice
        s, e = self.f.ask()
        for text in ("", "   ", "x" * 601, "가" * 601):
            with self.subTest(length=len(text)):
                self.f.submit(e, text)
                self.assertIsNone(self.f.record(s))
                self.assertIsNone(state._selected_entry(e).get("action_id"))
        text = "한국어  \n\n답 🙂\n" + "x" * 580
        result = self.f.submit(e, text)
        self.assertEqual(result["state"], "submitted")
        record = self.f.record(s)
        receipt = record["receipt"]
        encoded = json.dumps(receipt, separators=(",", ":"), sort_keys=True).encode()
        self.assertLessEqual(len(encoded), 2048)
        self.assertNotIn(text, encoded.decode())
        self.assertEqual(state.message_answer_body(receipt["answer_binding"]), text)
        self.assertTrue(session_notice.consumer_text(receipt).endswith(text))
        self.assertNotIn(text, session_notice.render_text(receipt))
        binding = dict(receipt["answer_binding"], body_sha256="0" * 64)
        with self.assertRaises(ValueError):
            state.message_answer_body(binding)
        self.assertEqual(state._read(state._signal_path(s))["lifecycle"], "open")

    def test_draft_utf8_boundaries_and_600_character_answer_boundary(self):
        s, e = self.f.ask()
        for size in (4095, 4096, 4097):
            text = "가" * (size // 3) + "x" * (size % 3)
            result = state.write_selected_draft(e, text)
            if size <= 4096:
                self.assertEqual(result["draft"], text)
            else:
                self.assertNotEqual(result.get("draft"), text)
        # Independent size limits: 600 Unicode characters fit at most 2400
        # UTF-8 bytes; the 4096-byte draft limit does not license 601 chars.
        for length in (599, 600, 601):
            s, e = self.f.ask(text="length " + str(length))
            result = self.f.submit(e, "🙂" * length, action="length-" + str(length))
            self.assertEqual(self.f.record(s) is not None, length <= 600)

    def test_same_precheck_two_submitters_make_one_claim_and_one_notice(self):
        _s, e = self.f.ask()
        barrier = threading.Barrier(2)
        submit = state._submit_message
        def synchronized(*args):
            barrier.wait(timeout=5)
            return submit(*args)
        with mock.patch.object(state, "_submit_message", side_effect=synchronized), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.f.submit, e, "first", "first-action")
            second = pool.submit(self.f.submit, e, "second", "second-action")
            results = [first.result(timeout=10), second.result(timeout=10)]
        actions = {result.get("action_id") for result in results}
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(__import__("peer_obligations").ObligationStore(self.f.root).list(kinds={"run-notice"})), 1)
        self.assertEqual(state.read_signals()[0]["lifecycle"], "open")

    def test_terminal_capacity_prunes_only_settled_entries_and_rejects_old_ids(self):
        first = None
        first_signal = None
        for index in range(257):
            s, e = self.f.ask(text="q " + str(index))
            first = first or e
            if index == 0:
                first_signal = s
                self.f.submit(e)
                wire, _ref, recipient = self.f.prepare(s)
                self.f.peer.receive_peer_message(wire, recipient)
            else:
                with __import__("session_tidy").seat_lock(s["seat_key"]):
                    self.assertTrue(state.close_signal(s["seat_key"], harness=s["harness"],
                        session_id=s["session_id"], sequence=s["sequence"], answer="done"))
        overlay = state._read_cache_file()["selected_responses"]
        self.assertEqual(len(overlay), 256)
        self.assertIsNone(state._selected_entry(first))
        self.assertFalse(state.read_selected_request(first)["support"]["supported"])
        self.assertEqual(self.f.record(first_signal)["state"], "acked")
        # Overlay pruning never removes the carrier's immutable receipt/ACK.
        state.flush_message_decisions()
        before = state._read(state._signal_path(s))
        self.f.peer.receive_peer_message(wire, recipient)
        self.assertEqual(state._read(state._signal_path(s)), before)

    def test_full_unresolved_overlay_keeps_source_and_draft(self):
        first = None
        for index in range(256):
            s, e = self.f.ask(text="unresolved " + str(index))
            state.write_selected_draft(e, "draft " + str(index))
            first = first or e
        before = state._signal_path(s).read_bytes()
        with self.assertRaisesRegex(ValueError, "capacity-unresolved"):
            self.f.ask(text="257")
        self.assertEqual(state._signal_path(s).read_bytes(), before)
        self.assertEqual(state._selected_entry(first)["draft"], "draft 0")

    def test_mixed_terminal_pruning_preserves_unresolved_draft_and_prune_submit_race(self):
        import session_tidy as tidy
        first, old = self.f.ask(text="unresolved old")
        state.write_selected_draft(old, "keep unresolved")
        for index in range(255):
            signal, _ = self.f.ask(text="settled " + str(index))
            with tidy.seat_lock(signal["seat_key"]):
                state.close_signal(signal["seat_key"], sequence=signal["sequence"],
                    harness=signal["harness"], session_id=signal["session_id"], answer="settled")
        signal, envelope = self.f.ask(text="race source")
        barrier = threading.Barrier(2)
        submit = state._submit_message
        def synchronized(*args):
            barrier.wait(timeout=5)
            return submit(*args)
        def replace():
            barrier.wait(timeout=5)
            return self.f.ask(text="race replacement")
        with mock.patch.object(state, "_submit_message", side_effect=synchronized), ThreadPoolExecutor(2) as pool:
            answer = pool.submit(self.f.submit, envelope)
            replacement = pool.submit(replace)
            answer.result(timeout=10)
            current, _ = replacement.result(timeout=10)
        self.assertEqual(state.read_signals()[0]["id"], current["id"])
        self.assertEqual(state._selected_entry(old)["draft"], "keep unresolved")
        record = self.f.record(signal)
        if record is not None:
            self.assertFalse(__import__("session_notice").notice_is_current(record))
        self.assertFalse(state.read_selected_request(envelope)["support"]["supported"])

    def test_durable_claim_survives_a_new_process_without_resend(self):
        import subprocess
        signal, envelope = self.f.ask()
        state.write_selected_draft(envelope, "private saved draft")
        self.f.submit(envelope)
        program = '''import json,sys
sys.path.insert(0,sys.argv[1])
import human_wait_state as h
ref=json.load(sys.stdin)
result=h.submit_selected_response(ref,{"answers":{"message-response":["retry"]}},action_id="retry-action")
print(json.dumps({"state":result["state"],"action_id":result.get("action_id"),"draft":result.get("draft")}))
'''
        child = subprocess.run([sys.executable, "-B", "-c", program, str(Path(state.__file__).parent)],
            input=json.dumps(envelope), text=True, capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        result = json.loads(child.stdout)
        self.assertEqual(result, {"state": "submitted", "action_id": "fixture-action", "draft": "private saved draft"})
        self.assertEqual(self.f.record(signal)["state"], "pending")

    def test_unreadable_overlay_is_preserved_and_partial_registration_retry_repairs_projection(self):
        signal, envelope = self.f.ask()
        state.write_selected_draft(envelope, "private draft")
        path = state._cache_path()
        original = path.read_bytes()
        path.write_text("{damaged")
        with self.assertRaisesRegex(ValueError, "storage-unreadable"):
            self.f.ask(text="cannot discard old overlay")
        self.assertEqual(path.read_text(), "{damaged")
        path.write_bytes(original)
        with mock.patch.object(state, "_write_cache_value", return_value=False):
            with self.assertRaises(OSError):
                self.f.ask(text="source written before projection")
        source = state._read(state._signal_path(signal))
        retried, _ = self.f.ask(text="source written before projection")
        self.assertEqual((source["id"], source["since"]), (retried["id"], retried["since"]))
        self.assertEqual(state._selected_entry(envelope)["draft"], "private draft")


if __name__ == "__main__":
    unittest.main()
