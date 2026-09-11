#!/usr/bin/env python3
"""M1–M4 behavioral checks against the same archived or working source.

Only time/wait and transport are controlled; authority checks remain real.
HEARTING_TEST_SOURCE_ROOT selects a read-only git archive for RED runs.
"""
import contextlib
import errno
import importlib.util
import io
import json
import os
import socket
import stat
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

ROOT = Path(os.environ.get("HEARTING_TEST_SOURCE_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / "utilities"))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RF = load("recovery_receipt_fixture", "human_gate_receipt.test.py")
GF = load("recovery_gateway", "codex-managed-gateway.py")
CF = load("recovery_completion", "codex-managed-completion.py")
SF = load("recovery_supervisor_fixture", "workflow_supervisor.test.py")
PD = RF.pending


class M1Decision(unittest.TestCase):
    def setUp(self):
        self.fixture = SF.TestGateRelease()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        with contextlib.redirect_stdout(io.StringIO()):
            self.source, self.route_file = self.fixture._blocked()
        self.source["launch_compatibility_tuple"] = {"jobs_path": {"path": str(self.fixture.jobs)}}
        self.gate = "frame-review"
        self.ledger = SF.SUP.ledger_for(self.source, self.fixture.jobs)

    def release(self, decision):
        with contextlib.redirect_stdout(io.StringIO()):
            result = SF.SUP.main(["release", "--route", str(self.route_file), "--gate", self.gate,
                                  "--decision", decision, "--actor", "fixture-user",
                                  "--jobs", str(self.fixture.jobs)])
        self.assertEqual(result, 0)

    def suffix(self, proof):
        return dict(source_route_id=self.source["route_id"], source_route_hash=self.source["route_hash"],
                    human_gate_bindings=self.source["human_gate_bindings"], reused_human_gate_releases=[proof])

    def builder_refuses(self):
        with self.assertRaisesRegex(ValueError, "continuation-human-gate-release-unproven"):
            SF.ROUTE._continuation_gate_release_proof(self.source, self.gate)

    def test_actual_stop_without_decision_is_not_proceed(self):
        self.release("stop")
        last = self.ledger.journal()[-1]
        self.assertEqual(last["workflow_state"], "CANCELLED")
        self.assertNotIn("decision", last["evidence"])
        self.assertEqual(SF.WS.human_gate_resolution(self.ledger.journal(), self.gate)["status"], "stop")
        self.builder_refuses()

    def test_actual_revise_is_not_proceed(self):
        self.release("revise")
        self.builder_refuses()

    def test_verifier_rejects_exact_stop_bytes_even_if_proof_says_proceed(self):
        self.release("stop")
        entries = self.ledger.journal()
        raised = next(e for e in entries if e["workflow_state"] == "BLOCKED_HUMAN_GATE")
        proof = dict(gate=self.gate, source_route_id=self.source["route_id"],
                     source_route_hash=self.source["route_hash"], epoch=1, decision="proceed",
                     jobs_path=str(self.fixture.jobs), journal_path=str(self.ledger.journal_path),
                     raise_entry_digest=SF.ROUTE._sha256_record(raised),
                     release_entry_digest=SF.ROUTE._sha256_record(entries[-1]))
        with self.assertRaisesRegex(ValueError, "continuation-human-gate"):
            SF.ROUTE._verify_continuation_gate_release_proofs(self.suffix(proof))

    def prior_proof(self):
        self.release("proceed")
        proof = SF.ROUTE._continuation_gate_release_proof(self.source, self.gate)
        SF.ROUTE._verify_continuation_gate_release_proofs(self.suffix(proof))
        with self.ledger.lock():
            self.ledger.set_workflow_state("BLOCKED_HUMAN_GATE", evidence={"gate": self.gate})
        return proof

    def assert_prior_invalid(self, proof):
        self.builder_refuses()
        with self.assertRaisesRegex(ValueError, "continuation-human-gate"):
            SF.ROUTE._verify_continuation_gate_release_proofs(self.suffix(proof))

    def test_latest_reraise_invalidates_prior_proof(self):
        self.assert_prior_invalid(self.prior_proof())

    def test_latest_stop_invalidates_prior_proof(self):
        proof = self.prior_proof()
        self.release("stop")
        self.assert_prior_invalid(proof)

    def test_latest_revise_invalidates_prior_proof(self):
        proof = self.prior_proof()
        self.release("revise")
        self.assert_prior_invalid(proof)

    def test_legacy_proceed_without_decision_remains_valid(self):
        with self.ledger.lock():
            self.ledger.set_workflow_state("RUNNING", evidence={"released_gate": self.gate,
                                          "released_by": "fixture-user", "actor_kind": "user"})
        proof = SF.ROUTE._continuation_gate_release_proof(self.source, self.gate)
        self.assertEqual(proof["decision"], "proceed")
        SF.ROUTE._verify_continuation_gate_release_proofs(self.suffix(proof))


class Sink:
    def __init__(self):
        self.sent = []

    def write_json(self, value):
        self.sent.append(value)

    def close(self):
        pass


class GatewayFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.f = RF.Fixture(self.root, RF.load_module())
        self.g = GF.ManagedGateway(listen_path=self.root / "front", upstream_path=self.root / "up",
                                   control_path=self.root / "ctrl", ledger_path=self.root / "gateway.json")
        self.g._tui = Sink()
        self.sink = self.g._upstream = Sink()
        self.g._epoch = self.f.gateway_epoch
        self.g._binding_thread_id = self.f.thread_id
        self.state = GF.ThreadState(active_turn_id="busy", steer_ready=False)
        self.g._threads[self.f.thread_id] = self.state
        self.request = dict(schema_version=1, op="deliver-human-gate", thread_id=self.f.thread_id,
                            parent_attempt_id=self.f.owner_attempt, sealed_batch_id=self.f.sealed_batch,
                            receipt=self.f.receipt, receipt_digest=self.f.module.receipt_digest(self.f.receipt),
                            delivery_id=self.f.module.gateway_delivery_id(self.f.receipt))
        self.did = self.request["delivery_id"]
        self.g._human_gate_validation(self.request)  # Fail setup if real authority checks reject.

    def request_now(self):
        with mock.patch.object(threading.Event, "wait", return_value=False):
            return self.g.deliver_human_gate(self.request)

    def response(self, message):
        with self.g._lock:
            pending = self.g._internal.pop(GF.request_key(self.sink.sent[-1]["id"]))
            self.g._handle_internal_response_locked(pending, message)


class M2PreparedIdentity(GatewayFixture):
    def test_two_requests_timeout_retry_queue_drain_accepted_replay(self):
        self.state.active_turn_id = ""
        self.state.pending_start_id = ("manual", 1)
        self.assertEqual(self.request_now()["status"], "sent-ambiguous")
        pending = self.g._delivery_pending[self.did]
        self.assertEqual(self.request_now()["status"], "sent-ambiguous")
        self.assertIs(self.g._delivery_pending[self.did], pending)
        self.assertEqual(self.state.queued, [pending])
        self.assertEqual(len(self.sink.sent), 0)
        self.state.pending_start_id = None
        self.state.active_turn_id = "busy"
        self.state.steer_ready = True
        with self.g._lock:
            self.g._drain_queued_locked(self.state)
            self.g._drain_queued_locked(self.state)
        self.assertEqual(len(self.sink.sent), 1)
        self.assertEqual(self.state.queued, [])
        self.response({"result": {"turnId": "busy"}})
        self.assertTrue(pending.event.is_set())
        self.assertEqual(pending.outcome["status"], "accepted")
        self.assertNotIn(self.did, self.g._delivery_pending)
        replay = self.g.deliver_human_gate(self.request)
        self.assertEqual((replay["status"], replay["replay"]), ("accepted", True))
        self.assertEqual(len(self.sink.sent), 1)

    def test_duplicate_prepared_request_has_one_idle_fallback(self):
        self.request_now()
        self.request_now()
        self.state.steer_ready = True
        with self.g._lock:
            self.g._drain_queued_locked(self.state)
        self.assertEqual(len(self.sink.sent), 1)
        self.response({"error": {"code": -1, "message": "turn no longer steerable"}})
        pending = self.g._delivery_pending[self.did]
        self.assertEqual(self.state.idle_completions, [pending])
        self.request_now()
        self.assertEqual(len(self.sink.sent), 1)
        self.state.active_turn_id = ""
        self.state.steer_ready = False
        with self.g._lock:
            self.g._start_next_idle_completion_locked(self.state)
            self.g._start_next_idle_completion_locked(self.state)
        self.assertEqual([m["method"] for m in self.sink.sent], ["turn/steer", "turn/start"])
        self.assertEqual(self.state.idle_completions, [])
        self.response({"result": {"turn": {"id": "new-turn"}}})
        self.assertEqual(self.g.deliver_human_gate(self.request)["status"], "accepted")
        self.assertEqual(len(self.sink.sent), 2)

    def test_sent_ambiguous_gateway_restart_never_resends(self):
        self.state.steer_ready = True
        self.request_now()
        self.g._delivery_pending.clear()  # Restart reconstructs durable state without old waiters.
        self.g.ledger = GF.DeliveryLedger(self.root / "gateway.json")
        self.assertEqual(self.g.deliver_human_gate(self.request)["reason"], "accept-not-observed-no-resend")
        self.assertEqual(len(self.sink.sent), 1)


class M3RecipientRace(GatewayFixture):
    def validation_race(self, dimension):
        validate = self.g._human_gate_validation

        def raced(request):
            result = validate(request)  # Validate first, inject only the race.
            if dimension == "epoch":
                self.g._epoch += 1
            else:
                self.g._binding_thread_id = "successor-thread"
            return result

        with mock.patch.object(self.g, "_human_gate_validation", side_effect=raced):
            result = self.request_now()
        self.assertEqual((result["status"], result["reason"]), ("rejected", "recipient-epoch-mismatch"))
        self.assertEqual(self.g._delivery_pending, {})
        self.assertEqual(self.state.queued, [])
        self.assertIsNone(self.g.ledger.get(self.did))
        self.assertEqual(len(self.sink.sent), 0)

    def test_validation_to_mutation_epoch_race(self):
        self.validation_race("epoch")

    def test_validation_to_mutation_thread_race(self):
        self.validation_race("thread")

    def queued_race(self, dimension):
        self.request_now()
        pending = self.g._delivery_pending[self.did]
        self.assertEqual(self.g.ledger.get(self.did)["state"], "prepared")
        if dimension == "epoch":
            self.g._epoch += 1
        else:
            self.g._binding_thread_id = "successor-thread"
        self.state.steer_ready = True
        with self.g._lock:
            self.g._drain_queued_locked(self.state)
        self.assertTrue(pending.event.is_set())
        self.assertEqual((pending.outcome["status"], pending.outcome["reason"]), ("rejected", "recipient-epoch-mismatch"))
        self.assertEqual(self.state.queued, [])
        self.assertEqual(self.state.idle_completions, [])
        self.assertEqual(self.g._delivery_pending, {})
        self.assertEqual(self.g._internal, {})
        durable = GF.DeliveryLedger(self.root / "gateway.json").get(self.did)
        self.assertEqual((durable["state"], durable["reason"]), ("rejected", "recipient-epoch-mismatch"))
        self.assertEqual(len(self.sink.sent), 0)

    def test_queued_epoch_race_closes_prepared(self):
        self.queued_race("epoch")

    def test_queued_thread_race_closes_prepared(self):
        self.queued_race("thread")


class M3RejectionCommitFailure(GatewayFixture):
    def commit_failure(self, dimension, fault):
        self.request_now()  # Real validation and durable prepared state.
        pending = self.g._delivery_pending[self.did]
        before = self.g.ledger.get(self.did)
        disk_before = (self.root / "gateway.json").read_bytes()
        entered = threading.Event()
        wait = pending.event.wait
        responses = []

        def bounded_wait(_timeout):
            entered.set()
            return wait(2)

        # A real second caller joins the same prepared request before the race.
        with mock.patch.object(pending.event, "wait", side_effect=bounded_wait):
            caller = threading.Thread(target=lambda: responses.append(self.g.deliver_human_gate(self.request)))
            caller.start()
            try:
                self.assertTrue(entered.wait(3))
                if dimension == "epoch":
                    self.g._epoch += 1
                else:
                    self.g._binding_thread_id = "successor-thread"
                self.state.steer_ready = True
                caught = None
                with contextlib.ExitStack() as stack:
                    if fault == "permission":
                        os.chmod(self.root, 0o500)
                        stack.callback(os.chmod, self.root, 0o700)
                        # Refuse a false RED on a privileged filesystem/user.
                        with self.assertRaises(PermissionError):
                            fd, probe = tempfile.mkstemp(dir=self.root)
                            os.close(fd)
                            Path(probe).unlink()
                    elif fault == "oversized":
                        # Exercise the real _write size guard, not a mocked transition.
                        stack.enter_context(mock.patch.object(GF, "MAX_LEDGER_BYTES", 1))
                    else:
                        fsync = os.fsync

                        def fail_directory_sync(fd):
                            if stat.S_ISDIR(os.fstat(fd).st_mode):
                                raise OSError(errno.EIO, "directory-sync-failed")
                            return fsync(fd)

                        # Real write/replace, followed by a failed durability barrier.
                        stack.enter_context(mock.patch.object(GF.os, "fsync", side_effect=fail_directory_sync))
                    try:
                        with self.g._lock:
                            self.g._drain_queued_locked(self.state)
                    except (OSError, GF.GatewayError) as exc:
                        caught = exc
                    signalled = pending.event.is_set()
            finally:
                caller.join(3)
        self.assertFalse(caller.is_alive())
        self.assertIsNone(caught, repr(caught))
        self.assertTrue(signalled)
        self.assertEqual(len(responses), 1)
        result = responses[0]
        self.assertEqual((result["status"], result["reason"]),
                         ("rejected", "rejection-ledger-commit-failed"))
        self.assertEqual(result["rejection_reason"], "recipient-epoch-mismatch")
        self.assertEqual(result["ledger_commit"], "unconfirmed")
        self.assertIn({"permission": "PermissionError", "oversized": "ledger-oversized",
                       "directory-sync": "directory-sync-failed"}[fault], result["ledger_error"])
        self.assertEqual(pending.outcome, result)
        self.assertEqual(self.state.queued, [])
        self.assertEqual(self.state.idle_completions, [])
        self.assertEqual(self.g._delivery_pending, {})
        self.assertEqual(self.g._internal, {})
        memory = self.g.ledger.get(self.did)
        audit = memory.pop("rejection_commit_failure")
        self.assertEqual(memory, before)  # No in-memory successful rejection.
        self.assertEqual(audit, result)
        disk = GF.DeliveryLedger(self.root / "gateway.json").get(self.did)
        if fault == "directory-sync":
            # A failed commit can already be visible. Never claim disk rollback.
            self.assertEqual(disk["state"], "rejected")
        else:
            self.assertEqual((self.root / "gateway.json").read_bytes(), disk_before)
            self.assertEqual(disk, before)
        # Disconnect cannot overwrite the already delivered commit failure.
        self.g._disconnect_epoch(self.g._epoch, None, None)
        self.assertTrue(pending.event.is_set())
        self.assertEqual(pending.outcome, result)
        self.assertEqual(len(self.sink.sent), 0)

    def test_queued_epoch_permission_failure_notifies_requester(self):
        self.commit_failure("epoch", "permission")

    def test_queued_thread_permission_failure_notifies_requester(self):
        self.commit_failure("thread", "permission")

    def test_queued_epoch_typed_write_failure_notifies_requester(self):
        self.commit_failure("epoch", "oversized")

    def test_queued_thread_typed_write_failure_notifies_requester(self):
        self.commit_failure("thread", "oversized")

    def test_post_replace_sync_failure_is_unconfirmed_not_rollback(self):
        self.commit_failure("epoch", "directory-sync")

    def idle_race(self, dimension):
        self.state.steer_ready = True
        self.request_now()
        self.response({"error": {"code": -1, "message": "not steerable"}})
        pending = self.g._delivery_pending[self.did]
        disk_before = (self.root / "gateway.json").read_bytes()
        self.state.active_turn_id = ""
        self.state.steer_ready = False
        if dimension == "epoch":
            self.g._epoch += 1
        else:
            self.g._binding_thread_id = "successor-thread"
        with self.g._lock:
            self.g._start_next_idle_completion_locked(self.state)
        self.assertTrue(pending.event.is_set())
        self.assertEqual(pending.outcome["reason"], "recipient-epoch-mismatch")
        self.assertEqual((self.root / "gateway.json").read_bytes(), disk_before)
        self.assertEqual(self.g.ledger.get(self.did)["state"], "sent")
        self.assertEqual(self.state.idle_completions, [])
        self.assertEqual(self.g._delivery_pending, {})
        self.assertEqual(self.g._internal, {})
        self.assertEqual(len(self.sink.sent), 1)

    def test_idle_epoch_race_preserves_sent_without_resend(self):
        self.idle_race("epoch")

    def test_idle_thread_race_preserves_sent_without_resend(self):
        self.idle_race("thread")


class M4LeaseRecovery(GatewayFixture):
    def setUp(self):
        super().setUp()
        f = self.f
        PD.create(f.jobs.parent, recipient_kind="codex-managed-gateway", recipient_key=f.thread_id,
                  delivery_id=f.delivery_id, session_generation=str(f.gateway_epoch), session_generation_supported="1",
                  attempt_ids=[f.owner_attempt], parent_attempt_id=f.owner_attempt, route_id=f.route["route_id"],
                  route_node=f.route_node, receipt=f.receipt, receipt_digest=f.record["receipt_digest"],
                  row_revisions=f.record["row_revisions"])
        self.path = PD.record_path(f.jobs.parent, f.thread_id, f.delivery_id)
        args = types.SimpleNamespace(jobs=f.jobs, parent_session_id=f.thread_id, sealed_batch_id=f.sealed_batch,
                                     control_socket=self.root / "ctrl", interval=.1)
        self.watcher = CF.HumanGateWatcher(args, {f.owner_attempt}, {"epoch": f.gateway_epoch})
        self.clock = CF.time.monotonic_ns()
        self.transport_calls = 0

    def record(self):
        return json.loads(self.path.read_text())

    def test_nine_pre_send_storage_failures_recover_without_reissuing_the_question(self):
        with mock.patch.object(self.g.ledger, "_write", side_effect=OSError("temporary-storage-failure")):
            for _ in range(9):
                self.one()
                self.clock += 31_000_000_000
        self.assertEqual(self.record()["attempts"], 9)
        self.assertEqual(len(self.sink.sent), 0)
        self.state.steer_ready = True
        self.one()
        self.assertEqual(len(self.sink.sent), 1)
        self.response({"result": {"turnId": "busy"}})
        self.clock += 31_000_000_000
        self.one()
        self.assertEqual(self.record()["state"], "acked")
        self.assertEqual(len(self.sink.sent), 1)

    def transport(self, _socket, request):
        if request["op"] == "status":
            return {"status": "ready", "capabilities": {"human_gate_delivery": {
                "version": 1, "thread_id": self.f.thread_id, "epoch": self.f.gateway_epoch}}}
        self.transport_calls += 1
        with mock.patch.object(threading.Event, "wait", return_value=False):
            return self.g.deliver_human_gate(request)

    def one(self, transport=None):
        with mock.patch.object(CF.time, "monotonic_ns", side_effect=lambda: self.clock), \
                mock.patch.object(CF, "gateway_request", side_effect=transport or self.transport):
            self.watcher._one(self.path)

    def test_real_clock_transport_exception_preserves_own_unexpired_claim(self):
        # No clock mock: the failing transport is reached AFTER this watcher claims.
        def fail(socket, request):
            if request["op"] == "status":
                return self.transport(socket, request)
            self.transport_calls += 1
            self.assertEqual(self.record()["claim_owner"], self.watcher.owner)
            raise CF.CompletionError("fixture-transport-disconnected")
        with mock.patch.object(CF, "gateway_request", side_effect=fail):
            self.watcher._one(self.path)
        self.assertEqual(self.transport_calls, 1)
        row = self.record()
        self.assertEqual(row["state"], "claimed")
        self.assertEqual(row["claim_owner"], self.watcher.owner)
        self.assertGreater(row["claim_deadline_ns"], CF.time.monotonic_ns())
        self.one()
        self.assertEqual(self.record(), row)
        self.assertEqual(self.transport_calls, 1)

    def test_expired_claim_reclaims_once_and_ambiguous_never_resends(self):
        with mock.patch.object(PD.time, "monotonic_ns", return_value=self.clock):
            old = PD.claim(self.f.jobs.parent, self.f.thread_id, self.f.delivery_id,
                           claim_owner="old-watcher", lease_seconds=1, require_generation_proof=True)
        self.one()
        self.assertEqual(self.record(), old)
        self.assertEqual(self.transport_calls, 0)
        self.clock += 2_000_000_000  # Change the observed clock, never fake the reclaim argument.
        self.state.steer_ready = True
        self.one()
        current = self.record()
        self.assertEqual((current["state"], current["attempts"]), ("sent-ambiguous", 2))
        self.assertEqual(len(self.sink.sent), 1)
        self.one()
        self.assertEqual(self.record(), current)
        self.assertEqual(self.transport_calls, 1)
        self.g._delivery_pending.clear()
        self.g.ledger = GF.DeliveryLedger(self.root / "gateway.json")
        self.clock += 31_000_000_000
        self.one()
        self.assertEqual((self.record()["state"], self.record()["attempts"]), ("sent-ambiguous", 3))
        for _ in range(10):
            self.clock += 31_000_000_000
            self.one()
        self.assertEqual(self.record()["attempts"], 13)
        self.assertEqual(len(self.sink.sent), 1)
        self.assertEqual(self.record()["state"], "sent-ambiguous")

    def test_expired_claim_has_one_cas_winner(self):
        with mock.patch.object(PD.time, "monotonic_ns", return_value=self.clock):
            PD.claim(self.f.jobs.parent, self.f.thread_id, self.f.delivery_id, claim_owner="old", lease_seconds=1)
        self.clock += 2_000_000_000
        PD.reclaim(self.f.jobs.parent, self.f.thread_id, self.f.delivery_id, now_ns=self.clock)
        winners, failures = [], []
        barrier = threading.Barrier(2)

        def compete(owner):
            barrier.wait(timeout=5)
            try:
                winners.append(PD.claim(self.f.jobs.parent, self.f.thread_id, self.f.delivery_id,
                                        claim_owner=owner, lease_seconds=30))
            except PD.PendingDeliveryError as exc:
                failures.append(str(exc))

        with mock.patch.object(PD.time, "monotonic_ns", return_value=self.clock):
            threads = [threading.Thread(target=compete, args=(owner,)) for owner in ("a", "b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
        self.assertEqual((len(winners), len(failures)), (1, 1))
        self.assertEqual(self.record()["attempts"], 2)

    def test_accepted_but_ack_lost_recovers_by_replay_without_send(self):
        self.state.steer_ready = True
        self.one()
        self.response({"result": {"turnId": "busy"}})
        self.assertEqual(self.g.ledger.get(self.did)["state"], "accepted")
        self.assertEqual(self.record()["state"], "sent-ambiguous")
        self.clock += 31_000_000_000
        self.one()
        self.assertEqual(self.record()["state"], "acked")
        self.assertEqual(len(self.sink.sent), 1)

    def test_reclaim_prepared_deduplicates_queue_then_epoch_refusal(self):
        self.one()
        pending = self.g._delivery_pending[self.did]
        self.clock += 31_000_000_000
        self.one()
        self.assertIs(self.g._delivery_pending[self.did], pending)
        self.assertEqual(self.state.queued, [pending])
        self.assertEqual(len(self.sink.sent), 0)
        self.g._epoch += 1
        self.state.steer_ready = True
        with self.g._lock:
            self.g._drain_queued_locked(self.state)
        self.assertIsInstance(pending.outcome, dict)
        self.assertEqual(pending.outcome["status"], "rejected")
        self.assertEqual(self.state.queued, [])
        self.assertEqual(self.g._delivery_pending, {})
        self.assertEqual(self.g.ledger.get(self.did)["state"], "rejected")
        self.assertEqual(len(self.sink.sent), 0)


    def test_m3d2_untyped_rejection_preserves_claim(self):
        def untyped(socket, request):
            if request["op"] == "status":
                return self.transport(socket, request)
            self.transport_calls += 1
            return {"status": "rejected", "reason": "[Errno 13] Permission denied"}
        self.one(untyped)
        self.assertEqual(self.transport_calls, 1)
        self.assertEqual(self.record()["state"], "claimed")
        self.assertIn(self.path, self.watcher._candidate_paths(self.path.parent))
        self.assertIn("gateway-rejection-untyped", self.watcher.errors[-1])

    def test_m3d2_unconfirmed_rejection_is_not_terminal(self):
        def unconfirmed(socket, request):
            if request["op"] == "status":
                return self.transport(socket, request)
            return {"status": "rejected", "reason": "rejection-ledger-commit-failed",
                    "ledger_commit": "unconfirmed"}
        self.one(unconfirmed)
        self.assertEqual(self.record()["state"], "claimed")

    def test_m3d2_prepare_retryable_claim_recovers_after_expiry(self):
        os.chmod(self.root, 0o500)
        self.addCleanup(os.chmod, self.root, 0o700)
        with self.assertRaises(PermissionError):
            fd, path = tempfile.mkstemp(dir=self.root)
            os.close(fd)
            Path(path).unlink()
        self.one()
        self.assertEqual(self.transport_calls, 1)
        self.assertEqual(self.record()["state"], "claimed")
        os.chmod(self.root, 0o700)
        self.clock += 31_000_000_000
        self.state.steer_ready = True
        self.one()
        self.assertEqual(len(self.sink.sent), 1)
        self.response({"result": {"turnId": "busy"}})
        self.clock += 31_000_000_000
        self.one()
        self.assertEqual(self.record()["state"], "acked")
        self.assertEqual(len(self.sink.sent), 1)


class M3D2StorageFailure(GatewayFixture):
    def readonly(self):
        os.chmod(self.root, 0o500)
        self.addCleanup(os.chmod, self.root, 0o700)
        with self.assertRaises(PermissionError):
            fd, path = tempfile.mkstemp(dir=self.root)
            os.close(fd)
            Path(path).unlink()

    def call(self):
        # Keep notification semantics real; only avoid a 120s timeout.
        with mock.patch.object(threading.Event, "wait", lambda event, timeout=None: event.is_set()):
            return self.g.deliver_human_gate(self.request)

    def clean_pending(self):
        self.assertEqual(self.g._delivery_pending, {})
        self.assertEqual(self.g._internal, {})
        self.assertEqual(self.state.queued, [])
        self.assertEqual(self.state.idle_completions, [])
        self.assertIsNone(self.state.pending_start_id)
        self.assertEqual(self.state.pending_start_owner, "")

    def test_s1_prepare_eacces_restores_retryable_identity(self):
        self.readonly()
        result = self.call()
        self.assertEqual((result["status"], result["reason"]),
                         ("retryable", "prepared-ledger-commit-failed"))
        self.assertIsNone(self.g.ledger.get(self.did))
        self.clean_pending()
        self.assertEqual(self.sink.sent, [])
        os.chmod(self.root, 0o700)
        self.state.active_turn_id = ""
        self.call()
        self.assertEqual(len(self.sink.sent), 1)
        self.response({"result": {"turn": {"id": "recovered"}}})
        self.assertEqual(self.g.deliver_human_gate(self.request)["status"], "accepted")

    def test_s2_sent_eacces_not_sent_cleans_and_retries(self):
        self.call()  # Real validation and prepared queue.
        pending = self.g._delivery_pending[self.did]
        self.readonly()
        self.state.steer_ready = True
        with self.g._lock:
            self.g._drain_queued_locked(self.state)
        self.assertTrue(pending.event.is_set())
        self.assertEqual((pending.outcome["status"], pending.outcome["reason"]),
                         ("retryable", "sent-ledger-commit-failed"))
        self.assertEqual(self.g.ledger.get(self.did)["state"], "prepared")
        self.assertEqual(GF.DeliveryLedger(self.root / "gateway.json").get(self.did)["state"], "prepared")
        self.clean_pending()
        self.assertEqual(self.sink.sent, [])
        os.chmod(self.root, 0o700)
        self.call()
        self.assertEqual(len(self.sink.sent), 1)

    def test_s3_transport_failure_is_ambiguous_without_phantom_start(self):
        self.state.active_turn_id = ""
        with mock.patch.object(self.sink, "write_json", side_effect=OSError(errno.EPIPE, "broken pipe")) as send:
            result = self.call()
        self.assertEqual(send.call_count, 1)
        self.assertEqual((result["status"], result["reason"]),
                         ("sent-ambiguous", "human-gate-send-failed"))
        self.clean_pending()
        self.assertEqual(GF.DeliveryLedger(self.root / "gateway.json").get(self.did)["state"], "sent")
        self.assertEqual(self.g.deliver_human_gate(self.request)["status"], "sent-ambiguous")
        self.assertEqual(self.sink.sent, [])

    def terminal_storage_failure(self, accepted):
        self.state.active_turn_id = ""
        self.call()
        pending = self.g._delivery_pending[self.did]
        self.readonly()
        response = {"result": {"turn": {"id": "accepted-turn"}}} if accepted else {
            "error": {"code": -1, "message": "boom"}}
        self.response(response)
        self.assertTrue(pending.event.is_set())
        expected = "accepted-ledger-commit-failed" if accepted else "rejection-ledger-commit-failed"
        self.assertEqual((pending.outcome["status"], pending.outcome["reason"]), ("sent-ambiguous", expected))
        self.assertEqual(self.g.ledger.get(self.did)["state"], "sent")
        self.assertEqual(GF.DeliveryLedger(self.root / "gateway.json").get(self.did)["state"], "sent")
        self.clean_pending()
        before = dict(pending.outcome)
        self.g._disconnect_epoch(self.g._epoch, None, None)
        self.assertEqual(pending.outcome, before)
        self.assertEqual(len(self.sink.sent), 1)

    def test_s4_accept_storage_failure_not_success_or_disconnect(self):
        self.terminal_storage_failure(True)

    def test_s5_reject_storage_failure_not_terminal_rejection(self):
        self.terminal_storage_failure(False)

    def test_s7_actual_control_socket_prepare_failure_is_typed_retryable(self):
        self.readonly()
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        client.settimeout(2)
        client.sendall((json.dumps(self.request) + "\n").encode())
        self.g._handle_control(server)
        result = json.loads(client.recv(16384))
        self.assertEqual((result["status"], result["reason"]),
                         ("retryable", "prepared-ledger-commit-failed"))
        self.assertNotIn("[Errno", result["reason"])

    def test_sent_post_replace_fsync_failure_never_resends(self):
        self.call()
        pending = self.g._delivery_pending[self.did]
        self.state.steer_ready = True
        fsync = GF.os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "directory fsync failed")
            return fsync(fd)
        with mock.patch.object(GF.os, "fsync", side_effect=fail_directory):
            with self.g._lock:
                self.g._drain_queued_locked(self.state)
        self.assertTrue(pending.event.is_set())
        self.assertEqual(pending.outcome["status"], "sent-ambiguous")
        self.clean_pending()
        self.assertEqual(GF.DeliveryLedger(self.root / "gateway.json").get(self.did)["state"], "sent")
        self.assertEqual(self.g.deliver_human_gate(self.request)["status"], "sent-ambiguous")
        self.assertEqual(self.sink.sent, [])

    def test_prepare_post_replace_fsync_failure_can_retry_before_any_send(self):
        fsync = GF.os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "directory fsync failed")
            return fsync(fd)
        with mock.patch.object(GF.os, "fsync", side_effect=fail_directory):
            result = self.call()
        self.assertEqual(result["status"], "retryable")
        self.assertEqual(GF.DeliveryLedger(self.root / "gateway.json").get(self.did)["state"], "prepared")
        self.clean_pending()
        self.state.active_turn_id = ""
        self.call()
        self.assertEqual(len(self.sink.sent), 1)

    def test_idle_sent_failure_clears_start_reservation(self):
        self.state.active_turn_id = ""
        write = self.g.ledger._write
        calls = []
        def fail_sent():
            calls.append(self.g.ledger.get(self.did)["state"])
            if calls[-1] == "sent":
                self.readonly()
            return write()
        with mock.patch.object(self.g.ledger, "_write", side_effect=fail_sent):
            result = self.call()
        self.assertEqual(calls, ["prepared", "sent"])
        self.assertEqual(result["status"], "retryable")
        self.clean_pending()
        os.chmod(self.root, 0o700)
        self.call()
        self.assertEqual(len(self.sink.sent), 1)

    def test_transport_written_then_exception_is_never_resent(self):
        self.state.active_turn_id = ""
        write = self.sink.write_json
        def partial(message):
            write(message)
            raise OSError(errno.EPIPE, "ack of send unavailable")
        with mock.patch.object(self.sink, "write_json", side_effect=partial):
            result = self.call()
        self.assertEqual(result["status"], "sent-ambiguous")
        self.clean_pending()
        self.assertEqual(self.g.deliver_human_gate(self.request)["status"], "sent-ambiguous")
        self.assertEqual(len(self.sink.sent), 1)

    def test_control_oserror_uses_typed_retryable_result(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        client.settimeout(2)
        with mock.patch.object(self.g, "_read_control_line", side_effect=OSError(errno.EIO, "read failure")):
            self.g._handle_control(server)
        result = json.loads(client.recv(16384))
        self.assertEqual((result["status"], result["reason"]), ("retryable", "control-io-failed"))


if __name__ == "__main__":
    unittest.main()
