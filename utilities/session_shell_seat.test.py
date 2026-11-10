#!/usr/bin/env python3
"""A shell-wrapped pane is its attachment's seat: one join, reused by every step (PG-9)."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
import peer_steward_base as base
import session_shell
import terminal_surface

spec = importlib.util.spec_from_file_location("seat_fixture", ROOT / "utilities/peer_shell_resolution.test.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class ShellSeatTest(unittest.TestCase):
    close_children = fixture.WrappedPaneResolutionTest.close_children
    publish = fixture.WrappedPaneResolutionTest.publish

    def setUp(self):
        # Real live children and private birth-bound attachments; only terminal
        # observations are synthetic. No user terminal or native model is touched.
        fixture.WrappedPaneResolutionTest.setUp(self)

    def test_the_foreground_owner_birth_joins_exactly_one_attachment(self):
        for harness in sorted(session_shell.HARNESSES):
            with self.subTest(harness=harness):
                value = self.publish(harness)
                self.assertEqual(session_shell.pane_seat(self.info, harness)["sid"], self.sid)
                self.assertEqual(session_shell.pane_seat(self.info)["native_pid"], value["native_pid"])
                session_shell.binding_path(harness, value["host_pid"]).unlink()
        self.publish("claude")
        other = {**self.info, "foreground_process_group_id": self.native["pid"]}
        self.assertIsNone(session_shell.pane_seat(other))           # not the owner
        missing = {**self.info, "foreground_processes": [{"pid": self.host["pid"]}]}
        self.assertIsNone(session_shell.pane_seat(missing))         # owner not foreground
        self.assertIsNone(session_shell.pane_seat(self.info, birth=lambda pid: "reused"))
        self.publish("claude", broker_start="reused")               # invalid attachment
        self.assertIsNone(session_shell.pane_seat(self.info))

    def test_an_unselected_seat_keeps_its_pane_and_never_names_a_session(self):
        value = self.publish("opencode", sid="-")
        self.assertTrue(session_shell.valid_binding(value))
        self.assertEqual(session_shell.pane_seat(self.info)["sid"], "-")
        self.assertEqual(session_shell.exact_binding("opencode", self.sid), (None, "attachment-unavailable"))
        with mock.patch("fleet.session_tags.assign_observed") as tag:
            self.assertTrue(session_shell.publish_binding(value))
        tag.assert_not_called()

    def test_tmux_agent_reads_the_seat_behind_the_pty_owner(self):
        surface = terminal_surface.TmuxSurface(socket="/private/tmux")
        pane = "%1@/private/tmux"
        info = {**self.info, "pane_id": pane}
        with mock.patch.object(surface, "_target", return_value="%1"), \
             mock.patch.object(surface, "process_info", return_value=info), \
             mock.patch.object(terminal_surface, "pane_runtime", return_value=None), \
             mock.patch.object(surface, "_started_name", return_value=None), \
             mock.patch.object(terminal_surface, "native_turn", return_value="unknown"), \
             mock.patch.object(session_shell, "control", return_value={"ok": True, "state": "idle"}):
            self.publish("opencode")
            agent = surface.agent(pane)["result"]["agent"]
            self.assertEqual((agent["agent"], agent["pid"], agent["agent_status"]),
                             ("opencode", self.native["pid"], "idle"))
            self.assertEqual(agent["agent_session"]["value"], self.sid)
            self.publish("opencode", sid="-")
            agent = surface.agent(pane)["result"]["agent"]
            self.assertNotIn("agent_session", agent)
        with mock.patch.object(surface, "_target", return_value="%1"), \
             mock.patch.object(surface, "process_info", return_value={**info, "foreground_process_group_id": 1}), \
             mock.patch.object(terminal_surface, "pane_runtime", return_value=None):
            self.assertEqual(surface.agent(pane), {"error": {"code": "agent_not_found"}})

    def test_a_requested_exit_is_not_gone_while_the_seat_owner_lives(self):
        gone_native = {"pid": 2 ** 22 + 7, "start": "1", "attachment": {
            "broker_pid": self.broker["pid"], "broker_start": self.broker["start"]}}
        duty = {"intent": {"identity": {"foreground": gone_native}}}
        self.assertEqual(base._retire_subject_state(duty), "same")
        duty["intent"]["identity"]["foreground"]["attachment"]["broker_start"] = "reused"
        self.assertEqual(base._retire_subject_state(duty), "gone")
        bare = {"intent": {"identity": {"foreground": {"pid": 2 ** 22 + 7, "start": "1"}}}}
        self.assertEqual(base._retire_subject_state(bare), "gone")

    def test_wrapped_retire_foreground_is_the_seat_native_for_every_harness(self):
        info = {**self.info, "shell_pid": os.getpid()}
        record = {"sessionId": self.sid, "procStart": self.native["start"]}
        for harness in ("claude", "opencode"):
            with self.subTest(harness=harness), \
                 mock.patch("fleet.collectors.claude.read_registry", return_value=record):
                value = self.publish(harness)
                found, observed = fixture.peer._wrapped_foreground(self.pane, harness, info)
                self.assertTrue(observed)
                self.assertEqual((found["pid"], found["session_id"]), (self.native["pid"], self.sid))
                self.assertEqual(found["attachment"]["broker_pid"], self.broker["pid"])
                session_shell.binding_path(harness, value["host_pid"]).unlink()
        # Claude's own record naming another session (a /resume) is no exit target.
        self.publish("claude")
        with mock.patch("fleet.collectors.claude.read_registry",
                        return_value={**record, "sessionId": "another-session"}):
            self.assertEqual(fixture.peer._wrapped_foreground(self.pane, "claude", info), (None, True))

    def test_native_screen_replaces_the_framed_pane_for_judgments(self):
        self.publish("claude")
        runs = [[["› ", False], ["placeholder", True]]]
        with mock.patch.object(session_shell, "control", return_value={"ok": True, "screen": runs}):
            self.assertEqual(fixture.peer._read_screen(self.pane),
                             [[("›", False), (" ", False)] + [(c, True) for c in "placeholder"]])
        with mock.patch.object(session_shell, "control", return_value={"ok": False}), \
             mock.patch.object(fixture.peer, "_herdr_argv", return_value=["false"]):
            self.assertIsNone(fixture.peer._read_screen(self.pane))  # falls back to the pane

    def test_herdr_unknown_turn_takes_the_shell_lifecycle_state_only_for_the_seat(self):
        self.publish("codex")
        payload = {"result": {"type": "agent_info", "agent": {"agent": "codex", "agent_status": "unknown"}}}
        with mock.patch.object(session_shell, "control", return_value={"ok": True, "state": "idle"}):
            turned = fixture.peer._shell_seat_turn(payload, self.pane)
            self.assertEqual(turned["result"]["agent"]["agent_status"], "idle")
            known = {"result": {"agent": {"agent": "codex", "agent_status": "working"}}}
            self.assertIs(fixture.peer._shell_seat_turn(known, self.pane), known)
            other = {"result": {"agent": {"agent": "claude", "agent_status": "unknown"}}}
            self.assertIs(fixture.peer._shell_seat_turn(other, self.pane), other)


if __name__ == "__main__":
    unittest.main()
