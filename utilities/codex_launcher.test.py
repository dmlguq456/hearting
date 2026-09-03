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


class InteractivePermissionModeTest(unittest.TestCase):
    """User decision 2026-09-03 — a managed interactive Codex session starts in bypass."""

    def _mode_env(self, value=None):
        env = {} if value is None else {"AGENT_CODEX_INTERACTIVE_PERMISSION_MODE": value}
        return mock.patch.dict(os.environ, env, clear=False)

    def _cleared(self):
        return mock.patch.dict(
            os.environ, {"AGENT_CODEX_INTERACTIVE_PERMISSION_MODE": ""}, clear=False
        )

    def test_default_mode_is_bypass_and_only_inherit_opts_out(self) -> None:
        with self._cleared():
            self.assertEqual(launcher.interactive_permission_mode(), "bypass")
        for value, expected in (("inherit", "inherit"), ("INHERIT", "inherit"),
                                (" inherit ", "inherit"), ("bypass", "bypass"),
                                ("yolo", "bypass"), ("", "bypass"), ("off", "bypass")):
            with self.subTest(value=value), self._mode_env(value):
                self.assertEqual(launcher.interactive_permission_mode(), expected)

    def test_bare_and_resume_invocations_gain_the_flag_in_front(self) -> None:
        with self._cleared():
            for args in ([], ["hello"], ["resume", "--last"], ["fork"],
                         ["--model", "gpt-test", "resume", "thread-id"]):
                with self.subTest(args=args):
                    applied = launcher.apply_interactive_permission_mode(list(args))
                    # In front, so it stays a root option ahead of any subcommand.
                    self.assertEqual(applied, [launcher.BYPASS_FLAG, *args])

    def test_inherit_leaves_the_invocation_byte_identical(self) -> None:
        with self._mode_env("inherit"):
            for args in ([], ["resume", "--last"], ["hello"]):
                with self.subTest(args=args):
                    self.assertEqual(
                        launcher.apply_interactive_permission_mode(list(args)), list(args)
                    )

    def test_a_caller_selected_posture_is_never_overridden(self) -> None:
        explicit = (
            ["-s", "read-only"],
            ["--sandbox", "workspace-write"],
            ["--sandbox=read-only"],
            ["-a", "on-request"],
            ["--ask-for-approval", "never"],
            ["--ask-for-approval=on-request"],
            ["--approve-for-me"],
            [launcher.BYPASS_FLAG],
            ["--yolo"],
            ["-p", "hardened"],
            ["--profile", "hardened"],
            ["-c", "approval_policy=never"],
            ["-c", "sandbox_mode=read-only"],
            ["--config", "sandbox_permissions=[]"],
            ["--config=approval_policy=never"],
            ["-capproval_policy=never"],
            ["resume", "--last", "-s", "read-only"],
        )
        with self._cleared():
            for args in explicit:
                with self.subTest(args=args):
                    self.assertTrue(launcher.selects_own_posture(list(args)))
                    self.assertEqual(
                        launcher.apply_interactive_permission_mode(list(args)), list(args)
                    )

    def test_unrelated_options_do_not_look_like_a_posture(self) -> None:
        with self._cleared():
            for args in (["--model", "gpt-test"], ["-m", "gpt-test"], ["-c", "model=\"o3\""],
                         ["--config", "features.hooks=true"], ["--search"],
                         ["-i", "shot.png"], ["--cd", "/tmp"], ["--no-alt-screen"]):
                with self.subTest(args=args):
                    self.assertFalse(launcher.selects_own_posture(list(args)))
                    self.assertEqual(
                        launcher.apply_interactive_permission_mode(list(args))[0],
                        launcher.BYPASS_FLAG,
                    )

    def test_a_value_that_merely_looks_like_a_flag_is_not_read_as_one(self) -> None:
        """`--model --sandbox` is a model NAMED `--sandbox`; value slots are skipped."""
        with self._cleared():
            self.assertFalse(launcher.selects_own_posture(["--model", "--sandbox"]))

    def test_everything_after_a_bare_double_dash_is_the_prompt(self) -> None:
        with self._cleared():
            self.assertFalse(launcher.selects_own_posture(["--", "-s", "read-only"]))

    def test_passthrough_surfaces_never_reach_the_default(self) -> None:
        """`codex exec` is the registered dispatch surface (stage-dispatch SD-125 (5)):
        approval_policy=never with a real sandbox, and no bypass flag. It must stay
        outside `should_manage`, which is what keeps it away from this default."""
        for args in (["exec", "task"], ["--model", "gpt-test", "exec", "task"],
                     ["review", "--help"], ["app-server"], ["--remote", "unix:///tmp/x"]):
            with self.subTest(args=args):
                self.assertFalse(launcher.should_manage(list(args)))

    def test_managed_command_carries_the_flag_through_to_the_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agent_home = root / "runtime"
            (agent_home / "utilities").mkdir(parents=True)
            (agent_home / "utilities" / "codex-managed-entry.py").write_text("", "utf-8")
            home = root / ".codex"
            (home / ".harness").mkdir(parents=True)
            binding = {"active_root": agent_home, "mode": "linked",
                       "revision": "rev-test", "identity": "linked:rev-test:-"}
            with self._cleared():
                applied = launcher.apply_interactive_permission_mode(["resume", "--last"])
            command = launcher.managed_command(applied, home, Path("/usr/bin/codex"), binding)
            trailing = command[command.index("--") + 1:]
            self.assertEqual(trailing, [launcher.BYPASS_FLAG, "resume", "--last"])


if __name__ == "__main__":
    unittest.main()
