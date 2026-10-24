#!/usr/bin/env python3
"""SD-111 P4: dispatch_session_sweep unit tests.

Every fixture injects HOME/XDG_STATE_HOME/HARNESS_STATE_ROOT into an isolated
temp tree and appends the actual values to evidence/sd111/fixture_env.tsv
(plan §10.1 hard gate), matching P1/P2's own fixture pattern.
"""

from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import os
import shutil
import subprocess
import select
import socket
import threading
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PD = _load("dispatch_pending_delivery", HERE / "dispatch_pending_delivery.py")
SWEEP = _load("dispatch_session_sweep", HERE / "dispatch_session_sweep.py")
HOOK = _load("dispatch_session_sweep_hook", ROOT / "hooks/dispatch-session-sweep.py")
REQUEST_RECONNECT = SWEEP.request_obligation_reconciliation
import dispatch_batch_obligations as OBLIGATIONS  # noqa: E402
import peer_obligations  # noqa: E402

FIXTURE_ENV_LOG = os.environ.get("SD111_FIXTURE_ENV_LOG")


def _log_fixture_env(test_file: str, home: str, xdg: str, harness: str) -> None:
    if not FIXTURE_ENV_LOG:
        return
    with open(FIXTURE_ENV_LOG, "a", encoding="utf-8") as handle:
        handle.write(f"{test_file}\t{home}\t{xdg}\t{harness}\n")


