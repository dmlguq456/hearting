#!/usr/bin/env python3
"""S3a — the one place a deferred completion row is judged as success or not."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch_attempt_policy as POLICY  # noqa: E402


def _deferred_pending_metadata() -> dict[str, str]:
    return {
        "note": "completion-deferred",
        "failure_class": "infrastructure",
        "classifier_source": POLICY.DEFERRED_COMPLETION_SOURCE,
        "reconcile_reason": "completion-transient:TimeoutExpired",
    }


def _deferred_completed_metadata() -> dict[str, str]:
    metadata = _deferred_pending_metadata()
    metadata.update({
        "note": "completed-marker",
        "completion_marker": "/artifacts/.runtime/completions/execute.json",
        "completion_marker_history": "/artifacts/.runtime/completions/execute.1.json",
    })
    return metadata


class DeferredCompletionStatesTest(unittest.TestCase):
    def test_non_deferred_row_is_untouched(self):
        ordinary_pass = {"note": "completed-supervisor", "failure_class": "pass"}
        ordinary_fail = {"note": "dead-worker-fail", "failure_class": "runtime"}
        self.assertEqual(POLICY.deferred_completion(ordinary_pass), "")
        self.assertTrue(POLICY.verdict_pass(ordinary_pass))
        self.assertTrue(POLICY.success_note(ordinary_pass))
        self.assertEqual(POLICY.deferred_completion(ordinary_fail), "")
        self.assertFalse(POLICY.verdict_pass(ordinary_fail))
        self.assertFalse(POLICY.success_note(ordinary_fail))

    def test_deferred_pending_row_is_not_success(self):
        pending = _deferred_pending_metadata()
        self.assertEqual(POLICY.deferred_completion(pending), "pending")
        self.assertFalse(POLICY.verdict_pass(pending))
        self.assertFalse(POLICY.success_note(pending))
        self.assertNotIn("completion-deferred", POLICY.SUCCESS_NOTES)

    def test_deferred_row_completed_by_marker_is_success(self):
        completed = _deferred_completed_metadata()
        self.assertEqual(POLICY.deferred_completion(completed), "completed")
        self.assertTrue(POLICY.verdict_pass(completed))
        self.assertTrue(POLICY.success_note(completed))

    def test_committed_outcome_deferred_states(self):
        self.assertEqual(
            POLICY.committed_outcome("done", _deferred_pending_metadata()),
            "completion-deferred",
        )
        self.assertEqual(
            POLICY.committed_outcome("done", _deferred_completed_metadata()),
            "succeeded",
        )
        self.assertEqual(
            POLICY.committed_outcome("done", {"note": "completed-marker", "failure_class": "pass"}),
            "succeeded",
        )


class PolicyCompletionDeferredIsNotFallbackTest(unittest.TestCase):
    def test_pending_deferred_row_asks_for_completion_not_retry(self):
        decision = POLICY.decide_attempt(
            "done", _deferred_pending_metadata(), process_state="quiescent",
        )
        self.assertEqual(decision.outcome, "completion-deferred")
        self.assertEqual(decision.action, "complete")
        self.assertEqual(decision.responsible, "completion-controller")
        self.assertFalse(decision.retry_allowed)
        self.assertEqual(POLICY.required_action("done", _deferred_pending_metadata()), "complete-deferred")

    def test_marker_bound_deferred_row_advances_as_succeeded(self):
        decision = POLICY.decide_attempt(
            "done", _deferred_completed_metadata(), process_state="quiescent",
        )
        self.assertEqual(decision.outcome, "succeeded")
        self.assertEqual(decision.action, "advance")
        self.assertEqual(
            POLICY.required_action("done", _deferred_completed_metadata()), "advance-completed",
        )


if __name__ == "__main__":
    unittest.main()
