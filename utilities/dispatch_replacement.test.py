#!/usr/bin/env python3
"""Proof, race and crash falsifiers for automatic replacement (no model calls)."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import dispatch_replacement as R
import dispatch_contract as D
import route_identity


class ReplacementTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.jobs=self.root/'jobs.log';self.jobs.touch()
        seat_state=mock.patch('session_tidy.state_root',return_value=self.root/'seat-state')
        seat_state.start();self.addCleanup(seat_state.stop)
        self.route={'route_id':'rt-test','route_hash':'sha256:test','artifact_root':str(self.root),
                    'cwd':str(self.root),'capability':'autopilot-code','nodes':[]}
        self.path=self.root/'route.json';self.path.write_text(json.dumps(self.route))
        self.route_mock=mock.patch.object(R,'_route',return_value=(self.path,self.route));self.route_mock.start();self.addCleanup(self.route_mock.stop)
        self.logical=mock.patch.object(R,'_logical_key',side_effect=lambda r,m:{'root_route_id':'rt-root','node':'__owner__' if m.get('worker_type')=='owner' else m['route_node']});self.logical.start();self.addCleanup(self.logical.stop)
        self.reuse=mock.patch.object(R,'_reuse_snapshot',return_value={'completed':[],'cycle_id':'cyc-test','producer_id':'prod-test','gate_releases':[]});self.reuse.start();self.addCleanup(self.reuse.stop)
        self.quiet=mock.patch.object(D,'attempt_process_quiescence',return_value=SimpleNamespace(state='quiescent',reason='process-absent'));self.quiet.start();self.addCleanup(self.quiet.stop)
        self.absent=mock.patch.object(R,'_terminal_absent',return_value=True);self.absent.start();self.addCleanup(self.absent.stop)
        self.meta={'attempt_schema_version':'2','dispatch_depth':'1','transport':'headless',
             'execution_surface':'registered-headless','registered_worker':'1','fallback_hop':'same-harness-headless',
             'attempt_id':'att-source','route_id':'rt-test','route_hash':'sha256:test','route_node':'frame',
             'worker_type':'frame','parent_sid':'parent','harness':'codex','note':'dead-exact-pid',
             'failure_class':'contract','launch_outcome':'never-launched'}
        self.args=args=SimpleNamespace(attempt_id='att-source',jobs_path=self.jobs,worktree=str(self.root),route_id='rt-test',route_node='frame',replacement_input_argv=['--start','--attempt-id','att-source','--prompt-text','the raw task'])
        fragment=R.seal_launch_input(args,'codex','the raw task')
        self.meta.update(D.parse_registry_metadata(fragment));self.write(self.meta)

    def write(self,meta,status='done',append=False,stamp='now'):
        line=stamp+'\t'+status+'\t'+str(self.root)+'\t'+str(self.root)+'\ttask\t'+','.join(k+'='+v for k,v in meta.items())+'\n'
        with self.jobs.open('a' if append else 'w') as f:f.write(line)

    def claim(self):return R.claim(self.jobs,'att-source')

    def bind_source_parent(self, *, carrier=False):
        self.args.dispatch_depth=1
        self.args.execution_surface='registered-headless';self.args.registered_worker=True
        R.route_authority.bind_runtime_parent(self.args,environ={'CODEX_THREAD_ID':'parent'})
        if carrier:self.args.parent_completion_delivery='codex-native-queue'
        self.meta['parent_harness']='codex'
        # Replace the original temporary fixture before it has any claim or launch.
        (R._directory(self.jobs)/'inputs/att-source.json').unlink()
        self.meta.update(D.parse_registry_metadata(R.seal_launch_input(self.args,'codex','the raw task')))
        self.write(self.meta)

    def parent_env(self, parent, harness):
        from harness_capabilities import CARRIER_ENV, parent_completion
        variable={'claude':'CLAUDE_SESSION_ID','codex':'CODEX_THREAD_ID','opencode':'OPENCODE_SESSION_ID'}[harness]
        return {variable:parent,'AGENT_DISPATCH_CALLER_HARNESS':harness,
                CARRIER_ENV:f"{parent_completion(harness)['carrier']}:{parent}"}

    def handover(self, successor='successor', harness='claude', **binding_changes):
        import dispatch_seat_handover as H
        import session_tidy as S
        seat=S.Seat('pane','replacement-test-seat','wY:p5')
        binding={**H.binding_of(self.jobs,self.meta),**binding_changes}
        snapshot={'schema':H.SCHEMA,'seat':{'kind':'pane','key':seat.key,'pane':seat.pane},
                  'from':{'sid':self.meta['parent_sid']},'bindings':[binding]}
        S.atomic_write_json(H._snapshot_path(seat.key),snapshot)
        path=S._ledger_path(seat);path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({'event':'handover','from':self.meta['parent_sid'],
                                   'sid':successor,'harness':harness,'ts':1,'bindings':[binding]})+'\n')

    def replacement_candidate(self, record, parent='successor', harness='claude'):
        source=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        replay=R.launch_input(self.jobs,'att-source',source)
        args=SimpleNamespace(**vars(self.args));args.attempt_id=record['replacement_attempt_id']
        args.replacement_input_argv=R._replacement_argv(record,source,replay)
        if hasattr(args,'parent_session_id'):
            with mock.patch.dict(os.environ,self.parent_env(parent,harness),clear=True):
                R.route_authority.bind_runtime_parent(args)
                if hasattr(args,'parent_completion_delivery'):
                    from dispatch_parent_completion import resolve_parent_completion_delivery
                    args.action='start';args.parent_completion_delivery=resolve_parent_completion_delivery(args)
        candidate={**self.meta,'attempt_id':record['replacement_attempt_id'],
                   'automatic_retry_of':'att-source','parent_sid':parent,'parent_harness':harness}
        candidate.update(D.parse_registry_metadata(R.seal_launch_input(args,'codex','the raw task')))
        return candidate

    def test_handover_owner_replacement_registers_and_lineage_reuses_the_same_claim(self):
        import review_round_cap as ROUND
        self.meta.update(worker_type='owner',owner_route_id='rt-test',owner_route_hash='sha256:test')
        self.bind_source_parent()
        self.handover()
        record=self.claim();candidate=self.replacement_candidate(record)
        before=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        lines=self.jobs.read_text().splitlines()
        self.assertEqual(R.admission(self.jobs,lines,candidate),record)
        row='now\topen\t'+str(self.root)+'\t'+str(self.root)+'\treplacement\t'+','.join(k+'='+v for k,v in candidate.items())
        self.assertTrue(D.claim_attempt_row(self.jobs,candidate['attempt_id'],row,launch=False))
        self.assertFalse(D.claim_attempt_row(self.jobs,candidate['attempt_id'],row,launch=False))
        registered=R._rows(self.jobs.read_text().splitlines())
        self.assertEqual(registered['att-source'][1],before)
        self.assertEqual(R.admission(self.jobs,self.jobs.read_text().splitlines(),registered[candidate['attempt_id']][1]),record)
        effective,mapping=R.effective_attempts(self.jobs,{'att-source'})
        self.assertEqual(effective,{candidate['attempt_id']})
        self.assertEqual(mapping[0]['replacement_attempt_id'],record['replacement_attempt_id'])
        projected=ROUND.logical_round_records(list(registered.values()),jobs=self.jobs)
        self.assertEqual([meta['attempt_id'] for _,meta in projected],[candidate['attempt_id']])
        self.assertEqual(self.claim(),record)
        self.assertEqual(len(list((R._directory(self.jobs)/'claims').glob('*.json'))),1)
        # B's registered edge survives B -> C; B no longer admits a fresh launch.
        import dispatch_seat_handover as H
        import session_tidy as S
        ledger=S._ledger_path(S.Seat('pane','replacement-test-seat','wY:p5'))
        with ledger.open('a') as handle:
            handle.write(json.dumps({'event':'handover','from':'successor','sid':'next-successor',
                                     'harness':'opencode','ts':2,
                                     'bindings':[H.binding_of(self.jobs,registered[candidate['attempt_id']][1])]})+'\n')
        self.assertEqual(R.effective_attempts(self.jobs,{'att-source'}),(effective,mapping))
        projected=ROUND.logical_round_records(list(registered.values()),jobs=self.jobs)
        self.assertEqual([meta['attempt_id'] for _,meta in projected],[candidate['attempt_id']])
        self.assertFalse(R.route_authority.replacement_parent_matches(registered['att-source'][1],candidate,self.jobs))
        self.assertEqual(self.claim(),record)

    def test_handover_replacement_admission_is_shared_by_all_parent_harnesses(self):
        self.bind_source_parent(carrier=True)
        record=self.claim()
        for harness in ('claude','codex','opencode'):
            with self.subTest(harness=harness):
                (R._directory(self.jobs)/'inputs'/(record['replacement_attempt_id']+'.json')).unlink(missing_ok=True)
                self.handover(harness=harness)
                candidate=self.replacement_candidate(record,harness=harness)
                from harness_capabilities import parent_completion
                sealed=R.launch_input(self.jobs,candidate['attempt_id'],candidate)
                self.assertEqual(sealed['resolved']['parent_completion_delivery'],parent_completion(harness)['carrier'])
                with mock.patch.dict(os.environ,self.parent_env('successor',harness),clear=True):
                    self.assertEqual(R.admission(self.jobs,self.jobs.read_text().splitlines(),candidate),record)

    def test_handover_launch_input_changes_only_the_confirmed_parent_values(self):
        self.bind_source_parent();self.handover()
        record=self.claim();candidate=self.replacement_candidate(record)
        path=R._directory(self.jobs)/'inputs'/(candidate['attempt_id']+'.json')
        original=json.loads(path.read_text())
        mutations=[('resolved',{key:'foreign'}) for key in
                   ('parent_session_id','parent_harness','parent_attempt_id','parent_transport','parent_sandbox','sandbox')]
        mutations += [('argv',R._canonical_argv(R._replace_options(original['argv'],{'--parent-session-id':'foreign'}))),
                      ('applied_permissions',{'execution_access':{'request_sha256':'foreign'}})]
        for field,value in mutations:
            with self.subTest(field=field,value=value):
                payload=json.loads(json.dumps(original))
                if field=='resolved':payload[field].update(value)
                else:payload[field]=value
                path.write_text(json.dumps(payload))
                changed={**candidate,'replacement_input_digest':R._digest(payload)}
                with self.assertRaises(D.DispatchContractError) as refused:
                    R.admission(self.jobs,self.jobs.read_text().splitlines(),changed)
                self.assertEqual(refused.exception.reason,
                                 'replacement-argv-mismatch' if field=='argv' else 'replacement-input-tuple-mismatch')
        path.write_text(json.dumps(original))
        self.assertEqual(R.admission(self.jobs,self.jobs.read_text().splitlines(),candidate),record)

    def test_handover_admission_keeps_unrelated_parents_and_other_bindings_strict(self):
        self.bind_source_parent()
        self.handover()
        record=self.claim();candidate=self.replacement_candidate(record)
        lines=self.jobs.read_text().splitlines()
        for change in ({'parent_sid':'foreign'},{'parent_attempt_id':'foreign-attempt'},
                       {'route_hash':'sha256:other'},{'route_node':'other'},{'dispatch_depth':'2'},
                       {'subsession_id':'other'},{'phase_brief_sha256':'other'},
                       {'fixed_inputs_sha256':'other'},{'narrow_verify_sha256':'other'}):
            with self.subTest(change=change),self.assertRaises(D.DispatchContractError) as refused:
                R.admission(self.jobs,lines,{**candidate,**change})
            self.assertEqual(refused.exception.reason,'replacement-launch-binding-mismatch')
        self.assertFalse(any(fields[1]=='open' for fields,_ in R._rows(lines).values()))

    def test_handover_for_a_different_route_node_or_registry_does_not_authorize(self):
        self.bind_source_parent()
        record=self.claim();candidate=self.replacement_candidate(record)
        for change in ({},{'route':'rt-other'},{'hash':'sha256:other'},{'node':'other'},
                       {'jobs':str(self.root/'foreign-jobs.log')}):
            with self.subTest(change=change):
                if change:self.handover(**change)
                with self.assertRaises(D.DispatchContractError) as refused:
                    R.admission(self.jobs,self.jobs.read_text().splitlines(),candidate)
                self.assertEqual(refused.exception.reason,'replacement-launch-binding-mismatch')

    def test_node_replay_accepts_only_the_recorded_depth1_parent_when_its_label_changes(self):
        spec=importlib.util.spec_from_file_location('replacement_handover_node',Path(__file__).with_name('dispatch-node.py'))
        node_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(node_module)
        self.meta['parent']='old-parent-label';self.write(self.meta)
        self.handover()
        record=self.claim()
        args=SimpleNamespace(adapter_args=['--','--automatic-retry-of','att-source'],
                             attempt_id=record['replacement_attempt_id'],adapter='codex',parent='new-parent-label')
        for session in ('parent','successor'):
            with self.subTest(session=session),mock.patch.object(node_module.ROUTE_AUTHORITY,'default_parent_session_id',return_value=session):
                self.assertEqual(node_module.replacement_task(args,self.route,{'id':'frame'},self.jobs),'the raw task')
        with mock.patch.object(node_module.ROUTE_AUTHORITY,'default_parent_session_id',return_value='foreign'):
            with self.assertRaises(D.DispatchContractError) as refused:
                node_module.replacement_task(args,self.route,{'id':'frame'},self.jobs)
            self.assertEqual(refused.exception.reason,'replacement-launch-binding-mismatch')
        # The same ledger is never authority to change a depth-2 owner's label or attempt.
        self.meta.update(dispatch_depth='2',parent_attempt_id='att-owner');self.write(self.meta)
        source={**self.meta}
        with mock.patch.object(R,'validate_claim_source',return_value=([],source,{'task':'the raw task'})), \
                mock.patch.object(node_module.ROUTE_AUTHORITY,'default_parent_session_id',return_value='parent'):
            with self.assertRaises(D.DispatchContractError):
                node_module.replacement_task(args,self.route,{'id':'frame'},self.jobs)

    def test_depth1_replacement_is_authorized_for_the_launching_session_or_its_same_seat_successor(self):
        import work_start
        meta={**self.meta,'dispatch_depth':'1'}
        with mock.patch.object(work_start,'_current_parent_session_id',return_value='parent'):
            R._authorized(self.jobs,{},meta)
        with mock.patch.object(work_start,'_current_parent_session_id',return_value='successor'):
            with self.assertRaises(D.DispatchContractError) as refused:
                R._authorized(self.jobs,{},meta)
            self.assertEqual(refused.exception.reason,'replacement-parent-identity-unproven')
            with mock.patch('dispatch_seat_handover.owns',side_effect=lambda m,session,jobs=None:session=='successor'):
                R._authorized(self.jobs,{},meta)

    def test_concurrent_consumers_share_one_claim_and_original_failure(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            records=list(pool.map(lambda _:self.claim(),range(16)))
        self.assertEqual(len({r['replacement_attempt_id'] for r in records}),1)
        self.assertEqual(len(list((R._directory(self.jobs)/'claims').glob('*.json'))),1)
        meta=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        self.assertEqual((meta['note'],meta['failure_class']),('dead-exact-pid','contract'))
        self.assertEqual(len(self.jobs.read_text().splitlines()),1)

    def test_crash_after_record_before_annotation_reuses_claim(self):
        with mock.patch.object(R,'_bind_source',side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):self.claim()
        self.assertNotIn('replacement_family_id',self.jobs.read_text())
        saved=next((R._directory(self.jobs)/'claims').glob('*.json')).read_text()
        result=self.claim()
        self.assertEqual(json.loads(saved),result)
        self.assertIn('replacement_family_id',self.jobs.read_text())

    def test_live_or_unknown_never_claims(self):
        for state in ['live','unverifiable']:
            with self.subTest(state=state),mock.patch.object(D,'attempt_process_quiescence',return_value=SimpleNamespace(state=state,reason='proof')):
                with self.assertRaisesRegex(D.DispatchContractError,'proof'):self.claim()
        self.assertFalse((R._directory(self.jobs)/'claims').exists())

    def test_semantic_result_and_invalid_handoff_never_replace(self):
        with mock.patch.object(R,'_terminal_absent',return_value=False):
            with self.assertRaises(D.DispatchContractError) as caught:self.claim()
        self.assertEqual(caught.exception.reason,'replacement-result-settlement-required')
        for note in ['dead-worker-fail','completed-review-blocking','dead-invalid-envelope','cancelled-by-user']:
            self.write({**self.meta,'note':note})
            with self.subTest(note=note),self.assertRaises(D.DispatchContractError):self.claim()
        self.assertFalse((R._directory(self.jobs)/'claims').exists())

    def test_owner_open_child_vetoes_even_if_process_seems_quiet(self):
        self.write({**self.meta,'worker_type':'owner'})
        self.write({**self.meta,'attempt_id':'att-child','parent_attempt_id':'att-source'},'open',append=True)
        with self.assertRaises(D.DispatchContractError) as caught:self.claim()
        self.assertEqual(caught.exception.reason,'replacement-owner-child-unsettled')

    def test_terminal_fence_vetoes_claim(self):
        with mock.patch.object(D,'ensure_terminal_claim_absent',side_effect=D.DispatchContractError('terminal-claim-pending')):
            with self.assertRaises(D.DispatchContractError):self.claim()
        self.assertFalse((R._directory(self.jobs)/'claims').exists())

    def test_missing_or_changed_input_never_claims(self):
        path=R._directory(self.jobs)/'inputs/att-source.json';path.write_text('{}')
        with self.assertRaises(D.DispatchContractError) as caught:self.claim()
        self.assertEqual(caught.exception.reason,'replacement-input-unproven')
        self.assertFalse((R._directory(self.jobs)/'claims').exists())

    def test_replacement_failure_cannot_claim_a_second_replacement(self):
        record=self.claim();self.write({**self.meta,'attempt_id':record['replacement_attempt_id'],
             'automatic_retry_of':'att-source','replacement_family_id':record['family_id']},append=True)
        with self.assertRaises(D.DispatchContractError) as caught:R.claim(self.jobs,record['replacement_attempt_id'])
        self.assertEqual(caught.exception.reason,'automatic-replacement-exhausted')
        self.assertEqual(len(list((R._directory(self.jobs)/'claims').glob('*.json'))),1)

    def test_registration_admission_requires_exact_claim_and_fresh_death(self):
        record=self.claim();candidate={**self.meta,'attempt_id':record['replacement_attempt_id'],'automatic_retry_of':'att-source'}
        source=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        replay=R.launch_input(self.jobs,'att-source',source)
        args=SimpleNamespace(**vars(self.args));args.attempt_id=record['replacement_attempt_id']
        args.replacement_input_argv=R._replacement_argv(record,source,replay)
        candidate.update(D.parse_registry_metadata(R.seal_launch_input(args,'codex','the raw task')))
        lines=self.jobs.read_text().splitlines()
        self.assertEqual(R.admission(self.jobs,lines,candidate),record)
        for change in [{'attempt_id':'att-invented'},{'parent_sid':'other'},{'route_hash':'sha256:other'}]:
            with self.subTest(change=change),self.assertRaises(D.DispatchContractError):R.admission(self.jobs,lines,{**candidate,**change})
        with mock.patch.object(D,'attempt_process_quiescence',return_value=SimpleNamespace(state='live',reason='live-now')):
            with self.assertRaises(D.DispatchContractError):R.admission(self.jobs,lines,candidate)

    def test_sd161_first_replacement_claim_keeps_semantic_round_at_bound(self):
        import review_input
        node={'id':'frame','kind':'review-worker','unit':'qa/plan-review','depends_on':[]}
        self.route.update(nodes=[node],effective_intensity='standard')
        self.path.write_text(json.dumps(self.route))
        evidence=self.root/'plan.md';evidence.write_text('sealed review input')
        self.meta.update(unit='qa/plan-review',route_file=str(self.path))
        candidate_input=review_input.resolve_input(self.route,node,self.jobs,evidence)
        self.meta[review_input.KEY]=review_input.seal_binding(self.jobs,self.meta,candidate_input)
        # This fixture's original launch predates the test-specific review tuple.
        (R._directory(self.jobs)/'inputs/att-source.json').unlink()
        self.args.reviewed_evidence=str(evidence)
        self.meta.update(D.parse_registry_metadata(R.seal_launch_input(self.args,'codex','the raw task')))
        self.write(self.meta)
        self.write(dict(self.meta,attempt_id='att-prior-crash'),'done',append=True)
        (self.root/'review-input-revisions').mkdir()
        record=self.claim();aid=record['replacement_attempt_id']
        source=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        replay=R.launch_input(self.jobs,'att-source',source)
        args=SimpleNamespace(**vars(self.args));args.attempt_id=aid
        args.replacement_input_argv=R._replacement_argv(record,source,replay)
        candidate={**self.meta,'attempt_id':aid,'automatic_retry_of':'att-source'}
        for key in ('note','failure_class','launch_outcome'):candidate.pop(key,None)
        candidate[review_input.KEY]=review_input.seal_binding(self.jobs,candidate,candidate_input,
            source={'attempt_id':'att-source','binding_digest':source[review_input.KEY]})
        candidate.update(D.parse_registry_metadata(R.seal_launch_input(args,'codex','the raw task')))
        rows=[(['now','done'],meta) for _,meta in R._rows(self.jobs.read_text().splitlines()).values()]
        route_module=SimpleNamespace(review_lineage_routes=lambda route,node:[route],
            review_round_records=lambda *args,**kw:rows,
            _dependency_revisions=lambda *args,**kw:[], REVIEW_ROUND_CAP=__import__('review_round_cap'))
        with mock.patch.object(review_input,'_route_module',return_value=route_module):
            with self.assertRaises(D.DispatchContractError) as failure:
                review_input.validate_revision_admission(self.jobs,candidate,candidate_input)
            self.assertEqual(failure.exception.reason,'reviewed-evidence-revision-not-admitted')
            row='now\topen\t'+str(self.root)+'\t'+str(self.root)+'\treview\t'+','.join(k+'='+v for k,v in candidate.items())
            self.assertTrue(D.claim_attempt_row(self.jobs,aid,row,launch=False))
            self.assertFalse(D.claim_attempt_row(self.jobs,aid,row,launch=False))
        registered=R._rows(self.jobs.read_text().splitlines())[aid][1]
        self.assertEqual(registered['replacement_family_id'],record['family_id'])
        self.assertEqual(registered['launch_claimed'],'0')
        self.assertEqual(R._rows(self.jobs.read_text().splitlines())['att-source'][1]['note'],'dead-exact-pid')

    def test_legacy_retry_consumes_same_budget(self):
        self.write({**self.meta,'automatic_retry_of':'att-earlier'})
        with self.assertRaises(D.DispatchContractError) as caught:self.claim()
        self.assertEqual(caught.exception.reason,'automatic-replacement-exhausted')

    def test_register_then_start_has_same_sealed_input(self):
        args=SimpleNamespace(**vars(self.args))
        args.replacement_input_argv=['--register','--attempt-id','att-source',
                                     '--prompt-file','irrelevant-input-path']
        self.assertEqual(R.seal_launch_input(args,'codex','the raw task'),
                         ',replacement_input_digest='+self.meta['replacement_input_digest'])
        args.replacement_input_argv=['--start','--worktree',str(self.root),
                                     '--jobs',str(self.jobs),'--prompt-text','the raw task']
        self.assertEqual(R.seal_launch_input(args,'codex','the raw task'),
                         ',replacement_input_digest='+self.meta['replacement_input_digest'])
        with self.assertRaises(D.DispatchContractError):R.seal_launch_input(args,'codex','changed')

    def test_a_never_started_attempt_reseals_its_input_from_a_newer_release(self):
        # The first launcher sealed the input, registered the row and stopped before
        # its claim. The next launcher runs from a newer release.
        args=SimpleNamespace(**vars(self.args));args.attempt_id='att-next'
        args.replacement_input_argv=['--start','--attempt-id','att-next','--prompt-text','the raw task']
        # The launcher holds the allow-list as a tuple; the sealed file reads it back as a list.
        args.resolved_permission_posture={'mode':'bypass','mode_flag':'bypassPermissions',
                 'allowed_tools':('Bash(git status)','Read'),'inherited_default_mode':'default'}
        first=R.seal_launch_input(args,'codex','the raw task')
        self.write({**self.meta,'attempt_id':'att-next','launch_claimed':'0',
                    **D.parse_registry_metadata(first)},'open',append=True)
        with mock.patch.object(R,'ROOT',Path('/newer/release')):
            second=R.seal_launch_input(args,'codex','the raw task')
        self.assertNotEqual(first,second)
        saved=json.loads((R._directory(self.jobs)/'inputs'/'att-next.json').read_text())
        self.assertEqual(saved['launch_home'],'/newer/release')
        self.assertEqual(',replacement_input_digest='+R._digest(saved),second)
        # Different work, or an attempt that already started, still conflicts.
        with mock.patch.object(R,'ROOT',Path('/third/release')):
            with self.assertRaises(D.DispatchContractError) as caught:
                R.seal_launch_input(args,'codex','changed task')
        self.assertEqual(caught.exception.reason,'replacement-record-conflict')
        self.write({**self.meta,'attempt_id':'att-next','launch_claimed':'1'},'open',append=True)
        with mock.patch.object(R,'ROOT',Path('/third/release')):
            with self.assertRaises(D.DispatchContractError) as caught:
                R.seal_launch_input(args,'codex','the raw task')
        self.assertEqual(caught.exception.reason,'replacement-record-conflict')

    def test_a_launcher_elsewhere_reseals_never_started_work_but_not_other_permissions(self):
        # BC rt-96bab699 (DIAG-1007): a Codex owner's tool shell sealed serial-chain phase G2 as
        # foreground-scoped / danger-full-access inside its own sandbox; the host-side session
        # supervisor advancing the chain seals the same work detached / workspace-write.
        args=SimpleNamespace(**vars(self.args));args.attempt_id='att-g2'
        args.replacement_input_argv=['--start','--attempt-id','att-g2','--sandbox','workspace-write',
                                     '--prompt-text','phase g2']
        args.launch_lifecycle='foreground-scoped';args.nested_headless_network=False
        args.replacement_runtime_sandbox='danger-full-access'
        first=R.seal_launch_input(args,'codex','phase g2')
        self.write({**self.meta,'attempt_id':'att-g2','launch_claimed':'0',
                    **D.parse_registry_metadata(first)},'open',append=True)
        host=SimpleNamespace(**vars(args));host.launch_lifecycle='detached'
        host.replacement_runtime_sandbox='workspace-write'
        second=R.seal_launch_input(host,'codex','phase g2')
        saved=json.loads((R._directory(self.jobs)/'inputs'/'att-g2.json').read_text())
        self.assertEqual((saved['applied_permissions']['launch_lifecycle'],saved['applied_permissions']['runtime_sandbox']),
                         ('detached','workspace-write'))
        self.assertEqual(',replacement_input_digest='+R._digest(saved),second)
        # A granted permission, the work or the sandbox it asks for still conflicts.
        for changes in ({'nested_headless_network':True},
                        {'resolved_permission_posture':{'mode':'bypass','mode_flag':'bypassPermissions',
                                                        'allowed_tools':('Read',),'inherited_default_mode':'default'}},
                        {'replacement_input_argv':['--start','--attempt-id','att-g2','--sandbox','danger-full-access',
                                                   '--prompt-text','phase g2']}):
            with self.subTest(changed=sorted(changes)):
                with self.assertRaises(D.DispatchContractError) as caught:
                    R.seal_launch_input(SimpleNamespace(**{**vars(host),**changes}),'codex','phase g2')
                self.assertEqual(caught.exception.reason,'replacement-record-conflict')
        with self.assertRaises(D.DispatchContractError):R.seal_launch_input(host,'codex','another phase')
        # Once claimed, even the launcher's location may not reseal it.
        self.write({**self.meta,'attempt_id':'att-g2','launch_claimed':'1'},'open',append=True)
        with self.assertRaises(D.DispatchContractError) as caught:
            R.seal_launch_input(args,'codex','phase g2')
        self.assertEqual(caught.exception.reason,'replacement-record-conflict')

    def test_the_registry_and_the_replacement_tuple_read_the_same_definition(self):
        import route_authority
        self.assertIs(D._RELAUNCH_STABLE_KEYS,route_authority.RELAUNCH_STABLE_KEYS)
        self.assertIs(R._RESEAL_STABLE_KEYS,route_authority.RESEAL_STABLE_KEYS)
        base=['2026-10-07T00:00:00Z','open','/repo','/wt','phase-g2']
        row={'attempt_id':'att-g2','parent_attempt_id':'att-owner','route_id':'rt-1','route_node':'test',
             'launch_claimed':'0','launch_lifecycle':'foreground-scoped','replacement_input_digest':'a'*64}
        relaunch={**row,'launch_lifecycle':'detached','replacement_input_digest':'b'*64,'launch_home':'/host'}
        self.assertTrue(D._never_launched_same_work(base,row,base,relaunch))
        self.assertFalse(D._never_launched_same_work(base,row,base,{**relaunch,'parent_attempt_id':'att-other'}))
        replay=R.launch_input(self.jobs,'att-source',R._rows(self.jobs.read_text().splitlines())['att-source'][1])
        shell={**replay,'applied_permissions':{'launch_lifecycle':'foreground-scoped','runtime_sandbox':'danger-full-access',
                                               'nested_headless_network':False}}
        R._check_tuple({**shell,'applied_permissions':{'launch_lifecycle':'detached','runtime_sandbox':'workspace-write',
                                                        'nested_headless_network':False}},shell)
        with self.assertRaises(D.DispatchContractError) as caught:
            R._check_tuple({**shell,'applied_permissions':{**shell['applied_permissions'],'nested_headless_network':True}},shell)
        self.assertEqual((caught.exception.reason,caught.exception.detail),
                         ('replacement-input-tuple-mismatch','applied_permissions'))

    def test_claim_publication_crash_blocks_legacy_retry(self):
        for fail_before_record in [True,False]:
            with self.subTest(before_record=fail_before_record):
                if fail_before_record:
                    real_once=R._once
                    def crash(path,value):
                        if path.parent.name=='claims':raise RuntimeError('crash')
                        return real_once(path,value)
                    patch=mock.patch.object(R,'_once',side_effect=crash)
                else:patch=mock.patch.object(R,'_bind_source',side_effect=RuntimeError('crash'))
                with patch,self.assertRaises(RuntimeError):self.claim()
                lines=self.jobs.read_text().splitlines()
                for key in ['automatic_retry_of','prior_attempt_id']:
                    with self.assertRaises(D.DispatchContractError) as caught:
                        R.admission(self.jobs,lines,{**self.meta,'attempt_id':'att-other',key:'att-source'})
                    self.assertEqual(caught.exception.reason,'replacement-claim-pending')
        self.assertEqual(self.claim()['replacement_attempt_id'],self.claim()['replacement_attempt_id'])

    def test_candidate_task_and_resolved_permissions_are_bound(self):
        record=self.claim();source=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        replay=R.launch_input(self.jobs,'att-source',source)
        candidate={**self.meta,'attempt_id':record['replacement_attempt_id'],'automatic_retry_of':'att-source'}
        args=SimpleNamespace(**vars(self.args));args.attempt_id=candidate['attempt_id']
        args.replacement_input_argv=R._replacement_argv(record,source,replay)
        candidate.update(D.parse_registry_metadata(R.seal_launch_input(args,'codex','other task')))
        with self.assertRaises(D.DispatchContractError) as caught:
            R.admission(self.jobs,self.jobs.read_text().splitlines(),candidate)
        self.assertEqual(caught.exception.reason,'replacement-task-mismatch')

    def test_changed_reused_evidence_refuses_actual_spawn(self):
        record=self.claim()
        with mock.patch.object(R,'_reuse_snapshot',return_value={'changed':'gate'}):
            with self.assertRaises(D.DispatchContractError) as caught:
                R.validate_claim_source(self.jobs,self.jobs.read_text().splitlines(),record)
        self.assertEqual(caught.exception.reason,'replacement-reuse-evidence-drift')

    def test_quick_owner_uses_explicit_route_without_owner_environment(self):
        self.write({**self.meta,'worker_type':'owner','route_file':str(self.path)})
        record=self.claim();source=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        cmd=R._command(self.jobs,record,source,R.launch_input(self.jobs,'att-source',source))
        self.assertEqual(cmd[cmd.index('--route-file')+1],str(self.path))
        with mock.patch.object(R,'_authorized'),mock.patch('dispatch_replacement_batch.command',return_value=None):
            result=R.advance(self.jobs,'att-source',run=lambda command,**kw: (
                self.assertFalse(any(k.startswith('AGENT_OWNER_ROUTE_') for k in kw['env']))
                or SimpleNamespace(returncode=0)))
        self.assertEqual(result['reason'],'replacement-launch-pending')

    def _launch_env(self,worker_type,prepare):
        self.write({**self.meta,'worker_type':worker_type,'route_file':str(self.path)} if worker_type=='owner' else {**self.meta,'worker_type':worker_type})
        seen={}
        def run(command,**kw):
            seen.update(kw['env']);return SimpleNamespace(returncode=0)
        stale={'AGENT_ARTIFACT_CYCLE_ID':'cyc-stale','AGENT_ARTIFACT_OUTPUT_DIR':'/stale/out'}
        with mock.patch.dict(os.environ,stale),mock.patch.object(R,'_authorized'),\
                mock.patch('dispatch_replacement_batch.command',return_value=None),\
                mock.patch('artifact_producer.prepare_route_artifact_env',side_effect=prepare) as prep:
            R.advance(self.jobs,'att-source',run=run)
        return seen,prep

    def test_owner_replacement_launch_gets_the_routes_own_cycle_env(self):
        route_env={'AGENT_ARTIFACT_ROOT':str(self.root),'AGENT_ARTIFACT_CYCLE_ID':'cyc-route','AGENT_ARTIFACT_OUTPUT_DIR':'/route/out'}
        env,prep=self._launch_env('owner',lambda *a,**k:route_env)
        prep.assert_called_once_with(self.path,start=False,jobs=self.jobs)
        self.assertEqual((env['AGENT_ARTIFACT_CYCLE_ID'],env['AGENT_ARTIFACT_OUTPUT_DIR']),('cyc-route','/route/out'))

    def test_owner_replacement_launch_survives_a_failed_cycle_lookup(self):
        import artifact_producer
        for error in (artifact_producer.ProducerError('route-artifact-root-missing'),OSError('gone'),ValueError('bad')):
            env,_=self._launch_env('owner',mock.Mock(side_effect=error))
            self.assertEqual(env['AGENT_ARTIFACT_CYCLE_ID'],'cyc-stale')

    def test_owner_replacement_launch_drops_a_stale_parent_output_dir(self):
        stale={'AGENT_ARTIFACT_PARENT_OUTPUT_DIR':'/stale/parent'}
        with mock.patch.dict(os.environ,stale):
            env,_=self._launch_env('owner',lambda *a,**k:{'AGENT_ARTIFACT_CYCLE_ID':'cyc-route'})
        self.assertNotIn('AGENT_ARTIFACT_PARENT_OUTPUT_DIR',env)
        with mock.patch.dict(os.environ,stale):
            env,_=self._launch_env('owner',lambda *a,**k:{'AGENT_ARTIFACT_PARENT_OUTPUT_DIR':'/route/parent'})
        self.assertEqual(env['AGENT_ARTIFACT_PARENT_OUTPUT_DIR'],'/route/parent')

    def test_stage_replacement_launch_env_is_not_touched_by_the_route_cycle(self):
        env,prep=self._launch_env('frame',lambda *a,**k:{'AGENT_ARTIFACT_CYCLE_ID':'cyc-route'})
        prep.assert_not_called()
        self.assertEqual(env['AGENT_ARTIFACT_CYCLE_ID'],'cyc-stale')

    def test_exhausted_sd106_budget_cannot_start_new_family(self):
        for addition in [{'recovery_exhausted':'1'}, {'start_permitted':'0'},
                         {'recovery_id':'rid-dead'}]:
            self.write({**self.meta,**addition})
            if addition.get('recovery_id'):
                D._write_recovery_attention(self.jobs,'att-source','rid-dead','exhausted')
            with self.subTest(addition=addition),self.assertRaises(D.DispatchContractError) as caught:self.claim()
            self.assertEqual(caught.exception.reason,'automatic-replacement-exhausted')
        self.assertFalse((R._directory(self.jobs)/'claims').exists())

    def test_sd106_attention_before_row_publication_already_consumes_budget(self):
        D._write_recovery_attention(self.jobs,'att-source','rid-dead','exhausted')
        self.assertNotIn('recovery_id',self.jobs.read_text())
        with self.assertRaises(D.DispatchContractError) as caught:self.claim()
        self.assertEqual(caught.exception.reason,'automatic-replacement-exhausted')

    def test_success_addition_and_same_gate_release_are_monotonic(self):
        old={'cycle_id':'cyc','producer_id':'prod','completed':[{'node':'done','marker_digest':'a'}],
             'gates':[{'gate':'frame-review','status':'blocked','epoch':1,'raised_at':'t','artifact':'brief'}]}
        new={**old,'completed':old['completed']+[{'node':'sibling','marker_digest':'b'}],
             'gates':[{**old['gates'][0],'status':'proceed','answers':{'direction':'yes'}}]}
        self.assertTrue(R._reuse_preserved(old,new))
        self.assertFalse(R._reuse_preserved(old,{**new,'completed':[]}))
        self.assertFalse(R._reuse_preserved(old,{**new,'gates':[{**new['gates'][0],'epoch':2}]}))
        self.assertFalse(R._reuse_preserved(new,{**new,'gates':[{**new['gates'][0],'answers':{'direction':'no'}}]}))

    def test_gate_authority_comes_from_journal_not_sidecar(self):
        import workflow_state as WS
        route={**self.route,'human_gate_bindings':[{'gate':'frame-review'}]}
        ledger=WS.WorkflowLedger(route['route_id'],route['route_hash'],jobs=self.jobs)
        ledger.journal_path.parent.mkdir(parents=True)
        def entry(state,evidence):return {'route_id':route['route_id'],'route_hash':route['route_hash'],
                                          'workflow_state':state,'evidence':evidence}
        raised=entry('BLOCKED_HUMAN_GATE',{'gate':'frame-review'})
        ledger.journal_path.write_text(json.dumps(raised)+'\n')
        self.assertEqual(R._gate_snapshot(self.jobs,route)[0]['status'],'blocked')
        released=entry('RUNNING',{'released_gate':'frame-review','decision':'proceed','answers':{'direction':'yes'}})
        with ledger.journal_path.open('a') as f:f.write(json.dumps(released)+'\n')
        self.assertEqual(R._gate_snapshot(self.jobs,route)[0]['status'],'proceed')
        for decision in ['revise','stop']:
            event=entry('CANCELLED',{'gate':'frame-review','abandon_reason':'operator-decision'}) if decision=='stop' else entry('RUNNING',{'released_gate':'frame-review','decision':'revise'})
            ledger.journal_path.write_text(json.dumps(raised)+'\n'+json.dumps(event)+'\n')
            with self.subTest(decision=decision),self.assertRaises(D.DispatchContractError):R._gate_snapshot(self.jobs,route)
        ledger.journal_path.write_text('{torn')
        with self.assertRaises(D.DispatchContractError):R._gate_snapshot(self.jobs,route)

    def test_registered_only_successor_resumes_original_transaction(self):
        record=self.claim()
        self.write({**self.meta,'attempt_id':record['replacement_attempt_id'],
                    'replacement_original_attempt_id':'att-source','note':'registered','launch_claimed':'0'},
                   'open',append=True)
        calls=[]
        def crashed_launcher(command,**kwargs):
            calls.append(command);return SimpleNamespace(returncode=75)
        with mock.patch.object(R,'_authorized'),mock.patch('dispatch_replacement_batch.command',return_value=None):
            result=R.advance(self.jobs,record['replacement_attempt_id'],run=crashed_launcher)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][calls[0].index('--attempt-id')+1],record['replacement_attempt_id'])
        self.assertEqual(result['reason'],'replacement-launch-pending')
        self.assertEqual(len(list((R._directory(self.jobs)/'claims').glob('*.json'))),1)

    def test_slow_launcher_is_named_and_the_same_attempt_relaunches(self):
        seen=[]
        def slow(command,**kwargs):
            seen.append((command,kwargs['timeout']))
            raise subprocess.TimeoutExpired(command,kwargs['timeout'],output='waiting on disk',stderr=b'')
        with mock.patch.object(R,'_authorized'),mock.patch('dispatch_replacement_batch.command',return_value=None):
            first=R.advance(self.jobs,'att-source',run=slow)
        self.assertEqual(first['reason'],'replacement-launch-timeout')
        self.assertIn('waiting on disk',first['launcher_diagnostic'])
        self.assertEqual(seen[0][1],R.LAUNCHER_TIMEOUT_SECONDS)
        self.write({**self.meta,'attempt_id':first['attempt_id'],'replacement_original_attempt_id':'att-source',
                    'note':'registered','launch_claimed':'0'},'open',append=True)
        with mock.patch.object(R,'_authorized'),mock.patch('dispatch_replacement_batch.command',return_value=None):
            R.advance(self.jobs,'att-source',run=slow)
        self.assertEqual(seen[1][0][seen[1][0].index('--attempt-id')+1],first['attempt_id'])

    def test_foreground_601_seconds_is_not_killed_by_detached_admission_budget(self):
        import dispatch_lifecycle as L
        seen=[]
        command=['wrapper','--launch-lifecycle','foreground-scoped','--foreground-timeout','3600']
        def finished(argv,**kw):
            seen.append(kw['timeout']);self.assertGreater(kw['timeout'],601)
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with mock.patch.object(R,'_command',return_value=command),mock.patch.object(R,'_authorized'), \
             mock.patch('dispatch_replacement_batch.command',return_value=None):
            R.advance(self.jobs,'att-source',run=finished)
        self.assertEqual(seen,[600+3600+L.FORWARDED_TERMINATION_GRACE])
        with mock.patch.object(L,'reconcile_launch_lifecycle',return_value=SimpleNamespace(effective='detached')):
            self.assertEqual(R._launcher_budget(['wrapper','--launch-lifecycle=detached'],self.meta),(False,600))

    def test_foreground_launch_uses_existing_forwarder_and_claimed_timeout_never_relaunches(self):
        import dispatch_lifecycle as L
        record=self.claim();aid=record['replacement_attempt_id']
        command=['wrapper','--launch-lifecycle','foreground-scoped','--foreground-timeout','3600']
        def interrupted(*args,**kw):
            self.assertTrue(kw['terminate_on_timeout'])
            self._successor(record,self.meta,'open')
            return L.ForwardedRun(0,'','',15,True,True)
        with mock.patch.object(R,'_command',return_value=command),mock.patch.object(R,'_authorized'), \
             mock.patch('dispatch_replacement_batch.command',return_value=None), \
             mock.patch.object(L,'run_forwarding_termination',side_effect=interrupted) as forward:
            result=R.advance(self.jobs,'att-source')
            self.assertEqual(result['reason'],'replacement-launch-timeout')
            self.assertTrue(result['cleanup_incomplete'])
            replay=R.advance(self.jobs,'att-source')
            self.assertEqual((replay['state'],replay['attempt_id']),('running',aid))
            self.assertEqual(forward.call_count,1)

    def test_soft_gate_uses_sealed_profile_policy_and_preserves_unknown_all_gated(self):
        capacity=R._capacity_reader()
        self.route['dispatch_allocation']={'strategy':'balanced','window':30,'usage_gate_used_percent':85,
                                          'harness_order':['claude','codex','opencode']}
        self.route['nodes']=[{'id':'frame','harness_policy':{'primary':['claude','codex'],
                             'relief':[],'last_resort':[],'promote_relief_below':0}}]
        source={**self.meta,'harness':'claude'}
        def report(claude,codex):return {'scores':{'claude':claude,'codex':codex,'opencode':None},
                                        'sources':{'claude':'taps','codex':'fixture','opencode':'unknown'}}
        with mock.patch.object(R,'_capacity_reader',return_value=capacity), \
             mock.patch('dispatch_capacity_evidence.harness_hold',return_value=None), \
             mock.patch('dispatch_capacity_evidence.usage_states',return_value=dict.fromkeys(['claude','codex','opencode'],'ok')), \
             mock.patch.object(capacity,'capacity_report',return_value=report(1,80)):
            hold=R._capacity_hold(self.jobs,source)
            self.assertEqual((hold['label'],hold['headroom'],hold['usage_gate_used_percent']),('allocation-usage-gate',1,85))
            self.write(source);before=self.jobs.read_bytes()
            with mock.patch.object(R,'_authorized'),mock.patch.object(R,'claim') as claim:
                result=R.advance(self.jobs,'att-source',run=mock.Mock(side_effect=AssertionError('spawn')))
            self.assertEqual(result['reason'],'replacement-capacity-wait');claim.assert_not_called()
            self.assertEqual(self.jobs.read_bytes(),before)
            for scores in (report(None,80),report(10,1)):
                with mock.patch.object(capacity,'capacity_report',return_value=scores):
                    self.assertIsNone(R._capacity_hold(self.jobs,source))
            self.route['nodes'][0]['harness_policy']['primary']=['claude']
            with mock.patch.object(capacity,'capacity_report',return_value=report(1,80)):
                self.assertIsNone(R._capacity_hold(self.jobs,source))

    def test_a_sealed_pin_moves_only_for_a_real_limit_and_an_unpinned_gate_says_when_it_lifts(self):
        # BC rt-96bab699 (`--pin owner=codex --pin worker=codex`): an owner replacement after
        # `correct` was held by the soft allocation gate, although the route's first launch
        # started on the pin with the same headroom.
        capacity=R._capacity_reader()
        policy={'primary':['claude','codex'],'relief':[],'last_resort':[],'promote_relief_below':0}
        self.route['dispatch_allocation']={'strategy':'balanced','window':30,'usage_gate_used_percent':85,
                                          'harness_order':['claude','codex','opencode']}
        self.route['owner_harness_policy']=policy
        self.route['nodes']=[{'id':'test','harness_policy':policy}]
        owner={**self.meta,'worker_type':'owner','route_node':'','harness':'codex'}
        stage={**self.meta,'worker_type':'stage','route_node':'test','dispatch_depth':'2','harness':'codex'}
        report={'scores':{'claude':80,'codex':10,'opencode':None},
                'sources':{'claude':'taps','codex':'live','opencode':'unknown'}}
        limited={'until_epoch':4102444800,'label':'2100-01-01T00:00:00Z'}
        with mock.patch.object(R,'_capacity_reader',return_value=capacity), \
             mock.patch('dispatch_capacity_evidence.usage_states',return_value=dict.fromkeys(['claude','codex','opencode'],'ok')), \
             mock.patch.object(capacity,'capacity_report',return_value=report), \
             mock.patch.object(capacity,'gate_release_epoch',return_value=None):
            with mock.patch('dispatch_capacity_evidence.harness_hold',return_value=None):
                for source in (stage,):
                    hold=R._capacity_hold(self.jobs,source)
                    self.assertEqual((hold['label'],hold['until_epoch'],hold['usage_gate_used_percent']),
                                     ('allocation-usage-gate',None,85))
                    self.assertNotIn('retry_at',R._capacity_wait(self.jobs,'att-source',source))
                self.route['selection_pins']={'owner':{'harness':'codex'},'worker':{'harness':'codex'}}
                for source in (owner,stage):                 # the pin starts despite the soft gate
                    self.assertIsNone(R._capacity_hold(self.jobs,source))
                self.route['selection_pins']={'owner':{'harness':'claude'}}
                self.assertIsNone(R._capacity_hold(self.jobs,owner))  # the ordinary selector honours the Claude pin
            self.route['selection_pins']={'owner':{'harness':'codex'},'worker':{'harness':'codex'}}
            with mock.patch('dispatch_capacity_evidence.harness_hold',return_value=limited):
                for source in (owner,stage):                 # a real usage limit still holds the pin
                    self.assertEqual(R._capacity_hold(self.jobs,source),limited)
            del self.route['selection_pins']
            with mock.patch('dispatch_capacity_evidence.harness_hold',return_value=None), \
                 mock.patch.object(capacity,'gate_release_epoch',return_value=4102444800) as release:
                self.assertIsNone(R._capacity_hold(self.jobs,owner))
                self.assertEqual(R._capacity_hold(self.jobs,stage)['until_epoch'],4102444800)
                release.assert_called_with('codex','live',usage_gate_used_percent=85)
                self.assertEqual(R._capacity_wait(self.jobs,'att-source',stage)['retry_at'],'2100-01-01T00:00:00Z')

    def test_an_io_failure_keeps_its_cause(self):
        with mock.patch.object(R,'_authorized'),mock.patch.object(R,'claim',side_effect=OSError('disk gone')):
            result=R.advance(self.jobs,'att-source',run=mock.Mock())
        self.assertEqual(result['reason'],'replacement-observation-unavailable')
        self.assertEqual(result['detail'],'OSError: disk gone')

    def test_concurrent_actual_spawn_releases_one_fenced_process(self):
        record=self.claim();aid=record['replacement_attempt_id']
        source=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        replay=R.launch_input(self.jobs,'att-source',source)
        args=SimpleNamespace(**vars(self.args));args.attempt_id=aid
        args.replacement_input_argv=R._replacement_argv(record,source,replay)
        candidate={**self.meta,'attempt_id':aid,'automatic_retry_of':'att-source'}
        for key in ('note','failure_class','launch_outcome'):candidate.pop(key,None)
        candidate.update(D.parse_registry_metadata(R.seal_launch_input(args,'codex','the raw task')))
        row='now\topen\t'+str(self.root)+'\t'+str(self.root)+'\ttask\t'+','.join(k+'='+v for k,v in candidate.items())
        self.assertTrue(D.claim_attempt_row(self.jobs,aid,row,launch=False))
        counter=self.root/'spawned';children=[]
        def spawn(fd):
            child=subprocess.Popen([sys.executable,str(Path(__file__).with_name('launch-fence.py')),
                    '--parent-pid',str(os.getpid()),'--gate-fd',str(fd),'--',sys.executable,'-c',
                    'from pathlib import Path; Path('+repr(str(counter))+').open("a").write("started\\n")'],
                    pass_fds=(fd,),start_new_session=True)
            children.append(child);return child
        def attempt(_):
            try:return D.spawn_claimed_attempt(self.jobs,aid,parent_binding=None,spawn=spawn,
                        launch_metadata={'launch_lifecycle':'detached'})
            except D.DispatchContractError:return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(attempt,range(12)))
        for child in children:child.wait(timeout=10)
        self.assertEqual(len(children),1)
        self.assertEqual(counter.read_text().splitlines(),['started'])
        self.assertEqual(R._rows(self.jobs.read_text().splitlines())['att-source'][1]['note'],'dead-exact-pid')

    def test_quick_and_advanced_owner_route_lookup(self):
        current=self.root/'current.json';current.write_text(json.dumps(self.route))
        self.route_mock.stop()
        with mock.patch.object(D,'_route_module',return_value=SimpleNamespace(verify_route=lambda route:None)):
            with mock.patch('owner_route_binding.resolve_owner_route_lifecycle',return_value=(None,None)):
                path,_=R._route(self.jobs,'att-source',{**self.meta,'worker_type':'owner','route_file':str(self.path)})
                self.assertEqual(path,self.path)
            with mock.patch('owner_route_binding.resolve_owner_route_lifecycle',return_value=(SimpleNamespace(route_file=str(current)),None)):
                path,_=R._route(self.jobs,'att-source',{**self.meta,'worker_type':'owner','owner_route_file':str(self.path)})
                self.assertEqual(path,current)

    def test_default_tuple_and_option_order_are_normalized_for_replay(self):
        args=SimpleNamespace(attempt_id='att-defaults',jobs_path=self.jobs,worktree=str(self.root),
                  route_id='rt-test',route_node='frame',parent_session_id='parent',sandbox='workspace-write',
                  replacement_input_argv=['--start','--slug','task','--route-file',str(self.path)])
        fragment=R.seal_launch_input(args,'codex','task')
        source={**self.meta,'attempt_id':args.attempt_id,'route_file':str(self.path),**D.parse_registry_metadata(fragment)}
        replay=R.launch_input(self.jobs,args.attempt_id,source)
        record={'route_file':str(self.path),'route_id':'rt-test','route_hash':'sha256:test',
                'replacement_attempt_id':'att-defaults-new','original_attempt_id':args.attempt_id}
        argv=R._replacement_argv(record,source,replay)
        args.attempt_id=record['replacement_attempt_id'];args.replacement_input_argv=argv
        fragment=R.seal_launch_input(args,'codex','task')
        candidate=R.launch_input(self.jobs,args.attempt_id,D.parse_registry_metadata(fragment))
        self.assertEqual(candidate['argv'],argv)
        self.assertEqual(candidate['resolved'],replay['resolved'])

    def test_config_permission_escalation_changes_sealed_authority(self):
        args=SimpleNamespace(**vars(self.args));args.attempt_id='att-permission';args.permission_mode='config'
        args.resolved_permission_posture={'mode':'allowlist','mode_flag':'acceptEdits',
                 'allowed_tools':['Read'],'inherited_default_mode':'default','reason':'config'}
        fragment=R.seal_launch_input(args,'claude','task')
        original=R.launch_input(self.jobs,args.attempt_id,D.parse_registry_metadata(fragment))
        args.resolved_permission_posture['reason']='diagnostic-only'
        self.assertEqual(R.seal_launch_input(args,'claude','task'),fragment)
        args.resolved_permission_posture.update(mode='bypass',mode_flag='bypassPermissions')
        with self.assertRaises(D.DispatchContractError):R.seal_launch_input(args,'claude','task')
        source={**self.meta,'attempt_id':'att-permission','harness':'claude',
                **D.parse_registry_metadata(fragment)}
        self.write(source)
        record=R.claim(self.jobs,'att-permission')
        args.attempt_id=record['replacement_attempt_id']
        args.replacement_input_argv=R._replacement_argv(record,source,original)
        candidate_fragment=R.seal_launch_input(args,'claude','task')
        candidate=R.launch_input(self.jobs,args.attempt_id,D.parse_registry_metadata(candidate_fragment))
        self.assertEqual(original['resolved'],candidate['resolved'])
        self.assertNotEqual(original['applied_permissions'],candidate['applied_permissions'])
        metadata={**source,'attempt_id':args.attempt_id,'automatic_retry_of':'att-permission',
                  **D.parse_registry_metadata(candidate_fragment)}
        with self.assertRaises(D.DispatchContractError) as caught:
            R.admission(self.jobs,self.jobs.read_text().splitlines(),metadata)
        self.assertEqual(caught.exception.reason,'replacement-input-tuple-mismatch')
        self.assertEqual(caught.exception.detail,'applied_permissions')

    def test_replay_uses_raw_task_and_new_identity(self):
        record=self.claim();source=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        cmd=R._command(self.jobs,record,source,R.launch_input(self.jobs,'att-source',source))
        self.assertEqual(cmd[cmd.index('--attempt-id')+1],record['replacement_attempt_id'])
        self.assertEqual(cmd[cmd.index('--automatic-retry-of')+1],'att-source')
        self.assertNotIn('--prompt-text',cmd)
        self.assertEqual(Path(cmd[cmd.index('--prompt-file')+1]).read_text(),'the raw task')
        self.assertEqual(cmd.count('--start'),1)

    def test_automatic_replacement_preserves_later_round_recovery_guidance(self):
        brief = 'Recover the failed leg only.\n## Round protocol\nReview only the prior blocking findings.'
        (R._directory(self.jobs)/'inputs/att-source.json').unlink()
        with mock.patch.dict(os.environ, {'AGENT_DISPATCH_RETRY_BRIEF': brief}):
            self.assertIn(brief, R.recovery_instructions(self.args))
            self.meta.update(D.parse_registry_metadata(R.seal_launch_input(self.args, 'codex', 'the raw task')))
        self.write(self.meta)
        record = self.claim()
        source = R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        replay = R.launch_input(self.jobs, 'att-source', source)
        self.assertEqual(replay['task'], 'the raw task')
        command = R._command(self.jobs, record, source, replay)
        self.assertEqual(Path(command[command.index('--prompt-file')+1]).read_text(), 'the raw task')
        successor = SimpleNamespace(**{**vars(self.args), 'attempt_id': record['replacement_attempt_id'],
                                      'automatic_retry_of': 'att-source', 'worker_type': 'stage',
                                      'replacement_input_argv': R._replacement_argv(record, source, replay)})
        del successor.replacement_retry_brief
        with mock.patch.dict(os.environ, {}, clear=True):
            guidance = R.recovery_instructions(successor)
        self.assertIn(brief, guidance)
        self.assertEqual(guidance.count('## Round protocol'), 1)
        fragment = R.seal_launch_input(successor, 'codex', 'the raw task')
        sealed = R.launch_input(self.jobs, successor.attempt_id, D.parse_registry_metadata(fragment))
        self.assertEqual(sealed['retry_brief'], brief)
        self.assertEqual(sealed['task'], 'the raw task')

    def test_unstarted_attempt_cannot_reseal_changed_recovery_guidance(self):
        self.args.replacement_retry_brief = 'Review only prior blocking findings.'
        (R._directory(self.jobs)/'inputs/att-source.json').unlink()
        R.seal_launch_input(self.args, 'codex', 'the raw task')
        self.args.replacement_retry_brief = 'Review a different scope.'
        with self.assertRaises(D.DispatchContractError):
            R.seal_launch_input(self.args, 'codex', 'the raw task')

    # SD106 writes retry_attempt_id on the original row only. These fixtures
    # deliberately retain that production shape, with no automatic_retry_of.
    def _legacy_real_route(self, name, parent=None):
        import route_lineage
        self.logical.stop()  # Exercise the real hash-verified family key.
        route = {'artifact_root': str(self.root), 'cwd': str(self.root),
                 'capability': 'autopilot-code', 'nodes': [], 'fixture_name': name}
        if parent is not None:
            route.update(continuation_contract_version=1,
                         source_route_id=parent['route_id'],
                         source_route_hash=parent['route_hash'])
        route['route_hash'] = route_identity.route_hash(route)
        route['route_id'] = route_identity.route_id_from_hash(route['route_hash'])
        path = route_lineage.canonical_route_path(self.root, route['route_id'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(route))
        lineage = route_lineage.verified_route_lineage(route)
        self.assertEqual(lineage[0]['route_id'], route['route_id'])
        if parent is not None:
            self.assertEqual(lineage[1]['route_id'], parent['route_id'])
        return route, path

    def _legacy_row(self, aid, route, path, node='frame'):
        meta = {key: value for key, value in self.meta.items()
                if key != 'replacement_input_digest'}
        meta.update(attempt_id=aid, route_id=route['route_id'],
                    route_hash=route['route_hash'], route_file=str(path), route_node=node,
                    cancellation_quiescence_receipt=D.ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT,
                    quiescence_pgid_proof=D.GROUP_REAP_PROOF,
                    quiescence_descendant_proof=D.ATTEMPT_DESCENDANT_PROOF,
                    cancellation_receipt_digest='sha256:' + 'b' * 64)
        args = SimpleNamespace(attempt_id=aid, jobs_path=self.jobs,
                               worktree=str(self.root), route_id=route['route_id'],
                               route_node=node, replacement_input_argv=[
                                   '--start', '--attempt-id', aid, '--prompt-text', 'the raw task'])
        meta.update(D.parse_registry_metadata(R.seal_launch_input(args, 'codex', 'the raw task')))
        return meta

    def _legacy_retry_claim(self, meta, remaining=8):
        identity = {'source_route_id': meta['route_id'],
                    'source_route_hash': meta['route_hash'],
                    'node_or_group_leg': meta['route_node'],
                    'original_attempt_id': meta['attempt_id'],
                    'cancellation_receipt_digest': meta['cancellation_receipt_digest']}
        return D.claim_recovery_retry(self.jobs,
            recovery_id=D._recovery_identity_digest(identity),
            source_route_id=identity['source_route_id'],
            source_route_hash=identity['source_route_hash'],
            node_or_group_leg=identity['node_or_group_leg'],
            original_attempt_id=identity['original_attempt_id'], remaining_cascade=remaining)

    def _legacy_claimed_pair(self):
        route, path = self._legacy_real_route('legacy-root')
        original = self._legacy_row('att-legacy-original', route, path)
        self.write(original)
        first = self._legacy_retry_claim(original)
        self.assertTrue(first.start_permitted)
        self.assertEqual(first.retry_ordinal, 1)
        self.assertEqual(first.retry_attempt_id, D._stable_recovery_attempt_id(first.recovery_id))
        # Existing SD106 idempotence must survive the shared-budget guard.
        self.assertEqual(self._legacy_retry_claim(original), first)
        original = R._rows(self.jobs.read_text().splitlines())[original['attempt_id']][1]
        target = self._legacy_row(first.retry_attempt_id, route, path)
        self.assertNotIn('automatic_retry_of', target)
        self.assertNotIn('retry_ordinal', target)
        self.write(target, append=True)
        R._route.return_value = (path, route)
        return route, path, original, target

    def _legacy_assert_exhausted(self, callback):
        before = self.jobs.read_text()
        with self.assertRaises(D.DispatchContractError) as caught:
            callback()
        self.assertEqual(caught.exception.reason, 'automatic-replacement-exhausted')
        self.assertEqual(self.jobs.read_text(), before)

    def test_sd106_stable_target_cannot_claim_automatic_replacement(self):
        _, _, original, target = self._legacy_claimed_pair()
        self._legacy_assert_exhausted(lambda: R.claim(self.jobs, target['attempt_id']))
        self.assertFalse((R._directory(self.jobs) / 'claims').exists())
        self.assertEqual(R._rows(self.jobs.read_text().splitlines())[original['attempt_id']][1]['note'],
                         'dead-exact-pid')

    def test_sd106_stable_target_cannot_admit_another_successor(self):
        _, _, _, target = self._legacy_claimed_pair()
        for predecessor_key in ('automatic_retry_of', 'prior_attempt_id'):
            with self.subTest(predecessor_key=predecessor_key):
                self._legacy_assert_exhausted(lambda: R.admission(
                    self.jobs, self.jobs.read_text().splitlines(),
                    {**target, 'attempt_id': 'att-second-retry', predecessor_key: target['attempt_id']}))

    def test_sd106_stable_target_cannot_claim_another_sd106_retry(self):
        _, _, _, target = self._legacy_claimed_pair()
        self._legacy_assert_exhausted(lambda: self._legacy_retry_claim(target))

    def test_automatic_retry_row_consumes_budget_and_delivers_failure_attention(self):
        route, path = self._legacy_real_route('automatic-root')
        R._route.return_value = (path, route)
        original = self._legacy_row('att-auto-original', route, path)
        successor = {**self._legacy_row('att-auto-successor', route, path),
                     'automatic_retry_of': original['attempt_id'],
                     'note': 'dead-exit-1', 'failure_class': 'runtime'}
        self.write(original); self.write(successor, append=True)
        rows = self.jobs.read_text().splitlines()
        self.assertTrue(R.legacy_budget_exhausted(self.jobs, rows, original, route=route))
        effective, lineage, attention = R.advance_batch(
            self.jobs, {original['attempt_id']}, authority_check=lambda *_: True)
        self.assertEqual(effective, {original['attempt_id']})
        self.assertEqual(lineage, [])
        self.assertEqual([item['reason'] for item in attention],
                         ['automatic-replacement-exhausted'])
        self.assertEqual(R.validate_attention(self.jobs, attention,
                         allowed_attempts={original['attempt_id']}), attention)
        before = self.jobs.read_bytes()
        self.assertEqual(R.advance_batch(self.jobs, {original['attempt_id']},
                         authority_check=lambda *_: True), (effective, lineage, attention))
        self.assertEqual(self.jobs.read_bytes(), before)
        self.assertFalse((R._directory(self.jobs) / 'claims').exists())

        continuation, continuation_path = self._legacy_real_route('automatic-continuation', route)
        current = self._legacy_row('att-auto-continuation', continuation, continuation_path)
        self.write(current, append=True)
        self.assertTrue(R.legacy_budget_exhausted(
            self.jobs, self.jobs.read_text().splitlines(), current, route=continuation))

    def test_automatic_retry_backlink_mismatch_fails_without_writes(self):
        route, path = self._legacy_real_route('automatic-negative')
        original = self._legacy_row('att-auto-original', route, path)
        successor = {**self._legacy_row('att-auto-successor', route, path),
                     'automatic_retry_of': original['attempt_id']}
        for change in ({'parent_sid': 'foreign'}, {'route_node': 'other-frame'},
                       {'route_hash': 'sha256:foreign'}):
            with self.subTest(change=change):
                self.write(original); self.write({**successor, **change}, append=True)
                before = self.jobs.read_bytes()
                with self.assertRaises(D.DispatchContractError) as caught:
                    R.legacy_budget_exhausted(self.jobs, before.decode().splitlines(),
                                              original, route=route)
                self.assertEqual(caught.exception.reason, 'replacement-legacy-budget-link-unproven')
                self.assertEqual(self.jobs.read_bytes(), before)

    def test_sd106_malformed_exact_backlink_fails_closed(self):
        route, _, original, target = self._legacy_claimed_pair()
        mutations = ({'retry_ordinal': '0'}, {'recovery_id': 'wrong-recovery'},
                     {'parent_sid': 'foreign-parent'}, {'route_node': 'foreign-node'})
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.write({**original, **mutation}); self.write(target, append=True)
                before = self.jobs.read_text()
                with self.assertRaises(D.DispatchContractError) as caught:
                    R.legacy_budget_exhausted(self.jobs, before.splitlines(), target, route=route)
                self.assertEqual(caught.exception.reason, 'replacement-legacy-budget-link-unproven')
                self.assertEqual(self.jobs.read_text(), before)

    def test_sd106_duplicate_exact_backlink_fails_closed(self):
        route, _, original, target = self._legacy_claimed_pair()
        self.write({**original, 'attempt_id': 'att-duplicate-original'}, append=True)
        with self.assertRaises(D.DispatchContractError) as caught:
            R.legacy_budget_exhausted(self.jobs, self.jobs.read_text().splitlines(), target, route=route)
        self.assertEqual(caught.exception.reason, 'replacement-legacy-budget-ambiguous')

    def test_sd106_verified_continuation_same_node_is_exhausted(self):
        root, _, _, _ = self._legacy_claimed_pair()
        route, path = self._legacy_real_route('continuation', root)
        current = self._legacy_row('att-continuation-frame', route, path)
        self.write(current, append=True); R._route.return_value = (path, route)
        self.assertTrue(R.legacy_budget_exhausted(
            self.jobs, self.jobs.read_text().splitlines(), current, route=route))
        self._legacy_assert_exhausted(lambda: R.claim(self.jobs, current['attempt_id']))
        self._legacy_assert_exhausted(lambda: self._legacy_retry_claim(current))

    def test_sd106_other_node_in_verified_continuation_has_own_budget(self):
        root, _, _, _ = self._legacy_claimed_pair()
        route, path = self._legacy_real_route('other-node-continuation', root)
        current = self._legacy_row('att-other-node', route, path, node='other-frame')
        self.write(current, append=True); R._route.return_value = (path, route)
        self.assertFalse(R.legacy_budget_exhausted(
            self.jobs, self.jobs.read_text().splitlines(), current, route=route))
        self.assertEqual(R.claim(self.jobs, current['attempt_id'])['original_attempt_id'], current['attempt_id'])

    def test_sd106_same_named_node_on_other_root_has_own_budget(self):
        self._legacy_claimed_pair()
        route, path = self._legacy_real_route('unrelated-root')
        current = self._legacy_row('att-other-root', route, path)
        self.write(current, append=True); R._route.return_value = (path, route)
        self.assertFalse(R.legacy_budget_exhausted(
            self.jobs, self.jobs.read_text().splitlines(), current, route=route))
        self.assertEqual(R.claim(self.jobs, current['attempt_id'])['original_attempt_id'], current['attempt_id'])

    def test_sd106_attention_first_write_crash_consumes_continuation_budget(self):
        root, path = self._legacy_real_route('attention-root')
        original = self._legacy_row('att-attention-original', root, path)
        self.write(original)
        real_once = R._once
        def crash_after_index(path, value):
            if path.parent.name == 'recovery-attention':
                raise RuntimeError('crash before attention record and row annotation')
            return real_once(path, value)
        with mock.patch.object(R, '_once', side_effect=crash_after_index):
            with self.assertRaisesRegex(RuntimeError, 'crash before'):
                self._legacy_retry_claim(original, remaining=0)
        self.assertNotIn('recovery_exhausted', self.jobs.read_text())
        self.assertEqual(len(list((self.jobs.parent / 'recovery-attention/by-source').glob('*.json'))), 1)
        route, path = self._legacy_real_route('attention-continuation', root)
        current = self._legacy_row('att-after-attention-crash', route, path)
        self.write(current, append=True); R._route.return_value = (path, route)
        self._legacy_assert_exhausted(lambda: R.claim(self.jobs, current['attempt_id']))
        self._legacy_assert_exhausted(lambda: self._legacy_retry_claim(current))

    def test_sd157_reservation_first_write_crash_consumes_continuation_budget(self):
        root, path = self._legacy_real_route('reservation-root')
        original = self._legacy_row('att-reserved-original', root, path)
        self.write(original); R._route.return_value = (path, root)
        real_once = R._once
        def crash_before_record(path, value):
            if path.parent.name == 'claims':
                raise RuntimeError('crash before family record')
            return real_once(path, value)
        with mock.patch.object(R, '_once', side_effect=crash_before_record):
            with self.assertRaisesRegex(RuntimeError, 'crash before'):
                R.claim(self.jobs, original['attempt_id'])
        self.assertIsNotNone(R.source_reservation(self.jobs, original['attempt_id']))
        self.assertNotIn('replacement_family_id', self.jobs.read_text())
        self.assertFalse(list((R._directory(self.jobs) / 'claims').glob('*.json')))
        route, path = self._legacy_real_route('reservation-continuation', root)
        current = self._legacy_row('att-after-reservation-crash', route, path)
        self.write(current, append=True); R._route.return_value = (path, route)
        self._legacy_assert_exhausted(lambda: R.claim(self.jobs, current['attempt_id']))
        self._legacy_assert_exhausted(lambda: self._legacy_retry_claim(current))

    def test_unrelated_corrupt_historical_route_does_not_block_healthy_stream(self):
        _, foreign_path, _, _ = self._legacy_claimed_pair()
        foreign = json.loads(foreign_path.read_text()); foreign['fixture_name'] = 'tampered'
        foreign_path.write_text(json.dumps(foreign))
        route, path = self._legacy_real_route('healthy-other-root')
        current = self._legacy_row('att-healthy', route, path)
        self.write(current, append=True); R._route.return_value = (path, route)
        self.assertFalse(R.legacy_budget_exhausted(
            self.jobs, self.jobs.read_text().splitlines(), current, route=route))
        self.assertEqual(R.claim(self.jobs, current['attempt_id'])['original_attempt_id'], current['attempt_id'])


    # -- an owner that stopped BLOCKED at a human gate is waiting, not dead ------------
    GATE='full-run-authorization'
    OWNER_STAMP='2026-09-29T00:00:00Z'
    RAISED_AT='2026-09-29T01:00:00Z'

    def _parked_route(self,raiser_type=None):
        raiser={'id':'smoke','continuation':{'kind':'human-gate','gate':self.GATE}}
        if raiser_type:raiser['worker_type']=raiser_type
        self.route['nodes']=[raiser,{'id':'full-run','depends_on':['smoke']}]
        self.route['human_gate_bindings']=[{'gate':self.GATE,'node':'full-run','position':'entry'}]

    def _journal(self,*entries):
        import workflow_state as WS
        ledger=WS.WorkflowLedger(self.route['route_id'],self.route['route_hash'],jobs=self.jobs)
        ledger.journal_path.parent.mkdir(parents=True,exist_ok=True)
        with ledger.journal_path.open('a') as f:
            for state,evidence,at in entries:
                f.write(json.dumps({'workflow_state':state,'evidence':evidence,'at':at})+'\n')
        return ledger

    def _raise(self,at=None):return ('BLOCKED_HUMAN_GATE',{'gate':self.GATE,'artifact':'/tmp/gate.md'},at or self.RAISED_AT)
    def _proceed(self):return ('RUNNING',{'released_gate':self.GATE,'decision':'proceed'},'2026-09-29T02:00:00Z')

    def _parked_owner(self,*journal,raiser_type=None,**changes):
        self._parked_route(raiser_type)
        self.owner={**self.meta,'worker_type':'owner','note':'dead-worker-blocked','failure_class':'blocked',**changes}
        self.write(self.owner,stamp=self.OWNER_STAMP)
        self._journal(*journal)

    def test_parked_owner_at_blocked_gate_is_not_replaced(self):
        self._parked_owner(self._raise())
        found=R.owner_parked_gate(self.jobs,'att-source')
        self.assertEqual((found['gate'],found['status'],found['epoch'],found['gated_nodes']),(self.GATE,'blocked',1,['full-run']))
        with self.assertRaises(D.DispatchContractError) as caught:self.claim()
        self.assertEqual(caught.exception.reason,'replacement-not-silent-death')
        result=R.advance(self.jobs,'att-source',authority_check=lambda *_:True)
        self.assertEqual((result['state'],result['parked_gate']['gate']),('not-applicable',self.GATE))
        self.assertFalse((R._directory(self.jobs)/'claims').exists())

    def test_released_parked_owner_is_replaced_once_with_gate_proof(self):
        self._parked_owner(self._raise(),self._proceed())
        with mock.patch.object(R,'_terminal_absent',return_value=False):
            record=self.claim()
            self.assertEqual((record['proof']['parked_gate'],record['proof']['gate_epoch']),(self.GATE,1))
            self.assertEqual(self.claim(),record)
            lines=self.jobs.read_text().splitlines()
            R.validate_claim_source(self.jobs,lines,record)

    def test_parked_proof_requires_owner_raise_and_unstarted_gated_node(self):
        def completion():
            marker=D.dispatch_state_root(self.jobs)/'completion'/'rt-test'/'full-run.json'
            marker.parent.mkdir(parents=True);marker.write_text('{}')
        def stage_row():
            self.write({**self.meta,'attempt_id':'att-stage','route_node':'full-run','worker_type':'stage'},append=True)
        cases={
            'raise-before-owner-start':lambda:self._parked_owner(self._raise('2026-08-01T00:00:00Z')),
            'frame-raiser':lambda:self._parked_owner(self._raise(),raiser_type='frame'),
            'gated-node-completed':lambda:(self._parked_owner(self._raise()),completion()),
            'gated-node-started':lambda:(self._parked_owner(self._raise()),stage_row()),
            'ordinary-failure-note':lambda:self._parked_owner(self._raise(),note='dead-worker-fail'),
            'owner-still-open':lambda:self._parked_owner(self._raise()) or self.write(self.owner,'open',stamp=self.OWNER_STAMP),
        }
        for name,build in cases.items():
            with self.subTest(case=name):
                self.tearDown_case()
                build()
                self.assertIsNone(R.owner_parked_gate(self.jobs,'att-source'))
                with self.assertRaises(D.DispatchContractError):self.claim()

    def tearDown_case(self):
        import shutil
        for name in ('completion','rt-test'):
            shutil.rmtree(D.dispatch_state_root(self.jobs)/name,ignore_errors=True)
        for child in R._directory(self.jobs).iterdir():
            if child.name!='inputs':shutil.rmtree(child,ignore_errors=True)
        (self.jobs.parent/'recovery-attention').exists() and shutil.rmtree(self.jobs.parent/'recovery-attention')
        self.write(self.meta)

    def test_revise_or_stop_parked_owner_never_replaces(self):
        cases={'revise':('RUNNING',{'released_gate':self.GATE,'decision':'revise'},'2026-09-29T02:00:00Z'),
               'stop':('CANCELLED',{'gate':self.GATE,'abandon_reason':'operator-decision'},'2026-09-29T02:00:00Z')}
        for decision,event in cases.items():
            with self.subTest(decision=decision):
                self.tearDown_case()
                self._parked_owner(self._raise(),event)
                self.assertEqual(R.owner_parked_gate(self.jobs,'att-source')['status'],decision)
                self.assertEqual(R.advance(self.jobs,'att-source',authority_check=lambda *_:True)['state'],'not-applicable')
                with self.assertRaises(D.DispatchContractError):self.claim()
                self.assertFalse((R._directory(self.jobs)/'claims').exists())

    def test_parked_recovery_instructions_forbid_reraising_the_gate(self):
        self._parked_owner(self._raise(),self._proceed())
        with mock.patch.object(R,'_terminal_absent',return_value=False):record=self.claim()
        text=R.recovery_instructions(SimpleNamespace(automatic_retry_of='att-source',worker_type='owner',
                                     jobs_path=self.jobs,attempt_id=record['replacement_attempt_id']))
        for expected in (f'Do not raise {self.GATE} again','await-release','--max 0','--answers-out',R.CONTINUATION_WAIT_NOTE):
            self.assertIn(expected,text)

    def test_every_replacement_owner_is_told_to_wait_live_at_a_later_gate(self):
        def parked_record():
            self._parked_owner(self._raise(),self._proceed())
            with mock.patch.object(R,'_terminal_absent',return_value=False):return self.claim()
        def silent_record():
            self.write({**self.meta,'worker_type':'owner'});return self.claim()
        for name,build,parked in (('parked-original',parked_record,True),('silent-death-original',silent_record,False)):
            with self.subTest(origin=name):
                self.tearDown_case()
                record=build()
                args=SimpleNamespace(automatic_retry_of='att-source',worker_type='owner',jobs_path=self.jobs,
                                     attempt_id=record['replacement_attempt_id'])
                text=R.recovery_instructions(args)
                for expected in (R.CONTINUATION_WAIT_NOTE,'await-release','do not end the turn at the gate',
                                 'Verified recovery context','report any second failure as needs-attention'):
                    self.assertIn(expected,text)
                self.assertEqual('Do not raise' in text,parked)
                self.assertEqual(R.recovery_instructions(SimpleNamespace(**{**vars(args),'worker_type':'stage'})),'')

    def _passed_owner(self,*journal,owner_executed=True,**changes):
        """An owner whose terminal result was PASS while the gate it should have waited at is raised."""
        self._parked_route()
        if owner_executed:
            self.route['nodes'][1].update(kind='capability-owner',unit='_kernel/owner',dispatch_depth=1,terminal=True)
        self.owner={**self.meta,'worker_type':'owner','note':'completed-supervisor','failure_class':'pass',
                    'workflow_completion':'runtime-v1',**changes}
        self.write(self.owner,stamp=self.OWNER_STAMP)
        self._journal(*journal)

    def test_a_passed_owner_before_its_own_gated_operation_is_recognised_as_waiting_and_never_replaced(self):
        for name,journal,status in (('raised-unreleased',(self._raise(),),'blocked'),
                                    ('released',(self._raise(),self._proceed()),'proceed'),
                                    ('revise',(self._raise(),('RUNNING',{'released_gate':self.GATE,'decision':'revise'},'2026-09-29T02:00:00Z')),'revise'),
                                    ('stop',(self._raise(),('CANCELLED',{'gate':self.GATE,'abandon_reason':'operator-decision'},'2026-09-29T02:00:00Z')),'stop')):
            with self.subTest(case=name):
                self.tearDown_case()
                self._passed_owner(*journal)
                found=R.owner_parked_gate(self.jobs,'att-source')
                self.assertEqual((found['gate'],found['status'],found['gated_nodes']),(self.GATE,status,['full-run']))
                # a passed owner is never a death: no claim, no replacement, whatever the gate says
                self.assertIsNone(R.death_kind(self.write_fields(),self.owner,jobs=self.jobs))
                with self.assertRaises(D.DispatchContractError):self.claim()
                self.assertEqual(R.advance(self.jobs,'att-source',authority_check=lambda *_:True)['state'],'not-applicable')
                self.assertFalse((R._directory(self.jobs)/'claims').exists())

    def write_fields(self):
        return self.jobs.read_text().splitlines()[-1].split('\t')

    def test_a_passed_owner_with_no_owner_executed_gated_node_is_not_a_park(self):
        self._passed_owner(self._raise(),owner_executed=False)
        self.assertIsNone(R.owner_parked_gate(self.jobs,'att-source'))

    def test_a_passed_owner_whose_gate_was_never_raised_is_not_a_park(self):
        self._passed_owner()
        self.assertIsNone(R.owner_parked_gate(self.jobs,'att-source'))

    def test_owner_parked_gate_is_read_only(self):
        self._parked_owner(self._raise(),self._proceed())
        def snapshot():return (self.jobs.read_bytes(),sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*')))
        before=snapshot()
        self.assertEqual(R.owner_parked_gate(self.jobs,'att-source')['status'],'proceed')
        self.assertEqual(snapshot(),before)
        self.assertFalse(any(p.name=='state.json' for p in self.root.rglob('*')))

    def test_first_automatic_retry_is_admitted_at_spawn_with_its_own_row(self):
        route,path=self._legacy_real_route('spawn-self')
        R._route.return_value=(path,route)
        original={**self._legacy_row('att-f-original',route,path),'note':'dead-invalid-envelope'}
        retry={**self._legacy_row('att-f-retry',route,path),'automatic_retry_of':'att-f-original'}
        self.write(original);before=self.jobs.read_text().splitlines()
        self.write(retry,'open',append=True);spawned=self.jobs.read_text().splitlines()
        self.assertIsNone(R.admission(self.jobs,before,retry))    # registration: own row not yet present
        self.assertIsNone(R.admission(self.jobs,spawned,retry))   # spawn: own row present
        second={**retry,'attempt_id':'att-f-second'}
        with self.assertRaises(D.DispatchContractError) as caught:R.admission(self.jobs,spawned,second)
        self.assertEqual(caught.exception.reason,'automatic-replacement-exhausted')

    # -- an owner stopped at a usage limit: a pause with its own family, never the silent budget ----
    def _owner(self,**changes):
        self.write({**self.meta,'worker_type':'owner',**changes})

    def _capacity_owner(self):
        self._owner(note='dead-capacity',failure_class='capacity')

    def _successor(self,record,parent,status='done',**changes):
        """The registered replacement row of `record`, sealed like the adapter seals it."""
        aid=record['replacement_attempt_id']
        source=R._rows(self.jobs.read_text().splitlines())[record['original_attempt_id']][1]
        replay=R.launch_input(self.jobs,record['original_attempt_id'],source)
        args=SimpleNamespace(**vars(self.args));args.attempt_id=aid
        args.replacement_input_argv=R._replacement_argv(record,source,replay)
        meta={k:v for k,v in parent.items() if k not in ('note','failure_class','launch_outcome','replacement_input_digest')}
        meta.update(attempt_id=aid,automatic_retry_of=record['original_attempt_id'],launch_claimed='1',
                    replacement_family_id=record['family_id'],replacement_original_attempt_id=record['original_attempt_id'],
                    replacement_ordinal='1',replacement_claim_digest=R._digest(record),**changes)
        meta.update(D.parse_registry_metadata(R.seal_launch_input(args,'codex','the raw task')))
        self.write(meta,status,append=True)
        return meta

    def _die(self,meta,**changes):
        """Rewrite a successor row as terminal with the given death."""
        rows=[l for l in self.jobs.read_text().splitlines()
              if not D.row_has_attempt(l.split('\t')[5],meta['attempt_id'])]
        self.jobs.write_text('\n'.join(rows)+'\n')
        meta={**meta,**changes};self.write(meta,append=True);return meta

    def test_death_kind_names_why_a_row_may_be_replaced(self):
        row=lambda note='',status='done',**m:([ 'now',status],{**self.meta,'worker_type':'owner','note':note,**m})
        self.assertEqual(R.death_kind(*row('dead-exact-pid')),'silent')
        self.assertEqual(R.death_kind(*row('dead-capacity',failure_class='capacity')),'capacity')
        self.assertEqual(R.death_kind(*row('dead-worker-fail',failure_class='capacity')),'capacity')
        for case in (row('dead-capacity',status='cancelled'),row('dead-capacity',status='killed'),
                     row('cancelled-by-user'),row('dead-worker-fail'),
                     (['now','done'],{**self.meta,'worker_type':'frame','note':'dead-capacity'}),
                     (['now','done'],{**self.meta,'worker_type':'stage','dispatch_depth':'2','note':'dead-capacity'})):
            self.assertIsNone(R.death_kind(*case),case)
        # a runtime death (the process exited, or the runtime returned an error envelope) is the owner's alone
        for note in ('dead-runtime-exit','dead-runtime-error'):
            self.assertEqual(R.death_kind(*row(note,failure_class='runtime')),'runtime',note)
        self.assertIsNone(R.death_kind(*row('dead-runtime-error',status='cancelled')))
        self.assertIsNone(R.death_kind(['now','done'],{**self.meta,'worker_type':'stage','dispatch_depth':'2','note':'dead-runtime-error'}))

    def test_runtime_death_is_settled_by_its_own_error_envelope(self):
        self.absent.stop()
        meta={**self.meta,'worker_type':'owner','note':'dead-runtime-error','failure_class':'runtime'}
        fields=['now','done','',str(self.root)]
        def seen(state,failure_class):
            return mock.patch('codex_dispatch_terminal.inspect_terminal_attempt',
                              return_value={'state':state,'failure_class':failure_class})
        # a runtime death needs no handoff: an error envelope or no result is the death itself ...
        for state,failure,ok in (('invalid','runtime',True),('absent','',True),
                                 ('invalid','contract-violation',False),('valid','pass',False),
                                 ('invalid','capacity',False)):
            with self.subTest(state=state,failure=failure),seen(state,failure):
                self.assertEqual(R._terminal_absent(fields,meta,runtime=True),ok)
        # ... a silent death is still settled only by an absent result
        with seen('invalid','runtime'):
            self.assertFalse(R._terminal_absent(fields,meta))

    def test_capacity_death_with_invalid_capacity_terminal_is_settled_absent(self):
        self.absent.stop()
        def settle(state,failure_class):
            return mock.patch('codex_dispatch_terminal.inspect_terminal_attempt',
                              return_value={'state':state,'failure_class':failure_class})
        self._capacity_owner()
        with settle('invalid','capacity'):
            self.assertEqual(self.claim()['proof']['death_kind'],'capacity')
        for state,failure in (('invalid','contract-violation'),('valid','pass')):
            with self.subTest(state=state),settle(state,failure):
                with self.assertRaises(D.DispatchContractError) as caught:
                    R.death_proof(*R._rows(self.jobs.read_text().splitlines())['att-source'])
                self.assertEqual(caught.exception.reason,'replacement-result-settlement-required')
        # a silent death is settled only by an absent result, capacity evidence does not help it
        self._owner(note='dead-exact-pid')
        with settle('invalid','capacity'),self.assertRaises(D.DispatchContractError) as caught:
            self.claim()
        self.assertEqual(caught.exception.reason,'replacement-result-settlement-required')

    def test_capacity_replacement_death_opens_a_new_capacity_family_not_exhaustion(self):
        self._capacity_owner()
        first=self.claim()
        self.assertIn('after_capacity',first['logical_node'])
        one=self._successor(first,self.meta|{'worker_type':'owner'})
        # the replacement stops at a limit too: a new family for a new pause
        one=self._die(one,note='dead-capacity',failure_class='capacity')
        second=R.claim(self.jobs,one['attempt_id'])
        self.assertNotEqual(second['family_id'],first['family_id'])
        self.assertEqual(second['logical_node']['after_capacity'],one['attempt_id'])
        self.assertEqual(second['original_attempt_id'],one['attempt_id'])
        self.assertEqual(R.claim(self.jobs,one['attempt_id']),second)  # replay, not a third claim
        self.assertEqual(len(list((R._directory(self.jobs)/'claims').glob('*.json'))),2)

    def test_silent_budget_is_one_even_after_capacity_generations(self):
        self._capacity_owner()
        first=self.claim()
        one=self._die(self._successor(first,self.meta|{'worker_type':'owner'}),note='dead-capacity',failure_class='capacity')
        second=R.claim(self.jobs,one['attempt_id'])
        two=self._die(self._successor(second,one),note='dead-exact-pid',failure_class='contract')
        silent=R.claim(self.jobs,two['attempt_id'])     # the one silent replacement is still there
        self.assertNotIn('after_capacity',silent['logical_node'])
        three=self._die(self._successor(silent,two),note='dead-exact-pid',failure_class='contract')
        with self.assertRaises(D.DispatchContractError) as caught:R.claim(self.jobs,three['attempt_id'])
        self.assertEqual(caught.exception.reason,'automatic-replacement-exhausted')
        # and the reverse order: the silent budget used first is not refilled by a later pause
        self.jobs.write_text('');import shutil;shutil.rmtree(R._directory(self.jobs)/'claims');shutil.rmtree(R._directory(self.jobs)/'by-source')
        self._owner(note='dead-exact-pid')
        silent=self.claim()
        one=self._die(self._successor(silent,self.meta|{'worker_type':'owner'}),note='dead-capacity',failure_class='capacity')
        pause=R.claim(self.jobs,one['attempt_id'])
        self.assertIn('after_capacity',pause['logical_node'])
        two=self._die(self._successor(pause,one),note='dead-exact-pid',failure_class='contract')
        with self.assertRaises(D.DispatchContractError) as caught:R.claim(self.jobs,two['attempt_id'])
        self.assertEqual(caught.exception.reason,'automatic-replacement-exhausted')

    def test_replacement_row_as_source_binds_through_index_not_row(self):
        self._capacity_owner()
        first=self.claim()
        one=self._die(self._successor(first,self.meta|{'worker_type':'owner'}),note='dead-capacity',failure_class='capacity')
        second=R.claim(self.jobs,one['attempt_id'])
        row=R._rows(self.jobs.read_text().splitlines())[one['attempt_id']][1]
        self.assertEqual(row['replacement_family_id'],first['family_id'])       # its own creation, untouched
        self.assertEqual(R.source_reservation(self.jobs,one['attempt_id'])['family_id'],second['family_id'])
        self.assertEqual(R.source_binding(self.jobs,row),
                         (second['family_id'],second['replacement_attempt_id'],R._digest(second)))
        source=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        self.assertEqual(R.source_binding(self.jobs,source),
                         (first['family_id'],first['replacement_attempt_id'],R._digest(first)))
        self.assertIsNone(R.source_binding(self.jobs,{**row,'attempt_id':'att-never-a-source'}))

    def test_capacity_wait_writes_nothing(self):
        self._capacity_owner()
        self.route['selection_pins'] = {'owner': {'harness': 'codex'}}
        def files():return sorted(str(p.relative_to(R._directory(self.jobs))) for p in R._directory(self.jobs).rglob('*') if p.is_file())
        before=(files(),self.jobs.read_bytes())
        result=R.advance(self.jobs,'att-source',authority_check=lambda *_:True)
        self.assertEqual((result['state'],result['reason'],result['source_attempt_id']),
                         ('needs-attention','replacement-capacity-wait','att-source'))
        hold={'until_epoch':4102444800,'label':'2100-01-01T00:00:00Z'}
        with mock.patch('dispatch_capacity_evidence.harness_hold',return_value=hold):
            result=R.advance(self.jobs,'att-source',authority_check=lambda *_:True,resume_capacity=True)
        self.assertEqual((result['reason'],result['retry_at']),('replacement-capacity-wait','2100-01-01T00:00:00Z'))
        # the hold also stops an ordinary silent replacement before any claim
        self._owner(note='dead-exact-pid');before=(files(),self.jobs.read_bytes())
        with mock.patch('dispatch_capacity_evidence.harness_hold',return_value=hold):
            result=R.advance(self.jobs,'att-source',authority_check=lambda *_:True)
        self.assertEqual(result['reason'],'replacement-capacity-wait')
        self.assertEqual((files(),self.jobs.read_bytes()),before)
        self.assertFalse((R._directory(self.jobs)/'claims').exists())
        # the wait stays a valid supervisor attention item
        self.assertEqual(R.validate_attention(self.jobs,[result])[0]['reason'],'replacement-capacity-wait')

    def test_a_launched_replacement_is_not_disturbed_by_a_later_hold_or_release_change(self):
        self._owner(note='dead-exact-pid')
        record=self.claim()
        self._successor(record,self.meta|{'worker_type':'owner'},'open')
        hold={'until_epoch':4102444800,'label':'2100-01-01T00:00:00Z'}
        with mock.patch('dispatch_capacity_evidence.harness_hold',return_value=hold), \
             mock.patch.dict(os.environ,{'HEARTING_GATES':'on'}), mock.patch.object(R,'ROOT',self.root/'newer-release'):
            result=R.advance(self.jobs,'att-source',authority_check=lambda *_:True)
        self.assertEqual(result.get('state'),'running',result)
        self.assertEqual(result['attempt_id'],record['replacement_attempt_id'])

    # -- an owner its launcher closed before spawning: a pause the next `start` resumes ----------
    def _unlaunched_owner(self,**changes):
        self._owner(note='dead-producer-binding-failed',launch_outcome='never-launched',launch_claimed='0',
                    log_file=str(self.root/'never-written.log'),**changes)

    def test_owner_closed_before_spawn_is_an_unlaunched_pause(self):
        self.absent.stop()   # the real settlement check: this attempt never wrote a log
        self._unlaunched_owner()
        fields,meta=R._rows(self.jobs.read_text().splitlines())['att-source']
        self.assertEqual(R.death_kind(fields,meta),'unlaunched')
        record=self.claim()
        self.assertIn('after_capacity',record['logical_node'])
        self.assertEqual(record['proof']['death_kind'],'unlaunched')

    def test_unlaunched_owner_is_relaunched_only_by_start(self):
        self._unlaunched_owner()
        def files():return sorted(str(p) for p in R._directory(self.jobs).rglob('*') if p.is_file())
        before=(files(),self.jobs.read_bytes())
        result=R.advance(self.jobs,'att-source',authority_check=lambda *_:True,run=lambda *a,**k:self.fail('launched'))
        self.assertEqual(result,{'state':'not-applicable'})
        self.assertEqual((files(),self.jobs.read_bytes()),before)
        self.assertFalse((R._directory(self.jobs)/'claims').exists())
        commands=[]
        with mock.patch('dispatch_capacity_evidence.harness_hold',return_value=None),\
                mock.patch('dispatch_replacement_batch.command',return_value=None):
            result=R.advance(self.jobs,'att-source',authority_check=lambda *_:True,resume_capacity=True,
                             run=lambda command,**kw:commands.append(command) or SimpleNamespace(returncode=0,stdout='',stderr=''))
        self.assertEqual(len(commands),1)
        self.assertEqual(result['reason'],'replacement-launch-pending')
        self.assertEqual(len(list((R._directory(self.jobs)/'claims').glob('*.json'))),1)

    def test_unlaunched_replacement_that_again_never_started_opens_a_new_pause_family(self):
        self._unlaunched_owner()
        first=self.claim()
        one=self._die(self._successor(first,self.meta|{'worker_type':'owner'}),
                      note='dead-producer-binding-failed',launch_outcome='never-launched',launch_claimed='0')
        with mock.patch('dispatch_capacity_evidence.harness_hold',return_value=None),\
                mock.patch('dispatch_replacement_batch.command',return_value=None):
            result=R.advance(self.jobs,'att-source',authority_check=lambda *_:True,resume_capacity=True,
                             run=lambda *a,**k:SimpleNamespace(returncode=0,stdout='',stderr=''))
        self.assertNotEqual(result.get('reason'),'automatic-replacement-exhausted')
        second=R.claim(self.jobs,one['attempt_id'])
        self.assertNotEqual(second['family_id'],first['family_id'])
        self.assertEqual(second['logical_node']['after_capacity'],one['attempt_id'])

    def test_launched_owner_rows_are_never_unlaunched(self):
        base={**self.meta,'worker_type':'owner','note':'dead-producer-binding-failed',
              'launch_outcome':'never-launched','launch_claimed':'0'}
        self.assertEqual(R.death_kind(['now','done'],base),'unlaunched')
        for changes in ({'launch_started':'1'},{'pid':'4242'},{'launch_claimed':'1'},
                        {'worker_type':'stage','dispatch_depth':'2'},{'launch_outcome':''}):
            with self.subTest(changes=changes):
                self.assertNotEqual(R.death_kind(['now','done'],{**base,**changes}),'unlaunched')
        self.assertNotEqual(R.death_kind(['now','open'],base),'unlaunched')

    def test_unlaunched_recovery_text_says_it_never_started(self):
        self._unlaunched_owner()
        record=self.claim()
        text=R.recovery_instructions(SimpleNamespace(automatic_retry_of='att-source',worker_type='owner',
                                     jobs_path=self.jobs,attempt_id=record['replacement_attempt_id']))
        self.assertIn('never started',text)
        self.assertNotIn('You replace exact-dead attempt',text)

    def _blocked_owner(self):
        """An owner that ended BLOCKED with its input channel opened at launch, as its supervisor leaves it."""
        import dispatch_owner_input as I
        meta = {**self.meta, 'worker_type': 'owner', 'note': 'dead-worker-blocked', 'failure_class': 'blocked'}
        self.write(meta, 'open')
        I.initialize_owner_input(self.jobs, meta['attempt_id'], 'claude-next-turn')
        self.jobs.write_text(self.jobs.read_text().replace('\topen\t', '\tdone\t'))
        return meta

    def _answer(self, aid='att-source', text='approved: start the full run', request_id='answer-1'):
        import dispatch_owner_input as I
        return I.submit(self.jobs, aid, text, request_id)

    def _launch(self, aid='att-source'):
        commands = []
        with mock.patch('dispatch_capacity_evidence.harness_hold', return_value=None), \
                mock.patch('dispatch_replacement_batch.command', return_value=None):
            result = R.advance(self.jobs, aid, authority_check=lambda *_: True, resume_capacity=True,
                               run=lambda command, **kw: commands.append(command)
                               or SimpleNamespace(returncode=0, stdout='', stderr=''))
        return result, commands

    def test_an_owner_that_ended_blocked_continues_once_its_answer_arrives(self):
        self._blocked_owner()
        fields, meta = R._rows(self.jobs.read_text().splitlines())['att-source']
        self.assertIsNone(R.death_kind(fields, meta, jobs=self.jobs))   # waiting, not dead
        self.assertEqual(R.advance(self.jobs, 'att-source', authority_check=lambda *_: True,
                                   resume_capacity=True)['state'], 'not-applicable')
        self.assertFalse((R._directory(self.jobs) / 'claims').exists())
        self.assertTrue(self._answer()['retained'])
        self.assertEqual(R.death_kind(fields, meta, jobs=self.jobs), R.CORRECTED)
        # Only an explicit start (or the answer's own `correct`) launches; a supervisor tick does not.
        tick = R.advance(self.jobs, 'att-source', authority_check=lambda *_: True,
                         run=lambda *a, **k: self.fail('a tick launched'))
        self.assertEqual(tick, {'state': 'not-applicable'})
        self.assertFalse((R._directory(self.jobs) / 'claims').exists())
        result, commands = self._launch()
        self.assertEqual((len(commands), result['reason']), (1, 'replacement-launch-pending'))
        record = result['record']
        self.assertEqual(record['proof']['death_kind'], R.CORRECTED)
        self.assertEqual(record['logical_node']['after_capacity'], 'att-source')
        import dispatch_owner_input as I
        self.assertEqual(record['proof']['corrections'],
                         [{'id': 'answer-1', 'digest': I._digest('approved: start the full run')}])
        text = R.recovery_instructions(SimpleNamespace(automatic_retry_of='att-source', worker_type='owner',
                                       jobs_path=self.jobs, attempt_id=record['replacement_attempt_id']))
        for expected in ('ended BLOCKED and a person has answered it', 'approved: start the full run',
                         'do not ask for it again', '<owner-corrections>'):
            self.assertIn(expected, text)
        self.assertNotIn('You replace exact-dead attempt', text)

    def test_runtime_death_receives_correction_on_the_same_route_without_replaying_reviews(self):
        meta = self._blocked_owner()
        meta.pop('launch_outcome', None)
        meta.update(note='dead-runtime-exit', failure_class='runtime', launch_started='1',
                    supervisor_lease='flock-v1',
                    supervisor_lease_file=str(D.supervisor_lease_path(self.jobs, meta['attempt_id'])))
        self.write(meta)
        answer = '이미 통과한 검토를 보존하고 남은 관찰과 verdict만 완료하세요.'
        self.assertTrue(self._answer(text=answer)['retained'])
        R._reuse_snapshot.return_value['completed'] = [{'node': 'independent-verify'}]
        result, commands = self._launch()
        self.assertEqual(len(commands), 1)
        record = result['record']
        self.assertEqual(record['route_id'], self.route['route_id'])
        self.assertEqual(record['proof']['source_result'], 'EXITED')
        text = R.recovery_instructions(SimpleNamespace(automatic_retry_of='att-source', worker_type='owner',
                                       jobs_path=self.jobs, attempt_id=record['replacement_attempt_id']))
        for expected in (answer, 'independent-verify', 'Continue only unfinished work',
                         'exited before settlement', 'existing route'):
            self.assertIn(expected, text)
        self.assertNotIn('ended BLOCKED', text)

    def test_interrupted_supervisor_accepts_answer_after_previous_replacement(self):
        import dispatch_owner_input as I
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(harness=harness):
                self.tearDown_case()
                self.meta['harness'] = harness
                meta = self._blocked_owner()
                self._answer()
                first = self.claim()
                successor = self._successor(first, meta, status='open')
                I.initialize_owner_input(self.jobs, successor['attempt_id'], 'claude-next-turn')
                ended = self._die(successor, note='dead-protocol', failure_class='protocol',
                          reconcile_reason='terminal-event-missing', terminal_event='dispatch.supervisor.error',
                          launch_started='1', supervisor_lease='flock-v1',
                          supervisor_lease_file=str(D.supervisor_lease_path(self.jobs, successor['attempt_id'])))
                before = self.jobs.read_bytes()
                tick = R.advance(self.jobs, successor['attempt_id'], authority_check=lambda *_: True)
                self.assertNotEqual(tick.get('state'), 'started')
                self.assertEqual(self.jobs.read_bytes(), before)
                self.assertTrue(self._answer(successor['attempt_id'], text='정전 뒤 남은 평가만 이어가세요.')['retained'])
                record = R.claim(self.jobs, successor['attempt_id'])
                self.assertEqual(record['proof']['source_result'], 'EXITED')
                self.assertEqual(record['route_id'], first['route_id'])
                self.assertNotEqual(record['family_id'], first['family_id'])
                self.assertEqual(R.claim(self.jobs, successor['attempt_id']), record)

    def test_interrupted_supervisor_correction_excludes_results_cancel_and_unknown_process(self):
        import route_authority as RA
        meta = self._blocked_owner()
        meta.update(note='dead-protocol', failure_class='protocol', launch_started='1',
                    supervisor_lease='flock-v1', supervisor_lease_file='lease',
                    reconcile_reason='terminal-event-missing', terminal_event='dispatch.supervisor.error')
        self.write(meta)
        self.assertTrue(RA.runtime_owner_can_resume('done', meta))
        for change in ({'note': 'dead-worker-fail'}, {'note': 'completed-supervisor'},
                       {'reconcile_reason': 'terminal-envelope-invalid'}, {'launch_started': '0'},
                       {'terminal_event': 'dispatch.supervisor.done'}):
            self.assertFalse(RA.runtime_owner_can_resume('done', {**meta, **change}))
        self.assertFalse(RA.runtime_owner_can_resume('cancelled', meta))
        with mock.patch.object(RA, 'readable_result', return_value='PASS'):
            self.assertFalse(RA.runtime_owner_can_resume('done', meta))
        for state in ('live', 'unverifiable'):
            with self.subTest(state=state), mock.patch.object(D, 'attempt_process_quiescence',
                    return_value=SimpleNamespace(state=state, reason='fixture')):
                with self.assertRaisesRegex(Exception, 'owner-input-unavailable'):
                    self._answer()

    # -- an owner that ended with a readable FAIL, answered with a fix a person approved ----------
    def _failed_owner(self, test_fails=2):
        """BC rt-96bab699: the owner reported its test FAIL as its result; the check's rounds are spent."""
        import dispatch_owner_input as I
        self.route.update(effective_intensity='standard',
                          nodes=[{'id': 'test', 'kind': 'pipeline-stage', 'worker_type': 'test'}])
        meta = {**self.meta, 'worker_type': 'owner', 'note': 'dead-worker-fail', 'failure_class': 'fail'}
        self.write(meta, 'open')
        I.initialize_owner_input(self.jobs, meta['attempt_id'], 'claude-next-turn')
        self.jobs.write_text(self.jobs.read_text().replace('\topen\t', '\tdone\t'))
        for index in range(1, test_fails + 1):
            self.write({'attempt_schema_version': '2', 'attempt_id': f'att-test-{index}', 'route_id': 'rt-test',
                        'route_hash': 'sha256:test', 'route_node': 'test', 'worker_type': 'stage',
                        'dispatch_depth': '2', 'note': 'dead-worker-fail', 'failure_class': 'fail',
                        'parent_attempt_id': 'att-source'}, append=True)
        return meta

    def test_a_person_approved_fix_continues_an_owner_that_ended_fail(self):
        import route_authority as RA
        import review_round_cap
        self._failed_owner()
        fields, meta = R._rows(self.jobs.read_text().splitlines())['att-source']
        self.assertIsNone(R.death_kind(fields, meta, jobs=self.jobs))   # a readable FAIL is not retried
        self.assertTrue(self._answer(text='approved fix: guard the abort path', request_id='fix-1')['retained'])
        self.assertEqual(R.death_kind(fields, meta, jobs=self.jobs), R.CORRECTED)
        result, commands = self._launch()
        self.assertEqual((len(commands), result['reason']), (1, 'replacement-launch-pending'), result)
        record = result['record']
        self.assertEqual((record['proof']['source_result'], record['proof']['answers']), ('FAIL', ['att-test-2']))
        text = R.recovery_instructions(SimpleNamespace(automatic_retry_of='att-source', worker_type='owner',
                                       jobs_path=self.jobs, attempt_id=record['replacement_attempt_id']))
        for expected in ('ended FAIL and a person approved a fix', 'approved fix: guard the abort path',
                         'Rerun the stage that makes the approved fix', 'closure-check', 'att-test-2'):
            self.assertIn(expected, text)
        self.assertNotIn('Do not rerun completed nodes', text)
        # The fix answers the spent check's last FAIL: one closure-check round, from admission's own rule.
        revisions = R.answered_fix_revisions(self.jobs, 'rt-test')
        self.assertEqual([item['answers'] for item in revisions], [['att-test-2']])
        rows = [(status, row) for _aid, (fields_, row) in R._rows(self.jobs.read_text().splitlines()).items()
                for status in [fields_[1]] if row.get('route_node') == 'test']
        node = self.route['nodes'][0]
        self.assertEqual(review_round_cap.round_budget(self.route, node, rows).state, 'exhausted')
        budget = review_round_cap.round_budget(self.route, node, rows, revisions=revisions)
        self.assertEqual((budget.state, budget.round_kind), ('admit', 'closure-check'))
        self.assertEqual(RA.fix_answers(self.route, self.jobs.read_text().splitlines(), self.jobs), (['att-test-2'], []))

    def test_failed_owner_without_failed_checks_continues_once_per_answer(self):
        import dispatch_owner_input as I
        self._failed_owner(test_fails=0)
        self.assertTrue(self._answer(text='approved fix: retain missing GPU observation',
                                    request_id='infra-fix')['retained'])
        result, commands = self._launch()
        self.assertEqual((len(commands), result['reason']), (1, 'replacement-launch-pending'), result)
        record = result['record']
        self.assertEqual(record['proof']['source_result'], 'FAIL')
        self.assertEqual(record['proof']['answers'], [])
        self.assertEqual(record['logical_node']['after_capacity'], 'att-source')
        text = R.recovery_instructions(SimpleNamespace(automatic_retry_of='att-source', worker_type='owner',
                                       jobs_path=self.jobs, attempt_id=record['replacement_attempt_id']))
        self.assertIn('retain missing GPU observation', text)
        self.assertNotIn('closure-check', text)
        self.assertEqual(R.answered_fix_revisions(self.jobs, 'rt-test'), [])
        # Replay converges on the claim; the next FAIL waits for its own new answer.
        self.assertEqual(self.claim(), record)
        successor = self._successor(record, self.meta | {'worker_type': 'owner'}, status='open')
        I.initialize_owner_input(self.jobs, successor['attempt_id'], 'claude-next-turn')
        successor = self._die(successor, note='dead-worker-fail', failure_class='fail')
        retry, commands = self._launch(successor['attempt_id'])
        self.assertEqual(commands, [])
        self.assertNotEqual(retry.get('state'), 'running')
        self.assertEqual(len(list((R._directory(self.jobs) / 'claims').glob('*.json'))), 1)
        self.assertTrue(self._answer(aid=successor['attempt_id'], text='another fix',
                                    request_id='infra-fix-2')['retained'])
        # The route may still name the original owner: following lineage sees the new answer too.
        retry, commands = self._launch('att-source')
        self.assertEqual(len(commands), 1, retry)
        second = retry['record']
        self.assertEqual(second['logical_node']['after_capacity'], successor['attempt_id'])
        self.assertNotEqual(second['family_id'], record['family_id'])
        self.assertEqual(second['proof']['corrections'][0]['id'], 'infra-fix-2')
        self.assertEqual(R.claim(self.jobs, successor['attempt_id']), second)
        self.assertEqual(len(list((R._directory(self.jobs) / 'claims').glob('*.json'))), 2)
        rows = R._rows(self.jobs.read_text().splitlines())
        for aid in ('att-source', successor['attempt_id']):
            self.assertEqual((rows[aid][0][1], rows[aid][1]['note'], rows[aid][1]['failure_class']),
                             ('done', 'dead-worker-fail', 'fail'))

    def test_resource_fail_answer_continues_after_the_silent_replacement_was_spent(self):
        import dispatch_owner_input as I
        self._owner(note='dead-exact-pid')
        first = self.claim()
        self.assertNotIn('after_capacity', first['logical_node'])
        successor = self._successor(first, self.meta | {'worker_type': 'owner'}, status='open')
        I.initialize_owner_input(self.jobs, successor['attempt_id'], 'claude-next-turn')
        successor = self._die(successor, note='dead-worker-fail', failure_class='fail')
        self.assertTrue(self._answer(aid=successor['attempt_id'], text='approved: fix GPU check and run __a2',
                                    request_id='resource-fix')['retained'])
        result, commands = self._launch('att-source')
        self.assertEqual(len(commands), 1, result)
        record = result['record']
        self.assertEqual(record['route_id'], first['route_id'])
        self.assertEqual(record['proof']['death_kind'], R.CORRECTED)
        self.assertEqual(record['proof']['source_result'], 'FAIL')
        self.assertEqual(record['proof']['answers'], [])
        self.assertEqual(record['logical_node']['after_capacity'], successor['attempt_id'])
        text = R.recovery_instructions(SimpleNamespace(automatic_retry_of=successor['attempt_id'],
                                       worker_type='owner', jobs_path=self.jobs,
                                       attempt_id=record['replacement_attempt_id']))
        self.assertIn('fix GPU check and run __a2', text)
        self.assertNotIn('closure-check', text)

    def test_a_fix_for_a_check_that_used_its_closure_check_makes_no_round(self):
        import route_authority as RA
        self._failed_owner(test_fails=3)                                  # cap 2 + its one closure-check
        self.assertEqual(RA.fix_answers(self.route, self.jobs.read_text().splitlines(), self.jobs),
                         ([], ['test:exhausted']))
        self.assertTrue(self._answer(text='another fix', request_id='fix-2')['retained'])
        result, commands = self._launch()
        self.assertEqual(commands, [])
        self.assertEqual(result.get('reason'), 'replacement-fix-round-spent', result)
        self.assertFalse((R._directory(self.jobs) / 'claims').exists())

    def test_a_fix_does_not_answer_a_check_bound_by_blocked_rounds(self):
        # RA-5: a FAIL followed by two BLOCKED rounds without progress binds the node; the
        # closure-check admission would not run, so the fix answers nothing there.
        import route_authority as RA
        self._failed_owner(test_fails=1)
        for index in (1, 2):
            self.write({'attempt_schema_version': '2', 'attempt_id': f'att-test-blocked-{index}',
                        'route_id': 'rt-test', 'route_hash': 'sha256:test', 'route_node': 'test',
                        'worker_type': 'stage', 'dispatch_depth': '2', 'note': 'dead-worker-blocked',
                        'failure_class': 'blocked', 'parent_attempt_id': 'att-source'}, append=True)
        self.assertEqual(RA.fix_answers(self.route, self.jobs.read_text().splitlines(), self.jobs),
                         ([], ['test:verdictless-bound']))

    def test_an_owner_whose_fail_is_not_readable_keeps_no_answer(self):
        import dispatch_owner_input as I
        import route_authority as RA
        for meta, expected in (({'note': 'dead-worker-fail', 'failure_class': 'fail'}, 'FAIL'),
                               ({'note': 'dead-worker-blocked', 'failure_class': 'blocked'}, 'BLOCKED'),
                               ({'note': 'dead-worker-fail'}, ''), ({'note': 'dead-exact-pid'}, ''),
                               ({'note': 'dead-worker-fail', 'failure_class': 'fail', 'worker_type': 'review'}, '')):
            with self.subTest(meta=meta):
                self.assertEqual(RA.answerable_owner_end('done', meta), expected)
        self.assertEqual(RA.answerable_owner_end('open', {'note': 'dead-worker-fail', 'failure_class': 'fail'}), '')
        meta = {**self.meta, 'worker_type': 'owner', 'note': 'dead-exact-pid'}
        self.write(meta, 'open')
        I.initialize_owner_input(self.jobs, meta['attempt_id'], 'claude-next-turn')
        self.jobs.write_text(self.jobs.read_text().replace('\topen\t', '\tdone\t'))
        with self.assertRaises(I.InputError) as refused:
            self._answer()
        self.assertEqual(str(refused.exception), "owner-input-unavailable-retain-correction")

    def _move_owner(self, harness='claude'):
        import route_authority as RA
        return RA.record_pin_change(self.route, target='owner', pin={'harness': harness}, by={'harness': 'codex',
                                    'session_id': 'parent'}, source='unattributed', tuples=[], candidates=[])

    def _moved_successor(self, record, command, harness='claude', write=True):
        """The new harness's wrapper seals the forwarded owner command and registers the replacement."""
        aid = record['replacement_attempt_id']
        args = SimpleNamespace(**vars(self.args)); args.attempt_id = aid
        args.automatic_retry_of = record['original_attempt_id']
        args.replacement_input_argv = command[3:]
        meta = {k: v for k, v in self.meta.items() if k not in ('note', 'failure_class', 'launch_outcome',
                                                                'replacement_input_digest')}
        meta.update(worker_type='owner', harness=harness, attempt_id=aid, automatic_retry_of='att-source',
                    launch_claimed='0', replacement_family_id=record['family_id'],
                    replacement_original_attempt_id='att-source', replacement_ordinal='1',
                    replacement_claim_digest=R._digest(record))
        meta.update(D.parse_registry_metadata(R.seal_launch_input(args, harness, 'the raw task')))
        if write:
            self.write(meta, 'open', append=True)
        return meta

    def test_a_parent_moved_owner_pin_continues_the_answered_owner_on_the_new_harness(self):
        # RA-3: BC's Codex owner ended BLOCKED; its parent moved the owner pin to Claude and answered.
        self._blocked_owner()
        self._answer()
        self.assertEqual(self._move_owner()['previous'], None)
        result, commands = self._launch()
        record = result['record']
        self.assertEqual(record['harness'], 'claude')
        (command,) = commands
        self.assertTrue(command[1].endswith('utilities/dispatch-owner.py'))
        self.assertNotIn('--adapter', command)  # ordinary selector reads the current route pin
        self.assertEqual(command[command.index('--attempt-id') + 1], record['replacement_attempt_id'])
        self.assertEqual(command[command.index('--automatic-retry-of') + 1], 'att-source')
        self.assertEqual(Path(command[command.index('--prompt-file') + 1]).read_text(), 'the raw task')
        # The Claude wrapper's own sealed input is admitted as the same work ...
        moved = self._moved_successor(record, command)
        lines = self.jobs.read_text().splitlines()
        self.assertEqual(R.admission(self.jobs, lines, moved)['harness'], 'claude')
        text = R.recovery_instructions(SimpleNamespace(automatic_retry_of='att-source', worker_type='owner',
                                       jobs_path=self.jobs, attempt_id=record['replacement_attempt_id']))
        self.assertIn('approved: start the full run', text)        # the answer goes first, unchanged
        # ... and a launch on any other harness is not.
        wrong = {**moved, 'attempt_id': record['replacement_attempt_id']}
        with mock.patch.object(R, 'launch_input', side_effect=lambda jobs, aid, meta: {
                **json.loads((R._directory(self.jobs) / 'inputs' / (aid + '.json')).read_text()),
                **({'harness': 'opencode'} if aid == record['replacement_attempt_id'] else {})}):
            with self.assertRaises(D.DispatchContractError) as refused:
                R.admission(self.jobs, lines, wrong)
        self.assertEqual((refused.exception.reason, refused.exception.detail),
                         ('pin-ignored-for-replacement', 'claude'))

    def test_a_replacement_owner_runs_with_the_access_request_its_parent_handed_over(self):
        # BC rt-839dbd48: the parent handed a new request and answered; the replacement replayed the first one.
        import execution_access as EA
        self._blocked_owner()
        self._answer()
        access = {'request_path': str(self.root / 'given.json'), 'request_sha256': 'b' * 64,
                  'source': 'unattributed', 'at': '2026-10-07T00:00:00Z'}
        with mock.patch.object(R, '_access_in_force', return_value=access):
            result, commands = self._launch()
        record = result['record']
        self.assertEqual(record['execution_access'], access)
        (command,) = commands
        self.assertEqual(command.count('--execution-access-file'), 1)
        self.assertEqual(command[command.index('--execution-access-file') + 1], access['request_path'])
        def grant(sha):
            return EA.ExecutionAccessGrant(request_sha256=sha, writable_roots=(), read_roots=(Path('/data'),),
                                           additional_writable_roots=(), absorbed_writable_roots=(),
                                           network='not-requested', file_enforcement='os-sandbox',
                                           network_enforcement='os-sandbox', unmet=())
        self.args.execution_access_grant = grant('b' * 64)
        successor = self._successor(record, self.meta | {'worker_type': 'owner'}, status='open')
        lines = self.jobs.read_text().splitlines()
        self.assertEqual(R.admission(self.jobs, lines, successor)['execution_access'], access)
        # A replacement that still carries the first request is not the claimed one.
        sealed = json.loads((R._directory(self.jobs) / 'inputs' / (successor['attempt_id'] + '.json')).read_text())
        sealed['applied_permissions']['execution_access']['request_sha256'] = 'c' * 64
        with mock.patch.object(R, 'launch_input', side_effect=lambda jobs, aid, meta: sealed
                               if aid == successor['attempt_id'] else json.loads(
                                   (R._directory(self.jobs) / 'inputs' / (aid + '.json')).read_text())):
            with self.assertRaises(D.DispatchContractError) as refused:
                R.admission(self.jobs, lines, successor)
        self.assertEqual((refused.exception.reason, refused.exception.detail),
                         ('replacement-input-tuple-mismatch', 'execution_access'))

    def test_the_access_in_force_is_prepared_from_the_route_s_record(self):
        import execution_access as EA
        import route_authority as RA
        route = {'route_id': 'rt-access', 'route_hash': 'sha256:' + 'e' * 64, 'artifact_root': str(self.root),
                 'cwd': str(self.root / 'wt'), 'capability': 'autopilot-code', 'work_request': {'text': 'task'}}
        (self.root / 'wt').mkdir()
        data = self.root / 'data'
        data.mkdir()
        self.assertIsNone(R._access_in_force(self.jobs, route))
        given = self.root / 'given-input.json'
        given.write_text(json.dumps({'schema_version': 1, 'writable_roots': [], 'read_roots': [str(data)],
                                     'network': {'required': False}}))
        context = EA.AccessContext.build(worktree=route['cwd'], artifact_root=self.root,
                                         dispatch_state_root=self.jobs.parent, agent_home=R.ROOT)
        request = EA.load_request(given, context=context)
        RA.record_access_change(route, request=EA.normalized_request(request), request_sha256=request.request_sha256,
                                by={}, source='derived')
        self.assertIsNone(R._access_in_force(self.jobs, route))     # a derived row is the first request itself
        RA.record_access_change(route, request=EA.normalized_request(request), request_sha256='f' * 64,
                                by={}, source='unattributed')
        self.assertIsNone(R._access_in_force(self.jobs, route))     # a row that does not digest to its request
        self.assertIsNone(RA.record_access_change(route, request=EA.normalized_request(request),
                                                  request_sha256=request.request_sha256, by={},
                                                  source='unattributed'))  # the same request is already in force
        (self.root / 'more').mkdir()
        given.write_text(json.dumps({'schema_version': 1, 'writable_roots': [], 'network': {'required': False},
                                     'read_roots': [str(data), str(self.root / 'more')]}))
        handed = EA.load_request(given, context=context)
        RA.record_access_change(route, request=EA.normalized_request(handed), request_sha256=handed.request_sha256,
                                by={}, source='unattributed')
        access = R._access_in_force(self.jobs, route)
        self.assertEqual((access['request_sha256'], access['source']), (handed.request_sha256, 'unattributed'))
        self.assertEqual(json.loads(Path(access['request_path']).read_text())['read_roots'],
                         [str(data), str(self.root / 'more')])

    def test_a_pin_moved_after_the_claim_controls_the_next_owner_launch(self):
        self._blocked_owner()
        self._answer()
        record = self.claim()
        self.assertNotIn('harness', record)
        self._move_owner()
        result, commands = self._launch()
        self.assertTrue(commands[0][1].endswith('utilities/dispatch-owner.py'))
        self.assertEqual(result['record'], record)  # lineage remains immutable
        moved = self._moved_successor(record, commands[0])
        self.assertEqual(R.admission(self.jobs, self.jobs.read_text().splitlines(), moved), record)
        self.assertIn('approved: start the full run', R.recovery_instructions(SimpleNamespace(
            automatic_retry_of='att-source', worker_type='owner', jobs_path=self.jobs,
            attempt_id=record['replacement_attempt_id'])))

    def test_an_unstarted_registered_owner_can_follow_a_later_pin(self):
        self._blocked_owner(); self._answer()
        record = self.claim()
        _, commands = self._launch()
        old = self._moved_successor(record, commands[0], harness='codex')
        self._move_owner('opencode')
        _, commands = self._launch()
        candidate = self._moved_successor(record, commands[0], harness='opencode', write=False)
        row = 'now\topen\t'+str(self.root)+'\t'+str(self.root)+'\ttask\t'+','.join(k+'='+v for k,v in candidate.items())
        self.assertFalse(D.claim_attempt_row(self.jobs, candidate['attempt_id'], row, launch=False))
        current = R._rows(self.jobs.read_text().splitlines())[candidate['attempt_id']][1]
        self.assertEqual((current['harness'], current['launch_claimed']), ('opencode', '0'))
        self.assertEqual(current['replacement_claim_digest'], old['replacement_claim_digest'])

    def test_a_started_replacement_with_a_conflicting_pin_returns_a_diagnostic(self):
        self._blocked_owner(); self._answer()
        record = self.claim()
        old = self._successor(record, self.meta | {'worker_type': 'owner'}, status='open')
        self._move_owner('opencode')
        result, commands = self._launch()
        self.assertEqual(commands, [])
        self.assertEqual((result['state'], result['reason'], result['requested_harness']),
                         ('needs-attention', 'pin-ignored-for-replacement', 'opencode'))
        self.assertEqual(R._rows(self.jobs.read_text().splitlines())[old['attempt_id']][1]['harness'], 'codex')
        self.assertEqual(R.validate_attention(self.jobs, [result])[0]['reason'], 'pin-ignored-for-replacement')

    def test_only_real_limits_on_every_selector_candidate_become_a_capacity_pause(self):
        self._blocked_owner(); self._answer()
        limited = {'label': 'limited', 'until_epoch': 4102444800}
        diagnostic = 'reason=no-eligible-route-evidence-candidate\nconfigured_candidates=claude,codex,opencode\n'
        for holds, expected in ((dict.fromkeys(('claude', 'codex', 'opencode'), limited), 'replacement-capacity-wait'),
                                ({'claude': limited, 'codex': None, 'opencode': limited}, 'replacement-launch-pending')):
            with self.subTest(expected=expected), mock.patch('dispatch_capacity_evidence.harness_hold',
                    side_effect=lambda jobs, harness, **kw: holds[harness]):
                result = R.advance(self.jobs, 'att-source', authority_check=lambda *_: True, resume_capacity=True,
                                   run=lambda *a, **k: SimpleNamespace(returncode=65, stdout=diagnostic, stderr=''))
            self.assertEqual(result['reason'], expected)
        # A pin rejected by the actual launch seam retains its explicit diagnostic.
        result = R.advance(self.jobs, 'att-source', authority_check=lambda *_: True, resume_capacity=True,
                           run=lambda *a, **k: SimpleNamespace(returncode=64, stdout='reason=pin-ignored-for-replacement\n', stderr=''))
        self.assertEqual(result['reason'], 'pin-ignored-for-replacement')

    def test_a_registered_owner_cannot_reseal_after_launch_started(self):
        self._blocked_owner(); self._answer()
        record = self.claim()
        _, commands = self._launch()
        self._moved_successor(record, commands[0], harness='codex')
        self.jobs.write_text(self.jobs.read_text().replace('launch_claimed=0', 'launch_claimed=1'))
        self._move_owner('opencode')
        before = (R._directory(self.jobs)/'inputs'/(record['replacement_attempt_id']+'.json')).read_bytes()
        with self.assertRaises(D.DispatchContractError):
            self._moved_successor(record, commands[0], harness='opencode', write=False)
        self.assertEqual((R._directory(self.jobs)/'inputs'/(record['replacement_attempt_id']+'.json')).read_bytes(), before)

    def test_the_replacement_command_uses_the_real_owner_selector(self):
        spec = importlib.util.spec_from_file_location('replacement_owner_selector_test', Path(__file__).with_name('dispatch_owner.test.py'))
        suite = importlib.util.module_from_spec(spec); spec.loader.exec_module(suite)
        self._blocked_owner(); self._answer()
        record = self.claim()  # the original Codex claim already exists
        helper = suite.RouteOwnerPinTest()
        for pin, scores, usage, expected in (
                ('claude', {'claude': 1, 'codex': 80, 'opencode': 80}, None, 'claude'),
                ('opencode', {'claude': 1, 'codex': 80, 'opencode': 1}, None, 'opencode'),
                ('codex', {'claude': 80, 'codex': 1, 'opencode': 80}, None, 'codex'),
                (None, {'claude': 80, 'codex': 1, 'opencode': 80}, None, 'claude'),
                (None, {'claude': 80, 'codex': 80, 'opencode': 80}, {'claude': 'limited', 'codex': 'limited', 'opencode': 'limited'}, None)):
            with self.subTest(pin=pin, expected=expected):
                path = helper._route(pin=pin, sealed=('claude', 'codex', 'opencode'))
                self.addCleanup(lambda p=path: __import__('shutil').rmtree(p.parent))
                route = json.loads(path.read_text()); route['cwd'] = str(self.root)
                path.write_text(json.dumps(route))
                command = R._owner_command(self.jobs, {**record, 'route_file': str(path)},
                                          self.meta | {'worker_type': 'owner'}, R.launch_input(self.jobs, 'att-source', self.meta))
                extra = command[3:]
                del extra[extra.index('--route-evidence'):extra.index('--route-evidence')+2]
                with mock.patch.object(suite.OWNER._capacity, 'capacity_report',
                                       return_value={'scores': scores, 'sources': dict.fromkeys(scores, 'fixture')}):
                    selected = helper._launch(path, None, usage=usage, scores=scores, extra=extra)
                self.assertEqual(selected.wrapper, expected, selected.out)
                if expected:
                    launched = selected.calls[0]
                    self.assertEqual(launched[launched.index('--model-profile')+1], 'deep')
                    self.assertEqual(launched[launched.index('--automatic-retry-of')+1], 'att-source')
                else:
                    self.assertIn('no-eligible-route-evidence-candidate', selected.out)

    def test_gpu_lab_correction_prepares_compute_defaults_instead_of_replaying_old_access(self):
        import execution_access as EA
        inventory = self.root / 'config/hearting/compute-hosts.yaml'
        inventory.parent.mkdir(parents=True)
        inventory.write_text(f'schema_version: 1\nrun_root: {self.root}/runs\nhosts:\n  fixture:\n    ssh_host: local\n')
        route = {'route_id': 'rt-compute', 'route_hash': 'sha256:' + 'a' * 64,
                 'artifact_root': str(self.root), 'cwd': str(self.root),
                 'capability': 'autopilot-lab', 'nodes': [], 'work_request': {'text': 'GPU lab'}}
        with mock.patch.dict(os.environ, {'COMPUTE_HOSTS_CONFIG': str(inventory)}):
            old = EA.prepare_task_request(route, self.jobs, environment=False)
            old_bytes = old.read_bytes()
            route['nodes'] = [{'id': 'full-run', 'kind': 'resource-runner'}]
            # No manual access-change row or extra parent input is required.
            access = R._access_in_force(self.jobs, route)
        request = json.loads(Path(access['request_path']).read_text())
        self.assertTrue(request['network']['required'])
        self.assertIn(str(inventory.parent), request['read_roots'])
        self.assertEqual(access['source'], 'lab-runtime-defaults')
        self.assertEqual(old.read_bytes(), old_bytes)
        replay = {'argv': ['--execution-access-file', str(old)], 'harness': 'opencode'}
        argv = R._replacement_argv({'execution_access': access}, {'worker_type': 'owner'}, replay)
        self.assertEqual(argv[argv.index('--execution-access-file') + 1], access['request_path'])
        self.assertNotIn(str(old), argv)

    def test_a_sealed_pin_also_controls_the_replacement_launch(self):
        self._blocked_owner()
        self._answer()
        self.route['selection_pins'] = {'owner': {'harness': 'claude'}}   # sealed, never followed by the launch
        result, commands = self._launch()
        self.assertNotIn('harness', result['record'])
        self.assertTrue(commands[0][1].endswith('utilities/dispatch-owner.py'))

    def test_an_answer_queued_just_before_the_owner_ended_blocked_continues_it(self):
        import dispatch_owner_input as I
        meta = {**self.meta, 'worker_type': 'owner'}
        self.write(meta, 'open')
        I.initialize_owner_input(self.jobs, 'att-source', 'claude-next-turn')
        self.assertFalse(I.submit(self.jobs, 'att-source', 'approved: go', 'early').get('retained'))
        self.write({**meta, 'note': 'dead-worker-blocked', 'failure_class': 'blocked'})
        fields, row = R._rows(self.jobs.read_text().splitlines())['att-source']
        self.assertEqual(R.death_kind(fields, row, jobs=self.jobs), R.CORRECTED)
        record = self.claim()
        text = R.recovery_instructions(SimpleNamespace(automatic_retry_of='att-source', worker_type='owner',
                                       jobs_path=self.jobs, attempt_id=record['replacement_attempt_id']))
        self.assertIn('approved: go', text)

    def test_a_changed_pinned_answer_is_refused_rather_than_sent(self):
        self._blocked_owner()
        self._answer()
        record = self.claim()
        import dispatch_owner_input as I
        path = I._path(self.jobs, 'att-source')
        value = json.loads(path.read_text())
        value['requests'][0]['text'] = 'something else'
        value['requests'][0]['digest'] = I._digest('something else')
        path.write_text(json.dumps(value))
        with self.assertRaises(D.DispatchContractError) as caught:
            R.recovery_instructions(SimpleNamespace(automatic_retry_of='att-source', worker_type='owner',
                                    jobs_path=self.jobs, attempt_id=record['replacement_attempt_id']))
        self.assertEqual(caught.exception.reason, 'replacement-correction-drift')

    def test_a_gate_park_keeps_its_own_answer_path(self):
        self._parked_owner(self._raise())
        import dispatch_owner_input as I
        lines = self.jobs.read_text().replace('\tdone\t', '\topen\t')
        self.jobs.write_text(lines)
        I.initialize_owner_input(self.jobs, 'att-source', 'claude-next-turn')
        self.jobs.write_text(lines.replace('\topen\t', '\tdone\t'))
        self._answer()
        fields, meta = R._rows(self.jobs.read_text().splitlines())['att-source']
        self.assertIsNone(R.death_kind(fields, meta, jobs=self.jobs))
        with self.assertRaises(D.DispatchContractError):
            self.claim()

    def test_an_answered_replacement_that_ends_blocked_again_continues_again(self):
        self._blocked_owner()
        self._answer()
        first = self.claim()
        successor = self._successor(first, self.meta | {'worker_type': 'owner'}, status='open')
        import dispatch_owner_input as I
        I.initialize_owner_input(self.jobs, successor['attempt_id'], 'claude-next-turn')
        successor = self._die(successor, note='dead-worker-blocked', failure_class='blocked')
        self._answer(successor['attempt_id'], 'approved: phase two as well', 'answer-2')
        result, commands = self._launch(successor['attempt_id'])
        self.assertNotEqual(result.get('reason'), 'automatic-replacement-exhausted', result)
        self.assertEqual(len(commands), 1)
        second = result['record']
        self.assertNotEqual(second['family_id'], first['family_id'])
        self.assertEqual(second['logical_node']['after_capacity'], successor['attempt_id'])

    def test_an_answered_replacement_its_launcher_refused_hands_the_answer_to_the_next_owner(self):
        self._blocked_owner()
        self._answer()
        first = self.claim()
        # The wrapper refused at spawn and closed the row: no model ever saw this prompt.
        refused = self._die(self._successor(first, self.meta | {'worker_type': 'owner'}, status='open'),
                            note='dead-launch-error', launch_outcome='never-launched', launch_claimed='0')
        result, commands = self._launch(refused['attempt_id'])
        self.assertEqual(len(commands), 1, result)
        second = result['record']
        self.assertEqual(second['proof']['death_kind'], 'unlaunched')
        def text():
            return R.recovery_instructions(SimpleNamespace(
                automatic_retry_of=refused['attempt_id'], worker_type='owner', jobs_path=self.jobs,
                attempt_id=second['replacement_attempt_id']))
        rendered = text()
        for expected in ('never started', f"{refused['attempt_id']} was itself the continuation of att-source",
                         'ended BLOCKED and a person has answered it', 'approved: start the full run',
                         'do not ask for it again', '<owner-corrections>'):
            self.assertIn(expected, rendered)
        # The answer is checked against the pinned digest exactly as before.
        import dispatch_owner_input as I
        path = I._path(self.jobs, 'att-source')
        value = json.loads(path.read_text())
        value['requests'][0].update(text='something else', digest=I._digest('something else'))
        path.write_text(json.dumps(value))
        with self.assertRaises(D.DispatchContractError) as caught:
            text()
        self.assertEqual(caught.exception.reason, 'replacement-correction-drift')

    def test_an_unlaunched_owner_that_was_not_a_refused_replacement_carries_nothing_more(self):
        self._unlaunched_owner()
        record = self.claim()
        text = R.recovery_instructions(SimpleNamespace(automatic_retry_of='att-source', worker_type='owner',
                                       jobs_path=self.jobs, attempt_id=record['replacement_attempt_id']))
        self.assertIn('never started', text)
        self.assertNotIn('was itself the continuation', text)
        self.assertNotIn('<owner-corrections>', text)

    def test_stage_worker_capacity_death_is_not_a_replacement_source(self):
        self.write({**self.meta,'worker_type':'stage','dispatch_depth':'2','note':'dead-capacity','failure_class':'capacity'})
        self.assertIsNone(R.death_kind(['now','done'],R._rows(self.jobs.read_text().splitlines())['att-source'][1]))
        self.assertEqual(R.advance(self.jobs,'att-source',authority_check=lambda *_:True,resume_capacity=True)['state'],'not-applicable')
        _,_,attention=R.advance_batch(self.jobs,{'att-source'},authority_check=lambda *_:True)
        self.assertEqual(attention,[])
        with self.assertRaises(D.DispatchContractError):self.claim()
        self.assertFalse((R._directory(self.jobs)/'claims').exists())

    def test_capacity_recovery_text_says_resume_not_replace(self):
        self._capacity_owner()
        record=self.claim()
        text=R.recovery_instructions(SimpleNamespace(automatic_retry_of='att-source',worker_type='owner',
                                     jobs_path=self.jobs,attempt_id=record['replacement_attempt_id']))
        self.assertIn('stopped at a usage limit; this resumes it',text)
        self.assertNotIn('You replace exact-dead attempt',text)
        self.assertIn(R.CONTINUATION_WAIT_NOTE,text)

    def test_cleanup_pending_owner_is_settled_only_when_the_runtime_can_prove_it(self):
        self._owner()
        calls=[]
        for state,expected in (('unverifiable',[('att-source',True)]),('live',[]),('quiescent',[])):
            calls.clear()
            rows=R._rows(self.jobs.read_text().splitlines())
            with mock.patch.object(D,'attempt_process_quiescence',return_value=SimpleNamespace(state=state,reason='r')), \
                 mock.patch.object(D,'resolve_attempt_cleanup',side_effect=lambda j,a,apply=False:calls.append((a,apply))):
                R._settle_terminal_cleanup(self.jobs,rows,'att-source',rows['att-source'][1])
            self.assertEqual(calls,expected,state)

    def test_release_drift_follows_the_release_and_identity_stays_strict(self):
        replay=R.launch_input(self.jobs,'att-source',R._rows(self.jobs.read_text().splitlines())['att-source'][1])
        old={**replay,'launch_home':'/releases/v-old','resolved':{'model':'m1','permission_mode':'default'}}
        R._check_tuple(old,old)
        newer={**old,'launch_home':replay['launch_home'],'resolved':{'model':'m2','permission_mode':'default'}}
        with mock.patch.dict(os.environ,{'HEARTING_GATES':'off'}):
            R._check_tuple(newer,old)
            with self.assertRaises(D.DispatchContractError) as caught:
                R._check_tuple({**newer,'resolved':{'model':'m2','permission_mode':'config'}},old)
            self.assertEqual((caught.exception.reason,caught.exception.detail),('replacement-input-tuple-mismatch','resolved'))
            with self.assertRaises(D.DispatchContractError):R._check_tuple({**newer,'harness':'claude'},old)
        # The release is where the launch ran, not the work: no refusal with the gates on either.
        with mock.patch.dict(os.environ,{'HEARTING_GATES':'on'}):
            R._check_tuple(newer,old)
            with self.assertRaises(D.DispatchContractError):
                R._check_tuple({**newer,'resolved':{'model':'m2','permission_mode':'config'}},old)
        same_release=dict(old,resolved={'model':'m3','permission_mode':'default'})
        with self.assertRaises(D.DispatchContractError):R._check_tuple(same_release,old)

    def test_a_release_root_spelled_through_its_pointer_is_the_same_permission(self):
        # TF rt-34ca7756: the owner's rules named releases/v3.13.0, the correcting session's
        # replacement named the `current` pointer to it, and the same work was refused.
        share=self.root/'share'
        for tree in (share/'releases'/'v1',share/'releases'/'v2',self.root/'dev'):
            (tree/'core').mkdir(parents=True)
            (tree/'core'/'CORE.md').write_text('core')
        (share/'current').symlink_to(share/'releases'/'v1')
        def launch(root,home,*extra):
            rules=[f'Bash(python3 {root}/utilities/capability-route.py *)',f'Edit(//{self.root}/wt/**)',*extra]
            return {'harness':'claude','jobs':str(self.jobs),'worktree':str(self.root/'wt'),'launch_home':str(home),
                    'resolved':{'model':'m1'},'applied_permissions':{'launch_lifecycle':'detached',
                    'claude':{'mode':'bypass','allowed_tools':rules}}}
        v1,v2=share/'releases'/'v1',share/'releases'/'v2'
        sealed=launch(v1,v1)
        import route_authority
        self.assertTrue(route_authority.same_sealed_work(sealed,launch(share/'current',v1)))
        with mock.patch.dict(os.environ,{'HEARTING_GATES':'on'}):
            R._check_tuple(launch(share/'current',v1),sealed)
            # BC r2: sealed through the pointer at v1; the pointer and the replacement are at v2 now.
            (share/'current').unlink();(share/'current').symlink_to(v2)
            R._check_tuple(launch(v2,v2),launch(share/'current',v1))
            R._check_tuple(launch(share/'current',v2),launch(share/'current',v1))
            # Another harness tree, such as a development checkout, is not the same release root.
            for changed in (launch(self.root/'dev',v1),launch(share/'current',v1,'Bash(git push *)'),
                            {**launch(share/'current',v1),'applied_permissions':{'claude':{'mode':'bypass',
                             'allowed_tools':[f'Bash(python3 {v1}/utilities/capability-route.py *)',
                                              f'Edit(//{self.root}/other/**)']}}}):
                with self.assertRaises(D.DispatchContractError) as caught:
                    R._check_tuple(changed,sealed)
                self.assertEqual((caught.exception.reason,caught.exception.detail),
                                 ('replacement-input-tuple-mismatch','applied_permissions'))


