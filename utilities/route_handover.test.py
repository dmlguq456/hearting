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
import dispatch_seat_handover as handover  # noqa: E402
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
        # A completion stored under the registered parent goes to the receiver, once, not to the sender.
        record = {"attempt_ids": ["att-r1"]}
        self.assertEqual(handover.record_recipient_binding(record, A, harness=h, jobs=self.jobs), (h, B))
        self.assertFalse(handover.record_for_session(record, None, session=A, harness=h, storage_key=A, jobs=self.jobs))
        self.assertTrue(handover.record_for_session(record, None, session=B, harness=h, storage_key=A, jobs=self.jobs))
        scopes = dict(handover.storage_recipients(B, harness=h))
        self.assertEqual(set(scopes[A]), {"att-r1", "att-r2"})
        # An attention notice asks the attempt's parent: the same holder answers.
        notice = handover.resolve_recipient("attempt-parent", metadata={
            "session_id": A, "harness": h, "attempt_id": "att-r1"}, jobs=self.jobs)
        self.assertEqual((notice.session_id, notice.harness), (B, h))
        # Questions, requests and the steward's own succession stay with the sender.
        self.assertEqual(handover.session_successor(A, harness=h), ("", ""))
        self.assertEqual([r["id"] for r in pr.session_requests(h, A)["out"]], [request_id])
        self.assertEqual(self.ledger_rows(A), [])
        self.assertIn("--- handed route (runtime) ---", self.body_of(request_id))

    def test_only_the_holder_can_hand_a_route_on(self):
        h = self.harness
        meta = self.row("att-r1", "rt-r1")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        rc, out = self.hand(C, "rt-r1")
        self.assertEqual((rc, out), (1, "request=- delivery=refused reason=route-not-held route=rt-r1"))
        self.assertEqual(pr.session_requests(h, C)["in"], [])
        self.assertEqual(self.ledger_rows(C), [])
        self.assertEqual(self.holder(meta), B)
        self.assertFalse(handover.lineage_graph().binding_conflicts)
        self.assertEqual(handover.record_recipient_binding({"attempt_ids": ["att-r1"]}, A, harness=h,
                                                           jobs=self.jobs), (h, B))
        rc, out = self.hand(C, "rt-r1", who=B)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.endswith(" route=rt-r1 holder=[cc]"), out)
        self.assertEqual(self.holder(meta), C)
        self.assertFalse(handover.lineage_graph().binding_conflicts)

    def test_refusals_send_and_record_nothing(self):
        held = self.row("att-r1", "rt-r1")
        self.row("att-x", "rt-x", parent=C)
        self.row("att-done", "rt-done", status="done")
        cases = [((B, "rt-x"), "route-not-held route=rt-x"),
                 ((B, "rt-done"), "route-closed route=rt-done"),
                 ((B, "rt-unknown"), "route-not-held route=rt-unknown"),
                 ((B, "rt-r1", "rt-x"), "route-not-held route=rt-r1,rt-x"),
                 (("sid-dddd-5555", "rt-r1"), "harness-mismatch route=rt-r1")]
        for argv, reason in cases:
            with self.subTest(argv=argv):
                self.assertEqual(self.hand(*argv), (1, "request=- delivery=refused reason=" + reason))
        self.liveness[B] = "ended"
        self.assertEqual(self.hand(B, "rt-r1"), (1, "request=- delivery=refused reason=receiver-not-live route=rt-r1"))
        self.assertEqual(self.holder(held), A)
        self.nothing_written()
        self.assertEqual(sorted(peer_message._pending_dir().glob("*.json"))
                         if hasattr(peer_message, "_pending_dir") else [], [])

    def test_without_route_the_request_is_unchanged(self):
        self.row("att-r1", "rt-r1")
        rc, out = self.hand(B)
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"^request=rq-[0-9a-f]{6} delivery=queued-not-delivered to=\[bb\] harness=%s "
                              r"via=\S+ reason=\S+ next=\S+$" % self.harness)
        self.assertNotIn("route=", out)
        self.assertEqual(self.body_of(out.split()[0].split("=")[1]), "이 흐름을 맡아 주세요")
        sessions = st.state_root() / "sessions"
        self.assertEqual(sorted(sessions.glob("*.jsonl")) if sessions.exists() else [], [])
        rc, out = self.hand(out.split()[0].split("=")[1], "rt-r1")
        self.assertEqual(rc, 1)
        self.assertTrue(out.endswith("reason=route-needs-new-request"), out)

    def test_a_failed_write_or_seal_leaves_no_half_handover(self):
        h = self.harness
        meta = self.row("att-r1", "rt-r1")
        # The ownership row cannot be written: nothing is sealed or sent.
        with mock.patch.object(handover, "_append_transition_row", side_effect=OSError("disk")):
            self.assertEqual(self.hand(B, "rt-r1"),
                             (1, "request=- delivery=refused reason=handover-unrecorded route=rt-r1"))
        self.assertEqual(self.holder(meta), A)
        self.nothing_written()
        # The row was written but no seal happened: the row is taken back.
        with mock.patch.object(peer_message, "prepare_peer_message", side_effect=OSError("seal")):
            rc, out = self.hand(B, "rt-r1")
        self.assertEqual(rc, 5, out)
        self.assertNotIn("route=", out)
        self.assertEqual(self.holder(meta), A)
        self.assertEqual(self.ledger_rows(B), [])
        self.assertEqual(pr.session_requests(h, A)["out"], [])
        # Sealed but its record update failed: the request is still delivered, so the route stays moved.
        with mock.patch.object(pr, "confirm", side_effect=ValueError("record")):
            rc, out = self.hand(B, "rt-r1")
        self.assertEqual(rc, 5, out)
        self.assertTrue(out.endswith(" route=rt-r1 holder=[bb]"), out)
        self.assertEqual(self.holder(meta), B)

    def test_receiver_clear_and_retire_follow_the_existing_lineage(self):
        h = self.harness
        meta = self.row("att-r1", "rt-r1")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        handover._append_transition_row(st._pane_seat_of("w1:pB"), {
            "ts": time.time(), "harness": h, "sid": B2, "event": "handover", "source": "clear",
            "from": {"harness": h, "sid": B}, "bindings": []})
        self.assertEqual(self.holder(meta), B2)
        self.assertEqual(handover.record_recipient_binding({"attempt_ids": ["att-r1"]}, A, harness=h,
                                                           jobs=self.jobs), (h, B2))
        row = handover.record_retire_handover(B2, h, C, h, jobs=self.jobs,
                                              successor_seat=st._pane_seat_of("w1:pC"))
        self.assertEqual([b["attempt"] for b in row["bindings"]], ["att-r1"])
        self.assertEqual(self.holder(meta), C)
        self.assertFalse(handover.lineage_graph().binding_conflicts)

    def test_folding_keeps_an_assign_row_while_its_route_is_open(self):
        h = self.harness
        meta = self.row("att-r1", "rt-r1")
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        seat = handover._assignment_seat(h, B)
        path = st._ledger_path(seat)
        filler = [{"ts": time.time() + i, "harness": h, "sid": "sid-x%04d" % i, "event": "handover",
                   "source": "retire", "from": {"harness": h, "sid": "sid-y%04d" % i}, "bindings": []}
                  for i in range(st.HANDOVER_KEEP_ROWS + 5)]
        filler += [{"ts": time.time(), "harness": h, "sid": B, "event": "prompt"}] * st.LEDGER_FOLD_LINES
        with path.open("a") as handle:
            handle.writelines(json.dumps(item) + "\n" for item in filler)
        st._fold_ledger(seat)
        self.assertEqual([r["source"] for r in self.ledger_rows(B) if r.get("source") == "assign"], ["assign"])
        self.assertEqual(self.holder(meta), B)
        self.row("att-r1", "rt-r1", status="done")      # finished, no open route or delivery
        with path.open("a") as handle:
            handle.writelines(json.dumps(item) + "\n" for item in filler)
        st._fold_ledger(seat)
        self.assertEqual([r for r in self.ledger_rows(B) if r.get("source") == "assign"], [])

    def test_running_routes_count_for_the_holder(self):
        self.row("att-r1", "rt-r1")
        with mock.patch.object(dispatch_supervision, "update_attempt_stall_observation",
                               return_value={"state": "active"}):
            self.assertEqual(dispatch_supervision.running_route_ids(self.jobs, ["rt-r1"], A), {"rt-r1"})
            self.assertEqual(self.hand(B, "rt-r1")[0], 0)
            self.assertEqual(dispatch_supervision.running_route_ids(self.jobs, ["rt-r1"], A), set())
            self.assertEqual(dispatch_supervision.running_route_ids(self.jobs, ["rt-r1"], B), {"rt-r1"})

    def test_a_continuation_joins_the_holders_chain(self):
        import capability_route_base as base
        h = self.harness
        rc = base._route_chain_module()

        def chain(sid, route):
            rc.append(h, sid, {"v": rc.SCHEMA, "harness": h, "session_id": sid, "route_id": route,
                               "route_file": "/synthetic/%s.json" % route})
        chain(A, "rt-r1")
        self.row("att-r1", "rt-r1")
        os.environ.update(AGENT_DISPATCH_DEPTH="1", AGENT_DISPATCH_PARENT_SESSION_ID=A,
                          AGENT_DISPATCH_ATTEMPT_ID="att-r1")
        identity = base._route_chain_identity("continuation", {"source_route_id": "rt-r1"})
        self.assertEqual(identity, (h, A, 1, "att-r1"))
        self.assertEqual(self.hand(B, "rt-r1")[0], 0)
        identity = base._route_chain_identity("continuation", {"source_route_id": "rt-r1"})
        self.assertEqual(identity, (h, B, 1, "att-r1"))
        chain(B, "rt-r1-next")
        identity = base._route_chain_identity("continuation", {"source_route_id": "rt-r1-next"})
        self.assertEqual(identity, (h, B, 1, "att-r1"))

    def test_the_note_carries_record_facts_within_two_kilobytes(self):
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
        # With the sender's card: its path, update time and how many of the person's words it keeps.
        card = st.card_json_path(handover._assignment_seat(h, B))
        card.parent.mkdir(parents=True, exist_ok=True)
        card.write_text(json.dumps({"authored_at_epoch": 0, "body": "사용자 요구 원문 발췌:\n- \"하나\"\n- \"둘\""}))
        line = handover._assignment_seat(h, B)
        rc, out = self.hand(C, "rt-r1", who=B)
        self.assertEqual(rc, 0, out)
        body = self.body_of(out.split()[0].split("=")[1])
        self.assertRegex(body, r"\n감독 카드: %s \(갱신 \d\d-\d\d \d\d:\d\d · 사용자 요구 원문 발췌 2건\) — "
                               r"이 흐름에 맞는 것만 따르세요$" % re.escape(str(card)))
        self.assertTrue(line)
        import peer_request_cmd
        with mock.patch.object(peer_request_cmd, "_card_line", return_value="x" * 5000):
            note = peer_request_cmd._route_notes({"harness": h, "session_id": C}, "[aa]", ["rt-r1"])
        self.assertLessEqual(len(note.split("\n", 1)[1].encode("utf-8")), peer_request_cmd._ROUTE_NOTE_BYTES)
        self.assertTrue(note.split("\n", 1)[1].startswith("route rt-r1 · autopilot-code"))


class ClaudePair(Cases, unittest.TestCase):
    harness = "claude"


class CodexPair(Cases, unittest.TestCase):
    harness = "codex"


class OpenCodePair(Cases, unittest.TestCase):
    harness = "opencode"


if __name__ == "__main__":
    unittest.main()
