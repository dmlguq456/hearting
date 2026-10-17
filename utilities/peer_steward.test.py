#!/usr/bin/env python3
"""Unit tests for utilities/peer-steward.py (SD-122 (9) steward wait/start)."""
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_HERE = Path(__file__).resolve().parent
# Real capture fixtures: codex-cli 0.162.1 via tmux capture-pane, 80x40 and
# 40x30, alt-screen and inline launches; README records each file's provenance.
_CODEX_REVIEW_FIXTURES = _HERE / "fixtures" / "codex-0.162.1-hook-review"


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
import fixture_processes  # noqa: E402


class _TmpRootMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_root = Path(self._tmp.name)
        self.jobs_path = self.tmp_root / "jobs.log"
        self.jobs_path.touch()
        self._old_environ = dict(os.environ)
        for key in tuple(os.environ):
            if key.startswith(("AGENT_DISPATCH_", "AGENT_SESSION_", "AGENT_RUNTIME_", "AGENT_THREAD_",
                               "AGENT_HERDR_", "HERDR_", "OPENCODE_", "CLAUDE_", "CODEX_")):
                os.environ.pop(key, None)
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs_path)
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.tmp_root)
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
        os.environ.pop("XDG_STATE_HOME", None)
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
                 mock.patch.object(peer_steward, "_proc_start_ticks", return_value="birth"), \
                 mock.patch.object(obligations, "execution_attempts_for_processes", return_value=()), \
                 mock.patch.object(obligations, "bound_work_for_pane", return_value=((), "observed")), \
                 mock.patch.object(obligations, "pane_readiness", side_effect=lambda **kw: kw):
                readiness = peer_steward._pane_readiness(
                    "w1:pX", "idle", expected_harness="codex", expected_sid=expected,
                    purpose="input", target_observation=observed, pane_info=info)
            self.assertEqual(dict(readiness["provenance"])["observed_state"], "idle")
            self.assertEqual((readiness["identity_verified"], readiness["session_id"]),
                             (verified, session))

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
             mock.patch.object(peer_steward, "_retire_target",
                               return_value=("idle", {"pane": "-"}, 0, None)):
            for _ in range(3):
                peer_steward._resume_message_obligation(store.get(duty["id"]), store)
        self.assertEqual(len(minted), 1)
        observation = store.get(duty["id"])["observation"]
        self.assertEqual((observation["ref"], observation["reason"]),
                         (minted[0], "target-identity-changed"))

    def test_watch_completion_consumes_real_bound_readiness_but_working_remains_native(self):
        from dispatch_contract import ObservedAttemptLiveness, claim_attempt_row, ATTEMPT_SCHEMA_VERSION
        class WatchExited(Exception):
            pass
        identity = {"harness": "codex", "session_id": "sid-peer", "pane": "w1:p9"}
        for process_state, native in (("live", "idle"), ("unverifiable", "done"), ("live", "working")):
            with self.subTest(process_state=process_state, native=native):
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
                payload = {"result": {"agent": {"agent": "codex", "agent_status": native,
                           "agent_session": {"value": "sid-peer"}, "pane_id": "w1:p9", "name": "peer"}}}
                checkpoints = []
                def release(_delay):
                    checkpoint = peer_steward._read_json(paths.observation)
                    checkpoints.append(checkpoint["reason"])
                    self.assertFalse(paths.receipt.exists())
                    self.jobs_path.write_text("")
                try:
                    with mock.patch.object(peer_steward, "_run_herdr_get", return_value=payload), \
                         mock.patch.object(peer_steward, "_run_herdr_wait", return_value=payload), \
                         mock.patch.object(peer_steward, "_retire_target", return_value=(native, identity, 0, None)), \
                         mock.patch.object(peer_steward, "_retire_pane_info", return_value={"shell_pid": 123}), \
                         mock.patch.object(peer_steward, "_proc_start_ticks", return_value="birth"), \
                         mock.patch("dispatch_contract.observed_attempt_liveness", return_value=ObservedAttemptLiveness(
                             "alive" if process_state == "live" else "unverifiable", "fixture", process_state, "fixture")), \
                         mock.patch.object(peer_steward.time, "sleep", side_effect=release), \
                         mock.patch.object(peer_steward.os, "_exit", side_effect=WatchExited), \
                         mock.patch.object(peer_steward, "_record"), \
                         mock.patch.object(peer_steward.peer_obligations, "pane_readiness",
                                           wraps=peer_steward.peer_obligations.pane_readiness) as policy:
                        with self.assertRaises(WatchExited):
                            peer_steward.cmd_watch_run(args)
                    self.assertEqual(peer_steward._read_json(paths.receipt)["state"], native)
                    if native == "working":
                        self.assertEqual(checkpoints, [])
                        policy.assert_not_called()
                    else:
                        suffix = "pending" if process_state == "live" else "unknown"
                        self.assertEqual(checkpoints, ["bound-registered-work-" + suffix])
                        self.assertEqual(policy.call_count, 2)
                finally:
                    os.close(fd)


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
        started with `--cwd <hearting>` came up in `SR_CorrNet` (measured 2026-09-10)."""
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
            (" Uihyeop@moving4  /home/nas/user/Uihyeop/NN_Zoo/BC_ResNet  ↱ main ± ", True),
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
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=_herdr_json(_agent_json("codex", "herdr-claim", "peer-c"))), \
             mock.patch.object(peer_steward, "_process_session", return_value=None) as resolve:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertEqual(rc, 0)
        resolve.assert_called_once_with("w1:pM", "codex")
        self.assertIn("started=true", out.getvalue())
        self.assertIn("session_id=-", out.getvalue())
        self.assertNotIn("session_bind=", out.getvalue())

    def test_the_receipt_uses_only_the_shared_resolver_identity(self):
        import io
        import contextlib
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=_herdr_json(_agent_json("codex", "herdr-claim", "peer-c"))), \
             mock.patch.object(peer_steward, "_process_session", return_value="proven-thread") as resolve:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertEqual(rc, 0)
        resolve.assert_called_once_with("w1:pM", "codex")
        self.assertIn("session_id=proven-thread", out.getvalue())

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

    def test_codex_start_skips_its_startup_update_dialog(self):
        for mode in ("bypass", "inherit"):
            with self.subTest(mode=mode), \
                    mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
                    mock.patch.object(peer_steward.subprocess, "run",
                                      return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
                self._start(kind="codex", permission_mode=mode)
                cmd = _agent_start_cmd(run_mock)
                self.assertEqual(cmd[cmd.index("--") + 1:][:2], ["-c", "check_for_update_on_startup=false"])
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
            self.assertEqual(self._start(name="peer-f", kind="claude"), 0)
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
                    self.assertEqual(self._start(name="peer-g", kind="codex"), 0)
                self.assertIn(expect, print_mock.call_args[0][0])
                self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})

    def test_herdr_missing_exit_4(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value=None):
            rc = self._start()
        self.assertEqual(rc, 4)

    def test_started_false_when_herdr_start_fails(self):
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="boom")):
            rc = self._start()
        self.assertEqual(rc, 0)
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

mode = os.environ.get("FAKE_HERDR_MODE", "idle")
mode_file = os.environ.get("FAKE_HERDR_MODE_FILE")
if mode_file and os.path.exists(mode_file):
    mode = open(mode_file, encoding="utf-8").read().strip() or mode
target = argv[2] if len(argv) > 2 else "-"
info = {"id": "cli:agent:wait", "result": {"agent": {
    "agent": "claude", "agent_session": {"value": "sid-fake"},
    "agent_status": "idle", "name": target, "pane_id": "w1:p9",
    "window_id": os.environ.get("FAKE_HERDR_WINDOW_ID", "window-before-move")}, "type": "agent_info"}}
identity_path = os.environ.get("FAKE_HERDR_IDENTITY_FILE")
if identity_path and os.path.exists(identity_path):
    with open(identity_path, encoding="utf-8") as fh:
        replacement = json.load(fh)
    info["result"]["agent"]["agent_session"]["value"] = replacement.get("session_id", "sid-fake")
    info["result"]["agent"]["pane_id"] = replacement.get("pane", "w1:p9")
    if replacement.get("once"):
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
    print(json.dumps({"result": {"process_info": {
        "pane_id": argv[-1], "shell_pid": os.getppid(),
        "foreground_process_group_id": os.getppid(),
        "foreground_processes": [{"pid": os.getppid(), "argv": ["zsh"]}]}}}))
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
    print(json.dumps(info)); sys.exit(0)
print(json.dumps(info)); sys.exit(0)
'''








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
             mock.patch.object(peer_steward, "_managed_ingress_dir", return_value=None), \
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
        thread = "01a0fa57-7f0e-7eb0-bffe-1d56df94e90c"
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
        # Registered after the tmp-dir cleanup so it runs first (LIFO): every watcher and
        # fake-herdr child is dead before the directory it writes into is removed.
        self.addCleanup(fixture_processes.reap, str(self.tmp_root),
                        fifo=str(self.tmp_root / "release.fifo"))
        self.bin = self.tmp_root / "fakebin"
        self.bin.mkdir()
        fake = self.bin / "herdr"
        fake.write_text(_FAKE_HERDR)
        fake.chmod(0o755)
        self.calllog = self.tmp_root / "calls.log"
        self.fifo = self.tmp_root / "release.fifo"
        os.mkfifo(self.fifo)
        self.watch_root = peer_steward.peer_message.peer_state_root() / "peer-watches"

    def _env(self, mode="idle", session_id="steward-1", with_herdr=True):
        env = dict(os.environ)
        for key in tuple(env):
            if key.startswith(("AGENT_DISPATCH_", "AGENT_SESSION_", "AGENT_RUNTIME_", "HERDR_")):
                env.pop(key, None)
        for key in (
            "AGENT_SESSION_ID", "AGENT_SESSION_ROLE", "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
            "OPENCODE_SESSION_ID", "OPENCODE_DISPATCH_SLUG",
        ):
            env.pop(key, None)
        env["AGENT_DISPATCH_JOBS"] = str(self.jobs_path)
        env["AGENT_PEER_LEDGER_ROOT"] = str(self.tmp_root)
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
        # A held reader can close between our open() and write() (a watcher that just
        # got EOF from the previous release): EPIPE means "no reader took it", retry.
        for _ in range(100):
            try:
                with open(self.fifo, "w") as fh:
                    fh.write("go")
                return
            except BrokenPipeError:
                time.sleep(0.01)
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
        self.fail(f"receipt never appeared for {watch_id}")

    def _wait_for_observation(self, watch_id, seconds=10):
        path = self.watch_root / f"{watch_id}.observation.json"
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if path.exists():
                return json.loads(path.read_text())
            time.sleep(0.02)
        self.fail(f"observation checkpoint never appeared for {watch_id}")


class WatchArmTest(_WatchMixin, unittest.TestCase):
    """A56-1."""

    def test_a_hook_armed_watch_tells_the_caller_to_end_the_turn(self):
        """Review round 2/3, M-B: the contract function was locked, its wiring
        was not -- reverting either call site passed every suite."""
        proc = self._run("watch", "peer-a", "--wake", "hook", env=self._env("idle"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        directive = self._directive(proc.stdout)
        self.assertEqual(directive.get("parent_next"), "end-turn", proc.stdout)
        self.assertEqual(directive.get("parent_next_reason"), "carrier-steward-watch")
        self.assertEqual(directive.get("parent_next_command"), "-")

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

    def _unbound_watch(self, mode):
        identity_path = self.tmp_root / "identity.json"
        identity_path.write_text(json.dumps({"session_id": ""}))
        env = self._env(mode)
        env["FAKE_HERDR_IDENTITY_FILE"] = str(identity_path)
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
        watch_id, _arm, _identity, _env = self._unbound_watch("idle")
        receipt = self._wait_for_receipt(watch_id)
        self.assertEqual((receipt["agent"]["session_id"], receipt["agent"]["pane"]), ("-", "w1:p9"))

    def test_pane_armed_watch_keeps_the_session_it_took(self):
        watch_id, arm_path, identity_path, env = self._unbound_watch("checkpoint")
        self.assertEqual(self._wait_for_observation(watch_id)["reason"], "timeout")
        identity_path.write_text(json.dumps({"session_id": "sid-first"}))
        again = self._run("watch", "peer-a", env=env)
        self.assertEqual(self._fields(again.stdout.splitlines()[0]).get("watch_id"), watch_id)
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

    def test_same_name_new_session_gets_separate_watch_and_old_rearm_cannot_overwrite_claim(self):
        env = self._env("timeout")
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
        duplicate = self._run("watch", "peer-a", env=env)
        self.assertEqual(self._fields(duplicate.stdout)["watch_id"], new_id)
        # A reconnect of the old duty writes only its own arm/observation.
        os.kill(old_arm["watcher"]["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 10
        while peer_steward._watcher_present(old_arm) and time.monotonic() < deadline:
            time.sleep(0.02)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(peer_steward._spawn_watch_observer(old_id, old_arm))
        self.assertEqual(claim.read_text().strip(), new_id)
        self.assertEqual(json.loads((self.watch_root / f"{new_id}.json").read_text())["agent"]["session_id"],
                         "new-session")
        self.assertFalse((self.watch_root / f"{old_id}.receipt.json").exists())

    def test_duplicate_arm_spawns_nothing(self):
        first = self._run("watch", "peer-a", env=self._env("held"))
        watch_id = self._fields(first.stdout)["watch_id"]
        self._wait_for_lock(watch_id)
        before = sorted(self.watch_root.glob("*.json"))
        second = self._run("watch", "peer-a", env=self._env("held"))
        self.assertEqual(second.returncode, 0)
        fields = self._fields(second.stdout)
        self.assertEqual(fields["state"], "already-armed")
        self.assertEqual(fields["watch_id"], watch_id)
        self.assertEqual(sorted(self.watch_root.glob("*.json")), before)
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
        self.assertEqual(len(list(self.watch_root.glob("*.json"))), 1)
        self._release()
        self._wait_for_receipt(watch_id)

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
            background_backoff = {"cmd_watch_run": "backoff", "cmd_obligation_runner": "delay"}

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
                    if enclosing_function(node) not in background_backoff:
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

        for name in ("peer-steward.py", "../hooks/peer-steward-rewake.py"):
            path = (_HERE / name).resolve()
            self.assertTrue(path.exists(), f"{name} missing")
            source = path.read_text()
            self.assertEqual(violations(source, bounded_ui=name == "peer-steward.py"),
                             [], f"{path.name}: foreground waits are fixed-deadline and task observers back off")
            if name == "peer-steward.py":
                self.assertIn("backoff = min(_WATCH_BACKOFF_MAX_SECONDS, backoff * 1.7)", source)
                self.assertIn("delay = min(15.0, delay * 1.7)", source)


class WatcherReceiptTest(_WatchMixin, unittest.TestCase):
    """A56-2."""

    def test_receipt_is_atomic_complete_and_text_free(self):
        proc = self._run("watch", "peer-a", env=self._env("held"))
        watch_id = self._fields(proc.stdout)["watch_id"]
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
        self.assertEqual(receipt["state"], "idle")
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

    def test_timeout_is_a_pending_checkpoint_then_same_duty_finishes(self):
        proc = self._run("watch", "peer-a", env=self._env("checkpoint"))
        watch_id = self._fields(proc.stdout)["watch_id"]
        checkpoint = self._wait_for_observation(watch_id)
        self.assertEqual(checkpoint["state"], "pending")
        self.assertEqual(checkpoint["reason"], "timeout")
        self.assertFalse((self.watch_root / f"{watch_id}.receipt.json").exists())
        self._release()
        self.assertEqual(self._wait_for_receipt(watch_id)["state"], "idle")

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


class WatchTerminalReconcileTest(unittest.TestCase):
    def test_server_rearm_drops_a_socket_path_bound_to_another_endpoint(self):
        module = peer_steward
        foreign = module._herdr_env_for_server("fourth", {
            "AGENT_HERDR_SESSION": "default", "HERDR_SOCKET_PATH": "/default.sock"})
        self.assertEqual(foreign["AGENT_HERDR_SESSION"], "fourth")
        self.assertNotIn("HERDR_SOCKET_PATH", foreign)
        exact = module._herdr_env_for_server("fourth", {
            "AGENT_HERDR_SESSION": "fourth", "HERDR_SOCKET_PATH": "/fourth.sock"})
        self.assertEqual(exact["HERDR_SOCKET_PATH"], "/fourth.sock")
        back_to_default = module._herdr_env_for_server("default", {
            "AGENT_HERDR_SESSION": "fourth", "HERDR_SOCKET_PATH": "/fourth.sock"})
        self.assertNotIn("HERDR_SOCKET_PATH", back_to_default)
        self.assertNotIn("AGENT_HERDR_SESSION", back_to_default)

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

            with mock.patch.object(peer_steward, "_watch_root", return_value=root), \
                 mock.patch.object(peer_steward.herdr_observation,
                                   "socket_path_for_endpoint", return_value="/fixture.sock"), \
                 mock.patch.object(peer_steward.herdr_observation,
                                   "ObservationTransport", return_value=Transport()), \
                 mock.patch.object(peer_steward, "_watcher_present", return_value=False), \
                 mock.patch.object(peer_steward, "_spawn_watch_observer") as spawn:
                peer_steward._ensure_watch_observers()
                spawn.assert_not_called()
                receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
                self.assertEqual((receipt["state"], receipt["reason"]),
                                 ("pane-exited", "pane-closed"))
                peer_steward._ensure_watch_observers()
                spawn.assert_not_called()


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
        self.addCleanup(idle.kill)
        never_locks = {
            "watch_id": "deadbeefdeadbeef", "target": "x",
            "watcher": {"pid": idle.pid, "pid_start": peer_steward.process_start_ticks(idle.pid)},
        }
        self.assertTrue(peer_steward._pid_identity_ok(idle.pid, never_locks["watcher"]["pid_start"]))
        self.assertFalse(peer_steward._is_zombie(idle.pid))
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
        first = self._run("ack", fields["watch_id"], "--carrier", "claude-async-rewake")
        second = self._run("ack", fields["watch_id"], "--carrier", "userprompt-sweep")
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertIn("ack=created", first.stdout)
        self.assertIn("ack=already", second.stdout)
        ack = json.loads((self.watch_root / f"{fields['watch_id']}.ack.json").read_text())
        self.assertEqual(ack["carrier"], "claude-async-rewake")
        self.assertEqual(ack["session_id"], "steward-1")

    def test_a_restarted_watch_line_does_not_claim_the_hook_either(self):
        """`state=rearmed` — the branch B1-a was originally raised about.

        Review round 4 found the code correct here and nothing asserting it:
        flipping this branch back to `arms_hook=True` passed all 68 tests.
        """
        dead = self._arm(target="peer-dead", mode="held")
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
        os.kill(int(dead["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and peer_steward._pid_identity_ok(
            int(dead["pid"]), dead["pid_start"]
        ):
            time.sleep(0.02)
        out = self._run("rearm", dead["watch_id"], env=self._env("held")).stdout
        fields = self._fields(out.splitlines()[0])
        self.assertEqual(fields["state"], "rearmed")
        self.assertEqual(fields["watch_id"], dead["watch_id"])
        new_arm = json.loads((self.watch_root / f"{fields['watch_id']}.json").read_text())
        self.assertEqual(new_arm["observer_generation"], 2)

        self._release()


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

    def test_checkpoint_survives_observer_restart_without_changing_watch_id(self):
        armed = self._fields(self._run("watch", "peer-a", env=self._env("checkpoint")).stdout)
        watch_id = armed["watch_id"]
        observation = self._wait_for_observation(watch_id)
        self.assertEqual(observation["reason"], "timeout")
        old_pid = int(armed["pid"])
        os.kill(old_pid, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and peer_steward._pid_identity_ok(
                old_pid, armed["pid_start"]):
            time.sleep(0.02)
        env = self._env("checkpoint")
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        resumed = self._run("__ensure-obligations", env=env)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        deadline = time.monotonic() + 10
        arm_path = self.watch_root / f"{watch_id}.json"
        updated = json.loads(arm_path.read_text())
        while time.monotonic() < deadline and updated["watcher"]["pid"] == old_pid:
            time.sleep(0.02)
            updated = json.loads(arm_path.read_text())
        self.assertNotEqual(updated["watcher"]["pid"], old_pid)
        self._release()
        receipt = self._wait_for_receipt(watch_id)
        self.assertEqual(receipt["watch_id"], watch_id)
        self.assertEqual(receipt["agent"]["session_id"], "sid-fake")
        self.assertEqual(receipt["agent"]["pane"], "w1:p9")

    def test_reconnect_preserves_pane_duty_across_tab_move_and_rejects_reused_identity(self):
        armed = self._fields(self._run("watch", "peer-a", env=self._env("checkpoint")).stdout)
        watch_id = armed["watch_id"]
        self.assertEqual(self._wait_for_observation(watch_id)["reason"], "timeout")
        old_pid = int(armed["pid"])
        os.kill(old_pid, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and peer_steward._pid_identity_ok(
                old_pid, armed["pid_start"]):
            time.sleep(0.02)

        identity_file = self.tmp_root / "reused-pane-once.json"
        identity_file.write_text(json.dumps({
            "session_id": "sid-reused", "pane": "w1:p-reused", "once": True,
        }), encoding="utf-8")
        env = self._env("checkpoint")
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env["FAKE_HERDR_IDENTITY_FILE"] = str(identity_file)
        env["FAKE_HERDR_WINDOW_ID"] = "window-after-tab-move"
        resumed = self._run("__ensure-obligations", env=env)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)

        deadline = time.monotonic() + 10
        arm_path = self.watch_root / f"{watch_id}.json"
        updated = json.loads(arm_path.read_text())
        while time.monotonic() < deadline and updated["watcher"]["pid"] == old_pid:
            time.sleep(0.02)
            updated = json.loads(arm_path.read_text())
        self.assertNotEqual(updated["watcher"]["pid"], old_pid)
        observation_path = self.watch_root / f"{watch_id}.observation.json"
        deadline = time.monotonic() + 10
        observation = {}
        while time.monotonic() < deadline:
            if observation_path.exists():
                observation = json.loads(observation_path.read_text())
                if observation.get("reason") == "target-identity-changed":
                    break
            time.sleep(0.02)
        self.assertEqual(observation.get("reason"), "target-identity-changed")
        self.assertFalse((self.watch_root / f"{watch_id}.receipt.json").exists())
        self.assertEqual(updated["watch_id"], watch_id)

        self._release()
        receipt = self._wait_for_receipt(watch_id)
        self.assertEqual(receipt["watch_id"], watch_id)
        self.assertEqual(receipt["agent"]["session_id"], "sid-fake")
        self.assertEqual(receipt["agent"]["pane"], "w1:p9")
        self.assertIn("window-after-tab-move", self.calllog.read_text(encoding="utf-8"))


class KillFixtureTest(_WatchMixin, unittest.TestCase):
    """A56-7 — the smallest falsifying test for the whole direction."""

    def _spawn_caller(self, subcommand):
        script = (
            "import subprocess,sys;"
            f"p=subprocess.run([sys.executable,{str(_HERE / 'peer-steward.py')!r},"
            f"{subcommand!r},'peer-a'],capture_output=True,text=True);"
            "open(%r,'w').write(p.stdout);" % str(self.tmp_root / "caller.out")
            + "import time;time.sleep(300)"
        )
        return subprocess.Popen([sys.executable, "-c", script], env=self._env("held"),
                                start_new_session=True)

    def test_watcher_survives_sigkill_of_the_callers_process_group(self):
        caller = self._spawn_caller("watch")
        # Read the watcher identity from disk, not from the caller's stdout:
        # a killed caller's stdout may never be flushed.
        deadline = time.monotonic() + 30
        arm = None
        while time.monotonic() < deadline:
            arms = [p for p in self.watch_root.glob("*.json")] if self.watch_root.is_dir() else []
            if arms:
                candidate = json.loads(arms[0].read_text())
                # The accepted record exists before its observer is published.
                # Kill the caller only after that observer's identity is ready.
                watcher = candidate.get("watcher") or {}
                if watcher.get("pid") and watcher.get("pid_start"):
                    arm = candidate
                    break
            time.sleep(0.02)
        self.assertIsNotNone(arm, "watch never armed")
        pid, pid_start = arm["watcher"]["pid"], arm["watcher"]["pid_start"]

        os.killpg(os.getpgid(caller.pid), signal.SIGKILL)
        caller.wait(timeout=10)

        self.assertTrue(peer_steward._pid_identity_ok(pid, pid_start),
                        "the watcher must outlive its caller's process group")
        self._release()
        receipt = self._wait_for_receipt(arm["watch_id"])
        self.assertEqual(receipt["state"], "idle")

    def test_wait_by_contrast_loses_its_herdr_child(self):
        # The regression proof: the identical fixture against (9) `wait`.
        caller = self._spawn_caller("wait")
        deadline = time.monotonic() + 30
        child = None
        while time.monotonic() < deadline and child is None:
            for entry in Path("/proc").glob("[0-9]*"):
                try:
                    if entry.joinpath("comm").read_text().strip() != "herdr":
                        continue
                    stat = entry.joinpath("stat").read_text()
                    if str(caller.pid) in stat.split(")")[-1].split()[2:4]:
                        child = int(entry.name)
                        break
                except OSError:
                    continue
            if child is None:
                time.sleep(0.02)
        os.killpg(os.getpgid(caller.pid), signal.SIGKILL)
        caller.wait(timeout=10)
        if child is not None:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and Path(f"/proc/{child}").exists():
                time.sleep(0.02)
            self.assertFalse(Path(f"/proc/{child}").exists(),
                             "`wait`'s herdr child must die with the caller's group")
        self.assertEqual(list(self.watch_root.glob("*.receipt.json"))
                         if self.watch_root.is_dir() else [], [],
                         "`wait` leaves no receipt -- that is the defect (10) fixes")
def _agent_json(harness, sid, name, status="idle"):
    return {"id": "cli:agent:get", "result": {"agent": {
        "agent": harness, "agent_status": status, "name": name, "pane_id": "w1:pX",
        "agent_session": ({"agent": harness, "kind": "id", "value": sid} if sid else None)}}}


class PeerReferenceAccessEvidenceTest(_TmpRootMixin, unittest.TestCase):
    def test_exact_local_codex_session_uses_declared_read_scope_evidence(self):
        target = {"harness": "codex", "session_id": "thread-exact", "pane": "w1:pX"}
        foreground = {"pid": os.getpid(), "start": "1", "argv": ["codex", "--resume"]}
        with mock.patch.object(peer_steward, "_retire_target",
                               return_value=("idle", target, 0, None)), \
             mock.patch.object(peer_steward, "_retire_foreground", return_value=foreground), \
             mock.patch.object(peer_steward.peer_message, "_reference_local_process_matches",
                               return_value=True):
            evidence = peer_steward._peer_reference_access_evidence(
                "codex-pane", "codex", "thread-exact", "w1:pX")
        self.assertEqual(evidence["recipient"], {
            "harness": "codex", "session_id": "thread-exact", "pane_id": "w1:pX"})
        self.assertEqual(evidence["local_process"]["pid"], os.getpid())
        self.assertEqual(evidence["local_process"]["pid_namespace"],
                         os.readlink(f"/proc/{os.getpid()}/ns/pid"))
        self.assertEqual(evidence["schema"], "peer-read-access-observation-v1")

    def test_proven_ordinary_codex_recipient_keeps_reference_presentation(self):
        target = {"harness": "codex", "session_id": "thread-exact", "pane": "w1:pX"}
        foreground = {"pid": os.getpid(), "start": "1", "argv": ["codex", "--resume"]}
        body = "[steer] normal supported delivery\n" + "evidence " * 300 + "\nlast condition: retain phase"
        with mock.patch.object(peer_steward, "_retire_target",
                               return_value=("idle", target, 0, None)), \
             mock.patch.object(peer_steward, "_retire_foreground", return_value=foreground), \
             mock.patch.object(peer_steward.peer_message, "_reference_local_process_matches",
                               return_value=True):
            evidence = peer_steward._peer_reference_access_evidence(
                "codex-pane", "codex", "thread-exact", "w1:pX")
            text, ref = peer_steward.peer_message.prepare_peer_message(
                body, {"harness": "claude", "session_id": "sender"},
                {"harness": "codex", "session_id": "thread-exact"},
                defer=True, reference_evidence=evidence)
        self.assertLess(len(text.encode("utf-8")), 1024)
        self.assertNotIn("last condition", text)
        self.assertEqual(peer_steward.peer_message.read_peer_payload(ref)["body"], body)
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["to"]["session_id"], "thread-exact")

    def test_unknown_sidless_other_runtime_and_changed_session_fail_conservative(self):
        with mock.patch.object(peer_steward, "_retire_target") as target:
            self.assertIsNone(peer_steward._peer_reference_access_evidence(
                "fresh-pane", "codex", None, "w1:pX"))
            self.assertIsNone(peer_steward._peer_reference_access_evidence(
                "claude-pane", "claude", "claude-session", "w1:pX"))
        target.assert_not_called()

        replies = iter((
            ("idle", {"harness": "codex", "session_id": "old-thread", "pane": "w1:pX"}, 0, None),
            ("idle", {"harness": "codex", "session_id": "replacement", "pane": "w1:pX"}, 0, None),
        ))
        with mock.patch.object(peer_steward, "_retire_target", side_effect=lambda _target: next(replies)), \
             mock.patch.object(peer_steward, "_retire_foreground",
                               return_value={"pid": os.getpid(), "start": "1", "argv": ["codex"]}), \
             mock.patch.object(peer_steward.peer_message, "_reference_local_process_matches",
                               return_value=True):
            self.assertIsNone(peer_steward._peer_reference_access_evidence(
                "codex-pane", "codex", "old-thread", "w1:pX"))


class F100cPromptAndResolutionTest(_TmpRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for key in ("AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
                    "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID"):
            os.environ.pop(key, None)
        runner = mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner", return_value=False)
        runner.start()
        self.addCleanup(runner.stop)
        readiness = mock.patch.object(peer_steward, "_pane_readiness",
                                      return_value=SimpleNamespace(state="ready", reason=""))
        readiness.start()
        self.addCleanup(readiness.stop)

    def _fake_run(self, payload, calls=None):
        def run(argv, **kwargs):
            if calls is not None:
                calls.append(argv)
            if argv[:3] == ["herdr", "agent", "get"]:
                return _herdr_json(payload)
            if argv[:3] == ["herdr", "pane", "process-info"]:
                pid = os.getpid()
                return _herdr_json({"result": {"process_info": {
                    "pane_id": "w1:pX", "shell_pid": pid,
                    "foreground_process_group_id": os.getpgrp(),
                    "foreground_processes": [{"pid": pid, "argv": ["python3", "peer_steward.test"]}],
                }}})
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
        return run

    @staticmethod
    def _pane_io_calls(calls):
        return [argv for argv in calls if argv[:3] in (
            ["herdr", "agent", "prompt"], ["herdr", "pane", "send-text"],
            ["herdr", "pane", "send-keys"], ["herdr", "agent", "send-text"],
            ["herdr", "agent", "send-keys"],
        )]

    def test_opencode_prompt_keeps_its_native_callback_path(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        harness = "opencode"
        calls = []
        payload = _agent_json(harness, "sid-" + harness, "child", status="idle")
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._fake_run(payload, calls)), \
             mock.patch.object(peer_steward, "_schedule_message_obligation"), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["prompt", "child", "[steer] deliver once"])
        self.assertEqual(rc, 3)
        self.assertIn("prompted=queued", printed.call_args[0][0])
        self.assertEqual(self._pane_io_calls(calls), [])
        row = peer_steward.peer_message._read_pending(self._all_records()[-1]["transfer_ref"])
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["to"]["session_id"], "sid-opencode")
        self.assertTrue(peer_steward.peer_message.parse_peer_trailer(
            row["text"], row["to"], include_ref=True))

    def _run_claude_prompt(self, *, state="idle", screen=None,
                           consume=False, readiness=None, foregrounds=None,
                           send_result=(0, {})):
        if screen is None:
            screen = CLAUDE_SUGGESTION
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        ident = {"harness": "claude", "session_id": "sid-child",
                 "name": "child", "pane": "w1:pX"}
        foreground = {"pid": os.getpid(), "start": "fixture-start",
                      "pid_namespace": os.readlink("/proc/self/ns/pid"),
                      "session_id": "sid-child"}
        snapshots = iter(foregrounds or [foreground, foreground])
        sent = []
        def deliver(target, text, *, wait, timeout_ms):
            sent.append((target, text, wait, timeout_ms))
            if consume:
                self.assertEqual(peer_steward.peer_message.receive_peer_message(
                    text, {"harness": "claude", "session_id": "sid-child"}), 0)
            return send_result
        ready = readiness or SimpleNamespace(state="ready", reason="")
        screen_reader = mock.Mock(return_value=peer_steward._screen_lines(screen))
        if state != "idle":
            screen_reader.side_effect = AssertionError("busy/blocked target screen must not be read")
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward, "_current_session_identity",
                               return_value=("sid-steward", "claude")), \
             mock.patch.object(peer_steward, "_from_name", return_value="steward"), \
             mock.patch.object(peer_steward, "_project_of", return_value="/fixture"), \
             mock.patch.object(peer_steward, "_resolve_target",
                               return_value=("claude", "sid-child", "child")), \
             mock.patch.object(peer_steward, "_agent_state",
                               return_value=(state, "w1:pX")), \
             mock.patch.object(peer_steward, "_retire_target",
                               return_value=(state, ident, 0, None)), \
             mock.patch.object(peer_steward, "_pane_readiness", return_value=ready), \
             mock.patch.object(peer_steward, "_claude_prompt_foreground_proof",
                               side_effect=lambda *_a, **_k: next(snapshots)), \
             mock.patch.object(peer_steward, "_read_screen", screen_reader), \
             mock.patch.object(peer_steward, "_herdr_prompt", side_effect=deliver), \
             mock.patch.object(peer_steward, "_schedule_message_obligation") as schedule, \
             mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner",
                               return_value=False), \
             mock.patch("builtins.print") as printed:
            args = peer_steward.build_parser().parse_args(
                ["prompt", "child", "[steer] exact private recipient ref"])
            rc = peer_steward.cmd_prompt(args)
            if state != "idle":
                self.assertEqual(screen_reader.call_count, 0)
        record = self._all_records()[-1]
        row = peer_steward.peer_message._read_pending(record["transfer_ref"])
        return rc, printed.call_args[0][0], row, sent, schedule

    def test_idle_claude_empty_composer_uses_exact_prompt_and_receiver_ref(self):
        rc, line, row, sent, schedule = self._run_claude_prompt(consume=True)
        self.assertEqual(rc, 0, line)
        self.assertIn("verify=exact-peer-ref", line)
        self.assertEqual(row["state"], "received")
        self.assertEqual(row["receipt"], "exact-peer-ref")
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0][0], sent[0][2]), ("w1:pX", False))
        self.assertEqual(schedule.call_count, 0)

    def test_idle_prompt_return_stays_queued_without_exact_receiver_ref(self):
        rc, line, row, sent, _schedule = self._run_claude_prompt()
        self.assertEqual(rc, 3, line)
        self.assertIn("verify=native-prompt-submitted", line)
        self.assertEqual((row["state"], row["receipt"]),
                         ("queued", "claude-pty-prompt-submitted"))
        self.assertEqual(len(sent), 1)

    def test_ambiguous_claude_prompt_reply_keeps_exact_claim_and_owner_without_retry(self):
        rc, line, row, sent, schedule = self._run_claude_prompt(
            send_result=(None, None))
        self.assertEqual(rc, 5, line)
        self.assertIn("verify=native-prompt-unverified", line)
        self.assertEqual(row["state"], "unverified")
        self.assertIsNotNone(row.get("rpc_claim"))
        self.assertEqual(len(sent), 1)
        schedule.assert_called_once()

    def test_busy_claude_uses_callback_without_reading_or_typing_into_pane(self):
        rc, line, row, sent, schedule = self._run_claude_prompt(
            state="working", screen="busy screen is intentionally unread")
        self.assertEqual(rc, 3, line)
        self.assertIn("verify=native-callback", line)
        self.assertEqual(row["state"], "pending")
        self.assertEqual(sent, [])
        schedule.assert_called_once()

    def test_busy_claude_with_native_busy_readiness_stays_callback_owned(self):
        readiness = SimpleNamespace(state="pending", reason="native-turn-busy")
        rc, line, row, sent, schedule = self._run_claude_prompt(
            state="working", readiness=readiness)
        self.assertEqual(rc, 3, line)
        self.assertIn("verify=native-callback", line)
        self.assertEqual((row["state"], sent), ("pending", []))
        schedule.assert_called_once()

    def test_claude_prompt_foreground_proof_requires_sid_pid_birth_and_namespace(self):
        pid = 424242
        group = pid
        info = {"pane_id": "w1:pX", "shell_pid": 123,
                "foreground_process_group_id": group,
                "foreground_processes": [{"pid": pid, "argv": ["/usr/bin/claude", "--tui"]}]}
        fields = ["S", "1", str(group), "0", "0", *(["0"] * 14), "123"]
        stat = f"{pid} (claude) " + " ".join(fields)

        def prove(*, session_ids=("sid-child", "sid-child"),
                  starts=("123", "123"),
                  target_ns="pid:[77]", sender_ns="pid:[77]"):
            def read_text(path, *args, **kwargs):
                if str(path) == f"/proc/{pid}/stat":
                    return stat
                if str(path) == f"/proc/{pid}/comm":
                    return "claude\n"
                raise AssertionError(f"unexpected proc read: {path}")
            def readlink(path):
                return sender_ns if str(path) == "/proc/self/ns/pid" else target_ns
            with mock.patch.object(peer_steward, "_retire_pane_info", return_value=info), \
                 mock.patch.object(peer_steward.Path, "read_text", autospec=True,
                                   side_effect=read_text), \
                 mock.patch.object(peer_steward, "_proc_start_ticks", side_effect=starts), \
                 mock.patch.object(peer_steward, "_proven_session", side_effect=session_ids), \
                 mock.patch.object(peer_steward.os, "readlink", side_effect=readlink):
                return peer_steward._claude_prompt_foreground_proof("w1:pX", "sid-child")

        proof = prove()
        self.assertEqual(proof, {"pid": pid, "start": "123",
                                 "pid_namespace": "pid:[77]", "session_id": "sid-child"})
        self.assertIsNone(prove(session_ids=("sid-child", "different-sid")))
        self.assertIsNone(prove(starts=("123", "456")))
        self.assertIsNone(prove(target_ns="pid:[other]"))

    def test_forms_drafts_unknown_boxes_and_blocked_state_never_receive_prompt_input(self):
        for state, screen in (("idle", CLAUDE_FORM), ("idle", CLAUDE_DRAFT),
                              ("idle", "screen without a composer"),
                              ("blocked", CLAUDE_EMPTY)):
            with self.subTest(state=state, screen=screen[:20]):
                rc, line, row, sent, _schedule = self._run_claude_prompt(state=state, screen=screen)
                self.assertEqual(rc, 3, line)
                self.assertEqual(row["state"], "pending")
                self.assertEqual(sent, [])

    def test_foreground_birth_change_between_checks_releases_unsent_claim(self):
        first = {"pid": os.getpid(), "start": "old-start",
                 "pid_namespace": os.readlink("/proc/self/ns/pid"), "session_id": "sid-child"}
        changed = {**first, "start": "new-start"}
        rc, line, row, sent, schedule = self._run_claude_prompt(
            foregrounds=[first, changed])
        self.assertEqual(rc, 3, line)
        self.assertIn("target-process-changed", line)
        self.assertEqual(row["state"], "pending")
        self.assertIsNone(row.get("rpc_claim"))
        self.assertEqual(sent, [])
        schedule.assert_called_once()

    def test_codex_prompt_uses_exact_native_queue_and_never_prompt_typing(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        calls, queued = [], []
        payload = _agent_json("codex", "exact-thread", "child", status="working")

        def accept(ref, *, timeout=1.0):
            row = peer_steward.peer_message._read_pending(ref)
            queued.append((row["to"]["session_id"], ref, row["text"]))
            return {"status": "queued", "reason": "native-queue-accepted"}

        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_run(payload, calls)), \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", side_effect=accept), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["prompt", "child", "[handoff] continue"])
        self.assertEqual(rc, 3)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][0], "exact-thread")
        self.assertEqual(self._pane_io_calls(calls), [])
        self.assertIn("native-queue-accepted", printed.call_args[0][0])
        record = self._all_records()[-1]
        self.assertEqual(record["delivery"]["status"], "unknown")
        self.assertIn("verify=native-queue-queued", record["delivery"]["receipt"])

    def test_codex_exact_history_receipt_is_the_only_prompted_true(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        payload = _agent_json("codex", "exact-thread", "child", status="idle")
        calls = []

        def consume(ref, *, timeout=1.0):
            with peer_steward.peer_message.pending_lock(ref):
                row = peer_steward.peer_message._read_pending(ref)
                peer_steward.peer_message._save_pending(dict(
                    row, state="received", text=None, receipt="exact-native-history"))
            return {"status": "received", "reason": "exact-native-history"}

        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_run(payload, calls)), \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", side_effect=consume), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["prompt", "child", "[steer] exact receipt"])
        self.assertEqual(rc, 0)
        self.assertIn("prompted=true", printed.call_args[0][0])
        self.assertIn("verify=native-queue-received", printed.call_args[0][0])
        self.assertEqual(peer_steward.peer_message._read_pending(self._all_records()[-1]["transfer_ref"])["state"],
                         "received")
        self.assertEqual(self._pane_io_calls(calls), [])

    def test_sidless_target_remains_pane_bound_until_native_session_exists(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        calls = []
        payload = _agent_json("codex", None, "fresh", status="idle")
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_run(payload, calls)), \
             mock.patch.object(peer_steward, "_proven_session", return_value=None), \
             mock.patch.object(peer_steward, "_process_session", return_value=None), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["prompt", "fresh", "wait for the exact thread"])
        self.assertEqual(rc, 3)
        self.assertIn("reason=target-session-unresolved", printed.call_args[0][0])
        row = peer_steward.peer_message._read_pending(self._all_records()[-1]["transfer_ref"])
        self.assertEqual((row["state"], row["to"]["session_id"], row["to"]["pane_id"]),
                         ("pending", None, "w1:pX"))
        self.assertEqual(self._pane_io_calls(calls), [])

    def test_sidless_codex_target_uses_the_thread_its_pane_tui_proves(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        calls, queued = [], []
        payload = _agent_json("codex", None, "fresh", status="idle")

        def accept(ref, *, timeout=1.0):
            queued.append(peer_steward.peer_message._read_pending(ref)["to"]["session_id"])
            return {"status": "queued", "reason": "native-queue-accepted"}

        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_run(payload, calls)), \
             mock.patch.object(peer_steward, "_process_session", return_value="proven-thread") as proof, \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", side_effect=accept), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["prompt", "fresh", "[steer] reach the proven thread"])
        self.assertEqual(rc, 3)
        proof.assert_called_once_with("w1:pX", "codex")
        self.assertEqual(queued, ["proven-thread"])
        self.assertIn("verify=native-queue-queued", printed.call_args[0][0])
        self.assertNotIn("target-session-unresolved", printed.call_args[0][0])
        row = peer_steward.peer_message._read_pending(self._all_records()[-1]["transfer_ref"])
        self.assertEqual((row["state"], row["to"]["session_id"]), ("pending", "proven-thread"))
        self.assertEqual(self._pane_io_calls(calls), [])

    def test_sidless_codex_obligation_binds_the_thread_its_pane_tui_proves(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        payload = _agent_json("codex", None, "fresh", status="idle")
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_run(payload)), \
             mock.patch.object(peer_steward, "_process_session", return_value=None), \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["prompt", "fresh", "[steer] after the proof"]), 3)
        ref = self._all_records()[-1]["transfer_ref"]
        store = peer_steward.peer_obligations.ObligationStore()
        duty_id = "message-" + ref
        sidless = {"harness": "codex", "session_id": "-", "pane": "w1:pX", "name": "fresh"}
        delivered = []

        def deliver(ref_, *, timeout=1.0):
            row = peer_steward.peer_message._read_pending(ref_)
            delivered.append(peer_steward.peer_message.pending_delivery_recipient(row)["session_id"])
            return {"status": "queued"}

        for proof, reason in ((None, "target-session-unresolved"), ("proven-thread", None)):
            with self.subTest(proof=proof), \
                 mock.patch.object(peer_steward, "_retire_target",
                                   return_value=("idle", sidless, 0, None)), \
                 mock.patch.object(peer_steward, "_process_session", return_value=proof) as resolver, \
                 mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", side_effect=deliver):
                peer_steward._resume_message_obligation(store.get(duty_id), store)
                resolver.assert_called_once_with("w1:pX", "codex")
                if reason:
                    self.assertEqual(store.get(duty_id)["observation"]["reason"], reason)
        self.assertEqual(delivered, ["proven-thread"])
        row = peer_steward.peer_message._read_pending(ref)
        self.assertEqual(row["bound_to"], {"harness": "codex", "session_id": "proven-thread",
                                           "pane_id": "w1:pX"})

    def test_same_sender_recipient_body_retry_reuses_the_same_ref(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        payload = _agent_json("claude", "sid-child", "child", status="idle")
        refs = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_run(payload)), \
             mock.patch("builtins.print"):
            for _ in range(2):
                self.assertEqual(peer_steward.main(["prompt", "child", "retry this exact body"]), 3)
                refs.append(self._all_records()[-1]["transfer_ref"])
        self.assertEqual(refs[0], refs[1])
        self.assertEqual(peer_steward.peer_message._read_pending(refs[0])["state"], "pending")

    def test_pending_flush_only_uses_codex_native_queue(self):
        claude_text, claude_ref = peer_steward.peer_message.prepare_peer_message(
            "preserve until Claude callback", {"harness": "codex", "session_id": "sender"},
            {"harness": "claude", "session_id": "claude-target"}, defer=True)
        with mock.patch.object(peer_steward.peer_message, "deliver_pending_codex") as queue, \
             mock.patch.object(peer_steward, "_herdr_prompt", side_effect=AssertionError("PTY prompt")):
            self.assertEqual(peer_steward._flush_pending_for_target(
                "claude-target", "claude", "claude-target", "idle")[0], 0)
        queue.assert_not_called()
        self.assertEqual(peer_steward.peer_message._read_pending(claude_ref)["text"], claude_text)

        _text, codex_ref = peer_steward.peer_message.prepare_peer_message(
            "exact Codex queued message", {"harness": "claude", "session_id": "sender"},
            {"harness": "codex", "session_id": "thread"}, defer=True)
        with mock.patch.object(peer_steward, "_resolve_target", return_value=("codex", "thread", "child")), \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex",
                               return_value={"status": "queued"}) as queue:
            self.assertEqual(peer_steward._flush_pending_for_target(
                "child", "codex", "thread", "idle"), (0, []))
        queue.assert_called_once_with(codex_ref, timeout=0.25)

    def test_claude_prompt_guard_accepts_a_finished_turn_but_not_work_or_questions(self):
        ident = {"harness": "claude", "session_id": "sid-1", "pane": "w1:p1"}
        proof = {"pid": 1, "start": "2", "pid_namespace": "pid:[1]", "session_id": "sid-1"}
        ready = mock.Mock(state="ready", reason=None)
        for state, expected in (("idle", None), ("done", None), ("working", "native-state-working"),
                                ("blocked", "native-state-blocked")):
            with self.subTest(state=state), \
                 mock.patch.object(peer_steward, "_retire_target", return_value=(state, ident, 0, None)), \
                 mock.patch.object(peer_steward, "_agent_state", return_value=(state, "w1:p1")), \
                 mock.patch.object(peer_steward, "_pane_readiness", return_value=ready), \
                 mock.patch.object(peer_steward, "_claude_prompt_foreground_proof", return_value=proof), \
                 mock.patch.object(peer_steward, "_read_screen", return_value=["> "]), \
                 mock.patch.object(peer_steward, "_screen_ready", return_value=None):
                reason, got = peer_steward._claude_idle_prompt_guard("w1:p1", "sid-1", "w1:p1")
                self.assertEqual(reason, expected)
                self.assertEqual(got, proof if expected is None else None)

    def test_source_has_no_general_prompt_observation_or_no_verify_bypass(self):
        source = (_HERE / "../utilities/peer-steward.py").resolve().read_text()
        start = source.index("def cmd_prompt(")
        end = source.index("# --- clear:", start)
        prompt = source[start:end]
        for obsolete in ("_prompt_box", "_verify_after_send", "_transcript_arrival",
                         "_prompt_input_reason", "no_verify", "state-flip"):
            self.assertNotIn(obsolete, prompt)
        self.assertIn("elif t_harness == \"claude\"", prompt)
        self.assertIn("_claude_idle_prompt_guard", prompt)
        self.assertNotIn("--no-verify", peer_steward.build_parser().format_help())


class ObligationStartupTest(_TmpRootMixin, unittest.TestCase):
    def pending_message(self):
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.create("message-fixture", "message",
                            {"harness": "codex", "session_id": "sid-fixture"}, {})
        return store, duty

    def test_existing_lifecycle_callback_reconnects_through_one_runner(self):
        self.pending_message()
        with mock.patch.object(peer_steward, "_watch_reconciliation_needed", return_value=False), \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "has_unowned_work", return_value=False), \
             mock.patch.object(peer_steward.peer_obligations, "ensure_runner", return_value=True) as runner, \
             mock.patch.object(peer_steward, "_reconcile_obligations") as inline, \
             mock.patch.object(peer_steward, "_ensure_watch_observers") as watches, \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "ensure_observers") as batches:
            self.assertEqual(peer_steward.main(["__ensure-obligations"]), 0)
        runner.assert_called_once_with(reconciliation_required=True)
        inline.assert_not_called()
        watches.assert_not_called()
        batches.assert_not_called()

    def test_legacy_or_unavailable_runner_uses_one_reconciliation_fallback(self):
        self.pending_message()
        with mock.patch.object(peer_steward, "_reconcile_external_obligations"), \
             mock.patch.object(peer_steward.peer_obligations, "ensure_runner", return_value=False) as runner, \
             mock.patch.object(peer_steward, "_reconcile_obligations") as inline:
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)
        runner.assert_called_once_with(reconciliation_required=True)
        inline.assert_called_once()

    def test_live_peer_runner_does_not_hide_new_external_work_on_next_callback(self):
        self.pending_message()
        with mock.patch.object(peer_steward, "_watch_reconciliation_needed",
                               side_effect=[False, True]), \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "has_unowned_work", side_effect=[False, True]), \
             mock.patch.object(peer_steward.peer_obligations, "ensure_runner", return_value=True), \
             mock.patch.object(peer_steward, "_ensure_watch_observers") as watches, \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "ensure_observers") as batches, \
             mock.patch.object(peer_steward, "_reconcile_obligations") as peer_work:
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)
            watches.assert_not_called()
            batches.assert_not_called()
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)
        watches.assert_called_once()
        batches.assert_called_once()
        peer_work.assert_not_called()

    def test_external_only_callback_recovers_without_an_idle_peer_runner(self):
        with mock.patch.object(peer_steward, "_watch_reconciliation_needed", return_value=True), \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "has_unowned_work", return_value=True), \
             mock.patch.object(peer_steward, "_ensure_watch_observers") as watches, \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "ensure_observers") as batches, \
             mock.patch.object(peer_steward.peer_obligations, "ensure_runner") as runner:
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)
        watches.assert_called_once()
        batches.assert_called_once()
        runner.assert_not_called()

    def test_pending_peer_ticks_do_not_scan_external_families(self):
        store, duty = self.pending_message()
        ticks = []

        def resume(row, current):
            ticks.append(row["id"])
            if len(ticks) == 3:
                current.update(row["id"], state="complete")

        with (store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME).open("w") as lock, \
             mock.patch.object(peer_steward, "_resume_message_obligation", side_effect=resume), \
             mock.patch.object(peer_steward, "_ensure_watch_observers") as watches, \
             mock.patch.object(peer_steward, "_watch_reconciliation_needed") as watch_probe, \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "ensure_observers") as batches, \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "has_unowned_work") as batch_probe, \
             mock.patch.object(peer_steward.time, "sleep") as sleep:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(peer_steward.cmd_obligation_runner(
                SimpleNamespace(state_root=self.tmp_root, lock_fd=lock.fileno())), 0)
        self.assertEqual(ticks, [duty["id"]] * 3)
        self.assertEqual(sleep.call_count, 2)
        for operation in (watches, watch_probe, batches, batch_probe):
            operation.assert_not_called()

    def test_work_accepted_at_unlock_is_reclaimed_before_runner_exit(self):
        store = peer_steward.peer_obligations.ObligationStore()
        store.root.mkdir(parents=True, exist_ok=True)
        accepted = []
        real_flock = fcntl.flock

        def unlock_and_accept(fd, operation):
            result = real_flock(fd, operation)
            if operation == fcntl.LOCK_UN and not accepted:
                accepted.append(store.create("message-at-exit", "message",
                    {"harness": "codex", "session_id": "sid-exit"}, {}))
            return result

        def resume(row, current):
            current.update(row["id"], state="complete")

        with (store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME).open("w") as lock, \
             mock.patch.object(peer_steward, "_resume_message_obligation",
                               side_effect=resume) as resumed:
            real_flock(lock, fcntl.LOCK_EX)
            with mock.patch.object(peer_steward.fcntl, "flock", side_effect=unlock_and_accept):
                self.assertEqual(peer_steward.cmd_obligation_runner(
                    SimpleNamespace(state_root=self.tmp_root, lock_fd=lock.fileno())), 0)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(resumed.call_count, 1)
        self.assertEqual(store.get(accepted[0]["id"])["state"], "complete")


class NormalProducerRunnerBindingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.jobs = self.base / "dispatch" / "jobs.log"
        self.env = {"AGENT_PEER_LEDGER_ROOT": str(self.state),
                    "AGENT_DISPATCH_JOBS": str(self.jobs),
                    "HOME": str(self.base / "home")}
        Path(self.env["HOME"]).mkdir()
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def _exact_retire(self):
        return {"server": "default", "pane": "fixture-pane", "harness": "codex",
                "session_id": "sid-retire", "name": "retire-target",
                "foreground": {"pid": 123, "start": "456"}}

    def test_message_and_delay_producers_use_the_shared_binding_helper(self):
        sender = {"harness": "claude", "session_id": "sid-sender", "name": "sender"}
        recipient = {"harness": "codex", "session_id": "sid-recipient", "name": "recipient"}
        message = {"ref": "a" * 32, "body_sha256": "b" * 64,
                   "from": sender, "to": recipient, "refs": []}
        with mock.patch.object(peer_steward.peer_obligations,
                               "ensure_normal_runner") as ensure:
            duty = peer_steward._schedule_message_obligation(message, "recipient", "fixture-pane")
            self.assertIsNotNone(duty)
            ensure.assert_called_once_with()

        delay_row = {"ref": "c" * 32, "created": time.time(), "refs": [],
                     "from": sender, "to": recipient}
        prepared = {"ref": "d" * 32, "body_sha256": "e" * 64,
                    "from": recipient, "to": sender, "refs": ["delay-notice:" + delay_row["ref"]]}
        with mock.patch.object(peer_steward.peer_obligations,
                               "ensure_normal_runner") as ensure, \
             mock.patch.object(peer_steward.peer_message, "prepare_peer_message",
                               return_value=("fixture text", prepared["ref"])), \
             mock.patch.object(peer_steward.peer_message, "_read_pending",
                               return_value=prepared):
            duty = peer_steward._ensure_sender_delay_notice(delay_row)
            self.assertIsNotNone(duty)
            self.assertEqual(ensure.call_count, 2)

    def test_retire_and_handoff_retire_producers_use_the_shared_binding_helper(self):
        exact = self._exact_retire()
        requester = {"session_id": "sid-requester", "harness": "codex",
                     "pane": "requester-pane", "server": "default"}
        with mock.patch.object(peer_steward.peer_obligations,
                               "ensure_normal_runner") as ensure:
            _store, duty = peer_steward._retire_request(
                "retire-target", exact, requester=requester)
            self.assertEqual(duty["intent"]["identity"], exact)
            ensure.assert_called_once_with()

        with mock.patch.object(peer_steward.peer_obligations,
                               "ensure_normal_runner") as ensure:
            handoff_exact = {**exact, "pane": "handoff-pane"}
            duty_id = peer_steward.reserve_handoff_retire(
                "handoff-target", handoff_exact, requester)
            self.assertTrue(duty_id.startswith("retire-"))
            self.assertEqual(ensure.call_count, 2)


class PromptDraftGuardTest(_TmpRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for key in ("AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
                    "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID"):
            os.environ.pop(key, None)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        self.harness, self.sid = "claude", "sid-child"
        self.readiness = type("Ready", (), {"state": "ready", "reason": ""})()
        self.enterContext(mock.patch.object(peer_steward.shutil, "which", return_value="/fixture/herdr"))
        self.enterContext(mock.patch.object(peer_steward.subprocess, "run", side_effect=self.run_herdr))
        self.enterContext(mock.patch.object(peer_steward, "_read_screen",
                                            side_effect=AssertionError("peer delivery read composer")))
        self.enterContext(mock.patch.object(peer_steward, "_pane_readiness",
                                            side_effect=lambda *a, **k: self.readiness))
        self.enterContext(mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner",
                                            return_value=False))
        self.enterContext(mock.patch("builtins.print"))

    def run_herdr(self, argv, **kwargs):
        if argv[:3] == ["herdr", "agent", "get"]:
            return _herdr_json(_agent_json(self.harness, self.sid, "child", status="working"))
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    def prompt(self, body="peer body"):
        return peer_steward.main(["prompt", "child", body])

    def test_non_codex_peer_prompt_retains_exact_ref_for_native_callback(self):
        self.assertEqual(self.prompt("[steer] native callback body"), 3)
        ref = self._all_records()[-1]["transfer_ref"]
        row = peer_steward.peer_message._read_pending(ref)
        self.assertEqual(row["state"], "pending")
        self.assertIn("native callback body", row["text"])
        self.assertEqual(row["to"], {"harness": "claude", "session_id": self.sid,
                                     "name": "child", "pane_id": "w1:pX"})

    def test_registered_work_readiness_withholds_queue_or_native_callback_delivery(self):
        self.readiness = type("NotReady", (), {
            "state": "pending", "reason": "bound-registered-work-pending"})()
        queue = mock.Mock()
        with mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", queue):
            self.assertEqual(self.prompt("keep execution ownership"), 3)
        queue.assert_not_called()
        row = peer_steward.peer_message._read_pending(self._all_records()[-1]["transfer_ref"])
        self.assertEqual((row["state"], row["receipt"]), ("pending", "bound-registered-work-pending"))

    def test_codex_queue_does_not_read_or_type_into_composer(self):
        self.harness, self.sid = "codex", "thread-exact"
        accepted = []
        def queue(ref):
            accepted.append(peer_steward.peer_message._read_pending(ref))
            return {"status": "queued", "reason": "native-queue-accepted"}
        with mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", side_effect=queue):
            self.assertEqual(self.prompt("[steer] exact queue"), 3)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["to"]["session_id"], "thread-exact")
        self.assertEqual(accepted[0]["state"], "pending")
        self.assertEqual(accepted[0]["text"].splitlines()[0], "[steer] exact queue")

    def test_reconnected_obligation_keeps_claude_payload_pending_without_ptysend(self):
        self.assertEqual(self.prompt("parent duty stays sealed"), 3)
        ref = self._all_records()[-1]["transfer_ref"]
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.get("message-" + ref)
        self.assertIsNotNone(duty)
        with mock.patch.object(peer_steward, "_retire_target", return_value=("idle", {
                "harness": "claude", "session_id": self.sid, "pane": "w1:pX", "name": "child"}, 0, None)), \
             mock.patch.object(peer_steward, "_herdr_prompt",
                               side_effect=AssertionError("obligation runner typed into pane")):
            peer_steward._resume_message_obligation(duty, store)
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "pending")
        self.assertEqual(store.get(duty["id"])["state"], "pending")

    def test_no_trailer_and_no_verify_arguments_cannot_reinstate_screen_transport(self):
        parser = peer_steward.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["prompt", "child", "body", "--no-verify"])
        self.assertEqual(self.prompt("ordinary native body"), 3)
        row = peer_steward.peer_message._read_pending(self._all_records()[-1]["transfer_ref"])
        self.assertEqual(row["state"], "pending")


class F100cStewardModeTest(_TmpRootMixin, unittest.TestCase):
    def test_steward_on_off_raises_and_clears_the_flag(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_steward.main(["steward", "on"]), 0)
        self.assertIn("steward=on", print_mock.call_args[0][0])
        markers = peer_steward.peer_message.read_steward_markers()
        self.assertIn(("claude", "sid-steward"), markers)
        entry = markers[("claude", "sid-steward")]["targets"]["-"]
        self.assertEqual((entry["kind"], entry["source"], entry["session_id"]), ("explicit", "explicit", None))
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_steward.main(["steward", "off"]), 0)
        self.assertIn("steward=off", print_mock.call_args[0][0])
        self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})

    def test_steward_on_without_identity_is_a_typed_failure(self):
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_steward.main(["steward", "on"]), 1)
        self.assertIn("reason=no-session-identity", print_mock.call_args[0][0])


class FromNameTest(_TmpRootMixin, unittest.TestCase):
    """C-3 — `_from_name` reads Claude via `claude_session_name` (unchanged) and
    Codex/OpenCode via `tools/fleet/session_registry.py` (B-1), the same lazy
    `tools/` sys.path trick `peer-message.py:412 steward_marker_roots` uses for
    non-Fleet consumers. Any other harness, or any failure along the way, is
    `None` — a name is never guessed."""

    def test_unknown_harness_and_missing_registry_record_are_none(self):
        self.assertIsNone(peer_steward._from_name("unknown", "sid-1"))
        with tempfile.TemporaryDirectory() as registry_dir:
            os.environ["FLEET_SESSION_REGISTRY_DIR"] = registry_dir
            self.assertIsNone(peer_steward._from_name("codex", "no-such-session"))
            self.assertIsNone(peer_steward._from_name("opencode", "no-such-session"))

    def test_codex_and_opencode_resolve_via_session_registry(self):
        with tempfile.TemporaryDirectory() as registry_dir:
            os.environ["FLEET_SESSION_REGISTRY_DIR"] = registry_dir
            for harness, pid, sid, name in (
                ("codex", 111, "codex-sess-1", "hearting-codex-1"),
                ("opencode", 222, "oc-sess-1", "hearting-oc-1"),
            ):
                d = Path(registry_dir) / harness
                d.mkdir(parents=True)
                (d / ("%d.json" % pid)).write_text(json.dumps(
                    {"pid": pid, "sessionId": sid, "name": name, "harness": harness}))
                self.assertEqual(peer_steward._from_name(harness, sid), name)


class FromNameBareSubprocessTest(unittest.TestCase):
    """C-3 — the lazy `tools/` sys.path insert must work in the actual condition
    a real herdr-launched `peer-steward.py` subprocess runs under: a cwd outside
    the repository and no inherited `PYTHONPATH`. `test_importable_with_only_
    tools_on_sys_path` (B-1) proves this for `session_registry` alone; this
    proves the full `peer-steward.py::_from_name` codex path reaches it too."""

    def test_from_name_resolves_a_codex_name_in_a_bare_subprocess(self):
        with tempfile.TemporaryDirectory() as registry_dir, \
             tempfile.TemporaryDirectory() as outside_cwd:
            pid = 424242
            record_dir = Path(registry_dir) / "codex"
            record_dir.mkdir(parents=True)
            (record_dir / ("%d.json" % pid)).write_text(json.dumps({
                "pid": pid, "sessionId": "codex-sess-c3", "name": "hearting-codex-c3",
                "harness": "codex",
            }))
            script = (
                "import importlib.util\n"
                "spec = importlib.util.spec_from_file_location('peer_steward', %r)\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(mod)\n"
                "print(mod._from_name('codex', 'codex-sess-c3') or '')\n"
            ) % str(_HERE / "peer-steward.py")
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env["FLEET_SESSION_REGISTRY_DIR"] = registry_dir
            proc = subprocess.run([sys.executable, "-c", script], cwd=outside_cwd,
                                  capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "hearting-codex-c3")


class CurrentSessionIdentityDelegationTest(unittest.TestCase):
    """F-<next> fleet-route-chain-r2 plan §3 B-3: `_current_session_identity` delegates to
    `dispatch_parent_completion.interactive_parent_identity` first, keeping the prior
    AGENT_SESSION_ID fallback for the ambiguous/unset case; a native id is never guessed."""

    _ENV_KEYS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID",
                 "CODEX_SESSION_ID", "OPENCODE_SESSION_ID", "AGENT_SESSION_ID",
                 "AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
                 "HERDR_PANE_ID")

    def _clean_env(self, **overrides):
        env = {key: None for key in self._ENV_KEYS}
        env.update(overrides)
        return mock.patch.dict(os.environ, {k: v for k, v in env.items() if v is not None},
                               clear=False)

    def setUp(self):
        for key in self._ENV_KEYS:
            os.environ.pop(key, None)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in self._ENV_KEYS])

    def test_explicit_caller_harness_wins(self):
        with self._clean_env(CLAUDE_CODE_SESSION_ID="sid-c", CODEX_THREAD_ID="sid-x",
                             AGENT_DISPATCH_CALLER_HARNESS="codex"):
            self.assertEqual(peer_steward._current_session_identity(), ("sid-x", "codex"))

    def test_ambiguous_multiple_sessions_records_unknown_sender(self):
        # No explicit caller harness + two sessions set -> interactive_parent_identity()
        # raises caller-harness-ambiguous. The sender is unknown, never the first of the
        # claude > codex > opencode guesses (that named a Codex thread `claude [bc]`).
        with self._clean_env(CLAUDE_CODE_SESSION_ID="sid-c", CODEX_THREAD_ID="sid-x"):
            self.assertEqual(peer_steward._current_session_identity(), ("", "unknown"))

    def test_ambiguous_env_keeps_the_agent_session_id_fallback(self):
        with self._clean_env(CLAUDE_CODE_SESSION_ID="sid-c", CODEX_THREAD_ID="sid-x",
                             AGENT_SESSION_ID="sid-legacy"):
            self.assertEqual(peer_steward._current_session_identity(), ("sid-legacy", "unknown"))

    def test_explicit_name_without_its_sid_never_takes_foreign_sid(self):
        with self._clean_env(CLAUDE_CODE_SESSION_ID="sid-c", AGENT_DISPATCH_CALLER_HARNESS="codex"):
            self.assertEqual(peer_steward._current_session_identity(), ("", "unknown"))

    def test_invalid_explicit_name_never_takes_a_native_sid(self):
        with self._clean_env(CODEX_THREAD_ID="sid-x", AGENT_DISPATCH_CALLER_HARNESS="gemini"):
            self.assertEqual(peer_steward._current_session_identity(), ("", "unknown"))

    def test_single_session_delegates_cleanly(self):
        with self._clean_env(CODEX_THREAD_ID="sid-solo"):
            self.assertEqual(peer_steward._current_session_identity(), ("sid-solo", "codex"))

    def test_agent_session_id_still_falls_back_to_unknown(self):
        with self._clean_env(AGENT_SESSION_ID="sid-legacy"):
            self.assertEqual(peer_steward._current_session_identity(), ("sid-legacy", "unknown"))

    def test_nothing_set_returns_empty_unknown(self):
        with self._clean_env():
            self.assertEqual(peer_steward._current_session_identity(), ("", "unknown"))


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
OPENCODE_HOME_BLANK = "\n" * 12 + "  /path/to/project:main\n\n"     # 1.18.34: nothing but the cwd line
OPENCODE_HOME = "\n\n  ┃  Ask anything... \"Fix broken tests\"\n  ┃\n  ┃  Build auto · Model OpenCode Go\n  ╹▀▀▀▀▀\n"
NO_BOX = "just some output\nwith no prompt box at all\n"
OLD_T = "01a0fbf0-3ad5-7501-9838-eb440504686e"
NEW_T = "01a0fbf0-d65e-7041-9744-c8f5300f35a3"


def codex_screen(footer):
    """A measured codex 0.160.0 bottom: the input box, one blank line, the status line, the hint line."""
    return ("• recap of the last turn\n\n\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m\n\n  "
            + footer + "\n  ? for shortcuts\n")


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
            return subprocess.CompletedProcess(argv, self.prompt_rc, stdout="{}", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    def typed(self):
        return [c for c in self.calls if c[:3] in (["herdr", "agent", "prompt"], ["herdr", "agent", "send-keys"])]


class ClearTest(_TmpRootMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        import session_tidy as st
        import session_tidy_clear as clear
        self.st, self.clear = st, clear
        self.seat = st.Seat("pane", st._digest("pane", "w1:pX"), "w1:pX", "claude", "")
        self.path = clear.reservation_path(self.seat.key)

    def book(self, harness="claude", sid="sid-A", pane="w1:pX", seq=0, deadline_in=600):
        st, clear = self.st, self.clear
        seat = st.Seat("pane", st._digest("pane", pane), pane, harness, "")
        now = time.time()
        with st.seat_lock(seat.key):
            card = st.write_card(seat, harness, sid, "card body", prompt_seq=seq)
            clear._write_reservation({
                "schema": 1, "nonce": "n0nce", "status": "reserved", "created": now, "deadline": now + deadline_in,
                "seat": {"kind": "pane", "key": seat.key, "pane": pane, "harness": harness, "project_key": ""},
                "harness": harness, "sid": sid, "cwd": str(self.tmp_root),
                "card_generation": card["generation"], "prompt_seq": seq})
        self.path = clear.reservation_path(seat.key)
        return seat

    def clear_cmd(self, world, nonce="n0nce", extra=()):
        argv = ["clear", world.pane, "--request", str(self.path), *( ["--nonce", nonce] if nonce else [] ), *extra]
        printed = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = peer_steward.main(argv)
        return rc, (printed[-1] if printed else "")

    def rows(self):
        return [r for r in self._all_records() if r.get("kind") == "notice"]

    # -- success ------------------------------------------------------------------------

    def test_an_idle_claude_with_an_empty_box_gets_exactly_one_clear_and_it_is_true(self):
        self.book()
        world = _ClearWorld(screens=[CLAUDE_EMPTY])
        rc, line = self.clear_cmd(world)
        self.assertEqual(rc, 0, line)
        self.assertIn("cleared=true", line)
        self.assertIn("old_session=sid-A", line)
        self.assertIn("new_session=sid-B", line)
        prompts = [c for c in world.calls if c[:3] == ["herdr", "agent", "prompt"]]
        self.assertEqual(prompts, [["herdr", "agent", "prompt", "w1:pX", "/clear"]])   # no trailer, no wait
        self.assertEqual([c for c in world.calls if c[:3] == ["herdr", "agent", "send-keys"]], [])

    def test_a_compact_booking_types_only_after_the_turn_ended_and_only_once(self):
        st, clear = self.st, self.clear
        transcript = self.tmp_root / "sid-A.jsonl"
        transcript.write_text(json.dumps({"type": "user", "uuid": "u-tool", "message": {
            "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}) + "\n", encoding="utf-8")
        info = transcript.stat()
        context = {"trigger": "auto", "identity": "steward-compact", "transcript": str(transcript),
                   "boundary_offset": info.st_size, "transcript_identity": f"{info.st_dev}:{info.st_ino}"}
        seat = st.Seat("pane", st._digest("pane", "w1:pX"), "w1:pX", "claude", "")
        now = time.time()
        with st.seat_lock(seat.key):
            card = st.write_card(seat, "claude", "sid-A", "summary", prompt_seq=0, origin="runtime-compact",
                                 compact_context=context)
            clear._write_reservation({
                "schema": 1, "nonce": "n0nce", "status": "reserved", "created": now, "deadline": now + 600,
                "seat": {"kind": "pane", "key": seat.key, "pane": "w1:pX", "harness": "claude", "project_key": ""},
                "harness": "claude", "sid": "sid-A", "cwd": str(self.tmp_root), "origin": "compact",
                "compact": {**context}, "card_generation": card["generation"], "prompt_seq": 0})
        self.path = clear.reservation_path(seat.key)
        world = _ClearWorld(screens=[CLAUDE_EMPTY])
        rc, line = self.clear_cmd(world)
        self.assertEqual(rc, 3, line)
        self.assertIn("reason=compact-pending", line)
        self.assertEqual(world.typed(), [])
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "assistant", "uuid": "a-end", "message": {
                "stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}}) + "\n")
        rc, line = self.clear_cmd(world)
        self.assertEqual(rc, 0, line)
        self.assertEqual(len(world.typed()), 1)
        rc, line = self.clear_cmd(world)            # the claim was consumed: nothing is typed again
        self.assertEqual(rc, 3, line)
        self.assertEqual(len(world.typed()), 1)

    def test_a_claude_suggestion_is_an_empty_box_but_typed_text_is_not(self):
        for screen, expect in ((CLAUDE_SUGGESTION, "cleared=true"), (CLAUDE_DRAFT, "reason=draft"),
                               (CLAUDE_DRAFT_SECOND_LINE, "reason=draft")):
            with self.subTest(screen=screen[:30]):
                self.book()
                world = _ClearWorld(screens=[screen])
                rc, line = self.clear_cmd(world)
                self.assertIn(expect, line)
                self.assertEqual(rc, 0 if expect == "cleared=true" else 3)
                self.assertEqual(len(world.typed()), 1 if rc == 0 else 0)

    def test_codex_placeholder_is_empty_a_draft_or_a_popup_is_not(self):
        for screen, expect in ((CODEX_EMPTY, "cleared=true"), (CODEX_DRAFT, "reason=draft"),
                               (CODEX_POPUP, "reason=draft-unknown")):
            with self.subTest(screen=screen[:40]):
                self.book(harness="codex", sid="thr-A")
                world = _ClearWorld(harness="codex", sid="thr-A", screens=[screen], new_sid="thr-B")
                rc, line = self.clear_cmd(world)
                self.assertIn(expect, line)
                self.assertEqual(len(world.typed()), 1 if expect == "cleared=true" else 0)

    def test_opencode_types_new_and_the_home_screen_is_the_proof(self):
        self.book(harness="opencode", sid="ses_A")
        world = _ClearWorld(harness="opencode", sid="ses_A", screens=[OPENCODE_EMPTY], new_sid=None,
                            home_after=OPENCODE_HOME)
        with mock.patch.object(peer_steward, "_process_session",
                               side_effect=lambda *_args: None if world.sent else "ses_A"):
            rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "cleared=true" in line), (0, True), line)
        self.assertEqual(world.typed(), [["herdr", "agent", "prompt", "w1:pX", "/new"]])
        for after, expect in ((OPENCODE_HOME_BLANK, "cleared=true"), (OPENCODE_EMPTY, "cleared=unverified"),
                              ("\n\n", "cleared=unverified")):
            with self.subTest(after=after[:20]):
                self.book(harness="opencode", sid="ses_A")
                world = _ClearWorld(harness="opencode", sid="ses_A", screens=[OPENCODE_EMPTY], new_sid=None,
                                    home_after=after)
                with mock.patch.object(peer_steward, "_process_session",
                                       side_effect=lambda *_args: None if world.sent else "ses_A"), \
                     mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                    rc, line = self.clear_cmd(world)
                self.assertIn(expect, line)
                self.assertEqual(len(world.typed()), 1, line)
        # OpenCode's pane stays `done` through /new, so the pause between looks is a wait for the
        # screen (event-driven), not the idle wait that would return at once.
        self.book(harness="opencode", sid="ses_A")
        world = _ClearWorld(harness="opencode", sid="ses_A", screens=[OPENCODE_EMPTY], new_sid=None,
                            home_after=OPENCODE_EMPTY)
        with mock.patch.object(peer_steward, "_process_session",
                               side_effect=lambda *_args: None if world.sent else "ses_A"), \
             mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 3):
            rc, line = self.clear_cmd(world)
        self.assertIn("cleared=unverified", line)
        self.assertEqual(len([c for c in world.calls if c[:3] == ["herdr", "pane", "wait-output"]]), 2)
        for screen, reason in ((OPENCODE_DRAFT, "draft"), (NO_BOX, "draft-unknown")):
            self.book(harness="opencode", sid="ses_A")
            world = _ClearWorld(harness="opencode", sid="ses_A", screens=[screen], new_sid=None)
            with mock.patch.object(peer_steward, "_process_session",
                                   side_effect=lambda *_args: None if world.sent else "ses_A"):
                rc, line = self.clear_cmd(world)
            self.assertEqual((rc, f"reason={reason}" in line, world.typed()), (3, True, []), line)

    # -- doubt is "not cleared": zero input ---------------------------------------------------

    def test_working_blocked_unreadable_and_form_screens_get_no_input(self):
        cases = (({"status": "working"}, "not-idle-working"), ({"status": "blocked"}, "form-open"),
                 ({"status": "unknown"}, "not-idle-unknown"), ({"screens": [None]}, "screen-unknown"),
                 ({"screens": [CLAUDE_FORM]}, "form-open"), ({"screens": [NO_BOX]}, "draft-unknown"))
        for kwargs, reason in cases:
            with self.subTest(reason=reason):
                self.book()
                world = _ClearWorld(**kwargs)
                rc, line = self.clear_cmd(world)
                self.assertEqual(rc, 3, line)
                self.assertIn(f"reason={reason}", line)
                self.assertEqual(world.typed(), [])

    def test_a_draft_that_appears_between_the_two_looks_is_never_typed_over(self):
        self.book()
        world = _ClearWorld(screens=[CLAUDE_EMPTY, CLAUDE_DRAFT])      # the user starts typing after look 1
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "reason=draft" in line, world.typed()), (3, True, []), line)

    def test_a_new_request_expiry_a_stale_nonce_or_a_finished_booking_get_no_input(self):
        seat = self.book(seq=0)
        with self.st.seat_lock(seat.key):
            self.st.bump_prompt_seq(seat, "claude", "sid-A", time.time())
        world = _ClearWorld()
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "reason=new-input" in line), (3, True))
        self.book(deadline_in=-5)
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "reason=expired" in line), (3, True))
        self.book()
        rc, line = self.clear_cmd(world, nonce="another")
        self.assertEqual((rc, "reason=superseded" in line), (3, True))
        self.assertEqual(world.typed(), [])
        self.assertEqual(self.rows(), [])                               # a dead request leaves no ledger row

    def test_the_target_must_be_the_booked_pane_harness_and_session(self):
        for kwargs, label in (({"harness": "codex"}, "harness"), ({"sid": "someone-else"}, "session")):
            with self.subTest(label):
                self.book()
                world = _ClearWorld(**kwargs)
                rc, line = self.clear_cmd(world)
                self.assertEqual((rc, "reason=target-changed" in line, world.typed()), (3, True, []), line)
        self.book()
        world = _ClearWorld()
        world.pane = "w9:pZ"
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "reason=target-changed" in line), (3, True))

    def test_the_new_session_is_the_process_one_when_herdr_reports_a_stale_session(self):
        for proof, expect in (("sid-NEW", "cleared=true"), ("sid-A", "cleared=unverified")):
            with self.subTest(process_says=proof):
                self.book()
                world = _ClearWorld(sid="sid-A", new_sid="sid-stale-older")   # herdr names an older session
                with mock.patch("fleet.collectors.claude.session_id_of_process", return_value=proof), \
                        mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                    rc, line = self.clear_cmd(world)
                self.assertIn(expect, line)
                if expect == "cleared=true":
                    self.assertIn("new_session=sid-NEW", line)

    def test_a_pane_record_that_lags_a_clear_is_settled_by_the_process_not_guessed(self):
        # herdr still reports the session before the previous /clear; the process knows better.
        for proof, expect_typed in (("sid-A", 1), ("sid-other", 0), (None, 0)):
            with self.subTest(process_says=proof):
                self.book()
                world = _ClearWorld(sid="sid-before-the-last-clear", new_sid="sid-B")
                with mock.patch("fleet.collectors.claude.session_id_of_process",
                                side_effect=lambda pid: "sid-B" if world.sent else proof):
                    rc, line = self.clear_cmd(world)
                self.assertEqual(len(world.typed()), expect_typed, line)
                self.assertEqual("cleared=true" in line, bool(expect_typed), line)
                if not expect_typed:
                    self.assertIn("reason=target-changed", line)

    def test_a_pane_record_without_a_session_id_is_cleared_only_when_the_process_names_the_booked_one(self):
        # herdr names no session (`-`): only a current native pane proof can vouch for it.
        for harness, collector in (("claude", "claude"), ("codex", "codex")):
            for proof, expect_typed in (("sid-A", 1), ("sid-other", 0), (None, 0)):
                with self.subTest(harness=harness, process_says=proof):
                    self.book(harness=harness)
                    screen = CLAUDE_EMPTY if harness == "claude" else CODEX_EMPTY
                    world = _ClearWorld(harness=harness, sid=None, new_sid=None, screens=[screen])
                    with mock.patch(f"fleet.collectors.{collector}.session_id_of_process", return_value=proof), \
                            mock.patch("pane_ownership.current_codex_thread",
                                       return_value=proof or ""), \
                            mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                        rc, line = self.clear_cmd(world)
                    self.assertEqual(len(world.typed()), expect_typed, line)
                    if not expect_typed:
                        self.assertEqual((rc, "reason=target-changed" in line), (3, True), line)

    def test_a_codex_pane_record_that_trails_the_seat_ledger_is_not_a_changed_target(self):
        # Shared app-server daemon: no rollout for the process check, herdr keeps the session
        # before the last clear.  The seat ledger (both sessions' hooks at this pane) orders them.
        st = self.st
        seat = st.Seat("pane", st._digest("pane", "w1:pX"), "w1:pX", "codex", "")
        with st.seat_lock(seat.key):
            st.record_event(seat, "codex", "sid-A", "prompt", cwd="/w", now=time.time() - 100)
            st.record_event(seat, "codex", "sid-B", "prompt", cwd="/w", now=time.time() - 50)
        for herdr_says, typed in (("sid-A", 1), ("sid-never-seen", 0), ("sid-B", 1)):
            with self.subTest(herdr_says=herdr_says):
                self.book(harness="codex", sid="sid-B")
                world = _ClearWorld(harness="codex", sid=herdr_says, new_sid=None, screens=[CODEX_EMPTY])
                # the process check may name the older session too (same-cwd fallback of the board)
                with mock.patch("fleet.collectors.codex.session_id_of_process",
                                return_value="sid-A" if herdr_says == "sid-A" else None), \
                        mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                    rc, line = self.clear_cmd(world)
                self.assertEqual(len(world.typed()), typed, line)
                if typed:
                    # the lagging value is never taken for the new session
                    self.assertIn("cleared=unverified", line)
                    self.assertNotIn("new_session=sid-A", line)
                else:
                    self.assertIn("reason=target-changed", line)

    # -- after the send: never a second one -------------------------------------------------

    def test_a_send_that_cannot_be_confirmed_is_unverified_and_nothing_else_is_typed(self):
        self.book()
        world = _ClearWorld(new_sid=None)                               # herdr keeps the old id, no hook note
        rc, line = self.clear_cmd(world)
        self.assertEqual(rc, 5, line)
        self.assertIn("cleared=unverified", line)
        self.assertEqual(len(world.typed()), 1)                         # one /clear, no Enter retry, no resend

    def test_the_start_hook_note_alone_proves_the_new_conversation(self):
        seat = self.book()
        world = _ClearWorld(new_sid=None)
        original = world.run

        def run(argv, **kw):
            result = original(argv, **kw)
            if argv[:3] == ["herdr", "agent", "prompt"]:               # the new session's start hook fires
                with self.st.seat_lock(seat.key):
                    self.clear.note_start_locked(seat, "claude", "sid-NEW", time.time() + 1)
            return result
        world.run = run
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "new_session=sid-NEW" in line), (0, True), line)

    def test_a_failing_herdr_prompt_is_failed_and_herdr_missing_is_failed(self):
        self.book()
        world = _ClearWorld()
        world.prompt_rc = 1
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "cleared=failed" in line), (1, True), line)
        printed = []
        with mock.patch.object(peer_steward.shutil, "which", return_value=None), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = peer_steward.main(["clear", "w1:pX", "--request", str(self.path)])
        self.assertEqual((rc, printed[-1]), (1, "cleared=failed reason=herdr-not-found"))

    def test_a_new_thread_on_the_codex_status_line_proves_the_clear(self):
        self.book(harness="codex", sid=OLD_T)
        world = _ClearWorld(harness="codex", sid=OLD_T, new_sid=None, screens=[codex_screen("proj · main · Fix it")],
                            home_after=codex_screen(f"proj · main · Context 0% used · {NEW_T}"))
        with mock.patch("fleet.collectors.codex.session_id_of_process", return_value=None), \
                mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
            rc, line = self.clear_cmd(world)
        self.assertEqual((rc, f"new_session={NEW_T}" in line), (0, True), line)
        self.assertEqual(len(world.typed()), 1)

    def test_only_the_complete_last_status_line_field_is_current_identity(self):
        other = "01a0fbf0-aaaa-7bbb-8ccc-dddddddddddd"
        st = self.st
        seat = st.Seat("pane", st._digest("pane", "w1:pX"), "w1:pX", "codex", "")
        with st.seat_lock(seat.key):
            st.record_event(seat, "codex", other, "prompt", cwd="/w", now=time.time() - 300)
        cases = ((f"proj · {NEW_T} · Fix it", f"proj · {NEW_T} · Fix it", "already shown", False),
                 ("proj · Fix it", f"proj · {NEW_T} · 01a0fbf0-eeee-7fff-8000-111111111111",
                  "earlier UUID does not override last", True),
                 ("proj · Fix it", "proj · Context 0% used · 01a0fbf0-d65e-7041-9744-c8f5300f…",
                  "cut id", False),
                 (f"proj · {OLD_T}", f"proj · {OLD_T} · {NEW_T}",
                  "last field wins over earlier UUID", True),
                 ("proj · Fix it", f"proj · {other}", "a session the seat already knows", False))
        for before, after, label, changed in cases:
            with self.subTest(label):
                self.book(harness="codex", sid=OLD_T)
                world = _ClearWorld(harness="codex", sid=OLD_T, new_sid=None, screens=[codex_screen(before)],
                                    home_after=codex_screen(after))
                with mock.patch("fleet.collectors.codex.session_id_of_process", return_value=None), \
                        mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                    rc, line = self.clear_cmd(world)
                self.assertEqual("cleared=true" in line, changed, line)
                self.assertEqual(rc, 0 if changed else 5, line)

    def test_a_user_opened_codex_location_menu_is_never_selected_by_clear(self):
        self.book(harness="codex", sid=OLD_T)
        menu = ("Where should the new conversation run?\n"
                "❯ Use current Git worktree\n"
                "Keep using the current working directory\n"
                "Create new Git worktree\n")
        world = _ClearWorld(harness="codex", sid=OLD_T, screens=[menu])
        rc, line = self.clear_cmd(world)
        self.assertEqual(rc, 3, line)
        self.assertIn("cleared=skipped", line)
        self.assertIn("reason=form-open", line)
        self.assertEqual(world.typed(), [])

    def test_a_clear_opened_location_menu_is_not_reported_as_cleared_or_selected(self):
        self.book(harness="codex", sid=OLD_T)
        menu = ("Where should the new conversation run?\n"
                "❯ Use current Git worktree\n"
                "Keep using the current working directory\n"
                "Create new Git worktree\n")
        world = _ClearWorld(harness="codex", sid=OLD_T, new_sid=NEW_T,
                            screens=[CODEX_EMPTY], home_after=menu)
        with mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
            rc, line = self.clear_cmd(world)
        self.assertEqual(rc, 5, line)
        self.assertIn("cleared=unverified", line)
        self.assertEqual(len(world.typed()), 1)  # the authorized /clear only
        self.assertEqual([c for c in world.calls if c[:3] == ["herdr", "agent", "send-keys"]], [])

    def test_every_judged_call_leaves_one_notice_ledger_row_with_the_action(self):
        self.book()
        self.clear_cmd(_ClearWorld(screens=[CLAUDE_DRAFT]))
        self.book()
        self.clear_cmd(_ClearWorld(screens=[CLAUDE_EMPTY]))
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["kind"] == "notice" and "action=clear" in r["delivery"]["receipt"] for r in rows))
        self.assertIn("cleared=skipped", rows[0]["delivery"]["receipt"])
        self.assertIn("cleared=true", rows[1]["delivery"]["receipt"])
        self.assertEqual((rows[1]["to"]["pane"], rows[1]["from"]["session_id"]), ("w1:pX", "sid-A"))

    def test_the_screen_reader_decides_from_layout_not_from_wishful_text(self):
        lines = peer_steward._screen_lines
        draft = peer_steward._draft_state
        self.assertEqual(draft("claude", lines(CLAUDE_EMPTY)), "empty")
        self.assertEqual(draft("claude", lines(RULE + "\n❯ typed\n")), "nonempty")   # no closing rule: still a draft
        self.assertEqual(draft("claude", lines(RULE + "\n❯\n")), "unknown")           # box never closes: unknown
        self.assertEqual(draft("codex", lines("›\n")), "empty")
        self.assertEqual(draft("opencode", lines("  ┃  just text\n")), "unknown")      # a single bar line is no box
        self.assertEqual(draft("unheard-of", lines(CLAUDE_EMPTY)), "unknown")


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


class IdleCardRequestTest(_TmpRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import session_tidy as st
        import session_tidy_clear as clear
        self.st, self.clear = st, clear

    def booking(self, harness="claude", sid="sid-A", pane="w1:pX"):
        st, clear = self.st, self.clear
        seat = st.Seat("pane", st._digest("pane", pane), pane, harness, "")
        now = time.time()
        with st.seat_lock(seat.key):
            card = st.write_card(seat, harness, sid, "prior card", prompt_seq=0)
            clear._write_reservation({
                "schema": 1, "nonce": "idle-nonce", "status": "reserved", "created": now - 60,
                "deadline": now + 300, "seat": seat.as_dict(), "harness": harness,
                "sid": sid, "cwd": str(self.tmp_root), "card_generation": card["generation"],
                "prompt_seq": 0, "origin": "idle", "followup_kind": "status-only",
                "snapshot_state": "ready", "idle": {"identity": "idle-event", "due_at": now - 1,
                                                          "tokens": 200000, "min_tokens": 200000}})
        return seat, clear.reservation_path(seat.key)

    def invoke(self, req, *, looks=None, prompt_reply=None):
        pane = req["seat"]["pane"]
        args = SimpleNamespace(request=str(self.clear.reservation_path(req["seat"]["key"])),
                               nonce=req["nonce"], target=pane)
        agent = {"pane": pane, "harness": req["harness"], "session_id": req["sid"]}
        result = {"accepted": "true"}

        def prompt(_target, text, **_kwargs):
            if prompt_reply:
                return prompt_reply(text)
            with mock.patch.object(self.st, "resolve_seat", return_value=self.seat_obj):
                self.st.run_hook(req["harness"], "prompt", req["sid"], cwd=req["cwd"], prompt_text=text)
            return 0, {}

        self.seat_obj = self.st.seat_from_dict(req["seat"])
        with mock.patch.object(peer_steward, "_herdr_missing", return_value=False), \
             mock.patch.object(peer_steward, "_clear_look", side_effect=looks or [(None, agent, [])] * 3), \
             mock.patch.object(peer_steward, "_herdr_prompt", side_effect=prompt) as send, \
             mock.patch.object(peer_steward, "_record"), mock.patch("builtins.print") as output:
            rc = peer_steward.cmd_idle_card_request(args)
        return rc, output.call_args[0][0], send

    def test_claimed_request_sends_once_and_needs_the_exact_prompt_arrival_receipt(self):
        seat, path = self.booking()
        self.seat_obj = seat
        req = self.clear.read_reservation(seat.key)
        with mock.patch.object(peer_steward, "_herdr_missing", return_value=False), \
             mock.patch.object(peer_steward, "_clear_look", return_value=(None,
                 {"pane": seat.pane, "harness": "claude", "session_id": "sid-A"}, [])), \
             mock.patch.object(peer_steward, "_record"), mock.patch("builtins.print") as output, \
             mock.patch.object(peer_steward, "_herdr_prompt") as send:
            def prompt(_target, text, **_kwargs):
                with mock.patch.object(self.st, "resolve_seat", return_value=seat):
                    self.st.run_hook("claude", "prompt", "sid-A", cwd=req["cwd"], prompt_text=text)
                return 0, {}
            send.side_effect = prompt
            rc = peer_steward.cmd_idle_card_request(SimpleNamespace(request=str(path), nonce=req["nonce"],
                                                                      target=seat.pane))
        self.assertEqual(rc, 0)
        self.assertIn("accepted=true", output.call_args[0][0])
        send.assert_called_once()
        booking = self.clear.read_reservation(seat.key)
        self.assertEqual(booking["idle_request"]["state"], "sent")
        self.assertEqual(booking["idle_request"]["arrival"]["sid"], "sid-A")

    def test_draft_or_other_opencode_session_sends_nothing(self):
        seat, path = self.booking()
        req = self.clear.read_reservation(seat.key)
        blocked = ("draft", {"pane": seat.pane, "harness": "claude", "session_id": "sid-A"}, None)
        rc, line, sent = self.invoke(req, looks=[blocked])
        self.assertEqual((rc, line.split()[0]), (3, "accepted=queued"))
        sent.assert_not_called()
        self.assertNotIn("idle_request", self.clear.read_reservation(seat.key))

        op_seat, op_path = self.booking(harness="opencode", sid="op-sid", pane="w1:pO")
        op_req = self.clear.read_reservation(op_seat.key)
        wrong = (None, {"pane": op_seat.pane, "harness": "opencode", "session_id": "foreign"}, [])
        rc, line, sent = self.invoke(op_req, looks=[wrong])
        self.assertEqual((rc, line), (3, "accepted=skipped target=w1:pO harness=opencode session=op-sid reason=target-changed"))
        sent.assert_not_called()


TIMEOUT_REPLY = (1, json.dumps({"error": {"code": "timeout"}}))


class ContinueTest(_TmpRootMixin, unittest.TestCase):
    rows = ClearTest.rows

    def setUp(self):
        super().setUp()
        import session_tidy as st
        import session_tidy_clear as clear
        self.st, self.clear = st, clear

    def cleared(self, harness="claude", sid="sid-A", new="sid-B", pane="w1:pX",
                seq=0, receipt=True, window=120):
        st, clear = self.st, self.clear
        shutil.rmtree(st.state_root(), ignore_errors=True)
        seat = st.Seat("pane", st._digest("pane", pane), pane, harness, "")
        now = time.time()
        with st.seat_lock(seat.key):
            card = st.write_card(seat, harness, sid, "card body", prompt_seq=seq)
            if receipt and harness == "claude":
                st._write_consumed(seat, generation=card["generation"], receipts=[f"claude:{new}:0"])
            clear._write_reservation({
                "schema": 1, "nonce": "n0nce", "status": "cleared", "created": now - 30,
                "deadline": now + 570,
                "seat": {"kind": "pane", "key": seat.key, "pane": pane,
                         "harness": harness, "project_key": ""},
                "harness": harness, "sid": sid, "cwd": str(self.tmp_root), "new_session": new,
                "card_generation": card["generation"], "prompt_seq": seq, "continue_off": False,
                "continued": {"state": "pending", "deadline": now + window}})
        self.seat_obj, self.path = seat, clear.reservation_path(seat.key)
        return seat

    def continue_cmd(self, nonce="n0nce"):
        printed = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward, "_continue_look", return_value=None), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = peer_steward.main(["continue", self.seat_obj.pane, "--request", str(self.path),
                                    *(["--nonce", nonce] if nonce else [])])
        return rc, printed[-1] if printed else ""

    def booking(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_claude_continue_types_once_into_the_idle_tui(self):
        self.cleared()
        guard = mock.Mock(return_value=(None, {"pid": 202}))
        with mock.patch.object(peer_steward, "_claude_idle_prompt_guard", guard), \
             mock.patch.object(peer_steward, "_herdr_prompt", return_value=(0, {})) as send:
            rc, line = self.continue_cmd()
        self.assertEqual(rc, 0, line)
        self.assertIn("continued=true", line)
        self.assertIn("verify=state-flip", line)
        send.assert_called_once()
        self.assertEqual(send.call_args.args, ("w1:pX", self.clear.CONTINUE_TEXT))
        self.assertEqual(guard.call_args_list[1].kwargs, {"expected_foreground": {"pid": 202}})
        ref = line.split("transfer_ref=", 1)[1].split()[0]
        row = peer_steward.peer_message._read_pending(ref)
        # Not pending: the new session's prompt hook must not deliver it a second time.
        self.assertEqual(row["state"], "queued")
        self.assertIsNone(peer_steward.peer_message.claim_pending_herdr(
            ref, {"harness": "claude", "session_id": "sid-B", "pane_id": "w1:pX"}))

    def test_claude_continue_send_without_state_flip_is_never_retyped(self):
        self.cleared()
        with mock.patch.object(peer_steward, "_claude_idle_prompt_guard",
                               return_value=(None, {"pid": 202})), \
             mock.patch.object(peer_steward, "_herdr_prompt",
                               return_value=(1, {"error": {"code": "timeout"}})):
            rc, line = self.continue_cmd()
        self.assertEqual(rc, 5, line)
        self.assertIn("continued=unverified", line)
        ref = line.split("transfer_ref=", 1)[1].split()[0]
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "unverified")

    def test_continue_keeps_exact_no_trailer_ref_pending_for_native_callback(self):
        self.cleared()
        refused = mock.patch.object(peer_steward, "_claude_idle_prompt_guard",
                                    return_value=("draft", None))
        no_send = mock.patch.object(peer_steward, "_herdr_prompt",
                                    side_effect=AssertionError("continue PTY send"))
        with refused, no_send:
            rc, line = self.continue_cmd()
        self.assertEqual(rc, 3, line)
        self.assertIn("continued=queued", line)
        self.assertIn("reason=draft", line)
        ref = line.split("transfer_ref=", 1)[1].split()[0]
        row = peer_steward.peer_message._read_pending(ref)
        self.assertEqual((row["state"], row["text"], row["no_trailer"], row["receipt"]),
                         ("pending", self.clear.CONTINUE_TEXT, True, "session-tidy-continue"))
        self.assertEqual(row["to"], {"harness": "claude", "session_id": "sid-B",
                                     "name": None, "pane_id": "w1:pX"})
        claim_id = line.split("claim_id=", 1)[1].split()[0]
        self.assertTrue(self.clear._finish_continue(
            self.seat_obj.key, "n0nce", "queued", "awaiting-native-receiver",
            claim_id=claim_id, transfer_ref=ref))
        booked = self.booking()["continued"]
        self.assertEqual((booked["state"], booked["transfer_ref"]), ("pending", ref))
        with refused, no_send:
            second_rc, second = self.continue_cmd()
        self.assertEqual(second_rc, 3, second)
        self.assertIn("transfer_ref=" + ref, second)
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "pending")

    def test_codex_continue_uses_exact_queue_and_receipt_settles_same_booking(self):
        self.cleared(harness="codex", sid="old-thread", new="new-thread")
        observed = []
        with mock.patch.object(peer_steward.peer_message, "deliver_pending_codex",
                               side_effect=lambda ref: observed.append(ref)
                               or {"status": "queued", "reason": "native-queue-accepted"}), \
             mock.patch.object(peer_steward, "_herdr_prompt", side_effect=AssertionError("continue PTY send")), \
             mock.patch.object(peer_steward, "_read_screen", side_effect=AssertionError("composer read")):
            rc, line = self.continue_cmd()
        self.assertEqual(rc, 3, line)
        ref = line.split("transfer_ref=", 1)[1].split()[0]
        self.assertEqual(observed, [ref])
        row = peer_steward.peer_message._read_pending(ref)
        self.assertEqual((row["to"]["session_id"], row["text"], row["no_trailer"]),
                         ("new-thread", self.clear.CONTINUE_TEXT, True))
        claim_id = line.split("claim_id=", 1)[1].split()[0]
        self.assertTrue(self.clear._finish_continue(
            self.seat_obj.key, "n0nce", "queued", "native-queue-accepted",
            claim_id=claim_id, transfer_ref=ref))

        def consumed(ref_arg):
            with peer_steward.peer_message.pending_lock(ref_arg):
                current = peer_steward.peer_message._read_pending(ref_arg)
                peer_steward.peer_message._save_pending(dict(
                    current, state="received", text=None, receipt="exact-native-history"))
            return {"status": "received", "reason": "exact-native-history"}

        with mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", side_effect=consumed):
            rc, line = self.continue_cmd()
        self.assertEqual(rc, 0, line)
        self.assertIn("verify=exact-native-history", line)
        self.assertEqual(self.booking()["continued"]["state"], "sending")
        claim_id = line.split("claim_id=", 1)[1].split()[0]
        self.assertTrue(self.clear._finish_continue(
            self.seat_obj.key, "n0nce", "true", "", claim_id=claim_id, transfer_ref=ref))
        self.assertEqual(self.booking()["continued"]["state"], "sent")

    def test_continue_claim_token_prevents_stale_queue_from_rewinding_newer_attempt(self):
        self.cleared()
        first, reason = self.clear.claim_continue(self.path, "n0nce")
        self.assertEqual(reason, "")
        self.assertTrue(self.clear.release_unsent_continue(first))
        second, reason = self.clear.claim_continue(self.path, "n0nce")
        self.assertEqual(reason, "")
        old_id = first["continued"]["claim_id"]
        new_id = second["continued"]["claim_id"]
        self.assertNotEqual(old_id, new_id)
        self.assertFalse(self.clear._finish_continue(
            self.seat_obj.key, "n0nce", "queued", "late", claim_id=old_id,
            transfer_ref="1" * 32))
        self.assertEqual(self.booking()["continued"]["claim_id"], new_id)
        self.assertTrue(self.clear._finish_continue(
            self.seat_obj.key, "n0nce", "queued", "native-pending", claim_id=new_id,
            transfer_ref="2" * 32))
        self.assertEqual(self.booking()["continued"]["transfer_ref"], "2" * 32)
        self.assertEqual(self.booking()["continued"]["state"], "pending")

    def test_continue_requires_exact_new_sid_and_idle_without_screen_reads(self):
        self.cleared()
        req = self.booking()
        info = {"pane_id": self.seat_obj.pane, "shell_pid": 101,
                "foreground_process_group_id": 202,
                "foreground_processes": [{"pid": 202, "argv": ["claude"]}]}
        for sid, native_sid, state, expected in (("sid-B", "sid-B", "idle", None),
                                                 ("sid-B", "old-sid", "idle", "target-changed"),
                                                 ("sid-B", "sid-B", "blocked", "not-idle-blocked")):
            ident = {"harness": "claude", "session_id": sid, "pane": self.seat_obj.pane, "name": "child"}
            with self.subTest(sid=sid, state=state), \
                 mock.patch.object(peer_steward, "_retire_target",
                                   return_value=(state, ident, 0, None)), \
                 mock.patch.object(peer_steward, "_retire_pane_info", return_value=info), \
                 mock.patch.object(peer_steward, "_continue_native_session", return_value=native_sid), \
                 mock.patch.object(peer_steward, "_pane_readiness",
                                   return_value=type("Ready", (), {"state": "ready", "reason": ""})()), \
                 mock.patch.object(peer_steward, "_read_screen", side_effect=AssertionError("composer read")):
                self.assertEqual(peer_steward._continue_look(self.seat_obj.pane, req), expected)

    def test_user_prompt_card_handoff_and_expiry_still_stop_continue(self):
        def bump(seat):
            with self.st.seat_lock(seat.key):
                self.st.bump_prompt_seq(seat, "claude", "sid-B", time.time())

        def rewrite(seat):
            with self.st.seat_lock(seat.key):
                self.st.write_card(seat, "claude", "sid-B", "new card")

        def expired(_seat):
            data = self.booking()
            data["continued"]["deadline"] = time.time() - 1
            self.path.write_text(json.dumps(data), encoding="utf-8")

        for change, reason in ((bump, "new-input"), (rewrite, "card-changed"),
                               (self.st.mark_card_handed_off, "handed-off"), (expired, "expired")):
            with self.subTest(reason=reason):
                change(self.cleared())
                with mock.patch.object(peer_steward, "_herdr_prompt",
                                       side_effect=AssertionError("continued an invalid booking")):
                    rc, line = self.continue_cmd()
                self.assertEqual(rc, 3, line)
                self.assertIn("reason=" + reason, line)
                self.assertFalse(self.booking().get("continued", {}).get("transfer_ref"))

    def test_herdr_missing_remains_a_typed_failure(self):
        self.cleared()
        printed = []
        with mock.patch.object(peer_steward.shutil, "which", return_value=None), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = peer_steward.main(["continue", self.seat_obj.pane, "--request", str(self.path)])
        self.assertEqual((rc, printed[-1]), (1, "continued=failed reason=herdr-not-found"))


class GridStartTest(_TmpRootMixin, unittest.TestCase):
    FULL = [(10, 2, 200, 100)]
    TWO = [(10, 2, 100, 100), (110, 2, 100, 100)]
    THREE = [(10, 2, 100, 100), (110, 2, 100, 50), (110, 52, 100, 50)]
    FOUR = [(10, 2, 100, 50), (10, 52, 100, 50), (110, 2, 100, 50), (110, 52, 100, 50)]

    def layout(self, rects, area=None):
        return {"workspace_id": "w1", "tab_id": "w1:tGrid",
                "area": dict(zip(("x", "y", "width", "height"), area or self.FULL[0])),
                "panes": [{"pane_id": f"w1:p{i}", "rect": dict(zip(("x", "y", "width", "height"), rect))}
                          for i, rect in enumerate(rects)]}

    def decision(self, layout, free=None, foreground=None, prompt=True):
        free = free or set()
        def run(argv, **kwargs):
            key = "tabs" if argv[1:3] == ["tab", "list"] else "layout"
            return _herdr_json({"result": {key: [] if key == "tabs" else layout}})
        with mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_pane_has_agent", side_effect=lambda p: None if p in free else "pane-occupied"), \
             mock.patch.object(peer_steward, "_pane_foreground_shell", return_value=foreground), \
             mock.patch.object(peer_steward, "_wait_for_shell_prompt", return_value=prompt):
            return peer_steward._start_placement("w1:p0")

    def workspace_decision(self, rects_by_tab, free=()):
        """Live inventory order differs from creation order; names are user-owned."""
        layouts, panes, tabs = {}, [], []
        for number, (tab, rects) in enumerate(rects_by_tab, 1):
            layout = self.layout(rects)
            layout["tab_id"] = tab
            for i, pane in enumerate(layout["panes"]):
                pane["pane_id"] = f"w1:p{number}-{i}"
                panes.append({"pane_id": pane["pane_id"], "tab_id": tab, "workspace_id": "w1"})
                layouts[pane["pane_id"]] = layout
            tabs.append({"tab_id": tab, "workspace_id": "w1", "number": number,
                         "label": f"직접 정한 이름 {number}"})
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[1:3] == ["tab", "list"]:
                return _herdr_json({"result": {"tabs": tabs}})
            if argv[1:3] == ["pane", "list"]:
                return _herdr_json({"result": {"panes": panes}})
            if argv[1:3] == ["pane", "layout"]:
                return _herdr_json({"result": {"layout": layouts[argv[-1]]}})
            raise AssertionError(argv)
        with mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_pane_has_agent", side_effect=lambda p: None if p in free else "pane-occupied"), \
             mock.patch.object(peer_steward, "_pane_foreground_shell", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_shell_prompt", return_value=True):
            decision = peer_steward._start_placement("w1:p1-0")
        return decision, calls

    def test_full_caller_fills_the_newest_other_partial_grid(self):
        decision, calls = self.workspace_decision([
            ("w1:tCaller", self.FOUR), ("w1:tOlder", self.TWO), ("w1:tNewest", self.FULL)])
        self.assertEqual(decision, ("split", "w1:p3-0", "right"))
        self.assertNotIn(["herdr", "pane", "layout", "--pane", "w1:p2-0"], calls)
        self.assertTrue(all(argv[1:3] in (["pane", "layout"], ["pane", "list"], ["tab", "list"])
                            for argv in calls))

    def test_all_workspace_tabs_full_create_a_new_tab(self):
        decision, _ = self.workspace_decision([
            ("w1:tCaller", self.FOUR), ("w1:tOther", self.FOUR)])
        self.assertEqual(decision, ("tab", "w1", None))

    def test_other_full_tab_with_an_empty_shell_is_reused(self):
        decision, _ = self.workspace_decision([
            ("w1:tCaller", self.FOUR), ("w1:tOther", self.FOUR)], free={"w1:p2-3"})
        self.assertEqual(decision, ("reuse", "w1:p2-3", None))

    def test_full_newest_tab_does_not_hide_an_older_partial_tab(self):
        decision, _ = self.workspace_decision([
            ("w1:tCaller", self.FOUR), ("w1:tOlder", self.THREE), ("w1:tNewest", self.FOUR)])
        self.assertEqual(decision, ("split", "w1:p2-0", "down"))

    def test_unreadable_or_foreign_tab_inventory_never_allocates(self):
        for inventory in ({"error": {"code": "unavailable"}}, {"result": {"tabs": None}},
                          {"result": {"tabs": [{"workspace_id": "w2", "tab_id": "w2:t1", "number": 9}]}}):
            calls = []
            def run(argv, **kwargs):
                calls.append(argv)
                return _herdr_json({"result": {"layout": self.layout(self.FOUR)}}
                                   if argv[1:3] == ["pane", "layout"] else inventory)
            with mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
                 mock.patch.object(peer_steward, "_pane_has_agent", return_value="pane-occupied"):
                self.assertIsNone(peer_steward._start_placement("w1:p0"))
            self.assertEqual([a[1:3] for a in calls], [["pane", "layout"], ["tab", "list"]])

    def test_one_two_three_fill_the_grid_even_when_the_reference_is_in_the_other_column(self):
        for rects, expected in ((self.FULL, ("split", "w1:p0", "right")),
                                (self.TWO, ("split", "w1:p1", "down")),
                                (self.THREE, ("split", "w1:p0", "down")),
                                ([(10, 2, 100, 50), (10, 52, 100, 50), (110, 2, 100, 100)],
                                 ("split", "w1:p2", "down"))):
            with self.subTest(rects=rects):
                self.assertEqual(self.decision(self.layout(rects)), expected)

    def test_horizontal_rows_fill_the_equivalent_grid(self):
        for rects, target in (([(10, 2, 200, 50), (10, 52, 200, 50)], "w1:p0"),
                              ([(10, 2, 100, 50), (10, 52, 200, 50), (110, 2, 100, 50)], "w1:p1"),
                              ([(10, 2, 200, 50), (10, 52, 100, 50), (110, 52, 100, 50)], "w1:p0")):
            self.assertEqual(self.decision(self.layout(rects)), ("split", target, "right"))

    def test_odd_terminal_dimensions_use_the_actual_half_cells(self):
        rects = [(23, 1, 138, 76), (161, 1, 137, 38), (161, 39, 137, 38)]
        self.assertEqual(self.decision(self.layout(rects, (23, 1, 275, 76))), ("split", "w1:p0", "down"))

    def test_user_resized_dividers_still_fill_the_same_tab(self):
        # The user drags column and row dividers; a 0.465 split is still the same grid.
        for rects, area, expected in (
                ([(0, 0, 125, 76), (125, 0, 143, 76)], (0, 0, 268, 76), ("split", "w1:p1", "down")),
                ([(10, 2, 93, 100), (103, 2, 107, 40), (103, 42, 107, 60)], None, ("split", "w1:p0", "down")),
                ([(10, 2, 140, 30), (10, 32, 140, 70), (150, 2, 60, 100)], None, ("split", "w1:p2", "down")),
                ([(10, 2, 200, 30), (10, 32, 200, 70)], None, ("split", "w1:p0", "right"))):
            with self.subTest(rects=rects):
                self.assertEqual(self.decision(self.layout(rects, area)), expected)
        # Two different row dividers are not one grid.
        uneven = [(10, 2, 100, 30), (10, 32, 100, 70), (110, 2, 100, 60)]
        self.assertEqual(self.decision(self.layout(uneven)), ("tab", "w1", None))

    def test_full_tab_reuses_only_an_empty_foreground_shell_otherwise_creates_a_tab(self):
        layout = self.layout(self.FOUR)
        self.assertEqual(self.decision(layout), ("tab", "w1", None))
        self.assertEqual(self.decision(layout, {"w1:p3"}), ("reuse", "w1:p3", None))
        self.assertEqual(self.decision(layout, {"w1:p0"}), ("reuse", "w1:p0", None))
        for state, prompt in (("pane-busy", True), ("pane-unknown", True), (None, False)):
            self.assertEqual(self.decision(layout, {"w1:p3"}, state, prompt), ("tab", "w1", None))

    def test_irregular_or_overfull_tab_never_splits(self):
        for rects in ([(10, 2, 50, 100), (60, 2, 50, 100), (110, 2, 100, 100)],
                      self.FOUR + [(60, 52, 50, 50)]):
            self.assertEqual(self.decision(self.layout(rects)), ("tab", "w1", None))

    def test_missing_duplicate_or_foreign_layout_never_allocates(self):
        missing = self.layout(self.FULL)
        missing["panes"][0]["pane_id"] = "w1:pElse"
        duplicate = self.layout(self.TWO)
        duplicate["panes"][1]["pane_id"] = "w1:p0"
        foreign = self.layout(self.FOUR)
        foreign["workspace_id"] = "w2"
        for layout in (None, {}, missing, duplicate, foreign):
            self.assertIsNone(self.decision(layout))

    def test_layout_read_failure_does_not_start_or_type(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json({"error": {"code": "pane_not_found"}})) as run, \
             mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["start", "grid", "--kind", "codex", "--beside", "w1:p0",
                                               "--cwd", str(self.tmp_root)]), 1)
        self.assertEqual(len(run.call_args_list), 1)
        self.assertIn("reason=pane-layout-unavailable", printed.call_args[0][0])

    def test_concurrent_starts_in_the_same_workspace_hold_the_claim_through_launch(self):
        self.assert_serialized(None, None)

    def test_implicit_and_explicit_default_session_share_the_workspace_claim(self):
        for first_session, second_session in ((None, "default"), ("default", None)):
            with self.subTest(first=first_session, second=second_session):
                self.assert_serialized(first_session, second_session)

    def test_project_start_and_beside_start_share_the_same_workspace_claim(self):
        self.assert_serialized(None, None, project=True)

    def assert_serialized(self, first_session, second_session, project=False):
        import multiprocessing
        ctx = multiprocessing.get_context("fork")
        first_entered, release, second_entered = ctx.Event(), ctx.Event(), ctx.Event()
        def launch(args):
            if args.name == "first":
                first_entered.set()
                release.wait(5)
            else:
                second_entered.set()
            return 0
        first_args = peer_steward.build_parser().parse_args(["start", "first", "--kind", "codex", "--beside", "w1:p0"])
        if project:
            first_args.beside = None
        second_args = peer_steward.build_parser().parse_args(["start", "second", "--kind", "claude", "--beside", "w1:p1"])
        def start(args, session):
            peer_steward._HERDR_SESSION = session
            peer_steward.cmd_start(args)
        with mock.patch.object(peer_steward, "_start_in_pane", side_effect=launch), \
             mock.patch.object(peer_steward, "_caller_pane", return_value=""), \
             mock.patch.object(peer_steward, "_project_start_workspace", return_value="w1"):
            first = ctx.Process(target=start, args=(first_args, first_session))
            second = ctx.Process(target=start, args=(second_args, second_session))
            first.start()
            try:
                self.assertTrue(first_entered.wait(5))
                second.start()
                self.assertFalse(second_entered.wait(.1))
                release.set()
                self.assertTrue(second_entered.wait(5))
            finally:
                release.set()
                for child in (first, second):
                    if child.pid:
                        child.join(5)
                        if child.is_alive():
                            child.terminate()
                            child.join(5)
            self.assertEqual((first.exitcode, second.exitcode), (0, 0))

    def test_each_harness_uses_the_decision_in_one_start_including_overflow_and_shell_reuse(self):
        for kind in ("claude", "codex", "opencode"):
            for rects, free, action, target, direction in (
                    (self.FULL, None, "split", "w1:p0", "right"),
                    (self.TWO, None, "split", "w1:p1", "down"),
                    (self.THREE, None, "split", "w1:p0", "down"),
                    (self.FOUR, "w1:p3", "reuse", "w1:p3", None),
                    (self.FOUR, None, "tab", "w1", None)):
                with self.subTest(kind=kind, action=action, target=target):
                    calls = []
                    def run(argv, **kwargs):
                        calls.append(argv)
                        if argv[1:3] == ["pane", "layout"]:
                            return _herdr_json({"result": {"layout": self.layout(rects)}})
                        if argv[1:3] == ["tab", "list"]:
                            return _herdr_json({"result": {"tabs": []}})
                        if argv[1:3] in (["pane", "split"], ["tab", "create"]):
                            return _herdr_json({"result": {"pane" if action == "split" else "root_pane":
                                                          {"pane_id": "w1:pNew", "focused": False}}})
                        if argv[1:3] == ["agent", "start"]:
                            return _herdr_json({"result": {"agent": {"agent": kind, "name": "grid"}}})
                        raise AssertionError(argv)
                    with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
                         mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
                         mock.patch.object(peer_steward, "_pane_has_agent", side_effect=lambda p: None if p == free else "pane-occupied"), \
                         mock.patch.object(peer_steward, "_pane_foreground_shell", return_value=None), \
                         mock.patch.object(peer_steward, "_wait_for_shell_prompt", return_value=True), \
                         mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
                         mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
                         mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
                         mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="ready"), \
                         mock.patch.object(peer_steward, "_export_opencode_tui_scoped", return_value=None), \
                         mock.patch.object(peer_steward, "_process_session", return_value=None), \
                         mock.patch.object(peer_steward, "_read_screen", return_value=None), \
                         mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), mock.patch("builtins.print"):
                        self.assertEqual(peer_steward.main(["start", "grid", "--kind", kind,
                                                           "--beside", "w1:p0", "--cwd", str(self.tmp_root)]), 0)
                    launch = next(a for a in calls if a[1:3] == ["agent", "start"])
                    self.assertEqual(launch[1:3], ["agent", "start"])
                    self.assertEqual(launch[launch.index("--pane") + 1], target if action == "reuse" else "w1:pNew")
                    if action == "reuse":
                        self.assertEqual(sum(a[1:3] == ["agent", "start"] for a in calls), 1)
                    else:
                        create = next(a for a in calls if a[1:3] in (["pane", "split"], ["tab", "create"]))
                        self.assertIn("--no-focus", create)
                        if action == "split":
                            self.assertEqual(create[create.index("--pane") + 1], target)
                            self.assertEqual(create[create.index("--direction") + 1], direction)
                            self.assertEqual(create[create.index("--ratio") + 1], "0.5")
                        else:
                            self.assertEqual(create[create.index("--workspace") + 1], "w1")


class ProjectStartTest(_TmpRootMixin, unittest.TestCase):
    """An unattached caller can place a peer without claiming a terminal seat."""

    def launch(self, kind="codex", *, free=None, foreign=False, missing=False):
        calls, lines = [], []
        pane = {"pane_id": "w1:pHint", "workspace_id": "w1",
                "foreground_cwd": str(self.tmp_root / "other" if foreign else self.tmp_root)}
        if foreign:
            (self.tmp_root / "other").mkdir(exist_ok=True)
        panes = [{"pane_id": "w1:pHint", "workspace_id": "w1"},
                 {"pane_id": "w1:pEmpty", "workspace_id": "w1"}]

        def run(argv, **kwargs):
            calls.append(argv)
            action = argv[1:3]
            if action == ["agent", "get"]:
                return _herdr_json({"result": {"agent": None if missing else pane}})
            if action == ["pane", "list"]:
                return _herdr_json({"result": {"panes": panes}})
            if action == ["tab", "create"]:
                return _herdr_json({"result": {"root_pane": {"pane_id": "w1:pNew", "focused": False}}})
            if action == ["agent", "start"]:
                return _herdr_json({"result": {"agent": {"agent": kind, "name": "project-peer"}}})
            raise AssertionError(argv)

        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pHint"}), \
             mock.patch.object(peer_steward, "_caller_pane", return_value=""), \
             mock.patch.object(peer_steward.os, "getcwd", return_value=str(self.tmp_root)), \
             mock.patch.object(peer_steward.INSTALL_PATHS, "primary_checkout", side_effect=lambda cwd: Path(cwd)), \
             mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_pane_has_agent", side_effect=lambda p: None if p == free else "pane-occupied"), \
             mock.patch.object(peer_steward, "_pane_foreground_shell", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_shell_prompt", return_value=True), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="ready"), \
             mock.patch.object(peer_steward, "_export_opencode_tui_scoped", return_value=None), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), \
             mock.patch.object(peer_steward, "_mark_seat_successor") as successor, \
             mock.patch("builtins.print", side_effect=lambda line: lines.append(line)):
            rc = peer_steward.main(["start", "project-peer", "--kind", kind])
        successor.assert_not_called()
        return rc, calls, lines

    def test_unproven_attachment_starts_in_an_unfocused_project_tab_on_every_harness(self):
        for kind in ("claude", "codex", "opencode"):
            with self.subTest(kind=kind):
                rc, calls, lines = self.launch(kind)
                self.assertEqual(rc, 0, lines)
                create = next(a for a in calls if a[1:3] == ["tab", "create"])
                self.assertEqual(create[create.index("--workspace") + 1], "w1")
                self.assertIn("--no-focus", create)
                self.assertEqual(create[create.index("--cwd") + 1], str(self.tmp_root))
                self.assertFalse(any(a[1:3] == ["pane", "split"] for a in calls))
                self.assertIn("placement=project-workspace", lines[-1])

    def test_empty_shell_is_reused_without_touching_the_hinted_pane(self):
        rc, calls, lines = self.launch(free="w1:pEmpty")
        self.assertEqual(rc, 0, lines)
        self.assertFalse(any(a[1:3] == ["tab", "create"] for a in calls))
        start = next(a for a in calls if a[1:3] == ["agent", "start"])
        self.assertEqual(start[start.index("--pane") + 1], "w1:pEmpty")

    def test_foreign_or_missing_workspace_hint_never_allocates_or_types(self):
        for options in ({"foreign": True}, {"missing": True}):
            with self.subTest(options=options):
                rc, calls, lines = self.launch(**options)
                self.assertEqual(rc, 1, lines)
                self.assertEqual([a[1:3] for a in calls], [["agent", "get"]])
                self.assertIn("reason=pane-unknown", lines[-1])

    def test_project_match_includes_a_real_linked_worktree_subdirectory(self):
        primary, linked = self.tmp_root / "primary", self.tmp_root / "linked"
        for argv in (["git", "init", "-q", str(primary)],
                     ["git", "-C", str(primary), "-c", "user.name=Fixture", "-c",
                      "user.email=fixture@example.invalid", "commit", "-q", "--allow-empty", "-m", "fixture"],
                     ["git", "-C", str(primary), "worktree", "add", "-q", "-b", "linked", str(linked)]):
            subprocess.run(argv, check=True, capture_output=True, text=True)
        nested = linked / "nested"
        nested.mkdir()
        pane = {"pane_id": "w1:pHint", "workspace_id": "w1", "foreground_cwd": str(primary)}
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pHint"}), \
             mock.patch.object(peer_steward.os, "getcwd", return_value=str(nested)), \
             mock.patch.object(peer_steward, "_placement_result", return_value={"agent": pane}):
            self.assertEqual(peer_steward._project_start_workspace(), "w1")
            pane["foreground_cwd"] = str(self.tmp_root)
            self.assertIsNone(peer_steward._project_start_workspace())
            del pane["foreground_cwd"]
            self.assertIsNone(peer_steward._project_start_workspace())

    def test_unreadable_duplicate_or_foreign_workspace_inventory_never_allocates(self):
        own = {"pane_id": "w1:pEmpty", "workspace_id": "w1"}
        for panes in (None, [], [own, own], [{**own, "workspace_id": "w2"}], [{}]):
            with self.subTest(panes=panes), \
                 mock.patch.object(peer_steward, "_placement_result", return_value={"panes": panes}), \
                 mock.patch.object(peer_steward, "_empty_shell_placement") as empty:
                self.assertIsNone(peer_steward._start_workspace_placement("w1"))
                empty.assert_not_called()


class BesideStartTest(_TmpRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # These tests own startup/readiness; GridStartTest owns live layout selection.
        placement = mock.patch.object(peer_steward, "_start_placement",
                                      side_effect=lambda pane: ("split", pane, "right"))
        placement.start()
        self.addCleanup(placement.stop)
        self.resolve_primary = peer_steward.INSTALL_PATHS.primary_checkout
        patch = mock.patch.object(peer_steward.INSTALL_PATHS, "primary_checkout",
                                  side_effect=lambda cwd: Path(cwd))
        self.primary_mock = patch.start()
        self.addCleanup(patch.stop)

    def test_default_cwd_resolves_a_real_linked_worktree_subfolder_to_primary(self):
        primary, linked = self.tmp_root / "primary", self.tmp_root / "linked"
        native_run = subprocess.run
        for argv in (["git", "init", "-q", str(primary)],
                     ["git", "-C", str(primary), "-c", "user.name=Fixture", "-c",
                      "user.email=fixture@example.invalid", "commit", "-q", "--allow-empty", "-m", "fixture"],
                     ["git", "-C", str(primary), "worktree", "add", "-q", "-b", "linked", str(linked)]):
            native_run(argv, check=True, capture_output=True, text=True)
        subfolder = linked / "nested"
        subfolder.mkdir()
        self.primary_mock.side_effect = self.resolve_primary
        calls = []

        def run(argv, **kw):
            if argv[0] == "git":
                return native_run(argv, **kw)
            calls.append(argv)
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "agent", "start"]:
                self.assertEqual(kw["cwd"], str(primary))
                return _herdr_json({"result": {"agent": {"agent": "codex", "name": "new"}}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.os, "getcwd", return_value=str(subfolder)), \
             mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="ready"), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), \
             mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "codex", "--beside", "w1:pOld"]), 0)
        self.assertEqual(calls[0][-2:], ["--cwd", str(primary)])
        self.assertEqual(calls[1][-2:], ["--cd", str(primary)])
        self.assertIn("cwd=" + str(primary), printed.call_args[0][0])

    def test_opencode_late_bootstrap_waits_for_stable_shell_before_one_native_start(self):
        clock, events = {"now": 0.0}, []

        def export(pane):
            events.append("export")
            return "exported"

        def snapshot(pane, shell, deadline=None):
            self.assertEqual(deadline, peer_steward._BESIDE_READY_SECONDS)
            events.append("snapshot")
            return "settled" if clock["now"] >= .05 else None

        def run(argv, **kw):
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "agent", "start"]:
                self.assertGreaterEqual(clock["now"], .05)
                events.append("native start")
                return _herdr_json({"result": {"agent": {"agent": "opencode", "name": "new"}}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_export_opencode_tui_scoped", side_effect=export), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", side_effect=snapshot), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: clock["now"]), \
             mock.patch.object(peer_steward.time, "sleep", side_effect=lambda n: clock.update(now=clock["now"] + n)), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "opencode", "--beside", "w1:pOld",
                                               "--cwd", str(self.tmp_root)]), 0)
        self.assertEqual(events, ["export", "snapshot", "snapshot", "native start"])
        self.assertIn("started=true", printed.call_args[0][0])
        self.primary_mock.assert_not_called()

    def test_unstable_bootstrap_uses_original_deadline_and_offers_retained_pane_reuse(self):
        import shlex
        clock = {"now": 0.0}
        split = _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=split) as run, \
             mock.patch.object(peer_steward, "_BESIDE_READY_SECONDS", .1), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_export_opencode_tui_scoped", return_value="exported"), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value=None), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: clock["now"]), \
             mock.patch.object(peer_steward.time, "sleep", side_effect=lambda n: clock.update(now=clock["now"] + n)), \
             mock.patch.object(peer_steward, "_close_pane") as close, mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "opencode", "--beside", "w1:pOld",
                                               "--cwd", str(self.tmp_root), "--permission-mode", "inherit",
                                               "--", "--model", "user/model"]), 1)
        self.assertEqual(clock["now"], .1)
        self.assertEqual(run.call_count, 1)
        close.assert_not_called()
        line = printed.call_args[0][0]
        self.assertIn("reason=beside-shell-readiness-timeout", line)
        self.assertIn("pane_cleanup=retained", line)
        reuse = next(v.split("=", 1)[1] for v in shlex.split(line) if v.startswith("reuse_command="))
        self.assertEqual(shlex.split(reuse), ["hearting", "run", "peer-steward", "start", "new",
                         "--kind", "opencode", "--pane", "w1:pN", "--cwd", str(self.tmp_root),
                         "--permission-mode", "inherit", "--", "--model", "user/model"])

    def test_same_tab_right_no_focus_split_reuses_start_with_new_pane_and_cwd(self):
        calls = []
        cwd = str(self.tmp_root)

        def run(argv, **kw):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False,
                                                          "tab_id": "w1:tA"}}})
            if argv[:3] == ["herdr", "agent", "start"]:
                return _herdr_json({"result": {"agent": {"agent": "claude", "name": "new",
                    "pane_id": "w1:pN", "agent_session": {"value": "new-native-sid"}}}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None) as ingress, \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="recorded screen"), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["start", "new", "--kind", "claude", "--beside", "w1:pOld",
                                   "--cwd", cwd, "--permission-mode", "inherit", "--", "--model", "opus"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0], ["herdr", "pane", "split", "--pane", "w1:pOld", "--direction",
                                   "right", "--ratio", "0.5", "--no-focus", "--cwd", cwd])
        self.assertEqual(calls[1], ["herdr", "agent", "start", "new", "--kind", "claude",
                                   "--pane", "w1:pN", "--", "--model", "opus"])
        ingress.assert_called_once_with("w1:pN", "claude", None)
        self.assertIn("started=true", printed.call_args[0][0])
        self.assertIn("pane=w1:pN", printed.call_args[0][0])

    def test_beside_waits_for_delayed_split_cwd_before_single_start(self):
        calls, events = [], []
        cwd = str(self.tmp_root)
        clock = {"now": 0.0}

        def run(argv, **kw):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "split"]:
                self.assertEqual(argv[-2:], ["--cwd", cwd])
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "pane", "get"]:
                return _herdr_json({"result": {"pane": {"agent": None}}})
            if argv[:3] == ["herdr", "pane", "process-info"]:
                busy = clock["now"] < .2
                return _herdr_json({"result": {"process_info": {"pane_id": "w1:pN",
                    "shell_pid": 101, "foreground_process_group_id": 202 if busy else 101,
                    "foreground_processes": [{"pid": 202 if busy else 101}]}}})
            if argv[:3] == ["herdr", "pane", "wait-output"]:
                self.assertGreaterEqual(clock["now"], .2)
                events.append("ready prompt")
                return subprocess.CompletedProcess(argv, 0, stdout="prompt", stderr="")
            if argv[:3] == ["herdr", "agent", "start"]:
                self.assertGreaterEqual(clock["now"], .2)
                self.assertIn("snapshot", events)
                events.append("native start")
                return _herdr_json({"result": {"agent": {"agent": "claude", "name": "new",
                    "pane_id": "w1:pN", "agent_session": {"value": "new-native-sid"}}}})
            raise AssertionError(argv)

        def snapshot(pane, shell, deadline=None):
            self.assertGreaterEqual(clock["now"], .2)
            self.assertEqual((pane, shell), ("w1:pN", (101, "700")))
            events.append("snapshot")
            return "ready screen"

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_managed_ingress_dir", return_value=None), \
             mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward, "_proc_cwd", side_effect=lambda pid:
                               cwd if clock["now"] >= .2 else "/previous"), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: clock["now"]), \
             mock.patch.object(peer_steward.time, "sleep", side_effect=lambda seconds:
                               clock.update(now=clock["now"] + seconds)), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", side_effect=snapshot), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), \
             mock.patch.object(peer_steward, "_close_pane") as close, mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["start", "new", "--kind", "claude", "--beside", "w1:pOld",
                                   "--cwd", cwd, "--permission-mode", "inherit"])
        self.assertEqual(rc, 0)
        self.assertIn("started=true", printed.call_args[0][0])
        self.assertEqual(sum(a[:3] == ["herdr", "agent", "start"] for a in calls), 1)
        self.assertFalse(any(a[:3] in (["herdr", "pane", "send-text"],
                                     ["herdr", "pane", "send-keys"]) for a in calls))
        self.assertEqual(events[-2:], ["snapshot", "native start"])
        close.assert_not_called()

    def test_slow_nas_prompt_finishes_in_the_same_start_call_on_every_harness(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                clock, calls = {"now": 0.0}, []
                cwd = str(self.tmp_root)

                def run(argv, **kw):
                    calls.append(list(argv))
                    if argv[:3] == ["herdr", "pane", "split"]:
                        return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
                    if argv[:3] == ["herdr", "pane", "get"]:
                        return _herdr_json({"result": {"pane": {"agent": None}}})
                    if argv[:3] == ["herdr", "pane", "process-info"]:
                        processes = [{"pid": 101, "argv": ["zsh"]}]
                        if clock["now"] < 240:
                            processes.append({"pid": 202, "argv": ["git", "status"]})
                        return _herdr_json({"result": {"process_info": {"pane_id": "w1:pN",
                            "shell_pid": 101, "foreground_process_group_id": 101,
                            "foreground_processes": processes}}})
                    if argv[:3] == ["herdr", "pane", "wait-output"]:
                        self.assertGreaterEqual(clock["now"], 240)
                        return subprocess.CompletedProcess(argv, 0, stdout="prompt", stderr="")
                    if argv[:3] == ["herdr", "agent", "start"]:
                        self.assertEqual(argv[7], "w1:pN")
                        self.assertGreaterEqual(clock["now"], 240)
                        return _herdr_json({"result": {"agent": {"agent": harness, "name": "new"}}})
                    raise AssertionError(argv)

                with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
                     mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
                     mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
                     mock.patch.object(peer_steward, "_proc_cwd", return_value=cwd), \
                     mock.patch.object(peer_steward, "_managed_ingress_dir", return_value=None), \
                     mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
                     mock.patch.object(peer_steward, "_export_opencode_tui_scoped", return_value="missing"), \
                     mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="stable prompt"), \
                     mock.patch.object(peer_steward, "_read_screen", return_value=None), \
                     mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), \
                     mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: clock["now"]), \
                     mock.patch.object(peer_steward.time, "sleep", side_effect=lambda seconds:
                                       clock.update(now=clock["now"] + max(seconds, 5))), \
                     mock.patch.object(peer_steward, "_close_pane") as close, mock.patch("builtins.print") as printed:
                    rc = peer_steward.main(["start", "new", "--kind", harness, "--beside", "w1:pOld",
                                           "--cwd", cwd, "--permission-mode", "inherit"])
                self.assertEqual(rc, 0)
                self.assertIn("started=true", printed.call_args[0][0])
                self.assertEqual(sum(a[:3] == ["herdr", "agent", "start"] for a in calls), 1)
                self.assertFalse(any(a[:3] in (["herdr", "pane", "send-text"],
                                             ["herdr", "pane", "send-keys"]) for a in calls))
                self.assertLess(clock["now"], 300)
                close.assert_not_called()

    def test_slow_shell_replacement_still_refuses_launch(self):
        info = {"pane_id": "w1:pN", "shell_pid": 101,
                "foreground_process_group_id": 101,
                "foreground_processes": [{"pid": 101}, {"pid": 202}]}
        with mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value=info), \
             mock.patch.object(peer_steward, "_proc_start_ticks", side_effect=["700", "701"]), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=None), \
             mock.patch.object(peer_steward.time, "sleep"), \
             mock.patch.object(peer_steward, "_wait_for_shell_prompt") as prompt:
            reason, shell = peer_steward._wait_for_created_shell(
                "w1:pN", "/fixture", (101, "700"), time.monotonic() + 300)
        self.assertEqual((reason, shell), ("beside-shell-changed", (101, "700")))
        prompt.assert_not_called()

    def test_beside_readiness_deadline_retains_pane_without_start_or_snapshot(self):
        clock = {"now": 0.0}
        calls = []
        cwd = str(self.tmp_root)

        def run(argv, **kw):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "pane", "get"]:
                return _herdr_json({"result": {"pane": {"agent": None}}})
            if argv[:3] == ["herdr", "pane", "process-info"]:
                return _herdr_json({"result": {"process_info": {"pane_id": "w1:pN",
                    "shell_pid": 101, "foreground_process_group_id": 101,
                    "foreground_processes": [{"pid": 101}]}}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_BESIDE_READY_SECONDS", .1), \
             mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward, "_proc_cwd", return_value="/previous"), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: clock["now"]), \
             mock.patch.object(peer_steward.time, "sleep", side_effect=lambda seconds:
                               clock.update(now=clock["now"] + seconds)), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress") as ingress, \
             mock.patch.object(peer_steward, "_start_shell_snapshot") as snapshot, \
             mock.patch.object(peer_steward, "_close_pane") as close, mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["start", "new", "--kind", "claude", "--beside", "w1:pOld",
                                   "--cwd", cwd, "--permission-mode", "inherit"])
        self.assertEqual(rc, 1)
        self.assertIn("started=false reason=beside-shell-readiness-timeout", printed.call_args[0][0])
        self.assertIn("pane=w1:pN", printed.call_args[0][0])
        self.assertIn("pane_cleanup=retained", printed.call_args[0][0])
        self.assertEqual(clock["now"], .1)
        self.assertFalse(any(a[:3] == ["herdr", "agent", "start"] for a in calls))
        ingress.assert_not_called(); snapshot.assert_not_called(); close.assert_not_called()

    def test_split_failure_malformed_focused_or_same_pane_never_starts_or_types(self):
        cases = [subprocess.CompletedProcess([], 1, stdout="", stderr="denied"),
                 subprocess.CompletedProcess([], 0, stdout="null", stderr=""),
                 _herdr_json({"error": {"code": "pane_not_found"}}),
                 _herdr_json({"result": {"pane": {"pane_id": "w1:pOld", "focused": False}}}),
                 _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": True}}}),
                 _herdr_json({"result": {"pane": {"pane_id": "w1:pN"}}})]
        for response in cases:
            with self.subTest(response=response), \
                 mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=response) as run, \
                 mock.patch.object(peer_steward, "_ensure_pane_ingress") as ingress, \
                 mock.patch("builtins.print") as printed:
                rc = peer_steward.main(["start", "new", "--kind", "codex", "--beside", "w1:pOld"])
                self.assertEqual(rc, 1)
                self.assertEqual(run.call_count, 1)
                ingress.assert_not_called()
                self.assertIn("started=false reason=pane-split-failed", printed.call_args[0][0])

    def test_start_with_no_pane_named_opens_beside_the_calling_pane_in_the_calling_cwd(self):
        response = _herdr_json({"error": {"code": "pane_not_found"}})   # stop right after the split
        with tempfile.TemporaryDirectory() as cwd, \
             mock.patch.dict(peer_steward.os.environ, {"HERDR_PANE_ID": "w1:pMe"}), \
             mock.patch.object(peer_steward.os, "getcwd", return_value=cwd), \
             mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=response) as run, \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "codex"]), 1)
            split = run.call_args_list[0][0][0]
            self.assertEqual(split[:5], ["herdr", "pane", "split", "--pane", "w1:pMe"])
            self.assertEqual(split[split.index("--cwd") + 1], os.path.realpath(cwd))
        self.assertEqual(self.resolve_primary(cwd), Path(cwd))

    def test_start_outside_a_herdr_pane_with_no_pane_named_says_so_and_runs_nothing(self):
        env = {k: v for k, v in os.environ.items() if k != "HERDR_PANE_ID"}
        with mock.patch.dict(peer_steward.os.environ, env, clear=True), \
             mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run") as run, \
             mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "claude"]), 1)
        run.assert_not_called()
        self.assertIn("started=false reason=pane-unknown", printed.call_args[0][0])

    def test_non_json_start_failure_has_bounded_reason_and_closes_only_owned_empty_split(self):
        calls = []

        def run(argv, **kw):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "agent", "start"]:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="\x1b[31mError: name too long\x1b[0m\n" + "secret second line")
            if argv[:3] == ["herdr", "pane", "close"]:
                return _herdr_json({"result": {"type": "ok"}})
            if argv[:3] == ["herdr", "pane", "process-info"]:
                return _herdr_json({"result": {"process_info": {"pane_id": "w1:pN", "shell_pid": 101,
                    "foreground_process_group_id": 101, "foreground_processes": [{"pid": 101}]}}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="recorded screen"), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
             mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward, "_start_pane_screen", return_value="recorded screen"), \
             mock.patch("builtins.print") as printed:
            peer_steward.main(["start", "too-long", "--kind", "claude", "--beside", "w1:pOld"])
        self.assertIn("started=false", printed.call_args[0][0])
        self.assertIn("reason=herdr-stderr-error-name-too-long", printed.call_args[0][0])
        self.assertIn("pane_cleanup=closed", printed.call_args[0][0])
        self.assertNotIn("secret", printed.call_args[0][0])
        self.assertEqual(calls[-1], ["herdr", "pane", "close", "w1:pN"])
        self.assertFalse(any(a[:3] == ["herdr", "pane", "close"] and a[-1] == "w1:pOld" for a in calls))

    def test_failed_cleanup_refuses_agent_changed_shell_unknown_or_draft(self):
        for occupied, shell, prompt in (("pane-occupied", (101, "700"), True),
                                        ("pane-unknown", (101, "700"), True),
                                        (None, (101, "701"), True),
                                        (None, None, True), (None, (101, "700"), False)):
            clock = iter(range(20))
            with self.subTest(occupied=occupied, shell=shell, prompt=prompt), \
                 mock.patch.object(peer_steward, "_pane_has_agent", return_value=occupied), \
                 mock.patch.object(peer_steward, "_start_shell_identity", return_value=shell), \
                 mock.patch.object(peer_steward, "_proc_start_ticks", return_value="701" if shell == (101, "701") else "700"), \
                 mock.patch.object(peer_steward, "_retire_pane_info", return_value=None if shell is None else {"shell_pid": 101}), \
                 mock.patch.object(peer_steward, "_start_pane_screen", return_value="recorded screen" if prompt else "changed screen"), \
                 mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
                 mock.patch.object(peer_steward.time, "sleep"), \
                 mock.patch.object(peer_steward, "_close_pane") as close:
                self.assertEqual(peer_steward._failed_start_cleanup("w1:pN", (101, "700"), "recorded screen"), "retained")
                close.assert_not_called()

    def test_existing_pane_failure_never_cleans_up_and_stderr_is_bounded(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_created_shell") as readiness, \
             mock.patch.object(peer_steward.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 1, stdout="", stderr="Error: duplicate name\n")) as run, \
             mock.patch.object(peer_steward, "_failed_start_cleanup") as cleanup, mock.patch("builtins.print") as printed:
            peer_steward.main(["start", "old", "--kind", "claude", "--pane", "w1:pOld"])
            self.assertIn("reason=herdr-stderr-error-duplicate-name", printed.call_args[0][0])
            cleanup.assert_not_called(); self.assertEqual(run.call_count, 2)  # one observation, one start
            readiness.assert_not_called()
        code = peer_steward._start_stderr_code("Error: " + "long\tvalue\x00" * 100 + "\n")
        self.assertLessEqual(len(code.encode()), 80)
        self.assertRegex(code, r"^[a-z0-9_-]+$")

    def test_failed_fresh_start_waits_for_same_shell_then_closes_once(self):
        clock = iter(range(20))
        with mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
             mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value={"shell_pid": 101}), \
             mock.patch.object(peer_steward, "_start_shell_identity", side_effect=[None, (101, "700"), (101, "700")]), \
             mock.patch.object(peer_steward, "_start_pane_screen", return_value="recorded screen"), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(peer_steward.time, "sleep") as pause, \
             mock.patch.object(peer_steward, "_close_pane", return_value=True) as close:
            self.assertEqual(peer_steward._failed_start_cleanup("w1:pN", (101, "700"), "recorded screen"), "closed")
            pause.assert_called_once()
            close.assert_called_once_with("w1:pN")

    def test_fresh_snapshot_requires_two_stable_nonblank_reads_and_same_shell(self):
        cases = [(["recorded screen", "recorded screen"], "recorded screen"),
                 (["", "recorded screen", "recorded screen"], "recorded screen"),
                 ([""] * 100, None), ([str(i) for i in range(100)], None), ([None], None)]
        for screens, expected in cases:
            clock = iter(i / 100 for i in range(1000))
            with self.subTest(screens=screens[:3]), \
                 mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
                 mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
                 mock.patch.object(peer_steward, "_start_pane_screen", side_effect=screens), \
                 mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
                 mock.patch.object(peer_steward.time, "sleep"):
                self.assertEqual(peer_steward._start_shell_snapshot("w1:pN", (101, "700")), expected)
        with mock.patch.object(peer_steward, "_pane_has_agent", return_value="pane-unknown"), \
             mock.patch.object(peer_steward, "_start_pane_screen") as read:
            self.assertIsNone(peer_steward._start_shell_snapshot("w1:pN", (101, "700")))
            read.assert_not_called()

    def test_visible_screen_is_verbatim_and_unavailable_read_is_unknown(self):
        for stdout, rc, expected in (("user@host $ ", 0, "user@host $ "),
                ("\x1b[0mhost  draft", 0, "\x1b[0mhost  draft"), ("", 0, ""),
                ("denied", 1, None), ("x" * 65537, 0, None), ('{"error":{"code":"denied"}}', 0, None)):
            with self.subTest(rc=rc, length=len(stdout)), mock.patch.object(peer_steward.subprocess,
                    "run", return_value=subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")) as run:
                self.assertEqual(peer_steward._start_pane_screen("w1:pN"), expected)
                self.assertEqual(run.call_args[0][0], ["herdr", "pane", "read", "w1:pN",
                    "--source", "visible", "--format", "ansi"])

    def test_post_record_drafts_and_redraw_never_close_owned_split(self):
        plain = "user@host $ "
        powerline = "user@host  /work  main  "
        styled = "\x1b[0m\x1b[38;5;2m\x1b[0m "
        cases = [(plain, "user@host $ echo $ "), ("user@host % ", "user@host % printf % "),
                 ("user@host ❯ ", "user@host ❯ : # "), (plain, "user@host $echo $ "),
                 ("%", "%printf % "), ("❯", "❯: # "),
                 (powerline, powerline + "echo " + styled),
                 (powerline, powerline + "printf " + styled),
                 (plain, "redrawn prompt"), (plain, None)]
        for recorded, current in cases:
            with self.subTest(current=current), \
                 mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
                 mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
                 mock.patch.object(peer_steward, "_retire_pane_info", return_value={"shell_pid": 101}), \
                 mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
                 mock.patch.object(peer_steward, "_start_pane_screen", return_value=current), \
                 mock.patch.object(peer_steward, "_close_pane", return_value=True) as close:
                self.assertEqual(peer_steward._failed_start_cleanup("w1:pN", (101, "700"), recorded), "retained")
                close.assert_not_called()
        with mock.patch.object(peer_steward, "_close_pane") as close:
            self.assertEqual(peer_steward._failed_start_cleanup("w1:pN", (101, "700"), None), "retained")
            close.assert_not_called()

    def test_beside_records_stable_screen_before_start_and_retains_later_change(self):
        events = []
        screens = iter(["before", "before", "after draft"])
        clock = iter(i / 100 for i in range(1000))

        def read(pane):
            value = next(screens); events.append("screen:" + value); return value

        def run(argv, **kw):
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "agent", "start"]:
                events.append("native start")
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="start rejected")
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value={"shell_pid": 101}), \
             mock.patch.object(peer_steward, "_start_pane_screen", side_effect=read), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(peer_steward.time, "sleep"), \
             mock.patch.object(peer_steward, "_close_pane") as close, mock.patch("builtins.print") as printed:
            peer_steward.main(["start", "new", "--kind", "claude", "--beside", "w1:pOld"])
            self.assertIn("pane_cleanup=retained", printed.call_args[0][0])
            close.assert_not_called()
        self.assertEqual(events, ["screen:before", "screen:before", "native start", "screen:after draft"])

    def test_invalid_cwd_does_not_split_and_pane_choices_are_exclusive(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run") as run, mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "codex", "--beside", "w1:pOld",
                                               "--cwd", str(self.tmp_root / "missing")]), 1)
        run.assert_not_called()
        with mock.patch.object(peer_steward.sys, "stderr"), self.assertRaises(SystemExit):
            peer_steward.build_parser().parse_args(["start", "new", "--kind", "codex", "--pane", "w1:pA",
                                                   "--beside", "w1:pB"])


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


class RetireTest(_TmpRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # The fixture PIDs (101 shell, 4242 agent) are plain numbers, not live
        # processes: on a host where either belongs to another user, reading its
        # /proc environ fails and the pane looks unverifiable.
        patcher = mock.patch.object(peer_steward.peer_obligations,
                                    "execution_attempts_for_processes", return_value=())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_retire_subject_treats_only_same_birth_same_namespace_zombie_as_gone(self):
        def stat_row(state, start):
            fields = [state, *(["0"] * 18), start]
            return "4242 (opencode) " + " ".join(fields)

        def readlink(path):
            return "pid:[same]" if str(path) in {"/proc/4242/ns/pid", "/proc/self/ns/pid"} else ""

        with mock.patch.object(peer_steward.Path, "read_text", return_value=stat_row("Z", "800")), \
             mock.patch.object(peer_steward.os, "readlink", side_effect=readlink):
            self.assertTrue(peer_steward._retire_zombie_matches(4242, "800"))
            self.assertFalse(peer_steward._retire_zombie_matches(4242, "801"))

        def foreign_namespace(path):
            return "pid:[foreign]" if str(path) == "/proc/4242/ns/pid" else "pid:[same]"

        with mock.patch.object(peer_steward.Path, "read_text", return_value=stat_row("Z", "800")), \
             mock.patch.object(peer_steward.os, "readlink", side_effect=foreign_namespace):
            self.assertFalse(peer_steward._retire_zombie_matches(4242, "800"))

        duty = {"intent": {"identity": {"foreground": {"pid": 4242, "start": "800"}}}}
        with mock.patch.object(peer_steward, "_retire_process_record", return_value=None), \
             mock.patch.object(peer_steward, "_retire_zombie_matches", return_value=True):
            self.assertEqual(peer_steward._retire_subject_state(duty), "gone")

    def test_exact_opencode_native_status_overrides_stale_herdr_state(self):
        world = _RetireWorld(harness="opencode", status="working")
        native = {"status": "idle"}
        with mock.patch.object(peer_steward.herdr_observation, "read_native_status",
                               return_value=native) as read_status:
            rc, line = self.retire(world)
        self.assertEqual(rc, 0, line)
        self.assertIn("retired=true reason=normal-exit", line)
        self.assertEqual(world.actions(), [
            ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
            ["herdr", "pane", "close", "w1:pOld"],
        ])
        self.assertTrue(any(call.args[0:4] == ("default", "w1:pOld", "opencode", "old-sid")
                            and call.args[4:] == (4242, "800")
                            for call in read_status.call_args_list))

    def test_exact_opencode_native_busy_status_overrides_stale_herdr_idle(self):
        world = _RetireWorld(harness="opencode", status="idle")
        with mock.patch.object(peer_steward.herdr_observation, "read_native_status",
                               return_value={"status": "working"}):
            rc, line = self.retire(world)
        self.assertEqual(rc, 0, line)
        self.assertIn("reason=agent-working-opencode-session-status", line)
        self.assertIn("pending=true", line)
        self.assertEqual(world.actions(), [])

    def test_normal_retire_requests_the_exact_watch_terminal_receipt(self):
        root = peer_steward._watch_root()
        root.mkdir(parents=True, exist_ok=True)
        watch_id = "retire-watch"
        arm = {"schema_version": 1, "watch_id": watch_id, "target": "old",
               "server": "default", "armed_ts": "then", "agent": {
                   "harness": "opencode", "session_id": "old-sid", "name": "old",
                   "pane": "w1:pOld"}, "foreground_identity": {"pid": 4242, "start": "800"},
               "steward": {}, "watcher": {}, "refs": []}
        paths = peer_steward._watch_paths(watch_id, root)
        paths.arm.write_text(json.dumps(arm), encoding="utf-8")
        lock_fd = peer_steward._open_lock(paths.lock)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            with mock.patch.object(peer_steward.herdr_observation, "read_native_status",
                                   return_value={"status": "idle"}):
                rc, line = self.retire(_RetireWorld(harness="opencode", status="working"))
            self.assertEqual(rc, 0, line)
            self.assertEqual(json.loads(paths.observation.read_text())["state"], "terminal_pending")
            self.assertFalse(paths.receipt.exists())
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        self.assertTrue(peer_steward._settle_terminal_watch_if_unobserved(
            watch_id, arm, root))
        receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
        self.assertEqual((receipt["state"], receipt["reason"]),
                         ("pane-exited", "completed-retire"))

    def test_current_runner_leaves_retire_untouched_while_legacy_runner_is_live(self):
        world = _RetireWorld(status="working")
        self.retire(world)
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.list()[0]
        world.status = "idle"
        with (store.root / "runner.lock").open("w") as legacy, \
             (store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME).open("w") as current:
            fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(current, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(peer_steward, "_resume_retire_obligation") as resume, \
                 mock.patch.object(peer_steward.time, "sleep", side_effect=InterruptedError("fixture")):
                with self.assertRaises(InterruptedError):
                    peer_steward.cmd_obligation_runner(
                        SimpleNamespace(state_root=self.tmp_root, lock_fd=current.fileno()))
                resume.assert_not_called()
        self.assertEqual(store.get(duty["id"]), duty)
        self.assertEqual(world.actions(), [])
        self._run_retire_runner(world)
        self.assertEqual(store.get(duty["id"])["state"], "complete")

    def _run_retire_runner(self, world, crash_phase=None):
        store = peer_steward.peer_obligations.ObligationStore()
        lock_fd = os.open(store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        original_stat = os.stat
        clock = iter(range(100))

        def stat(path, *args, **kwargs):
            if str(path) == "/proc/4242":
                if world.sent and world.exits:
                    raise FileNotFoundError(path)
                return object()
            return original_stat(path, *args, **kwargs)

        def run(argv, **kwargs):
            if crash_phase == "exit-requested" and world.sent and argv[:3] == ["herdr", "pane", "process-info"]:
                raise RuntimeError("fixture observation interrupted after exit send")
            if crash_phase == "shell-returned" and argv[:3] == ["herdr", "pane", "close"]:
                raise RuntimeError("fixture cleanup observation interrupted")
            return world.run(argv, **kwargs)

        try:
            with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
                 mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
                 mock.patch.object(peer_steward, "_retire_process_record",
                                   return_value={"start": "800", "group": 4242, "argv": [world.harness]}), \
                 mock.patch.object(peer_steward, "_proc_start_ticks",
                                   side_effect=lambda pid: "700" if pid == 101 else "800"), \
                 mock.patch.object(peer_steward.os, "stat", side_effect=stat), \
                 mock.patch.object(peer_steward, "_seat_handover", return_value=None), \
                 mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
                 mock.patch.object(peer_steward.time, "sleep",
                                   side_effect=InterruptedError("fixture runner stopped") if crash_phase else None), \
                 mock.patch("builtins.print"):
                if crash_phase:
                    with self.assertRaises(InterruptedError):
                        peer_steward.cmd_obligation_runner(
                            SimpleNamespace(state_root=self.tmp_root, lock_fd=lock_fd))
                else:
                    self.assertEqual(peer_steward.cmd_obligation_runner(
                        SimpleNamespace(state_root=self.tmp_root, lock_fd=lock_fd)), 0)
        finally:
            os.close(lock_fd)

    def test_observer_error_preserves_exit_phase_and_restart_only_finishes_cleanup(self):
        for phase in ("exit-requested", "shell-returned"):
            with self.subTest(phase=phase):
                world = _RetireWorld(status="working")
                rc, _line = self.retire(world)
                self.assertEqual(rc, 0)
                store = peer_steward.peer_obligations.ObligationStore()
                duty_id = store.list()[0]["id"]
                world.status = "idle"
                self._run_retire_runner(world, crash_phase=phase)
                interrupted = store.get(duty_id)
                self.assertEqual(interrupted["state"], "unknown")
                self.assertEqual(interrupted["observation"]["phase"], phase)
                self.assertEqual(interrupted["observation"]["foreground"]["pid"], 4242)
                self.assertEqual(interrupted["observation"]["reason"], "observer-unavailable")
                self._run_retire_runner(world)
                settled = store.get(duty_id)
                self.assertEqual((settled["state"], settled["result"], settled["cleanup"]),
                                 ("complete", "normal-exit", "complete"))
                self.assertEqual(world.actions(), [
                    ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
                    ["herdr", "pane", "close", "w1:pOld"],
                ])

    def test_stale_runner_observation_cannot_undo_foreground_exit_claim_or_completion(self):
        original = peer_steward._pane_readiness
        for exits in (False, True):
            with self.subTest(exits=exits):
                world = _RetireWorld(status="working", exits=exits)
                self.retire(world)
                store = peer_steward.peer_obligations.ObligationStore()
                duty_id = store.list()[0]["id"]
                raced = []
                def readiness(*args, **kwargs):
                    stale = original(*args, **kwargs)
                    if not raced:
                        raced.append(True)
                        world.status = "idle"
                        with mock.patch.object(peer_steward, "_pane_readiness", original):
                            self.retire(world, clear_existing=False, resume_request_id=duty_id)
                    return stale
                with mock.patch.object(peer_steward, "_pane_readiness", side_effect=readiness):
                    if exits:
                        self._run_retire_runner(world)
                    else:
                        self._run_retire_runner(world, crash_phase="checkpoint")
                duty = store.get(duty_id)
                if exits:
                    self.assertEqual((duty["state"], duty["observation"]["phase"]), ("complete", "complete"))
                else:
                    self.assertEqual(duty["observation"]["phase"], "exit-requested")
                    self.assertEqual(duty["observation"]["foreground"]["pid"], 4242)
                    world.exits = True
                    self._run_retire_runner(world)
                    self.assertEqual(store.get(duty_id)["state"], "complete")
                self.assertEqual(world.actions(), [["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
                                                   ["herdr", "pane", "close", "w1:pOld"]])

    def test_finalizing_retire_is_accepted_then_resumed_once(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness, status="finalizing")
                rc, line = self.retire(world)
                self.assertEqual(rc, 0)
                self.assertIn("reason=agent-finalizing", line)
                self.assertIn("pending=true", line)
                self.assertEqual(world.actions(), [])
                store = peer_steward.peer_obligations.ObligationStore()
                duty = store.list()[0]
                self.assertEqual(duty["state"], "pending")
                world.status = "idle"
                self._run_retire_runner(world)
                settled = store.get(duty["id"])
                self.assertEqual((settled["state"], settled["result"], settled["cleanup"]),
                                 ("complete", "normal-exit", "complete"))
                exits = ([
                    ["herdr", "pane", "send-text", "w1:pOld", "/exit"],
                    ["herdr", "pane", "send-keys", "w1:pOld", "enter"],
                ] if harness == "claude" else [
                    ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
                ])
                self.assertEqual(world.actions(), exits + [["herdr", "pane", "close", "w1:pOld"]])

    def test_idle_retire_with_bound_pending_or_unknown_work_sends_no_exit(self):
        from dispatch_contract import ObservedAttemptLiveness
        meta = ("attempt_id=att-bound,parent_sid=old-sid,"
                "parent_harness=codex")
        self.jobs_path.write_text(f"2026-10-09T00:00:00Z\topen\trepo\t-\tslug\t{meta}\n")
        for state, reason in (("live", "bound-registered-work-pending"),
                              ("unverifiable", "bound-registered-work-unknown")):
            with self.subTest(process_state=state), mock.patch(
                    "dispatch_contract.observed_attempt_liveness",
                    return_value=ObservedAttemptLiveness(
                        "alive" if state == "live" else "unverifiable", "fixture", state, "fixture")):
                world = _RetireWorld(status="idle")
                rc, line = self.retire(world)
                self.assertEqual(rc, 0)
                self.assertIn("reason=" + reason, line)
                self.assertIn("pending=true", line)
                self.assertEqual(world.actions(), [])

    def test_retire_uses_bounded_agent_wait_and_one_final_process_observation(self):
        world = _RetireWorld(harness="opencode", status="idle", exits=False)
        with mock.patch.object(peer_steward, "_OPENCODE_RETIRE_SECONDS", 0.01):
            rc, line = self.retire(world)
        self.assertEqual(rc, 0, line)
        self.assertIn("reason=agent-still-running", line)
        sent_at = next(index for index, argv in enumerate(world.calls)
                       if argv[:3] == ["herdr", "pane", "send-keys"])
        post_send_reads = [argv for argv in world.calls[sent_at + 1:]
                           if argv[:3] == ["herdr", "pane", "process-info"]]
        self.assertEqual(len(post_send_reads), 1)
        wait_args = next(argv for argv in world.calls if argv[:3] == ["herdr", "agent", "wait"])
        wait_states = [wait_args[index + 1] for index, value in enumerate(wait_args[:-1]) if value == "--until"]
        self.assertEqual(wait_states, ["done"])

    def retire(self, world, record=None, *, clear_existing=True, resume_request_id=None):
        printed = []
        store = peer_steward.peer_obligations.ObligationStore()
        if clear_existing:
            # Every row here belongs to this test's isolated temporary root.
            for path in store.root.glob("retire-*.json"):
                path.unlink()
        original_stat = os.stat
        clock = iter(range(100))

        def stat(path, *a, **kw):
            if str(path) == "/proc/4242" and world.sent and world.exits:
                raise FileNotFoundError(path)
            if str(path) == "/proc/4242":
                return object()
            return original_stat(path, *a, **kw)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run), \
             mock.patch.object(peer_steward, "_retire_process_record", return_value=(record if record is not None else
                              {"start": "800", "group": 4242, "argv": [world.harness]})), \
             mock.patch.object(peer_steward, "_proc_start_ticks", side_effect=lambda pid: "700" if pid == 101 else "800"), \
             mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner", return_value=False), \
             mock.patch.object(peer_steward.os, "stat", side_effect=stat), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(peer_steward.time, "sleep"), \
             mock.patch("builtins.print", side_effect=lambda *a, **kw: printed.append(" ".join(map(str, a)))):
            rc = (peer_steward.cmd_retire(SimpleNamespace(target="old", _resume_request_id=resume_request_id))
                  if resume_request_id else peer_steward.main(["retire", "old"]))
        return rc, printed[-1]

    def test_busy_retire_persists_then_task_runner_finishes_without_replaying_exit(self):
        world = _RetireWorld(status="working")
        rc, line = self.retire(world)
        self.assertEqual(rc, 0)
        self.assertIn("pending=true", line)
        self.assertEqual(world.actions(), [])
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.list()[0]
        self.assertEqual(duty["intent"]["kind"], "retire")
        self.assertEqual(duty["state"], "pending")

        world.status = "idle"
        lock_path = store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        original_stat = os.stat
        clock = iter(range(100))

        def stat(path, *args, **kwargs):
            if str(path) == "/proc/4242" and world.sent and world.exits:
                raise FileNotFoundError(path)
            if str(path) == "/proc/4242":
                return object()
            return original_stat(path, *args, **kwargs)

        try:
            with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
                 mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run), \
                 mock.patch.object(peer_steward, "_retire_process_record",
                                   return_value={"start": "800", "group": 4242,
                                                 "argv": [world.harness]}), \
                 mock.patch.object(peer_steward, "_proc_start_ticks",
                                   side_effect=lambda pid: "700" if pid == 101 else "800"), \
                 mock.patch.object(peer_steward.os, "stat", side_effect=stat), \
                 mock.patch.object(peer_steward, "_seat_handover", return_value=None), \
                 mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
                 mock.patch.object(peer_steward.time, "sleep"), \
                 mock.patch("builtins.print"):
                self.assertEqual(peer_steward.cmd_obligation_runner(
                    SimpleNamespace(state_root=self.tmp_root, lock_fd=lock_fd)), 0)
        finally:
            os.close(lock_fd)

        settled = store.get(duty["id"])
        self.assertEqual((settled["state"], settled["result"], settled["cleanup"]),
                         ("complete", "normal-exit", "complete"))
        self.assertEqual(world.actions(), [
            ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
            ["herdr", "pane", "close", "w1:pOld"],
        ])
        before = list(world.actions())
        rc, line = self.retire(world, clear_existing=False, resume_request_id=duty["id"])
        self.assertEqual(rc, 0)
        self.assertIn("reason=already-complete", line)
        self.assertEqual(world.actions(), before)

    def test_pane_before_its_first_session_retires_by_its_pane(self):
        """2026-10-10 regression: a Codex that never took input has no session
        in herdr, and its retire stayed `pane-identity-unverified` for good."""
        world = _RetireWorld(sid=None)
        with mock.patch.object(peer_steward, "_proven_session", return_value=None):
            rc, line = self.retire(world)
        self.assertEqual((rc, line), (0, "retired=true reason=normal-exit agent=codex name=old pane=w1:pOld"))
        self.assertEqual(world.actions(), [
            ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
            ["herdr", "pane", "close", "w1:pOld"],
        ])

    def test_pending_sessionless_retire_never_exits_a_later_process(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness, sid=None, status="working")
                with mock.patch.object(peer_steward, "_proven_session", return_value=None):
                    self.retire(world)
                    store = peer_steward.peer_obligations.ObligationStore()
                    duty = store.list()[0]
                    world.status = "idle"
                    rc, line = self.retire(world, clear_existing=False,
                        resume_request_id=duty["id"],
                        record={"start": "900", "group": 4242, "argv": [harness]})
                self.assertEqual(rc, 0)
                self.assertIn("reason=target-already-gone", line)
                self.assertEqual(world.actions(), [])
                settled = store.get(duty["id"])
                self.assertEqual((settled["state"], settled["result"], settled["cleanup"]),
                                 ("complete", "target-already-gone", "complete"))

    def test_runner_closes_gone_subject_even_when_pane_has_a_new_session(self):
        for reused in (False, True):
            with self.subTest(pid_reused=reused):
                world = _RetireWorld(sid=None, status="working")
                self.retire(world)
                store = peer_steward.peer_obligations.ObligationStore()
                duty = store.list()[0]
                original_stat = os.stat

                def stat(path, *args, **kwargs):
                    if str(path) == "/proc/4242":
                        raise FileNotFoundError(path)
                    return original_stat(path, *args, **kwargs)

                record = {"start": "900", "group": 4242, "argv": ["codex"]} if reused else None
                with mock.patch.object(peer_steward, "_retire_process_record", return_value=record), \
                     mock.patch.object(peer_steward.os, "stat", side_effect=stat), \
                     mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run), \
                     mock.patch.object(peer_steward, "_seat_handover") as handover:
                    world.sid, world.status = "new-sid", "idle"
                    peer_steward._resume_retire_obligation(duty, store)
                    peer_steward._resume_retire_obligation(store.get(duty["id"]), store)
                self.assertEqual(world.actions(), [])
                handover.assert_not_called()
                self.assertEqual(store.get(duty["id"])["result"], "target-already-gone")

    def test_unreadable_or_missing_original_birth_never_adopts_current_process(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                world = _RetireWorld(sid=None, status="working")
                self.retire(world)
                store = peer_steward.peer_obligations.ObligationStore()
                duty = store.list()[0]
                if legacy:
                    duty["intent"]["identity"].pop("foreground")
                original_stat = os.stat

                def stat(path, *args, **kwargs):
                    if str(path) == "/proc/4242":
                        raise PermissionError(path)
                    return original_stat(path, *args, **kwargs)

                with mock.patch.object(peer_steward, "_retire_process_record", return_value=None), \
                     mock.patch.object(peer_steward, "_proc_start_ticks", return_value=None), \
                     mock.patch.object(peer_steward.os, "stat", side_effect=stat), \
                     mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run):
                    world.status = "idle"
                    peer_steward._resume_retire_obligation(duty, store)
                self.assertEqual(world.actions(), [])
                self.assertEqual(store.get(duty["id"])["state"], "unknown")

    def test_busy_subject_is_booked_with_children_without_authorizing_busy_exit(self):
        world = _RetireWorld(sid=None, status="working")
        original_info = world.info

        def info():
            value = original_info()
            if world.status == "working":
                value["foreground_processes"].append({"pid": 4243, "argv": ["sleep"]})
            return value

        world.info = info
        self.retire(world)
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.list()[0]
        self.assertEqual(duty["intent"]["identity"]["foreground"]["start"], "800")
        self.assertEqual(world.actions(), [])
        world.status = "idle"
        self.retire(world, clear_existing=False, resume_request_id=duty["id"])
        self.assertEqual(store.get(duty["id"])["result"], "normal-exit")

    def test_new_explicit_sessionless_retire_has_its_own_process_identity(self):
        for sid in (None, "same-resumed-sid"):
            with self.subTest(sid=sid):
                world = _RetireWorld(sid=sid, status="working")
                self.retire(world)
                store = peer_steward.peer_obligations.ObligationStore()
                first = store.list()[0]
                self.retire(world, clear_existing=False,
                            record={"start": "900", "group": 4242, "argv": ["codex"]})
                duties = store.list()
                self.assertEqual(len(duties), 2)
                self.assertEqual({row["intent"]["identity"]["foreground"]["start"] for row in duties}, {"800", "900"})
                self.assertEqual(store.get(first["id"])["intent"], first["intent"])
                self.assertEqual(world.actions(), [])

    def test_start_tick_proves_reuse_when_argv_is_unreadable(self):
        world = _RetireWorld(sid=None, status="working")
        self.retire(world)
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.list()[0]
        with mock.patch.object(peer_steward, "_retire_process_record", return_value=None), \
             mock.patch.object(peer_steward, "_proc_start_ticks", return_value="900"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run):
            peer_steward._resume_retire_obligation(duty, store)
        self.assertEqual(world.actions(), [])
        self.assertEqual(store.get(duty["id"])["result"], "target-already-gone")

    def test_successor_after_exit_or_before_close_finishes_without_closing_its_pane(self):
        for phase in ("exit-requested", "shell-returned"):
            with self.subTest(phase=phase):
                world = _RetireWorld(sid=None, status="working")
                self.retire(world)
                store = peer_steward.peer_obligations.ObligationStore()
                duty = store.list()[0]
                store.claim_phase(duty["id"], {"waiting"}, phase,
                                  extra={"foreground": duty["intent"]["identity"]["foreground"]})
                world.sid, world.status = "new-sid", "idle"
                with mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run), \
                     mock.patch.object(peer_steward, "_retire_process_record",
                                       return_value={"start": "900", "group": 4242, "argv": ["codex"]}), \
                     mock.patch.object(peer_steward, "_proc_start_ticks",
                                       side_effect=lambda pid: "700" if pid == 101 else "900"), \
                     mock.patch.object(peer_steward, "_seat_handover") as handover:
                    peer_steward._resume_retire_obligation(store.get(duty["id"]), store)
                self.assertEqual(world.actions(), [])
                handover.assert_not_called()
                self.assertEqual(store.get(duty["id"])["result"], "target-already-gone")

    def test_pane_whose_process_proves_a_session_herdr_lacks_retires_by_that_session(self):
        """2026-10-10 recurrence: Codex 0.162 opens its thread before any input,
        so the process proves a session herdr was not told yet. That session is
        the pane's own: retire proceeds, and work bound to it still holds it."""
        from dispatch_contract import ObservedAttemptLiveness
        meta = "attempt_id=att-bound,parent_sid=thread-real,parent_harness=codex"
        self.jobs_path.write_text(f"2026-10-09T00:00:00Z\topen\trepo\t-\tslug\t{meta}\n")
        world = _RetireWorld(sid=None)
        with mock.patch.object(peer_steward, "_proven_session", return_value="thread-real"), \
             mock.patch("dispatch_contract.observed_attempt_liveness",
                        return_value=ObservedAttemptLiveness("alive", "fixture", "live", "fixture")):
            rc, line = self.retire(world)
        self.assertIn("retired=false reason=bound-registered-work-pending", line)
        self.assertEqual(world.actions(), [])
        self.jobs_path.write_text("")
        with mock.patch.object(peer_steward, "_proven_session", return_value="thread-real"):
            rc, line = self.retire(world, clear_existing=False)
        self.assertEqual((rc, line), (0, "retired=true reason=normal-exit agent=codex name=old pane=w1:pOld"))
        self.assertEqual(world.actions(), [
            ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
            ["herdr", "pane", "close", "w1:pOld"],
        ])

    def test_repeated_retire_retries_its_waiting_duty_now(self):
        world = _RetireWorld(status="working")
        rc, line = self.retire(world)
        self.assertEqual(rc, 0)
        self.assertIn("retired=false reason=agent-working", line)
        world.status = "idle"
        rc, line = self.retire(world, clear_existing=False)
        self.assertEqual((rc, line), (0, "retired=true reason=normal-exit agent=codex name=old pane=w1:pOld"))
        self.assertEqual(len(peer_steward.peer_obligations.ObligationStore().list()), 0)
        self.assertEqual(world.actions(), [
            ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
            ["herdr", "pane", "close", "w1:pOld"],
        ])

    def test_cancelled_retire_accepts_one_fresh_request_and_preserves_history(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness, status="working")
                self.retire(world)
                store = peer_steward.peer_obligations.ObligationStore()
                first = store.list()[0]
                cancelled = store.update(first["id"], state="cancelled", cleanup="complete",
                                         observation={"phase": "cancelled", "reason": "self-target"})
                with mock.patch.object(peer_steward, "_current_session_identity", return_value=("new-requester", "codex")):
                    rc, line = self.retire(world, clear_existing=False)
                    self.assertEqual(rc, 0)
                    self.assertIn("reason=agent-working", line)
                    fresh = store.list()[0]
                    self.assertNotEqual(fresh["id"], first["id"])
                    self.assertEqual(fresh["intent"]["retry_of"], first["id"])
                    self.assertEqual(fresh["intent"]["requester"]["session_id"], "new-requester")
                    self.retire(world, clear_existing=False)
                    self.assertEqual([d["id"] for d in store.list()], [fresh["id"]])
                    world.status = "idle"
                    rc, line = self.retire(world, clear_existing=False)
                self.assertEqual(rc, 0)
                self.assertIn("retired=true reason=normal-exit", line)
                self.assertEqual(store.get(first["id"]), cancelled)
                self.assertEqual(store.get(fresh["id"])["result"], "normal-exit")
                self.assertEqual(sum(a[:3] == ["herdr", "pane", "close"] for a in world.actions()), 1)

    def test_runner_in_target_environment_uses_recorded_requester(self):
        for harness in ("claude", "codex", "opencode"):
            for legacy in (False, True):
                with self.subTest(harness=harness, legacy=legacy):
                    world = _RetireWorld(harness=harness, status="working")
                    with mock.patch.object(peer_steward, "_current_session_identity", return_value=("requester-sid", "claude")), \
                         mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pCaller"}):
                        self.retire(world)
                    store = peer_steward.peer_obligations.ObligationStore()
                    duty = store.list()[0]
                    if legacy:
                        duty["intent"]["requester"].pop("pane", None)
                        duty["intent"]["requester"].pop("server", None)
                        peer_steward.peer_obligations._write_atomic(store._record_path(duty["id"]), duty)
                    world.status = "idle"
                    with mock.patch.object(peer_steward, "_current_session_identity", return_value=(world.sid, harness)), \
                         mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pOld"}):
                        self._run_retire_runner(world)
                    settled = store.get(duty["id"])
                    self.assertEqual((settled["state"], settled["result"]), ("complete", "normal-exit"))
                    self.assertEqual(sum(a[:3] == ["herdr", "pane", "close"] for a in world.actions()), 1)

    def test_resumed_self_target_is_refused_despite_different_runner(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness, status="working")
                self.retire(world)
                store = peer_steward.peer_obligations.ObligationStore()
                duty = store.list()[0]
                duty["intent"]["requester"] = {"harness": harness, "session_id": world.sid}
                peer_steward.peer_obligations._write_atomic(store._record_path(duty["id"]), duty)
                world.status = "idle"
                with mock.patch.object(peer_steward, "_current_session_identity", return_value=("runner-sid", "claude")), \
                     mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pOther"}):
                    self._run_retire_runner(world)
                self.assertEqual(store.get(duty["id"])["state"], "cancelled")
                self.assertEqual(world.actions(), [])

    def test_cancelled_resume_stays_cancelled_without_creating_new_request(self):
        world = _RetireWorld(status="working")
        self.retire(world)
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.list()[0]
        cancelled = store.update(duty["id"], state="cancelled", cleanup="complete",
                                 observation={"phase": "cancelled", "reason": "self-target"})
        world.status = "idle"
        rc, line = self.retire(world, clear_existing=False, resume_request_id=duty["id"])
        self.assertEqual(rc, 1)
        self.assertIn("reason=retire-cancelled", line)
        self.assertEqual(store.get(duty["id"]), cancelled)
        self.assertEqual(world.actions(), [])

    def test_confirmed_normal_exit_closes_once_and_records_notice(self):
        for harness, status in (("codex", "idle"), ("opencode", "done"), ("claude", "idle")):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness, status=status)
                rc, line = self.retire(world)
                self.assertEqual((rc, line), (0, f"retired=true reason=normal-exit agent={harness} name=old pane=w1:pOld"))
                expected = ([["herdr", "pane", "send-text", "w1:pOld", "/exit"],
                             ["herdr", "pane", "send-keys", "w1:pOld", "enter"]] if harness == "claude"
                            else [["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"]])
                self.assertEqual(world.actions(), expected + [["herdr", "pane", "close", "w1:pOld"]])
                row = self._all_records()[-1]
                self.assertEqual((row["kind"], row["to"]["pane"], row["delivery"]["receipt"]),
                                 ("notice", "w1:pOld", "normal-exit"))

    def test_a_retire_from_the_pane_started_beside_hands_the_routes_on(self):
        # RA-2 (decision 5f0f10): beside -> ACK -> retire moves the parent role, across harnesses.
        handed_line = "retired=true reason=normal-exit agent=codex name=old pane=w1:pOld"
        def run(mark_beside, mark_sid):
            peer_steward._mark_seat_successor("w1:pNew", mark_beside, "claude", mark_sid)
            with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pNew"}), \
                 mock.patch.object(peer_steward, "_current_session_identity", return_value=("new-sid", "claude")), \
                 mock.patch("dispatch_seat_handover.record_retire_handover",
                            return_value={"bindings": [{}, {}]}) as handed:
                rc, line = self.retire(_RetireWorld(harness="codex"))
            return rc, line, handed
        rc, line, handed = run("w1:pOld", "new-sid")
        self.assertEqual((rc, line), (0, handed_line + " handover=2"))
        handed.assert_called_once_with("old-sid", "codex", "new-sid", "claude", env=mock.ANY,
                                      successor_seat=mock.ANY)
        self.assertEqual(handed.call_args.kwargs["successor_seat"].pane, "w1:pNew")
        self.assertFalse(peer_steward._seat_successor_path("w1:pNew").exists())      # used once
        rc, line, handed = run("w1:pElse", "new-sid")                               # not beside this one
        self.assertEqual((rc, line), (0, handed_line))
        handed.assert_not_called()
        rc, line, handed = run("w1:pOld", "someone-else")                           # another session's pane
        self.assertEqual((rc, line), (0, handed_line + " handover=skipped:successor-unverified"))
        handed.assert_not_called()

    def test_prompt_git_helpers_after_exit_are_not_live_agents(self):
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness)
                normal_info = world.info

                def prompt_info():
                    value = normal_info()
                    if world.sent:
                        value["foreground_processes"] += [
                            {"pid": 102, "argv": ["zsh"]},
                            {"pid": 103, "argv": ["git", "status", "--porcelain",
                                                  "--ignore-submodules=dirty"]},
                        ]
                    return value

                world.info = prompt_info
                rc, line = self.retire(world)
                self.assertEqual(rc, 0)
                self.assertIn("retired=true reason=normal-exit", line)
                self.assertEqual(world.actions()[-1], ["herdr", "pane", "close", "w1:pOld"])

    def test_only_opencode_waits_for_a_slow_normal_exit(self):
        for harness in ("opencode", "codex", "claude"):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness, exits=False)
                if harness == "opencode":
                    transports = []

                    class EventTransport:
                        def __init__(self, *_args, **_kwargs):
                            self._state = SimpleNamespace(complete=True)
                            self.snapshot_count = 0
                            transports.append(self)

                        def observe(self):
                            return SimpleNamespace(endpoint="default", complete=True, panes=())

                        def resnapshot(self):
                            self.snapshot_count += 1
                            return SimpleNamespace(endpoint="default", complete=True, panes=())

                        def wait_for_event(self, pane_id, _timeout, event_types=None):
                            self.assert_target = pane_id
                            self.assert_types = event_types
                            world.exits = True
                            return {"event": "pane.closed", "data": {"pane_id": pane_id}}

                        def close(self):
                            pass

                    with mock.patch.object(peer_steward.herdr_observation,
                                           "socket_path_for_endpoint", return_value="/fixture.sock"), \
                         mock.patch.object(peer_steward.herdr_observation,
                                           "ObservationTransport", EventTransport):
                        rc, line = self.retire(world)
                else:
                    rc, line = self.retire(world)
                if harness == "opencode":
                    self.assertEqual(rc, 0)
                    self.assertIn("retired=true reason=normal-exit", line)
                    self.assertEqual(transports[0].snapshot_count, 1)
                    self.assertEqual(world.actions(), [
                        ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
                        ["herdr", "pane", "close", "w1:pOld"],
                    ])
                else:
                    self.assertEqual(rc, 0)
                    self.assertIn("retired=false reason=agent-still-running", line)
                    expected = ([
                        ["herdr", "pane", "send-text", "w1:pOld", "/exit"],
                        ["herdr", "pane", "send-keys", "w1:pOld", "enter"],
                    ] if harness == "claude" else [
                        ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
                    ])
                    self.assertEqual(world.actions(), expected)

    def test_opencode_still_running_is_bounded_without_another_exit_key(self):
        world = _RetireWorld(harness="opencode", exits=False)
        rc, line = self.retire(world)
        self.assertEqual(rc, 0)
        self.assertIn("retired=false reason=agent-still-running", line)
        self.assertEqual(world.actions(), [
            ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
        ])

    def test_busy_form_draft_unknown_self_or_changed_target_receives_no_exit(self):
        cases = [(_RetireWorld(status=s), "agent-" + s) for s in ("working", "blocked", "unknown")]
        cases += [(_RetireWorld(screen=s), reason) for s, reason in
                  ((CODEX_DRAFT, "draft"), (CODEX_POPUP, "draft-unknown"), (NO_BOX, "draft-unknown"),
                   (CLAUDE_FORM, "form-open"), ("", "screen-unavailable"))]
        cases += [(_RetireWorld("claude", screen=CLAUDE_DRAFT_SECOND_LINE), "draft"),
                  (_RetireWorld("opencode", screen=OPENCODE_DRAFT), "draft")]
        changed = _RetireWorld(); changed.changed = True
        cases.append((changed, "pane-identity-unverified"))
        for world, reason in cases:
            with self.subTest(reason=reason):
                rc, line = self.retire(world)
                self.assertEqual(rc, 0)
                self.assertIn("reason=" + reason, line)
                self.assertEqual(world.actions(), [])
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pOld"}):
            world = _RetireWorld(); rc, line = self.retire(world)
            self.assertIn("reason=self-target", line); self.assertEqual(world.actions(), [])

    def test_unverified_or_foreign_foreground_receives_no_exit(self):
        for patch in ({"pane_id": "w1:pForeign"}, {"shell_pid": True},
                      {"foreground_process_group_id": 999}, {"foreground_processes": []},
                      {"foreground_processes": [{"pid": 4242, "argv": ["other"]}]}):
            with self.subTest(patch=patch):
                world = _RetireWorld(); world.bad_process = patch
                rc, line = self.retire(world)
                self.assertIn("reason=", line); self.assertEqual(world.actions(), [])
        world = _RetireWorld()
        rc, line = self.retire(world, {"start": "800", "group": 999, "argv": ["codex"]})
        self.assertIn("reason=", line); self.assertEqual(world.actions(), [])

    def test_claude_exit_command_unknown_or_still_live_never_closes_or_retries(self):
        # One /exit submission; a missing shell return never permits a retry or close.
        for unknown in (False, True):
            world = _RetireWorld("claude", exits=False); world.post_unknown = unknown
            rc, line = self.retire(world)
            self.assertEqual(rc, 0)
            self.assertIn("reason=" + ("shell-return-unverified" if unknown else "agent-still-running"), line)
            self.assertEqual(world.actions(), [["herdr", "pane", "send-text", "w1:pOld", "/exit"],
                                               ["herdr", "pane", "send-keys", "w1:pOld", "enter"]])

    def test_error_body_on_send_or_close_is_not_success(self):
        world = _RetireWorld(); world.send_error = True
        rc, line = self.retire(world)
        self.assertIn("reason=exit-send-unknown", line)
        self.assertEqual(len(world.actions()), 1)
        world = _RetireWorld(); world.close_error = True
        rc, line = self.retire(world)
        self.assertIn("retired=false reason=pane-close-failed", line)
        self.assertEqual(len(world.actions()), 2)

    def test_claude_failed_exit_text_or_changed_foreground_never_submits_enter(self):
        world = _RetireWorld("claude")
        normal_run = world.run

        def failed_text(argv, **kw):
            if argv[:3] == ["herdr", "pane", "send-text"]:
                world.calls.append(list(argv))
                return _herdr_json({"error": {"code": "denied"}})
            return normal_run(argv, **kw)

        world.run = failed_text
        rc, line = self.retire(world)
        self.assertEqual(rc, 0); self.assertIn("reason=exit-send-unknown", line)
        self.assertEqual(world.actions(), [["herdr", "pane", "send-text", "w1:pOld", "/exit"]])
        world = _RetireWorld("claude")
        normal_info = world.info

        def changed_after_text():
            value = normal_info()
            if any(a[:3] == ["herdr", "pane", "send-text"] for a in world.calls):
                value["foreground_process_group_id"] = 999
            return value

        world.info = changed_after_text
        rc, line = self.retire(world)
        self.assertEqual(rc, 0); self.assertIn("reason=foreground-changed", line)
        self.assertEqual(world.actions(), [["herdr", "pane", "send-text", "w1:pOld", "/exit"]])

    def test_changed_shell_after_exit_is_not_closed(self):
        world = _RetireWorld()
        original_info = world.info

        def changed_after_exit():
            value = original_info()
            if world.sent:
                value["shell_pid"] = 999
            return value

        world.info = changed_after_exit
        rc, line = self.retire(world)
        self.assertIn("retired=false reason=agent-still-running", line)
        self.assertEqual(world.actions(), [["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"]])

    def test_protocol_error_or_missing_herdr_is_typed_and_never_exits(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward, "_run_herdr_get", return_value={"result": {"agent": {
                 "agent": "codex", "agent_status": "idle", "agent_session": "bad"}}}), \
             mock.patch.object(peer_steward.subprocess, "run") as run, mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["retire", "old"]), 1)
            self.assertIn("retired=false reason=herdr-protocol-error", printed.call_args[0][0])
            run.assert_not_called()
        with mock.patch.object(peer_steward.shutil, "which", return_value=None), \
             mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["retire", "old"]), 1)
            self.assertIn("retired=false reason=herdr-not-found", printed.call_args[0][0])

    def test_shell_return_requires_original_birth_and_absent_predecessor(self):
        identity = {"pid": 4242, "start": "800", "shell_pid": 101, "shell_start": "700"}
        info = {"shell_pid": 101, "foreground_process_group_id": 101, "foreground_processes": [{"pid": 101}]}
        with mock.patch.object(peer_steward, "_proc_start_ticks", return_value="701"):
            self.assertFalse(peer_steward._retire_shell_returned(info, identity))
        for error in (None, PermissionError()):
            with mock.patch.object(peer_steward, "_proc_start_ticks", side_effect=lambda pid: "700" if pid == 101 else "800"), \
                 mock.patch.object(peer_steward.os, "stat", return_value=object(), side_effect=error):
                self.assertFalse(peer_steward._retire_shell_returned(info, identity))
        with mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward.os, "stat", side_effect=FileNotFoundError()):
            self.assertTrue(peer_steward._retire_shell_returned(info, identity))

        for birth, expected in (("800", False), ("801", True), (None, False)):
            with self.subTest(birth=birth), \
                 mock.patch.object(peer_steward, "_proc_start_ticks", side_effect=lambda pid: "700" if pid == 101 else birth), \
                 mock.patch.object(peer_steward.os, "stat", return_value=object()):
                self.assertEqual(peer_steward._retire_shell_returned(info, identity), expected)

    def test_kernel_record_refuses_unreadable_namespace_or_reused_birth(self):
        stat = "4242 (codex) " + " ".join(["S", "101", "4242"] + ["0"] * 16 + ["800"])
        for birth, namespaces, read_error in (("800", ["pid:[1]", "pid:[1]"], None),
                ("801", [], None), ("800", ["pid:[2]", "pid:[1]"], None),
                ("800", [], PermissionError())):
            with self.subTest(birth=birth, namespaces=namespaces, read_error=read_error), \
                 mock.patch.object(peer_steward.Path, "read_text", return_value=stat, side_effect=read_error), \
                 mock.patch.object(peer_steward.Path, "read_bytes", return_value=b"codex\0"), \
                 mock.patch.object(peer_steward, "_proc_start_ticks", return_value=birth), \
                 mock.patch.object(peer_steward.os, "readlink", side_effect=namespaces):
                result = peer_steward._retire_process_record(4242)
                if birth == "800" and namespaces == ["pid:[1]", "pid:[1]"]:
                    self.assertEqual(result, {"start": "800", "group": 4242, "argv": ["codex"]})
                else:
                    self.assertIsNone(result)


