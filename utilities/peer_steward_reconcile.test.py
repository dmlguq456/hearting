#!/usr/bin/env python3
"""Slice-4 runner duties and notice d6-d9 contracts (narrow verify).

Every state root here is an isolated temporary home: no test touches live
dispatch, session-tidy, peer, obligation or run-registry state. The only
external observations (herdr census, pane process info) are replaced with
explicit fakes; a missing fake is an unknown observation, which the product
code must treat as "do not act".
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

_STEWARD_SPEC = importlib.util.spec_from_file_location(
    "peer_steward_reconcile_test", str(ROOT / "utilities" / "peer-steward.py"))
peer_steward = importlib.util.module_from_spec(_STEWARD_SPEC)
_STEWARD_SPEC.loader.exec_module(peer_steward)

import dispatch_completion_join as join  # noqa: E402
import dispatch_pending_delivery as pending  # noqa: E402
import dispatch_recipient_inbox as inbox  # noqa: E402
import dispatch_supervision as supervision  # noqa: E402
import herdr_observation  # noqa: E402
import peer_obligations  # noqa: E402
import peer_steward_native  # noqa: E402
import session_notice  # noqa: E402
import session_tidy as tidy  # noqa: E402
from route_identity import route_hash  # noqa: E402
from split_module_fixture import patch_split  # noqa: E402


def _notice(harness, sid, *, key, subject, text, jobs,
            required_action="read-notice", route_id="route-free"):
    """One stored notice record plus its retained run-notice duty."""
    import hashlib
    receipt = session_notice.validate({
        "schema_version": 1, "kind": "notice", "key": key,
        "subject": subject, "text": " ".join(text.split())[:600],
        "required_action": required_action})
    from harness_capabilities import parent_completion
    delivery_id = "delivery-" + hashlib.sha256(f"notice\0{key}".encode()).hexdigest()
    tag = "notice-" + delivery_id.removeprefix("delivery-")[:32]
    root = Path(jobs).parent
    record = pending.read(root, sid, delivery_id)
    if record is None:
        record = pending.create(
            root, recipient_kind=parent_completion(harness)["carrier"],
            recipient_key=sid, delivery_id=delivery_id, session_generation="",
            session_generation_supported="0", attempt_ids=[tag], parent_attempt_id=tag,
            route_id=route_id, route_node="notice", receipt=receipt,
            receipt_digest=session_notice.receipt_digest(receipt),
            row_revisions={tag: "notice"}, lineage=[])
    peer_obligations.retain_session_notice(Path(jobs), harness, sid, record)
    return record


class IsolatedCase(unittest.TestCase):
    """A private HOME/XDG/dispatch root per test; nothing resolves to live state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="runner-notices-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._saved_environ = dict(os.environ)
        self.addCleanup(self._restore_environ)
        for key in tuple(os.environ):
            if key.startswith(("AGENT_", "HERDR_", "OPENCODE_", "CLAUDE_", "CODEX_",
                               "XDG_", "HARNESS_")):
                os.environ.pop(key, None)
        self.home = self.root / "home"
        self.home.mkdir()
        os.environ.update({
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "AGENT_DISPATCH_JOBS": str(self.root / "jobs.log"),
            "AGENT_PEER_LEDGER_ROOT": str(self.root),
        })
        self.jobs = self.root / "jobs.log"
        self.jobs.touch()
        self._old_herdr = peer_steward._HERDR_SESSION
        peer_steward._HERDR_SESSION = None
        self.addCleanup(setattr, peer_steward, "_HERDR_SESSION", self._old_herdr)
        self.store = peer_obligations.ObligationStore(self.root)
        self.assertTrue(self.store.ensure_directory())
        runner_patch = mock.patch.object(peer_obligations, "ensure_normal_runner",
                                         return_value=False)
        runner_patch.start()
        self.addCleanup(runner_patch.stop)
        peer_steward._LINEAGE_GRAPH_CACHE.clear() if hasattr(peer_steward, "_LINEAGE_GRAPH_CACHE") else None
        import dispatch_seat_handover as handover
        handover._LINEAGE_GRAPH_CACHE.clear()
        self.addCleanup(handover._LINEAGE_GRAPH_CACHE.clear)

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self._saved_environ)

    # -- fixtures ---------------------------------------------------------
    def edge(self, origin, target, *, harness="claude", source="tidy-clear",
             event="succession", ts=1):
        tidy._append_ledger_entry(
            tidy.Seat("pane", f"ledger-{target}", harness=harness),
            {"event": event, "source": source, "harness": harness, "sid": target,
             "from": {"harness": harness, "sid": origin}, "ts": ts})

    def write_ledger(self, key, rows):
        path = tidy.state_root() / "sessions" / f"{key}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
        return path

    def duties(self):
        return self.store.list(kinds={"session-notice", "run-notice"})

    @contextlib.contextmanager
    def transport(self, sid="B", *, sent=None):
        """A shell-attached recipient; the courier's prompt is counted, never typed."""
        import session_shell
        binding = {"harness": "claude", "sid": sid, "native_pid": 0}

        def exact_binding(harness, session_id):
            return (binding, None) if (harness, session_id) == ("claude", sid) else (
                None, "attachment-missing")

        def courier(args, *, expected_recipient, on_prepared, producer_binding):
            body = Path(args.body_file).read_text()
            if sent is not None:
                sent.append({"body": body, "recipient": expected_recipient,
                             "target": args.target})
            return 3

        with mock.patch.object(session_shell, "exact_binding", side_effect=exact_binding), \
             mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier):
            yield

    def completion_fixture(self, *, attempt="att-run-parent", sid="A",
                           route_id="rt-run", carrier="claude-parent-runtime",
                           delivery_id="delivery-run-parent",
                           parent_attempt="att-parent"):
        """One exact terminal attempt row and its open completion record."""
        route = {"route_id": route_id, "parent_cycle_id": "cycle-run",
                 "nodes": [{"id": "frame"}]}
        route["route_hash"] = route_hash(route)
        route_path = self.root / (route_id + ".json")
        route_path.write_text(json.dumps(route))
        body = {"schema_version": 2, "state": "ready",
                "parent_attempt_id": parent_attempt, "job_registry": str(self.jobs),
                "delivery_classification": "success",
                "children": [{"attempt_id": attempt, "status": "done",
                              "readiness": "ready", "reason": "terminal-complete",
                              "required_action": "advance-completed",
                              "harness": "claude",
                              "delivery_classification": "success"}]}
        record = pending.create(
            self.root, recipient_kind=carrier, recipient_key=sid,
            delivery_id=delivery_id, session_generation="",
            session_generation_supported="0", attempt_ids=[attempt],
            parent_attempt_id=parent_attempt, route_id=route_id, route_node="frame",
            receipt=body, receipt_digest=pending._canonical_receipt_digest(body),
            row_revisions={attempt: "revision"})
        meta = dict(attempt_id=attempt, attempt_schema_version="2", dispatch_depth="1",
            registered_worker="1", worker_type="frame", parent_sid=sid,
            parent_harness="claude", parent_attempt_id=parent_attempt,
            parent_completion_delivery=carrier, delivery_intent="1",
            delivery_intent_at_ns=str(time.monotonic_ns()), delivery_id=delivery_id,
            delivery_receipt_digest=record["receipt_digest"],
            delivery_row_revision="revision", failure_class="pass",
            note="completed-marker", launch_outcome="reaped-before-publish",
            route_file=str(route_path), route_id=route_id,
            route_hash=route["route_hash"], route_node="frame")
        with self.jobs.open("a") as handle:
            handle.write("now\tdone\t/fixture/repo\t/fixture/wt\tframe\t"
                         + ",".join(key + "=" + value for key, value in meta.items())
                         + "\n")
        return record, meta

    def publish_run_state(self, run_id, *, status="succeeded", exit_code=0,
                          parent_attempt="att-run-parent"):
        inbox.write_run_states(inbox.dispatch_inbox_root(), {"runs": {run_id: {
            "status": status, "exit_code": exit_code, "ended_at": 123.0,
            "artifact_root": str(self.root / "artifacts"),
            "parent_attempt_id": parent_attempt, "jobs": str(self.jobs)}}})


