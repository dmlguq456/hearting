#!/usr/bin/env python3
"""Synthetic PR snapshots and review texts; no GitHub, network or session access."""
import contextlib
import importlib.util
import io
import json
import os
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
        self.assertEqual(self.state(snap(RED), f"최종 판정: 병합 가능 — head {HEAD[:7]}"), "ci-failed")
        self.assertEqual(self.state(snap(mergeable="CONFLICTING")), "conflict")

    def test_author_finish_requests_review(self):
        row = C.decide(snap(RUNNING))
        self.assertEqual((row["state"], row["role"]), ("needs-review", "author"))
        self.assertIn("PR #1425 head 8779aadca369", row["next"])

    def test_closed_merge_is_ready_or_waits_for_ci(self):
        for line in (f"최종 판정: 병합 가능 — head {HEAD[:7]}", f"**Final verdict: mergeable — head {HEAD}.**",
                     f"  **최종 판정: 병합 가능** — head `{HEAD[:12]}`  "):
            with self.subTest(line=line):
                row = C.decide(snap(), f"# 검토\n\n{line}\n\n해설: 수정 전까지 보류했던 이력, This is not an approval.\n")
                self.assertEqual((row["state"], row["review"], row["role"]), ("ready-to-publish", "merge", "publisher"))
        row = C.decide(snap(RUNNING), f"Final verdict: mergeable — head {HEAD[:12]}\n")
        self.assertEqual((row["state"], row["ci"]), ("awaiting-ci", "pending"))
        self.assertIn("hearting run merge-line 1425", row["next"])
        self.assertTrue(C.format_line(C.decide(snap(), f"최종 판정: 병합 가능 — head {HEAD[:7]}"))
                        .startswith("pr_chain=ready-to-publish pr=#1425 head=8779aadca369 ci=success review=merge"))

    def test_closed_fix_returns_to_author(self):
        for line in (f"최종 판정: 수정 후 병합 — head {HEAD[:7]}", f"Final verdict: changes requested — head {HEAD[:9]}."):
            with self.subTest(line=line):
                row = C.decide(snap(), line + "\n")
                self.assertEqual((row["state"], row["role"]), ("fix-requested", "author"))

    def test_closed_verdict_on_another_head_is_stale(self):
        for line in (f"최종 판정: 병합 가능 — head {OLD[:7]}", f"최종 판정: 수정 후 병합 — head {OLD[:7]}",
                     f"최종 판정: 병합 가능 — head {HEAD[:7]}0"):
            with self.subTest(line=line):
                row = C.decide(snap(), line + "\n")
                self.assertEqual((row["state"], row["role"]), ("review-stale", "reviewer"))

    def test_only_the_first_line_is_read(self):
        current, other = f"최종 판정: 병합 가능 — head {HEAD[:7]}", f"최종 판정: 수정 후 병합 — head {OLD[:7]}"
        fix_now = f"Final verdict: changes requested — head {HEAD}"
        for text, expected in ((f"{current}\n\n## 이력\n\n{other}\n", "ready-to-publish"),
                               (f"{other}\n\n## 재검토\n\n{current}\n", "review-stale"),
                               (f"{current}\n\n## 이력\n\n{fix_now}\n", "ready-to-publish"),
                               (f"# 검토\n\n## PR #1425\n\n**{fix_now}**\n\n{current}\n", "fix-requested")):
            with self.subTest(text=text):
                self.assertEqual(self.state(snap(), text), expected)

    def test_closed_lines_after_the_first_line_are_never_read(self):
        merge, fix = f"Final verdict: mergeable — head {HEAD[:7]}", f"최종 판정: 수정 후 병합 — head {HEAD[:7]}"
        for line in (merge, fix):
            # RV-PR1430-R4-1110 boundary: longer and mixed fences hold the example inside.
            for text in (f"검토 중.\n\n{line}\n", f"```text\n{line}\n```\n", f"~~~text\n{line}\n~~~\n",
                         f"````markdown\n```\n{line}\n```\n````\n", f"~~~markdown\n```\n{line}\n```\n~~~\n",
                         f"> {line}\n", f"- {line}\n", f"    {line}x\n\n{line}\n"):
                for checks in (OK, RUNNING):
                    with self.subTest(text=text, checks=checks):
                        row = C.decide(snap(checks), text)
                        self.assertEqual((row["state"], row["review"]), ("awaiting-review", "none"))


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
AUTO = ("ready-to-publish", "awaiting-ci", "fix-requested")
CLOSED_LINES = [
    (f"최종 판정: 병합 가능 — head {HEAD[:7]}", "ready-to-publish"),
    (f"Final verdict: mergeable — head {HEAD[:7]}", "ready-to-publish"),
    (f"최종 판정: 수정 후 병합 — head {HEAD[:7]}", "fix-requested"),
    (f"Final verdict: changes requested — head {HEAD[:7]}", "fix-requested"),
]
# Words a reviewer may add to a verdict line: conditions, notes, negations, plain prose.
EXTRA = ["(단, P1 수정 후 재검토 필요)", ". This is not an approval", ", but only after the P1 fix", "이 문구를 승인으로 취급하지 않습니다",
         "— 차단0, 수정 불필요", "(4b45ba0), P1 1건", "x", "?", "— 운영 배포에 수용 가능", "검토 중"]


