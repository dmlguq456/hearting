#!/usr/bin/env python3
"""A56-5 — Claude `asyncRewake` carrier for a detached steward watch.

Exercises the real `utilities/peer-steward.py` against a real native session (a
process the runtime's own registry record names), so the hook's contract with the
utility is tested rather than mocked. No Herdr is installed, reachable or faked: a
watch reads the target's native state and the shared runner publishes the change.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_HOOK = _HERE / "peer-steward-rewake.py"
_PEER_STEWARD = _HERE.parent / "utilities" / "peer-steward.py"
sys.path.insert(0, str(_HERE.parent / "tools"))
sys.path.insert(0, str(_HERE.parent / "utilities"))
import fixture_processes  # noqa: E402

class _HookMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # Reap detached watches before the target session and temporary state go away.
        self.addCleanup(fixture_processes.reap, str(self.root))
        self.jobs = self.root / "peer-state" / "jobs.log"
        self.jobs.parent.mkdir(parents=True)
        self.jobs.touch()
        self.session = "steward-hook-1"
        # The one session every watch below observes. It is real: a process the
        # production readers see plus the runtime's own registry record. No Herdr.
        self.target = fixture_processes.NativeSession(self, sid="sid-fixture", home=self.root / "home",
                                    name="peer-a")

    def _wake_runner(self):
        """The shared runner reads native state when it is woken; nothing polls."""
        import peer_obligations
        import shared_readiness
        root = self.root / "peer-state" / "peer-steward" / "obligations"
        shared_readiness.wake_runner(peer_obligations.ObligationStore(root).root)

    def _env(self, mode=None, session_id=None):
        env = dict(os.environ)
        # Drop inherited runtime/session/dispatch identities before supplying
        # fixture values; a worker runtime's own session id must never become
        # the fixture steward identity.
        for key in tuple(env):
            if key.startswith(("AGENT_DISPATCH_", "AGENT_SESSION_", "AGENT_RUNTIME_",
                               "AGENT_THREAD_", "HERDR_")):
                env.pop(key, None)
        for key in ("AGENT_SESSION_ID", "AGENT_SESSION_ROLE", "CLAUDE_CODE_SESSION_ID",
                    "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
                    "OPENCODE_SESSION_ID", "OPENCODE_DISPATCH_SLUG",
                    "AGENT_HERDR_SESSION"):
            env.pop(key, None)
        env["AGENT_DISPATCH_JOBS"] = str(self.jobs)
        # C-1 moved the peer ledger (and _watch_root(), which watch/status read/write
        # through) off the AGENT_DISPATCH_JOBS-anchored resolver onto peer_state_root();
        # isolating only AGENT_DISPATCH_JOBS no longer keeps this fixture's watch
        # records out of the real per-user ledger root.
        env["AGENT_PEER_LEDGER_ROOT"] = str(self.root / "peer-state")
        env["HOME"] = str(self.root / "home")
        env["CLAUDE_CONFIG_DIR"] = str(self.root / "home" / ".claude")
        env["XDG_STATE_HOME"] = str(self.root / "state")
        env["XDG_CONFIG_HOME"] = str(self.root / "config")
        env["XDG_DATA_HOME"] = str(self.root / "data")
        # `held` is a target mid-turn: the watch only notifies on the change to idle.
        # No mode leaves the target as it is.
        if mode is not None:
            self.target.set("working" if mode == "held" else "idle")
        env["CLAUDE_CODE_SESSION_ID"] = session_id or self.session
        env.pop("AGENT_PEER_STEWARD_REWAKE_MAX_SECONDS", None)
        return env

    def _watch_root(self):
        out = subprocess.run(
            [sys.executable, str(_PEER_STEWARD), "status", "--json"],
            capture_output=True, text=True, env=self._env(),
        ).stdout
        return Path(json.loads(out)["watch_root"])

    def _arm(self, mode="idle", target="peer-a", wake="hook", session_id=None, env=None):
        proc = subprocess.run(
            [sys.executable, str(_PEER_STEWARD), "watch", target, "--wake", wake],
            capture_output=True, text=True, env=env or self._env(mode, session_id),
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
        """The target finishes its turn: its runtime record changes, then the shared
        runner is woken for its next pass."""
        self.target.set("idle")
        self._wake_runner()


class ArmingTest(_HookMixin, unittest.TestCase):
    def test_repeated_watch_and_rearm_reconnect_the_same_exact_recipient(self):
        armed = self._arm(mode="held")
        watch_id = self._fields(armed)["watch_id"]
        spec = importlib.util.spec_from_file_location("rewake_repeat", _HOOK)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        with mock.patch.dict(os.environ, self._env()):
            for command, state in (("watch peer-a --wake hook", "already-armed"),
                                   (f"rearm {watch_id}", "alive"),
                                   (f"rearm {watch_id}", "rearmed")):
                line = armed.replace("state=armed", "state=" + state)
                payload = self._payload(line, command=f"python3 {_PEER_STEWARD} {command}")
                self.assertEqual(hook.parse_arm(payload), (watch_id, "peer-a"))
                payload["session_id"] = "foreign"
                self.assertIsNone(hook.parse_arm(payload))
        self._release()

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

    def test_old_async_callback_does_not_arm_after_confirmed_same_seat_successor(self):
        env = self._env("held", session_id=self.session)
        with mock.patch.dict(os.environ, env):
            import dispatch_seat_handover as handover
            import session_tidy
            peer_spec = importlib.util.spec_from_file_location("rewake_fixture_peer_steward", _PEER_STEWARD)
            peer_steward = importlib.util.module_from_spec(peer_spec)
            peer_spec.loader.exec_module(peer_steward)
            armed = self._arm(mode="held")
            fields = self._fields(armed)
            arm_path = peer_steward._watch_paths(fields["watch_id"]).arm
            arm = peer_steward._read_json(arm_path)
            seat = session_tidy.Seat("pane", "fixture-continuity-seat", "w1:p9", "claude")
            arm["steward_seat"] = seat.as_dict()
            peer_steward._write_json_atomic(arm_path, arm)
            steward = arm["steward"]
            watch = {
                "watch_id": fields["watch_id"],
                "origin": {"harness": steward["harness"], "sid": steward["session_id"]},
                "holder": {"harness": "claude", "sid": "successor-session"},
                "seat_key": seat.key,
                "storage_root": str(peer_steward.peer_message.peer_state_root()),
                "server": arm.get("server") or "default", "target": arm.get("target"),
                "agent": dict(arm.get("agent") or {}), "until": list(arm.get("until") or []),
                "armed_ts": arm.get("armed_ts"), "deadline": arm.get("deadline"),
                "lineage": [{"from": steward["session_id"], "to": "successor-session",
                             "snapshot_id": "fixture"}],
            }
            with session_tidy.seat_lock(seat.key):
                session_tidy._append_ledger_entry(seat, {
                    "ts": session_tidy.now_epoch(), "harness": "claude",
                    "sid": "successor-session", "event": "handover",
                    "from": steward["session_id"], "seat_key": seat.key,
                    "bindings": [], "steward": {"schema": 1, "roles": [], "watches": [watch]},
                })
            spec = importlib.util.spec_from_file_location("rewake_lineage_fixture", _HOOK)
            hook = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(hook)
            old_payload = self._payload(armed, session_id=steward["session_id"])
            self.assertIsNone(hook.parse_arm(old_payload))
            successor_env = self._env("held", session_id="successor-session")
            with mock.patch.dict(os.environ, successor_env):
                resolved = handover.resolve_watch(fields["watch_id"], seat_key=seat.key)
                status = subprocess.run(
                    [sys.executable, str(_PEER_STEWARD), "status", "--watch",
                     fields["watch_id"], "--json"], env=successor_env,
                    capture_output=True, text=True, timeout=10)
                parsed = hook.parse_arm(self._payload(armed, session_id="successor-session"))
                self.assertEqual(parsed[0] if parsed else None, fields["watch_id"],
                                 repr({"resolved": resolved, "status": status.stdout,
                                       "stderr": status.stderr}))
        self._release()
        with mock.patch.dict(os.environ, env):
            resolved = handover.resolve_watch(fields["watch_id"], seat_key=seat.key)
        self.assertEqual((resolved["holder"]["harness"], resolved["holder"]["sid"]),
                         ("claude", "successor-session"))


class DeliveryTest(_HookMixin, unittest.TestCase):
    def test_receipt_acks_once_and_wakes_with_exit_two(self):
        # Armed on a working target: only the change to idle is a notice.
        armed = self._arm(mode="held")
        watch_id = self._fields(armed)["watch_id"]
        self._release()
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

        # The shared runner read the target's native state and published readiness:
        # the identity is the native session itself, never a Herdr endpoint.
        readiness_root = (self.root / "peer-state" / "peer-steward" /
                          "obligations" / "readiness-v1")
        projections = [json.loads(path.read_text())
                       for path in readiness_root.glob("*.json")
                       if path.name != "runner.json"]
        matches = [item for item in projections
                   if item.get("identity", {}).get("session_id") == "sid-fixture"
                   and item.get("identity", {}).get("pane") == "w1:p9"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["result"]["state"], "ready")
        self.assertTrue(str(matches[0]["identity"].get("endpoint")).startswith("native:"),
                        matches[0]["identity"])

        # A second carrier must not create a second ack.
        again = subprocess.run(
            [sys.executable, str(_PEER_STEWARD), "ack", watch_id, "--carrier", "userprompt-sweep"],
            capture_output=True, text=True, env=self._env(),
        )
        self.assertEqual(again.returncode, 0)
        self.assertIn("ack=already", again.stdout)
        self.assertEqual(json.loads(ack_path.read_text())["carrier"], "claude-async-rewake")

    def test_dead_watcher_rearms_exactly_once_then_delivers(self):
        # The target's state belongs to the native session record, not to whichever
        # process spawned an observer: the shared runner's observer recovery and the
        # hook's rearm both restore this watch, and both must see the same target.
        armed = self._arm(mode="held")
        fields = self._fields(armed)
        arm_path = self._watch_root() / f"{fields['watch_id']}.json"
        watcher = json.loads(arm_path.read_text())["watcher"]
        os.kill(int(watcher["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and Path(f"/proc/{watcher['pid']}/stat").exists():
            time.sleep(0.02)
        # An observer restored before the switch already watches the working target;
        # the target turning idle is what the runner then publishes, whenever it started.
        self.target.set("idle")
        stop = threading.Event()
        def wake_runner():
            while not stop.wait(0.2):
                self._wake_runner()
        waker = threading.Thread(target=wake_runner, daemon=True)
        waker.start()
        try:
            proc = self._run_hook(self._payload(armed), env=self._env())
        finally:
            stop.set()
            waker.join(timeout=5)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("state=idle", proc.stderr)

        root = self._watch_root()
        arms = [
            (p, json.loads(p.read_text())) for p in root.glob("*.json")
            if not p.name.endswith((".receipt.json", ".ack.json", ".observation.json"))
        ]
        # Exactly one restore -- by the hook (`rearmed=1`) or by observer recovery
        # before it -- replaces only the observer: the same watch id survives with
        # a new observer generation, never a second watch row.
        self.assertEqual([p.name for p, _ in arms], [fields["watch_id"] + ".json"],
                         "exactly one rearm, same watch id")
        self.assertEqual(arms[0][1]["rearm_count"], 1)
        self.assertGreaterEqual(arms[0][1].get("observer_generation", 1), 2)

    def test_second_death_is_attention_not_another_rearm(self):
        # The hook's own rule, driven through its real `main` with a scripted utility:
        # the watcher dies, the hook rearms once, the rearmed watcher dies too, and the
        # hook stops at attention instead of rearming again.
        spec = importlib.util.spec_from_file_location("rewake_second_death", str(_HOOK))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        watch_id, new_id = "0123456789abcdef", "fedcba9876543210"
        status = {"watch_root": str(self.root), "watches": [{
            "recipient": {"harness": "claude", "session_id": self.session},
            "receipt": str(self.root / f"{watch_id}.receipt.json"), "target": "peer-a"}]}
        calls = []

        def fake_run(command, **kwargs):
            args = command[2:]
            calls.append(args[0])
            if args[0] == "status":
                return subprocess.CompletedProcess(command, 0, json.dumps(status), "")
            if args[0] == "join":
                return subprocess.CompletedProcess(
                    command, 5, "state=watcher-dead agent=claude session_id=sid-fixture "
                    "name=peer-a pane=w1:p9", "")
            if args[0] == "rearm":
                return subprocess.CompletedProcess(
                    command, 0, f"watch_id={new_id} state=rearmed", "")
            if args[0] == "ack":
                return subprocess.CompletedProcess(command, 0, "ack=created", "")
            self.fail(f"unexpected hook utility command: {args!r}")

        hook_sys = types.SimpleNamespace(
            stdin=io.StringIO(json.dumps(self._payload(f"state=armed wake=hook watch_id={watch_id}"))),
            stdout=io.StringIO(), stderr=io.StringIO(), executable=sys.executable)
        with mock.patch.object(module, "sys", hook_sys), \
                mock.patch.object(module, "subprocess", types.SimpleNamespace(run=fake_run)):
            result = module.main()
        self.assertEqual(result, 2)
        self.assertEqual(calls.count("rearm"), 1, calls)
        self.assertEqual(calls.count("join"), 2, calls)
        self.assertIn("state=watcher-dead", hook_sys.stderr.getvalue())
        self.assertIn("rearmed=1", hook_sys.stderr.getvalue())

    def test_join_outlives_the_short_subprocess_cap(self):
        # B1 regression: the join must not be cut by the utility subprocess cap.
        # The deterministic contract test below keeps the 1 s boundary. This
        # real utility case leaves room for cold-starting status and ACK calls.
        armed = self._arm(mode="held")
        env = self._env("held")
        env["AGENT_PEER_STEWARD_REWAKE_SUBPROCESS_SECONDS"] = "3"
        import threading
        threading.Timer(5.0, self._release).start()
        started = time.monotonic()
        proc = self._run_hook(self._payload(armed), env=env, timeout=60)
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("state=idle", proc.stderr)
        self.assertGreater(elapsed, 3.0, "the real hook must outlive the ordinary utility cap")
        watch_id = self._fields(armed)["watch_id"]
        ack_path = self._watch_root() / f"{watch_id}.ack.json"
        self.assertTrue(ack_path.exists())
        ack = json.loads(ack_path.read_text())
        self.assertEqual(ack["carrier"], "claude-async-rewake")
        self.assertEqual(ack["session_id"], self.session)

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


class SubprocessTimeoutContractTest(unittest.TestCase):
    def test_join_uses_its_budget_while_ack_keeps_the_short_cap(self):
        spec = importlib.util.spec_from_file_location("peer_steward_rewake", str(_HOOK))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        watch_id = "0123456789abcdef"
        with tempfile.TemporaryDirectory() as tmp:
            watch_root = Path(tmp)
            receipt = watch_root / f"{watch_id}.receipt.json"
            status = {
                "watch_root": str(watch_root),
                "watches": [{
                    "recipient": {"harness": "claude", "session_id": "sid-fixture"},
                    "receipt": str(receipt),
                    "target": "peer-a",
                }],
            }
            payload = {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "session_id": "sid-fixture",
                "tool_input": {"command": "python3 peer-steward.py watch peer-a --wake hook"},
                "tool_response": {
                    "stdout": f"state=armed wake=hook watch_id={watch_id}"
                },
            }
            calls = []

            def fake_run(command, **kwargs):
                args = command[2:]
                calls.append((args, kwargs["timeout"]))
                if args[0] == "status":
                    return subprocess.CompletedProcess(command, 0, json.dumps(status), "")
                if args[0] == "join":
                    joined = (
                        f"state=idle agent=claude session_id=sid-fixture name=peer-a "
                        f"pane=w1:p9 receipt={receipt}"
                    )
                    return subprocess.CompletedProcess(command, 2, joined, "")
                if args[0] == "ack":
                    return subprocess.CompletedProcess(command, 0, "ack=created", "")
                self.fail(f"unexpected hook utility command: {args!r}")

            hook_sys = types.SimpleNamespace(
                stdin=io.StringIO(json.dumps(payload)),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                executable=sys.executable,
            )
            with mock.patch.dict(os.environ, {
                "AGENT_PEER_STEWARD_REWAKE_SUBPROCESS_SECONDS": "1",
                "AGENT_PEER_STEWARD_REWAKE_MAX_SECONDS": str(module.BUDGET_SECONDS),
            }), mock.patch.object(module, "sys", hook_sys), \
                    mock.patch.object(module, "time", types.SimpleNamespace(
                        monotonic=lambda: 100.0
                    )), mock.patch.object(module, "subprocess", types.SimpleNamespace(
                        run=fake_run
                    )):
                result = module.main()

        self.assertEqual(result, 2)
        short_cap = 1.0
        remaining_seconds = module.BUDGET_SECONDS - module.BUDGET_MARGIN_SECONDS
        self.assertEqual(calls[0][0][0], "status")
        self.assertEqual(calls[0][1], short_cap)
        self.assertEqual(calls[1][0], [
            "join", watch_id, "--timeout", str(remaining_seconds * 1000), "--no-ack"
        ])
        self.assertEqual(calls[1][1], remaining_seconds + module.BUDGET_MARGIN_SECONDS)
        self.assertGreater(calls[1][1], short_cap)
        self.assertEqual(calls[2][0], [
            "ack", watch_id, "--carrier", "claude-async-rewake"
        ])
        self.assertEqual(calls[2][1], short_cap)


if __name__ == "__main__":
    unittest.main()
