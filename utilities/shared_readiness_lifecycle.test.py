#!/usr/bin/env python3
"""Bounded production-entry lifecycle checks for shared peer readiness."""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import argparse
import fcntl
import hashlib
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))

from dispatch_contract import process_start_ticks  # noqa: E402
import peer_address  # noqa: E402
import shared_readiness  # noqa: E402

# The production procfs reader. The regression modules selected below swap in a
# simulated-inventory bridge when they load, and a forked runner inherits whatever
# this process holds; the process-level cases restore the real reader.
_REAL_NATIVE_SESSIONS = peer_address.native_sessions


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


peer_steward = _load("lifecycle_peer_steward", HERE / "peer-steward.py")

# Communication and watch work never reaches Herdr. A `herdr` on PATH logs any call and
# fails it, so a regression shows as a logged call instead of a silent fallback.
_HERDR_GUARD = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ.get("LIFECYCLE_HERDR_LOG")
if log:
    with open(log, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(sys.argv[1:]) + "\n")
raise SystemExit(1)
'''


class ReadinessGate:
    """Counts the runner's native reads and can hold the first one.

    The runner is a forked child, so the counters and events are fork-shared. The
    runner reads the target's native state once per exact target group per pass; the
    gate wraps exactly that read, so "paused before its first snapshot" keeps its meaning.
    """

    def __init__(self, count_path):
        self.count_path = Path(count_path)
        context = multiprocessing.get_context("fork")
        self.observe_count = context.Value("i", 0)
        self.pause_after = context.Value("i", 2**31 - 1)
        self.pause_entered = context.Event()
        self.resume = context.Event()

    def wrap(self, native_agent):
        def observed(*args, **kwargs):
            with self.observe_count.get_lock():
                self.observe_count.value += 1
                sequence = self.observe_count.value
            if sequence > self.pause_after.value:
                self.pause_entered.set()
                if not self.resume.wait(20):
                    raise TimeoutError("lifecycle observation gate was not released")
            with self.count_path.open("a", encoding="utf-8") as stream:
                stream.write("snapshot\n")
            return native_agent(*args, **kwargs)
        return observed


class LifecycleProjectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="readiness-projection-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.start = str(process_start_ticks(os.getpid()))
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("fixture registry\n", encoding="utf-8")
        self.identity = shared_readiness.legacy_target_identity(
            server="fixture", endpoint="/tmp/fixture.sock", pane="w1:p1",
            harness="claude", session_id="sid-a", native_state="idle")
        shared_readiness.publish_runner_incarnation(
            self.root, os.getpid(), self.start, "runner-old")

    def registry_generation(self):
        from dispatch_registry_cache import registry_view
        return shared_readiness.registry_generation(
            self.jobs, registry_view(self.jobs))

    def test_legacy_projection_needs_current_runner_and_verified_lineage(self):
        shared_readiness.publish_target_projection(
            self.root, self.identity, {"state": "ready", "reason": "native-ready"},
            producer_pid=os.getpid(), producer_start=self.start, incarnation="runner-old")
        self.assertIsNone(shared_readiness.target_result(self.root, self.identity))
        shared_readiness.publish_target_projection(
            self.root, self.identity, {"state": "pending", "reason": "bound-work"},
            producer_pid=os.getpid(), producer_start=self.start, incarnation="runner-old",
            generation=self.registry_generation(),
            lineage={"shell_pid": os.getpid(), "shell_start": self.start})
        self.assertEqual(shared_readiness.target_result(self.root, self.identity)["state"],
                         "pending")
        shared_readiness.publish_runner_incarnation(
            self.root, os.getpid(), self.start, "runner-new")
        self.assertIsNone(shared_readiness.target_result(self.root, self.identity),
                          "a previous runner incarnation must be reobserved")

    def test_session_and_pane_replacement_get_distinct_unknown_bindings(self):
        changed = shared_readiness.legacy_target_identity(
            server="fixture", endpoint="/tmp/fixture.sock", pane="w1:p2",
            harness="claude", session_id="sid-b", native_state="idle")
        shared_readiness.publish_target_projection(
            self.root, self.identity, {"state": "ready", "reason": "native-ready"},
            producer_pid=os.getpid(), producer_start=self.start, incarnation="runner-old",
            lineage={"shell_pid": os.getpid(), "shell_start": self.start})
        self.assertIsNone(shared_readiness.target_result(self.root, changed))

    def test_replaced_process_birth_never_grants_ready(self):
        shared_readiness.publish_target_projection(
            self.root, self.identity, {"state": "ready", "reason": "native-ready"},
            producer_pid=os.getpid(), producer_start=self.start, incarnation="runner-old",
            generation=self.registry_generation(),
            lineage={"shell_pid": os.getpid(), "shell_start": "not-current"})
        self.assertIsNone(shared_readiness.target_result(self.root, self.identity))


class LifecycleRunnerProcessTest(unittest.TestCase):
    """Recover six real detached watchers through the existing real runner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="readiness-lifecycle-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state_root = self.base / "state"
        self.state_root.mkdir()
        self.jobs = self.state_root / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.jobs_before = hashlib.sha256(self.jobs.read_bytes()).hexdigest()
        self.home = self.base / "home"
        self.bin = self.base / "bin"
        self.bin.mkdir()
        herdr_guard = self.bin / "herdr"
        herdr_guard.write_text(_HERDR_GUARD, encoding="utf-8")
        herdr_guard.chmod(0o755)
        # The state the fixture last drove its native targets to (the acceptance
        # wrapper reports it before and after).
        self.state_file = self.base / "target-state.json"
        self.snapshot_count_path = self.base / "runner-snapshots.log"
        self.herdr_log_path = self.base / "herdr-argv.jsonl"
        self.endpoint = ReadinessGate(self.snapshot_count_path)
        self.old_env = dict(os.environ)
        for key in tuple(os.environ):
            if key.startswith(("AGENT_DISPATCH_", "AGENT_PEER_", "AGENT_HERDR_",
                               "AGENT_SESSION_", "HERDR_", "CLAUDE_", "CODEX_",
                               "OPENCODE_", "XDG_")):
                os.environ.pop(key, None)
        os.environ.update({
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_PEER_LEDGER_ROOT": str(self.state_root),
            "HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "LIFECYCLE_HERDR_LOG": str(self.herdr_log_path),
            "PATH": str(self.bin) + os.pathsep + self.old_env.get("PATH", ""),
        })
        # Real native sessions the runner and watchers read: the runtime's own
        # registry record plus a live process. `sid-a` is the target five stewards
        # watch; `sid-b` is an independent target.
        import fixture_processes
        self.native_a = fixture_processes.NativeSession(
            self, sid="sid-a", home=self.home, pane="w1:p1", name="peer-0")
        self.native_b = fixture_processes.NativeSession(
            self, sid="sid-b", home=self.home, pane="w1:p2", name="peer-control")
        self.set_status("working")
        self.addCleanup(self._restore_env)
        real_sessions = mock.patch.object(peer_address, "native_sessions", _REAL_NATIVE_SESSIONS)
        real_sessions.start()
        self.addCleanup(real_sessions.stop)
        self.watch_root = peer_steward._watch_root()
        self.watch_root.mkdir(parents=True, exist_ok=True)
        self.arms = []
        self.watchers = []
        self.runner_pid = None
        self.runner_lock_fd = None
        self.addCleanup(self._cleanup_processes)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self.old_env)

    def set_status(self, status):
        """The targets' runtime records change, as the runtime does."""
        self.native_a.set(status)
        self.native_b.set(status)
        self.state_file.write_text(json.dumps({"status": status}), encoding="utf-8")

    def set_idle(self):
        """The targets finish their turn; the shared runner is woken for its next pass."""
        self.set_status("idle")
        store = peer_steward.peer_obligations.ObligationStore(self.state_root)
        shared_readiness.wake_runner(store.root)

    def _cleanup_processes(self):
        self.endpoint.resume.set()
        for process in self.watchers:
            if process.poll() is None:
                process.terminate()
        for process in self.watchers:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if self.runner_pid is not None:
            try:
                os.kill(self.runner_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    pid, _status = os.waitpid(self.runner_pid, os.WNOHANG)
                except ChildProcessError:
                    pid = self.runner_pid
                if pid == self.runner_pid:
                    self.runner_pid = None
                    break
                time.sleep(0.02)
            if self.runner_pid is not None:
                try:
                    os.kill(self.runner_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(self.runner_pid, 0)
                except ChildProcessError:
                    pass
                self.runner_pid = None
        if self.runner_lock_fd is not None:
            os.close(self.runner_lock_fd)
            self.runner_lock_fd = None
        try:
            from fixture_processes import reap
            reap(str(self.state_root))
        except Exception:
            pass

    def _hold_runner_lock(self):
        store = peer_steward.peer_obligations.ObligationStore(self.state_root)
        store.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.runner_lock_fd = os.open(
            store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME,
            os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.runner_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _launch_watchers(self):
        for watch_id, arm, paths in self.arms:
            lock_fd = os.open(paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.set_inheritable(lock_fd, True)
            argv = [sys.executable, str(HERE / "peer-steward.py"),
                    *peer_steward._watch_observer_duty_argv(
                        watch_id, arm, rearm_generation=0, lock_fd=lock_fd,
                        timeout_before_refs=True)]
            log_fd = os.open(paths.log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
            try:
                process = subprocess.Popen(
                    argv, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                    close_fds=True, pass_fds=(lock_fd,), start_new_session=True,
                    cwd=str(self.state_root), env=dict(os.environ))
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                os.close(log_fd)
                os.close(lock_fd)
            arm["watcher"] = {"pid": process.pid,
                              "pid_start": str(process_start_ticks(process.pid) or "")}
            peer_steward._write_json_atomic(paths.arm, arm)
            self.watchers.append(process)

    def _start_runner(self, *, reconcile_barrier=None):
        import traceback

        control_read, control_write = os.pipe()
        try:
            child = os.fork()
        except BaseException:
            raise
        if child == 0:
            os.close(control_write)
            try:
                os.read(control_read, 1)
                os.close(control_read)
                peer_steward.native_agent = self.endpoint.wrap(peer_steward.native_agent)
                if reconcile_barrier is not None:
                    entered, release, snapshot_rows = reconcile_barrier
                    original_reconcile = peer_steward._reconcile_external_obligations

                    def gated_reconcile(*args, **kwargs):
                        snapshot = kwargs.get("snapshot") or {}
                        watch_id, _arm, _paths = self.arms[0]
                        snapshot_arm = next((arm for selected_id, arm in
                                             snapshot.get("watches", ())
                                             if selected_id == watch_id), None)
                        snapshot_rows.put({
                            "watch_id": watch_id,
                            "watcher": (snapshot_arm or {}).get("watcher") or {},
                        })
                        result = original_reconcile(*args, **kwargs)
                        entered.set()
                        if not release.wait(20):
                            raise TimeoutError("runner readiness pass barrier was not released")
                        return result

                    peer_steward._reconcile_external_obligations = gated_reconcile
                code = peer_steward.cmd_obligation_runner(SimpleNamespace(
                    lock_fd=self.runner_lock_fd, state_root=self.state_root,
                    service_current=None))
            except BaseException:
                traceback.print_exc()
                code = 1
            os._exit(int(code or 0))
        self.runner_pid = child
        os.close(control_read)
        os.write(control_write, b"go")
        os.close(control_write)
        os.close(self.runner_lock_fd)
        self.runner_lock_fd = None

    def _arm(self, index, *, separate=False, previous_consumer=None,
             server="fixture", target_override=None, session_override=None):
        import session_tidy

        watch_id = hashlib.sha256(f"lifecycle-{index}".encode()).hexdigest()[:16]
        target = target_override or ("peer-control" if separate else f"peer-{index}")
        native = self.native_b if separate else self.native_a
        pane, sid = ("w1:p2", "sid-b") if separate else ("w1:p1", "sid-a")
        if session_override is not None:
            sid = session_override
        # The exact native birth rides with the target, as `watch` arms it.
        birth = {} if session_override == "-" else {
            "pid": native.pid, "pid_start": native.birth,
            "pid_namespace": os.readlink("/proc/self/ns/pid")}
        arm = {
            "schema_version": peer_steward._WATCH_SCHEMA,
            "watch_id": watch_id,
            "target": target,
            "until": [],
            "timeout": 300000,
            "refs": [f"receipt-{index}"],
            "server": server,
            "steward_server": server,
            "readiness_endpoint": "native:" + server,
            "agent": {"harness": "claude", "session_id": sid,
                      "name": target, "pane": pane, **birth},
            "initial_state": "working",
            "wake": "none",
            "steward": {"harness": "claude", "session_id": f"steward-{index}",
                        "project": str(self.base)},
            "steward_seat": session_tidy.Seat(
                "pane", f"fixture-seat-{index}", f"w1:p{8 + index}", "claude",
                sid=f"steward-{index}", endpoint=server).as_dict(),
            "armed_ts": peer_steward._utc_now(),
            "deadline": peer_steward._watch_deadline(300000),
            "observer_protocol": 3,
            "observer_generation": 1,
            "rearm_count": 0,
            "watcher": {},
        }
        paths = peer_steward._watch_paths(watch_id, self.watch_root)
        peer_steward._write_json_atomic(paths.arm, arm)
        # A native session reports idle/working/blocked; there is no native `done`.
        native_state = "idle"
        native_agent = {"harness": "claude", "session_id": sid,
                        "name": target, "pane": pane}
        observation = {
            "schema_version": peer_steward._WATCH_SCHEMA,
            "watch_id": watch_id,
            "state": "waiting",
            "reason": "recovered-native-result",
            "native_wait": {"state": "result", "agent_state": native_state,
                            "agent": native_agent, "reason": native_state,
                            "done_ts": peer_steward._utc_now()},
        }
        if previous_consumer is not None:
            identity = peer_steward._watch_readiness_identity(arm, native_state)
            if identity is None:
                raise AssertionError("stale-consumer fixture identity was not bindable")
            token = "dead-previous-consumer-token"
            observation["readiness_transition"] = {
                "token": token, "identity": identity,
                "consumer": previous_consumer, "requested_at": time.time() - 30,
            }
            observation["readiness_contention"] = {"request_token": token}
        peer_steward._write_json_atomic(paths.observation, observation)
        self.arms.append((watch_id, arm, paths))

    def _wait_until(self, predicate, message, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        store = peer_steward.peer_obligations.ObligationStore(self.state_root)
        marker_path = store.root / "readiness-v1" / "runner.json"
        marker = peer_steward._read_json(marker_path) or {}
        projections = []
        for path in (store.root / "readiness-v1").glob("[0-9a-f]*.json"):
            if path.name == "runner.json":
                continue
            projections.append(peer_steward._read_json(path))
        watches = []
        for watch_id, _arm, paths in self.arms:
            watches.append({"watch_id": watch_id,
                            "watcher": peer_steward._read_json(paths.arm),
                            "observation": peer_steward._read_json(paths.observation),
                            "receipt": paths.receipt.exists()})
        try:
            runner_start = process_start_ticks(self.runner_pid) if self.runner_pid else None
        except (OSError, TypeError, ValueError):
            runner_start = None
        try:
            snapshot_count = len(self.snapshot_count_path.read_text().splitlines())
        except OSError:
            snapshot_count = 0
        self.fail(f"{message}; runner_pid={self.runner_pid} runner_start={runner_start} "
                  f"marker={marker} snapshots={snapshot_count} projections={projections} "
                  f"watches={watches}")

    def _herdr_calls(self):
        return ([json.loads(line) for line in
                 self.herdr_log_path.read_text(encoding="utf-8").splitlines()]
                if self.herdr_log_path.exists() else [])

    def _watch_notice_records(self):
        root = peer_steward.peer_message.peer_state_root() / "peer-messages"
        records = []
        for path in root.rglob("*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        row = json.loads(line)
                        if row.get("kind") == "notice":
                            records.append(row)
            except (OSError, ValueError):
                continue
        return records

    def test_readiness_notification_uses_current_observer_after_stale_snapshot(self):
        self._arm(0)
        watch_id, original_arm, paths = self.arms[0]
        original_native_wait = peer_steward._read_json(paths.observation)["native_wait"]
        original_deadline = original_arm["deadline"]
        store = peer_steward.peer_obligations.ObligationStore(self.state_root)
        self._hold_runner_lock()

        context = multiprocessing.get_context("fork")
        entered = context.Event()
        release = context.Event()
        snapshot_rows = context.Queue()
        self.addCleanup(release.set)
        self._start_runner(reconcile_barrier=(entered, release, snapshot_rows))
        self.assertTrue(entered.wait(10),
                        "normal runner did not reach the controlled readiness boundary")
        snapshot_row = snapshot_rows.get(timeout=2)
        self.assertEqual(snapshot_row, {"watch_id": watch_id, "watcher": {}},
                         "runner pass must retain its accepted-arm scheduling snapshot")

        observed = {}

        def current_request_is_live():
            current_arm = peer_steward._read_json(paths.arm) or {}
            observation = peer_steward._read_json(paths.observation) or {}
            request = observation.get("readiness_transition")
            watcher = current_arm.get("watcher") or {}
            if (not isinstance(request, dict)
                    or not request.get("token")
                    or request.get("consumer") != watcher
                    or request.get("identity") != peer_steward._watch_readiness_identity(
                        current_arm, original_native_wait["agent_state"])
                    or not shared_readiness.consumer_is_current({"consumer": watcher})):
                return False
            observed.update(arm=current_arm, observation=observation, request=request)
            return True

        self._wait_until(current_request_is_live,
                         "recovered observer did not publish a current transition request",
                         timeout=10)
        current_watcher = observed["arm"]["watcher"]
        self.assertNotEqual(current_watcher, snapshot_row["watcher"],
                            "observer publication must postdate the runner snapshot")
        self.assertTrue(peer_steward._pid_identity_ok(
            current_watcher["pid"], current_watcher["pid_start"]))
        self.set_idle()
        release.set()

        token = observed["request"]["token"]
        identity = observed["request"]["identity"]
        response = {}

        def ready_response_exists():
            current = shared_readiness.watch_transition_result(
                store.root, watch_id, identity, token)
            if not current or current.get("state") != "ready":
                return False
            raw = peer_steward._read_json(
                shared_readiness._watch_transition_path(store.root, watch_id)) or {}
            response.update(result=current, raw=raw)
            return True

        self._wait_until(ready_response_exists,
                         "normal runner did not publish the current token's ready response",
                         timeout=10)
        try:
            self._wait_until(lambda: paths.receipt.exists(),
                             "current response was published but the same detached observer was not woken",
                             timeout=8)
        except AssertionError as error:
            current_arm = peer_steward._read_json(paths.arm) or {}
            current_observation = peer_steward._read_json(paths.observation) or {}
            observer = current_arm.get("watcher") or {}
            proc_status = Path(f"/proc/{observer.get('pid', -1)}/status")
            signal_fields = {}
            try:
                for line in proc_status.read_text(encoding="utf-8").splitlines():
                    key, separator, value = line.partition(":")
                    if separator and key in {"State", "SigBlk", "SigPnd", "SigCgt"}:
                        signal_fields[key] = value.strip()
            except OSError:
                pass
            try:
                observer_wchan = Path(
                    f"/proc/{observer.get('pid', -1)}/wchan").read_text().strip()
            except OSError:
                observer_wchan = None
            lock_fd = os.open(paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_contended = False
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except BlockingIOError:
                    lock_contended = True
            finally:
                os.close(lock_fd)
            diagnostic = {
                "watcher": observer,
                "watcher_pid_current": peer_steward._pid_identity_ok(
                    observer.get("pid"), observer.get("pid_start")),
                "watcher_status": signal_fields,
                "watcher_wchan": observer_wchan,
                "watch_lock_contended": lock_contended,
                "request": current_observation.get("readiness_transition"),
                "transition_response": response["raw"],
            }
            raise AssertionError(f"{error}; snapshot_failure_diagnostic={diagnostic}") from error

        receipt = peer_steward._read_json(paths.receipt) or {}
        updated_arm = peer_steward._read_json(paths.arm) or {}
        self.assertEqual(receipt.get("watch_id"), watch_id)
        self.assertEqual(receipt.get("state"), "idle")
        self.assertEqual(updated_arm.get("deadline"), original_deadline)
        self.assertEqual(response["raw"].get("request_token"), token)
        self.assertEqual(response["raw"].get("identity"), identity)
        self.assertTrue(shared_readiness._generation_current(
            response["raw"].get("generation")))
        self.assertEqual(current_watcher, observed["request"]["consumer"])
        self.assertTrue(shared_readiness.consumer_is_current(
            {"consumer": observed["request"]["consumer"]}))
        self.assertEqual(original_native_wait["state"], "result")
        self.assertEqual(self._herdr_calls(), [], "a watch recovery must never reach Herdr")

        notices = []

        def notice_was_written():
            notices[:] = [row for row in self._watch_notice_records()
                          if (row.get("delivery") or {}).get("receipt") == watch_id]
            return bool(notices)

        self._wait_until(notice_was_written,
                         "receipt was written but its one notice was not published",
                         timeout=5)
        self.assertEqual(len(notices), 1)
        EVIDENCE_ROWS.append({
            "case": "stale-runner-snapshot-wakes-current-detached-observer",
            "watch_id": watch_id,
            "snapshot_watcher": snapshot_row["watcher"],
            "current_watcher": current_watcher,
            "request_token": token,
            "request_consumer": observed["request"]["consumer"],
            "transition_response": response["raw"],
            "native_wait_replayed": False,
            "receipt_count": int(paths.receipt.exists()),
            "notice_count": len(notices),
            "deadline_preserved": updated_arm.get("deadline") == original_deadline,
        })

    def test_runner_recovers_independent_watches_and_refreshes_without_jobs_change(self):
        previous_process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        previous_consumer = {
            "pid": previous_process.pid,
            "pid_start": str(process_start_ticks(previous_process.pid) or ""),
        }
        self.assertTrue(previous_consumer["pid_start"], "previous consumer identity must be valid")
        previous_process.terminate()
        previous_process.wait(timeout=2)
        self.assertFalse(Path(f"/proc/{previous_consumer['pid']}/stat").exists(),
                         "preseeded previous consumer must be dead and reaped")
        for index in range(5):
            self._arm(index, previous_consumer=previous_consumer if index == 0 else None)
        self._arm(5, separate=True)
        original_deadlines = {watch_id: arm["deadline"]
                              for watch_id, arm, _paths in self.arms}
        store = peer_steward.peer_obligations.ObligationStore(self.state_root)
        self.endpoint.pause_after.value = 0
        self.addCleanup(self.endpoint.resume.set)
        self._hold_runner_lock()
        self._launch_watchers()
        gate_started_at = time.monotonic()
        gate_deadline = gate_started_at + 13.0
        self._start_runner()

        def every_recovered_watch_asked_for_readiness():
            # A working target publishes no projection: the recovered idle result of
            # each watch only becomes a request, never a settled receipt.
            for watch_id, arm, _paths in self.arms:
                observation = peer_steward._read_json(
                    peer_steward._watch_paths(watch_id, self.watch_root).observation) or {}
                request = observation.get("readiness_transition")
                if not isinstance(request, dict) or not request.get("token"):
                    return False
            return True

        recovered = {}
        watch_id, arm, paths = self.arms[0]

        def fresh_request_survived_outer_pending_write():
            current = peer_steward._read_json(paths.observation) or {}
            request = current.get("readiness_transition")
            if not isinstance(request, dict):
                return False
            if (request.get("token") == "dead-previous-consumer-token"
                    or request.get("consumer") != arm.get("watcher")
                    or "readiness_contention" in current
                    or current.get("state") != "pending"
                    or current.get("reason") != "shared-readiness-revalidating"):
                return False
            recovered.update(observation=current, request=request)
            return True

        try:
            event_wait = min(4.0, max(0.0, gate_deadline - time.monotonic()))
            self.assertTrue(self.endpoint.pause_entered.wait(event_wait),
                            "real runner did not pause before its first readiness snapshot")
            self.assertEqual(self.endpoint.observe_count.value, 1,
                             "the readiness gate must stop the runner's first observation")
            snapshots_before_release = (
                self.snapshot_count_path.read_text(encoding="utf-8").splitlines()
                if self.snapshot_count_path.exists() else [])
            self.assertEqual(snapshots_before_release, [],
                             "the runner must be paused before publishing its first snapshot")
            self.assertFalse(any(paths.receipt.exists()
                                 for _watch, _arm, paths in self.arms),
                             "no watch may settle before the first observation gate")

            predicate_wait = min(10.0, gate_deadline - time.monotonic())
            self.assertGreater(predicate_wait, 0.0,
                               "first-observation gate exhausted its shared time budget")
            self._wait_until(
                fresh_request_survived_outer_pending_write,
                "fresh current-consumer request did not survive the stale cmd_watch_run pending write",
                timeout=predicate_wait)
            self.assertTrue(shared_readiness.consumer_is_current(
                {"consumer": recovered["request"]["consumer"]}))
            self.assertEqual(recovered["observation"]["native_wait"],
                             peer_steward._read_json(paths.observation)["native_wait"])
            runner_marker = peer_steward._read_json(
                store.root / "readiness-v1" / "runner.json") or {}
            self.assertEqual(
                str(process_start_ticks(runner_marker.get("pid", -1)) or ""),
                runner_marker.get("pid_start"),
                "the existing normal runner must own the recovery pass")
        finally:
            self.endpoint.resume.set()
            gate_released_at = time.monotonic()
        gate_pause_seconds = gate_released_at - gate_started_at
        self.assertLessEqual(gate_pause_seconds, 15.0,
                             "first-observation gate exceeded its 15-second pause budget")

        self._wait_until(every_recovered_watch_asked_for_readiness,
                         "recovered watches did not each request a runner readiness read")
        self.assertFalse(any(paths.receipt.exists() for _watch, _arm, paths in self.arms),
                         "a working target must settle no watch")
        for watch_id, arm, _paths in self.arms:
            identity = peer_steward._watch_readiness_identity(arm, "idle")
            self.assertIsNone(shared_readiness.target_result(store.root, identity),
                              "a working target must publish no ready projection")
        self.set_idle()

        self._wait_until(
            lambda: all(paths.receipt.exists() for _watch, _arm, paths in self.arms),
            "real watcher/runner path did not settle each independent receipt after readiness changed")
        self._wait_until(
            lambda: all(process.poll() is not None for process in self.watchers),
            "watchers did not finish notice publication after writing their receipts")
        for process in self.watchers:
            self.assertEqual(process.wait(timeout=2), 0)

        receipts = []
        for watch_id, arm, paths in self.arms:
            receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
            self.assertEqual(receipt.get("watch_id"), watch_id)
            expected_state = "idle"
            # Terminal receipt settlement removes the mutable observation; the
            # receipt itself must retain the original native result.
            self.assertEqual(receipt.get("state"), expected_state)
            self.assertEqual(receipt.get("agent", {}).get("session_id"), arm["agent"]["session_id"])
            self.assertEqual(arm["deadline"], original_deadlines[watch_id])
            updated_arm = json.loads(paths.arm.read_text(encoding="utf-8"))
            self.assertEqual(updated_arm["deadline"], original_deadlines[watch_id])
            receipts.append(receipt)
        self.assertEqual(len({row["watch_id"] for row in receipts}), 6)
        self.assertEqual({row["steward"]["session_id"] for row in receipts},
                         {f"steward-{index}" for index in range(6)})
        notice_rows = self._watch_notice_records()
        notices_by_watch = {}
        for watch_id, _arm, _paths in self.arms:
            notices = [row for row in notice_rows
                       if (row.get("delivery") or {}).get("receipt") == watch_id]
            self.assertEqual(len(notices), 1,
                             f"watch {watch_id} must retain one exact notice record")
            notices_by_watch[watch_id] = notices[0]

        from dispatch_seat_handover import acknowledge_watch
        first_id = self.arms[0][0]
        self.assertEqual(acknowledge_watch(first_id, "claude", "steward-0", "lifecycle-test"),
                         "created")
        for index, (watch_id, _arm, paths) in enumerate(self.arms):
            self.assertEqual(paths.ack.exists(), index == 0,
                             "one watch acknowledgement must not settle another watch")
        first_ack_isolation = (self.arms[0][2].ack.exists()
                               and not any(paths.ack.exists()
                                           for _watch, _arm, paths in self.arms[1:]))
        for index, (watch_id, _arm, _paths) in enumerate(self.arms[1:], start=1):
            self.assertEqual(acknowledge_watch(
                watch_id, "claude", f"steward-{index}", "lifecycle-test"), "created")
        self.assertTrue(all(paths.ack.exists() for _id, _arm, paths in self.arms))
        herdr_calls = self._herdr_calls()
        self.assertEqual(herdr_calls, [],
                         "recovery must consume the durable native result without any Herdr call")
        self.assertEqual(hashlib.sha256(self.jobs.read_bytes()).hexdigest(), self.jobs_before,
                         "target readiness changed while jobs.log stayed byte-identical")
        runner_snapshots = len(self.snapshot_count_path.read_text().splitlines())
        observations_by_group = {}
        receipt_by_watch = {row["watch_id"]: row for row in receipts}
        for watch_id, arm, _paths in self.arms:
            native_state = receipt_by_watch[watch_id]["state"]
            identity = (arm["agent"]["pane"], arm["agent"]["session_id"], native_state)
            group = observations_by_group.setdefault(identity, [])
            group.append(watch_id)
        observation_groups = [
            {"pane": pane, "session_id": session_id, "native_state": native_state,
             "watch_ids": sorted(watch_ids), "watch_count": len(watch_ids)}
            for (pane, session_id, native_state), watch_ids in sorted(observations_by_group.items())
        ]
        EVIDENCE_ROWS.append({
            "case": "real-runner-five-stewards-plus-independent-target",
            "watch_count": 6,
            "same_target_count": 5,
            "legacy_watch_count": 1,
            "distinct_stewards": 6,
            "native_result_counts": {"idle": 6},
            "native_result_by_watch": {row["watch_id"]: row["state"] for row in receipts},
            "jobs_sha256_before_after": [self.jobs_before,
                                         hashlib.sha256(self.jobs.read_bytes()).hexdigest()],
            "runner_session_snapshot_calls": runner_snapshots,
            "first_observation_gate_seconds": round(gate_pause_seconds, 3),
            "runner_process_count": int(self.runner_pid is not None),
            "observation_groups": observation_groups,
            "acknowledged_watch": first_id,
            "first_ack_isolation": first_ack_isolation,
            "notice_records": {watch_id: 1 for watch_id in notices_by_watch},
            "notice_transfer_refs_present": {
                watch_id: bool(peer_steward._read_json(paths.arm).get("notice_transfer_ref"))
                for watch_id, _arm, paths in self.arms
            },
            "remaining_independent_receipts": sum(paths.receipt.exists()
                                                   for _id, _arm, paths in self.arms),
            "dead_previous_consumer_recovery": {
                "previous_pid": previous_consumer["pid"],
                "previous_pid_start": previous_consumer["pid_start"],
                "old_token_replaced": recovered["request"]["token"] !=
                    "dead-previous-consumer-token",
                "fresh_consumer_matches_watch": recovered["request"]["consumer"] ==
                    self.arms[0][1]["watcher"],
                "stale_pending_write_survived": recovered["observation"].get("reason") ==
                    "shared-readiness-revalidating",
                "old_contention_marker_absent": "readiness_contention" not in
                    recovered["observation"],
                "native_wait_replayed": False,
            },
            "herdr_calls_during_recovery": len(herdr_calls),
            "fixture_target_pid": os.getpid(),
            "host_production_pid_claim": False,
        })

    def test_one_runner_serves_concurrent_server_groups_when_secondary_starts_first(self):
        # One shared runner, started under the secondary server name, serves the watch
        # groups of both server names. The name only labels a group: each watch keeps the
        # `native:<server>` identity it was armed with, and both read the same native
        # target through the one runner.
        self._arm(20, server="secondary", target_override="peer-0")
        self._arm(21, server="default", target_override="peer-0")
        self.assertEqual([arm["readiness_endpoint"] for _watch_id, arm, _paths in self.arms],
                         ["native:secondary", "native:default"])
        store = peer_steward.peer_obligations.ObligationStore(self.state_root)
        self._hold_runner_lock()
        self._launch_watchers()
        self._start_runner()

        endpoint_identities = {
            watch_id: peer_steward._watch_readiness_identity(arm, "idle")
            for watch_id, arm, _paths in self.arms
        }
        for watch_id, arm, _paths in self.arms:
            identity = endpoint_identities[watch_id]
            self.assertEqual(identity["endpoint"], arm["readiness_endpoint"])
            self.assertTrue(peer_steward._watch_readiness_identity_matches_arm(
                identity, arm, "idle"))

        def runner_read_both_groups():
            # Each server name is its own exact target group in the runner's pass.
            return (self.endpoint.observe_count.value >= 2
                    and self.snapshot_count_path.exists())

        self._wait_until(runner_read_both_groups,
                         "the secondary-started runner did not read both server groups")
        runner_marker = peer_steward._read_json(
            store.root / "readiness-v1" / "runner.json") or {}
        runner_start = str(process_start_ticks(self.runner_pid) or "")
        self.assertEqual((int(runner_marker.get("pid", 0)), runner_marker.get("pid_start")),
                         (self.runner_pid, runner_start))
        # The target is still working: nothing is projected and nothing settles.
        self.assertFalse(any(paths.receipt.exists() for _watch_id, _arm, paths in self.arms))
        for identity in endpoint_identities.values():
            self.assertIsNone(shared_readiness.target_result(store.root, identity))

        self.set_idle()
        self._wait_until(lambda: all(paths.receipt.exists()
                                     for _watch_id, _arm, paths in self.arms),
                         "the one runner did not publish both endpoint receipts")
        self.assertEqual((peer_steward._read_json(
            store.root / "readiness-v1" / "runner.json") or {}).get("pid"),
            self.runner_pid, "both endpoint consumers must use the same runner")
        self._wait_until(lambda: all(process.poll() is not None for process in self.watchers),
                         "both endpoint watchers did not finish notice publication")
        for process in self.watchers:
            self.assertEqual(process.wait(timeout=2), 0)

        receipts = [json.loads(paths.receipt.read_text(encoding="utf-8"))
                    for _watch_id, _arm, paths in self.arms]
        self.assertEqual([receipt["server"] for receipt in receipts],
                         ["secondary", "default"])
        self.assertTrue(all(receipt["state"] == "idle" for receipt in receipts))
        self.assertEqual(self._herdr_calls(), [], "no server name may reach Herdr")

        from dispatch_seat_handover import acknowledge_watch
        first_id = self.arms[0][0]
        self.assertEqual(acknowledge_watch(
            first_id, "claude", "steward-20", "endpoint-lifecycle-test"), "created")
        self.assertTrue(self.arms[0][2].ack.exists())
        self.assertFalse(self.arms[1][2].ack.exists(),
                         "the first endpoint ACK stays scoped to its own watch")
        self.assertEqual(acknowledge_watch(
            self.arms[1][0], "claude", "steward-21", "endpoint-lifecycle-test"), "created")
        self.assertTrue(all(paths.ack.exists() for _watch_id, _arm, paths in self.arms))

        for index, (watch_id, arm, paths) in enumerate(self.arms):
            identity = endpoint_identities[watch_id]
            projection = shared_readiness.target_result(store.root, identity)
            self.assertEqual(projection["state"], "ready")
            self.assertEqual(arm["server"], receipts[index]["server"])
        endpoint_values = {watch_id: identity["endpoint"]
                           for watch_id, identity in endpoint_identities.items()}
        self.assertEqual(set(endpoint_values.values()),
                         {"native:default", "native:secondary"})
        self.assertEqual(len(set(endpoint_values.values())), 2)

        runner_pid = self.runner_pid
        watcher_pids = [process.pid for process in self.watchers]
        self._cleanup_processes()
        ENDPOINT_EVIDENCE_ROWS.append({
            "case": "one-normal-runner-concurrent-secondary-first-native-server-groups",
            "runner_starter_server": "secondary",
            "runner_pid": runner_pid,
            "runner_process_count": 1,
            "server_groups": ["secondary", "default"],
            "selected_endpoint_identities": endpoint_values,
            "native_endpoints": ["native:secondary", "native:default"],
            "receipts": {receipt["watch_id"]: receipt["state"] for receipt in receipts},
            "notices": {watch_id: 1 for watch_id, _arm, _paths in self.arms},
            "acknowledgements": {watch_id: paths.ack.exists()
                                 for watch_id, _arm, paths in self.arms},
            "herdr_calls": 0,
            "cleanup": {
                "runner_reaped": self.runner_pid is None,
                "runner_lock_fd_closed": self.runner_lock_fd is None,
                "watchers_reaped": all(process.poll() is not None for process in self.watchers),
                "watcher_pids": watcher_pids,
            },
        })

    def test_unattributed_native_session_stays_unknown_while_the_attributed_target_settles(self):
        # Native evidence is exact or it is unknown: a target the runtime has not
        # attributed to a session id (`-`) has no native read, so unknown is never turned
        # into an idle receipt, while the attributed target next to it settles normally.
        self._arm(30, server="fixture", target_override="peer-0", session_override="-")
        self._arm(31, server="fixture", target_override="peer-control", separate=True)
        sidless_id, sidless_arm, sidless_paths = self.arms[0]
        bound_id, bound_arm, bound_paths = self.arms[1]
        self.assertEqual(sidless_arm["agent"]["session_id"], "-")
        store = peer_steward.peer_obligations.ObligationStore(self.state_root)
        self._hold_runner_lock()
        self._launch_watchers()
        self._start_runner()
        self.set_idle()

        self._wait_until(lambda: bound_paths.receipt.exists(),
                         "the attributed native target did not reach its watch receipt")
        receipt = json.loads(bound_paths.receipt.read_text(encoding="utf-8"))
        self.assertEqual(receipt["state"], "idle")
        identity = peer_steward._watch_readiness_identity(bound_arm, "idle")
        projection = shared_readiness.target_result(store.root, identity)
        self.assertEqual((projection["state"], projection["reason"]),
                         ("ready", "native-turn-ready"))
        # Several more runner passes later the sidless watch has still settled nothing.
        before = self.endpoint.observe_count.value
        for _ in range(3):
            self.set_idle()
            time.sleep(0.3)
        self.assertFalse(sidless_paths.receipt.exists(),
                         "an unattributed native session must never settle as idle")
        self.assertIsNone(shared_readiness.target_result(
            store.root, peer_steward._watch_readiness_identity(sidless_arm, "idle")))
        self.assertEqual(self._herdr_calls(), [])
        from dispatch_seat_handover import acknowledge_watch
        self.assertEqual(acknowledge_watch(
            bound_id, "claude", "steward-31", "sidless-lifecycle-test"), "created")
        self.assertTrue(bound_paths.ack.exists())
        self.assertFalse(sidless_paths.ack.exists())
        runner_pid = self.runner_pid
        watcher_pids = [process.pid for process in self.watchers]
        self._cleanup_processes()
        SIDLESS_EVIDENCE_ROWS.append({
            "case": "unattributed-native-session-stays-unknown-attributed-target-settles",
            "sidless_arm_settled": False,
            "attributed_projection": {"state": projection["state"], "reason": projection["reason"]},
            "attributed_receipt": receipt["state"],
            "acknowledged": bound_paths.ack.exists(),
            "runner_reads_during_sidless_passes": self.endpoint.observe_count.value - before,
            "runner_pid": runner_pid,
            "runner_reaped": self.runner_pid is None,
            "watcher_pids": watcher_pids,
            "watchers_reaped": all(process.poll() is not None for process in self.watchers),
        })


EVIDENCE_ROWS = []
ENDPOINT_EVIDENCE_ROWS = []
SIDLESS_EVIDENCE_ROWS = []


STEWARD_TEST_FILES = ("peer_steward.test.py", "peer_steward_prompt.test.py", "peer_steward_retire.test.py")


def _selected_regressions():
    hook_tests = _load("lifecycle_rewake_tests", ROOT / "hooks" / "dispatch_owner_rewake.test.py")
    # peer_steward.test.py is split into three files; a case may live in any of them.
    steward_tests = tuple(_load("lifecycle_" + name.replace(".", "_"), HERE / name)
                          for name in STEWARD_TEST_FILES)

    def locate(modules, method):
        modules = modules if isinstance(modules, tuple) else (modules,)
        for module in modules:
            for value in vars(module).values():
                if (isinstance(value, type) and issubclass(value, unittest.TestCase)
                        and callable(getattr(value, method, None))):
                    return value(method)
        raise LookupError(f"test case not found: {modules[0].__name__}.{method}")

    cases = [
        (hook_tests, "test_wait_is_one_process_until_exact_attempt_is_ready"),
        (hook_tests, "test_canonical_wait_consumes_runner_projection_without_helper_process"),
        (hook_tests, "test_probe_timeout_keeps_the_exact_owner_wait_armed"),
        (hook_tests, "test_probe_timeout_preserves_the_original_wait_deadline"),
        (hook_tests, "test_lost_wake_is_not_acked_and_successor_claim_is_preserved"),
        (hook_tests, "test_gate_notices_survives_an_expired_claim"),
        (hook_tests, "test_the_probe_rescans_only_on_a_directory_write_or_an_expired_lease"),
        (hook_tests, "test_an_unannounced_probe_sleeps_and_the_wait_stays_bounded"),
        (hook_tests, "test_a_probe_raced_by_the_sweep_resumes_waiting_for_the_terminal"),
        (hook_tests, "test_main_wakes_once_on_the_gate_and_leaves_the_record_for_the_sweep"),
        (steward_tests, "test_watch_completion_consumes_real_bound_readiness_but_working_remains_native"),
        (steward_tests, "test_watch_consumes_pending_projection_and_rechecks_ready_projection"),
        (steward_tests, "test_runner_reads_once_for_five_watches_per_exact_native_target"),
        (steward_tests, "test_a_watch_marker_names_the_native_session_and_its_pane"),
        (steward_tests, "test_name_pane_and_native_id_aliases_share_one_watch"),
        (steward_tests, "test_a_session_change_in_the_same_process_without_lineage_stays_silent"),
        (steward_tests, "test_historical_exact_pending_receipt_closes_and_is_excluded_next_pass"),
        (steward_tests, "test_missing_pending_row_without_exact_receipt_remains_unknown"),
        (steward_tests, "test_stale_runner_observation_cannot_undo_foreground_exit_claim_or_completion"),
        (steward_tests, "test_real_recovery_preserves_history_and_hands_only_open_original_route_once"),
        (steward_tests, "test_work_accepted_at_unlock_is_reclaimed_before_runner_exit"),
        (steward_tests, "test_external_batch_created_at_unlock_invalidates_the_runner_snapshot"),
        (steward_tests, "test_batch_created_between_scan_and_stamp_is_reclaimed_by_runner"),
        (steward_tests, "test_scan_then_stamp_legacy_order_is_caught_at_real_runner_boundary"),
    ]
    suite = unittest.TestSuite()
    selected = []
    for module, method in cases:
        test = locate(module, method)
        selected.append(f"{test.__class__.__name__}.{method}")
        suite.addTest(test)
    return suite, selected


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args(argv)
    EVIDENCE_ROWS.clear()
    ENDPOINT_EVIDENCE_ROWS.clear()
    SIDLESS_EVIDENCE_ROWS.clear()
    suite = unittest.TestSuite([
        LifecycleRunnerProcessTest(
            "test_readiness_notification_uses_current_observer_after_stale_snapshot"),
        LifecycleRunnerProcessTest(
            "test_runner_recovers_independent_watches_and_refreshes_without_jobs_change"),
        LifecycleRunnerProcessTest(
            "test_one_runner_serves_concurrent_server_groups_when_secondary_starts_first"),
        LifecycleRunnerProcessTest(
            "test_unattributed_native_session_stays_unknown_while_the_attributed_target_settles"),
    ])
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(LifecycleProjectionTest))
    regression_suite, selected = _selected_regressions()
    suite.addTests(regression_suite)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    effective_arguments = list(argv) if argv is not None else list(sys.argv[1:])
    evidence_path = (str(args.evidence_dir / "shared-readiness-lifecycle.json")
                     if args.evidence_dir else None)
    evidence = {
        "schema_version": 1,
        "scope": "execute phase 2 lifecycle; isolated production-entry fixture",
        "base_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                       check=True, capture_output=True, text=True).stdout.strip(),
        "source_sha256": {
            "utilities/shared_readiness.py": _sha256(HERE / "shared_readiness.py"),
            "utilities/peer-steward.py": _sha256(HERE / "peer-steward.py"),
            **{"utilities/" + name: _sha256(HERE / name) for name in STEWARD_TEST_FILES},
            "utilities/shared_readiness_lifecycle.test.py": _sha256(Path(__file__)),
        },
        "selected_regressions": selected,
        "invocation": {
            "script": str(Path(__file__).resolve()),
            "effective_arguments": effective_arguments,
            "cwd": str(Path.cwd().resolve()),
            "evidence_path": evidence_path,
        },
        "tests_run": result.testsRun,
        "failures": [str(item[0]) + "\n" + item[1] for item in result.failures],
        "errors": [str(item[0]) + "\n" + item[1] for item in result.errors],
        "skipped": [[str(test), reason] for test, reason in result.skipped],
        "fixtures": EVIDENCE_ROWS,
        "endpoint_fixtures": ENDPOINT_EVIDENCE_ROWS,
        "sidless_fixtures": SIDLESS_EVIDENCE_ROWS,
        "limitations": [
            "The detached production watcher entrypoints and production cmd_obligation_runner ran as isolated local processes against real native session processes with their runtime registry records; no Herdr, socket or transport double is involved (a logging `herdr` on PATH asserts zero calls).",
            "All process and RSS/identity evidence in this phase is from an isolated local fixture.",
            "No host production PID was inspected or declared dead.",
        ],
    }
    if args.evidence_dir:
        args.evidence_dir.mkdir(parents=True, exist_ok=True)
        Path(evidence_path).write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
        print(f"evidence={evidence_path}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
