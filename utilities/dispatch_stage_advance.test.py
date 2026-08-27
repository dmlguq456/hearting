#!/usr/bin/env python3
"""P-tier fixtures for SD-110's shared stage-advance core (plan.md §6)."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_stage_advance as SA  # noqa: E402


def _route_hash(payload):
    import hashlib

    bare = {k: v for k, v in payload.items() if k not in ("route_hash", "route_id")}
    canonical = json.dumps(bare, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def make_route(nodes, *, capability="fixture-cap", capability_mode="dev",
                intensity="standard", advance_generation=0, extra=None):
    payload = {
        "capability": capability,
        "capability_mode": capability_mode,
        "effective_intensity": intensity,
        "nodes": nodes,
        "advance_generation": advance_generation,
    }
    if extra:
        payload.update(extra)
    digest = _route_hash(payload)
    payload["route_hash"] = digest
    payload["route_id"] = "rt-" + digest.split(":", 1)[1][:16]
    return payload


def node(node_id, *, depends_on=(), terminal=False, advance_class="runtime-eligible",
          commit_expected=False, unit="dev/backend", parallel_group=None,
          leg_class=None, model_required_reason=None, completion_gate="gate",
          write_scope=("out/**",), inputs=("in",), outputs=("out",)):
    n = {
        "id": node_id,
        "depends_on": list(depends_on),
        "terminal": terminal,
        "advance_class": advance_class,
        "commit_expected": commit_expected,
        "unit": unit,
        "completion_gate": completion_gate,
        "write_scope": list(write_scope),
        "inputs": list(inputs),
        "outputs": list(outputs),
        "continuation": {"kind": "inline-next"},
    }
    if terminal:
        n["terminal_gate"] = completion_gate
    if parallel_group:
        n["parallel_group"] = parallel_group
    if leg_class:
        n["leg_class"] = leg_class
    if model_required_reason:
        n["model_required_reason"] = model_required_reason
    return n


class Sandbox:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs.registry"
        self.jobs.touch()

    def close(self):
        self.tmp.cleanup()

    def write_route(self, route):
        path = self.root / "route.json"
        path.write_text(json.dumps(route), encoding="utf-8")
        return path

    def add_registry_row(self, metadata: dict, *, status="open"):
        pipe = ",".join(f"{k}={v}" for k, v in metadata.items())
        line = "\t".join(["job", status, "worktree", "worktree", "slug", pipe])
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def make_request(sandbox: Sandbox, route_file: Path, *, predecessor_node="a",
                  predecessor_terminal_attempt_id="att-pred-0001",
                  phase="parked", delivered=frozenset(), schema=3):
    return SA.StageAdvanceRequest(
        jobs=sandbox.jobs,
        route_file=route_file,
        predecessor_node=predecessor_node,
        predecessor_terminal_attempt_id=predecessor_terminal_attempt_id,
        parent_attempt_id="att-parent-0001",
        supervisor_phase=phase,
        delivered_open_attempt_ids=delivered,
        receipt_schema_negotiated=schema,
        harness="claude",
        worktree=str(sandbox.root),
    )


class FakeServices:
    """Deterministic injected services (plan §6: 'tests inject a deterministic
    impl'). Records call counts so A-16 can assert `successor attempt <= 1`."""

    def __init__(self):
        self.close_gate_calls = 0
        self.claim_calls = 0
        self.start_calls = 0

    def close_gate(self, request, *, node, terminal_attempt_id, artifact):
        self.close_gate_calls += 1
        return {"closed": True, "node": node, "artifact": artifact}

    def claim(self, request, *, stage_advance_id, claim_key, successor_node):
        self.claim_calls += 1
        import dispatch_contract as DC

        registry_claim = DC.claim_stage_advance(
            request.jobs,
            stage_advance_id=stage_advance_id,
            route_hash=claim_key[0],
            successor_node=successor_node,
            advance_generation=claim_key[2],
            source_route_id="rt-fixture",
            predecessor_attempt_id=request.predecessor_terminal_attempt_id,
        )
        return SA.StageAdvanceClaim(
            stage_advance_id=registry_claim.stage_advance_id,
            claim_key=registry_claim.claim_key,
            successor_attempt_id=registry_claim.successor_attempt_id,
            replayed=registry_claim.replayed,
        )

    def start_successor(self, request, *, claim, successor, slug, prompt_file):
        self.start_calls += 1
        assert prompt_file.is_file()
        return {"registered": True, "started": True, "child_spawned": True, "slug": slug}

    def observe_record(self, request, *, stage_advance_id):
        return None

    def process_quiescence(self, request, *, attempt_id):
        return True


PASS_EVIDENCE = ("/tmp/fixture-artifact.md", "")


class CensusTest(unittest.TestCase):
    def test_census_reproduces_plan_numbers(self):
        registry = json.loads(
            (ROOT / "capabilities" / "topologies.json").read_text(encoding="utf-8")
        )
        standard = SA.census(registry, "standard")
        self.assertEqual(
            (standard["base"], standard["eligible"], standard["non_terminal"],
             standard["commit_expected_excluded"], standard["runtime_advanced"]),
            (40, 23, 14, 13, 12),
        )
        expected_standard_boundaries = [
            ("autopilot-apply", "apply", "verify"),
            ("autopilot-code", "plan", "plan-check"),
            ("autopilot-code", "plan-check", "execute"),
            ("autopilot-code", "impl-review", "test"),
            ("autopilot-design", "visual-verify", "critic-review"),
            ("autopilot-draft", "strategy-review", "draft-production"),
            ("autopilot-draft", "quality-review", "fact-verify"),
            ("autopilot-lab", "scaffold", "smoke"),
            ("autopilot-lab", "metrics", "media"),
            ("autopilot-lab", "media", "report"),
            ("autopilot-lab", "report", "independent-verify"),
            ("autopilot-research", "synthesis", "report"),
        ]
        actual = [(b["recipe"], b["predecessor"], b["successor"]) for b in standard["boundaries"]]
        self.assertEqual(actual, expected_standard_boundaries)

        strong = SA.census(registry, "strong")
        self.assertEqual(
            (strong["base"], strong["eligible"], strong["non_terminal"],
             strong["commit_expected_excluded"], strong["runtime_advanced"]),
            (40, 9, 3, 3, 3),
        )
        expected_strong_boundaries = [
            ("autopilot-lab", "metrics", "media"),
            ("autopilot-lab", "media", "report"),
            ("autopilot-research", "synthesis", "report"),
        ]
        actual_strong = [(b["recipe"], b["predecessor"], b["successor"]) for b in strong["boundaries"]]
        self.assertEqual(actual_strong, expected_strong_boundaries)


class FanInStaggeredTest(unittest.TestCase):
    """A-2: `a -> {b1, b2} -> c` (synthetic; real fan-in successors are all
    terminal or group in this registry, so this predicate needs its own
    fixture route, per plan §6)."""

    def test_first_leg_blocked_second_leg_advances_once(self):
        route = make_route([
            node("a"),
            node("b1", depends_on=("a",)),
            node("b2", depends_on=("a",)),
            node("c", depends_on=("b1", "b2")),
        ])
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)

            request_b1 = make_request(sandbox, route_file, predecessor_node="a")
            with mock.patch.object(SA, "gate_evidence", return_value=PASS_EVIDENCE):
                services = FakeServices()
                result = SA.coordinate_stage_advance(request_b1, services)
            self.assertEqual(result.outcome, "refused")
            self.assertEqual(result.reason, "stage-advance-successor-ambiguous")
            self.assertEqual(services.start_calls, 0)

            # `a`'s own completion marker was never written by the mocked
            # `close_gate` above; record it now (as the real supervisor would
            # have, via the checked `capability-route.py complete` wrapper)
            # so the second call's `completed` set reflects reality instead of
            # letting `a` reappear as a spurious third runnable node.
            completion_dir = capability_route_completion_dir(sandbox, route["route_id"])
            (completion_dir / "a.json").write_text("{}", encoding="utf-8")
            (completion_dir / "b1.json").write_text("{}", encoding="utf-8")
            sandbox.add_registry_row({"route_id": route["route_id"], "route_node": "b1"})

            request_b2 = make_request(sandbox, route_file, predecessor_node="b2")
            with mock.patch.object(SA, "gate_evidence", return_value=PASS_EVIDENCE):
                services2 = FakeServices()
                result2 = SA.coordinate_stage_advance(request_b2, services2)
            self.assertEqual(result2.outcome, "advanced")
            self.assertEqual(result2.successor_node, "c")
            self.assertEqual(services2.start_calls, 1)
        finally:
            sandbox.close()


