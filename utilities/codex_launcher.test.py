#!/usr/bin/env python3
"""Unit tests for interactive/pass-through Codex launcher routing."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("codex-launcher.py")
SPEC = importlib.util.spec_from_file_location("codex_launcher_runtime", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class CodexLauncherRuntimeTest(unittest.TestCase):
    def _state_fixture(self, root: Path, real: Path) -> Path:
        home = root / ".codex"
        harness = home / ".harness"
        harness.mkdir(parents=True)
        home.chmod(0o700)
        ingress = harness / "bin" / "codex"
        ingress.parent.mkdir()
        ingress.write_bytes(b"#!/bin/sh\n# protected ingress\n")
        ingress.chmod(0o755)
        (harness / "codex-launcher.json").write_text(json.dumps({
            "schema": 2, "phase": "installed", "real_command": str(real),
            "ingress_path": str(ingress), "wrapper_path": str(ingress),
        }), encoding="utf-8")
        (harness / "codex-launcher.json").chmod(0o600)
        return home

    def test_all_passthrough_commands_preserve_argv_after_vendor_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "vendor" / "codex"
            real.parent.mkdir()
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            home = self._state_fixture(root, real)
            replacement = root / "vendor" / "codex-v2"
            replacement.write_text("#!/bin/sh\n", encoding="utf-8")
            replacement.chmod(0o755)
            real.unlink()
            real.symlink_to(replacement)
            forms = [[name, "--fixture", "value"] for name in sorted(launcher.PASSTHROUGH_COMMANDS)]
            forms += [["login", "--device-auth"], ["logout"], ["update"], ["doctor"],
                      ["--help"], ["--version"], ["--remote", "unix:///private.sock"],
                      ["-h"], ["-V"]]
            for args in forms:
                with self.subTest(args=args), mock.patch.dict(
                    os.environ, {"CODEX_HOME": str(home), "HOME": str(root)}, clear=False
                ), mock.patch.object(launcher.os, "execv") as execv:
                    launcher.sys.argv = ["codex-launcher.py", *args]
                    self.assertIsNone(launcher.main())
                    execv.assert_called_once_with(str(real), [str(real), *args])
                    execv.reset_mock()
            self.assertFalse((home / ".harness" / "managed-sessions").exists())

    def test_interactive_forms_keep_pinned_environment_after_vendor_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = self._state_fixture(root, root / "vendor" / "codex")
            real = root / "vendor" / "codex"
            real.parent.mkdir(exist_ok=True)
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            real.chmod(0o755)
            active = root / "bundle"
            (active / "core").mkdir(parents=True)
            (active / "core" / "CORE.md").write_text("core\n", encoding="utf-8")
            (active / "utilities").mkdir()
            (active / "utilities" / "codex-managed-entry.py").write_text("# entry\n", encoding="utf-8")
            (home / "hearting").symlink_to(active, target_is_directory=True)
            (home / ".harness" / "activation.json").write_text(json.dumps({
                "schema": 2, "runtime": "codex", "mode": "linked",
                "active_root": str(active), "active_revision": "rev-test",
            }), encoding="utf-8")
            (home / ".harness" / "activation.json").chmod(0o600)
            (home / "auth.json").write_text("{}\n", encoding="utf-8")
            (home / "auth.json").chmod(0o600)
            for args in ([], ["resume", "--last"], ["fork"]):
                with self.subTest(args=args), mock.patch.dict(
                    os.environ, {"CODEX_HOME": str(home), "HOME": str(root)}, clear=False
                ), mock.patch.object(launcher.os, "execv") as execv:
                    launcher.sys.argv = ["codex-launcher.py", *args]
                    launcher.main()
                    command = execv.call_args.args[1]
                    self.assertEqual(command[command.index("--codex") + 1], str(real))
                    self.assertEqual(os.environ["AGENT_HOME"], str(active))
                    self.assertEqual(os.environ["AGENT_RUNTIME_IDENTITY"], "linked:rev-test:-")
                    execv.reset_mock()
    def test_only_interactive_surfaces_are_managed(self) -> None:
        managed = (
            [],
            ["hello"],
            ["resume", "--last"],
            ["fork"],
            ["--model", "gpt-test", "resume", "thread-id"],
        )
        passed_through = (
            ["exec", "task"],
            ["--model", "gpt-test", "exec", "task"],
            ["plugin", "list"],
            ["app-server", "--help"],
            ["--help"],
            ["resume", "--help"],
            ["--remote", "unix:///tmp/codex.sock"],
        )
        for args in managed:
            with self.subTest(args=args):
                self.assertTrue(launcher.should_manage(list(args)))
        for args in passed_through:
            with self.subTest(args=args):
                self.assertFalse(launcher.should_manage(list(args)))

    def test_bypass_environment_is_explicit(self) -> None:
        with mock.patch.dict(os.environ, {"AGENT_CODEX_LAUNCHER_BYPASS": "1"}):
            self.assertFalse(launcher.should_manage(["resume", "--last"]))

    def test_workspace_honors_global_cd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(launcher.Path, "cwd", return_value=root):
                self.assertEqual(
                    launcher.workspace(["-C", "nested", "resume"]),
                    root / "nested",
                )
                self.assertEqual(
                    launcher.workspace(["--cd=other", "fork"]),
                    root / "other",
                )

    def test_managed_command_uses_private_per_session_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".codex"
            home.mkdir()
            agent_home = root / "hearting"
            entry = agent_home / "utilities" / "codex-managed-entry.py"
            entry.parent.mkdir(parents=True)
            entry.write_text("# fixture\n", encoding="utf-8")
            real = root / "codex-real"
            real.write_text("# fixture\n", encoding="utf-8")
            command = launcher.managed_command(
                ["resume", "--last"],
                home,
                real,
                {"active_root": agent_home},
            )
            self.assertEqual(command[1], str(entry))
            self.assertEqual(command[command.index("--codex") + 1], str(real))
            state_dir = Path(command[command.index("--state-dir") + 1])
            self.assertEqual(state_dir.parent, home / ".harness" / "managed-sessions")
            self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                command[command.index("--jobs") + 1],
                str(home / ".harness" / "dispatch" / "jobs.log"),
            )
            self.assertEqual(command[-3:], ["--", "resume", "--last"])

    def test_managed_command_preserves_feature_opt_out_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".codex"
            home.mkdir()
            agent_home = root / "hearting"
            entry = agent_home / "utilities" / "codex-managed-entry.py"
            entry.parent.mkdir(parents=True)
            entry.write_text("# fixture\n", encoding="utf-8")
            real = root / "codex-real"
            real.write_text("# fixture\n", encoding="utf-8")
            original = [
                "-c",
                "features.default_mode_request_user_input=false",
                "resume",
                "--last",
            ]
            command = launcher.managed_command(
                original, home, real, {"active_root": agent_home}
            )
            separator = command.index("--")
            self.assertEqual(command[separator + 1 :], original)

    def test_auth_readiness_preserves_first_login_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.assertFalse(launcher.managed_auth_ready(home))
            auth = home / "auth.json"
            auth.write_text("{}\n", encoding="utf-8")
            auth.chmod(0o600)
            self.assertTrue(launcher.managed_auth_ready(home))
            auth.chmod(0o644)
            self.assertFalse(launcher.managed_auth_ready(home))

    def test_packaged_runtime_is_resolved_once_across_activation_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".codex"
            harness = home / ".harness"
            bundles = harness / "bundles"
            bundles.mkdir(parents=True)

            def packaged(name: str) -> Path:
                source = bundles / name / "source"
                (source / "core").mkdir(parents=True)
                (source / "core" / "CORE.md").write_text("core\n", encoding="utf-8")
                entry = source / "utilities" / "codex-managed-entry.py"
                entry.parent.mkdir()
                entry.write_text(f"# {name}\n", encoding="utf-8")
                (source.parent / "bundle.json").write_text(
                    '{"checksum":"sum-%s","source_revision":"rev-%s"}\n'
                    % (name, name),
                    encoding="utf-8",
                )
                return source

            first = packaged("first")
            second = packaged("second")
            projection = home / "hearting"
            projection.symlink_to(first, target_is_directory=True)
            activation = harness / "activation.json"

            def select(name: str, source: Path) -> None:
                activation.write_text(
                    '{"schema":2,"runtime":"codex","mode":"packaged",'
                    '"active_root":"%s","active_revision":"rev-%s",'
                    '"bundle_checksum":"sum-%s"}\n' % (source, name, name),
                    encoding="utf-8",
                )
                activation.chmod(0o600)

            select("first", first)
            binding = launcher.pinned_runtime(home)
            projection.unlink()
            projection.symlink_to(second, target_is_directory=True)
            select("second", second)

            real = root / "codex-real"
            real.write_text("fixture\n", encoding="utf-8")
            command = launcher.managed_command([], home, real, binding)
            self.assertEqual(
                Path(command[1]), first / "utilities" / "codex-managed-entry.py"
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                launcher.export_runtime_binding(binding)
                self.assertEqual(os.environ["AGENT_HOME"], str(first))
                self.assertEqual(os.environ["AGENT_RUNTIME_ROOT"], str(first))
                self.assertEqual(
                    os.environ["AGENT_RUNTIME_IDENTITY"],
                    "packaged:rev-first:sum-first",
                )

    def test_state_rejects_a_wrapper_real_command(self) -> None:
        # Binding to another install's ingress would exec this launcher forever.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".codex"
            state_dir = home / ".harness"
            state_dir.mkdir(parents=True)
            home.chmod(0o700)
            wrapper = root / "other" / "codex"
            wrapper.parent.mkdir()
            wrapper.write_bytes(
                b"#!/bin/sh\nexec python3 $HOME/.codex/hearting/utilities/"
                b"codex-launcher.py \"$@\"\n# hearting ingress\n"
            )
            wrapper.chmod(0o755)
            state = state_dir / "codex-launcher.json"
            state.write_text(
                '{"schema": 1, "phase": "installed", "real_command": "%s"}' % wrapper,
                encoding="utf-8",
            )
            state.chmod(0o600)
            with self.assertRaises(launcher.LauncherError):
                launcher._state(home)

    def test_reentry_guard_detects_a_circular_binding(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"AGENT_CODEX_LAUNCHER_GUARD_PID": str(os.getpid())},
        ):
            with mock.patch.object(launcher.sys, "argv", ["codex-launcher.py", "--version"]):
                self.assertEqual(launcher.main(), 69)

    def test_private_runtime_home_falls_back_to_global_binding_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_home = root / "private-codex"
            private_home.mkdir()
            default_home = root / ".codex"
            state = default_home / ".harness" / "codex-launcher.json"
            state.parent.mkdir(parents=True)
            state.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(launcher.Path, "home", return_value=root):
                self.assertEqual(launcher.launcher_state_home(private_home), default_home)

            private_state = private_home / ".harness" / "codex-launcher.json"
            private_state.parent.mkdir()
            private_state.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(launcher.Path, "home", return_value=root):
                self.assertEqual(launcher.launcher_state_home(private_home), private_home)


if __name__ == "__main__":
    unittest.main()
