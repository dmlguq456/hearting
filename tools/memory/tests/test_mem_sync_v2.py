#!/usr/bin/env python3
"""End-to-end, two-server checks through the public mem.py CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from helpers import MEMORY_DIR, git, init_bare, load_module


MEM = MEMORY_DIR / "mem.py"
REF = "refs/heads/memory-v2"


class MemTwoServerSyncTest(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.project = self.root / "project"
        self.project.mkdir()
        git(self.project, "init")
        git(self.project, "remote", "add", "origin", "https://example.invalid/team/project.git")
        (self.project / "README.md").write_text("project bytes must stay unchanged\n")
        self.remote = init_bare(self.root / "remote.git")
        self.stores = {name: self.root / f"store-{name}" for name in ("a", "b")}
        self.exchanges = {name: self.root / f"exchange-{name}.git" for name in ("a", "b")}
        self.projects_source = self.root / "runtime-projects"
        self.projects_source.mkdir()
        for name in self.stores:
            result = self._mem(name, "index", "--rebuild")
            self.assertEqual(result.returncode, 0, result.stderr)
            self._activate_fresh(name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _environment(self, name: str, *, remote: bool = False) -> dict[str, str]:
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("MEM_")
        }
        env.update({
            "AGENT_HOME": str(self.root / "agent-home"),
            "CODEX_SESSIONS": str(self.root / "codex-sessions"),
            "MEM_DUMP_COMMIT": "0",
            "MEM_PROJECTS": str(self.projects_source),
            "MEM_STORE": str(self.stores[name]),
            "XDG_STATE_HOME": str(self.root / f"state-{name}"),
        })
        if remote:
            env.update({
                "MEM_SYNC_DIR": str(self.exchanges[name]),
                "MEM_SYNC_REF": REF,
                "MEM_SYNC_REMOTE": "1",
                "MEM_SYNC_REMOTE_URL": str(self.remote),
            })
        return env

    def _mem(self, name: str, *args: str, remote: bool = False):
        return subprocess.run(
            [sys.executable, str(MEM), *args],
            cwd=self.project,
            env=self._environment(name, remote=remote),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )

    def _activate_fresh(self, name: str) -> None:
        sync = load_module("sync_v2")
        connection = sqlite3.connect(self.stores[name] / "memory.db")
        try:
            connection.execute("BEGIN IMMEDIATE")
            sync.initialize_fresh_v2_epoch(
                connection, "test-fresh-epoch", proof="empty-store-proof"
            )
            sync.activate_v2_only_fence(
                connection,
                "test-fresh-epoch",
                fence_proof="test-suite-v2-only-writer-proof",
                operator_authorized=True,
            )
            connection.commit()
        finally:
            connection.close()

    def _sync(self, name: str) -> dict:
        result = self._mem(name, "sync", "--json", remote=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def _rows(self, name: str) -> list[tuple[str, str]]:
        connection = sqlite3.connect(self.stores[name] / "memory.db")
        try:
            return connection.execute(
                "SELECT id,body FROM records ORDER BY id"
            ).fetchall()
        finally:
            connection.close()

    def _record_states(self, name: str) -> list[tuple]:
        connection = sqlite3.connect(self.stores[name] / "memory.db")
        try:
            return connection.execute(
                "SELECT id,body,status,canonical_id,superseded_by,strength "
                "FROM records ORDER BY id"
            ).fetchall()
        finally:
            connection.close()

    @staticmethod
    def _written_id(result: subprocess.CompletedProcess) -> str:
        for line in reversed(result.stdout.splitlines()):
            if re.match(r"^\[(write|upsert|reinforce)\]", line):
                return line.rsplit(maxsplit=1)[-1]
        raise AssertionError(f"write output contained no record id: {result.stdout!r}")

    def _digests(self, name: str) -> tuple[str, str]:
        connection = sqlite3.connect(self.stores[name] / "memory.db")
        try:
            return connection.execute(
                "SELECT object_set_digest,materialized_digest "
                "FROM sync_peer_state WHERE peer_id='origin'"
            ).fetchone()
        finally:
            connection.close()

    def test_offline_writes_converge_without_project_tree_mutation(self):
        project_before = {
            path.relative_to(self.project): path.read_bytes()
            for path in self.project.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }

        add_a = self._mem(
            "a", "add", "durable", "lesson",
            "server A durable memory survives an offline interval",
            "--entity", '"quoted-entity"', "--entity", "alpha-entity",
        )
        self.assertEqual(add_a.returncode, 0, add_a.stderr)
        first = self._sync("a")
        self.assertEqual(first["status"], "remote-confirmed")
        passive = self._sync("b")
        self.assertEqual(passive["status"], "remote-confirmed")
        self.assertEqual(passive["phases"]["remote-render"], "not-applicable")
        self.assertEqual(passive["phases"]["remote-commit"], "not-applicable")
        self.assertEqual(passive["phases"]["remote-push"], "not-applicable")
        self.assertEqual(passive["phases"]["remote-confirm"], "ok")
        self.assertEqual(self._rows("a"), self._rows("b"))

        # Both replicas author while disconnected. Sequential reconnects must
        # preserve the immutable union, then a final fetch must converge bytes.
        add_a2 = self._mem(
            "a", "add", "durable", "decision",
            "server A authors a second disconnected causal operation",
        )
        add_b2 = self._mem(
            "b", "add", "durable", "decision",
            "server B authors its own disconnected causal operation",
        )
        self.assertEqual(add_a2.returncode, 0, add_a2.stderr)
        self.assertEqual(add_b2.returncode, 0, add_b2.stderr)
        self._sync("a")
        self._sync("b")
        self._sync("a")

        self.assertEqual(self._rows("a"), self._rows("b"))
        self.assertEqual(len(self._rows("a")), 3)
        self.assertEqual(self._digests("a"), self._digests("b"))
        self.assertEqual(
            (self.stores["a"] / "dump.jsonl").read_bytes(),
            (self.stores["b"] / "dump.jsonl").read_bytes(),
        )
        tree_paths = git(self.remote, "ls-tree", "-r", "--name-only", REF).splitlines()
        self.assertTrue(tree_paths)
        self.assertTrue(all(path.startswith("protocol/v2/ops/") for path in tree_paths))

        project_after = {
            path.relative_to(self.project): path.read_bytes()
            for path in self.project.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }
        self.assertEqual(project_after, project_before)

    def test_cli_renders_outbox_before_an_offline_fetch(self):
        added = self._mem(
            "a", "add", "durable", "lesson", "render before offline transport"
        )
        self.assertEqual(added.returncode, 0, added.stderr + added.stdout)
        original_remote = self.remote
        self.remote = self.root / "missing-offline-remote.git"
        try:
            result = self._mem("a", "sync", "--json", remote=True)
        finally:
            self.remote = original_remote
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["reason"], "remote-unavailable")
        self.assertEqual(payload["phases"]["remote-render"], "ok")
        self.assertEqual(payload["phases"]["remote-fetch-validate"], "failed")
        connection = sqlite3.connect(self.stores["a"] / "memory.db")
        try:
            states = connection.execute(
                "SELECT state,rendered_path FROM sync_outbox"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0][0], "rendered")
        self.assertRegex(states[0][1], r"^protocol/v2/ops/[0-9a-f]{2}/[0-9a-f]{64}\.json$")

    def test_sync_json_redacts_host_paths_from_exchange_errors(self):
        env = self._environment("a", remote=True)
        unsafe = self.project / "unsafe-exchange"
        env["MEM_SYNC_DIR"] = str(unsafe)
        result = subprocess.run(
            [sys.executable, str(MEM), "sync", "--json"],
            cwd=self.project,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["reason"], "exchange-validation-failed")
        self.assertNotIn(str(self.project), result.stdout)
        self.assertFalse(unsafe.exists())

    def test_exchange_is_rejected_inside_another_registered_non_git_project(self):
        other_project = self.root / "plain-project-b"
        other_project.mkdir()
        encoded = re.sub(r"[/._]", "-", str(other_project))
        (self.projects_source / encoded).mkdir()
        unsafe = other_project / "exchange.git"
        env = self._environment("a", remote=True)
        env["MEM_SYNC_DIR"] = str(unsafe)
        result = subprocess.run(
            [sys.executable, str(MEM), "sync", "--json"],
            cwd=self.project,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertEqual(
            json.loads(result.stdout)["reason"], "exchange-validation-failed"
        )
        self.assertFalse(unsafe.exists())

    def test_explicit_descendant_decision_clears_blocked_status(self):
        added = self._mem(
            "a", "add", "durable", "lesson", "base for blocked resolution"
        )
        self.assertEqual(added.returncode, 0, added.stderr + added.stdout)
        record_id = self._written_id(added)
        self._sync("a")
        self._sync("b")

        reinforced = self._mem("a", "reinforce", record_id)
        deleted = self._mem("b", "delete", record_id)
        self.assertEqual(reinforced.returncode, 0, reinforced.stderr + reinforced.stdout)
        self.assertEqual(deleted.returncode, 0, deleted.stderr + deleted.stdout)
        self._sync("a")
        blocked_sync = self._mem("b", "sync", "--json", remote=True)
        self.assertEqual(blocked_sync.returncode, 1, blocked_sync.stderr + blocked_sync.stdout)
        self.assertEqual(json.loads(blocked_sync.stdout)["reason"], "blocked-operations")

        decision = self._mem("b", "reinforce", record_id)
        self.assertEqual(decision.returncode, 0, decision.stderr + decision.stdout)
        self._sync("b")
        self._sync("a")
        connection = sqlite3.connect(self.stores["b"] / "memory.db")
        try:
            resolved = connection.execute(
                "SELECT COUNT(*) FROM sync_applied "
                "WHERE result LIKE 'blocked-resolved:%'"
            ).fetchone()[0]
            unresolved = connection.execute(
                "SELECT COUNT(*) FROM sync_applied WHERE result LIKE 'blocked:%'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertGreaterEqual(resolved, 1)
        self.assertEqual(unresolved, 0)

    def test_blocked_resolution_requires_every_final_maximal_head(self):
        mem = load_module("mem")

        def operation(parents):
            return SimpleNamespace(
                parents=tuple(parents),
                payload={"mutations": [{"record_id": "record-r"}]},
            )

        operations = {
            "base": operation(()),
            "update": operation(("base",)),
            "delete": operation(("base",)),
            "partial": operation(("delete",)),
            "decision": operation(("update", "delete")),
        }
        classification = SimpleNamespace(operations=operations)
        unsafe = SimpleNamespace(
            classification=classification,
            accepted=tuple(operations),
            blocked={"delete": object()},
            frontiers={"record-r": ("update", "partial")},
        )
        self.assertEqual(mem._resolved_blocked_map(unsafe), {})
        safe = SimpleNamespace(
            classification=classification,
            accepted=tuple(operations),
            blocked={"delete": object()},
            frontiers={"record-r": ("decision",)},
        )
        self.assertEqual(mem._resolved_blocked_map(safe), {"delete": "decision"})

    def test_local_integration_fold_does_not_overstate_remote_peer_tip(self):
        mem = load_module("mem")
        protocol = load_module("protocol_v2")
        connection = sqlite3.connect(self.stores["a"] / "memory.db")
        try:
            fetched_tip = "a" * 40
            local_integration_tip = "b" * 40
            folded = protocol.fold_operations([])
            connection.execute("BEGIN IMMEDIATE")
            mem._apply_fold(connection, folded, fetched_tip, REF)
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            mem._apply_fold(
                connection,
                folded,
                local_integration_tip,
                REF,
                record_peer=False,
            )
            connection.commit()
            peer = connection.execute(
                "SELECT fetched_tip,folded_tip FROM sync_peer_state "
                "WHERE peer_id='origin'"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(peer, (fetched_tip, fetched_tip))

    def test_copied_database_blocks_writes_until_explicit_replica_rotation(self):
        added = self._mem(
            "a", "add", "durable", "lesson",
            "the original server authors state before a database copy",
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        clone_store = self.root / "store-clone"
        clone_store.mkdir()
        source = sqlite3.connect(self.stores["a"] / "memory.db")
        target = sqlite3.connect(clone_store / "memory.db")
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        self.stores["clone"] = clone_store

        blocked = self._mem(
            "clone", "add", "durable", "lesson",
            "a copied server must not reuse the predecessor replica dot",
        )
        self.assertEqual(blocked.returncode, 2, blocked.stderr + blocked.stdout)
        self.assertIn("copied replica state detected", blocked.stderr)
        status = self._mem("clone", "replica", "status", "--json")
        self.assertEqual(status.returncode, 2, status.stderr + status.stdout)
        self.assertTrue(json.loads(status.stdout)["rotation_required"])

        rotated = self._mem(
            "clone", "replica", "rotate", "--reason",
            "database copied to a distinct test server",
        )
        self.assertEqual(rotated.returncode, 0, rotated.stderr + rotated.stdout)
        accepted = self._mem(
            "clone", "add", "durable", "lesson",
            "the rotated server now owns a distinct replica identity",
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr + accepted.stdout)
        connection = sqlite3.connect(clone_store / "memory.db")
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sync_replica WHERE active=1"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sync_replica WHERE active=0"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_copied_database_cannot_bypass_rotation_through_import(self):
        status = self._mem("a", "replica", "status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
        clone_store = self.root / "store-import-clone"
        clone_store.mkdir()
        source = sqlite3.connect(self.stores["a"] / "memory.db")
        target = sqlite3.connect(clone_store / "memory.db")
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        self.stores["import-clone"] = clone_store
        donor = self.root / "donor.jsonl"
        donor.write_text(
            json.dumps({"id": "donor-record", "body": "must not import"}) + "\n",
            encoding="utf-8",
        )
        imported = self._mem("import-clone", "import", str(donor))
        self.assertEqual(imported.returncode, 2, imported.stderr + imported.stdout)
        self.assertIn("copied replica state detected", imported.stderr)
        self.assertEqual(self._rows("import-clone"), [])

    def test_local_phase_failure_is_typed_not_exit_zero(self):
        (self.stores["a"] / "dump.jsonl").mkdir()
        result = self._mem("a", "sync", "--json")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        status = json.loads(result.stdout)
        self.assertEqual(status["status"], "hard-failure")
        self.assertEqual(status["reason"], "local-phase-failed")
        self.assertEqual(status["phases"]["compatibility-export"], "failed")
        self.assertTrue(all(
            outcome == "not-reached"
            for phase, outcome in status["phases"].items()
            if phase.startswith("remote-")
        ))

    def test_bulk_lifecycle_chunks_protocol_operations_atomically(self):
        added = self._mem(
            "a", "add", "working", "lesson",
            "expired bulk lifecycle operation chunking fixture",
        )
        self.assertEqual(added.returncode, 0, added.stderr + added.stdout)
        source_id = self._written_id(added)
        connection = sqlite3.connect(self.stores["a"] / "memory.db")
        try:
            columns = [
                row[1] for row in connection.execute("PRAGMA table_info(records)")
            ]
            template = list(connection.execute(
                f"SELECT {','.join(columns)} FROM records WHERE id=?", (source_id,)
            ).fetchone())
            index = {name: position for position, name in enumerate(columns)}
            rows = []
            for number in range(129):
                row = list(template)
                record_id = f"bulk-expired-{number:03d}"
                row[index["id"]] = record_id
                row[index["canonical_id"]] = record_id
                row[index["body"]] = f"expired bulk fixture {number:03d}"
                row[index["source"]] = f"bulk-lifecycle:{number:03d}"
                row[index["expires"]] = "2000-01-01"
                rows.append(row)
            connection.execute("DELETE FROM records")
            connection.executemany(
                f"INSERT INTO records VALUES({','.join('?' for _ in columns)})", rows
            )
            connection.commit()
        finally:
            connection.close()

        result = self._mem("a", "lifecycle", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        connection = sqlite3.connect(self.stores["a"] / "memory.db")
        try:
            remaining = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            payloads = [
                json.loads(bytes(row[0]))
                for row in connection.execute(
                    "SELECT payload_bytes FROM sync_objects ORDER BY op_id"
                )
            ]
        finally:
            connection.close()
        tombstone_sizes = sorted(
            len(payload["mutations"])
            for payload in payloads if payload["kind"] == "tombstone"
        )
        self.assertEqual(remaining, 0)
        self.assertEqual(tombstone_sizes, [1, 128])

    def test_lifecycle_rollback_leaves_no_phantom_compat_graveyard(self):
        added = self._mem(
            "a", "add", "working", "lesson",
            "rollback must not publish phantom graveyard evidence",
        )
        self.assertEqual(added.returncode, 0, added.stderr + added.stdout)
        connection = sqlite3.connect(self.stores["a"] / "memory.db")
        try:
            connection.execute("UPDATE records SET expires='2000-01-01'")
            connection.commit()
        finally:
            connection.close()
        script = f"""
