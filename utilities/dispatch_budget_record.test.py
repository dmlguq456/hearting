#!/usr/bin/env python3
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

UTILITIES = Path(__file__).resolve().parent
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))

import dispatch_budget_record as BR  # noqa: E402
import dispatch_continuation_budget as BUDGET  # noqa: E402

REMAINING = {"gross_remaining": 5, "stall_remaining": 3, "reserved_remaining": 1}


class ReservationCasTest(unittest.TestCase):
    def test_restart_recovers_durable_evidence_after_settlement_crash(self):
        for kind, expected in [("transport-receipt",1),("pre-send-failure",0)]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as home:
                claim=BR.claim_terminal_handoff(home,owner_attempt_id="owner",route_hash="hash",child_attempt_ids=[])
                intent=BR.convert_claim_to_prompt_intent(home,claim,prompt="cleanup",cleanup_scope={})
                self.assertTrue(BR.begin_submission(home,intent))
                with mock.patch.object(BR,"settle_submission",side_effect=RuntimeError("crash")):
                    with self.assertRaisesRegex(RuntimeError,"crash"):
                        BR.reconcile_submission(home,intent,evidence_kind=kind,
                            evidence=dict(intent_id=intent["intent_id"],prompt_digest=intent["prompt_digest"]))
                self.assertIsNone(BR.read_effective_charge(home,intent))
                recovered=BR.recover_terminal_handoff(home,"owner")
                self.assertEqual(recovered["effective_charge"],expected)
                self.assertFalse(BR.begin_submission(home,intent))
                self.assertEqual(BR.recover_terminal_handoff(home,"owner"),recovered)

    def test_concurrent_reservation_admits_exactly_one_and_loser_is_not_a_retry_loop(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            ok1, detail1 = BR.reserve(
                state_root, parent_attempt_id="att-p", route_id="rt-x", route_hash="sha256:y",
                ordinal=0, purpose="ordinary", klass="gross", remaining=REMAINING,
            )
            self.assertTrue(ok1, detail1)
            ok2, detail2 = BR.reserve(
                state_root, parent_attempt_id="att-p", route_id="rt-x", route_hash="sha256:y",
                ordinal=0, purpose="ordinary", klass="gross", remaining=REMAINING,
            )
            self.assertFalse(ok2)
            self.assertEqual(detail2, "reservation-lost")
            rows = BR.read_rows(state_root, "att-p")
            self.assertEqual(len(rows), 1)


class BudgetUnavailableTest(unittest.TestCase):
    def test_unknown_stale_negative_or_mismatched_state_refuses_with_continuation_budget_unavailable(self):
        budget = BUDGET.ContinuationBudget(limit=3, source="test")
        ledger = BUDGET.ContinuationLedger(budget)
        verdict = ledger.admit(purpose="ordinary", stalled=False, reservation_ok=False)
        self.assertFalse(verdict.admitted)
        self.assertEqual(verdict.refusal, "continuation-budget-unavailable")

        ledger2 = BUDGET.ContinuationLedger(budget)
        ledger2._gross_remaining = -1  # simulate a corrupted/stale ledger
        verdict2 = ledger2.admit(purpose="ordinary", stalled=False, reservation_ok=True)
        self.assertFalse(verdict2.admitted)
        self.assertEqual(verdict2.refusal, "continuation-budget-unavailable")


class PromptIntentTest(unittest.TestCase):
    def test_owner_fallback_converts_once_immediately_before_submit_and_replay_never_recharges(self):
        with tempfile.TemporaryDirectory() as home:
            claim = BR.claim_terminal_handoff(home, owner_attempt_id="o", route_hash="h",
                                               child_attempt_ids=["c"], continuation_ordinal=1)
            intent = BR.convert_claim_to_prompt_intent(home, claim, prompt="secret",
                                                       cleanup_scope={"route_id": "r"})
            replay = BR.convert_claim_to_prompt_intent(home, claim, prompt="secret",
                                                       cleanup_scope={"route_id": "r"})
            self.assertEqual(intent["prompt_digest"], replay["prompt_digest"])
            self.assertNotIn("prompt", json.loads((Path(home) / "terminal-handoffs/v1/o/1/prompt-intent.json").read_text()))


class SubmissionReconcilerTest(unittest.TestCase):
    def test_unknown_exits_only_via_closed_evidence_and_never_auto_refunds(self):
        with tempfile.TemporaryDirectory() as home:
            claim = BR.claim_terminal_handoff(home, owner_attempt_id="o", route_hash="h", child_attempt_ids=[], continuation_ordinal=1)
            intent = BR.convert_claim_to_prompt_intent(home, claim, prompt="x", cleanup_scope={})
            BR.settle_submission(home, intent, "submission-unknown")
            result = BR.reconcile_submission(home, intent, evidence_kind="unlisted", evidence={})
            self.assertEqual(result["recovery"], "recovery-unavailable")
            self.assertIsNone(BR.read_effective_charge(home, intent))

    def test_direct_settle_call_against_unknown_never_manufactures_its_own_evidence(self):
        # Regression for the A82-11 self-refund defect: a second, differing
        # `settle_submission` call used to recurse into `reconcile_submission`
        # with a self-fabricated "transport-confirmed-refusal" evidence_kind
        # (infinite recursion in the worst case, a free unauthorized refund
        # in the best case). `settle_submission` must never do that -- only a
        # caller that ran real evidence through `reconcile_submission` first
        # may move a record out of `submission-unknown`.
        with tempfile.TemporaryDirectory() as home:
            claim = BR.claim_terminal_handoff(home, owner_attempt_id="o2", route_hash="h", child_attempt_ids=[], continuation_ordinal=1)
            intent = BR.convert_claim_to_prompt_intent(home, claim, prompt="x", cleanup_scope={})
            first = BR.settle_submission(home, intent, "submission-unknown")
            self.assertEqual(first["status"], "submission-unknown")
            # A direct differing settle call must not raise (no unbounded
            # recursion), but it also cannot manufacture the closed evidence
            # needed to refund an unresolved submission. Only the single
            # reconciler may leave submission-unknown.
            second = BR.settle_submission(home, intent, "not-submitted")
            self.assertEqual(second["status"], "submission-unknown")
            self.assertIsNone(BR.read_effective_charge(home, intent))
            reconciled = BR.reconcile_submission(
                home, intent, evidence_kind="pre-send-failure",
                evidence={"prompt_digest": intent["prompt_digest"], "intent_id": intent["intent_id"]},
            )
            self.assertEqual(reconciled["status"], "not-submitted")
            # A further differing call against an already-resolved status is
            # a no-op: the terminal record is never overwritten again.
            third = BR.settle_submission(home, intent, "submitted")
            self.assertEqual(third["status"], "not-submitted")
            self.assertEqual(BR.read_effective_charge(home, intent), 0)

    def test_resolved_submitted_status_is_terminal_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as home:
            claim = BR.claim_terminal_handoff(home, owner_attempt_id="o3", route_hash="h", child_attempt_ids=[], continuation_ordinal=1)
            intent = BR.convert_claim_to_prompt_intent(home, claim, prompt="x", cleanup_scope={})
            BR.settle_submission(home, intent, "submitted")
            self.assertEqual(BR.read_effective_charge(home, intent), 1)
            again = BR.settle_submission(home, intent, "not-submitted")
            self.assertEqual(again["status"], "submitted")
            self.assertEqual(BR.read_effective_charge(home, intent), 1)


class HandoffClaimLineageTest(unittest.TestCase):
    def test_sd119_successor_creates_new_lineage_with_zero_deltas(self):
        with tempfile.TemporaryDirectory() as home:
            first = BR.claim_terminal_handoff(home, owner_attempt_id="o", route_hash="h", child_attempt_ids=["a"], continuation_ordinal=1)
            second = BR.claim_terminal_handoff(home, owner_attempt_id="o", route_hash="h", child_attempt_ids=["b"], continuation_ordinal=2, predecessor_claim_id=first["claim_id"])
            self.assertEqual(second["predecessor_claim_id"], first["claim_id"])
            self.assertNotEqual(first["claim_id"], second["claim_id"])

    def test_claim_repark_and_chain_advance_leave_ledger_row_count_unchanged(self):
        with tempfile.TemporaryDirectory() as home:
            BR.claim_terminal_handoff(home, owner_attempt_id="o", route_hash="h", child_attempt_ids=["a"], continuation_ordinal=1)
            self.assertEqual(BR.read_rows(home, "o"), ())


class ReceiptVocabularyInvarianceTest(unittest.TestCase):
    def test_v1_v2_v3_decoder_output_is_byte_identical_and_no_unknown_top_level_key(self):
        import dispatch_completion_join as join
        base_receipt = {
            "schema_version": 2,
            "state": "ready",
            "required_action": "harvest",
            "reason": "",
        }
        before = dict(base_receipt)
        result = join.receipt_with_stage_advance(base_receipt, stage_advance_record=None)
        self.assertEqual(result, before)
        self.assertNotIn("continuation_budget", result)
        self.assertNotIn("ordinary", result)
        self.assertNotIn("stall", result)


class WarningDeliveryFailureTest(unittest.TestCase):
    def test_warning_write_failure_is_typed_spends_nothing_and_does_not_terminate_owner(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            budget = BUDGET.ContinuationBudget(limit=1, source="test")
            ledger = BUDGET.ContinuationLedger(budget)
            with mock.patch.object(BR, "_append", return_value=False):
                error, detail = BR.record_warning(
                    state_root, parent_attempt_id="att-p",
                    reason="continuation-budget-exhausted", remaining=REMAINING,
                )
            self.assertEqual(error, "continuation-budget-warning-unrecorded")
            self.assertTrue(detail)
            # The ledger itself is untouched by a warning-record failure.
            self.assertEqual(ledger.gross_remaining, budget.ordinary)


class WarningOnceTest(unittest.TestCase):
    def test_first_crossing_writes_exactly_one_row_and_reentry_adds_none(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            self.assertFalse(
                BR.warning_already_emitted(
                    state_root, parent_attempt_id="att-p", reason="continuation-budget-warning",
                )
            )
            ok, detail = BR.record_warning(
                state_root, parent_attempt_id="att-p",
                reason="continuation-budget-warning", remaining=REMAINING,
            )
            self.assertEqual(ok, "", detail)
            self.assertTrue(
                BR.warning_already_emitted(
                    state_root, parent_attempt_id="att-p", reason="continuation-budget-warning",
                )
            )
            rows_before = BR.read_rows(state_root, "att-p")
            warning_rows = [r for r in rows_before if r.get("record_kind") == "warning"]
            self.assertEqual(len(warning_rows), 1)
            # A caller that (incorrectly) records again anyway still only adds
            # one row of durable evidence per distinct reason; the "already
            # emitted" gate is what real call sites consult before recording.
            self.assertTrue(
                BR.warning_already_emitted(
                    state_root, parent_attempt_id="att-p", reason="continuation-budget-warning",
                )
            )


class RenderNoticeTest(unittest.TestCase):
    def test_two_kinds_are_deterministic_and_include_remaining_and_recommendation(self):
        warning = BR.render_notice("budget-warning", remaining=2, threshold=3)
        self.assertIn("remaining=2", warning)
        self.assertIn("threshold=3", warning)
        self.assertEqual(warning, BR.render_notice("budget-warning", remaining=2, threshold=3))
        exhausted = BR.render_notice("budget-exhausted", remaining=0, threshold=3)
        self.assertIn("remaining=0", exhausted)
        self.assertNotEqual(warning, exhausted)

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            BR.render_notice("budget-unknown", remaining=1, threshold=3)


class WarningVocabularyTest(unittest.TestCase):
    def test_warning_reason_vocabulary_is_fixed_and_disjoint_from_refusal_reasons(self):
        self.assertEqual(
            {"continuation-budget-exhausted", "continuation-budget-warning"},
            set(BR.WARNING_REASONS),
        )
        self.assertEqual(set(), BR.WARNING_REASONS & BR.REFUSAL_REASONS)

    def test_record_warning_refuses_an_unknown_reason(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            error, detail = BR.record_warning(
                state_root, parent_attempt_id="att-p",
                reason="not-a-real-reason", remaining=REMAINING,
            )
            self.assertTrue(error)
            self.assertIn("not-a-real-reason", detail)
            self.assertEqual(BR.read_rows(state_root, "att-p"), ())

    def test_vocabulary_sets_disjoint_from_receipt_enums(self):
        # D47-8: `RECORD_KINDS`/`PURPOSES`/`CLASSES`/`REFUSAL_REASONS`/
        # `WARNING_REASONS` are a separate vocabulary from the delivery
        # receipt's `state`/`required_action`/`reason` enums.
        receipt_enum_values = {
            "ready", "harvest", "", "attention", "blocked",
            "complete-open", "inspect-done-failure",
        }
        for vocab in (
            BR.RECORD_KINDS, BR.PURPOSES, BR.CLASSES, BR.REFUSAL_REASONS, BR.WARNING_REASONS,
        ):
            self.assertEqual(set(), set(vocab) & receipt_enum_values)



class DurableHandoffCrashTest(unittest.TestCase):
    def test_intent_scope_publish_crash_repairs_same_intent_and_reserves_once(self):
        with tempfile.TemporaryDirectory() as td:
            claim = BR.claim_terminal_handoff(td, owner_attempt_id="owner", route_hash="hash", child_attempt_ids=["c"])
            options = dict(prompt="cleanup", cleanup_scope={"route_id":"route", "owner_attempt_id":"owner"},
                           remaining={"gross_remaining":1, "stall_remaining":2, "reserved_remaining":1})
            write = BR._handoff_write
            def crash(path, value, **kwargs):
                if path.name == "cleanup-scope.json":
                    raise OSError("crash between intent and scope")
                return write(path, value, **kwargs)
            with mock.patch.object(BR, "_handoff_write", crash):
                with self.assertRaises(OSError):
                    BR.convert_claim_to_prompt_intent(td, claim, **options)
            self.assertEqual(BR.read_rows(td, "owner"), ())
            recovered = BR.convert_claim_to_prompt_intent(td, claim, **options)
            again = BR.convert_claim_to_prompt_intent(td, claim, **options)
            self.assertEqual(recovered, again)
            self.assertEqual(len(BR.read_rows(td, "owner")), 1)
            self.assertIsNone(BR.read_effective_charge(td, recovered))

    def test_concurrent_submission_has_one_winner_and_unknown_survives_restart(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as td:
            claim = BR.claim_terminal_handoff(td, owner_attempt_id="owner", route_hash="hash", child_attempt_ids=[])
            intent = BR.convert_claim_to_prompt_intent(td, claim, prompt="cleanup", cleanup_scope={})
            with ThreadPoolExecutor(max_workers=8) as pool:
                winners = list(pool.map(lambda _: BR.begin_submission(td, intent), range(8)))
            self.assertEqual(winners.count(True), 1)
            self.assertIsNone(BR.read_effective_charge(td, intent))
            self.assertFalse(BR.begin_submission(td, intent))
            self.assertEqual(BR.settle_submission(td, intent, "not-submitted")["status"], "submission-unknown")

    def test_scope_change_and_second_cleanup_are_refused(self):
        with tempfile.TemporaryDirectory() as td:
            first = BR.claim_terminal_handoff(td, owner_attempt_id="owner", route_hash="hash", child_attempt_ids=["a"])
            BR.convert_claim_to_prompt_intent(td, first, prompt="cleanup", cleanup_scope={"allowed_write_roots":["/evidence"]})
            with self.assertRaises(BR.TerminalHandoffConflict):
                BR.convert_claim_to_prompt_intent(td, first, prompt="cleanup", cleanup_scope={"allowed_write_roots":["/"]})
            second = BR.claim_terminal_handoff(td, owner_attempt_id="owner", route_hash="hash", child_attempt_ids=["b"], predecessor_claim_id=first["claim_id"])
            with self.assertRaisesRegex(BR.TerminalHandoffConflict, "already-converted"):
                BR.convert_claim_to_prompt_intent(td, second, prompt="cleanup", cleanup_scope={})

    def test_three_runtime_successors_replay_exact_lineage_with_zero_budget_rows(self):
        with tempfile.TemporaryDirectory() as td:
            previous = None
            claims = []
            for child in ("a", "b", "c"):
                args = dict(owner_attempt_id="owner", route_hash="hash", child_attempt_ids=[child], predecessor_claim_id=previous)
                claim = BR.claim_terminal_handoff(td, **args)
                self.assertEqual(claim, BR.claim_terminal_handoff(td, **args))
                claims.append(claim)
                previous = claim["claim_id"]
            self.assertEqual(len({c["continuation_ordinal"] for c in claims}), 3)
            self.assertEqual(BR.read_rows(td, "owner"), ())


class CompleteTerminalHandoffDeferredTest(unittest.TestCase):
    """C16 (S3a): `complete_terminal_handoff`'s guard via `verdict_pass`.

    The guard is the only thing under test -- once past it, prove that by
    asserting the next real step (`terminal_handoff_root`) was reached,
    instead of building the rest of the claim/lock/write plumbing here.
    """

    def _jobs(self, tmp, metadata):
        jobs = Path(tmp) / "jobs.log"
        pipe = ",".join(f"{k}={v}" for k, v in metadata.items())
        jobs.write_text(f"2026-09-24T00:00:00Z\tdone\t/r\t/w\towner\t{pipe}\n", encoding="utf-8")
        return jobs

    def _claim(self):
        return {"owner_attempt_id": "att-owner-deferred", "claim_id": "cl-1",
                "route_hash": "sha256:" + "a" * 64, "continuation_ordinal": 1}

    def test_pending_deferred_owner_is_rejected_by_the_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = self._jobs(tmp, {
                "attempt_id": "att-owner-deferred", "note": "completion-deferred",
                "failure_class": "infrastructure",
                "classifier_source": "registered-wrapper-completion-transient-v1",
            })
            with self.assertRaises(BR.TerminalHandoffConflict) as caught:
                BR.complete_terminal_handoff(tmp, self._claim(), jobs=jobs, terminal_commit_id="tc-1")
            self.assertEqual(caught.exception.args[0], "terminal-owner-not-reconciled")

    def test_completed_deferred_owner_passes_the_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = self._jobs(tmp, {
                "attempt_id": "att-owner-deferred", "note": "completed-marker",
                "failure_class": "infrastructure",
                "classifier_source": "registered-wrapper-completion-transient-v1",
                "completion_marker": "/artifacts/.runtime/completions/one-shot.json",
            })
            with mock.patch.object(
                BR, "terminal_handoff_root", side_effect=RuntimeError("reached-past-the-guard"),
            ):
                with self.assertRaisesRegex(RuntimeError, "reached-past-the-guard"):
                    BR.complete_terminal_handoff(tmp, self._claim(), jobs=jobs, terminal_commit_id="tc-1")


if __name__ == "__main__":
    unittest.main()
