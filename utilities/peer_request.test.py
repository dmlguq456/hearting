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
D = ("opencode", "sid-dddd-5555")  # an OpenCode receiver (same guidance string)
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
        self._orig_schedule = peer_steward._schedule_message_obligation
        self.patch(peer_steward, "_schedule_message_obligation", self._schedule)
        self.patch(peer_steward, "_prompt_carrier_note", lambda *a, **k: None)
        self.patch(peer_steward, "_record", lambda **k: self.records.append(k))
        self.records = []
        self.scheduled = []
        self.hold = False
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
        for name, (harness, sid) in (("bee", B), ("ay", A), ("cee", C), ("bee2", B2), ("dee", D)):
            if target in (name, sid):
                return {"harness": harness, "session_id": sid}, None
        return None, "target-not-found"

    def _deliver_codex(self, ref, **kw):
        # The native queue's far end: the row is queued, not received.
        row = peer_message._read_pending(ref)
        if peer_request_withdrawn(row):
            return {"status": "not-required", "reason": "request-withdrawn"}
        if self.hold:
            return {"status": "unverified", "reason": "native-queue-held"}
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
        self.assertIn("delivery=queued-not-delivered", out)
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


class RevisionAndCancelTest(Fixture):
    def test_c_late_report_stays_on_its_revision(self):
        request_id, _ = self.new_request(title="t", rev="판1")
        ref1 = pr.load(request_id)["revisions"][0]["transfer_ref"]
        self.ack(ref1)
        rc, out = self.request(A, request_id, "new head", rev="판2")
        self.assertEqual(rc, 0, out)
        self.assertIn("revision=2 supersedes=1", out)
        # B received only revision 1, so its report answers revision 1.
        rc, out = self.report(B, request_id, result="old verdict")
        self.assertIn("late=true", out)
        record = pr.load(request_id)
        self.assertEqual(record["state"], "open")
        self.assertEqual(record["revisions"][0]["state"], "superseded")
        self.assertTrue(record["reports"][0]["late"])
        line = peer_message._read_pending(record["reports"][0]["transfer_ref"])["text"]
        self.assertIn("판 1(대체됨)", line)
        rc, out = self.report(B, request_id, result="new verdict", rev=2)
        self.assertEqual(pr.load(request_id)["state"], "final")
        self.assertFalse(pr.load(request_id)["reports"][1]["late"])

    def test_d_superseded_pending_revision_is_not_sent_anywhere(self):
        self.hold = True
        request_id, _ = self.new_request(title="t", rev="판1")
        ref1 = pr.load(request_id)["revisions"][0]["transfer_ref"]
        self.assertEqual(peer_message._read_pending(ref1)["state"], "pending")
        self.hold = False
        rc, out = self.request(A, request_id, "second", rev="판2")
        self.assertEqual(rc, 0, out)
        row1 = peer_message._read_pending(ref1)
        self.assertTrue(peer_message._request_withdrawn(row1))
        # The native-queue carrier and the hook/plugin claim both refuse it.
        self.assertEqual(peer_message.deliver_pending_codex.__name__, "_deliver_codex")
        self.assertIsNone(peer_message._reserve_pending_herdr(
            ref1, {"harness": B[0], "session_id": B[1]}, receipt="opencode-prompt-inflight"))
        self.assertEqual(peer_message._read_pending(ref1)["state"], "pending")
        ref2 = pr.load(request_id)["revisions"][1]["transfer_ref"]
        self.assertFalse(peer_message._request_withdrawn(peer_message._read_pending(ref2)))

    def test_d_claude_hook_context_shows_only_the_current_revision(self):
        rc, out = self.request(A, "cee", "first", title="t", rev="판1")
        request_id = out.split()[0].split("=")[1]
        rc, out = self.request(A, request_id, "second", rev="판2")
        stream = io.StringIO()
        with mock.patch.object(peer_message, "_NATIVE_BOUNDARY_EVENTS", frozenset()):
            peer_message.emit_peer_context({"harness": C[0], "session_id": C[1]}, "UserPromptSubmit",
                                           stream=stream)
        text = stream.getvalue()
        self.assertEqual(text.count("[요청 %s]" % request_id), 1, text)

    def test_runner_closes_a_withdrawn_revision_without_a_sender_notice(self):
        self.hold = True
        with mock.patch.object(peer_obligations, "ensure_normal_runner"):
            self.patch(peer_steward, "_schedule_message_obligation", self._real_schedule)
            request_id, _ = self.new_request(title="t", rev="판1")
            ref1 = pr.load(request_id)["revisions"][0]["transfer_ref"]
            self.hold = False
            self.request(A, request_id, "second", rev="판2")
            store = peer_obligations.ObligationStore()
            duty = store.get("message-" + ref1)
            self.assertIsNotNone(duty)
            with mock.patch.object(peer_steward, "_ensure_sender_delay_notice") as delay:
                peer_steward._resume_message_obligation_scoped(duty, store)
            delay.assert_not_called()
            closed = store.get("message-" + ref1)
            self.assertEqual(closed["state"], "cancelled")
            self.assertEqual(closed["observation"]["reason"], "request-withdrawn")
            self.assertEqual(peer_message._read_pending(ref1)["state"], "pending")

    def _real_schedule(self, row, target, *a, **k):
        return self._orig_schedule(row, target, *a, **k)

    def test_e_cancel_after_delivery_sends_a_read_only_notice_and_keeps_receipt(self):
        request_id, _ = self.new_request(title="t")
        ref = pr.load(request_id)["revisions"][0]["transfer_ref"]
        self.ack(ref)
        rc, out = self.request(A, request_id, cancel=True, reason="not needed")
        self.assertIn("state=cancelled", out)
        self.assertIn("notice=", out)
        record = pr.load(request_id)
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(record["revisions"][0]["state"], "cancelled")
        self.assertEqual(peer_message._read_pending(ref)["state"], "received")
        notice = peer_message._read_pending(record["cancel"]["notice_ref"])
        self.assertIn("[요청 취소 %s]" % request_id, notice["text"])
        self.assertNotIn("멈", notice["text"])
        self.assertEqual(peer_request_kind(notice), ("notice", True))

    def test_cancel_before_delivery_needs_no_notice(self):
        self.hold = True
        request_id, _ = self.new_request(title="t")
        ref = pr.load(request_id)["revisions"][0]["transfer_ref"]
        rc, out = self.request(A, request_id, cancel=True)
        self.assertIn("notice=not-needed", out)
        self.assertTrue(peer_message._request_withdrawn(peer_message._read_pending(ref)))

    def test_cancel_is_the_requesters_alone_and_closed_requests_refuse(self):
        request_id, _ = self.new_request(title="t")
        rc, out = self.request(B, request_id, cancel=True)
        self.assertEqual(rc, 1)
        self.assertIn("not-the-requester", out)
        self.report(B, request_id, result="done")
        rc, out = self.request(A, request_id, cancel=True)
        self.assertIn("request-closed", out)


