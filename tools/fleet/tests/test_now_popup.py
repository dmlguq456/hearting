"""The NOW popup's backend: the list drawn from ``timeline.rows`` (oldest first, the bottom row stood on), the
folded long session, the page one row stands for, an event row's section, the empty list, the words, and the
size sweep.

Every timeline, conversation, ledger, route, question and message here is synthetic and lives in a private
temporary state; no model is called and no key is sent to any harness. The popup draws only what the timeline
layer gives: ``timeline.fold`` and ``timeline.passage`` are called, and ``timeline.scroll_steps`` is not.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import re
import time
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT / "utilities"))
from fleet import palette  # noqa: E402
from fleet.explorer import timeline as tl  # noqa: E402
from fleet.explorer import timeline_popup as popup  # noqa: E402
from fleet.tests.test_now_timeline_layer import SID, T0, _Box  # noqa: E402
import session_shell  # noqa: E402

# The keys the panel and the popups already paint with: no color of the popup's own.
OLD_KEYS = {"dim", "loc_repo", "g_blocked", "lvl_g", "hb_claude", "hb_codex", "hb_opencode"}


def synth(prompts=3, nows=2, harness="claude", sid=SID, events=True, days=1):
    """A story as ``timeline.rows`` gives it (the same fields), with no file behind it."""
    out, at = [], T0
    for day in range(days):
        at = T0 + day * 86400
        stamp = time.strftime("%Y-%m-%d", time.localtime(at))
        out.append({"key": "date:" + stamp, "at": at, "kind": "date", "date": stamp, "text": stamp[5:],
                    "today": day == days - 1, "harness": harness, "sid": sid, "generation": 0, "open": None})
        for number in range(prompts):
            at += 600
            key = "prompt:%s:%d:%d" % (sid, day, number)
            out.append({"key": key, "at": at, "kind": "prompt", "text": "요청 %d-%d 을 처리해 줘" % (day, number),
                        "harness": harness, "sid": sid, "generation": 0, "answer": number == 1, "lines": 1,
                        "paste": False, "seq": number, "open": {"kind": "passage", "row": "prompt", "harness": harness,
                                                                 "sid": sid, "at": at, "text": "x"}})
            for step in range(nows):
                at += 60
                out.append({"key": "now:%s:%d:%d:%d" % (sid, day, number, step), "at": at, "kind": "now",
                            "text": "요약 %d-%d-%d: 한 일을 적었다" % (day, number, step), "harness": harness, "sid": sid,
                            "generation": 0, "prompt": key, "current": False,
                            "open": {"kind": "passage", "row": "now", "harness": harness, "sid": sid}})
            if events and number == 0:
                out.append({"key": "route:rt-1:start:%d" % day, "at": at + 5, "kind": "route", "sub": "start",
                            "route_id": "rt-1", "text": "시험 route", "capability": "code", "intensity": "quick",
                            "state": "open", "harness": harness, "sid": sid, "generation": 0,
                            "open": {"kind": "popup", "popup": "routes", "id": "rt-1"}})
                out.append({"key": "route:rt-1:end:%d" % day, "at": at + 50, "kind": "route", "sub": "end",
                            "route_id": "rt-1", "text": "시험 route", "capability": "code", "intensity": "quick",
                            "state": "done", "result": "passed", "elapsed": 1860.0, "harness": harness, "sid": sid,
                            "generation": 0, "open": {"kind": "popup", "popup": "routes", "id": "rt-1"}})
                out.append({"key": "question:q1:asked", "at": at + 7, "kind": "question", "sub": "asked", "id": "q1",
                            "source": "S4", "text": "어느 쪽으로 할까요?", "state": "", "harness": harness, "sid": sid,
                            "generation": 0, "open": {"kind": "popup", "popup": "questions", "id": "q1"}})
                out.append({"key": "message:m1:received", "at": at + 9, "kind": "message", "sub": "received",
                            "who": "[e4]", "message_kind": "steer", "delivery": None, "id": "m1", "text": "같이 봐 주세요",
                            "harness": harness, "sid": sid, "generation": 0,
                            "open": {"kind": "popup", "popup": "messages", "id": "m1"}})
                out.append({"key": "message:m2:sent", "at": at + 11, "kind": "message", "sub": "sent", "who": "[a3]",
                            "message_kind": "request", "delivery": "delivered", "id": "m2", "text": "리뷰 부탁",
                            "harness": harness, "sid": sid, "generation": 0,
                            "open": {"kind": "popup", "popup": "messages", "id": "m2"}})
                out.append({"key": "divider:%d" % day, "at": at + 13, "kind": "divider", "sub": "compact", "text": "",
                            "harness": harness, "sid": sid, "generation": 0, "open": None})
    out.sort(key=lambda row: row["at"])
    for row in reversed(out):
        if row["kind"] == "now":
            row["current"] = True
            break
    return out


def text_of(row):
    return "".join(text for text, _key in row)


class _Korean(unittest.TestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"})
        patch.start()
        self.addCleanup(patch.stop)


class ListShapeTest(_Korean):
    def test_each_row_is_time_two_cells_mark_and_text_with_the_panels_keys(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                story = synth(harness=harness)
                view = popup.list_view(story, 100, 60)
                self.assertEqual(len(view["rows"]), len(story))
                keys = set()
                for row, drawn, note in zip(story, view["rows"], view["meta"]):
                    keys |= {key for _text, key in drawn if key}
                    line = text_of(drawn)
                    if row["kind"] == "date":
                        self.assertTrue(line.startswith("오늘 ") and "─" in line, line)
                        continue
                    self.assertEqual(line[:5], time.strftime("%H:%M", time.localtime(row["at"])), line)
                    self.assertEqual(line[5:7], "  ", line)
                    if row["kind"] == "prompt":
                        self.assertEqual(drawn[2], (">", "hb_" + harness))
                        self.assertEqual(drawn[-1][1], "loc_repo")
                        self.assertEqual(line[7:9], "> ")
                        if row["answer"]:
                            self.assertIn("> 답 · ", line)
                    elif row["kind"] == "now":
                        # A NOW stands two cells further in than a prompt's text, without a mark of its own.
                        self.assertEqual(line[7:11], "    ", line)
                        body = next(key for text, key in drawn if text.startswith(row["text"][:4]))
                        self.assertEqual(body, "loc_repo" if row["current"] else "dim")
                        self.assertEqual(row["current"], line.rstrip().endswith("지금"), line)
                    elif row["kind"] == "route":
                        self.assertIn(line[7], "⠏✓✕")
                    elif row["kind"] == "question":
                        self.assertEqual(line[7], "◑")
                    elif row["kind"] == "message":
                        self.assertIn(line[7], "←→")
                    self.assertEqual(note["select"], row["kind"] != "divider", (row["kind"], note))
                # No color of its own: the panel's own keys, and a route's glyph keys.
                from fleet.session_line import route_glyph
                allowed = OLD_KEYS | {route_glyph(marker)[1] for marker in ("working", "done", "dead")}
                self.assertLessEqual(keys, allowed, keys - allowed)
                self.assertIn("hb_" + harness, keys)

    def test_the_marks_and_keys_of_route_question_and_message_rows_are_the_panels(self):
        story = [row for row in synth() if row["kind"] in ("route", "question", "message")]
        from fleet.session_line import route_glyph
        marks = {}
        for row in story:
            drawn, _note = popup.draw_row(row, 90)
            marks[(row["kind"], row.get("sub"))] = drawn[2]
        self.assertEqual(marks[("route", "start")], ("⠏", route_glyph("working")[1]))
        self.assertEqual(marks[("route", "end")], ("✓", route_glyph("done")[1]))
        self.assertEqual(marks[("question", "asked")], ("◑", "g_blocked"))
        self.assertEqual(marks[("message", "received")], ("←", "lvl_g"))
        self.assertEqual(marks[("message", "sent")], ("→", "loc_repo"))
        end = next(row for row in story if row["kind"] == "route" and row["sub"] == "end")
        unproven, _note = popup.draw_row({**end, "result": "unproven"}, 90)
        self.assertEqual(unproven[2], ("✕", route_glyph("dead")[1]))

    def test_the_divider_and_date_rows_and_the_current_row_on_the_right(self):
        view = popup.list_view(synth(), 80, 60)
        lines = [text_of(row) for row in view["rows"]]
        self.assertTrue(any("○ 압축" in line for line in lines), lines)
        self.assertEqual(view["rows"][0][0][0], "오늘 " + synth()[0]["text"])
        self.assertTrue(lines[-1].endswith("지금"), lines[-1])
        self.assertEqual(len(lines[-1]) <= 80 or sum(popup.cells(c) for c in lines[-1]) <= 80, True)

    def test_the_list_opens_on_the_current_now_and_keeps_a_chosen_row(self):
        story = synth()
        view = popup.list_view(story, 80, 60)
        current = [row for row in story if row.get("current")][0]
        self.assertEqual(view["start"], current["key"])
        chosen = story[3]["key"]
        self.assertEqual(popup.list_view(story, 80, 60, chosen)["start"], chosen)
        self.assertEqual(popup.list_view(story, 80, 60, "gone")["start"], current["key"])

    def test_event_rows_name_their_sections_and_prompt_rows_read(self):
        view = popup.list_view(synth(), 80, 60)
        by = {}
        for note in view["meta"]:
            by.setdefault(note["kind"], note)
        self.assertEqual(by["route"]["go"], "routes:rt-1")
        self.assertEqual(by["question"]["go"], "questions")
        self.assertEqual(by["message"]["go"], "messages")
        self.assertTrue(by["prompt"]["read"] and by["now"]["read"])
        self.assertNotIn("go", by["prompt"])
        self.assertFalse(by["date"]["select"] or by["divider"]["select"])

    def test_a_row_text_is_only_what_the_layer_gave(self):
        row = {**synth()[2], "text": "비밀 sk-ant-api03-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"}
        self.assertIn("비밀 sk-ant-api03-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX", text_of(popup.draw_row(row, 90)[0]))

    def test_the_empty_list_is_one_line_in_words(self):
        view = popup.list_view([], 72, 12)
        self.assertEqual([text_of(row) for row in view["rows"]], ["이 세션에는 아직 남은 기록이 없습니다"])
        self.assertEqual(view["start"], "")
        self.assertFalse(any(note["select"] for note in view["meta"]))
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "en"}):
            self.assertEqual(text_of(popup.list_view([], 72, 12)["rows"][0]), "Nothing is recorded for this session yet")


class FoldTest(_Korean):
    def test_a_list_that_fits_is_whole_and_a_long_one_goes_through_timeline_fold(self):
        story = synth(prompts=6, nows=3, days=2)
        whole = popup.list_view(story, 80, len(story))
        self.assertFalse(whole["folded"])
        self.assertEqual(len(whole["rows"]), len(story))
        room = 25
        with mock.patch.object(tl, "fold", wraps=tl.fold) as fold:
            view = popup.list_view(story, 80, room)
        fold.assert_called_once()
        self.assertEqual(fold.call_args.args[1], room)
        self.assertTrue(view["folded"])
        self.assertLess(len(view["rows"]), len(story))
        # The prompt that holds the row stood on is open (all its NOWs); the others show their last NOW with `+N`.
        lines = [text_of(row) for row in view["rows"]]
        self.assertTrue(any(re.search(r"\+\d+ 접음", line) for line in lines), lines)
        self.assertEqual(sum(1 for note in view["meta"] if note["kind"] == "date"), 2)
        self.assertEqual(sum(1 for note in view["meta"] if note["kind"] == "divider"), len(
            [row for row in story if row["kind"] == "divider"]))

    def test_the_chosen_prompt_unfolds_and_the_rest_fold_by_the_layers_rule(self):
        story = synth(prompts=6, nows=3, days=1, events=False)
        first = next(row for row in story if row["kind"] == "now" and row["key"].endswith(":0:0:2"))
        view = popup.list_view(story, 80, 12, first["key"])
        keys = [note["key"] for note in view["meta"]]
        for step in range(3):
            self.assertIn(first["key"].rsplit(":", 1)[0] + ":%d" % step, keys)
        folded_rows = [row for row in view["rows"] if re.search(r"\+\d+ 접음", text_of(row))]
        self.assertGreaterEqual(len(folded_rows), 3)
        self.assertTrue(view["folded"])

    def test_the_folded_current_row_says_both_words(self):
        story = synth(prompts=6, nows=3, days=1, events=False)
        view = popup.list_view(story, 80, 10, story[1]["key"])
        last = text_of(view["rows"][-1])
        self.assertRegex(last, r"\+2 접음 · 지금$")


class MarkerColumnTest(_Korean):
    """11-17 review: the question marker is the screen's word, and the event markers fill one column."""

    def event_rows(self, story, width=100):
        view = popup.list_view(story, width, 200)
        return [(row, drawn) for row, drawn in zip(story, view["rows"]) if row["kind"] in ("route", "question", "message")]

    def test_the_question_marker_is_the_screens_word_in_both_languages(self):
        question = next(row for row in synth() if row["kind"] == "question")
        for lang, word in (("ko", "질문"), ("en", "question")):
            with self.subTest(lang=lang), mock.patch.dict(os.environ, {"FLEET_NOW_LANG": lang}):
                drawn, _note = popup.draw_row(question, 90)
                line = text_of(drawn)
                self.assertIn(" " + word + " ", line)
                self.assertEqual(popup.marker(question), word)
        self.assertNotIn("question", text_of(popup.draw_row(question, 90)[0]))   # ko: no hard-coded English left

    def test_the_text_of_every_event_row_starts_in_one_column(self):
        story = synth()
        story.append({**next(row for row in story if row["kind"] == "message"), "key": "message:m2:sent", "sub": "sent",
                      "message_kind": "request", "text": "다른 종류"})
        for lang in ("ko", "en"):
            with self.subTest(lang=lang), mock.patch.dict(os.environ, {"FLEET_NOW_LANG": lang}):
                events = self.event_rows(story)
                kinds = {popup.marker(row) for row, _ in events}
                self.assertGreaterEqual(len(kinds), 4, kinds)
                starts = {popup.cells("".join(text for text, _key in drawn[:4])) for _row, drawn in events}
                self.assertEqual(len(starts), 1, starts)
                widest = max(popup.cells(word) for word in kinds)
                self.assertEqual(starts.pop(), 5 + 2 + 1 + 1 + widest + 2)

    def test_the_column_does_not_move_when_a_fold_shows_fewer_rows(self):
        story = synth(prompts=9, nows=3)
        whole = popup.list_view(story, 100, 500)
        folded = popup.list_view(story, 100, 12)
        self.assertTrue(folded["folded"])
        def starts(view, count):
            return {popup.cells("".join(text for text, _key in drawn[:4])) for drawn, note in zip(view["rows"], view["meta"])
                    if note["kind"] in ("route", "question", "message")}
        self.assertEqual(starts(whole, 0), starts(folded, 0))


