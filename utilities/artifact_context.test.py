#!/usr/bin/env python3
"""Focused regressions for exact route context and typed output selection."""
import base64
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import artifact_context as C
import artifact_producer as P
import dispatch_terminal_commit as T
import dispatch_completion_join as JOIN
import codex_dispatch_terminal as terminal

_PRODUCER_FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "producer_fixture_for_terminal_artifact_context", Path(__file__).with_name("artifact_producer.test.py"))
PRODUCER_FIXTURE = importlib.util.module_from_spec(_PRODUCER_FIXTURE_SPEC)
_PRODUCER_FIXTURE_SPEC.loader.exec_module(PRODUCER_FIXTURE)


class ArtifactContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "artifacts"
        self.root.mkdir()
        self.source = self.base / "source"
        self.source.mkdir()
        self.route = {
            "route_id": "rt-context", "route_hash": "sha256:route",
            "artifact_root": str(self.root), "cwd": str(self.source),
            "capability": "autopilot-code", "effective_intensity": "standard",
            "nodes": [{"id": "test", "outputs": ["test_logs/**", "source-diff"],
                       "write_scope": ["test_logs/**", "source-scoped"]}],
        }
        self.cycle = {"campaign_id": "camp_fixture", "cycle_id": "cyc_fixture",
                      "producer_id": "prod_fixture", "route_id": "rt-context"}

    def _context(self, env=None, **kwargs):
        values = {"AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_ROUTE_NODE": "test",
                  "AGENT_DISPATCH_WORKTREE": str(self.source)}
        values.update(env or {})
        with mock.patch.object(P, "load_route", return_value=self.route), \
             mock.patch.object(P, "route_cycle_for", return_value=self.cycle), \
             mock.patch.object(P, "cycle_dir", return_value=self.root / "fixture-cycle"):
            return C.resolve_context(route=self.route, environ=values, **kwargs)

    def test_role_selection_uses_exact_declared_output_and_scope(self):
        context = self._context()
        report = context.resolve_output("test-report")
        diagnostics = context.resolve_output("test-diagnostics")
        self.assertEqual(report.path, self.root / "fixture-cycle/artifacts/test_logs/run-tests-report.tsv")
        self.assertEqual(diagnostics.path, self.root / "fixture-cycle/artifacts/test_logs/run-tests-diagnostics")
        self.assertNotEqual(report.path, diagnostics.path)
        with self.assertRaisesRegex(C.ArtifactContextError, "artifact-output-role-missing"):
            context.resolve_output("ship-deploy")

    def test_run_record_resolves_native_mapped_and_separate_resource_outputs(self):
        base = self._context()
        native_node = {"id": "full-run", "outputs": ["run.json"], "write_scope": ["run.json"]}
        native = replace(base, node=native_node, node_id="full-run")
        self.assertEqual(native.resolve_output("run-record").path,
                         self.root / "fixture-cycle/artifacts/run.json")

        mapped = "parts/autopilot-lab/full-run/run.json"
        borrowed_node = {"id": "lab-full-run", "part": "autopilot-lab:full-run",
                         "outputs": [mapped], "write_scope": [mapped],
                         "part_io": {"run.json": mapped}}
        borrowed = replace(base, node=borrowed_node, node_id="lab-full-run")
        self.assertEqual(borrowed.resolve_output("run-record").path,
                         self.root / "fixture-cycle/artifacts" / mapped)

        eval_output = "parts/autopilot-lab/eval-run/run.json"
        evaluation = replace(base, node={"id": "eval-run", "outputs": [eval_output],
                            "write_scope": [eval_output], "part_io": {"run.json": eval_output}},
                            node_id="eval-run")
        self.assertEqual(evaluation.resolve_output("run-record").path,
                         self.root / "fixture-cycle/artifacts" / eval_output)
        self.assertNotEqual(borrowed.resolve_output("run-record").path,
                            evaluation.resolve_output("run-record").path)

        traversal = replace(base, node={"id": "bad", "outputs": ["../run.json"],
                                       "write_scope": ["../run.json"]}, node_id="bad")
        with self.assertRaisesRegex(C.ArtifactContextError, "artifact-output-path-invalid"):
            traversal.resolve_output("run-record")
        outside = replace(base, node={"id": "outside", "outputs": ["elsewhere/run.json"],
                                     "write_scope": ["parts/autopilot-lab/full-run/**"]}, node_id="outside")
        with self.assertRaisesRegex(C.ArtifactContextError, "artifact-output-outside-node-scope"):
            outside.resolve_output("run-record")

    def _borrowed_smoke_context(self):
        base = self._context()
        part_root = "_internal/parts/autopilot-lab/smoke"
        mapped = f"{part_root}/reviews/smoke-attestation.json"
        node = {"id": "autopilot-lab-smoke", "part": "autopilot-lab:smoke",
                "outputs": [mapped],
                "write_scope": [f"{part_root}/reviews/smoke/**", mapped],
                "part_io": {"reviews/smoke-attestation.json": mapped}}
        return replace(base, node=node, node_id=node["id"]), part_root

    def test_config_seal_resolves_borrowed_smoke_part_support(self):
        context, part_root = self._borrowed_smoke_context()
        self.assertNotIn("reviews/smoke/**", context.node["part_io"])
        selected = context.resolve_output("config-seal", leaf="experiments/demo/_internal/configs")
        self.assertEqual(selected.path,
                         context.output_dir / part_root / "reviews/smoke/experiments/demo/_internal/configs")

        base = self._context()
        native = replace(base, node={"id": "smoke", "outputs": ["reviews/smoke/**"],
                                     "write_scope": ["reviews/smoke/**"]}, node_id="smoke")
        self.assertEqual(native.resolve_output("config-seal", leaf="experiments/demo/_internal/configs").path,
                         base.output_dir / "reviews/smoke/experiments/demo/_internal/configs")

    def test_config_seal_borrowed_role_keeps_mapping_scope_and_identity_refusals(self):
        context, part_root = self._borrowed_smoke_context()
        missing = replace(context, node={**context.node, "part_io": {}}, node_id="missing-map")
        with self.assertRaisesRegex(C.ArtifactContextError, "artifact-output-role-missing"):
            missing.resolve_output("config-seal", leaf="experiments/demo/_internal/configs")

        invalid_mapping = replace(context, node={**context.node,
            "part_io": {"reviews/smoke-attestation.json": "../outside/reviews/smoke-attestation.json"}},
            node_id="invalid-map")
        with self.assertRaisesRegex(C.ArtifactContextError, "artifact-output-role-missing"):
            invalid_mapping.resolve_output("config-seal", leaf="experiments/demo/_internal/configs")

        wrong_identity = replace(context, node={**context.node, "part": "autopilot-lab:full-run"},
                                 node_id="wrong-part")
        with self.assertRaisesRegex(C.ArtifactContextError, "artifact-output-role-missing"):
            wrong_identity.resolve_output("config-seal", leaf="experiments/demo/_internal/configs")

        unsupported = replace(context, node={**context.node,
            "outputs": [*context.node["outputs"], f"{part_root}/reviews/smoke/**"],
            "write_scope": [context.node["outputs"][0]]}, node_id="scope-missing")
        with self.assertRaisesRegex(C.ArtifactContextError, "artifact-output-outside-node-scope"):
            unsupported.resolve_output("config-seal", leaf="experiments/demo/_internal/configs")

        ambiguous = replace(context, node={**context.node,
            "outputs": [context.node["outputs"][0], f"{part_root}/reviews/smoke/left/**",
                        f"{part_root}/reviews/smoke/right/**"],
            "write_scope": [f"{part_root}/reviews/smoke/left/**",
                            f"{part_root}/reviews/smoke/right/**"]},
            node_id="ambiguous")
        with self.assertRaisesRegex(C.ArtifactContextError, "artifact-output-role-ambiguous"):
            ambiguous.resolve_output("config-seal", leaf="experiments/demo/_internal/configs")

        mapped = context.node["outputs"][0]
        top_level = replace(context, node={**context.node,
            "outputs": [mapped, "reviews/smoke/**"],
            "write_scope": [mapped, "reviews/smoke/**"]}, node_id="top-level")
        with self.assertRaisesRegex(C.ArtifactContextError, "artifact-output-role-missing"):
            top_level.resolve_output("config-seal", leaf="experiments/demo/_internal/configs")

        with self.assertRaisesRegex(C.ArtifactContextError, "artifact-output-path-invalid"):
            context.resolve_output("config-seal", leaf="experiments/../escape/_internal/configs")

    def test_config_seal_borrowed_role_rejects_resolved_sibling_part_symlink(self):
        context, part_root = self._borrowed_smoke_context()
        relative = f"{part_root}/reviews/smoke/experiments/demo/_internal/configs"
        candidate = context.output_dir / relative
        sibling = context.output_dir / "_internal/parts/autopilot-lab/full-run/reviews/smoke/experiments/demo/_internal/configs"
        sibling.mkdir(parents=True)
        candidate.parent.mkdir(parents=True)
        candidate.symlink_to(sibling, target_is_directory=True)
        with self.assertRaisesRegex(C.ArtifactContextError, "artifact-output-outside-node-scope"):
            context.resolve_output("config-seal", leaf="experiments/demo/_internal/configs")

    def test_logical_outputs_are_not_reconstructed_as_paths(self):
        self.assertEqual(C.render_outputs(self.root / "cycle/artifacts", ["source-diff"]), ["source-diff"])
        self.assertEqual(C.render_outputs(self.root / "cycle/artifacts", ["test_logs/**"]),
                         [str(self.root / "cycle/artifacts/test_logs/**")])

    def test_report_roles_select_subtree_and_exact_manifest_from_multiple_outputs(self):
        base = self._context()
        node = {"id": "report", "outputs": ["report/REPORT.md", "report/index.html",
                "report/report_manifest.json", "experiment-artifact"],
                "write_scope": ["report/REPORT.md", "report/index.html", "report/report_manifest.json"]}
        context = C.ArtifactContext(base.artifact_root, base.route, node, "report",
            base.cycle_record, base.cycle_dir, base.output_dir, base.source_worktree,
            base.binding_source)
        self.assertEqual(context.resolve_output("report-source").path,
                         base.output_dir / "report")
        self.assertEqual(context.resolve_output("report-manifest").path,
                         base.output_dir / "report/report_manifest.json")

    def test_component_scope_matching_rejects_prefix_and_traversal(self):
        self.assertTrue(C.scope_matches("test_logs/run.tsv", ["test_logs/**"]))
        self.assertFalse(C.scope_matches("test_logs-foreign/run.tsv", ["test_logs/**"]))
        self.assertFalse(C.scope_matches("test_logs/../outside", ["test_logs/**"]))
        self.assertFalse(C.scope_matches("test_logs/./run.tsv", ["test_logs/**"]))
        self.assertEqual(C.classify_scope("source-alternative/**"), "source")
        self.assertEqual(C.classify_scope("target-artifact"), "target")
        self.assertEqual(C.scope_values("tests/**;source-scoped;;dev_logs/**"),
                         ["dev_logs/**", "source-scoped", "tests/**"])

    def test_verified_route_lineage_overrides_stale_cycle_environment(self):
        context = self._context({"AGENT_ARTIFACT_CYCLE_ID": "cyc_stale",
                                 "AGENT_ARTIFACT_CAMPAIGN_ID": "camp_stale"})
        self.assertEqual(context.cycle_record["cycle_id"], "cyc_fixture")
        self.assertTrue(context.inherited_cycle_mismatch)
        self.assertEqual(context.binding_source, "route-lineage")

    def test_unbound_stale_environment_is_refused(self):
        values = {"AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_ROUTE_NODE": "test",
                  "AGENT_DISPATCH_WORKTREE": str(self.source),
                  "AGENT_ARTIFACT_CYCLE_ID": "cyc_stale"}
        with mock.patch.object(P, "load_route", return_value=self.route), \
             mock.patch.object(P, "route_cycle_for", return_value=None):
            with self.assertRaisesRegex(C.ArtifactContextError, "artifact-cycle-unbound"):
                C.resolve_context(route=self.route, environ=values)

    def test_explicit_node_cannot_reselect_a_different_bound_node(self):
        values = {"AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_ROUTE_NODE": "test",
                  "AGENT_DISPATCH_WORKTREE": str(self.source)}
        with mock.patch.object(P, "load_route", return_value=self.route), \
             mock.patch.object(P, "route_cycle_for", return_value=self.cycle), \
             mock.patch.object(P, "cycle_dir", return_value=self.root / "fixture-cycle"):
            with self.assertRaisesRegex(C.ArtifactContextError, "route-node-mismatch"):
                C.resolve_context(route=self.route, environ=values, node_id="other")

    def test_owner_producer_binding_precedes_stale_environment(self):
        bound = {**self.cycle, "cycle_id": "cyc_owner"}
        loaded = mock.Mock(binding=bound)
        admission = mock.Mock(allow=True)
        values = {"AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_ROUTE_NODE": "test",
                  "AGENT_DISPATCH_WORKTREE": str(self.source), "AGENT_ARTIFACT_CYCLE_ID": "cyc_old"}
        with mock.patch.object(P, "load_route", return_value=self.route), \
             mock.patch.object(P, "route_cycle_for") as route_cycle, \
             mock.patch.object(P, "read_cycle_record", return_value=bound), \
             mock.patch.object(P, "cycle_route_admission", return_value=admission), \
             mock.patch.object(P, "cycle_dir", return_value=self.root / "owner-cycle"), \
             mock.patch("dispatch_terminal_commit.load_producer_binding", return_value=loaded):
            context = C.resolve_context(route=self.route, environ=values, parent_attempt_id="att_owner")
        self.assertEqual(context.cycle_record["cycle_id"], "cyc_owner")
        self.assertEqual(context.binding_source, "producer-binding")
        self.assertTrue(context.inherited_cycle_mismatch)
        route_cycle.assert_not_called()

    def test_review_binding_selects_only_its_exact_report_target(self):
        review = {"cycle_id": "cyc_review", "producer_id": "prod_review",
                  "campaign_id": "camp_review"}
        output = self.root / "fixture-cycle/artifacts/plans/review.md"
        values = {"AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_ROUTE_NODE": "test",
            "AGENT_DISPATCH_WORKTREE": str(self.source), "AGENT_DISPATCH_JOBS": str(self.base / "jobs.log"),
            "AGENT_DISPATCH_ATTEMPT_ID": "att_review", "AGENT_DISPATCH_UNIT": "qa/code-review",
            "AGENT_REVIEW_OUTPUT": str(output), "AGENT_REVIEW_CYCLE_ID": review["cycle_id"],
            "AGENT_REVIEW_PRODUCER_ID": review["producer_id"]}
        with mock.patch.object(P, "load_route", return_value=self.route), \
             mock.patch.object(P, "read_cycle_record", return_value=review), \
             mock.patch.object(P, "cycle_dir", return_value=self.root / "fixture-cycle"), \
             mock.patch("dispatch_contract.validate_review_output_binding", return_value={
                 "cycle_id": review["cycle_id"], "producer_id": review["producer_id"],
                 "output_path": str(output)}):
            context = C.resolve_context(route=self.route, environ=values)
        self.assertEqual(context.binding_source, "review-binding")
        self.assertEqual(context.review_output, output)
        self.assertEqual(context.resolve_output("review-output").path, output)

    def test_source_provenance_uses_source_worktree_not_artifact_repository(self):
        def init_repo(path: Path, filename: str) -> str:
            path.mkdir()
            subprocess.run(["git", "init", "-q", str(path)], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.email", "fixture@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.name", "Fixture"], check=True)
            (path / filename).write_text(filename)
            subprocess.run(["git", "-C", str(path), "add", filename], check=True)
            subprocess.run(["git", "-C", str(path), "commit", "-qm", filename], check=True)
            return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()

        source_head = init_repo(self.base / "git-source", "source.txt")
        artifact_head = init_repo(self.base / "git-artifacts", "artifact.txt")
        state, observed = C.source_git_state(self.base / "git-source")
        self.assertEqual((state, observed), ("clean", source_head))
        self.assertNotEqual(source_head, artifact_head)
        (self.base / "git-source/new.txt").write_text("dirty")
        dirty, observed = C.source_git_state(self.base / "git-source")
        self.assertEqual((dirty, observed), ("untracked", source_head))
        (self.base / "git-source/new.txt").unlink()
        (self.base / "git-source/.gitignore").write_text("ignored.txt\n")
        subprocess.run(["git", "-C", str(self.base / "git-source"), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(self.base / "git-source"), "commit", "-qm", "ignore"], check=True)
        (self.base / "git-source/ignored.txt").write_text("ignored")
        ignored, _ = C.source_git_state(self.base / "git-source")
        self.assertEqual(ignored, "ignored")

    def test_source_provenance_without_git_uses_existing_unknown_state(self):
        state, commit = C.source_git_state(self.base)
        self.assertEqual((state, commit), ("unknown-no-git", None))

    def test_source_status_disables_optional_git_index_locks(self):
        repo = self.base / "git-status-env"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        # source_git_state reads status only once HEAD resolves.
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "base"], check=True)
        original_run = subprocess.run
        observed = []

        def capture_status(argv, **kwargs):
            if argv[:3] == ["git", "-C", str(repo)] and "status" in argv:
                observed.append(kwargs.get("env", {}).get("GIT_OPTIONAL_LOCKS"))
            return original_run(argv, **kwargs)

        with mock.patch.object(C.subprocess, "run", side_effect=capture_status):
            C.source_git_state(repo)
        self.assertEqual(observed, ["0"])