class HandoffAndAddressTest(Fixture):
    def test_f_confirmed_successor_inherits_the_open_work_and_unsent_revision(self):
        self.hold = True
        request_id, _ = self.new_request(title="t")
        ref = pr.load(request_id)["revisions"][0]["transfer_ref"]
        self.successors[B] = (B2[1], B2[0])
        self.assertEqual([r["id"] for r in pr.session_requests(*B2)["in"]], [request_id])
        self.assertEqual(pr.session_requests(*B)["in"], [])
        row = peer_message._read_pending(ref)
        self.assertEqual(peer_message.pending_delivery_recipient(row)["session_id"], B2[1])
        rc, out = self.report(B2, request_id, result="done by successor")
        self.assertEqual(rc, 0, out)
        self.assertEqual(pr.load(request_id)["state"], "final")
        self.assertEqual(pr.load(request_id)["reports"][0]["reporter"]["session_id"], B2[1])

    def test_f_report_goes_to_the_requesters_confirmed_successor(self):
        request_id, _ = self.new_request(title="t")
        self.successors[A] = (C[1], C[0])
        self.report(B, request_id, result="done")
        row = peer_message._read_pending(pr.load(request_id)["reports"][0]["transfer_ref"])
        self.assertEqual(row["to"]["session_id"], C[1])
        self.assertEqual([r["id"] for r in pr.session_requests(*C)["out"]], [request_id])

    def test_g_an_unconfirmed_namesake_neither_inherits_nor_may_report(self):
        request_id, _ = self.new_request(title="t")
        self.assertEqual(pr.session_requests(*B2)["in"], [])
        rc, out = self.report(B2, request_id, result="x")
        self.assertEqual(rc, 1)
        self.assertIn("reporter-not-recipient", out)
        self.assertEqual(pr.load(request_id)["reports"], [])
        rc, out = self.show(B2, request_id)
        self.assertIn("not-a-party", out)

    def test_g_an_unreadable_lineage_leaves_the_request_on_its_session(self):
        request_id, _ = self.new_request(title="t")
        with mock.patch("dispatch_seat_handover.session_successor", side_effect=OSError("unreadable")):
            self.assertEqual([r["id"] for r in pr.session_requests(*B)["in"]], [request_id])
            self.assertEqual(pr.current_recipient(pr.load(request_id))["session_id"], B[1])

    def test_o_ack_after_a_lineage_change_is_refused_for_the_old_session(self):
        self.hold = True
        request_id, _ = self.new_request(title="t")
        ref = pr.load(request_id)["revisions"][0]["transfer_ref"]
        self.successors[B] = (B2[1], B2[0])
        self.assertFalse(self.ack(ref, B))
        self.assertEqual(peer_message._read_pending(ref)["state"], "pending")
        self.assertTrue(self.ack(ref, B2))

    def test_h_unresolved_or_ambiguous_address_makes_no_record(self):
        rc, out = self.request(A, "nobody-home", "x")
        self.assertEqual(rc, 5)
        self.assertIn("request=- delivery=refused reason=target-not-found", out)
        with mock.patch.object(peer_address, "resolve", return_value=(None, "target-ambiguous")):
            rc, out = self.request(A, "dup", "x")
        self.assertIn("target-ambiguous", out)
        self.assertFalse(list((self.root / "peer-state" / "peer-messages").glob("requests/rq-*.json")))
        self.assertFalse(list((self.root / "peer-state" / "peer-messages").glob("pending/*.json")))

    def test_request_to_self_and_empty_body_are_refused(self):
        rc, out = self.request(A, "ay", "x")
        self.assertIn("request-to-self", out)
        rc, out = self.request(A, "bee", "  ")
        self.assertIn("empty-body", out)

    def test_j_report_without_an_id(self):
        first, _ = self.new_request(title="one")
        rc, out = self.report(B, result="done")
        self.assertEqual(rc, 0, out)
        self.assertEqual(pr.load(first)["state"], "final")
        a, _ = self.new_request(title="two")
        b, _ = self.new_request(title="three")
        rc, out = self.report(B, result="done")
        self.assertEqual(rc, 1)
        self.assertIn("request-ambiguous", out)
        self.assertIn(a, out)
        self.assertIn(b, out)
        rc, out = self.report(B, a, result="done")
        self.assertEqual(rc, 0, out)
        rc, out = self.report(B2, result="x")
        self.assertIn("no-open-request", out)


