#!/usr/bin/env python3
"""The intent at the start of a route: read from the prompt file, the person's words kept once
beside the route and outside its identity, shared by every leg (WORKFLOW §0.4)."""
from __future__ import annotations

import json
import os
import re
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

    def prompt_boundary(self, origin=None, text=None):
        import session_tidy
        seat = session_tidy.resolve_seat("claude", str(self.cwd), dict(self.env), self.sid, "")
        if text is not None:
            session_tidy._run_hook("claude", "prompt", self.sid, cwd=str(self.cwd), env=dict(self.env),
                                   prompt_text=text, render_context=False)
            return session_tidy.read_prompt_seq(seat)
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
        text = "사용자의 의도가 맞는지 체크하는 건 중요해 " + "가" * 700
        self.prompt_boundary(text=text)
        self.rows(self.user("an older prompt"),
                  {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
                  self.user(text), self.user("unrelated later transcript write"),
                  self.user([{"type": "tool_result", "content": "tool"}]),
                  self.user("<system note>", isMeta=True))
        excerpt = self.excerpt()
        self.assertTrue(excerpt.startswith("사용자의 의도가 맞는지"))
        self.assertEqual(len(excerpt), WI.MAX_ORIGINAL_CHARS)

    def test_a_bare_card_answer_has_no_request_quote(self):
        # Codex/OpenCode answer the plain-text card in the conversation; Claude's goes through a tool.
        self.prompt_boundary(text="진행")
        self.rows(self.user("로그인 API를 만들고 시험해 줘"),
                  {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "[의도 확인]"}]}},
                  self.user("진행"), self.user("  진행(권장) "), self.user("ok."))
        self.assertEqual(self.excerpt(), "")
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
        self.prompt_boundary(text="push with ghp_" + "z" * 36 + " please")
        self.rows(self.user("push with ghp_" + "z" * 36 + " please"))
        self.assertEqual(self.excerpt(), "push with [redacted] please")

    def test_second_request_uses_its_own_boundary_for_all_three_adapters(self):
        import session_tidy
        for harness, key in (("claude", "CLAUDE_CODE_SESSION_ID"), ("codex", "CODEX_THREAD_ID"),
                             ("opencode", "OPENCODE_SESSION_ID")):
            env = {k: v for k, v in self.env.items() if k != "CLAUDE_CODE_SESSION_ID"}
            env.update(AGENT_DISPATCH_CALLER_HARNESS=harness, **{key: self.sid})
            for text in ("First independent task", "Second independent task"):
                session_tidy._run_hook(harness, "prompt", self.sid, cwd=str(self.cwd), env=env,
                                       prompt_text=text, render_context=False)
                self.assertEqual(WI.original_excerpt(str(self.cwd), env), text)


class RecordTest(unittest.TestCase):
    """Once, beside the route, outside its identity; every leg reads its frame route's record."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.frame = {"route_id": "rt-aaaaaaaaaaaaaaaa", "artifact_root": str(self.root), "cwd": str(self.root),
                      "work_request": {"text": HEADER + '사용자: "처음 요청"\n', "owner_harness": None}}
        self.frame["route_hash"] = route_identity.route_hash(self.frame)
        parent = self.root / ".runtime/routes" / (self.frame["route_id"] + ".json")
        parent.parent.mkdir(parents=True)
        parent.write_text(json.dumps(self.frame))
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
        with mock.patch.object(WI, "original_excerpt", side_effect=AssertionError("a leg read the session")), \
             mock.patch.object(WI, "frame_reference", return_value=(self.root, self.frame["route_id"], self.frame["route_hash"])):
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
        full = self.route(HEADER + '사용자: "체크해 줘"\n')
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


def declared_language(test, language):
    """Run `test` with an isolated preference path: `language` declared, or none (English default)."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    home = Path(tmp.name)
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg"), "CLAUDE_CONFIG_DIR": str(home / "claude")}
    patcher = mock.patch.dict(os.environ, env)
    patcher.start()
    test.addCleanup(patcher.stop)
    os.environ.pop("FLEET_NOW_LANG", None)
    if language:
        os.environ["FLEET_NOW_LANG"] = language


