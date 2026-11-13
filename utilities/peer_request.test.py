#!/usr/bin/env python3
"""Peer request tests (stage 2): record, revisions, late reports, cancel, handoff.

Real files under a private state root. Only the carrier's far end is replaced
(which session is calling, address resolution, shell attachment, native queue).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import argparse
import contextlib
import importlib.util
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import peer_request as pr  # noqa: E402
import peer_address  # noqa: E402
import peer_obligations  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("peer_steward_request_test", str(_HERE / "peer-steward.py"))
peer_steward = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = peer_steward
_SPEC.loader.exec_module(peer_steward)
peer_message = peer_steward.peer_message

A = ("claude", "sid-aaaa-1111")   # requester
B = ("codex", "sid-bbbb-2222")    # recipient
B2 = ("codex", "sid-bbbb-3333")   # a lineage-confirmed successor of B, or an unconfirmed namesake
C = ("claude", "sid-cccc-4444")   # requester's successor


class Fixture(unittest.TestCase):
    """A private state root and a switchable "who is calling"."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._environ = dict(os.environ)
        self.addCleanup(self._restore)
        for key in tuple(os.environ):
            if key.startswith(("AGENT_DISPATCH_", "AGENT_SESSION_", "AGENT_THREAD_", "HERDR_")):
                os.environ.pop(key, None)
        for key, value in {
                "AGENT_PEER_LEDGER_ROOT": "peer-state", "HOME": "home", "XDG_STATE_HOME": "xdg-state",
                "XDG_CONFIG_HOME": "xdg-config", "XDG_DATA_HOME": "xdg-data",
                "HARNESS_STATE_ROOT": "harness-state", "HARNESS_DATA_ROOT": "harness-data",
                "AGENT_RUNTIME_ROOT": "runtime", "CODEX_HOME": "home/.codex",
                "CLAUDE_CONFIG_DIR": "home/.claude"}.items():
            os.environ[key] = str(self.root / value)
        (self.root / "jobs.log").touch()
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.root / "jobs.log")
        self.caller = A
        self.successors = {}      # {(harness, sid): (new_sid, new_harness)} confirmed lineage
        self.sent = []            # transports reached (shell/codex/native)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.patch(peer_steward, "_current_session_identity", lambda: (self.caller[1], self.caller[0]))
        self.patch(peer_steward, "_current_shell_binding", lambda h, s: (None, ""))
        self.patch(peer_steward, "_prompt_codex_native_owner", lambda sid: None)
        self.patch(peer_steward, "_prompt_codex_owner_conflict", lambda sid: False)
        self.patch(peer_steward, "_schedule_message_obligation", self._schedule)
        self.patch(peer_steward, "_prompt_carrier_note", lambda *a, **k: None)
        self.patch(peer_steward, "_record", lambda **k: self.records.append(k))
        self.records = []
        self.scheduled = []
        self.patch(peer_address, "resolve", self._resolve)
        self.patch(peer_message, "peer_alias", lambda h, s: "[%02x]" % (sum(map(ord, s)) % 256))
        self.patch(peer_message, "deliver_pending_codex", self._deliver_codex)
        import dispatch_seat_handover as handover
        self.patch(handover, "session_successor", self._successor)
        self.patch(handover, "lineage_graph", lambda **k: mock.Mock(edges=tuple(
            (h, old, new[0]) for (h, old), new in self.successors.items())))

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._environ)

    def patch(self, obj, name, value):
        patcher = mock.patch.object(obj, name, value)
        patcher.start()
        self.stack.callback(patcher.stop)

    def _schedule(self, row, target, *a, **k):
        self.scheduled.append(row["ref"])

    def _resolve(self, target, **kw):
        table = {"[bb]": B, "bee": B, "[aa]": A, "ay": A}
        for name, (harness, sid) in (("bee", B), ("ay", A), ("cee", C), ("bee2", B2)):
            if target in (name, sid):
                return {"harness": harness, "session_id": sid}, None
        return None, "target-not-found"

    def _deliver_codex(self, ref, **kw):
        # The native queue's far end: the row is queued, not received.
        row = peer_message._read_pending(ref)
        if peer_request_withdrawn(row):
            return {"status": "not-required", "reason": "request-withdrawn"}
        self.sent.append(ref)
        with peer_message.pending_lock(ref):
            current = peer_message._read_pending(ref)
            peer_message._save_pending(dict(current, state="queued", receipt="native-queue-accepted"))
        return {"status": "queued", "reason": "native-queue-accepted"}

    def _successor(self, sid, *, harness=None, read_context=None):
        return self.successors.get((harness, sid), ("", ""))

    # --- helpers
    def run_cmd(self, who, func, **fields):
        self.caller = who
        defaults = dict(target=None, text=None, body_file=None, body_stdin=False, title=None, rev=None,
                        cancel=False, reason=None, rq=None, partial=False, reject=None, result=None,
                        remaining=None, artifact=None, file=None, report=None, all=False)
        defaults.update(fields)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = func(argparse.Namespace(**defaults))
        return rc, out.getvalue()

    def request(self, who, target, text="please review", **kw):
        return self.run_cmd(who, peer_steward.cmd_request, target=target, text=text, **kw)

    def report(self, who, rq=None, **kw):
        return self.run_cmd(who, peer_steward.cmd_report, rq=rq, **kw)

    def show(self, who, rq=None, **kw):
        return self.run_cmd(who, peer_steward.cmd_show, rq=rq, **kw)

    def new_request(self, text="please review the change", **kw):
        rc, out = self.request(A, "bee", text, **kw)
        self.assertEqual(rc, 0, out)
        request_id = out.split()[0].split("=")[1]
        return request_id, out

    def ack(self, ref, who=B):
        """The exact receiver's ACK (what the native receive path writes)."""
        with peer_message.pending_lock(ref):
            row = peer_message._read_pending(ref)
            transfer = peer_message._read_transfer_record(ref)
            evidence = peer_message._exact_receiver_evidence(
                ref, transfer, row, {"harness": who[0], "session_id": who[1]})
            if evidence is None:
                return False
            peer_message._save_pending(dict(row, state="received", receipt="exact-native-history",
                                            received_at=time.time(), text=""))
            return True


