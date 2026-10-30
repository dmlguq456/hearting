#!/usr/bin/env python3
"""Per-repository supervision count: the one aggregation the grant limit and Fleet share.

Rows, markers and relations are injected, so no herdr, registry or ledger is read.
Linked worktrees fold into their primary through ``resolve_gitdir``.
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

_UTILITIES_DIR = os.path.join(os.path.dirname(_TOOLS_DIR), "utilities")
if _UTILITIES_DIR not in sys.path:
    sys.path.insert(0, _UTILITIES_DIR)

import claude_process_identity                                          # noqa: E402
from fleet.collectors import codex, opencode, procscan, steward       # noqa: E402

EXPLICIT = {"harness": "unknown", "session_id": None, "name": "-", "kind": "explicit",
            "source": "explicit"}


def _git(*argv):
    subprocess.run(["git", *argv], check=True, capture_output=True, text=True)


def _relation(sid, *, origin=None, generation=None, targets=(EXPLICIT,), harness="claude"):
    return {"generation": generation or f"gen-{sid}", "released": False,
            "origin": {"harness": harness, "sid": origin or sid},
            "holder": {"harness": harness, "sid": sid},
            "targets": [dict(target) for target in targets]}


class _RepoFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="steward-repo-cap-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.primary, self.linked, self.other = base / "alpha", base / "alpha-wt", base / "beta"
        for repo in (self.primary, self.other):
            _git("init", "-q", str(repo))
            _git("-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                 "commit", "-q", "--allow-empty", "-m", "fixture")
        _git("-C", str(self.primary), "worktree", "add", "-q", "-b", "linked", str(self.linked))
        self.registry = base / "registry"
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.registry),
                                               "FLEET_SESSION_REGISTRY_DIR": str(self.registry)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key = steward._repository_key(str(self.primary))
        # The Claude census is stubbed: complete, and nobody claims any session. That has
        # no existence evidence, so the proof stays "unknown" and the developer host's
        # real /proc never leaks in.
        self.claude_census = mock.Mock(return_value=(True, set(), set()))
        patcher = mock.patch.object(claude_process_identity, "claude_session_census",
                                    self.claude_census)
        patcher.start()
        self.addCleanup(patcher.stop)

    def claude_seen(self, holders=(), complete=True, reasons=()):
        self.claude_census.return_value = (complete, set(holders), set(reasons))

    def claude_history(self, *sids):
        self.registry.mkdir(parents=True, exist_ok=True)
        (self.registry / "history.jsonl").write_text(
            "".join(json.dumps({"sessionId": sid}) + "\n" for sid in sids), encoding="utf-8")

    def claude_transcript(self, sid):
        directory = self.registry / "projects" / "p"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")

    def row(self, sid, cwd, harness="claude"):
        return SimpleNamespace(harness=harness, session_id=sid, cwd=str(cwd))

    def census(self, rows, relations, markers=None, **kw):
        return steward.repo_supervision(repository_key=self.key, limit=kw.pop("limit", 2),
                                        markers=markers or {}, relations=relations,
                                        sessions=rows, agents=kw.pop("agents", []), **kw)

    def write_record(self, pid, sid, proc_start=None):
        directory = self.registry / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{pid}.json").write_text(
            json.dumps({"pid": pid, "sessionId": sid, "cwd": str(self.primary),
                        "procStart": proc_start}), encoding="utf-8")


class RepoSupervisionTest(_RepoFixture, unittest.TestCase):
    def test_primary_and_linked_worktree_count_as_one_repository(self):
        rows = [self.row("s1", self.primary), self.row("s2", self.linked)]
        result = self.census(rows, [_relation("s1"), _relation("s2")])
        self.assertEqual(result["counted"], 2)
        self.assertEqual(result["repository"], "alpha")
        self.assertEqual({h["basis"] for h in result["holders"]}, {"role"})
        self.assertFalse(result["over_limit"])

    def test_other_repository_holder_is_not_counted(self):
        rows = [self.row("s1", self.primary), self.row("s2", self.other)]
        result = self.census(rows, [_relation("s1"), _relation("s2")])
        self.assertEqual([h["session_id"] for h in result["holders"]], ["s1"])

    def test_same_session_with_two_generations_counts_once(self):
        rows = [self.row("s1", self.primary)]
        result = self.census(rows, [_relation("s1", generation="g1"), _relation("s1", generation="g2")])
        self.assertEqual(result["counted"], 1)

    def test_confirmed_handover_counts_the_successor_only(self):
        rows = [self.row("old", self.primary), self.row("new", self.primary)]
        result = self.census(rows, [_relation("new", origin="old")])
        self.assertEqual([h["session_id"] for h in result["holders"]], ["new"])

    def test_released_relation_is_not_a_holder(self):
        rows = [self.row("s1", self.primary)]
        relation = dict(_relation("s1"), released=True)
        self.assertEqual(self.census(rows, [relation])["counted"], 0)

    def test_grant_basis_counts_a_holder_whose_row_is_not_visible_yet(self):
        markers = {("claude", "s1"): {"repository": self.key}}
        result = self.census([], [_relation("s1")], markers=markers)
        self.assertEqual([(h["session_id"], h["basis"]) for h in result["holders"]], [("s1", "grant")])
        elsewhere = {("claude", "s1"): {"repository": str(self.other / ".git")}}
        self.assertEqual(self.census([], [_relation("s1")], markers=elsewhere)["counted"], 0)

    def test_grant_basis_yields_to_a_row_in_another_repository(self):
        markers = {("claude", "s1"): {"repository": self.key}}
        result = self.census([self.row("s1", self.other)], [_relation("s1")], markers=markers)
        self.assertEqual(result["counted"], 0)

    def test_ended_holder_frees_the_slot_and_unknown_still_counts(self):
        rows = [self.row("s1", self.primary), self.row("s2", self.primary)]
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.write_record(dead.pid, "s1")
        result = self.census(rows, [_relation("s1"), _relation("s2")])
        by_sid = {h["session_id"]: h for h in result["holders"]}
        self.assertEqual((by_sid["s1"]["liveness"], by_sid["s1"]["counted"]), ("ended", False))
        self.assertEqual((by_sid["s2"]["liveness"], by_sid["s2"]["counted"]), ("unknown", True))
        self.assertEqual(result["counted"], 1)

    def test_over_limit_is_shown_and_nothing_is_dropped(self):
        rows = [self.row(f"s{i}", self.primary) for i in range(3)]
        result = self.census(rows, [_relation(f"s{i}") for i in range(3)])
        self.assertEqual((result["counted"], result["limit"], result["over_limit"]), (3, 2, True))
        self.assertEqual(len(result["holders"]), 3)

    def test_supervision_by_repository_separates_repositories(self):
        rows = [self.row("s1", self.primary), self.row("s2", self.linked),
                self.row("s3", self.other)]
        board = steward.supervision_by_repository(
            rows, limit=2, markers={}, relations=[_relation(s) for s in ("s1", "s2", "s3")],
            agents=[])
        self.assertEqual({key: value["counted"] for key, value in board.items()},
                         {self.key: 2, steward._repository_key(str(self.other)): 1})
        self.assertEqual(board[self.key], self.census(rows, [_relation(s) for s in ("s1", "s2", "s3")]))

    def test_enrich_keeps_one_board_census_for_the_models_view(self):
        rows = [self.row("s1", self.primary), self.row("s3", self.other)]
        relations = [_relation("s1"), _relation("s3")]
        with mock.patch.object(steward, "_last_supervision", None), \
                mock.patch.object(steward, "read_relations", return_value=relations) as read, \
                mock.patch.object(steward, "_registry_sessions", return_value=[]), \
                mock.patch.object(steward, "_steward_limit", return_value=2), \
                mock.patch("fleet.collectors.herdr.list_agents", return_value=[]):
            steward.enrich(rows, markers={})
            census = steward.last_supervision()
        read.assert_called_once()
        self.assertEqual({key: value["counted"] for key, value in census.items()},
                         {self.key: 1, steward._repository_key(str(self.other)): 1})
        with mock.patch.object(steward, "_last_supervision", None), \
                mock.patch.object(steward, "read_relations", return_value=[]):
            steward.enrich(rows, markers={})
            self.assertEqual(steward.last_supervision(), {})


class SessionLivenessTest(_RepoFixture, unittest.TestCase):
    def test_herdr_row_means_live(self):
        agents = [{"agent": "claude", "agent_session": {"value": "s1"}}]
        self.assertEqual(steward.session_liveness("claude", "s1", agents=agents), "live")

    def test_dead_pid_is_ended(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.write_record(dead.pid, "s1")
        self.assertEqual(steward.session_liveness("claude", "s1", agents=[]), "ended")

    def test_recycled_pid_is_ended_and_matching_start_is_live(self):
        from fleet.collectors import procscan
        self.write_record(os.getpid(), "s1", proc_start="1")
        self.assertEqual(steward.session_liveness("claude", "s1", agents=[]), "ended")
        self.write_record(os.getpid(), "s1", proc_start=str(procscan.read_proc_start(os.getpid())))
        self.assertEqual(steward.session_liveness("claude", "s1", agents=[]), "live")

    def test_missing_registry_is_unknown(self):
        self.assertEqual(steward.session_liveness("claude", "s1", agents=[]), "unknown")
        observation = mock.Mock(codex=lambda: None, opencode=lambda: None)
        self.assertEqual(steward.session_liveness("opencode", "s1", agents=[],
                                                  observation=observation), "unknown")


class SessionLivenessClaudeTest(_RepoFixture, unittest.TestCase):
    def state(self, sid="s1", agents=()):
        return steward.session_liveness("claude", sid, agents=list(agents))

    def test_k1_a_census_holder_is_live_without_a_record_or_herdr_row(self):
        self.claude_seen(holders={"s1"})
        self.assertEqual(self.state(), "live")

    def test_k2_a_census_holder_beats_a_dead_registry_record_of_the_same_sid(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.write_record(dead.pid, "s1")
        self.claude_seen(holders={"s1"})
        self.assertEqual(self.state(), "live")

    def test_k3_unclaimed_session_in_native_history_is_ended_and_frees_the_slot(self):
        self.claude_history("s1")
        self.assertEqual(self.state(), "ended")
        markers = {("claude", "s1"): {"repository": self.key}}
        result = self.census([], [_relation("s1")], markers=markers)
        holder, = result["holders"]
        self.assertEqual((holder["basis"], holder["liveness"], holder["counted"]),
                         ("grant", "ended", False))
        self.assertEqual(result["counted"], 0)

    def test_k4_a_native_transcript_is_enough(self):
        self.claude_transcript("s1")
        self.assertEqual(self.state(), "ended")

    def test_k5_complete_census_without_existence_evidence_is_unknown_and_counts(self):
        self.claude_history("someone-else")
        self.assertEqual(self.state(), "unknown")
        result = self.census([self.row("s1", self.primary)], [_relation("s1")])
        self.assertEqual((result["holders"][0]["liveness"], result["counted"]), ("unknown", 1))

    def test_k6_incomplete_or_failed_census_is_unknown_even_with_history(self):
        self.claude_history("s1")
        self.claude_seen(complete=False, reasons={"pid-namespace-mismatch"})
        self.assertEqual(self.state(), "unknown")
        self.claude_census.side_effect = RuntimeError("no /proc")
        self.assertEqual(self.state(), "unknown")

    def test_k7_a_registry_record_still_decides_after_the_census_says_ended(self):
        self.claude_history("s1")
        self.write_record(os.getpid(), "s1", proc_start=str(procscan.read_proc_start(os.getpid())))
        self.assertEqual(self.state(), "live")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.write_record(dead.pid, "s1")
        (self.registry / "sessions" / f"{os.getpid()}.json").unlink()
        self.assertEqual(self.state(), "ended")

    def test_k8_one_census_per_pass_and_none_when_every_holder_has_a_herdr_row(self):
        sids = ("k-1", "k-2", "k-3")
        rows = [self.row(sid, cwd) for sid, cwd in zip(sids, (self.primary, self.primary, self.other))]
        relations = [_relation(sid) for sid in sids]
        steward.supervision_by_repository(rows, limit=2, markers={}, relations=relations, agents=[])
        self.assertEqual(self.claude_census.call_count, 1)
        self.claude_census.reset_mock()
        with mock.patch.object(steward, "_last_supervision", None), \
                mock.patch.object(steward, "read_relations", return_value=relations), \
                mock.patch.object(steward, "_registry_sessions", return_value=[]), \
                mock.patch.object(steward, "_steward_limit", return_value=2), \
                mock.patch("fleet.collectors.herdr.list_agents", return_value=[]):
            steward.enrich(rows, markers={})
        self.assertEqual(self.claude_census.call_count, 1)
        self.claude_census.reset_mock()
        agents = [{"agent": "claude", "agent_session": {"value": sid}} for sid in sids]
        steward.supervision_by_repository(rows, limit=2, markers={}, relations=relations,
                                          agents=agents)
        self.claude_census.assert_not_called()


CODEX_X = "aaaaaaaa-1111-2222-3333-444444444444"
CODEX_Y = "bbbbbbbb-1111-2222-3333-444444444444"
OPENCODE_X = "ses_endedxxxxxxxxxxxxxxxxxx"
OPENCODE_Y = "ses_shownyyyyyyyyyyyyyyyyyy"
_HOLD = "import sys, time; print('ready', flush=True); time.sleep(600)"
_SERVE = ("import socket, time; s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(16); "
          "print(s.getsockname()[1], flush=True); time.sleep(600)")
_CLIENT = ("import socket, sys, time; c = socket.create_connection(('127.0.0.1', int(sys.argv[-1]))); "
           "print('ready', flush=True); time.sleep(600)")


class _ProcessFixture(_RepoFixture):
    """Fixture ``codex``/``opencode`` processes (the interpreter under that name), a private
    Codex home, TUI-selection dir and OpenCode database. The observation functions are
    handed only the fixture pids, so the developer host's real sessions never leak in."""

    def setUp(self):
        super().setUp()
        base = Path(self.temp.name).resolve()
        self.bin, self.codex_home = base / "bin", base / "codex-home"
        self.bin.mkdir()
        self.codex_pids, self.opencode_pids = [], []
        self.db = base / "opencode.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE session_v2 (id TEXT, time_updated INTEGER, time_idle INTEGER)")
        con.execute("CREATE TABLE session (id TEXT, time_updated INTEGER)")
        con.commit()
        con.close()
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home),
                                               "XDG_STATE_HOME": str(base / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.namespace = mock.patch.object(procscan, "initial_pid_namespace", return_value=True)
        self.namespace.start()
        self.addCleanup(self.namespace.stop)
        self.open_rollouts = mock.Mock(side_effect=lambda: codex_read(pids=self.codex_pids))
        self.visibility = mock.Mock(side_effect=lambda: opencode_read(pids=self.opencode_pids))
        for target, name, replacement in ((codex, "open_rollout_sessions", self.open_rollouts),
                                          (opencode, "session_visibility", self.visibility),
                                          (opencode, "_db", lambda: str(self.db))):
            patch = mock.patch.object(target, name, replacement)
            patch.start()
            self.addCleanup(patch.stop)

    def spawn(self, name, code, *args):
        link = self.bin / name
        if not link.exists():
            os.symlink(sys.executable, link)
        proc = subprocess.Popen([str(link), "-c", code, *args], stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.reap, proc)
        return proc, proc.stdout.readline().strip()

    @staticmethod
    def reap(proc):
        proc.kill()
        proc.wait()
        proc.stdout.close()

    def rollout(self, sid, day="28"):
        path = self.codex_home / "sessions" / "2026" / "10" / day / f"rollout-2026-10-{day}T00-00-00-{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n")
        return path

    def codex_holding(self, sid):
        path = self.rollout(sid)
        proc, _ = self.spawn("codex", "import time; f = open(%r); print('ready', flush=True); "
                             "time.sleep(600)" % str(path))
        self.codex_pids.append(proc.pid)
        return proc

    def opencode_proc(self, code, *args):
        proc, line = self.spawn("opencode", code, *args)
        self.opencode_pids.append(proc.pid)
        return proc, line

    def select(self, proc, sid):
        directory = Path(os.environ["XDG_STATE_HOME"]) / "hearting" / "tui-identity"
        directory.mkdir(parents=True, exist_ok=True)
        start = opencode._proc_start_ticks(proc.pid)
        (directory / f"{proc.pid}-{start}.json").write_text(json.dumps({
            "schema": opencode.TUI_SELECTION_SCHEMA, "pid": proc.pid, "start": start,
            "sessionID": sid}))

    def put(self, sid, updated, idle=None, table="session_v2"):
        con = sqlite3.connect(self.db)
        if table == "session_v2":
            con.execute("INSERT INTO session_v2 VALUES (?, ?, ?)", (sid, updated, idle))
        else:
            con.execute("INSERT INTO session VALUES (?, ?)", (sid, updated))
        con.commit()
        con.close()

    def state(self, harness, sid):
        return steward.session_liveness(harness, sid, agents=[])

    def holders(self, harness, *sids):
        rows = [self.row(sid, self.primary, harness) for sid in sids]
        return rows, [_relation(sid, harness=harness) for sid in sids]