class _SlowRecoveryBarrier:
    """Hold the detached probe after entry until the test releases it."""

    def __init__(self, path: Path, timeout: float = 4.0):
        self.path = path
        self.timeout = timeout
        self._condition = threading.Condition()
        self._release_requested = False
        self._entered = False
        self._released = False
        self._finished = False
        self._error = None
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(1)
        self._server.settimeout(0.1)
        self._thread = threading.Thread(target=self._serve, name="slow-recovery-barrier", daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            while True:
                with self._condition:
                    if self._release_requested:
                        return
                try:
                    connection, _ = self._server.accept()
                except socket.timeout:
                    continue
                except OSError as exc:
                    with self._condition:
                        if not self._release_requested:
                            self._error = f"{type(exc).__name__}: {exc}"
                        self._condition.notify_all()
                    return
                with connection:
                    connection.settimeout(self.timeout)
                    try:
                        if connection.recv(1) != b"E":
                            raise RuntimeError("probe did not signal barrier entry")
                        with self._condition:
                            self._entered = True
                            self._condition.notify_all()
                            released = self._condition.wait_for(
                                lambda: self._release_requested, timeout=self.timeout
                            )
                            if not released:
                                self._error = "test did not release the entered probe before timeout"
                                self._release_requested = True
                                self._condition.notify_all()
                        connection.sendall(b"R" if released else b"T")
                        with self._condition:
                            self._released = released
                            self._condition.notify_all()
                    except Exception as exc:
                        with self._condition:
                            self._error = f"{type(exc).__name__}: {exc}"
                            self._condition.notify_all()
                return
        finally:
            self._server.close()
            self.path.unlink(missing_ok=True)
            with self._condition:
                self._finished = True
                self._condition.notify_all()

    @property
    def error(self):
        with self._condition:
            return self._error

    @property
    def is_held(self):
        with self._condition:
            return self._entered and not self._release_requested and not self._finished

    def wait_until_entered(self, timeout: float) -> bool:
        with self._condition:
            ready = self._condition.wait_for(
                lambda: self._entered or self._error is not None or self._finished,
                timeout=timeout,
            )
            return ready and self._entered

    def release(self):
        with self._condition:
            self._release_requested = True
            self._condition.notify_all()

    def join(self, timeout: float = 5.0) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()


def _wait_for_child_exit(test_case, pid: int, timeout: float = 5.0):
    """Observe the detached fixture helper exit before its temporary tree is removed."""
    if hasattr(os, "pidfd_open"):
        try:
            pidfd = os.pidfd_open(pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            pidfd = None
        if pidfd is not None:
            try:
                poller = select.poll()
                poller.register(pidfd, select.POLLIN)
                if poller.poll(int(timeout * 1000)):
                    return
                test_case.fail(f"detached recovery helper pid {pid} did not exit within {timeout}s")
            finally:
                os.close(pidfd)
            return

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        stat_path = Path(f"/proc/{pid}/stat")
        try:
            if stat_path.read_text(encoding="utf-8").split()[2] == "Z":
                return
        except OSError:
            return
        time.sleep(0.02)
    test_case.fail(f"detached recovery helper pid {pid} did not exit within {timeout}s")


def _finish_slow_recovery_probe(test_case, barrier, requests: Path, bodies: Path):
    """Release and observe the fixture server and any detached helper before teardown."""
    barrier.release()
    if not barrier.join():
        test_case.fail("slow recovery barrier server did not stop after release")
    if requests.exists():
        request_rows = _read_jsonl_after(test_case, requests)
        _read_jsonl_after(test_case, bodies)
        helper_pid = request_rows[0].get("child_pid")
        if helper_pid is not None:
            _wait_for_child_exit(test_case, helper_pid)


def _slow_recovery_probe(base: Path, record_path: Path, snapshot_path: Path,
                         barrier: _SlowRecoveryBarrier | None = None):
    """Instrument the common detached entry and hold only the existing ensure body."""
    support = base / "slow-recovery-site"
    support.mkdir(parents=True, exist_ok=True)
    requests = base / "common-recovery-requests.jsonl"
    bodies = base / "slow-recovery-bodies.jsonl"
    (support / "sitecustomize.py").write_text(
        "import json, os, socket, subprocess, time\n"
        "from pathlib import Path\n"
        "import peer_obligations\n"
        "_real_popen = subprocess.Popen\n"
        "_real_run = subprocess.run\n"
        "def _append(path, value):\n"
        "    with open(path, 'a', encoding='utf-8') as handle:\n"
        "        handle.write(json.dumps(value) + '\\n')\n"
        "def _tracked_popen(args, *pos, **kwargs):\n"
        "    argv = [str(item) for item in args] if not isinstance(args, str) else [args]\n"
        "    child = _real_popen(args, *pos, **kwargs)\n"
        "    if len(argv) >= 3 and Path(argv[1]).name == 'peer-steward.py' and argv[2] == '__ensure-obligations':\n"
        "        _append(os.environ['R1_REQUEST_LOG'], {'pid': os.getpid(), 'child_pid': child.pid, 'argv': argv, 'cwd': kwargs.get('cwd'), 'start_new_session': kwargs.get('start_new_session')})\n"
        "    return child\n"
        "subprocess.Popen = _tracked_popen\n"
        "def _tracked_run(args, *pos, **kwargs):\n"
        "    argv = [str(item) for item in args] if not isinstance(args, str) else [args]\n"
        "    if os.environ.get('R1_CONTEXT_OUTPUT') and any(Path(item).name == 'local-evidence-inject.sh' for item in argv):\n"
        "        return subprocess.CompletedProcess(args, 0, os.environ['R1_CONTEXT_OUTPUT'], '')\n"
        "    return _real_run(args, *pos, **kwargs)\n"
        "subprocess.run = _tracked_run\n"
        "def _slow_ensure(*args, **kwargs):\n"
        "    barrier_error = None\n"
        "    barrier_path = os.environ.get('R1_RECOVERY_BARRIER')\n"
        "    if barrier_path:\n"
        "        try:\n"
        "            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as barrier:\n"
        "                barrier.settimeout(float(os.environ['R1_RECOVERY_BARRIER_TIMEOUT']))\n"
        "                barrier.connect(barrier_path)\n"
        "                barrier.sendall(b'E')\n"
        "                if barrier.recv(1) != b'R':\n"
        "                    raise RuntimeError('recovery barrier was not released normally')\n"
        "        except Exception as exc:\n"
        "            barrier_error = f'{type(exc).__name__}: {exc}'\n"
        "    else:\n"
        "        time.sleep(float(os.environ['R1_RECOVERY_DELAY']))\n"
        "    try:\n"
        "        state = json.loads(Path(os.environ['R1_RECORD_PATH']).read_text(encoding='utf-8')).get('state')\n"
        "    except Exception as exc:\n"
        "        state = 'unreadable:' + type(exc).__name__\n"
        "    try:\n"
        "        snapshot = Path(os.environ['R1_SNAPSHOT_PATH']).read_text(encoding='utf-8')\n"
        "    except OSError:\n"
        "        snapshot = None\n"
        "    _append(os.environ['R1_BODY_LOG'], {'pid': os.getpid(), 'state': state, 'snapshot': snapshot, 'barrier_error': barrier_error, 'kwargs': {key: str(value) for key, value in kwargs.items()}})\n"
        "    return True\n"
        "peer_obligations.ensure_normal_runner = _slow_ensure\n",
        encoding="utf-8",
    )
    env = {
        "PYTHONPATH": str(support) + os.pathsep + str(Path(__file__).resolve().parent),
        "R1_REQUEST_LOG": str(requests),
        "R1_BODY_LOG": str(bodies),
        "R1_RECORD_PATH": str(record_path),
        "R1_SNAPSHOT_PATH": str(snapshot_path),
        "R1_RECOVERY_DELAY": "0.8",
    }
    if barrier is not None:
        env.update(R1_RECOVERY_BARRIER=str(barrier.path),
                   R1_RECOVERY_BARRIER_TIMEOUT=str(barrier.timeout))
    return env, requests, bodies


def _read_jsonl_after(test_case, path: Path, count: int = 1):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            if len(rows) >= count:
                return rows
        time.sleep(0.02)
    test_case.fail(f"timed out waiting for {count} rows in {path}")


class IsolatedRootMixin:
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sd111-p4-")
        base = Path(self._tmp.name)
        home = base / "home"
        xdg = base / "xdg-state"
        harness = base / "harness-state"
        for d in (home, xdg, harness):
            d.mkdir(parents=True, exist_ok=True)
        self._env_patch = {
            "HOME": str(home),
            "XDG_STATE_HOME": str(xdg),
            "HARNESS_STATE_ROOT": str(harness),
            "AGENT_DISPATCH_JOBS": str(harness / "dispatch" / "jobs.log"),
            "AGENT_PEER_LEDGER_ROOT": str(harness / "peer-ledger"),
            "AGENT_HOME": str(ROOT),
            # A registered worker's inherited role flags alter nested lifecycle
            # behavior. The suite starts as a main session; adapter tests opt
            # into worker mode explicitly in their subprocess environments.
            "AGENT_SESSION_ROLE": "",
            "AGENT_DISPATCH_CHILD": "",
            "AGENT_DISPATCH_DEPTH": "",
        }
        self._saved_env = {k: os.environ.get(k) for k in self._env_patch}
        os.environ.update(self._env_patch)
        _log_fixture_env(
            "dispatch_session_sweep.test.py", str(home), str(xdg), str(harness)
        )
        self.root = harness / "dispatch"
        self.enterContext(mock.patch.object(peer_obligations, "ensure_normal_runner"))

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()
        super().tearDown()


def _receipt(**overrides):
    base = {
        "schema_version": 2,
        "state": "delivered",
        "parent_attempt_id": "att-0000000000000000000000000000aaaa",
        "job_registry": "/tmp/sd111p4/jobs.log",
        "children": [
            {
                "attempt_id": "att-0000000000000000000000000000bbbb",
                "status": "done",
                "readiness": "ready",
                "reason": "terminal-failure-or-unclosed",
                "required_action": "inspect-done-failure",
                "harness": "claude",
                "delivery_classification": "attention",
            }
        ],
        "delivery_classification": "attention",
    }
    base.update(overrides)
    return base


def _project_settled_close(root, record, *, jobs=None):
    """Stand-in for a settled parent close: the carrier's in-memory view only."""
    receipt = record.get("receipt") or {}
    children = [dict(child, status="done", readiness="ready", reason="cancelled-by-parent",
                     required_action="advance-completed", delivery_classification="success")
                for child in receipt.get("children", [])]
    record["receipt"] = dict(receipt, children=children, delivery_classification="success")


class SweepTest(IsolatedRootMixin, unittest.TestCase):
    def test_worker_hook_flushes_and_acks_before_requesting_global_recovery(self):
        self._seed()
        hook = _load("worker_sweep_latency_fixture", ROOT / "hooks/dispatch-session-sweep.py")
        class Output(io.StringIO):
            flushed = False
            def flush(self):
                self.flushed = True
        output = Output()
        delivery_id = "delivery-" + "a" * 32
        observed = []
        def recovery(*, lifecycle_root=None):
            observed.append((output.flushed, delivery_id in output.getvalue(),
                             PD.read(self.root, "sess-owner", delivery_id)["state"]))
        with mock.patch.object(hook, "resolve_agent_home", return_value=ROOT), \
             mock.patch.object(hook, "dispatch_state_roots", return_value=(self.root,)), \
             mock.patch("peer_obligations.request_obligation_recovery", side_effect=recovery) as request, \
             mock.patch("dispatch_batch_obligations.ensure_observers") as expensive, \
             mock.patch.dict(os.environ, {"AGENT_SESSION_ROLE": "worker", "AGENT_DISPATCH_DEPTH": "2"}), \
             mock.patch.object(sys, "stdin", io.StringIO('{"session_id":"sess-owner","hook_event_name":"UserPromptSubmit"}')), \
             mock.patch.object(sys, "stdout", output):
            self.assertEqual(hook.main(), 0)
        expensive.assert_not_called()
        request.assert_called_once_with(lifecycle_root=ROOT)
        self.assertEqual(observed, [(True, True, "acked")])
        self.assertEqual(PD.read(self.root, "sess-owner", delivery_id)["attempts"], 1)
        again, _ = SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-owner")
        self.assertEqual(again, [])

    def test_records_past_the_prompt_budget_stay_pending_for_the_next_prompt(self):
        # Claude cancels a prompt hook at 10 s; a slow sweep hands out what it reached instead.
        self._seed()
        later = "delivery-" + "b" * 32
        child = "att-0000000000000000000000000000cccc"
        receipt = _receipt()
        receipt["children"] = [dict(receipt["children"][0], attempt_id=child)]
        self._seed(delivery_id=later, attempt_ids=[child], row_revisions={child: "deadbeef"},
                   receipt=receipt)
        with mock.patch.object(SWEEP, "DELIVER_BUDGET_SECONDS", 0):
            first, _ = SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-owner")
            self.assertEqual(len(first), 1)
            left = ({"delivery-" + "a" * 32, later} - {first[0]["delivery_id"]}).pop()
            self.assertEqual(PD.read(self.root, "sess-owner", left)["state"], "pending")
            second, _ = SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-owner")
        self.assertEqual([record["delivery_id"] for record in second], [left])

    def test_near_budget_24_member_batch_releases_whole_then_next_sweep_delivers(self):
        from types import SimpleNamespace
        self.root.mkdir(parents=True, exist_ok=True)
        jobs = self.root / "jobs.log"
        jobs.touch()
        ids = ["delivery-" + f"{index:032x}" for index in range(24)]
        for index, delivery_id in enumerate(ids):
            attempt = f"att-batch-{index:02d}"
            self._seed(delivery_id=delivery_id, attempt_ids=[attempt],
                       row_revisions={attempt: f"revision-{index}"})

        def batch_for(_jobs, record, **_kwargs):
            info = jobs.stat()
            return {"delivery_ids": tuple(ids), "held": False,
                    "registry_path": str(jobs),
                    "registry_signature": (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns),
                    "seed_identity": (record["delivery_id"], record["receipt_digest"],
                                      tuple(record["attempt_ids"]), record["recipient_digest"])}

        addressed = {**PD.read(self.root, "sess-owner", ids[0]),
                     SWEEP.STORAGE_KEY: "sess-owner"}
        def invoke(clock_values):
            clock = iter(clock_values)
            with mock.patch.object(SWEEP, "record_jobs", return_value=jobs), \
                 mock.patch("dispatch_notice_state.resolve_delivery_batch", side_effect=batch_for), \
                 mock.patch("dispatch_notice_state.keep_claim", return_value=True), \
                 mock.patch("dispatch_supervision.continue_for_parent", return_value=None), \
                 mock.patch.object(SWEEP, "addressed_records", return_value=([addressed], 24)), \
                 mock.patch.object(SWEEP, "_append_self_instrumentation"), \
                 mock.patch.object(SWEEP, "time", SimpleNamespace(
                     monotonic_ns=lambda: next(clock, clock_values[-1]))):
                return SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-owner")

        first, _ = invoke([0, 0, 0, 7_000_000_000])
        self.assertEqual(first, [])
        self.assertTrue(all(PD.read(self.root, "sess-owner", delivery_id)["state"] == "pending"
                            for delivery_id in ids))
        second, _ = invoke([0, 0, 0, 1_000_000_000])
        self.assertEqual(len(second), 24)
        self.assertEqual({record["delivery_id"] for record in second}, set(ids))

    def test_sessionstart_activation_leaves_one_recovery_request_to_hook_finally(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(HOOK, "resolve_agent_home", return_value=ROOT), \
             mock.patch.object(HOOK, "dispatch_state_roots", return_value=(self.root,)), \
             mock.patch.object(HOOK, "activate", return_value=[]) as activate, \
             mock.patch.object(HOOK, "request_obligation_reconciliation") as request, \
             mock.patch("sys.stdin", io.StringIO(json.dumps({
                 "session_id": "sess-owner", "hook_event_name": "SessionStart",
             }))), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(HOOK.main(), 0)
        activate.assert_called_once_with(self.root, HOOK.RECIPIENT_KIND, "sess-owner",
                                         request_recovery=False)
        request.assert_called_once_with(lifecycle_root=ROOT)
        self.assertEqual(output.getvalue(), "")

    def test_claude_native_feedback_settles_exact_receipt_without_reexposure(self):
        import dispatch_recipient_receipt as recipient_receipt
        import claude_project_dir
        self._seed()
        did = "delivery-" + "a" * 32
        PD.claim(self.root, "sess-owner", did, claim_owner="async-rewake",
                 lease_seconds=30, require_generation_proof=False)
        PD.mark_sent_ambiguous(self.root, "sess-owner", did, claim_owner="async-rewake")
        current = PD.read(self.root, "sess-owner", did)
        marker = recipient_receipt.receipt_marker(current, "sess-owner")
        runtime_home = Path(self._tmp.name) / "claude-home"
        project = runtime_home / "projects" / claude_project_dir.encode_project_dir(str(ROOT))
        project.mkdir(parents=True)
        transcript = project / "sess-owner.jsonl"
        transcript.write_text(json.dumps({
            "type": "user", "sessionId": "sess-owner",
            "origin": {"kind": "task-notification", "producer": "session-task"},
            "promptSource": "system", "turnOrigin": "task_notification",
            "message": {"content": (
                "<task-notification><task-id>fixture</task-id>"
                "<summary>Stop hook feedback</summary>" + marker +
                "</task-notification>")},
        }) + "\n", encoding="utf-8")
        payload = {"hook_event_name": "UserPromptSubmit", "cwd": str(ROOT),
                   "transcript_path": str(transcript)}
        output = io.StringIO()
        with mock.patch.object(HOOK, "resolve_agent_home", return_value=ROOT), \
             mock.patch.object(HOOK, "dispatch_state_roots", return_value=(self.root,)), \
             mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(runtime_home)}), \
             contextlib.redirect_stdout(output):
            self.assertEqual(HOOK._process_session(payload, "sess-owner"), 0)
        self.assertEqual(output.getvalue(), "")
        exact = PD.read(self.root, "sess-owner", did)
        self.assertEqual((exact["state"], exact["attempts"]), ("acked", 1))
        replay = io.StringIO()
        with mock.patch.object(HOOK, "resolve_agent_home", return_value=ROOT), \
             mock.patch.object(HOOK, "dispatch_state_roots", return_value=(self.root,)), \
             mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(runtime_home)}), \
             contextlib.redirect_stdout(replay):
            self.assertEqual(HOOK._process_session(payload, "sess-owner"), 0)
        self.assertEqual(replay.getvalue(), "")
        self.assertEqual(PD.read(self.root, "sess-owner", did)["attempts"], 1)

    def test_projected_parent_close_completion_is_delivered_with_its_stored_binding(self):
        import dispatch_recipient_receipt as recipient_receipt
        self._seed()
        did = "delivery-" + "a" * 32
        stored = PD.read(self.root, "sess-owner", did)
        with mock.patch("dispatch_notice_state.project_parent_close",
                        side_effect=_project_settled_close):
            records, _ = SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-owner")
        self.assertEqual(len(records), 1)
        claimed = PD.read(self.root, "sess-owner", did)
        self.assertEqual(claimed["receipt"], stored["receipt"])
        self.assertEqual(records[0][SWEEP.IDENTITY_KEY],
                         PD.received_ack_identity(claimed, "sess-owner", did))
        text = SWEEP.delivery_context([(self.root, records)])
        self.assertIn("required_action=advance-completed", text)
        self.assertNotIn("inspect-done-failure", text)
        # The prompt carrier marks the same binding the rewake carrier renders
        # from the stored record, so either carrier's receipt settles the other.
        self.assertIn("\n" + recipient_receipt.receipt_marker(claimed, "sess-owner"), text)

    def test_prompt_carrier_delivers_and_acks_a_projected_parent_close_completion(self):
        import dispatch_recipient_receipt as recipient_receipt
        self._seed()
        did = "delivery-" + "a" * 32
        output = io.StringIO()
        with mock.patch.object(HOOK, "resolve_agent_home", return_value=ROOT), \
             mock.patch.object(HOOK, "dispatch_state_roots", return_value=(self.root,)), \
             mock.patch("dispatch_notice_state.project_parent_close",
                        side_effect=_project_settled_close), \
             contextlib.redirect_stdout(output):
            self.assertEqual(HOOK._process_session(
                {"hook_event_name": "UserPromptSubmit", "cwd": str(ROOT)}, "sess-owner"), 0)
        context = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("required_action=advance-completed", context)
        acked = PD.read(self.root, "sess-owner", did)
        self.assertEqual(acked["state"], "acked")
        self.assertIn("\n" + recipient_receipt.receipt_marker(acked, "sess-owner"), context)

    def _seed(self, session_id="sess-owner", **overrides):
        receipt = overrides.pop("receipt", _receipt())
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "jobs.log").touch()
        receipt["job_registry"] = str(self.root / "jobs.log")
        kwargs = dict(
            root=self.root,
            recipient_kind="claude-parent-runtime",
            recipient_key=session_id,
            delivery_id="delivery-" + "a" * 32,
            session_generation="",
            session_generation_supported="0",
            attempt_ids=["att-0000000000000000000000000000bbbb"],
            parent_attempt_id="att-0000000000000000000000000000aaaa",
            route_id="rt-example",
            route_node="execute",
            receipt=receipt,
            receipt_digest=PD._canonical_receipt_digest(receipt),
            row_revisions={"att-0000000000000000000000000000bbbb": "deadbeef"},
        )
        kwargs.update(overrides)
        return PD.create(**kwargs)

    def _release_via_cli(self, record, *, session_id="sess-owner"):
        item = {
            "root": str(self.root),
            "storage_key": record.get(SWEEP.STORAGE_KEY) or session_id,
            "delivery_id": record["delivery_id"],
            "claim_owner": record["claim_owner"],
        }
        output = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"records": [item]}))), \
                contextlib.redirect_stdout(output):
            self.assertEqual(SWEEP.main([
                "release", "--recipient-kind", "claude-parent-runtime", "--session", session_id,
            ]), 0)
        return json.loads(output.getvalue())

    def test_empty_terminal_registry_uses_only_the_resolved_carrier_jobs_path(self):
        self.root.mkdir(parents=True, exist_ok=True)
        jobs=self.root/'jobs.log';jobs.touch()
        record={"receipt":{"job_registry":""}}
        with mock.patch("dispatch_contract.resolve_agent_home", return_value=self.root), \
             mock.patch("dispatch_contract.resolve_global_registry",
                        return_value=SimpleNamespace(path=jobs)):
            self.assertEqual(SWEEP.record_jobs(self.root, record), jobs.resolve())
            self.assertIsNone(SWEEP.record_jobs(self.root/'another-root', record))

    # -- 2026-08-29: Claude carrier 2 delivers (sweep_deliver / ack_delivered).

    def test_deliver_claims_pending_record_without_generation_proof(self):
        self._seed()
        records, count = SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-owner")
        self.assertEqual(count, 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["state"], "claimed")
        self.assertEqual(records[0]["attempts"], 1)
        text = SWEEP._bounded_receipt_text(records[0])
        self.assertIn("delivery_id=delivery-" + "a" * 32, text)
        self.assertIn("attempt_id=att-0000000000000000000000000000bbbb", text)
        self.assertNotIn("job_registry", text)

    def test_deliver_then_ack_makes_record_terminal_and_silent_next_time(self):
        self._seed()
        records, _ = SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-owner")
        self.assertEqual(SWEEP.ack_delivered(self.root, "sess-owner", records, acked_by="t"), 1)
        again, count = SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-owner")
        self.assertEqual(count, 1)
        self.assertEqual(again, [])
        state = PD.read(self.root, "sess-owner", "delivery-" + "a" * 32)["state"]
        self.assertEqual(state, "acked")

    def test_two_carriers_recheck_after_exact_receipt_at_final_output_barrier(self):
        import dispatch_recipient_receipt as recipient_receipt
        self._seed()
        did = "delivery-" + "a" * 32
        first_reads = threading.Barrier(2)
        final_recheck = threading.Event()
        receipt_written = threading.Event()
        snapshots = []
        local = threading.local()
        marker = recipient_receipt.receipt_marker(
            PD.read(self.root, "sess-owner", did), "sess-owner")

        def observe(_root, storage, record):
            count = getattr(local, "reads", 0) + 1
            local.reads = count
            if count == 1:
                snapshots.append(PD.received_ack_identity(record, storage, did))
                first_reads.wait(timeout=5)
                return recipient_receipt.claude_feedback_evidence([], "sess-owner")
            final_recheck.set()
            if not receipt_written.wait(timeout=5):
                return None
            return recipient_receipt.claude_feedback_evidence([{
                "type": "user", "sessionId": "sess-owner",
                "summary": "Stop hook feedback", "message": {"content": marker},
            }], "sess-owner")

        results = [None, None]
        def carrier(index):
            results[index] = SWEEP.sweep_deliver(
                self.root, "claude-parent-runtime", "sess-owner",
                receipt_observer=observe)[0]

        threads = [threading.Thread(target=carrier, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        self.assertTrue(final_recheck.wait(timeout=5))
        # Native receipt is recorded after both carriers read the same exact
        # identity and before the winner's final output recheck is released.
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0], snapshots[1])
        receipt_written.set()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, [[], []])
        current = PD.read(self.root, "sess-owner", did)
        self.assertEqual((current["state"], current["attempts"]), ("acked", 1))

    def test_deliver_retry_wait_survives_normal_release_and_new_recipient_progresses(self):
        self._seed(session_generation_supported="1")
        delivery_id = "delivery-" + "a" * 32
        now = time.time_ns()
        with mock.patch.object(PD.time, "time_ns", return_value=now):
            first, _ = SWEEP.sweep_deliver(
                self.root, "claude-parent-runtime", "sess-owner")
            self.assertEqual(len(first), 1)
            self.assertEqual(self._release_via_cli(first[0])["count"], 1)
        retry_path = PD.courier_retry_path(
            PD.record_path(self.root, "sess-owner", delivery_id))
        retry = json.loads(retry_path.read_text(encoding="utf-8"))["retry"]

        # Recalling the same recipient before its stored deadline cannot claim or emit again.
        with mock.patch.object(PD.time, "time_ns", return_value=retry["next_attempt_at_ns"] - 1):
            repeated, _ = SWEEP.sweep_deliver(
                self.root, "claude-parent-runtime", "sess-owner")
        self.assertEqual(repeated, [])
        self.assertEqual(PD.read(self.root, "sess-owner", delivery_id)["attempts"], 1)

        # A verified seat handover changes the actual recipient while keeping the same stored obligation.
        import dispatch_seat_handover as handover
        bound = frozenset({"att-0000000000000000000000000000bbbb"})
        with mock.patch.object(handover, "storage_recipients",
                               return_value=[("sess-owner", bound)]), \
                mock.patch.object(PD.time, "time_ns", return_value=retry["next_attempt_at_ns"] - 1):
            handed, _ = SWEEP.sweep_deliver(
                self.root, "claude-parent-runtime", "sess-new")
        self.assertEqual(len(handed), 1)
        self.assertEqual(SWEEP.ack_delivered(
            self.root, "sess-new", handed, acked_by="new-recipient-consumed"), 1)
        self.assertEqual(PD.read(self.root, "sess-owner", delivery_id)["state"], "acked")

    def test_probe_retry_wait_survives_normal_release_recall_and_ack(self):
        self._seed(session_generation_supported="1")
        delivery_id = "delivery-" + "a" * 32
        now = time.time_ns()
        with mock.patch.object(PD.time, "time_ns", return_value=now):
            outcome, count = SWEEP.sweep(
                self.root, "claude-parent-runtime", "sess-owner", "generation-proof")
            self.assertEqual((outcome, count), ("claimed", 1))
            first = PD.read(self.root, "sess-owner", delivery_id)
            self.assertEqual(self._release_via_cli(first)["count"], 1)
        retry = json.loads(PD.courier_retry_path(
            PD.record_path(self.root, "sess-owner", delivery_id)
        ).read_text(encoding="utf-8"))["retry"]

        with mock.patch.object(PD.time, "time_ns", return_value=retry["next_attempt_at_ns"] - 1):
            outcome, count = SWEEP.sweep(
                self.root, "claude-parent-runtime", "sess-owner", "generation-proof")
        self.assertEqual((outcome, count), ("refused", 1))
        self.assertEqual(PD.read(self.root, "sess-owner", delivery_id)["attempts"], 1)

        with mock.patch.object(PD.time, "time_ns", return_value=retry["next_attempt_at_ns"] + 1):
            outcome, count = SWEEP.sweep(
                self.root, "claude-parent-runtime", "sess-owner", "generation-proof")
        self.assertEqual((outcome, count), ("claimed", 1))
        claimed = PD.read(self.root, "sess-owner", delivery_id)
        self.assertEqual(SWEEP.ack_delivered(
            self.root, "sess-owner", [claimed], acked_by="probe-recipient-consumed"), 1)
        self.assertEqual(PD.read(self.root, "sess-owner", delivery_id)["state"], "acked")

    def test_deliver_reclaims_sent_ambiguous_record_only_after_lease_expiry(self):
        # The async rewake carrier leaves a record sent-ambiguous; a lost wake
        # must be re-deliverable by the sweep once the lease has expired.
        self._seed()
        first = PD.claim(
            self.root, "sess-owner", "delivery-" + "a" * 32,
            claim_owner="rewake:1", lease_seconds=60.0,
        )
        PD.mark_sent_ambiguous(
            self.root, "sess-owner", "delivery-" + "a" * 32, claim_owner="rewake:1"
        )
        early, _ = SWEEP.sweep_deliver(
            self.root, "claude-parent-runtime", "sess-owner", now_ns=first["claim_deadline_ns"] - 1
        )
        self.assertEqual(early, [])
        late, _ = SWEEP.sweep_deliver(
            self.root, "claude-parent-runtime", "sess-owner", now_ns=first["claim_deadline_ns"] + 1
        )
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]["attempts"], 2)

    def test_deliver_ignores_foreign_session_and_foreign_recipient_kind(self):
        self._seed()
        self.assertEqual(
            SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-other"), ([], 0)
        )
        records, count = SWEEP.sweep_deliver(self.root, "codex-stop-hook", "sess-owner")
        self.assertEqual((records, count), ([], 1))

    def test_standalone_notice_delivers_without_a_registry_binding(self):
        from session_notice import notify

        self.root.mkdir(parents=True, exist_ok=True)
        jobs = self.root / "jobs.log"
        jobs.touch()
        with mock.patch("dispatch_contract.resolve_global_registry",
                        return_value=SimpleNamespace(path=self.root / "elsewhere" / "jobs.log")):
            notice = notify("claude", "sess-owner", key="run-finished",
                            subject="compute run host-1", text="finished with exit 0",
                            jobs=jobs)
            claimed, count = SWEEP.sweep_deliver(
                self.root, "claude-parent-runtime", "sess-owner")

        self.assertEqual(count, 1)
        self.assertEqual([record["delivery_id"] for record in claimed], [notice["delivery_id"]])
        self.assertEqual(SWEEP.ack_delivered(self.root, "sess-owner", claimed, acked_by="test"), 1)
        self.assertEqual(SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-owner")[0], [])

    def test_a_successor_at_the_same_pane_receives_the_stored_record_and_acks_it_under_the_old_key(self):
        # After a /clear the record stays stored (and acked) under the registered parent; the
        # handover binds only the attempts it names to the new session.
        import dispatch_seat_handover as handover
        from unittest import mock
        self._seed(session_id="sess-old")
        self._seed(session_id="sess-old", delivery_id="delivery-" + "b" * 32,
                   attempt_ids=["att-0000000000000000000000000000cccc"],
                   row_revisions={"att-0000000000000000000000000000cccc": "beef"})
        bound = frozenset({"att-0000000000000000000000000000bbbb"})
        with mock.patch.object(handover, "storage_recipients",
                               return_value=[("sess-new", None), ("sess-old", bound)]):
            records, count = SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-new")
            self.assertEqual([r["delivery_id"] for r in records], ["delivery-" + "a" * 32])
            self.assertEqual(count, 2)                                  # both are seen; only the bound one is taken
            self.assertEqual(SWEEP.ack_delivered(self.root, "sess-new", records, acked_by="t"), 1)
        self.assertEqual(PD.read(self.root, "sess-old", "delivery-" + "a" * 32)["state"], "acked")
        self.assertEqual(PD.read(self.root, "sess-old", "delivery-" + "b" * 32)["state"], "pending")
        self.assertIsNone(PD.read(self.root, "sess-new", "delivery-" + "a" * 32))

    def test_real_clear_handover_delivers_late_and_aggregate_records_from_the_original_store(self):
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        import pane_ownership

        jobs = self.root / "jobs.log"
        self.root.mkdir(parents=True, exist_ok=True)
        jobs.touch()
        selected = [
            "att-0000000000000000000000000000bbbb",
            "att-0000000000000000000000000000cccc",
        ]
        foreign = "att-0000000000000000000000000000dddd"

        def append_attempt(attempt, status="running", *, route="rt-sweep", digest="sha256:sweep",
                           node="execute"):
            metadata = {
                "attempt_id": attempt,
                "parent_sid": "sess-old",
                "parent_harness": "claude",
                "dispatch_depth": "1",
                "worker_type": "frame",
                "route_id": route,
                "route_hash": digest,
                "route_node": node,
            }
            with jobs.open("a", encoding="utf-8") as handle:
                handle.write("now\t" + status + "\t/w\t/w\ttask\t" +
                             ",".join(f"{key}={value}" for key, value in metadata.items()) + "\n")
            return metadata

        append_attempt(selected[0])
        self._seed(session_id="sess-old", delivery_id="delivery-" + "a" * 32,
                   attempt_ids=[selected[0]], row_revisions={selected[0]: "old"})

        pane = "w1:pOld"
        old_environment = {
            "AGENT_DISPATCH_JOBS": str(jobs),
            "CLAUDE_SESSION_ID": "sess-old",
            "HERDR_PANE_ID": pane,
        }
        with mock.patch.dict(os.environ, old_environment, clear=False), \
             mock.patch.object(pane_ownership, "verified_pane", side_effect=lambda value, *a, **kw: value or ""):
            seat = tidy.resolve_seat("claude", str(self.root), os.environ, sid="sess-old")
            with tidy.seat_lock(seat.key):
                tidy.record_event(seat, "claude", "sess-old", "start", cwd=str(self.root))
                self.assertEqual(handover.write_snapshot_locked(
                    seat, "claude", "sess-old", jobs=jobs), 1)

            # A same-key attempt registered after the initial snapshot is refreshed
            # by the actual confirmed-start hook.
            append_attempt(selected[1])
            append_attempt(foreign, route="rt-other", digest="sha256:other", node="unrelated")
            os.environ["CLAUDE_SESSION_ID"] = "sess-new"
            tidy.run_hook("claude", "start", "sess-new", source="clear", cwd=str(self.root))

            snapshot = handover.read_snapshot(seat.key)
            ledger = handover.handover_rows(seat)
            expected = {(selected[0], "sess-old"), (selected[1], "sess-old")}
            self.assertEqual({(b["attempt"], b["parent"]) for b in snapshot["bindings"]}, expected)
            self.assertEqual({(b["attempt"], b["parent"]) for b in ledger[0]["bindings"]}, expected)

            # One completion is already pending before clear; the second is a late
            # completion materialized afterward.  A selected-only aggregate is
            # accepted, while an aggregate containing an unrelated attempt is refused.
            self._seed(session_id="sess-old", delivery_id="delivery-" + "b" * 32,
                       attempt_ids=[selected[1]], row_revisions={selected[1]: "late"})
            self._seed(session_id="sess-old", delivery_id="delivery-" + "c" * 32,
                       attempt_ids=selected, row_revisions={selected[0]: "old", selected[1]: "late"})
            self._seed(session_id="sess-old", delivery_id="delivery-" + "d" * 32,
                       attempt_ids=[selected[0], foreign], row_revisions={selected[0]: "old", foreign: "foreign"})

            addressed, count = SWEEP.addressed_records(self.root, "claude-parent-runtime", "sess-new")
            self.assertEqual(count, 4)
            self.assertEqual({record["delivery_id"] for record in addressed}, {
                "delivery-" + "a" * 32, "delivery-" + "b" * 32, "delivery-" + "c" * 32,
            })
            records, sweep_count = SWEEP.sweep_deliver(
                self.root, "claude-parent-runtime", "sess-new")
            self.assertEqual(sweep_count, 4)
            self.assertEqual({record["delivery_id"] for record in records}, {
                "delivery-" + "a" * 32, "delivery-" + "b" * 32, "delivery-" + "c" * 32,
            })
            self.assertEqual({record[SWEEP.STORAGE_KEY] for record in records}, {"sess-old"})
            self.assertEqual(SWEEP.ack_delivered(self.root, "sess-new", records, acked_by="clear-12-test"), 3)
            self.assertEqual(SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-new")[0], [])

        for suffix in "abc":
            stored = PD.read(self.root, "sess-old", "delivery-" + suffix * 32)
            self.assertEqual(stored["state"], "acked")
            self.assertIsNone(PD.read(self.root, "sess-new", stored["delivery_id"]))
        self.assertEqual(PD.read(self.root, "sess-old", "delivery-" + "d" * 32)["state"], "pending")

    def test_without_a_handover_another_session_still_receives_nothing(self):
        self._seed(session_id="sess-old")
        self.assertEqual(SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-new"), ([], 0))
        self.assertEqual(PD.read(self.root, "sess-old", "delivery-" + "a" * 32)["state"], "pending")

    def test_hook_injects_additional_context_and_acks(self):
        self._seed()
        output = io.StringIO()
        with mock.patch.object(HOOK, "resolve_agent_home", return_value=self.root.parent), \
             mock.patch.object(HOOK, "dispatch_state_roots", return_value=(self.root,)), \
             mock.patch.object(HOOK, "request_obligation_reconciliation"), \
             mock.patch("sys.stdin", io.StringIO(json.dumps({
                 "session_id": "sess-owner", "hook_event_name": "UserPromptSubmit",
             }))), contextlib.redirect_stdout(output):
            self.assertEqual(HOOK.main(), 0)
        payload = json.loads(output.getvalue())
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("att-0000000000000000000000000000bbbb", context)
        self.assertNotIn("Harvest each attempt", context)
        self.assertIn("Follow required_action", context)
        state = PD.read(self.root, "sess-owner", "delivery-" + "a" * 32)["state"]
        self.assertEqual(state, "acked")

    def test_reconnect_request_uses_detached_common_recovery_entry(self):
        with mock.patch.object(peer_obligations.subprocess, "Popen") as launch:
            REQUEST_RECONNECT(lifecycle_root=ROOT)
        command = launch.call_args.args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(Path(command[1]), ROOT / "utilities/peer-steward.py")
        self.assertEqual(command[2:], ["__ensure-obligations", "--lifecycle-root", str(ROOT)])
        self.assertEqual(launch.call_args.kwargs["cwd"], str(ROOT))
        self.assertEqual(launch.call_args.kwargs["env"], os.environ.copy())
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        self.assertTrue(launch.call_args.kwargs["close_fds"])
        self.assertIs(launch.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(launch.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(launch.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_user_prompt_flushes_real_pipe_before_ack_and_finishes_with_slow_recovery(self):
        self._seed()
        fixture = self.root.parent / "slow-recovery-fixture.py"
        gate = self.root.parent / "allow-ack"
        ack_entered = self.root.parent / "ack-entered"
        child_pid_file = self.root.parent / "recovery-child.pid"
        fixture.write_text(
            "import json,os,time\n"
            "open(os.environ['SLOW_RECOVERY_PID'],'w').write(str(os.getpid()))\n"
            "time.sleep(.05)\n"
            "env_path=os.environ['SLOW_RECOVERY_ENV']\n"
            "with open(env_path+'.tmp','w') as stream: json.dump({k:os.environ.get(k) for k in "
            "('AGENT_DISPATCH_JOBS','HARNESS_STATE_ROOT','AGENT_PEER_LEDGER_ROOT','XDG_STATE_HOME','AGENT_HOME','FIXTURE_LIFECYCLE_ROOT')},stream)\n"
            "os.replace(env_path+'.tmp',env_path)\n"
            "time.sleep(15)\n",
            encoding="utf-8",
        )
        harness = self.root.parent / "run-hook-fixture.py"
        harness.write_text(
            "import importlib.util,json,os,subprocess,sys,time\n"
            "from pathlib import Path\n"
            f"root=Path({str(ROOT)!r}); hook_path=root/'hooks/dispatch-session-sweep.py'\n"
            "sys.path.insert(0,str(root/'utilities'))\n"
            "spec=importlib.util.spec_from_file_location('fixture_hook',hook_path); hook=importlib.util.module_from_spec(spec); spec.loader.exec_module(hook)\n"
            "fixture=Path(os.environ['SLOW_RECOVERY_SCRIPT'])\n"
            "def request(*,lifecycle_root=None):\n"
            " env=os.environ.copy(); env['FIXTURE_LIFECYCLE_ROOT']=str(lifecycle_root)\n"
            " p=subprocess.Popen([sys.executable,str(fixture)],env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,start_new_session=True)\n"
            " Path(os.environ['SLOW_RECOVERY_PID_FILE']).write_text(str(p.pid))\n"
            " p.returncode=0\n"
            "hook.request_obligation_reconciliation=request\n"
            "hook.resolve_agent_home=lambda:Path(os.environ['FIXTURE_AGENT_HOME'])\n"
            "hook.dispatch_state_roots=lambda _home:(Path(os.environ['FIXTURE_DISPATCH_ROOT']),)\n"
            "real_ack=hook.ack_delivered\n"
            "def ack(*args,**kwargs):\n"
            " time.sleep(.05)\n"
            " Path(os.environ['ACK_ENTERED']).write_text(str(time.monotonic_ns()))\n"
            " deadline=time.monotonic()+5\n"
            " while not Path(os.environ['ACK_GATE']).exists() and time.monotonic()<deadline: time.sleep(.005)\n"
            " result=real_ack(*args,**kwargs)\n"
            " Path(os.environ['ACK_COMMITTED']).write_text(str(time.monotonic_ns()))\n"
            " return result\n"
            "hook.ack_delivered=ack\n"
            "sys.stdin=__import__('io').StringIO(json.dumps({'session_id':'sess-owner','hook_event_name':'UserPromptSubmit'}))\n"
            "raise SystemExit(hook.main())\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update({
            "FIXTURE_AGENT_HOME": str(self.root.parent),
            "FIXTURE_DISPATCH_ROOT": str(self.root),
            "AGENT_HOME": str(ROOT),
            "SLOW_RECOVERY_SCRIPT": str(fixture),
            "SLOW_RECOVERY_PID_FILE": str(child_pid_file),
            "SLOW_RECOVERY_PID": str(self.root.parent / "slow-recovery.pid"),
            "SLOW_RECOVERY_ENV": str(self.root.parent / "slow-recovery-env.json"),
            "ACK_GATE": str(gate),
            "ACK_ENTERED": str(ack_entered),
            "ACK_COMMITTED": str(self.root.parent / "ack-committed"),
        })
        started = time.monotonic()
        hook_process = subprocess.Popen(
            [sys.executable, str(harness)], env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )
        child_pid = None
        try:
            fd = hook_process.stdout.fileno()
            collected = bytearray()
            deadline = started + 5
            while b"\n" not in collected and time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], 0.1)
                if ready:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    collected.extend(chunk)
            self.assertIn(b"\n", collected, "hook output did not reach the pipe promptly")
            output_at = time.monotonic()
            output_line = bytes(collected).split(b"\n", 1)[0] + b"\n"
            output_payload = json.loads(output_line)
            self.assertEqual(output_payload["hookSpecificOutput"]["hookEventName"],
                             "UserPromptSubmit")
            self.assertIn("att-0000000000000000000000000000bbbb",
                          output_payload["hookSpecificOutput"]["additionalContext"])
            # Flush can wake this reader before the hook enters ACK.
            # The fixture delay exercises that scheduler boundary explicitly.
            while not ack_entered.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(ack_entered.exists(), "hook did not reach its ACK call")
            self.assertEqual(PD.read(self.root, "sess-owner", "delivery-" + "a" * 32)["state"],
                             "claimed", "ACK state changed before the pipe reader received output")
            gate.touch()
            deadline = min(time.monotonic() + 5, started + 10.0)
            while not child_pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(child_pid_file.exists(), "recovery request did not follow the ACK")
            child_pid = int(child_pid_file.read_text())
            tail, err = hook_process.communicate(
                timeout=min(8.0, max(0.001, started + 10.0 - time.monotonic())))
            hook_exit_at = time.monotonic()
            elapsed = hook_exit_at - started
            self.assertEqual(hook_process.returncode, 0, err.decode("utf-8", "replace"))
            self.assertLess(elapsed, 10.0, f"output, ACK, and hook exit took {elapsed:.3f}s")
            ack_at = int(Path(env["ACK_COMMITTED"]).read_text()) / 1_000_000_000
            self.assertLess(output_at, ack_at, "ACK committed before pipe output arrived")
            self.assertLess(ack_at, hook_exit_at, "hook exited before the exact ACK committed")
            self.assertLess(output_at - started, 10.0)
            self.assertLess(ack_at - started, 10.0)
            self.assertLess(hook_exit_at - started, 10.0)
            self.assertEqual(PD.read(self.root, "sess-owner", "delivery-" + "a" * 32)["state"],
                             "acked")
            self.assertTrue(tail == b"" or tail.endswith(b"\n"))
            self.assertTrue(self._pid_alive(child_pid), "slow recovery did not outlive hook exit")
            # Detached startup is independent of the completed hook's latency.
            # Atomic publication makes existence a complete-payload signal.
            child_env_path = self.root.parent / "slow-recovery-env.json"
            deadline = time.monotonic() + 5
            while not child_env_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(child_env_path.exists(), "slow recovery did not publish its environment")
            child_env = json.loads(child_env_path.read_text())
            self.assertEqual(child_env, {
                "AGENT_DISPATCH_JOBS": env["AGENT_DISPATCH_JOBS"],
                "HARNESS_STATE_ROOT": env["HARNESS_STATE_ROOT"],
                "AGENT_PEER_LEDGER_ROOT": env["AGENT_PEER_LEDGER_ROOT"],
                "XDG_STATE_HOME": env["XDG_STATE_HOME"],
                "AGENT_HOME": env["AGENT_HOME"],
                "FIXTURE_LIFECYCLE_ROOT": str(ROOT),
            })
        finally:
            if hook_process.poll() is None:
                hook_process.kill()
                hook_process.wait(timeout=3)
            for stream in (hook_process.stdout, hook_process.stderr):
                stream.close()
            if child_pid is None and child_pid_file.exists():
                child_pid = int(child_pid_file.read_text())
            if child_pid is not None:
                try:
                    os.kill(child_pid, 15)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 2
                while self._pid_alive(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.02)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_reconnect_is_requested_without_a_delivery_projection(self):
        self.root.mkdir(parents=True, exist_ok=True)
        output = io.StringIO()
        with mock.patch.object(HOOK, "resolve_agent_home", return_value=self.root.parent), \
             mock.patch.object(HOOK, "dispatch_state_roots", return_value=(self.root,)), \
             mock.patch.object(HOOK, "sweep_deliver", return_value=([], 0)), \
             mock.patch.object(HOOK, "request_obligation_reconciliation") as request, \
             mock.patch("sys.stdin", io.StringIO(json.dumps({
                 "session_id": "worker-session", "hook_event_name": "UserPromptSubmit",
             }))), contextlib.redirect_stdout(output):
            self.assertEqual(HOOK.main(), 0)
        request.assert_called_once_with(lifecycle_root=ROOT)
        self.assertEqual(output.getvalue(), "")

    def test_startup_and_resume_context_never_ack_or_take_a_delivery_lease(self):
        self._seed()
        before = PD.read(self.root, "sess-owner", "delivery-" + "a" * 32)
        for source in ("startup", "resume"):
            output = io.StringIO()
            with mock.patch.object(HOOK, "resolve_agent_home", return_value=self.root.parent), \
                 mock.patch.object(HOOK, "dispatch_state_roots", return_value=(self.root,)), \
                 mock.patch.object(HOOK, "request_obligation_reconciliation"), \
                 mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": "sess-owner",
                     "hook_event_name": "SessionStart", "source": source}))), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(HOOK.main(), 0)
            self.assertIn("att-0000000000000000000000000000bbbb", output.getvalue())
            self.assertEqual(PD.read(self.root, "sess-owner", before["delivery_id"]), before)
        self.test_hook_injects_additional_context_and_acks()

    def test_all_carriers_activate_without_claiming_and_reconnect_exact_attempt_only(self):
        import peer_obligations
        for kind in ("claude-parent-runtime", "codex-native-queue", "opencode-turn"):
            with self.subTest(kind=kind):
                delivery = "delivery-" + {"claude-parent-runtime": "a", "codex-native-queue": "b",
                                          "opencode-turn": "c"}[kind] * 32
                self._seed(recipient_kind=kind, delivery_id=delivery)
                before = PD.read(self.root, "sess-owner", delivery)
                with mock.patch.object(peer_obligations, "retain_registered_completion") as retain:
                    records = SWEEP.activate(self.root, kind, "sess-owner")
                    self.assertEqual(len(records), 1)
                    retain.assert_called_once_with(self.root / "jobs.log",
                        "att-0000000000000000000000000000bbbb", "sess-owner", kind)
                    self.assertEqual(SWEEP.activate(self.root, kind, "other-session"), [])
                    self.assertEqual(retain.call_count, 1)
                self.assertEqual(PD.read(self.root, "sess-owner", delivery), before)

    def test_activation_preserves_an_explicit_registry_filename(self):
        import peer_obligations
        self._seed()
        jobs = self.root / "route-jobs.tsv"
        (self.root / "jobs.log").rename(jobs)
        path = PD.record_path(self.root, "sess-owner", "delivery-" + "a" * 32)
        record = json.loads(path.read_text())
        record["receipt"]["job_registry"] = str(jobs)
        path.write_text(json.dumps(record))
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}), \
                mock.patch.object(peer_obligations, "retain_registered_completion") as retain:
            SWEEP.activate(self.root, "claude-parent-runtime", "sess-owner")
            self.assertEqual(retain.call_args.args[0], jobs)
            self.assertEqual(SWEEP.record_jobs(self.root, {"receipt": {"kind": "human-gate"}}), jobs)
            self.assertIn(str(jobs), SWEEP.delivery_context([(self.root, [record])]))

    def test_codex_session_start_reconnects_without_acknowledging_context(self):
        self._seed(recipient_kind="codex-native-queue")
        bridge = _load("activation_codex_bridge", ROOT / "adapters/codex/hooks/sessionstart-lifecycle.py")
        import dispatch_contract, peer_obligations
        before = PD.read(self.root, "sess-owner", "delivery-" + "a" * 32)
        out = io.StringIO()
        with mock.patch.object(bridge, "load_payload", return_value={"session_id": "sess-owner",
                "source": "resume"}), mock.patch.object(bridge, "is_worker_session", return_value=False), \
                mock.patch.object(bridge, "forget_shown_candidates"), \
                mock.patch.object(bridge, "card_context", return_value=("", "")), \
                mock.patch.object(bridge, "local_evidence_context", return_value=""), \
                mock.patch.object(dispatch_contract, "dispatch_state_roots", return_value=[self.root]), \
                mock.patch.object(dispatch_contract, "resolve_agent_home", return_value=ROOT), \
                mock.patch.object(peer_obligations, "retain_registered_completion") as retain, \
                mock.patch.object(bridge, "request_lifecycle_recovery") as request, \
                mock.patch.dict(sys.modules, {"herdr_session_projection": SimpleNamespace(project=lambda *a, **k: None)}), \
                mock.patch.object(bridge.sys, "stdout", out):
            self.assertEqual(bridge.main(), 0)
        self.assertIn("att-0000000000000000000000000000bbbb", out.getvalue())
        retain.assert_called_once()
        request.assert_called_once_with()
        self.assertEqual(PD.read(self.root, "sess-owner", before["delivery_id"]), before)

    # -- A-21: generation-unproven claim refused, state unchanged. ---------

    def test_a21_sentinel_record_refused_generation_unproven_state_unchanged(self):
        self._seed("sess-owner")
        outcome, count = SWEEP.sweep(
            self.root, "claude-parent-runtime", "sess-owner", "unsupported"
        )
        self.assertEqual((outcome, count), ("refused", 1))
        record = PD.read(self.root, "sess-owner", "delivery-" + "a" * 32)
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["attempts"], 0)
        self.assertIsNone(record["claim_owner"])

    def test_a21_refusal_reason_is_generation_fence_not_absence(self):
        # Distinguishes carrier 2's refusal (generation fence, this test)
        # from carrier 1's refusal (incarnation-binding mismatch, covered in
        # hooks/dispatch_owner_rewake.test.py) per round 2 C-3.
        self._seed("sess-owner")
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            PD.claim(
                self.root, "sess-owner", "delivery-" + "a" * 32,
                claim_owner="probe", lease_seconds=30.0,
                require_generation_proof=True,
            )
        self.assertEqual(ctx.exception.reason, "pending-delivery-generation-unproven")

    # -- A47-8: claim_authority is per-claim, not sticky across a reclaim. -

    def test_a47_8_carrier_timeout_keeps_pending_then_sweep_acks(self):
        # A carrier-2 (sweep_deliver) claim that times out before ack is a
        # deliverer-unproven claim that never got consumed -- the next
        # sweep_deliver pass reclaims it back to pending (§13.34.1-(2): a
        # timed-out claim is not a stuck grade). A record whose session
        # later proves generation (session_generation_supported="1", unlike
        # A-21's real-world unsupported fixtures above) can then be claimed
        # and acked by carrier 1 (sweep) as generation-proven -- the grade
        # recorded is whichever claim actually won, not whatever claimed it
        # first.
        self._seed("sess-owner", session_generation_supported="1")
        delivery_id = "delivery-" + "a" * 32
        first, _ = SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-owner")
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["claim_authority"], "deliverer-unproven")
        # Timeout: never acked. Lease expires; reclaim releases it to pending.
        PD.reclaim(
            self.root, "sess-owner", delivery_id, now_ns=first[0]["claim_deadline_ns"] + 1,
        )
        released = PD.read(self.root, "sess-owner", delivery_id)
        self.assertEqual(released["state"], "pending")
        outcome, count = SWEEP.sweep(self.root, "claude-parent-runtime", "sess-owner", "proven")
        self.assertEqual((outcome, count), ("claimed", 1))
        claimed = PD.read(self.root, "sess-owner", delivery_id)
        self.assertEqual(claimed["state"], "claimed")
        self.assertEqual(claimed["claim_authority"], "generation-proven")
        acked = PD.ack(self.root, "sess-owner", delivery_id, acked_by="carrier-1")
        self.assertEqual(acked["state"], "acked")

    # -- A-11(i): different session_id -> different digest -> dir absent. --

    def test_a11_foreign_session_reads_zero_claims_zero(self):
        self._seed("sess-owner")
        outcome, count = SWEEP.sweep(
            self.root, "claude-parent-runtime", "sess-foreign", "unsupported"
        )
        self.assertEqual((outcome, count), ("refused", 0))
        self.assertFalse(
            PD.record_directory(self.root, "sess-foreign").is_dir()
        )

    # -- A-11(ii)/A-21 second incarnation: carrier 2 has no process binding, -
    # -- so a second incarnation of the *same* session_id is indistinguish- -
    # -- able from the first by digest; the generation fence refuses both. -

    def test_second_incarnation_same_session_id_still_refused_by_fence(self):
        self._seed("sess-owner")
        first = SWEEP.sweep(self.root, "claude-parent-runtime", "sess-owner", "unsupported")
        second = SWEEP.sweep(self.root, "claude-parent-runtime", "sess-owner", "unsupported")
        self.assertEqual(first, ("refused", 1))
        self.assertEqual(second, ("refused", 1))
        record = PD.read(self.root, "sess-owner", "delivery-" + "a" * 32)
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["attempts"], 0)

    # -- empty / absent directory: no entries, no exception. ---------------

    def test_no_records_for_session_returns_refused_zero(self):
        outcome, count = SWEEP.sweep(
            self.root, "claude-parent-runtime", "sess-empty", "unsupported"
        )
        self.assertEqual((outcome, count), ("refused", 0))

    # -- fail-open: an unreadable directory must not raise. -----------------

    def test_unreadable_directory_fails_open(self):
        self._seed("sess-locked")
        directory = PD.record_directory(self.root, "sess-locked")
        original_mode = directory.stat().st_mode
        try:
            os.chmod(directory, 0)
            if os.access(directory, os.R_OK):
                self.skipTest("running as a user that bypasses directory permissions")
            outcome, count = SWEEP.sweep(
                self.root, "claude-parent-runtime", "sess-locked", "unsupported"
            )
            self.assertEqual((outcome, count), ("refused", 0))
        finally:
            os.chmod(directory, original_mode)

    # -- self-instrumentation: observation only, never a gate. -------------

    def test_sweep_never_materializes_a_missing_root(self):
        # Regression: the legacy <agent-home>/.dispatch read-order root does
        # not exist for a packaged release; sweeping it must not create
        # `<release>/.dispatch/logs/` (that write made every superseded
        # release refuse pruning with delta-digest-mismatch).
        missing = Path(self._tmp.name) / "release" / ".dispatch"
        records, entries = SWEEP.sweep_deliver(missing, "claude-parent-runtime", "sess-missing")
        self.assertEqual((records, entries), ([], 0))
        self.assertFalse(missing.exists())
        self.assertFalse((Path(self._tmp.name) / "release").exists())

    def test_self_instrumentation_appends_one_line_per_sweep(self):
        self._seed("sess-owner")
        log_path = self.root / "logs" / SWEEP.LOG_FILENAME
        self.assertFalse(log_path.exists())
        SWEEP.sweep(self.root, "claude-parent-runtime", "sess-owner", "unsupported")
        self.assertTrue(log_path.is_file())
        lines = log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(set(payload), {"ts_ns", "elapsed_ns", "entries", "claimed"})
        self.assertEqual(payload["entries"], 1)
        self.assertEqual(payload["claimed"], 0)
        SWEEP.sweep(self.root, "claude-parent-runtime", "sess-owner", "unsupported")
        self.assertEqual(
            len(log_path.read_text(encoding="utf-8").splitlines()), 2
        )

