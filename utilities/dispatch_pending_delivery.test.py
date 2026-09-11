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
import unittest.mock
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

    def test_a47_6_claim_authority_required_on_claimed_states(self):
        kwargs, record = self._create()
        self.assertEqual(record["claim_authority"], "")
        claimed = PD.claim(
            self.root, kwargs["recipient_key"], kwargs["delivery_id"],
            claim_owner="carrier-1", lease_seconds=30,
        )
        self.assertEqual(claimed["claim_authority"], "deliverer-unproven")
        broken = dict(claimed)
        broken["claim_authority"] = ""
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            PD._validate_record(broken)
        self.assertEqual(ctx.exception.reason, "delivery-persistence-refused")
        self.assertEqual(ctx.exception.detail, "claim-authority-invalid")

    def test_a47_6_legacy_v1_record_upgrades_to_deliverer_unproven(self):
        kwargs, record = self._create(recipient_kind="claude-parent-runtime")
        path = PD.record_path(self.root, kwargs["recipient_key"], kwargs["delivery_id"])
        legacy = dict(record)
        del legacy["claim_authority"]
        legacy["state"] = "sent-ambiguous"
        legacy["claim_owner"] = "legacy-owner"
        legacy["claim_deadline_ns"] = 0
        path.write_text(json.dumps(legacy), encoding="utf-8")
        read_back = PD.read(self.root, kwargs["recipient_key"], kwargs["delivery_id"])
        self.assertEqual(read_back["claim_authority"], "deliverer-unproven")
        acked = PD.ack(self.root, kwargs["recipient_key"], kwargs["delivery_id"], acked_by="t")
        self.assertEqual(acked["state"], "acked")

    def test_a47_6_legacy_v1_record_other_kind_upgrades_to_generation_proven(self):
        kwargs, record = self._create(recipient_kind="codex-stop-hook")
        path = PD.record_path(self.root, kwargs["recipient_key"], kwargs["delivery_id"])
        legacy = dict(record)
        del legacy["claim_authority"]
        legacy["state"] = "claimed"
        legacy["claim_owner"] = "legacy-owner"
        legacy["claim_deadline_ns"] = 0
        path.write_text(json.dumps(legacy), encoding="utf-8")
        read_back = PD.read(self.root, kwargs["recipient_key"], kwargs["delivery_id"])
        self.assertEqual(read_back["claim_authority"], "generation-proven")

    def test_a47_6_new_shape_missing_claim_authority_field_entirely_is_still_refused(self):
        # A brand-new-shape record (all REQUIRED_FIELDS keys present) that
        # simply omits claim_authority is NOT the legacy v1 shape (legacy is
        # exactly REQUIRED_FIELDS - {claim_authority}, i.e. missing key) --
        # confirm a record shaped exactly like the current REQUIRED_FIELDS
        # set is required, not "any subset works".
        kwargs, record = self._create()
        broken = dict(record)
        broken["unexpected_extra_field"] = "1"
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            PD._validate_record(broken)
        self.assertEqual(ctx.exception.detail, "record-shape-invalid")


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

    def test_expired_claims_do_not_cancel_delivery_after_old_eight_attempt_limit(self):
        deadline = None
        for owner in range(10):
            claimed = PD.claim(
                self.root, "sess-claim", self.kwargs["delivery_id"],
                claim_owner=f"carrier-{owner}", lease_seconds=1,
            )
            deadline = claimed["claim_deadline_ns"]
            PD.reclaim(
                self.root, "sess-claim", self.kwargs["delivery_id"],
                now_ns=deadline + 1,
            )
        final = PD.claim(self.root, "sess-claim", self.kwargs["delivery_id"],
                         claim_owner="carrier-final", lease_seconds=1)
        self.assertEqual(final["attempts"], 11)
        PD.ack(self.root, "sess-claim", self.kwargs["delivery_id"], acked_by="carrier-final")
        self.assertEqual(PD.read(self.root, "sess-claim", self.kwargs["delivery_id"])["state"], "acked")

    def test_recovery_preserves_one_claim_authority_and_live_lease_exclusion(self):
        deadline = None
        for owner in range(10):
            claimed = PD.claim(
                self.root, "sess-claim", self.kwargs["delivery_id"],
                claim_owner=f"carrier-{owner}", lease_seconds=1,
                require_generation_proof=False,
            )
            self.assertEqual(claimed["claim_authority"], "deliverer-unproven")
            deadline = claimed["claim_deadline_ns"]
            PD.reclaim(
                self.root, "sess-claim", self.kwargs["delivery_id"],
                now_ns=deadline + 1,
            )
        final = PD.claim(self.root, "sess-claim", self.kwargs["delivery_id"],
                         claim_owner="carrier-final", lease_seconds=1,
                         require_generation_proof=False)
        self.assertEqual(final["claim_authority"], "deliverer-unproven")
        with self.assertRaisesRegex(PD.PendingDeliveryError, "lease-not-expired"):
            PD.reclaim(self.root, "sess-claim", self.kwargs["delivery_id"], now_ns=final["claimed_at_ns"])


