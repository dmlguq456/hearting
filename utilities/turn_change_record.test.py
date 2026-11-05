#!/usr/bin/env python3
"""The automatic change record: each turn's own source changes, one record per session and
folder, never in the way."""
import importlib.util
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))
from fleet import route_chain  # noqa: E402
_spec = importlib.util.spec_from_file_location("turn_change_record", HERE / "turn_change_record.py")
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)

SID = "sess-auto-1"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args, env=None):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **GIT_ENV, **(env or {})}).stdout


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="turn-record-")))
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        (self.tmp / "home").mkdir()
        self.repo = self.tmp / "work" / "greet"
        self.repo.mkdir(parents=True)
        git(self.repo, "init", "-q")
        (self.repo / "greet.py").write_text('print("Helo")\n')
        (self.repo / "README.md").write_text("notes\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        self.root = self.repo / ".agent_reports"
        self.ledgers = self.tmp / "ledgers"
        self.env = {
            "HOME": str(self.tmp / "home"), "PATH": os.environ.get("PATH", ""),
            "TMPDIR": str(scratch), "FLEET_ROUTE_CHAIN_DIR": str(self.ledgers),
            "XDG_STATE_HOME": str(self.tmp / "home" / ".local" / "state"),
            "XDG_DATA_HOME": str(self.tmp / "home" / ".local" / "share"),
            "XDG_CONFIG_HOME": str(self.tmp / "home" / ".config"),
            "AGENT_ARTIFACT_CHECKPOINT": "off", "HEARTING_WORKFLOW_GROUP_REVIEW": "off",
        }
        self._tempdir = tempfile.tempdir
        tempfile.tempdir = str(scratch)
        self._environ = mock.patch.dict(os.environ, {"FLEET_ROUTE_CHAIN_DIR": str(self.ledgers)})
        self._environ.start()
        self.calls, self.appended = [], []
        self.fail_records = 0
        self.close_ok = True

    def tearDown(self):
        self._environ.stop()
        tempfile.tempdir = self._tempdir
        shutil.rmtree(self.tmp, ignore_errors=True)

    # A stand-in for compose+finish: the route exists (its ledger line too) before it closes.
    def recorder(self, harness, sid, top, root, items, env, remember=None):
        self.calls.append({"sources": [item["source"] for item in items], "root": root})
        if self.fail_records:
            self.fail_records -= 1
            return {"ok": False, "reason": "compose-failed", "detail": "fixture"}
        route_file = str(self.root / ".runtime" / "routes" / "rt-a1.json")
        self.ledger_line(harness, sid, "rt-a1")
        if remember:
            remember(route_file)
        return {"ok": True, "route_file": route_file, "closed": self.close_ok,
                "closed_by": "finish" if self.close_ok else None}

    def appender(self, harness, sid, top, root, entry, items, env):
        self.appended.append([item["source"] for item in items])
        return {"ok": True}

    def start(self, sid=SID, harness="claude"):
        return T.turn_start(harness, sid, str(self.repo), self.env,
                            recorder=self.recorder, appender=self.appender)

    def end(self, sid=SID, harness="claude"):
        return T.turn_end(harness, sid, str(self.repo), self.env,
                          recorder=self.recorder, appender=self.appender)

    def state(self, sid=SID, harness="claude"):
        return json.loads(T.state_path(self.env, harness, sid).read_text())

    def ledger_line(self, harness="claude", sid=SID, route_id="rt-own", automatic=False):
        directory = self.ledgers / harness
        directory.mkdir(parents=True, exist_ok=True)
        line = {"v": 1, "event": "compose", "harness": harness, "session_id": sid,
                "route_id": route_id, "artifact_root": str(self.root)}
        with open(directory / f"{sid}.jsonl", "a") as stream:
            stream.write(json.dumps({**line, "automatic": True} if automatic else line) + "\n")

    def killed(self, harness, sid, top, root, items, env, remember=None):
        """The record step dies before its route exists (the session ended, the step was killed)."""
        raise KeyboardInterrupt("record step killed")

    def end_killed(self, sid=SID, harness="claude"):
        with self.assertRaises(KeyboardInterrupt):
            T.turn_end(harness, sid, str(self.repo), self.env, recorder=self.killed, appender=self.appender)


class TurnAttributionTest(Fixture):
    def test_shell_edit_during_the_turn_is_recorded(self):
        self.assertEqual(self.start()["state"], "baseline")
        (self.repo / "greet.py").write_text('print("Hello")\n')  # what `sed -i` leaves
        (self.repo / "notes.txt").write_text("new\n")
        self.assertEqual(self.end()["state"], "recorded")
        self.assertEqual(self.calls, [{"sources": [["greet.py", "notes.txt"]], "root": str(self.root)}])
        self.assertTrue(self.state()["records"][str(self.root)]["closed"])

    def test_edits_between_turns_are_not_the_agents(self):
        self.start()
        self.assertEqual(self.end()["state"], "unchanged")
        (self.repo / "README.md").write_text("the person's own edit\n")
        self.start()
        self.assertEqual(self.end()["state"], "unchanged")
        self.assertEqual(self.calls, [])
        self.start()
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.assertEqual(self.end()["state"], "recorded")
        self.assertEqual(self.calls[0]["sources"], [["greet.py"]])

    def test_a_file_already_changed_before_the_turn_counts_when_the_turn_changes_it(self):
        (self.repo / "greet.py").write_text('print("Hel")\n')
        self.start()
        time.sleep(0.01)
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.assertEqual(self.end()["state"], "recorded")
        self.assertEqual(self.calls[0]["sources"], [["greet.py"]])

    def test_artifact_scratch_and_generated_paths_do_not_count(self):
        self.start()
        (self.root / "research").mkdir(parents=True)
        (self.root / "research" / "note.md").write_text("artifact\n")
        (self.repo / "__pycache__").mkdir()
        (self.repo / "__pycache__" / "greet.cpython-312.pyc").write_bytes(b"\0")
        (self.repo / "stray.pyc").write_bytes(b"\0")
        (self.repo / ".pytest_cache" / "v").mkdir(parents=True)
        (self.repo / ".pytest_cache" / ".gitignore").write_text("*\n")  # as pytest writes it
        (self.repo / ".pytest_cache" / "v" / "lastfailed").write_text("{}")
        self.assertEqual(self.end()["state"], "unchanged")
        self.assertEqual(self.calls, [])

    def test_tracked_files_count_wherever_they_live(self):
        (self.repo / "node_modules" / "pkg").mkdir(parents=True)
        (self.repo / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1\n")
        (self.repo / "tools").mkdir()
        (self.repo / "tools" / "cache.pyc").write_bytes(b"\0")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "vendored")
        self.start()
        (self.repo / "node_modules" / "pkg" / "index.js").write_text("module.exports = 2\n")
        (self.repo / "tools" / "cache.pyc").write_bytes(b"\1")
        self.assertEqual(self.end()["state"], "recorded")
        self.assertEqual(self.calls[0]["sources"], [["node_modules/pkg/index.js", "tools/cache.pyc"]])

    def test_a_session_holding_its_own_route_keeps_a_supplement_without_closing_it(self):
        self.ledger_line("claude", SID, "rt-own")
        self.assertEqual(self.start()["state"], "route-held-baseline")
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.assertEqual(self.end()["state"], "route-observed")
        self.assertEqual(self.calls, [])
        rows = T.supplementary_records('claude', SID, str(self.repo), self.env)
        self.assertTrue(rows[0]['record_saved'])
        self.assertIn('greet.py', Path(rows[0]['reference']).read_text())
        self.assertFalse((self.root/'.runtime/routes/rt-own.outcome.json').exists())

    def test_later_turns_are_appended_to_the_same_record(self):
        self.start()
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.assertEqual(self.end()["state"], "recorded")
        self.start()
        (self.repo / "README.md").write_text("more\n")
        self.assertEqual(self.end()["state"], "appended")
        self.start()
        self.assertEqual(self.end()["state"], "unchanged")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.appended, [[["README.md"]]])
        self.assertEqual(self.state()["records"][str(self.root)]["turns"], 2)

    def test_a_route_of_its_own_ends_the_appending(self):
        self.start()
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.end()
        self.ledger_line("claude", SID, "rt-own")  # the session composed real work here
        self.assertEqual(self.start()["state"], "route-held-baseline")
        (self.repo / "README.md").write_text("more\n")
        self.assertEqual(self.end()["state"], "route-observed")
        self.assertEqual(self.appended, [])

    def test_commit_made_during_the_turn_counts_but_pulled_history_does_not(self):
        upstream = git(self.repo, "rev-parse", "HEAD").strip()
        git(self.repo, "checkout", "-q", "-b", "upstream")
        (self.repo / "old.py").write_text("x = 1\n")
        git(self.repo, "add", "old.py")
        git(self.repo, "commit", "-q", "-m", "older upstream work",
            env={"GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z", "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z"})
        git(self.repo, "checkout", "-q", "-")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").strip(), upstream)
        self.start()
        git(self.repo, "merge", "-q", "--ff-only", "upstream")
        (self.repo / "greet.py").write_text('print("Hello")\n')
        git(self.repo, "commit", "-q", "-am", "fix greeting")
        self.assertEqual(self.end()["state"], "recorded")
        self.assertEqual(self.calls[0]["sources"], [["greet.py"]])

    def test_a_failed_record_is_retried_with_its_own_turn_not_later_edits(self):
        self.fail_records = 1
        self.start()
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.assertEqual(self.end()["state"], "retry")
        (self.repo / "README.md").write_text("the person's own edit between turns\n")
        self.assertEqual(self.start()["state"], "baseline")  # the retry runs at this boundary
        self.assertEqual(self.end()["state"], "unchanged")
        self.assertEqual([call["sources"] for call in self.calls], [[["greet.py"]], [["greet.py"]]])
        self.assertTrue(self.state()["records"][str(self.root)]["closed"])

    def test_a_record_that_keeps_failing_is_dropped_after_three_attempts(self):
        self.fail_records = 3
        self.start()
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.assertEqual(self.end()["state"], "retry")       # attempt 1
        self.start()                                            # attempt 2, at the next boundary
        self.assertEqual(self.end()["state"], "gave-up")       # attempt 3
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(self.state()["last_failure"]["gave_up"])
        self.assertEqual(self.state()["pending"], [])

    def test_a_turn_without_its_own_start_snapshot_is_not_attributed(self):
        self.start()
        self.assertEqual(self.end()["state"], "unchanged")
        (self.repo / "README.md").write_text("the person's own edit between turns\n")
        # The next turn's start step did not run (busy lock, slow git, killed child).
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.assertEqual(self.end()["state"], "no-start-snapshot")
        self.assertEqual(self.calls, [])

    def test_an_end_without_any_state_records_nothing(self):
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.assertEqual(self.end()["state"], "no-start-snapshot")
        self.assertEqual(self.calls, [])

    def test_non_git_scratch_is_excluded_but_real_temp_git_projects_record(self):
        plain = self.tmp / "work" / "plain"
        plain.mkdir()
        self.assertEqual(T.turn_start("claude", SID, str(plain), self.env)["state"], "no-work-tree")
        scratch_repo = Path(tempfile.gettempdir()) / "clone"
        scratch_repo.mkdir()
        git(scratch_repo, "init", "-q")
        self.assertEqual(T.turn_start("claude", SID, str(scratch_repo), self.env)["state"], "baseline")
        (scratch_repo/'fix.py').write_text('x = 1\n')
        self.assertEqual(T.turn_end('claude', SID, str(scratch_repo), self.env, recorder=self.recorder)['state'], 'recorded')
        self.assertEqual(self.calls[0]['sources'], [['fix.py']])

    def at(self, phase, when, sid=SID):
        step = T.turn_start if phase == "start" else T.turn_end
        return step("claude", sid, str(self.repo), self.env, now=when,
                    recorder=self.recorder, appender=self.appender)

    def test_a_next_start_that_runs_before_the_previous_end_keeps_both_turns(self):
        # A prompt queued or injected right after Stop: turn N+1's start step runs before
        # turn N's end step. Each step carries its hook's own event time.
        t0 = time.time() - 60
        self.assertEqual(self.at("start", t0)["state"], "baseline")
        (self.repo / "greet.py").write_text('print("Hello")\n')      # turn N
        self.assertEqual(self.at("start", t0 + 20)["state"], "baseline")  # N+1's start ran first
        self.assertEqual(self.at("end", t0 + 10)["state"], "recorded")    # N's late end
        self.assertEqual(self.calls[0]["sources"], [["greet.py"]])
        self.assertIsNone(self.state()["turn_ended_at"])                   # N+1 is still open
        (self.repo / "notes.txt").write_text("n+1\n")                     # turn N+1
        self.assertEqual(self.at("end", t0 + 30)["state"], "appended")
        self.assertEqual(self.appended, [[["notes.txt"]]])
        self.assertNotIn("prior_turn", self.state())

    def test_two_starts_before_the_earlier_ends_keep_the_middle_turn(self):
        # sN, sN+1, sN+2, then the three ends in order: N's snapshot was replaced by N+1's
        # at sN+2, so N's late end leaves the kept snapshot for N+1's late end.
        t0 = time.time() - 60
        self.at("start", t0)
        (self.repo / "greet.py").write_text('print("Hello")\n')         # turn N
        self.at("start", t0 + 20)
        (self.repo / "notes.txt").write_text("n+1\n")                    # turn N+1
        self.at("start", t0 + 40)
        self.assertEqual(self.at("end", t0 + 10)["state"], "late-end")   # N: its snapshot is gone
        self.assertEqual(self.state()["prior_turn"]["turn_started_at"], t0 + 20)
        self.assertEqual(self.at("end", t0 + 30)["state"], "recorded")   # N+1
        self.assertEqual(self.calls[0]["sources"], [["notes.txt"]])
        self.assertIsNone(self.state()["turn_ended_at"])                  # N+2 is still open
        self.assertNotIn("prior_turn", self.state())

    def test_an_interrupted_turn_is_still_not_compared(self):
        # No end event ever comes for an interrupted turn: its kept snapshot is dropped by
        # the next turn's own end, and the edits between the turns are nobody's.
        t0 = time.time() - 60
        self.at("start", t0)
        (self.repo / "README.md").write_text("interrupted, then the person's edit\n")
        self.at("start", t0 + 20)
        self.assertIn("prior_turn", self.state())
        self.assertEqual(self.at("end", t0 + 30)["state"], "unchanged")
        self.assertEqual(self.calls, [])
        self.assertNotIn("prior_turn", self.state())

    def test_a_late_end_without_a_kept_snapshot_leaves_the_open_turn(self):
        t0 = time.time() - 60
        self.at("start", t0 + 20)
        self.assertEqual(self.at("end", t0 + 10)["state"], "late-end")
        self.assertIsNone(self.state()["turn_ended_at"])
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.assertEqual(self.at("end", t0 + 30)["state"], "recorded")
        self.assertEqual(self.calls[0]["sources"], [["greet.py"]])

    def test_a_held_lock_is_never_pruned_by_its_age(self):
        lock = T._state_lock(T.state_path(self.env, "claude", SID))
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()
        old = time.time() - T.KEEP_SECONDS - 86400
        os.utime(lock, (old, old))
        with T._locked(lock, 0.0) as held:
            self.assertTrue(held)
            T._prune(lock.parent, time.time())
            self.assertTrue(lock.exists())

    def test_dirty_path_count_is_bounded_without_disabling_records(self):
        for index in range(30):
            (self.repo/f'dirty-{index:04d}.txt').write_text('before\n')
        with mock.patch.object(T, 'MAX_ENTRIES', 2):
            self.assertEqual(self.start()['state'], 'baseline')
            self.assertEqual(len(self.state()['entries']), 2)
            self.assertEqual(self.state()['entries_total'], 30)
            (self.repo/'dirty-0029.txt').write_text('after edit\n')
            self.assertEqual(self.end()['state'], 'recorded')
        last = self.state()['records'][str(self.root)]['last_turn']
        self.assertFalse(last['changed_files_exact'])
        self.assertEqual(last['dirty_before'], 30)
        self.assertEqual(last['dirty_after'], 30)
        self.assertIn('Changed total: None', last['summary'])


