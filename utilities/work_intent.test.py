#!/usr/bin/env python3
"""The intent at the start of a route: read from the prompt file, the person's words kept once
beside the route and outside its identity, shared by every leg (WORKFLOW §0.4)."""
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
import route_identity  # noqa: E402
import work_intent as WI  # noqa: E402
import work_start  # noqa: E402

HEADER = ("의도: 시작 때 의도를 한 번 고정한다.\n"
          "범위: /data/out에 결과 쓰기\n"
          "안 할 것: 새 게이트; 필수 입력\n"
          "완료: owner와 워커 지시문에 의도가 실린다.\n\n"
          "Seal the person's intent at compose. Then carry it.\n")


class DraftTest(unittest.TestCase):
    def test_header_lines_are_read_and_body_is_left(self):
        found = WI.draft(HEADER)
        self.assertEqual(found, {"intent": "시작 때 의도를 한 번 고정한다.", "scope": "/data/out에 결과 쓰기",
                                 "not_doing": ["새 게이트", "필수 입력"],
                                 "done": "owner와 워커 지시문에 의도가 실린다.", "basis": "header"})
        english = WI.draft("Intent: Keep one row.\n**Scope:** utilities/\nNot doing: new flags\nDone: test passes\n\nBody.")
        self.assertEqual((english["intent"], english["scope"], english["not_doing"]),
                         ("Keep one row.", "utilities/", ["new flags"]))

    def test_without_header_the_first_sentence_is_a_marked_draft(self):
        found = WI.draft("Fix: the Fleet row repeats. Keep one row per session.\n")
        self.assertEqual(found, {"intent": "Fix: the Fleet row repeats.", "basis": "first-sentence"})
        partial = WI.draft("안 할 것: 게이트\n\nKeep one row per session. More.")
        self.assertEqual(partial["intent"], "Keep one row per session.")
        self.assertEqual(partial["not_doing"], ["게이트"])
        self.assertEqual(WI.draft("   "), {})

    def test_the_route_request_shape_is_unchanged(self):
        # Older runtimes read the same `work_request`; nothing new is sealed into it.
        with self.assertRaisesRegex(ValueError, "work-request-invalid"):
            work_start.validate_request({"text": "t", "owner_harness": None, "intent": {"intent": "x"}})

    def test_credentials_are_masked(self):
        text = ("이 ghp_" + "a" * 36 + " 토큰과 sk-" + "b" * 24 + " 로 push 해. "
                "Authorization: Bearer abc.def.ghi https://user:pw@example.com/x password=hunter2hunter2 "
                "AKIA" + "C" * 16)
        masked = WI.redact_secrets(text)
        for secret in ("ghp_", "sk-", "abc.def.ghi", "user:pw", "hunter2", "AKIA"):
            self.assertNotIn(secret, masked)
        self.assertIn("로 push 해", masked)


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

    def excerpt(self, env=None):
        return WI.original_excerpt(str(self.cwd), dict(env or self.env))

    def test_the_latest_person_prompt_is_read_and_bounded(self):
        self.prompt_boundary()
        self.rows(self.user("an older prompt"),
                  {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
                  self.user("사용자의 의도가 맞는지 체크하는 건 중요해 " + "가" * 700),
                  self.user([{"type": "tool_result", "content": "tool"}]),
                  self.user("<system note>", isMeta=True))
        excerpt = self.excerpt()
        self.assertTrue(excerpt.startswith("사용자의 의도가 맞는지"))
        self.assertEqual(len(excerpt), WI.MAX_ORIGINAL_CHARS)

    def test_a_bare_card_answer_is_skipped_for_the_request_before_it(self):
        # Codex/OpenCode answer the plain-text card in the conversation; Claude's goes through a tool.
        self.prompt_boundary()
        self.rows(self.user("로그인 API를 만들고 시험해 줘"),
                  {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "[의도 확인]"}]}},
                  self.user("진행"), self.user("  진행(권장) "), self.user("ok."))
        self.assertEqual(self.excerpt(), "로그인 API를 만들고 시험해 줘")
        self.rows(self.user("<task-notification>\n<task-id>x</task-id>"), self.user("진행"))
        self.assertEqual(self.excerpt(), "")              # what precedes the answer is not the person's

    def test_peer_runtime_or_unknown_boundaries_leave_the_excerpt_out(self):
        self.rows(self.user("a person's words"))
        self.assertEqual(self.excerpt(), "")  # no boundary yet
        self.prompt_boundary(origin={"kind": "peer", "transfer_ref": "ref"})
        self.assertEqual(self.excerpt(), "")
        self.prompt_boundary()
        self.rows(self.user("<task-notification>\n<task-id>x</task-id>"))
        self.assertEqual(self.excerpt(), "")
        self.rows(self.user("words\n(peer-from: b6 · ref)"))
        self.assertEqual(self.excerpt(), "")
        ambiguous = {**self.env, "CODEX_THREAD_ID": "thread"}
        del ambiguous["AGENT_DISPATCH_CALLER_HARNESS"]
        self.rows(self.user("a person's words"))
        self.assertEqual(self.excerpt(ambiguous), "")

    def test_a_pasted_token_never_reaches_the_record(self):
        self.prompt_boundary()
        self.rows(self.user("push with ghp_" + "z" * 36 + " please"))
        self.assertEqual(self.excerpt(), "push with [redacted] please")


