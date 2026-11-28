#!/usr/bin/env python3
"""One native call's several questions are separate Hearting questions (11-28, piece 3b items).

The person (11-27): "질문은 지금 사실상 하나만 뜨게끔 되는것 같은데? 여러개가 뜨도록 해야지..". Each question of
the call is its own record, all new ones written in one replacement of the session's question file (they appear
together), the ids returned in the call's order; a question already open keeps its id. When none is open and one
cannot be held, nothing is written and the whole call opens natively; when some are open and another cannot be made,
the call is refused once, naming the open ids and the others, and asking only the others again settles it.
Intercepted records carry `origin: "native-question-tool"`; `questions ask` records do not.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import contextlib
import fcntl
import io
import json
from pathlib import Path
import re
import threading
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "tools"))
import human_wait_state as state  # noqa: E402
import native_question_redirect as redirect  # noqa: E402
import questions  # noqa: E402
from fleet import interaction  # noqa: E402

SID = "native-items-session"
HARNESSES = ("claude", "codex", "opencode")
ID = re.compile(r"S4:[0-9a-f]{64}")

SINGLE = {"question": "Which label goes on the cover?", "header": "Cover", "multiSelect": False,
          "options": [{"label": "Dogwood (Recommended)", "description": "The trail's name"},
                      {"label": "Field Guide", "description": "What the book is"}]}
MULTI = {"question": "Which maps go in?", "header": "Maps", "multiSelect": True,
         "options": [{"label": "Pond", "description": "North loop"}, {"label": "Ridge", "description": "South loop"}]}
PREVIEW = {"question": "Which cover layout?", "header": "Layout", "multiSelect": False,
           "options": [{"label": "Tall", "preview": "+------+\n| logo |\n+------+"}, {"label": "Wide"}]}
SECRET = {"question": "Printer account token?", "header": "Token", "isSecret": True, "options": []}
TOO_LARGE = {"question": "Read this first?\n" + "x" * 4200, "options": [{"label": "Yes"}, {"label": "No"}]}


def asked(questions_):
    return {"claude": {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                       "tool_input": {"questions": questions_}},
            "codex": {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "request_user_input",
                      "tool_input": {"questions": questions_}},
            "opencode": {"tool": "question", "sessionID": SID, "args": {"questions": questions_}}}


class Base(unittest.TestCase):
    def setUp(self):
        from fixtures.message_questions import MessageQuestionFixture
        import session_tidy as tidy
        from fleet import process_identity
        self.fixture = MessageQuestionFixture()
        self.f = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        self.tidy = tidy
        for patch in (mock.patch.object(tidy, "resolve_seat",
                                        side_effect=lambda harness, *a, **k: self.f.seat(harness, a[2] if len(a) > 2 else SID)),
                      mock.patch.object(process_identity, "current_launch_context", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)

    def rows(self, harness):
        rows, health = state.message_questions(harness, SID)
        self.assertEqual(health, "ok")
        return rows

    def open_ids(self, harness):
        return sorted(item_id for item_id, row in self.rows(harness).items() if row["lifecycle"] == "open")

    def question(self, harness, item_id):
        (found,) = questions.questions_of(state.read_selected_request(
            {"id": item_id, "harness": harness, "session_id": SID}))
        return found

    def ids(self, outcome, count=None):
        self.assertEqual(outcome.kind, "registered", outcome)
        found = ID.findall(outcome.reason)
        if count is not None:
            self.assertEqual(len(found), count, outcome.reason)
        return found

    def next_prompt(self, harness):
        self.tidy.bump_prompt_seq(self.f.seat(harness, SID), harness, SID, time.time())

    def done(self, harness):
        for item_id in self.open_ids(harness):
            state.close_message(harness, SID, self.f.seat(harness, SID).key, item_id)
        interaction.clear_wait(SID, harness)
        self.next_prompt(harness)

    @contextlib.contextmanager
    def file_writes(self, harness):
        """Every replacement of this session's question file, with the open ids a reader saw right after it."""
        real, seen, path = state._write, [], state._questions_path(harness, SID)

        def write(target, value):
            real(target, value)
            if Path(target).name == path.name:
                seen.append(set(self.open_ids(harness)))
        with mock.patch.object(state, "_write", side_effect=write):
            yield seen


