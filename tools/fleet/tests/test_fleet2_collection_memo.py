"""Collection-local seat-ledger memo contract."""
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
UTILITIES = TOOLS.parent / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))

from fleet.collectors import codex, dispatch  # noqa: E402
import session_tidy  # noqa: E402
sys.path.append(str(Path(__file__).resolve().parents[3] / "utilities"))  # split_module_fixture
from split_module_fixture import patch_split  # noqa: E402


class Fleet2CollectionMemoTest(unittest.TestCase):
    def test_route_state_roots_are_shared_only_inside_one_pass_and_remain_owned(self):
        from fleet import route
        with mock.patch.object(route, '_read_dispatch_state_roots',
                               side_effect=lambda *args: ['first-root']) as reader:
            with dispatch.collection_read_scope():
                route._dispatch_state_roots('/home', '/jobs').append('caller-owned')
                self.assertEqual(route._dispatch_state_roots('/home', '/jobs'), ['first-root'])
                reader.assert_called_once_with('/home', '/jobs')
                self.assertEqual(route._dispatch_state_roots('/other', '/jobs'), ['first-root'])
                self.assertEqual(reader.call_count, 2)
            reader.side_effect = lambda *args: ['changed-root']
            with dispatch.collection_read_scope():
                self.assertEqual(route._dispatch_state_roots('/home', '/jobs'), ['changed-root'])
            self.assertEqual(reader.call_count, 3)
            route._dispatch_state_roots('/home', '/jobs')
            self.assertEqual(reader.call_count, 4)

    def test_route_state_roots_keep_standalone_fresh_reads_without_the_scope_helper(self):
        from fleet import route
        with mock.patch.object(route, '_read_dispatch_state_roots',
                               side_effect=lambda *args: ['standalone-root']) as reader, \
                mock.patch.dict(sys.modules, {'runtime_observation': None}):
            self.assertEqual(route._dispatch_state_roots('/home', '/jobs'), ['standalone-root'])
            self.assertEqual(route._dispatch_state_roots('/home', '/jobs'), ['standalone-root'])
        self.assertEqual(reader.call_count, 2)

    def test_optional_agents_are_observed_once_but_fresh_after_the_pass(self):
        from fleet.collectors import steward
        current = [None]
        reader = mock.Mock(side_effect=lambda: current[0])
        with dispatch.collection_read_scope():
            self.assertIsNone(steward._observed_agents(reader))
            current[0] = [{'agent': 'claude'}]
            self.assertIsNone(steward._observed_agents(reader))
            reader.assert_called_once()
        with dispatch.collection_read_scope():
            self.assertEqual(steward._observed_agents(reader), current[0])
        self.assertEqual(reader.call_count, 2)
        current[0] = []
        self.assertEqual(steward._observed_agents(reader), [])
        self.assertEqual(reader.call_count, 3)

    def test_native_role_metadata_is_shared_within_a_pass_and_fresh_next_pass(self):
        from fleet.collectors import steward, opencode
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory)/'state.sqlite'
            with sqlite3.connect(database) as con:
                con.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, cwd TEXT)')
            peer = SimpleNamespace(steward_evidence_targets=lambda marker: [])
            with mock.patch.object(steward, '_peer_message_module', return_value=peer), mock.patch.object(
                    codex, '_state_db', return_value=database) as discover, mock.patch.object(
                    opencode, '_db', side_effect=AssertionError('unselected harness database')):
                wanted = {('codex','new-session'): {}}
                with dispatch.collection_read_scope():
                    self.assertEqual(steward._native_role_sessions(wanted, []), [])
                    with sqlite3.connect(database) as con:
                        con.execute('INSERT INTO threads VALUES (?,?)', ('new-session','/repo'))
                    self.assertEqual(steward._native_role_sessions(wanted, []), [])
                    self.assertEqual(discover.call_count, 1)
                with dispatch.collection_read_scope():
                    self.assertEqual(steward._native_role_sessions(wanted, [])[0].cwd, '/repo')
                self.assertEqual(discover.call_count, 2)
                self.assertEqual(steward._native_role_sessions({('claude','other'): {}}, []), [])
                self.assertEqual(discover.call_count, 2)

    def test_display_and_end_proof_share_one_fresh_metadata_census(self):
        from fleet.collectors import steward
        from fleet import session_registry
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, '123.json').write_text(json.dumps({'sessionId': 'known', 'cwd': '/repo'}))
            with mock.patch.object(session_registry, '_dir_for', return_value=directory), mock.patch.object(
                    session_registry, 'read', wraps=session_registry.read) as reader:
                with dispatch.collection_read_scope():
                    steward._registry_sessions()
                    before = reader.call_count
                    self.assertEqual(steward.end_observation().registry('codex')['known']['cwd'], '/repo')
                    self.assertEqual(reader.call_count, before)
                Path(directory, '123.json').write_text(json.dumps({'sessionId': 'new', 'cwd': '/changed'}))
                with dispatch.collection_read_scope():
                    self.assertIn('new', steward.end_observation().registry('codex'))
                    self.assertGreater(reader.call_count, before)

    def test_fresh_raw_bytes_share_passive_parses_but_keep_strict_readers_fresh(self):
        cache = session_tidy.LedgerParseCache()
        first = b'{"sid":"one","event":"start"}\n'
        suffix = b'{"sid":"two","event":"start"}\n'
        with self._state_root(), mock.patch.object(
                session_tidy, '_parse_ledger_lines', wraps=session_tidy._parse_ledger_lines) as parse:
            with session_tidy.seat_ledger_read_scope(cache):
                row = session_tidy._read_ledger_lines(self.seat, raw=first)
                row[0]['sid'] = 'caller mutation'
                again = session_tidy._read_ledger_lines(self.seat, raw=first)
                self.assertEqual(again[0]['sid'], 'one')
                self.assertEqual(parse.call_count, 1)
                appended = session_tidy._read_ledger_lines(self.seat, raw=first + suffix)
                self.assertEqual(parse.call_args.args[0], suffix)
                self.assertEqual([entry['sid'] for entry in appended], ['one', 'two'])
                session_tidy._read_ledger_lines(self.seat, raw=first, strict=True)
                self.assertTrue(parse.call_args.kwargs['strict'])
            before = parse.call_count
            session_tidy._read_ledger_lines(self.seat, raw=first)
            session_tidy._read_ledger_lines(self.seat, raw=first)
            self.assertEqual(parse.call_count, before + 2)

    def test_steward_relation_output_reuse_observes_marker_and_ledger_changes(self):
        from fleet.collectors import steward
        import dispatch_seat_handover as handover
        marker_dir = self.root / 'peer-steward' / 'codex'
        marker_dir.mkdir(parents=True)
        marker_path = marker_dir / 'holder.json'
        marker = dict(session_id='holder', harness='codex', seat_key='fixture', generation='gen',
                      targets={'self': dict(source='explicit', kind='explicit', harness='unknown', name='-')})
        marker_path.write_text(json.dumps(marker))
        def markers(_roots):
            return {('codex', 'holder'): json.loads(marker_path.read_text())}
        pm = SimpleNamespace(steward_marker_roots=lambda: ([self.root], 'fixture'),
                             read_steward_markers=markers,
                             steward_evidence_targets=lambda value: list(value['targets'].values()))
        with patch_split(session_tidy, 'state_root', return_value=self.root), \
                mock.patch.object(steward, '_peer_message_module', return_value=pm), \
                mock.patch.object(handover, '_peer_message', return_value=pm):
            expected = steward.read_relations()
            self.assertEqual(len(expected), 1)
            with mock.patch.object(handover, 'resolved_steward_relations',
                                   wraps=handover.resolved_steward_relations) as read:
                with dispatch.collection_read_scope():
                    first = steward.read_relations()
                first[0]['holder']['sid'] = 'caller mutation'
                with dispatch.collection_read_scope():
                    self.assertEqual(steward.read_relations(), expected)
                self.assertEqual(read.call_count, 1)
                marker['generation'] = 'new-generation'
                marker_path.write_text(json.dumps(marker))
                with dispatch.collection_read_scope():
                    changed = steward.read_relations()
                self.assertEqual(changed[0]['generation'], 'new-generation')
                self.assertEqual(read.call_count, 2)
                ledger = self.root / 'sessions' / 'fixture.jsonl'
                ledger.parent.mkdir(exist_ok=True)
                ledger.write_text(json.dumps(dict(event='steward-release', sid='holder', harness='codex',
                                                   generation='new-generation', seat_key='fixture')) + '\n')
                with dispatch.collection_read_scope():
                    released = steward.read_relations()
                self.assertTrue(released[0]['released'])
                self.assertEqual(read.call_count, 3)
                self.assertEqual(released, steward.read_relations())
                self.assertEqual(read.call_count, 4)

    def test_passive_ledger_generation_skips_reads_and_observes_replace_and_symlink(self):
        cache = session_tidy.LedgerParseCache()
        with self._state_root(), mock.patch.object(
                session_tidy, 'read_bytes', wraps=session_tidy.read_bytes) as read:
            def scan():
                with session_tidy.seat_ledger_read_scope(cache):
                    return session_tidy._read_ledger_lines(self.seat)
            self.assertEqual(scan(), scan())
            self.assertEqual(read.call_count, 1)
            stamp = self.path.stat()
            replacement = self.path.with_suffix('.new')
            replacement.write_text(json.dumps({'sid': 'new'}) + '\n')
            os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            replacement.replace(self.path)
            self.assertEqual([row['sid'] for row in scan()], ['new'])
            self.assertEqual(read.call_count, 2)
            self.path.rename(replacement)
            self.path.symlink_to(replacement)
            self.assertFalse(scan().usable)
            self.assertEqual(read.call_count, 3)
            self.path.unlink()
            self.assertFalse(scan().usable)

    def test_passive_ledger_cache_reuses_bytes_and_append_but_returns_isolated_rows(self):
        cache = session_tidy.LedgerParseCache()
        with self._state_root(), mock.patch.object(session_tidy, '_parse_ledger_lines',
                                wraps=session_tidy._parse_ledger_lines) as parse:
            with session_tidy.seat_ledger_read_scope(cache):
                first = session_tidy._read_ledger_lines(self.seat)
            first[0]['details']['label'] = 'caller mutation'
            with session_tidy.seat_ledger_read_scope(cache):
                again = session_tidy._read_ledger_lines(self.seat)
            self.assertEqual(parse.call_count, 1)
            self.assertEqual(again[0]['details']['label'], 'original')
            suffix = json.dumps({'sid': 'second', 'event': 'start'}) + '\n'
            with self.path.open('a') as handle:
                handle.write(suffix)
            with session_tidy.seat_ledger_read_scope(cache):
                appended = session_tidy._read_ledger_lines(self.seat)
            self.assertEqual(parse.call_args.args[0], suffix.encode())
            self.assertEqual([row['sid'] for row in appended], ['first', 'second'])
            self.path.write_text(json.dumps({'sid': 'replacement'}) + '\n')
            with session_tidy.seat_ledger_read_scope(cache):
                replaced = session_tidy._read_ledger_lines(self.seat)
            self.assertEqual([row['sid'] for row in replaced], ['replacement'])
            with session_tidy.seat_ledger_read_scope(cache):
                session_tidy._read_ledger_lines(self.seat, strict=True)
            self.assertTrue(parse.call_args.kwargs.get('strict'))

    def test_row_state_root_resolution_is_shared_only_for_one_pass(self):
        job = SimpleNamespace(_launch_home='/home/fixture', _registry_path='/state/jobs.log')
        with mock.patch.object(dispatch, 'dispatch_state_roots',
                               side_effect=[(Path('/one'),), (Path('/two'),)]) as resolve:
            with dispatch.collection_read_scope():
                self.assertEqual(dispatch._row_state_roots(job), ('/one',))
                self.assertEqual(dispatch._row_state_roots(job), ('/one',))
            self.assertEqual(resolve.call_count, 1)
            with dispatch.collection_read_scope():
                self.assertEqual(dispatch._row_state_roots(job), ('/two',))
            self.assertEqual(resolve.call_count, 2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seat = SimpleNamespace(key="seat-a")
        self.path = self.root / "sessions" / "seat-a.jsonl"
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({"sid": "first", "event": "start",
                                         "details": {"label": "original"}}) + "\n")

    def _state_root(self):
        return patch_split(session_tidy, "state_root", return_value=self.root)

    def test_scope_reads_once_and_returns_defensive_nested_copies(self):
        with self._state_root(), mock.patch.object(
                session_tidy, "read_bytes", wraps=session_tidy.read_bytes) as read:
            with session_tidy.seat_ledger_read_scope():
                first = session_tidy._read_ledger_lines(self.seat)
                first[0]["details"]["label"] = "caller mutation"
                second = session_tidy._read_ledger_lines(self.seat)
                self.assertEqual(second[0]["details"]["label"], "original")
                with session_tidy.seat_ledger_read_scope():
                    nested = session_tidy._read_ledger_lines(self.seat)
                    self.assertEqual(nested[0]["details"]["label"], "original")
            self.assertEqual(read.call_count, 1)

    def test_readonly_scope_reuses_rows_without_deepcopy_and_writer_invalidates(self):
        with self._state_root(), mock.patch.object(
                session_tidy, "read_bytes", wraps=session_tidy.read_bytes) as read, \
                mock.patch.object(session_tidy.copy, "deepcopy",
                                  wraps=session_tidy.copy.deepcopy) as deepcopy:
            with session_tidy.seat_ledger_read_scope():
                with session_tidy.seat_ledger_readonly_scope():
                    first = session_tidy._read_ledger_lines(self.seat)
                    second = session_tidy._read_ledger_lines(self.seat)
                    self.assertIs(first, second)
                    self.assertEqual(deepcopy.call_count, 0)
                    session_tidy._append_ledger_entry(
                        self.seat, {"sid": "second", "event": "start"})
                    updated = session_tidy._read_ledger_lines(self.seat)
                    self.assertIsNot(first, updated)
                    self.assertEqual([row["sid"] for row in updated], ["first", "second"])
            self.assertGreaterEqual(read.call_count, 2)

    def test_append_invalidates_scope_and_direct_reads_remain_fresh(self):
        with self._state_root(), mock.patch.object(
                session_tidy, "read_bytes", wraps=session_tidy.read_bytes) as read:
            with session_tidy.seat_ledger_read_scope():
                before = session_tidy._read_ledger_lines(self.seat)
                session_tidy._append_ledger_entry(
                    self.seat, {"sid": "second", "event": "start"})
                after = session_tidy._read_ledger_lines(self.seat)
                self.assertEqual([row["sid"] for row in before], ["first"])
                self.assertEqual([row["sid"] for row in after], ["first", "second"])
            self.assertEqual(read.call_count, 2)

            direct_first = session_tidy._read_ledger_lines(self.seat)
            with self.path.open("a") as stream:
                stream.write(json.dumps({"sid": "third", "event": "start"}) + "\n")
            direct_second = session_tidy._read_ledger_lines(self.seat)
            self.assertEqual([row["sid"] for row in direct_first], ["first", "second"])
            self.assertEqual([row["sid"] for row in direct_second], ["first", "second", "third"])

    def test_parallel_scopes_do_not_share_mutable_memos(self):
        with self._state_root(), mock.patch.object(
                session_tidy, "read_bytes", wraps=session_tidy.read_bytes) as read:
            def collect():
                with session_tidy.seat_ledger_read_scope():
                    state = session_tidy._LEDGER_READ_SCOPE.get()
                    rows = session_tidy._read_ledger_lines(self.seat)
                    return state, rows

            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = list(pool.map(lambda _index: collect(), range(2)))
            self.assertIsNot(first[0], second[0])
            self.assertEqual(first[1], second[1])
            self.assertEqual(read.call_count, 2)

    def test_dispatch_collection_scope_composes_and_resets_read_scopes(self):
        import dispatch_supervision
        with self._state_root():
            with dispatch.collection_read_scope():
                seat_scope = session_tidy._LEDGER_READ_SCOPE.get()
                identity_scope = codex._DAEMON_IDENTITY_SCOPE.get()
                route_scope = dispatch_supervision._ROUTE_ATTENTION_READ.get()
                self.assertIsNotNone(seat_scope)
                self.assertIsNotNone(identity_scope)
                self.assertIsNotNone(route_scope)
                with dispatch.collection_read_scope():
                    self.assertIs(session_tidy._LEDGER_READ_SCOPE.get(), seat_scope)
                    self.assertIs(codex._DAEMON_IDENTITY_SCOPE.get(), identity_scope)
                    self.assertIs(dispatch_supervision._ROUTE_ATTENTION_READ.get(), route_scope)
            self.assertIsNone(session_tidy._LEDGER_READ_SCOPE.get())
            self.assertIsNone(codex._DAEMON_IDENTITY_SCOPE.get())
            self.assertIsNone(dispatch_supervision._ROUTE_ATTENTION_READ.get())

    def test_stall_lineage_reuses_one_graph_but_not_the_next_scan(self):
        import dispatch_seat_handover as handover
        import dispatch_supervision as supervision
        graphs = (object(), object())
        with self._state_root(), \
                mock.patch.object(handover, "_lineage_signature", return_value=("observed",)) as signature, \
                mock.patch.object(handover, "lineage_graph", side_effect=graphs) as read:
            with dispatch.collection_read_scope():
                context = supervision._ROUTE_ATTENTION_READ.get()
                for _ in range(100):
                    self.assertIs(handover._pass_lineage_graph(context), graphs[0])
            self.assertEqual(read.call_count, 1)
            self.assertEqual(signature.call_count, 1)
            with dispatch.collection_read_scope():
                self.assertIs(handover._pass_lineage_graph(supervision._ROUTE_ATTENTION_READ.get()), graphs[1])
            self.assertEqual(read.call_count, 2)

    def test_registry_malformed_diagnostics_reuse_append_prefix_count(self):
        path = self.root / "jobs.log"
        valid = "date\topen\t/repo\t/work\tslug\tcapability=code\n"
        path.write_text("\nmalformed\n" + valid)
        with dispatch._dispatch_registry_cache.registry_read_scope():
            first = dispatch.parsed_registry_rows(path)
        self.assertEqual([row[0] for row in first], [2])
        self.assertEqual(dispatch._registry_diagnostics(path), (None, 1))

        with path.open("a") as stream:
            stream.write("\nalso\tmalformed\n")
            stream.write("date\topen\t/repo\t/work\tslug2\tcapability=code\n")
        with dispatch._dispatch_registry_cache.registry_read_scope():
            appended = dispatch.parsed_registry_rows(path)
        self.assertEqual([row[0] for row in appended], [2, 5])
        self.assertEqual(dispatch._registry_diagnostics(path), (None, 2))

        dispatch._PARSED_REGISTRY_DIAGNOSTICS.clear()
        with path.open("a") as stream:
            stream.write("\nthird-malformed\n")
            stream.write("date\topen\t/repo\t/work\tslug3\tcapability=code\n")
        with dispatch._dispatch_registry_cache.registry_read_scope():
            after_diagnostic_eviction = dispatch.parsed_registry_rows(path)
        self.assertEqual([row[0] for row in after_diagnostic_eviction], [2, 5, 8])
        self.assertEqual(dispatch._registry_diagnostics(path), (None, 3))


if __name__ == "__main__":
    unittest.main()
