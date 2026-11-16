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
        # The unchanged line enters the session once; OpenCode's system-prompt context lasts one
        # request, so there it is carried on every typed prompt.
        for harness, result in self.results.items():
            for number, prompt in enumerate(result["prompts"]):
                with self.subTest(harness=harness, prompt=prompt["prompt"]):
                    cards = [item["text"] for item in result["texts"]
                             if item["prompt"] == prompt["prompt"] and item["carrier"] == "prompt-card"
                             and item["text"]]
                    carried = number == 0 or harness == "opencode"
                    self.assertEqual(cards, ["[답변 필요] 답 필요 ×3"] if carried else [])


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

    def queue(self, operation, name=None, **extra):
        return {"type": "queue-operation", "sessionId": self.SID, "operation": operation,
                **({"content": self.note(f"delivery-{name}")} if name else {}), **extra}

    def test_claudes_own_queue_records_are_never_read_as_state(self):
        # `queue-operation` is not documented: a notification counts only where the model
        # reads it (a user event, an absorbed `queued_command`), never by what the queue holds.
        events = [self.queue("enqueue", name) for name in ("a", "b")]
        events += [self.queue("dequeue"), self.queue("remove", reason="dropped_by_hook"),
                   self.queue("popAll", "b")]
        evidence = receipt.claude_feedback_evidence(events, self.SID)
        self.assertEqual((evidence["bindings"], evidence["legacy_texts"]), ([], []))
        self.assertNotIn("queued", evidence)

    def quoted(self, delivery_id):
        # A background agent's completion whose result quotes a Hearting notice it read.
        note = self.note(delivery_id)
        return ("<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>\n"
                "<summary>Agent \"grep the transcript\" completed</summary>\n<result>matching lines: "
                + note + "\n" + json.dumps({"operation": "enqueue", "content": note}) + "</result>\n"
                "</task-notification>")

    def test_a_notification_quoted_inside_another_is_not_its_receipt(self):
        origin = {"kind": "task-notification", "producer": "session-task"}
        attachment = {"type": "attachment", "sessionId": self.SID, "attachment": {
            "type": "queued_command", "commandMode": "task-notification", "prompt": self.quoted("delivery-q"),
            "origin": origin}}
        turn = {"type": "user", "sessionId": self.SID, "origin": origin, "promptSource": "system",
                "turnOrigin": "task_notification", "message": {"content": self.quoted("delivery-q")}}
        for event in (attachment, turn):
            with self.subTest(event=event["type"]):
                evidence = receipt.claude_feedback_evidence([event], self.SID)
                self.assertEqual((evidence["bindings"], evidence["legacy_texts"]), ([], []))
        self.assertEqual(receipt.claude_prompt_bindings(self.quoted("delivery-q")), [])
        # Top-level notifications still count, each one, up to the first that is not one.
        joined = self.note("delivery-1") + "\n" + self.note("delivery-2") + "\n" + self.quoted("delivery-3")
        self.assertEqual(receipt.claude_prompt_bindings(joined), [("delivery-1", "a" * 64), ("delivery-2", "a" * 64)])
        evidence = receipt.claude_feedback_evidence([{**turn, "message": {"content": joined}}], self.SID)
        self.assertEqual(evidence["bindings"], [("delivery-1", "a" * 64), ("delivery-2", "a" * 64)])

    def test_the_prompt_of_the_turn_a_notification_started_is_its_receipt(self):
        self.assertEqual(receipt.claude_prompt_bindings(self.note("delivery-p")), [("delivery-p", "a" * 64)])
        typed = "please look at HEARTING_RECEIPT_V1 delivery-p " + "a" * 64
        self.assertEqual(receipt.claude_prompt_bindings(typed), [])
        self.assertEqual(receipt.claude_prompt_bindings("note: " + self.note("delivery-p")), [])


