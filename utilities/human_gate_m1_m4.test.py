#!/usr/bin/env python3
"""Closed behavioral regressions for the approved M1-M4 integration delta."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest


SOURCE = Path(os.environ.get("HEARTING_TEST_SOURCE_ROOT", Path(__file__).resolve().parents[1]))
UTIL = SOURCE / "utilities"
import sys
sys.path.insert(0, str(UTIL))


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, UTIL / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class NoWait:
    def wait(self, _seconds):
        return False
    def set(self):
        return None


class Sink:
    def __init__(self):
        self.messages = []
    def write_json(self, value):
        self.messages.append(value)


class M1M4(unittest.TestCase):
    def test_m1_cancelled_missing_decision_is_not_release(self):
        route_mod = load("m1_capability_route", "capability-route.py")
        gate = "frame-review"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs.log"
            jobs.write_text("", encoding="utf-8")
            route = {
                "route_id": "route-m1", "route_hash": "hash-m1",
                "launch_compatibility_tuple": {"jobs_path": {"path": str(jobs)}},
                "human_gate_bindings": [{"gate": gate, "release_authority": "any"}],
            }
            journal = jobs.parent / "workflow" / "route-m1" / "journal.jsonl"
            journal.parent.mkdir(parents=True)
            entries = [
                {"workflow_state": "BLOCKED_HUMAN_GATE", "at": "1", "evidence": {"gate": gate}},
                {"workflow_state": "CANCELLED", "at": "2", "evidence": {
                    "gate": gate, "released_gate": gate, "released_by": "user",
                    "actor_kind": "user", "abandon_reason": "operator-decision",
                }},
            ]
            journal.write_text("".join(json.dumps(x) + "\n" for x in entries), encoding="utf-8")
            with self.assertRaises(ValueError):
                route_mod._continuation_gate_release_proof(route, gate)

    def test_m2_prepared_duplicate_has_one_pending_and_queue(self):
        gateway = load("m2_gateway", "codex-managed-gateway.py")
        original_event = gateway.threading.Event
        gateway.threading.Event = NoWait
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                sink = Sink()
                g = gateway.ManagedGateway(
                    listen_path=root / "listen", upstream_path=root / "up",
                    control_path=root / "control", ledger_path=root / "ledger.json",
                )
                g._binding_thread_id, g._epoch = "thread", 7
                g._tui, g._upstream = object(), sink
                receipt = {"recipient_epoch": 7}
                digest = "digest"
                g._human_gate_validation = lambda _request: (
                    "thread", "parent", "batch", receipt, digest, "delivery-m2"
                )
                state = gateway.ThreadState(pending_start_id=("manual", 1))
                g._threads["thread"] = state
                first = g.deliver_human_gate({})
                second = g.deliver_human_gate({})
                self.assertEqual(first["status"], "sent-ambiguous")
                self.assertEqual(second["status"], "sent-ambiguous")
                self.assertEqual(len(state.queued), 1)
                self.assertEqual(len(g._delivery_pending), 1)
                self.assertEqual(len(sink.messages), 0)
        finally:
            gateway.threading.Event = original_event

    def test_m3_stale_epoch_rejects_and_cleans_prepared_state(self):
        gateway = load("m3_gateway", "codex-managed-gateway.py")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = gateway.ManagedGateway(
                listen_path=root / "listen", upstream_path=root / "up",
                control_path=root / "control", ledger_path=root / "ledger.json",
            )
            identity = {"thread_id": "thread", "parent_attempt_id": "parent",
                        "sealed_batch_id": "batch", "receipt_digest": "digest"}
            pending = gateway.PendingInternal(
                kind="human-gate-wait", thread_id="thread", delivery_id="delivery-m3",
                identity=identity, receipt={"recipient_epoch": 7},
            )
            g._binding_thread_id, g._epoch = "thread", 8
            g._upstream = Sink()
            g.ledger._transition("delivery-m3", "prepared", **identity)
            g._delivery_pending["delivery-m3"] = pending
            state = gateway.ThreadState(queued=[pending])
            g._threads["thread"] = state
            gateway.human_gate_receipt.context = lambda _receipt, _delivery: {}
            g._send_human_gate_locked(pending, state)
            self.assertEqual((pending.outcome or {}).get("status"), "rejected")
            self.assertNotIn("delivery-m3", g._delivery_pending)
            self.assertEqual(state.queued, [])
            self.assertEqual(g.ledger.get("delivery-m3")["state"], "rejected")

    def test_m4_watcher_exception_does_not_steal_live_claim(self):
        completion = load("m4_completion", "codex-managed-completion.py")
        receipt_test = load("m4_receipt_test", "human_gate_receipt.test.py")
        import dispatch_pending_delivery as pending
        receipt_mod = receipt_test.load_module()
        with tempfile.TemporaryDirectory() as td:
            fx = receipt_test.Fixture(Path(td), receipt_mod)
            pending.create(
                fx.jobs.parent, recipient_kind="codex-managed-gateway",
                recipient_key=fx.thread_id, delivery_id=fx.delivery_id,
                session_generation=str(fx.gateway_epoch), session_generation_supported="1",
                attempt_ids=[fx.owner_attempt], parent_attempt_id=fx.owner_attempt,
                route_id=fx.route["route_id"], route_node=fx.route_node,
                receipt=fx.receipt, receipt_digest=fx.record["receipt_digest"],
                row_revisions=fx.record["row_revisions"],
            )
            pending.claim(fx.jobs.parent, fx.thread_id, fx.delivery_id,
                          claim_owner="other", lease_seconds=30,
                          require_generation_proof=True)
            args = types.SimpleNamespace(
                jobs=fx.jobs, parent_session_id=fx.thread_id,
                interval=1, control_socket=Path(td) / "control",
                sealed_batch_id=fx.sealed_batch,
            )
            watcher = completion.HumanGateWatcher(args, {fx.owner_attempt}, {"epoch": 7})
            completion.negotiate_human_gate = lambda _args: {"epoch": 7}
            calls = iter([RuntimeError("transport")])
            def request(_socket, _payload):
                value = next(calls)
                if isinstance(value, Exception):
                    raise value
                return value
            completion.gateway_request = request
            watcher._one(pending.record_path(fx.jobs.parent, fx.thread_id, fx.delivery_id))
            self.assertEqual(pending.read(fx.jobs.parent, fx.thread_id, fx.delivery_id)["state"], "claimed")


if __name__ == "__main__":
    unittest.main()