class DeferredRetireHandoverTest(_TmpRootMixin, unittest.TestCase):
    """The recorded successor inherits; observers and later pane occupants do not."""

    def booking(self, predecessor="claude", successor="opencode"):
        import dispatch_seat_handover as handover
        self.handover = handover
        self.meta = {"attempt_id": "att-original", "dispatch_depth": "1", "worker_type": "owner",
                     "parent_sid": "old-sid", "parent_harness": predecessor,
                     "route_id": "rt-original", "route_hash": "sha256:original", "route_node": "owner"}
        unrelated = {**self.meta, "attempt_id": "att-unrelated", "parent_sid": "other-parent",
                     "route_id": "rt-unrelated", "route_hash": "sha256:unrelated"}
        self.jobs_path.write_text("".join("now\trunning\t0\tjob\tlog\t" +
            ",".join(f"{key}={value}" for key, value in row.items()) + "\n"
            for row in (self.meta, unrelated)))
        self.registry_bytes = self.jobs_path.read_bytes()
        self.unrelated = unrelated
        peer_steward._mark_seat_successor("w1:pNew", "w1:pOld", successor, "")
        requester = {"session_id": "new-sid", "harness": successor,
                     "pane": "w1:pNew", "server": "default"}
        foreground = {"pid": 4242, "start": "800", "shell_pid": 101, "shell_start": "700"}
        identity = {"session_id": "old-sid", "harness": predecessor,
                    "pane": "w1:pOld", "foreground": foreground, "server": "default", "name": "old"}
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.create("retire-fixture", "retire", identity,
                            {"target": "old", "requester": requester})
        return store, duty

    def assert_authority(self, successor="new-sid"):
        self.assertEqual(self.handover.effective_parent(self.meta, self.jobs_path), successor)
        self.assertEqual(self.handover.effective_parent(self.unrelated, self.jobs_path), "other-parent")
        self.assertEqual(self.jobs_path.read_bytes(), self.registry_bytes)

    def test_gone_predecessor_hands_over_from_an_observer_on_every_harness(self):
        import session_tidy
        import pane_ownership
        for predecessor, successor in (("claude", "opencode"), ("opencode", "codex"), ("codex", "claude")):
            with self.subTest(predecessor=predecessor, successor=successor):
                # Each chain has its own isolated state, as independent seats do.
                with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {
                        "XDG_STATE_HOME": root}):
                    store, duty = self.booking(predecessor, successor)
                    with mock.patch.object(peer_steward, "_caller_pane", return_value="w9:pObserver"), \
                         mock.patch.object(peer_steward, "_retire_subject_state", return_value="gone"), \
                         mock.patch.object(pane_ownership, "verified_pane", return_value=""), \
                         mock.patch.object(peer_steward.subprocess, "run") as native:
                        peer_steward._resume_retire_obligation(duty, store)
                        native.assert_not_called()
                    self.assert_authority()
                    settled = store.get(duty["id"])
                    self.assertEqual((settled["state"], settled["result"]), ("complete", "target-already-gone"))
                    seat = session_tidy._pane_seat_of("w1:pNew")
                    before = session_tidy._ledger_path(seat).read_bytes()
                    peer_steward._resume_retire_obligation(settled, store)
                    self.assertEqual(session_tidy._ledger_path(seat).read_bytes(), before)
                    # Keep the next case's immutable duty identity separate.
                    store._record_path(duty["id"]).unlink()

    def test_missing_pane_finishes_each_exit_phase_without_input_or_close(self):
        for phase in ("exit-requested", "shell-returned"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root, \
                 mock.patch.dict(os.environ, {"XDG_STATE_HOME": root}):
                store, duty = self.booking()
                duty = store.claim_phase(duty["id"], {"waiting"}, phase,
                                         extra={"foreground": duty["intent"]["identity"]["foreground"]})
                with mock.patch.object(peer_steward, "_retire_pane_info", return_value=None), \
                     mock.patch.object(peer_steward, "_retire_subject_state", return_value="gone"), \
                     mock.patch.object(peer_steward.subprocess, "run") as native:
                    peer_steward._resume_retire_obligation(duty, store)
                    native.assert_not_called()
                self.assert_authority()
                self.assertEqual(store.get(duty["id"])["state"], "complete")
                store._record_path(duty["id"]).unlink()

    def test_normal_exit_cleanup_uses_the_booked_successor_pane(self):
        store, duty = self.booking()
        foreground = duty["intent"]["identity"]["foreground"]
        info = {"pane_id": "w1:pOld", "shell_pid": 101, "foreground_process_group_id": 101}
        with mock.patch.object(peer_steward, "_caller_pane", return_value="w9:pObserver"), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value=info), \
             mock.patch.object(peer_steward, "_retire_shell_returned", return_value=True), \
             mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward, "_close_pane", return_value=True) as close:
            peer_steward._finish_retire_cleanup(store, duty, duty["intent"]["identity"], foreground)
        close.assert_called_once_with("w1:pOld")
        self.assert_authority()

    def test_completed_legacy_retire_repairs_via_the_same_command(self):
        store, duty = self.booking()
        completed = store.update(duty["id"], state="complete", result="normal-exit",
                                 observation={"phase": "complete"})
        with mock.patch.object(peer_steward, "_retire_target", return_value=("unknown", {
                "harness": "-", "name": "old", "pane": "-", "session_id": "-"}, 1, "pane-unverified")), \
             mock.patch.object(peer_steward, "_current_session_identity", return_value=("new-sid", "opencode")), \
             mock.patch.object(peer_steward, "_caller_pane", return_value="w1:pNew"), \
             mock.patch.object(peer_steward, "_herdr_missing", return_value=False), \
             mock.patch.object(peer_steward.subprocess, "run") as native:
            self.assertEqual(peer_steward.cmd_retire(SimpleNamespace(target="old")), 0)
            native.assert_not_called()
        self.assert_authority()
        self.assertEqual(store.get(duty["id"]), completed)

    def test_detached_lifecycle_repairs_only_the_booked_successor(self):
        store, duty = self.booking()
        store.update(duty["id"], state="complete", result="normal-exit")
        with mock.patch.object(peer_steward, "_current_session_identity", return_value=("observer", "claude")), \
             mock.patch.object(peer_steward, "_caller_pane", return_value=""), \
             mock.patch.object(peer_steward, "_ensure_watch_observers"), \
             mock.patch.object(peer_steward.dispatch_batch_obligations, "ensure_observers"), \
             mock.patch.object(peer_steward.peer_obligations, "ensure_runner", return_value=False):
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)
        self.assert_authority()

    def test_later_requester_cannot_replay_another_sessions_retire(self):
        store, duty = self.booking()
        store.update(duty["id"], state="complete", result="normal-exit")
        with mock.patch.object(peer_steward, "_retire_target", return_value=("unknown", {
                "harness": "-", "name": "old", "pane": "-", "session_id": "-"}, 1, "pane-unverified")), \
             mock.patch.object(peer_steward, "_current_session_identity", return_value=("later-sid", "opencode")), \
             mock.patch.object(peer_steward, "_caller_pane", return_value="w1:pNew"), \
             mock.patch.object(peer_steward, "_herdr_missing", return_value=False):
            self.assertEqual(peer_steward.cmd_retire(SimpleNamespace(target="old")), 1)
        self.assert_authority("old-sid")

    def test_cancelled_or_unknown_lifetime_grants_nothing(self):
        store, duty = self.booking()
        with mock.patch.object(peer_steward, "_retire_subject_state", return_value="unknown"):
            peer_steward._resume_retire_obligation(duty, store)
        self.assert_authority("old-sid")
        cancelled = store.update(duty["id"], state="cancelled")
        peer_steward._resume_retire_obligation(cancelled, store)
        self.assert_authority("old-sid")

    def test_a_later_or_mismatched_successor_mark_grants_nothing(self):
        store, duty = self.booking()
        marker = peer_steward._seat_successor_path("w1:pNew")
        original = json.loads(marker.read_text())
        for change in ({"at": duty["accepted_at"] + 1}, {"server": "another-server"},
                       {"successor": {"harness": "claude", "session_id": "new-sid"}},
                       {"successor": {"harness": "opencode", "session_id": "later-sid"}}):
            with self.subTest(change=change):
                marker.write_text(json.dumps({**original, **change}))
                self.assertEqual(peer_steward._retire_handover(duty), "skipped:successor-unverified")
                self.assert_authority("old-sid")
                self.assertTrue(marker.exists())

    def test_completed_retirement_without_original_birth_evidence_grants_nothing(self):
        store, duty = self.booking()
        completed = store.update(duty["id"], state="complete", result="normal-exit")
        for foreground in (None, {"pid": 4242}, {"start": "800"}):
            with self.subTest(foreground=foreground):
                value = json.loads(json.dumps(completed))
                value["intent"]["identity"]["foreground"] = foreground
                peer_steward._resume_retire_obligation(value, store)
                self.assert_authority("old-sid")

    def test_existing_transfer_to_another_successor_is_preserved(self):
        import session_tidy
        store, duty = self.booking()
        self.handover.record_retire_handover("old-sid", "claude", "first-successor", "codex",
            jobs=self.jobs_path, successor_seat=session_tidy._pane_seat_of("w1:pFirst"))
        store.update(duty["id"], state="complete", result="normal-exit")
        peer_steward._resume_retire_obligation(store.get(duty["id"]), store)
        self.assert_authority("first-successor")

    def test_concurrent_replacement_survives_the_original_marks_consumption(self):
        store, duty = self.booking()
        marker = peer_steward._seat_successor_path("w1:pNew")
        requested, replaced = threading.Event(), threading.Event()
        import opencode_session_runtime as runtime
        original_read = runtime._read_native_record
        reads = 0

        def replace():
            requested.wait(2)
            peer_steward._mark_seat_successor("w1:pNew", "w2:pLater", "opencode", "later-sid")
            replaced.set()

        writer = threading.Thread(target=replace)
        writer.start()

        def read(path, *args, **kwargs):
            nonlocal reads
            value = original_read(path, *args, **kwargs)
            if path == marker:
                reads += 1
                if reads == 2:
                    # Precisely between the preimage read and unlink. Without
                    # shared serialization the replacement is lost here.
                    requested.set()
                    self.assertFalse(replaced.wait(.1))
            return value

        try:
            with mock.patch.object(runtime, "_read_native_record", read):
                self.assertEqual(peer_steward._retire_handover(duty), "1")
        finally:
            requested.set()
            writer.join(2)
        self.assertFalse(writer.is_alive())
        self.assertTrue(replaced.is_set())
        self.assertEqual(json.loads(marker.read_text())["successor"]["session_id"], "later-sid")
        self.assert_authority()


