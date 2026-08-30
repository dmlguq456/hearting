#!/usr/bin/env python3

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "utilities" / "codex-managed-entry.py"


class ManagedEntryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "codex-home"
        self.state = self.base / "state"
        self.workspace = self.base / "workspace"
        for path in (self.home, self.state, self.workspace):
            path.mkdir()
        os.chmod(self.home, 0o700)
        os.chmod(self.state, 0o700)
        (self.home / "auth.json").write_text("{}\n", encoding="utf-8")
        os.chmod(self.home / "auth.json", 0o600)
        (self.home / "config.toml").write_text(
            "[features]\ndefault_mode_request_user_input = false\n",
            encoding="utf-8",
        )
        (self.home / "hooks.json").write_text("{}\n", encoding="utf-8")
        self.fake_codex = self.base / "fake-codex.py"
        self.argv_log = self.base / "fake-codex-argv.jsonl"
        self.fake_codex.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json, os, pathlib, signal, socket, sys, time
                log = pathlib.Path(__ARGV_LOG__)
                with log.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(sys.argv[1:]) + '\\n')
                if '--help' in sys.argv:
                    print('--listen --remote')
                    raise SystemExit(0)
                if sys.argv[1:3] == ['features', 'list']:
                    mode = os.environ.get('FAKE_FEATURE_MODE', 'enabled')
                    if mode == 'rejected':
                        raise SystemExit(2)
                    if mode == 'missing':
                        print('another_feature stable true')
                    elif mode == 'false':
                        print('default_mode_request_user_input under development false')
                    else:
                        print('default_mode_request_user_input under development true')
                    raise SystemExit(0)
                if '--remote' in sys.argv:
                    raise SystemExit(0)
                listen = sys.argv[sys.argv.index('--listen') + 1]
                path = listen[len('unix://'):] if listen.startswith('unix://') else listen
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(path)
                server.listen(1)
                stop = False
                def end(*_args):
                    global stop
                    stop = True
                signal.signal(signal.SIGTERM, end)
                while not stop:
                    time.sleep(0.02)
                server.close()
                """
            ).replace("__ARGV_LOG__", repr(str(self.argv_log))),
            encoding="utf-8",
        )
        self.fake_codex.chmod(0o755)
        self.client = self.base / "client.py"
        self.result = self.base / "client-result.json"
        self.client.write_text(
            textwrap.dedent(
                """\
                import json, os, pathlib, sys
                remote, result = sys.argv[1:]
                value = {
                    'remote': remote,
                    'gateway': os.environ.get('AGENT_CODEX_MANAGED_GATEWAY'),
                    'parent_runtime': os.environ.get('AGENT_CODEX_MANAGED_PARENT_RUNTIME'),
                    'control': os.environ.get('AGENT_CODEX_MANAGED_CONTROL_SOCKET'),
                    'codex_home': os.environ.get('CODEX_HOME'),
                    'agent_home': os.environ.get('AGENT_HOME'),
                    'jobs': os.environ.get('AGENT_DISPATCH_JOBS'),
                }
                pathlib.Path(result).write_text(json.dumps(value), encoding='utf-8')
                raise SystemExit(int(os.environ.get('FAKE_CLIENT_EXIT', '0')))
                """
            ),
            encoding="utf-8",
        )

    def command(self) -> list[str]:
        client_command = (
            f"{sys.executable} {self.client} {{remote}} {self.result}"
        )
        return [
            sys.executable,
            str(ENTRY),
            "--codex",
            str(self.fake_codex),
            "--codex-home",
            str(self.home),
            "--state-dir",
            str(self.state),
            "--workspace",
            str(self.workspace),
            "--client-command",
            client_command,
        ]

    def tui_command(self, client_args: list[str] | None = None) -> list[str]:
        command = self.command()
        command = command[: command.index("--client-command")]
        return [*command, "--", *(client_args or [])]

    def logged_commands(self) -> list[list[str]]:
        if not self.argv_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.argv_log.read_text(encoding="utf-8").splitlines()
        ]

    def test_new_session_opt_in_exports_managed_contract_and_cleans_sockets(self) -> None:
        result = subprocess.run(
            self.command(), text=True, capture_output=True, timeout=15
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        value = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(value["gateway"], "1")
        self.assertEqual(value["parent_runtime"], "codex")
        self.assertEqual(value["codex_home"], str(self.home))
        self.assertEqual(value["agent_home"], str(ROOT))
        self.assertEqual(value["jobs"], str(self.state / "jobs.log"))
        self.assertEqual((self.state / "jobs.log").stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            value["remote"], f"unix://{self.state / 'managed-tui.sock'}"
        )
        self.assertEqual(
            value["control"], str(self.state / "managed-control.sock")
        )
        for name in (
            "app-server.sock",
            "managed-tui.sock",
            "managed-control.sock",
        ):
            self.assertFalse((self.state / name).exists())

    def test_client_failure_and_gateway_fault_clean_only_exact_session_sockets(self) -> None:
        sentinel = self.state / "unrelated.sock"
        sentinel.write_bytes(b"preserve\n")
        for fault in ("none", "before-send", "after-send"):
            with self.subTest(fault=fault), mock.patch.dict(
                os.environ, {"FAKE_CLIENT_EXIT": "7"}, clear=False
            ):
                command = self.command()
                command.insert(command.index("--client-command"), "--gateway-fault")
                command.insert(command.index("--client-command"), fault)
                result = subprocess.run(command, text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 7, result.stderr)
                for name in ("app-server.sock", "managed-tui.sock", "managed-control.sock"):
                    self.assertFalse((self.state / name).exists())
                self.assertEqual(sentinel.read_bytes(), b"preserve\n")

    def test_nonprivate_state_dir_fails_before_process_start(self) -> None:
        os.chmod(self.state, 0o755)
        result = subprocess.run(
            self.command(), text=True, capture_output=True, timeout=5
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("state-dir-permissions-unsafe", result.stderr)
        self.assertFalse(self.result.exists())

    def test_check_validates_runtime_without_starting_sockets(self) -> None:
        command = self.command()
        command.insert(command.index("--client-command"), "--check")
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=5
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "ready")
        self.assertEqual(value["jobs"], str(self.state / "jobs.log"))
        self.assertEqual(
            value["feature_default_mode_request_user_input"], "enabled"
        )
        self.assertFalse(self.result.exists())
        for name in (
            "app-server.sock",
            "managed-tui.sock",
            "managed-control.sock",
        ):
            self.assertFalse((self.state / name).exists())

    def test_relative_jobs_path_fails_before_process_start(self) -> None:
        command = self.command()
        insert = command.index("--workspace")
        command[insert:insert] = ["--jobs", "relative-jobs.log"]
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=5
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("jobs-path-unsafe", result.stderr)
        self.assertFalse(self.result.exists())

    def test_feature_probe_is_exact_and_never_writes_runtime_owned_files(self) -> None:
        protected = {
            name: (self.home / name).read_bytes()
            for name in ("auth.json", "config.toml", "hooks.json")
        }
        for mode, expected in (
            ("enabled", "enabled"),
            ("false", "unsupported"),
            ("missing", "unsupported"),
            ("rejected", "unsupported"),
        ):
            with self.subTest(mode=mode), mock.patch.dict(
                os.environ, {"FAKE_FEATURE_MODE": mode}
            ):
                command = self.command()
                command.insert(command.index("--client-command"), "--check")
                result = subprocess.run(
                    command, text=True, capture_output=True, timeout=5
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout)[
                        "feature_default_mode_request_user_input"
                    ],
                    expected,
                )
        self.assertEqual(
            protected,
            {
                name: (self.home / name).read_bytes()
                for name in protected
            },
        )

    def test_supported_launch_enables_app_server_and_remote_tui(self) -> None:
        result = subprocess.run(
            self.tui_command(), text=True, capture_output=True, timeout=15
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.logged_commands()
        app_server = next(
            value
            for value in commands
            if value[:1] == ["app-server"] and "--help" not in value
        )
        remote = next(value for value in commands if "--remote" in value)
        for value in (app_server, remote):
            self.assertIn("--enable", value)
            self.assertEqual(
                value[value.index("--enable") + 1],
                "default_mode_request_user_input",
            )

    def test_explicit_disable_forms_win_for_both_processes(self) -> None:
        forms = (
            ["--disable", "default_mode_request_user_input"],
            ["--disable=default_mode_request_user_input"],
            ["-c", "features.default_mode_request_user_input=false"],
            ["-cfeatures.default_mode_request_user_input = false"],
            ["--config", "features.default_mode_request_user_input=false"],
            ["--config=features.default_mode_request_user_input=false"],
        )
        for form in forms:
            with self.subTest(form=form):
                self.argv_log.write_text("", encoding="utf-8")
                result = subprocess.run(
                    self.tui_command(list(form)),
                    text=True,
                    capture_output=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                commands = self.logged_commands()
                app_server = next(
                    value
                    for value in commands
                    if value[:1] == ["app-server"] and "--help" not in value
                )
                remote = next(value for value in commands if "--remote" in value)
                self.assertNotIn("--enable", app_server)
                self.assertNotIn("--enable", remote)
                self.assertEqual(
                    app_server[app_server.index("--disable") + 1],
                    "default_mode_request_user_input",
                )
                for token in form:
                    self.assertIn(token, remote)

                check = self.command()
                check.insert(check.index("--client-command"), "--check")
                check.extend(["--", *form])
                checked = subprocess.run(
                    check, text=True, capture_output=True, timeout=5
                )
                self.assertEqual(checked.returncode, 0, checked.stderr)
                self.assertEqual(
                    json.loads(checked.stdout)[
                        "feature_default_mode_request_user_input"
                    ],
                    "user-disabled",
                )

    def test_unsupported_feature_warns_once_and_launches_without_injection(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_FEATURE_MODE": "missing"}):
            result = subprocess.run(
                self.tui_command(), text=True, capture_output=True, timeout=15
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        warnings = [
            json.loads(line)
            for line in result.stderr.splitlines()
            if line.startswith("{")
        ]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["reason"], "unsupported")
        commands = self.logged_commands()
        app_server = next(
            value
            for value in commands
            if value[:1] == ["app-server"] and "--help" not in value
        )
        remote = next(value for value in commands if "--remote" in value)
        self.assertNotIn("--enable", app_server)
        self.assertNotIn("--enable", remote)


if __name__ == "__main__":
    unittest.main()