class RecordTest(unittest.TestCase):
    """Once, beside the route, outside its identity; every leg reads its frame route's record."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.frame = {"route_id": "rt-aaaaaaaaaaaaaaaa", "artifact_root": str(self.root), "cwd": str(self.root),
                      "work_request": {"text": HEADER, "owner_harness": None}}
        self.frame["route_hash"] = route_identity.route_hash(self.frame)
        decision = self.root / "campaigns/c/cyc/artifacts/shards/frame/route-decision.json"
        decision.parent.mkdir(parents=True)
        decision.write_text(json.dumps({"decision": {"frame_route": {"route_id": self.frame["route_id"]}}}))
        self.leg = {"route_id": "rt-bbbbbbbbbbbbbbbb", "artifact_root": str(self.root), "cwd": str(self.root),
                    "work_request": {"text": HEADER, "owner_harness": None},
                    "route_plan": {"decision": str(decision.relative_to(self.root)), "digest": "sha256:" + "0" * 64,
                                   "index": 0}}

    def test_the_first_write_wins_and_the_route_identity_does_not_move(self):
        before = route_identity.route_hash(self.frame)
        with mock.patch.object(WI, "original_excerpt", return_value="처음 요청"):
            self.assertEqual(WI.seal_original(self.frame), "처음 요청")
        with mock.patch.object(WI, "original_excerpt", return_value="계속 해 줘 다음 말"):
            self.assertEqual(WI.seal_original(self.frame), "")      # the same compose again keeps the first
        self.assertEqual(WI.read_original(self.frame), "처음 요청")
        self.assertEqual(route_identity.route_hash(self.frame), before)
        self.assertNotIn("intent", self.frame["work_request"])

    def test_a_leg_shares_the_frame_record_and_never_reads_the_session(self):
        with mock.patch.object(WI, "original_excerpt", return_value="처음 요청"):
            WI.seal_original(self.frame)
        with mock.patch.object(WI, "original_excerpt", side_effect=AssertionError("a leg read the session")):
            self.assertEqual(WI.seal_original(self.leg), "")
            self.assertEqual(WI.read_original(self.leg), "처음 요청")
            self.assertEqual(WI.route_intent(self.leg)["original"], "처음 요청")
        self.assertEqual(WI.work_key(self.leg), self.frame["route_id"])
        self.assertIsNone(WI.work_key({**self.leg, "route_plan": {**self.leg["route_plan"], "decision": "missing.json"}}))


class ReadersTest(unittest.TestCase):
    def route(self, text, original=""):
        return {"route_id": "rt-cccccccccccccccc", "work_request": {"text": text, "owner_harness": None},
                "artifact_root": "/nonexistent"}

    def test_card_and_prompt_show_only_what_the_route_says(self):
        full = self.route(HEADER)
        self.assertEqual(WI.card_lines(full, original="체크해 줘"), [
            "  [의도] 시작 때 의도를 한 번 고정한다.",
            "    범위 /data/out에 결과 쓰기 · 안 할 것 새 게이트; 필수 입력 · 완료 owner와 워커 지시문에 의도가 실린다.",
            "    원문 \"체크해 줘\""])
        with mock.patch.object(WI, "read_original", return_value="체크해 줘"):
            block = WI.prompt_block(full)
        self.assertIn("Person's words (excerpt): \"체크해 줘\"", block)
        self.assertIn("- Scope: /data/out에 결과 쓰기", block)
        self.assertIn("- Not doing: 새 게이트", block)
        drafted = self.route("first. second")
        self.assertEqual(WI.card_lines(drafted), ["  [의도] first. (요청 첫 문장)"])
        self.assertIn("drafted from the request's first sentence", WI.prompt_block(drafted))
        for empty in ({"route_id": "rt-x"}, self.route("   ")):
            self.assertEqual(WI.card_lines(empty), [])
            self.assertEqual(WI.prompt_block(empty), "")


if __name__ == "__main__":
    unittest.main()
