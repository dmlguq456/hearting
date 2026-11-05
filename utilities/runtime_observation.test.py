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


class VerificationRegistryTest(unittest.TestCase):
    def test_passive_catalog_reuses_static_parse_and_observes_changed_or_unavailable_file(self):
        from unittest import mock
        from types import SimpleNamespace
        import json
        import capability_route_verify as verify
        from runtime_observation import observation_read_scope
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.json'
            path.write_text('{"name":"one"}')
            load = mock.Mock(side_effect=lambda: json.loads(path.read_text()))
            digest = mock.Mock(side_effect=lambda doc: json.dumps(doc, sort_keys=True))
            topo = SimpleNamespace(REGISTRY=path, load_registry=load, registry_digest=digest)
            with mock.patch.object(verify, 'TOPO', topo):
                verify._VERIFICATION_REGISTRIES = None
                for _ in range(2):
                    with observation_read_scope():
                        self.assertEqual(verify._verification_registry(), ({'name': 'one'}, '{"name": "one"}'))
                self.assertEqual(load.call_count, 1)
                self.assertEqual(digest.call_count, 1)
                stamp = path.stat()
                path.write_text('{"name":"two"}')
                os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                with observation_read_scope():
                    self.assertEqual(verify._verification_registry()[0], {'name': 'two'})
                self.assertEqual(load.call_count, 2)
                # Active verifiers do not borrow the passive catalog.
                verify._verification_registry()
                self.assertEqual(load.call_count, 3)
                path.chmod(0)
                with observation_read_scope(), self.assertRaises(PermissionError):
                    verify._verification_registry()
                path.chmod(0o600)
                with observation_read_scope():
                    self.assertEqual(verify._verification_registry()[0], {'name': 'two'})
                path.write_text('{malformed')
                with observation_read_scope(), self.assertRaises(ValueError):
                    verify._verification_registry()
                path.unlink()
                with observation_read_scope(), self.assertRaises(FileNotFoundError):
                    verify._verification_registry()

    def test_passive_catalog_preserves_real_route_verification_and_owned_inputs(self):
        import copy
        import json
        import capability_route_verify as verify
        import owner_route_binding as binding
        from runtime_observation import observation_read_scope
        route_api = binding.ROUTE
        with tempfile.TemporaryDirectory() as directory:
            route = route_api.compile_route(
                capability='autopilot-code', capability_mode='dev', requested_intensity='direct',
                cwd=route_api.ROOT, artifact_root=Path(directory),
                predicates=['atomic-outcome', 'known-scope', 'no-shared-contract',
                            'no-resource-run', 'no-artifact-handoff', 'no-independent-verifier',
                            'focused-verification'],
                transport=None, inline_reason='atomic-direct', tracking='tracked',
                tracked_gate_evidence=dict(spec_read=dict(satisfied=True, source='canonical-prd-sha256'),
                    drift_verdict='within-spec', workflow_mode='tracked',
                    artifact_guard=dict(satisfied=True, source='conductor-prechecked')))
            expected = route_api.verify_route(copy.deepcopy(route), route_api.ROOT)
            verify._VERIFICATION_REGISTRIES = None
            for _ in range(2):
                with observation_read_scope():
                    self.assertEqual(route_api.verify_route(copy.deepcopy(route), route_api.ROOT), expected)
            cache = verify._VERIFICATION_REGISTRIES
            self.assertEqual(len(cache), 1)
            before = json.dumps(next(iter(cache.values()))[1], sort_keys=True)
            invalid = copy.deepcopy(route)
            invalid['conditional_extensions'] = []
            invalid['route_hash'] = route_api.route_hash(invalid)
            invalid['route_id'] = 'rt-' + invalid['route_hash'].split(':', 1)[1][:16]
            with observation_read_scope(), self.assertRaises(ValueError):
                route_api.verify_route(invalid, route_api.ROOT)
            self.assertEqual(json.dumps(next(iter(cache.values()))[1], sort_keys=True), before)
            self.assertEqual(route_api.verify_route(copy.deepcopy(route), route_api.ROOT), expected)

    def test_racing_or_oversized_catalog_is_returned_but_not_retained(self):
        from unittest import mock
        from types import SimpleNamespace
        import json
        import capability_route_verify as verify
        from runtime_observation import observation_read_scope, ProjectionCache
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.json'
            path.write_text('{"name":"one"}')
            def racing():
                doc = json.loads(path.read_text())
                path.write_text('{"name":"two"}')
                return doc
            topo = SimpleNamespace(REGISTRY=path, load_registry=racing, registry_digest=json.dumps)
            with mock.patch.object(verify, 'TOPO', topo):
                verify._VERIFICATION_REGISTRIES = ProjectionCache(max_entries=4, max_bytes=1)
                with observation_read_scope():
                    self.assertEqual(verify._verification_registry()[0], {'name': 'one'})
                self.assertEqual(len(verify._VERIFICATION_REGISTRIES), 0)
                topo.load_registry = lambda: json.loads(path.read_text())
                for _ in range(2):
                    with observation_read_scope():
                        self.assertEqual(verify._verification_registry()[0], {'name': 'two'})
                self.assertEqual(verify._VERIFICATION_REGISTRIES.rejections, 2)