class ClosedFormTest(unittest.TestCase):
    """Only a whole line in the closed list is read; everything else goes to a person."""

    def state(self, review, head=HEAD):
        return C.decide(snap(headRefOid=head), review)["state"]

    def test_closed_list_is_exactly_four_forms(self):
        self.assertEqual(sorted(kind for kind, _ in C.VERDICT_FORMS), ["fix", "fix", "merge", "merge"])
        for line, expected in CLOSED_LINES:
            with self.subTest(line=line):
                self.assertEqual(self.state(line + "\n"), expected)

    def test_any_added_word_on_the_line_is_not_a_verdict(self):
        for line, _ in CLOSED_LINES:
            label, hexa = line.rsplit(" — head ", 1)
            variants = [f"{line} {extra}" for extra in EXTRA] + [f"{line}{extra}" for extra in EXTRA]
            variants += [f"{label} {extra} — head {hexa}" for extra in EXTRA]
            variants += [f"{label}. head {hexa}", f"{label} head {hexa}", f"{label} — 검토 head {hexa}",
                         f"> {line}", f"- {line}", f"| {line} |", f"## {line}", line.replace("— head", "— reviewed head")]
            for text in variants:
                with self.subTest(text=text):
                    row = C.decide(snap(), text + "\n")
                    self.assertEqual((row["state"], row["review"], row["role"]), ("awaiting-review", "none", "reviewer"))
                    self.assertIn("person", row["next"])

    def test_commentary_after_a_closed_line_is_not_read(self):
        for line, expected in CLOSED_LINES:
            for after in ("단, P1 수정 후 재검토 필요.", "This is not an approval.", "첫 실제 변경 전까지 null인 절충도 수용한다.",
                          "추가 필수 수정은 없다."):
                with self.subTest(line=line, after=after):
                    self.assertEqual(self.state(f"{line}\n{after}\n"), expected)

    def test_unread_next_quotes_the_line(self):
        row = C.decide(snap(), f"**Final verdict: mergeable.** head {HEAD[:7]}. This is not an approval.\n")
        self.assertEqual(row["state"], "awaiting-review")
        self.assertIn("Final verdict: mergeable", row["next"])
        row = C.decide(snap(), "")
        self.assertEqual(row["state"], "awaiting-review")

    def test_review_verdict_helper(self):
        self.assertEqual(C.review_verdict(f"최종 판정: 병합 가능 — head {HEAD[:7]}", HEAD), ("merge", True))
        self.assertEqual(C.review_verdict(f"최종 판정: 병합 가능(단, P1 수정 후 재검토 필요) — head {HEAD[:7]}", HEAD),
                         ("none", False))
        self.assertEqual(C.review_verdict("", HEAD), ("none", False))


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
R3_HEAD = "b641be9147f52c737e00448dc592abc07e8a2a5f"
H, O, H3 = R2_HEAD[:7], R2_OLD[:7], R3_HEAD[:7]
# Reviewer probes, verbatim: RV-PR1430-1110 originals and RV-PR1430-R2-1110 (head R2_HEAD),
# RV-PR1430-R3-1110 new phrasings (head R3_HEAD). (name, head, text, state) - only a closed
# line reaches an automatic state.
PROBES = [
    ("orig-not-mergeable", R2_HEAD, f"Final verdict: not mergeable ({H})\n", "awaiting-review"),
    ("orig-korean-negated", R2_HEAD, f"최종 판정: 병합 가능하지 않음 {H}\n", "awaiting-review"),
    ("orig-with-changes", R2_HEAD, f"Verdict: approve with changes {H}\n", "awaiting-review"),
    ("orig-rejection", R2_HEAD, f"최종 판정: 병합 불가 {H}\n", "awaiting-review"),
    ("orig-do-not-merge", R2_HEAD, f"Verdict: do not merge {H}\n", "awaiting-review"),
    ("orig-bypassed", R2_HEAD, f"Verdict: bypassed {H}\n", "awaiting-review"),
    ("orig-current-rereview", R2_HEAD,
     f"최종 판정: 병합 가능 (head {O})\n\n본문\n\n## 재검토 head {H}\n\n최종 판정: 수정 후 병합\n", "awaiting-review"),
    ("orig-old-head-away", R2_HEAD, f"최종 판정: 병합 가능 head {O}\n\nauthor가 {H}로 push, 재검토 전\n", "awaiting-review"),
    ("orig-blocked", R2_HEAD, f"Verdict: BLOCKED — head {H}\n", "awaiting-review"),
    ("orig-conflicting", R2_HEAD, f"최종 판정: 병합 가능 {H}\n\n최종 판정: 수정 후 병합 {H}\n", "awaiting-review"),
    ("form-first", R2_HEAD, f"병합 가능 — 검토 head {H}\n", "awaiting-review"),
    ("form-label", R2_HEAD, f"# 독립 검토\n\n**판정: 수정 후 병합 — P2 1건.** exact head `{R2_HEAD}`\n", "awaiting-review"),
    ("form-title", R2_HEAD, f"# PR #1430 독립 검토 — 최종 병합 가능\n\n대상 head `{R2_HEAD}`, CI 13/13 SUCCESS.\n",
     "awaiting-review"),
    ("normal-nonblocking-notes", R2_HEAD, f"최종 판정: 병합 가능. 차단0, 수정 불필요 — head {H}\n", "awaiting-review"),
    ("conditional-ko-parenthesis", R2_HEAD, f"최종 판정: 병합 가능(단, P1 수정 후 재검토 필요) — head {H}\n", "awaiting-review"),
    ("conditional-ko-dash", R2_HEAD, f"최종 판정: 병합 가능 — 단, P1 수정 후 재검토가 필요하다. head {H}\n", "awaiting-review"),
    ("conditional-en-parenthesis", R2_HEAD, f"Final verdict: approved (with changes) — head {H}\n", "awaiting-review"),
    ("conditional-en-comma", R2_HEAD,
     f"Final verdict: mergeable, but do not merge until the P1 fix is reviewed. head {H}\n", "awaiting-review"),
    ("conditional-en-within-clause", R2_HEAD,
     f"Final verdict: mergeable only after the P1 correction is reviewed — head {H}\n", "awaiting-review"),
    ("negative-ko-deferred", R2_HEAD, f"최종 판정: 병합 가능 여부는 보류 — head {H}\n", "awaiting-review"),
    ("stale-ko-same-paragraph", R2_HEAD,
     f"최종 판정: 병합 가능 — head {O}\n작성자가 {H}로 push했으나 새 head는 아직 재검토하지 않았다.\n", "review-stale"),
    ("stale-en-same-paragraph", R2_HEAD,
     f"Final verdict: mergeable — reviewed head {O}\nThe author pushed {H}; this new head has not been reviewed.\n",
     "awaiting-review"),
    ("stale-ko-same-line", R2_HEAD, f"최종 판정: 병합 가능 — 검토 head {O} (현재 {H}는 미검토)\n", "awaiting-review"),
    ("stale-en-heading", R2_HEAD, f"## PR #1430 current head {H}\n\nFinal verdict: mergeable — reviewed head {O}\n",
     "awaiting-review"),
    ("unknown-line", R2_HEAD, f"Verdict: INCONCLUSIVE — head {H}\n", "awaiting-review"),
    ("unknown-form", R2_HEAD, f"검토 결과를 확정할 수 없습니다. head {H}\n", "awaiting-review"),
    ("r3-ko-possibility-evaluating", R3_HEAD, f"최종 판정: 병합 가능성 평가 중 — head {H3}\n", "awaiting-review"),
    ("r3-ko-premature-conclusion", R3_HEAD, f"최종 판정: 병합 가능하다고 결론 내리기에는 이릅니다 — head {H3}\n",
     "awaiting-review"),
    ("r3-en-contingent-review", R3_HEAD, f"Final verdict: mergeable contingent upon a fresh review — head {H3}\n",
     "awaiting-review"),
    ("r3-ko-current-negation-bold", R3_HEAD, f"**최종 판정: 병합 가능.** head {H3}. 이 문구를 승인으로 취급하지 않습니다.\n",
     "awaiting-review"),
    ("r3-ko-current-negation-plain", R3_HEAD, f"최종 판정: 병합 가능. head {H3}. 이 문구를 승인으로 취급하지 않습니다.\n",
     "awaiting-review"),
    ("r3-en-current-negation-bold", R3_HEAD, f"**Final verdict: mergeable.** head {H3}. This is not an approval.\n",
     "awaiting-review"),
    ("r3-en-current-negation-plain", R3_HEAD, f"Final verdict: mergeable. head {H3}. This is not an approval.\n",
     "awaiting-review"),
    ("r3-ko-approval-example-bold", R3_HEAD, f"**최종 판정: 병합 가능.** head {H3}. 단순 형식 예시이며 검토 결과가 아닙니다.\n",
     "awaiting-review"),
    ("r3-ko-review-ongoing-bold", R3_HEAD, f"**최종 판정: 병합 가능.** head {H3}\n검토 중인 임시 표기입니다.\n", "awaiting-review"),
    ("r3-ko-review-ongoing-plain", R3_HEAD, f"최종 판정: 병합 가능. head {H3}\n검토 중인 임시 표기입니다.\n", "awaiting-review"),
    ("r3-ko-condition-confirm-new-code", R3_HEAD,
     f"**최종 판정: 병합 가능.** head {H3}. 수정된 코드를 확인해야 출판할 수 있습니다.\n", "awaiting-review"),
    ("r3-control-ko", R3_HEAD, f"최종 판정: 병합 가능 — head {H3}\n", "ready-to-publish"),
    ("r3-control-history", R3_HEAD,
     f"**최종 판정: 병합 가능.** exact head `{R3_HEAD}`. 원 P2 종결·추가 필수0. 아래 수정 후 병합은 이력으로 보존한다.\n",
     "awaiting-review"),
    ("r3-control-english", R3_HEAD, f"Final verdict: approved — head {H3}\n", "awaiting-review"),
]

