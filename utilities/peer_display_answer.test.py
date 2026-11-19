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


if __name__ == "__main__":
    unittest.main()