class StrictGateReceiptRenderTest(unittest.TestCase):
    """The strict gate receipt a Codex owner raises is shown as a gate, with what to read."""

    def test_a_strict_gate_receipt_is_a_gate_notice_with_its_artifact(self):
        import dispatch_session_sweep as sweep
        record = {"delivery_id": "delivery-x", "receipt": {
            "kind": "human-gate", "route_id": "rt-0123456789abcdef", "route_file": "/r/route.json",
            "owner_attempt_id": "att-owner", "gate": "full-run-authorization",
            "artifact_path": "/art/gate.md"}}
        self.assertTrue(sweep.is_human_gate_record(record))
        text = sweep.delivery_context([(Path("/state"), [record])])
        self.assertTrue(text.startswith(sweep.GATE_DELIVERY_HEADER))
        self.assertIn("gate=full-run-authorization artifact=/art/gate.md", text)
        self.assertIn("route_file=/r/route.json", text)


class OpenCodeTurnCarrierTest(IsolatedRootMixin, unittest.TestCase):
    """The OpenCode plugin carrier: look, claim and render through the shared sweep, hand the text
    to the idle session's next turn, then ack (taken) or release (not taken)."""

    SID = "ses-oc-parent"
    _seed = SweepTest._seed

    def env(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "jobs.log").touch()
        env = dict(os.environ)
        env.update(HARNESS_STATE_ROOT=str(self.root), AGENT_DISPATCH_JOBS=str(self.root / "jobs.log"),
                   AGENT_HOME=str(ROOT))
        for key in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                    "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH"):
            env.pop(key, None)
        return env

    def seed_turn(self, **overrides):
        return self._seed(self.SID, recipient_kind="opencode-turn", **overrides)

    def seed_cycle_batch(self, *, materialize_second=True, member_count=2):
        from datetime import datetime, timezone
        from route_identity import route_hash
        serial = getattr(self, "_batch_serial", 0) + 1
        self._batch_serial = serial
        jobs = self.root / "jobs.log"
        jobs.parent.mkdir(parents=True, exist_ok=True)
        jobs.touch(exist_ok=True)
        route_path = self.root / f"batch-route-{serial}.json"
        route = {"route_id": f"rt-opencode-batch-{serial}",
                 "parent_cycle_id": f"cycle-opencode-batch-{serial}",
                 "nodes": [{"id": "frame"}]}
        route["route_hash"] = route_hash(route)
        route_path.write_text(json.dumps(route), encoding="utf-8")
        self._last_batch_route = (serial, route_path, route, jobs)
        ids = [f"delivery-{serial:08x}{index:024x}" for index in range(member_count)]
        attempts = [f"att-opencode-{serial}-{index}" for index in range(member_count)]
        for index, (delivery_id, attempt_id) in enumerate(zip(ids, attempts)):
            receipt = _receipt(
                state="ready", parent_attempt_id="att-opencode-parent",
                job_registry=str(jobs), delivery_classification="success",
                children=[{"attempt_id": attempt_id, "status": "done", "readiness": "ready",
                           "required_action": "advance-completed", "delivery_classification": "success"}])
            if index == 0 or materialize_second:
                self.seed_turn(delivery_id=delivery_id, attempt_ids=[attempt_id],
                               parent_attempt_id="att-opencode-parent", route_id=route["route_id"],
                               route_node="frame", receipt=receipt,
                               row_revisions={attempt_id: f"revision-{index}"})
            metadata = {"attempt_id": attempt_id, "dispatch_depth": "1", "registered_worker": "1",
                        "worker_type": "frame", "parent_sid": self.SID, "delivery_intent": "1",
                        "delivery_id": delivery_id, "failure_class": "pass", "note": "completed-marker",
                        "delivery_intent_at_ns": str(time.monotonic_ns()),
                        "launch_outcome": "reaped-before-publish",
                        "route_file": str(route_path), "route_id": route["route_id"],
                        "route_hash": route["route_hash"], "route_node": "frame"}
            stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            with jobs.open("a", encoding="utf-8") as handle:
                handle.write(stamp + "\tdone\t/r\t/w\ttask\t" +
                             ",".join(f"{key}={value}" for key, value in metadata.items()) + "\n")
        return ids

    def seed_cycle_failure(self):
        from datetime import datetime, timezone
        serial, route_path, route, jobs = self._last_batch_route
        delivery_id = f"delivery-{serial:08x}{99:024x}"
        attempt_id = f"att-opencode-{serial}-failure"
        receipt = _receipt(
            state="attention", parent_attempt_id="att-opencode-parent",
            job_registry=str(jobs), delivery_classification="attention",
            children=[{"attempt_id": attempt_id, "status": "done", "readiness": "ready",
                       "required_action": "inspect-done-failure",
                       "delivery_classification": "attention"}])
        self.seed_turn(delivery_id=delivery_id, attempt_ids=[attempt_id],
                       parent_attempt_id="att-opencode-parent", route_id=route["route_id"],
                       route_node="frame", receipt=receipt,
                       row_revisions={attempt_id: "failure-revision"})
        metadata = {"attempt_id": attempt_id, "dispatch_depth": "1", "registered_worker": "1",
                    "worker_type": "frame", "parent_sid": self.SID, "delivery_intent": "1",
                    "delivery_id": delivery_id, "failure_class": "fail", "note": "dead-worker-fail",
                    "delivery_intent_at_ns": str(time.monotonic_ns()),
                    "launch_outcome": "reaped-before-publish", "route_file": str(route_path),
                    "route_id": route["route_id"], "route_hash": route["route_hash"],
                    "route_node": "frame"}
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with jobs.open("a", encoding="utf-8") as handle:
            handle.write(stamp + "\tdone\t/r\t/w\ttask\t" +
                         ",".join(f"{key}={value}" for key, value in metadata.items()) + "\n")
        return delivery_id

    def state(self):
        return PD.read(self.root, self.SID, "delivery-" + "a" * 32)["state"]

    def _mark_sent_ambiguous(self, delivery_id):
        owner = "prior-carrier-" + delivery_id[-1]
        PD.claim(self.root, self.SID, delivery_id, claim_owner=owner,
                 lease_seconds=30, require_generation_proof=False)
        return PD.mark_sent_ambiguous(
            self.root, self.SID, delivery_id, claim_owner=owner)

    def _expire_ambiguous(self, delivery_ids):
        for delivery_id in delivery_ids:
            record = PD.read(self.root, self.SID, delivery_id)
            PD.reclaim(self.root, self.SID, delivery_id,
                       now_ns=record["claim_deadline_ns"] + 1)

    def cli(self, action, stdin=""):
        proc = subprocess.run([sys.executable, str(HERE / "dispatch_session_sweep.py"), action,
                               "--recipient-kind", "opencode-turn", "--session", self.SID],
                              input=stdin, capture_output=True, text=True, env=self.env(), check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def settle_at(self, handed, now_ns, messages=None):
        output = io.StringIO()
        payload = {**handed, "messages": messages or []}
        with mock.patch.object(SWEEP, "_state_roots", return_value=[self.root]), \
                mock.patch.object(SWEEP.time, "monotonic_ns", return_value=now_ns), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(output):
            self.assertEqual(SWEEP.main([
                "settle", "--recipient-kind", "opencode-turn", "--session", self.SID,
            ]), 0)
        return json.loads(output.getvalue())

    def test_the_sweep_steps_claim_render_and_settle_one_record(self):
        self.seed_turn()
        roots = self.cli("roots")
        self.assertIn(str(self.root), roots)
        owed = self.cli("deliver")
        self.assertEqual([item["delivery_id"] for item in owed["records"]], ["delivery-" + "a" * 32])
        self.assertIn("att-0000000000000000000000000000bbbb", owed["text"])
        self.assertIn("Follow required_action", owed["text"])
        self.assertEqual(self.state(), "claimed")
        self.assertEqual(self.cli("deliver")["records"], [])      # a live claim is not handed out twice
        self.assertEqual(self.cli("release", json.dumps(owed))["count"], 1)
        self.assertEqual(self.state(), "pending")
        again = self.cli("deliver")
        self.assertEqual(self.cli("ack", json.dumps(again))["count"], 1)
        self.assertEqual(self.state(), "acked")

    def test_opencode_batch_hold_ready_and_already_received_prompt_counts(self):
        held_ids = self.seed_cycle_batch(materialize_second=False, member_count=24)
        held = self.run_plugin(self.IDLE_THEN_WAIT)
        self.assertEqual(held["prompts"], [])
        self.assertTrue(all(PD.read(self.root, self.SID, did)["state"] == "pending"
                            for did in held_ids[:1]))
        failure_id = self.seed_cycle_failure()
        immediate_failure = self.run_plugin(self.IDLE_THEN_WAIT)
        self.assertEqual(len(immediate_failure["prompts"]), 1)
        failure_text = immediate_failure["prompts"][0]["body"]["parts"][0]["text"]
        self.assertIn(failure_id, failure_text)
        self.assertNotIn(held_ids[0], failure_text)
        self.assertEqual(PD.read(self.root, self.SID, failure_id)["state"], "acked")
        self.assertEqual(PD.read(self.root, self.SID, held_ids[0])["state"], "pending")

        ready_ids = self.seed_cycle_batch(materialize_second=True, member_count=24)
        ready = self.run_plugin(self.IDLE_THEN_WAIT)
        self.assertEqual(len(ready["prompts"]), 1, json.dumps(ready["logs"], sort_keys=True))
        ready_text = ready["prompts"][0]["body"]["parts"][0]["text"]
        self.assertEqual(ready_text.count("HEARTING_RECEIPT_V1"), 24)
        print(f"batch24 emissions=1 wakes={len(ready['prompts'])} chars={len(ready_text)}")
        self.assertTrue(all(PD.read(self.root, self.SID, did)["state"] == "acked"
                            for did in ready_ids))

        received_ids = self.seed_cycle_batch(materialize_second=True, member_count=24)
        received_records = []
        for did in received_ids:
            claimed = PD.claim(self.root, self.SID, did, claim_owner="prior-opencode",
                               lease_seconds=30, require_generation_proof=False)
            received_records.append(PD.mark_sent_ambiguous(
                self.root, self.SID, did, claim_owner="prior-opencode"))
        native_body = SWEEP.delivery_context([(self.root, [
            {**record, SWEEP.STORAGE_KEY: self.SID} for record in received_records])])
        already_received = self.run_plugin(
            self.IDLE_THEN_WAIT,
            native_messages=[{"info": {"sessionID": self.SID, "role": "user"},
                              "parts": [{"type": "text", "text": native_body}]}])
        self.assertEqual(already_received["prompts"], [])
        self.assertTrue(all(PD.read(self.root, self.SID, did)["state"] == "acked"
                            for did in received_ids))

    def test_batch_delivery_bounds_process_walks_and_batch_resolutions(self):
        ready_ids = self.seed_cycle_batch(materialize_second=True, member_count=24)
        with mock.patch("dispatch_contract.attempt_process_quiescence",
                        wraps=__import__("dispatch_contract").attempt_process_quiescence) as walks, \
             mock.patch("dispatch_notice_state.resolve_delivery_batch",
                        wraps=__import__("dispatch_notice_state").resolve_delivery_batch) as resolves:
            records, _ = SWEEP.sweep_deliver(self.root, "opencode-turn", self.SID)
        self.assertEqual({record["delivery_id"] for record in records}, set(ready_ids))
        self.assertLessEqual(walks.call_count, 1)
        self.assertLessEqual(resolves.call_count, 1)

        held_ids = self.seed_cycle_batch(materialize_second=True, member_count=24)
        jobs = self.root / "jobs.log"
        lines = jobs.read_text(encoding="utf-8").splitlines()
        changed = []
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if "att-opencode-2-" in fields[5]:
                attempt_id = fields[5].split("attempt_id=", 1)[1].split(",", 1)[0]
                if int(attempt_id.rsplit("-", 1)[1]) < 23:
                    fields[1] = "running"
            changed.append("\t".join(fields))
        jobs.write_text("\n".join(changed) + "\n", encoding="utf-8")
        with mock.patch("dispatch_contract.attempt_process_quiescence",
                        return_value=SimpleNamespace(state="live")) as walks:
            held_records, _ = SWEEP.sweep_deliver(self.root, "opencode-turn", self.SID)
        self.assertEqual(held_records, [])
        self.assertEqual(walks.call_count, 1)
        self.assertTrue(all(PD.read(self.root, self.SID, did)["state"] == "pending"
                            for did in held_ids))

    def test_every_runtime_renders_delivered_records_with_one_text(self):
        self.seed_turn()
        records, _ = SWEEP.sweep_deliver(self.root, "opencode-turn", self.SID)
        text = SWEEP.delivery_context([(self.root, records)])
        self.assertTrue(text.startswith(SWEEP.COMPLETION_DELIVERY_HEADER))
        self.assertIn("harvest", text)      # the exact next handle the queue carrier also sends
        gate = _receipt(children=[{**_receipt()["children"][0], "required_action": "human-gate:frame-review",
                                   "reason": "/tmp/frame-summary.json"}])
        gate_record = {"delivery_id": "delivery-gate", "receipt": gate}
        text = SWEEP.delivery_context([(self.root, [gate_record])])
        self.assertTrue(text.startswith(SWEEP.GATE_DELIVERY_HEADER))
        self.assertIn("gate=frame-review", text)
        self.assertEqual(SWEEP.delivery_context([]), "")
        for hook in (ROOT / "hooks" / "dispatch-session-sweep.py",
                     ROOT / "adapters" / "codex" / "hooks" / "userprompt-lifecycle.py"):
            self.assertIn("delivery_context", hook.read_text(encoding="utf-8"))

    def run_plugin(self, body, *, prompt_async="accept", restored_selector=False,
                   extra_env=None, native_messages=None, claim_change=False, claim_expire=False,
                   claim_receipt=False, late_native_text=None):
        plugin = ROOT / "adapters/opencode/plugins/hearting-guards.js"
        if restored_selector:
            fixture = self.root.parent / "restored-plugin/adapters/opencode/plugins/hearting-guards.js"
            fixture.parent.mkdir(parents=True, exist_ok=True)
            source = plugin.read_text().replace("let paneNativeOrigin //", 'let paneNativeOrigin = '
                + json.dumps({"pid": 123, "start": "456", "sid": self.SID,
                              "directory": str(self.root), "state": "pending"}) + ' //')
            fixture.write_text(source)
            plugin = fixture
        js = r'''
import { pathToFileURL } from "node:url";
import { writeFileSync } from "node:fs";
import childProcess from "node:child_process";
const { AgentHarnessGuards } = await import(pathToFileURL(PLUGIN));
const prompts = [];
const logs = [];
const nativeMessages = NATIVE_MESSAGES;
let nativeReads = 0;
const session = {
  messages: async () => {
    nativeReads++;
    const claimMutation = process.env.CLAIM_CHANGE_PY || process.env.CLAIM_EXPIRE_PY;
    if (claimMutation && nativeReads === 2) {
      const changed = childProcess.spawnSync("python3", ["-c", claimMutation],
        {encoding: "utf8"});
      if (changed.status !== 0) throw new Error(changed.stderr || "claim change failed");
      if (process.env.CLAIM_RECEIPT === "1") nativeMessages.push({
        info: {sessionID: "ses-oc-parent", role: "user"},
        parts: [{type: "text", text: process.env.EXPECTED_NATIVE_TEXT}],
      });
    }
    if (nativeReads === 2 && process.env.LATE_NATIVE_TEXT) nativeMessages.push({
      info: {sessionID: "ses-oc-parent", role: "user"},
      parts: [{type: "text", text: process.env.LATE_NATIVE_TEXT}],
    });
    return {data: nativeMessages};
  }, prompt: async () => ({data: null}),
  promptAsync: async (request) => {
    prompts.push(request);
    if (MODE === "accept" || MODE === "timeout-native") nativeMessages.push({
      info: {sessionID: "ses-oc-parent", role: "user"}, parts: request.body.parts,
    });
    if (MODE.startsWith("timeout")) throw new Error("carrier-timeout");
    return MODE === "accept" ? {data: undefined, response: {ok: true, status: 204}} : {error: {name: "NotFound"}, response: {ok: false, status: 404}};
  },
};
if (MODE === "absent") delete session.promptAsync;
process.env.HERDR_PANE_ID = "";
const hooks = await AgentHarnessGuards({client: {app: {log: async (entry) => { logs.push(entry.body); }}, session}, directory: process.cwd()});
const settle = async () => { for (let i = 0; i < 100 && !globalThis.done; i++) await new Promise(r => setTimeout(r, 50)); };
BODY
hooks.dispose();
console.log(JSON.stringify({prompts, logs}));
'''.replace("MODE", json.dumps(prompt_async)).replace("BODY", body).replace("PLUGIN", json.dumps(str(plugin))).replace(
            "NATIVE_MESSAGES", json.dumps(native_messages or []))
        env = self.env()
        if late_native_text is not None:
            env["LATE_NATIVE_TEXT"] = late_native_text
        if claim_change or claim_expire:
            record = {**PD.read(self.root, self.SID, "delivery-" + "a" * 32),
                      SWEEP.STORAGE_KEY: self.SID}
            record["attempts"] += 1  # deliver claims before rendering the native body.
            native_body = SWEEP.delivery_context([(self.root, [record])])
            env.update(CLAIM_RECEIPT="1" if claim_receipt else "0",
                       EXPECTED_NATIVE_TEXT=native_body)
        if claim_change:
            env["CLAIM_CHANGE_PY"] = (
                    "import os,sys; from pathlib import Path; "
                    "sys.path.insert(0, str(Path(os.environ['AGENT_HOME'])/'utilities')); "
                    "import dispatch_pending_delivery as p; "
                    "root=Path(os.environ['HARNESS_STATE_ROOT']); sid='ses-oc-parent'; "
                    "did='delivery-'+'a'*32; row=p.read(root,sid,did); "
                    "p.reclaim(root,sid,did,now_ns=row['claim_deadline_ns']+1); "
                    "p.claim(root,sid,did,claim_owner='replacement-carrier',lease_seconds=30,"
                    "require_generation_proof=False)")
        if claim_expire:
            env["CLAIM_EXPIRE_PY"] = (
                "import json,os,sys,time; from pathlib import Path; "
                "sys.path.insert(0, str(Path(os.environ['AGENT_HOME'])/'utilities')); "
                "import dispatch_pending_delivery as p; "
                "root=Path(os.environ['HARNESS_STATE_ROOT']); sid='ses-oc-parent'; "
                "did='delivery-'+'a'*32; row=p.read(root,sid,did); "
                "owner=row['claim_owner']; identity=p.received_ack_identity(row,sid,did); "
                "path=p.record_path(root,sid,did); raw=json.loads(path.read_text(encoding='utf-8')); "
                "raw['claim_deadline_ns']=time.monotonic_ns()-1; "
                "path.write_text(json.dumps(raw),encoding='utf-8'); "
                "after=p.read(root,sid,did); "
                "assert after['claim_owner']==owner and "
                "p.received_ack_identity(after,sid,did)==identity")
        if extra_env:
            env.update(extra_env)
        run = subprocess.run(["node", "--input-type=module", "-e", js], env=env, cwd=str(self.root),
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout.strip().splitlines()[-1])

    IDLE_THEN_WAIT = r'''
const out = {env: {}};
await hooks["shell.env"]({sessionID: "ses-oc-parent"}, out);
globalThis.carrierEnv = out.env.AGENT_PARENT_COMPLETION_CARRIER;
await hooks.event({event: {type: "session.idle", properties: {sessionID: "ses-oc-parent"}}});
for (let i = 0; i < 200; i++) {
  await new Promise(r => setTimeout(r, 50));
  if (prompts.length || logs.some(row => row.service === "hearting-completion-carrier"
      && row.extra?.stage === "settle" && row.extra?.reason === "native-receipt-before-send")) break;
}
if (prompts.length) {
  for (let i = 0; i < 200; i++) {
    if (logs.some(row => row.service === "hearting-completion-carrier"
        && row.extra?.stage === "settle"
        && ["native-receipt", "release-fallback"].includes(row.extra?.reason))) break;
    await new Promise(r => setTimeout(r, 50));
  }
}
console.log(JSON.stringify({carrierEnv: globalThis.carrierEnv}));
'''

    def test_an_idle_parent_gets_its_record_as_its_next_turn_and_the_record_is_acked(self):
        self.seed_turn()
        result = self.run_plugin(self.IDLE_THEN_WAIT)
        self.assertEqual(len(result["prompts"]), 1)
        request = result["prompts"][0]
        self.assertEqual(request["path"], {"id": self.SID})
        self.assertNotIn("noReply", request["body"])      # a turn, not a silent insert
        self.assertIn("att-0000000000000000000000000000bbbb", request["body"]["parts"][0]["text"])
        self.assertEqual(self.state(), "acked")
        carrier = [item["extra"] for item in result["logs"] if item["service"] == "hearting-completion-carrier"]
        self.assertIn(("claim", "claimed"), [(item["stage"], item["reason"]) for item in carrier])
        self.assertIn(("prompt", "accepted"), [(item["stage"], item["reason"]) for item in carrier])
        self.assertTrue(all("text" not in item for item in carrier))

    def test_native_receipt_from_another_carrier_suppresses_prompt_async(self):
        import dispatch_recipient_receipt as recipient_receipt
        self.seed_turn()
        did = "delivery-" + "a" * 32
        PD.claim(self.root, self.SID, did, claim_owner="other-carrier", lease_seconds=30,
                 require_generation_proof=False)
        PD.mark_sent_ambiguous(self.root, self.SID, did, claim_owner="other-carrier")
        current = PD.read(self.root, self.SID, did)
        marker = recipient_receipt.receipt_marker(current, self.SID)
        carrier_body = SWEEP.delivery_context([
            (self.root, [{**current, SWEEP.STORAGE_KEY: self.SID}])])
        self.assertIn("\n" + marker, carrier_body)
        messages = [{"info": {"sessionID": self.SID, "role": "user"},
                     "parts": [{"type": "text", "text": carrier_body}]}]
        result = self.run_plugin(self.IDLE_THEN_WAIT, native_messages=messages)
        self.assertEqual(result["prompts"], [])
        self.assertEqual(self.state(), "acked")
        self.assertEqual(PD.read(self.root, self.SID, did)["attempts"], 1)

    def test_claim_change_between_deliver_and_settle_without_receipt_never_sends(self):
        self.seed_turn()
        result = self.run_plugin(self.IDLE_THEN_WAIT, claim_change=True)
        self.assertEqual(result["prompts"], [])
        current = PD.read(self.root, self.SID, "delivery-" + "a" * 32)
        self.assertEqual(current["state"], "claimed")
        self.assertEqual(current["claim_owner"], "replacement-carrier")
        self.assertEqual(self.state(), "claimed")

    def test_exact_native_receipt_settles_after_claim_owner_changes(self):
        self.seed_turn()
        result = self.run_plugin(self.IDLE_THEN_WAIT, claim_change=True, claim_receipt=True)
        self.assertEqual(result["prompts"], [])
        self.assertEqual(self.state(), "acked")

    def test_expired_original_claim_does_not_send_and_remains_reclaimable(self):
        self.seed_turn()
        stale = self.run_plugin(self.IDLE_THEN_WAIT, claim_expire=True)
        self.assertEqual(stale["prompts"], [])
        record = PD.read(self.root, self.SID, "delivery-" + "a" * 32)
        self.assertEqual(record["state"], "claimed")
        self.assertLessEqual(record["claim_deadline_ns"], time.monotonic_ns())
        self.assertTrue(any(item.get("extra", {}).get("reason") == "native-receipt-before-send"
                            for item in stale["logs"]))

        fallback = self.run_plugin(self.IDLE_THEN_WAIT)
        self.assertEqual(len(fallback["prompts"]), 1)
        self.assertEqual(self.state(), "acked")

    def test_exact_native_receipt_settles_after_claim_deadline_expires(self):
        self.seed_turn()
        result = self.run_plugin(self.IDLE_THEN_WAIT, claim_expire=True, claim_receipt=True)
        self.assertEqual(result["prompts"], [])
        self.assertEqual(self.state(), "acked")
        settled = [item["extra"] for item in result["logs"]
                   if item.get("extra", {}).get("reason") == "native-receipt-before-send"]
        self.assertTrue(any(item.get("received") == 1 and item.get("acked") == 1
                            for item in settled))

    def test_preclaim_recognizes_exact_native_body_with_historical_attempt_count(self):
        self.seed_turn()
        did = "delivery-" + "a" * 32
        first = self._mark_sent_ambiguous(did)
        first_body = SWEEP.delivery_context([
            (self.root, [{**first, SWEEP.STORAGE_KEY: self.SID}])])
        self._expire_ambiguous([did])
        second = PD.claim(self.root, self.SID, did, claim_owner="second-carrier",
                          lease_seconds=30, require_generation_proof=False)
        self.assertEqual((first["attempts"], second["attempts"]), (1, 2))
        native = [{"info": {"sessionID": self.SID, "role": "user"},
                   "parts": [{"type": "text", "text": first_body}]}]

        result = self.cli("deliver", json.dumps({"messages": native}))

        self.assertEqual(result["records"], [])
        self.assertEqual(result["text"], "")
        self.assertEqual(PD.read(self.root, self.SID, did)["state"], "acked")
        self.assertEqual(PD.read(self.root, self.SID, did)["attempts"], 2)

    def deliver_in_process(self, messages=None):
        output = io.StringIO()
        with mock.patch.object(SWEEP, "_state_roots", return_value=[self.root]), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"messages": messages or []}))), \
                contextlib.redirect_stdout(output):
            self.assertEqual(SWEEP.main([
                "deliver", "--recipient-kind", "opencode-turn", "--session", self.SID,
            ]), 0)
        return json.loads(output.getvalue())

    def test_projected_parent_close_body_survives_settle_and_its_native_receipt_settles(self):
        self.seed_turn()
        did = "delivery-" + "a" * 32
        with mock.patch("dispatch_notice_state.project_parent_close",
                        side_effect=_project_settled_close):
            delivered = self.deliver_in_process()
            self.assertEqual(len(delivered["records"]), 1)
            claimed = PD.read(self.root, self.SID, did)
            self.assertEqual(delivered["records"][0]["immutable_identity"],
                             PD.received_ack_identity(claimed, self.SID, did))
            self.assertIn("required_action=advance-completed", delivered["text"])
            self.assertNotIn("inspect-done-failure", delivered["text"])
            # No native receipt yet: the body re-rendered for the send is the same projected body.
            before_send = self.settle_at(delivered, time.monotonic_ns())
            self.assertEqual(before_send["text"], delivered["text"])
            native = [{"info": {"sessionID": self.SID, "role": "user"},
                       "parts": [{"type": "text", "text": before_send["text"]}]}]
            observed = self.settle_at(before_send, time.monotonic_ns(), messages=native)
        self.assertEqual(observed["received_ids"], [did])
        self.assertEqual(self.state(), "acked")

    def test_projected_historical_body_is_recognized_before_a_second_claim_sends(self):
        self.seed_turn()
        did = "delivery-" + "a" * 32
        with mock.patch("dispatch_notice_state.project_parent_close",
                        side_effect=_project_settled_close):
            first_body = self.deliver_in_process()["text"]
            self.assertIn("required_action=advance-completed", first_body)
            self._expire_ambiguous([did])
            PD.claim(self.root, self.SID, did, claim_owner="second-carrier",
                     lease_seconds=30, require_generation_proof=False)
            native = [{"info": {"sessionID": self.SID, "role": "user"},
                       "parts": [{"type": "text", "text": first_body}]}]
            result = self.deliver_in_process(messages=native)
        self.assertEqual(result["records"], [])
        current = PD.read(self.root, self.SID, did)
        self.assertEqual((current["state"], current["attempts"]), ("acked", 2))

    def test_late_first_claim_receipt_settles_before_second_claim_prompt_async(self):
        self.seed_turn()
        did = "delivery-" + "a" * 32
        first = self._mark_sent_ambiguous(did)
        first_body = SWEEP.delivery_context([
            (self.root, [{**first, SWEEP.STORAGE_KEY: self.SID}])])
        self._expire_ambiguous([did])

        result = self.run_plugin(self.IDLE_THEN_WAIT, late_native_text=first_body)

        self.assertEqual(result["prompts"], [])
        current = PD.read(self.root, self.SID, did)
        self.assertEqual(current["state"], "acked")
        self.assertEqual(current["attempts"], 2)
        settled = [item["extra"] for item in result["logs"]
                   if item.get("extra", {}).get("reason") == "native-receipt-before-send"]
        self.assertTrue(any(item.get("received") == 1 and item.get("acked") == 1
                            for item in settled))

    def test_second_claim_without_original_receipt_keeps_normal_prompt_fallback(self):
        self.seed_turn()
        did = "delivery-" + "a" * 32
        self._mark_sent_ambiguous(did)
        self._expire_ambiguous([did])

        result = self.run_plugin(self.IDLE_THEN_WAIT)

        self.assertEqual(len(result["prompts"]), 1)
        self.assertIn("attempts=2", result["prompts"][0]["body"]["parts"][0]["text"])
        self.assertEqual(PD.read(self.root, self.SID, did)["state"], "acked")

    def test_impossible_historical_attempt_count_does_not_ack_current_claim(self):
        self.seed_turn()
        did = "delivery-" + "a" * 32
        first = self._mark_sent_ambiguous(did)
        first_body = SWEEP.delivery_context([
            (self.root, [{**first, SWEEP.STORAGE_KEY: self.SID}])])
        self._expire_ambiguous([did])
        handed = self.cli("deliver")
        self.assertEqual(handed["records"][0]["immutable_identity"]["delivery_id"], did)
        impossible = first_body.replace("attempts=1", "attempts=3")
        native = [{"info": {"sessionID": self.SID, "role": "user"},
                   "parts": [{"type": "text", "text": impossible}]}]

        result = self.cli("settle", json.dumps({**handed, "messages": native}))

        self.assertEqual(result["received_ids"], [])
        self.assertEqual(result["text"], handed["text"])
        self.assertEqual(PD.read(self.root, self.SID, did)["state"], "claimed")

    def test_historical_receipt_ack_failure_suppresses_send_and_retries_ack(self):
        self.seed_turn()
        did = "delivery-" + "a" * 32
        first = self._mark_sent_ambiguous(did)
        first_body = SWEEP.delivery_context([
            (self.root, [{**first, SWEEP.STORAGE_KEY: self.SID}])])
        self._expire_ambiguous([did])
        handed = self.cli("deliver")
        native = [{"info": {"sessionID": self.SID, "role": "user"},
                   "parts": [{"type": "text", "text": first_body}]}]

        with mock.patch.object(PD, "ack_received", side_effect=OSError("simulated ACK write failure")):
            failed_ack = self.settle_at(handed, time.monotonic_ns(), messages=native)
        self.assertEqual(failed_ack["received_ids"], [did])
        self.assertEqual(failed_ack["text"], "")
        self.assertEqual(failed_ack["records"], [])
        self.assertEqual(PD.read(self.root, self.SID, did)["state"], "claimed")

        retry = self.settle_at(handed, time.monotonic_ns(), messages=native)
        self.assertEqual(retry["received_ids"], [did])
        self.assertEqual(retry["settled_count"], 1)
        self.assertEqual(retry["text"], "")
        self.assertEqual(PD.read(self.root, self.SID, did)["state"], "acked")

    def test_prior_batch_settles_after_unrelated_pending_addition_and_sends_only_new_record(self):
        import dispatch_recipient_receipt as recipient_receipt
        first_id = "delivery-" + "a" * 32
        second_id = "delivery-" + "b" * 32
        third_id = "delivery-" + "c" * 32
        self.seed_turn()
        self._seed(self.SID, recipient_kind="opencode-turn", delivery_id=second_id,
                   route_id="rt-second")
        self._seed(self.SID, recipient_kind="opencode-turn", delivery_id=third_id,
                   route_id="rt-third")
        first = self._mark_sent_ambiguous(first_id)
        second = self._mark_sent_ambiguous(second_id)
        # The prior carrier's body preserves its original order even though the
        # current directory enumeration presents these records as A then B.
        prior_body = SWEEP.delivery_context([
            (self.root, [{**second, SWEEP.STORAGE_KEY: self.SID},
                         {**first, SWEEP.STORAGE_KEY: self.SID}])])
        native = [{"info": {"sessionID": self.SID, "role": "user"},
                   "parts": [{"type": "text", "text": prior_body}]}]

        result = self.run_plugin(self.IDLE_THEN_WAIT, native_messages=native)

        self.assertEqual(len(result["prompts"]), 1)
        sent_body = result["prompts"][0]["body"]["parts"][0]["text"]
        self.assertIn(third_id, sent_body)
        self.assertNotIn(first_id, sent_body)
        self.assertNotIn(second_id, sent_body)
        for delivery_id in (first_id, second_id, third_id):
            self.assertEqual(PD.read(self.root, self.SID, delivery_id)["state"], "acked")
        self.assertNotIn(recipient_receipt.receipt_marker(first, self.SID), sent_body)
        self.assertNotIn(recipient_receipt.receipt_marker(second, self.SID), sent_body)

    def test_previous_full_completion_body_matches_exactly_with_frozen_renderer(self):
        self.seed_turn()
        record = self._mark_sent_ambiguous("delivery-" + "a" * 32)
        current_followup = (SWEEP._CURRENT_RUNTIME_V1_FOLLOWUP_PREFIX +
                            "\nprior next-action handle")
        with mock.patch.object(SWEEP, "_followup", return_value=current_followup):
            current = SWEEP.delivery_context([(self.root, [{**record, SWEEP.STORAGE_KEY: self.SID}])])
            legacy = SWEEP._legacy_opencode_completion_body(current, [self.root], [record])
            messages = [{"info": {"sessionID": self.SID, "role": "user"},
                         "parts": [{"type": "text", "text": legacy}]}]
            accepted = SWEEP._opencode_delivery_expectations(
                [self.root], "opencode-turn", self.SID, messages)
            self.assertEqual(len(accepted), 1)
            self.assertEqual(accepted[0]["text"], legacy)
            altered = [{"info": {"sessionID": self.SID, "role": "user"},
                        "parts": [{"type": "text", "text": legacy + " "}]}]
            self.assertEqual(SWEEP._opencode_delivery_expectations(
                [self.root], "opencode-turn", self.SID, altered), [])

    def test_prior_batch_settles_open_member_after_other_member_was_acked(self):
        first_id = "delivery-" + "a" * 32
        second_id = "delivery-" + "b" * 32
        self.seed_turn()
        self._seed(self.SID, recipient_kind="opencode-turn", delivery_id=second_id,
                   route_id="rt-second")
        first = self._mark_sent_ambiguous(first_id)
        second = self._mark_sent_ambiguous(second_id)
        prior_body = SWEEP.delivery_context([
            (self.root, [{**second, SWEEP.STORAGE_KEY: self.SID},
                         {**first, SWEEP.STORAGE_KEY: self.SID}])])
        identity = PD.received_ack_identity(second, self.SID, second_id)
        PD.ack_received(self.root, self.SID, second_id, expected_identity=identity,
                        acked_by="independent-carrier")
        native = [{"info": {"sessionID": self.SID, "role": "user"},
                   "parts": [{"type": "text", "text": prior_body}]}]

        result = self.run_plugin(self.IDLE_THEN_WAIT, native_messages=native)

        self.assertEqual(result["prompts"], [])
        self.assertEqual(PD.read(self.root, self.SID, first_id)["state"], "acked")
        self.assertEqual(PD.read(self.root, self.SID, second_id)["state"], "acked")
        self.assertEqual(PD.read(self.root, self.SID, first_id)["attempts"], 1)
        self.assertEqual(PD.read(self.root, self.SID, second_id)["attempts"], 1)

    def test_partial_prior_batch_body_is_unreceived_and_recovers_both_members(self):
        import dispatch_recipient_receipt as recipient_receipt
        first_id = "delivery-" + "a" * 32
        second_id = "delivery-" + "b" * 32
        self.seed_turn()
        self._seed(self.SID, recipient_kind="opencode-turn", delivery_id=second_id,
                   route_id="rt-second")
        first = self._mark_sent_ambiguous(first_id)
        second = self._mark_sent_ambiguous(second_id)
        prior_body = SWEEP.delivery_context([
            (self.root, [{**first, SWEEP.STORAGE_KEY: self.SID},
                         {**second, SWEEP.STORAGE_KEY: self.SID}])])
        second_marker = recipient_receipt.receipt_marker(second, self.SID)
        partial_body = prior_body[:prior_body.index(second_marker)]
        self.assertIn(recipient_receipt.receipt_marker(first, self.SID), partial_body)
        self.assertNotIn(second_marker, partial_body)
        self._expire_ambiguous((first_id, second_id))
        native = [{"info": {"sessionID": self.SID, "role": "user"},
                   "parts": [{"type": "text", "text": partial_body}]}]

        result = self.run_plugin(self.IDLE_THEN_WAIT, native_messages=native)

        self.assertEqual(len(result["prompts"]), 1)
        sent_body = result["prompts"][0]["body"]["parts"][0]["text"]
        self.assertIn(first_id, sent_body)
        self.assertIn(second_id, sent_body)
        for delivery_id in (first_id, second_id):
            record = PD.read(self.root, self.SID, delivery_id)
            self.assertEqual(record["state"], "acked")
            self.assertEqual(record["attempts"], 2)

    def test_foreign_identity_in_prior_batch_is_unreceived_and_recovers_both_members(self):
        import dispatch_recipient_receipt as recipient_receipt
        first_id = "delivery-" + "a" * 32
        second_id = "delivery-" + "b" * 32
        self.seed_turn()
        self._seed(self.SID, recipient_kind="opencode-turn", delivery_id=second_id,
                   route_id="rt-second")
        first = self._mark_sent_ambiguous(first_id)
        second = self._mark_sent_ambiguous(second_id)
        prior_body = SWEEP.delivery_context([
            (self.root, [{**first, SWEEP.STORAGE_KEY: self.SID},
                         {**second, SWEEP.STORAGE_KEY: self.SID}])])
        genuine_marker = recipient_receipt.receipt_marker(second, self.SID)
        foreign_body = prior_body.replace(genuine_marker,
                                          genuine_marker.rsplit(" ", 1)[0] + " " + "0" * 64)
        self.assertNotEqual(foreign_body, prior_body)
        self._expire_ambiguous((first_id, second_id))
        native = [{"info": {"sessionID": self.SID, "role": "user"},
                   "parts": [{"type": "text", "text": foreign_body}]}]

        result = self.run_plugin(self.IDLE_THEN_WAIT, native_messages=native)

        self.assertEqual(len(result["prompts"]), 1)
        sent_body = result["prompts"][0]["body"]["parts"][0]["text"]
        self.assertIn(first_id, sent_body)
        self.assertIn(second_id, sent_body)
        for delivery_id in (first_id, second_id):
            record = PD.read(self.root, self.SID, delivery_id)
            self.assertEqual(record["state"], "acked")
            self.assertEqual(record["attempts"], 2)

    def test_settle_requires_a_strictly_future_integer_claim_deadline(self):
        self.seed_turn()
        handed = self.cli("deliver")
        did = handed["records"][0]["delivery_id"]
        path = PD.record_path(self.root, self.SID, did)
        now_ns = 20000
        cases = (
            ("missing", None, False),
            ("malformed", "20000", False),
            ("boolean", True, False),
            ("expired", now_ns - 1, False),
            ("exactly-now", now_ns, False),
            ("future", now_ns + 1, True),
        )
        for name, deadline, valid in cases:
            with self.subTest(deadline=name):
                record = json.loads(path.read_text(encoding="utf-8"))
                if deadline is None:
                    record.pop("claim_deadline_ns", None)
                else:
                    record["claim_deadline_ns"] = deadline
                path.write_text(json.dumps(record), encoding="utf-8")
                result = self.settle_at(handed, now_ns)
                if valid:
                    self.assertEqual(result["records"][0]["delivery_id"], did)
                    self.assertEqual(result["text"], handed["text"])
                else:
                    self.assertEqual(result["text"], "")
                    self.assertEqual(result["records"], [])

    def test_settle_with_replaced_identity_returns_no_stale_context(self):
        self.seed_turn()
        handed = self.cli("deliver")
        did = handed["records"][0]["delivery_id"]
        path = PD.record_path(self.root, self.SID, did)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["route_node"] = "replacement-node"
        path.write_text(json.dumps(raw), encoding="utf-8")
        native = [{"info": {"sessionID": self.SID, "role": "user"},
                   "parts": [{"type": "text", "text": handed["text"]}]}]
        result = self.cli("settle", json.dumps({**handed, "messages": native}))
        self.assertEqual(result["text"], "")
        self.assertEqual(result["records"], [])
        self.assertEqual(result["unknown_ids"], [did])
        self.assertEqual(PD.read(self.root, self.SID, did)["state"], "claimed")

    def test_settle_record_io_failure_returns_no_raw_fallback(self):
        self.seed_turn()
        handed = self.cli("deliver")
        did = handed["records"][0]["delivery_id"]
        output = io.StringIO()
        payload = {**handed, "messages": []}
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
             mock.patch.object(SWEEP.pending_delivery, "read", side_effect=OSError("read failed")), \
             contextlib.redirect_stdout(output):
            self.assertEqual(SWEEP.main([
                "settle", "--recipient-kind", "opencode-turn", "--session", self.SID,
            ]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["text"], "")
        self.assertEqual(result["records"], [])
        self.assertEqual(result["unknown_ids"], [did])
        self.assertEqual(PD.read(self.root, self.SID, did)["state"], "claimed")

    def test_quoted_marker_does_not_ack_or_hide_the_turn_fallback(self):
        import dispatch_recipient_receipt as recipient_receipt
        self.seed_turn()
        did = "delivery-" + "a" * 32
        PD.claim(self.root, self.SID, did, claim_owner="other-carrier", lease_seconds=30,
                 require_generation_proof=False)
        PD.mark_sent_ambiguous(self.root, self.SID, did, claim_owner="other-carrier")
        current = PD.read(self.root, self.SID, did)
        PD.reclaim(self.root, self.SID, did, now_ns=current["claim_deadline_ns"] + 1)
        marker = recipient_receipt.receipt_marker(PD.read(self.root, self.SID, did), self.SID)
        quoted = [{"info": {"sessionID": self.SID, "role": "user"},
                   "parts": [{"type": "text", "text": "Quoted marker: " + marker}]}]
        result = self.run_plugin(self.IDLE_THEN_WAIT, native_messages=quoted)
        self.assertEqual(len(result["prompts"]), 1)
        self.assertIn(marker, result["prompts"][0]["body"]["parts"][0]["text"])
        self.assertEqual(self.state(), "acked")
        self.assertEqual(PD.read(self.root, self.SID, did)["attempts"], 2)

    def test_mixed_batch_settles_only_the_unchanged_identity(self):
        self.seed_turn()
        second_id = "delivery-" + "b" * 32
        self._seed(self.SID, recipient_kind="opencode-turn", delivery_id=second_id,
                   route_id="rt-second")
        handed = self.cli("deliver")
        self.assertEqual(len(handed["records"]), 2)
        changed_id = next(item["delivery_id"] for item in handed["records"]
                          if item["delivery_id"] == second_id)
        path = PD.record_path(self.root, self.SID, changed_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["route_node"] = "replacement-node"
        path.write_text(json.dumps(raw), encoding="utf-8")
        native = [{"info": {"sessionID": self.SID, "role": "user"},
                   "parts": [{"type": "text", "text": handed["text"]}]}]
        result = self.cli("settle", json.dumps({**handed, "messages": native}))
        original_id = next(item["delivery_id"] for item in handed["records"]
                           if item["delivery_id"] != changed_id)
        self.assertEqual(result["received_ids"], [original_id])
        self.assertEqual(result["unknown_ids"], [changed_id])
        self.assertEqual(result["text"], "")
        self.assertEqual(result["records"], [])
        self.assertEqual(PD.read(self.root, self.SID, original_id)["state"], "acked")
        self.assertEqual(PD.read(self.root, self.SID, changed_id)["state"], "claimed")

    def test_restored_session_creation_reconnects_then_idle_starts_one_turn(self):
        self.seed_turn()
        result = self.run_plugin('''
await hooks.event({event: {type: "session.created", properties: {info: {id: "ses-oc-parent"}}}});
if (prompts.length) throw new Error("activation itself started a turn");
await hooks.event({event: {type: "session.idle", properties: {sessionID: "ses-oc-parent"}}});
await new Promise(r => setTimeout(r, 1500));
''')
        self.assertEqual(len(result["prompts"]), 1)
        self.assertEqual(result["prompts"][0]["path"], {"id": self.SID})
        self.assertEqual(self.state(), "acked")

    def test_existing_selected_session_restores_without_a_creation_event(self):
        self.seed_turn()
        result = self.run_plugin('''
if (prompts.length) throw new Error("startup context was treated as receipt");
await hooks.event({event: {type: "session.idle", properties: {sessionID: "ses-oc-parent"}}});
await new Promise(r => setTimeout(r, 1500));
''', restored_selector=True)
        self.assertEqual(len(result["prompts"]), 1)
        self.assertEqual(result["prompts"][0]["path"], {"id": self.SID})
        self.assertEqual(self.state(), "acked")

    def test_actual_open_code_activation_detaches_slow_recovery_until_delivery_ack(self):
        self.seed_turn()
        record_path = PD.record_path(self.root, self.SID, "delivery-" + "a" * 32)
        snapshot = self.root.parent / "opencode-activation-complete.txt"
        barrier = _SlowRecoveryBarrier(self.root.parent / "opencode-activation-recovery.sock")
        requests = self.root.parent / "common-recovery-requests.jsonl"
        bodies = self.root.parent / "slow-recovery-bodies.jsonl"
        try:
            probe_env, requests, bodies = _slow_recovery_probe(
                self.root.parent, record_path, snapshot, barrier=barrier)
            result = self.run_plugin(f'''writeFileSync(process.env.R1_SNAPSHOT_PATH, "activation-complete\\n");
await hooks.event({{event: {{type: "session.idle", properties: {{sessionID: "{self.SID}"}}}}}});
for (let i = 0; i < 200; i++) {{
  if (logs.some(row => row.service === "hearting-completion-carrier"
      && row.extra?.stage === "settle" && row.extra?.reason === "native-receipt")) break;
  await new Promise(r => setTimeout(r, 50));
}}
''', restored_selector=True, extra_env=probe_env)
            self.assertEqual(len(result["prompts"]), 1)
            self.assertIn("att-0000000000000000000000000000bbbb",
                          result["prompts"][0]["body"]["parts"][0]["text"])
            self.assertTrue(barrier.wait_until_entered(5), barrier.error or "recovery body did not enter barrier")
            self.assertTrue(barrier.is_held, "recovery body entered but was not held")
            self.assertFalse(bodies.exists(), "recovery body sampled state before barrier release")
            self.assertEqual(snapshot.read_text(encoding="utf-8"), "activation-complete\n")
            self.assertEqual(self.state(), "acked")
        finally:
            _finish_slow_recovery_probe(self, barrier, requests, bodies)
        self.assertTrue(barrier.join(), "recovery barrier server did not stop after release")
        request_rows = _read_jsonl_after(self, requests)
        self.assertEqual(len(request_rows), 1, request_rows)
        self.assertTrue(request_rows[0]["start_new_session"])
        self.assertEqual(Path(request_rows[0]["argv"][1]), ROOT / "utilities/peer-steward.py")
        body_rows = _read_jsonl_after(self, bodies)
        self.assertEqual(body_rows[0]["state"], "acked")
        self.assertEqual(body_rows[0]["snapshot"], "activation-complete\n")
        self.assertIsNone(body_rows[0]["barrier_error"])

    def test_actual_open_code_empty_activation_requests_recovery_without_roots_or_jobs(self):
        for case, root_exists, seed_record in (
            ("missing-root", False, False),
            ("missing-jobs", True, True),
        ):
            with self.subTest(case=case):
                case_base = self.root.parent / f"opencode-{case}"
                dispatch_root = case_base / "dispatch"
                if root_exists:
                    dispatch_root.mkdir(parents=True)
                delivery_id = "delivery-" + "a" * 32
                if seed_record:
                    previous_root = self.root
                    self.root = dispatch_root
                    try:
                        self.seed_turn()
                    finally:
                        self.root = previous_root
                    (dispatch_root / "jobs.log").unlink()

                record_path = PD.record_path(dispatch_root, self.SID, delivery_id)
                snapshot = case_base / "activation-complete.txt"
                probe_env, requests, bodies = _slow_recovery_probe(case_base, record_path, snapshot)
                extra_env = {
                    **probe_env,
                    "AGENT_HOME": str(ROOT),
                    "AGENT_DISPATCH_JOBS": str(dispatch_root / "jobs.log"),
                    "HARNESS_STATE_ROOT": str(case_base / "stable-state"),
                    "AGENT_PEER_LEDGER_ROOT": str(case_base / "peer-ledger"),
                }
                result = self.run_plugin(
                    'writeFileSync(process.env.R1_SNAPSHOT_PATH, "activation-complete\\n");',
                    restored_selector=True, extra_env=extra_env,
                )
                self.assertEqual(result["prompts"], [])
                carrier = [item["extra"] for item in result["logs"]
                           if item.get("service") == "hearting-completion-carrier"]
                self.assertFalse(any(item["stage"] in {"claim", "settle"} for item in carrier))
                request_rows = _read_jsonl_after(self, requests)
                self.assertEqual(len(request_rows), 1, request_rows)
                self.assertTrue(request_rows[0]["start_new_session"])
                self.assertEqual(Path(request_rows[0]["argv"][1]), ROOT / "utilities/peer-steward.py")
                body_rows = _read_jsonl_after(self, bodies)
                self.assertEqual(body_rows[0]["snapshot"], "activation-complete\n")
                if seed_record:
                    self.assertEqual(PD.read(dispatch_root, self.SID, delivery_id)["state"], "pending")

    def test_a_turn_opencode_did_not_take_is_released_for_the_next_pass(self):
        self.seed_turn()
        result = self.run_plugin(self.IDLE_THEN_WAIT, prompt_async="refuse")
        self.assertEqual(len(result["prompts"]), 1)
        self.assertEqual(self.state(), "pending")
        self.assertIn("not-accepted", [item["extra"]["reason"] for item in result["logs"]
                                       if item["service"] == "hearting-completion-carrier"])

    def test_timeout_with_exact_native_user_message_settles_without_resend(self):
        self.seed_turn()
        result = self.run_plugin(self.IDLE_THEN_WAIT, prompt_async="timeout-native")
        self.assertEqual(len(result["prompts"]), 1)
        self.assertEqual(self.state(), "acked")
        carrier = [item["extra"] for item in result["logs"]
                   if item["service"] == "hearting-completion-carrier"]
        self.assertIn(("prompt", "timeout"), [(item["stage"], item["reason"]) for item in carrier])
        self.assertIn(("settle", "native-receipt"), [(item["stage"], item["reason"]) for item in carrier])

    def test_timeout_without_native_user_message_releases_for_fallback(self):
        self.seed_turn()
        result = self.run_plugin(self.IDLE_THEN_WAIT, prompt_async="timeout-absent")
        self.assertEqual(len(result["prompts"]), 1)
        self.assertEqual(self.state(), "pending")
        carrier = [item["extra"] for item in result["logs"]
                   if item["service"] == "hearting-completion-carrier"]
        self.assertIn(("settle", "release-fallback"),
                      [(item["stage"], item["reason"]) for item in carrier])

    def test_a_pruned_import_root_keeps_the_exact_registry_and_wakes_the_parent(self):
        for cached_roots in (False, True):
            with self.subTest(cached_roots=cached_roots):
                # Import the plugin as a managed release, then retire that release
                # while the parent is idle. The current release and registry stay live.
                frozen = self.root.parent / ("imported-warm" if cached_roots else "imported-cold")
                plugin = frozen / "adapters/opencode/plugins/hearting-guards.js"
                plugin.parent.mkdir(parents=True)
                shutil.copyfile(ROOT / "adapters/opencode/plugins/hearting-guards.js", plugin)
                (frozen / "package.json").write_text('{"type":"module"}')
                for folder in ("core", "utilities", "tools", "hooks"):
                    (frozen / folder).symlink_to(ROOT / folder, target_is_directory=True)
                (frozen / "adapters/opencode/bin").symlink_to(ROOT / "adapters/opencode/bin", target_is_directory=True)
                data_home = frozen.parent / (frozen.name + "-data")
                current = data_home / "hearting/current"
                current.parent.mkdir(parents=True)
                current.symlink_to(ROOT, target_is_directory=True)
                env = self.env()
                env.update(AGENT_HOME=str(frozen), XDG_DATA_HOME=str(data_home))
                script = r'''
import { pathToFileURL } from "node:url";
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.AGENT_HOME + "/adapters/opencode/plugins/hearting-guards.js"));
const prompts = [], logs = [], nativeMessages = [];
process.env.HERDR_PANE_ID = "";
const session = {messages: async () => ({data: nativeMessages}), promptAsync: async (request) => {
  prompts.push(request);
  nativeMessages.push({info: {sessionID: "ses-oc-parent", role: "user"}, parts: request.body.parts});
  return {response: {ok: true, status: 204}};
}};
const hooks = await AgentHarnessGuards({client: {app: {log: async (entry) => {logs.push(entry.body)}}, session}, directory: process.cwd()});
await hooks["shell.env"]({sessionID: "ses-oc-parent"}, {env: {}});
await hooks.event({event: {type: "session.idle", properties: {sessionID: "ses-oc-parent"}}});
await new Promise(r => setTimeout(r, WAIT));
console.log("ready");
for (let i = 0; i < 200 && !prompts.length; i++) await new Promise(r => setTimeout(r, 100));
await new Promise(r => setTimeout(r, 1000));
hooks.dispose();
console.log(JSON.stringify({prompts, logs}));
'''.replace("WAIT", "5500" if cached_roots else "0")
                node = subprocess.Popen(["node", "--input-type=module", "-e", script],
                                        env=env, cwd=self.root, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(node.stdout.readline().strip(), "ready")
                    frozen.rename(frozen.with_name(frozen.name + "-retired"))
                    self.seed_turn()
                    out, err = node.communicate(timeout=40)
                finally:
                    if node.poll() is None:
                        node.kill()
                self.assertEqual(node.returncode, 0, err)
                result = json.loads(out.strip().splitlines()[-1])
                self.assertEqual(len(result["prompts"]), 1)
                self.assertEqual(result["prompts"][0]["path"], {"id": self.SID})
                self.assertEqual(self.state(), "acked")
                fallbacks = [item["extra"] for item in result["logs"]
                             if item["service"] == "hearting-completion-carrier"
                             and item["extra"]["reason"] == "root-fallback"]
                self.assertIn("deliver", [item["action"] for item in fallbacks])
                self.assertIn("settle", [item["action"] for item in fallbacks])
                self.assertTrue(all(item["sessionID"] == self.SID for item in fallbacks))
                self.assertTrue(all(item["root"] == str(current) for item in fallbacks))
                shutil.rmtree(PD.record_directory(self.root, self.SID))

    def test_a_record_written_after_the_parent_went_idle_arrives_by_the_interval_look(self):
        # The usual order: the parent yields first, the owner finishes later. Only the
        # plugin's own interval look (no event from the idle session) can deliver it.
        js = r'''
import { pathToFileURL } from "node:url";
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.AGENT_HOME + "/adapters/opencode/plugins/hearting-guards.js"));
const prompts = [], nativeMessages = [];
const session = {messages: async () => ({data: nativeMessages}), prompt: async () => ({data: null}),
  promptAsync: async (request) => {
    prompts.push(request);
    nativeMessages.push({info: {sessionID: "ses-oc-parent", role: "user"}, parts: request.body.parts});
    return {response: {ok: true, status: 204}};
  }};
process.env.HERDR_PANE_ID = "";
const hooks = await AgentHarnessGuards({client: {app: {log: async () => {}}, session}, directory: process.cwd()});
await hooks["shell.env"]({sessionID: "ses-oc-parent"}, {env: {}});
await hooks.event({event: {type: "session.idle", properties: {sessionID: "ses-oc-parent"}}});
console.log("idle");
for (let i = 0; i < 300 && !prompts.length; i++) await new Promise(r => setTimeout(r, 100));
await new Promise(r => setTimeout(r, 1500));
hooks.dispose();
console.log(JSON.stringify({prompts}));
process.exit(0);
'''
        self.env()
        node = subprocess.Popen(["node", "--input-type=module", "-e", js], env=self.env(), cwd=str(self.root),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(node.stdout.readline().strip(), "idle")
            self.seed_turn()                                    # the owner finishes now
            out, err = node.communicate(timeout=60)
        finally:
            if node.poll() is None:
                node.kill()
        self.assertEqual(node.returncode, 0, err)
        prompts = json.loads(out.strip().splitlines()[-1])["prompts"]
        self.assertEqual(len(prompts), 1)
        self.assertIn("att-0000000000000000000000000000bbbb", prompts[0]["body"]["parts"][0]["text"])
        self.assertEqual(self.state(), "acked")

    def test_the_carrier_names_itself_only_where_it_can_carry(self):
        script = r'''
const out = {env: {}};
await hooks["shell.env"]({sessionID: "ses-oc-parent"}, out);
console.log(JSON.stringify({carrierEnv: out.env.AGENT_PARENT_COMPLETION_CARRIER}));
'''
        for mode, expected in (("accept", "opencode-turn:ses-oc-parent"), ("absent", "")):
            with self.subTest(mode=mode):
                js_env = self.run_plugin_env(script, mode)
                self.assertEqual(js_env, expected)

    def run_plugin_env(self, script, mode):
        js = r'''
import { pathToFileURL } from "node:url";
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.AGENT_HOME + "/adapters/opencode/plugins/hearting-guards.js"));
const session = {promptAsync: async () => ({response: {ok: true}})};
if (MODE === "absent") delete session.promptAsync;
process.env.HERDR_PANE_ID = "";
const hooks = await AgentHarnessGuards({client: {app: {log: async () => {}}, session}, directory: process.cwd()});
BODY
hooks.dispose();
'''.replace("MODE", json.dumps(mode)).replace("BODY", script)
        run = subprocess.run(["node", "--input-type=module", "-e", js], env=self.env(), cwd=str(self.root),
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout.strip().splitlines()[-1])["carrierEnv"]


class OpenCodeLivenessSideChannelTest(IsolatedRootMixin, unittest.TestCase):
    """A dispatched OpenCode worker's plugin-load marker and heartbeat land where
    dispatch-liveness.py reads them, never inside the release that is AGENT_HOME."""

    def test_marker_and_heartbeat_land_beside_the_registry_not_in_the_release(self):
        # Under AGENT_HOME the reader never sees them, and the release grows a
        # .dispatch tree that the session sweep then logs into on every prompt,
        # so the superseded release refuses pruning (delta-digest-mismatch).
        self.root.mkdir(parents=True, exist_ok=True)
        jobs = self.root / "jobs.log"
        jobs.touch()
        release = self.root.parent / "release"
        plugin = release / "adapters/opencode/plugins/hearting-guards.js"
        plugin.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "adapters/opencode/plugins/hearting-guards.js", plugin)
        (release / "package.json").write_text('{"type":"module"}')
        for folder in ("core", "utilities", "tools", "hooks"):
            (release / folder).symlink_to(ROOT / folder, target_is_directory=True)
        (release / "adapters/opencode/bin").symlink_to(ROOT / "adapters/opencode/bin", target_is_directory=True)
        env = dict(os.environ)
        env.update(AGENT_HOME=str(release), AGENT_DISPATCH_JOBS=str(jobs), HARNESS_STATE_ROOT=str(self.root),
                   OPENCODE_DISPATCH_SLUG="oc-worker", AGENT_SESSION_ROLE="worker")
        script = r'''
import { pathToFileURL } from "node:url";
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.AGENT_HOME + "/adapters/opencode/plugins/hearting-guards.js"));
process.env.HERDR_PANE_ID = "";
const hooks = await AgentHarnessGuards({client: {app: {log: async () => {}}, session: {}}, directory: process.cwd()});
await hooks.event({event: {type: "session.idle", properties: {sessionID: "ses-oc-worker"}}});
hooks.dispose();
process.exit(0);
'''
        run = subprocess.run(["node", "--input-type=module", "-e", script], env=env, cwd=str(self.root),
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse((release / ".dispatch").exists())
        self.assertTrue((self.root / "plugin-load.oc-worker.mark").is_file())
        self.assertTrue((self.root / "logs" / "oc-worker.heartbeat").is_file())
        liveness = _load("opencode_dispatch_liveness", ROOT / "adapters/opencode/bin/dispatch-liveness.py")
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
            self.assertTrue(liveness.plugin_loaded_for_slug(release, "oc-worker"))
            self.assertIsNotNone(liveness.heartbeat_age_min(release, "oc-worker", time.time()))


class ActivationDispatchReconcileTest(IsolatedRootMixin, unittest.TestCase):
    def test_only_canonical_session_activation_runs_existing_reconcile_owners(self):
        self.root.mkdir(parents=True, exist_ok=True)
        jobs = self.root / "jobs.log"
        jobs.touch()
        secondary = self.root.parent / "secondary-dispatch"
        secondary.mkdir()
        with mock.patch("dispatch_contract.resolve_agent_home", return_value=self.root.parent), \
             mock.patch("dispatch_contract.dispatch_state_roots", return_value=(self.root, secondary)), \
             mock.patch.object(SWEEP, "addressed_records", return_value=([], 0)), \
             mock.patch("dispatch_completion_join.reconcile_pending_delivery") as reconcile, \
             mock.patch("dispatch_batch_obligations.ensure_observers") as ensure, \
             mock.patch("peer_obligations.request_obligation_recovery", return_value=True) as request:
            SWEEP.activate(self.root, "claude-parent-runtime", "sid-activation")
            SWEEP.activate(secondary, "claude-parent-runtime", "sid-activation")
        reconcile.assert_not_called()
        ensure.assert_not_called()
        request.assert_called_once_with(lifecycle_root=self.root.parent)

    def test_canonical_activation_without_jobs_does_not_request_recovery(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with mock.patch("dispatch_contract.resolve_agent_home", return_value=self.root.parent), \
             mock.patch("dispatch_contract.dispatch_state_roots", return_value=(self.root,)), \
             mock.patch.object(SWEEP, "addressed_records", return_value=([], 0)), \
             mock.patch("peer_obligations.request_obligation_recovery") as request:
            SWEEP.activate(self.root, "claude-parent-runtime", "sid-no-jobs")
        request.assert_not_called()
        peer_obligations.ensure_normal_runner.assert_not_called()

    def test_cli_activation_requests_once_after_flushed_empty_output_without_roots(self):
        class FlushOutput(io.StringIO):
            flushed = False

            def flush(self):
                self.flushed = True
                super().flush()

        output = FlushOutput()
        observed = []
        with mock.patch.object(SWEEP, "_state_roots", return_value=[]), \
             mock.patch.object(SWEEP, "request_obligation_reconciliation",
                               side_effect=lambda *, lifecycle_root=None:
                               observed.append((output.flushed, lifecycle_root))), \
             mock.patch.object(sys, "stdout", output):
            self.assertEqual(SWEEP.main(["activate", "--recipient-kind", "opencode-turn",
                                         "--session", "sid-empty"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload, {"text": "", "records": []})
        self.assertEqual(observed, [(True, ROOT)])

    def test_canonical_activation_continues_past_unreadable_registry(self):
        self.root.mkdir(parents=True, exist_ok=True)
        jobs = self.root / "jobs.log"
        jobs.touch()
        args = SimpleNamespace(
            jobs=jobs, sealed_batch_id="batch-activation", parent_attempt_id="att-parent",
            parent_session_id=None, attempt_id=["att-sidecar"],
            delivery_parent_id="att-parent", control_socket=self.root / "control.sock",
            queue_socket=None, thread_id=None, interval=0.1, timeout=1.0,
        )
        duty_id, _record = OBLIGATIONS.create(args)
        unreadable = b"\xffactivation-registry\n"
        jobs.write_bytes(unreadable)

        secondary = self.root.parent / "secondary-dispatch"
        secondary.mkdir()
        other_jobs = secondary / "jobs.log"
        metadata = ",".join((
            "attempt_schema_version=2", "dispatch_depth=1", "worker_type=owner",
            "launch_started=1", "supervisor_lease=flock-v1",
            "supervisor_lease_file=/fixture/owner.lease", "log_file=/fixture/owner.log",
            "attempt_id=att-activation-owner", "workflow_completion=runtime-v1",
            "note=completed-supervisor",
        ))
        other_jobs.write_text(
            f"2026-10-18T00:00:00Z\tdone\t/repo\t/wt\towner\t{metadata}\n",
            encoding="utf-8")
        other_before = other_jobs.read_bytes()
        other_args = SimpleNamespace(**vars(args))
        other_args.jobs = other_jobs
        other_args.sealed_batch_id = "batch-secondary-activation"
        other_args.attempt_id = ["att-secondary-sidecar"]
        other_args.control_socket = secondary / "control.sock"
        other_duty_id, _other_record = OBLIGATIONS.create(other_args)

        with mock.patch("dispatch_contract.resolve_agent_home", return_value=self.root.parent), \
             mock.patch("dispatch_contract.dispatch_state_roots", return_value=(self.root, secondary)), \
             mock.patch.object(SWEEP, "addressed_records", return_value=([], 0)), \
             mock.patch("dispatch_completion_join.reconcile_pending_delivery") as reconcile, \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle, \
             mock.patch("peer_obligations.request_obligation_recovery", return_value=True) as request:
            self.assertEqual(SWEEP.activate(self.root, "claude-parent-runtime", "sid-activation"), [])

        reconcile.assert_not_called()
        request.assert_called_once_with(lifecycle_root=self.root.parent)
        settle.assert_not_called()
        self.assertEqual(OBLIGATIONS.read(jobs, duty_id)["state"], "observing")
        self.assertEqual(OBLIGATIONS.read(other_jobs, other_duty_id)["state"], "observing")
        self.assertEqual(jobs.read_bytes(), unreadable)
        self.assertEqual(other_jobs.read_bytes(), other_before)


class AdapterRecoveryRequestTest(IsolatedRootMixin, unittest.TestCase):
    def test_codex_sessionstart_keeps_recovery_owner_when_root_resolution_raises(self):
        bridge = _load("codex_sessionstart_root_error_test",
                       ROOT / "adapters/codex/hooks/sessionstart-lifecycle.py")
        import dispatch_contract

        output = io.StringIO()
        with mock.patch.object(bridge, "load_payload", return_value={"session_id": "codex-root-error"}), \
             mock.patch.object(bridge, "is_worker_session", return_value=False), \
             mock.patch.object(bridge, "card_context", return_value=("fixture context", "")), \
             mock.patch.object(bridge, "local_evidence_context", return_value=""), \
             mock.patch.object(dispatch_contract, "resolve_agent_home", return_value=ROOT), \
             mock.patch.object(dispatch_contract, "dispatch_state_roots",
                               side_effect=RuntimeError("state roots unavailable")), \
             mock.patch.object(bridge, "request_lifecycle_recovery") as request, \
             mock.patch.dict(sys.modules, {
                 "herdr_session_projection": SimpleNamespace(project=lambda *args, **kwargs: None),
             }), contextlib.redirect_stdout(output):
            self.assertEqual(bridge.main(), 0)
        request.assert_called_once_with()
        payload = json.loads(output.getvalue())
        self.assertIn("fixture context", payload["hookSpecificOutput"]["additionalContext"])

    def test_codex_worker_prompt_and_sessionstart_request_recovery_without_main_context(self):
        prompt = _load("codex_userprompt_recovery_test",
                       ROOT / "adapters/codex/hooks/userprompt-lifecycle.py")
        sessionstart = _load("codex_sessionstart_recovery_test",
                             ROOT / "adapters/codex/hooks/sessionstart-lifecycle.py")
        worker_env = {
            "AGENT_SESSION_ROLE": "worker",
            "AGENT_DISPATCH_CHILD": "1",
            "AGENT_DISPATCH_DEPTH": "2",
        }
        with mock.patch.dict(os.environ, worker_env), \
             mock.patch.object(peer_obligations, "request_obligation_recovery") as request, \
             mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": "codex-worker"}))), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(prompt.main(), 0)
        request.assert_called_once_with(lifecycle_root=ROOT)
        self.assertEqual(output.getvalue(), "")

        request.reset_mock()
        with mock.patch.dict(os.environ, worker_env), \
             mock.patch.object(peer_obligations, "request_obligation_recovery") as request_start, \
             mock.patch.object(sessionstart, "run_preflight", side_effect=AssertionError("main memory path")), \
             mock.patch.object(sessionstart, "local_evidence_context",
                               side_effect=AssertionError("main context path")), \
             mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": "codex-worker"}))), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sessionstart.main(), 0)
        request_start.assert_called_once_with(lifecycle_root=ROOT)
        self.assertEqual(output.getvalue(), "")

    def test_claude_sessionstart_requests_recovery_before_missing_root_return(self):
        with mock.patch.object(HOOK, "request_obligation_reconciliation") as request, \
             mock.patch.object(HOOK, "resolve_agent_home", side_effect=RuntimeError("no projection")), \
             mock.patch("sys.stdin", io.StringIO(json.dumps({
                 "session_id": "claude-session", "hook_event_name": "SessionStart",
             }))):
            self.assertEqual(HOOK.main(), 0)
        request.assert_called_once_with(lifecycle_root=ROOT)

    def test_actual_codex_sessionstart_outputs_context_before_detached_slow_recovery_without_ack(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "jobs.log").touch()
        delivery_id = "delivery-" + "d" * 32
        SweepTest._seed(self, recipient_kind="codex-native-queue", delivery_id=delivery_id)
        record_path = PD.record_path(self.root, "sess-owner", delivery_id)
        snapshot = self.root.parent / "codex-sessionstart-output.json"
        probe_env, requests, bodies = _slow_recovery_probe(self.root.parent, record_path, snapshot)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "CLAUDE_", "CODEX_", "OPENCODE_", "HERDR_", "FLEET_"))}
        env.update(self._env_patch)
        env.update(probe_env)
        result = subprocess.run(
            [sys.executable, str(ROOT / "adapters/codex/hooks/sessionstart-lifecycle.py")],
            input=json.dumps({"session_id": "sess-owner", "source": "startup"}),
            capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("att-0000000000000000000000000000bbbb", context)
        self.assertEqual(PD.read(self.root, "sess-owner", delivery_id)["state"], "pending")
        snapshot.write_text(result.stdout, encoding="utf-8")
        request_rows = _read_jsonl_after(self, requests)
        self.assertEqual(len(request_rows), 1, request_rows)
        self.assertTrue(request_rows[0]["start_new_session"])
        self.assertIn("--lifecycle-root", request_rows[0]["argv"])
        self.assertEqual(request_rows[0]["argv"][-1], str(ROOT.resolve()))
        body_rows = _read_jsonl_after(self, bodies)
        self.assertEqual(body_rows[0]["state"], "pending")
        self.assertEqual(body_rows[0]["snapshot"], result.stdout)

    def test_actual_codex_sessionstart_requests_once_after_context_without_root_jobs_or_sid(self):
        for case, sid, root_exists, seed_record in (
            ("missing-root", "codex-no-root", False, False),
            ("missing-jobs", "codex-no-jobs", True, True),
            ("missing-sid", "", False, False),
        ):
            with self.subTest(case=case):
                case_base = self.root.parent / f"codex-{case}"
                dispatch_root = case_base / "dispatch"
                if root_exists:
                    dispatch_root.mkdir(parents=True)
                delivery_id = "delivery-" + "b" * 32
                if seed_record:
                    previous_root = self.root
                    self.root = dispatch_root
                    try:
                        SweepTest._seed(self, session_id=sid, recipient_kind="codex-native-queue",
                                        delivery_id=delivery_id)
                    finally:
                        self.root = previous_root
                    (dispatch_root / "jobs.log").unlink()

                record_path = PD.record_path(dispatch_root, sid or "missing-sid", delivery_id)
                snapshot = case_base / "sessionstart-output.json"
                probe_env, requests, bodies = _slow_recovery_probe(case_base, record_path, snapshot)
                env = {key: value for key, value in os.environ.items()
                       if not key.startswith(("AGENT_", "CLAUDE_", "CODEX_", "OPENCODE_", "HERDR_", "FLEET_"))}
                env.update(self._env_patch)
                env.update(probe_env)
                env.update({
                    "AGENT_HOME": str(ROOT),
                    "AGENT_DISPATCH_JOBS": str(dispatch_root / "jobs.log"),
                    "HARNESS_STATE_ROOT": str(case_base / "stable-state"),
                    "AGENT_PEER_LEDGER_ROOT": str(case_base / "peer-ledger"),
                    "R1_CONTEXT_OUTPUT": "fixture SessionStart context",
                })
                payload = {"source": "startup", "cwd": str(self.root.parent)}
                if sid:
                    payload["session_id"] = sid
                result = subprocess.run(
                    [sys.executable, str(ROOT / "adapters/codex/hooks/sessionstart-lifecycle.py")],
                    input=json.dumps(payload), capture_output=True, text=True,
                    env=env, cwd=str(ROOT), timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                emitted = json.loads(result.stdout)
                self.assertEqual(emitted["hookSpecificOutput"]["hookEventName"], "SessionStart")
                context = emitted["hookSpecificOutput"]["additionalContext"]
                self.assertIn("fixture SessionStart context", context)
                snapshot.write_text(result.stdout, encoding="utf-8")
                request_rows = _read_jsonl_after(self, requests)
                self.assertEqual(len(request_rows), 1, request_rows)
                self.assertTrue(request_rows[0]["start_new_session"])
                self.assertEqual(request_rows[0]["argv"][-1], str(ROOT.resolve()))
                body_rows = _read_jsonl_after(self, bodies)
                self.assertEqual(body_rows[0]["snapshot"], result.stdout)
                if seed_record:
                    self.assertIn("att-0000000000000000000000000000bbbb", context)
                    self.assertEqual(PD.read(dispatch_root, sid, delivery_id)["state"], "pending")

    def test_opencode_worker_init_and_session_created_use_shared_recovery_entry(self):
        capture = self.root.parent / "opencode-recovery-calls.tsv"
        bin_dir = self.root.parent / "fixture-bin"
        bin_dir.mkdir()
        fake_python = bin_dir / "python3"
        fake_python.write_text(
            "#!/bin/sh\n"
            "for arg in \"$@\"; do if [ \"$arg\" = '__ensure-obligations' ]; then\n"
            "printf '%s\\t%s\\t%s\\t%s\\t%s\\t' \"$AGENT_DISPATCH_JOBS\" \"$HARNESS_STATE_ROOT\" \"$AGENT_PEER_LEDGER_ROOT\" \"$XDG_STATE_HOME\" \"$AGENT_HOME\" >> \"$RECOVERY_CAPTURE\"\n"
            "for item in \"$@\"; do printf '%s ' \"$item\" >> \"$RECOVERY_CAPTURE\"; done\n"
            "printf '\\n' >> \"$RECOVERY_CAPTURE\"; fi; done\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "jobs.log").touch()
        env = os.environ.copy()
        env.pop("AGENT_DISPATCH_REGISTERED_WORKER", None)
        env.update({
            "AGENT_SESSION_ROLE": "worker",
            "AGENT_DISPATCH_CHILD": "1",
            "AGENT_DISPATCH_DEPTH": "2",
            "AGENT_DISPATCH_JOBS": str(self.root / "jobs.log"),
            "HARNESS_STATE_ROOT": str(self.root.parent / "harness-state"),
            "AGENT_PEER_LEDGER_ROOT": str(self.root.parent / "peer-ledger"),
            "XDG_STATE_HOME": str(self.root.parent / "xdg-state"),
            "AGENT_HOME": str(ROOT),
            "RECOVERY_CAPTURE": str(capture),
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        })
        script = f'''import {{ pathToFileURL }} from "node:url";
const {{ AgentHarnessGuards }} = await import(pathToFileURL({json.dumps(str(ROOT / "adapters/opencode/plugins/hearting-guards.js"))}));
const hooks = await AgentHarnessGuards({{client: {{app: {{log: async () => {{}}}}, session: {{}}}}, directory: process.cwd()}});
await hooks.event({{event: {{type: "session.created", properties: {{info: {{id: "worker-session"}}}}}}}});
hooks.dispose();
'''
        run = subprocess.run(["node", "--input-type=module", "-e", script], env=env,
                             cwd=str(self.root), capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (not capture.exists() or len(capture.read_text().splitlines()) < 2):
            time.sleep(0.02)
        calls = capture.read_text().splitlines()
        self.assertEqual(len(calls), 2, calls)
        for call in calls:
            jobs, state, peer, xdg, agent_home, command = call.split("\t", 5)
            self.assertEqual((jobs, state, peer, xdg, agent_home), (
                env["AGENT_DISPATCH_JOBS"], env["HARNESS_STATE_ROOT"],
                env["AGENT_PEER_LEDGER_ROOT"], env["XDG_STATE_HOME"], env["AGENT_HOME"],
            ))
            self.assertIn("__ensure-obligations", command)
            self.assertIn("--lifecycle-root", command)
            self.assertIn(str(ROOT.resolve()), command)

    def test_opencode_main_session_activation_owns_recovery_when_carrier_is_available(self):
        self.root.mkdir(parents=True, exist_ok=True)
        capture = self.root.parent / "opencode-main-recovery-calls.tsv"
        bin_dir = self.root.parent / "main-fixture-bin"
        bin_dir.mkdir()
        fake_python = bin_dir / "python3"
        fake_python.write_text(
            "#!/bin/sh\n"
            "for arg in \"$@\"; do\n"
            "  if [ \"$arg\" = '__ensure-obligations' ]; then printf 'ensure\\n' >> \"$RECOVERY_CAPTURE\"; exit 0; fi\n"
            "  if [ \"$arg\" = 'activate' ]; then printf 'activate\\n' >> \"$RECOVERY_CAPTURE\"; printf '{\"text\":\"\",\"records\":[]}\\n'; exit 0; fi\n"
            "done\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        env = os.environ.copy()
        env.update({
            "AGENT_SESSION_ROLE": "",
            "AGENT_DISPATCH_CHILD": "",
            "AGENT_DISPATCH_DEPTH": "",
            "OPENCODE_DISPATCH_SLUG": "",
            "RECOVERY_CAPTURE": str(capture),
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        })
        script = f'''import {{ pathToFileURL }} from "node:url";
const {{ AgentHarnessGuards }} = await import(pathToFileURL({json.dumps(str(ROOT / "adapters/opencode/plugins/hearting-guards.js"))}));
const hooks = await AgentHarnessGuards({{client: {{app: {{log: async () => {{}}}}, session: {{promptAsync: async () => ({{response: {{ok: true}}}})}}}}, directory: process.cwd()}});
await hooks.event({{event: {{type: "session.created", properties: {{info: {{id: "main-session"}}}}}}}});
hooks.dispose();
'''
        run = subprocess.run(["node", "--input-type=module", "-e", script], env=env,
                             cwd=str(self.root), capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (not capture.exists()
              or len(capture.read_text().splitlines()) < 2):
            time.sleep(0.02)
        calls = capture.read_text().splitlines()
        self.assertEqual(calls.count("ensure"), 1, calls)
        self.assertEqual(calls.count("activate"), 1, calls)


class DetachedCommonRecoveryTest(IsolatedRootMixin, unittest.TestCase):
    def test_real_detached_common_entry_reconnects_the_same_pending_batch_after_hook_exit(self):
        jobs_path = self.root / "jobs.log"
        jobs_path.parent.mkdir(parents=True, exist_ok=True)
        jobs_path.write_text("".join(
            "2026-10-18T00:00:00Z\topen\t/repo\t/wt\t" + attempt + "\t" + ",".join((
                "attempt_schema_version=2", "dispatch_depth=2", "transport=headless",
                "execution_surface=registered-headless", "registered_worker=1",
                f"attempt_id={attempt}", "parent_attempt_id=att-r6-parent", "note=fixture",
            )) + "\n" for attempt in ("att-r6-a", "att-r6-b")
        ), encoding="utf-8")
        args = SimpleNamespace(
            jobs=jobs_path,
            sealed_batch_id="batch-r6-fixture",
            parent_attempt_id="att-r6-parent",
            parent_session_id=None,
            attempt_id=["att-r6-a", "att-r6-b"],
            delivery_parent_id="att-r6-parent",
            control_socket=self.root.parent / "control.sock",
            queue_socket=None,
            thread_id=None,
            interval=0.05,
            timeout=120.0,
        )
        duty_id, before = OBLIGATIONS.create(args)
        original_jobs = jobs_path.read_bytes()

        lifecycle_root = self.root.parent / "fixture-lifecycle"
        fixture_utilities = lifecycle_root / "utilities"
        fixture_utilities.mkdir(parents=True)
        (lifecycle_root / "hooks").mkdir()
        for source in (ROOT / "utilities").iterdir():
            if source.name == "peer-steward.py":
                continue
            target = fixture_utilities / source.name
            target.symlink_to(source, target_is_directory=source.is_dir())
        shutil.copyfile(ROOT / "hooks/dispatch-session-sweep.py",
                        lifecycle_root / "hooks/dispatch-session-sweep.py")
        wrapper_pid_file = self.root.parent / "common-entry.pid"
        wrapper_env_file = self.root.parent / "common-entry-env.json"
        exec_marker = self.root.parent / "common-entry-exec.ns"
        real_entry = ROOT / "utilities/peer-steward.py"
        (fixture_utilities / "peer-steward.py").write_text(
            "import json,os,sys,time\n"
            "from pathlib import Path\n"
            "Path(os.environ['COMMON_ENTRY_PID_FILE']).write_text(str(os.getpid()))\n"
            "Path(os.environ['COMMON_ENTRY_ENV_FILE']).write_text(json.dumps({"
            "'env':{k:os.environ.get(k) for k in ('AGENT_DISPATCH_JOBS','HARNESS_STATE_ROOT','AGENT_PEER_LEDGER_ROOT','XDG_STATE_HOME','AGENT_HOME')},"
            "'args':sys.argv[1:]}))\n"
            "time.sleep(1.5)\n"
            "Path(os.environ['COMMON_ENTRY_EXEC_MARKER']).write_text(str(time.monotonic_ns()))\n"
            f"os.execv(sys.executable,[sys.executable,{str(real_entry)!r},*sys.argv[1:]])\n",
            encoding="utf-8",
        )
        (lifecycle_root / "core").mkdir()
        fixture_agent_home = self.root.parent / "fixture-agent-home"
        (fixture_agent_home / "core").mkdir(parents=True)
        (fixture_agent_home / "core/CORE.md").write_text("fixture home\n")
        harness = self.root.parent / "run-real-hook.py"
        harness.write_text(
            "import importlib.util,json,os,sys\n"
            "from pathlib import Path\n"
            f"hook_path=Path({str(lifecycle_root / 'hooks/dispatch-session-sweep.py')!r})\n"
            f"sys.path.insert(0,{str(lifecycle_root / 'utilities')!r})\n"
            "spec=importlib.util.spec_from_file_location('r6_hook',hook_path); hook=importlib.util.module_from_spec(spec); spec.loader.exec_module(hook)\n"
            "sys.stdin=__import__('io').StringIO(json.dumps({'session_id':'fixture-session','hook_event_name':'UserPromptSubmit'}))\n"
            "raise SystemExit(hook.main())\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update({
            "HOME": str(self.root.parent / "home"),
            "XDG_STATE_HOME": str(self.root.parent / "xdg-state"),
            "HARNESS_STATE_ROOT": str(self.root.parent / "harness-state"),
            "AGENT_PEER_LEDGER_ROOT": str(self.root.parent / "peer-ledger"),
            "AGENT_DISPATCH_JOBS": str(jobs_path),
            "AGENT_HOME": str(fixture_agent_home),
            "COMMON_ENTRY_PID_FILE": str(wrapper_pid_file),
            "COMMON_ENTRY_ENV_FILE": str(wrapper_env_file),
            "COMMON_ENTRY_EXEC_MARKER": str(exec_marker),
        })
        wrapper_pid = observer_pid = None
        try:
            started_ns = time.monotonic_ns()
            hook = subprocess.Popen([sys.executable, str(harness)], env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            output, error = hook.communicate(timeout=5)
            hook_exit_ns = time.monotonic_ns()
            self.assertEqual(hook.returncode, 0, error.decode("utf-8", "replace"))
            self.assertLess(hook_exit_ns - started_ns, 10_000_000_000)

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not exec_marker.exists():
                time.sleep(0.02)
            self.assertTrue(exec_marker.exists(), "detached request did not reach the real common entry")
            wrapper_pid = int(wrapper_pid_file.read_text())
            exec_ns = int(exec_marker.read_text())
            self.assertGreater(exec_ns, hook_exit_ns,
                               "real common entry began before its requesting hook exited")
            snapshot = json.loads(wrapper_env_file.read_text())
            self.assertEqual(snapshot["env"], {
                "AGENT_DISPATCH_JOBS": env["AGENT_DISPATCH_JOBS"],
                "HARNESS_STATE_ROOT": env["HARNESS_STATE_ROOT"],
                "AGENT_PEER_LEDGER_ROOT": env["AGENT_PEER_LEDGER_ROOT"],
                "XDG_STATE_HOME": env["XDG_STATE_HOME"],
                "AGENT_HOME": env["AGENT_HOME"],
            })
            self.assertEqual(snapshot["args"], [
                "__ensure-obligations", "--lifecycle-root", str(lifecycle_root),
            ])

            deadline = time.monotonic() + 5
            after = OBLIGATIONS.read(jobs_path, duty_id)
            while (after is None or not after.get("observer")) and time.monotonic() < deadline:
                time.sleep(0.02)
                after = OBLIGATIONS.read(jobs_path, duty_id)
            self.assertIsNotNone(after)
            observer = after["observer"]
            self.assertIsInstance(observer.get("pid"), int)
            observer_pid = observer["pid"]
            self.assertEqual(after["id"], before["id"])
            self.assertEqual(after["identity"], before["identity"])
            self.assertEqual(after["identity"]["attempt_ids"], ["att-r6-a", "att-r6-b"])
            self.assertEqual(after["identity"]["delivery_parent_id"], "att-r6-parent")
            self.assertEqual(after["effective_attempts"], ["att-r6-a", "att-r6-b"])
            self.assertEqual(after["state"], "observing")
            self.assertIsNone(after["outcome"])
            self.assertEqual(after["delivery"], "pending")
            self.assertEqual(jobs_path.read_bytes(), original_jobs,
                             "recovery replayed or rewrote a worker assignment")
            cmdline = Path(f"/proc/{observer_pid}/cmdline").read_bytes().replace(b"\0", b" ")
            self.assertIn(b"codex-managed-completion.py", cmdline)
            self.assertIn(b"--resume-obligation", cmdline)
            self.assertNotIn(b"dispatch-headless", cmdline)
            self.assertTrue(output == b"" or output.endswith(b"\n"))
        finally:
            for pid in (observer_pid, wrapper_pid):
                if pid is not None:
                    try:
                        os.kill(pid, 15)
                    except ProcessLookupError:
                        pass
            for pid in (observer_pid, wrapper_pid):
                if pid is None:
                    continue
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)


class NativeReceivingTurnRetentionTest(unittest.TestCase):
    def test_completion_receipt_is_ackable_only_after_exact_turn_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs.log"
            jobs.touch()
            record = {"delivery_id": "delivery-exact", "recipient_kind": "codex-native-queue",
                "attempt_ids": ["att-exact"], "receipt": {"children": [{"attempt_id": "att-exact"}]}}
            binding = {"thread_id": "sid-exact", "turn_id": "turn-exact", "status": "inProgress"}
            prompt = "Exact hook prompt\nOUTPUT_FORMAT_CANARY: exact JSON"
            with mock.patch.object(SWEEP, "record_jobs", return_value=jobs), \
                 mock.patch("peer_obligations.retain_registered_completion", return_value=True) as retain:
                ackable = SWEEP.retain_receiving_turn(
                    [(root, [record])], "sid-exact", binding, request_context=prompt)
                self.assertEqual(ackable, {(str(root), "delivery-exact")})
                self.assertEqual(retain.call_count, 1)
                self.assertEqual(retain.call_args.kwargs["receiving_turn"], binding)
                self.assertEqual(retain.call_args.kwargs["request_context"], prompt)
                self.assertEqual(SWEEP.retain_receiving_turn([(root, [record])], "sid-exact", None), set())
                self.assertEqual(retain.call_count, 1)

            notice = {"delivery_id": "delivery-notice", "receipt": {"kind": "supervision"}}
            self.assertEqual(SWEEP.retain_receiving_turn([(root, [notice])], "sid-exact", None),
                             {(str(root), "delivery-notice")})


if __name__ == "__main__":
    unittest.main()