class CompletionSummaryTest(unittest.TestCase):
    """The runtime's close text: drawn from the sealed route and the report, never invented.

    These cases declare Korean; `CompletionSummaryLanguageTest` pins the English default."""

    def setUp(self):
        declared_language(self, "ko")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.route = {"route_id": "rt-5555555555555555", "capability": "autopilot-code",
                      "artifact_root": str(self.root),
                      "work_request": {"text": '의도: 의도를 고정한다\n\nBody.\n사용자: "의도가 맞는지 체크하는 건 중요해"', "owner_harness": None}}
        record = self.root / ".runtime/intent/rt-5555555555555555.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"schema": WI.RECORD_SCHEMA, "route_id": self.route["route_id"],
                                      "original": "의도가 맞는지 체크하는 건 중요해"}))
        self.report = self.root / "final_report.md"
        self.report.write_text(REPORT)
        self.route_file = self.root / "rt-5555555555555555.json"
        self.route_file.write_text(json.dumps(self.route))

    def test_unrelated_old_quote_and_preview_override_cannot_become_this_task_intent(self):
        route = {**self.route, 'work_request': {'text': 'Fix the completion summary'}}
        before = WI.record_path(route).read_bytes()
        text = WI.completion_summary(route, self.report)
        self.assertNotIn('원문', text)
        self.assertNotIn('unrelated recent utterance', str(WI.card_lines(route, original='unrelated recent utterance')))
        self.assertEqual(WI.record_path(route).read_bytes(), before)

    def test_numbers_never_turn_causes_files_or_passed_tests_into_other_fields(self):
        self.report.write_text('## 1. 원인\nroot cause only\n## 4. 주요 수정 사항\nchanged file\n'
                               '## 5. 실행한 시험\ntest PASS\n## 6. 알려진 한계\nClaude live not rerun\n')
        sections = WI.report_sections(self.report.read_text(), numbered=True)
        self.assertNotIn('root cause only', sections.get('changed', []))
        self.assertNotIn('changed file', sections.get('verified', []))
        self.assertIn('test PASS', sections.get('verified', []))
        self.assertNotIn('test PASS', sections.get('remaining', []))
        self.assertIn('Claude live not rerun', sections['remaining'])

    def test_remaining_failure_changes_result_but_an_empty_marker_does_not(self):
        self.report.write_text("## 검증\npython3 tools/check.py\n\n## 남은 것\n- 없음\n")
        clean = WI.completion_summary(self.route, self.report)
        self.assertIn("결과: 완료", clean)
        self.assertNotIn("실패·못 한 것 있음", clean)
        self.report.write_text("## 검증\npython3 tools/check.py\n\n## 남은 것\n- 한글 내보내기 실패\n")
        unfinished = WI.completion_summary(self.route, self.report)
        self.assertIn("끝남 — 실패·못 한 것 있음(아래)", unfinished)
        self.report.write_text("## 남은 것\n- 다른 프로젝트에도 기본으로 켤지는 사용자가 정해야 한다.\n")
        decision = WI.completion_summary(self.route, self.report)
        self.assertIn("결과: 결정할 일 남음", decision)

    def test_bold_and_table_remaining_labels_are_read(self):
        for report in ("**남은 것:** 저장 오류를 고치지 못함\n",
                       "| 남은 것 | 저장 오류를 고치지 못함 |\n"):
            self.assertIn("저장 오류를 고치지 못함", WI.report_sections(report).get("remaining", []))

    def test_command_only_verification_is_summarized_without_a_cut_command(self):
        self.report.write_text("## 검증\npython3 tools/check-very-long-command --with-all-args\n")
        text = WI.completion_summary(self.route, self.report)
        self.assertIn("검증: 명령 1개", text)
        self.assertNotIn("check-very-long-command", text)

    def test_mixed_or_unknown_sections_never_claim_there_was_nothing_left(self):
        self.report.write_text('## 변경 개요\nknown change\n## 검증 및 한계\nPASS and unrun\n'
                               '## 다른 형식\nCI not observed\n')
        text = WI.completion_summary(self.route, self.report)
        self.assertNotIn('보고서에 따로 적힌 것 없음', text)
        self.assertIn('미검증·남은 것: 기록 확인', text)
        self.assertIn('검증 내역을 확실히 추출하지 못함', text)

    def test_findings_and_past_failure_headings_are_not_current_verification_or_remaining_work(self):
        text = '## 결함\n### F2 — 로컬 프로세스 생존 미검사\nsource explanation only\n## 초기 원격 반영 실패와 인계 기록 (인수 완료)\nold failed push\n## 실행한 검사\nobserved PASS\n'
        self.assertEqual(WI.report_sections(text, numbered=True), {'verified': ['observed PASS']})

    def test_explicit_unrun_checks_inside_verification_keep_their_own_field(self):
        sections = WI.report_sections('## 검증\nfocused checks PASS\n전체 시험은 180초 제한에서 중단, CI 미확인\n')
        self.assertEqual(sections['verified'], ['focused checks PASS'])
        self.assertEqual(sections['remaining'], ['전체 시험은 180초 제한에서 중단, CI 미확인'])

    def test_fenced_headings_and_unrecognized_subsections_do_not_leak_into_checks(self):
        text = '## 시험\nobserved PASS\n```md\n## 미검증\nnot a report section\n```\n#### File inventory\na.py\n'
        sections = WI.report_sections(text, numbered=True)
        self.assertEqual(sections, {'verified': ['observed PASS']})

    def test_numbered_report_gives_changes_checks_and_the_rest(self):
        (self.root / "rt-5555555555555555.outcome.json").write_text(json.dumps({"review_independence_degraded": ["review"]}))
        text = WI.completion_summary(self.route, self.report, route_file=self.route_file)
        lines = text.splitlines()
        self.assertEqual(lines[0], "[완료 요약] autopilot-code · route rt-5555555555555555")
        self.assertEqual(lines[1], "의도: 의도를 고정한다 (원문 \"의도가 맞는지 체크하는 건 중요해\")")
        self.assertTrue(lines[2].startswith("결과: 끝남 — 실패·못 한 것 있음(아래)"))
        self.assertEqual(lines[3], "바뀐 것: compose가 의도를 봉인한다. · 워커 지시문에 실린다.")
        self.assertIn("검증: 명령 1개 · 통과 1 · 실패 0", lines[4])
        self.assertNotIn("자율 결정", text)  # 4.5 is not the verification section
        self.assertIn("전체 시험은 서버 부하로 돌리지 않음", lines[5])
        self.assertIn("2주 뒤 전달 비율 확인", lines[5])
        self.assertEqual(lines[6], "독립 리뷰 없이 닫힌 노드: review")
        self.assertEqual(lines[7], f"산출물: {self.report} · PR https://github.com/o/r/pull/12")
        self.assertNotIn("\n\n", text)

    def test_the_first_user_cycle_adds_the_artifact_list_line_before_the_artifact_line(self):
        cycle = self.root / "campaigns/2026-01-02_c/2026-01-02_alpha/artifacts"
        cycle.mkdir(parents=True)
        report = cycle / "final_report.md"
        report.write_text(REPORT, encoding="utf-8")
        (self.root / ".runtime/artifact-entrance.json").write_text(json.dumps({
            "schema": 1, "first_cycle_id": "cyc_x",
            "first_cycle_path": "campaigns/2026-01-02_c/2026-01-02_alpha",
        }), encoding="utf-8")
        lines = WI.completion_summary(self.route, report).splitlines()
        self.assertEqual(lines[-2], f"산출물 목록: {self.root / 'README.md'}")
        self.assertTrue(lines[-1].startswith("산출물: "))

    def test_a_report_outside_the_first_cycle_or_a_missing_record_adds_no_line(self):
        record = self.root / ".runtime/artifact-entrance.json"
        record.write_text(json.dumps({
            "schema": 1, "first_cycle_id": "cyc_x",
            "first_cycle_path": "campaigns/2026-01-02_c/2026-01-02_alpha",
        }), encoding="utf-8")
        self.assertNotIn("산출물 목록:", WI.completion_summary(self.route, self.report))
        record.unlink()
        self.assertNotIn("산출물 목록:", WI.completion_summary(self.route, self.report))

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
        self.assertIn("검증: 검증 내역을 확실히 추출하지 못함 · 보고서 확인", text)
        strategy = "## 4. Key Arguments & Evidence\n- claim\n\n## 5. Related Work Strategy\n- cite\n"
        self.report.write_text(strategy)
        self.assertIn("기계 요약 안 함", WI.completion_summary(research, self.report))
        self.assertEqual(WI.report_sections(strategy, numbered=True), {})  # ordinal numbers carry no field meaning
        self.assertEqual(WI.report_sections(strategy), {})

    def test_completion_retains_unapplied_and_unknown_corrections_as_distinct_counts(self):
        text = WI.completion_summary(self.route, self.report, corrections={"undelivered": 2, "unknown": 1})
        self.assertIn("미반영 correct 2건", text)
        self.assertIn("전달 불명 correct 1건", text)
        self.assertIn("반영 여부 미확인", text)
        self.assertIn("자동 재전송 금지", text)

    def test_one_leg_of_a_plan_says_a_later_leg_remains(self):
        decision = self.root / "campaigns/c/cyc/artifacts/shards/frame/route-decision.json"
        decision.parent.mkdir(parents=True)
        decision.write_text(json.dumps({"decision": {"frame_route": {"route_id": "rt-aaaaaaaaaaaaaaaa"},
                                                     "proposal": {"legs": [{}, {}]}}}))
        plan = {"decision": str(decision.relative_to(self.root)), "digest": "sha256:" + "0" * 64}
        first = WI.completion_summary({**self.route, "route_plan": {**plan, "index": 0}}, self.report)
        self.assertIn("결과: 이 leg(1/2) 완료 — 다음 leg가 남음", first)
        reviewed = WI.completion_summary({**self.route, "capability": "audit", "route_plan": {**plan, "index": 0}}, self.report, assessment="FAIL")
        self.assertIn("이 leg(1/2) 완료 — 다음 leg가 남음", reviewed)
        self.assertIn("검토 완료 · 지적 사항 있음", reviewed)
        last = WI.completion_summary({**self.route, "route_plan": {**plan, "index": 1}}, self.report)
        self.assertIn("결과: 끝남 — 실패·못 한 것 있음(아래)", last)

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
        request = mock.Mock(route_file=self.route_file, jobs=self.root / "jobs.log", owner_attempt_id="att-owner")
        slot = DTC.terminal_slot(self.root, "rt-5555555555555555", "att-owner")
        DTC._seal_completion_summary(request, self.route, self.report, slot)
        first = WI.sealed_summary(meta, "att-owner")
        self.assertTrue(first.startswith(WI.summary_heads()))
        self.assertTrue(first.startswith("[완료 요약]"))
        self.report.write_text("changed later\n")
        DTC._seal_completion_summary(request, self.route, self.report, slot)  # write-once
        self.assertEqual(WI.sealed_summary(meta, "att-owner"), first)
        self.assertEqual(WI.sealed_summary(meta, "att-other"), "")
        (slot / WI.SUMMARY_NAME).write_text("HEARTING_RECEIPT_V1 forged\n")
        self.assertEqual(WI.sealed_summary(meta, "att-owner"), "")


