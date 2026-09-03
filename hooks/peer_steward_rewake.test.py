#!/usr/bin/env python3
"""A56-5 — Claude `asyncRewake` carrier for a detached steward watch.

Exercises the real `utilities/peer-steward.py` with only `herdr` faked, so the
hook's contract with the utility is tested rather than mocked.
"""
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

_HERE = Path(__file__).resolve().parent
_HOOK = _HERE / "peer-steward-rewake.py"
_PEER_STEWARD = _HERE.parent / "utilities" / "peer-steward.py"

_FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
mode = os.environ.get("FAKE_HERDR_MODE", "idle")
target = argv[2] if len(argv) > 2 else "-"
info = {"result": {"agent": {"agent": "claude", "agent_session": {"value": "sid-fake"},
        "agent_status": "idle", "name": target, "pane_id": "w1:p9"}, "type": "agent_info"}}
verb = argv[1] if len(argv) > 1 else ""
if verb == "wait" and mode == "held":
    with open(os.environ["FAKE_HERDR_FIFO"], "r") as fh:
        fh.read()
print(json.dumps(info)); sys.exit(0)
'''


def _reap_fixture_processes(marker, fifo):
    """review M4: kill watchers/fake-herdr children of this fixture, drain the FIFO."""
    import errno
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmdline = fh.read()
        except OSError:
            continue
        if marker.encode() not in cmdline or int(entry) == os.getpid():
            continue
        pid = int(entry)
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    if os.path.exists(fifo):
        for _ in range(64):
            try:
                fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                break
            try:
                os.write(fd, b"go")
            except OSError:
                pass
            os.close(fd)


class _HookMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.addCleanup(lambda: _reap_fixture_processes(str(self.root), str(self.root / "release.fifo")))
        self.jobs = self.root / "jobs.log"
        self.jobs.touch()
        self.bin = self.root / "fakebin"
        self.bin.mkdir()
        fake = self.bin / "herdr"
        fake.write_text(_FAKE_HERDR)
        fake.chmod(0o755)
        self.fifo = self.root / "release.fifo"
        os.mkfifo(self.fifo)
        self.session = "steward-hook-1"

    def _env(self, mode="idle", session_id=None):
        env = dict(os.environ)
        env["AGENT_DISPATCH_JOBS"] = str(self.jobs)
        env["FAKE_HERDR_MODE"] = mode
        env["FAKE_HERDR_FIFO"] = str(self.fifo)
        env["CLAUDE_CODE_SESSION_ID"] = session_id or self.session
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env.pop("AGENT_PEER_STEWARD_REWAKE_MAX_SECONDS", None)
        return env

    def _watch_root(self):
        out = subprocess.run(
            [sys.executable, str(_PEER_STEWARD), "status", "--json"],
            capture_output=True, text=True, env=self._env(),
        ).stdout
        return Path(json.loads(out)["watch_root"])

    def _arm(self, mode="idle", target="peer-a", wake="hook", session_id=None):
        proc = subprocess.run(
            [sys.executable, str(_PEER_STEWARD), "watch", target, "--wake", wake],
            capture_output=True, text=True, env=self._env(mode, session_id),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def _payload(self, armed_line, *, command=None, session_id=None):
        return {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": session_id or self.session,
            "tool_input": {"command": command or f"python3 {_PEER_STEWARD} watch peer-a --wake hook"},
            "tool_response": {"stdout": armed_line},
        }

    def _run_hook(self, payload, env=None, timeout=90):
        return subprocess.run(
            [sys.executable, str(_HOOK)], input=json.dumps(payload),
            capture_output=True, text=True, env=env or self._env(), timeout=timeout,
        )

    def _fields(self, text):
        out = {}
        for token in text.split():
            key, sep, value = token.partition("=")
            if sep:
                out[key] = value
        return out

    def _release(self):
        with open(self.fifo, "w") as fh:
            fh.write("go")


class ArmingTest(_HookMixin, unittest.TestCase):
    def test_non_watch_bash_call_is_silent_exit_zero(self):
        payload = self._payload("", command="ls -la")
        proc = self._run_hook(payload)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")

    def test_foreign_session_id_is_silent_exit_zero(self):
        armed = self._arm(mode="held")
        proc = self._run_hook(self._payload(armed, session_id="someone-else"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        watch_id = self._fields(armed)["watch_id"]
        self.assertFalse((self._watch_root() / f"{watch_id}.ack.json").exists())
        self._release()

    def test_wake_none_is_not_this_hooks_watch(self):
        armed = self._arm(mode="held", wake="none")
        proc = self._run_hook(self._payload(armed))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self._release()

    def test_unarmed_stdout_is_silent(self):
        proc = self._run_hook(self._payload("state=timeout agent=- session_id=- name=x pane=-"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")


class DeliveryTest(_HookMixin, unittest.TestCase):
    def test_receipt_acks_once_and_wakes_with_exit_two(self):
        armed = self._arm(mode="idle")
        watch_id = self._fields(armed)["watch_id"]
        proc = self._run_hook(self._payload(armed))

        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn(f"[peer-steward-rewake] watch_id={watch_id}", proc.stderr)
        self.assertIn("state=idle", proc.stderr)
        self.assertIn("target=peer-a", proc.stderr)
        self.assertIn("idle ≠ done", proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["systemMessage"].strip(), proc.stderr.strip())

        ack_path = self._watch_root() / f"{watch_id}.ack.json"
        ack = json.loads(ack_path.read_text())
        self.assertEqual(ack["carrier"], "claude-async-rewake")
        self.assertEqual(ack["session_id"], self.session)

        # A second carrier must not create a second ack.
        again = subprocess.run(
            [sys.executable, str(_PEER_STEWARD), "ack", watch_id, "--carrier", "userprompt-sweep"],
            capture_output=True, text=True, env=self._env(),
        )
        self.assertEqual(again.returncode, 0)
        self.assertIn("ack=already", again.stdout)
        self.assertEqual(json.loads(ack_path.read_text())["carrier"], "claude-async-rewake")

    def test_dead_watcher_rearms_exactly_once_then_delivers(self):
        armed = self._arm(mode="held")
        fields = self._fields(armed)
        os.kill(int(fields["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and Path(f"/proc/{fields['pid']}/stat").exists():
            time.sleep(0.02)

        proc = self._run_hook(self._payload(armed), env=self._env("idle"))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("rearmed=1", proc.stderr)
        self.assertIn("state=idle", proc.stderr)

        root = self._watch_root()
        rearmed = [
            json.loads(p.read_text()) for p in root.glob("*.json")
            if not p.name.endswith((".receipt.json", ".ack.json"))
        ]
        children = [a for a in rearmed if a.get("rearmed_from") == fields["watch_id"]]
        self.assertEqual(len(children), 1, "exactly one rearm")
        self.assertEqual(children[0]["rearm_count"], 1)

    def test_second_death_is_attention_not_another_rearm(self):
        armed = self._arm(mode="held")
        fields = self._fields(armed)
        os.kill(int(fields["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and Path(f"/proc/{fields['pid']}/stat").exists():
            time.sleep(0.02)

        # `herdr` missing -> the rearm cannot spawn a live watcher, so the hook
        # must stop at attention instead of rearming again.
        env = self._env("held")
        env["PATH"] = "/nonexistent"
        proc = self._run_hook(self._payload(armed), env=env)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("state=watcher-dead", proc.stderr)

    def test_join_outlives_the_short_subprocess_cap(self):
        # B1 regression: the join must not be cut by the utility subprocess cap.
        # Cap 1 s, release the target after 3 s, expect a real exit-2 wake.
        armed = self._arm(mode="held")
        env = self._env("held")
        env["AGENT_PEER_STEWARD_REWAKE_SUBPROCESS_SECONDS"] = "1"
        import threading
        threading.Timer(3.0, self._release).start()
        proc = self._run_hook(self._payload(armed), env=env, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("state=idle", proc.stderr)
        watch_id = self._fields(armed)["watch_id"]
        self.assertTrue((self._watch_root() / f"{watch_id}.ack.json").exists())

    def test_exhausted_budget_is_exit_two_hook_budget_expired(self):
        armed = self._arm(mode="held")
        env = self._env("held")
        env["AGENT_PEER_STEWARD_REWAKE_MAX_SECONDS"] = "61"   # 61 - 60 = 1s of join budget
        proc = self._run_hook(self._payload(armed), env=env, timeout=120)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("state=hook-budget-expired", proc.stderr)
        self.assertIn(self._fields(armed)["watch_id"], proc.stderr)
        self._release()


class BudgetArithmeticTest(unittest.TestCase):
    def test_default_join_budget_is_21_540_000_ms(self):
        spec = importlib.util.spec_from_file_location("peer_steward_rewake", str(_HOOK))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        seconds = module.BUDGET_SECONDS - module.BUDGET_MARGIN_SECONDS
        self.assertEqual(module.BUDGET_SECONDS, 21_600)
        self.assertEqual(seconds * 1000, 21_540_000)


if __name__ == "__main__":
    unittest.main()
