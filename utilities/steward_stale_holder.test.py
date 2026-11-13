#!/usr/bin/env python3
"""Stale supervisor regression; entirely synthetic homes and registry rows."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import io
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('cap_fixture', HERE / 'peer_steward_cap.test.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class StaleHolderTest(fixture.GrantFixture):
    def test_live_or_unknown_holders_cannot_silently_withhold_the_role(self):
        for state in ('live', 'unknown'):
            with self.subTest(state=state), mock.patch.object(fixture.fleet_steward, 'session_liveness', return_value=state):
                for n in range(4):
                    sid = f'{state}-{n}'
                    self.session(sid, self.primary)
                    result = self.grant(sid)
                    self.assertTrue(result['written'], result)
                    self.assertNotEqual(result['status'], 'limited')

    def test_all_three_harnesses_grant_roles_above_reference(self):
        for harness in ('claude', 'codex', 'opencode'):
            for n in range(3):
                sid = f'{harness}-{n}'
                self.session(sid, self.primary, harness=harness)
                result = fixture.peer_message.grant_steward(harness, sid, fixture.EXPLICIT_TO,
                    'explicit', fixture.TS, source='explicit', steward_seat=None)
                self.assertTrue(result['written'], result)

    def test_proven_seat_is_saved_and_nonlive_predecessor_does_not_count_twice(self):
        for sid in ('old', 'new'):
            self.session(sid, self.primary)
            self.grant(sid, steward_seat={'kind': 'pane', 'key': 'shared-seat', 'pane': '%1', 'harness': 'claude'})
        path = fixture.peer_message.steward_marker_path('claude', 'new')
        self.assertEqual(json.loads(path.read_text()).get('seat_key'), 'shared-seat')
        # Give the successor a strictly later marker generation timestamp.
        data = json.loads(path.read_text()); data['updated'] = '2026-11-13T00:00:00Z'; path.write_text(json.dumps(data))
        with mock.patch.object(fixture.fleet_steward, 'session_liveness', side_effect=lambda h,s,**kw: 'live' if s=='new' else 'unknown'):
            result = fixture.fleet_steward.repo_supervision(cwd=str(self.primary), agents=[])
        self.assertEqual(result['counted'], 1, result)
        old = next(h for h in result['holders'] if h['session_id']=='old')
        self.assertEqual(old['liveness'], 'unknown')
        self.assertEqual(old['count_reason'], 'same-seat-successor')

    def test_live_predecessor_is_never_discarded_by_a_newer_marker(self):
        for sid in ('old', 'new'):
            self.session(sid, self.primary)
            self.grant(sid, steward_seat={'kind':'pane','key':'same-seat'})
        with mock.patch.object(fixture.fleet_steward, 'session_liveness', return_value='live'):
            result=fixture.fleet_steward.repo_supervision(cwd=str(self.primary), agents=[])
        self.assertEqual(result['counted'], 2)

    def test_repeated_watch_directive_keeps_supported_carrier(self):
        arm = {'watch_id':'0123456789abcdef', 'target':'claude:child', 'until':['idle'], 'wake':'hook'}
        paths = SimpleNamespace(receipt=self.base/'receipt')
        text = fixture.peer_steward._already_armed_line(arm['watch_id'], arm, paths)
        self.assertIn('parent_next=end-turn', text)
        self.assertNotIn('steward-line-does-not-arm', text)

    def test_rearm_restores_observer_before_role_and_reports_current_identity(self):
        ps = fixture.peer_steward
        watch_id = '0123456789abcdef'
        paths = ps._watch_paths(watch_id, ps._watch_root())
        paths.arm.parent.mkdir(parents=True, exist_ok=True)
        arm = {'watch_id':watch_id, 'target':'child', 'wake':'hook',
               'watcher':{'pid':123,'pid_start':'old'}}
        paths.arm.write_text(json.dumps(arm))
        events=[]
        def spawn(*args):
            events.append('spawn')
            paths.arm.write_text(json.dumps(dict(arm,watcher={'pid':456,'pid_start':'new'})))
            return True
        def restore(wid,current):
            events.append('restore')
            self.assertEqual(current['watcher']['pid'],456)
        out=io.StringIO()
        with mock.patch.object(ps,'_watcher_present',return_value=False), \
             mock.patch.object(ps,'_spawn_watch_observer',side_effect=spawn), \
             mock.patch.object(ps,'_restore_watch_role',side_effect=restore), \
             contextlib.redirect_stdout(out):
            self.assertEqual(ps.cmd_rearm(SimpleNamespace(watch_id=watch_id)),0)
        self.assertEqual(events,['spawn','restore'])
        self.assertIn('pid=456 pid_start=new',out.getvalue())

    def test_taking_role_recovers_existing_watch_targets(self):
        ps = fixture.peer_steward
        self.session('owner', self.primary)
        paths = ps._watch_paths('0123456789abcdef', ps._watch_root())
        paths.arm.parent.mkdir(parents=True,exist_ok=True)
        paths.arm.write_text(json.dumps({'watch_id':'0123456789abcdef', 'steward':{'session_id':'owner','harness':'claude'},'agent':{'harness':'codex','session_id':'child','name':'child','pane':'%2'},
            'target':'codex:child','armed_ts':fixture.TS}))
        with mock.patch.object(ps, '_current_session_identity', return_value=('owner','claude')), \
             mock.patch.object(ps, '_ensure_steward_runner'):
            self.assertEqual(ps.cmd_steward(SimpleNamespace(state='on')),0)
        data=json.loads(fixture.peer_message.steward_marker_path('claude','owner').read_text())
        self.assertIn('child',data['targets'])


if __name__ == '__main__':
    unittest.main()
