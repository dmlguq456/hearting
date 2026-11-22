"""11-22 ("그 질문 사항은 왜 답변줄이 안뜨지?"): a registered question the panel shows keeps its choices and its answer
row while the shared answer-wait observation is missing or incomplete.

The panel took a question it already showed to have left its session's waits whenever the shared observation did not
list it, also when there was no observation to list it (the copy past its display time, the producer's failure record):
an open registered question then read `closed` (`닫힘`) with neither its choices nor its answer row. Now only a complete
observation that leaves it out closes it; without one it stands as its own request says, and an answer sent before the
next observation is refused with one line saying why, the typed answer kept. The question is registered through the
real `register_message` in a private state root (fixtures/message_questions.py) and observed by the producer's own pass;
no window, process or model is used.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
import human_wait_state as waits  # noqa: E402
import session_shell as shell  # noqa: E402

SID = "native-question-session"
CHOICES = ["first choice", "second choice", "third choice"]
TEXT = "\n".join("synthetic question line %d" % n for n in range(1, 15))

EDITOR = r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]);
const rows=JSON.parse(process.argv[2]),out=[];
const b=create({changed(){},action(){},answer:async()=>({state:'submitted'})});
for(const data of rows){
  b.update(data.questions,data.help,data.block);b.enter();
  out.push(b.detail({budget:24,columns:122}).map(r=>r.map(p=>p.text).join('')));b.leave();
}
console.log(JSON.stringify(out));
"""


class AnswerRowWithoutObservationTest(unittest.TestCase):
    def setUp(self):
        from fixtures.message_questions import MessageQuestionFixture
        self.fixture = MessageQuestionFixture()
        self.f = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        self.signal, _ = self.f.ask(sid=SID, text=TEXT, choices=CHOICES, recommended=0)
        self.assertTrue(waits._refresh_cache())          # the producer's own pass lists it

    def board(self, retained=(), now=None):
        view = waits.cached_view(request=False, now=now) or {}
        return shell.session_questions("claude", SID, view=view, retained=retained)

    def project(self, retained=()):
        import session_tidy
        with mock.patch.object(session_tidy, "prompt_display", return_value={"received": []}):
            return shell.panel_projection("claude", SID, columns=59, rows=30, state="working",
                                          retained_questions=list(retained), popup_columns=126)

    def drawn(self, *projections):
        if not self.f.node:
            self.skipTest("the session shell editor needs node")
        result = subprocess.run([self.f.node, "-e", EDITOR, str(ROOT / "utilities/session-shell/block-input.cjs"),
                                 json.dumps([{key: data[key] for key in ("questions", "help", "block")}
                                             for data in projections])],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_answerable_with_choices(self, rows):
        self.assertEqual([row["id"] for row in rows], [self.signal["id"]])
        self.assertIs(rows[0]["answerable"], True, rows[0])
        question = rows[0]["questions"][0]
        self.assertEqual(question["choices"], CHOICES)
        self.assertIs(question["free_text"], True)

    def test_a_shown_question_stands_with_its_choices_while_the_observation_is_missing_or_failed(self):
        shown = self.board()
        self.assert_answerable_with_choices(shown)
        retained = [{"id": row["id"], "revision": row["revision"]} for row in shown]
        # The copy past its display time (`questions` then says the observation is not available yet).
        later = time.time() + waits._DISPLAY_TTL + 20
        self.assertIsNone(waits.cached_view(request=False, now=later))
        self.assert_answerable_with_choices(self.board(retained, now=later))
        # The producer's own failure record: a fresh copy with no waits, not complete.
        self.assertTrue(waits.publish_cache({"items": [], "complete": False, "observed_at": time.time(),
                                             "health": [{"source": "producer", "state": "unknown"}]}))
        self.assert_answerable_with_choices(self.board(retained))
        # Never shown before the observation went: nothing to keep (the next pass lists it).
        self.assertEqual(self.board(), [])
        # A complete observation that leaves it out still closes it here (spec E9 rule 54 (e)).
        self.assertTrue(waits.publish_cache({"items": [], "complete": True, "observed_at": time.time()}))
        closed = self.board(retained)
        self.assertIs(closed[0]["answerable"], False)
        self.assertEqual(closed[0]["reason"], "retained-display-no-longer-current")

    def test_the_popup_keeps_the_choices_and_the_answer_row_across_the_gap(self):
        first = self.project()
        retained = [{"id": item["id"], "revision": item["revision"]} for item in first["questions"]]
        self.assertTrue(waits.publish_cache({"items": [], "complete": False, "observed_at": time.time(),
                                             "health": [{"source": "producer", "state": "unknown"}]}))
        gap = self.project(retained)
        for rows in self.drawn(first, gap):
            self.assertTrue(any(row.startswith("› ") for row in rows), "\n".join(rows))
            self.assertEqual(sum(1 for row in rows if "choice" in row and row.strip()[:1] in "▸123"), 3, "\n".join(rows))
            self.assertFalse(any("closed" in row for row in rows), "\n".join(rows))

    def test_an_answer_before_the_next_observation_says_why_and_goes_after_it(self):
        retained = self.board()
        binding = {"harness": "claude", "sid": SID}
        request = {"question_id": self.signal["id"], "revision": retained[0]["revision"],
                   "answers": {"message-response": ["second choice", "one space only"]}}
        self.assertTrue(waits.publish_cache({"items": [], "complete": False, "observed_at": time.time(),
                                             "health": [{"source": "producer", "state": "unknown"}]}))
        refused = shell._block_answer(binding, request)
        self.assertEqual(refused, {"ok": False, "reason": "not sent · reading the waits · Enter again soon"})
        envelope = waits.read_selected_request({"id": self.signal["id"], "harness": "claude", "session_id": SID})
        self.assertEqual(envelope["state"], "open")
        self.assertTrue(waits._refresh_cache())
        sent = shell._block_answer(binding, request)
        self.assertTrue(sent["ok"], sent)
        self.assertEqual(sent["state"], "submitted")


if __name__ == "__main__":
    unittest.main()
