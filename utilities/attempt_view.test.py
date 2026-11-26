#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import sys
import unittest
import os
import json
import re
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import attempt_view as view


class AttemptViewTests(unittest.TestCase):
    def test_sanitizes_terminal_controls_and_credentials(self):
        value = "a\x1b]52;c;secret\x07 https://name:pass@example.test Bearer xyz\x1b[31m!"
        clean = view.sanitize(value)
        self.assertNotIn("secret", clean)
        self.assertNotIn("name:pass", clean)
        self.assertNotIn("xyz", clean)
        self.assertNotIn("\x1b", clean)

    def test_c1_csi_sequence_is_removed_as_a_unit(self):
        self.assertEqual(view.sanitize("before\u009b31mred\u009b0mafter"), "beforeredafter")

    def test_claude_and_opencode_tool_inputs_render_real_lines(self):
        import contextlib
        import io
        command = "cat <<'EOF'\nhello\nEOF"
        fixtures = (
            ("claude", {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": command,
                 "note": "old\nnew", "count": 2}}]}}),
            ("opencode", {"type": "tool_use", "part": {"tool": "bash", "state": {
                "status": "running", "input": {"command": command, "note": "old\nnew", "count": 2}}}}),
        )
        for harness, record in fixtures:
            with self.subTest(harness=harness):
                lines = view.format_record(harness, record)
                self.assertEqual(lines[0].text.splitlines(), ["tool " + ("Bash" if harness == "claude" else "bash") + ":",
                                                               "cat <<'EOF'", "hello", "EOF"])
                rendered = "\n".join(line.text for line in lines)
                self.assertIn("note:\nold\nnew", rendered)
                for full in (False, True):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output): view._present(lines, full=full)
                    shown = output.getvalue()
                    self.assertIn("tool ", shown)
                    self.assertIn("cat <<'EOF'\nhello\nEOF", shown)
                    self.assertNotIn("\\nhello", shown)
                    self.assertIn("count: 2", shown)

    def test_unknown_and_raw_records_survive_as_one_line(self):
        self.assertEqual(len(view.format_record("codex", "not-json\nsecond")), 1)
        lines = view.format_record("claude", {"type": "new-event", "payload": "hello"})
        self.assertEqual(len(lines), 1)
        self.assertIn("new-event", lines[0].text)

    def test_codex_command_and_opencode_tool_keep_input_and_output(self):
        codex = view.format_record("codex", {"type": "command_execution", "command": "echo hi",
                                             "aggregated_output": "hi"})
        self.assertTrue(any("echo hi" in line.text for line in codex))
        tool = view.format_record("opencode", {"type": "tool", "name": "bash",
                                               "input": {"cmd": "pwd"}, "output": "here"})
        self.assertTrue(any("pwd" in line.text for line in tool))
        self.assertTrue(any("here" in line.text for line in tool))

    def test_fleet_import_is_not_part_of_viewer(self):
        self.assertFalse(any(name == "fleet" or name.startswith(("fleet.", "tools.fleet"))
                             for name in sys.modules))

    def test_claude_tool_result_shapes_and_empty_thinking(self):
        rows = view.format_record("claude", {"type":"assistant", "message":{"content":[
            {"type":"thinking", "thinking":""}, {"type":"thinking", "thinking":"reason"},
            {"type":"tool_use", "name":"read", "input":{"path":"a"}}]}})
        self.assertEqual(sum(line.kind == "thinking" for line in rows), 1)
        for content in ("plain", [{"type":"text", "text":"plain"}]):
            rows = view.format_record("claude", {"type":"user", "message":{"content":[
                {"type":"tool_result", "content":content}]}})
            self.assertTrue(any("plain" in line.text for line in rows))

    def test_codex_variants_and_malformed_envelopes(self):
        for record in ({"type":"command_execution", "command":"echo", "aggregated_output":"ok"},
                       {"type":"commandExecution", "command":"echo", "aggregatedOutput":"ok"},
                       {"type":"event_msg", "item":{"type":"commandExecution", "command":"echo", "aggregatedOutput":"ok"}}):
            rows = view.format_record("codex", record)
            self.assertTrue(any("echo" in line.text for line in rows))
        for value in ("not-json", "3", "[]", "{}", 7, ["x"]):
            self.assertTrue(view.format_record("claude", value))

    def test_opencode_run_json_part_envelopes(self):
        tool = view.format_record("opencode", {"type": "tool_use", "part": {
            "tool": "read", "callID": "c1", "state": {
                "status": "completed", "input": {"path": "a"}, "output": "one\ntwo"}}})
        self.assertEqual([line.kind for line in tool], ["tool", "tool", "output"])
        self.assertIn("path: a", tool[1].text)
        self.assertIn("one\ntwo", tool[2].text)
        receipt = view.format_record("opencode", {"type": "tool_use", "part": {
            "tool": "bash", "callID": "c2", "state": {"status": "running"}}})
        self.assertEqual(len(receipt), 1)
        self.assertIn("tool bash running", receipt[0].text)
        self.assertEqual(view.format_record("opencode", {"type": "step_start", "part": {}}), [])
        self.assertEqual(len(view.format_record("opencode", {"type": "step_finish", "part": {
            "reason": "done", "tokens": {"input": 2, "output": 3}}})), 1)
        self.assertEqual(view.format_record("opencode", {"type": "text", "part": {"text": "reply"}})[0].kind,
                         "message")
        self.assertEqual(view.format_record("opencode", {"type": "user", "part": {"text": "prompt"}})[0].kind,
                         "prompt")
        self.assertEqual(view.format_record("opencode", {"type": "reasoning", "part": {"text": "why"}})[0].kind,
                         "thinking")

    def test_opencode_v1_and_v2_rows_share_run_json_normalizer(self):
        v1 = {"cursor": 4, "role": "assistant", "data": {"type": "tool", "tool": "read",
              "callID": "c1", "state": {"status": "completed", "input": {"path": "a"},
              "output": "v1\noutput"}}}
        v2 = {"cursor": 5, "role": "assistant", "data": {"content": [{"type": "tool", "id": "c2",
              "name": "read", "state": {"status": "completed", "input": {"path": "b"},
              "content": [{"type": "text", "text": "v2\noutput"}]}}]}}
        normalized_v1 = view.normalize_opencode_db_row(v1, "v1")
        normalized_v2 = view.normalize_opencode_db_row(v2, "v2")
        self.assertEqual(normalized_v1[0]["type"], "tool_use")
        self.assertEqual(normalized_v2[0]["type"], "tool_use")
        for event, expected in ((normalized_v1[0], "v1\noutput"), (normalized_v2[0], "v2\noutput")):
            lines = view.format_record("opencode", event)
            self.assertEqual(lines[-1].kind, "output")
            self.assertIn(expected, lines[-1].text)

    def test_claude_hidden_attachment_and_rate_limit_shapes(self):
        for kind in ("last-prompt", "atis-latch", "mode", "api-request", "api-request-shape"):
            self.assertEqual(view.format_record("claude", {"type": kind, "private": "secret"}), [])
        self.assertEqual(view.format_record("claude", {"type": "assistant", "message": {
            "content": [{"type": "thinking", "thinking": ""}]}}), [])
        attachment = {"type": "attachment", "attachment": {"type": "image", "data": "private-email"}}
        self.assertEqual(view.format_record("claude", attachment), [view.Line("attachment", "attachment: image")])
        import contextlib, io
        output = io.StringIO()
        with contextlib.redirect_stdout(output): view._format_present("claude", [json.dumps(attachment)], {}, full=False)
        self.assertNotIn("private-email", output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output): view._format_present("claude", [json.dumps(attachment)], {}, full=True)
        self.assertIn("private-email", output.getvalue())
        rate = view.format_record("claude", {"type": "rate_limit_event", "status": "limited", "resets_at": 123})
        self.assertEqual(len(rate), 1)
        self.assertIn("limited", rate[0].text)
        self.assertIn("123", rate[0].text)

    def test_codex_known_event_shapes_never_fall_through_unknown(self):
        records = [
            {"type": "error", "message": "outer error"},
            {"type": "turn.failed", "message": "failed"},
            {"type": "item.completed", "item": {"type": "error", "message": "item error"}},
            {"type": "item.completed", "item": {"type": "file_change", "changes": [
                {"path": "src/a.py", "kind": "add"}]}},
            {"type": "webSearch", "query": "query text"},
            {"type": "item.completed", "item": {"type": "fileChange", "files": [
                {"new_path": "src/b.py", "status": "modify"}]}},
            {"type": "web_search", "search_query": "snake query"},
            {"type": "item.completed", "item": {"type": "reasoning", "id": "hidden-id"}},
            {"type": "item.completed", "item": {"type": "userMessage", "id": "u1"}},
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.started", "turn_id": "t1"},
            {"type": "turn.completed", "turn_id": "t1"},
        ]
        records += [{"type": "item.completed", "item": {"type": kind, "id": "id1", "detail": "x"}}
                    for kind in ("sleep", "contextCompaction", "imageView", "subAgentActivity",
                                 "collabAgentToolCall", "mcpToolCall")]
        for record in records:
            lines = view.format_record("codex", record)
            if record is records[7]:
                self.assertEqual(lines, [])  # Empty reasoning is deliberately hidden.
                continue
            self.assertTrue(lines, record)
            self.assertTrue(all(line.kind != "unknown" for line in lines), record)
        self.assertNotIn("hidden-id", " ".join(line.text for line in view.format_record("codex", records[7])))
        self.assertIn("src/a.py", " ".join(line.text for line in view.format_record("codex", records[3])))
        self.assertIn("query text", " ".join(line.text for line in view.format_record("codex", records[4])))
        self.assertIn("src/b.py", " ".join(line.text for line in view.format_record("codex", records[5])))
        self.assertIn("snake query", " ".join(line.text for line in view.format_record("codex", records[6])))

    def test_unknown_presentation_is_bounded_but_full_formatter_is_not(self):
        record = {"type": "future-event", "payload": "x" * 500}
        line = view.format_record("claude", record)[0]
        self.assertEqual(len(line.text), len("· future-event ") + len(json.dumps(record, ensure_ascii=False, separators=(",", ":"))))
        import contextlib, io
        output = io.StringIO()
        with contextlib.redirect_stdout(output): view._present([line])
        shown = output.getvalue().rstrip("\n")
        self.assertLessEqual(len(shown.split("… (+")[0]), 300)
        self.assertIn(f"(+{len(line.text) - 300}자 생략", shown)
        output = io.StringIO()
        with contextlib.redirect_stdout(output): view._present([line], full=True)
        self.assertIn("x" * 500, output.getvalue())

    def test_supervisor_banner_uses_parsed_envelope_type_only(self):
        self.assertFalse(view._has_session_envelope(['{"type":"message","text":"supervisor.session"}']))
        self.assertTrue(view._has_session_envelope(['{"type":"dispatch.supervisor.session-announced"}']))

    def test_codex_never_gets_cleaned_native_transcript_banner(self):
        import contextlib, io, tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "owner.jsonl"
            path.write_text('{"type":"message","text":"supervisor.session"}\n')
            source = view.Source(path, "codex")
            candidate = (Path(folder), ["2026", "done", "repo", folder, "owner", ""],
                         {"attempt_id": "att-codex", "harness": "codex"})
            output = io.StringIO()
            with mock.patch.object(view, "_attempt_prefix_matches", return_value=[candidate]), \
                 mock.patch.object(view, "resolve_sources", return_value=([source], None)), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(view.main(["att-codex"]), 0)
            self.assertNotIn("감독 기록만 남음", output.getvalue())

    def test_control_sequence_and_secret_matrix(self):
        for value in ("\x1b]8;;https://a\x07link\x1b]8;;\x07", "\x1b]52;c;secret\x1b\\",
                      "\x1bPsecret\x1b\\", "\x9d52;c;secret\x9c", "\x90x\x9c",
                      "\x1b]unterminated", "\x1b[31unterminated", "\x00\r\x7f"):
            clean = view.sanitize(value)
            self.assertFalse(re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", clean))
        for value in (
            "https://name:secret@example.test/path",
            "https://token@example.test/path",
            "ssh://token@example.test/path",
            "git+https://name:secret@example.test/path",
            "contact person@example.test",
            "aUtHoRiZaTiOn: bEaReR abc123",
            "https://token-se\x1b[31mcret@example.test/path",
        ):
            clean = view.sanitize(value)
            self.assertNotRegex(clean, r"(?i)Bearer\s+(?!\[redacted\])[^\s]+")
            self.assertNotIn("name:secret", clean)
            self.assertNotIn("token@", clean)
            self.assertNotIn("abc123", clean)
            self.assertNotIn("token-secret", clean)
        self.assertIn("https://[redacted]@example.test/path", view.sanitize("https://token@example.test/path"))
        self.assertIn("ssh://[redacted]@example.test/path", view.sanitize("ssh://token@example.test/path"))
        self.assertIn("git+https://[redacted]@example.test/path", view.sanitize("git+https://name:secret@example.test/path"))
        self.assertIn("person@example.test", view.sanitize("contact person@example.test"))
        self.assertIn("bEaReR [redacted]", view.sanitize("bEaReR abc123"))
        self.assertIn("https://[redacted]@example.test/path", view.sanitize("https://token-se\x1b[31mcret@example.test/path"))

    def test_full_presentation_still_sanitizes_and_preserves_assistant_prose(self):
        lines = view.format_record("claude", {"type":"assistant", "message":{"content":[
            {"type":"text", "text":"prose " + "x" * 5000 + " Bearer secret"}]}})
        self.assertTrue(any("x" * 5000 in line.text for line in lines))
        self.assertTrue(all("secret" not in line.text for line in lines))

    def test_cursor_buffers_split_utf8_and_notices_replacement(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log"
            path.write_bytes("한".encode()[:2])
            cursor = view.Cursor(path)
            self.assertEqual(cursor.read_new(), [])
            with path.open("ab") as stream: stream.write("한\n".encode()[2:])
            self.assertEqual(cursor.read_new(), ["한"])
            path.write_bytes(b"next\n")
            self.assertEqual(cursor.read_new(), ["next"])
            self.assertTrue(cursor.notices)

    def test_present_omission_counts_and_full_keeps_masking(self):
        import contextlib
        import io
        rows = [view.Line("output", "\n".join(str(i) for i in range(40))),
                view.Line("message", "assistant: " + "a" * 3000 + " Bearer token")]
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured): view._present(rows, full=False)
        self.assertIn("(+16줄 생략", captured.getvalue())
        self.assertIn("a" * 3000, captured.getvalue())
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured): view._present(rows, full=True)
        self.assertIn("39", captured.getvalue())
        self.assertNotIn("Bearer token", captured.getvalue())

    def test_session_selection_zero_one_many(self):
        from unittest import mock
        import contextlib
        import io
        def candidate(identifier):
            return (Path("/tmp"), ["2026", "open", "codex", "/tmp", "owner", ""],
                    {"attempt_id":identifier, "worker_type":"owner", "harness":"codex"})
        with mock.patch.object(view, "_owner_candidates", return_value=[]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out): self.assertEqual(view.main(["--session", "sid"]), 0)
            self.assertIn("No owner attempts", out.getvalue())
        with mock.patch.object(view, "_owner_candidates", return_value=[candidate("att-one")]), \
             mock.patch.object(view, "_attempt_prefix_matches", return_value=[candidate("att-one")]), \
             mock.patch.object(view, "resolve_sources", return_value=([], "attempt not found")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err): self.assertEqual(view.main(["--session", "sid"]), 2)
            self.assertIn("attempt not found", err.getvalue())
        with mock.patch.object(view, "_owner_candidates", return_value=[candidate("att-one"), candidate("att-two")]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out): self.assertEqual(view.main(["--session", "sid"]), 0)
            self.assertIn("att-one", out.getvalue())
            self.assertIn("att-two", out.getvalue())

    def test_opencode_v2_state_content_and_pending_refresh_deduplicate(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "opencode.db"
            sid = "ses_viewv2"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE session_message (session_id TEXT, type TEXT, seq INTEGER, data TEXT)")
                con.execute("INSERT INTO session_message VALUES (?, 'assistant', 1, ?)",
                            (sid, json.dumps({"content":[{"type":"tool", "name":"bash", "state":{
                                "status":"running", "input":{"cmd":"pwd"}, "content":[]}}]})))
            source = view.Source(db, "opencode", "opencode-db", sid)
            state = {"cursor":0,"refresh":[],"rows":{},"entities":{}}
            first = view._follow_db(source,state)
            self.assertTrue(any("pwd" in line.text for line in first))
            with sqlite3.connect(db) as con:
                con.execute("UPDATE session_message SET data=? WHERE seq=1", (json.dumps({"time":{"completed":1},"content":[
                    {"type":"tool", "name":"bash", "state":{"status":"completed", "input":{"cmd":"pwd"},
                     "content":[{"type":"text", "text":"done"}]}}]}),))
            second = view._follow_db(source,state)
            joined = "\n".join(line.text for line in second)
            self.assertIn("completed", joined)
            self.assertIn("done", joined)
            self.assertNotIn('cmd', joined)
            self.assertEqual(view._follow_db(source,state), [])

    def test_codex_stateful_commands_messages_envelopes_and_prompts(self):
        state = {}
        rows = [json.dumps({"type":"item.started", "item":{"id":"c1", "type":"command_execution", "command":"echo hi"}}),
                json.dumps({"type":"item.completed", "item":{"id":"c1", "type":"command_execution", "command":"echo hi", "aggregated_output":"hi", "exit_code":0}}),
                json.dumps({"type":"item.started", "item":{"id":"c2", "type":"command_execution", "command":"still running"}}),
                json.dumps({"type":"item.completed", "item":{"id":"m1", "type":"agent_message", "text":"said once"}}),
                json.dumps({"type":"item.completed", "item":{"id":"m1", "type":"agent_message", "text":"said once"}})]
        import contextlib, io
        output = io.StringIO()
        with contextlib.redirect_stdout(output): view._format_present("codex", rows, state)
        displayed = output.getvalue()
        self.assertEqual(displayed.count("echo hi"), 1)
        self.assertEqual(displayed.count("still running"), 1)
        self.assertEqual(displayed.count("said once"), 1)
        self.assertIn("hi", displayed)
        for typ in ("dispatch.supervisor.turn-started", "supervisor.turn.started",
                    "dispatch.supervisor.session-announced", "dispatch.supervisor.parked",
                    "dispatch.supervisor.resumed", "dispatch.supervisor.join", "dispatch.supervisor.terminal",
                    "dispatch.supervisor.future-event"):
            result = view.format_record("codex", {"type": typ, "private":"payload"})
            self.assertEqual(len(result), 1)
            self.assertNotIn("payload", result[0].text)
            self.assertEqual(result[0].kind, "supervisor")
        self.assertEqual(view.format_record("codex", {"type":"dispatch.supervisor.token_usage", "x":1}), [])
        for content in ("user prompt", [{"type":"text", "text":"user prompt"}]):
            self.assertTrue(any(row.kind == "prompt" for row in view.format_record(
                "claude", {"type":"user", "message":{"content":content}})))

    def test_a_completed_command_without_a_shown_start_keeps_its_command(self):
        state = {}
        completed = {"type": "item.completed", "item": {"id": "c9", "type": "command_execution",
                                                       "command": "ls -la", "aggregated_output": "total 0", "exit_code": 0}}
        lines = view.format_stream("codex", [json.dumps(completed)], state)
        self.assertEqual([line.text for line in lines], ["command: ls -la", "output: total 0", "exit: 0"])
        started = {"type": "item.started", "item": {"id": "c10", "type": "command_execution", "command": "pwd"}}
        done = {"type": "item.completed", "item": {"id": "c10", "type": "command_execution", "command": "pwd",
                                                  "aggregated_output": "/", "exit_code": 0}}
        lines = view.format_stream("codex", [json.dumps(started), json.dumps(done)], state)
        self.assertEqual([line.text for line in lines], ["command: pwd", "output: /", "exit: 0"])

    def test_sanitize_is_linear_on_one_very_long_run_of_letters(self):
        import time
        started = time.monotonic()
        clean = view.sanitize("z" * 400_000 + " https://u:p@host/x")
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(clean.endswith("https://[redacted]@host/x"))
        self.assertEqual(view.sanitize("see https://u:p@host/x"), "see https://[redacted]@host/x")

    def test_public_read_new_reuses_cursor_incrementally(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "att.log"
            path.write_text('{"type":"first"}\n')
            source, cursor = view.Source(path, "codex"), view.Cursor(path)
            first, cursor = view.read_new(source, cursor)
            self.assertEqual(first, ['{"type":"first"}'])
            with path.open("a") as stream: stream.write('{"type":"second"}\n')
            second, cursor = view.read_new(source, cursor)
            self.assertEqual(second, ['{"type":"second"}'])

    def test_registry_reader_substring_filters_and_throttles_changed_snapshot(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "jobs.log"
            path.write_text("attempt_id=one\nattempt_id=two\n")
            reader = view.RegistryReader(path)
            self.assertEqual(reader.read("two", now=2), ["attempt_id=two"])
            path.write_text("attempt_id=one\nattempt_id=two\nattempt_id=three\n")
            # A detected change inside the one-second budget keeps the last snapshot.
            self.assertEqual(reader.read("three", now=2.5), [])
            self.assertEqual(reader.read("three", now=3.1), ["attempt_id=three"])

    def test_registry_cli_selection_validation_and_read_only(self):
        import tempfile, contextlib, io
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "dispatch"
            logs = root / "logs"; logs.mkdir(parents=True)
            attempt = "att-sample"
            log = logs / f"x.{attempt}.codex.jsonl"
            log.write_text('{"type":"item.completed","item":{"id":"m","type":"agent_message","text":"hello"}}\n')
            registry = root / "jobs.log"
            def row(identifier, path, status="done", extra=""):
                return f"2026\t{status}\trepo\t/tmp\towner\tattempt_id={identifier},harness=codex,worker_type=owner,log_file={path}{extra}\n"
            registry.write_text(row(attempt, log))
            with mock.patch.object(view, "_dispatch_roots", return_value=(root,)):
                snapshot = {str(p.relative_to(folder)): p.read_bytes() for p in Path(folder).rglob("*") if p.is_file()}
                out = io.StringIO()
                with contextlib.redirect_stdout(out): self.assertEqual(view.main([attempt]), 0)
                self.assertIn("hello", out.getvalue())
                self.assertEqual(snapshot, {str(p.relative_to(folder)): p.read_bytes() for p in Path(folder).rglob("*") if p.is_file()})
                err = io.StringIO()
                with contextlib.redirect_stderr(err): self.assertEqual(view.main(["att-absent"]), 2)
                self.assertIn("Unknown attempt", err.getvalue())
                # Exact path carrying a registered attempt ID but different cwd/path is refused.
                mismatched = Path(folder) / f"other.att-{attempt}.codex.jsonl"
                mismatched.write_text("{}\n")
                err = io.StringIO()
                with contextlib.redirect_stderr(err): self.assertEqual(view.main([str(mismatched)]), 2)
                self.assertIn("does not match", err.getvalue())
                registry.write_text(row("att-sample-one", log) + row("att-sample-two", log))
                view._REGISTRY_READERS.clear()
                err = io.StringIO()
                with contextlib.redirect_stderr(err): self.assertEqual(view.main(["att-sample"]), 2)
                self.assertIn("Ambiguous", err.getvalue())
                registry.write_text(row(attempt, log))
                view._REGISTRY_READERS.clear()
                # Exact registry-bound log path is accepted; a same-name symlink elsewhere is refused.
                out = io.StringIO()
                with contextlib.redirect_stdout(out): self.assertEqual(view.main([str(log)]), 0)
                outside = Path(folder) / log.name; outside.symlink_to(log)
                err = io.StringIO()
                with contextlib.redirect_stderr(err): self.assertEqual(view.main([str(outside)]), 2)
                self.assertIn("does not match", err.getvalue())

    def test_cleaned_native_source_leaves_supervisor_record_notice(self):
        import tempfile, contextlib, io
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "dispatch"; logs = root / "logs"; logs.mkdir(parents=True)
            attempt = "att-cleaned"
            log = logs / f"x.{attempt}.claude.jsonl"
            log.write_text(json.dumps({"type":"dispatch.supervisor.session-announced", "session_id":"sid"})+"\n")
            (root / "jobs.log").write_text(
                f"2026\tdone\trepo\t/tmp\towner\tattempt_id={attempt},harness=claude,worker_type=owner,log_file={log}\n")
            output = io.StringIO()
            with mock.patch.object(view, "_dispatch_roots", return_value=(root,)), contextlib.redirect_stdout(output):
                self.assertEqual(view.main([attempt]), 0)
            self.assertIn("감독 기록만 남음", output.getvalue())

    def test_announced_source_deduplicates_sid_and_resolves_multiple_sessions(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); log = root / "owner.jsonl"
            log.write_text("\n".join(json.dumps({"type":"dispatch.supervisor.session-announced",
                "parent_attempt_id":"att-owner", "session_id":sid, "cwd":"/work"})
                for sid in ("sid-one", "sid-one", "sid-two")) + "\n")
            self.assertEqual(view._announced(str(log), "att-owner"),
                             [("sid-one", "/work"), ("sid-two", "/work")])
            paths = {sid: root / f"{sid}.jsonl" for sid in ("sid-one", "sid-two")}
            for path in paths.values(): path.touch()
            row = (root, ["2026", "done", "repo", "/work", "owner", ""],
                   {"attempt_id":"att-owner", "harness":"claude", "log_file":str(log),
                    "runtime_home":str(root), "cwd":"/work"})
            with mock.patch.object(view, "_find_attempt", return_value=row), \
                 mock.patch.object(view, "_log_path", return_value=str(log)), \
                 mock.patch("claude_project_dir.resolve_session_transcript",
                            side_effect=lambda sid, cwd, **kw: (paths[sid], None)) as resolver:
                sources, error = view.resolve_sources("att-owner")
            self.assertIsNone(error)
            self.assertEqual([s.session_id for s in sources[1:]], ["sid-one", "sid-two"])
            self.assertTrue(all(call.kwargs["expected_cwd"] == "/work" for call in resolver.call_args_list))

    def test_follow_owner_lines_children_final_drain_and_single_end_marker(self):
        import tempfile, contextlib, io, time
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "dispatch"; logs = root / "logs"; logs.mkdir(parents=True)
            owner_id, child_id = "att-owner", "att-child"
            owner = logs / f"x.{owner_id}.codex.jsonl"
            child = logs / f"x.{child_id}.codex.jsonl"
            owner.write_text('{"type":"session-start"}\n')
            child.write_text('{"type":"item.completed","item":{"id":"c","type":"agent_message","text":"child tail"}}\n')
            registry = root / "jobs.log"
            def line(attempt, status, path, extra=""):
                return f"2026\t{status}\trepo\t/tmp\towner\tattempt_id={attempt},harness=codex,worker_type={'owner' if attempt == owner_id else 'stage'},log_file={path}{extra}\n"
            def save(status):
                registry.write_text(line(owner_id, status, owner) +
                    line(child_id, "done", child, f",parent_attempt_id={owner_id},route_node=execute"))
            save("open")
            sleeps = []
            def tick(_):
                if sleeps: return
                sleeps.append(1)
                with owner.open("a") as stream:
                    stream.write(json.dumps({"type":"item.completed", "item":{"id":"m","type":"agent_message","text":"owner follow line"}})+"\n")
                    stream.write('{"type":"result","subtype":"success"}\n')
                save("done")
                time.sleep(1.01) # permit the changed registry snapshot's one-second refresh budget
            output = io.StringIO()
            with mock.patch.object(view, "_dispatch_roots", return_value=(root,)), \
                 contextlib.redirect_stdout(output):
                result = view.main([owner_id, "--follow", "--poll-interval", "0"] , clock=lambda:0, sleep=tick)
            text = output.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("owner follow line", text)
            self.assertIn("[execute]", text)
            self.assertIn("child tail", text)
            self.assertEqual(text.count("[끝]"), 1)
            self.assertNotIn("종료 기록 없음", text)

    def test_follow_final_refresh_discovers_child_registered_during_close_check(self):
        import tempfile, contextlib, io
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "dispatch"; logs = root / "logs"; logs.mkdir(parents=True)
            owner_id, child_id = "att-owner-race", "att-child-race"
            owner = logs / f"x.{owner_id}.codex.jsonl"
            child = logs / f"x.{child_id}.codex.jsonl"
            owner.write_text('{"type":"result","subtype":"success"}\n')
            child.write_text('{"type":"item.completed","item":{"id":"late","type":"agent_message","text":"late child row"}}\n')
            registry = root / "jobs.log"
            def line(attempt, status, path, extra=""):
                worker = "owner" if attempt == owner_id else "stage"
                return f"2026\t{status}\trepo\t/tmp\towner\tattempt_id={attempt},harness=codex,worker_type={worker},log_file={path}{extra}\n"
            registry.write_text(line(owner_id, "open", owner))
            original_find_attempt = view._find_attempt
            calls = [0]
            def close_after_child_registration(attempt):
                calls[0] += 1
                if calls[0] == 3:
                    # The loop's child scan has used the old snapshot. Register
                    # the child immediately before the owner's terminal close
                    # decision; ordinary reads remain throttled at this instant.
                    registry.write_text(line(owner_id, "done", owner) +
                        line(child_id, "done", child,
                             f",parent_attempt_id={owner_id},route_node=execute"))
                    self.assertEqual(view._registry_rows(root, child_id), [])
                    return (root, ["2026", "done", "repo", "/tmp", "owner", ""],
                            {"failure_class":"none"})
                return original_find_attempt(attempt)
            slept = [False]
            def finish_owner(_):
                if not slept[0]:
                    slept[0] = True
                    with owner.open("a") as stream:
                        stream.write('{"type":"result","subtype":"success"}\n')
            output = io.StringIO()
            with (mock.patch.object(view, "_dispatch_roots", return_value=(root,)),
                  mock.patch.object(view, "_find_attempt", side_effect=close_after_child_registration),
                  contextlib.redirect_stdout(output)):
                result = view.main([owner_id, "--follow", "--poll-interval", "0"], clock=lambda:0, sleep=finish_owner)
            text = output.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("[execute] ── 시작 (codex) ──", text)
            self.assertIn("[execute] assistant: late child row", text)
            self.assertEqual(text.count("[끝]"), 1)
            self.assertLess(text.index("late child row"), text.index("[끝]"))

    def test_follow_grace_marks_missing_terminal_but_turn_completed_is_not_terminal(self):
        import tempfile, contextlib, io, time
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "dispatch"; logs = root / "logs"; logs.mkdir(parents=True)
            attempt = "att-grace"
            owner = logs / f"x.{attempt}.codex.jsonl"
            owner.write_text('{"type":"turn.completed"}\n')
            registry = root / "jobs.log"
            def save(status):
                registry.write_text(f"2026\t{status}\trepo\t/tmp\towner\tattempt_id={attempt},harness=codex,worker_type=owner,log_file={owner}\n")
            save("open")
            elapsed, closed = [0.0], [False]
            def tick(_):
                if not closed[0]:
                    save("done"); closed[0] = True
                elapsed[0] += 1.1
                time.sleep(1.01)
            output = io.StringIO()
            with mock.patch.object(view, "_dispatch_roots", return_value=(root,)), contextlib.redirect_stdout(output):
                self.assertEqual(view.main([attempt, "--follow", "--grace", "1", "--poll-interval", "0"],
                                           clock=lambda:elapsed[0], sleep=tick), 0)
            self.assertEqual(output.getvalue().count("[끝]"), 1)
            self.assertIn("종료 기록 없음", output.getvalue())

    def test_supervisor_unknown_inventory_counts_type_names(self):
        types = Counter()
        for name in ("dispatch.supervisor.future-a", "dispatch.supervisor.future-a",
                     "dispatch.supervisor.future-b"):
            lines = view.format_record("codex", {"type":name, "payload":"hidden from short line"})
            types[name] += len(lines)
        self.assertEqual(types.most_common(1), [("dispatch.supervisor.future-a", 2)])


class BoundedReadTests(unittest.TestCase):
    """The optional read budget: tail, forward, earlier, oversize, truncation, database rows."""

    def write(self, directory, rows, name="log.jsonl"):
        path = Path(directory) / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def test_default_read_new_is_unbounded_and_unchanged(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [{"n": i} for i in range(500)])
            cursor = view.Cursor(path)
            self.assertEqual(len(cursor.read_new()), 500)
            self.assertEqual(cursor.read_new(), [])

    def test_tail_starts_at_the_last_whole_records_and_follows_by_budget(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [{"n": i} for i in range(500)])
            cursor = view.Cursor(path)
            rows = cursor.read_new(max_bytes=1_000_000, max_records=200, tail=True)
            self.assertEqual([json.loads(r)["n"] for r in rows], list(range(300, 500)))
            self.assertEqual(cursor.read_new(max_bytes=1_000_000, max_records=200), [])
            with path.open("a") as handle:
                handle.write("".join(json.dumps({"n": i}) + "\n" for i in range(500, 530)))
            more = cursor.read_new(max_bytes=1_000_000, max_records=10)
            self.assertEqual([json.loads(r)["n"] for r in more], list(range(500, 510)))
            rest = cursor.read_new(max_bytes=1_000_000, max_records=200)
            self.assertEqual([json.loads(r)["n"] for r in rest], list(range(510, 530)))

    def test_byte_budget_cuts_on_a_record_boundary(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [{"n": i, "pad": "x" * 100} for i in range(100)])
            cursor = view.Cursor(path)
            rows = cursor.read_new(max_bytes=1000, tail=True)
            self.assertTrue(0 < len(rows) <= 9)
            self.assertEqual(json.loads(rows[-1])["n"], 99)
            self.assertTrue(all(json.loads(r)["pad"] == "x" * 100 for r in rows))

    def test_one_record_larger_than_the_budget_is_read_whole(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [{"n": 1}, {"n": 2, "big": "y" * 5000}])
            cursor = view.Cursor(path)
            rows = cursor.read_new(max_bytes=100, tail=True)
            self.assertEqual([json.loads(r)["n"] for r in rows], [2])
            self.assertEqual(len(json.loads(rows[0])["big"]), 5000)
            before = cursor.read_before(max_bytes=100)
            self.assertEqual([json.loads(text)["n"] for _, text in before], [1])
            self.assertEqual(cursor.read_before(max_bytes=100), [])

    def test_a_record_over_the_limit_never_leaks_its_tail_across_reads(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "big.jsonl"
            path.write_bytes(b"a" * (view.MAX_RECORD_BYTES + 200_000) + b"\nGOOD\n")
            cursor = view.Cursor(path)
            rows = []
            for _ in range(400):
                rows += cursor.read_new(max_bytes=65536)
                if cursor.offset >= path.stat().st_size and not cursor.pending:
                    break
            self.assertEqual(rows, ["GOOD"])
            self.assertEqual(sum("size limit" in note for note in cursor.notices), 1)
            self.assertFalse(cursor.discarding)

    def test_a_tail_cut_by_end_of_file_stays_dropped_after_the_next_append(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "big.jsonl"
            path.write_bytes(b"a" * (view.MAX_RECORD_BYTES + 10))
            cursor = view.Cursor(path)
            rows = []
            for _ in range(200):
                rows += cursor.read_new(max_bytes=65536)
            self.assertEqual(rows, [])
            self.assertTrue(cursor.discarding)
            with path.open("ab") as handle:
                handle.write(b"tail of the same record")
            self.assertEqual(cursor.read_new(max_bytes=65536), [])
            self.assertEqual(cursor.read_new(max_bytes=65536, final=True), [])
            with path.open("ab") as handle:
                handle.write(b" end\nGOOD\n")
            self.assertEqual(cursor.read_new(max_bytes=65536), ["GOOD"])
            self.assertFalse(cursor.discarding)

    def test_final_does_not_emit_a_tail_that_is_being_dropped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "big.jsonl"
            path.write_bytes(b"ok\n" + b"a" * (view.MAX_RECORD_BYTES + 10))
            cursor = view.Cursor(path)
            rows = []
            for _ in range(200):
                rows += cursor.read_new(max_bytes=65536)
            self.assertTrue(cursor.discarding)
            rows += cursor.read_new(max_bytes=65536, final=True)
            self.assertEqual(rows, ["ok"])

    def test_earlier_records_are_read_only_when_asked_and_in_order(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [{"n": i} for i in range(100)])
            cursor = view.Cursor(path)
            cursor.read_new(max_bytes=1_000_000, max_records=10, tail=True)
            first = cursor.read_before(max_bytes=10_000, max_records=30)
            self.assertEqual([json.loads(text)["n"] for _, text in first], list(range(60, 90)))
            second = cursor.read_before(max_bytes=10_000, max_records=1000)
            self.assertEqual([json.loads(text)["n"] for _, text in second], list(range(0, 60)))
            self.assertEqual(cursor.read_before(max_bytes=10_000), [])
            self.assertEqual(cursor.start, 0)

    def test_truncation_bumps_the_epoch_and_a_missing_file_reports_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [{"n": i} for i in range(5)])
            source = view.Source(path, "codex")
            cursor = view.Cursor(path)
            self.assertEqual(len(view.read_entries(source, cursor, max_records=200, tail=True).entries), 5)
            path.write_text(json.dumps({"n": "new"}) + "\n")
            result = view.read_entries(source, cursor, max_records=200)
            self.assertTrue(result.reset)
            self.assertEqual([json.loads(e.record)["n"] for e in result.entries], ["new"])
            path.unlink()
            gone = view.read_entries(source, cursor, max_records=200)
            self.assertEqual((gone.entries, gone.state), ([], "missing"))

    def make_db(self, directory, generation, count=6):
        import sqlite3
        db = Path(directory) / "opencode.db"
        sid = "ses_boundedview"
        with sqlite3.connect(db) as con:
            if generation == "v1":
                con.execute("CREATE TABLE message (id TEXT, session_id TEXT, role TEXT)")
                con.execute("CREATE TABLE part (session_id TEXT, message_id TEXT, data TEXT)")
                con.execute("INSERT INTO message VALUES ('m', ?, 'assistant')", (sid,))
                for index in range(1, count + 1):
                    con.execute("INSERT INTO part VALUES (?, 'm', ?)", (sid, json.dumps({"type": "text", "text": f"row{index}"})))
            else:
                con.execute("CREATE TABLE session_message (session_id TEXT, type TEXT, seq INTEGER, data TEXT)")
                for index in range(1, count + 1):
                    con.execute("INSERT INTO session_message VALUES (?, 'assistant', ?, ?)",
                                (sid, index, json.dumps({"time": {"completed": 1}, "content": [{"type": "text", "text": f"row{index}"}]})))
        return view.Source(db, "opencode", "opencode-db", sid)

    def texts(self, entries):
        return [e.record["part"]["text"] for e in entries]

    def test_database_tail_follow_and_earlier_for_both_generations(self):
        import tempfile
        for generation in ("v1", "v2"):
            with self.subTest(generation=generation), tempfile.TemporaryDirectory() as directory:
                source = self.make_db(directory, generation)
                cursor = view.Cursor(source.path)
                tail = view.read_entries(source, cursor, max_records=3, tail=True)
                self.assertEqual(self.texts(tail.entries), ["row4", "row5", "row6"])
                self.assertEqual(view.read_entries(source, cursor, max_records=3).entries
                                 if generation == "v2" else [], [])
                self.assertTrue(view.has_earlier(source, cursor))
                earlier = view.read_earlier(source, cursor, max_records=2)
                self.assertEqual(self.texts(earlier.entries), ["row2", "row3"])
                everything = view.read_earlier(source, cursor, max_records=10)
                self.assertEqual(self.texts(everything.entries), ["row1"])
                self.assertFalse(view.has_earlier(source, cursor))

    def test_database_streaming_row_keeps_its_key_when_it_changes(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_db(directory, "v1", count=2)
            cursor = view.Cursor(source.path)
            first = view.read_entries(source, cursor, max_records=200, tail=True)
            self.assertEqual([e.key for e in first.entries], [(1, 0), (2, 0)])
            with sqlite3.connect(source.path) as con:
                con.execute("UPDATE part SET data=? WHERE rowid=2", (json.dumps({"type": "text", "text": "row2 grown"}),))
            second = view.read_entries(source, cursor, max_records=200)
            self.assertEqual([(e.key, e.record["part"]["text"]) for e in second.entries], [((2, 0), "row2 grown")])
            self.assertEqual(view.read_entries(source, cursor, max_records=200).entries, [])

    def test_database_failures_are_missing_or_unreadable_not_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            source = view.Source(Path(directory) / "gone.db", "opencode", "opencode-db", "ses_boundedview")
            result = view.read_entries(source, view.Cursor(source.path), max_records=5, tail=True)
            self.assertEqual((result.state, result.reason), ("missing", "database-unavailable"))
            broken = Path(directory) / "broken.db"
            broken.write_bytes(b"not a database" * 100)
            source = view.Source(broken, "opencode", "opencode-db", "ses_boundedview")
            result = view.read_entries(source, view.Cursor(broken), max_records=5, tail=True)
            self.assertEqual((result.state, result.reason), ("unreadable", "database-read-failed"))


@unittest.skipUnless(os.environ.get("HEARTING_ATTEMPT_VIEW_LOCAL") == "1",
                     "set HEARTING_ATTEMPT_VIEW_LOCAL=1 for local log checks")
class LocalSamples(unittest.TestCase):
    def test_all_local_dispatch_logs_format_without_stopping(self):
        files=set()
        roots=view._dispatch_roots()
        for root in roots:
            logs=root / "logs"
            if logs.is_dir(): files.update(p.resolve() for p in logs.rglob("*.jsonl") if p.is_file())
            try: registry=(root / "jobs.log").read_text(errors="replace").splitlines()
            except OSError: registry=[]
            for raw in registry:
                parsed=view._metadata(raw)
                if parsed:
                    fields,meta=parsed
                    admitted=view._log_path(root,fields,meta)
                    if admitted: files.add(Path(admitted).resolve())
        total=non_json=unknown=formatter_errors=unsafe=0
        unknown_types=Counter()
        unreadable=[]
        for index,path in enumerate(sorted(files),1):
            if index == 1 or index % 100 == 0:
                print(f"LocalSamples progress {index}/{len(files)}: {path}", flush=True)
            harness=next((h for h in ("claude", "codex", "opencode") if path.name.endswith("."+h+".jsonl")), "unknown")
            try:
                with path.open("rb") as stream:
                    for number,raw in enumerate(stream,1):
                        total+=1
                        text=raw.decode("utf-8", "replace").rstrip("\r\n")
                        try: record=json.loads(text)
                        except (ValueError, TypeError): record=text; non_json+=1
                        try:
                            for line in view.format_record(harness, record):
                                reasons=[]
                                if re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", line.text) or "\x1b" in line.text:
                                    reasons.append("control-sequence")
                                if any(match.group(1) != "[redacted]" for match in
                                       re.finditer(r"(?i)\bBearer\s+([^\s]+)", line.text)):
                                    reasons.append("unmasked-bearer")
                                if re.search(r"https?://[^\s/@]+:[^\s/@]+@", line.text, re.I):
                                    reasons.append("url-credentials")
                                if reasons:
                                    unsafe+=len(reasons)
                                    print(f"sanitizer: {','.join(reasons)}: {path}:{number}")
                                if line.kind == "unknown":
                                    unknown+=1
                                    name = (record.get("type") or record.get("kind") or record.get("method") or "unknown") if isinstance(record, dict) else "non-object"
                                    item = record.get("item") if isinstance(record, dict) and isinstance(record.get("item"), dict) else {}
                                    subtype = item.get("type") or "-"
                                    unknown_types[(harness, str(name), str(subtype))] += 1
                        except Exception:
                            formatter_errors+=1
                            print(f"formatter: exception: {path}:{number}")
            except OSError:
                unreadable.append(path)
                print(f"unreadable: disappeared or unreadable path: {path}:1")
        print(f"LocalSamples files={len(files)} lines={total} non_json={non_json} unknown={unknown} "
              f"unreadable={len(unreadable)} formatter_exceptions={formatter_errors} sanitizer_findings={unsafe}")
        print("LocalSamples top_unknown=" + ", ".join(
            f"{harness}/{name}/{subtype}:{count}"
            for (harness, name, subtype), count in unknown_types.most_common(15)))
        self.assertGreater(len(files), 0)
        self.assertEqual(formatter_errors, 0)
        self.assertEqual(unsafe, 0)


if __name__ == "__main__":
    unittest.main()