class ClassifyTest(IsolatedCase):
    def test_terminal_forms_use_existing_receipt_fields_only(self):
        base = {"schema_version": 1, "kind": "notice", "text": "t",
                "subject": "notice", "required_action": "read-notice",
                "key": "checkpoint:1"}
        self.assertEqual(session_notice.classify(base), session_notice.PROGRESS)
        self.assertEqual(session_notice.classify(
            {**base, "required_action": "report-to-user"}), session_notice.TERMINAL)
        # A final whose outcome the runner has not published yet needs attention; a
        # published success, or one its producer called quiet, is routine.
        final = {**base, "key": "experiment:run-1:final:1:claude:A",
                 "subject": "experiment final (1/1)"}
        self.assertEqual(session_notice.classify(final), session_notice.ATTENTION)
        self.publish_run_state("run-1", status="succeeded")
        self.assertEqual(session_notice.classify(final), session_notice.ROUTINE)
        self.publish_run_state("run-1", status="failed", exit_code=6)
        self.assertEqual(session_notice.classify(final), session_notice.ATTENTION)
        self.assertEqual(session_notice.classify(
            {**final, "subject": "experiment final, quiet (1/1)", "key": "experiment:run-2:final:1:claude:A"}),
            session_notice.ROUTINE)
        # An experiment attention is attention whatever its subject (it was `progress` before).
        self.assertEqual(session_notice.classify(
            {**base, "key": "experiment:run-1:attention:abc",
             "subject": "experiment final (1/1)"}), session_notice.ATTENTION)
        self.assertEqual(session_notice.classify(
            {**base, "key": "experiment:run-1:attention:abc"}), session_notice.ATTENTION)
        self.assertEqual(session_notice.classify(
            {**base, "key": "experiment:run-1:attention-checkpoint:abc:7:d"}), session_notice.PROGRESS)
        self.assertEqual(session_notice.classify(
            {**base, "key": "experiment:run-1:checkpoint:run:7:1:claude:A"}), session_notice.ROUTINE)
        compute = {**base, "key": "compute-run:h-20261114-101010-x", "subject": "compute run",
                   "required_action": "report-to-user",
                   "text": "run h-20261114-101010-x on h ended with exit 0; read it with: compute-hosts tail h-20261114-101010-x"}
        self.assertEqual(session_notice.classify(compute), session_notice.ROUTINE)
        self.assertEqual(session_notice.classify(
            {**compute, "text": compute["text"].replace("exit 0", "exit 6")}), session_notice.ATTENTION)
        # A broken receipt is progress, never a terminal wake.
        self.assertEqual(session_notice.classify({"kind": "notice"}), session_notice.PROGRESS)


class TerminalFirstDeliveryTest(IsolatedCase):
    def test_terminal_notice_is_sent_before_progress_in_the_same_group(self):
        progress = _notice("claude", "A", key="checkpoint:1", subject="progress",
                           text="routine progress", jobs=self.jobs)
        terminal = _notice("claude", "A", key="experiment:run-1:final:1:claude:A",
                           subject="experiment final (1/1)",
                           text="experiment finished", jobs=self.jobs)
        sent = []
        with mock.patch.object(peer_steward, "NOTICE_BATCH_LIMIT", 1), \
             self.transport("A", sent=sent):
            peer_steward._resume_session_notice_obligations(self.duties(), self.store)
        self.assertEqual(len(sent), 1)
        self.assertIn("experiment finished", sent[0]["body"])
        self.assertNotIn("routine progress", sent[0]["body"])
        self.assertEqual(pending.read(self.root, "A", terminal["delivery_id"])["state"],
                         "sent-ambiguous")
        self.assertEqual(pending.read(self.root, "A", progress["delivery_id"])["state"],
                         "pending")
        # The next pass carries the remaining progress notice.
        pending.ack(self.root, "A", terminal["delivery_id"], acked_by="fixture-receipt")
        with mock.patch.object(peer_steward, "NOTICE_BATCH_LIMIT", 1), \
             self.transport("A", sent=sent):
            peer_steward._resume_session_notice_obligations(self.duties(), self.store)
        self.assertEqual(len(sent), 2)
        self.assertIn("routine progress", sent[1]["body"])


class EndedOwnerNoticeTest(IsolatedCase):
    """A run notice whose owner session ended is never re-sent; it closes by TTL.

    The producer (``resource_progress._notice_targets``) makes one notice per
    destination at the start: one for the owner session and one for the
    owner's parent, the parent's own copy. So when the owner has ended, the
    parent already holds the failure, and the owner's copy has nowhere to go:
    the resolver refuses it, nobody is woken for it, and the existing notice
    TTL (plus the dead-lease reclaim) closes it.
    """

    DAY = 86400

    def world(self, token, *, harness="claude", status="done", attention=True,
              owner="sid-owner", supervisor="sid-supervisor",
              supervisor_harness="claude", waiting_supervisor=False,
              with_parent=True):
        """One isolated state root: the owner's row and the producer's notices."""
        import resource_progress
        from harness_capabilities import parent_completion
        state = self.root / f"world-{token}"
        state.mkdir()
        jobs = state / "jobs.log"
        store = peer_obligations.ObligationStore(state)
        self.assertTrue(store.ensure_directory())
        sup_carrier = parent_completion(supervisor_harness)["carrier"]
        attempt = f"att-{token}"
        meta = ",".join(("attempt_schema_version=2", f"attempt_id={attempt}",
                         "dispatch_depth=1", "worker_type=owner", f"harness={harness}",
                         f"parent_sid={supervisor}", f"parent_harness={supervisor_harness}",
                         f"parent_completion_delivery={sup_carrier}", "registered_worker=1",
                         f"route_id=rt-{token}", f"route_hash={'h' * 8}", "route_node=execute"))
        world = SimpleNamespace(state=state, jobs=jobs, store=store, attempt=attempt,
                                owner=owner, supervisor=supervisor, harness=harness,
                                meta=meta, run=f"run-{token}", records={})
        self.set_status(world, status)
        if waiting_supervisor:
            join._write_supervisor_state_unlocked(
                state / "supervisor-state" / (attempt + ".json"), attempt, set(),
                resource={"session_id": owner, "delivered": [], "outbox": None})
        if attention:
            subject, text = "compute run", (
                f"run {world.run} on here ended with exit 6; read it with: "
                f"compute-hosts tail {world.run}")
        else:
            subject, text = "compute run, quiet", (
                f"run {world.run} on here ended with exit 0; read it with: "
                f"compute-hosts tail {world.run}")
        # The producer's own destinations, then one notify per destination --
        # exactly what the resource-progress sender does for one event.
        targets = resource_progress._notice_targets({"jobs": str(jobs), "owner_wait": {
            "harness": harness, "session_id": owner, "parent_attempt_id": attempt,
            "jobs": str(jobs)}})
        kinds = {target["lineage"]["kind"] if isinstance(target, dict) else None
                 for target in targets}
        self.assertEqual(kinds, {"owner-session", "attempt-parent"})
        for target in targets:
            kind = target["lineage"]["kind"]
            if kind == "attempt-parent" and not with_parent:
                continue
            record = session_notice.notify(
                target["harness"], target["session_id"],
                key=f"compute-run:{world.run}:{target['harness']}:{target['session_id']}",
                subject=subject, text=text, lineage=target["lineage"], jobs=jobs)
            world.records[kind] = (target["session_id"], record)
        for duty in store.list(kinds={"run-notice"}):
            self.backdate(world, duty, hours=1.0)
        return world

    def set_status(self, world, status):
        world.jobs.write_text("\t".join(("2026-11-14T00:00:00Z", status, "/repo", "/wt",
                                         "owner", world.meta)) + "\n")
        import dispatch_seat_handover as handover
        handover._LINEAGE_GRAPH_CACHE.clear()

    def backdate(self, world, duty, *, hours):
        path = world.store._record_path(duty["id"])
        value = json.loads(path.read_text("utf-8"))
        value["accepted_at"] = time.time() - hours * 3600
        path.write_text(json.dumps(value), encoding="utf-8")

    def record_state(self, world, kind):
        sid, record = world.records[kind]
        return pending.read(world.state, sid, record["delivery_id"])

    def age_record(self, world, kind, *, days):
        sid, record = world.records[kind]
        old = time.time() - days * self.DAY
        os.utime(pending.record_path(world.state, sid, record["delivery_id"]), (old, old))

    def _pass(self, world, *, sent=None, lifetime=None, recipient=None):
        context = mock.patch.object(peer_steward_native, "native_lifetime",
                                    return_value=lifetime) if lifetime is not None else contextlib.nullcontext()
        with context, self.transport(recipient or world.supervisor, sent=sent):
            peer_steward._resume_session_notice_obligations(
                world.store.list(kinds={"run-notice"}), world.store)

    def failures_to(self, sent, world):
        return [entry for entry in sent if f"run {world.run} on here ended with exit 6" in entry["body"]]

    def test_the_parent_gets_the_failure_exactly_once_when_both_notices_wait(self):
        # Same pass: the owner ended while both of the producer's notices are
        # still pending. The parent's own copy is delivered; the owner's copy is
        # not re-sent to the parent, so the parent is woken once for the failure.
        for status, lifetime in (("killed", "live"), ("cancelled", "live"), ("done", "ended")):
            for harness in ("claude", "codex", "opencode"):
                with self.subTest(status=status, harness=harness):
                    world = self.world(f"same-{status}-{harness}", harness=harness, status=status)
                    sent = []
                    self._pass(world, sent=sent, lifetime=lifetime)
                    self._pass(world, sent=sent, lifetime=lifetime)
                    self.assertEqual(len(self.failures_to(sent, world)), 1)
                    self.assertEqual(sent[0]["recipient"], ("claude", world.supervisor))
                    self.assertEqual(self.record_state(world, "owner-session")["state"], "pending")
                    self.assertEqual(self.record_state(world, "attempt-parent")["state"], "sent-ambiguous")

    def test_the_parent_gets_the_failure_exactly_once_when_its_copy_was_read_first(self):
        # The owner is still alive (no waiting supervisor yet) when the failure
        # lands: the parent's copy is delivered and read. Only afterwards the
        # owner is killed; the parent is not woken a second time for the owner's copy.
        world = self.world("read-first", status="open")
        sent = []
        self._pass(world, sent=sent)
        self.assertEqual(len(self.failures_to(sent, world)), 1)
        sid, record = world.records["attempt-parent"]
        pending.ack(world.state, sid, record["delivery_id"], acked_by="fixture-receipt")
        self.set_status(world, "killed")
        self._pass(world, sent=sent)
        self._pass(world, sent=sent, lifetime="ended")
        self.assertEqual(len(self.failures_to(sent, world)), 1)
        self.assertEqual(self.record_state(world, "owner-session")["state"], "pending")

    def test_an_ended_owners_notice_wakes_nobody_and_closes_by_the_seven_day_ttl(self):
        for attention in (True, False):
            with self.subTest(attention=attention):
                world = self.world(f"ttl-{attention}", status="killed",
                                   attention=attention, with_parent=False)
                sent = []
                self._pass(world, sent=sent)
                self.assertEqual(sent, [])
                duty, = world.store.list(kinds={"run-notice"})
                self.assertEqual(duty["state"], "pending")
                self.assertEqual((duty.get("observation") or {}).get("reason"),
                                 "recipient-lineage-unavailable")
                # Before the age limit nothing closes; at eight days the notice TTL does.
                self.age_record(world, "owner-session", days=6)
                self.assertEqual(join.reconcile_pending_delivery(world.jobs)["expired"], 0)
                self.age_record(world, "owner-session", days=8)
                self.assertEqual(join.reconcile_pending_delivery(world.jobs)["expired"], 1)
                self.assertEqual(self.record_state(world, "owner-session")["state"], "expired")
                self._pass(world, sent=sent)
                self.assertEqual(sent, [])
                self.assertEqual(world.store.list(kinds={"run-notice"}), [])

    def test_an_owner_that_cannot_receive_is_neither_served_nor_redirected(self):
        # Every way the owner-session answer is refused keeps its notice
        # pending with nobody woken: no waiting supervisor, an unproven end, a
        # confirmed successor of the owner's seat.
        cases = {
            "done-without-waiting-supervisor": dict(status="done", lifetime="ended"),
            "done-session-not-provably-ended": dict(status="done", lifetime="unknown"),
            "killed-with-successor": dict(status="killed", lifetime="live", successor=True),
        }
        for name, case in cases.items():
            with self.subTest(case=name):
                world = self.world(f"refused-{name}", status=case["status"], with_parent=False)
                if case.get("successor"):
                    self.edge(world.owner, "sid-next")
                sent = []
                self._pass(world, sent=sent, lifetime=case["lifetime"])
                self.assertEqual(sent, [])
                duty, = world.store.list(kinds={"run-notice"})
                self.assertEqual(duty["state"], "pending")

    def test_a_live_owner_keeps_its_own_notice(self):
        # An owner whose waiting supervisor still names it receives its notice
        # itself, as before; the parent's copy is the parent's own.
        world = self.world("alive", status="open", waiting_supervisor=True, with_parent=False)
        sent = []
        self._pass(world, sent=sent, recipient=world.owner)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["recipient"], ("claude", world.owner))

    def _claim(self, world, *, lease_seconds):
        sid, record = world.records["owner-session"]
        claimed = pending.claim(world.state, sid, record["delivery_id"],
                                claim_owner="dead-runner", lease_seconds=lease_seconds)
        self.assertEqual(claimed["state"], "claimed")

    def test_a_dead_lease_on_an_unresolvable_notice_is_reclaimed_then_expires(self):
        world = self.world("lease-dead", status="killed", with_parent=False)
        self._claim(world, lease_seconds=0.001)
        time.sleep(0.05)
        sent = []
        self._pass(world, sent=sent)
        self.assertEqual(sent, [])
        record = self.record_state(world, "owner-session")
        self.assertEqual(record["state"], "pending")
        self.assertIsNone(record["claim_owner"])
        # Reclaimed, it ends like any other unreachable notice.
        self.age_record(world, "owner-session", days=8)
        self.assertEqual(join.reconcile_pending_delivery(world.jobs)["expired"], 1)
        self.assertEqual(self.record_state(world, "owner-session")["state"], "expired")

    def test_a_live_lease_on_an_unresolvable_notice_is_left_alone(self):
        world = self.world("lease-live", status="killed", with_parent=False)
        self._claim(world, lease_seconds=600)
        sent = []
        self._pass(world, sent=sent)
        self.assertEqual(sent, [])
        record = self.record_state(world, "owner-session")
        self.assertEqual(record["state"], "claimed")
        self.assertEqual(record["claim_owner"], "dead-runner")


