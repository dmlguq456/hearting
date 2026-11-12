"""Fast cells retain native identity and never advance a complete publication."""
import copy
import errno
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.fleet import native_refresh as fast
from tools.fleet.model import Session
from tools.fleet.refresh import LiveSnapshot
from tools.fleet.explorer.view_model import ExplorerViewModel


class NativeDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.native = Session('opencode', 1234, session_id='exact', proc_start='native-birth',
                              model='Old model', liveness='idle', context_policy={'state':'unknown'})
        self.snapshot = LiveSnapshot(sessions=[self.native])
        self.identity = ('opencode','exact','1234','native-birth')
        self.binding = dict(harness='opencode',sid='exact',native_pid=1234,native_start='native-birth',
                            host_pid=4321,host_start='host-birth',incarnation='a'*48,
                            socket=str(self.root/'control.sock'))
        self.starts = {1234:'native-birth',4321:'host-birth'}
        self.registry = self.root/'registry.json'
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(fast,'_attachment_index',return_value={self.identity:self.binding}).start()
        mock.patch.object(fast,'read_proc_start',side_effect=lambda pid:self.starts.get(pid)).start()
        mock.patch.object(fast.session_registry,'registry_path',return_value=str(self.registry)).start()
        mock.patch.object(fast.session_registry,'read',side_effect=lambda *a, **k:
                          json.loads(self.registry.read_text()) if self.registry.exists() else None).start()
        mock.patch.object(fast.opencode,'_db',return_value=str(self.root/'absent.db')).start()
        from tools.fleet import session_view
        mock.patch.object(session_view,'observe',return_value=({},[])).start()
        self.reader = fast.NativeDisplayReader()

    def write(self, percentage=37, **changes):
        value = dict(schema='hearting-native-display-v1',harness='opencode',sid='exact',
                     pid=1234,start='native-birth',host_pid=4321,host_start='host-birth',
                     incarnation='a'*48,values=dict(context_percent=percentage,model='Native model',effort='high'))
        value.update(changes)
        temporary=self.root/'next.json'
        temporary.write_text(json.dumps(value))
        temporary.chmod(0o600)
        temporary.replace(self.root/'native-display.json')

    def test_native_changes_update_without_mutating_or_recollecting_the_base(self):
        self.write()
        original=copy.deepcopy(self.native)
        first=self.reader.read(self.snapshot)
        self.assertEqual(first[0][1]['context'],37)
        self.assertEqual(first[0][1]['model'][0],'Native model')
        self.assertIs(self.reader.read(self.snapshot),first)
        self.write(71)
        self.assertEqual(self.reader.read(self.snapshot)[0][1]['context'],71)
        self.assertEqual(self.native,original)

    def test_wrong_session_or_incarnation_cannot_supply_another_value(self):
        self.write();self.reader.read(self.snapshot)
        self.write(91,sid='another')
        self.assertEqual(self.reader.read(self.snapshot),())
        self.write(91,incarnation='b'*48)
        self.assertEqual(self.reader.read(self.snapshot),())

    def test_native_and_host_births_stay_current_even_with_unchanged_files(self):
        self.write();self.reader.read(self.snapshot)
        self.starts[1234]='reused-pid'
        self.assertIsNone(self.reader.read(self.snapshot)[0][1]['context'])
        self.starts[1234]='native-birth'
        self.starts[4321]='reused-host'
        self.assertEqual(self.reader.read(self.snapshot),())

    def test_native_selection_change_invalidates_the_old_session(self):
        self.write();self.reader.read(self.snapshot)
        self.registry.write_text(json.dumps({'procStart':'native-birth','sessionId':'replacement'}))
        self.assertIsNone(self.reader.read(self.snapshot)[0][1]['context'])

    def test_attachment_change_cannot_reuse_values_from_its_previous_incarnation(self):
        self.write(); self.reader.read(self.snapshot)
        changed = dict(self.binding, incarnation='b'*48)
        with mock.patch.object(fast, '_attachment_index', return_value={self.identity:changed}):
            self.reader.invalidate()
            self.assertEqual(self.reader.read(self.snapshot), ())

    def test_symlink_or_malformed_feed_drops_only_the_fast_value(self):
        self.write();self.reader.read(self.snapshot)
        path=self.root/'native-display.json';path.unlink()
        target=self.root/'other';target.write_text('{}');path.symlink_to(target)
        self.assertEqual(self.reader.read(self.snapshot),())
        path.unlink();path.write_text('{')
        self.assertEqual(self.reader.read(self.snapshot),())

    def test_native_percentage_never_admits_or_replaces_an_applied_trigger(self):
        self.native.ctx_pct=23
        self.native.context_policy={'state':'applied','applied_trigger_tokens':100000}
        self.native.context_display_window_tokens=100000
        self.write(91)
        fields=self.reader.read(self.snapshot)[0][1]
        self.assertEqual(fields['context'],23)
        self.assertEqual(fields['window'],.1)
        self.assertEqual(self.native.context_policy['state'],'applied')

    def test_a_read_that_overlaps_a_replacement_keeps_the_last_value_when_the_base_is_unobserved(self):
        self.native.model = None
        self.write(41)
        shown = self.reader.read(self.snapshot)
        self.assertEqual(shown[0][1]['context'], 41)
        original, replaced = os.read, []
        def read(fd, size):
            data = original(fd, size)
            if size == 4097 and not replaced:
                self.write(41); replaced.append(True)
            return data
        self.write(41)  # a new generation, so this pass really opens the feed
        with mock.patch.object(fast.os, 'read', side_effect=read):
            overlapped = self.reader.read(self.snapshot)
        self.assertTrue(replaced)
        self.assertEqual(overlapped, shown)
        layer = fast.NativeDisplayLayer(); layer.update(overlapped)
        base = ExplorerViewModel.from_mapping(dict(
            identity=1, sessions=[dict(tag='73', harness='opencode', state='idle', context=None, model=None)],
            readiness={'ready': True}, source_records={'_detail_tags': {'opencode:exact': '73'},
            '_detail_bindings': {'opencode:exact': ('opencode', 'exact', '1234', 'native-birth', (), '', '')}}))
        self.assertEqual(layer.apply(base).sessions[0]['context'], 41)
        # The next settled read reflects a real new value.
        self.write(63)
        self.assertEqual(self.reader.read(self.snapshot)[0][1]['context'], 63)

    def test_a_failed_read_keeps_the_value_but_a_changed_identity_does_not(self):
        self.write(41); self.reader.read(self.snapshot)
        self.write(52)
        with mock.patch.object(fast.NativeDisplayReader, '_read', side_effect=OSError):
            self.assertEqual(self.reader.read(self.snapshot)[0][1]['context'], 41)
        self.write(52, sid='another')
        self.assertEqual(self.reader.read(self.snapshot), ())
        self.write(52); self.reader.read(self.snapshot)
        self.starts[1234] = 'reused-pid'
        with mock.patch.object(fast.NativeDisplayReader, '_read', side_effect=OSError):
            self.assertIsNone(self.reader.read(self.snapshot)[0][1]['context'])

    def test_a_kept_value_does_not_mark_its_unread_generation_as_read(self):
        self.write(47); self.assertEqual(self.reader.read(self.snapshot)[0][1]['context'], 47)
        self.write(63)  # a valid new generation; nothing writes the feed again
        original, calls = fast.NativeDisplayReader._read, []
        def fail_once(*args):
            calls.append(1)
            if len(calls) == 1:
                raise OSError('one read error')
            return original(*args)
        with mock.patch.object(fast.NativeDisplayReader, '_read', side_effect=fail_once):
            self.assertEqual(self.reader.read(self.snapshot)[0][1]['context'], 47)
            self.reader.invalidate()
            self.assertEqual(self.reader.read(self.snapshot)[0][1]['context'], 63)
            self.assertEqual(len(calls), 2)
            # Once that generation was read, unchanged inputs read nothing more.
            self.reader.invalidate(); self.reader.read(self.snapshot)
            self.assertEqual(len(calls), 2)
        # Errors that keep recurring keep the value without blanking it.
        self.write(71)
        with mock.patch.object(fast.NativeDisplayReader, '_read', side_effect=OSError):
            for _ in range(3):
                self.assertEqual(self.reader.read(self.snapshot)[0][1]['context'], 63)
        self.assertEqual(self.reader.read(self.snapshot)[0][1]['context'], 71)

    def test_a_read_that_overlapped_a_replacement_reads_the_settled_generation_next(self):
        self.write(47); self.reader.read(self.snapshot)
        original, replaced = os.read, []
        def read(fd, size):
            data = original(fd, size)
            if size == 4097 and not replaced:
                self.write(63); replaced.append(True)
            return data
        self.write(52)
        with mock.patch.object(fast.os, 'read', side_effect=read):
            self.assertEqual(self.reader.read(self.snapshot)[0][1]['context'], 47)
        self.assertTrue(replaced)
        self.assertEqual(self.reader.read(self.snapshot)[0][1]['context'], 63)

    def test_a_source_read_failure_keeps_and_retries_wherever_it_is_caught(self):
        # Failure location: raised out of _read, or met inside the feed helper.
        for where in ('outside-read', 'inside-feed'):
            with self.subTest(where=where):
                reader = fast.NativeDisplayReader()
                self.write(47); self.assertEqual(reader.read(self.snapshot)[0][1]['context'], 47)
                self.write(59)  # a valid new generation; nothing writes the feed again
                parse, read_bytes, calls, failures = fast.NativeDisplayReader._read, os.read, [], []
                def counted(*args):
                    calls.append(1)
                    if where == 'outside-read' and len(failures) < self.failing:
                        failures.append(1); raise OSError('read error')
                    return parse(*args)
                def feed_read(fd, size):
                    if where == 'inside-feed' and size == 4097 and len(failures) < self.failing:
                        failures.append(1); raise OSError(errno.EIO, 'feed read error')
                    return read_bytes(fd, size)
                with mock.patch.object(fast.NativeDisplayReader, '_read', side_effect=counted), \
                        mock.patch.object(fast.os, 'read', side_effect=feed_read):
                    self.failing = 1
                    self.assertEqual(reader.read(self.snapshot)[0][1]['context'], 47)
                    reader.invalidate()
                    self.assertEqual(reader.read(self.snapshot)[0][1]['context'], 59)
                    reader.invalidate(); reader.read(self.snapshot)
                    self.assertEqual(len(calls), 2)
                    # While failures recur, each request reads once and keeps the value.
                    self.write(71); failures.clear(); self.failing = 3
                    for _ in range(3):
                        self.assertEqual(reader.read(self.snapshot)[0][1]['context'], 59)
                    self.assertEqual(len(calls), 5)
                    self.assertEqual(reader.read(self.snapshot)[0][1]['context'], 71)

    def test_an_unread_attachment_index_is_read_again_by_the_next_request(self):
        self.write(47)
        calls = []
        def index():
            calls.append(1)
            if len(calls) == 1:
                raise OSError(errno.EIO, 'index read error')
            return {self.identity: self.binding}
        reader = fast.NativeDisplayReader()
        with mock.patch.object(fast, '_attachment_index', side_effect=index):
            self.assertEqual(reader.read(self.snapshot), ())
            self.assertEqual(reader.read(self.snapshot)[0][1]['context'], 47)
            reader.read(self.snapshot)
        self.assertEqual(len(calls), 2)

    def test_absent_refused_or_explicitly_empty_feeds_still_clear(self):
        feed = self.root/'native-display.json'
        cases = (('absent', feed.unlink, ()),
                 ('group-writable', lambda: feed.chmod(0o620), ()),
                 ('other-session', lambda: self.write(91, sid='another'), ()),
                 ('other-incarnation', lambda: self.write(91, incarnation='b'*48), ()))
        for name, change, expected in cases:
            with self.subTest(case=name):
                reader = fast.NativeDisplayReader()
                self.write(47); self.assertEqual(reader.read(self.snapshot)[0][1]['context'], 47)
                change()
                parse, calls = fast.NativeDisplayReader._read, []
                with mock.patch.object(fast.NativeDisplayReader, '_read',
                                       side_effect=lambda *a: calls.append(1) or parse(*a)):
                    self.assertEqual(reader.read(self.snapshot), expected)
                    reader.invalidate(); reader.read(self.snapshot)
                self.assertEqual(len(calls), 1)  # settled: not read again
        reader = fast.NativeDisplayReader()
        self.write(47); reader.read(self.snapshot)
        self.write(None)
        self.assertIsNone(reader.read(self.snapshot)[0][1]['context'])

    def test_departed_rows_do_not_accumulate_in_the_display_reader(self):
        self.write();self.reader.read(self.snapshot)
        self.assertEqual(self.reader.read(LiveSnapshot()),())
        self.assertEqual(self.reader._entries,{})


