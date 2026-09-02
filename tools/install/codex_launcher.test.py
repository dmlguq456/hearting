#!/usr/bin/env python3
"""Unit tests for the reversible Codex CLI launcher installation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_launcher as launcher
import fixture_env


class CodexLauncherInstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.codex_home = self.home / ".codex"
        self.bin_dir = self.home / ".local" / "bin"
        self.real = self.root / "runtime" / "codex-real"
        self.real.parent.mkdir(parents=True)
        self.real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.real.chmod(0o755)
        self.codex_home.mkdir(parents=True)
        self.codex_home.chmod(0o775)
        self.bin_dir.mkdir(parents=True)
        self.target = self.bin_dir / "codex"
        self.target.symlink_to(self.real)
        hermetic = fixture_env.build_environment(
            self.root,
            Path(__file__).resolve().parents[2],
            base={"PATH": os.environ.get("PATH", "")},
        )
        hermetic.update(
            {
                "CODEX_HOME": str(self.codex_home),
                "HARNESS_BIN_DIR": str(self.bin_dir),
                "PATH": str(self.bin_dir),
                "SHELL": "/bin/codex-launcher-test-unsupported-shell",
            }
        )
        fixture_env.prepare_environment(hermetic)
        self.environment = mock.patch.dict(os.environ, hermetic, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    # destructive-ok: reason=simulate vendor replacement of one fixture ingress; boundary=self.target inside this test TemporaryDirectory
    def test_install_repair_and_uninstall_restore_exact_binding(self) -> None:
        created = launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)
        self.assertEqual(created["status"], "created")
        self.assertTrue(self.target.is_file())
        self.assertFalse(self.target.is_symlink())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.codex_home.stat().st_mode & 0o777, 0o700)

        state = json.loads(launcher.state_path(self.codex_home).read_text())
        self.assertEqual(state["phase"], "installed")
        self.assertEqual(state["previous_wrapper"], {"kind": "symlink", "target": str(self.real)})
        self.assertEqual(state["previous_codex_home_mode"], 0o775)
        self.assertEqual(launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)["status"], "unchanged")

        self.target.unlink()
        self.target.symlink_to(self.real)
        repaired = launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)
        self.assertEqual(repaired["status"], "repaired")
        repaired_state = json.loads(launcher.state_path(self.codex_home).read_text())
        self.assertEqual(repaired_state["previous_codex_home_mode"], 0o775)

        restored = launcher.uninstall(codex_home=self.codex_home, bin_dir=self.bin_dir)
        self.assertEqual(restored["status"], "restored")
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(os.readlink(self.target), str(self.real))
        self.assertEqual(self.codex_home.stat().st_mode & 0o777, 0o775)
        # Uninstall is an endpoint: no harness-owned file may remain, the lock
        # pathname included (the post-publish release smoke asserts this).
        self.assertFalse(launcher.lock_path(self.codex_home).exists())

    def test_uninstall_on_not_installed_home_leaves_no_lock(self) -> None:
        result = launcher.uninstall(codex_home=self.codex_home, bin_dir=self.bin_dir)
        self.assertEqual(result["status"], "not-installed")
        self.assertFalse(launcher.lock_path(self.codex_home).exists())

    # destructive-ok: reason=inject a replaced lock pathname; boundary=one fixture lock below self.codex_home
    def test_launcher_lock_never_unlinks_a_successor_inode(self) -> None:
        path = launcher.lock_path(self.codex_home)
        lock = launcher._LauncherLock(path)
        lock.__enter__()
        path.unlink()
        path.write_bytes(b"successor")
        successor = path.stat()

        lock.__exit__(None, None, None)

        self.assertTrue(path.is_file())
        current = path.stat()
        self.assertEqual((current.st_dev, current.st_ino), (successor.st_dev, successor.st_ino))

    def test_symlinked_launcher_lock_is_rejected_without_mutating_target(self) -> None:
        path = launcher.lock_path(self.codex_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        foreign = self.root / "foreign-lock"
        foreign.write_bytes(b"foreign")
        path.symlink_to(foreign)

        with self.assertRaisesRegex(launcher.CodexLauncherError, "unsafe Codex launcher lock"):
            launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)

        self.assertEqual(foreign.read_bytes(), b"foreign")

    # destructive-ok: reason=construct an orphaned fixture wrapper; boundary=self.target inside this test TemporaryDirectory
    def test_adopts_byte_exact_orphaned_wrapper(self) -> None:
        self.target.unlink()
        self.target.write_bytes(launcher.wrapper_bytes())
        self.target.chmod(0o755)

        created = launcher.install(
            codex_home=self.codex_home,
            bin_dir=self.bin_dir,
            real_command=str(self.real),
        )

        self.assertEqual(created["status"], "created")
        state = json.loads(launcher.state_path(self.codex_home).read_text())
        self.assertEqual(
            state["previous_wrapper"],
            {"kind": "symlink", "target": str(self.real.absolute())},
        )
        self.assertEqual(
            launcher.uninstall(codex_home=self.codex_home, bin_dir=self.bin_dir)["status"],
            "restored",
        )
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(os.readlink(self.target), str(self.real.absolute()))
        self.assertFalse(launcher.state_path(self.codex_home).exists())

    def test_upgrade_repairs_previous_managed_wrapper_bytes(self) -> None:
        launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)
        upgraded = launcher.wrapper_bytes() + b"# upgraded fixture\n"

        with mock.patch.object(launcher, "wrapper_bytes", return_value=upgraded):
            repaired = launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)

        self.assertEqual(repaired["status"], "repaired")
        self.assertEqual(self.target.read_bytes(), upgraded)

    # destructive-ok: reason=simulate a moved fixture vendor command; boundary=self.real inside this test TemporaryDirectory
    def test_update_recovers_when_recorded_real_command_moves(self) -> None:
        launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)
        replacement = self.root / "codex-new"
        replacement.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        replacement.chmod(0o755)
        self.real.unlink()

        repaired = launcher.install(
            codex_home=self.codex_home,
            bin_dir=self.bin_dir,
            real_command=str(replacement),
        )

        self.assertEqual(repaired["status"], "repaired")
        state = json.loads(launcher.state_path(self.codex_home).read_text())
        self.assertEqual(state["real_command"], str(replacement))
        self.assertTrue(launcher.status(codex_home=self.codex_home, bin_dir=self.bin_dir)["healthy"])

    def test_dry_run_does_not_create_runtime_paths(self) -> None:
        other_home = self.root / "dry-home"
        other_bin = self.root / "dry-bin"
        result = launcher.install(
            codex_home=other_home,
            bin_dir=other_bin,
            real_command=str(self.real),
            dry_run=True,
        )
        self.assertEqual(result["status"], "planned")
        self.assertFalse(other_home.exists())
        self.assertFalse(other_bin.exists())

    # destructive-ok: reason=construct a foreign fixture ingress; boundary=self.target inside this test TemporaryDirectory
    def test_foreign_file_is_never_overwritten(self) -> None:
        self.target.unlink()
        self.target.write_text("user-owned\n", encoding="utf-8")
        before = self.target.read_bytes()
        with self.assertRaises(launcher.CodexLauncherError):
            launcher.install(
                codex_home=self.codex_home,
                bin_dir=self.bin_dir,
                real_command=str(self.real),
            )
        self.assertEqual(self.target.read_bytes(), before)
        self.assertFalse(launcher.state_path(self.codex_home).exists())

    # destructive-ok: reason=construct a foreign fixture symlink; boundary=self.target inside this test TemporaryDirectory
    def test_foreign_symlink_is_never_adopted(self) -> None:
        foreign = self.root / "foreign-vendor"
        foreign.write_bytes(b"vendor\n")
        foreign.chmod(0o755)
        self.target.unlink()
        self.target.symlink_to(foreign)
        with self.assertRaises(launcher.CodexLauncherError):
            launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir,
                             real_command=str(self.real))
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(self.target.readlink(), foreign)
        self.assertFalse(launcher.state_path(self.codex_home).exists())

    # destructive-ok: reason=simulate a missing fixture vendor command; boundary=self.target inside this test TemporaryDirectory
    def test_missing_real_cli_has_a_typed_result(self) -> None:
        self.target.unlink()
        with mock.patch.object(launcher.shutil, "which", return_value=None):
            with self.assertRaises(launcher.CodexUnavailableError):
                launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)

    def test_path_precedence_skips_entries_without_an_executable_codex(self) -> None:
        empty_bin = self.root / "empty-bin"
        empty_bin.mkdir()
        nonexec_bin = self.root / "nonexec-bin"
        nonexec_bin.mkdir()
        nonexec = nonexec_bin / "codex"
        nonexec.write_text("not executable\n", encoding="utf-8")
        nonexec.chmod(0o644)
        with mock.patch.dict(
            os.environ,
            {"PATH": os.pathsep.join([str(empty_bin), str(nonexec_bin), str(self.bin_dir)])},
        ):
            self.assertEqual(launcher._path_precedence(self.target), "first")

    def test_path_precedence_reports_an_earlier_executable_codex(self) -> None:
        foreign_bin = self.root / "foreign-bin"
        foreign_bin.mkdir()
        foreign = foreign_bin / "codex"
        foreign.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        foreign.chmod(0o755)
        with mock.patch.dict(
            os.environ,
            {"PATH": os.pathsep.join([str(foreign_bin), str(self.bin_dir)])},
        ):
            self.assertEqual(launcher._path_precedence(self.target), "shadowed")

    def _write_foreign_wrapper(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(launcher.wrapper_bytes())
        path.chmod(0o755)

    # destructive-ok: reason=construct a foreign fixture wrapper; boundary=self.target inside this test TemporaryDirectory
    def test_binding_skips_a_foreign_install_wrapper_on_path(self) -> None:
        # A second HOME's wrapper earlier on PATH must never become real_command:
        # binding to it makes the launcher exec itself forever.
        foreign_bin = self.root / "other-home" / ".local" / "bin"
        self._write_foreign_wrapper(foreign_bin / "codex")
        real_bin = self.root / "real-bin"
        real_bin.mkdir()
        real_cli = real_bin / "codex"
        real_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        real_cli.chmod(0o755)
        self.target.unlink()
        with mock.patch.dict(
            os.environ, {"PATH": os.pathsep.join([str(foreign_bin), str(real_bin)])}
        ):
            with mock.patch.object(
                launcher.shutil, "which", return_value=str(foreign_bin / "codex")
            ):
                created = launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)
        self.assertEqual(created["status"], "created")
        state = json.loads(launcher.state_path(self.codex_home).read_text())
        self.assertEqual(state["real_command"], str(real_cli))

    # destructive-ok: reason=construct an isolated wrapper-only PATH fixture; boundary=self.target inside this test TemporaryDirectory
    def test_binding_fails_closed_when_only_wrappers_are_on_path(self) -> None:
        foreign_bin = self.root / "other-home" / ".local" / "bin"
        self._write_foreign_wrapper(foreign_bin / "codex")
        self.target.unlink()
        with mock.patch.dict(os.environ, {"PATH": str(foreign_bin)}):
            with mock.patch.object(
                launcher.shutil, "which", return_value=str(foreign_bin / "codex")
            ):
                with self.assertRaises(launcher.CodexUnavailableError):
                    launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)

    # destructive-ok: reason=construct an explicit self-reference fixture; boundary=self.target inside this test TemporaryDirectory
    def test_explicit_wrapper_real_command_is_rejected(self) -> None:
        foreign = self.root / "elsewhere" / "codex"
        self._write_foreign_wrapper(foreign)
        self.target.unlink()
        with self.assertRaises(launcher.CodexLauncherError):
            launcher.install(
                codex_home=self.codex_home,
                bin_dir=self.bin_dir,
                real_command=str(foreign),
            )

    def test_manage_profile_preserves_foreign_bytes_and_is_idempotent(self) -> None:
        profile = self.home / ".bashrc"
        original = b"# foreign\r\nexport PATH=\"/foreign:$PATH\"\r\n"
        profile.write_bytes(original)
        profile.chmod(0o600)
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            result = launcher.install(
                codex_home=self.codex_home,
                bin_dir=self.root / "protected-bin",
                real_command=str(self.real),
                profile_policy="manage",
            )
            self.assertTrue(result["profile"]["managed"])
            after = profile.read_bytes()
            self.assertTrue(after.startswith(original))
            self.assertIn(str(self.root / "protected-bin").encode(), after)
            again = launcher.install(
                codex_home=self.codex_home,
                bin_dir=self.root / "protected-bin",
                real_command=str(self.real),
                profile_policy="manage",
            )
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual(profile.read_bytes(), after)

    def test_existing_deny_install_can_be_promoted_to_managed_profile(self) -> None:
        protected = self.root / "protected-bin"
        profile = self.home / ".bashrc"
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            launcher.install(
                codex_home=self.codex_home,
                bin_dir=protected,
                real_command=str(self.real),
                profile_policy="deny",
            )
            self.assertFalse(profile.exists())
            repaired = launcher.install(
                codex_home=self.codex_home,
                bin_dir=protected,
                real_command=str(self.real),
                profile_policy="manage",
            )
            unchanged = launcher.install(
                codex_home=self.codex_home,
                bin_dir=protected,
                real_command=str(self.real),
                profile_policy="manage",
            )
        self.assertEqual(repaired["status"], "repaired")
        self.assertEqual(unchanged["status"], "unchanged")
        state = json.loads(launcher.state_path(self.codex_home).read_text())
        self.assertEqual(state["profile"]["policy"], "manage")
        self.assertTrue(state["profile"]["restore"]["owned"])
        self.assertNotIn("payload", state["profile"]["restore"])

    def test_uninstall_removes_owned_profile_block_and_preserves_later_user_bytes(self) -> None:
        protected = self.root / "protected-bin"
        profile = self.home / ".bashrc"
        original = b"# foreign\r\nexport KEEP=1\r\n"
        suffix = b"# added after install\n"
        profile.write_bytes(original)
        profile.chmod(0o600)
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            launcher.install(
                codex_home=self.codex_home,
                bin_dir=protected,
                real_command=str(self.real),
                profile_policy="manage",
            )
            profile.write_bytes(profile.read_bytes() + suffix)
            result = launcher.uninstall(codex_home=self.codex_home, bin_dir=protected)
        self.assertEqual(result["profile"]["status"], "removed")
        self.assertEqual(profile.read_bytes(), original + suffix)
        self.assertFalse((protected / "codex").exists())

    def test_wrapper_refresh_preserves_managed_profile_restore_evidence(self) -> None:
        protected = self.root / "protected-bin"
        profile = self.home / ".bashrc"
        profile.write_bytes(b"foreign\n")
        profile.chmod(0o600)
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            launcher.install(
                codex_home=self.codex_home,
                bin_dir=protected,
                real_command=str(self.real),
                profile_policy="manage",
            )
            before = json.loads(launcher.state_path(self.codex_home).read_text())["profile"]
            with mock.patch.object(
                launcher,
                "wrapper_bytes",
                return_value=launcher.wrapper_bytes() + b"# next runtime\n",
            ):
                launcher.install(
                    codex_home=self.codex_home,
                    bin_dir=protected,
                    real_command=str(self.real),
                    profile_policy="deny",
                )
            after = json.loads(launcher.state_path(self.codex_home).read_text())["profile"]
            launcher.uninstall(codex_home=self.codex_home, bin_dir=protected)
        self.assertEqual(after, before)
        self.assertEqual(profile.read_bytes(), b"foreign\n")

    def test_uninstall_leaves_a_preexisting_exact_profile_block(self) -> None:
        protected = self.root / "protected-bin"
        profile = self.home / ".bashrc"
        block = launcher._profile_block(protected)
        profile.write_bytes(block)
        profile.chmod(0o600)
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            launcher.install(
                codex_home=self.codex_home,
                bin_dir=protected,
                real_command=str(self.real),
                profile_policy="manage",
            )
            state = json.loads(launcher.state_path(self.codex_home).read_text())
            result = launcher.uninstall(codex_home=self.codex_home, bin_dir=protected)
        self.assertFalse(state["profile"]["restore"]["owned"])
        self.assertEqual(result["profile"]["status"], "not-owned")
        self.assertEqual(profile.read_bytes(), block)

    def test_uninstall_fails_closed_on_ambiguous_managed_profile(self) -> None:
        protected = self.root / "protected-bin"
        profile = self.home / ".bashrc"
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            launcher.install(
                codex_home=self.codex_home,
                bin_dir=protected,
                real_command=str(self.real),
                profile_policy="manage",
            )
            profile.write_bytes(profile.read_bytes() + launcher._profile_block(protected))
            with self.assertRaisesRegex(
                launcher.CodexLauncherError,
                "managed shell profile block is ambiguous",
            ):
                launcher.uninstall(codex_home=self.codex_home, bin_dir=protected)
        self.assertTrue((protected / "codex").is_file())
        self.assertTrue(launcher.state_path(self.codex_home).is_file())

    @unittest.skipUnless(
        os.name == "posix" and Path("/var/tmp").is_dir() and os.access("/var/tmp", os.W_OK),
        "requires a writable POSIX sticky-temp hierarchy",
    )
    def test_profile_validation_allows_immutable_system_ancestors(self) -> None:
        # The full-suite runner deliberately places isolated homes below
        # /var/tmp.  /var is root-owned and non-writable, which is a safe
        # immutable ancestor rather than a reason to reject the user-owned
        # profile subtree below the sticky shared-temp boundary.
        with tempfile.TemporaryDirectory(dir="/var/tmp") as temporary:
            profile = Path(temporary) / "home" / ".bashrc"
            profile.parent.mkdir()
            launcher._validate_profile_path(profile)

    def test_profile_validation_rejects_foreign_writable_ancestor(self) -> None:
        foreign = self.root / "foreign-writable"
        owned = foreign / "owned"
        owned.mkdir(parents=True)
        foreign.chmod(0o777)
        profile = owned / ".bashrc"
        real_stat = Path.stat

        def stat_with_foreign_owner(candidate: Path, *args, **kwargs):
            info = real_stat(candidate, *args, **kwargs)
            if candidate == foreign:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid() + 1)
            return info

        with mock.patch.object(Path, "stat", new=stat_with_foreign_owner):
            with self.assertRaisesRegex(
                launcher.CodexLauncherError,
                "shell profile parent is not owner-writable",
            ):
                launcher._validate_profile_path(profile)

    def test_deny_profile_never_mutates_profile(self) -> None:
        profile = self.home / ".bashrc"
        original = b"foreign\n"
        profile.write_bytes(original)
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            result = launcher.install(
                codex_home=self.codex_home,
                bin_dir=self.root / "protected-bin",
                real_command=str(self.real),
                profile_policy="deny",
            )
        self.assertEqual(profile.read_bytes(), original)
        self.assertEqual(result["profile"]["reason"], "profile-authorization-required")

    def test_unknown_shell_never_mutates_and_reports_unsupported(self) -> None:
        # No .bashrc/.zshrc/fish conf.d exists anywhere under home for an
        # unrecognized shell; managing must fail closed without writing.
        before = set(self.home.iterdir())
        with mock.patch.dict(os.environ, {"SHELL": "/bin/tcsh"}):
            with self.assertRaises(launcher.CodexLauncherError):
                launcher.install(
                    codex_home=self.codex_home,
                    bin_dir=self.root / "protected-bin",
                    real_command=str(self.real),
                    profile_policy="manage",
                )
        self.assertEqual(set(self.home.iterdir()), before)

    def test_zsh_profile_uses_zdotdir(self) -> None:
        zdotdir = self.root / "zdot"
        zdotdir.mkdir()
        with mock.patch.dict(os.environ, {"SHELL": "/bin/zsh", "ZDOTDIR": str(zdotdir)}):
            result = launcher.install(
                codex_home=self.codex_home,
                bin_dir=self.root / "protected-bin",
                real_command=str(self.real),
                profile_policy="manage",
            )
        self.assertEqual(result["profile"]["path"], str(zdotdir / ".zshrc"))
        self.assertTrue((zdotdir / ".zshrc").exists())
        self.assertFalse((self.home / ".zshrc").exists())

    def test_fish_profile_uses_xdg_config_conf_d(self) -> None:
        xdg = self.root / "xdgcfg"
        with mock.patch.dict(os.environ, {"SHELL": "/usr/bin/fish", "XDG_CONFIG_HOME": str(xdg)}):
            result = launcher.install(
                codex_home=self.codex_home,
                bin_dir=self.root / "protected-bin",
                real_command=str(self.real),
                profile_policy="manage",
            )
        expected = xdg / "fish" / "conf.d" / "hearting-codex.fish"
        self.assertEqual(result["profile"]["path"], str(expected))
        self.assertTrue(expected.exists())

    def test_duplicate_marker_blocks_are_ambiguous_and_never_mutated(self) -> None:
        profile = self.home / ".bashrc"
        block = launcher._profile_block(self.root / "protected-bin")
        profile.write_bytes(block + block)
        before = profile.read_bytes()
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            with self.assertRaises(launcher.CodexLauncherError):
                launcher.install(
                    codex_home=self.codex_home,
                    bin_dir=self.root / "protected-bin",
                    real_command=str(self.real),
                    profile_policy="manage",
                )
        self.assertEqual(profile.read_bytes(), before)

    def test_nested_and_partial_marker_boundaries_are_never_mutated(self) -> None:
        profile = self.home / ".bashrc"
        block = launcher._profile_block(self.root / "protected-bin")
        cases = (
            block.replace(launcher.PROFILE_END, b"nested\n" + launcher.PROFILE_END),
            block[:-len(launcher.PROFILE_END)] + b"partial\n",
            launcher.PROFILE_START + b"foreign\n" + launcher.PROFILE_END,
        )
        for payload in cases:
            with self.subTest(payload=payload):
                profile.write_bytes(payload)
                with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
                    with self.assertRaises(launcher.CodexLauncherError):
                        launcher.install(codex_home=self.codex_home,
                                         bin_dir=self.root / "protected-bin",
                                         real_command=str(self.real),
                                         profile_policy="manage")
                self.assertEqual(profile.read_bytes(), payload)

    def test_symlinked_profile_is_unsafe_and_never_mutated(self) -> None:
        real_profile = self.root / "elsewhere-rc"
        real_profile.write_bytes(b"foreign\n")
        profile = self.home / ".bashrc"
        profile.symlink_to(real_profile)
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            with self.assertRaises(launcher.CodexLauncherError):
                launcher.install(
                    codex_home=self.codex_home,
                    bin_dir=self.root / "protected-bin",
                    real_command=str(self.real),
                    profile_policy="manage",
                )
        self.assertTrue(profile.is_symlink())
        self.assertEqual(real_profile.read_bytes(), b"foreign\n")

    def test_default_install_never_touches_vendor_bin_dir(self) -> None:
        # With no explicit bin_dir/HARNESS_BIN_DIR, install must land under
        # $CODEX_HOME/.harness/bin, never at the vendor's own ~/.local/bin.
        with mock.patch.dict(os.environ, {}, clear=False):
            del_key = "HARNESS_BIN_DIR"
            saved = os.environ.pop(del_key, None)
            try:
                created = launcher.install(codex_home=self.codex_home, real_command=str(self.real))
            finally:
                if saved is not None:
                    os.environ[del_key] = saved
        self.assertEqual(created["mode"], "protected-path-v1")
        protected_target = self.codex_home / ".harness" / "bin" / "codex"
        self.assertTrue(protected_target.exists())
        # The pre-seeded vendor symlink at self.bin_dir/codex is untouched.
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(os.readlink(self.target), str(self.real))

    def test_implicit_legacy_inplace_requires_explicit_authorization_flag(self) -> None:
        # With no explicit bin_dir/HARNESS_BIN_DIR and no compatibility flag,
        # the vendor bin dir is never chosen implicitly.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_BIN_DIR", None)
            created = launcher.install(
                codex_home=self.codex_home,
                real_command=str(self.real),
                allow_legacy_inplace=False,
            )
        self.assertEqual(created["mode"], "protected-path-v1")
        self.assertFalse((self.bin_dir / "codex").exists() and not self.target.is_symlink())

    # destructive-ok: reason=construct a legacy in-place fixture binding; boundary=self.target inside this test TemporaryDirectory
    def test_allow_legacy_inplace_opts_into_vendor_bin_dir_implicitly(self) -> None:
        self.target.unlink()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_BIN_DIR", None)
            created = launcher.install(
                codex_home=self.codex_home,
                real_command=str(self.real),
                allow_legacy_inplace=True,
            )
        self.assertEqual(created["mode"], "legacy-inplace-v1")
        self.assertTrue(self.target.exists())

    def test_activation_snapshot_excludes_profile_and_vendor_paths(self) -> None:
        profile = self.home / ".bashrc"
        profile.write_bytes(b"# user bytes\r\n")
        profile.chmod(0o600)
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            snapshot = launcher.capture_snapshot(
                codex_home=self.codex_home,
                bin_dir=self.root / "protected-bin",
                operation="activation",
            )
            self.assertNotIn("profile", snapshot["paths"])
            self.assertNotIn("vendor_binding", snapshot["paths"])
            profile.write_bytes(b"# transaction block\n")
            successor = self.root / "successor"
            successor.write_bytes(b"updater successor\n")
            successor.chmod(0o755)
            launcher.seal_snapshot(snapshot)
            launcher.restore_snapshot(snapshot, codex_home=self.codex_home,
                                      bin_dir=self.root / "protected-bin")
        self.assertEqual(profile.read_bytes(), b"# transaction block\n")
        self.assertEqual(successor.read_bytes(), b"updater successor\n")

    def test_absent_or_unsealed_snapshot_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            launcher.CodexLauncherError, "ownership-unproved"
        ):
            launcher.restore_snapshot(
                {
                    "codex_home": str(self.codex_home),
                    "ingress": str(self.target),
                }
            )
        snapshot = launcher.capture_snapshot(
            codex_home=self.codex_home,
            bin_dir=self.bin_dir,
            operation="activation",
        )
        try:
            with self.assertRaisesRegex(
                launcher.CodexLauncherError, "postimage is not sealed"
            ):
                launcher.restore_snapshot(snapshot)
        finally:
            launcher.discard_snapshot(snapshot)

    # destructive-ok: reason=construct one schema-1 launcher fixture; boundary=self.target inside this test TemporaryDirectory
    def _write_schema1_state(self) -> None:
        self.target.unlink(missing_ok=True)
        self.target.write_bytes(launcher.wrapper_bytes())
        self.target.chmod(0o755)
        launcher.state_path(self.codex_home).parent.mkdir(parents=True, exist_ok=True)
        launcher.state_path(self.codex_home).write_text(json.dumps({
            "schema": 1,
            "phase": "installed",
            "wrapper_path": str(self.target),
            "wrapper_sha256": launcher._digest(launcher.wrapper_bytes()),
            "real_command": str(self.real),
            "previous_wrapper": {"kind": "symlink", "target": str(self.real)},
            "previous_codex_home_mode": 0o775,
        }))

    def test_schema1_before_updater_migrates_and_restores_preimage(self) -> None:
        self._write_schema1_state()
        protected = self.codex_home / ".harness" / "bin"
        with mock.patch.dict(os.environ, {"HARNESS_BIN_DIR": ""}, clear=False):
            os.environ.pop("HARNESS_BIN_DIR", None)
            result = launcher.install(codex_home=self.codex_home, real_command=str(self.real))
        self.assertEqual(result["status"], "repaired")
        self.assertTrue((protected / "codex").exists())
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(os.readlink(self.target), str(self.real))
        state = json.loads(launcher.state_path(self.codex_home).read_text())
        self.assertEqual(state["migration"]["status"], "migrated")
        self.assertEqual(state["migration"]["schema1_preimage"]["target"], str(self.real))
        launcher.uninstall(codex_home=self.codex_home)
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(os.readlink(self.target), str(self.real))

    # destructive-ok: reason=simulate updater replacement of a fixture ingress; boundary=self.target inside this test TemporaryDirectory
    def test_schema1_after_updater_preserves_successor_on_migration_and_uninstall(self) -> None:
        self._write_schema1_state()
        successor = self.root / "vendor" / "current" / "bin" / "codex"
        successor.parent.mkdir(parents=True)
        successor.write_bytes(b"updater successor\n")
        successor.chmod(0o755)
        self.target.unlink()
        self.target.symlink_to(successor)
        before = self.target.readlink()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_BIN_DIR", None)
            result = launcher.install(codex_home=self.codex_home, real_command=str(successor))
        self.assertEqual(result["status"], "repaired")
        self.assertEqual(self.target.readlink(), before)
        self.assertEqual(self.target.read_bytes(), b"updater successor\n")
        state = json.loads(launcher.state_path(self.codex_home).read_text())
        self.assertEqual(state["migration"]["validated_successor"]["path"], str(self.target))
        launcher.uninstall(codex_home=self.codex_home)
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(self.target.readlink(), before)
        self.assertEqual(successor.read_bytes(), b"updater successor\n")

    # destructive-ok: reason=construct a future-schema fixture ingress; boundary=self.target inside this test TemporaryDirectory
    def test_future_schema_state_is_rejected_without_mutation(self) -> None:
        self.target.unlink(missing_ok=True)
        self.target.write_bytes(launcher.wrapper_bytes())
        self.target.chmod(0o755)
        state_file = launcher.state_path(self.codex_home)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"schema": 99, "phase": "installed"}))
        before = state_file.read_text()
        with self.assertRaises(launcher.CodexLauncherError):
            launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)
        self.assertEqual(state_file.read_text(), before)

    # destructive-ok: reason=construct a crash-repair fixture preimage; boundary=self.target inside this test TemporaryDirectory
    def test_injected_crash_during_repair_restores_exact_preimage(self) -> None:
        launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)
        original_state = launcher.state_path(self.codex_home).read_text()
        self.target.unlink()
        self.target.symlink_to(self.real)

        real_atomic_bytes = launcher._atomic_bytes

        def crash(path: Path, payload: bytes, mode: int, **kwargs) -> None:
            if Path(path) == self.target:
                raise OSError("simulated crash")
            return real_atomic_bytes(path, payload, mode, **kwargs)

        with mock.patch.object(launcher, "_atomic_bytes", side_effect=crash):
            with self.assertRaises(OSError):
                launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)

        self.assertTrue(self.target.is_symlink())
        self.assertEqual(os.readlink(self.target), str(self.real))
        self.assertEqual(launcher.state_path(self.codex_home).read_text(), original_state)

    def test_profile_install_failure_restores_profile_before_state_exists(self) -> None:
        profile = self.home / ".bashrc"
        profile.write_bytes(b"user prefix\n")
        profile.chmod(0o640)
        before = profile.read_bytes()
        protected = self.root / "protected-profile-failure"
        with (
            mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}),
            mock.patch.object(
                launcher, "_atomic_json", side_effect=OSError("state commit failed")
            ),
        ):
            with self.assertRaisesRegex(OSError, "state commit failed"):
                launcher.install(
                    codex_home=self.codex_home,
                    bin_dir=protected,
                    real_command=str(self.real),
                    profile_policy="manage",
                )
        self.assertEqual(profile.read_bytes(), before)
        self.assertFalse((protected / "codex").exists())
        self.assertFalse(launcher.state_path(self.codex_home).exists())

    def test_direct_uninstall_failure_rolls_back_profile_wrapper_and_state(self) -> None:
        profile = self.home / ".bashrc"
        profile.write_bytes(b"user prefix\n")
        profile.chmod(0o640)
        protected = self.root / "protected-uninstall-failure"
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            launcher.install(
                codex_home=self.codex_home,
                bin_dir=protected,
                real_command=str(self.real),
                profile_policy="manage",
            )
            profile_before = profile.read_bytes()
            wrapper_before = (protected / "codex").read_bytes()
            state_before = launcher.state_path(self.codex_home).read_bytes()
            with mock.patch.object(
                launcher,
                "_restore_wrapper",
                side_effect=OSError("wrapper restore failed"),
            ):
                with self.assertRaisesRegex(OSError, "wrapper restore failed"):
                    launcher.uninstall(
                        codex_home=self.codex_home, bin_dir=protected
                    )
        self.assertEqual(profile.read_bytes(), profile_before)
        self.assertEqual((protected / "codex").read_bytes(), wrapper_before)
        self.assertEqual(
            launcher.state_path(self.codex_home).read_bytes(), state_before
        )

    def test_schema1_migration_crash_recovery_restores_exact_start_state(self) -> None:
        self._write_schema1_state()
        original_state = json.loads(launcher.state_path(self.codex_home).read_text())
        original_legacy_bytes = self.target.read_bytes()
        protected_target = self.codex_home / ".harness" / "bin" / "codex"

        real_atomic_bytes = launcher._atomic_bytes

        def crash(path: Path, payload: bytes, mode: int, **kwargs) -> None:
            if Path(path) == protected_target:
                raise OSError("simulated crash")
            return real_atomic_bytes(path, payload, mode, **kwargs)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_BIN_DIR", None)
            with mock.patch.object(launcher, "_atomic_bytes", side_effect=crash):
                with self.assertRaises(OSError):
                    launcher.install(codex_home=self.codex_home, real_command=str(self.real))

        self.assertFalse(protected_target.exists())
        self.assertFalse(self.target.is_symlink())
        self.assertEqual(self.target.read_bytes(), original_legacy_bytes)
        restored_state = json.loads(launcher.state_path(self.codex_home).read_text())
        self.assertEqual(restored_state, original_state)

    def test_concurrent_installs_are_idempotent(self) -> None:
        created = launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)
        self.assertEqual(created["status"], "created")

        results: list[dict] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                results.append(launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir))
            except Exception as exc:  # noqa: BLE001 - captured for assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertTrue(all(result["status"] == "unchanged" for result in results))
        state = json.loads(launcher.state_path(self.codex_home).read_text())
        self.assertEqual(state["phase"], "installed")
        self.assertTrue(self.target.is_file())
        self.assertFalse(self.target.is_symlink())
        self.assertTrue(launcher.lock_path(self.codex_home).is_file())

    def test_hundred_invocation_reconciliation_smoke(self) -> None:
        created = launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)
        self.assertEqual(created["status"], "created")
        start = time.monotonic()
        for _ in range(100):
            result = launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)
            self.assertEqual(result["status"], "unchanged")
        duration = time.monotonic() - start
        self.assertLess(duration, 5.0)


if __name__ == "__main__":
    unittest.main()
