#!/usr/bin/env python3
"""Hermetic tests for the v28 migration transaction/artifact engine."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

MEMORY_DIR = Path(__file__).resolve().parents[1]
if str(MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_DIR))
TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

import migration_v2 as migration
import protocol_v2
import sync_v2
from helpers import make_operation, record_post_state

EPOCH = "e" * 32
REPLICA_A = "1" * 32
REPLICA_B = "2" * 32
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def member(replica: str) -> dict:
    return {"replica_id": replica, "logical_project_keys": ["project"],
            "protected_ref": "refs/hearting/memory/v2",
            "writer_capability_hash": DIGEST_A}


def operation(replica: str, counter: int, parents=(), *, body=None) -> dict:
    return make_operation(replica_id=replica, counter=counter,
        record_id="rollback-record", body=body or f"body-{counter}",
        parents=parents, frontier=parents, project_key="project")


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "state.db"
        self.connection = sqlite3.connect(self.db)

    def tearDown(self):
        self.connection.close(); self.temp.cleanup()

    def test_dry_run_is_read_only_and_matches_sync_authority(self):
        engine = migration.MigrationEngine(self.connection, EPOCH)
        current = engine.current()
        self.assertEqual(current["state_digest"], migration.state_digest(EPOCH, "legacy"))
        before = self.db.read_bytes()
        plan = engine.transition("roster-membership-seal", "membership-sealed",
            {"manifest": DIGEST_A}, expect=current["state_digest"],
            membership_digest=DIGEST_A)
        self.assertFalse(plan["changed"])
        self.assertEqual(plan["migration_state"], "legacy")
        self.assertEqual(before, self.db.read_bytes())
        tables = {row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("sync_migration_state", tables)

    def test_apply_uses_sync_v2_cas_and_exact_retry(self):
        engine = migration.MigrationEngine(self.connection, EPOCH)
        expect = engine.current()["state_digest"]
        receipt = engine.transition("roster-membership-seal", "membership-sealed",
            {"manifest": DIGEST_A}, expect=expect, apply=True,
            membership_digest=DIGEST_A)
        self.assertEqual(receipt["migration_state"], "membership-sealed")
        self.assertEqual(migration.verify_receipt(receipt), receipt)
        self.assertEqual(engine.current(), sync_v2.migration_status(self.connection, EPOCH))
        retry = engine.transition("roster-membership-seal", "membership-sealed",
            {"manifest": DIGEST_A}, expect=expect, apply=True,
            membership_digest=DIGEST_A)
        self.assertEqual(receipt, retry)
        tables = {row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("sync_migration_v2_transaction", tables)
        with self.assertRaises(sync_v2.SyncInvariantError):
            engine.transition("roster-membership-seal", "membership-sealed",
                {"manifest": DIGEST_B}, expect=expect, apply=True,
                membership_digest=DIGEST_A)

    def test_skip_and_stale_transition_fail_without_advancing(self):
        engine = migration.MigrationEngine(self.connection, EPOCH)
        expect = engine.current()["state_digest"]
        with self.assertRaises(migration.MigrationError):
            engine.transition("snapshot", "snapshots-sealed", {}, expect=expect,
                              membership_digest=DIGEST_A)
        self.assertEqual(engine.current()["migration_state"], "legacy")

    def test_rollback_barrier_plan_precedes_bundle_creation(self):
        engine = migration.MigrationEngine(self.connection, EPOCH)
        current = {"epoch_id": EPOCH, "migration_state": "v2-only-enabled",
            "state_digest": DIGEST_A, "membership_digest": DIGEST_B,
            "evidence_digest": "c" * 64, "writer_mode": "v2",
            "phase_seq": 14, "equality_digest": "d" * 64,
            "rollback_bundle_digest": None, "fence_capture_seq": 1,
            "last_receipt_digest": "e" * 64, "protocol_major": 2}
        with mock.patch.object(engine, "current", return_value=current):
            plan = engine.transition("rollback.barrier", "rollback-window", {},
                expect=DIGEST_A, writer_mode="fenced")
        self.assertEqual(plan["planned_state"], "rollback-window")
        self.assertFalse(plan["changed"])


class ArtifactTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.membership = migration.seal_membership(
            epoch_id=EPOCH, member_manifests=[member(REPLICA_B), member(REPLICA_A)])

    def tearDown(self):
        self.temp.cleanup()

    def test_membership_dry_run_and_apply_are_deterministic(self):
        out = self.root / "roster"
        self.assertFalse(out.exists())
        self.assertFalse(self.membership["changed"])
        first = migration.seal_membership(epoch_id=EPOCH,
            member_manifests=[member(REPLICA_A), member(REPLICA_B)],
            out=out, apply=True)
        second = migration.seal_membership(epoch_id=EPOCH,
            member_manifests=[member(REPLICA_B), member(REPLICA_A)],
            out=out, apply=True)
        self.assertTrue(first["changed"]); self.assertFalse(second["changed"])
        self.assertEqual(first["manifest_digest"], second["manifest_digest"])
        self.assertEqual(migration.verify_membership(out / "membership.json")[
            "manifest_digest"], first["manifest_digest"])

    def test_retirement_requires_unique_data_backup(self):
        retired = {"replica_id": "3" * 32, "operator_decision": True,
                   "reason": "lost", "last_known_frontier": {}}
        with self.assertRaises(migration.MigrationError) as caught:
            migration.seal_membership(epoch_id=EPOCH,
                member_manifests=[member(REPLICA_A)],
                retirement_manifests=[retired])
        self.assertEqual(caught.exception.reason, "retirement-backup-missing")

    def test_evidence_requires_exact_active_roster(self):
        def evidence(replica):
            result = {"replica_id": replica,
                      "membership_digest": self.membership["manifest_digest"]}
            result.update({field: DIGEST_B for field in migration._EVIDENCE_FIELDS})
            return result
        with self.assertRaises(migration.MigrationError):
            migration.seal_evidence(epoch_id=EPOCH, membership=self.membership,
                                    replica_evidence=[evidence(REPLICA_A)])
        sealed = migration.seal_evidence(epoch_id=EPOCH, membership=self.membership,
            replica_evidence=[evidence(REPLICA_B), evidence(REPLICA_A)])
        self.assertEqual([row["replica_id"] for row in sealed["replicas"]],
                         [REPLICA_A, REPLICA_B])

    def _source_db(self, capture_seq=7, counter=4) -> Path:
        path = self.root / "source.db"
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA user_version=9")
        connection.execute("CREATE TABLE records(id TEXT PRIMARY KEY,tier TEXT,"
                           "scope TEXT,type TEXT,status TEXT,body TEXT)")
        connection.execute("INSERT INTO records VALUES('r','long','global',"
                           "'fact','active','secret body')")
        connection.execute("CREATE TABLE sync_capture_clock(singleton INTEGER PRIMARY KEY,"
                           "capture_seq TEXT NOT NULL)")
        connection.execute("INSERT INTO sync_capture_clock VALUES(1,?)",
                           (str(capture_seq),))
        connection.execute("CREATE TABLE sync_replica(replica_id TEXT PRIMARY KEY,"
                           "counter TEXT NOT NULL,active INTEGER NOT NULL)")
        connection.execute("INSERT INTO sync_replica VALUES(?,?,1)",
                           (REPLICA_A, str(counter)))
        connection.commit(); connection.close()
        return path

    def test_snapshot_uses_backup_and_reopen_verification(self):
        source, out = self._source_db(), self.root / "snapshot"
        plan = migration.create_snapshot(db_path=source, epoch_id=EPOCH,
            membership=self.membership, replica_id=REPLICA_A, out=out,
            capture_enabled=True, snapshot_capture_seq=7)
        self.assertFalse(plan["changed"]); self.assertFalse(out.exists())
        snapshot = migration.create_snapshot(db_path=source, epoch_id=EPOCH,
            membership=self.membership, replica_id=REPLICA_A, out=out, apply=True,
            capture_enabled=True, snapshot_capture_seq=7, outbox_counter=4,
            db_high_watermark=7)
        self.assertTrue(snapshot["changed"])
        verified = migration.verify_snapshot(out / "snapshot.json")
        self.assertEqual(verified["snapshot_capture_seq"], 7)
        self.assertEqual(verified["record_profile"]["status"], {"active": 1})
        retry = migration.create_snapshot(db_path=source, epoch_id=EPOCH,
            membership=self.membership, replica_id=REPLICA_A, out=out, apply=True,
            capture_enabled=True, snapshot_capture_seq=7, outbox_counter=4,
            db_high_watermark=7)
        self.assertFalse(retry["changed"])

    def test_snapshot_tampering_is_detected(self):
        source, out = self._source_db(capture_seq=0, counter=0), self.root / "snapshot-tamper"
        migration.create_snapshot(db_path=source, epoch_id=EPOCH,
            membership=self.membership, replica_id=REPLICA_A, out=out, apply=True,
            capture_enabled=True, snapshot_capture_seq=0)
        with (out / "snapshot.db").open("ab") as handle: handle.write(b"x")
        with self.assertRaises(migration.MigrationError):
            migration.verify_snapshot(out / "snapshot.json")

    def test_snapshot_lock_quiesces_concurrent_semantic_writer(self):
        source = self._source_db()
        lock = migration._open_snapshot_lock(source)
        competing = sqlite3.connect(source, timeout=0)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                competing.execute("INSERT INTO records VALUES('x','long','global',"
                                  "'fact','active','must not commit')")
                competing.commit()
            competing.rollback()
        finally:
            competing.close(); lock.rollback(); lock.close()
        check = sqlite3.connect(source)
        try:
            self.assertEqual(check.execute(
                "SELECT COUNT(*) FROM records WHERE id='x'").fetchone()[0], 0)
        finally:
            check.close()

    def test_snapshot_raced_watermark_creates_no_output(self):
        source, out = self._source_db(capture_seq=7, counter=4), self.root / "raced"
        with self.assertRaises(migration.MigrationError) as caught:
            migration.create_snapshot(db_path=source, epoch_id=EPOCH,
                membership=self.membership, replica_id=REPLICA_A, out=out, apply=True,
                capture_enabled=True, snapshot_capture_seq=6, outbox_counter=4,
                db_high_watermark=6)
        self.assertEqual(caught.exception.reason, "snapshot-watermark-raced")
        self.assertFalse(out.exists())

    def test_seed_manifest_preserves_distinct_dots_and_is_idempotent(self):
        first, second = operation(REPLICA_A, 1), operation(REPLICA_A, 2)
        mappings = [{"source_identity": "row:1", "counter": 1,
                     "op_id": first["op_id"]},
                    {"source_identity": "row:2", "counter": 2,
                     "op_id": second["op_id"]}]
        out = self.root / "seed"
        built = migration.build_seed_manifest(epoch_id=EPOCH,
            membership_digest=self.membership["manifest_digest"],
            snapshot_digest=DIGEST_A, source_digest=DIGEST_B,
            replica_id=REPLICA_A, kind="snapshot", mappings=mappings,
            operations=[second, first], out=out, apply=True)
        self.assertTrue(built["changed"])
        migration.verify_seed_manifest(out / "seed.json")
        retry = migration.build_seed_manifest(epoch_id=EPOCH,
            membership_digest=self.membership["manifest_digest"],
            snapshot_digest=DIGEST_A, source_digest=DIGEST_B,
            replica_id=REPLICA_A, kind="snapshot", mappings=list(reversed(mappings)),
            operations=[first, second], out=out, apply=True)
        self.assertFalse(retry["changed"])

    def test_seed_uses_protocol_v2_lf_canonical_operation_bytes(self):
        op = operation(REPLICA_A, 1)
        out = self.root / "protocol-seed"
        built = migration.build_seed_manifest(epoch_id=EPOCH,
            membership_digest=self.membership["manifest_digest"],
            snapshot_digest=DIGEST_A, source_digest=DIGEST_B,
            replica_id=REPLICA_A, kind="snapshot",
            mappings=[{"source_identity": "row:1", "counter": 1,
                       "op_id": op["op_id"]}], operations=[op], out=out,
            apply=True)
        raw = (out / built["objects"][0]["path"]).read_bytes()
        self.assertEqual(raw, protocol_v2.canonical_bytes(op))
        self.assertTrue(raw.endswith(b"\n"))
        migration.verify_seed_manifest(out / "seed.json")

    def test_seed_duplicate_dot_fails(self):
        one, two = operation(REPLICA_A, 1), operation(REPLICA_A, 1)
        two = operation(REPLICA_A, 1, body="different")
        with self.assertRaises(migration.MigrationError) as caught:
            migration.build_seed_manifest(epoch_id=EPOCH,
                membership_digest=self.membership["manifest_digest"],
                snapshot_digest=DIGEST_A, source_digest=DIGEST_B,
                replica_id=REPLICA_A, kind="snapshot", mappings=[],
                operations=[one, two])
        self.assertEqual(caught.exception.reason, "seed-dot-duplicate")

    def test_snapshot_seed_missing_pre_snapshot_parent_fails_closed(self):
        missing = "0" * 64
        child = operation(REPLICA_A, 3, [missing])
        common = dict(epoch_id=EPOCH,
            membership_digest=self.membership["manifest_digest"],
            snapshot_digest=DIGEST_A, source_digest=DIGEST_B,
            replica_id=REPLICA_A,
            mappings=[{"source_identity": "capture:3", "counter": 3,
                       "op_id": child["op_id"]}], operations=[child])
        with self.assertRaises(migration.MigrationError) as caught:
            migration.build_seed_manifest(kind="snapshot", **common)
        self.assertEqual(caught.exception.reason,
                         "snapshot-seed-causal-closure-incomplete")

        out = self.root / "deferred-delta-seed"
        delta = migration.build_seed_manifest(kind="delta", out=out,
            apply=True, **common)
        self.assertEqual(delta["dispositions"], [{
            "op_id": child["op_id"], "classification": "deferred",
            "reason": "missing-parent",
            "diagnostic_id": delta["dispositions"][0]["diagnostic_id"]}])
        migration.verify_seed_manifest(out / "seed.json")

    def _graveyard_line(self, record_id="deleted-record"):
        prior = record_post_state(record_id, "recoverable prior body",
                                  project_key="project")
        tombstone = {"action": "legacy-delete", "pending": False,
            "prior_digest": hashlib.sha256(
                protocol_v2.canonical_bytes(prior)).hexdigest(),
            "record_id": record_id}
        payload = {"schema_version": 1, "record_id": record_id,
            "prior_state": prior, "tombstone": tombstone,
            "recovery_evidence_digest": "7" * 64}
        entry = {**payload, "entry_digest": migration.digest_json(payload)}
        return migration.canonical_bytes(entry) + b"\n"

    def test_graveyard_source_seals_only_proven_deletions_and_builds_closure(self):
        external = self.root / "deleted-records.jsonl"
        external.write_bytes(self._graveyard_line())
        out = self.root / "graveyard-seal"
        sealed = migration.seal_graveyard_source(epoch_id=EPOCH,
            membership_digest=self.membership["manifest_digest"],
            snapshot_digest=DIGEST_A, replica_id=REPLICA_A,
            source=external, out=out, apply=True)
        migration.verify_graveyard_source(out / "graveyard.json")
        identities = migration.graveyard_source_identities(
            out / "graveyard.json")
        self.assertEqual(identities, ["graveyard:deleted-record:prior",
                                      "graveyard:deleted-record:tombstone"])
        built = migration.build_graveyard_seed_operations(
            graveyard=out / "graveyard.json",
            counter_mappings=[{"source_identity": identity,
                               "counter": index + 1}
                              for index, identity in enumerate(identities)])
        folded = protocol_v2.fold_operations(built["operations"])
        self.assertFalse(folded.classification.hard_failures)
        self.assertFalse(folded.blocked)
        self.assertNotIn("deleted-record", folded.records)
        self.assertIn("deleted-record", folded.tombstones)
        seed = migration.build_seed_manifest(epoch_id=EPOCH,
            membership_digest=self.membership["manifest_digest"],
            snapshot_digest=DIGEST_A,
            source_digest=built["source_digest"], replica_id=REPLICA_A,
            kind="snapshot", mappings=built["mappings"],
            operations=built["operations"])
        self.assertEqual(len(seed["objects"]), 2)
        self.assertEqual(sealed["entries"][0]["record_id"], "deleted-record")

    def test_graveyard_absence_duplicate_and_equivocation_fail_closed(self):
        empty = self.root / "empty-deleted-records.jsonl"
        empty.write_bytes(b"")
        empty_out = self.root / "empty-graveyard"
        sealed = migration.seal_graveyard_source(epoch_id=EPOCH,
            membership_digest=self.membership["manifest_digest"],
            snapshot_digest=DIGEST_A, replica_id=REPLICA_A,
            source=empty, out=empty_out, apply=True)
        self.assertEqual(sealed["entries"], [])
        self.assertEqual(migration.graveyard_source_identities(
            empty_out / "graveyard.json"), [])

        duplicate = self.root / "duplicate-deleted-records.jsonl"
        duplicate.write_bytes(self._graveyard_line() * 2)
        with self.assertRaises(migration.MigrationError):
            migration.seal_graveyard_source(epoch_id=EPOCH,
                membership_digest=self.membership["manifest_digest"],
                snapshot_digest=DIGEST_A, replica_id=REPLICA_A,
                source=duplicate)
        with self.assertRaises(migration.MigrationError) as caught:
            migration.seal_graveyard_source(epoch_id=EPOCH,
                membership_digest=self.membership["manifest_digest"],
                snapshot_digest=DIGEST_A, replica_id=REPLICA_A,
                source=self._graveyard_line(), out=empty_out, apply=True)
        self.assertEqual(caught.exception.reason, "artifact-equivocation")


class EqualityRollbackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def _report(self, replica: str) -> dict:
        result = {field: DIGEST_A for field in migration._SHARED_FIELDS}
        result.update({"schema_version": 9, "canonicalizer_version": 1,
                       "reducer_version": 1, "exit_class": 0,
                       "replica_id": replica, "applied_set_digest": DIGEST_A,
                       "capture_frontier": {replica: 4},
                       "seed_manifest_digest": DIGEST_A,
                       "unbound_capture_count": 0,
                       "unconfirmed_epoch_outbox_count": 0,
                       "fresh_remote_ref_oid": "c" * 40,
                       "remote_operation_tree_digest": DIGEST_A,
                       "local_materialized_digest": DIGEST_A,
                       "writer_mode": "fenced", "report_digest": None})
        result["report_digest"] = migration.replica_report_digest(result)
        return result

    def _receipt(self, state="old-writers-fenced") -> dict:
        payload = {"changed": True, "epoch_id": EPOCH,
            "evidence_digest": None, "input_digest": DIGEST_A,
            "membership_digest": DIGEST_A, "migration_state": state,
            "phase": "fence.activate", "previous_receipt_digest": DIGEST_B,
            "protocol_major": 2, "schema_version": 1,
            "state_digest": "c" * 64, "status": "local-only"}
        return {**payload, "receipt_digest": migration.digest_json(payload)}

    def _state_receipt(self, membership_digest, phase, state) -> dict:
        payload = {"changed": True, "epoch_id": EPOCH,
            "evidence_digest": None, "input_digest": DIGEST_A,
            "membership_digest": membership_digest, "migration_state": state,
            "phase": phase, "previous_receipt_digest": DIGEST_B,
            "protocol_major": 2, "schema_version": 1,
            "state_digest": "c" * 64, "status": "local-only"}
        return {**payload, "receipt_digest": migration.digest_json(payload)}

    def _complete_rollback_inputs(self):
        membership = migration.seal_membership(epoch_id=EPOCH,
            member_manifests=[member(REPLICA_A)])
        source = self.root / "complete-source.db"
        con = sqlite3.connect(source)
        con.execute("PRAGMA user_version=9")
        con.execute("CREATE TABLE records(id TEXT PRIMARY KEY,tier TEXT,"
                    "scope TEXT,type TEXT,status TEXT,body TEXT)")
        con.execute("INSERT INTO records VALUES('r','long','global',"
                    "'fact','active','before cutover')")
        con.execute("CREATE TABLE sync_capture_clock("
                    "singleton INTEGER PRIMARY KEY,capture_seq TEXT NOT NULL)")
        con.execute("INSERT INTO sync_capture_clock VALUES(1,'0')")
        con.execute("CREATE TABLE sync_replica(replica_id TEXT PRIMARY KEY,"
                    "counter TEXT NOT NULL,active INTEGER NOT NULL)")
        con.execute("INSERT INTO sync_replica VALUES(?,'0',1)", (REPLICA_A,))
        con.commit(); con.close()
        snapshot_root = self.root / "complete-snapshot"
        snapshot = migration.create_snapshot(db_path=source, epoch_id=EPOCH,
            membership=membership, replica_id=REPLICA_A, out=snapshot_root,
            apply=True, capture_enabled=True, snapshot_capture_seq=0,
            outbox_counter=0, db_high_watermark=0,
            local_v1_git_tip="d" * 40)

        parent = operation(REPLICA_A, 1)
        child = operation(REPLICA_A, 2, [parent["op_id"]])
        fence_state = self._state_receipt(membership["manifest_digest"],
            "fence.activate", "old-writers-fenced")
        delta_root = self.root / "complete-delta"
        delta = migration.create_delta_manifest(epoch_id=EPOCH,
            membership_digest=membership["manifest_digest"], snapshot=snapshot,
            fence_receipt=fence_state, replica_id=REPLICA_A,
            fence_capture_seq=1,
            capture_entries=[{"capture_seq": 1, "op_id": child["op_id"]}],
            operations=[child], out=delta_root, apply=True)
        seed_snapshot_root = self.root / "complete-snapshot-seed"
        seed_snapshot = migration.build_seed_manifest(epoch_id=EPOCH,
            membership_digest=membership["manifest_digest"],
            snapshot_digest=snapshot["manifest_digest"],
            source_digest=DIGEST_A, replica_id=REPLICA_A, kind="snapshot",
            mappings=[{"source_identity": "row:r", "counter": 1,
                       "op_id": parent["op_id"]}], operations=[parent],
            out=seed_snapshot_root, apply=True)
        seed_delta_root = self.root / "complete-delta-seed"
        seed_delta = migration.build_seed_manifest(epoch_id=EPOCH,
            membership_digest=membership["manifest_digest"],
            snapshot_digest=snapshot["manifest_digest"],
            source_digest=delta["manifest_digest"], replica_id=REPLICA_A,
            kind="delta", mappings=[{"source_identity": "capture:1",
                "counter": 2, "op_id": child["op_id"]}], operations=[child],
            out=seed_delta_root, apply=True)
        fence_phase = migration.create_phase_receipt(epoch_id=EPOCH,
            replica_id=REPLICA_A, phase="fence.activate",
            membership_digest=membership["manifest_digest"],
            state_receipt=fence_state,
            extra={"state_receipt": fence_state, "fence_capture_seq": 1})
        no_tail = migration.create_no_tail_report(epoch_id=EPOCH,
            snapshot=snapshot, fence_receipt=fence_state, delta=delta,
            current_capture_seq=1, unbound_capture_count=0,
            unrendered_outbox_count=0, fence_active=True)
        activation_state = self._state_receipt(membership["manifest_digest"],
            "activate.v2-only", "v2-only-enabled")
        activation_phase = migration.create_phase_receipt(epoch_id=EPOCH,
            replica_id=REPLICA_A, phase="activate.v2-only",
            membership_digest=membership["manifest_digest"],
            state_receipt=activation_state,
            extra={"state_receipt": activation_state})
        replica_report = self._report(REPLICA_A)
        evidence_row = {"replica_id": REPLICA_A,
            "membership_digest": membership["manifest_digest"],
            "snapshot_digest": snapshot["manifest_digest"],
            "seed_digest": migration.rollback_seed_set_digest(
                [seed_snapshot_root / "seed.json", seed_delta_root / "seed.json"]),
            "delta_digest": delta["manifest_digest"],
            "fence_digest": fence_phase["receipt_digest"],
            "no_tail_digest": no_tail["manifest_digest"],
            "backup_digest": snapshot["backup"]["sha256"]}
        evidence_row["equality_input_digest"] = \
            migration.replica_equality_input_digest(epoch_id=EPOCH,
                **evidence_row)
        evidence = migration.seal_evidence(epoch_id=EPOCH,
            membership=membership, replica_evidence=[evidence_row])
        equality = migration.create_equality_report(epoch_id=EPOCH,
            evidence_digest=evidence["manifest_digest"],
            replica_reports=[replica_report],
            authoritative_ref=membership["protected_ref"],
            expected_replica_ids=[REPLICA_A])
        raw_id, raw = "f" * 64, b"malformed deferred object preserved exactly"
        classifications = {parent["op_id"]: "accepted",
                           child["op_id"]: "blocked", raw_id: "deferred"}
        state_sections = {name: {} for name in
                          migration.rollback_state_section_names()}
        state_sections["accepted_set"] = {
            "operation_ids": sorted([parent["op_id"], child["op_id"]])}
        kwargs = dict(epoch_id=EPOCH, membership=membership, evidence=evidence,
            equality=equality, protected_ref_evidence={
                "protected_ref": membership["protected_ref"],
                "fresh_fetch": True, "ref_oid": "c" * 40,
                "operation_tree_digest": DIGEST_A},
            precutover_replicas=[{"replica_id": REPLICA_A,
                "snapshot": snapshot_root / "snapshot.json",
                "v1_dump": migration.canonical_bytes({"id": "r"}) + b"\n",
                "v1_ref_evidence": {"ref_oid": "d" * 40}}],
            seed_manifests=[seed_snapshot_root / "seed.json",
                            seed_delta_root / "seed.json"],
            delta_manifests=[delta_root / "delta.json"],
            no_tail_reports=[no_tail], fence_receipts=[fence_phase],
            activation_receipts=[activation_phase],
            operation_objects={
                parent["op_id"]: protocol_v2.canonical_bytes(parent),
                child["op_id"]: protocol_v2.canonical_bytes(child), raw_id: raw},
            classifications=classifications,
            unconfirmed_operation_ids=[child["op_id"]],
            state_sections=state_sections,
            materialized_dump=migration.canonical_bytes({"id": "r"}) + b"\n",
            diagnostics={"blocked": [child["op_id"]]},
            post_cutover_delta_index={"operation_ids": [child["op_id"]]})
        return membership, evidence, equality, raw_id, raw, kwargs

    def _complete_install_authority(self):
        membership, evidence, equality, raw_id, _, kwargs = \
            self._complete_rollback_inputs()
        kwargs["operation_objects"] = dict(kwargs["operation_objects"])
        kwargs["operation_objects"].pop(raw_id)
        kwargs["classifications"] = dict(kwargs["classifications"])
        kwargs["classifications"].pop(raw_id)
        blocked = next(op_id for op_id, value in
                       kwargs["classifications"].items() if value == "blocked")
        kwargs["classifications"][blocked] = "accepted"
        collected = migration.collect_rollback_bundle_inputs(**kwargs)
        bundle_root = self.root / "install-complete-bundle"
        bundle = migration.create_rollback_bundle(epoch_id=EPOCH,
            membership_digest=membership["manifest_digest"],
            evidence_digest=evidence["manifest_digest"],
            equality_digest=equality["manifest_digest"], out=bundle_root,
            apply=True, require_complete=True, **collected)
        projection_root = self.root / "install-v1-projection"
        projection = migration.export_v1_projection(epoch_id=EPOCH,
            bundle=bundle_root, records=[{"id": "r"}], loss_items={},
            out=projection_root, apply=True)
        self.assertTrue(projection["representable"])
        return membership, bundle, bundle_root, projection_root / "v1-export.json"

    def _rollback_target_db(self, membership_digest, bundle_digest, name="target.db"):
        path = self.root / name
        con = sqlite3.connect(path)
        self.assertEqual(con.execute("PRAGMA journal_mode=WAL").fetchone()[0],
                         "wal")
        con.execute("CREATE TABLE records(id TEXT PRIMARY KEY)")
        con.execute("INSERT INTO records VALUES('old')")
        sync_v2.ensure_sync_schema(con)
        con.execute("DELETE FROM sync_replica")
        con.execute("INSERT INTO sync_replica(replica_id,counter,active) "
                    "VALUES(?,'0',1)", (REPLICA_A,))
        input_digest, previous_receipt, receipt_digest = (
            "8" * 64, "7" * 64, "6" * 64)
        state_payload = {"epoch_id": EPOCH, "equality_digest": None,
            "evidence_digest": DIGEST_B, "fence_capture_seq": None,
            "membership_digest": membership_digest,
            "migration_state": "rollback-window", "phase_seq": 15,
            "previous_receipt_digest": previous_receipt,
            "protocol_major": 2, "rollback_bundle_digest": None,
            "transition_input_digest": input_digest, "writer_mode": "fenced"}
        state_digest = sync_v2._digest(state_payload)
        con.execute("INSERT INTO sync_migration_state("
                    "epoch_id,phase,phase_seq,current,membership_digest,"
                    "evidence_digest,writer_mode,state_digest,last_receipt_digest,"
                    "rollback_bundle_digest) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (EPOCH, "rollback-window", 15, 1, membership_digest,
                     DIGEST_B, "fenced", state_digest, receipt_digest,
                     None))
        con.execute("INSERT INTO sync_migration_receipts("
                    "epoch_id,phase_seq,phase,expect_digest,state_digest,"
                    "previous_receipt_digest,input_digest,membership_digest,"
                    "evidence_digest,changed,receipt_bytes,receipt_digest) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (EPOCH, 15, "rollback.barrier", "5" * 64, state_digest,
                     previous_receipt, input_digest, membership_digest,
                     DIGEST_B, 1, b"{}", receipt_digest))
        con.execute("INSERT INTO sync_migration_rollback("
                    "epoch_id,equality_digest,bundle_digest,inventory_digest,state) "
                    "VALUES(?,?,?,?,?)", (EPOCH, "4" * 64, bundle_digest,
                                           "3" * 64, "prepared"))
        con.commit(); con.close()
        return path

    def _snapshot_reference(self, capture_seq=3) -> dict:
        return migration._with_digest({"manifest_version": 1, "protocol_major": 2,
            "kind": "snapshot", "epoch_id": EPOCH,
            "membership_digest": DIGEST_A, "replica_id": REPLICA_A,
            "snapshot_capture_seq": capture_seq})

    def test_receipt_delta_and_no_tail_bind_exact_interval(self):
        receipt = self._receipt(); migration.verify_receipt(receipt)
        snapshot = self._snapshot_reference()
        first, second = operation(REPLICA_A, 4), operation(REPLICA_A, 5)
        entries = [{"capture_seq": 4, "op_id": first["op_id"]},
                   {"capture_seq": 5, "op_id": second["op_id"]}]
        out = self.root / "delta"
        delta = migration.create_delta_manifest(epoch_id=EPOCH,
            membership_digest=DIGEST_A, snapshot=snapshot,
            fence_receipt=receipt, replica_id=REPLICA_A, fence_capture_seq=5,
            capture_entries=entries, operations=[second, first], out=out, apply=True)
        self.assertTrue(delta["changed"])
        migration.verify_delta_manifest(out / "delta.json")
        proof = migration.create_no_tail_report(epoch_id=EPOCH,
            snapshot=snapshot, fence_receipt=receipt, delta=delta,
            current_capture_seq=5, unbound_capture_count=0,
            unrendered_outbox_count=0, fence_active=True)
        self.assertTrue(proof["proven"])
        migration.verify_no_tail_report(proof, snapshot=snapshot,
            fence_receipt=receipt, delta=delta)

    def test_replica_phase_receipts_bind_epoch_phase_membership_and_roster(self):
        state = self._receipt()
        first = migration.create_phase_receipt(epoch_id=EPOCH,
            replica_id=REPLICA_A, phase="fence.activate",
            membership_digest=DIGEST_A, state_receipt=state,
            extra={"fence_capture_seq": 5})
        second = migration.create_phase_receipt(epoch_id=EPOCH,
            replica_id=REPLICA_B, phase="fence.activate",
            membership_digest=DIGEST_A, state_receipt=state)
        self.assertEqual(migration.verify_phase_receipt(first,
            epoch_id=EPOCH, replica_id=REPLICA_A, phase="fence.activate",
            membership_digest=DIGEST_A, state_receipt=state), first)
        for keyword in ({"epoch_id": "f" * 32}, {"phase": "barrier.enter"},
                        {"membership_digest": DIGEST_B}):
            with self.subTest(keyword=keyword), self.assertRaises(migration.MigrationError):
                migration.verify_phase_receipt(first, **keyword)
        verified = migration.verify_phase_receipts([second, first], epoch_id=EPOCH,
            phase="fence.activate", membership_digest=DIGEST_A,
            expected_replica_ids=[REPLICA_A, REPLICA_B])
        self.assertEqual([row["replica_id"] for row in verified],
                         [REPLICA_A, REPLICA_B])
        with self.assertRaises(migration.MigrationError) as caught:
            migration.verify_phase_receipts([first, first], epoch_id=EPOCH,
                phase="fence.activate", membership_digest=DIGEST_A)
        self.assertEqual(caught.exception.reason,
                         "phase-receipt-replica-duplicate")

    def test_delta_missing_sequence_and_advanced_tail_fail(self):
        receipt, snapshot = self._receipt(), self._snapshot_reference()
        op = operation(REPLICA_A, 5)
        with self.assertRaises(migration.MigrationError) as caught:
            migration.create_delta_manifest(epoch_id=EPOCH,
                membership_digest=DIGEST_A, snapshot=snapshot,
                fence_receipt=receipt, replica_id=REPLICA_A, fence_capture_seq=5,
                capture_entries=[{"capture_seq": 5, "op_id": op["op_id"]}],
                operations=[op])
        self.assertEqual(caught.exception.reason, "delta-interval-not-exact")
        one, two = operation(REPLICA_A, 4), operation(REPLICA_A, 5)
        delta = migration.create_delta_manifest(epoch_id=EPOCH,
            membership_digest=DIGEST_A, snapshot=snapshot,
            fence_receipt=receipt, replica_id=REPLICA_A, fence_capture_seq=5,
            capture_entries=[{"capture_seq": 4, "op_id": one["op_id"]},
                             {"capture_seq": 5, "op_id": two["op_id"]}],
            operations=[one, two])
        with self.assertRaises(migration.MigrationError) as caught:
            migration.create_no_tail_report(epoch_id=EPOCH, snapshot=snapshot,
                fence_receipt=receipt, delta=delta, current_capture_seq=6,
                unbound_capture_count=0, unrendered_outbox_count=0,
                fence_active=True)
        self.assertEqual(caught.exception.reason, "no-tail-watermark-advanced")

    def test_normalized_equality_ignores_local_frontier_shape(self):
        report = migration.create_equality_report(epoch_id=EPOCH,
            evidence_digest=DIGEST_B,
            replica_reports=[self._report(REPLICA_B), self._report(REPLICA_A)],
            authoritative_ref="refs/hearting/memory/v2")
        self.assertTrue(report["equal"])
        self.assertEqual([row["replica_id"] for row in report["replica_matrix"]],
                         [REPLICA_A, REPLICA_B])
        bad = self._report(REPLICA_B); bad["materialized_digest"] = DIGEST_B
        with self.assertRaises(migration.MigrationError):
            migration.create_equality_report(epoch_id=EPOCH,
                evidence_digest=DIGEST_B,
                replica_reports=[self._report(REPLICA_A), bad],
                authoritative_ref="refs/hearting/memory/v2")

    def test_rollback_bundle_checks_accepted_closure_but_preserves_raw_quarantine(self):
        parent = operation(REPLICA_A, 1)
        child = operation(REPLICA_A, 2, [parent["op_id"]])
        files = {
            f"objects/{parent['op_id']}.json": protocol_v2.canonical_bytes(parent),
            f"objects/{child['op_id']}.json": protocol_v2.canonical_bytes(child),
            f"raw/{DIGEST_B}.bin": b"malformed-missing-parent-is-preserved",
            "receipts/equality.json": b"{}",
        }
        classes = {parent["op_id"]: "accepted", child["op_id"]: "accepted",
                   DIGEST_B: "quarantined"}
        out = self.root / "bundle"
        bundle = migration.create_rollback_bundle(epoch_id=EPOCH,
            membership_digest=DIGEST_A, evidence_digest=DIGEST_B,
            equality_digest="c" * 64, files=files,
            accepted_operation_ids=[child["op_id"], parent["op_id"]],
            classifications=classes, out=out, apply=True)
        self.assertTrue(bundle["changed"])
        self.assertEqual(migration.verify_rollback_bundle(out)["manifest_digest"],
                         bundle["manifest_digest"])
        retry = migration.create_rollback_bundle(epoch_id=EPOCH,
            membership_digest=DIGEST_A, evidence_digest=DIGEST_B,
            equality_digest="c" * 64, files=files,
            accepted_operation_ids=[parent["op_id"], child["op_id"]],
            classifications=classes, out=out, apply=True)
        self.assertFalse(retry["changed"])

    def test_rollback_rejects_missing_accepted_parent(self):
        parent = operation(REPLICA_A, 1)
        child = operation(REPLICA_A, 2, [parent["op_id"]])
        out = self.root / "bad-bundle"
        with self.assertRaises(migration.MigrationError):
            migration.create_rollback_bundle(epoch_id=EPOCH,
                membership_digest=DIGEST_A, evidence_digest=DIGEST_B,
                equality_digest="c" * 64,
                files={f"objects/{child['op_id']}.json": protocol_v2.canonical_bytes(child)},
                accepted_operation_ids=[child["op_id"]],
                classifications={child["op_id"]: "accepted"}, out=out, apply=True)

    def test_blocked_and_unconfirmed_are_orthogonal_to_causal_closure(self):
        parent = operation(REPLICA_A, 1)
        child = operation(REPLICA_A, 2, [parent["op_id"]])
        out = self.root / "blocked-unconfirmed-bundle"
        bundle = migration.create_rollback_bundle(epoch_id=EPOCH,
            membership_digest=DIGEST_A, evidence_digest=DIGEST_B,
            equality_digest="c" * 64,
            files={
                f"objects/accepted/{parent['op_id']}.json":
                    protocol_v2.canonical_bytes(parent),
                f"objects/accepted/{child['op_id']}.json":
                    protocol_v2.canonical_bytes(child),
            },
            accepted_operation_ids=[parent["op_id"], child["op_id"]],
            classifications={parent["op_id"]: "accepted",
                             child["op_id"]: "blocked"},
            unconfirmed_operation_ids=[child["op_id"]], out=out, apply=True)
        verified = migration.verify_rollback_bundle(out)
        self.assertEqual(verified["accepted_operation_ids"],
                         sorted([parent["op_id"], child["op_id"]]))
        self.assertEqual(verified["unconfirmed_operation_ids"], [child["op_id"]])
        self.assertEqual(bundle["manifest_digest"], verified["manifest_digest"])

    def test_complete_collector_seals_full_inventory_and_preserves_raw_bytes(self):
        membership, evidence, equality, raw_id, raw, kwargs = \
            self._complete_rollback_inputs()
        collected = migration.collect_rollback_bundle_inputs(**kwargs)
        out = self.root / "complete-bundle"
        bundle = migration.create_rollback_bundle(epoch_id=EPOCH,
            membership_digest=membership["manifest_digest"],
            evidence_digest=evidence["manifest_digest"],
            equality_digest=equality["manifest_digest"], out=out, apply=True,
            require_complete=True, **collected)
        verified = migration.verify_rollback_bundle(out, require_complete=True)
        self.assertTrue(verified["complete"])
        self.assertEqual(verified["manifest_digest"], bundle["manifest_digest"])
        self.assertEqual((out / f"objects/raw/deferred/{raw_id}.bin").read_bytes(), raw)
        self.assertEqual(len(verified["inventory"]),
                         len(collected["files"]))

    def test_pre_equality_input_has_no_post_publish_report_cycle(self):
        _, evidence, equality, _, _, kwargs = self._complete_rollback_inputs()
        sealed_input = evidence["replicas"][0]["equality_input_digest"]
        final_report = equality["replica_matrix"][0]["report_digest"]
        self.assertNotEqual(sealed_input, final_report)
        # The complete chain remains verifiable because equality binds the
        # evidence manifest, while the evidence input binds pre-publish files.
        self.assertEqual(equality["evidence_digest"],
                         evidence["manifest_digest"])
        migration.collect_rollback_bundle_inputs(**kwargs)

    def test_complete_collector_rejects_missing_replica_seed_and_tampered_bytes(self):
        membership, evidence, equality, _, _, kwargs = \
            self._complete_rollback_inputs()
        missing = dict(kwargs)
        missing["seed_manifests"] = missing["seed_manifests"][:1]
        with self.assertRaises(migration.MigrationError) as caught:
            migration.collect_rollback_bundle_inputs(**missing)
        self.assertEqual(caught.exception.reason, "rollback-seed-roster-incomplete")

        collected = migration.collect_rollback_bundle_inputs(**kwargs)
        out = self.root / "tamper-complete-bundle"
        migration.create_rollback_bundle(epoch_id=EPOCH,
            membership_digest=membership["manifest_digest"],
            evidence_digest=evidence["manifest_digest"],
            equality_digest=equality["manifest_digest"], out=out, apply=True,
            require_complete=True, **collected)
        materialized = out / "state/materialized.jsonl"
        materialized.write_bytes(materialized.read_bytes() + b"{}\n")
        with self.assertRaises(migration.MigrationError):
            migration.verify_rollback_bundle(out, require_complete=True)

    def test_rollback_install_backs_up_then_atomically_installs_and_retries(self):
        membership, bundle, bundle_root, projection = \
            self._complete_install_authority()
        db = self._rollback_target_db(membership["manifest_digest"],
                                      bundle["manifest_digest"])
        target_root = self.root / "rollback-target-manifest"
        target = migration.create_rollback_target_manifest(epoch_id=EPOCH,
            replica_id=REPLICA_A, db_path=db, bundle=bundle_root,
            projection=projection, out=target_root, apply=True)
        migration.verify_rollback_target_manifest(target_root / "target.json")
        request = migration._with_digest({"schema_version": 1,
            "protocol_major": 2, "kind": "rollback-target-request",
            "epoch_id": EPOCH, "replica_id": REPLICA_A,
            "store": str(db), "projection": str(projection),
            "install_out": str(self.root / "rollback-install")})
        self.assertEqual(migration.verify_rollback_target_request(request), request)

        def installer(connection, rows):
            connection.execute("DELETE FROM records")
            connection.executemany("INSERT INTO records(id) VALUES(?)",
                                   [(row["id"],) for row in rows])

        install_root = Path(request["install_out"])
        installed = migration.install_rollback_projection(epoch_id=EPOCH,
            db_path=db, bundle=bundle_root, projection=projection,
            target=target, out=install_root, apply=True, installer=installer)
        self.assertTrue(installed["changed"])
        verified = migration.verify_rollback_install(
            install_root, require_installed=True)
        self.assertTrue(verified["installed"])
        self.assertEqual(sorted(path.name for path in install_root.iterdir()),
            ["install-result.json", "install.json", "target-backup.db",
             "verified-projection.jsonl"])
        self.assertFalse(any(path.name.endswith(("-wal", "-shm"))
                             for path in install_root.iterdir()))
        backup = sqlite3.connect(install_root / "target-backup.db")
        try:
            self.assertEqual(backup.execute(
                "SELECT id FROM records ORDER BY id").fetchall(), [("old",)])
        finally:
            backup.close()
        live = sqlite3.connect(db)
        try:
            self.assertEqual(live.execute(
                "SELECT id FROM records ORDER BY id").fetchall(), [("r",)])
        finally:
            live.close()
        retry = migration.install_rollback_projection(epoch_id=EPOCH,
            db_path=db, bundle=bundle_root, projection=projection,
            target=target, out=install_root, apply=True, installer=installer)
        self.assertFalse(retry["changed"])

    def test_rollback_install_failure_rolls_back_and_retry_equivocation_fails(self):
        membership, bundle, bundle_root, projection = \
            self._complete_install_authority()
        db = self._rollback_target_db(membership["manifest_digest"],
            bundle["manifest_digest"], "failure-target.db")
        target = migration.create_rollback_target_manifest(epoch_id=EPOCH,
            replica_id=REPLICA_A, db_path=db, bundle=bundle_root,
            projection=projection)
        install_root = self.root / "failure-install"

        def failing(connection, rows):
            connection.execute("DELETE FROM records")
            raise RuntimeError("crash before engine-owned commit")

        with self.assertRaises(RuntimeError):
            migration.install_rollback_projection(epoch_id=EPOCH, db_path=db,
                bundle=bundle_root, projection=projection, target=target,
                out=install_root, apply=True, installer=failing)
        live = sqlite3.connect(db)
        try:
            self.assertEqual(live.execute("SELECT id FROM records").fetchall(),
                             [("old",)])
        finally:
            live.close()
        self.assertFalse(migration.verify_rollback_install(install_root)["installed"])

        equivocal = migration._with_digest({
            **{key: value for key, value in target.items()
               if key not in {"changed", "manifest_digest"}},
            "operator_note": "different retry"})
        with self.assertRaises(migration.MigrationError) as caught:
            migration.install_rollback_projection(epoch_id=EPOCH, db_path=db,
                bundle=bundle_root, projection=projection, target=equivocal,
                out=install_root, apply=True, installer=lambda con, rows: None)
        self.assertEqual(caught.exception.reason,
                         "rollback-install-retry-equivocation")

    def test_rollback_target_rejects_lossy_or_pre_cutover_only_projection(self):
        membership, bundle, bundle_root, _ = self._complete_install_authority()
        db = self._rollback_target_db(membership["manifest_digest"],
            bundle["manifest_digest"], "projection-target.db")
        precutover = bundle_root / f"precutover/{REPLICA_A}/v1-dump.jsonl"
        with self.assertRaises(migration.MigrationError):
            migration.create_rollback_target_manifest(epoch_id=EPOCH,
                replica_id=REPLICA_A, db_path=db, bundle=bundle_root,
                projection=precutover)

    def test_bundle_symlink_and_traversal_fail_closed(self):
        with self.assertRaises(migration.MigrationError):
            migration.create_rollback_bundle(epoch_id=EPOCH,
                membership_digest=DIGEST_A, evidence_digest=DIGEST_B,
                equality_digest="c" * 64, files={"../escape": b"x"},
                accepted_operation_ids=[], classifications={},
                out=self.root / "escape")

    def test_v1_export_writes_deterministic_dump_only_when_representable(self):
        op = operation(REPLICA_A, 1); op_id = op["op_id"]
        bundle = self.root / "representable-bundle"
        migration.create_rollback_bundle(epoch_id=EPOCH,
            membership_digest=DIGEST_A, evidence_digest=DIGEST_B,
            equality_digest="c" * 64,
            files={f"objects/{op_id}.json": protocol_v2.canonical_bytes(op)},
            accepted_operation_ids=[op_id], classifications={op_id: "accepted"},
            out=bundle, apply=True)
        out = self.root / "v1"
        result = migration.export_v1_projection(epoch_id=EPOCH, bundle=bundle,
            records=[{"id": "b", "body": "two"}, {"id": "a", "body": "one"}],
            loss_items={}, out=out, apply=True)
        self.assertTrue(result["representable"])
        self.assertEqual((out / "dump.jsonl").read_bytes(),
            b'{"body":"one","id":"a"}\n{"body":"two","id":"b"}\n')
        migration.verify_v1_projection(out / "v1-export.json")

    def test_v1_export_emits_loss_report_and_refuses_lossy_dump(self):
        raw_id = "d" * 64
        bundle = self.root / "loss-bundle"
        migration.create_rollback_bundle(epoch_id=EPOCH,
            membership_digest=DIGEST_A, evidence_digest=DIGEST_B,
            equality_digest="c" * 64,
            files={f"raw/{raw_id}.bin": b"unsupported"},
            accepted_operation_ids=[], classifications={raw_id: "quarantined"},
            out=bundle, apply=True)
        out = self.root / "loss-v1"
        result = migration.export_v1_projection(epoch_id=EPOCH, bundle=bundle,
            records=[], loss_items={"conflicts": ["record-x"]},
            out=out, apply=True)
        self.assertFalse(result["representable"])
        self.assertEqual(result["status"], "hard-failure")
        self.assertTrue((out / "loss-report.json").is_file())
        self.assertFalse((out / "dump.jsonl").exists())
        migration.verify_v1_projection(out / "v1-export.json")


if __name__ == "__main__":
    unittest.main()
