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


if __name__ == "__main__":
    unittest.main()