class RunNoticeSupersedeTest(IsolatedCase):
    def run_notice(self, run_id="run-1", sid="A"):
        return _notice("claude", sid, key=f"experiment:{run_id}:final:1:claude:{sid}",
                       subject="experiment final (1/1)", text="experiment finished",
                       jobs=self.jobs)

    def test_exact_parent_completion_retires_the_run_notice(self):
        record, _meta = self.completion_fixture()
        self.publish_run_state("run-1")
        notice = self.run_notice()
        self.assertFalse(session_notice.notice_is_current(notice))
        self.assertEqual(session_notice.fold_summary(notice),
                         "compute run summary: run run-1 status=succeeded exit=0")
        # Unknown or still-running state keeps the notice current.
        self.publish_run_state("run-1", status="running", exit_code=None)
        self.assertTrue(session_notice.notice_is_current(notice))
        inbox.run_states_path(inbox.dispatch_inbox_root()).unlink()
        self.assertTrue(session_notice.notice_is_current(notice))
        # A missing exact completion record is also unknown, never folded.
        self.publish_run_state("run-1")
        record_path = pending.record_path(self.root, "A", record["delivery_id"])
        record_path.unlink()
        self.assertTrue(session_notice.notice_is_current(notice))

    def test_superseded_run_notice_is_folded_into_the_sent_body(self):
        self.completion_fixture()
        self.publish_run_state("run-1")
        folded = self.run_notice()
        kept = _notice("claude", "A", key="checkpoint:2", subject="progress",
                       text="still running", jobs=self.jobs)
        sent = []
        with self.transport("A", sent=sent):
            peer_steward._resume_session_notice_obligations(self.duties(), self.store)
        self.assertEqual(len(sent), 1)
        self.assertIn("still running", sent[0]["body"])
        self.assertIn("compute run summary: run run-1 status=succeeded exit=0",
                      sent[0]["body"])
        self.assertNotIn("experiment finished", sent[0]["body"])
        self.assertEqual(pending.read(self.root, "A", folded["delivery_id"])["state"],
                         "rejected")
        self.assertEqual(pending.read(self.root, "A", kept["delivery_id"])["state"],
                         "sent-ambiguous")


class BatchClaimContinuationTest(IsolatedCase):
    def test_a_non_foldable_rejection_does_not_abandon_a_terminal_claim(self):
        terminal = _notice("claude", "A", key="experiment:run-9:final:1:claude:A",
                           subject="experiment final (1/1)", text="experiment finished",
                           jobs=self.jobs)
        stale = _notice("claude", "A", key="steward-check:claude:A:epoch:1",
                        subject="stale role check", text="stale", jobs=self.jobs)
        # No active steward-check duty vouches for this key: keep_claim rejects
        # it, and it is not a run notice, so there is no folded replacement.
        self.assertFalse(session_notice.notice_is_current(stale))
        self.assertIsNone(session_notice.fold_summary(stale))
        sent = []
        with self.transport("A", sent=sent):
            peer_steward._resume_session_notice_obligations(self.duties(), self.store)
        self.assertEqual(len(sent), 1)
        self.assertIn("experiment finished", sent[0]["body"])
        self.assertEqual(pending.read(self.root, "A", terminal["delivery_id"])["state"],
                         "sent-ambiguous")
        self.assertEqual(pending.read(self.root, "A", stale["delivery_id"])["state"],
                         "rejected")