def capability_route_completion_dir(sandbox: Sandbox, route_id: str) -> Path:
    directory = SA.ROUTE.completion_dir(route_id, jobs=sandbox.jobs)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class BlockedAmbiguousTest(unittest.TestCase):
    def test_zero_runnable_with_partial_dependency_is_blocked(self):
        # `x` is already started (registry row exists) so it is excluded from
        # `runnable_successors`; `b` depends on both `a` (now completed) and
        # `x` (still open) -- a genuine partial-overlap block, not a fresh
        # independent root that would itself be trivially runnable.
        route = make_route([
            node("a"),
            node("x", depends_on=("a",)),
            node("b", depends_on=("a", "x")),
        ])
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)
            sandbox.add_registry_row({"route_id": route["route_id"], "route_node": "x"})
            request = make_request(sandbox, route_file, predecessor_node="a")
            with mock.patch.object(SA, "gate_evidence", return_value=PASS_EVIDENCE):
                services = FakeServices()
                result = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result.reason, "stage-advance-successor-blocked")
            self.assertEqual(services.start_calls, 0)
        finally:
            sandbox.close()


class ParallelGroupBoundaryTest(unittest.TestCase):
    def test_realized_leg_successor_refuses(self):
        route = make_route([
            node("a"),
            node("b", depends_on=("a",), parallel_group="grp"),
        ])
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)
            request = make_request(sandbox, route_file, predecessor_node="a")
            with mock.patch.object(SA, "gate_evidence", return_value=PASS_EVIDENCE):
                services = FakeServices()
                result = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result.reason, "stage-advance-parallel-group-boundary")
            self.assertEqual(services.start_calls, 0)
        finally:
            sandbox.close()


