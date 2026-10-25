#!/usr/bin/env python3

import importlib.util
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import worker_bootstrap as W
import dispatch_terminal_commit as T
import artifact_producer as AP
import dispatch_contract as DC
import dispatch_wrapper_common as C
import route_identity

ROOT = Path(__file__).resolve().parents[1]

_OPA_SPEC = importlib.util.spec_from_file_location(
    "owner_preview_for_bootstrap", Path(__file__).with_name("owner_preview_approval.test.py"))
OPA = importlib.util.module_from_spec(_OPA_SPEC)
_OPA_SPEC.loader.exec_module(OPA)


class WorkerBootstrapTest(unittest.TestCase):
    def test_stage_attempt_budget_prompt_and_legacy_rendering(self):
        from datetime import datetime, timezone
        args = SimpleNamespace(worker_type="stage", foreground_timeout=3600)
        rendered = W.runtime_progress_prompt(
            args, now=datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc))
        self.assertIn("3600 seconds (60 minutes)", rendered)
        self.assertIn("approximate deadline: 2026-10-22 06:00:00 UTC", rendered)
        self.assertIn("waiting with sleep, use this budget", rendered)
        self.assertIn("No per-tool heartbeat", W.runtime_progress_prompt())
        self.assertNotIn("execution budget", W.runtime_progress_prompt(
            SimpleNamespace(worker_type="owner", foreground_timeout=3600)))
        short = W.runtime_progress_prompt(
            SimpleNamespace(worker_type="stage", foreground_timeout=90),
            now=datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc))
        self.assertIn("90 seconds (1.5 minutes)", short)

    def test_stage_commit_policy_normalizes_sealed_and_legacy_inputs(self):
        args = SimpleNamespace(worker_type="stage", commit_expected=True,
                               subsession_id=None, stage_authority=1)
        self.assertTrue(W.stage_commit_enabled(args))
        with tempfile.TemporaryDirectory() as directory:
            route_path = Path(directory) / "route.json"
            args.route_file, args.route_node = str(route_path), "execute"
            route_path.write_text(json.dumps({"nodes": [{"id": "execute", "commit_expected": False}]}))
            self.assertFalse(W.stage_commit_enabled(args), "sealed policy beats the legacy value")
            route_path.write_text(json.dumps({"nodes": [{"id": "execute", "commit_expected": True}]}))
            args.commit_expected = False
            self.assertTrue(W.stage_commit_enabled(args))
            args.subsession_id = "slice-1"
            self.assertFalse(W.stage_commit_enabled(args))
            args.subsession_id, args.stage_authority = None, 0
            self.assertFalse(W.stage_commit_enabled(args))
            args.stage_authority, args.worker_type = 1, "owner"
            self.assertFalse(W.stage_commit_enabled(args))
            args.worker_type = "stage"
            route_path.write_text("broken JSON")
            self.assertFalse(W.stage_commit_enabled(args))

    def test_issued_cycle_context_supplies_missing_output_directory(self):
        env = {"AGENT_ARTIFACT_CYCLE_ID": "cyc-test", "AGENT_ARTIFACT_CYCLE_DIR": "/issued/cycle"}
        values = W.artifact_cycle_environment(env)
        self.assertEqual(values["AGENT_ARTIFACT_OUTPUT_DIR"], "/issued/cycle/artifacts")
        self.assertIn("/issued/cycle/artifacts", W.artifact_context_prompt(env))
        self.assertNotIn("AGENT_ARTIFACT_OUTPUT_DIR", env)
        self.assertEqual(W.artifact_context_prompt({}), "")

    def test_runtime_candidate_and_best_effort_preparation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            artifacts = project / ".agent_reports"
            artifacts.mkdir(parents=True)
            primary = W.runtime_dir_candidate(artifacts, {})
            self.assertEqual(primary, str(project / ".agent_runtime"))
            self.assertEqual(W.runtime_dir_candidate(artifacts, {"AGENT_RUNTIME_DIR": primary}), primary)
            explicit = base / "dedicated-runtime"
            self.assertEqual(W.runtime_dir_candidate(artifacts, {"AGENT_RUNTIME_DIR": str(explicit)}),
                             str(explicit))
            self.assertEqual(W.runtime_dir_candidate(artifacts, {"AGENT_RUNTIME_DIR": "relative"}), "")
            self.assertEqual(W.runtime_dir_candidate(artifacts, {"AGENT_RUNTIME_DIR": str(project / "cache")}), "")
            home_override = Path.home() / "unsafe-runtime-override"
            self.assertEqual(W.runtime_dir_candidate(artifacts, {"AGENT_RUNTIME_DIR": str(home_override)}), "")
            prepared = W.prepare_runtime_dir(str(explicit))
            self.assertEqual(prepared, str(explicit))
            self.assertEqual((explicit / ".gitignore").read_bytes(), b"*\n")
            self.assertFalse((explicit / "CACHEDIR.TAG").exists())
            conflict = base / "conflict"
            conflict.mkdir()
            (conflict / ".gitignore").write_text("keep-me\n")
            self.assertEqual(W.prepare_runtime_dir(str(conflict)), "")
            self.assertEqual((conflict / ".gitignore").read_text(), "keep-me\n")
            prompt = W.artifact_context_prompt({"AGENT_ARTIFACT_OUTPUT_DIR": "/out",
                                               "AGENT_RUNTIME_DIR": str(explicit)})
            self.assertIn(f"- runtime_dir: {explicit}", prompt)
            self.assertIn("checkpoints under runtime_dir", prompt)

    def test_non_git_runtime_uses_stable_state_and_exact_inheritance(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "original"
            source.mkdir()
            artifact_root = base / "external-artifacts"
            artifact_root.mkdir()
            home = base / "home"
            xdg = base / "xdg"
            harness = base / "harness-state"
            digest = hashlib.sha256(str(artifact_root.resolve()).encode("utf-8")).hexdigest()

            cases = (
                ({"HOME": str(home)}, home / ".local/state/hearting/dispatch"),
                ({"HOME": str(home), "XDG_STATE_HOME": str(xdg)}, xdg / "hearting/dispatch"),
                ({"HOME": str(home), "XDG_STATE_HOME": str(xdg),
                  "HARNESS_STATE_ROOT": str(harness)}, harness / "dispatch"),
            )
            for environ, stable_root in cases:
                with self.subTest(environ=tuple(environ)):
                    expected = stable_root / "project-runtimes" / digest
                    candidate = W.runtime_dir_candidate(
                        artifact_root, environ, repository_kind="non-git", source_root=source)
                    self.assertEqual(candidate, str(expected))
                    self.assertFalse(expected.exists(), "candidate selection must not create runtime state")
                    inherited = {**environ, "AGENT_RUNTIME_DIR": str(expected)}
                    self.assertEqual(W.runtime_dir_candidate(
                        artifact_root, inherited, repository_kind="non-git", source_root=source),
                        str(expected))

            self.assertEqual(W.runtime_dir_candidate(
                artifact_root, {"HOME": str(home)}, repository_kind="git", source_root=source),
                str(artifact_root.parent / ".agent_runtime"))
            self.assertEqual(W.runtime_dir_candidate(artifact_root, {}),
                             str(artifact_root.parent / ".agent_runtime"))

            unsafe_generated = (
                home / ".local/state/hearting/dispatch/project-runtimes" / ("f" * 64),
                home / ".local/state/hearting/dispatch/project-runtimes" / digest / "suffix",
                home / ".local/state/hearting/dispatch/project-runtimes" / (digest + "0"),
            )
            for value in unsafe_generated:
                with self.subTest(value=value):
                    self.assertEqual(W.runtime_dir_candidate(
                        artifact_root, {"HOME": str(home), "AGENT_RUNTIME_DIR": str(value)},
                        repository_kind="non-git", source_root=source), "")

            dedicated = base.parent / f"{base.name}-dedicated-runtime"
            self.assertEqual(W.runtime_dir_candidate(
                artifact_root, {"AGENT_RUNTIME_DIR": str(dedicated)},
                repository_kind="non-git", source_root=source), str(dedicated))
            self.assertEqual(W.runtime_dir_candidate(
                artifact_root, {"HOME": str(home), "XDG_STATE_HOME": "relative"},
                repository_kind="non-git", source_root=source), "")
            # A broken state source cannot displace a valid explicit override.
            self.assertEqual(W.runtime_dir_candidate(
                artifact_root, {"XDG_STATE_HOME": "relative", "AGENT_RUNTIME_DIR": str(dedicated)},
                repository_kind="non-git", source_root=source), str(dedicated))

    def test_non_git_generated_runtime_fails_closed_on_source_artifact_release_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            source.mkdir()
            artifact_root = base / "artifacts"
            artifact_root.mkdir()
            digest = hashlib.sha256(str(artifact_root.resolve()).encode("utf-8")).hexdigest()

            self.assertEqual(W.runtime_dir_candidate(
                artifact_root, {"HARNESS_STATE_ROOT": str(source)},
                repository_kind="non-git", source_root=source), "")
            self.assertEqual(W.runtime_dir_candidate(
                artifact_root, {"HARNESS_STATE_ROOT": str(artifact_root)},
                repository_kind="non-git", source_root=source), "")
            self.assertEqual(W.runtime_dir_candidate(
                artifact_root, {"HARNESS_STATE_ROOT": str(base / ".harness/releases/v1/state")},
                repository_kind="non-git", source_root=source), "")
            home_state = base / ".local/state/hearting/dispatch/project-runtimes"
            home_digest = hashlib.sha256(str(artifact_root.resolve()).encode("utf-8")).hexdigest()
            self.assertEqual(W.runtime_dir_candidate(
                artifact_root, {"HOME": str(base)}, repository_kind="non-git",
                source_root=base), str(home_state / home_digest))

            state = base / "state"
            stable = state / "dispatch"
            runtimes = stable / "project-runtimes"
            outside = base / "outside"
            outside.mkdir()
            stable.mkdir(parents=True)
            runtimes.symlink_to(outside, target_is_directory=True)
            self.assertEqual(W.runtime_dir_candidate(
                artifact_root, {"HARNESS_STATE_ROOT": str(state)},
                repository_kind="non-git", source_root=source), "")
            runtimes.unlink()
            runtimes.mkdir()
            (runtimes / digest).symlink_to(outside, target_is_directory=True)
            self.assertEqual(W.runtime_dir_candidate(
                artifact_root, {"HARNESS_STATE_ROOT": str(state)},
                repository_kind="non-git", source_root=source), "")
            self.assertEqual(list(outside.iterdir()), [])

    def test_non_git_generated_runtime_omits_namespace_parent_alias_before_preparation(self):
        for location, inherited in (("home", False), ("xdg", False), ("home", True)):
            for marker in (None, b"*\n"):
                with self.subTest(location=location, inherited=inherited, marker=marker), \
                        tempfile.TemporaryDirectory() as tmp:
                    base = Path(tmp)
                    source, artifacts, home = (base / name for name in ("source", "artifacts", "home"))
                    for path in (source, artifacts, home):
                        path.mkdir()
                    (source / "user.txt").write_bytes(b"preserve source\n")
                    (artifacts / "report.txt").write_bytes(b"preserve artifact\n")
                    values = {"HOME": str(home)}
                    if location == "xdg":
                        values["XDG_STATE_HOME"] = str(base / "xdg")
                    namespace = DC.stable_state_root(values) / "project-runtimes"
                    namespace.mkdir(parents=True)
                    (namespace / "other-project").mkdir()
                    (namespace / "other-project" / "checkpoint").write_bytes(b"preserve runtime\n")
                    if marker is not None:
                        (namespace / ".gitignore").write_bytes(marker)
                    digest = hashlib.sha256(str(artifacts.resolve()).encode("utf-8")).hexdigest()
                    alias = namespace / digest
                    alias.symlink_to(namespace, target_is_directory=True)
                    if inherited:
                        values["AGENT_RUNTIME_DIR"] = str(alias)

                    def snapshot():
                        return {str(path.relative_to(base)):
                                ("link", os.readlink(path)) if path.is_symlink() else
                                ("file", path.read_bytes(), path.stat().st_mtime_ns) if path.is_file() else
                                ("dir",) for path in base.rglob("*")}

                    before = snapshot()
                    candidate = W.runtime_dir_candidate(
                        artifacts, values, repository_kind="non-git", source_root=source)
                    record = {"campaign_id": "camp_test", "cycle_id": "cyc_test", "producer_id": "prod_test"}
                    route = {"source_commit": "unversioned", "cwd": str(source)}
                    with mock.patch.dict(os.environ, values, clear=True), \
                            mock.patch.object(AP, "cycle_dir", return_value=artifacts / "cycle"), \
                            mock.patch("artifact_workflow_groups.group_for_cycle", return_value=None):
                        produced = AP._env_for(artifacts, record, route)
                    prepared = C.prepare_runtime_root(
                        str(artifacts), values, repository_kind="non-git", source_root=str(source))
                    self.assertEqual({"candidate": candidate, "producer": produced.get("AGENT_RUNTIME_DIR", ""),
                                      "prepared": prepared, "snapshot_unchanged": snapshot() == before},
                                     {"candidate": "", "producer": "", "prepared": "", "snapshot_unchanged": True})

    def test_non_git_parent_alias_keeps_existing_outside_home_explicit_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            artifacts = source / ".agent_reports"
            source.mkdir()
            artifacts.mkdir()
            values = {"HOME": str(base / "home"), "XDG_STATE_HOME": str(base / "xdg")}
            namespace = DC.stable_state_root(values) / "project-runtimes"
            namespace.mkdir(parents=True)
            digest = hashlib.sha256(str(artifacts.resolve()).encode("utf-8")).hexdigest()
            alias = namespace / digest
            alias.symlink_to(namespace, target_is_directory=True)
            for explicit in (alias, namespace):
                with self.subTest(explicit=explicit):
                    # This is a generic dedicated override outside HOME, even
                    # when the generated candidate itself cannot be used.
                    self.assertEqual(W.runtime_dir_candidate(
                        artifacts, {**values, "AGENT_RUNTIME_DIR": str(explicit)},
                        repository_kind="non-git", source_root=source), str(namespace))
            self.assertFalse((namespace / ".gitignore").exists())

    def test_non_git_generated_runtime_keeps_safe_canonical_aliases_and_lazy_preparation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source, artifacts = base / "source", base / "artifacts"
            source.mkdir()
            artifacts.mkdir()
            artifact_alias = base / "artifact-alias"
            artifact_alias.symlink_to(artifacts, target_is_directory=True)
            values = {"HOME": str(base / "home")}
            stable = DC.stable_state_root(values)
            namespace = stable / "canonical-runtimes"
            namespace.mkdir(parents=True)
            (stable / "project-runtimes").symlink_to(namespace, target_is_directory=True)
            digest = hashlib.sha256(str(artifacts.resolve()).encode("utf-8")).hexdigest()
            expected = namespace / "dedicated-runtime"
            (namespace / digest).symlink_to(expected, target_is_directory=True)
            for root in (artifacts, artifact_alias):
                for override in ({}, {"AGENT_RUNTIME_DIR": str(namespace / digest)},
                                 {"AGENT_RUNTIME_DIR": str(expected)}):
                    with self.subTest(root=root, override=override):
                        self.assertEqual(W.runtime_dir_candidate(
                            root, {**values, **override}, repository_kind="non-git", source_root=source),
                            str(expected))
                        self.assertFalse(expected.exists(), "selection must remain pure")
            self.assertEqual(C.prepare_runtime_root(
                str(artifact_alias), values, repository_kind="non-git", source_root=str(source)), str(expected))
            marker = expected / ".gitignore"
            before = marker.read_bytes(), marker.stat().st_mtime_ns
            self.assertEqual(C.prepare_runtime_root(
                str(artifacts), values, repository_kind="non-git", source_root=str(source)), str(expected))
            self.assertEqual((marker.read_bytes(), marker.stat().st_mtime_ns), before)
            self.assertEqual(before[0], b"*\n")
            self.assertEqual(list(source.iterdir()), [])
            self.assertEqual(list(artifacts.iterdir()), [])

    def test_producer_passes_only_validated_unversioned_route_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "original"
            source.mkdir()
            artifact_root = base / "external-artifacts"
            artifact_root.mkdir()
            home = base / "home"
            record = {"campaign_id": "camp_test", "cycle_id": "cyc_test", "producer_id": "prod_test"}
            route = {"source_commit": "unversioned", "cwd": str(source)}
            values = {"HOME": str(home)}
            with mock.patch.dict(os.environ, values, clear=True), \
                    mock.patch.object(AP, "cycle_dir", return_value=base / "cycle"), \
                    mock.patch("artifact_workflow_groups.group_for_cycle", return_value=None):
                produced = AP._env_for(artifact_root, record, route)
                legacy = AP._env_for(artifact_root, record, {"source_commit": "abc123", "cwd": str(source)})
                route_free = AP._env_for(artifact_root, record)
            expected = W.runtime_dir_candidate(
                artifact_root, values, repository_kind="non-git", source_root=source)
            self.assertEqual(produced["AGENT_RUNTIME_DIR"], expected)
            self.assertEqual(W.runtime_dir_candidate(
                artifact_root, {**values, **produced}, repository_kind="non-git",
                source_root=source), expected)
            self.assertEqual(legacy["AGENT_RUNTIME_DIR"], str(artifact_root.parent / ".agent_runtime"))
            self.assertEqual(route_free["AGENT_RUNTIME_DIR"], str(artifact_root.parent / ".agent_runtime"))
            self.assertFalse(Path(expected).exists(), "producer environment issuance must not prepare runtime")

    def test_home_source_uses_only_exact_managed_default_and_preserves_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            home.mkdir()
            artifact_root = base / "artifacts"
            artifact_root.mkdir()
            source = home.resolve()
            cases = (
                ({"HOME": str(home)}, home / ".local/state/hearting/dispatch"),
                ({"HOME": str(home), "XDG_STATE_HOME": str(home / ".state")},
                 home / ".state/hearting/dispatch"),
            )
            for values, stable in cases:
                digest = hashlib.sha256(str(artifact_root.resolve()).encode("utf-8")).hexdigest()
                expected = stable / "project-runtimes" / digest
                candidate = W.runtime_dir_candidate(
                    artifact_root, values, repository_kind="non-git", source_root=source)
                self.assertEqual(candidate, str(expected))
                self.assertFalse(expected.exists())
                self.assertEqual(W.runtime_dir_candidate(
                    artifact_root, {**values, "AGENT_RUNTIME_DIR": candidate},
                    repository_kind="non-git", source_root=source), str(expected))
                record = {"campaign_id": "camp_test", "cycle_id": "cyc_test", "producer_id": "prod_test"}
                route = {"source_commit": "unversioned", "cwd": str(source)}
                with mock.patch.dict(os.environ, values, clear=True), \
                        mock.patch.object(AP, "cycle_dir", return_value=artifact_root / "cycle"), \
                        mock.patch("artifact_workflow_groups.group_for_cycle", return_value=None):
                    produced = AP._env_for(artifact_root, record, route)
                self.assertEqual(produced.get("AGENT_RUNTIME_DIR"), str(expected))
                self.assertEqual(C.prepare_runtime_root(
                    str(artifact_root), values, repository_kind="non-git", source_root=str(source)),
                    str(expected))
                self.assertEqual((expected / ".gitignore").read_bytes(), b"*\n")

            stable = home / ".local/state/hearting/dispatch"
            namespace = stable / "project-runtimes"
            outside = base / "outside-runtime"
            outside.mkdir()
            namespace.mkdir(parents=True, exist_ok=True)
            (namespace / "other-project").mkdir()
            self.assertEqual(W.runtime_dir_candidate(
                artifact_root, {"HOME": str(home), "AGENT_RUNTIME_DIR": str(namespace / "other-project")},
                repository_kind="non-git", source_root=source), "")
            alias = namespace / "alias"
            alias.symlink_to(outside, target_is_directory=True)
            self.assertEqual(W.runtime_dir_candidate(
                artifact_root, {"HOME": str(home), "AGENT_RUNTIME_DIR": str(alias)},
                repository_kind="non-git", source_root=source), "")
            self.assertEqual(W.runtime_dir_candidate(
                namespace, {"HOME": str(home)}, repository_kind="non-git",
                source_root=source), "")

    def test_nested_producer_environment_and_child_prompt_keep_default_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            artifact_root = project / ".agent_reports"
            artifact_root.mkdir(parents=True)
            runtime = project / ".agent_runtime"
            record = {"campaign_id": "camp_test", "cycle_id": "cyc_test", "producer_id": "prod_test"}
            with mock.patch.dict(os.environ, {"AGENT_RUNTIME_DIR": str(runtime)}, clear=False), \
                    mock.patch.object(AP, "cycle_dir", return_value=project / "child-cycle"), \
                    mock.patch("artifact_workflow_groups.group_for_cycle", return_value=None):
                child_env = AP._env_for(artifact_root, record)
            self.assertEqual(child_env["AGENT_RUNTIME_DIR"], str(runtime))
            self.assertEqual(W.runtime_dir_candidate(artifact_root, child_env), str(runtime))
            self.assertIn(f"- runtime_dir: {runtime}", W.artifact_context_prompt(child_env))

    def test_deterministic_fallback_types(self):
        self.assertEqual(W.resolve_worker_type(explicit=None, dispatch_depth=1), "owner")
        self.assertEqual(
            W.resolve_worker_type(explicit=None, dispatch_depth=2, route_node="test"),
            "stage",
        )
        self.assertEqual(
            W.resolve_worker_type(explicit=None, dispatch_depth=2, route_node="plan-review"),
            "review",
        )
        self.assertEqual(W.resolve_worker_type(explicit=None, dispatch_depth=2), "support")

    def test_worker_role_is_only_a_legacy_fallback(self):
        self.assertEqual(
            W.resolve_worker_type(
                explicit="support",
                dispatch_depth=2,
                worker_role="external-adversary",
                route_node="review",
            ),
            "support",
        )
        self.assertEqual(
            W.resolve_worker_type(explicit=None, dispatch_depth=2, worker_role="code-test"),
            "stage",
        )

    def test_explicit_and_profile_precedence(self):
        self.assertEqual(
            W.resolve_worker_type(
                explicit="review", dispatch_depth=1, profile_type="stage"
            ),
            "review",
        )
        self.assertEqual(
            W.resolve_worker_type(explicit=None, dispatch_depth=1, profile_type="support"),
            "support",
        )

    def test_explicit_frame_beats_depth_one_owner_fallback(self):
        # frame-universal (2026-09-10): a depth-1 frame node's explicit
        # worker_type must win over the dispatch_depth==1 "owner" fallback --
        # the same precedence already pinned for review at depth 1 above, now
        # pinned for frame too, so a depth-1 frame node is never silently
        # resolved away from "frame" (here to "owner"; elsewhere, if it ever
        # reached the unrelated topology-kind fallback, to "support").
        self.assertEqual(
            W.resolve_worker_type(explicit="frame", dispatch_depth=1),
            "frame",
        )
        self.assertEqual(
            W.resolve_worker_type(
                explicit="frame", dispatch_depth=1, worker_role="map-worker",
                route_node="frame",
            ),
            "frame",
        )

    def test_render_has_one_kernel_one_type_and_exact_handoff(self):
        rendered = W.render_worker_bootstrap(ROOT, "stage")
        self.assertEqual(rendered.count("# Portable Worker Kernel"), 1)
        self.assertEqual(rendered.count("# Worker Type:"), 1)
        self.assertIn(W.handoff_template(), rendered)
        self.assertIn("nothing after them", rendered)
        self.assertNotIn("```text\nartifact:", rendered)
        self.assertNotIn("# Worker Type: Owner", rendered)
        self.assertNotIn("# Worker Type: Review", rendered)

    def test_recurring_terminal_contract_is_the_kernel_section_only(self):
        contract = W.worker_terminal_contract(ROOT)
        self.assertIn(contract, W.render_worker_bootstrap(ROOT, "stage"))
        self.assertIn(W.handoff_template(), contract)
        self.assertIn("runtime_wait: registered-children", contract)
        self.assertIn("assigned completion", contract)
        self.assertIn("no judgment was reached", contract)
        self.assertNotIn("Portable Worker Kernel", contract)
        self.assertNotIn("memory lifecycle", contract)
        self.assertNotIn("worker-terminal-contract:", W.render_worker_bootstrap(ROOT, "stage"))

    def test_task_publication_scope_stays_with_the_approved_owner(self):
        owner = W.render_worker_bootstrap(ROOT, "owner")
        self.assertIn("When the sealed entry scope is `complete`", owner)
        self.assertIn("sealed entry scope is `complete`", owner)
        self.assertIn("ordinary non-force push", owner)
        self.assertIn("request wording is not an additional authorization test.", owner)
        self.assertIn("supervisor responsibilities", owner)
        for worker_type in ("stage", "review", "support", "frame"):
            text = W.render_worker_bootstrap(ROOT, worker_type)
            self.assertIn("Stage, review, support, and frame workers do not publish", text)
            self.assertNotIn("ordinary non-force push", text)

    def test_ordinary_worker_does_not_receive_chain_bookkeeping(self):
        for worker_type in W.WORKER_TYPES:
            text=W.render_worker_bootstrap(ROOT,worker_type)
            self.assertNotIn("after at most three",text)
            self.assertNotIn("Native helper support",text)
            self.assertNotIn("dispatch_subsession_handoff.py",text)
        self.assertIn("No per-tool heartbeat",W.runtime_progress_prompt())

    def test_render_appends_unit_persona_body(self):
        bare = W.render_worker_bootstrap(ROOT, "review")
        rendered = W.render_worker_bootstrap(ROOT, "review", unit="qa/code-review")
        self.assertTrue(rendered.startswith(bare.rstrip("\n")))
        self.assertIn("# Unit: qa/code-review", rendered)
        self.assertNotIn("worker_type: review\n", rendered)  # frontmatter stripped
        self.assertEqual(rendered.count("# Portable Worker Kernel"), 1)
        self.assertEqual(rendered.count("# Worker Type:"), 1)

    def test_reserved_and_absent_units_render_unchanged(self):
        bare = W.render_worker_bootstrap(ROOT, "owner")
        self.assertEqual(W.render_worker_bootstrap(ROOT, "owner", unit="_kernel/owner"), bare)
        self.assertEqual(W.render_worker_bootstrap(ROOT, "owner", unit=None), bare)
        self.assertIsNone(W.unit_persona_path(ROOT, "_kernel/resource"))
        self.assertIsNone(W.unit_persona_body(ROOT, None))

    def test_unknown_or_malformed_unit_fails_loud(self):
        with self.assertRaisesRegex(ValueError, "unknown unit"):
            W.unit_persona_path(ROOT, "qa/does-not-exist")
        with self.assertRaisesRegex(ValueError, "invalid unit ref"):
            W.unit_persona_path(ROOT, "../escape/path")
        with self.assertRaisesRegex(ValueError, "invalid unit ref"):
            W.render_worker_bootstrap(ROOT, "stage", unit="Bad/Unit")

    def test_unit_persona_path_points_into_catalog(self):
        path = W.unit_persona_path(ROOT, "editorial/report")
        self.assertEqual(path, ROOT / "roles" / "units" / "editorial" / "report.md")
        body = W.unit_persona_body(ROOT, "editorial/report")
        self.assertTrue(body.startswith("# Unit: editorial/report"))

    def test_profile_type_scalar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "profiles").mkdir()
            (root / "profiles" / "x.yaml").write_text(
                "name: x\nworker_type: review\n", encoding="utf-8"
            )
            self.assertEqual(W.profile_worker_type(root, "x"), "review")

    def test_assigned_stage_contract(self):
        self.assertEqual(
            W.assigned_contract(
                capability="autopilot-code",
                worker_type="stage",
                route_node="test",
                completion_gate="code-test",
                root=ROOT,
            ),
            "code-test",
        )
        self.assertEqual(
            W.assigned_contract(
                capability="autopilot-code",
                worker_type="owner",
                route_node=None,
                root=ROOT,
            ),
            "autopilot-code",
        )
        self.assertEqual(
            W.assigned_contract(
                capability="autopilot-design",
                worker_type="support",
                route_node="refs",
                completion_gate="design-refs",
                root=ROOT,
            ),
            "design-refs",
        )
        self.assertEqual(
            W.assigned_contract(
                capability="autopilot-spec",
                worker_type="support",
                route_node="research",
                completion_gate="spec-research",
                root=ROOT,
            ),
            "autopilot-spec",
        )

    def test_topology_kind_maps_only_to_bootstrap_type(self):
        self.assertEqual(W.worker_type_for_kind("capability-owner"), "owner")
        self.assertEqual(W.worker_type_for_kind("pipeline-stage"), "stage")
        self.assertEqual(W.worker_type_for_kind("review-worker"), "review")
        self.assertEqual(W.worker_type_for_kind("map-worker"), "support")
        with self.assertRaises(ValueError):
            W.worker_type_for_kind("resource-runner")

    def test_frame_contract_is_its_unit_even_with_legacy_owner_default(self):
        self.assertEqual(W.assigned_contract(capability="autopilot-code",worker_type="frame",
            route_node="frame",explicit="autopilot-code",unit="plan/frame",root=ROOT),"plan/frame")