class FrameTopCapacityTest(unittest.TestCase):
    """D2: a frame leg at the `top` the frame rule assigned is replaced once, at `deep`, when it stops
    at a usage limit. Nothing else lowers a profile, and a second limit is the existing exhaustion."""
    write=ReplacementTest.write
    claim=ReplacementTest.claim
    _successor=ReplacementTest._successor
    _die=ReplacementTest._die

    harness='claude'

    def setUp(self):
        ReplacementTest.setUp(self)
        import model_profile
        self.node={'id':'frame','worker_type':'frame','model_profile':'top','dispatch_depth':1,'profile_demand':None,
                   'profile_selection':model_profile.resolve_profile_demand(None,explicit_profile='top')}
        self.set_route()
        self.args.model_profile='top';self.args.model='m-top'
        self.args.resolved_model_settings={'profile':'top','model':'m-top','effort':'max'}
        (R._directory(self.jobs)/'inputs/att-source.json').unlink()
        self.meta.update(note='dead-capacity',failure_class='capacity',model_profile='top',harness=self.harness,
                         route_file=str(self.path),model='m-top',
                         **D.parse_registry_metadata(R.seal_launch_input(self.args,self.harness,'the raw task')))
        self.write(self.meta)

    def set_route(self,**changes):
        self.route.update(effective_intensity='standard',nodes=[self.node],profile_selection_contract_version=1,**changes)
        self.path.write_text(json.dumps(self.route))

    def rows(self):return R._rows(self.jobs.read_text().splitlines())

    def kind(self,**changes):
        self.write({**self.meta,**changes})
        fields,meta=self.rows()['att-source']
        return R.death_kind(fields,meta,jobs=self.jobs)

    def candidate(self,record,profile='deep'):
        """The replacement row as a wrapper registers it, launched at `profile`."""
        source=self.rows()['att-source'][1]
        replay=R.launch_input(self.jobs,'att-source',source)
        args=SimpleNamespace(**vars(self.args));args.attempt_id=record['replacement_attempt_id']
        args.replacement_input_argv=R._replacement_argv(record,source,replay)
        args.model_profile=profile;args.model='m-'+profile
        args.resolved_model_settings={'profile':profile,'model':'m-'+profile,'effort':'high'}
        meta={**self.meta,'attempt_id':record['replacement_attempt_id'],'automatic_retry_of':'att-source',
              'model_profile':profile,'model':'m-'+profile}
        for key in ('note','failure_class'):meta.pop(key)
        meta.update(D.parse_registry_metadata(R.seal_launch_input(args,self.harness,'the raw task')))
        return meta

    def test_a_frame_rule_top_capacity_death_is_its_own_kind(self):
        self.assertEqual(self.kind(),R.FRAME_CAPACITY)
        self.assertEqual(self.kind(note='dead-worker-fail',failure_class='capacity'),R.FRAME_CAPACITY)

    def test_nothing_but_a_frame_rule_top_capacity_death_is_a_downgrade_kind(self):
        for label,changes in {
            'auth':{'note':'dead-worker-fail','failure_class':'auth'},
            'valid fail':{'note':'dead-worker-fail','failure_class':'contract'},
            'cancelled':{'note':'cancelled-by-user','failure_class':''},
            'unverifiable death':{'note':'dead-unverifiable','failure_class':''},
            'not at top':{'model_profile':'deep'},
            'a replacement row':{'replacement_original_attempt_id':'att-earlier'},
            'a stage worker':{'worker_type':'stage','dispatch_depth':'2'},
        }.items():
            with self.subTest(label):self.assertNotEqual(self.kind(**changes),R.FRAME_CAPACITY)
        self.assertIsNone(self.kind(note='dead-worker-fail',failure_class='auth'))
        fields,meta=self.rows()['att-source']
        self.assertIsNone(R.death_kind(['now','cancelled'],{**self.meta,'model_profile':'top'},jobs=self.jobs))
        self.assertIsNone(R.death_kind(fields,{**self.meta},lines=None))  # no registry in hand: no reading of the route

    def test_a_top_the_person_chose_is_never_lowered(self):
        for label,changes in {
            'pin with effort':{'selection_pins':{'contract_version':1,'frame':{'harness':'claude','model':None,'effort':'max'}}},
            'pin with model':{'selection_pins':{'contract_version':1,'owner':{'harness':'claude','model':'opus','effort':None}}},
            'explicit node profile':{'explicit_profiles':{'frame':'top'}},
        }.items():
            with self.subTest(label):
                self.set_route(**changes)
                self.assertIsNone(self.kind())
                with self.assertRaises(D.DispatchContractError) as caught:self.claim()
                self.assertEqual(caught.exception.reason,'replacement-not-silent-death')
        self.set_route(selection_pins={'contract_version':1,'frame':{'harness':'claude','model':None,'effort':None}},
                       explicit_profiles={'__owner__':'deep'})
        self.assertEqual(self.kind(),R.FRAME_CAPACITY)  # a harness-only pin and an owner profile choose no frame profile
        self.set_route();self.node['model_profile']='deep'
        self.set_route();self.assertIsNone(self.kind())

    def test_the_claim_carries_the_one_transition_and_stays_a_single_logical_family(self):
        record=self.claim()
        self.assertEqual(record['profile_transition'],
                         {'from':'top','to':'deep','reason':'capacity','ordinal':1,'origin':'frame-rule'})
        self.assertNotIn('after_capacity',record['logical_node'])
        self.assertEqual(record['logical_node']['node'],'frame')
        self.assertEqual(record['proof']['death_kind'],R.FRAME_CAPACITY)
        self.assertEqual(self.claim(),record)
        self.assertEqual(len(list((R._directory(self.jobs)/'claims').glob('*.json'))),1)

    def test_an_ordinary_silent_death_claim_has_no_transition(self):
        self.write({**self.meta,'note':'dead-exact-pid','failure_class':'contract'})
        record=self.claim()
        self.assertNotIn('profile_transition',record)
        source=self.rows()['att-source'][1]
        argv=R._replacement_argv(record,source,R.launch_input(self.jobs,'att-source',source))
        self.assertEqual(argv[argv.index('--model-profile')+1] if '--model-profile' in argv else None,None)

    def test_the_replacement_argv_resolves_deep_only_with_the_transition(self):
        record=self.claim();source=self.rows()['att-source'][1]
        replay=R.launch_input(self.jobs,'att-source',source)
        replay['argv']=['--model-profile','top','--route-node','frame']
        argv=R._replacement_argv(record,source,replay)
        self.assertEqual(argv[argv.index('--model-profile')+1],'deep')
        bare={k:v for k,v in record.items() if k!='profile_transition'}
        with self.assertRaises(D.DispatchContractError) as caught:R._replacement_argv(bare,source,replay)
        self.assertEqual(caught.exception.reason,'replacement-profile-transition-invalid')

    def test_admission_accepts_the_verified_deep_launch_and_refuses_every_other_one(self):
        record=self.claim();lines=self.jobs.read_text().splitlines()
        self.assertEqual(R.admission(self.jobs,lines,self.candidate(record)),record)

    def test_a_launch_at_any_profile_but_the_transition_target_is_refused(self):
        record=self.claim();lines=self.jobs.read_text().splitlines()
        self.assertEqual(self.claim(),record)
        for profile in ('top','balanced-deep','light'):
            with self.subTest(profile):
                self.args.attempt_id='att-source'
                (R._directory(self.jobs)/'inputs'/(record['replacement_attempt_id']+'.json')).unlink(missing_ok=True)
                with self.assertRaises(D.DispatchContractError) as caught:
                    R.admission(self.jobs,lines,self.candidate(record,profile))
                self.assertIn(caught.exception.reason,{'replacement-input-tuple-mismatch','replacement-argv-mismatch'})

    def test_a_claim_less_deep_launch_is_refused(self):
        # a silent-death claim on the same frame leg: deep without a transition is a tuple mismatch
        self.write({**self.meta,'note':'dead-exact-pid','failure_class':'contract'})
        record=self.claim();lines=self.jobs.read_text().splitlines()
        self.assertNotIn('profile_transition',record)
        with self.assertRaises(D.DispatchContractError) as caught:
            R.admission(self.jobs,lines,self.candidate(record))
        self.assertEqual(caught.exception.reason,'replacement-input-tuple-mismatch')

    def test_a_claim_that_says_more_or_less_than_the_frame_rule_is_refused(self):
        record=self.claim();path=R._record_path(self.jobs,record['family_id'])
        for label,change in {'other target':{'to':'balanced-deep'},'other reason':{'reason':'auth'},
                             'other origin':{'origin':'user-pin'},'second ordinal':{'ordinal':2}}.items():
            with self.subTest(label):
                forged={**record,'profile_transition':{**record['profile_transition'],**change}}
                with self.assertRaises(D.DispatchContractError) as caught:R._transition_of(forged)
                self.assertEqual(caught.exception.reason,'replacement-profile-transition-invalid')
        stripped={k:v for k,v in record.items() if k!='profile_transition'}
        with self.assertRaises(D.DispatchContractError):R._transition_of(stripped)
        added={**record,'proof':{**record['proof'],'death_kind':'silent'}}
        with self.assertRaises(D.DispatchContractError):R._transition_of(added)

    def test_the_one_reader_verifies_the_claim_against_route_node_and_attempt(self):
        record=self.claim();aid=record['replacement_attempt_id']
        read=lambda **kw:R.read_profile_transition(self.jobs,**{'route':self.route,'node':'frame','attempt_id':aid,**kw})
        self.assertEqual(read(),record['profile_transition'])
        self.assertIsNone(read(attempt_id='att-invented'))
        with self.assertRaises(D.DispatchContractError) as caught:read(node='frame-alternative')
        self.assertEqual(caught.exception.reason,'replacement-profile-transition-mismatch')
        with self.assertRaises(D.DispatchContractError):read(route={**self.route,'route_hash':'sha256:other'})
        # a source that is no longer a proven capacity death stops the claim from counting
        self.write({**self.rows()['att-source'][1],'note':'dead-worker-fail','failure_class':'contract'})
        with self.assertRaises(D.DispatchContractError) as caught:read()
        self.assertEqual(caught.exception.reason,'replacement-not-silent-death')

    def test_the_reader_has_nothing_to_say_about_an_ordinary_replacement(self):
        self.write({**self.meta,'note':'dead-exact-pid','failure_class':'contract'})
        record=self.claim()
        self.assertIsNone(R.read_profile_transition(self.jobs,route=self.route,node='frame',
                                                    attempt_id=record['replacement_attempt_id']))

    def test_spawn_admission_runs_deep_only_with_the_claim_and_a_second_limit_spawns_nothing(self):
        record=self.claim();aid=record['replacement_attempt_id']
        candidate=self.candidate(record)
        row='now\topen\t'+str(self.root)+'\t'+str(self.root)+'\tframe\t'+','.join(k+'='+v for k,v in candidate.items())
        self.assertTrue(D.claim_attempt_row(self.jobs,aid,row,launch=False))
        class Spawned(Exception):pass
        def spawn(_fd):raise Spawned()
        with self.assertRaises(Spawned):   # admission passed; only the fake spawn stopped it
            D.spawn_claimed_attempt(self.jobs,aid,parent_binding=None,spawn=spawn)
        self.assertEqual(self.rows()[aid][1]['model_profile'],'deep')
        self.assertEqual(self.rows()[aid][1]['replacement_claim_digest'],R._digest(record))
        # the replacement stops at a limit too: no third attempt, the existing exhaustion
        self._die(self.rows()[aid][1],note='dead-capacity',failure_class='capacity',launch_claimed='1')
        with self.assertRaises(D.DispatchContractError):R.claim(self.jobs,aid)   # no kind: the replacement is nobody's source
        spawned=[]
        with self.assertRaises(D.DispatchContractError) as caught:
            R.admission(self.jobs,self.jobs.read_text().splitlines(),
                        {**self.candidate(record),'attempt_id':'att-third','automatic_retry_of':aid})
        self.assertEqual(caught.exception.reason,'automatic-replacement-exhausted')
        self.assertEqual(spawned,[])
        self.assertEqual(len(list((R._directory(self.jobs)/'claims').glob('*.json'))),1)

    def test_the_actual_spawn_verifies_the_transition_again(self):
        record=self.claim();aid=record['replacement_attempt_id'];candidate=self.candidate(record)
        row='now\topen\t'+str(self.root)+'\t'+str(self.root)+'\tframe\t'+','.join(k+'='+v for k,v in candidate.items())
        self.assertTrue(D.claim_attempt_row(self.jobs,aid,row,launch=False))   # registered at deep with the claim
        # before the spawn the source stops being a proven capacity death: the registered deep row may not run
        self._die(self.rows()['att-source'][1],note='dead-worker-fail',failure_class='contract')
        spawned=[]
        with self.assertRaises(D.DispatchContractError) as caught:
            D.spawn_claimed_attempt(self.jobs,aid,parent_binding=None,spawn=lambda _fd:spawned.append(aid))
        self.assertEqual(caught.exception.reason,'replacement-not-silent-death')
        self.assertEqual(spawned,[])

    def test_advance_replaces_once_at_deep_and_a_second_capacity_death_needs_attention(self):
        commands=[]
        def run(command,**kw):commands.append(command);return SimpleNamespace(returncode=0)
        with mock.patch.object(R,'_authorized'),mock.patch('dispatch_replacement_batch.command',return_value=None),\
                mock.patch('dispatch_capacity_evidence.harness_hold',return_value=None) as hold,\
                mock.patch.object(R,'_retry_model',return_value='m-deep') as retry:
            result=R.advance(self.jobs,'att-source',run=run)
        self.assertEqual(result['reason'],'replacement-launch-pending')  # the fake launcher registered nothing
        retry.assert_called_once_with(mock.ANY,R.FRAME_CAPACITY)
        self.assertEqual(len(commands),1)
        argv=commands[0]
        self.assertEqual(argv[argv.index('--model-profile')+1],'deep') if '--model-profile' in argv else None
        record=result['record'];aid=record['replacement_attempt_id']
        self._die(self._successor(record,self.meta,'open'),note='dead-capacity',failure_class='capacity')
        _,_,attention=R.advance_batch(self.jobs,{'att-source'},authority_check=lambda *_:True)
        self.assertEqual([a['reason'] for a in attention],['automatic-replacement-exhausted'])
        self.assertEqual(attention[0]['source_attempt_id'],aid)
        self.assertEqual(len(commands),1)   # nothing was launched for the second death

    def test_advance_asks_for_the_hold_of_the_model_it_would_run(self):
        seen=[]
        def hold(jobs,harness,*,model=None,**kw):seen.append((harness,model));return None
        with mock.patch.object(R,'_authorized'),mock.patch('dispatch_replacement_batch.command',return_value=None),\
                mock.patch('dispatch_capacity_evidence.harness_hold',side_effect=hold),\
                mock.patch('model_profile.resolve_runtime_profile',return_value=({'model':'m-deep'},None)):
            R.advance(self.jobs,'att-source',run=lambda *a,**k:SimpleNamespace(returncode=0))
        self.assertEqual(seen,[('claude','m-deep')])

    def test_a_waiting_hold_is_not_a_death_and_writes_nothing(self):
        hold={'until_epoch':4102444800,'label':'2100-01-01T00:00:00Z'}
        with mock.patch.object(R,'_authorized'),mock.patch('dispatch_capacity_evidence.harness_hold',return_value=hold),\
                mock.patch.object(R,'_retry_model',return_value='m-deep'):
            result=R.advance(self.jobs,'att-source',run=lambda *a,**k:self.fail('launched'))
        self.assertEqual(result['reason'],'replacement-capacity-wait')
        self.assertFalse((R._directory(self.jobs)/'claims').exists())

    def test_the_summary_names_node_profiles_cause_and_attempt(self):
        import work_start as W
        self.assertIsNone(W._frame_downgrade_summary(self.route,self.jobs))
        record=self.claim()
        self.assertIsNone(W._frame_downgrade_summary(self.route,self.jobs))   # a claim alone ran nothing
        meta=self._successor(record,self.meta,'done',model_profile='deep',route_node='frame',route_id='rt-test')
        self.assertEqual(W._frame_downgrade_summary(self.route,self.jobs),
                         [{'node':'frame','original_profile':'top','actual_profile':'deep','cause':'capacity',
                           'attempt_id':record['replacement_attempt_id'],'original_attempt_id':'att-source'}])
        # an ordinary silent replacement is not a downgrade
        self.write({**self.meta,'note':'dead-exact-pid','failure_class':'contract'})
        self.assertIsNone(W._frame_downgrade_summary(self.route,self.jobs))



