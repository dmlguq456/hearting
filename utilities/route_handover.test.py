#!/usr/bin/env python3
"""Route hand-off: `peer-steward request <session> --route <id>` (a live steward hands one
stream of routes to another live session of the same harness).

Real seat ledgers, request records and a synthetic registry under a private root. Only the
carrier's far end is replaced (who is calling, address resolution, the native queue, the
liveness proof). Every case runs once per harness pair (Claude, Codex, OpenCode).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import argparse
import contextlib
import importlib.util
import io
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import dispatch_pending_delivery as pending  # noqa: E402
import dispatch_seat_handover as handover  # noqa: E402
import dispatch_session_sweep as sweep_module  # noqa: E402
import dispatch_supervision  # noqa: E402
import peer_address  # noqa: E402
import peer_request as pr  # noqa: E402
import route_authority  # noqa: E402
import session_tidy as st  # noqa: E402
import steward_check  # noqa: E402
from harness_capabilities import parent_completion  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("peer_steward_route_handover_test", str(_HERE / "peer-steward.py"))
peer_steward = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = peer_steward
_SPEC.loader.exec_module(peer_steward)
peer_message = peer_steward.peer_message

A, B, B2, C = "sid-aaaa-1111", "sid-bbbb-2222", "sid-bbbb-3333", "sid-cccc-4444"
OTHER_HARNESS = {"claude": "codex", "codex": "opencode", "opencode": "claude"}


class Cases:
    harness = "claude"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in tuple(os.environ):
            if key.startswith(("AGENT_", "HERDR_", "CLAUDE_", "CODEX_", "OPENCODE_", "XDG_", "HARNESS_")):
                os.environ.pop(key, None)
        for key, value in {"AGENT_PEER_LEDGER_ROOT": "peer-state", "HOME": "home",
                           "XDG_STATE_HOME": "xdg-state", "XDG_CONFIG_HOME": "xdg-config",
                           "XDG_DATA_HOME": "xdg-data", "AGENT_RUNTIME_ROOT": "runtime"}.items():
            os.environ[key] = str(self.root / value)
        self.jobs = self.root / "dispatch" / "jobs.log"
        self.jobs.parent.mkdir()
        self.jobs.write_text("")
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs)
        h = self.harness
        self.sessions = {A: h, B: h, B2: h, C: h, "sid-dddd-5555": OTHER_HARNESS[h]}
        self.caller = A
        self.liveness = {}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for obj, name, value in (
                (peer_steward, "_current_session_identity", lambda: (self.caller, self.sessions[self.caller])),
                (peer_steward, "_current_shell_binding", lambda h, s: (None, "")),
                (peer_steward, "_prompt_codex_native_owner", lambda sid: None),
                (peer_steward, "_prompt_codex_owner_conflict", lambda sid: False),
                (peer_steward, "_schedule_message_obligation", lambda *a, **k: None),
                (peer_steward, "_prompt_carrier_note", lambda *a, **k: None),
                (peer_steward, "_record", lambda **k: None),
                (peer_address, "resolve", self._resolve),
                (peer_message, "peer_alias", lambda h, s: "[%s]" % s[4:6]),
                (peer_message, "deliver_pending_codex", self._queue),
                (steward_check, "_session_liveness", lambda h, s, *a, **k: self.liveness.get(s, "live"))):
            stack.enter_context(mock.patch.object(obj, name, value))

    def _resolve(self, target, **_kw):
        harness = self.sessions.get(target)
        return ({"harness": harness, "session_id": target}, None) if harness else (None, "target-not-found")

    def _queue(self, ref, **_kw):
        with peer_message.pending_lock(ref):
            row = peer_message._read_pending(ref)
            peer_message._save_pending(dict(row, state="queued", receipt="native-queue-accepted"))
        return {"status": "queued", "reason": "native-queue-accepted"}

    # --- fixtures
    def row(self, attempt, route, *, status="running", parent=A, depth="1", worker="owner",
            node="one-shot", **extra):
        h = self.harness
        meta = {"dispatch_depth": depth, "worker_type": worker, "attempt_id": attempt,
                "parent_sid": parent, "parent_harness": h, "harness": h,
                "parent_completion_delivery": parent_completion(h)["carrier"],
                "route_id": route, "route_hash": "sha256:" + route, "route_node": node,
                "attempt_schema_version": "2", **extra}
        with self.jobs.open("a") as handle:
            handle.write("now\t%s\t0\tjob\tlog\t%s\n" % (status, ",".join("%s=%s" % kv for kv in meta.items())))
        return meta

    def hand(self, target, *routes, who=A, text="이 흐름을 맡아 주세요"):
        self.caller = who
        args = argparse.Namespace(target=target, text=text, body_file=None, body_stdin=False, title=None,
                                  rev=None, cancel=False, reason=None, route=list(routes) or None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = peer_steward.cmd_request(args)
        return rc, out.getvalue().strip()

    def holder(self, meta):
        return handover.effective_parent(meta, self.jobs)

    def ledger_rows(self, sid):
        seat = handover._assignment_seat(self.harness, sid)
        raw = st.read_bytes(st._ledger_path(seat)) or b""
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def body_of(self, request_id):
        ref = pr.load(request_id)["revisions"][0]["transfer_ref"]
        return peer_message.read_peer_payload(ref)["body"]

    def nothing_written(self):
        sessions = st.state_root() / "sessions"
        self.assertEqual(sorted(sessions.glob("*.jsonl")) if sessions.exists() else [], [])
        self.assertEqual(pr.session_requests(self.harness, A)["out"], [])

    def waiting(self, name, **record):
        directory = pending.record_directory(self.jobs.parent, A)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ("delivery-%s.json" % name)).write_text(json.dumps(record))

    def finish(self, *attempts):
        """The registry row's own terminal edge, in place (one row per attempt, as the batch reader needs)."""
        lines = self.jobs.read_text().splitlines()
        self.jobs.write_text("".join((line.replace("\trunning\t", "\tdone\t", 1)
                                      if any("attempt_id=%s," % a in line for a in attempts) else line) + "\n"
                                     for line in lines))

    def completion(self, meta, *, success=False):
        h = self.harness
        child = dict(attempt_id=meta["attempt_id"], status="done", readiness="ready", harness=h,
                     required_action="advance-completed" if success else "inspect-done-failure",
                     reason="terminal-complete" if success else "terminal-failure-or-unclosed")
        receipt = dict(schema_version=2, state="ready" if success else "delivered", parent_attempt_id="att-parent",
                       job_registry=str(self.jobs), children=[dict(child, delivery_classification="success")]
                       if success else [child], **({"delivery_classification": "success"} if success else {}))
        return pending.create(root=self.jobs.parent, recipient_kind=parent_completion(h)["carrier"],
                              recipient_key=meta["parent_sid"], delivery_id=meta.get("delivery_id") or "delivery-" + meta["attempt_id"],
                              session_generation="", session_generation_supported="0", attempt_ids=[meta["attempt_id"]],
                              parent_attempt_id="att-parent", route_id=meta["route_id"], route_node=meta["route_node"],
                              receipt=receipt, receipt_digest=pending._canonical_receipt_digest(receipt),
                              row_revisions={meta["attempt_id"]: meta.get("delivery_row_revision", "original")})

    def sweep(self, sid):
        with mock.patch("dispatch_notice_state.keep_claim", return_value=True):
            claimed, _count = sweep_module.sweep_deliver(self.jobs.parent, parent_completion(self.harness)["carrier"], sid)
        return sorted(a for record in claimed for a in record["attempt_ids"])

    # --- cases
    def test_the_stream_moves_and_everything_else_stays(self):
        h = self.harness
        first, second = self.row("att-r1", "rt-r1"), self.row("att-r2", "rt-r2")
        other = self.row("att-other", "rt-other")
        before = self.jobs.read_bytes()
        rc, out = self.hand(B, "rt-r1", "rt-r2")
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"^request=rq-[0-9a-f]{6} delivery=queued-not-delivered to=\[bb\] harness=%s "
                              r"via=\S+ reason=\S+ next=\S+ route=rt-r1 holder=\[bb\] route=rt-r2 holder=\[bb\]$" % h)
        request_id = out.split()[0].split("=")[1]
        self.assertEqual(self.jobs.read_bytes(), before, "registry rows are never rewritten")
        rows = self.ledger_rows(B)
        self.assertEqual([(r["event"], r["source"], r["from"], r["request"]) for r in rows],
                         [("handover", "assign", {"harness": h, "sid": A}, request_id)])
        self.assertEqual(sorted(b["attempt"] for b in rows[0]["bindings"]), ["att-r1", "att-r2"])
        for meta in (first, second):
            self.assertEqual(self.holder(meta), B)
            # correct (owns) and close/start (require_owner) answer from the same holder.
            self.assertIs(route_authority.owns(meta, A, self.jobs), False)
            self.assertIs(route_authority.owns(meta, B, self.jobs), True)
            with self.assertRaises(Exception):
                route_authority.require_owner(meta, A, self.jobs, "parent-close-owner-not-owned")
            route_authority.require_owner(meta, B, self.jobs, "parent-close-owner-not-owned")
        self.assertEqual(self.holder(other), A)
        notice = handover.resolve_recipient("attempt-parent", metadata={
            "session_id": A, "harness": h, "attempt_id": "att-r1"}, jobs=self.jobs)
        self.assertEqual((notice.session_id, notice.harness), (B, h))
        self.assertEqual(handover.session_successor(A, harness=h), ("", ""))
        self.assertEqual([r["id"] for r in pr.session_requests(h, A)["out"]], [request_id])
        self.assertEqual(self.ledger_rows(A), [])
        # These rows name no route file: the note has no lines for them and the request still goes.
        self.assertEqual(self.body_of(request_id), "이 흐름을 맡아 주세요")

    def test_the_completion_reaches_the_receiver_once(self):
        meta = self.row("att-r1", "rt-r1")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        self.finish("att-r1")
        self.completion(meta)
        self.assertEqual(self.sweep(A), [])
        self.assertEqual(self.sweep(B), ["att-r1"])
        self.assertEqual(self.sweep(B), [], "a claimed completion is not claimed again")
        self.assertEqual(self.sweep(A), [])

    def batch(self, *names, cycle="cycle-shared"):
        from route_identity import route_hash
        rows = []
        for name in names:
            route = {"route_id": "rt-" + name, "parent_cycle_id": cycle, "nodes": [{"id": "execute"}]}
            route["route_hash"] = route_hash(route)
            path = self.root / ("rt-%s.json" % name)
            path.write_text(json.dumps(route, sort_keys=True))
            receipt_key = "revision-" + name
            rows.append(self.row("att-" + name, "rt-" + name, node="execute", route_hash=route["route_hash"],
                                 route_file=str(path), registered_worker="1", delivery_intent="1",
                                 delivery_intent_at_ns="1", failure_class="pass", note="completed-marker",
                                 delivery_id="delivery-" + name, delivery_row_revision=receipt_key,
                                 delivery_recipient_kind=parent_completion(self.harness)["carrier"]))
        return rows

    def test_a_completion_batch_moves_whole_or_not_at_all(self):
        first, second = self.batch("b1", "b2")
        self.assertEqual(self.hand(B, "rt-b1"),
                         (1, "request=- delivery=refused reason=batch-split route=rt-b1,rt-b2"))
        self.nothing_written()
        rc, out = self.hand(B, "rt-b1", "rt-b2")
        self.assertEqual(rc, 0, out)
        self.finish("att-b1", "att-b2")
        records = [self.completion(meta, success=True) for meta in (first, second)]
        self.assertBatched(records)
        self.assertEqual(self.sweep(A), [])
        self.assertEqual(self.sweep(B), ["att-b1", "att-b2"])

    def test_without_a_hand_off_the_batch_is_claimed_as_before(self):
        rows = self.batch("b1", "b2")
        self.finish("att-b1", "att-b2")
        records = [self.completion(meta, success=True) for meta in rows]
        self.assertBatched(records)
        self.assertEqual(self.sweep(A), ["att-b1", "att-b2"])

    def assertBatched(self, records):
        from dispatch_notice_state import resolve_delivery_batch
        batch = resolve_delivery_batch(self.jobs, records[0], recipient_key=A, delivery_root=self.jobs.parent)
        self.assertEqual(sorted(batch["delivery_ids"]), sorted(r["delivery_id"] for r in records))

    def test_only_a_running_route_with_nothing_waiting_moves(self):
        self.row("att-r1", "rt-r1")
        for name, record in (("completion", {"state": "pending", "route_id": "rt-other", "attempt_ids": ["att-r1"]}),
                             ("attention", {"state": "claimed", "route_id": "rt-r1", "attempt_ids": []}),
                             ("notice", {"state": "sent-ambiguous", "route_id": "route-free", "attempt_ids": [],
                                         "lineage": ["x:" + json.dumps({"attempt_id": "att-r1"})]})):
            with self.subTest(waiting=name):
                self.waiting(name, **record)
                self.assertEqual(self.hand(B, "rt-r1"),
                                 (1, "request=- delivery=refused reason=route-finishing route=rt-r1"))
                self.waiting(name, **dict(record, state="acked"))
        self.row("att-done", "rt-done", status="done")
        self.waiting("done", state="pending", route_id="rt-done", attempt_ids=["att-done"])
        self.assertEqual(self.hand(B, "rt-done"),
                         (1, "request=- delivery=refused reason=route-finishing route=rt-done"))
        self.waiting("done", state="acked", route_id="rt-done", attempt_ids=["att-done"])
        self.assertEqual(self.hand(B, "rt-done"),
                         (1, "request=- delivery=refused reason=route-closed route=rt-done"))
        self.nothing_written()
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)

    def test_a_route_never_returns_to_a_session_that_held_it(self):
        h = self.harness
        meta = self.row("att-r1", "rt-r1")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        self.assertEqual(self.hand(C, "rt-r1"), (1, "request=- delivery=refused reason=route-not-held route=rt-r1"))
        self.assertEqual(pr.session_requests(h, C)["in"], [])
        self.assertEqual(self.holder(meta), B)
        with mock.patch.object(st, "now_epoch", return_value=1.0):  # a clock that went backwards
            rc, out = self.hand(C, "rt-r1", who=B)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.holder(meta), C)
        for back in (A, B):
            with self.subTest(back=back):
                self.assertEqual(self.hand(back, "rt-r1", who=C),
                                 (1, "request=- delivery=refused reason=route-held-before route=rt-r1"))
        self.assertEqual(self.holder(meta), C)
        self.assertFalse(handover.lineage_graph().binding_conflicts)
        self.assertEqual(handover.record_recipient_binding({"attempt_ids": ["att-r1"]}, A, harness=h,
                                                           jobs=self.jobs), (h, C))

    def test_refusals_send_and_record_nothing(self):
        held = self.row("att-r1", "rt-r1")
        self.row("att-x", "rt-x", parent=C)
        other = OTHER_HARNESS[self.harness]
        self.row("att-pair", "rt-pair", parent_harness=other, harness=other,
                 parent_completion_delivery=parent_completion(other)["carrier"])  # same SID, other harness
        cases = [((B, "rt-x"), "route-not-held route=rt-x"),
                 ((B, "rt-unknown"), "route-not-held route=rt-unknown"),
                 ((B, "rt-r1", "rt-x"), "route-not-held route=rt-x"),
                 ((B, "rt-pair"), "route-not-held route=rt-pair"),
                 ((A, "rt-r1"), "request-to-self"),
                 (("sid-dddd-5555", "rt-r1"), "harness-mismatch route=rt-r1")]
        for argv, reason in cases:
            with self.subTest(argv=argv):
                self.assertEqual(self.hand(*argv)[1], "request=- delivery=refused reason=" + reason)
        for state in ("ended", "unknown"):
            with self.subTest(liveness=state):
                self.liveness[B] = state
                self.assertEqual(self.hand(B, "rt-r1"),
                                 (1, "request=- delivery=refused reason=receiver-not-live route=rt-r1"))
        self.assertEqual(self.holder(held), A)
        self.nothing_written()

    def test_without_route_the_request_is_unchanged(self):
        self.row("att-r1", "rt-r1")
        rc, out = self.hand(B)
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"^request=rq-[0-9a-f]{6} delivery=queued-not-delivered to=\[bb\] harness=%s "
                              r"via=\S+ reason=\S+ next=\S+$" % self.harness)
        self.assertEqual(self.body_of(out.split()[0].split("=")[1]), "이 흐름을 맡아 주세요")
        sessions = st.state_root() / "sessions"
        self.assertEqual(sorted(sessions.glob("*.jsonl")) if sessions.exists() else [], [])
        rc, out = self.hand(out.split()[0].split("=")[1], "rt-r1")
        self.assertEqual(rc, 1)
        self.assertTrue(out.endswith("reason=route-needs-new-request"), out)

    def test_a_send_that_fails_after_the_move_says_so_and_takes_nothing_back(self):
        h = self.harness
        meta = self.row("att-r1", "rt-r1")
        with mock.patch.object(handover, "_append_transition_row", side_effect=OSError("disk")):
            self.assertEqual(self.hand(B, "rt-r1"),
                             (1, "request=- delivery=refused reason=handover-unrecorded route=rt-r1"))
        self.assertEqual(self.holder(meta), A)
        self.nothing_written()
        with mock.patch.object(peer_message, "prepare_peer_message", side_effect=OSError("seal")):
            self.assertEqual(self.hand(B, "rt-r1"), (5, "request=- delivery=refused reason=peer-pending-unavailable "
                                                        "route=rt-r1 holder=[bb] note=undelivered"))
        self.assertEqual(self.holder(meta), B, "the route stays with the receiver")
        self.assertEqual(pr.session_requests(h, A)["out"], [])
        rc, out = self.hand(B)  # the text goes again as a plain request
        self.assertEqual(rc, 0, out)
        meta2 = self.row("att-r2", "rt-r2")
        with mock.patch.object(pr, "confirm", side_effect=ValueError("record")):
            rc, out = self.hand(B, "rt-r2")
        self.assertEqual(rc, 5, out)
        self.assertTrue(out.endswith(" route=rt-r2 holder=[bb] note=undelivered"), out)
        self.assertEqual(self.holder(meta2), B)

    def test_receiver_clear_and_retire_follow_the_existing_lineage(self):
        h = self.harness
        meta = self.row("att-r1", "rt-r1")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        handover._append_transition_row(st._pane_seat_of("w1:pB"), {
            "ts": time.time() + 5, "harness": h, "sid": B2, "event": "handover", "source": "clear",
            "from": {"harness": h, "sid": B}, "bindings": []})
        self.assertEqual(self.holder(meta), B2)
        self.assertEqual(handover.record_recipient_binding({"attempt_ids": ["att-r1"]}, A, harness=h,
                                                           jobs=self.jobs), (h, B2))
        row = handover.record_retire_handover(B2, h, C, h, jobs=self.jobs,
                                              successor_seat=st._pane_seat_of("w1:pC"))
        self.assertEqual([b["attempt"] for b in row["bindings"]], ["att-r1"])
        self.assertEqual(self.holder(meta), C)
        self.assertFalse(handover.lineage_graph().binding_conflicts)
        self.assertEqual(self.hand(B, "rt-r1", who=C)[1],
                         "request=- delivery=refused reason=route-held-before route=rt-r1")

    def test_folding_never_drops_an_assign_row(self):
        h = self.harness
        meta = self.row("att-r1", "rt-r1")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        seat = handover._assignment_seat(h, B)
        path = st._ledger_path(seat)
        filler = [{"ts": time.time() + i, "harness": h, "sid": "sid-x%04d" % i, "event": "handover",
                   "source": "retire", "from": {"harness": h, "sid": "sid-y%04d" % i}, "bindings": []}
                  for i in range(st.HANDOVER_KEEP_ROWS + 5)]
        filler += [{"ts": time.time(), "harness": h, "sid": B, "event": "prompt"}] * st.LEDGER_FOLD_LINES
        self.finish("att-r1")  # even a finished route keeps its row: folding reads no registry
        with path.open("a") as handle:
            handle.writelines(json.dumps(item) + "\n" for item in filler)
        with mock.patch("dispatch_registry_cache.registry_view", side_effect=AssertionError("registry read")):
            st._fold_ledger(seat)
        self.assertEqual([r["source"] for r in self.ledger_rows(B) if r.get("source") == "assign"], ["assign"])
        self.assertEqual(len(self.ledger_rows(B)) < len(filler), True)
        self.assertEqual(self.holder(meta), B)

    def test_a_missed_generation_bump_still_changes_the_inbox_digest(self):
        import dispatch_recipient_inbox as inbox
        self.row("att-r1", "rt-r1")
        before = handover.lineage_graph()
        self.assertEqual(handover.notice_edges(before), tuple(before.edges), "no hand-off: the same digest as main")
        with mock.patch.object(handover, "_bump_dispatch_edge_generation"):
            self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        scopes = handover.storage_recipients(B, harness=self.harness)
        self.assertNotEqual(inbox.edge_digest(scopes.edges), inbox.edge_digest(before.edges))
        self.assertEqual(inbox.edge_digest(scopes.edges),
                         inbox.edge_digest(handover.notice_edges(handover.lineage_graph())))

    def test_the_sender_still_counts_a_handed_route_as_running(self):
        """A known limit: running_route_ids stays on the registered parent (main, byte for byte)."""
        self.row("att-r1", "rt-r1")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        with mock.patch.object(dispatch_supervision, "update_attempt_stall_observation",
                               return_value={"state": "active"}):
            self.assertEqual(dispatch_supervision.running_route_ids(self.jobs, ["rt-r1"], A), {"rt-r1"})
            self.assertEqual(dispatch_supervision.running_route_ids(self.jobs, ["rt-r1"], B), set())

    def test_the_route_chain_is_display_only_and_the_completion_follows_the_binding(self):
        import capability_route_base as base
        h = self.harness
        rc = base._route_chain_module()
        rc.append(h, A, {"v": rc.SCHEMA, "harness": h, "session_id": A, "route_id": "rt-r1",
                         "route_file": "/synthetic/rt-r1.json"})
        meta = self.row("att-r1", "rt-r1")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        os.environ.update(AGENT_DISPATCH_DEPTH="1", AGENT_DISPATCH_PARENT_SESSION_ID=A,
                          AGENT_DISPATCH_ATTEMPT_ID="att-r1")
        self.assertEqual(base._route_chain_identity("continuation", {"source_route_id": "rt-r1"}),
                         (h, A, 1, "att-r1"))
        self.finish("att-r1")
        self.completion(meta)
        self.assertEqual((self.sweep(A), self.sweep(B)), ([], ["att-r1"]))

    def test_the_note_carries_record_facts_within_two_kilobytes(self):
        import peer_request_cmd
        import work_intent
        from dispatch_terminal_commit import terminal_slot
        h = self.harness
        artifacts = self.root / "artifacts"
        route_file = self.root / "routes" / "rt-r1.json"
        route_file.parent.mkdir()
        route_file.write_text(json.dumps({
            "route_id": "rt-r1", "route_hash": "sha256:rt-r1", "capability": "autopilot-code",
            "artifact_root": str(artifacts),
            "work_request": {"text": "의도: 로그인 오류 고치기\n\n- 사용자 원문: \"로그인이 안 돼요\"\n본문"}}))
        old = self.row("att-old", "rt-r1", status="done", route_file=str(route_file))
        self.row("att-new", "rt-r1", route_file=str(route_file))
        self.row("att-stage", "rt-r1", depth="2", worker="stage", node="code-execute")
        slot = terminal_slot(artifacts, "rt-r1", "att-old")
        slot.mkdir(parents=True)
        (slot / work_intent.SUMMARY_NAME).write_text(
            "%s autopilot-code · route rt-r1\n의도: 로그인\n산출물: /synthetic/report.md" % work_intent.summary_heads()[0])
        self.assertTrue(work_intent.sealed_summary(old, "att-old"))
        rc, out = self.hand(B, "rt-r1")
        self.assertEqual(rc, 0, out)
        body = self.body_of(out.split()[0].split("=")[1])
        self.assertIn("--- handed route (runtime) ---\nroute rt-r1 · autopilot-code · running · stage code-execute\n"
                      "[의도] 로그인 오류 고치기\n사용자 원문: \"로그인이 안 돼요\"\n"
                      "마지막 완료 요약: %s · 산출물: /synthetic/report.md\n"
                      "이제 [bb]가 받음: 완료 · 주의 알림 · start/correct/close" % (slot / work_intent.SUMMARY_NAME), body)
        self.assertNotIn("감독 카드", body)
        card = st.card_json_path(handover._assignment_seat(h, B))
        card.parent.mkdir(parents=True, exist_ok=True)
        card.write_text(json.dumps({"authored_at_epoch": 0, "body": "사용자 요구 원문 발췌:\n- \"하나\"\n- \"둘\""}))
        rc, out = self.hand(C, "rt-r1", who=B)
        self.assertEqual(rc, 0, out)
        body = self.body_of(out.split()[0].split("=")[1])
        self.assertRegex(body, r"\n감독 카드: %s \(갱신 \d\d-\d\d \d\d:\d\d · 사용자 요구 원문 발췌 2건\) — "
                               r"이 흐름에 맞는 것만 따르세요$" % re.escape(str(card)))
        me = {"harness": h, "session_id": C}
        with mock.patch.object(peer_request_cmd, "_card_line", return_value="x" * 5000):
            note = peer_request_cmd._route_notes(me, "[aa]", ["rt-r1"]).split("---\n", 1)[1]
        self.assertLessEqual(len(note.encode("utf-8")), peer_request_cmd._ROUTE_NOTE_BYTES)
        self.assertTrue(note.startswith("route rt-r1 · autopilot-code"))
        # Another route's file at that path, or text that cannot be encoded: no lines for it, no failure.
        for doc in ({"route_id": "rt-elsewhere", "route_hash": "sha256:rt-r1", "work_request": {"text": "남의 글"}},
                    {"route_id": "rt-r1", "route_hash": "sha256:rt-r1", "capability": "\ud800",
                     "artifact_root": str(artifacts), "work_request": {"text": "x"}}):
            with self.subTest(doc=doc["route_id"]):
                route_file.write_text(json.dumps(doc))
                self.assertEqual(peer_request_cmd._route_notes(me, "[aa]", ["rt-r1"]), "")


class ClaudePair(Cases, unittest.TestCase):
    harness = "claude"


class CodexPair(Cases, unittest.TestCase):
    harness = "codex"


class OpenCodePair(Cases, unittest.TestCase):
    harness = "opencode"


if __name__ == "__main__":
    unittest.main()
