#!/usr/bin/env python3
"""Tidy joins the existing wrapper attachment without granting stale input authority."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'utilities'), str(ROOT / 'tools')]
import pane_ownership as po
import session_shell
import session_tidy as st

spec = importlib.util.spec_from_file_location('tidy_wrapped_fixture',
    ROOT / 'utilities/peer_shell_resolution.test.py')
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
CURRENT_THREAD = po.current_codex_thread


class WrappedTidyTest(unittest.TestCase):
    close_children = f.WrappedPaneResolutionTest.close_children
    publish = f.WrappedPaneResolutionTest.publish

    def setUp(self):
        f.WrappedPaneResolutionTest.setUp(self)
        # The fixture uses real live children and private birth-bound attachments.
        # Only their terminal/runtime observations are synthetic; no native model
        # or user terminal is opened or receives input.
        self.binding = self.publish('codex')
        self.socket = self.home.name + '/herdr.sock'
        self.record = (self.native['pid'], self.broker['pid'], self.native['pid'],
                       123, self.native['pid'], self.native['start'])
        mock.patch.object(po, 'current_codex_thread', CURRENT_THREAD).start()
        mock.patch.dict(sys.modules, {'_hearting_panel_peer_steward': f.peer}).start()
        mock.patch.dict(os.environ, {'HERDR_PANE_ID': self.pane,
            'HERDR_SOCKET_PATH': self.socket, 'CODEX_THREAD_ID': self.sid,
            'AGENT_HERDR_SESSION': ''}).start()
        mock.patch.object(po, '_process', side_effect=lambda pid:
            self.record if pid == self.native['pid'] else None).start()
        mock.patch.object(po, '_runtime', side_effect=lambda pid:
            ('codex', False) if pid == self.native['pid'] else (None, False)).start()
        mock.patch.object(po, '_registered_worker', return_value=False).start()
        mock.patch.object(po, '_caller_runtime', return_value=(self.record, False)).start()
        mock.patch.object(po, '_pane_reader', return_value='/fixture/herdr').start()
        mock.patch.object(po, '_pane_process_info', side_effect=lambda *a, **kw: self.info).start()
        mock.patch.object(po, '_read_codex_footer_threads', return_value=set()).start()
        mock.patch('fleet.collectors.procscan.read_environ', return_value={
            'HERDR_PANE_ID': self.pane, 'HERDR_SOCKET_PATH': self.socket}).start()
        self.roots = mock.patch('fleet.collectors.codex.session_id_of_process',
                               return_value=self.sid).start()
        mock.patch.object(po.os, 'listdir', return_value=[str(self.native['pid'])]).start()
        mock.patch.object(po.shutil, 'which', return_value='/fixture/herdr').start()

    def loader(self):
        return po._loader_verified_pane(self.pane, self.sid, self.native['pid'],
            self.native['start'], executable='/fixture/herdr', socket_path=self.socket)

    def current(self, pid=None):
        return po.current_codex_thread(self.pane, pid, executable='/fixture/herdr',
                                       socket_path=self.socket)

    def test_exact_native_tidy_seat_resolves_inner_pty_behind_outer_broker(self):
        seat = st.resolve_seat('codex', self.home.name, sid=self.sid, project=False)
        self.assertEqual((seat.kind, seat.pane, st.session_identity(seat)), ('pane', self.pane, ('codex', self.sid)))
        self.assertEqual(self.loader(), self.pane)

    def test_effect_reader_accepts_only_the_bound_inner_or_outer_owner(self):
        for pid in (None, self.native['pid'], self.broker['pid']):
            with self.subTest(pid=pid):
                self.assertEqual(self.current(pid), self.sid)
        self.assertEqual(self.current(self.host['pid']), '')
        self.assertEqual(self.current(2147483000), '')

    def test_native_current_root_unknown_multiple_or_other_sid_withholds_physical_authority(self):
        self.assertEqual(self.loader(), self.pane)
        self.assertTrue(self.roots.call_args_list)
        for root in (None, '', '01000000-0000-7000-8000-000000000002'):
            with self.subTest(root=root), mock.patch(
                    'fleet.collectors.codex.session_id_of_process', return_value=root):
                self.assertEqual(self.loader(), '')
                self.assertEqual(self.current(self.broker['pid']), '')
        self.assertTrue(all(call.kwargs.get('fresh') for call in self.roots.call_args_list))

    def test_proven_inner_conflict_is_reused_at_discovery_and_final_recheck(self):
        other = '01000000-0000-7000-8000-000000000002'
        for phase in ('discovery', 'effect'):
            reads = []
            def identity(pid, harness, **kw):
                reads.append(pid)
                sid = other if phase == 'discovery' or len(reads) > 1 else self.sid
                return SimpleNamespace(confidence='proven', session_id=sid)
            with self.subTest(phase=phase), mock.patch(
                    'fleet.process_identity.process_identity', side_effect=identity):
                self.assertEqual(self.loader(), '')
                self.assertIn(self.native['pid'], reads)

    def test_changed_attachment_birth_endpoint_and_foreground_are_rejected(self):
        for field in ('native_start', 'host_start', 'broker_start'):
            with self.subTest(field=field):
                self.publish('codex', **{field: 'stale'})
                self.assertEqual(self.loader(), '')
                self.publish('codex')
        for info in ({**self.info, 'pane_id': 'another:p1'},
                     {**self.info, 'foreground_process_group_id': self.native['pid']}):
            with self.subTest(info=info), mock.patch.object(po, '_pane_process_info', return_value=info):
                self.assertEqual(self.loader(), '')
        with mock.patch('fleet.collectors.procscan.read_environ', return_value={
                'HERDR_PANE_ID': self.pane, 'HERDR_SOCKET_PATH': self.socket + '.other'}):
            self.assertEqual(self.loader(), '')
        with mock.patch.object(po, '_pane_process_info', side_effect=[self.info, {
                **self.info, 'foreground_process_group_id': self.native['pid']}]):
            self.assertEqual(self.loader(), '')

    def test_ambiguous_attachments_and_root_change_after_discovery_stay_unproven(self):
        self.assertEqual(self.loader(), self.pane)
        with mock.patch('fleet.collectors.codex.session_id_of_process', return_value=''):
            self.assertEqual(self.current(self.broker['pid']), '')
        self.publish('codex', host_pid=self.native['pid'], host_start=self.native['start'],
                     native_pid=self.host['pid'], native_start=self.host['start'])
        self.assertEqual(self.loader(), '')
        self.assertEqual(self.current(self.broker['pid']), '')

    def test_tidy_effect_rechecks_native_root_even_when_metadata_keeps_the_old_sid(self):
        agent = {"pane": self.pane, "harness": "codex", "session_id": self.sid}
        req = {"harness": "codex", "sid": self.sid, "seat": {"pane": self.pane}}
        with mock.patch.object(f.peer, '_run_herdr_get'), \
             mock.patch.object(f.peer.subprocess, 'run', return_value=SimpleNamespace(
                 stdout=json.dumps({'result': {'process_info': self.info}}))), \
             mock.patch.object(f.peer, '_interpret_payload', return_value=('idle', agent, 0, None)), \
             mock.patch.object(f.peer, '_tmux_input_refusal', return_value=None), \
             mock.patch.object(f.peer, '_read_screen', return_value=['synthetic empty composer']), \
             mock.patch.object(f.peer, '_screen_ready', return_value=None), \
             mock.patch.object(f.peer, '_herdr_prompt') as send:
            self.assertIsNone(f.peer._clear_look(self.pane, req)[0])
            with mock.patch('fleet.collectors.codex.session_id_of_process', return_value=''):
                self.assertEqual(f.peer._clear_look(self.pane, req)[0], 'session-identity-unknown')
            with mock.patch.object(f.peer, '_screen_ready', return_value='draft'):
                self.assertEqual(f.peer._clear_look(self.pane, req)[0], 'draft')
            send.assert_not_called()

    def test_unavailable_physical_binding_keeps_native_card_storage(self):
        with mock.patch('fleet.collectors.codex.session_id_of_process', return_value=''):
            seat = st.resolve_seat('codex', self.home.name, sid=self.sid)
        native = st.session_seat(seat, 'codex', self.sid)
        st.write_card(native, 'codex', self.sid, '# Synthetic continuity\nKeep the pending task.\n')
        self.assertEqual(st.read_latest_card(native)['body'], '# Synthetic continuity\nKeep the pending task.')
        self.assertEqual(st.session_identity(native), ('codex', self.sid))
        self.assertFalse(native.pane)


if __name__ == '__main__':
    unittest.main()