# Real reviewer reports (RV-*-1110): (name, reviewed head, report's meaning, verdict-line excerpt verbatim).
# The full files are read too when the artifact root is present (RealReportFileTest).
REAL_REVIEWS = [
    ("RV-PR1430-R3-1110", R3_HEAD, "fix", f"최종 판정: 수정 후 병합 — head {H3}\n\nPR #1430 재검토. 원 반례10개는 모두 닫혔다.\n"),
    ("RV-CAIRN11-R3-1110", "152c6f5c6e74a80f5b4628f48de7a20372442543", "merge",
     "# Cairn PR #11 R3 — R4 cron 보정 변경분 재검토\n\n**최종 판정: PASS — 운영 배포에 수용 가능.** exact "
     "`152c6f5c6e74a80f5b4628f48de7a20372442543`, 범위 `21cca3e..152c6f5`의2파일(기존 cron과 회귀 시험)만 재검토했다. "
     "원 R4 P2는 닫혔고 추가 필수 수정은 없다. 업그레이드 직후1회 관측을 없애 첫 실제 변경 전까지 `specMirror`가 null인 절충도 아래 이유로 수용한다.\n"),
    ("RV-PR1438-1110", "3b86ac1d17d3ca7f55e2587373fa98362f7f6d1b", "merge", "최종 판정: 병합 가능 — head 3b86ac1\n"),
    ("RV-PR1430-1110", R2_OLD, "fix", f"최종 판정: 수정 후 병합 — head {O} ({R2_OLD}), P1 1건 · P2 2건\n"),
    ("RV-BC-WRAPPED-CLAUDE-SEAT-1110", HEAD, "merge",
     f"**최종 판정: 병합 가능.** exact head `{HEAD}`의 CI42735775437 attempt1 13/13 SUCCESS(marker 포함)와 작성자 정상 마감을 합쳤다.\n"),
    ("RV-PR1428-1110", "d2803c3f5304b43c32d56ccaf2c631269809c7a9", "fix",
     "수정 후 병합 — 검토 head d2803c3 (PR #1428, branch steward-progress-solo-1110)\n"),
]