class ClosedParentNativeHandoverTest(_TmpRootMixin, unittest.TestCase):
    """Real native readers, resolver, writer and consumers in private state."""

    PARENT, HEADLESS, SUCCESSOR = "ses_original", "ses_headless", "codex-successor"
    SUCCESSOR_HARNESS = "codex"
    PID, START = 99999991, "800"

    def setUp(self):
        super().setUp()
        import dispatch_seat_handover as handover
        import opencode_session_runtime as native
        import owner_route_binding as owner
        import session_tidy
        self.handover, self.native, self.tidy = handover, native, session_tidy
        self.route = owner.ROUTE.compile_route(
            "autopilot-code", "debug", "quick", self.tmp_root, self.tmp_root / "artifacts",
            predicates=[], transport=None, tracking="tracked",
            tracked_gate_evidence={"spec_read": {"satisfied": True, "source": "canonical-prd-sha256"},
                "drift_verdict": "within-spec", "workflow_mode": "tracked",
                "artifact_guard": {"satisfied": True, "source": "conductor-prechecked"}},
            registered_headless_evidence={"candidates": [
                {"harness": h, "transport": "headless", "surface": "registered-headless",
                 "status": "supported", "probe_source": "fixture", "probe_time": "2026-10-13T00:00:00Z"}
                for h in ("claude", "codex", "opencode")]})
        self.route_path = self.tmp_root / "original-route.json"
        self.route_path.write_text(json.dumps(self.route))
        self.meta = {"attempt_schema_version": "2", "dispatch_depth": "1", "worker_type": "owner",
            "registered_worker": "1", "execution_surface": "registered-headless", "transport": "headless",
            "attempt_id": "att-selected", "harness": "opencode", "parent_harness": "opencode",
            "parent_sid": self.PARENT, "route_node": "owner", "note": "dead-worker-fail",
            "failure_class": "fail", "owner_route_file": str(self.route_path),
            "owner_route_id": self.route["route_id"], "owner_route_hash": self.route["route_hash"]}
        self.rows = [("done", {**self.meta, "attempt_id": aid})
                     for aid in ("att-ancestor-one", "att-ancestor-two", "att-selected")]
        self.foreign = {**self.meta, "attempt_id": "att-foreign", "harness": "codex",
            "parent_sid": self.HEADLESS, "owner_route_id": "rt-foreign", "owner_route_hash": "sha256:foreign"}
        self.rows.append(("running", self.foreign))
        self.write_jobs()
        self.native_args = SimpleNamespace(jobs=self.jobs_path, worktree=str(self.tmp_root),
                                           parent_attempt_id="att-selected")
        native.bind_session(self.native_args, self.HEADLESS)
        self.binding_path = native.binding_path(self.native_args)
        self.native_path = Path(os.environ["HOME"]) / ".local/state/hearting/tui-identity" / f"{self.PID}-{self.START}.json"
        self.native_path.parent.mkdir(parents=True)
        self.native_value = {"schema": "hearting-tui-selection-v1", "pid": self.PID, "start": self.START,
            "sessionID": self.PARENT,
            "writtenAt": peer_steward.datetime.datetime.now(peer_steward.datetime.timezone.utc).isoformat()}
        self.native_path.write_text(json.dumps(self.native_value))
        self.native_path.chmod(0o664)
        self.identity = {"session_id": self.HEADLESS, "harness": "opencode", "pane": "w1:pOld",
            "server": "default", "name": "old", "foreground": {"pid": self.PID, "start": self.START,
                "argv": ["opencode"], "shell_pid": 99999992, "shell_start": "700"}}
        self.store = peer_steward.peer_obligations.ObligationStore()
        self.book(self.SUCCESSOR_HARNESS)
        self.seat = session_tidy._pane_seat_of("w1:pNew")
        # Existing route/runtime payloads survive replay byte for byte.
        for name in ("resource.json", "completed-prefix.json", "owner.log"):
            (self.tmp_root / name).write_text('{"completed":["plan"],"round_ceiling":1,"state":"retained"}')

    def write_jobs(self):
        self.jobs_path.write_text("".join("now\t" + status + "\t" + str(self.tmp_root) + "\t" +
            str(self.tmp_root) + "\towner.log\t" + ",".join(f"{k}={v}" for k, v in meta.items()) + "\n"
            for status, meta in self.rows))

    def book(self, harness, duty_id="retire-native-fixture"):
        peer_steward._mark_seat_successor("w1:pNew", "w1:pOld", harness, "")
        self.mark = peer_steward._seat_successor_path("w1:pNew")
        self.mark.chmod(0o664)
        self.requester = {"session_id": self.SUCCESSOR, "harness": harness,
                          "pane": "w1:pNew", "server": "default"}
        self.accept(duty_id)

    def accept(self, duty_id="retire-native-fixture"):
        # Native and mark creation precede real acceptance, which precedes completion.
        self.duty = self.store.create(duty_id, "retire", self.identity,
                                      {"target": "w1:pOld", "requester": self.requester})
        self.duty = self.store.update(self.duty["id"], state="complete", result="target-already-gone",
            delivery="completed", cleanup="complete", observation={"phase": "complete"})

    def protected(self):
        paths = [self.store._record_path(self.duty["id"]), self.jobs_path, self.route_path,
                 *(self.tmp_root / name for name in ("resource.json", "completed-prefix.json", "owner.log"))]
        return {str(p): p.read_bytes() for p in paths}

    def authority_bytes(self):
        paths = (self.mark, self.tidy._ledger_path(self.seat), self.handover._snapshot_path(self.seat.key))
        return {str(p): p.read_bytes() if p.exists() else None for p in paths}

    def replay(self, duty=None):
        with mock.patch.object(peer_steward.subprocess, "run", side_effect=AssertionError("external transport")), \
             mock.patch.object(peer_steward, "_close_pane", side_effect=AssertionError("pane close")), \
             mock.patch.object(peer_steward, "_caller_pane", return_value="w9:pObserver"):
            return peer_steward._resume_retire_obligation(duty or self.duty, self.store)

    def assert_refused(self, duty=None):
        before, authority = self.protected(), self.authority_bytes()
        self.replay(duty)
        self.assertEqual(self.protected(), before)
        self.assertEqual(self.authority_bytes(), authority)
        self.assertEqual(self.handover.effective_parent(self.meta, self.jobs_path), self.PARENT)
        self.assertEqual(self.handover.effective_parent(self.foreign, self.jobs_path), self.HEADLESS)

    def test_real_recovery_preserves_history_and_hands_only_open_original_route_once(self):
        before = self.protected()
        self.replay()
        self.assertEqual(self.protected(), before)
        self.assertFalse(self.mark.exists())
        self.assertEqual(self.handover.effective_parent(self.meta, self.jobs_path), self.SUCCESSOR)
        self.assertEqual(self.handover.effective_parent_harness(self.meta, self.jobs_path), "codex")
        self.assertTrue(self.handover.owns(self.meta, self.SUCCESSOR, self.jobs_path))
        self.assertEqual(self.handover.effective_parent(self.foreign, self.jobs_path), self.HEADLESS)
        ledger = self.tidy._ledger_path(self.seat).read_bytes()
        row = json.loads(ledger)
        self.assertEqual({b["attempt"] for b in row["bindings"]}, {m["attempt_id"] for _, m in self.rows[:3]})
        snapshot_bindings = self.handover.read_snapshot(self.seat.key)["bindings"]
        self.assertEqual(len(snapshot_bindings), 3)
        expected = {(meta["attempt_id"], meta["parent_sid"]) for _, meta in self.rows[:3]}
        self.assertEqual({(binding["attempt"], binding["parent"]) for binding in snapshot_bindings}, expected)
        current = self.handover.current_bindings("opencode", self.SUCCESSOR, self.jobs_path)
        self.assertTrue(all(binding in current for binding in row["bindings"]))
        self.replay()
        self.assertEqual(self.tidy._ledger_path(self.seat).read_bytes(), ledger)
        self.assertEqual(self.protected(), before)

    def test_unrelated_closed_missing_sidecar_does_not_become_a_global_requirement(self):
        unrelated = {**self.meta, "attempt_id": "att-unrelated", "parent_sid": "ses_unrelated",
                     "owner_route_id": "rt-closed", "owner_route_hash": "sha256:closed"}
        self.rows.append(("done", unrelated))
        self.write_jobs()
        self.replay()
        self.assertTrue(self.handover.owns(self.meta, self.SUCCESSOR, self.jobs_path))
        self.assertEqual(self.handover.effective_parent(unrelated, self.jobs_path), "ses_unrelated")

    def test_failed_native_source_never_falls_back_to_open_foreign_parent_route(self):
        for raw in (None, b"false", b"null", b"[]", b"{", b"{}", b" " * 1025):
            with self.subTest(raw=raw):
                if raw is None:
                    self.native_path.unlink(missing_ok=True)
                else:
                    self.native_path.write_bytes(raw)
                self.assert_refused()

    def test_false_native_fields_namesake_and_latest_guesses_grant_nothing(self):
        for change in ({"sessionID": "ses_foreign"}, {"sessionID": False}, {"pid": self.PID + 1},
                       {"pid": True}, {"start": "801"}, {"start": True}, {"schema": "other"},
                       {"writtenAt": None}, {"writtenAt": "2026-10-13T00:00:00"}):
            with self.subTest(change=change):
                self.native_path.write_text(json.dumps({**self.native_value, **change}))
                self.assert_refused()
        self.native_path.unlink()
        with mock.patch.dict(os.environ, {"OPENCODE_SESSION_ID": self.PARENT, "AGENT_SESSION_ID": self.PARENT}):
            self.assert_refused()

    def test_original_birth_live_unknown_reused_and_wrong_argv(self):
        real_stat = os.stat
        def proc_stat(path, *args, **kw):
            return object() if str(path) == f"/proc/{self.PID}" else real_stat(path, *args, **kw)
        for birth in ("800", None):
            with self.subTest(birth=birth), \
                 mock.patch.object(peer_steward, "_retire_process_record", return_value=None), \
                 mock.patch.object(peer_steward, "_proc_start_ticks", return_value=birth), \
                 mock.patch.object(peer_steward.os, "stat", side_effect=proc_stat):
                self.assert_refused()
        for change in ({"pid": True}, {"pid": 0}, {"start": None}, {"start": "801"},
                       {"argv": ["opencode", "run"]}, {"argv": ["namesake"]}, {"argv": []}):
            with self.subTest(change=change):
                duty = json.loads(json.dumps(self.duty))
                duty["intent"]["identity"]["foreground"].update(change)
                self.assert_refused(duty)
        # Reuse is proof of the OLD lifetime's end, never a source for its SID.
        with mock.patch.object(peer_steward, "_retire_process_record", return_value={"start": "801"}):
            self.replay()
        self.assertTrue(self.handover.owns(self.meta, self.SUCCESSOR, self.jobs_path))

    def test_invalid_sidecar_and_unregistered_nonowner_relations_refuse(self):
        original = self.binding_path.read_bytes()
        for raw in (b"false", b"[]", b"{}", b"{", b" " * 4097,
                    json.dumps({"attempt_id": "att-other", "worktree": str(self.tmp_root),
                                "session_id": self.HEADLESS}).encode(),
                    json.dumps({"attempt_id": "att-selected", "worktree": str(self.tmp_root / "other"),
                                "session_id": self.HEADLESS}).encode()):
            self.binding_path.write_bytes(raw)
            self.assert_refused()
        self.binding_path.unlink()
        self.assert_refused()
        self.binding_path.write_bytes(original)
        for change in ({"dispatch_depth": "2"}, {"worker_type": "frame"}, {"registered_worker": "0"},
                       {"execution_surface": "native-subagent"}, {"owner_route_hash": ""}):
            self.rows[2] = ("done", {**self.meta, **change})
            self.write_jobs()
            self.assert_refused()

    def test_native_parent_mismatch_and_multiple_matching_attempts_include_closed_foreign(self):
        self.rows[2] = ("done", {**self.meta, "parent_sid": "ses_foreign"})
        self.write_jobs()
        self.assert_refused()
        for parent in (self.PARENT, "ses_foreign"):
            self.rows[2] = ("done", self.meta)
            other = {**self.meta, "attempt_id": "att-duplicate", "parent_sid": parent,
                     "owner_route_id": "rt-closed", "owner_route_hash": "sha256:closed"}
            self.rows = self.rows[:4] + [("done", other)]
            self.write_jobs()
            self.native.bind_session(SimpleNamespace(jobs=self.jobs_path, worktree=str(self.tmp_root),
                                                     parent_attempt_id="att-duplicate"), self.HEADLESS)
            self.assert_refused()

    def test_source_time_and_acceptance_are_real_not_backdated_payloads(self):
        for stamp in (None, False, float("nan"), float("inf"), 0, self.native_path.stat().st_ctime - 1):
            with self.subTest(stamp=stamp):
                duty = {**self.duty, "accepted_at": stamp}
                self.assert_refused(duty)
        # An old payload in a new generation remains too late.
        other = self.native_path.with_suffix(".new")
        other.write_text(json.dumps(self.native_value))
        os.replace(other, self.native_path)
        self.assert_refused()

    def test_late_sidecar_and_mark_with_old_payload_times_are_refused(self):
        original = self.binding_path.read_bytes()
        self.binding_path.write_bytes(original)
        self.assert_refused()

    def test_late_mark_with_old_payload_time_is_refused_independently(self):
        self.mark.write_bytes(self.mark.read_bytes())
        self.assert_refused()

    def test_native_mark_and_sidecar_unsafe_reads_leave_all_authority_unchanged(self):
        for number, path in enumerate((self.native_path, self.mark, self.binding_path)):
            original = path.read_bytes()
            path.unlink()
            path.symlink_to(path.with_suffix(".missing"))
            self.assert_refused()
            path.unlink()
            os.mkfifo(path)
            # Authority snapshots do not read FIFO contents.
            with mock.patch.object(self, "authority_bytes", return_value={"fixture": "unchanged"}):
                self.assertEqual(peer_steward._retire_handover(self.duty),
                                 "skipped:successor-unverified" if path == self.mark else None)
            path.unlink()
            path.write_bytes(original)
            # A restored source is newer than that acceptance; only a later one may use it.
            self.accept(f"retire-native-restored-{number}")
        # UID and EACCES injections use the actual descriptor readers.
        uid = os.getuid()
        with mock.patch.object(os, "getuid", return_value=uid + 1):
            self.assert_refused()
        real_open = os.open
        def denied(path, *args, **kwargs):
            if Path(path) == self.native_path:
                raise PermissionError()
            return real_open(path, *args, **kwargs)
        with mock.patch.object(os, "open", side_effect=denied):
            self.assert_refused()

    def test_requester_mark_and_unsupported_completed_state_refuse(self):
        for state, result in (("cancelled", "normal-exit"), ("complete", "unknown"),
                              ("complete", "force-killed")):
            self.assert_refused({**self.duty, "state": state, "result": result})
        self.assertIsNone(peer_steward._retire_handover({**self.duty, "state": "pending"}))
        for field, value in (("session_id", ""), ("harness", "unknown"), ("pane", "w1:pOther"), ("server", "foreign")):
            duty = json.loads(json.dumps(self.duty))
            duty["intent"]["requester"][field] = value
            self.assert_refused(duty)
        original = json.loads(self.mark.read_text())
        for change in ({"schema": False}, {"pane": "other"}, {"beside": "other"}, {"server": "other"},
                       {"successor": []}, {"successor": {"harness": "codex", "session_id": "other"}},
                       {"at": False}, {"at": float("nan")}, {"at": self.duty["accepted_at"] + 1}):
            self.mark.write_text(json.dumps({**original, **change}))
            self.assert_refused()

    def test_closed_route_and_another_successor_remain_authoritative(self):
        outcome = self.route_path.with_suffix(".outcome.json")
        outcome.write_text('{"state":"closed"}')
        self.assert_refused()
        outcome.unlink()
        self.handover.record_retire_handover(self.PARENT, "opencode", "first-successor", "claude",
            jobs=self.jobs_path, successor_seat=self.tidy._pane_seat_of("w1:pFirst"))
        before = self.authority_bytes()
        self.replay()
        self.assertEqual(self.authority_bytes(), before)
        self.assertEqual(self.handover.effective_parent(self.meta, self.jobs_path), "first-successor")

    def test_selected_generation_is_rechecked_under_mark_lock_without_global_jobs_freeze(self):
        real = peer_steward._retire_predecessor
        calls = 0
        def replaced(duty):
            nonlocal calls
            calls += 1
            if calls == 2:
                other = self.binding_path.with_suffix(".new")
                other.write_bytes(self.binding_path.read_bytes())
                os.replace(other, self.binding_path)
            return real(duty)
        with mock.patch.object(peer_steward, "_retire_predecessor", side_effect=replaced):
            self.assert_refused()

    def test_same_normal_command_and_detached_callback_consume_original_requester(self):
        with mock.patch.object(peer_steward, "_retire_target", return_value=("unknown", {
                "harness": "-", "name": "old", "pane": "-", "session_id": "-"}, 1, "pane-unverified")), \
             mock.patch.object(peer_steward, "_current_session_identity", return_value=(self.SUCCESSOR, "codex")), \
             mock.patch.object(peer_steward, "_caller_pane", return_value="w1:pNew"), \
             mock.patch.object(peer_steward, "_herdr_missing", return_value=False), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=AssertionError("transport")):
            self.assertEqual(peer_steward.cmd_retire(SimpleNamespace(target="w1:pOld")), 0)
        self.assertTrue(self.handover.owns(self.meta, self.SUCCESSOR, self.jobs_path))

    def test_detached_lifecycle_needs_no_observer_native_identity(self):
        before = self.protected()
        runner_start = len(peer_steward.peer_obligations._RUNNERS)
        with mock.patch.object(peer_steward, "_current_session_identity", return_value=("observer", "claude")), \
             mock.patch.object(peer_steward, "_caller_pane", return_value=""), \
             mock.patch.object(peer_steward, "_ensure_watch_observers"), \
             mock.patch.object(peer_steward.dispatch_batch_obligations, "ensure_observers"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=AssertionError("transport")):
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)
        for process in peer_steward.peer_obligations._RUNNERS[runner_start:]:
            self.assertEqual(process.wait(timeout=15), 0)
        self.assertTrue(self.handover.owns(self.meta, self.SUCCESSOR, self.jobs_path))
        self.assertEqual(self.protected(), before)

    def test_real_start_correct_admission_and_retained_input_use_written_effective_parent(self):
        import work_start
        import dispatch_replacement as replacement
        import dispatch_owner_input as owner_input
        from dispatch_contract import DispatchContractError
        # Normal input admission exists before the owner ends.
        self.rows[2] = ("running", self.meta)
        self.write_jobs()
        owner_input.initialize_owner_input(self.jobs_path, "att-selected", "opencode-next-turn")
        self.rows[2] = ("done", self.meta)
        self.write_jobs()
        before = self.protected()
        self.replay()
        self.assertEqual(self.protected(), before)
        rows = replacement._rows(self.jobs_path.read_text().splitlines())
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": self.SUCCESSOR,
                                         "AGENT_DISPATCH_CALLER_HARNESS": "codex"}):
            # This is the existing correct/start continuation admission, not a
            # mocked owns=True or a new path. Input writes have their own baseline.
            replacement._authorized(self.jobs_path, rows, self.meta)
            input_path = owner_input._path(self.jobs_path, "att-selected")
            initial = input_path.read_bytes()
            result = owner_input.submit(self.jobs_path, "att-selected", "Continue approved work", "exact-answer")
            self.assertTrue(result["retained"])
            self.assertNotEqual(input_path.read_bytes(), initial)
            value = json.loads(input_path.read_bytes())
            self.assertEqual(value["requests"][0]["source_session"], self.SUCCESSOR)
            self.assertEqual(self.meta["parent_sid"], self.PARENT)
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "foreign-successor",
                                         "AGENT_DISPATCH_CALLER_HARNESS": "codex"}):
            with self.assertRaises(DispatchContractError):
                replacement._authorized(self.jobs_path, rows, self.meta)
        # Independent normal live start reuse consumes the SAME logical binding.
        self.rows[2] = ("running", self.meta)
        self.write_jobs()
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": self.SUCCESSOR}):
            self.assertEqual(work_start._slot(self.route, "owner", work_start._rows(self.jobs_path), self.jobs_path),
                             "att-selected")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "foreign-successor"}):
            with self.assertRaises(DispatchContractError):
                work_start._slot(self.route, "owner", work_start._rows(self.jobs_path), self.jobs_path)

    def test_real_completion_storage_allowlist_lease_ack_and_no_redelivery(self):
        import dispatch_pending_delivery as pending
        import dispatch_session_sweep as sweep
        # The synchronous Claude carrier reads the same written handover; native
        # turn-proof support is not inferred for the other harnesses.
        self.book("claude", "retire-native-claude")
        self.replay()
        root, kind = self.jobs_path.parent, "claude-parent-runtime"
        delivery = "delivery-" + "a" * 32
        receipt = {"schema_version": 2, "state": "delivered", "parent_attempt_id": "att-selected",
            "job_registry": str(self.jobs_path), "delivery_classification": "attention",
            "children": [{"attempt_id": "att-selected", "status": "done", "readiness": "ready",
                          "reason": "terminal-failure-or-unclosed", "required_action": "inspect-done-failure",
                          "harness": "opencode", "delivery_classification": "attention"}]}
        pending.create(root, recipient_kind=kind, recipient_key=self.PARENT, delivery_id=delivery,
            session_generation="", session_generation_supported="0", attempt_ids=["att-selected"],
            parent_attempt_id="att-selected", route_id=self.route["route_id"], route_node="owner",
            receipt=receipt, receipt_digest=pending._canonical_receipt_digest(receipt),
            row_revisions={"att-selected": "original-revision"})
        original = pending.read(root, self.PARENT, delivery)
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pNew"}):
            recipients = self.handover.storage_recipients(self.SUCCESSOR, harness="claude")
            self.assertIn((self.PARENT, frozenset({"att-selected", "att-ancestor-one", "att-ancestor-two"})),
                          recipients)
            allowed = dict(recipients)[self.PARENT]
            self.assertFalse(self.handover.record_for_session({"attempt_ids": ["att-foreign"]}, allowed))
            self.assertEqual(sweep.addressed_records(root, kind, "foreign-successor"), ([], 0))
            records, _ = sweep.sweep_deliver(root, kind, self.SUCCESSOR)
            self.assertEqual([r["delivery_id"] for r in records], [delivery])
            claimed = pending.read(root, self.PARENT, delivery)
            for key in ("delivery_id", "receipt_digest", "recipient_digest", "row_revisions"):
                self.assertEqual(claimed[key], original[key])
            self.assertEqual(sweep.ack_delivered(root, self.SUCCESSOR, records, acked_by="fixture-consumed"), 1)
            self.assertEqual(pending.read(root, self.PARENT, delivery)["state"], "acked")
            self.assertEqual(sweep.sweep_deliver(root, kind, self.SUCCESSOR)[0], [])
            self.assertIsNone(pending.read(root, self.SUCCESSOR, delivery))

    def test_pending_gone_settlement_defers_the_stale_label_to_the_completed_replay(self):
        self.duty = self.store.create("retire-native-pending", "retire", self.identity,
                                      {"target": "w1:pOld", "requester": self.requester})
        authority = self.authority_bytes()
        with mock.patch.object(peer_steward.subprocess, "run", side_effect=AssertionError("transport")):
            self.assertTrue(peer_steward._complete_gone_retire(self.store, self.duty))
        # The pending path never forwards the stale headless label (route F stays).
        self.assertEqual(self.authority_bytes(), authority)
        self.assertEqual(self.handover.effective_parent(self.foreign, self.jobs_path), self.HEADLESS)
        settled = self.store.get(self.duty["id"])
        self.assertEqual((settled["state"], settled["result"]), ("complete", "target-already-gone"))
        self.replay(settled)
        self.assertTrue(self.handover.owns(self.meta, self.SUCCESSOR, self.jobs_path))
        self.assertEqual(self.handover.effective_parent(self.foreign, self.jobs_path), self.HEADLESS)

    def test_unrelated_registry_appends_do_not_freeze_the_selected_relation(self):
        real = peer_steward._retire_predecessor
        calls = 0
        def appended(duty):
            nonlocal calls
            calls += 1
            if calls == 2:
                with self.jobs_path.open("a") as handle:
                    handle.write("now\trunning\t/x\t/x\tlog\tattempt_id=att-later,dispatch_depth=2\n")
            return real(duty)
        with mock.patch.object(peer_steward, "_retire_predecessor", side_effect=appended):
            self.replay()
        self.assertEqual(calls, 2)
        self.assertTrue(self.handover.owns(self.meta, self.SUCCESSOR, self.jobs_path))

    def test_ordinary_completed_opencode_booking_keeps_its_label_without_retained_source(self):
        # A booked label that is no headless owner session is the ordinary path:
        # a disposed TUI record does not block the missed-handover replay.
        self.native_path.unlink()
        self.binding_path.unlink()
        self.rows = [("done", {**self.meta, "attempt_id": aid, "parent_sid": self.HEADLESS})
                     for aid in ("att-ancestor-one", "att-ancestor-two", "att-selected")]
        self.write_jobs()
        self.replay()
        self.assertFalse(self.mark.exists())
        self.assertEqual(self.handover.effective_parent(self.rows[2][1], self.jobs_path), self.SUCCESSOR)

    def test_retained_selection_that_contradicts_an_ordinary_label_grants_nothing(self):
        self.binding_path.unlink()
        self.rows = [("done", {**self.meta, "attempt_id": aid, "parent_sid": self.HEADLESS})
                     for aid in ("att-ancestor-one", "att-ancestor-two", "att-selected")]
        self.write_jobs()
        authority = self.authority_bytes()
        self.replay()
        self.assertEqual(self.authority_bytes(), authority)
        self.assertEqual(self.handover.effective_parent(self.rows[2][1], self.jobs_path), self.HEADLESS)


