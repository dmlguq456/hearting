#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "utilities" / "codex-managed-completion.py"
PARENT = "att-parent"
SESSION = "thread-managed-parent"


def native_add_result(result):
    """A successful fake native add still executes the production boundary callback."""
    def send(*_args, **kwargs):
        # An exact existing queue item is observation, so the real API never
        # enters before_add on that branch (including allow_add=False).
        if not result.get("already_pending") and kwargs.get("before_add"):
            kwargs["before_add"]()
        return result
    return send


def load_completion_module():
    sys.path.insert(0, str(ROOT / "utilities"))
    spec = importlib.util.spec_from_file_location(
        "codex_managed_completion_unit", SIDECAR
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def watcher_record(module, root, delivery_id, receipt, *, carrier="codex-native-queue", epoch="1"):
    """Complete private store record for the existing gate/watcher fixtures."""
    pending = module.pending_delivery
    return pending.create(root, recipient_kind=carrier, recipient_key=SESSION,
        delivery_id=delivery_id, session_generation=epoch, session_generation_supported="1",
        attempt_ids=["att-owner"], parent_attempt_id="att-owner",
        route_id="rt-notice", route_node="execute", receipt=receipt,
        receipt_digest=pending._canonical_receipt_digest(receipt),
        row_revisions={"att-owner": str(receipt.get("sealed_batch_id", "batch-session"))})


def row(
    attempt_id: str,
    *,
    harness: str,
    status: str = "done",
    parent: str = PARENT,
) -> str:
    return (
        f"2026-07-27T00:00:00Z\t{status}\t/repo\t/wt\tchild\t"
        "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        f"attempt_id={attempt_id},parent_attempt_id={parent},"
        f"harness={harness},note=RAW_CHILD_SENTINEL\n"
    )


def session_row(attempt_id: str, *, harness: str) -> str:
    return (
        f"2026-07-27T00:00:00Z\tdone\t/repo\t/wt\tchild\t"
        "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        "launch_claimed=1,launch_outcome=never-launched,"
        "parent_completion_delivery=codex-managed-gateway,"
        f"attempt_id={attempt_id},parent_sid={SESSION},harness={harness},"
        "note=RAW_SESSION_CHILD_SENTINEL\n"
    )


class ControlServer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(1)
        self.request: dict[str, Any] | None = None
        self.called = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            data = bytearray()
            while b"\n" not in data:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
            if not data:
                connection.close()
                continue
            request = json.loads(bytes(data).split(b"\n", 1)[0])
            if request.get("op") == "status":
                response = {
                    "schema_version": 1, "status": "ready",
                    "capabilities": {"human_gate_delivery": {
                        "version": 1, "thread_id": SESSION, "epoch": 1,
                    }},
                }
            else:
                self.request = request
                self.called.set()
                response = {
                    "schema_version": 1,
                    "status": "accepted",
                    "delivery_id": "dlv-fixture",
                    "action": "start",
                    "replay": False,
                }
            connection.sendall(
                (json.dumps(response, separators=(",", ":")) + "\n").encode()
            )
            connection.close()
            if request.get("op") != "status":
                return

    def close(self) -> None:
        self.listener.close()
        self.thread.join(timeout=2)


class ParentLifetimeStopTest(unittest.TestCase):
    def setUp(self):
        self.module = load_completion_module()
        self.binding = {"pid": 123, "pid_start": "456", "session_id": SESSION,
                        "pane": "w1:p9", "socket_path": "", "pid_namespace": "ns"}
        self.args = SimpleNamespace(parent_session_id=SESSION, thread_id=SESSION,
                                    parent_lifetime=dict(self.binding), jobs=Path("/jobs"))

    def test_capture_binds_one_native_tui_process_start(self):
        binding = {key: value for key, value in self.binding.items() if key != "pid_namespace"}
        with mock.patch("pane_ownership.codex_tui_process_bindings", return_value=[binding]), \
             mock.patch.object(self.module, "process_namespace_identity", side_effect=["ns", "ns"]):
            self.assertEqual(self.module.capture_parent_lifetime(self.args), self.binding)

    def _unbound_args(self):
        return SimpleNamespace(parent_session_id=SESSION, thread_id=SESSION,
                               jobs=Path("/jobs"))

    def test_unbound_lifetime_without_session_tui_or_successor_ends_the_observer(self):
        args = self._unbound_args()
        with mock.patch.object(self.module, "process_namespace_identity", return_value="ns"), \
             mock.patch("pane_ownership.codex_tui_processes_for_session", return_value=[]), \
             mock.patch.object(self.module, "_confirmed_parent_successor", return_value=""):
            self.assertEqual(self.module.parent_session_stop_reason(args, {"att-child"}),
                             self.module.PARENT_SESSION_ENDED)
        self.assertFalse(hasattr(args, "parent_lifetime"))

    def test_unbound_lifetime_binds_the_one_session_tui_and_keeps_observing(self):
        args = self._unbound_args()
        tui = {"pid": 77, "pid_start": "9", "session_id": SESSION}
        with mock.patch.object(self.module, "process_namespace_identity", return_value="ns"), \
             mock.patch("pane_ownership.codex_tui_processes_for_session", return_value=[tui]):
            self.assertEqual(self.module.parent_session_stop_reason(args, {"att-child"}), "")
        self.assertEqual(args.parent_lifetime, {**tui, "pid_namespace": "ns"})

    def test_unbound_lifetime_stays_unknown_when_evidence_is_ambiguous_or_unreadable(self):
        tuis = [{"pid": 77, "pid_start": "9", "session_id": SESSION},
                {"pid": 78, "pid_start": "10", "session_id": SESSION}]
        for label, patches in {
            "two-tuis": dict(tui=mock.Mock(return_value=tuis), namespace="ns"),
            "lookup-error": dict(tui=mock.Mock(side_effect=OSError("proc")), namespace="ns"),
            "no-namespace": dict(tui=mock.Mock(side_effect=AssertionError("unused")),
                                 namespace=None),
        }.items():
            with self.subTest(label), \
                 mock.patch.object(self.module, "process_namespace_identity",
                                   return_value=patches["namespace"]), \
                 mock.patch("pane_ownership.codex_tui_processes_for_session", patches["tui"]):
                args = self._unbound_args()
                self.assertEqual(
                    self.module.parent_session_stop_reason(args, {"att-child"}), "")

    def test_unbound_lifetime_with_unreadable_handover_evidence_keeps_the_duty(self):
        args = self._unbound_args()
        with mock.patch.object(self.module, "process_namespace_identity", return_value="ns"), \
             mock.patch("pane_ownership.codex_tui_processes_for_session", return_value=[]), \
             mock.patch.object(self.module, "_confirmed_parent_successor",
                               side_effect=OSError("registry")):
            self.assertEqual(self.module.parent_session_stop_reason(args, {"att-child"}), "")

    def test_attempt_parent_without_session_never_uses_the_tui_judgement(self):
        args = SimpleNamespace(parent_session_id=None, jobs=Path("/jobs"))
        with mock.patch("pane_ownership.codex_tui_processes_for_session",
                        side_effect=AssertionError("attempt parents have no session")):
            self.assertEqual(self.module.parent_session_stop_reason(args, {"att-child"}), "")

    def test_confirmed_successor_asks_for_a_deliverable_recipient(self):
        args = self._unbound_args()
        with mock.patch.object(self.module.batch_obligations, "parent_successor",
                               return_value="successor") as ask:
            self.assertEqual(
                self.module._confirmed_parent_successor(args, {"att-child"}, current_session="x"),
                "successor")
        ask.assert_called_once_with(args.jobs, SESSION, {"att-child"}, current_session="x")

    def test_live_exact_parent_does_not_trigger_observer_exit(self):
        with mock.patch.object(self.module, "process_namespace_identity", return_value="ns"), \
             mock.patch.object(self.module, "_parent_proc_state", return_value=("live", "456")), \
             mock.patch("pane_ownership._native_session", return_value=SESSION):
            self.assertEqual(self.module.parent_session_stop_reason(self.args, {"att-child"}), "")

    def test_proven_parent_exit_stops_only_without_a_bound_successor(self):
        with mock.patch.object(self.module, "process_namespace_identity", return_value="ns"), \
             mock.patch.object(self.module, "_parent_proc_state", return_value=("gone", None)), \
             mock.patch("pane_ownership.codex_tui_processes_for_session", return_value=[]), \
             mock.patch.object(self.module, "_confirmed_parent_successor", return_value=""):
            self.assertEqual(self.module.parent_session_stop_reason(self.args, {"att-child"}),
                             self.module.PARENT_SESSION_ENDED)

    def test_confirmed_attempt_successor_keeps_the_obligation(self):
        with mock.patch.object(self.module, "process_namespace_identity", return_value="ns"), \
             mock.patch.object(self.module, "_parent_proc_state", return_value=("gone", None)), \
             mock.patch("pane_ownership.codex_tui_processes_for_session", return_value=[]), \
             mock.patch.object(self.module, "_confirmed_parent_successor", return_value="successor"):
            self.assertEqual(self.module.parent_session_stop_reason(self.args, {"att-child"}), "")


class ReplacementAuthorityTest(unittest.TestCase):
    def test_requires_current_epoch_generation_and_exact_parent(self):
        from argparse import Namespace
        module = load_completion_module()
        args = Namespace(jobs=Path("/tmp/jobs.log"), parent_session_id="sid", control_socket=Path("/tmp/socket"),
                         gateway_epoch=7, binding_generation=3)
        metadata = {"dispatch_depth": "1", "parent_sid": "sid", "parent_completion_delivery": module.MANAGED_SESSION_PARENT_DELIVERY}
        status = {"status": "ready", "tui_connected": True, "thread_id": "sid", "epoch": 7, "binding_generation": 3}
        with mock.patch.object(module, "gateway_request", return_value=status):
            self.assertTrue(module.replacement_authority(args, args.jobs, "att-old", metadata))
            self.assertFalse(module.replacement_authority(args, args.jobs, "att-old", dict(metadata, parent_sid="foreign")))
            status["binding_generation"] = 4
            self.assertFalse(module.replacement_authority(args, args.jobs, "att-old", metadata))
            status["binding_generation"] = 3
            status["tui_connected"] = False
            self.assertFalse(module.replacement_authority(args, args.jobs, "att-old", metadata))


class ManagedCompletionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.jobs = self.base / "jobs.log"
        self.control_path = self.base / "control.sock"
        self.join = self.base / "fake_join.py"
        self.join_calls = self.base / "join-calls.log"
        self.join.write_text(
            """\
import json, sys, pathlib
pathlib.Path(__file__).with_name('join-calls.log').open('a').write('x\\n')
call_count = len(pathlib.Path(__file__).with_name('join-calls.log').read_text().splitlines())
mode = sys.argv[1]
if '--parent-session-id' in sys.argv:
    identity_key = 'parent_session_id'
    parent = sys.argv[sys.argv.index('--parent-session-id') + 1]
else:
    identity_key = 'parent_attempt_id'
    parent = sys.argv[sys.argv.index('--parent-attempt-id') + 1]
attempts = [sys.argv[i + 1] for i, value in enumerate(sys.argv) if value == '--attempt-id']
state = 'timeout' if mode == 'timeout' or (mode == 'timeout-once' and call_count == 1) else 'ready'
children = [
    {
        'attempt_id': attempt,
        'status': 'open' if state == 'timeout' or mode == 'terminal' else 'done',
        'readiness': 'ready' if state == 'ready' else 'pending',
        'reason': (
            'terminal-observed' if mode == 'terminal'
            else 'closure-blocked:completion-attempt-not-current' if mode == 'closure-blocked'
            else 'registry-closed' if state == 'ready'
            else 'process-alive'
        ),
        'required_action': (
            'complete-open' if state == 'timeout' or mode == 'terminal'
            else 'inspect-recovery' if mode == 'closure-blocked'
            else 'advance-completed'
        ),
        'slug': 'child',
    }
    for attempt in attempts
]
print(json.dumps({
    'schema_version': 2,
    'state': state,
    identity_key: parent,
    'children': children,
}))
raise SystemExit(3 if state == 'timeout' else 0)
""",
            encoding="utf-8",
        )
        self.addCleanup(self._stop_recovery_observers)

    def test_lost_observer_record_cannot_mutate_an_unknown_duty(self):
        import dispatch_batch_obligations as batch
        module = load_completion_module()

        self.jobs.parent.mkdir(parents=True, exist_ok=True)
        self.jobs.touch(exist_ok=True)
        args = SimpleNamespace(
            jobs=self.jobs, sealed_batch_id="sealed-observer-owner",
            parent_attempt_id=PARENT, parent_session_id=None,
            attempt_id=["att-observer-owner"], control_socket=None,
            queue_socket=None, thread_id=None, interval=1.0, timeout=30.0,
        )
        duty_id, _record = batch.create(args)
        lease_path = batch._root(self.jobs) / (duty_id + ".lease")
        fd = batch.acquire(self.jobs, duty_id)
        self.assertIsNotNone(fd)
        try:
            claimed = batch.update_for_observer(
                self.jobs, duty_id, fd, claim=True, reason="observing")
            self.assertIsNotNone(claimed)
            args.obligation_lock_fd = fd
            batch.update(self.jobs, duty_id, state="unknown",
                         observer={"pid": 1, "pid_start": "1"})
            before = batch.read(self.jobs, duty_id)
            with self.assertRaises(module.ObserverOwnershipLost):
                module._update_duty(args, duty_id, state="complete")
            self.assertEqual(batch.read(self.jobs, duty_id), before)
        finally:
            batch.release(fd, lease_path)

    def test_resumed_observer_without_parent_lifetime_uses_bounded_join_checkpoints(self):
        module = load_completion_module()
        args = SimpleNamespace(parent_lifetime=None, _resume_execution=True,
                               timeout=600.0, interval=1.0)
        with mock.patch.object(module, "run_join", return_value={"state": "timeout"}) as join:
            result = module._join_checkpoint(args, {"att-child"})
        self.assertEqual(result["state"], "timeout")
        bounded = join.call_args.args[0]
        self.assertEqual(bounded.timeout, module.JOIN_CHECKPOINT_SECONDS)
        self.assertEqual(bounded.interval, 1.0)
        self.assertEqual(args.timeout, 600.0)

    def fixture_env(self):
        """Sidecar subprocess env whose parent session has one live Codex TUI.

        A duty without a recorded TUI binding is judged by the session's live
        TUI, so subprocess fixtures that expect delivery need one to exist.
        """
        shim = self.base / "tui-shim"
        shim.mkdir(exist_ok=True)
        (shim / "sitecustomize.py").write_text(
            "import os, sys\n"
            f"sys.path.insert(0, {str(ROOT / 'utilities')!r})\n"
            "import pane_ownership\n"
            "def _tui(session_id, **kwargs):\n"
            "    return [{'pid': os.getppid(), 'pid_start': '1', 'session_id': session_id}]\n"
            "pane_ownership.codex_tui_processes_for_session = _tui\n",
            encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, (str(shim), env.get("PYTHONPATH", ""))))
        for key in list(env):
            if (key.startswith(("AGENT_DISPATCH_", "HERDR_"))
                    or key in {"AGENT_SESSION_ID", "AGENT_SESSION_ROLE",
                               "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID",
                               "OPENCODE_SESSION_ID", "OPENCODE_DISPATCH_SLUG"}):
                env.pop(key, None)
        return env

    def run_sidecar(self, command, **kwargs):
        kwargs.setdefault("env", self.fixture_env())
        return subprocess.run(command, **kwargs)

    def popen_sidecar(self, command, **kwargs):
        kwargs.setdefault("env", self.fixture_env())
        return subprocess.Popen(command, **kwargs)

    def _stop_recovery_observers(self):
        root = self.jobs.parent / "supervisor-state" / "obligations"
        if not root.is_dir():
            return
        for path in root.glob("batch-*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                observer = record.get("observer") or {}
                pid = int(observer.get("pid"))
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\\0", b" ")
                if (str(SIDECAR).encode() not in cmdline
                        or record.get("id", "").encode() not in cmdline):
                    continue
                os.kill(pid, 15)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue

    def command(
        self,
        attempts: list[str],
        *,
        mode: str = "ready",
        batch: str = "batch-1",
        timeout: str = "0.1",
    ) -> list[str]:
        command = [
            sys.executable,
            str(SIDECAR),
            "--control-socket",
            str(self.control_path),
            "--jobs",
            str(self.jobs),
            "--parent-attempt-id",
            PARENT,
            "--sealed-batch-id",
            batch,
            "--interval",
            "0.01",
            "--timeout",
            timeout,
            "--join-command",
            f"{sys.executable} {self.join} {mode}",
        ]
        for attempt in attempts:
            command += ["--attempt-id", attempt]
        return command

    def session_command(
        self,
        attempts: list[str],
        *,
        retry_window: float = 0.0,
        launch_ready_timeout: float = 1.0,
    ) -> list[str]:
        command = [
            sys.executable,
            str(SIDECAR),
            "--control-socket",
            str(self.control_path),
            "--jobs",
            str(self.jobs),
            "--parent-session-id",
            SESSION,
            "--thread-id",
            SESSION,
            "--sealed-batch-id",
            "batch-session",
            "--interval",
            "0.01",
            "--timeout",
            "0.1",
            "--launch-ready-timeout",
            str(launch_ready_timeout),
            "--delivery-retry-window",
            str(retry_window),
            "--delivery-retry-interval",
            "0.02",
            "--join-command",
            f"{sys.executable} {self.join} ready",
        ]
        for attempt in attempts:
            command += ["--attempt-id", attempt]
        return command

    def test_human_gate_watcher_rechecks_exact_batch_and_uses_gateway_identity(self) -> None:
        module = load_completion_module()
        self.jobs.write_text("fixture\n", encoding="utf-8")
        record_path = self.base / "delivery.json"
        receipt = {
            "kind": "human-gate", "owner_attempt_id": "att-owner",
            "sealed_batch_id": "batch-session",
        }
        record = watcher_record(module, self.jobs.parent, "delivery-pending", receipt,
                                carrier="codex-managed-gateway", epoch="9")
        record_path = module.pending_delivery.record_path(self.jobs.parent, SESSION, record["delivery_id"])
        args = type("Args", (), {
            "jobs": self.jobs, "parent_session_id": SESSION,
            "sealed_batch_id": "batch-session", "control_socket": self.control_path,
            "interval": 0.01,
        })()
        watcher = module.HumanGateWatcher(
            args, {"att-owner"}, {"epoch": 9}
        )
        with mock.patch.object(module.human_gate_receipt, "validate_pending_record") as validate, \
             mock.patch.object(module, "negotiate_human_gate", return_value={"epoch": 9}), \
             mock.patch.object(module.human_gate_receipt, "gateway_delivery_id",
                               return_value="hg-dlv-exact"), \
             mock.patch.object(module.pending_delivery, "ack_received", wraps=module.pending_delivery.ack_received) as ack, \
             mock.patch.object(module, "gateway_request",
                               return_value={"schema_version": 1, "status": "accepted"}) as send:
            watcher._one(record_path)
        self.assertEqual(validate.call_count, 2)
        for call in validate.call_args_list:
            self.assertEqual(call.kwargs["expected_attempts"], {"att-owner"})
            self.assertEqual(call.kwargs["expected_epoch"], 9)
            self.assertEqual(call.kwargs["expected_sealed_batch_id"], "batch-session")
        self.assertEqual(send.call_args.args[1]["delivery_id"], "hg-dlv-exact")
        ack.assert_called_once_with(
            self.jobs.parent, SESSION, "delivery-pending", acked_by=watcher.owner,
            expected_identity=module.pending_delivery.received_ack_identity(record, SESSION, "delivery-pending")
        )

    def test_watcher_error_diagnostics_are_bounded(self) -> None:
        module = load_completion_module()
        args = type("Args", (), {"jobs": self.jobs, "parent_session_id": SESSION,
                                  "interval": 0.01})()
        watcher = module.HumanGateWatcher(args, {PARENT}, {"epoch": 1})
        for index in range(100):
            watcher._record_error(f"malformed-{index}")
        self.assertLessEqual(len(watcher.errors), 32)
        self.assertEqual(watcher.error_count_total, 100)
        self.assertLessEqual(len(watcher.error_counts), 64)
        self.assertEqual(sum(watcher.error_counts.values()), 100)

    def test_codex_and_claude_children_share_one_bounded_receipt(self) -> None:
        attempts = ["att-codex", "att-claude"]
        self.jobs.write_text(
            row(attempts[0], harness="codex")
            + row(attempts[1], harness="claude"),
            encoding="utf-8",
        )
        server = ControlServer(self.control_path)
        self.addCleanup(server.close)
        result = self.run_sidecar(
            self.command(attempts),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(server.called.wait(2))
        assert server.request is not None
        children = server.request["receipt"]["children"]
        self.assertEqual(server.request["receipt"]["schema_version"], 2)
        self.assertEqual(server.request["receipt"]["job_registry"], str(self.jobs))
        self.assertEqual(
            {child["harness"] for child in children},
            {"codex", "claude"},
        )
        self.assertEqual(
            {child["required_action"] for child in children},
            {"inspect-done-failure"},
        )
        self.assertEqual(
            {child["delivery_classification"] for child in children},
            {"attention"},
        )
        timing = server.request["receipt"]["delivery_timing"]
        self.assertEqual(timing["delivery_timing_schema_version"], 1)
        self.assertIsInstance(timing["last_child_terminal_ns"], int)
        self.assertIsInstance(timing["join_completed_ns"], int)
        self.assertEqual(
            set(timing),
            {
                "delivery_timing_schema_version",
                "last_child_terminal_ns",
                "join_completed_ns",
                "same_thread_resume_ns",
                "exact_harvest_ns",
                "next_stage_start_ns",
                "final_report_marker_ns",
                "owner_terminal_envelope_ns",
            },
        )
        encoded = json.dumps(server.request)
        self.assertNotIn("RAW_CHILD_SENTINEL", encoded)
        self.assertLessEqual(
            len(
                json.dumps(
                    server.request["receipt"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ),
            2048,
        )

    def test_symlinked_registry_is_rejected_before_gateway(self) -> None:
        target = self.base / "real-jobs.log"
        target.write_text(row("att-one", harness="codex"), encoding="utf-8")
        self.jobs.symlink_to(target)
        result = self.run_sidecar(
            self.command(["att-one"]),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("jobs-path-invalid", result.stdout)
        self.assertFalse(self.control_path.exists())

    def test_timeout_retains_completion_carrier_without_terminal_delivery(self) -> None:
        attempts = ["att-a", "att-b"]
        self.jobs.write_text(row(attempts[0], harness="codex", status="open")
                             + row(attempts[1], harness="claude", status="open"), encoding="utf-8")
        before = self.jobs.read_bytes()
        # `--timeout` also seeds the sidecar's own unfinishable-watch deadline
        # (item 8) now, distinct from an ordinary per-join timeout the fake
        # join script below ignores entirely (it always returns immediately)
        # -- a large value here keeps this within-process-lifetime assertion
        # meaningful instead of exercising the deadline this test is not
        # about (see test_sidecar_stops_at_own_deadline for that).
        process = self.popen_sidecar(self.command(attempts, mode="timeout", timeout="30"),
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.7)
            self.assertIsNone(process.poll())
            self.assertEqual(self.jobs.read_bytes(), before)
            self.assertFalse(self.control_path.exists())
        finally:
            process.terminate()
            out, err = process.communicate(timeout=5)
        self.assertNotIn('"status": "delivered"', out)

    def test_sidecar_stops_at_own_deadline(self) -> None:
        # plan.md item 8, defect (3): `--timeout` is now also the sidecar's
        # own unfinishable-watch deadline -- a join that never settles must
        # not keep this process running forever, unlike the ordinary
        # "keeps retaining the completion carrier" case above.
        attempts = ["att-a", "att-b"]
        self.jobs.write_text(row(attempts[0], harness="codex", status="open")
                             + row(attempts[1], harness="claude", status="open"), encoding="utf-8")
        result = self.run_sidecar(
            self.command(attempts, mode="timeout", timeout="0.05"),
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "retryable")
        self.assertEqual(payload["reason"], "watch-deadline")
        # No gateway was ever contacted -- the deadline path returns before
        # `normalize_receipt`/`deliver_with_retry` run at all.
        self.assertFalse(self.control_path.exists())

    def test_sidecar_stops_when_receiver_gone_and_rows_terminal(self) -> None:
        # plan.md item 8, defect (4): the gateway is unreachable (ENOENT --
        # no server was ever started at this control-socket path) and every
        # monitored attempt is already terminal, so the sidecar gives up on
        # the very first join timeout instead of waiting out its own
        # deadline (deliberately set far larger than this test's runtime).
        attempts = ["att-a", "att-b"]
        self.jobs.write_text(row(attempts[0], harness="codex", status="done")
                             + row(attempts[1], harness="claude", status="done"), encoding="utf-8")
        result = self.run_sidecar(
            self.command(attempts, mode="timeout", timeout="30"),
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "retryable")
        self.assertEqual(payload["reason"], "receiver-unavailable")

    def test_late_batch_completion_resumes_same_join_and_delivers_after_deadline(self) -> None:
        self.jobs.write_text(row("att-late", harness="codex", status="open"), encoding="utf-8")
        server = ControlServer(self.control_path)
        self.addCleanup(server.close)
        initial = self.run_sidecar(
            self.command(["att-late"], mode="timeout-once", timeout="0"),
            text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(initial.returncode, 75, initial.stdout + initial.stderr)
        self.assertEqual(json.loads(initial.stdout)["reason"], "watch-deadline")
        self.assertTrue(server.called.wait(5), "recovered batch was not delivered")
        self.assertEqual(server.request["op"], "deliver")
        self.assertEqual(server.request["sealed_batch_id"], "batch-1")
        self.assertEqual(self.join_calls.read_text(encoding="utf-8").count("x\n"), 2)

        import dispatch_batch_obligations as obligations
        records = list((self.base / "supervisor-state" / "obligations").glob("batch-*.json"))
        self.assertEqual(len(records), 1)
        deadline = time.monotonic() + 5
        duty = obligations.read(self.jobs, records[0].stem)
        while duty["state"] != "complete" and time.monotonic() < deadline:
            time.sleep(0.02)
            duty = obligations.read(self.jobs, records[0].stem)
        self.assertEqual(duty["state"], "complete")
        self.assertEqual(duty["delivery"], "accepted")
        self.assertEqual(duty["outcome"]["state"], "ready")
        self.assertEqual(duty["effective_attempts"], ["att-late"])

        rescanned = self.run_sidecar(
            [sys.executable, str(SIDECAR), "--ensure-obligations", "--jobs", str(self.jobs)],
            text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(rescanned.returncode, 0, rescanned.stdout + rescanned.stderr)
        self.assertEqual(json.loads(rescanned.stdout)["resumed"], 0)
        self.assertEqual(self.join_calls.read_text(encoding="utf-8").count("x\n"), 2)

    def test_sidecar_delivers_closure_blocked_receipt_once_without_relaunching_join(self) -> None:
        # item 8, unfinishable-watch: when the join itself already returns a
        # typed ready receipt for a proven-permanent block (closure-blocked),
        # the sidecar must deliver it on the very first join call and exit --
        # not wait out its own --timeout deadline (set far larger than this
        # test's runtime) or call the join a second time.
        attempts = ["att-a"]
        self.jobs.write_text(row(attempts[0], harness="codex", status="done"), encoding="utf-8")
        server = ControlServer(self.control_path)
        self.addCleanup(server.close)
        result = self.run_sidecar(
            self.command(attempts, mode="closure-blocked", timeout="30"),
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "accepted")
        self.assertTrue(server.called.wait(2))
        assert server.request is not None
        child = server.request["receipt"]["children"][0]
        self.assertEqual(child["reason"], "closure-blocked:completion-attempt-not-current")
        self.assertEqual(child["required_action"], "inspect-recovery")
        self.assertEqual(
            self.join_calls.read_text(encoding="utf-8").count("x\n"), 1,
        )

    def test_terminal_observed_open_child_keeps_actionable_status(self) -> None:
        self.jobs.write_text(
            row("att-terminal", harness="codex", status="open"),
            encoding="utf-8",
        )
        server = ControlServer(self.control_path)
        self.addCleanup(server.close)
        result = self.run_sidecar(
            self.command(["att-terminal"], mode="terminal"),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(server.called.wait(2))
        assert server.request is not None
        child = server.request["receipt"]["children"][0]
        self.assertEqual(child["status"], "open")
        self.assertEqual(child["reason"], "terminal-failure-or-unclosed")
        self.assertEqual(child["required_action"], "complete-open")

    def test_foreign_or_missing_attempt_fails_before_gateway(self) -> None:
        self.jobs.write_text(
            row("att-current", harness="codex")
            + row(
                "att-foreign",
                harness="claude",
                parent="att-foreign-parent",
            ),
            encoding="utf-8",
        )
        result = self.run_sidecar(
            self.command(["att-current", "att-foreign"]),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 65)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "rejected")
        self.assertFalse(self.control_path.exists())

    def test_duplicate_attempt_argument_fails_closed(self) -> None:
        self.jobs.write_text(
            row("att-one", harness="codex"), encoding="utf-8"
        )
        result = self.run_sidecar(
            self.command(["att-one", "att-one"]),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("attempt-set-invalid", result.stdout)

    def test_direct_managed_sibling_children_use_hashed_parent(self) -> None:
        attempts = ["att-direct-codex", "att-direct-claude", "att-direct-opencode"]
        self.jobs.write_text(
            session_row(attempts[0], harness="codex")
            + session_row(attempts[1], harness="claude")
            + session_row(attempts[2], harness="opencode"),
            encoding="utf-8",
        )
        server = ControlServer(self.control_path)
        self.addCleanup(server.close)
        result = self.run_sidecar(
            self.session_command(attempts),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(server.called.wait(2))
        assert server.request is not None
        self.assertEqual(server.request["thread_id"], SESSION)
        self.assertTrue(
            server.request["parent_attempt_id"].startswith("parent-session-")
        )
        encoded = json.dumps(server.request)
        self.assertNotIn("RAW_SESSION_CHILD_SENTINEL", encoded)
        self.assertEqual(
            {child["harness"] for child in server.request["receipt"]["children"]},
            {"codex", "claude", "opencode"},
        )

    def test_retryable_disconnect_reconnect_sends_once_after_server_appears(self) -> None:
        self.jobs.write_text(
            session_row("att-reconnect", harness="codex"),
            encoding="utf-8",
        )
        holder: dict[str, ControlServer] = {}

        def delayed_server() -> None:
            import time
            time.sleep(0.08)
            holder["server"] = ControlServer(self.control_path)

        thread = threading.Thread(target=delayed_server)
        thread.start()
        result = self.run_sidecar(
            self.session_command(["att-reconnect"], retry_window=1.0),
            text=True,
            capture_output=True,
            timeout=5,
        )
        thread.join(timeout=2)
        server = holder["server"]
        self.addCleanup(server.close)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(server.called.wait(2))
        self.assertEqual(server.request["sealed_batch_id"], "batch-session")

    def test_prelaunched_sidecar_waits_for_atomic_worker_claim(self) -> None:
        unclaimed = session_row("att-prelaunch", harness="codex").replace(
            "\tdone\t", "\topen\t"
        ).replace(
            "launch_claimed=1,launch_outcome=never-launched,",
            "launch_claimed=0,",
        )
        claimed = unclaimed.replace("launch_claimed=0", "launch_claimed=1")
        self.jobs.write_text(unclaimed, encoding="utf-8")
        server = ControlServer(self.control_path)
        self.addCleanup(server.close)

        def claim_worker() -> None:
            import time
            time.sleep(0.08)
            self.jobs.write_text(claimed, encoding="utf-8")

        thread = threading.Thread(target=claim_worker)
        thread.start()
        result = self.run_sidecar(
            self.session_command(["att-prelaunch"]),
            text=True,
            capture_output=True,
            timeout=5,
        )
        thread.join(timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(server.called.wait(2))

    def test_never_launched_registration_fails_before_gateway(self) -> None:
        never_launched = session_row(
            "att-never-launched", harness="claude"
        ).replace("launch_claimed=1", "launch_claimed=0")
        self.jobs.write_text(never_launched, encoding="utf-8")
        result = self.run_sidecar(
            self.session_command(
                ["att-never-launched"], launch_ready_timeout=0.1
            ),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("registered-child-never-launched", result.stdout)
        self.assertFalse(self.control_path.exists())


class OneSidedCachedReceiptPathTest(unittest.TestCase):
    """Normal writer/cache/consumer path for one-sided canonical receipt fields."""

    def test_terminal_writer_and_cached_outcome_reach_the_real_classifier_and_queue(self):
        import dispatch_batch_obligations as batch
        import dispatch_completion_join as join
        import dispatch_contract as contract

        module = load_completion_module()
        session_id = SESSION
        delivery_parent = "parent-session-" + hashlib.sha256(session_id.encode()).hexdigest()

        class QueueSession:
            def __init__(self, *_args, **_kwargs):
                pass

            def close(self):
                pass

        def rpc(_path, method, _params, **_kwargs):
            if method == "thread/read":
                return {"thread": {"id": session_id, "cwd": "/repo"}}
            if method == "thread/queue/list":
                return {"items": []}
            if method == "thread/turns/list":
                return {"turns": []}
            return {}

        for cache_shape in ("normal", "hand-edited-missing-required-action"):
            with self.subTest(cache_shape=cache_shape), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                jobs = base / "jobs.log"
                attempt = "att-receipt-normal" if cache_shape == "normal" else "att-receipt-edited"
                metadata = ",".join((
                    "attempt_schema_version=2", "dispatch_depth=1", "transport=headless",
                    "execution_surface=registered-headless", "registered_worker=1",
                    "fallback_hop=same-harness-headless", "worker_type=stage", "unit=qa/test",
                    "launch_claimed=1", "launch_outcome=never-launched",
                    "parent_completion_delivery=codex-native-queue", f"attempt_id={attempt}",
                    f"parent_sid={session_id}", "harness=codex", "failure_class=pass",
                ))
                jobs.write_text(f"t\topen\t/repo\t/wt\tfixture\t{metadata}\n", encoding="utf-8")

                # The production terminal writer stamps the receipt; the production
                # materializer seals its pending ledger record.
                self.assertTrue(contract.close_attempt_row(jobs, attempt, "completed-marker"))
                record_path = join.materialize_after_terminal_close(jobs, attempt)
                self.assertIsNotNone(record_path)
                writer_receipt = json.loads(record_path.read_text(encoding="utf-8"))["receipt"]
                writer_child = writer_receipt["children"][0]
                self.assertNotIn("reason", writer_child)  # documented success-writer legacy shape
                self.assertEqual((writer_receipt["job_registry"], writer_receipt["parent_attempt_id"]),
                                 ("", ""))

                raw = {"schema_version": 2, "state": "ready", "parent_session_id": session_id,
                       "children": [{"attempt_id": attempt, "status": "done", "readiness": "ready",
                                     "reason": "registry-closed",
                                     "required_action": "advance-completed"}]}
                normalized = module.normalize_receipt(
                    raw, jobs=jobs, parent_attempt_id=None, parent_session_id=session_id,
                    delivery_parent_id=delivery_parent, attempts={attempt},
                    parent_completion_delivery="codex-native-queue")
                self.assertIn("required_action", normalized["children"][0])
                self.assertIn("harness", normalized["children"][0])
                cached = json.loads(json.dumps(normalized))
                if cache_shape == "hand-edited-missing-required-action":
                    # The normalizer rejects this shape. Persisting it below
                    # models direct cache editing, not a supported writer.
                    cached["children"][0].pop("required_action")
                    self.assertIn("required_action", normalized["children"][0])

                args = SimpleNamespace(
                    jobs=jobs, attempt_id=[attempt], sealed_batch_id="batch-" + "a" * 32,
                    parent_attempt_id=None, parent_session_id=session_id, thread_id=session_id,
                    queue_socket=base / "queue.sock", control_socket=None, timeout=0.06,
                    delivery_retry_interval=0.05, parent_lifetime=None,
                )
                duty_id, _record = batch.create(args)
                args._obligation_id = duty_id
                batch.update(jobs, duty_id, state="delivery-pending", reason="result-settled",
                             outcome=cached, effective_attempts=[attempt])

                classification = join.cached_batch_delivery_state(
                    jobs, {attempt}, recipient_sid=session_id, parent_attempt_id=delivery_parent,
                    parent_completion_delivery="codex-native-queue", cached=cached)
                self.assertEqual(classification, "current")

                # Exercise _execute_once and native queue message construction;
                # only its external socket/session and enqueue boundary are stubbed.
                with mock.patch.object(module.codex_queue_delivery, "DeliverySession",
                                       side_effect=QueueSession), \
                     mock.patch("pane_ownership.codex_tui_processes_for_session",
                                return_value=[{"pid": os.getpid(), "pid_start": "1",
                                               "session_id": session_id}]), \
                     mock.patch.object(module.codex_queue_delivery, "_rpc", side_effect=rpc), \
                     mock.patch.object(module.codex_queue_delivery, "send_at_least_once",
                                       side_effect=native_add_result({"status": "queued", "queued_submission_id": "q-1",
                                                     "already_pending": False,
                                                     "started_after_interrupt": False})) as send:
                    if cache_shape == "hand-edited-missing-required-action":
                        with self.assertRaises(KeyError):
                            module._execute_once(args)
                        send.assert_not_called()
                        continue
                    result, code = module._execute_once(args)

                self.assertIn((result["status"], code),
                              (("sent-ambiguous", 74), ("retryable", 75)))
                self.assertEqual(send.call_count, 1)
                delivered = json.loads(send.call_args.kwargs["message"].split("\n", 2)[1])
                delivered_child = delivered["children"][0]
                self.assertEqual(delivered_child["reason"], "registry-closed")
                self.assertEqual(delivered_child["required_action"], "advance-completed")
                self.assertEqual(delivered_child["harness"], "codex")


class NormalizeReceiptStageAdvanceNegotiationTest(unittest.TestCase):
    """SD-110 A-18: an un-negotiated (default) call to `normalize_receipt`
    takes the literal, unmodified v2 path -- golden-byte identical to the
    pre-SD-110 receipt."""

    def setUp(self):
        self.module = load_completion_module()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name) / "jobs.tsv"
        self.jobs.write_text(row("att-child", harness="codex"), encoding="utf-8")

    def _raw_receipt(self):
        return {
            "schema_version": 2,
            "state": "ready",
            "parent_attempt_id": PARENT,
            "children": [
                {
                    "attempt_id": "att-child",
                    "status": "done",
                    "readiness": "ready",
                    "reason": "registry-closed",
                    "required_action": "advance-completed",
                }
            ],
        }

    def _normalize(self, **extra):
        return self.module.normalize_receipt(
            self._raw_receipt(),
            jobs=self.jobs,
            parent_attempt_id=PARENT,
            parent_session_id=None,
            delivery_parent_id=PARENT,
            attempts={"att-child"},
            **extra,
        )

    def test_normalize_preserves_deferred_and_rejects_ordinary_projection_errors(self):
        with mock.patch.object(self.module, "wait_for_delivery_projection", side_effect=self.module.CompletionDeferred("termination-pending")):
            with self.assertRaises(self.module.CompletionDeferred):
                self._normalize()
        with mock.patch.object(self.module, "wait_for_delivery_projection", side_effect=self.module.JoinContractError("malformed")):
            with self.assertRaisesRegex(self.module.CompletionError, "delivery-classification-failed"):
                self._normalize()

    def test_deferred_observer_retains_duty_and_resumed_window_without_delivery(self):
        from types import SimpleNamespace
        import dispatch_supervision
        for resume in (False, True):
            with self.subTest(resume=resume):
                args = SimpleNamespace(parent_attempt_id=PARENT, parent_session_id=None, thread_id=None,
                    attempt_id=["att-child"], jobs=self.jobs, control_socket=Path(self.temp.name) / "control.sock",
                    queue_socket=None, timeout=0,
                    _resume_execution=resume, _obligation_id="existing-duty")
                now = [100.0]
                def pause(seconds):
                    now[0] += seconds
                with mock.patch.object(self.module, "wait_for_session_launch_claims"), \
                     mock.patch.object(self.module, "run_join", return_value=self._raw_receipt()) as join, \
                     mock.patch("dispatch_replacement.advance_batch", return_value=({"att-child"}, [], [])), \
                     mock.patch("dispatch_replacement.adopt_receipt", return_value=({"att-child"}, set())), \
                     mock.patch.object(self.module, "normalize_receipt", side_effect=self.module.CompletionDeferred("termination-pending")), \
                     mock.patch.object(self.module, "_receiver_unavailable_reason", return_value=""), \
                     mock.patch.object(self.module.batch_obligations, "read", return_value={"outcome": None}), \
                     mock.patch.object(self.module.batch_obligations, "update") as update, \
                     mock.patch.object(dispatch_supervision, "materialize", return_value=[]), \
                     mock.patch.object(self.module.time, "monotonic", side_effect=lambda: now[0]), \
                     mock.patch.object(self.module.time, "sleep", side_effect=pause) as sleep, \
                     mock.patch.object(self.module, "deliver_to_native_queue") as deliver:
                    result, code = self.module._execute_once(args)
                self.assertEqual((code, result["reason"]), (75, "termination-pending"))
                join.assert_called_once()
                deliver.assert_not_called()
                self.assertEqual(update.call_args.kwargs["outcome"], None)
                self.assertEqual(update.call_args.kwargs["delivery"], "pending")
                if resume:
                    sleep.assert_called_once_with(30.0)
                else:
                    sleep.assert_not_called()

    def test_exact_native_ack_is_recovered_before_join(self):
        module = self.module
        recovered = {"schema_version": 2, "state": "ready", "parent_attempt_id": "parent-session-x",
                     "job_registry": str(self.jobs), "children": []}
        args = SimpleNamespace(control_socket=None, queue_socket=Path("/tmp/codex.sock"),
            jobs=self.jobs, parent_attempt_id=None, parent_session_id=SESSION, thread_id=SESSION,
            attempt_id=["att-child"], timeout=0, _obligation_id="duty-exact", _resume_execution=False)
        current = {"outcome": None, "effective_attempts": ["att-child"]}
        with mock.patch.object(module.batch_obligations, "read",
                               side_effect=[current, current, {"outcome": recovered}]), \
             mock.patch.object(module.batch_obligations, "update") as update, \
             mock.patch("dispatch_completion_join.recover_acknowledged_batch_outcome",
                        return_value=recovered) as recover, \
             mock.patch("dispatch_completion_join.cached_batch_delivery_state",
                        return_value="current"), \
             mock.patch.object(module, "wait_for_session_launch_claims", side_effect=AssertionError("must not join")), \
             mock.patch.object(module, "parent_session_stop_reason", return_value=""), \
             mock.patch.object(module, "_observer_ownership_lost", return_value=False), \
             mock.patch.object(module, "deliver_to_native_queue", return_value={"status": "accepted"}) as deliver:
            result, code = module._execute_once(args)
        self.assertEqual((code, result["status"]), (0, "accepted"))
        recover.assert_called_once()
        update.assert_called_once()
        self.assertEqual(update.call_args.kwargs["outcome"], recovered)
        deliver.assert_called_once_with(args, recovered, {"att-child"}, stop_check=mock.ANY)

    def test_cached_outcome_with_changed_exact_identity_is_not_resent(self):
        module = self.module
        cached = {"schema_version": 2, "state": "ready", "parent_attempt_id": "parent-session-x",
                  "job_registry": str(self.jobs), "children": []}
        args = SimpleNamespace(control_socket=None, queue_socket=Path("/tmp/codex.sock"),
            jobs=self.jobs, parent_attempt_id=None, parent_session_id=SESSION, thread_id=SESSION,
            attempt_id=["att-child"], timeout=0, _obligation_id="duty-stale", _resume_execution=True)
        with mock.patch.object(module.batch_obligations, "read", return_value={
                "outcome": cached, "effective_attempts": ["att-child"]}), \
             mock.patch.object(module.batch_obligations, "update") as update, \
             mock.patch("dispatch_completion_join.cached_batch_delivery_state",
                        return_value="changed") as classify, \
             mock.patch.object(module, "wait_for_session_launch_claims",
                               side_effect=AssertionError("stale outcome must not join")), \
             mock.patch.object(module, "deliver_to_native_queue") as deliver:
            result, code = module._execute_once(args)
        self.assertEqual((code, result["status"], result["reason"]),
                         (75, "retryable", "cached-outcome-identity-unavailable"))
        self.assertNotIn("outcome", update.call_args.kwargs)
        self.assertEqual(update.call_args.kwargs["reason"], "cached-outcome-identity-unavailable")
        self.assertIs(classify.call_args.kwargs["cached"], cached)
        deliver.assert_not_called()

    @staticmethod
    def _without_live_timing(value):
        # `delivery_timing` embeds `time.monotonic_ns()` observed at call
        # time, so two independent calls legitimately differ there. The
        # byte-identity claim under test is about `schema_version` and the
        # `stage_advance` key, not wall-clock timing -- mask it out.
        masked = dict(value)
        masked["delivery_timing"] = "MASKED"
        return masked

    def test_attention_cannot_borrow_failure_from_another_batch(self):
        self.jobs.write_text(self.jobs.read_text() + row("att-foreign", harness="codex"))
        receipt = self._raw_receipt()
        receipt["replacement_attention"] = [{"source_attempt_id": "att-foreign", "state": "needs-attention",
            "node": "__owner__", "reason": "replacement-input-unproven"}]
        with self.assertRaisesRegex(ValueError, "replacement-attention-scope"):
            self.module.normalize_receipt(receipt, jobs=self.jobs, parent_attempt_id=PARENT,
                parent_session_id=None, delivery_parent_id=PARENT, attempts={"att-child"})

    def test_default_call_is_byte_identical_to_pre_sd110(self):
        normalized = self._normalize()
        golden = json.dumps(self._without_live_timing(normalized), sort_keys=True)
        negotiated_but_recordless = self._normalize(accept_stage_advance=True)
        self.assertEqual(normalized["schema_version"], 2)
        self.assertNotIn("stage_advance", normalized)
        self.assertEqual(
            json.dumps(
                self._without_live_timing(negotiated_but_recordless), sort_keys=True
            ),
            golden,
        )
        # SD-119: the chain-advance path exists but a join with no
        # sub-session chain metadata is a no-op -- this receipt never carries
        # a chain key, byte-identical to pre-SD-119.
        sys.path.insert(0, str(ROOT / "utilities"))
        import dispatch_subsession_advance as subsession_advance
        from types import SimpleNamespace

        no_chain = subsession_advance.coordinate_chain_advance_from_joined_rows(
            self.jobs, PARENT, {"att-child": SimpleNamespace(
                attempt_id="att-child", status="done", metadata={},
            )},
        )
        self.assertIsNone(no_chain)
        self.assertNotIn("chain_id", json.dumps(normalized, sort_keys=True))

    def test_negotiated_advanced_record_attaches_v3_block(self):
        record = {
            "schema_version": 1,
            "stage_advance_id": "sadv-" + "0" * 64,
            "route_id": "rt-0000000000000000",
            "route_hash": "sha256:" + "0" * 64,
            "predecessor_node": "plan",
            "predecessor_terminal_attempt_id": "att-plan",
            "successor_node": "execute",
            "successor_attempt_id": "att-execute",
            "claim_key": ["sha256:" + "0" * 64, "execute", 0],
            "brief_template_digest": "sha256:" + "1" * 64,
            "outcome": "advanced",
            "reason": "",
            "registered": True,
            "started": True,
            "child_spawned": True,
        }
        normalized = self._normalize(
            accept_stage_advance=True, stage_advance_record=record
        )
        self.assertEqual(normalized["schema_version"], 3)
        self.assertEqual(normalized["stage_advance"], record)


class ObserverGenerationEndTest(unittest.TestCase):
    """How one resumed generation ends decides who observes the duty next."""

    ATTEMPT = "att-generation-end"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.jobs = self.base / "jobs.log"
        self.module = load_completion_module()
        import dispatch_batch_obligations as batch
        self.batch = batch
        self.leases = []
        real_acquire = batch.acquire

        def acquire(*args, **kwargs):
            fd = real_acquire(*args, **kwargs)
            if fd is not None:
                self.leases.append(fd)
            return fd

        patcher = mock.patch.object(batch, "acquire", side_effect=acquire)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._release_leases)

    def _release_leases(self):
        # An exiting observer process releases its lease the same way.
        while self.leases:
            try:
                os.close(self.leases.pop())
            except OSError:
                pass

    def _duty(self, outcome):
        created = self.module.parser().parse_args([
            "--queue-socket", str(self.base / "app-server-control" / "app-server-control.sock"),
            "--jobs", str(self.jobs), "--parent-session-id", SESSION, "--thread-id", SESSION,
            "--sealed-batch-id", "batch-generation-end", "--attempt-id", self.ATTEMPT,
            "--timeout", "86400"])
        duty_id, _record = self.batch.create(created)
        self.batch.update(self.jobs, duty_id, state="delivery-pending",
                          reason="result-settled", outcome=outcome)
        return duty_id

    def _resume(self, duty_id):
        args = SimpleNamespace(jobs=self.jobs, resume_obligation=duty_id,
                               obligation_lock_fd=None, service_current=None,
                               ensure_obligations=False)
        return self.module.execute(args)

    def test_resolved_ledger_completes_the_duty_in_one_observation_without_successor(self):
        import dispatch_contract as contract
        import dispatch_completion_join as join
        self.jobs.write_text(
            "t\topen\t/repo\t/wt\tended\t"
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,worker_type=stage,unit=qa/test,"
            "launch_claimed=1,launch_outcome=never-launched,"
            "parent_completion_delivery=codex-native-queue,"
            f"attempt_id={self.ATTEMPT},parent_sid={SESSION},harness=codex\n")
        self.assertTrue(contract.close_attempt_row(self.jobs, self.ATTEMPT, "dead-exact-pid"))
        record_path = join.materialize_after_terminal_close(self.jobs, self.ATTEMPT)
        stored = json.loads(record_path.read_text())
        # The notice carrier already retired this exact delivery.
        record_path.write_text(json.dumps(
            {**stored, "state": "rejected", "expiry_reason": "notice-obligation-resolved"}))
        duty_id = self._duty(stored["receipt"])

        with mock.patch.object(self.batch, "handoff_observer") as handoff, \
             mock.patch.object(self.batch, "request_runner") as runner, \
             mock.patch.object(self.batch.subprocess, "Popen",
                               side_effect=AssertionError("no successor")), \
             mock.patch.object(self.module, "deliver_to_native_queue",
                               side_effect=AssertionError("nothing to send")):
            result, code = self._resume(duty_id)

        self.assertEqual((code, result["status"], result["reason"]),
                         (0, "accepted", "delivery-resolved"))
        duty = self.batch.read(self.jobs, duty_id)
        self.assertEqual((duty["state"], duty["reason"], duty["delivery"]),
                         ("complete", "delivery-resolved", "resolved"))
        handoff.assert_not_called()
        runner.assert_not_called()

    def test_immediate_pending_end_returns_to_the_runner_instead_of_a_successor(self):
        self.jobs.write_text("fixture\n", encoding="utf-8")
        duty_id = self._duty({"schema_version": 2, "state": "ready", "children": []})
        for state, reason in (("unmaterialized", "cached-outcome-delivery-unmaterialized"),
                              ("partial", "cached-outcome-delivery-partial"),
                              ("changed", "cached-outcome-identity-unavailable")):
            with self.subTest(state=state):
                self._release_leases()
                with mock.patch("dispatch_completion_join.cached_batch_delivery_state",
                                return_value=state), \
                     mock.patch.object(self.module, "deliver_to_native_queue",
                                       side_effect=AssertionError("never resent")), \
                     mock.patch.object(self.batch, "handoff_observer") as handoff, \
                     mock.patch.object(self.batch, "request_runner") as runner:
                    result, code = self._resume(duty_id)
                self.assertEqual((code, result["reason"]), (75, reason))
                self.assertEqual(self.batch.read(self.jobs, duty_id)["state"], "delivery-pending")
                handoff.assert_not_called()
                runner.assert_called_once_with(self.jobs)

        self._release_leases()
        with mock.patch.object(self.module, "_execute_once",
                               side_effect=self.module.CompletionError("join-contract-failed")), \
             mock.patch.object(self.batch, "handoff_observer") as handoff, \
             mock.patch.object(self.batch, "request_runner") as runner:
            with self.assertRaises(self.module.CompletionError):
                self._resume(duty_id)
        handoff.assert_not_called()
        runner.assert_called_once_with(self.jobs)

    def test_only_a_spent_time_budget_hands_the_lease_to_a_successor(self):
        self.jobs.write_text("fixture\n", encoding="utf-8")
        duty_id = self._duty({"schema_version": 2, "state": "ready", "children": []})

        def watch_deadline(args):
            args._time_budget_spent = True
            return {"schema_version": 1, "status": "retryable", "reason": "watch-deadline"}, 75

        with mock.patch.object(self.module, "_execute_once", side_effect=watch_deadline), \
             mock.patch.object(self.batch, "handoff_observer", return_value=True) as handoff, \
             mock.patch.object(self.batch, "request_runner") as runner:
            self._resume(duty_id)
        handoff.assert_called_once()
        runner.assert_not_called()

    def test_native_queue_deadline_marks_the_budget_spent(self):
        args = SimpleNamespace(queue_socket=Path("/tmp/app-server.sock"), thread_id=SESSION,
                               jobs=self.base / "jobs.log", timeout=0.0,
                               delivery_retry_interval=0.05)
        result = self.module.deliver_to_native_queue(args, {}, {self.ATTEMPT})
        self.assertEqual(result["reason"], "native-queue-timeout")
        self.assertTrue(args._time_budget_spent)

        stopped = SimpleNamespace(**{**vars(args), "timeout": 30.0})
        del stopped._time_budget_spent
        result = self.module.deliver_to_native_queue(
            stopped, {}, {self.ATTEMPT}, stop_check=lambda: "parent-session-ended")
        self.assertEqual(result["reason"], "parent-session-ended")
        self.assertFalse(getattr(stopped, "_time_budget_spent", False))


class NativeQueueDeliveryTest(unittest.TestCase):
    @staticmethod
    def _ledger_record(module, delivery_id, receipt, attempt_ids, *, revision="revision",
                       state="pending", attempts=0, acked_by=None):
        digest = module.pending_delivery._canonical_receipt_digest(receipt)
        return {
            "schema_version": 1, "delivery_id": delivery_id,
            "recipient_kind": "codex-native-queue",
            "recipient_digest": module.pending_delivery.recipient_digest(SESSION),
            "session_generation": "", "session_generation_supported": "0",
            "attempt_ids": list(attempt_ids), "parent_attempt_id": "att-parent",
            "route_id": "route-fixture", "route_node": "execute",
            "receipt_digest": digest, "receipt": receipt,
            "row_revisions": {attempt: revision for attempt in attempt_ids},
            "lineage": [], "state": state, "attempts": attempts,
            "claim_authority": "deliverer-unproven", "acked_by": acked_by,
        }

    def test_each_exact_observation_pass_owns_and_closes_one_queue_session(self):
        module = load_completion_module()

        class Session:
            def __init__(self, path, *, timeout):
                self.path = path
                self.timeout = timeout
                self.close_count = 0

            def close(self):
                self.close_count += 1

        sessions = []

        def new_session(path, *, timeout):
            value = Session(path, timeout=timeout)
            sessions.append(value)
            return value

        args = SimpleNamespace(queue_socket=Path("/tmp/app-server.sock"), thread_id=SESSION,
                               jobs=Path("/tmp/jobs.log"), timeout=2.0,
                               delivery_retry_interval=0.01)
        row = SimpleNamespace(attempt_id="att-one", metadata={})
        with mock.patch.object(module, "_queue_target_from_rows",
                               side_effect=[SESSION, "successor-thread"]), \
             mock.patch.object(module, "current_session_children", return_value=[row]), \
             mock.patch.object(module.codex_queue_delivery, "DeliverySession", side_effect=new_session), \
             mock.patch.object(module, "_native_queue_delivery_once", side_effect=[
                 {"status": "sent-ambiguous", "submitted": True},
                 {"status": "accepted"},
             ]) as deliver, \
             mock.patch.object(module.time, "sleep"):
            result = module.deliver_to_native_queue(args, {"receipt": True}, {"att-one"})

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(sessions), 2)
        self.assertIsNot(sessions[0], sessions[1])
        self.assertEqual([session.close_count for session in sessions], [1, 1])
        self.assertEqual([call.kwargs["delivery_target"] for call in deliver.call_args_list],
                         [SESSION, "successor-thread"])
        self.assertEqual([call.kwargs["allow_enqueue"] for call in deliver.call_args_list],
                         [True, False])

    def test_unchanged_delivery_uses_bounded_exponential_idle_observations(self):
        module = load_completion_module()
        args = SimpleNamespace(queue_socket=Path("/tmp/app-server.sock"), thread_id=SESSION,
                               jobs=Path("/tmp/jobs.log"), timeout=60.0,
                               delivery_retry_interval=0.01)
        row = SimpleNamespace(attempt_id="att-one", metadata={})
        checks = {"count": 0}

        def stop():
            checks["count"] += 1
            return "fixture-stop" if checks["count"] >= 8 else ""

        class Session:
            def __init__(self, path, *, timeout):
                self.path = path

            def close(self):
                pass

        with mock.patch.object(module, "current_session_children", return_value=[row]), \
             mock.patch.object(module, "_queue_target_from_rows", return_value=SESSION), \
             mock.patch.object(module.codex_queue_delivery, "DeliverySession", side_effect=Session), \
             mock.patch.object(module, "_native_queue_delivery_once", return_value={
                 "status": "sent-ambiguous", "reason": "native-queue-consumption-pending",
                 "submitted": True, "client_user_message_id": "delivery-stable"}) as observe, \
             mock.patch.object(module.time, "sleep") as sleep:
            result = module.deliver_to_native_queue(args, {"receipt": True}, {"att-one"},
                                                    stop_check=stop)

        self.assertEqual(result["reason"], "fixture-stop")
        self.assertEqual(observe.call_count, 4)
        waits = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(len(waits), 3)
        for actual, expected in zip(waits, [0.05, 0.085, 0.1445]):
            self.assertAlmostEqual(actual, expected)

    def test_courier_retry_deferral_is_waited_out_not_a_new_observation(self):
        # The ledger's deferral names a different remaining time on every
        # pass. Read as a new observation, it reset this wait to 2 s forever
        # while the courier admitted a claim only every 15 s.
        module = load_completion_module()
        args = SimpleNamespace(queue_socket=Path("/tmp/app-server.sock"), thread_id=SESSION,
                               jobs=Path("/tmp/jobs.log"), timeout=600.0,
                               delivery_retry_interval=2.0)
        row = SimpleNamespace(attempt_id="att-one", metadata={})
        checks = {"count": 0}

        def stop():
            checks["count"] += 1
            return "fixture-stop" if checks["count"] >= 10 else ""

        class Session:
            def __init__(self, path, *, timeout):
                self.path = path

            def close(self):
                pass

        failed = module.codex_queue_delivery.QueueDeliveryError("queue-thread-unavailable")
        deferred = [module.pending_delivery.PendingDeliveryError(
            "pending-delivery-retry-deferred", f"remaining_ns={seconds * 1_000_000_000}")
            for seconds in (14, 13)]
        with mock.patch.object(module, "current_session_children", return_value=[row]), \
             mock.patch.object(module, "_queue_target_from_rows", return_value=SESSION), \
             mock.patch.object(module.codex_queue_delivery, "DeliverySession", side_effect=Session), \
             mock.patch.object(module, "_native_queue_delivery_once",
                               side_effect=[failed, deferred[0], failed, deferred[1], failed]), \
             mock.patch.object(module.time, "sleep") as sleep:
            result = module.deliver_to_native_queue(args, {"receipt": True}, {"att-one"},
                                                    stop_check=stop)

        self.assertEqual(result["reason"], "fixture-stop")
        waits = [call.args[0] for call in sleep.call_args_list]
        # failure, deferral (waited out), the same failure again backs off.
        self.assertEqual(len(waits), 4)
        for actual, expected in zip(waits, [2.0, 14.0, 3.4, 13.0]):
            self.assertAlmostEqual(actual, expected)

    def test_standalone_stage_close_creates_the_real_ledger_then_queue_delivery_acks_it(self):
        # Exercise the real close, materializer, session-row lookup and ledger;
        # stub only the native queue transport, never the delivery record.
        module = load_completion_module()
        import dispatch_contract as contract
        import dispatch_completion_join as join

        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.log"
            attempt = "att-standalone-first-now"
            jobs.write_text(
                "t\topen\t/repo\t/wt\tfirst-now\t"
                "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,worker_type=stage,unit=qa/test,"
                "launch_claimed=1,launch_outcome=never-launched,"
                "parent_completion_delivery=codex-native-queue,"
                f"attempt_id={attempt},parent_sid={SESSION},harness=codex\n")
            self.assertTrue(contract.close_attempt_row(jobs, attempt, "dead-exact-pid"))
            record_path = join.materialize_after_terminal_close(jobs, attempt)
            self.assertIsNotNone(record_path)
            args = SimpleNamespace(queue_socket=Path("/tmp/app-server.sock"),
                                   thread_id=SESSION, jobs=jobs, timeout=1.0,
                                   delivery_retry_interval=0.01)
            receipt = json.loads(record_path.read_text())["receipt"]
            with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value={
                    "thread": {"id": SESSION, "cwd": "/repo"}}), \
                 mock.patch.object(module.codex_queue_delivery, "send_at_least_once",
                                   return_value={"status": "consumed", "turn_id": "turn-standalone",
                                                 "turn_status": "completed"}) as send, \
                 mock.patch("peer_obligations.retain_registered_completion", return_value=True):
                result = module.deliver_to_native_queue(args, receipt, {attempt})
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(json.loads(record_path.read_text())["state"], "acked")
            send.assert_called_once()
            self.assertEqual(send.call_args.kwargs["thread_id"], SESSION)
            self.assertIn(attempt, send.call_args.kwargs["message"])

    def test_strict_history_receipt_retries_ack_without_resending(self):
        # Real close, ledger and native history projection; only the thread
        # read RPC and the receiver duty store are stubbed. Strict history used
        # to return a turn without status, so a proven consumption was demoted
        # to sent-ambiguous on every pass until the 24 h observer expiry.
        module = load_completion_module()
        import dispatch_contract as contract
        import dispatch_completion_join as join
        spec = importlib.util.spec_from_file_location(
            "codex_queue_delivery_fixture", ROOT / "utilities" / "codex_queue_delivery.test.py")
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)

        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.log"
            attempt = "att-strict-history-consumed"
            jobs.write_text(
                "t\topen\t/repo\t/wt\tconsumed\t"
                "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,worker_type=stage,unit=qa/test,"
                "launch_claimed=1,launch_outcome=never-launched,"
                "parent_completion_delivery=codex-native-queue,"
                f"attempt_id={attempt},parent_sid={SESSION},harness=codex\n")
            self.assertTrue(contract.close_attempt_row(jobs, attempt, "dead-exact-pid"))
            record_path = join.materialize_after_terminal_close(jobs, attempt)
            receipt = json.loads(record_path.read_text())["receipt"]
            args = SimpleNamespace(queue_socket=Path("/tmp/app-server.sock"), thread_id=SESSION,
                                   jobs=jobs, timeout=1.0, delivery_retry_interval=0.01)
            thread = {"thread": {"id": SESSION, "cwd": "/repo"}}
            with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value=thread), \
                 mock.patch.object(module.codex_queue_delivery, "send_at_least_once",
                                   side_effect=native_add_result({"status": "queued", "queued_submission_id": "q-1",
                                                 "already_pending": False,
                                                 "started_after_interrupt": False})) as send:
                first = module._native_queue_delivery_once(args, receipt, {attempt})
            self.assertEqual(first["status"], "sent-ambiguous")
            message = send.call_args.kwargs["message"]
            client_id = send.call_args.kwargs["client_message_id"]

            # The parent consumed that exact item in a completed native turn.
            item = {"type": "userMessage", "clientId": client_id,
                    "content": [{"type": "text", "text": message, "text_elements": []}]}
            _home, endpoint, *_rest = fixture.native_fixture(
                Path(tmp) / "native", thread_id=SESSION, projected_ordinal=10,
                entries=[{"turn_id": "turn-consumed", "item_id": "item-1", "ordinal": 1,
                          "item_type": "userMessage", "item": item}])
            args.queue_socket = endpoint
            later = time.time_ns() + 3600 * 1_000_000_000
            with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value=thread), \
                 mock.patch.object(module.pending_delivery.time, "time_ns", return_value=later), \
                 mock.patch("peer_obligations.retain_registered_completion",
                            return_value=True) as retain, \
                 mock.patch.object(module.pending_delivery, "ack_received",
                                   side_effect=OSError("simulated ACK write failure")), \
                 mock.patch.object(module.codex_queue_delivery, "send_at_least_once") as resend:
                first_receipt = module._native_queue_delivery_once(args, receipt, {attempt})
            self.assertEqual(first_receipt["status"], "accepted")
            self.assertEqual(first_receipt["reason"], "native-receipt-ack-pending")
            self.assertEqual(json.loads(record_path.read_text())["state"], "pending")
            resend.assert_not_called()
            with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value=thread), \
                 mock.patch.object(module.pending_delivery.time, "time_ns", return_value=later), \
                 mock.patch("peer_obligations.retain_registered_completion", return_value=True), \
                 mock.patch.object(module.codex_queue_delivery, "send_at_least_once") as retry_send:
                result = module._native_queue_delivery_once(args, receipt, {attempt})
                self.assertEqual(json.loads(record_path.read_text())["state"], "acked")

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["reason"], "native-receipt-settled")
        retry_send.assert_not_called()
        self.assertEqual(retain.call_args.kwargs["receiving_turn"],
                         {"thread_id": SESSION, "turn_id": "turn-consumed", "status": "completed"})

    def test_native_queue_same_transport_failure_survives_observer_recall(self):
        module = load_completion_module()
        from dispatch_pending_delivery import create, read

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs.log"
            jobs.write_text("", encoding="utf-8")
            attempt = "att-native-retry"
            delivery_id = "delivery-" + "a" * 32
            receipt = {"schema_version": 2, "state": "ready", "job_registry": str(jobs),
                       "parent_attempt_id": "att-parent", "children": []}
            record = create(
                root=root, recipient_kind="codex-native-queue", recipient_key=SESSION,
                delivery_id=delivery_id, session_generation="",
                session_generation_supported="0", attempt_ids=[attempt],
                parent_attempt_id="att-parent", route_id="rt-native-retry",
                route_node="execute", receipt=receipt,
                receipt_digest=module.pending_delivery._canonical_receipt_digest(receipt),
                row_revisions={attempt: "revision-one"},
            )
            row = SimpleNamespace(
                attempt_id=attempt, raw="terminal-row",
                metadata={"delivery_id": delivery_id,
                          "delivery_recipient_kind": "codex-native-queue",
                          "delivery_row_revision": "revision-one",
                          "delivery_receipt_digest": record["receipt_digest"]},
            )
            args = SimpleNamespace(
                queue_socket=root / "app-server.sock", thread_id=SESSION,
                sealed_batch_id="batch-native-retry", jobs=jobs,
                delivery_retry_interval=0.05,
            )

            class Session:
                def __init__(self, path, *, timeout):
                    self.path = path

                def close(self):
                    pass

            fixed_now = time.time_ns()
            with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value={
                     "thread": {"id": SESSION, "cwd": str(root)}}), \
                 mock.patch.object(module, "current_session_children", return_value=[row]), \
                 mock.patch.object(module, "_queue_target_from_rows", return_value=SESSION), \
                 mock.patch.object(module.codex_queue_delivery, "DeliverySession", side_effect=Session), \
                 mock.patch("peer_obligations.retain_registered_completion",
                            return_value=True) as retain, \
                 mock.patch.object(module.codex_queue_delivery, "send_at_least_once", side_effect=[
                     module.codex_queue_delivery.QueueDeliveryError("transport-timeout"),
                     {"status": "consumed", "turn_id": "turn-after-retry",
                      "turn_status": "completed"},
                 ]) as send:
                with mock.patch.object(module.pending_delivery.time, "time_ns", return_value=fixed_now):
                    with self.assertRaisesRegex(
                            module.codex_queue_delivery.QueueDeliveryError, "transport-timeout"):
                        module._native_queue_delivery_once(args, receipt, {attempt})
                pending = read(root, SESSION, delivery_id)
                self.assertEqual(pending["state"], "pending")
                self.assertNotIn("courier_retry", pending)
                retry_path = module.pending_delivery.courier_retry_path(
                    module.pending_delivery.record_path(root, SESSION, delivery_id)
                )
                retry = json.loads(retry_path.read_text(encoding="utf-8"))["retry"]
                self.assertEqual(retry["failure_count"], 1)

                with mock.patch.object(module.pending_delivery.time, "time_ns",
                                       return_value=fixed_now + 1_000_000):
                    with self.assertRaises(module.pending_delivery.PendingDeliveryError) as caught:
                        module._native_queue_delivery_once(args, receipt, {attempt})
                self.assertEqual(caught.exception.reason, "pending-delivery-retry-deferred")
                self.assertEqual(send.call_count, 1)
                self.assertEqual(read(root, SESSION, delivery_id)["attempts"], 1)

                with mock.patch.object(module.pending_delivery.time, "time_ns",
                                       return_value=retry["next_attempt_at_ns"] + 1):
                    result = module._native_queue_delivery_once(args, receipt, {attempt})
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(send.call_count, 2)
            retain.assert_called_once()
            self.assertEqual(send.call_args_list[0].kwargs["client_message_id"],
                             send.call_args_list[1].kwargs["client_message_id"])
            delivered = read(root, SESSION, delivery_id)
            self.assertEqual(delivered["state"], "acked")
            self.assertNotIn("courier_retry", delivered)

    # The ledger is mocked here; the registry that keep_claim reads does not exist.
    @mock.patch("dispatch_notice_state.keep_claim", new=lambda *_a, **_k: True)
    def test_queue_acceptance_uses_exact_id_and_restarts_only_while_pending(self) -> None:
        from types import SimpleNamespace

        module = load_completion_module()
        receipt = {"schema_version": 2, "state": "ready", "children": []}
        delivery_id = "delivery-ledger-one"
        delivery_material = "\0".join((
            "codex-native-queue-v1", SESSION, delivery_id,
        ))
        client_id = "delivery-" + hashlib.sha256(
            delivery_material.encode("utf-8")
        ).hexdigest()[:32]
        receipt_digest = module.pending_delivery._canonical_receipt_digest(receipt)
        item = {"id": "queue-item-1", "clientUserMessageId": client_id}
        row = SimpleNamespace(
            attempt_id="att-one", raw="terminal-row",
            metadata={"parent_sid": SESSION, "parent_harness": "codex",
                      "delivery_id": delivery_id,
                      "delivery_recipient_kind": "codex-native-queue",
                      "delivery_row_revision": "revision",
                      "delivery_receipt_digest": receipt_digest},
        )
        ledger_record = self._ledger_record(
            module, delivery_id, receipt, ["att-one"], revision="revision")
        args = SimpleNamespace(
            queue_socket=Path("/tmp/app-server.sock"),
            thread_id=SESSION,
            sealed_batch_id="batch-exact",
            jobs=Path("/tmp/jobs.log"),
            timeout=3.0,
            interval=0.01,
            delivery_retry_interval=0.01,
        )
        with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value={
                "thread": {"id": SESSION, "cwd": "/tmp/repo"}}), \
             mock.patch.object(module, "current_session_children", return_value=[row]), \
             mock.patch.object(module.pending_delivery, "read", return_value=ledger_record), \
             mock.patch.object(module.pending_delivery, "claim") as claim, \
             mock.patch.object(module.pending_delivery, "release_claim") as release, \
             mock.patch.object(module.pending_delivery, "mark_sent_ambiguous") as ambiguous, \
             mock.patch.object(module.pending_delivery, "ack_received",
                               return_value={"state": "acked"}) as ack_received, \
             mock.patch.object(module.pending_delivery, "record_send"), \
             mock.patch.object(module.codex_queue_delivery, "send_at_least_once", side_effect=[{
                 "status": "queued", "queued_submission_id": item["id"]},
                 {"status": "consumed", "turn_id": "turn-1", "turn_status": "completed"}]) as send, \
             mock.patch.object(module.codex_queue_delivery, "list_queue", side_effect=[[item], []]), \
             mock.patch.object(module.codex_queue_delivery,
                               "find_turn_by_client_message_id", return_value={"id": "turn-1",
                                                                                "status": "completed"}), \
             mock.patch.object(module.codex_queue_delivery, "_known_consumed",
                               return_value={"id": "turn-1", "status": "completed"}), \
             mock.patch("peer_obligations.retain_registered_completion", return_value=True), \
             mock.patch.object(module.codex_queue_delivery, "_latest_turn_status", return_value="interrupted"), \
             mock.patch.object(module.codex_queue_delivery, "poll_owned_item") as restart, \
             mock.patch.object(module.time, "sleep"):
            result = module.deliver_to_native_queue(
                args, receipt, {"att-one"}
            )
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(send.call_args.kwargs["client_message_id"].startswith("delivery-"))
        self.assertIn("AGENT_HARNESS_COMPLETION_V1", send.call_args.kwargs["message"])
        self.assertEqual(claim.call_count, 2)
        self.assertEqual(release.call_count, 2)
        ambiguous.assert_called_once()
        ack_received.assert_called_once()
        self.assertEqual(send.call_count, 1)
        restart.assert_not_called()  # Transport owns exact restart decisions.


    # The ledger is mocked here; the registry that keep_claim reads does not exist.
    @mock.patch("dispatch_notice_state.keep_claim", new=lambda *_a, **_k: True)
    def test_a_cleared_windows_successor_receives_the_item_while_claim_and_ack_stay_with_the_registered_parent(self) -> None:
        from types import SimpleNamespace

        module = load_completion_module()
        receipt = {"schema_version": 2, "state": "ready", "children": []}
        delivery_id = "delivery-ledger-one"
        client_id = "delivery-" + hashlib.sha256(
            "\0".join(("codex-native-queue-v1", SESSION, delivery_id)).encode("utf-8")).hexdigest()[:32]
        receipt_digest = module.pending_delivery._canonical_receipt_digest(receipt)
        row = SimpleNamespace(
            attempt_id="att-one", raw="terminal-row",
            metadata={"delivery_id": delivery_id, "delivery_recipient_kind": "codex-native-queue",
                      "delivery_row_revision": "revision", "delivery_receipt_digest": receipt_digest})
        ledger_record = self._ledger_record(
            module, delivery_id, receipt, ["att-one"], revision="revision")
        args = SimpleNamespace(queue_socket=Path("/tmp/app-server.sock"), thread_id=SESSION,
                               sealed_batch_id="batch-exact", jobs=Path("/tmp/jobs.log"), timeout=3.0,
                               interval=0.01, delivery_retry_interval=0.01)
        with mock.patch.object(module, "_queue_target_from_rows", return_value="successor-thread"), \
             mock.patch.object(module.codex_queue_delivery, "_rpc", return_value={
                 "thread": {"id": "successor-thread", "cwd": "/tmp/repo"}}) as rpc, \
             mock.patch.object(module, "current_session_children", return_value=[row]), \
             mock.patch.object(module.pending_delivery, "read", return_value=ledger_record) as read, \
             mock.patch.object(module.pending_delivery, "claim") as claim, \
             mock.patch.object(module.pending_delivery, "release_claim"), \
             mock.patch.object(module.pending_delivery, "mark_sent_ambiguous"), \
             mock.patch.object(module.pending_delivery, "ack_received",
                               return_value={"state": "acked"}) as ack_received, \
             mock.patch.object(module.pending_delivery, "record_send"), \
             mock.patch.object(module.codex_queue_delivery, "_known_consumed", return_value=None) as consumed, \
             mock.patch.object(module.codex_queue_delivery, "send_at_least_once",
                               return_value={"status": "consumed", "turn_id": "turn-1",
                                             "turn_status": "completed"}) as send, \
             mock.patch("peer_obligations.retain_registered_completion", return_value=True):
            result = module.deliver_to_native_queue(args, receipt, {"att-one"})
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(rpc.call_args.args[2], {"threadId": "successor-thread"})
        self.assertEqual(send.call_args.kwargs["thread_id"], "successor-thread")
        self.assertEqual(send.call_args.kwargs["client_message_id"], client_id)       # the same stable id
        consumed.assert_called_once()
        self.assertEqual(consumed.call_args.args[:3],
                         (Path("/tmp/app-server.sock"), SESSION, client_id))
        self.assertEqual(consumed.call_args.kwargs["timeout"], 5.0)
        self.assertIsNotNone(consumed.call_args.kwargs["session"])
        self.assertEqual({call.args[1] for call in read.call_args_list}, {SESSION})
        self.assertEqual(claim.call_args.args[1], SESSION)
        self.assertEqual(ack_received.call_args.args[1], SESSION)

    def test_queue_target_uses_shared_observation_without_restoring_old_thread(self):
        import dispatch_seat_handover as handover
        module = load_completion_module()
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.log"
            self.assertEqual(module.queue_target(jobs, {"att-one"}, SESSION), SESSION)
            jobs.write_text("t\topen\t/w\t/w\tslug\tattempt_id=att-one,parent_sid=" + SESSION + "\n")
            self.assertEqual(module.queue_target(jobs, {"att-one"}, SESSION), SESSION)
            for sid, harness, state, expected in (("successor", "codex", "confirmed", "successor"),
                    ("claude-successor", "claude", "confirmed", ""), ("", "", "unreadable", "")):
                result = handover.RecipientResolution(sid, harness, state, SESSION, "codex")
                with mock.patch.object(handover, "resolve_recipient", return_value=result):
                    self.assertEqual(module.queue_target(jobs, {"att-one"}, SESSION), expected)

    def test_notice_watcher_reads_and_acks_the_original_key_through_a_real_tidy_lineage(self) -> None:
        import dispatch_seat_handover as handover
        import session_notice

        module = load_completion_module()
        old_session = "thread-managed-before-clear"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xdg = root / "xdg"
            ledger_dir = xdg / "hearting" / "session-tidy" / "sessions"
            ledger_dir.mkdir(parents=True)
            (ledger_dir / "fixture-seat.jsonl").write_text(json.dumps({
                "event": "succession", "source": "tidy-clear", "harness": "codex",
                "from": old_session, "sid": SESSION, "ts": 1,
            }) + "\n", encoding="utf-8")
            jobs = root / "dispatch" / "jobs.log"
            jobs.parent.mkdir()
            jobs.write_text("", encoding="utf-8")
            receipt = {
                "schema_version": 1, "kind": "notice", "subject": "remote run",
                "text": "completed", "required_action": "read-notice", "key": "fixture-run",
            }
            delivery_id = "delivery-" + hashlib.sha256(b"notice\0fixture-run").hexdigest()
            notice_attempt = "notice-" + hashlib.sha256(b"notice\0fixture-run").hexdigest()[:32]
            pending = module.pending_delivery
            pending.create(
                jobs.parent, recipient_kind="codex-native-queue", recipient_key=old_session,
                delivery_id=delivery_id, session_generation="", session_generation_supported="0",
                attempt_ids=[notice_attempt], parent_attempt_id=notice_attempt,
                route_id="route-free", route_node="notice", receipt=receipt,
                receipt_digest=session_notice.receipt_digest(receipt),
                row_revisions={notice_attempt: "notice"},
            )
            args = SimpleNamespace(
                jobs=jobs, parent_session_id=SESSION, sealed_batch_id="batch-current",
                queue_socket=Path("/tmp/app-server.sock"), interval=0.01,
            )
            seat = SimpleNamespace(key="fixture-seat")
            def consume(*_args, **kwargs):
                kwargs["before_add"]()
                return {"status": "consumed", "turn_id": "turn-fixture"}

            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(xdg)}), \
                 mock.patch.object(handover, "pane_seat", return_value=seat), \
                 mock.patch.object(handover, "_lineage_seats", return_value={}), \
                 mock.patch.object(module.codex_queue_delivery, "send_at_least_once",
                                   side_effect=consume) as send:
                handover._LINEAGE_GRAPH_CACHE.clear()
                watcher = module.NoticeWatcher(args, {"unrelated-managed-attempt"}, capability=None)
                candidates = watcher._candidate_paths()
                self.assertEqual(candidates, [(pending.record_path(jobs.parent, old_session, delivery_id),
                                               old_session)])
                watcher._one(*candidates[0][:1], storage_key=candidates[0][1])
                handover._LINEAGE_GRAPH_CACHE.clear()

            self.assertEqual(send.call_args.kwargs["thread_id"], SESSION)
            from dispatch_recipient_receipt import receipt_marker
            self.assertEqual(send.call_args.kwargs["message"],
                "remote run: completed (required_action=read-notice)\n" + receipt_marker(
                    pending.read(jobs.parent, old_session, delivery_id), old_session))
            stored = pending.read(jobs.parent, old_session, delivery_id)
            self.assertEqual(stored["state"], "acked")
            self.assertIsNone(pending.read(jobs.parent, SESSION, delivery_id))

    # The ledger is mocked here; the registry that keep_claim reads does not exist.
    @mock.patch("dispatch_notice_state.keep_claim", new=lambda *_a, **_k: True)
    def test_accepted_send_is_not_repeated_during_history_outage(self) -> None:
        from types import SimpleNamespace

        module = load_completion_module()
        receipt = {"schema_version": 2, "state": "ready", "children": []}
        delivery_id = "delivery-ledger-one"
        delivery_material = "\0".join((
            "codex-native-queue-v1", SESSION, delivery_id,
        ))
        client_id = "delivery-" + hashlib.sha256(
            delivery_material.encode("utf-8")
        ).hexdigest()[:32]
        receipt_digest = module.pending_delivery._canonical_receipt_digest(receipt)
        item = {"id": "queue-item-1", "clientUserMessageId": client_id}
        row = SimpleNamespace(
            attempt_id="att-one", raw="terminal-row",
            metadata={"parent_sid": SESSION, "parent_harness": "codex",
                      "delivery_id": delivery_id,
                      "delivery_recipient_kind": "codex-native-queue",
                      "delivery_row_revision": "revision",
                      "delivery_receipt_digest": receipt_digest},
        )
        ledger_record = self._ledger_record(
            module, delivery_id, receipt, ["att-one"], revision="revision")
        args = SimpleNamespace(
            queue_socket=Path("/tmp/app-server.sock"),
            thread_id=SESSION,
            sealed_batch_id="batch-exact",
            jobs=Path("/tmp/jobs.log"),
            timeout=3.0,
            interval=0.01,
            delivery_retry_interval=0.01,
        )
        with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value={
                "thread": {"id": SESSION, "cwd": "/tmp/repo"}}), \
             mock.patch.object(module, "current_session_children", return_value=[row]), \
             mock.patch.object(module.pending_delivery, "read", return_value=ledger_record), \
             mock.patch.object(module.pending_delivery, "claim") as claim, \
             mock.patch.object(module.pending_delivery, "release_claim") as release, \
             mock.patch.object(module.pending_delivery, "mark_sent_ambiguous") as ambiguous, \
             mock.patch.object(module.pending_delivery, "ack_received",
                               return_value={"state": "acked"}) as ack_received, \
             mock.patch.object(module.pending_delivery, "record_send"), \
             mock.patch.object(module.codex_queue_delivery, "send_at_least_once", side_effect=[{
                 "status": "queued", "queued_submission_id": item["id"]},
                 {"status": "consumed", "turn_id": "turn-1", "turn_status": "completed"}]) as send, \
             mock.patch.object(module.codex_queue_delivery, "list_queue", side_effect=[[item], []]), \
             mock.patch.object(module.codex_queue_delivery,
                               "find_turn_by_client_message_id", side_effect=[
                                   module.codex_queue_delivery.QueueDeliveryError("history-unavailable"),
                                   module.codex_queue_delivery.QueueDeliveryError("history-unavailable"),
                                   {"id": "turn-1", "status": "completed"}]), \
             mock.patch.object(module.codex_queue_delivery, "_known_consumed",
                               side_effect=[None, None, {"id": "turn-1", "status": "completed"}]), \
             mock.patch("peer_obligations.retain_registered_completion", return_value=True), \
             mock.patch.object(module.codex_queue_delivery, "_latest_turn_status", return_value="interrupted"), \
             mock.patch.object(module.codex_queue_delivery, "poll_owned_item", return_value={"pending": False, "started": False}) as restart, \
             mock.patch.object(module.time, "sleep"):
            result = module.deliver_to_native_queue(
                args, receipt, {"att-one"}
            )
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(send.call_args.kwargs["client_message_id"].startswith("delivery-"))
        self.assertIn("AGENT_HARNESS_COMPLETION_V1", send.call_args.kwargs["message"])
        self.assertEqual(claim.call_count, 4)
        self.assertEqual(release.call_count, 4)
        self.assertEqual(ambiguous.call_count, 3)
        ack_received.assert_called_once()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(restart.call_count, 2)  # Consumed item absent, history temporarily unavailable.


    # The ledger is mocked here; the registry that keep_claim reads does not exist.
    @mock.patch("dispatch_notice_state.keep_claim", new=lambda *_a, **_k: True)
    def test_refused_transport_releases_its_exact_claim_before_returning(self) -> None:
        from types import SimpleNamespace

        module = load_completion_module()
        receipt = {"schema_version": 2, "state": "ready", "children": []}
        delivery_id = "delivery-ledger-one"
        delivery_material = "\0".join((
            "codex-native-queue-v1", SESSION, delivery_id,
        ))
        client_id = "delivery-" + hashlib.sha256(
            delivery_material.encode("utf-8")
        ).hexdigest()[:32]
        receipt_digest = module.pending_delivery._canonical_receipt_digest(receipt)
        item = {"id": "queue-item-1", "clientUserMessageId": client_id}
        row = SimpleNamespace(
            attempt_id="att-one", raw="terminal-row",
            metadata={"parent_sid": SESSION, "parent_harness": "codex",
                      "delivery_id": delivery_id,
                      "delivery_recipient_kind": "codex-native-queue",
                      "delivery_row_revision": "revision",
                      "delivery_receipt_digest": receipt_digest},
        )
        ledger_record = self._ledger_record(
            module, delivery_id, receipt, ["att-one"], revision="revision")
        args = SimpleNamespace(
            queue_socket=Path("/tmp/app-server.sock"),
            thread_id=SESSION,
            sealed_batch_id="batch-exact",
            jobs=Path("/tmp/jobs.log"),
            timeout=3.0,
            interval=0.01,
            delivery_retry_interval=0.01,
        )
        def claim_record(*_args, **kwargs):
            ledger_record.update(state="claimed", claim_owner=kwargs["claim_owner"],
                                 claim_deadline_ns=time.monotonic_ns() + 60_000_000_000)
            return dict(ledger_record)

        def refuse(*_args, **kwargs):
            kwargs["before_add"]()
            raise module.codex_queue_delivery.QueueDeliveryError("queue-start-refused")

        with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value={
                "thread": {"id": SESSION, "cwd": "/tmp/repo"}}), \
             mock.patch.object(module, "current_session_children", return_value=[row]), \
             mock.patch.object(module.pending_delivery, "read", return_value=ledger_record), \
             mock.patch.object(module.pending_delivery, "claim", side_effect=claim_record) as claim, \
             mock.patch.object(module.pending_delivery, "read_submission_fence", return_value=None), \
             mock.patch.object(module.pending_delivery, "release_claim") as release, \
             mock.patch.object(module.pending_delivery, "mark_sent_ambiguous") as ambiguous, \
             mock.patch.object(module.pending_delivery, "ack") as ack, \
             mock.patch.object(module.codex_queue_delivery, "send_at_least_once",
                 side_effect=refuse):
            with self.assertRaisesRegex(module.codex_queue_delivery.QueueDeliveryError, "queue-start-refused"):
                module._native_queue_delivery_once(args, receipt, {"att-one"})
        release.assert_called_once()
        self.assertEqual(release.call_args.kwargs["claim_owner"], claim.call_args.kwargs["claim_owner"])
        ack.assert_not_called()

    def test_partial_ledger_ack_closes_the_same_batch_without_resending(self) -> None:
        from types import SimpleNamespace

        module = load_completion_module()
        receipt = {"schema_version": 2, "state": "ready", "children": []}
        ids = ["delivery-ledger-a", "delivery-ledger-b"]
        material = "\0".join(("codex-native-queue-v1", SESSION, *ids))
        client_id = "delivery-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
        rows = [SimpleNamespace(
            attempt_id=f"att-{index}", raw=f"row-{index}",
            metadata={"parent_sid": SESSION, "parent_harness": "codex",
                      "delivery_id": delivery_id,
                      "delivery_recipient_kind": "codex-native-queue",
                      "delivery_row_revision": "revision",
                      "delivery_receipt_digest": f"digest-{index}"},
        ) for index, delivery_id in enumerate(ids)]
        records = [{
            "delivery_id": delivery_id, "recipient_kind": "codex-native-queue",
            "attempt_ids": [f"att-{index}"], "receipt_digest": f"digest-{index}",
            "row_revisions": {f"att-{index}": "revision"},
            "state": "acked" if index == 0 else "sent-ambiguous",
            "acked_by": f"codex-native-queue:{client_id}" if index == 0 else None,
        } for index, delivery_id in enumerate(ids)]
        def read_record(_root, _thread, delivery_id):
            return dict(records[ids.index(delivery_id)])
        def ack_record(_root, _thread, delivery_id, **_kwargs):
            records[ids.index(delivery_id)].update(state="acked",
                                                   acked_by=f"codex-native-queue:{client_id}")
        args = SimpleNamespace(
            queue_socket=Path("/tmp/app-server.sock"), thread_id=SESSION,
            sealed_batch_id="batch-exact", jobs=Path("/tmp/jobs.log"),
            timeout=2.0, interval=0.01, delivery_retry_interval=0.01,
        )
        with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value={
                 "thread": {"id": SESSION, "cwd": "/tmp/repo"}}), \
             mock.patch.object(module, "current_session_children", return_value=rows), \
             mock.patch.object(module.pending_delivery, "read", side_effect=read_record), \
             mock.patch.object(module.pending_delivery, "ack", side_effect=ack_record) as ack, \
             mock.patch.object(module.codex_queue_delivery, "send_at_least_once") as send:
            result = module.deliver_to_native_queue(args, receipt, {"att-0", "att-1"})
        self.assertEqual(result["status"], "accepted")
        ack.assert_called_once_with(
            args.jobs.resolve(strict=False).parent, SESSION, ids[1],
            acked_by=f"codex-native-queue:{client_id}",
            expected_states=("pending", "claimed", "sent-ambiguous"),
        )
        send.assert_not_called()

    def test_native_queue_notice_stays_unacked_while_exact_item_is_pending(self) -> None:
        module = load_completion_module()
        pending = module.pending_delivery
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs.log"
            jobs.touch()
            receipt = {"schema_version": 1, "kind": "supervision",
                       "owner_attempt_id": "att-owner", "sealed_batch_id": "batch-exact"}
            delivery_id = "delivery-" + "a" * 32
            watcher_record(module, root, delivery_id, receipt)
            args = SimpleNamespace(queue_socket=root / "private.sock", jobs=jobs,
                parent_session_id=SESSION, sealed_batch_id="batch-exact", interval=0.1)
            watcher = module.NoticeWatcher(args, {"att-owner"}, capability=None)
            item = {"id": "queue-item", "clientUserMessageId": delivery_id}
            original = pending.claim(root, SESSION, delivery_id, claim_owner="old-notice",
                lease_seconds=30, require_generation_proof=True,
                live_recipient_generation=(SESSION, "1"), jobs=jobs)
            identity = pending.submission_fence_identity(original, SESSION)
            pending.prepare_submission_fence(root, SESSION, delivery_id,
                claim_owner="old-notice", expected_identity=identity)
            pending.record_submission_accepted(root, SESSION, delivery_id,
                claim_owner="old-notice", expected_identity=identity)
            raw_path = pending.record_path(root, SESSION, delivery_id)
            raw = json.loads(raw_path.read_text())
            raw["claim_deadline_ns"] = time.monotonic_ns() - 1
            raw_path.write_text(json.dumps(raw))
            # Exact already-pending observation invokes neither callback nor add.
            with mock.patch.object(module.notice_receipt, "validate_pending_record"), \
                 mock.patch.object(module.notice_receipt, "context", return_value={
                     "additionalContext": {"notice": {"value": "typed notice"}}}), \
                 mock.patch.object(module.notice_receipt, "gateway_delivery_id", return_value="notice-id"), \
                 mock.patch.object(module, "queue_target", return_value=SESSION), \
                 mock.patch.object(pending, "prepare_submission_fence", wraps=pending.prepare_submission_fence) as prepare, \
                 mock.patch.object(pending, "ack_received", wraps=pending.ack_received) as ack, \
                 mock.patch.object(module.codex_queue_delivery, "send_at_least_once",
                     side_effect=native_add_result({"status": "queued", "already_pending": True})) as send, \
                 mock.patch.object(module.codex_queue_delivery, "_known_consumed", return_value=None), \
                 mock.patch.object(module.codex_queue_delivery, "list_queue", return_value=[item]):
                watcher._one(pending.record_path(root, SESSION, delivery_id))
            send.assert_called_once()
            self.assertEqual(send.call_args.args[:1], (args.queue_socket,))
            self.assertEqual(send.call_args.kwargs["thread_id"], SESSION)
            self.assertEqual(send.call_args.kwargs["client_message_id"], delivery_id)
            from dispatch_recipient_receipt import receipt_marker
            stored = pending.read(root, SESSION, delivery_id)
            self.assertEqual(send.call_args.kwargs["message"], "typed notice\n" + receipt_marker(stored, SESSION))
            self.assertFalse(send.call_args.kwargs["allow_add"])
            self.assertFalse(send.call_args.kwargs["retry_ambiguous"])
            self.assertTrue(send.call_args.kwargs["require_message_match"])
            prepare.assert_not_called()
            ack.assert_not_called()
            self.assertEqual(stored["state"], "sent-ambiguous")
            fence = pending.read_submission_fence(root, SESSION, delivery_id)
            self.assertEqual(fence["state"], "accepted")
            self.assertEqual(fence["attempt"], 1)

    def test_native_queue_notice_ack_requires_exact_consumed_message(self) -> None:
        module = load_completion_module()
        pending = module.pending_delivery
        for consumed in (None, {"id": "turn-exact", "status": "completed"}):
            with self.subTest(consumed=consumed is not None), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                jobs = root / "jobs.log"
                jobs.touch()
                delivery_id = "delivery-" + ("a" if consumed is None else "b") * 32
                receipt = {"schema_version": 1, "kind": "supervision",
                    "owner_attempt_id": "att-owner", "sealed_batch_id": "batch-exact"}
                record = watcher_record(module, root, delivery_id, receipt)
                args = SimpleNamespace(queue_socket=root / "private.sock", jobs=jobs,
                    parent_session_id=SESSION, sealed_batch_id="batch-exact", interval=0.1)
                with mock.patch.object(module.notice_receipt, "validate_pending_record"), \
                     mock.patch.object(module.notice_receipt, "context", return_value={
                         "additionalContext": {"notice": {"value": "exact notice body"}}}), \
                     mock.patch.object(module.notice_receipt, "gateway_delivery_id", return_value="notice-id"), \
                     mock.patch.object(module, "queue_target", return_value=SESSION), \
                     mock.patch.object(pending, "ack_received", wraps=pending.ack_received) as ack, \
                     mock.patch.object(module.codex_queue_delivery, "send_at_least_once",
                         side_effect=native_add_result({"status": "queued", "already_pending": True})), \
                     mock.patch.object(module.codex_queue_delivery, "list_queue", return_value=[]), \
                     mock.patch.object(module.codex_queue_delivery, "_known_consumed", return_value=consumed) as observed:
                    module.NoticeWatcher(args, {"att-owner"}, capability=None)._one(
                        pending.record_path(root, SESSION, delivery_id))
                from dispatch_recipient_receipt import receipt_marker
                observed.assert_called_once_with(args.queue_socket, SESSION, delivery_id,
                    timeout=5.0, strict_history=True,
                    expected_message="exact notice body\n" + receipt_marker(record, SESSION))
                if consumed is None:
                    ack.assert_not_called()
                    self.assertEqual(pending.read(root, SESSION, delivery_id)["state"], "sent-ambiguous")
                else:
                    ack.assert_called_once()
                    self.assertEqual(pending.read(root, SESSION, delivery_id)["state"], "acked")

    def test_notice_fence_write_failure_stops_before_queue_add(self) -> None:
        module = load_completion_module()
        pending = module.pending_delivery
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs.log"
            jobs.touch()
            delivery_id = "delivery-" + "c" * 32
            receipt = {"schema_version": 1, "kind": "supervision",
                "owner_attempt_id": "att-owner", "sealed_batch_id": "batch-exact"}
            watcher_record(module, root, delivery_id, receipt)
            args = SimpleNamespace(queue_socket=root / "private.sock", jobs=jobs,
                parent_session_id=SESSION, sealed_batch_id="batch-exact", interval=0.1)
            queue_adds = []
            def queue_send(*_args, **kwargs):
                kwargs["before_add"]()
                queue_adds.append(kwargs["client_message_id"])
                return {"status": "queued", "already_pending": False}
            with mock.patch.object(module.notice_receipt, "validate_pending_record"), \
                 mock.patch.object(module.notice_receipt, "context", return_value={
                     "additionalContext": {"notice": {"value": "typed notice"}}}), \
                 mock.patch.object(module.notice_receipt, "gateway_delivery_id", return_value="notice-id"), \
                 mock.patch.object(module, "queue_target", return_value=SESSION), \
                 mock.patch.object(pending, "prepare_submission_fence",
                     side_effect=pending.PendingDeliveryError("delivery-persistence-refused", "forced")), \
                 mock.patch.object(pending, "mark_sent_ambiguous", wraps=pending.mark_sent_ambiguous) as ambiguous, \
                 mock.patch.object(pending, "ack_received", wraps=pending.ack_received) as ack, \
                 mock.patch.object(module.codex_queue_delivery, "send_at_least_once", side_effect=queue_send) as send:
                module.NoticeWatcher(args, {"att-owner"}, capability=None)._one(
                    pending.record_path(root, SESSION, delivery_id))
            send.assert_called_once()
            self.assertEqual(queue_adds, [])
            ambiguous.assert_not_called()
            ack.assert_not_called()
            self.assertEqual(pending.read(root, SESSION, delivery_id)["state"], "pending")

    def test_notice_fence_survives_accepted_before_record_crash_and_new_watcher_reclaim(self) -> None:
        module = load_completion_module()
        pending = module.pending_delivery
        # Only explicit clock advances expire leases; durable I/O may exceed 10ms.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                module.time, "monotonic_ns", return_value=time.monotonic_ns()) as clock:
            root = Path(tmp)
            jobs = root / "jobs.log"
            jobs.write_text("", encoding="utf-8")
            delivery_id = "delivery-" + "a" * 32
            receipt = {
                "schema_version": 1, "kind": "supervision",
                "owner_attempt_id": "att-owner", "sealed_batch_id": "batch-exact",
            }

            def create_notice(selected_id, selected_receipt):
                return pending.create(
                    root, recipient_kind="codex-native-queue", recipient_key=SESSION,
                    delivery_id=selected_id, session_generation="1",
                    session_generation_supported="1", attempt_ids=["att-owner"],
                    parent_attempt_id="att-owner", route_id="rt-notice",
                    route_node="execute", receipt=selected_receipt,
                    receipt_digest=pending._canonical_receipt_digest(selected_receipt),
                    row_revisions={"att-owner": selected_receipt["sealed_batch_id"]},
                )

            create_notice(delivery_id, receipt)
            initial = pending.claim(
                root, SESSION, delivery_id, claim_owner="crashed-watcher",
                lease_seconds=0.01, require_generation_proof=True,
                live_recipient_generation=(SESSION, "1"), jobs=jobs,
                retry_context=f"{SESSION}:1:{SESSION}")
            initial_deadline = initial["claim_deadline_ns"]
            self.assertLess(clock.return_value, initial_deadline)
            fence_identity = pending.submission_fence_identity(initial, SESSION)
            pending.prepare_submission_fence(
                root, SESSION, delivery_id, claim_owner="crashed-watcher",
                expected_identity=fence_identity)
            accepted_adds = [delivery_id]  # The queue accepted after the durable pre-add fence.

            args = SimpleNamespace(
                queue_socket=Path("/tmp/app-server.sock"), jobs=jobs,
                parent_session_id=SESSION, sealed_batch_id="batch-exact",
                interval=0.1,
            )
            observed_calls = []
            current_delivery = []

            def queue_observation(*_args, **kwargs):
                observed_calls.append(kwargs)
                current_delivery.append(kwargs["client_message_id"])
                if kwargs["allow_add"]:
                    kwargs["before_add"]()
                    accepted_adds.append(kwargs["client_message_id"])
                    return {"status": "queued", "queued_submission_id": "queue-new",
                            "already_pending": False}
                raise module.codex_queue_delivery.QueueDeliveryError(
                    "queue-add-previously-unconfirmed", ambiguous=True)

            with mock.patch.object(module.notice_receipt, "validate_pending_record"), \
                 mock.patch.object(module.notice_receipt, "context", return_value={
                     "additionalContext": {"notice": {"value": "typed notice"}},
                 }), \
                 mock.patch.object(module.notice_receipt, "gateway_delivery_id",
                                   return_value="notice-id"), \
                 mock.patch.object(module.codex_queue_delivery, "send_at_least_once",
                                   side_effect=queue_observation), \
                 mock.patch.object(module.codex_queue_delivery, "list_queue",
                                   side_effect=lambda _socket, _thread_id, **_kw: [
                                       {"id": "queue-new",
                                        "clientUserMessageId": current_delivery[-1]}]), \
                 mock.patch.object(module, "queue_target", return_value=SESSION), \
                 mock.patch.object(module.codex_queue_delivery, "_known_consumed", return_value=None):
                watcher = module.NoticeWatcher(args, {"att-owner"}, capability=None)
                clock.return_value = initial_deadline + 1
                watcher._one(pending.record_path(root, SESSION, delivery_id))
                self.assertEqual(pending.read_submission_fence(
                    root, SESSION, delivery_id,
                    expected_identity=fence_identity)["state"], "prepared")

                old_claim = pending.read(root, SESSION, delivery_id)
                clock.return_value = old_claim["claim_deadline_ns"] + 1
                module.NoticeWatcher(args, {"att-owner"}, capability=None)._one(
                    pending.record_path(root, SESSION, delivery_id))
                self.assertEqual(pending.read_submission_fence(
                    root, SESSION, delivery_id,
                    expected_identity=fence_identity)["state"], "prepared")

                new_id = "delivery-" + "b" * 32
                new_receipt = {**receipt, "sealed_batch_id": "batch-new-conflict"}
                create_notice(new_id, new_receipt)
                module.NoticeWatcher(args, {"att-owner"}, capability=None)._one(
                    pending.record_path(root, SESSION, new_id))
                self.assertEqual(pending.read_submission_fence(
                    root, SESSION, new_id,
                    expected_identity=pending.submission_fence_identity(
                        pending.read(root, SESSION, new_id), SESSION))["state"], "accepted")

            self.assertEqual(accepted_adds, [delivery_id, new_id])
            self.assertEqual(len(observed_calls), 3)
            self.assertFalse(observed_calls[0]["allow_add"])
            self.assertFalse(observed_calls[1]["allow_add"])
            self.assertTrue(observed_calls[2]["allow_add"])
            self.assertTrue(all(not call["retry_ambiguous"] for call in observed_calls))
            old_record = pending.read(root, SESSION, delivery_id)
            self.assertEqual(old_record["state"], "claimed")
            self.assertIsNone(old_record["acked_at_ns"])
            self.assertEqual(pending.read(root, SESSION, new_id)["state"], "sent-ambiguous")


