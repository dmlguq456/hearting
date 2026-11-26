"""11-15: a message body's and a question's markdown tables drawn by the documents' one table rule.

``transcript.table_parts(lines, width)`` keeps every line that is no table's as the string it was (its
caller wraps it as before) and gives each table as Fleet's drawn rows (``doc_rows``' rule: GFM recognition,
two cells between columns, the head and ``┈``, wide tables wrapped in their cells). The question popup's
body (``session_block.question_presentations``) draws a table in the question's text that way; the answer
path (choices, typed answer, sending) is untouched. ``doc_rows`` itself draws as before.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.fleet.explorer import transcript  # noqa: E402
from tools.fleet.explorer.grid import cell_width  # noqa: E402
from tools.fleet import session_block  # noqa: E402

TABLE = ["| 항목 | 상태 | 비고 |", "|:--|:-:|--:|", "| 팝업 | 병합 | 1 |", "| 표 `a|b` 칸 | 열림 | 12 |"]


def text(row):
    return "".join(part for part, _key in row)


class TablePartsTest(unittest.TestCase):
    def test_lines_stay_and_a_table_comes_drawn(self):
        lines = ["앞 줄 | 막대가 든 글", *TABLE, "뒤 줄"]
        parts = transcript.table_parts(lines, 40)
        self.assertEqual(parts[0], "앞 줄 | 막대가 든 글")      # a `|` line without a delimiter row is prose
        self.assertEqual(parts[-1], "뒤 줄")
        drawn = parts[1]
        self.assertIsInstance(drawn, list)
        self.assertEqual(len(parts), 3)
        # The same rows ``doc_rows`` draws for the table (its one rule), without the empty row after it.
        expected = transcript.doc_rows(TABLE, 40)
        self.assertEqual(drawn, expected[:len(drawn)])
        heads = text(drawn[0])
        self.assertTrue(heads.startswith("항목") and "상태" in heads and heads.rstrip().endswith("비고"), heads)
        self.assertEqual(set(text(drawn[1]).strip()), {"┈"})
        self.assertIn("a|b", text(drawn[3]))             # a bar in a code span stays in its cell
        self.assertTrue(text(drawn[2]).rstrip().endswith("1") and text(drawn[3]).rstrip().endswith("12"))
        self.assertEqual(cell_width(text(drawn[2]).rstrip()), cell_width(text(drawn[3]).rstrip()))  # right-aligned

    def test_every_width_keeps_the_rows_inside_and_fences_hold_no_table(self):
        fenced = ["```", *TABLE, "```"]
        self.assertEqual(transcript.table_parts(fenced, 60), fenced)
        indented = ["    " + line for line in TABLE]
        self.assertEqual(transcript.table_parts(indented, 60), indented)
        for width in range(8, 121):
            for item in transcript.table_parts(["x", *TABLE, "| 남는 | 칸 | 이 | 더 |", "y"], width):
                if isinstance(item, list):
                    for row in item:
                        self.assertLessEqual(sum(cell_width(part) for part, _key in row), width, (width, row))

    def test_the_question_popup_body_draws_a_table_in_the_question(self):
        question = "어느 쪽으로 할까요?\n" + "\n".join(TABLE) + "\n고른 뒤 보내 주세요."
        items = [{"id": "q", "answerable": True, "questions": [{"id": "a", "question": question, "choices": ["가", "나"]}]}]
        session_block.question_presentations(items, 64)
        display = items[0]["questions"][0]["display"]
        body = [text(row) for row in display["body"]]
        self.assertEqual(body[0], "어느 쪽으로 할까요?")
        self.assertEqual(body[-1], "고른 뒤 보내 주세요.")
        self.assertTrue(any(set(row.strip()) == {"┈"} for row in body), body)
        self.assertFalse(any("|:--" in row or row.startswith("| 항목") for row in body), body)
        # The choices (the answer path) are as before.
        self.assertEqual([text(row) for row in display["options"]], ["1  가", "2  나"])
        # A question without a table reads as it did (each line wrapped).
        plain = [{"id": "q", "answerable": True, "questions": [{"id": "a", "question": "첫 줄\n\n둘째 줄 | 막대"}]}]
        session_block.question_presentations(plain, 64)
        self.assertEqual([text(row) for row in plain[0]["questions"][0]["display"]["body"]], ["첫 줄", "둘째 줄 | 막대"])


if __name__ == "__main__":
    unittest.main()