class RunnerPassLineageReadTest(IsolatedCase):
    """One runner pass observes lineage a fixed number of times, whatever the open notice count.

    Per-notice lineage observations (a directory walk, or a read and hash of every
    ledger) made the live runner use 34-86% CPU with jobs.log unchanged
    (2026-11-04). Counts, not timings. The per-member transport check stays fresh by
    design and is counted apart.
    """

    @contextlib.contextmanager
    def counting(self):
        import dispatch_recipient_receipt as receipt
        import dispatch_seat_handover as handover
        counts = {"lineage_signature": 0, "readable_lineage_signature": 0}
        transport = []
        real_stat = handover._lineage_signature
        real_readable = handover._readable_lineage_signature
        real_send_current = receipt.claimed_send_current

        def stat(directory):
            if not transport:
                counts["lineage_signature"] += 1
            return real_stat(directory)

        def readable(directory):
            if not transport:
                counts["readable_lineage_signature"] += 1
            return real_readable(directory)

        def send_current(*args, **kwargs):
            transport.append(True)
            try:
                return real_send_current(*args, **kwargs)
            finally:
                transport.pop()

        handover._LINEAGE_GRAPH_CACHE.clear()
        with mock.patch.object(handover, "_lineage_signature", new=stat), \
             mock.patch.object(handover, "_readable_lineage_signature", new=readable), \
             mock.patch.object(receipt, "claimed_send_current", new=send_current):
            yield counts

    def open_notices(self, storage, total):
        for index in range(len(self.duties_for(storage)), total):
            _notice("claude", storage, key=f"open-{storage}-{index}", subject="progress",
                    text="routine progress", jobs=self.jobs)

    def duties_for(self, storage):
        return [duty for duty in self.duties()
                if duty["intent"]["identity"].get("storage_sid") == storage]

    def test_discovery_lineage_reads_do_not_grow_with_open_notices(self):
        from peer_read_context import PeerReadContext
        self.edge("A", "B")
        observed = []
        for total in (3, 12):
            self.open_notices("A", total)
            with self.counting() as counts:
                self.assertTrue(peer_steward._discover_session_notice_duties(
                    self.store, read_context=PeerReadContext(registry_append_only=True)))
            observed.append(counts)
        self.assertEqual(len(self.duties()), 12)
        self.assertEqual(observed[0], observed[1])

    def test_resume_lineage_reads_do_not_grow_with_notice_duties(self):
        from peer_read_context import PeerReadContext
        observed = []
        for total in (3, 12):
            storage, recipient = f"A{total}", f"B{total}"
            self.edge(storage, recipient)
            self.open_notices(storage, total)
            duties = self.duties_for(storage)
            self.assertEqual(len(duties), total)
            sent = []
            with self.transport(recipient, sent=sent), self.counting() as counts:
                peer_steward._resume_session_notice_obligations(
                    duties, self.store, read_context=PeerReadContext(registry_append_only=True))
            # The effect group really handed min(total, batch limit) records over.
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["body"].count("routine progress"),
                             min(total, peer_steward.NOTICE_BATCH_LIMIT))
            observed.append(counts)
        self.assertEqual(observed[0], observed[1])


class AttentionRetirementTest(IsolatedCase):
    OWNER = ("att-owner\tdone\t/fixture/repo\t/fixture/wt\towner\t"
             "attempt_id=att-owner,dispatch_depth=1,parent_sid=parent-test,"
             "parent_harness=claude,parent_completion_delivery=claude-parent-runtime,"
             "worker_type=owner,note=dead-worker-blocked")

    def _child(self, attempt, *, meta=None):
        fields = {"attempt_id": attempt, "parent_sid": "parent-test",
                  "parent_attempt_id": "att-owner",
                  "parent_completion_delivery": "claude-parent-runtime",
                  "pid": "99999998", "pid_start": "1"}
        delivery_keys = ("attempt_schema_version", "parent_attempt_id",
                         "delivery_intent", "delivery_intent_at_ns", "delivery_id",
                         "delivery_receipt_digest", "delivery_row_revision",
                         "failure_class", "note", "launch_outcome", "route_file",
                         "route_id", "route_hash", "route_node", "worker_type",
                         "dispatch_depth", "registered_worker")
        fields.update({key: meta[key] for key in delivery_keys if key in (meta or {})})
        return ("%s\tdone\t/fixture/repo\t/fixture/wt\tframe\t" % attempt
                + ",".join(key + "=" + value for key, value in fields.items()))

    def _rows(self, *children):
        self.jobs.write_text(self.OWNER + "\n" + "\n".join(children) + "\n")

    def test_attention_retires_only_when_every_monitored_attempt_has_completion(self):
        rec1, meta1 = self.completion_fixture(
            attempt="att-child", sid="parent-test", route_id="rt-att-1",
            delivery_id="delivery-att-1", parent_attempt="att-owner")
        rec2, meta2 = self.completion_fixture(
            attempt="att-child-2", sid="parent-test", route_id="rt-att-2",
            delivery_id="delivery-att-2", parent_attempt="att-owner")
        self._rows(self._child("att-child", meta=meta1),
                   self._child("att-child-2", meta=meta2))
        notice = supervision.materialize(
            self.jobs, {"att-child", "att-child-2"}, reason="attempt-stalled")[0]
        self.assertIsNotNone(supervision._recorded_completion_result(
            notice["receipt"], require_ack=False))
        with mock.patch.object(supervision, "_pending", return_value=True):
            self.assertFalse(supervision.notice_is_current(notice))
        # One unresolved sibling keeps the (new) attention current: the d8
        # guard must not retire it, so the ordinary currentness verdict wins.
        self._rows(self._child("att-child", meta=meta1),
                   self._child("att-child-2"))
        fresh = supervision.materialize(
            self.jobs, {"att-child", "att-child-2"}, reason="attempt-stalled")[0]
        with mock.patch.object(supervision, "_pending", return_value=True):
            self.assertTrue(supervision.notice_is_current(fresh))


    def test_completion_records_are_never_suppressed_by_attention(self):
        import dispatch_notice_state
        record, _meta = self.completion_fixture()
        self.assertTrue(dispatch_notice_state.notice_is_current(record, jobs=self.jobs))


