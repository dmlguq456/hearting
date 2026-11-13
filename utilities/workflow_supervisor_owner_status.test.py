#!/usr/bin/env python3
"""Initial owner execution is visible without mutating the route journal."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('owner_status', Path(__file__).with_name('workflow-supervisor.py'))
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


class OwnerStatusTest(unittest.TestCase):
    def render(self, process='live', status='open', journal='CREATED'):
        route = {'route_id':'rt-fixture', 'nodes':[{'id':'one-shot'}]}
        state = {'nodes':{}, 'workflow_state':journal, 'updated_at':None}
        ledger = mock.Mock()
        ledger.read_only_state.return_value = state
        ledger.claims.return_value = []
        rows = [{'time':'now','status':status,'meta':{'attempt_id':'new-owner',
                 'owner_route_id':'rt-fixture','dispatch_depth':'1','worker_type':'owner'}}]
        with contextlib.ExitStack() as stack:
            for name, value in [('route_module', SimpleNamespace(resolve_route_argument=lambda *a:'route.json')),
                                ('load_route',route),('ledger_for',ledger),('read_armed',{}),
                                ('terminal_gate_state',{}),('_closed_terminal_projection',False),
                                ('read_resource_watches',[]),('resource_children',[]),('ledger_metadata',{}),
                                ('_registry_rows',rows)]:
                stack.enter_context(mock.patch.object(supervisor,name,return_value=value))
            stack.enter_context(mock.patch('dispatch_contract.attempt_process_quiescence',
                return_value=SimpleNamespace(state=process,reason='exact-fixture')))
            out=io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(supervisor.cmd_status(SimpleNamespace(route='route.json',jobs='fixture-jobs',json=True)),0)
        ledger.read_only_state.assert_called_once()
        self.assertEqual(state['nodes'],{})
        return json.loads(out.getvalue())

    def test_late_predecessor_row_does_not_hide_replacement_owner(self):
        def row(attempt,status):
            return {'status':status,'meta':{'attempt_id':attempt,'owner_route_id':'rt-fixture',
                    'dispatch_depth':'1','worker_type':'owner'}}
        with mock.patch.object(supervisor,'_registry_rows',return_value=[row('old','open'),row('new','open'),row('old','done')]), \
             mock.patch('dispatch_contract.attempt_process_quiescence',return_value=SimpleNamespace(state='live',reason='fixture')):
            self.assertEqual(supervisor._status_owner({'route_id':'rt-fixture'},'jobs')['attempt_id'],'new')

    def test_first_owner_running_before_first_journal_event(self):
        result=self.render()
        self.assertEqual(result['workflow_state'],'RUNNING')
        self.assertEqual(result['journal_workflow_state'],'CREATED')
        self.assertEqual(result['owner_attempt_id'],'new-owner')
        self.assertEqual(result['owner']['liveness'],'live')
        self.assertEqual(result['current_stage'],['one-shot'])
        self.assertEqual(result['next_stage'],[])

    def test_missing_observation_does_not_invent_execution(self):
        result=self.render(process='unverifiable')
        self.assertEqual(result['workflow_state'],'CREATED')
        self.assertEqual(result['owner']['liveness'],'unknown')
        self.assertEqual(result['current_stage'],[])

    def test_dead_or_terminal_owner_is_not_projected_as_running(self):
        for process,status in [('quiescent','open'),('live','done')]:
            with self.subTest(process=process,status=status):
                self.assertEqual(self.render(process,status)['workflow_state'],'CREATED')

    def test_human_gate_state_survives_live_owner(self):
        self.assertEqual(self.render(journal='BLOCKED_HUMAN_GATE')['workflow_state'],'BLOCKED_HUMAN_GATE')


if __name__ == '__main__':
    unittest.main()
