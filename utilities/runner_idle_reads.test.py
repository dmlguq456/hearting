#!/usr/bin/env python3
"""The obligation runner does not re-read unchanged inputs on every pass.

Covers the reads that made a quiet runner busy: immutable shared spec revisions
(re-hashed whole for every later publication), seat ledgers behind steward role
resolution, and the active obligation index read by the readiness thread.
Changed inputs are still observed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_producer_spec as spec  # noqa: E402
import artifact_producer_store as producer_store  # noqa: E402
import dispatch_seat_handover as handover  # noqa: E402
import peer_obligations  # noqa: E402

REF = "ref_" + "1" * 32
REV = "rrev_" + "2" * 32


def write_revision(root: Path, files: dict[str, bytes]) -> Path:
    directory = root / "shared/spec" / REF / "revisions" / REV
    for rel, data in files.items():
        (directory / rel).parent.mkdir(parents=True, exist_ok=True)
        (directory / rel).write_bytes(data)
    rows = spec._spec_inventory(files)
    record = {"shared_reference_id": REF, "shared_reference_revision_id": REV,
              "files": rows, "content_digest": spec._spec_inventory_digest(rows)}
    (directory / producer_store.REVISION_RECORD_NAME).write_text(json.dumps(record))
    return directory


class SharedRevisionReads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        spec._VERIFIED_REVISIONS.clear()
        self.files = {"a/prd.md": b"# A\n", "b/prd.md": b"# B\n", "b/notes.md": b"n\n"}
        self.directory = write_revision(self.root, self.files)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(spec._VERIFIED_REVISIONS.clear)

    def count_tree_reads(self):
        reads = []
        original = Path.read_bytes

        def read_bytes(path):
            if path.name != producer_store.REVISION_RECORD_NAME:
                reads.append(str(path))
            return original(path)
        return reads, mock.patch.object(Path, "read_bytes", read_bytes)

    def test_unchanged_revision_is_not_read_again_and_a_rewrite_is_seen(self):
        reads, patch = self.count_tree_reads()
        with patch:
            first, record = spec._verified_shared_spec_record(self.root, REF, REV)
            self.assertEqual(len(reads), len(self.files))
            second, again = spec._verified_shared_spec_record(self.root, REF, REV)
            self.assertEqual(len(reads), len(self.files), "a proven revision is not re-read")
        self.assertEqual(first, second)
        self.assertEqual(first, spec._spec_inventory(self.files))
        self.assertEqual(record, again)

        # Same size, same mtime: the ctime still moves, so the bytes are re-proved.
        path = self.directory / "b/prd.md"
        info = path.stat()
        path.write_bytes(b"# X\n")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        with self.assertRaises(spec.ProducerError) as caught:
            spec._verified_shared_spec_record(self.root, REF, REV)
        self.assertEqual(caught.exception.code, "shared-revision-integrity")

    def test_added_entry_is_seen(self):
        spec._verified_shared_spec_record(self.root, REF, REV)
        (self.directory / "c.md").write_bytes(b"extra\n")
        with self.assertRaises(spec.ProducerError) as caught:
            spec._verified_shared_spec_record(self.root, REF, REV)
        self.assertEqual(caught.exception.code, "shared-revision-integrity")

    def test_one_read_pass_walks_a_revision_once(self):
        walks = []
        original = spec._walk_files

        def walk(top):
            walks.append(str(top))
            return original(top)
        spec._verified_shared_spec_record(self.root, REF, REV)
        with mock.patch.object(spec, "_walk_files", walk):
            with spec.shared_revision_read_scope():
                for _ in range(3):
                    spec._verified_shared_spec_record(self.root, REF, REV)
            self.assertEqual(len(walks), 1)
            spec._verified_shared_spec_record(self.root, REF, REV)
            self.assertEqual(len(walks), 2, "the next pass observes the revision again")

    def test_lazy_tree_reads_only_used_files_and_checks_their_digest(self):
        spec._verified_shared_spec_record(self.root, REF, REV)
        reads, patch = self.count_tree_reads()
        with patch:
            tree, _record = spec._verified_shared_spec_lazy(self.root, REF, REV)
            self.assertEqual(sorted(tree), sorted(self.files))
            self.assertEqual(reads, [])
            self.assertEqual(tree.get("a/prd.md"), self.files["a/prd.md"])
            self.assertIsNone(tree.get("missing.md"))
            self.assertEqual(len(reads), 1)
        tree, _record = spec._verified_shared_spec_lazy(self.root, REF, REV)
        with mock.patch.object(spec, "_digest", return_value="sha256:" + "0" * 64):
            with self.assertRaises(spec.ProducerError) as caught:
                tree["b/prd.md"]
        self.assertEqual(caught.exception.code, "shared-revision-integrity")

    def test_unverified_revision_returns_the_full_tree(self):
        tree, record = spec._verified_shared_spec_lazy(self.root, REF, REV)
        self.assertEqual(dict(tree), self.files)
        self.assertEqual(record["shared_reference_revision_id"], REV)


class SeatLedgerReads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        handover._TRANSITION_ROWS.clear()
        self.addCleanup(handover._TRANSITION_ROWS.clear)
        self.sessions = Path(self.tmp.name) / "hearting" / "session-tidy" / "sessions"
        self.sessions.mkdir(parents=True)
        self.ledger = self.sessions / "seat1.jsonl"
        self.append({"ts": 1, "harness": "claude", "sid": "s1", "event": "start"})
        self.append({"ts": 2, "harness": "claude", "sid": "s1", "event": "steward-release",
                     "seat_key": "seat1", "generation": "g1"})

    def append(self, row):
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def test_unchanged_ledger_is_not_read_again_and_an_append_is_seen(self):
        import session_tidy
        reads = []
        original = session_tidy._read_ledger_lines

        def read(seat, **options):
            reads.append(seat.key)
            return original(seat, **options)
        with mock.patch.object(session_tidy, "_read_ledger_lines", read):
            first = handover._transition_rows()
            self.assertEqual([row["generation"] for _key, row in first], ["g1"])
            self.assertEqual(handover._transition_rows("seat1"), first)
            self.assertEqual(reads, ["seat1"])
            self.append({"ts": 3, "harness": "claude", "sid": "s1", "event": "steward-release",
                         "seat_key": "seat1", "generation": "g2"})
            latest = handover._transition_rows("seat1")
        self.assertEqual([row["generation"] for _key, row in latest], ["g1", "g2"])
        self.assertEqual(reads, ["seat1", "seat1"])

    def test_open_read_scope_keeps_its_own_rows(self):
        import session_tidy
        handover._transition_rows()
        reads = []
        original = session_tidy._read_ledger_lines

        def read(seat, **options):
            reads.append(seat.key)
            return original(seat, **options)
        with mock.patch.object(session_tidy, "_read_ledger_lines", read):
            with session_tidy.seat_ledger_read_scope():
                handover._transition_rows("seat1")
        self.assertEqual(reads, ["seat1"])


class ActiveIndexReads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = peer_obligations.ObligationStore(Path(self.tmp.name) / "state")

    def test_unchanged_active_records_cost_one_stat_each(self):
        created = [self.store.create(f"active-{index}", "readiness-observation", {}, {})
                   for index in range(5)]
        self.store.list(kinds={"readiness-observation"}, fast_active_only=True)
        self.store.reset_discovery_metrics()
        listed = self.store.list(kinds={"readiness-observation"}, fast_active_only=True)
        metrics = self.store.discovery_metrics()
        self.assertEqual({row["id"] for row in listed}, {row["id"] for row in created})
        self.assertEqual(metrics["json_reads"], 0)
        self.assertEqual(metrics["stat_calls"], len(created))

        self.store.update(created[0]["id"], state="complete", result="consumer-exited")
        listed = self.store.list(kinds={"readiness-observation"}, fast_active_only=True)
        self.assertNotIn(created[0]["id"], {row["id"] for row in listed})


class FreedMemoryRelease(unittest.TestCase):
    def test_release_is_rate_limited_and_safe_without_libc(self):
        calls = []
        with mock.patch.object(peer_obligations, "_LIBC_TRIM", [lambda pad: calls.append(pad)]), \
                mock.patch.object(peer_obligations, "_MEMORY_RELEASED_AT", [0.0]):
            peer_obligations.release_freed_memory(min_interval=60.0)
            peer_obligations.release_freed_memory(min_interval=60.0)
            self.assertEqual(calls, [0])
            peer_obligations.release_freed_memory(min_interval=0.0)
            self.assertEqual(calls, [0, 0])
        with mock.patch.object(peer_obligations, "_LIBC_TRIM", [None]), \
                mock.patch.object(peer_obligations, "_MEMORY_RELEASED_AT", [0.0]):
            peer_obligations.release_freed_memory(min_interval=0.0)


class ReadinessProjectionRewrite(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec_ = importlib.util.spec_from_file_location(
            "peer_steward_idle_reads", Path(__file__).with_name("peer-steward.py"))
        self.steward = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(self.steward)

    def projection(self, at, state="pending"):
        return {"schema_version": 1, "identity": {"jobs": "j"}, "producer": {"pid": 1},
                "observed_at": at, "generation": [1], "result": {"state": state, "reason": "",
                                                                 "children": []}}

    def test_unchanged_young_projection_is_not_rewritten(self):
        fresh = self.steward._readiness_projection_still_fresh
        stored = {"shared_readiness": self.projection(100.0), "readiness_state": "pending",
                  "reason": ""}
        result = {"state": "pending", "reason": ""}
        self.assertTrue(fresh(stored, self.projection(101.0), result))
        limit = self.steward.shared_readiness.MAX_AGE_SECONDS / 3
        self.assertFalse(fresh(stored, self.projection(100.0 + limit), result),
                         "an aging projection is refreshed well inside the readers' max age")
        self.assertFalse(fresh(stored, self.projection(101.0, state="ready"),
                               {"state": "ready", "reason": ""}))
        self.assertFalse(fresh({}, self.projection(101.0), result))


if __name__ == "__main__":
    unittest.main()