class InterruptedRecordTest(Fixture):
    def test_a_turn_starting_while_the_records_route_is_being_created_still_records(self):
        # compose has written the automatic route's ledger line; the state does not know it yet.
        self.ledger_line("claude", SID, "rt-being-created", automatic=True)
        self.assertEqual(self.start()["state"], "baseline")
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.assertEqual(self.end()["state"], "recorded")

    def test_a_turn_whose_record_step_was_killed_is_written_at_the_sessions_next_boundary(self):
        self.start()
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.end_killed()
        self.assertEqual(len(self.state()["pending"]), 1)
        self.assertEqual(self.start()["state"], "baseline")
        self.assertEqual(self.calls[0]["sources"], [["greet.py"]])
        self.assertEqual(self.state()["pending"], [])

    def test_a_turn_left_queued_by_an_ended_session_is_written_by_a_record_pass_in_the_same_folder(self):
        self.start("ended-session")
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.end_killed("ended-session")
        marker = T._pending_marker(self.env, str(self.root), "claude", "ended-session")
        self.assertTrue(marker.exists())
        recorded_as = []

        def recorder(harness, sid, top, root, items, env, remember=None):
            recorded_as.append((harness, sid))
            return self.recorder(harness, sid, top, root, items, env, remember=remember)
        with mock.patch.object(T, "ORPHAN_AFTER", 0.0), \
                mock.patch.object(T, "session_ended", side_effect=lambda h, s: (h, s) == ("claude", "ended-session")):
            self.start("other-session", "codex")
            result = T.turn_end("codex", "other-session", str(self.repo), self.env,
                                recorder=recorder, appender=self.appender)
        self.assertEqual(recorded_as, [("claude", "ended-session")])
        self.assertEqual(result["adopted"], ["claude:ended-session:recorded"])
        ended = self.state("ended-session")
        self.assertEqual(ended["pending"], [])
        self.assertEqual(ended["records"][str(self.root)]["route_id"], "rt-a1")
        self.assertFalse(marker.exists())
        self.assertFalse(marker.parent.exists(), "the folder's marker directory is removed once empty")

    def carry(self, carrier_cwd=None, **patches):
        """The turn end of another session in ``carrier_cwd`` (default this repo)."""
        cwd = str(carrier_cwd or self.repo)
        with mock.patch.multiple(T, **patches):
            T.turn_start("codex", "carrier", cwd, self.env, recorder=self.recorder, appender=self.appender)
            return T.turn_end("codex", "carrier", cwd, self.env, recorder=self.recorder, appender=self.appender)

    def test_a_carry_writes_only_the_folder_the_carrying_turn_is_in(self):
        other = self.tmp / "work" / "other"
        other.mkdir()
        git(other, "init", "-q")
        (other / "y.py").write_text("y = 1\n")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "init")
        unavailable = lambda *a, **k: {"ok": False, "reason": "compose-failed"}  # noqa: E731
        for cwd, path in ((self.repo, self.repo / "greet.py"), (other, other / "y.py")):
            T.turn_start("claude", "ended-session", str(cwd), self.env, recorder=unavailable, appender=self.appender)
            path.write_text("changed\n")
            with self.assertRaises(KeyboardInterrupt):
                T.turn_end("claude", "ended-session", str(cwd), self.env, recorder=self.killed, appender=self.appender)
        self.assertEqual({row["root"] for row in self.state("ended-session")["pending"]},
                         {str(self.root), str(other / ".agent_reports")})
        result = self.carry(ORPHAN_AFTER=0.0, session_ended=lambda h, s: True)
        self.assertEqual(result["adopted"], ["claude:ended-session:recorded"])
        self.assertEqual([call["root"] for call in self.calls], [str(self.root)])
        self.assertEqual([row["root"] for row in self.state("ended-session")["pending"]],
                         [str(other / ".agent_reports")])

    def test_an_idle_but_alive_session_is_not_carried_and_keeps_its_attempts(self):
        self.start("idle-session")
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.end_killed("idle-session")
        os.utime(T.state_path(self.env, "claude", "idle-session"), (time.time() - 3600,) * 2)
        result = self.carry(session_ended=lambda h, s: False)  # quiet for an hour, not gone
        self.assertEqual(result["adopted"], [])
        self.assertEqual(self.calls, [])
        idle = self.state("idle-session")
        self.assertEqual((len(idle["pending"]), idle.get("attempts", 0)), (1, 0))

    def test_a_failed_carry_does_not_use_up_the_sessions_attempts(self):
        self.start("ended-session")
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.end_killed("ended-session")
        self.fail_records = 5
        for _ in range(4):
            self.carry(ORPHAN_AFTER=0.0, session_ended=lambda h, s: h == "claude")
        ended = self.state("ended-session")
        self.assertEqual(len(self.calls), 4)
        self.assertEqual((len(ended["pending"]), ended.get("attempts", 0)), (1, 0))
        self.assertEqual(ended["last_carry_failure"]["reason"], "compose-failed")

    def test_an_ended_session_is_carried_only_after_it_has_been_quiet(self):
        self.start("ended-session")
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.end_killed("ended-session")
        self.assertEqual(T.ORPHAN_AFTER, 120.0)
        gone = {"session_ended": lambda h, s: h == "claude"}
        self.assertEqual(self.carry(**gone)["adopted"], [])  # just ended: its own step may still be running
        state_file = T.state_path(self.env, "claude", "ended-session")
        os.utime(state_file, (time.time() - 200,) * 2)
        self.assertEqual(self.carry(**gone)["adopted"], ["claude:ended-session:recorded"])

    def test_session_ended_answers_only_from_the_runtimes_liveness_proof(self):
        sys.path.insert(0, str(ROOT / "tools"))
        from fleet.collectors import steward
        for answer, expected in (("ended", True), ("live", False), ("unknown", False)):
            with mock.patch.object(steward, "session_liveness", return_value=answer) as probe:
                self.assertIs(T.session_ended("claude", "s"), expected)
            probe.assert_called_once_with("claude", "s")

    def test_a_stale_marker_is_pruned_when_a_new_session_starts(self):
        stale = T._pending_marker(self.env, str(self.root), "claude", "long-gone")
        stale.parent.mkdir(parents=True)
        stale.touch()
        T.turn_start("opencode", "brand-new", str(self.repo), self.env)
        self.assertFalse(stale.exists())
        self.assertFalse(stale.parent.exists())

    def test_a_session_whose_record_pass_is_running_is_not_adopted(self):
        self.start("busy-session")
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.end_killed("busy-session")
        busy = T.state_path(self.env, "claude", "busy-session")
        with mock.patch.object(T, "ORPHAN_AFTER", 0.0), mock.patch.object(T, "session_ended", return_value=True), \
                T._locked(T._record_lock(busy), 0.0):
            self.start("other-session", "codex")
            result = T.turn_end("codex", "other-session", str(self.repo), self.env,
                                recorder=self.recorder, appender=self.appender)
        self.assertEqual(result["adopted"], ["claude:busy-session:busy"])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.state("busy-session")["pending"]), 1)