import sys
sys.path.insert(0, {str(MEMORY_DIR)!r})
import mem
def fail_capture(*args, **kwargs):
    raise RuntimeError('injected capture rollback')
mem._capture_tombstone_groups = fail_capture
try:
    mem.lifecycle(apply=True)
except RuntimeError:
    pass
else:
    raise SystemExit('expected injected rollback')
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.project,
            env=self._environment("a"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        connection = sqlite3.connect(self.stores["a"] / "memory.db")
        try:
            remaining = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            tombstones = connection.execute(
                "SELECT COUNT(*) FROM sync_transactional_graveyard"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual((remaining, tombstones), (1, 0))
        self.assertFalse((self.stores["a"] / "deleted-records.jsonl").exists())

    def test_fold_computation_does_not_hold_the_sqlite_writer_lock(self):
        script = f"""
import sys, threading, time
from types import SimpleNamespace
sys.path.insert(0, {str(MEMORY_DIR)!r})
import mem
original = mem.protocol_v2.fold_operations
started = threading.Event()
calls = [0]
def slow_fold(operations):
    calls[0] += 1
    started.set()
    time.sleep(1.25)
    return original(operations)
mem.protocol_v2.fold_operations = slow_fold
failure = []
def run_fold():
    try:
        mem._ingest_and_fold_snapshot(
            SimpleNamespace(operations={{}}, tip=None),
            'refs/heads/memory-v2',
        )
    except BaseException as exc:
        failure.append(repr(exc))
thread = threading.Thread(target=run_fold)
thread.start()
assert started.wait(5)
begin = time.monotonic()
record_id = mem.write_record(
    'durable', 'project', 'lesson',
    'concurrent writer remains available during remote full fold',
    quiet=True,
)
elapsed = time.monotonic() - begin
thread.join(10)
assert not thread.is_alive(), 'fold thread did not finish'
assert not failure, failure
assert record_id, 'writer failed'
assert elapsed < 1.0, elapsed
assert calls[0] >= 2, calls
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.project,
            env=self._environment("a"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_log_json_is_versioned_bounded_and_body_path_free(self):
        added = self._mem(
            "a", "add", "durable", "lesson",
            "secret-shaped body must not enter status telemetry",
        )
        self.assertEqual(added.returncode, 0, added.stderr + added.stdout)
        result = self._mem("a", "log", "--json", "--limit", "500")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status_schema"], 1)
        self.assertEqual(payload["status"], "local-only")
        self.assertEqual(payload["exit_code"], 0)
        self.assertLess(len(result.stdout.encode("utf-8")), 64 * 1024)
        self.assertTrue(payload["events"])
        for event in payload["events"]:
            self.assertNotIn("snippet", event)
            self.assertNotIn("cwd", event)
        self.assertNotIn("secret-shaped body", result.stdout)
        self.assertNotIn(str(self.project), result.stdout)

    def test_disabled_remote_does_not_hide_quarantine_in_sync_or_log(self):
        connection = sqlite3.connect(self.stores["a"] / "memory.db")
        try:
            connection.execute(
                "INSERT INTO sync_quarantine(op_id,classification,diagnostic_id,"
                "detail_code,payload_bytes) VALUES(?,?,?,?,?)",
                ("f" * 64, "quarantined-unsupported", "diag-future",
                 "unknown-minor", b"{}"),
            )
            connection.commit()
        finally:
            connection.close()
        synced = self._mem("a", "sync", "--json")
        self.assertEqual(synced.returncode, 1, synced.stderr + synced.stdout)
        sync_payload = json.loads(synced.stdout)
        self.assertEqual(sync_payload["status"], "quarantined")
        self.assertEqual(sync_payload["exit_code"], 1)

        logged = self._mem("a", "log", "--json")
        self.assertEqual(logged.returncode, 1, logged.stderr + logged.stdout)
        log_payload = json.loads(logged.stdout)
        self.assertEqual(log_payload["status"], "quarantined")
        self.assertEqual(log_payload["exit_code"], 1)
        self.assertEqual(log_payload["sync"]["status"], "quarantined")

    def test_remote_fold_preserves_server_local_last_accessed(self):
        added = self._mem(
            "a", "add", "durable", "lesson", "local access timestamp fixture"
        )
        self.assertEqual(added.returncode, 0, added.stderr + added.stdout)
        record_id = self._written_id(added)
        self._sync("a")
        self._sync("b")
        connection = sqlite3.connect(self.stores["b"] / "memory.db")
        try:
            connection.execute(
                "UPDATE records SET last_accessed='2099-01-01' WHERE id=?",
                (record_id,),
            )
            connection.commit()
        finally:
            connection.close()
        self._sync("b")
        connection = sqlite3.connect(self.stores["b"] / "memory.db")
        try:
            last_accessed = connection.execute(
                "SELECT last_accessed FROM records WHERE id=?", (record_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(last_accessed, "2099-01-01")

    def test_doctor_json_is_read_only_and_includes_local_plus_sync_checks(self):
        legacy_store = self.root / "store-legacy-doctor"
        legacy_store.mkdir()
        source = sqlite3.connect(self.stores["a"] / "memory.db")
        target = sqlite3.connect(legacy_store / "memory.db")
        try:
            source.backup(target)
            target.execute("PRAGMA user_version=7")
            target.commit()
        finally:
            target.close()
            source.close()
        self.stores["legacy-doctor"] = legacy_store
        state_root = self.root / "state-legacy-doctor"
        self.assertFalse(state_root.exists())

        result = self._mem("legacy-doctor", "doctor", "--json")
        self.assertIn(result.returncode, {0, 1}, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status_schema"], 1)
        self.assertIn("sync", payload)
        self.assertIn("migration", payload["sync"])
        self.assertIn("last_failure", payload["sync"]["migration"])
        self.assertIn("sync-v2", {item["name"] for item in payload["diagnostics"]})
        connection = sqlite3.connect(legacy_store / "memory.db")
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 7
            )
        finally:
            connection.close()
        self.assertFalse(state_root.exists())

    def test_fresh_epoch_refuses_compatibility_import_without_operation_capture(self):
        dump = self.root / "legacy-import.jsonl"
        dump.write_text(
            json.dumps({"id": "uncaptured-import", "body": "must not enter v2"})
            + "\n",
            encoding="utf-8",
        )
        result = self._mem("a", "import", str(dump))
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertIn("import refused", result.stderr)
        self.assertEqual(self._rows("a"), [])

    def test_remote_gate_detects_partial_uncaptured_semantic_state(self):
        added = self._mem(
            "a", "add", "durable", "lesson", "captured operation state"
        )
        self.assertEqual(added.returncode, 0, added.stderr + added.stdout)
        record_id = self._written_id(added)
        connection = sqlite3.connect(self.stores["a"] / "memory.db")
        try:
            connection.execute(
                "UPDATE records SET body='uncaptured old-writer mutation' WHERE id=?",
                (record_id,),
            )
            connection.commit()
        finally:
            connection.close()

        result = self._mem("a", "sync", "--json", remote=True)
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["reason"], "semantic-state-without-v2-objects")
        with self.assertRaises(subprocess.CalledProcessError):
            git(self.remote, "show-ref", "--verify", REF)

    def test_supersede_merge_delete_and_restore_converge(self):
        source = self._mem(
            "a", "add", "durable", "lesson", "superseded source state",
            "--headline", "source",
        )
        target = self._mem(
            "a", "add", "durable", "lesson", "supersede target state",
            "--headline", "target",
        )
        self.assertEqual(source.returncode, 0, source.stderr)
        self.assertEqual(target.returncode, 0, target.stderr)
        source_id = self._written_id(source)
        target_id = self._written_id(target)
        superseded = self._mem("a", "supersede", source_id, "--by", target_id)
        self.assertEqual(superseded.returncode, 0, superseded.stderr)
        self._sync("a")
        self._sync("b")
        self.assertEqual(self._record_states("a"), self._record_states("b"))
        source_state = next(row for row in self._record_states("b") if row[0] == source_id)
        self.assertEqual(source_state[2:5], ("superseded", target_id, target_id))

        merge_results = [
            self._mem(
                "a", "add", "durable", "lesson",
                f"merge member {number} carries a durable distinct synchronization fixture",
                "--headline", f"merge-{number}",
            )
            for number in range(3)
        ]
        for result in merge_results:
            self.assertEqual(result.returncode, 0, result.stderr)
        merge_ids = [self._written_id(result) for result in merge_results]
        merged = self._mem(
            "a", "merge", "--canonical", merge_ids[0], *merge_ids
        )
        self.assertEqual(merged.returncode, 0, merged.stderr + merged.stdout)
        self._sync("a")
        self._sync("b")
        self.assertEqual(self._record_states("a"), self._record_states("b"))
        self.assertFalse(
            {merge_ids[1], merge_ids[2]} & {row[0] for row in self._record_states("b")}
        )

        deleted = self._mem("a", "delete", merge_ids[0])
        self.assertEqual(deleted.returncode, 0, deleted.stderr + deleted.stdout)
        self._sync("a")
        self._sync("b")
        self.assertNotIn(merge_ids[0], {row[0] for row in self._record_states("b")})
        connection = sqlite3.connect(self.stores["b"] / "memory.db")
        try:
            graveyard = connection.execute(
                "SELECT destructive_op_id,effective,restored_by "
                "FROM sync_graveyard WHERE record_id=? "
                "ORDER BY recorded_at DESC,destructive_op_id DESC LIMIT 1",
                (merge_ids[0],),
            ).fetchone()
            self.assertIsNotNone(graveyard)
            self.assertEqual(graveyard[1:], (1, None))
            destructive_op_id = graveyard[0]
        finally:
            connection.close()

        # The peer that received the tombstone has no compatibility graveyard
        # file. It must reconstruct the exact prior state from immutable causal
        # operations and be able to author the restore itself.
        restored = self._mem("b", "restore", merge_ids[0])
        self.assertEqual(restored.returncode, 0, restored.stderr + restored.stdout)
        self._sync("b")
        self._sync("a")
        self.assertEqual(self._record_states("a"), self._record_states("b"))
        self.assertIn(merge_ids[0], {row[0] for row in self._record_states("b")})
        for name in ("a", "b"):
            connection = sqlite3.connect(self.stores[name] / "memory.db")
            try:
                restored_by = connection.execute(
                    "SELECT restored_by FROM sync_graveyard "
                    "WHERE destructive_op_id=? AND record_id=?",
                    (destructive_op_id, merge_ids[0]),
                ).fetchone()
                self.assertIsNotNone(restored_by)
                self.assertRegex(restored_by[0], r"^[0-9a-f]{64}$")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
