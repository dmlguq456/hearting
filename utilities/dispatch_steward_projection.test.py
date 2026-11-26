#!/usr/bin/env python3
"""Passive steward projections preserve the fresh reader's evidence rules."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
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
