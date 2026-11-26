from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
"""A question answer reaches the asking window as one line; the agent still gets the whole text.

The real retained courier builds the delivery, the producer's own builders make the line, and each
harness's own consumer (Claude hook, Codex hook, OpenCode plugin callback) reads what the window was
given. Fixtures and private roots only; no session, ledger or window of a real project is touched."""
import hashlib
import json
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
    # OpenCode, even when its window reads the one-line form: a secret answer takes the pointer.
    SECRET_ONLY_CASES = (("opencode", "marked"),)

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
            if secret and harness == "opencode":
                # OpenCode never gets a secret answer's one-line form: the earlier one is a pointer too.
                self.assertEqual(len(calls), 1)
                self.assertNotIn(answer, calls[0])
            else:
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
        for harness, case in self.CASES + self.SECRET_ONLY_CASES:
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

    # ---- the hook and plugin boundaries that take a waiting row (11-26, second follow-up) ----
    def waiting(self, harness, sid, answer, *, display=False):
        """A secret answer for a session with no shell host: it waits for that session's own
        callbacks (nothing reaches any window)."""
        from fixtures.message_questions import MessageQuestionFixture
        fixture = MessageQuestionFixture()
        node = self.fixture.node
        self.f = fixture.__enter__()
        self.f.node = node
        self.addCleanup(fixture.__exit__, None, None, None)
        self.f.display, self.f.shell = display, False
        signal, envelope = self.f.ask(harness, sid, text="질문", secret=True)
        self.f.submit(envelope, answer)
        calls, ref, row = self.f.courier(signal)
        self.assertEqual(calls, [])
        self.assertEqual(row["state"], "pending")
        return signal, ref, row, self.f.bodies[-1]

    def settled(self, signal, ref):
        self.assertEqual(self.source(signal)["completion"]["action_id"], "fixture-action")
        self.assertEqual(self.f.peer._read_pending(ref)["state"], "received")

    def test_a_claude_boundary_that_takes_a_waiting_secret_answer_gives_its_full_text(self):
        # A tool boundary, or a prompt of the person that is not the delivery: the hook context
        # (the agent's alone) carries the full text beside the pointer; the receipt records the row.
        for mode in ("post-tool", "prompt"):
            for form in ("pointer", "sealed"):
                with self.subTest(mode=mode, form=form):
                    sid = "boundary-%s-%s" % (mode, form)
                    signal, ref, row, body = self.waiting("claude", sid, "SECRET-SENTINEL-VALUE",
                                                          display=form == "sealed")
                    transfer = self.f.peer._read_transfer_record(ref)
                    self.assertEqual("display" in transfer, form == "sealed")
                    context = self.f.claude_boundary(sid, mode=mode)
                    if form == "pointer":
                        self.assertIn(row["text"] + "\n\nHearting delivery (the person sees only the pointer "
                                      "above); the full text:\n" + body + "\n\n" + SECRET_NOTE, context)
                    else:
                        self.assertIn(row["text"] + "\n\n" + SECRET_NOTE, context)
                        self.assertIn("SECRET-SENTINEL-VALUE", row["text"])
                    self.settled(signal, ref)

    def test_an_opencode_tool_boundary_gives_the_tool_result_the_pointer_and_the_agent_the_text(self):
        # The tool result, which OpenCode's TUI may draw, gets the pointer alone; the full text goes
        # to the plugin's system context for the tool loop's next model call; then the row settles.
        signal, ref, row, body = self.waiting("opencode", "tool-result", "SECRET-SENTINEL-VALUE", display=True)
        self.assertNotIn("display", self.f.peer._read_transfer_record(ref))   # OpenCode: pointer form
        appended, system = self.f.opencode_tool_boundary("tool-result")
        self.assertEqual(appended, row["text"])
        self.assertNotIn("SECRET-SENTINEL-VALUE", appended)
        self.assertIn("the full text:\n" + body + "\n\n" + SECRET_NOTE, system)
        self.settled(signal, ref)

    def idle(self, sid, *, second=False):
        signal, ref, row, body = self.waiting("opencode", sid, "SECRET-SENTINEL-VALUE")
        other = None
        if second:   # an ordinary peer message waits behind it
            _text, other = self.f.peer.prepare_peer_message("an ordinary peer message", {
                "harness": "codex", "session_id": "0100000a-0000-7000-8000-000000000012", "name": "fixture-peer"},
                {"harness": "opencode", "session_id": sid}, defer=True)
        return signal, ref, row, body, other

    def assert_model_calls(self, result, body, pointer, *, other=None):
        """Every model call that answers the pointer's message carries its full text; one that
        answers the other (ordinary) message carries that message's own guidance and no secret,
        whichever of the two the plugin took first."""
        block = "the full text:\n" + body + "\n\n" + SECRET_NOTE
        pointer_ids = {m["id"] for m in result["history"] if m["role"] == "user" and m["text"] == pointer}
        self.assertTrue(pointer_ids, result["history"])
        calls = [call for call in result["systems"] if call["for"] in pointer_ids]
        self.assertTrue(calls, result["systems"])
        for call in calls:
            self.assertIn(block, call["system"])
        if other is not None:
            other_ids = {m["id"] for m in result["history"] if m["role"] == "user" and m["text"].startswith(other)}
            other_calls = [call for call in result["systems"] if call["for"] in other_ids]
            self.assertTrue(other_calls, result["systems"])
            for call in other_calls:
                self.assertIn("Sender of the message above", call["system"])
                self.assertNotIn("SECRET-SENTINEL-VALUE", call["system"])
        self.assertFalse(any("SECRET-SENTINEL-VALUE" in m["text"] for m in result["history"]))

    def test_the_opencode_idle_delivery_resumes_each_saved_answer_with_its_own_full_text(self):
        # The plugin's own `session.idle` delivery (`pendingPeerDelivery`) saves the pointer and resumes
        # it: that resume's model call carries the full text, even when it reads the system context
        # late (one resume per idle, so no later save replaces it), and only then the row settles.
        for case, second, delay in (("single", False, 0), ("two-late", True, 30)):
            with self.subTest(case):
                signal, ref, row, body, other = self.idle("idle-" + case, second=second)
                result = self.f.opencode_plugin_session("idle-" + case, {"idles": 3, "delay": delay})
                self.assert_model_calls(result, body, row["text"],
                                        other="an ordinary peer message" if second else None)
                self.settled(signal, ref)
                if other:
                    self.assertEqual(self.f.peer._read_pending(other)["state"], "received")

    def test_the_opencode_recovery_resume_sets_the_full_text_itself(self):
        # Saved and queued by an earlier plugin process; a fresh process, after a prompt of the
        # person, resumes it: the guidance comes from the row itself, not from the save.
        import dispatch_contract
        import subprocess
        import sys
        import time
        signal, ref, row, body, other = self.idle("idle-restart", second=True)
        tool = Path(self.f.peer.__file__)
        me = [str(os.getpid()), str(dispatch_contract.process_start_ticks(os.getpid()))]

        def cli(*args):
            done = subprocess.run([sys.executable, "-B", str(tool), "pending", "--to-harness", "opencode",
                                   "--to-session-id", "idle-restart", *args], capture_output=True, text=True,
                                  timeout=60, env=dict(os.environ, AGENT_HOME=str(tool.parents[1])))
            self.assertEqual(done.returncode, 0, done.stderr)
            return json.loads(done.stdout) if done.stdout.strip() else None
        history = []
        for index, saved_ref in enumerate((ref, other)):   # both saved and queued by the earlier process
            reserved = cli("--reserve", saved_ref, "--claim-pid", me[0], "--claim-start", me[1])
            claimed = cli("--begin-write", saved_ref, "--claim-token", reserved["rpc_claim"]["token"],
                          "--claim-pid", me[0], "--claim-start", me[1])
            cli("--queued", saved_ref, "--message-id", "native-msg-saved-%d" % index)
            history.append({"info": {"id": "native-msg-saved-%d" % index, "role": "user", "sessionID": "idle-restart",
                                     "time": {"created": int(time.time() * 1000) + index}},
                            "parts": [{"type": "text", "text": claimed["text"]}]})
        # Each resume's model call reads its system context late; one resume an idle keeps each its own.
        result = self.f.opencode_plugin_session("idle-restart", {"history": history, "idles": 6, "delay": 30,
                                                                 "personFirst": "the person's own prompt"})
        self.assert_model_calls(result, body, row["text"], other="an ordinary peer message")
        self.settled(signal, ref)
        self.assertEqual(self.f.peer._read_pending(other)["state"], "received")

    def test_an_opencode_session_without_a_window_origin_gets_the_answer_at_its_tool_boundary(self):
        # `opencode -c` proves no window origin, so the idle delivery does not run: the working
        # turn's tool boundary takes the row (pointer on the tool result, text in the system context).
        signal, ref, row, body, _other = self.idle("no-origin")
        result = self.f.opencode_plugin_session("no-origin", {"idles": 2, "tools": 2}, origin=False)
        self.assertFalse(any("SECRET-SENTINEL-VALUE" in out for out in result["tools"]))
        self.assertIn(row["text"], result["tools"][0])
        self.assertIn("the full text:\n" + body + "\n\n" + SECRET_NOTE,
                      next(call["system"] for call in result["systems"] if call["for"] == "tool-0"))
        self.settled(signal, ref)

    def test_an_opencode_tool_boundary_leaves_a_one_line_form_secret_row_alone(self):
        # A one-line form row's own text is the whole old text: it never rides a tool result.
        import io
        signal, ref, row, body, _other = self.idle("one-line-row")
        out = io.StringIO()
        with mock.patch.object(self.f.peer, "_secret_answer_form", return_value="sealed"):
            self.f.peer.emit_peer_context({"harness": "opencode", "session_id": "one-line-row"}, "PostToolUse",
                                          native_delivery=True, stream=out)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.f.peer._read_pending(ref)["state"], "pending")

    def test_an_opencode_message_without_text_keeps_no_earlier_answer(self):
        # A held guidance belongs to the resume it was set for: a later message with no text (an
        # image alone) gets none of an earlier secret answer.
        signal, ref, row, body, _other = self.idle("image-after")
        result = self.f.opencode_plugin_session("image-after", {"idles": 2, "imageOnly": True})
        self.assert_model_calls(result, body, row["text"])
        image = next(call["system"] for call in result["systems"] if call["for"] == "person-image")
        self.assertNotIn("SECRET-SENTINEL-VALUE", image)

    def test_a_claude_secret_row_that_fits_alone_waits_for_room_then_goes_whole(self):
        # Beside a large other text a pointer that fits a context alone is not cut to "read the
        # file": it waits, and the next boundary with room gives it whole.
        import io
        signal, ref, row, body = self.waiting("claude", "fits-alone", "SECRET-SENTINEL-VALUE")
        recipient = {"harness": "claude", "session_id": "fits-alone"}
        crowded = io.StringIO()
        self.f.peer.emit_peer_context(recipient, "PostToolUse", extra_context="O" * 9000,
                                      native_delivery=True, stream=crowded)
        self.assertNotIn("SECRET-SENTINEL-VALUE", crowded.getvalue())
        self.assertNotIn("read it from the file", crowded.getvalue())
        self.assertEqual(self.f.peer._read_pending(ref)["state"], "pending")
        self.assertEqual(self.source(signal)["lifecycle"], "open")
        context = self.f.claude_boundary("fits-alone")
        self.assertIn("the full text:\n" + body + "\n\n" + SECRET_NOTE, context)
        self.settled(signal, ref)

    def test_an_answer_too_long_for_one_claude_context_names_its_file_and_makes_no_copy(self):
        # One answer is at most 4,096 characters a value and two values; two questions of one
        # session (two seats) answered together are one delivery, past one Claude hook context.
        import session_tidy as tidy
        from fixtures.message_questions import MessageQuestionFixture
        fixture = MessageQuestionFixture()
        node = self.fixture.node
        self.f = fixture.__enter__()
        self.f.node = node
        self.addCleanup(fixture.__exit__, None, None, None)
        self.f.display, self.f.shell = True, False
        sid = "too-long"
        signal, envelope = self.f.ask("claude", sid, text="질문", secret=True)
        state.submit_selected_response(envelope, {"answers": {"message-response": [
            "SECRET-LONG-A" + "a" * 4080, "SECRET-LONG-B" + "b" * 4080]}}, action_id="fixture-action")
        seat2 = tidy.session_seat(tidy.Seat("pane", "question-seat-2", pane="fixture:p2", harness="claude"),
                                  "claude", sid)
        second = state.register_message("claude", sid, seat2, "둘째", secret=True,
                                        sequence=tidy.read_prompt_seq(seat2), cwd=str(self.f.root))
        state.submit_selected_response(state.read_selected_request(
            {"id": second["id"], "harness": "claude", "session_id": sid}), {"answers": {"message-response": [
                "SECRET-LONG-C" + "c" * 4080, "SECRET-LONG-D" + "d" * 4080]}}, action_id="fixture-action")
        calls, ref, row = self.f.courier(signal)
        self.assertEqual((calls, row["state"]), ([], "pending"))
        self.assertNotIn("display", self.f.peer._read_transfer_record(ref))   # too long for the line
        self.assertGreater(len(self.f.peer.read_peer_payload(ref)["body"]), 9500)
        context = self.f.claude_boundary(sid)
        self.assertIn(row["text"] + "\n\n" + self.f.peer._PRIVATE_ANSWER_TOO_LONG, context)
        self.assertNotIn("SECRET-LONG-", context)
        overflow = list(self.f.root.rglob("delivery-text/*"))
        self.assertFalse(any("SECRET-LONG-" in path.read_text() for path in overflow), overflow)
        self.settled(signal, ref)

    def test_a_secret_answer_too_long_for_one_hook_context_whole_is_not_sealed_in_the_line(self):
        # Between 8,000 characters (one Claude hook context with room) and the line's own limit of
        # 12,000, a secret answer still takes the pointer: its one-line form's agent text would not
        # fit one hook context whole, and a secret answer gets no overflow copy.
        from fixtures.message_questions import MessageQuestionFixture
        fixture = MessageQuestionFixture()
        node = self.fixture.node
        self.f = fixture.__enter__()
        self.f.node = node
        self.addCleanup(fixture.__exit__, None, None, None)
        self.f.display = True
        signal, envelope = self.f.ask("claude", "line-too-long", text="질문", secret=True)
        state.submit_selected_response(envelope, {"answers": {"message-response": [
            "SECRET-MID-A" + "a" * 4080, "SECRET-MID-B" + "b" * 4080]}}, action_id="fixture-action")
        calls, ref, _row = self.f.courier(signal)
        body = self.f.bodies[-1]
        self.assertTrue(8000 < len(body) < self.f.peer._DISPLAY_AGENT_MAX_CHARS, len(body))
        self.assertNotIn("display", self.f.peer._read_transfer_record(ref))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("SECRET-MID-A", calls[0])

    def test_a_secret_row_over_the_claude_limit_waits_and_is_never_cut(self):
        import dispatch_session_sweep as sweep
        peer = self.f.peer
        released = []
        secret = {"ref": "1" * 32, "text": "POINTER", "shown_text": "POINTER\n\n" + "S" * 6000,
                  "secret_answer": True}
        other = {"ref": "2" * 32, "text": "O" * 6000}
        with mock.patch.object(peer, "release_pending_herdr_claim", side_effect=released.append), \
                mock.patch.object(sweep, "overflow_pointer", side_effect=AssertionError("no overflow copy")):
            # an ordinary claim goes back before a secret answer, wherever it stands
            claims, context = [other, secret], [other["text"], secret["shown_text"]]
            peer._fit_claude_context(claims, context)
            self.assertEqual((claims, context, released), ([secret], [secret["shown_text"]], [other]))
            # two secret answers: the later one goes back, found by the text it showed
            released.clear()
            second = dict(secret, ref="3" * 32, shown_text="POINTER-2\n\n" + "T" * 6000)
            claims, context = [secret, second], [secret["shown_text"], second["shown_text"]]
            peer._fit_claude_context(claims, context)
            self.assertEqual((claims, context, released), ([secret], [secret["shown_text"]], [second]))
            # fits alone, not beside other text: it waits (either form), never the file line
            for form in ("sealed", "pointer"):
                with self.subTest(form=form, alone="fits"):
                    released.clear()
                    claims, context = [secret], [secret["shown_text"], "X" * 6000]
                    with mock.patch.object(peer, "_secret_answer_form", return_value=form):
                        peer._fit_claude_context(claims, context)
                    self.assertEqual((claims, context, released), ([], ["X" * 6000], [secret]))
            # too long even alone: a sealed one waits, a pointer names its file
            for form, expect in (("sealed", None), ("pointer", "POINTER\n\n" + peer._PRIVATE_ANSWER_TOO_LONG)):
                with self.subTest(form=form, alone="too long"):
                    released.clear()
                    row = dict(secret, shown_text="POINTER\n\n" + "S" * 12000)
                    claims, context = [row], [row["shown_text"]]
                    with mock.patch.object(peer, "_secret_answer_form", return_value=form):
                        peer._fit_claude_context(claims, context)
                    if expect is None:
                        self.assertEqual((claims, context, released), ([], [], [row]))
                    else:
                        self.assertEqual((context, released), ([expect], []))

    def test_another_session_given_the_pointer_gets_no_secret(self):
        # Pasted into another session, the pointer stands for nothing there: no recovery copy.
        for harness, case in (("claude", "unsigned"), ("codex", "marked"), ("opencode", "unsigned")):
            with self.subTest(harness=harness):
                signal, window, ref, transfer, body = self.deliver(harness, case, "SECRET-SENTINEL-VALUE", True)
                context = self.f.consume(signal, window, sid="another-session-" + harness)
                self.assertNotIn("SECRET-SENTINEL-VALUE", context)
                self.assertNotEqual(self.f.peer._read_pending(ref)["state"], "received")
                self.assert_pointer(signal, window, ref, body)   # its recipient still gets it

    def test_a_file_reference_runtime_delivery_keeps_its_empty_guidance(self):
        # Only a private answer's payload becomes the agent's context; a Codex reference body of a
        # long runtime completion (`file-reference`) keeps the empty runtime guidance.
        signal, window, ref, transfer, body = self.deliver("codex", "marked", "SECRET-SENTINEL-VALUE", True)
        peer = self.f.peer
        real = peer._read_transfer_record

        def as_file_reference(found_ref, *args, **kwargs):
            record = real(found_ref, *args, **kwargs)
            if isinstance(record, dict) and record.get("body_ref"):
                record = {**record, "body_ref": {**record["body_ref"], "presentation_mode": "file-reference"}}
            return record
        recipient = {"harness": "codex", "session_id": signal["session_id"]}
        with mock.patch.object(peer, "_read_transfer_record", side_effect=as_file_reference):
            self.assertEqual(peer._private_answer_guidance(ref), "")
        self.assertIn("SECRET-SENTINEL-VALUE", peer._private_answer_guidance(ref))
        self.assertIn("SECRET-SENTINEL-VALUE", peer.receiver_guidance(window, recipient))

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
