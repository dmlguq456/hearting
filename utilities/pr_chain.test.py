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


if __name__ == "__main__":
    unittest.main()
