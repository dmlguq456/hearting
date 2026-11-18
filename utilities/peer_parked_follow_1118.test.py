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
SLEEP = "import time; time.sleep(60)"


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
            child.kill()
            child.wait()
        peer_message._PARKED_MEMO.clear()

    def spawn(self, *argv):
        child = subprocess.Popen([sys.executable, "-c", SLEEP, *argv], stdin=subprocess.DEVNULL)
        self.children.append(child)
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

    def test_resolve_does_not_follow_without_the_link(self):
        self.scene(parked=False)
        from fleet import session_tags
        with mock.patch.object(session_tags, "_read", return_value={("claude", OLD): {"tag": "c7"}}), \
                mock.patch.object(session_tags, "_path", return_value="x"), \
                mock.patch.object(session_tags, "assigned_tag", return_value="c7"):
            self.assertEqual(peer_address.resolve("[c7]")[0], {"harness": "claude", "session_id": OLD})


if __name__ == "__main__":
    unittest.main(verbosity=2)