class ArbiterDeclaredTest(unittest.TestCase):
    def test_auxiliary_leg_successor_refuses(self):
        route = make_route([
            node("a"),
            node("b", depends_on=("a",), leg_class="auxiliary"),
        ])
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)
            request = make_request(sandbox, route_file, predecessor_node="a")
            with mock.patch.object(SA, "gate_evidence", return_value=PASS_EVIDENCE):
                services = FakeServices()
                result = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result.reason, "stage-advance-arbiter-declared")
        finally:
            sandbox.close()

    def test_ordinary_fan_in_is_not_arbiter_declared(self):
        # An ordinary multi-dependency join that is NOT an auxiliary group's
        # arbiter is deliberately left to `runnable_successors`'s plain
        # `depends_on <= completed` test (see `FanInStaggeredTest`) rather
        # than being blanket-refused here just for having two dependencies.
        route = make_route([
            node("a"),
            node("x"),
            node("b", depends_on=("a", "x")),
        ])
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)
            completion_dir = capability_route_completion_dir(sandbox, route["route_id"])
            (completion_dir / "x.json").write_text("{}", encoding="utf-8")
            request = make_request(sandbox, route_file, predecessor_node="a")
            with mock.patch.object(SA, "gate_evidence", return_value=PASS_EVIDENCE):
                services = FakeServices()
                result = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result.outcome, "advanced")
            self.assertEqual(result.successor_node, "b")
        finally:
            sandbox.close()


