#!/usr/bin/env python3
"""git < 2.31 has no `rev-parse --path-format=absolute`: it echoes the unknown
option as an extra output line and exits 0. A PATH shim emulates that, so the
route worktree preparation and the index-lock binding must ask git plainly and
absolutize a relative answer themselves."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROUTE = load("route_path_format", HERE / "capability-route.py")
LOCK = load("git_index_lock_path_format", Path(__file__).with_name("git_index_lock.py"))

SHIM = """#!{python}
import os, sys
args = sys.argv[1:]
if "--path-format=absolute" in args:
    print("--path-format=absolute", flush=True)
    args = [a for a in args if a != "--path-format=absolute"]
os.execv(os.environ["H1_REAL_GIT"], [os.environ["H1_REAL_GIT"], *args])
"""


class OldGitPathFormatTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hearting h1 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        real_git = shutil.which("git")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "git"
        shim.write_text(SHIM.format(python=sys.executable), encoding="utf-8")
        shim.chmod(0o755)
        patched = mock.patch.dict(os.environ, {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "H1_REAL_GIT": real_git,
        })
        patched.start()
        self.addCleanup(patched.stop)
        self.real_git = real_git
        origin = self.root / "origin repo.git"
        self.primary = self.root / "primary repo"
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        self.git(self.root, "clone", "-q", str(origin), str(self.primary))
        self.git(self.primary, "config", "user.name", "Fixture")
        self.git(self.primary, "config", "user.email", "fixture@example.test")
        (self.primary / "a.txt").write_text("base\n", encoding="utf-8")
        self.git(self.primary, "add", "a.txt")
        self.git(self.primary, "commit", "-qm", "base")
        self.git(self.primary, "push", "-q", "-u", "origin", "main")
        (self.primary / "sub dir").mkdir()
        self.linked = self.root / "linked worktree"
        self.git(self.primary, "worktree", "add", "-q", "-b", "topic", str(self.linked))
        self.reports = self.primary / ".agent_reports"
        self.reports.mkdir()

    def git(self, cwd, *args, env=None):
        return subprocess.run([self.real_git, "-C", str(cwd), *args], check=True,
                              capture_output=True, text=True, env=env).stdout.strip()

    def git_path(self, cwd, path):
        value = self.git(cwd, "rev-parse", "--git-path", path)
        return os.path.realpath(value if os.path.isabs(value) else Path(cwd) / value)

    def test_shim_emulates_old_git(self):
        out = subprocess.run(["git", "-C", str(self.primary), "rev-parse",
                              "--path-format=absolute", "--git-common-dir"],
                             check=True, capture_output=True, text=True).stdout
        self.assertEqual(out.splitlines()[0], "--path-format=absolute")
        self.assertEqual(len(out.splitlines()), 2)

    def test_worktree_preparation_from_primary_and_linked(self):
        made = ROUTE.prepare_isolated_worktree(self.linked, "demo", artifact_root=self.reports)
        self.assertEqual(made["state"], "created", made)
        made = ROUTE.prepare_isolated_worktree(self.primary, "demo2", artifact_root=self.reports)
        self.assertEqual(made["state"], "created", made)
        expected_common = os.path.realpath(self.primary / ".git")
        self.assertEqual(ROUTE._git_common_dir(self.primary / "sub dir"), expected_common)
        self.assertEqual(ROUTE._git_common_dir(self.linked), expected_common)

    def test_index_lock_binds_relative_and_linked_paths(self):
        bound = LOCK._run_git_index(
            ["git", "-C", str(self.primary), "-C", "sub dir", "commit"], str(self.root),
                                    dict(os.environ), 5.0)
        self.assertIsNotNone(bound)
        self.assertEqual(bound["index"], os.path.realpath(self.primary / ".git" / "index"))
        bound = LOCK._run_git_index(["git", "commit"], str(self.linked), dict(os.environ), 5.0)
        self.assertIsNotNone(bound)
        self.assertEqual(bound["index"], self.git_path(self.linked, "index"))
        self.assertNotEqual(bound["index"], os.path.realpath(self.primary / ".git" / "index"))

    def test_index_context_variants_bind_the_exact_target(self):
        expected_primary = os.path.realpath(self.primary / ".git" / "index")
        cases = [
            ("caller cwd", ["git", "status"], str(self.primary / "sub dir"),
             dict(os.environ), expected_primary, self.primary / "sub dir"),
            ("repeated relative -C", ["git", "-C", str(self.primary), "-C", "sub dir", "status"],
             str(self.root), dict(os.environ), expected_primary, self.primary / "sub dir"),
            ("explicit separate git-dir/work-tree",
             ["git", "--git-dir", str(self.primary / ".git"), "--work-tree", str(self.primary), "status"],
             str(self.root), dict(os.environ), expected_primary, self.root),
            ("explicit equals git-dir/work-tree",
             ["git", f"--git-dir={self.primary / '.git'}", f"--work-tree={self.primary}", "status"],
             str(self.root), dict(os.environ), expected_primary, self.root),
        ]
        relative_env = dict(os.environ, GIT_INDEX_FILE="alt dir/index file")
        (self.primary / "alt dir").mkdir()
        self.git(self.primary, "read-tree", "HEAD", env=relative_env)
        cases.append(("relative GIT_INDEX_FILE after -C",
                      ["git", "-C", str(self.primary), "status"], str(self.root), relative_env,
                      os.path.realpath(self.primary / "alt dir" / "index file"), self.primary))
        absolute_index = self.root / "external index file"
        absolute_env = dict(os.environ, GIT_INDEX_FILE=str(absolute_index))
        self.git(self.primary, "read-tree", "HEAD", env=absolute_env)
        cases.append(("absolute GIT_INDEX_FILE",
                      ["git", "-C", str(self.primary), "status"], str(self.root), absolute_env,
                      os.path.realpath(absolute_index), self.primary))

        for label, argv, cwd, env, expected_index, expected_cwd in cases:
            with self.subTest(context=label):
                bound = LOCK._run_git_index(argv, cwd, env, 5.0)
                self.assertIsNotNone(bound)
                self.assertEqual(bound["index"], expected_index)
                self.assertEqual(bound["cwd"], os.path.realpath(expected_cwd))


if __name__ == "__main__":
    unittest.main()
