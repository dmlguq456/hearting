#!/usr/bin/env python3
"""Unit tests for utilities/peer-steward.py (SD-122 (9) steward wait/start)."""
import concurrent.futures
import contextlib
import copy
import errno
import fcntl
import hashlib
import io
import importlib.util
import json
import math
import multiprocessing
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from split_module_fixture import patch_split, split_patch  # noqa: E402

_HERE = Path(__file__).resolve().parent
# Real capture fixtures: codex-cli 0.162.1 via tmux capture-pane, 80x40 and
# 40x30, alt-screen and inline launches; README records each file's provenance.
_CODEX_REVIEW_FIXTURES = _HERE / "fixtures" / "codex-0.162.1-hook-review"


def _queue_error(*args, **kwargs):
    from codex_queue_delivery import QueueDeliveryError
    return QueueDeliveryError(*args, **kwargs)


def _codex_snapshot(name):
    text = (_CODEX_REVIEW_FIXTURES / name).read_text()
    separators = [index for index, row in enumerate(text.splitlines()) if row == "---"]
    return "\n".join(text.splitlines()[separators[1] + 1:]) if len(separators) >= 2 else text


def _codex_capture(name):
    text = (_CODEX_REVIEW_FIXTURES / name).read_text()
    body, delimiter, _tail = text.rpartition("# capture fixture delimiter\n")
    if not delimiter:
        raise AssertionError(f"capture fixture delimiter missing: {name}")
    return body


_SPEC = importlib.util.spec_from_file_location("peer_steward", str(_HERE / "peer-steward.py"))
peer_steward = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(peer_steward)
sys.path.insert(0, str(_HERE.parent / "tools"))
sys.path.insert(0, str(_HERE))
import fixture_processes  # noqa: E402
import claude_process_identity  # noqa: E402
terminal_surface = peer_steward.terminal_surface


def _register_completion_fixture(jobs, attempt, metadata):
    """Materialize a private registered completion through the ordinary row API."""
    import dispatch_contract as contract
    import dispatch_completion_join as join

    route = jobs.with_name(attempt + "-route.json")
    metadata = dict(attempt_schema_version="2", dispatch_depth="1", transport="headless",
        execution_surface="registered-headless", registered_worker="1",
        fallback_hop="same-harness-headless", worker_type="owner", harness="codex",
        attempt_id=attempt, route_hash="sha256:" + "a" * 64, route_file=str(route), **metadata)
    route.write_text(json.dumps({key: metadata[key] for key in ("route_id", "route_hash")}))
    pipe = ",".join(key + "=" + value for key, value in metadata.items())
    if not contract.claim_attempt_row(jobs, attempt, f"now\tdone\t{jobs.parent}\t-\tfixture\t{pipe}"):
        raise AssertionError("private completion fixture was not registered")
    return join.current_attempt_row(jobs, attempt)


class _TmpRootMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_root = Path(self._tmp.name)
        self.jobs_path = self.tmp_root / "jobs.log"
        self.jobs_path.touch()
        self._old_environ = dict(os.environ)
        for key in tuple(os.environ):
            if key.startswith(("AGENT_", "HERDR_", "OPENCODE_", "CLAUDE_", "CODEX_", "XDG_")):
                os.environ.pop(key, None)
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs_path)
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.tmp_root)
        artifacts = self.tmp_root / "artifacts"
        cycle_dir = artifacts / "cycle"
        output_dir = cycle_dir / "artifacts"
        runtime_dir = self.tmp_root / "runtime"
        for path in (output_dir, runtime_dir):
            path.mkdir(parents=True, exist_ok=True)
        os.environ["AGENT_ARTIFACT_ROOT"] = str(artifacts)
        os.environ["AGENT_ARTIFACT_CYCLE_DIR"] = str(cycle_dir)
        os.environ["AGENT_ARTIFACT_OUTPUT_DIR"] = str(output_dir)
        os.environ["AGENT_ARTIFACT_CAMPAIGN_ID"] = "campaign-fixture"
        os.environ["AGENT_ARTIFACT_CYCLE_ID"] = "cycle-fixture"
        os.environ["AGENT_ARTIFACT_PRODUCER_ID"] = "producer-fixture"
        os.environ["AGENT_RUNTIME_DIR"] = str(runtime_dir)
        fixture_store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        self.assertTrue(fixture_store.ensure_directory(),
                        "fixture starts with an existing obligations root")
        # `steward_marker_roots()`'s fleet-reader branch also adds
        # `stable_state_root(os.environ)` and every installed runtime's own root
        # (~/.codex, ~/.claude, ~/.config/opencode) as read candidates; without
        # isolating HOME too those fall through to this machine's real roots.
        os.environ["HOME"] = str(self.tmp_root / "home")
        # A worker runtime's XDG_CONFIG_HOME points at runtime-owned config
        # without the test's scoped files; pin it to the isolated HOME so
        # config-home lookups stay inside the fixture.
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp_root / "home" / ".config")
        os.environ["XDG_DATA_HOME"] = str(self.tmp_root / "home" / ".local" / "share")
        os.environ["XDG_CACHE_HOME"] = str(self.tmp_root / "home" / ".cache")
        os.environ["XDG_STATE_HOME"] = str(self.tmp_root / "home" / ".local" / "state")
        os.environ.pop("HARNESS_STATE_ROOT", None)
        os.environ.pop("CODEX_HOME", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("AGENT_HOME", None)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        os.environ.pop("CODEX_THREAD_ID", None)
        os.environ.pop("AGENT_SESSION_ID", None)
        os.environ.pop("AGENT_HERDR_SESSION", None)
        self._old_herdr_session = peer_steward._HERDR_SESSION
        peer_steward._HERDR_SESSION = None
        self._original_ensure_normal_runner = peer_steward.peer_obligations.ensure_normal_runner
        runner_patch = mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner", return_value=False)
        runner_patch.start()
        self.addCleanup(runner_patch.stop)
        # Existing peer tests use already admitted fixture panes. The shared
        # admission is tested against real ptys in pane_ownership.test.py.
        import pane_ownership
        proof = mock.patch.object(pane_ownership, "verified_pane", side_effect=lambda pane, *a, **kw: pane or "")
        proof.start()
        self.addCleanup(proof.stop)
        self.addCleanup(lambda: setattr(peer_steward, "_HERDR_SESSION", self._old_herdr_session))
        census_patch = mock.patch.object(
            claude_process_identity, "claude_session_census",
            return_value=(False, set(), {"fixture-census-incomplete"}),
        )
        census_patch.start()
        self.addCleanup(census_patch.stop)
        self.addCleanup(self._restore_environ)

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self._old_environ)

    def _all_records(self):
        root = peer_steward.peer_message.peer_state_root() / "peer-messages"
        if not root.is_dir():
            return []
        recs = []
        for month in root.glob("*"):
            for f in month.glob("*.jsonl"):
                for line in f.read_text().splitlines():
                    if line.strip():
                        recs.append(json.loads(line))
        return recs


