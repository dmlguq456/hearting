"""What Hearting puts into a route parent session's context (BC-SUPERVISOR-CONTEXT-COST-1114).

The route parent (the main session that starts registered work and receives its
completions) re-reads its whole context on every request, so a completion carried
twice, a fixed paragraph on every notice, a repeated line on every prompt, or an extra
turn for work that finished normally is paid again on each later request.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "hooks"))

import dispatch_pending_delivery as pending  # noqa: E402
import dispatch_recipient_receipt as receipt  # noqa: E402
import dispatch_session_sweep as sweep  # noqa: E402
import route_authority  # noqa: E402
import session_tidy  # noqa: E402
import parent_context_fixture as fixture  # noqa: E402
import work_intent  # noqa: E402

FIXED_PARAGRAPHS = ("First close: give the result", "Original request/format apply",
                    "Do not emit a periodic progress recap", "re-arm Background Bash",
                    "Tracked-Workflow Continuation", "Receipt/ACK isn't execution")


_MEASURED = {}


def _measured():
    """One measurement of this tree on every harness, shared by the scenario tests."""
    if not _MEASURED:
        with tempfile.TemporaryDirectory(prefix="scc-test-") as temporary:
            _MEASURED.update(fixture.run(ROOT, Path(temporary)))
    return _MEASURED


class RouteParentScenarioTest(unittest.TestCase):
    """Ten completions and five typed prompts, measured on each harness's own carriers."""

    @classmethod
    def setUpClass(cls):
        cls.results = _measured()

    def each(self):
        return [(harness, result, row) for harness, result in self.results.items()
                for row in result["completions"]]

    def case(self, harness, row):
        return self.subTest(harness=harness, completion=row["index"], kind=row["kind"], pattern=row["pattern"])

    def texts(self, result, index):
        return [item["text"] for item in result["texts"]
                if any(delivery.endswith(f"-{index:02d}") for delivery in item["ids"])]

    def test_every_completion_reaches_the_model_once_whichever_carrier_comes_first(self):
        for harness, _result, row in self.each():
            with self.case(harness, row):
                self.assertEqual(row["carried"], 1, row["carriers"])
                self.assertLessEqual(row["summary_copies"], 1)

    def test_no_fixed_paragraph_or_row_hash_is_carried(self):
        for harness, result, row in self.each():
            with self.case(harness, row):
                for text in self.texts(result, row["index"]):
                    for paragraph in FIXED_PARAGRAPHS:
                        self.assertNotIn(paragraph, text)
                    self.assertNotRegex(text, r"row_revision=|row_digest=")
                    # The receipt marker's digest is the only 64-hex value left: the runtime's ACK.
                    self.assertEqual(len(re.findall(r"\b[0-9a-f]{64}\b", text)),
                                     len(re.findall(r"HEARTING_RECEIPT_V1 ", text)))

    def test_a_review_that_ends_on_merge_after_fixes_is_a_normal_completion_with_its_verdict(self):
        for harness, result, row in self.each():
            with self.case(harness, row):
                if row["kind"] != "disposition":
                    continue
                self.assertEqual((row["note"], row["reason"]), ("completed-supervisor", "completed-audit-with-findings"))
                self.assertFalse(row["follow_up_command"])
                self.assertEqual(row["extra_turns"], 0)
                text = "\n".join(self.texts(result, row["index"]))
                self.assertIn("판정: 수정 후 병합", text)
                self.assertIn("검토 완료 · 지적 사항 있음", text)

    def test_a_real_failure_still_asks_for_attention(self):
        for harness, result, row in self.each():
            with self.case(harness, row):
                if row["kind"] != "failure":
                    continue
                self.assertEqual(row["note"], "dead-worker-fail")
                self.assertTrue(row["follow_up_command"])

    def test_a_normally_finished_completion_costs_no_extra_turn(self):
        for harness, _result, row in self.each():
            with self.case(harness, row):
                if row["kind"] != "failure":
                    self.assertEqual(row["extra_turns"], 0)
                    self.assertFalse(row["follow_up_command"])

    def test_the_summary_alone_names_verdict_publication_and_report(self):
        for harness, result, row in self.each():
            with self.case(harness, row):
                if row["kind"] == "failure":
                    continue
                text = "\n".join(self.texts(result, row["index"]))
                self.assertRegex(text, r"\n판정: ")
                self.assertRegex(text, r"\nPR: #41\d\d · head [0-9a-f]{12} · CI 15/15 통과 · MERGEABLE")
                self.assertRegex(text, r"\n산출물: \S+/final_report\.md")
                self.assertEqual(row["unextracted"], 0)

    def test_a_typed_prompt_carries_a_repeated_wait_line_once_with_its_count(self):
        for harness, result in self.results.items():
            for prompt in result["prompts"]:
                with self.subTest(harness=harness, prompt=prompt["prompt"]):
                    cards = [item["text"] for item in result["texts"]
                             if item["prompt"] == prompt["prompt"] and item["carrier"] == "prompt-card"]
                    self.assertEqual(cards, ["[답변 필요] 사용자 답 대기 ×3"])