class UnitCatalogProjectionTest(unittest.TestCase):
    def test_passive_catalog_reuses_unchanged_files_and_only_parses_changed_unit(self):
        import capability_route_nodes as nodes
        from runtime_observation import observation_read_scope
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'dev').mkdir()
            first, second = root / 'dev/a.md', root / 'dev/b.md'
            first.write_text('---\nname: a\n---\nbody one\n')
            second.write_text('---\nname: b\n---\nbody two\n')
            nodes._UNIT_FRONTMATTER_CACHE = nodes._UNIT_DIGEST_CACHE = None
            with mock.patch.object(nodes, '_capture_frontmatter_file', wraps=nodes._capture_frontmatter_file) as read:
                with observation_read_scope():
                    original = nodes.unit_catalog_digest(root)
                    self.assertEqual(nodes.unit_catalog_digest(root), original)
                self.assertEqual(read.call_count, 2)
                with observation_read_scope():
                    self.assertEqual(nodes.unit_catalog_digest(root), original)
                self.assertEqual(read.call_count, 2)
                first.write_text('---\nname: changed\n---\nbody one\n')
                with observation_read_scope():
                    changed = nodes.unit_catalog_digest(root)
                self.assertNotEqual(changed, original)
                self.assertEqual(read.call_count, 3)
                # Unit body prose stays outside the machine-contract digest.
                second.write_text('---\nname: b\n---\nnew body prose\n')
                with observation_read_scope():
                    self.assertEqual(nodes.unit_catalog_digest(root), changed)
                self.assertEqual(read.call_count, 4)
                self.assertEqual(nodes.unit_catalog_digest(root), changed)
                self.assertEqual(read.call_count, 6)  # Active callers remain fresh.

    def test_passive_catalog_observes_added_removed_and_unreadable_units(self):
        import capability_route_nodes as nodes
        from runtime_observation import observation_read_scope
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'dev').mkdir()
            a = root / 'dev/a.md'
            a.write_text('---\nname: a\n---\n')
            nodes._UNIT_FRONTMATTER_CACHE = nodes._UNIT_DIGEST_CACHE = None
            with observation_read_scope():
                before = nodes.unit_catalog_digest(root)
            b = root / 'dev/b.md'
            b.write_text('---\nname: b\n---\n')
            with observation_read_scope():
                after = nodes.unit_catalog_digest(root)
            self.assertNotEqual(before, after)
            b.unlink()
            with observation_read_scope():
                self.assertEqual(nodes.unit_catalog_digest(root), before)
            with mock.patch.object(Path, 'read_text', side_effect=PermissionError('unreadable')):
                with observation_read_scope():
                    with self.assertRaises(PermissionError):
                        nodes.unit_catalog_digest(root)


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
