#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

UTILITIES = Path(__file__).resolve().parent
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))

import dispatch_registry_inventory as INV  # noqa: E402


class InventoryHorizonTest(unittest.TestCase):
    def test_horizon_move_without_provenance_is_refused(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            with mock.patch.object(INV, "_append_line", return_value=False):
                error, detail = INV.record_horizon(
                    state_root, root_epoch="2026-08-21T06:09:31Z",
                    first_complete_observation="2026-08-21T06:09:31Z",
                    evidence_digest="sha256:" + "a" * 64,
                    cited_ledger_snapshot_digest="sha256:" + "b" * 64,
                )
            self.assertEqual(error, "horizon-provenance-unrecorded")
            self.assertTrue(detail)
            self.assertIsNone(INV.read_horizon(state_root))

    def test_successful_move_writes_provenance_before_horizon(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            error, _ = INV.record_horizon(
                state_root, root_epoch="2026-08-21T06:09:31Z",
                first_complete_observation="2026-08-21T06:09:31Z",
                evidence_digest="sha256:" + "a" * 64,
                cited_ledger_snapshot_digest="sha256:" + "b" * 64,
            )
            self.assertEqual(error, "")
            horizon = INV.read_horizon(state_root)
            self.assertEqual(horizon["root_epoch"], "2026-08-21T06:09:31Z")
            provenance = (state_root / "inventory" / "horizon-provenance.jsonl").read_text(encoding="utf-8")
            self.assertEqual(len(provenance.strip().splitlines()), 1)


class InventoryCompletenessTest(unittest.TestCase):
    def test_absent_horizon_marks_whole_corpus_incomplete(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            result = INV.inventory_query(state_root, from_ts="2026-01-01T00:00:00Z", to_ts="2026-01-02T00:00:00Z")
            self.assertFalse(result.inventory_complete)
            self.assertEqual(result.reasons, ("horizon-absent",))

    def test_gap_intersecting_query_is_false_and_disjoint_query_is_true(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            INV.record_horizon(
                state_root, root_epoch="2026-08-01T00:00:00Z",
                first_complete_observation="2026-08-01T00:00:00Z",
                evidence_digest="sha256:" + "a" * 64,
                cited_ledger_snapshot_digest="sha256:" + "b" * 64,
            )
            INV.record_gap(
                state_root, from_ts="2026-08-10T10:07:00Z", to_ts="2026-08-21T06:09:31Z",
                evidence_digest="sha256:" + "c" * 64, cited_ledger_snapshot_digest="sha256:" + "d" * 64,
                recoverable=False, discovered_by="gap-census",
            )
            intersecting = INV.inventory_query(state_root, from_ts="2026-08-15T00:00:00Z", to_ts="2026-08-16T00:00:00Z")
            self.assertFalse(intersecting.inventory_complete)
            self.assertTrue(any(r.startswith("gap-intersect:") for r in intersecting.reasons))
            disjoint = INV.inventory_query(state_root, from_ts="2026-08-22T00:00:00Z", to_ts="2026-08-23T00:00:00Z")
            self.assertTrue(disjoint.inventory_complete)
            self.assertEqual(disjoint.reasons, ())


class InventoryGapRecordTest(unittest.TestCase):
    def test_gap_digest_and_interval_match_source_snapshot_and_recoverable_is_false(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            evidence_digest = "sha256:" + "e" * 64
            snapshot_digest = "sha256:" + "f" * 64
            error, _ = INV.record_gap(
                state_root, from_ts="2026-08-10T10:07:00Z", to_ts="2026-08-21T06:09:31Z",
                evidence_digest=evidence_digest, cited_ledger_snapshot_digest=snapshot_digest,
                recoverable=False, discovered_by="gap-census",
            )
            self.assertEqual(error, "")
            gaps = INV.read_gaps(state_root)
            self.assertEqual(len(gaps), 1)
            gap = gaps[0]
            self.assertEqual(gap["evidence_digest"], evidence_digest)
            self.assertEqual((gap["from_ts"], gap["to_ts"]), ("2026-08-10T10:07:00Z", "2026-08-21T06:09:31Z"))
            self.assertFalse(gap["recoverable"])


class ArchiveImportTest(unittest.TestCase):
    def test_second_import_copies_nothing_and_live_jobs_log_row_delta_is_zero(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            source = Path(home) / "jobs.log"
            source.write_text("line1\nline2\nline3\n", encoding="utf-8")
            archive_id_1, error1, _ = INV.import_archive(state_root, source)
            self.assertEqual(error1, "")
            live_rows_before = source.read_text(encoding="utf-8").splitlines()
            archive_id_2, error2, _ = INV.import_archive(state_root, source)
            self.assertEqual(error2, "")
            self.assertEqual(archive_id_1, archive_id_2)
            self.assertEqual(source.read_text(encoding="utf-8").splitlines(), live_rows_before)
            rows = INV.read_archive(state_root, archive_id_1)
            self.assertEqual(len(rows), 3)


class ArchiveIsolationTest(unittest.TestCase):
    def test_archive_rows_never_appear_in_liveness_terminal_or_marker_call_paths(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            source = Path(home) / "jobs.log"
            source.write_text("legacy-row\n", encoding="utf-8")
            INV.import_archive(state_root, source)
            without_archive = INV.inventory_query(state_root)
            self.assertEqual(without_archive.rows, ())
            with_archive = INV.inventory_query(state_root, include_archive=True)
            self.assertEqual(len(with_archive.rows), 1)
            self.assertEqual(with_archive.rows[0]["source"], "archive")
            self.assertIn("liveness/terminal/marker", INV.__doc__)


class ArchiveConsumerTest(unittest.TestCase):
    def test_dispatch_registry_inventory_operation_is_a_live_non_test_consumer(self):
        registry_source = (UTILITIES / "dispatch-registry.py").read_text(encoding="utf-8")
        self.assertIn("archive-import", registry_source)
        self.assertIn("inventory_query", registry_source)
        self.assertIn("from dispatch_registry_inventory import", registry_source)


class InventoryResultTypeTest(unittest.TestCase):
    def test_result_defines_no_int_len_or_index_coercion(self):
        result = INV.InventoryResult(rows=(), inventory_complete=True, reasons=())
        self.assertFalse(hasattr(result, "__int__"))
        self.assertFalse(hasattr(result, "__index__"))
        self.assertFalse(hasattr(result, "__len__"))


class AttemptIdsUnderTest(unittest.TestCase):
    def test_extracts_attempt_ids_from_pipe_field(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            jobs = root / "jobs.log"
            jobs.write_text(
                "2026-08-01T00:00:00Z\topen\tclaude\trt-1\tnode\tattempt_id=att-1,launch_home=/x\n",
                encoding="utf-8",
            )
            self.assertEqual(INV.attempt_ids_under(root), frozenset({"att-1"}))

    def test_absent_registry_is_empty_set(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(INV.attempt_ids_under(Path(home)), frozenset())


if __name__ == "__main__":
    unittest.main()
