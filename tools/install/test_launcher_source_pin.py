"""F-80 — `~/.local/bin/*` launchers are pinned to the primary checkout.

User 2026-08-16, on a manual symlink fix: "이렇게 하는게 좀 땜빵의 성격이면 난 별로긴한데".
It was. `install_launchers` resolved its source through `paths.resolve_source()`, i.e.
`agent_home()` — the tree running the install — so `~/.local/bin/fleet` held whatever ran
install last. Observed 2026-08-15: it followed a release snapshot and the board silently
went back several versions.

The sharper risk was the next one: at that moment the codex runtime was activated from a
task worktree (`hearting-wt/namespace-local-post-exit-receipt`, one of eleven). An install
from there would have pointed a permanent launcher at a temporary tree, and the link would
break the moment that worktree was pruned.

The repository already draws this line for artifacts — `artifact-guard.sh` refuses writes
to a worktree's `.agent_reports` and redirects to the primary checkout — so the launchers
were the inconsistent surface, not a new policy. `primary_checkout()` resolves it the same
way `utilities/artifact-root.sh` does: the first `git worktree list --porcelain` entry.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


class PrimaryCheckoutTest(unittest.TestCase):

    def setUp(self):
        if not subprocess.run(["git", "--version"], capture_output=True).returncode == 0:
            self.skipTest("git unavailable")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.main = self.root / "main"
        self.main.mkdir()
        _git("init", "-q", cwd=self.main)
        _git("config", "user.email", "t@example.com", cwd=self.main)
        _git("config", "user.name", "t", cwd=self.main)
        (self.main / "seed.txt").write_text("x", encoding="utf-8")
        _git("add", "-A", cwd=self.main)
        _git("commit", "-qm", "seed", cwd=self.main)
        self.worktree = self.root / "wt" / "task"
        _git("worktree", "add", "-q", "-b", "task", str(self.worktree), cwd=self.main)

    def test_a_worktree_resolves_to_the_primary_checkout(self):
        self.assertEqual(paths.primary_checkout(self.worktree), self.main)

    def test_the_primary_resolves_to_itself(self):
        self.assertEqual(paths.primary_checkout(self.main), self.main)

    def test_symlinked_access_path_is_normalized(self):
        """Git names the primary by whatever path the caller arrived through, so two
        worktrees can report the same checkout under different strings. The launcher
        target must not depend on which one ran install."""
        alias = self.root / "alias"
        alias.symlink_to(self.main)
        self.assertEqual(paths.primary_checkout(alias), self.main)

    def test_a_non_git_tree_is_returned_unchanged(self):
        """An extracted managed release has no `.git`; it must keep resolving to itself
        rather than reaching for some unrelated checkout."""
        release = self.root / "release"
        release.mkdir()
        self.assertEqual(paths.primary_checkout(release), release)

    def test_a_missing_directory_is_returned_unchanged(self):
        absent = self.root / "gone"
        self.assertEqual(paths.primary_checkout(absent), absent)


class LauncherSourceTest(unittest.TestCase):
    """The pin is what `install_launchers` consumes."""

    def setUp(self):
        if not subprocess.run(["git", "--version"], capture_output=True).returncode == 0:
            self.skipTest("git unavailable")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.main = self.root / "main"
        self.main.mkdir()
        _git("init", "-q", cwd=self.main)
        _git("config", "user.email", "t@example.com", cwd=self.main)
        _git("config", "user.name", "t", cwd=self.main)
        (self.main / "seed.txt").write_text("x", encoding="utf-8")
        _git("add", "-A", cwd=self.main)
        _git("commit", "-qm", "seed", cwd=self.main)
        self.worktree = self.root / "wt" / "task"
        _git("worktree", "add", "-q", "-b", "task", str(self.worktree), cwd=self.main)
        self._prior = os.environ.get("AGENT_HOME")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._prior is None:
            os.environ.pop("AGENT_HOME", None)
        else:
            os.environ["AGENT_HOME"] = self._prior

    def test_installing_from_a_worktree_still_points_at_the_primary(self):
        os.environ["AGENT_HOME"] = str(self.worktree)
        self.assertEqual(paths.resolve_launcher_source("tools/fleet/fleet.sh"),
                         self.main / "tools/fleet/fleet.sh")

    def test_it_differs_from_resolve_source_exactly_in_that_case(self):
        """`resolve_source` still means "the tree running this install" — other installer
        steps depend on that. Only the launcher pin overrides it."""
        os.environ["AGENT_HOME"] = str(self.worktree)
        self.assertEqual(paths.resolve_source("tools/fleet/fleet.sh"),
                         self.worktree / "tools/fleet/fleet.sh")
        self.assertNotEqual(paths.resolve_launcher_source("tools/fleet/fleet.sh"),
                            paths.resolve_source("tools/fleet/fleet.sh"))

    def test_installing_from_the_primary_is_unchanged(self):
        os.environ["AGENT_HOME"] = str(self.main)
        self.assertEqual(paths.resolve_launcher_source("tools/fleet/fleet.sh"),
                         paths.resolve_source("tools/fleet/fleet.sh"))


if __name__ == "__main__":
    unittest.main()
