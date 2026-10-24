#!/usr/bin/env python3
"""Bounded production-entry lifecycle checks for shared peer readiness."""

from __future__ import annotations

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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))

from dispatch_contract import process_start_ticks  # noqa: E402
import shared_readiness  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


peer_steward = _load("lifecycle_peer_steward", HERE / "peer-steward.py")

_FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
log = os.environ.get("LIFECYCLE_HERDR_LOG")
if log:
    with open(log, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(argv) + "\n")
if argv[:1] == ["--session"]:
    argv = argv[2:]
if argv[:2] == ["agent", "get"]:
    target = next((item for item in argv[2:] if not item.startswith("-")), "peer-0")
    control = target == "peer-control"
    sid, pane = ("sid-b", "w1:p2") if control else ("sid-a", "w1:p1")
    if os.environ.get("LIFECYCLE_SIDLESS") == "1":
        sid = "-"
    try:
        state = json.loads(open(os.environ["LIFECYCLE_STATE_FILE"], encoding="utf-8").read())
        status = state.get("status", "working")
    except Exception:
        status = "working"
    print(json.dumps({"result": {"agent": {
        "agent": "claude", "agent_session": {"value": sid},
        "agent_status": status, "name": target, "pane_id": pane}}}))
    raise SystemExit(0)
if argv[:2] == ["pane", "process-info"]:
    pane = argv[-1]
    pid = int(os.environ["LIFECYCLE_SHELL_PID"])
    pgid = int(os.environ["LIFECYCLE_SHELL_PGID"])
    process_argv = json.loads(os.environ["LIFECYCLE_SHELL_ARGV"])
    print(json.dumps({"result": {"process_info": {
        "pane_id": pane, "shell_pid": pid,
        "foreground_process_group_id": pgid,
        "foreground_processes": [{"pid": pid, "argv": process_argv}]}}}))
    raise SystemExit(0)
