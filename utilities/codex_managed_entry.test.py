#!/usr/bin/env python3

from __future__ import annotations

import json
import fcntl
import importlib.util
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
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
                import fcntl, json, os, pathlib, signal, socket, sys, time
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
                if os.environ.get('FAKE_SOCKET_DELAY'):
                    time.sleep(float(os.environ['FAKE_SOCKET_DELAY']))
                lock = None
                if os.environ.get('FAKE_LOCK_FILE'):
                    lock = open(os.environ['FAKE_LOCK_FILE'], 'a+')
                    fcntl.flock(lock, fcntl.LOCK_EX)
                path = listen[len('unix://'):] if listen.startswith('unix://') else listen
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                daemon = os.environ.get('FAKE_DAEMON_DIR')
                if daemon:
                    # Codex 0.157: bind in a private short-path daemon dir and
                    # leave a symlink at the requested --listen path.
                    real = os.path.join(daemon, 'bound.sock')
                    server.bind(real)
                    os.symlink(real, path)
                else:
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
                if lock: lock.close()
                if daemon:
                    os.unlink(real)
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
                import json, os, pathlib, signal, sys, time
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
                if os.environ.get('FAKE_CLIENT_WAIT'):
                    marker = os.environ.get('FAKE_CLIENT_TERM_MARKER')
                    if marker:
                        def delayed_end(*_args):
                            pathlib.Path(marker).write_text('terminating')
                            time.sleep(0.2)
                            raise SystemExit(0)
                        signal.signal(signal.SIGTERM, delayed_end)
                    while True: time.sleep(0.1)
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

    def test_signal_unwinds_entry_and_all_children(self) -> None:
        def children(pid):
            path = Path(f"/proc/{pid}/task/{pid}/children")
            try:
                return [int(part) for part in path.read_text().split()]
            except FileNotFoundError:
                return []

        for sig in (signal.SIGHUP, signal.SIGTERM):
            lock_path = self.base / "app.lock"
            marker = self.base / "client-terminating"
            with self.subTest(sig=sig), mock.patch.dict(os.environ, {
                    "FAKE_CLIENT_WAIT": "1", "FAKE_LOCK_FILE": str(lock_path),
                    "FAKE_CLIENT_TERM_MARKER": str(marker)}):
                proc = subprocess.Popen(self.command(), stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
                observed = {}
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if self.result.exists() and len(children(proc.pid)) >= 3:
                            break
                        time.sleep(0.02)
                    self.assertTrue(self.result.exists(), "client never became ready")
                    observed = {pid: Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[19]
                                for pid in children(proc.pid)}
                    self.assertEqual(len(observed), 3)
                    proc.send_signal(sig)
                    while time.monotonic() < deadline and not marker.exists():
                        time.sleep(0.01)
                    self.assertTrue(marker.exists(), "client was not terminated")
                    proc.send_signal(signal.SIGTERM)  # cleanup must survive a second signal
                    proc.wait(timeout=10)
                    self.assertEqual(proc.returncode, 128 + sig)
                    self.assertTrue(all(not Path(f"/proc/{pid}").exists() for pid in observed))
                    for name in ("app-server.sock", "managed-tui.sock", "managed-control.sock"):
                        self.assertFalse(os.path.lexists(self.state / name), name)
                    with lock_path.open("a+") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=5)
                    for pid, start in observed.items():
                        path = Path(f"/proc/{pid}/stat")
                        try:
                            if path.read_text().split(") ", 1)[1].split()[19] == start:
                                os.kill(pid, signal.SIGKILL)
                        except (FileNotFoundError, ProcessLookupError):
                            pass
                    self.result.unlink(missing_ok=True)
                    marker.unlink(missing_ok=True)
                    for name in ("app-server.sock", "managed-tui.sock", "managed-control.sock"):
                        (self.state / name).unlink(missing_ok=True)

    def test_signal_during_app_server_startup_cleans_partial_children(self) -> None:
        self.argv_log.unlink(missing_ok=True)
        with mock.patch.dict(os.environ, {"FAKE_SOCKET_DELAY": "2"}):
            proc = subprocess.Popen(self.command(), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            children = []
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    path = Path(f"/proc/{proc.pid}/task/{proc.pid}/children")
                    if path.exists():
                        children = [int(part) for part in path.read_text().split()]
                    if (children and self.argv_log.exists()
                            and '"--listen"' in self.argv_log.read_text(encoding="utf-8")):
                        break
                    time.sleep(0.01)
                self.assertTrue(children, "app-server was not started")
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)
                self.assertEqual(proc.returncode, 143)
                self.assertTrue(all(not Path(f"/proc/{pid}").exists() for pid in children))
                for name in ("app-server.sock", "managed-tui.sock", "managed-control.sock"):
                    self.assertFalse(os.path.lexists(self.state / name), name)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

    def test_signal_during_each_spawn_transfer_owns_child(self) -> None:
        spec = importlib.util.spec_from_file_location("managed_entry_transfer", ENTRY)
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        original_popen = subprocess.Popen
        child_script = self.base / "signal-disposition.py"
        child_script.write_text(textwrap.dedent("""\
            import json, pathlib, signal, sys, time
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK, [])
            pathlib.Path(sys.argv[1]).write_text(json.dumps({
                "hup_default": signal.getsignal(signal.SIGHUP) == signal.SIG_DFL,
                "term_default": signal.getsignal(signal.SIGTERM) == signal.SIG_DFL,
                "hup_unblocked": signal.SIGHUP not in blocked,
                "term_unblocked": signal.SIGTERM not in blocked,
            }))
            time.sleep(60)
        """), encoding="utf-8")
        args = entry.parser().parse_args(self.command()[2:])
        for target, sig in ((target, sig)
                            for target in ("app-server", "gateway", "client")
                            for sig in (signal.SIGHUP, signal.SIGTERM)):
            with self.subTest(target=target, sig=sig):
                marker = self.base / f"{target}-{sig}-signals.json"
                spawned = []
                count = 0

                def interrupted_popen(command, **options):
                    nonlocal count
                    count += 1
                    if count != {"app-server": 1, "gateway": 2, "client": 3}[target]:
                        child = original_popen(command, **options)
                    else:
                        child = original_popen(
                            [sys.executable, "-B", str(child_script), str(marker)],
                            **options,
                        )
                    spawned.append(child)
                    if count == {"app-server": 1, "gateway": 2, "client": 3}[target]:
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline and not marker.exists():
                            time.sleep(0.01)
                        self.assertTrue(marker.exists(), "child never reached exec")
                        os.kill(os.getpid(), sig)
                    return child

                try:
                    with mock.patch.object(entry, "check_runtime", return_value="enabled"), \
                            mock.patch.object(entry.subprocess, "Popen", side_effect=interrupted_popen):
                        self.assertEqual(entry.execute(args), 128 + sig)
                    self.assertEqual(json.loads(marker.read_text()), {
                        "hup_default": True, "term_default": True,
                        "hup_unblocked": True, "term_unblocked": True,
                    })
                    self.assertEqual(len(spawned), {"app-server": 1, "gateway": 2, "client": 3}[target])
                    self.assertTrue(all(child.poll() is not None for child in spawned))
                    for name in ("app-server.sock", "managed-tui.sock", "managed-control.sock"):
                        self.assertFalse(os.path.lexists(self.state / name), name)
                finally:
                    for child in spawned:
                        if child.poll() is None:
                            child.kill()
                        child.wait(timeout=5)
                    marker.unlink(missing_ok=True)

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

    def test_app_server_socket_behind_a_symlink_is_ready_and_cleaned(self) -> None:
        # 2026-09-27 (Codex 0.157.1): the App Server bound its socket under
        # /tmp/codex-daemon-<uid>/ and linked the requested path to it; the
        # entry timed out on the link and killed a healthy App Server.
        daemon = self.base / "daemon"
        daemon.mkdir(mode=0o700)
        with mock.patch.dict(os.environ, {"FAKE_DAEMON_DIR": str(daemon)}, clear=False):
            result = subprocess.run(self.command(), text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(self.result.exists())
        for name in ("app-server.sock", "managed-tui.sock", "managed-control.sock"):
            self.assertFalse(os.path.lexists(self.state / name), name)

    def test_socket_ready_accepts_only_a_link_to_our_private_socket(self) -> None:
        import importlib.util
        import socket
        spec = importlib.util.spec_from_file_location("codex_managed_entry_mod", ENTRY)
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        private = self.base / "private"
        private.mkdir(mode=0o700)
        shared = self.base / "shared"
        shared.mkdir()
        os.chmod(shared, 0o777)
        servers = []
        for directory in (private, shared):
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(directory / "s.sock"))
            servers.append(server)
        self.addCleanup(lambda: [server.close() for server in servers])
        (self.base / "file").write_text("x", encoding="utf-8")
        cases = {
            "direct-socket": (private / "s.sock", None, True),
            "link-private": (self.state / "a.sock", private / "s.sock", True),
            "link-shared-dir": (self.state / "b.sock", shared / "s.sock", False),
            "link-regular-file": (self.state / "c.sock", self.base / "file", False),
            "link-relative": (self.state / "d.sock", Path("s.sock"), False),
        }
        for name, (path, target, expected) in cases.items():
            with self.subTest(name):
                if target is not None:
                    os.symlink(target, path)
                self.assertEqual(entry.socket_ready(path), expected)

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
