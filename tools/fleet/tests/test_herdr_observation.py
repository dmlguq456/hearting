"""Event-revision reuse for Fleet's optional Herdr process observations."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
import subprocess
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

_TOOLS_DIR = Path(__file__).resolve().parents[2]
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from fleet.collectors import herdr  # noqa: E402


@dataclass(frozen=True)
class TestView:
    endpoint: str
    socket_path: str
    generation: int
    revision: int
    panes: tuple = ()
    pane_revisions: dict = None
    terminal: dict = None
    agents: tuple = ()
    observed_at: float = 1.0
    complete: bool = True


def _module_for(socket_path="/isolated/herdr.sock", endpoint="fleet-named"):
    class Module:
        @staticmethod
        def endpoint_name(value=None):
            return value or endpoint

        @staticmethod
        def socket_path_for_endpoint(_session=None):
            return socket_path

        class ObservationTransport:
            def __init__(self, selected_endpoint, selected_socket, **_kwargs):
                self.endpoint = selected_endpoint
                self.socket_path = selected_socket
                self.close_count = 0

            def observe(self):
                raise AssertionError("observation runs through background_read")

            def close(self):
                self.close_count += 1

    return Module


class PaneProcessReuseTest(unittest.TestCase):
    def test_unchanged_refresh_reuses_process_info_until_pane_event_or_resnapshot(self):
        old_cache = herdr._PANE_INFO_CACHE.copy()
        herdr._PANE_INFO_CACHE.clear()
        self.addCleanup(lambda: (herdr._PANE_INFO_CACHE.clear(),
                                herdr._PANE_INFO_CACHE.update(old_cache)))
        observation = SimpleNamespace(
            endpoint="fleet-named", socket_path="/isolated/herdr.sock", generation=1,
            pane_revisions={"w1:p1": 0, "w1:p2": 0})
        panes = [{"pane_id": "w1:p1", "agent": "codex"},
                 {"pane_id": "w1:p2", "agent": "opencode"}]
        calls = Counter()

        def run(argv, **_kwargs):
            pane_id = argv[-1]
            calls[pane_id] += 1
            return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {
                "process_info": {"shell_pid": 100,
                                 "foreground_processes": [{"pid": 200, "name": "codex"}]}}}))

        with mock.patch.object(herdr.subprocess, "run", side_effect=run) as runner:
            herdr.pane_evidence(panes, runner=runner, observation=observation)
            herdr.pane_evidence(panes, runner=runner, observation=observation)
            self.assertEqual(calls, {"w1:p1": 1, "w1:p2": 1})

            # One changed pane invalidates its own cached sample; its sibling
            # remains reusable during the same collector lifetime.
            observation.pane_revisions["w1:p1"] += 1
            herdr.pane_evidence(panes, runner=runner, observation=observation)
            self.assertEqual(calls, {"w1:p1": 2, "w1:p2": 1})

            # Reconnect/resnapshot invalidates both panes. Four refreshes over
            # two panes make five process-info calls (baseline without reuse: 8).
            observation.generation += 1
            herdr.pane_evidence(panes, runner=runner, observation=observation)
            self.assertEqual(calls, {"w1:p1": 3, "w1:p2": 2})
            self.assertEqual(runner.call_count, 5)
            self.assertTrue(all(call.args[0][1:4] == ["--session", "fleet-named", "pane"]
                                for call in runner.call_args_list))

    def test_named_endpoint_is_preserved_in_cli_agent_and_process_observations(self):
        with mock.patch.dict("os.environ", {
                "AGENT_HERDR_SESSION": "fleet-named", "HERDR_SOCKET_PATH": ""}, clear=False), \
             mock.patch.object(herdr.shutil, "which", return_value="/bin/herdr"), \
             mock.patch.object(herdr.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout=json.dumps({"result": {"agents": []}}))) as run:
            self.assertEqual(herdr.list_agents(runner=run, which=herdr.shutil.which), [])
            self.assertEqual(run.call_args.args[0],
                             ["herdr", "--session", "fleet-named", "agent", "list"])

        process_calls = []

        def process_info(argv, **_kwargs):
            process_calls.append(argv)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {
                "process_info": {"shell_pid": 100, "foreground_processes": []}}}))

        with mock.patch.dict("os.environ", {
                "AGENT_HERDR_SESSION": "fleet-named", "HERDR_SOCKET_PATH": ""}, clear=False), \
             mock.patch.object(herdr.subprocess, "run", side_effect=process_info) as run:
            herdr.pane_evidence([{"pane_id": "w1:p1", "agent": "codex"}], runner=run)
        self.assertEqual(process_calls, [["herdr", "--session", "fleet-named",
                                          "pane", "process-info", "--pane", "w1:p1"]])

    def test_pending_socket_observation_never_blocks_or_spawns_duplicate_readers(self):
        old = {name: dict(getattr(herdr, name)) for name in (
            "_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS",
            "_OBSERVATION_ERRORS", "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING")}
        for name in old:
            getattr(herdr, name).clear()
        self.addCleanup(lambda: (herdr.close_observations(),
                                [getattr(herdr, name).update(rows) for name, rows in old.items()]))
        view = TestView("fleet-named", "/isolated/herdr.sock", 1, 1)
        future = Future()
        with mock.patch.object(herdr, "_observation_module", return_value=_module_for()), \
             mock.patch.object(herdr, "background_read", return_value=future) as start_read:
            self.assertIsNone(herdr.observation_view())
            self.assertIsNone(herdr.observation_view())
            start_read.assert_called_once()
            future.set_result(view)
            self.assertIs(herdr.observation_view(), view)
            self.assertIs(herdr.observation_view(), view)
            start_read.assert_called_once()

    def test_last_complete_view_is_shared_while_one_next_read_runs(self):
        for name in ("_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS",
                     "_OBSERVATION_ERRORS", "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING"):
            getattr(herdr, name).clear()
        self.addCleanup(herdr.close_observations)
        first, second = Future(), Future()
        with mock.patch.object(herdr, "_observation_module", return_value=_module_for()), \
             mock.patch.object(herdr, "background_read", side_effect=[first, second]) as start_read:
            self.assertIsNone(herdr.observation_view())
            first_view = TestView("fleet-named", "/isolated/herdr.sock", 1, 1)
            first.set_result(first_view)
            self.assertIs(herdr.observation_view(), first_view)

            key = ("fleet-named", "/isolated/herdr.sock")
            herdr._OBSERVATION_LAST_STARTED[key] -= 1.0
            stale = herdr.observation_view()
            self.assertEqual(stale.panes, first_view.panes)
            self.assertFalse(stale.complete)
            self.assertEqual(start_read.call_count, 2)
            self.assertEqual(herdr._observation_error(), "herdr refresh pending")

            same_stale = herdr.observation_view()
            self.assertFalse(same_stale.complete)
            self.assertEqual(start_read.call_count, 2)
            second_view = TestView("fleet-named", "/isolated/herdr.sock", 2, 1)
            second.set_result(second_view)
            self.assertIs(herdr.observation_view(), second_view)

    def test_failed_refresh_keeps_old_rows_but_marks_view_incomplete(self):
        for name in ("_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS",
                     "_OBSERVATION_ERRORS", "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING"):
            getattr(herdr, name).clear()
        self.addCleanup(herdr.close_observations)
        first, failed = Future(), Future()
        with mock.patch.object(herdr, "_observation_module", return_value=_module_for()), \
             mock.patch.object(herdr, "background_read", side_effect=[first, failed]):
            self.assertIsNone(herdr.observation_view())
            view = TestView("fleet-named", "/isolated/herdr.sock", 1, 1,
                            panes=({"pane_id": "w1:p1"},))
            first.set_result(view)
            self.assertIs(herdr.observation_view(), view)
            key = ("fleet-named", "/isolated/herdr.sock")
            herdr._OBSERVATION_LAST_STARTED[key] -= 1.0
            pending = herdr.observation_view()
            self.assertFalse(pending.complete)
            failed.set_exception(OSError("socket lost"))
            unhealthy = herdr.observation_view()
            self.assertEqual(unhealthy.panes, view.panes)
            self.assertFalse(unhealthy.complete)
            self.assertIn("OSError: socket lost", herdr._observation_error())

    def test_incomplete_cached_agent_sid_cannot_confirm_presence_or_absence(self):
        for name in ("_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS",
                     "_OBSERVATION_ERRORS", "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING"):
            getattr(herdr, name).clear()
        self.addCleanup(herdr.close_observations)
        first, pending, refreshed = Future(), Future(), Future()
        agent = {"agent": "codex", "agent_session": {"agent": "codex", "value": "thread-current"}}

        def attach(session_id):
            session = SimpleNamespace(harness="codex", session_id=session_id, pid=4312,
                                      proc_start=None, cwd="/project", is_child=False,
                                      app_server=False, mem_worker=False, detached=False)
            health = herdr.enrich([session])
            return session.herdr_attached, health

        with mock.patch.object(herdr, "_observation_module", return_value=_module_for()), \
             mock.patch.object(herdr, "background_read", side_effect=[first, pending, refreshed]):
            self.assertIsNone(herdr.observation_view())
            first.set_result(TestView("fleet-named", "/isolated/herdr.sock", 1, 1,
                                       agents=(agent,)))
            self.assertTrue(herdr.observation_view().complete)

            key = ("fleet-named", "/isolated/herdr.sock")
            herdr._OBSERVATION_LAST_STARTED[key] -= 1.0
            stale = herdr.observation_view()
            self.assertFalse(stale.complete)
            current, health = attach("thread-current")
            self.assertIsNone(current)
            self.assertFalse(health["complete"])
            absent, health = attach("thread-other")
            self.assertIsNone(absent)
            self.assertFalse(health["complete"])

            pending.set_exception(OSError("socket lost"))
            failed = herdr.observation_view()
            self.assertFalse(failed.complete)
            current, health = attach("thread-current")
            self.assertIsNone(current)
            self.assertFalse(health["complete"])
            absent, health = attach("thread-other")
            self.assertIsNone(absent)
            self.assertFalse(health["complete"])

            herdr._OBSERVATION_LAST_STARTED[key] -= 1.0
            self.assertFalse(herdr.observation_view().complete)
            refreshed.set_result(TestView("fleet-named", "/isolated/herdr.sock", 2, 1,
                                           agents=(agent,)))
            self.assertTrue(herdr.observation_view().complete)
            current, health = attach("thread-current")
            self.assertIs(current, True)
            self.assertTrue(health["complete"])

    def test_pending_and_failed_cached_pane_pid_cannot_confirm_current_attachment(self):
        old_pane_cache = herdr._PANE_INFO_CACHE.copy()
        herdr._PANE_INFO_CACHE.clear()
        for name in ("_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS",
                     "_OBSERVATION_ERRORS", "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING"):
            getattr(herdr, name).clear()

        def restore():
            herdr.close_observations()
            herdr._PANE_INFO_CACHE.clear()
            herdr._PANE_INFO_CACHE.update(old_pane_cache)

        self.addCleanup(restore)
        first, pending = Future(), Future()
        pane = {"pane_id": "w1:p1", "agent": "codex", "cwd": "/project",
                "agent_session": {"agent": "codex", "value": "thread-current"}}
        agent = {"agent": "codex", "pane_id": "w1:p1",
                 "agent_session": {"agent": "codex", "value": "thread-current"}}
        view = TestView("fleet-named", "/isolated/herdr.sock", 1, 1,
                        panes=(pane,), pane_revisions={"w1:p1": 0}, agents=(agent,))
        cache_key = (view.endpoint, view.socket_path, view.generation, 0, "w1:p1")
        herdr._PANE_INFO_CACHE[cache_key] = {
            "shell_pid": 4000,
            "foreground_processes": [{"pid": 4312, "name": "codex"}],
        }

        def attach_from_cached_view():
            session = SimpleNamespace(harness="codex", session_id="thread-other", pid=4312,
                                      proc_start="pid-start-before-reuse", cwd="/project",
                                      is_child=False, app_server=False, mem_worker=False,
                                      detached=False)
            health = herdr.enrich([session])
            return session, health

        observation_reads = iter((first, pending))

        def background_read(read):
            try:
                return next(observation_reads)
            except StopIteration:
                result = Future()
                try:
                    result.set_result(read())
                except Exception as exc:
                    result.set_exception(exc)
                return result

        with mock.patch.object(herdr, "_observation_module", return_value=_module_for()), \
                mock.patch.object(herdr, "background_read", side_effect=background_read):
            self.assertIsNone(herdr.observation_view())
            first.set_result(view)
            self.assertTrue(herdr.observation_view().complete)

            key = (view.endpoint, view.socket_path)
            herdr._OBSERVATION_LAST_STARTED[key] -= 1.0
            stale = herdr.observation_view()
            self.assertFalse(stale.complete)
            stale_pids = herdr.pane_evidence(stale.panes, observation=stale)
            self.assertIn(4312, stale_pids[1])
            self.assertFalse(stale_pids.complete)
            with mock.patch("fleet.collectors.procscan.read_proc_start",
                            return_value="pid-start-after-reuse"):
                session, health = attach_from_cached_view()
            self.assertIsNone(session.herdr_attached)
            self.assertFalse(health["complete"])

            pending.set_exception(OSError("socket lost"))
            failed = herdr.observation_view()
            self.assertFalse(failed.complete)
            failed_pids = herdr.pane_evidence(failed.panes, observation=failed)
            self.assertIn(4312, failed_pids[1])
            self.assertFalse(failed_pids.complete)
            with mock.patch("fleet.collectors.procscan.read_proc_start",
                            return_value="pid-start-after-reuse"):
                session, health = attach_from_cached_view()
            self.assertIsNone(session.herdr_attached)
            self.assertFalse(health["complete"])

    def test_refresh_discards_old_generation_and_close_clears_cached_views(self):
        for name in ("_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS",
                     "_OBSERVATION_ERRORS", "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING"):
            getattr(herdr, name).clear()
        self.addCleanup(herdr.close_observations)
        first, refreshed = Future(), Future()
        with mock.patch.object(herdr, "_observation_module", return_value=_module_for()), \
             mock.patch.object(herdr, "background_read", side_effect=[first, refreshed]) as start_read:
            self.assertIsNone(herdr.observation_view())
            old_view = TestView("fleet-named", "/isolated/herdr.sock", 1, 1)
            first.set_result(old_view)
            self.assertIs(herdr.observation_view(), old_view)
            self.assertIsNone(herdr.observation_view(refresh=True))
            self.assertEqual(start_read.call_count, 2)
            self.assertIsNone(herdr.observation_view())
            new_view = TestView("fleet-named", "/isolated/herdr.sock", 2, 1)
            refreshed.set_result(new_view)
            self.assertIs(herdr.observation_view(), new_view)
            herdr.close_observations()
            self.assertFalse(herdr._OBSERVATION_VIEWS)
            self.assertFalse(herdr._OBSERVATION_FUTURES)

    def test_refresh_requested_during_read_reconnects_after_that_read_finishes(self):
        for name in ("_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS",
                     "_OBSERVATION_ERRORS", "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING"):
            getattr(herdr, name).clear()
        self.addCleanup(herdr.close_observations)
        first, forced = Future(), Future()
        with mock.patch.object(herdr, "_observation_module", return_value=_module_for()), \
             mock.patch.object(herdr, "background_read", side_effect=[first, forced]) as start_read:
            self.assertIsNone(herdr.observation_view())
            self.assertIsNone(herdr.observation_view(refresh=True))
            old_view = TestView("fleet-named", "/isolated/herdr.sock", 1, 1)
            first.set_result(old_view)
            # A later ordinary consumer completes the pending refresh request;
            # it must not publish the just-finished pre-refresh generation.
            self.assertIsNone(herdr.observation_view())
            self.assertFalse(herdr._OBSERVATION_VIEWS)
            self.assertEqual(start_read.call_count, 2)
            self.assertIsNone(herdr.observation_view())
            new_view = TestView("fleet-named", "/isolated/herdr.sock", 2, 1)
            forced.set_result(new_view)
            self.assertIs(herdr.observation_view(), new_view)

    def test_same_endpoint_on_different_socket_paths_keeps_separate_views(self):
        for name in ("_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS",
                     "_OBSERVATION_ERRORS", "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING"):
            getattr(herdr, name).clear()
        self.addCleanup(herdr.close_observations)
        first, second = Future(), Future()
        module_a, module_b = _module_for("/sockets/a.sock"), _module_for("/sockets/b.sock")
        with mock.patch.object(herdr, "_observation_module", side_effect=[
                module_a, module_a, module_b, module_b]), \
             mock.patch.object(herdr, "background_read", side_effect=[first, second]) as start_read:
            with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/sockets/a.sock"}):
                self.assertIsNone(herdr.observation_view())
                view_a = TestView("fleet-named", "/sockets/a.sock", 1, 1)
                first.set_result(view_a)
                self.assertIs(herdr.observation_view(), view_a)
            with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/sockets/b.sock"}):
                self.assertIsNone(herdr.observation_view())
                view_b = TestView("fleet-named", "/sockets/b.sock", 1, 1)
                second.set_result(view_b)
                self.assertIs(herdr.observation_view(), view_b)
        self.assertEqual(start_read.call_count, 2)
        self.assertEqual(set(herdr._OBSERVATION_VIEWS.values()), {view_a, view_b})


_OBSERVATION_CACHE_NAMES = (
    "_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS",
    "_OBSERVATION_ERRORS", "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING",
    "_OBSERVATION_FIRST_WAIT_SPENT")
_CLI_AGENT = {"agent": "claude", "pane_id": "w9:p9"}


class ListAgentsFirstReadTest(unittest.TestCase):
    """d3: a fresh process's first ``list_agents()`` on a selected socket waits once."""

    KEY = ("fleet-named", "/isolated/herdr.sock")

    def setUp(self):
        old = {name: getattr(herdr, name).copy() for name in _OBSERVATION_CACHE_NAMES}
        for name in _OBSERVATION_CACHE_NAMES:
            getattr(herdr, name).clear()
        self.unfinished = []
        self.timers = []

        def restore():
            for timer in self.timers:
                timer.join(1.0)
            for future in self.unfinished:
                if not future.done():
                    future.set_result(None)
            herdr.close_observations()
            for name, rows in old.items():
                getattr(herdr, name).update(rows)

        self.addCleanup(restore)
        # The default runner path also requires ``shutil.which("herdr")``.
        # A temporary fake keeps that hermetic; if the CLI path ever runs, it
        # answers with an agent row the observation path never produces.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fake = Path(temporary.name) / "herdr"
        fake.write_text("#!/bin/sh\nprintf '%s' '" + json.dumps(
            {"result": {"agents": [_CLI_AGENT]}}) + "'\n", encoding="utf-8")
        fake.chmod(0o755)
        path = mock.patch.dict(os.environ, {
            "PATH": temporary.name + os.pathsep + os.environ.get("PATH", "")})
        path.start()
        self.addCleanup(path.stop)
        # Patching subprocess.run would disable the observation path because
        # the default runner is bound at definition time. Popen sees every
        # subprocess.run call without changing that identity.
        self.cli_calls = []
        real_popen = subprocess.Popen

        def popen_spy(argv, *args, **kwargs):
            self.cli_calls.append(list(argv) if isinstance(argv, (list, tuple)) else argv)
            return real_popen(argv, *args, **kwargs)

        popen = mock.patch.object(subprocess, "Popen", popen_spy)
        popen.start()
        self.addCleanup(popen.stop)

    def _read_answering(self, outcome, delay=0.02):
        """``background_read`` stand-in whose Future a timer thread fills."""
        future = Future()
        self.unfinished.append(future)
        if outcome is not None:
            timer = threading.Timer(delay, future.set_result, args=(outcome,))
            timer.daemon = True
            self.timers.append(timer)
            timer.start()
        return future

    def _timed_list_agents(self):
        started = time.monotonic()
        result = herdr.list_agents()
        return result, time.monotonic() - started

    def test_d3a_first_call_waits_for_the_initial_snapshot_without_cli(self):
        agent = {"agent": "claude", "pane_id": "w1:p1",
                 "agent_session": {"agent": "claude", "value": "sid-1"}}
        view = TestView(*self.KEY, 1, 1, agents=(agent,))
        with mock.patch.object(herdr, "_observation_module", return_value=_module_for()), \
             mock.patch.object(herdr, "background_read",
                               side_effect=lambda _read: self._read_answering(view)) as start_read:
            agents, elapsed = self._timed_list_agents()
        self.assertEqual(agents, [agent])
        self.assertLess(elapsed, herdr._OBSERVATION_WAIT_MAX_S + 0.5)
        start_read.assert_called_once()
        self.assertEqual(self.cli_calls, [])
        self.assertIn(self.KEY, herdr._OBSERVATION_FIRST_WAIT_SPENT)
        herdr.close_observations()
        self.assertFalse(herdr._OBSERVATION_FIRST_WAIT_SPENT)

    def test_d3b_unanswered_or_incomplete_first_wait_is_bounded_unknown_without_cli(self):
        incomplete = TestView(*self.KEY, 1, 1, agents=(_CLI_AGENT,), complete=False)
        for label, outcome in (("never-done", None), ("incomplete", incomplete)):
            with self.subTest(label):
                for name in _OBSERVATION_CACHE_NAMES:
                    getattr(herdr, name).clear()
                self.cli_calls.clear()
                with mock.patch.object(herdr, "_observation_module",
                                       return_value=_module_for()), \
                     mock.patch.object(herdr, "background_read",
                                       side_effect=lambda _read: self._read_answering(outcome)):
                    agents, elapsed = self._timed_list_agents()
                self.assertIsNone(agents)
                self.assertLess(elapsed, herdr._OBSERVATION_WAIT_MAX_S + 0.5)
                self.assertEqual(self.cli_calls, [])
                self.assertIn(self.KEY, herdr._OBSERVATION_FIRST_WAIT_SPENT)

    def test_d3c_second_call_after_unanswered_first_wait_does_not_wait(self):
        reads = []

        def start(_read):
            reads.append(self._read_answering(None))
            return reads[-1]

        observe = herdr.observation_view
        with mock.patch.object(herdr, "_observation_module", return_value=_module_for()), \
             mock.patch.object(herdr, "background_read", side_effect=start), \
             mock.patch.object(herdr, "observation_view", wraps=observe) as view_spy:
            first, first_elapsed = self._timed_list_agents()
            self.assertIsNone(first)
            self.assertGreaterEqual(first_elapsed, herdr._OBSERVATION_WAIT_MAX_S - 0.05)
            waits = [call.kwargs.get("wait_timeout", 0.0) for call in view_spy.call_args_list]
            self.assertEqual(waits, [0.0, herdr._OBSERVATION_WAIT_MAX_S])

            view_spy.reset_mock()
            second, second_elapsed = self._timed_list_agents()
            self.assertIsNone(second)
            self.assertLess(second_elapsed, 0.25)
            self.assertEqual([call.kwargs.get("wait_timeout", 0.0)
                              for call in view_spy.call_args_list], [0.0])

            # The first read ends incomplete and a later tick starts a new
            # read: that consumer still never waits again.
            reads[0].set_result(TestView(*self.KEY, 1, 1, complete=False))
            self.assertIsNone(herdr.list_agents())
            herdr._OBSERVATION_LAST_STARTED[self.KEY] -= 1.0
            view_spy.reset_mock()
            third, third_elapsed = self._timed_list_agents()
            self.assertIsNone(third)
            self.assertEqual(len(reads), 2)
            self.assertLess(third_elapsed, 0.25)
            self.assertEqual([call.kwargs.get("wait_timeout", 0.0)
                              for call in view_spy.call_args_list], [0.0])
        self.assertEqual(self.cli_calls, [])

    def test_d3d_without_a_selected_socket_list_agents_keeps_the_cli_path(self):
        with mock.patch.object(herdr, "_observation_module",
                               return_value=_module_for(socket_path="", endpoint="default")), \
             mock.patch.object(herdr, "background_read",
                               side_effect=AssertionError("no socket read")) as start_read:
            agents, elapsed = self._timed_list_agents()
        self.assertEqual(agents, [_CLI_AGENT])
        self.assertEqual(self.cli_calls, [["herdr", "agent", "list"]])
        start_read.assert_not_called()
        self.assertFalse(herdr._OBSERVATION_FIRST_WAIT_SPENT)
        self.assertLess(elapsed, herdr._TIMEOUT_S)


if __name__ == "__main__":
    unittest.main()
