#!/usr/bin/env python3

from __future__ import annotations

import gc
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch_registry_cache as CACHE  # noqa: E402


class RegistryLinesCacheTest(unittest.TestCase):
    def test_utf8_status_accepts_literal_replacement_and_malformed_historical_rows(self):
        self.jobs.write_bytes(
            b"t\topen\t/repo\t/wt\towner\tnote=" + "\ufffd".encode("utf-8")
            + b"\nmalformed\n")

        view = CACHE.registry_view(self.jobs)

        self.assertFalse(view.has_invalid_utf8)
        self.assertEqual(view.malformed_rows, 1)

    def test_split_utf8_append_tail_becomes_valid_without_full_generation_loss(self):
        prefix = b"t\topen\t/repo\t/wt\towner\tnote="
        self.jobs.write_bytes(prefix + b"\xef")
        first = CACHE.registry_view(self.jobs, append_only=True)
        self.assertTrue(first.has_invalid_utf8)

        with self.jobs.open("ab") as stream:
            stream.write(b"\xbf\xbd\n")
        second = CACHE.registry_view(self.jobs, append_only=True)

        self.assertFalse(second.has_invalid_utf8)
        self.assertEqual(second.lines, (prefix.decode("utf-8") + "\ufffd",))
        self.assertEqual(CACHE.metrics_snapshot()["delta_reads"], 1)

    def test_invalid_utf8_replacement_generation_does_not_inherit_old_status(self):
        self.jobs.write_bytes(b"t\topen\t/repo\t/wt\towner\tnote=\xff\n")
        invalid = CACHE.registry_view(self.jobs)
        self.assertTrue(invalid.has_invalid_utf8)

        staged = self.jobs.with_name("replacement.log")
        staged.write_bytes(b"t\topen\t/repo\t/wt\towner\tnote=valid\n")
        os.replace(staged, self.jobs)
        replacement = CACHE.registry_view(self.jobs, append_only=True)

        self.assertFalse(replacement.has_invalid_utf8)
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 2)

    def test_route_and_owner_candidates_keep_conflicts_duplicates_and_append_rows(self):
        def row(aid, extra):
            return ("now\tdone\t/repo\t/wt\tfixture\tattempt_schema_version=2,"
                    f"attempt_id={aid},{extra}\n")
        self.jobs.write_text(
            row("att-owner", "worker_type=owner,launch_started=1,supervisor_lease=flock-v1,route_id=rt-A")
            + row("att-child", "parent_attempt_id=att-owner,route_id=rt-B")
            + row("att-conflict", "worker_type=owner,launch_started=1,supervisor_lease=flock-v1,"
                                 "owner_route_id=rt-B,owner_route_hash=sha256:A"))
        first = CACHE.registry_view(self.jobs, append_only=True)
        self.assertEqual([record.order for record in first.completion_related("att-owner", "rt-A", "sha256:A")],
                         [0, 1, 2])
        self.assertEqual([record.order for record in first.settlement_owner_candidates()], [0, 2])
        with self.jobs.open("a") as out:
            out.write(row("att-child", "parent_attempt_id=att-owner,route_id=rt-A"))
        after = CACHE.registry_view(self.jobs, append_only=True)
        self.assertEqual([record.order for record in after.completion_related("att-owner", "rt-A", "sha256:A")],
                         [0, 1, 2, 3])
        self.assertTrue(after.has_duplicate_attempts)
        self.assertEqual(len(first.records), 3)

    def test_settlement_owner_delta_skips_historical_rows_and_resolves_related_changes(self):
        def owner(attempt):
            return ("now\tdone\t/repo\t/wt\towner\tattempt_schema_version=2,"
                    f"attempt_id={attempt},worker_type=owner,launch_started=1,"
                    "supervisor_lease=flock-v1,route_id=rt-delta,route_hash=sha256:delta\n")

        self.jobs.write_text(owner("att-owner-1") + owner("att-owner-2"), encoding="utf-8")
        first = CACHE.registry_view(self.jobs, append_only=True)
        self.assertEqual([row.order for row in first.settlement_owner_candidates_since(-1)],
                         [0, 1])
        cursor = first.last_complete_order

        with self.jobs.open("a", encoding="utf-8") as stream:
            stream.write("now\tdone\t/repo\t/wt\tchild\tattempt_id=att-child,"
                         "parent_attempt_id=att-owner-1,route_id=rt-delta\n")
        appended = CACHE.registry_view(self.jobs, append_only=True)
        self.assertTrue(appended.append_only_generation)
        self.assertEqual([row.order for row in appended.records_since(cursor)], [2])
        self.assertEqual(appended.settlement_owner_candidates_since(cursor), ())
        self.assertEqual(appended.settlement_owner_candidate("att-owner-1").order, 0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        # A distinct path per test keeps the process-wide cache from leaking
        # a stale entry from an earlier test into this one.
        CACHE.clear_cache()
        CACHE.reset_metrics()

    def test_scope_pins_one_generation_and_next_scope_observes_replacement(self):
        self.jobs.write_text("t\topen\t/repo\t/wt\tone\tattempt_id=one\n", encoding="utf-8")
        CACHE.reset_metrics()
        with CACHE.registry_read_scope():
            first = CACHE.registry_view(self.jobs)
            first_lines = CACHE.registry_lines(self.jobs)
            self.jobs.write_text("t\tdone\t/repo\t/wt\ttwo\tattempt_id=two\n", encoding="utf-8")
            self.assertIs(CACHE.registry_view(self.jobs), first)
            self.assertEqual(CACHE.registry_lines(self.jobs), first_lines)
            self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 1)
        with CACHE.registry_read_scope():
            fresh = CACHE.registry_view(self.jobs)
        self.assertIsNot(first, fresh)
        self.assertEqual(fresh.records[0].slug, "two")

    def test_scope_text_lines_equal_plain_text_decode_for_every_line_separator(self):
        raw = (b"a\tb\r\nc\rd\x0be\x0cf\x1cg\x1dh\x1ei" + "\x85j k l".encode("utf-8")
               + b"\n\nbad=\xff\nlast\rtail\x0cend")
        self.jobs.write_bytes(raw)
        expected = tuple(self.jobs.read_text(encoding="utf-8", errors="replace").splitlines())
        self.assertEqual(expected, tuple(raw.decode("utf-8", errors="replace").splitlines()))
        self.assertIsNone(CACHE.registry_text_lines(self.jobs))
        with CACHE.registry_read_scope():
            self.assertEqual(CACHE.registry_text_lines(self.jobs), expected)

    def test_scope_reuses_an_append_built_view_but_not_its_lf_segmented_lines(self):
        self.jobs.write_bytes(b"t\topen\t/repo\t/wt\tone\tattempt_id=one\n")
        CACHE.registry_view(self.jobs, append_only=True)
        with self.jobs.open("ab") as stream:
            stream.write(b"t\tdone\t/repo\t/wt\ttwo\tattempt_id=two\x0cnote=x\n")
        appended = CACHE.registry_view(self.jobs, append_only=True)
        self.assertEqual(CACHE.metrics_snapshot()["delta_reads"], 1)
        expected = tuple(self.jobs.read_bytes().decode("utf-8", errors="replace").splitlines())
        CACHE.reset_metrics()
        with CACHE.registry_read_scope():
            self.assertIs(CACHE.registry_view(self.jobs), appended)
            self.assertEqual(CACHE.registry_text_lines(self.jobs), expected)
            self.assertNotEqual(appended.lines, expected)
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 0)
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 1)

    def test_scope_pins_view_and_text_lines_from_one_read(self):
        first = "t\topen\t/repo\t/wt\tone\tattempt_id=one"
        self.jobs.write_text(first + "\n", encoding="utf-8")
        CACHE.reset_metrics()
        with CACHE.registry_read_scope():
            lines = CACHE.registry_text_lines(self.jobs)
            with self.jobs.open("a", encoding="utf-8") as stream:
                stream.write("t\tdone\t/repo\t/wt\ttwo\tattempt_id=two\n")
            view = CACHE.registry_view(self.jobs, append_only=True)
            self.assertEqual(lines, (first,))
            self.assertEqual([record.slug for record in view.records], ["one"])
            self.assertEqual(view.signature[2], len(first) + 1)
            self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 1)
        self.assertIsNone(CACHE.registry_text_lines(self.jobs))
        with CACHE.registry_read_scope():
            self.assertEqual(len(CACHE.registry_text_lines(self.jobs)), 2)

    def test_complete_snapshot_keeps_full_coverage_separate_from_selected_rows(self):
        self.jobs.write_text(
            "job\tdone\t/repo\t/wt\towner\tattempt_id=owner,attempt_schema_version=2\n"
            "job\topen\t/repo\t/wt\tprerequisite\tattempt_id=prereq,attempt_schema_version=2\n",
            encoding="utf-8")
        CACHE.reset_metrics()
        with CACHE.registry_read_scope():
            view = CACHE.registry_view(self.jobs)
            selected = view.select_rows(attempt_id="owner")
            snapshot = CACHE.registry_snapshot(self.jobs)
            self.assertTrue(snapshot.complete)
            self.assertEqual(len(selected), 1)
            self.assertEqual(len(snapshot.view.records), 2)
            self.assertEqual(snapshot.lines, view.lines)
            self.assertTrue(snapshot.contains_current_attempt(
                selected, attempt_id="owner", status="done"))
            self.assertFalse(snapshot.contains_current_attempt(
                [], attempt_id="owner", status="done"))
            self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 1)

    def test_snapshot_keeps_malformed_partial_unicode_and_noncanonical_text_unknown(self):
        damaged = (
            (b"job\tdone\t/repo\t/wt\towner\tattempt_id=one\nmalformed\n",
             "registry-malformed"),
            (b"job\tdone\t/repo\t/wt\towner\tattempt_id=one", "registry-partial-tail"),
            (b"job\tdone\t/repo\t/wt\towner\tvalue=\xff\n", "registry-invalid-utf8"),
            (b"job\tdone\t/repo\t/wt\towner\tattempt_id=one\rnext\n",
             "registry-malformed"),
        )
        for raw, reason in damaged:
            with self.subTest(reason=reason):
                self.jobs.write_bytes(raw)
                snapshot = CACHE.registry_snapshot(self.jobs)
                self.assertFalse(snapshot.complete)
                self.assertEqual(snapshot.reason, reason)

    def test_snapshot_missing_read_stays_missing_only_for_its_scope(self):
        missing = self.jobs.with_name("appears-during-pass.log")
        with CACHE.registry_read_scope():
            with self.assertRaises(FileNotFoundError):
                CACHE.registry_snapshot(missing)
            missing.write_text("job\tdone\t/repo\t/wt\tone\tattempt_id=one\n",
                               encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                CACHE.registry_snapshot(missing)
        self.assertTrue(CACHE.registry_snapshot(missing).complete)

    def test_snapshot_generation_isolated_by_jobs_root_and_refreshed_next_pass(self):
        other = self.jobs.with_name("other-jobs.log")
        self.jobs.write_text("job\topen\t/repo\t/wt\tfirst\tattempt_id=one\n",
                             encoding="utf-8")
        other.write_text("job\tdone\t/repo\t/wt\tsecond\tattempt_id=two\n",
                         encoding="utf-8")
        CACHE.reset_metrics()
        with CACHE.registry_read_scope():
            first = CACHE.registry_snapshot(self.jobs)
            separate = CACHE.registry_snapshot(other)
            self.assertNotEqual(first.path_identity, separate.path_identity)
            self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 2)
        with self.jobs.open("a", encoding="utf-8") as stream:
            stream.write("job\topen\t/repo\t/wt\tthird\tattempt_id=three\n")
        with CACHE.registry_read_scope():
            fresh = CACHE.registry_snapshot(self.jobs)
        self.assertNotEqual(first.signature, fresh.signature)
        self.assertEqual(len(first.view.records), 1)
        self.assertEqual(len(fresh.view.records), 2)
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 3)

    def test_scope_keeps_a_failed_read_failed_for_that_pass_only(self):
        missing = self.jobs.with_name("missing.log")
        with CACHE.registry_read_scope():
            with self.assertRaises(FileNotFoundError):
                CACHE.registry_snapshot(missing)
            missing.write_text("t\topen\t/repo\t/wt\tone\tattempt_id=one\n", encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                CACHE.registry_snapshot(missing)
        with CACHE.registry_read_scope():
            self.assertTrue(CACHE.registry_snapshot(missing).complete)

    def test_unchanged_file_parses_only_once(self):
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        first = CACHE.registry_lines(self.jobs)
        second = CACHE.registry_lines(self.jobs)
        third = CACHE.registry_lines(self.jobs)
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 1)
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 1)
        self.assertEqual(CACHE.metrics_snapshot()["bytes_read"], self.jobs.stat().st_size)

    def test_parsed_views_are_shared_but_compatibility_dicts_are_isolated(self):
        self.jobs.write_text(
            "job\topen\t/repo\ttime\towner\tattempt_id=one,attempt_schema_version=2,parent_sid=owner\n",
            encoding="utf-8")
        first = CACHE.registry_view(self.jobs)
        returned = first.handover_latest()
        returned["one"][1]["parent_sid"] = "corrupted"
        second = CACHE.registry_view(self.jobs)
        self.assertIs(first, second)
        self.assertEqual(second.handover_latest()["one"][1]["parent_sid"], "owner")
        row, error = second.completion_attempt("one")
        self.assertIsNone(error)
        self.assertEqual(row.completion_metadata()["parent_sid"], "owner")
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 1)

    def test_completion_and_handover_metadata_keep_distinct_key_rules(self):
        self.jobs.write_text(
            "job\topen\t/repo\ttime\towner\tattempt_id=one,attempt_schema_version=2,parent_sid=exact, parent_sid=trimmed\n",
            encoding="utf-8")
        row = CACHE.registry_view(self.jobs).records[0]
        self.assertEqual(row.completion_metadata()["parent_sid"], "exact")
        self.assertEqual(row.handover_metadata()["parent_sid"], "trimmed")

    def test_records_keep_one_row_backing_and_materialize_metadata_on_demand(self):
        line = "job\topen\t/repo\ttime\towner\tattempt_id=one,attempt_schema_version=2,parent_sid=owner"
        self.jobs.write_text(line + "\n", encoding="utf-8")
        row = CACHE.registry_view(self.jobs).records[0]
        self.assertFalse(hasattr(row, "__dict__"))
        self.assertEqual(row.raw, line)
        self.assertFalse(hasattr(row, "completion_items"))
        self.assertFalse(hasattr(row, "handover_items"))
        self.assertEqual(row.completion_metadata()["parent_sid"], "owner")
        self.assertEqual(row.handover_metadata()["parent_sid"], "owner")

    def test_selected_rows_use_generation_indexes_and_include_partial_tail(self):
        self.jobs.write_text(
            "job\topen\t/repo\ttime\towner\tparent=parent,attempt_id=one\n"
            "job\tdone\t/repo\ttime\towner\tparent=parent,attempt_id=one\n"
            "job\topen\t/repo\ttime\tother\tparent=other,attempt_id=two",
            encoding="utf-8")
        view = CACHE.registry_view(self.jobs)
        selected = view.select_rows(parent="parent", slug="owner", attempt_id="one")
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0][0][1], "done")
        tail = view.select_rows(attempt_id="two")
        self.assertEqual([(fields[4], meta["attempt_id"]) for fields, meta in tail],
                         [("other", "two")])
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 1)

        self.jobs.write_bytes(
            b"job\trunning\t/repo\ttime\tworker\tattempt_id=three\n"
            b"job\topen\t/repo\ttime\tworker\tattempt_id=three\v"
            b"job\tdone\t/repo\ttime\tworker\tattempt_id=three")
        split_tail = CACHE.registry_view(self.jobs).select_rows(attempt_id="three")
        self.assertEqual([(fields[1], metadata["attempt_id"])
                          for fields, metadata in split_tail], [("done", "three")])

    def test_duplicate_attempt_projection_is_generation_cached_across_segments(self):
        self.jobs.write_text(
            "a\topen\t/repo\t/wt\ta\tattempt_id=att-a,parent_attempt_id=\n",
            encoding="utf-8")
        first = CACHE.registry_view(self.jobs, append_only=True)
        self.assertFalse(first.has_duplicate_attempts)
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write("b\tdone\t/repo\t/wt\tb\tattempt_id=att-a,parent_attempt_id=\n")
        second = CACHE.registry_view(self.jobs, append_only=True)
        self.assertTrue(second.has_duplicate_attempts)
        self.assertFalse(first.has_duplicate_attempts)

    def test_same_size_rewrite_and_atomic_replace_rebuild_views(self):
        original = "job\topen\t/repo\ttime\towner\tf=1\n"
        replacement = "job	done\t/repo\ttime\towner\tf=1\n"
        self.assertEqual(len(original), len(replacement))
        self.jobs.write_text(original, encoding="utf-8")
        first = CACHE.registry_view(self.jobs)
        frozen_mtime = self.jobs.stat().st_mtime_ns
        self.jobs.write_text(replacement, encoding="utf-8")
        os.utime(self.jobs, ns=(frozen_mtime, frozen_mtime))
        second = CACHE.registry_view(self.jobs)
        self.assertNotEqual(first, second)
        self.assertEqual(second.lines[0].split("\t")[1], "done")

        staged = self.jobs.with_name("jobs.new")
        staged.write_text(original, encoding="utf-8")
        os.replace(staged, self.jobs)
        third = CACHE.registry_view(self.jobs)
        self.assertEqual(third.lines[0].split("\t")[1], "open")
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 3)

    def test_partial_final_row_is_reobserved_after_append(self):
        self.jobs.write_text("a\tb\tc\td\te\tf=1", encoding="utf-8")
        first = CACHE.registry_view(self.jobs)
        self.assertEqual(first.lines, ("a\tb\tc\td\te\tf=1",))
        with self.jobs.open("a", encoding="utf-8") as stream:
            stream.write("\na\tb\tc\td\te\tf=2\n")
        second = CACHE.registry_view(self.jobs)
        self.assertEqual(len(second.lines), 2)
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 2)

    def test_trusted_append_reads_only_new_bytes_and_does_not_promote_partial_row(self):
        base = "job\topen\t/repo\ttime\towner\tattempt_id=base,attempt_schema_version=2\n"
        self.jobs.write_bytes(base.encode() +
                              b"job\topen\t/repo\ttime\towner\tattempt_id=n\xc3")
        first = CACHE.registry_view(self.jobs, append_only=True)
        self.assertEqual(first.completion_attempt("néxt"), (None, None))
        suffix = b"\xa9xt,attempt_schema_version=2\n"
        with self.jobs.open("ab") as stream:
            stream.write(suffix)
        second = CACHE.registry_view(self.jobs, append_only=True)
        row, error = second.completion_attempt("néxt")
        self.assertIsNone(error)
        self.assertIsNotNone(row)
        metrics = CACHE.metrics_snapshot()
        self.assertEqual(metrics["full_metadata_parses"], 1)
        self.assertEqual(metrics["delta_reads"], 1)
        self.assertEqual(metrics["delta_bytes_read"], len(suffix))
        self.assertEqual(metrics["metadata_rows_parsed"], 2)
        self.assertEqual(second.lines, tuple(base.splitlines() +
                                              ["job\topen\t/repo\ttime\towner\tattempt_id=néxt,attempt_schema_version=2"]))

    def test_atomic_replacement_forces_full_rebuild_even_on_append_path(self):
        self.jobs.write_text("old\topen\t/repo\ttime\towner\tf=1\n", encoding="utf-8")
        first = CACHE.registry_view(self.jobs, append_only=True)
        staged = self.jobs.with_name("jobs.new")
        staged.write_text("old\topen\t/repo\ttime\towner\tf=1\nnew\topen\t/repo\ttime\towner\tf=2\n",
                          encoding="utf-8")
        os.replace(staged, self.jobs)
        second = CACHE.registry_view(self.jobs, append_only=True)
        self.assertIsNot(first, second)
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 2)
        self.assertEqual(CACHE.metrics_snapshot()["delta_reads"], 0)

    def test_handover_candidate_index_drops_attempt_after_parent_binding_changes(self):
        self.jobs.write_text(
            "job\topen\t/repo\ttime\towner\tattempt_id=one,parent_sid=old,"
            "owner_route_id=route,owner_route_hash=hash,route_node=execute\n",
            encoding="utf-8")
        first = CACHE.registry_view(self.jobs, append_only=True)
        self.assertEqual(len(first.handover_candidates("old")), 1)
        with self.jobs.open("a", encoding="utf-8") as stream:
            stream.write(
                "job\topen\t/repo\ttime\towner\tattempt_id=one,parent_sid=new,"
                "owner_route_id=route,owner_route_hash=hash,route_node=execute\n")
        second = CACHE.registry_view(self.jobs, append_only=True)
        self.assertEqual(second.handover_candidates("old"), ())
        self.assertEqual(len(second.handover_candidates("new")), 1)

    def test_handover_index_keeps_node_less_owner_bindings(self):
        self.jobs.write_text(
            "job\tdone\t/repo\ttime\towner\tattempt_id=owner-one,parent_sid=sid-A,worker_type=owner,"
            "owner_route_id=rt-1,owner_route_hash=sha256:aa\n", encoding="utf-8")
        view = CACHE.registry_view(self.jobs, append_only=True)
        found = view.handover_candidates("sid-B", {("rt-1", "sha256:aa", "")})
        self.assertEqual([(status, record.handover_metadata()["parent_sid"]) for status, record in found],
                         [("done", "sid-A")])
        self.assertEqual(view.handover_candidates("sid-B", {("rt-1", "sha256:other", "")}), ())

    def test_append_after_observation_gap_rebuilds_instead_of_using_old_prefix(self):
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        first = CACHE.registry_view(self.jobs, append_only=True)
        first.last_observed_at -= CACHE._APPEND_OBSERVATION_GAP_SECONDS + 1
        with self.jobs.open("a", encoding="utf-8") as stream:
            stream.write("a\tb\tc\td\te\tf=2\n")
        second = CACHE.registry_view(self.jobs, append_only=True)
        self.assertIsNot(first, second)
        self.assertEqual(CACHE.metrics_snapshot()["delta_reads"], 0)
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 2)

    def test_path_cache_retains_a_bounded_lru(self):
        for number in range(CACHE._CACHE_MAX_PATHS + 1):
            path = Path(self.tmp.name) / f"jobs-{number}.log"
            path.write_text(f"a\tb\tc\td\te\tf={number}\n", encoding="utf-8")
            CACHE.registry_view(path)
        metrics = CACHE.metrics_snapshot()
        self.assertEqual(metrics["cache_entries"], CACHE._CACHE_MAX_PATHS)
        self.assertEqual(metrics["cache_evictions"], 1)

    def test_registry_cache_bytes_are_bounded_even_for_one_oversized_path(self):
        with mock.patch.object(CACHE, "_CACHE_MAX_BYTES", 8):
            self.jobs.write_text("large\topen\t/repo\ttime\towner\tf=1\n", encoding="utf-8")
            view = CACHE.registry_view(self.jobs)
            self.assertEqual(view.lines[0].split("\t", 1)[0], "large")
            self.assertEqual(CACHE.metrics_snapshot()["cache_entries"], 0)
            self.assertEqual(CACHE.metrics_snapshot()["cache_bytes"], 0)

    def test_append_segments_compact_indexes_without_reparsing_history(self):
        self.jobs.write_text(
            "job\topen\t/repo\ttime\towner\tattempt_id=att-000,attempt_schema_version=2\n",
            encoding="utf-8")
        view = CACHE.registry_view(self.jobs, append_only=True)
        for number in range(1, 301):
            with self.jobs.open("a", encoding="utf-8") as stream:
                stream.write(
                    f"job\topen\t/repo\ttime\towner\tattempt_id=att-{number:03d},"
                    "attempt_schema_version=2\n")
            view = CACHE.registry_view(self.jobs, append_only=True)
        self.assertLessEqual(len(view._segments), CACHE._MAX_SEGMENTS)
        self.assertEqual(view.record_count, 301)
        self.assertEqual(view.completion_attempt("att-000")[0].order, 0)
        self.assertEqual(view.completion_attempt("att-300")[0].order, 300)
        metrics = CACHE.metrics_snapshot()
        self.assertEqual(metrics["full_metadata_parses"], 1)
        self.assertEqual(metrics["metadata_rows_parsed"], 301)
        self.assertEqual(metrics["delta_reads"], 300)
        self.assertGreater(metrics["segment_compactions"], 0)

    def test_unstable_read_retries_and_returns_only_the_new_generation(self):
        self.jobs.write_text("old\topen\t/repo\ttime\towner\tf=1\n", encoding="utf-8")
        real_read = os.read
        changed = [False]

        def rewrite_after_first_read(fd, size):
            data = real_read(fd, size)
            if not changed[0]:
                changed[0] = True
                self.jobs.write_text("new\tdone\t/repo\ttime\towner\tf=1\n", encoding="utf-8")
            return data

        with mock.patch.object(CACHE.os, "read", side_effect=rewrite_after_first_read):
            view = CACHE.registry_view(self.jobs)
        self.assertEqual(view.lines[0].split("\t", 1)[0], "new")
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 1)

    def test_failed_observation_drops_old_view_and_recovers(self):
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        first = CACHE.registry_view(self.jobs)
        self.jobs.write_text("x\tb\tc\td\te\tf=2\n", encoding="utf-8")
        with mock.patch.object(CACHE.os, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(OSError):
                CACHE.registry_view(self.jobs)
        self.jobs.unlink()
        with self.assertRaises(FileNotFoundError):
            CACHE.registry_view(self.jobs)
        self.jobs.write_text("x\tb\tc\td\te\tf=2\n", encoding="utf-8")
        recovered = CACHE.registry_view(self.jobs)
        self.assertIsNot(first, recovered)
        self.assertEqual(recovered.lines[0].split("\t", 1)[0], "x")

    def test_appended_write_invalidates_the_cache(self):
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        first = CACHE.registry_lines(self.jobs)
        self.assertEqual(len(first), 1)
        self.jobs.write_text(
            "a\tb\tc\td\te\tf=1\na\tb\tc\td\te\tf=2\n", encoding="utf-8"
        )
        second = CACHE.registry_lines(self.jobs)
        self.assertEqual(len(second), 2)
        # This fixture rewrites the file directly; size growth alone is not
        # writer evidence for the append-only fast path.
        self.assertEqual(CACHE.metrics_snapshot()["delta_reads"], 0)
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 2)
        self.jobs.write_text("a\tb\tc\td\te\tf=3\n", encoding="utf-8")
        self.assertEqual(len(CACHE.registry_lines(self.jobs)), 1)

    def test_symlink_aliases_have_separate_slots_and_follow_new_generations(self):
        alias = Path(self.tmp.name) / "jobs-alias.log"
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        alias.symlink_to(self.jobs)
        canonical_view = CACHE.registry_view(self.jobs)
        alias_view = CACHE.registry_view(alias)
        self.assertIsNot(canonical_view, alias_view)
        self.assertEqual(canonical_view.lines, alias_view.lines)
        self.jobs.write_text("x\tb\tc\td\te\tf=2\n", encoding="utf-8")
        self.assertEqual(CACHE.registry_view(alias).lines[0].split("\t", 1)[0], "x")
        self.assertEqual(CACHE.registry_view(self.jobs).lines[0].split("\t", 1)[0], "x")

    def test_replaced_file_same_mtime_is_still_distinguished_by_size(self):
        # A same-second rewrite with a different byte count must not be
        # mistaken for the cached content merely because mtime resolution
        # (whole seconds on some filesystems) did not advance.
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        first = CACHE.registry_lines(self.jobs)
        frozen_ns = self.jobs.stat().st_mtime_ns
        self.jobs.write_text(
            "a\tb\tc\td\te\tf=1\na\tb\tc\td\te\tf=2\n", encoding="utf-8"
        )
        os.utime(self.jobs, ns=(frozen_ns, frozen_ns))
        second = CACHE.registry_lines(self.jobs)
        self.assertNotEqual(first, second)
        self.assertEqual(len(second), 2)

    def test_missing_file_raises_like_a_direct_read(self):
        with self.assertRaises(FileNotFoundError):
            CACHE.registry_lines(self.jobs)

    def test_distinct_paths_cache_independently(self):
        other = Path(self.tmp.name) / "other.log"
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        other.write_text("x\tb\tc\td\te\tf=9\n", encoding="utf-8")
        first = CACHE.registry_lines(self.jobs)
        second = CACHE.registry_lines(other)
        self.assertNotEqual(first, second)

    def test_scoped_full_observation_reuses_verified_prefix_after_append_growth(self):
        original = b"a\topen\t/repo\t/wt\towner\tf=1\n"
        suffix = b"b\topen\t/repo\t/wt\towner\tf=2\n"
        self.jobs.write_bytes(original)
        first = CACHE.registry_view(self.jobs)
        first_record = first.records[0]

        with CACHE.registry_read_scope():
            pinned = CACHE.current_registry_view(self.jobs)
            with self.jobs.open("ab") as stream:
                stream.write(suffix)
            self.assertIs(CACHE.current_registry_view(self.jobs), pinned)

        with CACHE.registry_read_scope():
            second = CACHE.current_registry_view(self.jobs)

        self.assertIsNot(first, second)
        self.assertIs(first._segments[0], second._segments[0])
        self.assertIs(first_record, second.records[0])
        self.assertEqual(second.lines, (original[:-1].decode(), suffix[:-1].decode()))
        self.assertEqual(CACHE.metrics_snapshot()["delta_reads"], 1)
        self.assertEqual(CACHE.metrics_snapshot()["metadata_rows_parsed"], 2)

    def test_scoped_full_observation_rebuilds_on_prefix_rewrite_or_rotation(self):
        self.jobs.write_bytes(b"a\topen\t/repo\t/wt\towner\tf=1\n")
        first = CACHE.registry_view(self.jobs)
        self.jobs.write_bytes(b"x\topen\t/repo\t/wt\towner\tf=1\ny\topen\t/repo\t/wt\towner\tf=2\n")

        with CACHE.registry_read_scope():
            second = CACHE.current_registry_view(self.jobs)

        self.assertIsNot(first, second)
        self.assertIsNot(first._segments[0], second._segments[0])
        self.assertEqual(second.lines[0].split("\t", 1)[0], "x")
        self.assertEqual(CACHE.metrics_snapshot()["delta_reads"], 0)

    def test_fresh_full_rebuild_releases_cache_only_view_before_parsing(self):
        self.jobs.write_bytes(b"a\topen\t/repo\t/wt\towner\tf=1\n")
        CACHE.registry_view(self.jobs)
        identity = str(self.jobs.absolute())
        view_type = CACHE.RegistryView
        self.jobs.write_bytes(b"x\topen\t/repo\t/wt\towner\tf=2\n")
        constructed = []

        def construct(*args, **kwargs):
            retained = [item for item in gc.get_objects()
                        if isinstance(item, view_type) and item.path_identity == identity]
            constructed.append(len(retained))
            return view_type(*args, **kwargs)

        with mock.patch.object(CACHE, "RegistryView", side_effect=construct):
            CACHE.registry_view(self.jobs)
        self.assertEqual(constructed, [0])

    def test_scoped_full_rebuild_releases_cache_only_view_before_parsing(self):
        self.jobs.write_bytes(b"a\topen\t/repo\t/wt\towner\tf=1\n")
        CACHE.registry_view(self.jobs)
        identity = str(self.jobs.absolute())
        view_type = CACHE.RegistryView
        self.jobs.write_bytes(b"x\topen\t/repo\t/wt\towner\tf=2\n")
        constructed = []

        def construct(*args, **kwargs):
            retained = [item for item in gc.get_objects()
                        if isinstance(item, view_type) and item.path_identity == identity]
            constructed.append(len(retained))
            return view_type(*args, **kwargs)

        with mock.patch.object(CACHE, "RegistryView", side_effect=construct):
            with CACHE.registry_read_scope():
                CACHE.current_registry_view(self.jobs)
        self.assertEqual(constructed, [0])


if __name__ == "__main__":
    unittest.main()
