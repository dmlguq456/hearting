"""One failed herdr read keeps a same-process row's identity and says why it failed.

A Claude row's session id comes from its herdr pane. When a pass could not read the pane, every
row used to lose its id, so `_detail_bindings` was empty and `retain_unobserved_cells` had nothing
to match: the whole screen showed `·`. Isolated fake herdr observations only.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import socket
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import pass_scope, process_identity
from fleet.collectors import claude, herdr, procscan, _observation_health
from fleet.explorer import live
from fleet.explorer.runtime import ExplorerRuntime
from fleet.model import Session
from fleet.refresh import LiveSnapshot

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "utilities"))
import herdr_observation  # noqa: E402

PANE = "wB:p3N"


class Observed(tuple):
    """A fake foreground-process observation (what ``pane_pids`` returns)."""


def _pids(pid, *, complete):
    evidence = Observed((set(), {pid}))
    evidence.complete, evidence.errors = complete, () if complete else (PANE + ": TimeoutError: process-info deadline",)
    evidence.fresh_shells = evidence.fresh_foreground = set()
    return evidence


class FailedReadIdentityTest(unittest.TestCase):
    def setUp(self):
        herdr._PANE_CLAIM_MEMORY.clear()
        self.addCleanup(herdr._PANE_CLAIM_MEMORY.clear)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(home), "FLEET_TITLE_STATE_DIR": str(home / "titles"),
            "XDG_STATE_HOME": str(home / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.pid, self.start, self.cwd = 994243, "777", "/proj/z"
        project = home / "projects" / claude._enc_cwd(self.cwd)
        project.mkdir(parents=True)
        (home / "sessions").mkdir()
        (home / "sessions" / f"{self.pid}.json").write_text(json.dumps({
            "pid": self.pid, "sessionId": "old", "status": "idle", "updatedAt": 1000000}))
        (home / ".statusline").mkdir()
        for sid, pct in (("old", 26), ("current", 17), ("newer", 41)):
            (home / ".statusline" / (sid + ".json")).write_text(json.dumps({
                "session_id": sid, "context_window": {"used_percentage": pct}}))
            (project / (sid + ".jsonl")).write_text(json.dumps({"type": "user"}) + "\n")

    def one_pass(self, *, complete, sid="current", start=None, alive=True):
        """``herdr.enrich`` then the Claude enricher, for one row, as one collection pass."""
        start = start or self.start
        s = Session(harness="claude", pid=self.pid, proc_start=start, cwd=self.cwd)
        panes = [{"pane_id": PANE, "agent": "claude", "cwd": self.cwd,
                  "agent_session": {"agent": "claude", "value": sid}}]
        # A failed read leaves this process's pane unbound: nothing says which pane is its own.
        bindings = {self.pid: {PANE}} if complete else {}
        with mock.patch.object(procscan, "read_proc_start", return_value=start if alive else None), \
                mock.patch.object(procscan, "read_environ", return_value={}), \
                mock.patch.object(herdr, "pane_session_aliases", return_value=[]), \
                mock.patch.object(herdr, "_clear_gpu_session_aliases", return_value=[]), \
                mock.patch.object(process_identity, "pane_session_successors", return_value={"old": {sid}}):
            herdr.enrich([s], agents=[], panes=panes, pids=_pids(self.pid, complete=complete),
                         pane_bindings=bindings, lineage=lambda _pid: "herdr")
            claude.enrich(s, tick={self.pid: getattr(s, "_pane_session_claim", None)})
        return s

    def view(self, s, *, observed):
        """The explorer view model of one pass; an unobserved pass shows no title/model/context."""
        row = Session(harness="claude", pid=s.pid, proc_start=s.proc_start, cwd=s.cwd,
                      session_id=s.session_id, session_tag="a1", liveness="idle")
        if observed:
            row.title, row.model, row.effort, row.ctx_pct = "work title", "fixture-model", "high", 17
        snapshot = LiveSnapshot(sessions=[row], observations={})
        return live.build_view_model(snapshot, now=1)

    def test_failed_read_keeps_identity_so_unobserved_cells_stay(self):
        first = self.one_pass(complete=True)
        self.assertEqual(first.session_id, "current")
        failed = self.one_pass(complete=False)
        self.assertEqual(failed.session_id, "current")
        published = self.view(first, observed=True)
        candidate = self.view(failed, observed=False)
        self.assertTrue(candidate.source_records["_detail_bindings"])
        shown = live.retain_unobserved_cells(candidate, published)
        row = shown.sessions[0]
        self.assertEqual((row["title"], row["model"], row["context"]),
                         (published.sessions[0]["title"], published.sessions[0]["model"],
                          published.sessions[0]["context"]))
        self.assertTrue(row["title"] and row["model"] and row["context"] is not None)

    def test_restarted_dead_or_expired_process_keeps_nothing(self):
        self.one_pass(complete=True)
        restarted = self.one_pass(complete=False, start="778")
        self.assertIsNone(restarted.session_id)
        self.assertEqual(restarted.session_identity_evidence["verdict"], "unobserved")
        dead = self.one_pass(complete=False, alive=False)
        self.assertIsNone(getattr(dead, "_pane_session_claim", None))

    def test_identity_has_no_age_limit_and_covers_a_row_whose_pane_was_never_matched(self):
        self.one_pass(complete=True)
        # An hour on, still reads failing: the same live process keeps what a complete read confirmed.
        real = time.monotonic
        with mock.patch.object(time, "monotonic", side_effect=lambda: real() + 3600.0):
            for _ in range(3):
                self.assertEqual(self.one_pass(complete=False).session_id, "current")
        # The pane's cwd is not the process's cwd, so a failed read can neither bind the pane nor
        # name it a candidate (no ``unobserved`` claim at all): the earlier identity still holds.
        s = Session(harness="claude", pid=self.pid, proc_start=self.start, cwd=self.cwd)
        other = [{"pane_id": PANE, "agent": "claude", "cwd": "/pane/root",
                  "agent_session": {"agent": "claude", "value": "current"}}]
        with mock.patch.object(procscan, "read_proc_start", return_value=self.start):
            claims = herdr._session_claims([s], other, {}, complete=False)
        self.assertEqual(claims[self.pid]["session_id"], "current")
        # A complete read that finds no claim for the process ends it; a never-confirmed process has none.
        with mock.patch.object(procscan, "read_proc_start", return_value=self.start):
            herdr._session_claims([s], other, {}, complete=True)
            self.assertNotIn(self.pid, herdr._session_claims([s], other, {}, complete=False))

    def test_changed_session_id_is_read_not_carried(self):
        self.one_pass(complete=True)
        self.assertEqual(self.one_pass(complete=True, sid="newer").session_id, "newer")
        # The failed pass after it carries the newer id, never the first one.
        self.assertEqual(self.one_pass(complete=False, sid="newer").session_id, "newer")
        published = self.view(self.one_pass(complete=True, sid="current"), observed=True)
        candidate = self.view(self.one_pass(complete=True, sid="newer"), observed=False)
        self.assertEqual(published.sessions[0]["title"], "work title")
        # Different session id on the two sides: the earlier values are not received.
        shown = live.retain_unobserved_cells(candidate, published)
        self.assertIsNone(shown.sessions[0]["title"])


class FirstViewWaitTest(unittest.TestCase):
    def test_only_the_first_waiting_pass_waits_for_a_first_view(self):
        waits = []

        def view(session=None, *, refresh=False, wait_timeout=0.0):
            waits.append(wait_timeout)
            return None

        with mock.patch.object(herdr, "observation_view", side_effect=view), \
                mock.patch.object(herdr, "_spend_first_observation_wait", side_effect=[True, False]), \
                mock.patch.object(herdr, "_observation_module") as module:
            module.return_value.socket_path_for_endpoint.return_value = "/isolated/herdr.sock"
            herdr.enrich([], wait_timeout=0.2)   # first pass that waits: as long as list_agents does
            herdr.enrich([], wait_timeout=0.2)   # later: its own wait
            herdr.enrich([], wait_timeout=0.0)   # the fast first pass never waits
        self.assertEqual(waits, [herdr._OBSERVATION_WAIT_MAX_S, 0.2, 0.0])

    def test_a_read_must_fit_the_first_wait(self):
        # Two snapshot round trips make the first view; both must fit in the first wait.
        self.assertLessEqual(2 * herdr._OBSERVATION_OP_TIMEOUT_S, herdr._OBSERVATION_WAIT_MAX_S)


class FailureReasonTest(unittest.TestCase):
    def test_transport_names_why_it_failed(self):
        class Refusing:
            def __init__(self, *_args):
                pass

            def settimeout(self, _value):
                pass

            def connect(self, _path):
                raise TimeoutError("timed out")

            def close(self):
                pass

        transport = herdr_observation.ObservationTransport(
            "default", "/isolated/herdr.sock", timeout=0.05, connector=Refusing)
        self.assertIsNone(transport.observe())
        self.assertIn("subscribe", transport.last_failure or "")
        self.assertIn("TimeoutError", transport.last_failure)

    def test_view_error_carries_the_transport_reason(self):
        class Transport:
            last_failure = "snapshot: TimeoutError timed out"

        key = ("default", "/isolated/herdr.sock")
        with mock.patch.dict(herdr._OBSERVATION_TRANSPORTS, {key: Transport()}), \
                mock.patch.dict(herdr._OBSERVATION_ERRORS, clear=False):
            herdr._store_observation_value(key, None)
            self.assertIn("snapshot: TimeoutError", herdr._OBSERVATION_ERRORS[key])
            herdr._OBSERVATION_ERRORS.pop(key, None)

    def test_reason_reaches_the_persisted_observation_record(self):
        health = _observation_health("job", "herdr", "pane1: TimeoutError: process-info deadline")
        self.assertEqual(health["state"], "failed")
        runtime = ExplorerRuntime()
        snapshot = LiveSnapshot(sessions=[], observations={"herdr": health})
        _model, readiness = runtime.prepare(snapshot, None, vm=live.build_view_model(snapshot, now=1),
                                            generation=1, now=1.0)
        recorded = ExplorerRuntime._source_freshness(readiness)["herdr"]
        self.assertEqual(recorded["state"], "failed")
        self.assertIn("process-info deadline", recorded["reason"])


class FakeHerdrServer:
    """A herdr stand-in on a unix socket: ``events.subscribe`` and ``session.snapshot`` only."""

    def __init__(self, path, panes, latency=0.0):
        self.path, self.panes, self.latency = path, panes, latency
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(path)
        self.server.listen(16)
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.server.close()

    def _accept(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        buffer = b""
        try:
            while True:
                while b"\n" not in buffer:
                    chunk = client.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
                line, buffer = buffer.split(b"\n", 1)
                request = json.loads(line)
                time.sleep(self.latency)
                if request["method"] == "events.subscribe":
                    result = {"type": "subscribed"}
                else:
                    result = {"type": "session_snapshot", "snapshot": {
                        "protocol": 22, "panes": self.panes,
                        "agents": [dict(pane) for pane in self.panes if pane.get("agent")]}}
                client.sendall((json.dumps({"id": request["id"], "result": result}) + "\n").encode())
        except OSError:
            pass


class PassSharedViewTest(unittest.TestCase):
    """Which reader of a pass starts the herdr read must not decide whether it sees a complete view.

    A row's pane evidence used to be lost on every other pass: the reader that started the read got
    the old view marked "refresh pending" although the read ended in milliseconds.
    """

    NAMES = ("_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS", "_OBSERVATION_ERRORS",
             "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING", "_OBSERVATION_FIRST_WAIT_SPENT")

    def setUp(self):
        old = {name: getattr(herdr, name).copy() for name in self.NAMES}
        for name in self.NAMES:
            getattr(herdr, name).clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "h.sock")
        env = mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": self.path})
        env.start()
        gap = mock.patch.object(herdr, "_OBSERVATION_MIN_REFRESH_S", 0.03)
        gap.start()

        def restore():
            gap.stop()
            env.stop()
            herdr.close_observations()
            self.server.close()
            self.tmp.cleanup()
            for name, value in old.items():
                getattr(herdr, name).update(value)

        self.addCleanup(restore)
        os.environ.pop("HERDR_SESSION", None)
        os.environ.pop("AGENT_HERDR_SESSION", None)
        self.panes = [{"pane_id": "w1:p%d" % i, "agent": "claude", "cwd": "/x",
                       "agent_session": {"agent": "claude", "value": "s%d" % i}} for i in range(12)]
        self.server = FakeHerdrServer(self.path, self.panes)

    def passes(self, count, *, gap=0.06):
        """Three readers per pass, the later two ``gap`` apart (a codex pass, enrich, the job inventory)."""
        seen = []
        for _ in range(count):
            row = []
            with pass_scope.collection_pass():
                for index in range(3):
                    if index:
                        time.sleep(gap)
                    view = herdr.observation_view(wait_timeout=0.0)
                    row.append(None if view is None else view.complete)
            seen.append(row)
            time.sleep(0.1)
        return seen

    def test_every_reader_of_a_pass_sees_the_finished_read(self):
        seen = self.passes(8)
        # The very first pass has no finished read to report; afterwards no reader sees an unfinished one.
        for number, row in enumerate(seen[1:], 2):
            self.assertEqual(row, [True, True, True], "pass %d" % number)

    def test_a_pass_shares_one_answer_and_starts_one_next_read(self):
        self.passes(1)
        time.sleep(0.2)
        started = []
        real = herdr.background_read
        with mock.patch.object(herdr, "background_read",
                               side_effect=lambda read: started.append(1) or real(read)):
            with pass_scope.collection_pass():
                first = herdr.observation_view(wait_timeout=0.0)
                time.sleep(0.06)
                second = herdr.observation_view(wait_timeout=0.0)
                third = herdr.observation_view(wait_timeout=0.0)
        self.assertIs(first, second)
        self.assertIs(second, third)
        self.assertEqual(len(started), 1)

    def test_a_read_that_an_earlier_pass_started_and_has_not_finished_keeps_the_view_pending(self):
        self.passes(2)
        time.sleep(0.2)
        self.server.latency = 0.6    # the next snapshot outlasts the pass that started it
        with herdr._OBSERVATION_LOCK:
            transport = next(iter(herdr._OBSERVATION_TRANSPORTS.values()))
            transport._state.reconnect()   # an event invalidated the view: the next read takes a new snapshot
        with pass_scope.collection_pass():
            started = herdr.observation_view(wait_timeout=0.0)
        self.assertTrue(started.complete)   # this pass reports the finished read
        time.sleep(0.15)
        with pass_scope.collection_pass():
            later = herdr.observation_view(wait_timeout=0.0)
        self.assertFalse(later.complete)    # the read is slow: that is a real incomplete pass
        self.assertIn("read running", herdr._observation_cause() or "")
        self.assertEqual(herdr._observation_error(), "herdr refresh pending")

    def test_a_waiting_reader_still_gets_a_fresh_view_and_a_reader_outside_a_pass_is_unchanged(self):
        self.passes(1)
        time.sleep(0.2)
        fresh = herdr.observation_view(wait_timeout=0.2)
        self.assertTrue(fresh.complete)
        time.sleep(0.2)
        # No pass: the reader that starts the read keeps seeing it pending (the earlier meaning).
        self.assertFalse(herdr.observation_view(wait_timeout=0.0).complete)


class FailureCauseTest(unittest.TestCase):
    """The observation record says in which step a failed read failed (``cause``), not a generic text."""

    def enrich(self, *, pids, panes, observation_error=None):
        with mock.patch.object(herdr, "_observation_error", return_value=observation_error), \
                mock.patch.object(herdr, "_observation_cause", return_value=observation_error), \
                mock.patch.object(herdr, "_session_claims", return_value={}):
            return herdr.enrich([], agents=[], panes=panes, pids=pids, pane_bindings={})

    def test_pane_process_info_failure_names_pane_and_step(self):
        result = self.enrich(pids=_pids(1, complete=False), panes=[{"pane_id": PANE, "agent": "claude"}])
        self.assertFalse(result["complete"])
        self.assertIn("process-info deadline", result["cause"])
        self.assertTrue(result["cause"].startswith("pane process-info failed for 1/1 panes"), result["cause"])
        self.assertIn(PANE, result["cause"])
        self.assertIn("process-info deadline", result["last_error"])   # the screen text is unchanged

    def test_an_unexplained_incomplete_view_keeps_the_screen_text_and_records_a_specific_cause(self):
        evidence = Observed((set(), set()))
        evidence.complete, evidence.errors = False, ()
        result = self.enrich(pids=evidence, panes=[])
        self.assertEqual(result["last_error"], "pane 조회 불가")   # what the screen shows, unchanged
        self.assertNotEqual(result["cause"], result["last_error"])
        self.assertTrue(result["cause"])

    def test_cause_reaches_the_record_while_the_screen_text_stays(self):
        health = _observation_health("job", "herdr", "pane 조회 불가", "herdr refresh pending (read running 4.0s)")
        self.assertEqual(health["last_error"], "pane 조회 불가")
        self.assertEqual(health["reason"], "herdr refresh pending (read running 4.0s)")
        self.assertEqual(_observation_health("job", "herdr", "x")["reason"], "x")


if __name__ == "__main__":
    unittest.main()