class NativeQueueSiblingBatchTest(unittest.TestCase):
    """One ready sibling batch reaches the Codex queue as one message from any sidecar."""

    def setUp(self):
        self.module = load_completion_module()
        self.pending = self.module.pending_delivery
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.held = False
        self.sent: list[dict] = []
        self.ids: list[str] = []

    def _create(self, index, *, failure=False):
        attempt = f"att-{index:032x}"
        delivery_id = f"delivery-{index:032x}"
        child = {"attempt_id": attempt, "status": "done", "readiness": "ready",
                 "reason": "completed",
                 "required_action": "inspect-done-failure" if failure else "advance-completed",
                 "delivery_classification": "attention" if failure else "success"}
        receipt = {"schema_version": 2, "state": "ready", "children": [child],
                   "delivery_classification": child["delivery_classification"],
                   "job_registry": str(self.jobs.resolve()), "parent_attempt_id": PARENT}
        self.pending.create(
            self.root, recipient_kind="codex-native-queue", recipient_key=SESSION,
            delivery_id=delivery_id, session_generation="", session_generation_supported="0",
            attempt_ids=[attempt], parent_attempt_id=PARENT, route_id="rt-fixture",
            route_node="frame", receipt=receipt,
            receipt_digest=self.pending._canonical_receipt_digest(receipt),
            row_revisions={attempt: "revision"})
        self.ids.append(delivery_id)
        return attempt, delivery_id, receipt

    def _row(self, attempt, delivery_id, receipt):
        record = self.pending.read(self.root, SESSION, delivery_id)
        return SimpleNamespace(attempt_id=attempt, metadata={
            "delivery_id": delivery_id, "delivery_recipient_kind": "codex-native-queue",
            "delivery_row_revision": "revision",
            "delivery_receipt_digest": record["receipt_digest"]})

    def _resolve(self, jobs, record, *, recipient_key="", delivery_root=None, now_ns=None):
        receipt = record.get("receipt") or {}
        if receipt.get("delivery_classification") != "success":
            return None
        open_ids = []
        for delivery_id in self.ids:
            member = self.pending.read(self.root, SESSION, delivery_id)
            if (member and member["receipt"].get("delivery_classification") == "success"
                    and member["state"] not in {"acked", "expired", "rejected"}):
                open_ids.append(delivery_id)
        info = Path(jobs).stat()
        return {"delivery_ids": tuple(sorted(open_ids)), "held": self.held,
                "registry_path": str(jobs),
                "registry_signature": (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns),
                "seed_identity": (record["delivery_id"], record["receipt_digest"],
                                  tuple(record["attempt_ids"]), record["recipient_digest"])}

    def _run(self, members, *, current=None):
        """Run each sibling's own sidecar once, in order; return their results."""
        rows = {attempt: self._row(attempt, delivery_id, receipt)
                for attempt, delivery_id, receipt in members}
        args = SimpleNamespace(queue_socket=Path("/tmp/app-server.sock"), thread_id=SESSION,
                               jobs=self.jobs, timeout=2.0, delivery_retry_interval=0.01)

        def send(path, **kwargs):
            if kwargs.get("before_add"):
                kwargs["before_add"]()
            self.sent.append(kwargs)
            return {"status": "consumed", "turn_id": f"turn-{len(self.sent)}",
                    "turn_status": "completed"}

        results = []
        module = self.module
        with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value={
                "thread": {"id": SESSION, "cwd": "/tmp/repo"}}), \
             mock.patch.object(module, "current_session_children",
                               side_effect=lambda _jobs, _thread, attempts, _kind: [
                                   rows[a] for a in sorted(attempts)]), \
             mock.patch.object(module, "_queue_target_from_rows", return_value=SESSION), \
             mock.patch("dispatch_notice_state.resolve_delivery_batch", side_effect=self._resolve), \
             mock.patch("dispatch_notice_state.notice_is_current",
                        side_effect=current or (lambda record, jobs=None: True)), \
             mock.patch.object(self.pending, "_replacement_disposition", return_value=""), \
             mock.patch.object(module.codex_queue_delivery, "send_at_least_once",
                               side_effect=send), \
             mock.patch("peer_obligations.retain_registered_completion", return_value=True):
            for attempt, _delivery_id, receipt in members:
                try:
                    results.append(module._native_queue_delivery_once(
                        args, receipt, {attempt}))
                except self.pending.PendingDeliveryError as exc:
                    results.append(exc)
        return results

    def _states(self):
        return {delivery_id: (self.pending.read(self.root, SESSION, delivery_id) or {}).get("state")
                for delivery_id in self.ids}

    def test_24_ready_siblings_wake_the_parent_once_and_ack_every_record(self):
        members = [self._create(index) for index in range(24)]
        results = self._run(members)
        self.assertEqual(len(self.sent), 1)
        message = self.sent[0]["message"]
        self.assertEqual(self.module._batch_client_id(SESSION, [d for _a, d, _r in members]),
                         self.module.codex_queue_delivery.native_client_message_id(
                             SESSION, [d for _a, d, _r in members], batch=True))
        self.assertTrue(self.sent[0]["client_message_id"].startswith("delivery-"))
        self.assertEqual(sum(1 for line in message.splitlines()
                             if line.startswith("HEARTING_RECEIPT_V1")), 24)
        for _attempt, delivery_id, _receipt in members:
            self.assertIn("delivery_id=" + delivery_id, message)
        self.assertEqual(set(self._states().values()), {"acked"})
        self.assertTrue(all(isinstance(result, dict) and result["status"] == "accepted"
                            for result in results), results)
        print(f"batch24 emissions={len(self.sent)} wakes={len(self.sent)} chars={len(message)}")

    def test_a_held_batch_emits_nothing_and_keeps_every_record_pending(self):
        members = [self._create(index) for index in range(24)]
        self.held = True
        results = self._run(members)
        self.assertEqual(self.sent, [])
        self.assertTrue(all(isinstance(result, self.pending.PendingDeliveryError)
                            and result.detail == "batch-hold" for result in results), results)
        self.assertEqual(set(self._states().values()), {"pending"})
        self.assertEqual({self.pending.read(self.root, SESSION, delivery_id)["attempts"]
                          for delivery_id in self.ids}, {0})

    def test_a_failure_record_is_delivered_immediately_on_its_own(self):
        members = [self._create(index) for index in range(3)]
        failure = self._create(3, failure=True)
        self.held = True  # The success siblings are still held; the failure must not wait.
        results = self._run([failure])
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0]["message"].startswith("AGENT_HARNESS_COMPLETION_V1\n"))
        record = self.pending.read(self.root, SESSION, failure[1])
        followup = self.module.completion_followup_text(
            failure[2], jobs=str(self.jobs),
            surface=str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh"))
        self.assertEqual(self.sent[0]["message"], self.module.codex_queue_delivery.native_completion_body(
            SESSION, [record], failure[2], followup))
        self.assertEqual(results[0]["status"], "accepted")
        states = self._states()
        self.assertEqual(states[failure[1]], "acked")
        self.assertEqual({states[member[1]] for member in members}, {"pending"})

    def test_an_already_received_member_is_retired_without_a_second_emission(self):
        members = [self._create(index) for index in range(3)]
        received = members[1][1]
        results = self._run(members, current=lambda record, jobs=None:
                            record["delivery_id"] != received)
        self.assertEqual(len(self.sent), 1)
        message = self.sent[0]["message"]
        self.assertNotIn("delivery_id=" + received, message)
        self.assertEqual(sum(1 for line in message.splitlines()
                             if line.startswith("HEARTING_RECEIPT_V1")), 2)
        states = self._states()
        self.assertEqual(states[received], "rejected")
        self.assertEqual({states[m[1]] for m in (members[0], members[2])}, {"acked"})
        self.assertTrue(all(isinstance(result, dict) for result in results), results)
        # Replaying every sidecar finds nothing owed and emits nothing more.
        self._run(members)
        self.assertEqual(len(self.sent), 1)

    def test_all_acked_courier_dedupe_attempts_retirement_and_keeps_accepted_envelope(self):
        members = [self._create(index) for index in range(2)]
        first = self._run(members)
        self.assertTrue(all(isinstance(result, dict) and result["status"] == "accepted"
                            for result in first), first)
        with mock.patch.object(
                self.module.codex_queue_delivery, "retire_acked_completion",
                side_effect=self.module.codex_queue_delivery.QueueDeliveryError("unavailable")) as retire:
            results = self._run(members)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["status"] == "accepted"
                            and result["deduplicated_by"] == "pending-delivery-ledger"
                            for result in results), results)
        self.assertEqual(retire.call_count, 2)
        by_client = {call.kwargs["expected_client_id"]: call for call in retire.call_args_list}
        self.assertEqual({result["client_user_message_id"] for result in results}, set(by_client))
        for result in results:
            call = by_client[result["client_user_message_id"]]
            self.assertEqual(call.args[2], SESSION)
            self.assertEqual(len(call.kwargs["expected_body"]), 1)
            records = call.args[3]
            storage = call.args[4]
            receipt = records[0]["receipt"]
            expected_id = self.module.codex_queue_delivery.native_client_message_id(
                storage, [records[0]["delivery_id"]])
            followup = self.module.completion_followup_text(
                receipt, jobs=str(self.jobs),
                surface=str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh"))
            expected_body = self.module.codex_queue_delivery.native_completion_body(
                storage, records, receipt, followup)
            self.assertEqual(result["client_user_message_id"], expected_id)
            self.assertEqual(call.kwargs["expected_body"], [expected_body])

    def test_only_a_single_sibling_that_was_already_received_queues_nothing(self):
        member = self._create(0)
        results = self._run([member], current=lambda record, jobs=None: False)
        self.assertEqual(self.sent, [])
        self.assertEqual(results[0]["status"], "accepted")
        self.assertEqual(self._states()[member[1]], "rejected")

    def test_a_batch_owned_by_another_sidecar_stays_quiet_and_untouched(self):
        members = [self._create(index) for index in range(4)]
        with mock.patch("dispatch_notice_state.resolve_delivery_batch", side_effect=self._resolve), \
             mock.patch.object(self.pending, "_replacement_disposition", return_value=""):
            owned = self.pending.claim_ready_batch(
                self.root, SESSION, members[0][1], claim_owner="other-sidecar",
                lease_seconds=60.0, jobs=self.jobs)
        self.assertEqual(len(owned), 4)
        results = self._run(members[1:])
        self.assertEqual(self.sent, [])
        self.assertEqual({result["reason"] for result in results},
                         {"native-queue-batch-owned-elsewhere"})
        self.assertEqual({self.pending.read(self.root, SESSION, d)["claim_owner"]
                          for d in self.ids}, {"other-sidecar"})

    def test_a_batch_too_large_for_one_message_is_split_without_losing_a_record(self):
        members = [self._create(index) for index in range(24)]
        with mock.patch.object(self.module, "QUEUE_BODY_LIMIT", 4000):
            self._run(members)
        self.assertGreater(len(self.sent), 1)
        self.assertTrue(all(len(call["message"].encode("utf-8")) <= 4000 for call in self.sent))
        self.assertEqual(len({call["client_message_id"] for call in self.sent}), len(self.sent))
        delivered = [d for _a, d, _r in members
                     if any("delivery_id=" + d in call["message"] for call in self.sent)]
        self.assertEqual(len(delivered), 24)
        self.assertEqual(set(self._states().values()), {"acked"})

    def test_a_body_queued_with_the_previous_follow_up_wording_is_recognized_exactly(self):
        module = self.module
        member = self._create(0)
        current = module._CURRENT_SUCCESS_FOLLOWUP_START + " Fixture current text.\nnext leg note"
        prior = module._prior_followup_text(current)
        self.assertTrue(prior.startswith(module._PRIOR_SUCCESS_FOLLOWUP))
        self.assertTrue(prior.endswith("\nnext leg note"))
        self.assertIsNone(module._prior_followup_text("Other wording"))
        sent = []

        def refuse_new_wording(path, **kwargs):
            sent.append(kwargs["message"])
            raise module.codex_queue_delivery.QueueDeliveryError(
                "queue-peer-history-mismatch", ambiguous=True)

        def consumed_with_prior(path, thread, client_id, *, expected_message=None, **_kw):
            if expected_message == sent[0].replace(current, prior):
                return {"id": "turn-prior", "status": "completed"}
            return None

        rows = [self._row(*member)]
        args = SimpleNamespace(queue_socket=Path("/tmp/app-server.sock"), thread_id=SESSION,
                               jobs=self.jobs, timeout=2.0, delivery_retry_interval=0.01)
        with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value={
                "thread": {"id": SESSION, "cwd": "/tmp/repo"}}), \
             mock.patch.object(module, "current_session_children", return_value=rows), \
             mock.patch.object(module, "_queue_target_from_rows", return_value=SESSION), \
             mock.patch.object(module, "completion_followup_text", return_value=current), \
             mock.patch("dispatch_notice_state.resolve_delivery_batch", side_effect=self._resolve), \
             mock.patch("dispatch_notice_state.notice_is_current", return_value=True), \
             mock.patch.object(self.pending, "_replacement_disposition", return_value=""), \
             mock.patch.object(module.codex_queue_delivery, "send_at_least_once",
                               side_effect=refuse_new_wording), \
             mock.patch.object(module.codex_queue_delivery, "_known_consumed",
                               side_effect=consumed_with_prior), \
             mock.patch("peer_obligations.retain_registered_completion", return_value=True):
            result = module._native_queue_delivery_once(args, member[2], {member[0]})
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(sent), 1)  # Never resent: the previous wording was the consumed body.
        self.assertEqual(self._states()[member[1]], "acked")




