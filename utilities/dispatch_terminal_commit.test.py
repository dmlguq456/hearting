import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dispatch_terminal_commit as T
import owner_route_binding
import artifact_admission
import artifact_producer

# The one already-loaded handle on capability-route.py; the quick-branch tests
# below compile a real quick route rather than hand-rolling one, so the route
# they verify against is the route the compiler actually emits.
ROUTE = owner_route_binding.ROUTE


import importlib.util
import sys

_PRODUCER_SPEC = importlib.util.spec_from_file_location(
    "producer_fixture_for_terminal_commit", Path(__file__).with_name("artifact_producer.test.py"))
PRODUCER_FIXTURE = importlib.util.module_from_spec(_PRODUCER_SPEC)
_PRODUCER_SPEC.loader.exec_module(PRODUCER_FIXTURE)



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
        with mock.patch.object(artifact_producer, "cycle_route_admission",
                               return_value=SimpleNamespace(allow=True)):
            first = T.publish_producer_binding(artifact_root=self.root, jobs=self.jobs, route_file=self.route_file,
                                               owner_attempt_id="att-owner", cycle_id=self.cycle_id, owner_begin=True)
            stored = json.loads(first.path.read_text())
            stored["primary"] = str(self.root / "owner-report.md")
            (self.root / "owner-report.md").write_text("primary\n")
            first.path.write_text(json.dumps(stored, sort_keys=True, separators=(",", ":")) + "\n")
            second = T.publish_producer_binding(artifact_root=self.root, jobs=self.jobs, route_file=self.route_file,
                                                owner_attempt_id="att-owner", cycle_id=self.cycle_id, owner_begin=True)
        self.assertNotEqual(first.digest, second.digest)
        self.assertTrue(second.replay)
        self.assertEqual(second.binding["primary"], str(self.root / "owner-report.md"))
        with self.assertRaises(T.TerminalCommitError) as caught:
            T.publish_producer_binding(artifact_root=self.root, jobs=self.jobs, route_file=self.route_file,
                                       owner_attempt_id="att-owner", cycle_id="cyc_" + "d" * 32, owner_begin=True)
        self.assertEqual(caught.exception.code, "producer-binding-mismatch")

    def _record_begun_by(self, route_id, route_hash):
        path = self.root / ".runtime/artifact-producer/v1/cycles" / (self.cycle_id + ".json")
        record = json.loads(path.read_text())
        record.update(route_id=route_id, route_hash=route_hash)
        path.write_bytes(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())

    @mock.patch.object(T.artifact_lifecycle, "read_root_identity", return_value=SimpleNamespace(repository_id="repo_x", artifact_root_id="root_y"))
    @mock.patch.object(owner_route_binding, "resolve_owner_route_lifecycle")
    def test_continuation_owner_binds_the_cycle_its_verified_ancestor_began(self, resolve, _identity):
        resolve.return_value = (owner_route_binding.OwnerRouteBinding(str(self.route_file), "rt-abcdef12", "sha256:" + "a" * 64), "current")
        parent = {"route_id": "rt-parent01", "route_hash": "sha256:" + "p" * 64}
        self._record_begun_by(parent["route_id"], parent["route_hash"])
        own = {"route_id": "rt-abcdef12", "route_hash": "sha256:" + "a" * 64}
        with mock.patch.object(artifact_producer, "cycle_route_admission",
                               return_value=SimpleNamespace(allow=True)) as admission:
            result = T.publish_producer_binding(artifact_root=self.root, jobs=self.jobs, route_file=self.route_file,
                                                owner_attempt_id="att-owner", cycle_id=self.cycle_id, owner_begin=True)
        self.assertEqual(admission.call_args.args[2], own)
        self.assertEqual(admission.call_args.args[1]["route_id"], parent["route_id"])
        self.assertEqual(result.binding["route_id"], "rt-abcdef12")
        self.assertEqual(result.binding["cycle_id"], self.cycle_id)

    @mock.patch.object(T.artifact_lifecycle, "read_root_identity", return_value=SimpleNamespace(repository_id="repo_x", artifact_root_id="root_y"))
    @mock.patch.object(owner_route_binding, "resolve_owner_route_lifecycle")
    def test_a_cycle_begun_outside_the_lineage_or_under_another_hash_stays_refused(self, resolve, _identity):
        resolve.return_value = (owner_route_binding.OwnerRouteBinding(str(self.route_file), "rt-abcdef12", "sha256:" + "a" * 64), "current")
        own = {"route_id": "rt-abcdef12", "route_hash": "sha256:" + "a" * 64}
        parent = {"route_id": "rt-parent01", "route_hash": "sha256:" + "p" * 64}
        cases = (
            ("rt-stranger", "sha256:" + "s" * 64, [own, parent]),
            ("rt-parent01", "sha256:" + "x" * 64, [own, parent]),
            ("rt-parent01", "sha256:" + "p" * 64, T.route_lineage.RouteLineageError("route-lineage-unverified", "x")),
        )
        for begin_id, begin_hash, lineage in cases:
            with self.subTest(begin=begin_id, hash=begin_hash[-4:]):
                self._record_begun_by(begin_id, begin_hash)
                denied = SimpleNamespace(allow=False, reason="cycle-route-binding-mismatch", detail="fixture")
                with mock.patch.object(artifact_producer, "cycle_route_admission", return_value=denied), \
                        self.assertRaises(T.TerminalCommitError) as caught:
                    T.publish_producer_binding(artifact_root=self.root, jobs=self.jobs, route_file=self.route_file,
                                               owner_attempt_id="att-owner", cycle_id=self.cycle_id, owner_begin=True)
                self.assertEqual((caught.exception.code, caught.exception.detail), ("producer-binding-mismatch", "cycle-route"))

    def test_path_is_single_safe_derivation_and_digest_is_bytes(self):
        path = T.producer_binding_path(self.root, "rt-abcdef12", "att-owner")
        self.assertEqual(path, self.root / ".runtime/terminal-commits/v1/rt-abcdef12/att-owner/producer-binding.json")
        with self.assertRaises(T.TerminalCommitError):
            T.producer_binding_path(self.root, "../route", "att-owner")

    def test_a_repeated_refusal_keeps_one_counted_record_instead_of_a_file_per_retry(self):
        # A settlement retried every few seconds against the same refusal once
        # left hundreds of thousands of records in one terminal slot.
        request = T.TerminalCommitRequest(self.route_file, "att-owner", self.jobs, self.root)
        for _ in range(3):
            T._record_attempt(request, "producer-finalize-failed", "parent-cycle-not-sealed: " + self.cycle_id)
        T._record_attempt(request, "recovery-unavailable", "material-primary-required")
        attempts = T.terminal_slot(self.root, "rt-abcdef12", "att-owner") / "attempts"
        records = sorted((json.loads(path.read_text(encoding="utf-8")) for path in attempts.iterdir()),
                         key=lambda record: record["reason"])
        self.assertEqual([(record["reason"], record["count"]) for record in records],
                         [("producer-finalize-failed", 3), ("recovery-unavailable", 1)])
        self.assertLessEqual(records[0]["first_ns"], records[0]["last_ns"])
        self.assertEqual(records[1]["first_ns"], records[1]["last_ns"])

    def test_latest_refusal_is_request_bound_and_skips_corrupt_records(self):
        request = T.TerminalCommitRequest(self.route_file, "att-owner", self.jobs, self.root)
        T._record_attempt(request, "output-invalid", "locator-invalid-component:artifacts/bad|name.txt")
        T._record_attempt(request, "recovery-unavailable", "material-primary-required")
        attempts = T._commit_state_path(request).parent / "attempts"
        rows = {json.loads(path.read_text())["reason"]: path for path in attempts.glob("*.json")}
        output = json.loads(rows["output-invalid"].read_text())
        recovery = json.loads(rows["recovery-unavailable"].read_text())
        output["first_ns"], output["last_ns"] = 50, 100
        recovery["first_ns"], recovery["last_ns"] = 150, 200
        rows["output-invalid"].write_text(json.dumps(output))
        rows["recovery-unavailable"].write_text(json.dumps(recovery))
        latest = T.last_owner_refusal(request)
        self.assertEqual((latest["reason"], latest["last_ns"]), ("recovery-unavailable", 200))
        self.assertIsNone(latest["remediation"])

        rows["recovery-unavailable"].write_text("not-json")
        foreign = T.TerminalCommitRequest(self.route_file, "att-foreign", self.jobs, self.root)
        T._record_attempt(foreign, "transaction-conflict", "foreign-owner")
        fallback = T.last_owner_refusal(request)
        self.assertEqual(fallback["reason"], "output-invalid")
        self.assertEqual(fallback["detail"], "locator-invalid-component:artifacts/bad|name.txt")
        self.assertEqual(fallback["record_path"], str(rows["output-invalid"]))
        self.assertIn("Correct the reported locator", fallback["remediation"])

    def test_owner_prerequisites_keep_resource_evidence_and_do_not_invent_a_second_owner(self):
        # SD-154 A-2: `_marker_identity_row`'s own evidence-sha recompute is now
        # `evidence_currency`'s result (single recompute site, A-SD154-8), which
        # proves full schema-v2 identity (schema_version/sequence/history-byte
        # match) the way every real `write_completion_marker` marker already
        # does -- so the fixture marker needs those fields too, and a stale
        # digest now reads `completion-evidence-revised-unrecorded` (owner can
        # recover with `revise`), not the retired `completion-evidence-hash-mismatch`.
        evidence=self.root/"resource.json"; evidence.write_text('{"exit_code":0}')
        resource={"id":"run","kind":"resource-runner","completion_gate":"lab-run"}
        publish={"id":"publish","kind":"capability-owner","unit":"_kernel/owner",
                 "dispatch_depth":1,"depends_on":["run"]}
        terminal={**publish,"id":"sync","terminal":True,"depends_on":["publish"]}
        route={"route_id":"rt-resource-owner","route_hash":"sha256:fixture","nodes":[resource,publish,terminal]}
        directory=ROUTE.completion_dir(route["route_id"],jobs=self.jobs); directory.mkdir(parents=True)
        marker={"schema_version":2,"route_id":route["route_id"],"route_hash":route["route_hash"],
                "node_id":"run","completion_gate":"lab-run","registered_worker":False,"sequence":1,
                "evidence":{"path":str(evidence),"sha256":ROUTE.evidence_digest(evidence)}}
        (directory/"run.json").write_text(json.dumps(marker))
        (directory/"run.1.json").write_text(json.dumps(marker))
        self.assertEqual(ROUTE.owner_terminal_prerequisites(route,terminal,self.jobs),{})
        self.assertEqual(self.jobs.read_text(),"")
        # Use a different-size payload so stat-based digest caches cannot turn
        # this same-test rewrite into a false unchanged-content observation.
        evidence.write_text('{"exit_code":10}')
        # This test targets the checked gate-on classification while preserving
        # the user's runtime gate-off behavior tested by the default call above.
        with mock.patch.object(T.dispatch_contract, "gates_on", return_value=True):
            self.assertEqual(ROUTE.owner_terminal_prerequisites(route,terminal,self.jobs),
                             {"run":"completion-evidence-revised-unrecorded"})

    def test_route_children_consumes_exact_owner_and_inline_gate_proofs(self):
        route = {"route_id": "rt-gate-proof", "route_hash": "sha256:" + "a" * 64,
                 "nodes": [{"id": "execute", "terminal": True}],
                 "workflow_contract": {"terminal_nodes": ["execute"]}}
        inline_jobs = self.root / "inline-jobs.log"
        inline_jobs.write_text("", encoding="utf-8")
        request = T.TerminalCommitRequest(self.route_file, "att-owner", inline_jobs, self.root)
        inline_gate = {"execute": {"passed": True, "current": True,
                                    "attempt_id": "att-inline", "attempt_readiness": "ready"}}
        self.assertEqual(T._prove_route_children(request, route, inline_gate).status, "proved")
        owner_gate = {"execute": {"passed": True, "source": "owner-terminal",
                                   "attempt_id": "att-owner", "current": True,
                                   "attempt_readiness": "quiescent"}}
        self.assertEqual(T._prove_route_children(request, route, owner_gate).status, "proved")

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
             mock.patch.object(artifact_producer, "cycle_route_admission", return_value=SimpleNamespace(allow=True)), \
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


    def test_closed_or_moved_cycle_still_proves_the_binding(self):
        # §45 D-123: the cycle's state and campaign are not part of its identity.
        route = {"route_id": "rt-abcdef12", "route_hash": "sha256:" + "a" * 64,
                 "capability": "autopilot-code", "capability_mode": "dev", "nodes": []}
        seal_fixture_route(route, self.route_file, self.root, self.jobs, "att-owner")
        owner = owner_route_binding.OwnerRouteBinding(str(self.route_file), route["route_id"], route["route_hash"])
        cycle_id = "cyc_" + "d" * 32
        bound = {"state": "open", "campaign_id": "camp_" + "1" * 32, "cycle_id": cycle_id,
                 "producer_id": "prod_" + "2" * 32, "route_id": route["route_id"],
                 "route_hash": route["route_hash"], "route_file": str(self.route_file)}
        cycle_path = self.root / ".runtime/artifact-producer/v1/cycles" / f"{cycle_id}.json"
        cycle_path.parent.mkdir(parents=True, exist_ok=True)
        binding = SimpleNamespace(binding={
            "route_hash": route["route_hash"], "cycle_id": cycle_id, "campaign_id": bound["campaign_id"],
            "cycle_record_digest": T.cycle_identity_digest(bound)})

        def prove(record):
            cycle_path.write_text(json.dumps(record), encoding="utf-8")
            with mock.patch.object(T, "validate_owner_route", return_value=owner), \
                 mock.patch.object(T, "producer_lifecycle_applies", return_value=True), \
                 mock.patch.object(T, "_route_module") as route_module, \
                 mock.patch.object(T, "load_producer_binding", return_value=binding), \
                 mock.patch.object(artifact_producer, "cycle_route_admission", return_value=SimpleNamespace(allow=True)), \
                 mock.patch("artifact_producer._live_review_lease", return_value=None):
                route_module.return_value.terminal_gate_observation.return_value = {"route": {"passed": True}}
                return T.prove_terminal_authority(T.TerminalCommitRequest(
                    self.route_file, "att-owner", self.jobs, self.root))

        closed_and_moved = {**bound, "state": "sealed", "campaign_id": "camp_" + "9" * 32}
        self.assertEqual(prove(closed_and_moved).status, "proved")
        drifted = prove({**closed_and_moved, "producer_id": "prod_" + "8" * 32})
        self.assertEqual((drifted.status, drifted.reason, drifted.detail),
                         ("rejected", "producer-binding-mismatch", "cycle-identity-drift"))


class ContinuationTerminalSettlementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.route_file = self.root / "route.json"
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.route = {"route_id": "rt-a3fixture", "route_hash": "sha256:" + "a" * 64,
            "nodes": [{"id": "execute", "terminal": True}],
            "workflow_contract": {"terminal_nodes": ["execute"]},
            "capability": "fixture-cap", "capability_mode": "default"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_terminal_proof_uses_shared_lineage_admission_before_any_settlement_mutation(self):
        route = dict(self.route, capability="autopilot-code", effective_intensity="standard")
        seal_fixture_route(route, self.route_file, self.root, self.jobs, "att-a3fixture")
        owner = owner_route_binding.OwnerRouteBinding(
            str(self.route_file), route["route_id"], route["route_hash"])
        cycle_id = "cyc_" + "d" * 32
        cycle = {"cycle_id": cycle_id, "campaign_id": "camp_" + "b" * 32,
            "producer_id": "prod_" + "c" * 32, "route_id": route["route_id"],
            "route_hash": route["route_hash"], "route_file": str(self.route_file),
            "capability": route["capability"], "intensity": route["effective_intensity"], "state": "open"}
        cycle_path = self.root / ".runtime/artifact-producer/v1/cycles" / f"{cycle_id}.json"
        cycle_path.parent.mkdir(parents=True, exist_ok=True)
        cycle_path.write_text(json.dumps(cycle), encoding="utf-8")
        binding = SimpleNamespace(binding={"route_hash": route["route_hash"], "cycle_id": cycle_id,
            "cycle_record_digest": T.cycle_identity_digest(cycle)})
        before = sorted((str(path.relative_to(self.root)), path.read_bytes())
                        for path in self.root.rglob("*") if path.is_file())
        denied = SimpleNamespace(allow=False, reason="cycle-route-binding-mismatch:lineage-fork", detail="fork")
        with mock.patch.object(T, "validate_owner_route", return_value=owner), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=True), \
             mock.patch.object(T, "load_producer_binding", return_value=binding), \
             mock.patch.object(T, "_route_module") as route_module, \
             mock.patch.object(artifact_producer, "cycle_route_admission", return_value=denied):
            route_module.return_value.terminal_gate_observation.return_value = {"execute": {"passed": True}}
            proof = T.prove_terminal_authority(T.TerminalCommitRequest(
                self.route_file, "att-a3fixture", self.jobs, self.root))
        self.assertEqual((proof.status, proof.reason), ("rejected", "producer-binding-mismatch"))
        after = sorted((str(path.relative_to(self.root)), path.read_bytes())
                       for path in self.root.rglob("*") if path.is_file())
        self.assertEqual(before, after)


class RelatedOwnerDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.route_file = self.root / "route.json"
        self.jobs = self.root / "jobs.log"
        self.route = {
            "route_id": "rt-related-owner",
            "route_hash": "sha256:" + "c" * 64,
            "nodes": [{"id": "owner-terminal", "kind": "capability-owner",
                        "unit": "_kernel/owner", "dispatch_depth": 1, "terminal": True}],
            "workflow_contract": {"terminal_nodes": ["owner-terminal"]},
        }
        self.route_file.write_text(json.dumps(self.route), encoding="utf-8")
        self.jobs.write_text("", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, status, attempt, *, owner_route_id=None, owner_route_hash=None,
            note="dead-launch-error", failure_class="runtime-error",
            launch_outcome="reaped-before-publish", **values):
        fields = {
            "attempt_schema_version": "2", "dispatch_depth": "1",
            "transport": "headless", "execution_surface": "registered-headless",
            "registered_worker": "1", "fallback_hop": "same-harness-headless",
            "worker_type": "owner", "unit": "_kernel/owner", "attempt_id": attempt,
            "note": note, "failure_class": failure_class,
            "launch_outcome": launch_outcome,
        }
        if owner_route_id is not None:
            fields["owner_route_id"] = owner_route_id
        if owner_route_hash is not None:
            fields["owner_route_hash"] = owner_route_hash
        fields.update(values)
        return "2026-09-21T00:00:00Z\t{}\t{}\t{}\towner\t{}".format(
            status, self.root, self.root,
            ",".join(f"{key}={value}" for key, value in fields.items()))

    def request(self):
        return T.TerminalCommitRequest(self.route_file, "att-latest", self.jobs, self.root)

    def owner_gate(self, **values):
        return {"owner-terminal": {"passed": True, "current": True,
                                    "source": "owner-terminal", "attempt_id": "att-latest",
                                    "attempt_readiness": "quiescent", **values}}

    def test_earlier_owner_with_live_process_blocks_latest_quiescent_owner(self):
        process = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            identity = T.dispatch_contract.process_launch_identity(process.pid)
            live = {"pid": identity["pid"], "pid_start": identity["pid_start"],
                    "pgid": identity["pgid"], **identity}
            earlier = self.row("done", "att-earlier", owner_route_id=self.route["route_id"],
                               owner_route_hash=self.route["route_hash"], **live,
                               launch_outcome="running")
            latest = self.row("done", "att-latest", owner_route_id=self.route["route_id"],
                              owner_route_hash=self.route["route_hash"],
                              note="completed-marker", failure_class="pass",
                              launch_outcome="never-launched")
            self.jobs.write_text(earlier + "\n" + latest + "\n", encoding="utf-8")
            proof = T._prove_route_children(self.request(), self.route, self.owner_gate())
            self.assertEqual((proof.status, proof.reason), ("rejected", "child-not-quiescent"))
            calls = []
            owner = owner_route_binding.OwnerRouteBinding(
                str(self.route_file), self.route["route_id"], self.route["route_hash"])
            services = T.TerminalCommitServices(
                close_route=lambda *args, **kwargs: calls.append("close"),
                finalize_exact_cycle=lambda *args, **kwargs: calls.append("finalize"),
                seal_envelope=lambda **kwargs: calls.append("envelope"),
            )
            with mock.patch.object(T, "verify_request_identity"), \
                 mock.patch.object(T, "validate_owner_route", return_value=owner), \
                 mock.patch.object(T, "producer_lifecycle_applies", return_value=False), \
                 mock.patch.object(T, "_route_module") as route_module:
                route_module.return_value.terminal_gate_observation.return_value = self.owner_gate()
                transaction = T.settle_terminal_commit(self.request(), services)
            self.assertEqual((transaction.result, transaction.reason),
                             ("recoverable", "child-not-quiescent"))
            self.assertTrue(transaction.next_step["retry_when"])
            self.assertEqual(calls, [])
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_owner_conflict_is_not_hidden_by_owner_alias_binding(self):
        conflict = self.row("done", "att-conflict", owner_route_id=self.route["route_id"],
                            owner_route_hash=self.route["route_hash"],
                            terminal_conflict="1", conflicting_terminal_note="completed-marker",
                            conflicting_failure_class="runtime-error")
        latest = self.row("done", "att-latest", owner_route_id=self.route["route_id"],
                          owner_route_hash=self.route["route_hash"],
                          note="completed-marker", failure_class="pass",
                          launch_outcome="never-launched")
        self.jobs.write_text(conflict + "\n" + latest + "\n", encoding="utf-8")
        proof = T._prove_route_children(self.request(), self.route, self.owner_gate())
        self.assertEqual((proof.status, proof.reason, proof.detail),
                         ("rejected", "transaction-conflict", "child-terminal-conflict"))

    def test_latest_owner_is_quiescent_and_unrelated_owner_is_non_blocking(self):
        latest = self.row("done", "att-latest", owner_route_id=self.route["route_id"],
                          owner_route_hash=self.route["route_hash"],
                          note="completed-marker", failure_class="pass",
                          launch_outcome="never-launched")
        unrelated = self.row("done", "att-unrelated", owner_route_id="rt-unrelated",
                             owner_route_hash="sha256:" + "d" * 64,
                             note="dead-launch-error", failure_class="runtime-error")
        self.jobs.write_text(latest + "\n" + unrelated + "\n", encoding="utf-8")
        proof = T._prove_route_children(self.request(), self.route, self.owner_gate())
        self.assertEqual((proof.status, proof.reason), ("proved", None))

    def test_terminal_proof_keeps_damaged_or_uncommitted_registry_unknown(self):
        latest = self.row("done", "att-latest", owner_route_id=self.route["route_id"],
                          owner_route_hash=self.route["route_hash"],
                          note="completed-marker", failure_class="pass",
                          launch_outcome="never-launched")
        child = self.row("open", "att-hidden", dispatch_depth="2",
                         worker_type="stage", parent_attempt_id="att-latest")
        for suffix in (b"\xff\n", child.encode("utf-8")):
            with self.subTest(suffix=suffix[:20]):
                self.jobs.write_bytes((latest + "\n").encode("utf-8") + suffix)
                before = self.jobs.read_bytes()
                proof = T._prove_route_children(self.request(), self.route, self.owner_gate())
                self.assertEqual((proof.status, proof.reason),
                                 ("rejected", "route-identity-unverified"))
                self.assertEqual(self.jobs.read_bytes(), before)

        # Committing the newline reveals the real open child; it still cannot
        # be hidden by the cache's earlier prefix or by an exact owner gate.
        self.jobs.write_text(latest + "\n" + child + "\n", encoding="utf-8")
        self.assertEqual(T._prove_route_children(
            self.request(), self.route, self.owner_gate()).status, "rejected")

        # Valid malformed history and a literal replacement character keep
        # the prior tolerant semantics once the current text is complete.
        self.jobs.write_text(latest + "\nmalformed \ufffd\n", encoding="utf-8")
        self.assertEqual(T._prove_route_children(
            self.request(), self.route, self.owner_gate()).status, "proved")

    def test_a_registered_row_closed_without_ever_launching_does_not_block_settlement(self):
        # OpenCode r4 leg1: a `test` row was registered, never claimed, then closed by a marker;
        # it has no process identity because nothing was ever spawned.
        latest = self.row("done", "att-latest", owner_route_id=self.route["route_id"],
                          owner_route_hash=self.route["route_hash"],
                          note="completed-marker", failure_class="pass", launch_outcome="never-launched")
        stage = {"worker_type": "stage", "dispatch_depth": "2", "parent_attempt_id": "att-latest",
                 "launch_claimed": "0", "note": "completed-marker", "failure_class": "pass", "launch_outcome": ""}
        unlaunched = self.row("done", "att-unlaunched", **stage)
        self.jobs.write_text(latest + "\n" + unlaunched + "\n", encoding="utf-8")
        proof = T._prove_route_children(self.request(), self.route, self.owner_gate())
        self.assertEqual((proof.status, proof.reason), ("proved", None))
        # A row that did claim its launch but has lost its process identity still blocks.
        claimed = self.row("done", "att-unlaunched", **{**stage, "launch_claimed": "1"})
        T.dispatch_contract._atomic_registry_replace(self.jobs, [latest.rstrip("\n"), claimed.rstrip("\n")])
        proof = T._prove_route_children(self.request(), self.route, self.owner_gate())
        self.assertEqual((proof.status, proof.reason), ("rejected", "child-not-quiescent"))
        # Any process identity keeps the ordinary quiescence check, whatever the launch flags say.
        process = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            identity = T.dispatch_contract.process_launch_identity(process.pid)
            live = self.row("done", "att-unlaunched", **{**stage, **identity, "launch_outcome": "running"})
            T.dispatch_contract._atomic_registry_replace(self.jobs, [latest.rstrip("\n"), live.rstrip("\n")])
            proof = T._prove_route_children(self.request(), self.route, self.owner_gate())
            self.assertEqual((proof.status, proof.reason), ("rejected", "child-not-quiescent"))
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_a_poll_fallback_parent_gets_no_completion_notice_attempt(self):
        self.assertFalse(T._parent_has_notice_carrier(
            {"parent_sid": "ses", "parent_completion_delivery": "poll-fallback"}))
        self.assertFalse(T._parent_has_notice_carrier({"parent_completion_delivery": "opencode-turn"}))
        self.assertTrue(T._parent_has_notice_carrier(
            {"parent_sid": "ses", "parent_completion_delivery": "opencode-turn"}))
        self.assertTrue(T._parent_has_notice_carrier({"parent_attempt_id": "att-owner"}))

    def test_reaped_failed_owner_allows_valid_inline_fallback(self):
        failed = self.row("done", "att-reaped", owner_route_id=self.route["route_id"],
                          owner_route_hash=self.route["route_hash"])
        self.jobs.write_text(failed + "\n", encoding="utf-8")
        inline_gate = {"owner-terminal": {"passed": True, "current": True,
                                           "attempt_id": "att-inline", "attempt_readiness": "ready"}}
        proof = T._prove_route_children(self.request(), self.route, inline_gate)
        self.assertEqual((proof.status, proof.reason), ("proved", None))

    def test_mismatched_owner_aliases_fail_closed(self):
        for values in (
            {"owner_route_id": self.route["route_id"], "owner_route_hash": "sha256:" + "e" * 64},
            {"owner_route_id": "rt-other", "owner_route_hash": self.route["route_hash"]},
        ):
            with self.subTest(values=values):
                self.jobs.write_text(self.row("done", "att-bad", **values) + "\n", encoding="utf-8")
                proof = T._prove_route_children(self.request(), self.route, self.owner_gate())
                self.assertEqual((proof.status, proof.reason),
                                 ("rejected", "route-identity-unverified"))

    def test_unrelated_incomplete_owner_tuple_does_not_block_current_owner(self):
        latest = self.row("done", "att-latest", owner_route_id=self.route["route_id"],
                          owner_route_hash=self.route["route_hash"],
                          note="completed-marker", failure_class="pass",
                          launch_outcome="never-launched")
        unrelated = self.row("done", "att-unrelated",
                             owner_route_id="rt-unrelated",
                             note="dead-launch-error", failure_class="runtime-error")
        self.jobs.write_text(latest + "\n" + unrelated + "\n", encoding="utf-8")
        proof = T._prove_route_children(self.request(), self.route, self.owner_gate())
        self.assertEqual((proof.status, proof.reason), ("proved", None))

    def test_related_incomplete_owner_tuple_still_fails_closed(self):
        malformed = self.row("done", "att-malformed",
                             owner_route_id=self.route["route_id"])
        self.jobs.write_text(malformed + "\n", encoding="utf-8")
        proof = T._prove_route_children(self.request(), self.route, self.owner_gate())
        self.assertEqual((proof.status, proof.reason),
                         ("rejected", "route-identity-unverified"))

    def _report_terminal_route(self):
        route = dict(self.route)
        route["nodes"] = [{"id": "report", "kind": "pipeline-stage",
                           "unit": "editorial/report", "dispatch_depth": 2,
                           "terminal": True}]
        route["workflow_contract"] = {"terminal_nodes": ["report"]}
        self.route_file.write_text(json.dumps(route), encoding="utf-8")
        return route

    def _depth2_report_row(self, status, attempt, **values):
        return self.row(
            status, attempt,
            owner_route_id="rt-inherited-owner",
            owner_route_hash="sha256:" + "9" * 64,
            dispatch_depth="2", worker_type="stage", unit="editorial/report",
            route_id=self.route["route_id"], route_hash=self.route["route_hash"],
            route_node="report", parent_attempt_id="att-old-parent", **values,
        )

    def test_depth2_uses_own_route_tuple_despite_inherited_owner_context(self):
        route = self._report_terminal_route()
        process = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            identity = T.dispatch_contract.process_launch_identity(process.pid)
            live = {"pid": identity["pid"], "pid_start": identity["pid_start"],
                    "pgid": identity["pgid"], **identity}
            row = self._depth2_report_row(
                "done", "att-report-live", launch_outcome="running", **live)
            self.jobs.write_text(row + "\n", encoding="utf-8")
            request = T.TerminalCommitRequest(
                self.route_file, "att-current-owner", self.jobs, self.root)
            gates = {"report": {"passed": True, "current": True,
                                "attempt_id": "att-report-live",
                                "attempt_readiness": "quiescent"}}
            proof = T._prove_route_children(request, route, gates)
            self.assertEqual((proof.status, proof.reason),
                             ("rejected", "child-not-quiescent"))
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_reaped_depth2_with_inherited_owner_context_keeps_existing_policy(self):
        route = self._report_terminal_route()
        row = self._depth2_report_row("done", "att-report-reaped")
        self.jobs.write_text(row + "\n", encoding="utf-8")
        request = T.TerminalCommitRequest(
            self.route_file, "att-current-owner", self.jobs, self.root)
        gates = {"report": {"passed": True, "current": True,
                            "attempt_id": "att-report-reaped",
                            "attempt_readiness": "quiescent"}}
        self.assertTrue(T._related_route_identity(
            T.dispatch_contract.parse_registry_metadata(row.split("\t")[5]), route))
        proof = T._prove_route_children(request, route, gates)
        self.assertEqual((proof.status, proof.reason), ("proved", None))

    def test_owner_closure_review_row_does_not_block_terminal_proof(self):
        # F4: a depth-2 review row an owner sealed under `gate_closure:
        # owner-closure` keeps its verdict axis on FAIL (plan §2 row for
        # `_complete_node_locked`) while being the terminal record of a
        # successful owner sweep. The related-row full scan (fb0d43ad) must
        # not reject it the way a literal failure_class comparison would.
        route = self._report_terminal_route()
        row = self._depth2_report_row(
            "done", "att-review-closed", note="completed-marker",
            failure_class="fail", gate_closure="owner-closure",
        )
        self.jobs.write_text(row + "\n", encoding="utf-8")
        request = T.TerminalCommitRequest(
            self.route_file, "att-current-owner", self.jobs, self.root)
        gates = {"report": {"passed": True, "current": True,
                            "attempt_id": "att-review-closed",
                            "attempt_readiness": "quiescent"}}
        proof = T._prove_route_children(request, route, gates)
        self.assertEqual((proof.status, proof.reason), ("proved", None))

    def test_hashless_route_does_not_relate_every_row(self):
        # F5: a route with no route_hash must not treat a row with neither
        # owner nor route aliases as related merely because `None in
        # (None, None)` is True. Without the empty-alias exclusion this
        # raises ValueError (int(None)) instead of classifying the row as
        # simply unrelated.
        route = {"route_id": "rt-hashless", "nodes": [{"id": "owner-terminal",
                 "kind": "capability-owner", "unit": "_kernel/owner",
                 "dispatch_depth": 1, "terminal": True}]}
        metadata = {"attempt_id": "att-unrelated", "note": "dead-launch-error",
                    "failure_class": "runtime-error"}
        self.assertFalse(T._related_route_identity(metadata, route))


class ExactPassClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.route_file = self.root / "route.json"
        self.jobs = self.root / "jobs.log"
        self.route = {"route_id": "rt-exact-pass", "route_hash": "sha256:" + "f" * 64,
                      "nodes": [{"id": "child", "dispatch_depth": 2, "terminal": True}],
                      "workflow_contract": {"terminal_nodes": ["child"]}}
        self.route_file.write_text(json.dumps(self.route), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def metadata(self):
        return ("attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,route_id=rt-exact-pass,"
                "route_hash=sha256:" + "f" * 64 + ",route_node=child,"
                "attempt_id=att-contradiction,note=completed-marker,"
                "failure_class=runtime-error,launch_outcome=never-launched")

    def test_completed_marker_with_runtime_error_is_rejected_downstream(self):
        self.jobs.write_text(f"2026-09-21T00:00:00Z\tdone\t{self.root}\t{self.root}\tchild\t{self.metadata()}\n",
                             encoding="utf-8")
        request = T.TerminalCommitRequest(self.route_file, "att-owner", self.jobs, self.root)
        gates = {"child": {"passed": True, "current": True,
                            "attempt_id": "att-contradiction", "attempt_readiness": "quiescent"}}
        with mock.patch("dispatch_attempt_policy.decide_attempt",
                         side_effect=AssertionError("contradictory marker reached decision")):
            proof = T._prove_route_children(request, self.route, gates)
        self.assertEqual((proof.status, proof.reason, proof.detail),
                         ("rejected", "terminal-marker-not-current", "terminal-attempt-not-pass"))

    def test_readiness_rejects_completed_marker_without_exact_pass_class(self):
        self.jobs.write_text(f"2026-09-21T00:00:00Z\tdone\t{self.root}\t{self.root}\tchild\t{self.metadata()}\n",
                             encoding="utf-8")
        marker = {"attempt_id": "att-contradiction", "registered_worker": True}
        readiness = T.dispatch_contract.completion_attempt_readiness(
            self.route, self.route["nodes"][0], marker, self.jobs)
        self.assertEqual((readiness.state, readiness.reason),
                         ("unverifiable", "marker-attempt-failure-class-not-pass"))


class DeferredAndOwnerClosureVerdictTest(unittest.TestCase):
    """F2: completion-deferred success stays a pass (backlog #6), a still-
    pending deferred row stays not-terminal, independent of the new
    contradiction check."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.route_file = self.root / "route.json"
        self.jobs = self.root / "jobs.log"
        self.route = {"route_id": "rt-deferred-verdict", "route_hash": "sha256:" + "1" * 64,
                      "nodes": [{"id": "execute", "terminal": True}],
                      "workflow_contract": {"terminal_nodes": ["execute"]}}
        self.route_file.write_text(json.dumps(self.route), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _row(self, attempt_id, **overrides):
        fields = {
            "attempt_schema_version": "2", "dispatch_depth": "2",
            "transport": "headless", "execution_surface": "registered-headless",
            "registered_worker": "1", "fallback_hop": "same-harness-headless",
            "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
            "route_node": "execute", "attempt_id": attempt_id,
            "classifier_source": "registered-wrapper-completion-transient-v1",
            "launch_outcome": "reaped-before-publish",
        }
        fields.update(overrides)
        return "2026-09-21T00:00:00Z\tdone\t{}\t{}\texecute\t{}".format(
            self.root, self.root, ",".join(f"{k}={v}" for k, v in fields.items()))

    def test_completed_deferral_is_ready_and_proved(self):
        row = self._row("att-deferred-done", note="completed-marker",
                        failure_class="infrastructure",
                        completion_marker="/artifacts/.runtime/completions/execute.json")
        self.jobs.write_text(row + "\n", encoding="utf-8")
        marker = {"attempt_id": "att-deferred-done", "registered_worker": True}
        readiness = T.dispatch_contract.completion_attempt_readiness(
            self.route, self.route["nodes"][0], marker, self.jobs)
        self.assertEqual((readiness.state, readiness.reason[:0]), ("ready", ""))
        request = T.TerminalCommitRequest(self.route_file, "att-owner", self.jobs, self.root)
        gates = {"execute": {"passed": True, "current": True,
                             "attempt_id": "att-deferred-done", "attempt_readiness": "ready"}}
        proof = T._prove_route_children(request, self.route, gates)
        self.assertEqual((proof.status, proof.reason), ("proved", None))

    def test_pending_deferral_is_not_terminal(self):
        row = self._row("att-deferred-pending", note="completion-deferred",
                        failure_class="infrastructure")
        self.jobs.write_text(row + "\n", encoding="utf-8")
        marker = {"attempt_id": "att-deferred-pending", "registered_worker": True}
        readiness = T.dispatch_contract.completion_attempt_readiness(
            self.route, self.route["nodes"][0], marker, self.jobs)
        self.assertEqual((readiness.state, readiness.reason),
                         ("unverifiable", "marker-attempt-not-terminal"))


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
        with self.jobs.open("a") as handle:
            handle.write(f"2026-09-07T00:00:00Z\tdone\t{self.root}\t{self.root}\texecute\t"
                f"attempt_id=att-execute,parent_attempt_id=att-a3fixture,route_id={self.route['route_id']},"
                "route_node=execute,attempt_schema_version=2,dispatch_depth=2,registered_worker=1,"
                "transport=headless,execution_surface=registered-headless,fallback_hop=same-harness-headless,"
                "harness=codex,note=completed-marker,failure_class=pass,launch_outcome=reaped-before-publish\n")
        self.gates = {"execute": {"passed": True, "evidence": str(self.artifact),
            "node_id": "execute", "attempt_id": "att-execute", "completion_gate": "code-execute",
            "marker_digest": "a" * 64, "evidence_digest": "b" * 64}}

    def tearDown(self):
        self.tmp.cleanup()

    def request(self):
        return T.TerminalCommitRequest(self.route_file, "att-a3fixture", self.jobs, self.root)

    def _slot(self):
        return T._commit_state_path(self.request()).parent

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


class TerminalCommitErrorEvidenceTest(_TerminalCommitFixture):
    """D2 (2026-09-27 owner decision): the shared next-action evidence a
    session supervisor's top-level handler attaches to an escaped
    `TerminalCommitError`, instead of discarding `code`/`detail`."""

    def test_evidence_includes_the_terminal_slot_when_the_route_is_readable(self):
        with self.assertRaises(T.TerminalCommitError) as caught:
            T.load_producer_binding(artifact_root=self.root, route_id=self.route["route_id"],
                                    owner_attempt_id="att-a3fixture")
        exc = caught.exception
        binding_path = T.producer_binding_path(self.root, self.route["route_id"], "att-a3fixture")
        self.assertTrue(exc.detail.startswith(
            "not published at owner launch; next: the owner re-runs artifact_producer.py begin --require-cycle"))
        self.assertTrue(exc.detail.endswith(str(binding_path)))
        evidence = T.terminal_commit_error_evidence(
            exc, route_file=self.route_file, route_id=self.route["route_id"],
            owner_attempt_id="att-a3fixture")
        self.assertEqual(evidence["code"], "producer-binding-required")
        self.assertTrue(evidence["detail"].startswith(
            "not published at owner launch; next: the owner re-runs artifact_producer.py begin --require-cycle"))
        self.assertNotIn("route_file", evidence)
        self.assertEqual(evidence["terminal_slot"],
                         str(T.terminal_slot(self.root, self.route["route_id"], "att-a3fixture")))

    def test_evidence_never_raises_when_the_route_file_is_unreadable(self):
        exc = T.TerminalCommitError("transaction-conflict", "fixture-detail")
        missing_route = self.root / "does-not-exist.json"
        evidence = T.terminal_commit_error_evidence(
            exc, route_file=missing_route, route_id="rt-whatever", owner_attempt_id="att-whatever")
        self.assertEqual(evidence["code"], "transaction-conflict")
        self.assertEqual(evidence["detail"], "fixture-detail")
        self.assertNotIn("route_file", evidence)
        self.assertNotIn("terminal_slot", evidence)

    def test_detail_is_truncated_to_240_chars(self):
        exc = T.TerminalCommitError("recovery-unavailable", "x" * 5000)
        evidence = T.terminal_commit_error_evidence(
            exc, route_file=self.route_file, route_id=self.route["route_id"],
            owner_attempt_id="att-a3fixture")
        self.assertEqual(len(evidence["detail"]), 240)


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


class HeldAdmissionLockTest(unittest.TestCase):
    """A producer step that meets a busy admission lock is a recoverable finalize failure.

    The settle path turns `producer-finalize-failed` into `recoverable` (the replay tests above),
    so the reaper, join or an explicit `finish` try again; this pins the lock side with a real flock.
    """

    HOLDER = ("import fcntl, os, sys, time\n"
              "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
              "fcntl.flock(fd, fcntl.LOCK_EX)\n"
              "print('held', flush=True)\n"
              "time.sleep(30)\n")

    def test_finalize_under_a_held_admission_lock_is_recoverable_not_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = artifact_admission._lock_file_path(root)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            holder = subprocess.Popen([sys.executable, "-c", self.HOLDER, str(lock_path)],
                                      stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "held")
                probe = os.open(str(lock_path), os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(probe)
                with self.assertRaises(T.TerminalCommitError) as caught:
                    T._producer_operation(artifact_admission._acquire_lock, root, 0.3)
                self.assertEqual(caught.exception.code, "producer-finalize-failed")
            finally:
                holder.kill()
                holder.wait()
                holder.stdout.close()
            fd = T._producer_operation(artifact_admission._acquire_lock, root, 0.3)  # the retry succeeds
            artifact_admission._release_lock(root, fd)


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

    def _replay(self, owner):
        with mock.patch.object(T, "prove_terminal_authority", return_value=T.TerminalProof("proved")), \
             mock.patch.object(T, "validate_owner_route", return_value=owner), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=False), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = self.gates
            return T.settle_terminal_commit(self.request(), T.TerminalCommitServices())

    def test_primary_content_change_after_seal_is_news_beside_the_stored_envelope(self):
        """§45 D-127: the sealed envelope is delivered as stored; an edited report is information."""
        owner = self._seal_once()
        stored = (self._slot() / "owner-envelope.txt").read_text()
        # Mutate the sealed primary artifact's bytes after sealing.
        self.artifact.write_text("tampered after seal\n", encoding="utf-8")
        second = self._replay(owner)
        self.assertEqual((second.result, second.reason), ("completed", None))
        self.assertEqual(second.detail, "primary-changed-after-seal")
        self.assertEqual(second.envelope_text, stored)
        # A report that went away is the other piece of news.
        self.artifact.unlink()
        third = self._replay(owner)
        self.assertEqual((third.result, third.detail), ("completed", "primary-missing-after-seal"))
        self.assertEqual(third.envelope_text, stored)

    def test_a_tampered_stored_envelope_is_still_a_conflict(self):
        owner = self._seal_once()
        (self._slot() / "owner-envelope.txt").write_text("artifact: elsewhere\nverdict: PASS\nblocker: none\n")
        second = self._replay(owner)
        self.assertEqual((second.result, second.reason, second.detail),
                         ("recoverable", "transaction-conflict", "envelope-content-mismatch"))


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
        self.assertEqual(T._proof_failure("producer-binding-mismatch").reason, "producer-binding-mismatch")


def _open_cycle_fixture(root, route, cycle_id):
    cycle_dir = root / ".runtime/artifact-producer/v1/cycles"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    filler = (cycle_id.rsplit("_", 1)[-1] + "0" * 32)[:32]
    (cycle_dir / f"{cycle_id}.json").write_text(json.dumps({
        "route_id": route["route_id"], "route_hash": route["route_hash"],
        "cycle_id": cycle_id, "state": "open",
        "campaign_id": "camp_" + filler, "producer_id": "prod_" + filler,
    }), encoding="utf-8")


class LauncherPreparedCycleBindingTest(_TerminalCommitFixture):
    """R1 (2026-09-27 owner decision, D1): a runtime-v1 launcher's
    `prepare_route_artifact_env(start=True)` opens the route's cycle before
    the owner attempt row exists, so the owner's own `producer.begin()` never
    runs its "publish immediately after begin" step (PRD SS13.36.2(1)).

    At this call site the pre-existing outer `except TerminalCommitError`
    in `settle_terminal_commit` already turned the resulting
    `load_producer_binding` failure into a durably-recorded `ineligible`
    result (E1: `08:12:12 reason=producer-binding-required` was recorded, not
    a crash) -- the route/cycle then simply never closed, because nothing
    else in that call chain could write the missing binding (D1's root
    cause). The actual uncaught crash (E3) happened one layer up, in
    `claude-session-supervisor.py`'s `prepare_cleanup_handoff`, which is
    covered separately in `claude_session_supervisor.test.py`.
    `test_missing_binding_without_the_fix_stays_ineligible_forever` pins the
    pre-fix baseline at this layer; `test_missing_binding_is_self_healed_not_raised`
    is the fixed behavior.
    """

    def setUp(self):
        super().setUp()
        self.cycle_id = "cyc_" + "e" * 32
        _open_cycle_fixture(self.root, self.route, self.cycle_id)
        self.identity_patch = mock.patch.object(
            T.artifact_lifecycle, "read_root_identity",
            return_value=SimpleNamespace(repository_id="repo_x", artifact_root_id="root_y"))
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)
        self.owner = owner_route_binding.OwnerRouteBinding(
            str(self.route_file), self.route["route_id"], self.route["route_hash"])

    def _settle(self, services):
        with mock.patch.object(T, "prove_terminal_authority", return_value=T.TerminalProof("proved")), \
             mock.patch.object(T, "validate_owner_route", return_value=self.owner), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=True), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = self.gates
            return T.settle_terminal_commit(self.request(), services)

    def test_missing_binding_is_ineligible_without_settlement_write(self):
        binding_path = T.producer_binding_path(self.root, self.route["route_id"], "att-a3fixture")
        self.assertFalse(binding_path.exists())
        services, _ = self.services()
        result = self._settle(services)
        self.assertEqual((result.result, result.reason), ("ineligible", "producer-binding-required"))
        self.assertFalse(binding_path.exists())
        cycles = list((self.root / ".runtime/artifact-producer/v1/cycles").glob("*.json"))
        self.assertEqual(len(cycles), 1)


class SettleOwnerCompletionLauncherShapeTest(_TerminalCommitFixture):
    """Settlement does not publish a binding, even on the owner path."""

    def setUp(self):
        super().setUp()
        self.cycle_id = "cyc_" + "f" * 32
        _open_cycle_fixture(self.root, self.route, self.cycle_id)
        self.identity_patch = mock.patch.object(
            T.artifact_lifecycle, "read_root_identity",
            return_value=SimpleNamespace(repository_id="repo_x", artifact_root_id="root_y"))
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)
        lines = self.jobs.read_text().splitlines()
        for index, line in enumerate(lines):
            fields = line.split("\t")
            meta = T.dispatch_contract.parse_registry_metadata(fields[5])
            if meta.get("attempt_id") != "att-a3fixture":
                continue
            meta.update(attempt_schema_version="2", execution_surface="registered-headless",
                        transport="headless", fallback_hop="same-harness-headless",
                        failure_class="pass", note="completed-supervisor",
                        workflow_completion="runtime-v1", launch_outcome="reaped-before-publish")
            fields[1] = "done"
            fields[5] = ",".join(f"{k}={v}" for k, v in meta.items())
            lines[index] = "\t".join(fields)
        self.jobs.write_text("\n".join(lines) + "\n")
        from dispatch_completion_join import exact_attempt_row
        self.owner_meta = exact_attempt_row(self.jobs, "att-a3fixture").metadata

    def test_launcher_shape_does_not_write_binding(self):
        binding_path = T.producer_binding_path(self.root, self.route["route_id"], "att-a3fixture")
        self.assertFalse(binding_path.exists())
        result = T.settle_owner_completion(self.jobs, "done", self.owner_meta)
        self.assertIsNotNone(result)
        self.assertNotEqual(result.result, "completed", result)
        self.assertFalse(binding_path.exists())
        self.assertEqual(len(list((self.root / ".runtime/artifact-producer/v1/cycles").glob("*.json"))), 1)


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

    def test_a_completed_deferred_quick_owner_row_is_accepted(self):
        """C8 (S3a): the `done` branch's atomic pass check now goes through
        `verdict_pass`, so a marker-bound deferred owner row (failure_class
        stays `infrastructure`, only note/completion_marker prove success) is
        accepted instead of refused as `quick-owner-axes`."""
        node_id, worker_type, attempt = "one-shot", "owner", "att-quick-deferred"
        self.registered_row(node_id, worker_type, attempt)
        deferred_row = self.jobs.read_text(encoding="utf-8").rstrip("\n").replace(
            "\topen\t", "\tdone\t",
        ) + (",note=completed-marker,failure_class=infrastructure"
             ",classifier_source=registered-wrapper-completion-transient-v1"
             ",completion_marker=/artifacts/.runtime/completions/one-shot.json") + "\n"
        self.jobs.write_text(deferred_row, encoding="utf-8")
        owner = T.validate_owner_route(jobs=self.jobs, route_file=self.route_file,
                                       owner_attempt_id=attempt)
        self.assertEqual(owner.route_id, self.route["route_id"])

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


class OwnerCompletionStateTest(_TerminalCommitFixture):
    """plan.md item 8: `owner_completion_state` types the not-claimed-checkpoint
    finishing gate instead of collapsing every non-complete case into the old
    unconditional `owner_completion_pending() -> True`."""

    def _metadata(self, **overrides):
        meta = {
            "workflow_completion": "runtime-v1", "worker_type": "owner",
            "dispatch_depth": "1", "attempt_id": "att-a3fixture",
            "failure_class": "pass",
        }
        meta.update(overrides)
        return meta

    def test_owner_completion_judgment_runs_inside_a_passive_registry_scope(self):
        import dispatch_registry_cache
        observed = []

        def scoped(_jobs, _status, _metadata):
            observed.append(dispatch_registry_cache.registry_scope_active())
            return T.CompletionState("not-applicable")

        with mock.patch.object(T, "_owner_completion_state_scoped", side_effect=scoped):
            result = T.owner_completion_state(self.jobs, "done", self._metadata())
        self.assertEqual(result.state, "not-applicable")
        self.assertEqual(observed, [True])

    def test_completion_state_blocked_on_attempt_not_current(self):
        # A later attempt already claimed this route node -- this attempt can
        # never complete, so the caller must stop waiting on it, not retry
        # forever the way a bare `pending`/True would.
        gates = {"execute": {"passed": False, "reason": "completion-attempt-not-current"}}
        with mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = gates
            state = T.owner_completion_state(self.jobs, "done", self._metadata())
            self.assertEqual((state.state, state.reason),
                              ("blocked", "completion-attempt-not-current"))
            self.assertFalse(T.owner_completion_pending(self.jobs, "done", self._metadata()))

    def test_completion_state_evidence_revised_unrecorded_is_blocked(self):
        # SD-154 I-5: the retired `completion-evidence-hash-mismatch` reason is
        # `completion-evidence-revised-unrecorded` now (owner recovers with
        # `capability-route.py revise`, not by treating the row as a dead end).
        gates = {"execute": {"passed": False, "reason": "completion-evidence-revised-unrecorded"}}
        with mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = gates
            state = T.owner_completion_state(self.jobs, "done", self._metadata())
        self.assertEqual((state.state, state.reason),
                          ("blocked", "completion-evidence-revised-unrecorded"))

    def test_completion_state_marker_absent_stays_pending(self):
        # Cannot be proven permanent -- the marker may simply not be written
        # yet, so this must keep retrying, not be reported as a dead end.
        gates = {"execute": {"passed": False, "reason": "completion-marker-absent"}}
        with mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = gates
            state = T.owner_completion_state(self.jobs, "done", self._metadata())
            self.assertEqual((state.state, state.reason),
                              ("pending", "completion-marker-absent"))
            self.assertTrue(T.owner_completion_pending(self.jobs, "done", self._metadata()))

    def test_completion_state_not_applicable_for_non_runtime_v1_row(self):
        state = T.owner_completion_state(
            self.jobs, "done", self._metadata(workflow_completion="")
        )
        self.assertEqual(state.state, "not-applicable")

    def test_not_yet_proven_gate_reasons_stay_pending(self):
        # F3b: the readiness/fence vocabulary added by this change
        # (registry-unreadable, marker-attempt-not-terminal,
        # marker-attempt-failure-class-not-pass) must never be a member of
        # `_PROVEN_BLOCKED_GATE_REASONS` -- only a later-attempt fence or an
        # evidence-hash mismatch may prove a permanent block (plan §4).
        for reason in ("registry-unreadable", "marker-attempt-not-terminal",
                       "marker-attempt-failure-class-not-pass"):
            with self.subTest(reason=reason):
                gates = {"execute": {"passed": False, "reason": reason}}
                with mock.patch.object(T, "_route_module") as route_module:
                    route_module.return_value.terminal_gate_observation.return_value = gates
                    state = T.owner_completion_state(self.jobs, "done", self._metadata())
                self.assertEqual(state.state, "pending")
                self.assertEqual(state.reason, reason)
        self.assertEqual(T._PROVEN_BLOCKED_GATE_REASONS,
                         {"completion-attempt-not-current", "completion-evidence-revised-unrecorded"})

    def test_no_commit_gate_receives_complete_snapshot_and_rejects_stale_selection(self):
        import dispatch_registry_cache as registry

        owner_line = ("job\tdone\t/repo\t/wt\towner\t"
                      "attempt_id=att-a,attempt_schema_version=2,worker_type=owner\n")
        prerequisite_line = ("job\tdone\t/repo\t/wt\tprerequisite\t"
                             "attempt_id=att-prereq,attempt_schema_version=2\n")
        self.jobs.write_text(owner_line + prerequisite_line, encoding="utf-8")
        rows = registry.registry_view(self.jobs).select_rows(attempt_id="att-a")
        request = SimpleNamespace(route_file=self.route_file, jobs=self.jobs)
        self.route_file.write_text(json.dumps(self.route), encoding="utf-8")
        with mock.patch.object(T, "_historical_request", return_value=request), \
             mock.patch.object(T, "_completion_request", return_value=request), \
             mock.patch.object(T, "_commit_state_path", return_value=self.root / "missing-commit.json"), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = {
                "execute": {"passed": True}}
            state = T.owner_completion_state(
                self.jobs, "done", self._metadata(attempt_id="att-a"),
                selected_registry_rows=rows)
            self.assertEqual(state.state, "pending")
            kwargs = route_module.return_value.terminal_gate_observation.call_args.kwargs
            snapshot = kwargs["registry_snapshot"]
            self.assertTrue(snapshot.complete)
            self.assertEqual(len(snapshot.view.records), 2,
                             "the bounded owner row must not stand in for the full registry")
            self.assertEqual(snapshot.lines, (owner_line.rstrip("\n"), prerequisite_line.rstrip("\n")))

            stale_rows = [(list(rows[0][0]), dict(rows[0][1]))]
            stale_rows[0][0][1] = "open"
            changed = T.owner_completion_state(
                self.jobs, "done", self._metadata(attempt_id="att-a"),
                selected_registry_rows=stale_rows)
            self.assertEqual(changed.state, "unknown")
            self.assertEqual(changed.reason, "registry-generation-changed")
            self.assertEqual(route_module.return_value.terminal_gate_observation.call_count, 1)

    def test_owner_classifications_share_one_read_per_readiness_pass(self):
        import dispatch_registry_cache as registry

        def row(attempt):
            return (f"job\tdone\t/repo\t/wt\towner\tattempt_id={attempt},"
                    "attempt_schema_version=2,worker_type=owner\n")

        self.jobs.write_text(row("att-a") + row("att-b"), encoding="utf-8")
        self.route_file.write_text(json.dumps(self.route), encoding="utf-8")
        request = SimpleNamespace(route_file=self.route_file, jobs=self.jobs)
        metadata = [self._metadata(attempt_id=attempt) for attempt in ("att-a", "att-b")]
        selections = [registry.registry_view(self.jobs).select_rows(
            attempt_id=item["attempt_id"]) for item in metadata]
        with mock.patch.object(T, "_historical_request", return_value=request), \
             mock.patch.object(T, "_completion_request", return_value=request), \
             mock.patch.object(T, "_commit_state_path", return_value=self.root / "missing-commit.json"), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = {
                "execute": {"passed": True}}
            registry.reset_metrics()
            for item, selected in zip(metadata, selections):
                self.assertEqual(T.owner_completion_state(
                    self.jobs, "done", item, selected_registry_rows=selected).state,
                    "pending")
            repeated_reads = registry.metrics_snapshot()["content_reads"]
            self.assertEqual(repeated_reads, 2,
                             "separate standalone classifications each acquire their own generation")

            registry.reset_metrics()
            snapshots = []
            route_module.return_value.terminal_gate_observation.side_effect = (
                lambda _route, **kwargs: (
                    snapshots.append(kwargs["registry_snapshot"])
                    or {"execute": {"passed": True}}))
            with registry.registry_read_scope():
                pass_view = registry.registry_view(self.jobs)
                for item in metadata:
                    selected = pass_view.select_rows(attempt_id=item["attempt_id"])
                    self.assertEqual(T.owner_completion_state(
                        self.jobs, "done", item, selected_registry_rows=selected).state,
                        "pending")
            self.assertEqual(registry.metrics_snapshot()["content_reads"], 1)
            self.assertEqual(len(snapshots), 2)
            self.assertTrue(all(snapshot.view is pass_view for snapshot in snapshots))
            self.assertEqual(len({snapshot.signature for snapshot in snapshots}), 1)

    def test_registry_change_after_selection_keeps_gate_pinned_then_requires_fresh_generation(self):
        import dispatch_registry_cache as registry
        import shared_readiness

        owner_line = ("job\tdone\t/repo\t/wt\towner\t"
                      "attempt_id=att-a,attempt_schema_version=2,worker_type=owner\n")
        self.jobs.write_text(owner_line, encoding="utf-8")
        self.route_file.write_text(json.dumps(self.route), encoding="utf-8")
        request = SimpleNamespace(route_file=self.route_file, jobs=self.jobs)
        snapshots = []
        with mock.patch.object(T, "_historical_request", return_value=request), \
             mock.patch.object(T, "_completion_request", return_value=request), \
             mock.patch.object(T, "_commit_state_path", return_value=self.root / "missing-commit.json"), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.side_effect = (
                lambda _route, **kwargs: (
                    snapshots.append(kwargs["registry_snapshot"])
                    or {"execute": {"passed": True}}))
            registry.reset_metrics()
            with registry.registry_read_scope():
                selected_view = registry.registry_view(self.jobs)
                selected = selected_view.select_rows(attempt_id="att-a")
                with self.jobs.open("a", encoding="utf-8") as stream:
                    stream.write("job\topen\t/repo\t/wt\tother\t"
                                 "attempt_id=att-prereq,attempt_schema_version=2\n")
                state = T.owner_completion_state(
                    self.jobs, "done", self._metadata(attempt_id="att-a"),
                    selected_registry_rows=selected)
                self.assertEqual(state.state, "pending")
                self.assertIs(snapshots[-1].view, selected_view)
                self.assertEqual(snapshots[-1].lines, selected_view.lines)
                self.assertIsNone(shared_readiness.registry_generation(
                    self.jobs, snapshots[-1].view, selected=True))
            fresh = registry.registry_snapshot(self.jobs)
            self.assertNotEqual(fresh.signature, snapshots[-1].signature)
            self.assertIsNotNone(shared_readiness.registry_generation(
                self.jobs, fresh.view, selected=True))

    def test_incomplete_or_unreadable_registry_cannot_prove_no_commit_gate(self):
        request = SimpleNamespace(route_file=self.route_file, jobs=self.jobs)
        self.route_file.write_text(json.dumps(self.route), encoding="utf-8")
        with mock.patch.object(T, "_historical_request", return_value=request), \
             mock.patch.object(T, "_completion_request", return_value=request), \
             mock.patch.object(T, "_commit_state_path", return_value=self.root / "missing-commit.json"), \
             mock.patch.object(T, "_route_module") as route_module:
            for raw in (b"malformed\n", b"job\tdone\t/repo\t/wt\towner\tpartial",
                        b"job\tdone\t/repo\t/wt\towner\tvalue=\xff\n"):
                with self.subTest(raw=raw):
                    self.jobs.write_bytes(raw)
                    state = T.owner_completion_state(
                        self.jobs, "done", self._metadata())
                    self.assertEqual(state.state, "unknown")
            self.jobs.unlink()
            state = T.owner_completion_state(self.jobs, "done", self._metadata())
            self.assertEqual(state.state, "unknown")
            route_module.return_value.terminal_gate_observation.assert_not_called()


class CompletionRequestDeferredGuardTest(unittest.TestCase):
    """C10 (S3a): `_completion_request`'s atomic pass check via `verdict_pass`."""

    def test_pending_deferred_owner_is_rejected_by_the_guard(self):
        metadata = {
            "workflow_completion": "runtime-v1", "worker_type": "owner",
            "dispatch_depth": "1", "attempt_id": "att-owner-deferred",
            "note": "completion-deferred", "failure_class": "infrastructure",
            "classifier_source": "registered-wrapper-completion-transient-v1",
        }
        self.assertIsNone(T._completion_request(Path("/nonexistent/jobs.log"), "done", metadata))

    def test_completed_deferred_owner_passes_the_guard(self):
        # The guard is the only thing under test -- once past it, prove that
        # by asserting the next real step (route resolution) was reached,
        # instead of building the rest of the owner/route plumbing here.
        metadata = {
            "workflow_completion": "runtime-v1", "worker_type": "owner",
            "dispatch_depth": "1", "attempt_id": "att-owner-deferred",
            "note": "completed-marker", "failure_class": "infrastructure",
            "classifier_source": "registered-wrapper-completion-transient-v1",
            "completion_marker": "/artifacts/.runtime/completions/one-shot.json",
        }
        with mock.patch.object(
            owner_route_binding, "resolve_owner_route_lifecycle",
            side_effect=RuntimeError("reached-past-the-guard"),
        ):
            with self.assertRaises(RuntimeError):
                T._completion_request(Path("/nonexistent/jobs.log"), "done", metadata)


class ProveRouteChildrenDeferredTest(unittest.TestCase):
    """C9 (S3a): a marker-bound deferred child node row satisfies terminal proof."""

    def _request(self, jobs):
        return T.TerminalCommitRequest(Path("/route.json"), "att-owner", jobs, Path("/root"))

    def _child_row(self, jobs, route_id, node_id, attempt_id, metadata_extra):
        meta = {
            "attempt_id": attempt_id, "route_id": route_id, "route_node": node_id,
        }
        meta.update(metadata_extra)
        pipe = ",".join(f"{k}={v}" for k, v in meta.items())
        jobs.write_text(f"2026-09-24T00:00:00Z\tdone\t/w\t/w\t{node_id}\t{pipe}\n", encoding="utf-8")

    def test_completed_deferred_child_satisfies_terminal_proof(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            route_hash = "sha256:" + "e" * 64
            route = {"route_id": "rt-children-deferred", "route_hash": route_hash,
                     "nodes": [{"id": "execute", "terminal": True}]}
            self._child_row(jobs, route["route_id"], "execute", "att-execute-deferred", {
                "route_hash": route_hash,
                "attempt_schema_version": "2", "dispatch_depth": "2", "transport": "headless",
                "execution_surface": "registered-headless", "registered_worker": "1",
                "fallback_hop": "same-harness-headless", "parent_attempt_id": "att-owner",
                "note": "completed-marker", "failure_class": "infrastructure",
                "classifier_source": "registered-wrapper-completion-transient-v1",
                "completion_marker": "/artifacts/.runtime/completions/execute.json",
                "launch_outcome": "reaped-before-publish",
            })
            # Canonicalized to the real exact-terminal gate shape (plan §3
            # Step 4.1): the terminal node loop now trusts `gate["passed"]`
            # instead of reimplementing a route_id/route_node scan.
            gates = {"execute": {"passed": True, "current": True,
                                 "attempt_id": "att-execute-deferred",
                                 "attempt_readiness": "quiescent"}}
            proof = T._prove_route_children(self._request(jobs), route, gates)
        self.assertEqual(proof.status, "proved")

    def test_pending_deferred_child_is_not_terminal_pass(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            route_hash = "sha256:" + "e" * 64
            route = {"route_id": "rt-children-pending", "route_hash": route_hash,
                     "nodes": [{"id": "execute", "terminal": True}]}
            self._child_row(jobs, route["route_id"], "execute", "att-execute-pending", {
                "route_hash": route_hash,
                "attempt_schema_version": "2", "dispatch_depth": "2", "transport": "headless",
                "execution_surface": "registered-headless", "registered_worker": "1",
                "fallback_hop": "same-harness-headless", "parent_attempt_id": "att-owner",
                "note": "completion-deferred", "failure_class": "infrastructure",
                "classifier_source": "registered-wrapper-completion-transient-v1",
                "launch_outcome": "reaped-before-publish",
            })
            # The pending gate shape is derived from readiness itself, not
            # invented, so the fixture cannot silently drift from the real
            # exact-terminal gate contract.
            marker = {"attempt_id": "att-execute-pending", "registered_worker": True}
            readiness = T.dispatch_contract.completion_attempt_readiness(
                route, route["nodes"][0], marker, jobs)
            self.assertEqual((readiness.state, readiness.reason),
                             ("unverifiable", "marker-attempt-not-terminal"))
            gates = {"execute": {"passed": False, "reason": readiness.reason,
                                 "attempt_id": "att-execute-pending"}}
            proof = T._prove_route_children(self._request(jobs), route, gates)
        self.assertEqual(proof.status, "rejected")
        self.assertEqual(proof.detail, "terminal-attempt-not-pass")


class OwnerTerminalPlacementReplayTest(unittest.TestCase):
    """A loose owner report stays where it was written and keeps one terminal identity."""

    NATIVE = {
        "claude": lambda text: [{"type": "result", "subtype": "success", "is_error": False, "result": text}],
        "codex": lambda text: [{"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                               {"type": "turn.completed"}],
        "opencode": lambda text: [{"type": "text", "sessionID": "ses_test", "part": {"type": "text", "text": text}},
                                  {"type": "step_finish", "sessionID": "ses_test",
                                   "part": {"type": "step-finish", "reason": "stop"}}],
    }

    def _settled(self, harness, rel):
        terminal = T
        from dispatch_completion_join import exact_attempt_row
        fixture = PRODUCER_FIXTURE.TerminalTransactionIntegrationTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        route, path, jobs, owner, cycle, review, request = fixture._prepare_fixture(harness, "autopilot-spec")
        report = fixture.write_output(cycle, rel=rel, data=b"verified transaction report\n")
        text = f"artifact: {report}\nverdict: PASS\nblocker: none"
        log = jobs.parent / "owner.jsonl"
        log.write_text("\n".join(json.dumps(r) for r in self.NATIVE[harness](text)) + "\n")
        jobs.write_text(jobs.read_text().replace(
            "worker_type=owner", f"attempt_schema_version=2,worker_type=owner,log_file={log},workflow_completion=runtime-v1"))
        fixture._closed_owner(jobs, owner)
        meta = exact_attempt_row(jobs, owner).metadata
        return fixture, terminal, route, path, jobs, owner, report, meta

    def test_first_finalize_refusal_reaches_inspect_and_late_completion_notice(self):
        import dispatch_supervision as supervision
        from dispatch_completion_join import exact_attempt_row

        fixture = PRODUCER_FIXTURE.TerminalTransactionIntegrationTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        route, route_file, jobs, owner, cycle, _artifact, request = fixture._prepare_fixture("codex")
        # The marker must be absent so the ordinary invalid-locator validator
        # remains responsible for this first close attempt.
        conda = Path(cycle["cycle_dir"]) / "artifacts" / "environment" / "conda-meta"
        conda.mkdir(parents=True)
        bad = conda / "bad|name.txt"
        bad.write_bytes(b"preserve invalid locator payload")
        self.assertFalse((conda / "history").exists())

        log = jobs.parent / "owner.jsonl"
        text = f"artifact: {bad}\nverdict: PASS\nblocker: none"
        log.write_text(json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": text}}) + "\n" +
            json.dumps({"type": "turn.completed"}) + "\n", encoding="utf-8")
        jobs.write_text(jobs.read_text().replace("worker_type=owner",
            f"attempt_schema_version=2,worker_type=owner,log_file={log},workflow_completion=runtime-v1"))
        fixture._closed_owner(jobs, owner)
        meta = exact_attempt_row(jobs, owner).metadata
        before_bad = bad.read_bytes()

        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs),
                                          "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
            refused = T.settle_terminal_commit(request)
            self.assertEqual((refused.result, refused.reason), ("recoverable", "producer-finalize-failed"), refused)
            attempts = T._commit_state_path(request).parent / "attempts"
            records = [json.loads(path.read_text(encoding="utf-8")) for path in attempts.glob("*.json")]
            record = next(row for row in records if row.get("reason") == "producer-finalize-failed")
            self.assertIn("output-invalid: locator-invalid-component:artifacts/environment/conda-meta/bad|name.txt",
                          record["detail"])
            diagnosis = T.inspect_owner_completion(jobs, "done", meta)
            self.assertNotEqual(diagnosis.get("checkpoint"), "file-count-limit", diagnosis)
            refusal = diagnosis.get("last_refusal")
            self.assertIsInstance(refusal, dict, diagnosis)
            self.assertEqual(refusal["reason"], "producer-finalize-failed")
            self.assertIn("output-invalid: locator-invalid-component:artifacts/environment/conda-meta/bad|name.txt",
                          refusal["detail"])
            self.assertTrue(Path(refusal["record_path"]).is_file())
            self.assertEqual(Path(refusal["record_path"]), next(attempts.glob("*.json")))
            self.assertIn("Correct the reported locator", refusal["remediation"])

            notices = supervision.materialize(jobs, {owner}, reason="workflow-completion-pending")
            receipt = notices[0]["receipt"]
            receipt_digest = supervision.receipt_digest(receipt)
            notice = supervision.render_text(receipt)
            self.assertIn("output-invalid", notice)
            self.assertIn("conda-meta/bad|name.txt", notice)
            self.assertIn(Path(refusal["record_path"]).name, notice)
            self.assertIn("Correct the reported locator", notice)
            self.assertIn("retries closure automatically", notice)
            self.assertEqual(supervision.receipt_digest(receipt), receipt_digest)
        self.assertEqual(bad.read_bytes(), before_bad)

    def test_a_replayed_settlement_after_a_checkpoint_completes_for_every_harness(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                fixture, terminal, route, path, jobs, owner, report, meta = self._settled(harness, "owner-report.md")
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
                    first = terminal.settle_owner_completion(jobs, "done", meta)
                    self.assertEqual(first.result, "completed", first)
                    self.assertTrue(report.exists(), "nothing moves the loose report")
                    request = terminal.TerminalCommitRequest(path, owner, jobs, fixture.root)
                    state_path = terminal._commit_state_path(request)
                    stored = json.loads(state_path.read_text())
                    envelope = json.loads((state_path.parent / "owner-envelope.json").read_text())
                    producer_binding = terminal.load_producer_binding(
                        artifact_root=fixture.root, route_id=route["route_id"], owner_attempt_id=owner).binding
                    cycle_record = artifact_producer.read_cycle_record(fixture.root, producer_binding["cycle_id"])
                    cycle_dir = artifact_producer.cycle_dir(fixture.root, cycle_record["campaign_id"],
                                                            producer_binding["cycle_id"], cycle_record)
                    official = cycle_dir / "artifacts/spec/prd.md"
                    self.assertEqual(envelope["primary_path"], str(official.resolve()))
                    self.assertEqual(first.shared_publication["status"], "admitted", first)
                    manifest = json.loads((cycle_dir / "manifest.json").read_text())
                    primary_rows = [row for row in manifest["artifacts"] if row.get("role") == "primary"]
                    self.assertEqual(len(primary_rows), 1)
                    primary_id = primary_rows[0]["artifact_id"]
                    primary_revision = next(row for row in manifest["artifact_revisions"]
                                            if row["artifact_id"] == primary_id)
                    self.assertEqual(primary_revision["locator"]["path"],
                                     "artifacts/spec/prd.md")
                    handoff = terminal.completed_owner_handoff(jobs, "done", meta)
                    self.assertIn(f"artifact: {official.resolve()}", handoff)
                    import work_start
                    consumed = work_start._outcome(jobs, owner)
                    self.assertIn("handoff", consumed)
                    self.assertIn(f"artifact: {official.resolve()}", consumed["handoff"])
                    self.assertEqual(consumed["shared_publication"]["shared_reference_revision_id"],
                                     first.shared_publication["shared_reference_revision_id"])
                    self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).state, "complete")
                    second = terminal.settle_owner_completion(jobs, "done", meta)
                    self.assertEqual(second.result, "completed", second)
                    self.assertEqual(json.loads(state_path.read_text())["terminal_commit_id"], stored["terminal_commit_id"])
                    row = ROUTE.terminal_gate_observation(route, jobs=jobs, exact_terminal=True)["prd-transaction"]
                    self.assertTrue(row["passed"], row)
                    self.assertEqual(row["evidence"], str(report))  # identity stays the worker's own locator
                    self.assertEqual(row["evidence_digest"], ROUTE.evidence_digest(report))
                    self.assertFalse((ROUTE.completion_dir(route["route_id"], jobs=jobs) / "prd-transaction.json").exists())

    def test_settled_completion_does_not_depend_on_progress_ledger(self):
        import workflow_state
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                fixture, terminal, route, path, jobs, owner, report, meta = self._settled(harness, "owner-report.md")
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
                    request = terminal._completion_request(jobs, "done", meta)
                    # Seal the real outcome, cycle and envelope before the
                    # workflow controller has published its COMPLETE suffix.
                    self.assertEqual(terminal.settle_terminal_commit(request).result, "completed")
                    ledger = workflow_state.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
                    self.assertNotEqual(ledger.read_only_state()["workflow_state"], "COMPLETE")
                    before = ledger.journal()
                    self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).state, "complete")
                    self.assertEqual(ledger.journal(), before)
                    self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).state, "complete")

    def test_settled_history_reuses_its_producer_proof_until_an_input_changes(self):
        fixture, terminal, route, path, jobs, owner, report, meta = self._settled("claude", "owner-report.md")
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
            self.assertEqual(terminal.settle_owner_completion(jobs, "done", meta).result, "completed")
            artifact_producer._HISTORICAL_OBSERVATIONS.clear()
            artifact_producer._HISTORICAL_PROJECTIONS.clear()
            terminal._route_module.cache_clear()
            code_imports = []
            loader = importlib.util.spec_from_file_location
            def load_code(name, path, *args, **kwargs):
                if name == "terminal_capability_route":
                    code_imports.append(str(path))
                return loader(name, path, *args, **kwargs)
            verify = artifact_producer._verify_sealed_cycle_locked
            with mock.patch.object(artifact_producer, "_verify_sealed_cycle_locked", wraps=verify) as checked, \
                 mock.patch.object(importlib.util, "spec_from_file_location", side_effect=load_code):
                self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).state, "complete")
                first_count = checked.call_count
                self.assertGreater(first_count, 0)
                self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).state, "complete")
                self.assertEqual(checked.call_count, first_count)
                index_path = artifact_admission._index_path(fixture.root)
                original = index_path.read_bytes()
                index_path.write_bytes(b"{}")
                self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).state, "unknown")
                self.assertGreater(checked.call_count, first_count)
                index_path.write_bytes(original)
                self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).state, "complete")
            self.assertEqual(len(code_imports), 1, "code reuse must not suppress fresh data rejection/recovery")

    def test_historical_observer_protects_corruption_missing_duty_and_unsettled_child(self):
        fixture, terminal, route, path, jobs, owner, report, meta = self._settled("claude", "owner-report.md")
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
            self.assertEqual(terminal.settle_owner_completion(jobs, "done", meta).result, "completed")
            request = terminal.TerminalCommitRequest(path, owner, jobs, fixture.root)
            slot = terminal._commit_state_path(request).parent
            for target, damage in [(slot / "producer-binding.json", lambda raw: raw + b" "),
                    (slot / "owner-envelope.json", lambda raw: b"{}"),
                    (ROUTE.outcome_path(path), lambda raw: b"{}")]:
                raw = target.read_bytes()
                target.write_bytes(damage(raw))
                self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).state, "unknown", str(target))
                target.write_bytes(raw)
                self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).state, "complete")
            state_path = slot / "terminal-commit.json"
            original_state = state_path.read_bytes()
            state = json.loads(original_state)
            state["campaign_goal_duty"] = True
            state_path.write_text(json.dumps(state))
            self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).reason, "campaign-goal-close-pending")
            state_path.write_bytes(original_state)
            index_path = artifact_admission._index_path(fixture.root)
            index_bytes = index_path.read_bytes()
            index_path.unlink()
            with mock.patch.object(artifact_admission, "ensure_root_identity", side_effect=AssertionError("reader allocated identity")):
                self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).state, "unknown")
            self.assertFalse(index_path.exists())
            index_path.write_bytes(index_bytes)
            rows = jobs.read_bytes()
            jobs.write_text(jobs.read_text().replace("\tdone\t", "\topen\t", 1))
            self.assertNotEqual(terminal.owner_completion_state(jobs, "done", meta).state, "complete")
            jobs.write_bytes(rows)
            leases = artifact_producer._review_lease_dir(fixture.root,
                json.loads((slot / "producer-binding.json").read_text())["cycle_id"])
            leases.mkdir(parents=True, exist_ok=True)
            (leases / "unverifiable.json").write_text("corrupt unknown lease")
            self.assertNotEqual(terminal.owner_completion_state(jobs, "done", meta).state, "complete")

    def test_historical_inline_owner_marker_keeps_original_digest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs.log"
            path = root / "route.json"
            route = {"route_id": "rt-inline", "route_hash": "sha256:" + "a" * 64,
                "nodes": [{"id": "sync", "terminal": True, "terminal_gate": "lab-sync"}]}
            marker = {"route_id": route["route_id"], "route_hash": route["route_hash"],
                "node_id": "sync", "attempt_id": "att-owner-sync-inline", "completion_gate": "lab-sync",
                "execution_surface": "inline", "registered_worker": False,
                "evidence": {"sha256": "b" * 64, "path": str(root / "cleaned-report")}}
            raw = json.dumps(marker).encode()
            (root / "sync.json").write_bytes(raw)
            request = T.TerminalCommitRequest(path, "att-owner", jobs, root)
            module = mock.Mock()
            module.owner_executed_terminal.return_value = True
            module.completion_dir.return_value = root
            module.outcome_path.return_value = root / "missing-outcome"
            gates = T._settled_gate_rows(module, route, request, {"sync": {}}, historical=True)
            self.assertEqual(gates["sync"]["marker_digest"], T.hashlib.sha256(raw).hexdigest())
            self.assertEqual(gates["sync"]["attempt_id"], "att-owner-sync-inline")
            self.assertEqual(gates["sync"]["evidence_digest"], "b" * 64)
            self.assertEqual((root / "sync.json").read_bytes(), raw)

    def test_settled_worker_row_keeps_the_evidence_digest_its_closed_outcome_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs.log"
            path = root / "route.json"
            route = {"route_id": "rt-report-edit", "route_hash": "sha256:" + "a" * 64,
                "nodes": [{"id": "report", "terminal": True, "terminal_gate": "code-report"}]}
            marker = {"route_id": route["route_id"], "route_hash": route["route_hash"],
                "node_id": "report", "attempt_id": "att-report", "completion_gate": "code-report",
                "evidence": {"sha256": "b" * 64, "path": str(root / "final_report.md")}}
            raw = json.dumps(marker).encode()
            (root / "report.json").write_bytes(raw)
            recorded = {"passed": True, "current": True, "node_id": "report", "attempt_id": "att-report",
                "completion_gate": "code-report", "marker_digest": T.hashlib.sha256(raw).hexdigest(),
                "evidence_digest": "c" * 64, "evidence": str(root / "final_report.md")}
            outcome = root / "outcome.json"
            module = mock.Mock()
            module.owner_executed_terminal.return_value = False
            module.completion_dir.return_value = root
            module.outcome_path.return_value = outcome
            request = T.TerminalCommitRequest(path, "att-owner", jobs, root)
            cases = (
                ("edited-after-marker", recorded, "c" * 64),
                ("other-marker-bytes", {**recorded, "marker_digest": "d" * 64}, "b" * 64),
                ("other-attempt", {**recorded, "attempt_id": "att-other"}, "b" * 64),
            )
            for name, row, expected in cases:
                with self.subTest(name):
                    outcome.write_text(json.dumps({"route_id": route["route_id"], "route_hash": route["route_hash"],
                        "terminal_gate_proven": True, "terminal_gates": {"report": row}}))
                    for historical in (False, True):
                        gates = T._settled_gate_rows(module, route, request, {"report": {}}, historical=historical)
                        self.assertEqual(gates["report"]["evidence_digest"], expected)
                        self.assertEqual(gates["report"]["marker_digest"], T.hashlib.sha256(raw).hexdigest())
            outcome.unlink()
            gates = T._settled_gate_rows(module, route, request, {"report": {}})
            self.assertEqual(gates["report"]["evidence_digest"], "b" * 64)

    def test_historical_root_alias_cleaned_cwd_and_no_transitive_writes(self):
        fixture, terminal, route, path, jobs, owner, report, meta = self._settled("claude", "owner-report.md")
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
            self.assertEqual(terminal.settle_owner_completion(jobs, "done", meta).result, "completed")
            binding_path = terminal.producer_binding_path(fixture.root, route["route_id"], owner)
            raw = binding_path.read_bytes()
            old_root = fixture.root
            moved = old_root.with_name("moved-root")
            old_root.rename(moved)
            old_root.symlink_to(moved, target_is_directory=True)
            report.unlink()
            before = {str(p.relative_to(moved)): p.read_bytes() for p in moved.rglob("*") if p.is_file()}
            with mock.patch.object(artifact_admission, "_acquire_lock", side_effect=AssertionError("reader took lock")), \
                 mock.patch.object(artifact_producer, "refresh_cycle", side_effect=AssertionError("reader refreshed")):
                observed = terminal.owner_completion_state(jobs, "done", meta)
                self.assertEqual(observed.state, "complete", observed)
                self.assertIn("verdict: PASS", terminal.completed_owner_handoff(jobs, "done", meta))
            self.assertEqual(binding_path.read_bytes(), raw)
            self.assertEqual(before, {str(p.relative_to(moved)): p.read_bytes() for p in moved.rglob("*") if p.is_file()})
            request = terminal.TerminalCommitRequest(path, owner, jobs, moved)
            state = json.loads(terminal._commit_state_path(request).read_text())
            self.assertNotEqual(terminal._reverify_forward_recovery(request, state).status, "proved")
            slot = terminal._commit_state_path(request).parent
            goal = slot / "campaign-goal.json"
            goal.write_text(json.dumps({"status": "pending", "terminal_commit_id": state["terminal_commit_id"]}))
            self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).reason, "campaign-goal-close-pending")
            goal.unlink()
            envelope = slot / "owner-envelope.txt"
            envelope.write_text("corrupt")
            self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).state, "unknown")
            self.assertIsNone(terminal.completed_owner_handoff(jobs, "done", meta))

    def test_a_report_already_in_its_bucket_is_unchanged(self):
        fixture, terminal, route, path, jobs, owner, report, meta = self._settled("claude", "spec/_internal/owner-report.md")
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
            self.assertEqual(terminal.settle_owner_completion(jobs, "done", meta).result, "completed")
            self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).state, "complete")
            self.assertEqual(terminal.settle_owner_completion(jobs, "done", meta).result, "completed")

    def test_explicit_owner_handoff_primary_precedes_exact_owner_log_artifact(self):
        fixture, terminal, route, path, jobs, owner, report, meta = self._settled("claude", "owner-report.md")
        producer_binding = terminal.load_producer_binding(
            artifact_root=fixture.root, route_id=route["route_id"], owner_attempt_id=owner).binding
        cycle_record = artifact_producer.read_cycle_record(fixture.root, producer_binding["cycle_id"])
        cycle_dir = artifact_producer.cycle_dir(fixture.root, cycle_record["campaign_id"],
                                                producer_binding["cycle_id"], cycle_record)
        chosen = cycle_dir / "artifacts" / "plans" / "z-user-report.md"
        chosen.parent.mkdir(parents=True, exist_ok=True)
        chosen.write_bytes(b"explicit owner handoff\n")
        meta = {**meta, "owner_handoff": {"primary": str(chosen)}}
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
            result = terminal.settle_owner_completion(jobs, "done", meta)
            self.assertEqual(result.result, "completed", result)
            request = terminal._completion_request(jobs, "done", meta)
            envelope = json.loads((terminal._commit_state_path(request).parent / "owner-envelope.json").read_text())
        self.assertNotEqual(report.resolve(), chosen.resolve())
        self.assertEqual(envelope["primary_path"], str(chosen.resolve()))

    def test_private_cycle_hint_is_ignored_during_real_producer_settlement(self):
        fixture, terminal, route, path, jobs, owner, owner_report, meta = self._settled("claude", "owner-report.md")
        producer_binding = terminal.load_producer_binding(
            artifact_root=fixture.root, route_id=route["route_id"], owner_attempt_id=owner)
        original_primary = str(owner_report.resolve())
        terminal._atomic_json(producer_binding.path, {**producer_binding.binding, "primary": original_primary})
        cycle_record = artifact_producer.read_cycle_record(fixture.root, producer_binding.binding["cycle_id"])
        cycle_dir = artifact_producer.cycle_dir(fixture.root, cycle_record["campaign_id"],
                                                producer_binding.binding["cycle_id"], cycle_record)
        private = cycle_dir / ".cycle.json"
        self.assertTrue(private.is_file())
        meta = {**meta, "owner_handoff": {"primary": str(private)}}
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
            result = terminal.settle_owner_completion(jobs, "done", meta)
            self.assertEqual(result.result, "completed", result)
            request = terminal._completion_request(jobs, "done", meta)
            envelope = json.loads((terminal._commit_state_path(request).parent / "owner-envelope.json").read_text())
            binding = terminal.load_producer_binding(
                artifact_root=fixture.root, route_id=route["route_id"], owner_attempt_id=owner).binding
        self.assertEqual(binding.get("primary"), original_primary)
        envelope_primary = Path(envelope["primary_path"]).resolve()
        self.assertTrue(envelope_primary.is_relative_to((cycle_dir / "artifacts").resolve()),
                        f"envelope={envelope_primary} artifacts={cycle_dir / 'artifacts'}")
        manifest = json.loads((cycle_dir / "manifest.json").read_text())
        primary_rows = [row for row in manifest["artifacts"] if row.get("role") == "primary"]
        self.assertEqual(len(primary_rows), 1)
        primary_id = primary_rows[0]["artifact_id"]
        revision = next(row for row in manifest["artifact_revisions"] if row["artifact_id"] == primary_id)
        self.assertEqual(revision["locator"]["path"],
                         "artifacts/" + envelope_primary.relative_to(cycle_dir / "artifacts").as_posix())
        self.assertNotEqual(envelope["primary_path"], str(private.resolve()))

    def test_existing_exact_claim_without_local_state_replays_bound_primary(self):
        fixture, terminal, route, path, jobs, owner, original, meta = self._settled("claude", "owner-report.md")
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
            request = terminal._completion_request(jobs, "done", meta)
            interrupted = terminal.settle_terminal_commit(
                request, terminal.TerminalCommitServices(crash_after="claim-after"))
            self.assertEqual(interrupted.result, "recoverable")
            selected = terminal.load_producer_binding(
                artifact_root=fixture.root, route_id=route["route_id"], owner_attempt_id=owner).binding["primary"]
            claim = terminal.dispatch_contract.terminal_claim_observation(jobs, route["route_id"], owner)
            self.assertIsNotNone(claim)
            state_path = terminal._commit_state_path(request)
            state_path.unlink()
            # Resolve the cycle from the exact producer binding; the later hint
            # is a distinct, otherwise-valid payload artifact.
            binding = terminal.load_producer_binding(
                artifact_root=fixture.root, route_id=route["route_id"], owner_attempt_id=owner).binding
            cycle_record = artifact_producer.read_cycle_record(fixture.root, binding["cycle_id"])
            cycle_dir = artifact_producer.cycle_dir(
                fixture.root, cycle_record["campaign_id"], binding["cycle_id"], cycle_record)
            alternate = cycle_dir / "artifacts" / "alternate-owner-report.md"
            alternate.parent.mkdir(parents=True, exist_ok=True)
            alternate.write_text("later valid alternative\n")
            self.assertEqual(terminal._valid_cycle_primary(
                str(alternate), root=fixture.root, binding=binding), alternate.resolve())
            late_request = terminal.TerminalCommitRequest(
                path, owner, jobs, fixture.root, {"primary": str(alternate)})
            replay = terminal.settle_terminal_commit(late_request)
            self.assertEqual(replay.result, "completed", replay)
            envelope = json.loads((state_path.parent / "owner-envelope.json").read_text())
            binding_after = terminal.load_producer_binding(
                artifact_root=fixture.root, route_id=route["route_id"], owner_attempt_id=owner).binding
        self.assertEqual(binding_after["primary"], selected)
        self.assertEqual(Path(envelope["primary_path"]).name, Path(selected).name)
        self.assertNotEqual(envelope["primary_path"], str(alternate.resolve()))
        self.assertNotEqual(original.resolve(), alternate.resolve())

    def test_primary_binding_survives_crash_after_claim_and_exact_replay(self):
        fixture, terminal, route, path, jobs, owner, report, meta = self._settled("claude", "owner-report.md")
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
            request = terminal._completion_request(jobs, "done", meta)
            interrupted = terminal.settle_terminal_commit(
                request, terminal.TerminalCommitServices(crash_after="claim-after"))
            self.assertEqual((interrupted.result, interrupted.reason), ("recoverable", "recovery-unavailable"))
            binding = terminal.load_producer_binding(
                artifact_root=fixture.root, route_id=route["route_id"], owner_attempt_id=owner).binding
            official = Path(report).parent / "spec/prd.md"
            self.assertEqual(binding["primary"], str(official.resolve()))
            replay = terminal.settle_terminal_commit(request)
            self.assertEqual(replay.result, "completed", replay)
            handoff = terminal.completed_owner_handoff(jobs, "done", meta)
            # Admission also creates a shared PRD; the claimed cycle primary
            # above remains the owner handoff's representative on replay.
            placed = official
        self.assertIn(f"artifact: {placed.resolve()}", handoff)

    def test_a_changed_or_missing_report_is_history_after_settlement(self):
        for harness in ("claude",):   # the damage is to the report file, whatever shape the native result had
            for damage in ("bytes", "missing"):
                with self.subTest(harness=harness, damage=damage):
                    fixture, terminal, route, path, jobs, owner, report, meta = self._settled(harness, "owner-report.md")
                    with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
                        self.assertEqual(terminal.settle_owner_completion(jobs, "done", meta).result, "completed")
                        if damage == "bytes":
                            report.write_text("tampered after settlement")
                        else:
                            report.unlink()
                        self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).state, "complete")
                        replay = terminal.settle_owner_completion(jobs, "done", meta)
                        self.assertEqual(replay.result, "completed", replay)

    def test_finalize_and_admission_crash_replay_keep_one_primary_envelope_and_revision(self):
        for checkpoint in ("finalize-after", "admission-after"):
            with self.subTest(checkpoint=checkpoint):
                fixture, terminal, route, path, jobs, owner, report, meta = self._settled("codex", "spec/REPORT.md")
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
                    request = terminal._completion_request(jobs, "done", meta)
                    first = terminal.settle_terminal_commit(request, terminal.TerminalCommitServices(crash_after=checkpoint))
                    self.assertEqual(first.result, "recoverable", first)
                    outcome = ROUTE.outcome_path(path).read_bytes()
                    state = json.loads(terminal._commit_state_path(request).read_text())
                    commit_id = state["terminal_commit_id"]
                    binding = terminal.load_producer_binding(
                        artifact_root=fixture.root, route_id=route["route_id"], owner_attempt_id=owner).binding
                    cycle = artifact_producer.read_cycle_record(fixture.root, binding["cycle_id"])
                    directory = artifact_producer.cycle_dir(fixture.root, cycle["campaign_id"], cycle["cycle_id"], cycle)
                    manifest = (directory / "manifest.json").read_bytes()
                    source = {p: p.read_bytes() for p in (directory / "artifacts").rglob("*") if p.is_file()}
                    recovered = terminal.settle_owner_completion(jobs, "done", meta)
                    self.assertEqual(recovered.result, "completed", recovered)
                    self.assertEqual(recovered.shared_publication["status"], "admitted", recovered)
                    envelope_path = terminal._commit_state_path(request).parent / "owner-envelope.txt"
                    envelope = envelope_path.read_bytes()
                    retry = terminal.settle_owner_completion(jobs, "done", meta)
                    self.assertEqual(retry.shared_publication["shared_reference_revision_id"],
                                     recovered.shared_publication["shared_reference_revision_id"])
                    self.assertEqual(artifact_producer.list_references(fixture.root, "spec")[0]["revisions"],
                                     [recovered.shared_publication["shared_reference_revision_id"]])
                    self.assertEqual(json.loads(terminal._commit_state_path(request).read_text())["terminal_commit_id"], commit_id)
                    self.assertEqual(ROUTE.outcome_path(path).read_bytes(), outcome)
                    self.assertEqual((directory / "manifest.json").read_bytes(), manifest)
                    self.assertEqual(envelope_path.read_bytes(), envelope)
                    self.assertEqual(source, {p: p.read_bytes() for p in source})

    def test_admission_failure_is_visible_without_reopening_pass_or_overwriting_primary(self):
        fixture, terminal, route, path, jobs, owner, report, meta = self._settled("opencode", "spec/REPORT.md")
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
            with mock.patch.object(artifact_producer, "admit_shared",
                                   side_effect=artifact_producer.ProducerError("shared-spec-conflict", "policy")):
                completed = terminal.settle_owner_completion(jobs, "done", meta)
            self.assertEqual(completed.result, "completed", completed)
            self.assertEqual(completed.shared_publication["status"], "pending")
            request = terminal._completion_request(jobs, "done", meta)
            outcome = ROUTE.outcome_path(path).read_bytes()
            envelope = (terminal._commit_state_path(request).parent / "owner-envelope.txt").read_bytes()
            self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).state, "pending")
            diagnosis = terminal.inspect_owner_completion(jobs, "done", meta)
            self.assertEqual(diagnosis["shared_publication"]["status"], "pending")
            self.assertIn("--attempt", diagnosis["recovery_command"])
            import workflow_state
            self.assertEqual(workflow_state.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs).state()["workflow_state"], "COMPLETE")
            recovered = terminal.settle_owner_completion(jobs, "done", meta)
            self.assertEqual(recovered.shared_publication["status"], "admitted", recovered)
            self.assertEqual(terminal.owner_completion_state(jobs, "done", meta).state, "complete")
            self.assertEqual(ROUTE.outcome_path(path).read_bytes(), outcome)
            self.assertEqual((terminal._commit_state_path(request).parent / "owner-envelope.txt").read_bytes(), envelope)

    def test_missing_official_prd_keeps_report_fallback_and_publication_pending_without_synthesis(self):
        fixture, terminal, route, path, jobs, owner, report, meta = self._settled("claude", "spec/REPORT.md")
        (report.parent / "prd.md").unlink()
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
            completed = terminal.settle_owner_completion(jobs, "done", meta)
            self.assertEqual(completed.result, "completed", completed)
            self.assertEqual(completed.shared_publication["reason"], "official-spec-prd-missing")
            self.assertIn(f"artifact: {report}", completed.envelope_text)
            self.assertFalse((report.parent / "prd.md").exists())
            self.assertEqual(artifact_producer.list_references(fixture.root, "spec"), [])


