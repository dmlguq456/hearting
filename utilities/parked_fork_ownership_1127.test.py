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
import contextlib
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
        # Only the peer-message row memo (M4, not this change) is reset between steps; the
        # lineage reader keeps no memory of the live links, so nothing else is cleared.
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
            row = handover.record_parked_fork(logical, "claude", sid, event, source, now)
        self._forget()
        return row

    def parked_rows(self):
        found = {}
        for path in (tidy.state_root() / "sessions").glob("*.jsonl"):
            for line in path.read_text().splitlines():
                row = json.loads(line)
                if row.get("event") == handover.PARKED_FORK_EVENT:
                    found.setdefault((row["from"], row["sid"], json.dumps(row["parked"], sort_keys=True)), row)
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

    def owned_route_in(self, registry, tag):
        registry.parent.mkdir(parents=True, exist_ok=True)
        meta = dict(attempt_id="att-" + hashlib.sha256(tag.encode()).hexdigest()[:32],
                    parent_sid=OLD, parent_harness="claude", dispatch_depth="1", worker_type="owner",
                    route_id="rt-" + hashlib.sha256(tag.encode()).hexdigest()[:16],
                    route_hash="sha256:" + "d" * 64, route_node="one-shot")
        with registry.open("a") as stream:
            stream.write("now\trunning\t/w\t/w\ttask\t" + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        return meta

    def run_hook(self, seat, sid, event, source=""):
        with mock.patch.object(tidy, "resolve_seat", return_value=seat):
            tidy.run_hook("claude", event, sid, source=source, env=dict(os.environ), render_context=False)
        self._forget()

    def steward_role_and_watch(self, seat=True):
        pm = handover._peer_message()
        marker = pm.steward_marker_path("claude", OLD)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "schema_version": 1, "harness": "claude", "session_id": OLD, "since": "2026-11-27T00:00:00Z",
            "generation": "fixture-generation", **({"seat_key": OLD_SEAT.key} if seat else {}),
            "targets": {WORKER: {"harness": "codex", "session_id": WORKER, "source": "explicit",
                                 "kind": "explicit", "ts": "2026-11-27T00:00:00Z"}}}))
        watches = Path(pm.peer_state_root()) / "peer-watches"
        watches.mkdir(parents=True, exist_ok=True)
        (watches / "fixture-watch.json").write_text(json.dumps({
            "watch_id": "fixture-watch", "steward": {"harness": "claude", "session_id": OLD},
            **({"steward_seat": {"key": OLD_SEAT.key}} if seat else {}), "server": "default", "target": "w2:p1",
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
        self.assertEqual((row["from"], row["sid"], row["event"]), (OLD, NEW, "parked-fork"))
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
        self.assertEqual(first, second)
        self.assertEqual(first, third)

    def test_no_background_record_at_the_first_hook_is_read_again_at_the_next(self):
        self.window = self.viewer()
        self.bg = self.background(record=False)
        self.assertIsNone(self.hook(NEW_SEAT, NEW))
        self.assertEqual(self.follow(OLD), OLD)
        self.record(self.bg, sessionId=NEW, kind="bg", jobId=JOB, startedAt=int(time.time() * 1000))
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW, event="prompt"))
        self.assertEqual(self.follow(OLD), NEW)

    def test_a_record_that_comes_late_is_read_again_at_a_later_prompt(self):
        # 🔴8: the background record appears only after the first prompt.
        self.window = self.viewer()
        self.bg = self.background(record=False)
        self.assertIsNone(self.hook(NEW_SEAT, NEW))
        self.assertIsNone(self.hook(NEW_SEAT, NEW, event="prompt"))
        self.record(self.bg, sessionId=NEW, kind="bg", jobId=JOB, startedAt=int((time.time() - 60) * 1000))
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW, event="prompt"))
        self.assertEqual(self.follow(OLD), NEW)

    def test_a_background_session_the_window_selects_later_is_not_the_split(self):
        # 🔴8 guard: X began (from the same conversation, elsewhere) while the window's own
        # session was still in use; the window holding X's job later is not X's split.
        self.window = self.viewer(job=None)
        self.bg = self.background(sid=NEW, job=JOB2)
        with tidy.seat_lock(OLD_SEAT.key):
            tidy.record_event(OLD_SEAT, "claude", OLD, "prompt", now=time.time())
        self.record(self.window, sessionId=OLD, kind="interactive", startedAt=1, parkedJobId=JOB2)
        self.assertIsNone(self.hook(NEW_SEAT, NEW, event="start"))
        self.assertIsNone(self.hook(NEW_SEAT, NEW, event="prompt"))
        self.assertEqual((self.parked_rows(), self.follow(OLD)), ([], OLD))

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

    def test_a_message_question_keeps_its_exact_recipient_rule(self):
        # `[답변 필요]` questions belong to their exact session id; only a session that took the
        # seat ends them. A parked fork takes no seat, so the question stays the old id's own.
        import human_wait_state
        signal = {"harness": "claude", "session_id": OLD}
        self.assertTrue(human_wait_state._message_incarnation_current(signal))
        self.scene()
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW))
        self.assertTrue(human_wait_state._message_incarnation_current(signal))   # a fork took no seat
        self.record(self.window, sessionId=OLD, kind="interactive", startedAt=1)
        self.assertTrue(human_wait_state._message_incarnation_current(signal))

    def test_a_remembered_holder_does_not_outlive_the_link(self):
        import dispatch_registry_cache
        self.scene()
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW))
        self.owned_route("registered-by-the-old-id-after-the-split")    # nothing binds it
        records = dispatch_registry_cache.registry_view(self.jobs).records
        self.assertEqual([answer.session_id for answer in handover.job_holders(records, self.jobs)], [NEW])
        # Within one read pass the live links are one observation: the answer is reused.
        from peer_read_context import PeerReadContext
        context = PeerReadContext()
        handover.job_holders(records, self.jobs, read_context=context)
        before = handover.holder_metrics()["holder_resolves"]
        handover.job_holders(records, self.jobs, read_context=context)
        self.assertEqual(handover.holder_metrics()["holder_resolves"], before)
        graph = handover.lineage_graph()
        # The window takes its conversation back; no ledger changes, so the graph is the same one.
        (self.config / "sessions" / ("%d.json" % self.window.pid)).write_text(json.dumps(
            {"pid": self.window.pid, "procStart": sr._proc_start(self.window.pid), "sessionId": OLD,
             "kind": "interactive", "startedAt": 1}))
        self.assertIs(handover.lineage_graph(), graph)
        self.assertEqual([answer.session_id for answer in handover.job_holders(records, self.jobs)], [OLD])

    def test_the_background_process_ending_ends_the_link(self):
        self.scene()
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW))
        self.bg.stdin.close()
        self.bg.kill()
        self.bg.wait(timeout=10)
        self._forget()
        self.assertEqual(self.follow(OLD), OLD)

    # --- review round 1 (RV-PR1883): the scenes it reproduced ---------------------------------
    def test_an_answer_taken_as_the_conversation_goes_to_the_background_is_still_sent(self):
        # 🔴1: the answer is accepted, then the window's conversation goes to the background
        # before the answer is sent. The question still belongs to the old id exactly.
        import human_wait_state as waits
        import session_notice
        from fixtures.message_questions import MessageQuestionFixture
        with MessageQuestionFixture() as f:
            f.hold_projection_lock()
            signal, envelope = f.ask("claude", OLD, text="질문")
            real = session_notice.notify

            def split_then_send(*args, **kwargs):
                self.scene()
                self.assertIsNotNone(self.hook(NEW_SEAT, NEW))
                return real(*args, **kwargs)

            with mock.patch.object(session_notice, "notify", side_effect=split_then_send):
                f.submit(envelope, "그렇게 하세요")
            self.assertEqual(waits.read_selected_request(envelope)["state"], "submitted")
            self.assertIsNotNone(f.record(signal))                  # the answer's notice exists
            self.assertTrue(waits._message_incarnation_current(signal))

    def test_when_the_background_session_ends_what_it_held_comes_back_to_the_window(self):
        # 🔴2: completion, rewake and the steward watch use one judgment.
        meta = self.owned_route()
        watch = self.steward_role_and_watch()
        self.scene()
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW))
        record = {"attempt_ids": [meta["attempt_id"]]}
        self.record(self.window, sessionId=OLD, kind="interactive", startedAt=1)   # window back
        self.assertEqual(handover.record_recipient_binding(record, OLD, harness="claude", jobs=self.jobs),
                         ("claude", NEW))                                       # still the background's
        self.bg.stdin.close()
        self.bg.kill()
        self.bg.wait(timeout=10)
        self.assertEqual(handover.record_recipient_binding(record, OLD, harness="claude", jobs=self.jobs),
                         ("claude", OLD))
        self.assertTrue(handover.owns(meta, OLD, self.jobs))
        self.assertEqual(handover.resolve_watch(watch)["holder"]["sid"], OLD)
        self.assertTrue(handover.resolved_steward_relations("claude", OLD))
        self.assertIn(OLD, dict(handover.storage_recipients(OLD, harness="claude")))

    def test_one_read_pass_reads_the_links_once_and_an_effect_reads_them_again(self):
        # 🔴3: no memory outlives a read pass.
        from peer_read_context import PeerReadContext
        meta = self.owned_route()
        self.scene()
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW))
        later = self.owned_route("registered-after-the-split")
        with mock.patch.object(sr, "parked_links", wraps=sr.parked_links) as reads:
            context = PeerReadContext()
            for _ in range(5):
                handover.resolve_recipient("job-holder", metadata=later, jobs=self.jobs, read_context=context)
            self.assertEqual(reads.call_count, 1)
        self.assertEqual(handover.effective_parent(later, self.jobs), NEW)
        self.record(self.window, sessionId=OLD, kind="interactive", startedAt=1)
        self.assertEqual(handover.effective_parent(later, self.jobs), OLD)        # read afresh
        self.assertEqual(handover.chain_end("claude", OLD), (OLD, "self"))
        self.record(self.window, sessionId=OLD, kind="interactive", startedAt=1, parkedJobId=JOB2)
        self.bg2 = self.background(sid=NEW2, job=JOB2)
        self.assertEqual(handover.chain_end("claude", OLD)[1], "unresolved")    # second fork at once
        self.assertEqual(handover.effective_parent(meta, self.jobs), NEW)

    def test_the_inbox_observation_changes_when_a_link_starts_and_ends(self):
        # 🔴4: an inbox published from one observation is not current after the link changed.
        import dispatch_session_sweep as sweep
        self.scene()
        before = sweep.scopes_edge_digest(handover.storage_recipients(NEW, harness="claude"))
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW))
        linked = sweep.scopes_edge_digest(handover.storage_recipients(NEW, harness="claude"))
        self.record(self.window, sessionId=OLD, kind="interactive", startedAt=1)
        ended = sweep.scopes_edge_digest(handover.storage_recipients(NEW, harness="claude"))
        self.assertEqual(len({before, linked, ended}), 3)
        graph = handover.lineage_graph()
        self.assertNotEqual(handover.observed_edges(graph), tuple(graph.edges))

    def test_a_window_that_comes_back_while_the_job_lock_is_awaited_keeps_everything(self):
        # 🔴5: the window takes its conversation back while the writer waits for a job lock.
        meta = self.owned_route()
        watch = self.steward_role_and_watch()
        self.scene()
        real = handover.binding_mutexes

        @contextlib.contextmanager
        def window_returns(bindings, **kwargs):
            self.record(self.window, sessionId=OLD, kind="interactive", startedAt=1)
            with real(bindings, **kwargs) as held:
                yield held

        with mock.patch.object(handover, "binding_mutexes", side_effect=window_returns):
            self.assertIsNone(self.hook(NEW_SEAT, NEW))
        self.assertEqual(self.parked_rows(), [])
        self.assertEqual(handover.effective_parent(meta, self.jobs), OLD)
        self.assertEqual(handover.resolve_watch(watch)["holder"]["sid"], OLD)

    def test_a_marker_and_watch_without_a_seat_follow_too(self):
        # 🔴6: an older marker and watch that name no seat.
        watch = self.steward_role_and_watch(seat=False)
        self.scene()
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW))
        self.assertTrue(handover.resolved_steward_relations("claude", NEW))
        self.assertFalse(handover.resolved_steward_relations("claude", OLD))
        self.assertEqual(handover.resolve_watch(watch)["holder"]["sid"], NEW)

    def test_a_failed_write_leaves_nothing_and_the_next_hook_writes_it(self):
        # 🔴6: one record in one place; a failure in the middle is no half state.
        meta = self.owned_route()
        self.scene()
        with mock.patch.object(handover, "_append_transition_row", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.hook(NEW_SEAT, NEW)
        self.assertEqual((self.parked_rows(), handover.effective_parent(meta, self.jobs)), ([], OLD))
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW, event="prompt"))
        self.assertEqual((len(self.parked_rows()), handover.effective_parent(meta, self.jobs)), (1, NEW))

    def test_a_clear_inside_the_background_session_keeps_what_it_held(self):
        # 🔴7: the background session clears to NEW2; route, role and watch go on with it.
        meta = self.owned_route()
        watch = self.steward_role_and_watch()
        self.scene()
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW))
        own = self.owned_route("the-background-session's-own-route")
        own_meta = dict(own, parent_sid=NEW)
        with self.jobs.open("a") as stream:          # a route the background session started itself
            stream.write("now\trunning\t/w\t/w\ttask\t" + ",".join(
                k + "=" + v for k, v in dict(own_meta, attempt_id=own["attempt_id"][:-1] + "e",
                                              route_id=own["route_id"][:-1] + "e").items()) + "\n")
        logical = tidy.session_seat(NEW_SEAT, "claude", NEW)
        with tidy._session_state_locks([logical]):
            self.assertGreater(handover.write_snapshot_locked(logical, "claude", NEW, jobs=self.jobs), 0)
        self.record(self.bg, sessionId=NEW2, kind="bg", jobId=JOB, startedAt=int((time.time() - 60) * 1000))
        self.run_hook(NEW_SEAT, NEW2, "start", "clear")
        self.assertEqual(handover.chain_end("claude", NEW), (NEW2, "successor"))
        self.assertEqual(handover.effective_parent(meta, self.jobs), NEW2)
        self.assertTrue(handover.resolved_steward_relations("claude", NEW2))
        self.assertEqual(handover.resolve_watch(watch)["holder"]["sid"], NEW2)

    def test_the_window_clears_normally_after_taking_its_conversation_back(self):
        # 🔴7: the window took a snapshot, its conversation went away and came back, then it
        # clears; the hook runs in the window's own process while its record still shows the job.
        meta = self.owned_route()
        with tidy._session_state_locks([tidy.session_seat(OLD_SEAT, "claude", OLD)]):
            tidy.record_event(OLD_SEAT, "claude", OLD, "start", source="startup", now=time.time() - 300)
            handover.write_snapshot_locked(tidy.session_seat(OLD_SEAT, "claude", OLD), "claude", OLD,
                                           jobs=self.jobs)
        self.scene()
        self.assertIsNotNone(self.hook(NEW_SEAT, NEW))
        later = self.owned_route("after-the-window-came-back")
        with mock.patch.object(handover, "_caller_runtime_pid", return_value=self.window.pid):
            self.run_hook(OLD_SEAT, OTHER, "start", "clear")
            self.assertEqual(self.follow(OLD), OTHER)                  # the ordinary clear holds
            self.assertEqual(handover.effective_parent(later, self.jobs), OTHER)
        self.assertEqual(handover.effective_parent(meta, self.jobs), NEW)   # held by the background

    def test_a_route_in_another_registry_is_held_too(self):
        # 🔴9: Fleet reads the installed registries as well; their jobs are listed the same way.
        other = self.config / ".harness" / "dispatch" / "jobs.log"
        meta = self.owned_route_in(other, "route-in-the-installed-registry")
        self.scene()
        row = self.hook(NEW_SEAT, NEW)
        self.assertIn(meta["attempt_id"], [binding["attempt"] for binding in row["jobs"]])
        self.record(self.window, sessionId=OLD, kind="interactive", startedAt=1)
        self.assertEqual(handover.effective_parent(meta, other), NEW)
        self.assertTrue(handover.owns(meta, NEW, other))

    def test_an_older_release_reads_no_relation_in_the_row(self):
        # 🔴10: what a reader from before this row (or after a rollback) looks for is not there.
        self.owned_route()
        self.steward_role_and_watch()
        self.scene()
        row = self.hook(NEW_SEAT, NEW)
        self.assertNotIn(row["event"], {"handover", "succession", "steward-release"})
        self.assertFalse({"bindings", "steward", "snapshot_id", "source"} & set(row))
        self.assertIsNone(tidy.tidy_lineage_edge(row))
        self.assertIsNone(tidy.confirmed_predecessor_identity(NEW_SEAT, "claude", NEW))
        self.assertEqual(tidy.session_summary(tidy.session_seat(NEW_SEAT, "claude", NEW))[("claude", NEW)]
                         ["last_event"], "start")

    def test_a_row_with_any_field_in_doubt_is_no_row_for_every_reader(self):
        # 🟡: a row missing one evidence field is read the same way everywhere: not at all.
        meta = self.owned_route()
        self.scene()
        row = self.hook(NEW_SEAT, NEW)
        for path in (tidy.state_root() / "sessions").glob("*.jsonl"):
            lines = [json.loads(line) for line in path.read_text().splitlines()]
            for item in lines:
                if item.get("event") == handover.PARKED_FORK_EVENT:
                    item["parked"].pop("viewer_start")
            path.write_text("".join(json.dumps(item) + "\n" for item in lines))
        self.assertEqual(row["from"], OLD)
        self.assertEqual((self.follow(OLD), handover.effective_parent(meta, self.jobs)), (OLD, OLD))

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
