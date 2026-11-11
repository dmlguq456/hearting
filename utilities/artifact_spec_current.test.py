"""Normal derived projection, interruption/replay and read-only fallback."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_reader as reader
import artifact_spec_current as current
import artifact_spec_storage as storage

spec = importlib.util.spec_from_file_location('current_fixture', Path(__file__).with_name('artifact_spec_storage.test.py'))
fixture_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture_module)


class CurrentViewTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture_module.LogicalRevisionReaderTest()
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.view = self.f.base / 'current'
        self.pointer(self.f.old)

    def pointer(self, revision):
        (self.f.base / 'reference.json').write_text(json.dumps({'shared_reference_id': self.f.ref,
            'latest_revision_id': revision, 'revisions': [self.f.old, self.f.new]}))

    def refresh(self, revision=None):
        return current.refresh(self.f.root, self.f.ref, revision or self.f.new)

    def next_revision(self):
        logical = {**self.f.previous, 'topic/prd.md': b'# Changed\n'}
        self.f.revision(self.f.new, {'topic/prd.md': logical['topic/prd.md']}, logical,
            schema_version=2, sequence=2, stored_in={'topic/_internal/versions/v1/prd.md':
                {'revision_id': self.f.old, 'source_path': 'topic/_internal/versions/v1/prd.md'}})
        return logical

    def test_unchanged_files_keep_their_inode_and_old_replay_cannot_rewind(self):
        self.assertEqual(self.refresh(self.f.old)['file_count'], 2)
        history = self.view / 'topic/_internal/versions/v1/prd.md'; before = history.stat().st_ino
        self.next_revision()
        report = self.refresh()
        self.assertEqual((report['state'], report['file_count'], report['deleted_files']), ('updated', 1, 0))
        self.assertEqual(history.stat().st_ino, before)
        marker = (self.view / 'revision.json').read_bytes()
        self.assertEqual(self.refresh(self.f.old)['reason'], 'newer-latest')
        self.assertEqual((self.view / 'revision.json').read_bytes(), marker)

    def test_interrupted_projection_uses_immutable_bytes_and_next_writer_resumes(self):
        self.refresh(self.f.old); expected = self.next_revision()
        real_put = current._put
        def interrupt_marker(fd, parts, raw):
            if parts == ['revision.json']: raise OSError('synthetic interrupted marker')
            return real_put(fd, parts, raw)
        with mock.patch.object(current, '_put', side_effect=interrupt_marker):
            self.assertEqual(self.refresh()['state'], 'unknown')
        self.assertIsNone(current.read_file(self.f.root, self.f.ref, self.f.new, 'topic/prd.md'))
        before = {p: p.read_bytes() for p in self.view.rglob('*') if p.is_file()}
        resolved = reader.resolve_path(self.f.root, f'shared/spec/{self.f.ref}/current/topic/prd.md')
        self.assertEqual(Path(resolved['absolute']).read_bytes(), expected['topic/prd.md'])
        self.assertFalse(resolved['view'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.view.rglob('*') if p.is_file()})
        self.assertEqual(self.refresh()['state'], 'updated')
        self.assertEqual(current.read_file(self.f.root, self.f.ref, self.f.new, 'topic/prd.md').data, expected['topic/prd.md'])

    def test_first_projection_interruption_reuses_exact_partial_files(self):
        real_put = current._put
        def interrupt_marker(fd, parts, raw):
            if parts == ['revision.json']: raise OSError('synthetic first interruption')
            return real_put(fd, parts, raw)
        with mock.patch.object(current, '_put', side_effect=interrupt_marker):
            self.assertEqual(self.refresh(self.f.old)['state'], 'unknown')
        self.assertFalse((self.view / 'revision.json').exists())
        self.assertEqual(self.refresh(self.f.old)['state'], 'updated')

    def test_unknown_and_symlink_entries_are_preserved_and_never_followed(self):
        self.refresh(self.f.old); self.next_revision()
        foreign = self.view / 'foreign'; foreign.write_bytes(b'keep this')
        self.assertEqual(self.refresh()['state'], 'unknown')
        self.assertEqual(foreign.read_bytes(), b'keep this'); foreign.unlink()
        target = self.view / 'topic/prd.md'; target.unlink()
        outside = self.f.root.parent / 'outside'; outside.write_bytes(b'outside')
        target.symlink_to(outside)
        self.assertEqual(self.refresh()['state'], 'unknown')
        self.assertIsNone(current.read_file(self.f.root, self.f.ref, self.f.new, 'topic/prd.md'))
        self.assertEqual(outside.read_bytes(), b'outside')

    def test_deleted_files_leave_the_view_and_immutable_history_stays_exact(self):
        self.refresh(self.f.old)
        logical = {'topic/prd.md': b'# Changed\n'}
        self.f.revision(self.f.new, logical, logical, sequence=2)
        self.assertEqual(self.refresh()['deleted_files'], 1)
        self.assertFalse((self.view / 'topic/_internal/versions/v1/prd.md').exists())
        self.assertEqual(storage.read_revision(self.f.root, self.f.ref, self.f.old)[0], self.f.previous)

    def test_missing_and_drifting_view_reads_do_not_repair(self):
        self.next_revision()
        logical = f'shared/spec/{self.f.ref}/current/topic/prd.md'
        result = reader.resolve_path(self.f.root, logical)
        self.assertFalse(result['view']); self.assertFalse(self.view.exists())
        self.refresh(); (self.view / 'topic/prd.md').write_bytes(b'drift')
        result = reader.resolve_path(self.f.root, logical)
        self.assertFalse(result['view']); self.assertEqual((self.view / 'topic/prd.md').read_bytes(), b'drift')

    def test_orphan_atomic_temporaries_are_ignored_without_deletion(self):
        self.refresh(self.f.old); self.next_revision()
        temporary = self.view / ('.current-' + 'a' * 24); temporary.write_bytes(b'partial atomic bytes')
        self.assertEqual(self.refresh()['state'], 'updated')
        self.assertEqual(temporary.read_bytes(), b'partial atomic bytes')

    def test_viewer_opens_logical_files_directories_and_current_drift_without_writes(self):
        import artifact_viewer as viewer
        import http.client
        import threading
        self.refresh(self.f.old); expected = self.next_revision(); self.refresh()
        (self.view / 'topic/prd.md').write_bytes(b'drift')
        server = viewer.ViewerServer(self.f.root, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def stop():
            server.shutdown(); thread.join(5); server.server_close()
        self.addCleanup(stop)
        port = server.server_address[1]
        def get(path, method='GET', headers=None):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            try:
                connection.request(method, path, headers=headers or {})
                response = connection.getresponse()
                return response.status, response.read()
            finally: connection.close()
        before = {p: p.read_bytes() for p in self.f.root.rglob('*') if p.is_file()}
        prefix = f'/shared/spec/{self.f.ref}'
        for base in (f'{prefix}/revisions/{self.f.new}', f'{prefix}/current'):
            self.assertEqual(get(base + '/topic/prd.md?raw=1'), (200, expected['topic/prd.md']))
            self.assertEqual(get(base + '/topic/prd.md?raw=1', headers={'Range': 'bytes=0-2'}),
                (206, expected['topic/prd.md'][:3]))
            self.assertEqual(get(base + '/topic/prd.md?raw=1', method='HEAD'), (200, b''))
            status, body = get(base + '/topic/')
            self.assertEqual(status, 200); self.assertIn(b'prd.md', body); self.assertNotIn(b'_internal', body)
            self.assertEqual(get(base + '/topic/_internal/versions/v1/prd.md')[0], 404)
            logical = self.f.root / base.lstrip('/') / 'topic/prd.md'
            self.assertIsNotNone(viewer.viewer_url(self.f.root, logical, port=port))
        self.assertEqual(before, {p: p.read_bytes() for p in self.f.root.rglob('*') if p.is_file()})


if __name__ == '__main__': unittest.main()
