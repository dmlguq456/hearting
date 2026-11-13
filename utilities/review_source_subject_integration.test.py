#!/usr/bin/env python3
"""Isolated Git/jobs/completion consumers for source-bound implementation review."""
from __future__ import annotations

import importlib.util
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_contract as DC  # noqa: E402
import review_round_cap as CAP  # noqa: E402
import review_source_subject as SUBJECT  # noqa: E402
from route_identity import route_hash, route_id_from_hash  # noqa: E402
from split_module_fixture import patch_split  # noqa: E402


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROUTE_WRITER = _load("capability_route_source_subject_fixture", "utilities/capability-route.py")
DISPATCH_NODE = _load("dispatch_node_source_subject_fixture", "utilities/dispatch-node.py")


class SourceSubjectIntegrationTest(unittest.TestCase):
    def test_smoke_pass_reuse_preserves_budget_and_aba_fail_history(self):
        import hashlib
        smoke_tool = _load("ami_smoke_integration", "tools/smoke-attestation.py")
        producer = {"id": "scaffold", "kind": "pipeline-stage", "dispatch_depth": 2,
                    "completion_gate": "experiment-scaffold", "depends_on": [],
                    "write_scope": ["experiment-scaffold/**", "config/**"]}
        node = {"id": "smoke", "kind": "review-worker", "unit": "qa/ml-debug", "dispatch_depth": 2,
                "completion_gate": "hash-bound-smoke", "depends_on": ["scaffold"],
                "inputs": ["experiment-scaffold", "config"],
                "write_scope": ["reviews/smoke-attestation.json"]}
        self.route["nodes"] = [producer, node, {"id": "full-run", "kind": "resource-runner",
            "completion_gate": "resource-exit", "dispatch_depth": 2, "depends_on": ["smoke"]}]
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.route_file.write_text(json.dumps(self.route))
        source = self.artifact_root / "experiment-scaffold/batch.py"
        source.parent.mkdir()
        source.write_text("value = 'A'\n")
        config = self.artifact_root / "config/run.yaml"
        config.parent.mkdir()
        config.write_text("steps: 1\n")
        self.complete_node("scaffold", "att-scaffold", "prepared")
        evidence = self.artifact_root / "reviews/smoke-attestation.json"
        evidence.parent.mkdir()
        payload = smoke_tool.payload([source.parent, config.parent], [sys.executable, str(source)], str(self.repo))
        payload.update(status="passed", exit_code=0)
        payload["attestation_hash"] = "sha256:" + hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        evidence.write_text(json.dumps(payload))
        aid = "att-inline-smoke"
        metadata = {**self._metadata(node, aid, "review"), "registered_worker": "0",
                    "execution_surface": "inline", "transport": "interactive", "fallback_hop": "inline"}
        marker = ROUTE_WRITER._publish_completion_locked(self.route, node, node["id"], evidence,
            jobs=self.jobs, attempt_id=aid, attempt_metadata=metadata)
        before = self.jobs.read_bytes()
        budget = DISPATCH_NODE.admit_round(self.route, node, self.jobs).budget
        for _ in range(3):
            reused = CAP.reuse_deterministic_pass(self.route, node, self.jobs, budget)
            self.assertIsNotNone(reused)
            self.assertEqual(reused["attempt_id"], aid)
            self.assertEqual(reused["review_independence"], "degraded")
        self.assertEqual(self.jobs.read_bytes(), before)
        self.assertEqual(DISPATCH_NODE.admit_round(self.route, node, self.jobs).budget.verdict_rounds,
                         budget.verdict_rounds)
        # Observe B as a failed review, then return to A. Neither the old PASS
        # nor its unchanged attestation file can erase that lineage.
        source.write_text("value = 'B'\n")
        self.review_node = node
        self.prepare_review("att-smoke-B", note="completed-review-blocking", extra={"failure_class": "fail"})
        source.write_text("value = 'A'\n")
        returned = DISPATCH_NODE.admit_round(self.route, node, self.jobs).budget
        self.assertIsNone(CAP.reuse_deterministic_pass(self.route, node, self.jobs, returned))
        # Consume the actual recovery command, publish through the existing
        # inline completion writer, and admit the existing resource successor.
        import shlex
        fields = CAP.recovery_fields("review-worker", "exhausted", route=self.route,
            node=node, jobs=self.jobs, route_file=self.route_file)
        env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_")}
        executed = subprocess.run(shlex.split(fields["recovery_run_command"]), env=env,
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)
        fresh_payload = json.loads(evidence.read_text())
        self.assertTrue(smoke_tool.verify(fresh_payload))
        self.assertIn("executed_at", fresh_payload)
        aid = "att-inline-smoke-after-aba"
        fresh = ROUTE_WRITER._publish_completion_locked(self.route, node, node["id"], evidence,
            jobs=self.jobs, attempt_id=aid, attempt_metadata={**metadata, "attempt_id": aid})
        self.assertEqual(fresh["review_independence"], "degraded")
        DC.completion_marker_gate(str(self.route_file), "full-run", "start", self.agent_home, self.jobs)

    def test_native_fallback_without_a_proven_reviewer_completes_degraded(self):
        node = self.review_node
        for surface in ("claude-subagent", "codex-native-subagent"):
            for claim in (None, {"kind": "native-subagent", "transcript": str(self.base / "missing.txt")}):
                with self.subTest(surface=surface, claim=bool(claim)):
                    attempt = "att-native-degraded-" + surface + ("-invalid" if claim else "-absent")
                    evidence = self.artifact_root / (attempt + ".md")
                    evidence.write_text("Fresh limited review: " + attempt + "\n", encoding="utf-8")
                    metadata = {**self._metadata(node, attempt, "review"),
                                "registered_worker": "0", "transport": "headless",
                                "execution_surface": surface, "fallback_hop": "native-subagent"}
                    marker = ROUTE_WRITER._publish_completion_locked(
                        self.route, node, node["id"], evidence, jobs=self.jobs,
                        attempt_id=attempt, attempt_metadata=metadata, review_claim=claim)
                    self.assertEqual(marker["reviewer_kind"], "owner-inline")
                    self.assertEqual(marker["review_independence"], "degraded")
                    self.assertEqual(marker["execution_surface"], surface)
                    self.assertEqual(self.currency(marker).state, "current")

    def test_custom_registry_fallback_preserves_aba_currentness(self):
        self.select_custom_registry()
        node = self.review_node
        evidence = self.artifact_root / "custom-inline.md"
        evidence.write_text("Fresh owner review of source A\n", encoding="utf-8")
        attempt = "att-custom-inline"
        metadata = {**self._metadata(node, attempt, "owner"), "registered_worker": "0",
                    "transport": "interactive", "execution_surface": "inline", "fallback_hop": "inline"}
        marker = ROUTE_WRITER._publish_completion_locked(
            self.route, node, node["id"], evidence, jobs=self.jobs,
            attempt_id=attempt, attempt_metadata=metadata)
        source = self.repo / "engine.py"
        original = source.read_bytes()
        source.write_bytes(b"value = 'B'\n")
        self.prepare_review("att-custom-observed-B", note="dead-worker-blocked",
                            extra={"failure_class": "blocked"})
        source.write_bytes(original)
        self.assertEqual(self.currency(marker).state, "superseded")

    def test_unbound_document_without_a_resolvable_producer_keeps_legacy_identity(self):
        node = {"id": "plan-check", "kind": "review-worker", "unit": "qa/plan-review",
                "completion_gate": "code-plan-check", "dispatch_depth": 2, "depends_on": ["plan"]}
        self.route["nodes"] = [{"id": "plan", "kind": "pipeline-stage", "depends_on": []}, node]
        marker = SUBJECT.marker_subject(self.route, node, attempt_id="att-legacy-unbound",
            attempt_metadata={"registered_worker": "0", "execution_surface": "inline"},
            owner_inline=True, jobs=self.jobs,
            completion_dir=ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs))
        self.assertIsNone(marker)

    def test_native_source_test_fallback_is_test_owned_and_tracks_source(self):
        node = self.test_node
        for surface in ("claude-subagent", "codex-native-subagent"):
            with self.subTest(surface=surface):
                attempt = "att-test-" + surface
                evidence = self.artifact_root / (attempt + ".md")
                evidence.write_text("Executed the available checks; unavailable fixture remains unverified.\n",
                                    encoding="utf-8")
                metadata = {**self._metadata(node, attempt, "test"), "registered_worker": "0",
                            "transport": "headless", "execution_surface": surface,
                            "fallback_hop": "native-subagent"}
                try:
                    marker = ROUTE_WRITER._publish_completion_locked(
                        self.route, node, node["id"], evidence, jobs=self.jobs,
                        attempt_id=attempt, attempt_metadata=metadata)
                except ValueError as exc:
                    self.fail(f"native test fallback refused: {exc}")
                path = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs) / (node["id"] + ".json")
                self.assertEqual(DC.gate_currency(self.route, node, path, marker,
                    observe=True, jobs=self.jobs).state, "current")
                self.assertNotIn("review_independence", marker)
                self.assertNotEqual(marker.get("stage_authority"), "owner-closure")

    def test_document_design_and_source_fallbacks_share_currentness(self):
        import artifact_producer

        initial_route = dict(self.route)
        plan = self.artifact_root / "plan.md"
        plan.write_text("A bounded plan\n", encoding="utf-8")
        build = {"id": "build", "kind": "pipeline-stage", "unit": "design/maker",
                 "completion_gate": "design-build", "dispatch_depth": 2,
                 "write_scope": ["designs/02_tokens/**"], "depends_on": []}
        design = {"id": "visual-verify", "kind": "review-worker", "unit": "design/verifier",
                  "completion_gate": "design-verify", "dispatch_depth": 2,
                  "write_scope": ["designs/04_review/verify/**"], "depends_on": ["build"]}
        document = {"id": "plan-check", "kind": "review-worker", "unit": "qa/plan-review",
                    "completion_gate": "code-plan-check", "dispatch_depth": 2,
                    "write_scope": ["_internal/reviews/plan-check/**"], "depends_on": [],
                    "input_sources": {"plan.md": {"path": "plan.md"}}}
        cycle_dir = self.base / "design-cycle"
        tokens = cycle_dir / "artifacts/designs/02_tokens/tokens.json"
        tokens.parent.mkdir(parents=True)
        tokens.write_text('{"color":"blue"}\n', encoding="utf-8")
        cases = [("source", initial_route["nodes"], self.review_node, self.repo / "engine.py"),
                 ("document:plan", [document], document, plan),
                 ("design-build", [build, design], design, tokens)]
        for family, nodes, review, target in cases:
            self.route = {**initial_route, "nodes": nodes}
            self.route["route_hash"] = route_hash(self.route)
            self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
            self.route_file.write_text(json.dumps(self.route), encoding="utf-8")
            cycle = {"campaign_id": "camp_fixture", "cycle_id": "cyc_fixture",
                     "route_id": self.route["route_id"]}
            with patch_split(artifact_producer, "route_cycle_for", return_value=cycle), \
                 patch_split(artifact_producer, "cycle_dir", return_value=cycle_dir):
                for kind in ("owner-inline", "native-subagent"):
                    with self.subTest(family=family, reviewer=kind):
                        attempt = "att-" + family.replace(":", "-") + "-" + kind
                        evidence = self.artifact_root / (attempt + ".md")
                        evidence.write_text(f"Reviewed {family} ({kind}); no failing findings.\n", encoding="utf-8")
                        metadata = {**self._metadata(review, attempt, "owner"),
                                    "registered_worker": "0", "transport": "interactive",
                                    "execution_surface": "inline", "fallback_hop": "inline"}
                        claim = {"kind": "native-subagent", "transcript": str(evidence)} if kind == "native-subagent" else None
                        marker = ROUTE_WRITER._publish_completion_locked(
                            self.route, review, review["id"], evidence, jobs=self.jobs,
                            attempt_id=attempt, attempt_metadata=metadata, review_claim=claim)
                        subject = marker.get("source_subject") or {}
                        self.assertEqual(subject.get("family", "source") if subject else None, family)
                        path = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs) / (review["id"] + ".json")
                        self.assertEqual(DC.gate_currency(self.route, review, path, marker,
                            observe=True, jobs=self.jobs).state, "current")
                        broken = json.loads(json.dumps(marker))
                        broken["source_subject"]["inline_provenance"]["attempt_id"] = "att-foreign"
                        self.assertEqual(SUBJECT.marker_currency(self.route, review, broken,
                            path.parent, jobs=self.jobs)[0], "integrity-broken:identity-mismatch")
                target.write_bytes(target.read_bytes() + b"\nchanged\n")
                self.assertEqual(DC.gate_currency(self.route, review, path, marker,
                    observe=True, jobs=self.jobs).state, "superseded")

    def test_artifact_fallback_closes_accepted_unknown_without_fabricating_pass(self):
        setup = {"id": "release-setup", "kind": "pipeline-stage", "unit": "dev/backend",
                 "completion_gate": "ship-setup", "dispatch_depth": 2,
                 "write_scope": ["release-config/**"], "depends_on": []}
        review = {"id": "post-deploy-verify", "kind": "review-worker", "unit": "qa/test",
                  "completion_gate": "ship-post-deploy-verify", "dispatch_depth": 2,
                  "inputs": ["release-config/**"], "depends_on": ["release-setup"]}
        self.route.update(capability="autopilot-ship", nodes=[setup, review])
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.review_node = review
        self.route_file.write_text(json.dumps(self.route), encoding="utf-8")
        config = self.artifact_root / "release-config" / "deploy.json"
        config.parent.mkdir()
        config.write_text('{"release":1}\n', encoding="utf-8")
        for index in (1, 2):
            self.prepare_review(f"att-blocked-{index}", note="dead-worker-blocked",
                                extra={"failure_class": "blocked"})
        before = self.jobs.read_bytes()
        evidence = self.artifact_root / "completion.md"
        evidence.write_text("# Verification\nunverified: legacy fixture unavailable; user accepted this limit.\n",
                            encoding="utf-8")
        transcript = self.base / "native-review.txt"
        transcript.write_text("Reviewed release inputs; retained the accepted unknown.\n", encoding="utf-8")
        for surface in ("inline", "claude-subagent", "codex-native-subagent"):
            kind = "owner-inline" if surface == "inline" else "native-subagent"
            with self.subTest(surface=surface):
                attempt = "att-fallback-" + surface
                metadata = {**self._metadata(review, attempt, "owner"),
                            "registered_worker": "0", "transport": "interactive" if surface == "inline" else "headless",
                            "execution_surface": surface,
                            "fallback_hop": "inline" if surface == "inline" else "native-subagent"}
                claim = ({"kind": kind, "transcript": str(transcript)}
                         if kind == "native-subagent" else None)
                marker, row = ROUTE_WRITER.complete_node(
                    self.route, review, review["id"], evidence, jobs=self.jobs,
                    attempt_id=attempt, explicit_attempt_metadata=metadata, review_claim=claim)
                path = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs) / (review["id"] + ".json")
                currency = DC.gate_currency(self.route, review, path, marker, observe=True, jobs=self.jobs)
                self.assertEqual(currency.state, "current", currency.reason)
                self.assertEqual(marker["reviewer_kind"], kind)
                self.assertEqual(marker["review_independence"],
                                 "independent" if claim else "degraded")
                self.assertEqual(marker["round_census"]["closure_class"], "review-verdictless-bound")
                self.assertEqual(row["status"], "unregistered-complete")
                self.assertEqual(self.jobs.read_bytes(), before)  # BLOCKED history is untouched
                self.assertIn("unverified:", evidence.read_text())
                config.write_text('{"release":2}\n', encoding="utf-8")
                stale = DC.gate_currency(self.route, review, path, marker, observe=True, jobs=self.jobs)
                self.assertEqual(stale.state, "superseded")
                self.prepare_review("att-changed-" + surface, note="dead-worker-blocked",
                                    extra={"failure_class": "blocked"})
                config.write_text('{"release":1}\n', encoding="utf-8")
                self.assertEqual(DC.gate_currency(self.route, review, path, marker,
                    observe=True, jobs=self.jobs).state, "superseded")
                with self.assertRaisesRegex(ValueError, "review-source-subject-changed-before-completion"):
                    ROUTE_WRITER.complete_node(self.route, review, review["id"], evidence, jobs=self.jobs,
                        attempt_id=attempt + "-rebind", explicit_attempt_metadata=metadata, review_claim=claim)
                before = self.jobs.read_bytes()
                # New review bytes can assess the returned content, old bytes cannot be rebound.
                evidence = self.artifact_root / (surface + "-next.md")
                evidence.write_text(f"# Verification\nunverified: accepted fixture limit, reviewed again ({surface}).\n",
                                    encoding="utf-8")

    def test_design_review_subject_budget_and_marker_currentness_follow_build_content(self):
        import artifact_producer

        build = {"id": "build", "kind": "pipeline-stage", "unit": "design/maker",
                 "completion_gate": "design-build", "dispatch_depth": 2,
                 "write_scope": ["designs/02_tokens/**", "designs/03_components/**",
                                 "spec/design/02_tokens/**", "spec/design/03_components/**"],
                 "depends_on": []}
        visual = {"id": "visual-verify", "kind": "review-worker", "unit": "design/verifier",
                  "completion_gate": "design-verify", "dispatch_depth": 2,
                  "write_scope": ["designs/04_review/verify/**"], "depends_on": ["build"]}
        critic = {"id": "critic-review", "kind": "review-worker", "unit": "design/critic",
                  "completion_gate": "design-critic", "dispatch_depth": 2,
                  "write_scope": ["designs/04_review/critic/**"], "depends_on": ["build"]}
        self.route = {"schema_version": 2, "dispatch_contract_version": 3,
            "registry_digest": ROUTE_WRITER.TOPO.registry_digest(ROUTE_WRITER.TOPO.load_registry()),
            "capability": "autopilot-design", "capability_mode": "ui",
            "effective_intensity": "standard", "continuation_budget": {"review_round_cap": 2},
            "cwd": str(self.repo), "artifact_root": str(self.artifact_root),
            "nodes": [build, visual, critic]}
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.review_node = visual
        self.route_file.write_text(json.dumps(self.route, sort_keys=True), encoding="utf-8")
        cycle_dir = self.base / "design-cycle"
        output = cycle_dir / "artifacts"
        for relative, data in (("designs/02_tokens/tokens.json", b'{"color":"blue"}\n'),
                               ("designs/03_components/card.html", b"<article>Card</article>\n")):
            path = output / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        cycle = {"campaign_id": "camp_fixture", "cycle_id": "cyc_fixture",
                 "route_id": self.route["route_id"]}
        with patch_split(artifact_producer, "route_cycle_for", return_value=cycle), \
             patch_split(artifact_producer, "cycle_dir", return_value=cycle_dir):
            _first_meta, first = self.complete_review("att-design-A1")
            self.complete_review("att-design-A2")
            same = self.admission()
            self.assertEqual((same.budget.state, same.budget.verdict_rounds), ("exhausted", 2))
            (output / "designs/02_tokens/tokens.json").write_bytes(b'{"color":"green"}\n')
            b_candidate = self.prepare_review("att-design-B-no-marker")
            self.assertEqual(b_candidate["review_subject_digest"],
                             SUBJECT.read_binding(self.jobs, b_candidate)["subject_digest"])
            (output / "designs/02_tokens/tokens.json").write_bytes(b'{"color":"blue"}\n')
            a_again = self.admission()
            self.assertEqual(a_again.budget.verdict_rounds, 2)
            self.assertEqual(a_again.budget.state, "exhausted")
            self.assertEqual(self.currency(first).state, "superseded")
            legacy_closure = {"review_gate_closure": "owner-closure",
                              "review_independence": "owner-overridden"}
            self.assertIsNone(SUBJECT.marker_currency(
                self.route, self.review_node, legacy_closure,
                ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs),
                jobs=self.jobs))
            legacy_pass = {"reviewer_kind": "registered-worker", "verdict": "PASS"}
            self.assertEqual(SUBJECT.marker_currency(
                self.route, self.review_node, legacy_pass,
                ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs),
                jobs=self.jobs), ("superseded", "completion-review-subject-unknown"))

    def test_plan_review_binds_exact_existing_input_and_preserves_aba_budget(self):
        import review_input as INPUT
        review = {"id": "plan-check", "kind": "review-worker", "unit": "qa/plan-review",
                  "completion_gate": "code-plan-check", "dispatch_depth": 2,
                  "write_scope": ["_internal/reviews/plan-check/**"], "depends_on": []}
        self.route = {"schema_version": 2, "dispatch_contract_version": 3,
            "registry_digest": ROUTE_WRITER.TOPO.registry_digest(ROUTE_WRITER.TOPO.load_registry()),
            "capability": "autopilot-code", "capability_mode": "debug",
            "effective_intensity": "standard", "continuation_budget": {"review_round_cap": 2},
            "cwd": str(self.repo), "artifact_root": str(self.artifact_root),
            "nodes": [review]}
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.review_node = review
        self.route_file.write_text(json.dumps(self.route, sort_keys=True), encoding="utf-8")
        a = self.base / "plan-A.md"
        b = self.base / "plan-B.md"
        a.write_text("Plan A\n", encoding="utf-8")
        b.write_text("Plan B\n", encoding="utf-8")

        def register(attempt, evidence, *, note="completed-review-blocking"):
            candidate = INPUT.resolve_input(self.route, review, self.jobs, evidence)
            metadata = self._metadata(review, attempt, "review")
            metadata[INPUT.KEY] = INPUT.seal_binding(self.jobs, metadata, candidate)
            values = SUBJECT.seal_attempt(self.jobs, self.route, review, attempt,
                worktree=self.repo, expected_subject_digest=None, attempt_metadata=metadata)
            metadata.update(values)
            self._row(metadata, status="done", note=note)
            marker_subject = SUBJECT.marker_subject(self.route, review, attempt_id=attempt,
                attempt_metadata=metadata, jobs=self.jobs, worktree=self.repo,
                completion_dir=ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs))
            return metadata, {"attempt_id": attempt, "source_subject": marker_subject}

        self.assertFalse(SUBJECT.has_review_subject(self.route, review, self.jobs))
        _meta_a1, marker_a1 = register("att-plan-A1", a)
        _meta_a2, marker_a2 = register("att-plan-A2", a)
        self.assertEqual(DISPATCH_NODE.admit_round(self.route, review, self.jobs,
            reviewed_evidence=str(a), record_auto_revisions=False).budget.verdict_rounds, 2)
        register("att-plan-B-no-marker", b, note="dead-worker-blocked")
        a_again = DISPATCH_NODE.admit_round(self.route, review, self.jobs,
            reviewed_evidence=str(a), record_auto_revisions=False)
        self.assertEqual((a_again.budget.state, a_again.budget.verdict_rounds), ("exhausted", 2))
        before = self.jobs.read_bytes()
        with self.assertRaises(DC.DispatchContractError) as exhausted:
            self._claim_document_review("att-plan-A3", reviewed_evidence=a)
        self.assertEqual(exhausted.exception.reason, "reviewed-evidence-revision-not-admitted")
        self.assertEqual(self.jobs.read_bytes(), before)
        completion = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs)
        self.assertEqual(SUBJECT.marker_currency(self.route, review, marker_a1, completion,
                                                 jobs=self.jobs),
                         ("superseded", "completion-review-source-stale"))
        self.assertEqual(SUBJECT.marker_currency(self.route, review, marker_a2, completion,
                                                 jobs=self.jobs),
                         ("superseded", "completion-review-source-stale"))

    def test_plan_execute_test_consumes_checklist_progress_and_keeps_plan_identity(self):
        import review_input as INPUT

        plan = {"id": "plan", "kind": "pipeline-stage", "unit": "plan/plan-author",
                "completion_gate": "code-plan", "dispatch_depth": 2,
                "outputs": ["plan.md", "checklist.md"],
                "write_scope": ["plan.md", "checklist.md"], "depends_on": []}
        review = {"id": "plan-check", "kind": "review-worker", "unit": "qa/plan-review",
                  "completion_gate": "code-plan-check", "dispatch_depth": 2,
                  "inputs": ["plan.md", "checklist.md"],
                  "write_scope": ["_internal/reviews/plan-check/**"], "depends_on": ["plan"]}
        execute = {"id": "execute", "kind": "pipeline-stage", "unit": "dev/backend",
                   "completion_gate": "code-execute", "dispatch_depth": 2,
                   "write_scope": ["source/**", "checklist.md", "dev_logs/**", "evidence/**"],
                   "depends_on": ["plan-check"]}
        test = {"id": "test", "kind": "pipeline-stage", "unit": "qa/test",
                "completion_gate": "code-test", "dispatch_depth": 2,
                "write_scope": ["test_logs/**"], "depends_on": ["execute"]}
        self.route = {
            "schema_version": 2, "dispatch_contract_version": 3,
            "registry_digest": ROUTE_WRITER.TOPO.registry_digest(ROUTE_WRITER.TOPO.load_registry()),
            "capability": "autopilot-code", "capability_mode": "debug",
            "effective_intensity": "standard", "continuation_budget": {"review_round_cap": 2},
            "cwd": str(self.repo), "artifact_root": str(self.artifact_root),
            "nodes": [plan, review, execute, test],
        }
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.route_file.write_text(json.dumps(self.route, sort_keys=True), encoding="utf-8")
        self.review_node = review
        self.test_node = test

        plan_path = self.artifact_root / "plan.md"
        checklist_path = self.artifact_root / "checklist.md"

        def publish_plan(attempt, text, checklist):
            plan_path.write_text(text, encoding="utf-8")
            checklist_path.write_text(checklist, encoding="utf-8")
            return ROUTE_WRITER._publish_completion_locked(
                self.route, plan, "plan", plan_path, jobs=self.jobs,
                attempt_id=attempt, attempt_metadata=self._inline_metadata(plan, attempt),
            )

        def finish_plan_review(attempt):
            candidate = INPUT.resolve_input(self.route, review, self.jobs)
            metadata = self._metadata(review, attempt, "review")
            metadata[INPUT.KEY] = INPUT.seal_binding(self.jobs, metadata, candidate)
            metadata.update(SUBJECT.seal_attempt(
                self.jobs, self.route, review, attempt, worktree=self.repo,
                expected_subject_digest=None, attempt_metadata=metadata,
            ))
            self._row(metadata, extra={"launch_outcome": "reaped-before-publish"})
            evidence = self.artifact_root / f"{attempt}.md"
            evidence.write_text("independent plan review: PASS\n", encoding="utf-8")
            marker = ROUTE_WRITER._publish_completion_locked(
                self.route, review, review["id"], evidence, jobs=self.jobs,
                attempt_id=attempt, attempt_metadata=metadata,
            )
            return candidate, metadata, marker

        publish_plan("att-plan-A", "Plan A\n", "- [ ] implement\n")
        admission_a = DISPATCH_NODE.admit_round(
            self.route, review, self.jobs, record_auto_revisions=False)
        candidate_a, metadata_a, _marker_a = finish_plan_review("att-plan-check-A")
        digest_a = SUBJECT.read_binding(self.jobs, metadata_a)["subject_digest"]
        self.assertEqual(admission_a.source_subject_digest, digest_a)

        # A substantive plan update gets its own content budget before execute.
        publish_plan("att-plan-B", "Plan B changes the API contract\n", "- [ ] implement\n")
        admission_b = DISPATCH_NODE.admit_round(
            self.route, review, self.jobs, record_auto_revisions=False)
        candidate_b, metadata_b, marker_b = finish_plan_review("att-plan-check-B")
        digest_b = SUBJECT.read_binding(self.jobs, metadata_b)["subject_digest"]
        self.assertNotEqual(candidate_a["sha256"], candidate_b["sha256"])
        self.assertNotEqual(digest_a, digest_b)
        self.assertEqual(admission_b.source_subject_digest, digest_b)
        self.assertEqual(admission_b.budget.verdict_rounds, 0)

        # Execute consumes the current plan-check, then records routine progress
        # in the companion checklist. Test depends directly on execute.
        self.assertIsNone(DC.completion_marker_gate(
            str(self.route_file), "execute", "start", self.agent_home, self.jobs))
        checklist_path.write_text("- [x] implement\n", encoding="utf-8")
        execute_evidence = self.artifact_root / "execute-after-plan.md"
        execute_evidence.write_text("execute completed\n", encoding="utf-8")
        ROUTE_WRITER._publish_completion_locked(
            self.route, execute, "execute", execute_evidence, jobs=self.jobs,
            attempt_id="att-execute-after-plan",
            attempt_metadata=self._inline_metadata(execute, "att-execute-after-plan"),
        )
        completion = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs)
        self.assertEqual(SUBJECT.marker_currency(
            self.route, review, marker_b, completion, jobs=self.jobs),
            ("superseded", "completion-review-source-stale"))
        self.assertIsNone(DC.completion_marker_gate(
            str(self.route_file), "test", "start", self.agent_home, self.jobs))

        # Registration must use the same full subject as normal admission:
        # unchanged plan bytes plus changed consumed checklist, after two old
        # verdicts, is the first review of this subject at the unchanged cap.
        current = DISPATCH_NODE.admit_round(
            self.route, review, self.jobs, record_auto_revisions=False)
        self.assertEqual((current.budget.state, current.budget.verdict_rounds,
                          current.budget.cap, current.budget.round_kind),
                         ("admit", 0, 2, "first"))
        self.assertTrue(self._claim_document_review("att-plan-check-progress"))
        registered = self._row_metadata("att-plan-check-progress")
        self.assertEqual(INPUT.read_binding(self.jobs, registered)["sha256"],
                         candidate_b["sha256"])
        self.assertEqual(SUBJECT.read_binding(self.jobs, registered)["subject_digest"],
                         current.source_subject_digest)

    def test_handoff_consumer_requires_both_critic_group_completions(self):
        visual = {"id": "visual-verify", "kind": "review-worker", "depends_on": [],
                  "completion_gate": "design-verify", "parallel_group": "visual-verify"}
        visual_alt = {"id": "visual-verify-alternative", "kind": "review-worker", "depends_on": [],
                      "completion_gate": "design-verify", "parallel_group": "visual-verify"}
        critic = {"id": "critic-review", "kind": "review-worker", "depends_on": [],
                  "completion_gate": "design-critic", "parallel_group": "critic-review"}
        critic_alt = {"id": "critic-review-alternative", "kind": "review-worker", "depends_on": [],
                      "completion_gate": "design-critic", "parallel_group": "critic-review"}
        handoff = {"id": "handoff", "kind": "pipeline-stage", "completion_gate": "design-handoff",
                   "depends_on": [visual["id"], visual_alt["id"], critic["id"], critic_alt["id"]]}
        route = {"dispatch_contract_version": 3, "route_id": "rt-design-handoff-consumer",
                 "route_hash": "sha256:" + "d" * 64, "nodes": [visual, visual_alt, critic, critic_alt, handoff]}
        route_file = self.base / "design-handoff-route.json"
        route_file.write_text(json.dumps(route), encoding="utf-8")
        completion = self.state / "completion" / route["route_id"]
        completion.mkdir(parents=True)
        for node in (visual, visual_alt, critic, critic_alt):
            (completion / f"{node['id']}.json").write_text(json.dumps({
                "route_id": route["route_id"], "route_hash": route["route_hash"],
                "node_id": node["id"], "attempt_id": "att-" + node["id"],
            }), encoding="utf-8")
        with patch_split(DC, "dispatch_state_roots", return_value=[self.state]), \
             patch_split(DC, "completion_marker_is_current", return_value=True), \
             patch_split(DC, "completion_attempt_readiness",
                 return_value=DC.AttemptReadiness("ready", "fixture-settled", "att-peer")), \
             patch_split(DC, "_frame_pair_attempt_gate"), \
             patch_split(DC, "_human_gate_entry_fence"), \
             patch_split(DC, "_auxiliary_arbitration_gate"), \
             patch_split(DC, "_sibling_attempt_gate"):
            DC.completion_marker_gate(str(route_file), "handoff", "dry-run", self.agent_home, self.jobs)
            (completion / "critic-review-alternative.json").unlink()
            with self.assertRaises(DC.DispatchContractError) as missing:
                DC.completion_marker_gate(str(route_file), "handoff", "dry-run",
                                          self.agent_home, self.jobs)
        self.assertEqual((missing.exception.reason, missing.exception.detail),
                         ("completion-marker-" + "missing", "critic-review-alternative"))

    def test_replacement_reruns_inline_review_and_consumes_fresh_completion(self):
        import artifact_producer
        import dispatch_replacement as replacement
        import route_lineage
        from types import SimpleNamespace

        checked = {
            'parent_harness': 'claude', 'parent_transport': 'headless',
            'parent_sandbox': 'adapter-default', 'child_harness': 'codex',
            'launch_authority': 'conductor', 'status': 'supported',
            'probe_source': 'fixture-probe', 'probe_time': '2026-07-16T00:00:00Z',
            'failure_class': '', 'checked_worktree': str(self.repo),
            'failure_scope': 'none', 'codex_command': 'ok',
            'retry_on_isolated_worktree': 0,
        }
        with patch_split(ROUTE_WRITER, '_compose_readiness', return_value={
                'tuples': [checked], 'candidates': [{
                    'harness': 'codex', 'transport': 'headless',
                    'surface': 'registered-headless', 'status': 'supported',
                    'probe_source': 'fixture-probe',
                    'probe_time': '2026-07-20T00:00:00Z'}]}):
            self.route = ROUTE_WRITER.compose_route(
                capability='autopilot-code', capability_mode='debug',
                shape='staged', graph='plan,execute,impl-review,test,report',
                slug='fixture', cwd=self.repo, artifact_root=self.artifact_root,
                unassigned=True)
        ROUTE_WRITER.verify_route(self.route, expected_cwd=self.repo)
        self.review_node = self.get_node('impl-review')
        self.route_file = route_lineage.canonical_route_path(
            self.artifact_root, self.route['route_id'])
        self.route_file.parent.mkdir(parents=True, exist_ok=True)
        self.route_file.write_text(json.dumps(self.route), encoding='utf-8')
        artifact_producer.activate(
            self.artifact_root, repository_id='repo_' + 'a' * 32,
            artifact_root_id='root_' + 'b' * 32,
            w7={'campaign_id': 'camp_' + 'c' * 32},
        )
        begun = artifact_producer.begin(
            self.artifact_root, route_file=self.route_file,
            capability='autopilot-code', intensity='standard')
        self.output_dir = Path(begun['cycle_dir']) / 'artifacts'
        cycle = artifact_producer.route_cycle_for(self.artifact_root, self.route)
        self.assertTrue(artifact_producer.cycle_route_admission(
            self.artifact_root, cycle, self.route).allow)
        self.assertFalse(artifact_producer.cycle_route_admission(
            self.artifact_root, cycle,
            {**self.route, 'route_hash': 'sha256:' + '0' * 64}).allow)

        def finished_process():
            process = subprocess.Popen(['sleep', '0.05'], start_new_session=True)
            identity = {'pid': str(process.pid), 'pgid': str(os.getpgid(process.pid)),
                        'pid_start': str(DC.process_start_ticks(process.pid))}
            process.wait(timeout=3)
            return identity

        for node_id in ('plan', 'execute'):
            self.complete_node(node_id, f'att-replacement-inline-{node_id}', node_id)
            metadata = (self._row_metadata(f'att-replacement-inline-{node_id}')
                        if node_id == 'plan' and SUBJECT.has_review_subject(
                            self.route, self.get_node(node_id), self.jobs) else
                        self._metadata(self.get_node(node_id),
                                       f'att-replacement-inline-{node_id}', 'stage'))
            self._row(metadata,
                      extra=finished_process())
        node = self.review_node
        attempt = 'att-owner-inline-review'
        evidence = self.output_dir / 'owner-inline-review.md'
        evidence.write_text('historical owner review', encoding='utf-8')
        metadata = {**self._metadata(node, attempt, 'owner'),
                    'registered_worker': '0', 'execution_surface': 'inline',
                    'transport': 'interactive', 'fallback_hop': 'inline'}
        old = ROUTE_WRITER._publish_completion_locked(
            self.route, node, node['id'], evidence, jobs=self.jobs,
            attempt_id=attempt, attempt_metadata=metadata)
        # Materialize a marker published by the old writer. Both historical
        # bytes and the canonical head begin without source provenance.
        old.pop('source_subject')
        marker_dir = ROUTE_WRITER.completion_dir(self.route['route_id'], jobs=self.jobs)
        for path in (marker_dir / 'impl-review.json',
                     marker_dir / f"impl-review.{old['sequence']}.json"):
            path.write_text(json.dumps(old, sort_keys=True), encoding='utf-8')
        old_history = (marker_dir / f"impl-review.{old['sequence']}.json").read_bytes()
        self.assertEqual(old['reviewer_kind'], 'owner-inline')
        self.assertEqual(self.currency(old).reason, 'completion-source-subject-unknown')
        unknown_attempt = 'att-inline-claim-legacy-unknown'
        unknown_meta = {**self._metadata(node, unknown_attempt, 'owner'),
                        'registered_worker': '0', 'execution_surface': 'inline',
                        'transport': 'interactive', 'fallback_hop': 'inline'}
        with self.assertRaisesRegex(ValueError, '^review-source-binding-missing:'):
            ROUTE_WRITER.complete_node(
                self.route, node, node['id'], evidence,
                attempt_id=unknown_attempt, explicit_attempt_metadata=unknown_meta)
        self.assertEqual((marker_dir / f"impl-review.{old['sequence']}.json").read_bytes(),
                         old_history)
        self.complete_node('test', 'att-historical-test', 'historical test')
        self._row(self._row_metadata('att-historical-test'),
                  extra=finished_process())
        self.complete_node('report', 'att-historical-report', 'historical report')
        self._row(self._metadata(self.get_node('report'), 'att-historical-report', 'stage'),
                  extra=finished_process())
        snapshot = replacement._reuse_snapshot(self.jobs, self.route,
                                                self.jobs.read_text().splitlines())
        self.assertEqual(snapshot['pending'],
                         ['impl-review', 'test', 'report'])
        self.assertEqual([row['node'] for row in snapshot['completed']], ['plan', 'execute'])

        # Verify the sealed route, claim, answer retention, admission and reuse.
        # Only model launch and short-lived process identity are fixture seams.
        import dispatch_owner_input as owner_input
        owner = 'att-blocked-owner'
        args = SimpleNamespace(attempt_id=owner, jobs_path=self.jobs,
            worktree=str(self.repo), route_id=self.route['route_id'], route_node='plan',
            launch_lifecycle='detached', replacement_input_argv=[
                '--start', '--attempt-id', owner, '--prompt-text', 'finish the route'])
        sealed = DC.parse_registry_metadata(replacement.seal_launch_input(
            args, 'codex', 'finish the route'))
        owner_meta = {**self._metadata(node, owner, 'owner'), **sealed,
            'dispatch_depth': '1', 'route_node': 'frame', 'parent_sid': 'fixture-parent',
            'harness': 'codex', 'note': 'dead-worker-blocked', 'failure_class': 'blocked'}
        owner_meta.update(finished_process())
        self.assertEqual(DC.attempt_process_quiescence(owner_meta).state, 'quiescent')
        self._row(owner_meta, status='open', note='dead-worker-blocked',
                  extra={'failure_class': 'blocked'})
        owner_input.initialize_owner_input(self.jobs, owner, 'claude-next-turn')
        self._row(owner_meta, status='done', note='dead-worker-blocked',
                  extra={'failure_class': 'blocked'})
        answer = owner_input.submit(self.jobs, owner, 'approved: rerun fresh review', 'answer-1')
        self.assertTrue(answer['retained'])
        launches = []
        def fake_model_launch(command, **_kwargs):
            launches.append(command)
            record = replacement.claim(self.jobs, owner)
            source = replacement._rows(self.jobs.read_text().splitlines())[owner][1]
            replay = replacement.launch_input(self.jobs, owner, source)
            successor_args = SimpleNamespace(**vars(args))
            successor_args.attempt_id = record['replacement_attempt_id']
            successor_args.replacement_input_argv = replacement._replacement_argv(
                record, source, replay)
            candidate = {**owner_meta, 'attempt_id': record['replacement_attempt_id'],
                         'automatic_retry_of': owner}
            for transient in ('note', 'failure_class', 'pid', 'pid_start', 'pgid'):
                candidate.pop(transient, None)
            candidate.update(DC.parse_registry_metadata(replacement.seal_launch_input(
                successor_args, 'codex', 'finish the route')))
            self.assertEqual(replacement.admission(
                self.jobs, self.jobs.read_text().splitlines(), candidate), record)
            row = '\t'.join(('fixture', 'open', str(self.repo), str(self.repo),
                             'replacement', ','.join(f'{k}={v}' for k, v in sorted(candidate.items()))))
            self.assertTrue(DC.claim_attempt_row(
                self.jobs, candidate['attempt_id'], row, launch=False))
            registered = replacement._rows(self.jobs.read_text().splitlines())[
                candidate['attempt_id']][1]
            self.assertEqual(replacement.admission(
                self.jobs, self.jobs.read_text().splitlines(), registered), record)
            self.assertTrue(DC.claim_attempt_row(
                self.jobs, candidate['attempt_id'], row, launch=True))
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with mock.patch.dict(os.environ, {
                 'CODEX_THREAD_ID': 'fixture-parent',
                 'AGENT_DISPATCH_PARENT_SESSION_ID': 'fixture-parent'}), \
             mock.patch('dispatch_capacity_evidence.harness_hold', return_value=None):
            DC._route_module().verify_route(json.loads(self.route_file.read_text()))
            claim = replacement.claim(self.jobs, owner)
            report_path = ROUTE_WRITER.completion_dir(
                self.route['route_id'], jobs=self.jobs) / 'report.json'
            original_report = report_path.read_bytes()
            altered_report = {**json.loads(original_report), 'fixture_drift': 'after-claim'}
            report_path.write_text(json.dumps(altered_report), encoding='utf-8')
            try:
                with self.assertRaises(DC.DispatchContractError) as drift:
                    replacement.validate_claim_source(
                        self.jobs, self.jobs.read_text().splitlines(), claim)
                self.assertEqual((drift.exception.reason, drift.exception.detail),
                                 ('replacement-completion-unproven', 'report'))
            finally:
                report_path.write_bytes(original_report)
            started = replacement.advance(
                self.jobs, owner, resume_capacity=True, run=fake_model_launch)
            repeated = replacement.advance(
                self.jobs, owner, resume_capacity=True, run=fake_model_launch)
        self.assertEqual(claim['proof']['death_kind'], replacement.CORRECTED)
        self.assertEqual(started['state'], 'running')
        self.assertIn(repeated['state'], ('running', 'reused'))
        self.assertEqual(len(launches), 1)
        self.assertEqual(started['record']['replacement_attempt_id'],
                         claim['replacement_attempt_id'])
        self.assertEqual(claim['reuse']['pending'],
                         ['impl-review', 'test', 'report'])
        self.assertEqual(owner_input.retained(self.jobs, owner)[0]['text'],
                         'approved: rerun fresh review')

        fresh = 'att-fresh-owner-inline-review'
        new_evidence = self.output_dir / 'fresh-review.md'
        new_evidence.write_text('fresh owner inline review', encoding='utf-8')
        inline_meta = {**self._metadata(node, fresh, 'owner'),
                       'registered_worker': '0', 'execution_surface': 'inline',
                       'transport': 'interactive', 'fallback_hop': 'inline'}
        marker, result = ROUTE_WRITER.complete_node(
            self.route, node, node['id'], new_evidence,
            attempt_id=fresh, explicit_attempt_metadata=inline_meta)
        self.assertEqual(result['status'], 'unregistered-complete')
        self.assertEqual((marker['reviewer_kind'], marker['review_independence']),
                         ('owner-inline', 'degraded'))
        self.assertEqual(marker['sequence'], old['sequence'] + 1)
        self.assertEqual(self.currency(marker).state, 'current')
        self.assertEqual((marker_dir / f"impl-review.{old['sequence']}.json").read_bytes(),
                         old_history)
        self.assertEqual(DC.completion_marker_gate(
            str(self.route_file), 'test', 'start', self.agent_home, self.jobs), None)
        def consume_stage(node_id, attempt_id):
            stage_node = self.get_node(node_id)
            metadata = self._metadata(stage_node, attempt_id, 'stage')
            if SUBJECT.has_review_subject(self.route, stage_node, self.jobs):
                metadata.update(SUBJECT.seal_attempt(
                    self.jobs, self.route, stage_node, attempt_id,
                    worktree=self.repo))
            self._row(metadata,
                      status='running', note='launch-started',
                      extra=finished_process())
            evidence = self.output_dir / f'{attempt_id}.md'
            evidence.write_text(f'fresh {node_id}', encoding='utf-8')
            marker, result = ROUTE_WRITER.complete_node(
                self.route, stage_node, node_id, evidence,
                jobs=self.jobs, attempt_id=attempt_id)
            self.assertEqual(result['status'], 'closed')
            return marker

        consume_stage('test', 'att-fresh-test')
        self.assertEqual(DC.gate_currency(
            self.route, self.get_node('test'),
            ROUTE_WRITER.completion_dir(self.route['route_id'], jobs=self.jobs) / 'test.json',
            jobs=self.jobs).state, 'current')
        consume_stage('report', 'att-fresh-report')
        self.assertEqual(DC.gate_currency(
            self.route, self.get_node('report'),
            ROUTE_WRITER.completion_dir(self.route['route_id'], jobs=self.jobs) / 'report.json',
            jobs=self.jobs).state, 'current')

        completed_snapshot = replacement._reuse_snapshot(
            self.jobs, self.route, self.jobs.read_text().splitlines())
        self.assertEqual(completed_snapshot['pending'], [])
        self.assertEqual([item['node'] for item in completed_snapshot['completed']],
                         ['plan', 'execute', 'impl-review', 'test', 'report'])
        self.assertEqual(self.currency(marker).state, 'current')
        self.assertEqual(replacement._reuse_snapshot(
            self.jobs, self.route, self.jobs.read_text().splitlines())['pending'], [])

        bad = json.loads(json.dumps(marker))
        bad['source_subject']['inline_provenance'].pop('manifest_sha256')
        self.assertEqual(SUBJECT.marker_currency(
            self.route, node, bad, marker_dir, jobs=self.jobs),
            ('integrity-broken:identity-mismatch', 'completion-review-inline-provenance-invalid'))
        unbound = json.loads(json.dumps(marker))
        unbound['source_subject'].pop('inline_provenance')
        self.assertEqual(SUBJECT.marker_currency(
            self.route, node, unbound, marker_dir, jobs=self.jobs),
            ('superseded', 'completion-review-source-binding-unknown'))

        forged_markers = []
        for field, value in (
                ('attempt_id', 'att-other-attempt'),
                ('reviewer_identity', 'att-other-reviewer'),
                ('reviewer_kind', 'registered-worker'),
                ('review_independence', 'independent'),
                ('registered_worker', True),
                ('execution_surface', 'registered')):
            forged = json.loads(json.dumps(marker))
            forged[field] = value
            forged_markers.append((field, forged))
        forged = json.loads(json.dumps(marker))
        forged['source_subject']['inline_provenance']['attempt_id'] = 'att-other-provenance'
        forged_markers.append(('inline_provenance.attempt_id', forged))
        for identity_field, forged in forged_markers:
            with self.subTest(identity_field=identity_field):
                self.assertEqual(SUBJECT.marker_currency(
                    self.route, node, forged, marker_dir, jobs=self.jobs),
                    ('integrity-broken:identity-mismatch',
                     'completion-review-inline-provenance-invalid'))

        (self.repo / 'engine.py').write_text("value = 'after-inline'\n", encoding='utf-8')
        self.assertEqual(self.currency(marker).reason, 'completion-review-source-stale')
        b_attempt = 'att-owner-inline-review-B'
        b_evidence = self.output_dir / 'fresh-review-B.md'
        b_evidence.write_text('fresh owner inline review at source B', encoding='utf-8')
        b_metadata = {**self._metadata(node, b_attempt, 'owner'),
                      'registered_worker': '0', 'execution_surface': 'inline',
                      'transport': 'interactive', 'fallback_hop': 'inline'}
        b_marker, b_result = ROUTE_WRITER.complete_node(
            self.route, node, node['id'], b_evidence,
            attempt_id=b_attempt, explicit_attempt_metadata=b_metadata)
        self.assertEqual(b_result['status'], 'unregistered-complete')
        self.assertEqual(b_marker['source_subject']['epoch'], marker['source_subject']['epoch'] + 1)
        self.assertEqual(self.currency(b_marker).state, 'current')
        (self.repo / 'engine.py').write_text("value = 'A'\n", encoding='utf-8')
        source_a_again = SUBJECT.observe(self.route, node, worktree=self.repo)
        self.assertEqual(source_a_again['digest'], marker['source_subject']['digest'])
        self.assertEqual(SUBJECT.source_epoch(
            self.route, node, source_a_again['digest'], marker_dir),
            marker['source_subject']['epoch'] + 2)
        self.assertEqual(SUBJECT.marker_currency(
            self.route, node, marker, marker_dir, jobs=self.jobs),
            ('superseded', 'completion-review-source-stale'))
        self.assertEqual(self.currency(b_marker).reason, 'completion-review-source-stale')
        b_history_path = marker_dir / f"impl-review.{b_marker['sequence']}.json"
        b_history_before = b_history_path.read_bytes()
        b_canonical_before = (marker_dir / 'impl-review.json').read_bytes()
        replay_marker, replay_result = ROUTE_WRITER.complete_node(
            self.route, node, node['id'], b_evidence,
            attempt_id=b_attempt, explicit_attempt_metadata=b_metadata)
        self.assertEqual(replay_result['status'], 'unregistered-complete')
        self.assertEqual(replay_marker, b_marker)
        self.assertEqual(b_history_path.read_bytes(), b_history_before)

        for attempt_id, evidence_path in (
                ('att-inline-reuse-old-evidence-path', b_evidence),
                ('att-inline-reuse-copied-old-evidence', self.output_dir / 'copied-review-B.md'),
                ('att-inline-reuse-old-A-epoch', new_evidence)):
            if evidence_path != b_evidence:
                evidence_path.write_bytes(b_evidence.read_bytes())
            metadata = {**self._metadata(node, attempt_id, 'owner'),
                        'registered_worker': '0', 'execution_surface': 'inline',
                        'transport': 'interactive', 'fallback_hop': 'inline'}
            with self.subTest(attempt_id=attempt_id):
                with self.assertRaisesRegex(
                        ValueError, '^review-source-subject-changed-before-completion:'):
                    ROUTE_WRITER.complete_node(
                        self.route, node, node['id'], evidence_path,
                        attempt_id=attempt_id, explicit_attempt_metadata=metadata)
                self.assertEqual((marker_dir / 'impl-review.json').read_bytes(),
                                 b_canonical_before)
                self.assertEqual(b_history_path.read_bytes(), b_history_before)
        pending = replacement._reuse_snapshot(
            self.jobs, self.route, self.jobs.read_text().splitlines())['pending']
        self.assertIn('impl-review', pending)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.com")
        self.git("config", "user.name", "Fixture")
        (self.repo / "engine.py").write_text("value = 'A'\n", encoding="utf-8")
        self.git("add", "engine.py")
        self.git("commit", "-qm", "source A")
        self.artifact_root = self.base / ".agent_reports"
        self.artifact_root.mkdir()
        self.state = self.base / "dispatch"
        self.state.mkdir()
        self.jobs = self.state / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.agent_home = self.base / "agent-home"
        (self.agent_home / "core").mkdir(parents=True)
        (self.agent_home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        self.stable_home = self.base / "home"
        self.stable_home.mkdir()
        self.route = {
            "schema_version": 2, "dispatch_contract_version": 3,
            "route_id": "", "route_hash": "", "registry_digest": ROUTE_WRITER.TOPO.registry_digest(ROUTE_WRITER.TOPO.load_registry()),
            "capability": "autopilot-code", "capability_mode": "debug",
            "effective_intensity": "standard",
            "continuation_budget": {"review_round_cap": 2},
            "cwd": str(self.repo), "artifact_root": str(self.artifact_root),
            "nodes": [
                {"id": "execute", "kind": "pipeline-stage", "unit": "dev/backend",
                 "completion_gate": "code-execute", "dispatch_depth": 2,
                 "write_scope": ["source/**", "checklist.md", "dev_logs/**", "evidence/**"],
                 "depends_on": []},
                {"id": "smoke", "kind": "pipeline-stage", "unit": "qa/test",
                 "completion_gate": "code-test", "dispatch_depth": 2,
                 "write_scope": ["test_logs/**"], "depends_on": ["execute"]},
                {"id": "impl-review", "kind": "review-worker", "unit": "qa/code-review",
                 "completion_gate": "code-impl-review", "dispatch_depth": 2,
                 "write_scope": ["_internal/dev_reviews/**"], "depends_on": ["smoke"]},
                {"id": "test", "kind": "pipeline-stage", "unit": "qa/test",
                 "completion_gate": "code-test", "dispatch_depth": 2,
                 "write_scope": ["test_logs/**"], "depends_on": ["impl-review"]},
            ],
        }
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.review_node = self.route["nodes"][2]
        self.test_node = self.route["nodes"][3]
        self.route_file = self.base / "route.json"
        self.route_file.write_text(json.dumps(self.route, sort_keys=True), encoding="utf-8")
        env = {
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_HOME": str(self.agent_home),
            "AGENT_ARTIFACT_ROOT": str(self.artifact_root),
            "AGENT_ARTIFACT_CHECKPOINT": "off",
            "AGENT_DISPATCH_ATTEMPT_ID": "",
            "HOME": str(self.stable_home),
        }
        self.env = mock.patch.dict(os.environ, env, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def select_custom_registry(self):
        custom = self.state / "route-registry.log"
        self.jobs.rename(custom)
        self.jobs = custom
        os.environ["AGENT_DISPATCH_JOBS"] = str(custom)

        # Build the ordinary sealed tuple, then seal the exact custom filename
        # accepted by root-explicit and inherited registry selection.
        launch = ROUTE_WRITER.launch_compatibility_tuple(
            artifact_root=self.artifact_root, jobs=custom, cwd=self.repo,
        )
        jobs_identity = launch["jobs_path"]
        jobs_identity["path"] = str(custom.resolve())
        jobs_identity["binding_digest"] = ROUTE_WRITER._sha256_record({
            "kind": "jobs_path", "path": str(custom.resolve()),
            "release_id": jobs_identity["release_id"],
            "content_digest": jobs_identity["content_digest"],
        })
        self.route["launch_compatibility_tuple"] = {
            "contract_version": 1, **launch,
        }
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.review_node = self.route["nodes"][2]
        self.route_file.write_text(json.dumps(self.route, sort_keys=True), encoding="utf-8")

        missing_ambient = self.base / "ambient-missing" / "jobs.log"
        explicit = DC.resolve_global_registry(
            self.agent_home, str(custom), 1, "start",
            environ={"AGENT_DISPATCH_JOBS": str(missing_ambient), "HOME": str(self.stable_home)},
        )
        inherited = DC.resolve_global_registry(
            self.agent_home, None, 2, "start",
            environ={"AGENT_DISPATCH_JOBS": str(custom), "HOME": str(self.stable_home)},
        )
        self.assertEqual((explicit.path, explicit.source), (custom.resolve(), "root-explicit"))
        self.assertEqual((inherited.path, inherited.source), (custom.resolve(), "inherited-env"))

    def get_node(self, node_id):
        return next(item for item in self.route["nodes"] if item["id"] == node_id)

    def _metadata(self, node, attempt_id, worker_type):
        return {
            "attempt_id": attempt_id, "attempt_schema_version": 2,
            "dispatch_depth": 2, "transport": "headless",
            "execution_surface": "registered-headless", "registered_worker": "1",
            "fallback_hop": "same-harness-headless", "worker_type": worker_type,
            "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
            "route_node": node["id"], "route_file": str(self.route_file),
            "registry_digest": self.route["registry_digest"],
        }

    def _claim_document_review(self, attempt_id, *, reviewed_evidence=None):
        import review_input as INPUT

        admission = DISPATCH_NODE.admit_round(
            self.route, self.review_node, self.jobs,
            reviewed_evidence=str(reviewed_evidence) if reviewed_evidence is not None else None,
            record_auto_revisions=False)
        self.assertIsNotNone(admission.source_subject_digest)
        candidate = INPUT.resolve_input(
            self.route, self.review_node, self.jobs, reviewed_evidence)
        metadata = self._metadata(self.review_node, attempt_id, "review")
        metadata.update({
            "capability": "autopilot-code", "capability_mode": "debug",
            "qa": "standard", "intensity": "standard", "unit": self.review_node["unit"],
            "write_scope": "_internal/reviews/plan-check/**",
            "completion_gate": self.review_node["completion_gate"],
            "artifact_root": str(self.artifact_root), "log_file": str(self.base / "worker.log"),
            "launch_home": str(self.agent_home), "launch_authority": "conductor",
            "launch_fence": "registry-v1",
        })
        metadata[INPUT.KEY] = INPUT.seal_binding(self.jobs, metadata, candidate)
        row = "\t".join(("fixture", "open", str(self.repo), str(self.repo), attempt_id,
                         ",".join(f"{key}={value}" for key, value in metadata.items())))
        with mock.patch.dict(os.environ, {
                "AGENT_REVIEW_SOURCE_SUBJECT_DIGEST": admission.source_subject_digest}):
            return DC.claim_attempt_row(self.jobs, attempt_id, row, launch=False)

    def complete_node(self, node_id, attempt_id, evidence_text):
        node = self.get_node(node_id)
        evidence = getattr(self, "output_dir", self.artifact_root) / f"{attempt_id}.md"
        evidence.write_text(evidence_text, encoding="utf-8")
        metadata = self._metadata(node, attempt_id, "stage")
        if SUBJECT.has_review_subject(self.route, node, self.jobs):
            metadata.update(SUBJECT.seal_attempt(
                self.jobs, self.route, node, attempt_id, worktree=self.repo,
            ))
            self._row(metadata)
        return ROUTE_WRITER._publish_completion_locked(
            self.route, node, node_id, evidence, jobs=self.jobs,
            attempt_id=attempt_id, attempt_metadata=metadata,
        )

    def revise_execute(self, attempt_id, evidence_text):
        evidence = getattr(self, "output_dir", self.artifact_root) / f"{attempt_id}.md"
        evidence.write_text(evidence_text, encoding="utf-8")
        return ROUTE_WRITER.publish_revision_locked(
            self.route, "execute", evidence, basis="user-direction",
            direction="fixture-direction", author_attempt_id=attempt_id, jobs=self.jobs,
        )

    def _row(self, metadata, *, status="done", note="completed-marker", extra=None):
        record = {**metadata, "note": note, "failure_class": "pass", **(extra or {})}
        row = "\t".join(("fixture", status, str(self.repo), str(self.repo),
                         record["attempt_id"], ",".join(f"{k}={v}" for k, v in sorted(record.items()))))
        lines = self.jobs.read_text(encoding="utf-8").splitlines()
        matches = [i for i, line in enumerate(lines)
                   if len(line.split("\t")) == 6
                   and DC.parse_registry_metadata(line.split("\t")[5]).get("attempt_id") == record["attempt_id"]]
        if matches:
            lines[matches[0]] = row
            self.jobs.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            with self.jobs.open("a", encoding="utf-8") as stream:
                stream.write(row + "\n")
        return record

    def prepare_review(self, attempt_id, *, status="done", note="completed-marker", extra=None):
        metadata = self._metadata(self.review_node, attempt_id, "review")
        binding = SUBJECT.seal_attempt(self.jobs, self.route, self.review_node, attempt_id,
                                       worktree=self.repo)
        metadata.update(binding)
        return self._row(metadata, status=status, note=note, extra=extra)

    def complete_review(self, attempt_id, *, evidence_text="independent PASS"):
        metadata = self.prepare_review(attempt_id)
        evidence = getattr(self, "output_dir", self.artifact_root) / f"{attempt_id}.md"
        evidence.write_text(evidence_text, encoding="utf-8")
        marker = ROUTE_WRITER._publish_completion_locked(
            self.route, self.review_node, self.review_node["id"], evidence, jobs=self.jobs,
            attempt_id=attempt_id, attempt_metadata=metadata,
        )
        return metadata, marker

    def currency(self, marker):
        path = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs) / "impl-review.json"
        return DC.gate_currency(self.route, self.review_node, path, marker, observe=True)

    def admission(self):
        return DISPATCH_NODE.admit_round(self.route, self.review_node, self.jobs, record_auto_revisions=False)

    def test_admission(self):
        return DISPATCH_NODE.admit_round(self.route, self.test_node, self.jobs, record_auto_revisions=False)

    def current_smoke_markers(self, prefix):
        self.complete_node("execute", f"att-{prefix}-execute", f"execute {prefix}")
        self.complete_node("smoke", f"att-{prefix}-smoke", f"smoke {prefix}")

    def test_A_passes_do_not_consume_B_or_C_and_A_does_not_revive(self):
        self.current_smoke_markers("A1")
        _m1, a1 = self.complete_review("att-review-A1")
        _m2, a2 = self.complete_review("att-review-A2")
        self.assertEqual(self.currency(a2).state, "current")
        same_a = self.admission()
        self.assertEqual((same_a.budget.state, same_a.budget.verdict_rounds), ("exhausted", 2))
        prior_rows = DISPATCH_NODE.prior_round_attempts(
            self.jobs, self.route["route_id"], self.review_node["id"], route=self.route)
        same_source_prompt = DISPATCH_NODE.round_protocol_block(
            same_a.budget, prior_rows, "review", self.review_node["id"])
        self.assertIn("This is a correction round", same_source_prompt)

        # A pre-change PASS attempt finishes after B. Its exact launch binding
        # is retained and must not overwrite the canonical B completion.
        late_a_values = SUBJECT.seal_attempt(self.jobs, self.route, self.review_node,
                                             "att-review-A-late", worktree=self.repo)
        late_a_meta = {**self._metadata(self.review_node, "att-review-A-late", "review"), **late_a_values}
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        self.current_smoke_markers("B1")
        self.assertEqual(self.currency(a2).state, "superseded")
        b_admission = self.admission()
        self.assertEqual((b_admission.budget.state, b_admission.budget.verdict_rounds,
                          b_admission.budget.round_kind), ("admit", 0, "first"))
        new_source_prompt = DISPATCH_NODE.round_protocol_block(
            b_admission.budget, prior_rows, "review", self.review_node["id"])
        # The lineage's latest verdict passed, so changed content is new work:
        # an independent first review, not a closure check.
        self.assertIn("independent first review", new_source_prompt)
        self.assertIn("Earlier findings are context only", new_source_prompt)
        _bmeta, b1 = self.complete_review("att-review-B1")
        self.assertEqual(self.currency(b1).state, "current")
        canonical_before_late_a = (ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs)
                                   / "impl-review.json").read_bytes()
        late_path = self.artifact_root / "att-review-A-late.md"
        late_path.write_text("late old source PASS", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "review-source-subject-changed-before-completion"):
            ROUTE_WRITER._publish_completion_locked(
                self.route, self.review_node, self.review_node["id"], late_path, jobs=self.jobs,
                attempt_id="att-review-A-late", attempt_metadata=late_a_meta,
            )
        self.assertEqual(canonical_before_late_a,
                         (ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs)
                          / "impl-review.json").read_bytes())

        self.complete_review("att-review-B2")
        self.assertEqual(self.admission().budget.state, "exhausted")
        # Republishing the same source through ordinary completion preserves
        # the content epoch and cannot replenish B's two verdicts.
        self.complete_node("execute", "att-B-republish-execute", "execute B replay")
        self.complete_node("smoke", "att-B-republish-smoke", "smoke B replay")
        self.assertEqual(self.admission().budget.state, "exhausted")

        # A->B->A changes the source epoch, so historical A cannot revive;
        # its content budget remains exhausted. C then gets its own ordinary
        # first review after normal execute/smoke completion.
        (self.repo / "engine.py").write_text("value = 'A'\n", encoding="utf-8")
        self.current_smoke_markers("A2")
        self.assertEqual(self.currency(a2).state, "superseded")
        self.assertEqual(self.admission().budget.state, "exhausted")
        (self.repo / "engine.py").write_text("value = 'C'\n", encoding="utf-8")
        self.current_smoke_markers("C1")
        c_admission = self.admission()
        self.assertEqual((c_admission.budget.state, c_admission.budget.verdict_rounds,
                          c_admission.budget.round_kind), ("admit", 0, "first"))

        fields = CAP.recovery_fields("review-worker", "exhausted", route=self.route,
            node=self.review_node, jobs=self.jobs, route_file=self.route_file)
        self.assertEqual(fields["recovery_surface"], "owner-report")
        self.assertNotIn("recovery_check_command", fields)
        self.assertNotIn("recovery_complete_command", fields)
        # A PASS attempt remains immutable; the existing writer still rejects
        # new closure evidence linked to that exact PASS.
        different = self.artifact_root / "att-review-B2.owner-closure.md"
        different.write_text("owner disposition", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "immutable attempt completion differs from existing link"):
            ROUTE_WRITER._publish_completion_locked(
                self.route, self.review_node, self.review_node["id"], different, jobs=self.jobs,
                attempt_id="att-review-B2", attempt_metadata={
                    **self._metadata(self.review_node, "att-review-B2", "review"),
                    **SUBJECT.read_binding(self.jobs, self._row_metadata("att-review-B2")),
                }, owner_override=True,
            )

    def test_corrected_source_after_blocking_review_is_a_closure_check_with_exact_materials(self):
        import base64

        self.current_smoke_markers("A1")
        review_file = self.artifact_root / "round_1.md"
        review_file.write_text("🔴 engine value is wrong\n", encoding="utf-8")
        encoded = base64.urlsafe_b64encode(str(review_file).encode("utf-8")).decode("ascii").rstrip("=")
        self.prepare_review("att-review-A-blocked", note="completed-review-blocking",
                            extra={"failure_class": "fail", "review_artifact_b64": encoded})
        # The correction changes the reviewed source, which used to restart a
        # full first review on the new subject.
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        self.current_smoke_markers("B1")
        admission = self.admission()
        self.assertEqual((admission.budget.state, admission.budget.verdict_rounds,
                          admission.budget.round_kind), ("admit", 0, "closure-check"))
        rows = DISPATCH_NODE.prior_round_attempts(
            self.jobs, self.route["route_id"], self.review_node["id"], route=self.route)
        block = DISPATCH_NODE.round_protocol_block(
            admission.budget, rows, "review", self.review_node["id"],
            jobs=self.jobs, current_manifest=admission.source_subject_manifest)
        self.assertIn("This is a correction round", block)
        self.assertIn(f"Prior blocking review (att-review-A-blocked): `{review_file}`", block)
        self.assertRegex(block, r"Delta since that review \(reviewed at `[0-9a-f]{12}`\): `engine.py`\n")
        self.assertNotIn("independent first review", block)
        # Without the runtime records the block still renders, naming no path.
        bare = DISPATCH_NODE.round_protocol_block(admission.budget, rows, "review", self.review_node["id"])
        self.assertNotIn("Delta since that review", bare)

        # The closure check on B blocks again; the next correction is still
        # admitted inside its own subject budget as a closure check.
        self.prepare_review("att-review-B-blocked", note="completed-review-blocking",
                            extra={"failure_class": "fail"})
        (self.repo / "engine.py").write_text("value = 'C'\n", encoding="utf-8")
        self.current_smoke_markers("C1")
        again = self.admission()
        self.assertEqual((again.budget.state, again.budget.verdict_rounds, again.budget.round_kind),
                         ("admit", 0, "closure-check"))

    def _row_metadata(self, attempt_id):
        for line in self.jobs.read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            if len(fields) == 6:
                metadata = DC.parse_registry_metadata(fields[5])
                if metadata.get("attempt_id") == attempt_id:
                    return metadata
        raise AssertionError(attempt_id)

    def test_review_can_finish_before_its_source_upstream_marker(self):
        self.current_smoke_markers("A")
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        _meta, b_pass = self.complete_review("att-review-B-before-smoke")
        self.assertEqual(b_pass["source_subject"]["epoch"], 1)
        self.assertEqual(self.currency(b_pass).state, "current")
        self.current_smoke_markers("B")
        self.assertEqual(self.currency(b_pass).state, "current")

    def test_merge_conflict_keeps_source_currency_unreadable_and_history_intact(self):
        self.current_smoke_markers("merge-A")
        _metadata, marker = self.complete_review("att-review-merge-A")
        directory = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs)
        marker_path = directory / "impl-review.json"
        marker_bytes = marker_path.read_bytes()
        history_bytes = (directory / "impl-review.1.json").read_bytes()
        registry_bytes = self.jobs.read_bytes()

        base_branch = self.git("branch", "--show-current")
        self.git("checkout", "-qb", "merge-source")
        (self.repo / "engine.py").write_text("value = 'incoming'\n", encoding="utf-8")
        self.git("commit", "-qam", "incoming source")
        self.git("checkout", "-q", base_branch)
        (self.repo / "engine.py").write_text("value = 'current'\n", encoding="utf-8")
        self.git("commit", "-qam", "current source")
        merge = subprocess.run(["git", "-C", str(self.repo), "merge", "--no-ff", "--no-commit",
                                "merge-source"], capture_output=True, text=True)
        self.assertNotEqual(merge.returncode, 0)
        git_dir = Path(self.git("rev-parse", "--git-dir"))
        if not git_dir.is_absolute():
            git_dir = self.repo / git_dir
        merge_head = (git_dir / "MERGE_HEAD").read_bytes()
        index = (git_dir / "index").read_bytes()
        source = (self.repo / "engine.py").read_bytes()

        currency = DC.gate_currency(self.route, self.review_node, marker_path, marker,
                                    observe=True, jobs=self.jobs)
        self.assertEqual((currency.state, currency.reason),
                         ("completion-evidence-unreadable", "review-source-index-unmerged"))
        self.assertEqual(marker_path.read_bytes(), marker_bytes)
        self.assertEqual((directory / "impl-review.1.json").read_bytes(), history_bytes)
        self.assertEqual(self.jobs.read_bytes(), registry_bytes)
        self.assertEqual((git_dir / "MERGE_HEAD").read_bytes(), merge_head)
        self.assertEqual((git_dir / "index").read_bytes(), index)
        self.assertEqual((self.repo / "engine.py").read_bytes(), source)

    def test_simultaneous_B_upstream_and_review_completions_converge(self):
        self.current_smoke_markers("A")
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        execute = self.get_node("execute")
        execute_id = "att-execute-B-barrier"
        execute_meta = self._metadata(execute, execute_id, "stage")
        self._row(execute_meta, note="completed-supervisor")
        execute_evidence = self.artifact_root / f"{execute_id}.md"
        execute_evidence.write_text("execute B barrier", encoding="utf-8")

        review_id = "att-review-B-barrier"
        review_meta = self._metadata(self.review_node, review_id, "review")
        review_meta.update(SUBJECT.seal_attempt(self.jobs, self.route, self.review_node,
            review_id, worktree=self.repo))
        self._row(review_meta, note="completed-supervisor")
        review_evidence = self.artifact_root / f"{review_id}.md"
        review_evidence.write_text("independent B barrier review", encoding="utf-8")

        barrier = threading.Barrier(2)

        def complete(node, attempt, evidence):
            barrier.wait(timeout=5)
            return ROUTE_WRITER.complete_node(
                self.route, node, node["id"], evidence, self.jobs, attempt)

        with ThreadPoolExecutor(max_workers=2) as pool:
            execute_future = pool.submit(complete, execute, execute_id, execute_evidence)
            review_future = pool.submit(complete, self.review_node, review_id, review_evidence)
            execute_marker, _ = execute_future.result(timeout=15)
            review_marker, _ = review_future.result(timeout=15)
        self.assertEqual(execute_marker["source_subject"]["digest"],
                         review_marker["source_subject"]["digest"])
        self.assertEqual(execute_marker["source_subject"]["epoch"],
                         review_marker["source_subject"]["epoch"])
        self.assertEqual(self.currency(review_marker).state, "current")

    def test_revision_preserves_matching_B_pass_and_same_source_revision_keeps_budget(self):
        self.current_smoke_markers("A")
        self.complete_review("att-review-A")
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        _b_meta, b_pass = self.complete_review("att-review-B-before-revision")
        self.assertEqual(self.currency(b_pass).state, "current")

        first_revision = self.revise_execute("att-execute-revision-B", "execute evidence for B")
        self.assertNotIn("impl-review", first_revision["tombstoned"])
        self.assertEqual(self.currency(b_pass).state, "current")
        self.complete_node("smoke", "att-smoke-B-after-revision", "smoke B")

        # A second revision republishes the same source content. Existing
        # revision semantics reopen the dependent review, while its prior B
        # verdict still spends one unit of B's two verdict budget.
        second_revision = self.revise_execute("att-execute-revision-B-same", "second execute evidence for B")
        self.assertIn("impl-review", second_revision["tombstoned"])
        admission = self.admission()
        self.assertEqual(admission.budget.verdict_rounds, 1)
        self.assertEqual(admission.budget.round_kind, "closure-check")

    def test_same_content_revision_after_two_passes_keeps_public_admission_exhausted(self):
        self.current_smoke_markers("A-cap")
        _first_meta, first = self.complete_review("att-review-A-cap-1")
        _second_meta, second = self.complete_review("att-review-A-cap-2")
        digest_before = SUBJECT.observe(self.route, self.review_node, worktree=self.repo)["digest"]

        revision = self.revise_execute(
            "att-execute-A-same-content-after-cap",
            "revised upstream evidence without source changes",
        )
        self.complete_node("smoke", "att-smoke-A-after-same-content-revision", "smoke A after revision")
        digest_after = SUBJECT.observe(self.route, self.review_node, worktree=self.repo)["digest"]

        self.assertEqual(digest_after, digest_before)
        self.assertIn("impl-review", revision["tombstoned"])
        self.assertEqual(self.currency(first).state, "current")
        self.assertEqual(self.currency(second).state, "current")
        budget = self.admission().budget
        self.assertEqual((budget.state, budget.verdict_rounds, budget.cap), ("exhausted", 2, 2))
        self.assertNotEqual(budget.round_kind, "closure-check")

    def test_marker_currency_rejects_registry_and_manifest_binding_tamper(self):
        self.current_smoke_markers("A")
        metadata, marker = self.complete_review("att-review-bound")
        lines = self.jobs.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) == 6 and fields[4] == metadata["attempt_id"]:
                entries = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
                entries["review_subject_binding_digest"] = "0" * 64
                fields[5] = ",".join(f"{key}={value}" for key, value in entries.items())
                lines[index] = "\t".join(fields)
        self.jobs.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(self.currency(marker).reason,
                         "completion-review-source-row-binding-mismatch")

        self._row(metadata)
        subject_path = SUBJECT.binding_path(self.jobs, metadata["attempt_id"])
        record = json.loads(subject_path.read_text(encoding="utf-8"))
        record["manifest"][0]["content_sha256"] = "0" * 64
        subject_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        self.assertEqual(self.currency(marker).reason,
                         "completion-review-source-binding-missing")

    def test_marker_currency_maps_non_object_binding_sidecars_to_integrity_failure(self):
        _metadata, marker = self.complete_review("att-review-non-object-binding")
        subject_path = SUBJECT.binding_path(self.jobs, marker["attempt_id"])
        self.assertEqual(self.currency(marker).state, "current")

        for malformed in ([], "sidecar", None):
            with self.subTest(malformed=malformed):
                subject_path.write_text(json.dumps(malformed), encoding="utf-8")
                result = self.currency(marker)
                self.assertEqual((result.state, result.reason),
                                 ("integrity-broken:identity-mismatch",
                                  "completion-review-source-binding-missing"))

        subject_path.unlink()
        missing = self.currency(marker)
        self.assertEqual((missing.state, missing.reason),
                         ("integrity-broken:identity-mismatch",
                          "completion-review-source-binding-missing"))

    def test_marker_currency_uses_sealed_registry_under_ambient_mismatch(self):
        self.current_smoke_markers("sealed-registry")
        metadata, marker = self.complete_review("att-review-sealed-registry")
        sealed_before = self.jobs.read_bytes()

        missing_ambient = self.base / "missing-ambient" / "jobs.log"
        os.environ["AGENT_DISPATCH_JOBS"] = str(missing_ambient)
        missing_result = self.currency(marker)

        conflicting_ambient = self.base / "conflicting-ambient" / "jobs.log"
        conflicting_ambient.parent.mkdir(parents=True)
        row = next(line for line in self.jobs.read_text(encoding="utf-8").splitlines()
                   if DC.parse_registry_metadata(line.split("\t")[5]).get("attempt_id")
                   == metadata["attempt_id"])
        fields = row.split("\t")
        row_metadata = DC.parse_registry_metadata(fields[5])
        row_metadata["review_subject_binding_digest"] = "0" * 64
        fields[5] = ",".join(f"{key}={value}" for key, value in sorted(row_metadata.items()))
        conflicting_bytes = ("\t".join(fields) + "\n").encode("utf-8")
        conflicting_ambient.write_bytes(conflicting_bytes)
        os.environ["AGENT_DISPATCH_JOBS"] = str(conflicting_ambient)
        conflicting_result = self.currency(marker)

        self.assertFalse(missing_ambient.exists(), "currency must not create ambient registry")
        self.assertEqual(conflicting_ambient.read_bytes(), conflicting_bytes,
                         "currency must not mutate the conflicting ambient registry")
        self.assertEqual(self.jobs.read_bytes(), sealed_before,
                         "currency must only read the route-bound registry")
        self.assertEqual((missing_result.state, conflicting_result.state), ("current", "current"),
                         (missing_result, conflicting_result))

    def test_custom_selected_registry_currency_and_continuation_ignore_ambient_jobs(self):
        self.select_custom_registry()
        self.current_smoke_markers("custom-registry")
        metadata, marker = self.complete_review("att-review-custom-registry")
        marker_path = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs) / "impl-review.json"
        sealed_before = self.jobs.read_bytes()

        missing_ambient = self.base / "ambient-custom-missing" / "jobs.log"
        os.environ["AGENT_DISPATCH_JOBS"] = str(missing_ambient)
        direct_missing = DC.gate_currency(
            self.route, self.review_node, marker_path, marker, observe=True,
        )
        continuation_missing = ROUTE_WRITER._continuation_reused_evidence(
            self.route, self.review_node,
        )

        conflicting_ambient = self.base / "ambient-custom-conflicting" / "jobs.log"
        conflicting_ambient.parent.mkdir(parents=True)
        fields = next(
            line.split("\t") for line in sealed_before.decode().splitlines()
            if DC.parse_registry_metadata(line.split("\t")[5]).get("attempt_id") == metadata["attempt_id"]
        )
        conflicting_row = DC.parse_registry_metadata(fields[5])
        conflicting_row["review_subject_binding_digest"] = "0" * 64
        fields[5] = ",".join(f"{key}={value}" for key, value in sorted(conflicting_row.items()))
        conflicting_bytes = ("\t".join(fields) + "\n").encode("utf-8")
        conflicting_ambient.write_bytes(conflicting_bytes)
        os.environ["AGENT_DISPATCH_JOBS"] = str(conflicting_ambient)
        direct_conflict = DC.gate_currency(
            self.route, self.review_node, marker_path, marker, observe=True,
        )
        continuation_conflict = ROUTE_WRITER._continuation_reused_evidence(
            self.route, self.review_node,
        )

        self.assertEqual(direct_missing.state, "current", direct_missing)
        self.assertEqual(direct_conflict.state, "current", direct_conflict)
        self.assertEqual(continuation_missing[0]["node_id"], "impl-review")
        self.assertEqual(continuation_conflict[0]["node_id"], "impl-review")
        self.assertFalse(missing_ambient.exists())
        self.assertEqual(conflicting_ambient.read_bytes(), conflicting_bytes)
        self.assertEqual(self.jobs.read_bytes(), sealed_before)

        # The selected registry remains authoritative when it is damaged.
        self.jobs.unlink()
        missing_selected = DC.gate_currency(
            self.route, self.review_node, marker_path, marker, observe=True,
        )
        self.jobs.write_bytes(sealed_before)
        self.assertEqual(missing_selected.reason, "completion-review-source-registry-missing")

        self.jobs.write_text("", encoding="utf-8")
        absent_row = DC.gate_currency(
            self.route, self.review_node, marker_path, marker, observe=True,
        )
        self.jobs.write_bytes(sealed_before)
        self.assertEqual(absent_row.reason, "completion-review-source-row-not-exact")
        self.jobs.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(
            ValueError, "completion-review-source-row-not-exact",
        ):
            ROUTE_WRITER._continuation_reused_evidence(self.route, self.review_node)
        self.jobs.write_bytes(sealed_before)

        lines = self.jobs.read_text(encoding="utf-8").splitlines()
        changed = []
        for line in lines:
            row = line.split("\t")
            values = DC.parse_registry_metadata(row[5]) if len(row) == 6 else {}
            if values.get("attempt_id") == metadata["attempt_id"]:
                values["route_hash"] = "sha256:" + "0" * 64
                row[5] = ",".join(f"{key}={value}" for key, value in sorted(values.items()))
                line = "\t".join(row)
            changed.append(line)
        self.jobs.write_text("\n".join(changed) + "\n", encoding="utf-8")
        wrong_record = DC.gate_currency(
            self.route, self.review_node, marker_path, marker, observe=True,
        )
        self.jobs.write_bytes(sealed_before)
        self.assertEqual(wrong_record.reason, "completion-review-source-row-binding-mismatch")

        binding_path = SUBJECT.binding_path(self.jobs, metadata["attempt_id"])
        binding_bytes = binding_path.read_bytes()
        binding_path.write_text("{}\n", encoding="utf-8")
        corrupt_binding = DC.gate_currency(
            self.route, self.review_node, marker_path, marker, observe=True,
        )
        binding_path.write_bytes(binding_bytes)
        self.assertEqual(corrupt_binding.reason, "completion-review-source-binding-missing")

    def test_public_complete_node_binds_registered_review_source(self):
        self.current_smoke_markers("public-completion")
        attempt = "att-review-public-complete-node"
        metadata = self.prepare_review(attempt, status="running")
        evidence = self.artifact_root / f"{attempt}.md"
        evidence.write_text("public completion PASS", encoding="utf-8")

        marker, row = ROUTE_WRITER.complete_node(
            self.route, self.review_node, self.review_node["id"], evidence,
            jobs=self.jobs, attempt_id=attempt,
        )

        self.assertEqual(row["status"], "closed")
        self.assertEqual(marker["source_subject"]["digest"], metadata["review_subject_digest"])
        self.assertEqual(marker["source_subject"]["binding_digest"],
                         metadata["review_subject_binding_digest"])
        self.assertEqual(self.currency(marker).state, "current")
        with patch_split(
            DC, "attempt_process_quiescence",
            return_value=DC.ProcessQuiescence("quiescent", "fixture-terminal-observation"),
        ):
            terminal = ROUTE_WRITER._marker_identity_row(
                self.route, self.review_node, self.review_node["id"],
                self.review_node["completion_gate"], jobs=self.jobs, exact_terminal=True,
            )
        self.assertTrue(terminal["passed"], terminal)

    def test_common_writer_checks_exact_replay_before_fresh_source_epoch(self):
        attempt = "att-execute-common-replay-before-source"
        marker = self.complete_node("execute", attempt, "execute evidence")
        directory = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs)
        history = directory / f"execute.{marker['sequence']}.json"
        self.assertTrue(history.is_file())
        history.unlink()

        node = self.get_node("execute")
        evidence = self.artifact_root / f"{attempt}.md"
        metadata = self._metadata(node, attempt, "stage")
        with self.assertRaisesRegex(
                ValueError, "^canonical completion marker history conflict$"):
            ROUTE_WRITER.write_completion_marker(
                self.route, node, node["id"], evidence,
                attempt_id=attempt, attempt_metadata=metadata, jobs=self.jobs,
            )

    def test_does_not_reuse_a_stale_review_pass(self):
        self.current_smoke_markers("A")
        _meta, a_pass = self.complete_review("att-review-gate-off-A")
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        self.current_smoke_markers("B")
        self.assertEqual(self.currency(a_pass).state, "superseded")
        with patch_split(DC, "completion_attempt_readiness",
                               side_effect=AssertionError("stale PASS must not be consumed")):
            DC.completion_marker_gate(str(self.route_file), "test", "start",
                                      self.agent_home, self.jobs)

    def _gate_start(self, node, *, readiness=False):
        patches = []
        if not readiness:
            patches.append(patch_split(DC, "completion_attempt_readiness",
                                             side_effect=AssertionError("superseded PASS must not be consumed")))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            DC.completion_marker_gate(str(self.route_file), node, "start", self.agent_home, self.jobs)

    def test_stale_test_source_proceeds_without_reusing_pass(self):
        # N21: a source-bound test PASS whose source moved is dropped from the observation, never consumed.
        self.current_smoke_markers("A")
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        smoke = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs) / "smoke.json"
        marker = json.loads(smoke.read_text(encoding="utf-8"))
        currency = DC.gate_currency(self.route, self.get_node("smoke"), smoke, marker, observe=True)
        self.assertEqual((currency.state, currency.reason), ("superseded", "completion-test-source-stale"))
        self.assertTrue(DC.completion_superseded(currency))
        self._gate_start("impl-review")

    def test_unknown_review_subject_proceeds_without_reusing_pass(self):
        # N28: a review PASS that records no subject cannot be read as current; the next node proceeds.
        self.current_smoke_markers("A")
        self.complete_review("att-review-unbound")
        with patch_split(DC, "gate_currency", return_value=DC.GateCurrency(
                "superseded", "completion-review-subject-unknown")), \
                patch_split(DC, "completion_marker_is_current", return_value=False):
            self._gate_start("test")

    def test_missing_review_input_proceeds_without_reusing_pass(self):
        self.current_smoke_markers("A")
        self.complete_review("att-review-input-missing")
        with patch_split(DC, "gate_currency", return_value=DC.GateCurrency(
                "superseded", "completion-review-subject-input-missing")), \
                patch_split(DC, "completion_marker_is_current", return_value=False):
            self._gate_start("test")

    def test_superseded_rule_never_admits_integrity_breaks(self):
        for state in ("integrity-broken:history-conflict", "integrity-broken:identity-mismatch"):
            currency = DC.GateCurrency(state, "completion-marker-unreadable")
            self.assertFalse(DC.completion_superseded(currency))
            self.assertFalse(DC.completion_superseded(currency, answered=True))
        self.assertTrue(DC.completion_superseded(DC.GateCurrency("superseded", "x")))
        self.assertFalse(DC.completion_superseded(DC.GateCurrency("current", "x")))
        self.assertFalse(DC.completion_superseded(DC.GateCurrency("revised-unrecorded", "x")))
        # Only a verdict an approved fix answered is reopened whatever evidence change it shows.
        for state in ("current", "revised-unrecorded", "completion-evidence-unreadable"):
            self.assertTrue(DC.completion_superseded(DC.GateCurrency(state, "x"), answered=True))
        self.current_smoke_markers("A")
        smoke = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs) / "smoke.json"
        broken = json.loads(smoke.read_text(encoding="utf-8"))
        smoke.write_text(json.dumps({**broken, "route_hash": "sha256:" + "0" * 64}), encoding="utf-8")
        with self.assertRaises(DC.DispatchContractError) as refused:
            self._gate_start("impl-review")
        self.assertEqual(refused.exception.reason, "completion-marker-integrity-broken")

    def test_registration_and_launch_fences_reject_source_races_without_spawning(self):
        admitted_digest = SUBJECT.observe(self.route, self.review_node,
                                          worktree=self.repo)["digest"]
        attempt_id = "att-review-launch-race"
        metadata = self._metadata(self.review_node, attempt_id, "review")
        metadata.update({
            "capability": "autopilot-code", "capability_mode": "debug",
            "qa": "standard", "intensity": "standard", "unit": "qa/code-review",
            "route_node": self.review_node["id"], "write_scope": "_internal/dev_reviews/**",
            "completion_gate": "code-impl-review", "artifact_root": str(self.artifact_root),
            "log_file": str(self.base / "worker.log"), "launch_home": str(self.agent_home),
            "launch_authority": "conductor", "launch_fence": "registry-v1",
        })
        row = "\t".join(("fixture", "open", str(self.repo), str(self.repo), attempt_id,
                         ",".join(f"{key}={value}" for key, value in metadata.items())))
        with mock.patch.dict(os.environ, {"AGENT_REVIEW_SOURCE_SUBJECT_DIGEST": admitted_digest}):
            self.assertTrue(DC.claim_attempt_row(self.jobs, attempt_id, row, launch=False))

        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        spawned = []

        def never_spawn(_fd):
            spawned.append(True)
            raise AssertionError("the launch fence must run before spawn")

        with self.assertRaises(DC.DispatchContractError) as launch_refusal:
            DC.spawn_claimed_attempt(self.jobs, attempt_id, parent_binding=None, spawn=never_spawn)
        self.assertEqual(launch_refusal.exception.reason, "review-source-subject-changed-before-launch")
        self.assertEqual(spawned, [])

        second_id = "att-review-register-race"
        second_metadata = {**metadata, "attempt_id": second_id}
        second_row = "\t".join(("fixture", "open", str(self.repo), str(self.repo), second_id,
                                ",".join(f"{key}={value}" for key, value in second_metadata.items())))
        with mock.patch.dict(os.environ, {"AGENT_REVIEW_SOURCE_SUBJECT_DIGEST": admitted_digest}):
            with self.assertRaises(DC.DispatchContractError) as registration_refusal:
                DC.claim_attempt_row(self.jobs, second_id, second_row, launch=False)
        self.assertEqual(registration_refusal.exception.reason,
                         "review-source-admission-subject-changed")

    def test_code_test_registration_launch_currency_and_source_specific_round_budget(self):
        self.current_smoke_markers("test-A")
        first = self.complete_node("test", "att-code-test-A1", "test A pass one")
        second = self.complete_node("test", "att-code-test-A2", "test A pass two")
        completion = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs)
        marker_path = completion / "test.json"
        first_path = completion / f"test.{first['sequence']}.json"
        self.assertEqual(DC.gate_currency(self.route, self.test_node, marker_path, second,
                                          observe=True, jobs=self.jobs).state, "current")
        self.assertEqual(self.test_admission().budget.state, "exhausted")

        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        self.assertEqual(DC.gate_currency(self.route, self.test_node, marker_path, second,
                                          observe=True, jobs=self.jobs).state, "superseded")
        legacy = self._metadata(self.test_node, "att-code-test-legacy-B", "stage")
        self._row(legacy, note="completed-marker")  # historical PASS with no subject binding
        b_admission = self.test_admission()
        self.assertEqual((b_admission.budget.state, b_admission.budget.verdict_rounds,
                          b_admission.budget.round_kind), ("admit", 0, "first"))

        observed = SUBJECT.observe(self.route, self.test_node, worktree=self.repo, jobs=self.jobs)
        attempt = "att-code-test-B-launch-race"
        metadata = self._metadata(self.test_node, attempt, "stage")
        metadata.update({
            "capability": "autopilot-code", "capability_mode": "debug",
            "qa": "standard", "intensity": "standard", "unit": "qa/test",
            "write_scope": "test_logs/**", "completion_gate": "code-test",
            "artifact_root": str(self.artifact_root), "log_file": str(self.base / "worker.log"),
            "launch_home": str(self.agent_home), "launch_authority": "conductor",
            "launch_fence": "registry-v1",
        })
        row = "\t".join(("fixture", "open", str(self.repo), str(self.repo), attempt,
                          ",".join(f"{key}={value}" for key, value in metadata.items())))
        with mock.patch.dict(os.environ, {"AGENT_REVIEW_SOURCE_SUBJECT_DIGEST": observed["digest"]}):
            self.assertTrue(DC.claim_attempt_row(self.jobs, attempt, row, launch=False))

        (self.repo / "engine.py").write_text("value = 'C'\n", encoding="utf-8")
        spawned = []

        def never_spawn(_fd):
            spawned.append(True)
            raise AssertionError("the code-test launch fence must run before spawn")

        with self.assertRaises(DC.DispatchContractError) as refused:
            DC.spawn_claimed_attempt(self.jobs, attempt, parent_binding=None, spawn=never_spawn)
        self.assertEqual(refused.exception.reason, "review-source-subject-changed-before-launch")
        self.assertEqual(spawned, [])
        self._row(metadata, status="cancelled", note="dead-invalid-envelope")

        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        self.current_smoke_markers("test-B")
        self.complete_node("test", "att-code-test-B1", "test B pass one")
        self.complete_node("test", "att-code-test-B2", "test B pass two")
        self.assertEqual(self.test_admission().budget.state, "exhausted")
        (self.repo / "engine.py").write_text("value = 'A'\n", encoding="utf-8")
        self.assertEqual(DC.gate_currency(self.route, self.test_node, first_path, first,
                                          observe=True, jobs=self.jobs).state, "superseded")
        self.assertEqual(self.test_admission().budget.state, "exhausted")
        self.assertEqual(self.test_admission().budget.verdict_rounds, 2)

    def test_code_test_owner_closure_remains_refused(self):
        choice = __import__("route_authority").decide_continuation(
            self.route, action="owner-closure", node=self.test_node, rows=[], jobs=self.jobs,
        )
        self.assertFalse(choice.allowed)
        self.assertEqual(choice.reason, "owner-closure-node-not-review")

    def test_live_and_unsettled_other_subjects_still_block_B_admission(self):
        self.current_smoke_markers("A")
        live = self.prepare_review("att-review-A-live", status="open", note="")
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        self.current_smoke_markers("B")
        self.assertEqual(self.admission().budget.state, "blocked-live")
        self._row(live, status="done", note="dead-worker-blocked")
        self.prepare_review("att-review-A-unsettled", status="done", note="completed-marker",
            extra={"terminal_conflict": "1", "conflicting_terminal_note": "completed-marker",
                    "conflicting_failure_class": "pass", "conflicting_classifier_source": "fixture"})
        self.assertEqual(self.admission().budget.state, "blocked-unsettled")

    def test_continuation_inherits_the_source_epoch_and_cannot_revive_A(self):
        self.current_smoke_markers("A")
        parent_route = json.loads(json.dumps(self.route))
        routes_dir = self.artifact_root / ".runtime" / "routes"
        routes_dir.mkdir(parents=True, exist_ok=True)
        (routes_dir / f"{parent_route['route_id']}.json").write_text(
            json.dumps(parent_route), encoding="utf-8")

        child = json.loads(json.dumps(parent_route))
        child["source_route_id"] = parent_route["route_id"]
        child["source_route_hash"] = parent_route["route_hash"]
        child["continuation_contract_version"] = 1
        child["route_hash"] = route_hash(child)
        child["route_id"] = route_id_from_hash(child["route_hash"])
        child_exec = next(item for item in child["nodes"] if item["id"] == "execute")
        child_dir = self.state / "completion" / child["route_id"]
        source_a = SUBJECT.observe(child, child_exec, worktree=self.repo)
        self.assertEqual(SUBJECT.source_epoch(child, child_exec, source_a["digest"], child_dir), 0)

        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        evidence = self.artifact_root / "continuation-execute-B.md"
        evidence.write_text("continued execute B", encoding="utf-8")
        attempt = "att-continuation-execute-B"
        metadata = self._metadata(child_exec, attempt, "stage")
        metadata.update(route_id=child["route_id"], route_hash=child["route_hash"],
                        route_file=str(self.route_file))
        marker = ROUTE_WRITER._publish_completion_locked(
            child, child_exec, "execute", evidence, jobs=self.jobs,
            attempt_id=attempt, attempt_metadata=metadata,
        )
        self.assertEqual(marker["source_subject"]["epoch"], 1)
        (self.repo / "engine.py").write_text("value = 'A'\n", encoding="utf-8")
        source_a_again = SUBJECT.observe(child, child_exec, worktree=self.repo)
        self.assertEqual(SUBJECT.source_epoch(child, child_exec, source_a_again["digest"], child_dir), 2)

    def _inline_metadata(self, node, attempt_id):
        return {**self._metadata(node, attempt_id, 'owner'),
                'registered_worker': '0', 'execution_surface': 'inline',
                'transport': 'interactive', 'fallback_hop': 'inline'}

    def _completion_state(self, directory):
        return {path.name: path.read_bytes() for path in sorted(directory.iterdir())
                if path.is_file() and not path.name.startswith('.')}

    def _legacy_unknown_review(self):
        """Publish an inline review, then rewrite canonical and history as the old writer did."""
        node = self.review_node
        evidence = self.artifact_root / 'historical-unknown.md'
        evidence.write_text('historical unknown review bytes', encoding='utf-8')
        old = ROUTE_WRITER._publish_completion_locked(
            self.route, node, node['id'], evidence, jobs=self.jobs,
            attempt_id='att-original-inline',
            attempt_metadata=self._inline_metadata(node, 'att-original-inline'))
        old.pop('source_subject')
        directory = ROUTE_WRITER.completion_dir(self.route['route_id'], jobs=self.jobs)
        canonical = directory / 'impl-review.json'
        history = directory / f"impl-review.{old['sequence']}.json"
        canonical.write_text(json.dumps(old, sort_keys=True), encoding='utf-8')
        history.write_bytes(canonical.read_bytes())
        return old, evidence, directory, canonical, history

    def test_fresh_inline_review_requires_intact_canonical_head_and_history(self):
        """Writer-level: a damaged canonical/history pair cannot launder old or new evidence."""
        node = self.review_node
        refusals = {
            'missing': '^review-source-marker-history-missing:',
            'malformed': '^review-source-marker-unreadable:',
            'foreign': '^review-source-marker-history-conflict:',
            'content-mismatch': '^review-source-marker-history-conflict:',
        }
        for kind, expected in refusals.items():
            with self.subTest(history=kind):
                self.setUp()
                self.current_smoke_markers('A')
                old, evidence, directory, canonical, history = self._legacy_unknown_review()
                if kind == 'missing':
                    history.unlink()
                elif kind == 'malformed':
                    history.write_text('{invalid', encoding='utf-8')
                elif kind == 'foreign':
                    history.write_text(json.dumps(
                        {**old, 'route_hash': 'sha256:' + '0' * 64}), encoding='utf-8')
                else:
                    history.write_text(json.dumps(
                        {**old, 'attempt_id': 'att-different-origin'}), encoding='utf-8')
                fresh = self.artifact_root / 'fresh-different-bytes.md'
                fresh.write_text('new unrecorded review bytes', encoding='utf-8')
                current = SUBJECT.observe(self.route, node, worktree=self.repo)
                epoch_before = SUBJECT.source_epoch(self.route, node, current['digest'], directory)
                for label, candidate in (('historical-bytes', evidence), ('fresh-bytes', fresh)):
                    attempt = f'att-new-inline-{kind}-{label}'
                    before = self._completion_state(directory)
                    with self.subTest(history=kind, evidence=label):
                        with self.assertRaisesRegex(ValueError, expected):
                            ROUTE_WRITER.complete_node(
                                self.route, node, node['id'], candidate, attempt_id=attempt,
                                explicit_attempt_metadata=self._inline_metadata(node, attempt))
                        # Canonical, history, attempt links and epoch are all unchanged.
                        self.assertEqual(self._completion_state(directory), before)
                        self.assertEqual(
                            SUBJECT.source_epoch(self.route, node, current['digest'], directory),
                            epoch_before)
                        self.assertEqual(len(list(directory.glob(f'impl-review.{attempt}*'))), 0)
                    self.assertEqual(canonical.read_bytes(), before['impl-review.json'])

        # An intact legacy unknown canonical/history pair stays unknown and still admits
        # a new review's fresh evidence; the old record is never rewritten.
        with self.subTest(history='intact-unknown'):
            self.setUp()
            self.current_smoke_markers('A')
            old, evidence, directory, canonical, history = self._legacy_unknown_review()
            old_bytes = (canonical.read_bytes(), history.read_bytes())
            fresh = self.artifact_root / 'intact-fresh.md'
            fresh.write_text('new review bytes over an intact unknown head', encoding='utf-8')
            attempt = 'att-new-inline-intact'
            marker, result = ROUTE_WRITER.complete_node(
                self.route, node, node['id'], fresh, attempt_id=attempt,
                explicit_attempt_metadata=self._inline_metadata(node, attempt))
            self.assertEqual(result['status'], 'unregistered-complete')
            self.assertEqual(marker['sequence'], old['sequence'] + 1)
            self.assertEqual(self.currency(marker).state, 'current')
            self.assertEqual((directory / f"impl-review.{old['sequence']}.json").read_bytes(),
                             old_bytes[1])
            # The same historical bytes are still refused as unknown, never rebound.
            with self.assertRaisesRegex(ValueError, '^review-source-binding-missing:'):
                ROUTE_WRITER.complete_node(
                    self.route, node, node['id'], evidence, attempt_id='att-rebind-old',
                    explicit_attempt_metadata=self._inline_metadata(node, 'att-rebind-old'))

        # A canonical head lost between history and canonical writes recovers by exact replay.
        with self.subTest(history='canonical-absent-exact-replay'):
            self.setUp()
            self.current_smoke_markers('A')
            attempt = 'att-fresh-then-lose-canonical'
            fresh = self.artifact_root / 'replay-fresh.md'
            fresh.write_text('fresh review whose canonical head is lost', encoding='utf-8')
            marker, _result = ROUTE_WRITER.complete_node(
                self.route, node, node['id'], fresh, attempt_id=attempt,
                explicit_attempt_metadata=self._inline_metadata(node, attempt))
            directory = ROUTE_WRITER.completion_dir(self.route['route_id'], jobs=self.jobs)
            history_bytes = (directory / f"impl-review.{marker['sequence']}.json").read_bytes()
            (directory / 'impl-review.json').unlink()
            replay, _result = ROUTE_WRITER.complete_node(
                self.route, node, node['id'], fresh, attempt_id=attempt,
                explicit_attempt_metadata=self._inline_metadata(node, attempt))
            self.assertEqual(replay, marker)
            self.assertEqual((directory / f"impl-review.{marker['sequence']}.json").read_bytes(),
                             history_bytes)
            self.assertEqual(json.loads((directory / 'impl-review.json').read_text()), marker)

    def test_publication_retry_converges_without_duplicate_verdict_and_reads_do_not_write(self):
        node = self.get_node("execute")
        evidence = self.artifact_root / "retry.md"
        evidence.write_text("execute", encoding="utf-8")
        metadata = self._metadata(node, "att-execute-retry", "stage")
        original_atomic = ROUTE_WRITER.atomic_write
        failed = {"once": False}

        def interrupt(path, payload):
            if Path(path).name == "execute.json" and not failed["once"]:
                failed["once"] = True
                raise OSError("injected publication interruption")
            return original_atomic(path, payload)

        with patch_split(ROUTE_WRITER, "atomic_write", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "injected publication interruption"):
                ROUTE_WRITER._publish_completion_locked(
                    self.route, node, "execute", evidence, jobs=self.jobs,
                    attempt_id="att-execute-retry", attempt_metadata=metadata,
                )
        result = ROUTE_WRITER._publish_completion_locked(
            self.route, node, "execute", evidence, jobs=self.jobs,
            attempt_id="att-execute-retry", attempt_metadata=metadata,
        )
        directory = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs)
        self.assertEqual(result["sequence"], 1)
        self.assertEqual(len(list(directory.glob("execute.[0-9]*.json"))), 1)
        marker = directory / "execute.json"
        before_mtime = marker.stat().st_mtime_ns
        for _ in range(2):
            source_state = SUBJECT.observe(self.route, node, worktree=self.repo)
            self.assertEqual(source_state["digest"], result["source_subject"]["digest"])
        self.assertEqual(DC.gate_currency(self.route, node, marker, result).state, "current")
        self.assertEqual(marker.stat().st_mtime_ns, before_mtime)


