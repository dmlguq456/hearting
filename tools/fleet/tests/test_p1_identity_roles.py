"""Fleet P1: current identity and process roles, with isolated observations."""
import json
import os
import sys
import tempfile
import subprocess
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import model, projection, process_identity
from fleet.collectors import claude, codex, herdr, procscan
from fleet.model import Session, DispatchJob
from test_process_identity import FakeClaudeProc


class NativeBackgroundChainTest(unittest.TestCase):
    def test_unknown_native_is_not_a_new_row_but_legacy_claude_unknown_rows_remain(self):
        identity = procscan.claude_process_identity
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            f = FakeClaudeProc(temporary)
            executable = "/opt/claude/versions/8.7.6"
            f.process(71, argv=["unrecognized-runtime-title"])
            f.process(72, argv=["unrecognized-runtime-title"])
            f.process(73)
            (f.root / "73/cmdline").unlink()  # unreadable legacy comm=claude
            f.process(74, argv=["8.7.6"], executable="/tmp/8.7.6")
            f.process(75, argv=["claude", "daemon", "run"])
            real_observe = identity.observe_process
            stack.enter_context(mock.patch.object(identity, "observe_process",
                side_effect=lambda pid: real_observe(pid, proc_root=f.root)))
            lines = [f"71 8.7.6 01:00 {executable}", f"72 claude 01:00 {executable}",
                     "73 claude 01:00 claude", "74 8.7.6 01:00 /tmp/8.7.6",
                     "75 claude 01:00 claude daemon run"]
            for name, value in (("_ps_lines", lines), ("_pid_ttys", {}),
                    ("_detached_ttys", set()), ("proc_tree", {}), ("is_terminal_state", False),
                    ("exec_child", None), ("_orca_dead_socks", set()), ("read_environ", {}),
                    ("_read_argv", []), ("_read_cwd", (temporary, False))):
                stack.enter_context(mock.patch.object(procscan, name, return_value=value))
            stack.enter_context(mock.patch.object(procscan, "read_proc_start", return_value="101"))
            rows = procscan.scan()
            self.assertEqual([(r.pid, r.harness, r._invocation_mode) for r in rows],
                             [(72, "claude", "unknown"), (73, "claude", "unknown")])

    def test_old_daemon_tap_and_next_statusline_event_have_one_correct_native_row(self):
        from fleet import session_cwd
        from fleet.explorer.live import _session_records
        identity = procscan.claude_process_identity
        source = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            f = FakeClaudeProc(temporary)
            sid = "7a47c285-2f29-4d37-ab20-8411e48c9d70"
            project = str(Path(temporary) / "SR_CorrNet_SD")
            Path(project).mkdir()
            (Path(project) / ".git").mkdir()
            executable = "/opt/claude/versions/8.7.6"
            argv = {71: ["claude", "daemon", "run"],
                    72: ["claude bg-pty-host", "--bg-pty-host", "/tmp/fixture.sock"],
                    73: [executable, "--session-id", sid, "--fork-session", "--resume", "/old.jsonl"],
                    74: ["bash", "statusline.sh"],
                    75: ["rg", executable],
                    76: [executable, "--session-id", sid],
                    77: ["node", "/tmp/unrelated.js", executable]}
            for pid in argv:
                f.process(pid, parent=pid - 1 if pid > 71 else 1, start=str(pid + 100),
                          argv=argv[pid], executable={74: "/bin/bash", 76: "/usr/bin/python3",
                                                      77: "/usr/bin/node"}.get(pid, executable))
            f.registry(73, start="173", sid=sid, cwd=project, name="Automatic title", nameSource="auto")
            f.tap(sid=sid, pid=71, start="171")
            lines = [f"{pid} {'claude' if pid == 71 else '8.7.6'} 01:00 {' '.join(argv[pid])}"
                     for pid in (71, 72, 73, 75, 76, 77)]
            stack.enter_context(mock.patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": str(f.home), "FLEET_TITLE_STATE_DIR": temporary + "/titles",
                "XDG_STATE_HOME": temporary + "/state", "HERDR_PANE_ID": "",
            }))
            real_observe = identity.observe_process
            stack.enter_context(mock.patch.object(identity, "observe_process",
                side_effect=lambda pid, **kw: real_observe(pid, proc_root=f.root)))
            for name, value in (("_ps_lines", lines), ("_pid_ttys", {71: "?", 72: "?", 73: "pts/9"}),
                    ("_detached_ttys", set()), ("proc_tree", {}), ("is_terminal_state", False),
                    ("exec_child", None), ("_orca_dead_socks", set()), ("read_environ", {})):
                stack.enter_context(mock.patch.object(procscan, name, return_value=value))
            stack.enter_context(mock.patch.object(procscan, "read_proc_start", side_effect=lambda pid: str(pid + 100)))
            stack.enter_context(mock.patch.object(procscan, "_read_cwd", return_value=(temporary, False)))
            stack.enter_context(mock.patch.object(procscan, "_comm_of", return_value="8.7.6"))
            stack.enter_context(mock.patch.object(herdr, "pane_session_aliases", return_value=[]))
            stack.enter_context(mock.patch.object(identity, "claude_session_census", side_effect=AssertionError("no census")))

            def collect():
                rows = procscan.scan()
                for row in rows:
                    claude.enrich(row)
                process_identity.finalize_process_roles(rows, [])
                session_cwd.project(rows)
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual((row.pid, row.proc_start, row.session_id, row.process_role),
                                 (73, "173", sid, "conversation"))
                self.assertEqual(row.cwd, project)
                self.assertIsNone(row.session_tag)
                shown, keys = _session_records({"sessions": rows}, 0, model.project_of)
                self.assertEqual([(r["tag"], r["repo"], r["repo_id"]) for r in shown],
                                 [("?1", "SR_CorrNet_SD", "SR_CorrNet_SD")])
                self.assertEqual(keys["?1"]["sid"], sid)
                for selected in ({"claude"}, iter(["claude"])):
                    filtered = procscan.scan(harness_filter=selected)
                    self.assertEqual([(r.pid, r.harness) for r in filtered], [(73, "claude")])
                    self.assertFalse(filtered.census_complete)
                    self.assertIn("harness-filter-partial", filtered.census_reasons)
                for selected in ({"codex"}, {"opencode"}):
                    self.assertEqual(procscan.scan(harness_filter=selected), [])

            # Both consumers reject even an old resolved daemon-bound tap.
            self.assertEqual(process_identity.process_identity(71, "claude").confidence,
                             process_identity.HARNESS_ONLY)
            daemon = Session(harness="claude", pid=71, proc_start="171", session_id=sid)
            daemon.session_identity_evidence = {"verdict": "resolved", "session_id": sid}
            retained = [daemon]
            process_identity.finalize_process_roles(retained, [])
            self.assertEqual(retained, [])
            collect()

            # Drive the real statusline shell against the same private proc
            # files. Only the proc root/starting parent are fixture adapters;
            # every observation, SID check and tap write is production code.
            agent = Path(temporary) / "agent"
            (agent / "utilities").mkdir(parents=True)
            (agent / "utilities/claude_process_identity.py").write_text(
                "import importlib.util\n"
                f"s=importlib.util.spec_from_file_location('native_helper', {str(source / 'utilities/claude_process_identity.py')!r})\n"
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
                "def statusline_bindings(parent, sid, home):\n"
                f"    return m.statusline_bindings(74, sid, home, proc_root={str(f.root)!r})\n")
            result = subprocess.run(["bash", str(source / "adapters/claude/statusline.sh")],
                input=json.dumps({"session_id": sid, "cwd": project}), capture_output=True,
                text=True, timeout=30, env={**os.environ, "AGENT_HOME": str(agent),
                    "FLEET_TITLE_DISABLE": "1", "HARNESS_CAPACITY_REFRESH_DISABLE": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            tap = json.loads((f.home / ".statusline" / (sid + ".json")).read_text())
            self.assertEqual((tap["pid"], tap["proc_start"]), (73, "173"))
            collect()


class CurrentSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.home),
            "FLEET_TITLE_STATE_DIR": str(self.home / "titles"),
            "XDG_STATE_HOME": str(self.home / "state"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.pid, self.start, self.cwd = 994242, "777", "/proj/y"
        self.proj = self.home / "projects" / claude._enc_cwd(self.cwd)
        self.proj.mkdir(parents=True)
        (self.home / "sessions").mkdir()
        (self.home / "sessions" / f"{self.pid}.json").write_text(json.dumps({
            # Legacy registry has no PID/start current-selection proof. These
            # cases exercise continuity and conflict fallback; exact native
            # selection is covered by test_session_cwd_current_identity.py.
            "pid": self.pid, "sessionId": "old",
            "status": "idle", "name": "이틀 전 작업 완료", "updatedAt": 1000000,
        }))
        (self.home / ".statusline").mkdir()
        # These legacy taps prove raw percentages and identity, not an applied
        # compaction policy. Keep the raw diagnostic without inventing a gauge.
        for sid, pct in (("old", 26), ("current", 17)):
            (self.home / ".statusline" / (sid + ".json")).write_text(json.dumps({
                "session_id": sid, "context_window": {"used_percentage": pct},
            }))
            (self.proj / (sid + ".jsonl")).write_text(json.dumps({"type": "user"}) + "\n")

    def enrich(self, aliases, pane_sid="current"):
        s = Session(harness="claude", pid=self.pid, proc_start=self.start, cwd=self.cwd)
        with mock.patch.object(procscan, "read_proc_start", return_value=self.start), \
                mock.patch.object(procscan, "read_environ", return_value={}), \
                mock.patch.object(process_identity, "pane_session_successors", return_value={old: {pane_sid} for old in aliases}):
            claude.enrich(s, tick={self.pid: {"session_id": pane_sid, "pane": "wB:p3N",
                                           "proc_start": self.start}})
        return s

    def test_clear_uses_current_sid_for_all_telemetry_without_old_idle_or_name(self):
        s = self.enrich(["old"])
        self.assertEqual(s.session_id, "current")
        self.assertIsNone(s.ctx_pct)
        self.assertEqual(s.context_policy["raw_percentage"], 17)
        self.assertIsNone(s.status)
        self.assertIsNone(s.registry_name)
        self.assertTrue(s._transcript_path.endswith("current.jsonl"))

    def test_unrelated_pane_claim_is_unknown_without_borrowed_telemetry(self):
        s = self.enrich([])
        self.assertIsNone(s.session_id)
        self.assertIsNone(s.status)
        self.assertIsNone(s.ctx_pct)
        self.assertIsNone(s.mtime)
        self.assertEqual(s.session_identity_evidence["verdict"], "conflict")

    def test_continued_in_resolves_current_even_without_seat_history(self):
        (self.proj / "old.jsonl").write_text(json.dumps({"type": "continued-in",
            "sessionId": "old", "continuedInSessionId": "current"}) + "\n")
        self.assertEqual(self.enrich([]).session_id, "current")

    def test_stale_pane_claim_never_reverses_native_continuation(self):
        (self.proj / "old.jsonl").write_text(json.dumps({"type": "continued-in",
            "sessionId": "old", "continuedInSessionId": "current"}) + "\n")
        row = self.home / "sessions" / f"{self.pid}.json"
        record = json.loads(row.read_text())
        record["sessionId"] = "current"
        row.write_text(json.dumps(record))
        self.assertEqual(self.enrich([], pane_sid="old").session_id, "current")

    def test_missing_sid_does_not_borrow_neighbor_transcript(self):
        self.assertIsNone(claude._newest_transcript_path(str(self.home), self.cwd, None))

    def test_pane_claim_reuses_observation_and_rejects_recycled_pid(self):
        s = Session(harness="claude", pid=self.pid, proc_start=self.start, cwd=self.cwd)
        panes = [{"pane_id": "wB:p3N", "agent": "claude",
                  "agent_session": {"agent": "claude", "value": "current"}}]
        with mock.patch.object(procscan, "read_proc_start", return_value=self.start), \
                mock.patch.object(herdr, "pane_evidence", side_effect=AssertionError("duplicate probe")):
            self.assertEqual(process_identity.pane_process_claims(
                [s], panes, {self.pid: {"wB:p3N"}})[self.pid]["session_id"], "current")
        with mock.patch.object(procscan, "read_proc_start", return_value="recycled"):
            self.assertEqual(process_identity.pane_process_claims([s], panes, {self.pid: {"wB:p3N"}}), {})

    def test_process_identity_observes_target_pane_instead_of_callers_pane(self):
        panes = [{"pane_id": "wB:p3N", "agent": "claude", "cwd": self.cwd,
                  "agent_session": {"agent": "claude", "value": "current"}}]
        def observe(selected, bindings):
            self.assertEqual(selected, panes)
            bindings[self.pid] = {"wB:p3N"}
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "caller-pane"}), \
                mock.patch.object(procscan, "_comm_of", return_value="claude"), \
                mock.patch.object(procscan, "read_proc_start", return_value=self.start), \
                mock.patch.object(procscan, "_read_cwd", return_value=(self.cwd, False)), \
                mock.patch.object(herdr, "list_panes", return_value=panes), \
                mock.patch.object(herdr, "pane_evidence", side_effect=observe), \
                mock.patch.object(process_identity, "pane_session_successors", return_value={"old": {"current"}}):
            self.assertEqual(claude.session_id_of_process(self.pid, str(self.home)), "current")

    def test_incomplete_target_pane_does_not_reconfirm_old_registry(self):
        s = self._enrich_pane_observation(bound=False, complete=False)
        self.assertIsNone(s.session_id)
        self.assertIsNone(s.ctx_pct)
        self.assertIsNone(s.status)
        self.assertEqual(s.session_identity_evidence["verdict"], "unobserved")

    def test_incomplete_other_pane_keeps_current_identity_and_telemetry(self):
        s = self._enrich_pane_observation(bound=True, complete=False)
        self.assertEqual(s.session_id, "current")
        self.assertIsNone(s.ctx_pct)
        self.assertEqual(s.context_policy["raw_percentage"], 17)
        self.assertIsNone(s.status)

    def test_complete_pane_observation_resolves_first_snapshot(self):
        s = self._enrich_pane_observation(bound=True, complete=True)
        self.assertEqual(s.session_id, "current")
        self.assertIsNone(s.ctx_pct)
        self.assertEqual(s.context_policy["raw_percentage"], 17)

    def test_agent_list_failure_does_not_reconfirm_herdr_registry(self):
        s = self._enrich_failed_agent_list("herdr")
        self.assertIsNone(s.session_id)
        self.assertIsNone(s.ctx_pct)
        self.assertIsNone(s.status)
        self.assertEqual(s.session_identity_evidence["verdict"], "unobserved")

    def test_pane_list_failure_does_not_reconfirm_herdr_registry(self):
        s = Session(harness="claude", pid=self.pid, proc_start=self.start, cwd=self.cwd)
        with mock.patch.object(procscan, "read_proc_start", return_value=self.start), \
                mock.patch.object(herdr, "list_panes", return_value=None):
            herdr.enrich([s], agents=[], lineage=lambda _pid: "herdr")
            claude.enrich(s, tick={self.pid: getattr(s, "_pane_session_claim", None)})
        self.assertIsNone(s.session_id)
        self.assertIsNone(s.ctx_pct)
        self.assertIsNone(s.status)
        self.assertEqual(s.session_identity_evidence["verdict"], "unobserved")

    def test_agent_list_failure_preserves_already_observed_pane_identity(self):
        s = self._enrich_failed_agent_list("herdr", bound=True)
        self.assertEqual(s.session_id, "current")
        self.assertIsNone(s.ctx_pct)
        self.assertEqual(s.context_policy["raw_percentage"], 17)
        self.assertIsNone(s.status)

    def test_absent_herdr_preserves_plain_terminal_native_identity(self):
        s = self._enrich_failed_agent_list("terminal")
        self.assertEqual(s.session_id, "old")
        self.assertIsNone(s.ctx_pct)
        self.assertEqual(s.context_policy["raw_percentage"], 26)

    def test_standalone_failed_target_pane_is_unobserved(self):
        with mock.patch.object(procscan, "_comm_of", return_value="claude"), \
                mock.patch.object(procscan, "provenance", return_value="herdr"), \
                mock.patch.object(procscan, "read_proc_start", return_value=self.start), \
                mock.patch.object(procscan, "_read_cwd", return_value=(self.cwd, False)), \
                mock.patch.object(herdr, "list_panes", return_value=None):
            self.assertIsNone(claude.session_id_of_process(self.pid, str(self.home)))

    def _enrich_failed_agent_list(self, lineage, bound=False):
        s = Session(harness="claude", pid=self.pid, proc_start=self.start, cwd=self.cwd)
        panes = [{"pane_id": "wB:p3N", "agent": "claude", "cwd": self.cwd,
                  "agent_session": {"agent": "claude", "value": "current"}}] if bound else None
        bindings = {self.pid: {"wB:p3N"}} if bound else {}
        with mock.patch.object(procscan, "read_proc_start", return_value=self.start), \
                mock.patch.object(herdr, "observation_view", return_value=None), \
                mock.patch.object(herdr._observation_module(), "socket_path_for_endpoint", return_value=""), \
                mock.patch.object(herdr, "list_agents", return_value=None), \
                mock.patch.object(herdr, "list_panes", side_effect=AssertionError("extra pane probe")), \
                mock.patch.object(process_identity, "pane_session_successors", return_value={"old": {"current"}}):
            herdr.enrich([s], lineage=lambda _pid: lineage, panes=panes,
                         pids=herdr.PaneEvidence((set(), {self.pid})) if bound else None,
                         pane_bindings=bindings)
            claude.enrich(s, tick={self.pid: getattr(s, "_pane_session_claim", None)})
        return s

    def test_folded_seat_history_retains_direction_into_explicit_clear(self):
        process_identity._record()
        import session_tidy
        rows = [{"harness": "claude", "sid": "old", "event": "summary", "cwd": self.cwd, "ts": 1},
                {"harness": "claude", "sid": "other-project", "event": "summary", "cwd": "/proj/z", "ts": 1},
                {"harness": "claude", "sid": "current", "event": "start", "source": "clear", "cwd": self.cwd,
                 "resume_argv": ["claude", "--resume", "old"], "ts": 2}]
        with mock.patch.object(session_tidy, "_read_ledger_lines", return_value=rows):
            self.assertEqual(session_tidy.session_start_context("claude", "current", "wB:p3N")["resume_argv"],
                             ["claude", "--resume", "current"])
        with mock.patch.object(session_tidy, "_read_ledger_lines", return_value=rows), \
                mock.patch("fleet.gitinfo.resolve_gitdir", side_effect=lambda cwd: (None, cwd)):
            edges = process_identity.pane_session_successors("claude", "wB:p3N", self.cwd,
                                                           claims={"old", "current", "other-project"})
        self.assertEqual(edges, {"old": {"current"}})
        sid, _ = process_identity.resolve_session_claims({"registry": "current", "pane": "old"}, edges)
        self.assertEqual(sid, "current")
        rows[-1]["source"] = "fork"
        with mock.patch.object(session_tidy, "_read_ledger_lines", return_value=rows), \
                mock.patch("fleet.gitinfo.resolve_gitdir", side_effect=lambda cwd: (None, cwd)):
            self.assertEqual(process_identity.pane_session_successors("claude", "wB:p3N", self.cwd,
                                                                      claims={"old", "current"}), {})
        rows[-1]["source"] = "clear"
        for last_seen in (3, None):
            rows[0]["ts"] = last_seen
            with mock.patch.object(session_tidy, "_read_ledger_lines", return_value=rows), \
                    mock.patch("fleet.gitinfo.resolve_gitdir", side_effect=lambda cwd: (None, cwd)):
                self.assertEqual(process_identity.pane_session_successors("claude", "wB:p3N", self.cwd,
                                                                          claims={"old", "current"}), {})

    def test_raw_activity_after_clear_keeps_legacy_identity_unresolved(self):
        process_identity._record()
        import session_tidy
        rows = [{"harness": "claude", "sid": "old", "event": "summary", "cwd": self.cwd, "ts": 1},
                {"harness": "claude", "sid": "current", "event": "start", "source": "clear", "cwd": self.cwd, "ts": 2},
                {"harness": "claude", "sid": "old", "event": "prompt", "cwd": self.cwd, "ts": 3}]
        with mock.patch.object(session_tidy, "_read_ledger_lines", return_value=rows), \
                mock.patch("fleet.gitinfo.resolve_gitdir", side_effect=lambda cwd: (None, cwd)):
            edges = process_identity.pane_session_successors("claude", "wB:p3N", self.cwd,
                                                           claims={"old", "current"})
        sid, evidence = process_identity.resolve_session_claims({"registry": "current", "pane": "old"}, edges)
        self.assertIsNone(sid)
        self.assertEqual(evidence["verdict"], "conflict")

    def test_repeated_native_ledger_folding_preserves_clear_identity(self):
        process_identity._record()
        import session_tidy
        rows = [{"harness": "claude", "sid": "old", "event": "start", "source": "startup",
                 "cwd": self.cwd, "ts": 1},
                {"harness": "claude", "sid": "current", "event": "start", "source": "clear",
                 "cwd": self.cwd, "ts": 2},
                {"harness": "claude", "sid": "current", "event": "prompt", "cwd": self.cwd, "ts": 3}]
        seat = session_tidy._pane_seat_of("wB:p3N")
        def save(_path, body):
            rows[:] = [json.loads(line) for line in body.decode().splitlines()]
        with mock.patch.object(session_tidy, "_read_ledger_lines", side_effect=lambda _seat: rows[:]), \
                mock.patch.object(session_tidy, "atomic_write", side_effect=save), \
                mock.patch.object(session_tidy, "LEDGER_FOLD_LINES", 1), \
                mock.patch.object(session_tidy, "LEDGER_KEEP_RAW", 1), \
                mock.patch("fleet.gitinfo.resolve_gitdir", side_effect=lambda cwd: (None, cwd)):
            session_tidy._fold_ledger(seat)
            rows.extend([{"harness": "claude", "sid": "current", "event": "start", "source": "compact",
                          "session_seq": 400, "cwd": self.cwd, "ts": 4},
                         {"harness": "claude", "sid": "current", "event": "prompt", "cwd": self.cwd, "ts": 5}])
            session_tidy._fold_ledger(seat)
            current = next(row for row in rows if row["sid"] == "current" and row["event"] == "summary")
            self.assertEqual((current["start_source"], current["start_at"]), ("clear", 2))
            edges = process_identity.pane_session_successors("claude", "wB:p3N", self.cwd)
            rows.extend([{"harness": "claude", "sid": "current", "event": "start", "source": "resume",
                          "session_seq": 600, "resume_argv": ["claude", "--resume", "current"],
                          "launch_cwd": "/new-cwd", "transcript": "/transcript/current.jsonl",
                          "cwd": "/new-cwd", "ts": 6},
                         {"harness": "claude", "sid": "current", "event": "prompt",
                          "cwd": "/new-cwd", "ts": 7}])
            session_tidy._fold_ledger(seat)
            current = next(row for row in rows if row["sid"] == "current" and row["event"] == "summary")
            self.assertEqual((current["start_source"], current["start_at"]), ("clear", 2))
            context = session_tidy.session_start_context("claude", "current", "wB:p3N")
            self.assertEqual(context, {"session_start_source": "resume", "session_seq": 600,
                                      "resume_argv": ["claude", "--resume", "current"],
                                      "launch_cwd": "/new-cwd",
                                      "transcript": "/transcript/current.jsonl"})
            # A later resume without proven launch context must not inherit an
            # earlier launch's argv or cwd merely because the SID is unchanged.
            rows.extend([{"harness": "claude", "sid": "current", "event": "start", "source": "resume",
                          "session_seq": 800, "cwd": self.cwd, "launch_cwd": "/stale-launch", "ts": 8},
                         {"harness": "claude", "sid": "current", "event": "prompt",
                          "cwd": self.cwd, "ts": 9}])
            session_tidy._fold_ledger(seat)
            context = session_tidy.session_start_context("claude", "current", "wB:p3N")
            self.assertEqual((context["session_start_source"], context["session_seq"]), ("resume", 800))
            self.assertNotIn("resume_argv", context)
            self.assertNotIn("launch_cwd", context)
        sid, _ = process_identity.resolve_session_claims({"registry": "old", "pane": "current"}, edges)
        self.assertEqual(sid, "current")

    def _enrich_pane_observation(self, *, bound, complete):
        class Observation(tuple):
            pass
        observation = Observation((set(), {self.pid} if bound else set()))
        observation.complete = complete
        observation.errors = () if complete else ("wB:p9X: TimeoutError: process-info deadline",)
        s = Session(harness="claude", pid=self.pid, proc_start=self.start, cwd=self.cwd)
        panes = [{"pane_id": "wB:p3N", "agent": "claude", "cwd": self.cwd,
                  "agent_session": {"agent": "claude", "value": "current"}}]
        bindings = {self.pid: {"wB:p3N"}} if bound else {}
        with mock.patch.object(procscan, "read_proc_start", return_value=self.start), \
                mock.patch.object(herdr, "pane_session_aliases", return_value=[]), \
                mock.patch.object(herdr, "_clear_gpu_session_aliases", return_value=[]), \
                mock.patch.object(process_identity, "pane_session_successors", return_value={"old": {"current"}}):
            herdr.enrich([s], agents=[], panes=panes, pids=observation, pane_bindings=bindings)
            claude.enrich(s, tick={self.pid: getattr(s, "_pane_session_claim", None)})
        return s