class ExpandedBatchAuthorityTest(unittest.TestCase):

    def case(self, mode, count=2, body_limit=None):
        import dispatch_pending_delivery as pending
        import dispatch_seat_handover as handover
        from dispatch_notice_state import resolve_delivery_batch
        from route_identity import route_hash
        mod = load_completion_module()
        if body_limit is not None:
            mod.QUEUE_BODY_LIMIT = body_limit
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / 'jobs.log'
            sid = 'registered-parent'
            ids = []
            metas = []
            receipts = []
            for i in range(count):
                aid = f'att-review-{i}'
                did = f'delivery-{i:032x}'
                rid = f'rt-review-{i}'
                route = {'route_id': rid, 'parent_cycle_id': 'cycle-review-batch', 'nodes': [{'id': 'frame'}]}
                route['route_hash'] = route_hash(route)
                rp = root / (rid + '.json')
                rp.write_text(json.dumps(route))
                child = {'attempt_id': aid, 'status': 'done', 'readiness': 'ready', 'reason': 'terminal-complete', 'required_action': 'advance-completed', 'harness': 'codex', 'delivery_classification': 'success'}
                receipt = {'schema_version': 2, 'state': 'ready', 'parent_attempt_id': 'att-review-parent', 'job_registry': str(jobs), 'children': [child], 'delivery_classification': 'success'}
                record = pending.create(root, recipient_kind='codex-native-queue', recipient_key=sid, delivery_id=did, session_generation='', session_generation_supported='0', attempt_ids=[aid], parent_attempt_id='att-review-parent', route_id=rid, route_node='frame', receipt=receipt, receipt_digest=pending._canonical_receipt_digest(receipt), row_revisions={aid: 'revision'})
                meta = {'attempt_id': aid, 'attempt_schema_version': '2', 'execution_surface': 'registered-headless', 'launch_claimed': '1', 'launch_outcome': 'reaped-before-publish', 'dispatch_depth': '1', 'registered_worker': '1', 'worker_type': 'frame', 'parent_sid': sid, 'parent_harness': 'codex', 'harness': 'codex', 'parent_attempt_id': 'att-review-parent', 'route_file': str(rp), 'route_id': rid, 'route_hash': route['route_hash'], 'route_node': 'frame', 'delivery_intent': '1', 'delivery_intent_at_ns': str(time.monotonic_ns()), 'failure_class': 'pass', 'note': 'completed-marker', 'delivery_id': did, 'parent_completion_delivery': 'codex-native-queue', 'delivery_recipient_kind': 'codex-native-queue', 'delivery_row_revision': 'revision', 'delivery_receipt_digest': record['receipt_digest']}
                ids.append(did)
                metas.append(meta)
                receipts.append(receipt)
            jobs.write_text(''.join(('2026-10-25T00:00:00Z\tdone\t/fixture/repo\t/fixture/wt\tframe\t' + ','.join((k + '=' + v for k, v in meta.items())) + '\n' for meta in metas)))
            with mock.patch.dict(os.environ, {'AGENT_DISPATCH_JOBS': str(jobs), 'XDG_STATE_HOME': str(root / 'state')}, clear=False):
                getattr(handover, '_LINEAGE_GRAPH_CACHE', {}).clear()
                seen = []
                sent = []
                protected = {}

                def targets():
                    return [handover.effective_parent(meta, jobs) for meta in metas]

                def move(index):
                    folder = root / 'state/hearting/session-tidy/sessions'
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / 'explicit-away.jsonl').write_text(json.dumps({'event': 'handover', 'source': 'retire', 'harness': 'codex', 'sid': 'new-holder', 'from': {'harness': 'codex', 'sid': sid}, 'ts': 1, 'bindings': [handover.binding_of(jobs, metas[index])]}) + '\n')
                    snapshots = root / 'state/hearting/session-tidy/handover'
                    snapshots.mkdir(parents=True, exist_ok=True)
                    (snapshots / 'explicit-away.json').write_text(json.dumps({'schema': 1, 'seat': {'kind': 'pane', 'key': 'explicit-away'}, 'from': {'harness': 'codex', 'sid': sid}, 'bindings': [handover.binding_of(jobs, metas[index])]}))
                batch = resolve_delivery_batch(jobs, pending.read(root, sid, ids[0]), recipient_key=sid, delivery_root=root)
                self.assertFalse(batch['held'])
                self.assertEqual(set(batch['delivery_ids']), set(ids))
                self.assertEqual(targets(), [sid] * count)
                if mode == 'already-away':
                    move(1)
                    self.assertEqual(targets(), [sid, 'new-holder'])

                def mutate(index):
                    if mode.startswith('native-away') or mode.startswith('emit-away'):
                        move(index)
                    elif mode.startswith(('owner-', 'lease-', 'identity-')):
                        path = pending.record_path(root, sid, ids[index])
                        raw = json.loads(path.read_text())
                        if mode.startswith('owner-'):
                            raw['claim_owner'] = 'other-carrier'
                        elif mode.startswith('lease-'):
                            raw['claim_deadline_ns'] = True
                        else:
                            raw['row_revisions'] = {metas[index]['attempt_id']: 'changed'}
                        path.write_text(json.dumps(raw))
                        protected[index] = path.read_bytes()

                def keep(_r, _s, did, _record, _owner, **kw):
                    seen.append(did)
                    if mode == 'late-away' and did == ids[-1]:
                        move(0)
                    return True

                def send(_path, **kwargs):
                    if mode.startswith(('native-away', 'owner-', 'lease-', 'identity-')):
                        mutate(int(mode.rsplit('-', 1)[1]))
                    if kwargs.get('before_add'):
                        kwargs['before_add']()
                    sent.append(dict({k: v for k, v in kwargs.items() if k != 'before_add'}, actual_current_recipients=targets()))
                    return {'status': 'queued', 'queued_submission_id': 'fixture-queue-item'}
                args = SimpleNamespace(queue_socket=root / 'no-native.sock', thread_id=sid, jobs=jobs, timeout=2.0, delivery_retry_interval=0.01)
                with mock.patch.object(mod.codex_queue_delivery, '_rpc', return_value={'thread': {'id': sid, 'cwd': '/fixture/repo'}}), mock.patch.object(mod.codex_queue_delivery, 'send_at_least_once', side_effect=send), mock.patch('dispatch_notice_state.keep_claim', side_effect=keep):
                    try:
                        result = mod._native_queue_delivery_once(args, receipts[0], {metas[0]['attempt_id']})
                    except mod.CompletionError as exc:
                        result = {'refused': str(exc)}
                print(json.dumps({'mode': mode, 'real_batch_ids': list(batch['delivery_ids']), 'keep_order': seen, 'sends': sent, 'result': result, 'states': [pending.read(root, sid, d)['state'] for d in ids]}, sort_keys=True), flush=True)
                if mode == 'current':
                    self.assertEqual(len(sent), 1)
                    self.assertTrue(all((d in sent[0]['message'] for d in ids)))
                else:
                    self.assertEqual(sent, [], 'an expanded sibling batch must not send any member whose current recipient differs')
                    for index, did in enumerate(ids):
                        current = pending.read(root, sid, did)
                        self.assertIsNone(pending.read(root, 'new-holder', did))
                        if index in protected:
                            self.assertEqual(pending.record_path(root, sid, did).read_bytes(), protected[index])
                        else:
                            self.assertEqual(current['state'], 'pending')

    def test_future_unit_member_is_checked_before_first_native_add(self):
        self.case('native-away-23', count=24, body_limit=4000)

    def test_current_batch_positive(self):
        self.case('current')

    def test_already_reassigned_sibling_must_withhold_whole_batch(self):
        self.case('already-away')

    def test_later_member_check_cannot_leave_first_member_authority_stale(self):
        self.case('late-away')

    def test_singleton_and_four_member_positive(self):
        for count in (1, 4):
            with self.subTest(count=count):
                self.case('current', count)

    def test_every_member_at_native_boundary_and_mutable_claims(self):
        for kind in ('native-away', 'owner', 'lease', 'identity'):
            for index in (0, 1):
                with self.subTest(kind=kind, index=index):
                    self.case(kind + '-' + str(index))