class UpstreamOrderTest(unittest.TestCase):
    """C1: a result older than a completion upstream of it is not current.

    The shape of SR route rt-3a82704b6ee6824f: ``inspect`` completed (sequence 1), ``report`` completed
    after it, then ``inspect`` was reopened and completed again (sequence 2) while ``report`` stayed.
    """

    git = SourceSubjectIntegrationTest.git
    get_node = SourceSubjectIntegrationTest.get_node
    _metadata = SourceSubjectIntegrationTest._metadata

    def setUp(self):
        # The shared fixture, then the audit route.
        SourceSubjectIntegrationTest.setUp(self)
        self.route["nodes"] = [
            {"id": "inspect", "kind": "pipeline-stage", "unit": "research/fact-check",
             "completion_gate": "audit-inspect", "dispatch_depth": 2, "write_scope": ["reviews/audit/**"],
             "depends_on": []},
            {"id": "report", "kind": "pipeline-stage", "unit": "editorial/report",
             "completion_gate": "audit-report", "dispatch_depth": 2, "write_scope": ["reviews/audit-report.md"],
             "outputs": ["reviews/audit-report.md"], "depends_on": ["inspect"]},
            {"id": "ship", "kind": "pipeline-stage", "unit": "dev/backend",
             "completion_gate": "audit-ship", "dispatch_depth": 2, "write_scope": ["dev_logs/**"],
             "depends_on": ["report"]},
        ]
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.route_file.write_text(json.dumps(self.route, sort_keys=True), encoding="utf-8")

    def complete(self, node_id, attempt_id, text, evidence=None):
        node = self.get_node(node_id)
        evidence = evidence or self.artifact_root / f"{attempt_id}.md"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(text, encoding="utf-8")
        return ROUTE_WRITER._publish_completion_locked(
            self.route, node, node_id, evidence, jobs=self.jobs, attempt_id=attempt_id,
            attempt_metadata=self._metadata(node, attempt_id, "stage"))

    def path(self, node):
        return ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs) / f"{node}.json"

    def marker(self, node):
        return json.loads(self.path(node).read_text(encoding="utf-8"))

    def judge(self, node):
        return DC.gate_currency(self.route, self.get_node(node), self.path(node), self.marker(node), observe=True)

    def start(self, node):
        with patch_split(DC, "completion_attempt_readiness",
                                  side_effect=AssertionError("a superseded result must not be consumed")):
            DC.completion_marker_gate(str(self.route_file), node, "start", self.agent_home, self.jobs)

    def test_a_result_older_than_its_upstream_completion_is_superseded_until_it_completes_again(self):
        self.complete("inspect", "att-inspect-1", "inspect v1")
        self.complete("report", "att-report-1", "report v1")
        # Protection: an upstream that did not complete again leaves the downstream result current.
        self.assertEqual((self.judge("inspect").state, self.judge("report").state), ("current", "current"))
        self.assertEqual(DC.evidence_currency(self.route, self.get_node("report"), self.path("report")).state, "current")
        # inspect is reopened and completes again (a new attempt, a new sequence).
        again = self.complete("inspect", "att-inspect-2", "inspect v2")
        self.assertEqual(again["sequence"], 2)
        self.assertEqual(self.judge("inspect").state, "current")
        for currency in (self.judge("report"), DC.evidence_currency(
                self.route, self.get_node("report"), self.path("report"))):
            self.assertEqual((currency.state, currency.reason), ("superseded", "completion-evidence-superseded"))
            self.assertIn("inspect@2 completed again", currency.next_action)
        self.assertTrue(DC.completion_superseded(self.judge("report")))
        # Not a new refusal: the node after the stale result proceeds without consuming the old PASS.
        self.start("ship")
        # The reopened node's own new completion, after its upstream's, is current again.
        self.complete("report", "att-report-2", "report v2")
        self.assertEqual(self.judge("report").state, "current")

    def test_a_reopened_node_writes_its_new_result_beside_the_old_one(self):
        # C2: the report's sealed output is one exact file holding the old result. Run again, it writes
        # the output's next revision path and completes with that path; the old file stays as it was.
        report = self.artifact_root / "reviews" / "audit-report.md"
        revision = lambda: DC.reopened_output_path(self.route, self.get_node("report"), self.jobs, self.artifact_root)
        self.complete("inspect", "att-inspect-1", "inspect v1")
        self.complete("report", "att-report-1", "report v1", evidence=report)
        self.assertIsNone(revision())                       # a current result is not reopened
        self.complete("inspect", "att-inspect-2", "inspect v2")
        path = revision()
        self.assertEqual(path, str(report.with_name("audit-report.r2.md")))
        self.assertIsNone(DC.reopened_output_path(self.route, self.get_node("inspect"), self.jobs, self.artifact_root))
        import artifact_context
        # Inside the node's approved scope (guard and write oracle read the same rule); beside it only.
        self.assertTrue(artifact_context.scope_matches("reviews/audit-report.r2.md", ["reviews/audit-report.md"]))
        self.assertFalse(artifact_context.scope_matches("reviews/other.r2.md", ["reviews/audit-report.md"]))
        self.assertFalse(artifact_context.scope_matches("audit-report.r2.md", ["reviews/audit-report.md"]))
        self.complete("report", "att-report-2", "report v2", evidence=Path(path))
        self.assertEqual(report.read_text(encoding="utf-8"), "report v1")          # not overwritten
        marker = self.marker("report")
        self.assertEqual((marker["evidence"]["path"], marker["sequence"]), (path, 2))
        self.assertEqual(self.judge("report").state, "current")
        self.assertIsNone(revision())                       # current again: nothing to move aside
        # A continuation route (a new route id along the verified lineage) runs the node again: the sealed file
        # holds the source route's result, and the revision number is one past the lineage's highest sequence.
        child = {**self.route, "route_id": "rt-continuation0001", "route_hash": "sha256:" + "c" * 64,
                 "continuation_contract_version": 1, "source_route_id": self.route["route_id"]}
        import route_lineage
        with patch_split(route_lineage, "verified_route_lineage", return_value=[child, self.route]):
            self.assertEqual(DC.reopened_output_path(child, self.get_node("report"), self.jobs, self.artifact_root),
                             str(report.with_name("audit-report.r3.md")))
        with patch_split(route_lineage, "verified_route_lineage",
                               side_effect=route_lineage.RouteLineageError("route-lineage-unverified")):
            self.assertIsNone(DC.reopened_output_path(child, self.get_node("report"), self.jobs, self.artifact_root))
        # Another reopening: the sealed file is still an earlier marker's evidence, so the next revision
        # is named, and the node's assignment says so.
        self.complete("inspect", "att-inspect-3", "inspect v3")
        self.assertEqual(revision(), str(report.with_name("audit-report.r3.md")))
        import worker_bootstrap
        args = SimpleNamespace(route_node="report", jobs_path=self.jobs)
        scope = SimpleNamespace(output_dir=str(self.artifact_root))
        prompt = worker_bootstrap.reopened_output_prompt(args, self.route, scope)
        self.assertIn(str(report.with_name("audit-report.r3.md")), prompt)
        self.assertIn("do not overwrite the earlier file", prompt)
        self.assertEqual(worker_bootstrap.reopened_output_prompt(
            SimpleNamespace(route_node="inspect", jobs_path=self.jobs), self.route, scope), "")

    def test_the_terminal_gate_does_not_accept_the_older_result(self):
        router = ROUTE_WRITER
        self.complete("inspect", "att-inspect-1", "inspect v1")
        self.complete("report", "att-report-1", "report v1")
        row = lambda: router._marker_identity_row(self.route, self.get_node("report"), "report", "audit-report",
                                                  jobs=self.jobs)
        self.assertTrue(row()["passed"])
        self.complete("inspect", "att-inspect-2", "inspect v2")
        stale = row()
        self.assertEqual((stale["passed"], stale["reason"]), (False, "completion-evidence-superseded"))
        self.complete("report", "att-report-2", "report v2")
        self.assertTrue(row()["passed"])

    def test_a_revision_a_tombstone_an_unreadable_upstream_or_a_bound_result_is_not_older(self):
        self.complete("inspect", "att-inspect-1", "inspect v1")
        self.complete("report", "att-report-1", "report v1")
        report, mine, inspect_path = self.get_node("report"), self.marker("report"), self.path("inspect")
        first = self.marker("inspect")
        def judged(**changes):
            inspect_path.write_text(json.dumps({**first, **changes}), encoding="utf-8")
            return DC._upstream_completed_later(self.route, report, mine, self.path("report"))
        later = {"completed_at": "2999-01-01T00:00:00Z", "sequence": 2}
        self.assertEqual(judged(**later), ("inspect", 2))
        self.assertIsNone(judged(**later, stage_authority="revision"))           # an upstream revision
        self.assertIsNone(judged(**later, state="superseded-by-upstream-revision"))
        self.assertIsNone(judged(completed_at="1999-01-01T00:00:00Z"))            # earlier than the result
        # An upstream's first marker written after the result (a resource stage's owner settlement)
        # is not a new completion.
        self.assertIsNone(judged(completed_at="2999-01-01T00:00:00Z", sequence=1))
        inspect_path.write_text("{", encoding="utf-8")                           # unreadable: no judgement
        self.assertIsNone(DC._upstream_completed_later(self.route, report, mine, self.path("report")))
        inspect_path.write_text(json.dumps({**first, **later}), encoding="utf-8")
        # A result judged by its source subject is judged by that subject, not by order.
        self.assertIsNone(DC._upstream_completed_later(
            self.route, report, {**mine, "source_subject": {"digest": "x"}}, self.path("report")))
        # A marker whose own date cannot be read is not judged.
        self.assertIsNone(DC._upstream_completed_later(
            self.route, report, {k: v for k, v in mine.items() if k != "completed_at"}, self.path("report")))

    def test_a_completion_time_without_a_timezone_is_not_an_instant(self):
        # Observing failure is "cannot judge", never "older": a naive or unorderable value gives no instant.
        self.assertIsNotNone(DC._completion_instant({"completed_at": "2026-11-01T15:33:50Z"}))
        for value in ("2026-11-01T15:33:50", "2026-11-01", "not a time", None, 5):
            self.assertIsNone(DC._completion_instant({"completed_at": value}), value)
        self.complete("inspect", "att-inspect-1", "inspect v1")
        self.complete("report", "att-report-1", "report v1")
        report, mine, inspect_path = self.get_node("report"), self.marker("report"), self.path("inspect")
        inspect_path.write_text(json.dumps({**self.marker("inspect"), "completed_at": "2999-01-01T00:00:00",
                                            "sequence": 2}), encoding="utf-8")
        self.assertIsNone(DC._upstream_completed_later(self.route, report, mine, self.path("report")))
        self.assertIsNone(DC._upstream_completed_later(
            self.route, report, {**mine, "completed_at": "2026-11-01T15:33:50"}, self.path("report")))


if __name__ == "__main__":
    unittest.main()