class PruneTest(IsolatedRootMixin, unittest.TestCase):
    """SD-111 §(7) v66: terminal records are kept for the retention window and
    then pruned by the reconcile actor; open records are never pruned."""

    DAY = 86400

    def _record(self, suffix, *, state="pending", age_seconds=0.0):
        receipt = _receipt()
        delivery_id = "delivery-" + suffix * 32
        recipient_key = "sess-prune"
        PD.create(
            self.root, recipient_kind="claude-parent-runtime", recipient_key=recipient_key,
            delivery_id=delivery_id, session_generation="", session_generation_supported="0",
            attempt_ids=["att-" + suffix * 32], parent_attempt_id="att-" + "a" * 32,
            route_id="rt-prune", route_node="execute", receipt=receipt,
            receipt_digest=_digest(receipt), row_revisions={"att-" + suffix * 32: "deadbeef"},
        )
        if state in {"claimed", "sent-ambiguous", "acked"}:
            PD.claim(self.root, recipient_key, delivery_id, claim_owner="hook:1", lease_seconds=60)
        if state == "sent-ambiguous":
            PD.mark_sent_ambiguous(self.root, recipient_key, delivery_id, claim_owner="hook:1")
        if state == "acked":
            PD.ack(self.root, recipient_key, delivery_id, acked_by="fixture")
        if state == "expired":
            PD.expire_if_due(self.root, recipient_key, delivery_id, actor=PD.EXPIRY_ACTOR,
                             reason="pending-delivery-ttl-exceeded")
        path = PD.record_path(self.root, recipient_key, delivery_id)
        self.assertEqual(json.loads(path.read_text("utf-8"))["state"], state)
        old = time.time() - age_seconds
        os.utime(path, (old, old))
        return path

    def _orphan_lock(self, name, age_seconds):
        directory = self.root / "pending-delivery" / ("f" * 64)
        directory.mkdir(parents=True, exist_ok=True)
        lock = directory / f"{name}.json.lock"
        lock.write_bytes(b"")
        old = time.time() - age_seconds
        os.utime(lock, (old, old))
        return lock

    def test_plan_keeps_open_records_whatever_their_age(self):
        kept = [self._record(s, state=st, age_seconds=400 * self.DAY)
                for s, st in (("1", "pending"), ("2", "claimed"), ("3", "sent-ambiguous"))]
        plan = PD.prune_plan(self.root)
        self.assertEqual(plan["records"], [])
        self.assertEqual(plan["kept_open"], 3)
        result = PD.prune(self.root, apply=True)
        self.assertEqual(result["pruned_records"], 0)
        for path in kept:
            self.assertTrue(path.is_file())
            self.assertTrue(path.with_name(path.name + ".lock").is_file())

    def test_plan_lists_only_terminal_records_past_retention(self):
        old_acked = self._record("4", state="acked", age_seconds=8 * self.DAY)
        old_expired = self._record("5", state="expired", age_seconds=30 * self.DAY)
        fresh_acked = self._record("6", state="acked", age_seconds=1 * self.DAY)
        plan = PD.prune_plan(self.root)
        self.assertEqual(
            sorted(item["path"] for item in plan["records"]),
            sorted([str(old_acked), str(old_expired)]),
        )
        self.assertEqual(plan["kept_terminal_recent"], 1)
        self.assertEqual({item["state"] for item in plan["records"]}, {"acked", "expired"})
        # dry-run never unlinks
        PD.prune(self.root, apply=False)
        for path in (old_acked, old_expired, fresh_acked):
            self.assertTrue(path.is_file())

    def test_apply_unlinks_record_and_lock_and_leaves_a_tombstone(self):
        old_acked = self._record("7", state="acked", age_seconds=8 * self.DAY)
        result = PD.prune(self.root, apply=True)
        self.assertEqual(result["pruned_records"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertFalse(old_acked.exists())
        self.assertFalse(old_acked.with_name(old_acked.name + ".lock").exists())
        tomb = PD.tombstone_path(old_acked)
        self.assertTrue(tomb.is_file(), "the tombstone is what stops re-materialization (B1)")
        self.assertTrue(old_acked.parent.is_dir(), "recipient directories are never removed")
        # a second prune sees nothing to do and creates no orphan lock
        again = PD.prune(self.root, apply=True)
        self.assertEqual((again["pruned_records"], again["pruned_locks"], again["skipped"]), (0, 0, 0))
        self.assertEqual(sorted(p.name for p in old_acked.parent.iterdir()), [tomb.name])

    def test_apply_leaves_no_orphan_lock_when_the_record_vanished_since_the_plan(self):
        """Review round 1, minor 2: the skip path created a brand-new orphan."""
        old_acked = self._record("c", state="acked", age_seconds=8 * self.DAY)
        real_plan = PD.prune_plan
        def plan_then_vanish(root, **kwargs):
            plan = real_plan(root, **kwargs)
            old_acked.unlink()
            old_acked.with_name(old_acked.name + ".lock").unlink()
            return plan
        with unittest.mock.patch.object(PD, "prune_plan", side_effect=plan_then_vanish):
            result = PD.prune(self.root, apply=True)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(list(old_acked.parent.iterdir()), [], "no lock left behind")

    def test_record_lock_survives_the_lock_file_being_replaced(self):
        """Review round 1, M1: a holder on an unlinked inode must not share the
        critical section with a holder on the new inode."""
        import fcntl, threading
        path = self.root / "pending-delivery" / ("e" * 64) / "delivery-race.json"
        lock_path = path.with_name(path.name + ".lock")
        path.parent.mkdir(parents=True)
        lock_path.write_bytes(b"")
        old_fd = os.open(lock_path, os.O_RDWR)
        fcntl.flock(old_fd, fcntl.LOCK_EX)          # "B" holds the old inode
        os.unlink(lock_path)                        # the prune removes the file
        entered = threading.Event()
        def contender():
            with PD._record_lock(path):             # "C" opens the new inode
                entered.set()
        t = threading.Thread(target=contender); t.start()
        self.assertTrue(entered.wait(2.0), "a fresh holder acquires the new inode")
        t.join()
        # "B" itself, re-validating, would see the inode mismatch and reopen:
        self.assertNotEqual(os.fstat(old_fd).st_ino, os.stat(lock_path).st_ino)
        fcntl.flock(old_fd, fcntl.LOCK_UN); os.close(old_fd)

    def test_orphan_lock_is_unlinked_only_while_held_and_only_if_still_absent(self):
        old_lock = self._orphan_lock("delivery-held", 2 * self.DAY)
        real_plan = PD.prune_plan
        def plan_then_record_appears(root, **kwargs):
            plan = real_plan(root, **kwargs)
            old_lock.with_name(old_lock.name[:-5]).write_text("{}", encoding="utf-8")
            return plan
        with unittest.mock.patch.object(PD, "prune_plan", side_effect=plan_then_record_appears):
            result = PD.prune(self.root, apply=True)
        self.assertEqual(result["pruned_locks"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertTrue(old_lock.exists(), "a lock whose record reappeared is not an orphan")

    def test_apply_skips_a_record_rewritten_since_the_plan(self):
        old_acked = self._record("8", state="acked", age_seconds=8 * self.DAY)
        real_plan = PD.prune_plan
        def stale_plan(root, **kwargs):
            plan = real_plan(root, **kwargs)
            now = time.time()
            os.utime(old_acked, (now, now))  # rewritten between plan and apply
            return plan
        with unittest.mock.patch.object(PD, "prune_plan", side_effect=stale_plan):
            result = PD.prune(self.root, apply=True)
        self.assertEqual(result["pruned_records"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertTrue(old_acked.is_file())

    def test_orphan_locks_past_retention_are_pruned_recent_ones_kept(self):
        old_lock = self._orphan_lock("delivery-old", 2 * self.DAY)
        new_lock = self._orphan_lock("delivery-new", 3600)
        live = self._record("9", state="pending", age_seconds=2 * self.DAY)
        live_lock = live.with_name(live.name + ".lock")
        os.utime(live_lock, (time.time() - 2 * self.DAY,) * 2)
        plan = PD.prune_plan(self.root)
        self.assertEqual([item["path"] for item in plan["orphan_locks"]], [str(old_lock)])
        self.assertEqual(plan["kept_locks_recent"], 1)
        result = PD.prune(self.root, apply=True)
        self.assertEqual(result["pruned_locks"], 1)
        self.assertFalse(old_lock.exists())
        self.assertTrue(new_lock.exists())
        self.assertTrue(live_lock.exists(), "a lock with a live record is never an orphan")

    def test_cli_prune_defaults_to_dry_run_and_prints_the_table(self):
        old_acked = self._record("b", state="acked", age_seconds=8 * self.DAY)
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = PD.main(["prune", "--root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("DRY-RUN", buf.getvalue())
        self.assertIn("planned: records=1 orphan_locks=0", buf.getvalue())
        self.assertTrue(old_acked.is_file())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            PD.main(["prune", "--root", str(self.root), "--apply", "--json"])
        self.assertEqual(json.loads(buf.getvalue())["pruned_records"], 1)
        self.assertFalse(old_acked.exists())


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
