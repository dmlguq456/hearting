import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dispatch_terminal_commit as T
import owner_route_binding

# The one already-loaded handle on capability-route.py; the quick-branch tests
# below compile a real quick route rather than hand-rolling one, so the route
# they verify against is the route the compiler actually emits.
ROUTE = owner_route_binding.ROUTE


def seal_fixture_route(route, route_file, root, jobs, owner):
    route["artifact_root"] = str(root)
    route["route_hash"] = T.route_identity.route_hash(route)
    route["route_id"] = T.route_identity.route_id_from_hash(route["route_hash"])
    route_file.write_text(json.dumps(route))
    jobs.write_text(f"2026-09-07T00:00:00Z\topen\t{root}\t{root}\towner\t"
        f"attempt_id={owner},worker_type=owner,dispatch_depth=1,registered_worker=1,harness=claude,"
        f"owner_route_id={route['route_id']},owner_route_hash={route['route_hash']},owner_route_file={route_file}\n")


class ProducerBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.route_file = self.root / "route.json"
        self.route_file.write_text(json.dumps({"route_id": "rt-abcdef12", "route_hash": "sha256:" + "a" * 64}), encoding="utf-8")
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        cycle_dir = self.root / ".runtime/artifact-producer/v1/cycles"
        cycle_dir.mkdir(parents=True)
        self.cycle_id = "cyc_" + "a" * 32
        (cycle_dir / (self.cycle_id + ".json")).write_bytes(json.dumps({
            "route_id": "rt-abcdef12", "route_hash": "sha256:" + "a" * 64,
            "cycle_id": self.cycle_id, "state": "open", "campaign_id": "camp_" + "b" * 32,
            "producer_id": "prod_" + "c" * 32,
        }, sort_keys=True, separators=(",", ":")).encode())

    def tearDown(self):
        self.tmp.cleanup()

    @mock.patch.object(T.artifact_lifecycle, "read_root_identity", return_value=SimpleNamespace(repository_id="repo_x", artifact_root_id="root_y"))
    @mock.patch.object(owner_route_binding, "resolve_owner_route_lifecycle")
    def test_publish_replay_and_conflict(self, resolve, _identity):
        resolve.return_value = (owner_route_binding.OwnerRouteBinding(str(self.route_file), "rt-abcdef12", "sha256:" + "a" * 64), "current")
        first = T.publish_producer_binding(artifact_root=self.root, jobs=self.jobs, route_file=self.route_file,
                                           owner_attempt_id="att-owner", cycle_id=self.cycle_id, owner_begin=True)
        second = T.publish_producer_binding(artifact_root=self.root, jobs=self.jobs, route_file=self.route_file,
                                            owner_attempt_id="att-owner", cycle_id=self.cycle_id, owner_begin=True)
        self.assertEqual(first.digest, second.digest)
        self.assertTrue(second.replay)
        with self.assertRaises(T.TerminalCommitError) as caught:
            T.publish_producer_binding(artifact_root=self.root, jobs=self.jobs, route_file=self.route_file,
                                       owner_attempt_id="att-owner", cycle_id="cyc_" + "d" * 32, owner_begin=True)
        self.assertEqual(caught.exception.code, "producer-binding-mismatch")

    def test_path_is_single_safe_derivation_and_digest_is_bytes(self):
        path = T.producer_binding_path(self.root, "rt-abcdef12", "att-owner")
        self.assertEqual(path, self.root / ".runtime/terminal-commits/v1/rt-abcdef12/att-owner/producer-binding.json")
        with self.assertRaises(T.TerminalCommitError):
            T.producer_binding_path(self.root, "../route", "att-owner")

    def test_terminal_reasons_are_the_prd_closed_set(self):
        self.assertEqual(T.TERMINAL_REASONS, {
            "route-identity-unverified", "terminal-marker-not-current", "child-not-quiescent",
            "producer-binding-required", "producer-binding-mismatch", "route-close-failed",
            "producer-finalize-failed", "transaction-conflict", "recovery-unavailable",
        })

    def test_detail_rejection_axes_map_to_canonical_reason_without_mutation(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        expected = {
            "owner-route-mismatch": "route-identity-unverified",
            "terminal-attempt-not-pass": "terminal-marker-not-current",
            "child-not-terminal": "child-not-quiescent",
            "active-retry": "child-not-quiescent",
            "active-review-lease": "producer-finalize-failed",
            "binding-cycle-not-open": "producer-binding-mismatch",
        }
        for detail, reason in expected.items():
            proof = T._proof_failure(detail)
            self.assertEqual(proof.reason, reason)
            self.assertEqual(proof.detail, detail)
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")))

    def test_non_producer_absent_binding_is_not_applicable(self):
        route = {"route_id": "rt-abcdef12", "route_hash": "sha256:" + "a" * 64,
                 "capability": "fixture-cap", "capability_mode": "default", "nodes": []}
        seal_fixture_route(route, self.route_file, self.root, self.jobs, "att-owner")
        owner = owner_route_binding.OwnerRouteBinding(str(self.route_file), route["route_id"], route["route_hash"])
        with mock.patch.object(T, "validate_owner_route", return_value=owner), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = {"route": {"passed": True}}
            proof = T.prove_terminal_authority(T.TerminalCommitRequest(
                self.route_file, "att-owner", self.jobs, self.root))
        self.assertEqual((proof.status, proof.reason, proof.detail),
                         ("proved", None, "producer-binding:not-applicable"))

    def test_producer_missing_binding_remains_required(self):
        route = {"route_id": "rt-abcdef12", "route_hash": "sha256:" + "a" * 64,
                 "capability": "autopilot-code", "capability_mode": "dev", "nodes": []}
        seal_fixture_route(route, self.route_file, self.root, self.jobs, "att-owner")
        owner = owner_route_binding.OwnerRouteBinding(str(self.route_file), route["route_id"], route["route_hash"])
        with mock.patch.object(T, "validate_owner_route", return_value=owner), \
             mock.patch.object(T, "_route_module") as route_module, \
             mock.patch.object(T, "load_producer_binding",
                               side_effect=T.TerminalCommitError("producer-binding-required")):
            route_module.return_value.terminal_gate_observation.return_value = {"route": {"passed": True}}
            proof = T.prove_terminal_authority(T.TerminalCommitRequest(
                self.route_file, "att-owner", self.jobs, self.root))
        self.assertEqual((proof.status, proof.reason), ("rejected", "producer-binding-required"))

    def test_producer_present_binding_proves_and_foreign_binding_is_rejected_without_mutation(self):
        route = {"route_id": "rt-abcdef12", "route_hash": "sha256:" + "a" * 64,
                 "capability": "autopilot-code", "capability_mode": "dev", "nodes": []}
        seal_fixture_route(route, self.route_file, self.root, self.jobs, "att-owner")
        owner = owner_route_binding.OwnerRouteBinding(str(self.route_file), route["route_id"], route["route_hash"])
        cycle_id = "cyc_" + "d" * 32
        cycle_path = self.root / ".runtime/artifact-producer/v1/cycles" / f"{cycle_id}.json"
        cycle_path.parent.mkdir(parents=True, exist_ok=True)
        cycle_path.write_text(json.dumps({"state": "open"}), encoding="utf-8")
        binding = SimpleNamespace(binding={"route_hash": route["route_hash"], "cycle_id": cycle_id,
            "cycle_record_digest": T.cycle_identity_digest({"state":"open"})})
        with mock.patch.object(T, "validate_owner_route", return_value=owner), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=True), \
             mock.patch.object(T, "_route_module") as route_module, \
             mock.patch.object(T, "load_producer_binding", return_value=binding), \
             mock.patch("artifact_producer._live_review_lease", return_value=None):
            route_module.return_value.terminal_gate_observation.return_value = {"route": {"passed": True}}
            proof = T.prove_terminal_authority(T.TerminalCommitRequest(
                self.route_file, "att-owner", self.jobs, self.root))
        self.assertEqual((proof.status, proof.reason), ("proved", None))
        before = sorted((str(path.relative_to(self.root)), path.read_bytes())
                        for path in self.root.rglob("*") if path.is_file())
        foreign = SimpleNamespace(binding={"route_hash": "sha256:" + "f" * 64, "cycle_id": cycle_id})
        with mock.patch.object(T, "validate_owner_route", return_value=owner), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=True), \
             mock.patch.object(T, "_route_module") as route_module, \
             mock.patch.object(T, "load_producer_binding", return_value=foreign):
            route_module.return_value.terminal_gate_observation.return_value = {"route": {"passed": True}}
            rejected = T.prove_terminal_authority(T.TerminalCommitRequest(
                self.route_file, "att-owner", self.jobs, self.root))
        self.assertEqual((rejected.status, rejected.reason), ("rejected", "producer-binding-mismatch"))
        after = sorted((str(path.relative_to(self.root)), path.read_bytes())
                       for path in self.root.rglob("*") if path.is_file())
        self.assertEqual(before, after)


