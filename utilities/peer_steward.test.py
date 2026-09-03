#!/usr/bin/env python3
"""Unit tests for utilities/peer-steward.py (SD-122 (9) steward wait/start)."""
import fcntl
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("peer_steward", str(_HERE / "peer-steward.py"))
peer_steward = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(peer_steward)


class _TmpRootMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_root = Path(self._tmp.name)
        self.jobs_path = self.tmp_root / "jobs.log"
        self.jobs_path.touch()
        self._old_environ = dict(os.environ)
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs_path)
        os.environ.pop("AGENT_HOME", None)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        os.environ.pop("CODEX_THREAD_ID", None)
        os.environ.pop("AGENT_SESSION_ID", None)
        self.addCleanup(self._restore_environ)

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self._old_environ)

    def _all_records(self):
        root = peer_steward.peer_message._ledger_root() / "peer-messages"
        if not root.is_dir():
            return []
        recs = []
        for month in root.glob("*"):
            for f in month.glob("*.jsonl"):
                for line in f.read_text().splitlines():
                    if line.strip():
                        recs.append(json.loads(line))
        return recs


def _herdr_json(payload):
    """Measured herdr 0.8.0 shape: success -> stdout + exit 0; an `error` payload
    -> STDERR + exit 1.  The fixture must reproduce the split, or a stdout-only
    reader passes every test and still misclassifies every real timeout."""
    if isinstance(payload, dict) and "error" in payload:
        return subprocess.CompletedProcess(["herdr"], 1, stdout="", stderr=json.dumps(payload))
    return subprocess.CompletedProcess(["herdr"], 0, stdout=json.dumps(payload), stderr="")


class WaitTest(_TmpRootMixin, unittest.TestCase):
    def _wait(self, target="fleet-cycle2", until=None, timeout=None, ref=None):
        argv = ["wait", target]
        for u in until or []:
            argv += ["--until", u]
        if timeout is not None:
            argv += ["--timeout", str(timeout)]
        for r in ref or []:
            argv += ["--ref", r]
        return peer_steward.main(argv)

    def test_idle_target_returns_zero_with_five_typed_fields(self):
        payload = {
            "result": {
                "agent": {
                    "agent": "claude", "agent_session": {"value": "sid-123"},
                    "agent_status": "idle", "name": "fleet-cycle2", "pane_id": "w1:pM",
                },
                "type": "agent_info",
            }
        }
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)) as run_mock:
            rc = self._wait()
        self.assertEqual(rc, 0)
        run_mock.assert_called_once()
        cmd = run_mock.call_args[0][0]
        self.assertEqual(cmd[:3], ["herdr", "agent", "wait"])

    def test_unrecognized_agent_status_is_normalized_to_unknown(self):
        payload = {
            "result": {
                "agent": {
                    "agent": "claude", "agent_session": {"value": "sid-123"},
                    "agent_status": "busy", "name": "fleet-cycle2", "pane_id": "w1:pM",
                },
                "type": "agent_info",
            }
        }
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)):
            rc = self._wait()
        self.assertEqual(rc, 0)
        line = print_mock.call_args[0][0]
        self.assertIn("state=unknown", line)

    def test_timeout_exit_3(self):
        payload = {"error": {"code": "timeout"}}
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)):
            rc = self._wait()
        self.assertEqual(rc, 3)

    def test_agent_not_found_exit_2(self):
        payload = {"error": {"code": "agent_not_found"}}
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)):
            rc = self._wait()
        self.assertEqual(rc, 2)

    def test_herdr_binary_missing_is_herdr_unavailable_exit_4(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value=None):
            rc = self._wait()
        self.assertEqual(rc, 4)

    def test_json_parse_failure_is_herdr_unavailable_exit_4(self):
        bad = subprocess.CompletedProcess(["herdr"], 0, stdout="not json", stderr="")
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=bad):
            rc = self._wait()
        self.assertEqual(rc, 4)

    def test_non_timeout_non_agent_not_found_error_code_is_herdr_unavailable(self):
        payload = {"error": {"code": "some-other-protocol-error"}}
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)):
            rc = self._wait()
        self.assertEqual(rc, 4)

    def test_subprocess_run_called_exactly_once_no_poll_loop(self):
        payload = {"error": {"code": "timeout"}}
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)) as run_mock:
            self._wait()
        self.assertEqual(run_mock.call_count, 1)

    def test_one_watch_record_written_at_wait_start_body_empty(self):
        payload = {"error": {"code": "timeout"}}
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)):
            self._wait(target="peer-a", ref=["rt-abc"])
        recs = self._all_records()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["kind"], "watch")
        self.assertEqual(rec["delivery"]["surface"], "herdr")
        self.assertEqual(rec["to"]["name"], "peer-a")
        self.assertEqual(rec["body_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(rec["refs"], ["rt-abc"])

    def test_watch_record_written_even_when_herdr_missing(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value=None):
            self._wait(target="peer-b")
        recs = self._all_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["kind"], "watch")

    def test_herdr_unavailable_names_a_fallback(self):
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_steward.shutil, "which", return_value=None):
            self._wait()
        line = print_mock.call_args[0][0]
        self.assertIn("herdr-unavailable", line)
        self.assertIn("fallback=", line)

    def test_claude_session_env_selects_claude_native_notify_idle_fallback(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-claude"
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_steward.shutil, "which", return_value=None):
            self._wait()
        line = print_mock.call_args[0][0]
        self.assertIn("fallback=claude-native-notify-idle", line)

    def test_no_claude_session_env_selects_poll_fallback(self):
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_steward.shutil, "which", return_value=None):
            self._wait()
        line = print_mock.call_args[0][0]
        self.assertIn("fallback=poll-fallback", line)


