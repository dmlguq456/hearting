#!/usr/bin/env python3
"""W7D: the stage-session ledger lives under `.runtime/`, never a legacy bucket."""
from __future__ import annotations

import argparse
import json
import os
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_producer  # noqa: E402
import stage_session_runtime as runtime  # noqa: E402
from dispatch_contract import DispatchContractError  # noqa: E402


def _args(worktree: Path, brief: Path, fixed: Path, **overrides) -> argparse.Namespace:
    values = dict(
        subsession_id="ss-0001", subsession_index=1, subsession_count=1, subsession_mode="serial",
        subsession_purpose="planned", session_chain_id="ssc-0001", phase_brief=str(brief),
        stage_authority=0, fixed_file=[str(fixed)], narrow_verify="python3 -c pass",
        expected_round_trips=1, state_dir=None, dispatch_depth=2, route_id="rt-0123456789abcdef",
        route_node="execute", attempt_id="att-" + "1" * 32, worktree=str(worktree),
        execution_surface="registered-headless", registered_worker="1", fallback_hop="same-harness-headless",
        capability_owner="owner",
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class StageSessionStateDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "artifact-root"
        self.root.mkdir()
        self.worktree = base / "wt"
        self.worktree.mkdir()
        self.fixed = self.worktree / "a.py"
        self.fixed.write_text("x\n", encoding="utf-8")
        self.brief = base / "brief.md"
        self.brief.write_text("brief\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_state_dir_is_runtime_owned(self):
        self.assertEqual(
            runtime.default_state_dir(self.root, "rt-0123456789abcdef"),
            self.root / ".runtime" / "stage-sessions" / "rt-0123456789abcdef",
        )
        args = _args(self.worktree, self.brief, self.fixed)
        runtime.bind(args, artifact_root=self.root, action="dry-run")
        ledger = Path(args.state_ledger)
        self.assertEqual(ledger.parent, self.root / ".runtime" / "stage-sessions" / "rt-0123456789abcdef")
        self.assertEqual(artifact_producer.check_write(self.root, ledger)["reason"], "runtime-owned")

    def test_only_declared_subsession_gets_chain_responsibilities(self):
        args=_args(self.worktree,self.brief,self.fixed)
        runtime.bind(args,artifact_root=self.root,action="dry-run")
        fragment=runtime.prompt_fragment(args)
        self.assertIn("dispatch_subsession_handoff.py",fragment)
        self.assertIn("Before compaction",fragment)
        self.assertIn("Native helper support",fragment)
        self.assertIn(f"final handoff: `artifact: {args.state_ledger}`",fragment)
        self.assertEqual(runtime.prompt_fragment(argparse.Namespace(subsession_id=None)),"")

    def test_dry_run_creates_nothing(self):
        args = _args(self.worktree, self.brief, self.fixed)
        runtime.bind(args, artifact_root=self.root, action="dry-run")
        self.assertFalse((self.root / ".runtime").exists())

    def test_adapter_binding_accepts_next_route_literals_and_rejects_globs(self):
        names = ["app/[id]/page.tsx", "app/[...slug]/page.tsx", "app/(group)/page.tsx"]
        for name in names:
            path = self.worktree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("page\n")
        args = _args(self.worktree, self.brief, self.fixed, fixed_file=names)
        runtime.bind(args, artifact_root=self.root, action="dry-run")
        self.assertEqual(args.fixed_file, sorted(str(self.worktree / name) for name in names))
        for pattern in ("app/*/page.tsx", "app/?.tsx", "app/[ab]/page.tsx"):
            with self.subTest(pattern=pattern), self.assertRaises(DispatchContractError) as caught:
                runtime.bind(_args(self.worktree, self.brief, self.fixed, fixed_file=[pattern]),
                             artifact_root=self.root, action="dry-run")
            self.assertEqual(caught.exception.reason, "subsession-fixed-file-not-exact")
        self.assertFalse((self.root / ".runtime").exists())

    def test_a_bracket_in_the_checkout_name_does_not_turn_a_new_file_into_a_glob(self):
        tree = self.worktree / "project[1]"
        tree.mkdir()
        from stage_session_contract import _fixed_files
        expected = [str(tree / "new.tsx")]
        self.assertEqual(_fixed_files(["new.tsx"], worktree=tree, session_id="ss-fixture"), expected)
        args = _args(tree, self.brief, self.fixed, fixed_file=["new.tsx"])
        runtime.bind(args, artifact_root=self.root, action="dry-run")
        self.assertEqual(args.fixed_file, expected)

    def test_legacy_state_dir_is_refused_once_the_cutover_is_active(self):
        artifact_producer.activate(self.root, repository_id="repo_" + "c" * 32, artifact_root_id="root_" + "d" * 32)
        legacy = self.root / "plans" / "stage-sessions" / "rt-0123456789abcdef" / "_internal" / "state"
        args = _args(self.worktree, self.brief, self.fixed, state_dir=str(legacy))
        with self.assertRaises(DispatchContractError) as caught:
            runtime.bind(args, artifact_root=self.root, action="start")
        self.assertEqual(caught.exception.reason, "subsession-state-dir-write-denied")
        self.assertIn("legacy-top-level-write-denied", caught.exception.detail)
        self.assertFalse(legacy.exists())
        self.assertFalse(Path(str(legacy) + ".lock").exists())
        default = _args(self.worktree, self.brief, self.fixed)
        runtime.bind(default, artifact_root=self.root, action="dry-run")
        self.assertTrue(default.state_ledger.startswith(str(self.root / ".runtime" / "stage-sessions")))

    def _artifact_args(self, *, node="voter", worktree=None, brief_text=None, scope=None, outputs=None):
        worktree = worktree or self.worktree
        sources = ["pyproject.toml", "sr_corrnet_sd/diar.py", "sr_corrnet_sd/paths.py", "uv.lock"]
        for name in sources:
            path = worktree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")
        if brief_text is not None:
            self.brief.write_text(brief_text, encoding="utf-8")
        route = {
            "route_id": "rt-0123456789abcdef", "route_hash": "sha256:fixture",
            "artifact_root": str(self.root), "cwd": str(worktree),
            "capability": "autopilot-code", "effective_intensity": "standard",
            "nodes": [{"id": node, "unit": "qa/other-check", "outputs": outputs or ["reviews/claims/**"],
                       "write_scope": scope or ["reviews/claims/**", "reviews/claim-verdict.json"]}],
        }
        route_file = self.root / "route.json"
        route_file.write_text(json.dumps(route), encoding="utf-8")
        args = _args(worktree, self.brief, worktree / sources[0], fixed_file=sources,
                     route_node=node, route_file=str(route_file), artifact_root=str(self.root))
        cycle = {"campaign_id": "camp_fixture", "cycle_id": "cyc_fixture",
                 "producer_id": "prod_fixture", "route_id": route["route_id"]}
        output_dir = self.root / "campaigns" / "camp_fixture" / "cyc_fixture" / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        return args, route, cycle, output_dir

    def _bind_artifact(self, args, route, cycle, output_dir, action="dry-run"):
        import artifact_producer
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(artifact_producer, "load_route", return_value=route), \
             mock.patch.object(artifact_producer, "route_cycle_for", return_value=cycle), \
             mock.patch.object(artifact_producer, "cycle_dir", return_value=output_dir.parent):
            runtime.bind(args, artifact_root=self.root, action=action)

    def test_artifact_only_fence_uses_brief_outputs_and_keeps_declaration_identity(self):
        brief = ("사건 문장: `reviews/claims/voter3.json`, `reviews/claims/voter3-evidence.md`, "
                 "`reviews/claims/voter3-handoff.md`를 기록한다. `reviews/claim-verdict.json을` "
                 "강조 출력 **reviews/claims/voter3.json**, "
                 "_reviews/claims/voter3-evidence.md_, "
                 "__reviews/claims/voter3-handoff.md__도 적는다. "
                 "확인하고 읽지 말 경로는 `reviews/claims/claim-verify.md·x/voter1-*를` 포함한다. "
                 "연결 경로 a/b.json·reviews/claims/**, glob reviews/claims/voter*.json, "
                 "강조 경로 _internal/x.md와 reviews/claims/**, **/x.md도 제외한다. "
                 "디렉터리 reviews/claims/, URL https://example.test/out.json, "
                 "범위 밖 reports/other.md, ../escape.json, placeholder reviews/claims/<filename>.md.\n")
        args, route, cycle, output_dir = self._artifact_args(brief_text=brief)
        declarations = sorted(str((self.worktree / name).resolve()) for name in [
            "pyproject.toml", "sr_corrnet_sd/diar.py", "sr_corrnet_sd/paths.py", "uv.lock"])
        self._bind_artifact(args, route, cycle, output_dir, action="start")
        expected = {
            str(output_dir / "reviews/claims/voter3.json"),
            str(output_dir / "reviews/claims/voter3-evidence.md"),
            str(output_dir / "reviews/claims/voter3-handoff.md"),
            str(output_dir / "reviews/claims/claim-verify.md"),
            str(output_dir / "reviews/claim-verdict.json"),
        }
        self.assertEqual(args.fixed_file, sorted(str((self.worktree / name).resolve()) for name in [
            "pyproject.toml", "sr_corrnet_sd/diar.py", "sr_corrnet_sd/paths.py", "uv.lock"]))
        self.assertEqual(args.effective_fixed_files, sorted(expected))
        for relative in ("reviews/claims/voter3.json",
                         "reviews/claims/voter3-evidence.md",
                         "reviews/claims/voter3-handoff.md"):
            self.assertIn(str(output_dir / relative), args.effective_fixed_files)
        for excluded in ("_internal/x.md", "reviews/claims/**", "**/x.md"):
            self.assertNotIn(str(output_dir / excluded), args.effective_fixed_files)
        self.assertEqual(args.fixed_files_sha256, runtime._sha_files(declarations))
        self.assertEqual(runtime.metadata(args).split("fixed_files_sha256=")[1].split(",")[0],
                         runtime._sha_files(declarations))
        self.assertEqual(runtime.environment(args)["AGENT_WORKER_FIXED_FILES_SHA256"],
                         runtime._sha_files(declarations))
        ledger = Path(args.state_ledger).read_text(encoding="utf-8")
        meta = json.loads(ledger.splitlines()[0].removeprefix("<!-- worker-state-ledger:v1 ").removesuffix(" -->"))
        self.assertEqual(set(meta["fixed_files"]), expected)
        self.assertIn("## Forbidden files\n" + "\n".join(f"- {p}" for p in declarations), ledger)
        fragment = runtime.prompt_fragment(args)
        fence = fragment.split("- fixed files (exhaustive; out-of-list work stops with a handoff):\n", 1)[1]
        fence = fence.split("- declared worktree inputs", 1)[0]
        self.assertEqual(set(line[4:] for line in fence.splitlines() if line.startswith("  - ")), expected)
        self.assertIn("- declared worktree inputs are read-only (forbidden files):", fragment)
        voter = subprocess.run([sys.executable, str(Path(__file__).with_name("worker-state-ledger.py")),
                                "check", "--path", args.state_ledger, "--attempt-id", args.attempt_id,
                                "--file", str(output_dir / "reviews/claims/voter3.json")],
                               text=True, capture_output=True)
        source = subprocess.run([sys.executable, str(Path(__file__).with_name("worker-state-ledger.py")),
                                 "check", "--path", args.state_ledger, "--attempt-id", args.attempt_id,
                                 "--file", str(self.worktree / "pyproject.toml")],
                                text=True, capture_output=True)
        self.assertEqual(voter.returncode, 0, voter.stderr)
        self.assertEqual(source.returncode, 65)
        self.assertIn("file-outside-fixed-list", source.stderr)

        continuation = brief + "\n## From the previous attempt\nPrior output: reviews/claims/voter3.json\n"
        args2, route2, cycle2, output2 = self._artifact_args(brief_text=continuation)
        args2.attempt_id = "att-" + "2" * 32
        self._bind_artifact(args2, route2, cycle2, output2, action="start")
        self.assertEqual(args2.effective_fixed_files, args.effective_fixed_files)
        self.assertEqual(args2.fixed_files_sha256, args.fixed_files_sha256)
        self.assertNotEqual(args2.phase_brief_sha256, args.phase_brief_sha256)

    def test_artifact_fence_fallback_and_scope_boundaries(self):
        args, route, cycle, output_dir = self._artifact_args(brief_text="No path named here.\n",
                                                              outputs=["reviews/claim-verdict.json"])
        self._bind_artifact(args, route, cycle, output_dir)
        self.assertEqual(args.effective_fixed_files, [str(output_dir / "reviews/claim-verdict.json")])
        for scope in (["source/**"], ["source-alternative/**"], ["source-scoped"], ["tests/**"],
                      ["source/**", "reviews/claims/**"], ["target-artifact"]):
            with self.subTest(scope=scope):
                args, route, cycle, output_dir = self._artifact_args(scope=scope)
                self._bind_artifact(args, route, cycle, output_dir)
                self.assertEqual(args.effective_fixed_files, args.fixed_file)

    def test_nonconcrete_outputs_fall_back_to_declared_scope_and_unbound_keeps_declarations(self):
        args, route, cycle, output_dir = self._artifact_args(brief_text="Only reviews/claims/** is named.\n",
                                                              outputs=["reviews/claims/**", "revised-artifact"])
        self._bind_artifact(args, route, cycle, output_dir)
        self.assertEqual(args.effective_fixed_files, sorted([
            str(output_dir / "reviews/claims/**"),
            str(output_dir / "reviews/claim-verdict.json"),
        ]))
        import artifact_producer
        args, route, cycle, output_dir = self._artifact_args()
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(artifact_producer, "load_route", return_value=route), \
             mock.patch.object(artifact_producer, "route_cycle_for", return_value=None):
            runtime.bind(args, artifact_root=self.root, action="dry-run")
        self.assertEqual(args.effective_fixed_files, args.fixed_file)

    def test_glob_only_artifact_fallback_is_a_component_bounded_ledger_fence(self):
        args, route, cycle, output_dir = self._artifact_args(
            brief_text="이 노드 범위: reviews/claims/**만 다룬다.\n",
            outputs=["reviews/claims/**"],
            scope=["reviews/claims/**"],
        )
        self._bind_artifact(args, route, cycle, output_dir, action="start")
        pattern = str(output_dir / "reviews/claims/**")
        self.assertEqual(args.effective_fixed_files, [pattern])

        def check(candidate):
            return subprocess.run(
                [sys.executable, str(Path(__file__).with_name("worker-state-ledger.py")),
                 "check", "--path", args.state_ledger, "--attempt-id", args.attempt_id,
                 "--file", str(candidate)], text=True, capture_output=True,
            )

        self.assertEqual(check(output_dir / "reviews/claims/new-evidence.md").returncode, 0)
        foreign = check(output_dir / "reviews/claims-foreign/new-evidence.md")
        self.assertEqual(foreign.returncode, 65)
        self.assertIn("file-outside-fixed-list", foreign.stderr)
        source = check(self.worktree / "pyproject.toml")
        self.assertEqual(source.returncode, 65)
        self.assertIn("file-outside-fixed-list", source.stderr)

    def test_route_derivation_failures_fall_back_to_declared_files(self):
        for failure in ("missing", "malformed", "resolver"):
            with self.subTest(failure=failure):
                args, route, cycle, output_dir = self._artifact_args(brief_text="reviews/claims/item.md\n")
                declared = sorted(str((self.worktree / name).resolve()) for name in [
                    "pyproject.toml", "sr_corrnet_sd/diar.py", "sr_corrnet_sd/paths.py", "uv.lock"])
                if failure == "missing":
                    args.route_file = str(self.root / "missing-route.json")
                    self._bind_artifact(args, route, cycle, output_dir, action="start")
                elif failure == "malformed":
                    Path(args.route_file).write_text("{broken", encoding="utf-8")
                    self._bind_artifact(args, route, cycle, output_dir, action="start")
                else:
                    import worker_bootstrap
                    with mock.patch.object(worker_bootstrap, "resolve_node_scope",
                                           side_effect=ValueError("resolver fixture failure")):
                        self._bind_artifact(args, route, cycle, output_dir, action="start")
                self.assertEqual(args.effective_fixed_files, declared)
                self.assertEqual(args.readonly_input_files, [])
                self.assertTrue(Path(args.state_ledger).is_file())

    def test_declared_artifact_is_merged_before_worktree_readonly_classification(self):
        args, route, cycle, output_dir = self._artifact_args(worktree=self.root)
        declared_output = output_dir / "reviews/claims/declared.md"
        declared_output.parent.mkdir(parents=True, exist_ok=True)
        declared_output.write_text("fixture\n", encoding="utf-8")
        args.fixed_file.append(str(declared_output))
        self._bind_artifact(args, route, cycle, output_dir)
        self.assertIn(str(declared_output), args.effective_fixed_files)
        self.assertNotIn(str(declared_output), args.readonly_input_files)
        self.assertEqual(len(args.readonly_input_files), 4)

    def test_absolute_artifact_scope_is_matched_relative_to_cycle_output(self):
        args, route, cycle, output_dir = self._artifact_args(
            brief_text="reviews/claims/voter3.json\n")
        route["nodes"][0]["write_scope"] = [str(output_dir / "reviews/claims/**")]
        Path(args.route_file).write_text(json.dumps(route), encoding="utf-8")
        self._bind_artifact(args, route, cycle, output_dir)
        self.assertEqual(args.effective_fixed_files, [str(output_dir / "reviews/claims/voter3.json")])

    def test_fence_and_assignment_prompt_resolve_the_same_cycle_before_and_after_parent_binding(self):
        args, route, cycle, output_dir = self._artifact_args(
            brief_text="reviews/claims/voter3.json\n")
        self._bind_artifact(args, route, cycle, output_dir)
        import artifact_producer
        import worker_bootstrap
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(artifact_producer, "load_route", return_value=route), \
             mock.patch.object(artifact_producer, "route_cycle_for", return_value=cycle), \
             mock.patch.object(artifact_producer, "cycle_dir", return_value=output_dir.parent):
            lineage_prompt = worker_bootstrap.assignment_prompt(args, "task", {})
        scope_line = next(line for line in lineage_prompt.splitlines() if "artifact_write_scope=" in line)
        self.assertIn(str(output_dir), scope_line)

        args.parent_attempt_id = "att-owner"
        loaded = mock.Mock(binding=cycle)
        admission = mock.Mock(allow=True)
        with mock.patch.dict("os.environ", {"AGENT_ARTIFACT_CYCLE_ID": "cyc_stale",
                                             "AGENT_ROUTE_NODE": "parent-node",
                                             "AGENT_DISPATCH_WORKTREE": "/stale/parent"}, clear=True), \
             mock.patch.object(artifact_producer, "load_route", return_value=route), \
             mock.patch.object(artifact_producer, "read_cycle_record", return_value=cycle), \
             mock.patch.object(artifact_producer, "cycle_route_admission", return_value=admission), \
             mock.patch.object(artifact_producer, "cycle_dir", return_value=output_dir.parent), \
             mock.patch("dispatch_terminal_commit.load_producer_binding", return_value=loaded):
            bound_prompt = worker_bootstrap.assignment_prompt(args, "task", os.environ)
        bound_scope_line = next(line for line in bound_prompt.splitlines() if "artifact_write_scope=" in line)
        self.assertEqual(bound_scope_line, scope_line)

    def test_fence_is_generic_and_excludes_escape_prefix_and_other_cycle(self):
        brief = ("reviews/claims/voter3.json "
                 "reviews/claims-foreign/near.json "
                 "../cyc_fixture-other/artifacts/reviews/claims/foreign.json\n")
        args, route, cycle, output_dir = self._artifact_args(node="another-node", brief_text=brief)
        self._bind_artifact(args, route, cycle, output_dir)
        self.assertEqual(args.effective_fixed_files, [str(output_dir / "reviews/claims/voter3.json")])

    def test_symlink_escape_is_excluded(self):
        args, route, cycle, output_dir = self._artifact_args(
            brief_text="reviews/claims/voter3.json reviews/claims/escape.md\n")
        claims = output_dir / "reviews/claims"
        claims.mkdir(parents=True, exist_ok=True)
        outside = self.root / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        (claims / "escape.md").symlink_to(outside)
        self._bind_artifact(args, route, cycle, output_dir)
        self.assertEqual(args.effective_fixed_files, [str(claims / "voter3.json")])


if __name__ == "__main__":
    unittest.main()
