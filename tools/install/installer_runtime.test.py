#!/usr/bin/env python3
"""Installer-level fault injection at the Codex launcher retirement boundary.

`runtime_activation.test.py` and `runtime-activation.test.sh` already prove
`runtime_activation.py`'s own multi-runtime rollback and the end-to-end
protected-ingress lifecycle against a full adapter fixture. This module
isolates `installer.py`'s `cmd_runtime` rollback wiring instead: it stubs the
runtime-projection collaborator (`runtime_activation.capture_runtime_state` /
`activate` / `refresh` / `restore_runtime_state` / `discard_runtime_state`) so
a failure can be injected precisely after legacy launcher removal
(`HARNESS_INSTALLER_FAIL_AFTER_LAUNCHER=1`) and asserts the launcher state is
restored to its exact pre-transaction bytes.
"""
import os
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import installer  # noqa: E402
import bootstrap  # noqa: E402
import herdr_integration  # noqa: E402
import codex_launcher  # noqa: E402
import runtime_activation  # noqa: E402
import fixture_env  # noqa: E402
import manifest  # noqa: E402
import paths  # noqa: E402


def _write_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@contextmanager
def _stubbed_runtime_projection():
    fake_report = {"runtime": "codex", "freshness": "fresh", "next_action": "none"}
    with mock.patch.multiple(
        runtime_activation,
        validate_request=mock.DEFAULT,
        capture_runtime_state=mock.DEFAULT,
        seal_runtime_state=mock.DEFAULT,
        restore_runtime_state=mock.DEFAULT,
        discard_runtime_state=mock.DEFAULT,
        activate=mock.DEFAULT,
        refresh=mock.DEFAULT,
    ) as mocks:
        mocks["capture_runtime_state"].return_value = {
            "fake": "snapshot",
            "_sealed": True,
        }
        mocks["activate"].return_value = dict(fake_report)
        mocks["refresh"].return_value = dict(fake_report)
        yield mocks


class PeerReconnectTest(unittest.TestCase):
    def test_callback_uses_activated_root_and_keeps_existing_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            utility = root / "utilities" / "peer-steward.py"
            utility.parent.mkdir()
            utility.touch()
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": "/fixture/jobs.log"}), \
                 mock.patch.object(runtime_activation.subprocess, "Popen") as launch:
                self.assertTrue(runtime_activation.reconnect_peer_obligations(root))
                args, kwargs = launch.call_args
                self.assertEqual(args[0], [sys.executable, str(utility), "__ensure-obligations",
                                           "--lifecycle-root", str(root)])
                self.assertEqual(kwargs["env"]["AGENT_HOME"], str(root))
                self.assertEqual(kwargs["env"]["AGENT_DISPATCH_JOBS"], "/fixture/jobs.log")


class LauncherCommitBoundaryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.vendor_bin = root / "vendor-bin"
        self.home.mkdir()
        self.vendor_bin.mkdir()
        self.codex_home = self.home / ".codex"
        self.vendor_codex = self.vendor_bin / "codex"
        _write_executable(self.vendor_codex)
        # Synthetic user-owned surfaces the plan names explicitly
        # (credentials, sessions, config): no launcher install, refresh,
        # uninstall, or rollback transaction in this module may ever touch
        # these bytes.
        self.codex_home.mkdir(parents=True)
        (self.codex_home / "sessions").mkdir()
        self.protected_files = {
            self.codex_home / "auth.json": "protected-credential\n",
            self.codex_home / "sessions" / "existing-session.jsonl": "protected-session\n",
            self.codex_home / "config.toml": "protected-config\n",
        }
        for path, content in self.protected_files.items():
            path.write_text(content, encoding="utf-8")
        env = fixture_env.build_environment(
            root,
            Path(__file__).resolve().parents[2],
            base={"PATH": os.environ.get("PATH", "")},
        )
        env.update({
            "CODEX_HOME": str(self.codex_home),
            "PATH": str(self.vendor_bin) + os.pathsep + env.get("PATH", ""),
            # An unsupported shell name makes `_profile_path()` return None
            # unconditionally, so no launcher/uninstall transaction in this
            # module ever resolves a profile path from the ambient
            # SHELL/ZDOTDIR/XDG_CONFIG_HOME of the process running the test
            # (which would otherwise point outside the private fixture home
            # at a real, unrelated shell startup file). Shell-specific
            # profile-mapping behavior is exhaustively covered by
            # `codex_launcher.test.py`, not this module.
            "SHELL": "/bin/installer-runtime-test-unsupported-shell",
        })
        env.pop("HARNESS_BIN_DIR", None)
        fixture_env.prepare_environment(env)
        self._env_patch = mock.patch.dict(os.environ, env, clear=True)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        os.environ.pop("HARNESS_INSTALLER_FAIL_AFTER_LAUNCHER", None)
        self.addCleanup(os.environ.pop, "HARNESS_INSTALLER_FAIL_AFTER_LAUNCHER", None)
        os.environ.pop("HARNESS_INSTALLER_FAIL_AFTER_UNINSTALL_LAUNCHER", None)
        self.addCleanup(
            os.environ.pop, "HARNESS_INSTALLER_FAIL_AFTER_UNINSTALL_LAUNCHER", None
        )

    def _assert_protected_surfaces_untouched(self):
        for path, content in self.protected_files.items():
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                content,
                f"protected user-owned surface was mutated: {path}",
            )

    def _install_legacy_launcher(self):
        result = codex_launcher.install(dry_run=False)
        self.assertEqual(result["status"], "created")

    def _args(self, command):
        return Namespace(
            runtime=["codex"],
            runtime_command=command,
            mode="linked",
            source=str(Path(self._tmp.name) / "source"),
            scope="global",
            strict=False,
            report_bundle_root=None,
        )

    def _uninstall_args(self):
        return Namespace(
            runtimes=["codex"],
            target="codex",
            scope="global",
            dry_run=False,
        )

    def test_packaged_distribution_activation_reaches_real_inventory_ensure(self):
        # Keep the real _activate_release argv construction and parser/cmd_runtime
        # consumer. Only runtime projection work is stubbed; ensure() is real.
        real_run = installer.distribution.subprocess.run
        def run_packaged(argv, *args, **kwargs):
            if not (isinstance(argv, list) and len(argv) > 1 and argv[0] == "sh"
                    and argv[1].endswith("tools/install/harness.sh")):
                return real_run(argv, *args, **kwargs)
            env = kwargs["env"]
            args = installer.build_parser().parse_args(argv[2:])
            with mock.patch.dict(os.environ, env, clear=True), _stubbed_runtime_projection():
                payload = installer.cmd_runtime(args)
            return type("Completed", (), {
                "returncode": payload["exit"], "stdout": json.dumps(payload), "stderr": ""
            })()

        with mock.patch.object(installer.distribution.subprocess, "run", side_effect=run_packaged):
            first = installer.distribution._activate_release(Path(__file__).resolve().parents[2], ["codex"])
        self.assertEqual(first["runtimes"], ["codex"])
        self.assertEqual(first["report"]["compute_hosts_config"]["status"], "created")
        inventory = Path(os.environ["XDG_CONFIG_HOME"]) / "hearting/compute-hosts.yaml"
        self.assertTrue(inventory.is_file())
        self.assertEqual(inventory.stat().st_mode & 0o777, 0o600)
        original = inventory.read_bytes()
        stamp = inventory.stat().st_mtime_ns
        inventory.write_text("user-owned inventory\n", encoding="utf-8")
        os.utime(inventory, ns=(stamp, stamp))
        with _stubbed_runtime_projection():
            args = installer.build_parser().parse_args([
                "runtime", "refresh", "--runtime", "codex", "--json",
            ])
            result = installer.cmd_runtime(args)
        self.assertEqual(result["exit"], installer.EXIT_OK)
        self.assertEqual(result["compute_hosts_config"]["status"], "preserved")
        self.assertEqual(inventory.read_text(encoding="utf-8"), "user-owned inventory\n")
        self.assertEqual(inventory.stat().st_mtime_ns, stamp)
        override = Path(self._tmp.name) / "custom-config" / "inventory.yaml"
        with mock.patch.dict(os.environ, {"COMPUTE_HOSTS_CONFIG": str(override)}), \
             _stubbed_runtime_projection():
            args = installer.build_parser().parse_args([
                "runtime", "refresh", "--runtime", "codex", "--json",
            ])
            result = installer.cmd_runtime(args)
        self.assertEqual(result["compute_hosts_config"]["status"], "created")
        self.assertTrue(override.is_file())
        self.assertEqual(inventory.read_text(encoding="utf-8"), "user-owned inventory\n")

    def test_real_packaged_harness_activation_creates_inventory(self):
        root = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        env["HARNESS_SCHEDULER_NO_ACTIVATE"] = "1"
        codex_config = Path(env["CODEX_HOME"]) / "config.toml"
        codex_config.write_text("", encoding="utf-8")
        result = subprocess.run(
            ["sh", str(root / "tools/install/harness.sh"), "runtime", "activate",
             "--runtime", "codex", "--mode", "packaged", "--source", str(root), "--json"],
            env=env, text=True, capture_output=True, timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["compute_hosts_config"]["status"], "created")
        self.assertTrue(any("compute-hosts-config: created" in line
                            for line in payload.get("lines", [])), payload)
        inventory = Path(env["XDG_CONFIG_HOME"]) / "hearting/compute-hosts.yaml"
        self.assertTrue(inventory.is_file())
        self.assertEqual(inventory.stat().st_mode & 0o777, 0o600)

    def test_refresh_keeps_shared_history_key_in_common_ensure_path(self):
        target = self.home / ".local/bin/hearting"
        target.parent.mkdir(parents=True)
        source = self.home / "hearting-source"
        _write_executable(source)
        target.symlink_to(source)
        launchers = [{"name": "hearting", "target": str(target),
                      "status": "unchanged"}]
        with _stubbed_runtime_projection(), \
             mock.patch.object(bootstrap, "install_launchers", return_value=launchers) as install, \
             mock.patch.object(herdr_integration, "ensure_history_key",
                               return_value={"status": "unchanged", "changed": False}) as ensure:
            result = installer.cmd_runtime(self._args("refresh"))
        self.assertEqual(result["exit"], installer.EXIT_OK)
        install.assert_called_once_with(dry_run=True)
        ensure.assert_called_once_with(str(target), dry_run=False)

    def test_activation_reconnects_once_and_rollback_never_reconnects(self):
        with _stubbed_runtime_projection() as mocks, \
             mock.patch.object(runtime_activation, "reconnect_peer_obligations") as reconnect:
            mocks["activate"].return_value["active_root"] = "/fixture/release"
            result = installer.cmd_runtime(self._args("activate"))
            self.assertEqual(result["exit"], installer.EXIT_OK)
            reconnect.assert_called_once_with("/fixture/release")
        os.environ["HARNESS_INSTALLER_FAIL_AFTER_LAUNCHER"] = "1"
        with _stubbed_runtime_projection() as mocks, \
             mock.patch.object(runtime_activation, "reconnect_peer_obligations") as reconnect:
            mocks["refresh"].return_value["active_root"] = "/fixture/release"
            result = installer.cmd_runtime(self._args("refresh"))
            self.assertEqual(result["exit"], installer.EXIT_BLOCKED)
            reconnect.assert_not_called()

    def test_injected_failure_after_launcher_retirement_restores_exact_prior_state(self):
        self._install_legacy_launcher()
        with _stubbed_runtime_projection():
            first = installer.cmd_runtime(self._args("activate"))
        # Activation itself retires the pre-existing launcher, so seed it
        # again to exercise rollback at the retirement boundary.
        self._install_legacy_launcher()
        self.assertEqual(first["exit"], installer.EXIT_OK)

        state_path = codex_launcher.state_path(self.codex_home)
        self.assertTrue(state_path.exists())
        wrapper_target = codex_launcher.wrapper_path(codex_launcher.default_bin_dir())
        self.assertTrue(wrapper_target.exists())
        committed_state_bytes = state_path.read_bytes()
        committed_wrapper_bytes = wrapper_target.read_bytes()

        os.environ["HARNESS_INSTALLER_FAIL_AFTER_LAUNCHER"] = "1"
        with _stubbed_runtime_projection() as mocks:
            second = installer.cmd_runtime(self._args("refresh"))
            restore_runtime_state = mocks["restore_runtime_state"]

        self.assertEqual(second["exit"], installer.EXIT_BLOCKED)
        self.assertIn("injected failure after launcher commit boundary", second["lines"][-1])
        restore_runtime_state.assert_called_once_with(
            {"fake": "snapshot", "_sealed": True}
        )

        # The installer's rollback restores the legacy wrapper and state to
        # their exact pre-refresh bytes.
        self.assertEqual(state_path.read_bytes(), committed_state_bytes)
        self.assertEqual(wrapper_target.read_bytes(), committed_wrapper_bytes)
        status = codex_launcher.status(codex_home=self.codex_home)
        self.assertTrue(status["installed"])
        self.assertEqual(status["real_command"], str(self.vendor_codex))
        self._assert_protected_surfaces_untouched()

    def test_no_injection_leaves_refresh_committed(self):
        self._install_legacy_launcher()
        for command in ("activate", "refresh"):
            with _stubbed_runtime_projection() as mocks:
                result = installer.cmd_runtime(self._args(command))
                discard_runtime_state = mocks["discard_runtime_state"]
                restore_runtime_state = mocks["restore_runtime_state"]
            self.assertEqual(result["exit"], installer.EXIT_OK)
            discard_runtime_state.assert_called_once_with(
                {"fake": "snapshot", "_sealed": True}
            )
            restore_runtime_state.assert_not_called()
        status = codex_launcher.status(codex_home=self.codex_home)
        self.assertFalse(status["installed"])
        self._assert_protected_surfaces_untouched()

    def test_reinstall_after_activate_is_idempotent_at_installer_level(self):
        self._install_legacy_launcher()
        with _stubbed_runtime_projection():
            first = installer.cmd_runtime(self._args("activate"))
        self.assertEqual(first["exit"], installer.EXIT_OK)
        state_path = codex_launcher.state_path(self.codex_home)
        wrapper_target = codex_launcher.wrapper_path(codex_launcher.default_bin_dir())
        self.assertFalse(state_path.exists())
        self.assertFalse(wrapper_target.exists())

        # Reinstall: activate again over an already-activated runtime.
        with _stubbed_runtime_projection() as mocks:
            second = installer.cmd_runtime(self._args("activate"))
            mocks["restore_runtime_state"].assert_not_called()

        self.assertEqual(second["exit"], installer.EXIT_OK)
        status = codex_launcher.status(codex_home=self.codex_home)
        self.assertFalse(status["installed"])
        self._assert_protected_surfaces_untouched()

    def test_full_uninstall_removes_managed_launcher(self):
        with _stubbed_runtime_projection():
            first = installer.cmd_runtime(self._args("activate"))
        self.assertEqual(first["exit"], installer.EXIT_OK)
        self._install_legacy_launcher()
        wrapper_target = codex_launcher.wrapper_path(codex_launcher.default_bin_dir())
        self.assertTrue(wrapper_target.exists())

        result = installer.cmd_uninstall(self._uninstall_args())
        self.assertEqual(result["exit"], installer.EXIT_OK)
        self.assertFalse(wrapper_target.exists())
        status = codex_launcher.status(codex_home=self.codex_home)
        self.assertFalse(status["installed"])
        self._assert_protected_surfaces_untouched()

    def test_partial_uninstall_fault_injection_restores_exact_launcher_state(self):
        with _stubbed_runtime_projection():
            first = installer.cmd_runtime(self._args("activate"))
        self.assertEqual(first["exit"], installer.EXIT_OK)
        self._install_legacy_launcher()

        state_path = codex_launcher.state_path(self.codex_home)
        wrapper_target = codex_launcher.wrapper_path(codex_launcher.default_bin_dir())
        committed_state_bytes = state_path.read_bytes()
        committed_wrapper_bytes = wrapper_target.read_bytes()

        os.environ["HARNESS_INSTALLER_FAIL_AFTER_UNINSTALL_LAUNCHER"] = "1"
        result = installer.cmd_uninstall(self._uninstall_args())

        self.assertEqual(result["exit"], installer.EXIT_BLOCKED)
        self.assertIn(
            "injected failure after uninstall launcher commit boundary",
            result["lines"][-1],
        )

        # The launcher's own uninstall() call ran and committed (removed the
        # wrapper); the installer's rollback must have restored it — a
        # partial uninstall must never leave the protected ingress removed
        # while the rest of the runtime's uninstall never ran.
        self.assertTrue(wrapper_target.exists())
        self.assertEqual(state_path.read_bytes(), committed_state_bytes)
        self.assertEqual(wrapper_target.read_bytes(), committed_wrapper_bytes)
        status = codex_launcher.status(codex_home=self.codex_home)
        self.assertTrue(status["installed"])
        self.assertEqual(status["real_command"], str(self.vendor_codex))
        self._assert_protected_surfaces_untouched()


