#!/usr/bin/env python3

import json
from pathlib import Path
import tempfile
import unittest

import dispatch_continuation_budget as MODULE


class ContinuationBudgetTest(unittest.TestCase):
    def seal(self, value):
        import hashlib
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        value["route_hash"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
        value["route_id"] = "rt-" + value["route_hash"].split(":", 1)[1][:16]
        return value

    def test_bound_route_uses_nodes_plus_unique_retry_boundaries(self):
        with tempfile.TemporaryDirectory() as raw:
            route = Path(raw) / "route.json"
            value = self.seal({
                "schema_version": 2,
                "nodes": [{"id": f"node-{index}"} for index in range(8)],
                "resume_retry_boundaries": [f"node-{index}" for index in range(7)],
            })
            route.write_text(json.dumps(value), encoding="utf-8")
            budget = MODULE.resolve_continuation_budget(
                route_file=route,
                route_id=value["route_id"],
                route_hash=value["route_hash"],
            )
        self.assertEqual(15, budget.ordinary)
        self.assertEqual(15 + MODULE.TERMINAL_RESERVE_DEFAULT, budget.limit)
        self.assertEqual(budget.ordinary + budget.reserved, budget.limit)
        self.assertEqual("bound-route", budget.source)
        self.assertEqual(8, budget.declared_nodes)
        self.assertEqual(7, budget.retry_slots)

    def test_unbound_or_mismatched_route_keeps_finite_floor(self):
        with tempfile.TemporaryDirectory() as raw:
            route = Path(raw) / "route.json"
            value = self.seal({
                "schema_version": 2,
                "nodes": [{"id": "node"}],
                "resume_retry_boundaries": [],
            })
            route.write_text(json.dumps(value), encoding="utf-8")
            budget = MODULE.resolve_continuation_budget(
                route_file=route, route_id="rt-foreign", route_hash=value["route_hash"]
            )
        self.assertEqual(MODULE.COMPATIBILITY_FLOOR, budget.ordinary)
        self.assertEqual(MODULE.COMPATIBILITY_FLOOR + MODULE.TERMINAL_RESERVE_DEFAULT, budget.limit)
        self.assertEqual("compatibility-floor", budget.source)

    def test_positive_explicit_override_replaces_route_value(self):
        budget = MODULE.resolve_continuation_budget(explicit=3)
        self.assertEqual(3, budget.ordinary)
        self.assertEqual(3 + MODULE.TERMINAL_RESERVE_DEFAULT, budget.limit)
        self.assertEqual("explicit-owner-override", budget.source)


class BudgetSplitTest(unittest.TestCase):
    def test_limit_equals_ordinary_plus_reserved_and_ordinary_never_shrinks_below_prior_limit(self):
        prior_limit = MODULE.COMPATIBILITY_FLOOR
        budget = MODULE.resolve_continuation_budget()
        self.assertEqual(budget.limit, budget.ordinary + budget.reserved)
        self.assertGreaterEqual(budget.ordinary, prior_limit)
        self.assertGreaterEqual(budget.stall, 1)


class AdmissionGateTest(unittest.TestCase):
    def _ledger(self, ordinary=3, reserved=1, stall=2):
        import types
        budget = types.SimpleNamespace(ordinary=ordinary, reserved=reserved, stall=stall)
        return MODULE.ContinuationLedger(budget)

    def test_admit_equation_and_false_predicate_yields_zero_start_with_typed_refusal(self):
        ledger = self._ledger(ordinary=1, reserved=1, stall=1)
        # gross_remaining(1) is not > terminal_reserve(1) -> ordinary refused.
        verdict = ledger.admit(purpose="ordinary", stalled=False, reservation_ok=True)
        self.assertFalse(verdict.admitted)
        self.assertTrue(verdict.refusal)
        self.assertEqual(verdict.gross_remaining, 1)

        ledger2 = self._ledger(ordinary=3, reserved=1, stall=2)
        verdict2 = ledger2.admit(purpose="ordinary", stalled=False, reservation_ok=False)
        self.assertFalse(verdict2.admitted)
        self.assertEqual(verdict2.refusal, "continuation-budget-unavailable")

    def test_remainders_stay_non_negative_and_reserve_boundary_admits_terminal_handoff_only(self):
        ledger = self._ledger(ordinary=2, reserved=1, stall=5)
        first = ledger.admit(purpose="ordinary", stalled=False, reservation_ok=True)
        self.assertTrue(first.admitted)
        self.assertEqual(ledger.gross_remaining, 1)
        # gross_remaining(1) == terminal_reserve(1): ordinary is refused now.
        second = ledger.admit(purpose="ordinary", stalled=False, reservation_ok=True)
        self.assertFalse(second.admitted)
        self.assertGreaterEqual(ledger.gross_remaining, 0)
        terminal = ledger.admit(purpose="terminal-handoff", stalled=False, reservation_ok=True)
        self.assertTrue(terminal.admitted)
        self.assertEqual(terminal.charged, "reserved")
        self.assertGreaterEqual(ledger.reserved_remaining, 0)

    def test_broken_route_binding_floor_path_keeps_reserved_remaining_at_least_one(self):
        budget = MODULE.resolve_continuation_budget(route_file=None, route_id="", route_hash="")
        self.assertEqual(budget.source, "compatibility-floor")
        self.assertGreaterEqual(budget.reserved, 1)

    def test_non_terminal_purpose_consuming_reserve_is_continuation_reserved_scope_violation(self):
        ledger = self._ledger()
        verdict = ledger.admit(purpose="reserved", stalled=False, reservation_ok=True)
        self.assertFalse(verdict.admitted)
        self.assertEqual(verdict.refusal, "continuation-reserved-scope-violation")


if __name__ == "__main__":
    unittest.main()