class StewardScenarioTest(unittest.TestCase):
    """Main session A holds the steward role for B and C; the periodic check on each harness."""

    @classmethod
    def setUpClass(cls):
        cls.results = _measured()

    def test_with_no_live_target_only_its_own_records_changing_the_check_stays_quiet(self):
        for harness, result in self.results.items():
            with self.subTest(harness=harness):
                steps = result["steward"]["steps"]
                woke = [step["woke"] for step in steps]
                # Live targets are checked every interval; once every target ended the one
                # reminder goes out, and the holder's own changing records open no turn.
                self.assertEqual(woke, [True, True, True, True, False, False, False])

    def test_a_steward_notice_carries_no_fixed_paragraph(self):
        for harness, result in self.results.items():
            for step in result["steward"]["steps"]:
                if not step["woke"]:
                    continue
                with self.subTest(harness=harness, step=step["label"]):
                    for paragraph in ("about work you started", "polling loop", "re-invocation"):
                        self.assertNotIn(paragraph, step["text"])
                    self.assertLessEqual(step["fixed_header_chars"], 170)


# The notice header v4.32.4 shipped (frozen bytes): what it already sent is still recognized.
_FROZEN_SHIPPED_NOTICE_HEADER = (
    "Hearting notice about work you started (a paused route resumed, a remote run ended). "
    "Report observed metrics, trend, and elapsed or ETA against the criteria and plan; state when unavailable. "
    "Act only where a line's required_action asks for it. "
    "Do not start a separate harness-scheduled re-invocation, Monitor, dispatch-wait, or a polling loop."
)


class ShippedNoticeHeaderTest(unittest.TestCase):
    def test_a_notice_body_v4_32_4_sent_is_still_a_hearting_delivery(self):
        marker = "HEARTING_RECEIPT_V1 delivery-" + "a" * 32 + " " + "b" * 64
        body = sweep.NOTICE_DELIVERY_HEADER + "\n- steward-check: Steward check: x\n" + marker
        shipped = sweep._shipped_headers(body)
        self.assertEqual(shipped, _FROZEN_SHIPPED_NOTICE_HEADER + "\n- steward-check: Steward check: x\n" + marker)
        self.assertIn(_FROZEN_SHIPPED_NOTICE_HEADER + "\n", receipt._delivery_heads())
        # A quoted header inside a payload is not a block header.
        quoted = "Hearting runtime delivery. x\n- line\nnot a marker\n\n" + sweep.NOTICE_DELIVERY_HEADER + "\nq"
        self.assertEqual(sweep._shipped_headers(quoted).count(_FROZEN_SHIPPED_NOTICE_HEADER), 0)


