"""First-screen detail readiness is explicit and bound to the current basic pass."""

import copy
import unittest
import tempfile
from pathlib import Path
from unittest import mock

from tools.fleet import details, projection, route_chain
from tools.fleet.collectors import _usage_cache_observation
from tools.fleet.model import DispatchJob, Session, WorkProjection
from tools.fleet.projection import required_projection_details_complete
from tools.fleet.refresh import (
    LiveSnapshot,
    required_details_all_new,
    required_details_complete,
    set_required_details,
)


def _basic(identity):
    return LiveSnapshot(observations={
        "basic": {"state": "ok", "generation_identity": identity,
                  "observed_at": 100.0, "all_new": True},
        "required-details": {"state": "pending", "generation_identity": identity,
                              "all_new": False},
    })


class DetailInputReuseTests(unittest.TestCase):
    def test_herdr_telemetry_is_not_tag_metadata_but_tag_and_identity_changes_are(self):
        from tools.fleet import session_tags
        source = _basic('round-1')
        source.tag_metadata = [{'agent': 'claude', 'agent_session': {'kind': 'id', 'value': 's1'},
                                'pane_id': 'w:p1', 'display_agent': '[aa] Claude',
                                'last_seen': 1, 'cpu': 80}]
        cache = details.DetailReuse(optional=True)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'tag-assignments.json'
            path.write_text('{}')
            with mock.patch.object(session_tags, '_path', return_value=path):
                before = cache.input_key(source)
                source.tag_metadata[0].update(last_seen=2, cpu=1)
                self.assertEqual(cache.input_key(source), before)
                source.tag_metadata[0]['display_agent'] = '[bb] Claude'
                self.assertNotEqual(cache.input_key(source), before)
                source.tag_metadata[0]['display_agent'] = '[aa] Claude'
                path.write_text('{"updated":true}')
                self.assertNotEqual(cache.input_key(source), before)

    def test_optional_reuse_rejoins_current_child_context_subagents_and_title_time(self):
        from runtime_observation import InputGenerations
        from types import SimpleNamespace
        from tools.fleet.collectors import _adopt_child_titles, peer_messages, steward
        source = _basic('round-1')
        child = Session(harness='claude', session_id='child', pid=17, proc_start='100',
                        cwd='/repo', is_child=True, title='Child title', summary='Child NOW',
                        summary_ts=10, ctx_pct=30, subagents=[{'id': 'first'}])
        job = DispatchJob(key='code', harness='claude', pid=17, proc_start='100', cwd='/repo')
        source.sessions, source.jobs = [child], [job]
        set_required_details(source, 'ok', all_new=True)
        value = copy.deepcopy(source)
        _adopt_child_titles(value.sessions, value.jobs)
        value.jobs[0].title_ts = 123
        value.jobs[0]._detail_home_admitted = True
        value.observations['optional:steward'] = {'state': 'success'}
        cache = details.DetailReuse(optional=True)
        with mock.patch.object(steward, 'last_supervision', return_value={}):
            cache.remember(source, details.DetailSnapshot(details.state_key(source), value),
                           InputGenerations(), SimpleNamespace(agents=[], check=lambda _h: ()),
                           optional=True)
        fresh = copy.deepcopy(source)
        fresh.sessions[0].ctx_pct = 50
        fresh.sessions[0].subagents = [{'id': 'second'}]
        with mock.patch.object(peer_messages, 'collect', return_value=(
                {'records': [], 'by_session': {}}, SimpleNamespace(state='success'))):
            reused = cache.reuse(fresh, ())
        self.assertIsNotNone(reused)
        row = reused.snapshot.jobs[0]
        self.assertEqual((row.title, row.title_ts, row.summary), ('Child title', 123, 'Child NOW'))
        self.assertEqual(row.ctx_pct, 50)
        self.assertEqual(row.subagents, [{'id': 'second'}])
        self.assertIsNone(fresh.jobs[0].title)

    def test_optional_reuse_keeps_human_result_names_and_current_required_time_and_stage(self):
        from runtime_observation import InputGenerations
        from types import SimpleNamespace
        from tools.fleet.collectors import peer_messages, steward
        source = _basic('round-1')
        result = {'route_id': 'rt-done', 'artifact_root': '/repo/.agent_reports',
                  'name': 'raw-slug', 'at': 10}
        row = Session(harness='claude', session_id='holder', pid=17, proc_start='100')
        row.work_projection = WorkProjection(source='none', stage_label='execute', result=result)
        node = {'route_id': 'rt-done', 'result': dict(result)}
        row.route_chain = {'nodes': [node], 'current': node}
        source.sessions = [row]
        set_required_details(source, 'ok', all_new=True)
        value = copy.deepcopy(source)
        value.sessions[0].work_projection.result['name'] = 'Human cycle title'
        value.sessions[0].route_chain['nodes'][0]['result']['name'] = 'Human cycle title'
        value.observations['optional:steward'] = {'state': 'success'}
        cache = details.DetailReuse(optional=True)
        observation = SimpleNamespace(agents=[], check=lambda _holders: ())
        with mock.patch.object(steward, 'last_supervision', return_value={}):
            cache.remember(source, details.DetailSnapshot(details.state_key(source), value),
                           InputGenerations(), observation, optional=True)
        fresh = copy.deepcopy(source)
        fresh.observations = _basic('round-2').observations
        set_required_details(fresh, 'ok', all_new=True)
        fresh.sessions[0].work_projection = WorkProjection(
            source='none', stage_label='verify', result={**result, 'at': 20})
        fresh.sessions[0].route_chain['nodes'][0]['result']['at'] = 20
        with mock.patch.object(peer_messages, 'collect', return_value=(
                {'records': [], 'by_session': {}}, SimpleNamespace(state='success'))):
            reused = cache.reuse(fresh, ())
        self.assertIsNotNone(reused)
        row = reused.snapshot.sessions[0]
        self.assertEqual(row.work_projection.stage_label, 'verify')
        self.assertEqual(row.work_projection.result['at'], 20)
        self.assertEqual(row.work_projection.result['name'], 'Human cycle title')
        self.assertEqual(row.route_chain['current']['result']['at'], 20)
        self.assertEqual(row.route_chain['current']['result']['name'], 'Human cycle title')
        self.assertEqual(fresh.sessions[0].work_projection.result['name'], 'raw-slug')
        omitted = copy.deepcopy(fresh)
        omitted.sessions[0].work_projection = WorkProjection(source='none', stage_label='verify')
        omitted.sessions[0].route_chain['nodes'][0]['result'] = None
        set_required_details(omitted, 'continued', all_new=False, continuation_validated=True)
        with mock.patch.object(peer_messages, 'collect', return_value=(
                {'records': [], 'by_session': {}}, SimpleNamespace(state='success'))):
            continued = cache.reuse(omitted, ())
        self.assertIsNotNone(continued)
        self.assertIsNone(continued.snapshot.sessions[0].work_projection.result)
        self.assertEqual(continued.snapshot.observations['required-details']['state'], 'continued')
        new_target = copy.deepcopy(fresh)
        new_target.sessions[0].work_projection.result['route_id'] = 'rt-new'
        self.assertIsNone(cache.reuse(new_target, ()))
        set_required_details(fresh, 'partial', all_new=False)
        self.assertIsNone(cache.reuse(fresh, ()), 'optional reuse masked a required read failure')

    def test_a_resumed_required_pass_does_not_admit_unobserved_file_dependencies(self):
        from runtime_observation import InputGenerations
        from types import SimpleNamespace
        source = _basic('round-1')
        set_required_details(source, 'ok', all_new=False)
        cache = details.DetailReuse()
        cache.remember(source, details.DetailSnapshot(details.state_key(source), source),
                       InputGenerations(), SimpleNamespace(check=lambda _holders: ()))
        self.assertIsNone(cache.latest)
    def test_touching_an_existing_spec_marker_invalidates_the_detail_input(self):
        import os
        from runtime_observation import InputGenerations
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'marker'
            path.write_text('metadata marker')
            observed = InputGenerations()
            with observed.capture():
                projection._spec_marker_index(root)
            self.assertTrue(observed.current())
            before = path.stat()
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            self.assertFalse(observed.current())
    def test_registry_metadata_clock_change_reuses_roles_but_cwd_and_name_changes_do_not(self):
        import json
        from runtime_observation import InputGenerations
        from tools.fleet import session_registry
        from tools.fleet.collectors import dispatch, steward
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for harness in ('claude', 'codex', 'opencode'):
                (root / harness).mkdir()
            path = root / 'claude' / '17.json'
            record = {'pid': 17, 'sessionId': 'holder', 'cwd': '/repo', 'name': 'steward',
                      'updatedAt': 1, 'statusUpdatedAt': 1}
            path.write_text(json.dumps(record))
            with mock.patch.object(session_registry, '_dir_for', side_effect=lambda h, **_kwargs: root / h):
                observed = InputGenerations()
                with dispatch.collection_read_scope(), observed.capture():
                    self.assertEqual(steward._registry_sessions()[0].cwd, '/repo')
                record.update(updatedAt=2, statusUpdatedAt=2)
                path.write_text(json.dumps(record))
                self.assertTrue(observed.current())
                record['cwd'] = '/other-repository'
                path.write_text(json.dumps(record))
                self.assertFalse(observed.current())
                observed = InputGenerations()
                with dispatch.collection_read_scope(), observed.capture():
                    steward._registry_sessions()
                record['name'] = 'new steward'
                path.write_text(json.dumps(record))
                self.assertFalse(observed.current())

    def test_native_database_unrelated_update_reuses_roles_but_holder_cwd_change_does_not(self):
        import sqlite3
        from types import SimpleNamespace
        from runtime_observation import InputGenerations
        from tools.fleet.collectors import codex, dispatch, opencode, steward
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / 'state.sqlite'
            with sqlite3.connect(database) as db:
                db.execute('create table threads (id, cwd, updated_at)')
                db.execute('insert into threads values (?, ?, ?)', ('holder', '/repo', 1))
            observed = InputGenerations()
            module = SimpleNamespace(steward_evidence_targets=lambda _marker: ())
            with mock.patch.object(codex, '_state_db', return_value=str(database)), \
                 mock.patch.object(opencode, '_db', return_value=None), \
                 mock.patch.object(steward, '_peer_message_module', return_value=module):
                with dispatch.collection_read_scope(), observed.capture():
                    rows = steward._native_role_sessions({('codex', 'holder'): {}}, [])
                    self.assertEqual(rows[0].cwd, '/repo')
                with sqlite3.connect(database) as db:
                    db.execute('update threads set updated_at=2')
                self.assertTrue(observed.current())
                with sqlite3.connect(database) as db:
                    db.execute("update threads set cwd='/new-repository'")
                self.assertFalse(observed.current())

    def test_cache_drops_old_census_and_rejects_an_oversized_owned_projection(self):
        from runtime_observation import InputGenerations
        from types import SimpleNamespace
        source = _basic('round-1')
        source.sessions = [Session(harness='claude', session_id='holder', pid=17, proc_start='100')]
        source.sessions[0]._proc_tree = {'large old host observation': 'x' * 10000}
        source.node_evidence = {'rt': {'node': {'status': 'done'}}}
        value = copy.deepcopy(source)
        set_required_details(value, 'ok', all_new=True)
        cache = details.DetailReuse()
        cache.remember(source, details.DetailSnapshot(details.state_key(source), value),
                       InputGenerations(), SimpleNamespace(check=lambda _holders: ()))
        self.assertIsNotNone(cache.latest)
        self.assertEqual(cache.latest.snapshot.node_evidence, {})
        self.assertFalse(hasattr(cache.latest.snapshot.sessions[0], '_proc_tree'))
        self.assertTrue(hasattr(value.sessions[0], '_proc_tree'))
        cache.max_result_bytes = 1
        cache.remember(source, details.DetailSnapshot(details.state_key(source), value),
                       InputGenerations(), SimpleNamespace(check=lambda _holders: ()))
        self.assertIsNone(cache.latest)
        self.assertEqual(cache.rejections, 1)

    def test_loop_reuses_unchanged_round_refreshes_next_changed_round_and_selection(self):
        from contextlib import ExitStack
        from types import SimpleNamespace
        from tools.fleet import render
        from tools.fleet.collectors import herdr, peer_messages, steward
        from tools.fleet.explorer.state import NavState
        control = {'round': 0, 'phase': 'first', 'due': False, 'quit': False}
        fills, paints = [], []

        class Runtime:
            has_complete = False
            cached_continuation = None
            def configure_persistence(self, *_args, **_kwargs): pass
            def load_last_complete(self, *_args): return None
            def clear_cached_continuation(self): return None
            def collection_options(self, **_kwargs): return {}
            def publication_ready(self, readiness): return readiness.get('ready', False)
            def wait_for_writes(self, **_kwargs): pass
        runtime = Runtime()

        class Pump:
            def __init__(self, producer, interval, **kwargs):
                self.producer, self.name = producer, kwargs.get('name', 'fleet-refresh')
                self.generation, self.running, self.value = 0, False, None
                if self.name == 'fleet-refresh':
                    self_interval.append((interval, kwargs.get('adaptive_cadence')))
            def start(self): return self.request(force=True)
            def request(self, **_kwargs):
                self.value = self.producer()
                self.generation += 1
                return True
            def request_due(self, **_kwargs):
                if self.name == 'fleet-refresh' and control['due']:
                    control['due'] = False
                    return self.request()
                return False
            def poll(self, after=0):
                return (self.generation, self.value) if self.generation > after else None
            def health(self, *_args, **_kwargs): return {}
            def stop(self, **_kwargs): pass
        self_interval = []

        class Screen:
            def getmaxyx(self): return 84, 410
            def timeout(self, _value): pass
            def keypad(self, _value): pass
        class Reader:
            def __init__(self, *_args, **_kwargs): self.calls = 0
            def read(self, *_args, **_kwargs):
                self.calls += 1
                return (ord('q') if control['quit'] or self.calls > 40 else -1), None
            def has_buffered_input(self): return False

        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            path = Path(root) / 'title'
            path.write_text('first title')
            def collector(**_kwargs):
                control['round'] += 1
                source = _basic(str(control['round']))
                source.sessions = [Session(harness='claude', pid=17, proc_start='100',
                    session_id='sid', session_tag='aa', cwd='/repo', liveness='idle',
                    exec_child={'pid': 20, 'etime_s': control['round']})]
                return source
            def required(source):
                fills.append('required')
                value = copy.deepcopy(source)
                value.sessions[0].title = path.read_text()
                set_required_details(value, 'ok', all_new=True)
                return details.DetailSnapshot(details.state_key(source), value)
            def optional(source):
                fills.append('optional')
                value = copy.deepcopy(source)
                value.sessions[0].campaign_label = 'optional label'
                value.observations['optional:steward'] = {'state': 'success'}
                return details.DetailSnapshot(details.state_key(source), value)
            collector.required_detail_refresh, collector.detail_refresh = required, optional

            def draw(*_args, snapshot=None, **_kwargs):
                basic = (snapshot.observations or {}).get('basic', {})
                ready = required_details_complete(snapshot.observations)
                render._EXPLORER_READINESS = {'ready': ready,
                    'generation_identity': basic.get('generation_identity')}
                if not snapshot.sessions:
                    return
                row = snapshot.sessions[0]
                paints.append((control['round'], row.title, getattr(row, 'campaign_label', None),
                               row.exec_child['etime_s']))
                if ready:
                    runtime.has_complete = True
                if not ready or getattr(row, 'campaign_label', None) != 'optional label':
                    return
                if control['phase'] == 'first':
                    control.update(phase='unchanged', due=True)
                elif control['phase'] == 'unchanged' and control['round'] == 2:
                    self.assertEqual(fills, ['required', 'optional'])
                    path.write_text('changed title')
                    control.update(phase='changed', due=True)
                elif control['phase'] == 'changed' and control['round'] == 3:
                    self.assertEqual(row.title, 'changed title')
                    self.assertEqual(fills, ['required', 'optional', 'required', 'optional'])
                    render._EXPLORER_STATE = NavState(scope='session:aa', opened='aa')
                    control['phase'] = 'selection'
                elif control['phase'] == 'selection' and fills.count('optional') == 3:
                    control['quit'] = True

            # Keep the real loop, its queueing, merge and cache logic; only the
            # terminal, worker execution and unrelated external reads are fake.
            stack.enter_context(mock.patch.dict(render.__dict__, {
                '_EXPLORER_ENABLED': True, '_EXPLORER_STATE': NavState(),
                '_PROMPT': None, '_PENDING_KILL': None}))
            for name, value in {
                '_explorer_runtime': lambda: runtime, '_init_colors': lambda: None,
                '_configure_input': lambda *_a, **_k: None,
                '_CursesInput': Reader, 'RefreshPump': Pump,
                '_ExplorerFeed': lambda *_a, **_k: None,
                '_draw': draw, 'set_refresh_health': lambda **_k: None,
                '_explorer_question_pump_tick': lambda: None,
                '_explorer_handle': lambda *_a, **_k: None,
                '_collect_governor': lambda: None,
                '_collect_memory_observed': lambda **_k: ({}, 'absent'),
                '_collect_project_status': lambda *_a: {},
            }.items():
                stack.enter_context(mock.patch.object(render, name, value))
            stack.enter_context(mock.patch.object(render.curses, 'curs_set'))
            stack.enter_context(mock.patch.object(render.os, 'write', return_value=0))
            stack.enter_context(mock.patch.object(render.gitinfo, 'enrich_entities'))
            stack.enter_context(mock.patch('tools.fleet.explorer.focus.FocusReader'))
            stack.enter_context(mock.patch.object(steward, 'last_supervision', return_value={}))
            stack.enter_context(mock.patch.object(herdr, 'list_agents', return_value=[]))
            stack.enter_context(mock.patch.object(peer_messages, 'collect', return_value=(
                {'records': [], 'by_session': {}}, SimpleNamespace(state='success'))))
            self.assertEqual(render._loop(Screen(), collector, None, 'both', 2.0), 0)
            self.assertTrue(control['quit'], paints)
            self.assertEqual(self_interval, [(2.0, True)])
            self.assertEqual(fills, ['required', 'optional', 'required', 'optional', 'optional'])
            self.assertTrue(any(row[0] == 2 and row[3] == 2 for row in paints))

    def test_unchanged_input_rebinds_fresh_clock_and_generation_then_edit_invalidates(self):
        from runtime_observation import InputGenerations
        from tools.fleet.collectors import peer_messages, steward
        from types import SimpleNamespace
        basic = _basic('round-1')
        basic.sessions = [Session(harness='claude', pid=17, proc_start=100,
                                  session_id='s1', liveness='idle', cwd='/repo',
                                  exec_child={'pid': 20, 'etime_s': 1, 'leaf_etime_s': 1})]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'title.json'
            path.write_text('observed title')
            inputs = InputGenerations()
            with inputs.capture():
                text = path.read_text()
            value = copy.deepcopy(basic)
            value.sessions[0].title = text
            set_required_details(value, 'ok', all_new=True)
            cache = details.DetailReuse()
            cache.key = cache.input_key(basic)
            with mock.patch.object(steward, 'last_supervision', return_value={}):
                cache.remember(basic, details.DetailSnapshot(details.state_key(basic), value),
                               inputs, SimpleNamespace(check=lambda _holders: ()), optional=False)
            fresh = copy.deepcopy(basic)
            fresh.observations = _basic('round-2').observations
            fresh.sessions[0].exec_child['etime_s'] = 12
            fresh.sessions[0].exec_child['leaf_etime_s'] = 12
            with mock.patch.object(peer_messages, 'collect', return_value=(
                    {'records': [], 'by_session': {}}, SimpleNamespace(state='success'))):
                result = cache.reuse(fresh, ())
            self.assertIsNotNone(result)
            self.assertTrue(required_details_complete(result.snapshot.observations))
            self.assertFalse(result.snapshot.observations['required-details']['all_new'])
            self.assertEqual(result.snapshot.observations['required-details']['generation_identity'],
                             'round-2')
            self.assertEqual(result.snapshot.sessions[0].title, 'observed title')
            self.assertEqual(result.snapshot.sessions[0].exec_child['etime_s'], 12)
            self.assertEqual(value.sessions[0].exec_child['etime_s'], 1)
            path.write_text('new title')
            self.assertIsNone(cache.reuse(fresh, ()))
            # A child replacement is not a clock update.
            fresh.sessions[0].exec_child['pid'] = 21
            self.assertNotEqual(cache.input_key(fresh), cache.key)

    def test_holder_change_and_explicit_detail_request_require_interpretation(self):
        from runtime_observation import InputGenerations
        source = _basic('round-1')
        set_required_details(source, 'ok', all_new=True)
        cache = details.DetailReuse(optional=True)
        cache.latest = details.DetailSnapshot(details.state_key(source), source)
        cache.key = cache.input_key(source)
        cache.inputs = InputGenerations()
        cache.optional_complete = True
        cache.holder_states = ((('claude', 'holder'), 'live'),)
        self.assertIsNone(cache.reuse(source, ((('claude', 'holder'), 'ended'),)))
        self.assertIsNone(cache.reuse(source, cache.holder_states, force=True))

    def test_one_lazy_census_per_round_and_herdr_holders_do_not_read_proc(self):
        from tools.fleet.collectors import steward, herdr
        agent = {'agent': 'claude', 'agent_session': {'value': 'on-pane'}}
        source = _basic('round-1')
        with mock.patch.object(herdr, 'list_agents', return_value=[agent]) as agents, \
             mock.patch.object(steward._EndObservation, 'claude') as census:
            observation = steward.HolderObservation(source)
            self.assertEqual(observation.check([('claude', 'on-pane')])[0][1], 'live')
            self.assertEqual(observation.check([('claude', 'on-pane')])[0][1], 'live')
            agents.assert_called_once()
            census.assert_not_called()
        with mock.patch.object(herdr, 'list_agents', return_value=[]), \
             mock.patch.object(steward._EndObservation, 'claude') as census:
            census.return_value.state.return_value = 'alive'
            observation = steward.HolderObservation(source)
            observation.check([('claude', 'one'), ('claude', 'two')])
            observation.check([('claude', 'one')])
            # The census object's lazy once-per-harness cache is tested by the
            # steward suite; each selected holder is checked only once here.
            self.assertEqual(census.return_value.state.call_count, 2)


