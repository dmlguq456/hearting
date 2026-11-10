"""A missing optional wrapper attachment cannot hide independent native identity."""
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "utilities")]
from fleet.collectors import codex, herdr, procscan
from fleet.model import Session
import session_shell

SID = "01000000-0000-7000-8000-000000000001"
OTHER = "01000000-0000-7000-8000-000000000002"


class NativeDisplayIdentityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / "sessions" / ("rollout-test-" + SID + ".jsonl")
        self.path.parent.mkdir()
        self.path.write_text("\n".join(json.dumps(row) for row in (
            {"type": "session_meta", "payload": {"id": SID, "cwd": "/project"}},
            {"type": "turn_context", "payload": {"model": "fixture-model", "effort": "medium"}},
        )) + "\n")
        self.row = Session(harness="codex", pid=420001, proc_start="100", cwd="/project")
        self.stack = self.enterContext(ExitStack())
        for target, name, value in (
            (codex, "_home", str(self.home)),
            (codex, "process_rollouts", ({}, set())),
            (codex, "_transfer_managed_rollouts", None),
            (codex, "_tick_subagents", None),
            (codex.session_registry, "read", None),
            (herdr, "codex_pane_sessions", {}),
            (procscan, "read_environ", {}),
            (procscan, "read_proc_start", "100"),
            (session_shell, "bindings", []),
        ):
            self.stack.enter_context(mock.patch.object(target, name, return_value=value))
        self.native = self.stack.enter_context(mock.patch.object(
            codex, "session_id_of_process", return_value=SID))

    def collect(self):
        tick = codex.prepare_tick([self.row])
        codex.enrich(self.row, tick=tick)
        return self.row.session_id, self.row.model

    def test_current_native_sid_and_model_survive_absent_attachment(self):
        self.assertEqual(self.collect(), (SID, "fixture-model"))

    def test_native_identity_without_rollout_stays_known_without_borrowed_model(self):
        self.path.unlink()
        self.assertEqual(self.collect(), (SID, None))

    def test_unknown_or_multiple_roots_stay_unresolved(self):
        self.native.return_value = None  # Existing reader returns None for either case.
        self.assertEqual(self.collect(), (None, None))

    def test_unavailable_native_read_stays_unresolved(self):
        self.native.side_effect = OSError("native observation unavailable")
        self.assertEqual(self.collect(), (None, None))

    def test_birth_change_during_native_read_does_not_publish_identity(self):
        def changed(*args, **kwargs):
            procscan.read_proc_start.return_value = "101"
            return SID
        self.native.side_effect = changed
        self.assertEqual(self.collect(), (None, None))

    def test_an_existing_attachment_is_not_bypassed_when_its_join_failed(self):
        session_shell.bindings.return_value = [{"native_pid": self.row.pid, "sid": OTHER}]
        self.assertEqual(self.collect(), (None, None))
        self.native.assert_not_called()

    def test_normal_pane_identity_keeps_its_existing_path(self):
        herdr.codex_pane_sessions.return_value = {self.row.pid: SID}
        self.assertEqual(self.collect(), (SID, "fixture-model"))
        self.native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