class FrameCapacityFlow:
    """argv -> profile -> register -> spawn claim for one adapter, with the real wrapper parser and
    profile binding, the real transition reader and the real registration/spawn admission. Only the
    process spawn is a stand-in. Each adapter's `dispatch-headless.sd45.test.py` runs this once."""

    def __init__(self, wrapper, harness, case):
        self.wrapper, self.harness, self.case = wrapper, harness, case

    def fixture(self, death):
        fx = FrameTopCapacityTest('test_a_frame_rule_top_capacity_death_is_its_own_kind')
        fx.harness = self.harness
        fx.setUp()
        self.case.addCleanup(fx.doCleanups)
        self.case.addCleanup(fx.tmp.cleanup)
        (R._directory(fx.jobs)/'inputs/att-source.json').unlink()
        args = self.args(fx, self.cli(fx, 'att-source', 'top'))
        fx.meta.update(death, model=args.resolved_model_settings['model'],
                       **D.parse_registry_metadata(R.seal_launch_input(args, self.harness, 'the raw task')))
        fx.write(fx.meta)
        return fx

    def cli(self, fx, attempt, profile, *extra):
        return ['--start', '--attempt-id', attempt, '--jobs', str(fx.jobs), '--worktree', str(fx.root),
                '--prompt-text', 'the raw task', '--slug', 'frame-x', '--capability', 'autopilot-code',
                '--capability-mode', 'dev', '--worker-type', 'frame', '--unit', 'plan/frame',
                '--dispatch-depth', '1', '--registered-worker', '1', '--route-file', str(fx.path),
                '--route-id', fx.route['route_id'], '--route-hash', fx.route['route_hash'],
                '--route-node', 'frame', '--model-role', 'deep maker', '--model-profile', profile, *extra]

    def args(self, fx, argv):
        args = self.wrapper.parser().parse_args(argv)
        args.replacement_input_argv = list(argv)
        args.jobs_path = Path(args.jobs)
        args.worktree = str(Path(args.worktree).resolve())
        args.resolved_model_settings = self.wrapper.resolve_model_settings(args)
        return args

    def register(self, fx, args):
        """The row the wrapper would register for these parsed args (its own sealed input included)."""
        meta = {k: v for k, v in fx.meta.items() if k not in ('note', 'failure_class', 'replacement_input_digest')}
        settings = args.resolved_model_settings
        meta.update(attempt_id=args.attempt_id, automatic_retry_of='att-source',
                    model_profile=settings['profile'], model=settings['model'])
        meta.update(D.parse_registry_metadata(R.seal_launch_input(args, self.harness, 'the raw task')))
        row = 'now\topen\t'+str(fx.root)+'\t'+str(fx.root)+'\tframe\t'+','.join(k+'='+v for k, v in meta.items())
        return D.claim_attempt_row(fx.jobs, args.attempt_id, row, launch=False)

    def spawn(self, fx, attempt):
        spawned = []
        class Spawned(Exception): pass
        def stand_in(_fd):
            spawned.append(attempt); raise Spawned()
        try:
            D.spawn_claimed_attempt(fx.jobs, attempt, parent_binding=None, spawn=stand_in)
        except Spawned:
            pass
        return spawned

    def run(self):
        case = self.case
        fx = self.fixture({'note': 'dead-capacity', 'failure_class': 'capacity'})
        record = fx.claim()
        source = fx.rows()['att-source'][1]
        cmd = R._command(fx.jobs, record, source, R.launch_input(fx.jobs, 'att-source', source))
        case.assertTrue(cmd[1].endswith(f'adapters/{self.harness}/bin/dispatch-headless.py'), cmd[1])
        argv = cmd[2:]
        case.assertEqual(argv[argv.index('--model-profile')+1], 'deep')
        args = self.args(fx, argv)
        case.assertEqual(args.resolved_model_settings['profile'], 'deep')
        import model_profile
        case.assertEqual(model_profile.selection_receipt(args)['profile_selection_source'], 'explicit')
        case.assertTrue(self.register(fx, args))
        row = fx.rows()[args.attempt_id][1]
        case.assertEqual((row['model_profile'], row['replacement_claim_digest']), ('deep', R._digest(record)))
        case.assertEqual(self.spawn(fx, args.attempt_id), [args.attempt_id])   # admitted at spawn, at deep
        # the replacement stops at a limit too: nothing registers, nothing spawns
        fx._die(fx.rows()[args.attempt_id][1], note='dead-capacity', failure_class='capacity', launch_claimed='1')
        _, _, attention = R.advance_batch(fx.jobs, {'att-source'}, authority_check=lambda *_: True)
        case.assertEqual([a['reason'] for a in attention], ['automatic-replacement-exhausted'])
        third = self.args(fx, self.cli(fx, 'att-third', 'deep', '--automatic-retry-of', args.attempt_id))
        with case.assertRaises(D.DispatchContractError) as refused:
            self.register(fx, third)
        case.assertEqual(refused.exception.reason, 'automatic-replacement-exhausted')
        case.assertNotIn('att-third', fx.rows())
        with case.assertRaises(D.DispatchContractError) as nothing:   # no row to claim, so no spawn either
            self.spawn(fx, 'att-third')
        case.assertEqual(nothing.exception.reason, 'attempt-row-not-unique')
        case.assertEqual(len(list((R._directory(fx.jobs)/'claims').glob('*.json'))), 1)

    def run_claimless(self):
        """`deep` with no claim: refused by the profile binding, and again by registration."""
        case = self.case
        fx = self.fixture({'note': 'dead-exact-pid', 'failure_class': 'contract'})   # a silent death: no transition
        record = fx.claim()
        case.assertNotIn('profile_transition', record)
        forged = self.cli(fx, record['replacement_attempt_id'], 'deep', '--automatic-retry-of', 'att-source')
        args = self.args(fx, forged)
        import model_profile
        with case.assertRaises(model_profile.ModelProfileError) as refused:
            model_profile.selection_receipt(args)
        case.assertEqual(refused.exception.reason, 'profile-selection-mismatch')
        with case.assertRaises(D.DispatchContractError) as refused:   # even past the wrapper, the registry says no
            self.register(fx, args)
        case.assertEqual(refused.exception.reason, 'replacement-input-tuple-mismatch')
        case.assertNotIn(record['replacement_attempt_id'], fx.rows())



