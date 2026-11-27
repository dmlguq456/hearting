"""11-22: Fleet's question board gives a typed answer a place inside it (the shell's question input has the same one).

While an answer is typed (``editing``) and has text, ←→ move one character as the person sees it, Home/End (and Ctrl+A/E) go
to its ends, typed characters go in at the place, Backspace removes before it and Delete at it. Without text, or with the
answer not being typed, ←→ keep their meaning (the view switch, as Tab and Shift+Tab); the sent answer, the saved draft and
the 4,096-byte limit are as before. The place is drawn between the characters and a text wider than the row slides.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from types import SimpleNamespace
from unittest import mock

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.view_model import ExplorerViewModel

TABS = ["QUESTIONS", "OVERVIEW"]


class QuestionCaret(unittest.TestCase):
    def setup_question(self, **extra):
        ref = {"id": "S4:synthetic", "harness": "codex", "session_id": "synthetic-session",
               "request_id": "synthetic-request", "question_id": "q"}
        question = {"id": "q", "question": "합성 질문", "is_other": True, "choices": [], **extra}
        envelope = {"kind": "question", "state": "open", "harness": "codex", "questions": [question],
                    "support": {"supported": True}, "native_ref": {"transport": "hearting-message-answer"}}
        row = {"id": ref["id"], "tag": ref["id"], "ref": ref, "kind": "question", "h": "codex",
               "session_tag": "a1", "source": "S4", "wait": "1m"}
        vm = ExplorerViewModel(questions=(row,), system={"answer_waits": {"total": 1, "complete": True}})
        state = NavState(view="QUESTIONS", title="QUESTIONS", folds=(("qsel:QUESTIONS", ref["id"]),),
                         question_overlay={"ref": ref, "detail": envelope, "draft": None, "state": "open",
                                           "editing": False, "pending": False})
        return state, vm, ref

    def run_keys(self, keys, **extra):
        state, vm, ref = self.setup_question(**extra)
        for key in keys:
            if key == "click-write":
                event = {"type": "click", "action": ("question.edit", "q")}
            else:
                event = {"type": "key", "key": key, "tabs": TABS}
            state, effects = reduce(state, event)
        return state, vm, ref

    @staticmethod
    def typed(state):
        from tools.fleet.explorer.state import _question_text_draft
        return _question_text_draft(state.question_overlay)

    @staticmethod
    def row(vm, state, cols=100):
        rows = draw(vm, state, cols, 40, beat=1).text_rows()
        return next(line for line in rows if "> " in line and ("▏" in line or "…" in line)).rstrip()

    def type_text(self, text):
        return ["click-write"] + ["text:" + char for char in text]

    def test_typing_backspace_and_delete_go_to_the_place(self):
        state, vm, _ = self.run_keys(self.type_text("abcd") + ["left", "left", "text:X"])
        self.assertEqual(self.typed(state), "abXcd")
        self.assertIn("> abX▏cd", self.row(vm, state))
        state, vm, _ = self.run_keys(self.type_text("abcd") + ["left", "left", "backspace"])
        self.assertEqual(self.typed(state), "acd")
        self.assertIn("> a▏cd", self.row(vm, state))
        state, vm, _ = self.run_keys(self.type_text("abcd") + ["left", "left", "delete"])
        self.assertEqual(self.typed(state), "abd")
        self.assertIn("> ab▏d", self.row(vm, state))
        # Delete at the end and Backspace at the start take nothing, and neither pauses the typing.
        state, _, _ = self.run_keys(self.type_text("ab") + ["delete", "home", "backspace"])
        self.assertEqual(self.typed(state), "ab")
        self.assertTrue(state.question_overlay["editing"])
        # Delete with no text yet is the text's, not the view's.
        state, _, _ = self.run_keys(["click-write", "delete"])
        self.assertTrue(state.question_overlay["editing"])
        self.assertEqual(state.view, "QUESTIONS")

    def test_home_end_and_ctrl_a_e(self):
        for start, end in (("home", "end"), ("ctrl-a", "ctrl-e")):
            with self.subTest(start=start):
                state, vm, _ = self.run_keys(self.type_text("hello") + [start, "text:X"])
                self.assertEqual(self.typed(state), "Xhello")
                self.assertIn("> X▏hello", self.row(vm, state))
                state, vm, _ = self.run_keys(self.type_text("hello") + [start, end, "text:Y"])
                self.assertEqual(self.typed(state), "helloY")
                self.assertEqual(self.row(vm, state).split("> ", 1)[1].strip(), "helloY▏")
        # Arrows stop at the ends.
        state, _, _ = self.run_keys(self.type_text("ab") + ["left"] * 5 + ["text:Z"] + ["right"] * 5 + ["text:Y"])
        self.assertEqual(self.typed(state), "ZabY")

    def test_one_step_is_one_character_the_person_sees(self):
        state, _, _ = self.run_keys(self.type_text("가나다") + ["left", "left", "text:X"])
        self.assertEqual(self.typed(state), "가X나다")
        state, _, _ = self.run_keys(self.type_text("漢字語") + ["left", "left", "backspace"])
        self.assertEqual(self.typed(state), "字語")
        for text, keys, want in (("aéb", ["left", "backspace"], "ab"),      # Backspace: the whole character
                                 ("aéb", ["left", "left", "delete"], "ab"),  # Delete too
                                 ("각z", ["left", "left", "text:q"], "q각z"),
                                 ("a\U0001f44d\U0001f3fdb", ["left", "left", "delete"], "ab")):
            with self.subTest(text=text, keys=keys):
                state, _, _ = self.run_keys(self.type_text(text) + keys)
                self.assertEqual(self.typed(state), want)

    def test_arrows_keep_their_meaning_without_text_or_without_typing(self):
        # Not typing, no text: ←→ switch the view as before (Tab and Shift+Tab too).
        state, _, _ = self.run_keys(["right"])
        self.assertEqual(state.view, "OVERVIEW")
        for key in ("left", "tab", "shift-tab"):
            with self.subTest(key=key):
                state, _, _ = self.run_keys([key])
                self.assertEqual(state.view, "OVERVIEW")
        # Typing with no text yet: the same.
        state, _, _ = self.run_keys(["click-write", "right"])
        self.assertEqual(state.view, "OVERVIEW")
        # Text, but the typing paused (Esc): ←→ are the view's again; Tab always is.
        state, _, _ = self.run_keys(self.type_text("abc") + ["escape", "left"])
        self.assertEqual(state.view, "OVERVIEW")
        self.assertEqual(self.typed(state), "abc")
        # While typing with text, ←→ do not leave the view, and Tab still does.
        state, _, _ = self.run_keys(self.type_text("abc") + ["left", "right"])
        self.assertEqual(state.view, "QUESTIONS")
        state, _, _ = self.run_keys(self.type_text("abc") + ["tab"])
        self.assertEqual(state.view, "OVERVIEW")
        state, _, _ = self.run_keys(self.type_text("abc") + ["shift-tab"])
        self.assertEqual(state.view, "OVERVIEW")

    def test_the_sent_answer_and_the_saved_draft_have_the_same_shape(self):
        for keys in (self.type_text("abcd") + ["left", "left", "text:X"], self.type_text("abXcd")):
            state, vm, ref = self.run_keys(keys)
            before = state
            state, effects = reduce(state, {"type": "key", "key": "enter", "question_refs": [ref]})
            submit = next(effect for effect in effects if effect[0] == "questions.submit")
            self.assertEqual(submit[2]["answers"]["q"], ["abXcd"])
            self.assertNotIn("caret", str(submit))
            drafts = [effect for effect in reduce(before, {"type": "key", "key": "text:!"})[1] if effect[0] == "questions.draft"]
            self.assertEqual(len(drafts), 1)
            self.assertNotIn("caret", str(drafts))
        # The saved draft carries the text only.
        state, vm, ref = self.run_keys(self.type_text("ab"))
        state, effects = reduce(state, {"type": "key", "key": "left"})
        self.assertEqual(effects, ())
        state, effects = reduce(state, {"type": "key", "key": "text:X"})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(effects[0][2], "aXb")        # one text question: the draft is the text itself

    def test_the_limit_refuses_an_insert_in_the_middle_and_the_text_stays(self):
        state, vm, _ = self.setup_question()
        state, _ = reduce(state, {"type": "click", "action": ("question.edit", "q")})
        value = "a" * 4096
        state, effects = reduce(state, {"type": "key", "key": "text:" + value})
        self.assertEqual(effects[0][0], "questions.draft")
        state, _ = reduce(state, {"type": "key", "key": "left"})
        state, _ = reduce(state, {"type": "key", "key": "left"})
        state, effects = reduce(state, {"type": "key", "key": "text:b"})
        self.assertEqual(effects, ())
        self.assertTrue(state.question_overlay.get("answer_full"))
        self.assertEqual(self.typed(state), value)
        # The next key clears the notice, and an insert that fits (after a Delete) goes in at the place.
        state, effects = reduce(state, {"type": "key", "key": "delete"})
        self.assertFalse(state.question_overlay.get("answer_full"))
        state, effects = reduce(state, {"type": "key", "key": "text:b"})
        self.assertEqual(self.typed(state), "a" * 4094 + "b" + "a")

    def test_a_secret_answer_is_bullets_and_the_place_moves_over_them(self):
        state, vm, _ = self.run_keys(self.type_text("private") + ["left", "left", "left"], is_secret=True)
        row = self.row(vm, state)
        self.assertIn("> ••••▏•••", row)
        self.assertNotIn("private", "\n".join(draw(vm, state, 100, 40, beat=1).text_rows()))
        state, _, _ = self.run_keys(self.type_text("private") + ["left", "left", "left", "backspace"], is_secret=True)
        self.assertEqual(self.typed(state), "priate")

    def test_a_text_wider_than_the_row_wraps_so_the_place_stays_in_sight(self):
        # 11-26: a written answer wider than its row wraps onto the next rows (it slid within one row before).
        from tools.fleet.explorer.state import question_answer_rows
        text = "고쳐 쓰는 글이 한 줄보다 길어서 가운데에서 고치려면 자리가 보여야 합니다. " * 3
        for cols in (60, 100):
            with self.subTest(cols=cols):
                state, vm, _ = self.run_keys(self.type_text(text) + ["home"] + ["right"] * 20)
                frame = draw(vm, state, cols, 40, beat=1)
                width = dict(frame.answer_layout)["q"]
                rows = question_answer_rows(text, width)
                self.assertGreater(len(rows), 1)
                drawn = [line.replace("▏", "") for line in frame.text_rows()]
                for start, end in rows:
                    self.assertTrue(any(text[start:end].strip() in line for line in drawn), text[start:end])
                caret = next(line for line in frame.text_rows() if "▏" in line)
                home = max(i for i, (start, _end) in enumerate(rows) if start <= 20)
                self.assertTrue(caret.split("▏")[0].endswith(text[rows[home][0]:20]), caret)
                state, vm, _ = self.run_keys(self.type_text(text) + ["end"])
                caret = next(line for line in draw(vm, state, cols, 40, beat=1).text_rows() if "▏" in line)
                self.assertTrue(caret.rstrip().endswith("▏"), caret)

    def test_returning_to_a_question_puts_the_place_at_the_end(self):
        state, vm, ref = self.run_keys(self.type_text("abcd") + ["home"])
        self.assertEqual(state.question_overlay["caret"], ("q", 0))
        state, _ = reduce(state, {"type": "key", "key": "up"})        # leaves the typing
        self.assertNotIn("caret", state.question_overlay)
        state, _ = reduce(state, {"type": "click", "action": ("question.edit", "q")})
        state, _ = reduce(state, {"type": "key", "key": "text:Z"})
        self.assertEqual(self.typed(state), "abcdZ")

    def test_the_key_legend_names_the_keys_that_work_while_an_answer_is_typed(self):
        state, vm, _ = self.run_keys(self.type_text("abc"))
        legend = draw(vm, state, 140, 40, beat=1).text_rows()[-2]
        self.assertIn("←→·Home/End cursor", legend)
        self.assertIn("⏎ send · Ctrl+J newline", legend)
        self.assertNotIn("←→ focus", legend)
        self.assertNotIn("Home/End·wheel", legend)
        self.assertIn("Tab view", legend)
        for keys in ([], ["click-write"], self.type_text("abc") + ["escape"]):
            state, vm, _ = self.run_keys(keys)
            legend = draw(vm, state, 140, 40, beat=1).text_rows()[-2]
            self.assertIn("←→ focus", legend)
            self.assertIn("PgUp/Dn·Home/End·wheel scroll", legend)

    def test_the_terminal_keys_reach_the_reducer_only_while_an_answer_is_typed(self):
        import curses
        from tools.fleet import render
        seen = []
        state, vm, _ = self.run_keys(self.type_text("abc"))
        apply = lambda effects, *_args, **_kw: None  # noqa: E731
        with mock.patch.object(render, "_EXPLORER_MODEL", vm), \
                mock.patch.object(render, "_EXPLORER_STATE", state), \
                mock.patch.object(render, "_explorer_apply_effects", apply):
            for ch, key in ((curses.KEY_DC, "delete"), (1, "ctrl-a"), (5, "ctrl-e"), (curses.KEY_HOME, "home"),
                            (curses.KEY_END, "end"), (curses.KEY_LEFT, "left"), (curses.KEY_RIGHT, "right")):
                with self.subTest(key=key):
                    from tools.fleet.explorer import state as reducer
                    with mock.patch.object(reducer, "reduce", side_effect=lambda s, e, f=None: (seen.append(e["key"]) or (s, ()))):
                        render._explorer_handle(ch)
                    self.assertEqual(seen[-1], key)
            # Not typing: Delete, Ctrl+A and Ctrl+E are not keys of the board, as before.
            quiet, _, _ = self.run_keys(self.type_text("abc") + ["escape"])
            with mock.patch.object(render, "_EXPLORER_STATE", quiet):
                for ch in (curses.KEY_DC, 1, 5):
                    count = len(seen)
                    render._explorer_handle(ch)
                    self.assertEqual(len(seen), count)


if __name__ == "__main__":
    unittest.main()
