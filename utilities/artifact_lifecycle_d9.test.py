#!/usr/bin/env python3
"""D-9 direct/quick production-transaction contract tests for
`artifact_lifecycle.py run-direct-quick`. Every fixture uses an isolated
`tempfile.TemporaryDirectory()` artifact root; byte-delta measurement
reimplements the `_root_fingerprint()` pattern from `artifact_admission.test.py`
rather than importing that private helper.
"""
import hashlib
import importlib.util
import json
import os
import subprocess
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
_S = importlib.util.spec_from_file_location("route_for_lifecycle_d9_test", P)
R = importlib.util.module_from_spec(_S)
_S.loader.exec_module(R)

LIFECYCLE_SCRIPT = Path(__file__).with_name("artifact_lifecycle.py")

ALL = [
    "atomic-outcome", "known-scope", "no-shared-contract", "no-resource-run",
    "no-artifact-handoff", "no-independent-verifier", "focused-verification",
]


def _root_fingerprint(root):
    root = Path(root)
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        digest.update(rel.encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _lineage_fingerprint(root):
    """Hash only durable lineage/index state, excluding runtime routes."""
    digest = hashlib.sha256()
    for relative in (Path("campaigns"), Path(adm.ADMISSION_REL)):
        base = Path(root) / relative
        if not base.exists():
            continue
        for path in sorted([base, *base.rglob("*")]):
            rel = str(path.relative_to(root))
            digest.update(rel.encode("utf-8"))
            if path.is_file():
                digest.update(path.read_bytes())
    return digest.hexdigest()


class LifecycleD9TestBase(unittest.TestCase):
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

    def admitted_route(self, *, root=None, intensity="direct", mark_complete=True, cwd=None):
        """Compile and admit a route; optionally mark its terminal node
        complete (a D-9 durable close requires the terminal gate proven --
        the no-output/route-only path does not)."""
        root = root or self.root
        identity = adm.ensure_root_identity(root)
        if intensity == "quick":
            registered = {"candidates": [{
                "harness": "codex", "transport": "headless", "surface": "registered-headless",
                "status": "supported", "probe_source": "fixture-probe", "probe_time": "2026-08-13T00:00:00Z",
            }]}
            route = R.compile_route(**self.args(
                artifact_root=root, cwd=cwd or R.ROOT,
                requested_intensity="quick", predicates=[], transport=None,
                inline_reason=None, registered_headless_evidence=registered,
            ))
        else:
            route = R.compile_route(**self.args(artifact_root=root, cwd=cwd or R.ROOT))
        binding = L.admit_runtime_route(root, route)
        if mark_complete:
            node = next(n for n in route["nodes"] if n.get("terminal") is True)
            evidence = Path(self._tmp.name) / f"evidence-{route['route_id']}.txt"
            evidence.write_text("terminal evidence\n", encoding="utf-8")
            attempt_metadata = None
            if node.get("dispatch_depth") != 0 or node.get("execution_surface") != "inline":
                attempt_metadata = {
                    "attempt_schema_version": 2, "dispatch_depth": node.get("dispatch_depth"),
                    "transport": "headless", "execution_surface": node.get("execution_surface"),
                    "registered_worker": "1", "fallback_hop": "same-harness-headless",
                }
            R.write_completion_marker(
                route, node, node["id"], evidence,
                attempt_id="att-fixture" if attempt_metadata else None,
                attempt_metadata=attempt_metadata,
            )
        return identity, route, Path(binding.route_file)

    def durable_document(self, identity, route):
        alloc = idm.IdAllocator()
        camp_id = alloc.allocate("campaign")
        cyc_id = alloc.allocate("cycle")
        art_id = alloc.allocate("artifact")
        arev_id = alloc.allocate("artifact_revision")
        man_id = alloc.allocate("manifest")
        mrev_id = alloc.allocate("manifest_revision")
        evt_terminal_id = alloc.allocate("event")
        evt_completed_id = alloc.allocate("event")
        strm_a = alloc.allocate("stream")
        strm_b = alloc.allocate("stream")

        staging = Path(self._tmp.name) / f"staging-{cyc_id}"
        staging.mkdir(parents=True, exist_ok=True)
        plan_bytes = b"plan body\n"
        (staging / "plan.md").write_bytes(plan_bytes)
        digest = m.digest_bytes(plan_bytes)

        document = {
            "schema_version": 2, "manifest_kind": "artifact.cycle",
            "manifest_id": man_id, "manifest_revision_id": mrev_id,
            "repository_id": identity.repository_id, "artifact_root_id": identity.artifact_root_id,
            "campaign": {
                "campaign_id": camp_id, "goal": "g",
                "completion_criterion": {"statement": "s"}, "title": "t", "state": "active",
            },
            "cycle": {
                "cycle_id": cyc_id, "campaign_id": camp_id, "parent_cycle_id": None,
                "started_on": "2026-08-13T00:00:00Z", "input_digest": "sha256:" + "0" * 64,
                "outcome_criterion": {"required_artifact_roles": [], "decision_required": False},
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
                 "route_hash": route["route_hash"], "terminal_marker": "sha256:" + "0" * 64,
                 "terminal_evidence_id": evt_terminal_id},
            ],
            "events": [
                {
                    "event_id": evt_completed_id, "stream_id": strm_a, "stream_sequence": 1,
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
                    "event_id": evt_terminal_id, "stream_id": strm_b, "stream_sequence": 1,
                    "event_type": "route.terminal.recorded", "target_id": cyc_id,
                    "actor": {"kind": "system", "id": "capability-route"}, "recorded_at": "2026-08-13T00:00:01Z",
                    "provenance": {
                        "source_manifest_id": man_id, "source_revision_id": mrev_id,
                        "producer_route_id": route["route_id"], "algorithm_version": "v1",
                        "schema_version": 1, "source_digest": "sha256:" + "7" * 64,
                    },
                    "evidence_ids": [], "payload": {},
                },
            ],
            "producer": {"producer_id": alloc.allocate("producer"), "contract_version": m.CONTRACT_VERSION,
                         "source_revision": "abc"},
        }
        return document, staging, cyc_id


