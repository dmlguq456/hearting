"""One failed herdr read keeps a same-process row's identity and says why it failed.

A Claude row's session id comes from its herdr pane. When a pass could not read the pane, every
row used to lose its id, so `_detail_bindings` was empty and `retain_unobserved_cells` had nothing
to match: the whole screen showed `·`. Isolated fake herdr observations only.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import process_identity
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
        with mock.patch.object(herdr, "_PANE_CLAIM_CARRY_S", -1.0):
            expired = self.one_pass(complete=False)
        self.assertIsNone(expired.session_id)

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


if __name__ == "__main__":
    unittest.main()