class ProbeTest(unittest.TestCase):
    def test_probes_reach_an_automatic_state_only_on_a_closed_line(self):
        self.assertEqual(len(PROBES), 40)
        for name, head, text, expected in PROBES:
            with self.subTest(name=name):
                state = C.decide(snap(headRefOid=head), text)["state"]
                self.assertEqual(state, expected)
                closed = bool(C.closed_verdict(C.first_line(text)))
                self.assertEqual(state in AUTO or state == "review-stale", closed)

    def test_real_reports_are_never_misread(self):
        for name, head, meaning, text in REAL_REVIEWS:
            with self.subTest(name=name):
                reading = C.review_reading(text, head)
                self.assertIn(reading["verdict"], (meaning, "none"))
                state = C.decide(snap(headRefOid=head), text)["state"]
                self.assertIn(state, {"merge": ("ready-to-publish", "awaiting-review"),
                                      "fix": ("fix-requested", "awaiting-review")}[meaning])

    def test_public_cli_sends_unclosed_lines_to_a_person(self):
        texts = [next(text for case, _, text, _ in PROBES if case == name)
                 for name in ("r3-ko-premature-conclusion", "r3-ko-current-negation-bold",
                              "r3-en-current-negation-bold", "r3-en-contingent-review")]
        texts += [f"````markdown\n```\nFinal verdict: mergeable — head {H3}\n```\n````\n",
                  f"~~~markdown\n```\n최종 판정: 수정 후 병합 — head {H3}\n```\n~~~\n"]
        for text in texts:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "review.md"
                path.write_text(text, encoding="utf-8")
                out = io.StringIO()
                with mock.patch.object(C, "snapshot", return_value=snap(headRefOid=R3_HEAD)), \
                     mock.patch.object(C, "_gh", side_effect=AssertionError("network")), \
                     contextlib.redirect_stdout(out):
                    self.assertEqual(C.main(["next", "1430", "--review", str(path), "--json"]), 0)
                row = json.loads(out.getvalue())
                self.assertEqual((row["state"], row["role"]), ("awaiting-review", "reviewer"))
                self.assertNotIn("merge-line", row["next"])


