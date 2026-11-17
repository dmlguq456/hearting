"""11-17: the panel's MESSAGES sent row says what ``peer-steward show`` says about a message that has not gone in.

A sent message's pending row, still ``pending``, reads ``awaiting delivery`` (``전달 대기``) and, when the
courier recorded why, the same words ``peer_request.hold_words`` gives (busy, form, draft, paste, unsettled,
unobserved, old-shell, no-shell): the panel backend reads the rows of the (at most eight) visible sent messages
once, from the pending row the message already has, and watches those files. A message that went in, was seen or
failed reads as before. A real window is not used: the rows are synthetic and the window is not touched.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
spec = importlib.util.spec_from_file_location("session_shell_wait_1117", ROOT / "utilities/session_shell.py")
shell = importlib.util.module_from_spec(spec)
sys.modules["session_shell_wait_1117"] = shell
spec.loader.exec_module(shell)

SID = "00000000-0000-4000-8000-0000000000e9"
OTHER = "00000000-0000-4000-8000-0000000000e7"
CAUSES = ("busy", "form", "draft", "paste", "unsettled", "unobserved", "old-shell", "no-shell")


class MessageWaitTest(unittest.TestCase):
    def ledger(self, directory, harness, statuses):
        peer = shell._peer_message()
        shard = peer._ledger_path(SID)
        shard.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rows = [{"ts": stamp, "from": {"session_id": SID, "harness": harness},
                 "to": {"harness": harness, "session_id": OTHER, "name": "n"}, "kind": "steer", "summary": "NOTE_%d" % n,
                 "message_id": ("%032x" % (n + 1)), "delivery": {"status": status}} for n, status in enumerate(statuses)]
        shard.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        shell._SENT_CACHE.clear()
        return peer, ["%032x" % (n + 1) for n in range(len(statuses))]

    def project(self, harness):
        import session_tidy
        with mock.patch.object(session_tidy, "prompt_display", return_value={"received": []}):
            return shell.panel_projection(harness, SID, columns=60, rows=30)

    def test_each_reason_reads_as_peer_steward_show_reads_it_for_every_harness(self):
        import peer_request
        for harness in ("claude", "codex", "opencode"):
            for language in ("English", "Korean"):
                for reason in (None, *CAUSES):
                    with self.subTest(harness=harness, language=language, reason=reason), \
                            tempfile.TemporaryDirectory() as directory, \
                            mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": directory, "FLEET_NOW_LANG": language}):
                        peer, refs = self.ledger(directory, harness, ["sent"])
                        row = {"state": "pending", **({"held": {"reason": reason}} if reason else {})}
                        with mock.patch.object(peer, "_read_pending", side_effect=lambda ref, **k: row):
                            projection = self.project(harness)
                        delivery = projection["panel"]["messages"][0]["delivery"]
                        self.assertEqual(delivery[0], peer_request.delivery_word(row))
                        self.assertEqual(delivery[1], "dim")
                        self.assertTrue(delivery[0].startswith(delivery[2]))
                        self.assertIn(str(peer._pending_path(refs[0])), projection["watch"])

    def test_states_that_went_in_were_seen_or_failed_read_as_before_and_are_not_read(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, {"AGENT_PEER_LEDGER_ROOT": directory, "FLEET_NOW_LANG": "Korean"}):
            peer, refs = self.ledger(directory, "claude", ["received", "failed", "unknown", "sent"])
            read = []

            def pending(ref, **_):
                read.append(ref)
                return {"state": "queued"}                    # the receiving window took it in: `sent` as before
            with mock.patch.object(peer, "_read_pending", side_effect=pending):
                words = [m["delivery"][0] for m in self.project("claude")["panel"]["messages"]]
            self.assertEqual(words, ["전달됨", "실패", "전달 안 됨", "보냄"])
            self.assertEqual(read, [refs[3]])                  # only the one still `sent`, once

    def test_at_most_the_eight_visible_rows_are_read_and_an_unreadable_row_reads_as_sent(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, {"AGENT_PEER_LEDGER_ROOT": directory, "FLEET_NOW_LANG": "English"}):
            peer, refs = self.ledger(directory, "claude", ["sent"] * 20)
            read = []

            def pending(ref, **_):
                read.append(ref)
                raise ValueError("peer-pending-file-unsafe")
            with mock.patch.object(peer, "_read_pending", side_effect=pending):
                messages = self.project("claude")["panel"]["messages"]
            self.assertEqual(len(messages), 8)
            self.assertLessEqual(len(read), 8)
            self.assertEqual(len(read), len(set(read)))
            self.assertTrue(all(m["delivery"] == ["sent", "dim"] for m in messages))

    def test_a_real_pending_row_without_a_reason_reads_the_word_alone(self):
        import peer_request
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, {"AGENT_PEER_LEDGER_ROOT": directory, "FLEET_NOW_LANG": "English"}):
            peer = shell._peer_message()
            _text, ref = peer.prepare_peer_message("hello", {"harness": "claude", "session_id": SID},
                                                   {"harness": "claude", "session_id": OTHER}, defer=True)
            shard = peer._ledger_path(SID)
            shard.parent.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            shard.write_text(json.dumps({"ts": stamp, "from": {"session_id": SID}, "to": {"harness": "claude", "session_id": OTHER, "name": "n"},
                                         "kind": "steer", "summary": "NOTE", "message_id": ref, "delivery": {"status": "sent"}}) + "\n")
            shell._SENT_CACHE.clear()
            row = peer._read_pending(ref)
            self.assertEqual(row["state"], "pending")
            self.assertEqual(self.project("claude")["panel"]["messages"][0]["delivery"][0], peer_request.delivery_word(row))


if __name__ == "__main__":
    unittest.main()
