#!/usr/bin/env python3
"""Exact native metadata restores missing steward rows without borrowing caller cwd."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'utilities')]
from fleet.collectors import steward, codex, opencode
import steward_check as check
import peer_obligations


class NativeStewardProjectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {'HOME':str(self.root / 'home'),
            'XDG_STATE_HOME':str(self.root / 'state'), 'AGENT_PEER_LEDGER_ROOT':str(self.root / 'peers')})
        patch.start(); self.addCleanup(patch.stop)
        self.repos = []
        for name in ('project', 'foreign'):
            folder = self.root / name; folder.mkdir()
            subprocess.run(['git','init','-q',str(folder)],check=True,capture_output=True)
            self.repos.append(str(folder))
        self.db = self.root / 'state.sqlite'
        with sqlite3.connect(self.db) as con:
            con.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT)')
            con.executemany('INSERT INTO threads VALUES(?,?)',[
                ('actor',self.repos[0]),('newer',self.repos[0]),('target',self.repos[0]),
                ('foreign',self.repos[1])])
        for obj,name,value in ((codex,'_state_db',self.db),(codex,'_home',self.root),
                               (opencode,'_db',None)):
            p=mock.patch.object(obj,name,return_value=value);p.start();self.addCleanup(p.stop)
        self.markers = {('codex','actor'):{'harness':'codex','session_id':'actor','targets':{}}}
        self.relations = [self.relation('actor','2026-01-01T00:00:00Z')]
        self.rows = [SimpleNamespace(harness='codex',session_id='target',cwd=self.repos[0]),
                     SimpleNamespace(harness='codex',session_id='foreign',cwd=self.repos[1]),
                     SimpleNamespace(harness='codex',session_id='unknown',cwd='')]
        p=mock.patch.object(steward,'_projection_sessions',side_effect=lambda:list(self.rows));p.start();self.addCleanup(p.stop)
        p=mock.patch.object(steward,'read_markers',side_effect=lambda:self.markers);p.start();self.addCleanup(p.stop)
        p=mock.patch.object(steward,'read_relations',side_effect=lambda:self.relations);p.start();self.addCleanup(p.stop)

    def relation(self, actor, ts):
        return {'holder':{'harness':'codex','sid':actor},'released':False,'targets':[
            {'harness':'codex','session_id':sid,'source':'watch','kind':'watch','ts':ts}
            for sid in ('target','foreign','unknown')]}

    def targets(self, actor='actor'):
        return [r['session_id'] for r in steward.role_targets('codex',actor)]

    def test_actor_absent_from_snapshot_uses_its_own_native_repository(self):
        self.assertEqual(self.targets(),['target'])
        self.assertTrue(steward.role_flag('codex','actor'))

    def test_confirmed_relation_holder_is_a_named_native_actor_without_a_current_marker(self):
        self.markers = {}
        self.assertEqual(self.targets(),['target'])

    def test_actor_unknown_or_conflicting_repository_never_borrows_the_target_repository(self):
        with sqlite3.connect(self.db) as con:con.execute("DELETE FROM threads WHERE id='actor'")
        self.assertEqual(self.targets(),[])
        self.rows += [SimpleNamespace(harness='codex',session_id='actor',cwd=p) for p in self.repos]
        self.assertEqual(self.targets(),[])

    def test_authoritative_snapshot_repository_overrides_historical_native_metadata(self):
        self.rows.append(SimpleNamespace(harness='codex',session_id='actor',cwd=self.repos[1]))
        self.assertEqual(self.targets(),['foreign'])

    def test_current_actor_row_wins_over_historical_alias_metadata(self):
        self.rows.append(SimpleNamespace(harness='codex',session_id='actor',cwd=self.repos[0]))
        self.markers[('codex','old-actor')]={'harness':'codex','session_id':'old-actor','targets':{}}
        with sqlite3.connect(self.db) as con:
            con.execute('INSERT INTO threads VALUES(?,?)',('old-actor',self.repos[1]))
        targets=steward.role_targets('codex','actor',aliases=('old-actor',))
        self.assertEqual([t['session_id'] for t in targets],['target'])

    def test_latest_same_repository_winner_stays_unique(self):
        self.relations.append(self.relation('newer','2026-01-02T00:00:00Z'))
        self.assertEqual(self.targets(),[])
        self.assertEqual(self.targets('newer'),['target'])

    def test_existing_check_sees_targets_and_uses_the_existing_hourly_duty(self):
        store=peer_obligations.ObligationStore(self.root / 'duties')
        with mock.patch.object(check,'_collector',return_value=steward), \
             mock.patch.object(check,'_relations',side_effect=lambda:self.relations), \
             mock.patch.object(check,'_agents',return_value=[]), \
             mock.patch.object(check,'_end_observation',return_value=None), \
             mock.patch.object(check,'_session_liveness',return_value='live'), \
             mock.patch.object(check,'_human_wait_open',return_value=False), \
             mock.patch.object(check,'interval_seconds',return_value=3600), \
             mock.patch.object(check,'_now',return_value=1000.0), \
             mock.patch.object(peer_obligations,'ensure_normal_runner',return_value=False), \
             mock.patch('session_notice.notify',return_value={'state':'pending'}) as notify:
            seen=check._observe_role('codex','actor')
            self.assertEqual([r['session_id'] for r in seen['targets']],['target','unknown'])
            self.assertEqual(seen['targets'][1]['liveness'], 'unknown')
            self.assertNotIn('foreign', [r['session_id'] for r in seen['targets']])
            self.assertEqual(check._classify(seen),'run-steward-check')
            check.register('codex','actor',store=store,ensure_runner=False)
            duty=store.get(check.duty_id('codex','actor'))
            check.advance(duty,store,now=4599.0)
            notify.assert_not_called()
            check.advance(store.get(duty['id']),store,now=4601.0)
            self.assertEqual(notify.call_count,1)
            self.assertEqual(notify.call_args.kwargs['required_action'],'run-steward-check')


if __name__ == '__main__':unittest.main()
