#!/usr/bin/env python3
"""The three native question tools' replies read as one shape, kept for the frame question."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frame_native_answer as N  # noqa: E402

QUESTIONS = [{"id": "restate", "question": "이렇게 이해했습니다: 두 결과를 모두 남깁니다.",
              "options": [{"label": "예 (권장)"}, {"label": "아니오"}]},
             {"id": "scope", "question": "어디까지 고칠까요?", "options": [{"label": "둘 다"}, {"label": "승인만"}]}]
EXPECTED = [{"question": QUESTIONS[0]["question"], "options": ["예 (권장)", "아니오"], "answer": "예 (권장)"},
            {"question": QUESTIONS[1]["question"], "options": ["둘 다", "승인만"], "answer": "승인만만 고쳐 주세요"}]


class AskedFromTest(unittest.TestCase):
    def test_each_harness_reply_reads_as_the_same_questions_and_answers(self):
        claude = {"tool_name": "AskUserQuestion", "session_id": "s-claude", "tool_input": {"questions": QUESTIONS},
                  "tool_response": {"questions": QUESTIONS, "answers": {QUESTIONS[0]["question"]: "예 (권장)",
                                                                         QUESTIONS[1]["question"]: "승인만만 고쳐 주세요"}}}
        codex = {"tool_name": "request_user_input", "session_id": "s-codex", "tool_input": {"questions": QUESTIONS},
                 "tool_response": json.dumps({"answers": {"restate": {"answers": ["예 (권장)"]},
                                                          "scope": {"answers": ["user_note: 승인만만 고쳐 주세요"]}}})}
        opencode = {"tool": "question", "sessionID": "s-opencode", "args": {"questions": QUESTIONS},
                    "answers": [["예 (권장)"], ["승인만만 고쳐 주세요"]]}
        for harness, payload in (("claude", claude), ("codex", codex), ("opencode", opencode)):
            with self.subTest(harness):
                self.assertEqual(N.asked_from(harness, payload), (f"s-{harness}", EXPECTED))

    def test_a_picked_codex_option_wins_and_the_note_beside_it_is_kept(self):
        payload = {"tool_name": "request_user_input", "session_id": "s", "tool_input": {"questions": QUESTIONS[1:]},
                   "tool_response": json.dumps({"answers": {"scope": {"answers": ["둘 다", "user_note: 빨리"]}}})}
        row = N.asked_from("codex", payload)[1][0]
        self.assertEqual(row["answer"], "둘 다")
        self.assertEqual(row["note"], "빨리")     # GA-A5-007: the words beside it are kept as its note

    def test_another_tool_or_harness_is_not_a_reply(self):
        self.assertEqual(N.asked_from("claude", {"tool_name": "Bash"}), ("", []))
        self.assertEqual(N.asked_from("codex", {"tool_name": "request_user_input", "tool_response": "{not json"}), ("", []))
        self.assertEqual(N.asked_from("other", {}), ("", []))


class RecordTest(unittest.TestCase):
    def run_hook(self, flag, payload):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            self.assertEqual(N.main([flag]), 0)
        return out.getvalue()

    def test_the_newest_route_with_a_waiting_question_keeps_the_reply_and_the_session_is_told(self):
        payload = {"tool_name": "AskUserQuestion", "session_id": "s-claude", "tool_input": {"questions": QUESTIONS},
                   "tool_response": {"answers": {QUESTIONS[1]["question"]: "둘 다"}}}
        seen = []

        def candidate(route, jobs, asked):
            seen.append(route["route_id"])
            return (Path("/candidate/answers.native.json"), {}) if route["route_id"] == "rt-older" else None
        import tempfile
        import work_start
        tools = str(Path(__file__).resolve().parents[1] / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from fleet import route as route_reader
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for name in ("rt-newer", "rt-older"):
                files.append(Path(tmp) / f"{name}.json")
                files[-1].write_text(json.dumps({"route_id": name}), encoding="utf-8")
            with mock.patch.object(N, "_session_routes", return_value=[str(p) for p in files]), \
                    mock.patch.object(route_reader, "load", side_effect=lambda p: json.loads(Path(p).read_text())), \
                    mock.patch.object(work_start, "native_answer_candidate", side_effect=candidate), \
                    mock.patch.object(work_start, "record_native_answer", return_value=Path("/kept/answers.native.json")):
                told = json.loads(self.run_hook("--claude", payload))
        self.assertEqual(seen, ["rt-newer", "rt-older"])
        self.assertIn("/kept/answers.native.json", told["hookSpecificOutput"]["additionalContext"])

    def test_a_partial_reply_tells_claude_and_codex_how_many_questions_remain(self):
        import tempfile
        payload = {"tool_name": "AskUserQuestion", "session_id": "s", "tool_input": {"questions": QUESTIONS},
                   "tool_response": {"answers": {QUESTIONS[1]["question"]: "둘 다"}}}
        with tempfile.TemporaryDirectory() as tmp:
            partial = Path(tmp) / "answers.native.partial.json"
            partial.write_text(json.dumps({"missing": ["_understanding", "q-two"]}), encoding="utf-8")
            with mock.patch.object(N, "record", return_value=partial):
                for flag in ("--claude", "--codex"):
                    told = json.loads(self.run_hook(flag, payload))
                    self.assertIn("2 registered question(s) remain", told["hookSpecificOutput"]["additionalContext"])
                self.assertEqual(self.run_hook("--opencode", payload), "")
            partial.write_text("{broken", encoding="utf-8")
            with mock.patch.object(N, "record", return_value=partial):
                self.assertEqual(self.run_hook("--claude", payload), "")

    def test_nothing_answered_or_a_broken_payload_is_quiet(self):
        payload = {"tool_name": "AskUserQuestion", "session_id": "s", "tool_input": {"questions": QUESTIONS},
                   "tool_response": {"answers": {}}}
        with mock.patch.object(N, "_session_routes", side_effect=AssertionError("no lookup without an answer")):
            self.assertEqual(self.run_hook("--claude", payload), "")
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("{broken")), contextlib.redirect_stdout(out):
            self.assertEqual(N.main(["--codex"]), 0)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
