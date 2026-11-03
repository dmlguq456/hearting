#!/usr/bin/env python3
"""The automatic change record: one turn's own source changes, recorded once, never in the way."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
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
            "AGENT_ARTIFACT_CHECKPOINT": "off",
        }
        self._tempdir = tempfile.tempdir
        tempfile.tempdir = str(scratch)
        self._environ = mock.patch.dict(os.environ, {"FLEET_ROUTE_CHAIN_DIR": str(self.ledgers)})
        self._environ.start()
        self.calls = []

    def tearDown(self):
        self._environ.stop()
        tempfile.tempdir = self._tempdir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def recorder(self, ok=True):
        def run(harness, sid, top, root, source, commits, before, after, began, ended, env):
            self.calls.append({"source": source, "commits": commits, "root": root, "top": str(top)})
            return {"ok": True, "route_file": "rt-x.json", "closed_by": "finish"} if ok else \
                {"ok": False, "reason": "compose-failed", "detail": "fixture"}
        return run

    def start(self, sid=SID, harness="claude"):
        return T.turn_start(harness, sid, str(self.repo), self.env)

    def end(self, sid=SID, harness="claude", ok=True):
        return T.turn_end(harness, sid, str(self.repo), self.env, recorder=self.recorder(ok))

    def state(self, sid=SID, harness="claude"):
        return json.loads(T.state_path(self.env, harness, sid).read_text())

    def ledger_line(self, harness="claude", sid=SID):
        directory = self.ledgers / harness
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{sid}.jsonl").write_text(json.dumps(
            {"v": 1, "event": "compose", "harness": harness, "session_id": sid,
             "artifact_root": str(self.root)}) + "\n")


class TurnAttributionTest(Fixture):
    def test_shell_edit_during_the_turn_is_recorded(self):
        self.assertEqual(self.start()["state"], "baseline")
        (self.repo / "greet.py").write_text('print("Hello")\n')  # what `sed -i` leaves
        (self.repo / "notes.txt").write_text("new\n")
        result = self.end()
        self.assertEqual(result["state"], "recorded")
        self.assertEqual(self.calls[0]["source"], ["greet.py", "notes.txt"])
        self.assertEqual(self.calls[0]["root"], str(self.root))
        self.assertIsNone(self.state()["entries"])

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
        self.assertEqual(self.calls[0]["source"], ["greet.py"])

    def test_a_file_already_changed_before_the_turn_counts_when_the_turn_changes_it(self):
        (self.repo / "greet.py").write_text('print("Hel")\n')
        self.start()
        time.sleep(0.01)
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.assertEqual(self.end()["state"], "recorded")
        self.assertEqual(self.calls[0]["source"], ["greet.py"])

    def test_artifact_and_scratch_paths_do_not_count(self):
        self.start()
        (self.root / "research").mkdir(parents=True)
        (self.root / "research" / "note.md").write_text("artifact\n")
        self.assertEqual(self.end()["state"], "unchanged")
        self.assertEqual(self.calls, [])

    def test_a_session_holding_a_route_here_runs_no_git_and_records_nothing(self):
        self.ledger_line("claude", SID)
        with mock.patch.object(T, "snapshot", side_effect=AssertionError("no git for a routed session")):
            self.assertEqual(self.start()["state"], "route-held")
            (self.repo / "greet.py").write_text('print("Hello")\n')
            self.assertEqual(self.end()["state"], "route-held")
        self.assertEqual(self.calls, [])

    def test_the_record_happens_once_per_session_and_folder(self):
        self.start()
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.assertEqual(self.end()["state"], "recorded")
        self.ledger_line("claude", SID)  # what the record's own compose line adds
        self.start()
        (self.repo / "README.md").write_text("more\n")
        self.assertEqual(self.end()["state"], "route-held")
        self.assertEqual(len(self.calls), 1)

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
        result = self.end()
        self.assertEqual(result["state"], "recorded")
        self.assertEqual(self.calls[0]["source"], ["greet.py"])
        self.assertEqual([subject for _commit, subject in self.calls[0]["commits"]], ["fix greeting"])

    def test_a_failed_record_keeps_its_baseline_and_stops_after_three_attempts(self):
        self.start()
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.assertEqual(self.end(ok=False)["state"], "retry")
        self.assertEqual(self.state()["retry"], 1)
        self.assertEqual(self.start()["state"], "retry-kept")
        self.assertEqual(self.end(ok=False)["state"], "retry")
        self.start()
        self.assertEqual(self.end(ok=False)["state"], "gave-up")
        self.assertEqual([call["source"] for call in self.calls], [["greet.py"]] * 3)
        self.assertTrue(self.state()["last_failure"]["gave_up"])
        self.start()
        self.assertEqual(self.end()["state"], "unchanged")

    def test_an_end_without_a_start_only_sets_the_next_baseline(self):
        (self.repo / "greet.py").write_text('print("Hello")\n')
        self.assertEqual(self.end()["state"], "baseline")
        self.assertEqual(self.calls, [])

    def test_a_folder_outside_git_or_in_the_temp_directory_is_left_alone(self):
        plain = self.tmp / "work" / "plain"
        plain.mkdir()
        self.assertEqual(T.turn_start("claude", SID, str(plain), self.env)["state"], "no-work-tree")
        scratch_repo = Path(tempfile.gettempdir()) / "clone"
        scratch_repo.mkdir()
        git(scratch_repo, "init", "-q")
        self.assertEqual(T.turn_start("claude", SID, str(scratch_repo), self.env)["state"], "no-work-tree")


class ConcurrencyTest(Fixture):
    def test_overlapping_turns_in_the_same_work_tree_are_named(self):
        self.ledger_line("codex", "other-thread")
        T.turn_start("codex", "other-thread", str(self.repo), self.env, now=100.0)
        T.turn_start("opencode", "far-away", str(self.tmp), self.env, now=100.0)
        T.turn_start("claude", "earlier", str(self.repo), self.env, now=10.0)
        T.turn_end("claude", "earlier", str(self.repo), self.env, now=20.0, recorder=self.recorder())
        self.assertEqual(T.concurrent_sessions(self.env, "claude", SID, str(self.repo), 50.0, 200.0),
                         ["codex:other-thread"])
        text = T.evidence_text("claude", SID, self.repo, ["greet.py"], [], {"entries": {}},
                               {"entries": {"greet.py": [" M", 1, 1]}, "head": ""}, 50.0, 200.0,
                               ["codex:other-thread"], self.env)
        self.assertIn("codex:other-thread (some of these changes may be theirs)", text)

    def test_a_busy_session_lock_skips_the_start_instead_of_waiting(self):
        path = T.state_path(self.env, "claude", SID)
        with T._locked(path, 0.0) as held:
            self.assertTrue(held)
            self.assertEqual(self.start()["state"], "busy")


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
                    {"HEARTING_AUTO_RECORD": "off"}, {"AGENT_DISPATCH_CHILD": "1"}):
            self.assertFalse(T.launch("start", "claude", payload, environ=env, popen=never), env)
        self.assertFalse(T.launch("start", "claude", {"session_id": "../x"}, environ={}, popen=never))
        self.assertFalse(T.launch("later", "claude", payload, environ={}, popen=never))
        self.assertFalse(T.launch("start", "claude", payload, environ={}))  # test process, real spawn


class EndToEndTest(Fixture):
    """The real compose and finish, as the detached step runs them."""

    def test_a_shell_edit_turn_leaves_one_closed_direct_route_with_evidence(self):
        env = {**self.env, "AGENT_HOME": str(ROOT)}
        T.turn_start("claude", SID, str(self.repo), env)
        subprocess.run(["sed", "-i", "s/Helo/Hello/", str(self.repo / "greet.py")], check=True)
        started = time.monotonic()
        result = T.turn_end("claude", SID, str(self.repo), env)
        elapsed = time.monotonic() - started
        self.assertEqual(result["state"], "recorded", result)
        self.assertEqual(result["closed_by"], "finish")
        route = json.loads(Path(result["route_file"]).read_text())
        self.assertEqual(route["selection"]["shape"], "direct")
        self.assertEqual(route["cwd"], str(self.repo))
        self.assertTrue(route["tracked_gate_evidence"]["artifact_guard"]["source"].startswith("auto-record:"))
        outcome = json.loads(Path(result["route_file"]).with_suffix(".outcome.json").read_text())
        self.assertTrue(outcome["terminal_gate_proven"])
        self.assertIn("greet.py", outcome["summary"])
        evidence = Path(outcome["terminal_gates"]["inline"]["evidence"]).read_text()
        self.assertIn("greet.py", evidence)
        self.assertIn("greet.py | 2 +-", evidence)
        ledger = [json.loads(line) for line in
                  (self.ledgers / "claude" / f"{SID}.jsonl").read_text().splitlines()]
        self.assertEqual([(line["event"], line["artifact_root"]) for line in ledger],
                         [("compose", str(self.root))])
        self.assertFalse(T.state_path(env, "claude", SID).with_suffix(".record").exists())
        # The folder is now routed for this session: the next turn records nothing.
        self.assertEqual(T.turn_start("claude", SID, str(self.repo), env)["state"], "route-held")
        print(f"\nturn_change_record end-to-end record seconds={elapsed:.2f}", file=sys.stderr)


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

    def test_opencode_plugin_starts_the_shared_step_on_prompt_and_idle(self):
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
await hooks["chat.message"]({ sessionID: "oc-turn", messageID: "m1" },
  { parts: [{ type: "text", text: "fix the typo" }] });
await hooks.event({ event: { type: "session.idle", properties: { sessionID: "oc-turn" } } });
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
