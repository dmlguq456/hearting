#!/usr/bin/env python3
"""Wait/watch observation shares the verified attachment identity without inferring idle."""
from pathlib import Path
import contextlib, importlib.util, io, json, unittest
from types import SimpleNamespace
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('observed_fixture',ROOT/'utilities/peer_shell_resolution.test.py')
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)


class NativeObservation(f.WrappedPaneResolutionTest):
 def publish_only(self,harness):
  for kind in f.session_shell.HARNESSES:
   f.session_shell.binding_path(kind,self.host['pid']).unlink(missing_ok=True)
  return self.publish(harness)
 def test_wait_observes_a_real_unknown_attachment_on_each_harness(self):
  for harness in sorted(f.session_shell.HARNESSES):
   with self.subTest(harness=harness):
    self.publish_only(harness)
    args=f.peer.build_parser().parse_args(['wait',self.pane,'--until','unknown','--timeout','1000'])
    with mock.patch.object(f.peer,'_resolve_target',return_value=(None,None,None)), \
         mock.patch.object(f.peer,'_run_herdr_wait',return_value={'error':{'code':'agent_not_found'}}), \
         mock.patch.object(f.peer,'_mark_observed') as mark,contextlib.redirect_stdout(io.StringIO()) as output:
     code=f.peer.cmd_wait(args)
    self.assertEqual(code,0)
    self.assertIn('state=unknown',output.getvalue())
    self.assertIn(self.sid,output.getvalue())
    self.assertEqual(mark.call_args.args[:2],(harness,self.sid))

 def test_watch_arms_the_same_native_identity_without_a_real_observer_process(self):
  for harness in sorted(f.session_shell.HARNESSES):
   with self.subTest(harness=harness):
    self.publish_only(harness)
    args=f.peer.build_parser().parse_args(['watch',self.pane,'--until','unknown','--timeout','1000','--wake','none'])
    proc=SimpleNamespace(pid=self.children[0].pid)
    with mock.patch.object(f.peer,'_herdr_missing',return_value=False), \
         mock.patch.object(f.peer,'_steward_seat',return_value=None), \
         mock.patch.object(f.peer,'_run_herdr_get',return_value={'error':{'code':'agent_not_found'}}), \
         mock.patch.object(f.peer,'_retire_pane_info',side_effect=lambda pane,**kw:self.info), \
         mock.patch.object(f.peer,'_retire_foreground',return_value=None), \
         mock.patch.object(f.peer.subprocess,'Popen',return_value=proc), \
         mock.patch.object(f.peer,'end_with_test',side_effect=lambda child:child), \
         mock.patch.object(f.peer,'_mark_observed') as mark,contextlib.redirect_stdout(io.StringIO()) as output:
     code=f.peer.cmd_watch(args)
    self.assertEqual(code,0,output.getvalue())
    self.assertIn('state=armed',output.getvalue())
    self.assertEqual(mark.call_args.args[:2],(harness,self.sid))
    # Private arms from the previous synthetic harness are complete fixtures,
    # not a live observer to retire or reuse for the next harness.
    for path in f.peer._watch_root().glob('*'):
     if path.is_file():path.unlink()

 def test_wait_does_not_grant_a_conflicting_or_reused_attachment(self):
  self.publish('codex')
  args=f.peer.build_parser().parse_args(['wait',self.pane,'--until','unknown','--timeout','1000'])
  def identity(pid,kind,**kw):
   return SimpleNamespace(confidence='proven' if pid==self.native['pid'] else 'unknown',
    session_id='01000000-0000-7000-8000-000000000002' if pid==self.native['pid'] else '')
  for condition in ('inner-conflict','foreign-owner'):
   with self.subTest(condition=condition), \
        mock.patch.object(f.peer,'_resolve_target',return_value=(None,None,None)), \
        mock.patch.object(f.peer,'_run_herdr_wait',return_value={'error':{'code':'agent_not_found'}}), \
        mock.patch.object(f.peer,'_mark_observed') as mark,contextlib.redirect_stdout(io.StringIO()):
    if condition=='inner-conflict':
     with mock.patch('fleet.process_identity.process_identity',side_effect=identity):code=f.peer.cmd_wait(args)
    else:
     with mock.patch.object(f.peer,'_retire_pane_info',return_value={**self.info,'foreground_process_group_id':self.native['pid']}):code=f.peer.cmd_wait(args)
    self.assertNotEqual(code,0)
    mark.assert_not_called()

 def test_unknown_transport_is_not_replacement_but_known_identity_conflicts_stay_terminal(self):
  expected={'pane':self.pane,'harness':'codex','session_id':self.sid}
  pane={'pane_id':self.pane,'agent':'unknown'}
  view=SimpleNamespace(endpoint='default',complete=True,terminal={},panes=[pane])
  self.assertIsNone(f.peer._watch_terminal_from_view(view,expected,'default'))
  pane['agent_session']={'value':'01000000-0000-7000-8000-000000000002'}
  self.assertEqual(f.peer._watch_terminal_from_view(view,expected,'default'),'session-replaced')
  pane.pop('agent_session');pane['agent']='claude'
  self.assertEqual(f.peer._watch_terminal_from_view(view,expected,'default'),'session-replaced')
  view.panes=[]
  self.assertEqual(f.peer._watch_terminal_from_view(view,expected,'default'),'pane-absent')

 def test_known_transport_metadata_cannot_mask_an_inner_proven_conflict(self):
  self.publish('codex')
  payload={'result':{'agent':{'agent':'codex','pane_id':self.pane,
   'agent_status':'idle','agent_session':{'value':self.sid}}}}
  args=f.peer.build_parser().parse_args(['wait',self.pane,'--until','idle','--timeout','1000'])
  def identity(pid,kind,**kw):
   return SimpleNamespace(confidence='proven' if pid==self.native['pid'] else 'unknown',
    session_id='01000000-0000-7000-8000-000000000002' if pid==self.native['pid'] else '')
  with mock.patch.object(f.peer,'_resolve_target',return_value=('codex',self.sid,None)), \
       mock.patch.object(f.peer,'_run_herdr_wait',return_value=payload), \
       mock.patch('fleet.process_identity.process_identity',side_effect=identity), \
       mock.patch.object(f.peer,'_mark_observed') as mark,contextlib.redirect_stdout(io.StringIO()):
   self.assertNotEqual(f.peer.cmd_wait(args),0)
  mark.assert_not_called()

 def test_unwrapped_sidless_observation_does_not_add_a_process_info_query(self):
  payload={'result':{'agent':{'agent':'codex','pane_id':self.pane,'agent_status':'unknown'}}}
  args=f.peer.build_parser().parse_args(['wait',self.pane,'--until','unknown','--timeout','1000'])
  with mock.patch.object(f.peer,'_resolve_target',return_value=('codex',None,None)), \
       mock.patch.object(f.peer,'_run_herdr_wait',return_value=payload), \
       mock.patch.object(f.peer,'_retire_pane_info') as probe, \
       mock.patch.object(f.peer,'_mark_observed'),contextlib.redirect_stdout(io.StringIO()):
   self.assertEqual(f.peer.cmd_wait(args),0)
  probe.assert_not_called()

 def test_attachment_identity_does_not_assign_sidless_idle_to_a_native_session(self):
  self.publish('codex')
  for sid in (None,self.sid):
   with self.subTest(sid=sid):
    agent={'agent':'codex','pane_id':self.pane,'agent_status':'idle'}
    if sid:agent['agent_session']={'value':sid}
    args=f.peer.build_parser().parse_args(['wait',self.pane,'--until','unknown','--until','idle','--timeout','1000'])
    with mock.patch.object(f.peer,'_resolve_target',return_value=('codex',sid,None)), \
         mock.patch.object(f.peer,'_run_herdr_wait',return_value={'result':{'agent':agent}}), \
         mock.patch.object(f.peer,'_mark_observed'),contextlib.redirect_stdout(io.StringIO()) as output:
     self.assertEqual(f.peer.cmd_wait(args),0)
    self.assertIn('state='+('idle' if sid else 'unknown'),output.getvalue())
    self.assertIn(self.sid,output.getvalue())


if __name__=='__main__':
 names=[n for n in NativeObservation.__dict__ if n.startswith('test_')]
 result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(NativeObservation(n) for n in names))
 raise SystemExit(not result.wasSuccessful())
