"""The question popup's words wrap at spaces first (11-28), as the MESSAGES and MEM popups' do."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import re
import unittest
from tools.fleet.session_line import wrap_segments, wrap_words
from tools.fleet.explorer.grid import cell_width

KO = "이번 정리 작업을 어디까지 배포할까요? 운영 서버에는 아직 반영하지 않았습니다. 오늘 안에 끝나지만 문제가 생기면 되돌리기 어렵습니다."
EN = "How far should this cleanup go out? Production has not been touched yet, and it is hard to roll back if it breaks."
WIDE = "배포가 끝나면 알림에 🎉🎉 를 붙일까요? 실패하면 ⚠ 표시와 함께 담당자에게 알립니다 🙂🙂🙂"
PATH = "설정은 /home/user/projects/hearting/.agent_reports/campaigns/2026-11-02_v4-open/report.md 에 있습니다."
CJK = "吾輩は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。"


def texts(rows):
    return ["".join(text for text, _key in row) for row in rows]


def cells(text):
    return sum(map(cell_width, text))


class WrapWords(unittest.TestCase):
    def check(self, source, columns):
        """Every rule at once for one plain line: a fold falls on spaces (dropped) unless the word is
        wider than a row, no row is wider than the row, and nothing else of the line is lost."""
        rows = texts(wrap_words([(source, "loc_repo")], columns))
        self.assertTrue(all(cells(row) <= columns for row in rows), (columns, rows))
        self.assertTrue(all(not row[:1].isspace() for row in rows[1:]), (columns, rows))
        self.assertTrue(all(not row[-1:].isspace() for row in rows), (columns, rows))
        # The rows are the line in order, only the spaces at each fold (and its end) left out.
        joined = re.fullmatch("(\\s*)".join(map(re.escape, rows)) + "\\s*", source)
        self.assertIsNotNone(joined, (columns, rows))
        for at, gap in enumerate(joined.groups()):
            if not gap:                               # cut inside a word: only one wider than a row
                start = joined.start(at + 1)
                word = re.search(r"\S+$", source[:start]).group() + re.match(r"\S+", source[start:]).group()
                self.assertGreater(cells(word), columns, (columns, rows))
        return rows

    def test_korean_english_and_wide_letters_fold_between_words(self):
        for source in (KO, EN, WIDE, PATH, CJK):
            for columns in range(4, 90):
                with self.subTest(source=source[:10], columns=columns):
                    self.check(source, columns)

    def test_the_drawn_cases(self):
        # The #1948 drawing's rows: "되돌리기 어렵습니 / 다." and "touc / hed yet." no more.
        self.assertEqual(self.check("오늘 안에 끝나지만 문제가 생기면 되돌리기 어렵습니다.", 47),
                         ["오늘 안에 끝나지만 문제가 생기면 되돌리기", "어렵습니다."])
        self.assertEqual(self.check("How far should this cleanup go out? Production has not been touched yet.", 64),
                         ["How far should this cleanup go out? Production has not been", "touched yet."])
        self.assertEqual(self.check("이번 정리 작업을 어디까지 배포할까요? 운영 서버에는 아직 반영하지 않았습니다.", 56),
                         ["이번 정리 작업을 어디까지 배포할까요? 운영 서버에는 아직", "반영하지 않았습니다."])

    def test_a_word_wider_than_a_row_is_cut_at_cells(self):
        rows = self.check(PATH, 20)
        self.assertEqual(rows[0], "설정은")
        self.assertEqual("".join(rows[1:-1]) + rows[-1].split(" ")[0],
                         "/home/user/projects/hearting/.agent_reports/campaigns/2026-11-02_v4-open/report.md")
        self.assertEqual([cells(row) for row in rows[1:-1]], [20] * (len(rows) - 2))
        self.assertEqual(self.check(CJK, 20), ["吾輩は猫である。名前", "はまだ無い。どこで生", "れたかとんと見当がつ", "かぬ。"])
        # A wide letter that would cross the row's end starts the next row.
        self.assertEqual(self.check("가나다라마", 5), ["가나", "다라", "마"])

    def test_indent_and_inner_spaces_stay_where_nothing_folds(self):
        source = "    순서:  백업을   만든다    (약 5분)"
        self.assertEqual(texts(wrap_words([(source, "k")], 80)), [source])
        self.assertEqual(self.check(source, 16), ["    순서:", "백업을   만든다", "(약 5분)"])
        self.assertEqual(self.check("  - fall back to the backup", 12), ["  - fall", "back to the", "backup"])

    def test_each_piece_keeps_its_key(self):
        label = [("스테이징만 먼저", "loc_repo"), (" (recommended)", "dim")]
        self.assertEqual(wrap_words(label, 30), [[("스테이징만 먼저", "loc_repo"), (" (recommended)", "dim")]])
        # Not on the name's row: the mark goes to the next row whole, in its own key.
        self.assertEqual(wrap_words(label, 20), [[("스테이징만 먼저", "loc_repo")], [("(recommended)", "dim")]])
        self.assertEqual(wrap_words([("Staging and", "loc_repo"), (" production", "dim"), (" first", "loc_repo")], 17),
                         [[("Staging and", "loc_repo")], [("production", "dim"), (" first", "loc_repo")]])
        # A word made of two keys is one word.
        self.assertEqual(wrap_words([("ab ", "a"), ("cd", "a"), ("ef", "b")], 4), [[("ab", "a")], [("cd", "a"), ("ef", "b")]])

    def test_controls_and_spaces_only_draw_as_before(self):
        self.assertEqual(texts(wrap_words([("one\ttwo\x7fthree", "k")], 8)), ["one two", "three"])
        for blank in ("", "   ", "\t"):
            self.assertEqual(wrap_words([(blank, "k")], 10), wrap_segments([(blank, "k")], 10))

    def test_wrap_segments_still_cuts_at_cells(self):
        # Fleet's status rows and route text keep main's rule.
        self.assertEqual(texts(wrap_segments([("How far should this go", "k")], 6)), ["How fa", "r shou", "ld thi", "s go"])
        self.assertEqual(texts(wrap_segments([("되돌리기 어렵습니다.", "k")], 10)), ["되돌리기 ", "어렵습니다", "."])


class Popup(unittest.TestCase):
    def present(self, question, columns=64, brief_columns=None):
        from tools.fleet.session_block import question_presentations
        item = {"id": "q", "answerable": True, "age": "0m", "request_state": "open", "questions": [question]}
        question_presentations([item], columns, brief_columns=brief_columns)
        return item["questions"][0]["display"]

    def test_question_choice_names_and_their_words_fold_between_words(self):
        question = {"id": "q", "question": KO + "\n" + EN, "recommended": 0,
                    "choices": ["스테이징만 먼저 반영하고 하루 동안 지켜보기", "Staging and production together today"],
                    "descriptions": ["운영 반영 전에 하루 동안 오류 기록을 지켜봅니다. " + WIDE, EN]}
        for columns in (24, 40, 64, 76, 136):
            with self.subTest(columns=columns):
                display = self.present(question, columns)
                width = columns - 4
                body = texts(display["body"])
                self.assertTrue(all(cells(row) <= width and not row[:1].isspace() for row in body), body)
                self.assertEqual(" ".join(body), KO + " " + EN)
                options = texts(display["options"])
                self.assertTrue(all(cells(row) <= width - 2 for row in options), options)
                words = [row[3:] for row in options]   # after the number column
                self.assertTrue(all(not row[:1].isspace() for row in words), options)
                joined = " ".join(words)
                for whole in ("스테이징만 먼저 반영하고 하루 동안 지켜보기 (recommended)", "Staging and production together today",
                              "운영 반영 전에 하루 동안 오류 기록을 지켜봅니다. " + WIDE, EN):
                    self.assertIn(whole, joined)
                self.assertEqual({key for row in display["options"] for text, key in row if "recommended" in text}, {"dim"})

    def test_a_table_and_the_panel_summary_draw_as_on_main(self):
        from tools.fleet.explorer.transcript import table_parts
        from tools.fleet.session_block import _brief
        table = "어느 쪽으로 할까요?\n| 안 | 시간 | 위험 |\n|---|---|---|\n| 스테이징 먼저 | 하루 | 낮음 |\n| 운영과 함께 | 오늘 | 높음 |"
        display = self.present({"id": "q", "question": table, "choices": ["먼저", "함께"]}, 40, brief_columns=30)
        drawn = [row for line in table_parts(table.splitlines(), 36) if isinstance(line, list) for row in line]
        self.assertTrue(drawn)
        self.assertEqual(display["body"][1:], drawn)
        # A question without choices: the panel's two-row summary keeps main's cell wrap (``_brief``).
        text = "How far should this cleanup go out? Production has not been touched yet."
        brief = _brief({"question": text}, [], "open", "g_blocked", 28)["brief"]
        cut = wrap_segments([(text, "loc_repo")], 28)
        self.assertEqual(brief[0], [("◑ ", "g_blocked"), *cut[0]])


if __name__ == "__main__":
    unittest.main()
