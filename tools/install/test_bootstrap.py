#!/usr/bin/env python3
"""Regression tests for installer-owned PATH launcher migration."""

import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: E402
import installer  # noqa: E402


class LauncherMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.source = self.root / "nas/hearting"
        for _name, rel_source in bootstrap.LAUNCHERS:
            path = self.source / rel_source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(0o755)
        self.resolve = mock.patch.object(
            bootstrap.paths,
            "resolve_source",
            side_effect=lambda relpath: self.source / relpath,
        )
        self.resolve.start()
        self.addCleanup(self.resolve.stop)

    def _target(self, name):
        return self.home / ".local/bin" / name

    def _legacy_source(self, name, checkout="agent_setting"):
        rel_source = dict(bootstrap.LAUNCHERS)[name]
        return self.home / checkout / rel_source

    def test_dry_run_and_install_migrate_exact_legacy_launchers(self):
        self._target("fleet").parent.mkdir(parents=True)
        for name, _rel_source in bootstrap.LAUNCHERS:
            self._target(name).symlink_to(self._legacy_source(name))

        dry_run = {row["name"]: row for row in bootstrap.install_launchers(
            home=self.home, dry_run=True
        )}
        self.assertEqual(
            {row["status"] for row in dry_run.values()}, {"planned-migration"}
        )
        self.assertEqual(
            Path(os.readlink(self._target("fleet"))), self._legacy_source("fleet")
        )

        installed = {row["name"]: row for row in bootstrap.install_launchers(
            home=self.home
        )}
        self.assertEqual(
            {row["status"] for row in installed.values()}, {"migrated-legacy"}
        )
        for name, rel_source in bootstrap.LAUNCHERS:
            self.assertEqual(
                self._target(name).resolve(), (self.source / rel_source).resolve()
            )

        repeated = bootstrap.install_launchers(home=self.home)
        self.assertEqual({row["status"] for row in repeated}, {"unchanged"})

    def test_prior_canonical_checkout_is_also_a_safe_migration_source(self):
        target = self._target("fleet")
        target.parent.mkdir(parents=True)
        target.symlink_to(self._legacy_source("fleet", checkout="hearting"))
        result = {row["name"]: row for row in bootstrap.install_launchers(home=self.home)}
        self.assertEqual(result["fleet"]["status"], "migrated-legacy")
        self.assertEqual(
            target.resolve(), (self.source / dict(bootstrap.LAUNCHERS)["fleet"]).resolve()
        )

    def test_foreign_entries_are_preserved_while_missing_launchers_are_created(self):
        fleet = self._target("fleet")
        fleet.parent.mkdir(parents=True)
        fleet.write_text("user-owned\n", encoding="utf-8")
        hearting = self._target("hearting")
        foreign = self.home / "somewhere-else/hearting"
        hearting.symlink_to(foreign)

        rows = {row["name"]: row for row in bootstrap.install_launchers(home=self.home)}
        self.assertEqual(rows["fleet"]["status"], "skipped-collision")
        self.assertEqual(rows["hearting"]["status"], "skipped-collision")
        self.assertEqual(fleet.read_text(encoding="utf-8"), "user-owned\n")
        self.assertEqual(Path(os.readlink(hearting)), foreign)
        self.assertEqual(rows["harness"]["status"], "created")
        self.assertEqual(rows["mem"]["status"], "created")


class InstallerCollisionExitTest(unittest.TestCase):
    def test_install_returns_failure_when_a_foreign_launcher_is_preserved(self):
        args = SimpleNamespace(
            runtimes=["claude"], target=None, scope="global",
            plugin=False, dry_run=False,
        )
        driver = mock.Mock()
        driver.install.return_value = {"actions": [], "blocked": False}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                installer, "get_driver", return_value=driver
            ))
            stack.enter_context(mock.patch.object(
                installer.routing_config, "ensure", return_value={
                "status": "preserved", "path": "/tmp/config",
                "enabled": ["claude"],
            }))
            stack.enter_context(mock.patch.object(
                installer.bootstrap, "restore_memory", return_value={
                "action": "skipped", "detail": "present",
            }))
            stack.enter_context(mock.patch.object(
                installer.bootstrap, "install_launchers", return_value=[{
                "name": "fleet", "target": "/tmp/fleet", "source": "/src/fleet",
                "status": "skipped-collision", "detail": "foreign",
            }]))
            result = installer.cmd_install(args)
        self.assertEqual(result["exit"], installer.EXIT_FAIL)
        self.assertFalse(result["checks"][-1]["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
