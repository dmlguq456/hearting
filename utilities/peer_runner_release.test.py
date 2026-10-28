#!/usr/bin/env python3
"""Isolated A→B service exec keeps one PID, lock and acknowledged duty."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import peer_obligations as obligations  # noqa: E402
import dispatch_pending_delivery as pending_delivery  # noqa: E402

_MESSAGE_SPEC = importlib.util.spec_from_file_location(
    "runner_release_peer_message", ROOT / "utilities/peer-message.py")
peer_message = importlib.util.module_from_spec(_MESSAGE_SPEC)
_MESSAGE_SPEC.loader.exec_module(peer_message)
_STEWARD_SPEC = importlib.util.spec_from_file_location(
    "runner_release_peer_steward", ROOT / "utilities/peer-steward.py")
peer_steward = importlib.util.module_from_spec(_STEWARD_SPEC)
_STEWARD_SPEC.loader.exec_module(peer_steward)


class ReleaseTransitionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.data = self.base / "data"
        self.releases = self.data / "releases"
        self.state = self.base / "state"
        self.ready = self.base / "duty-in-flight"
        self.release_boundary = self.base / "release-duty-boundary"
        self.b_ready = self.base / "b-duty-in-flight"
        self.b_release_boundary = self.base / "release-b-duty-boundary"
        self.send_entered = self.base / "external-send-entered.json"
        self.send_release = self.base / "external-send-release"
        self.submissions = self.base / "physical-submissions.jsonl"
        self.target_disappeared = self.base / "target-disappeared.json"
        self.target_backup = self.base / "B-paused"
        self.markers = self.base / "runner-starts.jsonl"
        self.ack_marker = self.base / "b-consumed-ack.json"
        self.b_observation = self.base / "b-before-duty.json"
        for path in (self.home, self.releases, self.state):
            path.mkdir(parents=True, exist_ok=True)
        self.a = self._release("A")
        self.b = self._release("B")
        self.target_backup = self.base / "B-paused"
        self.current = self.data / "current"
        self._point_current(self.a)
        self.body = "[handoff] release fixture message"
        self.sender = {"harness": "codex", "session_id": "sid-release-sender", "name": "sender"}
        self.recipient = {"harness": "codex", "session_id": "sid-release-recipient", "name": "recipient"}
        self.env = self._environment(self.a)
        # Every parent-side helper and cleanup starts in this private fixture.
        # Nested contexts select deliberate release/transport variants only.
        fixture_environment = mock.patch.dict(os.environ, self.env, clear=True)
        fixture_environment.start()
        self.addCleanup(fixture_environment.stop)
        self.ref, self.body_digest, self.duty_id = self._seed_acknowledged_duty()
        self.env["RUNNER_FIXTURE_TARGET_REF"] = self.ref
        os.environ["RUNNER_FIXTURE_TARGET_REF"] = self.ref

    def _release(self, version):
        root = self.releases / version
        shutil.copytree(ROOT / "utilities", root / "utilities",
                        ignore=shutil.ignore_patterns("*.test.py", "*.test.sh", "__pycache__", "*.pyc"))
        for relative in ("tools/install", "core"):
            shutil.copytree(ROOT / relative, root / relative,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (root / "RELEASE_VERSION").write_text(version + "\n", encoding="utf-8")
        shutil.copy2(ROOT / "harness-manifest.json", root / "harness-manifest.json")
        runner = root / "utilities/peer-steward.py"
        source = runner.read_text(encoding="utf-8")
        prelude = '''import json as _runner_fixture_json
import os as _runner_fixture_os
import sys as _runner_fixture_sys
from pathlib import Path as _RunnerFixturePath

def _runner_fixture_start_record():
    marker = _runner_fixture_os.environ.get("RUNNER_FIXTURE_MARKERS")
    if not marker:
        return
    argv = _runner_fixture_sys.argv
    fd = int(argv[argv.index("--lock-fd") + 1])
    state_root = argv[argv.index("--state-root") + 1]
    info = _runner_fixture_os.fstat(fd)
    record = {"release": _RunnerFixturePath(__file__).resolve().parents[1].name,
              "pid": _runner_fixture_os.getpid(), "fd": fd,
              "device": info.st_dev, "inode": info.st_ino,
              "state_root": state_root,
              "service_current": argv[argv.index("--service-current") + 1]
                  if "--service-current" in argv else None,
              "jobs": _runner_fixture_os.environ.get("AGENT_DISPATCH_JOBS")}
    with open(marker, "a", encoding="utf-8") as stream:
        stream.write(_runner_fixture_json.dumps(record, sort_keys=True) + "\\n")
        stream.flush()
        _runner_fixture_os.fsync(stream.fileno())
    if (record["release"] == "B"
            and _runner_fixture_os.environ.get("RUNNER_FAIL_B_STARTUP") == "1"):
        _runner_fixture_os._exit(41)

_runner_fixture_start_record()
'''
        fixture_wrapper = '''
import pane_ownership as _runner_fixture_pane_ownership

_runner_fixture_current_codex_thread = _runner_fixture_pane_ownership.current_codex_thread
def _runner_fixture_codex_thread(pane, pid=None, **kwargs):
    if pane == "fixture-pane":
        return os.environ.get("RUNNER_FIXTURE_NATIVE_SESSION_ID", "")
    return _runner_fixture_current_codex_thread(pane, pid, **kwargs)
_runner_fixture_pane_ownership.current_codex_thread = _runner_fixture_codex_thread

_runner_fixture_retire_foreground = _retire_foreground
def _runner_fixture_foreground(pane, harness, *, strict=True, info=None):
    if pane == "fixture-pane" and harness == "codex":
        observed = info or _retire_pane_info(pane)
        processes = (observed or {}).get("foreground_processes") or ()
        pid = processes[0].get("pid") if processes and isinstance(processes[0], dict) else 1
        return {"pid": pid, "start": "fixture-birth", "argv": ["codex"],
                "shell_pid": (observed or {}).get("shell_pid", 1),
                "shell_start": "fixture-shell-birth"}
    return _runner_fixture_retire_foreground(
        pane, harness, strict=strict, info=info)
_retire_foreground = _runner_fixture_foreground

_production_resume_message_obligation = _resume_message_obligation

def _fixture_resume_message_obligation(duty, store, *, read_context=None):
    release = Path(__file__).resolve().parents[1].name
    duty_ref = (duty.get("intent") or {}).get("ref")
    target_ref = os.environ.get("RUNNER_FIXTURE_TARGET_REF")
    if release == "A" and os.environ.get("RUNNER_FIXTURE_READY"):
        ready = Path(os.environ["RUNNER_FIXTURE_READY"])
        if not ready.exists():
            ready.write_text(duty["id"], encoding="utf-8")
            boundary = Path(os.environ["RUNNER_FIXTURE_RELEASE"])
            deadline = time.monotonic() + 15
            while not boundary.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not boundary.exists():
                raise RuntimeError("fixture-boundary-timeout")
            return
    if (release == "A" and os.environ.get("RUNNER_FIXTURE_HOLD_DUTY") == "1"
            and duty_ref == target_ref):
        return
    if (release == "B" and os.environ.get("RUNNER_FIXTURE_B_READY")
            and duty_ref == target_ref):
        ready = Path(os.environ["RUNNER_FIXTURE_B_READY"])
        if not ready.exists():
            ready.write_text(duty["id"], encoding="utf-8")
            boundary = Path(os.environ["RUNNER_FIXTURE_B_RELEASE"])
            deadline = time.monotonic() + 15
            while not boundary.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not boundary.exists():
                raise RuntimeError("fixture-b-boundary-timeout")
            if os.environ.get("RUNNER_FIXTURE_B_CONTINUE") != "1":
                return
    if release == "B" and duty_ref == target_ref:
        ref = (duty.get("intent") or {}).get("ref")
        observed = peer_message._read_pending(ref)
        Path(os.environ["RUNNER_FIXTURE_OBSERVATION"]).write_text(
            json.dumps({"state": (observed or {}).get("state"),
                        "ref": ref,
                        "ledger_root": str(peer_message.peer_state_root()),
                        "env_ledger_root": os.environ.get("AGENT_PEER_LEDGER_ROOT")},
                       sort_keys=True), encoding="utf-8")
    result = _production_resume_message_obligation(
        duty, store, read_context=read_context)
    if (release in {"A", "B"} and store.get(duty["id"]).get("state") == "complete"
            and (not target_ref or duty_ref == target_ref)):
        received = peer_message._read_pending((duty.get("intent") or {}).get("ref"))
        record = {"release": release, "duty_id": duty["id"],
                  "ref": (duty.get("intent") or {}).get("ref"),
                  "body_sha256": (received or {}).get("body_sha256"),
                  "transfer_state": (received or {}).get("state")}
        Path(os.environ["RUNNER_FIXTURE_ACK_MARKER"]).write_text(
            json.dumps(record, sort_keys=True), encoding="utf-8")
    return result

_resume_message_obligation = _fixture_resume_message_obligation
'''
        source = prelude + source.replace(
            '\nif __name__ == "__main__":\n', fixture_wrapper + '\nif __name__ == "__main__":\n')
        runner.write_text(source, encoding="utf-8")
        (root / "utilities/codex_queue_dispatch.py").write_text(
            "def resolve_queue_socket():\n    return 'fixture-queue'\n", encoding="utf-8")
        (root / "utilities/codex_queue_delivery.py").write_text('''
import hashlib
import json
import os
import time
from pathlib import Path

class QueueDeliveryError(RuntimeError):
    def __init__(self, message, *, ambiguous=False):
        super().__init__(message)
        self.ambiguous = ambiguous

def send_at_least_once(socket, *, thread_id, client_message_id, message, timeout,
                       allow_restart, retry_ambiguous, allow_add, require_message_match,
                       before_add):
    if before_add:
        before_add()
    digest = hashlib.sha256(message.encode()).hexdigest()
    entered = os.environ.get("RUNNER_FIXTURE_SEND_ENTERED")
    release = os.environ.get("RUNNER_FIXTURE_SEND_RELEASE")
    if entered:
        record = {"thread_id": thread_id, "client_message_id": client_message_id,
                  "body_sha256": digest, "allow_add": allow_add}
        Path(entered).write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        deadline = time.monotonic() + 15
        while release and not Path(release).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if release and not Path(release).exists():
            raise RuntimeError("fixture-send-timeout")
    submissions = os.environ.get("RUNNER_FIXTURE_SUBMISSIONS")
    if submissions:
        with open(submissions, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"thread_id": thread_id,
                "client_message_id": client_message_id, "body_sha256": digest},
                sort_keys=True) + "\\n")
            stream.flush()
            os.fsync(stream.fileno())
    return {"status": "queued"}
''', encoding="utf-8")
        service = root / "utilities/peer_obligations.py"
        service_source = service.read_text(encoding="utf-8")
        service_hook = '''
_fixture_real_active_service_root = active_service_root
def _fixture_active_service_root(service_current=None, *, root=None):
    target = _fixture_real_active_service_root(service_current, root=root)
    marker = os.environ.get("RUNNER_FIXTURE_TARGET_DISAPPEARED")
    backup = os.environ.get("RUNNER_FIXTURE_TARGET_BACKUP")
    if (target and marker and backup and Path(__file__).resolve().parents[1].name == "A"
            and not Path(marker).exists() and target.name == "B"):
        target.rename(backup)
        Path(marker).write_text(str(target), encoding="utf-8")
    return target
active_service_root = _fixture_active_service_root
'''
        service.write_text(service_source + service_hook, encoding="utf-8")
        self._install_fixture_herdr()
        return root

    def _install_fixture_herdr(self):
        binary_dir = self.base / "fixture-bin"
        binary_dir.mkdir(exist_ok=True)
        herdr = binary_dir / "herdr"
        herdr.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:2] == ["agent", "get"]:
    result = {"result": {"agent": {"agent": "codex", "agent_status": "idle",
        "agent_session": {"value": "sid-release-recipient"}, "name": "recipient",
        "pane_id": "fixture-pane"}}}
elif args[:2] == ["pane", "process-info"]:
    pid = os.getppid()
    group = os.getpgid(pid)
    result = {"result": {"process_info": {"pane_id": "fixture-pane", "shell_pid": pid,
        "foreground_process_group_id": group,
        "foreground_processes": [{"pid": group, "argv": ["codex"]}]}}}
elif args[:2] == ["agent", "explain"]:
    print("rule: none")
    raise SystemExit(0)
else:
    result = {"result": {}}
print(json.dumps(result))
''', encoding="utf-8")
        herdr.chmod(0o700)

    def _environment(self, code_root):
        jobs = self.base / "dispatch" / "jobs.log"
        jobs.parent.mkdir(parents=True, exist_ok=True)
        jobs.touch(exist_ok=True)
        return {
            "HOME": str(self.home),
            "HARNESS_DATA_ROOT": str(self.data),
            "XDG_DATA_HOME": str(self.base / "xdg-data"),
            "XDG_STATE_HOME": str(self.base / "xdg-state"),
            "AGENT_HOME": str(code_root),
            "AGENT_PEER_LEDGER_ROOT": str(self.state),
            "AGENT_DISPATCH_JOBS": str(jobs),
            "PYTHONPATH": str(code_root / "utilities"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": str(self.base / "fixture-bin") + os.pathsep + os.environ.get("PATH", ""),
            "RUNNER_FIXTURE_READY": str(self.ready),
            "RUNNER_FIXTURE_RELEASE": str(self.release_boundary),
            "RUNNER_FIXTURE_MARKERS": str(self.markers),
            "RUNNER_FIXTURE_ACK_MARKER": str(self.ack_marker),
            "RUNNER_FIXTURE_OBSERVATION": str(self.b_observation),
            "RUNNER_FIXTURE_SUBMISSIONS": str(self.submissions),
            "RUNNER_FIXTURE_NATIVE_SESSION_ID": self.recipient["session_id"],
        }

    def _point_current(self, target):
        replacement = self.data / "current.next"
        replacement.unlink(missing_ok=True)
        replacement.symlink_to(target, target_is_directory=True)
        os.replace(replacement, self.current)

    def _seed_acknowledged_duty(self):
        text, ref = peer_message.prepare_peer_message(
            self.body, self.sender, self.recipient, defer=True,
            refs=["release-fixture-duty"], receipt="fixture-awaiting-recipient")
        body_digest = peer_message._read_pending(ref)["body_sha256"]
        self.assertEqual(peer_message.receive_peer_message(text, self.recipient), 0)
        received = peer_message._read_pending(ref)
        self.assertEqual(received["state"], "received")
        with mock.patch.object(obligations, "ensure_normal_runner", return_value=False):
            duty = peer_steward._schedule_message_obligation(
                received, "fixture-target", pane="fixture-pane")
        self.assertIsNotNone(duty)
        self.assertEqual(duty["intent"]["from"], received["from"])
        self.assertEqual(duty["intent"]["to"], received["to"])
        self.assertEqual(duty["intent"]["refs"], received["refs"])
        return ref, body_digest, duty["id"]

    def _seed_pending_duty(self, body):
        text, ref = peer_message.prepare_peer_message(
            body, self.sender, self.recipient, defer=True,
            refs=["release-fixture-pending"], receipt="fixture-awaiting-recipient")
        row = peer_message._read_pending(ref)
        store = obligations.ObligationStore(self.state)
        duty_id = "message-" + ref
        store.create(duty_id, "message",
                     {"server": "default", "pane": "fixture-pane",
                      "session_id": self.recipient["session_id"]},
                     {"ref": ref, "body_digest": row["body_sha256"],
                      "target": "fixture-target", "from": self.sender,
                      "to": self.recipient, "refs": row["refs"],
                      "identity": {"server": "default", "pane": "fixture-pane"}})
        return text, ref, row["body_sha256"], duty_id

    def _wait_for(self, path, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for fixture state: {path.name}")

    def _pending(self, root=None, ref=None):
        environ = self._environment(root or self.b)
        with mock.patch.dict(os.environ, environ, clear=True):
            return peer_message._read_pending(ref or self.ref)

    def _start_managed_runner(self, root, environ):
        obligations._RUNNERS.clear()
        with mock.patch.object(obligations, "__file__",
                               str(root / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, environ, clear=True):
            binding = obligations.managed_service_binding(root)
            self.assertEqual(binding, str(self.current))
            self.assertTrue(obligations.ensure_runner(self.state, service_current=binding))
        self.assertTrue(obligations._RUNNERS)
        return obligations._RUNNERS[-1]

    def _assert_same_lock_handoff(self, records):
        self.assertEqual([row["release"] for row in records], ["A", "B"])
        for key in ("pid", "fd", "device", "inode", "state_root"):
            self.assertEqual(records[0][key], records[1][key], key)
        self.assertEqual(records[0]["state_root"], str(self.state.resolve()))

    def _seed_dispatch_phase(self, delivery_id, state):
        receipt = {"schema_version": 2, "state": "delivered",
                   "parent_attempt_id": "att-matrix-parent",
                   "job_registry": str(self.state / "jobs.log"),
                   "delivery_classification": "attention",
                   "children": [{"attempt_id": "att-matrix-child", "status": "done",
                                 "readiness": "ready", "reason": "fixture",
                                 "required_action": "inspect-done-failure",
                                 "harness": "claude", "delivery_classification": "attention"}]}
        value = pending_delivery.create(
            self.state, recipient_kind="claude-parent-runtime", recipient_key="sid-matrix",
            delivery_id=delivery_id, session_generation="", session_generation_supported="0",
            attempt_ids=["att-matrix-child"], parent_attempt_id="att-matrix-parent",
            route_id="route-matrix", route_node="execute", receipt=receipt,
            receipt_digest=pending_delivery._canonical_receipt_digest(receipt),
            row_revisions={"att-matrix-child": "fixture"})
        if state in {"claimed", "sent-ambiguous"}:
            value = pending_delivery.claim(
                self.state, "sid-matrix", delivery_id,
                claim_owner="matrix-" + delivery_id, lease_seconds=60)
        if state == "sent-ambiguous":
            value = pending_delivery.mark_sent_ambiguous(
                self.state, "sid-matrix", delivery_id,
                claim_owner="matrix-" + delivery_id)
        return value

    def test_current_switch_execs_b_at_safe_boundary_and_consumes_exact_ack(self):
        proc = self._start_managed_runner(self.a, self.env)
        try:
            self._wait_for(self.ready)
            self.assertEqual(self.ready.read_text(encoding="utf-8"), self.duty_id)
            self.assertEqual([json.loads(line)["release"] for line in self.markers.read_text().splitlines()], ["A"])

            self._point_current(self.b)
            lock_path = self.state / "peer-steward" / "obligations" / obligations.RUNNER_LOCK_NAME
            probe = os.open(lock_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    import fcntl
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)

            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, self.env, clear=True), \
                 mock.patch.object(obligations.subprocess, "Popen") as concurrent:
                self.assertTrue(obligations.ensure_runner(self.state,
                                                          service_current=str(self.current)))
                concurrent.assert_not_called()

            self.assertFalse(self.ack_marker.exists(), "B cannot consume before A leaves its duty")
            self.release_boundary.touch()
            self.assertEqual(proc.wait(timeout=15), 0)
            records = [json.loads(line) for line in self.markers.read_text().splitlines()]
            self._assert_same_lock_handoff(records)
            ack = json.loads(self.ack_marker.read_text(encoding="utf-8"))
            self.assertEqual(ack, {"release": "B", "duty_id": self.duty_id, "ref": self.ref,
                                   "body_sha256": self.body_digest,
                                   "transfer_state": "received"})
            duty = obligations.ObligationStore(self.state).get(self.duty_id)
            self.assertEqual((duty["state"], duty["result"], duty["delivery"]),
                             ("complete", "received", "acknowledged"))
            self.assertEqual(self._pending()["state"], "received")
            observed = json.loads(self.b_observation.read_text(encoding="utf-8"))
            self.assertEqual(observed["state"], "received", observed)
        finally:
            self.release_boundary.touch()
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            obligations._RUNNERS.clear()

    def test_post_exec_startup_failure_keeps_acknowledged_duty_for_next_lifecycle(self):
        failing_env = dict(self.env, RUNNER_FAIL_B_STARTUP="1")
        proc = self._start_managed_runner(self.a, failing_env)
        try:
            self._wait_for(self.ready)
            self._point_current(self.b)
            self.release_boundary.touch()
            self.assertEqual(proc.wait(timeout=15), 41)
            duty = obligations.ObligationStore(self.state).get(self.duty_id)
            self.assertEqual(duty["state"], "pending")
            self.assertEqual(self._pending()["state"], "received")
            first_records = [json.loads(line) for line in self.markers.read_text().splitlines()]
            self.assertEqual([row["release"] for row in first_records], ["A", "B"])
            self.assertEqual(first_records[0]["pid"], first_records[1]["pid"])

            recovery_env = self._environment(self.b)
            recovered = self._start_managed_runner(self.b, recovery_env)
            self.assertEqual(recovered.wait(timeout=15), 0)
            duty = obligations.ObligationStore(self.state).get(self.duty_id)
            self.assertEqual((duty["state"], duty["result"], duty["delivery"]),
                             ("complete", "received", "acknowledged"))
            ack = json.loads(self.ack_marker.read_text(encoding="utf-8"))
            self.assertEqual((ack["ref"], ack["body_sha256"], ack["transfer_state"]),
                             (self.ref, self.body_digest, "received"))
        finally:
            self.release_boundary.touch()
            for process in obligations._RUNNERS:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            obligations._RUNNERS.clear()

    def test_current_rollback_reexecs_again_without_dropping_the_acknowledged_duty(self):
        rollback_env = dict(self.env,
                            RUNNER_FIXTURE_B_READY=str(self.b_ready),
                            RUNNER_FIXTURE_B_RELEASE=str(self.b_release_boundary))
        proc = self._start_managed_runner(self.a, rollback_env)
        try:
            self._wait_for(self.ready)
            self._point_current(self.b)
            self.release_boundary.touch()
            self._wait_for(self.b_ready)
            self.assertFalse(self.ack_marker.exists(), "B remains inside its duty until the rollback boundary")

            self._point_current(self.a)
            self.b_release_boundary.touch()
            self.assertEqual(proc.wait(timeout=15), 0)
            records = [json.loads(line) for line in self.markers.read_text().splitlines()]
            self.assertEqual([row["release"] for row in records], ["A", "B", "A"])
            for key in ("pid", "fd", "device", "inode", "state_root"):
                self.assertEqual(len({row[key] for row in records}), 1, key)
            ack = json.loads(self.ack_marker.read_text(encoding="utf-8"))
            self.assertEqual(ack, {"release": "A", "duty_id": self.duty_id,
                                   "ref": self.ref, "body_sha256": self.body_digest,
                                   "transfer_state": "received"})
            duty = obligations.ObligationStore(self.state).get(self.duty_id)
            self.assertEqual((duty["state"], duty["result"], duty["delivery"]),
                             ("complete", "received", "acknowledged"))
        finally:
            self.release_boundary.touch()
            self.b_release_boundary.touch()
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            obligations._RUNNERS.clear()

    def test_real_claim_and_submission_cross_release_before_exact_ack(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            original = obligations.ObligationStore(self.state).get(self.duty_id)
            obligations.ObligationStore(self.state).update(
                self.duty_id, state="complete", result="received",
                delivery="acknowledged", cleanup="complete")
            text, ref, digest, duty_id = self._seed_pending_duty(
                "[handoff] in-flight production transport")
        env = dict(self.env,
                   RUNNER_FIXTURE_TARGET_REF=ref,
                   RUNNER_FIXTURE_SEND_ENTERED=str(self.send_entered),
                   RUNNER_FIXTURE_SEND_RELEASE=str(self.send_release),
                   RUNNER_FIXTURE_B_READY=str(self.b_ready),
                   RUNNER_FIXTURE_B_RELEASE=str(self.b_release_boundary),
                   RUNNER_FIXTURE_B_CONTINUE="1")
        env.pop("RUNNER_FIXTURE_READY", None)
        env.pop("RUNNER_FIXTURE_RELEASE", None)
        self.ready.unlink(missing_ok=True)
        self.send_entered.unlink(missing_ok=True)
        self.send_release.unlink(missing_ok=True)
        self.b_ready.unlink(missing_ok=True)
        self.b_release_boundary.unlink(missing_ok=True)
        proc = self._start_managed_runner(self.a, env)
        try:
            self._wait_for(self.send_entered)
            claim = json.loads(self.send_entered.read_text(encoding="utf-8"))
            self.assertEqual(claim, {"thread_id": self.recipient["session_id"],
                                     "client_message_id": "delivery-peer-" + ref,
                                     "body_sha256": digest, "allow_add": True})
            claimed = self._pending(self.a, ref=ref)
            self.assertEqual((claimed["state"], claimed["body_sha256"]), ("unverified", digest))
            self.assertIsInstance(claimed.get("rpc_claim"), dict)

            self._point_current(self.b)
            self.assertEqual([json.loads(line)["release"]
                              for line in self.markers.read_text().splitlines()], ["A"])
            self.assertIsNone(proc.poll(), "A retains its process while production transport is held")
            lock_path = self.state / "peer-steward" / "obligations" / obligations.RUNNER_LOCK_NAME
            probe = os.open(lock_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    import fcntl
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
            self.assertEqual(obligations.ObligationStore(self.state).get(duty_id)["state"], "pending")

            self.send_release.touch()
            self._wait_for(self.b_ready)
            records = [json.loads(line) for line in self.markers.read_text().splitlines()]
            self._assert_same_lock_handoff(records)
            self.assertEqual(records[0]["service_current"], str(self.current))
            self.assertEqual(records[1]["service_current"], str(self.current))
            self.assertEqual(records[1]["jobs"], env["AGENT_DISPATCH_JOBS"])
            self.assertEqual(self._pending(self.b, ref=ref)["state"], "queued")
            self.assertEqual(len(self.submissions.read_text().splitlines()), 1)
            submission = json.loads(self.submissions.read_text().splitlines()[0])
            self.assertEqual(submission, {"thread_id": self.recipient["session_id"],
                                          "client_message_id": "delivery-peer-" + ref,
                                          "body_sha256": digest})
            self.assertFalse(self.ack_marker.exists())

            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(peer_message.receive_peer_message(text, self.recipient), 0)
            self.assertEqual(self._pending(self.b, ref=ref)["state"], "received")
            self.b_release_boundary.touch()
            self.assertEqual(proc.wait(timeout=15), 0)
            duty = obligations.ObligationStore(self.state).get(duty_id)
            self.assertEqual((duty["state"], duty["result"], duty["delivery"]),
                             ("complete", "received", "acknowledged"))
            ack = json.loads(self.ack_marker.read_text(encoding="utf-8"))
            self.assertEqual(ack, {"release": "B", "duty_id": duty_id, "ref": ref,
                                   "body_sha256": digest, "transfer_state": "received"})
            self.assertEqual(obligations.ObligationStore(self.state).get(self.duty_id)["state"],
                             "complete")
            self.assertEqual(original["state"], "pending")
        finally:
            self.send_release.touch()
            self.b_release_boundary.touch()
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            obligations._RUNNERS.clear()

    def test_target_disappearance_during_preparation_keeps_a_locked_until_retry(self):
        env = dict(self.env,
                   RUNNER_FIXTURE_TARGET_DISAPPEARED=str(self.target_disappeared),
                   RUNNER_FIXTURE_TARGET_BACKUP=str(self.target_backup),
                   RUNNER_FIXTURE_HOLD_DUTY="1")
        proc = self._start_managed_runner(self.a, env)
        try:
            self._wait_for(self.ready)
            self._point_current(self.b)
            self.release_boundary.touch()
            self._wait_for(self.target_disappeared)
            self.assertFalse(self.b.exists())
            self.assertTrue(self.target_backup.is_dir())
            self.assertIsNone(proc.poll(), "pre-exec preparation failure must keep A alive")
            records = [json.loads(line) for line in self.markers.read_text().splitlines()]
            self.assertEqual([row["release"] for row in records], ["A"])
            duty = obligations.ObligationStore(self.state).get(self.duty_id)
            self.assertEqual(duty["state"], "pending")
            lock_path = self.state / "peer-steward" / "obligations" / obligations.RUNNER_LOCK_NAME
            probe = os.open(lock_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    import fcntl
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
            self.target_backup.rename(self.b)
            self.assertEqual(proc.wait(timeout=15), 0)
            records = [json.loads(line) for line in self.markers.read_text().splitlines()]
            self._assert_same_lock_handoff(records)
            duty = obligations.ObligationStore(self.state).get(self.duty_id)
            self.assertEqual((duty["state"], duty["result"]), ("complete", "received"))
        finally:
            self.release_boundary.touch()
            if self.target_backup.exists() and not self.b.exists():
                self.target_backup.rename(self.b)
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            obligations._RUNNERS.clear()

    def test_delivery_phase_matrix_and_registered_gate_then_terminal_ack_cross_release(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            dispatch_phases = {
                state: self._seed_dispatch_phase("delivery-matrix-" + state, state)
                for state in ("pending", "claimed", "sent-ambiguous")
            }
            message_phases = {}
            for state in ("pending", "unverified", "queued", "received"):
                recipient = {"harness": "codex", "session_id": "sid-phase-" + state,
                             "name": state}
                text, ref = peer_message.prepare_peer_message(
                    "[notice] state phase " + state, self.sender, recipient, defer=True,
                    refs=["phase-matrix"] , with_trailer=(state != "queued"))
                if state == "unverified":
                    self.assertIsNotNone(peer_message.claim_pending_herdr(ref, recipient))
                elif state == "queued":
                    self.assertIsNotNone(peer_message.claim_pending_herdr(ref, recipient))
                    self.assertTrue(peer_message.mark_pending_queued(ref))
                elif state == "received":
                    self.assertEqual(peer_message.receive_peer_message(text, recipient), 0)
                message_phases[state] = {"ref": ref,
                                         "body_sha256": peer_message._read_pending(ref)["body_sha256"]}

        env = dict(self.env, RUNNER_FIXTURE_B_READY=str(self.b_ready),
                   RUNNER_FIXTURE_B_RELEASE=str(self.b_release_boundary),
                   RUNNER_FIXTURE_B_CONTINUE="1")
        self.ready.unlink(missing_ok=True)
        self.b_ready.unlink(missing_ok=True)
        self.b_release_boundary.unlink(missing_ok=True)
        proc = self._start_managed_runner(self.a, env)
        try:
            self._wait_for(self.ready)
            self._point_current(self.b)
            self.release_boundary.touch()
            self._wait_for(self.b_ready)
            records = [json.loads(line) for line in self.markers.read_text().splitlines()]
            self._assert_same_lock_handoff(records)

            # The exact ACK is received only after B owns the same runner lock.
            jobs = self.state / "jobs.log"
            jobs.parent.mkdir(parents=True, exist_ok=True)
            attempt_id = "att-registered-fixture"
            duty_id = "registered-batch-gate-fixture"
            sid = "sid-registered-fixture"
            carrier = "claude-parent-runtime"
            gate_delivery = "delivery-registered-gate"
            terminal_delivery = "delivery-registered-terminal"
            owner = "retained-rewake:" + duty_id
            open_meta = ("attempt_schema_version=2,dispatch_depth=1,attempt_id=" + attempt_id
                         + ",parent_sid=" + sid + ",parent_harness=claude,harness=claude"
                         + ",parent_completion_delivery=" + carrier)
            jobs.write_text("2026-10-16T00:00:00Z\topen\t/repo\t/wt\tfixture\t"
                            + open_meta + "\n", encoding="utf-8")
            identity = {"jobs": str(jobs.resolve()), "attempt_id": attempt_id,
                        "session_id": sid, "harness": "claude", "server": "default"}
            store = obligations.ObligationStore(self.state)
            duty = store.create(duty_id, "registered-batch", identity,
                                {"carrier": carrier})

            def seed_registered_delivery(delivery_id, receipt_kind):
                receipt = {"schema_version": 2, "state": "delivered",
                           "kind": receipt_kind,
                           "parent_attempt_id": "att-registered-parent",
                           "job_registry": str(jobs), "delivery_classification": "attention",
                           "children": [{"attempt_id": attempt_id, "status": "done",
                                         "readiness": "ready", "reason": "fixture",
                                         "required_action": "inspect-done-failure",
                                         "harness": "claude",
                                         "delivery_classification": "attention"}]}
                pending_delivery.create(
                    self.state, recipient_kind=carrier, recipient_key=sid,
                    delivery_id=delivery_id, session_generation="",
                    session_generation_supported="0", attempt_ids=[attempt_id],
                    parent_attempt_id="att-registered-parent", route_id="route-fixture",
                    route_node="execute", receipt=receipt,
                    receipt_digest=pending_delivery._canonical_receipt_digest(receipt),
                    row_revisions={attempt_id: "fixture"})
                pending_delivery.claim(self.state, sid, delivery_id,
                                       claim_owner=owner, lease_seconds=60)
                return {"duty_id": duty_id, "jobs": str(jobs), "attempt_id": attempt_id,
                        "carrier": carrier, "recipient_sid": sid,
                        "delivery_id": delivery_id, "storage_recipient": sid,
                        "claim_owner": owner}

            sender = {"harness": "codex", "session_id": "sid-registered-sender",
                      "name": "sender"}
            recipient = {"harness": "claude", "session_id": sid, "name": "parent"}
            gate_binding = seed_registered_delivery(gate_delivery, "human-gate")
            with mock.patch.dict(os.environ, env, clear=True):
                gate_text, gate_ref = peer_message.prepare_peer_message(
                    "[gate] registered human gate", sender, recipient,
                    defer=True, refs=[duty_id])
                peer_message.bind_dispatch_notice(gate_ref, gate_binding)
                gate_pending = peer_message._read_pending(gate_ref)
            bindings = list(obligations._registered_delivery_bindings(gate_pending))
            self.assertEqual(len(bindings), 1, (gate_pending, store.get(duty_id)))
            _bound_store, _bound_duty, bound_row, bound_record, _root, _storage, _delivery = bindings[0]
            self.assertEqual(bound_row.status, "open")
            self.assertEqual(bound_record["claim_owner"], gate_binding["claim_owner"])
            from dispatch_seat_handover import effective_parent
            self.assertEqual(effective_parent(bound_row.metadata, jobs), sid)
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(peer_message.parse_peer_trailer(
                    gate_text, recipient, include_ref=True)["transfer_ref"], gate_ref)
            with mock.patch.object(obligations, "acknowledge_registered_delivery",
                                   wraps=obligations.acknowledge_registered_delivery) as ack_handler:
                with mock.patch.object(peer_message, "_ack_registered_delivery",
                                       wraps=peer_message._ack_registered_delivery) as ack_dispatch, \
                     mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(peer_message.receive_peer_message(gate_text, recipient), 0)
                    gate_received = peer_message._read_pending(gate_ref)
                    self.assertEqual(gate_received["state"], "received")
                ack_dispatch.assert_called_once()
                ack_handler.assert_called_once()
            gate_record = pending_delivery.read(self.state, sid, gate_delivery)
            self.assertEqual(gate_record["state"], "acked")
            self.assertEqual(store.get(duty_id)["state"], "pending",
                             "a gate ACK alone must leave the registered duty open")
            self.assertEqual(gate_pending["body_sha256"], gate_received["body_sha256"])

            final_meta = open_meta + ",delivery_id=" + terminal_delivery
            jobs.write_text("2026-10-16T00:00:00Z\topen\t/repo\t/wt\tfixture\t"
                            + open_meta + "\n"
                            + "2026-10-16T00:00:01Z\tdone\t/repo\t/wt\tfixture\t"
                            + final_meta + "\n", encoding="utf-8")
            terminal_binding = seed_registered_delivery(terminal_delivery, "notice")
            with mock.patch.dict(os.environ, env, clear=True):
                terminal_text, terminal_ref = peer_message.prepare_peer_message(
                    "[notice] registered terminal delivery", sender, recipient,
                    defer=True, refs=[duty_id])
                peer_message.bind_dispatch_notice(terminal_ref, terminal_binding)
                self.assertEqual(peer_message.receive_peer_message(terminal_text, recipient), 0)
                completed = store.get(duty_id)
                self.assertEqual(peer_message.receive_peer_message(terminal_text, recipient), 0)
            self.assertEqual((completed["state"], completed["delivery"], completed["cleanup"]),
                             ("complete", "acknowledged", "complete"))
            self.assertEqual(store.get(duty_id), completed,
                             "replaying the exact terminal ACK must not complete twice")
            self.assertEqual(pending_delivery.read(self.state, sid, terminal_delivery)["state"],
                             "acked")

            self.b_release_boundary.touch()
            self.assertEqual(proc.wait(timeout=15), 0)

            for state, expected in (("pending", "pending"), ("claimed", "claimed"),
                                    ("sent-ambiguous", "sent-ambiguous")):
                self.assertEqual(pending_delivery.read(
                    self.state, "sid-matrix", "delivery-matrix-" + state)["state"], expected)
            for state, snapshot in message_phases.items():
                with mock.patch.dict(os.environ, env, clear=True):
                    current = peer_message._read_pending(snapshot["ref"])
                expected = {"pending": "pending", "unverified": "unverified",
                            "queued": "queued", "received": "received"}[state]
                self.assertEqual((current["state"], current["body_sha256"]),
                                 (expected, snapshot["body_sha256"]))
            claimed_delivery = dispatch_phases["claimed"]
            with self.assertRaisesRegex(pending_delivery.PendingDeliveryError,
                                        "lease-not-expired"):
                pending_delivery.reclaim(
                    self.state, "sid-matrix", "delivery-matrix-claimed",
                    now_ns=claimed_delivery["claim_deadline_ns"] - 1)
            reclaimed = pending_delivery.reclaim(
                self.state, "sid-matrix", "delivery-matrix-sent-ambiguous",
                now_ns=dispatch_phases["sent-ambiguous"]["claim_deadline_ns"] + 1)
            self.assertEqual(reclaimed["state"], "pending")
        finally:
            self.release_boundary.touch()
            self.b_release_boundary.touch()
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            obligations._RUNNERS.clear()

    def test_lifecycle_binding_survives_idle_exit_for_later_pinned_message_producer(self):
        store = obligations.ObligationStore(self.state)
        store.update(self.duty_id, state="complete", result="received",
                     delivery="acknowledged", cleanup="complete")
        lifecycle_env = dict(self.env)
        lifecycle_env.pop("RUNNER_FIXTURE_READY", None)
        lifecycle_env.pop("RUNNER_FIXTURE_RELEASE", None)
        lifecycle_env["RUNNER_FIXTURE_TARGET_REF"] = self.ref
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, lifecycle_env, clear=True), \
             mock.patch.object(peer_steward, "_ensure_watch_observers"), \
             mock.patch.object(peer_steward.dispatch_batch_obligations, "ensure_observers"):
            self.assertEqual(peer_steward.cmd_ensure_obligations(
                SimpleNamespace(lifecycle_root=str(self.a))), 0)
        lock_path = store.root / obligations.RUNNER_LOCK_NAME
        lock_identity = (lock_path.stat().st_dev, lock_path.stat().st_ino)
        control = json.loads(lock_path.read_text(encoding="utf-8"))
        self.assertEqual(control["service_current"], str(self.current))

        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, lifecycle_env, clear=True):
            first_text, first_ref = peer_message.prepare_peer_message(
                "[notice] runner settling before current changes",
                self.sender, self.recipient, defer=True, refs=["idle-lifecycle"])
            first_row = peer_message._read_pending(first_ref)
            self.assertEqual(peer_message.receive_peer_message(first_text, self.recipient), 0)
            first_duty = peer_steward._schedule_message_obligation(
                first_row, "fixture-target", "fixture-pane")
            first_id = "message-" + first_ref
            self.assertEqual(first_duty["id"], first_id)
            first_runner = obligations._RUNNERS[-1]
        self.assertEqual(first_runner.wait(timeout=15), 0)
        self.assertEqual(store.get(first_id)["state"], "complete")

        self._point_current(self.b)
        second_env = dict(lifecycle_env, RUNNER_FIXTURE_TARGET_REF="")
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, second_env, clear=True):
            text, ref = peer_message.prepare_peer_message(
                "[notice] ordinary producer follows remembered managed current",
                self.sender, self.recipient, defer=True, refs=["ordinary-after-idle"])
            digest = peer_message._read_pending(ref)["body_sha256"]
            row = peer_message._read_pending(ref)
            duty = peer_steward._schedule_message_obligation(row, "fixture-target", "fixture-pane")
            duty_id = "message-" + ref
            self.assertEqual(duty["id"], duty_id)
            second_runner = obligations._RUNNERS[-1]
        self._wait_for(self.submissions)
        second_marker = [json.loads(line) for line in self.markers.read_text().splitlines()][-1]
        self.assertEqual(second_marker["release"], "B")
        self.assertEqual(second_marker["service_current"], str(self.current))
        self.assertEqual(second_marker["jobs"], lifecycle_env["AGENT_DISPATCH_JOBS"])
        self.assertEqual(second_marker["state_root"], str(self.state.resolve()))
        self.assertEqual((lock_path.stat().st_dev, lock_path.stat().st_ino), lock_identity)
        self.assertEqual(os.environ.get("AGENT_HOME", lifecycle_env["AGENT_HOME"]),
                         lifecycle_env["AGENT_HOME"])
        with mock.patch.dict(os.environ, second_env, clear=True):
            self.assertEqual(peer_message.receive_peer_message(text, self.recipient), 0)
        self.assertEqual(second_runner.wait(timeout=15), 0)
        self.assertEqual(store.get(duty_id)["state"], "complete")
        submitted = [json.loads(line) for line in self.submissions.read_text().splitlines()]
        self.assertEqual(submitted[-1], {"thread_id": self.recipient["session_id"],
                                         "client_message_id": "delivery-peer-" + ref,
                                         "body_sha256": digest})
        obligations._RUNNERS.clear()

    def test_mixed_release_hands_legacy_reader_off_before_pending_quarantine(self):
        """A current pane-bound schema-1 row and a future row survive A→B takeover."""
        pane_target = {"harness": "claude", "session_id": None,
                       "name": "fresh", "pane_id": "w1:p-mixed"}
        proof = {"schema": "peer-first-input-process-v1", "harness": "claude",
                 "pane_id": pane_target["pane_id"], "pid": os.getpid(),
                 "start": "123", "pid_namespace": os.readlink("/proc/self/ns/pid")}
        with mock.patch.dict(os.environ, self.env, clear=True):
            pane_text, pane_ref = peer_message.prepare_peer_message(
                "[steer] current pane-bound payload", self.sender, pane_target,
                defer=True, first_input_process=proof)
            claim = peer_message.claim_pending_first_input(pane_ref, pane_target, proof)
            self.assertIsNotNone(claim)
            self.assertTrue(peer_message.mark_pending_prompt_queued(
                pane_ref, claim["rpc_claim"], receipt="native-first-input-submitted"))
            _future_text, future_ref = peer_message.prepare_peer_message(
                "[steer] future schema payload", self.sender, self.recipient, defer=True)
            future_path = peer_message._pending_path(future_ref)
            future_row = json.loads(future_path.read_text(encoding="utf-8"))
            # Schema 2 is the current unresolved-address variant; 3 remains future.
            future_path.write_text(json.dumps(dict(future_row, schema_version=3)),
                                   encoding="utf-8")
            initial_bytes = {
                pane_ref: peer_message._pending_path(pane_ref).read_bytes(),
                future_ref: future_path.read_bytes(),
            }

        # Model the prior managed reader's two failure branches in its isolated
        # release copy: it has no first-input exception and classifies unknown
        # schema as generic malformed data, which the scan quarantines.
        legacy_path = self.a / "utilities" / "peer-message.py"
        legacy_source = legacy_path.read_text(encoding="utf-8")
        schema_guard = 'raise UnsupportedPendingSchema("peer-pending-schema-unsupported")'
        pane_guard = 'and row.get("receipt") == "native-first-input-submitted"'
        self.assertIn(schema_guard, legacy_source)
        self.assertIn(pane_guard, legacy_source)
        legacy_source = legacy_source.replace(schema_guard, "pass  # pre-takeover legacy reader")
        legacy_source = legacy_source.replace(pane_guard, "and False  # pre-takeover legacy reader")
        legacy_path.write_text(legacy_source, encoding="utf-8")

        probe_root = self.base / "legacy-reader-probe"
        shutil.copytree(self.state / "peer-messages", probe_root / "peer-messages")
        legacy_spec = importlib.util.spec_from_file_location(
            "mixed_release_legacy_peer_message", legacy_path)
        legacy_reader = importlib.util.module_from_spec(legacy_spec)
        legacy_spec.loader.exec_module(legacy_reader)
        probe_env = dict(self.env, AGENT_PEER_LEDGER_ROOT=str(probe_root))
        with mock.patch.dict(os.environ, probe_env, clear=True):
            self.assertEqual(list(legacy_reader._pending_rows()), [])
        for ref in (pane_ref, future_ref):
            self.assertEqual(len(list((probe_root / "peer-messages" / "quarantine").glob(
                f"*/{ref}.json"))), 1)

        old_runner = self._start_managed_runner(self.a, self.env)
        try:
            self._wait_for(self.ready)
            self.assertEqual([json.loads(line)["release"]
                              for line in self.markers.read_text().splitlines()], ["A"])
            self._point_current(self.b)
            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                    mock.patch.dict(os.environ, self.env, clear=True), \
                    mock.patch.object(obligations.subprocess, "Popen") as concurrent:
                binding = obligations.managed_service_binding(self.a)
                self.assertEqual(binding, str(self.current))
                self.assertTrue(obligations.ensure_runner(self.state, service_current=binding))
                concurrent.assert_not_called()
            self.release_boundary.touch()
            self.assertEqual(old_runner.wait(timeout=15), 0)
            records = [json.loads(line) for line in self.markers.read_text().splitlines()]
            self._assert_same_lock_handoff(records)
            self.assertEqual(json.loads(self.ack_marker.read_text())["release"], "B")

            current_spec = importlib.util.spec_from_file_location(
                "mixed_release_current_peer_message", self.b / "utilities" / "peer-message.py")
            current_reader = importlib.util.module_from_spec(current_spec)
            current_spec.loader.exec_module(current_reader)
            with mock.patch.dict(os.environ, self._environment(self.b), clear=True):
                rows = list(current_reader._pending_rows())
                by_ref = {row["ref"]: row for row in rows}
                self.assertEqual(by_ref[pane_ref]["text"], pane_text)
                with self.assertRaises(current_reader.UnsupportedPendingSchema):
                    current_reader._read_pending(future_ref)
                self.assertEqual(current_reader._pending_path(pane_ref).read_bytes(),
                                 initial_bytes[pane_ref])
                self.assertEqual(current_reader._pending_path(future_ref).read_bytes(),
                                 initial_bytes[future_ref])
                self.assertTrue(current_reader._pending_path(future_ref).exists())
        finally:
            self.release_boundary.touch()
            if old_runner.poll() is None:
                old_runner.terminate()
                old_runner.wait(timeout=5)
            obligations._RUNNERS.clear()


if __name__ == "__main__":
    unittest.main()