class StatusVersionSkewTest(unittest.TestCase):
    def _status_args(self, runtimes):
        return Namespace(runtimes=runtimes, target=None, scope="user", plugin=False)

    def _fake_activation(self, source_root):
        return {"freshness": "fresh", "mode": "packaged", "source_root": source_root}

    def _fake_driver(self, version):
        driver = mock.Mock()
        driver.status.return_value = {"channel": "dev", "version": version, "drift_count": 0}
        return driver

    def test_status_reports_skew_when_runtimes_disagree(self):
        args = self._status_args(["claude", "codex"])
        drivers = {"claude": self._fake_driver("1.0.0"), "codex": self._fake_driver("2.0.0")}
        with mock.patch.object(runtime_activation, "status",
                               side_effect=lambda rt, scope: self._fake_activation(f"/src/{rt}")), \
             mock.patch.object(installer, "get_driver", side_effect=lambda rt: drivers[rt]):
            result = installer.cmd_status(args)
        self.assertTrue(any(line.startswith("version-skew:") for line in result["lines"]))
        self.assertTrue(any(line.startswith("next:") for line in result["lines"]))
        skew_checks = [c for c in result["checks"] if c["id"] == "runtime.version-skew"]
        self.assertEqual(len(skew_checks), 1)
        self.assertFalse(skew_checks[0]["ok"])
        self.assertEqual(result["version_skew"]["versions"], ["1.0.0", "2.0.0"])

    def test_status_exit_code_is_unchanged_under_skew(self):
        args = self._status_args(["claude", "codex"])
        drivers = {"claude": self._fake_driver("1.0.0"), "codex": self._fake_driver("2.0.0")}
        with mock.patch.object(runtime_activation, "status",
                               side_effect=lambda rt, scope: self._fake_activation(f"/src/{rt}")), \
             mock.patch.object(installer, "get_driver", side_effect=lambda rt: drivers[rt]):
            result = installer.cmd_status(args)
        self.assertEqual(result["exit"], installer.EXIT_OK)

    def test_status_is_silent_when_versions_agree(self):
        args = self._status_args(["claude", "codex"])
        drivers = {"claude": self._fake_driver("1.0.0"), "codex": self._fake_driver("1.0.0")}
        with mock.patch.object(runtime_activation, "status",
                               side_effect=lambda rt, scope: self._fake_activation(f"/src/{rt}")), \
             mock.patch.object(installer, "get_driver", side_effect=lambda rt: drivers[rt]):
            result = installer.cmd_status(args)
        self.assertFalse(any(line.startswith("version-skew:") for line in result["lines"]))
        self.assertNotIn("version_skew", result)

    def test_status_does_not_call_surface_skew(self):
        args = self._status_args(["claude", "codex"])
        drivers = {"claude": self._fake_driver("1.0.0"), "codex": self._fake_driver("1.0.0")}
        with mock.patch.object(runtime_activation, "status",
                               side_effect=lambda rt, scope: self._fake_activation(f"/src/{rt}")), \
             mock.patch.object(installer, "get_driver", side_effect=lambda rt: drivers[rt]), \
             mock.patch.object(runtime_activation, "surface_skew") as fake_skew:
            installer.cmd_status(args)
            fake_skew.assert_not_called()

    def _activation_unless(self, missing):
        def activation(rt, scope):
            if rt == missing:
                return {"freshness": "missing", "mode": None, "source_root": None}
            return self._fake_activation(f"/src/{rt}")
        return activation

    def test_status_ignores_a_runtime_that_was_never_installed(self):
        args = self._status_args(["claude", "codex", "opencode"])
        drivers = {"claude": self._fake_driver("1.0.0"), "codex": self._fake_driver("1.0.0"),
                   "opencode": self._fake_driver("not-installed")}
        with mock.patch.object(runtime_activation, "status",
                               side_effect=self._activation_unless("opencode")), \
             mock.patch.object(installer, "get_driver", side_effect=lambda rt: drivers[rt]):
            result = installer.cmd_status(args)
        self.assertIn("opencode: channel=dev version=not-installed drift=0 projection=not-activated",
                      result["lines"])
        self.assertFalse(any(line.startswith("version-skew:") for line in result["lines"]))
        self.assertNotIn("version_skew", result)

    def test_status_still_compares_a_copy_install_without_activation(self):
        args = self._status_args(["claude", "opencode"])
        drivers = {"claude": self._fake_driver("1.0.0"), "opencode": self._fake_driver("0.9.0")}
        with mock.patch.object(runtime_activation, "status",
                               side_effect=self._activation_unless("opencode")), \
             mock.patch.object(installer, "get_driver", side_effect=lambda rt: drivers[rt]):
            result = installer.cmd_status(args)
        self.assertEqual(result["version_skew"]["versions"], ["0.9.0", "1.0.0"])