# Set HEARTING_RV_INPUTS to the reports' directory to read them; skipped otherwise.
REPORTS = Path(os.environ.get("HEARTING_RV_INPUTS") or "/nonexistent")
# Each full report with the head it reviewed and its meaning as a person reads it.
REAL_FILES = {
    "RV-BC-CODEX-RESUME-SELECTION-1110.md": ("c16093de89a88461a534ccee01ce82efdf16aca3", "merge"),
    "RV-BC-FLEET-PEER-RESUMED-1110.md": ("c0825319c1553ba8766f68c4fae3d3810338c4d6", "merge"),
    "RV-BC-OPEN32-1110.md": ("cf08b89564a1770b3e0105651bc792d443a4bb64", "merge"),
    "RV-BC-SHELL-V33-1110.md": ("55bc06fefdd20baf48bc13e05cfe67be15b50b85", "merge"),
    "RV-BC-SHELL-V33-POLISH-1110.md": ("55bc06fefdd20baf48bc13e05cfe67be15b50b85", "merge"),
    "RV-BC-WRAPPED-CLAUDE-SEAT-1110.md": ("8779aadca36989d0933ae8314cacbd44db30359c", "merge"),
    "RV-BC-WRAPPED-FOREGROUND-RETIRE-1110.md": ("c0482f772733fc98dbf259595aec21235ecbc5a5", "merge"),
    "RV-CAIRN11-1408-1110.md": ("994e9eb", "merge"),
    "RV-CAIRN11-R2-1110.md": ("21cca3e747dc6024c58ba033ad027b1845068d77", "fix"),
    "RV-CAIRN11-R3-1110.md": ("152c6f5c6e74a80f5b4628f48de7a20372442543", "merge"),
    "RV-PR1428-1110.md": ("d2803c3", "fix"),
    "RV-PR1428-R2-1110.md": ("b9932bb", "merge"),
    "RV-PR1429-1110.md": ("a0e73fd49c88e15c6bd4c891ea9b36c301e159be", "fix"),
    "RV-PR1429-R2-1110.md": ("920f06d01348b06d8387f92dd1b5df339ba8eadf", "merge"),
    "RV-PR1430-1110.md": (R2_OLD, "fix"),
    "RV-PR1430-R2-1110.md": (R2_HEAD, "fix"),
    "RV-PR1430-R3-1110.md": (R3_HEAD, "fix"),
    "RV-PR1430-R4-1110.md": ("748e5ebc3392960c4d5700f506192d824ccd7a2a", "fix"),
    "RV-PR1431-1110.md": ("e3da694", "merge"),
    "RV-PR1434-1110.md": ("c0b45c1fc252735fa32047183a1d5aae1be7626f", "fix"),
    "RV-PR1434-R2-1110.md": ("fdbabf63525ae1ce10d40d406ae62c6bc9250ae4", "merge"),
    "RV-PR1435-1110.md": ("bdb7ca32212b9ed78c548882cacafafd133af64b", "fix"),
    "RV-PR1435-R2-1110.md": ("ca48f30", "merge"),
    "RV-PR1436-1110.md": ("6e4aca49e", "fix"),
    "RV-PR1437-1110.md": ("f502625", "fix"),
    "RV-PR1438-1110.md": ("3b86ac1d17d3ca7f55e2587373fa98362f7f6d1b", "merge"),
    "RV-PR1439-1110.md": ("2f1f366", "merge"),
    "RV-PR1441-1110.md": ("2a445ccb1b091fcb0c236f705570957a178182c0", "fix"),
}


@unittest.skipUnless(REPORTS.is_dir(), "real review reports are local artifacts (HEARTING_RV_INPUTS)")
class RealReportFileTest(unittest.TestCase):
    def test_full_reports_are_never_misread(self):
        for name, (head, meaning) in REAL_FILES.items():
            path = REPORTS / name
            if not path.is_file():
                continue
            with self.subTest(name=name):
                reading = C.review_reading(path.read_text(encoding="utf-8", errors="replace"), head)
                self.assertIn(reading["verdict"], (meaning, "none"))
                if reading["verdict"] != "none":
                    self.assertTrue(reading["names_head"])


if __name__ == "__main__":
    unittest.main()