class _TerminalCommitFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.route_file = self.root / "route.json"
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.artifact = self.root / "summary.md"
        self.artifact.write_text("verified\n", encoding="utf-8")
        self.route = {
            "route_id": "rt-a3fixture",
            "route_hash": "sha256:" + "a" * 64,
            "nodes": [{"id": "execute", "terminal": True}],
            "workflow_contract": {"terminal_nodes": ["execute"]},
            "capability": "fixture-cap",
            "capability_mode": "default",
        }
        seal_fixture_route(self.route, self.route_file, self.root, self.jobs, "att-a3fixture")
        self.gates = {"execute": {"passed": True, "evidence": str(self.artifact),
            "node_id": "execute", "attempt_id": "att-execute", "completion_gate": "code-execute",
            "marker_digest": "a" * 64, "evidence_digest": "b" * 64}}

    def tearDown(self):
        self.tmp.cleanup()

    def request(self):
        return T.TerminalCommitRequest(self.route_file, "att-a3fixture", self.jobs, self.root)

    def patch_settle(self, *, producer=False):
        proof = T.TerminalProof("proved")
        topology = mock.patch.object(T, "producer_lifecycle_applies", return_value=producer)
        authority = mock.patch.object(T, "prove_terminal_authority", return_value=proof)
        route_mod = mock.patch.object(T, "_route_module")
        route_mod.return_value.terminal_gate_observation.return_value = self.gates
        return mock.patch.multiple(T, _route_module=route_mod.return_value), topology, authority

    def services(self, calls=None, finalize=None):
        calls = calls if calls is not None else []
        def close(*args, **kwargs):
            calls.append("close")
        def finish(*args, **kwargs):
            calls.append("finalize")
            if finalize is not None:
                return finalize()
        def seal(**kwargs):
            calls.append("envelope")
            return T._default_seal_envelope(**kwargs)
        return T.TerminalCommitServices(close_route=close, finalize_exact_cycle=finish,
                                        seal_envelope=seal), calls


