"""Incremental Codex lifecycle regression tests for long append-only rollouts."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet.collectors import codex, liveness
from fleet.model import Session
from fleet.tests import benchmark_tick


FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "codex_lifecycle", "open_task_1_4m.json"
)


def _event(event_type, turn_id):
    return json.dumps({
        "type": "event_msg",
        "payload": {"type": event_type, "turn_id": turn_id},
    }) + "\n"


class IncrementalLifecycleTest(unittest.TestCase):
    def setUp(self):
        codex._LIFECYCLE_CACHE.clear()
        codex._EXACT_LIFECYCLE_CACHE.clear()
        codex._LIFECYCLE_DISK_MARK.clear()
        codex._LIFECYCLE_CACHE_EVICTIONS = 0
        cache_home = tempfile.TemporaryDirectory()
        self.addCleanup(cache_home.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": cache_home.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, path, *records):
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(records)

    def test_open_start_1_4m_before_eof_enriches_as_working(self):
        with open(FIXTURE, encoding="utf-8") as handle:
            fixture = json.load(handle)
        with tempfile.TemporaryDirectory() as tmp:
            sid = "01234567-89ab-cdef-0123-456789abcdef"
            path = os.path.join(tmp, "rollout-2026-09-21T00-00-00-%s.jsonl" % sid)
            note = json.dumps({
                "type": "event_msg",
                "payload": {"type": "note", "text": "x" * fixture["trailing_note_bytes"]},
            }) + "\n"
            self._write(path, _event("task_started", fixture["turn_id"]), note)
            sess = Session(harness="codex", pid=43210, cwd=tmp)
            codex._PROC_PATHS[sess.pid] = path
            with mock.patch.object(codex, "_config_model_effort", return_value=(None, None)), \
                 mock.patch.object(codex, "_thread_titles", return_value={}), \
                 mock.patch.object(codex, "_thread_subagents", return_value={}), \
                 mock.patch.object(codex, "_tail_token_count", return_value=None), \
                 mock.patch.object(codex, "_tail_open_tool_call", return_value=None), \
                 mock.patch.object(codex, "_tail_pending_request_user_input", return_value=None):
                codex.enrich(sess)
            with mock.patch.object(liveness, "_alive", return_value=True):
                state = liveness.classify(sess, now=os.path.getmtime(path) + 1)
            self.assertEqual(sess.task_lifecycle, fixture["expected_lifecycle"])
            self.assertEqual(state, fixture["expected_liveness"])
            self.assertEqual(sess.state_evidence["source"], fixture["expected_source"])
            self.assertEqual(
                codex._latest_task_lifecycle(path),
                ("task_started", fixture["turn_id"]),
            )

    def test_unchanged_reads_nothing_and_append_starts_at_saved_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "append.jsonl")
            self._write(path, _event("task_started", "turn"))
            first_size = os.path.getsize(path)
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
            raw = codex._read_lifecycle_range
            with mock.patch.object(codex, "_read_lifecycle_range", wraps=raw) as read_range:
                self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
                read_range.assert_not_called()
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(_event("task_complete", "turn"))
                self.assertEqual(
                    codex._latest_task_lifecycle(path), ("task_complete", "turn")
                )
            self.assertEqual(read_range.call_count, 1)
            self.assertEqual(read_range.call_args.args[1], first_size)
            self.assertEqual(read_range.call_args.args[2], os.path.getsize(path))

    def test_partial_record_is_completed_by_only_the_next_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "partial.jsonl")
            self._write(path, _event("task_started", "turn"))
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
            terminal = _event("task_complete", "turn")
            split = len(terminal) // 2
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(terminal[:split])
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(terminal[split:])
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_complete", "turn"))

    def test_max_scan_none_reads_current_file_without_using_or_mutating_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "modes.jsonl")
            self._write(path, _event("task_started", "old"), "{}\n" * 200)
            canonical = os.path.realpath(path)
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "old"))
            cached = codex._LIFECYCLE_CACHE[canonical]

            # A contract-external middle rewrite is intentionally invisible to the
            # cursor. The exact native-subagent mode must still read current bytes.
            with open(path, "r+b") as handle:
                handle.write(_event("task_started", "new").encode("utf-8"))
                handle.seek(0, os.SEEK_END)
                handle.write(b"{}\n")

            self.assertEqual(
                codex._latest_task_lifecycle(path, max_scan=None),
                ("task_started", "new"),
            )
            self.assertIs(codex._LIFECYCLE_CACHE[canonical], cached)
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "old"))

    def test_max_scan_none_reads_replacement_immediately_without_evicting_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rotation.jsonl")
            self._write(path, _event("task_started", "old"))
            canonical = os.path.realpath(path)
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "old"))
            cached = codex._LIFECYCLE_CACHE[canonical]

            replacement = os.path.join(tmp, "replacement.jsonl")
            self._write(replacement, _event("task_started", "new"))
            os.replace(replacement, path)

            self.assertEqual(
                codex._latest_task_lifecycle(path, max_scan=None),
                ("task_started", "new"),
            )
            self.assertIs(codex._LIFECYCLE_CACHE[canonical], cached)
            # The session cursor notices the rotation and reads the new file at once.
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "new"))
            self.assertIsNot(codex._LIFECYCLE_CACHE[canonical], cached)

    def test_max_scan_none_reuses_unchanged_file_and_rescans_on_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "child.jsonl")
            self._write(path, _event("task_started", "t1"), _event("task_complete", "t1"))
            with mock.patch.object(
                codex, "_parse_latest_task_lifecycle",
                wraps=codex._parse_latest_task_lifecycle,
            ) as scan:
                for _ in range(2):
                    self.assertEqual(
                        codex._latest_task_lifecycle(path, max_scan=None),
                        ("task_complete", "t1"),
                    )
                self.assertEqual(scan.call_count, 1)
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(_event("task_started", "t2"))
                self.assertEqual(
                    codex._latest_task_lifecycle(path, max_scan=None),
                    ("task_started", "t2"),
                )
                self.assertEqual(scan.call_count, 2)
            self.assertNotIn(os.path.realpath(path), codex._LIFECYCLE_CACHE)

    def test_max_scan_none_io_failure_does_not_evict_session_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "exact-error.jsonl")
            self._write(path, _event("task_started", "turn"))
            canonical = os.path.realpath(path)
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
            cached = codex._LIFECYCLE_CACHE[canonical]
            with mock.patch.object(
                codex, "_parse_latest_task_lifecycle", side_effect=OSError
            ):
                self.assertIsNone(codex._latest_task_lifecycle(path, max_scan=None))
            self.assertIs(codex._LIFECYCLE_CACHE[canonical], cached)

    def test_append_timestamp_regression_before_or_after_read_rereads_from_zero(self):
        for phase in ("before", "after"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                codex._LIFECYCLE_CACHE.clear()
                path = os.path.join(tmp, "timestamp-%s.jsonl" % phase)
                canonical = os.path.realpath(path)
                self._write(path, _event("task_started", "turn"))
                self.assertEqual(
                    codex._latest_task_lifecycle(path), ("task_started", "turn")
                )
                cached = codex._LIFECYCLE_CACHE[canonical]
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write("{}\n")
                current = os.stat(path)
                # Only the first look at the clock is falsified; the reread sees the real one.
                regressed = SimpleNamespace(
                    st_dev=current.st_dev,
                    st_ino=current.st_ino,
                    st_size=current.st_size,
                    st_mtime_ns=(
                        cached.mtime_ns - 1
                        if phase == "before" else current.st_mtime_ns
                    ),
                    st_ctime_ns=(
                        current.st_ctime_ns
                        if phase == "before" else cached.ctime_ns - 1
                    ),
                )
                real = os.stat if phase == "before" else codex._stable_path_stat
                calls = []

                def first_call_regressed(*args, **kwargs):
                    calls.append(args)
                    return regressed if len(calls) == 1 else real(*args, **kwargs)

                with mock.patch.object(
                    codex.os if phase == "before" else codex,
                    "stat" if phase == "before" else "_stable_path_stat",
                    side_effect=first_call_regressed,
                ), mock.patch.object(
                    codex, "_initialize_lifecycle_cursor",
                    wraps=codex._initialize_lifecycle_cursor,
                ) as reread:
                    self.assertEqual(codex._latest_task_lifecycle(path),
                                     ("task_started", "turn"))
                self.assertEqual(reread.call_count, 1)
                self.assertIsNot(codex._LIFECYCLE_CACHE[canonical], cached)
                self.assertEqual(codex._LIFECYCLE_CACHE[canonical].offset, current.st_size)

    def test_saved_boundary_change_rereads_and_short_read_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "boundary.jsonl")
            canonical = os.path.realpath(path)
            self._write(path, _event("task_started", "turn"), "{}\n" * 200)
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
            cached = codex._LIFECYCLE_CACHE[canonical]
            with open(path, "r+b") as handle:
                handle.seek(cached.offset - 2)
                handle.write(b"[]")
                handle.seek(0, os.SEEK_END)
                handle.write(b"{}\n")
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
            self.assertIsNot(codex._LIFECYCLE_CACHE[canonical], cached)

            with open(path, "a", encoding="utf-8") as handle:
                handle.write("{}\n")
            with mock.patch.object(codex, "_cursor_boundary", side_effect=OSError):
                self.assertIsNone(codex._latest_task_lifecycle(path))
            self.assertNotIn(canonical, codex._LIFECYCLE_CACHE)

    def test_unrelated_malformed_json_is_ignored_but_bad_lifecycle_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "malformed.jsonl")
            self._write(path, _event("task_started", "turn"))
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("{not-json}\n")
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": ""},
                }) + "\n")
            self.assertIsNone(codex._latest_task_lifecycle(path))

    def test_rotation_and_truncation_reinitialize_in_the_same_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "changing.jsonl")
            canonical = os.path.realpath(path)
            self._write(path, _event("task_started", "old"))
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "old"))
            first = codex._LIFECYCLE_CACHE[canonical]
            replacement = os.path.join(tmp, "replacement.jsonl")
            self._write(replacement, _event("task_started", "new"))
            os.replace(replacement, path)
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "new"))
            rotated = codex._LIFECYCLE_CACHE[canonical]
            self.assertNotEqual((rotated.dev, rotated.ino), (first.dev, first.ino))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}\n")
            # Truncated below the saved offset: the file is read again, and it holds
            # no lifecycle event, so the answer is a true "none".
            self.assertIsNone(codex._latest_task_lifecycle(path))
            self.assertEqual(codex._LIFECYCLE_CACHE[canonical].offset, 3)
            self.assertIsNone(codex._latest_task_lifecycle(path))

    def test_deletion_permission_read_and_stat_errors_drop_stale_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "errors.jsonl")
            canonical = os.path.realpath(path)

            self._write(path, _event("task_started", "delete"))
            codex._latest_task_lifecycle(path)
            os.unlink(path)
            self.assertIsNone(codex._latest_task_lifecycle(path))
            self.assertNotIn(canonical, codex._LIFECYCLE_CACHE)

            self._write(path, _event("task_started", "permission"))
            codex._latest_task_lifecycle(path)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("{}\n")
            with mock.patch("builtins.open", side_effect=PermissionError):
                self.assertIsNone(codex._latest_task_lifecycle(path))
            self.assertNotIn(canonical, codex._LIFECYCLE_CACHE)

            self.assertEqual(
                codex._latest_task_lifecycle(path), ("task_started", "permission")
            )
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("{}\n")
            with mock.patch.object(codex, "_read_lifecycle_range", side_effect=OSError):
                self.assertIsNone(codex._latest_task_lifecycle(path))
            self.assertNotIn(canonical, codex._LIFECYCLE_CACHE)

            self.assertEqual(
                codex._latest_task_lifecycle(path), ("task_started", "permission")
            )
            with mock.patch.object(codex.os, "stat", side_effect=OSError):
                self.assertIsNone(codex._latest_task_lifecycle(path))
            self.assertNotIn(canonical, codex._LIFECYCLE_CACHE)

    def test_lru_is_bounded_by_file_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(codex._LIFECYCLE_CACHE_MAX + 1):
                path = os.path.join(tmp, "%04d.jsonl" % index)
                self._write(path, _event("task_started", str(index)))
                codex._latest_task_lifecycle(path)
            self.assertEqual(len(codex._LIFECYCLE_CACHE), codex._LIFECYCLE_CACHE_MAX)
            self.assertEqual(codex._LIFECYCLE_CACHE_EVICTIONS, 1)

    def test_live_budget_pins_exact_boundary_and_violation(self):
        result = {
            "benchmark_schema": benchmark_tick.BENCHMARK_SCHEMA,
            "mode": "live-descriptive-only",
            "warm_samples": [{"wall_ns": 11_000_000_000}],
        }
        self.assertTrue(benchmark_tick._live_budget(result, 11.0)["pass"])
        result["warm_samples"][0]["wall_ns"] += 1
        self.assertFalse(benchmark_tick._live_budget(result, 11.0)["pass"])


def _call(call_id, name="request_user_input", **payload):
    return json.dumps({
        "type": "response_item", "timestamp": "2026-08-03T00:00:00Z",
        "payload": {"type": "function_call", "name": name, "call_id": call_id, **payload},
    }) + "\n"


def _async_call(call_id, count):
    return _call(call_id, "request_user_input_async",
                 arguments=json.dumps({"questions": [{"title": "Q"}] * count}))


class DiskLifecycleCursorTest(unittest.TestCase):
    """A new process continues from the cursor an earlier one left, or reads it all."""

    def setUp(self):
        codex._LIFECYCLE_CACHE.clear()
        codex._EXACT_LIFECYCLE_CACHE.clear()
        codex._LIFECYCLE_DISK_MARK.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.path.join(self.tmp.name, "cache")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = os.path.join(self.tmp.name, "rollout.jsonl")
        self.canonical = os.path.realpath(self.path)

    def _write(self, *records):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.writelines(records)

    def _append(self, *records):
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.writelines(records)

    def _leave_cursor(self, *records):
        self._write(*records)
        codex._latest_task_lifecycle(self.path)
        saved = codex._LIFECYCLE_CACHE[self.canonical]
        self._new_process()
        return saved

    def _new_process(self):
        codex._LIFECYCLE_CACHE.clear()
        codex._EXACT_LIFECYCLE_CACHE.clear()
        codex._LIFECYCLE_DISK_MARK.clear()

    def _observe(self):
        """The lifecycle and pending input a call returns, and how it got there."""
        resumes = codex._LIFECYCLE_DISK_RESUMES
        with mock.patch.object(codex, "_initialize_lifecycle_cursor",
                               wraps=codex._initialize_lifecycle_cursor) as init, \
                mock.patch.object(codex, "_read_lifecycle_range",
                                  wraps=codex._read_lifecycle_range) as ranges:
            lifecycle = codex._latest_task_lifecycle(self.path)
            pending = codex._tail_pending_request_user_input(self.path)
        return {"lifecycle": lifecycle, "pending": pending, "initialized": init.call_count,
                "resumed": codex._LIFECYCLE_DISK_RESUMES - resumes,
                "starts": [call.args[1] for call in ranges.call_args_list]}

    def _from_zero(self):
        """What a process with no cursor at all answers for the same file."""
        self._new_process()
        with mock.patch.object(codex, "_load_disk_lifecycle_cursor", return_value=None):
            return self._observe()

    def _rows(self):
        return [_event("task_started", "t1"), _call("blocking"), _async_call("async", 3),
                _event("task_complete", "t1"), "{}\n" * 50]

    def test_a_new_process_continues_from_the_saved_offset_with_the_same_answer(self):
        saved = self._leave_cursor(*self._rows())
        self._append(_event("task_started", "t2"), _call("later"), "{}\n" * 5)
        resumed = self._observe()
        self.assertEqual((resumed["initialized"], resumed["resumed"]), (0, 1))
        self.assertEqual(resumed["starts"], [saved.offset])
        fresh = self._from_zero()
        self.assertEqual((fresh["initialized"], fresh["resumed"], fresh["starts"][0]), (1, 0, 0))
        self.assertEqual({k: v for k, v in resumed.items() if k in ("lifecycle", "pending")},
                         {k: v for k, v in fresh.items() if k in ("lifecycle", "pending")})
        self.assertEqual(resumed["lifecycle"], ("task_started", "t2"))
        self.assertEqual(resumed["pending"]["call_id"], "later")

    def test_pending_input_state_survives_the_saved_cursor_exactly(self):
        saved = self._leave_cursor(*self._rows()[:3], _call("blocking-2"))
        self.assertGreater(len(saved.pending_calls), 1)
        self._append("{}\n")
        resumed = codex._LIFECYCLE_CACHE.get(self.canonical) or None
        self.assertIsNone(resumed)
        self.assertEqual(self._observe()["resumed"], 1)
        loaded = codex._LIFECYCLE_CACHE[self.canonical].pending_calls
        self.assertEqual(list(loaded), list(saved.pending_calls))
        self.assertEqual(loaded, saved.pending_calls)

    def test_an_unchanged_file_is_answered_from_the_cursor_without_reading_rows(self):
        saved = self._leave_cursor(*self._rows())
        seen = self._observe()
        self.assertEqual((seen["initialized"], seen["resumed"], seen["starts"]), (0, 1, [saved.offset]))
        self.assertEqual(seen["lifecycle"], saved.lifecycle)

    def test_every_mismatch_reads_from_byte_zero(self):
        def boundary(saved):
            # Past the head digest: only the boundary digest can see this change.
            self.assertGreaterEqual(saved.offset - codex._LIFECYCLE_BOUNDARY_BYTES,
                                    codex._LIFECYCLE_HEAD_BYTES)
            with open(self.path, "r+b") as handle:
                handle.seek(saved.offset - 2)
                handle.write(b"[]")
            self._append("{}\n")

        def head(saved):
            with open(self.path, "r+b") as handle:
                handle.seek(4)
                handle.write(b"Z")
            self._append("{}\n")

        def inode(saved):
            replacement = self.path + ".new"
            with open(self.path, "rb") as source, open(replacement, "wb") as target:
                target.write(source.read() + b"{}\n")
            os.replace(replacement, self.path)

        def regrown(saved):
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.write(_event("task_started", "other") + "{}\n" * 2000)
            self.assertGreater(os.path.getsize(self.path), saved.offset)

        def damaged(saved):
            directory = os.path.join(self.tmp.name, "cache", "hearting", "fleet-parses")
            for name in os.listdir(directory):
                full = os.path.join(directory, name)
                with open(full, encoding="utf-8") as stream:
                    record = json.load(stream)
                record["value"]["offset"] = "not a number"
                with open(full, "w", encoding="utf-8") as stream:
                    json.dump(record, stream)
            self._append("{}\n")

        def other_parser_version(saved):
            self._append("{}\n")

        cases = [("boundary", boundary, None), ("head", head, None), ("inode", inode, None),
                 ("truncated then longer", regrown, None), ("schema", damaged, None),
                 ("parser version", other_parser_version, "another-release")]
        for name, mutate, version in cases:
            with self.subTest(name):
                self._new_process()
                # Longer than the head digest plus the boundary window (4 KiB + 256 B).
                saved = self._leave_cursor(*self._rows(), "{}\n" * 1500)
                mutate(saved)
                with mock.patch.object(codex.parse_cache, "_VERSION",
                                       version or codex.parse_cache._VERSION):
                    got = self._observe()
                self.assertEqual((got["resumed"], got["initialized"], got["starts"][0]), (0, 1, 0), name)
                want = self._from_zero()
                self.assertEqual((got["lifecycle"], got["pending"]),
                                 (want["lifecycle"], want["pending"]), name)

    def test_an_unterminated_last_row_is_not_saved_until_it_completes(self):
        self._write(_event("task_started", "t1"), '{"type":"event_msg","payload":{"type":"task_com')
        with mock.patch.object(codex.parse_cache, "save") as save:
            codex._latest_task_lifecycle(self.path)
            self.assertEqual(save.call_count, 0)
            self._append('plete","turn_id":"t1"}}\n')
            self.assertEqual(codex._latest_task_lifecycle(self.path), ("task_complete", "t1"))
            self.assertEqual(save.call_count, 1)

    def test_growth_is_saved_only_every_interval(self):
        self._write(_event("task_started", "t1"))
        with mock.patch.object(codex.parse_cache, "save") as save, \
                mock.patch.object(codex, "_LIFECYCLE_SAVE_INTERVAL", 300):
            codex._latest_task_lifecycle(self.path)
            self.assertEqual(save.call_count, 1)
            for _ in range(5):                      # 15 bytes each: below the interval
                self._append("{}\n" * 5)
                codex._latest_task_lifecycle(self.path)
            self.assertEqual(save.call_count, 1)
            self._append("{}\n" * 120)
            codex._latest_task_lifecycle(self.path)
            self.assertEqual(save.call_count, 2)

    def test_exact_scan_reuses_only_an_identical_stat_from_disk(self):
        self._write(_event("task_started", "t1"), _event("task_complete", "t1"))
        self.assertEqual(codex._latest_task_lifecycle(self.path, max_scan=None),
                         ("task_complete", "t1"))
        self._new_process()
        with mock.patch.object(codex, "_parse_latest_task_lifecycle",
                               wraps=codex._parse_latest_task_lifecycle) as scan:
            self.assertEqual(codex._latest_task_lifecycle(self.path, max_scan=None),
                             ("task_complete", "t1"))
            self.assertEqual(scan.call_count, 0)
            self._new_process()
            self._append(_event("task_started", "t2"))
            self.assertEqual(codex._latest_task_lifecycle(self.path, max_scan=None),
                             ("task_started", "t2"))
            self.assertEqual(scan.call_count, 1)

    def test_a_stored_value_is_checked_before_use(self):
        valid = {"dev": 1, "ino": 2, "offset": 3, "mtime_ns": 4, "ctime_ns": 5,
                 "boundary_sha256": "a" * 64, "head_sha256": "b" * 64,
                 "lifecycle": ["task_started", "t"],
                 "pending": [["c", 1.5, "t", [0, 2], False], ["d", None, None, None, True]]}
        check = lambda value, parser="codex-lifecycle-cursor": codex.parse_cache._valid_value(parser, value)
        self.assertTrue(check(valid))
        for field, bad in (("offset", -1), ("dev", True), ("boundary_sha256", "xyz"),
                           ("lifecycle", ["bogus", "t"]), ("lifecycle", ["task_started"]),
                           ("pending", [["c", float("nan"), None, None, False]]),
                           ("pending", [["c", None, None, [2, 0], False]]),
                           ("pending", [["c", None, None, None, False]] * 2),
                           ("pending", "none")):
            self.assertFalse(check({**valid, field: bad}), (field, bad))
        self.assertFalse(check({k: v for k, v in valid.items() if k != "head_sha256"}))
        self.assertFalse(check({**valid, "carry": ""}))
        self.assertTrue(check({"lifecycle": None}, "codex-lifecycle-exact"))
        self.assertFalse(check({"lifecycle": ["task_started", ""]}, "codex-lifecycle-exact"))
        self.assertFalse(check({"lifecycle": None, "extra": 1}, "codex-lifecycle-exact"))


def _decode_every_row(lifecycle, carry, data, pending_calls=None):
    """The reader before rows were skipped by name: every complete row is decoded."""
    parts = (carry + data).split(b"\n")
    carry = parts.pop()
    for raw_line in parts:
        try:
            row = json.loads(raw_line)
        except Exception:
            row = None
        if pending_calls is not None:
            codex._advance_pending_input(pending_calls, lifecycle, row)
        lifecycle = codex._advance_task_lifecycle_row(lifecycle, row)
    return lifecycle, carry


class SkippedRowsEquivalenceTest(unittest.TestCase):
    """Skipping the rows that name none of the lifecycle events gives the same cursor."""

    @staticmethod
    def _rows(seed, count):
        import random
        rng = random.Random(seed)
        calls, rows = [], []
        for index in range(count):
            kind = rng.choice(("tool", "tool", "tool", "output", "start", "done", "abort", "ask", "ask_async",
                               "reply", "prompt", "delivery", "prose", "junk", "odd"))
            call = "call-%d" % rng.randrange(8)
            turn = "turn-%d" % rng.randrange(4)
            stamp = "2026-11-13T00:00:%02dZ" % (index % 60)
            if kind == "tool":
                row = {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                                                            "call_id": call, "arguments": "{}"}}
            elif kind == "output":
                row = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": call,
                                                            "output": json.dumps({"accepted": rng.random() < .5})}}
            elif kind in ("start", "done", "abort"):
                row = {"type": "event_msg", "payload": {
                    "type": {"start": "task_started", "done": "task_complete", "abort": "turn_aborted"}[kind],
                    "turn_id": turn if rng.random() < .9 else ""}}
            elif kind == "ask":
                row = {"type": "response_item", "payload": {"type": "function_call", "name": "request_user_input",
                                                            "call_id": call, "timestamp": stamp}, "timestamp": stamp}
            elif kind == "ask_async":
                row = {"type": "response_item", "timestamp": stamp, "payload": {
                    "type": "function_call", "name": "request_user_input_async", "call_id": call,
                    "arguments": json.dumps({"questions": [{"title": "q"}] * rng.randrange(0, 3)}),
                    "internal_chat_message_metadata_passthrough": {"turn_id": turn}}}
            elif kind == "reply":
                body = json.dumps([{"answer": "a", "questionItemId": json.dumps(
                    ["request_user_input_async", call, rng.randrange(2)])}])
                row = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "<send_user_message_question_reply>" + body
                     + "</send_user_message_question_reply>"}]}}
            elif kind == "prompt":
                row = {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                    "type": "UserMessage", "client_id": "11111111-2222-3333-4444-555555555555", "content": []}}}
            elif kind == "delivery":
                row = {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                    "type": "UserMessage", "client_id": "delivery-abc", "content": []}}}
            elif kind == "prose":
                row = {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "the word task_started or request_user_input in prose"}]}}
            elif kind == "junk":
                rows.append("{not json task_started\n")
                continue
            else:
                row = {"type": "turn_context", "payload": {"cwd": "/x", "note": "x" * rng.randrange(0, 400)}}
            rows.append(json.dumps(row) + "\n")
        return rows

    def _both(self, data, chunk):
        results = []
        for consume in (_decode_every_row, codex._consume_lifecycle_bytes):
            lifecycle, carry, pending, offset = None, b"", {}, 0
            while offset < len(data):
                lifecycle, carry = consume(lifecycle, carry, data[offset:offset + chunk], pending)
                offset += chunk
            plain, plain_carry = None, b""
            for offset in range(0, len(data), chunk):
                plain, plain_carry = consume(plain, plain_carry, data[offset:offset + chunk], None)
            results.append((lifecycle, carry, dict(pending), plain, plain_carry))
        return results

    def test_a_random_rollout_gives_the_same_cursor_with_and_without_the_skipping(self):
        for seed in range(40):
            data = "".join(self._rows(seed, 300 + seed * 7)).encode()
            for chunk in (64, 4096, 65536):
                reference, current = self._both(data, chunk)
                self.assertEqual(current, reference, (seed, chunk))

    def test_the_skipping_really_skips_the_bulk_rows(self):
        bulk = (json.dumps({"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                                                                  "call_id": "c", "arguments": "{}"}}) + "\n") * 400
        with mock.patch.object(codex.json, "loads", wraps=json.loads) as loads:
            codex._consume_lifecycle_bytes(None, b"", bulk.encode(), {})
            codex._consume_lifecycle_bytes(None, b"", bulk.encode(), None)
        self.assertEqual(loads.call_count, 0)
        started = (json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t"}}) + "\n").encode()
        self.assertEqual(codex._consume_lifecycle_bytes(None, b"", bulk.encode() + started, {})[0],
                         ("task_started", "t"))

    def test_a_tool_result_is_decoded_only_while_a_question_call_is_open(self):
        ask = (json.dumps({"type": "response_item", "payload": {"type": "function_call", "name": "request_user_input",
                                                               "call_id": "q"}}) + "\n").encode()
        output = (json.dumps({"type": "response_item", "payload": {"type": "function_call_output",
                                                                  "call_id": "q"}}) + "\n").encode()
        pending = {}
        codex._consume_lifecycle_bytes(None, b"", ask, pending)
        self.assertIn("q", pending)
        codex._consume_lifecycle_bytes(None, b"", output, pending)
        self.assertEqual(pending, {})


if __name__ == "__main__":
    unittest.main()
