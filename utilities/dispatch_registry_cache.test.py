#!/usr/bin/env python3

from __future__ import annotations

import gc
import json
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
    def test_canonical_completion_and_handover_share_one_metadata_parse(self):
        raw = "attempt_id=att-owner,parent=parent,model_role=deep maker"
        self.jobs.write_text("t\topen\t/repo\t/wt\towner\t" + raw + "\n")
        with mock.patch.object(CACHE, "_handover_metadata", side_effect=AssertionError("reparsed")):
            row = CACHE.registry_view(self.jobs).records[0]
            self.assertEqual(row.handover_metadata(), row.completion_metadata())

    def test_owner_rows_carry_the_key_a_pass_keeps_one_row_per_attempt_by(self):
        # the key is computed once per segment: the attempt id, or the row order when it has none;
        # it equals what parsing every row's metadata gives, in registry order, across a merge
        rows = ["t\tdone\t/repo\t/wt\towner\tworker_type=owner,attempt_id=att-a,parent_sid=p",
                "t\tdone\t/repo\t/wt\tchild\tworker_type=child,attempt_id=att-c",
                "t\tdone\t/repo\t/wt\towner\tworker_type=owner,parent_sid=p",
                "t\topen\t/repo\t/wt\towner\tworker_type=owner,attempt_id=att-a,parent_sid=p",
                "t\topen\t/repo\t/wt\towner\tworker_type=owner,attempt_id=,parent_sid=p"]
        self.jobs.write_text("".join(row + "\n" for row in rows))
        with mock.patch.object(CACHE, "_PARSE_CHUNK_LINES", 2):
            view = CACHE.registry_view(self.jobs)
            keyed = view.answer_wait_owner_keyed()
            self.assertGreater(len(view._segments), 1)
        by_parsing = tuple((record.completion_metadata().get("attempt_id") or record.order, record)
                           for record in view.answer_wait_owner_candidates())
        self.assertEqual(keyed, by_parsing)
        self.assertEqual([key for key, _record in keyed], ["att-a", 2, "att-a", 4])

    def test_noncanonical_key_collisions_keep_handover_ordering(self):
        examples = [" a=1,a=2, a=3", "=empty,parent=p", " a=one, a=two,a=three"]
        self.jobs.write_text("".join("t\topen\t/repo\t/wt\towner\t" + raw + "\n"
                                     for raw in examples))
        rows = CACHE.registry_view(self.jobs).records
        self.assertEqual([row.handover_metadata() for row in rows],
                         [dict(CACHE._handover_metadata(raw)) for raw in examples])

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
        self.assertEqual(CACHE.metrics_snapshot()["append_reuses"], 1)

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
        self.assertEqual(appended.prefix_proofs()[:len(first.prefix_proofs())],
                         first.prefix_proofs())
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

    def test_scoped_stat_collision_preserves_one_consistent_generation(self):
        # Include CRLF, a Unicode separator and malformed UTF-8: plain text
        # lines must retain read_text(errors="replace").splitlines semantics.
        old = b"attempt\topen\t/repo\t/wt\tworker\tattempt_id=child\r\nnotes\xe2\x80\xa8\xff\n"
        new = old.replace(b"open", b"done")
        self.jobs.write_bytes(old)
        signature = lambda s: (s.st_dev, 616161, s.st_size, 17, 19)
        with mock.patch.object(CACHE, "_stat_signature", side_effect=signature):
            with CACHE.registry_read_scope():
                first = CACHE.current_registry_view(self.jobs)
                old_lines = CACHE.registry_text_lines(self.jobs)
                self.assertEqual(old_lines, tuple(self.jobs.read_text(errors="replace").splitlines()))
                self.jobs.write_bytes(new)
                with CACHE.registry_read_scope():
                    self.assertIs(CACHE.current_registry_view(self.jobs), first)
                    self.assertEqual(CACHE.registry_text_lines(self.jobs), old_lines)
            with CACHE.registry_read_scope():
                second = CACHE.current_registry_view(self.jobs)
                lines = CACHE.registry_text_lines(self.jobs)
                self.assertEqual(lines, tuple(self.jobs.read_text(errors="replace").splitlines()))
                self.assertEqual(second.lines[0].split("\t")[1], "done")
                self.assertIsNot(first, second)
                self.assertIs(CACHE.registry_view(self.jobs, append_only=True), second)

    def test_scoped_text_lines_keep_splitlines_across_full_and_append_generations(self):
        row = "t\topen\t/repo\t/wt\tslug-{0}\tattempt_id=att-{0},note=한글\n"
        generations = [
            (row.format(0) + "\n" + row.format(1) + "partial-한").encode("utf-8"),
            "글 tail\n".encode("utf-8") + row.format(2).encode("utf-8"),
            b"malformed\n" + row.format(3).encode("utf-8") + "끝".encode("utf-8")[:2],
        ]
        data = b""
        for appended in generations:
            data += appended
            with self.jobs.open("ab") as handle:
                handle.write(appended)
            with CACHE.registry_read_scope():
                self.assertEqual(CACHE.registry_text_lines(self.jobs),
                                 tuple(data.decode("utf-8", errors="replace").splitlines()))
        self.assertEqual(CACHE.metrics_snapshot()["append_reuses"], 2)

    def test_append_observation_transient_stays_near_one_registry_image(self):
        import tracemalloc
        row = "t\tdone\t/repo\t/wt\tslug-{0}\tattempt_id=att-{0},note=" + "한글메모" * 50 + "\n"
        self.jobs.write_text("".join(row.format(index) for index in range(6000)), encoding="utf-8")
        with CACHE.registry_read_scope():
            CACHE.registry_text_lines(self.jobs)
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write(row.format("appended"))
        size = self.jobs.stat().st_size
        gc.collect()
        tracemalloc.start()
        self.addCleanup(tracemalloc.stop)
        base = tracemalloc.get_traced_memory()[0]
        with CACHE.registry_read_scope():
            lines = CACHE.registry_text_lines(self.jobs)
        peak = tracemalloc.get_traced_memory()[1] - base
        self.assertEqual(lines, tuple(self.jobs.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(CACHE.metrics_snapshot()["append_reuses"], 1)
        # A joined copy, whole-file revalidation decode and full text decode
        # used to stack about four registry images on every appended pass.
        self.assertLess(peak, 2 * size)

    def test_scope_pins_alias_across_retarget_and_next_scope_observes_new_target(self):
        other = self.jobs.with_name("other.log")
        alias = self.jobs.with_name("alias.log")
        self.jobs.write_text("old\topen\t/repo\t/wt\tworker\tf=1\n", encoding="utf-8")
        other.write_text("new\tdone\t/repo\t/wt\tworker\tf=2\n", encoding="utf-8")
        alias.symlink_to(self.jobs)
        with CACHE.registry_read_scope():
            first = CACHE.registry_view(alias)
            first_lines = CACHE.registry_text_lines(alias)
            alias.unlink()
            alias.symlink_to(other)
            with CACHE.registry_read_scope():
                self.assertIs(CACHE.current_registry_view(alias), first)
                self.assertEqual(CACHE.registry_text_lines(alias), first_lines)
            self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 1)
        with CACHE.registry_read_scope():
            second = CACHE.current_registry_view(alias)
            self.assertEqual(second.lines[0].split("\t")[0], "new")
            self.assertEqual(CACHE.registry_text_lines(alias),
                             tuple(other.read_text(errors="replace").splitlines()))
            self.assertIsNot(first, second)
        # Unscoped aliases still have independent lexical cache slots.
        self.assertIsNot(CACHE.registry_view(other), second)
        self.assertIs(CACHE.registry_view(alias), second)

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

    def test_pinned_scope_serves_the_given_generation_without_reading(self):
        self.jobs.write_text("t\topen\t/repo\t/wt\tone\tattempt_id=one\n", encoding="utf-8")
        view = CACHE.registry_view(self.jobs, append_only=True)
        CACHE.reset_metrics()
        with CACHE.registry_read_scope(pinned=view):
            self.assertIs(CACHE.registry_view(self.jobs), view)
            snapshot = CACHE.registry_snapshot(self.jobs)
            self.assertIs(snapshot.view, view)
            self.assertTrue(snapshot.matches_jobs(self.jobs))
            self.assertEqual(CACHE.registry_text_lines(self.jobs), tuple(view.lines))
            # The seeded generation stays pinned while the file changes.
            with self.jobs.open("a", encoding="utf-8") as stream:
                stream.write("t\tdone\t/repo\t/wt\ttwo\tattempt_id=two\n")
            self.assertIs(CACHE.registry_view(self.jobs), view)
            self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 0)

        # An outer scope keeps its own generation; ``pinned`` is ignored.
        with CACHE.registry_read_scope():
            outer = CACHE.registry_view(self.jobs)
            self.assertIsNot(outer, view)
            with CACHE.registry_read_scope(pinned=view):
                self.assertIs(CACHE.registry_view(self.jobs), outer)

        # A registry with CR bytes is not seeded: the scope reads it once.
        self.jobs.write_bytes(b"t\topen\t/repo\t/wt\tone\tattempt_id=one\r\n"
                              b"t\tdone\t/repo\t/wt\ttwo\tattempt_id=two\n")
        crlf = CACHE.registry_view(self.jobs)
        CACHE.reset_metrics()
        with CACHE.registry_read_scope(pinned=crlf):
            CACHE.registry_view(self.jobs)
            CACHE.registry_snapshot(self.jobs)
            self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 1)

    def test_pinned_generation_is_never_carried_into_a_later_effect_scope(self):
        """``pinned`` serves one manual observation section and ends with it.

        A claim or an effect group that opens its own scope after a write must read the
        registry fresh; the generation seeded into the observation scope is not reused."""
        self.jobs.write_text("t\topen\t/repo\t/wt\tone\tattempt_id=one\n", encoding="utf-8")
        view = CACHE.registry_view(self.jobs, append_only=True)
        with CACHE.registry_read_scope(pinned=view):
            self.assertTrue(CACHE.registry_scope_active())
            self.assertIs(CACHE.registry_view(self.jobs), view)
        self.assertFalse(CACHE.registry_scope_active(), "the pinned scope must end with its section")
        # The settlement write between the observation section and the effect.
        with self.jobs.open("a", encoding="utf-8") as stream:
            stream.write("t\tdone\t/repo\t/wt\ttwo\tattempt_id=two\n")
        CACHE.reset_metrics()
        with CACHE.registry_read_scope():
            effect = CACHE.registry_view(self.jobs)
            self.assertIsNot(effect, view, "an effect scope must not reuse the observation's generation")
            self.assertEqual(len(effect.lines), 2)
            self.assertEqual(len(view.lines), 1)
        # A scope that is already open (a discovery or effect scope) is never replaced.
        with CACHE.registry_read_scope():
            outer = CACHE.registry_view(self.jobs)
            with CACHE.registry_read_scope(pinned=view):
                self.assertIs(CACHE.registry_view(self.jobs), outer)
            self.assertIs(CACHE.registry_view(self.jobs), outer)

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
        self.assertEqual(CACHE.metrics_snapshot()["append_reuses"], 1)
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

    def test_atomic_replacements_rebuild_views_and_close_old_generations(self):
        original = "job\topen\t/repo\ttime\towner\tf=1\n"
        replacement = "job	done\t/repo\ttime\towner\tf=1\n"
        self.assertEqual(len(original), len(replacement))
        self.jobs.write_text(original, encoding="utf-8")
        first = CACHE.registry_view(self.jobs)
        current = first
        for index in range(12):
            staged = self.jobs.with_name("jobs.new")
            body = replacement if index % 2 == 0 else original
            staged.write_text(body, encoding="utf-8")
            os.replace(staged, self.jobs)
            updated = CACHE.registry_view(self.jobs, append_only=True)
            self.assertIsNot(current, updated)
            self.assertEqual(current._fd, -1)
            self.assertEqual(updated.lines[0].split("\t")[1], "done" if index % 2 == 0 else "open")
            current = updated
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 13)

    def test_generation_descriptors_close_on_clear_and_lru_eviction(self):
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        first = CACHE.registry_view(self.jobs)
        first_fd = first._fd
        CACHE.clear_cache()
        self.assertEqual(first._fd, -1)
        with self.assertRaises(OSError):
            os.fstat(first_fd)

        paths = []
        for number in range(CACHE._CACHE_MAX_PATHS + 1):
            path = Path(self.tmp.name) / f"jobs-{number}.log"
            path.write_text(f"a\tb\tc\td\te\tf={number}\n", encoding="utf-8")
            current = CACHE.registry_view(path)
            if number == 0:
                evicted_fd = current._fd
            paths.append(path)
        self.assertEqual(CACHE.metrics_snapshot()["cache_entries"], CACHE._CACHE_MAX_PATHS)
        with self.assertRaises(OSError):
            os.fstat(evicted_fd)

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
        self.assertEqual(metrics["append_reuses"], 1)
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
        # The rename is not trusted as an append (no delta read, a whole read); the
        # digest of the unchanged leading row is what lets its parse carry over.
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 2)
        self.assertEqual(CACHE.metrics_snapshot()["delta_reads"], 0)
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 1)
        self.assertIs(second._segments[0], first._segments[0])
        self.assertEqual([record.order for record in second.records], [0, 1])

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
        # No delta read: the whole file was read again. Its leading bytes are
        # proven by digest, not by the caller's append-only assertion, so the
        # parse of the unchanged row is kept.
        self.assertEqual(CACHE.metrics_snapshot()["delta_reads"], 0)
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 2)
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 1)
        self.assertEqual(CACHE.metrics_snapshot()["append_reuses"], 1)
        self.assertIs(second._segments[0], first._segments[0])
        self.assertEqual(len(second.records), 2)

    def test_path_cache_retains_a_bounded_lru(self):
        for number in range(CACHE._CACHE_MAX_PATHS + 1):
            path = Path(self.tmp.name) / f"jobs-{number}.log"
            path.write_text(f"a\tb\tc\td\te\tf={number}\n", encoding="utf-8")
            CACHE.registry_view(path)
        metrics = CACHE.metrics_snapshot()
        self.assertEqual(metrics["cache_entries"], CACHE._CACHE_MAX_PATHS)
        self.assertEqual(metrics["cache_evictions"], 1)

    def test_one_oversized_path_stays_cached_and_older_paths_leave_first(self):
        other = Path(self.tmp.name) / "other.log"
        other.write_text("o\topen\t/repo\ttime\towner\tf=1\n", encoding="utf-8")
        with mock.patch.object(CACHE, "_CACHE_MAX_BYTES", 8):
            CACHE.registry_view(other)
            self.jobs.write_text("large\topen\t/repo\ttime\towner\tf=1\n", encoding="utf-8")
            view = CACHE.registry_view(self.jobs)
            self.assertEqual(view.lines[0].split("\t", 1)[0], "large")
            self.assertGreaterEqual(view._fd, 0)
            metrics = CACHE.metrics_snapshot()
            self.assertEqual(metrics["cache_entries"], 1)
            self.assertEqual(metrics["cache_evictions"], 1)
            self.assertIs(CACHE.registry_view(self.jobs), view)
            self.assertEqual(CACHE.metrics_snapshot()["cache_hits"], 1)

    def test_largest_registry_is_not_pushed_out_by_smaller_paths(self):
        self.jobs.write_text("L\topen\t/repo\t/wt\towner\tnote=" + "x" * 60 + "\n",
                             encoding="utf-8")
        with mock.patch.object(CACHE, "_CACHE_MAX_BYTES", 30):  # one 19-byte path beside it
            main = CACHE.registry_view(self.jobs)
            for number in range(3):
                path = Path(self.tmp.name) / f"small-{number}.log"
                path.write_text(f"s\topen\t/r\t/w\to\tf={number}\n", encoding="utf-8")
                CACHE.registry_view(path)  # the large view stays least recently used
            metrics = CACHE.metrics_snapshot()
            self.assertEqual(metrics["cache_entries"], 2)
            self.assertEqual(metrics["cache_evictions"], 2)
            self.assertIs(CACHE.registry_view(self.jobs), main)
            self.assertGreaterEqual(main._fd, 0)
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 4)

    def test_cached_raw_content_is_included_in_budget_and_released_on_clear(self):
        content = b"a\tb\tc\td\te\tf=1\n"
        self.jobs.write_bytes(content)
        CACHE.registry_view(self.jobs)
        self.assertEqual(CACHE.metrics_snapshot()["cache_bytes"], len(content))
        CACHE.clear_cache()
        self.assertEqual(CACHE.metrics_snapshot()["cache_bytes"], 0)
        self.assertEqual(CACHE.metrics_snapshot()["cache_entries"], 0)

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
        self.assertEqual(metrics["append_reuses"], 300)
        self.assertEqual(metrics["delta_reads"], 300)
        self.assertGreater(metrics["segment_compactions"], 0)

    def test_atomic_replacement_during_read_retries_on_new_generation(self):
        self.jobs.write_text("old\topen\t/repo\ttime\towner\tf=1\n", encoding="utf-8")
        real_read = os.read
        changed = [False]

        def replace_after_first_read(fd, size):
            data = real_read(fd, size)
            if not changed[0]:
                changed[0] = True
                staged = self.jobs.with_name("jobs.during-read")
                staged.write_text("new\tdone\t/repo\ttime\towner\tf=1\n", encoding="utf-8")
                os.replace(staged, self.jobs)
            return data

        with mock.patch.object(CACHE.os, "read", side_effect=replace_after_first_read):
            view = CACHE.registry_view(self.jobs)
        self.assertEqual(view.lines[0].split("\t", 1)[0], "new")
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 2)

    def test_failed_observation_drops_old_view_and_recovers(self):
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        first = CACHE.registry_view(self.jobs)
        staged = self.jobs.with_name("jobs.permission-replacement")
        staged.write_text("x\tb\tc\td\te\tf=2\n", encoding="utf-8")
        os.replace(staged, self.jobs)
        with mock.patch.object(CACHE.os, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(OSError):
                CACHE.registry_view(self.jobs)
        self.jobs.unlink()
        with self.assertRaises(FileNotFoundError):
            CACHE.registry_view(self.jobs)
        staged = self.jobs.with_name("jobs.alias-replacement")
        staged.write_text("x\tb\tc\td\te\tf=2\n", encoding="utf-8")
        os.replace(staged, self.jobs)
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
        # writer evidence, so the file is read whole. Only the leading row whose
        # bytes the digest proves unchanged keeps its parse.
        self.assertEqual(CACHE.metrics_snapshot()["delta_reads"], 0)
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 2)
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 1)
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
        staged = self.jobs.with_name("jobs.alias-target-replacement")
        staged.write_text("x\tb\tc\td\te\tf=2\n", encoding="utf-8")
        os.replace(staged, self.jobs)
        self.assertEqual(CACHE.registry_view(alias).lines[0].split("\t", 1)[0], "x")
        self.assertEqual(CACHE.registry_view(self.jobs).lines[0].split("\t", 1)[0], "x")

    def test_same_stat_signature_atomic_replacement_uses_new_generation(self):
        old = b"attempt\topen\t/repo\t/wt\tworker\tattempt_id=child\n"
        new = b"attempt\tdone\t/repo\t/wt\tworker\tattempt_id=child\n"
        self.assertEqual(len(old), len(new))
        self.jobs.write_bytes(old)
        signature = lambda info: (info.st_dev, 424242, info.st_size, 7, 9)
        with mock.patch.object(CACHE, "_stat_signature", side_effect=signature):
            first = CACHE.registry_view(self.jobs)
            staged = self.jobs.with_name("jobs.same-stat-replacement")
            staged.write_bytes(new)
            os.replace(staged, self.jobs)
            second = CACHE.registry_view(self.jobs)
        self.assertIsNot(first, second)
        self.assertEqual(second.lines[0].split("\t")[1], "done")
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 2)

    def test_inode_collision_append_reuse_requires_entire_prefix(self):
        old_row = b"attempt\topen\t/repo\t/wt\tworker\tattempt_id=child\n"
        new_row = old_row.replace(b"open", b"done")
        self.assertEqual(len(old_row), len(new_row))
        tail = b"padding\topen\t/repo\t/wt\tworker\tnote=" + b"x" * 5000 + b"\n"
        self.jobs.write_bytes(old_row + tail)
        signature = lambda info: (info.st_dev, 818181, info.st_size, 11, 13)
        with mock.patch.object(CACHE, "_stat_signature", side_effect=signature):
            first = CACHE.registry_view(self.jobs, append_only=True)
            staged = self.jobs.with_name("jobs.replace")
            staged.write_bytes(new_row + tail + b"extra\topen\t/repo\t/wt\tworker\tf=1\n")
            os.replace(staged, self.jobs)
            second = CACHE.registry_view(self.jobs, append_only=True)
        self.assertEqual(second.lines[0].split("\t")[1], "done")
        self.assertIsNot(first._segments[0], second._segments[0])
        self.assertEqual(CACHE.metrics_snapshot()["append_reuses"], 0)
        self.assertEqual(CACHE.metrics_snapshot()["metadata_rows_parsed"], 5)

    def test_stable_read_retries_after_same_stat_atomic_replacement(self):
        old = b"attempt\topen\t/repo\t/wt\tworker\tf=1\n"
        new = b"attempt\tdone\t/repo\t/wt\tworker\tf=1\n"
        self.assertEqual(len(old), len(new))
        self.jobs.write_bytes(old)
        signature = lambda info: (info.st_dev, 424242, info.st_size, 7, 9)
        real_read = os.read
        calls = [0]

        def replace_during_read(fd, size):
            calls[0] += 1
            data = real_read(fd, size)
            if calls[0] == 1:
                staged = self.jobs.with_name("jobs.concurrent-replacement")
                staged.write_bytes(new)
                os.replace(staged, self.jobs)
            return data

        with mock.patch.object(CACHE, "_stat_signature", side_effect=signature), mock.patch.object(
                CACHE.os, "read", side_effect=replace_during_read):
            view = CACHE.registry_view(self.jobs)
        self.assertEqual(view.lines[0].split("\t")[1], "done")
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 2)
        self.assertEqual(CACHE.metrics_snapshot()["bytes_read"], len(old) + len(new))

    def test_permanently_unstable_observation_invalidates_cached_view(self):
        old = b"attempt\topen\t/repo\t/wt\tworker\tf=1\n"
        new = b"attempt\tdone\t/repo\t/wt\tworker\tf=1\n"
        self.assertEqual(len(old), len(new))
        self.jobs.write_bytes(old)
        cached = CACHE.registry_view(self.jobs)
        staged = self.jobs.with_name("jobs.unstable-start")
        staged.write_bytes(new)
        os.replace(staged, self.jobs)
        real_read = os.read
        flip = [False]

        def replace_after_each_nonempty_read(fd, size):
            data = real_read(fd, size)
            if data:
                flip[0] = not flip[0]
                staged = self.jobs.with_name("jobs.unstable-replacement")
                staged.write_bytes(new if flip[0] else old)
                os.replace(staged, self.jobs)
            return data

        with mock.patch.object(CACHE.os, "read", side_effect=replace_after_each_nonempty_read):
            with self.assertRaises(OSError):
                CACHE.registry_view(self.jobs)
        self.assertEqual(CACHE.metrics_snapshot()["cache_entries"], 0)
        recovered = CACHE.registry_view(self.jobs)
        self.assertIsNot(cached, recovered)
        self.assertEqual(recovered.lines[0].split("\t")[1], "done")

    def test_validation_byte_metrics_match_independent_os_read_spy(self):
        content = b"attempt\topen\t/repo\t/wt\tworker\tf=1\n"
        self.jobs.write_bytes(content)
        real_read = os.read
        total = [0]

        def counted_read(fd, size):
            data = real_read(fd, size)
            total[0] += len(data)
            return data

        with mock.patch.object(CACHE.os, "read", side_effect=counted_read):
            CACHE.registry_view(self.jobs)
        metrics = CACHE.metrics_snapshot()
        self.assertEqual(metrics["content_reads"], 1)
        self.assertEqual(metrics["bytes_read"], total[0])
        self.assertEqual(total[0], len(content))

    def test_symlink_alias_retarget_drops_old_alias_generation(self):
        alias = Path(self.tmp.name) / "jobs-retarget.log"
        other = Path(self.tmp.name) / "other-target.log"
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        other.write_text("x\tb\tc\td\te\tf=2\n", encoding="utf-8")
        alias.symlink_to(self.jobs)
        first = CACHE.registry_view(alias)
        alias.unlink()
        alias.symlink_to(other)
        second = CACHE.registry_view(alias)
        self.assertIsNot(first, second)
        self.assertEqual(second.lines[0].split("\t", 1)[0], "x")
        self.assertEqual(CACHE.metrics_snapshot()["cache_entries"], 1)

    def test_symlink_resolution_failure_invalidates_alias_and_recovers(self):
        alias = Path(self.tmp.name) / "jobs-loop.log"
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        alias.symlink_to(self.jobs)
        first = CACHE.registry_view(alias)
        alias.unlink()
        alias.symlink_to(alias)
        with self.assertRaises(OSError):
            CACHE.registry_view(alias)
        self.assertEqual(CACHE.metrics_snapshot()["cache_entries"], 0)
        alias.unlink()
        alias.symlink_to(self.jobs)
        recovered = CACHE.registry_view(alias)
        self.assertIsNot(first, recovered)
        self.assertEqual(recovered.lines[0].split("\t", 1)[0], "a")

    def test_genuine_append_keeps_prior_segment_identity(self):
        self.jobs.write_text("a\topen\t/repo\t/wt\tworker\tf=1\n", encoding="utf-8")
        first = CACHE.registry_view(self.jobs, append_only=True)
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write("b\topen\t/repo\t/wt\tworker\tf=2\n")
        second = CACHE.registry_view(self.jobs, append_only=True)
        self.assertIs(first._segments[0], second._segments[0])
        self.assertEqual(CACHE.metrics_snapshot()["append_reuses"], 1)
        self.assertEqual(CACHE.metrics_snapshot()["metadata_rows_parsed"], 2)

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
        self.assertEqual(CACHE.metrics_snapshot()["delta_reads"], 0)
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
        staged = self.jobs.with_name("jobs.fresh-replacement")
        staged.write_bytes(b"x\topen\t/repo\t/wt\towner\tf=2\n")
        os.replace(staged, self.jobs)
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
        staged = self.jobs.with_name("jobs.scoped-replacement")
        staged.write_bytes(b"x\topen\t/repo\t/wt\towner\tf=2\n")
        os.replace(staged, self.jobs)
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

    def _scoped(self):
        with CACHE.registry_read_scope():
            view = CACHE.current_registry_view(self.jobs)
            return view, CACHE.registry_text_lines(self.jobs)

    def test_scoped_reader_does_not_reread_a_settled_unchanged_generation(self):
        row = "t\topen\t/repo\t/wt\tone\tattempt_id=one\n"
        self.jobs.write_text(row, encoding="utf-8")
        with mock.patch.object(CACHE, "_SETTLED_NS", 0):
            first, lines = self._scoped()
            for _ in range(3):
                view, again = self._scoped()
                snapshot = CACHE.registry_snapshot(self.jobs)
                self.assertIs(view, first)
                self.assertIs(snapshot.view, first)
                self.assertIsNone(snapshot._pinned[1])  # text lines decode only on use
                self.assertEqual(again, lines)
                self.assertEqual(snapshot.lines, (row[:-1],))
        metrics = CACHE.metrics_snapshot()
        self.assertEqual(metrics["content_reads"], 1)
        self.assertEqual(metrics["bytes_read"], len(row))

    def test_scoped_reader_keeps_verifying_until_the_last_write_is_settled(self):
        self.jobs.write_text("t\topen\t/repo\t/wt\tone\tattempt_id=one\n", encoding="utf-8")
        with mock.patch.object(CACHE, "_SETTLED_NS", 10**18), \
                mock.patch.object(CACHE, "_SETTLED_FINE_NS", 10**18):
            self._scoped()
            self._scoped()
        # Read within the timestamp granularity of the write: a same-tick rewrite
        # could keep the stat, so each scope still compares the bytes.
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 2)
        with mock.patch.object(CACHE, "_SETTLED_NS", 0):
            self._scoped()  # verified once more, now settled
            self._scoped()
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 3)
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 1)

    def test_lf_only_stat_collision_inside_the_racy_window_is_still_read(self):
        old = b"t\topen\t/repo\t/wt\tone\tattempt_id=one\n"
        new = old.replace(b"open", b"done")
        self.jobs.write_bytes(old)
        signature = lambda s: (s.st_dev, 515151, s.st_size, 23, 29)
        # Rewritten in place within the first write's timestamp tick, so even
        # the real stat could stay equal; the generation is not settled yet.
        with mock.patch.object(CACHE, "_stat_signature", side_effect=signature), \
                mock.patch.object(CACHE, "_SETTLED_NS", 10**18), \
                mock.patch.object(CACHE, "_SETTLED_FINE_NS", 10**18):
            first, _lines = self._scoped()
            self.jobs.write_bytes(new)
            second, lines = self._scoped()
        self.assertFalse(first._settled)
        self.assertIsNot(second, first)
        self.assertEqual(second.records[0].status, "done")
        self.assertEqual(lines, (new[:-1].decode(),))
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 2)

    def test_whole_second_timestamps_need_the_coarse_settle_margin(self):
        second = 1_800_000_000 * 10**9
        coarse = mock.Mock(st_mtime_ns=second, st_ctime_ns=second)
        fine = mock.Mock(st_mtime_ns=second + 1, st_ctime_ns=second + 1)
        with mock.patch.object(CACHE.os, "fstat", return_value=coarse):
            self.assertFalse(CACHE._settled_read(0, second + CACHE._SETTLED_FINE_NS))
            self.assertFalse(CACHE._settled_read(0, second + CACHE._SETTLED_NS - 1))
            self.assertTrue(CACHE._settled_read(0, second + CACHE._SETTLED_NS))
        with mock.patch.object(CACHE.os, "fstat", return_value=fine):
            self.assertFalse(CACHE._settled_read(0, second + CACHE._SETTLED_FINE_NS))
            self.assertTrue(CACHE._settled_read(0, second + 1 + CACHE._SETTLED_FINE_NS))
        with mock.patch.object(CACHE.os, "fstat", side_effect=OSError("closed")):
            self.assertFalse(CACHE._settled_read(0, second * 2))

    def test_settled_scoped_reuse_sees_late_appends_and_completed_partial_rows(self):
        first = "t\topen\t/repo\t/wt\tone\tattempt_id=one\n"
        partial = "t\topen\t/repo\t/wt\ttwo\tattempt_id=two"
        with mock.patch.object(CACHE, "_SETTLED_NS", 0):
            self.jobs.write_text(first + partial, encoding="utf-8")
            view, lines = self._scoped()
            self.assertEqual([record.slug for record in view.records], ["one"])
            self.assertTrue(view.has_partial_tail)
            self.assertEqual(lines, (first[:-1], partial))
            self.assertIs(self._scoped()[0], view)
            with self.jobs.open("a", encoding="utf-8") as stream:
                stream.write(",late=1\n")
            completed, lines = self._scoped()
            self.assertIsNot(completed, view)
            self.assertEqual([record.slug for record in completed.records], ["one", "two"])
            self.assertFalse(completed.has_partial_tail)
            self.assertEqual(lines, (first[:-1], partial + ",late=1"))
            self.assertIs(self._scoped()[0], completed)
        metrics = CACHE.metrics_snapshot()
        self.assertEqual(metrics["content_reads"], 2)
        self.assertEqual(metrics["append_reuses"], 1)

    def test_settled_scoped_reuse_follows_rotation_and_atomic_replacement(self):
        row = "t\t{}\t/repo\t/wt\tone\tattempt_id=one\n"
        with mock.patch.object(CACHE, "_SETTLED_NS", 0):
            self.jobs.write_text(row.format("open"), encoding="utf-8")
            view, _lines = self._scoped()
            os.rename(self.jobs, self.jobs.with_name("jobs.log.1"))
            self.jobs.write_text(row.format("done"), encoding="utf-8")  # same size, new file
            rotated, _lines = self._scoped()
            self.assertIsNot(rotated, view)
            self.assertEqual(rotated.records[0].status, "done")
            staged = self.jobs.with_name("jobs.staged")
            staged.write_text(row.format("kill"), encoding="utf-8")
            os.replace(staged, self.jobs)
            replaced, _lines = self._scoped()
            self.assertEqual(replaced.records[0].status, "kill")
            self.assertIs(self._scoped()[0], replaced)
            self.jobs.unlink()
            with self.assertRaises(OSError):
                self._scoped()
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 3)

    def test_parent_session_projection_uses_latest_rows_once_per_generation(self):
        row = "t\t{status}\t/repo\t/wt\ts\tattempt_id={attempt},parent_sid={sid},{extra}\n"
        self.jobs.write_text(
            row.format(status="open", attempt="a", sid="old", extra="parent_harness=codex")
            + row.format(status="done", attempt="a", sid="new",
                         extra="parent_completion_delivery=codex-native-queue")
            + row.format(status="open", attempt="b", sid="new",
                         extra="parent_completion_delivery=codex-native-queue")
            + row.format(status="open", attempt="c", sid="", extra="main_runtime_harness=claude"),
            encoding="utf-8")
        view = CACHE.registry_view(self.jobs, append_only=True)
        expected = {meta and tuple(meta.get(key) for key in view.PARENT_SESSION_KEYS)
                    for _status, meta in view.handover_latest().values()}
        with mock.patch.object(CACHE, "_parse_handover", wraps=CACHE._parse_handover) as parse:
            first = view.handover_parent_sessions()
            parsed = parse.call_count
            self.assertIs(view.handover_parent_sessions(), first)
        self.assertEqual(parsed, 1)  # only the terminal latest row lacks retained pairs
        self.assertEqual(parse.call_count, parsed)
        self.assertEqual(set(first), expected)
        self.assertEqual(set(first), {("new", None, None, "codex-native-queue"),
                                      ("", None, "claude", None)})
        with self.jobs.open("a", encoding="utf-8") as stream:
            stream.write(row.format(status="done", attempt="b", sid="later", extra="parent_harness=x"))
        appended = CACHE.registry_view(self.jobs, append_only=True)
        self.assertIsNot(appended, view)
        self.assertIn(("later", "x", None, None), appended.handover_parent_sessions())

    def test_scoped_reader_verifies_a_generation_extended_by_an_asserted_suffix(self):
        old = "t\topen\t/repo\t/wt\tone\tattempt_id=one\n"
        with mock.patch.object(CACHE, "_SETTLED_NS", 0):
            self.jobs.write_text(old, encoding="utf-8")
            self._scoped()
            # An unsupported in-place rewrite that also grows the file: only the
            # plain append_only reader trusts the old prefix.
            self.jobs.write_text(old.replace("open", "done")
                                 + "t\topen\t/repo\t/wt\ttwo\tattempt_id=two\n", encoding="utf-8")
            asserted = CACHE.registry_view(self.jobs, append_only=True)
            self.assertEqual(asserted.records[0].status, "open")
            verified, lines = self._scoped()
            self.assertIsNot(verified, asserted)
            self.assertEqual([record.status for record in verified.records], ["done", "open"])
            self.assertEqual(lines, tuple(self.jobs.read_text(encoding="utf-8").splitlines()))
            self.assertIs(self._scoped()[0], verified)
        metrics = CACHE.metrics_snapshot()
        self.assertEqual(metrics["content_reads"], 3)
        self.assertEqual(metrics["delta_reads"], 1)


