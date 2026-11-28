from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
"""The OpenCode plugin's own idle delivery keeps a row's receipt on that row's own native message.

A short form (body + `— <sender>`, one `↳` line) is what a person sees, never a receipt key: a new ref
with the short text of a received one is still sent, rows saved with their sealed text before the
update still settle, a window host's write keeps its exact-ref receipt, and the short form goes out
only with the ref's own full text held for the model call. Fixtures and private roots only; the
plugin runs under node against a stand-in OpenCode client."""
import json
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import human_wait_state as state  # noqa: E402
import work_intent  # noqa: E402


class OpenCodeCourierForms(unittest.TestCase):
    """The OpenCode plugin's own idle delivery. OpenCode 1 pastes the short forms only with the ref's own
    full text held for the resume its save starts, and finds its write by the message id the save gave
    (or, for its own unfinished write, the short text from after the row was sealed). OpenCode 2, whose
    prompt gives no id, pastes the sealed text. Native history is kept as a list and counted."""

    SENDER = {"harness": "codex", "session_id": "0100000a-0000-7000-8000-000000000012", "name": "fixture-peer"}
    OTHER = dict(SENDER, session_id="0100000a-0000-7000-8000-000000000034")   # another sender, the same name
    PEER = "테스트 다 돌았어?\n\n— codex fixture-peer"
    LINE = "↳ 질문에 답함: 네"

    def setUp(self):
        from fixtures.message_questions import MessageQuestionFixture
        self.fixture = MessageQuestionFixture()
        self.f = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        self.f.hold_projection_lock()
        patch = mock.patch.object(work_intent, "ui_translator", return_value=work_intent._ui_text().translator("ko"))
        patch.start()
        self.addCleanup(patch.stop)
        self.f.display = True

    def rows(self, sid, sender, question):
        """A peer message from ``sender`` and the answer `네` to a new question, both waiting for ``sid``."""
        recipient = {"harness": "opencode", "session_id": sid}
        self.f.peer.note_display_ready(recipient)
        _text, peer_ref = self.f.peer.prepare_peer_message("테스트 다 돌았어?", sender, recipient, defer=True)
        signal, envelope = self.f.ask("opencode", sid, text="QUESTION-" + question)
        self.f.submit(envelope, "네")
        _text, answer_ref, _recipient = self.f.prepare(signal)
        return signal, peer_ref, answer_ref

    def cli(self, sid, *args, stdin=None):
        import subprocess
        tool = Path(self.f.peer.__file__)
        done = subprocess.run([sys.executable, "-B", str(tool), *args, "--to-harness", "opencode",
                               "--to-session-id", sid], capture_output=True, text=True, timeout=60,
                              env=dict(os.environ, AGENT_HOME=str(tool.parents[1])), input=stdin)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout) if args[0] == "pending" and done.stdout.strip() else None

    def receipts(self, ref):
        return [r["delivery"].get("receipt") for r in self.f.peer._iter_records()
                if r.get("transfer_ref") == ref and (r.get("delivery") or {}).get("status") == "received"]

    @staticmethod
    def users(result):
        return [m for m in result["history"] if m["role"] == "user"]

    def call(self, result, name):
        return next(call["system"] for call in result["systems"] if call["for"] == name)

    def test_opencode_1_pastes_the_short_forms_and_opencode_2_the_sealed_text(self):
        for host in ("v1", "v2"):
            with self.subTest(host=host):
                sid = "forms-" + host
                signal, peer_ref, answer_ref = self.rows(sid, self.SENDER, "1")
                sealed = [self.f.peer._read_pending(ref)["text"] for ref in (peer_ref, answer_ref)]
                agent = self.f.peer._read_transfer_record(answer_ref)["display"]["agent"]
                # Six idles on one history: after the two rows settle, nothing is sent again.
                result = self.f.opencode_plugin_session(sid, {"idles": 6, "v2": host == "v2"})
                users = self.users(result)
                self.assertEqual(len({m["id"] for m in users}), len(users))
                if host == "v1":
                    self.assertEqual(result["calls"], {"prompt": 2, "promptAsync": 2})
                    self.assertEqual(sorted(m["text"] for m in users), sorted([self.PEER, self.LINE]))
                    sent = {m["text"]: m["id"] for m in users}
                    self.assertIn("Sender of the message above (the person sees only `— codex fixture-peer`)",
                                  self.call(result, sent[self.PEER]))
                    self.assertIn("the full text:\n" + agent, self.call(result, sent[self.LINE]))
                else:
                    self.assertEqual(result["calls"], {"prompt": 0, "promptAsync": 2})
                    self.assertEqual(sorted(m["text"] for m in users), sorted(sealed))
                for ref in (peer_ref, answer_ref):
                    self.assertEqual(self.receipts(ref), ["exact-native-history"])
                self.assertEqual(self.f.peer._read_pending(peer_ref)["state"], "received")
                self.assertEqual(self.source_lifecycle(signal), "answered")

    def source_lifecycle(self, signal):
        return state._read(state._signal_path(signal))["lifecycle"]

    def test_a_new_ref_with_the_short_text_of_a_received_one_is_sent_and_settles_on_its_own_message(self):
        # A (a peer message, a question's answer) was received; B (another sender of the same name, the
        # next question with the same answer) gets the same short text, on the same history. B is sent
        # and settles on its own message, never on A's. OpenCode 1 also loses its save responses here:
        # its write is found from after B was sealed, so A's older message is not taken for it.
        for host in ("v1", "v2"):
            with self.subTest(host=host):
                sid, v2 = "again-" + host, host == "v2"
                first, a_peer, a_answer = self.rows(sid, self.SENDER, "1")
                before = self.f.opencode_plugin_session(sid, {"idles": 4, "v2": v2})
                self.assertEqual(self.source_lifecycle(first), "answered")
                second, b_peer, b_answer = self.rows(sid, self.OTHER, "2")
                for ref in (b_peer, b_answer):
                    self.assertIn("display", self.f.peer._read_transfer_record(ref))
                after = self.f.opencode_plugin_session(sid, {"idles": 4, "v2": v2, "history": before["native"],
                                                             "loseSave": not v2})
                self.assertEqual(after["calls"], {"prompt": 0 if v2 else 2, "promptAsync": 2})
                users = self.users(after)
                self.assertEqual(len(users), 4)
                self.assertEqual(len({m["id"] for m in users}), 4)
                if not v2:
                    self.assertEqual(sorted(m["text"] for m in users), sorted([self.PEER, self.PEER, self.LINE, self.LINE]))
                earlier = {m["id"] for m in self.users(before)}
                for ref in (b_peer, b_answer):
                    self.assertNotIn(self.f.peer._read_pending(ref)["actual_message_id"], earlier)
                for ref in (a_peer, a_answer, b_peer, b_answer):
                    self.assertEqual(self.receipts(ref), ["exact-native-history"])
                self.assertEqual(self.source_lifecycle(second), "answered")
                self.assertEqual(self.f.record(second)["state"], "acked")

    def test_rows_saved_with_their_sealed_text_before_the_update_still_settle(self):
        # Before this release the courier saved a row's sealed text. A row it saved and queued (the
        # message id kept) and one whose save response it lost before recording the id both settle on
        # that message, with nothing sent again.
        import dispatch_contract
        import time
        me = [str(os.getpid()), str(dispatch_contract.process_start_ticks(os.getpid()))]
        for host in ("v1", "v2"):
            with self.subTest(host=host):
                sid = "saved-" + host
                recipient = {"harness": "opencode", "session_id": sid}
                self.f.peer.note_display_ready(recipient)
                refs, history = [], []
                for index in range(2):
                    _text, ref = self.f.peer.prepare_peer_message("저장된 글 %d" % index, self.SENDER, recipient,
                                                                  defer=True)
                    self.assertIn("display", self.f.peer._read_transfer_record(ref))
                    reserved = self.cli(sid, "pending", "--reserve", ref, "--claim-pid", me[0], "--claim-start", me[1])
                    claimed = self.cli(sid, "pending", "--begin-write", ref, "--claim-token",
                                       reserved["rpc_claim"]["token"], "--claim-pid", me[0], "--claim-start", me[1])
                    if index == 0:
                        self.cli(sid, "pending", "--queued", ref, "--message-id", "saved-sealed-0")
                    history.append({"info": {"id": "saved-sealed-%d" % index, "role": "user", "sessionID": sid,
                                             "time": {"created": int(time.time() * 1000) + index}},
                                    "parts": [{"type": "text", "text": claimed["text"]}]})
                    refs.append(ref)
                result = self.f.opencode_plugin_session(sid, {"history": history, "idles": 4, "v2": host == "v2"})
                self.assertEqual(result["calls"], {"prompt": 0, "promptAsync": 0 if host == "v2" else 2})
                self.assertEqual(len(self.users(result)), 2)
                for index, ref in enumerate(refs):
                    row = self.f.peer._read_pending(ref)
                    self.assertEqual((row["state"], row["actual_message_id"]), ("received", "saved-sealed-%d" % index))
                    self.assertEqual(self.receipts(ref), ["exact-native-history"])

    def test_a_window_host_write_is_left_to_its_exact_ref_receipt(self):
        # The session-shell host reserved and began a row and pasted its short text. The courier's idle
        # passes find that message but leave it to the host's receipt, which the plugin's `receive` for
        # that prompt records late: one receipt, nothing sent.
        import dispatch_contract
        import time
        me = {"pid": os.getpid(), "start": dispatch_contract.process_start_ticks(os.getpid())}
        for host in ("v1", "v2"):
            with self.subTest(host=host):
                sid = "shell-" + host
                recipient = {"harness": "opencode", "session_id": sid}
                self.f.peer.note_display_ready(recipient)
                _text, ref = self.f.peer.prepare_peer_message("셸로 붙인 글", self.SENDER, recipient, defer=True)
                self.assertTrue(self.f.peer.reserve_pending_herdr(ref, recipient, receipt="session-shell-inflight"))
                self.assertTrue(self.f.peer.begin_pending_write(self.f.peer._read_pending(ref), recipient=recipient,
                                                                receipt="session-shell-inflight", claimant=me))
                pasted = self.f.peer.pasted_text(self.f.peer._read_pending(ref))
                self.assertEqual(pasted, "셸로 붙인 글\n\n— codex fixture-peer")
                history = [{"info": {"id": "shell-msg", "role": "user", "sessionID": sid,
                                     "time": {"created": int(time.time() * 1000) + 1}},
                            "parts": [{"type": "text", "text": pasted}]}]
                result = self.f.opencode_plugin_session(sid, {"history": history, "idles": 2, "v2": host == "v2"})
                self.assertEqual(result["calls"], {"prompt": 0, "promptAsync": 0})
                row = self.f.peer._read_pending(ref)
                self.assertEqual((row["state"], row["receipt"], row.get("actual_message_id")),
                                 ("unverified", "session-shell-inflight", None))
                self.assertEqual(self.receipts(ref), [])
                self.cli(sid, "receive", "--from-project", "fixture", stdin=pasted)
                self.assertEqual(self.receipts(ref), ["exact-peer-ref"])
                self.assertEqual(self.f.peer._read_pending(ref)["state"], "received")

    def test_the_courier_pastes_the_sealed_text_when_it_cannot_hold_the_rows_full_text(self):
        # OpenCode 1 reads the ref's own guidance (its full text) before its save; when that read fails
        # it pastes the sealed text, so the model's input is the full text. Once held, a failed read
        # later (the save's own `chat.message`, a re-read) does not take the full text from the model
        # call. A read that cannot find this very ref behind the short text (its fingerprint hint gone)
        # holds nothing either. OpenCode 2 pastes the sealed text whatever its reads do.
        for index, (host, fail) in enumerate((("v1", [1]), ("v1", [2, 3]), ("v1", []), ("v2", [1]))):
            with self.subTest(host=host, fail=fail):
                sid = "held-%s-%d" % (host, index)
                signal, envelope = self.f.ask("opencode", sid)
                self.f.submit(envelope, "네")
                _text, ref, _recipient = self.f.prepare(signal)
                sealed = self.f.peer._read_pending(ref)["text"]
                entry = self.f.peer._read_transfer_record(ref)["display"]
                self.assertEqual(sealed, entry["agent"])
                if not fail:
                    (self.f.peer._display_root() / entry["sha256"] / ref).unlink()
                result = self.f.opencode_plugin_session(sid, {"idles": 3, "failGuidance": fail, "v2": host == "v2"})
                users = self.users(result)
                self.assertEqual(result["calls"], {"prompt": 0, "promptAsync": 1} if host == "v2"
                                 else {"prompt": 1, "promptAsync": 1})
                if fail == [2, 3]:
                    self.assertEqual([m["text"] for m in users], [self.LINE])
                    self.assertIn("the full text:\n" + sealed, self.call(result, users[0]["id"]))
                else:
                    self.assertEqual([m["text"] for m in users], [sealed])
                self.assertEqual(self.source_lifecycle(signal), "answered")
                self.assertEqual(self.f.peer._read_pending(ref)["state"], "received")



if __name__ == "__main__":
    unittest.main()