class NoLostDeliveryTest(unittest.TestCase):
    """A tool boundary carries no completion, and neither it nor the turn's Stop loses one."""

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

    def evidence(self, received):
        record = pending.read(self.root, self.SID, self.delivery)
        binding = (self.delivery, receipt.binding_digest(record, self.SID))
        return {"harness": "claude", "session_id": self.SID, "source": "claude-transcript",
                "bindings": [binding] if received else [], "legacy_texts": []}

    def sweep(self, *, received, defer, seen=None):
        with mock.patch.object(sweep, "record_jobs", return_value=self.jobs):
            records, _count = sweep.sweep_deliver(self.root, "claude-parent-runtime", self.SID,
                                                  native_evidence=self.evidence(received),
                                                  defer_completions=defer, received_out=seen)
        sweep.release_claims(self.SID, [(self.root, records)], "claude-parent-runtime", reason="test")
        return [record["delivery_id"] for record in records]

    def test_a_tool_boundary_leaves_a_completion_to_the_rewake(self):
        self.assertEqual(self.sweep(received=False, defer=True), [])
        self.assertEqual(pending.read(self.root, self.SID, self.delivery)["state"], "sent-ambiguous")

    def test_the_turns_stop_and_the_next_prompt_still_carry_it(self):
        # Whatever the queue did with the rewake's notification, nothing but the model's own
        # transcript receipt ends the duty.
        self.assertEqual(self.sweep(received=False, defer=False), [self.delivery])

    def test_a_completion_the_transcript_shows_received_is_settled_and_counted_at_a_tool_boundary(self):
        seen = []
        self.assertEqual(self.sweep(received=True, defer=True, seen=seen), [])
        self.assertEqual([record["delivery_id"] for record in seen], [self.delivery])
        self.assertEqual(pending.read(self.root, self.SID, self.delivery)["state"], "acked")

    def test_only_a_completion_is_left_to_the_rewake(self):
        base = {"receipt": {"kind": "notice"}}
        for record, completion in (
                ({"receipt": {"children": [{"required_action": "advance-completed"}]}}, True),
                ({"receipt": {"kind": "notice"}}, False), ({"receipt": {"kind": "supervision"}}, False),
                ({"receipt": {"kind": "human-gate"}}, False),
                ({"receipt": {"children": [{"required_action": "human-gate:frame-review"}]}}, False)):
            with self.subTest(receipt=record["receipt"]):
                self.assertIs(sweep.is_completion_record(record), completion)


class QueueOrderTest(unittest.TestCase):
    """A completion reaches the model whatever Claude's queue does with the rewake's notification:
    before the turn ends and without a person, or, when the turn is cut (Esc, an API error),
    with the next typed prompt (the measured orders)."""

    NEXT_INPUT = {"q-esc", "q-error"}

    @classmethod
    def setUpClass(cls):
        cls.orders = {row["order"]: row for row in _measured()["claude"]["queue_orders"]}

    def test_every_order_is_measured(self):
        self.assertEqual(set(self.orders), {order for _kind, order in fixture.QUEUE_ORDERS})

    def test_a_completion_the_queue_drops_without_content_still_reaches_the_model(self):
        row = self.orders["q-dropped"]
        self.assertEqual((row["times"], row["without_person"], row["carriers"]), (1, True, ["stop"]))

    def test_a_completion_quoted_by_another_notification_still_reaches_the_model(self):
        row = self.orders["q-quoted"]
        self.assertEqual((row["times"], row["without_person"], row["carriers"]), (1, True, ["stop"]))

    def test_a_completion_the_queue_absorbs_reaches_the_model_once(self):
        self.assertEqual(self.orders["q-absorbed"]["carriers"], ["rewake"])

    def test_a_session_that_moved_folders_gets_the_absorbed_completion_once(self):
        # Before the fix its transcript was not found from the new folder, so Stop carried it again.
        row = self.orders["q-moved"]
        self.assertEqual((row["times"], row["without_person"], row["carriers"]),
                         (1, True, ["rewake"]))

    def test_a_rewake_that_was_never_armed_is_made_up_by_the_turns_stop(self):
        row = self.orders["q-unarmed"]
        self.assertEqual((row["times"], row["without_person"], row["carriers"]), (1, True, ["stop"]))

    def test_a_cut_turn_leaves_it_to_the_next_typed_prompt(self):
        for name in sorted(self.NEXT_INPUT):
            with self.subTest(order=name):
                row = self.orders[name]
                self.assertEqual((row["times"], row["without_person"], row["carriers"]),
                                 (1, False, ["prompt-hook"]))

    def test_no_tool_boundary_carries_a_completion_in_any_order(self):
        for name, row in self.orders.items():
            with self.subTest(order=name):
                self.assertNotIn("tool-boundary", row["carriers"])

    def test_every_order_reaches_the_model_without_a_person_at_most_twice(self):
        # Twice is the price of never leaving one to a queue that may lose it: a completion
        # still queued when the turn ends is shown by Stop and again by the turn the queue starts.
        for name, row in self.orders.items():
            with self.subTest(order=name):
                if name not in self.NEXT_INPUT:
                    self.assertTrue(row["without_person"])
                self.assertIn(row["times"], (1, 2))
        self.assertEqual({name for name, row in self.orders.items() if row["times"] == 2},
                         {"q-turn-end", "q-typed"})