class NodeScopeTest(unittest.TestCase):
    """Item 7: a stage worker's prompt must carry the route node's resolved
    (absolute) scope -- env binding first, the owner's producer binding
    second, and an explicit unbound statement when neither is available."""

    def _route(self, tmp: Path) -> Path:
        route_file = tmp / "route.json"
        route_file.write_text(json.dumps({
            "route_id": "rt-scope-test",
            "artifact_root": str(tmp / "artifact_root"),
            "nodes": [{
                "id": "research",
                "outputs": ["shards/research/notes.md"],
                "write_scope": ["spec/<component>/_internal/research/**"],
            }],
        }), encoding="utf-8")
        return route_file

    def test_sealed_scope_passes_the_exact_route_file_to_the_shared_resolver(self):
        node = {"id": "execute", "outputs": ["dev_logs/execute.md"],
                "write_scope": ["dev_logs/**"]}
        route = {"route_id": "rt-" + "a" * 16, "route_hash": "sha256:" + "a" * 64,
                 "artifact_root": "/tmp/artifacts", "nodes": [node]}
        context = SimpleNamespace(
            node=node, cycle_record=None, output_dir=None, source_worktree=Path("/worktree"),
            inherited_cycle_mismatch=False,
        )
        exact_file = "/tmp/noncanonical-route.json"
        with mock.patch("artifact_context.resolve_context", return_value=context) as resolver:
            W.resolve_node_scope(
                route, "execute", {}, route_file=exact_file,
                source_worktree="/linked-worktree", source_worktree_verified=True,
            )
        self.assertEqual(resolver.call_args.kwargs["route_file"], exact_file)
        self.assertEqual(resolver.call_args.kwargs["explicit"],
                         {"source_worktree": "/linked-worktree",
                          "source_worktree_verified": True})

    def test_stage_worker_prompt_carries_resolved_node_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            route_file = self._route(root)
            cycle_dir = root / "artifact_root" / "campaigns" / "camp1" / "cyc1"
            env = {"AGENT_ARTIFACT_CYCLE_ID": "cyc1", "AGENT_ARTIFACT_CYCLE_DIR": str(cycle_dir)}
            args = SimpleNamespace(worker_type="stage", route_file=str(route_file), route_node="research")
            prompt = W.assignment_prompt(args, "do the research", env)
            self.assertIn(
                str(cycle_dir / "artifacts" / "spec" / "<component>" / "_internal" / "research" / "**"),
                prompt,
            )
            self.assertIn(str(cycle_dir / "artifacts" / "shards" / "research" / "notes.md"), prompt)

    def test_completion_precedence_applies_without_route_or_done_when(self):
        task = ("Original request: O1 record the first result; O2 calculate all group totals; "
                "O3 inspect every file and report the findings. Execution scope: report.")
        for worker_type in ("owner", "stage", "review", "support"):
            with self.subTest(worker_type=worker_type):
                prompt = W.assignment_prompt(SimpleNamespace(worker_type=worker_type), task, {})
                self.assertTrue(prompt.startswith("Assignment:\n" + task + "\n\n"))
                self.assertIn("Completion precedence:", prompt)
                self.assertIn("within the approved endpoint and this node's assigned scope", prompt)
                self.assertIn("Stop at an approved report endpoint", prompt)
                self.assertIn("a bounded stage completes only its assigned portion", prompt)
                self.assertIn("report it with the existing outcome semantics", prompt)
                self.assertIn("presenting the whole request as complete", prompt)
                self.assertNotIn("Done when (sealed ids", prompt)

    def test_bounded_stage_keeps_its_scope_and_report_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            route_file = self._route(root)
            task = "Original request: O1, O2, O3. Execution scope: report."
            cycle_dir = root / "artifact_root" / "campaigns" / "camp1" / "cyc1"
            prompt = W.assignment_prompt(
                SimpleNamespace(worker_type="stage", route_file=str(route_file), route_node="research"),
                task,
                {"AGENT_ARTIFACT_CYCLE_ID": "cyc1", "AGENT_ARTIFACT_CYCLE_DIR": str(cycle_dir)},
            )
            self.assertTrue(prompt.startswith("Assignment:\n" + task + "\n\nCompletion precedence:"))
            self.assertIn(str(cycle_dir / "artifacts" / "shards" / "research" / "notes.md"), prompt)
            self.assertIn("approved report endpoint", prompt)
            self.assertNotIn("Done when (sealed ids", prompt)

    def test_child_prompt_uses_child_node_and_preserves_exact_review_cycle_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_root = root / "artifacts"
            artifact_root.mkdir()
            child_worktree = root / "child-worktree"
            child_worktree.mkdir()
            parent_worktree = root / "parent-worktree"
            parent_worktree.mkdir()
            route = {
                "capability": "autopilot-code",
                "effective_intensity": "standard",
                "artifact_root": str(artifact_root),
                "cwd": str(child_worktree),
                "slug": "child-review-binding",
                "slug_truncated": False,
                "nodes": [
                    {"id": "parent", "outputs": ["parent-report.md"],
                     "write_scope": ["parent/**"]},
                    {"id": "child", "outputs": ["dev_logs/child.md"],
                     "write_scope": ["dev_logs/**"]},
                ],
            }
            route["route_hash"] = route_identity.route_hash(route)
            route["route_id"] = route_identity.route_id_from_hash(route["route_hash"])
            route_file = root / "sealed-child-route.json"
            route_file.write_text(json.dumps(route), encoding="utf-8")

            jobs = root / "jobs.log"
            jobs.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}, clear=True):
                cycle = AP.begin(
                    artifact_root, route_file=route_file, capability="autopilot-code",
                    intensity="standard", require_cycle=True,
                )
            cycle_record = AP.read_cycle_record(artifact_root, cycle["cycle_id"])
            self.assertEqual(cycle_record["route_id"], route["route_id"])
            self.assertEqual(cycle_record["route_hash"], route["route_hash"])
            self.assertEqual(cycle_record["producer_id"], cycle["producer_id"])
            cycle_dir = AP.cycle_dir(
                artifact_root, cycle["campaign_id"], cycle["cycle_id"], cycle_record)
            review_output_path = cycle_dir / "artifacts" / "plans" / "registered-review.md"
            review_output_path.parent.mkdir(parents=True, exist_ok=True)
            review_output_path.write_text("registered review fixture\n", encoding="utf-8")
            review_binding = AP.prepare_review_output_binding(
                artifact_root, cycle_id=cycle["cycle_id"], producer_id=cycle["producer_id"],
                attempt_id="att-child-review", review_output=review_output_path,
                capability="autopilot-code", unit="qa/code-review", worktree=child_worktree,
            )
            review_output = review_binding["output_path"]
            review_metadata = (
                "attempt_schema_version=2,attempt_id=att-child-review,dispatch_depth=1,"
                "transport=headless,execution_surface=registered-headless,registered_worker=1,"
                "worker_type=review,unit=qa/code-review,capability=autopilot-code,"
                f"artifact_root={artifact_root},worktree={child_worktree},"
                f"review_cycle_id={cycle['cycle_id']},review_producer_id={cycle['producer_id']},"
                f"review_output_locator_b64={review_binding['locator_b64']},"
                f"review_output_digest={review_binding['digest']}"
            )
            jobs.write_text(
                f"2026-10-16T00:00:00Z\topen\t{child_worktree}\t{child_worktree}\t"
                f"review\t{review_metadata}\n",
                encoding="utf-8",
            )
            parent_env = {
                "AGENT_ARTIFACT_ROOT": str(artifact_root),
                "AGENT_ARTIFACT_CAMPAIGN_ID": cycle["campaign_id"],
                "AGENT_ARTIFACT_CYCLE_ID": cycle["cycle_id"],
                "AGENT_ARTIFACT_PRODUCER_ID": cycle["producer_id"],
                "AGENT_ARTIFACT_CYCLE_DIR": str(cycle_dir),
                "AGENT_ARTIFACT_OUTPUT_DIR": str(cycle_dir / "artifacts"),
                "AGENT_DISPATCH_JOBS": str(jobs),
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-review",
                "AGENT_DISPATCH_PARENT_ATTEMPT_ID": "att-grandparent",
                "AGENT_DISPATCH_WORKTREE": str(parent_worktree),
                "AGENT_DISPATCH_UNIT": "qa/code-review",
                "AGENT_REVIEW_OUTPUT": review_output,
                "AGENT_REVIEW_CYCLE_ID": cycle["cycle_id"],
                "AGENT_REVIEW_PRODUCER_ID": cycle["producer_id"],
                "AGENT_ROUTE_ID": route["route_id"],
                "AGENT_ROUTE_NODE": "parent",
            }
            original_env = dict(parent_env)
            args = SimpleNamespace(
                worker_type="stage", route_file=str(route_file), route_id=route["route_id"],
                artifact_root=str(artifact_root), route_node="child", worktree=str(child_worktree),
                parent_attempt_id="att-grandparent", attempt_id="att-child-review",
            )
            observed_bindings = []

            production_validator = DC.validate_review_output_binding

            def validate_review_binding(jobs_path, **kwargs):
                result = production_validator(jobs_path, **kwargs)
                observed_bindings.append((dict(kwargs), result))
                return result

            for worker_type in ("stage", "frame"):
                args.worker_type = worker_type
                with self.subTest(worker_type=worker_type), \
                     mock.patch.object(AP, "load_route", wraps=AP.load_route) as route_loader, \
                     mock.patch.object(DC, "validate_review_output_binding",
                                       side_effect=validate_review_binding) as review_validator:
                    prompt = W.assignment_prompt(args, "prepare the child", parent_env)

                self.assertIn(str(cycle_dir / "artifacts" / "dev_logs" / "child.md"), prompt)
                self.assertNotIn("parent-report.md", prompt)
                self.assertEqual(len(observed_bindings), 1)
                validated, actual_binding = observed_bindings.pop()
                self.assertEqual(validated["attempt_id"], "att-child-review")
                self.assertEqual(validated["output_path"], review_output)
                self.assertEqual(validated["cycle_id"], cycle["cycle_id"])
                self.assertEqual(validated["producer_id"], cycle["producer_id"])
                self.assertEqual(validated["capability"], "autopilot-code")
                self.assertEqual(validated["unit"], "qa/code-review")
                self.assertEqual(Path(validated["worktree"]), child_worktree)
                self.assertEqual(Path(validated["artifact_root"]), artifact_root)
                self.assertEqual(actual_binding["attempt_id"], "att-child-review")
                self.assertEqual(actual_binding["output_path"], review_output)
                self.assertEqual(actual_binding["cycle_id"], cycle["cycle_id"])
                self.assertEqual(actual_binding["producer_id"], cycle["producer_id"])
                self.assertEqual(actual_binding["worktree"], str(child_worktree))
                self.assertEqual(actual_binding["artifact_root"], str(artifact_root))
                self.assertEqual(actual_binding["digest"], review_binding["digest"])
                self.assertEqual(actual_binding["_registry_status"], "open")
                review_validator.assert_called_once()
                route_loader.assert_called()
                self.assertTrue(any(
                    call.args[0] == artifact_root
                    and call.args[1] == route_file
                    and call.kwargs.get("expected_identity") == route
                    for call in route_loader.call_args_list
                ), "scope resolution must use the production loader for the sealed route")
                self.assertEqual(parent_env, original_env)

            with self.assertRaises(DC.DispatchContractError) as wrong_attempt:
                production_validator(
                    jobs, attempt_id="att-unregistered-review", output_path=review_output,
                    cycle_id=cycle["cycle_id"], producer_id=cycle["producer_id"],
                    capability="autopilot-code", unit="qa/code-review",
                    worktree=child_worktree, artifact_root=artifact_root,
                )
            self.assertEqual(wrong_attempt.exception.reason, "review-attempt-not-unique")

            with self.assertRaises(DC.DispatchContractError) as wrong_output:
                production_validator(
                    jobs, attempt_id="att-child-review",
                    output_path=cycle_dir / "artifacts" / "plans" / "other-review.md",
                    cycle_id=cycle["cycle_id"], producer_id=cycle["producer_id"],
                    capability="autopilot-code", unit="qa/code-review",
                    worktree=child_worktree, artifact_root=artifact_root,
                )
            self.assertEqual(wrong_output.exception.reason, "review-binding-locator-mismatch")

    def test_code_one_shot_owns_approved_cycle_deliverables_and_source_is_not_an_artifact(self):
        node = {"id": "one-shot", "kind": "capability-owner", "unit": "_kernel/owner",
                "write_scope": ["source-scoped", "plans/<cycle>/**"]}
        route = {"capability": "autopilot-code", "effective_intensity": "quick",
                 "cwd": "/task/worktree", "nodes": [node]}
        before = json.dumps(route, sort_keys=True)
        env = {"AGENT_ARTIFACT_OUTPUT_DIR": "/primary/cycle/artifacts"}
        scope = W.resolve_node_scope(route, "one-shot", env)
        artifacts, source = W._resolved_write_scopes(scope)
        self.assertEqual(artifacts, ["/primary/cycle/artifacts/**"])
        self.assertEqual(source, ["/task/worktree/**"])
        self.assertIn("approved task", W.node_scope_prompt(scope))
        self.assertEqual(json.dumps(route, sort_keys=True), before)
        for changed in ({"capability": "autopilot-lab"}, {"effective_intensity": "standard"}):
            narrow = W.resolve_node_scope({**route, **changed}, "one-shot", env)
            self.assertEqual(W._resolved_write_scopes(narrow)[0], ["/primary/cycle/artifacts/plans/<cycle>/**"])
        unbound = W.resolve_node_scope(route, "one-shot", {})
        self.assertIn("do not write durable artifacts", W.node_scope_prompt(unbound))

    def test_scope_resolves_from_producer_binding_without_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            route_file = self._route(root)
            cycle_dir = root / "artifact_root" / "campaigns" / "camp1" / "cyc1"
            args = SimpleNamespace(
                worker_type="stage", route_file=str(route_file), route_node="research",
                parent_attempt_id="att-owner",
            )
            binding = T.ProducerBindingResult(
                "loaded", root / "binding.json",
                {"campaign_id": "camp1", "cycle_id": "cyc1"}, "digest", True,
            )
            with mock.patch.object(T, "load_producer_binding", return_value=binding), \
                 mock.patch.object(AP, "cycle_dir", return_value=cycle_dir):
                prompt = W.assignment_prompt(args, "do the research", {})
            self.assertIn(
                str(cycle_dir / "artifacts" / "spec" / "<component>" / "_internal" / "research" / "**"),
                prompt,
            )

    def test_scope_unbound_is_stated_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            route_file = self._route(root)
            args = SimpleNamespace(worker_type="stage", route_file=str(route_file), route_node="research")
            prompt = W.assignment_prompt(args, "do the research", {})
            self.assertIn("no open cycle is bound", prompt)
            self.assertNotIn(str(root / "artifact_root"), prompt)

    def test_inputs_compose_took_from_an_earlier_cycle_are_named_by_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            route_file = root / "route.json"
            node = {"id": "execute", "outputs": ["dev_logs/execute.md"], "write_scope": ["dev_logs/**"],
                    "inputs": ["plan.md", "task"],
                    "input_sources": {"plan.md": {"cycle_id": "cyc_1", "path": "campaigns/c/cyc_1/artifacts/plan.md"}}}
            route_file.write_text(json.dumps({"route_id": "rt-src", "cwd": str(root), "artifact_root": str(root / "reports"),
                                              "nodes": [node, {"id": "test", "outputs": [], "write_scope": []}]}),
                                  encoding="utf-8")
            args = SimpleNamespace(worker_type="stage", route_file=str(route_file), route_node="execute")
            prompt = W.assignment_prompt(args, "edit module", {})
            self.assertIn("plan.md=" + str(root / "reports/campaigns/c/cyc_1/artifacts/plan.md"), prompt)
            args.route_node = "test"                                     # a node with no earlier-cycle input
            self.assertNotIn("earlier cycle", W.assignment_prompt(args, "run tests", {}))

    def test_source_and_test_scope_stay_in_worktree_while_reports_use_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "linked-worktree"
            (worktree / "tests").mkdir(parents=True)
            (worktree / "module.py").write_text("before\n", encoding="utf-8")
            route_file = root / "route.json"
            route_file.write_text(json.dumps({
                "route_id": "rt-source-scope", "cwd": str(worktree),
                "artifact_root": str(root / "artifacts"),
                "nodes": [{"id": "execute", "outputs": ["dev_logs/execute.md"],
                           "write_scope": ["source/**", "source-alternative/**", "tests/**",
                                           str(root / "explicit-artifact.md")]}],
            }), encoding="utf-8")
            cycle_output = root / "canonical" / "artifacts"
            args = SimpleNamespace(worker_type="stage", route_file=str(route_file), route_node="execute")
            prompt = W.assignment_prompt(args, "edit module", {
                "AGENT_ARTIFACT_OUTPUT_DIR": str(cycle_output),
            })
            self.assertIn(str(worktree), prompt)
            self.assertIn(str(worktree / "tests"), prompt)
            self.assertIn(str(cycle_output / "dev_logs" / "execute.md"), prompt)
            self.assertIn(str(root / "explicit-artifact.md"), prompt)
            self.assertNotIn(str(cycle_output / "source"), prompt)
            (worktree / "module.py").write_text("after\n", encoding="utf-8")
            (cycle_output / "dev_logs").mkdir(parents=True)
            (cycle_output / "dev_logs" / "execute.md").write_text("handoff\n", encoding="utf-8")
            self.assertEqual((worktree / "module.py").read_text(), "after\n")
            self.assertTrue((cycle_output / "dev_logs" / "execute.md").is_file())