class RetireBackgroundDialogTest(unittest.TestCase):
    def _cells(self, text):
        return [(ch, False) for ch in text]

    def test_detects_live_selection_ui(self):
        lines = [self._cells("Background work is running"),
                 self._cells("python3 train.py"),
                 self._cells("1. Exit and stop tasks"),
                 self._cells("2. Move to background and exit"),
                 self._cells("3. Stay"),
                 self._cells("Enter to confirm · Esc to cancel"),
                 self._cells("❯ ")]
        tasks = peer_steward._retire_background_dialog_lines(lines)
        self.assertIsNotNone(tasks)

    def test_detects_cursor_prefixed_selection(self):
        lines = [self._cells("Background work is running"),
                 self._cells("❯ 1. Exit and stop tasks"),
                 self._cells("2. Move to background and exit"),
                 self._cells("3. Stay"),
                 self._cells("Enter to select · Esc to cancel")]
        tasks = peer_steward._retire_background_dialog_lines(lines)
        self.assertIsNotNone(tasks)

    def test_ignores_other_forms(self):
        lines = [self._cells("Do you want to proceed?"),
                 self._cells("Enter to confirm")]
        self.assertIsNone(peer_steward._retire_background_dialog_lines(lines))
        self.assertIsNone(peer_steward._retire_background_dialog_lines([]))
        self.assertIsNone(peer_steward._retire_background_dialog_lines(None))

    def test_ignores_quoted_transcript_without_live_ui(self):
        lines = [self._cells("● Reading the PR #288 diff"),
                 self._cells("+    The dialog reads \"Background work is running … 1. Exit and stop tasks /"),
                 self._cells("+    2. Move to background and exit / 3. Stay\". …"),
                 self._cells("❯ ")]
        self.assertIsNone(peer_steward._retire_background_dialog_lines(lines))

    def test_ignores_busy_answer_quoting_dialog(self):
        lines = [self._cells("✻ Working… (esc to interrupt)"),
                 self._cells("  The dialog reads \"Background work is running"),
                 self._cells("  1. Exit and stop tasks"),
                 self._cells("  2. Move to background and exit / 3. Stay\"."),
                 self._cells("  Enter to confirm · Esc to cancel is only in the real window."),
                 self._cells("❯ let me check the logs")]
        self.assertIsNone(peer_steward._retire_background_dialog_lines(lines))

    def test_retire_rechecks_before_confirmation_enter_if_another_form_appears(self):
        dialog = peer_steward._screen_lines(
            "Background work is running\n❯ 1. Exit and stop tasks\n"
            "2. Move to background and exit\n3. Stay\nEnter to confirm · Esc to cancel\n")
        unrelated_form = peer_steward._screen_lines("Do you want to proceed?\nEnter to confirm\n")
        for switch_at in (3, 5):
            with self.subTest(switch_at=switch_at):
                reads = [dialog] * (switch_at - 1) + [unrelated_form]
                calls = []
                def run(argv, **kwargs):
                    calls.append(argv)
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                with mock.patch.object(peer_steward, "_read_screen", side_effect=reads), \
                     mock.patch.object(peer_steward, "_retire_foreground", return_value="same-process"), \
                     mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
                     mock.patch.object(peer_steward, "_RETIRE_SECONDS", 0):
                    result = peer_steward._retire_claude_background_confirm(
                        "peer", "w1:pX", "claude", {}, "same-process",
                        lambda reason, **kwargs: reason, None, None,
                    )
                self.assertEqual(result, "form-open")
                keys = [argv[4:] for argv in calls if argv[:3] == ["herdr", "pane", "send-keys"]]
                self.assertEqual(len(keys), 1 if switch_at == 5 else 0)
                texts = [argv[-1] for argv in calls if argv[:3] == ["herdr", "pane", "send-text"]]
                self.assertEqual(texts, ["1"] if switch_at == 3 else ["1", "3"])