class SourceReadBoundaryTests(unittest.TestCase):
    """Each source _read uses: not read raises, absent or refused reads None."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.io_error = OSError(errno.EIO, 'injected read error')

    def assert_boundary(self, call, failing, absent):
        """call(raise_unread) with an I/O failure, then on an absent source."""
        with failing():
            with self.assertRaises(OSError):
                call(True)
            self.assertIn(call(False), (None, (None, None)))
        with absent():
            self.assertIn(call(True), (None, (None, None)))

    def test_registry_record(self):
        from tools.fleet import session_registry
        path = self.root/'1234.json'
        path.write_text(json.dumps({'sessionId': 'exact'})); path.chmod(0o600)
        target = mock.patch.object(session_registry, 'registry_path', return_value=str(path))
        with target:
            self.assertEqual(session_registry.read('codex', 1234, raise_unread=True)['sessionId'], 'exact')
            self.assert_boundary(
                lambda strict: session_registry.read('codex', 1234, raise_unread=strict),
                lambda: mock.patch.object(session_registry, 'open', create=True, side_effect=self.io_error),
                lambda: mock.patch.object(session_registry, 'registry_path', return_value=str(self.root/'absent')))

    def test_claude_display_tap(self):
        from tools.fleet import native_display
        tap = self.root/'exact.json'
        tap.write_text(json.dumps({'session_id': 'exact'})); tap.chmod(0o600)
        self.assertEqual(native_display.claude_callback(tap, 'exact', 1, 's', raise_unread=True)['session_id'], 'exact')
        self.assert_boundary(
            lambda strict: native_display.claude_callback(tap, 'exact', 1, 's', raise_unread=strict),
            lambda: mock.patch.object(native_display.os, 'read', side_effect=self.io_error),
            lambda: mock.patch.object(native_display.os, 'open', side_effect=FileNotFoundError(errno.ENOENT, 'absent')))
        link = self.root/'link.json'; link.symlink_to(tap)  # refused, not unread
        self.assertIsNone(native_display.claude_callback(link, 'exact', 1, 's', raise_unread=True))

    def test_codex_rollout_model_and_token_count(self):
        from tools.fleet.collectors import codex
        rollout = self.root/'rollout.jsonl'
        rollout.write_text('{"type":"event_msg","payload":{"type":"token_count"}}\n')
        for helper in (codex._rollout_model_effort, codex._tail_token_count):
            with self.subTest(helper=helper.__name__):
                codex._TURN_CONTEXT_CACHE.clear()
                self.assert_boundary(
                    lambda strict: helper(str(rollout), raise_unread=strict),
                    lambda: mock.patch.object(codex, 'open', create=True, side_effect=self.io_error),
                    lambda: mock.patch.object(codex.os, 'stat', side_effect=FileNotFoundError(errno.ENOENT, 'absent'))
                    if helper is codex._rollout_model_effort else
                    mock.patch.object(codex.os.path, 'getsize', side_effect=FileNotFoundError(errno.ENOENT, 'absent')))

    def test_attachment_directory_and_record(self):
        import importlib, sys
        sys.path.insert(0, str(Path(fast.__file__).resolve().parents[2] / 'utilities'))
        session_shell = importlib.import_module('session_shell')
        (self.root/'opencode').mkdir()
        record = self.root/'opencode'/'session-shell-4321.json'
        record.write_text('{}'); record.chmod(0o600)
        with mock.patch.object(session_shell, 'registry_root', return_value=self.root):
            self.assertEqual(session_shell.bindings(harness='opencode', raise_unread=True), [])
            for failing in (lambda: mock.patch.object(session_shell.os, 'listdir', side_effect=self.io_error),
                            lambda: mock.patch.object(session_shell, '_private_json', side_effect=self.io_error)):
                with failing():
                    with self.assertRaises(OSError):
                        session_shell.bindings(harness='opencode', raise_unread=True)
            # Without the flag, other callers keep their existing result.
            with mock.patch.object(session_shell, '_private_json', side_effect=self.io_error):
                self.assertEqual(session_shell.bindings(harness='opencode'), [])
            record.unlink(); (self.root/'opencode').rmdir()
            self.assertEqual(session_shell.bindings(harness='opencode', raise_unread=True), [])


class OpenCodeDatabaseReadTests(unittest.TestCase):
    """OpenCode without a native feed: a failed SQLite read keeps and retries (the #1518 rule).

    Review RV-PR1518-R3 reproduced it on a private V1 database: a model read, then one
    OperationalError, left the cell empty until the DB or WAL changed again."""

    def setUp(self):
        import sqlite3, sys
        tools = str(Path(fast.__file__).resolve().parents[1])  # the collector imports `fleet.*`
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from tools.fleet import session_view
        from tools.fleet.collectors import opencode
        self.sqlite3, self.session_view, self.opencode = sqlite3, session_view, opencode
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.child = subprocess.Popen(['sleep', '60'], cwd=self.root)
        self.addCleanup(self.child.wait)
        self.addCleanup(self.child.kill)
        self.pid = self.child.pid
        self.start = Path(f'/proc/{self.pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        self.sid = 'ses_fixture_db_read'
        self.db = self.root/'opencode.db'
        con = sqlite3.connect(self.db)
        con.execute('CREATE TABLE session (id TEXT,slug TEXT,agent TEXT,model TEXT,cost REAL,tokens_input INTEGER,'
                    'tokens_output INTEGER,tokens_reasoning INTEGER,time_updated INTEGER,parent_id TEXT,directory TEXT)')
        con.execute('INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (self.sid, 'fixture', 'default', json.dumps({'id': 'DB model', 'variant': 'high'}),
                     0, 100, 20, 0, 1000, None, str(self.root)))
        con.commit(); con.close()
        self.snapshot = LiveSnapshot(sessions=[Session('opencode', self.pid, session_id=self.sid, proc_start=self.start,
                                                       cwd=str(self.root), liveness='idle', model=None)])
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(fast, '_attachment_index', return_value={}).start()
        mock.patch.object(fast.session_registry, 'read', return_value=None).start()
        mock.patch.object(fast.session_registry, 'registry_path', return_value=str(self.root/'registry.json')).start()
        mock.patch.object(opencode, '_db', return_value=str(self.db)).start()
        mock.patch.object(opencode, '_resolve_current_identity', return_value=self.sid).start()
        mock.patch.object(opencode, '_enrich_v2', side_effect=lambda *a, **k: False).start()

    def set_model(self, name):
        con = self.sqlite3.connect(self.db)
        con.execute('UPDATE session SET model=?', (json.dumps({'id': name, 'variant': 'high'}),))
        con.commit(); con.close()

    def locked(self):
        return mock.patch.object(self.sqlite3, 'connect', side_effect=self.sqlite3.OperationalError('database is locked'))

    @staticmethod
    def model(values):
        cell = values[0][1].get('model') if values else None
        return cell[0] if cell else None

    def test_a_failed_database_read_keeps_the_display_and_the_next_request_reads_again(self):
        reader = fast.NativeDisplayReader()
        self.assertEqual(self.model(reader.read(self.snapshot)), 'DB model')
        self.set_model('New DB model')
        with self.locked():
            self.assertEqual(self.model(reader.read(self.snapshot)), 'DB model')
        # The database and WAL did not change again; the kept value is not the read generation.
        self.assertEqual(self.model(reader.read(self.snapshot)), 'New DB model')

    def test_the_panel_observation_keeps_its_cells_only_with_a_previous_one(self):
        first, _paths = self.session_view.observe('opencode', self.pid, self.start, self.sid)
        self.assertEqual(first['model'], 'DB model')
        with self.locked():
            kept, _paths = self.session_view.observe('opencode', self.pid, self.start, self.sid, previous=first)
            plain, _paths = self.session_view.observe('opencode', self.pid, self.start, self.sid)
            with self.assertRaises(OSError):
                self.session_view.observe('opencode', self.pid, self.start, self.sid, raise_unread=True)
        self.assertEqual((kept['model'], kept['effort']), ('DB model', first['effort']))
        self.assertIsNone(plain['model'])   # nothing is made up without a previous observation

    def test_a_refused_or_absent_database_still_clears(self):
        reader = fast.NativeDisplayReader()
        self.assertEqual(self.model(reader.read(self.snapshot)), 'DB model')
        self.set_model('New DB model')   # a new generation the reader has to read
        with self.locked(), mock.patch.object(self.opencode.os, 'access', return_value=False):
            self.assertIsNone(self.model(reader.read(self.snapshot)))
        reader = fast.NativeDisplayReader()
        self.assertEqual(self.model(reader.read(self.snapshot)), 'New DB model')
        self.db.unlink()
        self.assertIsNone(self.model(reader.read(self.snapshot)))

    def test_a_v2_database_read_failure_is_reported_as_unread(self):
        runtime = self.opencode._opencode_v2_runtime()
        mock.patch.object(self.opencode, '_enrich_v2', new=type(self).real_enrich_v2).start()
        with mock.patch.object(runtime, 'credential_store_present', return_value=True), \
             mock.patch.object(runtime, 'read_session_observation',
                               return_value={'state': 'unknown', 'reason': 'database-read-failed'}):
            session = Session('opencode', self.pid, session_id=self.sid, proc_start=self.start)
            with self.assertRaises(OSError):
                self.opencode.enrich(session, raise_unread=True)
            self.opencode.enrich(Session('opencode', self.pid, session_id=self.sid, proc_start=self.start))

    from tools.fleet.collectors.opencode import _enrich_v2 as real_enrich_v2


class NativeLayerTests(unittest.TestCase):
    def model(self, pid='1234'):
        return ExplorerViewModel.from_mapping(dict(
            identity=1,sessions=[dict(tag='73',harness='claude',state='idle',context=10,model=['Old','fam_other'])],
            readiness={'ready':True},source_records={'_detail_tags':{'claude:exact':'73'},
            '_detail_bindings':{'claude:exact':('claude','exact',pid,'birth',(),'', '')}}))

    def test_only_cells_change_and_the_published_model_and_bindings_stay_immutable(self):
        base=self.model();layer=fast.NativeDisplayLayer()
        layer.update(((('claude','exact','1234','birth'),{'context':37}),))
        shown=layer.apply(base)
        self.assertEqual(shown.sessions[0]['context'],37)
        self.assertEqual(base.sessions[0]['context'],10)
        self.assertIs(shown.readiness,base.readiness)
        self.assertIs(shown.source_records,base.source_records)
        self.assertEqual(shown.identity,base.identity)
        self.assertIs(layer.apply(base),shown)

    def test_pending_replay_or_changed_binding_cannot_borrow_the_live_value(self):
        layer=fast.NativeDisplayLayer()
        layer.update(((('claude','exact','1234','birth'),{'context':37}),))
        changed=self.model(pid='9999')
        self.assertIs(layer.apply(changed),changed)
        replay=self.model().with_updates(source_records={})
        self.assertIs(layer.apply(replay),replay)

    def test_a_late_fast_read_cannot_replace_cells_changed_by_a_newer_base(self):
        layer=fast.NativeDisplayLayer()
        layer.update(((('claude','exact','1234','birth'),
                       {'context':37,'_base':{'context':10}}),))
        base=self.model()
        newer=base.with_updates(sessions=[dict(base.sessions[0],context=81)])
        self.assertEqual(layer.apply(newer).sessions[0]['context'],81)

    def test_clock_and_usage_updates_reuse_rows_and_keep_the_new_metadata(self):
        base=self.model();layer=fast.NativeDisplayLayer()
        layer.update(((('claude','exact','1234','birth'),{'context':37}),))
        first=layer.apply(base)
        current=base.with_updates(clock=2,usage=[{'text':'fresh quota'}],readiness={'ready':False})
        shown=layer.apply(current)
        self.assertIs(shown.sessions,first.sessions)
        self.assertEqual(shown.usage,current.usage)
        self.assertEqual(shown.readiness,current.readiness)
        self.assertEqual(shown.clock,2)
        layer.update(((('claude','exact','1234','birth'),{'context':71}),))
        self.assertEqual(layer.apply(current).sessions[0]['context'],71)

    def test_cached_rows_cannot_follow_changed_bindings_with_the_same_sessions(self):
        base=self.model();layer=fast.NativeDisplayLayer()
        layer.update(((('claude','exact','1234','birth'),{'context':37}),))
        layer.apply(base)
        replaced=base.with_updates(source_records=self.model(pid='9999').source_records)
        self.assertIs(replaced.sessions,base.sessions)
        self.assertEqual(layer.apply(replaced).sessions[0]['context'],10)


class NativePublisherTests(unittest.TestCase):
    def test_latest_observation_wins_and_only_display_fields_are_shared(self):
        root=Path(__file__).resolve().parents[3]
        code=r'''
const assert=require('node:assert/strict'),fs=require('node:fs/promises');
const display=require(process.argv[1]),directory=process.argv[2];
const identity={harness:'codex',sid:'exact',native_pid:123,native_start:'n',host_pid:456,host_start:'h',incarnation:'a'.repeat(48),token:'PRIVATE'};
const publish=display.publisher(directory,()=>identity);
(async()=>{
 await publish({context_percent:10,model:'A',body:'not a display field'});
 const b=publish({context_percent:20,model:'B'});
 const a=publish({context_percent:10,model:'A'});
 await Promise.all([b,a]);
 let row=JSON.parse(await fs.readFile(directory+'/native-display.json','utf8'));
 assert.equal(row.values.context_percent,10);assert.equal(row.values.model,'A');
 assert.equal(row.token,undefined);assert.equal(row.values.body,undefined);
 identity.sid='replacement';await publish({context_percent:0,model:'New'});
 row=JSON.parse(await fs.readFile(directory+'/native-display.json','utf8'));
 assert.equal(row.sid,'replacement');assert.equal(row.values.context_percent,0);
 console.log('PASS');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run(['node','-e',code,str(root/'utilities/session-shell/native-display.cjs'),directory],
                                  text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('PASS',result.stdout)


if __name__=='__main__':unittest.main()
