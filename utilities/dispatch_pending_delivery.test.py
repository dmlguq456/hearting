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
import subprocess
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
import dispatch_attempt_reference as AR  # noqa: E402

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
        peer = base / "peer-ledger"
        jobs = harness / "jobs.log"
        for d in (home, xdg, harness, peer):
            d.mkdir(parents=True, exist_ok=True)
        self._env_patch = {
            "HOME": str(home),
            "XDG_STATE_HOME": str(xdg),
            "XDG_CONFIG_HOME": str(base / "xdg-config"),
            "HARNESS_STATE_ROOT": str(harness),
            "AGENT_PEER_LEDGER_ROOT": str(peer),
            "AGENT_DISPATCH_JOBS": str(jobs),
            "CODEX_HOME": str(home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
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

    def test_original_failure_is_deferred_until_successor_real_ack(self):
        import dispatch_replacement as replacement
        self.root.mkdir(parents=True, exist_ok=True)
        jobs = self.root / "jobs.log"
        jobs.write_text("fixture")
        receipt = _receipt(job_registry=str(jobs))
        kwargs, original = self._create(receipt=receipt, attempt_ids=["att-old"])
        target_receipt = _receipt(job_registry=str(jobs), children=[{"attempt_id": "att-new"}])
        target_kwargs, target = self._create(receipt=target_receipt, attempt_ids=["att-new"], delivery_id="delivery-new")
        rows = {"att-new": ([], {"parent_sid": "sess-abc", "delivery_id": "delivery-new"})}
        edge = {"original_attempt_id": "att-old", "replacement_attempt_id": "att-new"}
        with unittest.mock.patch.object(replacement, "effective_attempts", side_effect=lambda jobs, attempts:
                ({"att-new"}, [edge]) if attempts == {"att-old"} else (attempts, [])), \
             unittest.mock.patch.object(replacement, "_rows", return_value=rows):
            for expected in ("pending", "sent-ambiguous"):
                if expected == "sent-ambiguous":
                    PD.claim(self.root, "sess-abc", "delivery-new", claim_owner="test", lease_seconds=30)
                    PD.mark_sent_ambiguous(self.root, "sess-abc", "delivery-new", claim_owner="test")
                with self.assertRaises(PD.PendingDeliveryError):
                    PD.claim(self.root, "sess-abc", kwargs["delivery_id"], claim_owner="sweep", lease_seconds=30)
                self.assertEqual(PD.read(self.root, "sess-abc", kwargs["delivery_id"])["state"], "pending")
            PD.ack(self.root, "sess-abc", "delivery-new", acked_by="recipient-inference")
            with self.assertRaises(PD.PendingDeliveryError):
                PD.claim(self.root, "sess-abc", kwargs["delivery_id"], claim_owner="sweep", lease_seconds=30)
        retired = PD.read(self.root, "sess-abc", kwargs["delivery_id"])
        self.assertEqual((retired["state"], retired["expiry_reason"]), ("rejected", "replacement-consumed"))
        self.assertIsNone(retired["acked_at_ns"])
        self.assertEqual(retired["receipt"], original["receipt"])

    def test_blank_registry_uses_only_valid_carrier_context_without_rewriting_receipt(self):
        import dispatch_replacement as replacement
        self.root.mkdir(parents=True, exist_ok=True)
        jobs = self.root / "jobs.log"
        jobs.write_text("")
        kwargs, record = self._create(receipt=_receipt(job_registry=""))
        path = PD.record_path(self.root, kwargs["recipient_key"], kwargs["delivery_id"])
        sealed = path.read_bytes()
        outside = Path(self._tmp.name) / "outside" / "jobs.log"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("")
        symlink = self.root / "jobs-link.log"
        symlink.symlink_to(jobs)
        with unittest.mock.patch.object(replacement, "_rows", return_value={}), \
             unittest.mock.patch.object(replacement, "effective_attempts",
                                        side_effect=lambda _jobs, attempts: (attempts, [])), \
             unittest.mock.patch.object(replacement, "recovery_delivery_state", return_value="accepted"):
            self.assertEqual(PD._replacement_disposition(
                self.root, kwargs["recipient_key"], record), "")
            self.assertEqual(PD._replacement_disposition(
                self.root, kwargs["recipient_key"], record, jobs=jobs), "defer")
            self.assertEqual(PD._replacement_disposition(
                self.root, kwargs["recipient_key"], record, jobs=outside), "defer")
            self.assertEqual(PD._replacement_disposition(
                self.root, kwargs["recipient_key"], record, jobs=symlink), "defer")
        self.assertEqual(path.read_bytes(), sealed)

    def test_human_gate_is_not_deferred_with_its_replaced_attempt(self):
        import dispatch_replacement as replacement
        self.root.mkdir(parents=True, exist_ok=True)
        jobs = self.root / "jobs.log"
        jobs.write_text("")
        receipt = _receipt(job_registry=str(jobs), children=[{"attempt_id": "att-old", "required_action": "human-gate:answer"}])
        kwargs, _ = self._create(receipt=receipt, attempt_ids=["att-old"])
        with unittest.mock.patch.object(replacement, "effective_attempts", side_effect=AssertionError("gates never use replacement mapping")):
            claimed = PD.claim(self.root, "sess-abc", kwargs["delivery_id"], claim_owner="sweep", lease_seconds=30)
        self.assertEqual(claimed["state"], "claimed")

    def test_gateway_consumption_requires_durable_accept_and_replays_after_restart(self):
        import dispatch_replacement as replacement
        self.root.mkdir(parents=True, exist_ok=True)
        jobs = self.root / "jobs.log"
        jobs.write_text("")
        receipt = _receipt(job_registry=str(jobs), children=[{"attempt_id": "att-new"}],
            replacement_lineage=[{"original_attempt_id": "att-old", "replacement_attempt_id": "att-new"}])
        ledger = self.root / "gateway.json"
        import hashlib
        digest = "sha256:" + hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        value = {"deliveries": {"dlv-proof": {"state": "sent", "thread_id": "sess-abc", "receipt_digest": digest}}}
        ledger.write_text(json.dumps(value))
        with unittest.mock.patch.object(replacement, "adopt_receipt", return_value=({"att-new"}, {"att-old"})):
            with self.assertRaises(ValueError):
                PD.record_replacement_consumption(self.root, "sess-abc", receipt, ledger_path=ledger, delivery_id="dlv-proof")
            value["deliveries"]["dlv-proof"]["state"] = "accepted"
            ledger.write_text(json.dumps(value))
            PD.record_replacement_consumption(self.root, "sess-abc", receipt, ledger_path=ledger, delivery_id="dlv-proof")
            PD.record_replacement_consumption(self.root, "sess-abc", receipt, ledger_path=ledger, delivery_id="dlv-proof")
            self.assertTrue(PD._gateway_consumed(self.root, "sess-abc", "att-old"))
            self.assertFalse(PD._gateway_consumed(self.root, "foreign", "att-old"))
            value["deliveries"]["dlv-proof"]["state"] = "sent"
            ledger.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                PD._gateway_consumed(self.root, "sess-abc", "att-old")

    def test_create_persists_schema_v1_record(self):
        kwargs, record = self._create()
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["receipt"], kwargs["receipt"])
        self.assertEqual(set(record), set(PD.REQUIRED_FIELDS) | {PD.TIMESTAMP_PROVENANCE_FIELD})
        self.assertIsNotNone(record[PD.TIMESTAMP_PROVENANCE_FIELD]["record_materialized_at_utc"])
        path = PD.record_path(self.root, kwargs["recipient_key"], kwargs["delivery_id"])
        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(persisted), set(PD.REQUIRED_FIELDS))
        self.assertNotIn(PD.TIMESTAMP_PROVENANCE_FIELD, persisted)
        timeline_path = PD.courier_timeline_path(path)
        self.assertEqual(
            json.loads(timeline_path.read_text(encoding="utf-8"))[
                PD.TIMESTAMP_PROVENANCE_FIELD
            ],
            record[PD.TIMESTAMP_PROVENANCE_FIELD],
        )
        self.assertFalse(PD.courier_retry_path(path).exists())

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
        legacy.pop(PD.TIMESTAMP_PROVENANCE_FIELD, None)
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
        legacy.pop(PD.TIMESTAMP_PROVENANCE_FIELD, None)
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

    def test_positive_batch_hold_refuses_before_claim_or_retry_sidecar_write(self):
        delivery_id = self.kwargs["delivery_id"]
        record_path = PD.record_path(self.root, "sess-claim", delivery_id)
        sidecar = PD.courier_retry_path(record_path)
        jobs = self.root / "jobs.log"
        jobs.write_text("")
        record = PD.read(self.root, "sess-claim", delivery_id)
        info = jobs.stat()
        batch = {"delivery_ids": (delivery_id, "delivery-sibling"), "held": True,
                 "attempt_ids": ("att-child", "att-sibling"), "registry_path": str(jobs),
                 "registry_signature": (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns),
                 "seed_identity": (record["delivery_id"], record["receipt_digest"],
                                   tuple(record["attempt_ids"]), record["recipient_digest"])}
        with unittest.mock.patch("dispatch_notice_state.resolve_delivery_batch", return_value=batch):
            with self.assertRaises(PD.PendingDeliveryError) as caught:
                PD.claim(self.root, "sess-claim", delivery_id, claim_owner="held",
                         lease_seconds=30, jobs=jobs)
        self.assertEqual((caught.exception.reason, caught.exception.detail),
                         ("pending-delivery-claim-refused", "batch-hold"))
        self.assertEqual(PD.read(self.root, "sess-claim", delivery_id)["attempts"], 0)
        self.assertEqual(PD.read(self.root, "sess-claim", delivery_id)["state"], "pending")
        self.assertFalse(sidecar.exists())

    def _ready_group(self, sibling_id):
        seed = PD.read(self.root, "sess-claim", self.kwargs["delivery_id"])
        jobs = self.root / "jobs.log"
        jobs.write_text("")
        info = jobs.stat()
        return jobs, {"delivery_ids": tuple(sorted((self.kwargs["delivery_id"], sibling_id))),
                      "held": False, "registry_path": str(jobs),
                      "registry_signature": (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns),
                      "seed_identity": (seed["delivery_id"], seed["receipt_digest"],
                                        tuple(seed["attempt_ids"]), seed["recipient_digest"])}

    def test_ready_scalar_batch_claim_has_distinct_group_required_refusal(self):
        sibling_id = "delivery-" + "c" * 32
        jobs, batch = self._ready_group(sibling_id)
        with unittest.mock.patch("dispatch_notice_state.resolve_delivery_batch", return_value=batch):
            with self.assertRaises(PD.PendingDeliveryError) as caught:
                PD.claim(self.root, "sess-claim", self.kwargs["delivery_id"],
                         claim_owner="scalar", lease_seconds=30, jobs=jobs)
        self.assertEqual((caught.exception.reason, caught.exception.detail),
                         ("pending-delivery-claim-refused", "batch-group-claim-required"))

    def test_ready_batch_skips_already_terminal_member(self):
        sibling_id = "delivery-" + "c" * 32
        sibling_args = dict(self.kwargs, delivery_id=sibling_id,
                            attempt_ids=["att-0000000000000000000000000000cccc"],
                            row_revisions={"att-0000000000000000000000000000cccc": "cafe"})
        sibling = PD.create(**sibling_args)
        PD.claim(self.root, "sess-claim", sibling_id, claim_owner="prior", lease_seconds=30)
        PD.ack(self.root, "sess-claim", sibling_id, acked_by="prior-recipient")
        jobs, batch = self._ready_group(sibling_id)
        with unittest.mock.patch("dispatch_notice_state.resolve_delivery_batch", return_value=batch), \
             unittest.mock.patch.object(PD, "_replacement_disposition", return_value=""):
            claimed = PD.claim_ready_batch(
                self.root, "sess-claim", self.kwargs["delivery_id"], claim_owner="group",
                lease_seconds=30, jobs=jobs)
        self.assertEqual([record["delivery_id"] for record in claimed],
                         [self.kwargs["delivery_id"]])
        self.assertEqual(PD.read(self.root, "sess-claim", sibling_id)["state"], "acked")

    def test_ready_batch_does_not_reclaim_another_owners_live_lease(self):
        sibling_id = "delivery-" + "c" * 32
        sibling_args = dict(self.kwargs, delivery_id=sibling_id,
                            attempt_ids=["att-0000000000000000000000000000cccc"],
                            row_revisions={"att-0000000000000000000000000000cccc": "cafe"})
        PD.create(**sibling_args)
        foreign = PD.claim(self.root, "sess-claim", sibling_id, claim_owner="other-owner",
                           lease_seconds=60)
        jobs, batch = self._ready_group(sibling_id)
        with unittest.mock.patch("dispatch_notice_state.resolve_delivery_batch", return_value=batch), \
             unittest.mock.patch.object(PD, "_replacement_disposition", return_value=""):
            with self.assertRaises(PD.PendingDeliveryError) as caught:
                PD.claim_ready_batch(
                    self.root, "sess-claim", self.kwargs["delivery_id"], claim_owner="group",
                    lease_seconds=30, jobs=jobs)
        self.assertEqual(caught.exception.detail, "batch-owned-elsewhere")
        self.assertEqual(PD.read(self.root, "sess-claim", self.kwargs["delivery_id"])["state"],
                         "pending")
        self.assertEqual(PD.read(self.root, "sess-claim", sibling_id)["claim_owner"],
                         foreign["claim_owner"])

    def _retry_state(self):
        path = PD.record_path(self.root, "sess-claim", self.kwargs["delivery_id"])
        metadata = json.loads(PD.courier_retry_path(path).read_text(encoding="utf-8"))
        return metadata["retry"]

    def test_public_records_match_legacy_json_while_retry_sidecar_defers_claim(self):
        did = self.kwargs["delivery_id"]
        path = PD.record_path(self.root, "sess-claim", did)
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn(PD.TIMESTAMP_PROVENANCE_FIELD, raw)
        self.assertEqual(PD.read(self.root, "sess-claim", did)[PD.TIMESTAMP_PROVENANCE_FIELD],
                         PD.create(**self.kwargs)[PD.TIMESTAMP_PROVENANCE_FIELD])

        now = time.time_ns()
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=now):
            claimed = PD.claim(
                self.root, "sess-claim", did, claim_owner="first", lease_seconds=30,
                retry_context="target-a", retry_base_delay_seconds=2,
            )
        raw_claimed = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn(PD.TIMESTAMP_PROVENANCE_FIELD, raw_claimed)
        self.assertEqual(claimed[PD.TIMESTAMP_PROVENANCE_FIELD],
                         PD.read(self.root, "sess-claim", did)[PD.TIMESTAMP_PROVENANCE_FIELD])
        self.assertNotIn("courier_retry", claimed)
        self.assertEqual(PD.read(self.root, "sess-claim", did)["state"], raw_claimed["state"])

        with unittest.mock.patch.object(PD.time, "time_ns", return_value=now):
            released = PD.release_claim(
                self.root, "sess-claim", did, claim_owner="first",
                failure_reason="transport-timeout", retry_base_delay_seconds=2,
            )
        raw_released = json.loads(path.read_text(encoding="utf-8"))
        released_record = dict(released)
        self.assertIn(PD.TIMESTAMP_PROVENANCE_FIELD, released_record)
        released_record.pop(PD.TIMESTAMP_PROVENANCE_FIELD)
        self.assertEqual(released_record, raw_released)
        self.assertNotIn("courier_retry", released)
        self.assertEqual(PD.create(**self.kwargs), released)
        self.assertEqual(PD.read(self.root, "sess-claim", did), released)

        retry = self._retry_state()
        self.assertEqual(retry["failure_count"], 1)
        self.assertEqual(retry["next_attempt_at_ns"], now + retry["delay_ns"])
        self.assertLessEqual(retry["delay_ns"], 15_000_000_000)
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=now + 1_000_000):
            with self.assertRaises(PD.PendingDeliveryError) as caught:
                PD.claim(
                    self.root, "sess-claim", did, claim_owner="duplicate", lease_seconds=30,
                    retry_context="target-a", retry_base_delay_seconds=2,
                )
        self.assertEqual(caught.exception.reason, "pending-delivery-retry-deferred")
        self.assertEqual(PD.read(self.root, "sess-claim", did)["state"], raw_released["state"])
        self.assertEqual(PD.read(self.root, "sess-claim", did)[PD.TIMESTAMP_PROVENANCE_FIELD],
                         released[PD.TIMESTAMP_PROVENANCE_FIELD])
        self.assertEqual(self._retry_state(), retry)

    def test_owner_release_persists_a_retry_deadline(self):
        did = self.kwargs["delivery_id"]
        now = time.time_ns()
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=now):
            PD.claim(self.root, "sess-claim", did, claim_owner="queue", lease_seconds=86400)
            result = PD.release_claim(self.root, "sess-claim", did, claim_owner="queue")
        self.assertEqual(result["state"], "pending")
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=now + 1_000_000_001):
            PD.claim(self.root, "sess-claim", did, claim_owner="prompt", lease_seconds=30)
        result = PD.release_claim(self.root, "sess-claim", did, claim_owner="queue")
        self.assertEqual(result["claim_owner"], "prompt")
        PD.ack(self.root, "sess-claim", did, acked_by="prompt")
        self.assertEqual(PD.release_claim(self.root, "sess-claim", did, claim_owner="prompt")["state"], "acked")

    def test_same_failure_backoff_survives_recall_and_restart_until_due(self):
        did = self.kwargs["delivery_id"]
        now = time.time_ns()
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=now):
            PD.claim(self.root, "sess-claim", did, claim_owner="first", lease_seconds=30,
                     retry_context="target-a", retry_base_delay_seconds=0.05)
            first = PD.release_claim(
                self.root, "sess-claim", did, claim_owner="first",
                failure_reason="transport-timeout", retry_base_delay_seconds=0.05)
        self.assertNotIn("courier_retry", first)
        retry = self._retry_state()
        self.assertEqual(retry["failure_count"], 1)
        self.assertEqual(retry["next_attempt_at_ns"], now + retry["delay_ns"])
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=now + 10_000_000):
            with self.assertRaises(PD.PendingDeliveryError) as caught:
                PD.claim(self.root, "sess-claim", did, claim_owner="recalled", lease_seconds=30,
                         retry_context="target-a", retry_base_delay_seconds=0.05)
        self.assertEqual(caught.exception.reason, "pending-delivery-retry-deferred")
        self.assertEqual(PD.read(self.root, "sess-claim", did)["attempts"], 1)
        due = retry["next_attempt_at_ns"] + 1
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=due):
            PD.claim(self.root, "sess-claim", did, claim_owner="restarted", lease_seconds=30,
                     retry_context="target-a", retry_base_delay_seconds=0.05)
            second = PD.release_claim(
                self.root, "sess-claim", did, claim_owner="restarted",
                failure_reason="transport-timeout", retry_base_delay_seconds=0.05)
        second_retry = self._retry_state()
        self.assertEqual(second_retry["failure_count"], 2)
        self.assertGreater(second_retry["delay_ns"], retry["delay_ns"])
        self.assertNotIn("courier_retry", second)
        self.assertEqual(second["attempts"], 2)

    def test_send_timestamps_coexist_with_retry_sidecar_until_ack(self):
        did = self.kwargs["delivery_id"]
        now = time.time_ns()
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=now):
            PD.claim(self.root, "sess-claim", did, claim_owner="first", lease_seconds=30,
                     retry_context="target-a", retry_base_delay_seconds=0.05)
            PD.release_claim(
                self.root, "sess-claim", did, claim_owner="first",
                failure_reason="transport-timeout", retry_base_delay_seconds=0.05)

        path = PD.record_path(self.root, "sess-claim", did)
        retry_path = PD.courier_retry_path(path)
        timeline_path = PD.courier_timeline_path(path)
        retry_before = json.loads(retry_path.read_text(encoding="utf-8"))["retry"]
        due = retry_before["next_attempt_at_ns"] + 1
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=due):
            PD.claim(self.root, "sess-claim", did, claim_owner="retry", lease_seconds=30,
                     retry_context="target-a", retry_base_delay_seconds=0.05)
            sent = PD.record_send(
                self.root, "sess-claim", did, source="native-queue-accepted",
                event_id="client-item-after-retry")
            repeated = PD.record_send(
                self.root, "sess-claim", did, source="native-queue-accepted",
                event_id="client-item-after-retry")
            self.assertEqual(sent[PD.TIMESTAMP_PROVENANCE_FIELD]["observed_send_count"], 1)
            self.assertEqual(repeated[PD.TIMESTAMP_PROVENANCE_FIELD],
                             sent[PD.TIMESTAMP_PROVENANCE_FIELD])
            self.assertEqual(
                json.loads(retry_path.read_text(encoding="utf-8"))["retry"], retry_before)
            self.assertNotIn(PD.TIMESTAMP_PROVENANCE_FIELD,
                             json.loads(retry_path.read_text(encoding="utf-8")))
            acked = PD.ack(self.root, "sess-claim", did, acked_by="native-queue-consumed")

        self.assertEqual(acked["state"], "acked")
        self.assertEqual(acked[PD.TIMESTAMP_PROVENANCE_FIELD]["observed_send_count"], 1)
        self.assertEqual(acked[PD.TIMESTAMP_PROVENANCE_FIELD]["receive_source"],
                         "native-queue-consumed")
        self.assertFalse(retry_path.exists())
        self.assertTrue(timeline_path.exists())
        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("courier_retry", persisted)
        self.assertNotIn(PD.TIMESTAMP_PROVENANCE_FIELD, persisted)
        metadata = json.loads(timeline_path.read_text(encoding="utf-8"))
        self.assertEqual(set(metadata), {
            "schema_version", "delivery_id", "recipient_digest", "identity_digest",
            PD.TIMESTAMP_PROVENANCE_FIELD,
        })
        self.assertEqual(metadata[PD.TIMESTAMP_PROVENANCE_FIELD],
                         acked[PD.TIMESTAMP_PROVENANCE_FIELD])
        self.assertEqual(PD.read(self.root, "sess-claim", did)[PD.TIMESTAMP_PROVENANCE_FIELD],
                         acked[PD.TIMESTAMP_PROVENANCE_FIELD])

    def test_changed_delivery_context_and_clock_rollback_clear_stale_wait(self):
        did = self.kwargs["delivery_id"]
        now = time.time_ns()
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=now):
            PD.claim(self.root, "sess-claim", did, claim_owner="first", lease_seconds=30,
                     retry_context="target-a", retry_base_delay_seconds=0.05)
            PD.release_claim(self.root, "sess-claim", did, claim_owner="first",
                             failure_reason="transport-timeout", retry_base_delay_seconds=0.05)
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=now + 1_000_000):
            changed = PD.claim(self.root, "sess-claim", did, claim_owner="target-change",
                               lease_seconds=30, retry_context="target-b",
                               retry_base_delay_seconds=0.05)
        self.assertNotIn("courier_retry", changed)
        self.assertEqual(self._retry_state()["failure_count"], 0)
        PD.release_claim(self.root, "sess-claim", did, claim_owner="target-change",
                         failure_reason="transport-timeout", retry_base_delay_seconds=0.05)
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=now - 1_000_000_000):
            recovered = PD.claim(self.root, "sess-claim", did, claim_owner="clock-recovered",
                                 lease_seconds=30, retry_context="target-b",
                                 retry_base_delay_seconds=0.05)
        self.assertNotIn("courier_retry", recovered)
        self.assertEqual(self._retry_state()["failure_count"], 0)

    def _exercise_previous_reader_after_send(self, sealed, module_name):
        source = subprocess.run(
            ["git", "show", f"{sealed}:utilities/dispatch_pending_delivery.py"],
            cwd=HERE.parent, check=True, capture_output=True, text=True,
            env={**os.environ, "GIT_CONFIG_COUNT": "1",
                 "GIT_CONFIG_KEY_0": "safe.directory",
                 "GIT_CONFIG_VALUE_0": str(HERE.parent)},
        ).stdout
        old_path = Path(self._tmp.name) / f"{module_name}.py"
        old_path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(module_name, old_path)
        old = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[module_name] = old
        self.addCleanup(lambda: sys.modules.pop(module_name, None))
        spec.loader.exec_module(old)

        did = self.kwargs["delivery_id"]
        path = PD.record_path(self.root, "sess-claim", did)
        initial_timeline = PD.read(self.root, "sess-claim", did)[PD.TIMESTAMP_PROVENANCE_FIELD]
        now = time.time_ns()
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=now):
            PD.claim(self.root, "sess-claim", did, claim_owner="new-writer",
                     lease_seconds=30, retry_context="recipient:generation:target",
                     retry_base_delay_seconds=0.05)
            released = PD.release_claim(
                self.root, "sess-claim", did, claim_owner="new-writer",
                failure_reason="transport-timeout", retry_base_delay_seconds=0.05,
            )
        self.assertEqual(released["state"], "pending")
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("courier_retry", raw)
        self.assertNotIn(PD.TIMESTAMP_PROVENANCE_FIELD, raw)
        retry_path = PD.courier_retry_path(path)
        timeline_path = PD.courier_timeline_path(path)
        retry_metadata = json.loads(retry_path.read_text(encoding="utf-8"))
        self.assertEqual(set(retry_metadata), {
            "schema_version", "delivery_id", "recipient_digest", "identity_digest", "retry",
        })
        self.assertTrue(timeline_path.is_file())
        self.assertEqual(old._validate_record(raw)["state"], "pending")

        retry = retry_metadata["retry"]
        due = retry["next_attempt_at_ns"] + 1
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=due):
            sent = PD.record_send(
                self.root, "sess-claim", did, source="native-queue-accepted",
                event_id="legacy-reader-item")
            repeated = PD.record_send(
                self.root, "sess-claim", did, source="native-queue-accepted",
                event_id="legacy-reader-item")
        self.assertEqual(sent[PD.TIMESTAMP_PROVENANCE_FIELD]["observed_send_count"], 1)
        self.assertEqual(
            repeated[PD.TIMESTAMP_PROVENANCE_FIELD], sent[PD.TIMESTAMP_PROVENANCE_FIELD])
        self.assertEqual(sent[PD.TIMESTAMP_PROVENANCE_FIELD]["record_materialized_at_utc"],
                         initial_timeline["record_materialized_at_utc"])
        self.assertEqual(json.loads(retry_path.read_text(encoding="utf-8")), retry_metadata)

        # Exercise the actual historical reader's read/claim/consume implementation.
        self.assertEqual(old.read(self.root, "sess-claim", did)["state"], "pending")
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=due + 1):
            old.claim(self.root, "sess-claim", did, claim_owner="old-reader", lease_seconds=30)
        consumed = old.ack(self.root, "sess-claim", did, acked_by="old-recipient-consumed")
        self.assertEqual(consumed["state"], "acked")
        if sealed.startswith("7f9"):
            self.assertFalse(retry_path.exists())
        self.assertTrue(timeline_path.exists())
        read_back = PD.read(self.root, "sess-claim", did)
        self.assertEqual(read_back["state"], "acked")
        timeline = read_back[PD.TIMESTAMP_PROVENANCE_FIELD]
        sent_timeline = sent[PD.TIMESTAMP_PROVENANCE_FIELD]
        self.assertEqual(timeline["record_materialized_at_utc"],
                         initial_timeline["record_materialized_at_utc"])
        self.assertEqual(timeline["first_send_at_utc"], sent_timeline["first_send_at_utc"])
        self.assertEqual(timeline["last_send_at_utc"], sent_timeline["last_send_at_utc"])
        self.assertEqual(timeline["observed_send_count"], 1)
        self.assertEqual(timeline["last_send_event_id"], "legacy-reader-item")
        self.assertIsNone(timeline["receive_at_utc"])
        self.assertIsNone(timeline["receive_source"])
        self.assertNotIn(PD.TIMESTAMP_PROVENANCE_FIELD,
                         json.loads(path.read_text(encoding="utf-8")))

    def test_sealed_base_reader_accepts_new_writer_failed_obligation_and_consumes_it(self):
        self._exercise_previous_reader_after_send(
            "48c08f3231f35c9ef24731ccda5070b6d744c48e",
            "sealed_base_dispatch_pending_delivery_fixture",
        )

    def test_retry_aware_main_reader_accepts_new_writer_and_preserves_timeline(self):
        self._exercise_previous_reader_after_send(
            "7f9a5d5d1a1574c9f2a52931932258e9e53677b0",
            "retry_aware_dispatch_pending_delivery_fixture",
        )

    def test_timestamp_metadata_is_identity_bound_and_retained_until_terminal_prune(self):
        did = self.kwargs["delivery_id"]
        path = PD.record_path(self.root, "sess-claim", did)
        metadata_path = PD.courier_timeline_path(path)
        original_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

        other_id = "delivery-" + "c" * 32
        other_kwargs = dict(self.kwargs, delivery_id=other_id)
        PD.create(**other_kwargs)
        other_path = PD.record_path(self.root, "sess-claim", other_id)
        other_metadata_path = PD.courier_timeline_path(other_path)
        other_metadata_path.write_text(json.dumps(original_metadata), encoding="utf-8")
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            PD.read(self.root, "sess-claim", other_id)
        self.assertEqual(ctx.exception.detail, "courier-timeline-identity-invalid")

        PD.claim(self.root, "sess-claim", did, claim_owner="terminal", lease_seconds=30)
        PD.ack(self.root, "sess-claim", did, acked_by="recipient-consumed")
        terminal_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertFalse(PD.courier_retry_path(path).exists())
        self.assertEqual(PD.read(self.root, "sess-claim", did)[PD.TIMESTAMP_PROVENANCE_FIELD],
                         terminal_metadata[PD.TIMESTAMP_PROVENANCE_FIELD])

        old = time.time() - 10
        os.utime(path, (old, old))
        pruned = PD.prune(self.root, apply=True, now=time.time(), retention_seconds=1)
        self.assertEqual(pruned["pruned_records"], 1)
        self.assertFalse(path.exists())
        self.assertFalse(metadata_path.exists())
        self.assertFalse(PD.courier_retry_path(path).exists())

    def test_retry_sidecar_survives_record_write_failure_without_acknowledging(self):
        did = self.kwargs["delivery_id"]
        path = PD.record_path(self.root, "sess-claim", did)
        now = time.time_ns()
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=now):
            PD.claim(self.root, "sess-claim", did, claim_owner="carrier", lease_seconds=30,
                     retry_context="stable-target")

        real_replace = PD.os.replace

        def fail_record_replace(source, destination):
            if Path(destination) == path:
                raise OSError("simulated record CAS write failure")
            return real_replace(source, destination)

        with unittest.mock.patch.object(PD.os, "replace", side_effect=fail_record_replace):
            with self.assertRaises(PD.PendingDeliveryError):
                PD.release_claim(
                    self.root, "sess-claim", did, claim_owner="carrier",
                    failure_reason="transport-timeout", retry_base_delay_seconds=2,
                )

        record = PD.read(self.root, "sess-claim", did)
        self.assertEqual(record["state"], "claimed")
        self.assertEqual(record["claim_owner"], "carrier")
        metadata = json.loads(PD.courier_retry_path(path).read_text(encoding="utf-8"))
        self.assertEqual(metadata["retry"]["failure_count"], 1)
        self.assertGreater(metadata["retry"]["next_attempt_at_ns"], now)
        self.assertNotEqual(record["state"], "acked")

    def _claim_with_live_retry(self):
        did = self.kwargs["delivery_id"]
        path = PD.record_path(self.root, "sess-claim", did)
        retry_path = PD.courier_retry_path(path)
        timeline_path = PD.courier_timeline_path(path)
        failed_at = time.time_ns()
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=failed_at):
            PD.claim(self.root, "sess-claim", did, claim_owner="first", lease_seconds=30)
            PD.release_claim(
                self.root, "sess-claim", did, claim_owner="first",
                failure_reason="transport-timeout", retry_base_delay_seconds=2,
            )
        retry_bytes = retry_path.read_bytes()
        retry_metadata = json.loads(retry_bytes)
        retry = retry_metadata["retry"]
        with unittest.mock.patch.object(
                PD.time, "time_ns", return_value=retry["next_attempt_at_ns"] + 1):
            claimed = PD.claim(
                self.root, "sess-claim", did, claim_owner="terminal", lease_seconds=30,
                retry_base_delay_seconds=2,
            )
        return path, retry_path, timeline_path, claimed, retry_bytes, retry

    def test_terminal_ack_record_write_failure_preserves_retry_until_commit(self):
        did = self.kwargs["delivery_id"]
        path, retry_path, timeline_path, claimed, retry_bytes, retry = \
            self._claim_with_live_retry()
        self.assertGreater(retry["failure_count"], 0)
        self.assertGreater(retry["next_attempt_at_ns"], retry["last_failure_at_ns"])
        record_bytes = path.read_bytes()

        real_replace = PD.os.replace

        def fail_record_replace(source, destination):
            if Path(destination) == path:
                raise OSError("simulated terminal record CAS write failure")
            return real_replace(source, destination)

        with unittest.mock.patch.object(PD.os, "replace", side_effect=fail_record_replace):
            with self.assertRaises(PD.PendingDeliveryError):
                PD.ack(self.root, "sess-claim", did, acked_by="recipient-consumed")

        self.assertEqual(path.read_bytes(), record_bytes)
        self.assertEqual(PD.read(self.root, "sess-claim", did)["state"], "claimed")
        self.assertEqual(retry_path.read_bytes(), retry_bytes)

        PD.reclaim(self.root, "sess-claim", did, now_ns=claimed["claim_deadline_ns"])
        self.assertEqual(retry_path.read_bytes(), retry_bytes)
        early = retry["last_failure_at_ns"] + (
            retry["next_attempt_at_ns"] - retry["last_failure_at_ns"]
        ) // 2
        with unittest.mock.patch.object(PD.time, "time_ns", return_value=early):
            with self.assertRaises(PD.PendingDeliveryError) as caught:
                PD.claim(self.root, "sess-claim", did, claim_owner="too-early", lease_seconds=30)
        self.assertEqual(caught.exception.reason, "pending-delivery-retry-deferred")

        with unittest.mock.patch.object(
                PD.time, "time_ns", return_value=retry["next_attempt_at_ns"] + 1):
            PD.claim(self.root, "sess-claim", did, claim_owner="retry", lease_seconds=30,
                     retry_base_delay_seconds=2)
            acked = PD.ack(self.root, "sess-claim", did, acked_by="recipient-consumed")
        self.assertEqual(acked["state"], "acked")
        self.assertFalse(retry_path.exists())
        self.assertTrue(timeline_path.is_file())
        self.assertEqual(PD.read(self.root, "sess-claim", did)[PD.TIMESTAMP_PROVENANCE_FIELD],
                         json.loads(timeline_path.read_text(encoding="utf-8"))[
                             PD.TIMESTAMP_PROVENANCE_FIELD])

    def test_terminal_retry_cleanup_failure_keeps_committed_ack(self):
        did = self.kwargs["delivery_id"]
        path, retry_path, timeline_path, _claimed, _retry_bytes, _retry = \
            self._claim_with_live_retry()
        real_unlink = Path.unlink

        def fail_retry_unlink(target, *args, **kwargs):
            if target == retry_path:
                raise OSError("simulated retry cleanup failure")
            return real_unlink(target, *args, **kwargs)

        with unittest.mock.patch.object(Path, "unlink", fail_retry_unlink):
            with self.assertRaises(PD.PendingDeliveryError):
                PD.ack(self.root, "sess-claim", did, acked_by="recipient-consumed")

        self.assertEqual(PD.read(self.root, "sess-claim", did)["state"], "acked")
        self.assertTrue(retry_path.is_file())
        self.assertTrue(timeline_path.is_file())
        with self.assertRaises(PD.PendingDeliveryError):
            PD.claim(self.root, "sess-claim", did, claim_owner="must-not-redeliver",
                     lease_seconds=30)


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
        self.assertIsNotNone(acked[PD.TIMESTAMP_PROVENANCE_FIELD]["receive_at_utc"])

    def test_timestamp_provenance_is_observational_idempotent_and_keeps_legacy_unknown(self):
        kwargs = dict(self.kwargs)
        did = kwargs["delivery_id"]
        identity = PD.received_ack_identity(PD.read(self.root, "sess-claim", did), "sess-claim", did)
        PD.claim(self.root, "sess-claim", did, claim_owner="time-test", lease_seconds=30)
        first = PD.record_send(self.root, "sess-claim", did, source="native-queue-accepted",
                               event_id="client-item-1")
        again = PD.record_send(self.root, "sess-claim", did, source="native-queue-accepted",
                               event_id="client-item-1")
        second = PD.record_send(self.root, "sess-claim", did, source="native-queue-accepted",
                                event_id="client-item-2")
        timeline = second[PD.TIMESTAMP_PROVENANCE_FIELD]
        self.assertEqual(first, again)
        self.assertIsNotNone(timeline["first_send_at_utc"])
        self.assertEqual(timeline["observed_send_count"], 2)
        self.assertEqual(timeline["observed_redelivery_count"], 1)
        self.assertEqual(PD.received_ack_identity(second, "sess-claim", did), identity)

        legacy_kwargs = dict(self.kwargs, delivery_id="delivery-" + "c" * 32)
        legacy_record = PD.create(**legacy_kwargs)
        legacy_path = PD.record_path(self.root, "sess-claim", legacy_kwargs["delivery_id"])
        legacy = dict(legacy_record)
        legacy.pop(PD.TIMESTAMP_PROVENANCE_FIELD)
        legacy.pop("claim_authority")
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
        PD.courier_timeline_path(legacy_path).unlink()
        PD.claim(self.root, "sess-claim", legacy_kwargs["delivery_id"],
                 claim_owner="legacy-time", lease_seconds=30)
        observed = PD.record_send(self.root, "sess-claim", legacy_kwargs["delivery_id"],
                                  source="native-queue-accepted", event_id="legacy-retry")
        old_timeline = observed[PD.TIMESTAMP_PROVENANCE_FIELD]
        self.assertIsNone(old_timeline["record_materialized_at_utc"])
        self.assertIsNone(old_timeline["first_send_at_utc"])
        self.assertIsNotNone(old_timeline["last_send_at_utc"])

    def test_submission_fence_survives_acceptance_before_timing_and_claim_recovery(self):
        did = self.kwargs["delivery_id"]
        public_before = PD.read(self.root, "sess-claim", did)
        self.assertNotIn(PD.SUBMISSION_FENCE_FIELD, public_before)
        self.assertIsNone(PD.read_submission_fence(
            self.root, "sess-claim", did,
            expected_identity=PD.submission_fence_identity(public_before, "thread-exact")))

        claimed = PD.claim(self.root, "sess-claim", did, claim_owner="notice-first",
                           lease_seconds=1)
        identity = PD.submission_fence_identity(claimed, "thread-exact")
        prepared = PD.prepare_submission_fence(
            self.root, "sess-claim", did, claim_owner="notice-first",
            expected_identity=identity)
        self.assertEqual(prepared["state"], "prepared")
        self.assertEqual(prepared["attempt"], 1)

        accepted = PD.record_submission_accepted(
            self.root, "sess-claim", did, claim_owner="notice-first",
            expected_identity=identity, expected_attempt=1,
            queue_submission_id="queue-item-accepted")
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(accepted["queue_submission_id"], "queue-item-accepted")
        path = PD.record_path(self.root, "sess-claim", did)
        self.assertEqual(json.loads(path.read_text())[PD.SUBMISSION_FENCE_FIELD], accepted)
        self.assertNotIn(PD.SUBMISSION_FENCE_FIELD,
                         PD.read(self.root, "sess-claim", did))
        self.assertEqual(PD.read(self.root, "sess-claim", did)[
            PD.TIMESTAMP_PROVENANCE_FIELD]["observed_send_count"], 0)

        ambiguous = PD.mark_sent_ambiguous(
            self.root, "sess-claim", did, claim_owner="notice-first")
        self.assertIsNone(ambiguous["acked_at_ns"])
        PD.reclaim(self.root, "sess-claim", did,
                   now_ns=ambiguous["claim_deadline_ns"] + 1)
        successor = PD.claim(self.root, "sess-claim", did,
                             claim_owner="notice-restart", lease_seconds=30)
        observed = PD.read_submission_fence(
            self.root, "sess-claim", did, expected_identity=identity)
        self.assertEqual(observed["state"], "accepted")
        self.assertEqual(observed["attempt"], 1)
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            PD.prepare_submission_fence(
                self.root, "sess-claim", did, claim_owner=successor["claim_owner"],
                expected_identity=identity)
        self.assertEqual(ctx.exception.detail, "submission-already-fenced")
        self.assertIsNone(successor["acked_at_ns"])

    def test_submission_fence_retries_only_after_explicit_add_nonacceptance(self):
        did = self.kwargs["delivery_id"]
        claimed = PD.claim(self.root, "sess-claim", did, claim_owner="notice-first",
                           lease_seconds=30, retry_context="notice-target")
        identity = PD.submission_fence_identity(claimed, "thread-exact")
        first = PD.prepare_submission_fence(
            self.root, "sess-claim", did, claim_owner="notice-first",
            expected_identity=identity)
        reason = "queue-rpc-refused:thread/queue/add:-32600:invalid request"
        refused = PD.record_submission_not_accepted(
            self.root, "sess-claim", did, claim_owner="notice-first",
            expected_identity=identity, expected_attempt=first["attempt"], reason=reason)
        self.assertEqual(refused["state"], "not-accepted")
        self.assertEqual(refused["rejection_reason"], reason)
        with self.assertRaises(PD.PendingDeliveryError):
            PD.record_submission_not_accepted(
                self.root, "sess-claim", did, claim_owner="notice-first",
                expected_identity=identity, expected_attempt=first["attempt"],
                reason="queue-websocket-timeout")

        PD.release_claim(self.root, "sess-claim", did, claim_owner="notice-first",
                         failure_reason="explicit-add-refusal")
        retry_metadata = json.loads(PD.courier_retry_path(
            PD.record_path(self.root, "sess-claim", did)).read_text())
        with unittest.mock.patch.object(
                PD.time, "time_ns",
                return_value=retry_metadata["retry"]["next_attempt_at_ns"] + 1):
            second_claim = PD.claim(self.root, "sess-claim", did,
                                    claim_owner="notice-retry", lease_seconds=30,
                                    retry_context="notice-target")
        second = PD.prepare_submission_fence(
            self.root, "sess-claim", did, claim_owner="notice-retry",
            expected_identity=identity)
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(second["state"], "prepared")
        with self.assertRaises(PD.PendingDeliveryError):
            PD.read_submission_fence(
                self.root, "sess-claim", did,
                expected_identity=PD.submission_fence_identity(
                    second_claim, "successor-thread"))

    def test_submission_fence_optional_field_keeps_legacy_record_shape_valid(self):
        kwargs = dict(self.kwargs, delivery_id="delivery-" + "c" * 32)
        legacy = PD.create(**kwargs)
        path = PD.record_path(self.root, kwargs["recipient_key"], kwargs["delivery_id"])
        raw = json.loads(path.read_text())
        self.assertNotIn(PD.SUBMISSION_FENCE_FIELD, raw)
        self.assertEqual(PD.read(self.root, kwargs["recipient_key"], kwargs["delivery_id"]),
                         legacy)
        raw[PD.SUBMISSION_FENCE_FIELD] = {"state": "prepared"}
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            PD.read(self.root, kwargs["recipient_key"], kwargs["delivery_id"])
        self.assertEqual(ctx.exception.detail, "submission-fence-invalid")

    def test_submission_fence_keeps_original_full_identity_for_historical_reads(self):
        did = self.kwargs["delivery_id"]
        claimed = PD.claim(self.root, "sess-claim", did, claim_owner="original", lease_seconds=30)
        identity = PD.submission_fence_identity(claimed, "original-thread")
        PD.prepare_submission_fence(self.root, "sess-claim", did,
            claim_owner="original", expected_identity=identity)
        path = PD.record_path(self.root, "sess-claim", did)
        replacement = json.loads(path.read_text())
        replacement["route_id"] = "replacement-with-same-receipt"
        path.write_text(json.dumps(replacement))
        before = path.read_bytes()
        historical = PD.read_submission_fence(self.root, "sess-claim", did)
        self.assertEqual(historical["record_identity"], identity["record_identity"])
        self.assertNotIn("record_identity", PD.read(self.root, "sess-claim", did))
        for operation in (
                lambda: PD.record_submission_accepted(self.root, "sess-claim", did,
                    claim_owner="original", expected_identity=identity),
                lambda: PD.record_submission_not_accepted(self.root, "sess-claim", did,
                    claim_owner="original", expected_identity=identity, expected_attempt=1,
                    reason="queue-rpc-refused:thread/queue/add:private"),
                lambda: PD.reject_claimed(self.root, "sess-claim", did, claim_owner="original",
                    expected_identity=identity["record_identity"], reason="private-refusal"),
                lambda: PD.reclaim(self.root, "sess-claim", did, now_ns=claimed["claim_deadline_ns"] + 1,
                    expected_identity=identity["record_identity"]),
                lambda: PD.ack_received(self.root, "sess-claim", did,
                    expected_identity=identity["record_identity"], acked_by="original")):
            with self.subTest(operation=operation), self.assertRaises(PD.PendingDeliveryError):
                operation()
            self.assertEqual(path.read_bytes(), before)

    def test_submission_fence_first_write_requires_strict_future_integer_lease(self):
        did = self.kwargs["delivery_id"]
        claimed = PD.claim(self.root, "sess-claim", did, claim_owner="original", lease_seconds=30)
        identity = PD.submission_fence_identity(claimed, "original-thread")
        path = PD.record_path(self.root, "sess-claim", did)
        original = json.loads(path.read_text())
        for deadline in (True, float(time.monotonic_ns() + 10**12), time.monotonic_ns() - 1):
            with self.subTest(deadline=deadline):
                path.write_text(json.dumps(dict(original, claim_deadline_ns=deadline)))
                before = path.read_bytes()
                with self.assertRaises(PD.PendingDeliveryError):
                    PD.prepare_submission_fence(self.root, "sess-claim", did,
                        claim_owner="original", expected_identity=identity)
                self.assertEqual(path.read_bytes(), before)

    def test_generic_ack_does_not_gain_pending_state_authority(self):
        path = PD.record_path(self.root, "sess-claim", self.kwargs["delivery_id"])
        before = path.read_bytes()
        with self.assertRaises(PD.PendingDeliveryError):
            PD.ack(self.root, "sess-claim", self.kwargs["delivery_id"], acked_by="tokenless")
        self.assertEqual(path.read_bytes(), before)

    def test_verified_received_ack_survives_reclaim_and_claimant_change(self):
        did = self.kwargs["delivery_id"]
        original = PD.read(self.root, "sess-claim", did)
        identity = PD.received_ack_identity(original, "sess-claim", did)
        first = PD.claim(self.root, "sess-claim", did, claim_owner="first", lease_seconds=1)
        PD.reclaim(self.root, "sess-claim", did, now_ns=first["claim_deadline_ns"] + 1)
        successor = PD.claim(self.root, "sess-claim", did, claim_owner="successor", lease_seconds=30)
        self.assertNotEqual(successor["claim_owner"], original["claim_owner"])
        acked = PD.ack_received(self.root, "sess-claim", did,
                                expected_identity=identity, acked_by="peer-received:exact-ref")
        self.assertEqual(acked["state"], "acked")
        self.assertEqual(acked["attempts"], 2)
        self.assertEqual(PD.received_ack_identity(acked, "sess-claim", did), identity)

    def test_received_ack_rejects_changed_immutable_identity_without_write(self):
        did = self.kwargs["delivery_id"]
        original = PD.read(self.root, "sess-claim", did)
        identity = PD.received_ack_identity(original, "sess-claim", did)
        identity["route_node"] = "report"
        path = PD.record_path(self.root, "sess-claim", did)
        before = path.read_bytes()
        with self.assertRaisesRegex(PD.PendingDeliveryError, "received-identity-changed"):
            PD.ack_received(self.root, "sess-claim", did,
                            expected_identity=identity, acked_by="peer-received:exact-ref")
        self.assertEqual(path.read_bytes(), before)

    def test_received_ack_preserves_acked_expired_and_rejected_record_bytes(self):
        did = self.kwargs["delivery_id"]
        identity = PD.received_ack_identity(PD.read(self.root, "sess-claim", did), "sess-claim", did)
        claimed = PD.claim(self.root, "sess-claim", did, claim_owner="carrier", lease_seconds=30)
        PD.reject_claimed(self.root, "sess-claim", did, claim_owner="carrier", reason="permanent-refusal")
        path = PD.record_path(self.root, "sess-claim", did)
        before = path.read_bytes()
        result = PD.ack_received(self.root, "sess-claim", did,
                                 expected_identity=identity, acked_by="peer-received:exact-ref")
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(path.read_bytes(), before)

        other = dict(self.kwargs, delivery_id="delivery-" + "d" * 32)
        PD.create(**other)
        expired_identity = PD.received_ack_identity(PD.read(self.root, "sess-claim", other["delivery_id"]),
                                                    "sess-claim", other["delivery_id"])
        PD.expire_if_due(self.root, "sess-claim", other["delivery_id"],
                         actor="dispatch-reconcile", reason="pending-delivery-ttl-exceeded")
        expired_path = PD.record_path(self.root, "sess-claim", other["delivery_id"])
        expired_bytes = expired_path.read_bytes()
        result = PD.ack_received(self.root, "sess-claim", other["delivery_id"],
                                 expected_identity=expired_identity, acked_by="peer-received:exact-ref")
        self.assertEqual(result["state"], "expired")
        self.assertEqual(expired_path.read_bytes(), expired_bytes)

        acked_id = "delivery-" + "e" * 32
        acked_kwargs = dict(self.kwargs, delivery_id=acked_id)
        PD.create(**acked_kwargs)
        acked_identity = PD.received_ack_identity(PD.read(self.root, "sess-claim", acked_id), "sess-claim", acked_id)
        PD.claim(self.root, "sess-claim", acked_id, claim_owner="carrier", lease_seconds=30)
        PD.ack(self.root, "sess-claim", acked_id, acked_by="token-bearing-surface")
        acked_path = PD.record_path(self.root, "sess-claim", acked_id)
        acked_bytes = acked_path.read_bytes()
        result = PD.ack_received(self.root, "sess-claim", acked_id,
                                 expected_identity=acked_identity, acked_by="peer-received:exact-ref")
        self.assertEqual(result["state"], "acked")
        self.assertEqual(acked_path.read_bytes(), acked_bytes)

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
        proof = json.loads(tomb.read_text())
        self.assertEqual(proof["schema"], PD.PRUNE_TOMBSTONE_SCHEMA)
        self.assertEqual(proof["delivery_id"], old_acked.name[:-5])
        self.assertEqual(proof["recipient_digest"], old_acked.parent.name)
        self.assertEqual(proof["terminal_state"], "acked")
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