class ClaudeReceiptEvidenceTest(unittest.TestCase):
    """The transcript shapes Claude Code writes when a Stop-hook notification reaches the model."""

    SID = "ses-evidence"

    def note(self, delivery_id, digest="a" * 64):
        # Claude Code's shape: the notification block, then the hook output in a reminder.
        return ("<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n"
                "<system-reminder>\nStop hook blocking error from command \"PostToolUse:Bash\": body\n"
                f"HEARTING_RECEIPT_V1 {delivery_id} {digest}\n</system-reminder>")

    def test_a_notification_absorbed_mid_turn_is_a_receipt(self):
        attachment = {"type": "attachment", "sessionId": self.SID, "attachment": {
            "type": "queued_command", "commandMode": "task-notification", "prompt": self.note("delivery-x"),
            "origin": {"kind": "task-notification", "producer": "session-task"}}}
        evidence = receipt.claude_feedback_evidence([attachment], self.SID)
        self.assertIn(("delivery-x", "a" * 64), evidence["bindings"])
        for changed in ({"sessionId": "other"}, {"attachment": {**attachment["attachment"], "commandMode": "prompt"}},
                        {"attachment": {**attachment["attachment"], "origin": {"kind": "human"}}},
                        {"attachment": {**attachment["attachment"], "prompt": "HEARTING_RECEIPT_V1 delivery-x " + "a" * 64}}):
            self.assertEqual(receipt.claude_feedback_evidence([{**attachment, **changed}], self.SID)["bindings"], [])

    def test_a_notification_still_in_the_queue_is_queued_not_received(self):
        events = [{"type": "queue-operation", "sessionId": self.SID, "operation": "enqueue",
                   "content": self.note(f"delivery-{name}")} for name in ("a", "b", "c")]
        events += [{"type": "queue-operation", "sessionId": self.SID, "operation": "dequeue"},
                   {"type": "queue-operation", "sessionId": self.SID, "operation": "remove",
                    "content": self.note("delivery-c"), "reason": "absorbed_mid_turn"}]
        evidence = receipt.claude_feedback_evidence(events, self.SID)
        self.assertEqual(evidence["bindings"], [])
        self.assertEqual(evidence["queued"], [("delivery-b", "a" * 64)])

    def test_the_prompt_of_the_turn_a_notification_started_is_its_receipt(self):
        self.assertEqual(receipt.claude_prompt_bindings(self.note("delivery-p")), [("delivery-p", "a" * 64)])
        typed = "please look at HEARTING_RECEIPT_V1 delivery-p " + "a" * 64
        self.assertEqual(receipt.claude_prompt_bindings(typed), [])
        self.assertEqual(receipt.claude_prompt_bindings("note: " + self.note("delivery-p")), [])


class NoLostDeliveryTest(unittest.TestCase):
    """A boundary that leaves a delivery to Claude's own queue never drops it."""

    SID = "ses-queued"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="scc-lost-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.attempt = "att-" + uuid.uuid4().hex
        child = {"attempt_id": self.attempt, "status": "done", "readiness": "ready",
                 "reason": "terminal-complete", "required_action": "advance-completed",
                 "harness": "claude", "delivery_classification": "success"}
        body = {"schema_version": 2, "state": "ready", "parent_attempt_id": "att-parent",
                "job_registry": str(self.jobs), "children": [child], "delivery_classification": "success"}
        self.delivery = "delivery-" + uuid.uuid4().hex
        pending.create(self.root, recipient_kind="claude-parent-runtime", recipient_key=self.SID,
                       delivery_id=self.delivery, session_generation="", session_generation_supported="0",
                       attempt_ids=[self.attempt], parent_attempt_id="att-parent", route_id="rt-x",
                       route_node="one-shot", receipt=body, receipt_digest=pending._canonical_receipt_digest(body),
                       row_revisions={self.attempt: "r"})
        pending.claim(self.root, self.SID, self.delivery, claim_owner="claude-async-rewake:1:1",
                      lease_seconds=0.001)
        pending.mark_sent_ambiguous(self.root, self.SID, self.delivery, claim_owner="claude-async-rewake:1:1")
        time.sleep(0.01)  # the rewake's claim lease is over

    def evidence(self, queued):
        record = pending.read(self.root, self.SID, self.delivery)
        binding = (self.delivery, receipt.binding_digest(record, self.SID))
        return {"harness": "claude", "session_id": self.SID, "source": "claude-transcript",
                "bindings": [], "legacy_texts": [], "queued": [binding] if queued else []}

    def sweep(self, *, queued, defer):
        with mock.patch.object(sweep, "record_jobs", return_value=self.jobs):
            records, _count = sweep.sweep_deliver(self.root, "claude-parent-runtime", self.SID,
                                                  native_evidence=self.evidence(queued),
                                                  defer_native_queued=defer)
        sweep.release_claims(self.SID, [(self.root, records)], "claude-parent-runtime", reason="test")
        return [record["delivery_id"] for record in records]

    def test_a_tool_boundary_leaves_a_notice_in_claudes_queue_to_that_queue(self):
        self.assertEqual(self.sweep(queued=True, defer=True), [])
        self.assertEqual(pending.read(self.root, self.SID, self.delivery)["state"], "sent-ambiguous")

    def test_the_next_prompt_still_carries_a_queued_notice_the_queue_may_have_lost(self):
        self.assertEqual(self.sweep(queued=True, defer=False), [self.delivery])

    def test_without_a_queued_copy_the_tool_boundary_carries_it_as_before(self):
        self.assertEqual(self.sweep(queued=False, defer=True), [self.delivery])