class ArtifactLifecycleCliTest(LifecycleD9TestBase):
    # D1 -- document=None, no durable output: route-only, zero lineage byte delta.
    def test_no_output_direct_and_quick_write_only_route_outcome(self):
        for mode in ("direct", "quick"):
            with self.subTest(mode=mode):
                subroot = Path(self._tmp.name) / f"root-{mode}"
                subroot.mkdir(parents=True, exist_ok=True)
                identity, route, route_file = self.admitted_route(root=subroot, intensity=mode, mark_complete=False)
                before = _root_fingerprint(subroot)
                lineage_before = _lineage_fingerprint(subroot)
                request = L.DirectQuickRequest(route=route, route_file=route_file)
                decision = L.finalize_direct_quick(subroot, request)
                self.assertEqual(decision.status, "route-only", decision.to_payload())
                after = _root_fingerprint(subroot)
                # Only the route-close outcome sidecar changes; no lineage/index/folder appears.
                self.assertNotEqual(before, after)
                self.assertFalse((subroot / "campaigns").exists())
                self.assertEqual(_lineage_fingerprint(subroot), lineage_before)

    # D2/D3 -- document/staging mismatch is rejected with zero delta.
    def test_document_staging_mismatch_is_rejected_with_zero_delta(self):
        identity, route, route_file = self.admitted_route()
        document, staging, _cyc_id = self.durable_document(identity, route)
        before = _root_fingerprint(self.root)
        only_document = L.finalize_direct_quick(
            self.root, L.DirectQuickRequest(route=route, route_file=route_file, document=document),
        )
        self.assertEqual(only_document.status, "reject")
        self.assertEqual(only_document.reasons[0].code, "d9-document-without-durable-output")
        only_staging = L.finalize_direct_quick(
            self.root, L.DirectQuickRequest(route=route, route_file=route_file, staging_source=staging),
        )
        self.assertEqual(only_staging.status, "reject")
        self.assertEqual(only_staging.reasons[0].code, "d9-durable-output-without-document")
        self.assertEqual(_root_fingerprint(self.root), before)

    # D4/D5 -- empty-output manifest and missing idempotency_key are rejected before effects.
    def test_rejects_empty_or_partial_durable_lineage_before_effects(self):
        identity, route, route_file = self.admitted_route()
        document, staging, _cyc_id = self.durable_document(identity, route)
        before = _root_fingerprint(self.root)

        empty_doc = dict(document, artifacts=[], artifact_revisions=[])
        empty_decision = L.finalize_direct_quick(
            self.root,
            L.DirectQuickRequest(
                route=route, route_file=route_file, document=empty_doc, staging_source=staging,
                idempotency_key="k1",
            ),
        )
        self.assertEqual(empty_decision.status, "reject")
        self.assertEqual(empty_decision.reasons[0].code, "d9-empty-output-manifest")

        no_key_decision = L.finalize_direct_quick(
            self.root,
            L.DirectQuickRequest(route=route, route_file=route_file, document=document, staging_source=staging),
        )
        self.assertEqual(no_key_decision.status, "reject")
        self.assertEqual(no_key_decision.reasons[0].code, "d9-partial-lineage-request")
        self.assertEqual(_root_fingerprint(self.root), before)

    # D6 -- staged digest mismatch causes rejection with zero lineage delta.
    def test_staged_digest_mismatch_is_rejected_with_zero_lineage_delta(self):
        identity, route, route_file = self.admitted_route()
        document, staging, _cyc_id = self.durable_document(identity, route)
        (staging / "plan.md").write_bytes(b"tampered\n")
        before = _root_fingerprint(self.root)
        decision = L.finalize_direct_quick(
            self.root,
            L.DirectQuickRequest(
                route=route, route_file=route_file, document=document, staging_source=staging,
                idempotency_key="k2",
            ),
        )
        self.assertEqual(decision.status, "reject")
        self.assertFalse((self.root / "campaigns").exists())
        # The route outcome sidecar is independent runtime provenance and is not
        # written on this path either, since the staged-payload check runs first.
        self.assertEqual(_root_fingerprint(self.root), before)

    # D7 -- valid durable path: full-lineage, full minimum lineage present.
    def test_durable_success_admits_full_minimum_lineage_once(self):
        identity, route, route_file = self.admitted_route()
        document, staging, cyc_id = self.durable_document(identity, route)
        request = L.DirectQuickRequest(
            route=route, route_file=route_file, document=document, staging_source=staging,
            idempotency_key="k3",
        )
        decision = L.finalize_direct_quick(self.root, request)
        self.assertEqual(decision.status, "full-lineage", decision.to_payload())
        self.assertTrue(decision.detail["lineage_committed"])
        cycle_dirs = list((self.root / "campaigns").rglob("manifest.json"))
        self.assertEqual(len(cycle_dirs), 1)
        self.assertTrue(L.canonical_outcome_path(self.root, route["route_id"]).is_file())
        published = json.loads(cycle_dirs[0].read_text(encoding="utf-8"))
        self.assertNotEqual(published["routes"][0]["terminal_marker"], "sha256:" + "0" * 64)
        terminal_event = next(
            event for event in published["events"]
            if event["event_id"] == published["routes"][0]["terminal_evidence_id"]
        )
        self.assertEqual(terminal_event["event_type"], "route.terminal.recorded")
        self.assertEqual(terminal_event["payload"]["route_hash"], route["route_hash"])
        self.assertEqual(
            terminal_event["payload"]["terminal_marker_digest"],
            published["routes"][0]["terminal_marker"],
        )
        retry = L.finalize_direct_quick(self.root, request)
        self.assertEqual(retry.status, "full-lineage", retry.to_payload())
        self.assertEqual(retry.detail["admission"]["status"], "noop-idempotent")
        self.assertEqual(len(list((self.root / "campaigns").rglob("manifest.json"))), 1)

    # Durable failure leaves only the independent route outcome and zero lineage residue.
    def test_durable_failure_leaves_only_route_outcome_and_zero_lineage(self):
        identity, route, route_file = self.admitted_route()
        document, staging, _cyc_id = self.durable_document(identity, route)
        # A well-formed manifest/completion but a repository_id that does not
        # match the frozen root identity: passes every pure/completion check
        # this module runs (none of them inspect repository_id), so `close_route`
        # already ran, and only `artifact_admission.admit()`'s own tenancy check
        # rejects it -- the route outcome is independent runtime provenance and
        # survives; zero campaign/cycle/manifest lineage is committed.
        broken = json.loads(json.dumps(document))
        broken["repository_id"] = "repo_" + "f" * 32
        decision = L.finalize_direct_quick(
            self.root,
            L.DirectQuickRequest(
                route=route, route_file=route_file, document=broken, staging_source=staging,
                idempotency_key="k4",
            ),
        )
        self.assertEqual(decision.status, "reject", decision.to_payload())
        self.assertFalse((self.root / "campaigns").exists())
        self.assertTrue(L.canonical_outcome_path(self.root, route["route_id"]).is_file())

    def test_durable_retry_rejects_stale_failed_outcome_provenance(self):
        identity, route, route_file = self.admitted_route()
        document, staging, _cyc_id = self.durable_document(identity, route)
        broken = json.loads(json.dumps(document))
        broken["repository_id"] = "repo_" + "f" * 32

        first = L.finalize_direct_quick(
            self.root,
            L.DirectQuickRequest(
                route=route, route_file=route_file, document=broken,
                staging_source=staging, idempotency_key="stale-attempt-one",
                publication="failed", commit="a" * 40,
                summary="ATTEMPT-ONE-FAILED",
            ),
        )
        self.assertEqual(first.status, "reject", first.to_payload())
        self.assertFalse((self.root / "campaigns").exists())
        lineage_before = _lineage_fingerprint(self.root)

        second = L.finalize_direct_quick(
            self.root,
            L.DirectQuickRequest(
                route=route, route_file=route_file, document=document,
                staging_source=staging, idempotency_key="corrected-attempt-two",
                publication="succeeded", commit="b" * 40,
                summary="ATTEMPT-TWO-OK",
            ),
        )
        self.assertEqual(second.status, "reject", second.to_payload())
        self.assertEqual(
            second.reasons[0].code, "d9-route-outcome-provenance-conflict"
        )
        self.assertEqual(_lineage_fingerprint(self.root), lineage_before)
        self.assertFalse((self.root / "campaigns").exists())
        outcome = json.loads(
            L.canonical_outcome_path(self.root, route["route_id"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(outcome["publication"], "failed")
        self.assertEqual(outcome["summary"], "ATTEMPT-ONE-FAILED")
        self.assertEqual(outcome["head_commit"], "a" * 40)

    # D8 -- effective_intensity is standard.
    def test_rejects_non_direct_quick_intensity(self):
        identity, route, route_file = self.admitted_route()
        decision = L.finalize_direct_quick(
            self.root,
            L.DirectQuickRequest(route=dict(route, effective_intensity="standard"), route_file=route_file),
        )
        self.assertEqual(decision.status, "reject")
        self.assertEqual(decision.reasons[0].code, "d9-intensity-not-direct-or-quick")

    def test_route_cwd_may_be_a_distinct_project_worktree(self):
        project = Path(self._tmp.name) / "project"
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        identity, route, route_file = self.admitted_route(cwd=project, mark_complete=False)
        decision = L.finalize_direct_quick(
            self.root, L.DirectQuickRequest(route=route, route_file=route_file),
        )
        self.assertEqual(decision.status, "route-only", decision.to_payload())

    # D9 -- D1 and D7 through the real CLI seam, proving the production integration.
    def test_cli_seam_no_output_and_durable_success(self):
        root_a = Path(self._tmp.name) / "root-cli-a"
        root_a.mkdir(parents=True, exist_ok=True)
        identity, route, route_file = self.admitted_route(root=root_a)
        no_output = subprocess.run(
            [sys.executable, str(LIFECYCLE_SCRIPT), "run-direct-quick",
             "--route", str(route_file), "--artifact-root", str(root_a)],
            capture_output=True, text=True, cwd=str(R.ROOT),
        )
        self.assertEqual(no_output.returncode, 0, no_output.stderr)
        self.assertEqual(json.loads(no_output.stdout)["status"], "route-only")

        root_b = Path(self._tmp.name) / "root-cli-b"
        root_b.mkdir(parents=True, exist_ok=True)
        identity2, route2, route_file2 = self.admitted_route(root=root_b)
        document, staging, _cyc_id = self.durable_document(identity2, route2)
        doc_path = Path(self._tmp.name) / "document.json"
        doc_path.write_text(json.dumps(document), encoding="utf-8")
        durable = subprocess.run(
            [sys.executable, str(LIFECYCLE_SCRIPT), "run-direct-quick",
             "--route", str(route_file2), "--artifact-root", str(root_b),
             "--document", str(doc_path), "--staging-source", str(staging),
             "--idempotency-key", "k5"],
            capture_output=True, text=True, cwd=str(R.ROOT),
        )
        self.assertEqual(durable.returncode, 0, durable.stderr)
        self.assertEqual(json.loads(durable.stdout)["status"], "full-lineage")


if __name__ == "__main__":
    unittest.main()