class OwnerFinishPrimaryRegressionTest(_TerminalCommitFixture):
    def test_request_carries_optional_owner_handoff_primary(self):
        request = self.request()
        self.assertTrue(hasattr(request, "owner_handoff"))
        self.assertIsNone(request.owner_handoff)

    def test_explicit_owner_primary_wins_envelope_fallback_for_nonproducer_route(self):
        owner_report = self.root / "z-user-report.md"
        owner_report.write_text("owner report\n")
        request = T.TerminalCommitRequest(self.route_file, "att-a3fixture", self.jobs,
                                          self.root, {"primary": str(owner_report)})
        services, _ = self.services()
        with mock.patch.object(T, "prove_terminal_authority", return_value=T.TerminalProof("proved")), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=False), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = self.gates
            result = T.settle_terminal_commit(request, services)
        self.assertEqual(result.result, "completed", (result.reason, result.detail))
        envelope = json.loads((T._commit_state_path(request).parent / "owner-envelope.json").read_text())
        self.assertEqual(envelope["primary_path"], str(owner_report.resolve()))

    def test_outside_owner_hint_is_ignored_and_existing_terminal_fallback_remains(self):
        foreign = self.root.parent / "foreign-owner-report.md"
        foreign.write_text("outside root\n")
        request = T.TerminalCommitRequest(self.route_file, "att-a3fixture", self.jobs,
                                          self.root, {"primary": str(foreign)})
        services, _ = self.services()
        with mock.patch.object(T, "prove_terminal_authority", return_value=T.TerminalProof("proved")), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=False), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = self.gates
            result = T.settle_terminal_commit(request, services)
        self.assertEqual(result.result, "completed", (result.reason, result.detail))
        envelope = json.loads((T._commit_state_path(request).parent / "owner-envelope.json").read_text())
        self.assertEqual(envelope["primary_path"], str(self.artifact.resolve()))


if __name__ == "__main__":
    unittest.main()
