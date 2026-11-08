#!/usr/bin/env python3
"""Wrapped pane addresses use existing native attachments, never outer-PTY guesses."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import session_shell

spec = importlib.util.spec_from_file_location("peer_shell_resolution", ROOT / "utilities/peer-steward.py")
peer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer)


class WrappedPaneResolutionTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"HOME": self.home.name,
            "XDG_STATE_HOME": self.home.name + "/state",
            "AGENT_PEER_LEDGER_ROOT": self.home.name + "/peers"}).start()
        self.children = []
        for _ in range(2):
            child = subprocess.Popen([sys.executable, "-c", "import sys;sys.stdin.buffer.read()"],
                                     stdin=subprocess.PIPE)
            self.children.append(child)
        self.addCleanup(self.close_children)
        self.broker = session_shell.process(os.getpid())
        self.host, self.native = [session_shell.process(c.pid) for c in self.children]
        self.pane = "private:p1"
        self.sid = "01000000-0000-7000-8000-000000000001"
        self.info = {"pane_id": self.pane,
                     "foreground_process_group_id": self.broker["pid"],
                     "foreground_processes": [{"pid": self.broker["pid"]},
                                              {"pid": self.host["pid"]}]}
        # Only attachment/transport observations are synthetic. Record validation
        # checks real live children, their ancestry, process births and file modes.
        mock.patch.object(peer, "_retire_pane_info", side_effect=lambda pane: self.info
                          if pane == self.pane else None).start()
        mock.patch("pane_ownership.current_codex_thread", return_value="").start()
        mock.patch("fleet.process_identity.process_identity",
                   return_value=SimpleNamespace(confidence="unknown", session_id="")).start()
        mock.patch.object(peer, "_prompt_carrier_note").start()
        mock.patch.object(peer, "_record").start()
        mock.patch.object(peer, "_current_session_identity", return_value=("sender", "codex")).start()
        mock.patch.object(peer, "_from_name", return_value=None).start()
        mock.patch.object(peer, "_schedule_message_obligation").start()

    def close_children(self):
        for child in self.children:
            child.stdin.close()
            child.wait(timeout=5)

    def publish(self, harness, **changes):
        value = {"schema": session_shell.SCHEMA, "harness": harness, "sid": self.sid,
            "incarnation": "a" * 32, "socket": self.home.name + "/native.sock",
            "host_pid": self.host["pid"], "host_start": self.host["start"],
            "native_pid": self.native["pid"], "native_start": self.native["start"],
            "broker_pid": self.broker["pid"], "broker_start": self.broker["start"], **changes}
        path = session_shell.binding_path(harness, value["host_pid"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return value

    def observed(self, harness):
        ident = {"harness": harness, "session_id": "-", "pane": self.pane, "name": "peer-name"}
        return mock.patch.object(peer, "_retire_target", return_value=("working", ident, 0, None))

    def test_pane_without_vendor_session_resolves_the_inner_native_session_on_all_adapters(self):
        for harness in sorted(session_shell.HARNESSES):
            with self.subTest(harness=harness), self.observed(harness):
                self.publish(harness)
                candidate, error, panes = peer._resolve_prompt_target(self.pane)
                self.assertIsNone(error)
                self.assertEqual(candidate["session_id"], self.sid)
                self.assertEqual(candidate["harness"], harness)
                self.assertEqual(panes, [self.pane])

    def test_public_prompt_pins_one_native_carrier_without_physical_input(self):
        for harness in sorted(session_shell.HARNESSES):
            with self.subTest(harness=harness), self.observed(harness):
                self.publish(harness)
                args = SimpleNamespace(target=self.pane, body_file=None, body_stdin=False,
                                       text="synthetic report " + harness, ref=[], no_trailer=False)
                with mock.patch.object(session_shell, "deliver_pending", return_value={"ok": True}) as shell, \
                     mock.patch.object(peer.peer_message, "deliver_pending_codex",
                                       return_value={"status": "queued"}) as queue, \
                     mock.patch.object(peer, "_herdr_prompt") as physical, \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    peer.cmd_prompt(args)
                self.assertIn("prompted=queued", output.getvalue())
                self.assertNotIn("identity-unverified", output.getvalue())
                calls = queue.call_args_list if harness == "codex" else shell.call_args_list
                self.assertEqual(len(calls), 1)
                row = peer.peer_message._read_pending(calls[0].args[0])
                self.assertEqual(row["to"]["session_id"], self.sid)
                self.assertEqual(row["to"]["harness"], harness)
                (shell if harness == "codex" else queue).assert_not_called()
                physical.assert_not_called()

    def test_stale_native_host_or_owner_birth_and_unrelated_foreground_are_not_identity(self):
        for field in ("native_start", "host_start", "broker_start", "foreign-group"):
            with self.subTest(field=field), self.observed("codex"):
                self.publish("codex", **({field: "different"} if field != "foreign-group" else {}))
                info = self.info if field != "foreign-group" else {
                    **self.info, "foreground_process_group_id": self.native["pid"]}
                with mock.patch.object(peer, "_retire_pane_info", return_value=info):
                    candidate, error = peer._prompt_current_candidate(self.pane,
                        expected_harness="codex", expected_sid=self.sid, require_process_session=True)
                self.assertIsNone(candidate)
                self.assertEqual(error, "target-identity-unverified")

    def test_known_identity_change_conflicts_instead_of_retargeting(self):
        self.publish("codex")
        with self.observed("codex"):
            candidate, error = peer._prompt_current_candidate(self.pane,
                expected_sid="01000000-0000-7000-8000-000000000002")
        self.assertIsNone(candidate)
        self.assertEqual(error, "target-identity-changed")

    def test_selected_endpoint_is_used_and_a_reused_pane_cannot_borrow_the_attachment(self):
        self.publish("codex")
        with self.observed("codex"), mock.patch.object(peer, "_HERDR_SESSION", "other-endpoint"), \
             mock.patch.object(peer, "_retire_pane_info", return_value={**self.info,
                               "foreground_process_group_id": self.native["pid"]}):
            candidate, error = peer._prompt_current_candidate(self.pane,
                expected_sid=self.sid, require_process_session=True)
        self.assertIsNone(candidate)
        self.assertEqual(error, "target-identity-unverified")

    def test_outer_native_proof_cannot_disagree_with_the_attachment(self):
        self.publish("codex")
        with self.observed("codex"), mock.patch("pane_ownership.current_codex_thread",
                return_value="01000000-0000-7000-8000-000000000002"):
            candidate, error = peer._prompt_current_candidate(self.pane, require_process_session=True)
        self.assertIsNone(candidate)
        self.assertEqual(error, "target-identity-changed")

    def test_names_and_tags_reuse_the_same_attachment_proof(self):
        for harness in sorted(session_shell.HARNESSES):
            self.publish(harness)
            agent = {"agent": harness, "pane_id": self.pane, "name": "peer-name"}
            census = peer.herdr_observation.PaneCensus("default", (agent,), 1.0)
            with self.subTest(harness=harness), self.observed(harness), \
                 mock.patch.object(peer, "_tmux_inventory", return_value=None), \
                 mock.patch.object(peer, "_prompt_agent_inventory", return_value=([agent], None)), \
                 mock.patch.object(peer.herdr_observation, "pane_census", return_value=census), \
                 mock.patch.object(peer, "_prompt_fleet_tag", return_value=("ab", None)):
                for address in ("peer-name", "[ab]"):
                    candidate, error, _panes = peer._resolve_prompt_target(address)
                    self.assertIsNone(error)
                    self.assertEqual(candidate["session_id"], self.sid)

    def test_multiple_attachments_never_select_the_first_record(self):
        self.publish("codex")
        self.publish("codex", host_pid=self.native["pid"], host_start=self.native["start"],
                     native_pid=self.host["pid"], native_start=self.host["start"])
        with self.observed("codex"):
            candidate, error = peer._prompt_current_candidate(self.pane,
                expected_sid=self.sid, require_process_session=True)
        self.assertIsNone(candidate)
        self.assertEqual(error, "target-identity-unverified")

    def test_unwrapped_native_address_does_not_add_an_outer_pane_observation(self):
        with mock.patch.object(peer, "_retire_pane_info") as observe:
            self.assertIsNone(peer._pane_shell_binding(self.pane, "codex", self.sid))
        observe.assert_not_called()

    def test_optional_attachment_reader_absence_preserves_independent_native_proof(self):
        for native_sid in (self.sid, ""):
            with self.subTest(native_sid=native_sid), self.observed("codex"), \
                 mock.patch.object(session_shell, "bindings", side_effect=ImportError("optional reader")), \
                 mock.patch("pane_ownership.current_codex_thread", return_value=native_sid):
                candidate, error = peer._prompt_current_candidate(self.pane,
                    expected_sid=self.sid, require_process_session=True)
            if native_sid:
                self.assertIsNone(error)
                self.assertEqual(candidate["session_id"], self.sid)
            else:
                self.assertIsNone(candidate)
                self.assertEqual(error, "target-identity-unverified")

    def retained(self, recipient):
        _text, ref = peer.peer_message.prepare_peer_message("retained synthetic report",
            {"harness": "codex", "session_id": "sender"}, recipient, defer=True,
            receipt="pane-identity-unverified", original_target=self.pane)
        row = peer.peer_message._read_pending(ref)
        store = peer.peer_obligations.ObligationStore(Path(self.home.name) / "duties")
        duty = store.create("message-" + ref, "message",
            {"server": "default", "pane": self.pane, "harness": recipient["harness"],
             "session_id": recipient.get("session_id") or ""},
            {"ref": ref, "target": self.pane, "to": recipient,
             "from": row["from"], "body_digest": row["body_sha256"]})
        return row, duty, store

    def test_normal_reconciliation_keeps_one_unresolved_ref_and_its_body(self):
        self.publish("codex")
        original, duty, store = self.retained({"harness": "unknown", "session_id": None,
                                               "name": self.pane})
        with self.observed("codex"), \
             mock.patch.object(peer, "_message_terminal_evidence", return_value=None), \
             mock.patch.object(peer, "_pane_readiness", return_value=SimpleNamespace(
                 state="ready", reason="")), \
             mock.patch.object(peer, "_codex_interrupt_start_gate", return_value=False), \
             mock.patch.object(peer, "_deliver_codex_pending") as queue, \
             mock.patch.object(peer.peer_message, "prepare_peer_message") as prepare:
            peer._resume_message_obligation(duty, store)
        queue.assert_called_once_with(original["ref"], self.pane, timeout=0.25,
                                      start_interrupted=False)
        prepare.assert_not_called()
        current = peer.peer_message._read_pending(original["ref"])
        self.assertEqual(current["to"], original["to"])
        self.assertEqual(current["bound_to"]["session_id"], self.sid)
        for field in ("ref", "created", "text", "body_sha256"):
            self.assertEqual(current[field], original[field])

    def test_legacy_sidless_record_without_prompt_time_proof_stays_unsent(self):
        self.publish("codex")
        original, duty, store = self.retained({"harness": "codex", "session_id": None,
                                               "name": "peer-name", "pane_id": self.pane})
        with self.observed("codex"), \
             mock.patch.object(peer, "_message_terminal_evidence", return_value=None), \
             mock.patch.object(peer, "_deliver_codex_pending") as queue:
            peer._resume_message_obligation(duty, store)
        queue.assert_not_called()
        self.assertIsNone(peer.peer_message._read_pending(original["ref"]).get("bound_to"))
        self.assertIn(store.get(duty["id"])["observation"]["reason"],
                      {"target-process-proof-unknown", "target-session-unresolved"})


if __name__ == "__main__":
    unittest.main()
