"""Native footer selection fills display identity without changing input ownership."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from contextlib import ExitStack
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "utilities")]
from fleet.collectors import codex, herdr, procscan
from fleet.model import Session
import pane_ownership

SID = "01000000-0000-7000-8000-000000000001"
OTHER = "01000000-0000-7000-8000-000000000002"


class DisplaySelectionTest(unittest.TestCase):
    def setUp(self):
        self.stack = self.enterContext(ExitStack())
        self.row = Session(harness="codex", pid=420002, proc_start="native-birth", cwd="/project")
        self.pane = "w0:p1"
        self.socket = "/tmp/display-selection.sock"
        self.info = {"pane_id": self.pane, "foreground_process_group_id": 420001,
                     "foreground_processes": [{"pid": 420001, "name": "python3"}]}
        self.lines = ["Codex", "›", "", SID,
                      "← for agents · ? for shortcuts", "📁 project · codex · CTX ?"]
        self.records = {420001: (420001, 1, 420001, 100, 420001, "owner-birth"),
                        420002: (420002, 420001, 420002, 101, 420002, "native-birth")}
        self.env = {"HERDR_PANE_ID": self.pane, "HERDR_SOCKET_PATH": self.socket}
        patches = [
            mock.patch.object(procscan, "read_proc_start", return_value="native-birth"),
            mock.patch.object(procscan, "read_environ", side_effect=lambda _pid: self.env),
            mock.patch.object(pane_ownership, "current_codex_thread", return_value=""),
            mock.patch.object(pane_ownership, "_process", side_effect=self.records.get),
            mock.patch.object(pane_ownership, "_runtime", return_value=("codex", False)),
            mock.patch.object(pane_ownership, "_registered_worker", return_value=False),
            mock.patch.object(pane_ownership, "_pane_reader", return_value="/fake/herdr"),
            mock.patch.object(pane_ownership, "_pane_process_info", side_effect=lambda *a, **k: self.info),
            mock.patch.object(pane_ownership, "_read_pane_visible_lines", create=True,
                              side_effect=lambda *a, **k: self.lines),
            mock.patch.object(codex, "process_for_thread", return_value=(self.row.pid, self.row.proc_start)),
            mock.patch.object(herdr, "_ppid_of", side_effect=lambda pid: self.records.get(pid, (None, None))[1]),
        ]
        for patch in patches:
            self.stack.enter_context(patch)

    def runner(self, argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {"process_info": self.info}}))

    def collect(self):
        return herdr.codex_pane_sessions(
            [self.row], panes=[{"pane_id": self.pane, "agent": "codex", "cwd": "/project"}],
            runner=self.runner, force_current=True,
            observation=SimpleNamespace(endpoint="default", socket_path=self.socket))

    def test_normal_native_help_and_managed_footer_select_the_displayed_root(self):
        self.assertEqual(self.collect(), {self.row.pid: SID})
        self.assertEqual(pane_ownership._codex_current_footer(self.lines)[0], "none")

    def test_copy_selection_help_is_display_evidence_only(self):
        self.lines[-2] = "^c copy · enter copy & follow · esc clear"
        self.assertEqual(self.collect(), {self.row.pid: SID})
        self.assertEqual(pane_ownership._codex_current_footer(self.lines)[0], "none")

    def test_unknown_help_or_clipped_id_does_not_select_a_root(self):
        self.lines[-2] = "unrecognized help"
        self.assertEqual(self.collect(), {})
        self.lines[-2] = "← for agents · ? for shortcuts"
        self.lines[-3] = SID[:-5] + "…"
        self.assertEqual(self.collect(), {})

    def test_foreign_native_owner_or_endpoint_stays_unknown(self):
        codex.process_for_thread.return_value = (420003, "other-birth")
        self.assertEqual(self.collect(), {})
        codex.process_for_thread.return_value = (self.row.pid, self.row.proc_start)
        self.env["HERDR_SOCKET_PATH"] = self.socket + ".foreign"
        self.assertEqual(self.collect(), {})

    def test_selection_switch_between_reads_stays_unknown(self):
        count = 0
        def changed(*args, **kwargs):
            nonlocal count
            count += 1
            return self.lines if count == 1 else [*self.lines[:-3], OTHER, *self.lines[-2:]]
        pane_ownership._read_pane_visible_lines.side_effect = changed
        self.assertEqual(self.collect(), {})

    def test_birth_or_foreground_change_stays_unknown(self):
        def changed(*args, **kwargs):
            self.records[420002] = (*self.records[420002][:-1], "reused")
            return (self.row.pid, self.row.proc_start)
        codex.process_for_thread.side_effect = changed
        self.assertEqual(self.collect(), {})


if __name__ == "__main__":
    unittest.main()