class TerminalCommitHappyPathTest(_TerminalCommitFixture):
    def test_canonical_terminal_route_closes_finalizes_and_seals_without_owner_turn(self):
        calls = []
        services, calls = self.services(calls)
        with mock.patch.object(T, "prove_terminal_authority", return_value=T.TerminalProof("proved")), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=False), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = self.gates
            result = T.settle_terminal_commit(self.request(), services)
        self.assertEqual(result.result, "completed")
        self.assertEqual(calls, ["close", "envelope"])
        state = json.loads((T._commit_state_path(self.request())).read_text(encoding="utf-8"))
        self.assertEqual(state["state"], "owner-envelope-sealed")
        self.assertEqual(len(list((T._commit_state_path(self.request()).parent).glob("owner-envelope.*"))), 2)


class TerminalReasonVocabularyTest(_TerminalCommitFixture):
    def test_terminal_reasons_is_exactly_the_nine_prd_values(self):
        self.assertEqual(T.TERMINAL_REASONS, frozenset({
            "route-identity-unverified", "terminal-marker-not-current", "child-not-quiescent",
            "producer-binding-required", "producer-binding-mismatch", "route-close-failed",
            "producer-finalize-failed", "transaction-conflict", "recovery-unavailable",
        }))


class MaterialEnvelopeTest(_TerminalCommitFixture):
    def test_primary_selection_rules_and_dash_never_passes(self):
        binding = {"primary": str(self.artifact)}
        self.assertEqual(T.select_primary_artifact(self.route, self.gates, binding, artifact_root=self.root), self.artifact)
        self.assertEqual(T.select_primary_artifact(self.route, self.gates, {}, artifact_root=self.root), self.artifact)
        empty = self.root / "empty"
        empty.touch()
        invalid_route = dict(self.route, workflow_contract={"terminal_nodes": ["missing"]})
        for candidate in (empty, self.root / "missing"):
            self.assertIsNone(T.select_primary_artifact(invalid_route, {},
                                                        {"primary": str(candidate)}, artifact_root=self.root))
        route_without_evidence = dict(self.route, workflow_contract={"terminal_nodes": ["missing"]})
        self.assertIsNone(T.select_primary_artifact(route_without_evidence, {}, {}, artifact_root=self.root))
        self.assertIsNone(T.select_primary_artifact(route_without_evidence, {}, {}, artifact_root=self.root))


