#!/usr/bin/env python3
"""SQLite transaction and durable-outbox tests for protocol v2."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from helpers import (
    allocate_counter,
    envelope,
    ensure_sync_schema,
    make_operation,
    record_local_operation,
    remote_policy,
    table_names,
    transition_outbox,
)
import sync_v2


REPLICA_A = "11111111111111111111111111111111"
REPLICA_B = "22222222222222222222222222222222"
RECORD = "record-atomic"


class SyncDatabaseTest(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "memory.db"
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            "CREATE TABLE semantic_fixture (id TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )
        ensure_sync_schema(self.connection)
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    def _operation(self, *, counter: int = 1, body: str = "committed"):
        return make_operation(
            replica_id=REPLICA_A,
            counter=counter,
            record_id=RECORD,
            body=body,
        )

    def test_additive_schema_contains_all_protocol_ledgers(self):
        required = {
            "sync_replica",
            "sync_outbox",
            "sync_applied",
            "sync_frontier",
            "sync_conflicts",
            "sync_peer_state",
            "sync_quarantine",
            "sync_migration_epoch",
        }
        self.assertTrue(required <= table_names(self.connection))

    def test_semantic_row_applied_ledger_and_outbox_commit_atomically(self):
        op = self._operation()
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute(
            "INSERT INTO semantic_fixture(id, body) VALUES (?, ?)",
            (RECORD, "committed"),
        )
        record_local_operation(self.connection, op)
        self.connection.commit()

        self.assertEqual(
            self.connection.execute(
                "SELECT body FROM semantic_fixture WHERE id=?", (RECORD,)
            ).fetchone(),
            ("committed",),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM sync_outbox WHERE op_id=?", (op["op_id"],)
            ).fetchone(),
            ("queued",),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT result FROM sync_applied WHERE op_id=?", (op["op_id"],)
            ).fetchone(),
            ("local",),
        )

    def test_rollback_leaves_no_semantic_row_counter_applied_or_outbox(self):
        op = self._operation()
        self.connection.execute("BEGIN IMMEDIATE")
        allocated = allocate_counter(self.connection, REPLICA_A)
        self.assertGreaterEqual(allocated, 1)
        self.connection.execute(
            "INSERT INTO semantic_fixture(id, body) VALUES (?, ?)",
            (RECORD, "must roll back"),
        )
        record_local_operation(self.connection, op)
        self.connection.rollback()

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM semantic_fixture").fetchone(),
            (0,),
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM sync_outbox").fetchone(),
            (0,),
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM sync_applied").fetchone(),
            (0,),
        )
        self.connection.execute("BEGIN IMMEDIATE")
        retry_counter = allocate_counter(self.connection, REPLICA_A)
        self.connection.rollback()
        self.assertEqual(retry_counter, allocated)

    def test_outbox_states_are_monotonic_and_cannot_skip_confirmation_proof(self):
        op = self._operation()
        self.connection.execute("BEGIN IMMEDIATE")
        record_local_operation(self.connection, op)
        self.connection.commit()

        for state, evidence in (
            (
                "rendered",
                {
                    "rendered_path": f"protocol/v2/ops/{op['op_id'][:2]}/{op['op_id']}.json",
                    "rendered_commit": "b" * 40,
                },
            ),
            ("committed", {"local_commit": "c" * 40}),
        ):
            self.connection.execute("BEGIN IMMEDIATE")
            transition_outbox(self.connection, op["op_id"], state, evidence)
            self.connection.commit()

        invalid_evidence = (
            None,
            {"remote_tip": "a" * 40, "fetched_at": "now"},
            {"remote_tip": "a" * 39, "fresh_fetch": True, "fetched_at": "now"},
            {"remote_tip": "A" * 40, "fresh_fetch": True, "fetched_at": "now"},
            {"remote_tip": "a" * 40, "fresh_fetch": True, "fetched_at": ""},
        )
        for evidence in invalid_evidence:
            with self.subTest(evidence=evidence):
                self.connection.execute("BEGIN IMMEDIATE")
                with self.assertRaises(sync_v2.SyncInvariantError):
                    sync_v2.transition_outbox(
                        self.connection, op["op_id"], "confirmed", evidence
                    )
                self.connection.rollback()

        proof = {
            "remote_tip": "a" * 40,
            "fresh_fetch": True,
            "fetched_at": "2026-08-15T08:00:00+00:00",
        }
        self.connection.execute("BEGIN IMMEDIATE")
        sync_v2.transition_outbox(
            self.connection, op["op_id"], "confirmed", proof
        )
        self.connection.commit()
        self.assertEqual(
            self.connection.execute(
                "SELECT state, confirmed_remote_tip, confirmation_fetched_at "
                "FROM sync_outbox WHERE op_id=?",
                (op["op_id"],),
            ).fetchone(),
            ("confirmed", proof["remote_tip"], proof["fetched_at"]),
        )
        healthy = sync_v2.sync_status(
            self.connection, policy={"enabled": True, "allowed": True}
        )
        self.assertEqual(healthy["status"], "remote-confirmed")
        self.assertEqual(healthy["status_schema"], 1)

        self.connection.execute(
            "UPDATE sync_outbox SET confirmed_remote_tip=NULL, "
            "confirmation_fetched_at=NULL, evidence_json=NULL WHERE op_id=?",
            (op["op_id"],),
        )
        self.connection.commit()
        corrupt = sync_v2.sync_status(
            self.connection, policy={"enabled": True, "allowed": True}
        )
        self.assertEqual(corrupt["status"], "hard-failure")
        self.assertEqual(corrupt["exit_code"], 2)
        self.assertEqual(corrupt["reason"], "confirmed-proof-invalid")

        self.connection.execute("BEGIN IMMEDIATE")
        with self.assertRaises(sync_v2.SyncInvariantError):
            transition_outbox(self.connection, op["op_id"], "rendered")
        self.connection.rollback()

    def test_installation_fingerprint_detects_copy_and_requires_rotation(self):
        fingerprint_a = "aa" * 32
        fingerprint_b = "bb" * 32
        self.connection.execute("BEGIN IMMEDIATE")
        self.assertEqual(
            sync_v2.ensure_replica_identity(
                self.connection,
                REPLICA_A,
                installation_fingerprint=fingerprint_a,
            ),
            REPLICA_A,
        )
        self.connection.commit()

        self.connection.execute("BEGIN IMMEDIATE")
        self.assertEqual(
            sync_v2.allocate_counter(
                self.connection,
                REPLICA_A,
                installation_fingerprint=fingerprint_a,
            ),
            1,
        )
        self.connection.commit()

        self.connection.execute("BEGIN IMMEDIATE")
        with self.assertRaises(sync_v2.SyncInvariantError):
            sync_v2.allocate_counter(
                self.connection,
                REPLICA_A,
                installation_fingerprint=fingerprint_b,
            )
        self.connection.rollback()

        copied_write = make_operation(
            replica_id=REPLICA_A,
            counter=2,
            record_id="record-copied-installation",
            body="must rotate",
        )
        self.connection.execute("BEGIN IMMEDIATE")
        with self.assertRaises(sync_v2.SyncInvariantError):
            sync_v2.record_local_operation(
                self.connection,
                copied_write,
                installation_fingerprint=fingerprint_b,
            )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM sync_objects").fetchone(),
            (0,),
        )
        self.connection.rollback()

        self.connection.execute("BEGIN IMMEDIATE")
        rotated = sync_v2.rotate_replica_identity(
            self.connection,
            REPLICA_A,
            REPLICA_B,
            installation_fingerprint=fingerprint_b,
        )
        self.assertEqual(rotated, REPLICA_B)
        self.connection.commit()
        self.assertEqual(
            self.connection.execute(
                "SELECT replica_id, active, installation_fingerprint "
                "FROM sync_replica ORDER BY replica_id"
            ).fetchall(),
            [
                (REPLICA_A, 0, fingerprint_a),
                (REPLICA_B, 1, fingerprint_b),
            ],
        )
        self.connection.execute("BEGIN IMMEDIATE")
        self.assertEqual(
            sync_v2.allocate_counter(
                self.connection,
                REPLICA_B,
                installation_fingerprint=fingerprint_b,
            ),
            1,
        )
        self.connection.commit()

    def test_confirmation_accepts_sha256_object_id_with_fresh_proof(self):
        op = make_operation(
            replica_id=REPLICA_A,
            counter=1,
            record_id="record-sha256-confirm",
            body="committed",
        )
        self.connection.execute("BEGIN IMMEDIATE")
        record_local_operation(self.connection, op)
        self.connection.commit()
        for state, evidence in (
            (
                "rendered",
                {
                    "rendered_path": f"protocol/v2/ops/{op['op_id'][:2]}/{op['op_id']}.json",
                    "rendered_commit": "a" * 64,
                },
            ),
            ("committed", {"local_commit": "b" * 64}),
            (
                "confirmed",
                {
                    "remote_tip": "c" * 64,
                    "fresh_fetch": True,
                    "fetched_at": "2026-08-15T08:01:00+00:00",
                },
            ),
        ):
            self.connection.execute("BEGIN IMMEDIATE")
            sync_v2.transition_outbox(
                self.connection, op["op_id"], state, evidence
            )
            self.connection.commit()

    def test_unsupported_local_operation_is_not_recorded(self):
        supported = self._operation()
        payload = dict(supported["payload"])
        payload["schema_minor"] = 999
        unsupported = envelope(payload)

        self.connection.execute("BEGIN IMMEDIATE")
        with self.assertRaises(sync_v2.SyncInvariantError):
            record_local_operation(self.connection, unsupported)
        for table in ("sync_replica", "sync_objects", "sync_applied", "sync_outbox"):
            self.assertEqual(
                self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone(),
                (0,),
            )
        self.connection.rollback()

    def test_only_one_active_replica_can_allocate_or_author_local_operations(self):
        indexes = {
            row[1] for row in self.connection.execute("PRAGMA index_list(sync_replica)")
        }
        self.assertIn("sync_replica_one_active", indexes)

        self.connection.execute("BEGIN IMMEDIATE")
        self.assertEqual(allocate_counter(self.connection, REPLICA_A), 1)
        with self.assertRaises(sync_v2.SyncInvariantError):
            allocate_counter(self.connection, REPLICA_B)
        self.assertEqual(
            self.connection.execute(
                "SELECT replica_id, active FROM sync_replica"
            ).fetchall(),
            [(REPLICA_A, 1)],
        )
        self.connection.commit()

        foreign = make_operation(
            replica_id=REPLICA_B,
            counter=1,
            record_id="record-foreign-local-identity",
            body="must fail",
        )
        self.connection.execute("BEGIN IMMEDIATE")
        with self.assertRaises(sync_v2.SyncInvariantError):
            record_local_operation(self.connection, foreign)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM sync_objects").fetchone(),
            (0,),
        )
        self.connection.rollback()

    def test_recording_same_local_operation_is_idempotent_not_duplicated(self):
        op = self._operation()
        for _ in range(2):
            self.connection.execute("BEGIN IMMEDIATE")
            record_local_operation(self.connection, op)
            self.connection.commit()
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM sync_outbox WHERE op_id=?", (op["op_id"],)
            ).fetchone(),
            (1,),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM sync_applied WHERE op_id=?", (op["op_id"],)
            ).fetchone(),
            (1,),
        )

    def test_blocked_operation_evidence_prevents_clean_status(self):
        op = self._operation()
        self.connection.execute("BEGIN IMMEDIATE")
        record_local_operation(self.connection, op)
        self.connection.execute(
            "UPDATE sync_applied SET result='blocked:blocked-pending' "
            "WHERE op_id=?",
            (op["op_id"],),
        )
        self.connection.commit()
        status = sync_v2.sync_status(
            self.connection, policy={"enabled": True, "allowed": True}
        )
        self.assertEqual(status["status"], "fetched")
        self.assertEqual(status["exit_code"], 1)
        self.assertEqual(status["reason"], "blocked-operations")
        self.assertEqual(status["blocked_ids"], [op["op_id"]])

    def test_deferred_classification_remains_nonzero_in_later_status(self):
        op = self._operation()
        self.connection.execute("BEGIN IMMEDIATE")
        record_local_operation(self.connection, op)
        self.connection.execute(
            "UPDATE sync_objects SET classification='deferred-missing-parent' "
            "WHERE op_id=?",
            (op["op_id"],),
        )
        self.connection.commit()
        status = sync_v2.sync_status(self.connection)
        self.assertEqual(status["status"], "fetched")
        self.assertEqual(status["exit_code"], 1)
        self.assertEqual(status["reason"], "deferred-operations")
        self.assertEqual(status["deferred_ids"], [op["op_id"]])

    def test_status_json_is_bounded_below_64k_with_long_conflict_ids(self):
        long_prefix = "충돌" * 160
        for number in range(100):
            self.connection.execute(
                "INSERT INTO sync_conflicts(project_key,record_id,op_id,"
                "diagnostic_id,variant_bytes) VALUES(?,?,?,?,?)",
                (
                    "global",
                    f"{long_prefix}-{number:03d}",
                    f"{number:064x}",
                    f"diagnostic-{number}",
                    b"{}",
                ),
            )
        self.connection.commit()
        status = sync_v2.sync_status(self.connection)
        encoded = json.dumps(status, ensure_ascii=False).encode("utf-8")
        self.assertLess(len(encoded), 64 * 1024)
        self.assertLessEqual(len(status["conflict_ids"]), 8)
        self.assertGreater(status["conflict_ids_omitted"], 0)


class RemotePolicyTest(unittest.TestCase):

    def test_remote_is_disabled_by_default(self):
        policy = remote_policy({})
        self.assertFalse(policy["enabled"])
        self.assertEqual(policy["status_schema"], 1)
        self.assertFalse(policy.get("deprecated_alias", False))
        self.assertIn(policy.get("status"), {None, "not-configured", "local-only"})

    def test_legacy_dump_flag_selects_v2_exchange_with_warning_not_dump_push(self):
        policy = remote_policy({"MEM_DUMP_PUSH": "1"})
        self.assertTrue(policy["enabled"])
        self.assertTrue(policy["deprecated_alias"])
        self.assertEqual(policy["status_schema"], 1)
        self.assertEqual(policy.get("transport", "v2"), "v2")

    def test_canonical_remote_flag_has_precedence(self):
        policy = remote_policy({"MEM_SYNC_REMOTE": "1", "MEM_DUMP_PUSH": "1"})
        self.assertTrue(policy["enabled"])
        self.assertFalse(policy.get("deprecated_alias", False))
        self.assertEqual(policy["status_schema"], 1)

    def test_plain_bootstrap_mapping_cannot_forge_remote_readiness(self):
        forged = sync_v2.remote_policy(
            {"MEM_SYNC_REMOTE": "1"},
            bootstrap={
                "fresh_v2": True,
                "old_writer_fence_active": True,
                "v2_only": True,
                "epoch_state": "active",
            },
        )
        self.assertFalse(forged["allowed"])
        self.assertEqual(forged["exit_code"], 2)
        self.assertEqual(forged["reason"], "untrusted-bootstrap-evidence")
        self.assertEqual(forged["status_schema"], 1)

    def test_database_issued_bootstrap_evidence_remains_testable(self):
        connection = sqlite3.connect(":memory:")
        try:
            ensure_sync_schema(connection)
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            sync_v2.initialize_fresh_v2_epoch(
                connection, "fresh-test", proof="empty-store-proof"
            )
            sync_v2.activate_v2_only_fence(
                connection,
                "fresh-test",
                fence_proof="v2-only-writer-proof",
                operator_authorized=True,
            )
            connection.commit()

            evidence = sync_v2.trusted_bootstrap_evidence(connection)
            policy = sync_v2.remote_policy(
                {"MEM_SYNC_REMOTE": "1"}, bootstrap=evidence
            )
            self.assertTrue(policy["allowed"])
            self.assertEqual(policy["exit_code"], 0)
        finally:
            connection.close()


class MigrationStorageTest(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(
            "CREATE TABLE records(id TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )
        sync_v2.ensure_sync_schema(self.connection)
        self.connection.commit()
        self.epoch = "e" * 32

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    @staticmethod
    def _sha(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    @classmethod
    def _manifest(cls, payload):
        value = dict(payload)
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        value["manifest_digest"] = cls._sha(raw)
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode(), value["manifest_digest"]

    def _transition(self, target, **kwargs):
        current = sync_v2.migration_status(self.connection, self.epoch)
        input_digest = self._sha(f"input:{target}".encode())
        return sync_v2.migration_transition(
            self.connection,
            epoch_id=self.epoch,
            phase=target,
            target_state=target,
            expect_digest=current["state_digest"],
            input_digest=input_digest,
            **kwargs,
        )

    def _membership(self, replicas=(REPLICA_A,)):
        replicas = tuple(replicas)
        manifest_payload = {"membership": "sealed"}
        if replicas != (REPLICA_A,):
            manifest_payload["replicas"] = list(replicas)
        manifest, digest = self._manifest(manifest_payload)
        receipt = self._transition("membership-sealed", membership_digest=digest)
        sync_v2.record_migration_seal(
            self.connection,
            epoch_id=self.epoch,
            seal_kind="membership",
            manifest_bytes=manifest,
            receipt_digest=receipt["receipt_digest"],
            members=[
                {
                    "manifest_digest": self._sha(replica_id.encode()),
                    "replica_id": replica_id,
                    "retired": False,
                }
                for replica_id in replicas
            ],
        )
        return digest

    @classmethod
    def _rollback_apply_receipt(
        cls,
        *,
        epoch_id,
        state_digest,
        bundle_digest,
        replica_id,
        target_manifest_digest,
        backup_digest,
        projection_digest,
    ):
        payload = {
            "backup_digest": backup_digest,
            "bundle_digest": bundle_digest,
            "changed": True,
            "epoch_id": epoch_id,
            "migration_state": "rollback-window",
            "phase": "rollback.apply",
            "previous_state_digest": state_digest,
            "projection_digest": projection_digest,
            "protocol_major": 2,
            "replica_id": replica_id,
            "schema_version": 1,
            "status": "local-only",
            "target_manifest_digest": target_manifest_digest,
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return {**payload, "receipt_digest": cls._sha(canonical)}

    def test_v28_schema_is_additive_and_capture_sequence_is_transactional(self):
        required = {
            "sync_capture_clock",
            "sync_migration_artifacts",
            "sync_migration_attestations",
            "sync_migration_capture_bindings",
            "sync_migration_equality",
            "sync_migration_failures",
            "sync_migration_fold",
            "sync_migration_members",
            "sync_migration_receipts",
            "sync_migration_rollback",
            "sync_migration_rollback_targets",
            "sync_migration_seals",
            "sync_migration_seed_map",
            "sync_migration_seed_reservations",
            "sync_migration_state",
        }
        self.assertTrue(required <= table_names(self.connection))
        self.assertIn(
            "capture_seq",
            {
                row[1]
                for row in self.connection.execute("PRAGMA table_info(sync_objects)")
            },
        )

        first = make_operation(
            replica_id=REPLICA_A,
            counter=1,
            record_id="capture-one",
            body="one",
        )
        self.connection.execute("BEGIN IMMEDIATE")
        result = sync_v2.record_local_operation(self.connection, first)
        self.assertEqual(result["capture_seq"], 1)
        self.connection.commit()

        rolled_back = make_operation(
            replica_id=REPLICA_A,
            counter=2,
            record_id="capture-rollback",
            body="rollback",
        )
        self.connection.execute("BEGIN IMMEDIATE")
        self.assertEqual(
            sync_v2.record_local_operation(self.connection, rolled_back)[
                "capture_seq"
            ],
            2,
        )
        self.connection.rollback()
        self.assertEqual(sync_v2.capture_frontier(self.connection), 1)

        retry = make_operation(
            replica_id=REPLICA_A,
            counter=2,
            record_id="capture-retry",
            body="retry",
        )
        self.connection.execute("BEGIN IMMEDIATE")
        self.assertEqual(
            sync_v2.record_local_operation(self.connection, retry)["capture_seq"],
            2,
        )
        self.connection.commit()
        self.assertEqual(
            sync_v2.captured_operations(self.connection, after=0, through=2),
            [
                {"capture_seq": 1, "op_id": first["op_id"]},
                {"capture_seq": 2, "op_id": retry["op_id"]},
            ],
        )

    def test_migration_diagnostic_status_is_bounded_and_retains_last_failure(self):
        self.connection.execute("BEGIN IMMEDIATE")
        membership = self._membership()
        sync_v2.record_migration_attestation(
            self.connection,
            epoch_id=self.epoch,
            replica_id=REPLICA_A,
            kind="snapshot",
            payload_bytes=b"sealed-snapshot",
        )
        failure = sync_v2.record_migration_failure(
            self.connection,
            epoch_id=self.epoch,
            phase="snapshot.seal",
            reason="stale-state",
        )
        self.connection.commit()

        status = sync_v2.migration_diagnostic_status(self.connection, self.epoch)
        self.assertEqual(status["membership_digest"], membership)
        self.assertEqual(status["replicas"][0]["phase"], "snapshot")
        self.assertEqual(status["replicas"][0]["replica_id"], REPLICA_A)
        self.assertEqual(status["last_failure"]["reason"], "stale-state")
        self.assertEqual(status["last_failure"]["state_digest"],
                         failure["state_digest"])
        self.assertEqual(status["unconfirmed_outbox"], 0)

    def test_migration_receipts_enforce_monotonic_cas_and_exact_retry(self):
        membership = "a" * 64
        self.connection.execute("BEGIN IMMEDIATE")
        legacy = sync_v2.migration_status(self.connection, self.epoch)
        receipt = sync_v2.migration_transition(
            self.connection,
            epoch_id=self.epoch,
            phase="roster-membership-seal",
            target_state="membership-sealed",
            expect_digest=legacy["state_digest"],
            input_digest="b" * 64,
            membership_digest=membership,
        )
        retry = sync_v2.migration_transition(
            self.connection,
            epoch_id=self.epoch,
            phase="roster-membership-seal",
            target_state="membership-sealed",
            expect_digest=legacy["state_digest"],
            input_digest="b" * 64,
            membership_digest=membership,
        )
        self.assertEqual(retry, receipt)
        self.assertEqual(
            sync_v2.migration_receipt(
                self.connection, self.epoch, phase="roster-membership-seal"
            ),
            receipt,
        )
        artifact = Path(self.tempdir.name) / "membership.json"
        artifact.write_bytes(b"registered artifact")
        registered = sync_v2.record_migration_artifact(
            self.connection,
            epoch_id=self.epoch,
            artifact_kind="membership",
            manifest_digest=membership,
            local_path=artifact,
            receipt_digest=receipt["receipt_digest"],
        )
        self.assertFalse(registered["idempotent"])
        inventory = sync_v2.migration_artifacts(self.connection, self.epoch)
        self.assertEqual(inventory[0]["manifest_digest"], membership)
        self.assertEqual(inventory[0]["local_path"], str(artifact.resolve()))
        self.assertEqual(
            sync_v2.migration_receipt(
                self.connection,
                self.epoch,
                receipt_digest=receipt["receipt_digest"],
            ),
            receipt,
        )
        with self.assertRaises(sync_v2.SyncInvariantError):
            sync_v2.migration_transition(
                self.connection,
                epoch_id=self.epoch,
                phase="roster-membership-seal",
                target_state="membership-sealed",
                expect_digest=legacy["state_digest"],
                input_digest="c" * 64,
                membership_digest=membership,
            )
        with self.assertRaises(sync_v2.SyncInvariantError):
            sync_v2.migration_transition(
                self.connection,
                epoch_id=self.epoch,
                phase="snapshots-sealed",
                target_state="snapshots-sealed",
                expect_digest=receipt["state_digest"],
                input_digest="d" * 64,
            )
        self.connection.commit()
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM sync_migration_receipts"
            ).fetchone(),
            (1,),
        )

    def test_seed_counter_reservation_is_contiguous_idempotent_and_rollback_safe(self):
        self.connection.execute("BEGIN IMMEDIATE")
        membership = self._membership()
        sync_v2.ensure_replica_identity(self.connection, REPLICA_A)
        reservation = sync_v2.reserve_seed_counters(
            self.connection,
            epoch_id=self.epoch,
            replica_id=REPLICA_A,
            seed_kind="snapshot",
            source_digest="2" * 64,
            source_identities=["row-b", "row-a"],
            membership_digest=membership,
            activation_boundary="replica-a-generation-1",
            canonicalizer_version="v2.0",
        )
        self.assertEqual(reservation["counter_start"], 1)
        self.assertEqual(reservation["counter_end"], 2)
        self.assertEqual(
            [row["source_identity"] for row in reservation["mappings"]],
            ["row-a", "row-b"],
        )
        retry = sync_v2.reserve_seed_counters(
            self.connection,
            epoch_id=self.epoch,
            replica_id=REPLICA_A,
            seed_kind="snapshot",
            source_digest="2" * 64,
            source_identities=["row-b", "row-a"],
            membership_digest=membership,
            activation_boundary="replica-a-generation-1",
            canonicalizer_version="v2.0",
        )
        self.assertTrue(retry["idempotent"])
        seed_operation = make_operation(
            replica_id=REPLICA_A,
            counter=1,
            record_id="seed-row-a",
            body="seeded",
        )
        recorded = sync_v2.record_reserved_seed_operation(
            self.connection,
            seed_operation,
            epoch_id=self.epoch,
            seed_kind="snapshot",
            source_digest="2" * 64,
            source_identity="row-a",
        )
        self.assertEqual(recorded["op_id"], seed_operation["op_id"])
        self.connection.rollback()

        self.connection.execute("BEGIN IMMEDIATE")
        sync_v2.ensure_replica_identity(self.connection, REPLICA_A)
        self.assertEqual(sync_v2.allocate_counter(self.connection, REPLICA_A), 1)
        self.connection.rollback()

    def test_fence_trigger_fails_closed_for_old_writer_and_allows_v2_after_activation(self):
        self.connection.execute("BEGIN IMMEDIATE")
        self._membership()
        for phase in (
            "capture-enabled",
            "snapshots-sealed",
            "seeds-built",
            "fence-armed",
        ):
            self._transition(phase)
        self._transition("barrier-held", writer_mode="fenced")
        sync_v2.install_writer_fence(self.connection, self.epoch)
        self._transition(
            "old-writers-fenced",
            fence_capture_seq=sync_v2.capture_frontier(self.connection),
        )
        self.connection.commit()

        with self.assertRaises(sqlite3.OperationalError):
            self.connection.execute(
                "INSERT INTO records(id,body) VALUES('old','blocked')"
            )
        self.connection.rollback()
        sync_v2.register_writer_functions(self.connection, protocol_major=2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO records(id,body) VALUES('fenced','blocked')"
            )
        self.connection.rollback()

        self.connection.execute("BEGIN IMMEDIATE")
        for phase in (
            "deltas-drained",
            "no-tail-proven",
            "evidence-sealed",
            "seeds-published",
            "folded",
        ):
            extra = {"evidence_digest": "4" * 64} if phase == "evidence-sealed" else {}
            self._transition(phase, **extra)
        self._transition("equality-proven", equality_digest="5" * 64)
        self._transition("v2-only-enabled", writer_mode="v2")
        self.connection.commit()
        self.connection.execute(
            "INSERT INTO records(id,body) VALUES('v2','allowed')"
        )
        self.connection.commit()
        self.assertEqual(
            self.connection.execute("SELECT body FROM records WHERE id='v2'").fetchone(),
            ("allowed",),
        )

    def test_no_tail_requires_exact_capture_binding_rendered_seed_and_fence(self):
        captured = make_operation(
            replica_id=REPLICA_A,
            counter=1,
            record_id="captured-delta",
            body="captured",
        )
        self.connection.execute("BEGIN IMMEDIATE")
        sync_v2.record_local_operation(self.connection, captured)
        self._membership()
        for phase in (
            "capture-enabled",
            "snapshots-sealed",
            "seeds-built",
            "fence-armed",
        ):
            self._transition(phase)
        self._transition("barrier-held", writer_mode="fenced")
        sync_v2.install_writer_fence(self.connection, self.epoch)
        self._transition("old-writers-fenced", fence_capture_seq=1)
        blocked = sync_v2.no_tail_status(
            self.connection, epoch_id=self.epoch, after=0, through=1
        )
        self.assertFalse(blocked["proved"])
        self.assertEqual(blocked["unbound_capture_seq"], [1])
        registered = sync_v2.record_captured_delta_operation(
            self.connection,
            epoch_id=self.epoch,
            capture_seq=1,
            captured_op_id=captured["op_id"],
            source_digest="8" * 64,
        )
        self.assertFalse(registered["idempotent"])
        self.assertTrue(sync_v2.record_captured_delta_operation(
            self.connection, epoch_id=self.epoch, capture_seq=1,
            captured_op_id=captured["op_id"], source_digest="8" * 64,
        )["idempotent"])
        sync_v2.transition_outbox(
            self.connection,
            captured["op_id"],
            "rendered",
            {
                "rendered_commit": "a" * 40,
                "rendered_path": f"protocol/v2/ops/{captured['op_id'][:2]}/{captured['op_id']}.json",
            },
        )
        proved = sync_v2.no_tail_status(
            self.connection, epoch_id=self.epoch, after=0, through=1
        )
        self.assertTrue(proved["proved"])
        self.assertEqual(sync_v2.capture_frontier(self.connection), 1)
        self.connection.commit()

    def test_snapshot_seed_seal_requires_db_issued_equality_and_no_tail(self):
        self.connection.execute("BEGIN IMMEDIATE")
        membership = self._membership()
        sync_v2.ensure_replica_identity(self.connection, REPLICA_A)
        sync_v2.record_seed_epoch(self.connection, self.epoch, [REPLICA_A])
        with self.assertRaises(sync_v2.RemoteSafetyError):
            sync_v2.seal_seed_epoch(
                self.connection,
                self.epoch,
                no_tail_digest="6" * 64,
                accepted_set_digest="7" * 64,
                materialized_digest="8" * 64,
                operator_authorized=True,
            )
        for phase in (
            "capture-enabled",
            "snapshots-sealed",
            "seeds-built",
            "fence-armed",
        ):
            self._transition(phase)
        self._transition("barrier-held", writer_mode="fenced")
        sync_v2.install_writer_fence(self.connection, self.epoch)
        self._transition("old-writers-fenced", fence_capture_seq=0)
        self._transition("deltas-drained")
        no_tail = sync_v2.record_migration_attestation(
            self.connection,
            epoch_id=self.epoch,
            replica_id=REPLICA_A,
            kind="no-tail",
            payload_bytes=b'{"no_tail":true}',
        )
        self.assertEqual(len(no_tail["manifest_digest"]), 64)
        self.assertEqual(
            sync_v2.migration_attestation(
                self.connection,
                epoch_id=self.epoch,
                replica_id=REPLICA_A,
                kind="no-tail",
            )["manifest_digest"],
            no_tail["manifest_digest"],
        )
        self._transition("no-tail-proven")
        evidence_bytes, evidence_digest = self._manifest({"evidence": "sealed"})
        evidence_receipt = self._transition(
            "evidence-sealed", evidence_digest=evidence_digest
        )
        sync_v2.record_migration_seal(
            self.connection,
            epoch_id=self.epoch,
            seal_kind="evidence",
            manifest_bytes=evidence_bytes,
            receipt_digest=evidence_receipt["receipt_digest"],
        )
        self._transition("seeds-published")
        self._transition("folded")
        equality = sync_v2.record_equality_identity(
            self.connection,
            epoch_id=self.epoch,
            evidence_digest=evidence_digest,
            report_digests=["9" * 64],
            accepted_set_digest="a" * 64,
            operation_tree_digest="b" * 64,
            materialized_digest="c" * 64,
            authoritative_ref_oid="d" * 40,
        )
        self._transition(
            "equality-proven", equality_digest=equality["equality_digest"]
        )
        proof = sync_v2.trusted_migration_evidence(
            self.connection, self.epoch, kind="seed-seal"
        )
        sealed = sync_v2.seal_seed_epoch(
            self.connection,
            self.epoch,
            no_tail_digest=proof["no_tail_digest"],
            accepted_set_digest=proof["accepted_set_digest"],
            materialized_digest=proof["materialized_digest"],
            operator_authorized=True,
            evidence=proof,
        )
        self.assertTrue(sealed["seed_sealed"])
        with self.assertRaises(sync_v2.RemoteSafetyError):
            sync_v2.activate_v2_only_fence(
                self.connection,
                self.epoch,
                fence_proof="operator-fence-proof",
                operator_authorized=True,
                evidence=proof,
            )
        self._transition("v2-only-enabled", writer_mode="v2")
        activated = sync_v2.activate_v2_only_fence(
            self.connection,
            self.epoch,
            fence_proof="operator-fence-proof",
            operator_authorized=True,
            evidence=proof,
        )
        self.assertTrue(activated["remote_allowed"])
        self.connection.commit()

    def test_rollback_identity_is_tied_to_equality_and_closed_receipt(self):
        self.connection.execute("BEGIN IMMEDIATE")
        self._membership((REPLICA_A, REPLICA_B))
        sync_v2.ensure_replica_identity(self.connection, REPLICA_A)
        for phase in (
            "capture-enabled",
            "snapshots-sealed",
            "seeds-built",
            "fence-armed",
            "barrier-held",
        ):
            self._transition(phase)
        self._transition("old-writers-fenced", writer_mode="fenced", fence_capture_seq=0)
        sync_v2.record_migration_attestation(
            self.connection,
            epoch_id=self.epoch,
            replica_id=REPLICA_A,
            kind="no-tail",
            payload_bytes=b"no-tail",
        )
        for phase in ("deltas-drained", "no-tail-proven"):
            self._transition(phase)
        evidence = "e" * 64
        self._transition("evidence-sealed", evidence_digest=evidence)
        self._transition("seeds-published")
        fold = sync_v2.record_fold_identity(
            self.connection,
            epoch_id=self.epoch,
            evidence_digest=evidence,
            accepted_set_digest="2" * 64,
            operation_tree_digest="3" * 64,
            materialized_digest="4" * 64,
            reducer_version="v2.0",
        )
        self.assertEqual(len(fold["fold_digest"]), 64)
        self._transition("folded")
        equality = sync_v2.record_equality_identity(
            self.connection,
            epoch_id=self.epoch,
            evidence_digest=evidence,
            report_digests=["1" * 64],
            accepted_set_digest="2" * 64,
            operation_tree_digest="3" * 64,
            materialized_digest="4" * 64,
            authoritative_ref_oid="5" * 40,
        )["equality_digest"]
        self._transition("equality-proven", equality_digest=equality)
        self._transition("v2-only-enabled", writer_mode="v2")
        with self.assertRaises(sync_v2.SyncInvariantError):
            sync_v2.record_rollback_identity(
                self.connection,
                epoch_id=self.epoch,
                equality_digest=equality,
                bundle_digest="6" * 64,
                inventory_digest="7" * 64,
            )
        self._transition("rollback-window", writer_mode="fenced")
        installed = sync_v2.install_writer_fence(self.connection, self.epoch)
        self.assertEqual(len(installed["triggers"]), 3)
        rollback = sync_v2.record_rollback_identity(
            self.connection,
            epoch_id=self.epoch,
            equality_digest=equality,
            bundle_digest="6" * 64,
            inventory_digest="7" * 64,
        )
        self.assertTrue(
            sync_v2.record_rollback_identity(
                self.connection,
                epoch_id=self.epoch,
                equality_digest=equality,
                bundle_digest="6" * 64,
                inventory_digest="7" * 64,
            )["idempotent"]
        )
        with self.assertRaises(sync_v2.SyncInvariantError):
            self._transition(
                "closed", rollback_bundle_digest=rollback["bundle_digest"]
            )
        current = sync_v2.migration_status(self.connection, self.epoch)
        applied = sync_v2.record_rollback_apply(
            self.connection,
            epoch_id=self.epoch,
            expect_digest=current["state_digest"],
            bundle_digest=rollback["bundle_digest"],
            target_replica_id=REPLICA_A,
            target_manifest_digest="8" * 64,
            backup_digest="9" * 64,
            projection_digest="a" * 64,
        )
        retry = sync_v2.record_rollback_apply(
            self.connection,
            epoch_id=self.epoch,
            expect_digest=current["state_digest"],
            bundle_digest=rollback["bundle_digest"],
            target_replica_id=REPLICA_A,
            target_manifest_digest="8" * 64,
            backup_digest="9" * 64,
            projection_digest="a" * 64,
        )
        self.assertEqual(applied, retry)
        self.assertEqual(applied["backup_digest"], "9" * 64)
        self.assertEqual(applied["projection_digest"], "a" * 64)
        with self.assertRaises(sync_v2.SyncInvariantError):
            sync_v2.record_rollback_apply(
                self.connection,
                epoch_id=self.epoch,
                expect_digest=current["state_digest"],
                bundle_digest=rollback["bundle_digest"],
                target_replica_id=REPLICA_A,
                target_manifest_digest="8" * 64,
                backup_digest="b" * 64,
                projection_digest="a" * 64,
            )
        with self.assertRaises(sync_v2.SyncInvariantError):
            sync_v2.record_rollback_apply(
                self.connection,
                epoch_id=self.epoch,
                expect_digest=current["state_digest"],
                bundle_digest=rollback["bundle_digest"],
                target_replica_id=REPLICA_A,
                target_manifest_digest="8" * 64,
                backup_digest="9" * 64,
                projection_digest="b" * 64,
            )
        with self.assertRaises(sync_v2.SyncInvariantError):
            sync_v2.record_rollback_apply(
                self.connection,
                epoch_id=self.epoch,
                expect_digest=current["state_digest"],
                bundle_digest=rollback["bundle_digest"],
                target_replica_id=REPLICA_B,
                target_manifest_digest="8" * 64,
                backup_digest="9" * 64,
                projection_digest="a" * 64,
            )
        status = sync_v2.rollback_apply_status(self.connection, self.epoch)
        self.assertFalse(status["complete"])
        self.assertEqual(status["applied_replica_ids"], [REPLICA_A])
        self.assertEqual(status["missing_replica_ids"], [REPLICA_B])

        missing_roster = self._rollback_apply_receipt(
            epoch_id=self.epoch,
            state_digest=current["state_digest"],
            bundle_digest=rollback["bundle_digest"],
            replica_id="3" * 32,
            target_manifest_digest="b" * 64,
            backup_digest="c" * 64,
            projection_digest="d" * 64,
        )
        with self.assertRaises(sync_v2.SyncInvariantError):
            sync_v2.record_rollback_apply_receipt(
                self.connection, epoch_id=self.epoch, receipt=missing_roster
            )

        remote = self._rollback_apply_receipt(
            epoch_id=self.epoch,
            state_digest=current["state_digest"],
            bundle_digest=rollback["bundle_digest"],
            replica_id=REPLICA_B,
            target_manifest_digest="b" * 64,
            backup_digest="c" * 64,
            projection_digest="d" * 64,
        )
        remote_bytes = json.dumps(
            remote, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        imported = sync_v2.record_rollback_apply_receipt(
            self.connection, epoch_id=self.epoch, receipt=remote_bytes
        )
        self.assertEqual(imported, remote)
        self.assertEqual(
            sync_v2.record_rollback_apply_receipt(
                self.connection, epoch_id=self.epoch, receipt=remote
            ),
            remote,
        )
        equivocal = self._rollback_apply_receipt(
            epoch_id=self.epoch,
            state_digest=current["state_digest"],
            bundle_digest=rollback["bundle_digest"],
            replica_id=REPLICA_B,
            target_manifest_digest="b" * 64,
            backup_digest="c" * 64,
            projection_digest="e" * 64,
        )
        with self.assertRaises(sync_v2.SyncInvariantError):
            sync_v2.record_rollback_apply_receipt(
                self.connection, epoch_id=self.epoch, receipt=equivocal
            )
        with self.assertRaises(sync_v2.SyncInvariantError):
            sync_v2.record_rollback_apply_receipt(
                self.connection,
                epoch_id=self.epoch,
                receipt=remote_bytes + b"\n",
            )
        status = sync_v2.rollback_apply_status(self.connection, self.epoch)
        self.assertTrue(status["complete"])
        self.assertEqual(
            status["applied_replica_ids"], [REPLICA_A, REPLICA_B]
        )
        with self.assertRaises(sync_v2.RemoteSafetyError):
            sync_v2.trusted_migration_evidence(
                self.connection, self.epoch, kind="rollback"
            )
        self._transition(
            "closed", rollback_bundle_digest=rollback["bundle_digest"]
        )
        proof = sync_v2.trusted_migration_evidence(
            self.connection, self.epoch, kind="rollback"
        )
        self.assertEqual(proof["bundle_digest"], "6" * 64)
        removed = sync_v2.remove_writer_fence(self.connection, proof)
        self.assertEqual(len(removed["triggers"]), 3)
        self.assertFalse(
            any(
                str(row[0]).startswith("sync_cutover_records_")
                for row in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            )
        )
        self.connection.commit()

    def test_fresh_epoch_rejects_semantic_rows_without_v2_objects(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE records(id TEXT PRIMARY KEY)")
            ensure_sync_schema(connection)
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            sync_v2.initialize_fresh_v2_epoch(
                connection, "fresh-covered", proof="empty-store-proof"
            )
            sync_v2.activate_v2_only_fence(
                connection,
                "fresh-covered",
                fence_proof="v2-only-writer-proof",
                operator_authorized=True,
            )
            connection.commit()
            connection.execute("INSERT INTO records VALUES('uncaptured')")
            connection.commit()

            readiness = sync_v2.remote_readiness(connection)
            self.assertFalse(readiness["allowed"])
            self.assertFalse(readiness["semantic_state_covered"])
            self.assertEqual(
                readiness["reason"], "semantic-state-without-v2-objects"
            )
        finally:
            connection.close()

    def test_legacy_seed_cannot_be_sealed_with_caller_supplied_strings(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE records(id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO records VALUES('legacy')")
            ensure_sync_schema(connection)
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            replica = sync_v2.ensure_replica_identity(connection)
            sync_v2.record_seed_epoch(connection, "forged-seed", [replica])
            with self.assertRaises(sync_v2.RemoteSafetyError):
                sync_v2.seal_seed_epoch(
                    connection,
                    "forged-seed",
                    no_tail_digest="x",
                    accepted_set_digest="y",
                    materialized_digest="z",
                    operator_authorized=True,
                )
            self.assertFalse(sync_v2.remote_readiness(connection)["allowed"])
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