class TerminalNodeTest(unittest.TestCase):
    def test_terminal_successor_refuses_before_tuple_unsealed(self):
        # A-8: successor is BOTH terminal AND model-required/tuple-unsealed by
        # the sealing rule (terminal implies model-required); assert
        # terminal-node wins, proving the recorded priority deviation.
        route = make_route([
            node("a"),
            node("b", depends_on=("a",), terminal=True,
                 advance_class="model-required", model_required_reason="terminal-report"),
        ])
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)
            request = make_request(sandbox, route_file, predecessor_node="a")
            with mock.patch.object(SA, "gate_evidence", return_value=PASS_EVIDENCE):
                services = FakeServices()
                result = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result.reason, "stage-advance-terminal-node")
            self.assertEqual(services.close_gate_calls, 0)
            self.assertFalse(result.gate_closed)
        finally:
            sandbox.close()


class CommitExpectedTest(unittest.TestCase):
    def test_commit_expected_predecessor_closes_gate_but_does_not_start(self):
        route = make_route([
            node("a", commit_expected=True),
            node("b", depends_on=("a",)),
        ])
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)
            request = make_request(sandbox, route_file, predecessor_node="a")
            with mock.patch.object(SA, "gate_evidence", return_value=PASS_EVIDENCE):
                services = FakeServices()
                result = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result.reason, "stage-advance-commit-expected")
            self.assertTrue(result.gate_closed)
            self.assertFalse(result.started)
            self.assertEqual(services.close_gate_calls, 1)
            self.assertEqual(services.start_calls, 0)
        finally:
            sandbox.close()


class TupleUnsealedTest(unittest.TestCase):
    def test_each_model_required_reason_refuses_without_start(self):
        reasons = (
            "authored-brief", "arbiter-or-merge", "conditional-extension",
            "terminal-report", "commit-boundary", "parallel-group-member",
            "operator-pinned",
        )
        for reason in reasons:
            with self.subTest(reason=reason):
                route = make_route([
                    node("a"),
                    node("b", depends_on=("a",), advance_class="model-required",
                         model_required_reason=reason),
                ])
                sandbox = Sandbox()
                try:
                    route_file = sandbox.write_route(route)
                    request = make_request(sandbox, route_file, predecessor_node="a")
                    with mock.patch.object(SA, "gate_evidence", return_value=PASS_EVIDENCE):
                        services = FakeServices()
                        result = SA.coordinate_stage_advance(request, services)
                    self.assertEqual(result.reason, "stage-advance-tuple-unsealed")
                    self.assertEqual(services.start_calls, 0)
                finally:
                    sandbox.close()


class TamperedRouteHashTest(unittest.TestCase):
    def test_mutated_route_hash_typed_refuses(self):
        route = make_route([node("a"), node("b", depends_on=("a",))])
        route["nodes"][0]["id"] = "tampered"  # mutate payload without re-hashing
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)
            request = make_request(sandbox, route_file, predecessor_node="a")
            services = FakeServices()
            result = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result.reason, "stage-advance-launch-compatibility-mismatch")
            self.assertFalse(result.gate_closed)
            self.assertEqual(result.registered, False)
            self.assertEqual(result.started, False)
            self.assertEqual(result.child_spawned, False)
        finally:
            sandbox.close()


class EvidenceUnreadableTest(unittest.TestCase):
    def test_missing_artifact_refuses_before_gate_close(self):
        route = make_route([node("a"), node("b", depends_on=("a",))])
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)
            request = make_request(sandbox, route_file, predecessor_node="a")
            with mock.patch.object(SA, "gate_evidence", return_value=(None, "evidence-not-readable")):
                services = FakeServices()
                result = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result.reason, "stage-advance-evidence-unreadable")
            self.assertFalse(result.gate_closed)
            self.assertEqual(services.close_gate_calls, 0)
        finally:
            sandbox.close()

    def test_unproven_gate_refuses(self):
        route = make_route([node("a"), node("b", depends_on=("a",))])
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)
            request = make_request(sandbox, route_file, predecessor_node="a")
            with mock.patch.object(SA, "gate_evidence", return_value=(None, "evidence-not-valid")):
                services = FakeServices()
                result = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result.reason, "stage-advance-gate-unproven")
            self.assertFalse(result.gate_closed)
        finally:
            sandbox.close()