class ReconcileDutyTest(IsolatedCase):
    def _open_records(self, count):
        from harness_capabilities import parent_completion
        records = []
        for index in range(count):
            body = {"schema_version": 1, "kind": "notice", "subject": "bulk",
                "text": "bulk", "required_action": "read-notice",
                "key": f"bulk-{index}"}
            records.append(pending.create(
                self.root, recipient_kind=parent_completion("claude")["carrier"],
                recipient_key="A", delivery_id=f"delivery-bulk-{index}",
                session_generation="", session_generation_supported="0",
                attempt_ids=[], parent_attempt_id="",
                route_id="route-free", route_node="notice", receipt=body,
                receipt_digest=pending._canonical_receipt_digest(body),
                row_revisions={}))
        return records

    def test_manual_call_is_unchanged_and_bounded_call_stops_at_the_limit(self):
        self._open_records(5)
        unlimited = join.reconcile_pending_delivery(self.jobs)
        self.assertEqual(set(unlimited), {"materialized", "expired", "skipped",
                                          "pruned_records", "pruned_locks", "prune_skipped"})
        full = join.reconcile_pending_delivery(self.jobs, max_rows=500,
                                               time_budget_s=30)
        self.assertEqual(full["records_read"], 5)
        self.assertEqual(full["budget_exhausted"], 0)
        bounded = join.reconcile_pending_delivery(self.jobs, max_rows=2,
                                                  time_budget_s=30)
        self.assertEqual(bounded["records_read"], 2)
        self.assertEqual(bounded["budget_exhausted"], 1)

    def test_retention_prune_is_bounded_per_call_and_runs_without_a_finished_walk(self):
        records = self._open_records(5)
        paths = []
        for record in records:
            delivery_id = record["delivery_id"]
            pending.claim(self.root, "A", delivery_id, claim_owner="test", lease_seconds=60)
            pending.ack(self.root, "A", delivery_id, acked_by="test")
            path = pending.record_path(self.root, "A", delivery_id)
            old = time.time() - 9 * 86400
            os.utime(path, (old, old))
            paths.append(path)
        with mock.patch.object(pending, "PRUNE_RECORD_LIMIT", 2):
            first = join.reconcile_pending_delivery(self.jobs, max_rows=500, time_budget_s=30)
            self.assertEqual(first["pruned_records"], 2)
            self.assertEqual(sum(path.exists() for path in paths), 3)
            # the registry walk gets no time at all; the prune keeps its own budget
            second = join.reconcile_pending_delivery(self.jobs, time_budget_s=0)
            self.assertEqual(second["budget_exhausted"], 1)
            self.assertEqual(second["pruned_records"], 2)
            self.assertEqual(sum(path.exists() for path in paths), 1)
            third = join.reconcile_pending_delivery(self.jobs, max_rows=500, time_budget_s=30)
            self.assertEqual(third["pruned_records"], 1)
        self.assertFalse(any(path.exists() for path in paths))
        self.assertEqual(sum(pending.tombstone_path(path).is_file() for path in paths), 5)

    def test_maintenance_pass_runs_reconcile_once_per_due_window(self):
        self._open_records(3)
        first = peer_steward._runner_maintenance_pass(self.store)
        self.assertIn("reconcile", first)
        self.assertLessEqual(first["reconcile"]["records_read"], 500)
        state = peer_obligations.read_runner_duty_state(self.root, "reconcile")
        self.assertEqual(state["records_read"], 3)
        self.assertIsNone(state["cursor"])
        second = peer_steward._runner_maintenance_pass(self.store)
        self.assertNotIn("reconcile", second)
        # A different duty keeps its own due record.
        self.assertTrue(peer_obligations.read_runner_duty_state(
            self.root, "peer-history"))

    def _retention_starts(self, popen):
        return [call for call in popen.call_args_list
                if call.args and str(call.args[0][1:2]).find("dispatch_registry_archive.py") >= 0]

    def test_maintenance_pass_starts_the_retention_passes_once_per_due_window(self):
        # With nothing dispatched no reap watch ends, so the runner starts the post-exit
        # retention passes (registry archive included) itself, detached, for its own root.
        import dispatch_contract
        with mock.patch.object(peer_steward.subprocess, "Popen") as popen, \
                patch_split(dispatch_contract, "stable_state_root", return_value=self.root):
            popen.return_value.pid = 4242
            first = peer_steward._runner_maintenance_pass(self.store)
            second = peer_steward._runner_maintenance_pass(self.store)
        self.assertEqual(first["retention"], {"started": 4242})
        self.assertNotIn("retention", second)
        starts = self._retention_starts(popen)
        self.assertEqual(len(starts), 1)
        argv, kwargs = starts[0].args[0], starts[0].kwargs
        self.assertTrue(argv[1].endswith("utilities/dispatch_registry_archive.py"))
        self.assertEqual(argv[2:], ["retention", "--jobs", str(self.root / "jobs.log")])
        self.assertTrue(kwargs["start_new_session"])
        popen.return_value.wait.assert_called_with(timeout=10)   # its child exits at once: no zombie

    def test_a_retention_start_that_fails_is_not_retried_before_its_due_window(self):
        import dispatch_contract
        with mock.patch.object(peer_steward.subprocess, "Popen", side_effect=OSError("no fork")) as popen, \
                patch_split(dispatch_contract, "stable_state_root", return_value=self.root):
            peer_steward._runner_maintenance_pass(self.store)
            peer_steward._runner_maintenance_pass(self.store)
        self.assertEqual(len(self._retention_starts(popen)), 1)
        self.assertTrue(peer_obligations.read_runner_duty_state(self.root, "retention")["last_pass_ns"])

    def test_maintenance_pass_never_starts_retention_for_a_fixture_root(self):
        with mock.patch.object(peer_steward.subprocess, "Popen") as popen:
            result = peer_steward._runner_maintenance_pass(self.store)
        self.assertNotIn("retention", result)
        self.assertEqual(self._retention_starts(popen), [])

    def test_incomplete_registry_never_judges_records_and_still_prunes(self):
        records = self._open_records(3)
        result = join.reconcile_pending_delivery(self.jobs, time_budget_s=0)
        self.assertEqual(result["records_read"], 0)
        self.assertEqual(result["budget_exhausted"], 1)
        self.assertIn("pruned_records", result)
        for record in records:
            self.assertEqual(
                pending.read(self.root, "A", record["delivery_id"])["state"], "pending")

    def test_bounded_walk_resumes_from_the_returned_cursor(self):
        self._open_records(5)
        reads, cursor = [], None
        for _ in range(4):
            result = join.reconcile_pending_delivery(
                self.jobs, max_rows=2, time_budget_s=30, start_after=cursor)
            reads.append(result["records_read"])
            cursor = result["cursor"]
            if cursor is None:
                break
        self.assertEqual(sum(reads), 5)
        self.assertIsNone(cursor)
        self.assertEqual(reads, [2, 2, 1])

    def test_maintenance_clears_a_multi_pass_backlog_with_the_cursor(self):
        self._open_records(5)
        totals = []
        with mock.patch.object(peer_steward, "RECONCILE_MAX_ROWS", 2):
            for _ in range(4):
                result = peer_steward._runner_maintenance_pass(self.store)
                if "reconcile" not in result:
                    break
                totals.append(result["reconcile"]["records_read"])
                if result["reconcile"].get("cursor") is None:
                    break
            state = peer_obligations.read_runner_duty_state(self.root, "reconcile")
        self.assertGreaterEqual(sum(totals), 5)
        self.assertIsNone(state["cursor"])
        self.assertGreater(state["last_pass_ns"], 0)

    def _owner_rows(self, count):
        rows = []
        for index in range(count):
            meta = ",".join(f"{key}={value}" for key, value in (
                ("attempt_id", f"att-backlog-{index:04d}"), ("worker_type", "owner"),
                ("dispatch_depth", "1"), ("attempt_schema_version", "2")))
            rows.append(f"2026-11-03T00:00:{index % 60:02d}Z\tdone\t0\t-\tbacklog-{index}\t{meta}")
        self.jobs.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def test_bounded_registry_backstops_resume_instead_of_restarting(self):
        # 2026-11-03: a pass that could not reach the last registry row restarted
        # at row 0, so the runner re-read the same ~150 rows on every pass and
        # never reached the record walk or the duties after it.
        self._owner_rows(10)
        self._open_records(2)
        clock = [0.0]
        seen = []

        def backstop(_jobs, attempt):
            seen.append(attempt)
            clock[0] += 1.0     # each backstop costs one fake second

        import dispatch_owner_input
        with mock.patch.object(join.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(dispatch_owner_input, "materialize_unresolved",
                                  side_effect=backstop):
            passes, cursor, registry_cursor, records = 0, None, None, 0
            while passes < 10:
                passes += 1
                result = join.reconcile_pending_delivery(
                    self.jobs, max_rows=500, time_budget_s=3.5,
                    start_after=cursor, registry_start=registry_cursor)
                cursor, registry_cursor = result["cursor"], result["registry_cursor"]
                records += result["records_read"]
                if not result["budget_exhausted"] and cursor is None and registry_cursor is None:
                    break
        self.assertEqual(seen, [f"att-backlog-{index:04d}" for index in range(10)],
                         "every row's backstop runs once per cycle, in order")
        self.assertEqual(records, 2, "the record walk is reached once the backstops finish")
        self.assertLessEqual(passes, 4)

    def test_backstops_that_end_with_the_budget_continue_at_the_records(self):
        # Review of #1193: backstops that reach the last row just as the budget
        # runs out left both cursors empty, so the next pass restarted at row 0.
        self._owner_rows(10)
        self._open_records(2)
        clock = [0.0]
        seen = []

        def backstop(_jobs, attempt):
            seen.append(attempt)
            clock[0] += 1.0

        import dispatch_owner_input
        with mock.patch.object(join.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(dispatch_owner_input, "materialize_unresolved",
                                  side_effect=backstop):
            first = join.reconcile_pending_delivery(self.jobs, max_rows=500, time_budget_s=10.0)
            self.assertEqual((first["records_read"], first["budget_exhausted"]), (0, 1))
            self.assertEqual(first["registry_cursor"], 10)
            second = join.reconcile_pending_delivery(
                self.jobs, max_rows=500, time_budget_s=10.0, start_after=first["cursor"],
                registry_start=first["registry_cursor"])
        self.assertEqual(len(seen), 10, "no backstop runs twice in one cycle")
        self.assertEqual(second["records_read"], 2)
        self.assertIsNone(second["cursor"])
        self.assertIsNone(second["registry_cursor"])

    def test_a_reconcile_backlog_does_not_starve_the_peer_history_and_index(self):
        def exhausting(_jobs, *, time_budget_s=None, **_kwargs):
            time.sleep(time_budget_s or 0)
            return {"materialized": 0, "expired": 0, "skipped": 0, "pruned_records": 0,
                    "pruned_locks": 0, "prune_skipped": 0, "rows_read": 1,
                    "records_read": 0, "budget_exhausted": 1, "cursor": None,
                    "registry_cursor": 1}

        # 2026-11-03: the reconcile ran first and spent the whole shared budget on
        # every pass, so the prune and the index publisher never ran once.
        with mock.patch.object(peer_steward, "MAINTENANCE_TIME_BUDGET_S", 1.5), \
                mock.patch.object(join, "reconcile_pending_delivery", side_effect=exhausting):
            result = peer_steward._runner_maintenance_pass(self.store)
        self.assertIn("peer_history", result)
        self.assertNotEqual(result["peer_index"].get("deferred"), True, result)
        self.assertTrue(peer_obligations.read_runner_duty_state(self.root, "peer-history"))
        self.assertTrue(peer_obligations.read_runner_duty_state(self.root, "peer-index"))
        self.assertEqual(peer_obligations.read_runner_duty_state(
            self.root, "reconcile")["registry_cursor"], 1)

    def test_a_quiet_pass_reads_no_registry(self):
        now_ns = time.time_ns()
        for duty in ("reconcile", "peer-history", "peer-index", "retire",
                     "inbox-prefill", "steward-relations", "lineage-rows"):
            peer_obligations.write_runner_duty_state(self.root, duty, {
                "last_pass_ns": now_ns, "cursor": None, "edge_generation": 0})
        inbox.write_lineage_rows(self.root, {"directory": "/fixture",
                                             "ledgers": {}})
        with mock.patch.object(join, "reconcile_pending_delivery",
                               side_effect=AssertionError("registry read")):
            result = peer_steward._runner_maintenance_pass(self.store)
        self.assertNotIn("reconcile", result)
        self.assertNotIn("peer_history", result)

    def test_lineage_rows_duty_publishes_a_first_compact_observation(self):
        self.write_ledger("seat-lineage", [
            {"ts": 1, "event": "prompt", "harness": "claude", "sid": "lineage-holder"}])
        first = peer_steward._publish_lineage_rows_duty(self.root)
        self.assertTrue(first["published"])
        payload = inbox.read_lineage_rows(self.root)
        self.assertIsNotNone(payload)
        self.assertIn("seat-lineage", payload["ledgers"])
        second = peer_steward._publish_lineage_rows_duty(self.root)
        self.assertFalse(second["published"])

    def test_peer_index_duty_publishes_then_waits_for_its_due_window(self):
        peer_steward.peer_message.prepare_peer_message(
            "index duty body", {"harness": "codex", "session_id": "sender"},
            {"harness": "claude", "session_id": "A"}, defer=True)
        first = peer_steward._publish_peer_indexes(self.root)
        self.assertTrue(first["published"])
        self.assertGreaterEqual(first["recipients"], 1)
        self.assertTrue(peer_steward.peer_message.recipient_index_path(
            peer_steward.peer_message.peer_state_root(), "claude", "A").is_file())
        second = peer_steward._publish_peer_indexes(self.root)
        self.assertFalse(second["published"])

    def test_steward_relations_publish_and_refresh_on_edge_change(self):
        marker_dir = peer_steward.peer_message.peer_state_root() / "peer-steward" / "claude"
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "legacy.json").write_text(json.dumps({
            "schema_version": 1, "harness": "claude", "session_id": "legacy-holder",
            "targets": {"legacy-holder": {"harness": "claude",
                                          "session_id": "legacy-holder",
                                          "kind": "watch", "ts": "1"}},
        }), encoding="utf-8")
        self.write_ledger("legacy-seat", [
            {"ts": 1, "event": "prompt", "harness": "claude", "sid": "legacy-holder"}])
        first = peer_steward._publish_steward_relations(self.root)
        self.assertTrue(first["published"])
        self.assertIsNotNone(inbox.read_steward_relations(self.root))
        second = peer_steward._publish_steward_relations(self.root)
        self.assertFalse(second["published"])
        inbox.bump_edge_generation(self.root)
        third = peer_steward._publish_steward_relations(self.root)
        self.assertTrue(third["published"])


class PeerHistoryCursorTest(IsolatedCase):
    def _received(self, ref, received_at):
        pending_dir = peer_steward.peer_message._pending_dir()
        pending_dir.mkdir(parents=True, exist_ok=True)
        path = pending_dir / f"{ref}.json"
        path.write_text(json.dumps({
            "ref": ref, "state": "received", "received_at": received_at,
            "to": {"harness": "claude", "session_id": "A"},
            "from": {"harness": "runtime", "session_id": None},
        }), encoding="utf-8")
        return path

    def test_deadline_cursor_resumes_the_walk(self):
        now = time.time()
        first = self._received("a" * 32, now - 8 * 86400)
        self._received("b" * 32, now - 8 * 86400)
        self._received("c" * 32, now - 8 * 86400)
        clock = iter([0, 5])
        with patch_split(peer_steward, "time",
                               SimpleNamespace(time=lambda: now,
                                               monotonic_ns=lambda: next(clock))):
            cut = peer_steward._prune_received_peer_history(now=now, deadline_ns=3)
        self.assertFalse(cut["complete"])
        self.assertEqual(cut["examined"], 1)
        self.assertEqual(cut["cursor"], "a" * 32 + ".json")
        self.assertFalse(first.exists())
        with patch_split(peer_steward, "time",
                               SimpleNamespace(time=lambda: now,
                                               monotonic_ns=lambda: 0)):
            rest = peer_steward._prune_received_peer_history(
                now=now, deadline_ns=10**18, start_after=cut["cursor"])
        self.assertTrue(rest["complete"])
        self.assertIsNone(rest["cursor"])
        self.assertEqual(rest["removed"], 2)


class PeerHistoryCleanupTest(IsolatedCase):
    def _row(self, ref, state, received_at):
        return {"ref": ref, "state": state, "received_at": received_at,
                "to": {"harness": "claude", "session_id": "A"},
                "from": {"harness": "runtime", "session_id": None}}

    def test_only_received_rows_past_seven_days_are_removed(self):
        peer_message = peer_steward.peer_message
        pending_dir = peer_message._pending_dir()
        pending_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        old_received = pending_dir / ("a" * 32 + ".json")
        new_received = pending_dir / ("b" * 32 + ".json")
        old_pending = pending_dir / ("c" * 32 + ".json")
        old_received.write_text(json.dumps(self._row("a" * 32, "received", now - 8 * 86400)))
        new_received.write_text(json.dumps(self._row("b" * 32, "received", now - 86400)))
        old_pending.write_text(json.dumps(self._row("c" * 32, "pending", now - 30 * 86400)))
        transfers = peer_message.peer_state_root() / "peer-messages" / "transfers"
        transfers.mkdir(parents=True, exist_ok=True)
        transfer = transfers / ("a" * 32 + ".json")
        transfer.write_text(json.dumps({"ref": "a" * 32, "state": "received"}))
        result = peer_steward._prune_received_peer_history(now=now)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(old_received.exists())
        self.assertTrue(new_received.exists())
        self.assertTrue(old_pending.exists())
        self.assertTrue(transfer.exists())

    def test_queued_and_native_pending_rows_are_not_closed_by_the_seven_day_window(self):
        # #1576 review follow-up F9: the 7-day window closes only received rows. A row
        # accepted by a native carrier but never received (`claude-pty-continue-submitted`,
        # `queued`) or one still waiting for its native callback (`native-pending`) stays:
        # dropping it would drop the sender's still-unreceived sealed request, which is not a
        # one-condition extension of this sweep. Recorded, not changed.
        pending_dir = peer_steward.peer_message._pending_dir()
        pending_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        kept = {}
        for index, (state, receipt) in enumerate((("queued", "claude-pty-continue-submitted"),
                                                   ("pending", "native-pending"),
                                                   ("unverified", "hook-context-inflight"))):
            ref = f"{index + 1:032x}"
            row = self._row(ref, state, now - 30 * 86400)
            row["receipt"] = receipt
            path = pending_dir / (ref + ".json")
            path.write_text(json.dumps(row))
            kept[ref] = path
        result = peer_steward._prune_received_peer_history(now=now)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(all(path.exists() for path in kept.values()))

    def _lock(self, ref, mtime):
        path = peer_steward.peer_message._pending_dir() / (".lock-" + ref)
        path.touch(mode=0o600)
        os.utime(path, (mtime, mtime))
        return path

    def test_rowless_ref_locks_are_removed_and_held_or_rowed_locks_stay(self):
        import fcntl
        pending_dir = peer_steward.peer_message._pending_dir()
        now = time.time()
        old = now - 8 * 86400
        for index in range(5):
            self._lock(f"{index:032x}", old)
        rowed = self._lock("c" * 32, old)
        (pending_dir / ("c" * 32 + ".json")).write_text(
            json.dumps(self._row("c" * 32, "pending", old)))
        held = self._lock("d" * 32, old)
        young = self._lock("e" * 32, now - 86400)
        legacy_prepare = self._lock("prepare-" + "f" * 64, old)
        # A received row past the window goes first; its lock goes in the same pass.
        expired = self._lock("a" * 32, old)
        (pending_dir / ("a" * 32 + ".json")).write_text(
            json.dumps(self._row("a" * 32, "received", old)))
        holder = os.open(held, os.O_RDWR)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = peer_steward._prune_received_peer_history(now=now)
        finally:
            os.close(holder)
        self.assertEqual(sorted(path.name for path in pending_dir.glob(".lock-*")),
                         sorted(path.name for path in (rowed, held, young, legacy_prepare)))
        self.assertFalse(expired.exists())
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result.get("locks_removed"), 6)
        self.assertTrue(result["complete"])

    def test_lock_backlog_clears_across_budgeted_passes(self):
        import itertools
        pending_dir = peer_steward.peer_message._pending_dir()
        now = time.time()
        for index in range(7):
            self._lock(f"{index:032x}", now - 8 * 86400)
        ticks = itertools.count()
        clock = SimpleNamespace(time=lambda: now, monotonic_ns=lambda: next(ticks))
        cursor, passes, removed = None, 0, 0
        with patch_split(peer_steward, "time", clock):
            while passes < 20:
                passes += 1
                counts = peer_steward._prune_received_peer_history(
                    now=now, deadline_ns=next(ticks) + 3, start_after=cursor)
                removed += counts.get("locks_removed", 0)
                if counts["complete"]:
                    break
                cursor = counts["cursor"]
        self.assertEqual(removed, 7)
        self.assertGreater(passes, 3)
        self.assertEqual(list(pending_dir.glob(".lock-*")), [])