class LightRegistryViewTest(unittest.TestCase):
    OWNER = "attempt_id=att-{n},attempt_schema_version=2,parent_sid=sid,worker_type=owner"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        CACHE.clear_cache()
        CACHE.reset_metrics()

    def _rows(self, statuses):
        return "".join(f"t\t{status}\t/repo\t/wt\towner\t{self.OWNER.format(n=n)}\n"
                       for n, status in enumerate(statuses))

    def test_terminal_rows_parse_lazily_and_open_rows_keep_their_pairs(self):
        self.jobs.write_text(self._rows(["done", "open", "running", "killed"]), encoding="utf-8")
        view = CACHE.registry_view(self.jobs)
        self.assertEqual([bool(row._completion_pairs) for row in view.records],
                         [False, True, True, False])
        self.assertEqual([bool(row._handover_pairs) for row in view.records],
                         [False, True, True, False])
        done = view.records[0]
        self.assertEqual(done.completion_metadata()["attempt_id"], "att-0")
        self.assertEqual(done.handover_metadata()["parent_sid"], "sid")
        self.assertEqual(view.completion_attempt("att-0")[0], done)
        self.assertEqual(view.handover_attempt("att-0")[1]["parent_sid"], "sid")
        self.assertEqual(sorted(view.handover_latest()), ["att-0", "att-1", "att-2", "att-3"])

    def test_lazy_lookup_cache_is_small_and_cleared_with_the_generation(self):
        self.assertEqual(CACHE._completion_metadata.cache_info().maxsize, 256)
        self.assertEqual(CACHE._handover_metadata.cache_info().maxsize, 256)
        self.jobs.write_text(self._rows(["done", "done"]), encoding="utf-8")
        view = CACHE.registry_view(self.jobs)
        view.records[0].completion_metadata()
        view.records[1].handover_metadata()
        self.assertGreater(CACHE._completion_metadata.cache_info().currsize, 0)
        self.assertGreater(CACHE._handover_metadata.cache_info().currsize, 0)
        staged = self.jobs.with_name("jobs.replacement")
        # A replacement that keeps none of the old rows is a new generation.
        staged.write_text(self._rows(["done"]).replace("att-0", "att-other"), encoding="utf-8")
        os.replace(staged, self.jobs)
        CACHE.registry_view(self.jobs)
        self.assertEqual(CACHE._completion_metadata.cache_info().currsize, 0)
        self.assertEqual(CACHE._handover_metadata.cache_info().currsize, 0)

    def test_replacement_that_keeps_leading_rows_keeps_the_lookup_cache(self):
        patcher = mock.patch.object(CACHE, "_PARSE_CHUNK_LINES", 1)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.jobs.write_text(self._rows(["done", "done"]), encoding="utf-8")
        view = CACHE.registry_view(self.jobs)
        view.records[0].completion_metadata()
        staged = self.jobs.with_name("jobs.replacement")
        staged.write_text(self._rows(["done"]), encoding="utf-8")
        os.replace(staged, self.jobs)
        CACHE.registry_view(self.jobs)
        self.assertGreater(CACHE._completion_metadata.cache_info().currsize, 0)

    def test_append_reuse_keeps_the_lookup_cache(self):
        self.jobs.write_text(self._rows(["done"]), encoding="utf-8")
        view = CACHE.registry_view(self.jobs, append_only=True)
        view.records[0].completion_metadata()
        with self.jobs.open("a", encoding="utf-8") as stream:
            stream.write(self._rows(["done"]).replace("att-0", "att-1"))
        CACHE.registry_view(self.jobs, append_only=True)
        self.assertEqual(CACHE.metrics_snapshot()["append_reuses"], 1)
        self.assertGreater(CACHE._completion_metadata.cache_info().currsize, 0)
        CACHE.clear_cache()
        self.assertEqual(CACHE._completion_metadata.cache_info().currsize, 0)

    def test_scope_without_text_request_keeps_no_text_copy_and_lines_match(self):
        raw = ("t\tdone\t/repo\t/wt\towner\ta=1\n\nmalformed\n"
               "t\topen\t/repo\t/wt\towner\ta=2")
        self.jobs.write_bytes(raw.encode())
        with CACHE.registry_read_scope():
            view = CACHE.current_registry_view(self.jobs)
            entry = CACHE._pinned_generation(CACHE._READ_SCOPE.get(), self.jobs)
            self.assertIsNone(entry[1])
            lines = CACHE.registry_text_lines(self.jobs)
            self.assertIs(CACHE.registry_text_lines(self.jobs), lines)
            self.assertIs(entry[0], view)
            snapshot = CACHE.registry_snapshot(self.jobs)
        self.assertEqual(lines, tuple(raw.splitlines()))
        self.assertEqual(snapshot.lines, tuple(raw.splitlines()))

    def test_scope_keeps_plain_text_lines_for_non_lf_separators(self):
        raw = b"a\tb\tc\td\te\tf=1\r\nnotes\xe2\x80\xa8\xff\n"
        self.jobs.write_bytes(raw)
        with CACHE.registry_read_scope():
            CACHE.current_registry_view(self.jobs)
            entry = CACHE._pinned_generation(CACHE._READ_SCOPE.get(), self.jobs)
            self.assertIsNotNone(entry[1])
            self.assertEqual(CACHE.registry_text_lines(self.jobs),
                             tuple(raw.decode("utf-8", errors="replace").splitlines()))

    def test_streamed_lines_equal_splitlines_for_edge_shapes(self):
        shapes = [b"a\tb\tc\td\te\tf=1", b"a\tb\tc\td\te\tf=1\n",
                  b"a\tb\tc\td\te\tf=1\n\n\nz\tb\tc\td\te\tf=2\n",
                  b"a\tb\tc\td\te\tf=1\n\nzz", b"\n", b"", b"x\n\n"]
        for number, raw in enumerate(shapes):
            path = Path(self.tmp.name) / f"shape-{number}.log"
            path.write_bytes(raw)
            view = CACHE.registry_view(path)
            expected = tuple(raw.decode().splitlines())
            self.assertEqual(view.line_count, len(expected), raw)
            self.assertEqual(view.lines, expected, raw)
            self.assertEqual(view.has_partial_tail, bool(raw) and not raw.endswith(b"\n"))


