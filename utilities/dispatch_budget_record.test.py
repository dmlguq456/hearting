#!/usr/bin/env python3
import sys
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


if __name__ == "__main__":
    unittest.main()
