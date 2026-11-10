"""Display placement before a wrapped native process publishes vendor metadata."""
import sys
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet.collectors import herdr, procscan
from fleet.model import Session


class InnerLocationTest(unittest.TestCase):
    def observe(self, *, panes=None, parent=None, birth="100", complete=True):
        row = Session(harness="codex", pid=420002, proc_start="100", cwd="/project",
                      session_id="current-native-session")
        panes = panes if panes is not None else [{"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1", "agent": "codex"}]
        info = {p["pane_id"]: {"shell_pid": 420000,
            "foreground_process_group_id": 420001,
            "foreground_processes": [{"pid": 420001, "name": "python3"}]} for p in panes}
        pids = herdr.PaneEvidence(({420000}, {420001}), complete=complete, pane_infos=info)
        with mock.patch.object(herdr, "_ppid_of", side_effect=lambda pid: (parent if parent is not None else
                {420002: 420001, 420001: 420000}).get(pid)), \
             mock.patch.object(procscan, "read_proc_start", return_value=birth), \
             mock.patch.object(herdr, "_join_pane_aliases"), \
             mock.patch.object(herdr, "_mark_corners"):
            herdr.enrich([row], agents=[], panes=panes, pids=pids, pane_bindings={})
        return row

    def test_inner_native_location_uses_the_same_observed_foreground_without_sid_metadata(self):
        row = self.observe()
        self.assertTrue(row.herdr_attached)
        self.assertEqual(row.pane_location, "w1:t1·p1")
        self.assertEqual(row.multiplexer, "herdr")
        self.assertEqual(row.session_id, "current-native-session")
        self.assertIsNone(row._pane_session_claim)

    def test_unknown_birth_foreign_ancestry_or_duplicate_panes_do_not_select_a_location(self):
        for kwargs in ({"birth": "101"}, {"parent": {}}, {"complete": False}, {"panes": [
                {"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1", "agent": "codex"},
                {"pane_id": "w1:p2", "tab_id": "w1:t1", "workspace_id": "w1", "agent": "codex"}]}):
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(self.observe(**kwargs).pane_location)


if __name__ == "__main__":
    unittest.main()
