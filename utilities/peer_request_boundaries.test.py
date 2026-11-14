#!/usr/bin/env python3
"""Peer request boundaries (PR #1623 review): a writer killed mid-write, many open
requests, two writers at once, and a cancel racing the hand-over.

Each test runs the real commands over real files in the `peer_request.test.py`
fixture (a private state root; only the carrier's far end is replaced). A killed
writer is a forked child that SIGKILLs itself at the named point.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import importlib.util
import json
import signal
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("peer_request_fixture", str(_HERE / "peer_request.test.py"))
fixture = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fixture
_SPEC.loader.exec_module(fixture)
pr, pm, steward = fixture.pr, fixture.peer_message, fixture.peer_steward
A, B, B2, C = fixture.A, fixture.B, fixture.B2, fixture.C
import peer_request_cmd as pc  # noqa: E402


def _die(*_args, **_kwargs):
    os.kill(os.getpid(), signal.SIGKILL)


class Boundary(fixture.Fixture):
    def in_child(self, work):
        """Run ``work`` in a forked child; return its wait status."""
        pid = os.fork()
        if pid == 0:
            try:
                work()
            finally:
                os._exit(0)
        return os.waitpid(pid, 0)[1]

    def killed_in_on_prepared(self, work):
        """``work`` runs with every sealed send dying right after its seal, before the
        record step that follows it (the `on_prepared` callback)."""
        original = steward._cmd_prompt_native

        def send(*args, **kwargs):
            if kwargs.get("on_prepared") is not None:
                kwargs["on_prepared"] = _die
            return original(*args, **kwargs)

        def child():
            steward._cmd_prompt_native = send
            work()
        status = self.in_child(child)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status)

    def request_files(self):
        return [json.loads(p.read_text()) for p in pr.requests_dir().glob("rq-*.json")]

    def rows(self):
        directory = pr.requests_dir().parent / "pending"
        return [json.loads(p.read_text()) for p in directory.glob("*.json")]


class KilledWriterTest(Boundary):
    def test_killed_after_the_request_seal_the_next_list_confirms_it(self):
        self.killed_in_on_prepared(lambda: self.request(A, "bee", "SEALED-BODY", title="safe"))
        (record,) = self.request_files()
        (row,) = [r for r in self.rows() if (r.get("request") or {}).get("id") == record["id"]]
        # The next ordinary read finishes it from the seal: both lists, open, revision 1.
        self.assertEqual([r["id"] for r in pr.session_requests(*A)["out"]], [record["id"]])
        self.assertEqual([r["id"] for r in pr.session_requests(*B)["in"]], [record["id"]])
        settled = pr.load(record["id"])
        self.assertEqual(settled["state"], "open")
        self.assertEqual([(r["n"], r["transfer_ref"]) for r in settled["revisions"]], [(1, row["ref"])])
        rc, shown = self.show(B, record["id"])
        self.assertEqual(rc, 0, shown)
        self.assertIn("SEALED-BODY", shown)

    def test_killed_after_the_request_seal_show_and_the_runner_confirm_it_too(self):
        for reader in ("show", "runner"):
            with self.subTest(reader=reader):
                for path in pr.requests_dir().glob("rq-*.json"):
                    path.unlink()
                self.killed_in_on_prepared(lambda: self.request(A, "bee", "BODY-" + reader, title=reader))
                (record,) = self.request_files()
                if reader == "show":
                    rc, shown = self.show(B, record["id"])
                    self.assertEqual(rc, 0, shown)
                    self.assertIn("BODY-show", shown)
                else:
                    self.assertEqual(pr.maintain()["settled"], 1)
                self.assertEqual(pr.load(record["id"])["state"], "open")

    def test_killed_before_the_index_the_request_is_still_listed(self):
        def work():
            pr._index_record = _die
            self.request(A, "bee", "x", title="safe")
        status = self.in_child(work)
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
        (record,) = self.request_files()
        # The record is the truth: the index never written does not hide it from either side.
        self.assertEqual([r["id"] for r in pr.session_requests(*A)["out"]], [record["id"]])
        self.assertEqual([r["id"] for r in pr.session_requests(*B)["in"]], [record["id"]])
        self.assertIsNotNone(pr.load(record["id"]))

    def test_a_missing_cut_or_unreadable_index_is_rebuilt_from_the_records(self):
        first, _ = self.new_request(title="one")
        second, _ = self.new_request(title="two")
        for damage in ("missing", "cut", "garbage"):
            with self.subTest(damage=damage):
                path = pr.index_path()
                if damage == "missing":
                    path.unlink()
                elif damage == "cut":
                    pr._index_store({first: ["%s:%s" % A, "%s:%s" % B]})
                else:
                    path.write_text('{"parties": {"rq-', encoding="utf-8")
                    os.chmod(path, 0o600)
                self.assertEqual({r["id"] for r in pr.session_requests(*A)["out"]}, {first, second})
                self.assertEqual({r["id"] for r in pr.session_requests(*B)["in"]}, {first, second})
                self.assertEqual(set(pr._index_load()), {first, second})

    def test_killed_after_a_final_reports_seal_the_next_show_confirms_it(self):
        request_id, _ = self.new_request()
        self.ack(pr.load(request_id)["revisions"][0]["transfer_ref"])
        self.killed_in_on_prepared(lambda: self.report(B, request_id, result="FINAL-RESULT", rev=1))
        self.assertEqual(len([r for r in self.rows() if (r.get("request") or {}).get("role") == "report"]), 1)
        rc, shown = self.show(A, request_id, report=1)
        self.assertEqual(rc, 0, shown)
        self.assertIn("FINAL-RESULT", shown)
        record = pr.load(request_id)
        self.assertEqual(record["state"], "final")
        self.assertEqual([(r["n"], r["kind"]) for r in record["reports"]], [(1, "final")])

    def test_killed_before_a_reports_seal_the_request_stays_open_and_unconfirmed(self):
        request_id, _ = self.new_request()

        def work():
            pc._send = _die  # dies after the report is reserved, before anything is sealed
            self.report(B, request_id, result="never sealed", rev=1)
        self.in_child(work)
        record = pr.load(request_id)
        self.assertEqual(record["state"], "open")
        self.assertEqual([r["id"] for r in pr.session_requests(*A)["out"]], [request_id])
        self.assertEqual([r["id"] for r in pr.session_requests(*B)["in"]], [request_id])
        rc, shown = self.show(A, request_id)
        self.assertIn("report=1 kind=final revision=1 state=unconfirmed", shown)
        # The next report still gets its own number.
        rc, out = self.report(B, request_id, result="sealed", rev=1)
        self.assertEqual(rc, 0, out)
        self.assertIn("number=2", out)


class ManyOpenTest(Boundary):
    def test_open_requests_are_never_cut_for_their_count(self):
        requester = {"harness": A[0], "session_id": A[1]}
        recipient = {"harness": B[0], "session_id": B[1]}
        first = None
        for index in range(513):
            request_id = pr.reserve(requester, recipient, title=str(index))
            pr.commit_open(request_id, "%032x" % index)
            first = first or request_id
        listing = pr.session_requests(*A)["out"]
        self.assertEqual(len(listing), 513)
        self.assertIn(first, [r["id"] for r in listing])
        self.assertEqual(len(pr.session_requests(*B)["in"]), 513)
        # A confirmed successor of the requester inherits all of them as well.
        self.successors[A] = (C[1], C[0])
        self.assertEqual(len(pr.session_requests(*C)["out"]), 513)


class ConcurrentTest(Boundary):
    def together(self, works):
        """Run each work in its own child; each waits at `_send` until all reached it."""
        original, pids = pc._send, []
        for index, work in enumerate(works):
            pid = os.fork()
            if pid == 0:
                def barrier(*args, **kwargs):
                    (self.root / ("ready%d" % index)).touch()
                    deadline = time.time() + 10
                    while len(list(self.root.glob("ready*"))) < len(works):
                        if time.time() > deadline:
                            os._exit(8)
                        time.sleep(0.01)
                    if index:
                        time.sleep(0.1 * index)
                    return original(*args, **kwargs)
                pc._send = barrier
                try:
                    work()
                finally:
                    os._exit(0)
            pids.append(pid)
        for pid in pids:
            self.assertEqual(os.waitpid(pid, 0)[1], 0)

    def test_two_new_revisions_at_once_seal_the_numbers_they_record(self):
        self.hold = True
        request_id, _ = self.new_request()
        self.together([lambda: self.request(A, request_id, "body-1", rev="head-1"),
                       lambda: self.request(A, request_id, "body-2", rev="head-2")])
        record = pr.load(request_id)
        sealed = [(r["n"], pm._read_transfer_record(r["transfer_ref"])["request"]["revision"])
                  for r in record["revisions"]]
        self.assertEqual(sealed, [(1, 1), (2, 2), (3, 3)])
        self.assertEqual([r["state"] for r in record["revisions"]], ["superseded", "superseded", "current"])
        newest = pm._read_pending(record["revisions"][-1]["transfer_ref"])
        self.assertIn(pr.ref_for(request_id, 3, "request"), newest["refs"])
        self.assertIn("판 3", newest["text"])
        self.assertFalse(pm._request_withdrawn(newest))
        self.assertTrue(pm._request_withdrawn(pm._read_pending(record["revisions"][1]["transfer_ref"])))

    def test_a_partial_and_a_final_report_at_once_each_open_their_own_body(self):
        request_id, _ = self.new_request()
        self.ack(pr.load(request_id)["revisions"][0]["transfer_ref"])
        self.together([lambda: self.report(B, request_id, partial=True, result="REPORT-PARTIAL", rev=1),
                       lambda: self.report(B, request_id, result="REPORT-FINAL", rev=1)])
        record = pr.load(request_id)
        self.assertEqual(record["state"], "final")
        self.assertEqual(sorted(r["n"] for r in record["reports"]), [1, 2])
        for report in record["reports"]:
            line = pm._read_pending(report["transfer_ref"])["text"].splitlines()[0]
            self.assertIn("--report %d" % report["n"], line)
            rc, shown = self.show(A, request_id, report=report["n"])
            self.assertEqual(rc, 0, shown)
            body = shown.split("(sealed body) ---", 1)[1]
            mine = "REPORT-PARTIAL" if report["kind"] == "partial" else "REPORT-FINAL"
            other = "REPORT-FINAL" if mine == "REPORT-PARTIAL" else "REPORT-PARTIAL"
            self.assertIn(mine, body)
            self.assertNotIn(other, body)


class CancelAtTheHandOverTest(Boundary):
    """The Codex carrier checks the request, then hands the row over (`before_add`). A cancel
    lands between the two, or right after the hand-over began."""

    def codex_row(self):
        self.hold = True
        request_id, _ = self.new_request()
        ref = pr.load(request_id)["revisions"][0]["transfer_ref"]
        with pm.pending_lock(ref):
            pm._save_pending(dict(pm._read_pending(ref), state="pending", receipt="native-pending"))
        return request_id, ref

    def deliver(self, ref, send):
        import codex_queue_delivery as queue
        with mock.patch.object(queue, "send_at_least_once", send):
            return pm._deliver_pending_codex_row(ref, timeout=1, start_interrupted=False)

    def notices(self, request_id, to):
        return [row for row in self.rows() if (row.get("request") or {}).get("id") == request_id
                and row["request"]["role"] == "cancel" and row["to"]["session_id"] == to[1]]

    def test_a_cancel_before_the_hand_over_is_not_handed_over(self):
        for successor in (None, B2):
            with self.subTest(successor=successor):
                self.successors.clear()
                request_id, ref = self.codex_row()
                if successor:
                    self.successors[B] = (successor[1], successor[0])
                said, handed = [], []

                def send(_socket, **kwargs):
                    said.append(self.request(A, request_id, cancel=True, reason="before RPC"))
                    kwargs["before_add"]()  # raises: the cancel is seen at the hand-over
                    handed.append(kwargs["thread_id"])
                    return {"status": "queued", "reason": "probe"}

                outcome = self.deliver(ref, send)
                self.assertEqual(handed, [])
                self.assertEqual(outcome["reason"], "request-withdrawn-before-add")
                self.assertIn("notice=not-needed", said[0][1])
                self.assertEqual(pm._read_pending(ref)["state"], "pending")
                self.assertEqual(pr.load(request_id)["state"], "cancelled")

    def test_a_cancel_after_the_hand_over_began_sends_its_notice(self):
        for successor in (None, B2):
            with self.subTest(successor=successor):
                self.successors.clear()
                request_id, ref = self.codex_row()
                if successor:
                    self.successors[B] = (successor[1], successor[0])
                said, handed, original_cancel = [], [], pr.cancel

                def send(_socket, **kwargs):
                    def cancel(*args, **kw):
                        # The carrier hands the row over just before the cancel is recorded.
                        kwargs["before_add"]()
                        handed.append(kwargs["thread_id"])
                        return original_cancel(*args, **kw)
                    with mock.patch.object(pr, "cancel", cancel):
                        said.append(self.request(A, request_id, cancel=True, reason="after RPC"))
                    return {"status": "queued", "reason": "probe"}

                self.deliver(ref, send)
                to = successor or B
                self.assertEqual(handed, [to[1]])
                self.assertNotIn("notice=not-needed", said[0][1])
                self.assertRegex(said[0][1], r"state=cancelled notice=(delivered|queued-not-delivered)")
                self.assertEqual(len(self.notices(request_id, to)), 1)

    def test_the_common_write_boundary_refuses_a_cancelled_revision(self):
        # The shell host, Claude hook context, the OpenCode plugin and the first input all
        # write through `begin_pending_write`, after their own earlier check.
        self.hold = True
        request_id, _ = self.new_request()
        ref = pr.load(request_id)["revisions"][0]["transfer_ref"]
        recipient = {"harness": B[0], "session_id": B[1]}
        reserved = pm.reserve_pending_herdr(ref, recipient, receipt="session-shell-inflight")
        self.assertIsNotNone(reserved)
        rc, out = self.request(A, request_id, cancel=True)
        self.assertIn("notice=not-needed", out)
        self.assertIsNone(pm.begin_pending_write(reserved, recipient=recipient,
                                                 receipt="session-shell-inflight"))
        self.assertEqual(pm._read_pending(ref)["state"], "pending")


class SuccessorReceiptTest(Boundary):
    def test_a_confirmed_successors_receipt_settles_the_message_duty(self):
        self.hold = True
        request_id, _ = self.new_request()
        ref = pr.load(request_id)["revisions"][0]["transfer_ref"]
        row = pm._read_pending(ref)
        with mock.patch.object(fixture.peer_obligations, "ensure_normal_runner", return_value=True):
            duty = self._orig_schedule(row, "bee")
        self.successors[B] = (B2[1], B2[0])
        self.assertEqual(pm._receive_peer_message(row["text"], {"harness": B2[0], "session_id": B2[1]}), 0)
        self.assertEqual(pm._read_pending(ref)["state"], "received")
        store = fixture.peer_obligations.ObligationStore()
        steward._resume_message_obligation_scoped(store.get(duty["id"]), store)
        settled = store.get(duty["id"])
        self.assertEqual(settled["delivery"], "acknowledged", settled)
        self.assertNotEqual((settled.get("observation") or {}).get("reason"), "received-binding-unverified")
        # An unconfirmed namesake's receipt still settles nothing.
        self.assertFalse(fixture.peer_obligations.ObligationStore().acknowledge_message_receipt(
            duty["id"], dict(pm._read_pending(ref), exact_receiver_receipt=dict(
                pm._read_pending(ref)["exact_receiver_receipt"],
                effective_recipient={"harness": "codex", "session_id": "sid-other-0000"})),
            {"harness": "codex", "session_id": "sid-other-0000"}))


class ReportAgainTest(Boundary):
    def test_running_a_report_again_is_a_new_report_and_says_so(self):
        request_id, _ = self.new_request()
        rc, first = self.report(B, request_id, partial=True, result="SAME", rev=1)
        self.assertIn("report-again: running this command again sends report 2", first)
        rc, second = self.report(B, request_id, partial=True, result="SAME", rev=1)
        self.assertIn("number=2", second)
        self.assertEqual(len(pr.load(request_id)["reports"]), 2)


class CallerTest(Boundary):
    """The request commands name their caller as `prompt` does (`_current_session_identity`)."""

    WORKER = {"AGENT_SESSION_ROLE": "worker", "AGENT_DISPATCH_CHILD": "1", "AGENT_DISPATCH_DEPTH": "1"}

    def env(self, extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(
            ("AGENT_DISPATCH_", "AGENT_SESSION_", "CLAUDE_CODE_SESSION", "CLAUDE_SESSION",
             "CODEX_THREAD", "CODEX_SESSION", "OPENCODE_SESSION", "OPENCODE_ROOT"))}
        env.update(extra)
        return env

    def run_cli(self, extra, *argv):
        result = subprocess.run([sys.executable, str(_HERE / "peer-steward.py"), *argv],
                                env=self.env(extra), capture_output=True, text=True, timeout=30)
        return result.returncode, result.stdout

    def test_a_worker_is_its_own_session_never_its_parents(self):
        request_id, _ = self.new_request("PRIVATE-NONCE", title="public-title")
        recipient_worker = dict(self.WORKER, AGENT_DISPATCH_CURRENT_HARNESS="codex",
                                AGENT_DISPATCH_PARENT_SESSION_ID=B[1], CODEX_THREAD_ID="sid-worker-b-9")
        requester_worker = dict(self.WORKER, AGENT_DISPATCH_CURRENT_HARNESS="claude",
                                AGENT_DISPATCH_PARENT_SESSION_ID=A[1],
                                CLAUDE_CODE_SESSION_ID="sid-worker-a-9")
        callers = {
            "session": ({"CODEX_THREAD_ID": B[1]}, {"CLAUDE_CODE_SESSION_ID": A[1]}),
            "its worker": (recipient_worker, requester_worker),
            "unrelated session": ({"CODEX_THREAD_ID": "sid-other-9999"},
                                  {"CLAUDE_CODE_SESSION_ID": "sid-other-8888"}),
            "no session": ({}, {}),
        }
        table = {}
        for label, (as_recipient, as_requester) in callers.items():
            rc, shown = self.run_cli(as_recipient, "show", request_id)
            row = {"show": "body" if "PRIVATE-NONCE" in shown else shown.split("reason=")[-1].split()[0]}
            if label != "session":  # these refuse before anything is sealed or sent
                rc, out = self.run_cli(as_recipient, "report", request_id, "--rev", "1", "--result", "x")
                row["report"] = out.split("reason=")[-1].split()[0]
                rc, out = self.run_cli(as_requester, "request", request_id, "--cancel")
                row["cancel"] = out.split("reason=")[-1].split()[0]
            table[label] = row
        self.assertEqual(table, {
            "session": {"show": "body"},
            "its worker": {"show": "not-a-party", "report": "reporter-not-recipient",
                           "cancel": "not-the-requester"},
            "unrelated session": {"show": "not-a-party", "report": "reporter-not-recipient",
                                  "cancel": "not-the-requester"},
            "no session": {"show": "sender-unknown", "report": "sender-unknown",
                           "cancel": "sender-unknown"},
        }, json.dumps(table))
        self.assertEqual(pr.load(request_id)["state"], "open")
        self.assertEqual(pr.load(request_id)["reports"], [])
        # The session itself, through the same resolver: report and cancel work.
        rc, out = self.report(B, request_id, partial=True, result="mine", rev=1)
        self.assertEqual(rc, 0, out)
        rc, out = self.request(A, request_id, cancel=True)
        self.assertIn("state=cancelled", out)

    def test_the_resolver_names_a_worker_by_its_own_native_id(self):
        import session_identity
        for extra, expected in (
                (dict(self.WORKER, AGENT_DISPATCH_CURRENT_HARNESS="claude",
                      AGENT_DISPATCH_PARENT_SESSION_ID=A[1], CLAUDE_CODE_SESSION_ID="sid-worker-a-9"),
                 ("claude", "sid-worker-a-9")),
                (dict(self.WORKER, AGENT_DISPATCH_CURRENT_HARNESS="opencode",
                      AGENT_DISPATCH_PARENT_SESSION_ID=A[1], OPENCODE_SESSION_ID="ses_worker9"),
                 ("opencode", "ses_worker9"))):
            found = session_identity.identity(self.env(extra))
            self.assertEqual((found.harness, found.session_id), expected)


if __name__ == "__main__":
    unittest.main()