def _notice_fixture():
    name = "codex_notice_store_fixture"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "utilities/peer_obligations.test.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class NoticeWatcherIdentityTest(unittest.TestCase):
    """Actual notice writer/store/claim/fence and watcher at native effects and receipts."""
    setUp = _notice_fixture().ObligationStoreTest.setUp
    tearDown = _notice_fixture().ObligationStoreTest.tearDown
    notices = _notice_fixture().CanonicalNoticeJoinTest.notices
    parent_row = _notice_fixture().CanonicalNoticeJoinTest.parent_row
    edge = _notice_fixture().CanonicalNoticeJoinTest.edge

    def notice(self, *, legacy=False, inherited=False):
        import dispatch_seat_handover as handover
        module = load_completion_module()
        sid, target = "watcher-original", "watcher-original"
        index = len(list(self.root.glob("rt-canonical-*.json")))
        meta = self.parent_row(sid, harness="codex", index=index) if not legacy else None
        label = "watcher-" + str(len(list(self.root.rglob("delivery-*.json"))))
        if legacy:
            with mock.patch("peer_obligations.ensure_normal_runner", return_value=False):
                record = _notice_fixture()._legacy_notice("codex", sid, key=label,
                    subject="ordinary notice", text="completed", jobs=self.jobs)
        else:
            record, = self.notices("codex", sid, label=label, lineage={
                "kind": "attempt-parent", "harness": "codex", "session_id": sid,
                "attempt_id": meta["attempt_id"]})
        if inherited:
            target = "watcher-successor"
            self.edge("codex", sid, target)
        handover._LINEAGE_GRAPH_CACHE.clear()
        args = SimpleNamespace(jobs=self.jobs, parent_session_id=target,
            sealed_batch_id="private-batch", queue_socket=self.root / "private.sock", interval=0.1)
        watcher = module.NoticeWatcher(args, set() if meta is None else {meta["attempt_id"]}, None)
        path = module.pending_delivery.record_path(self.root, sid, record["delivery_id"])
        return module, watcher, path, sid, target, meta

    @staticmethod
    def change(path, **changes):
        import dispatch_pending_delivery as pending
        record = json.loads(path.read_text())
        record.update(changes)
        path.write_text(json.dumps(record))
        # A replacement fixture includes its own valid existing metadata too;
        # stale sidecar binding would test corruption instead of identity CAS.
        for metadata_path in (pending.courier_retry_path(path), pending.courier_timeline_path(path)):
            if metadata_path.exists():
                metadata = json.loads(metadata_path.read_text())
                metadata["identity_digest"] = pending._courier_retry_identity_digest(record)
                metadata_path.write_text(json.dumps(metadata))
        return record

    def pair_away(self, meta, sid):
        import dispatch_seat_handover as handover
        import session_tidy
        route, digest, node = handover.route_identity(meta)
        session_tidy._append_ledger_entry(session_tidy.Seat("pane", "watcher-binding"), {
            "schema": 1, "event": "handover", "source": "retire", "harness": "claude",
            "from": {"harness": "codex", "sid": sid}, "sid": sid, "ts": 2,
            "bindings": [{"jobs": str(self.jobs.resolve()), "route": route, "hash": digest,
                "node": node, "attempt": meta["attempt_id"], "parent": sid}]})
        handover._LINEAGE_GRAPH_CACHE.clear()

    def boundary(self, mutation, *, legacy=False, inherited=False, after=False, history=False):
        module, watcher, path, sid, target, meta = self.notice(legacy=legacy, inherited=inherited)
        pending = module.pending_delivery
        writes, admitted, mutated = [], [], []
        def mutate():
            value = json.loads(path.read_text())
            if mutation == "pair":
                self.pair_away(meta, sid)
            elif mutation == "terminal":
                self.change(path, state="rejected", expiry_reason="private-terminal")
            elif mutation == "fence":
                identity = pending.submission_fence_identity(value, target)
                pending.prepare_submission_fence(self.root, sid, value["delivery_id"],
                    claim_owner=watcher.owner, expected_identity=identity)
            else:
                changes = {
                    "route_id": {"route_id": "replacement-route"},
                    "route_node": {"route_node": "another-node"},
                    "lineage": {"lineage": [*value["lineage"], "replacement-lineage"]},
                    "row_revisions": {"row_revisions": {next(iter(value["attempt_ids"]), "unused"): "changed"}},
                    "session_generation": {"session_generation": "changed"},
                    "recipient_kind": {"recipient_kind": "claude-parent-runtime"},
                    "session_generation_supported": {"session_generation_supported": "0"},
                    "attempt_ids": {"attempt_ids": ["notice-replacement"]},
                    "parent_attempt_id": {"parent_attempt_id": "notice-replacement"},
                    "foreign": {"claim_owner": "foreign-owner"},
                    "expired": {"claim_deadline_ns": time.monotonic_ns() - 1},
                    "bool": {"claim_deadline_ns": True},
                    "float": {"claim_deadline_ns": float(time.monotonic_ns() + 10**12)},
                }[mutation]
                self.change(path, **changes)
            mutated.append(path.read_bytes())
        def send(*_args, **kwargs):
            # Capture from the actual claim before any mutation, not a scan row.
            original = pending.read(self.root, sid, path.stem)
            admitted.append(pending.received_ack_identity(original, sid, path.stem))
            if mutation != "none" and not after:
                mutate()
            kwargs["before_add"]()
            writes.append(kwargs)
            if mutation != "none" and after:
                mutate()
            return {"status": "queued" if history else "consumed", "turn_id": "exact-original"}
        with mock.patch.object(module.codex_queue_delivery, "send_at_least_once", side_effect=send), \
             mock.patch.object(module.codex_queue_delivery, "list_queue", return_value=[]), \
             mock.patch.object(module.codex_queue_delivery, "_known_consumed",
                 return_value={"id": "exact-original", "status": "completed"}):
            watcher._one(path, storage_key=sid)
        current = pending.read(self.root, sid, path.stem)
        self.assertEqual(len(writes), int(mutation == "none" or after))
        if mutation == "none":
            self.assertEqual(current["state"], "acked")
            self.assertEqual(pending.received_ack_identity(current, sid, path.stem), admitted[0])
            self.assertEqual(writes[0]["thread_id"], target)
            from dispatch_recipient_receipt import receipt_marker_for_identity
            self.assertIn(receipt_marker_for_identity(path.stem, admitted[0]), writes[0]["message"])
            self.assertIsNone(pending.read(self.root, target, path.stem) if inherited else None)
        else:
            self.assertNotEqual(current["state"], "acked")
            # Exception/rejection/finally writers preserve changed, foreign,
            # terminal and fenced rows, including bytes outside public records.
            self.assertTrue(mutated, list(watcher.errors))
            if mutation == "pair":
                self.assertEqual(current["state"], "pending")
                self.assertIsNone(current["claim_owner"])
                self.assertEqual(pending.received_ack_identity(current, sid, path.stem), admitted[0])
            else:
                self.assertEqual(path.read_bytes(), mutated[-1])

    def test_real_canonical_and_legacy_own_and_inherited_positive(self):
        # Each shape gets its own independent fixture/graph.
        for legacy, inherited in ((False, False), (True, False), (True, True)):
            with self.subTest(legacy=legacy, inherited=inherited):
                self.boundary("none", legacy=legacy, inherited=inherited)

    def test_full_identity_changes_at_actual_callback_are_held(self):
        for field in ("route_id", "lineage", "session_generation", "session_generation_supported", "recipient_kind"):
            with self.subTest(field=field):
                self.boundary(field)
        # Zero-attempt canonical records require route_node=notice and empty
        # row revisions. These mutations use the supported legacy record shape.
        for field in ("route_node", "row_revisions", "attempt_ids", "parent_attempt_id"):
            with self.subTest(field=field):
                self.boundary(field, legacy=True)

    def test_claim_and_lease_changes_at_callback_are_held(self):
        for mutation in ("foreign", "expired", "bool", "float"):
            with self.subTest(mutation=mutation):
                self.boundary(mutation)

    def test_terminal_and_fence_mutations_keep_original_responsibility(self):
        for mutation in ("terminal", "fence"):
            with self.subTest(mutation=mutation):
                self.boundary(mutation)

    def test_same_sid_cross_harness_is_held(self):
        self.boundary("pair")

    def test_current_send_uses_qualified_successor_pair(self):
        self.boundary("none", inherited=True)

    def test_native_observation_mutation_is_seen_by_real_queue_helper(self):
        module, watcher, path, sid, target, _meta = self.notice()
        reads, adds = [], []
        def history(*_args, **_kwargs):
            reads.append(True)
            if len(reads) == 2:
                self.change(path, route_id="replacement-after-native-observations")
            return None
        def rpc(_path, method, params, **kwargs):
            adds.append(method)
            raise AssertionError("ineligible delivery reached native RPC")
        with mock.patch.object(module.codex_queue_delivery, "_known_consumed", side_effect=history), \
             mock.patch.object(module.codex_queue_delivery, "list_queue", return_value=[]), \
             mock.patch.object(module.codex_queue_delivery, "_latest_turn_status", return_value="completed"), \
             mock.patch.object(module.codex_queue_delivery, "_rpc", side_effect=rpc):
            watcher._one(path)
        self.assertEqual(len(reads), 2)
        self.assertEqual(adds, [])
        self.assertEqual(json.loads(path.read_text())["route_id"], "replacement-after-native-observations")

    def test_direct_consumed_and_strict_history_reject_replacement_identity(self):
        for history in (False, True):
            with self.subTest(history=history):
                self.boundary("route_id", after=True, history=history)

    def test_receipt_cas_preserves_concurrent_terminal(self):
        for history in (False, True):
            with self.subTest(history=history):
                self.boundary("terminal", after=True, history=history)

    def test_original_receipt_remains_valid_after_send_authority_changes(self):
        for history in (False, True):
            for change in ("expired", "foreign", "pair"):
                with self.subTest(history=history, change=change):
                    module, watcher, path, sid, _target, meta = self.notice()
                    original = []
                    def send(*_args, **kwargs):
                        original.append(module.pending_delivery.received_ack_identity(
                            module.pending_delivery.read(self.root, sid, path.stem), sid, path.stem))
                        kwargs["before_add"]()
                        if change == "pair":
                            self.pair_away(meta, sid)
                        else:
                            self.change(path, **({"claim_owner": "new-owner"} if change == "foreign"
                                else {"claim_deadline_ns": time.monotonic_ns() - 1}))
                        return {"status": "queued" if history else "consumed", "turn_id": "exact"}
                    with mock.patch.object(module.codex_queue_delivery, "send_at_least_once", side_effect=send), \
                         mock.patch.object(module.codex_queue_delivery, "list_queue", return_value=[]), \
                         mock.patch.object(module.codex_queue_delivery, "_known_consumed", return_value={"id": "exact"}):
                        watcher._one(path)
                    value = module.pending_delivery.read(self.root, sid, path.stem)
                    self.assertEqual(value["state"], "acked")
                    self.assertEqual(module.pending_delivery.received_ack_identity(value, sid, path.stem), original[0])

    def test_ack_write_failure_restart_observes_original_fence_before_current_authority(self):
        module, watcher, path, sid, target, meta = self.notice(inherited=True)
        pending = module.pending_delivery
        admitted = []
        def consume(*_args, **kwargs):
            kwargs["before_add"]()
            admitted.append(kwargs)
            return {"status": "consumed", "turn_id": "exact-original"}
        with mock.patch.object(module.codex_queue_delivery, "send_at_least_once", side_effect=consume), \
             mock.patch.object(pending, "ack_received", side_effect=OSError("private ACK write failed")):
            watcher._one(path, storage_key=sid)
        self.assertEqual(len(admitted), 1)
        fence = pending.read_submission_fence(self.root, sid, path.stem)
        self.assertEqual(fence["target_thread_id"], target)
        self.assertEqual(fence["state"], "accepted")
        # Lose all current authority without altering original delivery identity.
        self.jobs.write_text("partial-registry")
        restart = module.NoticeWatcher(watcher.args, watcher.attempts, None)
        with mock.patch.object(module.codex_queue_delivery, "send_at_least_once") as send, \
             mock.patch.object(module.codex_queue_delivery, "_known_consumed", return_value={"id": "exact-original"}) as history, \
             mock.patch.object(module, "queue_target", side_effect=AssertionError("historical ACK asked for current target")):
            self.assertIn((path, sid), restart._candidate_paths())
            restart._one(path, storage_key=sid)
        send.assert_not_called()
        history.assert_called_once_with(watcher.args.queue_socket, target, path.stem,
            timeout=5.0, strict_history=True, expected_message=admitted[0]["message"])
        value = pending.read(self.root, sid, path.stem)
        self.assertEqual(value["state"], "acked")
        self.assertEqual(pending.received_ack_identity(value, sid, path.stem), fence["record_identity"])

    def test_old_body_never_acks_replacement_after_restart(self):
        module, watcher, path, sid, _target, _meta = self.notice()
        pending = module.pending_delivery
        def consume(*_args, **kwargs):
            kwargs["before_add"]()
            return {"status": "consumed", "turn_id": "exact-original"}
        with mock.patch.object(module.codex_queue_delivery, "send_at_least_once", side_effect=consume), \
             mock.patch.object(pending, "ack_received", side_effect=OSError("private ACK write failed")):
            watcher._one(path)
        self.change(path, route_id="replacement-after-restart")
        before = path.read_bytes()
        with mock.patch.object(module.codex_queue_delivery, "send_at_least_once") as send, \
             mock.patch.object(module.codex_queue_delivery, "_known_consumed", return_value={"id": "old-turn"}) as history:
            module.NoticeWatcher(watcher.args, watcher.attempts, None)._one(path)
        self.assertEqual(path.read_bytes(), before)
        send.assert_not_called()
        history.assert_not_called()

    def test_prepared_accepted_and_ambiguous_fences_do_not_add_again(self):
        for state in ("prepared", "accepted", "ambiguous"):
            with self.subTest(state=state):
                module, watcher, path, sid, target, _meta = self.notice()
                pending = module.pending_delivery
                original = pending.claim(self.root, sid, path.stem, claim_owner="old-watcher", lease_seconds=30,
                    require_generation_proof=True, live_recipient_generation=(sid, "1"),
                    jobs=self.jobs, retry_context=f"{sid}:1:{target}")
                identity = pending.submission_fence_identity(original, target)
                pending.prepare_submission_fence(self.root, sid, path.stem,
                    claim_owner="old-watcher", expected_identity=identity)
                if state == "accepted":
                    pending.record_submission_accepted(self.root, sid, path.stem,
                        claim_owner="old-watcher", expected_identity=identity)
                elif state == "ambiguous":
                    pending.mark_sent_ambiguous(self.root, sid, path.stem, claim_owner="old-watcher")
                self.change(path, claim_deadline_ns=time.monotonic_ns() - 1)
                def observe(*_args, **kwargs):
                    self.assertFalse(kwargs["allow_add"])
                    raise module.codex_queue_delivery.QueueDeliveryError("queue-add-previously-unconfirmed", ambiguous=True)
                with mock.patch.object(module.codex_queue_delivery, "_known_consumed", return_value=None), \
                     mock.patch.object(module.codex_queue_delivery, "send_at_least_once", side_effect=observe) as send:
                    watcher._one(path)
                self.assertEqual(send.call_count, 1, list(watcher.errors))
                self.assertNotEqual(pending.read(self.root, sid, path.stem)["state"], "acked")
                self.assertNotEqual(pending.read(self.root, sid, path.stem)["state"], "pending")

    def test_real_queue_helper_writes_one_original_body_then_acks(self):
        module, watcher, path, sid, target, _meta = self.notice()
        adds = []
        def history(*_args, **kwargs):
            if not adds:
                return None
            self.assertEqual(kwargs["expected_message"], adds[0]["input"][0]["text"])
            return {"id": "exact-turn", "status": "completed"}
        def rpc(_path, method, params, **_kwargs):
            self.assertEqual(method, "thread/queue/add")
            adds.append(params)
            return {"queuedSubmission": {"id": "private-item"}}
        with mock.patch.object(module.codex_queue_delivery, "_known_consumed", side_effect=history), \
             mock.patch.object(module.codex_queue_delivery, "list_queue", return_value=[]), \
             mock.patch.object(module.codex_queue_delivery, "_latest_turn_status", return_value="completed"), \
             mock.patch.object(module.codex_queue_delivery, "_rpc", side_effect=rpc):
            watcher._one(path)
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["threadId"], target)
        self.assertEqual(adds[0]["clientUserMessageId"], path.stem)
        self.assertEqual(module.pending_delivery.read(self.root, sid, path.stem)["state"], "acked")

    def test_real_exact_already_pending_observation_skips_callback_and_add(self):
        module, watcher, path, sid, target, _meta = self.notice()
        pending = module.pending_delivery
        # A previously queued native body carries the generation from its real claim.
        original = pending.claim(self.root, sid, path.stem, claim_owner="prior-watcher",
            lease_seconds=30, require_generation_proof=True, live_recipient_generation=(sid, "1"), jobs=self.jobs)
        identity = pending.submission_fence_identity(original, target)
        pending.prepare_submission_fence(self.root, sid, path.stem,
            claim_owner="prior-watcher", expected_identity=identity)
        pending.record_submission_accepted(self.root, sid, path.stem,
            claim_owner="prior-watcher", expected_identity=identity)
        body, _legacy = watcher._native_notice_text(original, sid)
        item = {"id": "private-item", "clientUserMessageId": path.stem,
                "input": [{"type": "text", "text": body}]}
        self.change(path, claim_deadline_ns=time.monotonic_ns() - 1)
        with mock.patch.object(module.codex_queue_delivery, "_known_consumed", return_value=None), \
             mock.patch.object(module.codex_queue_delivery, "list_queue", return_value=[item]), \
             mock.patch.object(module.codex_queue_delivery, "_latest_turn_status", return_value="completed"), \
             mock.patch.object(module.codex_queue_delivery, "_rpc") as rpc, \
             mock.patch.object(pending, "prepare_submission_fence", wraps=pending.prepare_submission_fence) as prepare:
            watcher._one(path)
        rpc.assert_not_called()
        prepare.assert_not_called()
        self.assertEqual(pending.read(self.root, sid, path.stem)["state"], "sent-ambiguous")
        self.assertEqual(pending.read_submission_fence(self.root, sid, path.stem)["record_identity"], identity["record_identity"])

    def test_expired_claim_is_not_renewed_by_ordinary_native_watcher(self):
        module, watcher, path, sid, _target, _meta = self.notice()
        writes = []
        def send(*_args, **kwargs):
            self.change(path, claim_deadline_ns=time.monotonic_ns() - 1)
            kwargs["before_add"]()
            writes.append(True)
            return {"status": "consumed"}
        with mock.patch.object(module.pending_delivery, "renew_unsubmitted_claim", return_value=None) as noop, \
             mock.patch.object(module.codex_queue_delivery, "send_at_least_once", side_effect=send):
            watcher._one(path)
        noop.assert_not_called()
        self.assertEqual(writes, [])
        self.assertNotEqual(module.pending_delivery.read(self.root, sid, path.stem)["state"], "acked")

    def test_native_refusal_and_ambiguous_cleanup_preserve_original_snapshot(self):
        for ambiguous in (False, True):
            for mutation in ("identity", "owner", "terminal"):
                with self.subTest(ambiguous=ambiguous, mutation=mutation):
                    module, watcher, path, sid, _target, _meta = self.notice()
                    held = []
                    def send(*_args, **kwargs):
                        kwargs["before_add"]()
                        self.change(path, **({"route_id": "replacement-after-refusal"} if mutation == "identity"
                            else {"claim_owner": "foreign-owner"} if mutation == "owner"
                            else {"state": "rejected", "expiry_reason": "private-terminal"}))
                        held.append(path.read_bytes())
                        reason = ("queue-rpc-refused:thread/queue/add:-32600:private"
                                  if not ambiguous else "queue-timeout")
                        raise module.codex_queue_delivery.QueueDeliveryError(reason, ambiguous=ambiguous)
                    with mock.patch.object(module.codex_queue_delivery, "send_at_least_once", side_effect=send):
                        watcher._one(path)
                    self.assertEqual(len(held), 1)
                    self.assertEqual(path.read_bytes(), held[0])

    def test_gateway_rejection_cleanup_does_not_adopt_replacement(self):
        module = load_completion_module()
        receipt = {"schema_version": 1, "kind": "human-gate", "owner_attempt_id": "att-owner",
                   "sealed_batch_id": "private-batch"}
        record = watcher_record(module, self.root, "delivery-" + "f" * 32, receipt,
                                carrier="codex-managed-gateway")
        path = module.pending_delivery.record_path(self.root, SESSION, record["delivery_id"])
        args = SimpleNamespace(jobs=self.jobs, parent_session_id=SESSION, sealed_batch_id="private-batch",
                               control_socket=self.root / "private-control.sock", interval=0.1)
        watcher = module.NoticeWatcher(args, {"att-owner"}, None)
        held = []
        def reject(*_args, **kwargs):
            self.change(path, route_id="replacement-before-rejection")
            held.append(path.read_bytes())
            return {"status": "rejected", "reason": "private-permanent-refusal"}
        with mock.patch.object(module, "negotiate_human_gate", return_value={"epoch": 1}), \
             mock.patch.object(module.notice_receipt, "validate_pending_record"), \
             mock.patch.object(module.notice_receipt, "gateway_delivery_id", return_value="private-gate"), \
             mock.patch.object(module, "gateway_request", side_effect=reject):
            watcher._one(path)
        self.assertEqual(len(held), 1)
        self.assertEqual(path.read_bytes(), held[0])

    def test_resolved_supervision_cleanup_uses_only_the_original_snapshot(self):
        for after_claim in (False, True):
            for replacement in (False, True):
                with self.subTest(after_claim=after_claim, replacement=replacement):
                    module = load_completion_module()
                    receipt = {"schema_version": 1, "kind": "supervision", "owner_attempt_id": "att-owner",
                               "sealed_batch_id": "private-batch"}
                    record = watcher_record(module, self.root,
                        "delivery-" + hashlib.sha256(f"{after_claim}:{replacement}".encode()).hexdigest(), receipt,
                        carrier="codex-managed-gateway")
                    path = module.pending_delivery.record_path(self.root, SESSION, record["delivery_id"])
                    args = SimpleNamespace(jobs=self.jobs, parent_session_id=SESSION, sealed_batch_id="private-batch",
                                           control_socket=self.root / "private-control.sock", interval=0.1)
                    watcher = module.NoticeWatcher(args, {"att-owner"}, None)
                    calls, held = [], []
                    def validate(*_args, **_kwargs):
                        calls.append(True)
                        if len(calls) == (2 if after_claim else 1):
                            if replacement:
                                self.change(path, route_id="replacement-before-resolution")
                                held.append(path.read_bytes())
                            raise ValueError("supervision-resolved")
                        return receipt
                    with mock.patch.object(module, "negotiate_human_gate", return_value={"epoch": 1}), \
                         mock.patch.object(module.notice_receipt, "validate_pending_record", side_effect=validate), \
                         mock.patch.object(module, "gateway_request") as send:
                        watcher._one(path)
                    send.assert_not_called()
                    self.assertEqual(len(calls), 2 if after_claim else 1)
                    if replacement:
                        self.assertEqual(path.read_bytes(), held[0])
                    else:
                        value = module.pending_delivery.read(self.root, SESSION, path.stem)
                        self.assertEqual(value["state"], "rejected")
                        self.assertEqual(value["expiry_reason"], "supervision-resolved")

if __name__ == "__main__":
    unittest.main()