def _hold_receipt_transition_locks(mode, jobs_lock, pane, server, socket_path,
                                   ready, jobs_released, release, hold_seconds):
    """Hold the cooperating writer locks in a real process for receipt tests."""
    import dispatch_seat_handover

    fd = None
    jobs_locked = False
    seat_lock = None
    try:
        if mode in {"jobs", "both"}:
            fd = os.open(jobs_lock, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            jobs_locked = True
        if mode in {"seat", "both"}:
            seat_lock = dispatch_seat_handover.readiness_transition_lock(
                pane, server, socket_path)
            seat_lock.__enter__()
        ready.set()
        if mode == "both":
            time.sleep(hold_seconds)
            fcntl.flock(fd, fcntl.LOCK_UN)
            jobs_locked = False
            jobs_released.set()
            release.wait(5)
        elif mode == "jobs":
            time.sleep(hold_seconds)
            fcntl.flock(fd, fcntl.LOCK_UN)
            jobs_locked = False
            jobs_released.set()
        else:
            release.wait(5)
    finally:
        if seat_lock is not None:
            seat_lock.__exit__(None, None, None)
        if jobs_locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
        if fd is not None:
            os.close(fd)
        jobs_released.set()


def _herdr_json(payload):
    """Measured herdr 0.8.0 shape: success -> stdout + exit 0; an `error` payload
    -> STDERR + exit 1.  The fixture must reproduce the split, or a stdout-only
    reader passes every test and still misclassifies every real timeout."""
    if isinstance(payload, dict) and "error" in payload:
        return subprocess.CompletedProcess(["herdr"], 1, stdout="", stderr=json.dumps(payload))
    return subprocess.CompletedProcess(["herdr"], 0, stdout=json.dumps(payload), stderr="")


def _idle_shell_run(argv, **kwargs):
    if argv[:3] == ["herdr", "pane", "process-info"]:
        return _herdr_json({"result": {"process_info": {
            "pane_id": "w1:pM", "shell_pid": 101, "foreground_process_group_id": 101,
            "foreground_processes": [{"pid": 101, "argv": ["zsh"]}],
        }}})
    return _herdr_json({"result": {"pane": {}}})


class CompletionConsumerCorrectionTest(_TmpRootMixin, unittest.TestCase):
    def test_message_uses_saved_server_for_readiness_and_transport_and_restores_on_error(self):
        store = peer_steward.peer_obligations.ObligationStore()
        ident = {"server": "secondary", "pane": "w1:p9", "harness": "claude",
                 "session_id": "sid-peer"}
        duty = store.create("message-server", "message", ident,
                            {"target": "peer", "ref": "r" * 32})
        row = {"state": "pending", "to": {**ident, "name": "peer"}, "created": time.time()}
        calls = []

        def target(_target):
            calls.append(("resolve", peer_steward._HERDR_SESSION))
            return "idle", ident, 0, None

        def ready(*a, **kw):
            calls.append(("readiness", peer_steward._HERDR_SESSION))
            return SimpleNamespace(state="ready", reason="native-idle")

        def flush(*a, **kw):
            calls.append(("flush", peer_steward._HERDR_SESSION))
            row["state"] = "received"

        peer_steward._HERDR_SESSION = "caller"
        with mock.patch.object(peer_steward.peer_message, "_read_pending", side_effect=lambda ref: row), \
             mock.patch.object(peer_steward, "_retire_target", side_effect=target), \
             mock.patch.object(peer_steward, "_prompt_current_candidate", return_value=(
                 {"state": "idle", "harness": "claude", "session_id": "sid-peer",
                  "pane": "w1:p9", "_identity": ident,
                  "_native_session_id": "sid-peer"}, None)), \
             mock.patch.object(peer_steward, "_pane_readiness", side_effect=ready), \
             mock.patch.object(peer_steward, "_flush_pending_for_target", side_effect=flush):
            peer_steward._resume_message_obligation(duty, store)
            self.assertEqual(calls, [(stage, "secondary") for stage in ("resolve", "readiness")])
            self.assertEqual(store.get(duty["id"])["state"], "pending")
            self.assertEqual(row["state"], "pending")
            self.assertEqual(peer_steward._HERDR_SESSION, "caller")
            row["state"] = "pending"
            with mock.patch.object(peer_steward, "_pane_readiness", side_effect=RuntimeError("probe")):
                with self.assertRaises(RuntimeError):
                    peer_steward._resume_message_obligation(duty, store)
            self.assertEqual(peer_steward._HERDR_SESSION, "caller")

    def test_herdr_sidless_readiness_accepts_only_the_session_its_process_proves(self):
        observed = ("idle", {"harness": "codex", "session_id": "-", "pane": "w1:pX"})
        info = {"shell_pid": 123, "foreground_processes": [{"pid": 456}]}
        obligations = peer_steward.peer_obligations
        for expected, verified, session in ((None, True, "proven-thread"),
                                            ("proven-thread", True, "proven-thread"),
                                            ("other-thread", False, "-")):
            with self.subTest(expected=expected), \
                 mock.patch.object(peer_steward, "_proven_session", return_value="proven-thread"), \
                 patch_split(peer_steward, "_proc_start_ticks", return_value="birth"), \
                 mock.patch.object(obligations, "execution_attempts_for_processes", return_value=()), \
                 mock.patch.object(obligations, "bound_work_for_pane", return_value=((), "observed")), \
                 mock.patch.object(obligations, "pane_readiness", side_effect=lambda **kw: kw):
                readiness = peer_steward._pane_readiness(
                    "w1:pX", "idle", expected_harness="codex", expected_sid=expected,
                    purpose="input", target_observation=observed, pane_info=info)
            self.assertEqual(dict(readiness["provenance"])["observed_state"], "idle")
            self.assertEqual((readiness["identity_verified"], readiness["session_id"]),
                             (verified, session))

    def test_readiness_forwards_the_runner_pass_context_to_bound_work(self):
        from peer_read_context import PeerReadContext

        context = PeerReadContext(registry_append_only=True)
        identity = {"harness": "codex", "session_id": "sid-peer", "pane": "w1:p9"}
        info = {"shell_pid": 101, "foreground_processes": []}
        with mock.patch.object(peer_steward, "_retire_pane_info", return_value=info), \
             patch_split(peer_steward, "_proc_start_ticks", return_value="22"), \
             mock.patch.object(peer_steward.peer_obligations, "execution_attempts_for_processes",
                               return_value=()), \
             mock.patch.object(peer_steward.peer_obligations, "bound_work_for_pane",
                               return_value=((), "observed")) as bound, \
             mock.patch.object(peer_steward.peer_obligations, "pane_readiness",
                               return_value=SimpleNamespace(state="ready", reason="")):
            peer_steward._pane_readiness(
                "peer", "idle", target_observation=("idle", identity), pane_info=info,
                read_context=context)
        bound.assert_called_once()
        self.assertIs(bound.call_args.kwargs["read_context"], context)
        self.assertTrue(bound.call_args.kwargs["registry_append_only"])

    def test_delay_notice_is_minted_once_across_replaced_checkpoints(self):
        store = peer_steward.peer_obligations.ObligationStore()
        sender = {"harness": "codex", "session_id": "sid-sender", "name": None}
        duty = store.create("delay-" + "o" * 32, "message", {"server": "default"},
                            {"delay_notice_for": "o" * 32, "target": "sid-sender",
                             "body": "[notice] still pending", "from": {"harness": "claude"},
                             "to": sender})
        minted = []

        def prepare(*a, **kw):
            minted.append("n%031d" % len(minted))
            return "text", minted[-1]

        row = {"state": "pending", "to": sender, "created": time.time()}
        with mock.patch.object(peer_steward.peer_message, "prepare_peer_message", side_effect=prepare), \
             mock.patch.object(peer_steward.peer_message, "_read_pending", side_effect=lambda ref: row), \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex",
                               side_effect=lambda *_a, **_kw: row.update(state="queued")) as transport, \
             mock.patch.object(peer_steward, "_retire_target",
                               return_value=("idle", {"pane": "-"}, 0, None)):
            for _ in range(3):
                peer_steward._resume_message_obligation(store.get(duty["id"]), store)
        self.assertEqual(len(minted), 1)
        observation = store.get(duty["id"])["observation"]
        self.assertEqual((observation["ref"], observation["reason"]),
                         (minted[0], "awaiting-native-receiver"))
        transport.assert_called_once()

    def test_watch_completion_consumes_real_bound_readiness_but_working_remains_native(self):
        from dispatch_contract import (ObservedAttemptLiveness, claim_attempt_row,
                                       ATTEMPT_SCHEMA_VERSION, process_start_ticks)
        from peer_read_context import PeerReadContext
        class WatchExited(Exception):
            pass
        real_time = peer_steward.time

        class FixtureClock:
            """Advance only when this fixture delivers one runner event."""
            def __init__(self):
                self.reset()

            def reset(self):
                self.wall = real_time.time()
                self.monotonic_value = real_time.monotonic()

            def advance(self, seconds):
                self.wall += seconds
                self.monotonic_value += seconds

            def time(self):
                return self.wall

            def monotonic(self):
                return self.monotonic_value

            def time_ns(self):
                return int(self.wall * 1_000_000_000)

            def monotonic_ns(self):
                return int(self.monotonic_value * 1_000_000_000)

            def gmtime(self, seconds=None):
                return real_time.gmtime(self.wall if seconds is None else seconds)

            def __getattr__(self, name):
                # Keep sleep and other unrelated time operations real.
                return getattr(real_time, name)

        clock = FixtureClock()
        with patch_split(peer_steward, "time", clock):
            identity = {"harness": "codex", "session_id": "sid-peer", "pane": "w1:p9"}
            for process_state, native in (("live", "idle"), ("unverifiable", "done"), ("live", "working")):
                with self.subTest(process_state=process_state, native=native):
                    clock.reset()
                    self.jobs_path.write_text("")
                    metadata = (f"attempt_schema_version={ATTEMPT_SCHEMA_VERSION},dispatch_depth=1,"
                                "transport=headless,execution_surface=registered-headless,registered_worker=1,"
                                "fallback_hop=same-harness-headless,attempt_id=att-watch,parent_sid=sid-peer,"
                                "parent_harness=codex,harness=codex")
                    claim_attempt_row(self.jobs_path, "att-watch",
                                      f"now\topen\trepo\t-\tslug\t{metadata}", launch=True)
                    watch_id = process_state + native
                    paths = peer_steward._watch_paths(watch_id)
                    fd = peer_steward._open_lock(paths.lock)
                    args = SimpleNamespace(watch_id=watch_id, lock_fd=fd, server="secondary",
                        expected_harness="codex", expected_session_id="sid-peer", expected_pane="w1:p9",
                        target="peer", until=[native], timeout=500, rearm_count=0,
                        steward_harness="codex", steward_session_id="sender", steward_project="fixture",
                        armed_ts="now", rearmed_from=None, ref=[])
                    # Durable arms and wait observations use normalized identity,
                    # while Herdr's wire payload is normalized before persistence.
                    agent = {**identity, "name": "peer"}
                    arm = {
                        "schema_version": peer_steward._WATCH_SCHEMA, "watch_id": watch_id,
                        "target": "peer", "until": [native], "timeout": 500,
                        "deadline": peer_steward._watch_deadline(500),
                        "server": "secondary", "agent": agent,
                        "steward": {"harness": "codex", "session_id": "sender"},
                        "observer_protocol": 2,
                    }
                    original_deadline = copy.deepcopy(arm["deadline"])
                    self.assertIsNot(arm["deadline"], original_deadline)
                    peer_steward._write_json_atomic(paths.arm, arm)
                    # The child wait has already settled. This isolates this test to
                    # readiness consumption and event wakeup; launch/result recovery
                    # has its own collector and watcher coverage.
                    peer_steward._write_json_atomic(paths.observation, {
                        "schema_version": peer_steward._WATCH_SCHEMA, "watch_id": watch_id,
                        "state": "waiting", "reason": "native-wait-result-consumed",
                        "native_wait": {"state": "result", "agent_state": native,
                                        "agent": agent, "reason": native},
                    })
                    store = peer_steward.peer_obligations.ObligationStore()
                    incarnation = f"consumer-correction-{watch_id}"
                    shell_start = str(process_start_ticks(os.getpid()))
                    peer_steward.shared_readiness.publish_runner_incarnation(
                        store.root, os.getpid(), shell_start, incarnation)
                    checkpoints = []
                    runner_passes = []
                    runner_diagnostics = []
                    deadline_remaining = []
                    def release(_delay):
                        checkpoint = peer_steward._read_json(paths.observation)
                        checkpoints.append(checkpoint["reason"])
                        self.assertFalse(paths.receipt.exists())
                        self.jobs_path.write_text("")
                    transport = mock.Mock()
                    transport.fileno.return_value = 987654
                    transport.observe.return_value = {}
                    runner_transport = SimpleNamespace(
                        observe=lambda: SimpleNamespace(complete=False), close=lambda: None)
                    snapshot = {"watch_root": paths.arm.parent, "watches": ((watch_id,
                        peer_steward._read_json(paths.arm)),)}

                    def runner_readiness_pass():
                        runner_passes.append(len(runner_passes) + 1)
                        published = peer_steward._publish_watch_readiness(
                            store, snapshot,
                            read_context=PeerReadContext(registry_append_only=True),
                            incarnation=incarnation)
                        projection_identity = peer_steward._watch_readiness_identity(
                            peer_steward._read_json(paths.arm), native)
                        projection = (peer_steward.shared_readiness.target_result(
                            store.root, projection_identity) if projection_identity else None)
                        remaining_ms, remaining_state = peer_steward._watch_timeout_remaining_ms(arm)
                        logical_remaining = arm["deadline"]["monotonic_deadline"] - clock.monotonic()
                        deadline_remaining.append((remaining_ms, remaining_state, logical_remaining))
                        runner_diagnostics.append({"published": published,
                                                   "projection": projection,
                                                   "deadline_remaining": deadline_remaining[-1]})

                    def deliver_event(readers, _writers, _errors, _timeout=None):
                        if native != "working":
                            if len(runner_passes) == 1:
                                release(None)
                            clock.advance(0.05)
                            runner_readiness_pass()
                            # The real runner wakes the watcher through its
                            # existing SIGUSR1/self-pipe carrier after publishing.
                            return [readers[0]], [], []
                        return [], [], []

                    try:
                        with mock.patch.object(peer_steward.herdr_observation,
                                               "socket_path_for_endpoint", return_value="/fixture.sock"), \
                             mock.patch.object(peer_steward.herdr_observation,
                                               "ObservationTransport", return_value=transport), \
                             mock.patch.object(peer_steward, "_runner_readiness_transport",
                                               return_value=runner_transport), \
                             patch_split(peer_steward, "_watch_terminal_from_view", return_value=None), \
                             mock.patch.object(peer_steward.select, "select", side_effect=deliver_event), \
                             mock.patch.object(peer_steward, "_retire_target", return_value=(native, identity, 0, None)), \
                             mock.patch.object(peer_steward, "_retire_pane_info", return_value={
                                 "shell_pid": os.getpid(), "foreground_processes": []}), \
                             split_patch("dispatch_contract.observed_attempt_liveness", return_value=ObservedAttemptLiveness(
                                 "alive" if process_state == "live" else "unverifiable", "fixture", process_state, "fixture")), \
                             mock.patch.object(peer_steward.peer_obligations,
                                               "ensure_normal_runner"), \
                             mock.patch.object(peer_steward.os, "_exit", side_effect=WatchExited), \
                             mock.patch.object(peer_steward, "_record"):
                            with self.assertRaises(WatchExited):
                                peer_steward.cmd_watch_run(args)
                        self.assertEqual(peer_steward._read_json(paths.receipt)["state"], native)
                        if native == "working":
                            self.assertEqual(checkpoints, [])
                            self.assertEqual(runner_passes, [])
                        else:
                            suffix = "pending" if process_state == "live" else "unknown"
                            self.assertEqual(checkpoints, ["bound-registered-work-" + suffix],
                                             f"runner passes={runner_passes}; diagnostics={runner_diagnostics}")
                            self.assertEqual(len(runner_passes), 2,
                                             f"the persisted refresh request is handled in the initial and changed passes: "
                                             f"{runner_diagnostics}")
                            projection_states = [
                                (item.get("projection") or {}).get("state")
                                for item in runner_diagnostics
                            ]
                            self.assertEqual(projection_states,
                                             ["pending" if process_state == "live" else "unknown",
                                              "ready"],
                                             f"the second pass must consume changed registry authority: "
                                             f"{runner_diagnostics}")
                            self.assertEqual(arm["timeout"], 500)
                            self.assertEqual(arm["deadline"]["requested_ms"], 500)
                            self.assertEqual(arm["deadline"], original_deadline)
                            self.assertEqual([row[1] for row in deadline_remaining],
                                             ["active", "active"])
                            clock_ulp = max(
                                math.ulp(abs(arm["deadline"]["monotonic_deadline"])),
                                math.ulp(abs(clock.monotonic())),
                            )
                            self.assertTrue(all(
                                abs(row[2] - expected) <= 2 * clock_ulp
                                for row, expected in zip(deadline_remaining, (0.45, 0.4))),
                                f"the two events must leave 450ms then 400ms within two absolute-clock ULPs "
                                f"(ULP={clock_ulp}): {deadline_remaining}")
                            logical_remaining = [row[2] for row in deadline_remaining]
                            self.assertLessEqual(
                                abs((logical_remaining[0] - logical_remaining[1]) - 0.05),
                                2 * clock_ulp,
                                f"the two runner events must remain 50ms apart within two absolute-clock "
                                f"ULPs (ULP={clock_ulp}): {deadline_remaining}")
                            self.assertTrue(all(
                                0 <= row[0] <= 500 for row in deadline_remaining),
                                f"remaining-time observations must stay within the original budget: "
                                f"{deadline_remaining}")
                    finally:
                        os.close(fd)

        self.assertIs(peer_steward.time, real_time)


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
        # F-100c: one `agent get` resolves the target's session id for the ledger record;
        # the WAIT itself is still exactly one herdr call — no poll loop.
        waits = [c[0][0] for c in run_mock.call_args_list if c[0][0][:3] == ["herdr", "agent", "wait"]]
        self.assertEqual(len(waits), 1)
        self.assertEqual(run_mock.call_args[0][0][:3], ["herdr", "agent", "wait"])

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

    def test_wait_on_a_mistyped_target_records_but_never_marks(self):
        """Review round 1 #2: the old code marked before herdr answered, so one typo was a
        permanent bold-yellow badge over an empty steward strip."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-innocent"
        payload = {"error": {"code": "agent_not_found"}}
        with mock.patch("builtins.print"), \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)):
            self.assertEqual(self._wait(target="no-such-target"), 2)
        self.assertEqual(len(self._all_records()), 1)
        self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})
        self.assertFalse(peer_steward.peer_message.steward_marker_path("claude", "sid-innocent").exists())

    def test_wait_marks_the_caller_only_after_a_real_target_answered(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        with mock.patch("builtins.print"), \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               return_value=_herdr_json(_agent_json("codex", "thread-1", "fleet-cycle2"))):
            self.assertEqual(self._wait(), 0)
        markers = peer_steward.peer_message.read_steward_markers()
        self.assertEqual(set(markers), {("claude", "sid-steward")})
        entry = markers[("claude", "sid-steward")]["targets"]["thread-1"]
        self.assertEqual((entry["harness"], entry["session_id"], entry["kind"], entry["source"]),
                         ("codex", "thread-1", "watch", "watch"))
        # a timeout still names a real target (herdr resolved it), so it counts
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward-2"
        with mock.patch("builtins.print"), \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               return_value=_herdr_json({"error": {"code": "timeout"}})):
            self.assertEqual(self._wait(), 3)
        self.assertIn(("claude", "sid-steward-2"), peer_steward.peer_message.read_steward_markers())
        # herdr missing: record, no flag
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward-3"
        with mock.patch("builtins.print"), mock.patch.object(peer_steward.shutil, "which", return_value=None):
            self.assertEqual(self._wait(), 4)
        self.assertNotIn(("claude", "sid-steward-3"), peer_steward.peer_message.read_steward_markers())

    def test_wait_marker_uses_resolved_name_and_pane_then_completes_with_sid(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        sidless = _agent_json("codex", "bad/id", "hearting-1d")
        identified = _agent_json("codex", "sid-new", "hearting-1d")
        with mock.patch("builtins.print"), \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward, "_resolve_target",
                               return_value=("codex", None, "raw-pane-alias")), \
             mock.patch.object(peer_steward, "_run_herdr_wait",
                               side_effect=[sidless, identified]):
            self.assertEqual(self._wait(target="raw-pane-alias"), 0)
            markers = peer_steward.peer_message.read_steward_markers()
            entries = markers[("claude", "sid-steward")]["targets"]
            self.assertEqual(set(entries), {"hearting-1d"})
            self.assertEqual(entries["hearting-1d"], {
                "harness": "codex", "session_id": None, "name": "hearting-1d",
                "kind": "watch", "ts": entries["hearting-1d"]["ts"],
                "source": "watch", "pane": "w1:pX"})
            self.assertEqual(self._wait(target="raw-pane-alias"), 0)
        entries = peer_steward.peer_message.read_steward_markers()[("claude", "sid-steward")]["targets"]
        self.assertEqual(set(entries), {"sid-new"})
        self.assertEqual((entries["sid-new"]["session_id"], entries["sid-new"]["name"],
                          entries["sid-new"]["pane"]), ("sid-new", "hearting-1d", "w1:pX"))

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
        waits = [c[0][0] for c in run_mock.call_args_list if c[0][0][:3] == ["herdr", "agent", "wait"]]
        self.assertEqual(len(waits), 1)                      # F-100c: + one `agent get`, never a loop

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


def _agent_start_cmd(run_mock):
    """The `herdr agent start …` argv among every herdr call the wrapper makes.

    `start` now also asks herdr about the pane and re-establishes the launcher ingress in
    it, so "the last call" is no longer the launch. Selecting by what the call IS keeps
    these assertions about the launch instead of about call ordering.
    """
    for call in run_mock.call_args_list:
        argv = call[0][0]
        if isinstance(argv, list) and argv[:3] == ["herdr", "agent", "start"]:
            return argv
    raise AssertionError("no `herdr agent start` call: %r"
                         % [c[0][0] for c in run_mock.call_args_list])


class StartTest(_TmpRootMixin, unittest.TestCase):
    def test_overlong_name_is_explained_before_any_pane_or_launch_action(self):
        name = "hearting-fleetaudit-structure-cx-1010"
        with mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json({"error": {"code": "invalid_agent_name"}})) as run, \
             mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["start", name, "--kind", "codex",
                                               "--beside", "w1:p0"]), 1)
        run.assert_not_called()
        self.assertIn("reason=agent-name-too-long", printed.call_args[0][0])
        self.assertIn("max_length=32", printed.call_args[0][0])
        self.assertIn("최대 32자", printed.call_args[0][0])

    def test_name_at_the_32_character_limit_still_reaches_start(self):
        with mock.patch.object(peer_steward, "_start_in_pane", return_value=0) as start:
            self.assertEqual(self._start(name="p" * 32), 0)
        self.assertEqual(start.call_args[0][0].name, "p" * 32)

    def _start(self, name="peer-c", kind="claude", pane="w1:pM", permission_mode=None, agent_args=None):
        argv = ["start", name, "--kind", kind, "--pane", pane]
        if permission_mode:
            argv += ["--permission-mode", permission_mode]
        if agent_args:
            argv += ["--"] + list(agent_args)
        return peer_steward.main(argv)

    def _ingress(self, kind="codex"):
        """A real wrapper on disk, so the existence check has something to find."""
        bindir = self.tmp_root / "codex-home" / ".harness" / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        wrapper = bindir / kind
        wrapper.write_text("#!/bin/sh\nexec true\n")
        wrapper.chmod(0o755)
        os.environ["CODEX_HOME"] = str(self.tmp_root / "codex-home")
        self.addCleanup(os.environ.pop, "CODEX_HOME", None)
        return str(bindir)

    def test_the_launcher_ingress_is_re_established_in_the_pane_first(self):
        """Only hearting's wrapper reaches the managed entry, and only the managed entry
        writes the session record — so a pane whose PATH misses the wrapper produces a
        session with no identity anywhere.

        A shell reads its startup files once. Measured 2026-09-10: `command -v codex` in a
        pane whose shell started 2026-08-24 — a week before the ingress was installed —
        answered with the vendor binary, while a pane opened 2026-09-09 answered with the
        wrapper. Nothing can reach the old pane afterwards; the profile cannot touch a
        running process. The launch can.
        """
        bindir = self._ingress()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                side_effect=_idle_shell_run) as run_mock:
            peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        argvs = [c[0][0] for c in run_mock.call_args_list]
        sends = [a for a in argvs if a[:3] == ["herdr", "pane", "send-text"]]
        self.assertEqual(len(sends), 1, argvs)
        self.assertIn(bindir, sends[0][-1])
        self.assertTrue(sends[0][-1].startswith("export PATH="))
        # ... and it has to be typed BEFORE the launch, or the launch resolves the old PATH.
        self.assertLess(argvs.index(sends[0]),
                        argvs.index(_agent_start_cmd(run_mock)))
        self.assertIn(["herdr", "pane", "send-keys", "w1:pM", "Enter"], argvs)

    def test_a_pane_already_running_an_agent_is_never_typed_into(self):
        # Text sent to an occupied pane lands in that agent's prompt, not a shell.
        self._ingress()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=_herdr_json({"result": {"pane": {"agent": "codex"}}})) as run_mock:
            peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        argvs = [c[0][0] for c in run_mock.call_args_list]
        self.assertEqual([a for a in argvs if a[:3] == ["herdr", "pane", "send-text"]], [])

    def test_a_harness_with_no_wrapper_gets_nothing_typed_on_its_behalf(self):
        # Claude and OpenCode have no launcher wrapper; there is no PATH to fix.
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                side_effect=_idle_shell_run) as run_mock:
            peer_steward.main(["start", "peer-c", "--kind", "claude", "--pane", "w1:pM"])
        argvs = [c[0][0] for c in run_mock.call_args_list]
        self.assertEqual([a for a in argvs if a[:3] == ["herdr", "pane", "send-text"]], [])
        self.assertEqual([a for a in argvs if a[:3] == ["herdr", "pane", "get"]], [])

    def test_an_uninstalled_wrapper_is_not_put_on_anyones_path(self):
        os.environ["CODEX_HOME"] = str(self.tmp_root / "no-such-home")
        self.addCleanup(os.environ.pop, "CODEX_HOME", None)
        self.assertIsNone(peer_steward._managed_ingress_dir("codex"))

    def test_the_receipt_says_whether_the_session_came_up_managed(self):
        import io
        import contextlib
        self._ingress()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                side_effect=_idle_shell_run), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=False):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertIn("managed=false", out.getvalue())

    def test_a_managed_launch_says_so(self):
        import io
        import contextlib
        self._ingress()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                side_effect=_idle_shell_run), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertIn("managed=true", out.getvalue())

    def test_the_managed_check_reads_the_entry_process_not_only_its_children(self):
        """`codex-managed-entry.py` sets `AGENT_CODEX_MANAGED_GATEWAY` for the app-server
        and TUI client it spawns, NOT for itself. Looking only for that variable on the
        pane's foreground process reported `managed=false` for a launch that really was
        managed — caught end-to-end 2026-09-10, after the unit tests were already green."""
        info = {"result": {"process_info": {"foreground_processes": [
            {"pid": 1, "argv": ["/usr/bin/python3",
                                "/x/utilities/codex-managed-entry.py", "--codex", "/y/codex"]}]}}}
        with mock.patch.object(peer_steward.subprocess, "run",
                               return_value=_herdr_json(info)):
            self.assertIs(peer_steward._pane_is_managed("w1:pM"), True)

    def test_a_plain_vendor_process_is_reported_unmanaged(self):
        info = {"result": {"process_info": {"foreground_processes": [
            {"pid": 999999999, "argv": ["codex", "--dangerously-bypass-approvals-and-sandbox"]}]}}}
        with mock.patch.object(peer_steward.subprocess, "run",
                               return_value=_herdr_json(info)):
            self.assertIs(peer_steward._pane_is_managed("w1:pM"), False)

    def test_an_unreadable_pane_is_unknown_not_a_verdict(self):
        # Claiming "unmanaged" because herdr did not answer would be a guess wearing a
        # receipt's clothes.
        with mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=OSError("herdr gone")):
            self.assertIsNone(peer_steward._pane_is_managed("w1:pM"))

    def test_cwd_reaches_the_agent_that_can_take_one(self):
        """`herdr agent start` has no cwd option, so the launched agent inherits the
        PANE's directory. `--cwd` used to be passed only to this CLI process: a session
        started with `--cwd <hearting>` came up in `SR_ModelA` (measured 2026-09-10)."""
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            rc = peer_steward.main(["start", "peer-c", "--kind", "codex",
                                    "--pane", "w1:pM", "--cwd", str(self.tmp_root)])
        self.assertEqual(rc, 0)
        cmd = _agent_start_cmd(run_mock)
        self.assertIn("--cd", cmd)
        self.assertIn(os.path.realpath(str(self.tmp_root)), cmd)

    def test_claude_cwd_is_quoted_and_applied_in_the_idle_pane_before_start(self):
        import shlex
        def fake_run(argv, **kwargs):
            if argv[:3] == ["herdr", "pane", "get"]:
                return _herdr_json({"result": {"pane": {}}})
            if argv[:3] == ["herdr", "pane", "wait-output"]:
                self.assertEqual(argv[argv.index("--timeout") + 1], "15000")
                self.assertEqual(kwargs["timeout"], 16)
                time.sleep(0.025)  # later than the old erroneous 15 ms budget
                return _herdr_json({"result": {"pane": {}}})
            if argv[:3] == ["herdr", "pane", "process-info"]:
                return _idle_shell_run(argv, **kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                side_effect=fake_run) as run_mock:
            rc = peer_steward.main(["start", "peer-c", "--kind", "claude",
                                    "--pane", "w1:pM", "--cwd", str(self.tmp_root)])
        self.assertEqual(rc, 0)
        argvs = [c[0][0] for c in run_mock.call_args_list]
        send = next(argv for argv in argvs if argv[:3] == ["herdr", "pane", "send-text"])
        self.assertEqual(send[-1], "cd -- " + shlex.quote(os.path.realpath(str(self.tmp_root))))
        ready = next(argv for argv in argvs if argv[:3] == ["herdr", "pane", "wait-output"])
        self.assertLess(argvs.index(ready), argvs.index(send))
        self.assertLess(argvs.index(send), argvs.index(_agent_start_cmd(run_mock)))

    def test_shell_prompt_glyphs_and_past_prompt_are_distinct(self):
        screens = [
            (" user@mgpu-a1  /home/example/Project_Zoo/KWS_Net  ↱ main ± ", True),
            ("host ~/project $ ", True), ("#\n\n", True), ("% ", True), ("❯ ", True),
            ("host $\nrunning command\n", False),
            ("host \nDownloading 42%\n", False),
            ("host $ echo hi", False),
            ("host $\nTrust this folder?\n1. Yes\n2. No", False),
        ]
        for screen, expected in screens:
            with self.subTest(screen=screen):
                def wait(argv, **kwargs):
                    self.assertEqual(argv[:5], ["herdr", "pane", "wait-output", "w1:pM", "--regex"])
                    matched = re.search(argv[5], screen) is not None
                    return subprocess.CompletedProcess(argv, 0 if matched else 1, "", "")
                with mock.patch.object(peer_steward.subprocess, "run", side_effect=wait):
                    self.assertEqual(peer_steward._wait_for_shell_prompt("w1:pM"), expected)

    def test_shell_wait_keeps_explicit_milliseconds_and_default_seconds(self):
        for milliseconds, expected_ms, expected_seconds in [(None, "15000", 16), (2750, "2750", 3.75), (0, "0", 1)]:
            with self.subTest(milliseconds=milliseconds), \
                 mock.patch.object(peer_steward, "_herdr_get_timeout", return_value=15), \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                self.assertTrue(peer_steward._wait_for_shell_prompt("w1:pM", milliseconds))
                argv = run.call_args.args[0]
                self.assertEqual(argv[argv.index("--timeout") + 1], expected_ms)
                self.assertEqual(run.call_args.kwargs["timeout"], expected_seconds)

    def test_old_prompt_busy_form_and_trust_receive_no_bootstrap_or_start(self):
        for screen, reason in [
            ("host $\ncommand still running", "pane-busy"),
            ("host \nSelect an option\n1. Continue\n2. Cancel", "pane-busy"),
            ("host $\nDo you trust the contents of this folder?", "native-trust-wait"),
        ]:
            with self.subTest(screen=screen):
                calls = []
                def busy(argv, **kwargs):
                    calls.append(argv)
                    if argv[:3] == ["herdr", "pane", "process-info"]:
                        return _herdr_json({"result": {"process_info": {
                            "pane_id": "w1:pM", "shell_pid": 101,
                            "foreground_process_group_id": 202,
                            "foreground_processes": [{"pid": 202, "argv": ["busy"]}],
                        }}})
                    if argv[:3] == ["herdr", "agent", "read"]:
                        return subprocess.CompletedProcess(argv, 0, screen, "")
                    # Even a stale successful wait receipt must not authorize input.
                    return _herdr_json({"result": {"pane": {}}})
                with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
                     mock.patch.object(peer_steward.subprocess, "run", side_effect=busy), \
                     mock.patch("builtins.print") as output:
                    rc = peer_steward.main(["start", "peer-c", "--kind", "claude", "--pane", "w1:pM", "--cwd", str(self.tmp_root)])
                self.assertEqual(rc, 1)
                self.assertIn("reason=" + reason, output.call_args.args[0])
                self.assertFalse(any(a[:3] in (["herdr", "pane", "send-text"], ["herdr", "pane", "send-keys"], ["herdr", "agent", "start"]) for a in calls))

    def test_occupancy_changed_during_wait_does_not_receive_bootstrap(self):
        gets = 0
        calls = []
        def changed(argv, **kwargs):
            nonlocal gets
            calls.append(argv)
            if argv[:3] == ["herdr", "pane", "get"]:
                gets += 1
                return _herdr_json({"result": {"pane": {} if gets == 1 else {"agent": "claude"}}})
            return _idle_shell_run(argv, **kwargs)
        with mock.patch.object(peer_steward.subprocess, "run", side_effect=changed):
            self.assertEqual(peer_steward._ensure_pane_ingress("w1:pM", "claude", str(self.tmp_root)), "pane-occupied")
        self.assertEqual(gets, 2)
        self.assertFalse(any(a[:3] in (["herdr", "pane", "send-text"], ["herdr", "pane", "send-keys"]) for a in calls))

    def test_unreadable_or_error_pane_never_reaches_wait_or_input(self):
        payloads = [[], {"result": "invalid"}, {"result": {"pane": None}},
                    {"error": {"code": "unavailable"}, "result": {"pane": {}}}]
        for payload in payloads:
            with self.subTest(payload=payload), \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)) as run:
                self.assertEqual(peer_steward._ensure_pane_ingress("w1:pM", "claude", str(self.tmp_root)), "pane-unknown")
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0][:3], ["herdr", "pane", "get"])

    def test_unknown_or_foreign_foreground_receives_no_bootstrap(self):
        infos = [None, {},
            {"pane_id": "w1:pOther", "shell_pid": 101, "foreground_process_group_id": 101, "foreground_processes": [{"pid": 101}]},
            {"pane_id": "w1:pM", "shell_pid": 101, "foreground_process_group_id": None, "foreground_processes": []},
            {"pane_id": "w1:pM", "shell_pid": True, "foreground_process_group_id": 101, "foreground_processes": [{"pid": 101}]},
            {"pane_id": "w1:pM", "shell_pid": 101, "foreground_process_group_id": 101, "foreground_processes": []},
            {"pane_id": "w1:pM", "shell_pid": 101, "foreground_process_group_id": 101, "foreground_processes": [{"pid": False}]},
        ]
        for info in infos:
            with self.subTest(info=info):
                calls = []
                def unknown(argv, **kwargs):
                    calls.append(argv)
                    if argv[:3] == ["herdr", "pane", "process-info"]:
                        return _herdr_json({"result": {"process_info": info}})
                    return _herdr_json({"result": {"pane": {}}})
                with mock.patch.object(peer_steward.subprocess, "run", side_effect=unknown):
                    self.assertEqual(peer_steward._ensure_pane_ingress("w1:pM", "claude", str(self.tmp_root)), "pane-unknown")
                self.assertFalse(any(a[:3] in (["herdr", "pane", "send-text"], ["herdr", "pane", "send-keys"]) for a in calls))
        for failure in (OSError("unavailable"), subprocess.TimeoutExpired(["herdr"], 5)):
            with self.subTest(failure=type(failure).__name__), \
                 mock.patch.object(peer_steward.subprocess, "run", side_effect=failure):
                self.assertEqual(peer_steward._pane_foreground_shell("w1:pM"), "pane-unknown")

    def test_opencode_uses_the_interactive_positional_project_argument(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            rc = peer_steward.main(["start", "peer-c", "--kind", "opencode",
                                    "--pane", "w1:pM", "--cwd", str(self.tmp_root)])
        self.assertEqual(rc, 0)
        cmd = _agent_start_cmd(run_mock)
        self.assertIn(os.path.realpath(str(self.tmp_root)), cmd[cmd.index("--") + 1:])
        self.assertNotIn("--cwd", cmd)

    def _scoped_tui_config(self):
        scoped = Path(os.environ["HOME"]) / ".config" / "opencode" / "tui"
        scoped.mkdir(parents=True, exist_ok=True)
        target = scoped / "hearting-owned-tui.json"
        target.write_text('{"$schema": "https://opencode.ai/tui.json", "plugin": ["./hearting-tui-identity.ts"]}\n',
                          encoding="utf-8")
        return str(target)

    def test_opencode_start_exports_scoped_tui_config_before_launch(self):
        import contextlib
        import io
        scoped = self._scoped_tui_config()
        out = io.StringIO()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                side_effect=_idle_shell_run) as run_mock, \
             contextlib.redirect_stdout(out):
            rc = peer_steward.main(["start", "peer-c", "--kind", "opencode", "--pane", "w1:pM"])
        self.assertEqual(rc, 0)
        argvs = [c[0][0] for c in run_mock.call_args_list]
        sends = [a for a in argvs if a[:3] == ["herdr", "pane", "send-text"]]
        self.assertEqual(len(sends), 1, argvs)
        self.assertIn("OPENCODE_TUI_CONFIG=", sends[0][-1])
        self.assertIn(scoped, sends[0][-1])
        self.assertLess(argvs.index(sends[0]),
                        argvs.index(_agent_start_cmd(run_mock)))
        self.assertIn(["herdr", "pane", "send-keys", "w1:pM", "Enter"], argvs)
        self.assertIn("tui_scoped=exported", out.getvalue())
        # The launch argv itself gains no new flag: the config travels in the pane env.
        cmd = _agent_start_cmd(run_mock)
        self.assertNotIn("OPENCODE_TUI_CONFIG", " ".join(cmd))

    def test_scoped_export_keeps_an_explicit_override_already_in_the_pane(self):
        # The captured prelaunch line runs in an isolated child shell: an
        # explicit user value survives, an unset value takes the scoped file.
        scoped = self._scoped_tui_config()
        line = "export OPENCODE_TUI_CONFIG=${OPENCODE_TUI_CONFIG:-%s}" % scoped
        keep = subprocess.run(["sh", "-c", line + '; printf %s "$OPENCODE_TUI_CONFIG"'],
                              capture_output=True, text=True, timeout=10,
                              env={"OPENCODE_TUI_CONFIG": "/user/tui.json", "PATH": os.environ["PATH"]})
        self.assertEqual(keep.stdout, "/user/tui.json")
        fill = subprocess.run(["sh", "-c", line + '; printf %s "$OPENCODE_TUI_CONFIG"'],
                              capture_output=True, text=True, timeout=10,
                              env={"PATH": os.environ["PATH"]})
        self.assertEqual(fill.stdout, scoped)

    def test_opencode_start_without_scoped_config_proceeds_and_notes_missing(self):
        import contextlib
        import io
        out = io.StringIO()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                side_effect=_idle_shell_run) as run_mock, \
             contextlib.redirect_stdout(out):
            rc = peer_steward.main(["start", "peer-c", "--kind", "opencode", "--pane", "w1:pM"])
        self.assertEqual(rc, 0)
        argvs = [c[0][0] for c in run_mock.call_args_list]
        self.assertEqual([a for a in argvs if a[:3] == ["herdr", "pane", "send-text"]], [])
        self.assertIn("tui_scoped=missing", out.getvalue())
        _agent_start_cmd(run_mock)

    def test_pane_input_waits_for_shell_readiness_and_times_out_without_start(self):
        self._ingress()
        calls = []
        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "wait-output"]:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="timeout")
            return _herdr_json({"result": {"pane": {}}})
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=fake_run), \
             mock.patch("builtins.print") as output:
            rc = peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertEqual(rc, 1)
        self.assertIn("reason=shell-readiness-timeout", output.call_args[0][0])
        self.assertFalse(any(argv[:3] == ["herdr", "pane", "send-text"] for argv in calls))
        self.assertFalse(any(argv[:3] == ["herdr", "agent", "start"] for argv in calls))

    def test_native_folder_trust_wait_is_explicit_and_unknown_screens_stay_unknown(self):
        trust = peer_steward._screen_lines("Trust this folder? Continue only if you trust this project.")
        unknown = peer_steward._screen_lines("Press Enter to continue")
        self.assertEqual(peer_steward._native_trust_reason("codex", trust), "native-trust-wait")
        self.assertIsNone(peer_steward._native_trust_reason("codex", unknown))

    def test_started_process_waits_for_native_trust_without_claiming_ready(self):
        import contextlib
        import io
        calls = []
        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "agent", "start"]:
                return _herdr_json(_agent_json("codex", "thread-9", "peer-c"))
            if argv[:3] == ["herdr", "agent", "read"]:
                return subprocess.CompletedProcess(argv, 0,
                    stdout="Trust this folder? Do you trust this project?", stderr="")
            if argv[:3] == ["herdr", "pane", "process-info"]:
                return _herdr_json({"result": {"process_info": {"foreground_processes": []}}})
            return _herdr_json({"result": {"pane": {}}})
        out = io.StringIO()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=fake_run), \
             contextlib.redirect_stdout(out):
            rc = peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertEqual(rc, 0)
        self.assertIn("started=true", out.getvalue())
        self.assertIn("ready=false reason=native-trust-wait", out.getvalue())
        self.assertFalse(any(argv[:3] == ["herdr", "pane", "send-text"] for argv in calls))

    def test_codex_hook_review_start_reports_not_ready_without_answering_the_form(self):
        import contextlib
        import io
        calls = []
        screen = _codex_capture("cap-inline80.txt")
        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "agent", "start"]:
                return _herdr_json(_agent_json("codex", "thread-review", "peer-c"))
            if argv[:3] == ["herdr", "agent", "read"]:
                return subprocess.CompletedProcess(argv, 0, stdout=screen, stderr="")
            if argv[:3] == ["herdr", "pane", "process-info"]:
                return _herdr_json({"result": {"process_info": {"foreground_processes": []}}})
            return _herdr_json({"result": {"pane": {}}})
        out = io.StringIO()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=fake_run), \
             contextlib.redirect_stdout(out):
            rc = peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertEqual(rc, 0)
        self.assertIn("ready=false reason=native-trust-wait", out.getvalue())
        self.assertFalse(any(argv[:3] in (["herdr", "pane", "send-text"],
                                         ["herdr", "pane", "send-keys"])
                             for argv in calls))

    def test_a_cwd_that_is_not_a_directory_never_reaches_herdr(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run") as run_mock:
            rc = peer_steward.main(["start", "peer-c", "--kind", "codex",
                                    "--pane", "w1:pM",
                                    "--cwd", str(self.tmp_root / "nope")])
        self.assertEqual(rc, 1)
        run_mock.assert_not_called()

    def test_no_cwd_leaves_the_launch_exactly_as_it_was(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertNotIn("--cd", _agent_start_cmd(run_mock))

    def test_the_receipt_says_when_the_launch_produced_no_identity(self):
        """A successful launch stays successful when shared identity is unknown."""
        import io
        import contextlib
        from fleet import session_tags
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=_herdr_json(_agent_json("codex", "herdr-claim", "peer-c"))), \
             mock.patch.object(peer_steward, "_process_session", return_value=None) as resolve, \
             mock.patch.object(session_tags, "assign_observed") as assign:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertEqual(rc, 0)
        resolve.assert_called_once_with("w1:pM", "codex")
        self.assertIn("started=true", out.getvalue())
        self.assertIn("session_id=-", out.getvalue())
        assign.assert_not_called()
        self.assertNotIn("session_bind=", out.getvalue())

    def test_the_receipt_uses_only_the_shared_resolver_identity(self):
        import io
        import contextlib
        from fleet import session_tags
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=_herdr_json(_agent_json("codex", "herdr-claim", "peer-c"))), \
             mock.patch.object(peer_steward, "_process_session", return_value="proven-thread") as resolve, \
             mock.patch.object(session_tags, "_inventory", side_effect=AssertionError("start census")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertEqual(rc, 0)
        resolve.assert_called_once_with("w1:pM", "codex")
        self.assertIn("session_id=proven-thread", out.getvalue())
        self.assertIsNotNone(session_tags.assigned_tag("codex", "proven-thread"))
        self.assertIsNone(session_tags.assigned_tag("codex", "herdr-claim"))

    def test_exact_start_remains_successful_and_numberless_during_writer_contention(self):
        import io
        import contextlib
        import fcntl
        from fleet import session_tags
        path = session_tags._path()
        path.parent.mkdir(parents=True)
        for harness in ("claude", "codex", "opencode"):
            with path.with_suffix(".lock").open("a") as lock, \
                 mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(
                     _agent_json(harness, "proven-session", "peer"))), \
                 mock.patch.object(peer_steward, "_process_session", return_value="proven-session"), \
                 mock.patch.object(session_tags, "_inventory", side_effect=AssertionError("start census")):
                fcntl.flock(lock, fcntl.LOCK_EX)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = peer_steward.main(["start", "peer", "--kind", harness, "--pane", "w1:pM"])
            self.assertEqual(rc, 0)
            self.assertIn("started=true", out.getvalue())
            self.assertIn("session_id=proven-session", out.getvalue())
            self.assertFalse(path.exists())

    def test_name_is_the_positional_right_after_start(self):
        """herdr `agent start <NAME> --kind --pane`: the name is a required positional.
        Without it herdr answers `unknown option: claude` and starts nothing, while the
        wrapper still printed started=false and recorded a `[start]` steer (measured
        2026-09-03 during the F-100 comms test)."""
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(name="peer-c", kind="claude", pane="w1:pM")
        cmd = _agent_start_cmd(run_mock)
        self.assertEqual(cmd[:6], ["herdr", "agent", "start", "peer-c", "--kind", "claude"])
        self.assertEqual(cmd[6:8], ["--pane", "w1:pM"])

    def test_default_bypass_prepends_claude_flag(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            rc = self._start(kind="claude")
        self.assertEqual(rc, 0)
        cmd = _agent_start_cmd(run_mock)
        self.assertIn("--permission-mode", cmd)
        self.assertIn("bypassPermissions", cmd)

    def test_default_bypass_prepends_codex_flag(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="codex")
        cmd = _agent_start_cmd(run_mock)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_codex_start_keeps_shared_daemon_and_caller_args_without_update_override(self):
        user_args = ["--remote", "unix:///tmp/caller-codex.sock"]
        for mode in ("bypass", "inherit"):
            with self.subTest(mode=mode), \
                    mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
                    mock.patch.object(peer_steward.subprocess, "run",
                                      return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
                self._start(kind="codex", permission_mode=mode, agent_args=user_args)
                cmd = _agent_start_cmd(run_mock)
                passed = cmd[cmd.index("--") + 1:]
                self.assertNotIn("check_for_update_on_startup=false", passed)
                self.assertIn(user_args, [passed[i:i + len(user_args)]
                                          for i in range(len(passed) - len(user_args) + 1)])
                self.assertEqual(passed.count("--no-daemon"), 0)
                if mode == "bypass":
                    self.assertIn("--dangerously-bypass-approvals-and-sandbox", passed)
                else:
                    self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", passed)
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
                mock.patch.object(peer_steward.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="claude")
        self.assertNotIn("check_for_update_on_startup=false", _agent_start_cmd(run_mock))

    def test_default_bypass_prepends_opencode_flag(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="opencode")
        cmd = _agent_start_cmd(run_mock)
        self.assertIn("--auto", cmd)

    def test_inherit_prepends_zero_flags(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="claude", permission_mode="inherit")
        cmd = _agent_start_cmd(run_mock)
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

    def test_start_that_launched_marks_the_launcher_with_source_start(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        started = json.dumps({"result": {"agent": {"agent_session": {"value": "child-sid"}}}})
        with mock.patch("builtins.print"), \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout=started, stderr="")), \
             mock.patch.object(peer_steward, "_process_session", return_value="child-sid"):
            self.assertEqual(self._start(name="peer-e", kind="codex"), 0)
        markers = peer_steward.peer_message.read_steward_markers()
        self.assertEqual(set(markers), {("claude", "sid-steward")})
        entry = markers[("claude", "sid-steward")]["targets"]["child-sid"]
        self.assertEqual((entry["harness"], entry["name"], entry["kind"], entry["source"]),
                         ("codex", "peer-e", "start", "start"))
        self.assertEqual(entry["session_id"], "child-sid")

    def test_start_that_herdr_refused_marks_nothing(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        with mock.patch("builtins.print"), \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="boom")):
            self.assertEqual(self._start(name="peer-f", kind="claude"), 1)
        self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})

    def test_start_with_an_error_body_or_no_agent_block_marks_nothing(self):
        """Review round 1 #9: exit 0 alone is not a launch."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        for stdout, expect in ((json.dumps({"error": {"code": "pane_busy"}}), "started=false"),
                               ("", "started=true"), ("not json", "started=true")):
            with self.subTest(stdout=stdout):
                with mock.patch("builtins.print") as print_mock, \
                     mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
                     mock.patch.object(peer_steward.subprocess, "run",
                                       return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")):
                    self.assertEqual(self._start(name="peer-g", kind="codex"), 1 if expect == "started=false" else 0)
                self.assertIn(expect, print_mock.call_args[0][0])
                self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})

    def test_herdr_missing_exit_4(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value=None):
            rc = self._start()
        self.assertEqual(rc, 4)

    def test_herdr_missing_start_says_so_once_and_queues_no_retry(self):
        # No herdr is permanent for this session: a retained start notice would only ask
        # the caller to repeat a start that cannot succeed.
        import contextlib
        import io
        output = io.StringIO()
        with mock.patch.object(peer_steward.shutil, "which", return_value=None), \
             mock.patch.object(peer_steward, "_retain_start_failure") as retain, \
             contextlib.redirect_stdout(output):
            self.assertEqual(peer_steward.main(["start", "helper", "--kind", "claude"]), 4)
        retain.assert_not_called()
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("started=false reason=herdr-not-found agent=claude name=helper pane=- "))

    def test_started_false_when_herdr_start_fails(self):
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="boom")):
            rc = self._start()
        self.assertEqual(rc, 1)
        line = print_mock.call_args[0][0]
        self.assertIn("started=false", line)

    def test_start_failure_explains_name_collision_and_bounded_fallbacks(self):
        for error, rc, stderr, reason in (
            ({"code": "agent_name_taken"}, 0, "private detail", "agent_name_taken"),
            ({"code": "bad code\nwith details"}, 0, "private detail", "herdr-start-error"),
            ("unstructured error", 0, "private detail", "herdr-start-error"),
            (None, 2, "private detail", "herdr-stderr-private-detail"),
            (None, 2, "", "herdr-start-failed"),
            (None, 2, "\x1b[31mName too long\x1b[0m\nprivate second line",
             "herdr-stderr-name-too-long"),
            (None, 2, "A" * 120 + "\nprivate second line", "herdr-stderr-" + "a" * 80),
        ):
            with self.subTest(error=error), mock.patch("builtins.print") as output, \
                 mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=subprocess.CompletedProcess(
                     [], rc, stdout=json.dumps({"error": error}), stderr=stderr)):
                self._start()
                line = output.call_args[0][0]
                self.assertIn("started=false", line)
                self.assertIn(f"reason={reason} herdr_rc={rc}", line)
                self.assertNotIn("private detail", line)
                self.assertNotIn("private second line", line)
                self.assertNotIn("\n", line)
                self.assertFalse(any(ord(char) < 32 or ord(char) == 127 for char in line))
                diagnostic = re.search(r"\breason=([^ ]+)", line).group(1)
                self.assertRegex(diagnostic, r"^[a-z0-9_-]+$")
                if diagnostic.startswith("herdr-stderr-"):
                    self.assertLessEqual(len(diagnostic.removeprefix("herdr-stderr-")), 80)

    def test_agent_args_pass_through_after_prefix_flags(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="claude", agent_args=["--extra-flag"])
        cmd = _agent_start_cmd(run_mock)
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
        fh.write(" ".join(argv) + " window=" + os.environ.get("FAKE_HERDR_WINDOW_ID", "window-before-move") + "\n")