class SeveralQuestionsTest(Base):
    def test_two_three_and_four_questions_are_one_each_in_order_and_appear_together(self):
        for harness in HARNESSES:
            for count in (2, 3, 4):
                with self.subTest(harness=harness, count=count):
                    call = [SINGLE, MULTI, PREVIEW, SECRET][:count]
                    with self.file_writes(harness) as writes:
                        found = self.ids(redirect.decide(harness, asked(call)[harness]), count)
                    self.assertEqual(len(set(found)), count)
                    # one replacement: a reader saw none of them, then all of them, never some
                    self.assertEqual(writes, [set(found)])
                    self.assertEqual(self.open_ids(harness), sorted(found))
                    shown = [self.question(harness, item_id) for item_id in found]
                    self.assertEqual([row["question"].splitlines()[0].split("  [")[0] for row in shown],
                                     [item["question"] for item in call])
                    cover = shown[0]
                    self.assertEqual((cover["choices"], cover["recommended"], cover["descriptions"], cover["secret"]),
                                     (["Dogwood", "Field Guide"], 0, ["The trail's name", "What the book is"], False))
                    maps = shown[1]
                    self.assertEqual(maps["choices"], [])
                    for part in ("[several may be chosen]", "1. Pond — North loop", "2. Ridge — South loop"):
                        self.assertIn(part, maps["question"])
                    if count >= 3:
                        self.assertEqual(shown[2]["choices"], [])
                        self.assertIn("+------+ | logo | +------+", shown[2]["question"])
                    if count == 4:
                        self.assertEqual((shown[3]["question"], shown[3]["secret"]), ("Printer account token?", True))
                    # the board lists them in the call's order
                    board = sorted((row for row in self.rows(harness).values() if row["lifecycle"] == "open"),
                                   key=lambda row: (row["since"], row["id"]))
                    self.assertEqual([row["id"] for row in board], found)
                    self.done(harness)

    def test_answering_one_leaves_the_others_waiting_and_each_answer_reaches_its_question(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                found = self.ids(redirect.decide(harness, asked([SINGLE, MULTI, SECRET])[harness]), 3)
                envelopes = [state.read_selected_request({"id": item_id, "harness": harness, "session_id": SID})
                             for item_id in found]
                self.assertEqual(self.f.submit(envelopes[1], "Pond and Ridge", "action-maps")["state"], "submitted")
                for other in (envelopes[0], envelopes[2]):
                    self.assertTrue(state.read_selected_request(other)["support"]["supported"])
                maps = state._read(state._signal_path(self.rows(harness)[found[1]]))
                wire, _, _ = self.f.prepare(maps)
                self.f.consume(maps, wire)
                rows = self.rows(harness)
                self.assertEqual(rows[found[1]]["completion"]["answer"], "Pond and Ridge")
                self.assertEqual([rows[found[0]]["lifecycle"], rows[found[2]]["lifecycle"]], ["open", "open"])
                self.assertEqual(self.f.submit(envelopes[0], "Dogwood", "action-cover")["state"], "submitted")
                cover = state._read(state._signal_path(rows[found[0]]))
                wire, _, _ = self.f.prepare(cover)
                self.f.consume(cover, wire)
                self.assertEqual(self.rows(harness)[found[0]]["completion"]["answer"], "Dogwood")
                self.assertEqual(self.open_ids(harness), [found[2]])
                self.done(harness)

    def test_a_session_with_question_a_asks_three_and_either_first_answer_reaches(self):
        for harness in HARNESSES:
            for first in ("A", "second of three"):
                with self.subTest(harness=harness, first=first):
                    a, envelope_a = self.f.ask(harness, SID, text="Print the trail sketches in color?",
                                               choices=["Color", "Gray"])
                    found = self.ids(redirect.decide(harness, asked([SINGLE, MULTI, PREVIEW])[harness]), 3)
                    self.assertEqual(self.open_ids(harness), sorted([a["id"], *found]))
                    target = a if first == "A" else self.rows(harness)[found[1]]
                    envelope = envelope_a if first == "A" else state.read_selected_request(
                        {"id": found[1], "harness": harness, "session_id": SID})
                    self.assertEqual(self.f.submit(envelope, "Gray", "action-first")["state"], "submitted")
                    signal = state._read(state._signal_path(target))
                    wire, _, _ = self.f.prepare(signal)
                    self.f.consume(signal, wire)
                    self.assertEqual(self.rows(harness)[target["id"]]["completion"]["answer"], "Gray")
                    self.assertEqual(len(self.open_ids(harness)), 3)
                    self.done(harness)

    def test_the_line_names_the_ids_in_order_replaces_ends_the_turn_and_each_answer_carries_its_id(self):
        outcome = redirect.decide("claude", asked([SINGLE, MULTI, SECRET])["claude"])
        found = self.ids(outcome, 3)
        self.assertNotIn("\n", outcome.reason)
        for part in ("replace this native question", "End your turn now", "marked with its id", "in this call's order"):
            self.assertIn(part, outcome.reason)
        for word in ("hearting run", "--", "command"):
            self.assertNotIn(word, outcome.reason)
        self.assertEqual(found, sorted(found, key=outcome.reason.index))


class SameQuestionsTest(Base):
    def test_the_same_call_again_is_the_same_ids_and_reordered_is_the_same_ids_in_its_order(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                call = [SINGLE, MULTI, PREVIEW, SECRET]
                found = self.ids(redirect.decide(harness, asked(call)[harness]), 4)
                for when in ("this prompt", "next prompt"):
                    if when == "next prompt":
                        self.next_prompt(harness)
                    with self.file_writes(harness) as writes:
                        self.assertEqual(self.ids(redirect.decide(harness, asked(call)[harness])), found)
                        reordered = self.ids(redirect.decide(harness, asked(call[::-1])[harness]))
                    self.assertEqual(reordered, found[::-1])
                    self.assertEqual(writes, [])                                      # nothing written
                self.assertEqual(self.open_ids(harness), sorted(found))
                self.done(harness)

    def test_questions_already_open_keep_their_ids_and_only_the_rest_are_new(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                cli, _ = self.f.ask(harness, SID, text=SINGLE["question"], choices=["Dogwood", "Field Guide"])
                self.next_prompt(harness)
                secret = self.ids(redirect.decide(harness, asked([SECRET])[harness]), 1)[0]
                with self.file_writes(harness) as writes:
                    found = self.ids(redirect.decide(harness, asked([MULTI, SINGLE, PREVIEW, SECRET])[harness]), 4)
                self.assertEqual((found[1], found[3]), (cli["id"], secret))
                self.assertEqual(writes, [set(self.open_ids(harness))])
                self.assertEqual(len(self.open_ids(harness)), 4)
                self.done(harness)

    def test_the_same_question_twice_in_one_call_is_one_id_twice(self):
        """Word for word, or with another recommendation (no part of what makes a question the same)."""
        moved = {**SINGLE, "options": [{"label": "Dogwood"}, {"label": "Field Guide (Recommended)"}]}
        for harness in HARNESSES:
            for twin in (dict(SINGLE), moved):
                with self.subTest(harness=harness, twin=twin["options"][0]["label"]):
                    with self.file_writes(harness) as writes:
                        found = self.ids(redirect.decide(harness, asked([SINGLE, MULTI, twin])[harness]), 3)
                    self.assertEqual((found[0] == found[2], len(set(found))), (True, 2))
                    self.assertEqual(writes, [set(found)])
                    self.assertEqual(self.open_ids(harness), sorted(set(found)))
                    self.done(harness)

    def test_two_seats_of_the_session_asking_the_same_question_at_once_make_one(self):
        """The same question is decided on the rows read under the lock that writes the file, so another
        seat's registration landing just before that lock is found, not doubled."""
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                other = self.tidy.session_seat(self.tidy.Seat("pane", "question-seat-other-" + harness,
                                                              pane="fixture:p2", harness=harness), harness, SID)
                real, raced = state._write_message, []

                def racing(signal, fresh=None):
                    if fresh and not raced:
                        raced.append("racing")
                        raced[0] = state.register_messages(harness, SID, other, [{
                            "text": SINGLE["question"], "choices": ["Dogwood", "Field Guide"]}],
                            sequence=self.tidy.read_prompt_seq(other))[0]
                    return real(signal, fresh)
                with mock.patch.object(state, "_write_message", side_effect=racing):
                    found = self.ids(redirect.decide(harness, asked([SINGLE, MULTI])[harness]), 2)
                self.assertEqual(found[0], raced[0]["id"])
                self.assertEqual(self.open_ids(harness), sorted(found))
                self.done(harness)

    def test_an_earlier_release_one_text_question_still_open_is_the_whole_call(self):
        """The installation boundary: a release before this wrote the call as one `Q1. … Q2. …` question;
        while that is open, the same call names it and asks nothing again."""
        call = [SINGLE, MULTI, PREVIEW]
        _, rows = redirect._normalized("claude", asked(call)["claude"])
        for harness in HARNESSES:
            for detail in (True, False):
                with self.subTest(harness=harness, detail=detail):
                    old, _ = self.f.ask(harness, SID, text=redirect.build(rows, detail=detail)["text"])
                    self.assertIn("Q2. Which maps go in?", old["question"])
                    self.next_prompt(harness)
                    with self.file_writes(harness) as writes:
                        outcome = redirect.decide(harness, asked(call)[harness])
                    self.assertEqual(set(self.ids(outcome)), {old["id"]})
                    self.assertIn("Registered as Hearting question " + old["id"] + ";", outcome.reason)
                    self.assertEqual((writes, self.open_ids(harness)), ([], [old["id"]]))
                    self.done(harness)


class CannotBeHeldTest(Base):
    def test_none_open_and_one_that_cannot_be_held_passes_the_whole_call_and_writes_nothing(self):
        shapeless = {"question": "Pick one", "options": [{"label": " "}]}
        for harness in HARNESSES:
            for bad in (TOO_LARGE, shapeless):
                with self.subTest(harness=harness, bad=bad["question"][:12]):
                    with self.file_writes(harness) as writes:
                        outcome = redirect.decide(harness, asked([SINGLE, bad, MULTI])[harness])
                    self.assertEqual(outcome, redirect.Outcome("pass"))
                    self.assertEqual((writes, self.open_ids(harness)), ([], []))
                    wait = interaction.read_wait(SID, harness)                 # one marker for the whole call
                    self.assertEqual(wait and wait["source"], "codex-question" if harness == "codex" else None)
                    self.done(harness)

    def test_some_open_and_one_that_cannot_be_made_is_refused_once_and_its_next_step_settles(self):
        shapeless = {"question": "Pick one", "options": [{"label": " "}]}
        for harness in HARNESSES:
            for bad in (TOO_LARGE, shapeless):
                with self.subTest(harness=harness, bad=bad["question"][:12]):
                    cli, _ = self.f.ask(harness, SID, text=SINGLE["question"], choices=["Dogwood", "Field Guide"])
                    with self.file_writes(harness) as writes:
                        outcome = redirect.decide(harness, asked([SINGLE, bad, MULTI])[harness])
                    self.assertEqual(outcome.kind, "unknown")
                    self.assertEqual((writes, self.open_ids(harness)), ([], [cli["id"]]))
                    self.assertIsNone(interaction.read_wait(SID, harness))               # no native question
                    self.assertIn("1 (" + cli["id"] + ")", outcome.reason)
                    self.assertIn("could not register questions 2, 3", outcome.reason)
                    self.assertIn("Ask questions 2, 3 again without the others", outcome.reason)
                    self.assertNotIn("\n", outcome.reason)
                    # the next step the line names settles it: the whole of 2 and 3 opens natively, or each alone
                    self.assertEqual(redirect.decide(harness, asked([bad, MULTI])[harness]).kind, "pass")
                    self.assertEqual(redirect.decide(harness, asked([bad])[harness]).kind, "pass")
                    maps = self.ids(redirect.decide(harness, asked([MULTI])[harness]), 1)[0]
                    self.assertEqual(self.open_ids(harness), sorted([cli["id"], maps]))
                    self.done(harness)

    def test_a_question_ended_at_this_prompt_is_one_that_cannot_be_made(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                cover, maps = self.ids(redirect.decide(harness, asked([SINGLE, MULTI])[harness]), 2)
                state.close_message(harness, SID, self.f.seat(harness, SID).key, cover)
                outcome = redirect.decide(harness, asked([SINGLE, MULTI])[harness])
                self.assertEqual(outcome.kind, "unknown")
                self.assertIn("2 (" + maps + ")", outcome.reason)
                self.assertIn("could not register question 1.", outcome.reason)
                self.assertEqual(self.open_ids(harness), [maps])
                self.assertEqual(redirect.decide(harness, asked([SINGLE])[harness]).kind, "pass")
                interaction.clear_wait(SID, harness)
                self.next_prompt(harness)
                again = self.ids(redirect.decide(harness, asked([SINGLE, MULTI])[harness]), 2)
                self.assertNotEqual(again[0], cover)
                self.assertEqual(again[1], maps)
                self.done(harness)

    def test_a_question_too_large_with_its_descriptions_goes_without_them_alone(self):
        long = {"question": "Which index wording?", "options": [
            {"label": "Short", "description": "s" * 2100}, {"label": "Long", "description": "l" * 2100}]}
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                cover, index = self.ids(redirect.decide(harness, asked([SINGLE, long])[harness]), 2)
                self.assertEqual(self.question(harness, cover)["descriptions"], ["The trail's name", "What the book is"])
                shown = self.question(harness, index)
                self.assertEqual((shown["choices"], "descriptions" in shown), (["Short", "Long"], False))
                self.done(harness)


class FailureTest(Base):
    """A failed write, a late registration and a lost result end as they do for one question."""

    def test_failures_before_the_write_pass_and_a_lost_result_reads_back_every_id(self):
        call = [SINGLE, MULTI, PREVIEW, SECRET]
        real = state.register_messages

        def slow(*args, **kwargs):
            time.sleep(10)

        def lost(*args, **kwargs):
            real(*args, **kwargs)
            raise TimeoutError("result lost after the write")
        for harness in HARNESSES:
            for name, patch in (("failed write", mock.patch.object(state, "_write", side_effect=OSError("disk full"))),
                                ("budget", mock.patch.object(state, "register_messages", side_effect=slow))):
                with self.subTest(harness=harness, failure=name):
                    started = time.monotonic()
                    with patch:
                        self.assertEqual(redirect.decide(harness, asked(call)[harness], seconds=1),
                                         redirect.Outcome("pass"))
                    self.assertLess(time.monotonic() - started, 4)
                    self.assertEqual(self.open_ids(harness), [])
                    self.done(harness)
            with self.subTest(harness=harness, failure="lost result"):
                with mock.patch.object(state, "register_messages", side_effect=lost):
                    found = self.ids(redirect.decide(harness, asked(call)[harness]), 4)
                self.assertEqual(self.open_ids(harness), sorted(found))
                self.assertEqual(self.ids(redirect.decide(harness, asked(call)[harness])), found)
                self.assertEqual(self.open_ids(harness), sorted(found))
                self.assertIsNone(interaction.read_wait(SID, harness))
                self.done(harness)


class OriginTest(Base):
    def test_origin_is_on_intercepted_records_only_and_a_reader_that_never_heard_of_it_counts_them(self):
        """3a's readers (`_question_row_ok`, unchanged here) take a row with a field they do not know, so a
        release with only 3a reads this file whole: no `unknown`, every question counted and listed."""
        cli, _ = self.f.ask("claude", SID, text="Print the trail sketches in color?", choices=["Color", "Gray"])
        found = self.ids(redirect.decide("claude", asked([SINGLE, MULTI, SECRET])["claude"]), 3)
        rows = self.rows("claude")
        self.assertNotIn("origin", rows[cli["id"]])
        self.assertEqual({rows[item_id]["origin"] for item_id in found}, {"native-question-tool"})
        health = []
        signals = [signal for signal in state.read_signals(health=health) if signal["session_id"] == SID]
        self.assertEqual(sorted(signal["id"] for signal in signals), sorted([cli["id"], *found]))
        self.assertEqual(health, [])
        self.assertEqual(state.wait_counts(signals)["total"], 4)
        self.assertTrue(state._refresh_cache())
        from fleet import process_identity
        with mock.patch("dispatch_parent_completion.interactive_parent_identity", return_value=("claude", SID)), \
             mock.patch.object(process_identity, "current_launch_context", return_value=None):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(questions.main(["list", "--json"]), 0)
        listed = json.loads(out.getvalue())
        self.assertTrue(listed["complete"])
        mine = [item["id"] for item in listed["items"] if item["id"] in {cli["id"], *found}]
        self.assertEqual(sorted(mine), sorted([cli["id"], *found]))


class FixOneTest(Base):
    """Review of #1948, fix 1: each back to main's path, an order changed or one condition, nothing added."""

    @contextlib.contextmanager
    def store_lock_taken(self):
        real, taken = state._cache_write_lock, []

        def spy():
            taken.append("store lock")
            return real()
        with mock.patch.object(state, "_cache_write_lock", side_effect=spy):
            yield taken

    def test_asking_an_open_question_again_takes_no_store_lock_and_a_new_one_decides_under_it(self):
        """Review 6: main returned the open id on the seat lock's read; the store lock is for writing."""
        if os.geteuid() == 0:
            self.skipTest("root opens a read-only lock file")
        choices = ["Dogwood", "Field Guide"]
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                cli, _ = self.f.ask(harness, SID, text=SINGLE["question"], choices=choices)
                maps = self.ids(redirect.decide(harness, asked([MULTI])[harness]), 1)[0]
                seat, lock = self.f.seat(harness, SID), state.state_root() / ".cache-write.lock"

                def ask_cli():
                    return state.register_message(harness, SID, seat, SINGLE["question"], choices=choices,
                                                  sequence=self.tidy.read_prompt_seq(seat), cwd=str(self.f.root))
                held = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(held, fcntl.LOCK_EX)                 # another process's write in progress
                result, thread = {}, threading.Thread(target=lambda: result.update(cli=ask_cli()))
                try:
                    started = time.monotonic()
                    with self.store_lock_taken() as taken:
                        thread.start()
                        thread.join(3)
                        self.assertFalse(thread.is_alive(), "questions ask waited for the store lock")
                        found = self.ids(redirect.decide(harness, asked([SINGLE, MULTI])[harness]), 2)
                    self.assertLess(time.monotonic() - started, 3)
                    self.assertEqual((result["cli"]["id"], found, taken), (cli["id"], [cli["id"], maps], []))
                finally:
                    fcntl.flock(held, fcntl.LOCK_UN)
                    os.close(held)
                    thread.join()
                os.chmod(lock, 0o400)                            # the lock file cannot be opened
                try:
                    self.assertEqual(ask_cli()["id"], cli["id"])
                    with self.store_lock_taken() as taken:
                        self.assertEqual(self.ids(redirect.decide(harness, asked([MULTI, SINGLE])[harness]), 2),
                                         [maps, cli["id"]])
                    self.assertEqual(taken, [])
                finally:
                    os.chmod(lock, 0o600)
                # one new record among them: decided again and written under the store lock, once
                with self.store_lock_taken() as taken, self.file_writes(harness) as writes:
                    added = self.ids(redirect.decide(harness, asked([SINGLE, PREVIEW, MULTI])[harness]), 3)
                self.assertEqual((added[0], added[2], len(taken), writes), (cli["id"], maps, 1, [set(added)]))
                self.done(harness)

    def test_the_interception_takes_the_open_same_question_before_this_registration_ended(self):
        """Review 4: an ended record of this very registration does not hide the same question still open."""
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                ended = self.ids(redirect.decide(harness, asked([SINGLE])[harness]), 1)[0]
                state.close_message(harness, SID, self.f.seat(harness, SID).key, ended)
                other, _ = self.f.ask(harness, SID, text=SINGLE["question"], choices=["Dogwood", "Field Guide"],
                                      recommended=1)               # the same question, another recommendation
                with self.file_writes(harness) as writes:
                    found = self.ids(redirect.decide(harness, asked([SINGLE, MULTI])[harness]), 2)
                self.assertEqual(found[0], other["id"])
                self.assertNotIn(found[1], (ended, other["id"]))
                self.assertEqual(writes, [{other["id"], found[1]}])
                self.done(harness)

    def test_questions_ask_still_returns_this_very_registration_ended_first(self):
        """`questions ask` (no `open_only`) is main's in every state."""
        choices = ["Dogwood", "Field Guide"]
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                first, _ = self.f.ask(harness, SID, text=SINGLE["question"], choices=choices)
                state.close_message(harness, SID, self.f.seat(harness, SID).key, first["id"])
                other, _ = self.f.ask(harness, SID, text=SINGLE["question"], choices=choices, recommended=1)
                again, _ = self.f.ask(harness, SID, text=SINGLE["question"], choices=choices)
                self.assertEqual((again["id"], again["lifecycle"]), (first["id"], "cancelled"))
                self.assertEqual(self.open_ids(harness), [other["id"]])
                self.done(harness)

    def test_registering_and_reading_back_a_lost_result_name_the_same_open_question(self):
        """Review 4: an earlier release's form of A (its descriptions in the text) and this release's A both open;
        the registration and the read-back take the same one: oldest first, then by id."""
        old_text = SINGLE["question"] + "\n- Dogwood: The trail's name\n- Field Guide: What the book is"
        words = {"Dogwood": "The trail's name", "Field Guide": "What the book is"}
        real = state.register_messages

        def lost(*args, **kwargs):
            real(*args, **kwargs)
            raise TimeoutError("result lost after the write")
        for harness in HARNESSES:
            for times in ("older first", "same time"):
                for path in ("registered", "lost result"):
                    with self.subTest(harness=harness, times=times, path=path):
                        old, _ = self.f.ask(harness, SID, text=old_text, choices=["Dogwood", "Field Guide"])
                        new, _ = self.f.ask(harness, SID, text=SINGLE["question"], choices=["Dogwood", "Field Guide"],
                                            recommended=0, descriptions=words)   # this call's own record of A
                        if times == "same time":         # the file lists the larger id first
                            rows = self.rows(harness)
                            for row in rows.values():
                                row["since"] = rows[old["id"]]["since"]
                            state._write(state._questions_path(harness, SID), {
                                "schema_version": 1, "source": "S4-questions", "harness": harness,
                                "session_id": SID, "questions": dict(sorted(rows.items(), reverse=True))})
                        first = min((old["id"], new["id"])) if times == "same time" else old["id"]
                        patch = (mock.patch.object(state, "register_messages", side_effect=lost)
                                 if path == "lost result" else contextlib.nullcontext())
                        with patch:
                            found = self.ids(redirect.decide(harness, asked([SINGLE, MULTI])[harness]), 2)
                        self.assertEqual(found[0], first)
                        self.done(harness)

    def test_a_question_too_large_still_finds_the_same_one_open_in_an_earlier_release_slot(self):
        """Review 5: the seat is known before the questions are built, so the read-back sees the earlier release's
        slot: its LF text fits (4,096 bytes), the same text with CRLF does not, and the open id is named."""
        def text(room):
            return "Read the notes?\n" + "x" * room + "\nok\ny"

        def size(value):
            return len(json.dumps(state._message_detail({"text": value}), separators=(",", ":")))
        lf_text = text(4096 - size(text(0)))
        crlf = {"question": lf_text.replace("\n", "\r\n"), "options": []}
        self.assertEqual(size(lf_text), 4096)
        with self.assertRaisesRegex(ValueError, "question-detail-too-large"):
            size(crlf["question"])                               # 4,102 bytes
        for harness in HARNESSES:
            for seat_known in (True, False):
                with self.subTest(harness=harness, seat_known=seat_known):
                    seat = self.f.seat(harness, SID)
                    old = {"schema_version": 1, "mode": "message", "source": "S4", "id": "S4:" + "c" * 64,
                           "harness": harness, "session_id": SID, "seat_key": seat.key, "sequence": 0,
                           "question": lf_text, "detail": state._message_detail({"text": lf_text}),
                           "since": time.time() - 60, "lifecycle": "open", "cwd": str(self.f.root), "incarnation": None}
                    slot = state._session_signal_path(old)
                    state._write(slot, old)                          # as an earlier release wrote it
                    unproven = (mock.patch.object(self.tidy, "resolve_seat", return_value=types.SimpleNamespace(key=None))
                                if not seat_known else contextlib.nullcontext())
                    with unproven, self.file_writes(harness) as writes:
                        outcome = redirect.decide(harness, asked([crlf])[harness])
                    if seat_known:
                        self.assertEqual(self.ids(outcome, 1), [old["id"]])
                        self.assertIsNone(interaction.read_wait(SID, harness))       # nothing opens natively
                    else:                                        # no proven seat: no slot to read, as before
                        self.assertEqual(outcome, redirect.Outcome("pass"))
                    self.assertEqual(writes, [])
                    os.unlink(slot)
                    self.done(harness)

    def test_options_that_are_no_list_make_that_question_one_a_record_cannot_carry(self):
        """Review 1: an integer or an object as a question's `options` sinks only that question."""
        for harness in HARNESSES:
            for bad in ({"question": "How many copies?", "options": 7},
                        {"question": "Which name?", "options": {"label": "Name"}}):
                with self.subTest(harness=harness, bad=bad["question"]):
                    with self.file_writes(harness) as writes:
                        outcome = redirect.decide(harness, asked([SINGLE, bad, MULTI])[harness])
                    self.assertEqual((outcome, writes), (redirect.Outcome("pass"), []))
                    wait = interaction.read_wait(SID, harness)               # one marker for the whole call
                    self.assertEqual(wait and wait["source"], "codex-question" if harness == "codex" else None)
                    self.done(harness)
                    self.assertEqual(redirect.decide(harness, asked([bad])[harness]), redirect.Outcome("pass"))
                    self.done(harness)
                    cli, _ = self.f.ask(harness, SID, text=SINGLE["question"], choices=["Dogwood", "Field Guide"])
                    outcome = redirect.decide(harness, asked([MULTI, bad, SINGLE])[harness])
                    self.assertEqual(outcome.kind, "unknown")
                    self.assertIn("3 (" + cli["id"] + ")", outcome.reason)
                    self.assertIn("could not register questions 1, 2", outcome.reason)
                    self.assertEqual((self.open_ids(harness), interaction.read_wait(SID, harness)), ([cli["id"]], None))
                    self.done(harness)

    def test_string_options_no_options_and_an_empty_list_are_as_before(self):
        for harness in HARNESSES:
            for options, choices in ((["Yes", "No"], ["Yes", "No"]), (None, []), ([], [])):
                with self.subTest(harness=harness, options=options):
                    call = {"question": "Print the index?", **({} if options is None else {"options": options})}
                    found = self.ids(redirect.decide(harness, asked([call])[harness]), 1)
                    self.assertEqual(self.question(harness, found[0])["choices"], choices)
                    self.done(harness)


if __name__ == "__main__":
    unittest.main()
