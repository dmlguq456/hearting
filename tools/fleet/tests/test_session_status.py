"""The shell panel's status value and popup lines (``tools/fleet/session_status.py``).

Fixtures are made-up campaign folders and route records in a ``TemporaryDirectory``. The route ->
campaign resolver (``route_autoclose.campaign_of_route``, tested on its own) is stood in for by a
table of key -> folder, so what is checked here is the choice among documents and the lines drawn.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
from pathlib import Path
import tempfile
import unicodedata
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "utilities"))

from tools.fleet import session_status  # noqa: E402
from tools.fleet.collectors import project_status  # noqa: E402
from tools.fleet.explorer.grid import cell_width  # noqa: E402
import route_autoclose  # noqa: E402

NOW = 1_763_192_400.0
DOC = "# {name} 현황\n\n갱신 2026-11-15 14:40\n\n**한 줄**: {name} 작업 중.\n\n## 결정 필요\n- 하나\n\n## 다음\n- 둘\n"
TABLE = "\n## 판\n\n| 항목 | 상태 |\n|:--|--:|\n| 팝업 | 병합 |\n"


def text(row):
    return "".join(part for part, _key in row)


def rid(n):
    return "rt-%016x" % n


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.lang = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Korean"})
        self.lang.start()
        self.addCleanup(self.lang.stop)
        self._tmp = tempfile.TemporaryDirectory(prefix="ss")
        self.addCleanup(self._tmp.cleanup)
        self.cwd = Path(self._tmp.name).resolve()
        self.root = self.cwd / ".agent_reports"
        (self.root / "campaigns").mkdir(parents=True)
        (self.root / ".runtime" / "routes").mkdir(parents=True)
        self.folders = {}               # campaign key -> its folder (what the resolver finds)
        patch = mock.patch.object(route_autoclose, "campaign_of_route", self.resolve)
        patch.start()
        self.addCleanup(patch.stop)
        session_status.clear_cache()
        project_status.clear_cache()
        self.addCleanup(session_status.clear_cache)
        self.addCleanup(project_status.clear_cache)

    def resolve(self, root, route):
        folder = self.folders.get(route.get("campaign_key"))
        return ("cmp-" + str(route.get("campaign_key")), folder) if folder else None

    def campaign(self, key, doc=True, age=3600, name=None, body=None, closed=False):
        folder = self.root / "campaigns" / ("2026-11-10_" + key)
        folder.mkdir(parents=True)
        if not closed:
            self.folders[key] = folder
        if doc:
            path = folder / (name or "현황.md")
            path.write_text(DOC.format(name=key) if body is None else body, encoding="utf-8")
            os.utime(path, (NOW - age, NOW - age))
        os.utime(folder, (NOW - age, NOW - age))
        return folder

    def route(self, n, key):
        record = {"route_id": rid(n)}
        if key:
            record["campaign_key"] = key
        (self.root / ".runtime" / "routes" / (rid(n) + ".json")).write_text(json.dumps(record), encoding="utf-8")
        return rid(n)

    def read(self, *routes):
        return session_status.read(self.cwd, list(routes), now=NOW)


class ChoiceTest(StatusTest):
    def test_no_artifact_root_gives_no_value(self):
        with tempfile.TemporaryDirectory(prefix="ss") as other:
            self.assertIsNone(session_status.read(other, [], now=NOW))
        self.assertIsNone(session_status.read("", [], now=NOW))

    def test_the_sessions_campaign_document_beats_the_one_fleet_picks(self):
        self.campaign("alpha", age=3 * 3600)
        self.campaign("beta", age=600)                     # newer: the document Fleet's project line picks
        mine = self.route(1, "alpha")
        view = self.read(mine)
        self.assertTrue(view["has_doc"])
        self.assertEqual((view["campaign"], view["age"], view["word"]), ("alpha", "3h ago", "STATUS"))   # English whatever the declared language
        self.assertEqual(view["title"], "STATUS alpha · 3h ago")
        self.assertTrue(view["path"].endswith("2026-11-10_alpha/현황.md"))
        self.assertEqual(self.read()["campaign"], "beta")                       # no route: Fleet's document
        self.assertEqual(self.read()["title"], "STATUS beta · 10m ago")

    def test_the_panel_opens_campaign_md_before_the_older_names(self):
        folder = self.campaign("alpha", age=3 * 3600)          # carries a hand written 현황.md
        page = folder / "campaign.md"
        page.write_text("<!-- hearting:campaign-document v1 -->\n" + DOC.format(name="alpha"), encoding="utf-8")
        os.utime(page, (NOW - 7200, NOW - 7200))
        view = self.read(self.route(1, "alpha"))
        self.assertEqual((view["campaign"], view["has_doc"]), ("alpha", True))
        self.assertTrue(view["path"].endswith("2026-11-10_alpha/campaign.md"))
        page.write_text("# Large\n" + "x" * 70000, encoding="utf-8")       # too large: no older name stands in
        os.utime(page, (NOW - 7200, NOW - 7200))
        self.assertTrue(self.read(self.route(1, "alpha"))["path"].endswith("2026-11-10_alpha/campaign.md"))

    def test_routes_in_several_campaigns_show_the_most_recently_changed_document(self):
        self.campaign("alpha", age=7200)
        self.campaign("gamma", age=1200)
        self.campaign("beta", age=60)
        view = self.read(self.route(1, "alpha"), self.route(2, "gamma"))
        self.assertEqual(view["campaign"], "gamma")
        self.assertEqual(self.read(self.route(2, "gamma"), self.route(1, "alpha"))["campaign"], "gamma")
        self.assertEqual(len([w for w in view["watch"] if w.endswith(os.sep)]), 2)   # both folders are watched

    def test_a_session_that_does_not_know_its_campaign_shows_fleets_document(self):
        self.campaign("alpha", age=7200)
        self.campaign("beta", age=60)
        for routes in ([], [self.route(1, None)], ["rt-0000000000000000", "../x", ""]):
            self.assertEqual(self.read(*routes)["campaign"], "beta")

    def test_a_closed_campaign_is_not_known_so_fleets_document_shows(self):
        self.campaign("old", age=60, closed=True)          # the resolver gives only open campaigns
        self.campaign("beta", age=7200)
        self.assertEqual(self.read(self.route(1, "old"))["campaign"], "old")      # Fleet's own pick is this one
        self.campaign("newer", age=10)
        session_status.clear_cache()
        project_status.clear_cache()
        self.assertEqual(self.read(self.route(1, "old"))["campaign"], "newer")

    def test_a_campaign_without_a_document_falls_to_fleets_and_without_any_has_a_place(self):
        self.campaign("alpha", doc=False)
        self.campaign("beta", age=7200)
        mine = self.route(1, "alpha")
        view = self.read(mine)
        self.assertEqual((view["has_doc"], view["campaign"]), (True, "beta"))
        for folder in (self.root / "campaigns" / "2026-11-10_beta",):
            (folder / "현황.md").unlink()
        session_status.clear_cache()
        project_status.clear_cache()
        view = self.read(mine)
        self.assertFalse(view["has_doc"])
        self.assertEqual(view["title"], "STATUS")
        self.assertEqual((view["campaign"], view["path"]), ("alpha", ""))
        self.assertEqual(view["slot"], "campaigns/2026-11-10_alpha/현황.md")
        self.assertIn(str(self.root / "campaigns" / "2026-11-10_alpha") + os.sep, view["watch"])
        self.assertEqual(self.read()["slot"], "")           # a campaign not known: no place to say

    def test_the_value_is_the_roots_existence_not_a_document(self):
        view = self.read()
        self.assertFalse(view["has_doc"])
        self.assertEqual(view["slot"], "")
        self.assertEqual([os.path.basename(w) for w in view["watch"]], ["backlog.md"])      # (11-21: only the backlog, which the panel's BACKLOG part reads)

    def test_english_screen_word(self):
        self.campaign("alpha")
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"}):
            self.assertEqual(self.read(self.route(1, "alpha"))["title"], "STATUS alpha · 1h ago")


    def test_a_route_record_that_is_a_link_out_of_the_root_picks_no_campaign(self):
        self.campaign("alpha", age=3 * 3600)
        self.campaign("beta", age=600)
        record = {"route_id": rid(1), "campaign_key": "alpha"}
        with tempfile.TemporaryDirectory(prefix="ss") as outside:
            elsewhere = Path(outside) / "rec.json"
            elsewhere.write_text(json.dumps(record), encoding="utf-8")
            (self.root / ".runtime" / "routes" / (rid(1) + ".json")).symlink_to(elsewhere)
            self.assertEqual(self.read(rid(1))["campaign"], "beta")         # Fleet's document, not alpha's
            # the same record, kept in a folder that is itself a link out of the root
            (self.root / ".runtime" / "routes" / (rid(1) + ".json")).unlink()
            (self.root / ".runtime" / "routes").rmdir()
            (Path(outside) / (rid(1) + ".json")).write_text(json.dumps(record), encoding="utf-8")
            (self.root / ".runtime" / "routes").symlink_to(outside)
            session_status.clear_cache()
            project_status.clear_cache()
            self.assertEqual(self.read(rid(1))["campaign"], "beta")

    def test_an_oversize_route_record_picks_no_campaign(self):
        self.campaign("alpha", age=3 * 3600)
        self.campaign("beta", age=600)
        record = {"route_id": rid(1), "campaign_key": "alpha", "pad": "x" * 70000}
        (self.root / ".runtime" / "routes" / (rid(1) + ".json")).write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self.read(rid(1))["campaign"], "beta")


NASTY = ("\x1b[2J \x1b]0;title\x07 \x1b]52;c;QUJD\x07 nul\x00 bel\x07 bs\x08 next\x85 csi\x9b31m "
         "rlo\u202e flip\u2066 end\u2069 fam\U0001F468\u200d\U0001F469")


def unsafe(line):
    return [char for char in line if unicodedata.category(char) == "Cc" or char in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"]


class PopupTest(StatusTest):
    def status(self):
        return {"campaigns": [{"campaign": "alpha", "line": "알파 작업 중.", "doc_age": "1h ago"}], "next": "둘"}

    def test_top_lines_then_an_empty_row_then_the_whole_document(self):
        self.campaign("alpha", body=DOC.format(name="alpha") + TABLE + "\n최근 마감: rt-0123456789abcdef 끝\n")
        rows = session_status.popup_rows(self.read(self.route(1, "alpha")), self.status(), 60)
        lines = [text(row) for row in rows]
        self.assertEqual(lines[0].strip(), "alpha: 알파 작업 중.")
        self.assertTrue(lines[1].strip().startswith("next:") and lines[1].rstrip().endswith("둘"))
        self.assertEqual(lines[2], "")
        self.assertEqual(lines[3], "alpha 현황")
        self.assertIn("항목  상태", lines)                 # a table, aligned: no `|---|`
        self.assertFalse([line for line in lines if "|--" in line])
        self.assertTrue([line for line in lines if "rt-0123456789abcdef" in line])  # a long id is kept
        self.assertTrue(all(cell_width(line) <= 60 for line in lines))

    def test_a_generated_campaign_document_opens_from_the_button_and_the_popup_draws_all_of_it(self):
        import campaign_document
        from datetime import timezone
        unit = {"key": "pr:7", "pr": 7, "v": "v4.1.0", "title": "무언가를 고침", "impl": {"slug": "x"}, "count": 3,
                "closed_at": NOW - 3600, "routes": 2, "activity": NOW}
        waits = {"status": "ok", "at": NOW, "items": ["첫째?", "둘째?"]}
        doc = campaign_document.render_document(
            "alpha 캠페인", "알파 작업 중.", {"active": [unit], "active_folded": 0, "done": [unit],
                                         "done_folded": 12}, waits, now=NOW, tz=timezone.utc)
        folder = self.campaign("alpha", doc=False)
        page = folder / "campaign.md"
        page.write_text(doc, encoding="utf-8")
        os.utime(page, (NOW - 600, NOW - 600))
        view = self.read(self.route(1, "alpha"))
        self.assertTrue(view["has_doc"])
        self.assertTrue(view["path"].endswith("2026-11-10_alpha/campaign.md"))
        lines = [text(row) for row in session_status.popup_rows(view, self.status(), 60)]
        shown = "\n".join(lines)
        for part in ("하는 중", "기다리는 것", "최근에 끝난 것", "무언가를 고침", "첫째?", "앞서 끝난 기록 12개는 접어 둠."):
            self.assertIn(part, shown)
        self.assertEqual(len(project_status.parse(doc)["decisions"]), 2)     # Fleet's decision count = open questions
        self.assertTrue(all(cell_width(line) <= 60 for line in lines))

    def test_the_top_lines_without_a_campaign_document_are_the_four_line_block(self):
        self.campaign("alpha")
        status = {"routes": 2, "questions": 1, "done": None, "changed": "3h"}
        lines = [text(row) for row in session_status.popup_rows(self.read(self.route(1, "alpha")), status, 80)]
        self.assertEqual([line.split()[0] for line in lines[:4]], ["status:", "decide:", "next:", "changed:"])
        self.assertEqual(lines[4], "")

    def test_no_document_says_so_with_the_place_and_nothing_else(self):
        self.campaign("alpha", doc=False)
        view = self.read(self.route(1, "alpha"))
        lines = [text(row) for row in session_status.popup_rows(view, {"routes": 1, "changed": "3h"}, 80)]
        self.assertEqual(lines[-1], "현황 문서 없음. · campaigns/2026-11-10_alpha/현황.md")
        self.assertEqual(lines[-2], "")
        lone = [text(row) for row in session_status.popup_rows(self.read(), None, 80)]
        self.assertEqual(lone, ["현황 문서 없음."])           # no campaign known: no place; Fleet's word once

    def test_a_document_over_64_kib_is_read_to_64_kib_and_its_torn_line_dropped(self):
        body = "# 큰 문서\n" + "".join("줄 %05d 가나다라마바사\n" % n for n in range(6000))
        self.assertGreater(len(body.encode("utf-8")), session_status.READ_LIMIT)
        self.campaign("alpha", body=body)
        view = self.read(self.route(1, "alpha"))
        rows = session_status.popup_rows(view, self.status(), 100)
        lines = [text(row) for row in rows]
        self.assertEqual(lines[-1], "… 64 KiB")
        self.assertEqual(rows[-1], [("… 64 KiB", "dim")])
        last = lines[-2]
        self.assertRegex(last, r"^줄 \d{5} 가나다라마바사$")                  # a whole line, not a torn one
        self.assertLess(int(last.split()[1]), 5999)
        kept, cut = session_status.read_text(view["path"])
        self.assertTrue(cut)
        self.assertLessEqual(len("\n".join(kept).encode("utf-8")), session_status.READ_LIMIT)

    def test_a_small_document_has_no_cut_line(self):
        self.campaign("alpha")
        rows = session_status.popup_rows(self.read(self.route(1, "alpha")), self.status(), 60)
        self.assertNotIn("… 64 KiB", [text(row) for row in rows])

    def test_no_control_character_or_direction_override_reaches_any_row(self):
        body = ("# 제목 " + NASTY + "\n\n" + NASTY + " 산문\n- 목록 " + NASTY + "\n\n```\n코드 " + NASTY + "\n```\n\n"
                "| 항목 " + NASTY + " | 상태 |\n|:--|--:|\n| 칸 " + NASTY + " | b |\n")
        self.campaign("alpha", body=body)
        view = self.read(self.route(1, "alpha"))
        status = {"campaigns": [{"campaign": "al" + NASTY, "line": "줄 " + NASTY, "doc_age": "1h ago"}], "next": "다음 " + NASTY}
        for width in (20, 40, 80):
            rows = session_status.popup_rows(view, status, width)
            self.assertTrue([line for line in map(text, rows) if "제목" in line or "칸" in line])
            for row in rows:
                self.assertEqual(unsafe(text(row)), [], (width, text(row)))
                self.assertLessEqual(cell_width(text(row)), width, (width, text(row)))
        self.assertIn("\u200d", "".join(text(row) for row in session_status.popup_rows(view, status, 80)))  # the emoji joiner stays
        # and the place a missing document would stand
        self.campaign("gamma", doc=False)
        slotted = dict(self.read(self.route(2, "gamma")), slot="campaigns/g" + NASTY + "/현황.md")
        for row in session_status.popup_rows(dict(slotted, has_doc=False, path=""), status, 60):
            self.assertEqual(unsafe(text(row)), [])
            self.assertLessEqual(cell_width(text(row)), 60)

    def test_a_document_Fleet_does_not_know_has_no_no_document_line_above_it(self):
        body = "# 큰 문서\n" + "".join("줄 %05d 가나다라마바사\n" % n for n in range(6000))
        self.campaign("alpha", body=body)                     # over 64 KiB: Fleet's project line does not list it
        view = self.read(self.route(1, "alpha"))
        self.assertTrue(view["has_doc"])
        for status in ({}, None):
            lines = [text(row) for row in session_status.popup_rows(view, status, 80)]
            self.assertNotIn("현황 문서 없음.", [line.strip() for line in lines])
            self.assertEqual(lines[0], "큰 문서")
        # Fleet's own page still says it when it holds nothing
        from tools.fleet.explorer import pages
        from tools.fleet.explorer.grid import CellGrid
        grid = CellGrid(60, 4)
        pages.repo_status_lines(grid, 0, 60, 0, {})
        self.assertIn("현황 문서 없음.", "".join(char for char, _key, _tint in grid.cells[0]))

    def test_widths_from_20_to_116_never_overflow(self):
        self.campaign("alpha", body=DOC.format(name="alpha") + TABLE + "```\n" + "x" * 300 + "\n```\n")
        view = self.read(self.route(1, "alpha"))
        for width in range(20, 117):
            for row in session_status.popup_rows(view, self.status(), width):
                self.assertLessEqual(cell_width(text(row)), width, (width, text(row)))


# A campaign document in the shape the campaign-document design gives it (fixture titles only).
CAMPAIGN = """<!-- hearting:campaign-document v1 -->
# 시안용 캠페인

