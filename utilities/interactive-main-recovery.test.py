#!/usr/bin/env python3

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import socket
import shlex
import shutil
import subprocess
import sys
import time
import tempfile
import io
import contextlib
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("interactive_main_recovery", HERE / "interactive-main-recovery.py")
RECOVERY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RECOVERY)


def completed(stdout: str, code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["herdr"], code, stdout=stdout, stderr="")


class Host:
    """The checked Herdr host: source pane `w-1:p-1` alone in tab `w-1:t-1`."""

    def __init__(self, test, *, split=None, created=None, run=None, full=False):
        self.test, self.calls = test, []
        self.split = split if split is not None else test.split_payload
        self.created = created if created is not None else test.created_pane
        self.run, self.full = run, full

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        verb = command[1:3]
        if verb == ["pane", "list"] and "--workspace" not in command:
            return completed(self.test.payload)
        if verb == ["pane", "list"]:
            return completed(json.dumps({"result": {"panes": [
                {"pane_id": pane, "workspace_id": "w-1", "tab_id": "w-1:t-1"}
                for pane in ("w-1:p-1", self.created["pane_id"])]}}))
        if verb == ["pane", "layout"]:
            rects = [(0, 0, 100, 40), (100, 0, 100, 40), (0, 40, 100, 40), (100, 40, 100, 40)] if self.full else [(0, 0, 200, 80)]
            return completed(json.dumps({"result": {"layout": {
                "workspace_id": "w-1", "tab_id": "w-1:t-1", "area": {"x": 0, "y": 0, "width": 200, "height": 80},
                "panes": [{"pane_id": "w-1:p-1" if i == 0 else f"w-1:p-busy{i}",
                           "rect": dict(zip(("x", "y", "width", "height"), rect))}
                          for i, rect in enumerate(rects)]}}}))
        if verb == ["tab", "list"]:
            return completed(json.dumps({"result": {"tabs": [
                {"tab_id": "w-1:t-1", "workspace_id": "w-1", "number": 1}]}}))
        if verb == ["tab", "create"]:
            return completed(json.dumps({"result": {"root_pane": {"pane_id": "w-1:p-2", "focused": False}}}))
        if verb == ["pane", "split"]:
            return completed(self.split) if isinstance(self.split, str) else self.split
        if verb == ["pane", "get"] and command[3] == self.created["pane_id"]:
            return completed(json.dumps({"result": {"pane": self.created}}))
        if verb == ["pane", "get"]:
            return completed(json.dumps({"result": {"pane": {"pane_id": command[3], "agent": "codex"}}}))
        if verb == ["pane", "run"]:
            return self.run(command) if self.run else completed("{}")
        raise AssertionError(command)

    def commands(self):
        return [command for command, _kwargs in self.calls]