class OwnerGatePromptTest(unittest.TestCase):
    """The owner's assignment names the approval gate on a node it executes itself.

    Text only: the gate is still enforced by the runtime. The one shared renderer serves all
    three adapters, so one rendering test plus a check that each adapter calls it covers them.
    """

    def _route(self, tmp: Path, *, bound=True, owner_node=True) -> Path:
        node = {"id": "transaction", "kind": "capability-owner", "unit": "_kernel/owner",
                "dispatch_depth": 1, "terminal": True} if owner_node else {
                "id": "transaction", "kind": "review-worker", "unit": "editorial/review", "dispatch_depth": 2}
        route_file = tmp / "rt-gate.json"
        route_file.write_text(json.dumps({
            "route_id": "rt-gate-prompt", "artifact_root": str(tmp / "artifact_root"),
            "nodes": [{"id": "review", "dispatch_depth": 2, "unit": "editorial/review",
                       "continuation": {"kind": "human-gate", "gate": "preview-disposition"}}, node],
            "human_gate_bindings": ([{"gate": "preview-disposition", "node": "transaction", "position": "entry"}]
                                    if bound else []),
        }), encoding="utf-8")
        return route_file

    def _owner(self, route_file, *, via_binding=False):
        binding = SimpleNamespace(route_file=str(route_file), route_id="rt-gate-prompt")
        return SimpleNamespace(worker_type="owner", route_file=None if via_binding else str(route_file),
                               route_node=None, owner_route_binding=binding if via_binding else None,
                               jobs="/tmp/jobs.log")

    def test_the_owner_is_told_to_raise_the_gate_before_applying(self):
        with tempfile.TemporaryDirectory() as tmp:
            route_file = self._route(Path(tmp))
            for via_binding in (False, True):
                with self.subTest(via_binding=via_binding):
                    prompt = W.assignment_prompt(self._owner(route_file, via_binding=via_binding), "Refine it", {})
                    self.assertIn("preview-disposition", prompt)
                    self.assertIn("transaction", prompt)
                    self.assertIn("workflow-supervisor.py gate --route", prompt)
                    self.assertIn("--block", prompt)
                    self.assertIn(str(route_file), prompt)
                    self.assertIn("do not apply", prompt)
                    self.assertIn("BLOCKED", prompt)

    def test_a_route_without_the_binding_or_for_a_child_node_adds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = W.assignment_prompt(self._owner(self._route(Path(tmp), bound=False)), "Refine it", {})
            self.assertNotIn("preview-disposition", plain)
            self.assertNotIn("--block", plain)
            child_node = W.assignment_prompt(self._owner(self._route(Path(tmp), owner_node=False)), "Refine it", {})
            self.assertNotIn("--block", child_node)
            stage = SimpleNamespace(worker_type="stage", route_file=str(self._route(Path(tmp))), route_node="review")
            self.assertNotIn("--block", W.assignment_prompt(stage, "Review it", {}))

    def test_all_three_adapters_render_the_assignment_through_the_shared_function(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                source = (ROOT / "adapters" / harness / "bin" / "dispatch-headless.py").read_text(encoding="utf-8")
                self.assertIn("assignment_prompt(args, task, os.environ)", source)


class WorkerEndingPromptTest(unittest.TestCase):
    TERMINAL = (
        "For the terminal final response, end with the kernel's exact three-line "
        "handoff as the entire message — no summary sentence before it, nothing after it.\n"
    )
    WAIT = (
        "End an intermediate child-registration or resource-wait turn only with the "
        "standalone line `runtime_wait: registered-children`, without a three-line handoff. "
    )

    @classmethod
    def setUpClass(cls):
        cls.adapters = {}
        for harness in ("claude", "codex", "opencode"):
            path = ROOT / "adapters" / harness / "bin" / "dispatch-headless.py"
            spec = importlib.util.spec_from_file_location(f"ending_{harness}_dispatch", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            cls.adapters[harness] = module

    def render(self, harness, *, intensity="standard", worker_type="owner", supervised=True):
        module = self.adapters[harness]
        args = module.parser().parse_args([
            "--worktree", str(ROOT), "--slug", "ending-fixture",
            "--capability", "autopilot-lab", "--capability-mode", "eval",
            "--qa", "standard", "--intensity", intensity,
            "--worker-type", worker_type,
            "--dispatch-depth", "1" if worker_type == "owner" else "2",
            "--prompt-text", "Run the assigned evaluation.",
        ])
        args.attempt_id = "att-ending-fixture"
        args.artifact_root = "/fixture/.agent_reports"
        args.resolved_completion_delivery = (
            "app-server-supervised" if harness == "codex" else "session-resume-supervised"
        ) if supervised else "poll-fallback"
        args.owner_route_binding = (
            SimpleNamespace(route_file=None)
            if supervised and worker_type == "owner" and intensity != "quick" else None
        )
        with mock.patch.dict(os.environ, {"AGENT_HOME": str(ROOT)}, clear=True):
            renderer = module.prompt if harness == "opencode" else module.dispatch_prompt
            return renderer(args)[0]

    def test_all_supervised_owners_receive_the_same_wait_and_terminal_rules(self):
        for intensity in ("quick", "standard", "strong", "thorough", "adversarial"):
            for harness in self.adapters:
                with self.subTest(harness=harness, intensity=intensity):
                    prompt = self.render(harness, intensity=intensity)
                    self.assertTrue(prompt.endswith(self.WAIT + self.TERMINAL))
                    self.assertEqual(prompt.count(self.WAIT), 1)
                    self.assertIn("The three-line handoff applies only to the terminal final response", prompt)

    def test_polling_owners_keep_the_terminal_ending(self):
        for harness in self.adapters:
            with self.subTest(harness=harness):
                prompt = self.render(harness, supervised=False)
                self.assertTrue(prompt.endswith(self.TERMINAL))
                self.assertNotIn(self.WAIT, prompt)
                # The full bootstrap must keep the wait directive conditional,
                # even when the adapter's trailing ending is correctly gated.
                compact = " ".join(prompt.split())
                self.assertIn(
                    "For a supervised owner, end a child-registration or resource-wait turn with only",
                    compact,
                )
                self.assertIn("polling fallback waits synchronously in the current turn.", compact)
                if harness == "claude":
                    self.assertIn("poll synchronously with utilities/dispatch-wait.sh in the current turn", compact)
                elif harness == "codex":
                    self.assertIn("after a child is registered, run only utilities/dispatch-wait.sh", compact)

    def test_stage_and_review_workers_keep_the_terminal_ending(self):
        for worker_type in ("stage", "review"):
            for harness in self.adapters:
                with self.subTest(harness=harness, worker_type=worker_type):
                    prompt = self.render(harness, worker_type=worker_type)
                    self.assertTrue(prompt.endswith(self.TERMINAL))
                    self.assertNotIn(self.WAIT, prompt)


class OwnerInlineMarkerPromptTest(OPA.OwnerRefineBase):
    """The owner is told to finish its stage first and how to publish a stage it ran itself (D2, round 1).

    The OpenCode r3 owner ran `review` inline because nothing told it that the inline run still
    publishes a marker, and then could not settle. Text only: settlement still refuses an unmarked stage.
    """

    def prompt(self, **kw):
        args = SimpleNamespace(worker_type="owner", route_file=str(self.path), jobs=str(self.jobs),
                               attempt_id=self.owner, **kw)
        return W.assignment_prompt(args, "Refine the README", {})

    def test_the_refine_owner_is_told_review_first_and_the_named_command_publishes_the_marker(self):
        self.build("opencode", review=False, close=False, parent="poll-fallback")
        text = self.prompt()
        self.assertIn("first finish stage review", text)
        self.assertLess(text.index("first finish stage review"), text.index("--gate preview-disposition --block"))
        command = re.search(r"`([^`]*capability-route\.py complete[^`]*)`", text).group(1)
        for token in ("--inline", "--node", "--reason"):
            self.assertIn(token, command)
        self.assertNotIn("--jobs", command)
        # the rendered command, run as written, publishes the marker settlement asks for
        argv = [a.replace("<node>", "review").replace("<one line>", "review done") for a in shlex.split(command)]
        env = {**os.environ, "AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_DISPATCH_JOBS": str(self.jobs),
               "AGENT_DISPATCH_REGISTERED_WORKER": "1", "AGENT_DISPATCH_ATTEMPT_ID": self.owner}
        done = subprocess.run([sys.executable, *argv[1:]], text=True, capture_output=True, env=env, cwd=str(OPA.R.ROOT))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        node = next(n for n in self.route["nodes"] if n["id"] == "transaction")
        with self.env():
            self.assertEqual(OPA.R.owner_terminal_prerequisites(self.route, node, self.jobs), {})

    def test_the_text_is_one_rendering_for_every_harness(self):
        self.build("claude", review=False, close=False)
        texts = [self.prompt(adapter=harness) for harness in ("claude", "codex", "opencode")]
        self.assertEqual(texts[0], texts[1])
        self.assertEqual(texts[1], texts[2])
        self.assertIn("first finish stage review", texts[0])

    def test_no_text_without_a_route_or_for_a_stage_worker(self):
        prompt = W.assignment_prompt(SimpleNamespace(worker_type="owner"), "T", {})
        self.assertIn("Assignment:\nT\n\nCompletion precedence:", prompt)
        self.assertEqual(W.owner_inline_marker_prompt(SimpleNamespace(worker_type="stage", route_file="x")), "")

    def test_a_code_owner_gets_the_marker_line_and_no_gate_line(self):
        os.environ["AGENT_HOME"] = str(OPA.R.ROOT)
        self.activate()
        route = OPA.R.compose_route(
            capability="autopilot-code", capability_mode="dev", shape="staged", graph="plan,execute,test",
            slug="code-owner", cwd=OPA.R.ROOT, artifact_root=self.root, intensity="standard",
            dispatch_evidence={"tuples": [OPA.nested("claude", "codex")]}, unassigned=True,
            work_request={"text": "Fix it", "owner_harness": "claude"})
        path = Path(OPA.L.admit_runtime_route(self.root, route).route_file)
        text = W.assignment_prompt(
            SimpleNamespace(worker_type="owner", route_file=str(path), jobs=str(self.jobs), attempt_id="att-code"),
            "Fix it", {})
        self.assertIn("A declared stage (plan, execute, test)", text)
        self.assertNotIn("Human gate", text)


class ReportContractTest(unittest.TestCase):
    def _contract(self, capability, gate, node, root=ROOT):
        return W.assigned_contract(capability=capability, worker_type="stage", route_node=node,
                                   completion_gate=gate, root=root)

    def test_report_gates_resolve_to_their_own_contract(self):
        self.assertEqual(self._contract("autopilot-lab", "lab-report", "report"), "autopilot-lab")
        self.assertEqual(self._contract("autopilot-research", "research-report", "report"), "autopilot-research")
        self.assertEqual(self._contract("audit", "audit-report", "report"), "audit")
        self.assertEqual(self._contract("autopilot-code", "code-report", "report"), "code-report")
        self.assertEqual(self._contract("autopilot-design", "design-handoff", "handoff"), "design-handoff")
        self.assertEqual(self._contract("autopilot-draft", "draft-finalize", "finalize"), "autopilot-draft")

    def test_every_recipe_node_matches_gate_contract_table(self):
        data = json.loads((ROOT / "capabilities" / "topologies.json").read_text(encoding="utf-8"))
        table = data["completion_gate_contracts"]
        worker_types = {"pipeline-stage": "stage", "review-worker": "review", "map-worker": "support"}
        checked = 0
        for recipe in data["recipes"]:
            for node in recipe["standard_plus"]["nodes"]:
                gate = node.get("completion_gate")
                if node.get("kind") not in worker_types or gate not in table:
                    continue
                entry = table[gate]
                expected = Path(entry["contract"]).stem if entry["kind"] == "capability-doc" else recipe["capability"]
                got = W.assigned_contract(capability=recipe["capability"], worker_type=worker_types[node["kind"]],
                                          route_node=node["id"], completion_gate=gate, root=ROOT)
                self.assertEqual(got, expected, f"{recipe['capability']}/{node['id']}/{gate}")
                checked += 1
        self.assertGreater(checked, 30)

    def test_node_id_fallback_is_autopilot_code_only(self):
        for node in ("report", "plan", "execute", "test"):
            self.assertEqual(W.assigned_contract(capability="autopilot-lab", worker_type="stage",
                                                 route_node=node, root=ROOT), "autopilot-lab")
        self.assertEqual(W.assigned_contract(capability="autopilot-code", worker_type="stage",
                                             route_node="report", root=ROOT), "code-report")

    def test_unreadable_gate_table_falls_back_quietly(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._contract("autopilot-lab", "lab-report", "report", root=Path(tmp)), "autopilot-lab")

    def test_report_reference_line_names_existing_lab_docs(self):
        lab = SimpleNamespace(worker_type="stage", completion_gate="lab-report", assigned_contract="autopilot-lab")
        for harness in ("claude", "codex", "opencode"):
            prompt = W.contract_read_prompt(lab, harness)
            self.assertIn(str(ROOT / "capabilities/autopilot-lab/references/eval-procedure.md"), prompt)
            self.assertIn(str(ROOT / "capabilities/autopilot-lab/references/data-contract.md"), prompt)
        code = SimpleNamespace(worker_type="stage", completion_gate="code-report", assigned_contract="code-report")
        self.assertNotIn("report format is specified in", W.contract_read_prompt(code, "claude"))
        review = SimpleNamespace(worker_type="review", completion_gate="lab-report", assigned_contract="autopilot-lab")
        self.assertNotIn("report format is specified in", W.contract_read_prompt(review, "claude"))


def _write(path: Path, size: int = 2048, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((text * size)[:size], encoding="utf-8")
    return path


def _cycle(root: Path, campaign: str, cycle: str, *, sealed: bool = True, files=()) -> Path:
    cycle_dir = root / "campaigns" / campaign / cycle
    (cycle_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    if sealed:
        (cycle_dir / "manifest.json").write_text("{}", encoding="utf-8")
    for rel, size in files:
        _write(cycle_dir / "artifacts" / rel, size)
    return cycle_dir


class DeliverableExemplarTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.root = self.tmp / ".agent_reports"
        self.root.mkdir()

    def _env(self, cycle: Path) -> dict:
        return {"AGENT_ARTIFACT_CYCLE_DIR": str(cycle)}

    def _current(self, campaign="camp-now", cycle="2026-09-30_now"):
        return _cycle(self.root, campaign, cycle, sealed=False, files=[("report/REPORT.md", 4096)])

    def test_same_campaign_sealed_report_is_preferred(self):
        current = self._current()
        same = _cycle(self.root, "camp-now", "2026-01-01_old", files=[("report/REPORT.md", 2048)])
        _cycle(self.root, "camp-other", "2026-09-01_newer", files=[("report/REPORT.md", 2048)])
        found = W._deliverable_exemplars("lab-report", self.root, self._env(current))
        self.assertEqual(found, [same / "artifacts/report/REPORT.md"])

    def test_design_handoff_prefers_flat_path_and_keeps_literal_legacy_path(self):
        current = self._current()
        previous = _cycle(self.root, "camp-now", "2026-01-01_design", files=[
            ("designs/05_handoff/handoff.md", 2048),
            ("designs/05_handoff/preview.html", 2048),
            ("designs/<cycle>/05_handoff/old.md", 2048),
            ("designs/<cycle>/05_handoff/old.html", 2048),
        ])
        found = W._deliverable_exemplars("design-handoff", self.root, self._env(current))
        self.assertEqual(found, [previous / "artifacts/designs/05_handoff/handoff.md",
                                 previous / "artifacts/designs/05_handoff/preview.html"])
        (previous / "artifacts/designs/05_handoff/handoff.md").unlink()
        (previous / "artifacts/designs/05_handoff/preview.html").unlink()
        found = W._deliverable_exemplars("design-handoff", self.root, self._env(current))
        self.assertEqual(found, [previous / "artifacts/designs/<cycle>/05_handoff/old.md",
                                 previous / "artifacts/designs/<cycle>/05_handoff/old.html"])

    def test_falls_back_to_project_when_campaign_has_none(self):
        current = self._current()
        _cycle(self.root, "camp-a", "2026-01-01_a", files=[("report/REPORT.md", 2048)])
        newer = _cycle(self.root, "camp-b", "2026-05-01_b", files=[("report/REPORT.md", 2048),
                                                                    ("report/index.html", 3000)])
        found = W._deliverable_exemplars("lab-report", self.root, self._env(current))
        self.assertEqual(found, [newer / "artifacts/report/REPORT.md", newer / "artifacts/report/index.html"])

    def test_current_and_unsealed_cycles_are_excluded(self):
        current = self._current()
        _cycle(self.root, "camp-now", "2026-09-29_open", sealed=False, files=[("report/REPORT.md", 2048)])
        self.assertEqual(W._deliverable_exemplars("lab-report", self.root, self._env(current)), [])
        (current / "manifest.json").write_text("{}", encoding="utf-8")
        self.assertEqual(W._deliverable_exemplars("lab-report", self.root, self._env(current)), [])

    def test_legacy_claude_reports_sibling_is_scanned(self):
        current = self._current()
        legacy = self.tmp / ".claude_reports"
        old = _cycle(legacy, "camp-old", "2025-01-01_old", files=[("experiments/e1/REPORT.md", 2048)])
        found = W._deliverable_exemplars("lab-report", self.root, self._env(current))
        self.assertEqual(found, [old / "artifacts/experiments/e1/REPORT.md"])

    def test_stub_and_internal_files_are_ignored(self):
        current = self._current()
        _cycle(self.root, "camp-a", "2026-02-01_stub", files=[("report/REPORT.md", 499)])
        _cycle(self.root, "camp-a", "2026-01-01_int", files=[("experiments/_internal/REPORT.md", 4096),
                                                             ("experiments/e1/REPORT.md", 5000)])
        found = W._deliverable_exemplars("lab-report", self.root, self._env(current))
        self.assertEqual([p.name for p in found], ["REPORT.md"])
        self.assertNotIn("_internal", str(found[0]))
        self.assertIn("2026-01-01_int", str(found[0]))

    def test_unknown_gate_has_no_exemplar(self):
        current = self._current()
        _cycle(self.root, "camp-a", "2026-01-01_a", files=[("report/REPORT.md", 2048)])
        self.assertEqual(W._deliverable_exemplars("no-such-gate", self.root, self._env(current)), [])
        self.assertEqual(W._deliverable_exemplars("lab-report", None, {}), [])

    def test_scan_respects_deadline(self):
        current = self._current()
        _cycle(self.root, "camp-a", "2026-01-01_a", files=[("report/REPORT.md", 2048)])
        self.assertEqual(W._deliverable_exemplars("lab-report", self.root, self._env(current), deadline_s=0), [])

    def test_scan_stops_after_max_cycles(self):
        current = self._current()
        for n in range(5):
            _cycle(self.root, "camp-a", f"2026-02-0{n}_x", sealed=False)
        _cycle(self.root, "camp-a", "2026-01-01_hit", files=[("report/REPORT.md", 2048)])
        self.assertEqual(W._deliverable_exemplars("lab-report", self.root, self._env(current), max_cycles=3), [])
        self.assertEqual(len(W._deliverable_exemplars("lab-report", self.root, self._env(current))), 1)


def _fake_unit_root(tmp: Path, frontmatter_extra: str, unit: str = "editorial/report") -> Path:
    path = tmp / "roles" / "units" / f"{unit}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nunit: {unit}\nrole: deep editor\naliases: {{}}\n{frontmatter_extra}---\n\n# Unit: {unit}\n",
                    encoding="utf-8")
    return tmp


def _fake_mem(tmp: Path, body: str) -> None:
    path = tmp / "tools" / "memory" / "mem.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class UnitBootstrapDeclarationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_declaration_is_parsed_from_frontmatter(self):
        want = {"memory": ("report-format",), "exemplar": "deliverable", "gates": ("x-report",)}
        for block in (
            "bootstrap:\n  memory: [report-format]\n  exemplar: deliverable\n  gates: [x-report]\n",
            "bootstrap:   # note\n  memory:   [ report-format ]  # topics\n\texemplar:\tdeliverable\n  gates: [x-report]\n",
        ):
            root = _fake_unit_root(self.tmp / "a", block)
            self.assertEqual(W.unit_bootstrap_declaration(root, "editorial/report"), want)

    def test_undeclared_reserved_missing_and_broken_are_empty(self):
        root = _fake_unit_root(self.tmp, "")
        self.assertEqual(W.unit_bootstrap_declaration(root, "editorial/report"), {})
        self.assertEqual(W.unit_bootstrap_declaration(root, "_kernel/owner"), {})
        self.assertEqual(W.unit_bootstrap_declaration(root, None), {})
        self.assertEqual(W.unit_bootstrap_declaration(root, "qa/none"), {})
        self.assertEqual(W.unit_bootstrap_declaration(root, "../bad"), {})
        broken = _fake_unit_root(self.tmp / "b", "bootstrap:\n  memory: [\n  ???\n")
        self.assertIsInstance(W.unit_bootstrap_declaration(broken, "editorial/report"), dict)

    def _args(self, **kw):
        base = dict(worker_type="stage", unit="editorial/report", completion_gate="lab-report",
                    artifact_root=None, worktree=str(self.tmp))
        base.update(kw)
        return SimpleNamespace(**base)

    def test_undeclared_unit_gets_nothing_and_spawns_nothing(self):
        root = _fake_unit_root(self.tmp, "")
        with mock.patch("subprocess.run") as run, mock.patch.object(W, "_deliverable_exemplars") as scan:
            self.assertEqual(W.unit_bootstrap_prompt(self._args(), "task", {}, root=root), "")
            self.assertEqual(W.unit_bootstrap_prompt(self._args(unit="_kernel/owner", worker_type="owner"), "t", {}, root=root), "")
            self.assertEqual(W.unit_bootstrap_prompt(self._args(unit=None), "t", {}, root=root), "")
            self.assertEqual(W.unit_bootstrap_prompt(self._args(worker_type="frame", unit="plan/frame"), "t", {}, root=root), "")
        run.assert_not_called()
        scan.assert_not_called()

    def test_owner_and_frame_ignore_a_declaration(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  memory: [report-format]\n")
        with mock.patch("subprocess.run") as run:
            for kind in ("owner", "frame"):
                self.assertEqual(W.unit_bootstrap_prompt(self._args(worker_type=kind), "t", {}, root=root), "")
        run.assert_not_called()

    def test_gate_limited_unit_skips_other_gates(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  memory: [report-format]\n  gates: [research-report]\n",
                               unit="research/research-survey")
        _fake_mem(root, "print('# Saved preferences\\n## [durable/feedback] id (global)\\nbody')\n")
        args = self._args(unit="research/research-survey", completion_gate="research-retrieval")
        with mock.patch("subprocess.run") as run:
            self.assertEqual(W.unit_bootstrap_prompt(args, "t", {}, root=root), "")
            run.assert_not_called()
        args = self._args(unit="research/research-survey", completion_gate="research-report")
        section = W.unit_bootstrap_prompt(args, "t", {}, root=root)
        self.assertIn("Unit bootstrap material", section)
        self.assertIn("body", section)

    def test_exemplar_without_a_location_row_still_carries_memory(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  memory: [report-format]\n  exemplar: deliverable\n")
        _fake_mem(root, "print('## [durable/feedback] id (global)\\nprefer html')\n")
        section = W.unit_bootstrap_prompt(self._args(completion_gate="brand-new-gate"), "t", {}, root=root)
        self.assertIn("prefer html", section)
        self.assertNotIn("Format exemplar", section)

    def test_exemplar_line_and_missing_artifact_root(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  exemplar: deliverable\n")
        reports = self.tmp / ".agent_reports"
        cycle = _cycle(reports, "c", "2026-01-01_a", files=[("report/REPORT.md", 3000)])
        section = W.unit_bootstrap_prompt(self._args(artifact_root=str(reports)), "t", {}, root=root)
        self.assertIn(str(cycle / "artifacts/report/REPORT.md"), section)
        self.assertIn("(3 KB)", section)
        self.assertNotIn("Saved preferences", section)
        args = self._args()
        del args.artifact_root
        self.assertEqual(W.unit_bootstrap_prompt(args, "t", {}, root=root), "")

    def test_memory_block_is_capped(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  memory: [report-format]\n")
        _fake_mem(root, "print('Q' * 20000)\n")
        section = W.unit_bootstrap_prompt(self._args(), "t", {}, root=root)
        self.assertLessEqual(len(section.encode()), 6144 + 700)
        self.assertLess(section.count("Q"), 6145)

    def test_memory_failures_are_silent(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  memory: [report-format]\n")
        for body in (None, "import sys; sys.exit(1)\n", "import time; time.sleep(10)\n", "print('')\n"):
            if body is not None:
                _fake_mem(root, body)
            got = W._bootstrap_memory_block(("report-format",), "t", str(self.tmp), root=root, timeout_s=0.5)
            self.assertEqual(got, "", body)
            self.assertEqual(W.unit_bootstrap_prompt(self._args(), "t", {}, root=root), "")

    def test_unknown_topic_and_kind_are_ignored(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  memory: [nope]\n  exemplar: nothing\n")
        with mock.patch("subprocess.run") as run:
            self.assertEqual(W.unit_bootstrap_prompt(self._args(), "t", {}, root=root), "")
        run.assert_not_called()

    def test_memory_query_carries_topic_project_and_task_words(self):
        root = _fake_unit_root(self.tmp, "bootstrap:\n  memory: [report-format]\n")
        wt = self.tmp / "My_Project-x"
        wt.mkdir()
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="## m\nbody\n")

        with mock.patch("subprocess.run", side_effect=fake_run):
            W._bootstrap_memory_block(("report-format",), "speech /path/x a=b 42 evaluation", str(wt), root=root)
        cmd = captured["cmd"]
        queries = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--query"]
        self.assertIn("report", queries[0])
        self.assertEqual(queries[1], "My Project speech evaluation")
        self.assertEqual(cmd[cmd.index("--limit") + 1], "4")
        self.assertEqual(cmd[cmd.index("--max-bytes") + 1], "6144")


if __name__ == "__main__":
    unittest.main()