class LineageBundleRetirementTest(IsolatedCase):
    OLD = 1_000_000.0

    def component(self, *, ts=None):
        ts = self.OLD if ts is None else ts
        self.write_ledger("seat-A", [
            {"ts": ts, "harness": "claude", "sid": "A", "event": "start", "source": "startup"}])
        self.write_ledger("seat-B", [
            {"ts": ts, "harness": "claude", "sid": "B", "event": "handover",
             "source": "clear", "from": {"harness": "claude", "sid": "A"}}])

    def retire(self, **kwargs):
        lifetime = kwargs.pop("lifetime", "ended")
        with mock.patch.object(peer_steward, "native_lifetime", return_value=lifetime) as proof:
            result = peer_steward._retire_finished_lineage_bundles(
                self.store, now=time.time(), **kwargs)
        self.lifetime_proof = proof
        return result

    def archived(self):
        archive = tidy.state_root() / peer_steward.RETIRE_ARCHIVE_DIRECTORY
        return sorted(path.name for path in archive.glob("*.jsonl")) if archive.is_dir() else []

    def sessions(self):
        sessions = tidy.state_root() / "sessions"
        return sorted(path.name for path in sessions.glob("*.jsonl")) if sessions.is_dir() else []

    def test_all_five_conditions_archive_both_ledgers_without_deleting(self):
        self.component()
        result = self.retire()
        self.assertEqual(result["retired"], 1)
        self.assertEqual(result["ledgers"], 2)
        self.assertEqual(self.sessions(), [])
        self.assertEqual(self.archived(), ["seat-A.jsonl", "seat-B.jsonl"])
        # Archive retention: 29 days kept, 31 days removed.
        archive = tidy.state_root() / peer_steward.RETIRE_ARCHIVE_DIRECTORY
        old = time.time() - 29 * 86400
        os.utime(archive / "seat-A.jsonl", (old, old))
        self.assertEqual(peer_steward._purge_retired_archives()["removed"], 0)
        older = time.time() - 31 * 86400
        os.utime(archive / "seat-B.jsonl", (older, older))
        self.assertEqual(peer_steward._purge_retired_archives()["removed"], 1)
        self.assertEqual(self.archived(), ["seat-A.jsonl"])

    def test_retirement_uses_native_lifetime_once_per_exact_session(self):
        self.component()
        self.write_ledger("seat-C", [
            {"ts": self.OLD, "harness": "claude", "sid": "C", "event": "start", "source": "startup"}])
        self.write_ledger("seat-D", [
            {"ts": self.OLD, "harness": "claude", "sid": "D", "event": "handover",
             "source": "clear", "from": {"harness": "claude", "sid": "C"}}])
        result = self.retire()
        calls = self.lifetime_proof.call_args_list
        self.assertCountEqual([call.args for call in calls],
                              [("claude", "A"), ("claude", "B"), ("claude", "C"), ("claude", "D")])
        # One shared session-holder reading serves the whole pass.
        observations = {id(call.kwargs.get("observation")) for call in calls}
        self.assertEqual(len(observations), 1)
        self.assertIsNotNone(calls[0].kwargs.get("observation"))
        self.assertEqual(result["retired"], 2)


    def test_retirement_stamps_the_archive_and_never_overwrites_an_entry(self):
        self.component()
        sessions = tidy.state_root() / "sessions"
        old = time.time() - 20 * 86400
        for path in sessions.glob("*.jsonl"):
            os.utime(path, (old, old))
        archive = tidy.state_root() / peer_steward.RETIRE_ARCHIVE_DIRECTORY
        archive.mkdir(parents=True, exist_ok=True)
        sentinel = archive / "seat-A.jsonl"
        sentinel.write_text("previous retirement\n", encoding="utf-8")
        started = time.time()
        result = self.retire()
        self.assertEqual(result["retired"], 1)
        self.assertEqual(result["ledgers"], 2)
        # The earlier archived ledger is never replaced; the second retirement
        # of a re-used key lands under a unique name.
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "previous retirement\n")
        names = self.archived()
        self.assertEqual(len(names), 3)
        unique = [name for name in names if name.startswith("seat-A.retired-")]
        self.assertEqual(len(unique), 1)
        moved = archive / unique[0]
        self.assertGreaterEqual(moved.stat().st_mtime, started - 1)
        # Retention counts from retirement, not from the ledger's last write.
        self.assertEqual(peer_steward._purge_retired_archives(
            now=started + 29 * 86400)["removed"], 0)
        self.assertEqual(peer_steward._purge_retired_archives(
            now=started + 31 * 86400)["removed"], 3)

    def test_each_blocking_condition_keeps_the_bundle(self):
        cases = {}

        def open_notice():
            self.component()
            _notice("claude", "B", key="open-1", subject="n", text="t", jobs=self.jobs)

        def recent_activity():
            self.component(ts=time.time())

        def open_binding():
            self.component()
            binding = {"jobs": str(self.jobs), "route": "rt", "hash": "sha256:" + "b" * 64,
                       "node": "execute", "attempt": "att-open", "parent": "A"}
            self.write_ledger("seat-B", [
                {"ts": self.OLD, "harness": "claude", "sid": "B", "event": "handover",
                 "source": "clear", "from": {"harness": "claude", "sid": "A"},
                 "bindings": [binding]}])
            with self.jobs.open("a") as handle:
                handle.write("now\trunning\t/fixture/repo\t/fixture/wt\tframe\t"
                             "attempt_id=att-open,parent_sid=A\n")

        def role_row():
            self.component()
            self.write_ledger("seat-B", [
                {"ts": self.OLD, "harness": "claude", "sid": "B", "event": "tidy-claim",
                 "claim_id": "c1", "nonce": "n1"}])

        def unresolved_component():
            self.component()
            self.write_ledger("seat-C", [
                {"ts": self.OLD, "harness": "claude", "sid": "C", "event": "handover",
                 "source": "clear", "from": {"harness": "claude", "sid": "A"}}])

        cases = {"open-notice": open_notice, "recent-activity": recent_activity,
                 "open-binding": open_binding, "role-row": role_row,
                 "unresolved": unresolved_component}
        for name, build in cases.items():
            with self.subTest(name=name):
                for path in (tidy.state_root() / "sessions").glob("*.jsonl"):
                    path.unlink()
                archive = tidy.state_root() / peer_steward.RETIRE_ARCHIVE_DIRECTORY
                if archive.is_dir():
                    for path in archive.glob("*.jsonl"):
                        path.unlink()
                for path in (self.root / "pending-delivery").rglob("*.json"):
                    path.unlink()
                self.jobs.write_text("")
                build()
                result = self.retire()
                self.assertEqual(result["retired"], 0, name)
                self.assertTrue(self.sessions(), name)
        self.component()
        result = self.retire(lifetime="unknown")
        self.assertEqual(result["retired"], 0)


