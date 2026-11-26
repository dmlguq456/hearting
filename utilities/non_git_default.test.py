#!/usr/bin/env python3
"""Task meaning and sequence survive non-Git placement; small inline edits still finish."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
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
            self.assertEqual(json.loads(result.stdout),{'shape':'framed','text':task})
            self.assertFalse((cwd/'.git').exists())

    def selection(self, *, shape=None, task=None, git=False, capability="autopilot-research", mode="academic", graph=None, profile=None, parent=None):
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
                    '--artifact-root',str(root/'artifacts'),'--prompt-file',str(prompt),
                    '--spec-read','fixture','--drift-verdict','within-spec','--artifact-guard','fixture']
            if capability: argv.extend(['--capability', capability])
            if mode: argv.extend(['--capability-mode', mode])
            if graph: argv.extend(['--graph', graph])
            if profile: argv.extend(['--profile', profile])
            if parent: argv.extend(['--parent-harness', parent])
            if shape: argv.extend(['--shape',shape])
            with mock.patch.dict(os.environ, {'AGENT_DISPATCH_DEPTH':'0'}), \
                 mock.patch.object(sys, 'argv', argv), mock.patch.object(R, 'compose_route', side_effect=stop):
                with self.assertRaises(StopBeforeCompile): R.main()
            self.assertFalse((cwd/'.git').exists()) if not git else None
            self.assertEqual((cwd/'notes.txt').read_text(), 'Existing notes\n')
            return observed

    def test_complex_document_uses_the_same_frame_entry_in_every_folder_and_harness(self):
        task = ("Prepare a joint results report from existing source files and three writers. "
                "Inspect materials, agree the outline and assignments, collect drafts, "
                "integrate the report and review it. Track missing contributions.")
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                selected = self.selection(task=task, capability=None, mode=None, parent=harness)
                self.assertEqual(selected["shape"], "framed")
                self.assertIsNone(selected["capability"])
                self.assertEqual(selected["work_request"]["text"], task)
                git_selected = self.selection(task=task, capability=None, mode=None, parent=harness, git=True)
                self.assertEqual(selected["shape"], git_selected["shape"])

    def test_non_git_frame_keeps_doc_capability_and_declared_stage_hints(self):
        graph = "material-strategy,draft-production,quality-review,finalize"
        selected = self.selection(capability="autopilot-draft", mode="doc", graph=graph, profile="deep")
        self.assertEqual(selected["shape"], "framed")
        self.assertEqual((selected["capability"], selected["capability_mode"]), ("autopilot-draft", "doc"))
        self.assertEqual(selected["graph"], graph)
        self.assertEqual(selected["profile"], "deep")

    def test_explicit_non_git_direct_preserves_the_existing_profile_input(self):
        selected = self.selection(shape="direct", capability="autopilot-refine", mode="default", profile="deep")
        self.assertEqual(selected["shape"], "direct")
        self.assertEqual(selected["profile"], "deep")

    def test_default_selection_uses_frame_with_the_explicit_capability_hint(self):
        selected = self.selection()
        self.assertEqual(selected['shape'], 'framed')
        self.assertEqual(selected['capability'], 'autopilot-research')
        self.assertEqual(selected['capability_mode'], 'academic')

    def test_explicit_shapes_named_new_repository_and_git_default_are_preserved(self):
        self.assertEqual(self.selection(shape='framed')['shape'], 'framed')
        self.assertEqual(self.selection(shape='solo')['shape'], 'solo')
        task = 'Create a new local git repository at ./named-project\nImplement the requested tool.'
        self.assertEqual(self.selection(task=task)['shape'], 'framed')
        self.assertEqual(self.selection(shape='direct', task=task)['shape'], 'direct')
        self.assertEqual(self.selection(git=True)['shape'], 'framed')

    def test_new_document_is_produced_and_finished_at_its_project_path(self):
        fixture = F.PublicInlineFinishTest()
        fixture.non_git = True
        fixture.CAPABILITY, fixture.CAPABILITY_MODE = "autopilot-draft", "doc"
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.assertIn("target-artifact", fixture.route["nodes"][0]["write_scope"])
        target = fixture.repo / "report.md"
        target.write_text("A first draft written directly in the project.\n")
        command = list(fixture.command)
        command[command.index("--evidence") + 1] = str(target)
        finished = subprocess.run(command, cwd=fixture.repo, env=fixture.env, capture_output=True, text=True)
        self.assertEqual(finished.returncode, 0, finished.stderr + finished.stdout)
        receipt = json.loads(finished.stdout)
        self.assertIsNone(receipt["commit"])
        self.assertEqual(target.read_text(), "A first draft written directly in the project.\n")
        self.assertEqual(F.artifact_producer.read_cycle_record(fixture.root, fixture.cycle["cycle_id"])["state"], "sealed")
        self.assertFalse((fixture.repo / ".git").exists())
        self.assertFalse(list(fixture.base.glob("*-wt")))

    def test_small_document_correction_finishes_without_git_or_worktree(self):
        task = "Correct only the known typo in the existing document."
        self.assertEqual(self.selection(shape="direct", capability="autopilot-refine", mode="default",
                                        task=task)["shape"], "direct")
        fixture = F.PublicInlineFinishTest()
        fixture.non_git = True
        fixture.CAPABILITY, fixture.CAPABILITY_MODE = "autopilot-refine", "default"
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        route = fixture.route
        self.assertEqual(route['selection']['shape'], 'direct')
        self.assertEqual(route['capability'], 'autopilot-refine')
        self.assertFalse(route.get('human_gate_bindings'))
        self.assertFalse((fixture.repo/'.git').exists())
        self.assertFalse(list(fixture.base.glob('*-wt')))
        self.assertEqual(route['cwd'], str(fixture.repo))
        import work_start
        inline = work_start.start_work(route, fixture.route_file, fixture.jobs)
        self.assertEqual(inline['required_action'], 'execute-inline')
        self.assertNotIn('Commit source changes', inline['next_step'])
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
