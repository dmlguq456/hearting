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


def _write_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@contextmanager
def _stubbed_runtime_projection():
    fake_report = {"runtime": "codex", "freshness": "fresh", "next_action": "none"}
    with mock.patch.multiple(
        runtime_activation,
        capture_runtime_state=mock.DEFAULT,
        restore_runtime_state=mock.DEFAULT,
        discard_runtime_state=mock.DEFAULT,
        activate=mock.DEFAULT,
        refresh=mock.DEFAULT,
    ) as mocks:
        mocks["capture_runtime_state"].return_value = {"fake": "snapshot"}
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
        env = {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex_home),
            "PATH": str(self.vendor_bin) + os.pathsep + os.environ.get("PATH", ""),
            # An unsupported shell name makes `_profile_path()` return None
            # unconditionally, so no launcher/uninstall transaction in this
            # module ever resolves a profile path from the ambient
            # SHELL/ZDOTDIR/XDG_CONFIG_HOME of the process running the test
            # (which would otherwise point outside the private fixture home
            # at a real, unrelated shell startup file). Shell-specific
            # profile-mapping behavior is exhaustively covered by
            # `codex_launcher.test.py`, not this module.
            "SHELL": "/bin/installer-runtime-test-unsupported-shell",
        }
        env.pop("HARNESS_BIN_DIR", None)
        self._env_patch = mock.patch.dict(os.environ, env, clear=False)
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
        restore_runtime_state.assert_called_once_with({"fake": "snapshot"})

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
            discard_runtime_state.assert_called_once_with({"fake": "snapshot"})
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


if __name__ == "__main__":
    unittest.main()
