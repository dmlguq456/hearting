import json
import os
import subprocess
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

    def test_owner_prerequisites_keep_resource_evidence_and_do_not_invent_a_second_owner(self):
        evidence=self.root/"resource.json"; evidence.write_text('{"exit_code":0}')
        resource={"id":"run","kind":"resource-runner","completion_gate":"lab-run"}
        publish={"id":"publish","kind":"capability-owner","unit":"_kernel/owner",
                 "dispatch_depth":1,"depends_on":["run"]}
        terminal={**publish,"id":"sync","terminal":True,"depends_on":["publish"]}
        route={"route_id":"rt-resource-owner","route_hash":"sha256:fixture","nodes":[resource,publish,terminal]}
        directory=ROUTE.completion_dir(route["route_id"],jobs=self.jobs); directory.mkdir(parents=True)
        marker={"route_id":route["route_id"],"route_hash":route["route_hash"],"node_id":"run",
                "completion_gate":"lab-run","registered_worker":False,
                "evidence":{"path":str(evidence),"sha256":ROUTE.evidence_digest(evidence)}}
        (directory/"run.json").write_text(json.dumps(marker))
        self.assertEqual(ROUTE.owner_terminal_prerequisites(route,terminal,self.jobs),{})
        self.assertEqual(self.jobs.read_text(),"")
        evidence.write_text('{"exit_code":1}')
        self.assertEqual(ROUTE.owner_terminal_prerequisites(route,terminal,self.jobs),
                         {"run":"completion-evidence-hash-mismatch"})

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
                             ("ineligible", "child-not-quiescent"))
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

    def test_completion_state_evidence_hash_mismatch_is_blocked(self):
        gates = {"execute": {"passed": False, "reason": "completion-evidence-hash-mismatch"}}
        with mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = gates
            state = T.owner_completion_state(self.jobs, "done", self._metadata())
        self.assertEqual((state.state, state.reason),
                          ("blocked", "completion-evidence-hash-mismatch"))

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
                         {"completion-attempt-not-current", "completion-evidence-hash-mismatch"})


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


if __name__ == "__main__":
    unittest.main()
