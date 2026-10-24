"""A process's harness session, in the one identity record shape (OpenCode included)."""
import json
from pathlib import Path
import os
import sys
import tempfile
import unittest
import unittest.mock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
for entry in (str(ROOT / "tools"), str(ROOT / "utilities")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from fleet import herdr_projection as hp  # noqa: E402
from fleet import process_identity as P  # noqa: E402
from fleet.collectors import opencode  # noqa: E402
from fleet.collectors import claude  # noqa: E402
import claude_process_identity as claude_identity  # noqa: E402

SID = "ses_abcDEF123"


class ClaudeNativeIdentityHelperTest(unittest.TestCase):
    def test_indexed_and_direct_taps_match_fleet_exact_pid_start_claims(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            taps = home / ".statusline"
            taps.mkdir()
            (taps / "one.json").write_text(json.dumps({
                "pid": 17, "proc_start": "100", "session_id": "sid-one"}))
            (taps / "two.json").write_text(json.dumps({
                "pid": "17", "proc_start": "100", "session_id": "sid-two"}))
            (taps / "stale.json").write_text(json.dumps({
                "pid": 17, "proc_start": "99", "session_id": "sid-stale"}))
            (taps / "bad.json").write_text("{")

            direct = claude_identity.tap_sids_by_pid(home, 17, "100")
            indexed = claude_identity.tap_sids_by_pid(
                home, 17, "100", index=claude_identity.tap_claim_index(home))
            self.assertEqual(direct, {"sid-one", "sid-two"})
            self.assertEqual(indexed, direct)
            self.assertEqual(claude._tap_sids_by_pid(home, 17, "100"), direct)
            self.assertEqual(claude_identity.tap_sids_by_pid(home, 17, "101"), set())

    def test_strict_census_tap_index_surfaces_unreadable_identity_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            taps = Path(temp) / ".statusline"
            taps.mkdir()
            (taps / "bad.json").write_text("{")
            with self.assertRaises(OSError):
                claude_identity.tap_claim_index(temp, strict=True)


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

    def test_current_selection_overrides_the_session_the_process_started_on(self):
        self.record()
        with unittest.mock.patch.object(
                opencode, "_cmdline", return_value=["opencode", "--session", "ses_stale"]):
            self.assertEqual(opencode.session_of_process(self.pid, self.env),
                             (SID, "opencode-tui-selection"))

    def test_the_started_session_argument(self):
        cases = ((["opencode", "--session", SID], SID), (["opencode", f"--session={SID}"], SID),
                 (["/usr/bin/opencode", "-s", SID, "--model", "x"], SID), (["opencode"], None),
                 (["opencode", "run", "--session", SID], None), (["opencode", "--session", "bad"], None),
                 (["opencode", "-s", SID, "-s", SID], None), (["node", "--session", SID], None))
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(opencode._argv_session(argv), expected)

    def test_retained_reader_uses_only_the_original_lifetime_and_keeps_legacy_mode(self):
        self.record()
        path = self.dir / f"{self.pid}-{self.start}.json"
        path.chmod(0o664)
        with unittest.mock.patch.object(opencode, "_proc_start_ticks", return_value=None):
            self.assertEqual(opencode.session_of_process(self.pid, self.env), (None, ""))
            record = opencode._tui_selection_record(self.pid, self.start, self.env)
        self.assertEqual(record["value"]["sessionID"], SID)
        self.assertGreater(record["source"]["written_at"], 0)
        self.assertEqual(record["source"]["mtime"], path.stat().st_mtime)
        for pid, start in ((True, self.start), (0, self.start), (self.pid, True),
                           (self.pid, ""), (self.pid, "../x"), (self.pid, "0"),
                           (self.pid, str(int(self.start) + 1))):
            with self.subTest(pid=pid, start=start):
                self.assertIsNone(opencode._tui_selection_record(pid, start, self.env))

    def test_native_record_types_paths_and_provenance_refuse_unknown(self):
        path = self.dir / f"{self.pid}-{self.start}.json"
        for value in (False, None, [], {}, {"schema": "hearting-tui-selection-v1",
                "pid": self.pid, "start": self.start, "sessionID": [SID]},
                {"schema": "hearting-tui-selection-v1", "pid": True,
                 "start": self.start, "sessionID": SID}):
            with self.subTest(value=value):
                path.write_text(json.dumps(value))
                self.assertIsNone(opencode._tui_selection_record(self.pid, self.start, self.env))
        for raw in (b"{", b"\xff", b" " * 1025):
            path.write_bytes(raw)
            self.assertIsNone(opencode._tui_selection_record(self.pid, self.start, self.env))
        path.unlink()
        path.symlink_to(self.dir / "absent")
        self.assertIsNone(opencode._tui_selection_record(self.pid, self.start, self.env))
        path.unlink()
        os.mkfifo(path)
        self.assertIsNone(opencode._tui_selection_record(self.pid, self.start, self.env))
        path.unlink()
        self.record()
        uid = os.getuid()
        with unittest.mock.patch.object(opencode.os, "getuid", return_value=uid + 1):
            self.assertIsNone(opencode._tui_selection_record(self.pid, self.start, self.env))
        with unittest.mock.patch.object(opencode.os, "open", side_effect=PermissionError()):
            self.assertIsNone(opencode._tui_selection_record(self.pid, self.start, self.env))

    def test_atomic_replacement_during_read_is_unknown_even_with_identical_bytes(self):
        self.record()
        path = self.dir / f"{self.pid}-{self.start}.json"
        real = os.fstat
        calls = 0
        def replace(fd):
            nonlocal calls
            calls += 1
            result = real(fd)
            if calls == 2:
                other = path.with_suffix(".new")
                other.write_bytes(path.read_bytes())
                os.replace(other, path)
            return result
        with unittest.mock.patch.object(opencode.os, "fstat", side_effect=replace):
            self.assertIsNone(opencode._tui_selection_record(self.pid, self.start, self.env))

    def test_written_at_is_optional_for_live_identity_but_not_retained_provenance(self):
        for written in (None, "yesterday", "2026-10-07T00:00:00", False):
            with self.subTest(written=written):
                self.record(writtenAt=written)
                record = opencode._tui_selection_record(self.pid, self.start, self.env)
                self.assertIsNone(record["source"]["written_at"])
                self.assertEqual(opencode.session_of_process(self.pid, self.env), (SID, "opencode-tui-selection"))

    def test_one_record_shape_for_every_harness(self):
        self.record()
        with unittest.mock.patch.dict(os.environ, self.env):
            found = P.process_identity(self.pid, "opencode")
        self.assertEqual((found.harness, found.session_id, found.source, found.confidence),
                         ("opencode", SID, "opencode-tui-selection", P.PROVEN))
        with unittest.mock.patch("fleet.collectors.claude.session_id_of_process", return_value="c-1"):
            found = P.process_identity(self.pid, "claude")
        self.assertEqual((found.harness, found.session_id, found.confidence), ("claude", "c-1", P.PROVEN))
        observation = object()
        with unittest.mock.patch("fleet.collectors.codex.session_id_of_process",
                                 return_value="c-1") as codex_identity:
            found = P.process_identity(self.pid, "codex", codex_observation=observation)
        codex_identity.assert_called_once_with(self.pid, None, observation=observation)
        self.assertEqual((found.harness, found.session_id, found.confidence),
                         ("codex", "c-1", P.PROVEN))
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
                    unittest.mock.patch("fleet.collectors.claude.session_id_of_process", return_value=None), \
                    unittest.mock.patch.object(hp, "verified_pane", return_value="fixture-pane"):
                self.assertEqual(hp.runtime_identity(), ("opencode", None))
                self.assertTrue(hp.may_report("opencode", "ses_switched", worker=False))


class OpenCodeReportTest(unittest.TestCase):
    def test_a_proven_opencode_session_must_match_and_an_unproven_one_keeps_the_harness_check(self):
        # Native SID policy on an admitted pane; real ownership is exercised in
        # utilities/pane_ownership.test.py on separate foreground/background ptys.
        with unittest.mock.patch.object(hp, "runtime_identity", return_value=("opencode", SID)), \
                unittest.mock.patch.object(hp, "verified_pane", return_value="fixture-pane"):
            self.assertTrue(hp.may_report("opencode", SID, worker=False))
            self.assertFalse(hp.may_report("opencode", "ses_other", worker=False))
        with unittest.mock.patch.object(hp, "runtime_identity", return_value=("opencode", None)), \
                unittest.mock.patch.object(hp, "verified_pane", return_value="fixture-pane"):
            self.assertTrue(hp.may_report("opencode", "ses_other", worker=False))


class LaunchContextTest(unittest.TestCase):
    def test_argv_and_cwd_are_returned_only_for_a_stable_proven_session_birth(self):
        found = SimpleNamespace(confidence=P.PROVEN, session_id=SID)
        argv = ["/opt/opencode/bin/opencode", "--resume", SID, "--model", "fixture"]
        with unittest.mock.patch.object(P, "_proc_stat", side_effect=[(1, "123"), (1, "123")]), \
                unittest.mock.patch.object(P, "process_identity", return_value=found), \
                unittest.mock.patch.object(P, "_proc_argv", return_value=argv), \
                unittest.mock.patch.object(P.os, "readlink", return_value="/sandbox/project"):
            context = P.launch_context(4321, "opencode", SID)
        self.assertEqual(context, {"pid": 4321, "proc_start": "123", "harness": "opencode",
                                  "session_id": SID, "argv": argv,
                                  "cwd": "/sandbox/project"})

    def test_pid_reuse_or_session_change_discards_launcher_context(self):
        found = SimpleNamespace(confidence=P.PROVEN, session_id=SID)
        with unittest.mock.patch.object(P, "_proc_stat", side_effect=[(1, "123"), (1, "124")]), \
                unittest.mock.patch.object(P, "process_identity", return_value=found), \
                unittest.mock.patch.object(P, "_proc_argv", return_value=["opencode", "--session", SID]), \
                unittest.mock.patch.object(P.os, "readlink", return_value="/sandbox/project"):
            self.assertIsNone(P.launch_context(4321, "opencode", SID))


class NativeResumeArgvTest(unittest.TestCase):
    def test_shared_case_table_rekeys_launches_and_preserves_raw_input(self):
        cases = json.loads((Path(__file__).with_name("native_resume_cases.json")).read_text())
        for case in cases:
            argv = list(case["argv"])
            with self.subTest(name=case["name"]):
                self.assertEqual(P.native_resume_wire_argv(argv, case["harness"], case["sid"]),
                                 case["expected"])
                self.assertEqual(argv, case["argv"])
                self.assertEqual(P.is_native_resume_argv(argv, case["harness"], case["sid"]),
                                 case["expected"] is not None)

    def test_bad_executables_foreign_wrappers_wrong_sid_and_bounded_inputs_are_rejected(self):
        cases = (
            (["/usr/bin/python3", "/opt/claude", "--resume", SID], "claude", SID),
            (["/usr/bin/env", "claude", "--resume", SID], "claude", SID),
            (["bin/claude", "--resume", SID], "claude", SID),
            (["/opt/claude", "--resume", SID, "\0bad"], "claude", SID),
            (["/opt/claude", "--resume", SID, "x" * 8192], "claude", SID),
            (["/opt/claude", *(["x"] * 63), SID], "claude", SID),
            (["/opt/claude", "--resume", SID], "codex", SID),
            (["claude"], "claude", ""),
        )
        for argv, harness, sid in cases:
            with self.subTest(argv=argv[:3], harness=harness, sid=sid):
                self.assertFalse(P.is_native_resume_argv(argv, harness, sid))
                self.assertIsNone(P.native_resume_wire_argv(argv, harness, sid))
        self.assertIsNone(P.native_resume_wire_argv(["claude", "--fork-session"],
                                                    "claude", "x" * 8192))


if __name__ == "__main__":
    unittest.main()
