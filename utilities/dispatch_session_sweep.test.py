#!/usr/bin/env python3
"""SD-111 P4: dispatch_session_sweep unit tests.

Every fixture injects HOME/XDG_STATE_HOME/HARNESS_STATE_ROOT into an isolated
temp tree and appends the actual values to evidence/sd111/fixture_env.tsv
(plan §10.1 hard gate), matching P1/P2's own fixture pattern.
"""

from __future__ import annotations

import importlib.util
import contextlib
import hashlib
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
sys.path.insert(0, str(ROOT / "tools"))
import fixture_processes  # noqa: E402

FIXTURE_ENV_LOG = os.environ.get("SD111_FIXTURE_ENV_LOG")


# Emitted bytes frozen from 2854679, independent of recovery constants.
_FROZEN_NOTICE_DELIVERY_HEADER = (
    "Hearting notice about work you started (a paused route resumed, a remote run ended). "
    "Report observed metrics, trend, and elapsed or ETA against the criteria and plan; state when unavailable. "
    "Act only where a line's required_action asks for it. "
    "Do not start Monitor, dispatch-wait, or a polling loop."
)
_FROZEN_GATE_DELIVERY_HEADER = (
    "Hearting human gate awaiting your decision (SD-123/129). Read the named artifact "
    "(an interview file or a frame summary), put the [방향 확인] card and every interview "
    "question to the user through AskUserQuestion -- one topic at a time, in plain words, "
    "recommended answer first -- then record the answer with `workflow-supervisor.py "
    "release --route <route file> --gate <name> --decision proceed|revise|stop "
    "[--answers <file from frame_interview.py answers-template>]`. The owner is waiting on "
    "`await-release` or has paused at the gate; the release continues the work either way. "
    "Do not start Monitor, dispatch-wait, or a polling loop."
)
_FROZEN_COMPLETION_DELIVERY_HEADER = (
    "Hearting runtime delivery. Follow required_action; inspect-recovery is diagnostic. "
    "Follow route's next-action receipt; runtime-v1 owners close/finalize. "
    "Tracked-Workflow Continuation. Keep new failures/conflicts, unfinished work, questions; "
    "no Monitor/dispatch-wait/polling."
)
_FROZEN_LEGACY_COMPLETION_DELIVERY_HEADER = (
    "Hearting runtime delivery (SD-111 durable pending records for this session). "
    "Follow each receipt's required_action. advance-completed confirms that attempt's "
    "completion: read its exact result and perform already approved parent review/follow-through "
    "this turn or its existing registered continuation. Receipt, queue consumption and ACK are not execution. "
    "On the first user-facing close of material work, explain what changed and why, observed before/after, "
    "checks actually run (including reused, skipped, and unrun checks), applicable commit/PR/deployment/serving, "
    "and limits or remaining work; do not invent deployment or operating results. Follow the owning "
    "route's next-action receipt for any remaining stages. Runtime-v1 owners have "
    "runtime-owned close/finalize; a closed route needs no restart. "
    "inspect-recovery is a runtime diagnostic, not a request for a user decision. "
    "For other unresolved actions use the exact checked recovery surface. "
    "An automatic repeat of the same exact result or cleanup-only notice after follow-through was handled "
    "does not repeat the action or request a new user ACK; preserve new failures, conflicts, unfinished work, "
    "and direct user questions. "
    "Do not start Monitor, dispatch-wait, or a polling loop."
)
_FROZEN_LEGACY_RUNTIME_V1_FOLLOWUP = (
    "The requested work is complete, including its declared stages and runtime workflow/route/cycle settlement. "
    "Continue and complete the original user task itself, preserving every explicit content and output-format "
    "requirement from that request; this receipt supplements the task and does not replace it with a receipt-only response. "
    "Read the exact result and perform the already approved parent review/follow-through in this turn, "
    "or run its existing registered continuation. On the first user-facing close, explain what changed and why, "
    "the observed before/after, checks actually run (including reused, skipped, and unrun checks), applicable "
    "commit/PR/deployment/serving state, and limits or remaining work. Do not invent deployment or operating "
    "results. Receipt, queue consumption and ACK are not execution. No harvest, route restart, or manual "
    "finalization is required. An automatic repeat of this exact result or a cleanup-only notice after the "
    "follow-through is handled does not call for the same action or a new user ACK; preserve a new failure, "
    "conflict, unfinished work, or direct user question."
)


