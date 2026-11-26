"""Synthetic native diagnostics only; no production logs or native input."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet.token_budget import codex_compaction_pressure, compaction_imminent, parse_codex_token_count


class CompactionPressureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.path = self.home / 'sessions/rollout-exact-session-id.jsonl'
        self.path.parent.mkdir()
        self.at = 1_000.5
        self.line = self.event('token_count', self.at)
        self.path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'exact-session-id', 'cli_version':'0.163.0'}})
                             + '\n' + self.line + '\n')
        self.database = self.home / 'logs_2.sqlite'
        self.connection = sqlite3.connect(self.database)
        self.addCleanup(self.connection.close)
        self.connection.executescript('CREATE TABLE logs(ts INTEGER,ts_nanos INTEGER,thread_id TEXT,target TEXT,feedback_log_body TEXT); CREATE INDEX ix ON logs(thread_id,ts);')
        self.add()

    def event(self, kind, at):
        return json.dumps({'type': 'event_msg', 'timestamp': datetime.fromtimestamp(at, timezone.utc).isoformat(),
                           'payload': {'type': kind, 'info': {'last_token_usage': {'total_tokens': 539613},
                                                            'model_context_window': 828400}}})

    def add(self, *, sid='exact-session-id', at=1_000.501,
            body='auto_compact_scope_tokens=799116 auto_compact_scope_limit=Some(784800) auto_compact_limit_scope=Total token_limit_reached=true'):
        self.connection.execute('INSERT INTO logs VALUES(?,?,?,?,?)',
                                (int(at), round((at % 1) * 1_000_000_000), sid,
                                 'codex_core::session::turn', body)); self.connection.commit()

    def read(self, **kw):
        return codex_compaction_pressure('exact-session-id', self.path, self.line,
                                         home=self.home, now=1_001, **kw)

    def clear(self):
        self.connection.execute('DELETE FROM logs'); self.connection.commit()

    def test_original_numbers_keep_server_usage_and_native_judgment_distinct(self):
        before = self.database.read_bytes()
        pressure = self.read()
        self.assertEqual((pressure['state'], pressure['scope_tokens'], pressure['limit_tokens'], pressure['scope']),
                         ('observed', 799116, 784800, 'Total'))
        self.assertTrue(compaction_imminent(pressure, now=1001))
        usage = parse_codex_token_count(self.line, session_id='exact-session-id')
        self.assertEqual((usage.active_context_tokens, usage.context_used_pct), (539613, 65))
        self.assertGreaterEqual(pressure['observed_at'], pressure['usage_at'])
        self.assertEqual(before, self.database.read_bytes())

    def test_foreign_session_or_previous_usage_point_cannot_supply_pressure(self):
        for sid, at in [('other-session-id', 1_000.501), ('exact-session-id', 1_000.49),
                        ('exact-session-id', 1_003.0)]:
            with self.subTest(sid=sid, at=at):
                self.clear(); self.add(sid=sid, at=at)
                self.assertEqual(self.read()['state'], 'unknown')
        self.assertFalse(compaction_imminent(None))

    def test_compaction_after_usage_and_newer_usage_invalidate_old_evidence(self):
        with self.path.open('a') as handle:
            handle.write(json.dumps({'type': 'compacted', 'timestamp': '1970-01-01T00:16:40.600Z'})+'\n')
        self.assertEqual(self.read()['state'], 'unknown')
        with self.path.open('a') as handle: handle.write(self.event('token_count', 1_000.8)+'\n')
        self.assertEqual(self.read()['state'], 'unknown')

    def test_same_new_interval_is_observed_without_reusing_precompact_judgment(self):
        compact = {'type': 'compacted', 'timestamp': '1970-01-01T00:16:39Z'}
        self.path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'exact-session-id', 'cli_version':'0.163.0'}})
                             +'\n'+json.dumps(compact)+'\n'+self.line+'\n')
        self.assertEqual(self.read()['compact_at'], 999)
        self.assertTrue(compaction_imminent(self.read(), now=1001))

    def test_missing_database_or_schema_and_changed_log_format_are_unknown(self):
        self.connection.close(); self.database.unlink()
        self.assertEqual(self.read()['state'], 'unknown'); self.assertFalse(self.database.exists())
        self.connection = sqlite3.connect(self.database)
        self.connection.execute('CREATE TABLE logs(other TEXT)'); self.connection.commit()
        self.assertEqual(self.read()['state'], 'unknown')
        self.connection.close()

    def test_exclusive_database_lock_and_foreign_rollout_are_unknown(self):
        self.connection.execute('BEGIN EXCLUSIVE')
        try:
            self.assertEqual(self.read()['state'], 'unknown')
        finally:
            self.connection.rollback()
        self.path.write_text(json.dumps({'type':'session_meta','payload':{'id':'other-session-id'}})
                             +'\n'+self.line+'\n')
        self.assertEqual(self.read()['state'], 'unknown')

    def test_incomplete_conflicting_unsupported_and_ambiguous_judgments_are_unknown(self):
        for body in ('auto_compact_scope_tokens=799116',
                     'auto_compact_scope_tokens=799116 auto_compact_scope_limit=None auto_compact_limit_scope=Total token_limit_reached=true',
                     'auto_compact_scope_tokens=799116 auto_compact_scope_limit=Some(784800) auto_compact_limit_scope=Future token_limit_reached=true',
                     'auto_compact_scope_tokens=799116 auto_compact_scope_limit=Some(784800) auto_compact_limit_scope=Total token_limit_reached=false'):
            with self.subTest(body=body):
                self.clear(); self.add(body=body); self.assertEqual(self.read()['state'], 'unknown')
        self.clear(); self.add(); self.add(at=1_000.502)
        self.assertEqual(self.read()['state'], 'unknown')

    def test_malformed_latest_boundary_never_reuses_an_older_judgment(self):
        for fragment in ('{"type":"compacted",', '{"payload":{"type":"token_count",',
                         '["token_count"]', '["compacted"]'):
            original=self.path.read_text()
            self.path.write_text(original+fragment+'\n')
            self.assertEqual(self.read()['state'],'unknown')
            self.path.write_text(original)

    def test_unverified_runtime_log_version_is_unknown(self):
        for version in ('0.162.1', '0.164.0', None):
            self.path.write_text(json.dumps({'type':'session_meta','payload':{
                'id':'exact-session-id','cli_version':version}})+'\n'+self.line+'\n')
            self.assertEqual(self.read()['reason'],'unsupported-log-version')

    def test_nonobject_metadata_and_truncated_log_bodies_stay_unknown(self):
        original=self.path.read_text()
        for row in (['session_meta'], 'session_meta', {'type':'session_meta','payload':[]}):
            self.path.write_text(json.dumps(row)+'\n'+self.line+'\n')
            self.assertEqual(self.read()['state'],'unknown')
        self.path.write_text(original)
        prefix='auto_compact_scope_tokens=799116 auto_compact_scope_limit=Some(784800) auto_compact_limit_scope=Total token_limit_reached=true '
        for padding in ('x'*8200, '한'*3000):
            self.clear();self.add(body=prefix+padding+' auto_compact_scope_tokens=1')
            self.assertEqual(self.read()['reason'],'partial-log-body')
        self.clear();self.add(body=prefix+' auto_compact_scope_tokens=1')
        self.assertEqual(self.read()['state'],'unknown')

    def test_old_or_untimed_usage_and_unseen_tail_remain_unknown(self):
        p = codex_compaction_pressure('exact-session-id', self.path, self.line,
                                     home=self.home, now=2_000)
        self.assertEqual(p['state'], 'unknown')
        self.line = json.dumps({'payload': {'type': 'token_count'}})
        self.assertEqual(self.read()['state'], 'unknown')
        with self.path.open('a') as handle: handle.write('x'*300000+'\n')
        self.assertEqual(self.read()['state'], 'unknown')


if __name__ == '__main__': unittest.main()