class StartTest(_TmpRootMixin, unittest.TestCase):
    def _start(self, name="peer-c", kind="claude", pane="w1:pM", permission_mode=None, agent_args=None):
        argv = ["start", name, "--kind", kind, "--pane", pane]
        if permission_mode:
            argv += ["--permission-mode", permission_mode]
        if agent_args:
            argv += ["--"] + list(agent_args)
        return peer_steward.main(argv)

    def test_name_is_the_positional_right_after_start(self):
        """herdr `agent start <NAME> --kind --pane`: the name is a required positional.
        Without it herdr answers `unknown option: claude` and starts nothing, while the
        wrapper still printed started=false and recorded a `[start]` steer (measured
        2026-09-03 during the F-100 comms test)."""
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(name="peer-c", kind="claude", pane="w1:pM")
        cmd = run_mock.call_args[0][0]
        self.assertEqual(cmd[:6], ["herdr", "agent", "start", "peer-c", "--kind", "claude"])
        self.assertEqual(cmd[6:8], ["--pane", "w1:pM"])

    def test_default_bypass_prepends_claude_flag(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            rc = self._start(kind="claude")
        self.assertEqual(rc, 0)
        cmd = run_mock.call_args[0][0]
        self.assertIn("--permission-mode", cmd)
        self.assertIn("bypassPermissions", cmd)

    def test_default_bypass_prepends_codex_flag(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="codex")
        cmd = run_mock.call_args[0][0]
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_default_bypass_prepends_opencode_flag(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="opencode")
        cmd = run_mock.call_args[0][0]
        self.assertIn("--auto", cmd)

    def test_inherit_prepends_zero_flags(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="claude", permission_mode="inherit")
        cmd = run_mock.call_args[0][0]
        self.assertNotIn("bypassPermissions", cmd)
        self.assertNotIn("--permission-mode", cmd)

    def test_record_and_typed_line_emitted(self):
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")):
            self._start(name="peer-d", kind="claude")
        line = print_mock.call_args[0][0]
        self.assertIn("started=true", line)
        self.assertIn("agent=claude", line)
        self.assertIn("name=peer-d", line)
        recs = self._all_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["kind"], "steer")
        self.assertEqual(recs[0]["summary"], "[start] peer-d kind=claude mode=bypass")
        self.assertEqual(recs[0]["delivery"]["surface"], "herdr")

    def test_herdr_missing_exit_4(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value=None):
            rc = self._start()
        self.assertEqual(rc, 4)

    def test_started_false_when_herdr_start_fails(self):
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="boom")):
            rc = self._start()
        self.assertEqual(rc, 0)
        line = print_mock.call_args[0][0]
        self.assertIn("started=false", line)

    def test_agent_args_pass_through_after_prefix_flags(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="claude", agent_args=["--extra-flag"])
        cmd = run_mock.call_args[0][0]
        self.assertIn("--extra-flag", cmd)
        self.assertLess(cmd.index("bypassPermissions"), cmd.index("--extra-flag"))