class WordsTest(_Korean):
    def test_every_new_word_has_both_languages(self):
        messages = json.loads((TOOLS / "i18n" / "messages.json").read_text(encoding="utf-8"))["messages"]
        used = set(re.findall(r'_word\("([a-z_]+)"', (TOOLS / "fleet/explorer/timeline_popup.py").read_text(encoding="utf-8")))
        used = {name for name in used if not name.endswith("_")}
        used |= {"now_divider_" + name for name in popup.DIVIDER_MARKS} | {"now_question_answered", "now_question_closed", "now_earlier"}
        used |= {"now_reason_" + name.replace("-", "_") for name in popup.REASONS} | {"popup_now", "popup_now_page"}
        self.assertTrue(used)
        for name in used:
            with self.subTest(word=name):
                entry = messages["fleet.session_panel." + name]
                self.assertTrue(entry["en"] and entry["ko"] and entry["en"] != entry["ko"] or name in ("now_route_passed",), entry)

    def test_a_reason_is_said_in_a_sentence_and_an_unknown_one_in_the_last(self):
        for reason in sorted(popup.REASONS):
            with self.subTest(reason=reason):
                said = popup.read_reason(reason)
                self.assertGreater(len(said), 8)
                self.assertNotIn(reason, said)
        self.assertEqual(popup.read_reason("something-new"), popup.read_reason("unreadable"))


