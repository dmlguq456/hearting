#!/usr/bin/env python3
"""The intent sealed at compose: optional, read from the prompt file and the
person's own prompt, carried to every leg, owner and worker (WORKFLOW §0.4)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import work_intent as WI  # noqa: E402
import work_start  # noqa: E402

HEADER = ("의도: 시작 때 의도를 한 번 고정한다.\n"
          "안 할 것: 새 게이트; 필수 입력\n"
          "완료: owner와 워커 지시문에 의도가 실린다.\n\n"
          "Seal the person's intent at compose. Then carry it.\n")


class DraftTest(unittest.TestCase):
    def test_header_lines_are_read_and_body_is_left(self):
        found = WI.draft(HEADER)
        self.assertEqual(found, {"intent": "시작 때 의도를 한 번 고정한다.",
                                 "not_doing": ["새 게이트", "필수 입력"],
                                 "done": "owner와 워커 지시문에 의도가 실린다.", "basis": "header"})
        english = WI.draft("Intent: Keep one row.\nNot doing: new flags\nDone: test passes\n\nBody.")
        self.assertEqual(english["intent"], "Keep one row.")
        self.assertEqual(english["not_doing"], ["new flags"])

    def test_without_header_the_first_sentence_is_a_marked_draft(self):
        found = WI.draft("Fix: the Fleet row repeats. Keep one row per session.\n")
        self.assertEqual(found, {"intent": "Fix: the Fleet row repeats.", "basis": "first-sentence"})
        partial = WI.draft("안 할 것: 게이트\n\nKeep one row per session. More.")
        self.assertEqual(partial["intent"], "Keep one row per session.")
        self.assertEqual(partial["not_doing"], ["게이트"])
        self.assertEqual(WI.draft("   "), {})

    def test_validation_bounds_shape_and_rejects_unknown_keys(self):
        WI.validate({"intent": "x"})
        for bad in ({}, {"basis": "header"}, {"intent": "x", "extra": 1}, {"intent": ""},
                    {"not_doing": []}, {"not_doing": ["a", "b", "c", "d"]}, {"original": "x" * 601},
                    {"intent": "x", "basis": "guess"}, {"done": "x", "basis": "header"}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "work-request-intent-invalid"):
                WI.validate(bad)
        work_start.validate_request({"text": "t", "owner_harness": None, "intent": {"intent": "x"}})
        with self.assertRaisesRegex(ValueError, "work-request-intent-invalid"):
            work_start.validate_request({"text": "t", "owner_harness": None, "intent": {"x": 1}})

    def test_a_leg_inherits_the_start_intent_and_never_reads_the_session(self):
        start = {"text": HEADER, "owner_harness": None,
                 "intent": {"original": "사람의 말", "intent": "고정", "basis": "header"}}
        with mock.patch.object(WI, "original_excerpt", side_effect=AssertionError("read")):
            self.assertEqual(WI.inherited(start, "leg text"), start["intent"])
            self.assertEqual(WI.inherited({"text": "old"}, HEADER)["intent"], "시작 때 의도를 한 번 고정한다.")
            self.assertIsNone(WI.inherited(None, ""))


class OriginalExcerptTest(unittest.TestCase):
    """The person's words come from the session record at a person's prompt boundary only."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sid = "11111111-2222-3333-4444-555555555555"
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.env = {"XDG_STATE_HOME": str(self.root / "state"), "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                    "AGENT_DISPATCH_CALLER_HARNESS": "claude", "CLAUDE_CODE_SESSION_ID": self.sid,
                    "HOME": str(self.root)}
        patch = mock.patch.dict(os.environ, self.env, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        self.transcript = self.root / "claude/projects/p" / f"{self.sid}.jsonl"
        self.transcript.parent.mkdir(parents=True)

    def prompt_boundary(self, origin=None):
        import session_tidy
        seat = session_tidy.resolve_seat("claude", str(self.cwd), dict(self.env), self.sid, "")
        seq = session_tidy.bump_prompt_seq(seat, "claude", self.sid, time.time())
        if origin is not None:
            path = session_tidy._prompt_seq_path(session_tidy.session_seat(seat, "claude", self.sid))
            data = json.loads(path.read_text())
            data["request_origin"] = origin
            path.write_text(json.dumps(data))
        return seq

    def rows(self, *rows):
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def user(self, text, **extra):
        return {"type": "user", "message": {"role": "user", "content": text}, **extra}

    def test_the_latest_person_prompt_is_read_and_bounded(self):
        self.prompt_boundary()
        self.rows(self.user("an older prompt"),
                  {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
                  self.user("사용자의 의도가 맞는지 체크하는 건 중요해 " + "가" * 700),
                  self.user([{"type": "tool_result", "content": "tool"}]),
                  self.user("<system note>", isMeta=True))
        excerpt = WI.original_excerpt(str(self.cwd), dict(self.env))
        self.assertTrue(excerpt.startswith("사용자의 의도가 맞는지"))
        self.assertEqual(len(excerpt), WI.MAX_ORIGINAL_CHARS)
        sealed = WI.capture(HEADER, str(self.cwd), dict(self.env))
        self.assertEqual(sealed["original"], excerpt)
        self.assertEqual(sealed["intent"], "시작 때 의도를 한 번 고정한다.")

    def test_peer_runtime_or_unknown_boundaries_leave_the_excerpt_out(self):
        self.rows(self.user("a person's words"))
        self.assertEqual(WI.original_excerpt(str(self.cwd), dict(self.env)), "")  # no boundary yet
        self.prompt_boundary(origin={"kind": "peer", "transfer_ref": "ref"})
        self.assertEqual(WI.original_excerpt(str(self.cwd), dict(self.env)), "")
        self.prompt_boundary()
        self.rows(self.user("<task-notification>\n<task-id>x</task-id>"))
        self.assertEqual(WI.original_excerpt(str(self.cwd), dict(self.env)), "")
        self.rows(self.user("words\n(peer-from: b6 · ref)"))
        self.assertEqual(WI.original_excerpt(str(self.cwd), dict(self.env)), "")
        ambiguous = {**self.env, "CODEX_THREAD_ID": "thread"}
        del ambiguous["AGENT_DISPATCH_CALLER_HARNESS"]
        self.rows(self.user("a person's words"))
        self.assertEqual(WI.original_excerpt(str(self.cwd), ambiguous), "")
        self.assertEqual(WI.capture("Body only.", str(self.cwd), ambiguous),
                         {"intent": "Body only.", "basis": "first-sentence"})


class ReadersTest(unittest.TestCase):
    def route(self, intent):
        return {"route_id": "rt-x", "work_request": {"text": "t", "owner_harness": None, "intent": intent}}

    def test_card_and_prompt_show_only_what_was_sealed(self):
        full = self.route({"original": "체크해 줘", "intent": "의도를 고정", "not_doing": ["게이트"],
                           "done": "워커에 실림", "basis": "header"})
        self.assertEqual(WI.card_lines(full), ["  [의도] 의도를 고정", "    안 할 것 게이트 · 완료 워커에 실림",
                                               "    원문 \"체크해 줘\""])
        block = WI.prompt_block(full)
        self.assertIn("Person's words (excerpt): \"체크해 줘\"", block)
        self.assertIn("- Not doing: 게이트", block)
        self.assertIn("- Done when: 워커에 실림", block)
        drafted = self.route({"intent": "first", "basis": "first-sentence"})
        self.assertEqual(WI.card_lines(drafted), ["  [의도] first (요청 첫 문장)"])
        self.assertIn("drafted from the request's first sentence", WI.prompt_block(drafted))
        for empty in ({"route_id": "rt-x"}, self.route({"bad": 1})):
            self.assertEqual(WI.card_lines(empty), [])
            self.assertEqual(WI.prompt_block(empty), "")


if __name__ == "__main__":
    unittest.main()
