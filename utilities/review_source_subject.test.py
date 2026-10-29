#!/usr/bin/env python3
"""Effective source-subject identity, binding, and replacement regressions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import review_source_subject as SUBJECT  # noqa: E402


class SourceSubjectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.com")
        self.git("config", "user.name", "Fixture")
        for relative, content in {
            "engine.py": "value = 'A'\n",
            "tests/test_engine.py": "assert True\n",
            "core/CONVENTIONS.md": "runtime meaning\n",
            "docs/manual.md": "pure documentation\n",
            "README.md": "pure root documentation\n",
            "checklist.md": "artifact tracking\n",
            "evidence/result.json": "{}\n",
            "dev_logs/run.md": "artifact log\n",
            ".agent_reports/report.md": "shadow report\n",
            ".gitignore": "ignored-output.log\n",
        }.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (self.repo / "outside-target").write_text("outside\n", encoding="utf-8")
        (self.repo / "external-link").symlink_to("outside-target")
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture A")
        self.route = {
            "route_id": "rt-subject", "route_hash": "sha256:subject",
            "cwd": str(self.repo), "artifact_root": str(self.repo / ".agent_reports"),
            "nodes": [
                {"id": "execute", "kind": "pipeline-stage", "completion_gate": "code-execute",
                 "write_scope": ["source/**", "checklist.md", "dev_logs/**", "evidence/**"],
                 "depends_on": []},
                {"id": "impl-review", "kind": "review-worker", "unit": "qa/code-review",
                 "completion_gate": "code-impl-review", "write_scope": ["_internal/dev_reviews/**"],
                 "depends_on": ["execute"]},
                {"id": "smoke", "kind": "pipeline-stage", "unit": "qa/test",
                 "completion_gate": "code-test", "write_scope": ["test_logs/**"],
                 "depends_on": ["impl-review"]},
            ],
        }
        self.node = self.route["nodes"][1]
        self.test_node = self.route["nodes"][2]

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def observe(self):
        return SUBJECT.observe(self.route, self.node, worktree=self.repo)

    def test_source_bytes_dirty_index_and_same_content_commit(self):
        before = self.observe()
        source = self.repo / "engine.py"
        source.write_text("value = 'B'\n", encoding="utf-8")
        dirty = self.observe()
        self.assertNotEqual(before["digest"], dirty["digest"])
        self.git("add", "engine.py")
        staged = self.observe()
        self.assertEqual(dirty["digest"], staged["digest"])
        self.git("commit", "-qm", "source B")
        committed = self.observe()
        self.assertEqual(staged["digest"], committed["digest"])
        self.git("commit", "--allow-empty", "-qm", "empty publication")
        empty_commit = self.observe()
        self.assertEqual(committed["digest"], empty_commit["digest"])
        self.assertNotEqual(before["provenance"]["head"], committed["provenance"]["head"])
        source = self.repo / "engine.py"
        source.write_text("value = 'C'\n", encoding="utf-8")
        self.git("add", "engine.py")
        source.write_text("value = 'B'\n", encoding="utf-8")
        index_only = self.observe()
        self.assertEqual(committed["digest"], index_only["digest"])
        self.assertNotEqual(committed["provenance"]["index_tree"],
                            index_only["provenance"]["index_tree"])

    def test_source_modes_symlink_delete_rename_and_untracked_files_are_effective(self):
        original = self.observe()["digest"]
        source = self.repo / "engine.py"
        source.chmod(source.stat().st_mode ^ 0o100)
        mode_changed = self.observe()["digest"]
        self.assertNotEqual(original, mode_changed)
        link = self.repo / "external-link"
        link.unlink()
        link.symlink_to("engine.py")
        link_changed = self.observe()["digest"]
        self.assertNotEqual(mode_changed, link_changed)
        (self.repo / "new_source.py").write_text("new = True\n", encoding="utf-8")
        untracked = self.observe()["digest"]
        self.assertNotEqual(link_changed, untracked)
        (self.repo / "tests" / "test_engine.py").unlink()
        deleted = self.observe()["digest"]
        self.assertNotEqual(untracked, deleted)
        self.git("mv", "engine.py", "renamed_engine.py")
        renamed = self.observe()["digest"]
        self.assertNotEqual(deleted, renamed)

    def test_source_snapshot_works_when_repository_status_is_unavailable(self):
        before = self.observe()
        original_git = SUBJECT._git

        def without_status(root, *args, **kwargs):
            if args and args[0] == "status":
                raise SUBJECT.SourceSubjectError("repository-status-unavailable")
            return original_git(root, *args, **kwargs)

        with mock.patch.object(SUBJECT, "_git", side_effect=without_status):
            current = self.observe()
        self.assertEqual(current["digest"], before["digest"])
        self.assertEqual(current["manifest"], before["manifest"])

    def test_new_untracked_source_during_capture_is_reobserved(self):
        original_reader = SUBJECT._read_regular
        added = False

        def add_source(path):
            nonlocal added
            result = original_reader(path)
            if path.name == "engine.py" and not added:
                added = True
                (self.repo / "late_source.py").write_text("late = True\n", encoding="utf-8")
            return result

        with mock.patch.object(SUBJECT, "_read_regular", side_effect=add_source):
            current = self.observe()
        self.assertIn("late_source.py", {item["path"] for item in current["manifest"]})

    def test_source_edit_during_final_inventory_is_reobserved(self):
        original_entries = SUBJECT._index_entries
        calls = 0
        updated = "value = 'late'\n"

        def edit_after_inventory(root):
            nonlocal calls
            result = original_entries(root)
            calls += 1
            if calls == 2:
                (self.repo / "engine.py").write_text(updated, encoding="utf-8")
            return result

        with mock.patch.object(SUBJECT, "_index_entries", side_effect=edit_after_inventory):
            current = self.observe()
        engine = next(item for item in current["manifest"] if item["path"] == "engine.py")
        self.assertEqual(engine["content_sha256"], hashlib.sha256(updated.encode()).hexdigest())

    def test_unrelated_artifact_churn_does_not_block_source_capture(self):
        before = self.observe()
        original_reader = SUBJECT._read_regular
        writes = 0

        def append_artifact(path):
            nonlocal writes
            result = original_reader(path)
            if path.name == "engine.py":
                writes += 1
                (self.repo / "dev_logs" / f"output-{writes}.bin").write_bytes(b"output\n")
            return result

        with mock.patch.object(SUBJECT, "_read_regular", side_effect=append_artifact):
            current = self.observe()
        self.assertEqual(current["digest"], before["digest"])
        self.assertEqual(writes, 1)

    def test_head_and_index_changes_during_capture_are_reobserved(self):
        original_reader = SUBJECT._read_regular
        for change in ("head", "index"):
            with self.subTest(change=change):
                before = self.observe()
                changed = False

                def change_git_state(path):
                    nonlocal changed
                    result = original_reader(path)
                    if path.name == "engine.py" and not changed:
                        changed = True
                        if change == "head":
                            self.git("commit", "--allow-empty", "-qm", "during capture")
                        else:
                            self.git("update-index", "--chmod=+x", "engine.py")
                    return result

                with mock.patch.object(SUBJECT, "_read_regular", side_effect=change_git_state):
                    current = self.observe()
                key = "head" if change == "head" else "index_tree"
                self.assertNotEqual(current["provenance"][key], before["provenance"][key])
                self.assertEqual(current["digest"], before["digest"])

    def test_semantic_markdown_is_source_and_reports_docs_and_artifacts_are_not(self):
        before = self.observe()["digest"]
        (self.repo / "README.md").write_text("changed readme\n", encoding="utf-8")
        (self.repo / "docs" / "manual.md").write_text("changed docs\n", encoding="utf-8")
        (self.repo / "checklist.md").write_text("changed checklist\n", encoding="utf-8")
        (self.repo / "evidence" / "result.json").write_text("changed evidence\n", encoding="utf-8")
        (self.repo / "dev_logs" / "run.md").write_text("changed log\n", encoding="utf-8")
        (self.repo / ".agent_reports" / "report.md").write_text("changed report\n", encoding="utf-8")
        (self.repo / "ignored-output.log").write_text("ignored\n", encoding="utf-8")
        self.assertEqual(before, self.observe()["digest"])
        (self.repo / "core" / "CONVENTIONS.md").write_text("changed runtime meaning\n", encoding="utf-8")
        self.assertNotEqual(before, self.observe()["digest"])

    def test_artifact_only_stage_scopes_do_not_become_source_subjects(self):
        planning_node = {
            "id": "plan-alternative", "kind": "pipeline-stage",
            "write_scope": ["plan.alternative.md", "checklist.alternative.md"],
        }
        self.assertFalse(SUBJECT.has_source_subject(self.route, planning_node))
        self.assertIsNone(SUBJECT.marker_subject(
            self.route, planning_node, completion_dir=self.temp,
        ))
        self.assertTrue(SUBJECT.has_source_subject(self.route, self.route["nodes"][0]))

    def _design_fixture(self, *, legacy=False):
        import artifact_producer
        output = Path(self.temp.name) / ("legacy-cycle" if legacy else "flat-cycle") / "artifacts"
        build_scopes = (["designs/<cycle>/02_tokens/**", "designs/<cycle>/03_components/**",
                         "spec/design/02_tokens/**", "spec/design/03_components/**"] if legacy else
                        ["designs/02_tokens/**", "designs/03_components/**",
                         "spec/design/02_tokens/**", "spec/design/03_components/**"])
        build = {"id": "build", "kind": "pipeline-stage", "completion_gate": "design-build",
                 "write_scope": build_scopes, "depends_on": []}
        visual = {"id": "visual-verify", "kind": "review-worker", "unit": "design/verifier",
                  "completion_gate": "design-verify", "depends_on": ["build"]}
        critic = {"id": "critic-review", "kind": "review-worker", "unit": "design/critic",
                  "completion_gate": "design-critic", "depends_on": ["build"]}
        route = {"route_id": "rt-design-subject", "route_hash": "sha256:design-subject",
                 "artifact_root": str(Path(self.temp.name) / "artifacts"),
                 "cwd": str(self.repo), "nodes": [build, visual, critic]}
        root = "designs/<cycle>" if legacy else "designs"
        for rel, data in ((f"{root}/02_tokens/tokens.json", b'{"color":"blue"}\n'),
                          (f"{root}/03_components/card.html", b"<article>Card</article>\n"),
                          ("spec/design/02_tokens/contract.json", b'{"token":"color"}\n')):
            path = output / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        cycle = {"campaign_id": "camp_fixture", "cycle_id": "cyc_fixture",
                 "route_id": route["route_id"]}
        return route, visual, critic, output, cycle, artifact_producer

    def test_design_review_uses_same_sealed_build_subject_and_excludes_review_output(self):
        route, visual, critic, output, cycle, producer = self._design_fixture()
        with mock.patch.object(producer, "route_cycle_for", return_value=cycle), \
             mock.patch.object(producer, "cycle_dir", return_value=output.parent):
            before = SUBJECT.observe(route, visual)
            self.assertEqual(SUBJECT.observe(route, critic)["digest"], before["digest"])
            self.assertEqual(before["family"], "design-build")
            report = output / "designs/04_review/verify/verdict.yaml"
            report.parent.mkdir(parents=True)
            report.write_text("verdict: PASS\n", encoding="utf-8")
            self.assertEqual(SUBJECT.observe(route, visual)["digest"], before["digest"])
            token = output / "designs/02_tokens/tokens.json"
            token.write_bytes(b'{"color":"green"}\n')
            changed = SUBJECT.observe(route, visual)
            self.assertNotEqual(changed["digest"], before["digest"])
            token.write_bytes(b'{"color":"blue"}\n')
            self.assertEqual(SUBJECT.observe(route, visual)["digest"], before["digest"])

    def test_literal_legacy_design_build_scopes_are_observed_without_rewriting_them(self):
        route, visual, _critic, output, cycle, producer = self._design_fixture(legacy=True)
        self.assertIn("designs/<cycle>/02_tokens/**", route["nodes"][0]["write_scope"])
        with mock.patch.object(producer, "route_cycle_for", return_value=cycle), \
             mock.patch.object(producer, "cycle_dir", return_value=output.parent):
            subject = SUBJECT.observe(route, visual)
        self.assertTrue(any(item["path"].startswith("designs/<cycle>/")
                            for item in subject["manifest"]))

    def test_review_input_subject_hashes_consumed_bytes_not_publication_path(self):
        import review_input
        producer = {"id": "plan", "write_scope": ["plan.md", "checklist.md"]}
        node = {"id": "plan-check", "kind": "review-worker", "unit": "qa/plan-review",
                "completion_gate": "code-plan-check", "depends_on": ["plan"]}
        route = {"route_id": "rt-plan-subject", "route_hash": "sha256:plan-subject",
                 "nodes": [producer, node]}
        left = Path(self.temp.name) / "first" / "plan.md"
        right = Path(self.temp.name) / "republished" / "plan.md"
        for path in (left, right):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("approved plan\n", encoding="utf-8")
            (path.parent / "checklist.md").write_text("same checklist\n", encoding="utf-8")
        def candidate(path, publication):
            return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "producer": {"attempt_id": publication, "marker_digest": publication}}
        first = SUBJECT.observe(route, node, reviewed_input=candidate(left, "attempt-A"))
        second = SUBJECT.observe(route, node, reviewed_input=candidate(right, "attempt-B"))
        self.assertTrue(SUBJECT.has_review_subject(route, node, reviewed_input=candidate(left, "A")))
        self.assertEqual(first["family"], "document:plan")
        self.assertEqual(first["digest"], second["digest"])
        (right.parent / "checklist.md").write_text("changed checklist\n", encoding="utf-8")
        self.assertNotEqual(first["digest"], SUBJECT.observe(
            route, node, reviewed_input=candidate(right, "attempt-C"))["digest"])
        tampered = candidate(left, "attempt-A")
        tampered["sha256"] = "0" * 64
        with self.assertRaises(SUBJECT.SourceSubjectError):
            SUBJECT.observe(route, node, reviewed_input=tampered)

    def test_unmarked_intermediate_review_binding_advances_design_generation(self):
        route, visual, _critic, output, cycle, producer = self._design_fixture()
        state = Path(self.temp.name) / "design-dispatch"
        state.mkdir()
        jobs = state / "jobs.log"
        jobs.write_text("", encoding="utf-8")
        completion = state / "completion" / route["route_id"]
        def register(attempt):
            binding = SUBJECT.seal_attempt(jobs, route, visual, attempt,
                                           expected_subject_digest=None)
            metadata = {"attempt_id": attempt, "route_id": route["route_id"],
                        "route_hash": route["route_hash"], "route_node": visual["id"],
                        **binding}
            row = "\t".join(("fixture", "done", str(self.repo), str(self.repo), attempt,
                             ",".join(f"{key}={value}" for key, value in metadata.items())))
            with jobs.open("a", encoding="utf-8") as stream:
                stream.write(row + "\n")
            return SUBJECT.read_binding(jobs, metadata)
        with mock.patch.object(producer, "route_cycle_for", return_value=cycle), \
             mock.patch.object(producer, "cycle_dir", return_value=output.parent):
            a1 = register("att-design-A1")
            (output / "designs/02_tokens/tokens.json").write_bytes(b'{"color":"green"}\n')
            _b = register("att-design-B")  # no completion marker for B
            (output / "designs/02_tokens/tokens.json").write_bytes(b'{"color":"blue"}\n')
            a2 = register("att-design-A2")
        self.assertEqual(a1["subject_digest"], a2["subject_digest"])
        self.assertLess(a1["source_epoch"], a2["source_epoch"])

    def test_other_capped_reviews_bind_only_concrete_sealed_artifact_inputs(self):
        import artifact_producer
        build = {"id": "report", "kind": "pipeline-stage", "depends_on": [],
                 "write_scope": ["report/REPORT.md", "report/index.html",
                                 "report/report_manifest.json", "report/media/**"]}
        verify = {"id": "independent-verify", "kind": "review-worker",
                  "completion_gate": "lab-independent-verify", "unit": "qa/reviewer",
                  "depends_on": ["report"], "inputs": ["report/REPORT.md", "report/index.html",
                      "report/report_manifest.json", "report/media/**"],
                  "write_scope": ["reviews/eval/**"]}
        route = {"route_id": "rt-report-review", "route_hash": "sha256:report-review",
                 "artifact_root": str(Path(self.temp.name) / "artifacts"),
                 "cwd": str(self.repo), "nodes": [build, verify]}
        cycle_dir = Path(self.temp.name) / "report-cycle"
        output = cycle_dir / "artifacts"
        for relative, data in (("report/REPORT.md", b"report A"),
                               ("report/index.html", b"<main>A</main>"),
                               ("report/report_manifest.json", b'{"version":1}'),
                               ("report/media/image.png", b"image A")):
            path = output / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        cycle = {"campaign_id": "camp_report", "cycle_id": "cyc_report",
                 "route_id": route["route_id"]}
        with mock.patch.object(artifact_producer, "route_cycle_for", return_value=cycle), \
             mock.patch.object(artifact_producer, "cycle_dir", return_value=cycle_dir):
            first = SUBJECT.observe(route, verify)
            self.assertEqual(first["family"], "artifact-review:lab-independent-verify")
            self.assertTrue(SUBJECT.has_review_subject(route, verify))
            output_report = output / "reviews/eval/verdict.yaml"
            output_report.parent.mkdir(parents=True)
            output_report.write_text("verdict: PASS\n", encoding="utf-8")
            self.assertEqual(SUBJECT.observe(route, verify)["digest"], first["digest"])
            report = output / "report/REPORT.md"
            report.write_bytes(b"report B")
            self.assertNotEqual(SUBJECT.observe(route, verify)["digest"], first["digest"])
            report.unlink()
            outside = Path(self.temp.name) / "outside-report.md"
            outside.write_bytes(b"outside")
            report.symlink_to(outside)
            with self.assertRaises(SUBJECT.SourceSubjectError) as refused:
                SUBJECT.observe(route, verify)
            self.assertEqual(refused.exception.reason, "review-artifact-input-symlink")

        resource = {"id": "smoke", "kind": "review-worker", "completion_gate": "hash-bound-smoke",
                    "inputs": ["experiment-scaffold", "config"], "depends_on": [],
                    "write_scope": ["reviews/smoke/**"]}
        resource_route = {"route_id": "rt-resource", "nodes": [resource]}
        self.assertFalse(SUBJECT.has_review_subject(resource_route, resource))

    def test_missing_optional_artifact_bytes_do_not_create_a_new_input_gate(self):
        import artifact_producer
        producer = {"id": "report", "kind": "pipeline-stage", "depends_on": [],
                    "write_scope": ["report/REPORT.md"]}
        verify = {"id": "independent-verify", "kind": "review-worker",
                  "completion_gate": "lab-independent-verify", "unit": "qa/reviewer",
                  "depends_on": ["report"], "inputs": ["report/REPORT.md"],
                  "write_scope": ["reviews/eval/**"]}
        artifact_root = Path(self.temp.name) / "missing-root"
        cycle_dir = Path(self.temp.name) / "missing-cycle"
        route = {"route_id": "rt-missing-report", "route_hash": "sha256:missing-report",
                 "artifact_root": str(artifact_root), "cwd": str(self.repo),
                 "nodes": [producer, verify]}
        cycle = {"campaign_id": "camp_missing", "cycle_id": "cyc_missing",
                 "route_id": route["route_id"]}
        with mock.patch.object(artifact_producer, "route_cycle_for", return_value=cycle), \
             mock.patch.object(artifact_producer, "cycle_dir", return_value=cycle_dir):
            self.assertFalse(SUBJECT.has_review_subject(route, verify))
            self.assertIsNone(SUBJECT.marker_currency(
                route, verify, {"attempt_id": "att-legacy"},
                Path(self.temp.name) / "state" / "completion" / route["route_id"],
                jobs=Path(self.temp.name) / "state" / "jobs.log"))

    def test_capped_selector_table_keeps_unresolved_symbols_on_the_legacy_budget(self):
        import artifact_producer
        cases = (
            ("quality-review", ["draft/**"], ["draft/**"], [("draft/body.md", b"draft")], True),
            ("run-verify", ["run.json", "logs/**"], ["run.json", "logs/**"],
             [("run.json", b'{"run":1}'), ("logs/output.log", b"large log")], True),
            ("claim-verify", ["report/**", "cards/**"], ["report/**", "cards/**"],
             [("report/claim.md", b"claim"), ("cards/one.md", b"card")], True),
            ("release-review", ["release-config/**", "ship-checklist"],
             ["release-config/**", "ship-checklist"], [("release-config/release.json", b"{}")], True),
            ("post-deploy-verify", ["deploy-record.json", "release-config/**"],
             ["deploy-record.json", "release-config/**"],
             [("deploy-record.json", b"{}"), ("release-config/release.json", b"{}")], True),
            ("verify", ["user_profile/**"], ["user_profile/**"],
             [("root:user_profile/profile.md", b"profile")], True),
            ("smoke", ["experiment-scaffold", "config"],
             ["experiment-scaffold/**", "config/**"],
             [("experiment-scaffold/scaffold.json", b"{}"), ("config/config.json", b"{}")], False),
            ("inspect", ["target-artifact"], ["target-artifact"], [], False),
            ("qa", ["analysis_project/<mode>/**"], ["analysis_project/<mode>/**"], [], False),
        )
        with mock.patch.object(artifact_producer, "route_cycle_for", side_effect=lambda _root, route: {
                "campaign_id": "camp-table", "cycle_id": "cyc-table", "route_id": route["route_id"]}), \
             mock.patch.object(artifact_producer, "cycle_dir", side_effect=lambda _root, *_args:
                               self.repo / "generic-cycle"):
            for index, (node_id, inputs, scopes, payloads, expected) in enumerate(cases):
                artifact_root = Path(self.temp.name) / f"root-{index}"
                output = self.repo / "generic-cycle" / "artifacts"
                output.mkdir(parents=True, exist_ok=True)
                producer = {"id": "producer", "write_scope": scopes, "depends_on": []}
                node = {"id": node_id, "kind": "review-worker", "completion_gate": node_id,
                        "inputs": inputs, "depends_on": ["producer"],
                        "unit": "qa/reviewer", "write_scope": ["reviews/check/**"]}
                route = {"route_id": f"rt-selector-{index}", "route_hash": "sha256:"+str(index)*64,
                         "artifact_root": str(artifact_root), "nodes": [producer, node]}
                for relative, data in payloads:
                    if relative.startswith("root:"):
                        path = artifact_root / relative[5:]
                    else:
                        path = output / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                self.assertEqual(SUBJECT.has_review_subject(route, node), expected, node_id)

    def test_custom_code_test_gate_binds_source_and_uses_ancestor_source_scope(self):
        self.assertTrue(SUBJECT.has_review_subject(self.route, self.test_node))
        self.assertTrue(SUBJECT.has_source_subject(self.route, self.test_node))
        self.assertIn("source/**", SUBJECT.source_scopes(self.route, self.test_node))
        jobs = Path(self.temp.name) / "state" / "jobs.log"
        jobs.parent.mkdir()
        route_file = Path(self.temp.name) / "route.json"
        route_file.write_text(json.dumps(self.route), encoding="utf-8")
        values = SUBJECT.seal_attempt(jobs, self.route, self.test_node,
                                      "att-test-source-A", worktree=self.repo)
        metadata = {"route_file": str(route_file), "route_id": self.route["route_id"],
                    "route_hash": self.route["route_hash"], "route_node": self.test_node["id"],
                    "completion_gate": "code-test", "unit": "qa/test",
                    "attempt_id": "att-test-source-A", **values}
        fields = ["now", "open", str(self.repo), str(self.repo), "test-source-A", ""]
        self.assertEqual(SUBJECT.validate_attempt(jobs, fields, metadata, require_new=True)["subject_digest"],
                         values["review_subject_digest"])
        marker_subject = SUBJECT.marker_subject(
            self.route, self.test_node, attempt_id=metadata["attempt_id"],
            attempt_metadata=metadata, jobs=jobs, worktree=self.repo,
            completion_dir=Path(self.temp.name) / "completion" / self.route["route_id"],
        )
        self.assertEqual(marker_subject["digest"], values["review_subject_digest"])
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        with self.assertRaises(SUBJECT.SourceSubjectError) as refused:
            SUBJECT.validate_attempt(jobs, fields, metadata)
        self.assertEqual(refused.exception.reason, "review-source-subject-changed-before-launch")
        with self.assertRaises(SUBJECT.SourceSubjectError) as completion_refused:
            SUBJECT.marker_subject(
                self.route, self.test_node, attempt_id=metadata["attempt_id"],
                attempt_metadata=metadata, jobs=jobs, worktree=self.repo,
                completion_dir=Path(self.temp.name) / "completion" / self.route["route_id"],
            )
        self.assertEqual(completion_refused.exception.reason,
                         "review-source-subject-changed-before-completion")

    def test_inline_code_test_marker_carries_current_source_without_a_registered_binding(self):
        completion = Path(self.temp.name) / "completion" / self.route["route_id"]
        attempt = "att-inline-code-test"
        metadata = {"attempt_id": attempt, "registered_worker": False,
                    "execution_surface": "inline"}
        subject = SUBJECT.marker_subject(
            self.route, self.test_node, attempt_id=attempt, attempt_metadata=metadata,
            worktree=self.repo, completion_dir=completion,
        )
        self.assertEqual(subject["digest"], self.observe_test()["digest"])
        marker = {"route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                  "node_id": self.test_node["id"], "completion_gate": "code-test",
                  "attempt_id": attempt, "registered_worker": False,
                  "execution_surface": "inline", "source_subject": subject}
        self.assertIsNone(SUBJECT.marker_currency(
            self.route, self.test_node, marker, completion,
        ))
        legacy = {key: value for key, value in marker.items() if key != "source_subject"}
        self.assertEqual(SUBJECT.marker_currency(self.route, self.test_node, legacy, completion),
                         ("superseded", "completion-source-subject-unknown"))

    def test_owner_inline_review_provenance_needs_its_exact_attempt(self):
        completion = Path(self.temp.name) / "completion" / self.route["route_id"]
        attempt = "att-owner-inline-review"
        subject = SUBJECT.marker_subject(
            self.route, self.node, attempt_id=attempt, worktree=self.repo,
            completion_dir=completion, owner_inline=True,
        )
        self.assertEqual(subject["inline_provenance"]["attempt_id"], attempt)
        marker = {"route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                  "node_id": self.node["id"], "completion_gate": "code-impl-review",
                  "attempt_id": attempt, "registered_worker": False,
                  "execution_surface": "inline", "reviewer_kind": "owner-inline",
                  "review_independence": "degraded", "reviewer_identity": attempt,
                  "source_subject": subject}
        self.assertIsNone(SUBJECT.marker_currency(self.route, self.node, marker, completion))
        # Without an exact attempt the provenance could never validate, so the
        # marker keeps the historical source-unknown shape and is rerun.
        self.assertIsNone(SUBJECT.marker_subject(
            self.route, self.node, attempt_id=None, worktree=self.repo,
            completion_dir=completion, owner_inline=True,
        ))

    def test_owner_inline_does_not_rebind_historical_evidence_subject(self):
        current_digest = self.observe()["digest"]
        evidence_sha = hashlib.sha256(b"previous review evidence").hexdigest()
        completion_root = Path(self.temp.name) / "completion"
        cases = (
            ("unknown", None, "review-source-binding-missing"),
            ("wrong-inline-attempt", {
                "schema_version": SUBJECT.SCHEMA_VERSION, "digest": current_digest,
                "epoch": 0, "inline_provenance": {
                    "kind": "owner-inline", "attempt_id": "att-other-review",
                    "manifest_sha256": "0" * 64,
                }}, "review-source-binding-missing"),
            ("stale", {"schema_version": SUBJECT.SCHEMA_VERSION,
                        "digest": "0" * 64, "epoch": 0},
             "review-source-subject-changed-before-completion"),
            ("same", {"schema_version": SUBJECT.SCHEMA_VERSION,
                      "digest": current_digest, "epoch": 0}, None),
        )
        for label, subject, expected_error in cases:
            with self.subTest(history=label):
                route = {**self.route, "route_id": f"rt-subject-{label}"}
                completion = completion_root / route["route_id"]
                completion.mkdir(parents=True)
                marker = {
                    "schema_version": 2, "route_id": route["route_id"],
                    "route_hash": route["route_hash"], "registry_digest": "sha256:fixture",
                    "node_id": self.node["id"],
                    "completion_gate": self.node["completion_gate"], "sequence": 1,
                    "attempt_id": f"att-history-{label}",
                    "evidence": {"path": "historical.md", "sha256": evidence_sha},
                }
                if subject is not None:
                    marker["source_subject"] = subject
                encoded = json.dumps(marker, sort_keys=True).encode("utf-8")
                (completion / "impl-review.1.json").write_bytes(encoded)
                (completion / "impl-review.json").write_bytes(encoded)

                if expected_error:
                    with self.assertRaises(SUBJECT.SourceSubjectError) as refused:
                        SUBJECT.marker_subject(
                            route, self.node, attempt_id="att-new-inline-review",
                            worktree=self.repo, completion_dir=completion,
                            owner_inline=True, evidence_sha256=evidence_sha,
                        )
                    self.assertEqual(refused.exception.reason, expected_error)
                else:
                    fresh = SUBJECT.marker_subject(
                        route, self.node, attempt_id="att-new-inline-review",
                        worktree=self.repo, completion_dir=completion,
                        owner_inline=True, evidence_sha256=evidence_sha,
                    )
                    self.assertEqual((fresh["digest"], fresh["epoch"]),
                                     (current_digest, 0))

    def _write_head(self, completion, *, route, node_id, sequence, subject=None,
                    evidence_sha=None, attempt="att-recorded", history=True, canonical=True):
        marker = {
            "schema_version": 2, "route_id": route["route_id"],
            "route_hash": route["route_hash"], "registry_digest": "sha256:fixture",
            "node_id": node_id,
            "completion_gate": next(item["completion_gate"] for item in route["nodes"]
                                    if item["id"] == node_id),
            "sequence": sequence, "attempt_id": attempt,
            "evidence": {"path": "recorded.md",
                         "sha256": evidence_sha or hashlib.sha256(attempt.encode()).hexdigest()},
        }
        if subject is not None:
            marker["source_subject"] = subject
        encoded = json.dumps(marker, sort_keys=True).encode("utf-8")
        completion.mkdir(parents=True, exist_ok=True)
        if history:
            (completion / f"{node_id}.{sequence}.json").write_bytes(encoded)
        if canonical:
            (completion / f"{node_id}.json").write_bytes(encoded)
        return marker

    def test_fresh_inline_binding_consumes_canonical_head_with_its_exact_history(self):
        digest = self.observe()["digest"]
        old_sha = hashlib.sha256(b"previous review evidence").hexdigest()
        new_sha = hashlib.sha256(b"unrecorded review evidence").hexdigest()
        completion_root = Path(self.temp.name) / "head-history"

        def bind(label, evidence_sha):
            route = {**self.route, "route_id": f"rt-head-{label}"}
            return route, completion_root / route["route_id"]

        def fresh(route, completion, evidence_sha):
            return SUBJECT.marker_subject(
                route, self.node, attempt_id="att-new-inline-review", worktree=self.repo,
                completion_dir=completion, owner_inline=True, evidence_sha256=evidence_sha)

        # A canonical head without its own numbered history is refused for old and fresh
        # bytes, whether it is source-unknown or source-known, and nothing is rewritten.
        for label, subject in (("unknown", None), ("known", {
                "schema_version": SUBJECT.SCHEMA_VERSION, "digest": digest, "epoch": 0})):
            for evidence_sha in (old_sha, new_sha):
                with self.subTest(missing=label, evidence=evidence_sha[:6]):
                    route, completion = bind(f"missing-{label}-{evidence_sha[:6]}", evidence_sha)
                    self._write_head(completion, route=route, node_id="impl-review", sequence=1,
                                     subject=subject, evidence_sha=old_sha, history=False)
                    before = (completion / "impl-review.json").read_bytes()
                    with self.assertRaises(SUBJECT.SourceSubjectError) as refused:
                        fresh(route, completion, evidence_sha)
                    self.assertEqual(refused.exception.reason, "review-source-marker-history-missing")
                    self.assertEqual((completion / "impl-review.json").read_bytes(), before)
                    self.assertFalse((completion / "impl-review.1.json").exists())

        # Malformed, foreign and different-content history are refused before any fresh subject.
        for label in ("malformed", "foreign-route", "foreign-node", "different-content"):
            with self.subTest(history=label):
                route, completion = bind(label, new_sha)
                marker = self._write_head(completion, route=route, node_id="impl-review",
                                          sequence=1, evidence_sha=old_sha)
                history = completion / "impl-review.1.json"
                if label == "malformed":
                    history.write_text("{invalid", encoding="utf-8")
                elif label == "foreign-route":
                    history.write_text(json.dumps({**marker, "route_hash": "sha256:other"}))
                elif label == "foreign-node":
                    history.write_text(json.dumps({**marker, "node_id": "smoke"}))
                else:
                    history.write_text(json.dumps({**marker, "attempt_id": "att-other"}))
                with self.assertRaises(SUBJECT.SourceSubjectError) as refused:
                    fresh(route, completion, new_sha)
                self.assertIn(refused.exception.reason, {
                    "review-source-marker-unreadable", "review-source-marker-history-conflict"})

        # A foreign or sequence-less canonical head is refused too.
        for label, patch in (("foreign-canonical", {"route_hash": "sha256:other"}),
                             ("sequence-less", {"sequence": 0})):
            with self.subTest(canonical=label):
                route, completion = bind(label, new_sha)
                marker = self._write_head(completion, route=route, node_id="impl-review",
                                          sequence=1, evidence_sha=old_sha)
                (completion / "impl-review.json").write_text(json.dumps({**marker, **patch}))
                with self.assertRaises(SUBJECT.SourceSubjectError):
                    fresh(route, completion, new_sha)

        # Normal relations remain admitted: an intact unknown head, an absent head with a
        # valid pending history, and an intact head with a valid pending history ahead of it.
        for label in ("intact-unknown", "absent-canonical-pending", "pending-ahead"):
            with self.subTest(normal=label):
                route, completion = bind(label, new_sha)
                if label == "intact-unknown":
                    self._write_head(completion, route=route, node_id="impl-review",
                                     sequence=1, evidence_sha=old_sha)
                elif label == "absent-canonical-pending":
                    self._write_head(completion, route=route, node_id="impl-review",
                                     sequence=1, evidence_sha=old_sha, canonical=False)
                else:
                    self._write_head(completion, route=route, node_id="impl-review",
                                     sequence=1, evidence_sha=old_sha)
                    self._write_head(completion, route=route, node_id="impl-review",
                                     sequence=2, evidence_sha=hashlib.sha256(b"pending").hexdigest(),
                                     canonical=False, subject={
                                         "schema_version": SUBJECT.SCHEMA_VERSION,
                                         "digest": digest, "epoch": 0})
                result = fresh(route, completion, new_sha)
                self.assertEqual((result["digest"], result["epoch"]), (digest, 0))
                # The old unknown bytes remain unknown even next to the admitted new evidence.
                if label != "pending-ahead":
                    with self.assertRaises(SUBJECT.SourceSubjectError) as rebound:
                        fresh(route, completion, old_sha)
                    self.assertEqual(rebound.exception.reason, "review-source-binding-missing")

    def test_source_epoch_does_not_skip_a_known_canonical_head_whose_history_is_missing(self):
        digest = self.observe()["digest"]
        route = {**self.route, "route_id": "rt-epoch-history"}
        completion = Path(self.temp.name) / "epoch-history" / route["route_id"]
        self._write_head(completion, route=route, node_id="execute", sequence=1, subject={
            "schema_version": SUBJECT.SCHEMA_VERSION, "digest": "1" * 64, "epoch": 4})
        # A source-unknown review head has no epoch, so it cannot lower the known maximum.
        self._write_head(completion, route=route, node_id="impl-review", sequence=1,
                         history=False)
        self.assertEqual(SUBJECT.source_epoch(route, self.node, digest, completion), 5)
        self.assertEqual(SUBJECT.source_epoch(route, self.node, "1" * 64, completion), 4)
        (completion / "execute.1.json").unlink()
        with self.assertRaises(SUBJECT.SourceSubjectError) as refused:
            SUBJECT.source_epoch(route, self.node, digest, completion)
        self.assertEqual(refused.exception.reason, "review-source-marker-history-missing")
        self.assertEqual((completion / "execute.json").exists(), True)

    def observe_test(self):
        return SUBJECT.observe(self.route, self.test_node, worktree=self.repo)

    def test_binding_is_attempt_exact_and_launch_rechecks_effective_source(self):
        jobs = Path(self.temp.name) / "state" / "jobs.log"
        jobs.parent.mkdir()
        route_file = Path(self.temp.name) / "route.json"
        route_file.write_text(json.dumps(self.route), encoding="utf-8")
        values = SUBJECT.seal_attempt(jobs, self.route, self.node, "att-review-A", worktree=self.repo)
        metadata = {"route_file": str(route_file), "route_id": self.route["route_id"],
                    "route_hash": self.route["route_hash"], "route_node": self.node["id"],
                    "completion_gate": self.node["completion_gate"], "unit": "qa/code-review",
                    "attempt_id": "att-review-A", **values}
        fields = ["now", "open", str(self.repo), str(self.repo), "review-A", ""]
        self.assertIsNotNone(SUBJECT.validate_attempt(jobs, fields, metadata, require_new=True))
        predecessor = SUBJECT.read_binding(jobs, metadata)
        replacement = SUBJECT.seal_attempt(jobs, self.route, self.node, "att-review-A-retry",
            worktree=self.repo, predecessor_attempt_id="att-review-A")
        self.assertEqual(replacement["review_subject_digest"], values["review_subject_digest"])
        changed = self.repo / "engine.py"
        changed.write_text("value = 'B'\n", encoding="utf-8")
        with self.assertRaises(SUBJECT.SourceSubjectError) as refused:
            SUBJECT.validate_attempt(jobs, fields, metadata)
        self.assertEqual(refused.exception.reason, "review-source-subject-changed-before-launch")
        self.assertEqual(predecessor["attempt_id"], "att-review-A")

    def test_unknown_legacy_attempt_is_not_inferred_from_launch_head(self):
        jobs = Path(self.temp.name) / "state" / "jobs.log"
        self.assertIsNone(SUBJECT.read_binding(jobs, {"attempt_id": "att-old"}, required=False))
        route_file = Path(self.temp.name) / "route.json"
        route_file.write_text(json.dumps(self.route), encoding="utf-8")
        fields = ["now", "open", str(self.repo), str(self.repo), "old", ""]
        metadata = {"route_file": str(route_file), "route_id": self.route["route_id"],
                    "route_hash": self.route["route_hash"], "route_node": self.node["id"],
                    "attempt_id": "att-old", "launch_head": self.git("rev-parse", "HEAD")}
        self.assertIsNone(SUBJECT.validate_attempt(jobs, fields, metadata))

    def test_gitlink_tracks_index_identity_and_dirty_submodule_content(self):
        sub = self.repo / "vendor"
        sub.mkdir()
        subprocess.run(["git", "init", "-q", str(sub)], check=True)
        subprocess.run(["git", "-C", str(sub), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(sub), "config", "user.name", "Fixture"], check=True)
        (sub / "library.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(sub), "add", "library.py"], check=True)
        subprocess.run(["git", "-C", str(sub), "commit", "-qm", "submodule"], check=True)
        oid = subprocess.check_output(["git", "-C", str(sub), "rev-parse", "HEAD"], text=True).strip()
        subprocess.run(["git", "-C", str(self.repo), "update-index", "--add", "--cacheinfo",
                        f"160000,{oid},vendor"], check=True)
        before = self.observe()["digest"]
        (sub / "library.py").write_text("x = 2\n", encoding="utf-8")
        self.assertNotEqual(before, self.observe()["digest"])

    def test_submodule_changes_during_later_parent_reads_are_reobserved(self):
        sub = self.repo / "vendor"
        sub.mkdir()
        subprocess.run(["git", "init", "-q", str(sub)], check=True)
        subprocess.run(["git", "-C", str(sub), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(sub), "config", "user.name", "Fixture"], check=True)
        (sub / "library.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(sub), "add", "library.py"], check=True)
        subprocess.run(["git", "-C", str(sub), "commit", "-qm", "submodule"], check=True)
        oid = subprocess.check_output(["git", "-C", str(sub), "rev-parse", "HEAD"], text=True).strip()
        self.git("update-index", "--add", "--cacheinfo", f"160000,{oid},vendor")
        original_reader = SUBJECT._read_regular
        for change in ("edit", "new-source"):
            with self.subTest(change=change):
                (self.repo / "z.py").write_text("parent = 'before'\n", encoding="utf-8")
                changed = False

                def change_submodule(path):
                    nonlocal changed
                    if path == self.repo / "z.py" and not changed:
                        changed = True
                        nested_path = sub / ("library.py" if change == "edit" else "late_source.py")
                        nested_path.write_text("x = 'after'\n", encoding="utf-8")
                        path.write_text("parent = 'after'\n", encoding="utf-8")
                    return original_reader(path)

                with mock.patch.object(SUBJECT, "_read_regular", side_effect=change_submodule):
                    current = self.observe()
                self.assertTrue(changed)
                self.assertEqual(current["digest"], self.observe()["digest"])

    def test_snapshot_race_stays_unknown_instead_of_publishing_a_mixed_subject(self):
        original_reader = SUBJECT._read_regular

        def race(path):
            result = original_reader(path)
            if path.name == "engine.py":
                path.write_text(path.read_text(encoding="utf-8") + "# race\n", encoding="utf-8")
            return result

        with mock.patch.object(SUBJECT, "_read_regular", side_effect=race):
            with self.assertRaises(SUBJECT.SourceSubjectError) as refused:
                self.observe()
        self.assertEqual(refused.exception.reason, "review-source-file-changed")


if __name__ == "__main__":
    unittest.main()
