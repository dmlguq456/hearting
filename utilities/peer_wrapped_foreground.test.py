#!/usr/bin/env python3
"""The common wrapped foreground follows native lifetime through existing controllers."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
import pane_ownership as po
import peer_steward_base as base
import session_tidy as tidy
import session_tidy_runner as tidy_runner

spec = importlib.util.spec_from_file_location("foreground_fixture",
    ROOT / "utilities/pane_ownership_wrapped_claude.test.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
peer = fixture.fixture.f.peer


class WrappedForegroundTest(unittest.TestCase):
    publish = fixture.WrappedClaudeSeatTest.publish
    close_children = fixture.WrappedClaudeSeatTest.close_children

    def setUp(self):
        fixture.WrappedClaudeSeatTest.setUp(self)
        self.harness, self.gone, self.actions = "claude", False, []
        self.current_sid, self.screen_reason = self.sid, None
        self.before_ready = lambda: None
        self.after_text = lambda: None
        self.info["shell_pid"] = self.host["pid"]
        self.ident = {"pane": self.pane, "harness": self.harness,
                      "session_id": self.sid, "name": "private-target"}
        self.store = peer.peer_obligations.ObligationStore(Path(self.home.name) / "duties")
        self.store.ensure_directory()
        mock.patch.object(peer.peer_obligations, "ObligationStore", return_value=self.store).start()
        mock.patch.object(peer.peer_obligations, "ensure_normal_runner", return_value=False).start()
        mock.patch.object(peer, "_herdr_missing", return_value=False).start()
        mock.patch.object(peer, "_caller_pane", return_value="private:sender").start()
        mock.patch.object(peer, "_retire_target", side_effect=lambda *a, **k:
                          ("idle", self.ident, 0, None)).start()
        mock.patch.object(peer, "_retire_pane_info", side_effect=self.process_info).start()
        mock.patch.object(po, "_pane_process_info", side_effect=self.process_info).start()
        mock.patch.object(po, "_runtime", side_effect=lambda pid:
                          (self.harness, False) if pid == self.native["pid"] else ("", False)).start()
        mock.patch("fleet.collectors.codex.session_id_of_process",
                   side_effect=lambda *a, **k: self.current_sid).start()
        self.registry.side_effect = lambda *a, **k: {**self.native_row, "sessionId": self.current_sid}
        actual_start = base._proc_start_ticks
        for module in (peer, base):
            mock.patch.object(module, "_proc_start_ticks", side_effect=lambda pid:
                None if self.gone and pid == self.native["pid"] else actual_start(pid)).start()
            mock.patch.object(module, "_retire_process_record", side_effect=lambda pid:
                {"start": self.native["start"], "group": self.native["pid"], "argv": [self.harness]}
                if not self.gone and pid == self.native["pid"] else None).start()
        original_stat = os.stat
        def stat(path, *args, **kwargs):
            if self.gone and str(path) == f"/proc/{self.native['pid']}":
                raise FileNotFoundError(path)
            return original_stat(path, *args, **kwargs)
        mock.patch.object(os, "stat", side_effect=stat).start()
        mock.patch.object(peer, "_pane_readiness", side_effect=self.ready).start()
        mock.patch.object(peer, "_read_screen", return_value=["synthetic composer"]).start()
        mock.patch.object(peer, "_screen_ready", side_effect=lambda *a, **k: self.screen_reason).start()
        mock.patch.object(peer, "_native_trust_reason", return_value=None).start()
        mock.patch.object(peer, "_tmux_input_refusal", return_value=None).start()
        mock.patch.object(peer, "_pane_send", side_effect=self.send).start()
        mock.patch.object(peer, "_close_pane", side_effect=lambda pane:
                          self.actions.append(("close", pane)) or True).start()
        mock.patch.object(peer, "_retire_handover", return_value=None).start()
        mock.patch.object(peer, "_mark_completed_retire_followup").start()
        mock.patch.object(peer, "_run_herdr_wait", return_value=None).start()
        mock.patch.object(peer.herdr_observation, "socket_path_for_endpoint", return_value=None).start()

    def select(self, harness):
        self.harness = harness
        self.ident["harness"] = harness
        self.publish(harness)
        self.actions, self.gone = [], False
        for path in self.store.root.glob("retire-*.json"):
            path.unlink()  # This fixture's private store only.

    def process_info(self, *args, **kwargs):
        return {**self.info, "foreground_process_group_id": self.host["pid"],
                "foreground_processes": [{"pid": self.host["pid"], "argv": ["zsh"]}]} if self.gone else self.info

    def ready(self, *args, **kwargs):
        self.before_ready()
        return SimpleNamespace(state="ready", reason="")

    def send(self, pane, operation, value):
        self.actions.append((operation, value))
        if operation == "send-text":
            self.after_text()
        else:
            self.gone = True
        return True

    def retire(self, request=None):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            rc = peer.cmd_retire(SimpleNamespace(target=self.pane, _resume_request_id=request))
        return rc, output.getvalue()

    def test_public_retire_exits_the_native_and_closes_only_its_returned_shell(self):
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                self.select(harness)
                rc, output = self.retire()
                self.assertEqual(rc, 0)
                self.assertIn("retired=true reason=normal-exit", output)
                expected = ([("send-text", "/exit"), ("send-keys", "enter")] if harness == "claude"
                            else [("send-keys", "ctrl+d")])
                self.assertEqual(self.actions, expected + [("close", self.pane)])
                duty = self.store.list()[0]
                self.assertEqual(duty["intent"]["identity"]["foreground"]["pid"], self.native["pid"])
                prior = list(self.actions)
                self.retire(duty["id"])
                self.assertEqual(self.actions, prior)

    def test_prompt_and_handoff_share_the_inner_native_lifetime(self):
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                self.select(harness)
                proof = peer._prompt_foreground_proof(self.pane, harness, self.sid)
                self.assertIsNotNone(proof)
                self.assertEqual(proof["pid"], self.native["pid"])
                foreground = peer._retire_foreground(self.pane, harness, strict=False)
                successor = peer._retire_request_identity(self.ident, foreground)
                row = {"to": {"harness": harness, "session_id": self.sid}}
                seat = tidy.Seat("pane", "fixture-seat", self.pane, harness, "")
                with mock.patch.object(tidy_runner, "_handoff_context",
                        return_value=({}, {}, successor, {}, {})):
                    _context, found = tidy_runner.validate_handoff_receiver(
                        row, (seat, harness, self.sid), steward=peer)
                self.assertEqual(found, successor)
                self.assertEqual(self.actions, [])

    def test_current_sid_change_after_booking_prevents_exit(self):
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                self.select(harness)
                self.current_sid = self.sid
                self.before_ready = lambda: setattr(self, "current_sid", "different-native-session")
                _rc, output = self.retire()
                self.assertEqual(self.current_sid, "different-native-session")
                self.assertEqual(self.actions, [])
                self.assertIn("foreground-unverified", output)

    def test_prompt_effect_keeps_the_same_attachment_when_native_pid_and_sid_stay_equal(self):
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                self.select(harness)
                proof = peer._prompt_foreground_proof(self.pane, harness, self.sid)
                self.assertIsNotNone(proof)
                self.publish(harness, incarnation="b" * 32)
                with mock.patch.object(peer, "_agent_state", return_value=("idle", self.pane)):
                    reason, _proof = peer._native_idle_prompt_guard(
                        self.pane, harness, self.sid, self.pane, expected_foreground=proof)
                self.assertEqual(reason, "target-process-changed")
                self.assertEqual(self.actions, [])

    def test_known_metadata_conflict_is_not_booked_as_the_current_native(self):
        self.select("claude")
        self.ident["session_id"] = "another-native-session"
        _rc, output = self.retire()
        self.assertEqual(self.actions, [])
        self.assertIn("foreground-unverified", output)
        self.assertIsNone(self.store.list()[0]["intent"]["identity"]["foreground"])

    def test_attachment_replacement_between_exit_text_and_enter_is_not_followed(self):
        self.select("claude")
        self.after_text = lambda: self.publish("claude", incarnation="b" * 32)
        _rc, output = self.retire()
        self.assertEqual(self.actions, [("send-text", "/exit")])
        self.assertIn("foreground-changed", output)

    def test_a_draft_still_withholds_every_exit_action(self):
        self.select("claude")
        self.screen_reason = "draft"
        _rc, output = self.retire()
        self.assertEqual(self.actions, [])
        self.assertIn("reason=draft", output)

    def unbound_request(self):
        self.select("claude")
        self.current_sid = ""
        with mock.patch.object(peer, "_caller_pane", return_value=""):
            self.retire()
        duty = self.store.list()[0]
        self.assertIsNone(duty["intent"]["identity"]["foreground"])
        self.assertEqual(self.actions, [])
        return duty

    def missing_target(self):
        return mock.patch.object(peer, "_retire_target", return_value=("agent-not-found",
            {"pane": "-", "harness": "-", "session_id": "-", "name": self.pane}, 1, "pane-unverified"))

    def test_closed_unbound_request_settles_without_inventing_native_exit_or_a_new_binding(self):
        duty = self.unbound_request()
        census = peer.herdr_observation.PaneCensus("default", (), 1.0)
        with self.missing_target(), mock.patch.object(peer.herdr_observation, "pane_census", return_value=census):
            rc, output = self.retire()
            self.assertEqual(rc, 0)
            self.assertIn("retired=false reason=target-closed-unproven", output)
            settled = self.store.get(duty["id"])
            self.assertEqual((settled["state"], settled["result"]), ("cancelled", "target-closed-unproven"))
            self.assertEqual(settled["intent"], duty["intent"])
            self.retire()
            self.assertEqual(self.store.get(duty["id"]), settled)
        self.assertEqual(self.actions, [])

    def test_unproven_settlement_needs_original_endpoint_absence_and_the_same_native_requester(self):
        duty = self.unbound_request()
        good = peer.herdr_observation.PaneCensus("default", (), 1.0)
        for census in (None, peer.herdr_observation.PaneCensus("default", (), 1.0, complete=False),
                       peer.herdr_observation.PaneCensus("other", (), 1.0),
                       peer.herdr_observation.PaneCensus("default", ({"pane_id": self.pane},), 1.0)):
            with self.subTest(census=census), self.missing_target(), \
                 mock.patch.object(peer.herdr_observation, "pane_census", return_value=census):
                self.retire()
            self.assertNotEqual(self.store.get(duty["id"])["state"], "cancelled")
        with self.missing_target(), mock.patch.object(peer.herdr_observation, "pane_census", return_value=good), \
             mock.patch.object(peer, "_current_session_identity", return_value=("someone-else", "codex")):
            self.retire()
        self.assertNotEqual(self.store.get(duty["id"])["state"], "cancelled")
        self.assertEqual(self.actions, [])

    def test_existing_retire_runner_settles_its_unsent_closed_request_without_operator_retry(self):
        duty = self.unbound_request()
        census = peer.herdr_observation.PaneCensus("default", (), 1.0)
        with mock.patch.object(peer.herdr_observation, "pane_census", return_value=census), \
             contextlib.redirect_stdout(io.StringIO()):
            peer._resume_retire_obligation(duty, self.store)
        settled = self.store.get(duty["id"])
        self.assertEqual((settled["state"], settled["result"]), ("cancelled", "target-closed-unproven"))
        self.assertEqual(settled["intent"], duty["intent"])
        self.assertEqual(self.actions, [])


if __name__ == "__main__":
    unittest.main()
