"""Exact-file reuse and unchanged snapshots when terminal retention runs early."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import builtins
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import collectors, model, render
from fleet.collectors import dispatch, parse_cache, procscan


class ParseCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.path = self.root / "attempt.jsonl"
        self.path.write_text(json.dumps({"type": "thread.started", "thread_id": "thread-a"}) + "\n")
        dispatch._CODEX_ATTEMPT_CACHE.clear()

    def test_job_failure_fallback_copies_mutable_fields_and_shares_node_evidence(self):
        jobs = [{"attempt_id": "att-1", "history": [{"status": "done"}]}]
        node_evidence = {"rt-1": {"execute": {"attempt_history": [{"order": 1}]}}}
        degradations = {"rt-1": {"state": "partial"}}
        pending_delivery = {"count": 2}
        cached = collectors._copy_job_observation_fallback(
            jobs, node_evidence, degradations, pending_delivery, malformed=3)

        jobs[0]["history"][0]["status"] = "changed"
        self.assertEqual(cached.jobs[0]["history"][0]["status"], "done")
        self.assertIs(cached.node_evidence, node_evidence)
        self.assertEqual(cached.malformed, 3)
        self.assertFalse(hasattr(cached, "resources"))

        cached.degradations["rt-1"]["state"] = "changed"
        cached.pending_delivery["count"] = 0
        self.assertEqual(degradations["rt-1"]["state"], "partial")
        self.assertEqual(pending_delivery["count"], 2)

    def test_fast_first_defers_optional_steward_enrichment(self):
        from fleet.collectors import steward
        sessions = [object()]
        with mock.patch.object(steward, "enrich") as enrich:
            collectors._enrich_steward(sessions, fast_first=True)
            enrich.assert_not_called()
            collectors._enrich_steward(sessions, fast_first=False)
        enrich.assert_called_once_with(sessions)

    def test_second_process_cache_does_not_read_source(self):
        cold = dispatch._parse_codex_attempt_tail(str(self.path))
        dispatch._CODEX_ATTEMPT_CACHE.clear()  # empty memory, as on next Fleet startup
        original = builtins.open
        def opened(path, *args, **kwargs):
            if str(path) == str(self.path):
                raise AssertionError("unchanged source read on disk-cache hit")
            return original(path, *args, **kwargs)
        with mock.patch.object(builtins, "open", side_effect=opened):
            self.assertEqual(dispatch._parse_codex_attempt_tail(str(self.path)), cold)

    def test_append_truncate_replace_and_same_size_edit_invalidate(self):
        path = str(self.path)
        self.assertEqual(dispatch._parse_codex_attempt_tail(path)["thread_id"], "thread-a")
        with self.path.open("a") as stream:
            stream.write(json.dumps({"type": "thread.started", "thread_id": "thread-b"}) + "\n")
        self.assertTrue(dispatch._parse_codex_attempt_tail(path)["thread_ambiguity"])
        self.path.write_text('{"type":"thread.started","thread_id":"thread-c"}\n')
        self.assertEqual(dispatch._parse_codex_attempt_tail(path)["thread_id"], "thread-c")
        stamp = self.path.stat()
        replacement = self.root / "replacement"
        replacement.write_bytes(self.path.read_bytes().replace(b"thread-c", b"thread-d"))
        os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        replacement.replace(self.path)
        self.assertEqual(dispatch._parse_codex_attempt_tail(path)["thread_id"], "thread-d")
        stamp = self.path.stat()
        self.path.write_bytes(self.path.read_bytes().replace(b"thread-d", b"thread-e"))
        os.utime(self.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
        dispatch._CODEX_ATTEMPT_CACHE.clear()
        self.assertEqual(dispatch._parse_codex_attempt_tail(path)["thread_id"], "thread-e")
        self.assertEqual(len(list(parse_cache._directory().glob("*.json"))), 1)

    def test_corrupt_or_unwritable_cache_falls_back_to_source(self):
        cold = dispatch._parse_codex_attempt_tail(str(self.path))
        next(parse_cache._directory().glob("*.json")).write_text("broken JSON")
        dispatch._CODEX_ATTEMPT_CACHE.clear()
        self.assertEqual(dispatch._parse_codex_attempt_tail(str(self.path)), cold)
        cache_block = self.root / "blocked"
        cache_block.write_text("file, not directory")
        with mock.patch.object(parse_cache, "_directory", return_value=cache_block / "cache"):
            dispatch._CODEX_ATTEMPT_CACHE.clear()
            self.assertEqual(dispatch._parse_codex_attempt_tail(str(self.path)), cold)

    def test_changed_source_during_parse_is_not_published(self):
        stamp = self.path.stat()
        sig = (stamp.st_mtime_ns, stamp.st_size, stamp.st_dev, stamp.st_ino)
        key = parse_cache.key("test", str(self.path), sig, ())
        self.path.write_text("changed\n")
        parse_cache.save(key, {"thread_id": "old"}, str(self.path), sig)
        self.assertIsNone(parse_cache.load(key))

    def test_valid_json_with_damaged_value_reparses_for_every_harness(self):
        fixtures = (
            (dispatch._parse_codex_attempt_tail, dispatch._CODEX_ATTEMPT_CACHE,
             {"type": "thread.started", "thread_id": "thread-a"},
             ({"activity": {}}, {"token_usage": ["damaged"]}, {"exec_tool": []})),
            (dispatch._parse_claude_stream_tail, dispatch._CLAUDE_STREAM_CACHE,
             {"type": "system", "subtype": "init", "session_id": "session-a"},
             ({"session_id": []}, {"active_context_tokens": {}}, {"ambiguity": True})),
            (dispatch._parse_opencode_attempt_tail, dispatch._OPENCODE_ATTEMPT_CACHE,
             {"type": "step_finish", "sessionID": "session-a", "part": {"tokens": {"input": 100}}},
             ({"active_context_tokens": []}, {"session_id": {}}, {"ambiguity": 123})),
        )
        for index, (parse, memory, event, corruptions) in enumerate(fixtures):
            path = self.root / ("fixture-%d.jsonl" % index)
            path.write_text(json.dumps(event) + "\n")
            memory.clear()
            good = parse(str(path))
            for corruption in corruptions:
                # Find the cached record by its valid parser value, then damage only
                # that value: the source stamp/signature deliberately stays exact.
                cache = next(p for p in parse_cache._directory().glob("*.json")
                             if json.loads(p.read_text())["value"] == good)
                record = json.loads(cache.read_text())
                record["value"].update(corruption)
                cache.write_text(json.dumps(record))
                memory.clear()
                self.assertEqual(parse(str(path)), good, corruption)


class RetentionGoldenTest(unittest.TestCase):
    def test_dropped_owners_never_enrich_and_all_snapshots_match(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            registry = root / "jobs.log"
            rows = []
            for harness, event in (
                ("claude", {"type": "system", "subtype": "init", "session_id": "cl-live", "model": "claude-sonnet-4-6"}),
                ("codex", {"type": "thread.started", "thread_id": "cx-live"}),
                ("opencode", {"type": "step_finish", "sessionID": "oc-live", "part": {"tokens": {"input": 100}}}),
            ):
                for old in (True, False):
                    attempt = "att-" + harness + ("-old" if old else "-live")
                    log = logs / ("worker." + attempt + "." + harness + ".jsonl")
                    log.write_text(json.dumps(event) + "\n")
                    meta = ",".join(("attempt_id=" + attempt, "harness=" + harness,
                                     "dispatch_depth=1", "worker_type=owner" if old else "worker_type=worker",
                                     "note=dead-runtime-exit" if old else "note=",
                                     "log_file=" + str(log), "pid=" + str(os.getpid()),
                                     "pid_start=" + str(procscan.read_proc_start(os.getpid()))))
                    # Old terminal rows cannot claim a live PID; active rows keep its identity.
                    if old:
                        meta = meta.replace("pid=" + str(os.getpid()), "pid=99999991")
                    rows.append("%s\t%s\tfixture\t%s\t%s\t%s\n" % (
                        "2000-01-01T00:00:00Z" if old else "2099-01-01T00:00:00Z",
                        "done" if old else "open", root, attempt, meta))
            registry.write_text("".join(rows))
            before_bytes = registry.read_bytes()
            stack.enter_context(mock.patch.dict(os.environ, {"HOME": tmp, "AGENT_HOME": tmp,
                "AGENT_DISPATCH_JOBS": str(registry), "XDG_CACHE_HOME": str(root / "cache")}, clear=True))
            stack.enter_context(mock.patch.object(dispatch, "_scan_processes", return_value=[]))
            stack.enter_context(mock.patch.object(dispatch, "_candidate_jobs_paths", return_value=[str(registry)]))
            stack.enter_context(mock.patch.object(dispatch, "_live_attempt_ids", return_value=set()))
            stack.enter_context(mock.patch.object(dispatch, "_codex_attempt_rollout", return_value=None))
            stack.enter_context(mock.patch.object(dispatch, "_scan_registry_evidence", return_value=({}, {})))
            stack.enter_context(mock.patch("time.time", return_value=2_000_000_000.0))
            retain = dispatch._retain_dead_terminal_owners
            calls = []
            def late_retention(jobs, *args, **kwargs):
                calls.append(1)
                return jobs if len(calls) == 1 else retain(jobs, *args, **kwargs)
            # Replay the previous placement: all details, then the identical retention rule.
            with mock.patch.object(dispatch, "_retain_dead_terminal_owners", side_effect=late_retention):
                model.reset_state_tracker()
                baseline = dispatch.collect(jobs_path=str(registry))
            with mock.patch.object(dispatch, "_enrich_codex_attempt_session", wraps=dispatch._enrich_codex_attempt_session) as enrich:
                model.reset_state_tracker()
                optimized = dispatch.collect(jobs_path=str(registry))
            self.assertEqual(len(enrich.call_args_list), 3)
            self.assertTrue(all("-old" not in call.args[0].attempt_id for call in enrich.call_args_list))
            self.assertEqual([j.to_dict() for j in baseline], [j.to_dict() for j in optimized])
            self.assertEqual(registry.read_bytes(), before_bytes)
            for show_all in (False, True):
                render.set_show_all(show_all)
                for process_view in (False, True):
                    render.set_process_view(process_view)
                    for width in (80, 120, 180):
                        a = render._build_lines([], baseline, "both", width < 100, 0,
                                                term_width=width, governor=None)
                        b = render._build_lines([], optimized, "both", width < 100, 0,
                                                term_width=width, governor=None)
                        self.assertEqual(a, b, (show_all, process_view, width))
            render.set_show_all(False)
            render.set_process_view(False)



class RegistryGenerationRetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        self.cache = dispatch._dispatch_registry_cache
        self._clear_caches()
        self.addCleanup(self._clear_caches)

    @staticmethod
    def _row(attempt, status="open"):
        return ("2026-10-20T00:00:00Z\t%s\t/repo\t/wt\tslug-%s\t"
                "attempt_id=%s,route_node=execute\n") % (status, attempt, attempt)

    @staticmethod
    def _cache_maps():
        return (dispatch._PARSED_REGISTRY_ROWS, dispatch._PARSED_REGISTRY_INDEXES,
                dispatch._PARSED_REGISTRY_LINE_COUNTS,
                dispatch._PARSED_REGISTRY_SEGMENTS,
                dispatch._PARSED_REGISTRY_DIAGNOSTICS)

    def _clear_caches(self):
        with dispatch._PARSED_REGISTRY_CACHE_LOCK:
            for cache in (*self._cache_maps(), dispatch._PARSED_REGISTRY_PATH_ORDER):
                cache.clear()
        self.cache.clear_cache()

    def _assert_only_path_generation(self, generation):
        key = (str(self.jobs.absolute()), generation)
        for cache in (dispatch._PARSED_REGISTRY_ROWS,
                      dispatch._PARSED_REGISTRY_LINE_COUNTS,
                      dispatch._PARSED_REGISTRY_SEGMENTS,
                      dispatch._PARSED_REGISTRY_DIAGNOSTICS):
            self.assertEqual(set(cache), {key})
        self.assertLessEqual(set(dispatch._PARSED_REGISTRY_INDEXES), {key})
        self.assertEqual(set(dispatch._PARSED_REGISTRY_PATH_ORDER), {key[0]})
        self.assertEqual(dispatch._PARSED_REGISTRY_PATH_ORDER[key[0]], key)

    def test_same_path_rewrites_release_superseded_maps_and_keep_returned_snapshots(self):
        self.jobs.write_text(self._row("att000"), encoding="utf-8")
        base_mtime = 1_800_000_000_000_000_000
        os.utime(self.jobs, ns=(base_mtime, base_mtime))
        first_rows = dispatch.parsed_registry_rows(str(self.jobs))
        first_index = dispatch._registry_derived_index(str(self.jobs), first_rows)
        stamp = self.jobs.stat()
        generation = (stamp.st_dev, stamp.st_ino, stamp.st_size,
                      stamp.st_mtime_ns, stamp.st_ctime_ns)
        self._assert_only_path_generation(generation)
        self.assertIs(dispatch.parsed_registry_rows(str(self.jobs)), first_rows)
        self.assertIs(dispatch._registry_derived_index(str(self.jobs), first_rows), first_index)

        previous_rows = first_rows
        for index in range(1, 41):
            attempt = "att%03d" % index
            mtime = base_mtime + index * 1_000_000_000
            self.jobs.write_text(self._row(attempt), encoding="utf-8")
            os.utime(self.jobs, ns=(mtime, mtime))
            rows = dispatch.parsed_registry_rows(str(self.jobs))
            derived = dispatch._registry_derived_index(str(self.jobs), rows)
            stamp = self.jobs.stat()
            generation = (stamp.st_dev, stamp.st_ino, stamp.st_size,
                          stamp.st_mtime_ns, stamp.st_ctime_ns)
            self._assert_only_path_generation(generation)
            self.assertEqual(rows[0][1][5], "attempt_id=%s,route_node=execute" % attempt)
            self.assertIs(dispatch.parsed_registry_rows(str(self.jobs)), rows)
            self.assertIs(dispatch._registry_derived_index(str(self.jobs), rows), derived)
            previous_rows = rows

        self.assertEqual(first_rows[0][1][5], "attempt_id=att000,route_node=execute")
        self.assertEqual(first_index["latest_by_attempt"]["att000"][1][1], "open")
        self.assertEqual(previous_rows[0][1][5], "attempt_id=att040,route_node=execute")

    def test_older_pinned_reader_returns_its_snapshot_without_replacing_current_cache(self):
        self.jobs.write_text(self._row("att-old"), encoding="utf-8")
        ready = threading.Event()
        resume = threading.Event()
        old_rows = []
        errors = []

        def read_pinned_generation():
            try:
                with self.cache.registry_read_scope():
                    dispatch._pinned_registry_lines(str(self.jobs))
                    ready.set()
                    if not resume.wait(5):
                        raise AssertionError("timed out waiting to read pinned generation")
                    old_rows.append(dispatch.parsed_registry_rows(str(self.jobs)))
            except BaseException as exc:
                errors.append(exc)

        reader = threading.Thread(target=read_pinned_generation)
        reader.start()
        try:
            self.assertTrue(ready.wait(5), "reader did not pin the initial generation")
            self.jobs.write_text(self._row("att-new"), encoding="utf-8")
            new_rows = dispatch.parsed_registry_rows(str(self.jobs))
        finally:
            resume.set()
            reader.join(5)
        self.assertFalse(reader.is_alive(), "pinned reader did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(old_rows[0][0][1][5], "attempt_id=att-old,route_node=execute")
        self.assertEqual(new_rows[0][1][5], "attempt_id=att-new,route_node=execute")
        stamp = self.jobs.stat()
        generation = (stamp.st_dev, stamp.st_ino, stamp.st_size,
                      stamp.st_mtime_ns, stamp.st_ctime_ns)
        self._assert_only_path_generation(generation)

    def test_append_reuses_only_the_latest_retained_prefix(self):
        base = self._row("att000") + self._row("att001")
        self.jobs.write_text(base, encoding="utf-8")
        with mock.patch.object(dispatch, "_registry_record_metadata",
                               wraps=dispatch._registry_record_metadata) as convert:
            with self.cache.registry_read_scope():
                prefix_rows = dispatch.parsed_registry_rows(str(self.jobs))
            self.assertEqual(convert.call_count, 2)
            with self.jobs.open("ab") as stream:
                stream.write(self._row("att002").encode("utf-8"))
            with self.cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(str(self.jobs))
            self.assertEqual(convert.call_count, 3)
        self.assertEqual(len(rows), 3)
        self.assertIs(rows[0], prefix_rows[0])
        self.assertIs(rows[1], prefix_rows[1])
        stamp = self.jobs.stat()
        generation = (stamp.st_dev, stamp.st_ino, stamp.st_size,
                      stamp.st_mtime_ns, stamp.st_ctime_ns)
        self._assert_only_path_generation(generation)

    def test_errors_replace_old_diagnostics_and_distinct_paths_share_one_lru_limit(self):
        missing = self.jobs.with_name("missing.log")
        self.assertEqual(dispatch.parsed_registry_rows(str(missing)), ())
        self.assertIsInstance(dispatch._registry_diagnostics(str(missing))[0], FileNotFoundError)
        missing.write_text(self._row("att-fixed"), encoding="utf-8")
        recovered = dispatch.parsed_registry_rows(str(missing))
        self.assertEqual(recovered[0][1][5], "attempt_id=att-fixed,route_node=execute")
        self.assertEqual(dispatch._registry_diagnostics(str(missing)), (None, 0))

        raced = self.jobs.with_name("raced.log")
        raced.write_text(self._row("att-before"), encoding="utf-8")
        dispatch.parsed_registry_rows(str(raced))
        raced.write_text(self._row("att-during"), encoding="utf-8")

        def remove_during_read(*args, **kwargs):
            raced.unlink()
            raise FileNotFoundError(str(raced))

        # The shared view reads first; its failure falls through to the text read.
        with mock.patch.object(Path, "read_text", side_effect=remove_during_read), \
                mock.patch.object(self.cache, "registry_view", side_effect=OSError("unreadable")):
            self.assertEqual(dispatch.parsed_registry_rows(str(raced)), ())
        self.assertIsInstance(dispatch._registry_diagnostics(str(raced))[0], FileNotFoundError)
        self.assertFalse(any(key[0] == str(raced.absolute())
                             for key in dispatch._PARSED_REGISTRY_ROWS))

        denied = self.jobs.with_name("denied.log")
        denied.write_text(self._row("att-denied"), encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")), \
                mock.patch.object(self.cache, "registry_view", side_effect=PermissionError("denied")):
            self.assertEqual(dispatch.parsed_registry_rows(str(denied)), ())
        self.assertIsInstance(dispatch._registry_diagnostics(str(denied))[0], PermissionError)
        self.assertFalse(any(key[0] == str(denied.absolute())
                             for key in dispatch._PARSED_REGISTRY_ROWS))
        self.assertEqual(dispatch.parsed_registry_rows(str(denied))[0][1][5],
                         "attempt_id=att-denied,route_node=execute")
        self.assertEqual(dispatch._registry_diagnostics(str(denied)), (None, 0))

        for index in range(40):
            path = Path(self.tmp.name) / ("registry-%02d.log" % index)
            path.write_text(self._row("path%03d" % index), encoding="utf-8")
            rows = dispatch.parsed_registry_rows(str(path))
            dispatch._registry_derived_index(str(path), rows)
        limit = dispatch._PARSED_REGISTRY_ROWS_LIMIT
        self.assertEqual(len(dispatch._PARSED_REGISTRY_PATH_ORDER), limit)
        for cache in self._cache_maps():
            self.assertLessEqual(len(cache), limit)
        retained_paths = set(dispatch._PARSED_REGISTRY_PATH_ORDER)
        for cache in self._cache_maps():
            self.assertEqual({key[0] for key in cache}, retained_paths)


    def test_a_rewrite_keeps_the_rows_of_segments_it_did_not_change(self):
        rows = [self._row("att%03d" % n, "done") for n in range(10)]
        self.jobs.write_text("".join(rows), encoding="utf-8")
        with mock.patch.object(self.cache, "_PARSE_CHUNK_LINES", 3), \
                mock.patch.object(dispatch, "_registry_record_metadata",
                                  wraps=dispatch._registry_record_metadata) as convert:
            first = dispatch.parsed_registry_rows(str(self.jobs))
            self.assertEqual(convert.call_count, 10)
            staged = self.jobs.with_name("jobs.staged")
            rows[8] = self._row("att008", "killed")
            rows.append(self._row("att010", "done"))
            staged.write_text("".join(rows), encoding="utf-8")
            os.replace(staged, self.jobs)
            second = dispatch.parsed_registry_rows(str(self.jobs))
            # Two untouched segments keep their rows; the changed segment (rows 6-8,
            # of which rows 6 and 7 convert by their exact fields) and the new row do not.
            self.assertEqual(convert.call_count, 10 + 1 + 1)
        self.assertEqual(len(second), 11)
        for index in range(6):
            self.assertIs(second[index], first[index])
        self.assertIsNot(second[8], first[8])
        self.assertEqual(second[8][1][1], "killed")
        self.cache.clear_cache()
        with dispatch._PARSED_REGISTRY_CACHE_LOCK:
            for cache in (*self._cache_maps(), dispatch._PARSED_REGISTRY_PATH_ORDER):
                cache.clear()
        fresh = dispatch.parsed_registry_rows(str(self.jobs))
        self.assertEqual([(o, f, dict(m)) for o, f, m in second],
                         [(o, f, dict(m)) for o, f, m in fresh])

    def test_a_direct_read_outside_a_pass_reads_the_file_once_through_the_shared_view(self):
        self.jobs.write_text(self._row("att-a") + self._row("att-b", "done"), encoding="utf-8")
        self.cache.reset_metrics()
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("text re-read")):
            rows = dispatch.parsed_registry_rows(str(self.jobs))
            again = dispatch.parsed_registry_rows(str(self.jobs))
        self.assertIs(again, rows)
        self.assertEqual(self.cache.metrics_snapshot()["content_reads"], 1)
        self.assertEqual([row[1][5] for row in rows],
                         ["attempt_id=att-a,route_node=execute", "attempt_id=att-b,route_node=execute"])
        self.assertEqual(dispatch._registry_diagnostics(str(self.jobs)), (None, 0))

    def test_a_direct_read_of_text_the_view_cannot_answer_keeps_the_text_path(self):
        self.jobs.write_bytes((self._row("att-a") + "x\ty\n").encode() + b"half a row")
        rows = dispatch.parsed_registry_rows(str(self.jobs))
        self.assertEqual([row[1][5] for row in rows], ["attempt_id=att-a,route_node=execute"])
        self.assertEqual(dispatch._registry_diagnostics(str(self.jobs))[1], 2)


class PinnedRegistryParityTest(unittest.TestCase):
    """A Fleet pass reads one registry generation without changing any reader's rows."""

    ROWS = (
        b"2026-10-20T00:00:00Z\topen\t/repo\t/wt\tslug-a\tattempt_id=att-a,route_id=rt-a,"
        b"route_node=plan,worker_type=stage,dispatch_depth=2\n"
        b"2026-10-20T00:00:01Z\tdone\t/repo\t/wt\tslug-a\tattempt_id=att-a,route_id=rt-a,"
        b"route_node=plan,note=x\x0cy\r"
        b"2026-10-20T00:00:02Z\topen\t/repo\t/wt\tslug-b\tattempt_id=att-b,route_id=rt-b,"
        b"route_node=execute,delivery_intent=1\r\n\n"
        + "2026-10-20T00:00:03Z\topen\t/repo\t/wt\tslug-c\tattempt_id=att-c,note=u v\x85w\n".encode()
        + b"2026-10-20T00:00:04Z\tdone\t/repo\t/wt\tslug-d\tattempt_id=att-d,note=\xff\x1dz"
    )

    def setUp(self):
        import dispatch_registry_cache
        self.cache = dispatch_registry_cache
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        self.jobs.write_bytes(self.ROWS)
        self.cache.clear_cache()
        self.addCleanup(self.cache.clear_cache)

    def readers(self):
        path = str(self.jobs)
        jobs, malformed = dispatch._scan_jobs_log(path, set())
        registry = dispatch._orphan_registry_module()
        return {
            "scan_jobs_log": ([(j.slug, j.status) for j in jobs], malformed),
            "jobs_log_fields": dispatch._jobs_log_fields([path]),
            "parent_extinction": dispatch._parent_extinction_rows([path]),
            "registry_evidence": dispatch._scan_registry_evidence([path]),
            "read_rows": registry.read_rows(self.jobs, lines=dispatch._pinned_registry_lines(path)),
        }

    def test_pinned_lines_keep_every_reader_result_for_unusual_separators(self):
        unpinned = self.readers()
        # An append-built cached generation segments only at LF; the pass must not reuse those lines.
        self.cache.registry_view(self.jobs, append_only=True)
        self.cache.reset_metrics()
        original = builtins.open
        def opened(path, *args, **kwargs):
            if str(path) == str(self.jobs):
                raise AssertionError("a reader reopened the pinned registry")
            return original(path, *args, **kwargs)
        with self.cache.registry_read_scope():
            self.assertEqual(dispatch._pinned_registry_lines(str(self.jobs)),
                             tuple(self.jobs.read_text(encoding="utf-8", errors="replace").splitlines()))
            with mock.patch.object(builtins, "open", side_effect=opened):
                pinned = self.readers()
        self.assertEqual(pinned, unpinned)
        self.assertEqual(self.cache.metrics_snapshot()["content_reads"], 1)

    def test_failed_pinned_read_leaves_each_reader_its_own_handling(self):
        missing = str(self.jobs.with_name("missing.log"))
        unpinned = (dispatch._scan_jobs_log(missing, set()), dispatch._jobs_log_fields([missing]),
                    dispatch._parent_extinction_rows([missing]))
        with self.cache.registry_read_scope():
            self.assertIsNone(dispatch._pinned_registry_lines(missing))
            pinned = (dispatch._scan_jobs_log(missing, set()), dispatch._jobs_log_fields([missing]),
                      dispatch._parent_extinction_rows([missing]))
        self.assertEqual(pinned, unpinned)

    def test_derived_indexes_are_reused_only_for_their_parsed_generation(self):
        path = str(self.jobs)
        first_rows = dispatch.parsed_registry_rows(path)
        first = dispatch._registry_derived_index(path, first_rows)
        self.assertIs(dispatch._registry_derived_index(path, first_rows), first)
        self.assertEqual(first["latest_by_attempt"]["att-a"][1][1], "done")
        self.assertEqual(len(first["all_by_attempt"]["att-a"]), 2)

        with self.jobs.open("ab") as stream:
            stream.write(b"\n2026-10-20T00:00:05Z\topen\t/repo\t/wt\tslug-a\t"
                         b"attempt_id=att-a,route_id=rt-a,route_node=execute\n")
        second_rows = dispatch.parsed_registry_rows(path)
        second = dispatch._registry_derived_index(path, second_rows)
        self.assertIsNot(second, first)
        self.assertEqual(second["latest_by_attempt"]["att-a"][1][1], "open")
        self.assertEqual(len(second["all_by_attempt"]["att-a"]), 3)
        self.assertEqual(first["latest_by_attempt"]["att-a"][1][1], "done")


if __name__ == "__main__":
    unittest.main()
