#!/usr/bin/env python3

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

from pathlib import Path
import os
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from runtime_observation import (ObservationContext, file_generation,
                                stable_read_bytes)  # noqa: E402


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
