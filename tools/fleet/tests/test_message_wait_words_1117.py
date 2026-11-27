"""11-17: a sent message that has not gone in reads ``awaiting delivery`` and why, in Fleet as in the shell panel.

``peer_message_rows`` / ``request_rows`` / the NOW popup's message rows / the session page's observation word a pending
row with ``peer.delivery.waiting`` and ``peer_request.hold_words`` -- the words ``peer-steward show`` uses. Everything is
synthetic.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

NOW = 1_800_000_000.0
CAUSES = ("busy", "form", "draft", "paste", "unsettled", "unobserved", "old-shell", "no-shell")


class SentWords(unittest.TestCase):
    """The same pending row is worded the same wherever a sent message is drawn."""

    def words(self, language, row):
        import peer_request
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": language}):
            return peer_request.delivery_word(row)

    def rows(self, language, state="pending", reason=None, status="sent", count=1, reads=None):
        from tools.fleet.session_line import peer_message_rows
        sent = [{"to": {"harness": "claude", "session_id": "sid-x", "name": "x"}, "summary": "note %d" % n,
                 "at": NOW - 100 * (count - n), "message_id": "m%d" % n, "kind": "steer", "status": status}
                for n in range(count)]
        row = {"state": state, **({"held": {"reason": reason}} if reason else {})}

        def pending_of(ref):
            if reads is not None:
                reads.append(ref)
            return row
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": language}):
            return peer_message_rows([], sent, lambda h, s: "6c", limit=count, now=NOW, pending_of=pending_of), row

    def test_each_reason_reads_as_peer_steward_show_reads_it_in_both_languages(self):
        for language in ("English", "Korean"):
            for reason in CAUSES:
                with self.subTest(language=language, reason=reason):
                    rows, row = self.rows(language, reason=reason)
                    self.assertEqual(rows[0]["delivery"][0], self.words(language, row))
                    self.assertEqual(rows[0]["delivery"][1], "dim")                # the answer's "전달 대기" color
                    self.assertTrue(rows[0]["delivery"][0].startswith(rows[0]["delivery"][2]))
                    self.assertGreater(len(rows[0]["delivery"][0]), len(rows[0]["delivery"][2]))

    def test_a_row_without_a_reason_reads_the_word_alone_and_the_others_read_as_before(self):
        for language, word, sent in (("English", "awaiting delivery", "sent"), ("Korean", "전달 대기", "보냄")):
            rows, _row = self.rows(language)
            self.assertEqual(rows[0]["delivery"], [word, "dim", word])
            # Gone in, seen, unverified and a row nobody could read: the words they had.
            for state in ("unverified", "queued", "received"):
                rows, _row = self.rows(language, state=state, reason="draft")
                self.assertEqual(rows[0]["delivery"], [sent, "dim"], (language, state))
        from tools.fleet.session_line import _message_delivery
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"}):
            self.assertEqual(_message_delivery("failed", 0, NOW), ["failed", "lvl_r"])
            self.assertEqual(_message_delivery("unknown", 0, NOW), ["not delivered", "lvl_y"])
            self.assertEqual(_message_delivery("sent", 0, NOW), ["sent", "dim"])
            self.assertEqual(_message_delivery("sent", 0, NOW, {"state": "queued"}), ["sent", "dim"])
        rows, _row = self.rows("English", status="received")
        self.assertEqual(rows[0]["delivery"], ["delivered", "dim"])

    def test_only_the_newest_eight_sent_rows_are_read_once_each(self):
        reads = []
        rows, _row = self.rows("English", reason="busy", count=30, reads=reads)
        self.assertEqual(len(rows), 30)
        self.assertEqual(len(reads), 8)
        self.assertEqual(len(set(reads)), 8)
        self.assertEqual(reads[0], "m29")
        reads = []
        self.rows("English", reason="busy", status="received", count=5, reads=reads)
        self.assertEqual(reads, [])                          # a message already received is never read

    def test_an_unreadable_row_reads_as_before(self):
        from tools.fleet.session_line import peer_message_rows
        sent = [{"to": {"harness": "claude", "session_id": "sid-x"}, "summary": "n", "at": NOW, "message_id": "m",
                  "kind": "steer", "status": "sent"}]

        def broken(ref):
            raise OSError("gone")
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"}):
            self.assertEqual(peer_message_rows([], sent, lambda h, s: "6c", now=NOW, pending_of=broken)[0]["delivery"],
                             ["sent", "dim"])
            self.assertEqual(peer_message_rows([], sent, lambda h, s: "6c", now=NOW)[0]["delivery"], ["sent", "dim"])

    def test_the_request_line_reads_the_same_words(self):
        from tools.fleet.session_panel import request_rows
        record = {"schema": "peer-request-v1", "id": "rq-0a1b2c", "title": "review", "created": NOW - 600, "updated": NOW - 60,
                  "requester": {"harness": "claude", "session_id": "sid-a", "name": "a"},
                  "recipient": {"harness": "codex", "session_id": "sid-b", "name": "b"}, "state": "open",
                  "revisions": [{"n": 1, "label": "", "transfer_ref": "a" * 32, "created": NOW - 600,
                                 "state": "current", "followups": []}], "reports": [], "cancel": None, "sending": []}
        for language in ("English", "Korean"):
            for reason in (None, *CAUSES):
                with self.subTest(language=language, reason=reason):
                    row = {"state": "pending", **({"held": {"reason": reason}} if reason else {})}
                    with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": language}):
                        out = request_rows([{"record": record, "side": "out", "gone": False, "body": "",
                                             "rows": {"a" * 32: row}}], NOW, lambda h, s: "6c")[0]
                    whole = self.words(language, row)
                    self.assertEqual(out["status"], whole)
                    self.assertEqual(out["status_head"], whole.split(" · ")[0])
                    self.assertIn(whole, [detail[1] for detail in out["details"]])
        # Gone in or unverified: the words they had; a request already answered keeps its work word.
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"}):
            for state, word in (("unverified", "sent"), ("queued", "delivered")):
                out = request_rows([{"record": record, "side": "out", "gone": False, "body": "",
                                     "rows": {"a" * 32: {"state": state}}}], NOW, lambda h, s: "6c")[0]
                self.assertEqual((out["status"], "status_head" in out), (word, False))

    def test_the_session_page_and_the_now_popup_read_the_same_words(self):
        from tools.fleet.explorer import timeline
        for language in ("English", "Korean"):
            for reason in (None, "draft", "no-shell"):
                row = {"state": "pending", **({"held": {"reason": reason}} if reason else {})}
                sent = [{"to": {"harness": "claude", "session_id": "sid-x", "name": "x"}, "summary": "n", "at": NOW - 5,
                         "message_id": "m1", "kind": "steer", "status": "sent"}]

                class Reader:
                    @staticmethod
                    def _read_pending(ref):
                        return row
                tidy = mock.Mock()
                tidy.prompt_display.return_value = {"received": []}
                with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": language}), \
                        mock.patch.object(timeline, "_sent_records", return_value=sent), \
                        mock.patch.object(timeline, "_peer_message", return_value=Reader), \
                        mock.patch.object(timeline, "_seat", return_value="seat"):
                    out = timeline._message_rows(tidy, "claude", "sid-me", 1, [], NOW)
                with self.subTest(language=language, reason=reason):
                    self.assertEqual(out[0]["delivery"], self.words(language, row))

    def test_the_session_pages_observation_reads_the_pending_rows_of_its_sent_messages_once(self):
        from tools.fleet import details
        from tools.fleet.collectors import peer_messages
        reads = []

        class Reader:
            @staticmethod
            def _read_pending(ref):
                reads.append(ref)
                return {"state": "pending", "held": {"reason": "busy"}}

        class Session:
            harness, session_id, session_aliases = "claude", "sid-me", ()
        records = [{"from": {"harness": "claude", "session_id": "sid-me"}, "to": {"harness": "codex", "session_id": "sid-x"},
                    "summary": "n%d" % n, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 100 + n)),
                    "message_id": "m%d" % n, "kind": "steer", "delivery": {"status": "sent" if n != 3 else "received"}}
                   for n in range(12)]
        with mock.patch.object(peer_messages, "_load_peer_message", return_value=Reader):
            observed = details._peer_requests([Session()], records, now=NOW)
        kept = observed["sent"]["claude:sid-me"]
        self.assertEqual(len(kept), 8)
        self.assertLessEqual(len(reads), 8)
        self.assertEqual(len(reads), len(set(reads)))
        self.assertTrue(all(observed["rows"][m["message_id"]]["state"] == "pending"
                            for m in kept if m["status"] == "sent"))
        self.assertEqual(observed["refs"], [])               # a message is not a request's transfer


if __name__ == "__main__":
    unittest.main()