class InstalledRuntimeTargetsTest(unittest.TestCase):
    """No target, or ``all``, names the runtimes installed here, not every known one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        environment = fixture_env.patched_environment(
            self._tmp.name,
            Path(__file__).resolve().parents[2],
            base={"PATH": os.environ.get("PATH", "")},
        )
        environment.__enter__()
        self.addCleanup(environment.__exit__, None, None, None)

    def _write_record(self, record):
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{}", encoding="utf-8")

    def _activate(self, runtime):
        self._write_record(paths.harness_state_dir(runtime, "global") / "activation.json")

    def _copy_install(self, runtime):
        self._write_record(manifest._manifest_path(runtime, "global"))

    def _args(self, target=None, runtimes=None):
        return Namespace(runtimes=runtimes, target=target, scope="global")

    def test_verify_default_names_the_installed_runtimes(self):
        self._activate("claude")
        self._copy_install("codex")
        for target in (None, "all"):
            self.assertEqual(
                installer.resolve_runtimes(self._args(target), installed_only=True),
                ["claude", "codex"],
            )

    def test_verify_default_names_every_runtime_when_none_is_installed(self):
        self.assertEqual(installer.resolve_runtimes(self._args(), installed_only=True),
                         list(installer.RUNTIMES))

    def test_named_runtime_is_kept_even_when_not_installed(self):
        self._activate("claude")
        self.assertEqual(installer.resolve_runtimes(self._args("opencode"), installed_only=True),
                         ["opencode"])
        self.assertEqual(
            installer.resolve_runtimes(self._args(runtimes=["opencode"]), installed_only=True),
            ["opencode"],
        )

    def test_other_commands_keep_every_runtime_by_default(self):
        self._activate("claude")
        self.assertEqual(installer.resolve_runtimes(self._args()), list(installer.RUNTIMES))

    def test_verify_does_not_check_a_runtime_that_was_never_installed(self):
        self._activate("claude")
        self._activate("codex")
        args = installer.build_parser().parse_args(["verify"])
        report = {"ok": True, "status": "ok", "freshness": "fresh", "next_action": "none"}
        with mock.patch.object(runtime_activation, "doctor", return_value=report) as doctor, \
             mock.patch.object(installer.verifier, "run") as legacy_checks, \
             mock.patch.object(installer.bootstrap, "compute_hosts_status",
                               return_value={"status": "missing", "target": "x"}), \
             mock.patch.object(installer.bootstrap, "compute_hosts_expected", return_value=False):
            result = installer.cmd_verify(args)
        self.assertEqual(result["runtime"], ["claude", "codex"])
        self.assertEqual([call.args[0] for call in doctor.call_args_list], ["claude", "codex"])
        legacy_checks.assert_not_called()
        self.assertFalse(any(check["id"].startswith("opencode.") for check in result["checks"]))

    def test_runtime_all_names_the_activated_runtimes(self):
        self._activate("claude")
        self._activate("codex")
        self._copy_install("opencode")  # a copy install has no activation to refresh
        for command in ("status", "doctor", "refresh"):
            self.assertEqual(installer._runtime_targets("all", command, "global"),
                             ["claude", "codex"])
            self.assertEqual(installer._runtime_targets(["all"], command, "global"),
                             ["claude", "codex"])
        self.assertEqual(installer._runtime_targets(["all"], "activate", "global"),
                         list(runtime_activation.RUNTIMES))

    def test_runtime_all_keeps_a_runtime_named_beside_it(self):
        self._activate("claude")
        self.assertEqual(installer._runtime_targets(["all", "opencode"], "refresh", "global"),
                         ["claude", "opencode"])

    def test_runtime_all_names_every_runtime_when_none_is_activated(self):
        self.assertEqual(installer._runtime_targets(["all"], "refresh", "global"),
                         list(runtime_activation.RUNTIMES))


class UpdateSkipHintTest(unittest.TestCase):
    def _update_args(self):
        return Namespace(dry_run=False, scope="global", plugin=False, reapply=False,
                         version="latest", runtimes=["claude", "codex", "opencode"],
                         auto=False, force_prune_unproven=False)

    def _managed_result(self, skipped):
        return {
            "status": "updated", "version": "9.9.9", "runtimes": [],
            "session_action": {}, "skipped": skipped, "release_root": "/releases/9.9.9",
        }

    def test_each_closed_reason_gets_its_own_hint(self):
        skipped = {"claude": "missing", "codex": "linked", "opencode": "foreign"}
        with mock.patch.object(installer.distribution, "is_managed", return_value=True), \
             mock.patch.object(installer.distribution, "update", return_value=self._managed_result(skipped)):
            result = installer.cmd_update(self._update_args())
        hints = result["skipped_hints"]
        self.assertEqual(set(hints), {"claude", "codex", "opencode"})
        self.assertEqual(len(set(hints.values())), 3)
        for rt in ("claude", "codex", "opencode"):
            self.assertIn({"id": f"update.skipped.{rt}", "ok": False,
                           "detail": f"{skipped[rt]}: {hints[rt]}"}, result["checks"])

    def test_release_skipped_map_is_untouched(self):
        skipped = {"claude": "missing"}
        with mock.patch.object(installer.distribution, "is_managed", return_value=True), \
             mock.patch.object(installer.distribution, "update", return_value=self._managed_result(skipped)):
            result = installer.cmd_update(self._update_args())
        self.assertEqual(result["release"]["skipped"], skipped)

    def test_update_exit_code_is_unchanged(self):
        skipped = {"claude": "missing"}
        with mock.patch.object(installer.distribution, "is_managed", return_value=True), \
             mock.patch.object(installer.distribution, "update", return_value=self._managed_result(skipped)):
            result = installer.cmd_update(self._update_args())
        self.assertEqual(result["exit"], installer.EXIT_OK)

    def test_unknown_reason_keeps_legacy_line_format(self):
        skipped = {"claude": "some-new-reason"}
        with mock.patch.object(installer.distribution, "is_managed", return_value=True), \
             mock.patch.object(installer.distribution, "update", return_value=self._managed_result(skipped)):
            result = installer.cmd_update(self._update_args())
        self.assertIn("skipped: claude (some-new-reason)", result["lines"])
        self.assertNotIn("skipped_hints", result)


if __name__ == "__main__":
    unittest.main()
