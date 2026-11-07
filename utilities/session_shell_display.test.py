"""Wrapped displays yield duplicate decorations and keep native fallback fields."""
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
    def statusline(self,wrapped,percentage):
        with tempfile.TemporaryDirectory(prefix='shell-native-line-') as directory:
            root=Path(directory);home=root/'claude';home.mkdir()
            env=dict(os.environ,HOME=str(root),CLAUDE_CONFIG_DIR=str(home),AGENT_HOME=str(root/'no-code'),
                     FLEET_TITLE_DISABLE='1',HARNESS_CAPACITY_REFRESH_DISABLE='1')
            env.pop('HEARTING_SHELL_SOCKET',None)
            if wrapped:env['HEARTING_SHELL_SOCKET']=str(root/'private.sock')
            payload={'session_id':'synthetic-native-session','cwd':str(root),'model':{'id':'claude-opus','display_name':'Opus'},
                     'context_window':{'used_percentage':percentage},'rate_limits':{'seven_day':{'used_percentage':42}}}
            result=subprocess.run(['bash',str(ROOT/'adapters/claude/statusline.sh')],input=json.dumps(payload),
                                  env=env,text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            return result.stdout if wrapped else re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',result.stdout).strip()

    def test_wrapped_claude_emits_no_native_fallback_even_for_unknown_context(self):
        self.assertEqual(self.statusline(True,37),'')
        self.assertEqual(self.statusline(True,0),'')
        self.assertEqual(self.statusline(True,None),'')

    def test_unwrapped_claude_keeps_its_existing_model_and_usage_display(self):
        line=self.statusline(False,37)
        self.assertIn('Opus',line);self.assertIn('37%',line);self.assertIn('42%',line)

    def test_codex_footer_changes_are_per_invocation_and_keep_original_arguments(self):
        spec=importlib.util.spec_from_file_location('shell_display_entry',ROOT/'utilities/session-shell/entry.py')
        entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
        original=['/synthetic/codex','--model','fixture','resume','native-session']
        transform=getattr(entry,'wrapped_command',lambda _harness,command:command)
        command=transform('codex',original)
        self.assertIn('tui.status_line=["thread-id","context-used"]',command)
        self.assertEqual(command[-len(original[1:]):],original[1:])
        self.assertEqual(original,['/synthetic/codex','--model','fixture','resume','native-session'])
        for harness in ('claude','opencode'):
            self.assertEqual(transform(harness,original),original)


if __name__=='__main__':unittest.main()
