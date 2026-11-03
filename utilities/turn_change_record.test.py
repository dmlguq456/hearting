#!/usr/bin/env python3
"""The automatic change record: each turn's own source changes, one record per session and
folder, never in the way."""
import importlib.util
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
        return T.turn_start(harness, sid, str(self.repo), self.env)

    def end(self, sid=SID, harness="claude"):
        return T.turn_end(harness, sid, str(self.repo), self.env,
                          recorder=self.recorder, appender=self.appender)

    def state(self, sid=SID, harness="claude"):
        return json.loads(T.state_path(self.env, harness, sid).read_text())

    def ledger_line(self, harness="claude", sid=SID, route_id="rt-own"):
        directory = self.ledgers / harness
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / f"{sid}.jsonl", "a") as stream:
            stream.write(json.dumps({"v": 1, "event": "compose", "harness": harness, "session_id": sid,
                                     "route_id": route_id, "artifact_root": str(self.root)}) + "\n")


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

    def test_a_session_holding_its_own_route_here_runs_no_git_and_records_nothing(self):
        self.ledger_line("claude", SID, "rt-own")
        with mock.patch.object(T, "snapshot", side_effect=AssertionError("no git for a routed session")):
            self.assertEqual(self.start()["state"], "route-held")
            (self.repo / "greet.py").write_text('print("Hello")\n')
            self.assertEqual(self.end()["state"], "route-held")
        self.assertEqual(self.calls, [])

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
        self.assertEqual(self.start()["state"], "route-held")
        (self.repo / "README.md").write_text("more\n")
        self.assertEqual(self.end()["state"], "route-held")
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
        self.assertEqual(self.start()["state"], "baseline")
        self.assertEqual(self.end()["state"], "recorded")
        self.assertEqual([call["sources"] for call in self.calls], [[["greet.py"]], [["greet.py"]]])

    def test_a_record_that_keeps_failing_is_dropped_after_three_attempts(self):
        self.fail_records = 3
        self.start()
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.assertEqual(self.end()["state"], "retry")
        self.start()
        self.assertEqual(self.end()["state"], "retry")
        self.start()
        self.assertEqual(self.end()["state"], "gave-up")
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

    def test_a_folder_outside_git_or_in_the_temp_directory_is_left_alone(self):
        plain = self.tmp / "work" / "plain"
        plain.mkdir()
        self.assertEqual(T.turn_start("claude", SID, str(plain), self.env)["state"], "no-work-tree")
        scratch_repo = Path(tempfile.gettempdir()) / "clone"
        scratch_repo.mkdir()
        git(scratch_repo, "init", "-q")
        self.assertEqual(T.turn_start("claude", SID, str(scratch_repo), self.env)["state"], "no-work-tree")

    def test_a_work_tree_too_large_to_compare_is_not_walked_again(self):
        (self.repo / "a.txt").write_text("a\n")
        (self.repo / "b.txt").write_text("b\n")
        with mock.patch.object(T, "MAX_ENTRIES", 1):
            self.assertEqual(self.start()["state"], "too-large")
            with mock.patch.object(T, "snapshot", side_effect=AssertionError("walked again")):
                self.assertEqual(self.start()["state"], "too-large")


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
        self.assertEqual(argv[2:], ["end", "--harness", "claude", "--session", SID, "--cwd", "/work/greet"])
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


if __name__ == "__main__":
    unittest.main()
