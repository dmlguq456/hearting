"""First real state, independent detail refresh, and exact delayed joins."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import details, model, projection, render, route, route_chain
from fleet.collectors import dispatch
from fleet.refresh import LiveSnapshot


class BasicObservationTest(unittest.TestCase):
    def test_basic_projection_reads_no_routes_artifacts_or_ledgers_but_keeps_resource_now(self):
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(harness=harness):
                owner = model.DispatchJob(key='lab', slug='owner', harness=harness,
                    attempt_id='att-owner', pid=42, proc_start='100', liveness='idle',
                    owner_route_id='rt-work', owner_route_hash='hash', owner_route_file='/fixture/route.json',
                    state_evidence={'inputs': {'observed_liveness': {'state': 'parked-supervised'}}},
                    summary='previous model turn')
                child = model.ResourceJob(run_id='run', parent_attempt_id='att-owner',
                    route_id='rt-work', route_hash='hash', route_file='/fixture/route.json', node='train',
                    liveness='working', pid=43, starttime='101', command=['python', 'train.py'])
                session = model.Session(harness=harness, pid=40, proc_start='90', session_id='parent')
                with mock.patch.object(projection, '_load_evidence_records', side_effect=AssertionError('routes')), \
                        mock.patch.object(projection, '_spec_marker_index', side_effect=AssertionError('markers')), \
                        mock.patch.object(projection, '_artifact_candidates', side_effect=AssertionError('artifacts')), \
                        mock.patch.object(route_chain, 'session_lines', side_effect=AssertionError('ledger')):
                    route_chain.enrich([session], jobs=[owner], fast_first=True)
                    projection.attach_projections([session], [owner], resources=[child], fast_first=True)
                self.assertEqual(owner.work_projection.node_state, 'unknown')
                self.assertIsNone(owner.work_projection.progress)
                self.assertIsNone(owner.work_projection._route_view)
                self.assertEqual(owner.resource_wait['run_ids'], ['run'])
                self.assertEqual(owner.resource_children, [child])
                with mock.patch.object(render, '_fresh_compute_hosts', return_value=(None, None)):
                    now = render._resource_now_text(owner)
                self.assertIn('train train.py', now)
                self.assertIn('host/GPU unknown', now)
                self.assertNotIn('previous model turn', now)
                # A different declared route does not acquire a parked wait.
                child.route_id = 'foreign'
                projection.attach_projections([], [owner], resources=[child], fast_first=True)
                self.assertIsNone(owner.resource_wait)

    def test_basic_now_reads_exact_summary_but_defers_title(self):
        row = model.DispatchJob(key='code', attempt_id='att-x', harness='codex')
        with mock.patch.object(dispatch, '_owned_attempt_log_path', return_value='/owned/log'), \
                mock.patch('fleet.titles.fresh_title', side_effect=AssertionError('title read')), \
                mock.patch('fleet.titles.fresh_summary_with_ts', return_value=('current work', 12)) as summary:
            dispatch._enrich_attempt_summary(row, fast_first=True)
        summary.assert_called_once_with('dispatch-att-x', harness='codex')
        self.assertEqual((row.summary, row.summary_ts), ('current work', 12))

    def test_unknown_basic_now_is_checking_and_does_not_invent_activity(self):
        owner = model.DispatchJob(key='code', harness='opencode', liveness='working')
        projection.attach_projections([], [owner], fast_first=True)
        self.assertIsNone(owner.work_projection.progress)
        text = render._plain(render._context_detail_row(owner, term_width=168)[0])
        self.assertIn('loading', text)
        self.assertNotIn('모델 턴', text)
        self.assertIsNone(owner.exec_tool)


    def test_basic_screen_keeps_dotted_owner_flag_now_and_observed_gpu(self):
        saved = {name: getattr(render, name) for name in (
            '_PROCESS_VIEW', '_SHOW_ALL', '_LAYOUT', '_COMPUTE_HOSTS', '_COMPUTE_HOSTS_SET_AT')}
        for name, value in saved.items():
            self.addCleanup(setattr, render, name, value)
        render._PROCESS_VIEW, render._SHOW_ALL, render._LAYOUT = False, False, 'auto'
        parent = model.Session(harness='codex', pid=40, proc_start='90', session_id='parent',
            session_tag='4d', steward=True, steward_targets=[{'harness': 'codex', 'session_id': 'target'}],
            cwd='/work/first-state', model='gpt-6.1-sol', liveness='working')
        target = model.Session(harness='codex', pid=45, proc_start='105', session_id='target',
                               session_tag='f5', cwd=parent.cwd, liveness='idle')
        owner = model.DispatchJob(key='lab', slug='owner', harness='opencode',
            model='gpt-6.1-sol', depth=1, dispatch_depth=1, worker_type='owner',
            parent_sid='parent', is_child=True, cwd=parent.cwd,
            attempt_id='att-owner', pid=42, proc_start='100', liveness='idle',
            owner_route_id='rt-work', owner_route_hash='hash', owner_route_file='/fixture/route.json',
            state_evidence={'inputs': {'observed_liveness': {'state': 'parked-supervised'}}})
        child = model.ResourceJob(run_id='run', parent_attempt_id='att-owner',
            route_id='rt-work', route_hash='hash', route_file='/fixture/route.json', node='train',
            liveness='working', pid=43, starttime='101', process_group=43,
            command=['python', 'train.py'], elapsed_min=2)
        projection.attach_projections([parent, target], [owner], resources=[child], fast_first=True)
        render.set_compute_hosts({'configured': True, 'collected_at': time.time(), 'hosts': [
            {'host': 'mgpu-a1', 'reachable': True, 'self': True, 'gpus': [{'index': 1, 'name': 'RTX',
                'processes': [{'pid': 43, 'proc_start': '101', 'pgid': 43, 'command': 'python train.py',
                               'used_memory_mib': 1024}]}]}]})
        with mock.patch.object(route, 'load', side_effect=AssertionError('render route read')), \
                mock.patch.object(render, '_git_branch', side_effect=AssertionError('render git read')):
            rows = render._build_lines([parent, target], [owner], 'both', False, 0,
                                       term_width=168, resources=[child])
        screen = '\n'.join(render._plain(row) for row in rows)
        for required in ('⚑', '┆', '╭', 'owner', 'train train.py', 'mgpu-a1:1', 'GPU mgpu-a1:1'):
            self.assertIn(required, screen)

    def test_basic_process_view_shows_route_bound_depth_two_worker_now(self):
        self.addCleanup(setattr, render, '_PROCESS_VIEW', render._PROCESS_VIEW)
        self.addCleanup(setattr, render, '_SHOW_ALL', render._SHOW_ALL)
        render._PROCESS_VIEW, render._SHOW_ALL = True, False
        worker = model.DispatchJob(key='autopilot-code', slug='worker-exact', harness='codex',
            attempt_id='att-stage', pid=45, proc_start='105', liveness='working', model='gpt-6.1-sol',
            depth=2, dispatch_depth=2, route_id='rt-exact', route_hash='hash',
            route_node='execute', route_file='/fixture/route.json', summary='exact stage NOW')
        projection.attach_projections([], [worker], fast_first=True)
        with mock.patch.object(route, 'load', side_effect=AssertionError('route read')), \
                mock.patch.object(render, '_fresh_compute_hosts', return_value=(None, None)):
            lines = render._build_lines([], [worker], 'both', False, 0, term_width=168)
        screen = '\n'.join(render._plain(line) for line in lines)
        self.assertIn('worker-exact', screen)
        self.assertIn('exact stage NOW', screen)
        self.assertIn('gpt-6.1-sol', screen)
        self.assertIn('route linking', screen)
        self.assertNotIn('no active route', screen)
        self.assertNotIn('no route record', screen)
        self.assertIsNone(worker.work_projection.progress)

    def test_real_detail_pass_fills_cycle_title_and_verified_process_route(self):
        self.addCleanup(setattr, render, '_PROCESS_VIEW', render._PROCESS_VIEW)
        self.addCleanup(setattr, render, '_SHOW_ALL', render._SHOW_ALL)
        render._PROCESS_VIEW, render._SHOW_ALL = True, False
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = {'schema_version': 1, 'cwd': tmp, 'artifact_root': tmp,
                'capability': 'autopilot-code', 'effective_intensity': 'standard',
                'nodes': [{'id': 'execute', 'depends_on': []}, {'id': 'train', 'depends_on': []}]}
            record['route_hash'] = route.route_hash(record)
            record['route_id'] = 'rt-' + record['route_hash'].split(':')[1][:16]
            path = root / 'route.json'
            path.write_text(json.dumps(record))
            cycles = root / '.runtime/artifact-producer/v1/cycles'
            cycles.mkdir(parents=True)
            (cycles / 'cyc_fixture.json').write_text(json.dumps(
                {'route_id': record['route_id'], 'title': 'actual cycle title'}))
            owner = model.DispatchJob(key='code', slug='owner', harness='codex', cwd=tmp,
                attempt_id='att-owner', pid=42, proc_start='100', liveness='idle',
                state_evidence={'inputs': {'observed_liveness': {'state': 'parked-supervised'}}},
                qa_source='explicit', worker_type='owner', depth=1, artifact_root=tmp,
                owner_route_id=record['route_id'], owner_route_hash=record['route_hash'],
                owner_route_file=str(path))
            worker = model.DispatchJob(key='code-execute', slug='exact-worker', harness='claude', cwd=tmp,
                attempt_id='att-execute', pid=44, proc_start='102', liveness='working',
                depth=2, dispatch_depth=2, parent_slug=owner.slug,
                route_id=record['route_id'], route_hash=record['route_hash'], route_file=str(path),
                route_node='execute', summary='real worker NOW', qa_source='explicit', artifact_root=tmp)
            worker.parent_attempt_id = owner.attempt_id
            resource = model.ResourceJob(run_id='run', parent_attempt_id=owner.attempt_id,
                route_id=record['route_id'], route_hash=record['route_hash'], route_file=str(path),
                node='train', liveness='working', pid=43, starttime='101', command=['python', 'train.py'])
            projection.attach_projections([], [owner, worker], resources=[resource], fast_first=True)
            source = LiveSnapshot(jobs=[owner, worker], resources=[resource])
            def screen(snapshot):
                with mock.patch.object(render, '_fresh_compute_hosts', return_value=(None, None)), \
                        mock.patch.object(dispatch.collect, 'last_route_nodes', {}):
                    lines = render._build_lines([], snapshot.jobs, 'both', False, 0,
                        term_width=168, resources=snapshot.resources, governor=None)
                return '\n'.join(render._plain(line) for line in lines)
            with mock.patch.object(route, 'load', side_effect=AssertionError('basic render route read')):
                before = screen(source)
            for value in ('owner', 'exact-worker', 'real worker NOW', 'train train.py', 'route linking'):
                self.assertIn(value, before)
            self.assertNotIn('no active route', before)
            with mock.patch.object(dispatch, '_pending_delivery_counts', return_value=None), \
                    mock.patch.object(dispatch, '_scan_degradations', return_value={}), \
                    mock.patch.object(projection, '_spec_marker_index', return_value={}), \
                    mock.patch.object(projection, '_capability_grounding_index', return_value={}), \
                    mock.patch('fleet.collectors.peer_messages.collect', return_value=None):
                filled = details.enrich(source)
            joined = details.merge(source, filled)
            row = joined.jobs[0]
            self.assertEqual(row.campaign_label, 'actual cycle title')
            self.assertEqual(row.work_projection.source, 'route-exact')
            self.assertIsNotNone(row.work_projection._route_view)
            self.assertEqual(row.resource_wait['run_ids'], ['run'])
            after = screen(joined)
            for value in ('owner', 'exact-worker', 'real worker NOW', 'train train.py'):
                self.assertIn(value, after)
            self.assertNotIn('route linking', after)
            self.assertNotIn('no active route', after)
            self.assertIsNone(owner.campaign_label)



class CampaignLabelsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cycles = self.root / '.runtime/artifact-producer/v1/cycles'
        self.cycles.mkdir(parents=True)
        for cache in (dispatch._CYCLE_RECORD_CACHE, dispatch._CYCLE_LABEL_INVENTORIES, dispatch._CYCLE_ROUTE_PATHS):
            cache.clear()

    def record(self, name, rid, title):
        (self.cycles / name).write_text(json.dumps({'route_id': rid, 'title': title}))

    def test_existing_producer_hint_reads_exact_record_without_admission_payload_or_inventory(self):
        import directory_record_index
        self.record('cyc_exact.json', 'rt-exact', 'exact title')
        index = self.root / '.runtime/artifact-admission/v1/index.json'
        index.parent.mkdir(parents=True)
        index.write_text('{a large admission payload not needed for a display label')
        directory_record_index.select(self.cycles, {'rt-exact'}, kind='cycle-routes',
                                      classify=directory_record_index.cycle_keys,
                                      read_json=dispatch._read_cycle_record)
        job = model.DispatchJob(key='code', route_id='rt-exact', artifact_root=str(self.root))
        original = dispatch._read_cycle_record
        def read(path):
            self.assertNotEqual(Path(path), index)
            return original(path)
        with mock.patch.object(dispatch.os, 'listdir', side_effect=AssertionError('cycle inventory')), \
                mock.patch.object(dispatch, '_read_cycle_record', side_effect=read):
            dispatch._campaign_labels([job])
        self.assertEqual(job.campaign_label, 'exact title')
        self.assertNotIn(str(index), dispatch._CYCLE_RECORD_CACHE)

    def test_stale_or_corrupt_hint_uses_bounded_records_and_does_not_repair_disk(self):
        import directory_record_index
        self.record('first.json', 'rt-wanted', 'old title')
        directory_record_index.select(self.cycles, {'rt-wanted'}, kind='cycle-routes',
                                      classify=directory_record_index.cycle_keys,
                                      read_json=dispatch._read_cycle_record)
        hint = self.cycles / '.cycle-routes-index.json'
        original_hint = hint.read_bytes()
        self.record('first.json', 'rt-foreign', 'foreign title')
        self.record('second.json', 'rt-wanted', 'current title')
        job = model.DispatchJob(key='code', route_id='rt-wanted', artifact_root=str(self.root))
        dispatch._campaign_labels([job])
        self.assertEqual(job.campaign_label, 'current title')
        self.assertEqual(hint.read_bytes(), original_hint)
        hint.write_text('{broken')
        dispatch._campaign_labels([job])
        self.assertEqual(job.campaign_label, 'current title')
        self.assertEqual(hint.read_text(), '{broken')

    def test_retired_admission_payloads_are_removed_from_preserved_label_cache(self):
        path = '/previous/root/.runtime/artifact-admission/v1/index.json'
        dispatch._CYCLE_RECORD_CACHE[path] = (('old stamp',), {'unused': 'old payload'})
        self.record('cycle.json', 'rt-exact', 'exact title')
        dispatch._campaign_labels([model.DispatchJob(key='code', route_id='rt-exact',
                                                    artifact_root=str(self.root))])
        self.assertNotIn(path, dispatch._CYCLE_RECORD_CACHE)

    def test_open_cycle_fallback_continues_in_bounded_batches_without_stat_sort(self):
        for index in range(401):
            self.record('%03d.json' % index, 'rt-%d' % index, 'title-%d' % index)
        job = model.DispatchJob(key='code', route_id='rt-400', artifact_root=str(self.root))
        original = dispatch._stat_stamp
        observed = []
        def stamp(path):
            observed.append(path)
            return original(path)
        with mock.patch.object(dispatch, '_stat_stamp', side_effect=stamp):
            dispatch._campaign_labels([job])
        self.assertIsNone(job.campaign_label)
        self.assertEqual(sum(Path(p).parent == self.cycles and not Path(p).name.startswith('.')
                             for p in observed), 200)
        dispatch._campaign_labels([job])
        self.assertIsNone(job.campaign_label)
        dispatch._campaign_labels([job])
        self.assertEqual(job.campaign_label, 'title-400')
        (self.cycles / '400.json').write_text(json.dumps({'route_id': 'rt-400', 'title': 'updated'}))
        with mock.patch.object(dispatch.os, 'listdir', side_effect=AssertionError('repeat inventory')):
            dispatch._campaign_labels([job])
        self.assertEqual(job.campaign_label, 'updated')
        (self.cycles / '400.json').unlink()
        job.campaign_label = None
        dispatch._campaign_labels([job])
        self.assertIsNone(job.campaign_label)

    def test_open_cycle_inventory_progresses_while_new_files_arrive_each_tick(self):
        for index in range(401):
            self.record('%03d.json' % index, 'rt-%d' % index, 'title-%d' % index)
        job = model.DispatchJob(key='code', route_id='rt-400', artifact_root=str(self.root))
        for tick in range(3):
            self.record('new-%d.json' % tick, 'rt-new-%d' % tick, 'new')
            dispatch._campaign_labels([job])
        self.assertEqual(job.campaign_label, 'title-400')
        # Additions are also picked up after the in-flight bounded sweep ends.
        new = model.DispatchJob(key='code', route_id='rt-new-2', artifact_root=str(self.root))
        for tick in range(3, 7):
            self.record('new-%d.json' % tick, 'rt-new-%d' % tick, 'new')
            dispatch._campaign_labels([new])
            if new.campaign_label:
                break
        self.assertEqual(new.campaign_label, 'new')

    def test_two_roots_same_route_id_keep_their_own_titles(self):
        other = self.root / 'other'
        folder = other / '.runtime/artifact-producer/v1/cycles'
        folder.mkdir(parents=True)
        self.record('one.json', 'rt-same', 'first')
        (folder / 'two.json').write_text(json.dumps({'route_id': 'rt-same', 'title': 'second'}))
        jobs = [model.DispatchJob(key='code', route_id='rt-same', artifact_root=str(p))
                for p in (self.root, other)]
        dispatch._campaign_labels(jobs)
        self.assertEqual([j.campaign_label for j in jobs], ['first', 'second'])


class DelayedJoinTest(unittest.TestCase):
    def source(self):
        row = model.DispatchJob(key='code', slug='owner', harness='opencode',
            attempt_id='att-exact', pid=42, proc_start='100', cwd='/fixture', liveness='working',
            route_id='rt-work', route_hash='hash', owner_route_file='/fixture/route.json')
        row._runtime_session_id = 'native-exact'
        row._detail_activity_key = ('log-v1',)
        row.summary, row.summary_ts = 'fresh basic NOW', 20
        row.work_projection = model.WorkProjection(source='registry-exact', node_state='unknown')
        return LiveSnapshot(jobs=[row])

    def filled(self, source):
        value = copy.deepcopy(source)
        row = value.jobs[0]
        row.title, row.campaign_label = 'title', 'campaign'
        row.summary, row.summary_ts = 'older detail NOW', 10
        row.exec_tool = {'name': 'python'}
        row.work_projection = model.WorkProjection(source='route-exact', stage_label='test', node_state='active')
        return details.DetailSnapshot(details.state_key(source), value)

    def test_details_arrive_across_new_activity_without_overwriting_new_now(self):
        source = self.source()
        detail = self.filled(source)
        source.jobs[0]._detail_activity_key = ('log-v2',)
        row = details.merge(source, detail).jobs[0]
        self.assertEqual((row.title, row.campaign_label), ('title', 'campaign'))
        self.assertEqual(row.work_projection.stage_label, 'test')
        self.assertEqual(row.summary, 'fresh basic NOW')
        self.assertIsNone(row.exec_tool)
        self.assertIsNone(source.jobs[0].title)  # copied rows, no mutation of basic source

    def test_new_native_title_and_observed_subagents_win_over_delayed_detail(self):
        source = self.source()
        session = model.Session(harness='codex', pid=70, proc_start='200',
            session_id='native-parent', cwd='/other', liveness='working',
            title='old title', subagents=[model.SubAgent(agent_type='old', active=True)])
        source.sessions.append(session)
        detail = self.filled(source)
        session.title, session.subagents = 'current title', []
        source.jobs[0].title, source.jobs[0].subagents = 'current job title', []
        detail.snapshot.jobs[0].subagents = [model.SubAgent(agent_type='old-job', active=True)]
        joined = details.merge(source, detail)
        self.assertEqual(joined.sessions[0].title, 'current title')
        self.assertEqual(joined.sessions[0].subagents, [])
        self.assertEqual(joined.jobs[0].title, 'current job title')
        self.assertEqual(joined.jobs[0].subagents, [])
        # None means unobserved; actual detail, including an observed [], fills it.
        session.subagents = None
        detail.snapshot.sessions[0].subagents = []
        self.assertEqual(details.merge(source, detail).sessions[0].subagents, [])

    def test_exact_child_fallback_context_and_exec_survive_the_join(self):
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(harness=harness):
                source = self.source()
                row = source.jobs[0]
                row.harness, row._detail_activity_key = harness, None
                child = model.Session(harness=harness, pid=row.pid, proc_start=row.proc_start,
                    session_id=row._runtime_session_id, cwd=row.cwd, is_child=True, liveness='working',
                    ctx_pct=36, active_context_tokens=36000, context_window_tokens=100000,
                    exec_tool={'name': 'python child.py'})
                child._context_evidence = model.ContextEvidence(used_pct=36, source='fixture',
                    sequence=(1, 2), source_head_sequence=(1, 2), observed_at=10, fresh_until=9999999999)
                source.sessions.append(child)
                detail = self.filled(source)
                from fleet.collectors import _adopt_child_titles
                _adopt_child_titles(detail.snapshot.sessions, detail.snapshot.jobs)
                projection.attach_projections(detail.snapshot.sessions, detail.snapshot.jobs,
                                              fast_first=True, now=20)
                joined = details.merge(source, detail).jobs[0]
                self.assertTrue(joined._dispatch_context_owned)
                self.assertEqual(joined.context.used_pct, 36)
                self.assertEqual(joined.ctx_pct, 36)
                self.assertEqual(joined.exec_tool, {'name': 'python child.py'})
                # A newer exact child observation cannot receive its old command/context.
                child.updated_at = 100
                joined = details.merge(source, detail).jobs[0]
                self.assertIsNone(joined.exec_tool)
                self.assertIsNone(joined.context)

    def test_changed_wait_reason_or_resource_route_path_rejects_old_wait(self):
        for change in ('reason', 'route_file', 'route'):
            with self.subTest(change=change):
                source = self.source()
                row = source.jobs[0]
                row.liveness = 'idle'
                row.state_evidence = {'inputs': {'observed_liveness': {'state': 'parked-supervised',
                                                                          'reason': 'resource-running'}}}
                resource = model.ResourceJob(run_id='run', parent_attempt_id=row.attempt_id,
                    liveness='working', route_id=row.route_id, route_hash=row.route_hash,
                    route_file='/fixture/route.json', route_node='train')
                source.resources.append(resource)
                detail = self.filled(source)
                detail.snapshot.jobs[0].resource_wait = {'run_ids': ['old-run']}
                if change == 'reason':
                    row.state_evidence['inputs']['observed_liveness'] = {'state': 'waiting-children',
                                                                       'reason': 'owner-idle'}
                else:
                    setattr(resource, change, '/fixture/rebound.json')
                joined = details.merge(source, detail).jobs[0]
                self.assertIsNone(joined.resource_wait)
                self.assertEqual(joined.work_projection.node_state, 'unknown')
                self.assertEqual(joined.campaign_label, 'campaign')

    def test_route_backing_jobs_rebind_to_current_now_in_process_screen(self):
        source = self.source()
        source.jobs[0].route_node = 'execute'
        detail = self.filled(source)
        old_job = detail.snapshot.jobs[0]
        nodes = [{'id': 'execute', 'state': 'active', 'job': old_job, 'harness': old_job.harness,
                  'pid': old_job.pid, 'model': old_job.model, 'effort': old_job.effort,
                  'elapsed_min': 0, 'depends_on': [], 'level': 0, 'gate': None}]
        backing = {'record': {'capability': 'autopilot-code'}, 'nodes': nodes,
                   'view': {'nodes': nodes}}
        old_job.work_projection = model.WorkProjection(source='route-exact', route_id='rt-work',
            route_hash='hash', node_state='active', stage_label='execute', _route_view=backing)
        joined = details.merge(source, detail)
        for shape in (joined.jobs[0].work_projection._route_view,
                      joined.jobs[0].work_projection._route_view['view']):
            self.assertIs(shape['nodes'][0]['job'], joined.jobs[0])
        self.assertIs(backing['nodes'][0]['job'], old_job)
        self.assertEqual(old_job.summary, 'older detail NOW')
        self.addCleanup(setattr, render, '_PROCESS_VIEW', render._PROCESS_VIEW)
        render._PROCESS_VIEW = True
        with mock.patch.object(render, '_fresh_compute_hosts', return_value=(None, None)):
            lines = render._build_lines([], joined.jobs, 'both', False, 0, term_width=168)
        screen = '\n'.join(render._plain(line) for line in lines)
        self.assertIn('fresh basic NOW', screen)
        self.assertNotIn('older detail NOW', screen)

    def test_attempt_pid_start_route_or_native_session_changes_reject_old_detail(self):
        for field, value in (('attempt_id', 'att-new'), ('proc_start', '101'),
                             ('route_hash', 'new-hash'), ('_runtime_session_id', 'native-new')):
            with self.subTest(field=field):
                source = self.source()
                detail = self.filled(source)
                setattr(source.jobs[0], field, value)
                row = details.merge(source, detail).jobs[0]
                self.assertIsNone(row.title)
                self.assertIsNone(row.exec_tool)
                self.assertEqual(row.work_projection.node_state, 'unknown')

    def test_changed_live_roster_preserves_new_state_and_resource_wait(self):
        source = self.source()
        detail = self.filled(source)
        source.jobs[0].liveness = 'idle'
        source.jobs[0].resource_wait = {'run_ids': ['current-run']}
        row = details.merge(source, detail).jobs[0]
        self.assertEqual(row.title, 'title')
        self.assertEqual(row.liveness, 'idle')
        self.assertEqual(row.resource_wait, {'run_ids': ['current-run']})
        self.assertEqual(row.work_projection.node_state, 'unknown')
        self.assertEqual(row.summary, 'fresh basic NOW')
        self.assertIsNone(row.exec_tool)

    def test_unrelated_activity_does_not_starve_route_detail(self):
        source = self.source()
        unrelated = model.Session(harness='claude', pid=70, proc_start='200',
                                  session_id='unrelated', cwd='/other', liveness='working')
        source.sessions.append(unrelated)
        detail = self.filled(source)
        unrelated.liveness = 'idle'
        source.sessions.append(model.Session(harness='codex', pid=71, proc_start='201',
                                            session_id='new-unrelated', cwd='/other', liveness='working'))
        row = details.merge(source, detail).jobs[0]
        self.assertEqual(row.work_projection.stage_label, 'test')
        self.assertEqual(row.summary, 'fresh basic NOW')

    def test_successful_empty_native_now_read_says_no_summary_yet(self):
        source = self.source()
        source.jobs[0].summary, source.jobs[0].summary_ts = None, None
        detail = self.filled(source)
        detail.snapshot.jobs[0].summary = None
        detail.snapshot.jobs[0].exec_tool = None
        row = details.merge(source, detail).jobs[0]
        text = render._plain(render._context_detail_row(row, term_width=168)[0])
        self.assertIn('no summary yet', text)
        self.assertNotIn('loading', text)
        self.assertEqual(row.liveness, 'working')


    def test_related_route_child_change_rejects_old_projection(self):
        source = self.source()
        child = model.DispatchJob(key='code-test', attempt_id='att-child', route_id='rt-work',
                                  route_hash='hash', liveness='working')
        source.jobs.append(child)
        detail = self.filled(source)
        child.liveness = 'done'
        row = details.merge(source, detail).jobs[0]
        self.assertIsNone(row.work_projection.stage_label)
        self.assertEqual(row.title, 'title')



class LiveLoopTest(unittest.TestCase):
    def test_real_basic_rows_refresh_while_details_block_then_detail_arrives(self):
        entered, release, basic_second, detail_drawn = [threading.Event() for _ in range(4)]
        self.addCleanup(release.set)
        self.addCleanup(setattr, render, '_REFRESH_HEALTH', render._REFRESH_HEALTH)
        self.addCleanup(setattr, render, '_BLINK_ON', render._BLINK_ON)
        calls, fast_calls, drawn = [], [], []

        def collector(harness_filter=None, fast_first=False):
            calls.append(len(calls) + 1)
            fast_calls.append(fast_first)
            row = model.Session(harness='codex', pid=42, proc_start='100', session_id='exact',
                                liveness='working', title=None)
            row.summary = 'basic-%d' % len(calls)
            row.work_projection = model.WorkProjection(source='none', node_state='unknown')
            return [row], []
        collector.last_resource_jobs = []
        collector.last_usage_snapshots = {}

        def fill(source):
            entered.set()
            self.assertTrue(release.wait(5))
            value = copy.deepcopy(source)
            value.sessions[0].title = 'filled title'
            return details.DetailSnapshot(details.state_key(source), value)
        collector.detail_refresh = fill

        def draw(screen, sessions, jobs, *args, **kwargs):
            if not kwargs.get('loading') and sessions:
                drawn.append((sessions[0].summary, sessions[0].title))
                if sessions[0].summary != 'basic-1':
                    basic_second.set()
                if sessions[0].title == 'filled title':
                    detail_drawn.set()

        class Screen:
            def timeout(self, value):
                pass
            def getch(self):
                time.sleep(.01)
                return ord('q') if detail_drawn.is_set() else -1
            def getmaxyx(self):
                return 50, 168

        result = []
        with mock.patch.object(render, '_init_colors'), \
                mock.patch.object(render.curses, 'curs_set'), \
                mock.patch.object(render, '_draw', side_effect=draw), \
                mock.patch.object(render, '_malformed', return_value=0), \
                mock.patch.object(render, '_collect_governor', return_value=None), \
                mock.patch.object(render, '_collect_memory', return_value=None), \
                mock.patch.object(render.gitinfo, 'enrich_entities'):
            worker = threading.Thread(target=lambda: result.append(render._loop(Screen(), collector, None, 'both', .1)))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(basic_second.wait(2), 'detail blocked a fresh real state')
                self.assertFalse(detail_drawn.is_set())
                release.set()
                self.assertTrue(detail_drawn.wait(2), 'completed detail never arrived')
            finally:
                release.set()
                detail_drawn.set()
                worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [0])
        self.assertTrue(all(fast_calls))
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(any(title is None for now, title in drawn))


if __name__ == '__main__':
    unittest.main()
