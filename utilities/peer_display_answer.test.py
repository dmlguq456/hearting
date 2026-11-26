from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
"""A question answer reaches the asking window as one line; the agent still gets the whole text.

The real retained courier builds the delivery, the producer's own builders make the line, and each
harness's own consumer (Claude hook, Codex hook, OpenCode plugin callback) reads what the window was
given. Fixtures and private roots only; no session, ledger or window of a real project is touched."""
import hashlib
import re
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import human_wait_state as state  # noqa: E402
import work_intent  # noqa: E402

SECRET_NOTE = "It answers a question asked as secret: use it, but do not repeat it in your replies or reports."


class QuestionAnswerLine(unittest.TestCase):
    def setUp(self):
        from fixtures.message_questions import MessageQuestionFixture
        self.fixture = MessageQuestionFixture()
        self.f = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        self.korean = work_intent._ui_text().translator("ko")
        patch = mock.patch.object(work_intent, "ui_translator", return_value=self.korean)
        patch.start()
        self.addCleanup(patch.stop)

    def source(self, signal):
        return state._read(state._signal_path(signal))

    def test_each_harness_reads_the_one_line_and_settles_the_answer_once(self):
        # Codex keeps its full text (its queue recovery reads the receipt marker lines); the
        # other two take the one line through their session-shell host.
        for harness in ("claude", "opencode"):
            with self.subTest(harness=harness):
                self.f.display = True
                signal, envelope = self.f.ask(harness, "line-" + harness)
                self.f.submit(envelope, "네, 그렇게 하세요")
                calls, ref, row = self.f.courier(signal)
                self.assertEqual(calls, ["↳ 질문에 답함: 네, 그렇게 하세요"])
                shown = calls[0]
                for noise in ("HEARTING_RECEIPT", "runtime-from", "request=S4:", "ref=", "sid=", "pane=",
                              "Hearting notice", "required_action"):
                    self.assertNotIn(noise, shown)
                self.assertIsNone(re.search(r"[0-9a-f]{32}", shown))
                self.assertEqual(row["state"], "queued")
                # the agent-only side: the same pending row carries the whole old text
                transfer = self.f.peer._read_transfer_record(ref)
                agent = transfer["display"]["agent"]
                self.assertIn("HEARTING_RECEIPT_V1 delivery-", agent)
                self.assertIn("request=S4:", agent)
                self.assertIn("(runtime-from: runtime [?] Hearting runtime ; ref=" + ref + ")", agent)
                guidance = self.f.peer.receiver_guidance(shown, {"harness": harness, "session_id": signal["session_id"]})
                self.assertIn(agent, guidance)
                # a copy of the line that was not sent to this session settles nothing
                self.f.consume(signal, shown + " ", sid="another-session-" + harness)
                self.assertEqual(self.source(signal)["lifecycle"], "open")
                # the harness's own consumer takes the line and settles the answer
                self.f.consume(signal, shown)
                self.assertEqual(self.source(signal)["completion"]["action_id"], "fixture-action")
                self.assertEqual(state._selected_entry(envelope)["state"], "applied")
                self.assertEqual(self.f.peer._read_pending(ref)["state"], "received")
                # the same prompt again changes nothing and writes no second receipt
                before = len([r for r in self.f.peer._iter_records() if r.get("transfer_ref") == ref])
                self.f.consume(signal, shown)
                self.assertEqual(before, len([r for r in self.f.peer._iter_records() if r.get("transfer_ref") == ref]))

    def test_the_same_short_answer_to_a_second_question_is_one_line_again(self):
        # A visible line is a record's key only while that record waits: once the first "네" is
        # received, the same line for the next question is a display form again, and its arrival
        # settles that question (not the earlier, received record).
        self.f.display = True
        first, first_envelope = self.f.ask("claude", "same-line", text="첫 질문")
        self.f.submit(first_envelope, "네")
        calls, ref1, _row1 = self.f.courier(first)
        self.assertEqual(calls, ["↳ 질문에 답함: 네"])
        self.f.consume(first, calls[0])
        self.assertEqual(self.source(first)["completion"]["action_id"], "fixture-action")
        self.assertEqual(self.f.peer._read_pending(ref1)["state"], "received")
        second, second_envelope = self.f.ask("claude", "same-line", text="둘째 질문")
        self.f.submit(second_envelope, "네")
        calls, ref2, _row2 = self.f.courier(second)
        self.assertEqual(calls, ["↳ 질문에 답함: 네"])
        self.assertNotEqual(ref1, ref2)
        self.assertEqual(self.f.peer._read_pending(ref2)["state"], "queued")
        recipient = {"harness": "claude", "session_id": "same-line"}
        self.assertEqual(self.f.peer._display_resolve(calls[0], recipient)["ref"], ref2)   # the waiting one
        self.f.consume(second, calls[0])
        self.assertEqual(self.source(second)["completion"]["action_id"], "fixture-action")
        self.assertEqual(self.f.peer._read_pending(ref2)["state"], "received")

    def test_a_secret_question_answer_is_hidden_in_the_line_and_kept_for_the_agent(self):
        # The line is the panel's own `•••`; the pending row's agent text still carries the answer,
        # and the answer settles as before. An ordinary answer keeps its text.
        self.f.display = True
        signal, envelope = self.f.ask("claude", "line-secret", text="토큰?", secret=True)
        self.f.submit(envelope, "SECRET-SENTINEL-VALUE")
        calls, ref, _row = self.f.courier(signal)
        self.assertEqual(calls, ["↳ 질문에 답함: •••"])
        self.assertNotIn("SECRET-SENTINEL-VALUE", calls[0])
        self.assertIn("SECRET-SENTINEL-VALUE", self.f.peer._read_transfer_record(ref)["display"]["agent"])
        context = self.f.consume(signal, calls[0])
        self.assertIn("SECRET-SENTINEL-VALUE", context)
        self.assertIn(SECRET_NOTE, context)   # use it, do not repeat it
        self.assertEqual(self.source(signal)["completion"]["action_id"], "fixture-action")
        plain, plain_envelope = self.f.ask("claude", "line-plain", text="토큰?")
        self.f.submit(plain_envelope, "공개 답")
        calls, _ref, _row = self.f.courier(plain)
        self.assertEqual(calls, ["↳ 질문에 답함: 공개 답"])
        context = self.f.consume(plain, calls[0])
        self.assertIn("공개 답", context)
        self.assertNotIn(SECRET_NOTE, context)

    def test_an_answer_whose_question_cannot_be_read_as_ordinary_is_hidden(self):
        import dispatch_session_sweep as sweep
        signal, envelope = self.f.ask("claude", "line-unreadable", text="질문")
        self.f.submit(envelope, "SECRET-SENTINEL-VALUE")
        record = self.f.record(signal)
        self.assertEqual(sweep.delivery_display_text([(self.f.root, [record])]), "↳ 질문에 답함: SECRET-SENTINEL-VALUE")
        self.assertFalse(sweep.delivery_holds_secret_answer([(self.f.root, [record])]))
        source = self.source(signal)
        for label, change in (("source gone", lambda: None),
                              ("flag missing", lambda: {**source, "detail": {**source["detail"], "questions": [
                                  {k: v for k, v in source["detail"]["questions"][0].items() if k != "is_secret"}]}}),
                              ("flag not a boolean false", lambda: {**source, "detail": {**source["detail"], "questions": [
                                  {**source["detail"]["questions"][0], "is_secret": None}]}}),
                              ("another request", lambda: {**source, "id": "S4:" + "0" * 64})):
            with self.subTest(label), mock.patch.object(state, "_read", return_value=change()):
                self.assertEqual(sweep.delivery_display_text([(self.f.root, [record])]), "↳ 질문에 답함: •••")
                # ... and its body, when no line is sealed, reaches the window only as a pointer.
                self.assertTrue(sweep.delivery_holds_secret_answer([(self.f.root, [record])]))

    def test_codex_keeps_the_full_text_and_settles_as_before(self):
        self.f.display = True
        signal, envelope = self.f.ask("codex", "line-codex")
        self.f.submit(envelope, "네")
        calls, ref, row = self.f.courier(signal)
        self.assertEqual(len(calls), 1)
        self.assertIn("HEARTING_RECEIPT_V1", calls[0])
        self.assertNotIn("display", self.f.peer._read_transfer_record(ref))
        self.f.consume(signal, calls[0])
        self.assertEqual(self.source(signal)["completion"]["action_id"], "fixture-action")

    def test_an_unsigned_window_gets_the_full_text_as_before(self):
        signal, envelope = self.f.ask("claude", "unsigned")
        self.f.submit(envelope, "네")
        calls, ref, _row = self.f.courier(signal)
        self.assertIn("HEARTING_RECEIPT_V1", calls[0])
        self.assertNotIn("display", self.f.peer._read_transfer_record(ref))
        self.f.consume(signal, calls[0])
        self.assertEqual(self.source(signal)["completion"]["action_id"], "fixture-action")

    # ---- a delivery whose one line is not sealed (11-26) ----
    # Every way the window gets the full form: Codex (always), a window that never showed it reads the
    # line or showed it more than two days ago, a batch with a notice that has no line, a folded notice,
    # and the same line still waiting for an earlier answer (every secret answer's line is `•••`).
    # OpenCode hands an ordinary notice to its own plugin carrier, so it never shares a batch
    # with an answer (no mixed or folded case there).
    CASES = (("codex", "marked"), ("codex", "mixed"), ("claude", "unsigned"), ("claude", "stale-sign"),
             ("claude", "mixed"), ("claude", "folded"), ("claude", "same-line-waiting"),
             ("opencode", "unsigned"), ("opencode", "stale-sign"), ("opencode", "same-line-waiting"))

    def deliver(self, harness, case, answer, secret, *, patches=()):
        import os
        import time
        import dispatch_notice_state
        import peer_obligations
        import session_notice
        from fixtures.message_questions import MessageQuestionFixture
        # Each case is its own machine: one courier pass serves one recipient, and nothing an
        # earlier case left stands in its way.
        fixture = MessageQuestionFixture()
        node = self.fixture.node          # found before the first fixture closed PATH
        self.f = fixture.__enter__()
        self.f.node = node
        self.addCleanup(fixture.__exit__, None, None, None)
        sid = "full-" + harness + "-" + case
        recipient = {"harness": harness, "session_id": sid}
        self.f.display = case not in ("unsigned", "stale-sign")
        if case == "stale-sign":
            self.f.peer.note_display_ready(recipient)
            old = time.time() - 3 * 86400
            os.utime(self.f.peer._display_ready_path(recipient), (old, old))
        if case == "same-line-waiting":
            # An earlier answer with the same line (every secret answer's is `•••`) was sent and is
            # not received yet; its notice duty has ended meanwhile, so the next answer's courier is
            # not held behind it.
            first, first_envelope = self.f.ask(harness, sid, text="첫 질문", secret=secret)
            self.f.submit(first_envelope, answer)
            calls, first_ref, _row = self.f.courier(first)
            self.assertEqual(calls, ["↳ 질문에 답함: " + ("•••" if secret else answer)])
            self.assertEqual(self.f.peer._read_pending(first_ref)["state"], "queued")
            store = peer_obligations.ObligationStore(self.f.root)
            for duty in store.list(kinds={"run-notice"}):
                if (duty.get("observation") or {}).get("transfer_ref") == first_ref:
                    store.update(duty["id"], state="complete", delivery="not-required", cleanup="complete")
        signal, envelope = self.f.ask(harness, sid, text="질문", secret=secret,
                                      **({"replace": True} if case == "same-line-waiting" else {}))
        self.f.submit(envelope, answer)
        patches = list(patches)
        if case in ("mixed", "folded"):
            self.assertIsNotNone(session_notice.notify(harness, sid, key="fixture-other-" + sid,
                subject="fixture notice", text="an ordinary notice", jobs=self.f.jobs))
        if case == "folded":
            keep = dispatch_notice_state.keep_claim
            def folding(root, storage, delivery_id, claim, owner, **kwargs):
                # The other notice is a run notice whose parent completion is already recorded.
                if "answer_binding" not in (claim.get("receipt") or {}):
                    return False
                return keep(root, storage, delivery_id, claim, owner, **kwargs)
            patches += [mock.patch.object(dispatch_notice_state, "keep_claim", side_effect=folding),
                        mock.patch.object(session_notice, "fold_summary",
                                          return_value="compute run summary: run r1 status=done exit=0")]
        for patch in patches:
            patch.start()
        try:
            calls, ref, _row = self.f.courier(signal)
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual(len(calls), 1, calls)
        transfer = self.f.peer._read_transfer_record(ref)
        self.assertNotIn("display", transfer)   # the full form's case: no one-line form was sealed
        return signal, calls[0], ref, transfer, self.f.bodies[-1]

    def assert_pointer(self, signal, window, ref, body, secret="SECRET-SENTINEL-VALUE"):
        # The window gets the header, the private copy's path, the receipt lines and the trailer;
        # the agent gets the very body the courier rendered through its own harness's context
        # (no file read), and the answer settles.
        self.assertNotIn(secret, window)
        self.assertIn(secret, body)
        payload = self.f.peer.read_peer_payload(ref)
        self.assertEqual(payload["body"], body)
        self.assertEqual(payload["descriptor"]["presentation_mode"], "private-answer")
        self.assertEqual(Path(payload["path"]).stat().st_mode & 0o777, 0o600)
        markers = [line for line in body.splitlines() if line.startswith("HEARTING_RECEIPT_V1 ")]
        self.assertTrue(markers)
        self.assertEqual(window, "\n".join([
            "Hearting delivery holding a private answer: the person's window shows only this pointer. "
            "Its full text comes to you as context with this message; the same text is in the file below.",
            "`" + payload["path"] + "`", *markers])
            + "\n\n(runtime-from: runtime [?] Hearting runtime ; ref=" + ref + ")")
        context = self.f.consume(signal, window)
        self.assertIn("Hearting delivery (the person sees only the pointer above); the full text:\n"
                      + body + "\n\n" + SECRET_NOTE, context)
        # The panel's received-message line takes the payload's first line: the delivery header.
        import session_tidy
        line = session_tidy._received_prompt_line(window, {"transfer_ref": ref})
        self.assertEqual(line, body.splitlines()[0])
        self.assertNotIn(secret, line)
        self.assertEqual(self.source(signal)["completion"]["action_id"], "fixture-action")
        self.assertEqual(self.f.peer._read_pending(ref)["state"], "received")
        return context

    def test_a_secret_answer_without_its_line_shows_the_window_only_a_pointer(self):
        for harness, case in self.CASES:
            with self.subTest(harness=harness, case=case):
                signal, window, ref, transfer, body = self.deliver(harness, case, "SECRET-SENTINEL-VALUE", True)
                context = self.assert_pointer(signal, window, ref, body)
                if case in ("mixed", "folded"):
                    # the batch's other notice reaches the agent with it
                    self.assertIn("an ordinary notice" if case == "mixed" else "compute run summary", context)

    def test_a_secret_answer_of_any_shape_stays_out_of_the_window(self):
        # Several lines, a long answer, control characters, a path-like answer.
        for label, secret in (("lines", "SECRET-ML-1\nSECRET-ML-2\nSECRET-ML-3"),
                              ("long", "SECRET-LONG-" + "x" * 3000),
                              ("control", "SECRET-CTRL-\x1b[31m\tend"),
                              ("path", "/home/someone/.ssh/SECRET-PATH-id")):
            with self.subTest(label):
                signal, window, ref, transfer, body = self.deliver("codex", "marked-" + label, secret, True)
                for piece in secret.split("\n"):
                    self.assertNotIn(piece, window)
                self.assert_pointer(signal, window, ref, body, secret=secret.split("\n")[0])

    def test_a_payload_that_cannot_be_written_sends_nothing_then_the_pointer(self):
        import peer_steward_base
        failing = mock.patch.object(peer_steward_base.peer_message, "_write_body_payload",
                                    side_effect=OSError("fixture: payload directory unavailable"))
        from fixtures.message_questions import MessageQuestionFixture
        fixture = MessageQuestionFixture()
        self.f = fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        signal, envelope = self.f.ask("claude", "payload-fails", text="질문", secret=True)
        self.f.submit(envelope, "SECRET-SENTINEL-VALUE")
        with failing:
            calls, ref, _row = self.f.courier(signal)
        self.assertEqual(calls, [])       # nothing reaches the window in full
        calls, ref, _row = self.f.courier(signal)
        self.assertEqual(len(calls), 1)
        self.assert_pointer(signal, calls[0], ref, self.f.bodies[-1])

    def test_an_ordinary_answers_full_form_is_unchanged(self):
        for harness, case in self.CASES:
            with self.subTest(harness=harness, case=case):
                signal, window, ref, transfer, body = self.deliver(harness, case, "공개 답", False)
                self.assertNotIn("body_ref", transfer)
                self.assertEqual(window, body + "\n\n(runtime-from: runtime [?] Hearting runtime ; ref=" + ref + ")")
                self.assertIn("\n공개 답\n", window)
                context = self.f.consume(signal, window)
                self.assertNotIn(SECRET_NOTE, context)
                self.assertEqual(self.source(signal)["completion"]["action_id"], "fixture-action")


if __name__ == "__main__":
    unittest.main()
