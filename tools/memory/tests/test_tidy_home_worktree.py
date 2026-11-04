#!/usr/bin/env python3
"""session-tidy runner: the worktree the registered memory worker is launched under.

A project session keeps its own git worktree.  A session outside any git project -- ``$HOME``
under a managed release, whose harness root is immutable and not a checkout either -- is
registered under the seat's home worktree, ``$XDG_DATA_HOME/hearting/home-worktree``, made
once and ignored by git below its README.  Everything runs inside ``tidy_isolation``.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "utilities"))

from tidy_isolation import isolated_env  # noqa: E402
import session_tidy_runner as runner  # noqa: E402
sys.path.append(str(Path(__file__).resolve().parents[3] / "utilities"))  # split_module_fixture
from split_module_fixture import patch_split  # noqa: E402


def git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=30)


class HomeWorktreeTest(unittest.TestCase):

    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        self.plain = self.iso.root / "plain"                 # a cwd outside any git project
        self.plain.mkdir()
        self.release = self.iso.root / "release"             # stands in for an immutable managed release root
        self.release.mkdir()
        patch = patch_split(runner, "ROOT", self.release)
        patch.start()
        self.addCleanup(patch.stop)

    def expected(self) -> Path:
        return runner.data_home() / "hearting" / "home-worktree"

    def test_a_project_session_is_registered_under_its_own_worktree(self):
        project = self.iso.root / "proj"
        project.mkdir()
        self.assertEqual(git(project, "init", "-q").returncode, 0)
        inner = project / "sub"
        inner.mkdir()
        with self.iso.patched_environ():
            self.assertEqual(runner._git_worktree(str(inner)), str(inner))
            self.assertFalse(self.expected().exists())

    def test_the_harness_checkout_is_preferred_over_the_home_worktree(self):
        checkout = self.iso.root / "checkout"
        checkout.mkdir()
        self.assertEqual(git(checkout, "init", "-q").returncode, 0)
        with patch_split(runner, "ROOT", checkout), self.iso.patched_environ():
            self.assertEqual(runner._git_worktree(str(self.plain)), str(checkout))
            self.assertFalse(self.expected().exists())

    def test_a_session_outside_any_git_project_gets_the_home_worktree_made_once(self):
        with self.iso.patched_environ():
            home = Path(runner._git_worktree(str(self.plain)))
            self.assertEqual(home, self.expected())
            self.assertTrue(str(home).startswith(str(self.iso.root)), home)   # isolated, never the real one
            self.assertEqual(git(home, "rev-parse", "--show-toplevel").stdout.strip(), str(home.resolve()))
            head = git(home, "rev-parse", "HEAD").stdout.strip()
            self.assertTrue(head)
            self.assertTrue((home / "README.md").is_file())
            self.assertTrue((home / ".gitignore").is_file())
            # the worker's folder under the artifact root never makes it dirty
            folder = home / ".agent_reports" / ".runtime" / "session-tidy" / "b1"
            folder.mkdir(parents=True)
            (folder / "input_v1.json").write_text("{}", encoding="utf-8")
            self.assertEqual(git(home, "status", "--porcelain").stdout, "")
            # made once: the next call returns the same worktree with the same HEAD
            self.assertEqual(runner._git_worktree(str(self.plain)), str(home))
            self.assertEqual(git(home, "rev-parse", "HEAD").stdout.strip(), head)
            self.assertEqual(runner.launch_worktree({"cwd": str(self.plain)}), str(home))

    def test_the_artifact_root_is_the_home_worktrees_own(self):
        with self.iso.patched_environ():
            home = Path(runner.home_worktree())
        with patch_split(runner, "ROOT", ROOT):        # the real artifact-root.sh
            self.assertEqual(runner.artifact_root_for(str(home)), home / ".agent_reports")

    def test_a_home_worktree_that_cannot_be_made_ends_the_tidy_with_one_line(self):
        with self.iso.patched_environ():
            parent = self.expected().parent
            parent.parent.mkdir(parents=True, exist_ok=True)
            parent.write_text("not a folder", encoding="utf-8")   # hearting/ is a file: mkdir fails
            with self.assertRaises(runner.RunnerFailure) as caught:
                runner._git_worktree(str(self.plain))
            self.assertTrue(str(caught.exception).startswith("cannot make the home worktree: "), caught.exception)


if __name__ == "__main__":
    unittest.main()
