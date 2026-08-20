#!/usr/bin/env python3
"""Hermetic public-CLI and writer-fence checks for the v28 cutover."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MEM = ROOT / "tools/memory/mem.py"
sys.path.insert(0, str(ROOT / "tools/memory"))
import sync_v2  # noqa: E402
import migration_v2  # noqa: E402


EPOCH = "0123456789abcdef0123456789abcdef"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class MigrationCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.root = Path(self.tmp.name)
        self.store = self.root / "store"
        self.env = {
            **os.environ,
            "MEM_STORE": str(self.store),
            "MEM_INIT": "1",
            "XDG_STATE_HOME": str(self.root / "state"),
            "MEM_PROJECTS": str(self.root / "projects"),
            "CODEX_SESSIONS": str(self.root / "codex-sessions"),
        }
        initialized = self.run_mem("index", json_output=False)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.db = self.store / "memory.db"

    def tearDown(self):
        self.tmp.cleanup()

    def run_mem(self, *args, json_output=True):
        argv = [sys.executable, str(MEM), *args]
        if json_output and "--json" not in argv:
            argv.append("--json")
        return subprocess.run(argv, env=self.env, text=True, capture_output=True)

    def payload(self, result):
        self.assertTrue(result.stdout.strip(), result.stderr)
        return json.loads(result.stdout)

    def save_payload(self, result, path):
        value = self.payload(result)
        path.write_text(canonical(value), encoding="utf-8")
        return value

    @staticmethod
    def next_expect(value):
        return value.get("state_digest") or value["extra"]["state_digest"]

    def prepare_snapshot(self, record_id, logical_project_keys, *, prefix):
        capability = self.payload(self.run_mem(
            "migration", "capabilities", "--epoch", EPOCH))
        con = sqlite3.connect(self.db)
        try:
            replica = con.execute(
                "SELECT replica_id FROM sync_replica WHERE active=1"
            ).fetchone()[0]
        finally:
            con.close()
        member_path = self.root / f"{prefix}-member.json"
        member_path.write_text(canonical({
            "replica_id": replica,
            "logical_project_keys": list(logical_project_keys),
            "protected_ref": "refs/heads/hearting-memory-v2",
            "writer_capability_hash": capability["writer_capability_hash"],
        }), encoding="utf-8")
        expect = self.payload(self.run_mem(
            "migration", "status", "--epoch", EPOCH))["state_digest"]
        membership_out = self.root / f"{prefix}-membership"
        receipt = self.payload(self.run_mem(
            "migration", "roster", "membership-seal", "--epoch", EPOCH,
            "--expect", expect, "--member", str(member_path),
            "--out", str(membership_out), "--apply"))
        expect = self.next_expect(receipt)
        snapshot_out = self.root / f"{prefix}-snapshot"
        for _ in range(2):
            receipt = self.payload(self.run_mem(
                "migration", "snapshot", "--epoch", EPOCH,
                "--expect", expect,
                "--membership", str(membership_out / "membership.json"),
                "--replica", replica, "--store", str(self.store),
                "--out", str(snapshot_out), "--apply"))
            expect = self.next_expect(receipt)
        source_path = self.root / f"{prefix}-source.json"
        source_path.write_text(canonical({"source_identities": [record_id]}),
                               encoding="utf-8")
        return {"expect": expect, "replica": replica,
            "membership": membership_out / "membership.json",
            "snapshot": snapshot_out / "snapshot.json", "source": source_path}

    def file_inventory(self):
        result = {}
        for path in sorted(self.store.rglob("*")):
            if path.is_file():
                result[path.relative_to(self.store).as_posix()] = hashlib.sha256(
                    path.read_bytes()).hexdigest()
        return result

    def test_status_and_capabilities_are_read_only(self):
        before = self.file_inventory()
        status = self.run_mem("migration", "status", "--epoch", EPOCH)
        self.assertEqual(status.returncode, 0, status.stderr)
        data = self.payload(status)
        self.assertEqual(data["migration_state"], "legacy")
        self.assertEqual(data["writer_mode"], "legacy-capture")
        self.assertEqual(data["migration"]["replicas"], [])
        self.assertIsNone(data["migration"]["last_failure"])

        capabilities = self.run_mem("migration", "capabilities", "--epoch", EPOCH)
        self.assertEqual(capabilities.returncode, 0, capabilities.stderr)
        report = self.payload(capabilities)
        self.assertRegex(report["writer_capability_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(report["reason"], "ok")
        self.assertEqual(before, self.file_inventory())

    def test_membership_dry_run_apply_retry_and_stale_cas(self):
        status = self.payload(self.run_mem("migration", "status", "--epoch", EPOCH))
        capability = self.payload(self.run_mem(
            "migration", "capabilities", "--epoch", EPOCH))
        con = sqlite3.connect(self.db)
        try:
            replica = con.execute(
                "SELECT replica_id FROM sync_replica WHERE active=1"
            ).fetchone()[0]
            counter_before = con.execute(
                "SELECT counter FROM sync_replica WHERE active=1"
            ).fetchone()[0]
        finally:
            con.close()
        member = self.root / "member.json"
        member.write_text(canonical({
            "replica_id": replica,
            "logical_project_keys": ["project-a"],
            "protected_ref": "refs/heads/hearting-memory-v2",
            "writer_capability_hash": capability["writer_capability_hash"],
        }), encoding="utf-8")
        out = self.root / "cutover"
        common = ("migration", "roster", "membership-seal", "--epoch", EPOCH,
                  "--expect", status["state_digest"], "--member", str(member),
                  "--out", str(out))

        planned = self.run_mem(*common)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertFalse(out.exists())
        plan = self.payload(planned)
        self.assertFalse(plan["changed"])
        self.assertEqual(plan["planned_state"], "membership-sealed")

        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM sync_migration_state").fetchone()[0], 0)
            self.assertEqual(con.execute(
                "SELECT counter FROM sync_replica WHERE active=1").fetchone()[0],
                counter_before)
        finally:
            con.close()

        applied = self.run_mem(*common, "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        receipt = self.payload(applied)
        self.assertTrue(receipt["changed"])
        self.assertEqual(receipt["migration_state"], "membership-sealed")
        self.assertTrue((out / "membership.json").is_file())

        evidence = self.root / "replica-evidence.json"
        evidence.write_text(canonical({
            "replica_id": replica,
            "membership_digest": receipt["membership_digest"],
            **{field: "2" * 64 for field in (
                "snapshot_digest", "seed_digest", "delta_digest",
                "fence_digest", "no_tail_digest", "backup_digest",
                "equality_input_digest")},
        }), encoding="utf-8")
        bad_out = self.root / "out-of-order-evidence"
        counter_and_phase = sqlite3.connect(self.db)
        try:
            before_bad = (
                counter_and_phase.execute(
                    "SELECT counter FROM sync_replica WHERE active=1").fetchone()[0],
                counter_and_phase.execute(
                    "SELECT phase_seq FROM sync_migration_state WHERE epoch_id=?",
                    (EPOCH,)).fetchone()[0],
            )
        finally:
            counter_and_phase.close()
        out_of_order = self.run_mem(
            "migration", "roster", "evidence-seal", "--epoch", EPOCH,
            "--expect", receipt["state_digest"], "--membership",
            str(out / "membership.json"), "--replica-evidence", str(evidence),
            "--out", str(bad_out), "--apply")
        self.assertEqual(out_of_order.returncode, 2)
        self.assertEqual(self.payload(out_of_order)["reason"],
                         "evidence-predecessor-invalid")
        self.assertFalse(bad_out.exists())
        counter_and_phase = sqlite3.connect(self.db)
        try:
            self.assertEqual(before_bad, (
                counter_and_phase.execute(
                    "SELECT counter FROM sync_replica WHERE active=1").fetchone()[0],
                counter_and_phase.execute(
                    "SELECT phase_seq FROM sync_migration_state WHERE epoch_id=?",
                    (EPOCH,)).fetchone()[0],
            ))
        finally:
            counter_and_phase.close()

        retry = self.run_mem(*common, "--apply")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(receipt, self.payload(retry))

        stale = self.run_mem(
            "migration", "fence", "arm", "--epoch", EPOCH,
            "--expect", "0" * 64, "--membership", str(out / "membership.json"),
            "--capabilities", str(member), "--apply")
        self.assertEqual(stale.returncode, 2)
        self.assertEqual(self.payload(stale)["reason"], "stale-state")
        after_stale = self.payload(self.run_mem(
            "migration", "status", "--epoch", EPOCH))
        self.assertEqual(after_stale["migration"]["last_failure"]["phase"],
                         "fence.arm")
        self.assertEqual(after_stale["migration"]["last_failure"]["reason"],
                         "stale-state")
        self.assertEqual(after_stale["migration"]["membership_digest"],
                         receipt["membership_digest"])
        self.assertEqual(after_stale["migration"]["replicas"][0]["replica_id"],
                         replica)

    def test_concurrent_mutations_serialize_and_stale_loser_writes_nothing(self):
        status = self.payload(self.run_mem("migration", "status", "--epoch", EPOCH))
        capability = self.payload(self.run_mem(
            "migration", "capabilities", "--epoch", EPOCH))
        con = sqlite3.connect(self.db)
        try:
            replica = con.execute(
                "SELECT replica_id FROM sync_replica WHERE active=1").fetchone()[0]
        finally:
            con.close()
        processes, outputs = [], []
        for suffix in ("a", "b"):
            member = self.root / f"member-{suffix}.json"
            member.write_text(canonical({
                "replica_id": replica,
                "logical_project_keys": [f"project-{suffix}"],
                "protected_ref": "refs/heads/hearting-memory-v2",
                "writer_capability_hash": capability["writer_capability_hash"],
            }), encoding="utf-8")
            out = self.root / f"membership-{suffix}"
            outputs.append(out)
            argv = [sys.executable, str(MEM), "migration", "roster",
                    "membership-seal", "--epoch", EPOCH, "--expect",
                    status["state_digest"], "--member", str(member),
                    "--out", str(out), "--apply", "--json"]
            processes.append(subprocess.Popen(argv, env=self.env, text=True,
                                              stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE))
        results = [process.communicate(timeout=60) + (process.returncode,)
                   for process in processes]
        self.assertEqual(sorted(result[2] for result in results), [0, 2], results)
        self.assertEqual(sum((out / "membership.json").is_file()
                             for out in outputs), 1)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM sync_migration_receipts WHERE epoch_id=?",
                (EPOCH,)).fetchone()[0], 1)
        finally:
            con.close()

    def test_snapshot_seed_rejects_unseeded_graveyard_evidence(self):
        con = sqlite3.connect(self.db)
        sync_v2.register_writer_functions(con)
        try:
            replica = con.execute(
                "SELECT replica_id FROM sync_replica WHERE active=1").fetchone()[0]
            con.execute("INSERT INTO records(id,tier,scope,type,body) "
                        "VALUES('legacy-grave','durable','global','note','body')")
            con.execute("INSERT INTO sync_graveyard("
                        "destructive_op_id,record_id,tombstone_bytes,effective) "
                        "VALUES(?,?,?,1)", ("a" * 64, "legacy-grave", b"{}"))
            con.commit()
        finally:
            con.close()
        capability = self.payload(self.run_mem(
            "migration", "capabilities", "--epoch", EPOCH))
        membership_out = self.root / "grave-membership"
        membership = migration_v2.seal_membership(epoch_id=EPOCH,
            member_manifests=[{"replica_id": replica,
                "logical_project_keys": ["global"],
                "protected_ref": "refs/heads/hearting-memory-v2",
                "writer_capability_hash": capability["writer_capability_hash"]}],
            out=membership_out, apply=True)
        snapshot_out = self.root / "grave-snapshot"
        snapshot = migration_v2.create_snapshot(db_path=self.db, epoch_id=EPOCH,
            membership=membership_out / "membership.json", replica_id=replica,
            out=snapshot_out, apply=True, capture_enabled=True,
            snapshot_capture_seq=0, outbox_counter=0, db_high_watermark=0)
        con = sqlite3.connect(self.db)
        sync_v2.register_writer_functions(con)
        try:
            current = sync_v2.migration_status(con, EPOCH)
            for phase in ("membership-sealed", "capture-enabled", "snapshots-sealed"):
                con.execute("BEGIN IMMEDIATE")
                kwargs = ({"membership_digest": membership["manifest_digest"]}
                          if phase == "membership-sealed" else {})
                receipt = sync_v2.migration_transition(con, epoch_id=EPOCH,
                    phase=f"test.{phase}", target_state=phase,
                    expect_digest=current["state_digest"],
                    input_digest=hashlib.sha256(phase.encode()).hexdigest(), **kwargs)
                con.commit()
                current = sync_v2.migration_status(con, EPOCH)
        finally:
            con.close()
        source = self.root / "grave-source.json"
        source.write_text(canonical({"source_identities": ["legacy-grave"]}),
                          encoding="utf-8")
        result = self.run_mem("migration", "seed", "build", "--epoch", EPOCH,
            "--expect", receipt["state_digest"], "--membership",
            str(membership_out / "membership.json"), "--snapshot",
            str(snapshot_out / "snapshot.json"), "--kind", "snapshot",
            "--source", str(source), "--out", str(self.root / "grave-seed"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.payload(result)["reason"],
                         "unseeded-graveyard-evidence")
        self.assertFalse((self.root / "grave-seed").exists())

    def test_graveyard_only_snapshot_seeds_proven_tombstone(self):
        body = "legacy-only deletion evidence"
        created = self.run_mem("add", "durable", "note", body,
                               "--scope", "project", json_output=False)
        self.assertEqual(created.returncode, 0, created.stderr)
        con = sqlite3.connect(self.db)
        try:
            record_id, project_key = con.execute(
                "SELECT id,cwd_origin FROM records WHERE body=?", (body,)
            ).fetchone()
            replica = con.execute(
                "SELECT replica_id FROM sync_replica WHERE active=1"
            ).fetchone()[0]
        finally:
            con.close()
        deleted = self.run_mem("delete", record_id, "--force",
                               json_output=False)
        self.assertEqual(deleted.returncode, 0,
                         deleted.stdout + deleted.stderr)
        legacy_raw = (self.store / "deleted-records.jsonl").read_bytes()
        con = sqlite3.connect(self.db)
        try:
            for table in ("sync_transactional_graveyard", "sync_graveyard",
                          "sync_outbox", "sync_applied", "sync_frontier",
                          "sync_parents", "sync_objects"):
                con.execute(f'DELETE FROM "{table}"')
            con.execute("UPDATE sync_replica SET counter='0' WHERE active=1")
            con.execute(
                "UPDATE sync_capture_clock SET capture_seq='0' WHERE singleton=1")
            con.commit()
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM records").fetchone()[0], 0)
        finally:
            con.close()

        capability = self.payload(self.run_mem(
            "migration", "capabilities", "--epoch", EPOCH))
        member = self.root / "graveyard-only-member.json"
        member.write_text(canonical({"replica_id": replica,
            "logical_project_keys": [project_key],
            "protected_ref": "refs/heads/hearting-memory-v2",
            "writer_capability_hash": capability["writer_capability_hash"]}),
            encoding="utf-8")
        expect = self.payload(self.run_mem(
            "migration", "status", "--epoch", EPOCH))["state_digest"]
        membership_out = self.root / "graveyard-only-membership"
        receipt = self.payload(self.run_mem(
            "migration", "roster", "membership-seal", "--epoch", EPOCH,
            "--expect", expect, "--member", str(member),
            "--out", str(membership_out), "--apply"))
        expect = self.next_expect(receipt)
        snapshot_out = self.root / "graveyard-only-snapshot"
        for _ in range(2):
            receipt = self.payload(self.run_mem(
                "migration", "snapshot", "--epoch", EPOCH,
                "--expect", expect, "--membership",
                str(membership_out / "membership.json"),
                "--replica", replica, "--store", str(self.store),
                "--out", str(snapshot_out), "--apply"))
            expect = self.next_expect(receipt)
        self.assertEqual(
            (snapshot_out / "graveyard" / "legacy-deleted-records.jsonl"
             ).read_bytes(), legacy_raw)
        source = self.root / "graveyard-only-source.json"
        source.write_text(canonical({"source_identities": []}), encoding="utf-8")
        seed_out = self.root / "graveyard-only-seed"
        result = self.run_mem(
            "migration", "seed", "build", "--epoch", EPOCH,
            "--expect", expect, "--membership",
            str(membership_out / "membership.json"), "--snapshot",
            str(snapshot_out / "snapshot.json"), "--kind", "snapshot",
            "--source", str(source), "--out", str(seed_out), "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        seed = migration_v2.verify_seed_manifest(seed_out / "seed.json")
        identities = {row["source_identity"] for row in seed["mappings"]}
        self.assertEqual(identities, {
            f"graveyard:{record_id}:prior",
            f"graveyard:{record_id}:tombstone"})
        envelopes = [json.loads((seed_out / row["path"]).read_text())
                     for row in seed["objects"]]
        folded = __import__("protocol_v2").fold_operations(envelopes)
        self.assertNotIn(record_id, folded.records)
        self.assertEqual(len(folded.tombstones), 1)

    def _add_record(self, body, *, delivery=None):
        args = ["add", "durable", "note", body, "--scope", "project"]
        if delivery == "pending":
            args = ["note", body, "--type", "handoff", "--requires-consume"]
        created = self.run_mem(*args, json_output=False)
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        # The write line names the exact id; the body is normalized on the way
        # in, so matching on it is not reliable.
        record_id = created.stdout.strip().rsplit("\u2192", 1)[-1].strip()
        con = sqlite3.connect(self.db)
        try:
            row = con.execute(
                "SELECT id,cwd_origin FROM records WHERE id=?", (record_id,)
            ).fetchone()
        finally:
            con.close()
        self.assertIsNotNone(row, created.stdout + created.stderr)
        return row

    def _reset_capture(self):
        """Model an imported legacy store: records only, no v2 capture state."""
        con = sqlite3.connect(self.db)
        try:
            for table in ("sync_transactional_graveyard", "sync_graveyard",
                          "sync_outbox", "sync_applied", "sync_frontier",
                          "sync_parents", "sync_objects"):
                con.execute(f'DELETE FROM "{table}"')
            con.execute("UPDATE sync_replica SET counter='0' WHERE active=1")
            con.execute(
                "UPDATE sync_capture_clock SET capture_seq='0' WHERE singleton=1")
            con.commit()
            return con.execute(
                "SELECT replica_id FROM sync_replica WHERE active=1").fetchone()[0]
        finally:
            con.close()

    def _seal_through_snapshot(self, replica, project_keys, prefix):
        capability = self.payload(self.run_mem(
            "migration", "capabilities", "--epoch", EPOCH))
        member = self.root / f"{prefix}-member.json"
        member.write_text(canonical({
            "replica_id": replica,
            "logical_project_keys": sorted(project_keys),
            "protected_ref": "refs/heads/hearting-memory-v2",
            "writer_capability_hash": capability["writer_capability_hash"]}),
            encoding="utf-8")
        expect = self.payload(self.run_mem(
            "migration", "status", "--epoch", EPOCH))["state_digest"]
        membership_out = self.root / f"{prefix}-membership"
        receipt = self.payload(self.run_mem(
            "migration", "roster", "membership-seal", "--epoch", EPOCH,
            "--expect", expect, "--member", str(member),
            "--out", str(membership_out), "--apply"))
        expect = self.next_expect(receipt)
        snapshot_out = self.root / f"{prefix}-snapshot"
        for _ in range(2):
            receipt = self.payload(self.run_mem(
                "migration", "snapshot", "--epoch", EPOCH, "--expect", expect,
                "--membership", str(membership_out / "membership.json"),
                "--replica", replica, "--store", str(self.store),
                "--out", str(snapshot_out), "--apply"))
            expect = self.next_expect(receipt)
        return (expect, membership_out / "membership.json",
                snapshot_out / "snapshot.json")

    def test_multi_entry_graveyard_seeds_every_legacy_deletion(self):
        """A real store deletes more than one record before it ever migrates.

        The deletion log is consumed as canonical JSONL, one entry per line;
        a pre-capsule row carries no canonical_id/status/capsule_version; and a
        row deleted while pending needs force authority, not a plain tombstone.
        """
        keys = set()
        deleted = []
        for index in range(3):
            record_id, project_key = self._add_record(
                f"legacy deletion evidence row number {index} for the seed")
            keys.add(project_key)
            deleted.append(record_id)
        pending_id, pending_key = self._add_record(
            "legacy pending handoff evidence retained for recovery",
            delivery="pending")
        keys.add(pending_key)
        deleted.append(pending_id)
        survivor_id, survivor_key = self._add_record(
            "surviving legacy row that the snapshot seed must carry")
        keys.add(survivor_key)
        for record_id in deleted:
            result = self.run_mem("delete", record_id, "--force",
                                  json_output=False)
            self.assertEqual(result.returncode, 0,
                             result.stdout + result.stderr)

        # Strip the capsule columns from one logged row the way a pre-capsule
        # deletion log looks, and prove the seed still reconstructs it.
        log = self.store / "deleted-records.jsonl"
        rows = [json.loads(line) for line in
                log.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(rows), len(deleted))
        for row in rows:
            if row["id"] == deleted[0]:
                for column in ("canonical_id", "status", "capsule_version",
                               "delivery_state", "headline"):
                    row.pop(column, None)
        log.write_text("".join(json.dumps(row) + "\n" for row in rows),
                       encoding="utf-8")

        replica = self._reset_capture()
        expect, membership, snapshot = self._seal_through_snapshot(
            replica, keys, "multi-graveyard")
        source = self.root / "multi-graveyard-source.json"
        source.write_text(canonical({"source_identities": [survivor_id]}),
                          encoding="utf-8")
        seed_out = self.root / "multi-graveyard-seed"
        result = self.run_mem(
            "migration", "seed", "build", "--epoch", EPOCH, "--expect", expect,
            "--membership", str(membership), "--snapshot", str(snapshot),
            "--kind", "snapshot", "--source", str(source),
            "--out", str(seed_out), "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        seed = migration_v2.verify_seed_manifest(seed_out / "seed.json")
        identities = {row["source_identity"] for row in seed["mappings"]}
        for record_id in deleted:
            self.assertIn(f"graveyard:{record_id}:prior", identities)
            self.assertIn(f"graveyard:{record_id}:tombstone", identities)
        envelopes = [json.loads((seed_out / row["path"]).read_text())
                     for row in seed["objects"]]
        protocol = __import__("protocol_v2")
        folded = protocol.fold_operations(envelopes)
        self.assertEqual(folded.blocked, {})
        self.assertEqual(set(folded.records), {survivor_id})
        self.assertEqual(len(folded.tombstones), len(deleted))
        kinds = {envelope["payload"]["kind"] for envelope in envelopes}
        self.assertIn("force-tombstone", kinds)

    def test_refused_seed_namespace_leaves_no_operations(self):
        """A roster refusal must not leave the operations it refused behind."""
        record_id, project_key = self._add_record(
            "legacy row whose project is absent from the sealed roster")
        replica = self._reset_capture()
        expect, membership, snapshot = self._seal_through_snapshot(
            replica, {project_key}, "refused-namespace")
        # Seal a roster the snapshot's own project is absent from.
        foreign = json.loads(Path(membership).read_text(encoding="utf-8"))
        for member in foreign["members"]:
            member["logical_project_keys"] = ["git:example.invalid/other"]
        foreign.pop("manifest_digest", None)
        foreign_path = self.root / "foreign-membership.json"
        foreign_path.write_text(canonical(
            migration_v2._with_digest(foreign)), encoding="utf-8")
        source = self.root / "refused-source.json"
        source.write_text(canonical({"source_identities": [record_id]}),
                          encoding="utf-8")
        result = self.run_mem(
            "migration", "seed", "build", "--epoch", EPOCH, "--expect", expect,
            "--membership", str(foreign_path), "--snapshot", str(snapshot),
            "--kind", "snapshot", "--source", str(source),
            "--out", str(self.root / "refused-seed"), "--apply")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM sync_objects").fetchone()[0], 0)
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM sync_frontier").fetchone()[0], 0)
            self.assertEqual(con.execute(
                "SELECT counter FROM sync_replica WHERE active=1"
            ).fetchone()[0], "0")
        finally:
            con.close()

    def test_snapshot_seed_rejects_namespace_without_reserving(self):
        con = sqlite3.connect(self.db)
        sync_v2.register_writer_functions(con)
        try:
            con.execute("INSERT INTO records(id,tier,scope,type,body,cwd_origin) "
                        "VALUES('legacy-global','durable','global','note',"
                        "'legacy global durable body','global')")
            con.commit()
        finally:
            con.close()
        prepared = self.prepare_snapshot(
            "legacy-global", ["project-a"], prefix="namespace")
        con = sqlite3.connect(self.db)
        try:
            before = (con.execute(
                "SELECT counter FROM sync_replica WHERE active=1").fetchone()[0],
                con.execute("SELECT COUNT(*) FROM sync_migration_seed_reservations"
                            ).fetchone()[0],
                con.execute("SELECT COUNT(*) FROM sync_migration_seed_map"
                            ).fetchone()[0])
        finally:
            con.close()
        seed_out = self.root / "namespace-seed"
        result = self.run_mem(
            "migration", "seed", "build", "--epoch", EPOCH,
            "--expect", prepared["expect"],
            "--membership", str(prepared["membership"]),
            "--snapshot", str(prepared["snapshot"]), "--kind", "snapshot",
            "--source", str(prepared["source"]), "--out", str(seed_out),
            "--apply")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.payload(result)["reason"],
                         "seed-namespace-outside-membership")
        self.assertFalse(seed_out.exists())
        con = sqlite3.connect(self.db)
        try:
            after = (con.execute(
                "SELECT counter FROM sync_replica WHERE active=1").fetchone()[0],
                con.execute("SELECT COUNT(*) FROM sync_migration_seed_reservations"
                            ).fetchone()[0],
                con.execute("SELECT COUNT(*) FROM sync_migration_seed_map"
                            ).fetchone()[0])
        finally:
            con.close()
        self.assertEqual(before, after)

    def test_post_snapshot_tail_blocks_seed_before_counter_reservation(self):
        con = sqlite3.connect(self.db)
        sync_v2.register_writer_functions(con)
        try:
            con.execute("INSERT INTO records(id,tier,scope,type,body,cwd_origin) "
                        "VALUES('legacy-global','durable','global','note',"
                        "'legacy global durable body','global')")
            con.commit()
        finally:
            con.close()
        prepared = self.prepare_snapshot(
            "legacy-global", ["global"], prefix="post-snapshot-tail")
        tail = self.run_mem("add", "durable", "note",
            "post snapshot mutation that must remain captured",
            "--scope", "global", json_output=False)
        self.assertEqual(tail.returncode, 0, tail.stdout + tail.stderr)
        con = sqlite3.connect(self.db)
        try:
            before = (con.execute(
                "SELECT counter FROM sync_replica WHERE active=1").fetchone()[0],
                con.execute("SELECT COUNT(*) FROM sync_migration_seed_reservations"
                            ).fetchone()[0],
                con.execute("SELECT COUNT(*) FROM sync_migration_seed_map"
                            ).fetchone()[0],
                con.execute("SELECT COUNT(*) FROM sync_objects").fetchone()[0])
        finally:
            con.close()
        seed_out = self.root / "post-snapshot-tail-seed"
        result = self.run_mem(
            "migration", "seed", "build", "--epoch", EPOCH,
            "--expect", prepared["expect"],
            "--membership", str(prepared["membership"]),
            "--snapshot", str(prepared["snapshot"]), "--kind", "snapshot",
            "--source", str(prepared["source"]), "--out", str(seed_out),
            "--apply")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.payload(result)["reason"],
                         "snapshot-tail-before-seed")
        self.assertFalse(seed_out.exists())
        con = sqlite3.connect(self.db)
        try:
            after = (con.execute(
                "SELECT counter FROM sync_replica WHERE active=1").fetchone()[0],
                con.execute("SELECT COUNT(*) FROM sync_migration_seed_reservations"
                            ).fetchone()[0],
                con.execute("SELECT COUNT(*) FROM sync_migration_seed_map"
                            ).fetchone()[0],
                con.execute("SELECT COUNT(*) FROM sync_objects").fetchone()[0])
        finally:
            con.close()
        self.assertEqual(before, after)

    def test_fence_blocks_current_and_old_writers_but_allows_authority(self):
        first = self.run_mem("add", "durable", "note", "before fence",
                             "--scope", "global", json_output=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        con = sqlite3.connect(self.db)
        sync_v2.register_writer_functions(con)
        try:
            current = sync_v2.migration_status(con, EPOCH)
            membership = "1" * 64
            for target in sync_v2.MIGRATION_PHASES[1:8]:
                con.execute("BEGIN IMMEDIATE")
                kwargs = {}
                if target == "membership-sealed":
                    kwargs["membership_digest"] = membership
                if target == "old-writers-fenced":
                    kwargs.update(writer_mode="fenced", fence_capture_seq=0)
                sync_v2.migration_transition(
                    con, epoch_id=EPOCH, phase=f"test.{target}",
                    target_state=target, expect_digest=current["state_digest"],
                    input_digest=hashlib.sha256(target.encode()).hexdigest(), **kwargs)
                if target == "old-writers-fenced":
                    sync_v2.install_writer_fence(con, EPOCH)
                con.commit()
                current = sync_v2.migration_status(con, EPOCH)
            count = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        finally:
            con.close()

        blocked = self.run_mem("add", "durable", "note", "blocked after fence",
                               "--scope", "global", json_output=False)
        self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
        self.assertIn("writer-fenced", blocked.stderr)

        old = sqlite3.connect(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                old.execute("UPDATE records SET body='old writer' WHERE 1")
            old.rollback()
        finally:
            old.close()

        authority = sqlite3.connect(self.db)
        sync_v2.register_writer_functions(authority, cutover_authority=True)
        try:
            authority.execute("BEGIN IMMEDIATE")
            authority.execute("UPDATE records SET body=body WHERE 1")
            authority.commit()
        finally:
            authority.close()
        check = sqlite3.connect(self.db)
        try:
            self.assertEqual(check.execute("SELECT COUNT(*) FROM records").fetchone()[0], count)
        finally:
            check.close()

    def test_public_cutover_e2e_through_v2_activation(self):
        body = "legacy migration body with durable context"
        created = self.run_mem("add", "durable", "note", body,
                               "--scope", "project", json_output=False)
        self.assertEqual(created.returncode, 0, created.stderr)
        deleted_body = "legacy deleted context retained for recovery"
        created_deleted = self.run_mem(
            "add", "durable", "note", deleted_body,
            "--scope", "project", json_output=False)
        self.assertEqual(created_deleted.returncode, 0, created_deleted.stderr)
        con = sqlite3.connect(self.db)
        try:
            legacy_id, project_key = con.execute(
                "SELECT id,cwd_origin FROM records WHERE body=?", (body,)
            ).fetchone()
            deleted_id, deleted_project_key = con.execute(
                "SELECT id,cwd_origin FROM records WHERE body=?", (deleted_body,)
            ).fetchone()
            self.assertTrue(project_key)
            self.assertEqual(deleted_project_key, project_key)
        finally:
            con.close()
        deleted = self.run_mem(
            "delete", deleted_id, "--force", json_output=False)
        self.assertEqual(deleted.returncode, 0,
                         deleted.stdout + deleted.stderr)
        legacy_graveyard_raw = (self.store / "deleted-records.jsonl").read_bytes()
        con = sqlite3.connect(self.db)
        try:
            # Model an imported v8 row: retain its canonical project metadata,
            # and raw legacy deletion log, but remove all v2 setup objects.
            for table in ("sync_transactional_graveyard", "sync_graveyard",
                          "sync_outbox", "sync_applied", "sync_frontier",
                          "sync_parents", "sync_objects"):
                con.execute(f'DELETE FROM "{table}"')
            con.execute("UPDATE sync_replica SET counter='0' WHERE active=1")
            con.execute("UPDATE sync_capture_clock SET capture_seq='0' WHERE singleton=1")
            con.commit()
            replica = con.execute(
                "SELECT replica_id FROM sync_replica WHERE active=1").fetchone()[0]
        finally:
            con.close()

        capability_path = self.root / "capability.json"
        capability = self.save_payload(self.run_mem(
            "migration", "capabilities", "--epoch", EPOCH), capability_path)
        member_path = self.root / "member.json"
        protected_ref = "refs/heads/hearting-memory-v2"
        member_path.write_text(canonical({
            "replica_id": replica, "logical_project_keys": [project_key],
            "protected_ref": protected_ref,
            "writer_capability_hash": capability["writer_capability_hash"],
        }), encoding="utf-8")
        expect = self.payload(self.run_mem(
            "migration", "status", "--epoch", EPOCH))["state_digest"]

        membership_out = self.root / "membership"
        receipt = self.payload(self.run_mem(
            "migration", "roster", "membership-seal", "--epoch", EPOCH,
            "--expect", expect, "--member", str(member_path),
            "--out", str(membership_out), "--apply"))
        expect = self.next_expect(receipt)
        membership_path = membership_out / "membership.json"

        snapshot_out = self.root / "snapshot"
        receipt = self.payload(self.run_mem(
            "migration", "snapshot", "--epoch", EPOCH, "--expect", expect,
            "--membership", str(membership_path), "--replica", replica,
            "--store", str(self.store), "--out", str(snapshot_out), "--apply"))
        expect = self.next_expect(receipt)
        pre_snapshot_write = self.run_mem(
            "reinforce", legacy_id, json_output=False)
        self.assertEqual(pre_snapshot_write.returncode, 0,
                         pre_snapshot_write.stdout + pre_snapshot_write.stderr)
        receipt = self.payload(self.run_mem(
            "migration", "snapshot", "--epoch", EPOCH, "--expect", expect,
            "--membership", str(membership_path), "--replica", replica,
            "--store", str(self.store), "--out", str(snapshot_out), "--apply"))
        expect = self.next_expect(receipt)
        snapshot_path = snapshot_out / "snapshot.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual((snapshot_out / "graveyard"
                          / "legacy-deleted-records.jsonl").read_bytes(),
                         legacy_graveyard_raw)

        source_path = self.root / "seed-source.json"
        source_path.write_text(canonical({"source_identities": [legacy_id]}),
                               encoding="utf-8")
        seed_out = self.root / "seed"
        seed_result = self.run_mem(
            "migration", "seed", "build", "--epoch", EPOCH,
            "--expect", expect, "--membership", str(membership_path),
            "--snapshot", str(snapshot_path), "--kind", "snapshot",
            "--source", str(source_path), "--out", str(seed_out), "--apply")
        self.assertEqual(seed_result.returncode, 0,
                         seed_result.stdout + seed_result.stderr)
        receipt = self.payload(seed_result)
        expect = self.next_expect(receipt)
        seed_path = seed_out / "seed.json"
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        grave_identities = {row["source_identity"] for row in seed["mappings"]
                            if row["source_identity"].startswith("graveyard:")}
        self.assertEqual(grave_identities, {
            f"graveyard:{deleted_id}:prior",
            f"graveyard:{deleted_id}:tombstone"})
        row_mapping = next(row for row in seed["mappings"]
                           if row["source_identity"] == legacy_id)
        row_object = next(row for row in seed["objects"]
                          if row["op_id"] == row_mapping["op_id"])
        raw_seed = json.loads((seed_out / row_object["path"]).read_text())
        self.assertEqual(raw_seed["payload"]["project_key"], project_key)
        self.assertEqual(len(raw_seed["payload"]["parents"]), 1)
        self.assertEqual(raw_seed["payload"]["frontiers"][0]["heads"],
                         raw_seed["payload"]["parents"])
        self.assertTrue(set(raw_seed["payload"]["parents"]) <= {
            row["op_id"] for row in seed["objects"]})

        reinforced = self.run_mem("reinforce", legacy_id, json_output=False)
        self.assertEqual(reinforced.returncode, 0,
                         reinforced.stdout + reinforced.stderr)

        receipt = self.payload(self.run_mem(
            "migration", "fence", "arm", "--epoch", EPOCH,
            "--expect", expect, "--membership", str(membership_path),
            "--capabilities", str(capability_path), "--apply"))
        expect = self.next_expect(receipt)
        barrier_path = self.root / "barrier-receipt.json"
        barrier = self.save_payload(self.run_mem(
            "migration", "barrier", "enter", "--epoch", EPOCH,
            "--expect", expect, "--replica", replica, "--apply"), barrier_path)
        expect = self.next_expect(barrier)
        fence_path = self.root / "fence-receipt.json"
        fence = self.save_payload(self.run_mem(
            "migration", "fence", "activate", "--epoch", EPOCH,
            "--expect", expect, "--membership", str(membership_path),
            "--barrier-receipt", str(barrier_path), "--apply"), fence_path)
        expect = self.next_expect(fence)

        delta_out = self.root / "delta"
        delta_result = self.run_mem(
            "migration", "delta", "drain", "--epoch", EPOCH,
            "--expect", expect, "--replica", replica,
            "--snapshot", str(snapshot_path), "--fence-receipt", str(fence_path),
            "--out", str(delta_out), "--apply")
        self.assertEqual(delta_result.returncode, 0,
                         delta_result.stdout + delta_result.stderr)
        receipt = self.payload(delta_result)
        expect = self.next_expect(receipt)
        delta_path = delta_out / "delta.json"
        delta = json.loads(delta_path.read_text(encoding="utf-8"))
        delta_seed_path = delta_out / "seed" / "seed.json"
        delta_seed = migration_v2.verify_seed_manifest(delta_seed_path)
        self.assertEqual(len(delta["entries"]), 1)
        self.assertEqual(len(delta["objects"]), 1)
        self.assertEqual(len(delta_seed["mappings"]), 1)
        self.assertEqual(len(delta_seed["objects"]), 1)
        no_tail_path = self.root / "no-tail.json"
        no_tail = self.save_payload(self.run_mem(
            "migration", "no-tail", "verify", "--epoch", EPOCH,
            "--replica", replica, "--snapshot", str(snapshot_path),
            "--delta", str(delta_path), "--fence-receipt", str(fence_path)),
            no_tail_path)
        self.assertTrue(no_tail["proven"])

        evidence_input = self.root / "replica-evidence.json"
        evidence_row = {
            "replica_id": replica,
            "membership_digest": snapshot["membership_digest"],
            "snapshot_digest": snapshot["manifest_digest"],
            "seed_digest": migration_v2.rollback_seed_set_digest(
                [seed_path, delta_seed_path]),
            "delta_digest": delta["manifest_digest"],
            "fence_digest": fence["receipt_digest"],
            "no_tail_digest": no_tail["manifest_digest"],
            "backup_digest": snapshot["backup"]["sha256"],
            "no_tail_report": str(no_tail_path),
        }
        evidence_row["equality_input_digest"] = \
            migration_v2.replica_equality_input_digest(epoch_id=EPOCH,
                **{key: value for key, value in evidence_row.items()
                   if key != "no_tail_report"})
        evidence_input.write_text(canonical(evidence_row), encoding="utf-8")
        evidence_out = self.root / "evidence"
        receipt = self.payload(self.run_mem(
            "migration", "roster", "evidence-seal", "--epoch", EPOCH,
            "--expect", expect, "--membership", str(membership_path),
            "--replica-evidence", str(evidence_input),
            "--out", str(evidence_out), "--apply"))
        expect = self.next_expect(receipt)
        receipt = self.payload(self.run_mem(
            "migration", "roster", "evidence-seal", "--epoch", EPOCH,
            "--expect", expect, "--membership", str(membership_path),
            "--replica-evidence", str(evidence_input),
            "--out", str(evidence_out), "--apply"))
        expect = self.next_expect(receipt)
        evidence_path = evidence_out / "evidence.json"

        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       text=True, capture_output=True)
        checkout = self.root / "exchange"
        self.env.update({"MEM_SYNC_REMOTE_URL": str(remote),
                         "MEM_SYNC_REF": protected_ref,
                         "MEM_SYNC_DIR": str(checkout)})
        receipt = self.payload(self.run_mem(
            "migration", "seed", "publish", "--epoch", EPOCH,
            "--expect", expect, "--evidence", str(evidence_path),
            "--seed-manifest", str(seed_path),
            "--seed-manifest", str(delta_seed_path),
            "--checkout", str(checkout), "--ref", protected_ref, "--apply"))
        expect = self.next_expect(receipt)
        receipt = self.payload(self.run_mem(
            "migration", "fold", "--epoch", EPOCH, "--expect", expect,
            "--evidence", str(evidence_path), "--checkout", str(checkout),
            "--apply"))
        expect = self.next_expect(receipt)

        con = sqlite3.connect(self.db)
        try:
            fold = con.execute(
                "SELECT accepted_set_digest,operation_tree_digest,materialized_digest "
                "FROM sync_migration_fold WHERE epoch_id=?", (EPOCH,)).fetchone()
        finally:
            con.close()
        tip = subprocess.run(["git", "ls-remote", str(remote), protected_ref],
                             check=True, text=True, capture_output=True).stdout.split()[0]
        shared = {field: hashlib.sha256(field.encode()).hexdigest()
                  for field in migration_v2._SHARED_FIELDS
                  if field.endswith("_digest") or field == "accepted_operation_set_digest"}
        shared.update({"accepted_operation_set_digest": fold[0],
                       "operation_tree_digest": fold[1],
                       "materialized_digest": fold[2],
                       "schema_version": 9, "canonicalizer_version": 1,
                       "reducer_version": 1, "exit_class": 0})
        report = {**shared, "replica_id": replica,
                  "applied_set_digest": fold[0], "capture_frontier": {replica: 0},
                  "seed_manifest_digest": seed["manifest_digest"],
                  "unbound_capture_count": 0, "unconfirmed_epoch_outbox_count": 0,
                  "fresh_remote_ref_oid": tip, "remote_operation_tree_digest": fold[1],
                  "local_materialized_digest": fold[2], "writer_mode": "fenced",
                  "report_digest": None}
        report["report_digest"] = migration_v2.replica_report_digest(report)
        report_path = self.root / "replica-report.json"
        report_path.write_text(canonical(report), encoding="utf-8")
        equality_path = self.root / "equality.json"
        equality = self.save_payload(self.run_mem(
            "migration", "compare", "--epoch", EPOCH,
            "--evidence", str(evidence_path), "--report", str(report_path),
            "--ref", protected_ref), equality_path)
        self.assertTrue(equality["equal"])

        activate_result = self.run_mem(
            "migration", "activate", "--epoch", EPOCH, "--expect", expect,
            "--equality", str(equality_path), "--fence-receipt", str(fence_path),
            "--apply")
        self.assertEqual(activate_result.returncode, 0,
                         activate_result.stdout + activate_result.stderr)
        receipt = self.payload(activate_result)
        expect = self.next_expect(receipt)
        receipt = self.payload(self.run_mem(
            "migration", "activate", "--epoch", EPOCH, "--expect", expect,
            "--equality", str(equality_path), "--fence-receipt", str(fence_path),
            "--apply"))
        self.assertEqual(receipt["migration_state"], "v2-only-enabled")
        expect = self.next_expect(receipt)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute(
                "SELECT writer_mode FROM sync_migration_state WHERE epoch_id=?",
                (EPOCH,)).fetchone()[0], "v2")
            row = con.execute(
                "SELECT body,strength FROM records WHERE id=?", (legacy_id,)).fetchone()
            self.assertEqual(row, (body, 3))
            self.assertIsNone(con.execute(
                "SELECT 1 FROM records WHERE id=?", (deleted_id,)).fetchone())
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM sync_graveyard WHERE record_id=? "
                "AND effective=1", (deleted_id,)).fetchone()[0], 1)
        finally:
            con.close()

        rollback_out = self.root / "rollback"
        barrier_result = self.run_mem(
            "migration", "rollback", "prepare", "--epoch", EPOCH,
            "--expect", expect, "--equality", str(equality_path),
            "--out", str(rollback_out), "--apply")
        self.assertEqual(barrier_result.returncode, 0,
                         barrier_result.stdout + barrier_result.stderr)
        rollback_barrier = self.payload(barrier_result)
        self.assertEqual(rollback_barrier["migration_state"], "rollback-window")
        expect = self.next_expect(rollback_barrier)
        con = sqlite3.connect(self.db)
        try:
            activation_path = Path(con.execute(
                "SELECT local_path FROM sync_migration_artifacts "
                "WHERE epoch_id=? AND artifact_kind='activation'",
                (EPOCH,)).fetchone()[0])
        finally:
            con.close()
        held_path = activation_path.with_suffix(".held")
        activation_path.rename(held_path)
        try:
            incomplete = self.run_mem(
                "migration", "rollback", "prepare", "--epoch", EPOCH,
                "--expect", expect, "--equality", str(equality_path),
                "--out", str(rollback_out), "--apply")
            self.assertEqual(incomplete.returncode, 2,
                             incomplete.stdout + incomplete.stderr)
            self.assertEqual(self.payload(incomplete)["reason"],
                             "rollback-artifact-file-missing")
            self.assertFalse(rollback_out.exists())
            con = sqlite3.connect(self.db)
            try:
                self.assertEqual(con.execute(
                    "SELECT COUNT(*) FROM sync_migration_rollback"
                ).fetchone()[0], 0)
            finally:
                con.close()
        finally:
            held_path.rename(activation_path)
        prepare_result = self.run_mem(
            "migration", "rollback", "prepare", "--epoch", EPOCH,
            "--expect", expect, "--equality", str(equality_path),
            "--out", str(rollback_out), "--apply")
        self.assertEqual(prepare_result.returncode, 0,
                         prepare_result.stdout + prepare_result.stderr)
        bundle = migration_v2.verify_rollback_bundle(
            rollback_out, require_complete=True)
        self.assertTrue(bundle["complete"])
        bundled_graveyard = json.loads(
            (rollback_out / "state" / "graveyard.json").read_text())
        legacy_source = bundled_graveyard["legacy_sources"][0]
        self.assertEqual(bytes.fromhex(legacy_source["raw_hex"]),
                         legacy_graveyard_raw)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute(
                "SELECT state FROM sync_migration_rollback WHERE epoch_id=?",
                (EPOCH,)).fetchone()[0], "prepared")
            self.assertEqual(con.execute(
                "SELECT writer_mode FROM sync_migration_state WHERE epoch_id=?",
                (EPOCH,)).fetchone()[0], "fenced")
        finally:
            con.close()

        projection_out = self.root / "rollback-v1"
        exported = self.run_mem(
            "migration", "rollback", "export-v1", "--epoch", EPOCH,
            "--expect", expect, "--bundle", str(rollback_out),
            "--out", str(projection_out), "--apply")
        self.assertEqual(exported.returncode, 0,
                         exported.stdout + exported.stderr)
        projection_path = projection_out / "v1-export.json"
        projection = migration_v2.verify_v1_projection(projection_path)
        self.assertTrue(projection["representable"])

        install_out = self.root / "rollback-install"
        request_payload = {"schema_version": 1, "protocol_major": 2,
            "kind": "rollback-target-request", "epoch_id": EPOCH,
            "replica_id": replica, "store": str(self.db.resolve()),
            "projection": str(projection_path.resolve()),
            "install_out": str(install_out.resolve())}
        target_request = {**request_payload,
            "manifest_digest": migration_v2.digest_json(request_payload)}
        target_request_path = self.root / "rollback-target-request.json"
        target_request_path.write_text(canonical(target_request), encoding="utf-8")

        con = sqlite3.connect(self.db)
        try:
            con.execute("CREATE TRIGGER injected_rollback_apply_failure "
                        "BEFORE INSERT ON sync_migration_rollback_targets "
                        "BEGIN SELECT RAISE(ABORT,'injected-receipt-failure'); END")
            con.commit()
        finally:
            con.close()
        interrupted = self.run_mem(
            "migration", "rollback", "apply", "--epoch", EPOCH,
            "--expect", expect, "--bundle", str(rollback_out),
            "--target", str(target_request_path), "--apply")
        self.assertEqual(interrupted.returncode, 2,
                         interrupted.stdout + interrupted.stderr)
        self.assertTrue((install_out / "install-result.json").is_file(),
                        interrupted.stdout + interrupted.stderr + repr(
                            sorted(path.name for path in install_out.glob("*"))))
        self.assertTrue((install_out.parent / f"{install_out.name}.target"
                         / "target.json").is_file())
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM sync_migration_rollback_targets"
            ).fetchone()[0], 0)
            con.execute("DROP TRIGGER injected_rollback_apply_failure")
            con.commit()
        finally:
            con.close()

        apply_receipt_path = self.root / "rollback-apply-receipt.json"
        apply_receipt = self.save_payload(self.run_mem(
            "migration", "rollback", "apply", "--epoch", EPOCH,
            "--expect", expect, "--bundle", str(rollback_out),
            "--target", str(target_request_path), "--apply"),
            apply_receipt_path)
        self.assertEqual(apply_receipt["phase"], "rollback.apply")

        missing = self.run_mem(
            "migration", "rollback", "close", "--epoch", EPOCH,
            "--expect", expect, "--bundle", str(rollback_out), "--apply")
        self.assertEqual(missing.returncode, 2)
        duplicate = self.run_mem(
            "migration", "rollback", "close", "--epoch", EPOCH,
            "--expect", expect, "--bundle", str(rollback_out),
            "--apply-receipt", str(apply_receipt_path),
            "--apply-receipt", str(apply_receipt_path), "--apply")
        self.assertEqual(duplicate.returncode, 2,
                         duplicate.stdout + duplicate.stderr)
        self.assertEqual(self.payload(duplicate)["reason"],
                         "rollback-close-receipt-roster-mismatch")
        for field, value in (("epoch_id", "fedcba9876543210fedcba9876543210"),
                             ("bundle_digest", "9" * 64)):
            bad = dict(apply_receipt)
            bad[field] = value
            unsigned = dict(bad); unsigned.pop("receipt_digest")
            bad["receipt_digest"] = migration_v2.digest_json(unsigned)
            bad_path = self.root / f"bad-{field}-receipt.json"
            bad_path.write_text(canonical(bad), encoding="utf-8")
            rejected = self.run_mem(
                "migration", "rollback", "close", "--epoch", EPOCH,
                "--expect", expect, "--bundle", str(rollback_out),
                "--apply-receipt", str(bad_path), "--apply")
            self.assertEqual(rejected.returncode, 2,
                             rejected.stdout + rejected.stderr)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute(
                "SELECT phase FROM sync_migration_state WHERE epoch_id=?",
                (EPOCH,)).fetchone()[0], "rollback-window")
        finally:
            con.close()

        closed = self.run_mem(
            "migration", "rollback", "close", "--epoch", EPOCH,
            "--expect", expect, "--bundle", str(rollback_out),
            "--apply-receipt", str(apply_receipt_path), "--apply")
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        close_receipt = self.payload(closed)
        self.assertEqual(close_receipt["migration_state"], "closed")
        retry = self.run_mem(
            "migration", "rollback", "close", "--epoch", EPOCH,
            "--expect", expect, "--bundle", str(rollback_out),
            "--apply-receipt", str(apply_receipt_path), "--apply")
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertEqual(self.payload(retry), close_receipt)
        status_result = self.run_mem("migration", "status", "--epoch", EPOCH)
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        terminal = self.payload(status_result)
        self.assertEqual(terminal["reason"], "rollback-closed-v1-only")
        self.assertFalse(terminal["writer_allowed"])
        self.assertTrue(terminal["rollback"]["complete"])
        self.assertEqual(terminal["rollback"]["state"], "closed")
        current_v2 = self.run_mem(
            "add", "durable", "note", "v2 must remain fenced after rollback",
            "--scope", "global", json_output=False)
        self.assertNotEqual(current_v2.returncode, 0)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute(
                "SELECT phase,writer_mode FROM sync_migration_state "
                "WHERE epoch_id=?", (EPOCH,)).fetchone(), ("closed", "fenced"))
            self.assertEqual(con.execute(
                "SELECT state FROM sync_migration_rollback WHERE epoch_id=?",
                (EPOCH,)).fetchone()[0], "closed")
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'sync_cutover_records_%'").fetchone()[0], 0)
            con.execute("INSERT INTO records(id,tier,scope,type,body) "
                        "VALUES('old-v1-after-close','durable','global','note',"
                        "'old v1 writer is re-enabled after verified close')")
            con.commit()
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
