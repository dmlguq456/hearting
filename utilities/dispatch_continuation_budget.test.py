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

    def test_exhausted_stall_counter_does_not_block_a_real_progress_gross_admit(self):
        # §13.34.4-(2) declares three separate remainders, not one equation
        # gated on a single `stalled` axis: "① gross ceiling은 기존 상한을
        # 유지한다" (the gross ceiling is a safety ceiling on its own, never
        # replaced by the stall detector) together with "② ... 이 두 계열은
        # gross ceiling을 소비하지 않는다" (the stall counter's two
        # no-progress series never draw from the gross ceiling) is only
        # satisfiable if the inverse also holds: an ordinary, real-progress
        # (`stalled=False`) admit never draws from -- or gets blocked by --
        # the stall counter. Reading the admission gate as one literal
        # equation that ANDs `stall_remaining > 0` into every `purpose=
        # "ordinary"` admit (stalled or not) would mean a single stall
        # exhaustion permanently blocks all further gross-ceiling progress,
        # which breaks ①'s "existing ceiling is preserved" guarantee the
        # moment stall hits zero. This is the only reading that satisfies
        # both clauses at once, so a `stall_remaining == 0`, real-progress
        # `stalled=False` request with headroom on the gross ceiling must be
        # admitted.
        ledger = self._ledger(ordinary=3, reserved=1, stall=0)
        verdict = ledger.admit(purpose="ordinary", stalled=False, reservation_ok=True)
        self.assertTrue(verdict.admitted)
        self.assertEqual(verdict.charged, "gross")
        self.assertEqual(ledger.gross_remaining, 2)
        self.assertEqual(ledger.stall_remaining, 0)

    def test_exhausted_stall_counter_still_refuses_a_stalled_admit(self):
        # Same starting state as above, but requested as `stalled=True` (the
        # no-progress axis §13.34.4-(2)-② actually gates) -- this must be
        # refused with a typed refusal even though the gross ceiling has
        # headroom, because the stall counter -- not the gross ceiling -- is
        # the one being spent on this axis.
        ledger = self._ledger(ordinary=3, reserved=1, stall=0)
        verdict = ledger.admit(purpose="ordinary", stalled=True, reservation_ok=True)
        self.assertFalse(verdict.admitted)
        self.assertEqual(verdict.refusal, "continuation-admission-refused")
        self.assertEqual(ledger.gross_remaining, 3)

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