# ---------------------------------------------------------------------------
# SD-122 (10) detached steward watch — A56-1..A56-4, A56-7
# ---------------------------------------------------------------------------

_FAKE_HERDR = r'''#!/usr/bin/env python3
"""Fake herdr 0.8.0. Reproduces the measured stdout/stderr + exit-code split."""
import json, os, sys

argv = sys.argv[1:]
log = os.environ.get("FAKE_HERDR_CALLLOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(" ".join(argv) + "\n")

mode = os.environ.get("FAKE_HERDR_MODE", "idle")
target = argv[2] if len(argv) > 2 else "-"
info = {"id": "cli:agent:wait", "result": {"agent": {
    "agent": "claude", "agent_session": {"value": "sid-fake"},
    "agent_status": "idle", "name": target, "pane_id": "w1:p9"}, "type": "agent_info"}}

def fail(code):
    sys.stderr.write(json.dumps({"error": {"code": code, "message": code}}))
    sys.exit(1)

verb = argv[1] if len(argv) > 1 else ""
if verb == "get" and mode == "hang-get":
    with open(os.environ["FAKE_HERDR_FIFO"], "r") as fh:   # wedged socket
        fh.read()
if mode == "not-found":
    fail("agent_not_found")
if verb == "get":
    print(json.dumps(info)); sys.exit(0)
if verb == "wait":
    if mode == "timeout":
        fail("timeout")
    if mode == "held":
        fifo = os.environ["FAKE_HERDR_FIFO"]
        with open(fifo, "r") as fh:   # blocks until the test writes: an event, not a poll
            fh.read()
    print(json.dumps(info)); sys.exit(0)
print(json.dumps(info)); sys.exit(0)
'''



def _reap_fixture_processes(marker, fifo=None):
    """Kill every watcher (session leader -> its group holds the fake herdr) and
    every leftover fake-herdr child whose cmdline carries this fixture's path,
    then drain the FIFO so no reader stays blocked on an unlinked pipe
    (review M4: three tests SIGKILLed the watcher without releasing the FIFO and
    the reparented `herdr agent wait` child survived until reboot)."""
    import errno
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmdline = fh.read()
        except OSError:
            continue
        if marker.encode() not in cmdline:
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    if fifo and os.path.exists(fifo):
        for _ in range(64):
            try:
                fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    break
                break
            try:
                os.write(fd, b"go")
            except OSError:
                pass
            os.close(fd)

