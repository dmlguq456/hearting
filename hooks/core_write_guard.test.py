#!/usr/bin/env python3
"""The two kept write gates: installed release copies and the shared checkout."""
import importlib.util
import json
import os
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOOK = Path(__file__).resolve().with_name("core-write-guard.py")
_spec = importlib.util.spec_from_file_location("core_write_guard", HOOK)
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)


class CoreWriteGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.home = self.tmp / "home"
        (self.home / ".local/share/hearting/releases/v1/hooks").mkdir(parents=True)
        (self.home / ".claude/.harness/bundles/b1").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        self.env.start()
        os.environ.pop("XDG_DATA_HOME", None)
        for key in ("AGENT_ROUTE_FILE", "AGENT_ROUTE_ID", "AGENT_ROUTE_HASH",
                    "AGENT_OWNER_ROUTE_FILE", "AGENT_OWNER_ROUTE_ID", "AGENT_OWNER_ROUTE_HASH",
                    "AGENT_DISPATCH_REGISTERED_WORKER"):
            os.environ.pop(key, None)
        self.primary = self.tmp / "repo"
        (self.primary / ".git/worktrees/feat").mkdir(parents=True)
        self.worktree = self.tmp / "repo-wt/feat"
        self.worktree.mkdir(parents=True)
        (self.worktree / ".git").write_text(
            f"gitdir: {self.primary}/.git/worktrees/feat\n", encoding="utf-8")

    def tearDown(self):
        self.env.stop()

    def test_installed_release_copies_are_refused(self):
        for target in (self.home / ".local/share/hearting/releases/v1/hooks/x.py",
                       self.home / ".claude/.harness/bundles/b1/y.md"):
            self.assertIn("installed release copy", G.violation(str(target), str(self.tmp)))

    def test_worktree_session_is_pointed_at_its_own_copy(self):
        reason = G.violation(str(self.primary / "utilities/a.py"), str(self.worktree))
        self.assertIn(str(self.worktree / "utilities/a.py"), reason)

    def test_allowed_writes(self):
        self.assertEqual(G.violation(str(self.worktree / "utilities/a.py"), str(self.worktree)), "")
        self.assertEqual(G.violation(str(self.primary / ".agent_reports/r.md"), str(self.worktree)), "")
        self.assertEqual(G.violation(str(self.primary / "utilities/a.py"), str(self.primary)), "")
        self.assertEqual(G.violation("relative.py", str(self.worktree)), "")

    def test_exact_primary_runtime_directory_is_writable(self):
        self.assertEqual(G.violation(str(self.primary / ".agent_runtime" / "job" / "run.py"),
                                     str(self.worktree)), "")
        reason = G.violation(str(self.primary / ".agent_runtime-cache" / "run.py"), str(self.worktree))
        self.assertIn("shared checkout", reason)

    def test_runtime_write_exception_remains_narrow_and_external_targets_are_allowed(self):
        target = self.tmp / "external-runtime" / "job.py"
        self.assertFalse(G._runtime_override_allowed(self.home / "cache", self.primary))
        self.assertFalse(G._runtime_override_allowed(self.primary / "cache", self.primary))
        self.assertTrue(G._runtime_override_allowed(target.parent, self.primary))
        self.assertEqual(G.violation(str(self.primary / ".agent_runtime/job.py"),
                                     str(self.worktree)), "")
        with mock.patch.dict(os.environ, {"AGENT_RUNTIME_DIR": str(target.parent)}, clear=False):
            self.assertEqual(G.violation(str(target), str(self.worktree)), "")

    def test_registered_unversioned_worker_scopes_to_sealed_route_cwd(self):
        route_cwd = self.tmp / "recordish"
        route_cwd.mkdir()
        nested_cwd = route_cwd / "sub"
        nested_cwd.mkdir()
        artifact_inside = route_cwd / ".agent_reports"
        artifact_inside.mkdir()
        artifact_outside = self.tmp / "external-artifacts"
        artifact_outside.mkdir()
        unrelated = self.tmp / "unrelated"
        unrelated.mkdir()
        route_file = self.tmp / "route.json"
        route_file.write_text(json.dumps({"cwd": str(route_cwd)}), encoding="utf-8")
        user_file = route_cwd / "user.txt"
        user_file.write_text("keep\n")
        inside_output = artifact_inside / "result.md"
        inside_output.write_text("result\n")
        outside_output = artifact_outside / "result.md"
        outside_output.write_text("result\n")
        outside_link = route_cwd / "artifact-link.md"
        outside_link.symlink_to(inside_output)
        artifact_escape = artifact_inside / "escape.md"
        artifact_escape.symlink_to(unrelated / "outside.md")
        (unrelated / "outside.md").write_text("outside\n")

        def check(artifact):
            route = {"cwd": str(route_cwd), "artifact_root": str(artifact),
                     "source_commit": "unversioned"}
            module = SimpleNamespace(validated_unversioned_route=lambda path: (
                route if Path(path).resolve() == route_cwd.resolve() else None))
            env = {
                "AGENT_HOME": str(Path(__file__).resolve().parents[1]),
                "AGENT_ROUTE_FILE": str(route_file),
                "AGENT_DISPATCH_REGISTERED_WORKER": "1",
                "AGENT_ROUTE_ID": "rt-fixture",
                "AGENT_ROUTE_HASH": "sha256:fixture",
            }
            return mock.patch.dict(sys.modules, {"dispatch_contract": module}), \
                mock.patch.dict(os.environ, env)

        with check(artifact_inside)[0], check(artifact_inside)[1]:
            self.assertIn("이번 cycle 출력", G.violation(str(user_file), str(artifact_inside)))
            self.assertIn("이번 cycle 출력", G.violation(str(user_file), str(nested_cwd)))
            self.assertIn("이번 cycle 출력", G.violation("../user.txt", str(nested_cwd)))
            self.assertIn("이번 cycle 출력", G.violation(str(outside_link), str(artifact_inside)))
            self.assertIn("이번 cycle 출력", G.violation(str(artifact_escape), str(route_cwd)))
            self.assertEqual(G.violation(str(inside_output), str(artifact_inside)), "")
        with check(artifact_outside)[0], check(artifact_outside)[1]:
            self.assertIn("이번 cycle 출력", G.violation(str(user_file), str(artifact_outside)))
            self.assertEqual(G.violation(str(outside_output), str(artifact_outside)), "")
            self.assertEqual(G.violation(str(unrelated / "outside.md"), str(unrelated)), "")

    def test_registered_unversioned_worker_outside_route_and_artifact_is_unrelated(self):
        route_cwd = self.tmp / "recordish"
        route_cwd.mkdir()
        artifact = self.tmp / "artifacts"
        artifact.mkdir()
        unrelated = self.tmp / "elsewhere"
        unrelated.mkdir()
        route_file = self.tmp / "route.json"
        route_file.write_text(json.dumps({"cwd": str(route_cwd)}), encoding="utf-8")
        route = {"cwd": str(route_cwd), "artifact_root": str(artifact),
                 "source_commit": "unversioned"}
        module = SimpleNamespace(validated_unversioned_route=lambda path: route)
        with mock.patch.dict(sys.modules, {"dispatch_contract": module}), \
             mock.patch.dict(os.environ, {
                 "AGENT_HOME": str(Path(__file__).resolve().parents[1]),
                 "AGENT_ROUTE_FILE": str(route_file),
                 "AGENT_DISPATCH_REGISTERED_WORKER": "1",
                 "AGENT_ROUTE_ID": "rt-fixture",
             }):
            self.assertEqual(G.violation(str(unrelated / "note.md"), str(unrelated)), "")

    def test_invalid_bound_unversioned_route_fails_closed(self):
        cwd = self.tmp / "recordish"
        cwd.mkdir()
        route_file = self.tmp / "route.json"
        route_file.write_text(json.dumps({"cwd": str(cwd), "source_commit": "unversioned"}),
                              encoding="utf-8")
        module = SimpleNamespace(validated_unversioned_route=lambda _path: None)
        with mock.patch.dict(sys.modules, {"dispatch_contract": module}), \
             mock.patch.dict(os.environ, {
                 "AGENT_HOME": str(Path(__file__).resolve().parents[1]),
                 "AGENT_ROUTE_FILE": str(route_file),
                 "AGENT_DISPATCH_REGISTERED_WORKER": "1",
                 "AGENT_ROUTE_ID": "rt-fixture",
             }):
            reason = G.violation(str(cwd / "source.txt"), str(self.tmp))
            self.assertIn("route를 검증할 수 없어", reason)

    def test_hook_modes(self):
        payload = {"tool_name": "Edit", "cwd": str(self.worktree),
                   "tool_input": {"file_path": str(self.primary / "core/CORE.md")}}
        env = {**os.environ}
        claude = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                                text=True, capture_output=True, env=env)
        self.assertEqual(claude.returncode, 2)
        self.assertIn("shared checkout", claude.stderr)
        patch = {"tool_name": "apply_patch", "cwd": str(self.worktree),
                 "tool_input": {"input": "*** Begin Patch\n*** Update File: "
                                         f"{self.primary}/core/CORE.md\n*** End Patch\n"}}
        codex = subprocess.run([sys.executable, str(HOOK), "--codex"], input=json.dumps(patch),
                               text=True, capture_output=True, env=env)
        self.assertEqual(codex.returncode, 0)
        self.assertEqual(json.loads(codex.stdout)["decision"], "block")
        check = subprocess.run([sys.executable, str(HOOK), "--check", "notes.md",
                                "--cwd", str(self.worktree)], text=True, capture_output=True, env=env)
        self.assertEqual(check.returncode, 0)
        garbage = subprocess.run([sys.executable, str(HOOK)], input="not json",
                                 text=True, capture_output=True, env=env)
        self.assertEqual(garbage.returncode, 0)


if __name__ == "__main__":
    unittest.main()