class SealedBodyTest(Fixture):
    def test_i_show_reads_the_sealed_revision_even_if_the_source_file_changes(self):
        source = self.root / "body.txt"
        source.write_text("BODY-file-v1\nline two\n", encoding="utf-8")
        rc, out = self.request(A, "bee", title="t", text=None, body_file=str(source))
        self.assertEqual(rc, 0, out)
        request_id = out.split()[0].split("=")[1]
        source.write_text("CHANGED", encoding="utf-8")
        source.unlink()
        rc, shown = self.show(B, request_id)
        self.assertEqual(rc, 0, shown)
        self.assertIn("BODY-file-v1", shown)
        self.assertIn("line two", shown)
        self.assertEqual(pr.load(request_id)["state"], "open")

    def test_i_unreadable_body_is_named_and_recorded_once_and_the_request_stays_open(self):
        request_id, _ = self.new_request("sealed body", title="t")
        ref = pr.load(request_id)["revisions"][0]["transfer_ref"]
        payload = peer_message._payload_path(ref)
        payload.chmod(0o600)
        payload.write_text("tampered", encoding="utf-8")
        rc, shown = self.show(B, request_id)
        self.assertEqual(rc, 1)
        self.assertIn("body=unreadable", shown)
        record = pr.load(request_id)
        self.assertEqual(record["state"], "open")
        self.assertEqual([e["event"] for e in record["events"]].count("body_unreadable"), 1)

    def test_show_without_an_id_lists_open_work_by_direction(self):
        request_id, _ = self.new_request(title="the title")
        rc, listing = self.show(B)
        self.assertIn("◂ %s open" % request_id, listing)
        rc, listing = self.show(A)
        self.assertIn("→ %s open" % request_id, listing)
        self.report(B, request_id, result="done")
        self.assertIn("requests=none", self.show(B)[1])

    def test_k_guidance_names_show_and_report_without_the_body_for_every_harness(self):
        texts = {}
        for name, who in (("codex", B), ("claude", C), ("opencode", D)):
            rc, out = self.request(A, {"codex": "bee", "claude": "cee", "opencode": "dee"}[name],
                                   "SECRET-BODY-nonce", title="t")
            request_id = out.split()[0].split("=")[1]
            record = pr.load(request_id)
            row = peer_message._read_pending(record["revisions"][0]["transfer_ref"])
            guidance = peer_message.receiver_guidance(
                row["text"], {"harness": who[0], "session_id": who[1]})
            self.assertIn("show %s" % request_id, guidance)
            self.assertIn("report %s" % request_id, guidance)
            self.assertNotIn("SECRET-BODY-nonce", guidance)
            self.assertNotIn("SECRET-BODY-nonce", row["text"])
            texts[name] = guidance.replace(request_id, "RQ")
        self.assertEqual(len(set(texts.values())), 1, texts)

    def test_k_reports_and_cancel_notices_are_read_only_guidance(self):
        request_id, _ = self.new_request(title="t")
        self.report(B, request_id, result="done")
        record = pr.load(request_id)
        row = peer_message._read_pending(record["reports"][0]["transfer_ref"])
        guidance = peer_message.receiver_guidance(row["text"], {"harness": A[0], "session_id": A[1]})
        self.assertIn("read only", guidance)
        self.assertNotIn("Reply once", guidance)

    def test_request_rows_have_no_inline_fallback_for_a_non_codex_receiver(self):
        rc, out = self.request(A, "cee", "NO-INLINE-nonce\n" * 400, title="t")
        request_id = out.split()[0].split("=")[1]
        row = peer_message._read_pending(pr.load(request_id)["revisions"][0]["transfer_ref"])
        guidance = peer_message.receiver_guidance(row["text"], {"harness": C[0], "session_id": C[1]})
        self.assertNotIn("NO-INLINE-nonce", guidance)
        self.assertLess(len(row["text"].encode()), 1500)