class ReferenceAdmissionTests(IsolatedRootMixin, unittest.TestCase):
    def _create(self, suffix, attempt):
        receipt = _receipt()
        delivery_id = "delivery-" + suffix * 32
        recipient = "sess-reference-admission"
        PD.create(
            self.root, recipient_kind="claude-parent-runtime", recipient_key=recipient,
            delivery_id=delivery_id, session_generation="", session_generation_supported="0",
            attempt_ids=[attempt], parent_attempt_id="att-reference-parent",
            route_id="rt-reference", route_node="execute", receipt=receipt,
            receipt_digest=_digest(receipt), row_revisions={attempt: "fixture"},
        )
        return recipient, delivery_id, PD.record_path(self.root, recipient, delivery_id)

    def test_ack_and_terminal_state_writes_wait_for_the_reference_admission(self):
        import fcntl, threading

        recipient, delivery_id, path = self._create("a", "att-reference-ack")
        PD.claim(self.root, recipient, delivery_id, claim_owner="carrier", lease_seconds=60)
        attempted, errors = threading.Event(), []
        real_flock = AR.fcntl.flock

        def observed_flock(fd, operation):
            if (threading.current_thread().name == "reference-state-writer"
                    and operation & fcntl.LOCK_EX):
                attempted.set()
            return real_flock(fd, operation)

        with AR.admission(self.root, ["att-reference-ack"]):
            def ack():
                try:
                    PD.ack(self.root, recipient, delivery_id, acked_by="consumed")
                except Exception as exc:
                    errors.append(repr(exc))

            thread = threading.Thread(target=ack, name="reference-state-writer")
            with unittest.mock.patch.object(AR.fcntl, "flock", observed_flock):
                thread.start()
                self.assertTrue(attempted.wait(3), "ACK writer did not reach its reference lock")
                self.assertEqual(json.loads(path.read_text())["state"], "claimed")
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(path.read_text())["state"], "acked")

        recipient, delivery_id, path = self._create("b", "att-reference-expire")
        attempted.clear()
        with AR.admission(self.root, ["att-reference-expire"]):
            def expire():
                try:
                    PD.expire_if_due(
                        self.root, recipient, delivery_id, actor=PD.EXPIRY_ACTOR,
                        reason="pending-delivery-ttl-exceeded")
                except Exception as exc:
                    errors.append(repr(exc))

            thread = threading.Thread(target=expire, name="reference-state-writer")
            with unittest.mock.patch.object(AR.fcntl, "flock", observed_flock):
                thread.start()
                self.assertTrue(attempted.wait(3), "terminal writer did not reach its reference lock")
                self.assertEqual(json.loads(path.read_text())["state"], "pending")
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(path.read_text())["state"], "expired")

    def test_prune_transition_waits_for_reference_admission_and_keeps_tombstone(self):
        import fcntl, threading

        recipient, delivery_id, path = self._create("c", "att-reference-prune")
        PD.claim(self.root, recipient, delivery_id, claim_owner="carrier", lease_seconds=60)
        PD.ack(self.root, recipient, delivery_id, acked_by="consumed")
        old = time.time() - 8 * 86400
        os.utime(path, (old, old))
        attempted, result, errors = threading.Event(), [], []
        real_flock = AR.fcntl.flock

        def observed_flock(fd, operation):
            if (threading.current_thread().name == "reference-pruner"
                    and operation & fcntl.LOCK_EX):
                attempted.set()
            return real_flock(fd, operation)

        with AR.admission(self.root, ["att-reference-prune"]):
            def prune():
                try:
                    result.append(PD.prune(
                        self.root, apply=True, retention_seconds=1,
                        lock_retention_seconds=86400))
                except Exception as exc:
                    errors.append(repr(exc))

            thread = threading.Thread(target=prune, name="reference-pruner")
            with unittest.mock.patch.object(AR.fcntl, "flock", observed_flock):
                thread.start()
                self.assertTrue(attempted.wait(3), "pruner did not reach its reference lock")
                self.assertTrue(path.is_file())
                self.assertFalse(PD.tombstone_path(path).exists())
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0]["pruned_records"], 1)
        self.assertFalse(path.exists())
        self.assertEqual(json.loads(PD.tombstone_path(path).read_text())["terminal_state"], "acked")

    def test_stale_reference_mutexes_are_removed_by_normal_prune(self):
        recipient, delivery_id, _path = self._create("d", "att-reference-stale")
        lock = AR.lock_path(self.root, "att-reference-stale")
        old = time.time() - 2 * 86400
        os.utime(lock, (old, old))

        plan = PD.prune_plan(self.root, lock_retention_seconds=3600)
        self.assertEqual([item["path"] for item in plan["reference_locks"]], [str(lock)])
        result = PD.prune(self.root, apply=True, lock_retention_seconds=3600)
        self.assertEqual(result["pruned_reference_locks"], 1)
        self.assertFalse(lock.exists())
        with AR.admission(self.root, ["att-reference-stale"]):
            self.assertTrue(lock.exists(), "the ordinary writer recreates a pruned key safely")

    def test_normal_create_does_not_resurrect_a_pruned_delivery_tombstone(self):
        recipient, delivery_id, path = self._create("e", "att-reference-pruned")
        PD.claim(self.root, recipient, delivery_id, claim_owner="carrier", lease_seconds=60)
        PD.ack(self.root, recipient, delivery_id, acked_by="consumed")
        old = time.time() - 8 * 86400
        os.utime(path, (old, old))
        result = PD.prune(self.root, apply=True, retention_seconds=1)
        self.assertEqual(result["pruned_records"], 1)
        tombstone = PD.tombstone_path(path)
        self.assertTrue(tombstone.is_file())

        with self.assertRaises(PD.PendingDeliveryError) as caught:
            self._create("e", "att-reference-pruned")
        self.assertEqual(caught.exception.detail, "record-pruned")
        self.assertFalse(path.exists())
        self.assertTrue(tombstone.is_file())

    def test_reference_waiter_reopens_after_an_old_inode_is_unlinked(self):
        import fcntl, threading

        attempt = "att-reference-inode-race"
        path = AR.lock_path(self.root, attempt)
        with AR.admission(self.root, [attempt]):
            pass
        old_fd = os.open(path, os.O_RDWR)
        fcntl.flock(old_fd, fcntl.LOCK_EX)
        waiter_attempted, waiter_reopened = threading.Event(), threading.Event()
        new_holder_entered, release_new_holder, waiter_entered = (
            threading.Event(), threading.Event(), threading.Event())
        errors, calls = [], {"waiter": 0}
        real_flock = AR.fcntl.flock

        def observed_flock(fd, operation):
            if (threading.current_thread().name == "old-inode-waiter"
                    and operation & fcntl.LOCK_EX):
                calls["waiter"] += 1
                if calls["waiter"] == 1:
                    waiter_attempted.set()
                elif calls["waiter"] == 2:
                    waiter_reopened.set()
            return real_flock(fd, operation)

        def waiter():
            try:
                with AR.admission(self.root, [attempt]):
                    waiter_entered.set()
            except Exception as exc:
                errors.append(repr(exc))

        def new_holder():
            try:
                with AR.admission(self.root, [attempt]):
                    new_holder_entered.set()
                    if not release_new_holder.wait(3):
                        errors.append("new lock holder timed out")
            except Exception as exc:
                errors.append(repr(exc))

        first = threading.Thread(target=waiter, name="old-inode-waiter")
        second = threading.Thread(target=new_holder, name="new-inode-holder")
        try:
            with unittest.mock.patch.object(AR.fcntl, "flock", observed_flock):
                first.start()
                self.assertTrue(waiter_attempted.wait(3))
                old_identity = os.fstat(old_fd).st_ino
                path.unlink()
                second.start()
                self.assertTrue(new_holder_entered.wait(3))
                self.assertNotEqual(old_identity, path.stat().st_ino)
                fcntl.flock(old_fd, fcntl.LOCK_UN)
                os.close(old_fd)
                old_fd = -1
                self.assertTrue(waiter_reopened.wait(3), "waiter must reopen the current lock inode")
                self.assertFalse(waiter_entered.is_set(), "waiter must respect the new inode holder")
                release_new_holder.set()
                self.assertTrue(waiter_entered.wait(3))
        finally:
            release_new_holder.set()
            if old_fd >= 0:
                fcntl.flock(old_fd, fcntl.LOCK_UN)
                os.close(old_fd)
            first.join(3)
            second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])

    def test_reference_admission_releases_after_exception(self):
        import subprocess

        attempt = "att-reference-exception"
        with self.assertRaisesRegex(RuntimeError, "fixture interruption"):
            with AR.admission(self.root, [attempt]):
                raise RuntimeError("fixture interruption")
        self.assertEqual(AR.held_attempts(), frozenset())
        with AR.admission(self.root, [attempt]) as admitted:
            self.assertTrue(admitted)

        child = (
            "import os,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
            "import dispatch_attempt_reference as a; "
            "ctx=a.admission(Path(sys.argv[2]),[sys.argv[3]]); "
            "ctx.__enter__(); print('held',flush=True); os._exit(0)"
        )
        crashed = subprocess.run(
            [sys.executable, "-c", child, str(HERE), str(self.root), attempt],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(crashed.returncode, 0, crashed.stderr)
        self.assertEqual(crashed.stdout.strip(), "held")
        with AR.admission(self.root, [attempt]) as admitted:
            self.assertTrue(admitted, "process interruption releases the advisory flock")

    def test_writer_reference_extraction_matches_retention_attempt_relation(self):
        record = {
            "attempt_ids": ["att-reference-child", "notice-" + "a" * 32, "malformed"],
            "parent_attempt_id": "att-reference-parent",
        }
        expected = frozenset({"att-reference-child", "att-reference-parent"})
        self.assertEqual(AR.record_attempts(record), expected)
        self.assertEqual(AR.lock_paths(self.root, record["attempt_ids"] + [record["parent_attempt_id"]]),
                         tuple(sorted(AR.lock_path(self.root, item) for item in expected)))


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
