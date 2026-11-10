"""Claude native rows and the block share the same status formatter."""
from pathlib import Path
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class WrappedDisplayTest(unittest.TestCase):
    def statusline(self,wrapped,percentage,panel=None,record=None):
        with tempfile.TemporaryDirectory(prefix='shell-native-line-') as directory:
            root=Path(directory);home=root/'claude';home.mkdir()
            if panel is not None:(root/'claude-panel.json').write_text(json.dumps(panel))
            env=dict(os.environ,HOME=str(root),CLAUDE_CONFIG_DIR=str(home),AGENT_HOME=str(root/'no-code'),
                     FLEET_TITLE_DISABLE='1',HARNESS_CAPACITY_REFRESH_DISABLE='1')
            env.pop('HEARTING_SHELL_SOCKET',None)
            if wrapped:env['HEARTING_SHELL_SOCKET']=str(root/'private.sock')
            payload={'session_id':'synthetic-native-session','cwd':str(root),'model':{'id':'claude-opus','display_name':'Opus'},
                     'context_window':{'used_percentage':percentage},'rate_limits':{'seven_day':{'used_percentage':42}}}
            result=subprocess.run(['bash',str(ROOT/'adapters/claude/statusline.sh')],input=json.dumps(payload),
                                  env=env,text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            if record is not None and (root/'claude-status.json').exists():
                record.update(json.loads((root/'claude-status.json').read_text()))
            return re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',result.stdout).strip()

    def test_wrapped_claude_paints_the_common_native_row_even_with_unknown_context(self):
        for percentage,reading in ((37,'37%'),(0,'0%'),(None,None)):
            with self.subTest(percentage=percentage):
                record={}
                line=self.statusline(True,percentage,record=record)
                if reading:self.assertIn('context ',line);self.assertIn(reading,line)
                # Before Claude's first response it reports no context; like the
                # native statusline, the row leaves the reading out, not '?'.
                else:self.assertNotIn('context',line)
                # Every field stays here whenever the panel is not painting it.
                self.assertIn('Opus',line);self.assertIn('7d 42%',line)
                self.assertIn('[?]',line)  # The shared formatter, not legacy fallback.
                # Exactly the painted plain text, for the shell's exact-match blanking.
                self.assertEqual(record['session_id'],'synthetic-native-session')
                self.assertEqual('\n'.join(record['lines']),line)

    def test_the_native_row_stays_complete_even_beside_a_stale_panel_report(self):
        # A panel report left by an earlier host (painted=true) must not shrink
        # the callback: if the panel is gone, every value is already here.
        full=self.statusline(True,37)
        for field in ('37%','Opus','7d 42%','◑'):self.assertIn(field,full)
        record={}
        line=self.statusline(True,37,panel={'session_id':'synthetic-native-session','painted':True},record=record)
        # Each call has its own temporary folder; only its name may differ.
        folder=lambda text:re.sub(r'shell-native-line-[^ ]+','shell-native-line-*',text)
        self.assertEqual(folder(line),folder(full))
        self.assertEqual('\n'.join(record['lines']),line)


    def test_first_claude_paint_reuses_metadata_without_waiting_for_refresh(self):
        import sys
        from unittest import mock
        sys.path.insert(0,str(ROOT/'tools'))
        sys.path.insert(0,str(ROOT/'utilities'))
        from fleet.session_line import claude_first_statusline
        import claude_process_identity
        with tempfile.TemporaryDirectory(prefix='shell-first-cache-') as directory:
            root=Path(directory);tap=root/'.statusline';tap.mkdir()
            old={'session_id':'exact','cwd':directory,'hearting_display':{
                'S_SESSION_DISPLAY':'Last observed title','git_repo':'0','git_observed_label':'12:00'}}
            (tap/'exact.json').write_text(json.dumps(old))
            env=dict(os.environ,CLAUDE_CONFIG_DIR=directory,HEARTING_SHELL_DIRECTORY=directory,COLUMNS='180')
            callback={'session_id':'exact','cwd':directory,'context_window':{'used_percentage':37},'model':{'display_name':'Native'}}
            with mock.patch.object(claude_process_identity,'statusline_bindings',return_value=(None,None,'unobserved')),mock.patch('subprocess.Popen') as worker:
                line=claude_first_statusline(callback,env,str(ROOT/'adapters/claude/statusline.sh'))
                self.assertIn('Last observed title',line);self.assertIn('37%',line)
                refreshes=lambda:[c for c in worker.call_args_list if c.args[0]==['bash',str(ROOT/'adapters/claude/statusline.sh')]]
                self.assertEqual(len(refreshes()),1)
                self.assertEqual(refreshes()[0].kwargs['env']['HEARTING_SHELL_INITIAL_REFRESH'],'1')
                self.assertIsNone(claude_first_statusline(callback,env,'unused'))
                self.assertEqual(len(refreshes()),1)

    def test_first_claude_paint_of_a_new_session_reads_the_shared_git_observation(self):
        import sys,time
        from unittest import mock
        sys.path.insert(0,str(ROOT/'tools'))
        sys.path.insert(0,str(ROOT/'utilities'))
        from fleet import gitinfo
        from fleet.session_line import claude_first_statusline
        import claude_process_identity
        with tempfile.TemporaryDirectory(prefix='shell-first-git-') as directory:
            env=dict(os.environ,CLAUDE_CONFIG_DIR=directory,HEARTING_SHELL_DIRECTORY=directory,COLUMNS='180')
            fields=dict.fromkeys(gitinfo._DISPLAY_FIELDS)
            fields.update(repo=1,branch='feature-x',operation='none',extra_worktrees=0,dirty=0,merged=0,branch_done=0)
            observed={'git':fields,'observed_at':time.time(),'completed_at':time.time(),'jobs':''}
            # Before the first response Claude reports no context or quota.
            callback={'session_id':'fresh','cwd':directory,'workspace':{'project_dir':directory},'model':{'display_name':'Native'}}
            with mock.patch.object(claude_process_identity,'statusline_bindings',return_value=(None,None,'unobserved')),\
                 mock.patch.object(gitinfo,'cached_display',return_value=observed) as cached,mock.patch('subprocess.Popen'):
                line=claude_first_statusline(callback,env,str(ROOT/'adapters/claude/statusline.sh'))
            self.assertEqual(cached.call_args.kwargs,{'request':False,'project_dir':directory})
            plain=re.sub(r'\x1b\[[0-9;]*m','',line)
            self.assertIn('feature-x',plain);self.assertIn('fresh',plain);self.assertNotIn('⎇ ?',plain)
            self.assertIn('Native',plain)
            # Labels only: the random folder name may itself contain '5h' or '7d'.
            self.assertNotRegex(plain,r'(^| )(context|5h|7d) ')

    def test_an_older_initial_refresh_cannot_replace_new_native_telemetry(self):
        import sys
        sys.path.insert(0,str(ROOT/'tools'))
        from fleet.native_display import write_claude_tap
        with tempfile.TemporaryDirectory(prefix='shell-tap-order-') as directory:
            root=Path(directory);(root/'claude-first-display.json').write_text('{}')
            path=root/'exact.json';env={'HEARTING_SHELL_SOCKET':str(root/'control.sock')}
            write_claude_tap(path,json.dumps({'context':80}),{**env,'HEARTING_STATUS_OBSERVED_AT':'200'})
            write_claude_tap(path,json.dumps({'context':37}),{**env,'HEARTING_STATUS_OBSERVED_AT':'100'})
            self.assertEqual(json.loads(path.read_text())['context'],80)

    def test_unwrapped_claude_keeps_its_existing_model_and_usage_display(self):
        line=self.statusline(False,37)
        self.assertIn('Opus',line);self.assertIn('37%',line);self.assertIn('42%',line)

    def test_codex_wrapper_keeps_native_shared_ingress_and_original_arguments(self):
        spec=importlib.util.spec_from_file_location('shell_display_entry',ROOT/'utilities/session-shell/entry.py')
        entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
        original=['/synthetic/codex','--model','fixture','resume','native-session']
        transform=getattr(entry,'wrapped_command',lambda _harness,command:command)
        command=transform('codex',original)
        self.assertEqual(command,original)
        self.assertEqual(original,['/synthetic/codex','--model','fixture','resume','native-session'])
        for harness in ('claude','opencode'):
            self.assertEqual(transform(harness,original),original)



if __name__=='__main__':unittest.main()
