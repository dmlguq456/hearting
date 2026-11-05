#!/usr/bin/env python3
"""Ordinary non-Git omitted-shape starts remain inline through normal finish."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'utilities'), str(ROOT/'tools')]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT/path)
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
R = load('nongit_default_route', 'utilities/capability-route.py')
F = load('nongit_default_finish_fixture', 'utilities/inline_finish.test.py')

class StopBeforeCompile(Exception): pass

class NonGitDefaultTest(unittest.TestCase):
    def test_one_shot_stdin_is_shared_by_default_slug_selection_and_compilation(self):
        task = 'Research the local documentation and write a short report.\n'
        probe = '''import importlib.util,json,sys
spec=importlib.util.spec_from_file_location('stdin_compose_probe',sys.argv[1])
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
def capture(**kwargs):
    print(json.dumps({'shape':kwargs['shape'],'text':kwargs['work_request']['text']}))
    raise SystemExit(0)
module.compose_route=capture
sys.argv=sys.argv[1:]
module.main()
'''
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); cwd = root/'project'; cwd.mkdir()
            result = subprocess.run([sys.executable,'-c',probe,str(ROOT/'utilities/capability-route.py'),
                'compose','--start','--cwd',str(cwd),'--artifact-root',str(root/'artifacts'),
                '--campaign-key','stdin-fixture','--capability','autopilot-research',
                '--prompt-file','/dev/stdin','--spec-read','fixture','--drift-verdict','within-spec',
                '--artifact-guard','fixture'],input=task,text=True,capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),{'shape':'direct','text':task})
            self.assertFalse((cwd/'.git').exists())

    def selection(self, *, shape=None, task=None, git=False):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); cwd = root/'project'; cwd.mkdir()
            (cwd/'notes.txt').write_text('Existing notes\n')
            if git:
                subprocess.run(['git','init','-q',str(cwd)], check=True)
            prompt = root/'task.md'
            prompt.write_text(task or 'Research the local documentation and write a short report.')
            observed = {}
            def stop(**kwargs):
                observed.update(kwargs)
                raise StopBeforeCompile()
            argv = ['capability-route.py','compose','--start','--cwd',str(cwd),
                    '--artifact-root',str(root/'artifacts'),'--capability','autopilot-research',
                    '--capability-mode','academic','--prompt-file',str(prompt),
                    '--spec-read','fixture','--drift-verdict','within-spec','--artifact-guard','fixture']
            if shape: argv.extend(['--shape',shape])
            with mock.patch.dict(os.environ, {'AGENT_DISPATCH_DEPTH':'0'}), \
                 mock.patch.object(sys, 'argv', argv), mock.patch.object(R, 'compose_route', side_effect=stop):
                with self.assertRaises(StopBeforeCompile): R.main()
            self.assertFalse((cwd/'.git').exists()) if not git else None
            self.assertEqual((cwd/'notes.txt').read_text(), 'Existing notes\n')
            return observed

    def test_default_selection_is_direct_with_explicit_capability_hint(self):
        selected = self.selection()
        self.assertEqual(selected['shape'], 'direct')
        self.assertEqual(selected['capability'], 'autopilot-research')
        self.assertEqual(selected['capability_mode'], 'academic')

    def test_explicit_shapes_named_new_repository_and_git_default_are_preserved(self):
        self.assertEqual(self.selection(shape='framed')['shape'], 'framed')
        self.assertEqual(self.selection(shape='solo')['shape'], 'solo')
        task = 'Create a new local git repository at ./named-project\nImplement the requested tool.'
        self.assertEqual(self.selection(task=task)['shape'], 'framed')
        self.assertEqual(self.selection(shape='direct', task=task)['shape'], 'direct')
        self.assertEqual(self.selection(git=True)['shape'], 'framed')

    def test_automatic_direct_has_one_line_card_and_finishes_without_git_or_worktree(self):
        # Fail before any real CLI start if the old compiler would select a worker.
        self.assertEqual(self.selection()['shape'], 'direct')
        fixture = F.PublicInlineFinishTest()
        fixture.non_git = True
        fixture.CAPABILITY, fixture.CAPABILITY_MODE = 'autopilot-research', 'academic'
        original = subprocess.run
        def omit_shape(argv, *args, **kwargs):
            if (isinstance(argv, list) and len(argv) > 2 and argv[2] == 'compose'
                    and str(argv[1]).endswith('/utilities/capability-route.py')):
                argv = list(argv)
                for flag in ('--shape','--intensity'):
                    index = argv.index(flag); del argv[index:index+2]
            return original(argv, *args, **kwargs)
        with mock.patch.object(subprocess,'run',side_effect=omit_shape):
            fixture.setUp()
        self.addCleanup(fixture.tearDown)
        route = fixture.route
        self.assertEqual(route['selection']['shape'], 'direct')
        self.assertEqual(route['capability'], 'autopilot-research')
        self.assertFalse(route.get('human_gates'))
        self.assertFalse((fixture.repo/'.git').exists())
        self.assertFalse(list(fixture.base.glob('*-wt')))
        self.assertEqual(route['cwd'], str(fixture.repo))
        card = F.CAP.compose_card(route, non_git_decision={'kind':'direct'})
        self.assertEqual(len(card.splitlines()), 1, card)
        finished = subprocess.run(fixture.command, cwd=fixture.repo, env=fixture.env,
                                  capture_output=True, text=True)
        self.assertEqual(finished.returncode, 0, finished.stderr+finished.stdout)
        self.assertIsNone(json.loads(finished.stdout)['commit'])
        outcome = json.loads(F.CAP.outcome_path(fixture.route_file).read_text())
        self.assertTrue(outcome['terminal_gate_proven'])
        self.assertFalse((fixture.repo/'.git').exists())
        self.assertEqual(F.artifact_producer.read_cycle_record(fixture.root,fixture.cycle['cycle_id'])['state'],'sealed')

if __name__=='__main__': unittest.main()
