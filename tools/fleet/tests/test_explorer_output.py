"""Raw output of the selected request: the rows, the budget, the health words and the canonical screens.

The records are opened and cut by the shared ``utilities/attempt_view.py``; these tests build real
files and databases (Codex JSONL, Claude transcripts, OpenCode v1/v2 SQLite) and assert the rows the
pane would draw, not only that something was read.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.fleet.explorer.output import (CLEANED, RESET_NOTE, SUPERVISOR_ONLY, TAIL_RECORDS, OutputLine,
                                         OutputReader, SourcePlan, attempt_view, fit_row, line_row,
                                         render_output)
from tools.fleet.explorer.types import FocusKey, Health
from tools.fleet.tests import explorer_fixture as ef

VIEW = attempt_view()
Line = VIEW.Line


def write_jsonl(path, rows):
    Path(path).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def message(text, ident, at="2026-10-20T12:00:00.000Z", user=False):
    kind = "user_message" if user else "agent_message"
    return {"type": "item.completed", "timestamp": at, "item": {"id": ident, "type": kind, "text": text}}


def command(cmd, out, code, ident, at="2026-10-20T12:00:00.000Z"):
    return {"type": "item.completed", "timestamp": at,
            "item": {"id": ident, "type": "command_execution", "command": cmd,
                     "aggregated_output": out, "exit_code": code}}


def plan(path, harness="codex", label="test", attempt="att-fixture", abbr=None):
    abbr = abbr or {"codex": "CX", "claude": "CL", "opencode": "OC"}[harness]
    return SourcePlan(attempt, label, harness, abbr, "stage", direct=str(path))


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def reader(self, **options):
        return ef.output_reader(**options)

    def rows(self, view):
        return [fit_row(row.text, row.omitted_lines, 400) for row in view.rows]


class RowFormat(unittest.TestCase):
    def test_a_row_is_one_physical_line_with_the_omitted_count(self):
        self.assertEqual(fit_row("abc", 0, 20), "abc")
        self.assertEqual(fit_row("abc", 3, 20), "abc (+3줄)")
        self.assertEqual(fit_row("한" * 40, 0, 20), "한" * 9 + "…")

    def test_a_clipped_first_line_keeps_the_marker_and_ends_with_the_ellipsis(self):
        row = fit_row("x" * 100, 7, 30)
        self.assertEqual(row, "x" * 21 + " (+7줄)…")
        wide = fit_row("가" * 50, 2, 30)
        self.assertTrue(wide.endswith(" (+2줄)…"))
        from tools.fleet.explorer.output import cell_width
        self.assertLessEqual(cell_width(wide), 30)

    def test_render_output_scrolls_from_the_top_or_follows_the_newest(self):
        records = [OutputLine(f"row{i}") for i in range(10)]
        self.assertEqual(render_output(records, 20, 3, scroll=0), ("row0", "row1", "row2"))
        self.assertEqual(render_output(records, 20, 3, scroll=None), ("row7", "row8", "row9"))
        self.assertEqual(render_output(records, 20, 3, scroll=99), ("row7", "row8", "row9"))
        self.assertEqual(render_output(records, 20, 0), ())

    def test_each_kind_of_line_has_its_glyph(self):
        cases = [
            (Line("tool", "command: ls -la"), "[test] CX $ ls -la"),
            (Line("output", "output: total 0"), "[test] CX > total 0"),
            (Line("result", "exit: 0"), "[test] CX exit 0"),
            (Line("result", "exit: -1"), "[test] CX exit -1"),
            (Line("message", "assistant: done"), "[test] CX » done"),
            (Line("prompt", "user: hello"), "[test] CX « hello"),
            (Line("thinking", "thinking: hmm"), "[test] CX ~ hmm"),
            (Line("notice", "notice: careful"), "[test] CX ! careful"),
            (Line("unknown", "· weird {}"), "[test] CX · weird {}"),
            (Line("tool", "tool Bash:\npwd"), "[test] CX $ pwd"),
            (Line("tool", "tool Read:"), "[test] CX ◆ Read"),
        ]
        for line, expected in cases:
            with self.subTest(line=line):
                self.assertEqual(line_row(line, "test", "CX").text, expected)

    def test_only_the_formatters_own_label_is_taken_off_a_message(self):
        row = line_row(Line("message", "assistant: artifact: /x/y\nsecond\nthird"), "test", "CX")
        self.assertEqual((row.text, row.omitted_lines), ("[test] CX » artifact: /x/y", 2))

    def test_supervisor_bookkeeping_is_not_output(self):
        self.assertIsNone(line_row(Line("supervisor", "supervisor: turn.started"), "owner", "CL"))

    def test_the_time_prefix_goes_before_the_label(self):
        self.assertEqual(line_row(Line("message", "assistant: hi"), "owner", "CL", "21:15:03 ").text,
                         "21:15:03 [owner] CL » hi")


class Harnesses(TempCase):
    def test_typed_events_keep_multiline_bodies_while_legacy_rows_stay_single_line(self):
        path = self.dir / "multiline.codex.jsonl"
        write_jsonl(path, [command("printf fixture", "first line\nsecond line", 0, "c1")])
        view = self.reader().read("k", "t", [plan(path)])
        output_rows = [row for row in view.rows if "first line" in row.text]
        output_events = [event for event in view.events if "first line" in event.text]
        self.assertEqual(len(output_rows), 1)
        self.assertIn("(+1줄)", output_rows[0].text)
        self.assertEqual(len(output_events), 1)
        self.assertEqual(output_events[0].kind, "output")
        self.assertIn("first line\nsecond line", output_events[0].text)

    def test_user_and_assistant_are_told_apart_for_codex(self):
        path = self.dir / "a.codex.jsonl"
        write_jsonl(path, [message("what now?", "u1", user=True), message("do this", "m1")])
        view = self.reader().read("k", "t", [plan(path)])
        self.assertEqual(self.rows(view), ["[test] CX « what now?", "[test] CX » do this"])

    def test_user_and_assistant_are_told_apart_for_a_claude_transcript(self):
        path = self.dir / "a.claude.jsonl"
        write_jsonl(path, [{"type": "user", "message": {"role": "user", "content": "question"}},
                           {"type": "assistant", "message": {"role": "assistant",
                                                              "content": [{"type": "text", "text": "answer"}]}}])
        view = self.reader().read("k", "t", [plan(path, "claude")])
        self.assertEqual(self.rows(view), ["[test] CL « question", "[test] CL » answer"])

    def opencode_rows(self, generation):
        if generation == "v1":
            return [{"rowid": 1, "message": "m1", "role": "user", "data": {"type": "text", "text": "question"}},
                    {"rowid": 2, "message": "m2", "role": "assistant", "data": {"type": "text", "text": "answer"}}]
        return [{"seq": 1, "type": "user", "data": {"text": "question"}},
                {"seq": 2, "type": "assistant", "data": {"time": {"completed": 1},
                                                          "content": [{"type": "text", "text": "answer"}]}}]

    def database(self, generation, rows=None, name="oc.db"):
        db = self.dir / name
        ef.write_opencode_db(db, "ses_boundedview", generation, rows or self.opencode_rows(generation))
        return db

    def oc_reader(self, db):
        source = VIEW.Source(db, "opencode", "opencode-db", "ses_boundedview")
        return self.reader(resolver=lambda attempt: ([source], None))

    def test_user_and_assistant_are_told_apart_for_opencode_in_both_generations(self):
        for generation in ("v1", "v2"):
            with self.subTest(generation=generation):
                view = self.oc_reader(self.database(generation, name=generation + ".db")).read(
                    "k", "t", [SourcePlan("att-oc", "owner", "opencode", "OC", "owner")])
                self.assertEqual(self.rows(view), ["[owner] OC « question", "[owner] OC » answer"])

    def test_an_unknown_record_stays_visible_as_a_row(self):
        path = self.dir / "a.codex.jsonl"
        write_jsonl(path, [{"type": "item.completed", "item": {"id": "z", "type": "mystery_widget", "x": 1}}])
        rows = self.rows(self.reader().read("k", "t", [plan(path)]))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith("[test] CX · item.completed "))
        self.assertIn("mystery_widget", rows[0])

    def test_secrets_are_masked_by_the_shared_formatter(self):
        path = self.dir / "a.codex.jsonl"
        write_jsonl(path, [message("see https://user:hunter2@example.test/x and Bearer sk-abcdef123", "m1")])
        row = self.rows(self.reader().read("k", "t", [plan(path)]))[0]
        self.assertNotIn("hunter2", row)
        self.assertNotIn("sk-abcdef123", row)
        self.assertIn("[redacted]", row)

    def test_a_codex_command_without_a_shown_start_still_shows_its_command(self):
        path = self.dir / "a.codex.jsonl"
        write_jsonl(path, [command("ls -la", "total 0\nfile", 0, "c1")])
        self.assertEqual(self.rows(self.reader().read("k", "t", [plan(path)])),
                         ["[test] CX $ ls -la", "[test] CX > total 0 (+1줄)", "[test] CX exit 0"])


class SourceHealth(TempCase):
    def test_a_cleaned_claude_owner_is_a_supervisor_only_record_not_an_error(self):
        log = self.dir / "owner.att-x.claude.jsonl"
        write_jsonl(log, ef.supervisor_rows("att-x", "sid-1", str(self.dir)))
        view = self.reader().read("k", "t", [plan(log, "claude", "owner", "att-x")])
        self.assertEqual(view.notes, (SUPERVISOR_ONLY,))
        self.assertEqual(SUPERVISOR_ONLY, "감독 기록만 남음 · 원천 정리됨")
        self.assertEqual(view.rows, ())
        self.assertEqual(view.health.state, "success")

    def test_a_missing_source_is_cleaned_and_an_unreadable_one_is_not(self):
        reader = self.reader()
        gone = reader.read("k1", "t", [plan(self.dir / "gone.codex.jsonl")])
        self.assertEqual(gone.notes, (CLEANED,))
        self.assertEqual(gone.health.state, "success")
        blocked = self.dir / "blocked.codex.jsonl"
        blocked.mkdir()
        view = reader.read("k2", "t", [plan(blocked)])
        self.assertEqual(len(view.notes), 1)
        self.assertTrue(view.notes[0].startswith("읽지 못함 · "), view.notes)
        self.assertNotIn("정리", view.notes[0])
        self.assertEqual(view.health.state, "failed")

    def test_permission_denied_is_an_unreadable_source_not_a_cleaned_one(self):
        path = self.dir / "p.codex.jsonl"
        write_jsonl(path, [message("x", "m1")])
        with mock.patch.object(Path, "stat", side_effect=PermissionError("no")):
            view = self.reader().read("k", "t", [plan(path)])
        self.assertEqual(view.notes, ("읽지 못함 · PermissionError",))
        self.assertEqual(view.health.error, "PermissionError")

    def test_a_locked_database_and_an_ambiguous_schema_are_unreadable(self):
        db = self.dir / "oc.db"
        ef.write_opencode_db(db, "ses_boundedview", "v1", [{"rowid": 1, "message": "m", "role": "user",
                                                            "data": {"type": "text", "text": "q"}}])
        source = VIEW.Source(db, "opencode", "opencode-db", "ses_boundedview")
        reader = self.reader(resolver=lambda attempt: ([source], None))
        owner = SourcePlan("att-oc", "owner", "opencode", "OC", "owner")
        locked = {"state": "unknown", "reason": "database-read-failed", "parts": []}
        with mock.patch("opencode_runtime.read_session_parts", return_value=locked):
            view = reader.read("k", "t", [owner])
        self.assertEqual(view.notes, ("읽지 못함 · database-read-failed",))
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE session_message (session_id TEXT, type TEXT, seq INTEGER, data TEXT)")
            con.execute("INSERT INTO session_message VALUES ('ses_boundedview', 'user', 1, '{}')")
        view = self.reader(resolver=lambda attempt: ([source], None)).read("k", "t", [owner])
        self.assertEqual(view.notes, ("읽지 못함 · session-generation-ambiguous",))

    def test_a_database_that_is_gone_is_cleaned(self):
        source = VIEW.Source(self.dir / "gone.db", "opencode", "opencode-db", "ses_boundedview")
        view = self.reader(resolver=lambda attempt: ([source], None)).read(
            "k", "t", [SourcePlan("att-oc", "owner", "opencode", "OC", "owner")])
        self.assertEqual(view.notes, (CLEANED,))

    def test_last_success_moves_only_on_a_successful_read_and_the_rows_stay(self):
        path = self.dir / "a.codex.jsonl"
        write_jsonl(path, [message("kept", "m1")])
        clock = [1000.0]
        reader = self.reader(clock=lambda: clock[0])
        first = reader.read("k", "t", [plan(path)])
        self.assertEqual((first.health.state, first.health.last_success_at), ("success", 1000.0))
        clock[0] = 1010.0
        with mock.patch.object(Path, "stat", side_effect=PermissionError("no")):
            failed = reader.read("k", "t", [plan(path)])
        self.assertEqual((failed.health.state, failed.health.last_success_at), ("stale", 1000.0))
        self.assertEqual(self.rows(failed), ["[test] CX » kept"])
        clock[0] = 1020.0
        again = reader.read("k", "t", [plan(path)])
        self.assertEqual((again.health.state, again.health.last_success_at), ("success", 1020.0))

    def test_no_success_yet_is_failed_not_a_made_up_age(self):
        blocked = self.dir / "blocked.codex.jsonl"
        blocked.mkdir()
        view = self.reader().read("k", "t", [plan(blocked)])
        self.assertEqual((view.health.state, view.health.last_success_at), ("failed", None))

    def test_several_sources_name_the_one_that_has_the_note(self):
        good, blocked = self.dir / "a.codex.jsonl", self.dir / "b.codex.jsonl"
        write_jsonl(good, [message("fine", "m1")])
        blocked.mkdir()
        view = self.reader().read("k", "t", [plan(good, label="plan", attempt="a1"),
                                              plan(blocked, label="test", attempt="a2")], stamped=True)
        self.assertEqual(len(view.notes), 1)
        self.assertTrue(view.notes[0].startswith("[test] 읽지 못함"))
        self.assertEqual(view.health.state, "partial")
        self.assertEqual(len(view.rows), 1)


class Budget(TempCase):
    def lines(self, count, start=0, pad=""):
        return [message(f"m{start + i}{pad}", f"id{start + i}", at=f"2026-10-20T12:{(start + i) // 60 % 60:02d}:{(start + i) % 60:02d}.000Z")
                for i in range(count)]

    def test_the_first_read_is_the_last_200_lines(self):
        path = self.dir / "a.codex.jsonl"
        write_jsonl(path, self.lines(1000))
        reader = self.reader()
        view = reader.read("k", "t", [plan(path)])
        self.assertEqual(len(view.rows), TAIL_RECORDS)
        self.assertEqual(view.rows[0].text, "[test] CX » m800")
        self.assertEqual(view.rows[-1].text, "[test] CX » m999")
        self.assertTrue(view.more)
        self.assertEqual(reader.reads, 1)

    def test_the_first_read_is_also_bounded_by_64_kib(self):
        path = self.dir / "a.codex.jsonl"
        write_jsonl(path, self.lines(100, pad="y" * 2000))
        view = self.reader().read("k", "t", [plan(path)])
        self.assertTrue(20 <= len(view.rows) <= 33, len(view.rows))
        self.assertTrue(view.rows[-1].text.startswith("[test] CX » m99"))
        self.assertTrue(view.more)

    def test_each_later_read_takes_up_to_200_new_records_and_continues_from_the_cursor(self):
        path = self.dir / "a.codex.jsonl"
        write_jsonl(path, self.lines(10))
        reader = self.reader()
        reader.read("k", "t", [plan(path)])
        with path.open("a") as handle:
            for row in self.lines(450, start=10):
                handle.write(json.dumps(row) + "\n")
        sizes = [len(reader.read("k", "t", [plan(path)]).rows) for _ in range(3)]
        self.assertEqual(sizes, [210, 410, 460])
        self.assertEqual(len(reader.read("k", "t", [plan(path)]).rows), 460)

    def test_a_record_larger_than_the_budget_is_read_whole(self):
        path = self.dir / "a.codex.jsonl"
        write_jsonl(path, [message("first", "m1"), message("z" * 300_000 + "\nsecond line", "m2")])
        reader = self.reader()
        view = reader.read("k", "t", [plan(path)])
        self.assertEqual(len(view.rows), 1)                # the earlier small record is outside the budget
        self.assertEqual(view.rows[0].omitted_lines, 1)
        self.assertTrue(len(view.rows[0].text) == len("[test] CX » ") + 300_000)
        self.assertTrue(view.more)
        older = reader.read("k", "t", [plan(path)], earlier=True)
        first, huge = self.rows(older)
        self.assertEqual(first, "[test] CX » first")
        self.assertTrue(huge.startswith("[test] CX » zzzz") and huge.endswith(" (+1줄)…"))

    def test_older_records_are_read_only_when_asked(self):
        path = self.dir / "a.codex.jsonl"
        write_jsonl(path, self.lines(1000))
        reader = self.reader()
        reader.read("k", "t", [plan(path)])
        for _ in range(3):
            reader.read("k", "t", [plan(path)])
        self.assertEqual(reader.reads, 4)
        sources = reader.scopes["k"].slots["att-fixture"].sources
        self.assertEqual(sources[0].cursor.start > 0, True)
        before = sources[0].cursor.start
        view = reader.read("k", "t", [plan(path)], earlier=True)
        self.assertEqual(len(view.rows), 400)
        self.assertEqual(view.rows[0].text, "[test] CX » m600")
        self.assertLess(sources[0].cursor.start, before)
        for _ in range(3):
            view = reader.read("k", "t", [plan(path)], earlier=True)
        self.assertEqual(len(view.rows), 1000)
        self.assertEqual(view.rows[0].text, "[test] CX » m0")
        self.assertFalse(view.more)

    def test_the_older_rows_come_first_after_an_earlier_read(self):
        path = self.dir / "a.codex.jsonl"
        write_jsonl(path, self.lines(300))
        reader = self.reader()
        reader.read("k", "t", [plan(path)])
        rows = [row.text for row in reader.read("k", "t", [plan(path)], earlier=True).rows]
        self.assertEqual(rows, [f"[test] CX » m{i}" for i in range(300)])

    def test_a_truncated_or_replaced_file_is_read_again_and_says_so(self):
        path = self.dir / "a.codex.jsonl"
        write_jsonl(path, self.lines(5))
        reader = self.reader()
        reader.read("k", "t", [plan(path)])
        write_jsonl(path, [message("replacement", "n1")])
        view = reader.read("k", "t", [plan(path)])
        self.assertEqual(self.rows(view), ["[test] CX » replacement"])
        self.assertEqual(view.notes, (RESET_NOTE,))

    def test_end_of_file_adds_nothing_and_a_repeated_message_is_not_shown_twice(self):
        path = self.dir / "a.codex.jsonl"
        write_jsonl(path, [message("once", "m1")])
        reader = self.reader()
        self.assertEqual(len(reader.read("k", "t", [plan(path)]).rows), 1)
        self.assertEqual(len(reader.read("k", "t", [plan(path)]).rows), 1)
        with path.open("a") as handle:
            handle.write(json.dumps(message("once", "m1")) + "\n")
        self.assertEqual(len(reader.read("k", "t", [plan(path)]).rows), 1)

    def test_a_half_written_last_line_waits_for_its_newline(self):
        path = self.dir / "a.codex.jsonl"
        full = json.dumps(message("done", "m1"), ensure_ascii=False)
        path.write_text(full[:20], encoding="utf-8")
        reader = self.reader()
        self.assertEqual(reader.read("k", "t", [plan(path)]).rows, ())
        with path.open("a") as handle:
            handle.write(full[20:] + "\n")
        self.assertEqual(self.rows(reader.read("k", "t", [plan(path)])), ["[test] CX » done"])


class Streaming(TempCase):
    def setUp(self):
        super().setUp()
        self.db = self.dir / "oc.db"
        ef.write_opencode_db(self.db, "ses_boundedview", "v1", [
            {"rowid": 1, "message": "m1", "role": "assistant", "data": {"type": "text", "text": "start"}},
            {"rowid": 2, "message": "m2", "role": "assistant", "data": {"type": "text", "text": "grow"}}])
        self.source = VIEW.Source(self.db, "opencode", "opencode-db", "ses_boundedview")
        self.reader_ = self.reader_factory()
        self.owner = SourcePlan("att-oc", "owner", "opencode", "OC", "owner")

    def reader_factory(self):
        return ef.output_reader(resolver=lambda attempt: ([self.source], None))

    def test_a_row_that_changes_in_place_is_replaced_not_added(self):
        first = self.reader_.read("k", "t", [self.owner])
        self.assertEqual(self.rows(first), ["[owner] OC » start", "[owner] OC » grow"])
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE part SET data=? WHERE rowid=2", (json.dumps({"type": "text", "text": "grown"}),))
        second = self.reader_.read("k", "t", [self.owner])
        self.assertEqual(self.rows(second), ["[owner] OC » start", "[owner] OC » grown"])
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO part (rowid, session_id, message_id, data) VALUES (3, 'ses_boundedview', 'm2', ?)",
                        (json.dumps({"type": "text", "text": "next"}),))
        third = self.reader_.read("k", "t", [self.owner])
        self.assertEqual(self.rows(third), ["[owner] OC » start", "[owner] OC » grown", "[owner] OC » next"])

    def test_a_session_that_moves_to_the_other_generation_is_read_again(self):
        self.reader_.read("k", "t", [self.owner])
        os.unlink(self.db)
        ef.write_opencode_db(self.db, "ses_boundedview", "v2", [
            {"seq": 1, "type": "assistant", "data": {"time": {"completed": 1},
                                                     "content": [{"type": "text", "text": "new world"}]}}])
        view = self.reader_.read("k", "t", [self.owner])
        self.assertEqual(self.rows(view), ["[owner] OC » new world"])
        self.assertEqual(view.notes, (RESET_NOTE,))


if __name__ == "__main__":
    unittest.main()