def peer_request_withdrawn(row):
    return peer_message._request_withdrawn(row)


class RecordAndFlowTest(Fixture):
    def test_a_delivery_complete_leaves_the_request_open_for_both_sides(self):
        request_id, out = self.new_request()
        self.assertIn("delivery=queued-not-delivered", out.split("\n")[0].replace("delivery=delivered", ""))
        ref = pr.load(request_id)["revisions"][0]["transfer_ref"]
        self.assertTrue(self.ack(ref))
        self.assertEqual(peer_message._read_pending(ref)["state"], "received")
        self.assertEqual(pr.load(request_id)["state"], "open")
        self.assertEqual([r["id"] for r in pr.session_requests(*A)["out"]], [request_id])
        self.assertEqual([r["id"] for r in pr.session_requests(*B)["in"]], [request_id])

    def test_wire_is_one_line_and_the_body_is_only_in_the_sealed_payload(self):
        request_id, out = self.new_request("BODY-1-nonce\nsecond line", title="검토 TITLE-nonce")
        ref = pr.load(request_id)["revisions"][0]["transfer_ref"]
        row = peer_message._read_pending(ref)
        self.assertNotIn("BODY-1-nonce", row["text"])
        self.assertIn("[요청 %s]" % request_id, row["text"])
        self.assertIn("show %s" % request_id, row["text"])
        self.assertEqual(peer_message.read_peer_payload(ref)["body"], "BODY-1-nonce\nsecond line")
        rc, shown = self.show(B, request_id)
        self.assertEqual(rc, 0, shown)
        self.assertIn("BODY-1-nonce", shown)

    def test_b_partial_keeps_open_final_closes_reject_carries_reason(self):
        request_id, _ = self.new_request()
        rc, out = self.report(B, request_id, partial=True, result="half done")
        self.assertEqual(rc, 0, out)
        self.assertIn("state=partial", out)
        self.assertEqual(pr.load(request_id)["state"], "partial")
        rc, out = self.report(B, request_id, result="all done")
        self.assertIn("state=final", out)
        self.assertEqual(pr.load(request_id)["state"], "final")
        other, _ = self.new_request("second")
        rc, out = self.report(B, other, reject="out of scope")
        self.assertEqual(pr.load(other)["state"], "rejected")
        line = peer_message._read_pending(pr.load(other)["reports"][0]["transfer_ref"])["text"]
        self.assertIn("거절", line)
        self.assertIn("out of scope", line)

    def test_report_reaches_the_requester_as_one_line_and_names_the_report(self):
        request_id, _ = self.new_request()
        self.report(B, request_id, result="수정 후 병합 - head 7403b0a")
        record = pr.load(request_id)
        row = peer_message._read_pending(record["reports"][0]["transfer_ref"])
        self.assertEqual((row["to"]["harness"], row["to"]["session_id"]), A)
        self.assertIn("[요청 답 %s]" % request_id, row["text"])
        self.assertEqual(record["reports"][0]["reporter"]["session_id"], B[1])
        kind = [r for r in self.records if r["kind"] == "report"]
        self.assertTrue(kind)


if __name__ == "__main__":
    unittest.main()
