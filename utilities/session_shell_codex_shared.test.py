"""A shared-daemon callback must select its proved TUI, never inherited sockets."""
from pathlib import Path
from unittest import mock
import importlib.util
import os
import subprocess
import sys
import unittest
import tempfile
import socket
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'utilities'),str(ROOT/'tools')]
import session_shell as shell
import codex_tui_config
import pane_ownership
import session_tidy
from fleet.collectors import codex,procscan
spec=importlib.util.spec_from_file_location('shared_shell_entry',ROOT/'utilities/session-shell/entry.py')
entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)

class SharedCodexTest(unittest.TestCase):
 def setUp(self):
  self.child=subprocess.Popen([sys.executable,'-c','import sys;sys.stdin.buffer.read()'],stdin=subprocess.PIPE)
  self.addCleanup(self.close_child)
  self.native=shell.process(self.child.pid)
  self.owner=(self.child.pid,0,0,0,0,self.native['start'])
  self.sid='00000000-0000-4000-8000-000000000052'
  self.env={'HEARTING_SHELL_SOCKET':'/old/client.sock','HEARTING_SHELL_TOKEN':'old-client'}
  self.current={'HEARTING_SHELL_SOCKET':'/actual/tui.sock','HEARTING_SHELL_TOKEN':'actual-tui'}
 def close_child(self):
  self.child.stdin.close();self.child.wait(timeout=5)
 def test_wrapper_does_not_force_embedded_mode_or_rewrite_explicit_native_options(self):
  for argv in (['codex'],['codex','--no-daemon'],['codex','-c','model="user-choice"'],['codex','resume',self.sid],['codex','--profile','user-profile']):
   self.assertEqual(entry.wrapped_command('codex',argv),argv)
 def test_service_callback_uses_the_unique_current_tui_environment_and_birth(self):
  with mock.patch.object(pane_ownership,'_caller_runtime',return_value=(self.owner,True)), mock.patch.object(pane_ownership,'_registered_worker',return_value=False), mock.patch.object(codex,'process_for_thread',return_value=(self.child.pid,self.native['start'])), mock.patch.object(codex,'session_id_of_process',return_value=self.sid), mock.patch.object(procscan,'read_environ',return_value=self.current), mock.patch.object(session_tidy,'is_worker',return_value=False):
   self.assertEqual(shell._lifecycle_owner('codex',self.sid,self.env),(self.native,self.current))
   with mock.patch.object(codex,'process_for_thread',return_value=None):
    self.assertIsNone(shell._lifecycle_owner('codex',self.sid,self.env))
   with mock.patch.object(codex,'session_id_of_process',return_value='another-thread'):
    self.assertIsNone(shell._lifecycle_owner('codex',self.sid,self.env))
   with mock.patch.object(pane_ownership,'_registered_worker',return_value=True):
    self.assertIsNone(shell._lifecycle_owner('codex',self.sid,self.env))
   with mock.patch.object(session_tidy,'is_worker',return_value=True):
    self.assertIsNone(shell._lifecycle_owner('codex',self.sid,self.env))
   with mock.patch.object(codex,'process_for_thread',return_value=(self.child.pid,'old-birth')):
    self.assertIsNone(shell._lifecycle_owner('codex',self.sid,self.env))
   with mock.patch.object(shell,'process',side_effect=[self.native,{**self.native,'start':'changed-during-observation'}]):
    self.assertIsNone(shell._lifecycle_owner('codex',self.sid,self.env))
   self.assertIsNone(shell._lifecycle_owner('claude',self.sid,self.env))
 def test_service_routing_runs_even_without_a_socket_in_the_daemon_environment(self):
  with mock.patch.object(shell,'_lifecycle_owner',return_value=(self.native,self.current),create=True) as lookup, mock.patch.object(shell.socket,'socket') as channel:
   channel.return_value.__enter__.return_value.recv.return_value=b'{}\n'
   shell.lifecycle('codex','start',self.sid,env={})
   lookup.assert_called_once_with('codex',self.sid,{})
   channel.return_value.__enter__.return_value.connect.assert_called_once_with('/actual/tui.sock')
 def test_an_unavailable_daemon_observation_is_silent_and_never_opens_a_transport(self):
  with mock.patch.object(shell,'_lifecycle_owner',side_effect=OSError('unavailable')),mock.patch.object(shell.socket,'socket') as channel:
   shell.lifecycle('codex','start',self.sid,env=self.env)
   channel.assert_not_called()

 def test_callback_rpc_connection_must_belong_to_the_actual_service_and_birth(self):
  with tempfile.TemporaryDirectory(prefix='codex-peer-',dir='/tmp') as tmp:
   endpoint=str(Path(tmp)/'native.sock')
   code="import socket,sys,select;server=socket.socket(socket.AF_UNIX);server.bind(sys.argv[1]);server.listen();print('READY',flush=True)\nwhile True:\n r,_,_=select.select([server,sys.stdin],[],[])\n if sys.stdin in r:break\n c,_=server.accept();c.close()\n"
   server=subprocess.Popen([sys.executable,'-c',code,endpoint],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
   try:
    self.assertEqual(server.stdout.readline().strip(),'READY')
    birth=shell.process(server.pid)['start']
    with socket.socket(socket.AF_UNIX) as client:
     client.connect(endpoint);view=SimpleNamespace(socket=client)
     self.assertTrue(codex._daemon_client_matches_service(view,(server.pid,birth)))
     self.assertFalse(codex._daemon_client_matches_service(view,(self.child.pid,self.native['start'])))
     self.assertFalse(codex._daemon_client_matches_service(view,(server.pid,'old-birth')))
   finally:
    server.stdin.close();server.wait(timeout=5);server.stdout.close()
 def test_default_footer_plan_matches_the_seven_fields_painted_in_the_block(self):
  from fleet.session_line import status_lines
  with tempfile.TemporaryDirectory(prefix='codex-footer-parity-') as tmp:
   path=Path(tmp)/'config.toml';path.write_text(codex_tui_config.merge_status_line_text(''));path.chmod(0o600)
   values={'harness':'codex','native_sid':self.sid,'title':'Fixture title','cwd':'/project','branch':'fixture*','ctx_pct':42,'model':'fixture','effort':'high','quota_windows':[('5h',23,None),('7d',60,None)]}
   with mock.patch.object(procscan,'read_environ',return_value={'CODEX_HOME':tmp}),mock.patch.object(procscan,'_read_argv',return_value=['codex']):
    for width in (410,274,100):
     rows=status_lines(values,width);plan,_=shell._codex_footer_plan(values,self.sid,self.child.pid,rows,rows)
     plain='\n'.join(plan['lines'])
     for value in (self.sid,'Fixture title','fixture*','42%','fixture high','5h 23%','7d 60%'):
      self.assertIn(value,plain)
     padded,_=shell._codex_footer_plan(values,self.sid,self.child.pid,rows,rows,padding='  ')
     self.assertEqual(padded['lines'],['  '+line for line in plan['lines']])
     self.assertEqual(plan['config']['items'],list(codex_tui_config.DEFAULT_STATUS_LINE))
     self.assertEqual(len(plan['fields']),7)
     for index,value in enumerate((self.sid,'Fixture title','fixture','Context 42% used','fixture high','5h 77% left','Weekly 40% left')):
      self.assertIn(value,plan['fields'][index])
     # Unpainted common rows leave only the shell-owned thread-id field concealable.
     partial=shell._codex_footer_plan(values,self.sid,self.child.pid,rows,rows[:-1])[0]
     self.assertFalse(partial['common']);self.assertEqual(partial['fields'],[[self.sid]]+[None]*6)
    long={**values,'title':'A'*100};rows=status_lines(long,100)
    plan,_=shell._codex_footer_plan(long,self.sid,self.child.pid,rows,rows)
    self.assertNotIn(long['title'],plan['fields'][1])
    long={**values,'branch':'long-branch-'*15};rows=status_lines(long,100)
    self.assertEqual(shell._codex_footer_plan(long,self.sid,self.child.pid,rows,rows)[0]['fields'][1:],[None]*6)

 def test_only_a_thread_id_first_config_has_a_managed_footer_observation(self):
  with tempfile.TemporaryDirectory(prefix='codex-footer-config-') as tmp:
   path=Path(tmp)/'config.toml';path.write_text(codex_tui_config.merge_status_line_text(''));path.chmod(0o600)
   self.assertEqual(codex_tui_config.managed_status_line_observation(tmp)['items'],list(codex_tui_config.DEFAULT_STATUS_LINE))
   for argv in (['--profile','custom'],['-c','tui.status_line=[]']):
    self.assertIsNone(codex_tui_config.managed_status_line_observation(tmp,argv=argv))
   # Any order with thread-id first is observed; the shell owns only that field.
   path.write_text('[tui]\nstatus_line=["thread-id","context-used"]\n')
   self.assertEqual(codex_tui_config.managed_status_line_observation(tmp)['items'],['thread-id','context-used'])
   path.write_text('[tui]\nstatus_line=["thread-id"]\n')
   self.assertEqual(codex_tui_config.managed_status_line_observation(tmp)['items'],['thread-id'])
   for layout in ('["context-used","thread-id"]','["thread-id","thread-id"]','[]'):
    path.write_text('[tui]\nstatus_line='+layout+'\n')
    self.assertIsNone(codex_tui_config.managed_status_line_observation(tmp))
   path.write_text(codex_tui_config.merge_status_line_text(''));path.chmod(0o666)
   self.assertIsNone(codex_tui_config.managed_status_line_observation(tmp))
   path.chmod(0o600);other=path.with_name('other.toml');path.rename(other);path.symlink_to(other)
   self.assertIsNone(codex_tui_config.managed_status_line_observation(tmp))
if __name__=='__main__':unittest.main()

