#!/usr/bin/env python3
"""11-18: a number or a name of a window whose conversation went to the background reaches it.

``peer_address`` follows a number or a name that finds the window's viewer to the background
session holding the conversation, and ``peer-message`` binds the rows still addressed to the
old id to that session, but only the ones made after its process started. Real child processes
carry the argv, real pending rows are made and delivered by the native hook boundary.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import io
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
spec = importlib.util.spec_from_file_location("peer_message_parked_1118", HERE / "peer-message.py")
peer_message = importlib.util.module_from_spec(spec)
sys.modules["peer_message_parked_1118"] = peer_message
spec.loader.exec_module(peer_message)

import peer_address  # noqa: E402
from fleet import session_registry as sr  # noqa: E402

OLD = "92d7cdcd-3cef-4583-80e6-f9a4561ac629"
NEW = "e675a253-0561-410f-aa04-c12345cabf70"
OTHER = "11111111-2222-4333-8444-555555555555"
SENDER = "66666666-7777-4888-8999-000000000000"
HOLD = "import os; os.write(1, b'R'); os.read(0, 1)"


class Parked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ppf-")
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
        jobs = self.root / "jobs.log"
        jobs.touch()
        os.environ.update(HOME=str(self.home), CLAUDE_CONFIG_DIR=str(self.config),
                          XDG_STATE_HOME=str(self.home / ".local" / "state"),
                          AGENT_DISPATCH_JOBS=str(jobs), AGENT_PEER_LEDGER_ROOT=str(self.root / "peer"))
        peer_message._PARKED_MEMO.clear()
        self.children = []
        self.addCleanup(self._reap)

    def _reap(self):
        for child in self.children:
            child.stdin.close()
            child.kill()
            child.wait(timeout=10)
        self.children.clear()
        peer_message._PARKED_MEMO.clear()

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
        (self.config / "sessions" / ("%d.json" % child.pid)).write_text(json.dumps(
            {"pid": child.pid, "procStart": sr._proc_start(child.pid), **fields}))

    def scene(self, parked=True, started=None):
        """The viewer of OLD, and NEW that its job holds; NEW's process began at ``started`` (epoch s)."""
        self.started = started if started is not None else time.time() - 600
        self.viewer = self.spawn()
        self.record(self.viewer, sessionId=OLD, kind="interactive", startedAt=1,
                    **({"parkedJobId": "job1"} if parked else {}))
        self.bg = self.spawn("--session-id", NEW, "--fork-session", "--resume", "/p/%s.jsonl" % OLD)
        self.record(self.bg, sessionId=NEW, kind="bg", jobId="job1", startedAt=int(self.started * 1000))

    def pend(self, recipient_sid, created, body="hello across the split"):
        """A real pending row to ``recipient_sid`` (as an older sender made it), dated ``created``."""
        recipient = {"harness": "claude", "session_id": recipient_sid, "name": None}
        sender = {"harness": "unknown", "session_id": "", "name": None}   # the writer the native hook accepts
        text, ref = peer_message.prepare_peer_message(body, sender, recipient, defer=True)
        row = json.loads(peer_message._pending_path(ref).read_text())
        row.update(created=created, state="unverified", receipt="peer-endpoint-unverified")
        peer_message._save_pending(row)
        return text, ref

    def hook(self, sid):
        stream = io.StringIO()
        peer_message.emit_peer_context({"harness": "claude", "session_id": sid}, "UserPromptSubmit",
                                       stream=stream, native_delivery=True)
        peer_message._PARKED_MEMO.clear()
        return (json.loads(stream.getvalue())["hookSpecificOutput"]["additionalContext"]
                if stream.getvalue() else "")

    def state(self, ref):
        return peer_message._read_pending(ref)["state"]

    # --- M4: the receiving side ------------------------------------------------------------
    def test_rows_made_after_the_process_started_reach_it_once(self):
        self.scene()
        text, ref = self.pend(OLD, self.started + 30)
        self.assertEqual(self.hook(NEW), text)
        self.assertEqual(self.state(ref), "received")
        self.assertEqual(self.hook(NEW), "")                      # delivered once
        self.assertEqual(self.hook(OLD), "")

    def test_rows_made_before_the_process_started_stay_with_the_original(self):
        self.scene()
        before, ref_before = self.pend(OLD, self.started - 60, "written to the original")
        after, ref_after = self.pend(OLD, self.started + 1, "written after the split")
        self.assertEqual(self.hook(NEW), after)                   # many old rows never pour into NEW
        self.assertNotEqual(self.state(ref_before), "received")
        self.assertEqual(self.state(ref_after), "received")
        self.assertEqual([row["ref"] for row in peer_message.pending_messages({"harness": "claude", "session_id": NEW})], [])
        self.assertEqual([row["ref"] for row in peer_message.pending_messages({"harness": "claude", "session_id": OLD})],
                         [ref_before])

    def test_a_row_for_the_held_session_itself_is_unchanged(self):
        self.scene()
        text, ref = self.pend(NEW, self.started + 5)
        self.assertEqual(self.hook(NEW), text)
        self.assertEqual(self.state(ref), "received")

    def test_the_viewer_no_longer_takes_a_row_that_now_belongs_to_the_held_session(self):
        self.scene()
        _text, ref = self.pend(OLD, self.started + 5)
        recipient = {"harness": "claude", "session_id": OLD}
        self.assertEqual(peer_message.pending_messages(recipient), [])
        self.assertEqual(peer_message.pending_delivery_recipient(peer_message._read_pending(ref))["session_id"], NEW)
        self.assertFalse(peer_message._pending_target_matches(peer_message._read_pending(ref), recipient))

    def test_no_link_leaves_every_row_with_the_old_id(self):
        # A deliberate fork: the argv matches but the viewer holds no job.
        self.scene(parked=False)
        _text, ref = self.pend(OLD, self.started + 5)
        self.assertEqual(self.hook(NEW), "")
        self.assertNotEqual(self.state(ref), "received")
        self.assertEqual(len(peer_message.pending_messages({"harness": "claude", "session_id": OLD})), 1)

    def test_a_dead_held_process_returns_the_row_to_the_original(self):
        self.scene()
        _text, ref = self.pend(OLD, self.started + 5)
        self.bg.kill()
        self.bg.wait()
        peer_message._PARKED_MEMO.clear()
        self.assertEqual(self.hook(NEW), "")
        self.assertEqual([row["ref"] for row in peer_message.pending_messages({"harness": "claude", "session_id": OLD})], [ref])

    def test_other_harnesses_and_other_sessions_are_not_followed(self):
        self.scene()
        recipient = {"harness": "codex", "session_id": OLD, "name": None}
        sender = {"harness": "claude", "session_id": SENDER, "name": None}
        _text, ref = peer_message.prepare_peer_message("to a codex thread", sender, recipient, defer=True)
        row = json.loads(peer_message._pending_path(ref).read_text())
        row.update(created=self.started + 5)
        peer_message._save_pending(row)
        self.assertEqual(peer_message.pending_delivery_recipient(peer_message._read_pending(ref))["session_id"], OLD)
        _text, other = self.pend(OTHER, self.started + 5)
        self.assertEqual(peer_message.pending_delivery_recipient(peer_message._read_pending(other))["session_id"], OTHER)

    # --- a scan asks about the processes once, however many rows it looks at ----------------
    def reads(self):
        real = sr._live_claude_records
        return mock.patch.object(sr, "_live_claude_records", side_effect=real)

    def test_a_scan_reads_the_session_records_once_for_every_row(self):
        for linked in (False, True):
            with self.subTest(linked=linked):
                self.tearDown_children()
                self.scene(parked=linked)
                for index in range(40):
                    self.pend(OLD if index % 2 else OTHER, self.started + index)
                peer_message._PARKED_MEMO.clear()
                with self.reads() as read:
                    list(peer_message._pending_rows(strict=True))     # a send's duplicate check: every row
                # With no job held by any window the raw records say so and nothing is judged.
                self.assertEqual(read.call_count, 1 if linked else 0)
                peer_message._PARKED_MEMO.clear()
                with self.reads() as read:
                    peer_message.pending_messages({"harness": "claude", "session_id": NEW})
                    peer_message.pending_messages({"harness": "claude", "session_id": OLD})
                self.assertEqual(read.call_count, 1 if linked else 0)  # the table is shared inside the second

    def test_no_window_held_means_no_per_row_work(self):
        self.scene(parked=False)
        for index in range(10):
            self.pend(OLD, self.started + index)
        peer_message._PARKED_MEMO.clear()
        with mock.patch.object(sr, "parked_successor") as one, mock.patch.object(sr, "parked_origin") as two, \
                mock.patch.object(sr, "_parked_from_viewer", wraps=sr._parked_from_viewer) as judged:
            list(peer_message._pending_rows(strict=True))
        one.assert_not_called()
        two.assert_not_called()
        judged.assert_not_called()                                    # nothing holds a job: nothing to judge

    def tearDown_children(self):
        self._reap()
        for path in (self.config / "sessions").iterdir():
            path.unlink()

    # --- what the held session received is received (the duty and the index) --------------
    def steward(self):
        spec2 = importlib.util.spec_from_file_location("peer_steward_parked_1118", HERE / "peer-steward.py")
        module = importlib.util.module_from_spec(spec2)
        sys.modules["peer_steward_parked_1118"] = module
        spec2.loader.exec_module(module)
        module.peer_message._PARKED_MEMO.clear()
        self.addCleanup(module.peer_message._PARKED_MEMO.clear)
        return module

    def duty_after(self, steward, ref):
        import peer_obligations
        store = peer_obligations.ObligationStore()
        with mock.patch.object(peer_obligations, "ensure_normal_runner", return_value=None):
            steward._resume_message_obligation(store.get("message-" + ref), store)
        return store.get("message-" + ref) or {}

    def test_a_row_the_held_session_received_closes_its_duty(self):
        import peer_obligations
        steward = self.steward()
        self.scene()
        steward.peer_message._PARKED_MEMO.clear()
        sender = {"harness": "claude", "session_id": SENDER, "name": None}
        text, ref = steward.peer_message.prepare_peer_message(
            "duty across the split", sender, {"harness": "claude", "session_id": OLD, "name": None}, defer=True)
        row = steward.peer_message._read_pending(ref)
        row.update(created=self.started + 30)
        steward.peer_message._save_pending(row)
        row = steward.peer_message._read_pending(ref)
        with mock.patch.object(peer_obligations, "ensure_normal_runner", return_value=None):
            duty = steward._schedule_message_obligation(row, "claude:" + OLD)
        self.assertEqual(duty["intent"]["to"]["session_id"], OLD)    # the duty keeps the row's own target
        self.assertEqual(duty["intent"]["identity"]["session_id"], NEW)         # and who is to take it now
        steward.peer_message._PARKED_MEMO.clear()
        self.assertEqual(self.hook(NEW), text)
        self.assertEqual(self.state(ref), "received")
        for duty in (peer_obligations.ObligationStore().get("message-" + ref), self.duty_after(steward, ref)):
            self.assertEqual((duty["state"], duty["delivery"]), ("complete", "acknowledged"))
            self.assertEqual(duty["observation"]["receiver_receipt"]["recipient"]["session_id"], NEW)

    def test_a_duty_made_before_the_split_closes_when_the_held_session_receives(self):
        import peer_obligations
        steward = self.steward()
        sender = {"harness": "claude", "session_id": SENDER, "name": None}
        text, ref = steward.peer_message.prepare_peer_message(
            "made before the split", sender, {"harness": "claude", "session_id": OLD, "name": None}, defer=True)
        row = steward.peer_message._read_pending(ref)
        with mock.patch.object(peer_obligations, "ensure_normal_runner", return_value=None):
            duty = steward._schedule_message_obligation(row, "claude:" + OLD)
        self.assertEqual(duty["intent"]["to"]["session_id"], OLD)
        self.scene()                                                  # the conversation goes to the background
        steward.peer_message._PARKED_MEMO.clear()
        row = steward.peer_message._read_pending(ref)
        row.update(created=self.started + 30)
        steward.peer_message._save_pending(row)
        self.assertEqual(self.hook(NEW), text)
        duty = peer_obligations.ObligationStore().get("message-" + ref)
        self.assertEqual((duty["state"], duty["delivery"]), ("complete", "acknowledged"))

    def test_a_receipt_without_the_link_is_not_accepted_for_another_session(self):
        import peer_obligations
        steward = self.steward()
        sender = {"harness": "claude", "session_id": SENDER, "name": None}
        _text, ref = steward.peer_message.prepare_peer_message(
            "for the other one", sender, {"harness": "claude", "session_id": OTHER, "name": None}, defer=True)
        row = steward.peer_message._read_pending(ref)
        with mock.patch.object(peer_obligations, "ensure_normal_runner", return_value=None):
            steward._schedule_message_obligation(row, "claude:" + OTHER)
        self.scene()
        row = dict(steward.peer_message._read_pending(ref), state="received", receipt="exact-peer-ref")
        proof = {"receipt_type": "exact-peer-ref", "transfer_ref": ref, "transfer_body_sha256": row["body_sha256"],
                 "provenance": peer_obligations._MESSAGE_RECEIVER_PROVENANCE,
                 "effective_recipient": {"harness": "claude", "session_id": NEW},
                 "recipient_binding": {"recipient": {"harness": "claude", "session_id": NEW}, "watches": [],
                                       "parked_from": {"harness": "claude", "session_id": OLD}}}
        store = peer_obligations.ObligationStore()
        self.assertFalse(store.acknowledge_message_receipt(
            "message-" + ref, dict(row, exact_receiver_receipt=proof), {"harness": "claude", "session_id": NEW}))

    def test_the_old_sessions_index_shows_the_row_as_soon_as_the_link_ends(self):
        self.scene()
        _text, ref = self.pend(OLD, self.started + 5, "after the split")
        recipient = {"harness": "claude", "session_id": OLD}
        peer_message._PARKED_MEMO.clear()
        self.assertEqual(peer_message.pending_messages(recipient), [])     # NEW holds it now
        index = peer_message.recipient_index_path(peer_message._recipient_index_write_root(), "claude", OLD)
        self.assertFalse(index.is_file())                                  # nothing published while it is held
        self.bg.kill()
        self.bg.wait()
        peer_message._PARKED_MEMO.clear()
        self.assertEqual([row["ref"] for row in peer_message.pending_messages(recipient)], [ref])

    def test_the_runner_does_not_publish_the_old_sessions_index_while_it_is_held(self):
        self.scene()
        _text, ref = self.pend(OLD, self.started + 5, "after the split")
        _text, other = self.pend(OLD, self.started - 60, "before the split")
        peer_message._PARKED_MEMO.clear()
        root = peer_message._recipient_index_write_root()
        result = peer_message.rebuild_recipient_index(root)
        self.assertTrue(result["complete"])
        self.assertFalse(peer_message.recipient_index_path(root, "claude", OLD).is_file())
        self.assertTrue(peer_message.recipient_index_path(root, "claude", NEW).is_file())
        self.bg.kill()
        self.bg.wait()
        peer_message._PARKED_MEMO.clear()
        self.assertEqual(sorted(row["ref"] for row in peer_message.pending_messages(
            {"harness": "claude", "session_id": OLD})), sorted([ref, other]))

    # --- M4: the sending side --------------------------------------------------------------
    def test_a_number_or_a_name_that_finds_the_viewer_reaches_the_held_session(self):
        self.scene()
        found = peer_address._follow_parked(peer_address._identity("claude", OLD))
        self.assertEqual(found, {"harness": "claude", "session_id": NEW})
        self.assertEqual(peer_address._follow_parked(peer_address._identity("claude", NEW)),
                         {"harness": "claude", "session_id": NEW})
        self.assertEqual(peer_address._follow_parked(peer_address._identity("codex", OLD)),
                         {"harness": "codex", "session_id": OLD})

    def test_resolve_follows_a_number_and_a_name_but_not_an_exact_id(self):
        self.scene()
        from fleet import session_tags
        tag_rows = {("claude", OLD): {"tag": "c7"}}
        with mock.patch.object(session_tags, "_read", return_value=tag_rows), \
                mock.patch.object(session_tags, "_path", return_value="x"), \
                mock.patch.object(session_tags, "assigned_tag", return_value="c7"):
            self.assertEqual(peer_address.resolve("[c7]")[0], {"harness": "claude", "session_id": NEW})
        with mock.patch.object(peer_address, "native_sessions",
                               return_value=[{"harness": "claude", "sid": OLD, "pid": self.viewer.pid, "env": {}}]), \
                mock.patch.object(peer_address, "_registry_name", return_value="main-window"):
            self.assertEqual(peer_address.resolve("main-window")[0], {"harness": "claude", "session_id": NEW})
        # An exact `harness:session_id` names exactly that session.
        self.assertEqual(peer_address.resolve("claude:" + OLD)[0], {"harness": "claude", "session_id": OLD})

    def test_resolving_a_number_or_a_name_reads_the_session_records_once(self):
        self.scene()
        from fleet import session_tags
        with mock.patch.object(session_tags, "_read", return_value={("claude", OLD): {"tag": "c7"}}), \
                mock.patch.object(session_tags, "_path", return_value="x"), \
                mock.patch.object(session_tags, "assigned_tag", return_value="c7"), self.reads() as read:
            peer_address.resolve("[c7]")
        self.assertEqual(read.call_count, 1)
        with mock.patch.object(peer_address, "native_sessions",
                               return_value=[{"harness": "claude", "sid": OLD, "pid": self.viewer.pid, "env": {}}]), \
                mock.patch.object(peer_address, "_registry_name", return_value="main-window"), self.reads() as read:
            peer_address.resolve("main-window")
        self.assertEqual(read.call_count, 1)

    def test_resolve_does_not_follow_without_the_link(self):
        self.scene(parked=False)
        from fleet import session_tags
        with mock.patch.object(session_tags, "_read", return_value={("claude", OLD): {"tag": "c7"}}), \
                mock.patch.object(session_tags, "_path", return_value="x"), \
                mock.patch.object(session_tags, "assigned_tag", return_value="c7"):
            self.assertEqual(peer_address.resolve("[c7]")[0], {"harness": "claude", "session_id": OLD})


if __name__ == "__main__":
    unittest.main(verbosity=2)
