#!/usr/bin/env python3
"""A-1 route surface (D-2/D-5) and D-10 fixed-input cycle / completion contract
tests for `artifact_lifecycle.py`. Every fixture uses an isolated
`tempfile.TemporaryDirectory()` artifact root and `AGENT_HOME` -- never the
real canonical registry or routes directory.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_lifecycle as L
import artifact_admission as adm
import artifact_identity as idm
import artifact_manifest as m

P = Path(__file__).with_name("capability-route.py")
_S = importlib.util.spec_from_file_location("route_for_lifecycle_test", P)
R = importlib.util.module_from_spec(_S)
_S.loader.exec_module(R)

ALL = [
    "atomic-outcome", "known-scope", "no-shared-contract", "no-resource-run",
    "no-artifact-handoff", "no-independent-verifier", "focused-verification",
]


class LifecycleTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "artifact-root"
        self.root.mkdir(parents=True, exist_ok=True)
        home = Path(self._tmp.name) / "agent-home"
        (home / "core").mkdir(parents=True, exist_ok=True)
        (home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        self._prev_home = os.environ.get("AGENT_HOME")
        os.environ["AGENT_HOME"] = str(home)
        self._prev_jobs = os.environ.get("AGENT_DISPATCH_JOBS")
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._prev_home is None:
            os.environ.pop("AGENT_HOME", None)
        else:
            os.environ["AGENT_HOME"] = self._prev_home
        if self._prev_jobs is None:
            os.environ.pop("AGENT_DISPATCH_JOBS", None)
        else:
            os.environ["AGENT_DISPATCH_JOBS"] = self._prev_jobs
        self._tmp.cleanup()

    def args(self, **kw):
        gate = {
            "spec_read": {"satisfied": True, "source": "fixture"},
            "drift_verdict": "within-spec", "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "fixture"},
        }
        d = dict(
            capability="autopilot-code", capability_mode="dev", requested_intensity="direct",
            cwd=R.ROOT, artifact_root=self.root, predicates=ALL, transport=None,
            inline_reason="atomic-direct", tracking="tracked", tracked_gate_evidence=gate,
        )
        d.update(kw)
        return d

    def compile_route(self, *, root=None, **kw):
        kw.setdefault("artifact_root", root or self.root)
        return R.compile_route(**self.args(**kw))

    def other_root(self):
        other = Path(self._tmp.name) / "artifact-root-2"
        other.mkdir(parents=True, exist_ok=True)
        return other


class ArtifactLifecycleRouteTest(LifecycleTestBase):
    # N1 -- alias basename inside the canonical directory.
    def test_rejects_alias_basename_inside_canonical_dir(self):
        route = self.compile_route()
        alias = R.canonical_routes_dir(self.root) / "alias-name.json"
        decision = L.validate_route_target(alias, self.root, route["route_id"], kind="route")
        self.assertFalse(decision.ok)
        self.assertEqual(decision.reasons[0].code, "route-target-alias-basename")

    # N2 -- every legacy location and an unrelated outside target are read-only.
    def test_rejects_targets_outside_canonical_route_dir(self):
        route = self.compile_route()
        route_id = route["route_id"]
        targets = {
            "legacy-root": self.root / f"{route_id}-route.json",
            "legacy-routes": self.root / "routes" / f"{route_id}.json",
            "legacy-_routes": self.root / "_routes" / f"{route_id}.json",
            "legacy-.routes": self.root / ".routes" / f"{route_id}.json",
            "outside-root": Path(self._tmp.name) / "outside" / f"{route_id}.json",
        }
        for location, target in targets.items():
            with self.subTest(location=location):
                decision = L.validate_route_target(
                    target, self.root, route_id, kind="route"
                )
                self.assertFalse(decision.ok)
                self.assertEqual(
                    decision.reasons[0].code, "route-target-outside-canonical-dir"
                )

    # N3 -- <route_id>.foo.json sidecar.
    def test_rejects_unknown_sidecar_basename(self):
        route = self.compile_route()
        sidecar = R.canonical_routes_dir(self.root) / f"{route['route_id']}.foo.json"
        decision = L.validate_route_target(sidecar, self.root, route["route_id"], kind="route")
        self.assertFalse(decision.ok)
        self.assertEqual(decision.reasons[0].code, "route-target-unknown-sidecar")

    # N4 -- (root_id, route_id) already present in the runtime directory.
    def test_rejects_duplicate_root_route_composite_before_write(self):
        route = self.compile_route()
        L.admit_runtime_route(self.root, route)
        with self.assertRaises(L.LifecycleError) as ctx:
            L.admit_runtime_route(self.root, route)
        self.assertEqual(ctx.exception.code, "route-composite-duplicate-runtime")

    # N5 -- same composite already present in the step-1 index.
    def test_rejects_duplicate_composite_already_in_index(self):
        route = self.compile_route()
        identity = adm.ensure_root_identity(self.root)
        empty = __import__("artifact_index").empty(identity.artifact_root_id)
        indexed = dict(empty.routes)
        indexed[identity.artifact_root_id] = {route["route_id"]: {"cycle_id": None, "route_hash": None}}
        import dataclasses
        seeded = dataclasses.replace(empty, routes=indexed)
        decision = L.evaluate_route_admission(self.root, route, index=seeded)
        self.assertFalse(decision.ok)
        self.assertEqual(decision.reasons[0].code, "route-composite-duplicate-index")

    # P3 -- two roots with different artifact_root_id reusing one route_id: both accepted.
    # `route_id` is path-derived (it hashes the absolute `artifact_root`), so
    # a naturally-compiled route never collides across roots -- this pins the
    # composite check itself by forcing the same `route_id` into two roots.
    def test_same_route_id_is_independent_across_root_ids(self):
        other = self.other_root()
        adm.ensure_root_identity(self.root)
        adm.ensure_root_identity(other)
        route_a = self.compile_route(root=self.root)
        route_b = dict(route_a)
        route_b["artifact_root"] = str(other)
        binding_a = L.admit_runtime_route(self.root, route_a)
        binding_b = L.admit_runtime_route(other, route_b)
        self.assertEqual(route_a["route_id"], route_b["route_id"])
        self.assertNotEqual(binding_a.artifact_root_id, binding_b.artifact_root_id)

    # forged root tenancy.
    def test_rejects_expected_root_id_mismatch(self):
        route = self.compile_route()
        adm.ensure_root_identity(self.root)
        with self.assertRaises(L.LifecycleError) as ctx:
            L.admit_runtime_route(self.root, route, expected_root_id="root_" + "f" * 32)
        self.assertEqual(ctx.exception.code, "artifact-root-id-mismatch")

    # P4 -- auxiliary non-route JSON in .runtime/routes is ignored by the scan.
    def test_scan_ignores_auxiliary_non_route_json(self):
        R.canonical_routes_dir(self.root).mkdir(parents=True, exist_ok=True)
        (R.canonical_routes_dir(self.root) / "not-a-route.json").write_text(
            json.dumps({"note": "auxiliary"}), encoding="utf-8"
        )
        self.assertEqual(L.scan_runtime_routes(self.root), ())

    # P5 -- root with no issued identity degrades with a typed note, no exception.
    def test_admission_degrades_when_root_identity_unissued(self):
        route = self.compile_route()
        decision = L.evaluate_route_admission(self.root, route)
        self.assertTrue(decision.ok)
        self.assertIn("root-identity-unissued", decision.detail.get("notes", []))
        self.assertIsNone(decision.detail.get("artifact_root_id"))

    def test_read_root_identity_never_allocates(self):
        L.read_root_identity(self.root)
        self.assertFalse((self.root / adm.ADMISSION_REL / "root-identity.json").exists())

    def test_admit_runtime_route_writes_canonical_file(self):
        route = self.compile_route()
        binding = L.admit_runtime_route(self.root, route)
        self.assertEqual(Path(binding.route_file), R.canonical_routes_dir(self.root) / f"{route['route_id']}.json")
        self.assertTrue(Path(binding.route_file).is_file())


def _cycle(**kw):
    base = {
        "cycle_id": "cyc_" + "1" * 32,
        "campaign_id": "camp_" + "1" * 32,
        "parent_cycle_id": None,
        "input_digest": "sha256:" + "0" * 64,
        "outcome_criterion": {"required_artifact_roles": [], "decision_required": False},
        "state": "active",
    }
    base.update(kw)
    return base


class ArtifactLifecycleCycleTest(LifecycleTestBase):
    # P1 -- identical input_digest, compatible criterion, prior active.
    def test_compatible_unresolved_resume_preserves_cycle_id(self):
        prior = _cycle()
        candidate = _cycle()
        decision = L.decide_cycle_start_or_resume(prior, candidate)
        self.assertEqual(decision.status, "resume-same-cycle")
        self.assertEqual(decision.detail["cycle_id"], prior["cycle_id"])

    # N6 -- changed input_digest, no parent_cycle_id -> new-child-cycle-required + link-missing.
    def test_changed_input_requires_distinct_child_cycle(self):
        prior = _cycle()
        candidate = _cycle(input_digest="sha256:" + "9" * 64)
        decision = L.decide_cycle_start_or_resume(prior, candidate)
        self.assertEqual(decision.status, "new-child-cycle-required")
        self.assertTrue(any(r.code == "material-input-change-reused-cycle-id" for r in decision.reasons))

    def test_changed_outcome_requires_distinct_child_cycle(self):
        prior = _cycle()
        candidate = _cycle(
            outcome_criterion={"required_artifact_roles": ["primary"], "decision_required": False},
        )
        decision = L.decide_cycle_start_or_resume(prior, candidate)
        self.assertEqual(decision.status, "new-child-cycle-required")

    def test_well_formed_child_cycle_is_accepted(self):
        prior = _cycle()
        candidate = _cycle(
            cycle_id="cyc_" + "2" * 32,
            parent_cycle_id=prior["cycle_id"],
            input_digest="sha256:" + "9" * 64,
        )
        decision = L.decide_cycle_start_or_resume(prior, candidate)
        self.assertEqual(decision.status, "new-child-cycle-required")
        self.assertEqual(decision.reasons, ())
        self.assertEqual(decision.detail["parent_cycle_id"], prior["cycle_id"])

    # N7 -- prior cycle is terminal.
    def test_rejects_resume_of_terminal_prior_cycle(self):
        prior = _cycle(state="completed")
        candidate = _cycle()
        decision = L.decide_cycle_start_or_resume(prior, candidate)
        self.assertEqual(decision.status, "reject")
        self.assertEqual(decision.reasons[0].code, "cycle-prior-terminal")

    # N8 -- prior descriptor not backed by a published manifest.
    def test_rejects_unverified_prior_descriptor(self):
        candidate = _cycle()
        decision = L.decide_cycle_start_or_resume({"not": "a-cycle-shape"}, candidate)
        self.assertEqual(decision.status, "reject")
        self.assertEqual(decision.reasons[0].code, "cycle-prior-descriptor-unverified")

    def test_new_root_cycle_with_parent_is_rejected(self):
        candidate = _cycle(parent_cycle_id="cyc_" + "9" * 32)
        decision = L.decide_cycle_start_or_resume(None, candidate)
        self.assertEqual(decision.status, "reject")

    def test_read_admitted_cycle_returns_none_when_absent(self):
        self.assertIsNone(L.read_admitted_cycle(self.root, "camp_" + "1" * 32, "cyc_" + "1" * 32))


class ArtifactLifecycleCompletionTest(LifecycleTestBase):
    """Baseline-and-mutate: one fully wired `complete` fixture (route, marker,
    outcome, manifest) plus targeted mutations for each N9-N16/P2/P6 case."""

    def _fixture(self, *, decision_required=True):
        identity = adm.ensure_root_identity(self.root)
        route = self.compile_route()
        binding = L.admit_runtime_route(self.root, route)
        node = next(n for n in route["nodes"] if n.get("terminal") is True)
        node_id = node["id"]
        content_dir = Path(self._tmp.name) / "content"
        content_dir.mkdir(parents=True, exist_ok=True)
        evidence = content_dir / "evidence.txt"
        evidence.write_text("terminal evidence\n", encoding="utf-8")
        R.write_completion_marker(route, node, node_id, evidence)
        outcome, _ = R.close_route(route, binding.route_file, commit="a" * 40, summary="fixture")
        self.assertTrue(outcome["terminal_gate_proven"])
        marker_digest = L._marker_digest(R, route)
        outcome_digest = L._sha256_path(Path(binding.outcome_file))

        alloc = idm.IdAllocator()
        camp_id = alloc.allocate("campaign")
        cyc_id = alloc.allocate("cycle")
        art_id = alloc.allocate("artifact")
        arev_id = alloc.allocate("artifact_revision")
        man_id = alloc.allocate("manifest")
        mrev_id = alloc.allocate("manifest_revision")
        repo_id = alloc.allocate("repository")
        prod_id = alloc.allocate("producer")
        evt_artifact_id = alloc.allocate("event")
        evt_completed_id = alloc.allocate("event")
        evt_terminal_id = alloc.allocate("event")
        evt_decision_id = alloc.allocate("event")
        # Each event owns a distinct stream_id: `_check_no_transition_out_of_terminal`
        # flags any later event on the *same* stream after a terminal-type event, and
        # these events are logically independent facts, not one ordered stream.
        strm_artifact_id = alloc.allocate("stream")
        strm_completed_id = alloc.allocate("stream")
        strm_terminal_id = alloc.allocate("stream")
        strm_decision_id = alloc.allocate("stream")

        plan_bytes = b"plan body\n"
        (content_dir / "plan.md").write_bytes(plan_bytes)
        digest = m.digest_bytes(plan_bytes)

        events = [
            {
                "event_id": evt_artifact_id, "stream_id": strm_artifact_id, "stream_sequence": 1,
                "event_type": "artifact.revision.recorded", "target_id": art_id,
                "actor": {"kind": "producer", "id": "p"}, "recorded_at": "2026-08-13T00:00:00Z",
                "provenance": {
                    "source_manifest_id": man_id, "source_revision_id": mrev_id,
                    "producer_route_id": route["route_id"], "algorithm_version": "v1",
                    "schema_version": 1, "source_digest": "sha256:" + "6" * 64,
                },
                "evidence_ids": [], "payload": {},
            },
            {
                "event_id": evt_completed_id, "stream_id": strm_completed_id, "stream_sequence": 1,
                "event_type": "cycle.completed", "target_id": cyc_id,
                "actor": {"kind": "system", "id": "capability-route"}, "recorded_at": "2026-08-13T00:00:01Z",
                "provenance": {
                    "source_manifest_id": man_id, "source_revision_id": mrev_id,
                    "producer_route_id": route["route_id"], "algorithm_version": "v1",
                    "schema_version": 1, "source_digest": "sha256:" + "9" * 64,
                },
                "evidence_ids": [], "payload": {},
            },
            {
                "event_id": evt_terminal_id, "stream_id": strm_terminal_id, "stream_sequence": 1,
                "event_type": "route.terminal.recorded", "target_id": cyc_id,
                "actor": {"kind": "system", "id": "capability-route"}, "recorded_at": "2026-08-13T00:00:01Z",
                "provenance": {
                    "source_manifest_id": man_id, "source_revision_id": mrev_id,
                    "producer_route_id": route["route_id"], "algorithm_version": "v1",
                    "schema_version": 1, "source_digest": "sha256:" + "7" * 64,
                },
                "evidence_ids": [],
                "payload": {
                    "artifact_root_id": identity.artifact_root_id, "route_id": route["route_id"],
                    "route_hash": route["route_hash"], "outcome_digest": outcome_digest,
                    "terminal_marker_digest": marker_digest,
                },
            },
        ]
        if decision_required:
            events.append({
                "event_id": evt_decision_id, "stream_id": strm_decision_id, "stream_sequence": 1,
                "event_type": "decision.recorded", "target_id": cyc_id,
                "actor": {"kind": "user", "id": "u"}, "recorded_at": "2026-08-13T00:00:02Z",
                "provenance": {
                    "source_manifest_id": man_id, "source_revision_id": mrev_id,
                    "producer_route_id": route["route_id"], "algorithm_version": "v1",
                    "schema_version": 1, "source_digest": "sha256:" + "8" * 64,
                },
                "evidence_ids": [], "payload": {},
            })

        document = {
            "schema_version": 2, "manifest_kind": "artifact.cycle",
            "manifest_id": man_id, "manifest_revision_id": mrev_id,
            "repository_id": repo_id, "artifact_root_id": identity.artifact_root_id,
            "campaign": {
                "campaign_id": camp_id, "goal": "g",
                "completion_criterion": {"statement": "s"}, "title": "t", "state": "active",
            },
            "cycle": {
                "cycle_id": cyc_id, "campaign_id": camp_id, "parent_cycle_id": None,
                "started_on": "2026-08-13T00:00:00Z", "input_digest": "sha256:" + "0" * 64,
                "outcome_criterion": {
                    "required_artifact_roles": ["primary"], "decision_required": decision_required,
                },
                "state": "completed",
            },
            "artifacts": [
                {"artifact_id": art_id, "cycle_id": cyc_id, "role": "primary", "type": "doc",
                 "capability": "autopilot-code", "title": "t"},
            ],
            "artifact_revisions": [
                {"artifact_revision_id": arev_id, "artifact_id": art_id, "revision_sequence": 1,
                 "content_digest": digest, "byte_size": len(plan_bytes), "media_type": "text/plain",
                 "locator": {"kind": "cycle-relative", "path": "plan.md"},
                 "provenance": {
                     "source_manifest_id": man_id, "source_revision_id": mrev_id,
                     "producer_route_id": route["route_id"], "algorithm_version": "v1",
                     "schema_version": 1, "source_digest": "sha256:" + "2" * 64,
                 }},
            ],
            "shared_references": [], "shared_reference_revisions": [],
            "routes": [
                {"artifact_root_id": identity.artifact_root_id, "route_id": route["route_id"],
                 "route_hash": route["route_hash"], "terminal_marker": marker_digest,
                 "terminal_evidence_id": evt_terminal_id},
            ],
            "events": events,
            "producer": {"producer_id": prod_id, "contract_version": m.CONTRACT_VERSION, "source_revision": "abc"},
        }
        return {
            "document": document, "content_dir": content_dir, "route_file": Path(binding.route_file),
            "identity": identity, "route": route,
        }

    def _evaluate(self, fx, document=None):
        return L.evaluate_cycle_completion(
            document if document is not None else fx["document"],
            content_root=fx["content_dir"], route_file=fx["route_file"],
        )

    def test_baseline_fixture_is_complete(self):
        fx = self._fixture()
        decision = self._evaluate(fx)
        self.assertEqual(decision.status, "complete", decision.to_payload())

    # P6 -- completed cycle with no authorized campaign.satisfied: campaign stays active.
    def test_completed_cycle_does_not_satisfy_campaign(self):
        fx = self._fixture()
        decision = self._evaluate(fx)
        self.assertEqual(decision.status, "complete")
        self.assertEqual(fx["document"]["campaign"]["state"], "active")

    # N9 -- terminal_evidence_id matching no event in the manifest.
    def test_rejects_unbound_terminal_evidence(self):
        fx = self._fixture()
        doc = json.loads(json.dumps(fx["document"]))
        doc["routes"][0]["terminal_evidence_id"] = "evd_" + "f" * 32
        decision = self._evaluate(fx, doc)
        self.assertEqual(decision.status, "reject")
        self.assertEqual(decision.reasons[0].code, "completion-terminal-evidence-unbound")

    # N10 -- terminal_evidence_id resolves to a wrong-typed event.
    def test_rejects_wrong_event_type_terminal_evidence(self):
        fx = self._fixture()
        doc = json.loads(json.dumps(fx["document"]))
        artifact_event = next(e for e in doc["events"] if e["event_type"] == "artifact.revision.recorded")
        doc["routes"][0]["terminal_evidence_id"] = artifact_event["event_id"]
        decision = self._evaluate(fx, doc)
        self.assertEqual(decision.status, "reject")
        self.assertIn(
            decision.reasons[0].code,
            ("completion-terminal-evidence-wrong-event-type", "completion-terminal-evidence-unbound"),
        )

    def test_rejects_terminal_event_payload_mismatch(self):
        fx = self._fixture()
        doc = json.loads(json.dumps(fx["document"]))
        terminal_id = doc["routes"][0]["terminal_evidence_id"]
        terminal = next(
            event for event in doc["events"] if event["event_id"] == terminal_id
        )
        terminal["payload"]["outcome_digest"] = "sha256:" + "f" * 64
        decision = self._evaluate(fx, doc)
        self.assertEqual(decision.status, "reject")
        self.assertEqual(
            decision.reasons[0].code, "completion-terminal-evidence-unbound"
        )

    # N11 -- routes[].route_hash differs from the sealed route.
    def test_rejects_route_hash_mismatch(self):
        fx = self._fixture()
        doc = json.loads(json.dumps(fx["document"]))
        doc["routes"][0]["route_hash"] = "sha256:" + "9" * 64
        decision = self._evaluate(fx, doc)
        self.assertEqual(decision.status, "reject")
        self.assertEqual(decision.reasons[0].code, "completion-route-hash-mismatch")

    # N12 -- completion marker absent or identity mismatched.
    def test_rejects_forged_terminal_marker(self):
        fx = self._fixture()
        doc = json.loads(json.dumps(fx["document"]))
        doc["routes"][0]["terminal_marker"] = "sha256:" + "0" * 64
        decision = self._evaluate(fx, doc)
        self.assertEqual(decision.status, "reject")
        self.assertEqual(decision.reasons[0].code, "completion-terminal-marker-unverified")

    # N13 -- .outcome.json has terminal_gate_proven false.
    def test_rejects_unproven_terminal_gate(self):
        fx = self._fixture()
        outcome_path = L.canonical_outcome_path(self.root, fx["route"]["route_id"])
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        outcome["terminal_gate_proven"] = False
        outcome_path.write_text(json.dumps(outcome), encoding="utf-8")
        decision = self._evaluate(fx)
        self.assertEqual(decision.status, "reject")
        self.assertEqual(decision.reasons[0].code, "completion-terminal-gate-unproven")

    # N14 -- published payload digest differs from the manifest.
    def test_rejects_failed_artifact_verification(self):
        fx = self._fixture()
        (fx["content_dir"] / "plan.md").write_bytes(b"tampered\n")
        decision = self._evaluate(fx)
        self.assertEqual(decision.status, "reject")
        self.assertEqual(decision.reasons[0].code, "completion-artifact-verification-failed")

    # N15 -- decision_required: true with no decision.recorded event.
    def test_rejects_missing_required_decision(self):
        fx = self._fixture(decision_required=True)
        doc = json.loads(json.dumps(fx["document"]))
        doc["events"] = [e for e in doc["events"] if e["event_type"] != "decision.recorded"]
        decision = self._evaluate(fx, doc)
        self.assertEqual(decision.status, "reject")
        self.assertEqual(decision.reasons[0].code, "completion-decision-outcome-missing")

    # N16 -- publication value outside the enum.
    def test_rejects_unknown_publication_value(self):
        fx = self._fixture()
        decision = L.evaluate_cycle_completion(
            fx["document"], content_root=fx["content_dir"], route_file=fx["route_file"],
            publication="bogus",
        )
        self.assertEqual(decision.status, "reject")
        self.assertEqual(decision.reasons[0].code, "publication-unknown-result")

    # P2 -- publication="failed" with all other evidence complete: primary stays complete.
    def test_publication_failed_leaves_primary_result_complete(self):
        fx = self._fixture()
        decision = L.evaluate_cycle_completion(
            fx["document"], content_root=fx["content_dir"], route_file=fx["route_file"],
            publication="failed",
        )
        self.assertEqual(decision.status, "complete")
        self.assertEqual(decision.detail["publication"], "failed")

    def test_all_publication_values_leave_primary_verdict_unchanged(self):
        fx = self._fixture()
        for value in sorted(L.PUBLICATION_RESULTS):
            with self.subTest(publication=value):
                decision = L.evaluate_cycle_completion(
                    fx["document"], content_root=fx["content_dir"], route_file=fx["route_file"],
                    publication=value,
                )
                self.assertEqual(decision.status, "complete")


if __name__ == "__main__":
    unittest.main()