class Fleet2DetailReadinessTests(unittest.TestCase):
    @staticmethod
    def _session(*, route_id=None):
        row = Session(harness="claude", pid=17, proc_start=100, session_id="sid-a",
                      cwd="/repo", liveness="idle", route_id=route_id)
        row.peer_last_recv = {"from_session_id": "sid-peer", "age_min": 2}
        row.peer_last_sent = {"to_session_id": "sid-peer", "age_min": 3}
        row.peer_sent_1h = 2
        row.peer_recv_1h = 1
        row.peer_obligations = [{"kind": "watch"}]
        return row

    def test_optional_last_good_survives_required_only_pass_for_exact_row(self):
        previous = LiveSnapshot(
            sessions=[self._session()], memory={
                "by_repo": {"repo": [{"id": "record"}]},
                "database": {"state": "success", "by_repo": {"repo": [{"id": "backlog"}]}}},
            observations={"optional:memory": {"state": "success"},
                          "optional:memory-db": {"state": "success"},
                          "optional:peers": {"state": "success"}})
        current = LiveSnapshot(sessions=[self._session()], memory=None)
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, current))
        row = retained.snapshot.sessions[0]
        self.assertEqual(retained.snapshot.memory, previous.memory)
        self.assertEqual(row.peer_last_recv["from_session_id"], "sid-peer")
        self.assertEqual(row.peer_sent_1h, 2)
        self.assertEqual(row.peer_obligations, [{"kind": "watch"}])
        self.assertEqual(retained.snapshot.observations["optional:peers"]["state"], "success")
        self.assertEqual(retained.snapshot.memory["database"]["by_repo"]["repo"][0]["id"],
                         "backlog")

    def test_failed_database_read_keeps_prior_backlog_with_failed_health(self):
        previous = LiveSnapshot(
            memory={"database": {"state": "success", "by_repo": {"repo": [{"id": "task-1"}]}}},
            observations={"optional:memory-db": {"state": "success"}})
        current = LiveSnapshot(
            memory={"database": {"state": "failed", "by_repo": {}}},
            observations={"optional:memory-db": {"state": "failed"}})
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, current))
        database = retained.snapshot.memory["database"]
        self.assertEqual(database["state"], "failed")
        self.assertTrue(database["last_good"])
        self.assertEqual(database["last_good_by_repo"]["repo"][0]["id"], "task-1")

    def test_unknown_peer_observation_keeps_exact_last_good_and_marks_stale(self):
        prior_session = self._session()
        previous = LiveSnapshot(sessions=[prior_session],
                                observations={"optional:peers": {"state": "success",
                                                                  "observed_at": 10}})
        current_session = self._session()
        current_session.peer_last_recv = None
        current_session.peer_last_sent = None
        current_session.peer_sent_1h = 0
        current_session.peer_recv_1h = 0
        current_session.peer_obligations = []
        current = LiveSnapshot(sessions=[current_session],
                               observations={"optional:peers": {"state": "unknown",
                                                                 "observed_at": 11}})
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, current))
        self.assertEqual(retained.snapshot.sessions[0].peer_last_recv["from_session_id"], "sid-peer")
        self.assertEqual(retained.snapshot.observations["optional:peers"]["state"], "unknown")
        self.assertTrue(retained.snapshot.observations["optional:peers"]["last_good"])

    def test_peer_health_success_empty_clears_and_failed_partial_preserves(self):
        from types import SimpleNamespace

        empty = {"records": [], "by_session": {}}
        row = self._session()
        success = details._apply_peer_observation(
            lambda *, include_health: (empty, SimpleNamespace(state="success")), [row])
        self.assertEqual(success, "success")
        self.assertIsNone(row.peer_last_recv)
        self.assertIsNone(row.peer_last_sent)
        self.assertEqual(row.peer_sent_1h, 0)
        self.assertEqual(row.peer_obligations, [])

        row = self._session()
        prior = (copy.deepcopy(row.peer_last_recv), copy.deepcopy(row.peer_last_sent),
                 row.peer_sent_1h, row.peer_recv_1h, copy.deepcopy(row.peer_obligations))
        partial = {"records": [{"kind": "partial"}], "by_session": {}}
        failed = details._apply_peer_observation(
            lambda *, include_health: (partial, SimpleNamespace(state="failed")), [row])
        self.assertEqual(failed, "failed")
        self.assertEqual((row.peer_last_recv, row.peer_last_sent, row.peer_sent_1h,
                          row.peer_recv_1h, row.peer_obligations), prior)

    def test_legacy_empty_peer_mapping_is_unknown_and_absent_does_not_clear_last_good(self):
        row = self._session()
        prior = copy.deepcopy(row.peer_last_recv)
        state = details._apply_peer_observation(
            lambda: {"records": [], "by_session": {}}, [row])
        self.assertEqual(state, "unknown")
        self.assertEqual(row.peer_last_recv, prior)

        current = self._session()
        current.peer_last_recv = current.peer_last_sent = None
        current.peer_sent_1h = current.peer_recv_1h = 0
        current.peer_obligations = []
        previous = LiveSnapshot(sessions=[self._session()],
                                observations={"optional:peers": {"state": "success"}})
        current_snapshot = LiveSnapshot(sessions=[current],
                                        observations={"optional:peers": {"state": "absent"}})
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, current_snapshot))
        self.assertEqual(retained.snapshot.sessions[0].peer_last_recv["from_session_id"], "sid-peer")

    def test_optional_last_good_does_not_cross_changed_binding_or_confirmed_empty(self):
        previous = LiveSnapshot(
            sessions=[self._session(route_id="route-old")], memory={"by_repo": {"repo": [1]}},
            observations={"optional:memory": {"state": "success"},
                          "optional:peers": {"state": "success"}})
        changed_row = self._session(route_id="route-new")
        changed_row.peer_last_recv = None
        changed_row.peer_last_sent = None
        changed_row.peer_sent_1h = 0
        changed_row.peer_recv_1h = 0
        changed_row.peer_obligations = []
        changed = LiveSnapshot(sessions=[changed_row], memory=None)
        changed.observations = {"optional:memory": {"state": "absent"},
                                "optional:peers": {"state": "success"}}
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, changed))
        self.assertIsNone(retained.snapshot.memory)
        self.assertIsNone(retained.snapshot.sessions[0].peer_last_recv)
        self.assertEqual(retained.snapshot.sessions[0].peer_sent_1h, 0)

    def _steward_session(self, *, route_id=None, steward=True):
        row = self._session(route_id=route_id)
        row.steward = steward
        row.steward_targets = ([{"harness": "codex", "session_id": "sid-worker", "source": "watch"}]
                               if steward else None)
        row.steward_parents = None
        return row

    def test_required_pass_keeps_last_judged_steward_role_through_merge(self):
        # Only the optional pass judges the role; the next required pass carries
        # the model default False, which must not overwrite a judged steward.
        previous = LiveSnapshot(sessions=[self._steward_session()],
                                observations={"optional:steward": {"state": "success",
                                                                    "observed_at": 10}})
        required = LiveSnapshot(sessions=[self._session()], observations={})
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, required))
        row = retained.snapshot.sessions[0]
        self.assertTrue(row.steward)
        self.assertEqual(row.steward_targets[0]["session_id"], "sid-worker")
        self.assertEqual(retained.snapshot.observations["optional:steward"]["state"], "success")

        basic = LiveSnapshot(sessions=[self._session()])
        merged = details.merge(basic, details.DetailSnapshot(details.state_key(basic),
                                                             retained.snapshot))
        self.assertTrue(merged.sessions[0].steward)
        self.assertEqual(merged.sessions[0].steward_targets[0]["session_id"], "sid-worker")

        failed = LiveSnapshot(sessions=[self._session()],
                              observations={"optional:steward": {"state": "failed",
                                                                  "observed_at": 11}})
        kept = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, failed))
        self.assertTrue(kept.snapshot.sessions[0].steward)
        self.assertEqual(kept.snapshot.observations["optional:steward"]["state"], "failed")
        self.assertTrue(kept.snapshot.observations["optional:steward"]["last_good"])

    def test_judged_steward_clear_and_changed_binding_do_not_inherit_role(self):
        previous = LiveSnapshot(sessions=[self._steward_session(route_id="route-old")],
                                observations={"optional:steward": {"state": "success"}})
        cleared = LiveSnapshot(sessions=[self._steward_session(route_id="route-old",
                                                               steward=False)],
                               observations={"optional:steward": {"state": "success"}})
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, cleared))
        self.assertFalse(retained.snapshot.sessions[0].steward)
        self.assertIsNone(retained.snapshot.sessions[0].steward_targets)

        moved = LiveSnapshot(sessions=[self._session(route_id="route-new")], observations={})
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, moved))
        self.assertFalse(retained.snapshot.sessions[0].steward)
        self.assertIsNone(retained.snapshot.sessions[0].steward_targets)

        never_judged = LiveSnapshot(sessions=[self._steward_session()], observations={})
        required = LiveSnapshot(sessions=[self._session()], observations={})
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, never_judged), details.DetailSnapshot({}, required))
        self.assertFalse(retained.snapshot.sessions[0].steward)
        self.assertNotIn("optional:steward", retained.snapshot.observations)

    def test_required_details_need_success_and_exact_basic_identity(self):
        pending = _basic("basic-1")
        self.assertFalse(pending.required_details_ready)
        self.assertFalse(required_details_complete(pending.observations))

        set_required_details(pending, "ok", all_new=True, observed_at=101.0)
        self.assertTrue(pending.required_details_ready)
        self.assertTrue(required_details_all_new(pending.observations))

        pending.observations["required-details"]["generation_identity"] = "basic-older"
        self.assertFalse(pending.required_details_ready)

    def test_readiness_and_all_new_are_distinct(self):
        continued = _basic("basic-2")
        set_required_details(continued, "ok", all_new=False, observed_at=102.0)
        self.assertTrue(continued.required_details_ready)
        self.assertFalse(continued.required_details_all_new)

    def test_usage_cache_read_completes_while_http_refresh_remains_pending(self):
        observed = _usage_cache_observation(
            {"freshness": "stale", "observed_at": 99.0}, refresh_pending=True)
        self.assertEqual(observed["state"], "ok")
        self.assertEqual(observed["cache_state"], "stale")
        self.assertEqual(observed["refresh_state"], "pending")

        failed = _usage_cache_observation(None)
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["attempted"])

    def test_required_projection_rejects_missing_route_proof_and_required_read_errors(self):
        row = DispatchJob(key="execute", slug="worker", route_id="route-1")
        row.work_projection = WorkProjection(
            route_id="route-1", route_hash="sha256:route",
            _route_view={"record": {"route_id": "route-1", "route_hash": "sha256:route"}})
        self.assertTrue(required_projection_details_complete([row]))

        row._required_detail_error = True
        self.assertFalse(required_projection_details_complete([row]))
        row._required_detail_error = False
        row.work_projection = WorkProjection(route_id="route-1", ambiguity="route-record-mismatch")
        self.assertFalse(required_projection_details_complete([row]))

    def test_validated_continuation_needs_matching_route_binding(self):
        row = DispatchJob(key="execute", slug="worker", route_id="route-2",
                          route_hash="sha256:route-2")
        row._continuation_validated = True
        row.work_projection = WorkProjection(route_id="route-2", route_hash="sha256:route-2")
        self.assertTrue(required_projection_details_complete([row]))

        row.work_projection = WorkProjection(route_id="route-2", route_hash="sha256:stale")
        self.assertFalse(required_projection_details_complete([row]))

    def test_merge_only_adopts_details_from_the_current_basic_identity(self):
        current = _basic("basic-current")
        completed = copy.deepcopy(current)
        set_required_details(completed, "ok", all_new=True, observed_at=103.0)
        merged = details.merge(
            current, details.DetailSnapshot(details.state_key(current), completed))
        self.assertTrue(merged.required_details_ready)
        self.assertTrue(merged.required_details_all_new)

        old_basic = _basic("basic-old")
        old_detail = copy.deepcopy(old_basic)
        set_required_details(old_detail, "ok", all_new=True, observed_at=104.0)
        newer_basic = _basic("basic-newer")
        stale = details.merge(
            newer_basic, details.DetailSnapshot(details.state_key(old_basic), old_detail))
        self.assertFalse(stale.required_details_ready)
        self.assertEqual(stale.observations["required-details"]["state"], "pending")

    def test_complete_basic_pass_skips_a_second_required_detail_pass(self):
        source = _basic("basic-complete")
        set_required_details(source, "ok", all_new=True, observed_at=105.0)
        with mock.patch("tools.fleet.collectors.dispatch._fill_locations"), \
                mock.patch("tools.fleet.collectors.dispatch._campaign_labels") as campaigns, \
                mock.patch("tools.fleet.collectors._adopt_child_titles") as child_titles, \
                mock.patch("tools.fleet.titles.annotate") as title_details, \
                mock.patch.object(route_chain, "enrich") as route_enrich, \
                mock.patch.object(projection, "attach_projections") as attach, \
                mock.patch("tools.fleet.collectors.dispatch._scan_degradations") as degradations, \
                mock.patch("tools.fleet.work_titles.annotate") as work_titles, \
                mock.patch("tools.fleet.collectors.dispatch._pending_delivery_counts",
                           return_value=None) as deliveries, \
                mock.patch("tools.fleet.collectors.dispatch._candidate_jobs_paths",
                           return_value=[]), \
                mock.patch("tools.fleet.collectors.peer_messages.collect", return_value={}) as peers, \
                mock.patch("tools.fleet.collectors.steward.enrich") as steward, \
                mock.patch("tools.fleet.herdr_projection.refresh_observed_tag_metadata") as panes:
            result = details._enrich(source, required=False)

        steward.assert_called_once()
        self.assertEqual(result.snapshot.observations["optional:steward"]["state"], "success")
        route_enrich.assert_not_called()
        attach.assert_not_called()
        degradations.assert_not_called()
        campaigns.assert_called_once()
        child_titles.assert_called_once()
        title_details.assert_called_once()
        work_titles.assert_called_once()
        deliveries.assert_called_once()
        peers.assert_called_once()
        panes.assert_called_once()
        self.assertTrue(result.snapshot.required_details_ready)

    def test_failed_steward_judgment_is_recorded_not_reported_as_clear(self):
        source = _basic("basic-steward-failed")
        set_required_details(source, "ok", all_new=True, observed_at=106.0)
        with mock.patch("tools.fleet.collectors.dispatch._fill_locations"), \
                mock.patch("tools.fleet.collectors.dispatch._campaign_labels"), \
                mock.patch("tools.fleet.collectors._adopt_child_titles"), \
                mock.patch("tools.fleet.titles.annotate"), \
                mock.patch("tools.fleet.work_titles.annotate"), \
                mock.patch("tools.fleet.collectors.dispatch._pending_delivery_counts",
                           return_value=None), \
                mock.patch("tools.fleet.collectors.dispatch._candidate_jobs_paths",
                           return_value=[]), \
                mock.patch("tools.fleet.collectors.peer_messages.collect", return_value={}), \
                mock.patch("tools.fleet.collectors.steward.enrich",
                           side_effect=RuntimeError("marker root unreadable")), \
                mock.patch("tools.fleet.herdr_projection.refresh_observed_tag_metadata"):
            result = details._enrich(source, required=False)
        self.assertEqual(result.snapshot.observations["optional:steward"]["state"], "failed")

    def test_required_only_pass_defers_optional_detail_sources(self):
        source = _basic("basic-pending")
        with mock.patch("tools.fleet.collectors.dispatch._fill_locations"), \
                mock.patch("tools.fleet.collectors.dispatch._campaign_labels") as campaigns, \
                mock.patch("tools.fleet.collectors._adopt_child_titles") as child_titles, \
                mock.patch("tools.fleet.titles.annotate") as title_details, \
                mock.patch.object(route_chain, "enrich") as route_enrich, \
                mock.patch.object(projection, "attach_projections") as attach, \
                mock.patch("tools.fleet.collectors.dispatch._scan_degradations") as degradations, \
                mock.patch("tools.fleet.work_titles.annotate") as work_titles, \
                mock.patch("tools.fleet.collectors.dispatch._pending_delivery_counts") as deliveries, \
                mock.patch("tools.fleet.collectors.peer_messages.collect") as peers, \
                mock.patch("tools.fleet.collectors.steward.enrich") as steward, \
                mock.patch("tools.fleet.herdr_projection.refresh_observed_tag_metadata") as panes:
            result = details._enrich(source, required_only=True)

        route_enrich.assert_called_once()
        attach.assert_called_once()
        degradations.assert_called_once()
        self.assertNotIn("optional:steward", result.snapshot.observations)
        for optional in (campaigns, child_titles, title_details, work_titles,
                         deliveries, peers, steward, panes):
            optional.assert_not_called()
        self.assertTrue(result.snapshot.required_details_ready)

    def test_required_detail_keeps_default_report_verification_policy(self):
        source = _basic("basic-report-policy")
        with mock.patch("tools.fleet.collectors.dispatch._fill_locations"), \
                mock.patch("tools.fleet.collectors.dispatch._scan_degradations",
                           return_value={}), \
                mock.patch.object(route_chain, "enrich"), \
                mock.patch.object(projection, "attach_projections") as attach, \
                mock.patch.object(projection, "required_projection_details_complete",
                                  return_value=True):
            details._enrich(source, required_only=True)
            self.assertIs(attach.call_args.kwargs["include_report_verification"], True)

            attach.reset_mock()
            details._enrich(source, required_only=True,
                            include_report_verification=False)
            self.assertIs(attach.call_args.kwargs["include_report_verification"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
