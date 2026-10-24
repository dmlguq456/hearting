"""Resume/clear current selection and native session directories, without live writes."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "utilities"))
from fleet import model, process_identity, session_cwd
from fleet.collectors import claude, codex, herdr, opencode, procscan
from fleet.model import Session


class NativeCurrentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.home), "XDG_STATE_HOME": str(self.home / "state"),
            "FLEET_TITLE_STATE_DIR": str(self.home / "titles"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.pid, self.start = 994242, "777"
        self.record = {"pid": self.pid, "procStart": self.start, "sessionId": "current",
                       "cwd": "/repo", "status": "idle", "name": "지금 작업"}
        (self.home / ".statusline").mkdir()
        for sid in ("empty-start", "previous", "current"):
            (self.home / ".statusline" / (sid + ".json")).write_text(json.dumps({
                "session_id": sid, "pid": self.pid, "proc_start": self.start,
            }))
        self.proj = self.home / "projects" / claude._enc_cwd("/repo")
        self.proj.mkdir(parents=True)
        for sid in ("previous", "current"):
            (self.proj / (sid + ".jsonl")).write_text(json.dumps({
                "type": "user", "sessionId": sid, "cwd": "/repo",
            }) + "\n")
        self.write_registry()

    def write_registry(self):
        (self.home / "sessions").mkdir(exist_ok=True)
        (self.home / "sessions" / f"{self.pid}.json").write_text(json.dumps(self.record))

    def identity(self):
        with mock.patch.object(procscan, "read_proc_start", return_value=self.start), \
                mock.patch.object(procscan, "_comm_of", return_value="fixture"), \
                mock.patch.object(procscan, "_read_cwd", return_value=("/home/user", False)):
            return process_identity.process_identity(self.pid, "claude")

    def test_one_shot_claude_identity_waits_for_bounded_exact_pane_observation(self):
        pane = {"pane_id": "w1:p1", "agent": "claude", "cwd": "/repo",
                "agent_session": {"agent": "claude", "value": "current"}}

        def bind_observed_pane(selected, bindings):
            self.assertEqual(selected, [pane])
            bindings[self.pid] = {"w1:p1"}
            return herdr.PaneEvidence((), complete=True)

        with mock.patch.object(procscan, "read_proc_start", return_value=self.start), \
                mock.patch.object(procscan, "_comm_of", return_value="claude"), \
                mock.patch.object(procscan, "_read_cwd", return_value=("/repo", False)), \
                mock.patch.object(herdr, "list_panes", return_value=[pane]) as list_panes, \
                mock.patch.object(herdr, "pane_evidence", side_effect=bind_observed_pane):
            self.assertEqual(claude.session_id_of_process(self.pid, str(self.home)), "current")

        list_panes.assert_called_once_with(wait_timeout=0.75)

    def test_resume_ignores_both_empty_and_messaged_previous_taps(self):
        identity = self.identity()
        self.assertEqual((identity.session_id, identity.confidence), ("current", process_identity.PROVEN))
        s = Session(harness="claude", pid=self.pid, proc_start=self.start, cwd="/home/user")
        with mock.patch.object(procscan, "read_proc_start", return_value=self.start):
            claude.enrich(s, tick={self.pid: {"session_id": "previous", "pane": "fixture-pane",
                                           "proc_start": self.start}})
        self.assertEqual((s.session_id, s.runtime_name, s.status), ("current", "지금 작업", "idle"))
        self.assertEqual(s.session_identity_evidence["historical_claims"]["pane"], "previous")
        session_cwd.project([s])
        self.assertEqual(s.cwd, "/repo")

    def test_resume_back_overrides_historical_clear_direction(self):
        (self.proj / "previous.jsonl").write_text(json.dumps({
            "type": "continued-in", "sessionId": "previous", "continuedInSessionId": "current",
        }) + "\n")
        self.record["sessionId"] = "previous"
        self.write_registry()
        self.assertEqual(self.identity().session_id, "previous")

    def test_clear_works_before_any_new_transcript_exists(self):
        self.record["sessionId"] = "cleared"
        self.write_registry()
        self.assertEqual(self.identity().session_id, "cleared")

    def test_missing_start_wrong_pid_and_pid_reuse_cannot_override_conflict(self):
        for change in ({"procStart": None}, {"procStart": "888"}, {"pid": 994243}):
            with self.subTest(change=change):
                self.record.update(pid=self.pid, procStart=self.start)
                self.record.update(change)
                self.write_registry()
                self.assertEqual(self.identity().confidence, process_identity.HARNESS_ONLY)

    def test_without_registry_unrelated_taps_stay_unknown(self):
        (self.home / "sessions" / f"{self.pid}.json").unlink()
        self.assertEqual(self.identity().confidence, process_identity.HARNESS_ONLY)

    def test_continue_uses_exact_native_current_identity_over_stale_herdr_for_all_harnesses(self):
        spec = importlib.util.spec_from_file_location("cwd_fixture_peer", ROOT / "utilities/peer-steward.py")
        peer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(peer)
        class Ready:
            state = "ready"
            reason = ""

        info = {"pane_id": "fixture-pane", "shell_pid": 101,
                "foreground_process_group_id": 202,
                "foreground_processes": [{"pid": 202, "argv": ["claude"]}]}
        for harness in ("claude", "codex", "opencode"):
            req = {"harness": harness, "new_session": "current", "seat": {"pane": "fixture-pane"}}
            stale_herdr = {"harness": harness, "session_id": "previous", "pane": "fixture-pane"}
            with self.subTest(harness=harness), \
                    mock.patch.object(peer, "_retire_target", return_value=("idle", stale_herdr, 0, None)), \
                    mock.patch.object(peer, "_continue_card_reason", return_value=None), \
                    mock.patch.object(peer, "_retire_pane_info", return_value=info), \
                    mock.patch.object(peer, "_continue_native_session", return_value="current") as native, \
                    mock.patch.object(peer, "_pane_readiness", return_value=Ready()) as readiness:
                self.assertIsNone(peer._continue_look("fixture-pane", req))
                native.assert_called_once_with("fixture-pane", harness, process_info=info)
                kwargs = readiness.call_args.kwargs
                self.assertEqual(kwargs["expected_sid"], "current")
                self.assertEqual(kwargs["native_session_id"], "current")
                self.assertIs(kwargs["pane_info"], info)
                self.assertEqual(kwargs["target_observation"], ("idle", stale_herdr))

        for native_sid, expected in (("changed", "target-changed"), (None, "new-session-unknown")):
            req = {"harness": "claude", "new_session": "current", "seat": {"pane": "fixture-pane"}}
            stale_herdr = {"harness": "claude", "session_id": "current", "pane": "fixture-pane"}
            with self.subTest(native_sid=native_sid), \
                    mock.patch.object(peer, "_retire_target", return_value=("idle", stale_herdr, 0, None)), \
                    mock.patch.object(peer, "_continue_card_reason", return_value=None), \
                    mock.patch.object(peer, "_retire_pane_info", return_value=info), \
                    mock.patch.object(peer, "_continue_native_session", return_value=native_sid), \
                    mock.patch.object(peer, "_pane_readiness") as readiness:
                self.assertEqual(peer._continue_look("fixture-pane", req), expected)
                readiness.assert_not_called()

        for wrong in ({"harness": "codex", "session_id": "current", "pane": "fixture-pane"},
                      {"harness": "claude", "session_id": "current", "pane": "different-pane"}):
            req = {"harness": "claude", "new_session": "current", "seat": {"pane": "fixture-pane"}}
            with mock.patch.object(peer, "_retire_target", return_value=("idle", wrong, 0, None)), \
                    mock.patch.object(peer, "_continue_native_session") as native:
                self.assertEqual(peer._continue_look("fixture-pane", req), "target-changed")
                native.assert_not_called()

    def test_continue_native_identity_fails_closed_on_missing_birth_and_codex_selection(self):
        spec = importlib.util.spec_from_file_location("cwd_fixture_peer_birth", ROOT / "utilities/peer-steward.py")
        peer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(peer)
        info = {"pane_id": "fixture-pane", "foreground_processes": [{"pid": 202, "argv": ["claude"]}]}
        with mock.patch.object(peer, "_retire_foreground", return_value=None):
            self.assertIsNone(peer._continue_native_session("fixture-pane", "claude", process_info=info))

        foreground = {"pid": 202, "start": "birth-1"}
        with mock.patch.object(peer, "_retire_foreground", return_value=foreground), \
                mock.patch.object(peer.shutil, "which", return_value="/fixture/herdr"), \
                mock.patch("pane_ownership.current_codex_thread", return_value="") as current:
            self.assertIsNone(peer._continue_native_session("fixture-pane", "codex", process_info=info))
            current.assert_called_once_with("fixture-pane", 202, executable="/fixture/herdr", server=None)


class SessionDirectoryTest(unittest.TestCase):
    SID = "11111111-1111-1111-1111-111111111111"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.home),
            "XDG_STATE_HOME": str(self.home / "state"), "FLEET_TITLE_STATE_DIR": str(self.home / "titles"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        session_cwd._CACHE.clear()
        codex._TURN_CONTEXT_CACHE.clear()
        codex._SUBAGENT_INDEX.clear()

    def rollout(self, rows=(), cwd="/original", sid=None):
        path = self.home / "sessions/2026/10/10" / ("rollout-2026-10-10T00-00-00-" + self.SID + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {"type": "session_meta", "payload": {"id": sid or self.SID, "cwd": cwd}}
        path.write_text("".join(json.dumps(row) + "\n" for row in (meta, *rows)))
        return path

    def codex_session(self, path):
        s = Session(harness="codex", pid=994242, proc_start="777", cwd="/home/user", slug="user")
        tick = codex._CodexTick(default_home=str(self.home), proc_paths={s.pid: str(path)}, subagents_by_home={})
        codex.enrich(s, tick=tick)
        return s

    def test_codex_uses_latest_turn_cwd_after_process_attribution(self):
        path = self.rollout([
            {"type": "turn_context", "payload": {"cwd": "/repo/first", "model": "gpt-session"}},
            {"type": "turn_context", "payload": {"cwd": "/repo/current", "model": "gpt-session"}},
            {"type": "response_item", "payload": "x" * 131072},
        ])
        s = self.codex_session(path)
        self.assertEqual(s.cwd, "/home/user")
        session_cwd.project([s])
        self.assertEqual((s.session_id, s.cwd, s.slug), (self.SID, "/repo/current", "current"))
        self.assertEqual(model.project_of(s.cwd), "current")

    def test_codex_append_and_partial_rows_keep_last_complete_cwd(self):
        path = self.rollout()
        self.assertEqual(session_cwd.jsonl_cwd(str(path), "codex", self.SID), "/original")
        row = json.dumps({"type": "turn_context", "payload": {"cwd": "/repo/new"}})
        with path.open("a") as handle:
            handle.write(row)
        self.assertEqual(session_cwd.jsonl_cwd(str(path), "codex", self.SID), "/original")
        with path.open("a") as handle:
            handle.write("\n")
        self.assertEqual(session_cwd.jsonl_cwd(str(path), "codex", self.SID), "/repo/new")
        with mock.patch("builtins.open", side_effect=AssertionError("unchanged file reread")):
            self.assertEqual(session_cwd.jsonl_cwd(str(path), "codex", self.SID), "/repo/new")

    def test_codex_missing_invalid_or_foreign_directory_keeps_process_fallback(self):
        for cwd, sid in ((None, self.SID), ("relative", self.SID), ("/foreign", "different")):
            with self.subTest(cwd=cwd, sid=sid):
                s = self.codex_session(self.rollout(cwd=cwd, sid=sid))
                session_cwd.project([s])
                self.assertEqual(s.cwd, "/home/user")

    def test_claude_exact_transcript_directory_with_no_native_registry(self):
        folder = self.home / "projects" / claude._enc_cwd("/repo")
        folder.mkdir(parents=True)
        (folder / "own.jsonl").write_text(json.dumps({
            "type": "user", "sessionId": "own", "cwd": "/repo",
        }) + "\n")
        s = Session(harness="claude", pid=994242, cwd="/home/user", session_id="own")
        claude.enrich(s)
        session_cwd.project([s])
        self.assertEqual(s.cwd, "/repo")
        self.assertTrue(s._transcript_path.endswith("own.jsonl"))

    def test_claude_duplicate_exact_transcripts_and_foreign_rows_are_not_guessed(self):
        for directory in ("first", "second"):
            folder = self.home / "projects" / directory
            folder.mkdir(parents=True)
            (folder / "own.jsonl").write_text(json.dumps({"sessionId": "foreign", "cwd": "/foreign"}) + "\n")
        self.assertIsNone(claude._newest_transcript_path(str(self.home), "/home/user", "own"))
        self.assertIsNone(session_cwd.jsonl_cwd(str(folder / "own.jsonl"), "claude", "own"))

    def test_opencode_directory_is_the_selected_sessions_not_its_launch_argument(self):
        db = self.home / "opencode.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE session (id TEXT, slug TEXT, agent TEXT, model TEXT, cost REAL, "
                    "tokens_input INT, tokens_output INT, tokens_reasoning INT, time_updated INT, "
                    "parent_id TEXT, directory TEXT)")
        con.execute("INSERT INTO session VALUES ('ses_selected','selected',NULL,NULL,0,0,0,0,0,NULL,'/repo')")
        con.commit()
        con.close()
        with mock.patch.object(opencode, "_db", return_value=str(db)), \
                mock.patch.object(opencode, "session_of_process", return_value=("ses_selected", "opencode-tui-selection")), \
                mock.patch.object(opencode, "_proc_start_ticks", return_value="777"):
            s = Session(harness="opencode", pid=994242, proc_start="777", cwd="/home/user")
            opencode.enrich(s)
            session_cwd.project([s])
            self.assertEqual((s.session_id, s.cwd), ("ses_selected", "/repo"))
        with mock.patch.object(opencode, "_db", return_value=str(db)), \
                mock.patch.object(opencode, "session_of_process", return_value=("ses_selected", "opencode-argv")), \
                mock.patch.object(opencode, "_proc_start_ticks", return_value="777"):
            s = Session(harness="opencode", pid=994242, proc_start="777", cwd="/home/user")
            opencode.enrich(s)
            session_cwd.project([s])
            self.assertEqual(s.cwd, "/home/user")

    def test_changed_identity_rejects_old_directory_and_preserves_friendly_names(self):
        s = Session(harness="claude", pid=994242, cwd="/home/user", slug="내 작업", session_id="own")
        session_cwd.observe(s, "/repo", "own")
        s.session_id = "other"
        session_cwd.project([s])
        self.assertEqual(s.cwd, "/home/user")
        s.session_id = "own"
        session_cwd.project([s])
        self.assertEqual((s.cwd, s.slug), ("/repo", "내 작업"))


if __name__ == "__main__":
    unittest.main()