class RouteIdentityHashRegressionTest(unittest.TestCase):
    """SD-116 WP1 anchor: a route carrying the SD-118 lineage fields
    (`owner_attempt_id`, `route_family_key`) must resolve as `bound-route`,
    not fall to the compatibility floor -- this is the exact defect that made
    every post-SD-118 route lose its declared budget."""

    def test_lineage_fields_resolve_as_bound_route(self):
        with tempfile.TemporaryDirectory() as raw:
            route = Path(raw) / "route.json"
            value = {
                "schema_version": 2,
                "nodes": [{"id": f"node-{index}"} for index in range(8)],
                "resume_retry_boundaries": [f"node-{index}" for index in range(7)],
                "owner_attempt_id": "att-lineage-example",
                "route_family_key": "sha256:" + "f" * 64,
            }
            import route_identity as RI
            digest = RI.route_hash(value)
            value["route_hash"] = digest
            value["route_id"] = RI.route_id_from_hash(digest)
            route.write_text(json.dumps(value), encoding="utf-8")
            budget = MODULE.resolve_continuation_budget(
                route_file=route,
                route_id=value["route_id"],
                route_hash=value["route_hash"],
            )
        self.assertEqual("bound-route", budget.source)
        self.assertEqual(8, budget.declared_nodes)
        self.assertEqual(7, budget.retry_slots)
        self.assertEqual(15, budget.ordinary)
        self.assertEqual(16, budget.limit)

    def test_pre_wp1_two_key_hash_scheme_falls_to_floor(self):
        """Control: a route whose stored `route_hash` was sealed with the old
        (`route_hash`,`route_id`)-only exclusion scheme -- i.e. computed over
        a payload that already includes the lineage fields -- no longer
        matches `route_identity.route_hash()`'s 4-key exclusion. This is the
        mirror image of the real SD-118 defect and confirms the resolver
        genuinely depends on the shared exclusion set rather than coincidence."""
        import hashlib
        with tempfile.TemporaryDirectory() as raw:
            route = Path(raw) / "route.json"
            value = {
                "schema_version": 2,
                "nodes": [{"id": f"node-{index}"} for index in range(8)],
                "resume_retry_boundaries": [f"node-{index}" for index in range(7)],
                "owner_attempt_id": "att-lineage-example",
                "route_family_key": "sha256:" + "e" * 64,
            }
            old_scheme_bare = {
                key: val for key, val in value.items()
                if key not in {"route_hash", "route_id"}
            }
            old_digest = "sha256:" + hashlib.sha256(
                json.dumps(old_scheme_bare, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            value["route_hash"] = old_digest
            value["route_id"] = "rt-" + old_digest.split(":", 1)[1][:16]
            route.write_text(json.dumps(value), encoding="utf-8")
            budget = MODULE.resolve_continuation_budget(
                route_file=route,
                route_id=value["route_id"],
                route_hash=value["route_hash"],
            )
        self.assertEqual("compatibility-floor", budget.source)


class SealedBudgetBlockTest(unittest.TestCase):
    """SD-116 WP4 (D47-9, SD-116 (a)): a compiled `continuation_budget`
    block takes priority over the legacy nodes+retry-boundaries derivation."""

    def _sealed_route(self, block):
        value = {
            "schema_version": 2,
            "nodes": [{"id": "node-0"}],
            "resume_retry_boundaries": [],
            "continuation_budget": block,
        }
        import route_identity as RI
        digest = RI.route_hash(value)
        value["route_hash"] = digest
        value["route_id"] = RI.route_id_from_hash(digest)
        return value

    def test_sealed_block_route_resolves_with_source_sealed_block_and_block_values(self):
        block = {
            "contract_version": 1, "declared_nodes": 20, "review_round_cap": 2,
            "gap": 1, "retry": 1, "reserved": 1, "ordinary": 22, "limit": 23,
        }
        with tempfile.TemporaryDirectory() as raw:
            route = Path(raw) / "route.json"
            value = self._sealed_route(block)
            route.write_text(json.dumps(value), encoding="utf-8")
            budget = MODULE.resolve_continuation_budget(
                route_file=route, route_id=value["route_id"], route_hash=value["route_hash"],
            )
        self.assertEqual("sealed-block", budget.source)
        self.assertEqual(22, budget.ordinary)
        self.assertEqual(23, budget.limit)
        self.assertEqual(20, budget.declared_nodes)
        self.assertEqual(1, budget.reserved)

    def test_invariant_holds_across_all_three_sources(self):
        # D47-9: limit == ordinary + reserved, and ordinary >= COMPATIBILITY_FLOOR,
        # on the sealed-block path, the legacy bound-route path, and the floor.
        sealed_block = {
            "contract_version": 1, "declared_nodes": 5, "review_round_cap": 2,
            "gap": 1, "retry": 1, "reserved": 1, "ordinary": 30, "limit": 31,
        }
        with tempfile.TemporaryDirectory() as raw:
            route = Path(raw) / "route.json"
            value = self._sealed_route(sealed_block)
            route.write_text(json.dumps(value), encoding="utf-8")
            sealed = MODULE.resolve_continuation_budget(
                route_file=route, route_id=value["route_id"], route_hash=value["route_hash"],
            )
        self.assertEqual(sealed.limit, sealed.ordinary + sealed.reserved)
        self.assertGreaterEqual(sealed.ordinary, MODULE.COMPATIBILITY_FLOOR)

        with tempfile.TemporaryDirectory() as raw:
            route = Path(raw) / "route.json"
            bound_value = {
                "schema_version": 2,
                "nodes": [{"id": f"node-{index}"} for index in range(3)],
                "resume_retry_boundaries": [],
            }
            import route_identity as RI
            digest = RI.route_hash(bound_value)
            bound_value["route_hash"] = digest
            bound_value["route_id"] = RI.route_id_from_hash(digest)
            route.write_text(json.dumps(bound_value), encoding="utf-8")
            bound = MODULE.resolve_continuation_budget(
                route_file=route, route_id=bound_value["route_id"], route_hash=bound_value["route_hash"],
            )
        self.assertEqual("bound-route", bound.source)
        self.assertEqual(bound.limit, bound.ordinary + bound.reserved)
        self.assertGreaterEqual(bound.ordinary, MODULE.COMPATIBILITY_FLOOR)

        floor = MODULE.resolve_continuation_budget(route_file=None, route_id="", route_hash="")
        self.assertEqual(floor.limit, floor.ordinary + floor.reserved)
        self.assertGreaterEqual(floor.ordinary, MODULE.COMPATIBILITY_FLOOR)

    def test_malformed_sealed_block_falls_through_to_legacy_derivation_not_floor(self):
        # A malformed block (limit != ordinary+reserved) is ignored rather
        # than trusted -- but the route itself is otherwise well-formed, so
        # it still resolves via the legacy nodes+retry-boundaries path.
        malformed = {
            "contract_version": 1, "declared_nodes": 1, "review_round_cap": 2,
            "gap": 1, "retry": 1, "reserved": 1, "ordinary": 5, "limit": 999,
        }
        with tempfile.TemporaryDirectory() as raw:
            route = Path(raw) / "route.json"
            value = self._sealed_route(malformed)
            route.write_text(json.dumps(value), encoding="utf-8")
            budget = MODULE.resolve_continuation_budget(
                route_file=route, route_id=value["route_id"], route_hash=value["route_hash"],
            )
        self.assertEqual("bound-route", budget.source)

    def test_d47_6_floor_path_still_keeps_reserved_remaining_at_least_one(self):
        budget = MODULE.resolve_continuation_budget(route_file=None, route_id="", route_hash="")
        self.assertEqual("compatibility-floor", budget.source)
        ledger = MODULE.ContinuationLedger(budget)
        self.assertGreaterEqual(ledger.reserved_remaining, 1)
        terminal = ledger.admit(purpose="terminal-handoff", stalled=False, reservation_ok=True)
        self.assertTrue(terminal.admitted)

    def test_d47_7_unavailable_budget_state_admits_nothing_new(self):
        budget = MODULE.ContinuationBudget(limit=3, source="test")
        ledger = MODULE.ContinuationLedger(budget)
        ledger._gross_remaining = -1
        verdict = ledger.admit(purpose="ordinary", stalled=False, reservation_ok=True)
        self.assertFalse(verdict.admitted)
        self.assertEqual("continuation-budget-unavailable", verdict.refusal)
        verdict2 = ledger.admit(purpose="ordinary", stalled=False, reservation_ok=False)
        self.assertFalse(verdict2.admitted)
        self.assertEqual("continuation-budget-unavailable", verdict2.refusal)


if __name__ == "__main__":
    unittest.main()
