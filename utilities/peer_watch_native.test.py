#!/usr/bin/env python3
"""Native watch contract: exact identity, meaningful transitions, unknown retention."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
import peer_address  # noqa: E402
import peer_obligations  # noqa: E402
import peer_steward_watch  # noqa: E402
import terminal_surface  # noqa: E402
from split_module_fixture import patch_split  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("peer_watch_native_steward", ROOT / "utilities" / "peer-steward.py")
peer_steward = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(peer_steward)


class NativeWatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="peer-watch-native-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.watch_root = self.state / "peer-watches"
        self.watch_root.mkdir(parents=True)
        self.store = peer_obligations.ObligationStore(self.state)
        self.pid = os.getpid()
        from dispatch_contract import process_start_ticks
        self.pid_start = process_start_ticks(self.pid)
        self.pid_namespace = os.readlink(f"/proc/{self.pid}/ns/pid")
        self.watch_id = "native-watch-test"
        self.arm = {
            "schema_version": 1, "watch_id": self.watch_id, "target": "claude:sid-native",
            "claim_key": "logical-test", "until": [], "timeout": 10000,
            "refs": [], "server": "default", "steward_server": "default",
            "readiness_endpoint": "native:default",
            "agent": {"harness": "claude", "session_id": "sid-native", "name": "native",
                      "pane": "w1:p9", "pid": self.pid, "pid_start": self.pid_start,
                      "pid_namespace": self.pid_namespace},
            "steward": {"harness": "claude", "session_id": "sid-steward", "project": "fixture"},
            "steward_seat": None, "armed_ts": "2026-11-12T00:00:00Z",
            "initial_state": "working", "deadline": None, "observer_protocol": 3,
            "wake": "none", "watcher": {"pid": self.pid, "pid_start": self.pid_start},
            "observer_generation": 1,
        }
        self.arm_path = self.watch_root / f"{self.watch_id}.json"
        self.arm_path.write_text(json.dumps(self.arm), encoding="utf-8")

    def paths(self):
        return peer_steward._watch_paths(self.watch_id, self.watch_root)

    def native_payload(self, state, **overrides):
        agent = {"agent": "claude", "agent_status": state,
                 "agent_session": {"value": "sid-native"}, "name": "native",
                 "pane_id": "w1:p9", "pid": self.pid, "pid_start": self.pid_start,
                 "pid_namespace": self.pid_namespace}
        agent.update(overrides)
        return {"result": {"type": "agent_info", "agent": agent}}

    def test_cmd_wait_resolves_and_waits_without_herdr(self):
        identity = {"harness": "claude", "session_id": "sid-native", "pane": "w1:p9"}
        result = self.native_payload("idle")
        args = SimpleNamespace(target="claude:sid-native", ref=[], until=["idle"], timeout=50)
        output = []
        with mock.patch.object(peer_address, "resolve", return_value=(identity, None)), \
             mock.patch.object(terminal_surface, "wait_native", return_value=result) as wait, \
             mock.patch.object(peer_steward, "_record"), \
             mock.patch.object(peer_steward, "_mark_observed"), \
             mock.patch.object(peer_steward, "_run_herdr_wait", side_effect=AssertionError("Herdr wait")), \
             mock.patch.object(peer_steward, "_resolve_target", side_effect=AssertionError("Herdr get")), \
             mock.patch.object(peer_steward, "_herdr_missing", side_effect=AssertionError("Herdr probe")), \
             contextlib.redirect_stdout(_ListWriter(output)):
            self.assertEqual(peer_steward.cmd_wait(args), 0)
        wait.assert_called_once_with("claude", "sid-native", ["idle"], 50,
                                     pane="w1:p9", server="default")
        self.assertIn("state=idle", "".join(output))

    def test_runner_publishes_one_blocked_change_for_exact_native_identity(self):
        payload = self.native_payload("blocked")
        snapshot = {"watch_root": self.watch_root, "watches": [(self.watch_id, self.arm)]}
        with mock.patch.object(peer_steward, "native_agent", return_value=payload) as observe, \
             mock.patch.object(peer_steward, "_wake_watch_observer", return_value=True) as wake:
            self.assertTrue(peer_steward._publish_watch_readiness(
                self.store, snapshot, read_context=None, incarnation="test-runner"))
            observation = json.loads(self.paths().observation.read_text())
            self.assertEqual(observation["native_wait"]["agent_state"], "blocked")
            self.paths().receipt.write_text("{}", encoding="utf-8")
            self.assertFalse(peer_steward._publish_watch_readiness(
                self.store, snapshot, read_context=None, incarnation="test-runner"))
        observe.assert_called_once()
        wake.assert_called_once()

    def test_working_unknown_and_pid_namespace_mismatch_never_notify(self):
        snapshot = {"watch_root": self.watch_root, "watches": [(self.watch_id, self.arm)]}
        for state, payload in (("working", self.native_payload("working")),
                               ("unknown", self.native_payload("unknown")),
                               ("namespace", self.native_payload("idle", pid_namespace="pid:[999999]"))):
            with self.subTest(state=state):
                with mock.patch.object(peer_steward, "native_agent", return_value=payload), \
                     mock.patch.object(peer_steward, "_wake_watch_observer") as wake:
                    peer_steward._publish_watch_readiness(
                        self.store, snapshot, read_context=None, incarnation="test-runner")
                wake.assert_not_called()
                if self.paths().observation.exists():
                    self.paths().observation.unlink()

    def test_same_pane_new_session_without_confirmed_lineage_stays_unknown(self):
        payload=self.native_payload("idle",agent_session={"value":"same-pane-new-sid"})
        snapshot={"watch_root":self.watch_root,"watches":[(self.watch_id,self.arm)]}
        with mock.patch.object(peer_steward,"native_agent",return_value=payload), \
             mock.patch.object(peer_steward_watch._WatchCloseEvidence,"chain_end",
                               return_value=("sid-native","unresolved")), \
             mock.patch.object(peer_steward,"_wake_watch_observer") as wake:
            self.assertFalse(peer_steward._publish_watch_readiness(
                self.store,snapshot,read_context=None,incarnation="test-runner"))
        wake.assert_not_called()
        self.assertFalse(self.paths().receipt.exists())

    def test_idle_until_transition_publishes_once_after_the_native_change(self):
        from dispatch_contract import process_start_ticks
        root=self.state; root.mkdir(parents=True,exist_ok=True)
        self.store.ensure_directory()
        peer_steward.shared_readiness.publish_runner_incarnation(
            self.store.root,self.pid,self.pid_start,"idle-runner")
        snapshot={"watch_root":self.watch_root,"watches":[(self.watch_id,self.arm)]}
        readiness=SimpleNamespace(state="ready",reason="native-turn-ready",
            identity=(("server","default"),("pane","w1:p9"),("harness","claude"),
                      ("session_id","sid-native"),("pid_birth",f"{self.pid}:{self.pid_start}")))
        with mock.patch.object(peer_steward,"native_agent",return_value=self.native_payload("idle")), \
             mock.patch.object(peer_steward,"_pane_readiness",return_value=readiness), \
             mock.patch.object(peer_steward,"_wake_watch_observer",return_value=True) as wake:
            self.assertTrue(peer_steward._publish_watch_readiness(
                self.store,snapshot,read_context=None,incarnation="idle-runner"))
            self.assertEqual(self.paths().observation.read_text() and
                             json.loads(self.paths().observation.read_text())["native_wait"]["agent_state"],
                             "idle")
            self.paths().receipt.write_text("{}",encoding="utf-8")
            self.assertFalse(peer_steward._publish_watch_readiness(
                self.store,snapshot,read_context=None,incarnation="idle-runner"))
        self.assertEqual(wake.call_count,1)

    def test_confirmed_end_and_successor_are_terminal_but_unknown_is_not(self):
        evidence = peer_steward_watch._WatchCloseEvidence()
        with mock.patch.object(peer_steward_watch._WatchCloseEvidence, "chain_end",
                               return_value=("sid-native", "self")), \
             mock.patch.object(peer_steward_watch._WatchCloseEvidence, "liveness",
                               return_value="unknown"):
            self.assertIsNone(peer_steward_watch._watch_close_reason(
                self.watch_id, self.arm, evidence))
        with mock.patch.object(peer_steward_watch._WatchCloseEvidence, "chain_end",
                               return_value=("sid-native", "self")), \
             mock.patch.object(peer_steward_watch._WatchCloseEvidence, "liveness",
                               return_value="ended"):
            self.assertEqual(peer_steward_watch._watch_close_reason(
                self.watch_id, self.arm, evidence), "session-ended")
        with mock.patch.object(peer_steward_watch._WatchCloseEvidence, "chain_end",
                               return_value=("sid-successor", "successor")):
            self.assertEqual(peer_steward_watch._watch_close_reason(
                self.watch_id, self.arm, evidence), "session-replaced")
        original_paths=peer_steward._watch_paths
        self.paths().observation.write_text(json.dumps({"native_wait":{
            "state":"result","terminal_reason":"session-replaced",
            "successor_session_id":"sid-successor"}}),encoding="utf-8")
        with mock.patch.object(peer_steward,"_watch_paths",
                side_effect=lambda current,selected=None: original_paths(current,selected or self.watch_root)), \
             mock.patch.object(peer_steward,"_prepare_watch_notice",return_value=None):
            receipt=peer_steward._write_terminal_watch_receipt(
                self.watch_id,self.arm,"session-replaced")
        self.assertEqual(receipt["successor_session_id"],"sid-successor")

    def test_old_protocol_two_arm_stays_unknown(self):
        old = dict(self.arm, observer_protocol=2)
        snapshot = {"watch_root": self.watch_root, "watches": [(self.watch_id, old)]}
        with mock.patch.object(peer_steward, "native_agent") as observe:
            self.assertFalse(peer_steward._publish_watch_readiness(
                self.store, snapshot, read_context=None, incarnation="test-runner"))
        observe.assert_not_called()
        self.assertFalse(self.paths().receipt.exists())

    def test_terminal_session_end_receipt_is_idempotent_and_has_no_herdr_field(self):
        original_paths=peer_steward._watch_paths
        prepared=mock.Mock(return_value=None)
        with mock.patch.object(peer_steward,"_watch_paths",
                               side_effect=lambda current,selected=None: original_paths(current,selected or self.watch_root)), \
             mock.patch.object(peer_steward,"_prepare_watch_notice",prepared):
            first=peer_steward._write_terminal_watch_receipt(
                self.watch_id,self.arm,"session-ended")
            second=peer_steward._write_terminal_watch_receipt(
                self.watch_id,self.arm,"session-ended")
        self.assertEqual(first["state"],"session-ended")
        self.assertEqual(second,first)
        self.assertNotIn("herdr_exit",first)
        prepared.assert_called_once()

    def test_exact_process_birth_distinguishes_dead_live_and_namespace_unknown(self):
        from dispatch_contract import process_start_ticks
        import peer_steward_native
        start=process_start_ticks(os.getpid())
        namespace=os.readlink(f"/proc/{os.getpid()}/ns/pid")
        with mock.patch("dispatch_contract.process_identity_disposition",return_value="dead"):
            self.assertEqual(peer_steward_native.native_lifetime(
                "claude","sid",pid=os.getpid(),pid_start=start,pid_namespace=namespace),"ended")
        with mock.patch("dispatch_contract.process_identity_disposition",return_value="live"):
            self.assertEqual(peer_steward_native.native_lifetime(
                "claude","sid",pid=os.getpid(),pid_start=start,pid_namespace=namespace),"live")
        self.assertEqual(peer_steward_native.native_lifetime(
            "claude","sid",pid=os.getpid(),pid_start=start,pid_namespace="pid:[999999]"),"unknown")

    def test_blocked_receipt_carries_only_its_exact_open_question(self):
        signals = [
            {"harness": "claude", "session_id": "other", "question_id": "other-q", "stale": False},
            {"harness": "claude", "session_id": "sid-native", "question_id": "stale-q", "stale": True},
            {"harness": "claude", "session_id": "sid-native", "question_id": "question-7", "stale": False},
        ]
        import human_wait_state
        with mock.patch.object(human_wait_state, "read_signals", return_value=signals):
            self.assertEqual(peer_steward._watch_receipt_question_fields(
                "blocked",self.arm["agent"]),{"question_id":"question-7"})
            self.assertEqual(peer_steward._watch_receipt_question_fields(
                "working",self.arm["agent"]),{})


class _ListWriter:
    def __init__(self, rows):
        self.rows = rows
    def write(self, value):
        self.rows.append(value)
    def flush(self):
        pass


if __name__ == "__main__":
    unittest.main()
