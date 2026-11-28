#!/usr/bin/env python3
"""Route hand-off: `peer-steward request <session> --route <id>` hands a resting one-step route
(its round ended and its result settled) to another live session of the same harness.

Real seat ledgers, request records, route files, closure records and pending deliveries, with a
synthetic registry, under a private root. Only the carrier's far end is replaced (who is calling,
address resolution, the native queue, the liveness proof). Every case runs once per harness pair
(Claude, Codex, OpenCode).
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
import peer_address  # noqa: E402
import peer_request as pr  # noqa: E402
import route_authority  # noqa: E402
import session_tidy as st  # noqa: E402
import steward_check  # noqa: E402
from harness_capabilities import parent_completion  # noqa: E402
from route_identity import route_hash  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("peer_steward_route_handover_test", str(_HERE / "peer-steward.py"))
peer_steward = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = peer_steward
_SPEC.loader.exec_module(peer_steward)
peer_message = peer_steward.peer_message

A, B, B2, C = "sid-aaaa-1111", "sid-bbbb-2222", "sid-bbbb-3333", "sid-cccc-4444"
OTHER_HARNESS = {"claude": "codex", "codex": "opencode", "opencode": "claude"}
RUNNING_HINT = "hint=hand it over after this round ends and you have read its result"
FINISHING_HINT = "hint=its result is still being settled or delivered; hand it over once it has arrived"


class Cases:
    harness = "claude"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in tuple(os.environ):
            if key.startswith(("AGENT_", "HERDR_", "CLAUDE_", "CODEX_", "OPENCODE_", "XDG_", "HARNESS_", "FLEET_")):
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
        self.alias_ready = True
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
                (peer_message, "peer_alias", lambda h, s: "[%s]" % s[4:6] if self.alias_ready else "[?]"),
                (peer_message, "deliver_pending_codex", self._queue),
                (steward_check, "_session_liveness", self._liveness)):
            stack.enter_context(mock.patch.object(obj, name, value))

    def _liveness(self, harness, sid, *a, **k):
        value = self.liveness.get(sid, "live")
        return value() if callable(value) else value

    def _resolve(self, target, **_kw):
        harness = self.sessions.get(target)
        return ({"harness": harness, "session_id": target}, None) if harness else (None, "target-not-found")

    def _queue(self, ref, **_kw):
        with peer_message.pending_lock(ref):
            row = peer_message._read_pending(ref)
            peer_message._save_pending(dict(row, state="queued", receipt="native-queue-accepted"))
        return {"status": "queued", "reason": "native-queue-accepted"}

    # --- fixtures
    def route(self, name, *, closed=True, nodes=1, cycle=None, **outcome):
        """A route file (one-shot unless ``nodes`` > 1) and, when ``closed``, its closure on a PASS."""
        doc = {"route_id": "rt-" + name, "capability": "autopilot-code", "artifact_root": str(self.root / "artifacts"),
               "nodes": [{"id": "one-shot"}] if nodes == 1 else [{"id": "frame"}, {"id": "frame-alternative"}],
               "work_request": {"text": "의도: 로그인 오류 고치기\n\n- 사용자 원문: \"로그인이 안 돼요\"\n본문"}}
        if cycle:
            doc["parent_cycle_id"] = cycle
        doc["route_hash"] = route_hash(doc)
        path = self.root / "routes" / ("rt-%s.json" % name)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(doc))
        if closed:
            path.with_name(path.stem + ".outcome.json").write_text(json.dumps(
                {"route_id": doc["route_id"], "route_hash": doc["route_hash"], "terminal_gate_proven": True, **outcome}))
        return doc, path

    def row(self, attempt, route, *, status="done", parent=A, depth="1", worker="owner", node="one-shot",
            doc=None, **extra):
        h = self.harness
        meta = {"dispatch_depth": depth, "worker_type": worker, "attempt_id": attempt,
                "parent_sid": parent, "parent_harness": h, "harness": h,
                "parent_completion_delivery": parent_completion(h)["carrier"],
                "route_id": route, "route_hash": doc["route_hash"] if doc else "sha256:" + route, "route_node": node,
                "attempt_schema_version": "2", "failure_class": "pass", **extra}
        if doc is not None:
            meta["route_file"] = str(self.root / "routes" / (route + ".json"))
        with self.jobs.open("a") as handle:
            handle.write("now\t%s\t0\tjob\tlog\t%s\n" % (status, ",".join("%s=%s" % kv for kv in meta.items())))
        return meta

    def resting(self, name, **row):
        doc, _path = self.route(name)
        return self.row("att-" + name, doc["route_id"], doc=doc, **row)

    def hand(self, target, *routes, who=A, text="이 흐름을 맡아 주세요"):
        self.caller = who
        args = argparse.Namespace(target=target, text=text, body_file=None, body_stdin=False, title=None,
                                  rev=None, cancel=False, reason=None, route=list(routes) or None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = peer_steward.cmd_request(args)
        return rc, out.getvalue().strip()

    def refused(self, word, route, hint=None):
        return "request=- delivery=refused reason=%s route=%s%s" % (word, route, (" " + hint) if hint else "")

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

    def completion(self, meta):
        h = self.harness
        child = dict(attempt_id=meta["attempt_id"], status="done", readiness="ready", harness=h,
                     required_action="inspect-done-failure", reason="terminal-failure-or-unclosed")
        receipt = dict(schema_version=2, state="delivered", parent_attempt_id="att-parent",
                       job_registry=str(self.jobs), children=[child])
        return pending.create(root=self.jobs.parent, recipient_kind=parent_completion(h)["carrier"],
                              recipient_key=meta["parent_sid"], delivery_id="delivery-" + meta["attempt_id"],
                              session_generation="", session_generation_supported="0", attempt_ids=[meta["attempt_id"]],
                              parent_attempt_id="att-parent", route_id=meta["route_id"], route_node=meta["route_node"],
                              receipt=receipt, receipt_digest=pending._canonical_receipt_digest(receipt),
                              row_revisions={meta["attempt_id"]: "original"})

    def finish(self, attempt):
        """The registry row's own terminal edge, in place (one row per attempt, as the delivery reader needs)."""
        lines = self.jobs.read_text().splitlines()
        self.jobs.write_text("".join((line.replace("\trunning\t", "\tdone\t", 1)
                                      if "attempt_id=%s," % attempt in line else line) + "\n" for line in lines))

    def sweep(self, sid):
        with mock.patch("dispatch_notice_state.keep_claim", return_value=True):
            claimed, _count = sweep_module.sweep_deliver(self.jobs.parent, parent_completion(self.harness)["carrier"], sid)
        return sorted(a for record in claimed for a in record["attempt_ids"])

    # --- the hand-off
    def test_a_resting_route_moves_and_everything_else_stays(self):
        h = self.harness
        first, second, other = self.resting("r1"), self.resting("r2"), self.resting("other")
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
            self.assertIs(route_authority.owns(meta, A, self.jobs), False)
            self.assertIs(route_authority.owns(meta, B, self.jobs), True)
            self.assertTrue(handover.route_assigned(meta, self.jobs))
        self.assertEqual(self.holder(other), A)
        self.assertFalse(handover.route_assigned(other, self.jobs))
        self.assertEqual(handover.session_successor(A, harness=h), ("", ""))
        self.assertEqual([r["id"] for r in pr.session_requests(h, A)["out"]], [request_id])
        self.assertIn("--- handed route (runtime) ---\nroute rt-r1 · autopilot-code · done · stage one-shot\n"
                      "[의도] 로그인 오류 고치기\n사용자 원문: \"로그인이 안 돼요\"\n마지막 완료 요약: 없음\n"
                      "이제 [bb]가 받음: 완료 · 주의 알림 · start/correct/close", self.body_of(request_id))

    def test_a_completion_the_sender_already_received_is_not_received_again(self):
        meta = self.resting("r1")
        self.completion(meta)
        self.assertEqual(self.sweep(A), ["att-r1"])       # claimed by the sender, not yet acknowledged
        self.assertEqual(self.hand(B, "rt-r1")[1], self.refused("route-finishing", "rt-r1", FINISHING_HINT))
        sweep_module.ack_delivered(self.jobs.parent, A, [pending.read(self.jobs.parent, A, "delivery-att-r1")],
                                   acked_by="fixture-render")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        self.assertEqual((self.sweep(B), self.sweep(A)), ([], []), "a received completion goes to nobody again")

    def test_the_receivers_next_turn_reports_only_to_the_receiver(self):
        import work_start
        h = self.harness
        meta = self.resting("r1")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        for sid, allowed in ((A, False), (B, True)):   # the authority step of `correct --route`
            with self.subTest(correct_by=sid), mock.patch.object(work_start, "_current_parent_session_id", return_value=sid):
                if allowed:
                    work_start.require_route_parent(meta, self.jobs, "att-r1")
                else:
                    with self.assertRaises(Exception):
                        work_start.require_route_parent(meta, self.jobs, "att-r1")
        doc = json.loads((self.root / "routes" / "rt-r1.json").read_text())
        nxt = self.row("att-r1-turn2", "rt-r1", doc=doc, parent=B, status="running")  # the turn B's `correct` starts
        self.assertEqual(self.holder(nxt), B)
        self.assertEqual(self.hand(C, "rt-r1", who=B)[1], self.refused("route-running", "rt-r1", RUNNING_HINT))
        self.finish("att-r1-turn2")
        self.completion(nxt)
        self.assertEqual((self.sweep(A), self.sweep(B), self.sweep(B)), ([], ["att-r1-turn2"], []))
        question = handover.resolve_recipient("attempt-parent", metadata={
            "session_id": B, "harness": h, "attempt_id": "att-r1-turn2"}, jobs=self.jobs)
        self.assertEqual((question.session_id, question.harness), (B, h))

    def test_a_running_or_waiting_route_waits_for_its_round_to_end(self):
        import unit_continue as UC
        doc, _ = self.route("run", closed=False)
        self.row("att-run", "rt-run", doc=doc, status="running")
        self.assertEqual(self.hand(B, "rt-run"), (1, self.refused("route-running", "rt-run", RUNNING_HINT)))
        self.resting("stage")
        self.row("att-stage-2", "rt-stage", depth="2", worker="stage", node="execute", status="open")
        self.assertEqual(self.hand(B, "rt-stage")[1], self.refused("route-running", "rt-stage", RUNNING_HINT))
        self.resting("kept")
        UC.submit(self.jobs, "rt-kept", "a later request that waits for a seat")   # kept, its turn not started
        self.assertEqual(self.hand(B, "rt-kept")[1], self.refused("route-running", "rt-kept", RUNNING_HINT))
        self.nothing_written()

    def test_a_multistep_route_is_refused_by_its_kind(self):
        hint = "hint=only a one-step route moves; leave it here or let the receiver start new work"
        frame, _ = self.route("frame", nodes=2)
        self.row("att-frame", "rt-frame", doc=frame)
        cycle, _ = self.route("cycle", cycle="cycle-plan")
        self.row("att-cycle", "rt-cycle", doc=cycle)
        self.resting("sealed", managed_sealed_batch_id="batch-1", managed_sealed_attempt_ids="att-sealed")
        for route in ("rt-frame", "rt-cycle", "rt-sealed"):
            with self.subTest(route=route):
                self.assertEqual(self.hand(B, route), (1, self.refused("route-multistep", route, hint)))
        self.nothing_written()

    def test_a_route_its_holder_could_not_continue_is_refused(self):
        hint = "hint=this route cannot take a later request; start new work from the receiver"
        cases = {"fail": dict(row={"failure_class": "fail"}), "auto": dict(outcome={"autoclose": True}),
                 "gate": dict(outcome={"terminal_gate_proven": False}), "stopped": dict(row={"status": "cancelled"})}
        for name, case in cases.items():
            doc, _ = self.route(name, **case.get("outcome", {}))
            self.row("att-" + name, doc["route_id"], doc=doc, **case.get("row", {}))
        array, path = self.route("array")
        self.row("att-array", "rt-array", doc=array)
        path.write_text("[]")
        foreign, path = self.route("foreign")
        self.row("att-foreign", "rt-foreign", doc=foreign)
        path.write_text(json.dumps(dict(foreign, work_request={"text": "changed after sealing"})))
        for route in ("rt-fail", "rt-auto", "rt-gate", "rt-stopped", "rt-array", "rt-foreign"):
            with self.subTest(route=route):
                self.assertEqual(self.hand(B, route), (1, self.refused("route-closed", route, hint)))
        settling, _ = self.route("settling", closed=False)
        self.row("att-settling", "rt-settling", doc=settling)
        self.assertEqual(self.hand(B, "rt-settling")[1], self.refused("route-finishing", "rt-settling", FINISHING_HINT))
        self.nothing_written()

    def test_an_unsettled_completion_or_notice_keeps_the_route(self):
        self.resting("r1")
        for name, record in (("completion", {"state": "pending", "route_id": "rt-other", "attempt_ids": ["att-r1"]}),
                             ("attention", {"state": "claimed", "route_id": "rt-r1", "attempt_ids": []}),
                             ("notice", {"state": "sent-ambiguous", "route_id": "route-free", "attempt_ids": [],
                                         "lineage": ["x:" + json.dumps({"attempt_id": "att-r1"})]})):
            with self.subTest(waiting=name):
                self.waiting(name, **record)
                self.assertEqual(self.hand(B, "rt-r1")[1], self.refused("route-finishing", "rt-r1", FINISHING_HINT))
                self.waiting(name, **dict(record, state="acked"))
        self.nothing_written()
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)

    def test_the_second_reviews_races_are_refused_at_hand_off(self):
        meta = self.resting("r1")
        doc = json.loads((self.root / "routes" / "rt-r1.json").read_text())
        # The sender starts the next turn between the last look and the write: the look under the locks sees it.
        real = handover.binding_mutexes

        @contextlib.contextmanager
        def turn_starts(bindings, **kwargs):
            self.row("att-r1-next", "rt-r1", doc=doc, status="running")
            with real(bindings, **kwargs):
                yield
        with mock.patch.object(handover, "binding_mutexes", turn_starts):
            self.assertEqual(self.hand(B, "rt-r1")[1], self.refused("route-running", "rt-r1", RUNNING_HINT))
        self.assertEqual(self.holder(meta), A)
        # The receiver ends between the first look and the write: the look right before the write refuses.
        self.resting("r2")
        looks = iter(["live", "live"])
        self.liveness[B] = lambda: next(looks, "ended")
        self.assertEqual(self.hand(B, "rt-r2")[1], self.refused(
            "receiver-not-live", "rt-r2", "hint=hand it to a session that is running now"))
        self.nothing_written()

    def test_refusals_send_and_record_nothing(self):
        held = self.resting("r1")
        self.resting("x", parent=C)
        other = OTHER_HARNESS[self.harness]
        self.resting("pair", parent_harness=other, harness=other,
                     parent_completion_delivery=parent_completion(other)["carrier"])  # same SID, other harness
        not_held = "hint=only the session that holds the route now can hand it over"
        cases = [((B, "rt-x"), self.refused("route-not-held", "rt-x", not_held)),
                 ((B, "rt-unknown"), self.refused("route-not-held", "rt-unknown", not_held)),
                 ((B, "rt-r1", "rt-x"), self.refused("route-not-held", "rt-x", not_held)),
                 ((B, "rt-pair"), self.refused("route-not-held", "rt-pair", not_held)),
                 ((A, "rt-r1"), "request=- delivery=refused reason=request-to-self"),
                 (("sid-dddd-5555", "rt-r1"), self.refused("harness-mismatch", "rt-r1",
                                                           "hint=hand it to a session of the same harness"))]
        for argv, line in cases:
            with self.subTest(argv=argv):
                self.assertEqual(self.hand(*argv)[1], line)
        for state in ("ended", "unknown"):
            with self.subTest(liveness=state):
                self.liveness[B] = state
                self.assertEqual(self.hand(B, "rt-r1"), (1, self.refused(
                    "receiver-not-live", "rt-r1", "hint=hand it to a session that is running now")))
        self.assertEqual(self.holder(held), A)
        self.nothing_written()

    def test_without_route_the_request_is_unchanged(self):
        self.resting("r1")
        real = peer_message.prepare_peer_message

        def tag_appears_while_sending(*a, **k):   # the receiver's tag exists only once the carrier runs
            self.alias_ready = True
            return real(*a, **k)
        self.alias_ready = False
        with mock.patch.object(peer_message, "prepare_peer_message", tag_appears_while_sending):
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
        meta = self.resting("r1")
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
        self.assertEqual(self.hand(B)[0], 0)  # the text goes again as a plain request
        meta2 = self.resting("r2")
        with mock.patch.object(pr, "confirm", side_effect=ValueError("record")):
            rc, out = self.hand(B, "rt-r2")
        self.assertEqual(rc, 5, out)
        ref = re.search(r" note=queued ref=(\S+)$", out)
        self.assertTrue(ref, out)  # sealed already: it still arrives on its own, nothing to send again
        self.assertTrue(out.split(" note=")[0].endswith(" route=rt-r2 holder=[bb]"), out)
        self.assertIsNotNone(peer_message._read_pending(ref.group(1)))
        self.assertEqual(self.holder(meta2), B)

    def test_a_route_never_returns_to_a_session_that_held_it(self):
        h = self.harness
        meta = self.resting("r1")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        self.assertEqual(self.hand(C, "rt-r1")[1].split(" hint=")[0], self.refused("route-not-held", "rt-r1"))
        with mock.patch.object(st, "now_epoch", return_value=1.0):  # a clock that went backwards
            rc, out = self.hand(C, "rt-r1", who=B)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.holder(meta), C)
        for back in (A, B):
            with self.subTest(back=back):
                self.assertEqual(self.hand(back, "rt-r1", who=C), (1, self.refused(
                    "route-held-before", "rt-r1", "hint=a session that held this route before cannot take it again")))
        self.assertFalse(handover.lineage_graph().binding_conflicts)
        self.assertEqual(handover.record_recipient_binding({"attempt_ids": ["att-r1"]}, A, harness=h,
                                                           jobs=self.jobs), (h, C))

    def test_receiver_clear_and_retire_follow_the_existing_lineage(self):
        h = self.harness
        meta = self.resting("r1")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        handover._append_transition_row(st._pane_seat_of("w1:pB"), {
            "ts": time.time() + 5, "harness": h, "sid": B2, "event": "handover", "source": "clear",
            "from": {"harness": h, "sid": B}, "bindings": []})
        self.assertEqual(self.holder(meta), B2)
        own = self.resting("own", parent=B2)  # a resting route B2 launched itself, for comparison
        handover.record_retire_handover(B2, h, C, h, jobs=self.jobs, successor_seat=st._pane_seat_of("w1:pC"))
        # retire carries unfinished work only: a resting route stays with B2 exactly as one it launched does.
        self.assertEqual((self.holder(meta), self.holder(own)), (B2, B2))
        self.assertFalse(handover.lineage_graph().binding_conflicts)
        self.assertEqual(self.hand(B, "rt-r1", who=B2)[1].split(" hint=")[0],
                         self.refused("route-held-before", "rt-r1"))
        self.assertFalse(handover.route_assigned(own, self.jobs), "clear/retire alone is no hand-off")

    def test_folding_never_drops_an_assign_row(self):
        h = self.harness
        meta = self.resting("r1")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        seat = handover._assignment_seat(h, B)
        path = st._ledger_path(seat)
        filler = [{"ts": time.time() + i, "harness": h, "sid": "sid-x%04d" % i, "event": "handover",
                   "source": "retire", "from": {"harness": h, "sid": "sid-y%04d" % i}, "bindings": []}
                  for i in range(st.HANDOVER_KEEP_ROWS + 5)]
        filler += [{"ts": time.time(), "harness": h, "sid": B, "event": "prompt"}] * st.LEDGER_FOLD_LINES
        with path.open("a") as handle:
            handle.writelines(json.dumps(item) + "\n" for item in filler)
        with mock.patch("dispatch_registry_cache.registry_view", side_effect=AssertionError("registry read")):
            st._fold_ledger(seat)
        self.assertEqual([r["source"] for r in self.ledger_rows(B) if r.get("source") == "assign"], ["assign"])
        self.assertLess(len(self.ledger_rows(B)), len(filler))
        self.assertEqual(self.holder(meta), B)

    def test_a_missed_generation_bump_still_changes_the_inbox_digest(self):
        import dispatch_recipient_inbox as inbox
        self.resting("r1")
        before = handover.lineage_graph()
        self.assertEqual(handover.notice_edges(before), tuple(before.edges), "no hand-off: the same digest as main")
        with mock.patch.object(handover, "_bump_dispatch_edge_generation"):
            self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        scopes = handover.storage_recipients(B, harness=self.harness)
        self.assertNotEqual(inbox.edge_digest(scopes.edges), inbox.edge_digest(before.edges))
        self.assertEqual(inbox.edge_digest(scopes.edges),
                         inbox.edge_digest(handover.notice_edges(handover.lineage_graph())))

    def test_the_route_chain_is_display_only_and_the_completion_follows_the_binding(self):
        import capability_route_base as base
        h = self.harness
        rc = base._route_chain_module()
        rc.append(h, A, {"v": rc.SCHEMA, "harness": h, "session_id": A, "route_id": "rt-r1",
                         "route_file": "/synthetic/rt-r1.json"})
        meta = self.resting("r1")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        os.environ.update(AGENT_DISPATCH_DEPTH="1", AGENT_DISPATCH_PARENT_SESSION_ID=A,
                          AGENT_DISPATCH_ATTEMPT_ID="att-r1")
        self.assertEqual(base._route_chain_identity("continuation", {"source_route_id": "rt-r1"}),
                         (h, A, 1, "att-r1"))
        self.completion(meta)  # a late notice of the handed turn, stored under the launcher
        self.assertEqual((self.sweep(A), self.sweep(B)), ([], ["att-r1"]))

    def test_fleet_marks_only_a_handed_route_with_its_holder(self):
        import types
        sys.path.insert(0, str(_HERE.parent))
        from tools.fleet import collectors
        h = self.harness
        handed, kept = self.resting("r1"), self.resting("r2")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        jobs = [types.SimpleNamespace(is_child=True, parent_sid=meta["parent_sid"], slug="s-" + meta["attempt_id"],
                                      harness=h, source=None, parent_managed_dir=None, caller_sid=None,
                                      _registry_metadata=meta, _registry_path=str(self.jobs)) for meta in (handed, kept)]
        sessions = [types.SimpleNamespace(session_id=sid, harness=h, is_child=False, liveness="idle", session_aliases=(),
                                          app_server=False, managed_dir=None, cwd="/x") for sid in (A, B)]
        collectors.resolve_parent_edges(sessions, jobs)
        self.assertEqual(getattr(jobs[0], "_route_holder", None), (h, B))
        self.assertIsNone(getattr(jobs[1], "_route_holder", None))

    def test_the_note_carries_record_facts_within_two_kilobytes(self):
        import peer_request_cmd
        import work_intent
        from dispatch_terminal_commit import terminal_slot
        h = self.harness
        meta = self.resting("r1")
        slot = terminal_slot(self.root / "artifacts", "rt-r1", "att-r1")
        slot.mkdir(parents=True)
        (slot / work_intent.SUMMARY_NAME).write_text(
            "%s autopilot-code · route rt-r1\n의도: 로그인\n산출물: /synthetic/report.md" % work_intent.summary_heads()[0])
        self.assertTrue(work_intent.sealed_summary(meta, "att-r1"))
        card = st.card_json_path(handover._assignment_seat(h, A))
        card.parent.mkdir(parents=True, exist_ok=True)
        card.write_text(json.dumps({"authored_at_epoch": 0, "body": "사용자 요구 원문 발췌:\n- \"하나\"\n- \"둘\""}))
        rc, out = self.hand(B, "rt-r1")
        self.assertEqual(rc, 0, out)
        body = self.body_of(out.split()[0].split("=")[1])
        self.assertIn("마지막 완료 요약: %s · 산출물: /synthetic/report.md\n이제 [bb]가 받음: 완료 · 주의 알림 · "
                      "start/correct/close\n" % (slot / work_intent.SUMMARY_NAME), body)
        self.assertRegex(body, r"\n감독 카드: %s \(갱신 \d\d-\d\d \d\d:\d\d · 사용자 요구 원문 발췌 2건\) — "
                               r"이 흐름에 맞는 것만 따르세요$" % re.escape(str(card)))
        me = {"harness": h, "session_id": A}
        with mock.patch.object(peer_request_cmd, "_card_line", return_value="x" * 5000):
            note = peer_request_cmd._route_notes(me, "[bb]", ["rt-r1"]).split("---\n", 1)[1]
        self.assertLessEqual(len(note.encode("utf-8")), peer_request_cmd._ROUTE_NOTE_BYTES)
        self.assertTrue(note.startswith("route rt-r1 · autopilot-code"))
        path = self.root / "routes" / "rt-r1.json"
        doc = json.loads(path.read_text())
        for changed in ({"route_id": "rt-elsewhere"}, {"work_request": {"text": "남의 글"}}, {"capability": "\ud800"}):
            with self.subTest(changed=sorted(changed)):
                path.write_text(json.dumps(dict(doc, **changed)))
                self.assertEqual(peer_request_cmd._route_notes(me, "[bb]", ["rt-r1"]), "")


class ClaudePair(Cases, unittest.TestCase):
    harness = "claude"


class CodexPair(Cases, unittest.TestCase):
    harness = "codex"


class OpenCodePair(Cases, unittest.TestCase):
    harness = "opencode"


if __name__ == "__main__":
    unittest.main()
