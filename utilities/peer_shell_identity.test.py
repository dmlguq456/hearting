#!/usr/bin/env python3
"""Independent attachment inner-SID conflict probe, at discovery and carrier admission."""
from pathlib import Path
import importlib.util, sys, contextlib, io, unittest, json
from unittest import mock
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'utilities'),str(ROOT/'tools')]
spec=importlib.util.spec_from_file_location('conflict_fixture',ROOT/'utilities/peer_shell_resolution.test.py')
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
MODE='head'
class InnerConflict(f.WrappedPaneResolutionTest):
 def run_conflict(self,harness,effect_change):
  self.publish(harness);other='01000000-0000-7000-8000-000000000002';native_pid=self.native['pid'];reads=[];current=[self.sid if effect_change else other]
  def identity(pid,kind,**kw):
   reads.append(pid);return SimpleNamespace(confidence='proven' if pid==native_pid else 'unknown',session_id=current[0] if pid==native_pid else '')
  def prepared(ref):
   if effect_change:current[0]=other
  args=SimpleNamespace(target=self.pane,body_file=None,body_stdin=False,text='private exact inner SID conflict '+harness+str(effect_change),ref=[],no_trailer=False)
  with self.observed(harness),mock.patch('fleet.process_identity.process_identity',side_effect=identity),mock.patch.object(f.peer.peer_message,'deliver_pending_codex',return_value={'status':'queued'}) as queue,mock.patch.object(f.session_shell,'deliver_pending',return_value={'ok':True}) as shell,contextlib.redirect_stdout(io.StringIO()) as out:
   f.peer.cmd_prompt(args,on_prepared=prepared)
  calls=queue.call_args_list+shell.call_args_list;recipient=None
  if calls:recipient=f.peer.peer_message._read_pending(calls[0].args[0])['to']
  print(json.dumps({'source':MODE,'harness':harness,'phase':'effect' if effect_change else 'discovery','expected_proved_inner_sid':current[0],'attachment_sid':self.sid,'inner_pid_read':native_pid in reads,'carrier_calls':len(calls),'recipient':recipient,'result':out.getvalue().strip()}))
  self.assertEqual(len(calls),0,'A proved current inner SID conflict must withhold the carrier')
 def test_proved_inner_sid_conflict_at_discovery_does_not_authorize_old_attachment(self):
  for h in sorted(f.session_shell.HARNESSES):
   with self.subTest(harness=h):self.run_conflict(h,False)
 def test_inner_sid_changes_before_effect_without_process_rebirth_stay_unsent(self):
  for h in sorted(f.session_shell.HARNESSES):
   with self.subTest(harness=h):self.run_conflict(h,True)
 def test_native_address_metadata_cannot_hide_a_proven_inner_conflict(self):
  for harness in sorted(f.session_shell.HARNESSES):
   with self.subTest(harness=harness):
    self.publish(harness);native_pid=self.native['pid']
    def identity(pid,kind,**kw):
     return SimpleNamespace(confidence='proven' if pid==native_pid else 'unknown',
      session_id='01000000-0000-7000-8000-000000000002' if pid==native_pid else '')
    with self.observed(harness),mock.patch('fleet.process_identity.process_identity',side_effect=identity):
     candidate,error=f.peer._prompt_current_candidate(self.pane,expected_harness=harness,
      expected_sid=self.sid,require_process_session=True,native_address=(harness,self.sid,self.pane))
    self.assertIsNone(candidate)
    self.assertEqual(error,'target-identity-changed')
 def test_matching_proven_and_unknown_inner_evidence_keep_the_existing_policy(self):
  for harness in sorted(f.session_shell.HARNESSES):
   for confidence in ('proven','unknown'):
    with self.subTest(harness=harness,confidence=confidence):
     self.publish(harness);native_pid=self.native['pid']
     def identity(pid,kind,**kw):
      return SimpleNamespace(confidence=confidence if pid==native_pid else 'unknown',
       session_id=self.sid if confidence=='proven' else 'unproven-other-session')
     args=SimpleNamespace(target=self.pane,body_file=None,body_stdin=False,
      text='synthetic unchanged policy '+harness+confidence,ref=[],no_trailer=False)
     with self.observed(harness),mock.patch('fleet.process_identity.process_identity',side_effect=identity),mock.patch.object(f.peer.peer_message,'deliver_pending_codex',return_value={'status':'queued'}) as queue,mock.patch.object(f.session_shell,'deliver_pending',return_value={'ok':True}) as shell,contextlib.redirect_stdout(io.StringIO()):
      f.peer.cmd_prompt(args)
     calls=queue.call_args_list+shell.call_args_list
     self.assertEqual(len(calls),1)
     self.assertEqual(f.peer.peer_message._read_pending(calls[0].args[0])['to']['session_id'],self.sid)

if __name__=='__main__':
 names=[n for n in InnerConflict.__dict__ if n.startswith('test_')]
 result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(InnerConflict(n) for n in names))
 print(json.dumps({'methods':result.testsRun,'failures':len(result.failures),'errors':len(result.errors)}))
 raise SystemExit(not result.wasSuccessful())
