#!/usr/bin/env python3
"""The session list's name and Fleet's number agree for a Claude window (11-28).

The list shows a window by the name Claude gave its process when it registered,
``<folder>-<xx>`` (``nameSource: derived``), kept across /clear, compaction and an in-process
resume. Fleet's number is stored per session id. After compaction, a resume (in a new process or
in the same one) and a conversation sent to the background, the number the window's row shows
is the name's number, and the name itself never changes. A number another live session holds is
never taken, and a number a session moves off stays its address while it lives: it never reaches
another session. Only certain evidence moves a number: the window's own record with its process
start time, not a row a later writer replaced, and for a held conversation the window #1849 chose.
Real child processes stand in for Claude; their records sit in an isolated Claude home. Made-up
ids only.
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
for _path in (_TOOLS_DIR, os.path.join(os.path.dirname(_TOOLS_DIR), "utilities")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fleet import collectors, session_registry as sr, session_tags  # noqa: E402
from fleet.collectors import herdr as herdr_collector  # noqa: E402
from fleet.model import Session  # noqa: E402
from fleet.session_handle import minted_tag  # noqa: E402
import peer_address  # noqa: E402
import session_shell  # noqa: E402

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
        self.agents = []
        agents = mock.patch.object(herdr_collector, "list_agents", lambda *a, **k: self.agents)
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

    def address(self, tag):
        """Who a remembered ``[xx]`` reaches: a peer message, and the session shell's target."""
        found, _ = peer_address.resolve("[%s]" % tag)
        with mock.patch.object(session_shell, "exact_binding", lambda harness, sid: ((harness, sid), None)):
            shell, _ = session_shell.resolve_target("[%s]" % tag)
        self.assertEqual(shell, found and (found["harness"], found["session_id"]))
        return found and found["session_id"]

    def rows(self):
        return session_tags._read(session_tags._path())

    def resumed_on_a_new_name(self):
        """OLD shows ``[8e]``; a new process resumes it under ``proj-72`` and reports it first."""
        self.spawn("first", "--resume", OLD)
        self.record("first", OLD, "proj-8e")
        self.fleet_pass(self.row("first", OLD))
        self.end("first")
        self.spawn("w", "--resume", OLD)
        self.record("w", OLD, "proj-72")
        self.observe("w", OLD)
        self.assertEqual(session_tags.assigned_tag("claude", OLD), "8e")

    def stale_census(self, during):
        """A full pass whose inventory was read before ``during`` ran and wrote."""
        inventory = session_tags._inventory

        def late(sessions):
            found = inventory(sessions)
            during()
            return found
        return mock.patch.object(session_tags, "_inventory", side_effect=late)

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
        self.assertEqual(self.address(drawn), OLD)   # the early number still reaches the same session

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

    def test_two_windows_with_one_name_the_one_stored_first_takes_its_number(self):
        self.spawn("x", "--resume", OTHER)
        self.record("x", OTHER, "proj-72")
        self.fleet_pass(self.row("x", OTHER))
        self.spawn("first", "--resume", OLD)
        self.record("first", OLD, "proj-31")
        self.fleet_pass(self.row("x", OTHER), self.row("first", OLD))
        self.end("first")
        self.spawn("a", "--resume", OLD)
        self.record("a", OLD, "proj-72")
        self.fleet_pass(self.row("x", OTHER), self.row("a", OLD))
        self.assertEqual(session_tags.assigned_tag("claude", OLD), "31")   # [72] is the other's
        self.end("x")
        self.spawn("b", "--resume", NEW)
        self.record("b", NEW, "proj-72")
        self.fleet_pass(self.row("a", OLD), self.row("b", NEW))
        self.assertSame("a", OLD)
        self.assertNotIn(session_tags.assigned_tag("claude", NEW), ("72", "31"))

    def test_a_new_session_candidate_does_not_block_a_stored_window(self):
        self.resumed_on_a_new_name()
        sid = next("synthetic-live-72-%d" % n for n in range(4096) if minted_tag("synthetic-live-72-%d" % n) == "72")
        self.spawn("codex")
        codex = Session(harness="codex", pid=self.children["codex"].pid, session_id=sid,
                        proc_start=sr._proc_start(self.children["codex"].pid))
        self.fleet_pass(self.row("w", OLD), codex)
        self.assertSame("w", OLD)
        self.assertNotIn(session_tags.assigned_tag("codex", sid), (None, "72", "8e"))

    def test_an_independent_background_record_moves_nothing(self):
        self.spawn("bg", "--session-id", NEW)
        self.record("bg", NEW, kind="bg")
        self.fleet_pass(self.row("bg", NEW, kind="bg"))
        before = session_tags.assigned_tag("claude", NEW)
        self.record("bg", NEW, "proj-%s" % ("73" if before == "72" else "72"), kind="bg")
        self.fleet_pass(self.row("bg", NEW, kind="bg"))
        self.assertEqual(session_tags.assigned_tag("claude", NEW), before)

    # --- the number a session moves off stays its address (review 1) ---------------------------
    def test_the_number_moved_off_is_not_drawn_by_a_new_session_in_the_same_pass(self):
        self.resumed_on_a_new_name()
        sid = next("synthetic-live-8e-%d" % n for n in range(4096) if minted_tag("synthetic-live-8e-%d" % n) == "8e")
        self.spawn("codex")
        codex = Session(harness="codex", pid=self.children["codex"].pid, session_id=sid,
                        proc_start=sr._proc_start(self.children["codex"].pid))
        self.fleet_pass(self.row("w", OLD), codex)
        self.assertSame("w", OLD)
        self.assertNotIn(session_tags.assigned_tag("codex", sid), (None, "8e", "72"))
        self.assertEqual(self.address("8e"), OLD)
        self.assertEqual(self.address("72"), OLD)

    def test_the_number_moved_off_is_not_drawn_by_a_later_narrow_report(self):
        self.resumed_on_a_new_name()
        self.fleet_pass(self.row("w", OLD))
        self.assertSame("w", OLD)
        self.assertEqual(self.address("8e"), OLD)
        sid = next("synthetic-live-8e-%d" % n for n in range(4096) if minted_tag("synthetic-live-8e-%d" % n) == "8e")
        self.assertNotIn(session_tags.assign_observed("codex", sid, pane_id="w:codex"), (None, "8e", "72"))
        self.assertEqual(self.address("8e"), OLD)
        self.end("w")                                # the session ends: its numbers free together
        session_tags.assign([])
        self.assertEqual(self.address("8e"), None)
        self.assertNotIn(OLD, [key[1] for key in self.rows()])

    def test_the_number_moved_off_is_not_taken_by_another_window_named_for_it(self):
        self.spawn("first", "--resume", OLD)
        self.record("first", OLD, "proj-8e")
        self.fleet_pass(self.row("first", OLD))
        self.spawn("b", "--resume", OTHER)
        self.record("b", OTHER, "proj-8e")
        self.fleet_pass(self.row("first", OLD), self.row("b", OTHER))
        self.assertEqual(session_tags.assigned_tag("claude", OTHER), "8f")
        self.end("first")
        self.spawn("w", "--resume", OLD)
        self.record("w", OLD, "proj-72")
        self.observe("w", OLD)
        self.fleet_pass(self.row("w", OLD), self.row("b", OTHER))
        self.assertSame("w", OLD)
        self.assertEqual(session_tags.assigned_tag("claude", OTHER), "8f")
        self.assertEqual(self.address("8e"), OLD)

    # --- a late census never renames or retires a newer row (review 2) ---------------------------
    def test_a_late_census_does_not_move_a_number_by_a_name_it_read_before_a_newer_report(self):
        self.resumed_on_a_new_name()                 # the census reads proj-72 in process w ...

        def resumed_again():                         # ... while w ends and w2 resumes it as proj-83
            self.end("w")
            self.spawn("w2", "--resume", OLD)
            self.record("w2", OLD, "proj-83")
            self.observe("w2", OLD)
        row = self.row("w", OLD)
        with self.stale_census(resumed_again):
            self.fleet_pass(row)
        self.assertEqual(session_tags.assigned_tag("claude", OLD), "8e")
        self.assertEqual(self.rows()[("claude", OLD)]["pid"], self.children["w2"].pid)
        self.fleet_pass(self.row("w2", OLD))
        self.assertSame("w2", OLD)
        self.assertEqual(self.address("8e"), OLD)

    def test_a_late_census_does_not_retire_a_newer_row_for_the_window_name(self):
        for change in ("pane", "reaffirm"):
            with self.subTest(change=change):
                session_tags._path().unlink(missing_ok=True)
                self.spawn("q", "--resume", NEW)
                self.record("q", NEW, "proj-8e")
                self.spawn("p", "--resume", OLD)
                self.record("p", OLD, "proj-72")
                self.fleet_pass(self.row("q", NEW), self.row("p", OLD))
                self.end("q")
                self.record("p", NEW, "proj-72")       # the census reads p resumed into NEW ...
                child = self.children["p"]

                def back():                            # ... then p goes back to OLD and reports it
                    self.record("p", OLD, "proj-72")
                    session_tags.assign_observed("claude", OLD, pid=child.pid, proc_start=sr._proc_start(child.pid),
                                                 pane_id="w:moved" if change == "pane" else None)
                with self.stale_census(back):
                    self.fleet_pass(self.row("p", NEW))
                self.assertEqual(session_tags.assigned_tag("claude", OLD), "72")
                self.assertEqual(session_tags.assigned_tag("claude", NEW), "8e")
                self.end("p")

    # --- only a record of this very process names it (review 3) ----------------------------------
    def test_a_record_without_its_process_start_does_not_take_a_number(self):
        cases = [(harness, False) for harness in ("claude", "codex", "opencode")]
        cases += [(harness, True) for harness in ("codex", "opencode")]
        for harness, confirmed in cases:
            with self.subTest(harness=harness, confirmed=confirmed):
                session_tags._path().unlink(missing_ok=True)
                self.spawn("p", "--resume", OLD)
                child = self.children["p"]
                start = sr._proc_start(child.pid)
                self.record("p", OLD, "proj-72")
                current = session_tags.assign_observed(harness, OLD, pid=child.pid, proc_start=start)
                # The file left under this PID names an older session; it omits procStart.
                body = {"pid": child.pid, "sessionId": EARLIER, "kind": "interactive",
                        "name": "proj-%s" % current, "nameSource": "derived"}
                if confirmed:
                    body["procStart"] = start
                self.path(child).write_text(json.dumps(body))
                session_tags.assign([])
                self.assertEqual(session_tags.assigned_tag(harness, OLD), current)
                self.assertNotEqual(session_tags.assigned_tag("claude", EARLIER), current)
                self.end("p")

    # --- a held conversation is named only by the window #1849 chose (review 4) ------------------
    def test_a_held_conversation_takes_no_name_from_another_process(self):
        for other_sid in (OLD, NEW):
            with self.subTest(other=other_sid):
                session_tags._path().unlink(missing_ok=True)
                self.spawn("viewer", "--resume", OLD)
                self.record("viewer", OLD, "proj-82")
                self.fleet_pass(self.row("viewer", OLD))
                self.spawn("held", "--session-id", NEW, "--fork-session", "--resume",
                           "/h/.claude/projects/p/%s.jsonl" % OLD)
                self.record("held", NEW, kind="bg", jobId=JOB, startedAt=1_700_000_500_000)
                self.record("viewer", OLD, "proj-82", parkedJobId=JOB)
                rows = [self.row("viewer", OLD), self.row("held", NEW, kind="bg")]
                collectors.apply_session_aliases(rows)
                self.assertEqual([s.session_id for s in rows], [NEW])
                self.path(self.children["viewer"]).write_text('{"pid": ')   # half written
                self.spawn("other", "--resume", other_sid)
                self.record("other", other_sid, "proj-91")
                session_tags.assign(rows)
                self.assertEqual(session_tags.assigned_tag("claude", NEW), "82")
                for key in ("viewer", "held", "other"):
                    self.end(key)
                sr._STEADY.clear()
                sr._STEADY_UNCONFIRMED.clear()

    # --- a retired earlier session hands its seat over (review 5) --------------------------------
    def test_a_retired_earlier_session_hands_its_seat_to_the_next_report(self):
        self.spawn("p", "--resume", OLD)
        self.record("p", OLD, "proj-72")
        self.agents = [{"agent": "claude", "pane_id": "w:seat", "agent_session": {"kind": "id", "value": OLD}}]
        self.fleet_pass(self.row("p", OLD))
        seat = self.rows()[("claude", OLD)]
        self.spawn("q", "--resume", NEW)
        self.record("q", NEW, "proj-8e")
        self.fleet_pass(self.row("p", OLD), self.row("q", NEW))
        self.assertEqual(session_tags.assigned_tag("claude", NEW), "8e")
        self.end("q")
        self.record("p", NEW, "proj-72")             # /resume NEW inside p
        self.agents = None                           # Herdr does not answer this pass
        self.fleet_pass(self.row("p", NEW))
        self.assertSame("p", NEW)
        moved = self.rows()[("claude", NEW)]
        self.assertEqual((moved.get("pane_id"), moved["first_seen"], moved["started_at"]),
                         (seat["pane_id"], seat["first_seen"], seat["started_at"]))
        self.assertEqual(self.address("8e"), NEW)
        self.record("p", EARLIER, "proj-72")         # the next session in the same seat reports itself
        child = self.children["p"]
        session_tags.assign_observed("claude", EARLIER, pane_id="w:seat", pid=child.pid,
                                     proc_start=sr._proc_start(child.pid))
        self.assertSame("p", EARLIER)
        self.assertIsNone(session_tags.assigned_tag("claude", NEW))
        self.assertEqual(self.address("8e"), EARLIER)


if __name__ == "__main__":
    unittest.main()
