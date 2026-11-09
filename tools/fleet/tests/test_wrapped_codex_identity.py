"""Fleet reuses the native pane proof when a wrapper owns the outer foreground."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'utilities')]
from fleet.collectors import codex, herdr, procscan
from fleet.model import Session
import pane_ownership
import session_tidy

spec = importlib.util.spec_from_file_location('wrapped_fleet_fixture',
    ROOT / 'utilities/pane_ownership_wrapped.test.py')
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


class WrappedFleetIdentityTest(unittest.TestCase):
    close_children = f.WrappedTidyTest.close_children
    publish = f.WrappedTidyTest.publish

    def setUp(self):
        f.WrappedTidyTest.setUp(self)
        self.row = Session(harness='codex', pid=self.native['pid'],
                           proc_start=self.native['start'], cwd='/inner/project')
        self.panes = [{'pane_id': self.pane, 'agent': 'codex', 'cwd': '/outer/project',
                       'agent_session': {'agent': 'codex',
                                         'value': '01000000-0000-7000-8000-000000000002'}}]
        self.observation = SimpleNamespace(endpoint='default', socket_path=self.socket)
        mock.patch.object(procscan, 'read_proc_start',
                          side_effect=lambda pid: self.native['start'] if pid == self.row.pid else None).start()

    def runner(self, argv, **kwargs):
        self.assertEqual(argv[-3:], ['process-info', '--pane', self.pane])
        return SimpleNamespace(returncode=0, stdout=json.dumps({'result': {'process_info': self.info}}))

    def collect(self, **kwargs):
        return herdr.codex_pane_sessions([self.row], panes=self.panes, runner=self.runner,
                                         observation=self.observation, force_current=True, **kwargs)

    def test_wrapped_inner_pid_is_proved_without_inventing_outer_foreground_membership(self):
        original = copy.deepcopy(self.info)
        self.assertEqual(self.collect(), {self.row.pid: self.sid})
        self.assertNotIn(self.row.pid, [p['pid'] for p in self.info['foreground_processes']])
        self.assertEqual(self.info, original)

    def test_bare_current_native_proof_still_works(self):
        self.panes[0]['cwd'] = self.row.cwd
        self.info = {**self.info, 'foreground_process_group_id': self.row.pid,
                     'foreground_processes': [{'pid': self.row.pid, 'name': 'codex'}]}
        with mock.patch.object(pane_ownership, '_native_session', return_value=self.sid):
            self.assertEqual(self.collect(), {self.row.pid: self.sid})

    def test_prepare_and_enrich_recover_the_exact_rollout_model(self):
        home = Path(self.home.name) / 'codex'
        path = home / 'sessions' / ('rollout-test-' + self.sid + '.jsonl')
        path.parent.mkdir(parents=True)
        path.write_text('\n'.join(json.dumps(r) for r in (
            {'type': 'session_meta', 'payload': {'id': self.sid, 'cwd': self.row.cwd}},
            {'type': 'turn_context', 'payload': {'model': 'fixture-model', 'effort': 'medium'}},
        )) + '\n')
        original = herdr.codex_pane_sessions
        def current(rows, **kwargs):
            return original(rows, panes=self.panes, runner=self.runner, observation=self.observation,
                            force_current=kwargs.get('force_current', False))
        with mock.patch.object(codex, '_home', return_value=str(home)), \
             mock.patch.object(codex, 'process_rollouts', return_value=({}, set())), \
             mock.patch.object(codex, '_transfer_managed_rollouts'), \
             mock.patch.object(codex, '_tick_subagents'), \
             mock.patch.object(herdr, 'codex_pane_sessions', side_effect=current):
            tick = codex.prepare_tick([self.row])
            codex.enrich(self.row, tick=tick)
        self.assertEqual((self.row.session_id, self.row.model), (self.sid, 'fixture-model'))

    def test_changed_birth_unknown_root_and_proven_other_sid_remain_unresolved(self):
        with mock.patch.object(procscan, 'read_proc_start', side_effect=[self.native['start'], 'reused']):
            self.assertEqual(self.collect(), {})
        for current in (None, '', '01000000-0000-7000-8000-000000000002'):
            with self.subTest(current_root=current), mock.patch(
                    'fleet.collectors.codex.session_id_of_process', return_value=current):
                self.assertEqual(self.collect(), {})
        with mock.patch('fleet.process_identity.process_identity', return_value=SimpleNamespace(
                confidence='proven', session_id='01000000-0000-7000-8000-000000000002')):
            self.assertEqual(self.collect(), {})

    def test_competing_attachment_and_foreign_endpoint_remain_unresolved(self):
        self.observation.socket_path = self.socket + '.foreign'
        self.assertEqual(self.collect(), {})
        self.observation.socket_path = self.socket
        self.publish('codex', sid='01000000-0000-7000-8000-000000000002',
                     host_pid=self.native['pid'], host_start=self.native['start'],
                     native_pid=self.host['pid'], native_start=self.host['start'])
        self.assertEqual(self.collect(), {})

    def test_unobserved_hint_does_not_start_another_pane_query(self):
        with mock.patch.object(herdr, 'pane_evidence', return_value=herdr.PaneEvidence((set(), set(), {}))), \
             mock.patch.object(pane_ownership, 'current_codex_thread') as reader:
            self.assertEqual(self.collect(), {})
        reader.assert_not_called()

    def test_cli_default_reuses_the_existing_server_reported_endpoint(self):
        original = herdr.pane_evidence
        def evidence(panes, **kwargs):
            return original(panes, runner=self.runner, bindings=kwargs.get('bindings'))
        with mock.patch.dict(os.environ, {'HERDR_SOCKET_PATH': ''}), \
             mock.patch.object(herdr, 'list_panes', return_value=(self.panes, None)), \
             mock.patch.object(herdr, 'pane_evidence', side_effect=evidence), \
             mock.patch.object(session_tidy, '_herdr_endpoint_socket', return_value=self.socket) as endpoint:
            self.assertEqual(herdr.codex_pane_sessions([self.row], force_current=True),
                             {self.row.pid: self.sid})
        endpoint.assert_called_once_with('')
        with mock.patch.dict(os.environ, {'HERDR_SOCKET_PATH': ''}), \
             mock.patch.object(herdr, 'list_panes', return_value=(self.panes, None)), \
             mock.patch.object(herdr, 'pane_evidence', side_effect=evidence), \
             mock.patch.object(session_tidy, '_herdr_endpoint_socket', return_value=''), \
             mock.patch.object(pane_ownership, 'current_codex_thread') as reader:
            self.assertEqual(herdr.codex_pane_sessions([self.row], force_current=True), {})
        reader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