갱신 2026-11-20 10:00

**한 줄**: 시험용 문서.

## 결정 기다리는 것
- 질문판에 열린 질문 2개
- 시안 B로 갈지 정하기

## 하는 중
- 시험용 작업 가 · 기록 2
- 시험용 작업 나 · #1801
- (하루 넘게 움직임 없는 열린 route 4개는 접어 둠)

## 끝난 일
- 11-19 시험용 끝난 일 하나 · #1790
- 11-18 시험용 끝난 일 둘 · #1788 · v4.60.1

## 예정
- 시험용 예정 하나
"""


class GroupsTest(StatusTest):
    """11-20: the document's lists (``parse_groups`` / ``read_groups``) are kept for the popup's coming list; the panel's value does not read them."""

    def kinds(self, text_):
        return [(g["kind"], g["count"], g["items"]) for g in session_status.parse_groups(text_)]

    def test_the_four_lists_of_a_campaign_document_in_the_panels_order(self):
        groups = session_status.parse_groups(CAMPAIGN)
        self.assertEqual([g["kind"] for g in groups], ["running", "waiting", "done", "next"])
        self.assertEqual([g["name"] for g in groups], ["하는 중", "기다리는 것", "최근에 끝난 것", "나중에 할 것"])
        self.assertEqual([g["count"] for g in groups], [2, 2, 2, 1])
        # The note in parentheses is no title; a title keeps what the document wrote.
        self.assertEqual(groups[0]["items"], ["시험용 작업 가 · 기록 2", "시험용 작업 나 · #1801"])
        self.assertEqual(groups[2]["items"][1], "11-18 시험용 끝난 일 둘 · #1788 · v4.60.1")

    def test_the_wait_list_with_nothing_under_it_is_a_count_of_zero_and_other_empty_lists_are_left_out(self):
        text_ = "# t\n\n## 결정 기다리는 것\n지금 없음\n\n## 하는 중\n\n## 끝난 일\n- 하나\n"
        self.assertEqual(self.kinds(text_), [("waiting", 0, []), ("done", 1, ["하나"])])

    def test_both_heading_name_sets_of_the_campaign_document_read_as_the_same_four_groups(self):
        """The document's maker has not settled the names yet: each name of a pair reads as the same group, whatever the order in the file."""
        old = ("# t\n\n## 결정 기다리는 것\n- 질문 하나\n\n## 하는 중\n- 일 하나\n\n## 끝난 일\n- 11-19 끝남 하나 · #1790\n\n## 예정\n- 나중 하나\n")
        new = ("# t\n\n## 하는 중\n- 일 하나\n\n## 기다리는 것\n- 질문 하나\n\n## 최근에 끝난 것\n- 11-19 끝남 하나 · #1790\n\n## 나중에 할 것\n- 나중 하나\n")
        shuffled = ("# t\n\n## 나중에 할 것\n- 나중 하나\n\n## 최근에 끝난 것\n- 11-19 끝남 하나 · #1790\n\n## 기다리는 것\n- 질문 하나\n\n## 하는 중\n- 일 하나\n")
        want = [("running", 1, ["일 하나"]), ("waiting", 1, ["질문 하나"]), ("done", 1, ["11-19 끝남 하나 · #1790"]), ("next", 1, ["나중 하나"])]
        for text_ in (old, new, shuffled):
            self.assertEqual(self.kinds(text_), want, text_)
        # A line that is no bullet is a note, not a job; a lone `예정` missing is no group.
        notes = "# t\n\n## 기다리는 것\n질문판 기준 10:00\n지금 없음.\n\n## 하는 중\n- 일 하나\n6시간 넘게 움직임 없는 열린 일 32개는 접어 둠.\n"
        self.assertEqual(self.kinds(notes), [("running", 1, ["일 하나"]), ("waiting", 0, [])])

    def test_a_hand_written_page_gives_what_it_can_and_a_page_without_lists_gives_none(self):
        self.assertEqual(self.kinds(DOC.format(name="x")), [("waiting", 1, ["하나"]), ("next", 1, ["둘"])])
        self.assertEqual(session_status.parse_groups("# 제목\n\n그냥 글\n\n## 판\n- 항목\n"), [])
        self.assertEqual(session_status.parse_groups(""), [])
        # A record of what was decided is no list of work; nested bullets are no title of their own.
        self.assertEqual(self.kinds("# t\n\n## 결정 기록\n- 지난 결정\n\n## 하는 중\n- 하나\n  - 안쪽\n"), [("running", 1, ["하나"])])

    def test_titles_are_capped_and_terminal_safe(self):
        body = "# t\n\n## 끝난 일\n" + "".join("- 줄 %d\n" % n for n in range(40)) + "- 긴 줄 " + "가" * 500 + "\n"
        done = session_status.parse_groups(body)[0]
        self.assertEqual((done["count"], len(done["items"])), (41, session_status.GROUP_ITEMS))
        longest = session_status.parse_groups("# t\n\n## 끝난 일\n- 긴 줄 " + "가" * 500 + "\x1b[31m\n")[0]["items"][0]
        self.assertLessEqual(len(longest), session_status.GROUP_TITLE)
        self.assertNotIn("\x1b", longest)

    def test_a_heading_is_read_by_how_it_begins_and_a_code_fence_is_no_list(self):
        """Hand-written pages (the review's counter-examples): a word inside a heading names no list, and a fence holds no heading and no job."""
        for heading in ("Definition of Done", "Not done yet", "Context: what we are doing", "How to do the release", "CI 기다리는 PR", "Where to look next",
                        "Backlog (to do later)", "Work done so far"):
            self.assertEqual(self.kinds("# t\n\n## %s\n- 하나\n- 둘\n" % heading), [], heading)
        # The words the near spellings begin with still read, in either case, with a word after them.
        for heading, kind in (("Done", "done"), ("Next steps", "next"), ("To do", "next"), ("TODO", "next"), ("In progress", "running"),
                              ("Doing now", "running"), ("Waiting on you", "waiting"), ("Decisions needed", "waiting"), ("다음에 할 일", "next")):
            self.assertEqual(self.kinds("# t\n\n## %s\n- 하나\n" % heading), [(kind, 1, ["하나"])], heading)
        # A number or a picture before the words is not part of the name (closure review 3), and the counter-examples stay counter-examples behind them.
        for heading, kind in (("1. 하는 중", "running"), ("2) 결정 필요", "waiting"), ("⏳ 하는 중", "running"), ("🔴 결정 필요", "waiting"),
                              ("3. ✅ 끝난 일", "done"), ("[예정]", "next"), ("4. Next steps", "next"), ("🔴 In progress", "running")):
            self.assertEqual(self.kinds("# t\n\n## %s\n- 하나\n" % heading), [(kind, 1, ["하나"])], heading)
        for heading in ("1. Definition of Done", "⏳ Not done yet", "2) Context: what we are doing", "🔴 How to do the release", "3. CI 기다리는 PR"):
            self.assertEqual(self.kinds("# t\n\n## %s\n- 하나\n- 둘\n" % heading), [], heading)
        # A line in a code fence is no job, and a heading in one starts nothing.
        fenced = "# t\n\n## 하는 중\n- 일 하나\n```\n- code line\n## 끝난 일\n- not a job\n```\n- 일 둘\n"
        self.assertEqual(self.kinds(fenced), [("running", 2, ["일 하나", "일 둘"])])

    def test_the_comment_marker_line_belongs_to_no_group(self):
        """The campaign document's first line is an HTML comment marker; it is no heading and no job, whatever follows it."""
        doc = "<!-- hearting:campaign-document v1 -->\n# t\n\n## 하는 중\n- 일 하나\n"
        groups = session_status.parse_groups(doc)
        self.assertEqual([(g["kind"], g["count"], g["items"]) for g in groups], [("running", 1, ["일 하나"])])

    def test_the_value_does_not_read_the_document_and_carries_no_groups(self):
        """11-20 (the panel has the button alone): the value is the document's place, title and age; the panel's refresh never opens the document."""
        folder2 = self.campaign("beta", body=CAMPAIGN)
        with mock.patch.object(session_status, "read_text", side_effect=AssertionError("the value read the document")), \
                mock.patch.object(session_status, "read_groups", side_effect=AssertionError("the value read the groups")):
            view = self.read(self.route(2, "beta"))
        self.assertTrue(view["has_doc"])
        self.assertNotIn("groups", view)
        self.assertTrue(view["path"].endswith("현황.md"), view["path"])
        # A changed document shows at the next read (its age), still without being opened.
        path = folder2 / "현황.md"
        os.utime(path, (NOW - 10, NOW - 10))
        with mock.patch.object(session_status, "read_text", side_effect=AssertionError("the value read the document")):
            again = self.read(self.route(2, "beta"))
        self.assertNotEqual(again["age"], "")
        self.assertNotIn("groups", again)

    def test_a_campaign_md_document_is_read_by_name_and_read_groups_gives_its_lists(self):
        self.campaign("alpha", body=CAMPAIGN, name="campaign.md")
        view = self.read(self.route(1, "alpha"))
        self.assertTrue(view["has_doc"])
        self.assertTrue(view["path"].endswith("2026-11-10_alpha/campaign.md"), view["path"])
        self.assertEqual([g["kind"] for g in session_status.read_groups(view["path"])], ["running", "waiting", "done", "next"])

    def test_read_groups_gives_none_for_a_document_that_cannot_be_read(self):
        self.assertEqual(session_status.read_groups(str(self.root / "nowhere.md")), [])
        self.campaign("alpha", doc=False)
        self.assertNotIn("groups", self.read(self.route(1, "alpha")))


