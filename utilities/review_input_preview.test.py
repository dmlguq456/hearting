#!/usr/bin/env python3
"""SD154/161 merged dry-run proof reaches each actual wrapper main."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib, importlib.util, io, json, os, sys, unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'utilities'))
spec=importlib.util.spec_from_file_location('preview_fallback_fixture',ROOT/'utilities/stage_dispatch_fallback.test.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
import review_input
from split_module_fixture import patch_split

class WrapperPreviewTest(unittest.TestCase):
 def setUp(self):
  self.f=fixture.FallbackTest('runTest');self.f.setUp();self.addCleanup(self.f.tearDown)
 def test_replacement_preview_keeps_same_round_and_strict_normal_gate(self):
  from types import SimpleNamespace
  args=SimpleNamespace(action='dry-run',route_file='route.json',automatic_retry_of='att-source')
  with mock.patch.object(review_input,'_dispatch_node_module',side_effect=AssertionError('new round')):
   self.assertIsNone(review_input.preview_request_admission(args,self.f.jobs))
 def test_declared_phase_preview_is_not_a_full_stage_round_and_the_stage_stays_exhausted(self):
  # BC rt-96bab699: the dry-run of a stage_authority=0 test phase was refused
  # `reviewed-evidence-revision-not-admitted detail=exhausted`, although its
  # launch (dispatch-node.py) never spends a full-stage round.
  from types import SimpleNamespace
  f=self.f
  with f.dispatch_env():
   path=f.route(same_status='supported',intensity='standard');route=json.loads(path.read_text())
   f.seed_review_rounds(route,'test',2)
   full=dict(action='dry-run',route_file=str(path),route_id=route['route_id'],route_hash=route['route_hash'],
             route_node='test',attempt_id=None,dispatch_depth=2)
   with self.assertRaises(review_input.DC.DispatchContractError) as refused:
    review_input.preview_request_admission(SimpleNamespace(**full),f.jobs)
   self.assertEqual((refused.exception.reason,refused.exception.detail),('reviewed-evidence-revision-not-admitted','exhausted'))
   phase=dict(full,subsession_id='ss-gap-1',subsession_index=1,subsession_count=2,subsession_mode='serial',
              session_chain_id='ssc-gap-1',phase_brief='brief.md',narrow_verify='true',expected_round_trips=1,
              stage_authority=0,attempt_id='att-gap-1')
   with mock.patch.object(review_input,'_dispatch_node_module',side_effect=AssertionError('full-stage round')):
    self.assertIsNone(review_input.preview_request_admission(SimpleNamespace(**phase),f.jobs))
   # One raw flag is not a declaration: it is refused, never admitted.
   for raw,reason in ((dict(full,stage_authority=0),'stage-authority-zero-without-subsession'),
                      (dict(full,subsession_id='ss-gap-1',stage_authority=0),'subsession-arguments-incomplete'),
                      (dict(phase,stage_authority=1),'subsession-stage-authority-forbidden'),
                      (dict(phase,dispatch_depth=1),'subsession-route-binding-invalid')):
    with self.subTest(reason=reason),self.assertRaises(review_input.DC.DispatchContractError) as caught:
     review_input.preview_request_admission(SimpleNamespace(**raw),f.jobs)
    self.assertEqual(caught.exception.reason,reason)
 def _changed_plan_preview(self, *, prior_quiescent):
  f=self.f
  with f.dispatch_env():
   path=f.route(same_status='supported',intensity='standard');route=json.loads(path.read_text())
   f.seed_parent();evidence=f.seed_plan_marker(route);f.seed_review_rounds(route,'plan-check',1)
   evidence.write_text('Corrected input, preview only.\n')
   marker=fixture.R.completion_dir(route['route_id'],jobs=f.jobs)/'plan.json'
   before={str(p):p.read_bytes() for p in marker.parent.iterdir() if p.is_file()}
   before_jobs=f.jobs.read_bytes()
   for harness in ('codex','claude','opencode'):
    with self.subTest(harness=harness),contextlib.ExitStack() as stack:
     spec=importlib.util.spec_from_file_location('preview_main_'+harness,ROOT/f'adapters/{harness}/bin/dispatch-headless.py')
     w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
     stack.enter_context(mock.patch.object(w,'resolve_artifact_root',return_value=str(f.art)))
     # The route tuple is constructed by the fixture compiler; native runtime
     # discovery is outside this no-model test. The real main completion gate,
     # common input resolver and readonly admission are deliberately unmocked.
     # The old closed review row has no process identity. Supply only this
     # unit's shared process observation, preserving its original payload.
     original_observer=review_input.DC.attempt_process_quiescence
     def observe(meta, **kwargs):
      if meta.get('attempt_id')=='att-plan-check-round-1':
       return review_input.DC.ProcessQuiescence(
        'quiescent' if prior_quiescent else 'unverifiable',
        'fixture-group-empty' if prior_quiescent else 'process-identity-missing')
      return original_observer(meta, **kwargs)
     stack.enter_context(patch_split(review_input.DC,'attempt_process_quiescence',side_effect=observe))
     stack.enter_context(mock.patch.object(w,'validate_route_record',side_effect=lambda args:setattr(args,'route_validation',{}) or 0))
     stack.enter_context(mock.patch.object(w,'headless_attempt_policy',return_value={
      'fallback_hop':'same-harness-headless','fallback_ordinal':1,'quick':False,
      'terminal_attempt_limit':None,'replacement_attempt_limit':0,'replacement_notes':frozenset()}))
     stack.enter_context(mock.patch.object(w.shutil,'which',return_value='/bin/true'))
     stack.enter_context(mock.patch.object(w,'shell_command',return_value='true'))
     for name in ('check_runtime_projection','ensure_runtime_home_projection'):
      if hasattr(w,name): stack.enter_context(mock.patch.object(w,name,return_value=0 if name=='check_runtime_projection' else None))
     spawn=stack.enter_context(mock.patch.object(w,'spawn_claimed_attempt',side_effect=AssertionError('model spawned')))
     argv=['dispatch-headless.py','--dry-run','--worktree',str(f.repo),'--jobs',str(f.jobs),
      '--slug','preview-'+harness,'--parent','owner','--parent-attempt-id','att-fallback-parent',
      '--capability','autopilot-code','--capability-mode','dev','--worker-type','review',
      '--worker-mode','qa/plan-review','--unit','qa/plan-review','--dispatch-depth','1','--intensity','standard',
      '--route-file',str(path),'--route-id',route['route_id'],'--route-hash',route['route_hash'],'--route-node','plan-check',
      '--model-profile',next(n for n in route['nodes'] if n['id']=='plan-check')['model_profile'],
      '--model-role',next(n for n in route['nodes'] if n['id']=='plan-check')['role'],
      '--parent-harness','claude','--parent-transport','headless','--parent-sandbox','default']
     with contextlib.redirect_stdout(io.StringIO()) as out:
      code=w.main(argv)
     self.assertEqual(code,0 if prior_quiescent else 78,out.getvalue());spawn.assert_not_called()
     if not prior_quiescent:
      self.assertIn('prior-attempt-unverifiable',out.getvalue())
      self.assertIn('child_spawned=0',out.getvalue())
     self.assertEqual(before_jobs,f.jobs.read_bytes())
     self.assertEqual(before,{str(p):p.read_bytes() for p in marker.parent.iterdir() if p.is_file()})
   self.assertEqual(fixture.R.gate_currency(route,next(n for n in route['nodes'] if n['id']=='plan'),marker,
                                            observe=True,jobs=f.jobs).state,'revised-unrecorded')

 def test_all_three_mains_prove_changed_plan_without_publishing(self):
  self._changed_plan_preview(prior_quiescent=True)
 def test_all_three_mains_preserve_unknown_prior_row_without_publishing(self):
  self._changed_plan_preview(prior_quiescent=False)

if __name__=='__main__':unittest.main()