class InboxPrefillTest(IsolatedCase):
    def test_prefill_publishes_a_current_inbox_and_skips_incomplete_scopes(self):
        notice = _notice("claude", "A", key="open-1", subject="n", text="t",
                         jobs=self.jobs)
        self.edge("A", "B")
        deadline = time.monotonic_ns() + 30 * 1_000_000_000
        with patch_split(peer_steward, "_live_recipients",
                               return_value={("claude", "B")}):
            result = peer_steward._prefill_live_inboxes(self.root, deadline_ns=deadline)
        self.assertEqual(result["written"], 1)
        payload = inbox.read_inbox(self.root, "claude", "B")
        self.assertIsNotNone(payload)
        self.assertIn({"storage_key": "A", "delivery_id": notice["delivery_id"]},
                      [{"storage_key": item["storage_key"],
                        "delivery_id": item["delivery_id"]}
                       for item in payload["delivery_ids"]])
        self.assertTrue(inbox.is_current(payload, self.root))
        # A new record in a named scope makes the inbox stale; prefill repairs it.
        _notice("claude", "A", key="open-2", subject="n2", text="t2", jobs=self.jobs)
        self.assertFalse(inbox.is_current(payload, self.root))
        with patch_split(peer_steward, "_live_recipients",
                               return_value={("claude", "B")}):
            peer_steward._prefill_live_inboxes(self.root, deadline_ns=deadline)
        self.assertTrue(inbox.is_current(inbox.read_inbox(self.root, "claude", "B"),
                                         self.root))

    def test_incomplete_scopes_are_never_published(self):
        import dispatch_seat_handover as handover
        _notice("claude", "A", key="open-1", subject="n", text="t", jobs=self.jobs)
        self.edge("A", "B")
        incomplete = handover.RecipientScopes()
        incomplete.complete = False
        with mock.patch.object(handover, "storage_recipients", return_value=incomplete):
            payload = peer_steward._prefill_recipient_inbox(
                self.root, "claude", "B",
                deadline_ns=time.monotonic_ns() + 30 * 1_000_000_000)
        self.assertIsNone(payload)
        self.assertIsNone(inbox.read_inbox(self.root, "claude", "B"))

    def test_prefill_names_only_open_records_never_terminal_ones(self):
        open_record = _notice("claude", "A", key="open-1", subject="n", text="t",
                              jobs=self.jobs)
        terminal = _notice("claude", "A", key="terminal-1", subject="done", text="done",
                           jobs=self.jobs)
        pending.claim_ready_batch(self.root, "A", terminal["delivery_id"],
                                  claim_owner="fixture", lease_seconds=30,
                                  require_generation_proof=False, jobs=self.jobs)
        pending.ack(self.root, "A", terminal["delivery_id"], acked_by="fixture")
        self.edge("A", "B")
        payload = peer_steward._prefill_recipient_inbox(
            self.root, "claude", "B",
            deadline_ns=time.monotonic_ns() + 30 * 1_000_000_000)
        self.assertIsNotNone(payload)
        named = [item["delivery_id"] for item in payload["delivery_ids"]]
        self.assertEqual(named, [open_record["delivery_id"]])
        self.assertNotIn(terminal["delivery_id"], named)
        self.assertTrue(inbox.is_current(inbox.read_inbox(self.root, "claude", "B"),
                                         self.root))
        # The hook then sees exactly the open delivery and nothing owed.
        import dispatch_session_sweep as sweep
        addressed, _count = sweep.addressed_records(self.root, "claude-parent-runtime", "B")
        self.assertEqual([record["delivery_id"] for record in addressed], named)

    def test_fresh_install_at_generation_zero_still_bootstraps_an_opencode_inbox(self):
        notice = _notice("opencode", "B", key="oc-open-1", subject="n", text="t",
                         jobs=self.jobs)
        self.assertEqual(inbox.read_edge_generation(self.root), 0)
        with patch_split(peer_steward, "_live_recipients",
                               return_value={("opencode", "B")}):
            result = peer_steward._runner_maintenance_pass(self.store)
        self.assertIn("inbox", result)
        self.assertEqual(result["inbox"]["written"], 1)
        payload = inbox.read_inbox(self.root, "opencode", "B")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["edge_generation"], 0)
        self.assertIn(notice["delivery_id"],
                      [item["delivery_id"] for item in payload["delivery_ids"]])
        self.assertTrue(inbox.is_current(payload, self.root))

    def test_a_missed_edge_bump_is_repaired_by_the_runner_digest_check(self):
        _notice("claude", "A", key="open-1", subject="n", text="t", jobs=self.jobs)
        self.edge("A", "B")
        deadline = time.monotonic_ns() + 30 * 1_000_000_000
        with patch_split(peer_steward, "_live_recipients",
                               return_value={("claude", "B")}):
            first = peer_steward._prefill_live_inboxes(self.root, deadline_ns=deadline)
        self.assertEqual(first["written"], 1)
        recorded = inbox.read_inbox(self.root, "claude", "B")
        self.assertTrue(inbox.is_current(recorded, self.root))
        # A confirmed edge writer whose best-effort bump failed changes the
        # edge set without moving the generation.
        self.edge("X", "Y")
        self.assertEqual(inbox.read_edge_generation(self.root), 0)
        # The hook's current path is stat-only, so it still trusts what it has.
        self.assertTrue(inbox.is_current(recorded, self.root))
        with patch_split(peer_steward, "_live_recipients",
                               return_value={("claude", "B")}):
            second = peer_steward._prefill_live_inboxes(self.root, deadline_ns=deadline)
        self.assertEqual(second["written"], 1)
        repaired = inbox.read_inbox(self.root, "claude", "B")
        self.assertNotEqual(repaired["folded_edge_digest"],
                            recorded["folded_edge_digest"])
        self.assertTrue(inbox.is_current(repaired, self.root,
                                         folded_edge_digest=repaired["folded_edge_digest"]))

    def test_runner_prefill_refuses_old_scopes_after_a_completed_edge_writer(self):
        import dispatch_seat_handover as handover
        notice = _notice("claude", "A", key="open-1", subject="n", text="t",
                         jobs=self.jobs)
        real = handover.storage_recipients

        def scopes_then_writer(*args, **kwargs):
            result = real(*args, **kwargs)
            # The confirmed edge writer completes after the scopes were
            # enumerated and before the inbox is published.
            self.edge("A", "B")
            inbox.bump_edge_generation(self.root)
            return result

        deadline = time.monotonic_ns() + 30 * 1_000_000_000
        with mock.patch.object(handover, "storage_recipients",
                               side_effect=scopes_then_writer):
            with self.assertRaises(OSError):
                peer_steward._prefill_recipient_inbox(
                    self.root, "claude", "B", deadline_ns=deadline)
        self.assertIsNone(inbox.read_inbox(self.root, "claude", "B"))
        payload = peer_steward._prefill_recipient_inbox(
            self.root, "claude", "B", deadline_ns=deadline)
        self.assertIn(notice["delivery_id"],
                      [item["delivery_id"] for item in payload["delivery_ids"]])
        self.assertTrue(inbox.is_current(inbox.read_inbox(self.root, "claude", "B"),
                                         self.root))

    def test_prefill_publishes_nothing_when_the_graph_read_spent_the_deadline(self):
        import dispatch_seat_handover as handover
        _notice("claude", "A", key="open-1", subject="n", text="t", jobs=self.jobs)
        self.edge("A", "B")
        real = handover.lineage_graph

        def slow_graph(*args, **kwargs):
            graph = real(*args, **kwargs)
            time.sleep(0.08)  # the full-graph read is not interruptible
            return graph

        started = time.monotonic()
        with mock.patch.object(handover, "lineage_graph", side_effect=slow_graph), \
                patch_split(peer_steward, "_live_recipients",
                                  return_value={("claude", "B"), ("claude", "C")}), \
                patch_split(peer_steward, "_prefill_recipient_inbox",
                                  side_effect=AssertionError("prefill after deadline")):
            result = peer_steward._prefill_live_inboxes(
                self.root, deadline_ns=time.monotonic_ns() + 40_000_000)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(result, {"checked": 0, "written": 0, "skipped": 2})
        self.assertIsNone(inbox.read_inbox(self.root, "claude", "B"))


