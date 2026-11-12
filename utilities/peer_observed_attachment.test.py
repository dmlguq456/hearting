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
 def native_wait(self,harness,sid,state,pane=None,until=('unknown',)):
  """Run `wait` on the native path: the resolver and the native terminal read are patched, no Herdr."""
  agent={'agent':harness,'pane_id':self.pane,'agent_status':state}
  if sid:agent['agent_session']={'value':sid}
  args=f.peer.build_parser().parse_args(['wait',self.pane,*[x for u in until for x in ('--until',u)],'--timeout','1000'])
  identity={'harness':harness,'session_id':sid,'pane':self.pane}
  with mock.patch('peer_address.resolve',return_value=(identity,None)), \
       mock.patch.object(f.peer.terminal_surface,'wait_native',return_value={'result':{'agent':agent}}), \
       mock.patch.object(f.peer,'_mark_observed') as mark,contextlib.redirect_stdout(io.StringIO()) as output:
   code=f.peer.cmd_wait(args)
  return code,output.getvalue(),mark

 def test_wait_prints_a_real_unknown_attachment_on_each_harness_without_marking_it(self):
  for harness in sorted(f.session_shell.HARNESSES):
   with self.subTest(harness=harness):
    self.publish_only(harness)
    code,output,mark=self.native_wait(harness,self.sid,'unknown')
    self.assertEqual(code,0)
    self.assertIn('state=unknown',output)
    self.assertIn(self.sid,output)
    # Unknown is not an observed target: nothing is recorded as seen.
    mark.assert_not_called()

 def test_watch_arms_the_same_native_identity_without_a_real_observer_process(self):
  for harness in sorted(f.session_shell.HARNESSES):
   with self.subTest(harness=harness):
    self.publish_only(harness)
    args=f.peer.build_parser().parse_args(['watch',self.pane,'--until','unknown','--timeout','1000','--wake','none'])
    proc=SimpleNamespace(pid=self.children[0].pid)
    identity={'harness':harness,'session_id':self.sid,'pane':self.pane}
    native={'result':{'agent':{'agent':harness,'pane_id':self.pane,'agent_status':'unknown',
        'agent_session':{'value':self.sid},'pid':self.native['pid'],'pid_start':str(self.native.get('start') or self.native.get('pid_start') or ''),
        'pid_namespace':''}}}
    with mock.patch('peer_address.resolve',return_value=(identity,None)), \
         mock.patch.object(f.peer,'native_agent',return_value=native), \
         mock.patch.object(f.peer,'_steward_seat',return_value=None), \
         mock.patch.object(f.peer,'_herdr_missing',side_effect=AssertionError('watch must not ask Herdr')), \
         mock.patch.object(f.peer,'_retire_pane_info',side_effect=AssertionError('watch must not read pane info')), \
         mock.patch.object(f.peer.subprocess,'Popen',return_value=proc), \
         mock.patch.object(f.peer,'end_with_test',side_effect=lambda child:child), \
         mock.patch.object(f.peer,'_mark_observed') as mark,contextlib.redirect_stdout(io.StringIO()) as output:
     code=f.peer.cmd_watch(args)
    self.assertEqual(code,0,output.getvalue())
    self.assertIn('state=armed',output.getvalue())
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
  with mock.patch.object(f.peer,'_retire_pane_info') as probe:
   code,output,mark=self.native_wait('codex',None,'unknown')
  # A native record with no session id is not a target at all; no pane-info query is added.
  self.assertNotEqual(code,0)
  probe.assert_not_called()
  mark.assert_not_called()

 def test_attachment_identity_does_not_assign_sidless_idle_to_a_native_session(self):
  self.publish('codex')
  for sid in (None,self.sid):
   with self.subTest(sid=sid):
    code,output,mark=self.native_wait('codex',sid,'idle',until=('unknown','idle'))
    if sid:
     self.assertEqual(code,0)
     self.assertIn('state=idle',output)
     self.assertIn(self.sid,output)
    else:
     # No native session id: the wait never reports idle for it.
     self.assertNotEqual(code,0)
     self.assertNotIn('state=idle',output)


if __name__=='__main__':
 names=[n for n in NativeObservation.__dict__ if n.startswith('test_')]
 result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(NativeObservation(n) for n in names))
 raise SystemExit(not result.wasSuccessful())
