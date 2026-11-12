"""A reopened session takes over its own open routes; synthetic records only.

No real route is started and no process outlives a test: `subprocess.Popen` and
`subprocess.run` are replaced, and the ledger and state live in an isolated root.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools"), str(ROOT / "tools/memory/tests")]
from tidy_isolation import isolated_env
from fleet import route_chain
import session_resume
import session_route_resume as srr
import session_tidy as st
import capability_route_close

SID = "11111111-aaaa-bbbb-cccc-000000000001"
OTHER = "22222222-aaaa-bbbb-cccc-000000000002"


class _Base(unittest.TestCase):
    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        self.env = self.iso.patched_environ()
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        os.environ["FLEET_ROUTE_CHAIN_DIR"] = str(self.iso.root / "chain")
        os.environ["FLEET_SESSION_REGISTRY_DIR"] = str(self.iso.root / "registry")
        self.addCleanup(os.environ.pop, "FLEET_ROUTE_CHAIN_DIR", None)
        self.addCleanup(os.environ.pop, "FLEET_SESSION_REGISTRY_DIR", None)
        route_chain._FULL_HISTORY_CACHE.clear()
        self.routes = self.iso.root / "routes"
        self.routes.mkdir()

    def route(self, name, *, harness="claude", sid=SID, ts=1.0, event="compose", exists=True, closed=False,
              write=True):
        path = self.routes / f"{name}.json"
        if exists:
            path.write_text(json.dumps({"route_id": "rt-" + name}))
        if closed:
            capability_route_close.outcome_path(path).write_text("{}")
        if write:
            line = route_chain.build_line({"route_id": "rt-" + name}, event=event, harness=harness,
                                          session_id=sid, route_file=path, now=ts)
            self.assertTrue(route_chain.append(harness, sid, line))
        return str(path)


class OpenRoutesTest(_Base):
    def test_open_routes_oldest_first_unique_and_open_only(self):
        newest = self.route("c", ts=30)
        oldest = self.route("a", ts=10)
        self.route("a", ts=11, event="start")          # a repeated line for one route collapses
        self.route("closed", ts=20, closed=True)       # has an outcome
        self.route("gone", ts=25, exists=False)        # file vanished
        self.route("other", sid=OTHER, ts=5)           # someone else's ledger
        self.assertEqual(srr.open_routes("claude", SID), [oldest, newest])

    def test_compose_only_route_counts(self):
        only = self.route("composed", event="compose")
        self.assertEqual(srr.open_routes("claude", SID), [only])

    def test_no_ledger_no_routes(self):
        self.assertEqual(srr.open_routes("claude", SID), [])
        self.assertEqual(srr.open_routes("codex", SID), [])


class ChildEnvTest(_Base):
    def test_child_env_anchors_exactly_this_session(self):
        base = {"PATH": "/usr/bin", "AGENT_DISPATCH_DEPTH": "2", "AGENT_DISPATCH_CHILD": "1",
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex", "AGENT_SESSION_ROLE": "worker",
                "CODEX_THREAD_ID": "stale-codex", "OPENCODE_SESSION_ID": "stale-oc",
                "CLAUDE_SESSION_ID": "stale-claude"}
        with mock.patch.dict(os.environ, base, clear=True):
            env = srr.child_env("claude", SID)
        for name in ("AGENT_DISPATCH_DEPTH", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_CURRENT_HARNESS",
                     "AGENT_SESSION_ROLE", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID", "CLAUDE_SESSION_ID"):
            self.assertNotIn(name, env)
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["AGENT_DISPATCH_CALLER_HARNESS"], "claude")
        from harness_capabilities import session_env
        self.assertEqual(env[session_env()["claude"][0]], SID)
        # The depth-0 writer anchor the real `start` computes is exactly this session.
        self.assertEqual(route_chain.writer_identity(env), ("claude", SID))

    def test_every_harness_resolves_to_its_own_identity(self):
        for harness in srr.HARNESSES:
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin", "CLAUDE_SESSION_ID": "x"}, clear=True):
                env = srr.child_env(harness, SID)
            self.assertEqual(route_chain.writer_identity(env), (harness, SID), harness)


class RequestTest(_Base):
    resume = {"session_seq": 3, "ts": 100.0}

    def request(self, harness="claude", sid=SID):
        with mock.patch.object(srr.subprocess, "Popen") as popen:
            result = srr.request(harness, sid, resume=self.resume)
        return result, popen

    def test_no_open_route_spawns_nothing(self):
        result, popen = self.request()
        self.assertFalse(result)
        popen.assert_not_called()

    def test_spawns_once_per_resume_generation(self):
        self.route("a")
        result, popen = self.request()
        self.assertTrue(result)
        self.assertEqual(popen.call_count, 1)
        command, kwargs = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(command[1:], [str(Path(srr.__file__).resolve()), "run", "--harness", "claude", "--sid", SID])
        self.assertTrue(kwargs["start_new_session"] and kwargs["close_fds"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(route_chain.writer_identity(kwargs["env"]), ("claude", SID))
        # The same resume generation (hook retry, steward pass) does not spawn again.
        again, popen2 = self.request()
        self.assertFalse(again)
        popen2.assert_not_called()
        # A later resume of the same session is a new generation.
        self.resume = {"session_seq": 4, "ts": 200.0}
        later, popen3 = self.request()
        self.assertTrue(later)
        self.assertEqual(popen3.call_count, 1)

    def test_ledger_mark_is_ignored_by_resume_lookup(self):
        self.route("a")
        st_seat = session_resume._seat("claude", SID)
        st._append_ledger_entry(st_seat, {"event": "start", "harness": "claude", "sid": SID, "source": "resume",
                                          "ts": 100.0, "session_seq": 3, "prompt_seq": 0})
        before = session_resume._resume("claude", SID)
        self.assertEqual(before["session_seq"], 3)
        self.request()
        after = session_resume._resume("claude", SID)
        self.assertEqual(after["session_seq"], 3)

    def test_failures_are_swallowed(self):
        self.route("a")
        with mock.patch.object(srr.subprocess, "Popen", side_effect=OSError("no exec")):
            self.assertFalse(srr.request("claude", SID, resume=self.resume))
        with mock.patch.object(srr, "open_routes", side_effect=RuntimeError("boom")):
            self.assertFalse(srr.request("claude", SID, resume=self.resume))
        self.assertFalse(srr.request("nope", SID, resume=self.resume))
        self.assertFalse(srr.request("claude", "", resume=self.resume))


class RunTest(_Base):
    def completed(self, code=0, err=""):
        return subprocess.CompletedProcess([], code, stdout="", stderr=err)

    def test_starts_each_open_route_oldest_first_and_records_result(self):
        first = self.route("a", ts=1)
        second = self.route("b", ts=2)
        self.route("done", ts=3, closed=True)
        with mock.patch.object(srr.subprocess, "run", return_value=self.completed()) as run:
            results = srr.run("claude", SID)
        commands = [c.args[0] for c in run.call_args_list]
        self.assertEqual([c[-1] for c in commands], [first, second])
        for command in commands:
            self.assertEqual(command[2:4], ["start", "--route"])
            self.assertTrue(command[1].endswith("capability-route.py"))
        self.assertEqual([r["state"] for r in results], ["started", "started"])
        path = srr._result_path("claude", SID)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        record = json.loads(path.read_text())
        self.assertEqual([r["route_id"] for r in record["routes"]], ["rt-a", "rt-b"])

    def test_a_refused_route_does_not_stop_the_next(self):
        self.route("a", ts=1)
        self.route("b", ts=2)
        answers = [self.completed(2, "work-parent-recovery-required"), self.completed(0)]
        with mock.patch.object(srr.subprocess, "run", side_effect=answers) as run:
            results = srr.run("claude", SID)
        self.assertEqual(run.call_count, 2)
        self.assertEqual([r["state"] for r in results], ["refused", "started"])
        self.assertIn("work-parent-recovery-required", results[0]["reason"])

    def test_timeout_and_exec_failure_are_recorded_and_the_walk_continues(self):
        self.route("a", ts=1)
        self.route("b", ts=2)
        answers = [subprocess.TimeoutExpired("x", srr.START_TIMEOUT), OSError("no exec")]
        with mock.patch.object(srr.subprocess, "run", side_effect=answers):
            results = srr.run("claude", SID)
        self.assertEqual([r["state"] for r in results], ["timeout", "error"])

    def test_no_open_route_does_nothing(self):
        with mock.patch.object(srr.subprocess, "run") as run:
            self.assertEqual(srr.run("claude", SID), [])
        run.assert_not_called()
        self.assertFalse(srr._result_path("claude", SID).exists())

    def test_main_never_raises(self):
        with mock.patch.object(srr, "run", side_effect=RuntimeError("boom")):
            self.assertEqual(srr.main(["run", "--harness", "claude", "--sid", SID]), 0)


class RegisterHookTest(_Base):
    """`register` asks for the takeover for every harness, before the notice filter."""

    def setUp(self):
        super().setUp()
        self.recover = mock.patch("peer_obligations.request_obligation_recovery", return_value=False)
        self.recover.start()
        self.addCleanup(self.recover.stop)
        self.runner = mock.patch("peer_obligations.ensure_normal_runner", return_value=False)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.work = mock.patch("session_resume.open_work", return_value=False)
        self.work.start()
        self.addCleanup(self.work.stop)

    def record_start(self, harness, source):
        seat = session_resume._seat(harness, SID)
        st._append_ledger_entry(seat, {"event": "start", "harness": harness, "sid": SID, "source": source,
                                       "ts": 100.0, "session_seq": 1, "prompt_seq": 0})

    def test_resume_requests_takeover_for_each_harness_even_when_nothing_is_notified(self):
        for harness in srr.HARNESSES:
            self.record_start(harness, "resume")
            with mock.patch("session_resume.observation", return_value="completed"), \
                 mock.patch.object(srr, "request") as request:
                self.assertIsNone(session_resume.register(harness, SID, source="resume"))
            request.assert_called_once()
            self.assertEqual(request.call_args.args, (harness, SID))
            self.assertEqual(request.call_args.kwargs["resume"]["session_seq"], 1)

    def test_not_a_resume_requests_nothing(self):
        self.record_start("claude", "startup")
        with mock.patch.object(srr, "request") as request:
            self.assertIsNone(session_resume.register("claude", SID, source="startup"))
            self.assertIsNone(session_resume.register("claude", SID, source="resume"))  # no resume start recorded
        request.assert_not_called()

    def test_worker_requests_nothing(self):
        self.record_start("claude", "resume")
        with mock.patch.object(srr, "request") as request:
            session_resume.register("claude", SID, source="resume", env={"AGENT_DISPATCH_DEPTH": "1"})
        request.assert_not_called()

    def test_a_failing_request_does_not_change_the_notice_flow(self):
        self.record_start("claude", "resume")
        with mock.patch.object(srr, "request", side_effect=RuntimeError("boom")), \
             mock.patch("session_resume.observation", return_value="completed"):
            self.assertIsNone(session_resume.register("claude", SID, source="resume"))
        with mock.patch.object(srr, "request", side_effect=RuntimeError("boom")), \
             mock.patch("session_resume.observation", return_value="interrupted-turn"), \
             mock.patch("session_notice.notify", return_value=None) as notify:
            session_resume.register("claude", SID, source="resume")
        notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