class _WatchMixin(_TmpRootMixin):
    """Real subprocesses, real files, only `herdr` faked."""

    def setUp(self):
        super().setUp()
        # Registered before the tmp-dir cleanup so it runs first (LIFO).
        self.addCleanup(lambda: _reap_fixture_processes(str(self.tmp_root), str(self.tmp_root / "release.fifo")))
        self.bin = self.tmp_root / "fakebin"
        self.bin.mkdir()
        fake = self.bin / "herdr"
        fake.write_text(_FAKE_HERDR)
        fake.chmod(0o755)
        self.calllog = self.tmp_root / "calls.log"
        self.fifo = self.tmp_root / "release.fifo"
        os.mkfifo(self.fifo)
        self.watch_root = peer_steward.peer_message._ledger_root() / "peer-watches"

    def _env(self, mode="idle", session_id="steward-1", with_herdr=True):
        env = dict(os.environ)
        env["AGENT_DISPATCH_JOBS"] = str(self.jobs_path)
        env["FAKE_HERDR_MODE"] = mode
        env["FAKE_HERDR_CALLLOG"] = str(self.calllog)
        env["FAKE_HERDR_FIFO"] = str(self.fifo)
        env["CLAUDE_CODE_SESSION_ID"] = session_id
        base = env.get("PATH", "")
        env["PATH"] = (str(self.bin) + os.pathsep + base) if with_herdr else "/nonexistent"
        return env

    def _run(self, *argv, env=None, timeout=30, **kw):
        return subprocess.run(
            [sys.executable, str(_HERE / "peer-steward.py"), *argv],
            capture_output=True, text=True, env=env or self._env(), timeout=timeout, **kw
        )

    def _fields(self, line):
        out = {}
        for token in line.strip().split():
            key, sep, value = token.partition("=")
            if sep:
                out[key] = value
        return out

    def _release(self):
        with open(self.fifo, "w") as fh:
            fh.write("go")

    def _wait_for_lock(self, watch_id, seconds=30):
        """Bound-wait until the watcher has actually taken its flock.

        `watch` returns as soon as the watcher is spawned, so for a few hundred
        milliseconds the watch is legitimately `armed` and not yet `alive` --
        that ordering is the whole reason `join` must not decide `watcher-dead`
        from lock acquisition alone.
        """
        lock = self.watch_root / f"{watch_id}.lock"
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if peer_steward._lock_held(lock):
                return
            time.sleep(0.02)
        self.fail(f"watcher never took its lock for {watch_id}")

    def _wait_for_receipt(self, watch_id, seconds=30):
        """Bound-wait in the TEST (allowed); the product waits on events only."""
        path = self.watch_root / f"{watch_id}.receipt.json"
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if path.exists():
                return json.loads(path.read_text())
            time.sleep(0.02)
        self.fail(f"receipt never appeared for {watch_id}")


