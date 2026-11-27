#!/usr/bin/env python3
"""11-27: when a window's conversation goes to the background, what the old id owned goes with it.

Claude's `/bg` leaves the window's process as a viewer of the old session id (OLD) and goes on
under a new id (NEW) in another process. NEW's own hook writes one ``parked-fork`` transition row
when both live facts agree (the viewer's record holds the job, the background process resumes
OLD); requests, route completion and rewake, the steward marker and watch, the watched target and
a route question's owner then follow it. Every doubt writes nothing, and records addressed to OLD
follow the row only while that same link lives. Real child processes carry the argv (read after
each one signals it is ready); seats, ledgers and records are real, under a private root.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import hashlib
import importlib.util
import json
import select
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "tools")]
spec = importlib.util.spec_from_file_location("peer_message_parked_fork_1127", HERE / "peer-message.py")
peer_message = importlib.util.module_from_spec(spec)
sys.modules["peer_message_parked_fork_1127"] = peer_message
spec.loader.exec_module(peer_message)

import dispatch_seat_handover as handover  # noqa: E402
import peer_request  # noqa: E402
import session_tidy as tidy  # noqa: E402
from fleet import session_registry as sr  # noqa: E402

# Invented ids only.
OLD = "4b7d2e10-6a3c-4f58-9e21-0c8d5a7f3b64"
NEW = "8f1e6c42-2d9b-4a07-b5c3-71e0f4a9d826"
NEW2 = "c3a9e5d1-7b42-4e6f-8a10-5d2c9f7e4b38"
OTHER = "1d6f0b3e-9c27-4a85-b4e1-6e3a8d0c5f92"
WORKER = "a2c4e6f8-1b3d-4f5a-8c7e-9d0b2f4a6c81"
HOLD = "import os; os.write(1, b'R'); os.read(0, 1)"
JOB, JOB2 = "5e3a7c1d", "9b2f4d6e"
OLD_SEAT = tidy.Seat("pane", "fixture-old-pane-seat", pane="w1:p1", harness="claude")
NEW_SEAT = tidy.Seat("native", "fixture-new-native-seat", harness="claude")
NEW2_SEAT = tidy.Seat("native", "fixture-new2-native-seat", harness="claude")


class ParkedForkOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pfo-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in tuple(os.environ):
            if key.startswith(("AGENT_", "HERDR_", "OPENCODE_", "CLAUDE_", "CODEX_", "XDG_", "HARNESS_")):
                os.environ.pop(key, None)
        self.home = self.root / "home"
        self.config = self.home / ".claude"
        (self.config / "sessions").mkdir(parents=True)
        self.jobs = self.root / "dispatch" / "jobs.log"
        self.jobs.parent.mkdir()
        self.jobs.touch()
        os.environ.update(HOME=str(self.home), CLAUDE_CONFIG_DIR=str(self.config),
                          XDG_STATE_HOME=str(self.root / "state"), AGENT_DISPATCH_JOBS=str(self.jobs),
                          AGENT_PEER_LEDGER_ROOT=str(self.root / "peer"))
        self.children = []
        self.addCleanup(self._reap)
        self._forget()

    def _forget(self):
        handover._LINEAGE_GRAPH_CACHE.clear()
        handover._PARKED_LINKS.clear()
        peer_message._PARKED_MEMO.clear()

    def _reap(self):
        for child in self.children:
            child.stdin.close()
            child.kill()
            child.wait(timeout=10)
        self.children.clear()
        self._forget()

    # --- the processes and their records ------------------------------------------------
    def spawn(self, *argv):
        child = subprocess.Popen([sys.executable, "-c", HOLD, *argv],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.children.append(child)
        # A returned PID can precede readable resume arguments in procfs.
        with child.stdout:
            self.assertTrue(select.select([child.stdout], [], [], 10)[0], "fixture child did not start")
            self.assertEqual(child.stdout.read(1), b"R", "fixture child exited before it was ready")
        return child

    def record(self, child, **fields):
        fields.setdefault("procStart", sr._proc_start(child.pid))
        (self.config / "sessions" / ("%d.json" % child.pid)).write_text(json.dumps(
            {"pid": child.pid, **fields}))
        self._forget()

    def viewer(self, job=JOB, sid=OLD):
        child = self.spawn("--resume", sid)
        self.record(child, sessionId=sid, kind="interactive", startedAt=1,
                    **({"parkedJobId": job} if job else {}))
        return child

    def background(self, sid=NEW, job=JOB, resumes=OLD, kind="bg", record=True):
        child = self.spawn("--session-id", sid, "--fork-session", "--resume", "/p/%s.jsonl" % resumes)
        if record:
            self.record(child, sessionId=sid, kind=kind, jobId=job, startedAt=int((time.time() - 60) * 1000))
        return child

    def scene(self):
        self.window = self.viewer()
        self.bg = self.background()

    # --- seats and hooks -----------------------------------------------------------------
    def hook(self, seat, sid, event="start", source="fork", now=None):
        """What the session hook does for ``sid`` at ``seat``: its own row, then the parked writer
        with the seat locks held, then the same native-ledger copy."""
        now = time.time() if now is None else now
        logical = tidy.session_seat(seat, "claude", sid)
        with tidy._session_state_locks([logical]):
            if event == "prompt":
                tidy.bump_prompt_seq(logical, "claude", sid, now)
            tidy.record_event(logical, "claude", sid, event, source=source if event == "start" else "", now=now)
            row = handover.record_parked_fork(
                logical, "claude", sid, event, source, now,
                held_keys={tidy.seat_state_key(logical), logical.key})
            if row:
                tidy._append_ledger_entry(tidy.physical_seat(logical), row)
        self._forget()
        return row

    def parked_rows(self):
        found = {}
        for path in (tidy.state_root() / "sessions").glob("*.jsonl"):
            for line in path.read_text().splitlines():
                row = json.loads(line)
                if row.get("source") == handover.PARKED_FORK_SOURCE:
                    found.setdefault(row["snapshot_id"], row)
        return list(found.values())

    # --- what OLD owns before the split ------------------------------------------------
    def owned_route(self, tag="held-route"):
        meta = dict(attempt_id="att-" + hashlib.sha256(tag.encode()).hexdigest()[:32],
                    parent_sid=OLD, parent_harness="claude", dispatch_depth="1", worker_type="owner",
                    route_id="rt-" + hashlib.sha256(tag.encode()).hexdigest()[:16],
                    route_hash="sha256:" + "c" * 64, route_node="one-shot")
        with self.jobs.open("a") as stream:
            stream.write("now\trunning\t/w\t/w\ttask\t" + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        return meta

    def steward_role_and_watch(self):
        pm = handover._peer_message()
        marker = pm.steward_marker_path("claude", OLD)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "schema_version": 1, "harness": "claude", "session_id": OLD, "since": "2026-11-27T00:00:00Z",
            "generation": "fixture-generation", "seat_key": OLD_SEAT.key,
            "targets": {WORKER: {"harness": "codex", "session_id": WORKER, "source": "explicit",
                                 "kind": "explicit", "ts": "2026-11-27T00:00:00Z"}}}))
        watches = Path(pm.peer_state_root()) / "peer-watches"
        watches.mkdir(parents=True, exist_ok=True)
        (watches / "fixture-watch.json").write_text(json.dumps({
            "watch_id": "fixture-watch", "steward": {"harness": "claude", "session_id": OLD},
            "steward_seat": {"key": OLD_SEAT.key}, "server": "default", "target": "w2:p1",
            "agent": {"harness": "codex", "session_id": WORKER}, "until": ["done"],
            "armed_ts": "2026-11-27T00:00:00Z", "deadline": None}))
        return "fixture-watch"

    def follow(self, sid):
        return peer_request.follow({"harness": "claude", "session_id": sid})["session_id"]

    def chain(self, sid):
        return handover._chain_end(handover.lineage_graph(), "claude", sid)

    def message_to(self, sid, created):
        return peer_message.pending_delivery_binding(
            {"to": {"harness": "claude", "session_id": sid, "name": None}, "created": created,
             "refs": []})["recipient"]["session_id"]

    # --- (가) both facts: one edge, and what OLD owned follows -------------------------------
    def test_both_facts_write_one_edge_and_every_owned_thing_follows(self):
        meta = self.owned_route()
        watch = self.steward_role_and_watch()
        self.assertEqual(handover.effective_parent(meta, self.jobs), OLD)
        self.assertTrue(handover.resolved_steward_relations("claude", OLD))
        self.scene()
        row = self.hook(NEW_SEAT, NEW)
        self.assertIsNotNone(row)
        self.assertEqual((row["from"], row["sid"], row["source"]), (OLD, NEW, "parked-fork"))
        self.assertEqual(len(self.parked_rows()), 1)
        # A request addressed to OLD (peer_request.follow) and the watched target (`_chain_end`).
        self.assertEqual(self.follow(OLD), NEW)
        self.assertEqual(self.chain(OLD), (NEW, "successor"))
        # Route completion recipient, rewake/owner authority, a route question's owner.
        self.assertEqual(handover.effective_parent(meta, self.jobs), NEW)
        self.assertTrue(handover.owns(meta, NEW, self.jobs))
        self.assertFalse(handover.owns(meta, OLD, self.jobs))
        self.assertEqual(handover.resolve_recipient("job-holder", metadata=meta, jobs=self.jobs).state, "confirmed")
        self.assertIn(OLD, dict(handover.storage_recipients(NEW, harness="claude")))
        # The steward marker and its watch.
        self.assertTrue(handover.resolved_steward_relations("claude", NEW))
        self.assertFalse(handover.resolved_steward_relations("claude", OLD))
        self.assertEqual(handover.resolve_watch(watch)["holder"]["sid"], NEW)
        self.assertEqual(handover.watch_delivery_binding([watch])["recipient"]["session_id"], NEW)
        # The old id's requests are listed by NEW.
        self.assertIn(OLD, peer_request._predecessors("claude", NEW))

    def test_the_same_hook_twice_writes_one_edge(self):
        self.scene()
        first = self.hook(NEW_SEAT, NEW)
        second = self.hook(NEW_SEAT, NEW, event="prompt")
        third = self.hook(NEW_SEAT, NEW)
        self.assertEqual(len(self.parked_rows()), 1)
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["snapshot_id"], third["snapshot_id"])

    def test_no_background_record_at_the_first_hook_is_read_again_at_the_next(self):
        self.window = self.viewer()
        self.bg = self.background(record=False)
        self.assertIsNone(self.hook(NEW_SEAT, NEW))
        self.assertEqual(self.follow(OLD), OLD)
        self.record(self.bg, sessionId=NEW, kind="bg", jobId=JOB, startedAt=int(time.time() * 1000))
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW, event="prompt"))
        self.assertEqual(self.follow(OLD), NEW)

    def test_the_look_again_is_the_first_prompt_only(self):
        self.window = self.viewer()
        self.bg = self.background(record=False)
        self.assertIsNone(self.hook(NEW_SEAT, NEW))
        self.assertIsNone(self.hook(NEW_SEAT, NEW, event="prompt"))
        self.record(self.bg, sessionId=NEW, kind="bg", jobId=JOB, startedAt=int(time.time() * 1000))
        self.assertIsNone(self.hook(NEW_SEAT, NEW, event="prompt"))   # a later prompt is not the split
        self.assertEqual(self.parked_rows(), [])

    # --- one fact only, or doubt: nothing moves ---------------------------------------------
    def test_a_deliberate_fork_session_writes_nothing(self):
        self.window = self.viewer(job=None)
        self.bg = self.background(kind="interactive")
        self.assertIsNone(self.hook(NEW_SEAT, NEW))
        self.assertEqual((self.parked_rows(), self.follow(OLD)), ([], OLD))

    def test_a_background_session_started_elsewhere_writes_nothing(self):
        self.window = self.viewer(job=None)
        self.bg = self.background()
        self.assertIsNone(self.hook(NEW_SEAT, NEW))
        self.window2 = self.viewer(job=JOB, sid=OTHER)      # another window holds the job: argv says OLD
        self.assertIsNone(self.hook(NEW_SEAT, NEW))
        self.assertEqual((self.parked_rows(), self.follow(OLD)), ([], OLD))

    def test_an_original_that_already_has_a_successor_writes_nothing(self):
        with tidy.seat_lock(OLD_SEAT.key):
            tidy._append_ledger_entry(OLD_SEAT, {"event": "succession", "source": "tidy-clear", "harness": "claude",
                                                 "sid": OTHER, "from": {"harness": "claude", "sid": OLD}, "ts": 2})
        self._forget()
        self.scene()
        self.assertIsNone(self.hook(NEW_SEAT, NEW))
        self.assertEqual((self.parked_rows(), self.follow(OLD)), ([], OTHER))

    def test_a_reused_process_writes_nothing(self):
        self.window = self.viewer()
        self.bg = self.background(record=False)
        self.record(self.bg, sessionId=NEW, kind="bg", jobId=JOB, startedAt=1,
                    procStart=str(int(sr._proc_start(self.bg.pid)) + 7))
        self.assertIsNone(self.hook(NEW_SEAT, NEW))
        self.assertEqual(self.parked_rows(), [])

    def test_the_link_is_read_again_just_before_the_write(self):
        self.scene()
        real = handover.current_bindings

        def window_takes_it_back(*args, **kwargs):
            # Between the first read and the write, the window resumes its own conversation.
            self.record(self.window, sessionId=OLD, kind="interactive", startedAt=1)
            return real(*args, **kwargs)

        with mock.patch.object(handover, "current_bindings", side_effect=window_takes_it_back):
            self.assertIsNone(self.hook(NEW_SEAT, NEW))
        self.assertEqual((self.parked_rows(), self.follow(OLD)), ([], OLD))

    def test_a_broken_record_writes_nothing(self):
        self.window = self.viewer()
        self.bg = self.background(record=False)
        (self.config / "sessions" / ("%d.json" % self.bg.pid)).write_text('{"pid": %d, "sessionId": "' % self.bg.pid)
        self.assertIsNone(self.hook(NEW_SEAT, NEW))
        self.assertEqual(self.parked_rows(), [])

    # --- after the edge -------------------------------------------------------------------
    def test_a_second_fork_writes_no_second_edge_and_the_old_id_is_unresolved(self):
        meta = self.owned_route()
        self.scene()
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW))
        # The window took its conversation back and sent it away again: a new job, a new id.
        self.record(self.window, sessionId=OLD, kind="interactive", startedAt=1, parkedJobId=JOB2)
        self.bg2 = self.background(sid=NEW2, job=JOB2)
        self.assertIsNone(self.hook(NEW2_SEAT, NEW2))
        self.assertEqual(len(self.parked_rows()), 1)
        self.assertEqual(handover.resolve_recipient("seat-successor", session=OLD, harness="claude").state,
                         "unreadable")
        self.assertEqual(self.chain(OLD)[1], "unresolved")
        self.assertEqual(self.follow(OLD), OLD)                  # neither arm takes a new request
        self.assertEqual(handover.effective_parent(meta, self.jobs), NEW)   # what moved stays moved

    def test_after_the_link_ends_new_mail_to_the_old_id_stays_with_it(self):
        meta = self.owned_route()
        self.scene()
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW))
        self.assertEqual(self.message_to(OLD, time.time()), NEW)
        # The window resumes its own conversation: its record holds no job anymore.
        self.record(self.window, sessionId=OLD, kind="interactive", startedAt=1)
        self.assertEqual(self.message_to(OLD, time.time()), OLD)
        self.assertEqual(self.follow(OLD), OLD)
        self.assertEqual(self.chain(OLD), (OLD, "self"))
        self.assertEqual(handover.effective_parent(meta, self.jobs), NEW)   # bound at the split
        later = self.owned_route("route-after-the-link-ended")
        self.assertEqual(handover.effective_parent(later, self.jobs), OLD)

    def test_the_background_process_ending_ends_the_link(self):
        self.scene()
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW))
        self.bg.stdin.close()
        self.bg.kill()
        self.bg.wait(timeout=10)
        self._forget()
        self.assertEqual(self.follow(OLD), OLD)

    # --- the installed hook entry ---------------------------------------------------------
    def test_the_session_hook_writes_the_edge_at_a_fork_start(self):
        self.scene()
        env = dict(os.environ)
        with mock.patch.object(tidy, "resolve_seat", return_value=NEW_SEAT):
            tidy.run_hook("claude", "start", NEW, source="fork", env=env, render_context=False)
            self._forget()
            tidy.run_hook("claude", "start", NEW, source="startup", env=env, render_context=False)
        self._forget()
        self.assertEqual([(row["from"], row["sid"]) for row in self.parked_rows()], [(OLD, NEW)])
        self.assertEqual(self.follow(OLD), NEW)
        # No card or seat number is owed to a fork that carries the whole conversation.
        self.assertIsNone(tidy.confirmed_predecessor_identity(NEW_SEAT, "claude", NEW))


if __name__ == "__main__":
    unittest.main()