class InteractiveMainRecoveryTest(unittest.TestCase):
    def test_explicit_socket_is_used_for_every_host_request(self):
        host = Host(self)
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/wrong/server.sock",
                                          "AGENT_PEER_LEDGER_ROOT": str(self.root / "ledger")}, clear=True):
            RECOVERY.start(self.args(), runner=host, which=lambda _: "herdr")
        self.assertGreaterEqual(len(host.calls), 5)
        for command, kwargs in host.calls:
            self.assertEqual(kwargs.get("env", {}).get("HERDR_SOCKET_PATH"), str(self.socket_path))
        run = next(command for command in host.commands() if command[1:3] == ["pane", "run"])
        self.assertIn(str(self.launcher), run[-1])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.socket.close)
        self.socket_path = self.root / "herdr.sock"
        self.socket.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.launcher = self.root / "codex"
        self.launcher.write_text("#!/bin/sh\nexec python3 codex-launcher.py \"$@\"\n", encoding="utf-8")
        self.launcher.chmod(0o700)
        self.launcher_state = self.root / "codex-launcher.json"
        self.launcher_state.write_text(json.dumps({
            "schema": 2, "phase": "installed", "ingress_path": str(self.launcher),
            "ingress_sha256": hashlib.sha256(self.launcher.read_bytes()).hexdigest(),
        }), encoding="utf-8")
        self.launcher_state.chmod(0o600)
        self.payload = json.dumps({"result": {"panes": [{
            "pane_id": "w-1:p-1", "workspace_id": "w-1", "tab_id": "w-1:t-1", "cwd": "/tmp/project"
        }]}})
        self.created_pane = {
            "pane_id": "w-1:p-2", "workspace_id": "w-1", "tab_id": "w-1:t-1", "cwd": "/tmp/project"
        }
        self.split_payload = json.dumps({"result": {"pane": {"pane_id": "w-1:p-2", "focused": False}}})
        self.get_payload = json.dumps({"result": {"pane": self.created_pane}})
        ledger = mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": str(self.root / "ledger")})
        ledger.start()
        self.addCleanup(ledger.stop)

    def args(self, **changes):
        values = {
            "pane": "w-1:p-1", "name": "recovery", "workspace": "/tmp/project",
            "socket": str(self.socket_path), "launcher_state": str(self.launcher_state),
            "agent_args": [],
        }
        values.update(changes)
        return type("Args", (), values)()

    def test_check_reads_pane_inventory_only(self) -> None:
        calls: list[list[str]] = []

        def run(command, **kwargs):
            calls.append(command)
            return completed(self.payload)

        with mock.patch.dict("os.environ", {"HERDR_WORKSPACE_ID": "w-1", "HERDR_TAB_ID": "w-1:t-1"}, clear=True):
            result = RECOVERY.checked_pane(
                "w-1:p-1", workspace="/tmp/project", socket_path=str(self.socket_path),
                runner=run, which=lambda _: "herdr",
            )
        self.assertEqual(result["pane_id"], "w-1:p-1")
        self.assertEqual(calls, [["herdr", "pane", "list"]])

    def test_start_opens_an_unfocused_grid_pane_with_native_host_api(self) -> None:
        host = Host(self)
        with mock.patch.dict("os.environ", {"AGENT_PEER_LEDGER_ROOT": str(self.root / "ledger")}, clear=True):
            result = RECOVERY.start(self.args(), runner=host, which=lambda _: "/usr/bin/herdr")
        self.assertEqual(result["status"], "launch-requested")
        self.assertEqual((result["pane"]["pane_id"], result["placement"]), ("w-1:p-2", "split"))
        commands = host.commands()
        self.assertEqual(commands[0], ["herdr", "pane", "list"])
        split = next(command for command in commands if command[1:3] == ["pane", "split"])
        self.assertEqual(split, ["herdr", "pane", "split", "--pane", "w-1:p-1", "--direction", "right",
                                 "--ratio", "0.5", "--no-focus", "--cwd", "/tmp/project"])
        self.assertFalse(any("--focus" in command for command in commands))
        run = next(command for command in commands if command[1:3] == ["pane", "run"])
        self.assertEqual(run[3], "w-1:p-2")
        self.assertEqual(shlex.split(run[-1]), [str(self.launcher), "--cd", "/tmp/project"])
        self.assertFalse(any("tmux" in command for command in commands))

    def test_full_source_tab_opens_a_new_tab_in_the_same_workspace(self) -> None:
        created = dict(self.created_pane, tab_id="w-1:t-2")
        host = Host(self, created=created, full=True)
        with mock.patch.dict("os.environ", {"HERDR_TAB_ID": "w-1:t-1",
                                            "AGENT_PEER_LEDGER_ROOT": str(self.root / "ledger")}, clear=True):
            result = RECOVERY.start(self.args(), runner=host, which=lambda _: "/usr/bin/herdr")
        self.assertEqual((result["placement"], result["pane"]["tab_id"]), ("tab", "w-1:t-2"))
        create = next(command for command in host.commands() if command[1:3] == ["tab", "create"])
        self.assertIn("--no-focus", create)

    def test_foreign_tab_is_rejected(self) -> None:
        with self.assertRaises(RECOVERY.RecoveryError) as raised:
            with mock.patch.dict("os.environ", {"HERDR_WORKSPACE_ID": "w-1", "HERDR_TAB_ID": "other"}, clear=True):
                RECOVERY.checked_pane(
                    "w-1:p-1", workspace="/tmp/project", socket_path=str(self.socket_path),
                    runner=lambda _, **kwargs: completed(self.payload), which=lambda _: "herdr",
                )
        self.assertEqual(raised.exception.reason, "tab-mismatch")

    def test_missing_or_mismatched_cwd_is_rejected(self) -> None:
        for cwd, reason in ((None, "pane-cwd-missing"), ("/tmp/other", "pane-cwd-mismatch")):
            pane = {"pane_id": "w-1:p-1", "workspace_id": "w-1", "tab_id": "w-1:t-1"}
            if cwd is not None:
                pane["cwd"] = cwd
            payload = json.dumps({"result": {"panes": [pane]}})
            with self.subTest(cwd=cwd), mock.patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(RECOVERY.RecoveryError) as raised:
                    RECOVERY.checked_pane(
                        "w-1:p-1", workspace="/tmp/project", socket_path=str(self.socket_path),
                        runner=lambda _, **kwargs: completed(payload), which=lambda _: "herdr",
                    )
            self.assertEqual(raised.exception.reason, reason)

    def test_path_shadow_cannot_replace_protected_launcher(self) -> None:
        host = Host(self)
        with mock.patch.dict("os.environ", {"PATH": str(self.root),
                                            "AGENT_PEER_LEDGER_ROOT": str(self.root / "ledger")}, clear=True):
            RECOVERY.start(self.args(), runner=host, which=lambda _: str(self.root / "fake-herdr"))
        run = next(command for command in host.commands() if command[1:3] == ["pane", "run"])
        self.assertEqual(shlex.split(run[-1])[0], str(self.launcher))

    def test_split_failure_starts_no_agent(self) -> None:
        for split in (completed("split failed", code=7),
                      completed(json.dumps({"result": {"pane": {"pane_id": "w-1:p-2", "focused": True}}}))):
            host = Host(self, split=split)
            with self.subTest(split=split.stdout), mock.patch.dict(
                    "os.environ", {"AGENT_PEER_LEDGER_ROOT": str(self.root / "ledger")}, clear=True):
                with self.assertRaises(RECOVERY.RecoveryError) as raised:
                    RECOVERY.start(self.args(), runner=host, which=lambda _: "/usr/bin/herdr")
            self.assertEqual(raised.exception.reason, "herdr-pane-split-failed")
            self.assertFalse(any(command[1:3] == ["pane", "run"] for command in host.commands()))

    def test_post_open_validation_reports_created_pane(self) -> None:
        foreign = dict(self.created_pane, workspace_id="w-other")
        host = Host(self, created=foreign)
        with mock.patch.dict("os.environ", {"AGENT_PEER_LEDGER_ROOT": str(self.root / "ledger")}, clear=True):
            with self.assertRaises(RECOVERY.RecoveryError) as raised:
                RECOVERY.start(self.args(), runner=host, which=lambda _: "/usr/bin/herdr")
        self.assertEqual(raised.exception.reason, "workspace-mismatch")
        self.assertEqual(raised.exception.created_pane, "w-1:p-2")
        self.assertFalse(any(command[1:3] == ["pane", "run"] for command in host.commands()))

    def test_created_pane_in_the_wrong_directory_is_rejected(self) -> None:
        host = Host(self, created=dict(self.created_pane, cwd="/tmp/elsewhere"))
        with mock.patch.dict("os.environ", {"AGENT_PEER_LEDGER_ROOT": str(self.root / "ledger")}, clear=True):
            with self.assertRaises(RECOVERY.RecoveryError) as raised:
                RECOVERY.start(self.args(), runner=host, which=lambda _: "/usr/bin/herdr")
        self.assertEqual((raised.exception.reason, raised.exception.created_pane), ("pane-cwd-mismatch", "w-1:p-2"))

    def test_launch_transport_error_keeps_created_pane(self):
        def refuse(command):
            raise RECOVERY.RecoveryError("herdr-invocation-failed", "timed out")
        host = Host(self, run=refuse)
        with mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": str(self.root / "ledger")}, clear=True), \
             self.assertRaises(RECOVERY.RecoveryError) as raised:
            RECOVERY.start(self.args(), runner=host, which=lambda _: "herdr")
        self.assertEqual(raised.exception.created_pane, "w-1:p-2")

    def test_missing_host_location_is_rejected_before_mutation(self):
        payload = json.dumps({"result": {"panes": [{"pane_id": "w-1:p-1", "cwd": "/tmp/project"}]}})
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(RECOVERY.RecoveryError) as raised:
            RECOVERY.checked_pane("w-1:p-1", socket_path=str(self.socket_path),
                                  runner=lambda _, **kwargs: completed(payload), which=lambda _: "herdr")
        self.assertEqual(raised.exception.reason, "pane-location-missing")

    def test_post_open_read_failure_keeps_the_launch(self) -> None:
        def flaky(command, **kwargs):
            if command[1:3] == ["pane", "list"] and "--workspace" in command:
                raise RECOVERY.RecoveryError("herdr-invocation-failed", "timed out")
            return Host.__call__(host, command, **kwargs)
        host = Host(self)
        with mock.patch.dict("os.environ", {"AGENT_PEER_LEDGER_ROOT": str(self.root / "ledger")}, clear=True):
            result = RECOVERY.start(self.args(), runner=flaky, which=lambda _: "/usr/bin/herdr")
        self.assertEqual((result["status"], result["notes"]), ("launch-requested", ["pane-list-unconfirmed"]))
        self.assertTrue(any(command[1:3] == ["pane", "run"] for command in host.commands()))

    def test_full_tab_reuses_an_idle_shell_without_claiming_it(self) -> None:
        idle = {"pane_id": "w-1:p-busy3", "workspace_id": "w-1", "tab_id": "w-1:t-1", "cwd": "/tmp/elsewhere"}
        def run_shell(command, **kwargs):
            verb = command[1:3]
            if verb == ["pane", "get"] and command[3] == "w-1:p-busy3":
                return completed(json.dumps({"result": {"pane": idle}}))
            if verb == ["pane", "process-info"] and command[-1] == "w-1:p-busy3":
                return completed(json.dumps({"result": {"process_info": {
                    "pane_id": "w-1:p-busy3", "shell_pid": 7, "foreground_process_group_id": 7,
                    "foreground_processes": [{"pid": 7}]}}}))
            if verb == ["pane", "wait-output"]:
                return completed("{}")
            if verb == ["pane", "process-info"]:
                return completed(json.dumps({"error": {"code": "busy"}}), code=1)
            return Host.__call__(host, command, **kwargs)
        for refuse in (False, True):
            host = Host(self, full=True, run=(lambda command: completed("", code=7)) if refuse else None)
            with self.subTest(refuse=refuse), mock.patch.dict(
                    "os.environ", {"AGENT_PEER_LEDGER_ROOT": str(self.root / "ledger")}, clear=True):
                if refuse:
                    with self.assertRaises(RECOVERY.RecoveryError) as raised:
                        RECOVERY.start(self.args(), runner=run_shell, which=lambda _: "/usr/bin/herdr")
                    self.assertEqual((raised.exception.reason, raised.exception.created_pane),
                                     ("herdr-agent-start-failed", ""))
                else:
                    result = RECOVERY.start(self.args(), runner=run_shell, which=lambda _: "/usr/bin/herdr")
                    self.assertEqual((result["placement"], result["pane"]["pane_id"]), ("reuse", "w-1:p-busy3"))
                    run = next(c for c in host.commands() if c[1:3] == ["pane", "run"])
                    self.assertEqual((run[3], shlex.split(run[-1])[1:3]), ("w-1:p-busy3", ["--cd", "/tmp/project"]))
                self.assertFalse(any(c[1:3] in (["pane", "split"], ["tab", "create"]) for c in host.commands()))

    def test_agent_start_failure_is_typed(self) -> None:
        host = Host(self, run=lambda command: completed("", code=7))
        with mock.patch.dict("os.environ", {"AGENT_PEER_LEDGER_ROOT": str(self.root / "ledger")}, clear=True):
            with self.assertRaises(RECOVERY.RecoveryError) as raised:
                RECOVERY.start(self.args(), runner=host, which=lambda _: "/usr/bin/herdr")
        self.assertEqual(raised.exception.reason, "herdr-agent-start-failed")
        self.assertEqual(raised.exception.created_pane, "w-1:p-2")