# -- 11-21: the popup's list screen (design C: what waits for the person first) ---------------------------------------------

import campaign_document  # noqa: E402

GENERATED = campaign_document.MARKER + """
# 시험용 캠페인

갱신 2025-11-15 11:02

**한 줄**: 시험용 한 줄.

## 하는 중
- 일 하나 · #11 · 기록 1
- 일 둘 · #12 · 기록 2
- 일 셋 · 기록 1
- 일 넷 · 기록 1
- 일 다섯 · 기록 1
6시간 넘게 움직임 없는 열린 일 23개는 접어 둠.

## 기다리는 것
- 결정: 첫 질문의 첫 줄
- 둘째 질문?
질문판 기준 11:01

## 최근에 끝난 것
- 11-21 끝난 일 · #1 · 기록 6
앞서 끝난 기록 1,392개는 접어 둠.
"""


def shown(entries, key=None):
    """The first row of each entry as text (the rows of the ones with ``key`` set, if asked)."""
    return [text(entry["rows"][0]) for entry in entries if key is None or entry.get("key")]


class ListTest(StatusTest):
    def setUp(self):
        super().setUp()
        english = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})     # the words the screen puts in are English
        english.start()
        self.addCleanup(english.stop)

    def page(self, body=GENERATED, age=600, name="campaign.md"):
        self.pages = getattr(self, "pages", 0) + 1
        key = "page%d" % self.pages
        self.campaign(key, doc=False)
        path = self.root / "campaigns" / ("2026-11-10_" + key) / name
        path.write_text(body, encoding="utf-8")
        os.utime(path, (NOW - age, NOW - age))
        return self.read(self.route(100 + self.pages, key))

    def listing(self, body=GENERATED, width=92, age=600, name="campaign.md"):
        view = self.page(body, age, name)
        rows, listing = session_status.popup_doc(view, width, NOW, want_list=True)
        self.assertIsNotNone(rows)
        return listing, view

    @staticmethod
    def gid(name, nth=1):
        """The id of the ``nth`` group that says ``name``: a digest of the heading (never the heading), ``-N`` after the first."""
        return session_status._digest(name) + ("" if nth == 1 else "-%d" % nth)

    def test_the_marker_is_the_documents_own(self):
        self.assertEqual(session_status.CAMPAIGN_MARKER, campaign_document.MARKER)

    def test_the_screen_is_the_head_then_what_waits_then_what_runs_then_the_folded_headings_then_the_full_document(self):
        entries, _ = self.listing()
        lines = shown(entries)
        self.assertEqual(lines[:3], ["시험용 캠페인", "updated 11-15 11:02", "시험용 한 줄."])          # (how long ago is the title band's)
        self.assertEqual(lines[3], "")
        # The document has `하는 중` first; the screen puts `기다리는 것` before it (every bullet, each marked `!`).
        self.assertEqual(lines[4:8], ["기다리는 것 2", "  ! 결정: 첫 질문의 첫 줄", "  ! 둘째 질문?", "    질문판 기준 11:01"])
        self.assertEqual(lines[8], "")
        self.assertEqual(lines[9:13], ["하는 중 5", "  일 하나 · #11 · 기록 1", "  일 둘 · #12 · 기록 2", "  일 셋 · 기록 1"])
        self.assertEqual(lines[13:17], ["  일 넷 · 기록 1", "  일 다섯 · 기록 1", "  all 5 ›", "    6시간 넘게 움직임 없는 열린 일 23개는 접어 둠."])
        self.assertEqual(lines[17:], ["", "  › 최근에 끝난 것 1", "    11-21 끝난 일 · #1 · 기록 6", "    앞서 끝난 기록 1,392개는 접어 둠.", "",
                                      "  full document ›"])

    def test_which_entries_can_be_chosen_and_when_they_show(self):
        entries, _ = self.listing()
        kinds = [entry["key"].split(":")[0] for entry in entries if entry.get("key")]
        self.assertEqual(kinds, ["i", "i", "i", "i", "i", "i", "i", "a", "g", "i", "full"])
        by_key = {entry["key"]: entry for entry in entries if entry.get("key")}
        self.assertFalse([key for key in by_key if "\n" in key or len(key) > 40])      # a key is short and one line, whatever the words are
        self.assertFalse([key for key in by_key if "하는" in key or "끝난" in key])      # (and is no part of the words)
        run = next(entry["group"] for entry in entries if entry.get("key", "").startswith("a:"))
        self.assertEqual(run, self.gid("하는 중"))
        running = [entry for entry in entries if entry.get("key", "").startswith("i:" + run)]
        self.assertEqual([entry.get("when") for entry in running], [None, None, None, "open", "open"])   # three, then the rest on `all N ›`
        self.assertEqual(text(by_key["a:" + run]["rows"][0]), "  all 5 ›")
        self.assertEqual(text(by_key["a:" + run]["alt"][0]), "  all 5 ▾")
        head = next(entry for entry in entries if entry.get("key", "").startswith("g:"))
        self.assertEqual((text(head["rows"][0]), text(head["alt"][0])), ("  › 최근에 끝난 것 1", "  ▾ 최근에 끝난 것 1"))
        done = [entry for entry in entries if entry.get("group") == head["group"] and not entry.get("key", "g:").startswith("g:")]
        self.assertTrue(done and all(entry.get("when") == "open" for entry in done))     # the folded group's items and notes: open only

    def test_a_running_list_of_three_or_fewer_has_no_all_row(self):
        entries, _ = self.listing(GENERATED.replace("- 일 넷 · 기록 1\n- 일 다섯 · 기록 1\n", ""))
        self.assertFalse([entry for entry in entries if entry.get("key", "").startswith("a:")])
        self.assertNotIn("open", [entry.get("when") for entry in entries if entry.get("key", "").startswith("i:" + self.gid("하는 중"))])

    def test_a_waiting_list_of_zero_is_its_heading_a_zero_and_the_documents_note_only(self):
        entries, _ = self.listing(GENERATED.replace("- 결정: 첫 질문의 첫 줄\n- 둘째 질문?\n", "지금 없음.\n"))
        lines = shown(entries)
        at = lines.index("기다리는 것 0")
        self.assertEqual(lines[at + 1], "    지금 없음.")
        self.assertEqual(lines[at + 2], "    질문판 기준 11:01")
        self.assertFalse([entry for entry in entries if entry.get("key", "").startswith("i:" + self.gid("기다리는 것"))])

    def test_a_heading_the_screen_does_not_know_is_a_folded_group_in_the_documents_order(self):
        body = GENERATED + "\n## 나중에 할 것\n- 첫째\n- 둘째\n\n## 아무도 모르는 머리말\n- 하나\n덧말 하나\n"
        entries, _ = self.listing(body)
        heads = [text(entry["rows"][0]) for entry in entries if entry.get("key", "").startswith("g:")]
        self.assertEqual(heads, ["  › 최근에 끝난 것 1", "  › 나중에 할 것 2", "  › 아무도 모르는 머리말 1"])      # the headings as written, the count of bullets
        note = [entry for entry in entries if text(entry["rows"][0]) == "    덧말 하나"]
        self.assertEqual((len(note), note[0].get("when"), note[0]["rows"][0][-1][1]), (1, "open", "dim"))   # a note: dim, at the group's end

    def test_the_folded_group_with_nothing_in_it_still_stands_with_its_zero(self):
        entries, _ = self.listing(GENERATED.replace("## 최근에 끝난 것\n- 11-21 끝난 일 · #1 · 기록 6\n", "## 최근에 끝난 것\n"))
        self.assertIn("  › 최근에 끝난 것 0", shown(entries))

    def test_a_second_heading_of_a_kind_is_a_folded_group_and_a_hand_written_near_spelling_is_read_the_same_way(self):
        body = GENERATED.replace("## 하는 중", "## 진행 중") + "\n## 진행 중\n- 또 하나\n"
        entries, _ = self.listing(body)
        lines = shown(entries)
        self.assertIn("진행 중 5", lines)
        self.assertIn("  › 진행 중 1", lines)

    def test_a_table_a_code_fence_and_an_indented_line_are_left_to_the_full_document(self):
        body = GENERATED.replace("질문판 기준 11:01\n", "질문판 기준 11:01\n\n| 가 | 나 |\n|--|--|\n| 1 | 2 |\n\n```\n- 코드 안의 글머리\n## 코드 안의 머리말\n```\n  - 들여쓴 글머리\n")
        entries, _ = self.listing(body)
        lines = shown(entries)
        for left_out in ("| 가 | 나 |", "- 코드 안의 글머리", "코드 안의 머리말", "들여쓴 글머리", "|--|--|"):
            self.assertFalse([line for line in lines if left_out in line], left_out)
        self.assertIn("기다리는 것 2", lines)

    def test_a_long_item_is_cut_in_its_row_and_carries_its_whole_words_and_a_short_one_carries_none(self):
        long = "긴 글 " + "가나다라 마바사아 " * 30 + "끝"
        entries, _ = self.listing(GENERATED.replace("- 둘째 질문?", "- " + long), width=60)
        entry = next(entry for entry in entries if entry.get("text", "").startswith("긴 글"))
        row = text(entry["rows"][0])
        self.assertTrue(row.endswith("…"), row)
        self.assertLessEqual(cell_width(row), 60)
        self.assertEqual(entry["text"], long)               # nothing is dropped: the host sets these words in place of the row
        # An item that is not cut has nothing to open: it carries no words of its own (they would be shown twice).
        short = next(entry for entry in entries if text(entry["rows"][0]) == "  ! 결정: 첫 질문의 첫 줄")
        self.assertNotIn("text", short)
        self.assertFalse([entry for entry in entries if "text" in entry and not text(entry["rows"][0]).endswith("…")])
        # The line that just fits is not cut, one cell more is.
        for extra, cut in ((0, False), (1, True)):
            fits = "가" * ((60 - 4) // 2) + "a" * extra
            entries, _ = self.listing(GENERATED.replace("- 둘째 질문?", "- " + fits), width=60)
            entry = next(entry for entry in entries if entry.get("key", "").startswith("i:") and ("가" * 10) in text(entry["rows"][0]))
            self.assertEqual("text" in entry, cut, (extra, text(entry["rows"][0])))

    def test_the_same_words_twice_in_a_group_are_two_choices(self):
        entries, _ = self.listing(GENERATED.replace("- 둘째 질문?", "- 결정: 첫 질문의 첫 줄"))
        keys = [entry["key"] for entry in entries if entry.get("key", "").startswith("i:" + self.gid("기다리는 것"))]
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)

    def test_every_item_is_on_the_screen_whatever_the_count(self):
        """No count of the screen's own stops a group: what waits, `all N`, and a folded group are every bullet of the document."""
        for count in (60, 61, 300):
            body = GENERATED.replace("- 둘째 질문?\n", "".join("- 질문 %04d?\n" % n for n in range(count)))
            body = body.replace("- 일 하나 · #11 · 기록 1\n", "".join("- 일 %04d\n" % n for n in range(count)))
            body = body.replace("- 11-21 끝난 일 · #1 · 기록 6\n", "".join("- 끝 %04d\n" % n for n in range(count)))
            entries, _ = self.listing(body)
            items = lambda name: [text(entry["rows"][0]) for entry in entries if entry.get("key", "").startswith("i:" + self.gid(name))]
            self.assertEqual(len(items("기다리는 것")), count + 1, count)
            self.assertEqual(items("기다리는 것")[-1], "  ! 질문 %04d?" % (count - 1))
            self.assertEqual(len(items("하는 중")), count + 4, count)
            self.assertEqual(items("하는 중")[-1], "  일 다섯 · 기록 1")
            self.assertEqual(len(items("최근에 끝난 것")), count, count)
            self.assertIn("기다리는 것 %d" % (count + 1), shown(entries))
            self.assertIn("  all %d ›" % (count + 4), shown(entries))
            self.assertFalse([line for line in shown(entries) if "more in the full document" in line])
            self.assertEqual(len({entry["key"] for entry in entries if entry.get("key")}), len([1 for entry in entries if entry.get("key")]))

    def test_two_headings_that_say_the_same_words_are_two_groups_with_their_own_keys_and_open_states(self):
        body = GENERATED + "\n## Other\n- A\n\n## Other\n- B\n- A\n"
        entries, _ = self.listing(body)
        heads = [entry for entry in entries if entry.get("key", "").startswith("g:")]
        self.assertEqual([text(entry["rows"][0]) for entry in heads], ["  › 최근에 끝난 것 1", "  › Other 1", "  › Other 2"])
        keys = [entry["key"] for entry in entries if entry.get("key")]
        self.assertEqual(len(keys), len(set(keys)))                                  # every line that can be chosen has a key of its own
        ids = [entry["group"] for entry in heads]
        self.assertEqual(ids[1:], [self.gid("Other"), self.gid("Other", 2)])         # which of the two is open is told apart too
        self.assertNotEqual(ids[1], ids[2])
        # Each one's items stand under its own group, and the same words in the two are two choices.
        a_items = [entry for entry in entries if entry.get("group") == ids[1] and entry.get("key", "").startswith("i:")]
        b_items = [entry for entry in entries if entry.get("group") == ids[2] and entry.get("key", "").startswith("i:")]
        self.assertEqual((len(a_items), len(b_items)), (1, 2))
        self.assertTrue({e["key"] for e in a_items}.isdisjoint({e["key"] for e in b_items}))
        # Taking one of them out, or putting them in another order, does not change what the other is called.
        solo, _ = self.listing(GENERATED + "\n## Other\n- B\n- A\n")
        self.assertEqual([entry["group"] for entry in solo if entry.get("key", "").startswith("g:")][-1], self.gid("Other"))

    def test_a_key_is_never_made_of_the_words_so_a_very_long_heading_and_any_script_keep_it_short(self):
        for heading in ("h" * 1100, "가" * 1100, "😀" * 600, "e\u0301" * 700, "제목 " + "x" * 5000):
            body = GENERATED + "\n## %s\n- HIDDEN_ITEM\n" % heading
            entries, _ = self.listing(body)
            keys = [entry["key"] for entry in entries if entry.get("key")]
            self.assertTrue(all(len(key) <= 40 for key in keys), heading[:6])
            self.assertEqual(len(keys), len(set(keys)))
            head = [entry for entry in entries if entry.get("key", "").startswith("g:") and heading[:3] in text(entry["rows"][0])]
            self.assertEqual(len(head), 1)                                           # the folded group can be chosen
            self.assertTrue(head[0]["group"] and len(head[0]["group"]) <= 40)
            self.assertTrue([entry for entry in entries if "HIDDEN_ITEM" in text(entry["rows"][0]) and entry.get("when") == "open"
                             and entry.get("group") == head[0]["group"]])

    def test_the_whole_words_of_an_item_are_carried_at_any_length_the_answer_may_hold(self):
        for size in (4000, 4001, 12000):
            words = "가" * (size - 1) + "끝"
            entries, _ = self.listing(GENERATED.replace("- 둘째 질문?", "- " + words), width=80)
            entry = next(entry for entry in entries if entry.get("text", "").startswith("가"))
            self.assertEqual(entry["text"], words)
        emoji = "😀가e\u0301" * 900 + "끝"
        entries, _ = self.listing(GENERATED.replace("- 둘째 질문?", "- " + emoji), width=80)
        self.assertEqual(next(entry for entry in entries if entry.get("text", "").startswith("😀"))["text"], emoji)

    def test_a_second_heading_of_the_kind_that_waits_is_a_folded_group_and_only_the_first_comes_first(self):
        """``parse_groups`` reads the first heading of each of the two kinds; a second `기다리는 것` is one of the folded groups."""
        entries, _ = self.listing(GENERATED + "\n## 기다리는 것\n- 둘째 머리말의 글\n")
        lines = shown(entries)
        self.assertEqual(lines[lines.index("기다리는 것 2") + 1], "  ! 결정: 첫 질문의 첫 줄")
        self.assertIn("  › 기다리는 것 1", lines)
        self.assertNotIn("  ! 둘째 머리말의 글", lines)
        self.assertEqual(lines.index("  › 기다리는 것 1") > lines.index("기다리는 것 2"), True)

    def test_the_list_is_made_only_when_it_is_asked_for(self):
        view = self.page()
        rows, listing = session_status.popup_doc(view, 80, NOW)
        self.assertIsNone(listing)                                                   # the document read whole is the rows it always had
        self.assertEqual(rows, session_status.popup_doc_rows(view, 80))
        with mock.patch.object(session_status, "listing_entries", side_effect=AssertionError("a list was made")), \
                mock.patch.object(session_status, "parse_listing", side_effect=AssertionError("a list was read")):
            session_status.popup_doc(view, 80, NOW)
            session_status.popup_doc_rows(view, 80)

    def test_a_very_long_title_and_one_line_are_folded_to_two_rows_with_the_rest_marked(self):
        body = GENERATED.replace("# 시험용 캠페인", "# " + "제목 " * 80).replace("시험용 한 줄.", "요약 " * 120)
        entries, _ = self.listing(body, width=50)
        head = [entry for entry in entries[:3]]
        self.assertEqual([len(entry["rows"]) for entry in (head[0], head[2])], [2, 2])
        for entry in (head[0], head[2]):
            self.assertTrue(text(entry["rows"][-1]).endswith("…"))
            self.assertTrue(all(cell_width(text(row)) <= 50 for row in entry["rows"]))

    def test_the_age_and_an_old_document(self):
        fresh, _ = self.listing(age=600)
        self.assertEqual(text(fresh[1]["rows"][0]), "updated 11-15 11:02")       # the age is said once, in the title band
        old, _ = self.listing(age=4 * 86400 + 3600)
        row = old[1]["rows"][0]
        self.assertEqual(text(row), "! updated 11-15 11:02 · " + session_status.ui_text("fleet.status.popup.old"))
        self.assertNotIn("ago", text(row))
        self.assertEqual(row[0], ("! ", "lvl_y"))              # a mark, not the colour alone
        edge, _ = self.listing(age=3 * 86400)                   # the panel's own limit: three days (live.STATUS_STALE_DAYS)
        self.assertTrue(text(edge[1]["rows"][0]).startswith("! "))
        almost, _ = self.listing(age=3 * 86400 - 60)
        self.assertFalse(text(almost[1]["rows"][0]).startswith("! "))

    def test_a_time_that_cannot_be_read_is_said_not_left_empty(self):
        entries, _ = self.listing(GENERATED.replace("갱신 2025-11-15 11:02", "갱신 어제 오후"))
        said = text(entries[1]["rows"][0])
        self.assertIn(session_status.ui_text("fleet.status.popup.updated_unknown"), said)
        self.assertTrue(said)
        entries, _ = self.listing(GENERATED.replace("갱신 2025-11-15 11:02\n", ""))
        self.assertIn(session_status.ui_text("fleet.status.popup.updated_unknown"), text(entries[1]["rows"][0]))

    def test_a_year_that_is_not_this_one_is_said(self):
        entries, _ = self.listing(GENERATED.replace("2025-11-15", "2019-11-15"))
        self.assertIn("updated 2019-11-15 11:02", text(entries[1]["rows"][0]))

    def test_every_row_of_the_screen_fits_the_width(self):
        body = GENERATED + "\n## 나중에 할 것\n" + "".join("- 한 줄 %d " % n + "글 " * (n * 7) + "\n" for n in range(1, 6))
        for width in (20, 30, 41, 60, 92, 116, 200):
            entries, _ = self.listing(body, width=width)
            for entry in entries:
                for part in ("rows", "alt"):
                    for row in entry.get(part) or ():
                        self.assertLessEqual(cell_width(text(row)), max(8, width), (width, text(row)))

    def test_no_control_character_reaches_the_screen(self):
        entries, _ = self.listing(GENERATED.replace("둘째 질문?", "둘째\x1b[31m 질문\u202e?\x07"))
        for entry in entries:
            for part in ("rows", "alt"):
                for row in entry.get(part) or ():
                    self.assertNotRegex(text(row), "[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")

    def test_a_page_that_is_not_the_generated_one_has_no_list_screen_and_the_rows_it_always_had(self):
        for body, name in ((DOC.format(name="alpha") + TABLE, "현황.md"),
                           (GENERATED.split("\n", 1)[1], "campaign.md"),             # the marker gone
                           ("\n" + GENERATED, "campaign.md"),                        # not on the first line
                           (GENERATED.replace(campaign_document.MARKER, "<!-- hearting:campaign-document v2 -->"), "campaign.md")):
            view = self.page(body, name=name)
            rows, listing = session_status.popup_doc(view, 80, NOW, want_list=True)
            self.assertIsNone(listing, name)
            lines = body.splitlines()
            expected = [[(session_status.transcript.clean(t), k) for t, k in row] for row in session_status.transcript.doc_rows(lines, 80)]
            self.assertEqual(rows, expected)
            self.assertEqual(rows, session_status.popup_doc_rows(view, 80))

    def test_no_document_an_unreadable_one_and_a_cut_one_keep_their_screens(self):
        self.campaign("beta", doc=False)
        none = self.read(self.route(2, "beta"))
        rows, listing = session_status.popup_doc(none, 80, NOW)
        self.assertEqual((listing, [text(row) for row in rows]), (None, [session_status.ui_text("fleet.repo.no_status") + " · campaigns/2026-11-10_beta/현황.md"]))
        view = self.page()
        with mock.patch.object(session_status, "read_text", return_value=(None, False)):
            self.assertEqual(session_status.popup_doc(view, 80, NOW), (None, None))
            self.assertIsNone(session_status.popup_doc_rows(view, 80))
        big = GENERATED + "".join("- 하나 %05d 가나다라마바사아자차\n" % n for n in range(6000))
        view = self.page(big)
        rows, listing = session_status.popup_doc(view, 80, NOW, want_list=True)
        self.assertIsNone(listing)                               # cut at 64 KiB: the document as it was, with its `… 64 KiB`
        self.assertEqual(text(rows[-1]), "… 64 KiB")

    def test_a_real_generated_document_gives_the_screen(self):
        from datetime import timezone
        unit = {"key": "pr:7", "pr": 7, "v": "v4.1.0", "title": "무언가를 고침", "impl": {"slug": "x"}, "count": 3,
                "closed_at": NOW - 3600, "routes": 2, "activity": NOW}
        waits = {"status": "ok", "at": NOW, "items": ["첫째?", "둘째?"]}
        doc = campaign_document.render_document("alpha 캠페인", "알파 작업 중.", {"active": [unit], "active_folded": 4, "done": [unit],
                                                                          "done_folded": 12}, waits, now=NOW, tz=timezone.utc)
        entries, _ = self.listing(doc)
        lines = shown(entries)
        for part in ("alpha 캠페인", "updated 11-15 ", "알파 작업 중.", "기다리는 것 2", "  ! 첫째?", "  ! 둘째?", "하는 중 1",
                     "  무언가를 고침 · #7 · v4.1.0 · 기록 3", "  › 최근에 끝난 것 1", "  full document ›"):
            self.assertTrue(any(line.startswith(part) or line == part for line in lines), (part, lines))


if __name__ == "__main__":
    unittest.main()
