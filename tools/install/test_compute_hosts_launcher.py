#!/usr/bin/env python3
"""Focused checks for the shared compute-hosts PATH launcher."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: E402
import distribution  # noqa: E402
import installer  # noqa: E402
import fixture_env  # noqa: E402


class ComputeHostsLauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.source = self.root / "checkout"
        self.wrapper = self.source / "utilities/compute-hosts"
        self.wrapper.parent.mkdir(parents=True)
        repo_wrapper = Path(__file__).resolve().parents[2] / "utilities/compute-hosts"
        self.wrapper.symlink_to(repo_wrapper)
        (self.source / "core").mkdir()
        (self.source / "core/CORE.md").write_text("# core\n", encoding="utf-8")
        self.config = self.root / "config.yaml"
        self.config.write_text("user-owned\n", encoding="utf-8")
        self.managed = mock.patch.object(distribution, "is_managed", return_value=False)
        self.managed.start()
        self.addCleanup(self.managed.stop)

    def _fake_root(self, name):
        root = self.root / name
        (root / "core").mkdir(parents=True)
        (root / "core/CORE.md").write_text("# core\n", encoding="utf-8")
        utilities = root / "utilities"
        utilities.mkdir()
        cli = utilities / "compute-hosts.py"
        cli.write_text(
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'root': os.environ.get('AGENT_HOME'), "
            "'config': os.environ.get('COMPUTE_HOSTS_CONFIG')}))\n",
            encoding="utf-8",
        )
        return root

    # destructive-ok: reason=replace one compute-hosts fixture link before invocation; boundary=one target below self.home in this TemporaryDirectory
    def _invoke(self, root, *args):
        target = self.home / ".local/bin/compute-hosts"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()
        target.symlink_to(self.wrapper)
        env = fixture_env.build_environment(
            self.root,
            root,
            base={"PATH": os.environ.get("PATH", "")},
        )
        env["COMPUTE_HOSTS_CONFIG"] = str(self.config)
        fixture_env.prepare_environment(env)
        return subprocess.run([str(target), *args], env=env, capture_output=True, text=True)

    def test_forwards_exact_arguments_for_runtime_style_pinned_roots(self):
        for name in ("claude", "codex", "opencode"):
            root = self._fake_root(name)
            result = self._invoke(root, "run", "gpu host", "--", "echo", "a b", "--flag")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["root"], str(root))
            self.assertEqual(payload["argv"], ["run", "gpu host", "--", "echo", "a b", "--flag"])
            self.assertEqual(self.config.read_text(encoding="utf-8"), "user-owned\n")

    def test_invalid_root_fails_closed(self):
        result = self._invoke(self.root / "missing", "--help")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active Hearting root is invalid", result.stderr)

    def test_bootstrap_ownership_status_and_exact_uninstall(self):
        with mock.patch.object(bootstrap.paths, "resolve_launcher_source", return_value=self.wrapper):
            created = {row["name"]: row for row in bootstrap.install_launchers(home=self.home)}
            self.assertEqual(created["compute-hosts"]["status"], "created")
            self.assertTrue((self.home / ".local/bin/compute-hosts").is_symlink())
            self.assertEqual(bootstrap.compute_hosts_status(self.home)["status"], "healthy")
            self.assertEqual(bootstrap.install_launchers(home=self.home)[-1]["status"], "unchanged")
            removed = bootstrap.uninstall_compute_hosts(home=self.home)
            self.assertEqual(removed["status"], "removed")
            self.assertFalse((self.home / ".local/bin/compute-hosts").exists())

    def test_foreign_entry_and_inventory_are_preserved(self):
        target = self.home / ".local/bin/compute-hosts"
        target.parent.mkdir(parents=True)
        target.write_text("foreign\n", encoding="utf-8")
        with mock.patch.object(bootstrap.paths, "resolve_launcher_source", return_value=self.wrapper):
            self.assertEqual(bootstrap.compute_hosts_status(self.home)["status"], "foreign-collision")
            result = bootstrap.uninstall_compute_hosts(home=self.home)
            self.assertEqual(result["status"], "preserved-foreign")
        self.assertEqual(target.read_text(encoding="utf-8"), "foreign\n")
        self.assertEqual(self.config.read_text(encoding="utf-8"), "user-owned\n")

    # destructive-ok: reason=simulate current-pointer replacement in a fixture; boundary=two links below this test TemporaryDirectory
    def test_managed_health_requires_lexical_current_target(self):
        current = self.root / "data/hearting/current"
        release = self.root / "data/hearting/releases/old"
        current_cli = current / "utilities/compute-hosts"
        old_cli = release / "utilities/compute-hosts"
        current_cli.parent.mkdir(parents=True)
        old_cli.parent.mkdir(parents=True)
        current_cli.write_text("current\n", encoding="utf-8")
        old_cli.write_text("old\n", encoding="utf-8")
        current_cli.chmod(0o755)
        old_cli.chmod(0o755)
        target = self.home / ".local/bin/compute-hosts"
        target.parent.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "data")}), \
             mock.patch.object(distribution, "is_managed", return_value=True), \
             mock.patch.object(distribution, "current_path", return_value=current):
            target.symlink_to(old_cli)
            self.assertEqual(bootstrap.compute_hosts_status(self.home)["status"], "owned-drift")
            target.unlink()
            target.symlink_to(current_cli)
            self.assertEqual(bootstrap.compute_hosts_status(self.home)["status"], "healthy")
            target.unlink()
            target.symlink_to(self.root / "foreign")
            self.assertEqual(bootstrap.compute_hosts_status(self.home)["status"], "foreign-collision")

    # destructive-ok: reason=simulate removal of one fixture launcher; boundary=harness_target below self.home in this TemporaryDirectory
    def test_launcher_expectation_distinguishes_activation_only_from_install(self):
        self.assertFalse(bootstrap.compute_hosts_expected(self.home))

        harness_source = self.source / "tools/install/harness.sh"
        harness_source.parent.mkdir(parents=True)
        harness_source.write_text("#!/bin/sh\n", encoding="utf-8")
        harness_target = self.home / ".local/bin/harness"
        harness_target.parent.mkdir(parents=True)
        harness_target.symlink_to(harness_source)
        with mock.patch.object(
            bootstrap.paths, "resolve_launcher_source", return_value=harness_source
        ):
            self.assertTrue(bootstrap.compute_hosts_expected(self.home))

        harness_target.unlink()
        with mock.patch.object(distribution, "is_managed", return_value=True):
            self.assertTrue(bootstrap.compute_hosts_expected(self.home))

    def test_linked_update_repairs_compute_hosts_including_dry_run(self):
        args = type("Args", (), {
            "runtimes": ["claude"], "target": None, "scope": "global",
            "plugin": False, "dry_run": True, "reapply": False,
            "version": None, "auto": False,
        })()
        with mock.patch.object(distribution, "is_managed", return_value=False), \
             mock.patch.object(installer.manifest, "check_drift", return_value=[]), \
             mock.patch.object(installer.bootstrap, "install_launchers", return_value=[
                 {"name": "compute-hosts", "status": "planned-migration"}
             ]) as install:
            result = installer.cmd_update(args)
        install.assert_called_once_with(dry_run=True)
        self.assertEqual(result["exit"], installer.EXIT_OK)
        self.assertTrue(any(c["id"] == "bootstrap.launcher.compute-hosts" for c in result["checks"]))

    def test_verify_records_launcher_success_and_failure(self):
        args = type("Args", (), {
            "runtimes": ["claude"], "target": None, "scope": "global",
            "plugin": False,
        })()
        with mock.patch.object(installer.paths, "harness_state_dir", return_value=self.root / "missing"), \
             mock.patch.object(installer.verifier, "run", return_value=[]), \
             mock.patch.object(installer.routing_config, "validate", return_value={"ok": True, "status": "ok", "path": "x"}), \
             mock.patch.object(installer.report_bundle_config, "validate", return_value={"ok": True, "status": "ok", "path": "x"}), \
             mock.patch.object(installer.bootstrap, "compute_hosts_status", return_value={"status": "healthy", "target": str(self.home / "bin")}), \
             mock.patch.object(installer.bootstrap, "compute_hosts_expected", return_value=True), \
             mock.patch.object(installer.subprocess, "run", return_value=type("R", (), {"returncode": 0, "stderr": ""})()):
            result = installer.cmd_verify(args)
        self.assertEqual(result["exit"], installer.EXIT_OK)
        self.assertTrue(any(c["id"] == "bootstrap.launcher.compute-hosts-smoke" and c["ok"] for c in result["checks"]))

        with mock.patch.object(installer.bootstrap, "compute_hosts_status", return_value={"status": "owned-drift", "target": "x"}), \
             mock.patch.object(installer.bootstrap, "compute_hosts_expected", return_value=True), \
             mock.patch.object(installer.paths, "harness_state_dir", return_value=self.root / "missing"), \
             mock.patch.object(installer.verifier, "run", return_value=[]), \
             mock.patch.object(installer.routing_config, "validate", return_value={"ok": True, "status": "ok", "path": "x"}), \
             mock.patch.object(installer.report_bundle_config, "validate", return_value={"ok": True, "status": "ok", "path": "x"}):
            result = installer.cmd_verify(args)
        self.assertEqual(result["exit"], installer.EXIT_VERIFY_FAIL)

    def test_verify_skips_missing_launcher_for_activation_only_channel(self):
        args = type("Args", (), {
            "runtimes": ["claude"], "target": None, "scope": "global",
            "plugin": False,
        })()
        with mock.patch.object(installer.paths, "harness_state_dir", return_value=self.root / "missing"), \
             mock.patch.object(installer.verifier, "run", return_value=[]), \
             mock.patch.object(installer.routing_config, "validate", return_value={"ok": True, "status": "ok", "path": "x"}), \
             mock.patch.object(installer.report_bundle_config, "validate", return_value={"ok": True, "status": "ok", "path": "x"}), \
             mock.patch.object(installer.bootstrap, "compute_hosts_status", return_value={"status": "missing", "target": "x"}), \
             mock.patch.object(installer.bootstrap, "compute_hosts_expected", return_value=False), \
             mock.patch.object(installer.subprocess, "run") as smoke:
            result = installer.cmd_verify(args)
        self.assertEqual(result["exit"], installer.EXIT_OK)
        check = next(c for c in result["checks"] if c["id"] == "bootstrap.launcher.compute-hosts")
        self.assertTrue(check["ok"])
        self.assertIn("SKIP", check["detail"])
        smoke.assert_not_called()

    def test_bare_full_uninstall_removes_owned_but_partial_retains(self):
        base = {"scope": "global", "dry_run": True, "runtimes": None, "target": None}
        with mock.patch("installer.resolve_runtimes", return_value=list(installer.RUNTIMES)), \
             mock.patch.object(installer.runtime_activation, "deactivate", return_value=None), \
             mock.patch.object(installer.codex_launcher, "uninstall", return_value={"status": "not-installed"}), \
             mock.patch.object(installer.manifest, "_manifest_path", return_value=self.root / "none"), \
             mock.patch.object(installer.manifest, "_load_manifest", return_value=None), \
             mock.patch.object(installer.bootstrap, "uninstall_compute_hosts", return_value={"status": "planned-remove"}) as remove:
            result = installer.cmd_uninstall(type("Args", (), base)())
        remove.assert_called_once_with(dry_run=True)
        self.assertTrue(any(c["id"] == "bootstrap.launcher.compute-hosts" for c in result["checks"]))

        with mock.patch("installer.resolve_runtimes", return_value=["claude"]), \
             mock.patch.object(installer.runtime_activation, "deactivate", return_value=None), \
             mock.patch.object(installer.manifest, "_manifest_path", return_value=self.root / "none"), \
             mock.patch.object(installer.manifest, "_load_manifest", return_value=None), \
             mock.patch.object(installer.bootstrap, "uninstall_compute_hosts") as remove:
            installer.cmd_uninstall(type("Args", (), {**base, "runtimes": ["claude"]})())
        remove.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