class DispositionHandoffTest(unittest.TestCase):
    """A review disposition in the verdict line is the verdict it names; nothing else is."""

    REVIEW = "최종 판정: 수정 후 병합 — head abc1234\n\n## 필수 지적\n- P1 하나\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="scc-verdict-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def handoff(self, verdict, blocker="none", report=REVIEW):
        path = self.root / f"report-{uuid.uuid4().hex}.md"
        if report is not None:
            path.write_text(report, encoding="utf-8")
        return f"artifact: {path}\nverdict: {verdict}\nblocker: {blocker}"

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

    def test_a_fail_side_word_needs_a_finished_review_behind_it(self):
        held = "최종 판정: 보류 — head abc1234\n\n## 근거\n- x\n"
        self.assertEqual(route_authority.handoff_search(self.handoff("보류", report=held))["verdict"], "FAIL")
        for label, verdict, blocker, report in (
                ("work report", "보류", "none", "# 구현 보고\n\n## 바꾼 것\n- a.py\n"),
                ("real blocker", "수정 후 병합", "사용자 결정 필요 — B안/C안", self.REVIEW),
                ("no report", "hold", "none", None),
                ("merge report", "수정 후 병합", "none", "최종 판정: 병합 가능 — head abc1234\n"),
                ("fenced verdict", "보류", "none", "```\n" + self.REVIEW + "```\n")):
            with self.subTest(label=label):
                self.assertIsNone(route_authority.handoff_search(self.handoff(verdict, blocker, report)))

    def test_the_live_classifier_keeps_a_true_contract_violation(self):
        import dispatch_supervisor_terminal as terminal
        classify = lambda text: terminal.classify_session_result(  # noqa: E731
            {"subtype": "success", "is_error": False, "result": text}, 0, runtime="claude")
        self.assertEqual(classify(self.handoff("수정 후 병합")).note, "dead-worker-fail")
        self.assertEqual(classify(self.handoff("수정 후 병합", report=None)).note, "dead-contract")
        self.assertEqual(classify(self.handoff("병합 가능")).note, "completed-supervisor")
        self.assertEqual(classify(self.handoff("병합 가능", "P1 남음")).note, "dead-contract")
        self.assertEqual(classify(self.handoff("끝남")).note, "dead-contract")
        self.assertEqual(classify("artifact: /r\nverdict: 수정 후 병합").note, "dead-contract")