def _previous_native_body(text, *, legacy=False):
    text = text.replace(SWEEP.GATE_DELIVERY_HEADER, _FROZEN_GATE_DELIVERY_HEADER, 1)
    text = text.replace(SWEEP.NOTICE_DELIVERY_HEADER, _FROZEN_NOTICE_DELIVERY_HEADER, 1)
    text = text.replace(SWEEP.COMPLETION_DELIVERY_HEADER,
                        _FROZEN_LEGACY_COMPLETION_DELIVERY_HEADER if legacy
                        else _FROZEN_COMPLETION_DELIVERY_HEADER, 1)
    if legacy:
        text = text.replace(SWEEP._CURRENT_RUNTIME_V1_FOLLOWUP_PREFIX,
                            _FROZEN_LEGACY_RUNTIME_V1_FOLLOWUP, 1)
    return text


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
                                lambda: self._release_requested
                            )
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
    from dispatch_contract import pidfd_open

    try:
        pidfd = pidfd_open(pid, 0)
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
        "                barrier.settimeout(None)  # held until the test's finally releases it\n"
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
        if self._testMethodName == "test_real_clear_handover_delivers_late_and_aggregate_records_from_the_original_store":
            self._env_patch.update({"CODEX_THREAD_ID": "", "CODEX_SESSION_ID": "",
                                    "AGENT_DISPATCH_CURRENT_HARNESS": "",
                                    "AGENT_DISPATCH_CALLER_HARNESS": ""})
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
        # A real `__ensure-obligations` child starts the shared obligation runner
        # outside the in-process patch; it keeps writing obligations/ until reaped.
        fixture_processes.reap(self._tmp.name)
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
    def test_recorded_publication_pending_delivery_is_light_for_all_carriers(self):
        import dispatch_terminal_commit as terminal
        import dispatch_supervision as supervision
        import artifact_producer
        fixture = _load("sweep_recorded_notice_fixture", HERE / "dispatch_notice_state.test.py")
        for carrier in ("claude-parent-runtime", "codex-native-queue", "opencode-turn"):
            with self.subTest(carrier=carrier):
                jobs, meta, request = fixture.recorded_owner_fixture(self.root / carrier, carrier=carrier)
                terminal._record_attempt(request, "producer-finalize-failed", "output-invalid: locator-invalid-component:artifacts/bad|name")
                notice = supervision.materialize(jobs, {"att-owner"}, reason="workflow-completion-pending")[0]
                with contextlib.ExitStack() as stack:
                    heavy = []
                    for module, names in ((terminal, ("owner_completion_state", "owner_completion_pending", "inspect_owner_completion",
                        "_completion_request", "_observe_settled_transaction", "_publication_observation")),
                        (artifact_producer, ("completed_spec_publication", "_code_spec_tree_changed", "_spec_bytes")),
                        (terminal._route_module(), ("terminal_gate_observation",))):
                        for name in names:
                            heavy.append(stack.enter_context(mock.patch.object(module, name, side_effect=AssertionError(name))))
                    reader = stack.enter_context(mock.patch.object(terminal, "recorded_owner_completion_notice", wraps=terminal.recorded_owner_completion_notice))
                    begin = time.monotonic()
                    started = SWEEP.activate(jobs.parent, carrier, "parent", request_recovery=False)
                    startup_text = SWEEP.delivery_context([(jobs.parent, started)])
                    self.assertIn("output-invalid", startup_text)
                    self.assertEqual(PD.read(jobs.parent, "parent", notice["delivery_id"])["state"], "pending")
                    reader.reset_mock()
                    records, _ = SWEEP.sweep_deliver(jobs.parent, carrier, "parent")
                    self.assertEqual(len(records), 1)
                    text = SWEEP.delivery_context([(jobs.parent, records)])
                    self.assertIn("artifacts/bad|name", text)
                    self.assertIn("HEARTING_RECEIPT_V1", text)
                    self.assertEqual(reader.call_count, 1)
                    self.assertEqual(SWEEP.ack_delivered(jobs.parent, "parent", records, acked_by="fixture-parent"), 1)
                    self.assertEqual(PD.read(jobs.parent, "parent", notice["delivery_id"])["state"], "acked")
                    for check in heavy:
                        check.assert_not_called()
                    print(f"recorded pending {carrier}: {time.monotonic() - begin:.4f}s; heavy=0; select/render=1")

    def test_activation_filters_stale_without_claiming_or_rejecting_and_retains_duties(self):
        self._seed()
        did = "delivery-" + "a" * 32
        before = PD.read(self.root, "sess-owner", did)
        with mock.patch("dispatch_notice_state.notice_is_current", return_value=False), \
             mock.patch("peer_obligations.retain_registered_completion") as retain, \
             mock.patch.object(SWEEP, "_reconcile_canonical_dispatch_state") as recover:
            self.assertEqual(SWEEP.activate(self.root, "claude-parent-runtime", "sess-owner"), [])
            retain.assert_called_once()
            recover.assert_called_once_with(self.root)
        self.assertEqual(PD.read(self.root, "sess-owner", did), before)

    def test_claude_hook_renders_recorded_pending_then_flushes_before_ack(self):
        import dispatch_terminal_commit as terminal
        import dispatch_supervision as supervision
        fixture = _load("claude_recorded_notice_fixture", HERE / "dispatch_notice_state.test.py")
        jobs, meta, request = fixture.recorded_owner_fixture(self.root)
        terminal._record_attempt(request, "output-invalid", "locator-invalid-component:artifacts/first|bad")
        notice = supervision.materialize(jobs, {"att-owner"}, reason="workflow-completion-pending")[0]
        class Output(io.StringIO):
            flushed = False
            def flush(self):
                self.flushed = True
        for event, expected_state in (("SessionStart", "pending"), ("UserPromptSubmit", "acked")):
            output = Output()
            observed = []
            def recovery(*, lifecycle_root=None):
                observed.append((output.flushed, PD.read(self.root, "parent", notice["delivery_id"])["state"]))
            begin = time.monotonic()
            with mock.patch.object(HOOK, "resolve_agent_home", return_value=ROOT), \
                 mock.patch.object(HOOK, "dispatch_state_roots", return_value=(self.root, self.root)), \
                 mock.patch.object(HOOK, "request_obligation_reconciliation", side_effect=recovery), \
                 mock.patch.object(terminal, "owner_completion_pending", side_effect=AssertionError("heavy selection")) as full, \
                 mock.patch.object(terminal, "inspect_owner_completion", side_effect=AssertionError("heavy render")) as inspect, \
                 mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"session_id": "parent", "hook_event_name": event}))), \
                 mock.patch.object(sys, "stdout", output):
                self.assertEqual(HOOK.main(), 0)
            body = json.loads(output.getvalue())["hookSpecificOutput"]
            self.assertEqual(body["hookEventName"], event)
            self.assertIn("first|bad", body["additionalContext"])
            self.assertEqual(observed, [(True, expected_state)])
            full.assert_not_called()
            inspect.assert_not_called()
            print(f"Claude {event} recorded pending hook: {time.monotonic() - begin:.4f}s; state={expected_state}; heavy=0")

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
                                         request_recovery=False, notice_context=mock.ANY)
        request.assert_called_once_with(lifecycle_root=ROOT)
        self.assertEqual(output.getvalue(), "")

    def test_cli_activation_root_failure_still_requests_detached_recovery(self):
        with mock.patch.object(SWEEP, "_state_roots", side_effect=OSError("fixture-roots-unreadable")), \
             mock.patch.object(SWEEP, "request_obligation_reconciliation") as request:
            with self.assertRaisesRegex(OSError, "fixture-roots-unreadable"):
                SWEEP.main(["activate", "--recipient-kind", "opencode-turn", "--session", "parent"])
        request.assert_called_once_with(lifecycle_root=ROOT)

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
            "immutable_identity": record.get(SWEEP.IDENTITY_KEY) or PD.received_ack_identity(
                record, record.get(SWEEP.STORAGE_KEY) or session_id, record["delivery_id"]),
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

    def test_non_codex_ack_never_resolves_a_queue_endpoint(self):
        self._seed()
        claimed, _ = SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-owner")
        with mock.patch("codex_queue_dispatch.resolve_queue_socket") as resolve:
            self.assertEqual(SWEEP.ack_delivered(
                self.root, "sess-owner", claimed, acked_by="claude-hook"), 1)
        resolve.assert_not_called()
        self.assertEqual(PD.read(
            self.root, "sess-owner", claimed[0]["delivery_id"])["state"], "acked")

    def _native_batch_records(self, count=2):
        serial = getattr(self, "_native_batch_serial", 0) + 1
        self._native_batch_serial = serial
        records = []
        for index in range(count):
            attempt = f"att-native-batch-{serial}-{index}"
            delivery_id = f"delivery-native-batch-{serial}-{index}"
            receipt = {"kind": "completion", "children": [{
                "attempt_id": attempt, "status": "done", "reason": "completed",
                "required_action": "advance-completed",
                "delivery_classification": "success",
            }], "job_registry": str(self.root / "jobs.log")}
            record = PD.create(
                root=self.root, recipient_kind="codex-native-queue", recipient_key="sess-owner",
                delivery_id=delivery_id, session_generation="",
                session_generation_supported="0", attempt_ids=[attempt],
                parent_attempt_id="att-parent", route_id="rt-native-batch", route_node="execute",
                receipt=receipt, receipt_digest=PD._canonical_receipt_digest(receipt),
                row_revisions={attempt: "revision"})
            records.append(PD.claim(
                self.root, "sess-owner", delivery_id, claim_owner="native-batch-test",
                lease_seconds=30.0, jobs=self.root / "jobs.log"))
        return records

    def _ack_native_with_fake_queue(self, records, queue, *, failure=None):
        import codex_queue_delivery as delivery
        socket_path = Path("/tmp/fake-native-batch.sock")
        rpc_calls = []
        def rpc(_path, method, params, **_kwargs):
            rpc_calls.append((method, params))
            if method == "thread/queue/list":
                return {"data": list(queue)}
            if method == "thread/queue/delete":
                if failure == "delete-raise":
                    raise delivery.QueueDeliveryError("fake-delete-failure")
                if failure == "delete-false":
                    return {"deleted": False}
                queue[:] = [item for item in queue if item["id"] != params["queuedSubmissionId"]]
                return {"deleted": True}
            raise AssertionError(method)
        resolver = (mock.patch("codex_queue_dispatch.resolve_queue_socket",
                              side_effect=RuntimeError("resolve failed"))
                    if failure == "resolve-raise" else
                    mock.patch("codex_queue_dispatch.resolve_queue_socket", return_value=socket_path))
        with resolver, \
             mock.patch.object(delivery, "DeliverySession") as session_type, \
             mock.patch.object(delivery, "_rpc", side_effect=rpc):
            session_type.return_value.__enter__.return_value = object()
            if failure == "connect-failure":
                session_type.return_value.__enter__.side_effect = delivery.QueueDeliveryError(
                    "fake-connect-failure")
            count = SWEEP.ack_delivered(self.root, "sess-owner", records, acked_by="native-test")
        return count, rpc_calls

    def test_ack_delivered_retires_fully_acked_batch_and_partial_batch_after_last_ack(self):
        import codex_queue_delivery as delivery
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "jobs.log").touch()
        # Fully ACKed batch: the post-ACK drain sees no item.
        full = self._native_batch_records()
        body = delivery.native_batch_unit_body(self.root, "sess-owner", full)
        full_id = delivery.native_client_message_id(
            "sess-owner", [record["delivery_id"] for record in full], batch=True)
        queue = [{"id": "submission-full", "clientUserMessageId": full_id,
                  "input": [{"type": "text", "text": body, "text_elements": []}]}]
        count, calls = self._ack_native_with_fake_queue(full, queue)
        self.assertEqual(count, 2)
        self.assertEqual(queue, [])
        self.assertEqual([call[1]["threadId"] for call in calls], ["sess-owner", "sess-owner"])

        # One sibling ACK cannot remove the shared batch; the final ACK can.
        partial = self._native_batch_records()
        body = delivery.native_batch_unit_body(self.root, "sess-owner", partial)
        client_id = delivery.native_client_message_id(
            "sess-owner", [record["delivery_id"] for record in partial], batch=True)
        queue = [{"id": "submission-partial", "clientUserMessageId": client_id,
                  "input": [{"type": "text", "text": body, "text_elements": []}]}]
        count, calls = self._ack_native_with_fake_queue(partial[:1], queue)
        self.assertEqual(count, 1)
        self.assertEqual(len(queue), 1)
        self.assertEqual(calls[0][1]["threadId"], "sess-owner")
        count, calls = self._ack_native_with_fake_queue(partial[1:], queue)
        self.assertEqual(count, 1)
        self.assertEqual(queue, [])
        self.assertEqual(calls[-1][1]["threadId"], "sess-owner")

    def test_ack_retirement_failures_preserve_ack_count_state_and_acknowledger(self):
        import codex_queue_delivery as delivery
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "jobs.log").touch()
        for failure in ("delete-false", "delete-raise", "resolve-raise", "connect-failure"):
            record = self._native_batch_records(count=1)[0]
            body = delivery.native_batch_unit_body(self.root, "sess-owner", [record])
            client_id = delivery.native_client_message_id(
                "sess-owner", [record["delivery_id"]], batch=True)
            queue = [{"id": f"submission-{failure}", "clientUserMessageId": client_id,
                      "input": [{"type": "text", "text": body, "text_elements": []}]}]
            count, _calls = self._ack_native_with_fake_queue([record], queue, failure=failure)
            stored = PD.read(self.root, "sess-owner", record["delivery_id"])
            self.assertEqual(count, 1, failure)
            self.assertEqual(stored["state"], "acked", failure)
            self.assertEqual(stored["acked_by"], "native-test", failure)
            self.assertEqual(len(queue), 1, failure)

    def test_ack_retirement_preserves_edited_wrong_digest_and_duplicate_client_items(self):
        import codex_queue_delivery as delivery
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "jobs.log").touch()
        for mutation in ("edited-body", "wrong-digest", "duplicate-client-id"):
            record = self._native_batch_records(count=1)[0]
            body = delivery.native_batch_unit_body(self.root, "sess-owner", [record])
            client_id = delivery.native_client_message_id(
                "sess-owner", [record["delivery_id"]], batch=True)
            if mutation == "edited-body":
                body += " edited"
            elif mutation == "wrong-digest":
                lines = body.splitlines()
                marker_index = next(i for i, line in enumerate(lines)
                                    if line.startswith("HEARTING_RECEIPT_V1 "))
                marker = lines[marker_index].split()
                marker[-1] = "0" * 64 if marker[-1] != "0" * 64 else "1" * 64
                lines[marker_index] = " ".join(marker)
                body = "\n".join(lines)
            item = {"id": f"submission-{mutation}", "clientUserMessageId": client_id,
                    "input": [{"type": "text", "text": body, "text_elements": []}]}
            queue = [item, dict(item, id=f"duplicate-{mutation}")] \
                if mutation == "duplicate-client-id" else [item]
            count, _calls = self._ack_native_with_fake_queue([record], queue)
            self.assertEqual(count, 1, mutation)
            self.assertTrue(queue, mutation)

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
                mock.patch.object(handover, "record_recipient_binding", return_value=("claude", "sess-new")), \
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

    def test_succession_only_notice_is_delivered_and_acked_under_its_original_key(self):
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        from session_notice import notify

        self.root.mkdir(parents=True, exist_ok=True)
        jobs = self.root / "jobs.log"
        jobs.touch()
        os.environ["AGENT_DISPATCH_JOBS"] = str(jobs)
        seat = tidy.Seat("pane", "succession-only-seat", pane="w1:pSuccession", harness="claude")
        ledger = tidy._ledger_path(seat)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        tidy._append_ledger_entry(seat, {
            "event": "succession", "source": "tidy-clear", "harness": "claude",
            "from": {"harness": "claude", "sid": "sess-old"}, "sid": "sess-new", "ts": 1,
        })
        notice = notify("claude", "sess-old", key="succession-notice", subject="run finished",
                        text="exit 0", jobs=jobs)
        self.assertIsNotNone(notice)
        with mock.patch.object(handover, "pane_seat", return_value=seat):
            records, count = SWEEP.sweep_deliver(
                self.root, "claude-parent-runtime", "sess-new")
            self.assertEqual(count, 1)
            self.assertEqual([record["delivery_id"] for record in records], [notice["delivery_id"]])
            self.assertEqual(records[0][SWEEP.STORAGE_KEY], "sess-old")
            self.assertEqual(SWEEP.ack_delivered(
                self.root, "sess-new", records, acked_by="successor-consumed"), 1)
            self.assertEqual(SWEEP.sweep_deliver(
                self.root, "claude-parent-runtime", "sess-new")[0], [])
        self.assertEqual(PD.read(self.root, "sess-old", notice["delivery_id"])["state"], "acked")
        self.assertIsNone(PD.read(self.root, "sess-new", notice["delivery_id"]))

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
                               return_value=[("sess-new", None), ("sess-old", bound)]), \
                mock.patch.object(handover, "record_recipient_binding",
                                  side_effect=lambda record, _storage, **_kwargs:
                                  ("claude", "sess-new" if set(record.get("attempt_ids") or ()) <= bound
                                  else "sess-old")):
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
            # An eligible, unbound depth-1 attempt follows the confirmed chain too;
            # its record is still claimed and acked under its original storage SID.
            self.assertEqual({record["delivery_id"] for record in addressed}, {
                "delivery-" + "a" * 32, "delivery-" + "b" * 32,
                "delivery-" + "c" * 32, "delivery-" + "d" * 32,
            })
            records, sweep_count = SWEEP.sweep_deliver(
                self.root, "claude-parent-runtime", "sess-new")
            self.assertEqual(sweep_count, 4)
            self.assertEqual({record["delivery_id"] for record in records}, {
                "delivery-" + "a" * 32, "delivery-" + "b" * 32,
                "delivery-" + "c" * 32, "delivery-" + "d" * 32,
            })
            self.assertEqual({record[SWEEP.STORAGE_KEY] for record in records}, {"sess-old"})
            self.assertEqual(SWEEP.ack_delivered(self.root, "sess-new", records, acked_by="clear-12-test"), 4)
            self.assertEqual(SWEEP.sweep_deliver(self.root, "claude-parent-runtime", "sess-new")[0], [])

        for suffix in "abcd":
            stored = PD.read(self.root, "sess-old", "delivery-" + suffix * 32)
            self.assertEqual(stored["state"], "acked")
            self.assertIsNone(PD.read(self.root, "sess-new", stored["delivery_id"]))
        self.assertEqual(PD.read(self.root, "sess-old", "delivery-" + "d" * 32)["state"], "acked")

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
                mock.patch.object(bridge, "card_context", return_value=("", "", None)), \
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