codex_read = codex.open_rollout_sessions
opencode_read = opencode.session_visibility


class SessionLivenessCodexTest(_ProcessFixture, unittest.TestCase):
    def test_c1_rollout_held_open_is_live(self):
        self.codex_holding(CODEX_X)
        self.assertEqual(self.state("codex", CODEX_X), "live")

    def test_c2_open_rollout_beats_an_exited_registry_record(self):
        self.codex_holding(CODEX_X)
        self.write_codex_record(CODEX_X, status="exited")
        self.assertEqual(self.state("codex", CODEX_X), "live")

    def write_codex_record(self, sid, **fields):
        directory = self.registry / "codex"
        directory.mkdir(parents=True, exist_ok=True)
        record = {"pid": 999999, "sessionId": sid, **fields}
        (directory / f"{record['pid']}.json").write_text(json.dumps(record))

    def test_c2b_a_registry_record_still_decides_when_one_exists(self):
        self.rollout(CODEX_X)
        self.write_codex_record(CODEX_X, pid=os.getpid())  # a running process: registry says live
        self.assertEqual(self.state("codex", CODEX_X), "live")
        self.write_codex_record(CODEX_X, pid=os.getpid(), status="exited")
        self.assertEqual(self.state("codex", CODEX_X), "ended")

    def test_c3_rollout_nobody_holds_is_ended_and_frees_the_slot(self):
        proc = self.codex_holding(CODEX_X)
        self.codex_holding(CODEX_Y)
        self.reap_now(proc)
        rows, relations = self.holders("codex", CODEX_X, CODEX_Y, "no-rollout-sid")
        result = self.census(rows, relations)
        by_sid = {h["session_id"]: (h["liveness"], h["counted"]) for h in result["holders"]}
        self.assertEqual(by_sid, {CODEX_X: ("ended", False), CODEX_Y: ("live", True),
                                  "no-rollout-sid": ("unknown", True)})
        self.assertEqual(result["counted"], 2)

    def reap_now(self, proc):
        proc.kill()
        proc.wait()

    def test_c4_no_rollout_file_or_two_of_them_is_unknown(self):
        self.assertEqual(self.state("codex", CODEX_X), "unknown")
        self.rollout(CODEX_X, day="27")
        self.assertEqual(self.state("codex", CODEX_X), "ended")
        self.rollout(CODEX_X, day="28")
        self.assertEqual(self.state("codex", CODEX_X), "unknown")

    def test_c5_unreadable_fd_table_or_process_list_is_unknown(self):
        self.codex_holding(CODEX_Y)
        self.rollout(CODEX_X)
        self.assertEqual(self.state("codex", CODEX_X), "ended")
        with mock.patch.object(codex, "_rollout_fd_paths", side_effect=PermissionError("fd")):
            self.assertEqual(self.state("codex", CODEX_X), "unknown")
        with mock.patch.object(codex, "_rollout_fd_paths", return_value=([], True)):
            self.assertEqual(self.state("codex", CODEX_X), "unknown")
        self.open_rollouts.side_effect = lambda: None
        self.assertEqual(self.state("codex", CODEX_X), "unknown")

    def test_c6_outside_the_initial_pid_namespace_proves_no_end(self):
        self.codex_holding(CODEX_Y)
        self.rollout(CODEX_X)
        self.namespace.stop()
        self.addCleanup(self.namespace.start)
        with mock.patch.object(procscan, "initial_pid_namespace", return_value=False):
            self.assertEqual(self.state("codex", CODEX_X), "unknown")
            self.assertEqual(self.state("codex", CODEX_Y), "live")

    def test_c7_fd_helper_reads_the_open_rollout(self):
        proc = self.codex_holding(CODEX_X)
        paths, partial = codex._rollout_fd_paths(proc.pid)
        self.assertEqual(([codex._sid(path) for path in paths], partial), ([CODEX_X], False))
        proc.kill()
        proc.wait()
        with self.assertRaises(OSError):
            codex._rollout_fd_paths(proc.pid)
        self.assertIsNone(codex._proc_rollout(proc.pid, str(self.primary), str(self.codex_home)))

    def test_c8_claude_holders_read_only_the_claude_census(self):
        rows, relations = self.holders("claude", "c1", "c2")
        result = self.census(rows, relations)
        self.assertEqual({h["liveness"] for h in result["holders"]}, {"unknown"})
        self.open_rollouts.assert_not_called()
        self.visibility.assert_not_called()
        self.assertEqual(self.claude_census.call_count, 1)

    def test_c9_one_observation_per_pass_and_only_for_the_harness_in_use(self):
        sids = ("cx-1", "cx-2", "cx-3")
        rows = [self.row(sid, cwd, "codex") for sid, cwd in zip(sids, (self.primary, self.primary, self.other))]
        relations = [_relation(sid, harness="codex") for sid in sids]
        steward.supervision_by_repository(rows, limit=2, markers={}, relations=relations, agents=[])
        self.assertEqual((self.open_rollouts.call_count, self.visibility.call_count), (1, 0))
        self.open_rollouts.reset_mock()
        with mock.patch.object(steward, "_last_supervision", None), \
                mock.patch.object(steward, "read_relations", return_value=relations), \
                mock.patch.object(steward, "_registry_sessions", return_value=[]), \
                mock.patch.object(steward, "_steward_limit", return_value=2), \
                mock.patch("fleet.collectors.herdr.list_agents", return_value=[]):
            steward.enrich(rows, markers={})
        self.assertEqual((self.open_rollouts.call_count, self.visibility.call_count), (1, 0))