# The real CLI accepts a selected endpoint before the command. Keep it in the
# call log while parsing the same command shapes for endpoint-isolation tests.
if argv[:1] == ["--session"]:
    argv = argv[2:]

mode = os.environ.get("FAKE_HERDR_MODE", "idle")
mode_file = os.environ.get("FAKE_HERDR_MODE_FILE")
if mode_file and os.path.exists(mode_file):
    mode = open(mode_file, encoding="utf-8").read().strip() or mode
target = argv[2] if len(argv) > 2 else "-"
agent_state = os.environ.get("FAKE_HERDR_AGENT_STATE", "idle")
agent_state_file = os.environ.get("FAKE_HERDR_AGENT_STATE_FILE")
if agent_state_file and os.path.exists(agent_state_file):
    agent_state = open(agent_state_file, encoding="utf-8").read().strip() or agent_state
target_session = {"peer-live": "sid-peer-live", "peer-dead": "sid-peer-dead"}.get(
    target, "sid-fake")
info = {"id": "cli:agent:wait", "result": {"agent": {
    "agent": "claude", "agent_session": {"value": target_session},
    "agent_status": agent_state, "name": os.environ.get("FAKE_HERDR_NAME", target), "pane_id": "w1:p9",
    "window_id": os.environ.get("FAKE_HERDR_WINDOW_ID", "window-before-move")}, "type": "agent_info"}}
identity_path = os.environ.get("FAKE_HERDR_IDENTITY_FILE")
if identity_path and os.path.exists(identity_path):
    with open(identity_path, encoding="utf-8") as fh:
        replacement = json.load(fh)
    info["result"]["agent"]["agent_session"]["value"] = replacement.get("session_id", "sid-fake")
    info["result"]["agent"]["pane_id"] = replacement.get("pane", "w1:p9")
    if replacement.get("once"):
        with open(identity_path, "w", encoding="utf-8") as fh:
            json.dump({"session_id": replacement.get("session_id", ""),
                       "settle_after_get": True}, fh)
    elif replacement.get("settle_after_get"):
        os.unlink(identity_path)

def fail(code):
    sys.stderr.write(json.dumps({"error": {"code": code, "message": code}}))
    sys.exit(1)

verb = argv[1] if len(argv) > 1 else ""
if argv[:2] == ["pane", "list"]:
    if mode == "pane-list-error":
        fail("server_unavailable")
    print(json.dumps({"result": {"panes": json.loads(os.environ.get(
        "FAKE_HERDR_PANES", '[{"pane_id":"w1:p9"}]'))}}))
    sys.exit(0)
if verb == "process-info":
    shell_pid = int(os.environ.get("FAKE_HERDR_SHELL_PID", os.getppid()))
    print(json.dumps({"result": {"process_info": {
        "pane_id": argv[-1], "shell_pid": shell_pid,
        "foreground_process_group_id": shell_pid,
        "foreground_processes": [{"pid": shell_pid, "argv": ["zsh"]}]}}}))
    sys.exit(0)
if verb == "get" and mode == "hang-get":
    with open(os.environ["FAKE_HERDR_FIFO"], "r") as fh:   # wedged socket
        fh.read()
if mode == "not-found" and verb in {"get", "wait"}:
    fail("agent_not_found")
if verb == "get":
    print(json.dumps(info)); sys.exit(0)
if verb == "wait":
    if mode == "timeout":
        fail("timeout")
    if mode == "checkpoint":
        marker = (log or "") + ".checkpoint"
        if not os.path.exists(marker):
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write("timeout")
            fail("timeout")
    if mode in ("held", "checkpoint"):
        fifo = os.environ["FAKE_HERDR_FIFO"]
        with open(fifo, "r") as fh:   # blocks until the test writes: an event, not a poll
            fh.read()
        if mode_file and os.path.exists(mode_file):
            mode = open(mode_file, encoding="utf-8").read().strip() or mode
        if mode == "not-found":
            fail("agent_not_found")
        if identity_path and os.path.exists(identity_path):
            with open(identity_path, encoding="utf-8") as fh:
                replacement = json.load(fh)
            session = replacement.get("session_id") or "sid-fake"
            info["result"]["agent"]["agent_session"]["value"] = session
            info["result"]["agent"]["pane_id"] = replacement.get("pane", "w1:p9")
    if os.environ.get("FAKE_HERDR_WAIT_STATE"):
        info["result"]["agent"]["agent_status"] = os.environ["FAKE_HERDR_WAIT_STATE"]
    if verb == "wait" and mode == "idle" and not info["result"]["agent"]["agent_session"]["value"]:
        info["result"]["agent"]["agent_session"]["value"] = "sid-fake"
    print(json.dumps(info)); sys.exit(0)
