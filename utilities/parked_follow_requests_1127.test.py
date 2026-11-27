#!/usr/bin/env python3
"""11-27 (narrow): what follows a conversation sent to the background, and what never moves.

Claude's `/bg` leaves the window's process as a viewer of the old session id (OLD) while the
conversation goes on under a new id (NEW) in another process. Only what is judged at delivery
time follows NEW, and only while both live facts agree (``session_registry.parked_links``, read
afresh at each call): a request addressed to OLD, the open request list, and the end of a watch
whose target is OLD. Nothing is written, so nothing has to come back: routes OLD started, their
completion and rewake, OLD's steward role and watches, and its message questions stay OLD's in
every state. Real child processes carry the argv and are read after they signal they are ready;
seats, ledgers, requests, markers, watches and the registry are real files under a private root.
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
spec = importlib.util.spec_from_file_location("peer_message_parked_follow_1127", HERE / "peer-message.py")
peer_message = importlib.util.module_from_spec(spec)
sys.modules["peer_message_parked_follow_1127"] = peer_message
spec.loader.exec_module(peer_message)

import dispatch_seat_handover as handover  # noqa: E402
import human_wait_state  # noqa: E402
import peer_request  # noqa: E402
import peer_steward_watch  # noqa: E402
from fleet import session_registry as sr  # noqa: E402

# Invented ids only.
OLD = "5a7c9e1b-3d4f-4a6b-8c2d-0e1f2a3b4c5d"
NEW = "9b1d3f5a-7c2e-4b8d-a4f6-1c3e5a7b9d0f"
NEW2 = "d2e4f6a8-0b1c-4d3e-9f5a-6b7c8d9e0f1a"
OTHER = "3e5a7c9b-1d2f-4e4a-b6c8-2d4f6a8b0c1e"
REQUESTER = "7f9b1d3e-5a2c-4f6e-8d0a-4b6c8e0a2c3d"
WORKER = "b4c6e8a0-2d1f-4b3c-9e5a-7f8a0c2e4b6d"
HOLD = "import os; os.write(1, b'R'); os.read(0, 1)"
JOB, JOB2 = "6d8f0b2c", "8e0a2c4d"


class ParkedFollow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pfr-")
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
        self.jobs = self.root / "jobs.log"
        self.jobs.touch()
        os.environ.update(HOME=str(self.home), CLAUDE_CONFIG_DIR=str(self.config),
                          XDG_STATE_HOME=str(self.root / "state"), AGENT_DISPATCH_JOBS=str(self.jobs),
                          AGENT_PEER_LEDGER_ROOT=str(self.root / "peer"))
        self.children = []
        self.addCleanup(self._reap)

    def _reap(self):
        for child in self.children:
            child.stdin.close()
            child.kill()
            child.wait(timeout=10)
        self.children.clear()

    # --- the processes and their records (no memory of the links anywhere to clear) -----------
    def spawn(self, *argv):
        child = subprocess.Popen([sys.executable, "-c", HOLD, *argv], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.children.append(child)
        with child.stdout:      # /proc is read only once the child says it runs
            self.assertTrue(select.select([child.stdout], [], [], 10)[0], "fixture child did not start")
            self.assertEqual(child.stdout.read(1), b"R", "fixture child exited before it was ready")
        return child

    def record(self, child, **fields):
        fields.setdefault("procStart", sr._proc_start(child.pid))
        (self.config / "sessions" / ("%d.json" % child.pid)).write_text(json.dumps({"pid": child.pid, **fields}))

    def viewer(self, job=JOB, sid=OLD):
        child = self.spawn("--resume", sid)
        self.record(child, sessionId=sid, kind="interactive", startedAt=1, **({"parkedJobId": job} if job else {}))
        return child

    def background(self, sid=NEW, job=JOB, resumes=OLD, kind="bg", record=True):
        child = self.spawn("--session-id", sid, "--fork-session", "--resume", "/p/%s.jsonl" % resumes)
        if record:
            self.record(child, sessionId=sid, kind=kind, jobId=job, startedAt=int(time.time() * 1000))
        return child

    def scene(self):
        self.window = self.viewer()
        self.bg = self.background()

    def take_back(self):
        """The window resumes its own conversation: its record holds no job."""
        self.record(self.window, sessionId=OLD, kind="interactive", startedAt=1)

    def end_background(self):
        self.bg.stdin.close()
        self.bg.kill()
        self.bg.wait(timeout=10)

    # --- what OLD owns -----------------------------------------------------------------------
    def owned(self):
        """A request to OLD, one OLD sent, a route OLD started, OLD's steward role and watch."""
        self.request_in, _entry = peer_request.open_request(
            {"harness": "claude", "session_id": REQUESTER, "name": None},
            {"harness": "claude", "session_id": OLD, "name": None}, title="review the draft")
        self.request_out, _entry = peer_request.open_request(
            {"harness": "claude", "session_id": OLD, "name": None},
            {"harness": "claude", "session_id": REQUESTER, "name": None}, title="check the numbers")
        self.meta = dict(attempt_id="att-" + hashlib.sha256(b"route-a").hexdigest()[:32], parent_sid=OLD,
                         parent_harness="claude", dispatch_depth="1", worker_type="owner",
                         route_id="rt-" + hashlib.sha256(b"route-a").hexdigest()[:16],
                         route_hash="sha256:" + "c" * 64, route_node="one-shot")
        with self.jobs.open("a") as stream:
            stream.write("now\trunning\t/w\t/w\ttask\t" + ",".join(k + "=" + v for k, v in self.meta.items()) + "\n")
        pm = handover._peer_message()
        marker = pm.steward_marker_path("claude", OLD)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"schema_version": 1, "harness": "claude", "session_id": OLD,
                                      "since": "2026-11-27T00:00:00Z", "generation": "steward-generation",
                                      "seat_key": "old-window-seat", "targets": {WORKER: {
                                          "harness": "codex", "session_id": WORKER, "source": "explicit",
                                          "kind": "explicit", "ts": "2026-11-27T00:00:00Z"}}}))
        watches = Path(pm.peer_state_root()) / "peer-watches"
        watches.mkdir(parents=True, exist_ok=True)
        (watches / "old-watch.json").write_text(json.dumps({
            "watch_id": "old-watch", "steward": {"harness": "claude", "session_id": OLD},
            "steward_seat": {"key": "old-window-seat"}, "server": "default", "target": "w2:p1",
            "agent": {"harness": "codex", "session_id": WORKER}, "until": ["done"],
            "armed_ts": "2026-11-27T00:00:00Z", "deadline": None}))

    def follow(self, sid=OLD):
        return peer_request.follow({"harness": "claude", "session_id": sid})["session_id"]

    def listed_for(self, sid):
        lists = peer_request.session_requests("claude", sid)
        return sorted(r["id"] for r in lists["in"]), sorted(r["id"] for r in lists["out"])

    def assert_owned_things_stay_with_the_window(self):
        """What carries authority never moves: route completion/rewake, steward, watch, questions."""
        self.assertEqual(handover.effective_parent(self.meta, self.jobs), OLD)
        self.assertTrue(handover.owns(self.meta, OLD, self.jobs))
        self.assertFalse(handover.owns(self.meta, NEW, self.jobs))
        self.assertEqual(handover.record_recipient_binding({"attempt_ids": [self.meta["attempt_id"]]}, OLD,
                                                           harness="claude", jobs=self.jobs), ("claude", OLD))
        self.assertTrue(handover.resolved_steward_relations("claude", OLD))
        self.assertFalse(handover.resolved_steward_relations("claude", NEW))
        self.assertEqual(handover.resolve_watch("old-watch")["holder"]["sid"], OLD)
        self.assertTrue(human_wait_state._message_incarnation_current({"harness": "claude", "session_id": OLD}))

    # --- the cases of the options note, 6 ----------------------------------------------------------
    def test_both_facts_the_requests_follow_and_nothing_else_moves(self):
        self.owned()
        self.scene()
        self.assertEqual(self.follow(), NEW)
        self.assertEqual(self.listed_for(NEW), ([self.request_in["id"]], [self.request_out["id"]]))
        self.assertEqual(self.listed_for(OLD), ([], []))
        self.assert_owned_things_stay_with_the_window()

    def test_the_window_taking_its_conversation_back_brings_the_requests_back_at_once(self):
        self.owned()
        self.scene()
        self.assertEqual(self.follow(), NEW)
        self.take_back()
        self.assertEqual(self.follow(), OLD)                      # read afresh, nothing remembered
        self.assertEqual(self.listed_for(OLD), ([self.request_in["id"]], [self.request_out["id"]]))
        self.assertEqual(self.listed_for(NEW), ([], []))
        self.assert_owned_things_stay_with_the_window()

    def test_the_background_ending_brings_the_requests_back(self):
        self.owned()
        self.scene()
        self.end_background()
        self.assertEqual(self.follow(), OLD)
        self.assert_owned_things_stay_with_the_window()

    def test_a_deliberate_fork_session_takes_nothing(self):
        self.owned()
        self.window = self.viewer(job=None)
        self.bg = self.background(kind="interactive")
        self.assertEqual(self.follow(), OLD)
        self.assert_owned_things_stay_with_the_window()

    def test_a_background_session_started_elsewhere_takes_nothing(self):
        self.owned()
        self.window = self.viewer(job=None)
        self.bg = self.background()
        self.viewer(job=JOB, sid=OTHER)                           # another window holds the job
        self.assertEqual(self.follow(), OLD)
        self.assert_owned_things_stay_with_the_window()

    def test_a_reused_process_takes_nothing(self):
        self.owned()
        self.window = self.viewer()
        self.bg = self.background(record=False)
        self.record(self.bg, sessionId=NEW, kind="bg", jobId=JOB, startedAt=1,
                    procStart=str(int(sr._proc_start(self.bg.pid)) + 7))
        self.assertEqual(self.follow(), OLD)
        self.assert_owned_things_stay_with_the_window()

    def test_a_broken_record_takes_nothing(self):
        self.owned()
        self.window = self.viewer()
        self.bg = self.background(record=False)
        (self.config / "sessions" / ("%d.json" % self.bg.pid)).write_text('{"pid": %d, "sessionId": "' % self.bg.pid)
        self.assertEqual(self.follow(), OLD)
        self.assert_owned_things_stay_with_the_window()

    def test_a_second_fork_follows_the_conversation_the_window_holds_now(self):
        self.owned()
        self.scene()
        self.assertEqual(self.follow(), NEW)
        self.take_back()
        self.record(self.window, sessionId=OLD, kind="interactive", startedAt=1, parkedJobId=JOB2)
        self.background(sid=NEW2, job=JOB2)
        self.assertEqual(self.follow(), NEW2)
        self.assertEqual(self.listed_for(NEW2)[0], [self.request_in["id"]])
        self.assertEqual(self.listed_for(NEW)[0], [])
        self.assert_owned_things_stay_with_the_window()

    # --- the delivery boundary and the watched target -----------------------------------------------
    def test_a_request_message_still_addressed_to_the_window_is_bound_to_the_background(self):
        self.owned()
        self.scene()
        row = {"to": {"harness": "claude", "session_id": OLD, "name": None}, "created": time.time(),
               "refs": ["peer-request:%s:1:request" % self.request_in["id"]]}
        self.assertEqual(peer_message.pending_delivery_binding(row)["recipient"]["session_id"], NEW)
        self.take_back()
        self.assertEqual(peer_message.pending_delivery_binding(row)["recipient"]["session_id"], OLD)

    def test_a_watch_on_the_window_ends_as_replaced_and_names_the_background(self):
        arm = {"watch_id": "watch-on-window", "agent": {"harness": "claude", "session_id": OLD},
               "steward": {"harness": "codex", "session_id": WORKER}}
        self.window = self.viewer(job=None)
        self.assertEqual(peer_steward_watch.replaced_by("claude", OLD), (OLD, "self"))
        self.assertNotEqual(peer_steward_watch._watch_close_reason(
            "watch-on-window", arm, peer_steward_watch._WatchCloseEvidence()), "session-replaced")
        self.record(self.window, sessionId=OLD, kind="interactive", startedAt=1, parkedJobId=JOB)
        self.bg = self.background()
        self.assertEqual(peer_steward_watch.replaced_by("claude", OLD), (NEW, "successor"))
        self.assertEqual(peer_steward_watch._watch_close_reason(
            "watch-on-window", arm, peer_steward_watch._WatchCloseEvidence()), "session-replaced")
        # The receipt the steward reads names the session that carries the conversation on.
        spec = importlib.util.spec_from_file_location("peer_steward_parked_follow_1127", HERE / "peer-steward.py")
        steward = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(steward)
        with mock.patch.object(steward, "_prepare_watch_notice"):
            receipt = steward._write_terminal_watch_receipt("watch-on-window", arm, "session-replaced")
        self.assertEqual((receipt["state"], receipt.get("successor_session_id")), ("session-replaced", NEW))

    def test_one_listing_reads_the_links_once_however_many_requests(self):
        self.owned()
        for title in ("one more", "and another"):
            peer_request.open_request({"harness": "claude", "session_id": REQUESTER, "name": None},
                                      {"harness": "claude", "session_id": OLD, "name": None}, title=title)
        self.scene()
        with mock.patch.object(sr, "_live_claude_records", wraps=sr._live_claude_records) as parsed:
            received, _sent = self.listed_for(NEW)
        self.assertEqual((len(received), parsed.call_count), (3, 1))

    def test_without_any_held_job_no_process_record_is_parsed(self):
        # Every other session (all of Codex and OpenCode, and Claude without /bg) costs one byte
        # scan of the record folder and reads exactly as before.
        self.window = self.viewer(job=None)
        with mock.patch.object(sr, "_live_claude_records", wraps=sr._live_claude_records) as parsed:
            self.assertEqual(self.follow(), OLD)
            self.assertEqual(peer_request.follow({"harness": "codex", "session_id": OLD})["session_id"], OLD)
            self.assertEqual(peer_steward_watch.replaced_by("claude", OLD), (OLD, "self"))
        self.assertEqual(parsed.call_count, 0)


if __name__ == "__main__":
    unittest.main()
