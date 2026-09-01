#!/usr/bin/env python3
"""Installer-level fault injection at the Codex launcher commit boundary.

`runtime_activation.test.py` and `runtime-activation.test.sh` already prove
`runtime_activation.py`'s own multi-runtime rollback and the end-to-end
protected-ingress lifecycle against a full adapter fixture. This module
isolates `installer.py`'s `cmd_runtime` rollback wiring instead: it stubs the
runtime-projection collaborator (`runtime_activation.capture_runtime_state` /
`activate` / `refresh` / `restore_runtime_state` / `discard_runtime_state`) so
a failure can be injected precisely at the launcher-commit boundary added by
this plan (`HARNESS_INSTALLER_FAIL_AFTER_LAUNCHER=1`, right after
`codex_launcher.install()` returns but before the transaction is reported as
durable), and asserts the real `codex_launcher` state/wrapper on disk is
restored to its exact pre-transaction bytes.
"""
import os
import stat
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import installer  # noqa: E402
import codex_launcher  # noqa: E402
import runtime_activation  # noqa: E402
import fixture_env  # noqa: E402


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

    def test_injected_failure_after_launcher_commit_restores_exact_prior_state(self):
        with _stubbed_runtime_projection():
            first = installer.cmd_runtime(self._args("activate"))
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

        # The launcher install() call inside the injected attempt ran and
        # committed (it is a real, unmocked transaction); the installer's
        # rollback must have driven codex_launcher.restore_snapshot() to put
        # the wrapper and state back to the pre-refresh committed bytes.
        self.assertEqual(state_path.read_bytes(), committed_state_bytes)
        self.assertEqual(wrapper_target.read_bytes(), committed_wrapper_bytes)
        status = codex_launcher.status(codex_home=self.codex_home)
        self.assertTrue(status["installed"])
        self.assertEqual(status["real_command"], str(self.vendor_codex))
        self._assert_protected_surfaces_untouched()

    def test_no_injection_leaves_refresh_committed(self):
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
        self.assertTrue(status["installed"])
        self.assertEqual(status["real_command"], str(self.vendor_codex))
        self._assert_protected_surfaces_untouched()

    def test_reinstall_after_activate_is_idempotent_at_installer_level(self):
        with _stubbed_runtime_projection():
            first = installer.cmd_runtime(self._args("activate"))
        self.assertEqual(first["exit"], installer.EXIT_OK)
        state_path = codex_launcher.state_path(self.codex_home)
        wrapper_target = codex_launcher.wrapper_path(codex_launcher.default_bin_dir())
        committed_state_bytes = state_path.read_bytes()
        committed_wrapper_bytes = wrapper_target.read_bytes()

        # Reinstall: activate again over an already-activated runtime.
        with _stubbed_runtime_projection() as mocks:
            second = installer.cmd_runtime(self._args("activate"))
            mocks["restore_runtime_state"].assert_not_called()

        self.assertEqual(second["exit"], installer.EXIT_OK)
        self.assertEqual(state_path.read_bytes(), committed_state_bytes)
        self.assertEqual(wrapper_target.read_bytes(), committed_wrapper_bytes)
        status = codex_launcher.status(codex_home=self.codex_home)
        self.assertTrue(status["installed"])
        self.assertEqual(status["real_command"], str(self.vendor_codex))
        self._assert_protected_surfaces_untouched()

    def test_full_uninstall_removes_managed_launcher(self):
        with _stubbed_runtime_projection():
            first = installer.cmd_runtime(self._args("activate"))
        self.assertEqual(first["exit"], installer.EXIT_OK)
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
