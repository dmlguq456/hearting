#!/usr/bin/env python3
"""Same launch, same commit decision, on every adapter wrapper (audit §4 #8, A6)."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import commit_policy  # noqa: E402
from split_module_fixture import split_patch  # noqa: E402

HARNESSES = ("claude", "codex", "opencode")


def calls(harness: str) -> set[str]:
    tree = ast.parse((ROOT / "adapters" / harness / "bin" / "dispatch-headless.py").read_text(encoding="utf-8"))
    return {node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "commit_policy"}


def common_calls() -> set[str]:
    tree = ast.parse((ROOT / "utilities" / "dispatch_wrapper_common.py").read_text(encoding="utf-8"))
    return {node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "commit_policy"}


class CommitPolicyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.primary = base / "primary"
        self.primary.mkdir()
        git = ["git", "-C", str(self.primary)]
        subprocess.run([*git[:1], "init", "-q", str(self.primary)], check=True)
        subprocess.run([*git, "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run([*git, "config", "user.name", "Fixture"], check=True)
        subprocess.run([*git, "-c", "user.email=f@example.com", "-c", "user.name=F",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        self.linked = base / "linked"
        subprocess.run([*git, "worktree", "add", "-q", "-b", "fx", str(self.linked)], check=True)

    def args(self, worker_type, worktree=None, **extra):
        return argparse.Namespace(worker_type=worker_type, write_scope="source/**",
                                  worktree=str(worktree or self.linked), agent_home=self.primary, **extra)

    def test_who_may_commit(self):
        owner, stage = self.args("owner"), self.args("stage")
        sealed = self.args("stage", commit_expected=True)
        slice_ = self.args("stage", commit_expected=True, subsession_id="slice-1")
        self.assertEqual([commit_policy.may_commit(a) for a in (owner, stage, sealed, slice_)],
                         [True, False, True, False])
        self.assertEqual([commit_policy.no_commit_stage(a) for a in (owner, stage, sealed, slice_)],
                         [False, True, False, True])
        # the primary checkout is not a linked worktree: nothing to hold back there
        self.assertFalse(commit_policy.no_commit_stage(self.args("stage", worktree=self.primary)))
        # a stage that edits no source is not a no-commit worker
        self.assertFalse(commit_policy.no_commit_stage(
            argparse.Namespace(worker_type="stage", write_scope="reports/**", worktree=str(self.linked))))

    def test_verified_unversioned_route_is_known_idle_but_never_commit_or_publish(self):
        route_file = str(self.primary / "route.json")
        for worker_type in ("owner", "stage", "frame"):
            args = self.args(worker_type, self.primary, route_file=route_file)
            with mock.patch.object(commit_policy, "git_operation_state", return_value=(None, False)), \
                 split_patch("dispatch_contract.validated_unversioned_route",
                            return_value={"source_commit": "unversioned"}):
                projection = commit_policy.runtime_permission_projection(args)
                prompt = commit_policy.prompt_clause(args)
            self.assertEqual(projection, {
                "git_operation": None, "git_state_known": True,
                "observation_only": False, "may_commit": False,
                "task_publish_scope": None,
            })
            self.assertNotIn("Git operation in progress", prompt)

    def test_approved_document_commit_is_path_limited_and_preserves_other_work(self):
        git = ["git", "-C", str(self.linked)]
        readme = self.linked / "README.md"
        staged = self.linked / "other-staged.txt"
        unstaged = self.linked / "other-unstaged.txt"
        artifact = self.linked / "cycle-artifact.md"

        readme.write_text("# Existing README\n\nOriginal text.\n", encoding="utf-8")
        staged.write_text("staged baseline\n", encoding="utf-8")
        unstaged.write_text("unstaged baseline\n", encoding="utf-8")
        subprocess.run([*git, "add", "--", "README.md", "other-staged.txt", "other-unstaged.txt"], check=True)
        subprocess.run([*git, "-c", "user.email=f@example.com", "-c", "user.name=F",
                        "commit", "-q", "-m", "tracked baselines"], check=True)

        staged.write_text("staged change owned by another task\n", encoding="utf-8")
        subprocess.run([*git, "add", "--", "other-staged.txt"], check=True)
        unstaged.write_text("unstaged change owned by another task\n", encoding="utf-8")
        artifact.write_bytes(b"cycle artifact bytes stay outside source commits\n")
        baseline_head = subprocess.run([*git, "rev-parse", "HEAD"], check=True,
                                       text=True, capture_output=True).stdout.strip()
        staged_blob = subprocess.run([*git, "rev-parse", ":other-staged.txt"], check=True,
                                     text=True, capture_output=True).stdout.strip()
        staged_diff = subprocess.run([*git, "diff", "--cached", "--binary", "--", "other-staged.txt"],
                                     check=True, capture_output=True).stdout
        unstaged_diff = subprocess.run([*git, "diff", "--binary", "--", "other-unstaged.txt"],
                                       check=True, capture_output=True).stdout
        staged_bytes = staged.read_bytes()
        unstaged_bytes = unstaged.read_bytes()
        artifact_bytes = artifact.read_bytes()
        baseline_status = subprocess.run([*git, "status", "--porcelain", "--untracked-files=all"],
                                         check=True, text=True, capture_output=True).stdout

        verified_readme = "# Approved repository README\n\nReviewed and corrected text.\n"
        readme.write_text(verified_readme, encoding="utf-8")
        self.assertTrue(all(line == line.rstrip() for line in verified_readme.splitlines()))
        subprocess.run([*git, "diff", "--check", "--", "README.md"], check=True)
        subprocess.run([*git, "-c", "user.email=f@example.com", "-c", "user.name=F",
                        "commit", "--only", "-q", "-m", "Refine approved README", "--", "README.md"],
                       check=True)

        committed_paths = subprocess.run([*git, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
                                          check=True, text=True, capture_output=True).stdout.splitlines()
        self.assertEqual(committed_paths, ["README.md"])
        self.assertEqual(subprocess.run([*git, "rev-parse", "HEAD^"], check=True,
                                        text=True, capture_output=True).stdout.strip(), baseline_head)
        committed_readme = subprocess.run([*git, "show", "HEAD:README.md"], check=True,
                                          text=True, capture_output=True).stdout
        self.assertEqual(committed_readme, verified_readme)
        self.assertEqual(subprocess.run([*git, "rev-parse", ":other-staged.txt"], check=True,
                                        text=True, capture_output=True).stdout.strip(), staged_blob)
        self.assertEqual(subprocess.run([*git, "diff", "--cached", "--binary", "--", "other-staged.txt"],
                                        check=True, capture_output=True).stdout, staged_diff)
        self.assertEqual(subprocess.run([*git, "diff", "--binary", "--", "other-unstaged.txt"],
                                        check=True, capture_output=True).stdout, unstaged_diff)
        self.assertEqual(staged.read_bytes(), staged_bytes)
        self.assertEqual(unstaged.read_bytes(), unstaged_bytes)
        self.assertEqual(artifact.read_bytes(), artifact_bytes)
        self.assertEqual(subprocess.run([*git, "status", "--porcelain", "--untracked-files=all"],
                                        check=True, text=True, capture_output=True).stdout, baseline_status)
        self.assertNotEqual(subprocess.run([*git, "cat-file", "-e", "HEAD:cycle-artifact.md"],
                                           check=False, capture_output=True).returncode, 0)

    def test_prompt_row_and_git_metadata_follow_the_decision(self):
        stage, owner = self.args("stage"), self.args("owner")
        self.assertEqual(commit_policy.prompt_clause(stage), commit_policy.PROMPT_CLAUSE)
        self.assertEqual(commit_policy.registry_fragment(stage), ",no_commit=1")
        self.assertEqual((commit_policy.prompt_clause(owner), commit_policy.registry_fragment(owner)), ("", ""))
        self.assertNotIn("Claude", commit_policy.PROMPT_CLAUSE)  # no harness is named as the committer
        common = (self.primary / ".git").resolve()
        self.assertEqual(commit_policy.commit_git_metadata_dirs(owner),
                         ((common / "worktrees" / "linked").resolve(), common / "objects", common / "refs", common / "logs"))
        self.assertEqual(commit_policy.commit_git_metadata_dirs(stage), ())
        self.assertEqual(commit_policy.commit_git_metadata_dirs(self.args("owner", worktree=self.primary)), ())

    def test_task_publish_scope_uses_sealed_complete_scope_without_wording_gate(self):
        base = Path(self._tmp.name)
        from route_identity import route_hash, route_id_from_hash
        origin = base / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
        git = ["git", "-C", str(self.primary)]
        subprocess.run([*git, "remote", "add", "origin", str(origin)], check=True)
        subprocess.run([*git, "push", "-q", "origin", "HEAD:refs/heads/main"], check=True)
        subprocess.run([*git, "fetch", "-q", "origin", "main"], check=True)
        subprocess.run([*git, "remote", "set-head", "origin", "main"], check=True)
        route = {
            "capability": "autopilot-code", "cwd": str(self.linked),
            "artifact_root": str(base / ".agent_reports"),
            "entry_scope_contract_version": 1, "entry_execution_scope": "complete",
            "work_request": {"text": "Publish this approved task branch PR and verify its final CI."},
        }
        route_file = base / "route.json"
        output_dir = Path(route["artifact_root"]) / "campaigns" / "fixture" / "cycle" / "artifacts"
        output_dir.mkdir(parents=True)
        binding = SimpleNamespace(route_file=str(route_file), route_id="", route_hash="")

        def write_route():
            route["route_hash"] = route_hash(route)
            route["route_id"] = route_id_from_hash(route["route_hash"])
            binding.route_id = route["route_id"]
            binding.route_hash = route["route_hash"]
            route_file.write_text(json.dumps(route), encoding="utf-8")

        write_route()
        args = argparse.Namespace(worker_type="owner", dispatch_depth=1,
            owner_route_binding=binding, route_file=None, route_id=None, route_hash=None,
            worktree=str(self.linked))
        publish_env = mock.patch.dict(os.environ, {"AGENT_ARTIFACT_OUTPUT_DIR": str(output_dir)})
        publish_env.start()
        self.addCleanup(publish_env.stop)
        scope = commit_policy.task_publish_scope(args)
        self.assertEqual((scope["branch"], scope["base"]), ("fx", "main"))
        commands = commit_policy.task_publish_commands(scope)
        self.assertIn("git push -u origin HEAD:refs/heads/fx", commands)
        self.assertIn(f"gh pr create --base main --head fx --body-file {output_dir}/dev_logs/pr-description.md",
                      commands)
        self.assertFalse(any("force" in command for command in commands))
        context = {"route_file": str(route_file), "route_id": route["route_id"],
                   "route_hash": route["route_hash"], "worktree": str(self.linked),
                   "capability": "autopilot-code", "artifact_root": route["artifact_root"]}
        self.assertTrue(commit_policy.runtime_permission_context_is_current(context))
        original_text = route["work_request"]["text"]
        route["work_request"]["text"] = "route file changed after binding"
        route_file.write_text(json.dumps(route), encoding="utf-8")
        self.assertFalse(commit_policy.runtime_permission_context_is_current(context))
        self.assertIsNone(commit_policy.task_publish_scope(args))
        route["work_request"]["text"] = original_text
        write_route()

        route["entry_execution_scope"] = "report"
        write_route()
        self.assertIsNone(commit_policy.task_publish_scope(args))
        # A direct compose seals no entry scope: nothing declared a stop
        # before publication, so the owner keeps the same capacity on every
        # harness (Codex owners otherwise lose their sandbox network).
        del route["entry_scope_contract_version"], route["entry_execution_scope"]
        write_route()
        unscoped = commit_policy.task_publish_scope(args)
        self.assertEqual((unscoped["branch"], unscoped["base"]), ("fx", "main"))
        for version, marker in ((1, None), (None, "complete"), (2, "complete"), (1, "partial")):
            with self.subTest(version=version, marker=marker):
                route["entry_scope_contract_version"] = version
                route["entry_execution_scope"] = marker
                write_route()
                self.assertIsNone(commit_policy.task_publish_scope(args))
        route["entry_scope_contract_version"] = 1
        route["entry_execution_scope"] = "complete"
        for wording in (
            "Create a pull request and verify automated checks.",
            "Finish the requested task through PR·CI까지.",
            "Do not publish; this sentence quotes an older PR and CI request.",
            "진행한 변경을 확인하고 전달해 주세요.",
        ):
            with self.subTest(wording=wording):
                route["work_request"]["text"] = wording
                write_route()
                wording_scope = commit_policy.task_publish_scope(args)
                self.assertEqual((wording_scope["branch"], wording_scope["base"],
                                  wording_scope["body_file"]),
                                 (scope["branch"], scope["base"], scope["body_file"]))
        route["capability"] = "autopilot-ship"
        write_route()
        self.assertIsNone(commit_policy.task_publish_scope(args))

    def test_codex_launcher_network_projection_is_the_owner_rule(self):
        # RV-PR1474 P2-1, regressed on the real launcher projection: a depth-1
        # owner has network whatever its capability or entry scope; a depth-2
        # worker, a read-only owner and an owner mid-merge do not.
        import importlib.util
        from route_identity import route_hash, route_id_from_hash
        spec = importlib.util.spec_from_file_location(
            "codex_network_projection", ROOT / "adapters/codex/bin/dispatch-headless.py")
        codex = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(codex)
        origin = Path(self._tmp.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
        subprocess.run(["git", "-C", str(self.primary), "remote", "add", "origin", str(origin)], check=True)
        output_dir = self.primary / "cycle" / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        route_file = Path(self._tmp.name) / "network-route.json"
        rows = []
        for capability, scope, depth, worker, sandbox in (
            ("autopilot-code", "report", 1, "owner", "workspace-write"),
            ("autopilot-code", "complete", 1, "owner", "workspace-write"),
            ("audit", None, 1, "owner", "workspace-write"),
            ("audit", None, 2, "stage", "workspace-write"),
            ("audit", None, 1, "owner", "read-only"),
            # A publication scope adds no network of its own.
            ("autopilot-code", "complete", 1, "owner", "read-only"),
        ):
            route = {"schema_version": 2, "capability": capability,
                     "requested_intensity": "quick", "effective_intensity": "quick",
                     "cwd": str(self.linked), "artifact_root": str(self.primary),
                     "slug": "network-projection"}
            if scope:
                route.update(entry_scope_contract_version=1, entry_execution_scope=scope)
            digest = route_hash(route)
            route.update(route_hash=digest, route_id=route_id_from_hash(digest))
            route_file.write_text(json.dumps(route), encoding="utf-8")
            args = SimpleNamespace(worktree=str(self.linked), route_file=str(route_file),
                                   artifact_output_dir=str(output_dir), dispatch_depth=depth,
                                   worker_type=worker, intensity="quick", sandbox=sandbox,
                                   gpu_execution_scope=False, owner_route_binding=None,
                                   route_id=None, route_hash=None)
            rows.append(codex.nested_headless_network_enabled(args))
            if scope == "complete":
                self.assertTrue(commit_policy.runtime_permission_projection(args)["task_publish_scope"])
        git_dir = commit_policy.worktree_git_dirs(self.linked)[0]
        (git_dir / "MERGE_HEAD").write_text("fixture\n", encoding="utf-8")
        try:
            merge = SimpleNamespace(worktree=str(self.linked), dispatch_depth=1, worker_type="owner",
                                    intensity="quick", sandbox="workspace-write",
                                    gpu_execution_scope=False)
            rows.append(codex.nested_headless_network_enabled(merge))
            self.assertEqual(codex.effective_runtime_sandbox(merge), "read-only")
        finally:
            (git_dir / "MERGE_HEAD").unlink(missing_ok=True)
        self.assertEqual(rows, [True, True, True, False, False, False, False])

    def test_unreadable_git_state_keeps_the_projection_fail_closed(self):
        owner = self.args("owner", dispatch_depth=1)
        with mock.patch.object(commit_policy, "worktree_git_dirs", return_value=None):
            projection = commit_policy.runtime_permission_projection(owner)
            self.assertFalse(projection["git_state_known"])
            self.assertTrue(projection["observation_only"])
            self.assertFalse(projection["may_commit"])
            self.assertIsNone(projection["task_publish_scope"])
            self.assertFalse(commit_policy.may_commit(owner))

    def test_launch_projection_is_shared_but_rechecked_at_the_fence(self):
        owner = self.args("owner", dispatch_depth=1)
        captured = commit_policy.capture_launch_permission_projection(owner)
        self.assertEqual(commit_policy.launch_permission_projection(owner), captured)
        with mock.patch.object(commit_policy, "runtime_permission_projection", return_value=captured) as observe:
            self.assertEqual(commit_policy.assert_launch_permission_projection(owner), captured)
        observe.assert_called_once_with(owner)

        changed = dict(captured, git_state_known=False, git_operation=None,
                       observation_only=True, may_commit=False, task_publish_scope=None)
        with mock.patch.object(commit_policy, "runtime_permission_projection", return_value=changed):
            with self.assertRaises(Exception) as raised:
                commit_policy.assert_launch_permission_projection(owner)
        self.assertEqual(raised.exception.reason, "runtime-permission-projection-changed")
        self.assertIn("axis=git_state_known", raised.exception.detail)
        self.assertIn("predecessor=True", raised.exception.detail)
        self.assertIn("candidate=False", raised.exception.detail)
        self.assertIn("child_spawned=0", raised.exception.detail)

    def test_git_marker_and_unknown_to_known_transitions_fail_before_spawn(self):
        owner = self.args("owner", dispatch_depth=1)
        commit_policy.capture_launch_permission_projection(owner)
        git_dir = commit_policy.worktree_git_dirs(owner.worktree)[0]
        marker = git_dir / "MERGE_HEAD"
        marker.write_text("fixture\n", encoding="utf-8")
        try:
            with self.assertRaises(Exception) as active:
                commit_policy.assert_launch_permission_projection(owner)
            self.assertIn("axis=git_operation", active.exception.detail)
            self.assertIn("candidate='merge'", active.exception.detail)
        finally:
            marker.unlink(missing_ok=True)

        with mock.patch.object(commit_policy, "worktree_git_dirs", return_value=None):
            unknown = commit_policy.capture_launch_permission_projection(owner)
        self.assertFalse(unknown["git_state_known"])
        with self.assertRaises(Exception) as restored:
            commit_policy.assert_launch_permission_projection(owner)
        self.assertIn("axis=git_state_known", restored.exception.detail)
        self.assertIn("predecessor=False", restored.exception.detail)
        self.assertIn("candidate=True", restored.exception.detail)

    def test_merge_state_keeps_owner_in_observation_mode(self):
        git = ["git", "-C", str(self.linked)]
        subprocess.run([*git, "switch", "-c", "merge-source"], check=True, capture_output=True)
        (self.linked / "merge-source.txt").write_text("source\n", encoding="utf-8")
        subprocess.run([*git, "add", "merge-source.txt"], check=True)
        subprocess.run([*git, "-c", "user.email=f@example.com", "-c", "user.name=F",
                        "commit", "-q", "-m", "merge source"], check=True)
        subprocess.run([*git, "switch", "fx"], check=True, capture_output=True)
        subprocess.run([*git, "merge", "--no-commit", "--no-ff", "merge-source"], check=True,
                       capture_output=True)
        git_dir = commit_policy.worktree_git_dirs(self.linked)[0]
        before = {name: (git_dir / name).read_bytes() for name in ("MERGE_HEAD", "index")}
        owner = self.args("owner", dispatch_depth=1)
        self.assertEqual(commit_policy.git_operation(self.linked), "merge")
        self.assertFalse(commit_policy.may_commit(owner))
        self.assertIn("Leave source, index, and merge metadata untouched", commit_policy.prompt_clause(owner))
        import importlib.util
        claude_spec = importlib.util.spec_from_file_location(
            "claude_merge_observation", ROOT / "adapters/claude/bin/dispatch-headless.py")
        claude = importlib.util.module_from_spec(claude_spec)
        claude_spec.loader.exec_module(claude)
        claude_args = argparse.Namespace(worker_type="owner", dispatch_depth=1, worktree=str(self.linked),
            artifact_root=str(self.primary), agent_home=str(ROOT), route_file=None, route_id=None,
            route_hash=None, owner_route_binding=None, permission_mode="config")
        with mock.patch.object(claude, "_configured_permission_posture", return_value=("bypass", "fixture")):
            posture = claude.resolve_permission_posture(claude_args)
        self.assertEqual((posture["mode"], posture["reason"]), ("plan", "active-git-operation"))
        self.assertNotIn(f"Edit(//{str(self.linked).lstrip('/')}/**)", posture["allowed_tools"])
        self.assertIn(f"Edit(//{str(self.primary).lstrip('/')}/**)", posture["allowed_tools"])

        codex_spec = importlib.util.spec_from_file_location(
            "codex_merge_observation", ROOT / "adapters/codex/bin/dispatch-headless.py")
        codex = importlib.util.module_from_spec(codex_spec)
        codex_spec.loader.exec_module(codex)
        codex_args = SimpleNamespace(worktree=str(self.linked), dispatch_depth=1, worker_type="owner",
            intensity="standard", sandbox="workspace-write", gpu_execution_scope=False)
        self.assertFalse(codex.nested_headless_network_enabled(codex_args))
        self.assertEqual(codex.effective_runtime_sandbox(codex_args), "read-only")

        opencode_spec = importlib.util.spec_from_file_location(
            "opencode_merge_observation", ROOT / "adapters/opencode/bin/dispatch-headless.py")
        opencode = importlib.util.module_from_spec(opencode_spec)
        opencode_spec.loader.exec_module(opencode)
        import dispatch_replacement
        for version, source in (("1.19.0", {}),
                                ("2.0.32", {"permissions": [], "agents": {}})):
            with self.subTest(version=version), mock.patch.dict(os.environ, {
                    "OPENCODE_RUNTIME_VERSION": version,
                    "OPENCODE_CONFIG_CONTENT": json.dumps(source),
            }, clear=False):
                opencode_config = json.loads(opencode.scoped_external_directory_config(
                    str(self.primary), worktree=str(self.linked), merge_observation_only=True))
                if version.startswith("2."):
                    self.assertNotIn("permission", opencode_config)
                    self.assertNotIn("agent", opencode_config)
                    self.assertIn("permissions", opencode_config)
                    self.assertIn("agents", opencode_config)
                recorded = dispatch_replacement._applied_permissions(SimpleNamespace(
                    opencode_config_content=json.dumps(opencode_config)))
                self.assertTrue(recorded["opencode_permission"])
                permissions = recorded["opencode_permission"]
                self.assertEqual(permissions["bash"]["git status *"], "allow")
                self.assertEqual(permissions["bash"]["*"], "deny")
                self.assertEqual(permissions["edit"]["*"], "deny")
                self.assertEqual(permissions["edit"][f"{self.primary}/**"], "allow")
        self.assertEqual({name: (git_dir / name).read_bytes() for name in before}, before)

    def test_every_wrapper_asks_the_same_module(self):
        # append_job is shared in dispatch_wrapper_common: the registry
        # fragment lives there now, reached through each wrapper's append_job.
        shared = common_calls()
        self.assertIn("registry_fragment", shared)
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                used = calls(harness) | shared
                self.assertLessEqual({"prompt_clause", "registry_fragment"}, used)
        # Each launch captures the same projection; the shared registry path
        # rechecks it at registration and at the final spawn fence.
        for harness in HARNESSES:
            self.assertIn("capture_launch_permission_projection", calls(harness))
        self.assertIn("launch_permission_projection", calls("codex"))
        self.assertIn("assert_launch_permission_projection", common_calls())



class OpenCodeRealizationTest(unittest.TestCase):
    """OpenCode refuses `git commit` to a no-commit worker through its own permission rules."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "opencode_dispatch_headless_commit", ROOT / "adapters/opencode/bin/dispatch-headless.py")
        cls.wrapper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.wrapper)

    def deny(self, config, agent=None):
        import json
        with mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": "1.19.0"}):
            return json.loads(self.wrapper.deny_commands(
                json.dumps(config), commit_policy.COMMIT_COMMANDS, agent))

    def test_the_global_and_agent_bash_rules_end_with_the_denies(self):
        denies = {"git commit": "deny", "git commit *": "deny"}
        self.assertEqual(self.deny({})["permission"], {"bash": denies})
        out = self.deny({"permission": {"bash": "allow", "edit": "allow"}})["permission"]
        self.assertEqual(list(out), ["edit", "bash"])                       # last match wins
        self.assertEqual(out["bash"], {"*": "allow", **denies})
        out = self.deny({"permission": {"bash": {"git *": "allow"}}})["permission"]["bash"]
        self.assertEqual(out, {"git *": "allow", **denies})
        out = self.deny({"permission": "allow", "agent": {"build": {"permission": {"*": "allow"}}}}, "build")
        self.assertEqual(out["permission"], {"*": "allow", "bash": denies})
        self.assertEqual(out["agent"]["build"]["permission"], {"*": "allow", "bash": denies})

    def test_the_launch_applies_it_only_to_a_no_commit_stage(self):
        source = (ROOT / "adapters/opencode/bin/dispatch-headless.py").read_text(encoding="utf-8")
        self.assertIn("if commit_policy.no_commit_stage(args):", source)
        self.assertIn("deny_commands(", source)

    def test_task_publish_projection_is_exact_and_preserves_inherited_denies(self):
        scope = {"branch": "task/pr-ci", "base": "main", "route_id": "rt-task",
                 "artifact_root": "/tmp/canonical/artifacts",
                 "body_file": "/tmp/canonical/artifacts/dev_logs/pr-description.md"}
        commands = commit_policy.task_publish_commands(scope)
        with mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": "",
                                          "OPENCODE_RUNTIME_VERSION": "1.19.0"}, clear=False):
            allowed = json.loads(self.wrapper.scoped_external_directory_config(
                "/tmp/canonical/artifacts", task_publish_scope=scope))
        bash = allowed["permission"]["bash"]
        self.assertEqual(bash[commands[0]], "allow")
        self.assertEqual(bash[commands[1]], "allow")
        self.assertTrue(all(bash[command] == "allow" for command in commands))
        self.assertFalse(any("force" in command or "pr merge" in command for command in commands))

        with mock.patch.dict(os.environ, {
            "OPENCODE_CONFIG_CONTENT": json.dumps({"permission": {"bash": {"*": "deny"}}}),
            "OPENCODE_RUNTIME_VERSION": "1.19.0",
        }, clear=False):
            denied = json.loads(self.wrapper.scoped_external_directory_config(
                "/tmp/canonical/artifacts", task_publish_scope=scope,
            ))
        denied_bash = denied["permission"]["bash"]
        self.assertEqual(denied_bash, {"*": "deny"})

    def test_v2_task_publish_projection_keeps_native_shell_rules_and_recorder_view(self):
        import dispatch_replacement
        scope = {"branch": "task/pr-ci", "base": "main", "route_id": "rt-task",
                 "artifact_root": "/tmp/canonical/artifacts",
                 "body_file": "/tmp/canonical/artifacts/dev_logs/pr-description.md"}
        commands = commit_policy.task_publish_commands(scope)

        def effect(rows, action, resource):
            import fnmatch
            result = None
            for row in rows:
                if fnmatch.fnmatchcase(action, row["action"]) and fnmatch.fnmatchcase(
                        resource, row["resource"]):
                    result = row["effect"]
            return result

        cases = (
            ({"permissions": [{"action": "shell", "resource": "*", "effect": "ask"}],
              "agents": {}}, "allow"),
            ({"permissions": [{"action": "shell", "resource": "*", "effect": "deny"}],
              "agents": {}}, "deny"),
        )
        with mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": "2.0.32"}, clear=False):
            for source, expected in cases:
                with self.subTest(expected=expected), mock.patch.dict(
                        os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(source)}, clear=False):
                    config = json.loads(self.wrapper.scoped_external_directory_config(
                        "/tmp/canonical/artifacts", task_publish_scope=scope))
                    self.assertNotIn("permission", config)
                    self.assertNotIn("agent", config)
                    self.assertIn("permissions", config)
                    self.assertIn("agents", config)
                    for command in commands:
                        self.assertEqual(effect(config["permissions"], "shell", command), expected)
                    self.assertFalse(any("force" in command or "pr merge" in command
                                         for command in commands))
                    applied = dispatch_replacement._applied_permissions(SimpleNamespace(
                        opencode_config_content=json.dumps(config)))
                    self.assertTrue(applied["opencode_permission"])

    def test_claude_owner_allowlist_contains_only_the_approved_task_publish_commands(self):
        import importlib.util
        path = ROOT / "adapters/claude/bin/dispatch-headless.py"
        spec = importlib.util.spec_from_file_location("claude_task_publish_permission", path)
        claude = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(claude)
        scope = {"branch": "task/pr-ci", "base": "main", "route_id": "rt-task",
                 "artifact_root": "/tmp/canonical/artifacts",
                 "body_file": "/tmp/canonical/artifacts/dev_logs/pr-description.md"}
        rules = claude._allowlist_rules(ROOT, ROOT, "/tmp/canonical/artifacts", "owner",
                                        commit_expected=True, task_publish_scope=scope)
        expected = {f"Bash({command})" for command in commit_policy.task_publish_commands(scope)}
        self.assertTrue(expected.issubset(set(rules)))
        self.assertNotIn("Bash(git push *)", rules)
        self.assertNotIn("Bash(gh pr merge *)", rules)

if __name__ == "__main__":
    unittest.main()