class PassWithMemoHandoffTest(unittest.TestCase):
    """A PASS that carries a memo (after `verdict: PASS` or after `blocker: none`) is a normal
    completion; a real blockage, a failing or held verdict and a handoff that cannot be read still
    ask for attention. Reports and values are synthetic."""

    REVIEW = "최종 판정: 수정 후 병합 — head 8cbccb3\n\n## 필수 지적\n- P1 하나\n"

    def setUp(self):
        import dispatch_supervisor_terminal
        self.terminal = dispatch_supervisor_terminal
        self.tmp = tempfile.TemporaryDirectory(prefix="scc-memo-")
        self.addCleanup(self.tmp.cleanup)
        self.report = Path(self.tmp.name) / "report.md"
        self.report.write_text(self.REVIEW, encoding="utf-8")

    def classify(self, verdict, blocker):
        text = f"artifact: {self.report}\nverdict: {verdict}\nblocker: {blocker}"
        return self.terminal.classify_session_result(
            {"subtype": "success", "is_error": False, "result": text}, 0, runtime="claude")

    def test_the_two_real_shapes_are_normal_completions(self):
        for label, verdict, blocker in (
                ("blocker note after a dash", "PASS", "none — 최종 판정: 수정 후 병합 — head 8cbccb3 (필수 지적 2건: ① a, ② b)"),
                ("verdict note in parentheses", "PASS (최종 판정: 병합 가능 — head d79893e)", "none"),
                ("blocker note after a colon", "PASS", "none: 필수 지적 2건"),
                ("blocker note in parentheses", "PASS", "none (필수 지적 2건)"),
                ("verdict note after a dash", "PASS — 병합 가능", "none"),
                ("verdict note after a hyphen", "PASS - 병합 가능", "none"),
                ("both notes", "PASS (병합 가능)", "none — 필수 지적 없음")):
            with self.subTest(label):
                outcome = self.classify(verdict, blocker)
                self.assertEqual((outcome.note, outcome.failure_class), ("completed-supervisor", "pass"))
                found = route_authority.handoff_search(f"artifact: {self.report}\nverdict: {verdict}\nblocker: {blocker}")
                self.assertEqual(found["verdict"], "PASS")
        self.assertEqual(route_authority.pass_blocker_note("none — a note"), "a note")
        self.assertEqual(route_authority.pass_blocker_note("none: a note"), "a note")

    def test_a_real_blockage_still_asks_for_attention(self):
        for label, verdict, blocker in (
                ("blocker text", "PASS", "필수 지적 2건"),
                ("blocker starting with none", "PASS", "none, but G1 was skipped"),
                ("blocker naming something else", "PASS", "nonexistent (x)"),
                ("blocker with an empty note", "PASS", "none —"),
                ("not PASS", "not PASS", "none"),
                ("FAIL naming PASS", "FAIL (was PASS)", "none"),
                ("BLOCKED with a note", "BLOCKED (PASS pending)", "none"),
                ("PASSED", "PASSED", "none"),
                ("PASS-like word", "PASS-ish", "none"),
                ("empty verdict note", "PASS (", "none"),
                ("no verdict word", "완료 (PASS)", "none")):
            with self.subTest(label):
                outcome = self.classify(verdict, blocker)
                self.assertEqual((outcome.note, outcome.failure_class), ("dead-contract", "contract"))
        for verdict, note in (("FAIL", "dead-worker-fail"), ("BLOCKED", "dead-worker-blocked"),
                              ("보류 (PASS 아님)", "dead-worker-fail")):
            with self.subTest(verdict):
                self.assertEqual(self.classify(verdict, "none — see report").note, note)
        self.assertEqual(self.classify("PASS", "none").note, "completed-supervisor")
        for text in ("artifact: /r\nverdict: PASS (ok)", "verdict: PASS (ok)\nblocker: none"):
            with self.subTest(text):
                self.assertEqual(self.terminal.classify_session_result(
                    {"subtype": "success", "is_error": False, "result": text}, 0, runtime="claude").note,
                    "dead-contract")


