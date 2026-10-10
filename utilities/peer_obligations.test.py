#!/usr/bin/env python3
"""Temporary-root checks for durable peer obligations and native readiness."""

from __future__ import annotations

import fcntl
import importlib.util
import fcntl
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

import peer_obligations as obligations

_HERE = Path(__file__).resolve().parent
_STEWARD_SPEC = importlib.util.spec_from_file_location(
    "peer_steward_under_obligation_test", str(_HERE / "peer-steward.py"))
peer_steward = importlib.util.module_from_spec(_STEWARD_SPEC)
_STEWARD_SPEC.loader.exec_module(peer_steward)


class ObligationStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = obligations.ObligationStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_current_runner_starts_once_while_legacy_runner_lock_is_held(self):
        self.store.create("message-reconnect", "message", {"session_id": "parent"},
                          {"ref": "a" * 32})
        with (self.store.root / "runner.lock").open("w") as legacy, \
             mock.patch.object(obligations.subprocess, "Popen") as spawn:
            fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(obligations.ensure_runner(self.root))
            spawn.assert_called_once()
            argv = spawn.call_args.args[0]
            self.assertEqual(argv[-2:], ["--state-root", str(self.root)])
            with (self.store.root / obligations.RUNNER_LOCK_NAME).open("w") as current:
                fcntl.flock(current, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(obligations.ensure_runner(self.root))
                spawn.assert_called_once()

    def test_retire_lock_waits_for_legacy_and_releases_after_processing_error(self):
        self.store.root.mkdir(parents=True)
        with (self.store.root / "runner.lock").open("w") as legacy:
            fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with obligations.legacy_runner_lock(self.store) as acquired:
                self.assertFalse(acquired)
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with obligations.legacy_runner_lock(self.store) as acquired:
                self.assertTrue(acquired)
                raise RuntimeError("fixture")
        with obligations.legacy_runner_lock(self.store) as acquired:
            self.assertTrue(acquired)

    def test_create_is_idempotent_and_keeps_body_out_of_duty_record(self):
        intent = {"ref": "a" * 32, "body_digest": "b" * 64}
        identity = {"server": "fixture-server", "pane": "w1:p7", "session_id": "sid-1"}
        first = self.store.create("message-" + "a" * 32, "message", identity, intent)
        again = self.store.create("message-" + "a" * 32, "message", identity, intent)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(first["intent"], again["intent"])
        self.assertNotIn("text", first["intent"])

    def test_immutable_identity_conflict_is_refused(self):
        self.store.create("retire-1", "retire", {"pane": "w1:p1"}, {"target": "worker"})
        with self.assertRaisesRegex(obligations.ObligationError, "intent-conflict"):
            self.store.create("retire-1", "retire", {"pane": "w1:p2"}, {"target": "worker"})

    def test_result_is_immutable_across_later_transport_updates(self):
        self.store.create("batch-1", "registered-batch", {"parent": "att-parent"},
                          {"attempt_ids": ["att-child"]})
        self.store.update("batch-1", result="succeeded", delivery="pending")
        updated = self.store.update("batch-1", observation={"reason": "receiver-unavailable"},
                                    delivery="pending")
        self.assertEqual(updated["result"], "succeeded")
        with self.assertRaisesRegex(obligations.ObligationError, "result-conflict"):
            self.store.update("batch-1", result="failed")

    def test_ordinary_pane_without_registry_row_is_valid_native_evidence(self):
        jobs = self.root / "dispatch" / "jobs.log"
        work, state = obligations.bound_work_for_pane(
            "w1:p4", "codex", "thread-1", jobs=jobs)
        self.assertEqual((work, state), ((), "observed"))
        ready = obligations.pane_readiness(
            server="fixture-server", pane="w1:p4", harness="codex",
            session_id="thread-1", pid_birth="pid:9@start:4",
            identity_verified=True, native_turn="idle",
            bound_work=work, bindings_state=state,
        )
        self.assertEqual((ready.state, ready.scope, ready.outcome),
                         ("ready", "native-turn", None))

    def test_a_received_gate_does_not_close_a_later_terminal_completion(self):
        from types import SimpleNamespace
        identity = {"jobs": str(self.root / "route-jobs.tsv"), "attempt_id": "att-owner",
                    "session_id": "sid", "harness": "claude"}
        duty = self.store.create("registered-batch-fixture", "registered-batch", identity,
                                 {"carrier": "claude-parent-runtime"})
        observation = {"transfer_ref": "a" * 32, "recipient_sid": "sid",
                       "delivery_id": "delivery-gate", "storage_recipient": "sid", "claim_owner": "owner"}
        self.store.update(duty["id"], observation=observation)
        message = {"state": "received", "ref": "a" * 32, "refs": [duty["id"]],
                   "to": {"harness": "claude", "session_id": "sid"},
                   "dispatch_notice": {**observation, "duty_id": duty["id"],
                       "jobs": identity["jobs"], "attempt_id": identity["attempt_id"],
                       "carrier": "claude-parent-runtime"}}
        row = SimpleNamespace(status="done", attempt_id="att-owner",
                              metadata={"parent_sid": "sid", "delivery_id": "delivery-final"})
        record = {"state": "claimed", "attempt_ids": ["att-owner"], "claim_owner": "owner",
                  "recipient_kind": "claude-parent-runtime"}
        with mock.patch("dispatch_completion_join.current_attempt_row", return_value=row), \
                mock.patch("dispatch_pending_delivery.read", return_value=record), \
                mock.patch("dispatch_pending_delivery.ack") as ack:
            obligations.acknowledge_registered_delivery(message, roots=[self.root])
            ack.assert_called_once()
            self.assertEqual(self.store.get(duty["id"])["state"], "pending")
            self.store.update(duty["id"], observation={**observation, "transfer_ref": "b" * 32,
                                                      "delivery_id": "delivery-final"})
            record["state"] = "acked"
            self.assertTrue(obligations.registered_delivery_settled(message, roots=[self.root]))
            obligations.acknowledge_registered_delivery(message, roots=[self.root])
            self.assertEqual(self.store.get(duty["id"])["state"], "pending")
            record["state"] = "claimed"
            final = {**message, "ref": "b" * 32,
                     "dispatch_notice": {**message["dispatch_notice"], "delivery_id": "delivery-final"}}
            obligations.acknowledge_registered_delivery(final, roots=[self.root])
            self.assertEqual(self.store.get(duty["id"])["state"], "complete")

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfd handoff")
    def test_unsupported_observer_releases_same_lock_without_losing_accepted_duties(self):
        self._check_observer_handoff(supported=False)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfd handoff")
    def test_supported_observer_keeps_its_pid_and_same_lock(self):
        self._check_observer_handoff(supported=True)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfd handoff")
    def test_claude_only_observer_is_replaced_for_shared_activation_delivery(self):
        self._check_observer_handoff(supported=False, native_only=True)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfd handoff")
    def test_a_legacy_observer_keeps_its_pid_while_current_runner_starts(self):
        self._check_observer_handoff(supported=False, legacy=True)

    def _check_observer_handoff(self, supported, legacy=False, native_only=False):
        duty = self.store.create("registered-batch-fixture", "registered-batch",
                                 {"session_id": "parent"}, {"carrier": "claude-parent-runtime"})
        prior = self.store.create("message-fixture", "message", {"session_id": "other"}, {"ref": "old"})
        script = self.root / "peer-steward.py"
        source = "import time\nprint('ready', flush=True)\ntime.sleep(30)\n"
        if supported:
            source = ("def _resume_registered_obligation():\n"
                      "    from dispatch_session_sweep import addressed_records\n" + source)
        elif native_only:
            source = "def _resume_registered_obligation(): pass\n" + source
        script.write_text(source)
        lock_path = self.store.root / ("runner.lock" if legacy else obligations.RUNNER_LOCK_NAME)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        process = subprocess.Popen([sys.executable, str(script), "__obligation-runner",
                                    "--state-root", str(self.root), "--lock-fd", str(fd)],
                                   pass_fds=(fd,), stdout=subprocess.PIPE, text=True)
        os.close(fd)
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            with mock.patch.object(obligations.subprocess, "Popen") as spawned, \
                    mock.patch.object(obligations, "_RUNNERS", []):
                self.assertTrue(obligations.ensure_runner(self.root))
                if legacy:
                    self.assertIsNone(process.poll())
                    spawned.assert_called_once()
                    self.assertEqual(len(spawned.call_args.kwargs["pass_fds"]), 1)
                    self.assertNotEqual(lock_path.name, obligations.RUNNER_LOCK_NAME)
                elif supported:
                    self.assertIsNone(process.poll())
                    spawned.assert_not_called()
                else:
                    self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
                    spawned.assert_called_once()
                    self.assertEqual(len(spawned.call_args.kwargs["pass_fds"]), 1)
            self.assertEqual(self.store.get(duty["id"]), duty)
            self.assertEqual(self.store.get(prior["id"]), prior)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            process.stdout.close()

    def test_observer_error_after_fulfillment_cannot_reopen_duty(self):
        self.store.create("retire-done", "retire", {"pane": "w1:p1"},
                          {"target": "worker"})
        done = self.store.update("retire-done", state="complete", result="normal-exit",
                                 observation={"phase": "complete"}, cleanup="complete")
        after_error = self.store.update("retire-done", observer_error="observer-unavailable")
        self.assertEqual(after_error, done)

    def test_conditional_observation_preserves_claim_and_terminal_cancellation(self):
        self.store.create("retire-race", "retire", {"pane": "w1:p1"}, {"target": "peer"})
        claimed = self.store.claim_phase("retire-race", {"waiting"}, "exit-requested",
                                        extra={"foreground": {"pid": 42}})
        stale = self.store.update("retire-race", state="pending", expected_phases={"waiting"},
                                  observation={"phase": "waiting", "reason": "stale"})
        self.assertEqual(stale, claimed)
        cancelled = self.store.update("retire-race", state="cancelled",
                                      observation={"phase": "waiting", "reason": "cancelled"})
        self.assertEqual(self.store.update("retire-race", state="unknown", expected_phases={"waiting"},
                                          observation={"phase": "waiting"}), cancelled)
        self.assertIsNone(self.store.claim_phase("retire-race", {"waiting"}, "exit-requested"))

    def test_unreadable_binding_source_is_unknown(self):
        jobs = self.root / "not-a-file"
        jobs.mkdir()
        work, state = obligations.bound_work_for_pane(
            "w1:p4", "codex", "thread-1", jobs=jobs)
        self.assertEqual((work, state), ((), "unknown"))
        ready = obligations.pane_readiness(
            server="fixture-server", pane="w1:p4", harness="codex",
            session_id="thread-1", pid_birth="pid:9@start:4",
            identity_verified=True, native_turn="idle",
            bound_work=work, bindings_state=state,
        )
        self.assertEqual((ready.state, ready.reason),
                         ("unknown", "registered-bindings-unavailable"))

    def test_foreground_tag_binds_executor_even_when_it_also_owns_children(self):
        from dispatch_contract import ObservedAttemptLiveness
        jobs = self.root / "jobs.log"
        for harness in ("claude", "codex", "opencode"):
            jobs.write_text(
                f"now\topen\trepo\t-\texec\tattempt_id=att-exec,harness={harness},parent_sid=other\n"
                f"now\topen\trepo\t-\tchild\tattempt_id=att-child,parent_sid=sid,parent_harness={harness}\n")
            with mock.patch("dispatch_contract.observed_attempt_liveness", return_value=
                            ObservedAttemptLiveness("alive", "fixture", "live", "fixture")):
                work, state = obligations.bound_work_for_pane(
                    "pane", harness, "sid", jobs=jobs, execution_attempt_ids=("att-exec",))
            self.assertEqual(state, "observed")
            self.assertEqual([(w.attempt_id, w.pane_relation) for w in work],
                             [("att-exec", "executor"), ("att-child", "parent")])

    def test_foreground_tag_read_is_birth_checked_and_does_not_use_parent_tag(self):
        with mock.patch("dispatch_contract._runtime_ancestry_proc_stat", return_value={"start": 7}), \
             mock.patch.object(Path, "read_bytes", return_value=b"AGENT_DISPATCH_ATTEMPT_ID=att-own\0"):
            self.assertEqual(obligations.execution_attempts_for_processes([{"pid": 123}]), ("att-own",))
        with mock.patch("dispatch_contract._runtime_ancestry_proc_stat",
                        side_effect=[{"start": 7}, {"start": 8}]), \
             mock.patch.object(Path, "read_bytes", return_value=b"AGENT_DISPATCH_ATTEMPT_ID=att-own\0"):
            self.assertEqual(obligations.execution_attempts_for_processes([{"pid": 123}]), ())
        with mock.patch("dispatch_contract._runtime_ancestry_proc_stat", return_value={"start": 7}), \
             mock.patch.object(Path, "read_bytes", side_effect=PermissionError()):
            with self.assertRaisesRegex(obligations.ObligationError, "pane-execution-unavailable"):
                obligations.execution_attempts_for_processes([{"pid": 123}])


_FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
mode = os.environ.get("FAKE_HERDR_MODE", "idle")
target = argv[2] if len(argv) > 2 else "-"
info = {"result": {"agent": {"agent": "claude", "agent_session": {"value": "sid-fake"},
        "agent_status": "idle", "name": target, "pane_id": "w1:p9",
        "window_id": os.environ.get("FAKE_HERDR_WINDOW_ID", "window-fixture")},
        "type": "agent_info"}}
verb = argv[1] if len(argv) > 1 else ""
if verb == "process-info":
    print(json.dumps({"result": {"process_info": {
        "pane_id": argv[-1], "shell_pid": os.getppid(),
        "foreground_process_group_id": os.getppid(),
        "foreground_processes": [{"pid": os.getppid(), "argv": ["zsh"]}]}}}))
    sys.exit(0)
if verb == "wait" and mode == "held":
    with open(os.environ["FAKE_HERDR_FIFO"], "r") as fh:
        fh.read()
print(json.dumps(info))
sys.exit(0)
'''

_SCRUB_PREFIXES = ("AGENT_DISPATCH_", "AGENT_SESSION_", "AGENT_RUNTIME_",
                   "AGENT_THREAD_", "HERDR_")
_SCRUB_KEYS = ("AGENT_SESSION_ID", "AGENT_SESSION_ROLE", "CLAUDE_CODE_SESSION_ID",
               "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
               "OPENCODE_SESSION_ID", "OPENCODE_DISPATCH_SLUG",
               "AGENT_HERDR_SESSION")


class StewardRecoveryIntegrationTest(unittest.TestCase):
    """Actual steward/controller recovery: fixture transport, death, restart, display."""


def _fixture_env(root: Path, jobs: Path, state: Path, bindir: Path,
                 fifo: Path, session: str, mode: str) -> dict:
    """Fixture transport with inherited runtime/session/dispatch identities removed."""
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith(_SCRUB_PREFIXES):
            env.pop(key, None)
    for key in _SCRUB_KEYS:
        env.pop(key, None)
    env["AGENT_DISPATCH_JOBS"] = str(jobs)
    env["AGENT_PEER_LEDGER_ROOT"] = str(state)
    env["HOME"] = str(root / "home")
    env["CLAUDE_CODE_SESSION_ID"] = session
    env["FAKE_HERDR_MODE"] = mode
    env["FAKE_HERDR_FIFO"] = str(fifo)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    return env


class StewardRecoveryIntegrationTest(unittest.TestCase):
    """Actual steward/controller recovery: fixture transport, death, restart, display."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.touch()
        self.state = self.root / "peer-state"
        self.bindir = self.root / "fakebin"
        self.bindir.mkdir()
        fake = self.bindir / "herdr"
        fake.write_text(_FAKE_HERDR, encoding="utf-8")
        fake.chmod(0o755)
        self.fifo = self.root / "release.fifo"
        os.mkfifo(self.fifo)
        self.session = "steward-obligation-1"
        self.saved_environ = dict(os.environ)
        self.addCleanup(self._restore_environ)
        self._children: list[int] = []
        self.addCleanup(self._reap_children)
        self._old_herdr_session = peer_steward._HERDR_SESSION
        peer_steward._HERDR_SESSION = None
        self.addCleanup(self._restore_herdr_session)

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self.saved_environ)

    def _restore_herdr_session(self):
        peer_steward._HERDR_SESSION = self._old_herdr_session

    def _reap_children(self):
        for pid in self._children:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        deadline = time.monotonic() + 10
        remaining = set(self._children)
        while remaining and time.monotonic() < deadline:
            for pid in tuple(remaining):
                try:
                    waited, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    remaining.discard(pid)
                    continue
                except OSError:
                    continue
                if waited:
                    remaining.discard(pid)
            time.sleep(0.02)

    def _apply_fixture_env(self, mode="held"):
        os.environ.clear()
        os.environ.update(_fixture_env(
            self.root, self.jobs, self.state, self.bindir,
            self.fifo, self.session, mode))

    def _arm_watch(self, mode="held"):
        env = _fixture_env(self.root, self.jobs, self.state, self.bindir,
                           self.fifo, self.session, mode)
        proc = subprocess.run(
            [sys.executable, str(_HERE / "peer-steward.py"), "watch", "peer-a"],
            capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fields = {}
        for token in proc.stdout.split():
            key, separator, value = token.partition("=")
            if separator:
                fields[key] = value
        return fields["watch_id"], int(fields["pid"]), env

    @staticmethod
    def _wait_for_death(pid: int, seconds: float = 10.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.02)
        raise AssertionError(f"fixture observer {pid} never died")

    def test_watch_duty_survives_observer_death_and_resumes_same_id(self):
        watch_id, pid, _env = self._arm_watch(mode="held")
        arm_path = self.state / "peer-watches" / f"{watch_id}.json"
        first = json.loads(arm_path.read_text(encoding="utf-8"))
        self.assertEqual(first["watch_id"], watch_id)
        os.kill(pid, signal.SIGKILL)
        self._wait_for_death(pid)

        self._apply_fixture_env(mode="held")
        peer_steward._ensure_watch_observers()

        resumed = json.loads(arm_path.read_text(encoding="utf-8"))
        self.assertEqual(resumed["watch_id"], watch_id,
                         "restart replaces only the observer, never the duty")
        self.assertGreaterEqual(resumed.get("observer_generation", 1), 2)
        new_pid = int((resumed.get("watcher") or {})["pid"])
        self.assertNotEqual(new_pid, pid)
        self._children.append(new_pid)
        try:
            os.kill(new_pid, 0)
        except OSError:
            self.fail("resumed observer is not alive")
        # Death never fulfills the duty: no terminal receipt was written.
        self.assertFalse((self.state / "peer-watches" / f"{watch_id}.receipt.json").exists())

    def test_pending_duties_reach_the_read_only_collector_body_free(self):
        watch_id, pid, _env = self._arm_watch(mode="held")
        self._children.append(pid)
        store = obligations.ObligationStore(self.state)
        store.create("message-" + "r" * 32, "message",
                     {"server": "default", "pane": "w1:p9",
                      "harness": "claude", "session_id": "sid-fake"},
                     {"ref": "r" * 32, "body_digest": "d" * 64,
                      "target": "peer-a",
                      "to": {"harness": "claude", "session_id": "sid-fake"}})
        tools = str(_HERE.parent / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from fleet.collectors import peer_messages
        result = peer_messages.collect(state_roots=[str(self.state)])
        pending = result["by_session"][("claude", "sid-fake")]["pending_obligations"]
        kinds = {item["kind"] for item in pending}
        self.assertIn("watch", kinds)
        self.assertIn("message", kinds)
        for item in pending:
            self.assertLessEqual(set(item), {"kind", "state", "age_min", "ref"})
        os.kill(pid, signal.SIGKILL)
        self._wait_for_death(pid)
        self._children.remove(pid)

    def test_bound_registered_row_overrides_idle_pane_through_real_jobs(self):
        from dispatch_contract import (ATTEMPT_SCHEMA_VERSION, claim_attempt_row,
                                       process_launch_identity, ObservedAttemptLiveness)
        metadata = {"attempt_schema_version": str(ATTEMPT_SCHEMA_VERSION),
                    "dispatch_depth": "1", "transport": "headless",
                    "execution_surface": "registered-headless", "registered_worker": "1",
                    "fallback_hop": "same-harness-headless", "harness": "codex",
                    "attempt_id": "att-fixture-1", "parent_sid": "sid-1",
                    "parent_harness": "codex", **process_launch_identity(os.getpid())}
        self.assertNotIn("parent_pane", metadata)
        row = "2026-10-09T00:00:00Z\topen\trepo\t-\tslug\t" + ",".join(
            f"{key}={value}" for key, value in metadata.items())
        self.assertTrue(claim_attempt_row(self.jobs, "att-fixture-1", row, launch=True))
        for process_state, expected in (("live", "pending"), ("unverifiable", "unknown")):
            with self.subTest(process_state=process_state), mock.patch(
                    "dispatch_contract.observed_attempt_liveness", return_value=ObservedAttemptLiveness(
                        "alive" if process_state == "live" else "unverifiable",
                        "fixture", process_state, "fixture")):
                work, state = obligations.bound_work_for_pane(
                    "w1:p4", "codex", "sid-1", jobs=self.jobs)
                self.assertEqual((len(work), state), (1, "observed"))
                ready = obligations.pane_readiness(
                    server="fixture-server", pane="w1:p4", harness="codex",
                    session_id="sid-1", pid_birth="pid:9@start:4",
                    identity_verified=True, native_turn="idle", bound_work=work,
                    bindings_state=state)
                self.assertEqual(ready.state, expected)
                self.assertIsNone(ready.outcome)
        for harness, sid in (("codex", "unrelated"), ("claude", "sid-1")):
            self.assertEqual(obligations.bound_work_for_pane(
                "w1:p4", harness, sid, jobs=self.jobs), ((), "observed"))
        with mock.patch("dispatch_seat_handover.effective_parent", return_value="successor"), \
             mock.patch("dispatch_seat_handover.effective_parent_harness", return_value="claude"):
            work, state = obligations.bound_work_for_pane(
                "w2:p7", "claude", "successor", jobs=self.jobs)
            self.assertEqual((len(work), state), (1, "observed"))


if __name__ == "__main__":
    unittest.main()
