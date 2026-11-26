"""11-17: Fleet's unfolded question parts its words from its choices as the shell's question popup does.

The question's words, one empty row, a thin line, then the choices with each number in the question's rose and bold;
a question answered in words has neither the empty row nor the line; short of rows the empty row goes first and a
choice never loses its row to them. Synthetic values only.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.view_model import ExplorerViewModel

LABELS = ("진행 (그대로)", "수정", "멈춤")
WORDS = "패널·상태줄·Fleet 세 화면을 고칩니다. 어느 안으로 할까요?\n- 가: 그대로\n- 나: 조금 바꿈"
REF = {"id": "S1:q0", "harness": "codex", "session_id": "sid-0", "request_id": "req-0", "turn_id": "turn-0",
       "question_id": "qq-0"}


def _vm():
    row = {"id": REF["id"], "tag": REF["id"], "ref": REF, "kind": "question", "source": "S1-question", "wait": "1m",
           "h": "codex", "m": ("gpt-6.1-sol", "fam_gpt"), "e": "xhigh", "session_tag": "a0", "open_tag": "a0", "pane": "w1:p2"}
    return ExplorerViewModel(questions=(row,), system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})


def _state(choices):
    envelope = {"v": 1, "harness": "codex", "session_id": REF["session_id"], "request_id": REF["request_id"],
                "turn_id": REF["turn_id"], "question_id": REF["question_id"], "kind": "question", "state": "open",
                "detail": {"questions": [{"id": REF["question_id"], "question": WORDS, "choices": list(choices)}]},
                "support": {"supported": True}, "native_ref": {"transport": "fixture", "epoch": 1}}
    return NavState(view="QUESTIONS", title="QUESTIONS", scroll=(), folds=(("qsel:QUESTIONS", REF["id"]),),
                    question_overlay={"ref": REF, "detail": envelope, "draft": None, "state": "open",
                                      "editing": False, "pending": False})


def _rows(choices, rows=45, cols=140):
    frame = draw(_vm(), _state(choices), cols, rows)
    text = [row.rstrip() for row in frame.text_rows()]
    return frame, text


def _pane(text, index):
    """What the question's pane holds on one row (the tree's column stays out of it)."""
    return text[index][34:].strip().rstrip("│").strip()


def _at(text, needle):
    return next(i for i, row in enumerate(text) if needle in row)


class FleetQuestionSplit(unittest.TestCase):
    def test_words_then_one_empty_row_then_a_line_then_the_choices(self):
        frame, text = _rows(LABELS)
        last = _at(text, "- 나: 조금 바꿈")
        first = _at(text, "1  진행")
        self.assertEqual(first, last + 3, "\n".join(text))
        self.assertEqual(_pane(text, last + 1), "", "one empty row")
        line = _pane(text, last + 2)
        self.assertTrue(line and set(line) == {"─"}, text[last + 2])
        # The line starts where the question's words start; the empty row is empty to its edge.
        self.assertEqual(text[last + 2].index("─"), text[last].index("-"))
        self.assertEqual([_at(text, f"{n}  ") for n in (1, 2, 3)], [first, first + 1, first + 2])

    def test_each_number_is_the_questions_rose_and_bold_and_the_line_is_dim(self):
        frame, text = _rows(LABELS)
        first = _at(text, "1  진행")
        column = text[first].index("1  진행")
        number = frame.cells[first][column]
        self.assertEqual(number[0], "1")
        from tools.fleet.palette import style_components
        rose, mark = style_components(number[1]), style_components("g_blocked")
        self.assertEqual(rose["foreground"], mark["foreground"])   # the `◑` mark's color, and bold
        self.assertTrue(rose["bold"] and not rose["dim"])
        self.assertEqual(frame.cells[first][column + 3][1], "loc_repo")
        rule = frame.cells[first - 1][text[first - 1].index("─")]
        self.assertTrue(style_components(rule[1])["dim"])

    def test_a_question_answered_in_words_has_neither(self):
        frame, text = _rows(())
        last = _at(text, "- 나: 조금 바꿈")
        # The row under its words is the next thing, not an empty row and not a line.
        self.assertIn("answer in its window", text[last + 1], "\n".join(text))
        self.assertNotIn("1  ", "\n".join(text[last:last + 3]))

    def test_short_of_rows_the_empty_row_goes_first_and_no_choice_loses_its_row(self):
        seen = set()
        for rows in range(12, 40):
            frame, text = _rows(LABELS, rows=rows)
            joined = "\n".join(text)
            present = sum(1 for n in (1, 2, 3) if f"{n}  " in joined)
            if "1  진행" not in joined:
                continue
            at = _at(text, "1  진행")
            has_line = bool(_pane(text, at - 1)) and set(_pane(text, at - 1)) == {"─"}
            has_blank = has_line and not _pane(text, at - 2)
            seen.add((has_blank, has_line))
            if has_blank:
                self.assertTrue(has_line)
            if present < 3:
                continue
            self.assertIn(f"3  {LABELS[2]}", joined)
        # Every state is reached: roomy (empty row and line), tight (the line alone), and none where a choice would give way.
        self.assertIn((True, True), seen)
        self.assertIn((False, True), seen)


if __name__ == "__main__":
    unittest.main()