class NoScrollTest(unittest.TestCase):
    def test_the_popup_never_asks_for_the_harness_window_to_move(self):
        for path in (TOOLS / "fleet/explorer/timeline_popup.py", ROOT / "utilities/session-shell/host.cjs"):
            self.assertNotIn("scroll_steps", path.read_text(encoding="utf-8"), path)
        text = (ROOT / "utilities/session_shell.py").read_text(encoding="utf-8")
        body = text[text.index("def _now_popup"):text.index("def _panel_popup")]
        self.assertNotIn("scroll_steps", body)
        self.assertNotIn("child.write", (ROOT / "utilities/session-shell/host.cjs").read_text(encoding="utf-8").split("function enterNow")[1].split("function covered")[0])


class SizeSweepTest(_Korean):
    def test_no_row_runs_over_and_nothing_raises_from_60_to_160_wide_and_16_to_60_high(self):
        story = synth(prompts=7, nows=3, days=3)
        # The popup's inside and room, as panel-view's `popupWidth` and `popupRoom` give them.
        for cols in range(60, 161, 5):
            width = cols - 4 if cols < 80 else min(116, cols - 4)
            inner = width - 4
            for height in range(16, 61, 4):
                high = height - 2 if height >= 8 else height          # panel-view's `popupHeight`, then `popupRoom`
                room = max(3, (high - 2 if high > 3 else high - 1) - 3)
                with self.subTest(cols=cols, height=height):
                    view = popup.list_view(story, inner, room, story[5]["key"])
                    for drawn in view["rows"]:
                        self.assertLessEqual(popup.cells(text_of(drawn)), inner, text_of(drawn))
                    self.assertEqual(len(view["rows"]), len(view["meta"]))
                    # The time and the mark stay when the text is cut.
                    for drawn, note in zip(view["rows"], view["meta"]):
                        if note["kind"] in ("prompt", "now"):
                            self.assertRegex(text_of(drawn)[:5], r"\d\d:\d\d")

    def test_a_long_text_is_cut_with_an_ellipsis_and_the_time_and_mark_stay(self):
        story = synth(events=False)
        first = next(row for row in story if row["kind"] == "prompt")
        drawn, _note = popup.draw_row({**first, "text": "아주 긴 글 " * 40}, 40)
        line = text_of(drawn)
        self.assertIn(popup.cells(line), (39, 40))      # a wide character that does not fit leaves its cell blank
        self.assertTrue(line.endswith("…") and line[:5].count(":") == 1 and line[7] == ">", line)
        now = next(row for row in story if row["kind"] == "now" and row["current"])
        drawn, _note = popup.draw_row({**now, "text": "긴 요약 " * 40}, 40)
        self.assertTrue(text_of(drawn).rstrip().endswith("지금") and "…" in text_of(drawn), text_of(drawn))
        self.assertLessEqual(popup.cells(text_of(drawn)), 40)