class BlockingScreenClassifierTest(unittest.TestCase):
    @staticmethod
    def cells(text):
        return [(char, False) for char in text]

    def test_codex_hook_review_is_recognized_at_top_for_normal_and_wrapped_views(self):
        for screen in (
            "Hooks need review\n1 hook is new or changed\nReview hooks\n"
            "Trust all and continue\nContinue without trusting\nEnter to confirm · Esc to skip",
            "Hooks need review\n2 hooks are new or changed\nChecking hooks…\n"
            "1. Review hooks\n2. Trust all and continue\n"
            "3. Continue without trusting (hooks\n   won't run)",
        ):
            with self.subTest(screen=screen):
                self.assertEqual("native-trust-wait", peer_steward._blocking_screen(
                    "codex", [self.cells(line) for line in screen.splitlines()]))

    def test_real_codex_01621_captures_and_upstream_snapshots_are_recognized(self):
        capture_names = (
            "cap-alt80.txt", "cap-alt80.ansi", "cap-inline80.txt", "cap-inline80.ansi",
            "cap-alt40.txt", "cap-alt40.ansi", "cap-inline40.txt", "cap-inline40.ansi",
            "cap-80x40-noalt.txt",
        )
        for name in capture_names:
            with self.subTest(capture=name):
                self.assertEqual("native-trust-wait", peer_steward._blocking_screen(
                    "codex", _codex_capture(name)))
        for name in ("startup_hooks_review_prompt.snap", "startup_hooks_picker_progress_40.snap",
                     "startup_hooks_review_prompt_with_trust_error.snap"):
            with self.subTest(snapshot=name):
                self.assertEqual("native-trust-wait", peer_steward._blocking_screen(
                    "codex", _codex_snapshot(name)))

    def test_codex_location_popup_under_transcript_blocks_but_quotes_do_not(self):
        popup = _codex_snapshot("worktrees_new_choices.snap")
        chat_popup = "banner: project · main\nPrior chat text\n" + popup
        self.assertEqual("form-open", peer_steward._blocking_screen("codex", chat_popup))
        quoted = [self.cells("● Working"), self.cells("Quoted: Where should the new conversation run?"),
                  self.cells("› Use current Git worktree; Enter select · Esc back"),
                  self.cells("❯ Continue with the requested change")]
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness):
                self.assertIsNone(peer_steward._blocking_screen(harness, quoted))
        # Codex 0.162.1's own composer glyph is `›`, usually followed by its
        # status footer, so the composer is not necessarily the last row.
        popup_rows = popup.strip("\n").splitlines()
        for tail in (["› ordinary draft"],
                     ["› ordinary draft", "", "  ? for shortcuts            100% context left"]):
            with self.subTest(tail=tail):
                quoted_popup = "\n".join(["• Earlier answer quoting the picker:"] + popup_rows + tail)
                self.assertIsNone(peer_steward._blocking_screen("codex", quoted_popup))

    def test_singular_count_with_period_matches_real_dialog_shape(self):
        screen = ("Hooks need review\n1 hook is new or changed.\nReview hooks\n"
                  "Trust all and continue\nContinue without trusting\n")
        self.assertEqual("native-trust-wait", peer_steward._blocking_screen("codex", screen))

    def test_codex_hook_review_quote_and_unknown_screen_do_not_block(self):
        quote = [self.cells("● Reviewing documentation"),
                 self.cells("+ Hooks need review 1 hook is new or changed"),
                 self.cells("+ Review hooks / Trust all and continue / Continue without trusting"),
                 self.cells("❯ let me inspect the implementation")]
        self.assertIsNone(peer_steward._blocking_screen("codex", quote))
        self.assertIsNone(peer_steward._blocking_screen("codex", None))
        self.assertIsNone(peer_steward._blocking_screen("codex", []))

    def test_hook_review_signature_is_codex_scoped(self):
        lines = [self.cells(row) for row in (
            "Hooks need review", "1 hook is new or changed", "Review hooks",
            "Trust all and continue", "Continue without trusting")]
        self.assertEqual("native-trust-wait", peer_steward._blocking_screen("codex", lines))
        self.assertIsNone(peer_steward._blocking_screen("claude", lines))
        self.assertIsNone(peer_steward._blocking_screen("opencode", lines))


