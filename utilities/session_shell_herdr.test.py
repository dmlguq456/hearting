"""Use Herdr's wrapper hint only at an eligible native-wrapper exec boundary."""
import importlib.util,unittest,subprocess,sys,io,contextlib
from types import SimpleNamespace
from unittest import mock
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sp=importlib.util.spec_from_file_location('shell_herdr_entry',ROOT/'utilities/session-shell/entry.py')
entry=importlib.util.module_from_spec(sp);sp.loader.exec_module(entry)
class HerdrHintTest(unittest.TestCase):
 def environment(self):
  return {'HERDR_ENV':'1','HERDR_PANE_ID':'private:p1','HERDR_BIN_PATH':'/private/herdr',
          'HERDR_SOCKET_PATH':'/private/server.sock','UNCHANGED':'value'}
 def test_each_native_kind_gets_its_own_initial_environment_without_mutating_the_caller(self):
  for kind in ('claude','codex','opencode'):
   original=self.environment();before=dict(original)
   result=entry.herdr_environment(kind,original)
   self.assertEqual(result,{**original,'HERDR_AGENT':kind})
   self.assertEqual(original,before)
 def test_outside_partial_and_explicit_herdr_contexts_keep_their_existing_ingress(self):
  self.assertIsNone(entry.herdr_environment('codex',{}))
  for field in ('HERDR_ENV','HERDR_PANE_ID','HERDR_BIN_PATH','HERDR_SOCKET_PATH'):
   env=self.environment();env.pop(field)
   self.assertIsNone(entry.herdr_environment('codex',env))
  self.assertIsNone(entry.herdr_environment('codex',{**self.environment(),'HERDR_AGENT':'operator-choice'}))
 def test_native_title_signals_survive_without_allowing_control_injection(self):
  script=r"""const a=require('node:assert/strict'),d=require(process.argv[1]);
a.equal(d.titleSequence('⠏ Working'), '\x1b]2;⠏ Working\x07');
a.equal(d.titleSequence(''), '\x1b]2;\x07');
a.equal(d.titleSequence('safe\x1b]52;bad\x07\nname'), '\x1b]2;safe]52;badname\x07');
a.ok(d.titleSequence('🙂'.repeat(2000)).length<5000);
"""
  p=subprocess.run(['node','-e',script,str(ROOT/'utilities/session-shell/native-display.cjs')],text=True,capture_output=True)
  self.assertEqual(p.returncode,0,p.stderr)
class PaneAttachmentTest(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  sp=importlib.util.spec_from_file_location('shell_herdr_peer',ROOT/'utilities/peer-steward.py')
  cls.peer=importlib.util.module_from_spec(sp);sp.loader.exec_module(cls.peer)
 def setUp(self):
  import session_shell
  self.shell=session_shell
  self.children=[]
  for _ in range(2):
   child=subprocess.Popen([sys.executable,'-c','import sys;sys.stdin.buffer.read()'],stdin=subprocess.PIPE)
   self.children.append(child)
  self.addCleanup(self.close_children)
  pid=self.children[0].pid
  self.binding={'harness':'claude','sid':'owned-native-session','broker_pid':pid,
   'broker_start':self.peer._proc_start_ticks(pid),'incarnation':'owned-attachment','socket':'/private/attachment'}
  self.info={'foreground_process_group_id':pid,'foreground_processes':[{'pid':pid}]}
 def close_children(self):
  for child in self.children:child.stdin.close();child.wait(timeout=5)
 def test_only_the_exact_foreground_owner_can_join_an_existing_native_binding(self):
  with mock.patch.object(self.shell,'exact_binding',return_value=(self.binding,'')), mock.patch.object(self.peer,'_retire_pane_info',return_value=self.info):
   self.assertEqual(self.peer._pane_shell_binding_result('private:p1','claude','owned-native-session')[0],self.binding)
   other=self.children[1].pid
   with mock.patch.object(self.peer,'_retire_pane_info',return_value={'foreground_process_group_id':other,'foreground_processes':[{'pid':other}]}):
    self.assertIsNone(self.peer._pane_shell_binding_result('private:p1','claude','owned-native-session')[0])
   with mock.patch.object(self.peer,'_proc_start_ticks',return_value='different-birth'):
    self.assertIsNone(self.peer._pane_shell_binding_result('private:p1','claude','owned-native-session')[0])
   self.assertIsNone(self.peer._pane_shell_binding_result('private:p1','claude','owned-native-session',expected={**self.binding,'incarnation':'old-attachment'})[0])
 def test_pane_owner_change_at_submission_never_writes_or_queues_native_input(self):
  # The attachment observed at resolution is rechecked at submission; when it no
  # longer owns the session nothing is written to the host or queued natively.
  args=SimpleNamespace(target='private:p1',ref=[],no_trailer=False)
  for harness in ('claude','codex','opencode'):
   binding={**self.binding,'harness':harness}
   with mock.patch.object(self.peer.peer_message,'prepare_peer_message',return_value=('sealed fixture','fixture-ref')), mock.patch.object(self.peer.peer_message,'_read_pending',return_value={'state':'pending'}), mock.patch.object(self.shell,'exact_binding',return_value=(None,'attachment-not-found')) as check, mock.patch.object(self.shell,'deliver_pending') as shell_send, mock.patch.object(self.peer.peer_message,'deliver_pending_codex') as queue_send, mock.patch.object(self.peer,'_schedule_message_obligation'), mock.patch.object(self.peer,'_record'), mock.patch.object(self.peer,'_prompt_carrier_note'), contextlib.redirect_stdout(io.StringIO()) as output:
    self.peer._cmd_prompt_native(args,'fixture',harness,'owned-native-session',binding=binding,from_identity=(None,'runtime',''),from_name='fixture')
    check.assert_called_once_with(harness,'owned-native-session')
    shell_send.assert_not_called();queue_send.assert_not_called()
    self.assertIn('target-process-changed',output.getvalue())
if __name__=='__main__':unittest.main()

