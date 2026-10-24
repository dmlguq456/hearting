"""Repository identity, normal CLI rekey, and cross-replica compatibility."""
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

from helpers import git, load_module
import test_mem_sync_v2 as fixtures


OLD = "git:github.com/example/old-name"
NEW = "git:github.com/example/new-name"


class RepositoryRenameTest(unittest.TestCase):
    setUp = fixtures.MemTwoServerSyncTest.setUp
    tearDown = fixtures.MemTwoServerSyncTest.tearDown
    _mem = fixtures.MemTwoServerSyncTest._mem
    _activate_fresh = fixtures.MemTwoServerSyncTest._activate_fresh
    _sync = fixtures.MemTwoServerSyncTest._sync
    _rows = fixtures.MemTwoServerSyncTest._rows
    _written_id = staticmethod(fixtures.MemTwoServerSyncTest._written_id)
    _digests = fixtures.MemTwoServerSyncTest._digests

    def _environment(self, name, *, remote=False):
        env = fixtures.MemTwoServerSyncTest._environment(self, name, remote=remote)
        if hasattr(self, "bin"):
            env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
        return env

    def _repository(self, *, same=True):
        self.bin = self.root / "bin"
        self.bin.mkdir()
        values = {"repos/example/old-name": {"id": 101, "full_name": "example/new-name"},
                  "repos/example/new-name": {"id": 101 if same else 202,
                                             "full_name": "example/new-name"}}
        (self.bin / "repos.json").write_text(json.dumps(values))
        program = ("#!" + sys.executable + "\nimport json,sys\nfrom pathlib import Path\n"
                   "data=json.loads(Path(__file__).with_name('repos.json').read_text())\n"
                   "value=data.get(sys.argv[2])\n"
                   "if value is None: sys.exit(1)\nprint(json.dumps(value))\n")
        (self.bin / "gh").write_text(program)
        (self.bin / "gh").chmod(0o700)
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/old-name.git")

    def _add(self, name="a", *, pending=False, body="Repository memory survives a rename"):
        args = ["add", "durable", "decision", body, "--headline", "Repository rename"]
        if pending:
            args.append("--requires-consume")
        result = self._mem(name, *args)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return self._written_id(result)

    def _states(self, name):
        with sqlite3.connect(self.stores[name] / "memory.db") as con:
            return con.execute("SELECT id,cwd_origin,body,delivery_state FROM records ORDER BY id").fetchall()

    def _operations(self, name):
        protocol = load_module("protocol_v2")
        with sqlite3.connect(self.stores[name] / "memory.db") as con:
            return [{"op_id": rid, "payload": protocol.canonical_loads(bytes(raw))}
                    for rid, raw in con.execute("SELECT op_id,payload_bytes FROM sync_objects")]

    def test_normal_sync_moves_records_and_frontiers_and_preserves_pending_on_two_servers(self):
        self._repository()
        rid = self._add(pending=True)
        self._sync("a")
        self._sync("b")
        before = self._states("a")
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        # A record already written under the new address must also retain its
        # existing lineage when later writes use the unified project identity.
        newer = self._add(body="Already created after changing the repository origin")
        self._sync("a")
        self._sync("b")
        self._sync("a")
        self.assertEqual(self._states("a"), self._states("b"))
        states = {row[0]: row for row in self._states("a")}
        self.assertEqual(states[rid], (rid, NEW, before[0][2], "pending"))
        for name in ("a", "b"):
            with sqlite3.connect(self.stores[name] / "memory.db") as con:
                self.assertEqual({row[0] for row in con.execute("SELECT project_key FROM sync_frontier")}, {NEW})
        self.assertTrue(list(self.stores["a"].glob("memory-rekey-*.bak")))
        # Existing ID remains the normal consume address after the rename.
        result = self._mem("a", "consume", rid)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        result = self._mem("a", "reinforce", newer)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        added_after = self._add(body="New memory authored after the project rename was followed")
        self._sync("a")
        self._sync("b")
        self._sync("a")
        self.assertEqual(self._states("a"), self._states("b"))
        self.assertEqual(self._digests("a"), self._digests("b"))
        self.assertIn(added_after, {row[0] for row in self._states("b")})
        # A down-level reader quarantines the new lineage, preserving its
        # previous records instead of misclassifying it as project corruption.
        protocol = load_module("protocol_v2")
        with mock.patch.object(protocol, "SUPPORTED_SCHEMA_MINORS", frozenset({0})):
            legacy = protocol.fold_operations(self._operations("a"))
        self.assertFalse(legacy.classification.hard_failures)
        self.assertTrue(legacy.classification.quarantined)
        self.assertEqual(legacy.records[rid]["cwd_origin"], OLD)
        self.assertEqual(legacy.records[rid]["delivery_state"], "pending")

    def test_existing_reattribute_follows_verified_repository_rename_for_whole_project(self):
        self._repository()
        first = self._add(pending=True)
        second = self._add(body="Second old repository record remains unchanged")
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        result = self._mem("a", "reattribute", first)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual({row[0] for row in self._states("a")}, {first, second})
        self.assertEqual({row[1] for row in self._states("a")}, {NEW})
        backup = next(self.stores["a"].glob("memory-rekey-*.bak"))
        with sqlite3.connect(backup) as con:
            self.assertEqual({row[0] for row in con.execute("SELECT cwd_origin FROM records")}, {OLD})
        count = len(self._operations("a"))
        self._sync("a")
        self.assertEqual(len(self._operations("a")), count)

    def test_migration_undo_moves_only_its_records_and_keeps_current_identity(self):
        self._repository()
        ordinary = self._mem(
            "a", "add", "durable", "decision", "oldneedle ordinary body",
            "--headline", "oldneedle ordinary memory")
        self.assertEqual(ordinary.returncode, 0, ordinary.stderr + ordinary.stdout)
        ordinary_id = self._written_id(ordinary)
        pending = self._mem(
            "a", "add", "durable", "decision", "pendingneedle protected body",
            "--headline", "pendingneedle protected memory", "--requires-consume")
        self.assertEqual(pending.returncode, 0, pending.stderr + pending.stdout)
        pending_id = self._written_id(pending)
        global_result = self._mem(
            "a", "add", "durable", "decision", "global sentinel body",
            "--headline", "global sentinel", "--scope", "global")
        self.assertEqual(global_result.returncode, 0, global_result.stderr + global_result.stdout)
        global_id = self._written_id(global_result)
        other_project = self.root / "other-project"
        other_project.mkdir()
        git(other_project, "init")
        git(other_project, "remote", "add", "origin",
            "https://github.com/example/unrelated.git")
        other = subprocess.run(
            [sys.executable, str(fixtures.MEM), "add", "durable", "decision",
             "other project sentinel body", "--headline", "other project sentinel"],
            cwd=other_project, env=self._environment("a"), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(other.returncode, 0, other.stderr + other.stdout)
        other_id = self._written_id(other)

        git(self.project, "remote", "set-url", "origin",
            "https://github.com/example/new-name.git")
        preexisting = self._mem(
            "a", "add", "durable", "decision", "new address existing body",
            "--headline", "new address sentinel")
        self.assertEqual(preexisting.returncode, 0, preexisting.stderr + preexisting.stdout)
        preexisting_id = self._written_id(preexisting)
        self.assertNotIn(ordinary_id, self._mem("a", "recall", "oldneedle").stdout)
        self.assertNotIn(ordinary_id, self._mem("a", "candidates", "oldneedle").stdout)

        applied = self._mem("a", "reattribute", ordinary_id, "--json")
        self.assertEqual(applied.returncode, 0, applied.stderr + applied.stdout)
        apply_receipt = json.loads(applied.stdout)
        self.assertEqual(apply_receipt["state"], "applied")
        self.assertEqual(set(apply_receipt["record_ids"]), {ordinary_id, pending_id})
        self.assertIn(ordinary_id, self._mem("a", "recall", "oldneedle").stdout)
        self.assertIn(ordinary_id, self._mem("a", "candidates", "oldneedle").stdout)
        states = {row[0]: row for row in self._states("a")}
        self.assertEqual(states[ordinary_id][1], NEW)
        self.assertEqual(states[pending_id][1], NEW)
        self.assertEqual(states[pending_id][3], "pending")
        self.assertEqual(states[preexisting_id][1], NEW)
        self.assertEqual(states[global_id][1], "global")
        self.assertEqual(states[other_id][1], "git:github.com/example/unrelated")
        independent = self._add(body="new address write after apply remains independent")
        applied_op_count = len(self._operations("a"))
        repeated_apply = self._mem("a", "reattribute", ordinary_id, "--json")
        self.assertEqual(repeated_apply.returncode, 0,
                         repeated_apply.stderr + repeated_apply.stdout)
        self.assertEqual(json.loads(repeated_apply.stdout)["state"], "already-applied")
        self.assertEqual(len(self._operations("a")), applied_op_count)

        before_undo_ops = len(self._operations("a"))
        undone = self._mem("a", "reattribute", "--undo",
                           apply_receipt["migration_id"], "--json")
        self.assertEqual(undone.returncode, 0, undone.stderr + undone.stdout)
        undo_receipt = json.loads(undone.stdout)
        self.assertEqual(undo_receipt["state"], "undone")
        self.assertEqual(set(undo_receipt["record_ids"]), {ordinary_id, pending_id})
        self.assertEqual(len(self._operations("a")), before_undo_ops + 1)
        states = {row[0]: row for row in self._states("a")}
        self.assertEqual(states[ordinary_id][1], OLD)
        self.assertEqual(states[pending_id][1], OLD)
        self.assertEqual(states[pending_id][3], "pending")
        self.assertEqual(states[preexisting_id][1], NEW)
        self.assertEqual(states[independent][1], NEW)
        self.assertEqual(states[global_id][1], "global")
        self.assertEqual(states[other_id][1], "git:github.com/example/unrelated")
        self.assertNotIn(ordinary_id, self._mem("a", "recall", "oldneedle").stdout)

        repeat = self._mem("a", "reattribute", "--undo",
                           apply_receipt["migration_id"], "--json")
        self.assertEqual(repeat.returncode, 0, repeat.stderr + repeat.stdout)
        self.assertEqual(json.loads(repeat.stdout)["state"], "already-undone")
        self._mem("a", "sync", "--json")
        states = {row[0]: row for row in self._states("a")}
        self.assertEqual(states[ordinary_id][1], OLD)
        self.assertEqual(states[pending_id][1], OLD)
        self._sync("a")
        self._sync("b")
        self.assertEqual(self._states("a"), self._states("b"))
        rows_b = {row[0]: row for row in self._states("b")}
        self.assertEqual(rows_b[ordinary_id][1], OLD)
        self.assertEqual(rows_b[pending_id][1], OLD)
        self.assertEqual(rows_b[independent][1], NEW)
        reapplied = self._mem("a", "reattribute", ordinary_id, "--json")
        self.assertEqual(reapplied.returncode, 0, reapplied.stderr + reapplied.stdout)
        reapply_receipt = json.loads(reapplied.stdout)
        self.assertEqual(reapply_receipt["state"], "applied")
        self.assertNotEqual(reapply_receipt["migration_id"], apply_receipt["migration_id"])
        self._sync("a")
        self._sync("b")
        self.assertEqual(self._states("a"), self._states("b"))
        rows_b = {row[0]: row for row in self._states("b")}
        self.assertEqual(rows_b[ordinary_id][1], NEW)
        self.assertEqual(rows_b[pending_id][1], NEW)

    def test_761_record_migration_and_undo_use_six_bounded_batches(self):
        self._repository()
        base_id = self._add(pending=True, body="bulk migration seed body")
        git(self.project, "remote", "set-url", "origin",
            "https://github.com/example/new-name.git")
        new_base_id = self._add(body="preexisting destination seed body")
        script = f"""
import sys
sys.path.insert(0, {str(fixtures.MEMORY_DIR)!r})
import mem
con = mem.get_con()
try:
    columns = mem.RECORD_COLS
    old_base = dict(zip(columns, con.execute(
        "SELECT " + ",".join(columns) + " FROM records WHERE id=?", ({base_id!r},)
    ).fetchone()))
    new_base = dict(zip(columns, con.execute(
        "SELECT " + ",".join(columns) + " FROM records WHERE id=?", ({new_base_id!r},)
    ).fetchone()))
    rows = []
    grouped_ids = {{{OLD!r}: [], {NEW!r}: []}}
    for number in range(1, 761):
        rid = f"bulk-record-{{number:04d}}"
        row = dict(old_base)
        row.update({{
            "id": rid, "cwd_origin": {OLD!r}, "body": f"bulk preserved body {{number:04d}}",
            "source": f"bulk-fixture:{{number:04d}}", "headline": f"bulk headline {{number:04d}}",
            "aliases": "[]", "entities": "[]", "topics": "[]", "artifact_refs": "[]",
            "delivery_state": "pending" if number <= 34 else "ordinary",
            "status": "superseded" if number >= 759 else "active",
            "canonical_id": {base_id!r} if number >= 759 else rid,
            "superseded_by": {base_id!r} if number >= 759 else None,
        }})
        rows.append(tuple(row[column] for column in columns))
        grouped_ids[{OLD!r}].append(rid)
    for number in range(1, 1058):
        rid = f"destination-record-{{number:04d}}"
        row = dict(new_base)
        row.update({{
            "id": rid, "cwd_origin": {NEW!r}, "body": f"destination preserved body {{number:04d}}",
            "source": f"destination-fixture:{{number:04d}}",
            "headline": f"destination headline {{number:04d}}",
            "aliases": "[]", "entities": "[]", "topics": "[]", "artifact_refs": "[]",
            "delivery_state": "ordinary", "status": "active",
            "canonical_id": rid, "superseded_by": None,
        }})
        rows.append(tuple(row[column] for column in columns))
        grouped_ids[{NEW!r}].append(rid)
    con.execute("BEGIN IMMEDIATE")
    con.executemany(
        "INSERT INTO records (" + ",".join(columns) + ") VALUES (" +
        ",".join("?" for _ in columns) + ")", rows)
    for ids in grouped_ids.values():
        for start in range(0, len(ids), mem.protocol_v2.MAX_MUTATIONS):
            mem._capture_v2_operation(
                con, "put", post_ids=ids[start:start + mem.protocol_v2.MAX_MUTATIONS],
                reason="bounded synthetic migration fixture")
    con.commit()
finally:
    con.close()
"""
        seeded = subprocess.run(
            [sys.executable, "-c", script], cwd=self.project,
            env=self._environment("a"), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(seeded.returncode, 0, seeded.stderr + seeded.stdout)

        def snapshot():
            with sqlite3.connect(self.stores["a"] / "memory.db") as con:
                return con.execute(
                    "SELECT * FROM records WHERE cwd_origin IN (?,?) ORDER BY id",
                    (OLD, NEW)).fetchall()

        before = snapshot()
        self.assertEqual(len(before), 1819)
        old_ids = {row[0] for row in before if row[4] == OLD}
        new_ids = {row[0] for row in before if row[4] == NEW}
        self.assertEqual((len(old_ids), len(new_ids)), (761, 1058))
        before_digest = hashlib.sha256(json.dumps(
            [tuple((*row[:4], *row[5:])) for row in before],
            separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        applied = self._mem("a", "reattribute", base_id, "--json")
        self.assertEqual(applied.returncode, 0, applied.stderr + applied.stdout)
        apply_receipt = json.loads(applied.stdout)
        self.assertEqual(apply_receipt["record_count"], 761)
        self.assertEqual(len(apply_receipt["operation_ids"]), 6)
        apply_operations = [operation for operation in self._operations("a")
                            if operation["payload"]["kind"] == "rekey"
                            and json.loads(operation["payload"]["provenance"]["reason"])
                                .get("phase") == "apply"]
        self.assertEqual(len(apply_operations), 6)
        self.assertEqual(sorted(len(op["payload"]["mutations"]) for op in apply_operations),
                         [121, 128, 128, 128, 128, 128])
        self.assertEqual({json.loads(op["payload"]["provenance"]["reason"])
                          ["batch_count"] for op in apply_operations}, {6})
        with sqlite3.connect(apply_receipt["backup"]["path"]) as con:
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM records WHERE cwd_origin=?", (OLD,)).fetchone()[0], 761)
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM records WHERE cwd_origin=?", (NEW,)).fetchone()[0], 1058)

        moved = snapshot()
        self.assertEqual({row[4] for row in moved}, {NEW})
        self.assertEqual(hashlib.sha256(json.dumps(
            [tuple((*row[:4], *row[5:])) for row in moved],
            separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(), before_digest)
        self.assertEqual(sum(row[15] == "pending" for row in moved), 35)
        self.assertEqual(sum(row[21] == "superseded" for row in moved), 2)

        undone = self._mem("a", "reattribute", "--undo",
                           apply_receipt["migration_id"], "--json")
        self.assertEqual(undone.returncode, 0, undone.stderr + undone.stdout)
        undo_receipt = json.loads(undone.stdout)
        self.assertEqual(undo_receipt["record_count"], 761)
        self.assertEqual(len(undo_receipt["inverse_operation_ids"]), 6)
        restored = snapshot()
        restored_origins = {row[0]: row[4] for row in restored}
        self.assertTrue(all(restored_origins[rid] == OLD for rid in old_ids))
        self.assertTrue(all(restored_origins[rid] == NEW for rid in new_ids))
        self.assertEqual(hashlib.sha256(json.dumps(
            [tuple((*row[:4], *row[5:])) for row in restored],
            separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(), before_digest)

    def test_backup_failure_and_second_batch_failure_leave_no_partial_rekey(self):
        self._repository()
        base_id = self._add(body="atomic rekey seed")
        script = f"""
import sys
sys.path.insert(0, {str(fixtures.MEMORY_DIR)!r})
import mem
con = mem.get_con()
try:
    columns = mem.RECORD_COLS
    base = dict(zip(columns, con.execute(
        "SELECT " + ",".join(columns) + " FROM records WHERE id=?", ({base_id!r},)
    ).fetchone()))
    rows = []
    ids = []
    for number in range(128):
        rid = f"atomic-record-{{number:03d}}"
        row = dict(base)
        row.update({{"id": rid, "cwd_origin": {OLD!r},
                    "body": f"atomic body {{number:03d}}", "source": None,
                    "headline": f"atomic {{number:03d}}", "canonical_id": rid,
                    "superseded_by": None}})
        rows.append(tuple(row[column] for column in columns))
        ids.append(rid)
    con.execute("BEGIN IMMEDIATE")
    con.executemany(
        "INSERT INTO records (" + ",".join(columns) + ") VALUES (" +
        ",".join("?" for _ in columns) + ")", rows)
    mem._capture_v2_operation(con, "put", post_ids=ids, reason="atomic fixture")
    con.commit()
finally:
    con.close()
"""
        seeded = subprocess.run(
            [sys.executable, "-c", script], cwd=self.project,
            env=self._environment("a"), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(seeded.returncode, 0, seeded.stderr + seeded.stdout)

        def protocol_snapshot():
            with sqlite3.connect(self.stores["a"] / "memory.db") as con:
                state = {"records": con.execute("SELECT * FROM records ORDER BY id").fetchall()}
                for (table,) in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name LIKE 'sync_%' ORDER BY name"):
                    rows = con.execute(f' SELECT * FROM "{table}"').fetchall()
                    state[table] = sorted(rows, key=repr)
                return state

        before = protocol_snapshot()
        fail_capture = f"""
import sys
sys.path.insert(0, {str(fixtures.MEMORY_DIR)!r})
import mem
original = mem._capture_v2_operation
calls = [0]
def fail_second(*args, **kwargs):
    calls[0] += 1
    if calls[0] == 2:
        raise RuntimeError("injected second-batch failure")
    return original(*args, **kwargs)
mem._capture_v2_operation = fail_second
try:
    mem._rekey_project({OLD!r}, {NEW!r}, "github-repository-id:101")
except RuntimeError as exc:
    assert str(exc) == "injected second-batch failure"
else:
    raise SystemExit("second-batch injection did not fail")
"""
        result = subprocess.run(
            [sys.executable, "-c", fail_capture], cwd=self.project,
            env=self._environment("a"), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(protocol_snapshot(), before)

        fail_backup = f"""
import json,sys
sys.path.insert(0, {str(fixtures.MEMORY_DIR)!r})
import mem
def fail(*args, **kwargs):
    raise OSError("injected backup failure")
mem._repository_rekey_backup = fail
print(json.dumps(mem._rekey_project(
    {OLD!r}, {NEW!r}, "github-repository-id:101", return_result=True)))
"""
        result = subprocess.run(
            [sys.executable, "-c", fail_backup], cwd=self.project,
            env=self._environment("a"), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["reason"], "backup-failed")
        self.assertEqual(protocol_snapshot(), before)

    def test_undo_refuses_any_moved_record_change_without_partial_inverse(self):
        self._repository()
        changed = self._add(body="record changed after repository rekey")
        protected = self._add(pending=True, body="pending remains protected after rekey")
        git(self.project, "remote", "set-url", "origin",
            "https://github.com/example/new-name.git")
        applied = self._mem("a", "reattribute", changed, "--json")
        self.assertEqual(applied.returncode, 0, applied.stderr + applied.stdout)
        migration_id = json.loads(applied.stdout)["migration_id"]
        independent = self._add(body="unrelated current-address addition")
        reinforced = self._mem("a", "reinforce", changed)
        self.assertEqual(reinforced.returncode, 0, reinforced.stderr + reinforced.stdout)
        before_ops = len(self._operations("a"))

        undo = self._mem("a", "reattribute", "--undo", migration_id, "--json")
        self.assertEqual(undo.returncode, 1, undo.stderr + undo.stdout)
        receipt = json.loads(undo.stdout)
        self.assertEqual(receipt["state"], "refused")
        self.assertEqual(receipt["reason"], "affected-record-changed")
        self.assertEqual(len(self._operations("a")), before_ops)
        rows = {row[0]: row for row in self._states("a")}
        self.assertEqual(rows[changed][1], NEW)
        self.assertEqual(rows[protected][1], NEW)
        self.assertEqual(rows[protected][3], "pending")
        self.assertEqual(rows[independent][1], NEW)

    def test_different_repository_or_missing_evidence_is_not_moved(self):
        self._repository(same=False)
        rid = self._add()
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        before = self._states("a")
        self._sync("a")
        self.assertEqual(self._states("a"), before)
        result = self._mem("a", "reattribute", rid)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._states("a"), before)
        self.assertFalse(list(self.stores["a"].glob("memory-rekey-*.bak")))
        (self.bin / "repos.json").write_text("{}")
        self._sync("a")
        self.assertEqual(self._states("a"), before)

    def test_rekey_cannot_change_body_or_consume_an_obligation(self):
        self._repository()
        rid = self._add(pending=True)
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        result = self._mem("a", "reattribute", rid)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        protocol = load_module("protocol_v2")
        operations = self._operations("a")
        rekey = next(op for op in operations if op["payload"]["kind"] == "rekey")
        for field, value in (("body", "Forged new body"), ("delivery_state", "consumed")):
            payload = json.loads(json.dumps(rekey["payload"]))
            payload["mutations"][0]["post_state"][field] = value
            forged = protocol.build_operation(payload)
            folded = protocol.fold_operations([op for op in operations if op != rekey] + [forged])
            self.assertFalse(folded.classification.hard_failures)
            self.assertIn(forged["op_id"], folded.blocked)
            self.assertEqual(folded.records[rid]["cwd_origin"], OLD)

    def test_records_from_both_addresses_can_use_normal_supersede(self):
        self._repository()
        old_record = self._add(body="Old repository decision with a preserved history")
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        new_record = self._add(body="Replacement decision already authored under the new origin")
        self._sync("a")
        result = self._mem("a", "supersede", old_record, "--by", new_record)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self._sync("a")
        self._sync("b")
        self.assertEqual(self._states("a"), self._states("b"))
        protocol = load_module("protocol_v2")
        folded = protocol.fold_operations(self._operations("a"))
        self.assertFalse(folded.blocked)
        self.assertEqual(folded.records[old_record]["superseded_by"], new_record)
        with mock.patch.object(protocol, "SUPPORTED_SCHEMA_MINORS", frozenset({0})):
            legacy = protocol.fold_operations(self._operations("a"))
        self.assertFalse(legacy.classification.hard_failures)

    def test_repeated_rename_and_rename_back_preserve_lineage(self):
        self._repository()
        rid = self._add(pending=True)
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        self._sync("a")
        # GitHub reports the same repository renamed back to its original address.
        values = {"repos/example/old-name": {"id":101, "full_name":"example/old-name"},
                  "repos/example/new-name": {"id":101, "full_name":"example/old-name"}}
        (self.bin / "repos.json").write_text(json.dumps(values))
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/old-name.git")
        self._sync("a")
        result = self._mem("a", "consume", rid)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self._sync("a")
        self._sync("b")
        self.assertEqual(self._states("a"), self._states("b"))
        self.assertEqual(self._states("a")[0][1], OLD)

    def test_new_records_wait_for_successful_repository_identity(self):
        self._repository()
        first = self._add()
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        self.assertEqual(self._mem("a", "reattribute", first).returncode, 0)
        added = self._add(body="New record with a causal repository identity")
        protocol = load_module("protocol_v2")
        operations = self._operations("a")
        rekey = next(op for op in operations if op["payload"]["kind"] == "rekey")
        new = next(op for op in operations if op["payload"]["mutations"][0]["record_id"] == added)
        self.assertIn(rekey["op_id"], new["payload"]["namespace_parents"])
        without = protocol.fold_operations([op for op in operations if op != rekey])
        self.assertIn(new["op_id"], without.deferred)
        self.assertNotIn(added, without.records)
        payload = json.loads(json.dumps(rekey["payload"]))
        payload["mutations"][0]["post_state"]["body"] = "Invalid rename changes content"
        forged = protocol.build_operation(payload)
        payload = json.loads(json.dumps(new["payload"]))
        payload["namespace_parents"] = [forged["op_id"]]
        forged_new = protocol.build_operation(payload)
        folded = protocol.fold_operations([op for op in operations if op not in (rekey, new)]
                                          + [forged, forged_new])
        self.assertIn(forged_new["op_id"], folded.blocked)
        self.assertNotIn(added, folded.records)

    def test_delete_restore_uses_serving_project_after_rename(self):
        self._repository()
        rid = self._add()
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        self._sync("a")
        before = self._states("a")
        for command in ("delete", "restore"):
            result = self._mem("a", command, rid)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self._sync("a")
        self._sync("b")
        self.assertEqual(self._states("a"), before)
        self.assertEqual(self._states("a"), self._states("b"))

    def test_conflicts_after_rename_remain_visible_and_resolvable(self):
        self._repository()
        rid = self._add()
        self._sync("a")
        self._sync("b")
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        self._sync("a")
        self._sync("b")
        self._sync("a")
        self.assertEqual(self._mem("a", "reinforce", rid).returncode, 0)
        for _ in range(2):
            self.assertEqual(self._mem("b", "reinforce", rid).returncode, 0)
        self._sync("a")
        result = self._mem("b", "sync", "--json", remote=True)
        self.assertEqual(json.loads(result.stdout)["reason"], "unresolved-conflicts")
        listed = self._mem("b", "conflicts", "--json")
        self.assertEqual(json.loads(listed.stdout)["count"], 1)
        shown = self._mem("b", "show-conflict", rid, "--json")
        variants = json.loads(shown.stdout)["variants"]
        result = self._mem("b", "resolve", rid, "--parents", *[v["op_id"] for v in variants])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self._sync("b")
        self._sync("a")
        self.assertEqual(self._states("a"), self._states("b"))
        self.assertEqual(self._states("a")[0][1], NEW)

    def test_unresolved_old_conflict_does_not_stop_normal_sync(self):
        self._repository()
        rid = self._add()
        self._sync("a")
        self._sync("b")
        self._mem("a", "reinforce", rid)
        self._mem("b", "reinforce", rid)
        self._mem("b", "reinforce", rid)
        self._sync("a")
        result = self._mem("b", "sync", "--json", remote=True)
        self.assertEqual(json.loads(result.stdout)["reason"], "unresolved-conflicts")
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        result = self._mem("b", "sync", "--json", remote=True)
        self.assertEqual(json.loads(result.stdout)["reason"], "unresolved-conflicts")
        self.assertEqual(json.loads(result.stdout)["phases"]["remote-fetch-validate"], "ok")
        listed = self._mem("b", "conflicts", "--json")
        self.assertEqual(json.loads(listed.stdout)["count"], 1)
        shown = self._mem("b", "show-conflict", rid, "--json")
        variants = json.loads(shown.stdout)["variants"]
        result = self._mem("b", "resolve", rid, "--parents", *[v["op_id"] for v in variants])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self._sync("b")
        self._sync("a")
        self.assertEqual(self._states("a"), self._states("b"))


if __name__ == "__main__":
    unittest.main()