class PhaseIneligibleTest(unittest.TestCase):
    def test_delivered_open_children_refuse(self):
        route = make_route([node("a"), node("b", depends_on=("a",))])
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)
            request = make_request(
                sandbox, route_file, predecessor_node="a", delivered=frozenset({"att-open"})
            )
            services = FakeServices()
            result = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result.reason, "stage-advance-phase-ineligible")
        finally:
            sandbox.close()

    def test_running_turn_phase_refuses(self):
        route = make_route([node("a"), node("b", depends_on=("a",))])
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)
            request = make_request(sandbox, route_file, predecessor_node="a", phase="unknown-phase")
            services = FakeServices()
            result = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result.reason, "stage-advance-phase-ineligible")
        finally:
            sandbox.close()

    def test_unsupported_receipt_schema_refuses(self):
        route = make_route([node("a"), node("b", depends_on=("a",))])
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)
            request = make_request(sandbox, route_file, predecessor_node="a", schema=1)
            services = FakeServices()
            result = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result.reason, "stage-advance-receipt-schema-unsupported")
        finally:
            sandbox.close()

    def test_unnegotiated_v2_consumer_refuses_before_any_side_effect(self):
        """Block 4's flag-default: a caller that has not negotiated receipt
        v3 (schema=2, the ordinary un-negotiated value -- see
        `dispatch_completion_join.StageAdvanceReceiptNegotiationTest`) never
        reaches gate close, claim, or start. `2` is a legal schema VALUE but
        not a v3 negotiation, so it refuses exactly like an invalid one."""

        route = make_route([node("a"), node("b", depends_on=("a",))])
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)
            request = make_request(sandbox, route_file, predecessor_node="a", schema=2)
            with mock.patch.object(SA, "gate_evidence", return_value=PASS_EVIDENCE):
                services = FakeServices()
                result = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result.outcome, "refused")
            self.assertEqual(result.reason, "stage-advance-receipt-schema-unsupported")
            self.assertFalse(result.gate_closed)
            self.assertEqual(services.close_gate_calls, 0)
            self.assertEqual(services.claim_calls, 0)
            self.assertEqual(services.start_calls, 0)
        finally:
            sandbox.close()


class EligibleLinearAdvanceTest(unittest.TestCase):
    def test_marker_one_successor_attempt_one_record_one(self):
        route = make_route([node("a"), node("b", depends_on=("a",))])
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)
            request = make_request(sandbox, route_file, predecessor_node="a")
            with mock.patch.object(SA, "gate_evidence", return_value=PASS_EVIDENCE):
                services = FakeServices()
                result = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result.outcome, "advanced")
            self.assertEqual(result.successor_node, "b")
            self.assertTrue(result.gate_closed)
            self.assertTrue(result.registered)
            self.assertTrue(result.started)
            self.assertTrue(result.child_spawned)
            self.assertEqual(services.close_gate_calls, 1)
            self.assertEqual(services.claim_calls, 1)
            self.assertEqual(services.start_calls, 1)
            self.assertTrue(result.record_path.is_file())

            # Replay: same stage_advance_id, no second start.
            with mock.patch.object(SA, "gate_evidence", return_value=PASS_EVIDENCE):
                result2 = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result2.stage_advance_id, result.stage_advance_id)
            self.assertEqual(services.start_calls, 1)
        finally:
            sandbox.close()


