"""D: easy titles reach actual result/resource rows without runtime-specific rules."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import details, model, render, route, work_titles
from fleet.refresh import LiveSnapshot
from fleet.collectors import dispatch
from fleet.tests.test_fleet_hot_path import _build_root, CAMP, CYC

ROOT = 'root_' + 'c' * 32


class WorkTitlesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = _build_root(Path(self.temp.name))
        producer = self.root / '.runtime/artifact-producer/v1'
        (producer / 'cycles').mkdir(parents=True)
        self.record = dict(route_id='rt-one', cycle_id=CYC, campaign_id=CAMP,
                           title='fleet-freshness-p2-1010', slug='fleet-freshness-p2-1010')
        (producer / 'cycles' / (CYC + '.json')).write_text(json.dumps(self.record))
        admission = self.root / '.runtime/artifact-admission/v1'
        admission.mkdir(parents=True)
        (admission / 'root-identity.json').write_text(json.dumps({'artifact_root_id': ROOT}))
        self.meta_path = self.root / 'campaigns' / CAMP / 'meta.json'
        self.meta = dict(schema_version=1, contract='artifact-meta/v1', artifact_root_id=ROOT,
                         campaign_id=CAMP, campaign={'title': '화면 관측 개선'},
                         cycles={CYC: {'title': '최근 결과의 시각 표시'}})
        self.save_meta()
        for cache in (dispatch._CYCLE_RECORD_CACHE, dispatch._CYCLE_ROUTE_PATHS,
                      dispatch._CYCLE_LABEL_INVENTORIES):
            cache.clear()

    def save_meta(self):
        self.meta_path.write_text(json.dumps(self.meta, ensure_ascii=False))

    def test_title_validation_reuses_observed_documents_and_invalidates_changed_member(self):
        import artifact_meta
        extra = 'cyc_' + 'd' * 32
        self.meta['cycles'][extra] = {'title': 'Other cycle'}
        self.save_meta()
        path = self.root / '.runtime/artifact-producer/v1/cycles' / (extra + '.json')
        member = {'cycle_id': extra, 'campaign_id': CAMP}
        path.write_text(json.dumps(member))
        job = self.job()
        with mock.patch.object(artifact_meta, '_validate_meta_doc', wraps=artifact_meta._validate_meta_doc) as validate:
            dispatch._campaign_labels([job])
            original = job.campaign_label
            dispatch._campaign_labels([job])
            self.assertEqual(validate.call_count, 1)
            before = path.stat()
            member['campaign_id'] = 'camp_' + 'e' * 32
            path.write_text(json.dumps(member))
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            dispatch._campaign_labels([job])
            self.assertEqual(validate.call_count, 2)
            self.assertNotEqual(job.campaign_label, original)
            oracle = work_titles.cycle_title(self.root, self.record,
                                             lambda value: dispatch._read_cycle_record(value))
            self.assertEqual(job.campaign_label, oracle)

    def test_complete_cycle_hint_avoids_negative_sweep_and_directory_change_reopens_it(self):
        import directory_record_index as index
        directory = self.root / '.runtime/artifact-producer/v1/cycles'
        hint = directory / '.cycle-routes-index.json'
        index._save(hint, index._listing(directory, ()), {'rt-one': [CYC + '.json']})
        job = self.job()
        job.route_id = 'rt-new'
        with mock.patch.object(dispatch, '_read_cycle_record',
                               side_effect=AssertionError('negative locator swept history')):
            dispatch._campaign_labels([job])
        self.assertIsNone(job.campaign_label)
        record = {**self.record, 'route_id': 'rt-new', 'cycle_id': 'cyc_' + 'e' * 32,
                  'title': 'New cycle title'}
        (directory / (record['cycle_id'] + '.json')).write_text(json.dumps(record))
        # The old index was not updated: its listing no longer covers the source.
        dispatch._campaign_labels([job])
        self.assertEqual(job.campaign_label, 'New cycle title')

    def job(self, harness='codex'):
        return model.DispatchJob(key='code', slug=self.record['slug'], harness=harness,
                                 route_id='rt-one', artifact_root=str(self.root))

    def test_cycle_easy_title_wins_and_edits_refresh_for_every_harness(self):
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(harness=harness):
                job = self.job(harness)
                dispatch._campaign_labels([job])
                self.assertEqual(job.campaign_label, '최근 결과의 시각 표시')
        self.meta['cycles'][CYC]['title'] = '바뀐 쉬운 제목'
        self.save_meta()
        job = self.job()
        dispatch._campaign_labels([job])
        self.assertEqual(job.campaign_label, '바뀐 쉬운 제목')

    def test_summary_and_campaign_fallback(self):
        self.meta['cycles'][CYC] = {'summary': '완료 시각을 정확히 표시함'}
        self.save_meta()
        job = self.job()
        dispatch._campaign_labels([job])
        self.assertEqual(job.campaign_label, '완료 시각을 정확히 표시함')
        self.meta['cycles'] = {}
        self.save_meta()
        dispatch._campaign_labels([job])
        self.assertEqual(job.campaign_label, '화면 관측 개선')

    def test_slug_fallback_removes_locator_date_and_execution_suffix(self):
        outcome = dict(present=True, match=True, terminal_gate_proven=True,
                       closed_at='2026-10-10T00:00:00Z')
        result = route.result_projection({'slug': '2026-10-10_fleet-freshness-p2-1010'}, outcome)
        self.assertEqual(result['name'], 'fleet freshness p2')

    def test_resource_and_gpu_use_same_title(self):
        child = model.ResourceJob(run_id='mgpu-a1-20261008-203517-css-AMI8var-v3',
                                  node='train', liveness='working')
        child.display_title = '환경별 모델 비교'
        rows = render._resource_child_rows(self.job(), children=[child])
        line = '\n'.join(render._plain(row) for row in rows)
        self.assertIn(child.display_title, line)
        self.assertIn(child.display_title, render._resource_gpu_suffix([child]))
        self.assertNotIn(child.run_id, line)

    def test_now_does_not_repeat_same_command_as_title(self):
        child = model.ResourceJob(run_id='run', node='train', liveness='working',
                                  command=['python', 'train.py'])
        child.display_title = 'train.py'
        job = self.job()
        job.liveness = 'idle'
        job.resource_children = [child]
        with mock.patch.object(render, '_fresh_compute_hosts', return_value=(None, None)):
            text = render._resource_now_text(job)
        self.assertEqual(text.count('train.py'), 1)

    def test_gpu_does_not_repeat_same_command_as_title(self):
        child = model.ResourceJob(run_id='run', node='train', liveness='working',
                                  display_title='train.py', elapsed_min=3)
        gpu = dict(host='mgpu-a1', index=1, processes=[dict(pid=42, proc_start='100', command='python train.py')])
        with mock.patch.object(render, '_gpu_commands_folded', return_value=False):
            rows = render._gpu_resource_strip([gpu], term_width=160, resource_children=[child])
        self.assertEqual(render._plain(rows[0]).count('train.py'), 1)

    def test_gpu_shared_cycle_title_once_keeps_each_run_progress(self):
        title = '환경별 모델 비교'
        children = [model.ResourceJob(run_id='run-' + node, node=node,
            artifact_root=str(self.root), route_id='rt-one', display_title=title,
            liveness='working', elapsed_min=elapsed,
            progress=dict(completed=completed, total=10, unit='epoch', age_s=age))
            for node, elapsed, completed, age in (('train', 3, 2, 120), ('eval', 10, 4, 180))]
        gpu = dict(host='mgpu-a1', index=1, processes=[])
        rows = render._gpu_resource_strip([gpu], term_width=240, resource_children=children)
        text = render._plain(rows[0])
        self.assertEqual(text.count(title), 1)
        for fact in ('train 3m', 'eval 10m', '2/10 epoch', '4/10 epoch', '2m ago', '3m ago'):
            self.assertIn(fact, text)
        self.assertEqual([child.run_id for child in children], ['run-train', 'run-eval'])

    def test_gpu_identical_titles_on_different_routes_remain_distinct(self):
        for root, rid in ((str(self.root), 'rt-other'), ('/another/root', 'rt-one'), ('', 'rt-one')):
            with self.subTest(root=root, route_id=rid):
                children = [model.ResourceJob(run_id='run-' + str(index), node='train',
                    artifact_root=child_root, route_id=child_rid, display_title='환경별 모델 비교',
                    liveness='working', elapsed_min=3)
                    for index, (child_root, child_rid) in enumerate(((str(self.root), 'rt-one'), (root, rid)))]
                self.assertEqual(render._resource_gpu_suffix(children).count('환경별 모델 비교'), 2)

    def test_historical_result_keeps_its_exact_title_after_current_route_changes(self):
        result = route.result_projection(dict(slug=self.record['slug'], route_id='rt-one',
                                             artifact_root=str(self.root)),
                                        dict(present=True, match=True, terminal_gate_proven=True,
                                             closed_at='2026-10-10T00:00:00Z'))
        session = model.Session(harness='codex', pid=123, cwd='/work/project', liveness='idle')
        session.route_chain = {'nodes': [{'result': result}], 'current': {'route_id': 'rt-later'}}
        work_titles.annotate([session], [], [])
        self.assertEqual(result['name'], '최근 결과의 시각 표시')
        self.assertEqual(result['at'], 1791590400.0)
        self.assertEqual(result['result'], 'success')
        lines = render._build_lines([session], [], 'both', False, 0, term_width=160)
        screen = '\n'.join(render._plain(row) for row in lines)
        self.assertIn('✓ 최근 결과의 시각 표시', screen)

    def test_invalid_or_foreign_metadata_falls_back_without_mutation(self):
        for update in ({'artifact_root_id': 'foreign'}, {'campaign_id': 'foreign'},
                       {'cycles': {CYC: {'title': '색\x1b[31m'}}}):
            with self.subTest(update=update):
                payload = json.dumps(dict(self.meta, **update))
                self.meta_path.write_text(payload)
                dispatch._campaign_labels([job := self.job()])
                self.assertEqual(job.campaign_label, 'hot path fixture')
                self.assertEqual(self.meta_path.read_text(), payload)
        self.meta_path.unlink()
        dispatch._campaign_labels([job])
        self.assertEqual(job.campaign_label, 'hot path fixture')

    def test_detail_title_join_keeps_new_liveness_and_rejects_pid_reuse(self):
        child = model.ResourceJob(run_id='run', route_id='rt-one', artifact_root=str(self.root),
                                  pid=42, starttime='100', liveness='working')
        basic = LiveSnapshot(resources=[child])
        titled = model.ResourceJob(**child.to_dict())
        titled.display_title = '환경별 모델 비교'
        detail = details.DetailSnapshot(details.state_key(basic), LiveSnapshot(resources=[titled]))
        child.liveness = 'exited'
        merged = details.merge(basic, detail)
        self.assertEqual(merged.resources[0].display_title, titled.display_title)
        self.assertEqual(merged.resources[0].liveness, 'exited')
        child.starttime = '200'
        self.assertIsNone(details.merge(basic, detail).resources[0].display_title)

    def test_detail_merge_keeps_one_resource_shared_with_owner(self):
        child = model.ResourceJob(run_id='run', pid=42, starttime='100', liveness='working')
        job = self.job()
        job.resource_children = [child]
        basic = LiveSnapshot(jobs=[job], resources=[child])
        import copy
        detail_snapshot = copy.deepcopy(basic)
        detail_snapshot.resources[0].display_title = '학습 비교'
        detail = details.DetailSnapshot(details.state_key(basic), detail_snapshot)
        merged = details.merge(basic, detail)
        self.assertIs(merged.jobs[0].resource_children[0], merged.resources[0])
        self.assertEqual(merged.resources[0].display_title, '학습 비교')

    def test_known_campaign_reads_no_inventory_and_render_reads_no_metadata(self):
        reader = dispatch.artifact_reader
        jobs = [self.job(harness) for harness in ('claude', 'codex', 'opencode')]
        with mock.patch.object(reader.artifact_locator, 'scan_index', wraps=reader.artifact_locator.scan_index) as scan:
            dispatch._campaign_labels(jobs)
        self.assertEqual(scan.call_count, 0)
        child = model.ResourceJob(run_id='run', display_title=jobs[0].campaign_label, liveness='working')
        with mock.patch.object(work_titles, 'cycle_title', side_effect=AssertionError('render IO')):
            self.assertIn(child.display_title, render._plain(render._resource_child_rows(jobs[0], children=[child])[0]))

    def test_invalid_route_title_falls_back_to_cleaned_slug(self):
        result = route.result_projection(dict(slug=self.record['slug'], title=42),
            dict(present=True, match=True, terminal_gate_proven=True, closed_at='2026-10-10T00:00:00Z'))
        self.assertEqual(result['name'], 'fleet freshness p2')

    def test_automatically_recorded_campaign_title_uses_cleaned_cycle_name(self):
        self.meta_path.unlink()
        campaign_path = self.meta_path.with_name('campaign.json')
        campaign = json.loads(campaign_path.read_text())
        campaign.update(title='2026-10-10_fleet-freshness-p2-1010',
                        slug='2026-10-10_fleet-freshness-p2-1010')
        campaign_path.write_text(json.dumps(campaign))
        dispatch._campaign_labels([job := self.job()])
        self.assertEqual(job.campaign_label, 'fleet freshness p2')

    def test_invalid_or_foreign_cycle_declaration_is_ignored(self):
        self.meta_path.unlink()
        identity = self.root / '.runtime/artifact-admission/v1/root-identity.json'
        identity.write_text(json.dumps(dict(artifact_root_id=ROOT, repository_id='repo_' + 'd' * 32)))
        declaration = self.root / '.runtime/artifact-producer/v1/cycle-display-titles.json'
        for schema in ('invalid', 'hearting-cycle-display-titles/v1'):
            declaration.write_text(json.dumps(dict(schema=schema, artifact_root_id=ROOT,
                repository_id='foreign', entries=[dict(campaign_id=CAMP, cycle_id=CYC, display_title='무관한 제목')])))
            dispatch._campaign_labels([job := self.job()])
            self.assertEqual(job.campaign_label, 'hot path fixture')


if __name__ == '__main__':
    unittest.main()