class OpenRouteTest(Fixture):
    def test_a_route_whose_close_failed_is_kept_and_closed_at_the_next_boundary(self):
        self.close_ok = False
        self.start()
        (self.repo / "greet.py").write_text('print("Hello")\n')
        with mock.patch.object(T, "settle", return_value={"closed": False, "reason": "settle-failed"}):
            self.assertEqual(self.end()["state"], "recorded")
        entry = self.state()["records"][str(self.root)]
        self.assertEqual((entry["route_id"], entry["closed"]), ("rt-a1", False))
        self.assertEqual(self.state()["pending"], [])
        closing = []
        with mock.patch.object(T, "settle", side_effect=lambda route, work, env: closing.append(route)
                               or {"closed": True, "closed_by": "close"}):
            self.start()
        self.assertEqual(closing, [entry["route_file"]])
        self.assertTrue(self.state()["records"][str(self.root)]["closed"])
        self.assertEqual(len(self.calls), 1, "the route is closed, never composed again")

    def test_a_route_created_before_the_step_died_is_kept_before_it_closes(self):
        def dies_after_compose(harness, sid, top, root, items, env, remember=None):
            self.recorder(harness, sid, top, root, items, env, remember=remember)
            raise KeyboardInterrupt("killed between compose and finish")
        self.start()
        (self.repo / "greet.py").write_text('print("Hello")\n')
        with self.assertRaises(KeyboardInterrupt):
            T.turn_end("claude", SID, str(self.repo), self.env, recorder=dies_after_compose,
                       appender=self.appender)
        state = self.state()
        self.assertEqual(state["records"][str(self.root)]["route_id"], "rt-a1")
        self.assertFalse(state["records"][str(self.root)]["closed"])
        self.assertEqual(state["pending"], [])

    def test_a_record_closed_without_a_cycle_keeps_its_evidence_and_later_turns_beside_the_route(self):
        routes = self.root / ".runtime" / "routes"
        routes.mkdir(parents=True)
        route = routes / "rt-a1.json"
        route.write_text("{}")
        work = T._work(self.env, "claude", SID, str(self.root))
        work.mkdir(parents=True)
        (work / "evidence.md").write_text("# Automatic change record\n\n## Turn one\ngreet.py\n")
        (work / "summary.txt").write_text("Automatic record\n")

        def run(argv, **kwargs):
            refused = argv[2] == "finish"
            return subprocess.CompletedProcess(argv, 1 if refused else 0, "", "finish-scoped-tracked-dirt" if refused else "")
        with mock.patch.object(T.subprocess, "run", side_effect=run):
            settled = T.settle(str(route), work, self.env)
        self.assertEqual(settled["closed_by"], "close")
        kept = T.kept_record(str(route))
        self.assertEqual(kept, self.root / ".runtime" / "auto-record" / "rt-a1.md")
        self.assertIn("Turn one", kept.read_text())
        entry = {"route_file": str(route), "route_id": "rt-a1", "closed": True, "closed_by": "close"}
        later = [{"id": "2", "root": str(self.root), "top": str(self.repo), "source": ["README.md"],
                  "section": "## Turn two\nREADME.md\n"}]
        self.assertTrue(T.append("claude", SID, self.repo, str(self.root), entry, later, self.env)["ok"])
        self.assertIn("Turn two", kept.read_text())
        self.assertIn("Turn one", kept.read_text())

    def test_finish_that_times_out_falls_back_to_close(self):
        work = self.tmp / "work-dir"
        work.mkdir()
        (work / "evidence.md").write_text("e\n")
        (work / "summary.txt").write_text("s\n")
        route = self.tmp / "rt-x.json"
        route.write_text("{}")
        commands = []

        def run(argv, **kwargs):
            commands.append(argv[2])
            if argv[2] == "finish":
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(T.subprocess, "run", side_effect=run):
            settled = T.settle(str(route), work, self.env)
        self.assertEqual(commands, ["finish", "close"])
        self.assertEqual((settled["closed"], settled["closed_by"]), (True, "close"))


class ConcurrencyTest(Fixture):
    def test_overlapping_turns_in_the_same_work_tree_are_named(self):
        self.ledger_line("codex", "other-thread")
        T.turn_start("codex", "other-thread", str(self.repo), self.env, now=100.0)
        T.turn_start("opencode", "far-away", str(self.tmp), self.env, now=100.0)
        T.turn_start("claude", "earlier", str(self.repo), self.env, now=10.0)
        T.turn_end("claude", "earlier", str(self.repo), self.env, now=20.0,
                   recorder=self.recorder, appender=self.appender)
        self.assertEqual(T.concurrent_sessions(self.env, "claude", SID, str(self.repo), 50.0, 200.0),
                         ["codex:other-thread"])
        text = T.turn_section("claude", SID, self.repo, ["greet.py"], [], {"entries": {}},
                              {"entries": {"greet.py": [" M", 1, 1]}, "head": ""}, 50.0, 200.0,
                              ["codex:other-thread"], self.env)
        self.assertIn("codex:other-thread (some of these changes may be theirs)", text)

    def test_a_state_lock_held_past_its_wait_skips_the_start(self):
        path = T.state_path(self.env, "claude", SID)
        with T._locked(T._state_lock(path), 0.0) as held, mock.patch.object(T, "STATE_LOCK_WAIT", 0.3):
            self.assertTrue(held)
            self.assertEqual(self.start()["state"], "busy")

    def test_the_next_turn_starts_and_is_kept_while_a_record_is_being_written(self):
        # The person sends the next prompt while the first record is still composing.
        inside = {}

        def slow_recorder(harness, sid, top, root, items, env, remember=None):
            inside["start"] = self.start()["state"]
            (self.repo / "README.md").write_text("the next turn's edit\n")
            return self.recorder(harness, sid, top, root, items, env, remember=remember)
        self.start()
        (self.repo / "greet.py").write_text('print("Hello")\n')
        T.turn_end("claude", SID, str(self.repo), self.env, recorder=slow_recorder, appender=self.appender)
        self.assertEqual(inside["start"], "baseline")
        self.assertEqual(self.end()["state"], "appended")
        self.assertEqual(self.appended, [[["README.md"]]])

    def test_a_turn_that_ends_while_a_record_is_written_is_taken_by_that_record_pass(self):
        ended = {}

        def slow_recorder(harness, sid, top, root, items, env, remember=None):
            self.start()
            (self.repo / "README.md").write_text("the next turn's edit\n")
            worker = threading.Thread(target=lambda: ended.update(self.end()))
            worker.start()
            deadline = time.monotonic() + 10
            while len(self.state().get("pending") or []) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            ended["worker"] = worker
            return self.recorder(harness, sid, top, root, items, env, remember=remember)
        self.start()
        (self.repo / "greet.py").write_text('print("Hello")\n')
        first = T.turn_end("claude", SID, str(self.repo), self.env, recorder=slow_recorder, appender=self.appender)
        ended["worker"].join(30)
        self.assertEqual(first["state"], "appended")
        self.assertEqual(self.appended, [[["README.md"]]])
        self.assertEqual(self.state()["pending"], [])
        self.assertEqual(self.state()["records"][str(self.root)]["turns"], 2)


class FleetChainTest(unittest.TestCase):
    """An automatic record does not replace the session's own route chain in Fleet."""

    def line(self, route_id, key, automatic=False, ts=0):
        row = {"v": 1, "ts": ts, "event": "compose", "route_id": route_id, "campaign_key": key,
               "route_file": f"/r/{route_id}.json"}
        return {**row, "automatic": True} if automatic else row

    def test_an_automatic_record_line_is_marked_and_does_not_end_the_session_chain(self):
        auto = {"route_id": "rt-auto", "tracked_gate_evidence": {"artifact_guard": {
            "satisfied": True, "source": T.GATE_VALUE}}, "selection": {}}
        own = {"route_id": "rt-own", "tracked_gate_evidence": {"artifact_guard": {
            "satisfied": True, "source": "compose-prechecked"}}, "selection": {}}
        self.assertTrue(route_chain.build_line(auto, event="compose", harness="claude", session_id=SID,
                                               route_file="/r/rt-auto.json")["automatic"])
        self.assertNotIn("automatic", route_chain.build_line(own, event="compose", harness="claude",
                                                             session_id=SID, route_file="/r/rt-own.json"))
        lines = [self.line("rt-1", "foo", ts=1), self.line("rt-2", "foo", ts=2),
                 self.line("rt-auto", None, automatic=True, ts=3)]
        self.assertEqual([line["route_id"] for line in route_chain.current_segment(lines)], ["rt-1", "rt-2"])
        # With nothing of its own, the session shows its automatic record.
        self.assertEqual([line["route_id"] for line in route_chain.current_segment(lines[2:])], ["rt-auto"])