@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TmuxRecoveryTest(unittest.TestCase):
    """The same checked recovery on a private tmux server named by its socket."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.socket = self.root / "r.sock"
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in ("TMUX", "TMUX_PANE", "HERDR_PANE_ID", "HERDR_SOCKET_PATH", "AGENT_HERDR_SESSION"):
            os.environ.pop(key, None)
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.root / "ledger")
        self.env = dict(os.environ, SHELL="/bin/sh")
        started = self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "r1", "-x", "200", "-y", "60",
                            "-c", str(self.workspace), "sh")
        if started.returncode:
            self.fail(f"isolated tmux server did not start: {started.stderr.strip()}")
        self.addCleanup(lambda: subprocess.run(["tmux", "-S", str(self.socket), "kill-server"],
                                               capture_output=True, env=self.env, timeout=10, check=False))
        self.pane = self.tmux("display-message", "-p", "-t", "r1", "#{pane_id}").stdout.strip()
        self.argv_file = self.root / "launched.txt"
        launcher = self.root / "codex"
        launcher.write_text("#!/bin/sh\n# stands in for codex-launcher.py\n"
                            f"printf '%s\\n' \"$@\" > {shlex.quote(str(self.argv_file))}\nexec sleep 600\n")
        launcher.chmod(0o700)
        self.state = self.root / "codex-launcher.json"
        self.state.write_text(json.dumps({"schema": 2, "phase": "installed", "ingress_path": str(launcher),
                                          "ingress_sha256": hashlib.sha256(launcher.read_bytes()).hexdigest()}))
        self.state.chmod(0o600)

    def tmux(self, *args):
        return subprocess.run(["tmux", "-S", str(self.socket), *args], capture_output=True, text=True,
                              env=self.env, timeout=10, check=False)

    def main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = RECOVERY.main([*argv, "--tmux-socket", str(self.socket)])
        return rc, json.loads(out.getvalue())

    def test_check_then_start_opens_one_unfocused_pane_and_runs_the_protected_launcher(self) -> None:
        session = self.tmux("display-message", "-p", "-t", self.pane, "#{session_id}").stdout.strip()
        window = self.tmux("display-message", "-p", "-t", self.pane, "#{window_id}").stdout.strip()
        rc, checked = self.main("--check", "--pane", self.pane, "--workspace", str(self.workspace))
        self.assertEqual(rc, 0, checked)
        self.assertEqual(checked["pane"], {"pane_id": self.pane, "workspace_id": session, "tab_id": window,
                                           "cwd": str(self.workspace.resolve()), "agent": None})
        rc, started = self.main("--start", "--pane", self.pane, "--workspace", str(self.workspace),
                                "--launcher-state", str(self.state))
        self.assertEqual(rc, 0, started)
        self.assertEqual((started["status"], started["surface"], started["placement"]),
                         ("launch-requested", "tmux", "split"))
        created = started["pane"]["pane_id"]
        self.assertNotEqual(created, self.pane)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.argv_file.exists():
            time.sleep(.05)
        self.assertEqual(self.argv_file.read_text().splitlines(), ["--cd", str(self.workspace.resolve())])
        active = self.tmux("display-message", "-p", "-t", "r1", "#{pane_id}").stdout.strip()
        self.assertEqual(active, self.pane)

    def test_a_synchronized_window_runs_no_launcher_line(self) -> None:
        self.tmux("set-option", "-w", "-t", self.pane, "synchronize-panes", "on")
        rc, result = self.main("--start", "--pane", self.pane, "--workspace", str(self.workspace),
                               "--launcher-state", str(self.state))
        self.assertEqual((rc, result["reason"]), (RECOVERY.EXIT_UNAVAILABLE, "tmux-agent-start-failed"))
        time.sleep(.3)
        self.assertFalse(self.argv_file.exists())
        typed = self.tmux("capture-pane", "-p", "-t", self.pane).stdout
        self.assertNotIn("launched.txt", typed)

    def test_a_pane_in_another_directory_or_an_unsafe_socket_opens_nothing(self) -> None:
        other = self.root / "other"
        other.mkdir()
        rc, result = self.main("--start", "--pane", self.pane, "--workspace", str(other),
                               "--launcher-state", str(self.state))
        self.assertEqual((rc, result["reason"]), (RECOVERY.EXIT_UNAVAILABLE, "pane-cwd-mismatch"))
        os.chmod(self.socket, 0o666)
        rc, result = self.main("--check", "--pane", self.pane, "--workspace", str(self.workspace))
        self.assertEqual((rc, result["reason"]), (RECOVERY.EXIT_UNAVAILABLE, "tmux-socket-mode-unsafe"))
        panes = self.tmux("list-panes", "-a", "-F", "#{pane_id}").stdout.split()
        self.assertEqual(panes, [self.pane])
        self.assertFalse(self.argv_file.exists())


if __name__ == "__main__":
    unittest.main()
