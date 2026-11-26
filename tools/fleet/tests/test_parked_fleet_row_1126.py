#!/usr/bin/env python3
"""Fleet's row, number and pane header follow a conversation sent to the background (F1, 11-26).

Claude's `/bg` leaves the window's process as a viewer of the old session id while the
conversation goes on as another session in another process. Real child processes carry the
fixed argv and their records sit in an isolated Claude home; the one judgment
(``session_registry.steady_parked_links``, read once per pass) links them only when both facts
agree. Then Fleet draws one row (the held session, in the viewer's window, also answering to the
old id and carrying its waits for the person), the held session continues the window's number,
and the window's pane header names the held session. With one fact only, or when the merge
refuses, everything stays as two rows with their own numbers. A read that fails does not change
what the previous pass decided. Made-up ids only.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import copy
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _path in (_TOOLS_DIR, os.path.join(os.path.dirname(_TOOLS_DIR), "utilities")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fleet import collectors, herdr_projection, session_registry as sr, session_tags, titles  # noqa: E402
from fleet.collectors import herdr as herdr_collector  # noqa: E402
from fleet.model import Session  # noqa: E402

OLD = "3f1c0a7e-5b2d-4c6e-9a8f-1d2e3f4a5b6c"
NEW = "7a9b8c6d-2e4f-4a1b-8c3d-5e6f7a8b9c0d"
NEW2 = "5c4b3a29-8f7e-4d6c-9b5a-4f3e2d1c0b9a"
OTHER = "0e1d2c3b-4a59-4687-a5b4-c3d2e1f0a9b8"
JOB, JOB2 = "7a9b8c6d", "5c4b3a29"
PANE = "w1:p1"
SLEEP = "import time; time.sleep(120)"


class Split(unittest.TestCase):
    """An isolated Claude home with a window (viewer, OLD) and the held session (NEW)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pf-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        self.config = root / "claude"
        (self.config / "sessions").mkdir(parents=True)
        env = {"CLAUDE_CONFIG_DIR": str(self.config), "XDG_STATE_HOME": str(root / "state"),
               "XDG_CONFIG_HOME": str(root / "config"), "FLEET_SESSION_REGISTRY_DIR": str(root / "registry")}
        for name in ("FLEET_TITLE_STATE_DIR", "HERDR_SESSION_METADATA_FORMATTER"):
            os.environ.pop(name, None)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The judgment remembers the previous pass in this process only; each test starts fresh.
        sr._STEADY.clear()
        self.addCleanup(sr._STEADY.clear)
        self.agents = []
        agents = mock.patch.object(herdr_collector, "list_agents", lambda *a, **k: copy.deepcopy(self.agents))
        agents.start()
        self.addCleanup(agents.stop)
        self.children = {}
        self.addCleanup(self._reap)

    def _reap(self):
        for child in self.children.values():
            child.kill()
            child.wait()
        self.children = {}

    def spawn(self, key, *argv, executable=None):
        child = subprocess.Popen([executable or sys.executable, "-c", SLEEP, *argv], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children[key] = child
        return child

    def record(self, key, sid, **fields):
        child = self.children[key]
        path = self.config / "sessions" / ("%d.json" % child.pid)
        body = {"pid": child.pid, "procStart": sr._proc_start(child.pid), "sessionId": sid, **fields}
        path.write_text(json.dumps(body))
        return path

    def split(self, *, parked=True, resume=OLD, executable=None):
        """The window (viewer, OLD) and the held session (NEW), both live; their Fleet rows."""
        self.spawn("viewer", "--resume", OLD, executable=executable)
        self.record("viewer", OLD, kind="interactive", startedAt=1_700_000_000_000)
        argv = ["--session-id", NEW, "--fork-session"] + (
            ["--resume", "/h/.claude/projects/p/%s.jsonl" % resume] if resume else [])
        self.spawn("held", *argv, executable=executable)
        self.record("held", NEW, kind="bg", jobId=JOB, startedAt=1_700_000_500_000)
        if parked:
            self.park()
        return self.rows()

    def park(self, job=JOB):
        self.record("viewer", OLD, kind="interactive", startedAt=1_700_000_000_000,
                    **({"parkedJobId": job} if job else {}))

    def rows(self, *, held_start=None):
        viewer, held = self.children["viewer"], self.children["held"]
        window = Session(harness="claude", pid=viewer.pid, session_id=OLD, kind="interactive",
                         proc_start=sr._proc_start(viewer.pid), herdr_attached=True,
                         pane_location="w1:t1·p1", multiplexer="herdr", title="before the split")
        conversation = Session(harness="claude", pid=held.pid, session_id=NEW, kind="bg",
                               proc_start=held_start or sr._proc_start(held.pid), herdr_attached=False,
                               title="the conversation goes on")
        return [window, conversation]

    def number_pass(self, sessions):
        """One Fleet pass over these rows: the row join, then the number assignment."""
        collectors.apply_session_aliases(sessions)
        returned = session_tags.assign(sessions)
        return sessions, returned

    def snapshot(self):
        return session_tags._path().read_bytes()


class ParkedFleetRow(Split):
    # --- one row ---------------------------------------------------------------------------
    def test_linked_window_is_one_row_of_the_held_session_in_the_window(self):
        sessions = self.split()
        collectors.apply_session_aliases(sessions)
        self.assertEqual([s.session_id for s in sessions], [NEW])
        row = sessions[0]
        self.assertEqual((row.pid, row.title), (self.children["held"].pid, "the conversation goes on"))
        self.assertEqual((row.pane_location, row.multiplexer, row.herdr_attached), ("w1:t1·p1", "herdr", True))
        # Relations still addressed to the old id (requests, steward marker, messages) join this row.
        self.assertIn(("claude", OLD), sr.session_join_keys(row))
        self.assertEqual(row._parked_viewer, OLD)

    def test_one_fact_only_keeps_two_rows(self):
        for name, kwargs in (("deliberate --fork-session, no job held", {"parked": False}),
                             ("job held, argv names another conversation", {"resume": OTHER})):
            with self.subTest(name):
                self._reap()
                for path in (self.config / "sessions").glob("*.json"):
                    path.unlink()
                sessions = self.split(**kwargs)
                collectors.apply_session_aliases(sessions)
                self.assertEqual([s.session_id for s in sessions], [OLD, NEW])
                self.assertIsNone(sessions[1].pane_location)
                self.assertNotIn(("claude", OLD), sr.session_join_keys(sessions[1]))

    def test_a_second_row_on_either_side_is_doubt(self):
        sessions = self.split()
        sessions.append(Session(harness="claude", pid=self.children["held"].pid, session_id=NEW, kind="bg"))
        collectors.apply_session_aliases(sessions)
        self.assertEqual([s.session_id for s in sessions], [OLD, NEW, NEW])

    def test_a_held_row_of_another_process_start_is_not_merged(self):
        # The row's process start is not the link's (the pid was reused between the two reads).
        self.split()
        sessions = self.rows(held_start="1")
        collectors.apply_session_aliases(sessions)
        self.assertEqual([s.session_id for s in sessions], [OLD, NEW])
        self.assertFalse(any(getattr(s, "_parked_viewer", None) for s in sessions))

    def test_the_window_waits_move_to_the_row(self):
        sessions = self.split()
        sessions[0].interaction_state = {"kind": "decision", "source": "fleet", "waiting_since": 10.0}
        sessions[0].interaction_observations = [{"kind": "decision", "item_id": "w1", "waiting_since": 9.0}]
        collectors.apply_session_aliases(sessions)
        row, = sessions
        self.assertEqual(row.interaction_state["waiting_since"], 10.0)
        self.assertEqual([item["item_id"] for item in row.interaction_observations], ["w1"])
        # The row's own wait stays when it is the later one (the rule of interaction.enrich).
        sessions = self.rows()
        sessions[0].interaction_state = {"kind": "decision", "source": "fleet", "waiting_since": 10.0}
        sessions[1].interaction_state = {"kind": "approval", "source": "fleet", "waiting_since": 20.0}
        collectors.apply_session_aliases(sessions)
        self.assertEqual(sessions[0].interaction_state["kind"], "approval")

    def test_only_the_merged_row_answers_to_the_old_id(self):
        # A second background session and an unrelated `--resume OLD` elsewhere both carry the old
        # id in their argv; the window's old id is still a live process's identity.
        sessions = self.split()
        self.spawn("bg2", "--session-id", NEW2, "--fork-session", "--resume", "/h/p/%s.jsonl" % OLD)
        self.record("bg2", NEW2, kind="bg", jobId=JOB2)
        self.spawn("other", "--session-id", OTHER, "--resume", OLD)
        self.record("other", OTHER, kind="interactive")
        sessions += [Session(harness="claude", pid=self.children[key].pid, session_id=sid, kind="bg",
                             proc_start=sr._proc_start(self.children[key].pid))
                     for key, sid in (("bg2", NEW2), ("other", OTHER))]
        collectors.apply_session_aliases(sessions)
        answering = [s.session_id for s in sessions if ("claude", OLD) in sr.session_join_keys(s)]
        self.assertEqual(answering, [NEW])

    # --- one number ------------------------------------------------------------------------
    def test_held_session_continues_the_window_number_and_frees_its_own(self):
        sessions = self.split(parked=False)
        window_tag = session_tags.assign_observed("claude", OLD, pid=self.children["viewer"].pid,
                                                  proc_start=sr._proc_start(self.children["viewer"].pid))
        session_tags.assign(sessions)
        held_tag = session_tags.assigned_tag("claude", NEW)
        self.assertTrue(window_tag and held_tag and window_tag != held_tag)
        # Claude writes parkedJobId into the window's record: one row, and it keeps the window's number.
        self.park()
        self.number_pass(self.rows())
        self.assertEqual(session_tags.assigned_tag("claude", NEW), window_tag)
        self.assertIsNone(session_tags.assigned_tag("claude", OLD))
        snapshot = json.loads(session_tags._path().read_text())["assignments"]
        self.assertNotIn(held_tag, [row["tag"] for row in snapshot])
        # The link ends (the window holds no job): the number stays with the conversation.
        self.park(job=None)
        self.number_pass(self.rows())
        self.assertEqual(session_tags.assigned_tag("claude", NEW), window_tag)

    def test_one_fact_only_keeps_both_numbers(self):
        sessions = self.split(parked=False)
        window_tag = session_tags.assign_observed("claude", OLD, pid=self.children["viewer"].pid,
                                                  proc_start=sr._proc_start(self.children["viewer"].pid))
        session_tags.assign(sessions)
        held_tag = session_tags.assigned_tag("claude", NEW)
        self.number_pass(self.rows())
        self.assertEqual((session_tags.assigned_tag("claude", OLD), session_tags.assigned_tag("claude", NEW)),
                         (window_tag, held_tag))

    def test_a_refused_merge_keeps_the_window_number(self):
        # Two rows stay, so the window keeps being an observation and keeps its number.
        self.split(parked=False)
        window_tag = session_tags.assign_observed("claude", OLD, pid=self.children["viewer"].pid,
                                                  proc_start=sr._proc_start(self.children["viewer"].pid))
        session_tags.assign(self.rows())
        self.park()
        for name, sessions in (("held row of another process start", self.rows(held_start="1")),
                               ("two held rows", self.rows() + self.rows()[1:]),
                               ("two window rows", self.rows()[:1] + self.rows())):
            with self.subTest(name):
                self.number_pass(sessions)
                self.assertGreater(len(sessions), 1)
                self.assertEqual(session_tags.assigned_tag("claude", OLD), window_tag)

    # --- a read that fails changes nothing ---------------------------------------------------
    def test_a_failed_read_keeps_the_previous_pass(self):
        self.split(parked=False)
        window_tag = session_tags.assign_observed("claude", OLD, pid=self.children["viewer"].pid,
                                                  proc_start=sr._proc_start(self.children["viewer"].pid))
        self.agents = [{"agent": "claude", "pane_id": PANE, "display_agent": "[%s] claude" % window_tag,
                        "agent_session": {"kind": "id", "value": OLD}}]
        session_tags.assign(self.rows())
        self.park()
        sessions, _ = self.number_pass(self.rows())
        self.assertEqual([s.session_id for s in sessions], [NEW])
        settled = self.snapshot()
        real = sr._live_claude_records

        def unread(*_a, **_k):
            raise OSError("the record folder was not read")
        viewer_path = self.config / "sessions" / ("%d.json" % self.children["viewer"].pid)
        whole = viewer_path.read_text()
        for name in ("folder not read", "the window's record half written"):
            with self.subTest(name):
                if name == "folder not read":
                    patch = mock.patch.object(sr, "_live_claude_records", unread)
                else:
                    viewer_path.write_text(whole[:len(whole) // 2])
                    patch = mock.patch.object(sr, "_live_claude_records", real)
                with patch:
                    sessions, returned = self.number_pass(self.rows())
                viewer_path.write_text(whole)
                self.assertEqual([s.session_id for s in sessions], [NEW])
                self.assertEqual(self.snapshot(), settled)   # the number file is not rewritten
                self.assertEqual(session_tags.assigned_tag("claude", NEW), window_tag)
                self.assertEqual([agent.get("held_session") for agent in returned], [NEW])
        # A read that decides (the window holds no job) ends the link at once.
        self.park(job=None)
        sessions, _ = self.number_pass(self.rows())
        self.assertEqual([s.session_id for s in sessions], [OLD, NEW])

    def test_a_failed_first_read_makes_no_link(self):
        self.split()

        def unread(*_a, **_k):
            raise OSError("the record folder was not read")
        with mock.patch.object(sr, "_live_claude_records", unread):
            sessions, _ = self.number_pass(self.rows())
        self.assertEqual([s.session_id for s in sessions], [OLD, NEW])

    # --- the pane header -------------------------------------------------------------------
    def test_window_pane_header_follows_the_held_session_with_the_same_number(self):
        self.split(parked=False)
        window_tag = session_tags.assign_observed("claude", OLD, pane_id=PANE, pid=self.children["viewer"].pid,
                                                  proc_start=sr._proc_start(self.children["viewer"].pid))
        titles.write(OLD, "before the split", now=time.time() - 60)
        # The header as the window's own hooks left it before the split.
        before = herdr_projection._metadata_command("herdr", "claude", OLD, PANE)
        self.agents = [{"agent": "claude", "pane_id": PANE,
                        "display_agent": before[before.index("--display-agent") + 1],
                        "title": before[before.index("--title") + 1],
                        "agent_session": {"kind": "id", "value": OLD}}]
        self.assertEqual(list(herdr_projection._tag_metadata_targets(self.agents)), [])
        session_tags.assign(self.rows())
        titles.write(NEW, "the conversation goes on", now=time.time())
        self.park()
        _, returned = self.number_pass(self.rows())
        self.assertEqual([(agent["pane_id"], agent.get("held_session")) for agent in returned], [(PANE, NEW)])
        # Same number, stale title: the window's pane is a target, and its header names the held session.
        targets = list(herdr_projection._tag_metadata_targets(returned))
        self.assertEqual(targets, [((PANE, "claude", OLD), NEW)])
        with mock.patch.object(herdr_projection, "is_steward", lambda h, s, **k: s == OLD):
            command = herdr_projection._metadata_command("herdr", "claude", OLD, PANE, held=NEW)
        self.assertEqual(command[command.index("--display-agent") + 1], "[%s] ⚑ claude" % window_tag)
        self.assertIn("the conversation goes on", command[command.index("--title") + 1])
        # The re-check right before sending passes: the number it prepared is the number now.
        self.assertEqual(command[command.index("--display-agent") + 1][1:3],
                         session_tags.assigned_tag("claude", NEW))
        # Once herdr holds that header, the next pass writes nothing.
        sent = herdr_projection._metadata_command("herdr", "claude", OLD, PANE, held=NEW)
        self.agents = [dict(self.agents[0], display_agent=sent[sent.index("--display-agent") + 1],
                            title=sent[sent.index("--title") + 1])]
        _, returned = self.number_pass(self.rows())
        self.assertEqual(list(herdr_projection._tag_metadata_targets(returned)), [])
        # A pane whose herdr record has no title is judged by its number only.
        self.assertEqual(list(herdr_projection._tag_metadata_targets(
            [{key: value for key, value in returned[0].items() if key != "title"}])), [])
        # The link ends and the window goes on as its own session: its header names it again.
        self.park(job=None)
        _, returned = self.number_pass(self.rows())
        own = session_tags.assigned_tag("claude", OLD)
        self.assertTrue(own and own != window_tag)
        self.assertEqual(list(herdr_projection._tag_metadata_targets(returned)), [((PANE, "claude", OLD), None)])

    # --- the pane proof reads the same module ----------------------------------------------
    def test_pane_proof_withdraws_on_one_fact(self):
        self.assertTrue(sr.holds_parked_job({"parkedJobId": JOB}))
        for record in ({}, {"parkedJobId": ""}, {"parkedJobId": None}, None):
            self.assertFalse(sr.holds_parked_job(record))


class ParkedWaitInFleet(Split):
    """Fleet's own collection: a wait the old id opened is shown on the merged row, once."""

    def setUp(self):
        super().setUp()
        self.binary = self.root / "bin" / "claude"   # Fleet reads a process as Claude by its executable
        self.binary.parent.mkdir()
        shutil.copy(os.path.realpath(sys.executable), self.binary)

    def collect(self, view, keys):
        from fleet.collectors import procscan
        import human_wait_state
        pids = [self.children[key].pid for key in keys]
        ps = subprocess.run(["ps", "-o", "pid=,comm=,etime=,args=", "-p", ",".join(map(str, pids))],
                            capture_output=True, text=True, check=True).stdout.splitlines()
        viewer = self.children["viewer"].pid

        def herdr(sessions, **_kw):
            for s in sessions:
                s._gpu_session_aliases, s._herdr_name, s._pane_session_claim, s._herdr_alias_pane = [], None, None, None
                s.herdr_attached = s.pid == viewer
                if s.pid == viewer:
                    s.pane_location, s.multiplexer = "w1:t1·p1", "herdr"
            return {"complete": True}
        patches = [mock.patch.object(procscan, "_ps_lines", lambda: procscan._CensusRows(ps, complete=True)),
                   mock.patch.object(procscan, "proc_tree", lambda: {pid: (1, 60, "claude") for pid in pids}),
                   mock.patch.object(procscan, "_pid_ttys", lambda: {}),
                   mock.patch.object(procscan, "_detached_ttys", lambda: set()),
                   mock.patch.object(herdr_collector, "enrich", herdr),
                   mock.patch.object(human_wait_state, "cached_fleet_view", lambda now=None: copy.deepcopy(view))]
        try:
            from fleet.collectors import resource_runs
            patches.append(mock.patch.object(resource_runs, "collect", lambda *a, **k: []))
        except ImportError:
            pass
        for patch in patches:
            patch.start()
        try:
            return collectors.collect_all(harness_filter={"claude"}).sessions
        finally:
            for patch in reversed(patches):
                patch.stop()

    def test_a_wait_of_the_old_id_stays_on_the_window_row(self):
        self.split(executable=str(self.binary))
        self.spawn("other", "--session-id", OTHER, "--resume", OLD, executable=str(self.binary))
        self.record("other", OTHER, kind="interactive")
        now = time.time()
        item = {"id": "qw-made-up-1", "harness": "claude", "session_id": OLD, "kind": "human gate",
                "source": "human-gate", "since": now - 120, "stale": "current", "answer": None}
        view = {"items": [item], "by_session": {("claude", OLD): ["qw-made-up-1"]}, "complete": True,
                "total": 1, "older": 0, "health": []}
        sessions = self.collect(view, ("viewer", "held", "other"))
        carrying = [(s.session_id, s.liveness) for s in sessions
                    if any(obs.get("item_id") == "qw-made-up-1" for obs in s.interaction_observations or [])]
        self.assertEqual(carrying, [(NEW, "blocked")])
        self.assertEqual(sorted(s.session_id for s in sessions), sorted([NEW, OTHER]))


if __name__ == "__main__":
    unittest.main()
