#!/usr/bin/env python3
"""Synthetic PR snapshots and review texts; no GitHub, network or session access."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("pr_chain", Path(__file__).with_name("pr-chain.py"))
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)

HEAD = "8779aadca36989d0933ae8314cacbd44db30359c"
OK = [{"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}]
RUNNING = [{"__typename": "CheckRun", "name": "test", "status": "IN_PROGRESS", "conclusion": None}]
RED = [{"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "FAILURE"}]


def snap(checks=OK, **extra):
    row = {"number": 1425, "state": "OPEN", "isDraft": False, "baseRefName": "main",
           "headRefOid": HEAD, "mergeable": "MERGEABLE", "statusCheckRollup": checks}
    row.update(extra)
    return row


class DecideTest(unittest.TestCase):
    def state(self, snapshot, review=None):
        return C.decide(snapshot, review)["state"]

    def test_pr_states_come_first(self):
        self.assertEqual(self.state(snap(state="MERGED")), "merged")
        self.assertEqual(self.state(snap(state="CLOSED")), "closed")
        self.assertEqual(self.state(snap(isDraft=True)), "draft")
        self.assertEqual(self.state(snap(RED), f"최종 판정: 병합 가능 {HEAD}"), "ci-failed")
        self.assertEqual(self.state(snap(mergeable="CONFLICTING")), "conflict")

    def test_author_finish_requests_review(self):
        row = C.decide(snap(RUNNING))
        self.assertEqual((row["state"], row["role"]), ("needs-review", "author"))
        self.assertIn("PR #1425 head 8779aadca369", row["next"])

    def test_initial_note_is_not_final(self):
        text = "# 검토\n초도 판정: 차단0, 최종 병합 가능 판정은 아직 없다.\n"
        self.assertEqual(self.state(snap(), text), "awaiting-review")

    def test_fix_verdict_returns_to_author(self):
        row = C.decide(snap(), f"**최종 판정: 수정 후 병합.** head {HEAD[:8]}\n")
        self.assertEqual((row["state"], row["role"]), ("fix-requested", "author"))
        self.assertEqual(self.state(snap(), f"## Verdict: FAIL\nhead {HEAD}\n"), "fix-requested")

    def test_merge_verdict_must_name_current_head(self):
        text = "**최종 판정: 병합 가능.** exact head `3918ef4d1234`\n"
        row = C.decide(snap(), text)
        self.assertEqual((row["state"], row["review"]), ("review-stale", "merge-other-head"))

    def test_first_final_line_is_current_and_history_is_ignored(self):
        text = (f"**최종 판정: 병합 가능.** exact head `{HEAD}` 차단0, 수정 불필요.\n\n"
                "## 이력\n최종 판정: 수정 후 병합 (3918ef4d)\n")
        self.assertEqual(self.state(snap(), text), "ready-to-publish")

    def test_merge_verdict_with_running_ci_names_merge_line(self):
        row = C.decide(snap(RUNNING), f"Final verdict: mergeable at {HEAD[:12]}\n")
        self.assertEqual((row["state"], row["role"], row["ci"]), ("awaiting-ci", "publisher", "pending"))
        self.assertIn("hearting run merge-line 1425", row["next"])

    def test_ready_to_publish(self):
        row = C.decide(snap(), f"## Verdict: PASS\nhead {HEAD}\n")
        self.assertEqual((row["state"], row["ci"], row["review"]), ("ready-to-publish", "success", "merge"))
        line = C.format_line(row)
        self.assertTrue(line.startswith("pr_chain=ready-to-publish pr=#1425 head=8779aadca369 ci=success"))


class FinishLineTest(unittest.TestCase):
    def test_no_github_remote_makes_no_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            with mock.patch.object(C, "snapshot", side_effect=AssertionError("network")):
                self.assertEqual(C.finish_line(tmp), "")
        self.assertEqual(C.finish_line(None), "")

    def test_open_pr_prints_next_and_failures_stay_silent(self):
        with mock.patch.object(C, "github_remote", return_value=True), \
             mock.patch.object(C.subprocess, "run", return_value=mock.Mock(stdout="topic\n")), \
             mock.patch.object(C, "snapshot", return_value=snap(RUNNING)) as view:
            line = C.finish_line("/repo")
        view.assert_called_once()
        self.assertTrue(line.startswith("next: pr_chain=needs-review pr=#1425"))
        with mock.patch.object(C, "github_remote", return_value=True), \
             mock.patch.object(C.subprocess, "run", return_value=mock.Mock(stdout="topic\n")), \
             mock.patch.object(C, "snapshot", side_effect=RuntimeError("no pull requests found")):
            self.assertEqual(C.finish_line("/repo"), "")
        with mock.patch.object(C, "github_remote", return_value=True), \
             mock.patch.object(C.subprocess, "run", return_value=mock.Mock(stdout="topic\n")), \
             mock.patch.object(C, "snapshot", return_value=snap(state="MERGED")):
            self.assertEqual(C.finish_line("/repo"), "")


OLD = "3918ef4d1234abcd5678ef901234abcd5678ef90"


class ReviewRegressionTest(unittest.TestCase):
    """RV-PR1430-1110 counterexamples: rejection never publishes; real verdict forms are read."""

    def state(self, review, snapshot=None):
        return C.decide(snapshot or snap(), review)["state"]

    def test_negated_or_conditional_merge_is_never_ready(self):
        for text in (f"Final verdict: not mergeable ({HEAD[:7]})\n",
                     f"최종 판정: 병합 가능하지 않음 {HEAD[:7]}\n",
                     f"Verdict: approve with changes {HEAD[:7]}\n"):
            with self.subTest(text=text):
                self.assertEqual(self.state(text), "fix-requested")

    def test_explicit_rejection_returns_to_author(self):
        for text in (f"최종 판정: 병합 불가 {HEAD[:7]}\n", f"Verdict: do not merge {HEAD[:7]}\n"):
            with self.subTest(text=text):
                self.assertEqual(self.state(text), "fix-requested")

    def test_token_inside_a_word_is_not_a_verdict(self):
        row = C.decide(snap(), f"Verdict: bypassed {HEAD[:7]}\n")
        self.assertEqual((row["state"], row["review"]), ("awaiting-review", "none"))

    def test_appended_rereview_of_current_head_wins(self):
        text = (f"최종 판정: 병합 가능 (head {OLD[:7]})\n\n본문\n\n"
                f"## 재검토 head {HEAD[:7]}\n\n최종 판정: 수정 후 병합\n")
        self.assertEqual(self.state(text), "fix-requested")

    def test_head_mentioned_away_from_the_verdict_is_stale(self):
        text = (f"최종 판정: 병합 가능 head {OLD[:7]}\n\n"
                f"author가 {HEAD[:7]}로 push, 재검토 전\n")
        self.assertEqual(self.state(text), "review-stale")

    def test_conflicting_verdicts_on_the_current_head_are_not_read(self):
        text = (f"최종 판정: 병합 가능 {HEAD[:7]}\n\n## 이력\n\n"
                f"최종 판정: 수정 후 병합 {HEAD[:7]}\n")
        row = C.decide(snap(), text)
        self.assertEqual((row["state"], row["review"]), ("awaiting-review", "none"))
        self.assertIn("person", row["next"])

    def test_unreadable_verdict_names_a_next_action_for_a_person(self):
        row = C.decide(snap(), f"verdict: BLOCKED {HEAD[:7]}\n")
        self.assertEqual((row["state"], row["review"], row["role"]), ("awaiting-review", "none", "reviewer"))
        self.assertIn("BLOCKED", row["next"])
        self.assertIn("person", row["next"])

    def test_assignment_first_line_form(self):
        self.assertEqual(self.state(f"병합 가능 {HEAD[:7]}\n\n# 검토\n"), "ready-to-publish")
        self.assertEqual(self.state(f"병합 가능 — 검토 head {HEAD[:7]} (PR #1425)\n"), "ready-to-publish")
        self.assertEqual(self.state(f"수정 후 병합 — 검토 head {HEAD[:7]}\n"), "fix-requested")

    def test_bare_verdict_label_form(self):
        text = f"# PR #1427 검토\n\n**판정: 수정 후 병합 — P2 1건.** exact head `{HEAD}`\n"
        self.assertEqual(self.state(text), "fix-requested")
        text = f"# PR #1424 검토\n\n**판정: 병합 가능. 차단 지적 없음.** exact head `{HEAD}`\n"
        self.assertEqual(self.state(text), "ready-to-publish")

    def test_title_verdict_form(self):
        text = f"# #1421 Fleet 독립 검토 — 최종 병합 가능\n\n대상 head `{HEAD}`, CI 13/13 SUCCESS.\n"
        self.assertEqual(self.state(text), "ready-to-publish")

    def test_draft_notes_stay_unread(self):
        for text in (f"**변경분 판정: 원 두 P2 종결. 최종 병합 가능은 CI 대기.** head `{HEAD}`\n",
                     f"**초안: 수정 필요(P2 2건). 최종 병합 가능 판정 전이다.** 대상 `{HEAD}`\n"):
            with self.subTest(text=text):
                self.assertEqual(self.state(text), "awaiting-review")

    def test_other_sha_sharing_a_short_prefix_is_not_the_head(self):
        other = HEAD[:7] + "0" * 33
        self.assertEqual(self.state(f"최종 판정: 병합 가능 exact `{other}`\n"), "review-stale")


class FinishCapabilityTest(unittest.TestCase):
    def test_only_source_routes_print_the_author_line(self):
        with mock.patch.object(C, "github_remote", return_value=True), \
             mock.patch.object(C.subprocess, "run", return_value=mock.Mock(stdout="topic\n")), \
             mock.patch.object(C, "snapshot", return_value=snap(RUNNING)) as view:
            self.assertTrue(C.finish_line("/repo", capability="autopilot-code").startswith("next: pr_chain="))
            for capability in ("audit", "autopilot-research", "analyze-project"):
                with self.subTest(capability=capability):
                    self.assertEqual(C.finish_line("/repo", capability=capability), "")
        view.assert_called_once()



R2_HEAD = "f54d2573fb49e86619bf87dec771fb03210f8412"
R2_OLD = "4b45ba02f07b3441f138381f8102ae2e9fa8d65f"
H, O = R2_HEAD[:7], R2_OLD[:7]
FIX_OR_PERSON = ("fix-requested", "awaiting-review")
STALE_OR_PERSON = ("review-stale", "awaiting-review")
# RV-PR1430-R2-1110 probe_pr1430.py cases, verbatim: (name, review text, accepted states).
R2_CASES = [
    ("orig-not-mergeable", f"Final verdict: not mergeable ({H})\n", ("fix-requested",)),
    ("orig-korean-negated", f"최종 판정: 병합 가능하지 않음 {H}\n", ("fix-requested",)),
    ("orig-with-changes", f"Verdict: approve with changes {H}\n", ("fix-requested",)),
    ("orig-rejection", f"최종 판정: 병합 불가 {H}\n", ("fix-requested",)),
    ("orig-do-not-merge", f"Verdict: do not merge {H}\n", ("fix-requested",)),
    ("orig-bypassed", f"Verdict: bypassed {H}\n", ("awaiting-review",)),
    ("orig-current-rereview", f"최종 판정: 병합 가능 (head {O})\n\n본문\n\n## 재검토 head {H}\n\n최종 판정: 수정 후 병합\n",
     ("fix-requested",)),
    ("orig-old-head-away", f"최종 판정: 병합 가능 head {O}\n\nauthor가 {H}로 push, 재검토 전\n", ("review-stale",)),
    ("orig-blocked", f"Verdict: BLOCKED — head {H}\n", ("awaiting-review",)),
    ("orig-conflicting", f"최종 판정: 병합 가능 {H}\n\n최종 판정: 수정 후 병합 {H}\n", ("awaiting-review",)),
    ("form-first", f"병합 가능 — 검토 head {H}\n", ("ready-to-publish",)),
    ("form-label", f"# 독립 검토\n\n**판정: 수정 후 병합 — P2 1건.** exact head `{R2_HEAD}`\n", ("fix-requested",)),
    ("form-title", f"# PR #1430 독립 검토 — 최종 병합 가능\n\n대상 head `{R2_HEAD}`, CI 13/13 SUCCESS.\n",
     ("ready-to-publish",)),
    ("normal-nonblocking-notes", f"최종 판정: 병합 가능. 차단0, 수정 불필요 — head {H}\n", ("ready-to-publish",)),
    ("conditional-ko-parenthesis", f"최종 판정: 병합 가능(단, P1 수정 후 재검토 필요) — head {H}\n", FIX_OR_PERSON),
    ("conditional-ko-dash", f"최종 판정: 병합 가능 — 단, P1 수정 후 재검토가 필요하다. head {H}\n", FIX_OR_PERSON),
    ("conditional-en-parenthesis", f"Final verdict: approved (with changes) — head {H}\n", FIX_OR_PERSON),
    ("conditional-en-comma", f"Final verdict: mergeable, but do not merge until the P1 fix is reviewed. head {H}\n",
     FIX_OR_PERSON),
    ("conditional-en-within-clause", f"Final verdict: mergeable only after the P1 correction is reviewed — head {H}\n",
     FIX_OR_PERSON),
    ("negative-ko-deferred", f"최종 판정: 병합 가능 여부는 보류 — head {H}\n", FIX_OR_PERSON),
    ("stale-ko-same-paragraph", f"최종 판정: 병합 가능 — head {O}\n작성자가 {H}로 push했으나 새 head는 아직 재검토하지 않았다.\n",
     STALE_OR_PERSON),
    ("stale-en-same-paragraph", f"Final verdict: mergeable — reviewed head {O}\nThe author pushed {H}; this new head has not been reviewed.\n",
     STALE_OR_PERSON),
    ("stale-ko-same-line", f"최종 판정: 병합 가능 — 검토 head {O} (현재 {H}는 미검토)\n", STALE_OR_PERSON),
    ("stale-en-heading", f"## PR #1430 current head {H}\n\nFinal verdict: mergeable — reviewed head {O}\n", STALE_OR_PERSON),
    ("unknown-line", f"Verdict: INCONCLUSIVE — head {H}\n", ("awaiting-review",)),
    ("unknown-form", f"검토 결과를 확정할 수 없습니다. head {H}\n", ("awaiting-review",)),
]

# Excerpts of real reviewer reports (RV-*-1110): each report's verdict block, verbatim.
REAL_REVIEWS = [
    ('RV-BC-OPEN32-1110', 'cf08b89564a1770b3e0105651bc792d443a4bb64', 'ready-to-publish',
     '# PR #1427 — 셸 블록 open 집계 독립 검토\n\n**최종 판정: 병합 가능.** 보정 exact head `cf08b89564a1770b3e0105651bc792d443a4bb64`, base `2a447b81153b63d14e972ffa3d213be72861c867`. 원 P2의 두 증상 종결·delta 추가 필수0, 새 exact CI42751637310 13/13 success와 보정 owner-terminal 마감을 합쳤다. 아래 최초f2a의 수정 후 병합·두 실패는 이력으로 보존한다.\n'),
    ('RV-PR1428-1110', 'd2803c3f5304b43c32d56ccaf2c631269809c7a9', 'fix-requested',
     '수정 후 병합 — 검토 head d2803c3 (PR #1428, branch steward-progress-solo-1110)\n'),
    ('RV-PR1431-1110', 'e3da694', 'ready-to-publish',
     '병합 가능 — 검토 head e3da694 (PR #1431, launcher-first-update-1110)\n'),
    ('RV-BC-CODEX-RESUME-SELECTION-1110', 'c16093de89a88461a534ccee01ce82efdf16aca3', 'ready-to-publish',
     '# PR #1433 — /resume native 현재 선택 독립 검토\n\n**최종 판정: 병합 가능.** exact head `c16093de89a88461a534ccee01ce82efdf16aca3`, base `84bb2ed4f615d0a5d5ce4383e6923357b0d864be`. 원 P2 종결·delta 추가 필수0에 새 CI42752499175 attempt1 13/13 success·작성자 정상 마감·공유 PRD 정상 admitted/원본 byte 일치를 합쳤다. 아래 원 실패·중간 판정은 이력으로 보존하며 구형 host 즉시 반영/운영 답 소비 완료로 확대하지 않는다.\n'),
    ('RV-PR1429-1110', 'a0e73fd49c88e15c6bd4c891ea9b36c301e159be', 'fix-requested',
     '판정: 수정 후 병합 (P1 3건, P2 2건, P3 4건) · head a0e73fd\n'),
    ('RV-PR1428-R2-1110', 'b9932bb', 'ready-to-publish',
     '병합 가능 — 재검토 head b9932bb (PR #1428, branch steward-progress-solo-1110)\n'),
    ('RV-PR1434-1110', 'c0b45c1fc252735fa32047183a1d5aae1be7626f', 'fix-requested',
     '최종 판정: FAIL (수정 요청) — PR #1434 head c0b45c1\n'),
    ('RV-BC-SHELL-V33-1110', '9b61357c5ac90bab1d6bbab1c6bba51867a1a0b3', 'awaiting-review',
     '# PR #1432 — 승인 v3.3 UI 독립 검토 (준비 head)\n\n**현재 출판 상태: 보류 — c0의 새 사용자 지시(25b65335a3ec00660878af8d27b55f35), 세 하네스 표시·조작 일관성과 표시 보정 delta 대기.** 아래의 병합 가능 판정은 기존 exact9b613에 대한 이력이며 새 head의 수용/출판 지시가 아니다.\n'),
    ('RV-BC-FLEET-PEER-RESUMED-1110', 'c0825319c1553ba8766f68c4fae3d3810338c4d6', 'ready-to-publish',
     '# PR #1424 — 재개된 native 주소·Fleet 위치 독립 검토\n\n**최종 판정: 병합 가능.** exact head `c0825319c1553ba8766f68c4fae3d3810338c4d6`의 Checks42732390707 attempt1 13/13 SUCCESS(marker 포함)와 작성자 정상 마감을 합쳤다. 추가 필수 수정은 없다. 아래 초도 관측과 원 실패 해석은 이력으로 보존한다.\n'),
    ('RV-BC-WRAPPED-CLAUDE-SEAT-1110', '8779aadca36989d0933ae8314cacbd44db30359c', 'ready-to-publish',
     '**최종 판정: 병합 가능.** exact head `8779aadca36989d0933ae8314cacbd44db30359c`의 CI42735775437 attempt1 13/13 SUCCESS(marker 포함)와 작성자 정상 마감을 합쳤다. 원 P2는 수정하지 않은 재현의2경계 PASS로 닫혔으며 추가 필수 수정은 없다. 아래 `3918ef4d`의 수정 후 병합 판정·2FAIL과 당시CI상태는 이력으로 보존한다. 옛3918green으로 대체하지 않았다.\n'),
    ('RV-BC-WRAPPED-FOREGROUND-RETIRE-1110', 'c0482f772733fc98dbf259595aec21235ecbc5a5', 'ready-to-publish',
     '# PR #1426 — wrapped foreground·retire 독립 검토\n\n**최종 판정: 병합 가능.** exact head `c0482f772733fc98dbf259595aec21235ecbc5a5`, base `2a447b81153b63d14e972ffa3d213be72861c867`. 원 P2 종결·delta 추가 필수 수정 0, 새 exact CI42745217494 attempt1 13/13 success와 작성자 정상 finish를 합쳤다. 아래 최초 `a36effffd`의 수정 후 병합 판정·실패 및 중간 대기 시점은 이력으로 보존한다.\n'),
    ('RV-CAIRN11-R2-1110', '21cca3e747dc6024c58ba033ad027b1845068d77', 'awaiting-review',
     '# Cairn PR #11 권고 보정 변경분 재검토 — 2026-11-10\n\n**판정: 수정 후 진행 — R1~R3 종결, R4 보정의 운영 회귀 P2 1건. 현재21cca3e를 배포 가능한 최종 PASS로 판정하지 않는다.** 범위는 `994e9eb19b0ae33fe13c9c2b697612ab56f8e44a..21cca3e747dc6024c58ba033ad027b1845068d77`의14파일뿐이다. 이전994e9eb의 PASS·비차단권고4건 판정은 이력으로 유지하며, 이번 R4 delta에서 생긴 동작을 구분한다.\n'),
    ('RV-PR1430-1110', '4b45ba02f07b3441f138381f8102ae2e9fa8d65f', 'fix-requested',
     '최종 판정: 수정 후 병합 — head 4b45ba0 (4b45ba02f07b3441f138381f8102ae2e9fa8d65f), P1 1건 · P2 2건\n'),
]


class CertainMergeTest(unittest.TestCase):
    """RV-PR1430-R2-1110: a merge is read only when certain; real reports keep their reading."""

    def state(self, review, head=R2_HEAD):
        return C.decide(snap(headRefOid=head), review)["state"]

    def test_probe_cases(self):
        for name, text, accepted in R2_CASES:
            with self.subTest(name=name):
                self.assertIn(self.state(text), accepted)

    def test_real_reports_keep_their_reading(self):
        self.assertEqual(len(REAL_REVIEWS), 13)
        for name, head, expected, text in REAL_REVIEWS:
            with self.subTest(name=name):
                self.assertEqual(self.state(text, head), expected)

    def test_condition_after_the_bold_verdict_is_not_a_merge(self):
        for text in (f"**최종 판정: 병합 가능.** head {H}. 단, P1 수정 후 재검토 필요.\n",
                     f"**Final verdict: mergeable.** head {H}\nPending the P1 fix, do not merge.\n",
                     f"**최종 판정: 병합 가능.** head {H}\n다만 CI 재확인 전까지 보류.\n"):
            with self.subTest(text=text):
                self.assertIn(self.state(text), FIX_OR_PERSON)

    def test_head_on_the_verdict_line_is_not_replaced_by_its_block(self):
        text = f"Final verdict: mergeable — base {O}, head {H}\n"
        self.assertEqual(self.state(text), "ready-to-publish")
        text = f"## current head {H}\n\nFinal verdict: mergeable — head {O}\n"
        self.assertEqual(C.review_reading(text, R2_HEAD)["names_head"], False)

    def test_review_verdict_helper(self):
        self.assertEqual(C.review_verdict(f"최종 판정: 병합 가능 — head {H}", R2_HEAD), ("merge", True))
        self.assertEqual(C.review_verdict(f"최종 판정: 병합 가능(단, P1 수정 후 재검토 필요) — head {H}", R2_HEAD),
                         ("fix", True))
        self.assertEqual(C.review_verdict("", R2_HEAD), ("none", False))


if __name__ == "__main__":
    unittest.main()