class CrashReplayMatrixTest(_TerminalCommitFixture):
    def test_each_named_crash_checkpoint_replays_same_transaction_at_most_once(self):
        for checkpoint in ("claim-after", "close-after"):
            with self.subTest(checkpoint=checkpoint):
                self.setUp()
                calls = []
                services, calls = self.services(calls)
                services = T.TerminalCommitServices(close_route=services.close_route,
                                                    finalize_exact_cycle=services.finalize_exact_cycle,
                                                    seal_envelope=services.seal_envelope,
                                                    crash_after=checkpoint)
                owner = owner_route_binding.OwnerRouteBinding(
                    str(self.route_file), self.route["route_id"], self.route["route_hash"])
                with mock.patch.object(T, "prove_terminal_authority", return_value=T.TerminalProof("proved")), \
                     mock.patch.object(T, "validate_owner_route", return_value=owner), \
                     mock.patch.object(T, "producer_lifecycle_applies", return_value=False), \
                     mock.patch.object(T, "_route_module") as route_module:
                    route_module.return_value.terminal_gate_observation.return_value = self.gates
                    first = T.settle_terminal_commit(self.request(), services)
                    second = T.settle_terminal_commit(self.request(), T.TerminalCommitServices(
                        close_route=services.close_route, finalize_exact_cycle=services.finalize_exact_cycle,
                        seal_envelope=services.seal_envelope))
                self.assertEqual(first.result, "recoverable")
                self.assertEqual(second.result, "completed")
                self.assertLessEqual(calls.count("close"), 1)
                self.assertLessEqual(calls.count("envelope"), 1)
                self.assertEqual(json.loads(T._commit_state_path(self.request()).read_text())["state"],
                                 "owner-envelope-sealed")
                self.tearDown()


class ForwardRecoveryTest(_TerminalCommitFixture):
    def test_non_producer_replay_never_calls_finalize(self):
        calls = []
        failures = [True]
        def flaky_finalize():
            if failures.pop(0):
                raise T.TerminalCommitError("producer-finalize-failed", "fixture")
        binding = T.ProducerBindingResult("loaded", self.root / "binding", {
            "cycle_id": "cyc-a3fixture", "route_hash": self.route["route_hash"]}, "sha256:" + "b" * 64)
        services, calls = self.services(calls, flaky_finalize)
        owner = owner_route_binding.OwnerRouteBinding(
            str(self.route_file), self.route["route_id"], self.route["route_hash"])
        with mock.patch.object(T, "prove_terminal_authority", return_value=T.TerminalProof("proved")), \
             mock.patch.object(T, "validate_owner_route", return_value=owner), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=False), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = self.gates
            first = T.settle_terminal_commit(self.request(), services)
            state = json.loads(T._commit_state_path(self.request()).read_text())
            second = T.settle_terminal_commit(self.request(), services)
        self.assertEqual(first.result, "completed")
        self.assertEqual(state["state"], "owner-envelope-sealed")
        self.assertEqual(second.result, "completed")
        self.assertEqual(calls.count("close"), 1)
        self.assertEqual(calls.count("finalize"), 0)