class UnfinishedOwnerVerdictTest(unittest.TestCase):
    """A registered owner that stops on `보류`/`hold` still asks for attention; a finished
    review ending on `수정 후 병합` stays a normal completion (the live terminal path)."""

    def setUp(self):
        import dispatch_supervisor_terminal as terminal
        import route_identity
        self.terminal = terminal
        language = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"})
        language.start()
        self.addCleanup(language.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="scc-owner-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.artifacts = self.root / ".agent_reports"
        self.repo.mkdir()
        self.artifacts.mkdir()
        self.jobs = self.root / "jobs.log"
        self.route = {"capability": "autopilot-code", "capability_mode": "dev",
                      "work_request": {"text": "구현 과제"}, "artifact_root": str(self.artifacts),
                      "cwd": str(self.repo)}
        self.route["route_hash"] = route_identity.route_hash(self.route)
        self.route["route_id"] = route_identity.route_id_from_hash(self.route["route_hash"])
        self.route_file = self.artifacts / (self.route["route_id"] + ".json")
        self.route_file.write_text(json.dumps(self.route), encoding="utf-8")

    def finish(self, report_text, verdict, blocker):
        report = self.artifacts / "report.md"
        report.write_text(report_text, encoding="utf-8")
        attempt = "att-" + uuid.uuid4().hex
        text = f"artifact: {report}\nverdict: {verdict}\nblocker: {blocker}"
        log = self.root / (attempt + ".jsonl")
        log.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                   "result": text}) + "\n", encoding="utf-8")
        meta = {"attempt_schema_version": "2", "attempt_id": attempt, "dispatch_depth": "1",
                "harness": "claude", "owner_harness": "claude", "worker_type": "owner",
                "workflow_completion": "runtime-v1", "artifact_root": str(self.artifacts),
                "route_file": str(self.route_file), "route_hash": self.route["route_hash"],
                "route_id": self.route["route_id"], "parent_sid": "ses-x",
                "parent_completion_delivery": "claude-parent-runtime", "log_file": str(log)}
        self.jobs.write_text(f"2026-11-15T00:00:00Z\topen\t{self.repo}\t{self.repo}\towner\t"
                             + ",".join(f"{k}={v}" for k, v in meta.items()) + "\n", encoding="utf-8")
        raw = self.terminal.classify_session_result(
            {"subtype": "success", "is_error": False, "result": text}, 0, runtime="claude")
        return self.terminal.completed_owner_assessment(raw, text, jobs=self.jobs, attempt_id=attempt)

    def test_work_that_stops_on_hold_still_asks_for_attention(self):
        work = "# 구현 보고\n\n## 바꾼 것\n- a.py 절반\n## 남은 것\n- B안/C안 결정 필요\n"
        for verdict, blocker in (("보류", "사용자 결정 필요 — B안/C안"),
                                 ("hold — CI failing, not finished", "CI shard 3 red"),
                                 ("보류", "none")):
            with self.subTest(verdict=verdict, blocker=blocker):
                outcome = self.finish(work, verdict, blocker)
                self.assertEqual((outcome.note, outcome.failure_class), ("dead-contract", "contract"))
                self.assertNotEqual(outcome.reconcile_reason, "completed-audit-with-findings")

    def test_a_review_that_ends_on_merge_after_fixes_is_a_normal_completion(self):
        outcome = self.finish("최종 판정: 수정 후 병합 — head abcdef1\n\n## 필수 지적\n- P1\n",
                              "수정 후 병합", "none")
        self.assertEqual((outcome.note, outcome.failure_class, outcome.reconcile_reason),
                         ("completed-supervisor", "pass", "completed-audit-with-findings"))


