"""A document drawn for a popup: markdown tables as aligned tables, fences as written, prose as Fleet cards.

``transcript.doc_rows`` takes a document's lines and a width and returns ``(text, key)`` rows. These
checks use made-up documents only; nothing reads a real status document.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from collections import Counter
from pathlib import Path
import time
import unicodedata
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.fleet.explorer import transcript  # noqa: E402
from tools.fleet.explorer.grid import cell_width  # noqa: E402


def text(row):
    return "".join(part for part, _key in row)


def drawn(lines, width):
    return [text(row) for row in transcript.doc_rows(lines, width)]


def keys(lines, width):
    return [[(part, key) for part, key in row] for row in transcript.doc_rows(lines, width)]


class TableTest(unittest.TestCase):
    def test_korean_columns_line_up_by_cell_width(self):
        rows = drawn(["| 항목 | 값 |", "|---|---|", "| 팝업 틀 | 병합 |", "| ab | 시안 |"], 60)
        self.assertEqual(rows[0], "항목" + " " * 5 + "값")           # 항목 is 4 cells, 팝업 틀 is 7
        self.assertEqual(rows[1], "┈" * 13)                          # 7 + 2 + 4
        self.assertEqual(rows[2], "팝업 틀  병합")
        self.assertEqual(rows[3], "ab" + " " * 7 + "시안")
        self.assertEqual(rows[4], "")                                # one empty row after the table
        for row in rows[:4]:
            self.assertLessEqual(cell_width(row), 13)

    def test_the_three_alignments_and_the_header_follow_the_delimiter(self):
        rows = drawn(["| 가 | 나 | 다 |", "|:--|:-:|--:|", "| a | b | c |", "| aaaa | bbbb | cccc |"], 60)
        self.assertEqual(rows[2], "a      b       c")
        self.assertEqual(rows[3], "aaaa  bbbb  cccc")
        self.assertTrue(rows[0].startswith("가") and rows[0].endswith("다"))
        aligns = transcript.table_aligns("|:--|:-:|--:|", 3)
        self.assertEqual(aligns, ["left", "center", "right"])

    def test_header_rule_and_cell_keys(self):
        rows = keys(["| `cmd` | **bold** | [link](http://x) |", "|---|---|---|", "| `a` | **b** | [c](http://y) |"], 60)
        self.assertEqual([k for _t, k in rows[0] if k], ["code", "head", "head"])   # head key, code stays code
        self.assertEqual(rows[1], [("┈" * 15, "line")])
        body = [(t, k) for t, k in rows[2] if k]
        self.assertEqual(body, [("a", "code"), ("b", "bold"), ("c", "soft")])         # a link is its words

    def test_wide_table_wraps_inside_columns_and_loses_no_character(self):
        cells = [["날짜", "명령", "메모"],
                 ["11-14", "python3 tools/run-tests.py --select session_shell_panel", "높이 8~60 폭 24~60 훑기 겹침 없음"],
                 ["11-15", "tools_run_tests_select_session_shell_table_popup_pty_" + "x" * 30, "통과"]]
        lines = ["| " + " | ".join(row) + " |" for row in cells]
        lines.insert(1, "|---|---|---|")
        want = Counter("".join(cell.replace(" ", "") for row in cells for cell in row))
        for width in (116, 80, 60, 40, 30, 24):
            rows = drawn(lines, width)
            for row in rows:
                self.assertLessEqual(cell_width(row), width)
            body = [row for row in rows if not row.startswith("┈")]
            self.assertEqual(Counter("".join(body).replace(" ", "")), want, width)    # nothing lost
        narrow = drawn(lines, 40)
        self.assertGreater(len(narrow), len(drawn(lines, 116)))                      # cells wrapped into more rows

    def test_a_table_that_cannot_reach_the_floors_stands_as_written(self):
        lines = ["| aaaaaaaa | bbbbbbbb | cccccccc | dddddddd |", "|---|---|---|---|", "| 1 | 2 | 3 | 4 |"]
        rows = drawn(lines, 20)
        self.assertEqual(rows[0][-1], "…")
        self.assertEqual(rows[1], "|---|---|---|---|")
        self.assertEqual(rows[2], "| 1 | 2 | 3 | 4 |")
        self.assertTrue(all(cell_width(row) <= 20 for row in rows))
        self.assertIsNone(transcript.table_rows(["a" * 10] * 4, ["left"] * 4, [["x"] * 4], 20))

    def test_rows_short_of_cells_get_empty_cells_and_extra_cells_join_the_last(self):
        rows = drawn(["| a | b | c |", "|---|---|---|", "| 1 |", "| 1 | 2 | 3 | 4 | 5 |"], 60)
        self.assertEqual(rows[2], "1")
        self.assertEqual(rows[3], "1  2  3 | 4 | 5")

    def test_a_pipe_line_without_a_delimiter_row_is_plain_text(self):
        rows = drawn(["| 이 줄은 막대로 시작한다", "입력 | 출력 | 둘 다", "", "다음 문단"], 60)
        self.assertEqual(rows, ["| 이 줄은 막대로 시작한다", "입력 | 출력 | 둘 다", "다음 문단"])
        # a delimiter row of another cell count is not a table either
        rows = drawn(["| a | b |", "|---|", "| 1 | 2 |"], 60)
        self.assertEqual(rows[0], "| a | b |")
        self.assertIn("|---|", rows)

    def test_a_fence_is_as_written_and_a_table_inside_it_is_left_alone(self):
        lines = ["앞", "```", "| a | b |", "|---|---|", "| 1 | 2 | " + "x" * 80, "```", "| c | d |", "|---|---|", "| 3 | 4 |"]
        rows = drawn(lines, 40)
        self.assertEqual(rows[1], "  | a | b |")
        self.assertEqual(rows[2], "  |---|---|")
        self.assertTrue(rows[3].endswith("…"))
        self.assertEqual(cell_width(rows[3]), 40)
        self.assertEqual(rows[4], "c  d")                         # the table after it is a table
        self.assertEqual([k for _t, k in keys(lines, 40)[1]], ["code"])

    def test_an_escaped_bar_and_a_bar_inside_code_stay_in_the_cell(self):
        self.assertEqual(transcript.split_cells("| a\\|b | `x|y` | c |"), ["a|b", "`x|y`", "c"])
        rows = drawn(["| 명령 | 뜻 |", "|---|---|", "| `--select a|b` | 둘 중 하나\\|다른 하나 |"], 60)
        self.assertEqual(rows[2], "--select a|b  둘 중 하나|다른 하나")
        # an unmatched backtick does not swallow the bars after it
        self.assertEqual(transcript.split_cells("| `a | b |"), ["`a", "b"])

    def test_a_whole_line_html_comment_is_not_drawn_and_a_comment_in_code_or_text_is(self):
        doc = ["<!-- hearting:campaign-document v1 -->", "# 제목", "", "본문 <!-- 안 --> 끝", "```", "<!-- 코드 -->", "```"]
        rows = drawn(doc, 60)
        self.assertEqual(rows[0], "제목")
        self.assertNotIn("hearting:campaign-document", "\n".join(rows))
        self.assertIn("본문 <!-- 안 --> 끝", rows)
        self.assertIn("  <!-- 코드 -->", rows)

    def test_a_backslash_escape_outside_code_shows_its_mark_and_inside_code_stays(self):
        rows = drawn(["# a\\<b\\> \\*c\\* \\_d\\_ \\[e\\](f) \\`g\\`", "",
                      "- 항목 \\*\\*굵게 아님\\*\\* 와 `코드 \\< 안`", "", "경로 C:\\dir 와 \\\\ 둘"], 80)
        self.assertEqual(rows[0], "a<b> *c* _d_ [e](f) `g`")
        self.assertEqual(rows[1], "• 항목 **굵게 아님** 와 코드 \\< 안")
        self.assertEqual(rows[2], "경로 C:\\dir 와 \\ 둘")
        marked = keys(["\\*\\*x\\*\\* **y**"], 40)[0]
        self.assertEqual([(t, k) for t, k in marked], [("**x** ", "soft"), ("y", "bold")])
        # a code fence keeps every backslash; a table cell shows the mark, and \| is the cell's bar as before
        self.assertEqual(drawn(["```", "a\\<b", "```"], 40), ["  a\\<b"])
        cells = drawn(["| 명령 | 뜻 |", "|---|---|", "| `a\\<b` | 뜻\\<\\*\\|끝 |"], 60)
        self.assertEqual(cells[2], "a\\<b  뜻<*|끝")

    def test_prose_keeps_long_ids_and_headings_use_the_section_key(self):
        rows = keys(["# 제목", "", "최근 마감: rt-0123456789abcdef 끝", "", "## 다음", "- 하나"], 80)
        self.assertEqual(rows[0], [("제목", "sect")])
        self.assertIn("rt-0123456789abcdef", text(rows[1]))
        self.assertEqual({k for t, k in rows[1] if "rt-" in t}, {"soft"})
        self.assertEqual(rows[2], [])                              # Fleet's card parts a ## from what came before
        self.assertEqual(rows[3], [("다음", "sect")])

    def test_no_row_is_wider_than_the_popup_from_20_to_116(self):
        doc = ["# 현황", "한 줄: 아주 긴 글 " * 8, "- 목록 " + "가나다 " * 20, "      - " + "깊은 목록 " * 6,
               "| 항목 | 상태 | 메모 |", "|:--|:-:|--:|", "| 팝업 틀 | **병합** | v2.1.0 " + "길게 " * 12 + " |",
               "| `python3 tools/run-tests.py --select " + "x" * 50 + "` | 통과 | |", "| 짧음 |",
               "", "| 열 | 열 | 열 | 열 | 열 | 열 |", "|--|--|--|--|--|--|", "| a | b | c | d | e | f |",
               "```", "x" * 200, "```", "[링크](http://example.invalid/" + "a" * 100 + ") 끝"]
        for width in range(20, 117):
            for row in transcript.doc_rows(doc, width):
                self.assertLessEqual(cell_width(text(row)), width, (width, text(row)))


class SafetyTest(unittest.TestCase):
    NASTY = "a\x1b[2Jb\x1b]0;t\x07c\x00d\x08e\x85f\x9bg\u202eh\u2066i\u2069j\u200dk"

    def test_no_control_character_or_direction_override_survives_in_prose_list_heading_fence_or_cell(self):
        nasty = self.NASTY
        lines = ["# " + nasty, nasty, "- " + nasty, "```", nasty, "```", "| h " + nasty + " |", "|--|", "| c " + nasty + " |"]
        for width in (12, 30, 80):
            for line in drawn(lines, width):
                self.assertEqual([c for c in line if unicodedata.category(c) == "Cc" or c in "\u202e\u2066\u2069"], [], line)
                self.assertLessEqual(cell_width(line), width, line)
        joined = "".join(drawn(lines, 80))
        self.assertIn("\u200d", joined)                    # the emoji joiner is kept
        self.assertEqual(transcript.clean("x\ty\nz"), "x\ty\nz")  # tab and newline are not control characters here

    def test_a_long_run_of_open_brackets_is_not_quadratic(self):
        self.assertEqual(transcript._unlink("[a [b](u)"), "[a b")        # the words hold no ``[``
        self.assertEqual(transcript._unlink("see [a](u) and ![b](v)"), "see a and b")
        for line in ("[" * 32000, "[](" * 10000, "[a" * 16000):
            start = time.process_time()
            rows = transcript.doc_rows([line], 80)
            self.assertTrue(rows)
            self.assertLess(time.process_time() - start, 1.5, line[:3])      # generous: it took 2.7 s before

    def test_a_table_indented_four_spaces_is_code_and_stands_as_written(self):
        lines = ["    | a | b |", "    |---|---|", "    | 1 | 2 |"]
        self.assertEqual([line.strip() for line in drawn(lines, 40)], ["| a | b |", "|---|---|", "| 1 | 2 |"])
        for indent in ("", " ", "   "):
            rows = drawn([indent + "| a | b |", indent + "|---|---|", indent + "| 1 | 2 |"], 40)
            self.assertFalse([row for row in rows if "|--" in row], rows)          # up to three spaces: still a table
        self.assertEqual(drawn(["    | a | b |", "|---|---|"], 40)[0].strip(), "| a | b |")


class CardSpansTest(unittest.TestCase):
    def test_card_rows_default_still_leaves_a_long_id_out(self):
        plain = transcript.card_rows(["id rt-0123456789abcdef 끝"], 60)
        kept = transcript.card_rows(["id rt-0123456789abcdef 끝"], 60, spans=transcript.markdown_spans)
        self.assertNotIn("rt-0123456789abcdef", text(plain[0][0]))      # Fleet's cards leave a long id out
        self.assertEqual(text(kept[0][0]), "id rt-0123456789abcdef 끝")


if __name__ == "__main__":
    unittest.main()