class ForwardRecoveryIdentityMismatchTest(_TerminalCommitFixture):
    """A82-7/§13.53.5: forward recovery re-entry recomputes the exact
    terminal identity instead of trusting a stored state string. If the
    current route/marker/binding no longer produces the same
    `terminal_commit_id` the durable record holds, that is
    `transaction-conflict` with zero mutation -- never a silent PASS."""

    def test_marker_set_drift_after_claim_is_a_transaction_conflict_not_a_silent_pass(self):
        owner = owner_route_binding.OwnerRouteBinding(
            str(self.route_file), self.route["route_id"], self.route["route_hash"])
        with mock.patch.object(T, "prove_terminal_authority", return_value=T.TerminalProof("proved")), \
             mock.patch.object(T, "validate_owner_route", return_value=owner), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=False), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = self.gates
            services = T.TerminalCommitServices(
                close_route=lambda *a, **k: None,
                finalize_exact_cycle=lambda *a, **k: None,
                seal_envelope=lambda **k: None,
                crash_after="close-after",
            )
            first = T.settle_terminal_commit(self.request(), services)
            self.assertEqual(first.result, "recoverable")
            path = T._commit_state_path(self.request())
            self.assertEqual(json.loads(path.read_text())["state"], "route-closed")
            before = path.read_text(encoding="utf-8")
            # The durable claim now reflects one marker set (recorded at
            # "route-closed"). Simulate drift on the forward-recovery
            # re-entry, which no longer goes through `prove_terminal_authority`
            # (mocked "proved" above) but through `_reverify_forward_recovery`:
            # a different set of terminal gates observed on re-entry (e.g. a
            # different attempt clobbered the marker between claim and
            # retry) must not be waved through as the same commit.
            route_module.return_value.terminal_gate_observation.return_value = {
                "execute": dict(self.gates["execute"], attempt_id="att-drifted")
            }
            second = T.settle_terminal_commit(self.request(), T.TerminalCommitServices(
                close_route=lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("close must not run on identity mismatch")),
                finalize_exact_cycle=lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("finalize must not run on identity mismatch")),
                seal_envelope=lambda **k: (_ for _ in ()).throw(
                    AssertionError("seal must not run on identity mismatch")),
            ))
        self.assertEqual(second.result, "recoverable")
        self.assertEqual(second.reason, "transaction-conflict")
        after = path.read_text(encoding="utf-8")
        self.assertEqual(before, after)


class EnvelopeReplayReverificationTest(_TerminalCommitFixture):
    """A82-10/§13.53.6: replaying an `owner-envelope-sealed` state must
    re-verify the envelope and its sealed primary artifact, not just observe
    that `owner-envelope.txt` exists on disk."""

    def _seal_once(self):
        owner = owner_route_binding.OwnerRouteBinding(
            str(self.route_file), self.route["route_id"], self.route["route_hash"])
        with mock.patch.object(T, "prove_terminal_authority", return_value=T.TerminalProof("proved")), \
             mock.patch.object(T, "validate_owner_route", return_value=owner), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=False), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = self.gates
            services = T.TerminalCommitServices(
                close_route=lambda *a, **k: None,
                finalize_exact_cycle=lambda *a, **k: None,
                seal_envelope=T._default_seal_envelope,
            )
            first = T.settle_terminal_commit(self.request(), services)
        self.assertEqual(first.result, "completed")
        return owner

    def test_unchanged_primary_replays_completed(self):
        owner = self._seal_once()
        with mock.patch.object(T, "prove_terminal_authority", return_value=T.TerminalProof("proved")), \
             mock.patch.object(T, "validate_owner_route", return_value=owner), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=False), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = self.gates
            second = T.settle_terminal_commit(self.request(), T.TerminalCommitServices())
        self.assertEqual(second.result, "completed")

    def test_primary_content_drift_after_seal_is_transaction_conflict_not_replay(self):
        owner = self._seal_once()
        # Mutate the sealed primary artifact's bytes after sealing.
        self.artifact.write_text("tampered after seal\n", encoding="utf-8")
        with mock.patch.object(T, "prove_terminal_authority", return_value=T.TerminalProof("proved")), \
             mock.patch.object(T, "validate_owner_route", return_value=owner), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=False), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = self.gates
            second = T.settle_terminal_commit(self.request(), T.TerminalCommitServices())
        self.assertEqual(second.result, "recoverable")
        self.assertEqual(second.reason, "transaction-conflict")
        self.assertEqual(second.detail, "primary-content-drifted-after-seal")


