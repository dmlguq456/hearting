"""11-22: Fleet's unfolded question keeps its choice rows and its answer row in sight, and a registered question with
choices takes words beside them.

A session registers a question with ``questions ask --choice …``; it used to read as a choice-only question (no answer
row). A long question used to push the choices off the board: now only its words give way, ending in
``… +N more lines``; short of rows the empty row goes first, then the thin line, then the words.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.view_model import ExplorerViewModel


def board(lines=1, choices=4, transport="hearting-message-answer", **extra):
    ref = {"id": "S4:synthetic", "harness": "codex", "session_id": "synthetic-session",
           "request_id": "synthetic-request", "question_id": "q"}
    question = {"id": "q", "question": "\n".join(f"question line {n + 1}" for n in range(lines)),
                "choices": [{"value": f"v{n + 1}", "label": f"Choice {n + 1}"} for n in range(choices)], **extra}
    envelope = {"kind": "question", "state": "open", "harness": "codex", "questions": [question],
                "support": {"supported": True}, "native_ref": {"transport": transport}}
    row = {"id": ref["id"], "tag": ref["id"], "ref": ref, "kind": "question", "h": "codex",
           "session_tag": "a1", "source": "S4", "wait": "1m"}
    vm = ExplorerViewModel(questions=(row,), system={"answer_waits": {"total": 1, "complete": True}})
    state = NavState(view="QUESTIONS", title="QUESTIONS", folds=(("qsel:QUESTIONS", ref["id"]),),
                     question_overlay={"ref": ref, "detail": envelope, "draft": None, "state": "open",
                                       "editing": False, "pending": False})
    return state, vm, ref


def rows_of(vm, state, height, cols=120):
    return draw(vm, state, cols, height, beat=1).text_rows()


def shown(rows):
    return [int(line.split("Choice ")[1].split()[0]) for line in rows if "Choice " in line]


class QuestionChoices(unittest.TestCase):
    def test_a_registered_question_with_four_choices_has_its_answer_row_and_takes_the_text(self):
        state, vm, ref = board(choices=4)       # no `is_other` flag at all
        rows = rows_of(vm, state, 30)
        self.assertEqual(shown(rows), [1, 2, 3, 4])
        self.assertTrue(any(line.strip().startswith(("> ", "│     > ")) or "  > " in line for line in rows), rows)
        state, effects = reduce(state, {"type": "click", "action": ("question.edit", "q")})
        state, effects = reduce(state, {"type": "key", "key": "text:책임져"})
        self.assertEqual(effects[0][0], "questions.draft")
        # (A choice and the text together, and text alone, go out in the existing forms: see
        # `test_choice_and_typed_note_survive_the_board_and_courier` in `test_explorer_question_board.py`.)
        self.assertEqual(state.question_overlay["draft"]["answers"]["q"], ["책임져"])

    def test_a_native_question_with_choices_stays_choice_only(self):
        state, vm, _ = board(choices=3, transport="fixture")
        rows = rows_of(vm, state, 30)
        self.assertEqual(shown(rows), [1, 2, 3])
        self.assertFalse(any("  > " in line for line in rows), rows)

    def test_a_long_question_never_pushes_the_choices_out(self):
        for lines in (1, 5, 17, 40):
            for choices in (2, 4, 9):
                for height in (22, 30, 40):
                    with self.subTest(lines=lines, choices=choices, height=height):
                        state, vm, _ = board(lines=lines, choices=choices)
                        rows = rows_of(vm, state, height)
                        seen = shown(rows)
                        self.assertGreaterEqual(len(seen), 1, "\n".join(rows))
                        capacity = height - 5 - 4        # rows under the board's bottom, less its own rows
                        if choices + 3 <= capacity:
                            self.assertEqual(seen, list(range(1, choices + 1)), "\n".join(rows))
                            if lines > capacity - choices - 2:
                                self.assertTrue(any("more lines" in line for line in rows), "\n".join(rows))

    def test_a_short_question_draws_as_before(self):
        state, vm, _ = board(lines=2, choices=3)
        rows = rows_of(vm, state, 40)
        self.assertFalse(any("more lines" in line for line in rows))
        self.assertTrue(any("question line 2" in line for line in rows))
        index = next(i for i, line in enumerate(rows) if "question line 2" in line)
        self.assertEqual(rows[index + 1].split("│")[-1].strip(), "")
        self.assertIn("─", rows[index + 2])


if __name__ == "__main__":
    unittest.main()
