#!/usr/bin/env python3
"""Passive steward projections preserve the fresh reader's evidence rules."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(os.environ.get('AGENT_HOME', Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(ROOT / 'utilities'))
import dispatch_seat_handover as handover
import dispatch_supervision as supervision
import session_tidy as tidy
from split_module_fixture import patch_split


class StewardProjectionTest(unittest.TestCase):
    def test_snapshot_seat_projection_revalidates_changed_and_unsafe_files(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch_split(tidy, 'state_root', return_value=Path(directory)):
            root = Path(directory) / 'handover'
            root.mkdir()
            path = root / 'fixture.json'
            doc = dict(schema=handover.SCHEMA, bindings=[],
                       seat=dict(key='seat-a', kind='pane'), **{'from': dict(sid='origin')})
            path.write_text(json.dumps(doc))
            with mock.patch.object(handover, 'read_snapshot', wraps=handover.read_snapshot) as read:
                self.assertEqual(handover._snapshot_seat_keys(), {'seat-a'})
                self.assertEqual(handover._snapshot_seat_keys(), {'seat-a'})
                self.assertEqual(read.call_count, 2)
                with supervision.route_attention_read_scope():
                    self.assertEqual(handover._snapshot_seat_keys(), {'seat-a'})
                    self.assertEqual(handover._snapshot_seat_keys(), {'seat-a'})
                    self.assertEqual(read.call_count, 3)
                    doc['seat']['key'] = 'seat-b'
                    replacement = root / 'replacement'
                    replacement.write_text(json.dumps(doc))
                    replacement.replace(path)
                    self.assertEqual(handover._snapshot_seat_keys(), {'seat-b'})
                    doc['bindings'] = [dict(jobs='incomplete')]
                    path.write_text(json.dumps(doc))
                    with self.assertRaises(ValueError):
                        handover._snapshot_seat_keys()
                    path.unlink()
                    path.symlink_to(replacement)
                    with self.assertRaises(ValueError):
                        handover._snapshot_seat_keys()

    def test_lineage_validation_reuses_only_unchanged_snapshot_fields(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch_split(tidy, 'state_root', return_value=Path(directory)):
            root = Path(directory) / 'handover'
            root.mkdir()
            path = root / 'fixture.json'
            doc = dict(schema=handover.SCHEMA, bindings=[],
                       seat=dict(key='seat-a', kind='pane'), **{'from': dict(sid='origin')})
            path.write_text(json.dumps(doc))
            expected = handover.lineage_graph()
            handover._PASSIVE_SNAPSHOT_SEATS.clear()
            with mock.patch.object(handover, 'read_snapshot', wraps=handover.read_snapshot) as read:
                for _ in range(2):
                    with supervision.route_attention_read_scope():
                        self.assertEqual(handover.lineage_graph(), expected)
                self.assertEqual(read.call_count, 1)
                # Active observations retain their original complete reads.
                handover.lineage_graph()
                self.assertEqual(read.call_count, 2)
                doc['bindings'] = [dict(jobs='incomplete')]
                path.write_text(json.dumps(doc))
                with supervision.route_attention_read_scope(), self.assertRaises(ValueError):
                    handover.lineage_graph()
                path.write_text(json.dumps(dict(doc, bindings=[])))
                with supervision.route_attention_read_scope():
                    self.assertEqual(handover.lineage_graph(), expected)
                with mock.patch.object(handover._PASSIVE_SNAPSHOT_SEATS, 'max_bytes', 1):
                    handover._PASSIVE_SNAPSHOT_SEATS.clear()
                    with supervision.route_attention_read_scope():
                        self.assertEqual(handover.lineage_graph(), expected)
                    self.assertEqual(len(handover._PASSIVE_SNAPSHOT_SEATS), 0)

    def test_stall_route_validation_keeps_journal_reads_fresh(self):
        import human_gate_observation as waits
        import route_identity
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs, path = root / 'jobs.log', root / 'route.json'
            jobs.write_text('')
            doc = dict(route_id='rt-fixture', artifact_root=str(root), nodes=[])
            doc['route_hash'] = route_identity.route_hash(doc)
            path.write_text(json.dumps(doc))
            meta = dict(route_file=str(path), route_id=doc['route_id'], route_hash=doc['route_hash'])
            supervision._PASSIVE_ROUTE_DOCUMENTS.clear()
            journal = [dict(route_id=doc['route_id'], route_hash=doc['route_hash'],
                            workflow_state='BLOCKED_HUMAN_GATE', at='2026-10-14T01:02:03Z',
                            evidence=dict(gate='fixture'))]
            with mock.patch.object(waits, '_journal', side_effect=lambda _ledger: list(journal)) as read, \
                    mock.patch.object(route_identity, 'route_hash', wraps=route_identity.route_hash) as digest:
                for _ in range(2):
                    with supervision.route_attention_read_scope():
                        self.assertTrue(supervision._attempt_human_wait(meta, jobs))
                self.assertEqual(digest.call_count, 1)
                self.assertEqual(read.call_count, 2)
                journal.clear()
                with supervision.route_attention_read_scope():
                    self.assertFalse(supervision._attempt_human_wait(meta, jobs))
                self.assertEqual(digest.call_count, 1)
                # Same-size edit with restored mtime still invalidates via ctime.
                stamp = path.stat()
                path.write_text(path.read_text().replace('rt-fixture', 'rt-changed'))
                os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                with supervision.route_attention_read_scope():
                    self.assertIsNone(supervision._attempt_human_wait(meta, jobs))
                path.write_text(json.dumps(doc))
                with supervision.route_attention_read_scope():
                    self.assertFalse(supervision._attempt_human_wait(meta, jobs))
                self.assertEqual(digest.call_count, 2)
                # Carriers outside the passive scope never borrow cached JSON/hash.
                self.assertFalse(supervision._attempt_human_wait(meta, jobs))
                self.assertEqual(digest.call_count, 3)
            with supervision.route_attention_read_scope():
                route, entry = supervision._read_route_document(path)
                hash_now = supervision._observed_route_hash(route, entry)
                with mock.patch.object(route_identity, 'route_hash', return_value='changed-policy'):
                    self.assertEqual(supervision._observed_route_hash(route, entry), 'changed-policy')
                self.assertEqual(supervision._observed_route_hash(route, entry), hash_now)
            path.write_text('{malformed')
            with supervision.route_attention_read_scope():
                self.assertIsNone(supervision._attempt_human_wait(meta, jobs))
            path.unlink()
            with supervision.route_attention_read_scope():
                self.assertIsNone(supervision._attempt_human_wait(meta, jobs))
            path.write_text(json.dumps(doc))
            path.chmod(0)
            with supervision.route_attention_read_scope():
                self.assertIsNone(supervision._attempt_human_wait(meta, jobs))
            path.chmod(0o600)
            with mock.patch.object(supervision._PASSIVE_ROUTE_DOCUMENTS, 'max_bytes', 1):
                supervision._PASSIVE_ROUTE_DOCUMENTS.clear()
                with supervision.route_attention_read_scope():
                    self.assertFalse(supervision._attempt_human_wait(meta, jobs))
                self.assertEqual(len(supervision._PASSIVE_ROUTE_DOCUMENTS), 0)

    def test_route_attention_keeps_current_recipient_and_receipt_age_fresh(self):
        import hashlib
        from types import SimpleNamespace
        import route_identity
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs, path = root / 'jobs.log', root / 'route.json'
            jobs.write_text('')
            doc = dict(route_id='rt-fixture', nodes=[])
            doc['route_hash'] = route_identity.route_hash(doc)
            path.write_text(json.dumps(doc))
            meta = dict(route_file=str(path), route_id=doc['route_id'], route_hash=doc['route_hash'])
            key = hashlib.sha256(b'codex\0parent').hexdigest()
            receipts = root / 'start-receipts'
            receipts.mkdir()
            (receipts / (key + '.json')).write_text(json.dumps(dict(
                schema='start_receipt_v1', harness='codex', session_id='parent', saved_at=80,
                receipt=dict(state='needs-attention', route_id=doc['route_id'], reason='fixture'))))
            supervision._PASSIVE_ROUTE_DOCUMENTS.clear()
            with mock.patch.object(handover, 'resolve_recipient',
                    return_value=SimpleNamespace(session_id='parent', harness='codex')) as recipient:
                with supervision.route_attention_read_scope():
                    self.assertEqual(supervision._route_needs_attention(meta, jobs, 100)['reason'], 'fixture')
                with supervision.route_attention_read_scope():
                    self.assertIsNone(supervision._route_needs_attention(meta, jobs, 5000))
                recipient.return_value = SimpleNamespace(session_id='successor', harness='claude')
                with supervision.route_attention_read_scope():
                    self.assertIsNone(supervision._route_needs_attention(meta, jobs, 100))
                self.assertEqual(recipient.call_count, 3)

    def test_grouped_role_supersession_matches_original_oracle(self):
        states = {}
        for generation in range(80):
            before = dict(seat_key='seat', generation=generation,
                          origin=dict(harness='codex', sid='a'), holder=dict(harness='codex', sid='a'),
                          marker_digest='digest', targets=['repo'], lineage=[])
            after = copy.deepcopy(before)
            after['holder']['sid'] = 'b'
            after['lineage'] = [dict(snapshot_id='snapshot', **{'from': 'a', 'to': 'b'})]
            edge = json.dumps(dict(harness='codex', sid='b', snapshot_id='snapshot', **{'from': 'a'}))
            states[(str(generation), 'marker')] = before
            states[(str(generation), edge)] = after
        expected = {identity: relation for identity, relation in states.items()
                    if not (handover._role_transition_matches(identity, relation) and any(
                        handover._role_transition_matches(other_id, other)
                        and handover._role_follows(relation, other)
                        for other_id, other in states.items()))}
        with mock.patch.object(handover, '_role_follows', wraps=handover._role_follows) as follows:
            self.assertEqual(handover._current_roles(states), expected)
            self.assertLessEqual(follows.call_count, len(states) * 2)


if __name__ == '__main__':
    unittest.main()
