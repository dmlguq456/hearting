"""F-48 structured Codex request_user_input call/output pairing."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet.collectors import codex  # noqa: E402
from fleet.collectors import interaction, liveness  # noqa: E402
from fleet.model import Session  # noqa: E402


def record(payload, timestamp="2026-08-03T00:00:00Z"):
    return {"timestamp": timestamp, "type": "response_item", "payload": payload}


class CodexPendingTest(unittest.TestCase):
    def setUp(self):
        codex._LIFECYCLE_CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "rollout.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, *rows):
        with open(self.path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(row if isinstance(row, str) else json.dumps(row))
                handle.write("\n")

    def call(self, call_id="c1", name="request_user_input", timestamp="2026-08-03T00:00:00Z"):
        return record({"type": "function_call", "name": name, "call_id": call_id}, timestamp)

    def output(self, call_id="c1"):
        return record({"type": "function_call_output", "call_id": call_id})

    def async_call(self, call_id="async", count=1, **extra):
        return record({"type": "function_call", "name": "request_user_input_async",
                       "call_id": call_id, "arguments": json.dumps({
                           "questions": [{"title": "PRIVATE QUESTION"}] * count,
                       }), **extra})

    def accepted(self, call_id="async", value=True):
        return record({"type": "function_call_output", "call_id": call_id,
                       "output": json.dumps({"accepted": value})})

    def reply(self, call_id="async", index=0, answer="PRIVATE ANSWER", role="user"):
        body = json.dumps([{"question": "PRIVATE QUESTION", "answer": answer,
                            "questionItemId": json.dumps([
                                "request_user_input_async", call_id, index])}])
        return record({"type": "message", "role": role, "content": [{
            "type": "input_text", "text": "<send_user_message_question_reply>\n" + body
            + "\n</send_user_message_question_reply>",
        }]})

    def event(self, kind, turn="turn"):
        return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn}}

    def user_prompt(self, client_id, text="PRIVATE TYPED REPLY"):
        """The composer-submit event the TUI writes beside the response_item message."""
        item = {"type": "UserMessage", "id": "u", "content": [
            {"type": "text", "text": text, "text_elements": []}]}
        if client_id is not None:
            item["client_id"] = client_id
        return {"type": "event_msg", "payload": {"type": "item_completed", "item": item}}

    def reply_text(self, index=0, call_id="async"):
        return self.reply(call_id, index)["payload"]["content"][0]["text"]

    def append(self, *rows):
        with open(self.path, "a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def append(self, *rows):
        with open(self.path, "a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def event(self, kind, turn="t1"):
        return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn}}

    def test_open_and_closed_call(self):
        self.write(self.call())
        pending = codex._tail_pending_request_user_input(self.path)
        self.assertEqual(pending["call_id"], "c1")
        self.assertIsInstance(pending["waiting_since"], float)
        self.write(self.call(), self.output())
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_prose_and_other_function_are_not_evidence(self):
        prose = record({"type": "message", "content": "request_user_input should be used"})
        self.write(prose, self.call(name="exec_command"))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_call_id_reuse_reopens_after_output(self):
        self.write(
            self.call("same", timestamp="2026-08-03T00:00:00Z"),
            self.output("same"),
            self.call("same", timestamp="2026-08-03T00:00:10Z"),
        )
        pending = codex._tail_pending_request_user_input(self.path)
        self.assertEqual(pending["call_id"], "same")
        self.assertGreater(pending["waiting_since"], 0)

    def test_latest_open_call_wins_and_malformed_is_skipped(self):
        self.write("{broken", self.call("old"), self.call("new"), "not json")
        self.assertEqual(codex._tail_pending_request_user_input(self.path)["call_id"], "new")

    def test_absent_file_is_silent(self):
        self.assertIsNone(codex._tail_pending_request_user_input(self.path + ".missing"))

    def test_live_async_acceptance_is_not_an_answer_and_exact_reply_clears(self):
        # Observed 2026-10-01: the tool returns accepted:true immediately, while
        # the reply arrives later as a user message with a JSON questionItemId.
        self.write(self.event("task_started"), self.async_call(), self.accepted())
        self.assertEqual(codex._tail_pending_request_user_input(self.path)["call_id"], "async")
        self.append(self.event("task_complete"))
        self.assertEqual(codex._tail_pending_request_user_input(self.path)["call_id"], "async")
        self.append(self.reply())
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_relayed_async_question_closes_when_a_later_turn_starts(self):
        # Real runtime shape from the [bb] rollout: acceptance is followed by a
        # peer-injected user message, then the session starts another turn.
        injected = record({"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "supervisor relay: keep waiting"},
        ]})
        self.write(self.event("task_started", "asking"), self.async_call(),
                   self.accepted(), self.event("task_complete", "asking"), injected)
        self.assertEqual(codex._tail_pending_request_user_input(self.path)["call_id"], "async")
        self.append(self.event("task_started", "following"), self.event("task_complete", "following"))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_unanswered_async_question_without_later_turn_stays_pending(self):
        self.write(self.event("task_started", "asking"), self.async_call(), self.accepted(),
                   self.event("task_complete", "asking"))
        self.assertEqual(codex._tail_pending_request_user_input(self.path)["call_id"], "async")

    def test_partial_and_duplicate_replies_do_not_close_other_questions(self):
        self.write(self.async_call(count=2), self.accepted(), self.reply(index=0))
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
        self.append(self.reply(index=0), self.reply(call_id="foreign"), self.reply(index=9))
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
        self.append(self.reply(index=1))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_empty_reply_resolves_display_without_implying_a_user_decision(self):
        self.write(self.async_call(), self.accepted(), self.reply(answer=""))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_async_failure_clears_only_the_exact_call(self):
        self.write(self.async_call("old"), self.accepted("old"),
                   self.async_call("new"), self.accepted("new", value=False))
        self.assertEqual(codex._tail_pending_request_user_input(self.path)["call_id"], "old")
        self.append(self.output("old"))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_only_originating_turn_interruption_clears_async_wait(self):
        self.write(self.event("task_started"), self.async_call(), self.accepted(),
                   self.event("turn_aborted", "foreign"))
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
        self.append(self.event("turn_aborted"))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_turn_identity_from_native_metadata_beats_latest_lifecycle(self):
        self.write(self.event("task_started", "unrelated"), self.async_call(
            internal_chat_message_metadata_passthrough={"turn_id": "origin"}),
            self.accepted(), self.event("turn_aborted", "unrelated"))
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
        self.append(self.event("turn_aborted", "origin"))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_assistant_quotes_and_malformed_reply_envelopes_are_not_answers(self):
        self.write(self.async_call(), self.accepted(), self.reply(role="assistant"))
        malformed = self.reply()
        malformed["payload"]["content"][0]["text"] = "<send_user_message_question_reply>oops</send_user_message_question_reply>"
        self.append(malformed, record({"type": "message", "role": "user",
                                      "content": [{"type": "input_text", "text": "yes"}]}))
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))

    def test_cursor_keeps_no_question_or_answer_content(self):
        self.write(self.async_call(count=2), self.accepted(), self.reply(index=0))
        codex._tail_pending_request_user_input(self.path)
        pending = codex._LIFECYCLE_CACHE[os.path.realpath(self.path)].pending_calls
        self.assertNotIn("PRIVATE", repr(pending))
        self.assertEqual(pending["async"].questions, frozenset({1}))

    def test_typed_reply_after_turn_end_clears_async_question(self):
        # Observed 2026-10-02: the box is dropped at turn end and the answer is an ordinary message.
        self.write(self.event("task_started"), self.async_call(), self.accepted(),
                   self.event("task_complete"))
        self.assertEqual(codex._tail_pending_request_user_input(self.path)["call_id"], "async")
        self.append(self.user_prompt("3b21a49f-2a77-4c1d-9a0e-5d3f6a7b8c90"))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_completion_delivery_prompt_keeps_async_question(self):
        self.write(self.event("task_started"), self.async_call(), self.accepted(),
                   self.event("task_complete"), self.user_prompt("delivery-" + "ab" * 32))
        self.assertEqual(codex._tail_pending_request_user_input(self.path)["call_id"], "async")

    def test_prompt_before_question_in_same_turn_does_not_clear_it(self):
        self.write(self.user_prompt("3b21a49f-2a77-4c1d-9a0e-5d3f6a7b8c90"),
                   self.event("task_started"), self.async_call(), self.accepted())
        self.assertEqual(codex._tail_pending_request_user_input(self.path)["call_id"], "async")

    def test_structured_reply_prompt_clears_only_the_answered_index(self):
        self.write(self.async_call(count=2), self.accepted(),
                   self.user_prompt("3b21a49f-2a77-4c1d-9a0e-5d3f6a7b8c90", self.reply_text(0)),
                   self.reply(index=0))
        self.assertEqual(codex._tail_pending_request_user_input(self.path)["call_id"], "async")
        pending = codex._LIFECYCLE_CACHE[os.path.realpath(self.path)].pending_calls
        self.assertEqual(pending["async"].questions, frozenset({1}))

    def test_prompt_without_client_id_is_unchanged(self):
        self.write(self.event("task_started"), self.async_call(), self.accepted(),
                   self.event("task_complete"), self.user_prompt(None))
        self.assertEqual(codex._tail_pending_request_user_input(self.path)["call_id"], "async")

    def test_prompt_leaves_a_blocking_call_to_its_own_rules(self):
        self.write(self.event("task_started"), self.call("blocking"),
                   self.user_prompt("3b21a49f-2a77-4c1d-9a0e-5d3f6a7b8c90"))
        self.assertEqual(codex._tail_pending_request_user_input(self.path)["call_id"], "blocking")

    def test_prompt_clear_keeps_no_typed_content(self):
        self.write(self.async_call(), self.accepted(),
                   self.user_prompt("3b21a49f-2a77-4c1d-9a0e-5d3f6a7b8c90"))
        codex._tail_pending_request_user_input(self.path)
        self.assertNotIn("PRIVATE", repr(codex._LIFECYCLE_CACHE[os.path.realpath(self.path)].pending_calls))

    def test_namespaced_calls_and_malformed_async_requests(self):
        self.write(self.async_call(name="functions.request_user_input_async"), self.accepted())
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
        self.write(record({"type": "function_call", "name": "request_user_input_async",
                           "call_id": "broken", "arguments": "{broken"}),
                   record({"type": "function_call", "name": ["request_user_input_async"],
                           "call_id": "invalid-name"}))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_partial_append_resolves_only_after_complete_reply(self):
        self.write(self.async_call(), self.accepted())
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
        reply = json.dumps(self.reply()) + "\n"
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(reply[:len(reply) // 2])
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(reply[len(reply) // 2:])
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_question_outside_tail_survives_unrelated_activity_without_rescan(self):
        self.write(self.event("task_started"), self.call(),
                   record({"type": "message", "content": "x" * 150000}))
        self.assertEqual(codex._tail_pending_request_user_input(self.path)["call_id"], "c1")
        with mock.patch.object(codex, "_read_lifecycle_range", wraps=codex._read_lifecycle_range) as read:
            self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
            read.assert_not_called()
            offset = os.path.getsize(self.path)
            self.append(self.output("foreign"))
            self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
            self.assertEqual(read.call_args.args[1], offset)
        self.append(self.output())
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_turn_boundaries_clear_abandoned_question(self):
        for kind in ("task_complete", "turn_aborted", "task_started"):
            with self.subTest(kind=kind):
                codex._LIFECYCLE_CACHE.clear()
                self.write(self.event("task_started"), self.call())
                self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
                self.append(self.event(kind))
                self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_foreign_turn_terminal_does_not_answer_question(self):
        self.write(self.event("task_started"), self.call(),
                   self.event("task_complete", "other"), self.event("turn_aborted", "other2"))
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
        self.append(self.event("task_complete"))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_partial_output_does_not_clear_until_complete_line(self):
        self.write(self.call())
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
        output = json.dumps(self.output())
        with open(self.path, "a") as handle:
            handle.write(output)
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
        with open(self.path, "a") as handle:
            handle.write("\n")
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_rotation_drops_old_question(self):
        self.write(self.call())
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
        os.rename(self.path, self.path + ".old")
        self.write(self.event("task_started", "new"))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_enrichment_blocks_without_gateway_and_unblocks_after_answer(self):
        sid = "01234567-89ab-cdef-0123-456789abcdef"
        self.path = os.path.join(self.tmp.name, "rollout-test-%s.jsonl" % sid)
        self.write(self.event("task_started"), self.call())
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": self.tmp.name}), \
             mock.patch.object(codex, "_config_model_effort", return_value=(None, None)), \
             mock.patch.object(codex, "_thread_titles", return_value={}), \
             mock.patch.object(codex, "_thread_subagents", return_value={}), \
             mock.patch.object(codex, "_tail_token_count", return_value=None), \
             mock.patch.object(liveness, "_alive", return_value=True):
            for answered in (False, True):
                if answered:
                    self.append(self.output())
                sess = Session(harness="codex", pid=43210, cwd=self.tmp.name)
                with mock.patch.dict(codex._PROC_PATHS, {sess.pid: self.path}):
                    codex.enrich(sess)
                interaction.enrich(sess)
                state = liveness.classify(sess, now=os.path.getmtime(self.path) + 1)
                self.assertEqual(state, "working" if answered else "blocked")
                if not answered:
                    self.assertEqual(set(sess.interaction_state), {"kind", "source", "waiting_since"})
                    self.assertEqual(sess.interaction_state["source"], "codex-rollout")


if __name__ == "__main__":
    unittest.main()
