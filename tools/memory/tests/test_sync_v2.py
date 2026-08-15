#!/usr/bin/env python3
"""SQLite transaction and durable-outbox tests for protocol v2."""

from __future__ import annotations

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
