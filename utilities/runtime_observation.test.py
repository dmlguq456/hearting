#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from runtime_observation import (ObservationContext, file_generation,
                                stable_read_bytes)  # noqa: E402


class InputGenerationsTest(unittest.TestCase):
    def test_external_display_input_stays_fresh_outside_the_explicit_scope(self):
        from runtime_observation import InputGenerations, passive_input
        current, calls = ['first proof'], []
        def read():
            calls.append(1)
            return current[0]
        self.assertEqual(passive_input('git-proof', read), 'first proof')
        current[0] = 'next proof'
        self.assertEqual(passive_input('git-proof', read), 'next proof')
        self.assertEqual(len(calls), 2)
        observed = InputGenerations()
        with observed.capture():
            self.assertEqual(passive_input('git-proof', read), 'next proof')
            self.assertEqual(passive_input('git-proof', read), 'next proof')
        self.assertEqual(len(calls), 3)
        self.assertTrue(observed.current())
        current[0] = 'changed proof'
        self.assertFalse(observed.current())

    def test_dangling_symlink_retarget_and_lstat_are_observed(self):
        from runtime_observation import InputGenerations, input_path_call
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'link'
            path.symlink_to('missing-one')
            observed = InputGenerations()
            with observed.capture():
                input_path_call(path, 'lstat')
                self.assertTrue(input_path_call(path, 'is_symlink'))
            self.assertTrue(observed.current())
            path.unlink()
            path.symlink_to('missing-two')
            self.assertFalse(observed.current())

    def test_read_set_detects_same_size_rewrite_and_missing_file_creation(self):
        from runtime_observation import InputGenerations
        with tempfile.TemporaryDirectory() as root:
            present, missing = Path(root) / 'present', Path(root) / 'missing'
            present.write_text('old')
            observed = InputGenerations()
            with observed.capture():
                self.assertEqual(present.read_text(), 'old')
                with self.assertRaises(FileNotFoundError):
                    missing.read_text()
            self.assertTrue(observed.current())
            before = present.stat()
            present.write_text('new')
            os.utime(present, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertFalse(observed.current())
            observed = InputGenerations()
            with observed.capture():
                with self.assertRaises(FileNotFoundError):
                    missing.read_text()
            missing.write_text('created')
            self.assertFalse(observed.current())

    def test_directory_discovery_and_stat_only_cache_hit_are_dependencies(self):
        from runtime_observation import InputGenerations, input_stat
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'record'
            path.write_text('old')
            observed = InputGenerations()
            with observed.capture():
                self.assertEqual(os.listdir(root), ['record'])
                input_stat(path)  # the parser returned its cached document
            self.assertTrue(observed.current())
            path.write_text('changed')
            self.assertFalse(observed.current())
            observed = InputGenerations()
            with observed.capture():
                os.listdir(root)
            (Path(root) / 'new-record').write_text('new')
            self.assertFalse(observed.current())

    def test_sqlite_wal_change_and_read_set_limit_never_reuse_old_result(self):
        import sqlite3
        from runtime_observation import InputGenerations
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'state.sqlite'
            with sqlite3.connect(path) as db:
                db.execute('create table value (n)')
            observed = InputGenerations()
            with observed.capture(), sqlite3.connect(path) as db:
                db.execute('select * from value').fetchall()
            self.assertTrue(observed.current())
            Path(str(path) + '-wal').write_bytes(b'published')
            self.assertFalse(observed.current())
            observed = InputGenerations(max_bytes=1)
            with observed.capture():
                path.read_bytes()
            self.assertFalse(observed.current())
            self.assertEqual(observed.paths, {})

    def test_fresh_census_is_excluded_and_an_unclassified_rpc_prevents_reuse(self):
        import socket
        from runtime_observation import InputGenerations, untracked_inputs
        observed = InputGenerations()
        with observed.capture(), untracked_inputs():
            with open('/proc/self/stat') as stream:
                stream.read()
        self.assertTrue(observed.current())
        observed = InputGenerations()
        with observed.capture(), socket.socket(socket.AF_UNIX) as stream:
            with self.assertRaises(OSError):
                stream.connect('/nonexistent-hearting-test-socket')
        self.assertFalse(observed.current())


class PassObservationTest(unittest.TestCase):
    def test_a_changed_effective_uid_has_its_own_observation(self):
        from runtime_observation import observation_read_scope, observe_once
        from unittest import mock
        calls = []
        @observe_once
        def read():
            calls.append(1)
            return len(calls)
        with observation_read_scope(), mock.patch('runtime_observation.os.geteuid', side_effect=[1,2,2]):
            self.assertEqual(read(), 1)
            self.assertEqual(read(), 2)
            self.assertEqual(read(), 2)

    def test_pass_shares_reads_and_next_pass_observes_again(self):
        from runtime_observation import observation_read_scope, observe_once
        observed = []
        @observe_once
        def read(pid):
            observed.append(pid)
            return {'pid': pid, 'identity': len(observed)}
        with observation_read_scope():
            first = read(42)
            first['identity'] = 'caller mutation'
            with observation_read_scope():
                self.assertEqual(read(42)['identity'], 1)
        with observation_read_scope():
            self.assertEqual(read(42)['identity'], 2)
        self.assertEqual(read(42)['identity'], 3)
        self.assertEqual(read(42)['identity'], 4)

    def test_fault_is_not_reused_as_negative_evidence(self):
        from runtime_observation import observation_read_scope, observe_once
        calls = []
        @observe_once
        def read():
            calls.append(1)
            if len(calls) == 1:
                raise OSError('transient observation fault')
            return 'fresh identity'
        with observation_read_scope():
            with self.assertRaises(OSError):
                read()
            self.assertEqual(read(), 'fresh identity')
        self.assertEqual(len(calls), 2)

    def test_large_observations_do_not_remain_in_the_pass_cache(self):
        from runtime_observation import observation_read_scope, observe_once, _PASS_READS, _PASS_READ_BYTES
        @observe_once(immutable=True)
        def read(number):
            return str(number) + 'x' * (512 * 1024)
        with observation_read_scope():
            for number in range(30):
                read(number)
            entries, weight = _PASS_READS.get()
            self.assertLessEqual(weight, _PASS_READ_BYTES)
            self.assertLess(len(entries), 30)
        self.assertIsNone(_PASS_READS.get())


class ProjectionOwnershipTest(unittest.TestCase):
    def test_policy_mocks_do_not_fabricate_projection_storage(self):
        from runtime_observation import retained_size
        from unittest.mock import Mock
        value = Mock()
        self.assertEqual(retained_size(value), sys.getsizeof(value))
        self.assertEqual(value.mock_calls, [])

    def test_byte_limit_releases_old_graphs_and_rejects_oversized_graph(self):
        from runtime_observation import ProjectionCache
        import weakref, gc
        class Payload(dict):
            pass
        cache = ProjectionCache(max_entries=32, max_bytes=64 * 1024)
        refs = []
        for number in range(8):
            value = Payload(text=str(number) + 'x' * (32 * 1024))
            refs.append(weakref.ref(value))
            cache[number] = value
        del value
        gc.collect()
        self.assertLessEqual(cache.bytes, cache.max_bytes)
        self.assertLessEqual(sum(ref() is not None for ref in refs), 1)
        cache['oversized'] = {'text': 'x' * (128 * 1024)}
        self.assertNotIn('oversized', cache)
        cache.clear()
        self.assertEqual(cache.bytes, 0)
        self.assertFalse(cache.weights)

    def test_projection_admission_counters_expose_capacity_churn(self):
        from runtime_observation import ProjectionCache
        cache = ProjectionCache(max_entries=2, max_bytes=10)
        cache.remember('a', 1, weight=6)
        cache.remember('b', 2, weight=6)
        cache.remember('large', 3, weight=11)
        self.assertEqual((cache.admissions, cache.evictions, cache.rejections), (2, 1, 1))
        self.assertEqual(list(cache), ['b'])

    def test_replacement_and_delete_release_byte_accounting(self):
        from runtime_observation import ProjectionCache, retained_size
        cache = ProjectionCache(max_entries=2, max_bytes=1024)
        cache['one'] = {'x': 1}
        cache['one'] = {'x': 2}
        self.assertEqual(cache.bytes, retained_size(('one', {'x': 2})))
        del cache['one']
        self.assertEqual(cache.bytes, 0)


class ObservationContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "input.json"
        self.path.write_text('{"value": 1}', encoding="utf-8")
        self.context = ObservationContext(max_entries=2, max_weight=8)

    def test_reuses_projection_for_same_dependencies(self):
        loads = []
        loader = lambda: loads.append(1) or ("verified", 7)
        key = (str(self.path), "cycle-a")
        self.assertEqual(self.context.memoize(key, [self.path], loader), ("verified", 7))
        self.assertEqual(self.context.memoize(key, [self.path], loader), ("verified", 7))
        self.assertEqual(len(loads), 1)
        self.assertEqual(self.context.metrics_snapshot()["hits"], 1)

    def test_same_size_rewrite_with_restored_mtime_invalidates(self):
        key = "same-size"
        old = self.path.stat().st_mtime_ns
        first = file_generation(self.path)
        self.context.memoize(key, [self.path], lambda: "old")
        self.path.write_text('{"value": 2}', encoding="utf-8")
        os.utime(self.path, ns=(old, old))
        self.assertNotEqual(first, file_generation(self.path))
        self.assertEqual(self.context.memoize(key, [self.path], lambda: "new"), "new")

    def test_missing_dependency_does_not_reuse_old_projection(self):
        key = "missing"
        self.context.memoize(key, [self.path], lambda: "old")
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.context.memoize(key, [self.path], lambda: "incorrect")
        self.assertEqual(self.context.metrics_snapshot()["entries"], 0)

    def test_lru_and_weight_are_bounded(self):
        for number in range(4):
            self.context.memoize(number, [self.path], lambda number=number: number,
                                 weight=3)
        metrics = self.context.metrics_snapshot()
        self.assertLessEqual(metrics["entries"], 2)
        self.assertLessEqual(metrics["weight"], 8)
        self.assertGreater(metrics["evictions"], 0)

    def test_loader_change_does_not_publish_under_old_generation(self):
        calls = []

        def loader():
            calls.append(1)
            if len(calls) == 1:
                self.path.write_text('{"value": 3}', encoding="utf-8")
            return len(calls)

        with self.assertRaisesRegex(OSError, "dependency-changed"):
            self.context.memoize("racing", [self.path], loader)
        self.assertEqual(self.context.memoize("racing", [self.path], loader), 2)

    def test_stable_reader_returns_exact_bytes_and_observer_state_survives_checkpoint(self):
        self.assertEqual(stable_read_bytes(self.path), b'{"value": 1}')
        progress = self.context.state("join-checkpoint", list)
        progress.append("settlement-backoff")
        self.assertIs(progress, self.context.state("join-checkpoint", list))
        self.assertEqual(self.context.metrics_snapshot()["state_hits"], 1)


if __name__ == "__main__":
    unittest.main()