class SweepBatchBoundaryTest(IsolatedRootMixin, unittest.TestCase):
    """Real registered groups; only semantic readiness and native observation are fake."""

    _seed = SweepTest._seed
    SID = "sess-owner"
    KIND = "claude-parent-runtime"

    def settle_batch(self, size=2):
        self.KIND = "opencode-turn"
        self.seed_batch(size)
        jobs = self.root / "jobs.log"
        jobs.write_text(jobs.read_text().replace("parent_harness=claude", "parent_harness=opencode"))
        for did in self.ids:
            path = PD.record_path(self.root, self.SID, did)
            raw = json.loads(path.read_text())
            raw["recipient_kind"] = self.KIND
            path.write_text(json.dumps(raw))
        records, _count = self.sweep()
        self.assertEqual(len(records), size)
        self.identities = [PD.received_ack_identity(self.read(did), self.SID, did) for did in self.ids]
        return {"text": SWEEP.delivery_context([(self.root, records)]), "records": [
            {"root": str(self.root), "storage_key": self.SID, "delivery_id": record["delivery_id"],
             "claim_owner": record["claim_owner"], "immutable_identity": identity}
            for record, identity in zip(records, self.identities)], "messages": []}

    def settle(self, handed, *, roots=None):
        output = io.StringIO()
        with mock.patch.object(SWEEP, "_state_roots", return_value=roots or [self.root]), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(handed))), \
                contextlib.redirect_stdout(output):
            self.assertEqual(SWEEP.main(["settle", "--recipient-kind", self.KIND,
                                         "--session", self.SID]), 0)
        return json.loads(output.getvalue())

    def test_final_settle_holds_mixed_redirection_and_current_holder_recovers_original_key(self):
        import dispatch_seat_handover as handover
        for positions in ([0], [1], [0, 1]):
            with self.subTest(positions=positions):
                handed = self.settle_batch()
                seat = handover._st().Seat("pane", "settle-" + str(self._batch_serial))
                handover._st()._append_ledger_entry(seat, dict(event="handover", source="retire",
                    harness="opencode", sid="new-holder", **{"from": self.SID}, ts=1,
                    bindings=[handover.binding_of(self.root / "jobs.log", self.metas[i]) for i in positions]))
                result = self.settle(handed)
                self.assertEqual((result["records"], result["text"]), ([], ""))
                self.assertEqual([self.read(did)["state"] for did in self.ids], ["pending"] * 2)
                # Complete the handoff, then the existing sweep recovers the same group.
                handover._st()._append_ledger_entry(seat, dict(event="handover", source="retire",
                    harness="opencode", sid="new-holder", **{"from": self.SID}, ts=2,
                    bindings=[handover.binding_of(self.root / "jobs.log", meta) for meta in self.metas]))
                with self.retry_ready():
                    recovered, _ = self.sweep(session="new-holder")
                self.assertEqual([record["delivery_id"] for record in recovered], self.ids)
                self.assertEqual(SWEEP.ack_delivered(self.root, "new-holder", recovered,
                                                    acked_by="fixture-exact-receipt"), 2)
                self.assert_original()

    def test_final_settle_exact_receipt_precedes_unavailable_authority_and_ack_retry(self):
        import session_tidy as tidy
        handed = self.settle_batch()
        handed["messages"] = [{"info": {"sessionID": self.SID, "role": "user"},
                               "parts": [{"type": "text", "text": handed["text"]}]}]
        path = tidy._ledger_path(tidy.Seat("pane", "unavailable-final-settle"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad\n")
        with mock.patch.object(PD, "ack_received", side_effect=OSError("fixture ACK write failed")):
            first = self.settle(handed)
        self.assertEqual(first["received_ids"], self.ids)
        self.assertEqual((first["text"], first["records"], first["settled_count"]), ("", [], 0))
        second = self.settle(handed)
        self.assertEqual(second["settled_count"], 2)
        self.assertEqual([self.read(did)["state"] for did in self.ids], ["acked"] * 2)
        self.assert_original()

    def test_final_settle_resolves_each_records_original_registry_filename_and_root(self):
        import dispatch_seat_handover as handover
        for default in ("empty", "copied"):
            with self.subTest(default=default):
                self.seed_batch()
                old_root = self.root
                seeds = [self.read(did) for did in self.ids]
                self.root = old_root / "original-dispatch"
                self.root.mkdir()
                jobs = self.root / "retained-jobs.tsv"
                original_rows = (old_root / "jobs.log").read_text().replace(
                    "parent_harness=claude", "parent_harness=opencode")
                jobs.write_text(original_rows)
                default_jobs = old_root / "new-default-jobs.tsv"
                default_jobs.write_text(original_rows if default == "copied" else "")
                self.enterContext(mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(default_jobs)}))
                self.KIND = "opencode-turn"
                for seed in seeds:
                    body = {**seed["receipt"], "job_registry": str(jobs)}
                    PD.create(self.root, recipient_kind=self.KIND, recipient_key=self.SID,
                        delivery_id=seed["delivery_id"], session_generation="", session_generation_supported="0",
                        attempt_ids=seed["attempt_ids"], parent_attempt_id=seed["parent_attempt_id"],
                        route_id=seed["route_id"], route_node=seed["route_node"], receipt=body,
                        receipt_digest=PD._canonical_receipt_digest(body), row_revisions=seed["row_revisions"])
                claimed = PD.claim_ready_batch(self.root, self.SID, self.ids[0],
                    claim_owner="named-registry-carrier", lease_seconds=30, jobs=jobs)
                self.assertEqual(len(claimed), 2)
                handed = {"text": SWEEP.delivery_context([(self.root, [
                    {**record, SWEEP.STORAGE_KEY: self.SID} for record in claimed])]), "messages": [], "records": [
                    {"root": str(self.root), "storage_key": self.SID, "delivery_id": record["delivery_id"],
                     "claim_owner": record["claim_owner"], "immutable_identity": PD.received_ack_identity(
                         record, self.SID, record["delivery_id"])} for record in claimed]}
                self.assertEqual(len(self.settle(handed)["records"]), 2)
                self.assertEqual([record["delivery_id"] for record in SWEEP.addressed_records(
                    self.root, self.KIND, self.SID)[0]], self.ids)
                seat = handover._st().Seat("pane", "named-registry-" + default)
                handover._st()._append_ledger_entry(seat, dict(event="handover", source="retire",
                    harness="opencode", sid="new-holder", **{"from": self.SID}, ts=1,
                    bindings=[handover.binding_of(jobs, self.metas[0])]))
                self.assertEqual(handover.record_recipient_binding(self.read(self.ids[0]), self.SID,
                    harness="opencode", jobs=jobs), ("opencode", "new-holder"))
                self.assertEqual([record["delivery_id"] for record in SWEEP.addressed_records(
                    self.root, self.KIND, self.SID)[0]], self.ids[1:])
                settled = self.settle(handed)
                self.assertEqual((settled["text"], settled["records"]), ("", []))
                self.assertEqual([self.read(did)["state"] for did in self.ids], ["pending"] * 2)
                # An exact historical receipt still settles before unavailable
                # registry admission, even though its current holder moved.
                jobs.rename(jobs.with_suffix(".unavailable"))
                handed["messages"] = [{"info": {"sessionID": self.SID, "role": "user"},
                                       "parts": [{"type": "text", "text": handed["text"]}]}]
                received = self.settle(handed)
                self.assertEqual(received["received_ids"], self.ids)
                self.assertEqual(received["settled_count"], 2)

    def test_final_settle_holds_every_original_root_but_preserves_known_exact_receipts(self):
        handed = self.settle_batch(1)
        original = self.read(self.ids[0])
        other_root = self.root.parent / "other-dispatch"
        other_root.mkdir()
        other_jobs = other_root / "original-jobs.tsv"
        other_id = "delivery-other-original-root"
        other_jobs.write_text((self.root / "jobs.log").read_text().replace(self.ids[0], other_id))
        body = {**original["receipt"], "job_registry": str(other_jobs)}
        PD.create(other_root, recipient_kind=self.KIND, recipient_key=self.SID, delivery_id=other_id,
            session_generation="", session_generation_supported="0", attempt_ids=original["attempt_ids"],
            parent_attempt_id=original["parent_attempt_id"], route_id=original["route_id"],
            route_node=original["route_node"], receipt=body, receipt_digest=PD._canonical_receipt_digest(body),
            row_revisions=original["row_revisions"])
        other = PD.claim(other_root, self.SID, other_id, claim_owner="other-root-carrier",
                         lease_seconds=30, jobs=other_jobs)
        other_path = PD.record_path(other_root, self.SID, other_id)
        before = other_path.read_bytes()
        handed["text"] = SWEEP.delivery_context([
            (self.root, [{**original, SWEEP.STORAGE_KEY: self.SID}]),
            (other_root, [{**other, SWEEP.STORAGE_KEY: self.SID}])])
        handed["records"].append({"root": str(other_root), "storage_key": self.SID,
            "delivery_id": other_id, "claim_owner": other["claim_owner"],
            "immutable_identity": PD.received_ack_identity(other, self.SID, other_id)})
        # The later root observation omitted an original member. It cannot
        # authorize a partial new body or access/release the unadmitted root.
        result = self.settle(handed)
        self.assertEqual((result["text"], result["records"]), ("", []))
        self.assertIn(other_id, result["unknown_ids"])
        self.assertEqual(self.read(self.ids[0])["state"], "pending")
        self.assertEqual(other_path.read_bytes(), before)
        handed["messages"] = [{"info": {"sessionID": self.SID, "role": "user"},
                               "parts": [{"type": "text", "text": handed["text"]}]}]
        known = self.settle(handed)
        self.assertEqual(known["received_ids"], self.ids)
        self.assertEqual(self.read(self.ids[0])["state"], "acked")
        self.assertEqual(other_path.read_bytes(), before)
        recovered = self.settle(handed, roots=[self.root, other_root])
        self.assertEqual(set(recovered["received_ids"]), set(self.ids + [other_id]))
        self.assertEqual(PD.read(other_root, self.SID, other_id)["state"], "acked")

    def seed_batch(self, size=2):
        from route_identity import route_hash
        import dispatch_seat_handover as handover

        serial = getattr(self, "_batch_serial", 0) + 1
        self._batch_serial = serial
        base = Path(self._tmp.name) / f"case-{serial}"
        self.root = base / "dispatch"
        self.root.mkdir(parents=True)
        self.enterContext(mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(base / "state"),
            "AGENT_DISPATCH_JOBS": str(self.root / "jobs.log"),
        }))
        jobs = self.root / "jobs.log"
        jobs.touch()
        ids, metas = [], []
        for index in range(size):
            did = f"delivery-{serial:08x}{index:024x}"
            aid, rid = f"att-boundary-{serial}-{index}", f"rt-boundary-{serial}-{index}"
            route = {"route_id": rid, "parent_cycle_id": f"cycle-boundary-{serial}",
                     "nodes": [{"id": "frame"}]}
            route["route_hash"] = route_hash(route)
            path = self.root / f"{rid}.json"
            path.write_text(json.dumps(route), encoding="utf-8")
            meta = {"attempt_id": aid, "dispatch_depth": "1", "registered_worker": "1",
                    "worker_type": "frame", "parent_sid": self.SID, "parent_harness": "claude",
                    "delivery_intent": "1", "delivery_id": did, "failure_class": "pass",
                    "note": "completed-marker", "delivery_intent_at_ns": str(time.monotonic_ns()),
                    "launch_outcome": "reaped-before-publish", "route_file": str(path),
                    "route_id": rid, "route_hash": route["route_hash"], "route_node": "frame"}
            with jobs.open("a", encoding="utf-8") as handle:
                handle.write("2026-10-25T00:00:00Z\tdone\t/fixture/repo\t/fixture/wt\tfixture\t" +
                             ",".join(f"{key}={value}" for key, value in meta.items()) + "\n")
            receipt = _receipt(state="ready", parent_attempt_id="att-boundary-parent",
                               job_registry=str(jobs), delivery_classification="success",
                               children=[{"attempt_id": aid, "status": "done", "readiness": "ready",
                                          "required_action": "advance-completed",
                                          "delivery_classification": "success"}])
            self._seed(delivery_id=did, attempt_ids=[aid], parent_attempt_id="att-boundary-parent",
                       route_id=rid, route_node="frame", receipt=receipt,
                       row_revisions={aid: "fixture-revision"})
            ids.append(did)
            metas.append(meta)
        self.ids, self.metas = ids, metas
        self.identities = [PD.received_ack_identity(self.read(did), self.SID, did) for did in ids]
        handover._LINEAGE_GRAPH_CACHE.clear()
        from dispatch_notice_state import resolve_delivery_batch
        batch = resolve_delivery_batch(jobs, self.read(ids[0]), recipient_key=self.SID,
                                       delivery_root=self.root)
        self.assertFalse(batch["held"])
        self.assertEqual(batch["delivery_ids"], tuple(ids))
        return ids

    def read(self, did):
        return PD.read(self.root, self.SID, did)

    def move(self, indices, target="new-holder"):
        import dispatch_seat_handover as handover
        directory = Path(os.environ["XDG_STATE_HOME"]) / "hearting/session-tidy/sessions"
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "boundary-retire.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "handover", "source": "retire", "harness": "claude",
                                     "sid": target, "from": {"harness": "claude", "sid": self.SID},
                                     "ts": time.monotonic_ns(), "bindings": [handover.binding_of(
                                         self.root / "jobs.log", self.metas[i]) for i in indices]}) + "\n")

    def sweep(self, observer=None, *, session=None, continuation=None):
        with mock.patch("dispatch_notice_state.keep_claim", return_value=True), \
             mock.patch("dispatch_seat_handover.pane_seat", return_value=SimpleNamespace(
                 kind="pane", key="fixture-boundary-seat")), \
             mock.patch("dispatch_supervision.continue_for_parent", side_effect=continuation or (
                 lambda *_a, **_k: None)):
            return SWEEP.sweep_deliver(self.root, self.KIND, session or self.SID,
                                       receipt_observer=observer)

    def later_observer(self, action, *, trigger=None, evidence=None):
        trigger = trigger or self.ids[-1]
        observed, acted = [], []
        def observe(_root, _storage, record):
            observed.append(record["delivery_id"])
            # The first seed observation is before the group claim. Only
            # mutate while the real common group owns all these records.
            if (record["delivery_id"] == trigger and not acted
                    and all(self.read(did)["state"] == "claimed" for did in self.ids)):
                acted.append(True)
                action()
            return evidence() if evidence else None
        return observe, observed, acted

    def retry_ready(self):
        # Advance only the courier's existing wall-clock retry interval.
        deadlines = []
        for did in self.ids:
            path = PD.courier_retry_path(PD.record_path(self.root, self.SID, did))
            deadlines.append(json.loads(path.read_text())["retry"]["next_attempt_at_ns"]
                             if path.exists() else 0)
        return mock.patch.object(PD.time, "time_ns", return_value=max(deadlines) + 1)

    def assert_original(self):
        for did, identity in zip(self.ids, self.identities):
            self.assertEqual(PD.received_ack_identity(self.read(did), self.SID, did), identity)
            self.assertIsNone(PD.read(self.root, "new-holder", did))

    def test_all_current_groups_and_singleton_keep_original_identities(self):
        for size in (1, 2, 4):
            with self.subTest(size=size):
                ids = self.seed_batch(size)
                records, count = self.sweep()
                self.assertEqual(count, size)
                self.assertEqual([r["delivery_id"] for r in records], ids)
                self.assertEqual([r[SWEEP.STORAGE_KEY] for r in records], [self.SID] * size)
                self.assertEqual([r[SWEEP.IDENTITY_KEY] for r in records], self.identities)
                self.assert_original()

    def test_native_observation_cannot_leave_any_group_position_stale(self):
        for size, target, trigger in ((1, 0, 0), (2, 0, 1), (2, 1, 0), (4, 0, 3), (4, 2, 3)):
            with self.subTest(size=size, target=target, trigger=trigger):
                ids = self.seed_batch(size)
                observe, observed, acted = self.later_observer(
                    lambda: self.move([target]), trigger=ids[trigger])
                records, count = self.sweep(observe)
                self.assertTrue(acted)
                self.assertIn(ids[trigger], observed)
                self.assertEqual(count, size)
                self.assertEqual(records, [])
                self.assertEqual([self.read(d)["state"] for d in ids], ["pending"] * size)
                self.assert_original()

    def test_continuation_change_holds_group_and_singleton_at_output(self):
        for size in (1, 3):
            with self.subTest(size=size):
                ids = self.seed_batch(size)
                acted = []
                def continuation(record, **_kwargs):
                    if record["delivery_id"] == ids[-1]:
                        acted.append(True)
                        self.move([0])
                records, _ = self.sweep(continuation=continuation)
                self.assertTrue(acted)
                self.assertEqual(records, [])
                self.assertEqual([self.read(d)["state"] for d in ids], ["pending"] * size)
                self.assert_original()

    def test_whole_group_recovers_for_current_recipient_after_late_handover(self):
        ids = self.seed_batch(3)
        observe, _observed, acted = self.later_observer(lambda: self.move(range(3)))
        self.assertEqual(self.sweep(observe)[0], [])
        self.assertTrue(acted)
        with self.retry_ready():
            self.assertEqual(self.sweep()[0], [])
            records, _ = self.sweep(session="new-holder")
        self.assertEqual([r["delivery_id"] for r in records], ids)
        self.assertEqual(SWEEP.ack_delivered(self.root, "new-holder", records,
                                            acked_by="fixture-received"), 3)
        self.assertEqual([self.read(d)["state"] for d in ids], ["acked"] * 3)
        self.assertEqual(self.sweep(session="new-holder")[0], [])
        self.assert_original()

    def test_late_native_evidence_settles_earlier_member_and_holds_remainder(self):
        import dispatch_recipient_receipt as receipt
        for ack_failure in (False, True):
            with self.subTest(ack_failure=ack_failure):
                ids = self.seed_batch(3)
                evidence = [None]
                def received():
                    evidence[0] = receipt.claude_feedback_evidence([{
                        "type": "user", "sessionId": self.SID, "summary": "Stop hook feedback",
                        "message": {"content": receipt.receipt_marker(self.read(ids[0]), self.SID)},
                    }], self.SID)
                observe, _observed, acted = self.later_observer(received, evidence=lambda: evidence[0])
                with contextlib.ExitStack() as stack:
                    if ack_failure:
                        stack.enter_context(mock.patch.object(PD, "ack_received", side_effect=OSError("fixture ACK failure")))
                    records, _ = self.sweep(observe)
                self.assertTrue(acted)
                self.assertEqual(records, [])
                self.assertEqual([self.read(d)["state"] for d in ids],
                                 ["pending" if ack_failure else "acked", "pending", "pending"])
                with self.retry_ready():
                    remaining, _ = self.sweep(lambda *_a: evidence[0])
                self.assertEqual([r["delivery_id"] for r in remaining], ids[1:])
                self.assertEqual(self.read(ids[0])["state"], "acked")
                self.assert_original()

    def test_late_claim_owner_change_preserves_other_carrier_and_releases_remainder(self):
        ids = self.seed_batch(3)
        successor = []
        def transfer():
            current = self.read(ids[0])
            PD.reclaim(self.root, self.SID, ids[0], now_ns=current["claim_deadline_ns"] + 1)
            successor.append(PD.claim(self.root, self.SID, ids[0], claim_owner="other-carrier",
                                      lease_seconds=120, require_generation_proof=False))
        observe, _observed, acted = self.later_observer(transfer)
        self.assertEqual(self.sweep(observe)[0], [])
        self.assertTrue(acted)
        self.assertEqual(self.read(ids[0]), successor[0])
        self.assertEqual([self.read(d)["state"] for d in ids[1:]], ["pending", "pending"])
        self.assert_original()

    def test_historical_receipt_survives_late_claim_or_recipient_change(self):
        import dispatch_recipient_receipt as receipt
        for changed in ("recipient", "claim-owner", "lease"):
            with self.subTest(changed=changed):
                ids = self.seed_batch(3)
                evidence = [None]
                def received():
                    current = self.read(ids[0])
                    if changed == "recipient":
                        self.move([0])
                    elif changed == "claim-owner":
                        PD.reclaim(self.root, self.SID, ids[0], now_ns=current["claim_deadline_ns"] + 1)
                        PD.claim(self.root, self.SID, ids[0], claim_owner="other-carrier",
                                 lease_seconds=120, require_generation_proof=False)
                    else:
                        path = PD.record_path(self.root, self.SID, ids[0])
                        value = json.loads(path.read_text())
                        value["claim_deadline_ns"] = 0
                        path.write_text(json.dumps(value))
                    evidence[0] = receipt.claude_feedback_evidence([{
                        "type": "user", "sessionId": self.SID, "summary": "Stop hook feedback",
                        "message": {"content": receipt.receipt_marker(current, self.SID)},
                    }], self.SID)
                observe, _observed, acted = self.later_observer(received, evidence=lambda: evidence[0])
                self.assertEqual(self.sweep(observe)[0], [])
                self.assertTrue(acted)
                self.assertEqual([self.read(d)["state"] for d in ids], ["acked", "pending", "pending"])
                self.assert_original()

    def test_later_observation_cannot_discard_an_already_observed_exact_receipt(self):
        import dispatch_recipient_receipt as receipt
        for later in ("unavailable", "empty", "foreign"):
            with self.subTest(later=later):
                ids = self.seed_batch(4)
                observed = []
                def observe(_root, _storage, record):
                    observed.append(record["delivery_id"])
                    if record["delivery_id"] == ids[2]:
                        return receipt.claude_feedback_evidence([{
                            "type": "user", "sessionId": self.SID, "summary": "Stop hook feedback",
                            "message": {"content": receipt.receipt_marker(self.read(ids[0]), self.SID)},
                        }], self.SID)
                    if record["delivery_id"] == ids[3] and later != "unavailable":
                        return receipt.claude_feedback_evidence([], "foreign-session" if later == "foreign" else self.SID)
                    return None
                self.assertEqual(self.sweep(observe)[0], [])
                self.assertIn(ids[2], observed)
                self.assertIn(ids[3], observed)
                self.assertEqual([self.read(d)["state"] for d in ids], ["acked", "pending", "pending", "pending"])
                self.assert_original()

    def test_expired_or_invalid_late_lease_never_reaches_output(self):
        for deadline in (0, None, True, "future"):
            for size in (1, 3):
                with self.subTest(deadline=deadline, size=size):
                    ids = self.seed_batch(size)
                    def expire():
                        path = PD.record_path(self.root, self.SID, ids[0])
                        value = json.loads(path.read_text())
                        value["claim_deadline_ns"] = deadline
                        path.write_text(json.dumps(value))
                    observe, _observed, acted = self.later_observer(expire)
                    self.assertEqual(self.sweep(observe)[0], [])
                    self.assertTrue(acted)
                    # Ordinary singleton recovery keeps its established lease.
                    self.assertEqual([self.read(d)["state"] for d in ids],
                                     ["claimed"] if size == 1 else ["pending"] * size)
                    self.assert_original()

    def test_late_immutable_replacement_is_not_emitted_or_released(self):
        ids = self.seed_batch(3)
        changed = []
        def replace():
            path = PD.record_path(self.root, self.SID, ids[0])
            value = json.loads(path.read_text())
            value["row_revisions"] = {self.metas[0]["attempt_id"]: "replacement-revision"}
            path.write_text(json.dumps(value))
            changed.append(path.read_bytes())
        observe, _observed, acted = self.later_observer(replace)
        self.assertEqual(self.sweep(observe)[0], [])
        self.assertTrue(acted)
        self.assertEqual(PD.record_path(self.root, self.SID, ids[0]).read_bytes(), changed[0])
        self.assertEqual([self.read(d)["state"] for d in ids[1:]], ["pending", "pending"])

    def test_native_settlement_during_later_observation_holds_partial_group(self):
        ids = self.seed_batch(3)
        def settle():
            PD.ack_received(self.root, self.SID, ids[0], expected_identity=self.identities[0],
                            acked_by="fixture-other-native-receipt")
        observe, _observed, acted = self.later_observer(settle)
        self.assertEqual(self.sweep(observe)[0], [])
        self.assertTrue(acted)
        self.assertEqual([self.read(d)["state"] for d in ids], ["acked", "pending", "pending"])
        with self.retry_ready():
            remaining, _ = self.sweep()
        self.assertEqual([r["delivery_id"] for r in remaining], ids[1:])
        self.assert_original()

    def test_native_observation_budget_releases_whole_group_for_next_sweep(self):
        ids = self.seed_batch(3)
        clock = [0]
        observe, _observed, acted = self.later_observer(lambda: clock.__setitem__(0, 7_000_000_000))
        with mock.patch.object(SWEEP, "time", SimpleNamespace(monotonic_ns=lambda: clock[0], time_ns=time.time_ns)):
            records, _ = self.sweep(observe)
        self.assertTrue(acted)
        self.assertEqual(records, [])
        self.assertEqual([self.read(d)["state"] for d in ids], ["pending"] * 3)
        with self.retry_ready():
            recovered, _ = self.sweep()
        self.assertEqual([r["delivery_id"] for r in recovered], ids)
        self.assert_original()

    def test_submitted_earlier_member_is_held_without_requeue(self):
        for submitted in ("sent-ambiguous", "prepared", "accepted"):
            with self.subTest(submitted=submitted):
                ids = self.seed_batch(3)
                saved = []
                def submit():
                    current = self.read(ids[0])
                    if submitted == "sent-ambiguous":
                        PD.mark_sent_ambiguous(self.root, self.SID, ids[0], claim_owner=current["claim_owner"])
                    else:
                        identity = PD.submission_fence_identity(current, self.SID)
                        PD.prepare_submission_fence(self.root, self.SID, ids[0],
                                                    claim_owner=current["claim_owner"], expected_identity=identity)
                        if submitted == "accepted":
                            PD.record_submission_accepted(self.root, self.SID, ids[0],
                                                          claim_owner=current["claim_owner"],
                                                          expected_identity=identity,
                                                          queue_submission_id="fixture-queue")
                    saved.append(PD.record_path(self.root, self.SID, ids[0]).read_bytes())
                observe, _observed, acted = self.later_observer(submit)
                self.assertEqual(self.sweep(observe)[0], [])
                self.assertTrue(acted)
                self.assertEqual(PD.record_path(self.root, self.SID, ids[0]).read_bytes(), saved[0])
                self.assertEqual([self.read(d)["state"] for d in ids[1:]], ["pending", "pending"])
                self.assertEqual(self.sweep()[0], [])
                self.assert_original()


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

    def _compute_notice(self, run_id, host, code=0, session_id=None):
        import session_notice
        text = (f"run {run_id} on {host} ended with exit {code}"
                f"; read it with: compute-hosts tail {run_id}")
        row = session_notice.notify(
            "opencode", session_id or self.SID, key=f"compute-run:{run_id}", subject="compute run",
            text=text, required_action="report-to-user", jobs=self.root / "jobs.log")
        return {**row, SWEEP.STORAGE_KEY: session_id or self.SID}

    def test_compute_run_notices_bundle_deterministically_and_preserve_each_marker(self):
        records = [self._compute_notice(
            f"gpu-a-20261024-12000{i}-resnet-e4-w4", "gpu-a") for i in (1, 2, 3)]
        records += [self._compute_notice(
            f"gpu-a-20261024-12000{i}-resnet-e4-w5", "gpu-a") for i in (4, 5)]
        records.append(self._compute_notice("gpu-a-20261024-120006-resnet-e4-w4", "gpu-a", 2))
        forward = SWEEP.delivery_context([(self.root, records)])
        reverse = SWEEP.delivery_context([(self.root, list(reversed(records)))])
        self.assertEqual(forward, reverse)
        self.assertIn("compute run summary: 3 runs on gpu-a label=resnet-e4-w4", forward)
        self.assertIn("exit 0:3", forward)
        self.assertIn("compute-hosts tail gpu-a-20261024-120003-resnet-e4-w4", forward)
        self.assertEqual(forward.count("HEARTING_RECEIPT_V1 "), len(records))
        self.assertIn("exit 2", forward)
        self.assertIn("resnet-e4-w5", forward)
        self.assertEqual(forward.count("run gpu-a-20261024-120004-resnet-e4-w5"), 1)

    def test_idle_opencode_finds_and_acks_notice_under_lineage_predecessor(self):
        predecessor = "ses-oc-before-clear"
        record = self._compute_notice("gpu-a-20261024-130000-resnet-e4", "gpu-a",
                                      session_id=predecessor)
        sessions = Path(os.environ["XDG_STATE_HOME"]) / "hearting" / "session-tidy" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "pane-ledger.jsonl").write_text(json.dumps({
            "event": "succession", "source": "tidy-clear", "harness": "opencode",
            "from": predecessor, "sid": self.SID,
        }) + "\n", encoding="utf-8")
        import dispatch_seat_handover as handover
        seat = SimpleNamespace(kind="pane", key="fixture-pane")
        with mock.patch.object(handover, "pane_seat", return_value=seat):
            addressed, _count = SWEEP.addressed_records(self.root, "opencode-turn", self.SID)
            self.assertEqual([item["delivery_id"] for item in addressed], [record["delivery_id"]])
            claimed, _count = SWEEP.sweep_deliver(self.root, "opencode-turn", self.SID)
            self.assertEqual([item["delivery_id"] for item in claimed], [record["delivery_id"]])
            self.assertEqual(SWEEP.ack_delivered(self.root, self.SID, claimed,
                                                 acked_by="fixture-opencode"), 1)
        stored = PD.read(self.root, predecessor, record["delivery_id"])
        self.assertEqual(stored["state"], "acked")

    def test_plugin_refreshes_changed_lineage_and_expires_negative_owed_cache(self):
        predecessor = "ses-oc-cache-origin"
        sessions = Path(os.environ["XDG_STATE_HOME"]) / "hearting" / "session-tidy" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        ledger = sessions / "pane-cache.jsonl"
        ledger.write_text(json.dumps({"event": "succession", "source": "tidy-clear",
                                      "harness": "opencode", "from": predecessor,
                                      "sid": self.SID}) + "\n", encoding="utf-8")
        body = r'''
const sid = "ses-oc-parent";
        const ledger = `${process.env.XDG_STATE_HOME}/hearting/session-tidy/sessions/pane-cache.jsonl`;
        const out = {env: {}};
        await hooks["shell.env"]({sessionID: sid}, out);
await hooks.event({event: {type: "session.idle", properties: {sessionID: sid}}});
await new Promise(r => setTimeout(r, 250));
writeFileSync(ledger, JSON.stringify({event: "succession", source: "tidy-clear",
  harness: "opencode", from: "ses-oc-cache-origin", sid: "ses-oc-next"}) + "\n");
await hooks.event({event: {type: "session.idle", properties: {sessionID: sid}}});
await new Promise(r => setTimeout(r, 250));
assert.equal(prompts.length, 0, "a cached predecessor digest survived a lineage generation change");
const created = childProcess.spawnSync("python3", ["-c", `import os,sys; from pathlib import Path; ` +
  `sys.path.insert(0, os.path.join(os.environ["AGENT_HOME"], "utilities")); ` +
  `import session_notice; session_notice.notify("opencode", "${sid}", ` +
  `key="cache-expiry", subject="cache", text="arrived", jobs=Path(os.environ["HARNESS_STATE_ROOT"])/"jobs.log")`],
  {encoding: "utf8"});
assert.equal(created.status, 0, created.stderr);
await new Promise(r => setTimeout(r, 1350));
await hooks.event({event: {type: "session.idle", properties: {sessionID: sid}}});
for (let i = 0; i < 100 && !prompts.length; i++) await new Promise(r => setTimeout(r, 50));
''';
        const_result = self.run_plugin(body)
        self.assertEqual(len(const_result["prompts"]), 1,
                         f"negative cache expiry did not reveal the new record: {const_result['logs']}")

    def test_internal_recipients_action_returns_lineage_digest_and_generation(self):
        output = io.StringIO()
        graph = SimpleNamespace(generation=("/isolated/sessions", (("a.jsonl", 1, 2, 3, 7, 9, 0o100600,
                                                                  "readable-bytes-digest"),), (1, 2, 3, 4, 5, 0o40700)))
        with mock.patch.object(SWEEP, "_state_roots", return_value=[self.root]), \
                mock.patch("dispatch_seat_handover.lineage_graph", return_value=graph), \
                mock.patch("dispatch_seat_handover.storage_recipients",
                           return_value=[(self.SID, None), ("ses-predecessor", frozenset())]), \
                mock.patch.dict(os.environ, {"HARNESS_STATE_ROOT": str(self.root)}), \
                contextlib.redirect_stdout(output):
            self.assertEqual(SWEEP.main(["recipients", "--recipient-kind", "opencode-turn",
                                         "--session", self.SID]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["digests"], sorted(hashlib.sha256(s.encode()).hexdigest()
                                                    for s in (self.SID, "ses-predecessor")))
        self.assertTrue(result["generation"])
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

    def test_v2_plugin_delivers_one_turn_and_settles_without_resend(self):
        """The V2 translation layer carries one opencode-turn record through the native
        prompt shape, settles it from session.context, and does not send it again."""
        self.v2_turn_carrier_case()

    def test_v2_execution_and_form_events_preserve_waits_and_one_delivery(self):
        self.v2_turn_carrier_case(interleave_events=True)

    def test_v2_carrier_withholds_unknown_native_identity_idle_and_changed_selection(self):
        for scenario in ("wrong-session", "child", "directory", "get-error", "history-error",
                         "wait-error", "missing-wait", "busy", "selection-at-second-wait"):
            with self.subTest(scenario=scenario):
                self.v2_turn_carrier_case(scenario)

    def v2_turn_carrier_case(self, scenario="success", *, interleave_events=False):
        self.seed_turn()
        plugin = ROOT / "adapters/opencode/plugins/hearting-guards.js"
        js = r'''
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
const plugin = (await import(pathToFileURL(PLUGIN))).default;
const prompts = [], logs = [], contextRows = [];
let releaseIdle, releaseSecond, secondProcessed = false;
const idleGate = new Promise((resolve) => { releaseIdle = resolve });
const secondGate = new Promise((resolve) => { releaseSecond = resolve });
const sessionID = "ses-oc-parent";
const scenario = SCENARIO;
const interleaveEvents = INTERLEAVE_EVENTS;
let releaseExecution, observedWait;
const executionGate = new Promise(resolve => { releaseExecution = resolve });
const firstWait = new Promise(resolve => { observedWait = resolve });
let waits = 0;
const hooks = new Map();
const ctx = {
  location: { directory: process.cwd(), project: { id: "fixture" } },
  app: { log: async (entry) => { logs.push(entry.body); } },
  session: {
    get: async ({ sessionID: sid }) => {
      if (scenario === "get-error") throw new Error("native read unavailable");
      return { id: scenario === "wrong-session" ? "other" : sid,
        projectID: "fixture", agent: "build", title: "Main work",
        ...(scenario === "child" ? {parentID: "parent"} : {}),
        time: { created: 1, updated: 1 },
        location: { directory: scenario === "directory" ? process.env.HOME : process.cwd() } };
    },
    wait: async ({ sessionID: sid }, { signal }) => {
      assert.equal(sid, sessionID);
      waits++;
      if (interleaveEvents && waits === 1) { observedWait(); await executionGate; }
      if (scenario === "wait-error") throw new Error("native idle unavailable");
      if (scenario === "busy") await new Promise((_, reject) =>
        signal.addEventListener("abort", () => reject(new Error("native wait aborted")), {once: true}));
      if (scenario === "selection-at-second-wait" && waits === 2) {
        carrierFs.writeFileSync(`${selection}/${process.pid}-${ownStart}.json`, JSON.stringify({
          schema: "hearting-tui-selection-v1", sessionID: "other", pid: process.pid, start: ownStart }));
      }
    },
    prompt: async (request) => {
      if (!request || typeof request.text !== "string" || request.prompt) {
        throw new Error("PromptInput requires top-level text");
      }
      prompts.push(request);
      contextRows.push({ id: "msg_fixture", time: { created: Date.now() },
                         type: "user", text: request.text });
    },
    context: async () => {
      if (scenario === "history-error") throw new Error("native context unavailable");
      return contextRows;
    },
    hook: async (name, fn) => hooks.set("session." + name, fn),
  },
  tool: { hook: async (name, fn) => hooks.set("tool." + name, fn) },
  shell: { hook: async (name, fn) => hooks.set("shell." + name, fn) },
  event: { subscribe: async function* ({ signal }) {
    await idleGate;
    yield { type: "session.execution.succeeded", data: { sessionID } };
    if (interleaveEvents) {
      // A new execution arrives while the retained delivery is awaiting native idle.
      await firstWait;
      yield { type: "session.execution.started", data: { sessionID } };
      releaseExecution();
      for (let i = 0; i < 200 && !carrierLog("skip", "native-admission-withheld"); i++) {
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      assert.ok(carrierLog("skip", "native-admission-withheld"), JSON.stringify(logs));
      assert.equal(prompts.length, 0, "execution start must stop the in-flight delivery");
      const sidecar = `${process.env.XDG_STATE_HOME}/agent-fleet/interactions/opencode/${sessionID}.json`;
      for (const close of ["form.replied", "form.cancelled"]) {
        const id = "form-" + close;
        yield { type: "form.created", data: { form: { sessionID, id,
          questions: [{ question: "PRIVATE-PROMPT", options: ["PRIVATE-CHOICE"] }] } } };
        for (let i = 0; i < 200 && !carrierFs.existsSync(sidecar); i++) {
          await new Promise(resolve => setTimeout(resolve, 50));
        }
        const observation = JSON.parse(carrierFs.readFileSync(sidecar, "utf8"));
        assert.equal(observation.session_id, sessionID);
        assert.equal(observation.request_id, id);
        assert.equal(observation.source, "opencode-form");
        assert.doesNotMatch(JSON.stringify(observation), /PRIVATE-|questions|options/);
        assert.equal(prompts.length, 0, "an open form cannot admit the retained delivery");
        yield { type: close, data: { sessionID, id, answers: ["PRIVATE-ANSWER"] } };
        for (let i = 0; i < 200 && carrierFs.existsSync(sidecar); i++) {
          await new Promise(resolve => setTimeout(resolve, 50));
        }
        assert.equal(carrierFs.existsSync(sidecar), false, "the exact form must close its wait");
      }
      yield { type: "session.execution.failed", data: { sessionID } };
    }
    await secondGate;
    yield { type: "session.execution.succeeded", data: { sessionID } };
    secondProcessed = true;
    await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
  } },
  permission: { hook: async (name, fn) => hooks.set("permission." + name, fn) },
};
if (scenario === "missing-wait") delete ctx.session.wait;
const cleanup = await plugin.setup(ctx);
const carrierLog = (stage, reason) => logs.some((row) =>
  row?.service === "hearting-completion-carrier" && row.extra?.stage === stage
  && row.extra?.reason === reason);
try {
  await hooks.get("tool.execute.before")({ tool: "shell", sessionID, input: { command: "launch" } });
  const env = {};
  await hooks.get("shell.create.before")({ command: "launch", env });
  assert.equal(env.AGENT_PARENT_COMPLETION_CARRIER, "opencode-turn:" + sessionID);
  releaseIdle();
  if (scenario !== "success") {
    const reason = scenario === "history-error" ? "native-history-unavailable"
      : scenario === "selection-at-second-wait" ? "final-admission-withheld-released"
      : "native-admission-withheld";
    for (let i = 0; i < 200 && !carrierLog("skip", reason); i++) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    assert.ok(carrierLog("skip", reason), JSON.stringify(logs));
    assert.equal(prompts.length, 0, "unknown native evidence or another selection cannot admit a prompt");
    if (scenario === "selection-at-second-wait") assert.equal(waits, 2);
    console.log(JSON.stringify({ prompts, logs }));
  } else {
  for (let i = 0; i < 200 && !carrierLog("settle", "native-receipt"); i++) {
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.ok(carrierLog("settle", "native-receipt"),
            "the V2 carrier did not settle from session.context evidence: " + JSON.stringify(logs));
  assert.equal(prompts.length, 1, "one record must produce exactly one prompt");
  assert.equal(prompts[0].sessionID, sessionID);
  assert.equal(prompts[0].delivery, "steer");
  assert.equal(typeof prompts[0].text, "string");
  assert.ok(prompts[0].text.includes("att-0000000000000000000000000000bbbb"));
  releaseSecond();
  for (let i = 0; i < 100 && !secondProcessed; i++) {
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.equal(secondProcessed, true, "the second idle was not processed");
  await new Promise((resolve) => setTimeout(resolve, 1000));
  assert.equal(prompts.length, 1, "a settled turn was delivered again on the next idle");
  console.log(JSON.stringify({ prompts, logs }));
  }
} finally { await cleanup(); }
'''.replace("PLUGIN", json.dumps(str(plugin))).replace("SCENARIO", json.dumps(scenario)).replace(
            "INTERLEAVE_EVENTS", json.dumps(interleave_events))
        env = self.env()
        js = self.admitted_plugin_script(js)
        run = subprocess.run([self.native_node(), "--input-type=module", "-e", js], env=env, cwd=str(self.root),
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        self.assertEqual(self.state(), "acked" if scenario == "success" else "pending")
        result = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(len(result["prompts"]), int(scenario == "success"))
        if scenario == "success":
            self.assertNotIn("prompt", result["prompts"][0])

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

    def native_node(self):
        native_bin = self.root / "native-node"
        native_bin.mkdir(exist_ok=True)
        native_node = native_bin / "opencode"
        if not native_node.exists():
            native_node.symlink_to(shutil.which("node"))
        return str(native_node)

    def admitted_plugin_script(self, script):
        # A private Node process with a synthetic process-bound TUI selection
        # and native SDK responses. The production plugin/root reader is exact.
        prelude = r'''
import * as carrierFs from "node:fs";
const ownStat = carrierFs.readFileSync("/proc/self/stat", "utf8");
const ownStart = ownStat.slice(ownStat.lastIndexOf(") ") + 2).trim().split(/\s+/)[19];
const selection = `${process.env.XDG_STATE_HOME}/hearting/tui-identity`;
carrierFs.mkdirSync(selection, {recursive: true});
carrierFs.writeFileSync(`${selection}/${process.pid}-${ownStart}.json`, JSON.stringify({
  schema: "hearting-tui-selection-v1", sessionID: "ses-oc-parent", pid: process.pid, start: ownStart,
}));
'''
        api = '''
  get: async () => ({data: {id: "ses-oc-parent", projectID: "fixture", directory: process.cwd(),
    slug: "fixture", title: "fixture", version: "1", time: {created: 1, updated: 1}},
    response: {ok: true, status: 200}}),
  status: async () => ({data: {}, response: {ok: true, status: 200}}),
'''
        return prelude + script.replace("const session = {", "const session = {" + api, 1)

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
        run = subprocess.run([self.native_node(), "--input-type=module", "-e", self.admitted_plugin_script(js)],
                             env=env, cwd=str(self.root),
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

    def test_actual_transport_revalidates_after_helper_stdout_before_prompt(self):
        """Mutate real lineage after the asynchronous helper has flushed its result.

        The imported committed plugin and both transport implementations are
        exact. Only the private helper entrypoint adds deterministic scheduling.
        """
        ids = self.seed_cycle_batch()
        runtime = self.root.parent / "transport-runtime"
        (runtime / "utilities").mkdir(parents=True)
        for name in ("core", "adapters", "hooks", "tools"):
            (runtime / name).symlink_to(ROOT / name, target_is_directory=True)
        for path in (ROOT / "utilities").iterdir():
            if path.name != "dispatch_session_sweep.py":
                (runtime / "utilities" / path.name).symlink_to(path, target_is_directory=path.is_dir())
        helper = runtime / "utilities/dispatch_session_sweep.py"
        helper.write_text(
            "import sys\nfrom pathlib import Path\n"
            + "sys.path.insert(0, " + repr(str(ROOT / "utilities")) + ")\n"
            + "import dispatch_session_sweep as sweep\nimport session_tidy as tidy\n"
            + "code = sweep.main(sys.argv[1:])\n"
            + "marker = Path(" + repr(str(runtime / "changed")) + ")\n"
            + "if sys.argv[1] == 'settle' and not marker.exists():\n"
            + "    marker.touch()\n"
            + "    tidy._append_ledger_entry(tidy.Seat('pane', 'transport-successor'), "
            + repr(dict(event="succession", source="tidy-clear", harness="opencode",
                        sid="ses-oc-current-C", **{"from": self.SID}, ts=1)) + ")\n"
            + "sys.exit(code)\n")
        result = self.run_plugin(self.IDLE_THEN_WAIT.replace(
            'row.extra?.reason === "native-receipt-before-send"',
            '["native-receipt-before-send", "effect-boundary-withheld"].includes(row.extra?.reason)'),
            extra_env={"AGENT_HOME": str(runtime)})
        self.assertTrue((runtime / "changed").exists())
        self.assertEqual(result["prompts"], [])
        self.assertTrue(any(row.get("extra", {}).get("reason") == "effect-boundary-withheld"
                            for row in result["logs"]))
        self.assertEqual([PD.read(self.root, self.SID, did)["state"] for did in ids], ["pending"] * 2)
        with mock.patch("dispatch_seat_handover.pane_seat", return_value=SimpleNamespace(
                kind="pane", key="transport-successor")):
            current, _ = SWEEP.sweep_deliver(self.root, "opencode-turn", "ses-oc-current-C")
        self.assertEqual([record["delivery_id"] for record in current], ids)
        self.assertEqual(SWEEP.ack_delivered(self.root, "ses-oc-current-C", current,
                                            acked_by="fixture-exact-receipt"), 2)
        self.assertTrue(all(PD.read(self.root, "ses-oc-current-C", did) is None for did in ids))

    def test_retained_run_notice_uses_busy_to_idle_prompt_async_and_exact_native_receipt(self):
        import importlib.util
        import session_notice
        from peer_obligations import ObligationStore
        jobs = self.root / "jobs.log"
        with mock.patch("peer_obligations.ensure_normal_runner", return_value=False), \
                mock.patch("peer_obligations.request_dispatch_reconciliation", return_value=False):
            notice = session_notice.notify("opencode", self.SID, key="checkpoint:run-1:slot-1",
                subject="experiment checkpoint", text="run=run-1; ETA total 2h",
                route_id="route-fixture", jobs=jobs)
        duty, = ObligationStore(self.root).list(kinds={"run-notice"})
        body = r'''
const out = {env: {}};
await hooks["shell.env"]({sessionID: "ses-oc-parent"}, out);
await hooks.event({event: {type: "session.status", properties: {
  sessionID: "ses-oc-parent", status: {type: "busy"}}}});
await new Promise(r => setTimeout(r, 100));
if (prompts.length) throw new Error("busy OpenCode session was prompted");
await hooks.event({event: {type: "session.idle", properties: {sessionID: "ses-oc-parent"}}});
for (let i = 0; i < 200 && !prompts.length; i++) await new Promise(r => setTimeout(r, 50));
hooks.dispose();
console.log(JSON.stringify({prompts, logs}));
'''
        result = self.run_plugin(body)
        self.assertEqual(len(result["prompts"]), 1)
        sent_text = result["prompts"][0]["body"]["parts"][0]["text"]
        self.assertIn(SWEEP.NOTICE_DELIVERY_HEADER, sent_text)
        self.assertIn("run=run-1", sent_text)
        stored = PD.read(self.root, self.SID, notice["delivery_id"])
        self.assertEqual(stored["state"], "acked")
        self.assertEqual(stored["attempt_ids"], [])

        spec = importlib.util.spec_from_file_location(
            "run_notice_steward", ROOT / "utilities" / "peer-steward.py")
        steward = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(steward)
        store = ObligationStore(self.root)
        steward._reconcile_obligations(store,
            duties=steward._runner_duties(store, [duty]))
        self.assertEqual(store.get(duty["id"])["state"], "complete")

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
        first_body = _previous_native_body(first_body)
        native = [{"info": {"sessionID": self.SID, "role": "user"},
                   "parts": [{"type": "text", "text": first_body}]}]

        result = self.cli("deliver", json.dumps({"messages": native}))

        self.assertEqual(result["records"], [])
        self.assertEqual(result["text"], "")
        self.assertEqual(PD.read(self.root, self.SID, did)["state"], "acked")
        self.assertEqual(PD.read(self.root, self.SID, did)["attempts"], 2)

    def test_completion_notice_original_body_survives_refusal_changes_ack_retry_and_reclaim(self):
        import dispatch_supervision as supervision
        import dispatch_terminal_commit as terminal
        fixture = _load("opencode_recorded_notice_fixture", HERE / "dispatch_notice_state.test.py")
        jobs, meta, request = fixture.recorded_owner_fixture(self.root, carrier="opencode-turn", session=self.SID)
        terminal._record_attempt(request, "producer-finalize-failed", "output-invalid: locator-invalid-component:artifacts/first|bad")
        notice = supervision.materialize(jobs, {"att-owner"}, reason="workflow-completion-pending")[0]
        handed = self.deliver_in_process()
        did = notice["delivery_id"]
        self.assertIn("first|bad", handed["text"])
        self.assertNotIn(SWEEP.NOTICE_CONTEXT_KEY, json.dumps(handed))
        # The native carrier still has its exact handed body after diagnosis changes.
        terminal._record_attempt(request, "output-invalid", "locator-invalid-component:artifacts/second|bad")
        path = terminal._commit_state_path(request)
        state = json.loads(path.read_text())
        state["state"] = "route-closed"
        path.write_text(json.dumps(state))
        self._expire_ambiguous([did])
        PD.claim(self.root, self.SID, did, claim_owner="second-carrier", lease_seconds=30, require_generation_proof=False)
        native = [{"info": {"sessionID": self.SID, "role": "user"},
                   "parts": [{"type": "text", "text": handed["text"]}]}]
        with mock.patch.object(PD, "ack_received", side_effect=PD.PendingDeliveryError("fixture-ack-failed")):
            failed_ack = self.settle_at(handed, time.monotonic_ns(), messages=native)
        self.assertEqual(failed_ack["received_ids"], [did])
        self.assertEqual(failed_ack["records"], [])
        self.assertEqual(PD.read(self.root, self.SID, did)["state"], "claimed")
        retried = self.settle_at(handed, time.monotonic_ns(), messages=native)
        self.assertEqual(retried["received_ids"], [did])
        self.assertEqual(retried["records"], [])
        self.assertEqual((PD.read(self.root, self.SID, did)["state"], PD.read(self.root, self.SID, did)["attempts"]), ("acked", 2))

    def test_previous_completion_notice_wording_requires_the_exact_historical_body(self):
        import dispatch_supervision as supervision
        import dispatch_terminal_commit as terminal
        fixture = _load("opencode_previous_notice_fixture", HERE / "dispatch_notice_state.test.py")
        jobs, meta, request = fixture.recorded_owner_fixture(self.root, carrier="opencode-turn", session=self.SID)
        terminal._record_attempt(request, "output-invalid", "locator-invalid-component:artifacts/first|bad")
        notice = supervision.materialize(jobs, {"att-owner"}, reason="workflow-completion-pending")[0]
        handed = self.deliver_in_process()
        previous = _previous_native_body(handed["text"]).replace("The last recorded closure refusal is ", "The latest closure refusal is ")
        native = [{"info": {"sessionID": self.SID, "role": "user"},
                   "parts": [{"type": "text", "text": previous + " forged"}]}]
        self.assertEqual(SWEEP._opencode_delivery_expectations([self.root], "opencode-turn", self.SID, native), [])
        native[0]["parts"][0]["text"] = previous
        result = self.deliver_in_process(messages=native)
        self.assertEqual(result["records"], [])
        self.assertEqual(PD.read(self.root, self.SID, notice["delivery_id"])["state"], "acked")

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
        first_body = _previous_native_body(first_body)
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
        prior_body = _previous_native_body(prior_body)
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
                            "\nprior next-action handle\n\n" +
                            SWEEP.NOTICE_DELIVERY_HEADER + "\nquoted payload header")
        with mock.patch.object(SWEEP, "_followup", return_value=current_followup):
            current = SWEEP.delivery_context([(self.root, [{**record, SWEEP.STORAGE_KEY: self.SID}])])
            previous = current.replace(SWEEP.COMPLETION_DELIVERY_HEADER,
                                       _FROZEN_COMPLETION_DELIVERY_HEADER, 1)
            legacy = current.replace(SWEEP.COMPLETION_DELIVERY_HEADER,
                                     _FROZEN_LEGACY_COMPLETION_DELIVERY_HEADER, 1).replace(
                                         SWEEP._CURRENT_RUNTIME_V1_FOLLOWUP_PREFIX,
                                         _FROZEN_LEGACY_RUNTIME_V1_FOLLOWUP, 1)
            for version, body in (("current", current),
                                  ("previous-short", previous), ("legacy-long", legacy)):
                with self.subTest(version=version):
                    messages = [{"info": {"sessionID": self.SID, "role": "user"},
                                 "parts": [{"type": "text", "text": body}]}]
                    accepted = SWEEP._opencode_delivery_expectations(
                        [self.root], "opencode-turn", self.SID, messages)
                    self.assertEqual(len(accepted), 1)
                    self.assertEqual(accepted[0]["text"], body)
                    for altered in (body + " ", body.replace("prior next-action handle", "tampered"),
                                    body.replace(SWEEP.NOTICE_DELIVERY_HEADER,
                                                 _FROZEN_NOTICE_DELIVERY_HEADER, 1)):
                        messages[0]["parts"][0]["text"] = altered
                        self.assertEqual(SWEEP._opencode_delivery_expectations(
                            [self.root], "opencode-turn", self.SID, messages), [])

    def test_previous_mixed_headers_settle_exact_native_receipts_without_resend(self):
        import session_notice
        for version, legacy in (("previous-short", False), ("legacy-long", True)):
            with self.subTest(version=version):
                suffix = "a" if legacy else "b"
                completion_id = "delivery-" + suffix * 32
                gate_id = "delivery-" + ("c" if legacy else "d") * 32
                self.seed_turn(delivery_id=completion_id)
                self.seed_turn(delivery_id=gate_id, receipt={
                    "kind": "human-gate", "route_id": "rt-example", "route_file": "/r/route.json",
                    "owner_attempt_id": "att-owner", "gate": "frame-review",
                    "artifact_path": "/art/frame-summary.md"})
                notice = session_notice.notify(
                    "opencode", self.SID, key="mixed:" + version, subject="run finished",
                    text="run=fixture", jobs=self.root / "jobs.log")
                ids = [completion_id, gate_id, notice["delivery_id"]]
                records = [{**self._mark_sent_ambiguous(did), SWEEP.STORAGE_KEY: self.SID}
                           for did in ids]
                followup = SWEEP._CURRENT_RUNTIME_V1_FOLLOWUP_PREFIX + "\nprior next-action handle"
                with mock.patch.object(SWEEP, "_followup", return_value=followup):
                    current = SWEEP.delivery_context([(self.root, records)])
                    previous = _previous_native_body(current, legacy=legacy)
                    native = [{"info": {"sessionID": self.SID, "role": "user"},
                               "parts": [{"type": "text", "text": previous}]}]
                    self._expire_ambiguous(ids)
                    result = self.deliver_in_process(messages=native)
                    self.assertEqual(result["records"], [])
                    self.assertEqual(result["text"], "")
                    self.assertEqual(self.deliver_in_process(messages=native)["text"], "")
                for did in ids:
                    record = PD.read(self.root, self.SID, did)
                    self.assertEqual((record["state"], record["attempts"]), ("acked", 1))

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
        prior_body = _previous_native_body(prior_body)
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
        prior_body = _previous_native_body(prior_body)
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
        prior_body = _previous_native_body(prior_body)
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
// The delivery chain spawns Python children; wait for its settle record, not a fixed 1.5 s.
for (let i = 0; i < 200 && !logs.some(row => row.extra?.stage === "settle"); i++)
  await new Promise(r => setTimeout(r, 50));
''')
        self.assertEqual(len(result["prompts"]), 1)
        self.assertEqual(result["prompts"][0]["path"], {"id": self.SID})
        self.assertEqual(self.state(), "acked")

    def test_existing_selected_session_restores_without_a_creation_event(self):
        self.seed_turn()
        result = self.run_plugin('''
if (prompts.length) throw new Error("startup context was treated as receipt");
await hooks.event({event: {type: "session.idle", properties: {sessionID: "ses-oc-parent"}}});
// The delivery chain spawns Python children; wait for its settle record, not a fixed 1.5 s.
for (let i = 0; i < 200 && !logs.some(row => row.extra?.stage === "settle"); i++)
  await new Promise(r => setTimeout(r, 50));
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
                node = subprocess.Popen([self.native_node(), "--input-type=module", "-e", self.admitted_plugin_script(script)],
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
        node = subprocess.Popen([self.native_node(), "--input-type=module", "-e", self.admitted_plugin_script(js)],
                                env=self.env(), cwd=str(self.root),
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
             mock.patch.object(bridge, "card_context", return_value=("fixture context", "", None)), \
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