class LaunchTest(unittest.TestCase):
    def test_launch_starts_one_detached_child_and_returns(self):
        calls = []
        payload = {"session_id": SID, "cwd": "/work/greet", "hook_event_name": "Stop"}
        self.assertTrue(T.launch("end", "claude", payload, environ={"PATH": "/usr/bin"},
                                 popen=lambda argv, **kw: calls.append((argv, kw))))
        argv, options = calls[0]
        self.assertEqual(argv[2:9], ["end", "--harness", "claude", "--session", SID, "--cwd", "/work/greet"])
        self.assertEqual(argv[9], "--at")
        self.assertLess(abs(float(argv[10]) - time.time()), 60)
        self.assertTrue(options["start_new_session"])
        self.assertEqual(options["stdout"], subprocess.DEVNULL)

    def test_codex_nested_thread_ids_are_found(self):
        calls = []
        payload = {"context": {"thread_id": "019a-thread", "cwd": "/w"}}
        self.assertTrue(T.launch("start", "codex", payload, environ={},
                                 popen=lambda argv, **kw: calls.append(argv)))
        self.assertIn("019a-thread", calls[0])

    def test_workers_owners_ci_switch_and_bad_ids_start_nothing(self):
        payload = {"session_id": SID, "cwd": "/w"}
        never = lambda *a, **k: self.fail("spawned")  # noqa: E731
        for env in ({"AGENT_DISPATCH_DEPTH": "1"}, {"AGENT_SESSION_ROLE": "owner"}, {"CI": "true"},
                    {"HEARTING_AUTO_RECORD": "off"}, {"AGENT_DISPATCH_CHILD": "1"},
                    {"AGENT_DISPATCH_REGISTERED_WORKER": "1"}):
            self.assertFalse(T.launch("start", "claude", payload, environ=env, popen=never), env)
        self.assertFalse(T.launch("start", "claude", {"session_id": "../x"}, environ={}, popen=never))
        self.assertFalse(T.launch("later", "claude", payload, environ={}, popen=never))
        self.assertFalse(T.launch("start", "claude", payload, environ={}))  # test process, real spawn

    def test_the_record_commands_start_no_background_model(self):
        env = T._child_env({"PATH": "/usr/bin"}, "claude", SID)
        self.assertEqual(env["HEARTING_WORKFLOW_GROUP_REVIEW"], "off")
        self.assertEqual(env["HEARTING_CAMPAIGN_TITLE_AUTO"], "off")
        self.assertEqual(env["AGENT_DISPATCH_CALLER_HARNESS"], "claude")


class ChildExemptionTest(Fixture):
    """The child step OpenCode starts directly applies the same exemptions as ``launch``."""

    def test_the_child_step_applies_the_shared_exemptions(self):
        for extra in ({"AGENT_SESSION_ROLE": "owner"}, {"CI": "true"},
                      {"AGENT_DISPATCH_REGISTERED_WORKER": "1"}, {"HEARTING_AUTO_RECORD": "off"}):
            with mock.patch.dict(os.environ, {**self.env, **extra}):
                self.assertEqual(T.main(["start", "--harness", "opencode", "--session", "oc-1",
                                         "--cwd", str(self.repo)]), 0)
            self.assertFalse(T.state_path(self.env, "opencode", "oc-1").exists(), extra)
        with mock.patch.dict(os.environ, self.env):
            for key in ("AGENT_SESSION_ROLE", "CI", "AGENT_DISPATCH_REGISTERED_WORKER", "AGENT_DISPATCH_DEPTH",
                        "AGENT_DISPATCH_CHILD", "OPENCODE_DISPATCH_SLUG", "HEARTING_AUTO_RECORD"):
                os.environ.pop(key, None)
            T.main(["start", "--harness", "opencode", "--session", "oc-1", "--cwd", str(self.repo)])
        self.assertEqual(self.state("oc-1", "opencode")["entries"], {})

    def test_the_child_step_dates_its_turn_by_the_hooks_event_time(self):
        with mock.patch.dict(os.environ, self.env):
            for key in ("AGENT_SESSION_ROLE", "CI", "AGENT_DISPATCH_REGISTERED_WORKER", "AGENT_DISPATCH_DEPTH",
                        "AGENT_DISPATCH_CHILD", "OPENCODE_DISPATCH_SLUG", "HEARTING_AUTO_RECORD"):
                os.environ.pop(key, None)
            at = time.time() - 5
            T.main(["start", "--harness", "opencode", "--session", "oc-2", "--cwd", str(self.repo),
                    "--at", f"{at:.6f}"])
            self.assertAlmostEqual(self.state("oc-2", "opencode")["turn_started_at"], at, places=3)
            # A time from the future is not an event time: the step's own clock is used.
            T.main(["start", "--harness", "opencode", "--session", "oc-3", "--cwd", str(self.repo),
                    "--at", f"{time.time() + 3600:.6f}"])
            self.assertLess(self.state("oc-3", "opencode")["turn_started_at"], time.time() + 1)


class EndToEndTest(Fixture):
    """The real compose, finish and producer checkpoint, as the detached step runs them."""

    def setUp(self):
        super().setUp()
        self.env = {**self.env, "AGENT_HOME": str(ROOT)}
        # An unrelated, more recently changed campaign the record must not join.
        (self.root / "campaigns" / "2026-09-01_docs").mkdir(parents=True)
        (self.root / "campaigns" / "2026-10-01_payments-migration").mkdir(parents=True)
        os.utime(self.root / "campaigns" / "2026-09-01_docs", (1, 1))

    def turn(self, change):
        T.turn_start("claude", SID, str(self.repo), self.env)
        change()
        return T.turn_end("claude", SID, str(self.repo), self.env)

    def test_one_closed_unassigned_record_that_later_turns_append_to(self):
        started = time.monotonic()
        result = self.turn(lambda: subprocess.run(["sed", "-i", "s/Helo/Hello/", str(self.repo / "greet.py")],
                                                  check=True))
        elapsed = time.monotonic() - started
        self.assertEqual(result["state"], "recorded", result)
        entry = result["records"][str(self.root)]
        self.assertTrue(entry["closed"], entry)
        route = json.loads(Path(entry["route_file"]).read_text())
        self.assertTrue(T.is_automatic_route(route))
        self.assertEqual(route["selection"]["shape"], "direct")
        self.assertIsNone(route.get("campaign_key"))
        self.assertTrue(route.get("campaign_unassigned"))
        added = {p.name for p in (self.root / "campaigns").iterdir() if p.is_dir()} - {
            "2026-09-01_docs", "2026-10-01_payments-migration"}
        self.assertTrue(added and all(name.endswith("unassigned") for name in added), added)
        self.assertEqual(list((self.root / "campaigns" / "2026-10-01_payments-migration").iterdir()), [])
        outcome = json.loads(Path(entry["route_file"]).with_suffix(".outcome.json").read_text())
        self.assertTrue(outcome["terminal_gate_proven"])
        evidence = Path(outcome["terminal_gates"]["inline"]["evidence"])
        self.assertIn("greet.py | 2 +-", evidence.read_text())
        ledger = [json.loads(line) for line in
                  (self.ledgers / "claude" / f"{SID}.jsonl").read_text().splitlines()]
        self.assertEqual([(line["event"], line["campaign_key"], line.get("automatic")) for line in ledger],
                         [("compose", None, True)])

        result = self.turn(lambda: (self.repo / "README.md").write_text("notes, revised\n"))
        self.assertEqual(result["state"], "appended", result)
        appended = evidence.parents[1] / T.APPENDED
        self.assertIn("README.md", appended.read_text())
        self.assertEqual(len(list((self.root / ".runtime" / "routes").glob("rt-*.outcome.json"))), 1)

        sys.path.insert(0, str(HERE))
        import artifact_workflow_group_review as review
        cycle_id = json.loads(Path(entry["route_file"]).read_text())["route_id"]
        cycle = next(row for row in review.producer.list_cycle_records(self.root)
                     if row.get("route_id") == cycle_id)
        chosen = review.select_targets(self.root, None, cycles=[cycle["cycle_id"]], auto=True)
        self.assertIn({"cycle_id": cycle["cycle_id"], "reason": "automatic-record"}, chosen.skipped)
        print(f"\nturn_change_record end-to-end record seconds={elapsed:.2f}", file=sys.stderr)

    def test_a_route_left_open_by_a_failed_close_is_closed_at_the_next_turn(self):
        real = T.settle
        with mock.patch.object(T, "settle", return_value={"closed": False, "reason": "settle-failed"}):
            result = self.turn(lambda: (self.repo / "greet.py").write_text('print("Hello")\n'))
        entry = result["records"][str(self.root)]
        self.assertFalse(entry["closed"])
        self.assertFalse(Path(entry["route_file"]).with_suffix(".outcome.json").exists())
        with mock.patch.object(T, "settle", side_effect=real):
            self.assertEqual(T.turn_start("claude", SID, str(self.repo), self.env)["state"], "baseline")
        self.assertTrue(Path(entry["route_file"]).with_suffix(".outcome.json").exists())
        self.assertTrue(self.state()["records"][str(self.root)]["closed"])
        self.assertEqual(len([path for path in (self.root / ".runtime" / "routes").glob("rt-*.json")
                              if "." not in path.stem]), 1)