class KindAndRetentionTest(Fixture):
    CASES = (("[steer] go", "steer", False), ("[handoff] card", "handoff", False),
             ("[gate] ask", "gate-relay", False), ("[report] done", "report", True),
             ("[notice] fyi", "notice", True), ("plain words", "steer", False),
             ("[요청 rq-aaaaaa] x", "request", False), ("[요청 답 rq-aaaaaa] x", "report", True),
             ("[요청 취소 rq-aaaaaa] x", "notice", True))

    def test_m_one_table_gives_the_same_kind_to_ledger_carrier_and_hook(self):
        import codex_turn_carrier as carrier
        hook_spec = importlib.util.spec_from_file_location(
            "peer_message_record_hook", str(_HERE.parent / "hooks" / "peer-message-record.py"))
        hook = importlib.util.module_from_spec(hook_spec)
        hook_spec.loader.exec_module(hook)
        for text, kind, read_only in self.CASES:
            self.assertEqual(peer_steward._prompt_message_kind(text), kind, text)
            self.assertEqual(hook._kind_for_body(text, False), kind, text)
            self.assertEqual(pr.message_kind(text), (kind, read_only), text)
            item = carrier.peer_body_item(text)
            self.assertEqual(item is None, read_only, text)
        sealed = {"request": pr.request_seal("rq-aaaaaa", 1, "report")}
        self.assertEqual(pr.message_kind("plain words", sealed), ("report", True))
        sealed = {"request": pr.request_seal("rq-aaaaaa", 1, "request")}
        self.assertEqual(pr.message_kind("[report] looks like one", sealed), ("request", False))

    def test_l_retention_removes_only_closed_records_older_than_fourteen_days(self):
        old_open, _ = self.new_request(title="open")
        closed, _ = self.new_request(title="closed")
        self.report(B, closed, result="done")
        for request_id in (old_open, closed):
            record = pr.load(request_id)
            record["updated"] = time.time() - 40 * 86400
            pr._atomic_write(pr._record_path(request_id), record)
        removed = pr.prune()
        self.assertEqual(removed, [closed])
        self.assertIsNotNone(pr.load(old_open))
        self.assertEqual([r["id"] for r in pr.session_requests(*A)["out"]], [old_open])
        recent, _ = self.new_request(title="recent")
        self.report(B, recent, result="done")
        self.assertEqual(pr.prune(), [])

    def test_l_index_and_record_disagree_the_record_wins(self):
        request_id, _ = self.new_request(title="t")
        pr._index_update(*C, "in", request_id, add=True)
        pr._index_update(*C, "in", "rq-ffffff", add=True)
        self.assertEqual(pr.session_requests(*C)["in"], [])
        self.assertEqual([r["id"] for r in pr.session_requests(*B)["in"]], [request_id])


