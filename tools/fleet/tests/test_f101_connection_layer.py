"""F-101 connection-strip and peer-correlation contract tests."""
import json
import os
import sys
import tempfile
import time
import unittest
from inspect import signature

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from fleet import render
from fleet.collectors import peer_messages


def _rec(frm, to, kind, minutes=1, status="sent", harness="claude"):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes * 60))
    return {"ts": ts, "kind": kind,
            "from": {"harness": harness, "session_id": frm, "name": "sender"},
            "to": {"harness": harness, "session_id": to},
            "delivery": {"status": status}}


class StripContractTest(unittest.TestCase):
    def test_all_connection_strips_accept_width(self):
        for name in ("_subagent_strip", "_gpu_resource_strip", "_peer_link_strip",
                     "_steward_link_strip"):
            self.assertIn("term_width", signature(getattr(render, name)).parameters)

    def test_peer_and_steward_fail_soft_and_fit(self):
        peer = render._peer_link_strip({"from_session_id": "sid", "from_name": "a",
                                        "kind": "handoff", "age_min": 2}, term_width=12)
        self.assertLessEqual(sum(render._dw(t) for t, _ in peer[0]), 12)
        self.assertEqual(render._peer_link_strip({"from_session_id": ""}), [])
        steward = render._steward_link_strip(
            [{"harness": "claude", "session_id": "s%d" % i} for i in range(20)],
            {("claude", "s%d" % i): "%02x" % i for i in range(20)}, term_width=60)
        self.assertLessEqual(sum(render._dw(t) for t, _ in steward[0]), 60)
        self.assertIn("+", "".join(t for t, _ in steward[0]))


class PeerCorrelationTest(unittest.TestCase):
    def _write(self, root, records, sender="source"):
        path = os.path.join(root, "peer-messages", "2026-09")
        os.makedirs(path)
        with open(os.path.join(path, sender + ".jsonl"), "w") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")

    def test_notice_inherits_kind_and_is_not_double_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, [_rec("a", "b", "steer"), _rec("a", "b", "notice")])
            row = peer_messages.collect(state_roots=[tmp])["by_session"][("claude", "b")]
        self.assertEqual(row["recv_1h"], 1)
        self.assertEqual(row["last_recv"]["kind"], "steer")
        self.assertEqual(set(row["last_recv"]),
                         {"from_name", "from_session_id", "from_harness", "kind", "age_min"})

    def test_cross_harness_identity_and_failed_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, [_rec("a", "same", "steer", harness="claude"),
                              _rec("a", "same", "handoff", harness="codex"),
                              _rec("a", "b", "steer", status="failed")])
            result = peer_messages.collect(state_roots=[tmp])
        self.assertIn(("claude", "same"), result["by_session"])
        self.assertIn(("codex", "same"), result["by_session"])
        failed_sender = result["by_session"][("claude", "a")]
        self.assertEqual(failed_sender["sent_1h"], 2)
        self.assertNotIn(("claude", "b"), result["by_session"])


if __name__ == "__main__":
    unittest.main()
