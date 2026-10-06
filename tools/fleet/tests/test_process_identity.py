"""A process's harness session, in the one identity record shape (OpenCode included)."""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for entry in (str(ROOT / "tools"), str(ROOT / "utilities")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from fleet import herdr_projection as hp  # noqa: E402
from fleet import process_identity as P  # noqa: E402
from fleet.collectors import opencode  # noqa: E402

SID = "ses_abcDEF123"


class OpenCodeProcessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self._tmp.name}
        self.pid = os.getpid()
        self.start = opencode._proc_start_ticks(self.pid)
        self.dir = Path(self._tmp.name) / "hearting" / "tui-identity"
        self.dir.mkdir(parents=True)

    def record(self, **overrides):
        row = {"schema": "hearting-tui-selection-v1", "sessionID": SID, "pid": self.pid,
               "start": self.start, "writtenAt": "2026-10-07T00:00:00Z", **overrides}
        (self.dir / f"{self.pid}-{self.start}.json").write_text(json.dumps(row))

    def test_the_tui_selection_record_names_the_pane_session(self):
        self.assertEqual(opencode.session_of_process(self.pid, self.env), (None, ""))
        self.record()
        self.assertEqual(opencode.session_of_process(self.pid, self.env), (SID, "opencode-tui-selection"))
        for bad in ({"start": "1"}, {"pid": self.pid + 1}, {"schema": "other"}, {"sessionID": "not-a-session"}):
            with self.subTest(bad=bad):
                self.record(**bad)
                self.assertEqual(opencode.session_of_process(self.pid, self.env), (None, ""))
        (self.dir / f"{self.pid}-{self.start}.json").write_text(" " * 2000)
        self.assertEqual(opencode.session_of_process(self.pid, self.env), (None, ""))

    def test_the_started_session_argument(self):
        cases = ((["opencode", "--session", SID], SID), (["opencode", f"--session={SID}"], SID),
                 (["/usr/bin/opencode", "-s", SID, "--model", "x"], SID), (["opencode"], None),
                 (["opencode", "run", "--session", SID], None), (["opencode", "--session", "bad"], None),
                 (["opencode", "-s", SID, "-s", SID], None), (["node", "--session", SID], None))
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(opencode._argv_session(argv), expected)

    def test_one_record_shape_for_every_harness(self):
        self.record()
        with unittest.mock.patch.dict(os.environ, self.env):
            found = P.process_identity(self.pid, "opencode")
        self.assertEqual((found.harness, found.session_id, found.source, found.confidence),
                         ("opencode", SID, "opencode-tui-selection", P.PROVEN))
        with unittest.mock.patch("fleet.collectors.claude.session_id_of_process", return_value="c-1"):
            found = P.process_identity(self.pid, "claude")
        self.assertEqual((found.harness, found.session_id, found.confidence), ("claude", "c-1", P.PROVEN))
        with unittest.mock.patch("fleet.collectors.codex.session_id_of_process", return_value=None):
            found = P.process_identity(self.pid, "codex")
        self.assertEqual((found.harness, found.session_id, found.confidence), ("codex", "", P.HARNESS_ONLY))


class StartedOnIsNotProofTest(unittest.TestCase):
    def test_a_start_time_session_argument_is_reported_but_never_proof(self):
        # A pane started on X can switch to Y inside the TUI; only the selection record follows.
        with unittest.mock.patch.object(opencode, "session_of_process", return_value=(SID, "opencode-argv")):
            found = P.process_identity(os.getpid(), "opencode")
            self.assertEqual((found.session_id, found.confidence), (SID, P.STARTED_ON))
            with unittest.mock.patch.object(hp, "_comm", return_value="opencode"), \
                    unittest.mock.patch("fleet.collectors.claude.session_id_of_process", return_value=None):
                self.assertEqual(hp.runtime_identity(), ("opencode", None))
                self.assertTrue(hp.may_report("opencode", "ses_switched", worker=False))


class OpenCodeReportTest(unittest.TestCase):
    def test_a_proven_opencode_session_must_match_and_an_unproven_one_keeps_the_harness_check(self):
        with unittest.mock.patch.object(hp, "runtime_identity", return_value=("opencode", SID)):
            self.assertTrue(hp.may_report("opencode", SID, worker=False))
            self.assertFalse(hp.may_report("opencode", "ses_other", worker=False))
        with unittest.mock.patch.object(hp, "runtime_identity", return_value=("opencode", None)):
            self.assertTrue(hp.may_report("opencode", "ses_other", worker=False))


if __name__ == "__main__":
    unittest.main()