class ObservedStartTest(_TmpRootMixin, unittest.TestCase):
    def invoke(self, kind, world):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name, value in (("_herdr_missing", False), ("_ensure_pane_ingress", None),
                                ("_export_opencode_tui_scoped", "missing"),
                                ("_read_screen", None), ("_pane_is_managed", False),
                                ("_process_session", "observed-sid" if kind == "codex" else None),
                                ("_current_session_identity", ("parent-sid", "claude"))):
                stack.enter_context(mock.patch.object(peer_steward, name, return_value=value))
            stack.enter_context(mock.patch.object(peer_steward.subprocess, "run", side_effect=world))
            stack.enter_context(mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: world.clock))
            stack.enter_context(mock.patch.object(peer_steward.time, "sleep", side_effect=world.sleep))
            output = stack.enter_context(mock.patch("builtins.print"))
            rc = peer_steward.main(["start", "requested", "--kind", kind, "--pane", "w1:pM",
                                    "--cwd", str(self.tmp_root)])
        return rc, output.call_args[0][0]

    def world(self, kind, *, existing=False, error="timeout", name=None, sid="observed-sid"):
        owner = self

        class World:
            clock = 0.0
            launched = existing
            calls = None

            def __init__(self):
                self.calls = []
                self.name = name
                self.kind = kind
                self.sid = sid
                self.cwd = str(owner.tmp_root)
                self.ready_after = .1

            def sleep(self, value):
                self.clock += value

            def __call__(self, argv, **kwargs):
                self.calls.append(argv)
                if argv[:3] == ["herdr", "agent", "get"]:
                    if not self.launched or (not existing and self.clock < self.ready_after):
                        return _herdr_json({"error": {"code": "agent_not_found"}})
                    return _herdr_json({"result": {"agent": {
                        "agent": self.kind, "pane_id": "w1:pM", "name": self.name,
                        "cwd": self.cwd, "agent_session": {"value": self.sid}, "agent_status": "idle"}}})
                if argv[:3] == ["herdr", "agent", "start"]:
                    self.launched = True
                    return _herdr_json({"error": {"code": error}})
                if argv[:3] == ["herdr", "agent", "rename"]:
                    self.name = argv[-1]
                    return _herdr_json({"result": {"type": "ok"}})
                raise AssertionError(argv)

        return World()

    def test_late_native_timeout_and_busy_settle_once_on_all_harnesses(self):
        for kind in ("claude", "codex", "opencode"):
            for error in ("timeout", "agent_pane_busy"):
                with self.subTest(kind=kind, error=error):
                    world = self.world(kind, error=error)
                    rc, line = self.invoke(kind, world)
                    self.assertEqual(rc, 0)
                    self.assertIn("started=true", line)
                    self.assertIn("start_verify=observed-pane", line)
                    self.assertEqual(sum(a[:3] == ["herdr", "agent", "start"] for a in world.calls), 1)
                    self.assertEqual(sum(a[:3] == ["herdr", "agent", "rename"] for a in world.calls), 1)
                    pane_info_reads = [a for a in world.calls
                                       if a[:3] == ["herdr", "pane", "process-info"]]
                    self.assertEqual(pane_info_reads, [])  # this case stubs the shared resolver
                    marker = peer_steward.peer_message.read_steward_markers()[("claude", "parent-sid")]
                    entry = marker["targets"]["observed-sid"]
                    self.assertEqual((entry["harness"], entry["pane"], entry["source"]), (kind, "w1:pM", "start"))

    def test_reuse_observes_existing_pane_without_another_launch_or_keyboard_input(self):
        for kind in ("claude", "codex", "opencode"):
            with self.subTest(kind=kind):
                world = self.world(kind, existing=True)
                rc, line = self.invoke(kind, world)
                self.assertEqual(rc, 0)
                self.assertIn("started=true", line)
                self.assertEqual([a[2] for a in world.calls], ["get", "rename", "get"])

    def test_delayed_sid_completes_only_the_matching_name_and_pane_entry(self):
        world = self.world("opencode", existing=True, sid="-")
        _, line = self.invoke("opencode", world)
        self.assertIn("session_id=-", line)
        marker = peer_steward.peer_message.read_steward_markers()[("claude", "parent-sid")]
        self.assertIsNone(marker["targets"]["requested"]["session_id"])
        world.sid = "ses_observed"
        self.invoke("opencode", world)
        marker = peer_steward.peer_message.read_steward_markers()[("claude", "parent-sid")]
        self.assertEqual(set(marker["targets"]), {"ses_observed"})
        self.assertEqual(marker["targets"]["ses_observed"]["pane"], "w1:pM")

    def test_foreign_name_kind_or_directory_never_claims_an_existing_agent(self):
        for change in (dict(name="foreign"), dict(kind="codex"), dict(cwd="/elsewhere")):
            with self.subTest(change=change):
                world = self.world("opencode", existing=True, name="requested")
                for key, value in change.items():
                    setattr(world, key, value)
                rc, line = self.invoke("opencode", world)
                self.assertIn("started=false", line)
                self.assertFalse(any(a[:3] == ["herdr", "agent", "rename"] for a in world.calls))
                self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})

    def test_a_real_timeout_stays_failed_with_no_marker_and_one_launch(self):
        world = self.world("opencode")
        world.ready_after = 100
        _, line = self.invoke("opencode", world)
        self.assertIn("started=false", line)
        self.assertIn("reason=timeout", line)
        self.assertLessEqual(world.clock, 5)
        self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})
        self.assertEqual(sum(a[:3] == ["herdr", "agent", "start"] for a in world.calls), 1)


if __name__ == "__main__":
    unittest.main()
