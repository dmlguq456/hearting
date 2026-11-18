#!/usr/bin/env python3
"""The one judgment that a window's viewer holds a conversation sent to the background (M1).

Real child processes carry the fixed argv; their records sit in an isolated Claude home. The
link holds only when both facts agree for live processes, and every other shape is ``None``.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import session_registry as sr  # noqa: E402

OLD = "92d7cdcd-3cef-4583-80e6-f9a4561ac629"
NEW = "e675a253-0561-410f-aa04-c12345cabf70"
OTHER = "11111111-2222-4333-8444-555555555555"
THIRD = "66666666-7777-4888-8999-000000000000"
SLEEP = "import time; time.sleep(60)"


class ParkedSuccessor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pk-")
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        (Path(self.home) / "sessions").mkdir()
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
        start = sr._proc_start(child.pid)
        body = {"pid": child.pid, "sessionId": OLD, "kind": "interactive", "procStart": start,
                "startedAt": 1_700_000_000_000, **fields}
        (Path(self.home) / "sessions" / ("%d.json" % child.pid)).write_text(json.dumps(body))
        return body

    def viewer(self, sid=OLD, job="job1", **fields):
        child = self.spawn()
        self.record(child, sessionId=sid, **({"parkedJobId": job} if job else {}), **fields)
        return child

    def background(self, sid=NEW, job="job1", resume=OLD, **fields):
        argv = ["--session-id", sid, "--fork-session"]
        if resume:
            argv += ["--resume", "/h/.claude/projects/p/%s.jsonl" % resume]
        child = self.spawn(*argv)
        self.record(child, sessionId=sid, kind="bg", jobId=job, startedAt=1_700_000_500_000, **fields)
        return child

    def succ(self, sid=OLD, **kw):
        return sr.parked_successor("claude", sid, home=self.home, **kw)

    # --- the link -----------------------------------------------------------------------
    def test_both_facts_link_in_both_directions(self):
        viewer, bg = self.viewer(), self.background()
        found = self.succ()
        self.assertEqual((found["session_id"], found["pid"], found["viewer_sid"], found["viewer_pid"]),
                         (NEW, bg.pid, OLD, viewer.pid))
        self.assertEqual(found["started_at"], 1_700_000_500.0)
        self.assertEqual(self.succ(viewer_pid=viewer.pid, viewer_start=sr._proc_start(viewer.pid))["session_id"], NEW)
        origin = sr.parked_origin("claude", NEW, home=self.home)
        self.assertEqual((origin["viewer_sid"], origin["session_id"]), (OLD, NEW))
        # The background side is not a viewer; the viewer side is not a background session.
        self.assertIsNone(self.succ(NEW))
        self.assertIsNone(sr.parked_origin("claude", OLD, home=self.home))

    def test_uppercase_ids_link_like_lowercase(self):
        self.viewer(), self.background()
        self.assertEqual(self.succ(OLD.upper())["session_id"], NEW)

    # --- shapes that must not link ------------------------------------------------------
    def test_deliberate_fork_session_has_argv_but_no_parked_job(self):
        self.viewer(job=None)
        self.background()
        self.assertIsNone(self.succ())
        self.assertIsNone(sr.parked_origin("claude", NEW, home=self.home))

    def test_background_session_started_elsewhere_has_no_viewer_job(self):
        self.background(resume=None)
        self.assertIsNone(self.succ())
        self.assertIsNone(sr.parked_origin("claude", NEW, home=self.home))

    def test_job_only_without_argv_resume_does_not_link(self):
        self.viewer()
        self.background(resume=None)
        self.assertIsNone(self.succ())

    def test_argv_names_another_conversation(self):
        self.viewer()
        self.background(resume=OTHER)
        self.assertIsNone(self.succ())

    def test_original_conversation_resumed_in_the_window_drops_the_job(self):
        self.viewer(job=None)  # parkedJobId gone from the live record
        self.background()
        self.assertIsNone(self.succ())

    def test_dead_background_process(self):
        self.viewer()
        bg = self.background()
        bg.kill()
        bg.wait()
        self.assertIsNone(self.succ())

    def test_dead_viewer_process(self):
        viewer = self.viewer()
        self.background()
        viewer.kill()
        viewer.wait()
        self.assertIsNone(self.succ())

    def test_reused_pid_is_not_the_recorded_process(self):
        viewer = self.viewer()
        self.background()
        path = Path(self.home) / "sessions" / ("%d.json" % viewer.pid)
        body = json.loads(path.read_text())
        body["procStart"] = "1"
        path.write_text(json.dumps(body))
        self.assertIsNone(self.succ())

    def test_another_window_pointing_at_the_same_job(self):
        self.viewer()
        self.viewer(sid=OTHER)  # a second live viewer holds the same job
        self.background()
        self.assertIsNone(self.succ())
        self.assertIsNone(self.succ(OTHER))

    def test_two_live_processes_answer_to_the_original_id(self):
        self.viewer()
        self.viewer(job=None)  # the conversation also runs in another window
        self.background()
        self.assertIsNone(self.succ())

    def test_two_background_processes_for_one_job(self):
        self.viewer()
        self.background()
        self.background(sid=THIRD)
        self.assertIsNone(self.succ())

    def test_broken_or_unreadable_record(self):
        viewer = self.viewer()
        self.background()
        path = Path(self.home) / "sessions" / ("%d.json" % viewer.pid)
        path.write_text("{not json")
        self.assertIsNone(self.succ())
        path.write_text(json.dumps(["not", "a", "record"]))
        self.assertIsNone(self.succ())
        path.unlink()
        self.assertIsNone(self.succ())

    def test_background_record_without_job_or_session(self):
        self.viewer()
        bg = self.background()
        path = Path(self.home) / "sessions" / ("%d.json" % bg.pid)
        body = json.loads(path.read_text())
        broken = dict(body, jobId=None)
        path.write_text(json.dumps(broken))
        self.assertIsNone(self.succ())
        path.write_text(json.dumps(dict(body, sessionId="not-a-session-id")))
        self.assertIsNone(self.succ())

    def test_viewer_identity_must_match_the_caller_when_given(self):
        viewer = self.viewer()
        self.background()
        self.assertIsNone(self.succ(viewer_pid=viewer.pid + 1))
        self.assertIsNone(self.succ(viewer_start="1"))

    def test_missing_folder_and_other_harnesses(self):
        self.assertIsNone(sr.parked_successor("claude", OLD, home=self.home + "/missing"))
        self.viewer(), self.background()
        for harness in ("codex", "opencode"):
            self.assertIsNone(sr.parked_successor(harness, OLD, home=self.home))
            self.assertIsNone(sr.parked_origin(harness, NEW, home=self.home))
        self.assertIsNone(sr.parked_successor("claude", "", home=self.home))
        self.assertIsNone(sr.parked_successor("claude", None, home=self.home))

    def test_link_ends_with_either_process_and_is_never_stored(self):
        viewer, bg = self.viewer(), self.background()
        self.assertEqual(self.succ()["session_id"], NEW)
        before = sorted(os.listdir(self.home)), sorted(os.listdir(Path(self.home) / "sessions"))
        self.assertEqual(self.succ()["session_id"], NEW)
        self.assertEqual((sorted(os.listdir(self.home)), sorted(os.listdir(Path(self.home) / "sessions"))), before)
        bg.kill()
        bg.wait()
        self.assertIsNone(self.succ())


if __name__ == "__main__":
    unittest.main(verbosity=2)
