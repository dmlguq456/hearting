"""11-27/28 ("질문은 지금 사실상 하나만 뜨게끔 되는것 같은데? 여러개가 뜨도록 해야지"): one session holds several
open message questions, and each answer reaches its own question (design 3a).

Each harness and session id keeps one private file of its message questions (`id -> record`). A different question
is added beside the open ones and closes none; the same open question returns its id. An answer on its way to one
question leaves the others answerable. The session's input line takes a number, a choice's words or a sentence as an
answer only while exactly one question is open there. `questions close ID` withdraws only that id. The list, the
panel's count, the popup's `i/N` and Fleet count every question by its full id. An earlier release's one-question
slot joins by id and keeps its address; an unreadable file is unknown, never "none". The questions are registered
through the real `register_message` in a private state root (fixtures/message_questions.py); no window, process or
model is used.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import io
import json
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
import human_wait_state as waits  # noqa: E402
import questions  # noqa: E402
import session_notice  # noqa: E402
import session_shell as shell  # noqa: E402
import session_tidy as tidy  # noqa: E402

SID = "native-question-session"      # the fixture's own session
HARNESSES = ("claude", "codex", "opencode")


def _run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = questions.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class SeveralQuestionsTest(unittest.TestCase):
    def setUp(self):
        from fixtures.message_questions import MessageQuestionFixture
        self.fixture = MessageQuestionFixture()
        self.f = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)

    def open_ids(self, harness="claude", sid=SID):
        return sorted(signal["id"] for signal in waits.read_signals()
                      if (signal["harness"], signal["session_id"]) == (harness, sid))

    def source(self, signal):
        return waits._read(waits._signal_path(signal))

    def prompt(self, signal, text):
        """The real prompt hook with context rendered, as each harness adapter calls it."""
        seat = self.f.seat(signal["harness"], signal["session_id"])
        lines = []
        with mock.patch.object(tidy, "resolve_seat", return_value=seat):
            tidy.run_hook(signal["harness"], "prompt", signal["session_id"], cwd=str(self.f.root),
                          prompt_text=text, env=os.environ, render_context=True, emit=lines.append)
        return "\n".join(lines)

    def caller(self, harness="claude", sid=SID):
        from fleet import process_identity
        stack = contextlib.ExitStack()
        for patch in (mock.patch("dispatch_parent_completion.interactive_parent_identity", return_value=(harness, sid)),
                      mock.patch.object(tidy, "resolve_seat", return_value=self.f.seat(harness, sid)),
                      mock.patch.object(process_identity, "current_launch_context", return_value=None)):
            stack.enter_context(patch)
        return stack

    def test_each_different_question_opens_beside_the_others_and_the_same_one_is_its_open_id(self):
        # The three harnesses use the one registrar and the one session file.
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                a, _ = self.f.ask(harness, SID, text="Cover label?", choices=["Dogwood", "Field Guide"])
                b, _ = self.f.ask(harness, SID, text="Use the pond map?", choices=["Yes", "No"])
                c, _ = self.f.ask(harness, SID, text="Index wording?")
                self.assertEqual(len({a["id"], b["id"], c["id"]}), 3)
                self.assertEqual(self.open_ids(harness), sorted([a["id"], b["id"], c["id"]]))
                path = waits._questions_path(harness, SID)
                self.assertEqual(sorted(waits._read_questions_file(path)), sorted([a["id"], b["id"], c["id"]]))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                # The same question (line endings and edge spaces aside; the recommendation is no
                # part of it) at a later prompt is the one still open: its id and time.
                tidy.bump_prompt_seq(self.f.seat(harness, SID), harness, SID, time.time())
                again, _ = self.f.ask(harness, SID, text="  Cover label?\r\n", choices=["Dogwood", "Field Guide"],
                                      recommended=1)
                self.assertEqual((again["id"], again["since"]), (a["id"], a["since"]))
                # Other choice order, or secrecy, is another question.
                other, _ = self.f.ask(harness, SID, text="Cover label?", choices=["Field Guide", "Dogwood"])
                secret, _ = self.f.ask(harness, SID, text="Index wording?", secret=True)
                self.assertEqual(len(self.open_ids(harness)), 5)
                self.assertNotIn(other["id"], (a["id"], b["id"]))
                self.assertNotEqual(secret["id"], c["id"])

    def test_answers_reach_each_question_while_another_is_on_its_way(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                a, ea = self.f.ask(harness, SID, text="Cover label?", choices=["Dogwood", "Field Guide"])
                b, eb = self.f.ask(harness, SID, text="Use the pond map?", choices=["Yes", "No"])
                self.assertEqual(self.f.submit(ea, "Dogwood", "action-a")["state"], "submitted")
                # A is on its way; B is still answerable and its answer is kept apart.
                self.assertEqual(waits.read_selected_request(eb)["support"]["supported"], True)
                self.assertEqual(self.f.submit(eb, "No", "action-b")["state"], "submitted")
                # A second answer to A, from another place, does not claim it again.
                again = self.f.submit(ea, "Field Guide", "action-a2")
                self.assertEqual(again["action_id"], "action-a")
                for signal in (a, b):
                    self.assertTrue(session_notice.notice_is_current(self.f.record(signal)))
                # B arrives first; A's late receipt has not come, and B settles alone.
                wire_b, _, _ = self.f.prepare(b)
                self.f.consume(b, wire_b)
                self.assertEqual(self.source(b)["completion"]["answer"], "No")
                self.assertEqual(self.source(a)["lifecycle"], "open")
                self.assertEqual(waits._selected_entry(ea)["state"], "submitted")
                self.assertTrue(session_notice.notice_is_current(self.f.record(a)))
                wire_a, _, _ = self.f.prepare(a)
                self.f.consume(a, wire_a)
                self.assertEqual((self.source(a)["completion"]["answer"], self.source(a)["completion"]["action_id"]),
                                 ("Dogwood", "action-a"))
                self.assertEqual(self.source(b)["completion"]["action_id"], "action-b")
                self.assertEqual(self.open_ids(harness), [])

    def test_the_input_line_answers_only_while_one_question_is_open_at_its_seat(self):
        choices = ["first", "second", "third"]
        a, _ = self.f.ask(text="One open", choices=choices)
        self.prompt(a, "2")
        self.assertEqual(self.source(a)["completion"]["answer"], "second")
        b, eb = self.f.ask(text="Two open: B", choices=choices)
        c, _ = self.f.ask(text="Two open: C", choices=choices)
        contexts = []
        for text in ("2", "second", "the second one, please"):
            contexts.append(self.prompt(b, text))
            self.assertEqual((self.source(b)["lifecycle"], self.source(c)["lifecycle"]), ("open", "open"))
        self.assertIn("[답변 필요] 이 자리에 열린 질문 2건", contexts[0])
        self.assertNotIn(b["id"], contexts[0])
        # B's answer goes from the panel; C is the one left, and `2` answers it again.
        self.f.submit(eb, "first", "panel-b")
        self.prompt(c, "2")
        self.assertEqual(self.source(c)["completion"]["answer"], "second")
        self.assertEqual(self.source(b)["lifecycle"], "open")       # still waits for its own receipt

    def test_the_count_line_is_told_once_while_unchanged(self):
        # It goes with the `[답변 필요]` lines the existing repeat filter folds (Claude, Codex).
        b, _ = self.f.ask(text="B?", choices=["x", "y"])
        self.f.ask(text="C?", choices=["x", "y"])
        first, second = self.prompt(b, "hello"), self.prompt(b, "hello again")
        self.assertIn("열린 질문 2건", first)
        self.assertNotIn("열린 질문 2건", second)
        self.f.ask(text="D?")
        self.assertIn("열린 질문 3건", self.prompt(b, "and again"))

    def test_close_withdraws_only_that_question(self):
        a, ea = self.f.ask(text="Withdrawn?", choices=["x", "y"])
        b, eb = self.f.ask(text="Kept?", choices=["x", "y"])
        waits.write_selected_draft(ea, "draft of A")
        waits.write_selected_draft(eb, "draft of B")
        with self.caller():
            code, out, err = _run("close", a["id"])
            self.assertEqual((code, out.strip(), err), (0, "questions: %s cancelled" % a["id"], ""))
            self.assertEqual(self.source(a)["lifecycle"], "cancelled")
            self.assertEqual(waits._selected_entry(ea)["draft"], "")
            self.assertEqual((self.source(b)["lifecycle"], waits._selected_entry(eb)["draft"]), ("open", "draft of B"))
            self.assertEqual(waits.closed_question(a["id"])["status"], "cancelled")
            # Asked again, it is the same end state (closing twice is still closed).
            self.assertEqual(_run("close", a["id"], "--json")[:2], (0, json.dumps({"id": a["id"], "state": "cancelled"}) + "\n"))
            # An answer on its way is not taken back: its receipt ends the question.
            self.f.submit(eb, "y", "panel-b")
            self.assertEqual(json.loads(_run("close", b["id"], "--json")[1])["state"], "submitted")
            wire, _, _ = self.f.prepare(b)
            self.f.consume(b, wire)
            self.assertEqual(json.loads(_run("close", b["id"], "--json")[1])["state"], "answered")
            other, _ = self.f.ask(sid="another-session", text="Not yours")
            code, _out, err = _run("close", other["id"])
            self.assertEqual((code, err.strip()), (2, "questions: question-not-in-this-session"))
        self.assertEqual(self.source(other)["lifecycle"], "open")

    def test_list_panel_popup_and_fleet_count_every_question(self):
        for count in (0, 1, 2, 3, 5, 10):
            with self.subTest(count=count):
                sid = "count-%d" % count
                asked = [self.f.ask(sid=sid, text="Question %d of %d" % (n, count), choices=["x", "y"])[0]
                         for n in range(count)]
                self.assertTrue(waits._refresh_cache())
                view = waits.cached_view(request=False)
                listed = [item for item in view["items"] if (item.get("owner") or {}).get("session_id") == sid]
                self.assertEqual(sorted(item["id"] for item in listed), sorted(s["id"] for s in asked))
                fleet = [row for row in waits.cached_fleet_view(request=False)["items"] if row.get("session_id") == sid]
                self.assertEqual(len(fleet), count)
                board = shell.session_questions("claude", sid, view=view)
                self.assertEqual(len(board), count)
                from fleet.session_block import question_presentations
                question_presentations(board, 64, brief_columns=40)
                heads = ["".join(text for text, _key in question["display"]["head"])
                         for item in board for question in item["questions"]]
                self.assertEqual([("%d QUESTIONS · %d/%d" % (count, n, count)) in head
                                  for n, head in enumerate(heads, 1)], [True] * count)
                with mock.patch.object(questions, "load_board", return_value={**view, "items": listed}):
                    code, out, _err = _run("list", "--json")
                self.assertEqual((code, json.loads(out)["counts"]["total"], len(json.loads(out)["items"])),
                                 (0, count, count))
                if count == 10:
                    self.assertEqual(self.popup(board), [10, 10, "Question 9 of 10"])

    def popup(self, board):
        """The local editor's count, the references it asks for, and the question Tab reaches last."""
        if not self.f.node:
            self.skipTest("the session shell editor needs node")
        rows = [{key: item[key] for key in ("id", "revision", "answerable", "questions")} for item in board]
        for item in rows:
            item["questions"] = [{key: q[key] for key in ("id", "question", "choices")} for q in item["questions"]]
        script = r"""
const create=require(process.argv[1]),rows=JSON.parse(process.argv[2]);
const block=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
block.update(rows);block.enter();
for(let n=1;n<rows.length;n++)block.input(Buffer.from('\t'));
const line=JSON.stringify(block.summary().line);
console.log(JSON.stringify([block.summary().count,block.references().length,(line.match(/Question \d+ of \d+/)||[''])[0]]));"""
        result = subprocess.run([self.f.node, "-e", script, str(ROOT / "utilities/session-shell/block-input.cjs"),
                                 json.dumps(rows)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_an_older_single_record_joins_by_id_and_keeps_its_address(self):
        seat = self.f.seat("claude", SID)
        old = {"schema_version": 1, "mode": "message", "source": "S4", "id": "S4:" + "a" * 64,
               "harness": "claude", "session_id": SID, "seat_key": seat.key, "sequence": 0,
               "question": "Older question?", "detail": {"questions": [{
                   "id": "message-response", "question": "Older question?", "choices": ["x", "y"],
                   "recommended": None, "is_secret": False, "is_other": True, "required": True}]},
               "since": time.time() - 60, "lifecycle": "open", "cwd": str(self.f.root), "incarnation": None}
        slot = waits._session_signal_path(old)
        waits._write(slot, old)                                     # as the earlier release wrote it
        new, en = self.f.ask(text="Newer question?", choices=["x", "y"])
        self.assertEqual(self.open_ids(), sorted([old["id"], new["id"]]))
        self.assertEqual(waits._signal_path(old), slot)
        # The older question answers through its own address; the newer one is untouched.
        eo = waits.read_selected_request({"id": old["id"], "harness": "claude", "session_id": SID})
        self.assertEqual(self.f.submit(eo, "y", "older-action")["state"], "submitted")
        wire, _, _ = self.f.prepare(old)
        self.f.consume(old, wire)
        self.assertEqual(waits._read(slot)["completion"]["answer"], "y")
        self.assertEqual(self.source(new)["lifecycle"], "open")
        self.assertNotIn(old["id"], waits._read_questions_file(waits._questions_path("claude", SID)))
        from fleet import titles
        self.prompt(new, "1")
        self.assertEqual(self.source(new)["lifecycle"], "answered")
        self.assertTrue(titles.read_timeline(SID, harness="claude")[-1].get("answer"))
        # With nothing open, the older answered record left in its slot makes no prompt an answer.
        self.prompt(new, "just talking")
        self.assertFalse(titles.read_timeline(SID, harness="claude")[-1].get("answer"))
        # An earlier release writing its slot again cannot replace a row of the session's file.
        waits._write(slot, {**self.source(new), "lifecycle": "open"})
        self.assertEqual(self.source(new)["lifecycle"], "answered")
        self.assertEqual(self.open_ids(), [])
        # An earlier release reads the file as no question of its own (`_read` of the plain path), never breaks.
        self.assertIsNone(waits._read(Path(str(waits._questions_path("claude", SID)))))

    def test_an_unreadable_question_file_is_unknown_not_none(self):
        a, _ = self.f.ask(text="Still there?", choices=["x", "y"])
        path = waits._questions_path("claude", SID)
        kept = path.read_bytes()
        path.write_text("{broken")
        self.assertEqual(waits.message_questions("claude", SID), ({}, "unknown"))
        health = []
        self.assertEqual([s for s in waits.read_signals(health=health) if s["session_id"] == SID], [])
        self.assertIn({"source": "S4", "state": "unknown", "reason": "question-file-unreadable"}, health)
        with self.assertRaisesRegex(ValueError, "question-storage-unreadable"):
            self.f.ask(text="Over it?")
        self.prompt(a, "1")                                          # not an answer while unknown
        path.write_bytes(kept)
        self.assertEqual(self.source(a)["lifecycle"], "open")
        self.assertEqual(self.open_ids(), [a["id"]])

    def test_the_same_session_resumed_keeps_its_open_questions_and_the_answer_on_its_way(self):
        start = waits._proc_start_ticks(os.getpid())
        incarnation = {"pid": os.getpid(), "proc_start": start}
        a, ea = self.f.ask(text="Resume A?", choices=["x", "y"], incarnation=incarnation)
        b, eb = self.f.ask(text="Resume B?", choices=["x", "y"], incarnation=incarnation)
        self.f.submit(ea, "x", "before-resume")
        with mock.patch.object(waits, "_proc_start_ticks", return_value="replacement-start"):
            self.assertEqual(waits.read_selected_request(ea)["state"], "submitted")
            current = waits.read_selected_request(eb)
            self.assertEqual((current["state"], current["support"]["supported"]), ("open", True))
            self.assertEqual(self.open_ids(), sorted([a["id"], b["id"]]))
            wire, _, _ = self.f.prepare(a)
            self.f.consume(a, wire)
        self.assertEqual(self.source(a)["completion"]["action_id"], "before-resume")
        self.assertEqual(self.source(b)["lifecycle"], "open")

    def test_the_words_under_the_choices_ride_in_the_session_file(self):
        # #1908's words under the choices are part of the record the session's file keeps; other words for the
        # same open question are still that question (its id and first words).
        a, ea = self.f.ask(text="Cover label?", choices=["Dogwood", "Field Guide"], descriptions={"Dogwood": "The tree"})
        row = waits._read_questions_file(waits._questions_path("claude", SID))[a["id"]]
        self.assertEqual(row["detail"]["questions"][0]["choice_descriptions"], {"Dogwood": "The tree"})
        self.assertEqual(ea["detail"]["questions"][0]["choice_descriptions"], {"Dogwood": "The tree"})
        again, _ = self.f.ask(text="Cover label?", choices=["Dogwood", "Field Guide"],
                              descriptions={"Dogwood": "Other words"})
        self.assertEqual((again["id"], again["detail"]), (a["id"], a["detail"]))

    # Independent review 1 (11-28), the five scenes it reproduced on this source.

    def test_an_ended_question_takes_no_answer_slot_and_a_late_draft_or_answer_does_not_reopen_it(self):
        # Review 6: each read of a question closed with no draft made an answer slot the store never prunes;
        # once the slots ran out no new answer stood in any session. A draft from a screen opened before the
        # close made its answer record `open` again.
        with mock.patch.object(waits, "_SELECTED_RESPONSE_MAX", 4):
            for n in range(8):
                closed, ec = self.f.ask(text="Closed %d?" % n, choices=["x", "y"])
                if n % 2:
                    waits.write_selected_draft(ec, "draft %d" % n)     # what a close leaves is prunable
                self.assertEqual(waits.close_message("claude", SID, closed["seat_key"], closed["id"])["state"],
                                 "cancelled")
                for _read in range(3):
                    self.assertEqual(waits.read_selected_request(ec)["state"], "cancelled")
            fresh, ef = self.f.ask(text="Fresh?", choices=["x", "y"])
            self.assertEqual(waits.write_selected_draft(ef, "fresh draft").get("draft"), "fresh draft")
            self.assertEqual(self.f.submit(ef, "x", "fresh-action")["state"], "submitted")
            self.assertEqual(self.f.record(fresh)["receipt"]["answer_binding"]["action_id"], "fresh-action")
        a, old_screen = self.f.ask(text="Closed under an old screen?", choices=["x", "y"])
        b, eb = self.f.ask(text="Kept beside it?", choices=["x", "y"])
        waits.write_selected_draft(eb, "draft of B")
        with self.caller():
            self.assertEqual(_run("close", a["id"])[0], 0)
        self.assertNotEqual(waits.write_selected_draft(old_screen, "late draft").get("draft"), "late draft")
        for place in ("panel", "fleet"):                              # two places answer from the old screen
            self.assertNotEqual(self.f.submit(old_screen, "y", "late-" + place)["state"], "submitted")
        self.assertIsNone(waits._selected_entry(old_screen))
        again = waits.read_selected_request(old_screen)
        self.assertEqual((again["state"], again.get("draft", "")), ("cancelled", ""))
        self.assertIsNone(self.f.record(a))
        self.assertEqual((self.source(b)["lifecycle"], waits._selected_entry(eb)["draft"]), ("open", "draft of B"))

    def test_an_ended_question_is_kept_by_when_it_ended_and_until_its_decision_is_queued(self):
        # Review 4: the 32 ended rows were kept by registration time, so the longest-open question went in the
        # very write that answered it: no decision record, and closing or asking about it no longer knew it.
        import tidy_decisions
        path = waits._questions_path("claude", SID)

        def answered(text, value, action, *, owed=False):
            signal, envelope = self.f.ask(text=text, choices=["x", "y"])
            self.assertEqual(self.f.submit(envelope, value, action)["state"], "submitted")
            yield signal
            wire, _, _ = self.f.prepare(signal)
            with mock.patch.object(tidy_decisions, "save_pending", return_value=None) if owed else \
                    contextlib.nullcontext():
                self.f.consume(signal, wire)
                waits.flush_message_decisions()
            self.assertEqual(self.source(signal)["completion"]["action_id"], action)
            yield signal

        def close_later(label):
            for n in range(waits._TERMINAL_QUESTIONS_KEPT):
                later, _ = self.f.ask(text="%s %d?" % (label, n))
                waits.close_message("claude", SID, later["seat_key"], later["id"])

        def decisions():
            return sorted(payload["answers"] for _path, payload in tidy_decisions.list_pending())
        # The longest-open A is answered after 32 later questions closed: it is the newest ending.
        steps = answered("Asked first, answered last?", "y", "action-a")
        a = next(steps)
        close_later("Closed before A arrives")
        next(steps)
        self.assertIn(a["id"], waits._read_questions_file(path))
        self.assertEqual(decisions(), [["y"]])
        self.assertEqual(waits.closed_question(a["id"])["status"], "answered")
        with self.caller():
            self.assertEqual(json.loads(_run("close", a["id"], "--json")[1])["state"], "answered")
        # D's decision record could not be written: 32 newer endings do not take D before it is queued.
        steps = answered("Its decision is owed?", "x", "action-d", owed=True)
        d = next(steps)
        next(steps)
        close_later("Closed after D arrived")
        self.assertIn(d["id"], waits._read_questions_file(path))
        waits.flush_message_decisions()
        self.assertEqual(decisions(), [["x"], ["y"]])
        self.assertNotIn(d["id"], waits._read_questions_file(path))   # queued, it is one more old ending
        self.assertEqual(len(waits._read_questions_file(path)), waits._TERMINAL_QUESTIONS_KEPT)

    def test_a_file_missing_what_reads_need_or_at_another_sessions_address_is_unknown(self):
        # Review 5: a row without `detail` passed (health ok, then KeyError on reading or registering), and
        # another session's file put at this session's address let this session close that session's question.
        a, _ = self.f.ask(text="A?", choices=["x", "y"])
        b, _ = self.f.ask(text="B?", choices=["x", "y"])
        other, _ = self.f.ask(sid="another-session", text="Theirs?", choices=["x", "y"])
        path = waits._questions_path("claude", SID)
        kept = path.read_bytes()
        cases = {"foreign file": waits._questions_path("claude", "another-session").read_bytes()}
        for field, value in (("detail", None), ("detail", {"questions": []}), ("sequence", "3"),
                             ("lifecycle", None)):
            rows = json.loads(kept)
            rows["questions"][b["id"]][field] = value
            if value is None:
                del rows["questions"][b["id"]][field]
            cases["%s=%r" % (field, value)] = json.dumps(rows).encode()
        for name, raw in cases.items():
            with self.subTest(case=name):
                path.write_bytes(raw)
                self.assertEqual(waits.message_questions("claude", SID), ({}, "unknown"))
                health = []
                self.assertEqual([s["id"] for s in waits.read_signals(health=health)], [other["id"]])
                self.assertIn({"source": "S4", "state": "unknown", "reason": "question-file-unreadable"}, health)
                self.assertTrue(waits._refresh_cache())
                with self.caller():
                    code, out, _err = _run("list", "--json")
                    self.assertEqual((code, json.loads(out)["complete"]), (0, False))
                    for argv in (("ask", "C?"), ("close", b["id"]), ("close", other["id"])):
                        self.assertEqual(_run(*argv)[::2], (2, "questions: question-storage-unreadable\n"))
                self.assertEqual(self.source(other)["lifecycle"], "open")
        path.write_bytes(kept)
        self.assertEqual(self.open_ids(), sorted([a["id"], b["id"]]))

    def test_asking_again_after_the_same_registration_ended_says_its_end_not_a_wait(self):
        # Review 7: closing A and asking the same text at the same prompt said "registered, waiting" with
        # exit 0 while nothing waited. The next prompt opens a new id, as before.
        waiting = waits.ui_translator()("questions.registered")
        with self.caller():
            code, out, _err = _run("ask", "Same prompt?", "--json")
            first = json.loads(out)
            self.assertEqual((code, first["state"]), (0, "open"))
            self.assertEqual(_run("close", first["id"])[0], 0)
            self.assertEqual(_run("ask", "Same prompt?", "--json")[:2],
                             (1, json.dumps({"id": first["id"], "state": "cancelled"}) + "\n"))
            code, out, _err = _run("ask", "Same prompt?")
            self.assertEqual(code, 1)
            self.assertIn(first["id"] + " cancelled", out)
            self.assertNotIn(waiting, out)
            self.assertEqual(self.open_ids(), [])
            tidy.bump_prompt_seq(self.f.seat("claude", SID), "claude", SID, time.time())
            code, out, _err = _run("ask", "Same prompt?", "--json")
            self.assertEqual(code, 0)
            self.assertNotEqual(json.loads(out)["id"], first["id"])
            self.assertEqual(self.open_ids(), [json.loads(out)["id"]])

    # Independent review 2 (11-28).

    def test_a_draft_returns_while_the_hook_holds_the_seat_lock(self):
        # Review 2's first: the draft took the seat lock, so a Fleet save waited for as long as the prompt hook held
        # it. A draft takes no seat lock (main never did); it returns at once.
        import threading
        a, ea = self.f.ask(text="Drafted while the hook runs?", choices=["x", "y"])
        saved = []
        with tidy.seat_lock(a["seat_key"]):                           # the hook, paused inside its lock
            worker = threading.Thread(target=lambda: saved.append(waits.write_selected_draft(ea, "typed")))
            worker.start()
            worker.join(10)
            returned = not worker.is_alive()
        worker.join()
        self.assertTrue(returned)
        self.assertEqual((saved[0].get("draft"), waits._selected_entry(ea)["draft"]), ("typed", "typed"))


if __name__ == "__main__":
    unittest.main()
