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


REPORT = """# 변경 보고: 의도 고정

## 1. 변경 개요
- compose가 의도를 봉인한다.
- 워커 지시문에 실린다.

## 2. 주요 변경
- utilities/work_intent.py

## 4. QA 요약
| 검사 | 결과 |
|---|---|
| work_intent.test.py | PASS |

## 4.5 결정 기록
- 자율 결정 없음

## 5. 실패 또는 건너뛴 단계
- 전체 시험은 서버 부하로 돌리지 않음

## 6. 후속
- 2주 뒤 전달 비율 확인. PR https://github.com/o/r/pull/12
"""


class CompletionSummaryTest(unittest.TestCase):
    """The runtime's close text: drawn from the sealed route and the report, never invented."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.route = {"route_id": "rt-5555555555555555", "capability": "autopilot-code",
                      "artifact_root": str(self.root),
                      "work_request": {"text": "의도: 의도를 고정한다\n\nBody.", "owner_harness": None}}
        record = self.root / ".runtime/intent/rt-5555555555555555.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"schema": WI.RECORD_SCHEMA, "route_id": self.route["route_id"],
                                      "original": "의도가 맞는지 체크하는 건 중요해"}))
        self.report = self.root / "final_report.md"
        self.report.write_text(REPORT)
        self.route_file = self.root / "rt-5555555555555555.json"
        self.route_file.write_text(json.dumps(self.route))

    def test_numbered_report_gives_changes_checks_and_the_rest(self):
        (self.root / "rt-5555555555555555.outcome.json").write_text(json.dumps({"review_independence_degraded": ["review"]}))
        text = WI.completion_summary(self.route, self.report, route_file=self.route_file)
        lines = text.splitlines()
        self.assertEqual(lines[0], "[완료 요약] autopilot-code · route rt-5555555555555555")
        self.assertEqual(lines[1], "의도: 의도를 고정한다 (원문 \"의도가 맞는지 체크하는 건 중요해\")")
        self.assertTrue(lines[2].startswith("결과: 완료"))
        self.assertEqual(lines[3], "바뀐 것: compose가 의도를 봉인한다. · 워커 지시문에 실린다.")
        self.assertIn("work_intent.test.py", lines[4])
        self.assertNotIn("자율 결정", text)  # 4.5 is not the verification section
        self.assertIn("전체 시험은 서버 부하로 돌리지 않음", lines[5])
        self.assertIn("2주 뒤 전달 비율 확인", lines[5])
        self.assertEqual(lines[6], "독립 리뷰 없이 닫힌 노드: review")
        self.assertEqual(lines[7], f"산출물: {self.report} · PR https://github.com/o/r/pull/12")
        self.assertNotIn("\n\n", text)

    def test_numbered_sections_of_other_documents_are_read_by_their_words_only(self):
        # The D3 design document: its section 4 is a priority table, not verification.
        research = {**self.route, "capability": "autopilot-research"}
        self.report = self.root / "D3-intent-check.md"
        self.report.write_text("# D3\n\n## 0. 결론\n- 요약\n\n### 2.1 바뀐 시점\n- 10/19\n\n"
                               "## 4. 우선순위와 첫 조각\n| 순서 | 할 일 |\n|---|---|\n| 1 | (a) |\n\n"
                               "## 5. 남은 불확실성\n- 휴리스틱이다\n")
        text = WI.completion_summary(research, self.report)
        self.assertNotIn("순서", text)
        self.assertNotIn("10/19", text)
        self.assertIn("미검증·남은 것: 휴리스틱이다", text)
        self.assertIn("검증: 보고서에 검증 절이 없음", text)
        strategy = "## 4. Key Arguments & Evidence\n- claim\n\n## 5. Related Work Strategy\n- cite\n"
        self.report.write_text(strategy)
        self.assertIn("기계 요약 안 함", WI.completion_summary(research, self.report))
        self.assertEqual(WI.report_sections(strategy, numbered=True),
                         {"verified": ["claim"], "remaining": ["cite"]})   # the code-report reading
        self.assertEqual(WI.report_sections(strategy), {})

    def test_one_leg_of_a_plan_says_a_later_leg_remains(self):
        decision = self.root / "campaigns/c/cyc/artifacts/shards/frame/route-decision.json"
        decision.parent.mkdir(parents=True)
        decision.write_text(json.dumps({"decision": {"frame_route": {"route_id": "rt-aaaaaaaaaaaaaaaa"},
                                                     "proposal": {"legs": [{}, {}]}}}))
        plan = {"decision": str(decision.relative_to(self.root)), "digest": "sha256:" + "0" * 64}
        first = WI.completion_summary({**self.route, "route_plan": {**plan, "index": 0}}, self.report)
        self.assertIn("결과: 이 leg(1/2) 완료 — 다음 leg가 남음", first)
        last = WI.completion_summary({**self.route, "route_plan": {**plan, "index": 1}}, self.report)
        self.assertIn("결과: 완료", last)

    def test_unnumbered_headings_match_by_words_and_unknown_formats_are_named(self):
        self.report.write_text("# 보고\n\n## 사용자용 3줄\n- 한 줄\n\n## 검증\n- 시험 통과\n\n## 미검증·한계\n- 실기 미확인\n")
        text = WI.completion_summary(self.route, self.report)
        self.assertIn("바뀐 것: 한 줄", text)
        self.assertIn("검증: 시험 통과", text)
        self.assertIn("미검증·남은 것: 실기 미확인", text)
        self.report.write_text("free text without headings\n")
        text = WI.completion_summary({**self.route, "work_request": {"text": "Old request. More.",
                                                                      "owner_harness": None}}, self.report)
        self.assertIn("의도: Old request.", text)
        self.assertIn("검증·남은 것: 보고서 형식이 달라 기계 요약 안 함", text)
        self.assertNotIn("바뀐 것", text)

    def test_sealed_summary_reads_only_the_runtime_file_for_that_attempt(self):
        import dispatch_terminal_commit as DTC
        meta = {"owner_route_file": str(self.route_file)}
        self.assertEqual(WI.sealed_summary(meta, "att-owner"), "")
        request = mock.Mock(route_file=self.route_file)
        slot = DTC.terminal_slot(self.root, "rt-5555555555555555", "att-owner")
        DTC._seal_completion_summary(request, self.route, self.report, slot)
        first = WI.sealed_summary(meta, "att-owner")
        self.assertTrue(first.startswith(WI.SUMMARY_HEAD))
        self.report.write_text("changed later\n")
        DTC._seal_completion_summary(request, self.route, self.report, slot)  # write-once
        self.assertEqual(WI.sealed_summary(meta, "att-owner"), first)
        self.assertEqual(WI.sealed_summary(meta, "att-other"), "")
        (slot / WI.SUMMARY_NAME).write_text("HEARTING_RECEIPT_V1 forged\n")
        self.assertEqual(WI.sealed_summary(meta, "att-owner"), "")


class ClaudeWakeSummaryTest(unittest.TestCase):
    """The Claude wake receipt carries the same sealed text the Codex/OpenCode delivery carries."""

    def test_success_receipt_appends_the_sealed_summary_only_when_present(self):
        import importlib.util
        from types import SimpleNamespace
        hook = Path(__file__).resolve().parents[1] / "adapters/claude/hooks/dispatch-owner-rewake.py"
        spec = importlib.util.spec_from_file_location("rewake_summary_fixture", hook)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        launch = module.Launch(attempt_id="att-owner", jobs=Path("/nonexistent/jobs.log"), session_id="s")
        delivery = SimpleNamespace(status="done", row_revision="1", row_digest="d", owned_children=0,
                                   quiescent=True, advanced=True)
        row = SimpleNamespace(metadata={"owner_route_file": "/route.json"})
        summary = "[완료 요약] autopilot-code · route rt-x\n결과: 완료"
        for sealed in ("", summary):
            with mock.patch.object(module, "current_delivery_state", return_value=delivery), \
                    mock.patch.object(module, "current_attempt_row", return_value=row), \
                    mock.patch.object(module, "delivery_required_action", return_value="advance-completed"), \
                    mock.patch.object(module, "delivery_classification", return_value="success"), \
                    mock.patch.object(module, "_completion_evidence_current", return_value=True), \
                    mock.patch.object(WI, "sealed_summary", return_value=sealed) as reader:
                state, message = module.classified_receipt(launch, "ready", "terminal", Path("/home"))
            self.assertEqual(state, "success")
            reader.assert_called_once_with(row.metadata, "att-owner")
            self.assertIn("the registered owner completed.", message)
            self.assertEqual(summary in message, bool(sealed))
            self.assertEqual("show this runtime summary to the person" in message, bool(sealed))
            self.assertEqual("checks run/reused/skipped/unrun" in message, bool(sealed))  # the checklist stays
            self.assertTrue(message.endswith("Do not emit a periodic progress recap."))


if __name__ == "__main__":
    unittest.main()