class NamespaceExtinctDeathProofTest(unittest.TestCase):
    """5th Codex run: a closed row whose recorded namespaces left the host."""

    def row(self, note):
        meta = {'attempt_schema_version': '2', 'dispatch_depth': '2', 'transport': 'headless',
                'execution_surface': 'registered-headless', 'registered_worker': '1',
                'fallback_hop': 'same-harness-headless', 'worker_type': 'stage',
                'attempt_id': 'att-namespace-dead', 'route_id': 'rt-test', 'route_node': 'plan',
                'pid': '464', 'pid_start': '537327887', 'pgid': '464',
                'pid_scope': 'namespace-local', 'pid_observer_ns': 'pid:[4026534323]',
                'pid_ns': 'pid:[4026534323]', 'launch_lifecycle': 'foreground-scoped',
                'note': note, 'failure_class': 'runtime'}
        return ['now', 'done', '/repo', '/repo', 'plan'], meta

    def namespace(self, gone):
        def probe(_metadata, *, host_complete=False):
            if host_complete:
                return D.ProcessGroupObservation('empty')
            return D.ProcessGroupObservation('unverifiable', (), 'observer-namespace-mismatch')
        stack = __import__('contextlib').ExitStack()
        stack.enter_context(mock.patch.object(D, 'namespace_gone', return_value=gone))
        stack.enter_context(mock.patch.object(D, 'attempt_tagged_descendants', side_effect=probe))
        return stack

    def test_dead_namespace_absent_is_a_silent_death_with_an_exact_proof(self):
        fields, meta = self.row('dead-namespace-absent')
        self.assertEqual(R.death_kind(fields, meta), 'silent')
        with self.namespace('extinct'):
            proof = R.death_proof(fields, meta)
        self.assertEqual((proof['state'], proof['reason'], proof['death_kind']),
                         ('quiescent', D.NAMESPACE_EXTINCT_REASON, 'silent'))

    def test_without_proven_extinction_the_receipt_gate_still_holds(self):
        fields, meta = self.row('dead-namespace-absent')
        for gone in ('present', 'unverifiable'):
            with self.subTest(gone=gone), self.namespace(gone), \
                    self.assertRaises(D.DispatchContractError):
                R.death_proof(fields, meta)

    def test_an_interrupted_foreground_worker_is_not_replaced_by_the_runtime(self):
        # The caller stopped it; its next explicit start retries (B2).
        fields, meta = self.row('dead-interrupted')
        self.assertIsNone(R.death_kind(fields, meta))
        with self.namespace('extinct'), self.assertRaises(D.DispatchContractError) as refused:
            R.death_proof(fields, meta)
        self.assertEqual(refused.exception.reason, 'replacement-not-silent-death')


if __name__=='__main__':unittest.main()