class WatchArmTest(_WatchMixin, unittest.TestCase):
    """A56-1."""

    def test_arm_emits_typed_line_immutable_record_and_one_ledger_row(self):
        proc = self._run("watch", "peer-a", "--wake", "hook", env=self._env("held"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fields = self._fields(proc.stdout)
        self.assertEqual(fields["state"], "armed")
        self.assertEqual(fields["target"], "peer-a")
        self.assertEqual(fields["wake"], "hook")
        for key in ("watch_id", "until", "pid", "pid_start", "receipt"):
            self.assertIn(key, fields)
        self.assertEqual(len(fields["watch_id"]), 16)

        arm = json.loads((self.watch_root / f"{fields['watch_id']}.json").read_text())
        self.assertEqual(arm["watch_id"], fields["watch_id"])
        self.assertEqual(arm["steward"]["session_id"], "steward-1")
        self.assertEqual(arm["watcher"]["pid"], int(fields["pid"]))
        self.assertTrue(arm["watcher"]["pid_start"])

        rows = [r for r in self._all_records() if r["kind"] == "watch"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delivery"]["surface"], "herdr")
        self.assertEqual(rows[0]["delivery"]["status"], "sent")
        self.assertEqual(rows[0]["delivery"]["receipt"], fields["watch_id"])

        self._release()
        self._wait_for_receipt(fields["watch_id"])

    def test_watcher_runs_in_its_own_session_not_the_callers(self):
        # `setsid`, proven exactly -- `os.getsid`, never a /proc/<pid>/stat field
        # index (comm can contain spaces, and the session id is not field 6 of
        # the whitespace split).
        proc = self._run("watch", "peer-a", env=self._env("held"))
        pid = int(self._fields(proc.stdout)["pid"])
        self.assertNotEqual(os.getsid(pid), os.getsid(os.getpid()))
        self.assertEqual(os.getsid(pid), pid)
        self._release()
        self._wait_for_receipt(self._fields(proc.stdout)["watch_id"])

    def test_duplicate_arm_spawns_nothing(self):
        first = self._run("watch", "peer-a", env=self._env("held"))
        watch_id = self._fields(first.stdout)["watch_id"]
        self._wait_for_lock(watch_id)
        before = sorted(self.watch_root.glob("*.json"))
        second = self._run("watch", "peer-a", env=self._env("held"))
        self.assertEqual(second.returncode, 0)
        fields = self._fields(second.stdout)
        self.assertEqual(fields["state"], "already-armed")
        self.assertEqual(fields["watch_id"], watch_id)
        self.assertEqual(sorted(self.watch_root.glob("*.json")), before)
        self._release()
        self._wait_for_receipt(watch_id)

    def test_duplicate_arm_holds_during_the_spawn_latency_window(self):
        """The regression the E2 fix exists for.

        The second `watch` runs before the first watcher has taken its flock, so
        a lock-based liveness test reports the healthy watch as dead and spawns a
        duplicate. Dedupe must decide on PID identity alone.
        """
        first = self._run("watch", "peer-a", env=self._env("held"))
        watch_id = self._fields(first.stdout)["watch_id"]
        # The lock is held from before `watch` returned -- the caller takes it
        # and hands the open description to the watcher, so no window exists in
        # which a healthy watch looks unlocked.
        self.assertTrue(peer_steward._lock_held(self.watch_root / f"{watch_id}.lock"))
        second = self._run("watch", "peer-a", env=self._env("held"))
        self.assertEqual(self._fields(second.stdout)["state"], "already-armed")
        self.assertEqual(len(list(self.watch_root.glob("*.json"))), 1)
        self._release()
        self._wait_for_receipt(watch_id)

    def test_herdr_missing_is_exit_4_with_zero_watch_files(self):
        proc = self._run("watch", "peer-a", env=self._env(with_herdr=False))
        self.assertEqual(proc.returncode, 4)
        self.assertIn("herdr-unavailable", proc.stdout)
        self.assertEqual(list(self.watch_root.glob("*.json")) if self.watch_root.is_dir() else [], [])

    def test_absent_target_is_exit_2_with_zero_watch_files(self):
        proc = self._run("watch", "ghost", env=self._env("not-found"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("state=agent-not-found", proc.stdout)
        self.assertEqual(list(self.watch_root.glob("*.json")), [])

    def test_product_modules_contain_no_sleep_or_poll_loop(self):
        """The user-facing constraint, asserted rather than left to review.

        Checked against the AST, not the raw text: a comment explaining why a
        loop is bounded must not be able to fail the guard, and a `sleep`
        hidden in a string must not be able to pass it.
        """
        import ast

        for name in ("peer-steward.py", "../hooks/peer-steward-rewake.py"):
            path = (_HERE / name).resolve()
            self.assertTrue(path.exists(), f"{name} missing")
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    dotted = (
                        isinstance(func, ast.Attribute) and func.attr == "sleep"
                    ) or (isinstance(func, ast.Name) and func.id == "sleep")
                    self.assertFalse(dotted, f"{path.name} must not sleep")
                if isinstance(node, ast.While):
                    test = node.test
                    unbounded = isinstance(test, ast.Constant) and test.value is True
                    self.assertFalse(unbounded, f"{path.name} must not poll-loop")


class WatcherReceiptTest(_WatchMixin, unittest.TestCase):
    """A56-2."""

    def test_receipt_is_atomic_complete_and_text_free(self):
        proc = self._run("watch", "peer-a", env=self._env("held"))
        watch_id = self._fields(proc.stdout)["watch_id"]
        self._release()
        receipt = self._wait_for_receipt(watch_id)

        self.assertEqual(set(receipt), {
            "schema_version", "watch_id", "target", "steward", "armed_ts", "done_ts",
            "state", "agent", "herdr_exit", "watcher", "rearmed_from", "refs",
        })
        self.assertEqual(receipt["state"], "idle")
        self.assertEqual(receipt["steward"]["session_id"], "steward-1")
        self.assertEqual(receipt["agent"]["name"], "peer-a")
        for key in receipt:
            self.assertNotRegex(key, r"body|text|screen|output")

        waits = [l for l in self.calllog.read_text().splitlines() if " wait " in f" {l} "]
        self.assertEqual(len(waits), 1, self.calllog.read_text())

        notices = [r for r in self._all_records() if r["kind"] == "notice"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["delivery"]["status"], "received")
        self.assertEqual(notices[0]["delivery"]["receipt"], watch_id)
        self.assertEqual(notices[0]["from"]["session_id"], "steward-1")

        lock = self.watch_root / f"{watch_id}.lock"
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # released by exit
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def test_uninterpretable_herdr_yields_a_receipt_not_an_endless_wait(self):
        proc = self._run("watch", "peer-a", env=self._env("timeout"))
        watch_id = self._fields(proc.stdout)["watch_id"]
        self.assertEqual(self._wait_for_receipt(watch_id)["state"], "timeout")


class JoinTest(_WatchMixin, unittest.TestCase):
    """A56-3."""

    def test_existing_receipt_returns_immediately_even_at_timeout_zero(self):
        armed = self._run("watch", "peer-a", env=self._env("idle"))
        watch_id = self._fields(armed.stdout)["watch_id"]
        self._wait_for_receipt(watch_id)
        proc = self._run("join", watch_id, "--timeout", "0")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        fields = self._fields(proc.stdout)
        self.assertEqual(fields["state"], "idle")
        self.assertEqual(fields["watch_id"], watch_id)
        self.assertTrue(fields["receipt"].endswith(f"{watch_id}.receipt.json"))

    def test_join_returns_on_the_watcher_exit_event(self):
        armed = self._run("watch", "peer-a", env=self._env("held"))
        watch_id = self._fields(armed.stdout)["watch_id"]
        joiner = subprocess.Popen(
            [sys.executable, str(_HERE / "peer-steward.py"), "join", watch_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self._env("held"),
        )
        self._release()
        out, err = joiner.communicate(timeout=30)   # the TEST bound-waits
        self.assertEqual(joiner.returncode, 0, out + err)
        self.assertEqual(self._fields(out)["state"], "idle")

    def test_killed_watcher_is_watcher_dead_exit_5(self):
        armed = self._run("watch", "peer-a", env=self._env("held"))
        fields = self._fields(armed.stdout)
        os.kill(int(fields["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and peer_steward._pid_identity_ok(
            int(fields["pid"]), fields["pid_start"]
        ):
            time.sleep(0.02)
        proc = self._run("join", fields["watch_id"], "--timeout", "5000")
        self.assertEqual(proc.returncode, 5, proc.stdout + proc.stderr)
        self.assertIn("state=watcher-dead", proc.stdout)

    def test_bounded_join_that_misses_is_timeout_exit_6_with_the_watcher_alive(self):
        armed = self._run("watch", "peer-a", env=self._env("held"))
        fields = self._fields(armed.stdout)
        started = time.monotonic()
        proc = self._run("join", fields["watch_id"], "--timeout", "700")
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 6, proc.stdout + proc.stderr)
        self.assertIn("state=join-timeout", proc.stdout)
        self.assertLess(elapsed, 15, "the SIGALRM bound must actually fire")
        self.assertTrue(peer_steward._pid_identity_ok(int(fields["pid"]), fields["pid_start"]))
        self._release()
        self._wait_for_receipt(fields["watch_id"])


class StatusRearmTest(_WatchMixin, unittest.TestCase):
    """A56-4."""

    def _arm(self, target="peer-a", mode="held", session_id="steward-1"):
        proc = self._run("watch", target, env=self._env(mode, session_id=session_id))
        return self._fields(proc.stdout)

    def test_alive_needs_pid_starttime_and_lock_together(self):
        fields = self._arm()
        self._wait_for_lock(fields["watch_id"])
        arm = json.loads((self.watch_root / f"{fields['watch_id']}.json").read_text())
        self.assertTrue(peer_steward._alive(arm, self.watch_root), "live watcher holding the lock")

        # a reaped pid -> dead
        dead = dict(arm, watch_id=arm["watch_id"], watcher={"pid": 2 ** 22 - 1, "pid_start": "1"})
        self.assertFalse(peer_steward._alive(dead, self.watch_root))

        # alive pid whose start ticks do not match -> dead
        skewed = dict(arm, watcher={"pid": arm["watcher"]["pid"], "pid_start": "999999999"})
        self.assertFalse(peer_steward._alive(skewed, self.watch_root))

        # a live process that never flocks -> DEAD. This is the fixture that
        # proves the lock condition is really ANDed and not decorative.
        idle = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                stdin=subprocess.PIPE)
        self.addCleanup(idle.kill)
        never_locks = {
            "watch_id": "deadbeefdeadbeef", "target": "x",
            "watcher": {"pid": idle.pid, "pid_start": peer_steward.process_start_ticks(idle.pid)},
        }
        self.assertTrue(peer_steward._pid_identity_ok(idle.pid, never_locks["watcher"]["pid_start"]))
        self.assertFalse(peer_steward._is_zombie(idle.pid))
        self.assertFalse(peer_steward._alive(never_locks, self.watch_root))

        self._release()
        self._wait_for_receipt(fields["watch_id"])

    def test_undelivered_lists_only_this_sessions_unacked_receipts(self):
        mine = self._arm(mode="idle")
        self._wait_for_receipt(mine["watch_id"])
        other = self._arm(target="peer-b", mode="idle", session_id="steward-2")
        self._wait_for_receipt(other["watch_id"])

        proc = self._run("status", "--undelivered", "--json")
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["watch_root"], str(self.watch_root))
        ids = [row["watch_id"] for row in payload["watches"]]
        self.assertEqual(ids, [mine["watch_id"]])

        self.assertEqual(self._run("ack", mine["watch_id"], "--carrier", "t").returncode, 0)
        after = json.loads(self._run("status", "--undelivered", "--json").stdout)
        self.assertEqual(after["watches"], [])

    def test_ack_is_created_once_then_silently_skipped(self):
        fields = self._arm(mode="idle")
        self._wait_for_receipt(fields["watch_id"])
        first = self._run("ack", fields["watch_id"], "--carrier", "claude-async-rewake")
        second = self._run("ack", fields["watch_id"], "--carrier", "userprompt-sweep")
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertIn("ack=created", first.stdout)
        self.assertIn("ack=already", second.stdout)
        ack = json.loads((self.watch_root / f"{fields['watch_id']}.ack.json").read_text())
        self.assertEqual(ack["carrier"], "claude-async-rewake")
        self.assertEqual(ack["session_id"], "steward-1")

    def test_rearm_only_replaces_a_dead_unreceipted_watch(self):
        done = self._arm(mode="idle")
        self._wait_for_receipt(done["watch_id"])
        self.assertIn("state=already-done", self._run("rearm", done["watch_id"]).stdout)

        live = self._arm(target="peer-live", mode="held")
        self._wait_for_lock(live["watch_id"])
        self.assertIn("state=alive", self._run("rearm", live["watch_id"]).stdout)

        dead = self._arm(target="peer-dead", mode="held")
        os.kill(int(dead["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and peer_steward._pid_identity_ok(
            int(dead["pid"]), dead["pid_start"]
        ):
            time.sleep(0.02)
        out = self._run("rearm", dead["watch_id"], env=self._env("held")).stdout
        fields = self._fields(out)
        self.assertEqual(fields["state"], "rearmed")
        self.assertEqual(fields["rearmed_from"], dead["watch_id"])
        self.assertNotEqual(fields["watch_id"], dead["watch_id"])
        new_arm = json.loads((self.watch_root / f"{fields['watch_id']}.json").read_text())
        self.assertEqual(new_arm["rearmed_from"], dead["watch_id"])
        self.assertEqual(new_arm["rearm_count"], 1)

        self._release()
        self._release()


class ReviewRoundOneTest(_WatchMixin, unittest.TestCase):
    def test_rearm_keeps_the_original_steward_identity_without_env(self):
        # M1: a rearm issued from a process that carries no session id (the
        # hook) must not rewrite steward.session_id to "".
        dead = self._fields(self._run("watch", "peer-a", env=self._env("held")).stdout)
        os.kill(int(dead["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and peer_steward._pid_identity_ok(
            int(dead["pid"]), dead["pid_start"]
        ):
            time.sleep(0.02)
        env = self._env("held")
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        out = self._run("rearm", dead["watch_id"], env=env).stdout
        fields = self._fields(out)
        self.assertEqual(fields["state"], "rearmed", out)
        new_arm = json.loads((self.watch_root / f"{fields['watch_id']}.json").read_text())
        self.assertEqual(new_arm["steward"]["session_id"], "steward-1")
        self.assertEqual(new_arm["steward"]["harness"], "claude")
        # Same dedupe key: a third `watch` for the target is `already-armed`, not a second watcher.
        again = self._run("watch", "peer-a", env=self._env("held"))
        self.assertIn("state=already-armed", again.stdout)
        self._release()

    def test_hanging_herdr_get_is_herdr_unavailable_not_a_hang(self):
        # M3: a wedged `herdr agent get` must return exit 4 within the bound.
        env = self._env("hang-get")
        env["AGENT_PEER_STEWARD_HERDR_GET_TIMEOUT"] = "1"
        started = time.monotonic()
        proc = self._run("watch", "peer-a", env=env, timeout=30)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("herdr-unavailable", proc.stdout)
        self.assertEqual(list(self.watch_root.glob("*.json")), [])

    def test_receipt_carries_the_real_herdr_exit_code(self):
        # m3: a timeout from herdr exits 1; the receipt must say so.
        armed = self._fields(self._run("watch", "peer-a", env=self._env("timeout")).stdout)
        receipt = self._wait_for_receipt(armed["watch_id"])
        self.assertEqual(receipt["state"], "timeout")
        self.assertEqual(receipt["herdr_exit"], 1)


class KillFixtureTest(_WatchMixin, unittest.TestCase):
    """A56-7 — the smallest falsifying test for the whole direction."""

    def _spawn_caller(self, subcommand):
        script = (
            "import subprocess,sys;"
            f"p=subprocess.run([sys.executable,{str(_HERE / 'peer-steward.py')!r},"
            f"{subcommand!r},'peer-a'],capture_output=True,text=True);"
            "open(%r,'w').write(p.stdout);" % str(self.tmp_root / "caller.out")
            + "import time;time.sleep(300)"
        )
        return subprocess.Popen([sys.executable, "-c", script], env=self._env("held"),
                                start_new_session=True)

    def test_watcher_survives_sigkill_of_the_callers_process_group(self):
        caller = self._spawn_caller("watch")
        # Read the watcher identity from disk, not from the caller's stdout:
        # a killed caller's stdout may never be flushed.
        deadline = time.monotonic() + 30
        arm = None
        while time.monotonic() < deadline:
            arms = [p for p in self.watch_root.glob("*.json")] if self.watch_root.is_dir() else []
            if arms:
                arm = json.loads(arms[0].read_text())
                break
            time.sleep(0.02)
        self.assertIsNotNone(arm, "watch never armed")
        pid, pid_start = arm["watcher"]["pid"], arm["watcher"]["pid_start"]

        os.killpg(os.getpgid(caller.pid), signal.SIGKILL)
        caller.wait(timeout=10)

        self.assertTrue(peer_steward._pid_identity_ok(pid, pid_start),
                        "the watcher must outlive its caller's process group")
        self._release()
        receipt = self._wait_for_receipt(arm["watch_id"])
        self.assertEqual(receipt["state"], "idle")

    def test_wait_by_contrast_loses_its_herdr_child(self):
        # The regression proof: the identical fixture against (9) `wait`.
        caller = self._spawn_caller("wait")
        deadline = time.monotonic() + 30
        child = None
        while time.monotonic() < deadline and child is None:
            for entry in Path("/proc").glob("[0-9]*"):
                try:
                    if entry.joinpath("comm").read_text().strip() != "herdr":
                        continue
                    stat = entry.joinpath("stat").read_text()
                    if str(caller.pid) in stat.split(")")[-1].split()[2:4]:
                        child = int(entry.name)
                        break
                except OSError:
                    continue
            if child is None:
                time.sleep(0.02)
        os.killpg(os.getpgid(caller.pid), signal.SIGKILL)
        caller.wait(timeout=10)
        if child is not None:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and Path(f"/proc/{child}").exists():
                time.sleep(0.02)
            self.assertFalse(Path(f"/proc/{child}").exists(),
                             "`wait`'s herdr child must die with the caller's group")
        self.assertEqual(list(self.watch_root.glob("*.receipt.json"))
                         if self.watch_root.is_dir() else [], [],
                         "`wait` leaves no receipt -- that is the defect (10) fixes")

if __name__ == "__main__":
    unittest.main()