class AnswerTest(_Box, unittest.TestCase):
    """The backend's answer over a real (synthetic) timeline file and conversation."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"})
        patch.start()
        self.addCleanup(patch.stop)

    def build(self):
        from fleet import titles
        path = self.claude_file()
        f1 = self.put(path, self.user("첫 요청이다", T0))
        self.put(path, self.said("첫 답을 했다", T0 + 5))
        e1 = path.stat().st_size
        f2 = self.put(path, self.user("둘째 요청이다", T0 + 120))
        self.put(path, self.said("둘째 답을 했다", T0 + 130))
        e2 = path.stat().st_size
        for row in ({"kind": "prompt", "at": T0, "seq": 1, "text": "첫 요청이다", "lines": 1, "from": f1},
                    {"kind": "now", "at": T0 + 10, "text": "첫 일을 했다", "from": 0, "to": e1, "unit": "bytes"},
                    {"kind": "prompt", "at": T0 + 120, "seq": 2, "text": "둘째 요청이다", "lines": 1, "from": f2},
                    {"kind": "now", "at": T0 + 140, "text": "둘째 일을 했다", "from": e1, "to": e2, "unit": "bytes"}):
            titles.append_timeline(SID, row)
        self.route_record("r1", "팝업 시험 route", T0 + 30, closed=T0 + 90)

    def ask(self, **extra):
        request = {"kind": "now", "columns": 72, "rows": 12, **extra}
        return session_shell._now_popup({"harness": "claude", "sid": SID}, request, 72, 256, False)

    def test_the_list_then_a_prompt_read_then_back_without_a_key_to_the_window(self):
        self.build()
        listed = self.ask()
        self.assertTrue(listed["ok"] and listed["kind"] == "now" and not listed["page"])
        self.assertEqual(len(listed["rows"]), len(listed["meta"]))
        kinds = [note["kind"] for note in listed["meta"]]
        self.assertEqual(kinds[0], "date")
        self.assertIn("route", kinds)
        self.assertEqual(listed["meta"][[n["key"] for n in listed["meta"]].index(listed["start"])]["kind"], "now")
        # Each run carries its cells, so the host lays the row out by the measure Fleet drew it with.
        self.assertTrue(all(len(run) == 3 for row in listed["rows"] for run in row))
        first = next(note for note in listed["meta"] if note["kind"] == "prompt")
        read = self.ask(open=first["key"], page=True)
        self.assertTrue(read["ok"] and read["page"] and read["read"], read)
        words = "\n".join("".join(run[0] for run in row) for row in read["rows"])
        self.assertIn("첫 요청이다", words)
        self.assertIn("첫 답을 했다", words)
        again = self.ask(open=first["key"])
        self.assertEqual(again["start"], first["key"])
        self.assertEqual(again["rows"], self.ask(open=first["key"])["rows"])

    def test_a_now_row_reads_the_conversation_it_stands_for(self):
        self.build()
        now = next(n for n in self.ask()["meta"] if n["kind"] == "now")
        read = self.ask(open=now["key"], page=True)
        self.assertTrue(read["read"], read)
        self.assertIn("첫 답을 했다", "\n".join("".join(run[0] for run in row) for row in read["rows"]))

    def test_a_part_that_cannot_be_read_is_one_line_in_words(self):
        self.build()
        os.remove(next(Path(os.environ["CLAUDE_CONFIG_DIR"]).rglob(SID + ".jsonl")))
        now = next(n for n in self.ask()["meta"] if n["kind"] == "now")
        read = self.ask(open=now["key"], page=True)
        self.assertTrue(read["ok"] and not read["read"], read)
        self.assertEqual(len(read["rows"]), 1)
        self.assertEqual("".join(run[0] for run in read["rows"][0]), popup.read_reason("no-transcript"))
        # A row that is not there asks for nothing.
        self.assertEqual(self.ask(open="gone", page=True), {"ok": False, "reason": "no-row"})
        # Nor does the same key read a route row (no passage).
        route = next(n for n in self.ask()["meta"] if n["kind"] == "route")
        self.assertEqual(self.ask(open=route["key"], page=True), {"ok": False, "reason": "no-row"})

    def test_an_empty_session_answers_the_one_line(self):
        listed = self.ask()
        self.assertTrue(listed["ok"])
        self.assertEqual(["".join(run[0] for run in row) for row in listed["rows"]], ["이 세션에는 아직 남은 기록이 없습니다"])
        self.assertEqual(listed["start"], "")

    def test_the_popup_kind_is_the_panel_popups_one(self):
        self.build()
        answer = session_shell._panel_popup({"harness": "claude", "sid": SID}, {"kind": "now", "columns": 72, "rows": 12})
        self.assertTrue(answer["ok"] and answer["kind"] == "now")
        self.assertEqual(session_shell._panel_popup({"harness": "claude", "sid": SID}, {"kind": "now", "columns": 10})["reason"], "invalid-popup")

    def test_an_answer_stays_under_the_hosts_limit_and_keeps_the_newest_rows(self):
        story = synth(prompts=400, nows=3, days=3)
        with mock.patch.object(tl, "rows", return_value=story):
            answer = session_shell._now_popup({"harness": "claude", "sid": SID}, {"rows": 30, "columns": 100}, 100, 256, False)
            page = session_shell._now_popup({"harness": "claude", "sid": SID}, {"rows": 30, "columns": 100, "page": True,
                                                                              "open": story[2]["key"]}, 100, 256, False)
        current = [row for row in story if row.get("current")][0]
        self.assertLessEqual(len(json.dumps({"id": 0, **answer}, ensure_ascii=False).encode()), session_shell.POPUP_ANSWER_BYTES)
        self.assertEqual(answer["meta"][-1]["key"], current["key"])
        self.assertEqual(len(answer["rows"]), len(answer["meta"]))
        self.assertTrue(page["ok"])

    def test_a_list_past_the_limit_says_how_many_rows_it_left_off_and_starts_at_a_group(self):
        story = synth(prompts=240, nows=1, days=3)
        for row in story:
            if row["kind"] in ("prompt", "now"):
                row["text"] = "한국어로 길게 적은 요청과 요약이다 " * 6
        with mock.patch.object(tl, "rows", return_value=story):
            answer = session_shell._now_popup({"harness": "claude", "sid": SID}, {"rows": 30, "columns": 120}, 120, 256, False)
            whole = popup.list_view(story, 120, 30)
        self.assertLessEqual(len(json.dumps({"id": 0, **answer}, ensure_ascii=False).encode()), session_shell.POPUP_ANSWER_BYTES)
        self.assertGreater(len(whole["rows"]), len(answer["rows"]) - 1, "the case must really be over the limit")
        first = "".join(run[0] for run in answer["rows"][0])
        dropped = len(whole["rows"]) - (len(answer["rows"]) - 1)
        self.assertEqual(first, "… 이전 %d줄" % dropped)
        self.assertEqual(answer["meta"][0]["select"], False)
        # The row under it begins a group: a date, a divider or a prompt, never a NOW without its prompt.
        self.assertIn(answer["meta"][1]["kind"], ("date", "divider", "prompt"), answer["meta"][:3])
        self.assertTrue(dropped > 0 and answer["meta"][-1]["key"] == [row for row in story if row.get("current")][0]["key"])
        self.assertEqual(len(answer["rows"]), len(answer["meta"]))
        # Dim, like the `외 N개` of the other popups.
        self.assertEqual(answer["styles"][answer["rows"][0][0][1]].get("dim"), True)

    def test_a_list_within_the_limit_says_nothing_of_the_kind(self):
        story = synth(prompts=9, nows=2)
        with mock.patch.object(tl, "rows", return_value=story):
            answer = session_shell._now_popup({"harness": "claude", "sid": SID}, {"rows": 30, "columns": 120}, 120, 256, False)
        self.assertFalse(any("이전" in "".join(run[0] for run in row) for row in answer["rows"]))

    def test_a_long_page_keeps_its_top_and_its_end_with_a_gap(self):
        self.build()
        now = next(n for n in self.ask()["meta"] if n["kind"] == "now")
        rows = [([("줄 %d " % n + "가나다 " * 6, "dim")], None) for n in range(2000)]
        with mock.patch.object(tl, "passage", return_value={"ok": True, "reason": "", "rows": rows, "marks": [], "clipped": False,
                                                           "span": None, "head": ""}):
            read = self.ask(open=now["key"], page=True)
        self.assertLessEqual(len(json.dumps({"id": 0, **read}, ensure_ascii=False).encode()), session_shell.POPUP_ANSWER_BYTES)
        words = ["".join(run[0] for run in row) for row in read["rows"]]
        self.assertTrue(words[0].startswith("줄 0 "))
        self.assertIn("…", words)
        self.assertTrue(words[-1].startswith("줄 1999 "))


class PaintTest(_Korean):
    """Colors: the same keys in every color count; none where there is none."""

    def paint(self, harness, colors, no_color):
        story = synth(harness=harness)
        with mock.patch.object(tl, "rows", return_value=story):
            return session_shell._now_popup({"harness": harness, "sid": SID}, {"rows": 60, "columns": 100}, 100, colors, no_color)

    def test_three_harnesses_and_every_color_count(self):
        marks = {"claude": palette.style_components("hb_claude", colors=256), "codex": palette.style_components("hb_codex", colors=256),
                 "opencode": palette.style_components("hb_opencode", colors=256)}
        self.assertEqual(len({marks[name]["foreground"] for name in marks}), 3)
        for harness in marks:
            for colors, no_color in ((1 << 24, False), (256, False), (8, False), (8, True), (256, True)):
                with self.subTest(harness=harness, colors=colors, no_color=no_color):
                    answer = self.paint(harness, colors, no_color)
                    prompt = next(row for row, note in zip(answer["rows"], answer["meta"]) if note["kind"] == "prompt")
                    mark = answer["styles"][prompt[2][1]]
                    self.assertEqual(prompt[2][0], ">")
                    expect = palette.style_components("hb_" + harness, colors=colors, no_color=no_color)
                    self.assertEqual({key: mark[key] for key in expect}, expect)
                    for style in answer["styles"]:
                        if no_color:
                            self.assertFalse(style.get("foreground") not in (None, -1) or style.get("bold") or style.get("dim"), style)
                        elif colors == 8:
                            self.assertTrue(style.get("foreground") is None or -1 <= style["foreground"] < 8, style)


if __name__ == "__main__":
    unittest.main()