class BoundTerminalArtifactTest(PRODUCER_FIXTURE.ProducerTestBase):
    """Exercise issued cycle outputs through the shared terminal and reconcile path."""

    def setUp(self):
        super().setUp()
        self.source = Path(self._tmp.name) / "source-worktree"
        self.source.mkdir()
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.email",
                        "fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.name", "Fixture"], check=True)
        (self.source / "README.md").write_text("fixture\n", encoding="utf-8")
        (self.source / ".gitignore").write_text(".agent_reports/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.source), "add", "README.md", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(self.source), "commit", "-qm", "fixture"], check=True)
        self.root = self.source / ".agent_reports"
        self.root.mkdir()
        self.activate()
        gate = PRODUCER_FIXTURE.gate_evidence()
        gate["spec_read"]["source"] = "fixture-terminal-envelope"
        dispatch = PRODUCER_FIXTURE.dispatch_evidence()
        for item in dispatch["tuples"]:
            item["checked_worktree"] = str(self.source.resolve())
        self.route = PRODUCER_FIXTURE.R.compile_route(
            "autopilot-code", "dev", "standard", predicates=[], transport="headless",
            dispatch_evidence=dispatch, cwd=self.source,
            artifact_root=self.root, tracking="tracked", tracked_gate_evidence=gate,
            slug="terminal-envelope-fixture", campaign_key=None, parent_cycle_id=None,
        )
        route_binding = PRODUCER_FIXTURE.L.admit_runtime_route(self.root, self.route)
        self.route_file = Path(route_binding.route_file)
        self.cycle = P.begin(self.root, route_file=self.route_file, capability="autopilot-code",
                             intensity="standard", require_cycle=True)
        self.output = (Path(self.cycle["cycle_dir"]) / "artifacts"
                       / "shards/frame/direction-brief.md")
        self.output.parent.mkdir(parents=True)
        self.output.write_text("Frame direction\n", encoding="utf-8")
        frame = next(node for node in self.route["nodes"] if node.get("id") == "frame")
        self.assertIn("shards/frame/direction-brief.md", frame.get("outputs", []))
        self.assertIn("shards/frame/**", frame.get("write_scope", []))
        identity = PRODUCER_FIXTURE.L.read_root_identity(self.root)
        binding = {
            "schema_version": 1, "contract": T.CONTRACT,
            "owner_attempt_id": "att-owner-frame-fixture",
            "route_file": str(self.route_file.resolve()),
            "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
            "artifact_root": str(self.root.resolve()),
            "root_identity": {"repository_id": identity.repository_id,
                              "artifact_root_id": identity.artifact_root_id},
            "campaign_id": self.cycle["campaign_id"], "cycle_id": self.cycle["cycle_id"],
            "producer_id": self.cycle["producer_id"],
        }
        binding_path = T.producer_binding_path(self.root, self.route["route_id"],
                                               "att-owner-frame-fixture")
        self.owner_binding_path = binding_path
        binding_path.parent.mkdir(parents=True)
        binding_path.write_text(json.dumps(binding, sort_keys=True, separators=(",", ":")) + "\n",
                                encoding="utf-8")
        self.binding = {
            "attempt_id": "att-frame-fixture", "parent_attempt_id": "att-owner-frame-fixture",
            "worker_type": "frame", "route_file": str(self.route_file.resolve()),
            "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
            "route_node": "frame", "artifact_root": str(self.root.resolve()),
        }
        other_gate = PRODUCER_FIXTURE.gate_evidence()
        other_gate["spec_read"]["source"] = "fixture-other-terminal-envelope"
        other_dispatch = PRODUCER_FIXTURE.dispatch_evidence()
        for item in other_dispatch["tuples"]:
            item["checked_worktree"] = str(self.source.resolve())
        self.other_route = PRODUCER_FIXTURE.R.compile_route(
            "autopilot-code", "dev", "standard", predicates=[], transport="headless",
            dispatch_evidence=other_dispatch, cwd=self.source,
            artifact_root=self.root, tracking="tracked", tracked_gate_evidence=other_gate,
            slug="other-terminal-envelope-fixture", campaign_key=None, parent_cycle_id=None,
        )
        other_route_binding = PRODUCER_FIXTURE.L.admit_runtime_route(self.root, self.other_route)
        self.other_route_file = Path(other_route_binding.route_file)
        self.other_cycle = P.begin(
            self.root, route_file=self.other_route_file, capability="autopilot-code",
            intensity="standard", require_cycle=True,
        )
        other_owner = "att-owner-other-route"
        other_binding_path = T.producer_binding_path(
            self.root, self.other_route["route_id"], other_owner)
        other_binding_path.parent.mkdir(parents=True)
        other_binding_path.write_text(json.dumps({
            **binding, "owner_attempt_id": other_owner,
            "route_file": str(self.other_route_file.resolve()),
            "route_id": self.other_route["route_id"],
            "route_hash": self.other_route["route_hash"],
            "campaign_id": self.other_cycle["campaign_id"],
            "cycle_id": self.other_cycle["cycle_id"],
            "producer_id": self.other_cycle["producer_id"],
        }, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    def test_explicit_noncanonical_route_file_is_read_by_the_real_loader(self):
        canonical = Path(self.route_file)
        saved = canonical.with_name(canonical.name + ".saved")
        explicit = self.root / "route-fixture.json"
        explicit.write_bytes(canonical.read_bytes())
        canonical.rename(saved)
        try:
            with mock.patch.object(P, "route_cycle_for", return_value=None):
                context = C.resolve_context(
                    route=self.route, route_file=explicit,
                    environ={"AGENT_DISPATCH_WORKTREE": str(self.source)},
                )
            self.assertEqual(context.route["route_id"], self.route["route_id"])
            self.assertEqual(context.route["route_hash"], self.route["route_hash"])
            self.assertEqual(context.source_worktree, self.source.resolve())
        finally:
            saved.rename(canonical)

    def test_explicit_route_file_mismatch_and_missing_file_do_not_fall_back_by_id(self):
        env = {"AGENT_DISPATCH_WORKTREE": str(self.source)}
        with self.subTest(case="identity-mismatch"):
            with self.assertRaises(C.ArtifactContextError) as raised:
                C.resolve_context(route=self.route, route_file=self.other_route_file, environ=env)
            self.assertEqual(raised.exception.code, "completion-route-hash-mismatch")
        with self.subTest(case="missing-explicit-file"):
            missing = self.root / "missing-route.json"
            with self.assertRaises(C.ArtifactContextError) as raised:
                C.resolve_context(route=self.route, route_file=missing, environ=env)
            self.assertEqual(raised.exception.code, "route-unreadable")

    def _log(self, harness, artifact, suffix):
        envelope = f"artifact: {artifact}\nverdict: PASS\nblocker: none"
        rows = {
            "codex": [
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": envelope}},
                {"type": "turn.completed"},
            ],
            "claude": [{"type": "result", "subtype": "success", "is_error": False,
                        "result": envelope}],
            "opencode": [
                {"type": "text", "part": {"type": "text", "text": envelope}},
                {"type": "step_finish", "part": {"type": "step-finish", "reason": "stop"}},
            ],
        }[harness]
        path = Path(self._tmp.name) / f"{harness}-{suffix}.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _decoded(encoded):
        return Path(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8"))

    def _inspect(self, artifact, *, binding=None, harness="codex", suffix="case"):
        log = self._log(harness, artifact, suffix)
        return terminal.inspect_terminal_attempt(
            log, worktree=self.source, artifact_root_metadata=self.root,
            artifact_binding=self.binding if binding is None else binding,
        )

    def test_three_harness_logs_reconcile_the_exact_cycle_relative_frame_output(self):
        reconciled = None
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness), mock.patch.dict(os.environ, {}, clear=True):
                log = self._log(harness, "shards/frame/direction-brief.md", "relative")
                metadata = {**self.binding, "log_file": str(log), "artifact_root": str(self.root),
                            "worker_type": "stage", "harness": harness}
                artifact, reason = JOIN.route_completion_evidence(metadata, worktree=str(self.source))
                self.assertEqual((artifact, reason), (str(self.output), ""))
                reconciled = artifact

        frame = next(node for node in self.route["nodes"] if node.get("id") == "frame")
        row_metadata = {
            **self.binding, "attempt_schema_version": "2",
            "dispatch_depth": str(frame["dispatch_depth"]), "transport": "headless",
            "execution_surface": "registered-headless", "registered_worker": "1",
            "fallback_hop": "same-harness-headless", "worker_type": "frame",
            "harness": "codex", "artifact_root": str(self.root.resolve()),
        }
        pipe = ",".join(f"{key}={value}" for key, value in row_metadata.items())
        self.jobs.write_text("\t".join([
            "2026-10-16T00:00:00Z", "open", str(self.source), str(self.source), "frame", pipe,
        ]) + "\n", encoding="utf-8")
        marker, row = PRODUCER_FIXTURE.R.complete_node(
            self.route, frame, "frame", Path(reconciled), jobs=self.jobs,
            attempt_id=self.binding["attempt_id"],
        )
        self.assertEqual(row["status"], "closed")
        self.assertEqual(marker["evidence"]["path"], str(self.output))

    def test_parentless_pre_owner_frames_reconcile_and_complete_live_and_sealed_cycles(self):
        self._assert_parentless_frame_reconciles_and_completes("frame")

    def test_parentless_alternative_pre_owner_frames_reconcile_and_complete_live_and_sealed_cycles(self):
        self._assert_parentless_frame_reconciles_and_completes("frame-alternative")

    def _assert_parentless_frame_reconciles_and_completes(self, node_id):
        # This matches the real depth-1 frame shape: route identity is present,
        # but there is no parent owner attempt to bind. Keep the fixture cycle
        # owner-binding-free so route lineage is the only cycle authority.
        self.owner_binding_path.unlink()
        frame = next(node for node in self.route["nodes"] if node.get("id") == node_id)
        self.assertEqual(frame.get("dispatch_depth"), 1)
        self.assertEqual(frame.get("worker_type"), "frame")
        relative = f"shards/{node_id}/direction-brief.md"
        self.assertIn(relative, frame["outputs"])
        output = Path(self.cycle["cycle_dir"]) / "artifacts" / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("Frame direction\n", encoding="utf-8")
        for phase in ("live", "sealed"):
            if phase == "sealed":
                self.close(self.route, self.route_file)
                clean_env = {
                    "AGENT_HOME": str(Path(self._tmp.name) / "agent-home"),
                    "AGENT_DISPATCH_JOBS": str(self.jobs),
                    "HEARTING_GATES": "off",
                }
                with mock.patch.dict(os.environ, clean_env, clear=True):
                    sealed = P.finalize(self.root, cycle_id=self.cycle["cycle_id"])
                self.assertIn(sealed.get("status"), {"sealed", "already-sealed"})

            for harness in ("claude", "codex", "opencode"):
                attempt_id = f"att-parentless-{node_id}-{phase}-{harness}"
                attempt = {key: value for key, value in self.binding.items()
                           if key != "parent_attempt_id"}
                attempt.update({
                    "attempt_id": attempt_id,
                    "route_node": node_id,
                    "dispatch_depth": "1",
                    "worker_type": "frame",
                })
                log = self._log(harness, relative, f"{node_id}-{phase}-parentless")
                metadata = {
                    **attempt,
                    "attempt_schema_version": "2",
                    "transport": "headless",
                    "execution_surface": "registered-headless",
                    "registered_worker": "1",
                    "fallback_hop": "same-harness-headless",
                    "harness": harness,
                    "artifact_root": str(self.root.resolve()),
                    "log_file": str(log),
                }
                self.assertNotIn("parent_attempt_id", metadata)
                ambient = (
                    {},
                    {
                        "AGENT_ARTIFACT_ROOT": str(Path(self._tmp.name) / "wrong-root"),
                        "AGENT_ARTIFACT_CYCLE_ID": "cyc_" + "f" * 32,
                        "AGENT_ARTIFACT_OUTPUT_DIR": str(Path(self._tmp.name) / "wrong-output"),
                        "AGENT_ROUTE_FILE": str(Path(self._tmp.name) / "wrong-route.json"),
                        "AGENT_ROUTE_ID": "rt-unrelated",
                        "AGENT_ROUTE_NODE": "research",
                        "AGENT_DISPATCH_PARENT_ATTEMPT_ID": "att-unrelated-owner",
                    },
                )
                for env in ambient:
                    isolated = {
                        "AGENT_HOME": str(Path(self._tmp.name) / "agent-home"),
                        "AGENT_DISPATCH_JOBS": str(self.jobs),
                        "HEARTING_GATES": "off",
                        **env,
                    }
                    with self.subTest(phase=phase, harness=harness, ambient=bool(env)), \
                            mock.patch.dict(os.environ, isolated, clear=True):
                        resolved, reason = JOIN.route_completion_evidence(
                            metadata, worktree=str(self.source))
                        self.assertEqual((resolved, reason), (str(output), ""))
                        pipe = ",".join(f"{key}={value}" for key, value in metadata.items())
                        self.jobs.write_text("\t".join([
                            "2026-10-16T00:00:00Z", "open", str(self.source), str(self.source),
                            node_id, pipe,
                        ]) + "\n", encoding="utf-8")
                        marker, row = PRODUCER_FIXTURE.R.complete_node(
                            self.route, frame, node_id, Path(resolved), jobs=self.jobs,
                            attempt_id=attempt_id,
                        )
                        self.assertEqual(row["status"], "closed")
                        self.assertEqual(marker["evidence"]["path"], str(output))

    def test_ambient_context_cannot_replace_the_attempt_binding_on_reinspection(self):
        for env in (
            {},
            {"AGENT_ARTIFACT_ROOT": str(Path(self._tmp.name) / "other-root"),
             "AGENT_ROUTE_FILE": str(Path(self._tmp.name) / "other-route.json"),
             "AGENT_ROUTE_ID": "rt-unrelated", "AGENT_ROUTE_NODE": "research",
             "AGENT_ARTIFACT_CYCLE_ID": "cyc_" + "f" * 32,
             "AGENT_ARTIFACT_OUTPUT_DIR": str(Path(self._tmp.name) / "other-output")},
        ):
            with self.subTest(env=bool(env)), mock.patch.dict(os.environ, env, clear=True):
                result = self._inspect("shards/frame/direction-brief.md", suffix="ambient")
                self.assertEqual((result["state"], self._decoded(result["artifact_path_b64"])),
                                 ("valid", self.output))

    def test_absolute_and_legacy_root_relative_handoffs_keep_their_existing_base(self):
        absolute = self._inspect(str(self.output), suffix="absolute")
        self.assertEqual((absolute["state"], self._decoded(absolute["artifact_path_b64"])),
                         ("valid", self.output))
        legacy = self.root / "target-artifact.md"
        legacy.write_text("authorized existing target\n", encoding="utf-8")
        relative = self._inspect("target-artifact.md", suffix="target")
        self.assertEqual((relative["state"], self._decoded(relative["artifact_path_b64"])),
                         ("valid", legacy))
        absolute_target = self._inspect(str(legacy), suffix="absolute-target")
        self.assertEqual((absolute_target["state"],
                          self._decoded(absolute_target["artifact_path_b64"])),
                         ("valid", legacy))

    def test_wrong_owner_cycle_and_node_scope_do_not_select_the_cycle_output(self):
        wrong_owner = {**self.binding, "parent_attempt_id": "att-owner-other-route"}
        wrong_cycle = {**self.binding, "parent_attempt_id": "att-owner-frame-fixture"}
        path = T.producer_binding_path(self.root, self.route["route_id"], "att-owner-frame-fixture")
        original = json.loads(path.read_text(encoding="utf-8"))
        changed = {**original, "campaign_id": self.other_cycle["campaign_id"],
                   "cycle_id": self.other_cycle["cycle_id"],
                   "producer_id": self.other_cycle["producer_id"]}
        path.write_text(json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8")
        try:
            cycle_result = self._inspect("shards/frame/direction-brief.md", binding=wrong_cycle,
                                         suffix="wrong-cycle")
        finally:
            path.write_text(json.dumps(original, sort_keys=True, separators=(",", ":")) + "\n",
                            encoding="utf-8")
        owner_result = self._inspect("shards/frame/direction-brief.md", binding=wrong_owner,
                                     suffix="wrong-owner")
        wrong_scope = {**self.binding, "route_node": "plan"}
        scope_result = self._inspect("shards/frame/direction-brief.md", binding=wrong_scope,
                                     suffix="wrong-scope")
        for result in (cycle_result, owner_result, scope_result):
            self.assertEqual((result["state"], result["reason"]),
                             ("invalid", "artifact-outside-root"))

        missing_binding = {**self.binding, "parent_attempt_id": "att-owner-not-published"}
        missing_result = self._inspect("shards/frame/direction-brief.md", binding=missing_binding,
                                       suffix="missing-owner-binding")
        self.assertEqual((missing_result["state"], missing_result["reason"]),
                         ("invalid", "artifact-outside-root"))

        non_frame = {key: value for key, value in self.binding.items()
                     if key != "parent_attempt_id"}
        non_frame.update({"worker_type": "stage", "dispatch_depth": "1"})
        non_frame_result = self._inspect("shards/frame/direction-brief.md", binding=non_frame,
                                         suffix="parentless-non-frame")
        self.assertEqual((non_frame_result["state"], non_frame_result["reason"]),
                         ("invalid", "artifact-outside-root"))

        wrong_route_file = {**self.binding, "route_file": str(self.other_route_file.resolve())}
        wrong_route_id = {**self.binding, "route_id": self.other_route["route_id"]}
        wrong_route_hash = {**self.binding, "route_hash": "sha256:" + "f" * 64}
        for suffix, bad_binding in (("wrong-route-file", wrong_route_file),
                                    ("wrong-route-id", wrong_route_id),
                                    ("wrong-route-hash", wrong_route_hash)):
            with self.subTest(route_binding=suffix):
                result = self._inspect("shards/frame/direction-brief.md", binding=bad_binding,
                                       suffix=suffix)
                self.assertEqual((result["state"], result["reason"]),
                                 ("invalid", "artifact-outside-root"))

        undeclared = self.output.parent / "not-declared.md"
        undeclared.write_text("inside cycle, absent from node outputs\n", encoding="utf-8")
        undeclared_result = self._inspect("shards/frame/not-declared.md", suffix="undeclared-output")
        self.assertEqual((undeclared_result["state"], undeclared_result["reason"]),
                         ("invalid", "artifact-outside-root"))

    def test_invalid_existing_terminal_row_is_unchanged_by_reinspection(self):
        log = self._log("codex", "../outside.md", "existing-invalid")
        metadata = {
            **self.binding,
            "attempt_schema_version": "2",
            "dispatch_depth": "1",
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": "1",
            "fallback_hop": "same-harness-headless",
            "worker_type": "frame",
            "harness": "codex",
            "artifact_root": str(self.root.resolve()),
            "log_file": str(log),
        }
        pipe = ",".join(f"{key}={value}" for key, value in metadata.items())
        self.jobs.write_text("\t".join([
            "2026-10-16T00:00:00Z", "done", str(self.source), str(self.source),
            "frame", pipe,
        ]) + "\n", encoding="utf-8")
        before = self.jobs.read_bytes()
        artifact, reason = JOIN.route_completion_evidence(metadata, worktree=str(self.source))
        self.assertIsNone(artifact)
        self.assertEqual(reason, "evidence-not-valid")
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_traversal_symlink_escape_and_ambiguous_legacy_output_remain_refused(self):
        traversal = self._inspect("../outside.md", suffix="traversal")
        self.assertEqual((traversal["state"], traversal["reason"]),
                         ("invalid", "artifact-outside-root"))

        self.output.unlink()
        outside = Path(self._tmp.name) / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        self.output.symlink_to(outside)
        symlink = self._inspect("shards/frame/direction-brief.md", suffix="symlink")
        self.assertEqual((symlink["state"], symlink["reason"]),
                         ("invalid", "artifact-outside-root"))
        self.output.unlink()
        self.output.write_text("Frame direction\n", encoding="utf-8")

        duplicate = self.root / "shards/frame/direction-brief.md"
        duplicate.parent.mkdir(parents=True)
        duplicate.write_text("legacy duplicate\n", encoding="utf-8")
        ambiguous = self._inspect("shards/frame/direction-brief.md", suffix="ambiguous")
        self.assertEqual((ambiguous["state"], ambiguous["artifact_state"]),
                         ("invalid", "unchecked"))


if __name__ == "__main__":
    unittest.main()