class SuccessorStartFailedTest(unittest.TestCase):
    """A-13's `stage-advance-successor-start-failed` leg (§13.32.1-(3)D):
    gate close succeeded, `services.start_successor` raised a typed
    `StageAdvanceError` (governor denial, launch fence rejection, etc) --
    the transaction refuses cleanly instead of propagating the exception,
    marker 1 / start 0, and replay returns the identical refusal without a
    second start attempt."""

    class _FailingStartServices(FakeServices):
        def start_successor(self, request, *, claim, successor, slug, prompt_file):
            self.start_calls += 1
            raise SA.StageAdvanceError(
                "stage-advance-successor-start-failed", "governor-denied"
            )

    def test_start_failure_is_typed_refusal_not_a_crash(self):
        route = make_route([node("a"), node("b", depends_on=("a",))])
        sandbox = Sandbox()
        try:
            route_file = sandbox.write_route(route)
            request = make_request(sandbox, route_file, predecessor_node="a")
            services = self._FailingStartServices()
            with mock.patch.object(SA, "gate_evidence", return_value=PASS_EVIDENCE):
                result = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result.outcome, "refused")
            self.assertEqual(result.reason, "stage-advance-successor-start-failed")
            self.assertTrue(result.gate_closed)
            self.assertTrue(result.registered)
            self.assertFalse(result.started)
            self.assertFalse(result.child_spawned)
            self.assertEqual(services.close_gate_calls, 1)
            self.assertEqual(services.claim_calls, 1)
            self.assertEqual(services.start_calls, 1)

            # Replay: identical refusal, no second start attempt, no second
            # gate close.
            with mock.patch.object(SA, "gate_evidence", return_value=PASS_EVIDENCE):
                result2 = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result2.outcome, "refused")
            self.assertEqual(result2.reason, "stage-advance-successor-start-failed")
            self.assertEqual(result2.stage_advance_id, result.stage_advance_id)
            self.assertEqual(services.start_calls, 1)
            self.assertEqual(services.close_gate_calls, 1)
        finally:
            sandbox.close()


class ClaimConflictTest(unittest.TestCase):
    def test_second_predecessor_same_claim_key_conflicts(self):
        import dispatch_contract as DC

        sandbox = Sandbox()
        try:
            DC.claim_stage_advance(
                sandbox.jobs,
                stage_advance_id="sadv-" + "0" * 64,
                route_hash="sha256:" + "a" * 64,
                successor_node="b",
                advance_generation=0,
                source_route_id="rt-fixture",
                predecessor_attempt_id="att-first",
            )
            with self.assertRaises(DC.DispatchContractError) as ctx:
                DC.claim_stage_advance(
                    sandbox.jobs,
                    stage_advance_id="sadv-" + "1" * 64,
                    route_hash="sha256:" + "a" * 64,
                    successor_node="b",
                    advance_generation=0,
                    source_route_id="rt-fixture",
                    predecessor_attempt_id="att-second",
                )
            self.assertEqual(ctx.exception.reason, "stage-advance-claim-conflict")
        finally:
            sandbox.close()

    def test_same_stage_advance_id_replays(self):
        import dispatch_contract as DC

        sandbox = Sandbox()
        try:
            first = DC.claim_stage_advance(
                sandbox.jobs,
                stage_advance_id="sadv-" + "2" * 64,
                route_hash="sha256:" + "b" * 64,
                successor_node="b",
                advance_generation=0,
                source_route_id="rt-fixture",
                predecessor_attempt_id="att-first",
            )
            second = DC.claim_stage_advance(
                sandbox.jobs,
                stage_advance_id="sadv-" + "2" * 64,
                route_hash="sha256:" + "b" * 64,
                successor_node="b",
                advance_generation=0,
                source_route_id="rt-fixture",
                predecessor_attempt_id="att-first",
            )
            self.assertTrue(second.replayed)
            self.assertEqual(first.successor_attempt_id, second.successor_attempt_id)
        finally:
            sandbox.close()


class NoGitMutationTest(unittest.TestCase):
    def test_no_git_invocation_in_core_module(self):
        source = (ROOT / "utilities" / "dispatch_stage_advance.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn('"git"', line)
            self.assertNotIn("'git'", line)


if __name__ == "__main__":
    unittest.main()