class FailureBoundaryTest(Fixture):
    def _fail_seal(self):
        patch = mock.patch.object(peer_message, "_write_body_payload", side_effect=OSError("disk"))
        patch.start()
        self.addCleanup(patch.stop)
        return patch

    def test_n_a_failed_seal_leaves_no_record_and_no_index(self):
        patch = self._fail_seal()
        rc, out = self.request(A, "bee", "x", title="t")
        self.assertNotEqual(rc, 0)
        self.assertTrue(out.startswith("request=- delivery=refused"), out)
        self.assertEqual(list((self.root / "peer-state" / "peer-messages" / "requests").glob("rq-*.json")), [])
        self.assertEqual(pr.session_requests(*A)["out"], [])

    def test_n_a_failed_new_revision_seal_keeps_the_current_one(self):
        request_id, _ = self.new_request(title="t", rev="판1")
        patch = self._fail_seal()
        rc, out = self.request(A, request_id, "second", rev="판2")
        self.assertNotEqual(rc, 0)
        self.assertIn("delivery=refused", out)
        record = pr.load(request_id)
        self.assertEqual(len(record["revisions"]), 1)
        self.assertEqual(record["revisions"][0]["state"], "current")

    def test_n_a_failed_report_seal_leaves_the_state(self):
        request_id, _ = self.new_request(title="t")
        patch = self._fail_seal()
        rc, out = self.report(B, request_id, result="done")
        self.assertNotEqual(rc, 0)
        self.assertEqual(pr.load(request_id)["state"], "open")
        self.assertEqual(pr.load(request_id)["reports"], [])

    def test_n_a_queued_report_is_already_final_and_exit_zero(self):
        self.new_request(title="t")
        request_id = pr.session_requests(*B)["in"][0]["id"]
        self.hold = False
        rc, out = self.report(B, request_id, result="done")
        self.assertEqual(rc, 0)
        self.assertIn("state=final", out)
        self.assertIn("delivery=queued-not-delivered", out)

    def test_n_first_line_is_the_delivery_word_and_preparing_ids_are_incomplete(self):
        request_id, out = self.new_request(title="t")
        self.assertRegex(out.split()[1], r"^delivery=(delivered|queued-not-delivered)$")
        dangling = pr.reserve(pr.load(request_id)["requester"], pr.load(request_id)["recipient"], title="x")
        self.assertIn("request-incomplete", self.show(B, dangling)[1])
        self.assertIn("request-incomplete", self.report(B, dangling, result="x")[1])
        self.assertEqual([r["id"] for r in pr.session_requests(*B)["in"]], [request_id])