class PrefixReuseAfterReplacementTest(unittest.TestCase):
    """A rewritten registry keeps the parse of the leading rows its bytes prove unchanged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        CACHE.clear_cache()
        CACHE.reset_metrics()
        for name, value in (("_PARSE_CHUNK_LINES", 4),):
            patcher = mock.patch.object(CACHE, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _row(number, status="done", extra=""):
        return (f"t\t{status}\t/repo\t/wt\tslug{number % 3}\t"
                f"attempt_id=att-{number},attempt_schema_version=2,parent=p{number % 3}"
                f",parent_sid=sid{number % 2}{extra}\n")

    def _write(self, rows):
        self.jobs.write_bytes("".join(rows).encode())

    def _replace(self, rows):
        staged = self.jobs.with_name("jobs.staged")
        staged.write_bytes("".join(rows).encode())
        os.replace(staged, self.jobs)

    def _read(self, scoped):
        if not scoped:
            return CACHE.registry_view(self.jobs)
        with CACHE.registry_read_scope():
            return CACHE.current_registry_view(self.jobs)

    @staticmethod
    def _answers(view):
        attempts = sorted({record.completion_metadata().get("attempt_id")
                           for record in view.records} - {None})
        return {
            "lines": view.lines, "line_count": view.line_count,
            "orders": [record.order for record in view.records],
            "fields": [record.fields for record in view.records],
            "select": view.select_rows(), "select_parent": view.select_rows(parent="p1"),
            "latest": {attempt: (view.completion_attempt(attempt)[0].order,
                                 view.completion_attempt(attempt)[1])
                       for attempt in attempts},
            "records": {attempt: [record.order for record in
                                  view.completion_attempt_records(attempt)]
                        for attempt in attempts},
            "handover": view.handover_latest(),
            "candidates": [(status, record.order) for status, record in
                           view.handover_candidates("sid1")],
            "owners": [record.order for record in view.settlement_owner_candidates()],
            "duplicates": view.has_duplicate_attempts,
            "malformed": (view.malformed_rows, tuple(line for segment in view._segments for line in segment.malformed_lines)),
            "last": view.last_complete_order, "reason": view.completeness_reason,
        }

    def _assert_same_as_fresh_read(self, view):
        got = self._answers(view)
        raw = self.jobs.read_bytes()
        CACHE.clear_cache()
        self.assertEqual(got, self._answers(CACHE.registry_view(self.jobs)))
        return raw

    def _assert_digests(self, view):
        raw = self.jobs.read_bytes()
        previous_order = 0
        for segment in view._segments:
            self.assertEqual(segment.prefix_sha256,
                             __import__("hashlib").sha256(raw[:segment.byte_end]).digest())
            self.assertEqual(raw[segment.byte_end - 1:segment.byte_end], b"\n")
            self.assertGreater(segment.order_end, previous_order)
            previous_order = segment.order_end

    def _rows(self, count=14):
        return [self._row(number) for number in range(count)]

    def test_replacement_keeping_leading_segments_reuses_them(self):
        for scoped in (False, True):
            with self.subTest(scoped=scoped):
                CACHE.clear_cache()
                CACHE.reset_metrics()
                rows = self._rows()
                self._write(rows)
                first = self._read(scoped)
                self.assertEqual(len(first._segments), 4)
                self._assert_digests(first)
                changed = list(rows)
                changed[5] = self._row(5, "killed")       # second segment
                changed.append(self._row(14))
                self._replace(changed)
                second = self._read(scoped)
                self.assertIsNot(first, second)
                self.assertIs(second._segments[0], first._segments[0])
                self.assertIsNot(second._segments[1], first._segments[1])
                # The two segments after the changed one hold the same rows at the same order:
                # new objects at new byte positions that share the old rows and indexes.
                for index in (2, 3):
                    self.assertIsNot(second._segments[index], first._segments[index])
                    self.assertIs(second._segments[index].records, first._segments[index].records)
                    self.assertEqual(second._segments[index].content_sha256,
                                     first._segments[index].content_sha256)
                self.assertIsNot(second._segments[1].records, first._segments[1].records)
                metrics = CACHE.metrics_snapshot()
                self.assertEqual(metrics["full_metadata_parses"], 1)
                self.assertEqual(metrics["prefix_reuses"], 1)
                self.assertEqual(metrics["prefix_segments_reused"], 1)
                self.assertGreater(metrics["prefix_bytes_reused"], 0)
                self.assertEqual(metrics["segment_resyncs"], 2)
                self.assertEqual(metrics["segment_rebuilds"], 4 + 2)   # first read's four, the changed one, the new row
                self.assertEqual(metrics["metadata_rows_parsed"], 14 + 15 - 4 - 6)
                self.assertEqual(second.completion_attempt("att-5")[0].status, "killed")
                self._assert_digests(second)
                self._assert_same_as_fresh_read(second)

    def test_changed_first_row_reuses_no_segment(self):
        rows = self._rows()
        self._write(rows)
        self._read(False)
        rows[0] = self._row(0, "killed")
        self._replace(rows)
        view = self._read(False)
        metrics = CACHE.metrics_snapshot()
        self.assertEqual((metrics["full_metadata_parses"], metrics["prefix_reuses"]), (2, 0))
        # Rows with the same text are still not split and parsed twice.
        self.assertGreater(metrics["line_parse_reuses"], 0)
        self._assert_same_as_fresh_read(view)

    def test_same_size_byte_change_reparses_from_that_segment(self):
        rows = self._rows()
        self._write(rows)
        first = self._read(False)
        raw = self.jobs.read_bytes()
        target = raw.index(b"att-6")
        changed = raw[:target] + b"att-X" + raw[target + 5:]
        self.assertEqual(len(changed), len(raw))
        staged = self.jobs.with_name("jobs.staged")
        staged.write_bytes(changed)
        os.replace(staged, self.jobs)
        second = self._read(False)
        self.assertIs(second._segments[0], first._segments[0])
        self.assertIsNot(second._segments[1], first._segments[1])
        self.assertIsNone(second.completion_attempt("att-6")[0])
        self.assertIsNotNone(second.completion_attempt("att-X")[0])
        self._assert_same_as_fresh_read(second)

    def test_byte_change_inside_the_first_segment_reuses_nothing(self):
        self._write(self._rows())
        self._read(False)
        raw = self.jobs.read_bytes()
        target = raw.index(b"att-1,")
        staged = self.jobs.with_name("jobs.staged")
        staged.write_bytes(raw[:target] + b"att-Y" + raw[target + 5:])
        os.replace(staged, self.jobs)
        view = self._read(False)
        self.assertEqual(CACHE.metrics_snapshot()["prefix_reuses"], 0)
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 2)
        self._assert_same_as_fresh_read(view)

    def test_deleted_row_shifts_later_orders_to_the_new_generation(self):
        for scoped in (False, True):
            with self.subTest(scoped=scoped):
                CACHE.clear_cache()
                CACHE.reset_metrics()
                rows = self._rows()
                self._write(rows)
                first = self._read(scoped)
                del rows[5]
                rows.append(self._row(14))
                self._replace(rows)
                second = self._read(scoped)
                self.assertIs(second._segments[0], first._segments[0])
                self.assertEqual([record.order for record in second.records], list(range(14)))
                # The shifted rows' text is unchanged, so their parse carries over.
                self.assertGreater(CACHE.metrics_snapshot()["line_parse_reuses"], 0)
                self.assertEqual(second.completion_attempt("att-6")[0].order, 5)
                self._assert_digests(second)
                self._assert_same_as_fresh_read(second)

    def test_new_separator_or_invalid_utf8_reuses_nothing(self):
        for extra in (b"\r", b"\xe2\x80\xa8", b"\xff"):
            with self.subTest(extra=extra):
                CACHE.clear_cache()
                CACHE.reset_metrics()
                rows = self._rows()
                self._write(rows)
                self._read(False)
                data = "".join(rows).encode() + b"t\topen\t/r\t/w\ts\tattempt_id=att-z" + extra + b"\n"
                staged = self.jobs.with_name("jobs.staged")
                staged.write_bytes(data)
                os.replace(staged, self.jobs)
                view = self._read(False)
                self.assertEqual(CACHE.metrics_snapshot()["prefix_reuses"], 0)
                self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 2)
                self.assertFalse(view._prefix_reuse_eligible)
                self.assertTrue(all(segment.byte_end is None for segment in view._segments))
                self.assertEqual(view.lines, tuple(self.jobs.read_bytes()
                                                   .decode("utf-8", errors="replace").splitlines()))

    def test_replacement_after_compaction_reuses_up_to_the_merged_run(self):
        with mock.patch.object(CACHE, "_MAX_SEGMENTS", 4), \
                mock.patch.object(CACHE, "_COMPACT_SEGMENTS", 2):
            rows = self._rows(8)
            self._write(rows)
            view = self._read(False)
            for number in range(8, 14):
                rows.append(self._row(number))
                with self.jobs.open("ab") as stream:
                    stream.write(rows[-1].encode())
                view = CACHE.registry_view(self.jobs, append_only=True)
            self.assertGreaterEqual(CACHE.metrics_snapshot()["segment_compactions"], 1)
            self._assert_digests(view)
            rows[12] = self._row(12, "killed")
            self._replace(rows)
            second = self._read(False)
            self.assertGreater(CACHE.metrics_snapshot()["prefix_segments_reused"], 0)
            self.assertIs(second._segments[0], view._segments[0])
            self._assert_digests(second)
            self._assert_same_as_fresh_read(second)

    def test_appended_view_keeps_the_digest_chain_for_a_later_replacement(self):
        rows = self._rows(10)
        self._write(rows)
        view = self._read(False)
        with self.jobs.open("ab") as stream:
            stream.write(self._row(10).encode() + b"partial-tail")
        appended = CACHE.registry_view(self.jobs, append_only=True)
        self.assertEqual(CACHE.metrics_snapshot()["append_reuses"], 1)
        self._assert_digests(appended)
        with self.jobs.open("ab") as stream:
            stream.write(b"\n")
        completed = CACHE.registry_view(self.jobs, append_only=True)
        self._assert_digests(completed)
        rows.extend([self._row(10), "partial-tail\n", self._row(11)])
        rows[9] = self._row(9, "killed")
        self._replace(rows)
        second = self._read(False)
        self.assertIs(second._segments[0], view._segments[0])
        self._assert_digests(second)
        self._assert_same_as_fresh_read(second)

    def test_unchanged_prefix_of_a_pure_append_keeps_its_prefix_proofs(self):
        rows = self._rows(8)
        self._write(rows)
        first = self._read(True)
        with self.jobs.open("ab") as stream:
            stream.write(self._row(8).encode())
        second = self._read(True)
        self.assertEqual(second.prefix_proofs()[:len(first.prefix_proofs())],
                         first.prefix_proofs())
        self.assertIs(second._segments[0], first._segments[0])
        self.assertEqual(CACHE.metrics_snapshot()["append_reuses"], 1)
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 1)
        self._assert_same_as_fresh_read(second)

    def test_prefix_proofs_follow_the_bytes_not_the_file(self):
        # owner_candidate_floor keeps a cursor's floor only where these digests
        # still match: whether the file was renamed, kept, or got an old inode
        # number back is not asked. The parse is reused in every case.
        def appended_in_place(rows):
            with self.jobs.open("ab") as stream:
                stream.write(self._row(len(rows)).encode())
            return rows + [self._row(len(rows))]

        def changed_row(rows):
            changed = list(rows)
            changed[6] = self._row(6, "killed")
            self._replace(changed)

        cases = {
            "append by rename": (self._rows(8), lambda rows: self._replace(rows + [self._row(8)]),
                                 False, True),
            "identical rewrite": (self._rows(8), lambda rows: self._replace(rows), False, True),
            "changed row": (self._rows(8), changed_row, False, False),
            "replacement of an empty generation": ([], lambda rows: self._replace(self._rows(3)),
                                                   False, True),
            "replacement of a tail-only generation": (
                ["t\topen\t/repo\t/wt\ts\tattempt_id=att-tail"],
                lambda rows: self._replace(self._rows(3)), False, True),
            "append in place": (self._rows(8), appended_in_place, True, True),
        }
        for scoped in (False, True):
            for name, (rows, change, same_file, kept) in cases.items():
                with self.subTest(case=name, scoped=scoped):
                    CACHE.clear_cache()
                    CACHE.reset_metrics()
                    self._replace(rows)
                    first = self._read(scoped)
                    change(rows)
                    second = self._read(scoped)
                    self.assertIsNot(second, first)
                    self.assertEqual(second.signature[:2] == first.signature[:2], same_file)
                    # A generation without a complete row proves nothing to keep.
                    self.assertEqual(set(first.prefix_proofs()) <= set(second.prefix_proofs()),
                                     kept)
                    if first._segments and kept:
                        self.assertIs(second._segments[0], first._segments[0])
                        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 1)
                    self._assert_same_as_fresh_read(second)

    def test_compaction_keeps_the_proofs_it_merges_within_a_bound(self):
        # A cursor taken a few appends ago keeps its proof after compaction
        # merges its boundary into one segment; the list itself stays bounded.
        for name, value in (("_MAX_SEGMENTS", 8), ("_COMPACT_SEGMENTS", 4)):
            patcher = mock.patch.object(CACHE, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        rows = self._rows(4)
        self._write(rows)
        previous = [CACHE.registry_view(self.jobs, append_only=True)]
        for number in range(4, 40):
            with self.jobs.open("ab") as stream:
                stream.write(self._row(number).encode())
            view = CACHE.registry_view(self.jobs, append_only=True)
            raw = self.jobs.read_bytes()
            proofs = view.prefix_proofs()
            self.assertEqual([proof[0] for proof in proofs], sorted({proof[0] for proof in proofs}))
            for byte_end, digest, rows_inside in proofs:
                self.assertEqual(digest, __import__("hashlib").sha256(raw[:byte_end]).digest())
                self.assertEqual(raw[:byte_end].count(b"\n"), rows_inside)
            for earlier in previous[-3:]:
                self.assertIn(earlier.prefix_proofs()[-1], proofs)
            self.assertLessEqual(len(proofs), 8 + 4 + 1)
            previous.append(view)
        self.assertGreaterEqual(CACHE.metrics_snapshot()["segment_compactions"], 2)

    def test_chunking_does_not_change_the_answers(self):
        rows = self._rows(30)
        rows[7] = "malformed row\n"
        rows[11] = self._row(3)                      # a duplicate attempt
        self._write(rows)
        chunked = self._answers(self._read(False))
        CACHE.clear_cache()
        with mock.patch.object(CACHE, "_PARSE_CHUNK_LINES", 10_000):
            whole = self._answers(CACHE.registry_view(self.jobs))
        self.assertEqual(chunked, whole)
        self.assertTrue(chunked["duplicates"])


class RegistryLineageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        self.jobs.write_bytes(b"a\nb\n")

    def _key(self, size=None, ino=None, mtime=None):
        info = self.jobs.stat()
        return [info.st_dev, info.st_ino if ino is None else ino,
                info.st_size if size is None else size,
                info.st_mtime_ns if mtime is None else mtime,
                info.st_mode, info.st_ctime_ns]

    def test_chain_resolves_to_the_smallest_prefix_and_its_anchor(self):
        first, second, third = self._key(ino=1), self._key(ino=2), self._key(ino=3)
        CACHE.record_lineage(self.jobs, first, second, 2, "a" * 64)
        CACHE.record_lineage(self.jobs, second, third, 4, "b" * 64)
        self.assertEqual(CACHE.resolve_lineage(self.jobs, first, third), (2, "a" * 64))
        self.assertEqual(CACHE.resolve_lineage(self.jobs, second, third), (4, "b" * 64))

    def test_complete_chain_shorter_than_a_later_entry_wins(self):
        first, second, third = self._key(ino=1), self._key(ino=2), self._key(ino=3)
        CACHE.record_lineage(self.jobs, first, third, 8, "c" * 64)
        CACHE.record_lineage(self.jobs, first, second, 1, "a" * 64)
        CACHE.record_lineage(self.jobs, second, third, 3, "b" * 64)
        self.assertEqual(CACHE.resolve_lineage(self.jobs, first, third), (1, "a" * 64))

    def test_broken_or_missing_chain_falls_back(self):
        first, second, third = self._key(ino=1), self._key(ino=2), self._key(ino=3)
        CACHE.record_lineage(self.jobs, first, second, 2, "a" * 64)
        self.assertIsNone(CACHE.resolve_lineage(self.jobs, first, third))
        self.assertIsNone(CACHE.resolve_lineage(self.jobs, third, first))
        Path(CACHE.lineage_path(self.jobs)).write_text("{not json")
        self.assertIsNone(CACHE.resolve_lineage(self.jobs, first, second))

    def test_only_the_last_sixty_four_entries_are_kept(self):
        keys = [self._key(ino=i, size=i) for i in range(1, 72)]
        for parent, child in zip(keys, keys[1:]):
            CACHE.record_lineage(self.jobs, parent, child, 1, "a" * 64)
        data = json.loads(Path(CACHE.lineage_path(self.jobs)).read_text())
        self.assertEqual(len(data["entries"]), 64)
        self.assertIsNone(CACHE.resolve_lineage(self.jobs, keys[0], keys[-1]))
        self.assertEqual(CACHE.resolve_lineage(self.jobs, keys[-2], keys[-1])[0], 1)

    def test_malformed_entries_are_ignored_not_repaired(self):
        path = Path(CACHE.lineage_path(self.jobs))
        path.write_text(json.dumps({"schema": 1, "jobs": str(self.jobs.resolve()),
                                    "entries": [{"p": [1, 2], "c": [3, 4], "n": 1, "a": "x"}]}))
        self.assertIsNone(CACHE.resolve_lineage(self.jobs, [1, 2, 0, 0, 0, 0], [3, 4, 0, 0, 0, 0]))

    def test_common_prefix_is_line_aligned(self):
        self.assertEqual(CACHE.common_prefix_line_aligned(b"", b"a\n"), 0)
        self.assertEqual(CACHE.common_prefix_line_aligned(b"a\nb\n", b"a\nb\n"), 4)
        self.assertEqual(CACHE.common_prefix_line_aligned(b"a\nb\n", b"a\nc\n"), 2)
        self.assertEqual(CACHE.common_prefix_line_aligned(b"a\nlong-line\n", b"a\nlong-other\n"), 2)
        self.assertEqual(CACHE.common_prefix_line_aligned(b"a\nb", b"a\nb\nc\n"), 2)
        self.assertEqual(CACHE.common_prefix_line_aligned(b"x" * 3000000, b"x" * 2000000), 0)


if __name__ == "__main__":
    unittest.main()