class DispositionHandoffTest(unittest.TestCase):
    """A review disposition in the verdict line is the verdict it names; nothing else is."""

    def handoff(self, verdict, blocker="none"):
        return f"artifact: /r/report.md\nverdict: {verdict}\nblocker: {blocker}"

    def test_closed_disposition_words_read_as_their_verdict(self):
        for word, verdict in (("수정 후 병합", "FAIL"), ("수정 후 병합 — P1 하나", "FAIL"), ("**수정 후 병합**", "FAIL"),
                              ("Changes requested (two P1)", "FAIL"), ("보류", "FAIL"), ("병합 가능 — head abc1234", "PASS"),
                              ("mergeable", "PASS")):
            with self.subTest(word=word):
                found = route_authority.handoff_search(self.handoff(word))
                self.assertEqual((found["verdict"], found["disposition"]), (verdict, word))
        for word in ("완료", "통과 못 함", "보류하지 않음", "LGTM", "수정 필요할 수도"):
            with self.subTest(word=word):
                self.assertIsNone(route_authority.handoff_search(self.handoff(word)))
        self.assertNotIn("disposition", route_authority.handoff_search(self.handoff("PASS")))

    def test_the_live_classifier_keeps_a_true_contract_violation(self):
        import dispatch_supervisor_terminal as terminal
        classify = lambda text: terminal.classify_session_result(  # noqa: E731
            {"subtype": "success", "is_error": False, "result": text}, 0, runtime="claude")
        self.assertEqual(classify(self.handoff("수정 후 병합")).note, "dead-worker-fail")
        self.assertEqual(classify(self.handoff("병합 가능")).note, "completed-supervisor")
        self.assertEqual(classify(self.handoff("병합 가능", "P1 남음")).note, "dead-contract")
        self.assertEqual(classify(self.handoff("끝남")).note, "dead-contract")
        self.assertEqual(classify("artifact: /r\nverdict: 수정 후 병합").note, "dead-contract")


class SummaryContentTest(unittest.TestCase):
    def test_verdict_and_publication_come_from_the_report(self):
        text = ("# 보고\n\n**최종 판정: 수정 후 병합 — head abcdef012345**\n\n## 해결하지 못한 것\n- P1\n\n"
                "- PR #1640 · head `0123456789abcdef0123` · CI 15/15 통과 · GitHub mergeable `MERGEABLE`\n")
        self.assertEqual(work_intent.report_verdict(text), "수정 후 병합 — head abcdef012345")
        self.assertEqual(work_intent.report_publication(text),
                         "#1640 · head 0123456789ab · CI 15/15 통과 · MERGEABLE")
        self.assertEqual(work_intent.report_verdict("## 바꾼 것\n- x\n"), "")
        self.assertEqual(work_intent.report_publication("PR #12 opened\n"), "")
        self.assertEqual(work_intent.report_sections(text)["remaining"], ["P1", text.splitlines()[-1][2:]])

    def test_answer_waits_fold_repeats(self):
        self.assertEqual(session_tidy.answer_wait_text(["사용자 답 대기"] * 3 + ["배포할까요?"]),
                         ["[답변 필요] 사용자 답 대기 ×3", "[답변 필요] 배포할까요?"])
        self.assertEqual(session_tidy.answer_wait_text([f"q{n}" for n in range(5)]),
                         ["[답변 필요] q0", "[답변 필요] q1", "[답변 필요] q2"])


if __name__ == "__main__":
    unittest.main()
