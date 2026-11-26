"""11-26: a written answer holding a receipt marker line (a piece of a session record pasted into it) is delivered,
received once and read as Hearting's delivery, never as the person's prompt, on each harness's carrier.

An answer is the person's words and goes as written. Before, a line with `HEARTING_RECEIPT_V1` in it made a bare
delivery (the OpenCode turn carrier's promptAsync body, any carrier's text in the prompt hook) read as the person's own
prompt, and OpenCode's history check could not find an earlier delivery of it (so it could be sent again). The real
courier, claim, delivery text, native receipt checks and prompt hook run on private roots; no model or window is used.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dispatch_pending_delivery as pending  # noqa: E402
import dispatch_recipient_receipt as rr  # noqa: E402
import dispatch_session_sweep as sweep  # noqa: E402
import human_wait_state as state  # noqa: E402
import session_tidy as tidy  # noqa: E402

FAKE = "HEARTING_RECEIPT_V1 delivery-0123abcd " + "a" * 64
# A line that is a whole marker, one with a marker inside it, an empty line, a tab, Korean and a long line.
ANSWER = "세션 기록:\n" + FAKE + "\n기록에 " + FAKE + " 가 보였다\n\n\t탭 줄\n" + "긴" * 300
KIND = {"claude": "claude-parent-runtime", "codex": "codex-native-queue", "opencode": "opencode-turn"}


class AnswerReceiptMarker(unittest.TestCase):
    def setUp(self):
        from fixtures.message_questions import MessageQuestionFixture
        self.fixture = MessageQuestionFixture()
        self.f = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)

    def source(self, signal):
        return state._read(state._signal_path(signal))

    def prompt_source(self, harness, sid, text):
        """What the real prompt hook takes ``text`` for: a runtime source, or ``real`` (the person's own prompt)."""
        seat = self.f.seat(harness, sid)
        with mock.patch.object(tidy, "resolve_seat", return_value=seat):
            tidy.run_hook(harness, "prompt", sid, cwd=str(self.f.root), prompt_text=text, env=os.environ,
                          render_context=False)
        boundary = tidy.read_json(tidy._prompt_seq_path(tidy.session_seat(seat, harness, sid)))
        return boundary.get("source")

    def claimed_text(self, harness, sid, signal):
        """The bare delivery each harness's own carrier sends: the claimed record's delivery text."""
        delivery = self.f.record(signal)["delivery_id"]
        claimed, _count = sweep.sweep_deliver(self.f.root, KIND[harness], sid)
        claimed = [record for record in claimed if record["delivery_id"] == delivery]
        if not claimed:   # Claude's sweep refuses the generation proof; its Stop carrier claims as here
            claim = pending.claim(self.f.root, sid, delivery, claim_owner="claude-stop:fixture", lease_seconds=30,
                                  require_generation_proof=False, jobs=self.f.jobs)
            claimed = [{**claim, sweep.STORAGE_KEY: sid, rr.IDENTITY_KEY: pending.received_ack_identity(claim, sid, delivery)}]
        return sweep.delivery_context([(self.f.root, claimed)])

    def test_the_courier_delivers_it_as_written_and_it_is_received_once(self):
        for harness, display in (("claude", True), ("claude", False), ("codex", True), ("opencode", True),
                                 ("opencode", False)):
            with self.subTest(harness=harness, display=display):
                self.f.display = display
                sid = f"courier-{harness}-{display}"
                signal, envelope = self.f.ask(harness, sid)
                self.f.submit(envelope, ANSWER)
                calls, ref, _row = self.f.courier(signal)
                self.assertEqual(len(calls), 1)
                agent = (self.f.peer._read_transfer_record(ref).get("display") or {}).get("agent") or calls[0]
                self.assertIn(ANSWER, agent, "the agent reads the answer exactly as written")
                self.f.consume(signal, calls[0])
                completion = self.source(signal)["completion"]
                self.assertEqual((completion["actor"], completion["answer"]), ("fleet", ANSWER))
                self.assertEqual(self.f.peer._read_pending(ref)["state"], "received")
                self.assertEqual(self.f.record(signal)["state"], "acked")
                self.assertEqual(self.prompt_source(harness, sid, calls[0]), "peer")
                with self.assertRaises(StopIteration):   # no duty is left to deliver it again
                    self.f.courier(signal)

    def test_a_bare_delivery_with_a_marker_line_in_its_answer_is_the_runtime_and_received(self):
        for harness in ("claude", "codex", "opencode"):
            for answer in (ANSWER, "HEARTING_RECEIPT_V1 이 뭐야?"):     # the one-line answer reached it too
                with self.subTest(harness=harness, answer=answer[:12]):
                    sid = f"bare-{harness}-{len(answer)}"
                    signal, envelope = self.f.ask(harness, sid)
                    self.f.submit(envelope, answer)
                    text = self.claimed_text(harness, sid, signal)
                    self.assertIn(answer, text, "the answer goes as written")
                    record = self.f.record(signal)
                    self.assertEqual(rr.bound_delivery_ids(text, sid, [self.f.root]), [record["delivery_id"]])
                    self.assertEqual(self.prompt_source(harness, sid, text), "runtime", "never the person's prompt")
                    identity = pending.received_ack_identity(record, sid, record["delivery_id"])
                    if harness == "claude":
                        event = {"type": "user", "sessionId": sid, "isMeta": True,
                                 "message": {"content": rr.CLAUDE_STOP_FEEDBACK_PREFIX + "\n" + text}}
                        evidence = rr.claude_feedback_evidence([event], sid)
                    elif harness == "codex":
                        evidence = rr.codex_consumed_evidence(session_id=sid, client_message_id="c1",
                                                              confirmed_client_message_id="c1", expected_message=text)
                    else:
                        messages = [{"info": {"sessionID": sid, "role": "user"}, "parts": [{"type": "text", "text": text}]}]
                        # The earlier delivery is found again in the session's own history (no second send).
                        rebuilt = sweep._opencode_delivery_expectations([self.f.root], KIND[harness], sid, messages)
                        self.assertEqual([item["text"] for item in rebuilt], [text])
                        evidence = rr.opencode_messages_evidence(messages, sid, rebuilt)
                    outcome = rr.settle_native_receipt(self.f.root, sid, harness, sid,
                                                       pending.read(self.f.root, sid, record["delivery_id"]), evidence,
                                                       acked_by="fixture")
                    self.assertTrue(outcome["received"], outcome)
                    self.assertEqual(pending.read(self.f.root, sid, record["delivery_id"])["state"], "acked")
                    self.assertEqual(identity, pending.received_ack_identity(
                        pending.read(self.f.root, sid, record["delivery_id"]), sid, record["delivery_id"]))

    def test_a_copied_marker_names_nothing_and_a_persons_own_words_stay_the_persons(self):
        harness, sid = "opencode", "copied-marker"
        first, first_envelope = self.f.ask(harness, sid, text="earlier")
        self.f.submit(first_envelope, "earlier answer", action="first-action")
        old = self.f.record(first)
        claim = pending.claim(self.f.root, sid, old["delivery_id"], claim_owner="fixture-earlier", lease_seconds=30,
                              require_generation_proof=False, jobs=self.f.jobs)
        sweep.ack_delivered(self.f.root, sid, [{**claim, sweep.STORAGE_KEY: sid}], acked_by="fixture-earlier")
        signal, envelope = self.f.ask(harness, sid)
        # A line copied from the earlier delivery to this very session: inside the answer it names no member.
        self.f.submit(envelope, "앞서 받은 것:\n" + rr.receipt_marker(old, sid) + "\n끝")
        text = self.claimed_text(harness, sid, signal)
        self.assertEqual(rr.bound_delivery_ids(text, sid, [self.f.root]), [self.f.record(signal)["delivery_id"]])
        # The person's own words around a delivery, or a changed answer, are the person's, as before.
        self.assertEqual(rr.bound_delivery_ids(text + "\n그리고 내 말", sid, [self.f.root]), [])
        self.assertEqual(rr.bound_delivery_ids(text.replace("\n끝\n", "\n끝\n" + FAKE + "\n"), sid, [self.f.root]), [])
        self.assertEqual(self.prompt_source(harness, sid, "내 말\n" + FAKE), "real")


if __name__ == "__main__":
    unittest.main()
