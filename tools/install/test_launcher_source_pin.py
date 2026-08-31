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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: E402
import paths  # noqa: E402
import distribution  # noqa: E402


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


class ManagedReleaseMigrationTest(unittest.TestCase):
    """F-80b — a launcher already pointing into a release snapshot self-corrects.

    The collision guard never overwrites a link it does not recognize, which is right for a
    foreign file and wrong for the installer's own release tree: the snapshot path was in no
    `prior` set, so `hearting`/`harness`/`mem` stayed pinned to v2.49.0 while `fleet` had to
    be repointed by hand (user: "그럼 전부 고쳐").
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.bin_dir = self.home / ".local" / "bin"
        self.bin_dir.mkdir(parents=True)
        self.source = self.root / "checkout"
        self.release = (self.home / ".local" / "share" / "hearting"
                        / "releases" / "v1.0.0")
        for tree in (self.source, self.release):
            for _name, rel in bootstrap.LAUNCHERS:
                path = tree / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#!/bin/sh\n", encoding="utf-8")
        self.resolve = mock.patch.object(
            bootstrap.paths, "resolve_launcher_source",
            side_effect=lambda rel: self.source / rel,
        )
        self.resolve.start()
        self.addCleanup(self.resolve.stop)
        self.managed = mock.patch.object(distribution, "is_managed", return_value=False)
        self.managed.start()
        self.addCleanup(self.managed.stop)

    def _link_all_to_release(self):
        for name, rel in bootstrap.LAUNCHERS:
            (self.bin_dir / name).symlink_to(self.release / rel)

    def test_release_snapshot_links_migrate_to_the_checkout(self):
        self._link_all_to_release()
        rows = {r["name"]: r for r in bootstrap.install_launchers(home=self.home)}
        for name, rel in bootstrap.LAUNCHERS:
            self.assertEqual(rows[name]["status"], "migrated-legacy", name)
            self.assertEqual((self.bin_dir / name).resolve(), (self.source / rel).resolve())

    def test_the_current_symlink_tree_is_recognized_too(self):
        """Installs point `current` at the live release; a launcher may name either."""
        current = self.home / ".local" / "share" / "hearting" / "current"
        current.symlink_to(self.release)
        for name, rel in bootstrap.LAUNCHERS:
            (self.bin_dir / name).symlink_to(current / rel)
        rows = {r["name"]: r["status"] for r in bootstrap.install_launchers(home=self.home)}
        self.assertEqual(set(rows.values()), {"migrated-legacy"})

    # destructive-ok: reason=simulate removal of one prior fixture launcher link; boundary=target below this test TemporaryDirectory
    def test_prior_runtime_bundle_links_migrate_after_activation_advances(self):
        """The old bundle is no longer named by activation.json after a refresh."""
        rel = dict(bootstrap.LAUNCHERS)["fleet"]
        bundle_dirs = (
            self.home / ".claude" / ".harness" / "bundles",
            self.home / ".codex" / ".harness" / "bundles",
            self.home / ".config" / "opencode" / ".harness" / "bundles",
        )
        target = self.bin_dir / "fleet"
        for index, bundles in enumerate(bundle_dirs):
            with self.subTest(bundles=bundles):
                prior = bundles / f"prior-{index}" / "source" / rel
                prior.parent.mkdir(parents=True)
                prior.write_text("#!/bin/sh\n", encoding="utf-8")
                if target.is_symlink():
                    target.unlink()
                target.symlink_to(prior)

                rows = {r["name"]: r for r in bootstrap.install_launchers(home=self.home)}

                self.assertEqual(rows["fleet"]["status"], "migrated-legacy")
                self.assertEqual(target.resolve(), (self.source / rel).resolve())

    def test_symlinked_runtime_bundle_entry_is_still_foreign(self):
        rel = dict(bootstrap.LAUNCHERS)["fleet"]
        foreign = self.root / "foreign-bundle" / "source" / rel
        foreign.parent.mkdir(parents=True)
        foreign.write_text("#!/bin/sh\n", encoding="utf-8")
        bundles = self.home / ".codex" / ".harness" / "bundles"
        bundles.mkdir(parents=True)
        (bundles / "lookalike").symlink_to(self.root / "foreign-bundle")
        target = self.bin_dir / "fleet"
        target.symlink_to(foreign)

        rows = {r["name"]: r["status"] for r in bootstrap.install_launchers(home=self.home)}

        self.assertEqual(rows["fleet"], "skipped-collision")
        self.assertEqual(target.resolve(), foreign.resolve())

    def test_a_foreign_link_is_still_preserved(self):
        """The guard's whole purpose survives: only installer-owned trees are re-pointed."""
        foreign = self.root / "elsewhere" / "fleet.sh"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("#!/bin/sh\n", encoding="utf-8")
        (self.bin_dir / "fleet").symlink_to(foreign)
        rows = {r["name"]: r["status"] for r in bootstrap.install_launchers(home=self.home)}
        self.assertEqual(rows["fleet"], "skipped-collision")
        self.assertEqual((self.bin_dir / "fleet").resolve(), foreign.resolve())

    def test_an_already_correct_link_is_left_alone(self):
        for name, rel in bootstrap.LAUNCHERS:
            (self.bin_dir / name).symlink_to(self.source / rel)
        rows = {r["name"]: r["status"] for r in bootstrap.install_launchers(home=self.home)}
        self.assertEqual(set(rows.values()), {"unchanged"})


if __name__ == "__main__":
    unittest.main()
