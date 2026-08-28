#!/usr/bin/env python3
"""SD-111 P1: dispatch_pending_delivery state-machine unit tests.

Every fixture injects HOME/XDG_STATE_HOME/HARNESS_STATE_ROOT into an isolated
temp tree and appends the actual values to evidence/sd111/fixture_env.tsv
(plan §10.1 hard gate) even though this module never reads them itself --
callers derive `root` and pass it in, but the blanket fixture-isolation
requirement applies to every new SD-111 test file regardless.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "dispatch_pending_delivery", HERE / "dispatch_pending_delivery.py"
)
PD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = PD
SPEC.loader.exec_module(PD)

FIXTURE_ENV_LOG = os.environ.get("SD111_FIXTURE_ENV_LOG")


def _log_fixture_env(test_file: str, home: str, xdg: str, harness: str) -> None:
    if not FIXTURE_ENV_LOG:
        return
    line = f"{test_file}\t{home}\t{xdg}\t{harness}\n"
    with open(FIXTURE_ENV_LOG, "a", encoding="utf-8") as handle:
        handle.write(line)


class IsolatedRootMixin:
    """Injects HOME/XDG_STATE_HOME/HARNESS_STATE_ROOT into a temp tree and
    derives `self.root` from HARNESS_STATE_ROOT (SD-112 stable-root order),
    matching the plan's mandatory env-isolation gate for every new fixture."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sd111-p1-")
        base = Path(self._tmp.name)
        home = base / "home"
        xdg = base / "xdg-state"
        harness = base / "harness-state"
        for d in (home, xdg, harness):
            d.mkdir(parents=True, exist_ok=True)
        self._env_patch = {
            "HOME": str(home),
            "XDG_STATE_HOME": str(xdg),
            "HARNESS_STATE_ROOT": str(harness),
        }
        self._saved_env = {k: os.environ.get(k) for k in self._env_patch}
        os.environ.update(self._env_patch)
        _log_fixture_env(
            "dispatch_pending_delivery.test.py",
            str(home),
            str(xdg),
            str(harness),
        )
        self.root = harness / "dispatch"

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()
        super().tearDown()


def _receipt(**overrides):
    base = {
        "schema_version": 2,
        "state": "delivered",
        "parent_attempt_id": "att-0000000000000000000000000000aaaa",
        "job_registry": "/tmp/sd111p1/jobs.log",
        "children": [
            {
                "attempt_id": "att-0000000000000000000000000000bbbb",
                "status": "done",
                "readiness": "ready",
                "reason": "terminal-failure-or-unclosed",
                "required_action": "inspect-done-failure",
                "harness": "claude",
                "delivery_classification": "attention",
            }
        ],
        "delivery_classification": "attention",
    }
    base.update(overrides)
    return base


def _digest(receipt):
    return PD._canonical_receipt_digest(receipt)


class CreateTest(IsolatedRootMixin, unittest.TestCase):
    def _create(self, **overrides):
        receipt = overrides.pop("receipt", _receipt())
        kwargs = dict(
            root=self.root,
            recipient_kind="claude-parent-runtime",
            recipient_key="sess-abc",
            delivery_id="delivery-" + "a" * 32,
            session_generation="",
            session_generation_supported="0",
            attempt_ids=["att-0000000000000000000000000000bbbb"],
            parent_attempt_id="att-0000000000000000000000000000aaaa",
            route_id="rt-example",
            route_node="execute",
            receipt=receipt,
            receipt_digest=_digest(receipt),
            row_revisions={"att-0000000000000000000000000000bbbb": "deadbeef"},
        )
        kwargs.update(overrides)
        return kwargs, PD.create(**kwargs)

    def test_create_persists_schema_v1_record(self):
        kwargs, record = self._create()
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["receipt"], kwargs["receipt"])
        self.assertEqual(set(record), set(PD.REQUIRED_FIELDS))

    def test_create_is_idempotent_o_excl_semantics(self):
        kwargs, first = self._create()
        _, second = self._create(receipt=kwargs["receipt"])
        self.assertEqual(first, second)

    def test_second_trigger_converges_on_one_file_no_carrier_involved(self):
        # Round 2 C-1 invariant: N materializer triggers -> one record. Model
        # a crash-recovery double-call (trigger 1 then trigger 2) directly.
        self._create()
        directory = PD.record_directory(self.root, "sess-abc")
        files = list(directory.glob("*.json"))
        self._create()
        self.assertEqual(list(directory.glob("*.json")), files)
        self.assertEqual(len(files), 1)

    def test_identity_conflict_on_attempt_ids_mismatch(self):
        self._create()
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            self._create(attempt_ids=["att-0000000000000000000000000000cccc"])
        self.assertEqual(ctx.exception.reason, "pending-delivery-identity-conflict")

    def test_identity_conflict_on_receipt_digest_mismatch_against_declared(self):
        receipt = _receipt()
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            self._create(receipt=receipt, receipt_digest="0" * 64)
        self.assertEqual(ctx.exception.reason, "pending-delivery-identity-conflict")

    def test_oversized_receipt_is_refused(self):
        receipt = _receipt(job_registry="x" * PD.MAX_RECEIPT_BYTES)
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            self._create(receipt=receipt, receipt_digest=_digest(receipt))
        self.assertEqual(ctx.exception.reason, "pending-delivery-oversized")

    def test_unknown_recipient_kind_is_rejected(self):
        with self.assertRaises(PD.PendingDeliveryError):
            self._create(recipient_kind="unknown-surface")

    def test_directory_and_file_permissions(self):
        _, record = self._create()
        path = PD.record_path(self.root, "sess-abc", record["delivery_id"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), PD.FILE_MODE)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), PD.DIR_MODE)
        lock_path = path.with_name(path.name + ".lock")
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), PD.FILE_MODE)