class LockOrderTest(Fixture):
    def test_o_request_lock_is_outermost_and_never_held_across_a_send(self):
        held_when_sending, nested = [], []
        original_send = peer_steward._cmd_prompt_native
        original_lock = pr.request_lock

        def send(*a, **k):
            held_when_sending.append(list(pr._LOCKS_HELD))
            return original_send(*a, **k)

        @contextlib.contextmanager
        def pending_probe(key="prepare"):
            with original_pending(key):
                yield
        original_pending = peer_message.pending_lock

        def request_probe(key):
            # A request (or index) lock taken while a pending lock is held is the inversion.
            if getattr(self, "_pending_depth", 0):
                nested.append(key)
            return original_lock(key)

        @contextlib.contextmanager
        def counting_pending(key="prepare"):
            self._pending_depth = getattr(self, "_pending_depth", 0) + 1
            try:
                with original_pending(key):
                    yield
            finally:
                self._pending_depth -= 1

        self.patch(peer_steward, "_cmd_prompt_native", send)
        self.patch(peer_message, "pending_lock", counting_pending)
        self.patch(pr, "request_lock", request_probe)
        request_id, _ = self.new_request(title="t", rev="판1")
        self.request(A, request_id, "second", rev="판2")
        self.report(B, request_id, result="old", rev=1)
        self.report(B, request_id, result="new", rev=2)
        other, _ = self.new_request(title="u")
        self.request(A, other, cancel=True)
        self.assertTrue(held_when_sending)
        self.assertEqual([held for held in held_when_sending if held], [])
        self.assertEqual(nested, [])


class RealLineageTest(Fixture):
    """The confirmed-successor rule against Hearting's own lineage rows, not a replaced reader."""

    def setUp(self):
        super().setUp()
        self.stack.close()  # undo the Fixture's lineage replacements; keep the carrier ones
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("_current_session_identity", lambda: (self.caller[1], self.caller[0])),
                            ("_current_shell_binding", lambda h, s: (None, "")),
                            ("_prompt_codex_native_owner", lambda sid: None),
                            ("_prompt_codex_owner_conflict", lambda sid: False),
                            ("_schedule_message_obligation", self._schedule),
                            ("_prompt_carrier_note", lambda *a, **k: None),
                            ("_record", lambda **k: self.records.append(k))):
            self.patch(peer_steward, name, value)
        self.patch(peer_address, "resolve", self._resolve)
        self.patch(peer_message, "peer_alias", lambda h, s: "[%02x]" % (sum(map(ord, s)) % 256))
        self.patch(peer_message, "deliver_pending_codex", self._deliver_codex)
        self.hold = True

    def write_succession(self, harness, old, new):
        directory = Path(os.environ["XDG_STATE_HOME"]) / "hearting" / "session-tidy" / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / "seat-1.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "succession", "source": "tidy-clear", "harness": harness,
                                     "from": old, "sid": new, "ts": 1}) + "\n")

    def test_a_session_tidy_succession_row_moves_the_request_and_nothing_else_does(self):
        request_id, _ = self.new_request(title="t")
        self.assertEqual(pr.session_requests(*B2)["in"], [])
        self.write_succession(B[0], B[1], B2[1])
        self.assertEqual([r["id"] for r in pr.session_requests(*B2)["in"]], [request_id])
        rc, out = self.report(B2, request_id, result="from the successor")
        self.assertEqual(rc, 0, out)
        stranger = ("codex", "sid-bbbb-9999")
        rc, out = self.report(stranger, request_id, result="x")
        self.assertIn("reporter-not-recipient", out)


def peer_request_kind(row):
    transfer = peer_message._read_transfer_record(row["ref"])
    return pr.message_kind(row.get("text") or "", transfer)


if __name__ == "__main__":
    unittest.main()
