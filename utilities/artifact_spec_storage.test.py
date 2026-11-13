"""Approved D reader contract; real current implementation, private trees."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import json
import importlib.util
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_producer as P
import artifact_spec_storage as storage


class LogicalRevisionReaderTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='spec-d-reader-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / '.agent_reports'
        self.ref = 'ref_' + 'a' * 32
        self.old, self.new = 'rrev_' + '1' * 32, 'rrev_' + '2' * 32
        self.base = self.root / 'shared/spec' / self.ref
        self.previous = {'topic/prd.md': b'# Earlier exact PRD\n',
                         'topic/_internal/versions/v1/prd.md': b'# Older exact PRD\n'}
        self.revision(self.old, self.previous, self.previous, sequence=1)

    def revision(self, rid, physical, logical, **extra):
        directory = self.base / 'revisions' / rid
        directory.mkdir(parents=True)
        for name, raw in physical.items():
            target = directory / name; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        rows = P._spec_inventory(logical)
        record = {'schema_version': 1, 'shared_reference_id': self.ref,
            'shared_reference_revision_id': rid, 'kind': 'shared-spec',
            'files': rows, 'content_digest': P._spec_inventory_digest(rows), **extra}
        (directory / 'revision.json').write_text(json.dumps(record))
        (self.base / 'reference.json').write_text(json.dumps({'shared_reference_id': self.ref,
            'latest_revision_id': rid, 'revisions': [self.old, self.new]}))
        return directory

    def read(self):
        return P._verified_shared_spec(self.root, self.ref, self.new)[0]

    def assert_complete(self, expected):
        try:
            result = self.read()
        except P.ProducerError as error:
            self.fail('logical revision must remain readable: ' + str(error))
        self.assertEqual(result, expected)

    def test_v1_stored_in_reconstructs_the_full_inventory(self):
        logical = {**self.previous, 'topic/prd.md': b'# Current PRD\n'}
        self.revision(self.new, {'topic/prd.md': logical['topic/prd.md']}, logical, sequence=2,
            stored_in={'topic/_internal/versions/v1/prd.md': self.old})
        self.assert_complete(logical)

    def test_versioned_source_path_reuses_exact_previous_prd_for_its_new_preimage_slot(self):
        history = 'topic/_internal/versions/v2/prd.md'
        logical = {'topic/prd.md': b'# Current PRD\n', history: self.previous['topic/prd.md']}
        self.revision(self.new, {'topic/prd.md': logical['topic/prd.md']}, logical,
            schema_version=2, sequence=2, stored_in={history: {'revision_id': self.old, 'source_path': 'topic/prd.md'}})
        self.assert_complete(logical)

    def test_ordinary_full_revisions_remain_compatible(self):
        self.revision(self.new, self.previous, self.previous, sequence=2)
        self.assert_complete(self.previous)

    def test_tampered_physical_source_never_returns_an_empty_success(self):
        logical = dict(self.previous)
        self.revision(self.new, {}, logical, sequence=2,
            stored_in={name: self.old for name in logical})
        (self.base / 'revisions' / self.old / 'topic/prd.md').write_bytes(b'changed')
        with self.assertRaises(P.ProducerError):self.read()

    def test_storage_reference_cannot_escape_to_another_reference(self):
        self.revision(self.new, {}, self.previous, sequence=2,
            stored_in={'topic/prd.md': '../another-reference/revisions/' + self.old})
        with self.assertRaises(P.ProducerError):self.read()

    def test_symlink_source_is_not_a_regular_storage_file(self):
        source = self.base / 'revisions' / self.old / 'topic/prd.md'
        outside = self.root.parent / 'outside-prd'; outside.write_bytes(source.read_bytes())
        source.unlink(); source.symlink_to(outside)
        self.revision(self.new, {}, self.previous, sequence=2,
            stored_in={name: self.old for name in self.previous})
        with self.assertRaises(P.ProducerError):self.read()

    def test_extra_physical_bytes_not_in_the_inventory_are_rejected(self):
        self.revision(self.new, {**self.previous, 'extra': b'undeclared'}, self.previous, sequence=2)
        with self.assertRaises(P.ProducerError):self.read()

    def test_storage_rows_not_in_the_logical_inventory_are_rejected(self):
        self.revision(self.new, self.previous, self.previous, sequence=2,
            stored_in={'undeclared/prd.md': self.old})
        with self.assertRaises(P.ProducerError):self.read()

    def test_one_logical_file_cannot_have_two_storage_declarations(self):
        self.revision(self.new, self.previous, self.previous, sequence=2,
            stored_in={'topic/prd.md': self.old})
        with self.assertRaises(P.ProducerError):self.read()

    def test_storage_metadata_is_not_silently_ignored_when_its_shape_is_wrong(self):
        self.revision(self.new, self.previous, self.previous, sequence=2, stored_in=['not-a-mapping'])
        with self.assertRaises(P.ProducerError):self.read()

    def test_versioned_source_path_cannot_contain_parent_traversal(self):
        self.revision(self.new, self.previous, self.previous, schema_version=2, sequence=2,
            stored_in={'topic/prd.md': {'revision_id': self.old, 'source_path': '../outside-prd'}})
        with self.assertRaises(P.ProducerError):self.read()

    def test_a_later_revision_cannot_supply_an_earlier_logical_revision(self):
        future = 'rrev_' + '3' * 32
        self.revision(future, self.previous, self.previous, sequence=3)
        self.revision(self.new, {}, self.previous, sequence=2,
            stored_in={name: future for name in self.previous})
        (self.base / 'reference.json').write_text(json.dumps({'shared_reference_id': self.ref,
            'latest_revision_id': future, 'revisions': [self.old, self.new, future]}))
        with self.assertRaises(P.ProducerError):self.read()

    def test_file_lookup_keeps_the_logical_revision_and_actual_provider_distinct(self):
        history = 'topic/_internal/versions/v2/prd.md'
        logical = {'topic/prd.md': b'# Current\n', history: self.previous['topic/prd.md']}
        self.revision(self.new, {'topic/prd.md': logical['topic/prd.md']}, logical,
            schema_version=2, sequence=2, stored_in={history: {'revision_id': self.old, 'source_path': 'topic/prd.md'}})
        result = storage.read_file(self.root, self.ref, self.new, history)
        self.assertEqual(result.data, self.previous['topic/prd.md'])
        self.assertEqual((result.reference_id, result.revision_id, result.logical_path), (self.ref, self.new, history))
        self.assertEqual((result.source_revision_id, result.source_path), (self.old, 'topic/prd.md'))
        self.assertEqual(result.path, self.base / 'revisions' / self.old / 'topic/prd.md')

    def test_verification_before_the_existing_pointer_commit_needs_no_override_flag(self):
        logical = {**self.previous, 'topic/prd.md': b'# Next\n'}
        self.revision(self.new, {'topic/prd.md': logical['topic/prd.md']}, logical, sequence=2,
            stored_in={'topic/_internal/versions/v1/prd.md': self.old})
        (self.base / 'reference.json').write_text(json.dumps({'shared_reference_id': self.ref,
            'latest_revision_id': self.old, 'revisions': [self.old]}))
        self.assert_complete(logical)
        self.assertEqual(json.loads((self.base / 'reference.json').read_text())['latest_revision_id'], self.old)

    def test_reading_referenced_files_creates_no_view_or_new_state(self):
        logical = {**self.previous, 'topic/prd.md': b'# Next\n'}
        self.revision(self.new, {'topic/prd.md': logical['topic/prd.md']}, logical, sequence=2,
            stored_in={'topic/_internal/versions/v1/prd.md': self.old})
        def snapshot():
            return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        before = snapshot()
        self.assert_complete(logical)
        storage.read_file(self.root, self.ref, self.new, 'topic/_internal/versions/v1/prd.md')
        self.assertEqual(snapshot(), before)
        self.assertFalse((self.base / 'current').exists())

    def test_old_full_revisions_can_contain_empty_directories(self):
        directory = self.revision(self.new, self.previous, self.previous, sequence=2)
        (directory / 'empty/subdir').mkdir(parents=True)
        self.assert_complete(self.previous)

    def test_payload_directory_symlinks_cannot_be_used_as_physical_sources(self):
        source = self.base / 'revisions' / self.old / 'topic'
        outside = self.root.parent / 'outside-topic'
        source.rename(outside); source.symlink_to(outside, target_is_directory=True)
        self.revision(self.new, {}, self.previous, sequence=2,
            stored_in={name: self.old for name in self.previous})
        with self.assertRaises(P.ProducerError):self.read()

    def indirect(self):
        history = 'topic/_internal/versions/v20/prd.md'
        logical = {**self.previous, history: self.previous['topic/prd.md']}
        self.revision(self.new, {}, logical, schema_version=2, sequence=2,
            stored_in={name: {'revision_id': self.old,
                'source_path': 'topic/prd.md' if name == history else name} for name in logical})
        return logical

    def test_existing_resolve_surface_opens_exact_logical_file_and_retains_both_identities(self):
        import artifact_reader
        self.indirect()
        rel = f'shared/spec/{self.ref}/revisions/{self.new}/topic/_internal/versions/v20/prd.md'
        result = artifact_reader.resolve_path(self.root, rel)
        self.assertEqual(result.get('resolution'), 'stored')
        self.assertEqual(result['absolute'], str(self.base / 'revisions' / self.old / 'topic/prd.md'))
        self.assertEqual((result['revision_id'], result['source_revision_id']), (self.new, self.old))

    def test_governing_prd_candidates_keep_the_actual_immutable_read_path(self):
        import artifact_cutover
        self.indirect()
        self.assertEqual(artifact_cutover.prd_candidates(self.root),
            [str(self.base / 'revisions' / self.old / 'topic/prd.md')])

    def transaction(self):
        spec = importlib.util.spec_from_file_location('d_transaction', Path(__file__).with_name('spec-transaction.py'))
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def test_component_seed_materializes_logical_history_and_exact_preimage(self):
        expected = self.indirect()
        spec = self.root.parent / 'authoring'; spec.mkdir()
        reference = json.loads((self.base / 'reference.json').read_text())
        self.transaction().seed_component_spec(spec, self.root, reference, None, ('topic',))
        actual = {name: (spec / name).read_bytes() for name in expected if (spec / name).is_file()}
        self.assertEqual(actual, expected)

    def test_counter_sees_logical_history_slots_without_a_physical_directory(self):
        self.indirect()
        self.assertEqual(self.transaction().versions_max(self.base / 'revisions' / self.new / 'topic'), 20)

    def test_whole_tree_seed_reconstructs_all_logical_files(self):
        expected = self.indirect()
        target = self.root.parent / 'authoring'; target.mkdir()
        self.transaction().seed_cycle_spec(target, self.root)
        self.assertEqual({name: (target / name).read_bytes() for name in expected if (target / name).is_file()}, expected)

    def test_latest_file_uses_shared_storage_and_corruption_does_not_fall_back_to_legacy(self):
        import artifact_reader
        self.indirect()
        legacy = self.root / 'spec/topic/prd.md'; legacy.parent.mkdir(parents=True); legacy.write_bytes(b'stale')
        self.assertEqual(artifact_reader.spec_file(self.root, 'topic/prd.md').read_bytes(), self.previous['topic/prd.md'])
        (self.base / 'revisions' / self.old / 'topic/prd.md').write_bytes(b'broken')
        with self.assertRaises(storage.StorageError):artifact_reader.spec_file(self.root, 'topic/prd.md')
        rel = f'shared/spec/{self.ref}/revisions/{self.new}/topic/prd.md'
        self.assertEqual(artifact_reader.resolve_path(self.root, rel)['resolution'], 'unknown')

    def test_handoff_snapshot_observes_the_logical_file_and_direct_provider_metadata(self):
        self.indirect()
        spec = importlib.util.spec_from_file_location('d_handoff', Path(__file__).resolve().parents[1] / 'tools/artifact-w8-handoff.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        snapshot = module.LocatorSnapshot(self.root)
        name = f'shared/spec/{self.ref}/revisions/{self.new}/topic/_internal/versions/v20/prd.md'
        raw = self.previous['topic/prd.md']
        identity = snapshot.observe_target(name, P._digest(raw), len(raw))
        snapshot.population_targets.append((name, P._digest(raw), len(raw), identity))
        snapshot.verify_unchanged()
        (self.base / 'revisions' / self.new / 'revision.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'snapshot-drift'):snapshot.verify_unchanged()

    def test_schema_types_are_checked_and_legacy_inventory_order_is_preserved(self):
        for schema in (True, 1.0):
            with self.subTest(schema=schema):
                directory = self.base / 'revisions' / self.new
                if directory.exists():
                    import shutil
                    shutil.rmtree(directory)
                self.revision(self.new, self.previous, self.previous, schema_version=schema, sequence=2)
                with self.assertRaises(P.ProducerError):self.read()
        metadata = self.base / 'revisions' / self.new / 'revision.json'
        record = json.loads(metadata.read_text()); record['schema_version'] = 1
        record['files'].reverse(); record['content_digest'] = P._spec_inventory_digest(record['files'])
        metadata.write_text(json.dumps(record))
        self.assert_complete(self.previous)

    def test_directory_cli_carries_all_logical_file_sources_without_materializing(self):
        import artifact_reader
        import contextlib
        import io
        expected = self.indirect()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = artifact_reader.main(['spec-dir', '--artifact-root', str(self.root)])
        result = json.loads(out.getvalue())
        self.assertEqual(code, 0); self.assertFalse(result['physical_complete'])
        self.assertEqual(result['revision_id'], self.new)
        self.assertEqual({row['logical_path']: Path(row['path']).read_bytes()
            for row in result['logical_files']}, expected)
        self.assertFalse((self.base / 'current').exists())
        self.assertEqual([path.name for path, _ in artifact_reader.iter_bucket_children(self.root, 'spec')], ['topic'])

    def test_legacy_map_targets_use_logical_storage_without_changing_the_map(self):
        from unittest import mock
        import artifact_cutover
        self.indirect()
        rel = f'shared/spec/{self.ref}/revisions/{self.new}/topic/prd.md'
        table = {'spec/topic/prd.md': rel}
        with mock.patch.object(artifact_cutover, '_load_maps', return_value=[('existing-map', table)]):
            result = artifact_cutover.resolve_legacy(self.root, 'spec/topic/prd.md')
        self.assertEqual(result['resolution'], 'mapped')
        self.assertEqual((result['revision_id'], result['source_revision_id']), (self.new, self.old))
        self.assertEqual(Path(result['absolute']).read_bytes(), self.previous['topic/prd.md'])
        self.assertEqual(table, {'spec/topic/prd.md': rel})

    def test_canonical_topic_rows_keep_unknown_axes_and_exact_logical_identity(self):
        import artifact_entrance
        self.indirect()
        identity = self.root / '.runtime/artifact-admission/v1/root-identity.json'
        identity.parent.mkdir(parents=True); identity.write_text('{}')
        source = storage.entrance_snapshot(self.root)
        self.assertEqual(source['coverage'], 'complete')
        self.assertEqual(len(source['rows']), 1)
        row = source['rows'][0]
        self.assertEqual((row['reference_id'], row['component'], row['revision_id'], row['source_revision_id']),
            (self.ref, 'topic', self.new, self.old))
        self.assertTrue(all(row[key] is None for key in ('artifact_root_id', 'campaign_id', 'cycle_id', 'artifact_id')))
        self.assertEqual(row['status'], 'recorded')
        self.assertEqual(artifact_entrance._source_row(self.root, row)['revision_id'], self.new)
        (self.base / 'revisions' / self.old / 'topic/prd.md').write_bytes(b'corrupt')
        damaged = storage.entrance_snapshot(self.root)
        self.assertEqual(damaged['coverage'], 'unknown'); self.assertTrue(damaged['errors'])

    def test_fleet_and_memory_read_the_same_stored_pipeline_state(self):
        import subprocess
        state = b'phases:\n  dev: in_progress\n'
        for name in ('pipeline_state.yaml', 'topic/pipeline_state.yaml'):
            self.previous[name] = state
            path = self.base / 'revisions' / self.old / name
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(state)
        metadata = self.base / 'revisions' / self.old / 'revision.json'
        record = json.loads(metadata.read_text())
        record['files'] = P._spec_inventory(self.previous)
        record['content_digest'] = P._spec_inventory_digest(record['files'])
        metadata.write_text(json.dumps(record))
        self.indirect()
        source = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(source / 'tools'))
        from fleet import projection
        self.assertEqual(projection._spec_stage_parts(str(self.root.parent), 'topic'), ('topic', 'dev'))
        result = subprocess.run([sys.executable, str(source / 'tools/memory/mem.py'), 'curate-artifacts'],
            cwd=self.root.parent, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SPEC phases:', result.stdout)
        self.assertIn('dev: in_progress', result.stdout)
        # Consumers use the already observed bytes, never reopen the address
        # after it has been returned by the common reader.
        from unittest import mock
        read = projection._artifact_reader().read_spec_file
        outside = self.root.parent / 'outside-state'; outside.write_bytes(b'phases:\n  outside: in_progress\n')
        def swap_after_read(*args, **kwargs):
            observed = read(*args, **kwargs)
            observed.path.unlink(); observed.path.symlink_to(outside)
            return observed
        with mock.patch.object(projection._artifact_reader(), 'read_spec_file', side_effect=swap_after_read):
            self.assertEqual(projection._spec_stage_parts(str(self.root.parent), 'topic'), ('topic', 'dev'))

    def test_plain_legacy_spec_does_not_follow_a_directory_link(self):
        import artifact_reader
        root = self.root.parent / 'plain-root'; root.mkdir()
        outside = self.root.parent / 'outside-spec'; outside.mkdir()
        (outside / 'pipeline_state.yaml').write_bytes(b'outside')
        (root / 'spec').symlink_to(outside, target_is_directory=True)
        self.assertIsNone(artifact_reader.read_spec_file(root, 'pipeline_state.yaml'))

    def test_seed_does_not_reread_obsolete_prd_bytes_when_latest_has_exact_history(self):
        expected = dict(self.previous)
        self.revision(self.new, expected, expected, sequence=2)
        (self.base / 'revisions' / self.old / 'topic/prd.md').write_bytes(b'obsolete bytes changed')
        target = self.root.parent / 'authoring'; target.mkdir()
        reference = json.loads((self.base / 'reference.json').read_text())
        try:
            self.transaction().seed_component_spec(target, self.root, reference, None, ('topic',))
        except P.ProducerError as error:
            self.fail('history carry must read only missing history, not obsolete current PRDs: ' + str(error))
        self.assertEqual({name: (target / name).read_bytes() for name in expected}, expected)

    def test_counter_counts_recorded_slots_without_reading_irrelevant_payload_bytes(self):
        (self.base / 'revisions' / self.old / 'topic/prd.md').write_bytes(b'obsolete bytes changed')
        try:
            result = self.transaction().versions_max(self.base / 'revisions' / self.old / 'topic')
        except Exception as error:
            self.fail('history numbering enumerates slots; it does not read unrelated PRD bytes: ' + str(error))
        self.assertEqual(result, 1)

    def test_open_topic_search_keeps_unknown_cycle_and_uses_the_logical_viewer_address(self):
        import contextlib
        import io
        from unittest import mock
        import artifact_entrance as entrance
        self.indirect()
        row = entrance._source_row(self.root, storage.entrance_snapshot(self.root)['rows'][0])
        with mock.patch.object(entrance, '_refresh_and_snapshot', return_value=({}, {'recent': [row]})), \
                mock.patch.object(entrance, '_open_externally') as external, \
                mock.patch.object(entrance, '_show_viewer', return_value=None) as viewer, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(entrance._command_open(self.root, ['topic']), 0)
        self.assertEqual(viewer.call_args.args[1], self.root / row['logical_locator'])
        external.assert_called_once()


if __name__ == '__main__':unittest.main()
