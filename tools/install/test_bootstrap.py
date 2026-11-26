#!/usr/bin/env python3
"""Regression tests for installer-owned PATH launchers and the memory store."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
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
        self.assertEqual(rows["fleet-monitor"]["status"], "created")
        self.assertEqual(rows["fleet2"]["status"], "created")
        self.assertEqual(rows["mem"]["status"], "created")

    def test_fleet_names_map_to_the_public_monitor_and_compatibility_launchers(self):
        expected = {
            "fleet": "tools/fleet/fleet.sh",
            "fleet-monitor": "tools/fleet/fleet-monitor.sh",
            "fleet2": "tools/fleet/fleet2.sh",
        }
        self.assertEqual(
            {name: rel for name, rel in bootstrap.LAUNCHERS if name in expected},
            expected,
        )
        rows = {row["name"]: row for row in bootstrap.install_launchers(home=self.home)}
        for name, rel_source in expected.items():
            self.assertEqual(rows[name]["status"], "created")
            self.assertEqual(self._target(name).resolve(), (self.source / rel_source).resolve())

        removed = bootstrap.uninstall_fleet_launchers(home=self.home)
        self.assertEqual({row["name"]: row["status"] for row in removed}, {
            "fleet": "removed", "fleet-monitor": "removed", "fleet2": "removed",
        })
        self.assertTrue(all(not self._target(name).exists() and not self._target(name).is_symlink()
                            for name in expected))

    def test_full_fleet_uninstall_preserves_foreign_and_user_owned_names(self):
        target_dir = self.home / ".local/bin"
        target_dir.mkdir(parents=True)
        foreign = self.root / "foreign/fleet-monitor"
        foreign.parent.mkdir()
        foreign.write_text("user-owned\n", encoding="utf-8")
        self._target("fleet-monitor").symlink_to(foreign)
        self._target("fleet2").write_text("user command\n", encoding="utf-8")

        installed = {row["name"]: row for row in bootstrap.install_launchers(home=self.home)}
        self.assertEqual(installed["fleet"]["status"], "created")
        self.assertEqual(installed["fleet-monitor"]["status"], "skipped-collision")
        self.assertEqual(installed["fleet2"]["status"], "skipped-collision")

        removed = {row["name"]: row for row in bootstrap.uninstall_fleet_launchers(home=self.home)}
        self.assertEqual(removed["fleet"]["status"], "removed")
        self.assertEqual(removed["fleet-monitor"]["status"], "preserved-foreign")
        self.assertEqual(removed["fleet2"]["status"], "preserved-foreign")
        self.assertEqual(self._target("fleet-monitor").resolve(), foreign.resolve())
        self.assertEqual(self._target("fleet2").read_text(encoding="utf-8"), "user command\n")

    def _managed_update_fixture(self, *, release_extra_launchers=()):
        data_home = self.root / "xdg-data"
        state_home = self.root / "xdg-state"
        data_root = data_home / "hearting"
        old_root = data_root / "releases" / "v1.0.0"
        bin_dir = self.home / ".local" / "bin"
        old_root.mkdir(parents=True)
        bin_dir.mkdir(parents=True)

        # This pre-fleet release knew only the two older common tool launchers.
        # The next archive carries the complete launcher inventory.
        old_inventory = {
            "mem": "tools/memory/mem.py",
            "compute-hosts": "utilities/compute-hosts",
        }
        for relative in ("tools/install/harness.sh", *old_inventory.values()):
            source = old_root / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("#!/bin/sh\n", encoding="utf-8")
        (data_root / "current").symlink_to(old_root)
        for name, relative in old_inventory.items():
            (bin_dir / name).symlink_to(old_root / relative)

        foreign = bin_dir / "user-command"
        foreign.write_text("user-owned command\n", encoding="utf-8")
        old_state = {
            "schema": distribution.STATE_SCHEMA,
            "repository": distribution.DEFAULT_REPOSITORY,
            "version": "v1.0.0",
            "archive_sha256": "a" * 64,
            "release_root": str(old_root),
            "runtimes": ["codex"],
            "channel": "stable",
        }
        state_path = state_home / "hearting" / "distribution.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps(old_state), encoding="utf-8")
        old_state_bytes = state_path.read_bytes()

        package = self.root / "new-release" / "hearting"
        for relative in distribution.REQUIRED_RELEASE_FILES:
            path = package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            content = (
                "v2.0.0\n"
                if relative == "RELEASE_VERSION"
                else f"new release: {relative}\n"
            )
            path.write_text(content, encoding="utf-8")
        if release_extra_launchers:
            # The new release declares a command this (older) installer's own
            # TOOL_LAUNCHERS table does not know about.
            table = (*distribution.TOOL_LAUNCHERS, *release_extra_launchers)
            (package / "tools/install/distribution.py").write_text(
                f"TOOL_LAUNCHERS = {table!r}\n", encoding="utf-8"
            )
            for _name, relative in release_extra_launchers:
                source = package / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("#!/bin/sh\n", encoding="utf-8")
        archive = self.root / "hearting-v2.0.0.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(package, arcname="hearting")
        archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        return {
            "data_home": data_home,
            "state_home": state_home,
            "bin_dir": bin_dir,
            "old_root": old_root,
            "old_state_bytes": old_state_bytes,
            "foreign": foreign,
            "archive": archive,
            "archive_digest": archive_digest,
            "old_inventory": old_inventory,
        }

    def _run_managed_update(self, fixture, *, fail_state_commit=False):
        env = {
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(fixture["data_home"]),
            "XDG_STATE_HOME": str(fixture["state_home"]),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
            "HARNESS_BIN_DIR": str(fixture["bin_dir"]),
            "HEARTING_FIXTURE_ROOT": str(self.root),
        }
        release = {
            "version": "v2.0.0",
            "assets": {
                distribution.ARCHIVE_NAME: "https://fixture.invalid/hearting.tar.gz",
                distribution.CHECKSUM_NAME: "https://fixture.invalid/hearting.tar.gz.sha256",
            },
            "metadata_url": "https://fixture.invalid/releases/v2.0.0",
        }
        activate_result = {"runtimes": ["codex"], "session_action": {}}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env))
            stack.enter_context(mock.patch.object(
                distribution, "_release_metadata", return_value=release
            ))
            stack.enter_context(mock.patch.object(
                distribution, "_expected_checksum", return_value=fixture["archive_digest"]
            ))

            def download(_url, destination, expected):
                self.assertEqual(expected, fixture["archive_digest"])
                shutil.copyfile(fixture["archive"], destination)

            stack.enter_context(mock.patch.object(
                distribution, "_download_archive", side_effect=download
            ))
            stack.enter_context(mock.patch.object(
                distribution, "_migration_m0_preflight", return_value={}
            ))
            stack.enter_context(mock.patch.object(
                distribution, "_activate_release", return_value=activate_result
            ))
            stack.enter_context(mock.patch.object(distribution, "_cleanup_releases"))
            stack.enter_context(mock.patch.object(distribution, "_succeed_dispatch_state", return_value=True))
            stack.enter_context(mock.patch.object(distribution, "run_dispatch_state_migration"))
            if fail_state_commit:
                stack.enter_context(mock.patch.object(
                    distribution,
                    "_write_distribution_state",
                    side_effect=distribution.DistributionError("injected state-write failure"),
                ))
            return distribution.update(version="v2.0.0", runtimes=["codex"])

    def test_old_client_update_restores_fleet_launchers_and_preserves_foreign_entry(self):
        fixture = self._managed_update_fixture()
        result = self._run_managed_update(fixture)

        self.assertEqual(result["status"], "updated")
        new_root = Path(result["release_root"])
        self.assertEqual(new_root.name, "v2.0.0")
        for name, relative in distribution.TOOL_LAUNCHERS:
            target = fixture["bin_dir"] / name
            self.assertTrue(target.is_symlink(), name)
            self.assertEqual(target.resolve(), new_root / relative, name)
        for name in ("fleet", "fleet-monitor", "fleet2"):
            self.assertNotIn(name, fixture["old_inventory"])
            self.assertTrue((new_root / dict(distribution.TOOL_LAUNCHERS)[name]).is_file())
        self.assertEqual(
            fixture["foreign"].read_text(encoding="utf-8"),
            "user-owned command\n",
        )

    def test_first_update_links_launchers_only_the_new_release_declares(self):
        extra = (
            ("hearting-new-tool", "tools/new/new-tool.sh"),
            ("hearting-foreign-new", "tools/new/foreign.sh"),
        )
        fixture = self._managed_update_fixture(release_extra_launchers=extra)
        foreign = fixture["bin_dir"] / "hearting-foreign-new"
        foreign.write_text("user-owned command\n", encoding="utf-8")

        result = self._run_managed_update(fixture)

        self.assertEqual(result["status"], "updated")
        new_root = Path(result["release_root"])
        target = fixture["bin_dir"] / "hearting-new-tool"
        self.assertTrue(target.is_symlink())
        self.assertEqual(
            target.readlink(),
            fixture["data_home"] / "hearting/current/tools/new/new-tool.sh",
        )
        self.assertEqual(target.resolve(), new_root / "tools/new/new-tool.sh")
        self.assertEqual(foreign.read_text(encoding="utf-8"), "user-owned command\n")

    def test_failed_update_rolls_back_release_declared_launchers(self):
        extra = (("hearting-new-tool", "tools/new/new-tool.sh"),)
        fixture = self._managed_update_fixture(release_extra_launchers=extra)
        with self.assertRaisesRegex(distribution.DistributionError, "injected state-write failure"):
            self._run_managed_update(fixture, fail_state_commit=True)
        target = fixture["bin_dir"] / "hearting-new-tool"
        self.assertFalse(target.exists() or target.is_symlink())

    def test_release_launcher_table_is_read_without_execution(self):
        root = self.root / "release"
        (root / "tools/install").mkdir(parents=True)
        source = root / "tools/install/distribution.py"
        marker = self.root / "executed"
        source.write_text(
            f"open({str(marker)!r}, 'w').close()\n"
            "TOOL_LAUNCHERS = (('mem', 'tools/memory/mem.py'), ('extra', 'x/extra.sh'))\n",
            encoding="utf-8",
        )
        self.assertEqual(
            distribution._release_extra_tool_launchers(root),
            (("extra", "x/extra.sh"),),
        )
        self.assertFalse(marker.exists())
        for unsafe in ("('../x', 'a')", "('ok', '../a')", "('ok', '/abs')", "'bad'"):
            source.write_text(f"TOOL_LAUNCHERS = ({unsafe},)\n", encoding="utf-8")
            self.assertEqual(distribution._release_extra_tool_launchers(root), (), unsafe)
        empty = self.root / "release-without-table"
        empty.mkdir()
        self.assertEqual(distribution._release_extra_tool_launchers(empty), ())

    def test_old_client_update_rolls_back_launchers_after_state_commit_failure(self):
        fixture = self._managed_update_fixture()
        with self.assertRaisesRegex(distribution.DistributionError, "injected state-write failure"):
            self._run_managed_update(fixture, fail_state_commit=True)

        current = fixture["data_home"] / "hearting" / "current"
        self.assertEqual(current.readlink(), fixture["old_root"])
        for name, relative in fixture["old_inventory"].items():
            target = fixture["bin_dir"] / name
            self.assertTrue(target.is_symlink(), name)
            self.assertEqual(target.resolve(), fixture["old_root"] / relative, name)
        for name in ("fleet", "fleet-monitor", "fleet2"):
            target = fixture["bin_dir"] / name
            self.assertFalse(target.exists() or target.is_symlink(), name)
        self.assertEqual(
            (fixture["state_home"] / "hearting" / "distribution.json").read_bytes(),
            fixture["old_state_bytes"],
        )
        self.assertEqual(
            fixture["foreign"].read_text(encoding="utf-8"),
            "user-owned command\n",
        )


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