class ClaimCasTest(IsolatedRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        receipt = _receipt()
        self.kwargs = dict(
            root=self.root,
            recipient_kind="claude-parent-runtime",
            recipient_key="sess-claim",
            delivery_id="delivery-" + "b" * 32,
            session_generation="",
            session_generation_supported="0",
            attempt_ids=["att-0000000000000000000000000000bbbb"],
            parent_attempt_id="att-0000000000000000000000000000aaaa",
            route_id="rt-example",
            route_node="execute",
            receipt=receipt,
            receipt_digest=_digest(receipt),
            row_revisions={"att-0000000000000000000000000000bbbb": "deadbeef"},
        )
        PD.create(**self.kwargs)

    def test_first_claim_succeeds_second_is_refused(self):
        first = PD.claim(
            self.root, "sess-claim", self.kwargs["delivery_id"],
            claim_owner="carrier-1", lease_seconds=30,
        )
        self.assertEqual(first["state"], "claimed")
        self.assertEqual(first["claim_owner"], "carrier-1")
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            PD.claim(
                self.root, "sess-claim", self.kwargs["delivery_id"],
                claim_owner="carrier-2", lease_seconds=30,
            )
        self.assertEqual(ctx.exception.reason, "pending-delivery-claim-refused")

    def test_generation_unproven_carrier_is_rejected(self):
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            PD.claim(
                self.root, "sess-claim", self.kwargs["delivery_id"],
                claim_owner="carrier-2", lease_seconds=30,
                require_generation_proof=True,
            )
        self.assertEqual(ctx.exception.reason, "pending-delivery-generation-unproven")

    def test_claim_emit_sent_ambiguous_never_reaches_acked_on_token_less_path(self):
        PD.claim(
            self.root, "sess-claim", self.kwargs["delivery_id"],
            claim_owner="carrier-1", lease_seconds=30,
        )
        emitted = PD.mark_sent_ambiguous(
            self.root, "sess-claim", self.kwargs["delivery_id"], claim_owner="carrier-1"
        )
        self.assertEqual(emitted["state"], "sent-ambiguous")
        self.assertIsNone(emitted["acked_at_ns"])
        self.assertIsNone(emitted["acked_by"])

    def test_ack_available_for_token_bearing_surface(self):
        PD.claim(
            self.root, "sess-claim", self.kwargs["delivery_id"],
            claim_owner="carrier-2", lease_seconds=30,
        )
        acked = PD.ack(
            self.root, "sess-claim", self.kwargs["delivery_id"], acked_by="codex-managed-gateway"
        )
        self.assertEqual(acked["state"], "acked")
        self.assertIsNotNone(acked["acked_at_ns"])

    def test_reclaim_before_lease_expiry_is_refused(self):
        claimed = PD.claim(
            self.root, "sess-claim", self.kwargs["delivery_id"],
            claim_owner="carrier-1", lease_seconds=30,
        )
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            PD.reclaim(
                self.root, "sess-claim", self.kwargs["delivery_id"],
                now_ns=claimed["claimed_at_ns"],
            )
        self.assertEqual(ctx.exception.reason, "pending-delivery-claim-refused")

    def test_reclaim_after_lease_expiry_returns_to_pending(self):
        claimed = PD.claim(
            self.root, "sess-claim", self.kwargs["delivery_id"],
            claim_owner="carrier-1", lease_seconds=1,
        )
        reclaimed = PD.reclaim(
            self.root, "sess-claim", self.kwargs["delivery_id"],
            now_ns=claimed["claim_deadline_ns"] + 1,
        )
        self.assertEqual(reclaimed["state"], "pending")
        self.assertIsNone(reclaimed["claim_owner"])

    def test_reclaim_exhaustion_is_a_typed_terminal_refusal(self):
        deadline = None
        for owner in range(PD.RECLAIM_LIMIT):
            claimed = PD.claim(
                self.root, "sess-claim", self.kwargs["delivery_id"],
                claim_owner=f"carrier-{owner}", lease_seconds=1,
            )
            deadline = claimed["claim_deadline_ns"]
            PD.reclaim(
                self.root, "sess-claim", self.kwargs["delivery_id"],
                now_ns=deadline + 1,
            )
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            PD.claim(
                self.root, "sess-claim", self.kwargs["delivery_id"],
                claim_owner="carrier-final", lease_seconds=1,
            )
        self.assertEqual(ctx.exception.reason, "pending-delivery-reclaim-exhausted")


class ExpiryTest(IsolatedRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        receipt = _receipt()
        self.kwargs = dict(
            root=self.root,
            recipient_kind="codex-stop-hook",
            recipient_key="sess-expiry",
            delivery_id="delivery-" + "c" * 32,
            session_generation="",
            session_generation_supported="0",
            attempt_ids=["att-0000000000000000000000000000bbbb"],
            parent_attempt_id="att-0000000000000000000000000000aaaa",
            route_id="rt-example",
            route_node="execute",
            receipt=receipt,
            receipt_digest=_digest(receipt),
            row_revisions={"att-0000000000000000000000000000bbbb": "deadbeef"},
        )
        PD.create(**self.kwargs)

    def test_only_declared_actor_may_expire(self):
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            PD.expire_if_due(
                self.root, "sess-expiry", self.kwargs["delivery_id"],
                actor="fleet-collector", reason="pending-delivery-ttl-exceeded",
            )
        self.assertEqual(ctx.exception.reason, "pending-delivery-expiry-actor-invalid")

    def test_declared_actor_expires_a_pending_record(self):
        expired = PD.expire_if_due(
            self.root, "sess-expiry", self.kwargs["delivery_id"],
            actor="dispatch-reconcile", reason="pending-delivery-ttl-exceeded",
        )
        self.assertEqual(expired["state"], "expired")
        self.assertEqual(expired["expiry_reason"], "pending-delivery-ttl-exceeded")

    def test_unknown_liveness_never_expires(self):
        result = PD.expire_if_due(
            self.root, "sess-expiry", self.kwargs["delivery_id"],
            actor="dispatch-reconcile", reason="recipient-session-gone",
            liveness="unknown",
        )
        self.assertEqual(result["state"], "pending")

    def test_expired_record_is_never_deleted(self):
        PD.expire_if_due(
            self.root, "sess-expiry", self.kwargs["delivery_id"],
            actor="dispatch-reconcile", reason="receipt-row-superseded",
        )
        path = PD.record_path(self.root, "sess-expiry", self.kwargs["delivery_id"])
        self.assertTrue(path.is_file())
        again = PD.read(self.root, "sess-expiry", self.kwargs["delivery_id"])
        self.assertEqual(again["state"], "expired")

    def test_expiring_an_already_terminal_record_is_a_no_op(self):
        first = PD.expire_if_due(
            self.root, "sess-expiry", self.kwargs["delivery_id"],
            actor="dispatch-reconcile", reason="pending-delivery-ttl-exceeded",
        )
        second = PD.expire_if_due(
            self.root, "sess-expiry", self.kwargs["delivery_id"],
            actor="dispatch-reconcile", reason="recipient-session-gone",
        )
        self.assertEqual(first["expiry_reason"], second["expiry_reason"])


if __name__ == "__main__":
    unittest.main()
