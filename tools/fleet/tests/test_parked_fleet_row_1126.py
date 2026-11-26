#!/usr/bin/env python3
"""Fleet's row, number and pane header follow a conversation sent to the background (F1, 11-26).

Claude's `/bg` leaves the window's process as a viewer of the old session id while the
conversation goes on as another session in another process. Real child processes carry the
fixed argv and their records sit in an isolated Claude home; the one judgment
(``session_registry.parked_links``) links them only when both facts agree. Then Fleet draws one
row (the held session, in the viewer's window, also answering to the old id), the held session
continues the window's number, and the window's pane header names the held session. With one
fact only, everything stays as two rows with their own numbers.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import collectors, herdr_projection, session_registry as sr, session_tags, titles  # noqa: E402
from fleet.collectors import herdr as herdr_collector  # noqa: E402
from fleet.model import Session  # noqa: E402

OLD = "92d7cdcd-3cef-4583-80e6-f9a4561ac629"
NEW = "e675a253-0561-410f-aa04-c12345cabf70"
JOB = "e675a253"
PANE = "w1:p1"
SLEEP = "import time; time.sleep(60)"


class ParkedFleetRow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pf-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = root / "claude"
        (self.config / "sessions").mkdir(parents=True)
        env = {"CLAUDE_CONFIG_DIR": str(self.config), "XDG_STATE_HOME": str(root / "state"),
               "XDG_CONFIG_HOME": str(root / "config"), "FLEET_SESSION_REGISTRY_DIR": str(root / "registry")}
        for name in ("FLEET_TITLE_STATE_DIR", "HERDR_SESSION_METADATA_FORMATTER"):
            os.environ.pop(name, None)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.agents = []
        agents = mock.patch.object(herdr_collector, "list_agents", lambda *a, **k: list(self.agents))
        agents.start()
        self.addCleanup(agents.stop)
        self.children = []
        self.addCleanup(self._reap)

    def _reap(self):
        for child in self.children:
            child.kill()
            child.wait()

    def spawn(self, *argv):
        child = subprocess.Popen([sys.executable, "-c", SLEEP, *argv], stdin=subprocess.DEVNULL)
        self.children.append(child)
        return child

    def record(self, child, **fields):
        path = self.config / "sessions" / ("%d.json" % child.pid)
        body = {"pid": child.pid, "procStart": sr._proc_start(child.pid), **fields}
        path.write_text(json.dumps(body))

    def split(self, *, parked=True, resume=OLD):
        """The window (viewer, OLD) and the held session (NEW), both live; their Fleet rows."""
        self.viewer = self.spawn()
        self.record(self.viewer, sessionId=OLD, kind="interactive", startedAt=1_700_000_000_000)
        argv = ["--session-id", NEW, "--fork-session"] + (
            ["--resume", "/h/.claude/projects/p/%s.jsonl" % resume] if resume else [])
        self.held = self.spawn(*argv)
        self.record(self.held, sessionId=NEW, kind="bg", jobId=JOB, startedAt=1_700_000_500_000)
        if parked:
            self.park()
        return self.rows()

    def park(self, job=JOB):
        self.record(self.viewer, sessionId=OLD, kind="interactive", startedAt=1_700_000_000_000,
                    **({"parkedJobId": job} if job else {}))

    def rows(self):
        window = Session(harness="claude", pid=self.viewer.pid, session_id=OLD, kind="interactive",
                         proc_start=sr._proc_start(self.viewer.pid), herdr_attached=True,
                         pane_location="w1:t1·p1", multiplexer="herdr", title="before the split")
        held = Session(harness="claude", pid=self.held.pid, session_id=NEW, kind="bg",
                       proc_start=sr._proc_start(self.held.pid), herdr_attached=False,
                       title="the conversation goes on")
        return [window, held]

    # --- one row ---------------------------------------------------------------------------
    def test_linked_window_is_one_row_of_the_held_session_in_the_window(self):
        sessions = self.split()
        collectors.apply_session_aliases(sessions)
        self.assertEqual([s.session_id for s in sessions], [NEW])
        row = sessions[0]
        self.assertEqual((row.pid, row.title), (self.held.pid, "the conversation goes on"))
        self.assertEqual((row.pane_location, row.multiplexer, row.herdr_attached), ("w1:t1·p1", "herdr", True))
        # Relations still addressed to the old id (requests, steward marker, messages) join this row.
        self.assertIn(("claude", OLD), sr.session_join_keys(row))

    def test_one_fact_only_keeps_two_rows(self):
        for name, kwargs in (("deliberate --fork-session, no job held", {"parked": False}),
                             ("job held, argv names another conversation",
                              {"resume": "11111111-2222-4333-8444-555555555555"})):
            with self.subTest(name):
                self._reap()
                self.children = []
                for path in (self.config / "sessions").glob("*.json"):
                    path.unlink()
                sessions = self.split(**kwargs)
                collectors.apply_session_aliases(sessions)
                self.assertEqual([s.session_id for s in sessions], [OLD, NEW])
                self.assertIsNone(sessions[1].pane_location)
                self.assertNotIn(("claude", OLD), sr.session_join_keys(sessions[1]))

    def test_a_second_row_on_either_side_is_doubt(self):
        sessions = self.split()
        twin = Session(harness="claude", pid=self.held.pid, session_id=NEW, kind="bg")
        sessions.append(twin)
        collectors.apply_session_aliases(sessions)
        self.assertEqual([s.session_id for s in sessions], [OLD, NEW, NEW])

    # --- one number ------------------------------------------------------------------------
    def test_held_session_continues_the_window_number_and_frees_its_own(self):
        start = sr._proc_start
        sessions = self.split(parked=False)
        window_tag = session_tags.assign_observed("claude", OLD, pid=self.viewer.pid,
                                                  proc_start=start(self.viewer.pid))
        session_tags.assign(sessions)
        held_tag = session_tags.assigned_tag("claude", NEW)
        self.assertTrue(window_tag and held_tag and window_tag != held_tag)
        # Claude writes parkedJobId into the window's record: one row, and it keeps the window's number.
        self.park()
        sessions = self.rows()
        collectors.apply_session_aliases(sessions)
        session_tags.assign(sessions)
        self.assertEqual(session_tags.assigned_tag("claude", NEW), window_tag)
        self.assertIsNone(session_tags.assigned_tag("claude", OLD))
        snapshot = json.loads(session_tags._path().read_text())["assignments"]
        self.assertNotIn(held_tag, [row["tag"] for row in snapshot])
        # The link ends (the window holds no job): the number stays with the conversation.
        self.park(job=None)
        session_tags.assign(self.rows())
        self.assertEqual(session_tags.assigned_tag("claude", NEW), window_tag)

    def test_one_fact_only_keeps_both_numbers(self):
        start = sr._proc_start
        sessions = self.split(parked=False)
        window_tag = session_tags.assign_observed("claude", OLD, pid=self.viewer.pid,
                                                  proc_start=start(self.viewer.pid))
        session_tags.assign(sessions)
        held_tag = session_tags.assigned_tag("claude", NEW)
        session_tags.assign(self.rows())
        self.assertEqual((session_tags.assigned_tag("claude", OLD), session_tags.assigned_tag("claude", NEW)),
                         (window_tag, held_tag))

    # --- the pane header -------------------------------------------------------------------
    def test_window_pane_header_names_the_held_session(self):
        start = sr._proc_start
        self.split(parked=False)
        window_tag = session_tags.assign_observed("claude", OLD, pane_id=PANE, pid=self.viewer.pid,
                                                  proc_start=start(self.viewer.pid))
        self.agents = [{"agent": "claude", "pane_id": PANE, "display_agent": "[%s] claude" % window_tag,
                        "agent_session": {"kind": "id", "value": OLD}}]
        session_tags.assign(self.rows())
        titles.write(NEW, "the conversation goes on", now=time.time())
        self.park()
        sessions = self.rows()
        collectors.apply_session_aliases(sessions)
        returned = session_tags.assign(sessions)
        # The window's pane (still reported under OLD) is among the panes whose header shows a number.
        self.assertEqual([agent["pane_id"] for agent in returned], [PANE])
        targets = list(herdr_projection._tag_metadata_targets(
            [dict(self.agents[0], display_agent="[00] claude")]))
        self.assertEqual(targets, [((PANE, "claude", OLD), NEW)])
        with mock.patch.object(herdr_projection, "is_steward", lambda h, s, **k: s == OLD):
            command = herdr_projection._metadata_command("herdr", "claude", OLD, PANE, held=NEW)
        agent = command[command.index("--display-agent") + 1]
        header = command[command.index("--title") + 1]
        self.assertEqual(agent, "[%s] ⚑ claude" % window_tag)
        self.assertIn("the conversation goes on", header)
        # The link ends (the window holds no job and goes on as its own session): the next census
        # gives it a number again and its pane header names its own session.
        self.park(job=None)
        session_tags.assign(self.rows())
        own = session_tags.assigned_tag("claude", OLD)
        self.assertTrue(own and own != window_tag)
        self.assertEqual(list(herdr_projection._tag_metadata_targets(
            [dict(self.agents[0], display_agent="[00] claude")])), [((PANE, "claude", OLD), None)])

    # --- the pane proof reads the same module ----------------------------------------------
    def test_pane_proof_withdraws_on_one_fact(self):
        self.assertTrue(sr.holds_parked_job({"parkedJobId": JOB}))
        for record in ({}, {"parkedJobId": ""}, {"parkedJobId": None}, None):
            self.assertFalse(sr.holds_parked_job(record))


if __name__ == "__main__":
    unittest.main()
