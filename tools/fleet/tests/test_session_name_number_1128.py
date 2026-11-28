#!/usr/bin/env python3
"""The session list's name and Fleet's number agree for a Claude window (11-28).

The list shows a window by the name Claude gave its process when it registered,
``<folder>-<xx>`` (``nameSource: derived``), kept across /clear, compaction and an in-process
resume. Fleet's number is stored per session id. After compaction, a resume (in a new process or
in the same one) and a conversation sent to the background, the number the window's row shows
is the name's number, and the name itself never changes. A number another live session holds is
never taken. Real child processes stand in for Claude; their records sit in an isolated Claude
home. Made-up ids only.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
import select
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import collectors, session_registry as sr, session_tags  # noqa: E402
from fleet.collectors import herdr as herdr_collector  # noqa: E402
from fleet.model import Session  # noqa: E402

OLD = "3f2e1d0c-9b8a-4766-8554-433221100fed"
NEW = "7a9b8c6d-2e4f-4a1b-8c3d-5e6f7a8b9c0d"
EARLIER = "11223344-5566-4778-8899-aabbccddeeff"
OTHER = "0e1d2c3b-4a59-4687-a5b4-c3d2e1f0a9b8"
JOB = NEW[:8]
HOLD = "import os; os.write(1, b'R'); os.read(0, 1)"


class SessionNameNumber(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="snn-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.config = root / "claude"
        (self.config / "sessions").mkdir(parents=True)
        env = {"CLAUDE_CONFIG_DIR": str(self.config), "XDG_STATE_HOME": str(root / "state"),
               "XDG_CONFIG_HOME": str(root / "config"), "FLEET_SESSION_REGISTRY_DIR": str(root / "registry")}
        for name in ("FLEET_TITLE_STATE_DIR", "HERDR_SESSION_METADATA_FORMATTER"):
            os.environ.pop(name, None)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for memory in (sr._STEADY, sr._STEADY_UNCONFIRMED):
            memory.clear()
            self.addCleanup(memory.clear)
        agents = mock.patch.object(herdr_collector, "list_agents", lambda *a, **k: [])
        agents.start()
        self.addCleanup(agents.stop)
        self.children = {}
        self.names = {}
        self.addCleanup(self._reap)

    def _reap(self):
        for key in list(self.children):
            self.end(key)

    def spawn(self, key, *argv):
        child = subprocess.Popen([sys.executable, "-c", HOLD, *argv], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.children[key] = child
        with child.stdout:
            self.assertTrue(select.select([child.stdout], [], [], 10)[0], "fixture child did not start")
            self.assertEqual(child.stdout.read(1), b"R", "fixture child exited before it was ready")

    def end(self, key):
        child = self.children.pop(key)
        child.stdin.close()
        child.kill()
        child.wait(timeout=10)
        self.path(child).unlink(missing_ok=True)
        self.names.pop(key, None)

    def path(self, child):
        return self.config / "sessions" / ("%d.json" % child.pid)

    def record(self, key, sid, name=None, **fields):
        """A process record as Claude writes it; the name it registered stays as it was."""
        child = self.children[key]
        body = {"pid": child.pid, "procStart": sr._proc_start(child.pid), "sessionId": sid,
                "kind": "interactive", "startedAt": 1_700_000_000_000, **fields}
        if name:
            body.update(name=name, nameSource="derived")
            self.names[key] = name
        self.path(child).write_text(json.dumps(body))

    def row(self, key, sid, kind="interactive"):
        child = self.children[key]
        return Session(harness="claude", pid=child.pid, session_id=sid, kind=kind,
                       proc_start=sr._proc_start(child.pid))

    def fleet_pass(self, *rows):
        """One Fleet pass: the row join (#1849 merge), then the shared number assignment."""
        rows = list(rows)
        collectors.apply_session_aliases(rows)
        session_tags.assign(rows)
        return rows

    def observe(self, key, sid):
        """A native start report (startup, resume, clear or compact) for this process."""
        child = self.children[key]
        session_tags.assign_observed("claude", sid, pid=child.pid, proc_start=sr._proc_start(child.pid))

    def assertSame(self, key, sid):
        """The list's number for this window is the number Fleet shows, and the name is unchanged."""
        name = json.loads(self.path(self.children[key]).read_text()).get("name")
        self.assertEqual(name, self.names[key])
        self.assertEqual(session_tags.assigned_tag("claude", sid), name.rsplit("-", 1)[1])

    # --- compaction --------------------------------------------------------------------------
    def test_compaction_keeps_the_name_and_the_number(self):
        self.spawn("w", "--resume", OLD)
        self.record("w", OLD, "proj-72")
        self.fleet_pass(self.row("w", OLD))
        self.assertSame("w", OLD)
        self.observe("w", OLD)                       # the same session id after compaction
        self.fleet_pass(self.row("w", OLD))
        self.assertSame("w", OLD)

    def test_a_number_drawn_before_the_record_named_the_session_follows_the_name(self):
        self.spawn("w", "--resume", OLD)
        self.record("w", EARLIER, "proj-72")         # the record does not carry the new id yet
        self.observe("w", OLD)
        drawn = session_tags.assigned_tag("claude", OLD)
        self.assertNotEqual(drawn, "72")
        self.record("w", OLD, "proj-72")
        self.fleet_pass(self.row("w", OLD))
        self.assertSame("w", OLD)
        self.observe("w", OLD)
        self.fleet_pass(self.row("w", OLD))
        self.assertSame("w", OLD)
        self.assertNotIn(drawn, [row["tag"] for row in json.loads(session_tags._path().read_text())["assignments"]])

    # --- resume ------------------------------------------------------------------------------
    def test_resume_in_a_new_process_shows_the_new_name_number(self):
        self.spawn("first", "--resume", OLD)
        self.record("first", OLD, "proj-8e")
        self.fleet_pass(self.row("first", OLD))
        self.assertSame("first", OLD)
        self.end("first")
        self.spawn("w", "--resume", OLD)
        self.record("w", OLD, "proj-72")
        self.observe("w", OLD)
        self.fleet_pass(self.row("w", OLD))
        self.assertSame("w", OLD)

    def test_resume_inside_the_window_keeps_the_window_number(self):
        self.spawn("earlier", "--resume", EARLIER)
        self.record("earlier", EARLIER, "proj-8e")
        self.fleet_pass(self.row("earlier", EARLIER))
        self.end("earlier")
        self.spawn("w", "--resume", OLD)
        self.record("w", OLD, "proj-72")
        self.fleet_pass(self.row("w", OLD))
        self.assertSame("w", OLD)
        self.record("w", EARLIER, "proj-72")         # /resume: Claude keeps the registered name
        self.fleet_pass(self.row("w", EARLIER))
        self.assertSame("w", EARLIER)
        self.assertIsNone(session_tags.assigned_tag("claude", OLD))

    # --- conversation sent to the background (#1849) -----------------------------------------
    def test_background_split_shows_the_window_name_number(self):
        self.spawn("first", "--resume", OLD)
        self.record("first", OLD, "proj-8e")
        self.fleet_pass(self.row("first", OLD))
        self.end("first")
        self.spawn("viewer", "--resume", OLD)
        self.record("viewer", OLD, "proj-82")
        self.fleet_pass(self.row("viewer", OLD))
        self.assertSame("viewer", OLD)
        self.spawn("held", "--session-id", NEW, "--fork-session", "--resume", "/h/.claude/projects/p/%s.jsonl" % OLD)
        self.record("held", NEW, kind="bg", jobId=JOB, startedAt=1_700_000_500_000)
        self.record("viewer", OLD, "proj-82", parkedJobId=JOB)
        rows = self.fleet_pass(self.row("viewer", OLD), self.row("held", NEW, kind="bg"))
        self.assertEqual([s.session_id for s in rows], [NEW])
        self.assertSame("viewer", NEW)

    def test_split_seen_first_still_shows_the_window_name_number(self):
        self.spawn("viewer", "--resume", OLD)
        self.spawn("held", "--session-id", NEW, "--fork-session", "--resume", "/h/.claude/projects/p/%s.jsonl" % OLD)
        self.record("held", NEW, kind="bg", jobId=JOB, startedAt=1_700_000_500_000)
        self.record("viewer", OLD, "proj-82", parkedJobId=JOB)
        self.fleet_pass(self.row("viewer", OLD), self.row("held", NEW, kind="bg"))
        self.assertSame("viewer", NEW)

    # --- what never moves --------------------------------------------------------------------
    def test_a_number_another_live_session_holds_is_never_taken(self):
        self.spawn("a", "--resume", OTHER)
        self.record("a", OTHER, "proj-8e")
        self.fleet_pass(self.row("a", OTHER))
        self.spawn("first", "--resume", OLD)
        self.record("first", OLD, "proj-31")
        self.fleet_pass(self.row("a", OTHER), self.row("first", OLD))
        self.end("first")
        self.spawn("w", "--resume", OLD)             # resumed under the name the other one shows
        self.record("w", OLD, "proj-8e")
        self.fleet_pass(self.row("a", OTHER), self.row("w", OLD))
        self.assertSame("a", OTHER)
        self.assertEqual(session_tags.assigned_tag("claude", OLD), "31")

    def test_two_names_for_one_session_move_nothing(self):
        self.spawn("first", "--resume", OLD)
        self.record("first", OLD, "proj-31")
        self.fleet_pass(self.row("first", OLD))
        self.spawn("second", "--resume", OLD)
        self.record("second", OLD, "proj-72")
        self.fleet_pass(self.row("first", OLD), self.row("second", OLD))
        self.assertEqual(session_tags.assigned_tag("claude", OLD), "31")


if __name__ == "__main__":
    unittest.main()