class ProcessRolesTest(unittest.TestCase):
    def test_scan_omits_unbound_services_for_all_three_harnesses(self):
        lines = ["10 claude 01:00 claude daemon run --origin transient",
                 "11 codex 01:00 codex app-server proxy", "12 opencode 01:00 opencode serve",
                 "20 claude 01:00 claude --model opus", "21 codex 01:00 codex",
                 "22 opencode 01:00 opencode",
                 "30 codex 01:00 codex app-server --listen unix:///tmp/managed-sessions/x/app-server.sock",
                 "40 claude 01:00 claude -p work"]
        with mock.patch.object(procscan, "_ps_lines", return_value=lines), \
                mock.patch.object(procscan, "_pid_ttys", return_value={
                    10: "?", 11: "?", 12: "?", 20: "pts/1", 21: "pts/2", 22: "pts/3", 30: "?", 40: "?"}), \
                mock.patch.object(procscan, "_detached_ttys", return_value=set()), \
                mock.patch.object(procscan, "proc_tree", return_value={}), \
                mock.patch.object(procscan, "_read_argv", side_effect=lambda pid: next(
                    line.split(maxsplit=3)[3].split() for line in lines if line.startswith(str(pid) + " "))), \
                mock.patch.object(procscan, "_read_cwd", return_value=("/proj", False)), \
                mock.patch.object(procscan, "read_environ", side_effect=lambda pid: {
                    "AGENT_DISPATCH_ATTEMPT_ID": "att-40", "AGENT_DISPATCH_DEPTH": "1"
                } if pid == 40 else {}), \
                mock.patch.object(procscan, "read_proc_start", return_value="777"), \
                mock.patch.object(procscan, "is_terminal_state", return_value=False), \
                mock.patch.object(procscan, "exec_child", return_value=None), \
                mock.patch.object(procscan, "_orca_dead_socks", return_value=set()), \
                mock.patch("fleet.session_registry.read", return_value=None):
            rows = procscan.scan()
            jobs = [DispatchJob(key="code", slug="worker", pid=40, proc_start="777",
                                harness="claude", attempt_id="att-40")]
            process_identity.finalize_process_roles(rows, jobs)
        self.assertEqual({s.pid for s in rows}, {20, 21, 22, 30, 40})
        self.assertEqual({s.process_role for s in rows}, {"conversation", "session-backend", "registered-worker"})
        generic_app_server = next(session for session in rows if session.pid == 30)
        self.assertTrue(generic_app_server.app_server)
        self.assertEqual(generic_app_server._invocation_mode, "command")

    def test_unbound_session_cannot_infer_stage_from_cwd_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            neighbor = home / "2026-10-10_home"
            neighbor.mkdir()
            (neighbor / "REPORT.md").write_text("완료")
            s = Session(harness="claude", pid=1, cwd=tmp, slug="home")
            projection.attach_projections([s], [], artifact_root=tmp)
            self.assertEqual(s.work_projection.source, "none")
            self.assertIsNone(s.work_projection.stage_label)

    def test_inherited_attempt_cannot_turn_a_native_service_command_into_a_worker(self):
        for harness, argv in (("claude", ["claude", "daemon", "run"]),
                              ("codex", ["codex", "app-server", "proxy"]),
                              ("opencode", ["opencode", "serve"])):
            mode = procscan.invocation_mode(harness, argv)
            self.assertEqual(process_identity.process_role(tty="?", registered=True,
                                                           invocation=mode), "service")

    def test_unpublished_or_unobservable_headless_conversation_stays_unknown(self):
        for harness, argv in (("claude", ["claude", "-p", "work"]),
                              ("codex", ["codex", "exec", "work"]),
                              ("opencode", ["opencode", "run", "work"])):
            mode = procscan.invocation_mode(harness, argv)
            self.assertEqual(process_identity.process_role(tty="?", invocation=mode), "unknown")
            self.assertEqual(process_identity.process_role(tty="?", invocation=mode,
                                                           native_session=True), "conversation")

    def test_unknown_option_arity_cannot_hide_a_conversation_as_a_service(self):
        for harness, command in (("claude", "daemon"), ("codex", "app-server"),
                                 ("opencode", "serve")):
            self.assertEqual(procscan.invocation_mode(harness, [harness, "--unknown-option", command]),
                             "unknown")

    def test_codex_yolo_alias_is_a_session_flag(self):
        self.assertEqual(procscan.invocation_mode("codex", ["codex", "--yolo"]), "session")
        # A valueless flag must not swallow the word after it.
        self.assertEqual(procscan.invocation_mode("codex", ["codex", "--yolo", "resume"]), "session")
        self.assertEqual(procscan.invocation_mode("codex", ["codex", "--yolo", "app-server"]), "command")
        for argv in (["codex", "--yolo-not-a-flag"], ["codex", "--unknown-option"]):
            self.assertEqual(procscan.invocation_mode("codex", argv), "unknown")

    def test_native_rollout_fd_keeps_a_headless_session_command(self):
        s = Session(harness="codex", pid=994241, proc_start="777", cwd="/proj",
                    session_id="0f5d1a7e-5b1c-4d2e-9f3a-2b6c8d0e1f47", app_server=True)
        s._tty, s._invocation_mode = "?", "command"
        path = "/tmp/rollout-0f5d1a7e-5b1c-4d2e-9f3a-2b6c8d0e1f47.jsonl"
        with mock.patch.object(codex, "_proc_rollout", return_value=path):
            codex.process_rollouts([s], "/tmp/codex")
            rows = [s]
            process_identity.finalize_process_roles(rows, [])
        self.assertEqual(rows, [s])
        self.assertEqual(s.process_role, "conversation")

    def test_failed_argv_read_does_not_parse_flat_ps_prompt_or_remove_row(self):
        with mock.patch.object(procscan, "_ps_lines", return_value=["994242 claude 01:00 claude -p user's prompt"]), \
                mock.patch.object(procscan, "_pid_ttys", return_value={994242: "?"}), \
                mock.patch.object(procscan, "_detached_ttys", return_value=set()), \
                mock.patch.object(procscan, "proc_tree", return_value={}), \
                mock.patch.object(procscan, "_read_argv", return_value=[]), \
                mock.patch.object(procscan, "_read_cwd", return_value=("/proj", False)), \
                mock.patch.object(procscan, "read_environ", return_value={}), \
                mock.patch.object(procscan, "read_proc_start", return_value="777"), \
                mock.patch.object(procscan, "is_terminal_state", return_value=False), \
                mock.patch.object(procscan, "exec_child", return_value=None), \
                mock.patch("fleet.session_registry.read", return_value=None):
            rows = procscan.scan()
            process_identity.finalize_process_roles(rows, [])
        self.assertEqual([s.pid for s in rows], [994242])
        self.assertEqual(rows[0].process_role, "unknown")

    def test_no_observation_cannot_claim_idle(self):
        state, _ = model.classify_session({"harness": "claude", "pid_alive": True}, 1000)
        self.assertEqual(state, "unknown")


if __name__ == "__main__":
    unittest.main()