class WiringTest(unittest.TestCase):
    def test_claude_registers_the_bridge_on_prompt_and_stop(self):
        settings = json.loads((ROOT / "adapters/claude/settings.json").read_text())
        commands = {event: [hook["command"] for entry in settings["hooks"][event] for hook in entry["hooks"]]
                    for event in ("UserPromptSubmit", "Stop")}
        self.assertTrue(any(c.endswith("python3 turn-change-record.py start") for c in commands["UserPromptSubmit"]))
        self.assertTrue(any(c.endswith("python3 turn-change-record.py end") for c in commands["Stop"]))
        self.assertTrue((ROOT / "adapters/claude/hooks/turn-change-record.py").resolve().samefile(
            ROOT / "hooks/turn-change-record.py"))

    def test_claude_bridge_is_silent_and_quick(self):
        payload = json.dumps({"session_id": SID, "cwd": "/nonexistent", "hook_event_name": "Stop"})
        env = {**os.environ, "HEARTING_AUTO_RECORD": "off"}
        started = time.monotonic()
        result = subprocess.run([sys.executable, str(ROOT / "hooks/turn-change-record.py"), "end"],
                                input=payload, capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertLess(time.monotonic() - started, 5)

    def test_codex_prompt_and_stop_hooks_call_the_shared_launcher(self):
        stop = (ROOT / "adapters/codex/hooks/stop-lifecycle.py").read_text()
        prompt = (ROOT / "adapters/codex/hooks/userprompt-lifecycle.py").read_text()
        self.assertIn('turn_change_record.launch("end", "codex", payload)', stop)
        self.assertIn('launch_turn_record("start", "codex", payload)', prompt)

    def test_opencode_plugin_starts_the_shared_step_for_main_sessions_only(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        with tempfile.TemporaryDirectory(prefix="opencode-turn-record-") as raw:
            script = '''import assert from "node:assert/strict";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { pathToFileURL } from "node:url";
const spawnCalls = [];
childProcess.spawnSync = () => ({ status: 0, stdout: "[]", stderr: "" });
childProcess.spawn = (command, args, options = {}) => {
  spawnCalls.push({ command, args, options });
  return { on() {}, unref() {}, stdin: { on() {}, end() {} } };
};
syncBuiltinESMExports();
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.PLUGIN_PATH));
const hooks = await AgentHarnessGuards({ directory: process.env.HOME, worktree: process.env.HOME,
  client: {} }, { initialize: false });
await hooks.event({ event: { type: "session.created",
  properties: { info: { id: "oc-child", parentID: "oc-turn" } } } });
for (const sid of ["oc-turn", "oc-child"]) {
  await hooks["chat.message"]({ sessionID: sid, messageID: "m-" + sid },
    { parts: [{ type: "text", text: "fix the typo" }] });
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: sid } } });
}
await new Promise((resolve) => setTimeout(resolve, 50));
const turns = spawnCalls.filter((call) => String(call.args?.[0] || "").endsWith("turn_change_record.py"));
assert.deepEqual(turns.map((call) => call.args.slice(1, 6)),
  [["start", "--harness", "opencode", "--session", "oc-turn"],
   ["end", "--harness", "opencode", "--session", "oc-turn"]]);
assert.ok(turns.every((call) => call.options.detached === true && call.args[7] === process.env.HOME));
hooks.dispose?.({ silent: true });
'''
            env = {**os.environ, "PLUGIN_PATH": str(ROOT / "adapters/opencode/plugins/hearting-guards.js"),
                   "HOME": raw, "XDG_CONFIG_HOME": str(Path(raw) / ".config"),
                   "XDG_DATA_HOME": str(Path(raw) / ".data"), "XDG_STATE_HOME": str(Path(raw) / ".state"),
                   "AGENT_HOME": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                   "AGENT_SESSION_ROLE": "", "AGENT_DISPATCH_CHILD": "",
                   "AGENT_DISPATCH_DEPTH": "", "OPENCODE_DISPATCH_SLUG": "",
                   "FLEET_TITLE_REFRESH": "", "HERDR_PANE_ID": ""}
            result = subprocess.run([node, "--input-type=module", "-e", script], cwd=ROOT,
                                    env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_opencode_plugin_runs_an_older_step_again_without_the_event_time(self):
        # An older turn_change_record.py rejects --at with exit 2: the same step runs once
        # more without it. Any other exit, or an exit 2 of that retry, is left alone.
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        with tempfile.TemporaryDirectory(prefix="opencode-turn-record-old-") as raw:
            script = '''import assert from "node:assert/strict";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { pathToFileURL } from "node:url";
const turns = [];
childProcess.spawnSync = () => ({ status: 0, stdout: "[]", stderr: "" });
childProcess.spawn = (command, args, options = {}) => {
  const handlers = {};
  const call = { args, handlers };
  if (String(args?.[0] || "").endsWith("turn_change_record.py")) turns.push(call);
  return { on(event, handler) { handlers[event] = handler }, unref() {},
    stdin: { on() {}, end() {} } };
};
syncBuiltinESMExports();
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.PLUGIN_PATH));
const hooks = await AgentHarnessGuards({ directory: process.env.HOME, worktree: process.env.HOME,
  client: {} }, { initialize: false });
await hooks["chat.message"]({ sessionID: "oc-old", messageID: "m-1" },
  { parts: [{ type: "text", text: "fix the typo" }] });
await new Promise((resolve) => setTimeout(resolve, 50));
assert.equal(turns.length, 1);
assert.deepEqual(turns[0].args.slice(8, 9), ["--at"]);
turns[0].handlers.exit(2);
assert.equal(turns.length, 2);
assert.deepEqual(turns[1].args.slice(1, 8),
  ["start", "--harness", "opencode", "--session", "oc-old", "--cwd", process.env.HOME]);
assert.equal(turns[1].args.length, 8);
assert.equal(turns[1].handlers.exit, undefined);
await hooks.event({ event: { type: "session.idle", properties: { sessionID: "oc-old" } } });
await new Promise((resolve) => setTimeout(resolve, 50));
assert.equal(turns.length, 3);
turns[2].handlers.exit(0);
assert.equal(turns.length, 3);
hooks.dispose?.({ silent: true });
'''
            env = {**os.environ, "PLUGIN_PATH": str(ROOT / "adapters/opencode/plugins/hearting-guards.js"),
                   "HOME": raw, "XDG_CONFIG_HOME": str(Path(raw) / ".config"),
                   "XDG_DATA_HOME": str(Path(raw) / ".data"), "XDG_STATE_HOME": str(Path(raw) / ".state"),
                   "AGENT_HOME": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                   "AGENT_SESSION_ROLE": "", "AGENT_DISPATCH_CHILD": "",
                   "AGENT_DISPATCH_DEPTH": "", "OPENCODE_DISPATCH_SLUG": "",
                   "FLEET_TITLE_REFRESH": "", "HERDR_PANE_ID": ""}
            result = subprocess.run([node, "--input-type=module", "-e", script], cwd=ROOT,
                                    env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TinyFixContractTest(Fixture):
    def native(self, harness='codex'):
        path = self.tmp / f'{SID}.jsonl'
        path.write_text('')
        return path

    def row(self, path, payload, **extra):
        from datetime import datetime, timezone
        with path.open('a') as stream:
            stream.write(json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(),
                                     'type': 'response_item', 'payload': payload, **extra}) + '\n')

    def test_misclassified_large_change_keeps_counts_bounded_paths_checks_and_final_reply(self):
        path = self.native()
        T.turn_start('codex', SID, str(self.repo), self.env, payload={'transcript_path': str(path)})
        for index in range(260):
            (self.repo / f'changed-{index:03d}.py').write_text('x = 42\n' * 60)
        self.row(path, {'type': 'function_call', 'name': 'functions.exec_command', 'call_id': 'check',
                        'arguments': json.dumps({'cmd': 'python3 -m unittest'})})
        self.row(path, {'type': 'function_call_output', 'call_id': 'check',
                        'output': json.dumps({'exit_code': 1, 'output': 'FAILED: details stay at native source'})})
        self.row(path, {'type': 'message', 'role': 'assistant', 'channel': 'final',
                        'content': [{'type': 'output_text', 'text': 'Changed the implementation; tests failed.'}]})
        captured = []
        def record(*args, **kwargs):
            captured.extend(args[4])
            return self.recorder(*args, **kwargs)
        self.assertEqual(T.turn_end('codex', SID, str(self.repo), self.env, recorder=record)['state'], 'recorded')
        self.assertEqual(len(captured[0]['source']), T.MAX_LISTED)
        self.assertEqual(captured[0]['source_count'], 260)
        body = captured[0]['section']
        for i in range(T.MAX_LISTED):
            self.assertIn(f'changed-{i:03d}.py', body)
        self.assertNotIn('changed-259.py', body)
        self.assertIn('Changed files (260)', body)
        self.assertIn('60 paths omitted', body)
        self.assertLessEqual(len(body.encode('utf-8')), T.EVIDENCE_LIMIT)
        self.assertIn('python3 -m unittest', body)
        self.assertIn('exit codes [1]', body)
        self.assertIn('tests failed.', body)
        self.assertIn('#byte=', body)
        self.assertNotIn('details stay at native source', body)
        supplement = T.supplementary_records('codex', SID, str(self.repo), self.env)
        self.assertEqual(supplement[0]['changed_files'], 260)
        self.assertIn('tests failed.', supplement[0]['summary'])
        self.assertEqual(T.supplementary_records('codex', 'different-sid', str(self.repo), self.env), [])

    def test_many_checks_keep_final_reply_in_the_fleet_and_tidy_supplement(self):
        path = self.native()
        T.turn_start('codex', SID, str(self.repo), self.env, payload={'transcript_path':str(path)})
        (self.repo/'greet.py').write_text('print("Hello")\n')
        for index in range(80):
            self.row(path, {'type':'function_call','name':'functions.exec_command','call_id':str(index),
                            'arguments':json.dumps({'cmd':'pytest '+('focused-check '*12)})})
            self.row(path, {'type':'function_call_output','call_id':str(index),'output':'{"exit_code":0}'})
        self.row(path, {'type':'message','role':'assistant','channel':'final',
                       'content':[{'type':'output_text','text':'Final conclusion survives many checks.'}]})
        self.assertEqual(T.turn_end('codex', SID, str(self.repo), self.env, recorder=self.recorder)['state'],'recorded')
        supplement = T.supplementary_records('codex', SID, str(self.repo), self.env)[0]
        self.assertIn('Final conclusion survives many checks.',supplement['summary'])
        self.assertIn('Reference:',supplement['summary'])

    def test_codex_code_mode_text_has_no_exit_authority(self):
        import tidy_transcripts as tt
        path = self.native()
        anchor = tt.turn_anchor('codex', SID, str(path))
        self.row(path, {'type':'custom_tool_call','name':'exec','call_id':'code-mode',
                        'input':'text(await tools.exec_command({cmd:"pytest"}));'})
        result = {'i':0,'status':'fulfilled','value':{'exit_code':1,'output':'failure stdout remains private'}}
        self.row(path, {'type':'custom_tool_call_output','call_id':'code-mode',
                        'output':[{'type':'input_text','text':'Script completed\nWall time: 0.1s\nOutput:\n'},
                                  {'type':'input_text','text':json.dumps(result)}]})
        facts = tt.turn_observations('codex', SID, anchor)
        self.assertEqual(len(facts['commands']),1)
        self.assertEqual(facts['commands'][0]['state'],'unknown')
        self.assertEqual(facts['commands'][0]['exit_codes'],[])
        self.assertNotIn('stdout remains private',json.dumps(facts))

    def test_native_running_failure_and_stop_reply_do_not_become_a_pass(self):
        import tidy_transcripts as tt
        path = self.native()
        anchor = tt.turn_anchor('codex', SID, str(path))
        for ident, output in [('running', {'session_id': 123, 'output': 'working'}),
                              ('done', {'exit_code': 0, 'output': 'Process exited with code 91'})]:
            self.row(path, {'type': 'function_call', 'name': 'functions.exec_command', 'call_id': ident,
                            'arguments': json.dumps({'cmd': 'pytest'})})
            self.row(path, {'type': 'function_call_output', 'call_id': ident, 'output': json.dumps(output)})
        facts = tt.turn_observations('codex', SID, anchor,
            {'last_assistant_message': 'Interrupted; no final verification.', 'interrupted': True})
        self.assertEqual(facts['terminal'], 'interrupted')
        self.assertEqual(facts['commands'][0]['state'], 'running')
        self.assertEqual(facts['commands'][1]['exit_codes'], [0])
        self.assertEqual(facts['reply_reference'], 'native Stop.last_assistant_message')
        self.assertNotIn('PASS', json.dumps(facts))

    def test_closed_manual_route_does_not_disable_new_records_but_unknown_does(self):
        self.ledger_line()
        self.assertEqual(self.start()['state'], 'route-held-baseline')
        routes = self.root / '.runtime/routes'
        routes.mkdir(parents=True)
        (routes / 'rt-own.outcome.json').write_text(json.dumps({'route_id': 'rt-own', 'route_hash': 'hash',
            'terminal_gate_proven': False, 'closed_at': '2026-11-05T00:00:00Z'}))
        self.assertEqual(self.start()['state'], 'baseline')
        (self.repo / 'greet.py').write_text('print("Hello")\n')
        self.assertEqual(self.end()['state'], 'recorded')

    def test_promotion_links_snapshot_and_checks_without_changing_user_dirt(self):
        path = self.native()
        (self.repo / 'README.md').write_text('user owns these bytes\n')
        T.turn_start('codex', SID, str(self.repo), self.env, payload={'transcript_path': str(path)})
        (self.repo / 'greet.py').write_text('print("Hello")\n')
        self.row(path, {'type': 'function_call', 'name': 'functions.exec_command', 'call_id': 'test',
                        'arguments': json.dumps({'cmd': 'pytest'})})
        self.row(path, {'type': 'function_call_output', 'call_id': 'test', 'output': '{"exit_code":0}'})
        before = git(self.repo, 'diff')
        route = {'route_id': 'rt-promoted', 'artifact_root': str(self.root), 'cwd': str(self.tmp/'new-worktree')}
        with mock.patch.object(route_chain, 'writer_identity', return_value=('codex', SID)):
            evidence = T.link_promotion(route, self.env, source_cwd=str(self.repo))
        body = Path(evidence).read_text()
        self.assertIn('Start snapshot', body)
        self.assertIn('README.md', body)
        self.assertIn('greet.py', body)
        self.assertIn('exit codes [0]', body)
        self.assertEqual(git(self.repo, 'diff'), before)
        self.assertEqual((self.repo/'README.md').read_text(), 'user owns these bytes\n')
        nested = self.repo/'src'
        nested.mkdir()
        route_in_place = {**route, 'route_id':'rt-in-place', 'cwd':str(nested)}
        with mock.patch.object(route_chain, 'writer_identity', return_value=('codex', SID)):
            in_place = T.link_promotion(route_in_place,self.env,source_cwd=str(self.repo))
        self.assertIn('already remain in this same worktree',Path(in_place).read_text())
        from types import SimpleNamespace
        import worker_bootstrap
        route_file = self.root/'promoted-route.json'
        route_file.write_text(json.dumps(route))
        args = SimpleNamespace(route_id='rt-promoted',route_file=str(route_file),worker_type='frame')
        for harness in ('claude','codex','opencode'):
            prompt = worker_bootstrap.contract_read_prompt(args, harness)
            self.assertIn(evidence, prompt)
            self.assertIn('not completion or write authority', prompt)

    def test_late_end_native_range_excludes_the_new_turn(self):
        import tidy_transcripts as tt
        path = self.native()
        first = tt.turn_anchor('codex', SID, str(path))
        self.row(path, {'type': 'message', 'role': 'assistant', 'channel': 'final',
                        'content': [{'type': 'output_text', 'text': 'first turn result'}]})
        boundary = path.stat().st_size
        self.row(path, {'type': 'message', 'role': 'assistant', 'channel': 'final',
                        'content': [{'type': 'output_text', 'text': 'second turn private result'}]})
        facts = tt.turn_observations('codex', SID, first, until=boundary)
        self.assertEqual(facts['reply'], 'first turn result')

    def test_unicode_stop_handoff_stays_within_the_child_byte_bound(self):
        seen = {}
        def spawn(command, **kwargs):
            raw = kwargs['stdin'].read()
            self.assertLess(len(raw), 24 * 1024)
            seen.update(json.loads(raw))
            return mock.Mock()
        self.assertTrue(T.launch('end','codex',{'session_id':SID,'cwd':str(self.repo),
            'transcript_path':'/'+'경로'*3000,'last_assistant_message':'최종 답변 '*5000},
            environ=self.env,popen=spawn))
        self.assertTrue(seen['last_assistant_message'].startswith('최종 답변'))

    def test_start_before_new_codex_prompt_does_not_replay_the_previous_turn(self):
        import tidy_transcripts as tt
        path = self.native()
        self.row(path, {'type':'message','role':'user','content':[{'type':'input_text','text':'old task'}]},
                 timestamp='2026-01-01T00:00:00Z')
        self.row(path, {'type':'message','role':'assistant','channel':'final',
                       'content':[{'type':'output_text','text':'old result must not become current'}]},
                 timestamp='2026-01-01T00:00:01Z')
        anchor = tt.turn_anchor('codex',SID,str(path),at=time.time())
        self.assertEqual(anchor['cursor'],path.stat().st_size)
        self.row(path, {'type':'message','role':'assistant','channel':'final',
                       'content':[{'type':'output_text','text':'new result'}]})
        facts = tt.turn_observations('codex',SID,anchor)
        self.assertEqual(facts['reply'],'new result')

    def test_three_native_sources_and_old_payload(self):
        import sqlite3
        import tidy_transcripts as tt
        claude = self.native('claude')
        anchor = tt.turn_anchor('claude', SID, str(claude))
        self.row(claude, {}, type='assistant', message={'role':'assistant','content':[
            {'type':'tool_use','name':'Bash','id':'c','input':{'command':'pytest'}}]})
        self.row(claude, {}, type='user', toolUseResult={'exit_code':2}, message={'role':'user','content':[
            {'type':'tool_result','tool_use_id':'c','is_error':True,'content':'failed'}]})
        facts = tt.turn_observations('claude', SID, anchor, {'last_assistant_message':'Failed check'})
        self.assertEqual(facts['commands'][0]['state'], 'failed')
        self.assertEqual(facts['commands'][0]['exit_codes'], [2])
        db = self.tmp/'native.db'
        with sqlite3.connect(db) as con:
            con.executescript('CREATE TABLE message(id TEXT,session_id TEXT,data TEXT); CREATE TABLE part(session_id TEXT,message_id TEXT,data TEXT,time_created INT);')
            con.execute('INSERT INTO message VALUES(?,?,?)',('u',SID,'{"role":"user"}'))
            con.execute('INSERT INTO message VALUES(?,?,?)',('a',SID,'{"role":"assistant"}'))
            con.execute('INSERT INTO part VALUES(?,?,?,?)',(SID,'u','{"type":"text","text":"fix"}',1))
        anchor = tt.turn_anchor('opencode', SID, str(db))
        with sqlite3.connect(db) as con:
            con.execute('INSERT INTO part VALUES(?,?,?,?)',(SID,'a',json.dumps({'type':'tool','tool':'bash','callID':'o',
                'state':{'status':'completed','input':{'command':'pytest'},'metadata':{'exit':1}}}),2))
            con.execute('INSERT INTO part VALUES(?,?,?,?)',(SID,'a','{"type":"text","text":"OpenCode failed check"}',3))
            con.execute('INSERT INTO part VALUES(?,?,?,?)',('another-sid','a','{"type":"text","text":"never join this"}',4))
        facts = tt.turn_observations('opencode', SID, anchor)
        self.assertEqual(facts['commands'][0]['exit_codes'], [1])
        self.assertEqual(facts['reply'], 'OpenCode failed check')
        facts = tt.turn_observations('codex', SID, {}, {})
        self.assertEqual(facts['coverage'], 'unknown')
        self.assertTrue(facts['error'])

    def test_launcher_hands_native_clues_to_child_without_reading_the_transcript(self):
        seen = {}
        def spawn(command, **kwargs):
            seen.update(json.loads(kwargs['stdin'].read()))
            return mock.Mock()
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('hook must not read native history')):
            self.assertTrue(T.launch('end','codex',{'session_id':SID,'cwd':str(self.repo),
                'transcript_path':'/unreadable/native.jsonl','last_assistant_message':'normal final answer'},
                environ=self.env,popen=spawn))
        self.assertEqual(seen['last_assistant_message'], 'normal final answer')


class ReviewRegressionTest(Fixture):
    def test_large_git_status_and_legacy_queue_have_bounded_state_and_evidence(self):
        paths = [f'source-{i:05d}.py' for i in range(25000)]
        status = b''.join(b'?? '+path.encode()+b'\0' for path in paths)
        original = T._git
        def probe(top, args, env, **kwargs):
            if args[0] == 'status':
                return subprocess.CompletedProcess(args, 0, status, b'')
            return original(top, args, env, **kwargs)
        self.start()
        captured = []
        def record(*args, **kwargs):
            captured.extend(args[4])
            return self.recorder(*args, **kwargs)
        with mock.patch.object(T, '_git', side_effect=probe):
            result = T.turn_end('claude', SID, str(self.repo), self.env, recorder=record,
                payload={'last_assistant_message':'The oversized turn failed verification; final reply preserved.'})
        self.assertEqual(result['state'], 'recorded')
        item = captured[0]
        self.assertEqual(item['source_count'], 25000)
        self.assertLessEqual(len(item['source']), T.MAX_LISTED)
        self.assertLessEqual(len(item['section'].encode()), T.EVIDENCE_LIMIT)
        self.assertIn('Changed files (25000)', item['section'])
        self.assertIn('truncated', item['section'])
        self.assertIn('final reply preserved', item['section'])
        snap = {path:['??',1,1] for path in paths}
        queued = {**item, 'source':paths, 'fresh':paths, 'section':'old large inventory\n'*100000,
                  'observations':'Last reply: preserve this legacy queued observation.'}
        data = {'entries':snap,'pending':[dict(queued,id=str(i)) for i in range(T.MAX_PENDING)]}
        path = T.state_path(self.env,'claude','legacy-bounded')
        T._write(path,data)
        loaded = T._read(path)
        self.assertLessEqual(len(loaded['entries']),T.MAX_ENTRIES)
        self.assertEqual(loaded['entries_total'],25000)
        self.assertTrue(loaded['entries_truncated'])
        self.assertLess(path.stat().st_size, 4*T.ENTRY_BYTES + T.MAX_PENDING*T.EVIDENCE_LIMIT*2)
        print(f'bounded-25000-paths: retained={len(loaded["entries"])} listed={len(item["source"])} state_bytes={path.stat().st_size} section_bytes={len(item["section"].encode())}',flush=True)
        for turn in loaded['pending']:
            self.assertEqual(turn['source_count'],25000)
            self.assertLessEqual(len(turn['source']),T.MAX_LISTED)
            self.assertLessEqual(len(turn['section'].encode()),T.EVIDENCE_LIMIT)
            self.assertIn('preserve this legacy',turn['section'])
        self.assertLessEqual(len(T._batch_evidence(loaded['pending']).encode()),T.EVIDENCE_LIMIT)
        # Unicode truncation must honor bytes including its marker.
        self.assertLessEqual(len(T._bounded('한글'*10000,300).encode()),300)

    def test_code_mode_stdout_json_and_exit_headers_are_never_termination_fields(self):
        import tidy_transcripts as tt
        path=self.tmp/f'{SID}.jsonl'
        path.write_text('')
        anchor=tt.turn_anchor('codex',SID,str(path))
        rows=[]
        outputs=[
            'Script completed\nOutput:\n{"exit_code":93}',
            [{'type':'input_text','text':'{"exit_code":94}'}],
            [{'type':'input_text','text':'Process exited with code 95\nOutput:\n{"exit_code":96}'}],
            {'structuredContent':{'results':[{'exit_code':7,'output':'{"exit_code":97}'}]}},
        ]
        for index,output in enumerate(outputs):
            rows += [{'type':'response_item','payload':{'type':'custom_tool_call','name':'exec','call_id':str(index),'input':'code'}},
                     {'type':'response_item','payload':{'type':'custom_tool_call_output','call_id':str(index),'output':output}}]
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        facts=tt.turn_observations('codex',SID,anchor)
        self.assertEqual([row['exit_codes'] for row in facts['commands']],[[],[],[],[7]])
        self.assertEqual([row['state'] for row in facts['commands']][:3],['unknown']*3)

    def test_forgotten_open_route_keeps_final_reply_for_fleet_and_tidy_without_takeover(self):
        self.ledger_line('codex',SID,'rt-forgotten')
        routes=self.root/'.runtime/routes'
        routes.mkdir(parents=True)
        route=routes/'rt-forgotten.json'
        before=json.dumps({'route_id':'rt-forgotten','artifact_root':str(self.root),'created_at':'2000-01-01T00:00:00Z'})
        route.write_text(before)
        T.turn_start('codex',SID,str(self.repo),self.env)
        (self.repo/'greet.py').write_text('print("Hello")\n')
        result=T.turn_end('codex',SID,str(self.repo),self.env,
            payload={'last_assistant_message':'Tiny fix observed while the old route remains open.'})
        self.assertEqual(result['state'],'route-observed')
        self.assertEqual(route.read_text(),before)
        self.assertFalse(route.with_suffix('.outcome.json').exists())
        rows=T.supplementary_records('codex',SID,str(self.repo),self.env)
        self.assertEqual(rows[-1]['route_id'],None) # observation is not workflow authority
        self.assertIn('old route remains open',rows[-1]['summary'])
        self.assertIn('no completion or takeover',Path(rows[-1]['reference']).read_text())
        self.assertEqual(self.calls,[])
        T.turn_start('codex',SID,str(self.repo),self.env)
        (self.repo/'README.md').write_text('a second small fix\n')
        T.turn_end('codex',SID,str(self.repo),self.env,
            payload={'last_assistant_message':'Second supplementary fix; workflow ownership stays unchanged.'})
        history=Path(rows[-1]['reference']).read_text()
        self.assertIn('old route remains open',history)
        self.assertIn('Second supplementary fix',history)
        self.assertEqual(route.read_text(),before)
        self.assertFalse(route.with_suffix('.outcome.json').exists())

    def test_real_compose_start_carries_dirty_origin_to_a_fresh_worktree(self):
        import worker_bootstrap
        from types import SimpleNamespace
        path=self.tmp/f'{SID}.jsonl'
        path.write_text('')
        env={**self.env,**GIT_ENV,**T.QUIET_ENV,'AGENT_HOME':str(ROOT),'CODEX_THREAD_ID':SID,
             'AGENT_DISPATCH_CALLER_HARNESS':'codex','GIT_CEILING_DIRECTORIES':str(self.tmp),
             'DISPATCH_DEFAULTS_CONFIG':str(ROOT/'profiles/dispatch-defaults.yaml')}
        (self.repo/'README.md').write_text('user-only dirty bytes\n')
        T.turn_start('codex',SID,str(self.repo),env,payload={'transcript_path':str(path)})
        (self.repo/'greet.py').write_text('print("Hello")\n')
        original_diff=git(self.repo,'diff')
        task=self.tmp/'promotion-task.md'
        task.write_text('Finish the greeting fix, preserving unrelated user edits.\n')
        command=[sys.executable,str(ROOT/'utilities/capability-route.py'),'compose','--start','--shape','direct',
                 '--capability','autopilot-code','--capability-mode','dev','--unassigned','--slug','promotion-e2e',
                 '--cwd',str(self.repo),'--artifact-root',str(self.root),'--prompt-file',str(task),
                 '--parent-harness','codex','--spec-read','fixture','--drift-verdict','within-spec']
        result=subprocess.run(command,cwd=self.repo,env=env,capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stderr)
        receipt=json.loads(result.stdout.strip().splitlines()[-1])
        destination=Path(receipt['worktree']['cwd'])
        self.assertNotEqual(destination,self.repo)
        evidence=Path(receipt['prior_turn_evidence'])
        self.assertEqual(Path(receipt['prior_worktree']),self.repo)
        self.assertIn('Dirty edits remain',receipt['prior_turn_note'])
        self.assertEqual((destination/'greet.py').read_text(),'print("Helo")\n')
        self.assertIn(str(self.repo),evidence.read_text())
        self.assertIn(str(destination),evidence.read_text())
        route_file=receipt['route_file']
        args=SimpleNamespace(route_id=receipt['route_id'],route_file=route_file,worker_type='frame')
        for harness in ('claude','codex','opencode'):
            prompt=worker_bootstrap.contract_read_prompt(args,harness)
            self.assertIn(str(evidence),prompt)
            self.assertIn('Dirty edits can remain only in the original',prompt)
        # An inline consumer follows that input and reapplies the task-scoped diff;
        # no model/worker is started and no original dirty data is moved or reset.
        patch=git(self.repo,'diff','--binary','--','greet.py')
        applied=subprocess.run(['git','-C',str(destination),'apply','-'],input=patch,text=True,capture_output=True)
        self.assertEqual(applied.returncode,0,applied.stderr)
        self.assertEqual((destination/'greet.py').read_text(),'print("Hello")\n')
        self.assertEqual((destination/'README.md').read_text(),'notes\n')
        self.assertEqual((self.repo/'README.md').read_text(),'user-only dirty bytes\n')
        self.assertEqual(git(self.repo,'diff'),original_diff)
        report=self.tmp/'promotion-result.md';report.write_text('Task-scoped diff reapplied; original user bytes unchanged.\n')
        summary=self.tmp/'promotion-summary.txt';summary.write_text('Promotion fixture verified.\n')
        closed=subprocess.run([sys.executable,str(ROOT/'utilities/capability-route.py'),'finish','--route',route_file,
            '--evidence',str(report),'--summary-file',str(summary)],cwd=destination,env=env,capture_output=True,text=True,timeout=90)
        self.assertEqual(closed.returncode,0,closed.stdout+closed.stderr)


class HeldHistoryAndSnapshotLockTest(Fixture):
    def test_new_start_pending_keeps_the_previous_end_and_separates_both_turns(self):
        captured = []
        def record(*args, **kwargs):
            captured.extend(args[4])
            return self.recorder(*args, **kwargs)
        def append(*args, **kwargs):
            captured.extend(args[5])
            return self.appender(*args, **kwargs)
        def step(phase, when, **kwargs):
            return getattr(T, 'turn_' + phase)('claude', SID, str(self.repo), self.env, now=when,
                recorder=record, appender=append, **kwargs)
        step('start', 100)
        (self.repo/'greet.py').write_text('first turn A edit\n')
        entered, release, ended = threading.Event(), threading.Event(), threading.Event()
        replies, errors = {}, []
        original = T.snapshot
        def take(*args):
            value = original(*args)  # real Git/metadata snapshot, then delay publication
            if threading.current_thread() is newer:
                entered.set()
                if not release.wait(15):
                    raise AssertionError('test did not release the new start')
            return value
        def start_new():
            try:
                replies['start'] = step('start', 120)
            except BaseException as exc:
                errors.append(exc)
        def end_old():
            try:
                replies['end'] = step('end', 110,
                    payload={'last_assistant_message':'A final reply; check failed, exit unknown.'})
            except BaseException as exc:
                errors.append(exc)
            finally:
                ended.set()
        newer = threading.Thread(target=start_new)
        older = threading.Thread(target=end_old)
        with mock.patch.object(T, 'snapshot', side_effect=take):
            newer.start()
            try:
                self.assertTrue(entered.wait(10))
                with T._locked(T._state_lock(T.state_path(self.env,'claude',SID)), 0.0) as available:
                    self.assertTrue(available)
                (self.repo/'README.md').write_text('second turn B edit only\n')
                older.start()
                self.assertTrue(ended.wait(10), 'old end must not wait on the new Git capture')
            finally:
                release.set()
                newer.join(15)
                if older.ident is not None:
                    older.join(15)
        self.assertFalse(newer.is_alive())
        self.assertFalse(older.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.calls[0]['sources'], [['greet.py']])
        self.assertEqual(captured[0]['source_count'], 1)
        self.assertIn('A final reply', captured[0]['section'])
        self.assertIsNone(self.state()['turn_ended_at'])
        self.assertEqual(self.state()['turn_started_at'], 120)
        step('end', 140, payload={'last_assistant_message':'B final reply.'})
        self.assertEqual(self.appended, [[['README.md']]])
        self.assertEqual(captured[1]['source_count'], 1)
        self.assertIn('B final reply', captured[1]['section'])
        self.assertNotIn('prior_turn', self.state())

    @contextlib.contextmanager
    def starting(self, when, *, resume=True, unreadable=False):
        entered, release = threading.Event(), threading.Event()
        replies, errors = {}, []
        snapshot, drain = T.snapshot, T._resume_ends
        def take(*args):
            value = snapshot(*args)
            if threading.current_thread() is worker:
                entered.set()
                if not release.wait(15):
                    raise AssertionError('test did not release capture')
                if unreadable:
                    return None
            return value
        def resuming(*args, **kwargs):
            return [] if not resume and threading.current_thread() is worker else drain(*args, **kwargs)
        def start():
            try:
                replies.update(T.turn_start('claude', SID, str(self.repo), self.env, now=when,
                    recorder=self.recorder, appender=self.appender))
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=start)
        with mock.patch.object(T,'snapshot',side_effect=take), mock.patch.object(T,'_resume_ends',side_effect=resuming):
            worker.start()
            try:
                self.assertTrue(entered.wait(10))
                yield replies
            finally:
                release.set()
                worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_own_end_during_start_capture_is_saved_with_its_reply_and_interruption(self):
        with self.starting(100):
            (self.repo/'greet.py').write_text('this turn\n')
            ended = T.turn_end('claude',SID,str(self.repo),self.env,now=110,
                recorder=self.recorder,appender=self.appender,
                payload={'interrupted':True,'last_assistant_message':'A interrupted final reply.'})
            self.assertEqual(ended['state'],'start-pending')
            self.assertEqual(self.calls,[])
        self.assertEqual(self.calls[0]['sources'],[['greet.py']])
        self.assertEqual(self.state()['turn_ended_at'],110)
        summary = self.state()['records'][str(self.root)]['last_turn']['summary']
        self.assertIn('A interrupted final reply',summary)
        self.assertIn('native boundary: interrupted',summary)
        self.assertNotIn('deferred_end',self.state())

    def test_next_end_drains_a_prior_end_left_after_start_publication(self):
        T.turn_start('claude',SID,str(self.repo),self.env,now=100)
        (self.repo/'greet.py').write_text('A edit\n')
        with self.starting(120,resume=False):
            (self.repo/'README.md').write_text('B edit\n')
            T.turn_end('claude',SID,str(self.repo),self.env,now=110,
                recorder=self.recorder,appender=self.appender,
                payload={'last_assistant_message':'A reply token=unsafe-token-value '+('x'*20000)})
        self.assertEqual(self.calls,[])
        event=self.state()['prior_turn']['deferred_end']
        self.assertLessEqual(len(event['payload']['last_assistant_message'].encode()),8192)
        self.assertNotIn('unsafe-token-value',json.dumps(event))
        T.turn_end('claude',SID,str(self.repo),self.env,now=140,
            recorder=self.recorder,appender=self.appender)
        self.assertEqual(self.calls[0]['sources'],[['greet.py']])
        self.assertEqual(self.appended,[[['README.md']]])
        self.assertNotIn('prior_turn',self.state())

    def test_prior_pending_start_can_publish_after_a_newer_boundary_without_replacing_it(self):
        with self.starting(100) as reply:
            (self.repo/'greet.py').write_text('A edit\n')
            T.turn_start('claude',SID,str(self.repo),self.env,now=120,
                recorder=self.recorder,appender=self.appender)
            self.assertEqual(T.turn_end('claude',SID,str(self.repo),self.env,now=110,
                recorder=self.recorder,appender=self.appender)['state'],'start-pending')
            (self.repo/'README.md').write_text('B edit\n')
        self.assertEqual(reply['state'],'late-start')
        self.assertEqual(self.calls[0]['sources'],[['greet.py']])
        self.assertEqual(self.state()['turn_started_at'],120)
        T.turn_end('claude',SID,str(self.repo),self.env,now=140,
            recorder=self.recorder,appender=self.appender)
        self.assertEqual(self.appended,[[['README.md']]])

    def test_failed_pending_capture_keeps_the_unverifiable_boundary_unknown(self):
        T.turn_start('claude',SID,str(self.repo),self.env,now=100)
        (self.repo/'greet.py').write_text('A edit\n')
        with self.starting(120,unreadable=True) as reply:
            (self.repo/'README.md').write_text('B edit\n')
            T.turn_end('claude',SID,str(self.repo),self.env,now=110,
                recorder=self.recorder,appender=self.appender)
        self.assertEqual(reply['state'],'unreadable')
        self.assertEqual(self.calls,[])
        self.assertIn('deferred_end',self.state()['prior_turn'])
        self.assertEqual(T.turn_end('claude',SID,str(self.repo),self.env,now=140,
            recorder=self.recorder,appender=self.appender)['state'],'no-start-snapshot')
        self.assertEqual(self.calls,[])

    def test_held_history_evicts_oldest_with_marker_and_preserves_latest_reply(self):
        self.ledger_line('codex', SID, 'rt-still-open')
        routes = self.root / '.runtime/routes'
        routes.mkdir(parents=True)
        route = routes / 'rt-still-open.json'
        original = json.dumps({'route_id':'rt-still-open', 'artifact_root':str(self.root)})
        route.write_text(original)
        target = self.root / '.runtime/auto-record' / f'codex-{SID}.held.md'
        for index in range(8):
            T.turn_start('codex', SID, str(self.repo), self.env, now=100 + index*10)
            (self.repo/'greet.py').write_text(f'print({index})\n')
            observation = (f'Check {index}: exit 7 (failed).\n' + '한글 관측\n'*3500 +
                           f'Final reply {index}: verification failed; still recorded.\n')
            with mock.patch.object(T, 'observation_section', return_value=observation):
                result = T.turn_end('codex', SID, str(self.repo), self.env, now=105 + index*10)
            self.assertEqual(result['state'], 'route-observed')
            self.assertLessEqual(target.stat().st_size, 256*1024)
        history = target.read_text()
        self.assertIn('history truncated', history)
        self.assertNotIn('Final reply 0:', history)
        self.assertNotIn('Final reply 1:', history)
        self.assertIn('Final reply 6:', history)
        self.assertIn('Check 7: exit 7 (failed).', history)
        self.assertIn('Final reply 7:', history)
        self.assertEqual(route.read_text(), original)
        self.assertFalse(route.with_suffix('.outcome.json').exists())
        rows = T.supplementary_records('codex', SID, str(self.repo), self.env)
        self.assertTrue(rows[-1]['record_saved'])
        self.assertEqual(rows[-1]['reference'], str(target))

    def test_legacy_oversized_history_is_capped_on_the_next_append(self):
        self.ledger_line('codex', SID, 'rt-legacy-open')
        target = self.root / '.runtime/auto-record' / f'codex-{SID}.held.md'
        target.parent.mkdir(parents=True)
        target.write_text('OLD-FIRST\n' + '한글 old history\n'*100000 + 'RECENT-LEGACY\n')
        T.turn_start('codex', SID, str(self.repo), self.env)
        (self.repo/'greet.py').write_text('print("fixed")\n')
        result = T.turn_end('codex', SID, str(self.repo), self.env,
            payload={'last_assistant_message':'Latest reply survives the legacy history trim.'})
        self.assertEqual(result['state'], 'route-observed')
        self.assertLessEqual(target.stat().st_size, 256*1024)
        history = target.read_text()  # valid UTF-8 after trimming inside multibyte text
        self.assertIn('history truncated', history)
        self.assertNotIn('OLD-FIRST', history)
        self.assertIn('RECENT-LEGACY', history)
        self.assertIn('Latest reply survives', history)

    def test_held_write_failure_keeps_history_and_reports_unknown_without_taking_over(self):
        self.ledger_line('codex', SID, 'rt-write-failure')
        target = self.root / '.runtime/auto-record' / f'codex-{SID}.held.md'
        target.parent.mkdir(parents=True)
        target.write_text('Existing history must survive a failed replacement.\n')
        T.turn_start('codex', SID, str(self.repo), self.env)
        (self.repo/'greet.py').write_text('a source change\n')
        replace = T.os.replace
        def fail_history(source, destination):
            if Path(destination) == target:
                raise PermissionError('fixture')
            return replace(source, destination)
        with mock.patch.object(T.os, 'replace', side_effect=fail_history):
            result = T.turn_end('codex', SID, str(self.repo), self.env)
        self.assertEqual(result['state'], 'route-observed')
        self.assertEqual(target.read_text(), 'Existing history must survive a failed replacement.\n')
        self.assertFalse(self.state(harness='codex')['held_turn']['record_saved'])
        self.assertEqual(self.state(harness='codex')['held_turn']['error'], 'PermissionError')
        self.assertEqual(list(target.parent.glob('.held-*')), [])

    def test_unreadable_off_lock_snapshot_is_not_an_unchanged_or_successful_turn(self):
        self.start()
        (self.repo/'greet.py').write_text('an unreadable change\n')
        with mock.patch.object(T, 'snapshot', return_value=None):
            self.assertEqual(self.end()['state'], 'unreadable')
            self.assertEqual(self.start()['state'], 'unreadable')
        self.assertEqual(self.end()['state'], 'no-start-snapshot')
        self.assertEqual(self.calls, [])

    def test_25000_paths_keep_counts_and_leave_state_lock_available_during_both_snapshots(self):
        status = b''.join(f'?? source-{i:05d}.py\0'.encode() for i in range(25000))
        original_git, original_snapshot = T._git, T.snapshot
        lock = T._state_lock(T.state_path(self.env, 'claude', SID))
        checked = []
        def take(*args):
            with T._locked(lock, 0.0) as available:
                self.assertTrue(available, 'snapshot must not hold the state lock')
                checked.append(available)
            return original_snapshot(*args)
        def probe(top, args, env, **kwargs):
            if args[0] == 'status':
                return subprocess.CompletedProcess(args, 0, status, b'')
            return original_git(top, args, env, **kwargs)
        with mock.patch.object(T, 'snapshot', side_effect=take):
            self.start()
            with mock.patch.object(T, '_git', side_effect=probe):
                self.assertEqual(self.end()['state'], 'recorded')
        self.assertEqual(len(checked), 2)
        last = self.state()['records'][str(self.root)]['last_turn']
        self.assertEqual(last['changed_files'], 25000)
        self.assertTrue(last['changed_files_exact'])
        self.assertTrue(last['truncated'])

    def test_slow_old_start_cannot_replace_the_newer_turn(self):
        entered, release = threading.Event(), threading.Event()
        original = T.snapshot
        replies, errors = {}, []
        def take(*args):
            value = original(*args)
            if threading.current_thread() is worker:
                entered.set()
                if not release.wait(10):
                    raise AssertionError('test did not release snapshot')
            return value
        def old_start():
            try:
                replies.update(T.turn_start('claude', SID, str(self.repo), self.env, now=100))
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=old_start)
        with mock.patch.object(T, 'snapshot', side_effect=take):
            worker.start()
            try:
                self.assertTrue(entered.wait(10))
                with T._locked(T._state_lock(T.state_path(self.env,'claude',SID)), 0.0) as available:
                    self.assertTrue(available)
                (self.repo/'greet.py').write_text('edit before the newer turn\n')
                self.assertEqual(T.turn_start('claude',SID,str(self.repo),self.env,now=120)['state'],'baseline')
                (self.repo/'README.md').write_text('the newer turn only\n')
            finally:
                release.set()
                worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(replies['state'], 'late-start')
        self.assertEqual(self.state()['turn_started_at'], 120)
        T.turn_end('claude', SID, str(self.repo), self.env, now=130,
                   recorder=self.recorder, appender=self.appender)
        self.assertEqual(self.calls[0]['sources'], [['README.md']])

    def test_slow_end_uses_the_late_end_boundary_without_closing_the_new_turn(self):
        self._overlapping_end(110, 'recorded')

    def test_end_capture_superseded_by_an_earlier_timestamped_start_stays_unknown(self):
        self._overlapping_end(130, 'superseded-end')

    def _overlapping_end(self, end_time, expected):
        T.turn_start('claude', SID, str(self.repo), self.env, now=100)
        (self.repo/'greet.py').write_text('first turn\n')
        original = T.snapshot
        entered, release = threading.Event(), threading.Event()
        replies, errors = {}, []
        def take(*args):
            value = original(*args)
            if threading.current_thread() is worker:
                entered.set()
                if not release.wait(10):
                    raise AssertionError('test did not release snapshot')
            return value
        def old_end():
            try:
                replies.update(T.turn_end('claude', SID, str(self.repo), self.env, now=end_time,
                    recorder=self.recorder, appender=self.appender))
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=old_end)
        with mock.patch.object(T, 'snapshot', side_effect=take):
            worker.start()
            try:
                self.assertTrue(entered.wait(10))
                with T._locked(T._state_lock(T.state_path(self.env,'claude',SID)), 0.0) as available:
                    self.assertTrue(available)
                self.assertEqual(T.turn_start('claude',SID,str(self.repo),self.env,now=120)['state'],'baseline')
                (self.repo/'README.md').write_text('second turn\n')
            finally:
                release.set()
                worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(replies['state'], expected)
        self.assertEqual(self.state()['turn_started_at'], 120)
        self.assertIsNone(self.state()['turn_ended_at'])
        T.turn_end('claude', SID, str(self.repo), self.env, now=140,
                   recorder=self.recorder, appender=self.appender)
        if expected == 'recorded':
            self.assertEqual(self.calls[0]['sources'], [['greet.py']])
            self.assertEqual(self.appended, [[['README.md']]])
        else:
            self.assertEqual(self.calls[0]['sources'], [['README.md']])


if __name__ == "__main__":
    unittest.main()