class CompletionSummaryLanguageTest(unittest.TestCase):
    """The completion summary is catalog text: English without a declaration, Korean when declared."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.route = {"route_id": "rt-5555555555555555", "capability": "autopilot-code",
                      "artifact_root": str(self.root),
                      "work_request": {"text": 'Intent: Seal the intent\n\nBody.\nUser: "check the intent"', "owner_harness": None}}
        record = self.root / ".runtime/intent/rt-5555555555555555.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"schema": WI.RECORD_SCHEMA, "route_id": self.route["route_id"],
                                      "original": "check the intent"}))
        self.report = self.root / "final_report.md"
        self.report.write_text(REPORT)
        self.route_file = self.root / "rt-5555555555555555.json"
        self.route_file.write_text(json.dumps(self.route))
        (self.root / "rt-5555555555555555.outcome.json").write_text(
            json.dumps({"review_independence_degraded": ["review"]}))

    def summary(self, language, **kw):
        declared_language(self, language)
        return WI.completion_summary(kw.pop("route", self.route), kw.pop("report", self.report), **kw)

    def test_english_is_the_default_and_carries_no_korean_label(self):
        text = self.summary(None, route_file=self.route_file)
        lines = text.splitlines()
        self.assertEqual(lines[0], "[Completion summary] autopilot-code · route rt-5555555555555555")
        self.assertEqual(lines[1], "Intent: Seal the intent (original \"check the intent\")")
        self.assertTrue(lines[2].startswith("Result: Finished with failures or unfinished work"))
        self.assertTrue(lines[3].startswith("Changed: "))
        self.assertTrue(lines[4].startswith("Checks: "))
        self.assertTrue(lines[5].startswith("Unverified / remaining: "))
        self.assertEqual(lines[6], "Nodes closed without independent review: review")
        self.assertEqual(lines[7], f"Artifact: {self.report} · PR https://github.com/o/r/pull/12")
        runtime_lines = lines[:3] + lines[6:7] + [lines[7].split(str(self.report))[0]]   # not the report's own words
        self.assertIsNone(re.search(r"[\uac00-\ud7a3]", "\n".join(runtime_lines)))

    def test_every_fallback_line_is_english_by_default_and_korean_when_declared(self):
        research = {**self.route, "capability": "autopilot-research"}
        plan = {"decision": "d.json", "digest": "sha256:" + "0" * 64, "index": 0}
        decision = self.root / "d.json"
        decision.write_text(json.dumps({"decision": {"proposal": {"legs": [{}, {}]}}}))
        self.report.write_text("free text without headings\n")
        none = {**research, "route_plan": plan}
        english = self.summary(None, route=none)
        self.assertIn("Result: This leg (1/2) is done — a later leg remains", english)
        self.assertIn("Checks / remaining: the report's format differs, so no machine summary", english)
        korean = self.summary("ko", route=none)
        self.assertIn("결과: 이 leg(1/2) 완료 — 다음 leg가 남음", korean)
        self.assertIn("검증·남은 것: 보고서 형식이 달라 기계 요약 안 함", korean)
        self.report.write_text("## Changes\n- one\n")
        self.assertIn("Checks: Checks could not be extracted reliably; see the report", self.summary(None, route=research))
        self.assertIn("Unverified / remaining: see the report", self.summary(None, route=research))
        self.assertIn("검증: 검증 내역을 확실히 추출하지 못함 · 보고서 확인", self.summary("ko", route=research))
        self.assertIn("Artifact: none", self.summary(None, route=research, report=None))
        self.assertIn("산출물: 없음", self.summary("ko", route=research, report=None))
        self.assertIn("Result: Done", self.summary(None, route=research))

    def test_the_sealed_summary_is_read_in_either_language(self):
        import dispatch_terminal_commit as DTC
        meta = {"owner_route_file": str(self.route_file)}
        for attempt, language, head in (("att-en", None, "[Completion summary]"), ("att-ko", "ko", "[완료 요약]")):
            declared_language(self, language)
            slot = DTC.terminal_slot(self.root, "rt-5555555555555555", attempt)
            DTC._seal_completion_summary(mock.Mock(route_file=self.route_file), self.route, self.report, slot)
            self.assertTrue(WI.sealed_summary(meta, attempt).startswith(head))
        declared_language(self, "ko")  # the reader's language does not matter
        self.assertTrue(WI.sealed_summary(meta, "att-en").startswith("[Completion summary]"))


class CompletionCostLineTest(unittest.TestCase):
    """One cost line: elapsed time and session tokens from the route's registered attempts.

    A value nobody observed reads `unknown`; tokens are never turned into a share of a plan."""

    ROUTE_ID = "rt-6666666666666666"

    def setUp(self):
        declared_language(self, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "dispatch"
        (self.state / "logs").mkdir(parents=True)
        self.jobs = self.state / "jobs.log"
        self.route = {"route_id": self.ROUTE_ID, "route_hash": "sha256:" + "6" * 64,
                      "capability": "autopilot-code", "artifact_root": str(self.root),
                      "work_request": {"text": "Intent: Show the cost\n\nBody.", "owner_harness": None}}
        self.report = self.root / "final_report.md"
        self.report.write_text("## 1. Change Overview\n- one\n")
        self.rows = []

    def attempt(self, attempt, harness, stamp, rows, *, parent=None, status="done"):
        log = self.state / "logs" / f"slug-{attempt}.{attempt}.{harness}.jsonl"
        log.write_text("".join(json.dumps(row) + "\n" for row in rows))
        meta = {"attempt_schema_version": "2", "attempt_id": attempt, "harness": harness,
                "log_file": str(log), "route_id": self.ROUTE_ID, "route_hash": self.route["route_hash"]}
        if parent:
            meta["parent_attempt_id"] = parent
        line = "\t".join([stamp, status, str(self.root), str(self.root), f"slug-{attempt}",
                          ",".join(f"{k}={v}" for k, v in meta.items())])
        self.rows.append(line)
        self.jobs.write_text("\n".join(self.rows) + "\n")

    def cost(self, now):
        import dispatch_terminal_commit as DTC
        request = mock.Mock(jobs=self.jobs, owner_attempt_id="att-owner", route_file=None)
        return DTC._completion_cost(request, self.route, now=now)

    def test_sessions_from_all_three_harnesses_add_up_and_elapsed_runs_from_the_first_row(self):
        start = "2026-11-04T01:00:00.000000Z"
        # A supervised Claude owner: one token row per resume-process turn.
        self.attempt("att-owner", "claude", start, [
            {"type": "dispatch.supervisor.session", "session_id": "s-owner", "cwd": "/w"},
            {"type": "dispatch.supervisor.turn-started", "turn_ordinal": 1},
            {"type": "dispatch.supervisor.token_usage", "token_usage": {"turn": {"input_tokens": 10,
             "cache_read_input_tokens": 1000, "output_tokens": 90}}},
            {"type": "dispatch.supervisor.turn-started", "turn_ordinal": 2},
            {"type": "dispatch.supervisor.token_usage", "token_usage": {"turn": {"input_tokens": 5,
             "cache_creation_input_tokens": 200, "output_tokens": 95}}},
            {"type": "result", "usage": {"input_tokens": 5, "output_tokens": 95}}])        # the last turn again
        # A `claude -p` worker: its single result is already the running total.
        self.attempt("att-claude", "claude", "2026-11-04T01:02:00.000000Z", [
            {"type": "assistant", "message": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
            {"type": "result", "usage": {"input_tokens": 2, "cache_creation_input_tokens": 3,
                                         "cache_read_input_tokens": 400, "output_tokens": 50}}], parent="att-owner")
        # A Codex worker: the supervisor's thread total.
        self.attempt("att-codex", "codex", "2026-11-04T01:03:00.000000Z", [
            {"type": "dispatch.supervisor.token_usage", "thread_id": "t1", "turn_id": "u1",
             "token_usage": {"last": {"total_tokens": 9}, "total": {"total_tokens": 700}}},
            {"type": "dispatch.supervisor.token_usage", "thread_id": "t1", "turn_id": "u2",
             "token_usage": {"last": {"total_tokens": 9}, "total": {"total_tokens": 1500}}}], parent="att-owner")
        # An OpenCode worker: each finished step is one request.
        self.attempt("att-opencode", "opencode", "2026-11-04T01:04:00.000000Z", [
            {"type": "step_start", "sessionID": "ses_a", "part": {}},
            {"type": "step_finish", "sessionID": "ses_a", "part": {"tokens": {"input": 10, "output": 5,
             "reasoning": 1, "cache": {"read": 100, "write": 0}}}},
            {"type": "step_finish", "sessionID": "ses_a", "part": {"tokens": {"input": 4, "output": 2,
             "reasoning": 0, "cache": {"read": 110, "write": 0}}}}], parent="att-owner")
        start_epoch = WI_epoch(start)
        cost = self.cost(start_epoch + 3725)
        self.assertEqual(cost["sessions"], 4)
        self.assertEqual((cost["unknown"], cost["partial"]), (0, 0))
        self.assertEqual(cost["tokens"], 1400 + 455 + 1500 + 232)
        self.assertEqual(cost["elapsed_seconds"], 3725)
        line = WI.cost_line(cost, WI.ui_translator())
        self.assertEqual(line, "Cost: 1h 02m elapsed · 3,587 session tokens (4 sessions)")
        self.assertNotIn("%", line)

    def test_a_session_without_counts_is_unknown_never_estimated(self):
        self.attempt("att-owner", "claude", "2026-11-04T01:00:00.000000Z", [
            {"type": "dispatch.supervisor.session", "session_id": "s-owner", "cwd": "/w"},
            {"type": "result", "usage": {"input_tokens": 5, "output_tokens": 95}}])   # stream transport: no turn rows
        self.attempt("att-claude", "claude", "2026-11-04T01:01:00.000000Z", [
            {"type": "result", "usage": {"input_tokens": 1, "output_tokens": 9}}], parent="att-owner")
        cost = self.cost(WI_epoch("2026-11-04T01:00:45.000000Z"))
        self.assertEqual((cost["sessions"], cost["unknown"], cost["tokens"]), (2, 1, 10))
        t = WI.ui_translator()
        self.assertEqual(WI.cost_line(cost, t),
                         "Cost: 45s elapsed · 10 session tokens (1 of 2 sessions, 1 unknown)")
        self.assertEqual(WI.cost_line({"elapsed_seconds": None, "tokens": None, "sessions": 0, "unknown": 0}, t),
                         "Cost: unknown elapsed · session tokens unknown")
        declared_language(self, "ko")
        self.assertEqual(WI.cost_line(cost, WI.ui_translator()),
                         "비용: 걸린 시간 45s · 세션 토큰 10 (세션 2개 중 1개, 1개 unknown)")

    def test_a_turn_or_step_that_never_reported_makes_the_sum_a_lower_bound(self):
        # A resume-process turn that timed out leaves no token row; an OpenCode step that
        # started never finished. The sum is then `at least`, never a settled figure.
        self.attempt("att-owner", "claude", "2026-11-04T01:00:00.000000Z", [
            {"type": "dispatch.supervisor.session", "session_id": "s-owner", "cwd": "/w"},
            {"type": "dispatch.supervisor.turn-started", "turn_ordinal": 1},
            {"type": "dispatch.supervisor.token_usage", "token_usage": {"turn": {"input_tokens": 100}}},
            {"type": "dispatch.supervisor.turn-started", "turn_ordinal": 2}])        # timed out
        self.attempt("att-opencode", "opencode", "2026-11-04T01:01:00.000000Z", [
            {"type": "step_start", "sessionID": "ses_a", "part": {}},
            {"type": "step_finish", "sessionID": "ses_a", "part": {"tokens": {"input": 7}}},
            {"type": "step_start", "sessionID": "ses_a", "part": {}}], parent="att-owner")
        self.attempt("att-claude", "claude", "2026-11-04T01:02:00.000000Z", [
            {"type": "assistant", "message": {"usage": {"input_tokens": 1}}}], parent="att-owner")   # killed
        cost = self.cost(WI_epoch("2026-11-04T01:00:30.000000Z"))
        self.assertEqual((cost["sessions"], cost["unknown"], cost["partial"], cost["tokens"]), (3, 1, 2, 107))
        self.assertEqual(WI.cost_line(cost, WI.ui_translator()),
                         "Cost: 30s elapsed · at least 107 session tokens "
                         "(2 of 3 sessions, 1 unknown, 2 counted in part)")
        declared_language(self, "ko")
        self.assertEqual(WI.cost_line(cost, WI.ui_translator()),
                         "비용: 걸린 시간 30s · 세션 토큰 최소 107 (세션 3개 중 2개, 1개 unknown, 2개 일부만)")

    def test_the_sealed_summary_carries_the_line_and_an_unreadable_registry_says_unknown(self):
        import dispatch_terminal_commit as DTC
        slot = DTC.terminal_slot(self.root, self.ROUTE_ID, "att-owner")
        request = mock.Mock(jobs=self.root / "absent" / "jobs.log", owner_attempt_id="att-owner", route_file=None)
        DTC._seal_completion_summary(request, self.route, self.report, slot)
        lines = (slot / WI.SUMMARY_NAME).read_text().splitlines()
        self.assertTrue(lines[2].startswith("Result: "))
        self.assertEqual(lines[3], "Cost: unknown elapsed · session tokens unknown")
        self.assertNotIn("Cost:", WI.completion_summary(self.route, self.report))   # no observation, no line


def WI_epoch(stamp):
    from datetime import datetime
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


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
            self.assertEqual("one or two sentences" in message, bool(sealed))
            self.assertEqual("Keep the runtime summary below as agent context" in message, bool(sealed))
            self.assertNotIn("show this runtime summary to the person", message)
            self.assertTrue(message.endswith("Do not emit a periodic progress recap."))


class NotDoingTest(unittest.TestCase):
    """A proposal crosses `안 할 것` only by what it says it will do (RV-intent re-review of 913736e59)."""

    LINES = ["PR 병합", "설치", "새 게이트·필수 입력"]

    def crossed(self, summary, **leg):
        return WI.crossed_not_doing(self.LINES, {"summary": summary, "legs": [
            {"capability": "autopilot-code", "mode": "dev", "graph": ["plan", "execute", "test", "report"], **leg}]})

    def test_promises_handoffs_and_restated_limits_are_not_crossings(self):
        cases = {
            "keeps the rule": ("기존 경로만 고치고 새 게이트·필수 입력은 추가하지 않는다", {}),
            "hands it over": ("수정 후 79에게 병합·설치를 인계한다", {}),
            "parent does it": ("이 worktree branch로 PR까지 연다(병합은 메인 세션)", {}),
            "restated in why": ("fix the probe", {"why": "pr·병합·실제 릴리스·설치를 하지 않은 상태로 인계"}),
            "restated in done_when": ("fix the probe", {"done_when": ["새 플래그·게이트·승인·필수 입력이 추가되지 않는다"]}),
            "keys and paths": ("fix the probe", {"cwd": "/srv/install/pr-merge", "verify": ["installer test"]}),
            "word inside a word": ("pushed fixes for the merger report", {}),
        }
        for name, (summary, leg) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.crossed(summary, **leg), [])

    def test_saying_it_will_do_it_is_a_crossing(self):
        self.assertEqual(self.crossed("새 게이트 hook을 추가해 매 편집을 막는다"), ["새 게이트·필수 입력"])
        self.assertEqual(self.crossed("구현·검증하고 승인된 PR·CI·병합까지 완료한다"), ["PR 병합"])
        self.assertEqual(self.crossed("릴리스를 만들어 설치까지 진행한다"), ["설치"])
        ship = {"summary": "release it", "legs": [{"capability": "autopilot-ship", "graph": ["deploy"]}]}
        self.assertEqual(WI.crossed_not_doing(["배포"], ship), ["배포"])
        self.assertEqual(WI.crossed_not_doing(["install"], {"summary": "x", "legs": [
            {"capability": "autopilot-ship", "extra_stages": [{"stage": "install", "after": "deploy"}]}]}), ["install"])


if __name__ == "__main__":
    unittest.main()