class SummaryContentTest(unittest.TestCase):
    def test_verdict_and_publication_come_from_the_report(self):
        text = ("# 보고\n\n**최종 판정: 수정 후 병합 — head 0123456789ab**\n\n## 해결하지 못한 것\n- P1\n\n"
                "- PR #1640 · head `0123456789abcdef0123` · CI 15/15 통과 · GitHub mergeable `MERGEABLE`\n")
        self.assertEqual(work_intent.report_verdict(text), "수정 후 병합 — head 0123456789ab")
        self.assertEqual(work_intent.report_publication(text, "PR #1640 검토"),
                         "#1640 · head 0123456789ab · CI 15/15 통과 · MERGEABLE")
        self.assertEqual(work_intent.report_publication(text), "")
        self.assertEqual(work_intent.report_verdict("## 바꾼 것\n- x\n"), "")
        self.assertEqual(work_intent.report_publication("PR #12 opened\n"), "")
        self.assertEqual(work_intent.report_sections(text)["remaining"], ["P1", text.splitlines()[-1][2:]])

    def test_the_publication_is_this_works_pr_only(self):
        # The author report's shape: its own PR only in the verdict line, a fenced example
        # notice naming a fixture PR with head and CI words.
        report = ("최종 판정: 완료 — PR #1637 head 350a5b4 · CI 15/15 통과\n\n# 보고\n\n## PR\n"
                  "- PR: https://github.com/o/r/pull/1637 (base `main`)\n\n## 알림 한 건 (합성 값)\n```text\n"
                  "PR: #4101 · head 01ababababab · CI 15/15 통과 · MERGEABLE\n"
                  "산출물: <fixture>/final_report.md · PR https://github.com/example/hearting/pull/4101\n```\n")
        with tempfile.TemporaryDirectory(prefix="scc-summary-") as temporary:
            path = Path(temporary) / "report.md"
            path.write_text(report, encoding="utf-8")
            summary = work_intent.completion_summary(
                {"capability": "autopilot-code", "work_request": {"text": "x"}}, str(path))
        self.assertNotIn("4101", summary)
        self.assertIn("pull/1637", summary)
        self.assertEqual(work_intent.report_publication(report), "")
        self.assertEqual(work_intent.report_pr_urls(report), ["https://github.com/o/r/pull/1637"])

    def test_a_pr_the_evidence_names_first_is_not_this_works_publication(self):
        report = ("최종 판정: 병합 가능 — PR #1637 head 350a5b4\n\n## 근거\n"
                  "- base main = PR #1636 merge, head 54922faf6 (CI 15/15)\n- https://github.com/o/r/pull/1636\n")
        own = report + "- 대상: PR #1637 head 350a5b428243 · CI 15/15 통과 · MERGEABLE\n"
        self.assertEqual(work_intent.report_publication(own), "#1637 · head 350a5b428243 · CI 15/15 통과 · MERGEABLE")
        self.assertEqual((work_intent.report_publication(report), work_intent.report_pr_urls(report)), ("", []))

    def summary(self, report, request="x", capability="review"):
        with tempfile.TemporaryDirectory(prefix="scc-summary-") as temporary:
            path = Path(temporary) / "report.md"
            path.write_text(report, encoding="utf-8")
            return work_intent.completion_summary(
                {"capability": capability, "work_request": {"text": request}}, str(path))

    @staticmethod
    def carried(summary):
        """The summary's `PR:` lines and the PR links on its artifact line."""
        lines = summary.splitlines()
        return ([line for line in lines if line.startswith("PR:")],
                re.findall(r"pull/\d+", " ".join(line for line in lines if line.startswith(("Artifact", "산출물")))))

    EVIDENCE = ("- base main = PR #1636 merge, head 54922faf6 (CI 15/15)\n- https://github.com/o/r/pull/1636\n"
                "- 대상: PR #1637 head 350a5b428243 · CI 15/15 통과\n")
    CLOSED = "최종 판정: 병합 가능 — head 350a5b4\n\n"
    OWN = "#1637 · head 350a5b428243 · CI 15/15 통과"

    def test_the_pr_is_the_one_the_verdict_line_or_title_names_else_the_one_the_request_names(self):
        # A closed review verdict cannot name a PR and this report has no title: no PR is guessed
        # from the evidence, though the PR line's head equals the verdict's.
        report = self.CLOSED + "## 근거\n" + self.EVIDENCE
        self.assertEqual((work_intent.report_publication(report), work_intent.report_pr_urls(report)), ("", []))
        self.assertEqual(self.carried(self.summary(report)), ([], []))
        # The title names this work's PR: it wins over the request, and the other PR's link goes too.
        titled = self.CLOSED + "# 검토 — PR #1637\n\n## 근거\n" + self.EVIDENCE
        self.assertEqual(work_intent.report_publication(titled), self.OWN)
        self.assertEqual(work_intent.report_publication(titled, "PR #1636 검토"), self.OWN)
        self.assertEqual(work_intent.report_pr_urls(titled), [])
        self.assertEqual(self.carried(self.summary(titled)), (["PR: " + self.OWN], []))
        # No PR in the verdict line or title: the one PR the request names, once the report has
        # a line for that number.
        self.assertEqual(work_intent.report_publication(report, "PR #1637 head 350a5b4 를 검토"), self.OWN)
        self.assertEqual(self.carried(self.summary(report, "PR #1637 검토")), (["PR: " + self.OWN], []))
        self.assertEqual(work_intent.report_pr_urls(report, request="PR #1636 검토"),
                         ["https://github.com/o/r/pull/1636"])
        for request in ("x", "", None, "PR #1636과 PR #1637 비교", "https://github.com/o/r/pull/1636 와 PR 1637"):
            with self.subTest(request=request):
                self.assertEqual((work_intent.report_publication(report, request), work_intent.report_pr_urls(report, request=request)),
                                 ("", []))

    def test_a_request_pr_the_report_never_names_is_not_carried(self):
        report = self.CLOSED + "## 근거\n- base main = PR #1636 merge, head 54922faf6 (CI 15/15)\n- 대상 head 350a5b428243 · CI 15/15\n"
        self.assertEqual((work_intent.report_publication(report, "PR #1637 검토"),
                          work_intent.report_pr_urls(report, request="PR #1637 검토")), ("", []))
        self.assertEqual(self.carried(self.summary(report, "PR #1637 검토")), ([], []))

    def test_a_pr_the_request_body_names_for_reference_is_not_this_works(self):
        # Only the request's first line (its title) says which PR this work is about; a body that
        # mentions another PR as a reference carries no PR line or link.
        report = self.CLOSED + "## 근거\n" + self.EVIDENCE.replace("- 대상: PR #1637 head 350a5b428243 · CI 15/15 통과\n", "")
        report += "- PR #1636 · head 54922faf6 · CI 15/15 통과\n"
        for request in ("# 겹치는 파일 정리\n\nPR #1636과 겹치는 파일을 정리한다", "# 후속 정리\n\n앞 PR #1636의 후속 정리",
                        "\n\n# 정리\n본문 https://github.com/o/r/pull/1636"):
            with self.subTest(request=request):
                self.assertEqual((work_intent.report_publication(report, request), work_intent.report_pr_urls(report, request=request)),
                                 ("", []))
                self.assertEqual(self.carried(self.summary(report, request)), ([], []))
        # The title naming the PR still carries it (and a blank first line is skipped).
        for request in ("# 검토 — PR #1636\n\nPR #1637과 비교", "\n# 검토 — PR #1636\n본문"):
            self.assertTrue(work_intent.report_publication(report, request).startswith("#1636 · head 54922faf6 · CI 15/15"))

    def test_no_other_pr_is_carried_whatever_its_head(self):
        # R3's synthetic shapes: another PR's line or link is never carried, with or without a
        # head that matches the verdict's, a merge's second head, a fenced example or a section
        # heading naming it.
        other = "- base main = PR #1636 (https://github.com/o/r/pull/1636) merge, head 54922faf6 (CI 15/15)\n"
        merge = "최종 판정: 완료 — head 7002da8\n\n- PR #1636 · head 2b1d684fb · CI 15/15 통과\n"
        for label, report, request in (
                ("other PR with its link", self.CLOSED + "## 근거\n" + other + "- 대상 head 350a5b428243 · CI 15/15\n", "PR #1637 검토"),
                ("other PR link, target line has no PR", self.CLOSED + "## 근거\n- https://github.com/o/r/pull/1636\n- 대상: head 350a5b428243 · CI 15/15\n", "PR #1637 검토"),
                ("a merge's second head", merge, "PR #1637 검토"),
                ("a fenced title", self.CLOSED + "```text\n# PR #1636 예시\n```\n## 근거\n" + other, "PR #1637 검토"),
                ("a code line", self.CLOSED + "    # PR #1636 예시\n" + other, "PR #1637 검토"),
                ("a section heading", self.CLOSED + "## PR #1636과 겹치는 파일\n" + other, "PR #1637 검토"),
                ("a request naming two", self.CLOSED + other, "PR #1636 · PR #1637")):
            with self.subTest(label):
                self.assertEqual((work_intent.report_publication(report, request), work_intent.report_pr_urls(report, request=request)),
                                 ("", []))

    def test_the_head_is_not_compared_when_the_pr_is_known(self):
        # A merge makes the verdict's head and the PR line's head differ; the PR is known, so both read.
        for line in ("7002da8af (내 commit 2b1d684fb + main 병합 7002da8af)", "2b1d684fb (+ main 병합 commit 7002da8af)", "2b1d684fb"):
            with self.subTest(line):
                report = "최종 판정: 완료 — PR #1637, head 7002da8\n\n- PR #1637 · head " + line + " · CI 15/15 통과\n"
                self.assertTrue(work_intent.report_publication(report).startswith("#1637 · head "))
        self.assertEqual(work_intent.report_pr_urls("최종 판정: 완료 — PR #1637\n\n- https://github.com/o/r/pull/1637\n"),
                         ["https://github.com/o/r/pull/1637"])

    def test_a_wide_table_row_is_never_the_verification(self):
        # A proposal's results table: its first cell may start with a heading word, its other cells are data.
        report = ("# 제안서\n\n## 실제 수\n\n| 일 | Claude | Codex | OpenCode |\n|---|---|---|---|\n"
                  "| 스크립트(python) | 17% · 18% | 12% · 12% | 15% · 16% |\n| 시험·점검 | 12% · 16% | 6% · 5% | 14% · 14% |\n")
        self.assertEqual(work_intent.report_sections(report), {})
        text = self.summary(report)
        self.assertIn(work_intent.ui_translator()("completion.unreadable"), text.splitlines())
        self.assertFalse([line for line in text.splitlines() if line.startswith(("Checks:", "검증:"))])
        # A two-cell `| label | value |` row is still a field.
        self.assertEqual(work_intent.report_sections("| 남은 것 | 저장 오류를 고치지 못함 |\n"),
                         {"remaining": ["저장 오류를 고치지 못함"]})

    def test_the_check_counts_survive_a_long_prose_line(self):
        prose = "검증은 격리 HOME과 사설 tmux에서 loopback 모델로 세 하네스 각각 돌렸고 보고서 기록에 남김 — " * 3
        lines = [prose, "python3 tools/run-tests.py --select a.test.py: 21 passed",
                 "python3 tools/run-tests.py --select b.test.py: 2 failed"]
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"}):
            checks = work_intent._verified_summary(lines, work_intent.ui_translator())
        self.assertLessEqual(len(checks), work_intent.MAX_CHECKS_CHARS)
        self.assertTrue(checks.startswith("명령 2개 · 통과 1 · 실패 1 · 검증은"), checks)

    def test_answer_waits_fold_repeats(self):
        self.assertEqual(session_tidy.answer_wait_text(["사용자 답 대기"] * 3 + ["배포할까요?"]),
                         ["[답변 필요] 사용자 답 대기 ×3", "[답변 필요] 배포할까요?"])
        self.assertEqual(session_tidy.answer_wait_text([f"q{n}" for n in range(5)]),
                         ["[답변 필요] q0", "[답변 필요] q1", "[답변 필요] q2"])


if __name__ == "__main__":
    unittest.main()