print(json.dumps(info)); sys.exit(0)
'''








class SupervisedPromptContractTest(_TmpRootMixin, unittest.TestCase):
    """The supervisor sender is internal: no CLI flag, keyword-only, mutually exclusive."""

    def test_prompt_cli_exposes_no_sender_or_source_flag(self):
        args = peer_steward.build_parser().parse_args(["prompt", "w1:p2", "body"])
        self.assertFalse(hasattr(args, "sender"))
        self.assertFalse(hasattr(args, "source"))
        for flag in ("--sender", "--from", "--from-session"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                peer_steward.build_parser().parse_args(
                    ["prompt", "w1:p2", "body", flag, "x"])

    def test_sender_is_keyword_only_and_conflicts_with_a_producer_binding(self):
        import inspect
        parameter = inspect.signature(peer_steward.cmd_prompt).parameters["sender"]
        self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        args = peer_steward.build_parser().parse_args(["prompt", "w1:p2", "body"])
        with self.assertRaisesRegex(ValueError, "producer-binding-with-sender"):
            peer_steward.cmd_prompt(args, producer_binding={}, sender={
                "harness": "claude", "session_id": "predecessor", "pane": "w1:p1"})

    def test_retain_failure_is_off_only_when_the_supervised_path_asks(self):
        args = peer_steward.build_parser().parse_args(
            ["start", "grid", "--kind", "codex", "--beside", "w1:p0"])
        with mock.patch.object(peer_steward, "_start_request", return_value=1), \
             mock.patch.object(peer_steward, "_retain_start_failure") as retain:
            self.assertEqual(peer_steward.cmd_start(args, retain_failure=False), 1)
        retain.assert_not_called()
        with mock.patch.object(peer_steward, "_start_request", return_value=1), \
             mock.patch.object(peer_steward, "_retain_start_failure") as retain:
            self.assertEqual(peer_steward.cmd_start(args), 1)
        retain.assert_called_once()


class SharedDaemonCodexStartTest(_TmpRootMixin, unittest.TestCase):
    """Exercise the real steward start path with Codex's shared defaults."""

    def _start(self, agent_args, *, proof=None, herdr_sid="herdr-claim"):
        import contextlib
        import io

        def fake_run(argv, **kwargs):
            if argv[:3] == ["herdr", "agent", "start"]:
                return _herdr_json(_agent_json("codex", herdr_sid, argv[3]))
            return _idle_shell_run(argv, **kwargs)

        output = io.StringIO()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=fake_run) as run_mock, \
             patch_split(peer_steward, "_managed_ingress_dir", return_value=None), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), \
             mock.patch.object(peer_steward, "_process_session", return_value=proof) as resolver, \
             contextlib.redirect_stdout(output):
            rc = peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM",
                                    "--", *agent_args])
        self.assertEqual(rc, 0)
        resolver.assert_called_once_with("w1:pM", "codex")
        return output.getvalue(), _agent_start_cmd(run_mock)

    @staticmethod
    def _typed(command):
        return command[command.index("--") + 1:]

    def test_fresh_start_keeps_shared_default_and_does_not_inject_no_daemon(self):
        out, command = self._start([], proof=None, herdr_sid="untrusted-start-response")
        self.assertNotIn("--no-daemon", self._typed(command))
        self.assertIn("started=true", out)
        self.assertIn("session_id=-", out)
        self.assertNotIn("session_bind=", out)

    def test_explicit_no_daemon_and_remote_arguments_are_preserved_once(self):
        for args, expected in (
            (["--no-daemon"], ["--no-daemon"]),
            (["--remote", "unix:///tmp/explicit-codex.sock"],
             ["--remote", "unix:///tmp/explicit-codex.sock"]),
            (["--no-daemon", "--remote=unix:///tmp/explicit-codex.sock"],
             ["--no-daemon", "--remote=unix:///tmp/explicit-codex.sock"]),
        ):
            with self.subTest(args=args):
                _out, command = self._start(args)
                typed = self._typed(command)
                self.assertIn(expected, [typed[i:i + len(expected)]
                                         for i in range(len(typed) - len(expected) + 1)])
                self.assertEqual(typed.count("--no-daemon"), args.count("--no-daemon"))

    def test_resume_and_fork_targets_are_forwarded_without_reshaping(self):
        thread = "01000013-0000-7000-8000-00000000001b"
        for args in (["resume", thread], ["fork", thread]):
            with self.subTest(args=args):
                _out, command = self._start(args)
                typed = self._typed(command)
                self.assertIn(args, [typed[i:i + len(args)]
                                     for i in range(len(typed) - len(args) + 1)])
                self.assertNotIn("--no-daemon", typed)


class _WatchMixin(_TmpRootMixin):
    """Real subprocesses, real files, only `herdr` faked."""

    def setUp(self):
        super().setUp()
        # Keep one real fixture-owned identity across CLI, detached watcher,
        # and shared-runner observations. os.getppid() changes at every hop.
        self.shell = subprocess.Popen(
            ["/bin/sleep", "3600"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.addCleanup(self._stop_fixture_shell)
        # Registered after the tmp-dir cleanup so it runs first (LIFO): every watcher and
        # fake-herdr child is dead before the directory it writes into is removed.
        self.addCleanup(fixture_processes.reap, str(self.tmp_root),
                        fifo=str(self.tmp_root / "release.fifo"))
        self.bin = self.tmp_root / "fakebin"
        self.bin.mkdir()
        census_home = self.tmp_root / "home" / ".claude"
        (census_home / "sessions").mkdir(parents=True, exist_ok=True)
        (census_home / ".statusline").mkdir(parents=True, exist_ok=True)
        self.census_process = subprocess.Popen(
            ["bash", "-c", "exec -a /usr/bin/claude sleep 600"],
            env=dict(os.environ), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._stop_census_process)
        fake = self.bin / "herdr"
        fake.write_text(_FAKE_HERDR)
        fake.chmod(0o755)
        self.calllog = self.tmp_root / "calls.log"
        self.fifo = self.tmp_root / "release.fifo"
        os.mkfifo(self.fifo)
        self.watch_root = peer_steward.peer_message.peer_state_root() / "peer-watches"

    def _stop_fixture_shell(self):
        if self.shell.poll() is None:
            self.shell.terminate()
            try:
                self.shell.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.shell.kill()
                self.shell.wait(timeout=3)

    def _stop_census_process(self):
        if self.census_process.poll() is None:
            self.census_process.terminate()
            try:
                self.census_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.census_process.kill()
                self.census_process.wait(timeout=2)

    def _env(self, mode="idle", session_id="steward-1", with_herdr=True):
        env = dict(os.environ)
        for key in tuple(env):
            if key.startswith(("AGENT_DISPATCH_", "AGENT_SESSION_", "AGENT_RUNTIME_",
                               "HERDR_", "FAKE_HERDR_")):
                env.pop(key, None)
        for key in (
            "AGENT_SESSION_ID", "AGENT_SESSION_ROLE", "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
            "OPENCODE_SESSION_ID", "OPENCODE_DISPATCH_SLUG",
        ):
            env.pop(key, None)
        env["AGENT_DISPATCH_JOBS"] = str(self.jobs_path)
        env["AGENT_PEER_LEDGER_ROOT"] = str(self.tmp_root)
        env["FAKE_HERDR_SHELL_PID"] = str(self.shell.pid)
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

    def _arm_records(self):
        return sorted(path for path in self.watch_root.glob("*.json")
                      if re.fullmatch(r"[0-9a-f]{16}\.json", path.name))

    def _wait_calls(self):
        if not self.calllog.exists():
            return []
        return [line for line in self.calllog.read_text().splitlines()
                if " agent wait " in f" {line} "]

    def _wait_for_wait_calls(self, count, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if len(self._wait_calls()) >= count:
                return self._wait_calls()
            time.sleep(0.02)
        self.fail(f"only {len(self._wait_calls())} native waits appeared; expected {count}")

    def _directive(self, stdout):
        """The `parent_next*` fields, read from the directive line only.

        `parent_next_command` holds spaces, so it is the rest of its own line.
        """
        for line in stdout.splitlines():
            if line.startswith("parent_next="):
                head, _, command = line.partition("parent_next_command=")
                fields = self._fields(head)
                fields["parent_next_command"] = command.strip()
                return fields
        return {}

    def _release(self):
        # A held reader can close between retries. Nonblocking open turns "no
        # reader" into ENXIO so this bounded recovery loop cannot hang at open().
        # The arm receipt precedes the native child's interpreter startup.
        # Wait for the FIFO reader handshake within the existing startup
        # budget; a fixed count of 100 retries gave it only one second.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            fd = None
            try:
                fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.write(fd, b"go")
                return
            except (BrokenPipeError, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno not in {errno.ENXIO, errno.EPIPE}:
                    raise
                time.sleep(0.01)
            finally:
                if fd is not None:
                    os.close(fd)
        self.fail("no reader took the FIFO release")

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
        arm_path = self.watch_root / f"{watch_id}.json"
        observation_path = self.watch_root / f"{watch_id}.observation.json"
        readiness_root = peer_steward.peer_obligations.ObligationStore().root / "readiness-v1"
        diagnostic = {
            "arm": peer_steward._read_json(arm_path),
            "observation": peer_steward._read_json(observation_path),
            "watcher_log": (self.watch_root / f"{watch_id}.log").read_text(
                errors="replace")[-2000:]
                if (self.watch_root / f"{watch_id}.log").exists() else "",
            "readiness": [peer_steward.shared_readiness._read_json(item)
                          for item in readiness_root.glob("*.json")]
                if readiness_root.is_dir() else [],
        }
        self.fail(f"receipt never appeared for {watch_id}; diagnostics={diagnostic}")

    def _wait_for_observation(self, watch_id, seconds=10):
        path = self.watch_root / f"{watch_id}.observation.json"
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if path.exists():
                return json.loads(path.read_text())
            time.sleep(0.02)
        self.fail(f"observation checkpoint never appeared for {watch_id}")

    def _wait_for_native_wait_started(self, watch_id, seconds=10):
        path = self.watch_root / f"{watch_id}.observation.json"
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            observation = peer_steward._read_json(path) or {}
            native_wait = observation.get("native_wait") or {}
            if (native_wait.get("state") == "started"
                    and native_wait.get("pid") and native_wait.get("pid_start")):
                return native_wait
            time.sleep(0.02)
        self.fail(f"native wait never published its process identity for {watch_id}")


class WatchDeadlineTest(unittest.TestCase):
    def test_receipt_transition_reuses_the_arm_monotonic_deadline(self):
        absolute = peer_steward._watch_deadline(
            60_000, wall=1_000, monotonic=100, boot_id="boot-a")
        arm = {"timeout": 60_000, "deadline": absolute}
        self.assertEqual(peer_steward._watch_monotonic_deadline(
            arm, wall=1_001, monotonic=101, boot_id="boot-a"), 160)
        self.assertEqual(peer_steward._watch_monotonic_deadline(
            arm, wall=1_050, monotonic=150, boot_id="boot-a"), 160)

    def test_same_boot_deadline_uses_monotonic_time_after_wall_clock_reversal(self):
        deadline = peer_steward._watch_deadline(
            60_000, wall=1_000, monotonic=100, boot_id="boot-a")
        remaining, state = peer_steward._watch_timeout_remaining_ms(
            {"timeout": 60_000, "deadline": deadline},
            wall=100, monotonic=101, boot_id="boot-a")
        self.assertEqual((remaining, state), (59_000, "active"))

    def test_reboot_rebuilds_a_clamped_budget_from_the_wall_deadline(self):
        deadline = peer_steward._watch_deadline(
            60_000, wall=1_000, monotonic=100, boot_id="boot-a")
        arm = {"timeout": 60_000, "deadline": deadline}
        self.assertEqual(peer_steward._watch_timeout_remaining_ms(
            arm, wall=1_010, monotonic=1, boot_id="boot-b"), (50_000, "active"))
        self.assertEqual(peer_steward._watch_timeout_remaining_ms(
            arm, wall=100, monotonic=1, boot_id="boot-b"), (60_000, "active"))

    def test_incomplete_legacy_timeout_is_unknown_without_a_fresh_budget(self):
        remaining, state = peer_steward._watch_timeout_remaining_ms(
            {"timeout": 30_000, "armed_ts": "not-a-timestamp"},
            wall=1_000, monotonic=100, boot_id="boot-a")
        self.assertIsNone(remaining)
        self.assertEqual(state, "unknown")


class WatchPendingRegisteredWorkTest(_TmpRootMixin, unittest.TestCase):
    def _view(self, sid="sid-current"):
        return SimpleNamespace(complete=True, endpoint="default", panes=[{
            "pane_id": "w1:p9", "agent": "claude", "agent_session": {"value": sid}}])

    def _expected(self):
        return {"pane": "w1:p9", "harness": "claude", "session_id": "sid-current"}

    def _row(self, status="running", sid="sid-current"):
        import dispatch_contract
        metadata = {
            "attempt_schema_version": "2", "dispatch_depth": "2", "transport": "headless",
            "execution_surface": "registered-headless", "registered_worker": "1",
            "fallback_hop": "same-harness-headless", "attempt_id": "att-watch-live",
            "harness": "codex", "parent_sid": sid, "parent_harness": "claude",
            **dispatch_contract.process_launch_identity(os.getpid()),
        }
        pipe = ",".join(f"{key}={value}" for key, value in metadata.items())
        self.jobs_path.write_text(f"now\t{status}\trepo\tworktree\twatch-live\t{pipe}\n")

    def test_unchanged_registry_does_not_cache_liveness_or_terminal_status(self):
        from dispatch_contract import ObservedAttemptLiveness
        self._row()
        check = peer_steward._watch_registered_work_pending
        self.assertTrue(check(self._view(), self._expected(), "default"))
        with split_patch("dispatch_contract.observed_attempt_liveness", return_value=
                        ObservedAttemptLiveness("unverifiable", "fixture", "unverifiable", "fixture")):
            self.assertFalse(check(self._view(), self._expected(), "default"))
        self._row(status="done")
        self.assertFalse(check(self._view(), self._expected(), "default"))
        self._row(sid="sid-foreign")
        self.assertFalse(check(self._view(), self._expected(), "default"))

    def test_replacement_incomplete_census_and_observation_errors_use_full_readiness(self):
        self._row()
        check = peer_steward._watch_registered_work_pending
        self.assertFalse(check(self._view("sid-foreign"), self._expected(), "default"))
        self.assertFalse(check(None, self._expected(), "default"))
        view = self._view()
        view.complete = False
        self.assertFalse(check(view, self._expected(), "default"))
        with mock.patch.object(peer_steward.peer_obligations, "bound_work_for_pane",
                               side_effect=OSError("unreadable")):
            self.assertFalse(check(self._view(), self._expected(), "default"))


class WatchWakeTest(unittest.TestCase):
    def test_malformed_observer_protocol_does_not_interrupt_watch_sweep(self):
        with mock.patch.object(peer_steward.os, "kill") as kill:
            self.assertFalse(peer_steward._wake_watch_observer({"observer_protocol": "bad"}))
        kill.assert_not_called()


class WatcherIdentityTest(unittest.TestCase):
    def test_exited_watcher_is_absent_even_when_its_row_vanishes_between_reads(self):
        def stat_row(state, start):
            return "4242 (python3) " + " ".join([state, *(["0"] * 18), start])

        for state, present in (("S", True), ("R", True), ("Z", False), ("X", False)):
            with self.subTest(state=state), mock.patch.object(
                    peer_steward.Path, "read_text", return_value=stat_row(state, "800")):
                self.assertIs(peer_steward._pid_identity_ok(4242, "800"), present)
                self.assertFalse(peer_steward._pid_identity_ok(4242, "801"))
        # The parent can reap an exited watcher between two reads of its row
        # (CI: a replaced observer was then reported present and never respawned).
        with mock.patch.object(peer_steward.Path, "read_text",
                               side_effect=[stat_row("Z", "800"), FileNotFoundError()]):
            self.assertFalse(peer_steward._watcher_present(
                {"watcher": {"pid": 4242, "pid_start": "800"}}))

    def test_observer_never_names_itself_without_its_own_start_ticks(self):
        watch_id = "0123456789abcdef"
        with tempfile.TemporaryDirectory() as tmp:
            paths = peer_steward._watch_paths(watch_id, Path(tmp))
            arm = {"watch_id": watch_id, "target": "peer-a",
                   "steward": {"session_id": "steward-1"}, "watcher": {}}
            paths.arm.write_text(json.dumps(arm), encoding="utf-8")
            # An empty pid_start would make this live observer read as exited.
            with patch_split(peer_steward, "_proc_start_ticks", return_value=None), \
                    patch_split(peer_steward, "process_start_ticks", return_value=None):
                peer_steward._record_own_watch_identity(watch_id, paths, "default")
            self.assertEqual(json.loads(paths.arm.read_text())["watcher"], {})

    def test_observer_never_overwrites_a_live_watcher_or_a_settled_watch(self):
        watch_id = "0123456789abcdef"
        other = subprocess.Popen(["/bin/sleep", "60"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        live = {"pid": other.pid, "pid_start": peer_steward._proc_start_ticks(other.pid)}
        with tempfile.TemporaryDirectory() as tmp:
            paths = peer_steward._watch_paths(watch_id, Path(tmp))
            for watcher, settled in ((live, False), ({}, True)):
                with self.subTest(watcher=watcher, settled=settled):
                    arm = {"watch_id": watch_id, "target": "peer-a",
                           "steward": {"session_id": "steward-1"}, "watcher": watcher}
                    paths.arm.write_text(json.dumps(arm), encoding="utf-8")
                    if settled:
                        paths.receipt.write_text("{}", encoding="utf-8")
                    peer_steward._record_own_watch_identity(watch_id, paths, "default")
                    self.assertEqual(json.loads(paths.arm.read_text())["watcher"], watcher)


class WatchArmTest(_WatchMixin, unittest.TestCase):
    """A56-1."""

    def test_second_server_watch_does_not_use_default_socket_or_finish_on_default_pane(self):
        env = self._env("held")
        env["HERDR_SOCKET_PATH"] = str(self.tmp_root / "default-herdr.sock")
        env["HERDR_PANE_ID"] = "w1:p1"
        armed = self._run("--herdr-session", "second", "watch", "peer-a", env=env)
        self.assertEqual(armed.returncode, 0, armed.stderr)
        watch_id = self._fields(armed.stdout)["watch_id"]
        arm_path = self.watch_root / f"{watch_id}.json"
        arm = peer_steward._read_json(arm_path)
        self.assertEqual((arm["server"], arm["steward_server"]), ("second", "default"))
        waits = self._wait_for_wait_calls(1)
        self.assertTrue(any(line.startswith("--session second agent wait peer-a") for line in waits), waits)
        self.assertFalse((self.watch_root / f"{watch_id}.receipt.json").exists())

    def test_explicit_watch_timeout_is_terminal_and_sweep_does_not_restart_it(self):
        env = self._env("held")
        armed = self._run("watch", "peer-a", "--timeout", "500", env=env)
        self.assertEqual(armed.returncode, 0, armed.stderr)
        watch_id = self._fields(armed.stdout)["watch_id"]
        arm_path = self.watch_root / f"{watch_id}.json"
        original_deadline = peer_steward._read_json(arm_path)["deadline"]
        receipt = self._wait_for_receipt(watch_id)
        self.assertEqual(receipt["state"], "timeout")
        self.assertEqual(receipt["reason"], "watch-deadline")
        waits = [line for line in self.calllog.read_text().splitlines()
                 if line.startswith("agent wait ")]
        # The total budget can expire before the observer launches its waiter.
        # Either path must remain terminal without another wait or a new budget.
        self.assertLessEqual(len(waits), 1, self.calllog.read_text())
        final_arm = peer_steward._read_json(arm_path)
        self.assertEqual(final_arm["deadline"], original_deadline)
        self.assertEqual(peer_steward._watch_timeout_remaining_ms(final_arm)[1], "expired")
        before = self.calllog.read_text()
        self.assertEqual(self._run("__ensure-obligations", env=env).returncode, 0)
        self.assertEqual(self.calllog.read_text(), before)

    def test_zero_timeout_writes_receipt_without_launching_a_native_wait(self):
        env = self._env("held")
        armed = self._run("watch", "peer-a", "--timeout", "0", env=env)
        self.assertEqual(armed.returncode, 0, armed.stderr)
        watch_id = self._fields(armed.stdout)["watch_id"]
        receipt = self._wait_for_receipt(watch_id)
        self.assertEqual((receipt["state"], receipt["reason"]), ("timeout", "watch-deadline"))
        self.assertFalse(any(line.startswith("agent wait ")
                             for line in self.calllog.read_text().splitlines()))

    def test_a_hook_armed_watch_tells_the_caller_to_end_the_turn(self):
        """Review round 2/3, M-B: the contract function was locked, its wiring
        was not -- reverting either call site passed every suite."""
        proc = self._run("watch", "peer-a", "--wake", "hook", env=self._env("idle"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        directive = self._directive(proc.stdout)
        self.assertEqual(directive.get("parent_next"), "end-turn", proc.stdout)
        self.assertEqual(directive.get("parent_next_reason"), "carrier-steward-watch")
        self.assertEqual(directive.get("parent_next_command"), "-")

    def _codex_env(self, with_socket):
        import socket as socket_module
        env = self._env("idle")
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env["CODEX_THREAD_ID"] = "019a0000-0000-7000-8000-0000000000cc"
        home = self.tmp_root / ("codex-home-socket" if with_socket else "codex-home-none")
        (home / "app-server-control").mkdir(parents=True, exist_ok=True)
        env["CODEX_HOME"] = str(home)
        if with_socket:
            sock = socket_module.socket(socket_module.AF_UNIX)
            path = home / "app-server-control" / "app-server-control.sock"
            try:
                sock.bind(str(path))
            except OSError:
                sock.close()
                self.skipTest("unix socket path too long here")
            self.addCleanup(sock.close)
        return env

    def test_a_codex_watch_with_its_native_queue_ends_the_turn(self):
        proc = self._run("watch", "peer-a", env=self._codex_env(True))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("wake=queue", proc.stdout)
        directive = self._directive(proc.stdout)
        self.assertEqual(directive.get("parent_next"), "end-turn", proc.stdout)
        self.assertEqual(directive.get("parent_next_reason"), "carrier-steward-watch-codex-queue")

    def test_a_codex_queue_watch_receipt_is_handed_to_the_message_courier(self):
        code = r"""
import importlib.util, json, sys
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("ps", sys.argv[1] + "/peer-steward.py")
ps = importlib.util.module_from_spec(spec); spec.loader.exec_module(ps)
sid = "019a0000-0000-7000-8000-0000000000cc"
steward = {"harness": "codex", "session_id": sid}
_text, ref = ps.peer_message.prepare_peer_message(
    "[peer-steward] watch_id=w1 state=done target=t; exact watch receipt is available",
    steward, steward, defer=True, refs=["peer-watch:w1"], receipt="peer-steward-watch:w1")
import peer_obligations
peer_obligations.ensure_normal_runner = lambda *a, **k: None
import peer_steward_watch
queued = peer_steward_watch._queue_watch_notice(ref, {"wake": "queue", "server": "default"})
duty = ps.peer_obligations.ObligationStore().get("message-" + ref)
print(json.dumps({"queued": bool(queued), "kind": ((duty or {}).get("intent") or {}).get("kind"),
                  "to": ((duty or {}).get("intent") or {}).get("to")}))
"""
        done = subprocess.run([sys.executable, "-c", code, str(_HERE)], capture_output=True,
                              text=True, env=self._codex_env(True), timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual((result["queued"], result["kind"]), (True, "message"), result)
        self.assertEqual(result["to"]["harness"], "codex")

    def test_a_codex_watch_without_a_queue_socket_still_waits(self):
        proc = self._run("watch", "peer-a", env=self._codex_env(False))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("wake=none", proc.stdout)
        self.assertEqual(self._directive(proc.stdout).get("parent_next"), "bounded-wait")

    def test_a_watch_with_no_carrier_tells_the_caller_to_wait_with_a_bound(self):
        proc = self._run("watch", "peer-a", "--wake", "none", env=self._env("idle"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        directive = self._directive(proc.stdout)
        self.assertEqual(directive.get("parent_next"), "bounded-wait", proc.stdout)
        self.assertEqual(directive.get("parent_next_reason"), "steward-wake-none")
        command = directive.get("parent_next_command", "")
        self.assertIn("peer-steward.py join ", command)
        self.assertIn("--timeout ", command)
        self.assertNotIn("dispatch-wait", command)

    def test_no_line_the_hook_cannot_arm_from_ever_says_end_turn(self):
        """The dedupe hit and `rearm` both print from the session, not `watch`.

        Answering `end-turn` there is worst exactly when it is reached: a caller
        re-runs `watch`/`rearm` *because* it doubts the first arm carried.
        """
        first = self._run("watch", "peer-a", "--wake", "hook", env=self._env("held"))
        self.assertEqual(first.returncode, 0, first.stderr)
        watch_id = self._fields(first.stdout.splitlines()[0])["watch_id"]
        self._wait_for_lock(watch_id)
        for argv in (
            ("watch", "peer-a", "--wake", "hook"),  # dedupe -> already-armed
            ("rearm", watch_id),                    # live watcher -> alive
        ):
            with self.subTest(argv=argv):
                proc = self._run(*argv, env=self._env("held"))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                state = self._fields(proc.stdout.splitlines()[0]).get("state")
                self.assertIn(state, {"already-armed", "alive"}, proc.stdout)
                directive = self._directive(proc.stdout)
                self.assertEqual(
                    directive.get("parent_next"), "bounded-wait", proc.stdout
                )
                self.assertIn("--timeout ", directive.get("parent_next_command", ""))
        self._release()
        self._wait_for_receipt(watch_id)

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

        # F-100c-2: arming a watcher on a resolved target is steward evidence (source=watch)
        markers = peer_steward.peer_message.read_steward_markers()
        self.assertIn(("claude", "steward-1"), markers)
        entry = next(iter(markers[("claude", "steward-1")]["targets"].values()))
        self.assertEqual((entry["name"], entry["kind"], entry["source"]), ("peer-a", "watch", "watch"))

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

    def _unbound_watch(self, extra_env=None):
        identity_path = self.tmp_root / "identity.json"
        # The first get sees no session; the following native wait/get sees the
        # session created by first input. Keep that transition consistent across
        # the fake Herdr calls so the observer can prove the same pane identity.
        identity_path.write_text(json.dumps({"session_id": "", "once": True}))
        # Held: the detached watcher's native wait blocks on the FIFO, so the
        # first session cannot bind before the unbound arm is read below. An
        # idle wait returned at once and let a slow test read the bound arm.
        env = self._env("held")
        env["FAKE_HERDR_IDENTITY_FILE"] = str(identity_path)
        env.update(extra_env or {})
        proc = self._run("watch", "peer-a", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fields = self._fields(proc.stdout.splitlines()[0])
        self.assertEqual(fields["state"], "armed", proc.stdout)
        arm_path = self.watch_root / f"{fields['watch_id']}.json"
        arm = json.loads(arm_path.read_text())
        self.assertEqual((arm["agent"]["session_id"], arm["agent"]["pane"]), ("-", "w1:p9"))
        return fields["watch_id"], arm_path, identity_path, env

    def test_watch_on_a_pane_before_its_first_session_arms_on_the_pane(self):
        """2026-10-10 regression: a fresh Codex has no session until its first
        input, and `watch` refused it (`watch-identity-unverified`)."""
        watch_id, _arm, identity_path, _env = self._unbound_watch()
        identity_path.write_text(json.dumps({"session_id": "sid-fake"}))
        self._release()
        receipt = self._wait_for_receipt(watch_id)
        self.assertEqual((receipt["agent"]["session_id"], receipt["agent"]["pane"]), ("sid-fake", "w1:p9"))

    def test_sidless_watch_marker_uses_resolved_name_and_pane(self):
        _watch_id, _arm, _identity, env = self._unbound_watch(
            {"FAKE_HERDR_NAME": "hearting-1d"})
        markers = peer_steward.peer_message.read_steward_markers()
        entry = markers[("claude", "steward-1")]["targets"]["hearting-1d"]
        self.assertEqual((entry["harness"], entry["session_id"], entry["name"], entry["pane"]),
                         ("claude", None, "hearting-1d", "w1:p9"))

    def test_pane_armed_watch_keeps_the_session_it_took(self):
        watch_id, arm_path, identity_path, env = self._unbound_watch()
        identity_path.write_text(json.dumps({"session_id": "sid-first"}))
        again = self._run("watch", "w1:p9", env=env)
        self.assertEqual(self._fields(again.stdout.splitlines()[0]).get("watch_id"), watch_id)
        self.assertIn("state=already-armed", again.stdout)
        self.assertEqual(json.loads(arm_path.read_text())["target"], "peer-a")
        self.assertEqual(len(self._arm_records()), 1)
        self.assertEqual(len(self._wait_for_wait_calls(1)), 1)
        self._release()
        receipt = self._wait_for_receipt(watch_id)
        self.assertEqual(receipt["agent"]["session_id"], "sid-first")
        deadline = time.monotonic() + 10
        while (json.loads(arm_path.read_text())["agent"]["session_id"] != "sid-first"
               and time.monotonic() < deadline):
            time.sleep(0.02)
        self.assertEqual(json.loads(arm_path.read_text())["agent"]["session_id"], "sid-first")
        # Another session in that pane is a different target, as for an exact arm.
        identity_path.write_text(json.dumps({"session_id": "sid-other"}))
        other = self._run("watch", "peer-a", env=env)
        self.assertEqual(self._fields(other.stdout.splitlines()[0]).get("state"), "armed", other.stdout)
        self.assertNotEqual(self._fields(other.stdout.splitlines()[0])["watch_id"], watch_id)

    def test_registration_revalidates_after_observer_binds_first_native_session(self):
        import contextlib
        import io

        state_file = self.tmp_root / "agent-state.txt"
        state_file.write_text("working")
        watch_id, arm_path, identity_path, env = self._unbound_watch(
            {"FAKE_HERDR_AGENT_STATE_FILE": str(state_file),
                     "FAKE_HERDR_WAIT_STATE": "idle"})
        original_arm = json.loads(arm_path.read_text())
        original_deadline = original_arm["deadline"]
        paths = peer_steward._watch_paths(watch_id, self.watch_root)
        observation = self._wait_for_observation(watch_id)
        deadline = time.monotonic() + 10
        while (time.monotonic() < deadline
               and (observation.get("native_wait", {}).get("state") != "started"
                    or "pid" not in observation.get("native_wait", {}))):
            time.sleep(0.01)
            observation = json.loads(paths.observation.read_text())
        self.assertEqual(observation["native_wait"]["state"], "started")
        self._wait_for_wait_calls(1)

        alias_key = peer_steward._watch_alias_lock_key(
            "steward-1", original_arm["agent"], [], "default")
        alias_path = self.watch_root / f"{alias_key}.lock"
        payload_b = {"result": {"agent": {
            "agent": "claude", "agent_status": "working",
            "agent_session": {"value": "sid-fake"}, "name": "peer-a",
            "pane_id": "w1:p9"}}}
        alias_opened = threading.Event()
        release_registration = threading.Event()
        alias_fds = set()
        spawned = []
        calls = []
        result = {}
        output = io.StringIO()
        real_open_lock = peer_steward._open_lock
        real_acquire = peer_steward._acquire_bounded
        real_popen = peer_steward.subprocess.Popen

        def cleanup_spawned():
            for process in spawned:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

        self.addCleanup(cleanup_spawned)

        def tracked_open(path):
            fd = real_open_lock(path)
            if threading.current_thread().name == "register-B" and Path(path) == alias_path:
                alias_fds.add(fd)
            return fd

        def staged_acquire(fd, timeout_ms):
            if threading.current_thread().name == "register-B":
                if fd in alias_fds:
                    alias_opened.set()
                    if not release_registration.wait(10):
                        return False
                fcntl.flock(fd, fcntl.LOCK_EX)
                return True
            return real_acquire(fd, timeout_ms)

        def get_b(_target):
            calls.append("sid-fake")
            return payload_b

        def tracked_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            spawned.append(process)
            return process

        def register_b():
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(peer_steward, "_run_herdr_get", side_effect=get_b), \
                    patch_split(peer_steward, "_open_lock", side_effect=tracked_open), \
                    patch_split(peer_steward, "_acquire_bounded", side_effect=staged_acquire), \
                    mock.patch.object(peer_steward.subprocess, "Popen", side_effect=tracked_popen), \
                    contextlib.redirect_stdout(output):
                result["rc"] = peer_steward.main(["watch", "w1:p9"])
            result["stdout"] = output.getvalue()

        registration = threading.Thread(target=register_b, name="register-B")
        registration.start()
        self.assertTrue(alias_opened.wait(10), "registration did not reach pane admission")
        self.assertEqual(calls, ["sid-fake"], "pre-lock lookup should only select the pane lock")

        # Complete the original single native wait while registration is held
        # at admission. The observer must bind A under the shared pane lock.
        identity_path.write_text(json.dumps({"session_id": "sid-A"}))
        self._release()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            bound_arm = json.loads(arm_path.read_text())
            observation = json.loads(paths.observation.read_text())
            if (bound_arm["agent"].get("session_id") == "sid-A"
                    and observation.get("native_wait", {}).get("state") == "result"):
                break
            time.sleep(0.01)
        self.assertEqual(bound_arm["agent"]["session_id"], "sid-A")
        self.assertEqual(observation["native_wait"]["agent"]["session_id"], "sid-A")
        self.assertFalse(paths.receipt.exists(), "the original A obligation must still be active")
        self.assertEqual(bound_arm["deadline"], original_deadline)
        self.assertEqual(len(self._wait_calls()), 1, self.calllog.read_text())

        # A conflicting later result is neither successful nor allowed to
        # overwrite the established native identity.
        conflicting = SimpleNamespace(
            target="peer-a", steward_session_id="steward-1", until=[],
            expected_harness="claude", expected_pane="w1:p9")
        self.assertFalse(peer_steward._persist_watch_session(
            conflicting, paths, "default", "sid-fake"))
        self.assertEqual(json.loads(arm_path.read_text())["agent"]["session_id"], "sid-A")

        # The new observer must return the B pane occupant before it launches.
        identity_path.unlink()
        release_registration.set()
        registration.join(30)
        self.assertFalse(registration.is_alive(), "registration did not leave pane admission")
        self.assertEqual(result.get("rc"), 0, result.get("stdout", ""))
        fields = self._fields(result["stdout"].splitlines()[0])
        self.assertEqual(fields["state"], "armed", result["stdout"])
        self.assertNotEqual(fields["watch_id"], watch_id)
        admitted = json.loads((self.watch_root / f"{fields['watch_id']}.json").read_text())
        self.assertEqual(admitted["agent"]["session_id"], "sid-fake")
        self.assertEqual(json.loads(arm_path.read_text())["agent"]["session_id"], "sid-A")
        self.assertEqual(json.loads(arm_path.read_text())["deadline"], original_deadline)
        waits = self._wait_for_wait_calls(2)
        self.assertEqual(sum("agent wait peer-a" in row for row in waits), 1, self.calllog.read_text())
        self.assertEqual(sum("agent wait w1:p9" in row for row in waits), 1, self.calllog.read_text())

    def test_conflicting_native_result_keeps_established_sid_and_unknown_evidence(self):
        identity_path = self.tmp_root / "native-result-identity.json"
        env = self._env("held")
        env["FAKE_HERDR_IDENTITY_FILE"] = str(identity_path)
        armed = self._run("watch", "peer-a", env=env)
        self.assertEqual(armed.returncode, 0, armed.stderr)
        watch_id = self._fields(armed.stdout)["watch_id"]
        arm_path = self.watch_root / f"{watch_id}.json"
        original_arm = json.loads(arm_path.read_text())
        self.assertEqual(original_arm["agent"]["session_id"], "sid-fake")
        paths = peer_steward._watch_paths(watch_id, self.watch_root)
        observation = self._wait_for_observation(watch_id)
        deadline = time.monotonic() + 10
        while (time.monotonic() < deadline
               and (observation.get("native_wait", {}).get("state") != "started"
                    or "pid" not in observation.get("native_wait", {}))):
            time.sleep(0.01)
            observation = json.loads(paths.observation.read_text())
        wait_record = observation["native_wait"]
        self.assertEqual(wait_record["state"], "started")
        self._wait_for_wait_calls(1)

        identity_path.write_text(json.dumps({"session_id": "sid-A"}))
        self._release()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            observation = json.loads(paths.observation.read_text())
            if observation.get("state") == "unknown":
                break
            time.sleep(0.01)
        self.assertEqual(observation["state"], "unknown")
        preserved = observation["native_wait"]
        self.assertEqual((preserved["state"], preserved["reason"],
                          preserved["pid"], preserved["pid_start"]),
                         ("unknown", "target-identity-conflict",
                          wait_record["pid"], wait_record["pid_start"]))
        self.assertEqual(preserved["agent"]["session_id"], "sid-A")
        self.assertEqual(preserved["agent_state"], "idle")
        self.assertEqual(json.loads(arm_path.read_text())["agent"]["session_id"], "sid-fake")
        self.assertFalse(paths.receipt.exists())
        self.assertEqual(len(self._wait_calls()), 1, self.calllog.read_text())

    def test_admission_reconciles_native_wait_result_before_unbound_arm_scan(self):
        import contextlib
        import io

        watch_id, arm_path, identity_path, env = self._unbound_watch()
        original_arm = json.loads(arm_path.read_text())
        original_deadline = original_arm["deadline"]
        paths = peer_steward._watch_paths(watch_id, self.watch_root)
        observation = self._wait_for_observation(watch_id)
        deadline = time.monotonic() + 10
        while (time.monotonic() < deadline
               and (observation.get("native_wait", {}).get("state") != "started"
                    or "pid" not in observation.get("native_wait", {}))):
            time.sleep(0.01)
            observation = json.loads(paths.observation.read_text())
        wait_record = observation["native_wait"]
        self.assertEqual(wait_record["state"], "started")
        self._wait_for_wait_calls(1)
        alias_key = peer_steward._watch_alias_lock_key(
            "steward-1", original_arm["agent"], [], "default")
        alias_path = self.watch_root / f"{alias_key}.lock"
        alias_fd = peer_steward._open_lock(alias_path)
        fcntl.flock(alias_fd, fcntl.LOCK_EX)
        try:
            identity_path.write_text(json.dumps({"session_id": "sid-A"}))
            self._release()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                raw_log = paths.log.read_text(encoding="utf-8") if paths.log.exists() else ""
                if '"value": "sid-A"' in raw_log and not peer_steward._pid_identity_ok(
                        wait_record["pid"], wait_record["pid_start"]):
                    break
                time.sleep(0.01)
            self.assertIn('"value": "sid-A"', raw_log)
            self.assertEqual(json.loads(arm_path.read_text())["agent"]["session_id"], "-")
            self.assertEqual(len(self._wait_calls()), 1)

            payload_b = {"result": {"agent": {
                "agent": "claude", "agent_status": "working",
                "agent_session": {"value": "sid-fake"}, "name": "peer-a",
                "pane_id": "w1:p9"}}}
            alias_opened = threading.Event()
            alias_fds = set()
            spawned = []
            result = {}
            output = io.StringIO()
            real_open_lock = peer_steward._open_lock
            real_popen = peer_steward.subprocess.Popen
            real_receipt = peer_steward._write_terminal_watch_receipt

            def cleanup_spawned():
                for process in spawned:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)

            self.addCleanup(cleanup_spawned)

            def tracked_open(path):
                fd = real_open_lock(path)
                if threading.current_thread().name == "register-B" and Path(path) == alias_path:
                    alias_fds.add(fd)
                return fd

            def blocking_acquire(fd, _timeout_ms):
                if threading.current_thread().name == "register-B" and fd in alias_fds:
                    alias_opened.set()
                fcntl.flock(fd, fcntl.LOCK_EX)
                return True

            def register_b():
                def tracked_popen(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    spawned.append(process)
                    return process

                def capture_receipt(target_id, *args, **kwargs):
                    if target_id == watch_id:
                        result["observation_before_receipt"] = peer_steward._read_json(
                            paths.observation)
                    return real_receipt(target_id, *args, **kwargs)

                with mock.patch.dict(os.environ, env), \
                        mock.patch.object(peer_steward, "_run_herdr_get", return_value=payload_b), \
                        patch_split(peer_steward, "_open_lock", side_effect=tracked_open), \
                        patch_split(peer_steward, "_acquire_bounded", side_effect=blocking_acquire), \
                        mock.patch.object(peer_steward.subprocess, "Popen", side_effect=tracked_popen), \
                        mock.patch.object(peer_steward, "_write_terminal_watch_receipt",
                                          side_effect=capture_receipt), \
                        contextlib.redirect_stdout(output):
                    result["rc"] = peer_steward.main(["watch", "w1:p9"])
                result["stdout"] = output.getvalue()

            registration = threading.Thread(target=register_b, name="register-B")
            registration.start()
            self.assertTrue(alias_opened.wait(10), "registration did not wait at pane admission")

            # The observer cannot persist A while this lock is held. Reap only
            # that exact fixture observer so admission must consume the durable
            # native-wait log result itself.
            observer = original_arm["watcher"]
            if peer_steward._pid_identity_ok(observer["pid"], observer["pid_start"]):
                os.kill(observer["pid"], signal.SIGKILL)
            deadline = time.monotonic() + 10
            while (peer_steward._pid_identity_ok(observer["pid"], observer["pid_start"])
                   and time.monotonic() < deadline):
                time.sleep(0.01)
            self.assertFalse(peer_steward._pid_identity_ok(observer["pid"], observer["pid_start"]))
            identity_path.unlink()
        finally:
            fcntl.flock(alias_fd, fcntl.LOCK_UN)
            os.close(alias_fd)

        registration.join(30)
        self.assertFalse(registration.is_alive(), "registration did not leave pane admission")
        self.assertEqual(result.get("rc"), 0, result.get("stdout", ""))
        fields = self._fields(result["stdout"].splitlines()[0])
        self.assertEqual(fields["state"], "armed", result["stdout"])
        self.assertNotEqual(fields["watch_id"], watch_id)
        admitted = json.loads((self.watch_root / f"{fields['watch_id']}.json").read_text())
        original = json.loads(arm_path.read_text())
        self.assertEqual(original["agent"]["session_id"], "sid-A")
        self.assertEqual(original["deadline"], original_deadline)
        preserved_observation = result.get("observation_before_receipt")
        self.assertIsInstance(preserved_observation, dict)
        preserved_wait = preserved_observation["native_wait"]
        self.assertEqual((preserved_wait["state"], preserved_wait["pid"],
                          preserved_wait["pid_start"]),
                         ("result", wait_record["pid"], wait_record["pid_start"]))
        self.assertEqual(preserved_wait["agent"]["session_id"], "sid-A")
        self.assertEqual(admitted["agent"]["session_id"], "sid-fake")
        waits = self._wait_for_wait_calls(2)
        self.assertEqual(sum("agent wait peer-a" in row for row in waits), 1, self.calllog.read_text())
        self.assertEqual(sum("agent wait w1:p9" in row for row in waits), 1, self.calllog.read_text())

    def test_conflicting_admission_result_stays_unknown_across_reattach_and_admits_new_sid(self):
        identity_path = self.tmp_root / "admission-result-identity.json"
        env = self._env("held")
        env["FAKE_HERDR_IDENTITY_FILE"] = str(identity_path)
        env["FAKE_HERDR_WAIT_STATE"] = "idle"
        identity_path.write_text(json.dumps({"session_id": "sid-A"}))

        armed = self._run("watch", "peer-a", "--timeout", "60000", env=env)
        self.assertEqual(armed.returncode, 0, armed.stdout + armed.stderr)
        watch_id = self._fields(armed.stdout)["watch_id"]
        arm_path = self.watch_root / f"{watch_id}.json"
        paths = peer_steward._watch_paths(watch_id, self.watch_root)
        original_arm = json.loads(arm_path.read_text())
        self.assertEqual(original_arm["agent"]["session_id"], "sid-A")
        original_deadline = original_arm["deadline"]

        observation = self._wait_for_observation(watch_id)
        deadline = time.monotonic() + 10
        while (observation.get("native_wait", {}).get("state") != "started"
               and time.monotonic() < deadline):
            time.sleep(0.01)
            observation = json.loads(paths.observation.read_text())
        wait_record = observation["native_wait"]
        self.assertEqual(wait_record["state"], "started")
        self.assertTrue(wait_record.get("pid_start"))
        self._wait_for_wait_calls(1)

        # Interrupt only this fixture observer, then let its exact native wait
        # finish with B so the next observer starts from the old poisoned
        # admission record shape.
        original_watcher = original_arm["watcher"]
        if peer_steward._pid_identity_ok(
                original_watcher["pid"], original_watcher["pid_start"]):
            os.kill(original_watcher["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 10
        while (peer_steward._pid_identity_ok(
                original_watcher["pid"], original_watcher["pid_start"])
                and time.monotonic() < deadline):
            time.sleep(0.01)
        self.assertFalse(peer_steward._pid_identity_ok(
            original_watcher["pid"], original_watcher["pid_start"]))

        identity_path.write_text(json.dumps({"session_id": "sid-B"}))
        self._release()
        deadline = time.monotonic() + 10
        while (peer_steward._pid_identity_ok(wait_record["pid"], wait_record["pid_start"])
               and time.monotonic() < deadline):
            time.sleep(0.01)
        self.assertFalse(peer_steward._pid_identity_ok(
            wait_record["pid"], wait_record["pid_start"]))
        payload = json.loads(paths.log.read_text(encoding="utf-8"))
        state, result_agent, _code, reason = peer_steward._interpret_payload(payload, "peer-a")
        self.assertEqual((state, result_agent["session_id"]), ("idle", "sid-B"))

        # Recover a disk record produced by the old vulnerable publisher.
        poisoned = dict(wait_record)
        poisoned.update({"state": "result", "agent_state": state,
                         "agent": result_agent, "reason": reason or state})
        observation["state"] = "waiting"
        observation["reason"] = "native-wait-running"
        observation["native_wait"] = poisoned
        paths.observation.write_text(json.dumps(observation))
        reattached_processes = []
        real_popen = peer_steward.subprocess.Popen

        def tracked_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            reattached_processes.append(process)
            return process

        def cleanup_reattached_processes():
            for process in reattached_processes:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

        self.addCleanup(cleanup_reattached_processes)
        with mock.patch.dict(os.environ, env):
            # The pre-spawn closed-watch check reads herdr (agent list, pane census); this
            # test counts only the observer process, so those reads are held to "no evidence".
            with mock.patch.object(peer_steward.subprocess, "Popen", side_effect=tracked_popen), \
                    mock.patch.object(peer_steward.herdr_observation, "pane_census",
                                      return_value=None), \
                    mock.patch.object(peer_steward._steward_check_module(), "_agents",
                                      return_value=[]):
                self.assertTrue(peer_steward._spawn_watch_observer(watch_id, original_arm))
        reattached_arm = json.loads(arm_path.read_text())
        reattached_watcher = reattached_arm["watcher"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            recovered = json.loads(paths.observation.read_text())
            if recovered.get("state") == "unknown":
                break
            time.sleep(0.01)
        self.assertEqual(recovered.get("reason"), "target-identity-conflict")
        self.assertEqual(recovered["native_wait"]["state"], "unknown")
        self.assertEqual((recovered["native_wait"]["pid"],
                          recovered["native_wait"]["pid_start"]),
                         (wait_record["pid"], wait_record["pid_start"]))
        self.assertEqual(recovered["native_wait"]["unresolved_result"]["agent"]["session_id"],
                         "sid-B")
        self.assertFalse(paths.receipt.exists())
        self.assertEqual((reattached_arm["agent"]["session_id"],
                          reattached_arm["deadline"]), ("sid-A", original_deadline))
        deadline = time.monotonic() + 10
        while (peer_steward._pid_identity_ok(
                reattached_watcher["pid"], reattached_watcher["pid_start"])
                and time.monotonic() < deadline):
            time.sleep(0.01)
        self.assertFalse(peer_steward._pid_identity_ok(
            reattached_watcher["pid"], reattached_watcher["pid_start"]))
        self.assertEqual(len(reattached_processes), 1)
        self.assertEqual(reattached_processes[0].wait(timeout=2), 0)
        self.assertEqual(len(self._wait_for_wait_calls(1)), 1, self.calllog.read_text())

        # Admission now sees the same completed B result. It must not publish
        # B as A's result; the existing target-replacement lifecycle may then
        # settle A as pane-exited under its normal terminal-pending contract.
        retained_arm = json.loads(arm_path.read_text())
        admitted = self._run("watch", "peer-a", "--timeout", "120000", env=env)
        self.assertEqual(admitted.returncode, 0, admitted.stdout + admitted.stderr)
        admitted_fields = self._fields(admitted.stdout.splitlines()[0])
        self.assertEqual(admitted_fields["state"], "armed", admitted.stdout)
        self.assertNotEqual(admitted_fields["watch_id"], watch_id)
        admitted_arm = json.loads(
            (self.watch_root / f"{admitted_fields['watch_id']}.json").read_text())
        self.assertEqual(admitted_arm["agent"]["session_id"], "sid-B")
        retained_arm = json.loads(arm_path.read_text())
        self.assertEqual((retained_arm["agent"]["session_id"], retained_arm["deadline"]),
                         ("sid-A", original_deadline))
        self.assertEqual((recovered["state"], recovered["reason"],
                          recovered["native_wait"]["state"],
                          recovered["native_wait"]["pid"],
                          recovered["native_wait"]["pid_start"]),
                         ("unknown", "target-identity-conflict", "unknown",
                          wait_record["pid"], wait_record["pid_start"]))
        self.assertEqual(recovered["native_wait"]["unresolved_result"]["agent"]["session_id"],
                         "sid-B")
        receipt = json.loads(paths.receipt.read_text())
        self.assertEqual((receipt["state"], receipt["reason"]),
                         ("pane-exited", "session-replaced"))
        self.assertNotIn(receipt["state"], {"idle", "done"})
        waits = self._wait_for_wait_calls(2)
        self.assertEqual(len(waits), 2, self.calllog.read_text())
        admitted_paths = peer_steward._watch_paths(admitted_fields["watch_id"], self.watch_root)
        admitted_observation = self._wait_for_observation(admitted_fields["watch_id"])
        deadline = time.monotonic() + 10
        while (admitted_observation.get("native_wait", {}).get("state") != "started"
               and time.monotonic() < deadline):
            time.sleep(0.01)
            admitted_observation = json.loads(admitted_paths.observation.read_text())
        admitted_wait = admitted_observation["native_wait"]
        self.assertEqual(admitted_wait["state"], "started")
        self._release()
        admitted_receipt = self._wait_for_receipt(admitted_fields["watch_id"])
        self.assertEqual((admitted_receipt["state"],
                          admitted_receipt["agent"]["session_id"]), ("idle", "sid-B"))
        admitted_watcher = admitted_arm["watcher"]
        deadline = time.monotonic() + 10
        while (peer_steward._pid_identity_ok(
                admitted_watcher["pid"], admitted_watcher["pid_start"])
                or peer_steward._pid_identity_ok(admitted_wait["pid"], admitted_wait["pid_start"])) \
                and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(peer_steward._pid_identity_ok(
            admitted_watcher["pid"], admitted_watcher["pid_start"]))
        self.assertFalse(peer_steward._pid_identity_ok(
            admitted_wait["pid"], admitted_wait["pid_start"]))

    def test_watch_admission_fails_closed_when_pane_or_endpoint_changes(self):
        import contextlib
        import io

        base_payload = {"result": {"agent": {
            "agent": "claude", "agent_status": "working",
            "agent_session": {"value": "sid-fake"}, "name": "peer-a",
            "pane_id": "w1:p9"}}}
        variants = (
            ("pane", {"pane_id": "w1:p10"}),
            ("harness", {"agent": "codex"}),
        )
        for label, change in variants:
            with self.subTest(change=label):
                output = io.StringIO()
                reads = 0

                def get_changed(_target):
                    nonlocal reads
                    reads += 1
                    agent = dict(base_payload["result"]["agent"])
                    if reads > 1:
                        agent.update(change)
                    return {"result": {"agent": agent}}

                before = len(self._arm_records())
                with mock.patch.dict(os.environ, self._env("held")), \
                        mock.patch.object(peer_steward, "_run_herdr_get", side_effect=get_changed), \
                        contextlib.redirect_stdout(output):
                    rc = peer_steward.main(["watch", "peer-a"])
                self.assertEqual(rc, 4, output.getvalue())
                self.assertIn("watch-target-pane-changed", output.getvalue())
                self.assertEqual(len(self._arm_records()), before)

        output = io.StringIO()
        original_server = peer_steward._HERDR_SESSION
        try:
            with mock.patch.dict(os.environ, self._env("held")), \
                    mock.patch.object(peer_steward, "_run_herdr_get", side_effect=lambda _target: (
                        setattr(peer_steward, "_HERDR_SESSION", "changed-endpoint") or base_payload)), \
                    contextlib.redirect_stdout(output):
                rc = peer_steward.main(["watch", "peer-a"])
            self.assertEqual(rc, 4, output.getvalue())
            self.assertIn("watch-endpoint-changed", output.getvalue())
        finally:
            peer_steward._HERDR_SESSION = original_server

    def test_same_name_new_session_gets_separate_watch_and_old_rearm_cannot_overwrite_claim(self):
        env = self._env("held")
        first = self._run("watch", "peer-a", env=env)
        old_id = self._fields(first.stdout)["watch_id"]
        self._wait_for_observation(old_id)
        old_path = self.watch_root / f"{old_id}.json"
        old_arm = json.loads(old_path.read_text())
        identity_path = self.tmp_root / "identity.json"
        identity_path.write_text(json.dumps({"session_id": "new-session"}))
        env["FAKE_HERDR_IDENTITY_FILE"] = str(identity_path)
        second = self._run("watch", "peer-a", env=env)
        new_id = self._fields(second.stdout)["watch_id"]
        self.assertNotEqual(old_id, new_id)
        self._wait_for_observation(new_id)
        claim = self.watch_root / (peer_steward._dedupe_key("steward-1", "peer-a", []) + ".arm")
        self.assertEqual(claim.read_text().strip(), new_id)
        self.assertEqual(json.loads(old_path.read_text())["agent"]["session_id"], "sid-fake")
        old_receipt = self._wait_for_receipt(old_id)
        self.assertEqual((old_receipt["state"], old_receipt["reason"]),
                         ("pane-exited", "session-replaced"))
        duplicate = self._run("watch", "peer-a", env=env)
        self.assertIn("state=already-armed", duplicate.stdout)
        self.assertEqual(self._fields(duplicate.stdout)["watch_id"], new_id)
        self.assertEqual(claim.read_text().strip(), new_id)
        self.assertEqual(json.loads((self.watch_root / f"{new_id}.json").read_text())["agent"]["session_id"],
                         "new-session")
        self._release()
        self.assertEqual(self._wait_for_receipt(new_id)["agent"]["session_id"], "new-session")

    def test_duplicate_arm_spawns_nothing(self):
        first = self._run("watch", "peer-a", env=self._env("held"))
        watch_id = self._fields(first.stdout)["watch_id"]
        self._wait_for_lock(watch_id)
        before = self._arm_records()
        second = self._run("watch", "peer-a", env=self._env("held"))
        self.assertEqual(second.returncode, 0)
        fields = self._fields(second.stdout)
        self.assertEqual(fields["state"], "already-armed")
        self.assertEqual(fields["watch_id"], watch_id)
        self.assertEqual(self._arm_records(), before)
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
        self.assertEqual(len(self._arm_records()), 1)
        self._release()
        self._wait_for_receipt(watch_id)

    def test_name_pane_and_agent_id_aliases_share_one_watch_and_native_wait(self):
        env = self._env("held")
        # The selected server name is sufficient endpoint identity when the
        # optional observation socket is absent; readiness comes from CLI.
        first = self._run("watch", "peer-a", env=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_fields = self._fields(first.stdout)
        watch_id = first_fields["watch_id"]
        self._wait_for_lock(watch_id)
        original_arm = json.loads((self.watch_root / f"{watch_id}.json").read_text())

        for alias in ("w1:p9", "sid-fake"):
            with self.subTest(alias=alias):
                duplicate = self._run("watch", alias, env=env)
                self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
                fields = self._fields(duplicate.stdout.splitlines()[0])
                self.assertEqual((fields["state"], fields["watch_id"]),
                                 ("already-armed", watch_id), duplicate.stdout)
                self.assertEqual(fields["target"], "peer-a")
                self.assertEqual(len(self._arm_records()), 1)
                self.assertEqual(json.loads((self.watch_root / f"{watch_id}.json").read_text())[
                    "deadline"], original_arm["deadline"])

        self.assertEqual(len(self._wait_for_wait_calls(1)), 1, self.calllog.read_text())
        rows = [row for row in self._all_records() if row["kind"] == "watch"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["to"]["name"], "peer-a")
        self.assertEqual(json.loads((self.watch_root / f"{watch_id}.json").read_text())["target"],
                         "peer-a")
        self._release()
        deadline = time.monotonic() + 10
        observation = self._wait_for_observation(watch_id)
        while ((observation.get("native_wait") or {}).get("state") != "result"
               and time.monotonic() < deadline):
            time.sleep(0.02)
            observation = peer_steward._read_json(
                self.watch_root / f"{watch_id}.observation.json") or {}
        self.assertEqual((observation.get("native_wait") or {}).get("agent_state"), "idle")
        receipt = self._wait_for_receipt(watch_id)
        self.assertEqual(receipt["state"], "idle")

    def test_concurrent_alias_registration_is_serialized_to_one_arm(self):
        env = self._env("held")
        start = threading.Barrier(2)

        def register(target):
            start.wait(timeout=10)
            return self._run("watch", target, env=env)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(register, target) for target in ("peer-a", "w1:p9")]
            results = [future.result(timeout=30) for future in futures]

        for result in results:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        heads = [self._fields(result.stdout.splitlines()[0]) for result in results]
        ids = {head["watch_id"] for head in heads}
        states = sorted(head["state"] for head in heads)
        self.assertEqual(len(ids), 1)
        self.assertEqual(states, ["already-armed", "armed"])
        watch_id = ids.pop()
        self._wait_for_lock(watch_id)
        self.assertEqual(len(self._arm_records()), 1)
        self.assertEqual(len(self._wait_for_wait_calls(1)), 1, self.calllog.read_text())
        self._release()
        self._wait_for_receipt(watch_id)

    def test_alias_reattachment_keeps_legacy_raw_claim_deadline_and_native_wait(self):
        env = self._env("held")
        initial = self._fields(self._run(
            "watch", "peer-a", "--timeout", "60000", env=env).stdout)
        watch_id = initial["watch_id"]
        observation = self._wait_for_observation(watch_id)
        deadline = time.monotonic() + 10
        while (observation.get("native_wait", {}).get("state") != "started"
               and time.monotonic() < deadline):
            time.sleep(0.02)
            observation = json.loads((self.watch_root / f"{watch_id}.observation.json").read_text())
        native_wait = observation["native_wait"]
        self.assertEqual(native_wait["state"], "started")
        self.assertTrue(peer_steward._pid_identity_ok(
            native_wait["pid"], native_wait["pid_start"]))

        arm_path = self.watch_root / f"{watch_id}.json"
        arm = json.loads(arm_path.read_text())
        original_deadline = arm["deadline"]
        original_target_claim = self.watch_root / (
            peer_steward._dedupe_key("steward-1", "peer-a", []) + ".arm")
        self.assertEqual(original_target_claim.read_text().strip(), watch_id)
        # A pre-fix arm has no logical claim key and is located through its raw claim.
        arm.pop("claim_key", None)
        arm_path.write_text(json.dumps(arm))

        old_watcher = int(initial["pid"])
        os.kill(old_watcher, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while (time.monotonic() < deadline
               and peer_steward._pid_identity_ok(old_watcher, initial["pid_start"])):
            time.sleep(0.02)

        duplicate = self._run("watch", "sid-fake", "--timeout", "120000", env=env)
        self.assertEqual(duplicate.returncode, 0, duplicate.stdout + duplicate.stderr)
        fields = self._fields(duplicate.stdout.splitlines()[0])
        self.assertEqual((fields["state"], fields["watch_id"]),
                         ("already-armed", watch_id), duplicate.stdout)
        reattached = json.loads(arm_path.read_text())
        self.assertNotEqual(reattached["watcher"]["pid"], old_watcher)
        self.assertEqual(reattached["target"], "peer-a")
        self.assertEqual(reattached["timeout"], 60000)
        self.assertEqual(reattached["deadline"], original_deadline)
        self.assertNotIn("claim_key", reattached)
        resumed = json.loads((self.watch_root / f"{watch_id}.observation.json").read_text())
        self.assertEqual((resumed["native_wait"]["pid"], resumed["native_wait"]["pid_start"]),
                         (native_wait["pid"], native_wait["pid_start"]))
        self.assertEqual(len(self._wait_calls()), 1, self.calllog.read_text())

        self._release()
        receipt = self._wait_for_receipt(watch_id)
        self.assertEqual(receipt["target"], "peer-a")

    def test_alias_scope_keeps_steward_endpoint_and_until_obligations_distinct(self):
        other_endpoint_env = self._env("held", session_id="steward-1")
        other_endpoint_env["AGENT_HERDR_SESSION"] = "other-endpoint"
        other_endpoint = self._fields(self._run(
            "watch", "sid-fake", env=other_endpoint_env).stdout)
        self.assertEqual(other_endpoint["state"], "armed")

        first_env = self._env("held", session_id="steward-1")
        first = self._fields(self._run("watch", "peer-a", env=first_env).stdout)

        other_steward_env = self._env("held", session_id="steward-2")
        other_steward = self._fields(self._run(
            "watch", "w1:p9", env=other_steward_env).stdout)

        other_until = self._fields(self._run(
            "watch", "w1:p9", "--until", "idle", env=first_env).stdout)
        explicit_default = self._fields(self._run(
            "watch", "sid-fake", "--until", "idle", "--until", "done", "--until", "blocked",
            env=first_env).stdout)
        reordered_default = self._fields(self._run(
            "watch", "peer-a", "--until", "blocked", "--until", "idle", "--until", "done",
            env=first_env).stdout)

        default_ids = {first["watch_id"], other_steward["watch_id"],
                       other_until["watch_id"], explicit_default["watch_id"]}
        self.assertEqual(len(default_ids), 4)
        self.assertNotIn(other_endpoint["watch_id"], default_ids)
        self.assertEqual([first["state"], other_steward["state"],
                          other_until["state"], explicit_default["state"]],
                         ["armed", "armed", "armed", "armed"])
        self.assertEqual((reordered_default["state"], reordered_default["watch_id"]),
                         ("already-armed", explicit_default["watch_id"]))
        arms = [json.loads(path.read_text()) for path in self._arm_records()]
        self.assertEqual(len(arms), 5)
        by_id = {arm["watch_id"]: arm for arm in arms}
        self.assertEqual(by_id[first["watch_id"]]["server"], "default")
        self.assertEqual(by_id[other_steward["watch_id"]]["steward"]["session_id"], "steward-2")
        self.assertEqual(by_id[other_until["watch_id"]]["until"], ["idle"])
        self.assertEqual(set(by_id[explicit_default["watch_id"]]["until"]),
                         {"idle", "done", "blocked"})
        self.assertEqual(by_id[other_endpoint["watch_id"]]["server"], "other-endpoint")
        self.assertEqual(by_id[other_endpoint["watch_id"]]["readiness_endpoint"],
                         "server:other-endpoint")
        self.assertTrue(all(by_id[watch_id]["readiness_endpoint"] == "server:default"
                            for watch_id in default_ids))
        self._wait_for_wait_calls(len(arms))
        for _ in arms:
            self._release()
        for watch_id in (*default_ids, other_endpoint["watch_id"]):
            self._wait_for_receipt(watch_id)

        self.assertEqual(self._wait_for_receipt(other_endpoint["watch_id"])["server"],
                         "other-endpoint")
        from dispatch_seat_handover import acknowledge_watch
        for watch_id in (*default_ids, other_endpoint["watch_id"]):
            arm = by_id[watch_id]
            self.assertEqual(acknowledge_watch(
                watch_id, "claude", arm["steward"]["session_id"],
                "concurrent-endpoint-test"), "created")
            self.assertTrue(peer_steward._watch_paths(watch_id, self.watch_root).ack.exists())

    def test_different_steward_seats_stay_independent_and_successor_retirement_is_separate(self):
        import contextlib
        import io
        import session_tidy
        seat_by_steward = {
            "steward-1": "seat-A",
            "steward-2": "seat-B",
            "steward-3": "seat-A",
        }
        real_popen = subprocess.Popen
        spawned = []

        def tracked_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            spawned.append(process)
            return process

        def register(session_id, target):
            output = io.StringIO()
            env = self._env("held", session_id=session_id)
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(peer_steward.subprocess, "Popen", side_effect=tracked_popen), \
                    mock.patch.object(
                        peer_steward, "_steward_seat",
                        side_effect=lambda _harness, sid: {
                            "kind": "pane", "key": seat_by_steward[sid],
                        }), \
                    contextlib.redirect_stdout(output):
                rc = peer_steward.main(["watch", target])
            self.assertEqual(rc, 0, output.getvalue())
            fields = self._fields(output.getvalue().splitlines()[0])
            arm = json.loads((self.watch_root / f"{fields['watch_id']}.json").read_text())
            watcher = next(process for process in spawned
                           if process.pid == arm["watcher"]["pid"])
            return fields, watcher

        first, first_watcher = register("steward-1", "peer-a")
        second, second_watcher = register("steward-2", "w1:p9")
        self.assertEqual((first["state"], second["state"]), ("armed", "armed"))
        self.assertNotEqual(first["watch_id"], second["watch_id"])
        self._wait_for_wait_calls(2)

        # A durable same-seat handover transfers the original watch identity.
        # A different seat stays independent even when Herdr reports the same pane.
        first_arm_path = self.watch_root / f"{first['watch_id']}.json"
        first_arm = json.loads(first_arm_path.read_text())
        seat = session_tidy.Seat("pane", "seat-A", "w1:p9", "claude")
        first_arm["steward_seat"] = seat.as_dict()
        peer_steward._write_json_atomic(first_arm_path, first_arm)
        steward = first_arm["steward"]
        carried_watch = {
            "watch_id": first["watch_id"],
            "origin": {"harness": steward["harness"], "sid": steward["session_id"]},
            "holder": {"harness": "claude", "sid": "steward-3"},
            "seat_key": seat.key, "storage_root": str(peer_steward.peer_message.peer_state_root()),
            "server": first_arm["server"], "target": first_arm["target"],
            "agent": dict(first_arm["agent"]), "until": list(first_arm["until"]),
            "armed_ts": first_arm["armed_ts"], "deadline": first_arm["deadline"],
            "lineage": [{"from": "steward-1", "to": "steward-3", "snapshot_id": "fixture"}],
        }
        with mock.patch.dict(os.environ, self._env("held", session_id="steward-1")):
            with session_tidy.seat_lock(seat.key):
                session_tidy._append_ledger_entry(seat, {
                    "ts": session_tidy.now_epoch(), "harness": "claude", "sid": "steward-3",
                    "event": "handover", "source": "clear", "from": "steward-1", "seat_key": seat.key,
                    "bindings": [], "steward": {"schema": 1, "roles": [],
                                                   "watches": [carried_watch]},
                })
        successor, successor_watcher = register("steward-3", "sid-fake")
        self.assertEqual(successor["state"], "already-armed")
        self.assertEqual(successor["watch_id"], first["watch_id"])
        self.assertFalse((self.watch_root / f"{second['watch_id']}.receipt.json").exists())
        self.assertEqual(len(self._arm_records()), 2)

        self._wait_for_wait_calls(2)
        self._release()
        self._release()
        self._wait_for_receipt(first["watch_id"])
        self._wait_for_receipt(second["watch_id"])
        self.assertIs(first_watcher, successor_watcher)
        for process in (first_watcher, second_watcher):
            process.wait(timeout=10)
        for process in spawned:
            if process.poll() is None:
                process.wait(timeout=10)

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

        def violations(source, *, bounded_ui):
            tree = ast.parse(source)
            parents = {child: parent for parent in ast.walk(tree)
                       for child in ast.iter_child_nodes(parent)}
            background_backoff = {"cmd_obligation_runner": "delay"}

            def enclosing_function(node):
                parent = parents.get(node)
                while parent is not None:
                    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        return parent.name
                    parent = parents.get(parent)
                return None

            found = []
            for node in ast.walk(tree):
                if (isinstance(node, ast.While) and isinstance(node.test, ast.Constant)
                        and node.test.value is True):
                    if enclosing_function(node) == "cmd_watch_run":
                        event_wait = any(
                            isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Attribute)
                            and call.func.attr == "select"
                            and isinstance(call.func.value, ast.Name)
                            and call.func.value.id == "select"
                            for statement in node.body for call in ast.walk(statement)
                        )
                        if not event_wait:
                            found.append((node.lineno, "watch loop lacks blocking event wait"))
                    elif enclosing_function(node) not in background_backoff:
                        found.append((node.lineno, "unbounded while"))
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                sleeping = (isinstance(func, ast.Attribute) and func.attr == "sleep"
                            or isinstance(func, ast.Name) and func.id == "sleep")
                if not sleeping:
                    continue
                background = background_backoff.get(enclosing_function(node))
                if background:
                    if (isinstance(func, ast.Attribute) and func.attr == "sleep"
                            and isinstance(func.value, ast.Name) and func.value.id == "time"
                            and len(node.args) == 1 and isinstance(node.args[0], ast.Name)
                            and node.args[0].id == background):
                        continue
                    found.append((node.lineno, "background observer sleep is not bounded backoff"))
                    continue
                branch, parent = node, parents.get(node)
                bounded = False
                while parent is not None:
                    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef,
                                           ast.Lambda, ast.ClassDef, ast.For, ast.AsyncFor)):
                        break
                    if isinstance(parent, ast.While):
                        test = parent.test
                        if (bounded_ui and branch in parent.body
                                and isinstance(test, ast.Compare)
                                and len(test.ops) == len(test.comparators) == 1
                                and isinstance(test.ops[0], ast.Lt)
                                and isinstance(test.left, ast.Call)
                                and not test.left.args and not test.left.keywords
                                and isinstance(test.left.func, ast.Attribute)
                                and test.left.func.attr == "monotonic"
                                and isinstance(test.left.func.value, ast.Name)
                                and test.left.func.value.id == "time"
                                and isinstance(test.comparators[0], ast.Name)):
                            deadline = test.comparators[0].id
                            bounded = not any(
                                isinstance(inner, ast.Name) and inner.id == deadline
                                and isinstance(inner.ctx, (ast.Store, ast.Del))
                                for statement in parent.body for inner in ast.walk(statement)
                            )
                        break
                    branch, parent = parent, parents.get(parent)
                if not bounded:
                    found.append((node.lineno, "sleep outside fixed monotonic deadline body"))
            return found

        # Structural examples exercise the guard itself; function names grant
        # nothing, and a deadline cannot be refreshed inside its observed loop.
        bounded = "while time.monotonic() < deadline:\n    time.sleep(.05)\n"
        self.assertEqual(violations(bounded, bounded_ui=True), [])
        self.assertTrue(violations(bounded, bounded_ui=False))  # wake hook never sleeps
        self.assertEqual(violations("text = 'time.sleep(1)'  # while True\n", bounded_ui=True), [])
        for forbidden in (
            "time.sleep(1)\n",
            "while True:\n    pass\n",
            "while ready():\n    time.sleep(1)\n",
            "while time.time() < deadline:\n    time.sleep(1)\n",
            "while time.monotonic() <= deadline:\n    time.sleep(1)\n",
            "while time.monotonic() < deadline:\n    pass\nelse:\n    time.sleep(1)\n",
            "while time.monotonic() < deadline:\n    deadline = time.monotonic() + 1\n    time.sleep(1)\n",
            "while time.monotonic() < deadline:\n    def later():\n        time.sleep(1)\n",
            "while time.monotonic() < deadline:\n    for event in events():\n        time.sleep(1)\n",
            "while time.monotonic() < deadline:\n    while ready():\n        time.sleep(1)\n",
        ):
            with self.subTest(source=forbidden):
                self.assertTrue(violations(forbidden, bounded_ui=True))

        # peer-steward.py and the modules it is split into (peer_steward_*.py).
        parts = [path.name for path in sorted(_HERE.glob("peer_steward_*.py"))
                 if "Split out of `peer-steward.py`" in path.read_text()]
        for name in ("peer-steward.py", *parts, "../hooks/peer-steward-rewake.py"):
            path = (_HERE / name).resolve()
            self.assertTrue(path.exists(), f"{name} missing")
            source = path.read_text()
            self.assertEqual(violations(source, bounded_ui=name != "../hooks/peer-steward-rewake.py"),
                             [], f"{path.name}: foreground waits are fixed-deadline and task observers are event-driven")
            if name == "peer-steward.py":
                self.assertIn("delay = min(15.0, delay * 1.7)", source)


class WatcherReceiptTest(_WatchMixin, unittest.TestCase):
    """A56-2."""

    def test_recovered_native_result_refreshes_authority_without_replaying_wait(self):
        watch_id = "recovered-ready-watch"
        arm = {
            "schema_version": 1, "watch_id": watch_id, "target": "peer-a",
            "server": "named", "armed_ts": "then", "timeout": 30_000,
            "observer_protocol": 2, "rearmed_from": "old-watch", "rearm_count": 1,
            "agent": {"harness": "claude", "session_id": "sid-a",
                      "name": "peer-a", "pane": "w1:p1"},
            "steward": {"harness": "claude", "session_id": "steward-a",
                        "project": "fixture"},
        }
        paths = peer_steward._watch_paths(watch_id, self.watch_root)
        self.watch_root.mkdir(parents=True, exist_ok=True)
        peer_steward._write_json_atomic(paths.arm, arm)
        native_result = {"state": "result", "agent_state": "idle",
                         "agent": arm["agent"], "reason": "idle"}
        peer_steward._write_json_atomic(paths.observation, {
            "state": "pending", "native_wait": native_result,
        })
        store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        lock_fd = peer_steward._open_lock(paths.lock)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        args = SimpleNamespace(
            lock_fd=lock_fd, server="named", expected_harness="claude",
            expected_session_id="sid-a", expected_pane="w1:p1", watch_id=watch_id,
            target="peer-a", until=["idle"], timeout=30_000, ref=["cycle-ref"],
            steward_harness="claude", steward_session_id="steward-a",
            steward_project="fixture", armed_ts="then", rearmed_from="old-watch",
            rearm_count=1, service_current=None,
        )
        identity = {"server": "named", "pane": "w1:p1", "native_state": "idle"}
        try:
            with patch_split(peer_steward, "_watch_root", return_value=self.watch_root), \
                    mock.patch.object(peer_steward.peer_obligations, "ObligationStore",
                                      return_value=store), \
                    mock.patch.object(peer_steward, "_watch_timeout_remaining_ms",
                                      return_value=(30_000, "active")), \
                    mock.patch.object(peer_steward, "_terminal_watch_request", return_value=None), \
                    patch_split(peer_steward, "_watch_native_result_identity",
                                      return_value=("match", "sid-a", "")), \
                    mock.patch.object(peer_steward, "_watch_readiness_identity",
                                      return_value=identity), \
                    mock.patch.object(peer_steward.herdr_observation,
                                      "socket_path_for_endpoint", return_value=None), \
                    mock.patch.object(peer_steward.shared_readiness, "wake_runner",
                                      return_value=True), \
                    mock.patch.object(peer_steward.peer_obligations,
                                      "ensure_normal_runner"), \
                    mock.patch.object(peer_steward, "_watch_transition_readiness",
                                      return_value=SimpleNamespace(state="ready",
                                                                   reason="fresh-ready")) as refresh, \
                    mock.patch.object(peer_steward, "_prepare_watch_notice"), \
                    mock.patch.object(peer_steward, "_record"), \
                    mock.patch.object(peer_steward.subprocess, "Popen",
                                      side_effect=AssertionError("native wait replayed")), \
                    mock.patch.object(peer_steward.os, "_exit", side_effect=SystemExit):
                with self.assertRaises(SystemExit):
                    peer_steward.cmd_watch_run(args)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
        self.assertEqual((receipt["state"], receipt["rearmed_from"],
                          receipt["observer_generation"]), ("idle", "old-watch", 2))
        self.assertEqual(refresh.call_count, 2)
        self.assertTrue(refresh.call_args.kwargs.get("authority_lock_held"))
        self.assertEqual(len(self._wait_calls()), 0)

    def test_pending_receipt_recheck_waits_for_the_same_watch_refresh(self):
        watch_id = "pending-receipt-refresh-watch"
        arm = {
            "schema_version": 1, "watch_id": watch_id, "target": "peer-a",
            "server": "named", "armed_ts": "then", "timeout": 30_000,
            "observer_protocol": 2,
            "agent": {"harness": "claude", "session_id": "sid-a",
                      "name": "peer-a", "pane": "w1:p1"},
            "steward": {"harness": "claude", "session_id": "steward-a",
                        "project": "fixture"},
        }
        paths = peer_steward._watch_paths(watch_id, self.watch_root)
        self.watch_root.mkdir(parents=True, exist_ok=True)
        peer_steward._write_json_atomic(paths.arm, arm)
        native_result = {"state": "result", "agent_state": "idle",
                         "agent": arm["agent"], "reason": "idle"}
        identity = {"server": "named", "pane": "w1:p1", "native_state": "idle"}
        start = str(peer_steward.process_start_ticks(os.getpid()) or "")
        token = "same-watch-delayed-refresh-token"
        original_observation = {
            "state": "pending", "native_wait": native_result,
            "readiness_transition": {
                "token": token, "identity": identity,
                "consumer": {"pid": os.getpid(), "pid_start": start},
                "requested_at": time.time(),
            },
        }
        peer_steward._write_json_atomic(paths.observation, original_observation)
        store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        lock_fd = peer_steward._open_lock(paths.lock)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        args = SimpleNamespace(
            lock_fd=lock_fd, server="named", expected_harness="claude",
            expected_session_id="sid-a", expected_pane="w1:p1", watch_id=watch_id,
            target="peer-a", until=["idle"], timeout=30_000, ref=["cycle-ref"],
            steward_harness="claude", steward_session_id="steward-a",
            steward_project="fixture", armed_ts="then", rearmed_from=None,
            rearm_count=0, service_current=None,
        )
        results = [
            SimpleNamespace(state="ready", reason="first-runner-ready"),
            SimpleNamespace(state="pending", reason="shared-readiness-revalidating"),
            SimpleNamespace(state="ready", reason="fresh-runner-ready"),
            SimpleNamespace(state="ready", reason="fresh-runner-ready"),
        ]
        lock_checks = []
        observation_writes = []
        write_observation = peer_steward._write_watch_observation

        def delayed_refresh(*_args, **kwargs):
            lock_checks.append(bool(kwargs.get("authority_lock_held")))
            result = results.pop(0)
            if result.state == "pending":
                # Model the existing runner's response wake arriving while the
                # observer owns its watch lock and is between authority checks.
                wake_fd = peer_steward._WATCH_WAKE_FD
                if wake_fd is None:
                    raise AssertionError("watch observer wake pipe is unavailable")
                os.write(wake_fd, b"ready")
            return result

        def capture_observation(write_paths, value, **kwargs):
            observation_writes.append(dict(value))
            return write_observation(write_paths, value, **kwargs)

        try:
            with patch_split(peer_steward, "_watch_root", return_value=self.watch_root), \
                    mock.patch.object(peer_steward.peer_obligations, "ObligationStore",
                                      return_value=store), \
                    mock.patch.object(peer_steward, "_watch_timeout_remaining_ms",
                                      return_value=(30_000, "active")), \
                    mock.patch.object(peer_steward, "_terminal_watch_request", return_value=None), \
                    patch_split(peer_steward, "_watch_native_result_identity",
                                      return_value=("match", "sid-a", "")), \
                    mock.patch.object(peer_steward, "_watch_readiness_identity",
                                      return_value=identity), \
                    patch_split(peer_steward, "_write_watch_observation",
                                      side_effect=capture_observation), \
                    mock.patch.object(peer_steward.herdr_observation,
                                      "socket_path_for_endpoint", return_value=None), \
                    mock.patch.object(peer_steward.shared_readiness, "wake_runner",
                                      return_value=True), \
                    mock.patch.object(peer_steward.peer_obligations,
                                      "ensure_normal_runner"), \
                    mock.patch.object(peer_steward, "_watch_transition_readiness",
                                      side_effect=delayed_refresh) as refresh, \
                    mock.patch.object(peer_steward, "_prepare_watch_notice") as prepare_notice, \
                    mock.patch.object(peer_steward, "_record") as record, \
                    mock.patch.object(peer_steward.subprocess, "Popen",
                                      side_effect=AssertionError("native wait replayed")), \
                    mock.patch.object(peer_steward.os, "_exit", side_effect=SystemExit):
                with self.assertRaises(SystemExit):
                    peer_steward.cmd_watch_run(args)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        self.assertTrue(paths.receipt.exists(),
                        "a pending receipt-edge authority check must leave the observer waiting")
        receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
        self.assertEqual((receipt["watch_id"], receipt["state"]), (watch_id, "idle"))
        pending_writes = [value for value in observation_writes
                          if value.get("reason") == "shared-readiness-revalidating"]
        self.assertTrue(pending_writes, "receipt-edge pending state must be durably observed")
        self.assertEqual(pending_writes[0]["native_wait"], native_result)
        self.assertEqual(pending_writes[0]["readiness_transition"]["token"], token)
        self.assertEqual(lock_checks, [False, True, False, True])
        self.assertEqual((refresh.call_count, prepare_notice.call_count, record.call_count),
                         (4, 1, 1))
        self.assertEqual(len(self._wait_calls()), 0)

    def test_conflicting_native_session_stays_unknown_without_replaying_wait(self):
        env = self._env("held")
        identity_path = self.tmp_root / "conflicting-native-identity.json"
        env["FAKE_HERDR_IDENTITY_FILE"] = str(identity_path)
        armed = self._fields(self._run("watch", "peer-a", env=env).stdout)
        watch_id = armed["watch_id"]
        wait_record = self._wait_for_native_wait_started(watch_id)
        self._wait_for_wait_calls(1)
        before = self.calllog.read_bytes()

        # The production watcher has already entered its select-based event wait.
        # A short bounded fixture window measures repeated transport calls without
        # turning the product wait into a timer loop.
        time.sleep(0.2)
        self.assertEqual(self.calllog.read_bytes(), before)
        self.assertEqual(len(self._wait_calls()), 1)
        self.assertTrue(peer_steward._pid_identity_ok(
            wait_record["pid"], wait_record["pid_start"]))

        identity_path.write_text(json.dumps({"session_id": "sid-contradictory",
                                             "pane": "w1:p9"}), encoding="utf-8")
        self._release()
        deadline = time.monotonic() + 10
        observation = {}
        while time.monotonic() < deadline:
            observation = peer_steward._read_json(
                self.watch_root / f"{watch_id}.observation.json") or {}
            native = observation.get("native_wait") or {}
            if native.get("state") == "unknown" and observation.get("state") == "unknown":
                break
            time.sleep(0.02)
        self.assertEqual((observation.get("state"),
                          (observation.get("native_wait") or {}).get("agent_state")),
                         ("unknown", "idle"),
                         f"observation={observation}; watcher_log="
                         f"{(self.watch_root / f'{watch_id}.log').read_text(errors='replace')[-2000:]}")
        self.assertEqual(observation.get("reason"), "target-identity-conflict")
        self.assertEqual(((observation.get("native_wait") or {}).get("unresolved_result")
                          or {}).get("agent", {}).get("session_id"), "sid-contradictory")
        self.assertFalse((self.watch_root / f"{watch_id}.receipt.json").exists(),
                         "a conflicting nonempty native SID must not settle a watch")
        self.assertEqual(len(self._wait_calls()), 1)

    def test_receipt_is_atomic_complete_and_text_free(self):
        env = self._env("held")
        env["FAKE_HERDR_WAIT_STATE"] = "working"
        proc = self._run("watch", "peer-a", env=env)
        watch_id = self._fields(proc.stdout)["watch_id"]
        self._wait_for_wait_calls(1)
        self._release()
        receipt = self._wait_for_receipt(watch_id)

        # Receipt publication precedes the notice write and process exit.
        # Wait for the real lock release before asserting either one; seeing
        # the receipt is not proof that the watcher has already exited.
        lock = self.watch_root / f"{watch_id}.lock"
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT)
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        self.fail(f"watcher did not release its lock for {watch_id}")
                    time.sleep(0.02)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

        self.assertEqual(set(receipt), {
            "schema_version", "watch_id", "target", "steward", "armed_ts", "done_ts",
            "state", "agent", "herdr_exit", "watcher", "rearmed_from", "refs",
            "server", "observer_generation",
        })
        self.assertEqual(receipt["state"], "working")
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

    def test_native_timeout_is_terminal_after_one_wait(self):
        proc = self._run("watch", "peer-a", env=self._env("checkpoint"))
        watch_id = self._fields(proc.stdout)["watch_id"]
        receipt = self._wait_for_receipt(watch_id)
        self.assertEqual((receipt["state"], receipt["reason"]), ("timeout", "wait-timeout"))
        waits = [line for line in self.calllog.read_text().splitlines()
                 if line.startswith("agent wait ")]
        self.assertEqual(len(waits), 1, self.calllog.read_text())

    def test_agent_not_found_and_complete_pane_absence_settle_terminal_receipt(self):
        env = self._env("held")
        mode_file = self.tmp_root / "herdr-mode"
        env["FAKE_HERDR_MODE_FILE"] = str(mode_file)
        env["FAKE_HERDR_PANES"] = "[]"
        armed = self._run("watch", "peer-a", env=env)
        self.assertEqual(armed.returncode, 0, armed.stderr)
        fields = self._fields(armed.stdout)
        self._wait_for_lock(fields["watch_id"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if "agent wait" in self.calllog.read_text(encoding="utf-8"):
                break
            time.sleep(0.01)
        else:
            self.fail("watcher did not enter the bounded native wait")
        mode_file.write_text("not-found", encoding="utf-8")
        self._release()
        receipt = self._wait_for_receipt(fields["watch_id"])
        self.assertEqual(receipt["state"], "pane-exited")
        self.assertEqual(receipt["reason"], "pane-absent")
        self.assertEqual(peer_steward._JOIN_EXIT[receipt["state"]], 2)
        self.assertFalse(any(row["kind"] == "notice" and
                             row["delivery"]["status"] == "received"
                             for row in self._all_records()))
        before = len(self.calllog.read_text(encoding="utf-8").splitlines())
        for _ in range(2):
            self.assertEqual(self._run("__ensure-obligations", env=env).returncode, 0)
        after = len(self.calllog.read_text(encoding="utf-8").splitlines())
        self.assertEqual(after, before, "a terminal receipt must prevent observer resurrection")

    def test_completed_retire_requests_terminal_receipt_under_existing_watch_lock(self):
        self.watch_root.mkdir(parents=True, exist_ok=True)
        watch_id = "closed-pane-watch"
        arm = {"schema_version": 1, "watch_id": watch_id, "target": "peer-a",
               "server": "named", "armed_ts": "then", "agent": {
                   "harness": "opencode", "session_id": "sid-9", "name": "peer-a",
                   "pane": "w1:p9"}, "steward": {}, "watcher": {}, "refs": []}
        paths = peer_steward._watch_paths(watch_id, self.watch_root)
        paths.arm.write_text(json.dumps(arm), encoding="utf-8")
        lock_fd = peer_steward._open_lock(paths.lock)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            matched = peer_steward._request_watch_terminal({
                "server": "named", "pane": "w1:p9", "harness": "opencode",
                "session_id": "sid-9", "foreground": None,
            }, "completed-retire")
            self.assertEqual(matched, 1)
            self.assertFalse(paths.receipt.exists())
            self.assertEqual(json.loads(paths.observation.read_text())["state"], "terminal_pending")
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        self.assertTrue(peer_steward._settle_terminal_watch_if_unobserved(
            watch_id, arm, self.watch_root))
        receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
        self.assertEqual((receipt["state"], receipt["reason"]),
                         ("pane-exited", "completed-retire"))
        self.assertFalse(paths.observation.exists())

    def test_seatless_claude_watch_settles_only_on_complete_empty_census(self):
        self.watch_root.mkdir(parents=True, exist_ok=True)
        watch_id = "seatless-ended"
        arm = {"schema_version": 1, "watch_id": watch_id, "target": "peer-a",
               "server": "default", "armed_ts": "then", "refs": ["attempt:one"],
               "observer_generation": 4, "steward_seat": None,
               "agent": {"harness": "codex", "session_id": "target-sid",
                         "name": "peer-a", "pane": "-"},
               "steward": {"harness": "claude", "session_id": "recipient-sid",
                           "project": "SR_ModelA"},
               "watcher": {}}
        paths = peer_steward._watch_paths(watch_id, self.watch_root)
        paths.arm.write_text(json.dumps(arm), encoding="utf-8")
        native_home = Path.home() / ".claude"
        (native_home / "history.jsonl").write_text(
            json.dumps({"sessionId": "recipient-sid"}) + "\n", encoding="utf-8")

        with mock.patch.object(claude_process_identity, "claude_session_census",
                               return_value=(True, set(), set())):
            settled = peer_steward._settle_ended_seatless_recipient(
                watch_id, arm, self.watch_root)

        self.assertTrue(settled)
        receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
        self.assertEqual((receipt["state"], receipt["reason"]),
                         ("steward-exited", "steward-exited"))
        self.assertEqual(receipt["refs"], ["attempt:one"])
        self.assertEqual(receipt["observer_generation"], 4)
        self.assertFalse(paths.observation.exists())

    def test_seatless_claude_watch_without_native_existence_evidence_stays_pending(self):
        self.watch_root.mkdir(parents=True, exist_ok=True)
        watch_id = "seatless-unseen"
        arm = {"schema_version": 1, "watch_id": watch_id, "target": "peer-a",
               "server": "default", "armed_ts": "then", "steward_seat": None,
               "agent": {"harness": "codex", "session_id": "target-sid", "pane": "-"},
               "steward": {"harness": "claude", "session_id": "never-seen-session"},
               "watcher": {}}
        paths = peer_steward._watch_paths(watch_id, self.watch_root)
        paths.arm.write_text(json.dumps(arm), encoding="utf-8")

        with mock.patch.object(claude_process_identity, "claude_session_census",
                               return_value=(True, set(), set())):
            self.assertFalse(peer_steward._settle_ended_seatless_recipient(
                watch_id, arm, self.watch_root))

        self.assertTrue(paths.arm.exists())
        self.assertFalse(paths.receipt.exists())
        self.assertFalse(paths.observation.exists())

    def test_seatless_watch_alive_or_incomplete_census_remains_pending(self):
        from recipient_lifetime import RecipientCensus
        self.watch_root.mkdir(parents=True, exist_ok=True)
        watch_id = "seatless-uncertain"
        arm = {"schema_version": 1, "watch_id": watch_id, "target": "peer-a",
               "server": "default", "armed_ts": "then", "steward_seat": None,
               "agent": {"harness": "codex", "session_id": "target-sid", "pane": "-"},
               "steward": {"harness": "claude", "session_id": "recipient-sid"},
               "watcher": {}}
        paths = peer_steward._watch_paths(watch_id, self.watch_root)
        paths.arm.write_text(json.dumps(arm), encoding="utf-8")
        for result in ("alive", "unknown"):
            with self.subTest(result=result):
                census = RecipientCensus(lambda *_args, value=result: value)
                self.assertFalse(peer_steward._settle_ended_seatless_recipient(
                    watch_id, arm, self.watch_root, census=census))
                self.assertFalse(paths.receipt.exists())

    def test_terminal_pending_reason_is_written_before_a_busy_observer_is_woken(self):
        from recipient_lifetime import RecipientCensus
        self.watch_root.mkdir(parents=True, exist_ok=True)
        watch_id = "seatless-lock-race"
        arm = {"schema_version": 1, "watch_id": watch_id, "target": "peer-a",
               "server": "default", "armed_ts": "then", "steward_seat": None,
               "agent": {"harness": "codex", "session_id": "target-sid", "pane": "-"},
               "steward": {"harness": "claude", "session_id": "recipient-sid"},
               "watcher": {}}
        paths = peer_steward._watch_paths(watch_id, self.watch_root)
        paths.arm.write_text(json.dumps(arm), encoding="utf-8")
        lock_fd = peer_steward._open_lock(paths.lock)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self.assertTrue(peer_steward._settle_ended_seatless_recipient(
                watch_id, arm, self.watch_root,
                census=RecipientCensus(lambda *_args: "ended")))
            observation = json.loads(paths.observation.read_text(encoding="utf-8"))
            self.assertEqual((observation["state"], observation["reason"]),
                             ("terminal_pending", "steward-exited"))
            self.assertFalse(paths.receipt.exists())
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


class WatchTerminalReconcileTest(unittest.TestCase):
    def test_live_seatless_claude_watch_schedules_existing_reconciliation_pass(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        arm = {"watch_id": "seatless-live", "steward_seat": None,
               "steward": {"harness": "claude", "session_id": "recipient-sid"},
               "watcher": {"pid": 123, "pid_start": "456"}}
        with patch_split(peer_steward, "_watch_root", return_value=root), \
             patch_split(peer_steward, "_watch_entries", return_value=[("seatless-live", arm)]), \
             patch_split(peer_steward, "_watcher_present", return_value=True):
            self.assertTrue(peer_steward._watch_reconciliation_needed(entries=[("seatless-live", arm)]))

    def test_server_rearm_drops_a_socket_path_bound_to_another_endpoint(self):
        module = peer_steward
        foreign = module._herdr_env_for_server("fourth", {
            "HERDR_SOCKET_PATH": "/default.sock"})
        self.assertEqual(foreign["AGENT_HERDR_SESSION"], "fourth")
        self.assertNotIn("HERDR_SOCKET_PATH", foreign)
        exact = module._herdr_env_for_server("fourth", {
            "AGENT_HERDR_SESSION": "default", "HERDR_SESSION": "fourth",
            "HERDR_SOCKET_PATH": "/fourth.sock"})
        self.assertEqual(exact["HERDR_SOCKET_PATH"], "/fourth.sock")
        agent_conflict = module._herdr_env_for_server("fourth", {
            "AGENT_HERDR_SESSION": "default", "HERDR_SESSION": "fourth",
            "HERDR_SOCKET_PATH": "/fourth.sock"})
        self.assertEqual(agent_conflict["HERDR_SOCKET_PATH"], "/fourth.sock")
        back_to_default = module._herdr_env_for_server("default", {
            "HERDR_SESSION": "fourth", "HERDR_SOCKET_PATH": "/fourth.sock"})
        self.assertNotIn("HERDR_SOCKET_PATH", back_to_default)
        self.assertNotIn("AGENT_HERDR_SESSION", back_to_default)
        same_default = module._herdr_env_for_server("default", {
            "AGENT_HERDR_SESSION": "fourth", "HERDR_SOCKET_PATH": "/default.sock"})
        self.assertEqual(same_default["HERDR_SOCKET_PATH"], "/default.sock")

    def test_terminal_event_requires_a_fresh_snapshot_that_rejects_reused_pane_id(self):
        expected = {"harness": "codex", "session_id": "sid-old", "pane": "w1:p9"}
        initial = SimpleNamespace(endpoint="named", complete=True,
                                  terminal={"w1:p9": "pane-closed"}, panes=[])

        class Transport:
            def __init__(self, rows):
                self.rows = rows

            def resnapshot(self):
                return SimpleNamespace(endpoint="named", complete=True,
                                       terminal={}, panes=self.rows)

        reused = Transport([{"pane_id": "w1:p9", "agent": "codex",
                             "agent_session": {"value": "sid-old"}}])
        self.assertIsNone(peer_steward._watch_terminal_from_view(
            initial, expected, "named", reused))
        new_expected = {"harness": "codex", "session_id": "sid-new", "pane": "w1:p9"}
        new_occupant = Transport([{"pane_id": "w1:p9", "agent": "codex",
                                   "agent_session": {"value": "sid-new"}}])
        self.assertIsNone(peer_steward._watch_terminal_from_view(
            initial, new_expected, "named", new_occupant))
        absent = Transport([])
        self.assertEqual(peer_steward._watch_terminal_from_view(
            initial, expected, "named", absent), "pane-closed")

    def test_ensure_settles_closed_pane_receipt_before_spawning_orphan_observer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watch_id = "terminal-reconcile"
            arm = {"schema_version": 1, "watch_id": watch_id, "target": "peer",
                   "server": "named", "armed_ts": "then", "agent": {
                       "harness": "codex", "session_id": "sid-old", "name": "peer",
                       "pane": "w1:p9"}, "steward": {}, "watcher": {}, "refs": []}
            paths = peer_steward._watch_paths(watch_id, root)
            paths.arm.write_text(json.dumps(arm), encoding="utf-8")
            view = SimpleNamespace(endpoint="named", complete=True,
                                   terminal={"w1:p9": "pane-closed"}, panes=[])

            class Transport:
                def observe(self):
                    return view

                def resnapshot(self):
                    return SimpleNamespace(endpoint="named", complete=True,
                                           terminal={}, panes=[])

                def close(self):
                    pass

            with patch_split(peer_steward, "_watch_root", return_value=root), \
                 mock.patch.object(peer_steward.herdr_observation,
                                   "socket_path_for_endpoint", return_value="/fixture.sock"), \
                 mock.patch.object(peer_steward.herdr_observation,
                                   "ObservationTransport", return_value=Transport()), \
                 patch_split(peer_steward, "_watcher_present", return_value=False), \
                 mock.patch.object(peer_steward, "_spawn_watch_observer") as spawn:
                peer_steward._ensure_watch_observers()
                spawn.assert_not_called()
                receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
                self.assertEqual((receipt["state"], receipt["reason"]),
                                 ("pane-exited", "pane-closed"))
                peer_steward._ensure_watch_observers()
                spawn.assert_not_called()

    def test_update_replaces_only_the_exact_live_watch_and_rearms_same_duty(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watch_id = "legacy-watch"
            arm = {"schema_version": 1, "watch_id": watch_id, "target": "fixture-target",
                   "server": "named", "armed_ts": "then", "until": ["idle"],
                   "agent": {"harness": "codex", "session_id": "sid-watch", "pane": "w1:p9"},
                   "steward": {"harness": "claude", "session_id": "sid-steward"},
                   "watcher": {"pid": 321, "pid_start": "654"}, "refs": ["attempt-one"],
                   "rearm_count": 0, "observer_generation": 1}
            paths = peer_steward._watch_paths(watch_id, root)
            paths.arm.write_text(json.dumps(arm), encoding="utf-8")
            binding = "/fixture/managed/current"
            required = peer_steward._watch_observer_duty_argv(
                watch_id, arm, rearm_generation=0, timeout_before_refs=True)
            with patch_split(peer_steward, "_watch_root", return_value=root), \
                 patch_split(peer_steward, "_watch_entries", return_value=[(watch_id, arm)]), \
                 mock.patch.object(peer_steward, "_settle_terminal_watch_if_unobserved",
                                   return_value=False), \
                 patch_split(peer_steward, "_watcher_present", side_effect=[True, False]), \
                 mock.patch.object(peer_steward.peer_obligations, "validate_service_binding",
                                   return_value=binding), \
                 mock.patch.object(peer_steward.peer_obligations,
                                   "replace_managed_observer", return_value="replaced") as replace, \
                 mock.patch.object(peer_steward.herdr_observation,
                                   "socket_path_for_endpoint", return_value=None), \
                 patch_split(peer_steward, "_watch_terminal_from_view", return_value=None), \
                 mock.patch.object(peer_steward, "_spawn_watch_observer") as spawn:
                peer_steward._ensure_watch_observers(service_current=binding)
            replace.assert_called_once_with(
                321, "654", paths.lock, "utilities/peer-steward.py", required,
                binding, lock_fd_option="--lock-fd", exact_arguments=True,
                argument_variants=[])
            spawn.assert_called_once_with(watch_id, arm, service_current=binding,
                                          recipient_census=mock.ANY)


class ClosedWatchBeforeRearmTest(unittest.TestCase):
    """A watch whose supervisor closed or whose target was replaced ends without a socket."""

    WATCH = "closed-before-rearm"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.arm = {"schema_version": 1, "watch_id": self.WATCH, "target": "peer", "server": "named",
                    "armed_ts": "then", "until": ["idle"], "refs": [], "watcher": {},
                    "agent": {"harness": "codex", "session_id": "sid-target", "pane": "w1:p9"},
                    "steward": {"harness": "codex", "session_id": "sid-steward"},
                    "steward_server": "named",
                    "steward_seat": {"kind": "pane", "key": "seat-k", "pane": "w1:p1"},
                    "rearm_count": 31}
        self.paths = peer_steward._watch_paths(self.WATCH, self.root)
        self.paths.arm.write_text(json.dumps(self.arm), encoding="utf-8")
        self.rows = {"w1:p9": ("codex", "sid-target"), "w1:p1": ("codex", "sid-steward")}
        self.liveness = {}
        self.chain = {}
        self.holder = {"harness": "codex", "sid": "sid-steward"}
        self.present = False
        self.socket = None
        check = peer_steward._steward_check_module()
        import dispatch_seat_handover as handover
        for target, name, replacement in (
                (peer_steward, "_watch_root", lambda: self.root),
                (peer_steward.herdr_observation, "socket_path_for_endpoint",
                 lambda *a, **k: self.socket),
                (peer_steward.herdr_observation, "pane_census", lambda **k: self.census()),
                (peer_steward, "_watcher_present", lambda arm: self.present),
                (peer_steward, "_herdr_missing", lambda: False),
                (peer_steward, "_wake_watch_observer", lambda arm: True),
                (check, "_agents", lambda: []),
                (check, "_end_observation", lambda: None),
                (check, "_session_liveness",
                 lambda harness, sid, agents=None, observation=None:
                     self.liveness.get((harness, sid), "unknown")),
                (handover, "resolve_watch",
                 lambda watch_id, **k: {"holder": dict(self.holder)} if self.holder else None),
                (handover, "_chain_end",
                 lambda graph, harness, sid: self.chain.get((harness, sid), (sid, "self"))),
                (handover, "lineage_graph", lambda *a, **k: object())):
            patcher = patch_split(target, name, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        spawn = mock.patch.object(peer_steward, "_spawn_watch_observer")
        self.spawn = spawn.start()
        self.addCleanup(spawn.stop)

    def census(self):
        return peer_steward.herdr_observation.PaneCensus(
            "named", tuple({"pane_id": pane, "agent": harness, "agent_session": {"value": sid}}
                           for pane, (harness, sid) in self.rows.items()), 1.0, True)

    def run_pass(self):
        peer_steward._ensure_watch_observers(entries=[(self.WATCH, self.arm)])
        return (json.loads(self.paths.receipt.read_text()) if self.paths.receipt.exists() else None)

    def test_supervisor_pane_absent_ends_the_watch_and_never_rearms(self):
        del self.rows["w1:p1"]
        receipt = self.run_pass()
        self.assertEqual((receipt["state"], receipt["reason"]), ("steward-exited", "steward-exited"))
        self.spawn.assert_not_called()
        self.assertEqual(json.loads(self.paths.arm.read_text())["rearm_count"], 31)

    def test_supervisor_pane_now_holding_another_session_is_not_an_exit(self):
        self.rows["w1:p1"] = ("codex", "someone-else")
        self.assertIsNone(self.run_pass())
        self.spawn.assert_called_once()

    def test_target_pane_session_replaced(self):
        self.rows["w1:p9"] = ("claude", "sid-new")
        self.assertEqual(self.run_pass()["reason"], "session-replaced")
        self.spawn.assert_not_called()

    def test_target_with_a_confirmed_lineage_successor_is_replaced(self):
        self.chain[("codex", "sid-target")] = ("sid-target-2", "successor")
        self.assertEqual(self.run_pass()["reason"], "session-replaced")
        self.spawn.assert_not_called()

    def test_supervisor_session_ended_by_liveness(self):
        self.liveness[("codex", "sid-steward")] = "ended"
        self.assertEqual(self.run_pass()["reason"], "steward-exited")
        self.spawn.assert_not_called()

    def test_a_live_successor_holder_keeps_the_watch_even_when_the_original_ended(self):
        self.holder = {"harness": "codex", "sid": "sid-successor"}
        self.liveness[("codex", "sid-steward")] = "ended"
        self.liveness[("codex", "sid-successor")] = "live"
        self.assertIsNone(self.run_pass())
        self.spawn.assert_called_once()

    def test_an_unresolved_holder_is_not_judged(self):
        self.holder = None
        del self.rows["w1:p1"]
        self.assertIsNone(self.run_pass())
        self.spawn.assert_called_once()

    def test_control_unchanged_panes_and_unknown_liveness_spawn_as_before(self):
        self.assertIsNone(self.run_pass())
        self.spawn.assert_called_once()
        self.assertFalse(self.paths.receipt.exists())

    def test_an_incomplete_or_missing_census_is_not_evidence(self):
        with mock.patch.object(peer_steward.herdr_observation, "pane_census", return_value=None):
            self.assertIsNone(self.run_pass())
        self.spawn.assert_called_once()

    def test_a_live_socketless_observer_is_asked_to_settle_a_closed_watch(self):
        self.present = True
        del self.rows["w1:p1"]
        fd = os.open(self.paths.lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with patch_split(peer_steward, "_wake_watch_observer", return_value=True) as wake:
            self.assertIsNone(self.run_pass())
        wake.assert_called_once()
        observation = json.loads(self.paths.observation.read_text())
        self.assertEqual((observation["state"], observation["reason"]),
                         ("terminal_pending", "steward-exited"))
        self.spawn.assert_not_called()


class RetireReleasesClosedSessionTest(unittest.TestCase):
    """A retired session lets go of its roles and supervision watches, once the pane is closed."""

    IDENT = {"harness": "claude", "session_id": "sid-retired", "name": "old", "pane": "w1:p9",
             "server": "default"}

    def setUp(self):
        for name, value in (("_request_watch_terminal", 0), ("_retire_followup_successor_pane", None)):
            patcher = mock.patch.object(peer_steward, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def ready(self, **kwargs):
        return peer_steward._retire_followup_ready({"id": "d"}, dict(self.IDENT), "completed-retire",
                                                   None, **kwargs)

    def test_follow_up_releases_the_retired_sessions_roles_and_records_the_summary(self):
        summary = {"roles": 1, "marker": "removed", "watches": 2, "skipped": 0}
        with mock.patch.object(peer_steward.peer_message, "release_closed_session",
                               return_value=summary) as release:
            record = {}
            self.assertTrue(self.ready(summary=record))
        release.assert_called_once_with("claude", "sid-retired", reason="retired",
                                        settle_watch=peer_steward.settle_steward_exited_watch)
        self.assertEqual(json.loads(record["steward_release"]), summary)

    def test_a_transient_failure_keeps_the_follow_up_pending_for_retry(self):
        with mock.patch.object(peer_steward.peer_message, "release_closed_session",
                               side_effect=OSError("busy")):
            self.assertFalse(self.ready())

    def test_any_other_failure_never_blocks_the_follow_up(self):
        with mock.patch.object(peer_steward.peer_message, "release_closed_session",
                               side_effect=RuntimeError("steward-release-relation-unresolved")):
            record = {}
            self.assertTrue(self.ready(summary=record))
        self.assertEqual(json.loads(record["steward_release"]), {"error": "RuntimeError"})

    def test_an_identity_without_a_session_releases_nothing(self):
        with mock.patch.object(peer_steward.peer_message, "release_closed_session") as release:
            self.assertTrue(peer_steward._retire_followup_ready(
                {"id": "d"}, {**self.IDENT, "session_id": "-"}, "completed-retire", None))
        release.assert_not_called()

    def test_settle_steward_exited_watch_writes_the_receipt_once(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        arm = {"schema_version": 1, "watch_id": "exited-1", "target": "peer", "server": "default",
               "armed_ts": "then", "refs": [], "steward": {"harness": "claude", "session_id": "sid-retired"},
               "agent": {"harness": "codex", "session_id": "sid-t", "pane": "w1:p2"}}
        paths = peer_steward._watch_paths("exited-1", root)
        paths.arm.write_text(json.dumps(arm), encoding="utf-8")
        with patch_split(peer_steward, "_watch_root", return_value=root):
            self.assertTrue(peer_steward.settle_steward_exited_watch("exited-1"))
            self.assertTrue(peer_steward.settle_steward_exited_watch("exited-1"))
            self.assertFalse(peer_steward.settle_steward_exited_watch("no-such-watch"))
        self.assertEqual(json.loads(paths.receipt.read_text())["reason"], "steward-exited")


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
        self._wait_for_wait_calls(1)
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
        self._wait_for_wait_calls(1)
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
        def reap_idle_process():
            if idle.poll() is None:
                idle.kill()
            try:
                idle.wait(timeout=3)
            except subprocess.TimeoutExpired:
                idle.kill()
                idle.wait(timeout=3)
            finally:
                if idle.stdin is not None:
                    idle.stdin.close()
        self.addCleanup(reap_idle_process)
        never_locks = {
            "watch_id": "deadbeefdeadbeef", "target": "x",
            "watcher": {"pid": idle.pid, "pid_start": peer_steward.process_start_ticks(idle.pid)},
        }
        self.assertTrue(peer_steward._pid_identity_ok(idle.pid, never_locks["watcher"]["pid_start"]))
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
        with mock.patch.dict(os.environ, self._env("idle")):
            import dispatch_seat_handover as handover
            self.assertIsNotNone(handover.resolve_watch(fields["watch_id"]),
                                 repr({"roots": handover._watch_roots(handover._peer_message()),
                                       "arm": peer_steward._read_json(
                                           peer_steward._watch_paths(fields["watch_id"]).arm),
                                       "refs": handover.resolved_watch_references()}))
        # Exercise cmd_ack's ordinary recovery of a canonical path left torn by
        # an interrupted writer; the callback itself must revalidate the holder.
        torn_ack = self.watch_root / f"{fields['watch_id']}.ack.json"
        torn_ack.write_bytes(b'{"watch_id":')
        first = self._run("ack", fields["watch_id"], "--carrier", "claude-async-rewake")
        second = self._run("ack", fields["watch_id"], "--carrier", "userprompt-sweep")
        self.assertEqual((first.returncode, second.returncode), (0, 0),
                         first.stdout + first.stderr + second.stdout + second.stderr)
        self.assertIn("ack=created", first.stdout, first.stdout + first.stderr)
        self.assertIn("ack=already", second.stdout, second.stdout + second.stderr)
        ack = json.loads((self.watch_root / f"{fields['watch_id']}.ack.json").read_text())
        self.assertEqual(ack["carrier"], "claude-async-rewake")
        self.assertEqual(ack["session_id"], "steward-1")

    def test_ack_fsync_failure_does_not_settle_original_watch_ref_until_retry(self):
        import contextlib
        import io
        import dispatch_seat_handover as handover

        fields = self._arm(mode="idle")
        self._wait_for_receipt(fields["watch_id"])
        ack = self.watch_root / f"{fields['watch_id']}.ack.json"
        with mock.patch.dict(os.environ, self._env("idle")), \
             mock.patch.object(peer_steward, "_current_session_identity",
                               return_value=("steward-1", "claude")), \
             mock.patch.object(peer_steward.peer_message, "settle_watch_transfers",
                               return_value=False) as settle, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(handover.os, "fsync",
                                   side_effect=OSError("fixture ACK fsync failure")):
                self.assertEqual(peer_steward.cmd_ack(SimpleNamespace(
                    watch_id=fields["watch_id"], carrier="fixture-carrier")), 0)
            settle.assert_not_called()
            self.assertFalse(ack.exists())

            self.assertEqual(peer_steward.cmd_ack(SimpleNamespace(
                watch_id=fields["watch_id"], carrier="retry-carrier")), 0)
            settle.assert_called_once_with(fields["watch_id"], "claude", "steward-1")
            ack_data = json.loads(ack.read_text(encoding="utf-8"))
            self.assertEqual((ack_data["watch_id"], ack_data["harness"], ack_data["session_id"]),
                             (fields["watch_id"], "claude", "steward-1"))

    def test_a_restarted_watch_line_does_not_claim_the_hook_either(self):
        """`state=rearmed` — the branch B1-a was originally raised about.

        Review round 4 found the code correct here and nothing asserting it:
        flipping this branch back to `arms_hook=True` passed all 68 tests.
        """
        dead = self._arm(target="peer-dead", mode="held")
        # Kill the watcher only after its one native wait is running: the
        # rearmed watcher adopts that wait (the FIFO reader released below).
        # A watcher killed before launching it leaves no reader at all, and
        # under CI load that made the final release fail.
        self._wait_for_lock(dead["watch_id"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not [
                line for line in self._wait_calls() if " peer-dead" in f" {line} "]:
            time.sleep(0.02)
        self.assertEqual(
            len([line for line in self._wait_calls() if " peer-dead" in f" {line} "]), 1,
            self._wait_calls())
        os.kill(int(dead["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and peer_steward._pid_identity_ok(
            int(dead["pid"]), dead["pid_start"]
        ):
            time.sleep(0.02)
        proc = self._run("rearm", dead["watch_id"], env=self._env("held"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        head = self._fields(proc.stdout.splitlines()[0])
        self.assertEqual(head["state"], "rearmed", proc.stdout)
        directive = self._directive(proc.stdout)
        self.assertEqual(directive.get("parent_next"), "bounded-wait", proc.stdout)
        self.assertEqual(
            directive.get("parent_next_reason"), "steward-line-does-not-arm", proc.stdout
        )
        # The wait continues to join the original logical watch.
        self.assertIn(f"join {head['watch_id']}", directive.get("parent_next_command", ""))
        self.assertEqual(head["watch_id"], dead["watch_id"])
        self._release()

    def test_rearm_only_replaces_a_dead_unreceipted_watch(self):
        done = self._arm(mode="idle")
        self._wait_for_receipt(done["watch_id"])
        self.assertIn("state=already-done", self._run("rearm", done["watch_id"]).stdout)

        live = self._arm(target="peer-live", mode="held")
        self._wait_for_lock(live["watch_id"])
        self.assertIn("state=alive", self._run("rearm", live["watch_id"]).stdout)

        dead = self._arm(target="peer-dead", mode="held")
        self.assertNotEqual(live["watch_id"], dead["watch_id"],
                            f"live={live} dead={dead}")
        self._wait_for_lock(dead["watch_id"])
        # Wait for this watch's own native wait, not for the total count to grow:
        # that wait may already be logged before the count is read.
        def dead_waits():
            return [line for line in self._wait_calls() if " peer-dead" in f" {line} "]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not dead_waits():
            time.sleep(0.02)
        self.assertEqual(len(dead_waits()), 1, self._wait_calls())
        os.kill(int(dead["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and peer_steward._pid_identity_ok(
            int(dead["pid"]), dead["pid_start"]
        ):
            time.sleep(0.02)
        out = self._run("rearm", dead["watch_id"], env=self._env("held")).stdout
        fields = self._fields(out.splitlines()[0])
        self.assertIn(fields["state"], {"rearmed", "alive"}, out)
        self.assertEqual(fields["watch_id"], dead["watch_id"])
        new_arm = json.loads((self.watch_root / f"{fields['watch_id']}.json").read_text())
        self.assertEqual(new_arm["observer_generation"], 2)
        self.assertNotEqual(new_arm["watcher"]["pid"], int(dead["pid"]))
        self.assertTrue(peer_steward._pid_identity_ok(
            new_arm["watcher"]["pid"], new_arm["watcher"]["pid_start"]))
        self.assertTrue(peer_steward._lock_held(
            self.watch_root / f"{fields['watch_id']}.lock"))
        self.assertEqual(len(dead_waits()), 1,
                         "reattachment adopts the one already launched native wait")

        self._release()


    def test_rearm_waits_for_a_concurrent_observer_to_publish_its_generation(self):
        dead = self._arm(target="peer-dead", mode="held")
        self._wait_for_wait_calls(1)
        paths = peer_steward._watch_paths(dead["watch_id"], self.watch_root)
        arm = peer_steward._read_json(paths.arm)
        os.kill(int(dead["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 10
        while (peer_steward._pid_identity_ok(int(dead["pid"]), dead["pid_start"])
               and time.monotonic() < deadline):
            time.sleep(0.02)
        self.assertFalse(peer_steward._watcher_present(arm))

        ctx = multiprocessing.get_context("fork")
        spawned, publish = ctx.Event(), ctx.Event()
        real_popen = peer_steward.subprocess.Popen

        def restart():
            def delayed_publication(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                spawned.set()
                if not publish.wait(15):
                    raise RuntimeError("fixture did not release observer publication")
                return process

            with mock.patch.dict(os.environ, self._env("held")), \
                 mock.patch.object(peer_steward.subprocess, "Popen",
                                   side_effect=delayed_publication):
                if not peer_steward._spawn_watch_observer(dead["watch_id"], arm):
                    raise RuntimeError("fixture observer restart failed")

        contender = ctx.Process(target=restart)
        contender.start()
        finished = threading.Event()
        release = threading.Thread(target=lambda: (finished.wait(1), publish.set()))
        try:
            self.assertTrue(spawned.wait(10), "first observer was not spawned")
            self.assertEqual(peer_steward._read_json(paths.arm)["observer_generation"], 1)
            release.start()
            try:
                with mock.patch.dict(os.environ, self._env("held")), \
                     mock.patch("builtins.print") as output:
                    result = peer_steward.cmd_rearm(SimpleNamespace(watch_id=dead["watch_id"]))
                observed = peer_steward._read_json(paths.arm)
            finally:
                finished.set()
            self.assertEqual(result, 0)
            self.assertEqual(observed["observer_generation"], 2)
            self.assertNotEqual(observed["watcher"]["pid"], int(dead["pid"]))
            self.assertTrue(peer_steward._watcher_present(observed))
            self.assertIn("state=rearmed", output.call_args_list[0].args[0])
            self.assertEqual(len(self._wait_calls()), 1,
                             "both restart callers retain the one native wait")
        finally:
            publish.set()
            finished.set()
            if release.ident is not None:
                release.join(3)
            contender.join(10)
            if contender.is_alive():
                contender.kill()
                contender.join(3)
        self.assertEqual(contender.exitcode, 0)
        self._release()


class WatchMessageTransportSettlementTest(_WatchMixin, unittest.TestCase):
    _STEWARD_PANE = "w1:p8"

    def _watch(self, target):
        import session_tidy

        env = self._env("idle")
        # The shared obligation runner these commands start can reach a duty before
        # its ACK; a census without the recipient would cancel it as
        # recipient-session-absent, so list the recipient's session.
        # The target and recipient are different sessions. Reporting the
        # recipient on w1:p9 contradicts get/wait's sid-fake target and makes
        # the runner retire this watch as session-replaced while it is ready.
        env.update({"HERDR_PANE_ID": self._STEWARD_PANE,
                    "AGENT_DISPATCH_CALLER_HARNESS": "claude",
                    "FAKE_HERDR_PANES": json.dumps([
                        {"pane_id": "w1:p9", "agent_session": {"value": "sid-fake"}},
                        {"pane_id": self._STEWARD_PANE,
                         "agent_session": {"value": "steward-1"}},
                    ])})
        proc = self._run("watch", target, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        fields = self._fields(proc.stdout.splitlines()[0])
        receipt = self._wait_for_receipt(fields["watch_id"])
        self.assertEqual(receipt["state"], "idle", receipt)
        seat = session_tidy.Seat("pane", "watch-transport-" + fields["watch_id"],
                                 self._STEWARD_PANE, "claude")
        arm_path = self.watch_root / f"{fields['watch_id']}.json"
        arm = json.loads(arm_path.read_text(encoding="utf-8"))
        arm["steward_seat"] = seat.as_dict()
        peer_steward._write_json_atomic(arm_path, arm)
        return fields, env, seat

    _TRANSPORT_SENDER = {"harness": "claude", "session_id": "transport-sender", "name": "sender"}
    _TRANSPORT_RECIPIENT = {"harness": "claude", "session_id": "steward-1", "name": "steward"}

    def _transfer(self, watch_id, *, old_transfer):
        _text, ref = peer_steward.peer_message.prepare_peer_message(
            "[peer-steward] watch receipt transport", self._TRANSPORT_SENDER,
            self._TRANSPORT_RECIPIENT, defer=True, refs=[f"peer-watch:{watch_id}"])
        if old_transfer:
            path = peer_steward.peer_message._transfer_path(ref)
            transfer = json.loads(path.read_text(encoding="utf-8"))
            transfer.pop("refs", None)
            path.write_text(json.dumps(transfer), encoding="utf-8")
        return ref

    def _message_duty(self, ref):
        row = peer_steward.peer_message._read_pending(ref)
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.create("message-" + ref, "message",
            {"server": "default", "pane": self._STEWARD_PANE,
             **self._TRANSPORT_RECIPIENT},
            {"ref": ref, "body_digest": row["body_sha256"], "from": self._TRANSPORT_SENDER,
             "to": row["to"], "refs": row["refs"]})
        return row, store, duty

    def _transfer_duty(self, watch_id, *, old_transfer):
        ref = self._transfer(watch_id, old_transfer=old_transfer)
        row, store, duty = self._message_duty(ref)
        return ref, row, store, duty

    def _resume_until(self, store, duty_id, settled):
        # The detached shared runner may checkpoint this duty from a read taken
        # before the ACK; a stale revision then skips our CAS write. Resume from
        # the current row, as the runner's next pass would (2026-10-21, after #719).
        deadline = time.monotonic() + 10
        while True:
            peer_steward._resume_message_obligation(store.get(duty_id), store)
            latest = store.get(duty_id)
            if settled(latest) or time.monotonic() >= deadline:
                return latest
            time.sleep(0.05)

    def test_cmd_ack_settles_old_and_new_watch_transport_without_body_receipt(self):
        for target, old_transfer in (("watch-old-transfer", True),
                                     ("watch-new-transfer", False)):
            with self.subTest(old_transfer=old_transfer):
                fields, env, _seat = self._watch(target)
                ref, _row, store, duty = self._transfer_duty(
                    fields["watch_id"], old_transfer=old_transfer)
                first = self._run("ack", fields["watch_id"], "--carrier", "fixture-test",
                                  env=env)
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                self.assertIn("ack=created", first.stdout)
                second = self._run("ack", fields["watch_id"], "--carrier", "fixture-replay",
                                   env=env)
                self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                self.assertIn("ack=already", second.stdout)

                row = peer_steward.peer_message._read_pending(ref)
                self.assertEqual((row["state"], row["receipt"]),
                                 ("received", "watch-receipt-displayed"))
                self.assertNotIn("exact_receiver_receipt", row)
                self.assertEqual(row["watch_receipt_binding"]["recipient"],
                                 {"harness": "claude", "session_id": "steward-1"})

                settled = self._resume_until(
                    store, duty["id"], lambda latest: latest["state"] == "complete")
                self.assertEqual((settled["state"], settled["result"], settled["delivery"]),
                                 ("complete", None, "not-required"))
                self.assertNotIn("receiver_receipt", settled["observation"])
                self.assertEqual(settled["observation"]["watch_transport_receipt"]["receipt"],
                                 "watch-receipt-displayed")

                with mock.patch.object(peer_steward, "_resume_message_obligation",
                                       wraps=peer_steward._resume_message_obligation) as resume:
                    peer_steward._reconcile_obligations(store)
                resume.assert_not_called()

    def test_watch_transport_rejects_ack_from_a_stale_handover_generation(self):
        import session_tidy
        import dispatch_seat_handover as handover

        fields, env, seat = self._watch("watch-stale-transport")
        # The message duty is created only after the handover below. While it
        # exists, the detached shared runner may resume it at any moment; one
        # pass between the ACK and the handover settles it with the ACK's then
        # still current binding (complete, which is correct), and this test
        # would never see the stale-generation case it is about.
        ref = self._transfer(fields["watch_id"], old_transfer=True)
        ack = self._run("ack", fields["watch_id"], "--carrier", "fixture-test", env=env)
        self.assertEqual(ack.returncode, 0, ack.stdout + ack.stderr)
        pending = peer_steward.peer_message._read_pending(ref)
        old_binding = pending["watch_receipt_binding"]

        with mock.patch.dict(os.environ, env):
            current = handover.resolve_watch(fields["watch_id"], include_acked=True)
            self.assertEqual(current["seat_key"], seat.key)
            successor = dict(current,
                holder={"harness": "claude", "sid": "steward-2"},
                lineage=[{"from": "steward-1", "to": "steward-2",
                          "snapshot_id": "fixture-stale-ack"}])
            with session_tidy.seat_lock(seat.key):
                session_tidy._append_ledger_entry(seat, {
                    "ts": session_tidy.now_epoch(), "harness": "claude", "sid": "steward-2",
                    "event": "handover", "from": "steward-1", "seat_key": seat.key,
                    "bindings": [],
                    "steward": {"schema": 1, "roles": [], "watches": [successor]},
                })
            current_binding = peer_steward.peer_message.pending_delivery_binding(
                pending, include_acked=True)
            self.assertNotEqual(current_binding, old_binding)

        _row, store, duty = self._message_duty(ref)
        latest = self._resume_until(
            store, duty["id"],
            lambda row: row["observation"].get("reason") == "received-binding-unverified")
        # CI still saw 'complete' here after the duty moved behind the handover
        # (run 41551665759). The record's observation names the settling path.
        self.assertEqual(latest["state"], "unknown", latest)
        self.assertEqual(latest["observation"]["reason"], "received-binding-unverified")
        self.assertEqual(latest["delivery"], "pending")


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

    def test_restart_adopts_the_same_wait_child_without_a_second_call(self):
        armed = self._fields(self._run("watch", "peer-a", env=self._env("held")).stdout)
        watch_id = armed["watch_id"]
        native_wait = self._wait_for_native_wait_started(watch_id)
        self.assertEqual(native_wait["state"], "started")
        self.assertTrue(peer_steward._pid_identity_ok(
            native_wait["pid"], native_wait["pid_start"]))

        old_watcher = int(armed["pid"])
        os.kill(old_watcher, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while (time.monotonic() < deadline
               and peer_steward._pid_identity_ok(old_watcher, armed["pid_start"])):
            time.sleep(0.02)
        env = self._env("held")
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        self.assertEqual(self._run("__ensure-obligations", env=env).returncode, 0)
        arm_path = self.watch_root / f"{watch_id}.json"
        deadline = time.monotonic() + 10
        while True:
            updated = json.loads(arm_path.read_text())
            watcher = updated.get("watcher") or {}
            if (watcher.get("pid") != old_watcher and watcher.get("pid")
                    and peer_steward._pid_identity_ok(watcher["pid"], watcher.get("pid_start", ""))):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(.02)
        self.assertNotEqual(updated["watcher"]["pid"], old_watcher)
        resumed_wait = self._wait_for_native_wait_started(watch_id)
        self.assertEqual((resumed_wait["pid"], resumed_wait["pid_start"]),
                         (native_wait["pid"], native_wait["pid_start"]))
        waits = [line for line in self.calllog.read_text().splitlines()
                 if line.startswith("agent wait ")]
        self.assertEqual(len(waits), 1, self.calllog.read_text())

        self._release()
        receipt = self._wait_for_receipt(watch_id)
        self.assertEqual((receipt["state"], receipt["agent"]["session_id"]),
                         ("idle", "sid-fake"))

    def test_exact_terminal_request_stops_only_its_watch_wait_and_sweep_keeps_receipt(self):
        armed = self._fields(self._run("watch", "peer-a", env=self._env("held")).stdout)
        watch_id = armed["watch_id"]
        wait_record = self._wait_for_native_wait_started(watch_id)
        request = {"server": "default", "pane": "w1:p9", "harness": "claude",
                   "session_id": "sid-fake"}
        self.assertEqual(peer_steward._request_watch_terminal(
            request, "completed-retire"), 1)
        receipt = self._wait_for_receipt(watch_id)
        self.assertEqual((receipt["state"], receipt["reason"]),
                         ("pane-exited", "completed-retire"))
        self.assertFalse(peer_steward._pid_identity_ok(
            wait_record["pid"], wait_record["pid_start"]))
        before = self.calllog.read_text()
        self.assertEqual(self._run("__ensure-obligations", env=self._env("held")).returncode, 0)
        self.assertEqual(self.calllog.read_text(), before)


class NativeResultRunnerWakeTest(_WatchMixin, unittest.TestCase):
    def test_native_result_over_a_pending_cached_projection_wakes_the_runner(self):
        """A projection cached before the native result cannot reflect it.

        The watcher requests a refresh only when the cache reads ready or
        unavailable. For any other cached result it must still wake the runner,
        or the runner reads the new result only at its next backoff pass
        (CI 41449820688: the request came 30 s after the native result).
        """
        from dispatch_registry_cache import registry_view

        store = peer_steward.peer_obligations.ObligationStore()
        # Hold the runner lock and the wake input ourselves: no runner starts,
        # and every wake the watcher sends lands here.
        lock_fd = os.open(store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME,
                          os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lock_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        wake_fd = peer_steward.shared_readiness.open_runner_wake(store.root)
        self.addCleanup(os.close, wake_fd)

        env = self._env("held")
        fields = self._fields(self._run("watch", "peer-a", env=env).stdout)
        self._wait_for_native_wait_started(fields["watch_id"])
        arm = json.loads((self.watch_root / f"{fields['watch_id']}.json").read_text())
        start = str(peer_steward.process_start_ticks(os.getpid()))
        with mock.patch.dict(os.environ, env):
            identity = peer_steward._watch_readiness_identity(arm, "idle")
            jobs = peer_steward._watch_selected_jobs_path()
            generation = peer_steward.shared_readiness.registry_generation(
                jobs, registry_view(jobs), selected=True)
        peer_steward.shared_readiness.publish_runner_incarnation(
            store.root, os.getpid(), start, "fixture-runner")
        peer_steward.shared_readiness.publish_target_projection(
            store.root, identity, {"state": "pending", "reason": "fixture-cached-pending"},
            producer_pid=os.getpid(), producer_start=start, incarnation="fixture-runner",
            generation=generation, lineage={key: arm["pane_identity"][key]
                                            for key in ("shell_pid", "shell_start")})
        with contextlib.suppress(BlockingIOError):
            while os.read(wake_fd, 4096):
                pass

        self._release()
        observation_path = self.watch_root / f"{fields['watch_id']}.observation.json"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (peer_steward._read_json(
                observation_path) or {}).get("reason") != "fixture-cached-pending":
            time.sleep(0.02)
        self.assertEqual(peer_steward._read_json(observation_path).get("reason"),
                         "fixture-cached-pending")
        ready, _, _ = select.select([wake_fd], [], [], 10)
        self.assertEqual(ready, [wake_fd], "the runner was not woken for the native result")

        # At most once per observer: later wake-ups re-read the same cached
        # projection without waking the runner again.
        with contextlib.suppress(BlockingIOError):
            while os.read(wake_fd, 4096):
                pass
        watcher = arm["watcher"]
        for _attempt in range(3):
            before = observation_path.stat().st_mtime_ns
            os.kill(watcher["pid"], signal.SIGUSR1)
            deadline = time.monotonic() + 10
            while (observation_path.stat().st_mtime_ns == before
                   and time.monotonic() < deadline):
                time.sleep(0.02)
            self.assertNotEqual(observation_path.stat().st_mtime_ns, before,
                                "the observer did not handle its wake-up")
        self.assertTrue(peer_steward._pid_identity_ok(watcher["pid"], watcher["pid_start"]))
        ready, _, _ = select.select([wake_fd], [], [], 0.5)
        self.assertEqual(ready, [], "a later wake-up woke the runner again")
def _agent_json(harness, sid, name, status="idle"):
    return {"id": "cli:agent:get", "result": {"agent": {
        "agent": harness, "agent_status": status, "name": name, "pane_id": "w1:pX",
        "agent_session": ({"agent": harness, "kind": "id", "value": sid} if sid else None)}}}


# --- clear (session-tidy auto-clear) ---------------------------------------------------

RULE = "─" * 40
CLAUDE_EMPTY = "\x1b[0m\x1b[38;2;80;80;80m❯ \x1b[0m\x1b[38;2;255;255;255m[earlier message]\x1b[0m\n\n" \
    + RULE + "\n❯ \r\n" + RULE + "\n  \U0001f4c1 proj │ main\n  bypass permissions on\n"
CLAUDE_SUGGESTION = "❯ earlier\n\n" + RULE + "\n❯ \x1b[2mrun the tests again\x1b[0m\n" + RULE + "\n  footer\n"
CLAUDE_DRAFT = "❯ earlier\n\n" + RULE + "\n❯ half typed text\n" + RULE + "\n  footer\n"
CLAUDE_DRAFT_SECOND_LINE = RULE + "\n❯ \x1b[2m\x1b[0m\n  second line of a draft\n" + RULE + "\n  footer\n"
CLAUDE_FORM = "Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel · Tab to amend\n"
CODEX_EMPTY = "recap line\n\n\n\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m\n\n  proj · master · Context 85% used\n"
CODEX_DRAFT = "recap line\n\x1b[1m›\x1b[0m fix the flaky test\n\n  proj · master\n"
CODEX_POPUP = "recap line\n\x1b[1m›\x1b[0m \n  /clear   start a new chat\n  /compact summarize\n"
OPENCODE_EMPTY = "     ▣  Build · Model · 46m\n\n  ┃\n  ┃\n  ┃\n  ┃  Build auto · Model OpenCode Go\n  ╹▀▀▀▀▀\n   /path/to/project\n"
OPENCODE_WIDE_EMPTY = "     \u25a3  Build \u00b7 Model\n\n  \u2503" + " " * 60 + "\n  \u2503" + " " * 60 + "\n  \u2503" + " " * 120 + "/path/to/project:\n  \u2503  Build \u00b7 Model OpenCode Go" + " " * 80 + "main\n  \u2579\u2580\u2580\u2580\u2580\u2580\n"
OPENCODE_DRAFT = "     ▣  Build · Model\n\n  ┃  hello there\n  ┃\n  ┃  Build auto · Model OpenCode Go\n  ╹▀▀▀▀▀\n"
# The token-based form classifier owns the shape; this is the OpenCode question/
# permission surface in the same keymap footer OpenCode prints.
OPENCODE_FORM = "Permission required\n ❯ 1. Allow once\n   2. Always allow\n Esc to cancel · Enter to select\n"
CODEX_FORM = "Permission required\n ❯ 1. Allow once\n   2. Always allow\n Esc to cancel · Enter to select\n"
NO_BOX = "just some output\nwith no prompt box at all\n"
OLD_T = "01000014-0000-7000-8000-00000000001e"


class _ClearWorld:
    """A fake herdr: one pane whose agent record and visible screen change as the test says."""

    def __init__(self, harness="claude", sid="sid-A", status="idle", pane="w1:pX", screens=None,
                 new_sid="sid-B", home_after=None):
        self.harness, self.sid, self.status, self.pane = harness, sid, status, pane
        self.screens = list(screens or [CLAUDE_EMPTY])
        self.new_sid, self.home_after = new_sid, home_after
        self.sent = False
        self.calls = []
        self.prompt_rc = 0
        self.reads = 0
        self.on_send = None

    def agent(self):
        sid = self.new_sid if (self.sent and self.new_sid) else self.sid
        return {"agent": self.harness, "agent_status": self.status, "name": "w", "pane_id": self.pane,
                "agent_session": ({"agent": self.harness, "kind": "id", "value": sid}
                                  if self.harness != "opencode" else None)}

    def run(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:3] == ["herdr", "agent", "get"] or argv[:3] == ["herdr", "agent", "wait"]:
            return _herdr_json({"id": "x", "result": {"agent": self.agent(), "type": "agent_info"}})
        if argv[:3] == ["herdr", "agent", "read"]:
            if self.sent and self.home_after is not None:
                screen = self.home_after
            else:
                screen = self.screens[min(self.reads, len(self.screens) - 1)]
                self.reads += 1
            return subprocess.CompletedProcess(argv, 0 if screen is not None else 1,
                                               stdout=screen or "", stderr="")
        if argv[:3] == ["herdr", "pane", "process-info"]:
            return _herdr_json({"id": "x", "result": {"process_info": {"foreground_processes": [
                {"name": self.harness, "pid": 4242}]}, "type": "pane_process_info"}})
        if argv[:3] == ["herdr", "agent", "prompt"]:
            if self.prompt_rc == 0:
                self.sent = True
                if self.on_send:
                    self.on_send()
            return subprocess.CompletedProcess(argv, self.prompt_rc, stdout="{}", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    def typed(self):
        return [c for c in self.calls if c[:3] in (["herdr", "agent", "prompt"], ["herdr", "agent", "send-keys"])]


class _ContinueWorld(_ClearWorld):
    """The window after a confirmed clear: herdr's session stays what it says, the prompt answers as told."""

    def __init__(self, harness="claude", herdr_sid="sid-B", screens=None, reply=(0, ""), **kw):
        super().__init__(harness=harness, sid=herdr_sid, new_sid=None, screens=screens or [CLAUDE_EMPTY], **kw)
        self.reply = reply
        self.on_prompt = self.on_wait = None

    def run(self, argv, **kw):
        if argv[:3] == ["herdr", "agent", "prompt"]:
            self.calls.append(list(argv))
            if self.on_prompt:
                self.on_prompt()
            rc, err = self.reply
            return subprocess.CompletedProcess(argv, rc, stdout="{}" if rc == 0 else "", stderr=err)
        if argv[:3] == ["herdr", "agent", "wait"] and self.on_wait:
            self.on_wait()
        return super().run(argv, **kw)


TIMEOUT_REPLY = (1, json.dumps({"error": {"code": "timeout"}}))


class _RetireWorld:
    def __init__(self, harness="codex", status="idle", screen=None, exits=True, sid="old-sid"):
        self.harness, self.status, self.sid = harness, status, sid
        self.screen = screen if screen is not None else {
            "codex": CODEX_EMPTY, "claude": CLAUDE_EMPTY, "opencode": OPENCODE_EMPTY}[harness]
        self.calls, self.gets, self.process_reads = [], 0, 0
        self.sent, self.exits, self.changed = False, exits, False
        self.bad_process = None
        self.post_unknown, self.close_error, self.send_error = False, False, False
        self.final_changed = False

    def agent(self):
        self.gets += 1
        return {"agent": self.harness, "agent_status": self.status, "name": "old",
                "pane_id": "w1:pOld", "agent_session": {"value": "other" if self.changed and self.gets > 1 else self.sid}}

    def info(self):
        self.process_reads += 1
        shell = self.sent and self.exits
        result = {"pane_id": "w1:pOld", "shell_pid": 101,
                  "foreground_process_group_id": 101 if shell else 4242,
                  "foreground_processes": [{"pid": 101 if shell else 4242,
                    "argv": ["/usr/bin/zsh"] if shell else [self.harness],
                    "name": "zsh" if shell else self.harness}]}
        if self.bad_process:
            result.update(self.bad_process)
        if self.final_changed and self.process_reads >= 4:
            result["shell_pid"] = 999
        return result

    def run(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:3] == ["herdr", "agent", "get"]:
            return _herdr_json({"result": {"agent": self.agent()}})
        if argv[:3] == ["herdr", "agent", "wait"]:
            return _herdr_json({"result": {"agent": self.agent()}})
        if argv[:3] == ["herdr", "agent", "read"]:
            return subprocess.CompletedProcess(argv, 0 if self.screen else 1, stdout=self.screen or "", stderr="")
        if argv[:3] == ["herdr", "pane", "process-info"]:
            return (_herdr_json({"error": {"code": "unavailable"}}) if self.sent and self.post_unknown
                    else _herdr_json({"result": {"process_info": self.info()}}))
        if argv[:3] == ["herdr", "pane", "send-text"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:3] == ["herdr", "pane", "send-keys"]:
            self.sent = True
            return (subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"error": {"code": "denied"}}), stderr="")
                    if self.send_error else subprocess.CompletedProcess(argv, 0, stdout="", stderr=""))
        if argv[:3] == ["herdr", "pane", "close"]:
            return _herdr_json({"error": {"code": "denied"}} if self.close_error else {"result": {"type": "ok"}})
        raise AssertionError(argv)

    def actions(self):
        return [a for a in self.calls if a[:3] in (["herdr", "pane", "send-text"], ["herdr", "pane", "send-keys"], ["herdr", "pane", "close"])]


def _legacy_notice(harness, sid, *, key, subject, text, jobs, route_id="route-free",
                   required_action="read-notice"):
    """Historical private notice tag and duty, as stored before the zero-attempt writer; no registry row."""
    import hashlib
    import dispatch_pending_delivery as pending
    import session_notice
    receipt = session_notice.validate({"schema_version": 1, "kind": "notice", "key": key,
        "subject": subject, "text": " ".join(text.split())[:600], "required_action": required_action})
    from harness_capabilities import parent_completion
    delivery_id = "delivery-" + hashlib.sha256(f"notice\0{key}".encode()).hexdigest()
    tag = "notice-" + delivery_id.removeprefix("delivery-")[:32]
    root = Path(jobs).parent
    record = pending.read(root, sid, delivery_id)
    if record is None:
        record = pending.create(root, recipient_kind=parent_completion(harness)["carrier"],
            recipient_key=sid, delivery_id=delivery_id, session_generation="",
            session_generation_supported="0", attempt_ids=[tag], parent_attempt_id=tag,
            route_id=route_id, route_node="notice", receipt=receipt,
            receipt_digest=session_notice.receipt_digest(receipt), row_revisions={tag: "notice"}, lineage=[])
    import peer_obligations
    # Historical retention failure leaves the original record for discovery.
    peer_obligations.retain_session_notice(Path(jobs), harness, sid, record)
    return record


_REAL_PREPARE = peer_steward.peer_message.prepare_peer_message


class _PromptProcess:
    """Just enough of `subprocess.Popen` for `_herdr_prompt`'s split error contract."""

    def __init__(self, *, rc=0, payload=None, communicate_error=None, timeout=False):
        self.returncode = rc
        self.args = []
        self.stdout = json.dumps(payload if payload is not None else {})
        self.stderr = ""
        self._communicate_error = communicate_error
        self._timeout = timeout
        self.killed = False

    def communicate(self, _input=None, timeout=None):
        if self._timeout:
            raise subprocess.TimeoutExpired("herdr", timeout)
        if self._communicate_error is not None:
            raise self._communicate_error
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


_REAL_HERDR_PROMPT = peer_steward._herdr_prompt


if __name__ == "__main__":
    unittest.main()
