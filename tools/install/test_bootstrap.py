#!/usr/bin/env python3
"""Regression tests for installer-owned PATH launchers and the memory store."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: E402
import installer  # noqa: E402
import distribution  # noqa: E402
import fixture_env  # noqa: E402
import runtime_activation  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


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
        # F-80: launchers resolve through `resolve_launcher_source` (primary checkout),
        # not `resolve_source` (whichever tree is running install).
        self.resolve = mock.patch.object(
            bootstrap.paths,
            "resolve_launcher_source",
            side_effect=lambda relpath: self.source / relpath,
        )
        self.resolve.start()
        self.addCleanup(self.resolve.stop)
        self.managed = mock.patch.object(distribution, "is_managed", return_value=False)
        self.managed.start()
        self.addCleanup(self.managed.stop)

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

    def test_active_arbitrary_checkout_is_a_safe_migration_source(self):
        prior_source = self.root / "mounted/team/hearting"
        rel_source = dict(bootstrap.LAUNCHERS)["fleet"]
        prior_launcher = prior_source / rel_source
        prior_launcher.parent.mkdir(parents=True)
        prior_launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        activation = self.home / ".claude/.harness/activation.json"
        activation.parent.mkdir(parents=True)
        activation.write_text(json.dumps({
            "schema": 2,
            "runtime": "claude",
            "scope": "global",
            "source_root": str(prior_source),
        }), encoding="utf-8")
        target = self._target("fleet")
        target.parent.mkdir(parents=True)
        target.symlink_to(prior_launcher)

        result = {row["name"]: row for row in bootstrap.install_launchers(
            home=self.home
        )}

        self.assertEqual(result["fleet"]["status"], "migrated-legacy")
        self.assertEqual(target.resolve(), (self.source / rel_source).resolve())

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
            plugin=False, dry_run=False, report_bundle_root=None,
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
                installer.report_bundle_config, "ensure", return_value={
                    "status": "preserved", "path": "/tmp/report-bundle.json",
                    "root": "/tmp/reports",
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


class MemoryStoreBootstrapTest(unittest.TestCase):
    """A first install creates the default memory store; nothing else gets one."""

    BODY = "The build uses the shared cache directory under the state root for every runtime."

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # AGENT_HOME during installation is the release, which holds no store.
        self.release = self.root / "release"
        self.release.mkdir()
        env = fixture_env.build_environment(self.root, self.release)
        del env["MEM_STORE"]  # a real machine names no store
        env["MEM_EXCHANGE_AUTO"] = "0"
        fixture_env.prepare_environment(env)
        environment = mock.patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        source = mock.patch.object(
            bootstrap.paths, "resolve_source", side_effect=lambda relpath: REPO / relpath
        )
        source.start()
        self.addCleanup(source.stop)
        self.home = Path(env["HOME"])
        self.store = Path(env["XDG_DATA_HOME"]) / "hearting" / "memory"

    def _mem_add(self):
        """Write one record the way a hook does: no MEM_STORE and no MEM_INIT."""
        return subprocess.run(
            [sys.executable, str(REPO / "tools/memory/mem.py"), "add", "working",
             "decision", self.BODY, "--scope", "global"],
            cwd=self.root, capture_output=True, text=True,
        )

    def _record_count(self):
        con = sqlite3.connect(self.store / "memory.db")
        try:
            return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        finally:
            con.close()

    def _runtime(self, command):
        args = Namespace(
            runtime=["claude"], runtime_command=command, mode="packaged",
            source=str(REPO), scope="global", strict=False, report_bundle_root=None,
        )
        report = {"runtime": "claude", "freshness": "fresh", "next_action": "none"}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.multiple(
                runtime_activation,
                validate_request=mock.DEFAULT,
                capture_runtime_state=mock.Mock(return_value={"_sealed": True}),
                seal_runtime_state=mock.DEFAULT,
                restore_runtime_state=mock.DEFAULT,
                discard_runtime_state=mock.DEFAULT,
                activate=mock.Mock(return_value=dict(report)),
                refresh=mock.Mock(return_value=dict(report)),
            ))
            stack.enter_context(mock.patch.object(
                installer.routing_config, "ensure", return_value={
                    "status": "preserved", "path": "/tmp/config", "enabled": ["claude"],
                }))
            stack.enter_context(mock.patch.object(
                installer.report_bundle_config, "ensure", return_value={
                    "status": "preserved", "path": "/tmp/report-bundle.json",
                    "root": "/tmp/reports",
                }))
            return installer.cmd_runtime(args)

    def test_legacy_install_dry_run_does_not_create_inventory_parent_or_file(self):
        config_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "hearting"
        driver = mock.Mock()
        driver.install.return_value = {"actions": [], "blocked": False}
        args = Namespace(runtimes=["claude"], scope="global", plugin=False,
                         dry_run=True, report_bundle_root=None)
        with mock.patch.object(installer, "get_driver", return_value=driver):
            result = installer.cmd_install(args)
        self.assertEqual(result["exit"], installer.EXIT_OK)
        self.assertIn("compute-hosts-config: would-create", "\n".join(result["lines"]))
        self.assertFalse(config_dir.exists())

    def test_a_first_install_creates_a_store_mem_then_writes_to(self):
        refused = self._mem_add()
        self.assertEqual(refused.returncode, 2, refused.stderr)  # the derived-path guard
        self.assertFalse(self.store.exists())

        result = bootstrap.restore_memory()

        self.assertEqual(result["action"], "initialized", result["detail"])
        self.assertEqual(self._record_count(), 0)
        written = self._mem_add()
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(self._record_count(), 1)
        self.assertEqual(bootstrap.restore_memory()["action"], "skipped")

    def test_a_legacy_store_is_left_alone_and_not_shadowed(self):
        # The installer's own root, then mem.py's fallbacks when AGENT_HOME is unset.
        for legacy in (
            self.release / "memory",
            self.home / "hearting" / "memory",
            self.home / "agent_setting" / "memory",
            self.home / ".claude" / "memory",
        ):
            with self.subTest(legacy=str(legacy)):
                legacy.mkdir(parents=True)
                (legacy / "notes.txt").write_text("user data\n", encoding="utf-8")

                self.assertEqual(bootstrap.restore_memory()["action"], "skipped")

                self.assertEqual(os.listdir(legacy), ["notes.txt"])
                self.assertFalse(self.store.exists())
                legacy.rename(legacy.with_name("memory.moved"))

    def test_a_named_store_is_left_for_mem_to_create(self):
        named = self.root / "named-store"
        with mock.patch.dict(os.environ, {"MEM_STORE": str(named)}):
            self.assertEqual(bootstrap.restore_memory()["action"], "skipped")
        self.assertEqual(bootstrap.restore_memory(named)["action"], "skipped")
        self.assertFalse(named.exists())
        self.assertFalse(self.store.exists())

    def test_an_existing_database_is_kept(self):
        self.store.mkdir(parents=True)
        (self.store / "memory.db").write_bytes(b"existing")
        self.assertEqual(
            bootstrap.restore_memory(),
            {"action": "skipped", "detail": "memory.db already present"},
        )
        self.assertEqual((self.store / "memory.db").read_bytes(), b"existing")

    def test_runtime_activation_creates_the_first_store(self):
        activated = self._runtime("activate")
        self.assertEqual(activated["exit"], installer.EXIT_OK)
        self.assertIn(
            "bootstrap: mem-store -> initialized "
            f"(created an empty memory.db in {self.store})",
            activated["lines"],
        )
        self.assertEqual(self._record_count(), 0)

        refreshed = self._runtime("refresh")
        self.assertIn(
            "bootstrap: mem-store -> skipped (memory.db already present)",
            refreshed["lines"],
        )

    def test_runtime_activation_leaves_a_dump_to_install(self):
        self.store.mkdir(parents=True)
        (self.store / "dump.jsonl").write_text("{}\n", encoding="utf-8")
        activated = self._runtime("activate")
        self.assertEqual(activated["exit"], installer.EXIT_OK)
        self.assertIn(
            "bootstrap: mem-store -> skipped (dump.jsonl present; not imported here)",
            activated["lines"],
        )
        self.assertFalse((self.store / "memory.db").exists())
        self.assertEqual((self.store / "dump.jsonl").read_text(encoding="utf-8"), "{}\n")

    def test_a_scheduled_update_sees_a_named_store(self):
        # The update timer runs activation with its own environment; without the named
        # store it would take the machine for a first install.
        self.assertNotIn("MEM_STORE", distribution._scheduler_environment())
        named = self.root / "named-store"
        with mock.patch.dict(os.environ, {"MEM_STORE": str(named)}):
            self.assertEqual(distribution._scheduler_environment()["MEM_STORE"], str(named))
        with mock.patch.dict(os.environ, {"MEM_STORE": "relative/store"}):
            self.assertNotIn("MEM_STORE", distribution._scheduler_environment())

    def test_a_memory_failure_never_fails_activation(self):
        with mock.patch.object(
            installer.bootstrap, "restore_memory", side_effect=OSError("no python3")
        ):
            result = self._runtime("activate")
        self.assertEqual(result["exit"], installer.EXIT_OK)
        self.assertIn("bootstrap: mem-store -> failed (no python3)", result["lines"])
        self.assertNotIn("rolled_back", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