class SessionLivenessOpenCodeTest(_ProcessFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.now_ms = int(time.time() * 1000)

    def test_o1_argv_session_or_tui_selection_is_live(self):
        self.opencode_proc(_HOLD, "-s", OPENCODE_X)
        self.assertEqual(self.state("opencode", OPENCODE_X), "live")
        proc, _ = self.opencode_proc(_HOLD)
        self.select(proc, OPENCODE_Y)
        self.assertEqual(self.state("opencode", OPENCODE_Y), "live")

    def test_o2_older_than_every_running_opencode_is_ended(self):
        self.put(OPENCODE_X, 1_000)
        self.assertEqual(self.state("opencode", OPENCODE_X), "ended")  # nothing runs at all
        self.opencode_proc(_HOLD, "-s", OPENCODE_Y)
        rows, relations = self.holders("opencode", OPENCODE_X, OPENCODE_Y)
        by_sid = {h["session_id"]: (h["liveness"], h["counted"])
                  for h in self.census(rows, relations)["holders"]}
        self.assertEqual(by_sid, {OPENCODE_X: ("ended", False), OPENCODE_Y: ("live", True)})

    def test_o3_runtime_idle_mark_ends_a_session_updated_after_start(self):
        self.opencode_proc(_HOLD, "-s", OPENCODE_Y)
        later = self.now_ms + 60_000
        self.put(OPENCODE_X, later, idle=later)
        self.assertEqual(self.state("opencode", OPENCODE_X), "ended")

    def test_o4_recent_update_without_idle_mark_is_unknown(self):
        self.opencode_proc(_HOLD, "-s", OPENCODE_Y)
        later = self.now_ms + 60_000
        self.put(OPENCODE_X, later)
        self.assertEqual(self.state("opencode", OPENCODE_X), "unknown")
        self.put("ses_otherzzzzzzzzzzzzzzzz", later, idle=later - 1)
        self.assertEqual(self.state("opencode", "ses_otherzzzzzzzzzzzzzzzz"), "unknown")

    def test_o5_tui_that_has_not_chosen_a_session_is_unknown(self):
        self.opencode_proc(_HOLD)
        self.put(OPENCODE_X, 1_000)
        self.assertEqual(self.state("opencode", OPENCODE_X), "unknown")

    def test_o6_a_client_the_walk_cannot_classify_is_unknown(self):
        self.put(OPENCODE_X, 1_000)
        _server, port = self.opencode_proc(_SERVE, "serve")
        self.opencode_proc(_CLIENT, "-s", OPENCODE_Y, port)
        self.assertEqual(self.state("opencode", OPENCODE_X), "ended")  # only opencode clients
        stranger = socket.create_connection(("127.0.0.1", int(port)))
        self.addCleanup(stranger.close)
        self.assertEqual(self.state("opencode", OPENCODE_X), "unknown")
        self.assertEqual(self.state("opencode", OPENCODE_Y), "live")

    def test_o6b_a_runtime_without_a_visible_listener_is_unknown(self):
        self.put(OPENCODE_X, 1_000)
        self.opencode_proc(_HOLD, "serve")
        self.assertEqual(self.state("opencode", OPENCODE_X), "unknown")

    def test_o7_run_continue_and_fork_are_unresolved_but_run_session_is_live(self):
        self.put(OPENCODE_X, 1_000)
        self.opencode_proc(_HOLD, "run", "-s", OPENCODE_Y)
        self.assertEqual(self.state("opencode", OPENCODE_Y), "live")
        self.assertEqual(self.state("opencode", OPENCODE_X), "ended")
        for flag in ("-c", "--continue", "--fork"):
            proc, _ = self.opencode_proc(_HOLD, "run", flag)
            self.assertEqual(self.state("opencode", OPENCODE_X), "unknown", flag)
            self.opencode_pids.remove(proc.pid)

    def test_o8_missing_from_the_database_or_hidden_processes_is_unknown(self):
        self.assertEqual(self.state("opencode", OPENCODE_X), "unknown")
        self.put(OPENCODE_X, 1_000)
        with mock.patch.object(procscan, "initial_pid_namespace", return_value=False):
            self.assertEqual(self.state("opencode", OPENCODE_X), "unknown")
        self.assertEqual(self.state("opencode", OPENCODE_X), "ended")

    def test_o9_legacy_session_table_is_enough(self):
        self.opencode_proc(_HOLD, "-s", OPENCODE_Y)
        self.put(OPENCODE_X, 1_000, table="session")
        self.assertEqual(self.state("opencode", OPENCODE_X), "ended")
        self.put("ses_recentzzzzzzzzzzzzzzz", self.now_ms + 60_000, table="session")
        self.assertEqual(self.state("opencode", "ses_recentzzzzzzzzzzzzzzz"), "unknown")

    def test_utility_commands_neither_show_a_session_nor_block_the_proof(self):
        self.put(OPENCODE_X, 1_000)
        self.opencode_proc(_HOLD, "models")
        self.assertEqual(self.state("opencode", OPENCODE_X), "ended")


class OpenCodeV2AttributionTest(_RepoFixture, unittest.TestCase):
    """``_native_role_sessions`` reads ``session_v2`` first, then the legacy table."""

    def setUp(self):
        super().setUp()
        self.db = Path(self.temp.name).resolve() / "attrib.db"
        patcher = mock.patch.object(opencode, "_db", lambda: str(self.db))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.con = sqlite3.connect(self.db)
        self.addCleanup(self.con.close)

    def tables(self, v2=True, legacy=True):
        if v2:
            self.con.execute("CREATE TABLE session_v2 (id TEXT, directory TEXT)")
        if legacy:
            self.con.execute("CREATE TABLE session (id TEXT, directory TEXT)")
        self.con.commit()

    def put(self, table, sid, directory):
        self.con.execute("INSERT INTO %s VALUES (?, ?)" % table, (sid, directory))
        self.con.commit()

    def native(self, sid):
        markers = {("opencode", sid): {}}
        return {(row.session_id, row.cwd)
                for row in steward._native_role_sessions(markers, [], ())}

    def test_v1_a_v2_only_session_gets_its_directory(self):
        self.tables()
        self.put("session_v2", "ses_v2only", str(self.primary))
        self.assertEqual(self.native("ses_v2only"), {("ses_v2only", str(self.primary))})
        self.assertEqual(steward.repository_for_session("opencode", "ses_v2only", sessions=[]),
                         self.key)

    def test_v2_the_v2_row_wins_and_a_null_directory_falls_back_to_the_legacy_table(self):
        self.tables()
        self.put("session_v2", "ses_both", str(self.primary))
        self.put("session", "ses_both", str(self.other))
        self.put("session_v2", "ses_null", None)
        self.put("session", "ses_null", str(self.other))
        self.assertEqual(self.native("ses_both"), {("ses_both", str(self.primary))})
        self.assertEqual(self.native("ses_null"), {("ses_null", str(self.other))})

    def test_v3_a_database_without_session_v2_still_answers_from_the_legacy_table(self):
        self.tables(v2=False)
        self.put("session", "ses_old", str(self.primary))
        self.assertEqual(self.native("ses_old"), {("ses_old", str(self.primary))})

    def test_v4_a_v2_only_target_carries_the_role_attribution_to_its_supervisor(self):
        self.tables()
        self.put("session_v2", "ses_v2only", str(self.primary))
        target = {"harness": "opencode", "session_id": "ses_v2only", "name": "worker",
                  "kind": "watch", "source": "watch"}
        rows = [self.row("boss", self.primary)]
        result = self.census(rows, [_relation("boss", targets=(target,))])
        holder, = result["holders"]
        self.assertEqual((holder["session_id"], holder["basis"], holder["counted"]),
                         ("boss", "role", True))


def _current_relations(relations):
    """Fleet consumes the common reader's current projection, including for legacy fixtures."""
    import dispatch_seat_handover as handover
    states = {(handover._role_identity(row), "marker"): row for row in relations}
    return list(handover._current_roles(states).values())


def _chain(generation, *links):
    """The common projection of the exact same-evidence chain, as supplied to Fleet."""
    out = []
    for index, sid in enumerate(links):
        relation = _relation(sid, generation=generation, origin=links[0])
        relation["lineage"] = [{"from": links[i], "to": links[i + 1], "snapshot_id": "x"}
                               for i in range(index)]
        out.append(relation)
    return _current_relations(out)


class SameGenerationChainTest(_RepoFixture, unittest.TestCase):
    def sids(self, relations, *names):
        rows = [self.row(name, self.primary) for name in names]
        return sorted(h["session_id"] for h in self.census(rows, relations)["holders"])

    def test_t1_only_the_chain_end_of_one_generation_is_counted(self):
        self.assertEqual(self.sids(_chain("G", "a", "b", "c"), "a", "b", "c"), ["c"])

    def test_t2_a_fork_counts_every_end(self):
        fork = [_relation(sid, generation="G", origin="a") for sid in ("a", "b", "c")]
        fork[1]["lineage"] = [{"from": "a", "to": "b", "snapshot_id": "x"}]
        fork[2]["lineage"] = [{"from": "a", "to": "c", "snapshot_id": "x"}]
        self.assertEqual(self.sids(_current_relations(fork), "a", "b", "c"), ["b", "c"])

    def test_distinct_same_generation_evidence_is_not_suppressed_again_by_census(self):
        import dispatch_seat_handover as handover
        marker = {"harness": "claude", "session_id": "a", "seat_key": "seat",
                  "generation": "G", "targets": {"-": dict(EXPLICIT, ts="2")}}
        role = {"seat_key": "seat", "generation": "G", "origin": {"harness": "claude", "sid": "a"},
                "holder": {"harness": "claude", "sid": "b"}, "marker_digest": "old-evidence",
                "targets": [dict(EXPLICIT, ts="1")],
                "lineage": [{"from": "a", "to": "b", "snapshot_id": "AB"}]}
        row = {"event": "handover", "from": "a", "sid": "b", "harness": "claude",
               "seat_key": "seat", "snapshot_id": "AB", "source": "clear", "steward": {"roles": [role]}}
        peer = SimpleNamespace(steward_marker_roots=lambda: ([], "fixture"),
            read_steward_markers=lambda _roots: {("claude", "a"): marker},
            steward_evidence_targets=lambda value: list(value["targets"].values()))
        with mock.patch.object(handover, "_peer_message", return_value=peer), \
             mock.patch.object(handover, "_all_snapshots", return_value=[{"seat": {"key": "seat"}}]), \
             mock.patch.object(handover, "_transition_rows", return_value=[("seat", row)]):
            relations = handover.resolved_steward_relations()
        self.assertEqual(len(relations), 2)
        rows = [self.row(sid, self.primary) for sid in ("a", "b")]
        for ordered in (relations, list(reversed(relations))):
            census = self.census(rows, ordered)
            self.assertEqual(sorted(item["session_id"] for item in census["holders"]), ["a", "b"])
            self.assertEqual(census["counted"], 2)
            board = steward.supervision_by_repository(rows, markers={}, relations=ordered, agents=[])
            self.assertEqual(board[self.key]["counted"], 2)

    def test_t3_without_lineage_every_holder_counts(self):
        relations = [_relation(sid, generation="G") for sid in ("a", "b", "c")]
        self.assertEqual(self.sids(relations, "a", "b", "c"), ["a", "b", "c"])

    def test_t4_a_holder_that_is_mid_chain_in_one_generation_and_an_end_in_another_counts_once(self):
        relations = _chain("G1", "a", "b") + [_relation("a", generation="G2")]
        result = self.census([self.row(s, self.primary) for s in ("a", "b")], relations)
        self.assertEqual(sorted(h["session_id"] for h in result["holders"]), ["a", "b"])
        self.assertEqual(result["counted"], 2)

    def test_t5_a_repository_with_only_a_mid_chain_holder_is_not_a_board_key(self):
        rows = [self.row("a", self.other), self.row("b", self.primary)]
        board = steward.supervision_by_repository(
            rows, limit=2, markers={}, relations=_chain("G", "a", "b"), agents=[])
        self.assertEqual(sorted(board), [self.key])
        self.assertEqual([h["session_id"] for h in board[self.key]["holders"]], ["b"])

    def test_t6_other_generations_are_not_merged(self):
        relations = _chain("G1", "a", "b") + [dict(_relation("c", generation="G2"),
                                                   lineage=[{"from": "a", "to": "c"}])]
        self.assertEqual(self.sids(relations, "a", "b", "c"), ["b", "c"])

    def test_t7_a_lineage_that_leaves_no_end_drops_nothing(self):
        loop = [dict(_relation("a", generation="G"), lineage=[{"from": "b", "to": "a"}]),
                dict(_relation("b", generation="G"), lineage=[{"from": "a", "to": "b"}])]
        self.assertEqual(self.sids(loop, "a", "b"), ["a", "b"])


class RepositoryForSessionTest(_RepoFixture, unittest.TestCase):
    def test_linked_worktree_row_folds_to_the_primary_key(self):
        rows = [self.row("s1", self.linked)]
        self.assertEqual(steward.repository_for_session("claude", "s1", sessions=rows), self.key)

    def test_missing_or_ambiguous_row_is_none_and_never_the_process_cwd(self):
        self.assertIsNone(steward.repository_for_session("claude", "s1", sessions=[]))
        rows = [self.row("s1", self.primary), self.row("s1", self.other)]
        self.assertIsNone(steward.repository_for_session("claude", "s1", sessions=rows))


if __name__ == "__main__":
    unittest.main()