class RunStatesPublishTest(IsolatedCase):
    def test_publish_merges_public_status_with_registry_exit_and_jobs(self):
        import resource_run_registry as registry
        artifact_root = self.root / "artifacts"
        artifact_root.mkdir()
        public = artifact_root / "resource-runs" / "run-1.json"
        public.parent.mkdir(parents=True)
        public.write_text(json.dumps({"schema_version": 1, "run_id": "run-1",
                                      "status": "succeeded", "ended_at": 42.0}))
        registry_path = self.root / "runs.json"
        registry_path.write_text(json.dumps({"schema_version": 1, "runs": {"run-1": {
            "run_id": "run-1", "status": "running", "exit_code": 0, "ended_at": 41.0,
            "artifact_root": str(artifact_root), "parent_attempt_id": "att-run-parent",
            "jobs": str(self.jobs)}}}))
        index_path = self.root / "resource-runs.index.json"
        index_path.write_text(json.dumps({"schema_version": 1, "registries": {
            "fixture": {"path": str(registry_path)}}}))
        os.environ["AGENT_RESOURCE_RUN_INDEX"] = str(index_path)
        _notice("claude", "A", key="experiment:run-1:final:1:claude:A",
                subject="experiment final (1/1)", text="experiment finished",
                jobs=self.jobs)
        result = peer_steward._publish_run_states(self.store, self.duties())
        self.assertTrue(result["published"])
        self.assertEqual(result["runs"], 1)
        payload = inbox.read_run_states(inbox.dispatch_inbox_root())
        row = payload["runs"]["run-1"]
        self.assertEqual(row["status"], "succeeded")   # public record wins
        self.assertEqual(row["exit_code"], 0)          # registry row supplies exit
        self.assertEqual(row["parent_attempt_id"], "att-run-parent")
        self.assertEqual(row["jobs"], str(self.jobs))  # the run's own recorded jobs
        self.assertEqual(row["ended_at"], 42.0)
        # A second pass inside the due window does not rescan.
        again = peer_steward._publish_run_states(self.store, self.duties())
        self.assertFalse(again["published"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
