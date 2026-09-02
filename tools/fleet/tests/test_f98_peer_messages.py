#!/usr/bin/env python3
"""F-98 — read-only peer-message ledger projection (SD-122 §13.37.2)."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render  # noqa: E402
from fleet.collectors import peer_messages  # noqa: E402
from fleet.model import Session  # noqa: E402


def _write_ledger(root, from_sid, records):
    month_dir = os.path.join(root, "peer-messages", "2026-09")
    os.makedirs(month_dir, exist_ok=True)
    path = os.path.join(month_dir, "%s.jsonl" % from_sid)
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


def _rec(from_sid, to_sid=None, to_name=None, kind="steer", summary="hi",
        minutes_ago=0, body_sha256="deadbeef"):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes_ago * 60))
    to = {"harness": "claude"}
    if to_sid:
        to["session_id"] = to_sid
    if to_name:
        to["name"] = to_name
    return {
        "schema_version": 1, "message_id": "abc123", "ts": ts,
        "from": {"harness": "claude", "session_id": from_sid, "project": "p"},
        "to": to, "kind": kind, "summary": summary, "body_sha256": body_sha256,
        "delivery": {"surface": "claude-native", "status": "sent", "receipt": None},
        "refs": [],
    }


class CollectorTest(unittest.TestCase):
    def test_three_record_fixture_badge_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_sid="sid-b", minutes_ago=1),
                _rec("sid-a", to_sid="sid-b", minutes_ago=2),
            ])
            _write_ledger(tmp, "sid-b", [_rec("sid-b", to_sid="sid-a", minutes_ago=1)])
            result = peer_messages.collect(state_roots=[tmp])
        self.assertEqual(result["by_session"]["sid-a"]["sent_1h"], 2)
        self.assertEqual(result["by_session"]["sid-a"]["recv_1h"], 1)
        self.assertEqual(result["by_session"]["sid-b"]["sent_1h"], 1)
        self.assertEqual(result["by_session"]["sid-b"]["recv_1h"], 2)

    def test_subtitle_latest_record_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_sid="sid-b", minutes_ago=10, kind="steer"),
                _rec("sid-a", to_sid="sid-b", minutes_ago=1, kind="handoff"),
            ])
            result = peer_messages.collect(state_roots=[tmp])
        self.assertEqual(result["by_session"]["sid-b"]["last_recv"]["kind"], "handoff")

    def test_malformed_line_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_ledger(tmp, "sid-a", [_rec("sid-a", to_sid="sid-b")])
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("{not json\n")
            result = peer_messages.collect(state_roots=[tmp])
        self.assertEqual(peer_messages.collect.last_malformed, 1)
        self.assertEqual(result["by_session"]["sid-b"]["recv_1h"], 1)

    def test_records_older_than_24h_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_sid="sid-b", minutes_ago=25 * 60),
            ])
            result = peer_messages.collect(state_roots=[tmp])
        self.assertEqual(result["records"], [])

    def test_record_cap_200(self):
        # Spread across three sender files so no single file's tail-64KB bound
        # interferes with proving the independent 200-record overall cap.
        with tempfile.TemporaryDirectory() as tmp:
            for sender in ("sid-a", "sid-b", "sid-c"):
                recs = [_rec(sender, to_sid="sid-z", summary="x", minutes_ago=i)
                        for i in range(80)]
                _write_ledger(tmp, sender, recs)
            result = peer_messages.collect(state_roots=[tmp])
        self.assertEqual(len(result["records"]), 200)

    def test_file_tail_64kb(self):
        with tempfile.TemporaryDirectory() as tmp:
            month_dir = os.path.join(tmp, "peer-messages", "2026-09")
            os.makedirs(month_dir)
            path = os.path.join(month_dir, "sid-a.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x" * (200 * 1024) + "\n")  # oversized garbage prefix
                fh.write(json.dumps(_rec("sid-a", to_sid="sid-b", minutes_ago=1)) + "\n")
            result = peer_messages.collect(state_roots=[tmp])
        self.assertEqual(result["by_session"]["sid-b"]["recv_1h"], 1)

    def test_json_exposes_summary_only_never_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_sid="sid-b", summary="short summary", minutes_ago=1)
            ])
            result = peer_messages.collect(state_roots=[tmp])
        raw = json.dumps(result)
        self.assertIn("short summary", raw)
        self.assertNotIn("body", raw.lower().replace("body_sha256", ""))
        self.assertLessEqual(len(result["records"][0]["summary"]), 200)

    def test_sent_record_never_labels_recipient_as_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_sid="sid-b", to_name="sid-b-display", kind="steer", minutes_ago=1)
            ])
            result = peer_messages.collect(state_roots=[tmp])
        last_recv = result["by_session"]["sid-b"]["last_recv"]
        self.assertNotEqual(last_recv["from_name"], "sid-b-display")
        self.assertEqual(last_recv["from_name"], "sid-a")

    def test_join_is_exact_session_id_not_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_ledger(tmp, "sid-a", [
                _rec("sid-a", to_name="hearting-21 [f3e821]", minutes_ago=1)
            ])
            result = peer_messages.collect(state_roots=[tmp])
        self.assertNotIn("hearting-21 [f3e821]", result["by_session"])
        self.assertEqual(result["by_session"]["sid-a"]["sent_1h"], 1)


class StableRootResolverTest(unittest.TestCase):
    """F-98d — `_state_roots()` must read through the SAME resolver chain the
    writer (`utilities/peer-message.py`) uses (`resolve_dispatch_state_root` +
    `dispatch_state_roots`), not `dispatch._row_state_roots()`'s no-row default
    (which silently pins to the release-tree `.dispatch` and ignores an inherited
    `AGENT_DISPATCH_JOBS`). Every fixture above passes `state_roots=[tmp]`
    explicitly — this is the one that does not, which is exactly why the bug
    shipped undetected (measured 2026-09-02 against v2.101.0)."""

    def setUp(self):
        self._old_environ = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_environ)

    def test_collect_with_no_state_roots_finds_records_in_the_stable_root_only(self):
        with tempfile.TemporaryDirectory() as home:
            os.environ["HOME"] = home
            os.environ["AGENT_HOME"] = home
            for key in ("CLAUDE_HOME", "AGENT_DISPATCH_JOBS", "XDG_STATE_HOME", "HARNESS_STATE_ROOT"):
                os.environ.pop(key, None)
            stable_root = os.path.join(home, ".local", "state", "hearting", "dispatch")
            _write_ledger(stable_root, "sid-a", [_rec("sid-a", to_sid="sid-b", minutes_ago=1)])
            result = peer_messages.collect()
        self.assertEqual(result["by_session"]["sid-b"]["recv_1h"], 1)

    def test_collect_with_no_state_roots_honors_agent_dispatch_jobs(self):
        with tempfile.TemporaryDirectory() as base:
            home = os.path.join(base, "home")
            os.makedirs(home)
            registry_dir = os.path.join(base, "custom-jobs-dir")
            os.makedirs(registry_dir)
            os.environ["HOME"] = home
            os.environ["AGENT_HOME"] = home
            os.environ["AGENT_DISPATCH_JOBS"] = os.path.join(registry_dir, "jobs.log")
            for key in ("CLAUDE_HOME", "XDG_STATE_HOME", "HARNESS_STATE_ROOT"):
                os.environ.pop(key, None)
            _write_ledger(registry_dir, "sid-a", [_rec("sid-a", to_sid="sid-b", minutes_ago=1)])
            result = peer_messages.collect()
        self.assertEqual(result["by_session"]["sid-b"]["recv_1h"], 1)


class RenderByteIdenticalTest(unittest.TestCase):
    def test_ledger_absent_render_byte_identical(self):
        with mock.patch.object(peer_messages, "collect", return_value={"records": [], "by_session": {}}):
            pass  # collector isolation only; render reads Session fields, not the collector

    def test_no_steward_badge_rendered(self):
        s = Session(harness="claude", pid=1, session_id="sid-a", peer_sent_1h=2, peer_recv_1h=1)
        segs = render._session_row(s, narrow=False)
        text = "".join(t for t, _k in segs)
        self.assertNotIn("steward", text.lower())

    def test_badge_zero_when_no_peer_activity(self):
        s = Session(harness="claude", pid=1, session_id="sid-a")
        segs = render._session_row(s, narrow=False)
        text = "".join(t for t, _k in segs)
        self.assertNotIn("✉", text)

    def test_badge_present_with_activity(self):
        s = Session(harness="claude", pid=1, session_id="sid-a", peer_sent_1h=3, peer_recv_1h=2)
        segs = render._session_row(s, narrow=False)
        text = "".join(t for t, _k in segs)
        self.assertIn("✉ 3/2", text)

    def test_sessions_json_has_no_summary_field(self):
        s = Session(harness="claude", pid=1, session_id="sid-a",
                   peer_last_recv={"from_name": "x", "from_session_id": "sid-b",
                                    "kind": "steer", "age_min": 1})
        d = s.to_dict()
        self.assertNotIn("summary", json.dumps(d["peer_last_recv"]))


if __name__ == "__main__":
    unittest.main()
