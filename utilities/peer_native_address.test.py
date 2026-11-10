#!/usr/bin/env python3
"""A first exact native message does not depend on a prior shell lifecycle hook."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
import session_shell
import codex_queue_delivery
import codex_queue_dispatch
from fleet.collectors import codex, procscan

spec = importlib.util.spec_from_file_location("peer_native_address", ROOT / "utilities/peer-steward.py")
peer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer)


class NativeAddressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "HOME": self.tmp.name, "XDG_STATE_HOME": self.tmp.name + "/state",
            "AGENT_PEER_LEDGER_ROOT": self.tmp.name + "/peers",
            "AGENT_DISPATCH_STATE_ROOT": self.tmp.name + "/dispatch"}))
        self.sid = "01000000-0000-7000-8000-000000000001"
        self.owner = (420001, "100")
        self.current = self.sid
        self.submitted = []
        self.rpc_count = 0
        self.phase = "add"
        self.before_rpc = lambda: None
        self.patch(session_shell, "resolve_target", return_value=(None, "attachment-unavailable"))
        self.attachments = self.patch(session_shell, "bindings", return_value=[])
        self.patch(codex, "process_for_thread", side_effect=lambda sid: self.owner)
        self.patch(codex, "session_id_of_process", side_effect=lambda *a, **k: self.current)
        self.patch(procscan, "read_proc_start", side_effect=lambda pid: self.owner[1] if self.owner else None)
        self.patch(peer, "_surface_missing", return_value=False)
        self.patch(peer, "_tmux_inventory", return_value=None)
        self.patch(peer, "_tmux_target_address", side_effect=lambda address: address)
        self.patch(peer, "_retire_target", return_value=("agent-not-found", None, 1, None))
        self.patch(peer, "_current_session_identity", return_value=("sender", "codex"))
        self.patch(peer, "_from_name", return_value="sender")
        for name in ("_record", "_prompt_failure_ledger", "_schedule_message_obligation", "_prompt_carrier_note"):
            self.patch(peer, name)
        self.physical = self.patch(peer, "_herdr_prompt")
        self.patch(codex_queue_dispatch, "resolve_queue_socket", return_value=Path(self.tmp.name) / "queue.sock")
        self.patch(codex_queue_delivery, "send_at_least_once", side_effect=self.rpc)

    def patch(self, target, name, **kw):
        return self.stack.enter_context(mock.patch.object(target, name, **kw))

    def rpc(self, path, **kw):
        self.rpc_count += 1
        self.before_rpc()
        if self.phase == "steer":
            kw["before_steer"]("turn-fixture")
        else:
            kw["before_add"]()
        self.assertFalse(kw["allow_restart"])
        self.submitted.append(kw["thread_id"])
        return {"status": "queued", "queued_submission_id": "item-fixture"}

    def prompt(self):
        args = SimpleNamespace(target="codex:" + self.sid, body_file=None, body_stdin=False,
                               text="Continue the registered local task.", ref=[], no_trailer=False)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = peer.cmd_prompt(args)
        self.physical.assert_not_called()
        return code, output.getvalue()

    def test_first_exact_message_uses_native_carrier_before_attachment_or_inventory(self):
        code, output = self.prompt()
        self.assertEqual(self.submitted, [self.sid])
        self.assertIn("prompted=queued", output)
        self.assertNotIn("prompted=true", output)
        rows = list(peer.peer_message._pending_rows())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["to"]["session_id"], self.sid)
        self.assertEqual(rows[0]["original_target"], "codex:" + self.sid)

    def test_unknown_or_different_current_root_is_not_an_address_proof(self):
        for current in (None, "01000000-0000-7000-8000-000000000002"):
            with self.subTest(current=current):
                self.current = current
                self.prompt()
                self.assertEqual(self.submitted, [])
        with mock.patch.object(codex, "process_for_thread", side_effect=OSError("unavailable")):
            self.prompt()
        self.assertEqual(self.submitted, [])

    def test_birth_or_current_root_changes_at_add_and_steer_boundary_do_not_submit(self):
        for phase in ("add", "steer"):
            for change in ("birth", "unknown", "other"):
                with self.subTest(phase=phase, change=change):
                    self.owner, self.current = (420001, "100"), self.sid
                    self.phase = phase
                    def change_owner():
                        if change == "birth":
                            self.owner = (420001, "101")
                        else:
                            self.current = None if change == "unknown" else "different-root"
                    self.before_rpc = change_owner
                    count = self.rpc_count
                    self.prompt()
                    self.assertEqual(self.rpc_count, count + 1)
                    self.assertEqual(self.submitted, [])

    def test_known_conflicting_or_ambiguous_attachment_is_not_bypassed(self):
        self.attachments.return_value = [{"native_pid": 420001, "native_start": "100", "sid": "other"}]
        self.prompt()
        self.assertEqual(self.submitted, [])
        self.attachments.return_value = []
        with mock.patch.object(session_shell, "resolve_target", return_value=(None, "attachment-ambiguous")):
            self.prompt()
        self.assertEqual(self.submitted, [])


if __name__ == "__main__":
    unittest.main()