class CleanupScopeDurabilityTest(_TerminalCommitFixture):
    def intent(self, state_root):
        import dispatch_budget_record as budget
        claim = budget.claim_terminal_handoff(state_root, owner_attempt_id="att-owner",
                                               route_hash="hash", child_attempt_ids=[], continuation_ordinal=10)
        return budget.convert_claim_to_prompt_intent(state_root, claim, prompt="cleanup",
                    cleanup_scope={"route_id": "route", "owner_attempt_id": "att-owner"})

    def test_missing_scope_sidecar_after_crash_still_restricts_owner(self):
        state_root = self.root / "state"
        self.intent(state_root)
        (state_root / "terminal-handoffs/v1/att-owner/10/cleanup-scope.json").unlink()
        scope = T.load_active_cleanup_scope(state_root, "att-owner")
        self.assertEqual(scope.route_id, "route")
        self.assertEqual(T.cleanup_tool_permission(scope, tool="Bash", arguments={"command":"git status; touch /tmp/escape"},
            cwd=self.root, owner_attempt_id="att-owner", route_id="route").verdict, "denied-operation")

    def test_scope_identity_tamper_is_not_unrestricted_absence(self):
        state_root = self.root / "state"
        self.intent(state_root)
        path = state_root / "terminal-handoffs/v1/att-owner/10/prompt-intent.json"
        value = json.loads(path.read_text())
        value["cleanup_scope"]["allowed_write_roots"] = ["/"]
        path.write_text(json.dumps(value))
        with self.assertRaises(T.TerminalCommitError):
            T.load_active_cleanup_scope(state_root, "att-owner")

    def test_cleanup_scope_rejects_current_context_identity_drift(self):
        scope = T.CleanupScope(
            artifact_root=self.root,
            route_id="route-1",
            owner_attempt_id="att-owner",
            route_hash="sha256:route",
            terminal_commit_id="commit-1",
            claim_id="claim-1",
            intent_id="intent-1",
            allowed_operations=("read",),
            allowed_read_roots=(self.root,),
        )
        denied = T.authorize_cleanup_operation(
            scope, operation="read", target=self.root / "evidence.md",
            route_id="route-1", cycle_id=None,
            owner_attempt_id="att-owner", route_hash="sha256:other",
            terminal_commit_id="commit-1", claim_id="claim-1", intent_id="intent-1",
        )
        self.assertEqual(denied.verdict, "denied-identity")
        allowed = T.authorize_cleanup_operation(
            scope, operation="read", target=self.root / "evidence.md",
            route_id="route-1", cycle_id=None,
            owner_attempt_id="att-owner", route_hash="sha256:route",
            terminal_commit_id="commit-1", claim_id="claim-1", intent_id="intent-1",
        )
        self.assertEqual(allowed.verdict, "allowed")


class ProducerBindingMatrixTest(_TerminalCommitFixture):
    def test_required_not_applicable_foreign_and_stale_bindings_fail_closed(self):
        request = self.request()
        with mock.patch.object(T, "validate_owner_route", side_effect=T.TerminalCommitError("producer-binding-required")):
            self.assertEqual(T.prove_terminal_authority(request).reason, "producer-binding-required")
        self.assertEqual(T._proof_failure("owner-route-mismatch").reason, "route-identity-unverified")
        self.assertEqual(T._proof_failure("binding-cycle-not-open").reason, "producer-binding-mismatch")
        self.assertEqual(T._proof_failure("producer-binding-mismatch").reason, "producer-binding-mismatch")