print(json.dumps({"result": {}}))
'''


class FakeReadinessTransport:
    """Pipe-woken runner transport; avoids host Herdr and socket dependencies."""

    def __init__(self, state_file, count_path, *, sidless=False):
        self.state_file = Path(state_file)
        self.count_path = Path(count_path)
        self.sidless = sidless
        context = multiprocessing.get_context("fork")
        self.observe_count = context.Value("i", 0)
        self.pause_after = context.Value("i", 2**31 - 1)
        self.pause_entered = context.Event()
        self.resume = context.Event()
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        os.set_blocking(self.write_fd, False)

    def fileno(self):
        return self.read_fd if self.read_fd >= 0 else None

    def observe(self):
        with self.observe_count.get_lock():
            self.observe_count.value += 1
            sequence = self.observe_count.value
        if sequence > self.pause_after.value:
            self.pause_entered.set()
            if not self.resume.wait(20):
                raise TimeoutError("lifecycle observation gate was not released")
        while self.read_fd >= 0:
            try:
                if not os.read(self.read_fd, 4096):
                    break
            except BlockingIOError:
                break
            except OSError:
                break
        with self.count_path.open("a", encoding="utf-8") as stream:
            stream.write("snapshot\n")
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        status = state.get("status", "working")
        sid_a = "-" if self.sidless else "sid-a"
        agents = [{"name": f"peer-{index}", "agent": "claude",
                   "agent_session": {"value": sid_a}, "pane_id": "w1:p1",
                   "agent_status": status}
                  for index in range(5)]
        agents.append({"name": "peer-control", "agent": "claude",
                       "agent_session": {"value": "-" if self.sidless else "sid-b"},
                       "pane_id": "w1:p2",
                       "agent_status": status})
        return SimpleNamespace(agents=tuple(agents))

    def set_idle(self):
        self.state_file.write_text(json.dumps({"status": "idle"}), encoding="utf-8")
        try:
            os.write(self.write_fd, b"changed")
        except (BlockingIOError, OSError):
            pass

    def close(self):
        for name in ("read_fd", "write_fd"):
            fd = getattr(self, name)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, -1)


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
        self.target_shell = subprocess.Popen(
            ["/bin/sh", "-c", "trap 'exit 0' TERM; while :; do sleep 1; done"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self._stop_target_shell)
        fake = self.bin / "herdr"
        fake.write_text(_FAKE_HERDR, encoding="utf-8")
        fake.chmod(0o755)
        self.state_file = self.base / "target-state.json"
        self.state_file.write_text(json.dumps({"status": "working"}), encoding="utf-8")
        self.snapshot_count_path = self.base / "runner-snapshots.log"
        self.herdr_log_path = self.base / "herdr-argv.jsonl"
        self.api_path = self.base / "herdr.sock"
        self.endpoint = FakeReadinessTransport(self.state_file, self.snapshot_count_path)
        self.old_env = dict(os.environ)
        for key in tuple(os.environ):
            if key.startswith(("AGENT_DISPATCH_", "AGENT_PEER_", "AGENT_HERDR_",
                               "AGENT_SESSION_", "HERDR_", "CLAUDE_", "CODEX_",
                               "OPENCODE_", "XDG_")):
                os.environ.pop(key, None)
        os.environ.update({
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_PEER_LEDGER_ROOT": str(self.state_root),
            "AGENT_HERDR_SESSION": "fixture",
            "HERDR_SOCKET_PATH": str(self.api_path),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "LIFECYCLE_STATE_FILE": str(self.state_file),
            "LIFECYCLE_SHELL_PID": str(self.target_shell.pid),
            "LIFECYCLE_SHELL_PGID": str(os.getpgid(self.target_shell.pid)),
            "LIFECYCLE_SHELL_ARGV": json.dumps([
                part.decode("utf-8", errors="replace") for part in
                Path(f"/proc/{self.target_shell.pid}/cmdline").read_bytes().split(b"\0")
                if part]),
            "LIFECYCLE_HERDR_LOG": str(self.herdr_log_path),
            "PATH": str(self.bin) + os.pathsep + self.old_env.get("PATH", ""),
        })
        self.addCleanup(self._restore_env)
        self.addCleanup(self.endpoint.close)
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

    def _stop_target_shell(self):
        if self.target_shell.poll() is None:
            self.target_shell.terminate()
        try:
            self.target_shell.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.target_shell.kill()
            self.target_shell.wait(timeout=2)

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

    def _start_runner(self, transport_selector=None):
        import traceback

        control_read, control_write = os.pipe()
        old_transport = peer_steward._runner_readiness_transport
        old_transports = dict(peer_steward._RUNNER_READINESS_TRANSPORTS)
        peer_steward._RUNNER_READINESS_TRANSPORTS.clear()
        peer_steward._runner_readiness_transport = (
            transport_selector or (lambda _server, endpoint=None: self.endpoint))
        try:
            child = os.fork()
        except BaseException:
            peer_steward._runner_readiness_transport = old_transport
            peer_steward._RUNNER_READINESS_TRANSPORTS.clear()
            peer_steward._RUNNER_READINESS_TRANSPORTS.update(old_transports)
            raise
        if child == 0:
            os.close(control_write)
            try:
                os.read(control_read, 1)
                os.close(control_read)
                code = peer_steward.cmd_obligation_runner(SimpleNamespace(
                    lock_fd=self.runner_lock_fd, state_root=self.state_root,
                    service_current=None))
            except BaseException:
                traceback.print_exc()
                code = 1
            os._exit(int(code or 0))
        peer_steward._runner_readiness_transport = old_transport
        peer_steward._RUNNER_READINESS_TRANSPORTS.clear()
        peer_steward._RUNNER_READINESS_TRANSPORTS.update(old_transports)
        self.runner_pid = child
        os.close(control_read)
        os.write(control_write, b"go")
        os.close(control_write)
        os.close(self.runner_lock_fd)
        self.runner_lock_fd = None

    def _arm(self, index, *, legacy=False, separate=False, previous_consumer=None,
             server="fixture", target_override=None, session_override=None):
        import session_tidy

        watch_id = hashlib.sha256(f"lifecycle-{index}".encode()).hexdigest()[:16]
        target = target_override or ("peer-control" if separate else f"peer-{index}")
        pane, sid = ("w1:p2", "sid-b") if separate else ("w1:p1", "sid-a")
        if session_override is not None:
            sid = session_override
        arm = {
            "schema_version": peer_steward._WATCH_SCHEMA,
            "watch_id": watch_id,
            "target": target,
            "until": [],
            "timeout": 300000,
            "refs": [f"receipt-{index}"],
            "server": server,
            "readiness_endpoint": (
                peer_steward.herdr_observation.socket_path_for_endpoint(server)
                or "server:" + peer_steward.herdr_observation.endpoint_name(server)),
            "agent": {"harness": "claude", "session_id": sid,
                      "name": target, "pane": pane},
            "steward": {"harness": "claude", "session_id": f"steward-{index}",
                        "project": str(self.base)},
            "steward_seat": session_tidy.Seat(
                "pane", f"fixture-seat-{index}", f"w1:p{8 + index}", "claude",
                sid=f"steward-{index}", endpoint=server).as_dict(),
            "armed_ts": peer_steward._utc_now(),
            "deadline": peer_steward._watch_deadline(300000),
            "observer_protocol": 2,
            "observer_generation": 1,
            "rearm_count": 0,
            "watcher": {},
        }
        if not legacy:
            arm["pane_identity"] = {
                "shell_pid": self.target_shell.pid,
                "shell_start": str(process_start_ticks(self.target_shell.pid)),
            }
        paths = peer_steward._watch_paths(watch_id, self.watch_root)
        peer_steward._write_json_atomic(paths.arm, arm)
        native_state = "done" if index == 1 else "idle"
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
            self._arm(index, legacy=(index == 0),
                      previous_consumer=previous_consumer if index == 0 else None)
        self._arm(5, separate=True)
        original_deadlines = {watch_id: arm["deadline"]
                              for watch_id, arm, _paths in self.arms}
        store = peer_steward.peer_obligations.ObligationStore(self.state_root)
        self._hold_runner_lock()
        self._launch_watchers()
        self._start_runner()

        def pending_projection_exists():
            for watch_id, arm, _paths in self.arms:
                observation = peer_steward._read_json(
                    peer_steward._watch_paths(watch_id, self.watch_root).observation) or {}
                native = observation.get("native_wait") or {}
                identity = peer_steward._watch_readiness_identity(arm, native["agent_state"])
                if identity is None:
                    return False
                result = shared_readiness.target_result(store.root, identity)
                if not result or result.get("state") != "pending":
                    return False
            return True

        self._wait_until(pending_projection_exists,
                         "real runner did not publish pending projections for recovered native results")
        self.assertFalse(any(paths.receipt.exists() for _watch, _arm, paths in self.arms))
        self.endpoint.pause_after.value = self.endpoint.observe_count.value + 1
        self.endpoint.set_idle()
        self.assertTrue(self.endpoint.pause_entered.wait(10),
                        "real runner did not pause after publishing ready projections")

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

        self._wait_until(
            fresh_request_survived_outer_pending_write,
            "fresh current-consumer request did not survive the stale cmd_watch_run pending write",
            timeout=10)
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
        self.endpoint.resume.set()

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
            expected_state = "done" if arm["target"] == "peer-1" else "idle"
            # Terminal receipt settlement removes the mutable observation; the
            # receipt itself must retain the original native result.
            self.assertIn(receipt.get("state"), {"idle", "done"})
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
        herdr_calls = [json.loads(line) for line in
                       self.herdr_log_path.read_text(encoding="utf-8").splitlines()]
        self.assertFalse(any(call[:2] == ["agent", "wait"] for call in herdr_calls),
                         "recovery must consume the durable native result without replaying wait")
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
            "native_result_counts": {"idle": 5, "done": 1},
            "native_result_by_watch": {row["watch_id"]: row["state"] for row in receipts},
            "jobs_sha256_before_after": [self.jobs_before,
                                         hashlib.sha256(self.jobs.read_bytes()).hexdigest()],
            "runner_session_snapshot_calls": runner_snapshots,
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
            "herdr_wait_calls_during_recovery": sum(
                call[:2] == ["agent", "wait"] for call in herdr_calls),
            "fixture_target_pid": os.getpid(),
            "host_production_pid_claim": False,
        })

    def test_one_runner_serves_concurrent_endpoints_when_secondary_starts_first(self):
        # Start the shared runner under the secondary session, then make it
        # process pending duties for both endpoint names through each watch's
        # persisted selected identity and explicit CLI server argument.
        self.state_file.write_text(json.dumps({"status": "working"}), encoding="utf-8")
        os.environ["AGENT_HERDR_SESSION"] = "secondary"
        os.environ["HERDR_SESSION"] = "secondary"
        os.environ["HERDR_SOCKET_PATH"] = "/fixture/secondary.sock"
        self._arm(20, server="secondary", target_override="peer-0")
        os.environ["AGENT_HERDR_SESSION"] = "default"
        os.environ.pop("HERDR_SESSION", None)
        os.environ["HERDR_SOCKET_PATH"] = "/fixture/default.sock"
        self._arm(21, server="default", target_override="peer-0")
        self.assertEqual([arm["readiness_endpoint"] for _watch_id, arm, _paths in self.arms],
                         ["/fixture/secondary.sock", "/fixture/default.sock"])
        # Runner startup context is secondary, while the default watch keeps
        # the exact URI selected when it was armed.
        os.environ["AGENT_HERDR_SESSION"] = "secondary"
        os.environ["HERDR_SESSION"] = "secondary"
        os.environ["HERDR_SOCKET_PATH"] = "/fixture/secondary.sock"
        store = peer_steward.peer_obligations.ObligationStore(self.state_root)
        self._hold_runner_lock()
        self._launch_watchers()
        selected_endpoints = {("secondary", "/fixture/secondary.sock"),
                              ("default", "/fixture/default.sock")}

        def endpoint_transport(server, endpoint=None):
            if (server, endpoint) not in selected_endpoints:
                raise AssertionError(f"runner borrowed the wrong endpoint: {server}/{endpoint}")
            return self.endpoint

        self._start_runner(endpoint_transport)

        endpoint_identities = {
            watch_id: peer_steward._watch_readiness_identity(arm, "idle")
            for watch_id, arm, _paths in self.arms
        }

        def both_endpoint_projections_are_pending():
            found = {}
            for watch_id, arm, _paths in self.arms:
                identity = endpoint_identities[watch_id]
                if (identity["endpoint"] != arm["readiness_endpoint"]
                        or not peer_steward._watch_readiness_identity_matches_arm(
                            identity, arm, "idle")):
                    return False
                projection = shared_readiness.target_result(store.root, identity)
                if (not projection or projection.get("state") != "pending"
                        or projection.get("reason") != "native-turn-busy"):
                    return False
                found[arm["server"]] = identity
            return set(found) == {"default", "secondary"}

        self._wait_until(both_endpoint_projections_are_pending,
                         "the secondary-started runner did not project both pending endpoint groups")
        runner_marker = peer_steward._read_json(
            store.root / "readiness-v1" / "runner.json") or {}
        runner_start = str(process_start_ticks(self.runner_pid) or "")
        self.assertEqual((int(runner_marker.get("pid", 0)), runner_marker.get("pid_start")),
                         (self.runner_pid, runner_start))

        self.endpoint.set_idle()
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
        calls = [json.loads(line) for line in self.herdr_log_path.read_text(
            encoding="utf-8").splitlines()]
        self.assertTrue(any(call[:3] == ["--session", "secondary", "agent"]
                            and call[3:5] == ["get", "peer-0"] for call in calls), calls)
        self.assertTrue(any(call[:2] == ["agent", "get"] and call[2] == "peer-0"
                            for call in calls), calls)

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
                         {"/fixture/default.sock", "/fixture/secondary.sock"})
        self.assertEqual(len(set(endpoint_values.values())), 2)

        runner_pid = self.runner_pid
        watcher_pids = [process.pid for process in self.watchers]
        self._cleanup_processes()
        ENDPOINT_EVIDENCE_ROWS.append({
            "case": "one-normal-runner-concurrent-secondary-first-qualified-endpoints",
            "runner_starter_server": "secondary",
            "runner_pid": runner_pid,
            "runner_process_count": 1,
            "server_groups": ["secondary", "default"],
            "selected_endpoint_identities": endpoint_values,
            "qualified_endpoint_paths": ["/fixture/secondary.sock",
                                          "/fixture/default.sock"],
            "receipts": {receipt["watch_id"]: receipt["state"] for receipt in receipts},
            "notices": {watch_id: 1 for watch_id, _arm, _paths in self.arms},
            "acknowledgements": {watch_id: paths.ack.exists()
                                 for watch_id, _arm, paths in self.arms},
            "cli_calls_scoped_by_server": True,
            "cleanup": {
                "runner_reaped": self.runner_pid is None,
                "runner_lock_fd_closed": self.runner_lock_fd is None,
                "watchers_reaped": all(process.poll() is not None for process in self.watchers),
                "watcher_pids": watcher_pids,
            },
        })

    def test_sidless_native_result_is_published_consumed_and_acknowledged(self):
        self.endpoint.sidless = True
        os.environ["LIFECYCLE_SIDLESS"] = "1"
        self.state_file.write_text(json.dumps({"status": "idle"}), encoding="utf-8")
        self._arm(30, server="fixture", target_override="peer-0", session_override="-")
        watch_id, arm, paths = self.arms[0]
        self._hold_runner_lock()
        self._launch_watchers()
        self._start_runner(lambda _server, endpoint=None: self.endpoint)

        self._wait_until(lambda: paths.receipt.exists(),
                         "sidless native result did not reach the watch receipt")
        receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
        observation = peer_steward._read_json(paths.observation) or {}
        native_session_id = (observation.get("native_wait") or {}).get(
            "agent", {}).get("session_id")
        if native_session_id in {None, ""}:
            native_session_id = "-"
        identity = peer_steward._watch_readiness_identity(arm, "idle")
        store = peer_steward.peer_obligations.ObligationStore(self.state_root)
        projection = shared_readiness.target_result(store.root, identity)
        self.assertEqual((arm["agent"]["session_id"],
                          native_session_id,
                          receipt["state"], projection["state"], projection["reason"]),
                         ("-", "-", "idle", "ready", "native-turn-ready"))
        from dispatch_seat_handover import acknowledge_watch
        self.assertEqual(acknowledge_watch(
            watch_id, "claude", "steward-30", "sidless-lifecycle-test"), "created")
        self.assertTrue(paths.ack.exists())
        runner_pid = self.runner_pid
        watcher_pids = [process.pid for process in self.watchers]
        self._cleanup_processes()
        SIDLESS_EVIDENCE_ROWS.append({
            "case": "sidless-complete-census-native-to-runner-to-receipt-ack",
            "sidless_arm_and_native_result": True,
            "projection": {"state": projection["state"], "reason": projection["reason"]},
            "receipt": receipt["state"],
            "acknowledged": paths.ack.exists(),
            "runner_pid": runner_pid,
            "runner_reaped": self.runner_pid is None,
            "watcher_pids": watcher_pids,
            "watchers_reaped": all(process.poll() is not None for process in self.watchers),
        })


EVIDENCE_ROWS = []
ENDPOINT_EVIDENCE_ROWS = []
SIDLESS_EVIDENCE_ROWS = []


def _selected_regressions():
    hook_tests = _load("lifecycle_rewake_tests", ROOT / "hooks" / "dispatch_owner_rewake.test.py")
    steward_tests = _load("lifecycle_peer_steward_tests", HERE / "peer_steward.test.py")

    def locate(module, method):
        for value in vars(module).values():
            if (isinstance(value, type) and issubclass(value, unittest.TestCase)
                    and callable(getattr(value, method, None))):
                return value(method)
        raise LookupError(f"test case not found: {module.__name__}.{method}")

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
        (steward_tests, "test_unchanged_registry_does_not_cache_liveness_or_terminal_status"),
        (steward_tests, "test_replacement_incomplete_census_and_observation_errors_use_full_readiness"),
        (steward_tests, "test_watch_consumes_pending_projection_and_rechecks_ready_projection"),
        (steward_tests, "test_runner_reads_once_for_five_stewards_and_separates_another_pane"),
        (steward_tests, "test_sidless_watch_marker_uses_resolved_name_and_pane"),
        (steward_tests, "test_name_pane_and_agent_id_aliases_share_one_watch_and_native_wait"),
        (steward_tests, "test_terminal_event_requires_a_fresh_snapshot_that_rejects_reused_pane_id"),
        (steward_tests, "test_historical_exact_pending_receipt_closes_and_is_excluded_next_pass"),
        (steward_tests, "test_missing_pending_row_without_exact_receipt_remains_unknown"),
        (steward_tests, "test_live_rpc_claim_blocks_terminal_cancellation"),
        (steward_tests, "test_dead_pane_bound_process_closes_but_unresolved_process_does_not"),
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
            "test_runner_recovers_independent_watches_and_refreshes_without_jobs_change"),
        LifecycleRunnerProcessTest(
            "test_one_runner_serves_concurrent_endpoints_when_secondary_starts_first"),
        LifecycleRunnerProcessTest(
            "test_sidless_native_result_is_published_consumed_and_acknowledged"),
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
            "utilities/peer_steward.test.py": _sha256(HERE / "peer_steward.test.py"),
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
            "The detached production watcher entrypoints and production cmd_obligation_runner ran as isolated local processes; only the runner observation transport was pipe-backed test data because this sandbox denies local Unix-socket bind.",
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
