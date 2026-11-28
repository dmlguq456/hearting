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

import fcntl
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
import session_tidy  # noqa: E402

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

    def fresh(self):
        """A new fixture for one subtest, whatever the previous one left: no child, record or number."""
        self._reap()
        session_tags._path().unlink(missing_ok=True)
        sr._STEADY.clear()
        sr._STEADY_UNCONFIRMED.clear()
        self.agents = []

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
            body["name"] = name
            body.setdefault("nameSource", "derived")
            self.names[key] = name
        self.path(child).write_text(json.dumps(body))

    def row(self, key, sid, kind="interactive", *, harness="claude"):
        child = self.children[key]
        return Session(harness=harness, pid=child.pid, session_id=sid, kind=kind,
                       proc_start=sr._proc_start(child.pid))

    def bind(self, key, sid, *, harness="claude", busy=False):
        """Publish a birth-checked attachment, optionally while its tag report cannot write."""
        host_key = key + "-host"
        if host_key not in self.children:
            self.spawn(host_key)
        host, native = self.children[host_key].pid, self.children[key].pid
        value = {"schema": session_shell.SCHEMA, "harness": harness, "sid": sid,
                 "host_pid": host, "host_start": sr._proc_start(host),
                 "native_pid": native, "native_start": sr._proc_start(native),
                 "broker_pid": os.getpid(), "broker_start": sr._proc_start(os.getpid()),
                 "incarnation": "a" * 32, "socket": str(self.root / "unused.sock")}
        if busy:
            with session_tags._path().with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(session_shell.publish_binding(value))
        else:
            self.assertTrue(session_shell.publish_binding(value))
        self.assertEqual(session_shell.exact_binding(harness, sid), (value, ""))

    def attached_addresses(self, tag):
        peer, _ = peer_address.resolve(f"[{tag}]")
        shell, _ = session_shell.resolve_target(f"[{tag}]")
        return peer and peer["session_id"], shell and shell["sid"]

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

    def codex(self, tag):
        """A live Codex session whose id hashes to ``tag``."""
        sid = next("synthetic-live-%s-%d" % (tag, n) for n in range(4096)
                   if minted_tag("synthetic-live-%s-%d" % (tag, n)) == tag)
        self.spawn("codex-" + tag)
        child = self.children["codex-" + tag]
        return Session(harness="codex", pid=child.pid, session_id=sid, proc_start=sr._proc_start(child.pid))

    def stored(self, *rows):
        """Rewrite the snapshot as another writer left it."""
        session_tags._path().parent.mkdir(parents=True, exist_ok=True)
        session_tags._path().write_text(json.dumps({"version": 1, "assignments": list(rows)}))

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
                self.fresh()
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

    # --- only a record of this very process names it (review 3) ----------------------------------
    def test_a_record_without_its_process_start_does_not_take_a_number(self):
        cases = [(harness, False) for harness in ("claude", "codex", "opencode")]
        cases += [(harness, True) for harness in ("codex", "opencode")]
        for harness, confirmed in cases:
            with self.subTest(harness=harness, confirmed=confirmed):
                self.fresh()
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

    # --- a held conversation is named only by the window #1849 chose (review 4) ------------------
    def test_a_held_conversation_takes_no_name_from_another_process(self):
        for other_sid in (OLD, NEW):
            with self.subTest(other=other_sid):
                self.fresh()
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

    # --- review 2 (11-28): the seat, the process evidence, the owner of a number -----------------
    def test_a_late_census_does_not_hand_a_newer_row_to_the_seat_successor(self):
        for successor_stored, pane in ((True, "w:seat"), (False, "w:seat"),
                                       (True, "w:prior"), (False, "w:prior")):
            with self.subTest(successor_stored=successor_stored, pane=pane):
                self.fresh()
                self.resumed_on_a_new_name()
                self.agents = [{"agent": "claude", "pane_id": "w:prior", "agent_session": {"kind": "id", "value": OLD}}]
                self.fleet_pass(self.row("w", OLD))  # OLD = [72], [8e] held, at w:prior
                own = None
                if successor_stored:
                    self.spawn("q", "--resume", NEW)
                    self.record("q", NEW, "proj-33")
                    self.fleet_pass(self.row("w", OLD), self.row("q", NEW))
                    own = session_tags.assigned_tag("claude", NEW)
                    self.assertNotIn(own, (None, "72", "8e"))
                    self.end("q")
                self.record("w", NEW, "proj-72")     # the census reads w resumed into NEW ...
                self.agents = [{"agent": "claude", "pane_id": pane, "agent_session": {"kind": "id", "value": NEW}}]
                child = self.children["w"]

                def back():                          # ... then w goes back to OLD and reports it there
                    self.record("w", OLD, "proj-72")
                    session_tags.assign_observed("claude", OLD, pane_id=pane, pid=child.pid,
                                                 proc_start=sr._proc_start(child.pid))
                with self.stale_census(back):
                    self.fleet_pass(self.row("w", NEW))
                kept = self.rows()[("claude", OLD)]
                self.assertEqual((kept["tag"], kept.get("held"), kept.get("pane_id")), ("72", ["8e"], pane))
                self.assertEqual(session_tags.assigned_tag("claude", NEW), own)
                self.assertEqual((self.address("72"), self.address("8e")), (OLD, OLD))

    def test_an_unrelated_reaffirmation_does_not_interrupt_seat_continuation(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                self.fresh()
                self.spawn("p")
                old = next(f"fixture-seat-{i}" for i in range(4096) if minted_tag(f"fixture-seat-{i}") == "ab")
                p = self.row("p", old, harness=harness)
                self.assertEqual(session_tags.assign_observed(
                    harness, old, pane_id="w:seat", pid=p.pid, proc_start=p.proc_start), "ab")
                self.bind("p", NEW, harness=harness, busy=True)
                self.assertNotIn((harness, NEW), self.rows())
                self.agents = [{"agent": harness, "pane_id": "w:seat",
                                "agent_session": {"kind": "id", "value": NEW}}]
                x = self.codex("4d")

                def report_x():
                    session_tags.assign_observed("codex", x.session_id, pane_id="w:other",
                                                 pid=x.pid, proc_start=x.proc_start)

                def reaffirm_x():
                    path = session_tags._path()
                    before = self.rows()
                    generation = path.stat()
                    report_x()
                    written = self.rows()
                    self.assertEqual({key: row for key, row in written.items() if key != ("codex", x.session_id)},
                                     {key: row for key, row in before.items() if key != ("codex", x.session_id)})
                    for rows in (before, written):
                        rows[("codex", x.session_id)].pop("observation_revision", None)
                    self.assertEqual(written, before)
                    self.assertNotEqual(path.stat().st_ctime_ns, generation.st_ctime_ns)

                report_x()
                with self.stale_census(reaffirm_x):
                    self.fleet_pass(self.row("p", NEW, harness=harness))
                self.assertEqual(session_tags.assigned_tag(harness, NEW), "ab")
                self.assertEqual(self.attached_addresses("ab"), (NEW, NEW))

                seat = session_tidy.Seat("pane", "fixture-seat", pane="w:seat")
                session_tidy._append_ledger_entry(seat, {"event": "succession", "source": "tidy-clear",
                                                        "harness": harness, "sid": EARLIER, "from": NEW, "ts": 50})
                session_tidy._continue_seat_number(seat, harness, EARLIER)
                self.bind("p", EARLIER, harness=harness)
                self.assertNotIn((harness, NEW), self.rows())
                self.assertEqual(session_tags.assigned_tag(harness, EARLIER), "ab")
                self.assertEqual(self.attached_addresses("ab"), (EARLIER, EARLIER))

    def test_a_start_time_from_another_record_does_not_retire_a_live_session(self):
        self.spawn("q", "--resume", OLD)
        self.record("q", OLD, "proj-72")
        self.fleet_pass(self.row("q", OLD))
        self.assertSame("q", OLD)
        self.spawn("p")
        p = self.children["p"]
        self.path(self.children["q"]).write_text('{"pid": ')   # q's record is unreadable for a while
        # a file under the reused PID p still names OLD and omits procStart
        self.path(p).write_text(json.dumps({"pid": p.pid, "sessionId": OLD, "kind": "interactive"}))
        session_tags.assign([])                      # OLD's row takes p's PID and start time
        self.assertEqual(self.rows()[("claude", OLD)]["pid"], p.pid)
        self.record("p", NEW, "proj-72")             # p now registers NEW, with its start time
        session_tags.assign([])
        self.assertEqual(session_tags.assigned_tag("claude", OLD), "72")
        self.assertNotEqual(session_tags.assigned_tag("claude", NEW), "72")
        self.assertEqual(self.address("72"), OLD)

    def test_a_name_from_one_process_does_not_retire_a_session_of_another(self):
        self.spawn("p2", "--resume", OLD)
        self.record("p2", OLD, "proj-72")
        self.fleet_pass(self.row("p2", OLD))
        self.assertSame("p2", OLD)
        self.spawn("p1", "--resume", NEW)
        self.record("p1", NEW, "proj-72")            # p1 names NEW by the list's derived name ...
        self.record("p2", NEW)                       # ... while p2 resumed NEW too, under no derived name
        first = "%d.json" % self.children["p1"].pid
        glob = Path.glob
        with mock.patch.object(Path, "glob", lambda path, pattern: sorted(glob(path, pattern),
                                                                          key=lambda f: f.name != first)):
            session_tags.assign([])                  # p1's record is read first, p2's last
        self.assertEqual(session_tags.assigned_tag("claude", OLD), "72")
        self.assertNotEqual(session_tags.assigned_tag("claude", NEW), "72")
        self.assertEqual(self.address("72"), OLD)

    def test_duplicate_processes_cannot_retire_a_predecessor_by_name(self):
        for other_first in (True, False):
            for evidence in ("user-with-binding", "user", "unnamed"):
                with self.subTest(other_first=other_first, evidence=evidence):
                    self.fresh()
                    self.spawn("p1", "--resume", OLD)
                    self.record("p1", OLD, "proj-72")
                    self.fleet_pass(self.row("p1", OLD))
                    if evidence == "user-with-binding":
                        self.spawn("q", "--resume", OLD)
                        self.bind("q", OLD, busy=True)
                        self.path(self.children["q"]).write_text('{"pid": ')
                    self.assertEqual(self.rows()[("claude", OLD)]["pid"], self.children["p1"].pid)
                    self.record("p1", NEW, "proj-72")
                    self.spawn("p2", "--resume", NEW)
                    if evidence == "unnamed":
                        self.record("p2", NEW)
                    else:
                        self.record("p2", NEW, "chosen-91", nameSource="user")
                    first = self.path(self.children["p2" if other_first else "p1"])
                    glob = Path.glob
                    with mock.patch.object(Path, "glob", lambda path, pattern: sorted(
                            glob(path, pattern), key=lambda entry: entry != first)):
                        session_tags.assign([])      # only native evidence, as in a harness-filtered collector
                    self.assertEqual(session_tags.assigned_tag("claude", OLD), "72")
                    self.assertNotEqual(session_tags.assigned_tag("claude", NEW), "72")
                    if evidence == "user-with-binding":
                        self.assertEqual(self.attached_addresses("72"), (OLD, OLD))

    def test_a_live_binding_preserves_the_predecessor_even_with_one_named_process(self):
        self.fresh()
        self.spawn("p", "--resume", OLD)
        self.record("p", OLD, "proj-72")
        self.fleet_pass(self.row("p", OLD))
        self.spawn("q", "--resume", OLD)
        self.bind("q", OLD, busy=True)
        self.path(self.children["q"]).write_text('{"pid": ')
        self.assertEqual(self.rows()[("claude", OLD)]["pid"], self.children["p"].pid)
        self.record("p", NEW, "proj-72")
        session_tags.assign([])
        self.assertEqual(session_tags.assigned_tag("claude", OLD), "72")
        self.assertNotEqual(session_tags.assigned_tag("claude", NEW), "72")
        self.assertEqual(self.attached_addresses("72"), (OLD, OLD))

    def test_a_held_number_taken_back_keeps_the_earlier_number_too(self):
        self.resumed_on_a_new_name()
        self.fleet_pass(self.row("w", OLD))          # OLD = [72], [8e] held
        self.end("w")
        self.spawn("p", "--resume", OLD)             # a new process named proj-8e reports OLD ...
        self.record("p", OLD, "proj-8e")
        self.observe("p", OLD)
        self.record("p", NEW, "proj-8e")             # ... and resumes NEW in place before a full pass
        codex = self.codex("72")
        self.fleet_pass(self.row("p", NEW), codex)
        self.assertSame("p", NEW)
        self.assertEqual((self.address("72"), self.address("8e")), (NEW, NEW))
        self.assertNotIn(session_tags.assigned_tag("codex", codex.session_id), (None, "72", "8e"))

    def test_a_legacy_duplicate_number_has_one_owner_after_one_pass(self):
        for names in (("proj-72", "proj-8e"), ("proj-8e", "proj-72")):
            with self.subTest(names=names):
                self.fresh()
                rows = []
                for n, (key, sid, name) in enumerate((("a", OLD, names[0]), ("b", NEW, names[1]))):
                    self.spawn(key, "--resume", sid)
                    self.record(key, sid, name)
                    child = self.children[key]
                    rows.append({"harness": "claude", "session_id": sid, "tag": "8e", "first_seen": 1000 + n,
                                 "started_at": 1000 + n, "pid": child.pid, "proc_start": sr._proc_start(child.pid)})
                self.stored(*rows)                   # an older writer left both on [8e]
                self.fleet_pass(self.row("a", OLD), self.row("b", NEW))
                self.assertEqual(self.address("8e"), OLD)
                tags = {session_tags.assigned_tag("claude", sid) for sid in (OLD, NEW)}
                self.assertEqual(len(tags - {None}), 2)

    # --- accepted limit (user, 11-28 17:03): an earlier-version writer right after an install ------
    def test_accepted_limit_an_earlier_version_census_drops_the_moved_off_number(self):
        self.resumed_on_a_new_name()
        self.fleet_pass(self.row("w", OLD))
        self.assertEqual(self.address("8e"), OLD)
        # An earlier version's full census rewrites the observed row without `held`.
        self.stored(*({k: v for k, v in row.items() if k not in ("held", "proc_confirmed")}
                      for row in self.rows().values()))
        self.assertEqual(self.address("8e"), None)
        codex = self.codex("8e")
        self.assertEqual(session_tags.assign_observed("codex", codex.session_id), "8e")
        self.assertEqual(self.address("8e"), codex.session_id)   # the remembered [8e] reaches it
        self.assertSame("w", OLD)

    def test_accepted_limit_an_earlier_version_report_overlaps_the_moved_off_number(self):
        self.resumed_on_a_new_name()
        self.fleet_pass(self.row("w", OLD))
        codex = self.codex("8e")
        # An earlier version's native report draws [8e] for a new session: it keeps `held` but ignores it.
        self.stored(*self.rows().values(), {"harness": "codex", "session_id": codex.session_id, "tag": "8e",
                                            "first_seen": 9e9, "started_at": 9e9})
        self.assertEqual(self.address("8e"), None)   # fleet-tag-ambiguous: neither one receives it
        self.fleet_pass(self.row("w", OLD), codex)
        self.assertEqual(self.address("8e"), None)
        self.assertSame("w", OLD)


if __name__ == "__main__":
    unittest.main()