class QuickWorkerTypeAxesTest(unittest.TestCase):
    """`validate_owner_route`'s quick branch admits `frame` as well as `owner`.

    Quick is a three-node route and both worker types terminate through here.
    Assuming `one-shot` made every frame leg's termination fail as
    `route-identity-unverified: quick-owner-tuple` -- the row named `frame`,
    the derived tuple named `one-shot`, and the two could never agree.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "core").mkdir(parents=True, exist_ok=True)
        (self.base / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        self._previous = {key: os.environ.get(key)
                          for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS", "XDG_STATE_HOME")}
        os.environ["AGENT_HOME"] = str(self.base)
        os.environ["XDG_STATE_HOME"] = str(self.base / "state")
        state_jobs = self.base / "state" / "jobs.log"
        state_jobs.parent.mkdir(parents=True, exist_ok=True)
        state_jobs.write_text("", encoding="utf-8")
        os.environ["AGENT_DISPATCH_JOBS"] = str(state_jobs)
        self.addCleanup(self._restore)
        self.route_file = self.base / "quick-route.json"
        self.route = ROUTE.compile_route(
            "autopilot-code", "dev", "quick", ROUTE.ROOT, ROUTE.ROOT,
            predicates=[], transport=None, tracking="tracked",
            tracked_gate_evidence={
                "spec_read": {"satisfied": True, "source": "canonical-prd-sha256"},
                "drift_verdict": "within-spec", "workflow_mode": "tracked",
                "artifact_guard": {"satisfied": True, "source": "conductor-prechecked"}},
            registered_headless_evidence={"candidates": [
                {"harness": harness, "transport": "headless",
                 "surface": "registered-headless", "status": "supported",
                 "probe_source": "fixture-probe", "probe_time": "2026-07-20T00:00:00Z"}
                for harness in ("codex", "claude")]})
        self.route_file.write_text(json.dumps(self.route), encoding="utf-8")
        self.jobs = self.base / "jobs.log"

    def _restore(self):
        for key, value in self._previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def registered_row(self, node_id, worker_type, attempt):
        """One registered quick worker row, sealing the node's complete tuple.

        No `owner_route_*` fields: a quick worker seals its identity in the
        `route_*` fields instead, which is exactly the path that reaches the
        quick branch of `validate_owner_route`.
        """
        node = next(n for n in self.route["nodes"] if n["id"] == node_id)
        scope = node["write_scope"]
        meta = ",".join([
            f"attempt_id={attempt}", f"worker_type={worker_type}", "dispatch_depth=1",
            "registered_worker=1", "harness=codex", "capability=autopilot-code",
            "capability_mode=dev", "intensity=quick",
            f"route_file={self.route_file}", f"route_id={self.route['route_id']}",
            f"route_hash={self.route['route_hash']}", f"route_node={node_id}",
            f"registry_digest={self.route['registry_digest']}",
            "write_scope=" + (";".join(scope) if isinstance(scope, list) else str(scope)),
            f"completion_gate={node['completion_gate']}",
        ])
        self.jobs.write_text("\t".join([
            "2026-09-10T00:00:00Z", "open", str(ROUTE.ROOT), str(ROUTE.ROOT),
            f"quick-{node_id}", meta]) + "\n", encoding="utf-8")
        return attempt

    def test_a_frame_leg_row_derives_its_own_tuple_and_the_owner_row_is_unchanged(self):
        for node_id, worker_type in (("one-shot", "owner"), ("frame", "frame"),
                                     ("frame-alternative", "frame")):
            with self.subTest(node_id=node_id):
                attempt = self.registered_row(node_id, worker_type, f"att-quick-{node_id}")
                owner = T.validate_owner_route(jobs=self.jobs, route_file=self.route_file,
                                               owner_attempt_id=attempt)
                self.assertEqual(owner.route_id, self.route["route_id"])
                self.assertEqual(owner.route_hash, self.route["route_hash"])
                self.assertEqual(owner.route_file, str(self.route_file.resolve()))

    def test_a_worker_type_outside_the_widened_pair_is_still_refused(self):
        """Widened to `{owner, frame}`, not opened. A `review` or `stage` row
        holding the same otherwise-valid tuple must still fail closed."""
        for worker_type in ("review", "stage", ""):
            with self.subTest(worker_type=worker_type):
                attempt = self.registered_row("frame", worker_type, "att-quick-other")
                with self.assertRaises(T.TerminalCommitError) as caught:
                    T.validate_owner_route(jobs=self.jobs, route_file=self.route_file,
                                           owner_attempt_id=attempt)
                self.assertEqual((caught.exception.code, caught.exception.detail),
                                 ("route-identity-unverified", "quick-owner-axes"))

    def test_a_frame_row_naming_the_wrong_node_is_a_tuple_refusal(self):
        """The row's `route_node` is what the tuple is derived from, so a frame
        row whose sealed gate/scope belong to another node cannot pass."""
        node = next(n for n in self.route["nodes"] if n["id"] == "frame")
        attempt = self.registered_row("frame", "frame", "att-quick-crossed")
        crossed = self.jobs.read_text(encoding="utf-8").replace(
            f"completion_gate={node['completion_gate']}", "completion_gate=quick-complete")
        self.jobs.write_text(crossed, encoding="utf-8")
        with self.assertRaises(T.TerminalCommitError) as caught:
            T.validate_owner_route(jobs=self.jobs, route_file=self.route_file,
                                   owner_attempt_id=attempt)
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ("route-identity-unverified", "quick-owner-tuple"))


if __name__ == "__main__":
    unittest.main()
