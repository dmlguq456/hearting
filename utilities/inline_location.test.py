#!/usr/bin/env python3
"""Inline execution and finish agree across a repository and its route worktree."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'utilities'))
spec=importlib.util.spec_from_file_location('inline_location_fixture',ROOT/'utilities/inline_finish.test.py')
F=importlib.util.module_from_spec(spec);sys.modules[spec.name]=F;spec.loader.exec_module(F)

class InlineLocationTest(unittest.TestCase):
    def fixture(self):
        fixture=F.PublicInlineFinishTest()
        real=subprocess.run
        def prepare(argv,*args,**kwargs):
            if isinstance(argv,list) and len(argv)>2 and argv[2]=='compose' and str(argv[1]).endswith('/utilities/capability-route.py'):
                self.worktree=fixture.base/'execution-worktree'
                real(['git','-C',str(fixture.repo),'worktree','add','--quiet','--detach',str(self.worktree),'HEAD'],check=True)
                argv=list(argv);argv[argv.index('--cwd')+1]=str(self.worktree)
            return real(argv,*args,**kwargs)
        with mock.patch.object(subprocess,'run',side_effect=prepare): fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.assertEqual(fixture.route['cwd'],str(self.worktree))
        self.source=fixture.route['source_commit']
        return fixture

    def commit_result(self,fixture):
        (fixture.repo/'README.md').write_text('The requested source result.\n')
        subprocess.run(['git','-C',str(fixture.repo),'add','README.md'],check=True)
        subprocess.run(['git','-C',str(fixture.repo),'commit','-qm','Implement the requested result'],check=True)
        return subprocess.check_output(['git','-C',str(fixture.repo),'rev-parse','HEAD'],text=True).strip()

    def finish(self,fixture,*,route_id=False,commit=None,cwd=None):
        command=list(fixture.command)
        if route_id:command[command.index('--route')+1]=fixture.route['route_id']
        if commit:command+=['--commit',commit]
        env=fixture.env.copy();env.pop('AGENT_ARTIFACT_ROOT',None)
        done=subprocess.run(command,cwd=cwd or fixture.repo,env=env,text=True,capture_output=True)
        self.assertEqual(done.returncode,0,done.stderr+done.stdout)
        return json.loads(done.stdout)

    def test_execute_inline_receipt_names_actual_worktree(self):
        fixture=self.fixture()
        import work_start
        receipt=work_start.start_work(fixture.route,fixture.route_file,fixture.jobs)
        self.assertEqual(receipt.get('cwd'),str(self.worktree))
        self.assertIn(str(self.worktree),receipt.get('next_step',''))
        import start_receipt
        self.assertIn(str(self.worktree),start_receipt.next_line(receipt))

    def test_route_id_finish_finds_the_parent_artifact_from_the_repository(self):
        fixture=self.fixture()
        receipt=self.finish(fixture,route_id=True)
        self.assertEqual(receipt['route_id'],fixture.route['route_id'])
        self.assertTrue(F.CAP.outcome_path(fixture.route_file).exists())

    def test_changed_caller_commit_replaces_the_empty_worktree_default(self):
        fixture=self.fixture();result=self.commit_result(fixture)
        receipt=self.finish(fixture)
        self.assertEqual(receipt['commit'],result)
        self.assertEqual(receipt['commit_observation']['selected_from'],'caller-working-directory')
        self.assertTrue(receipt['commit_observation']['warnings'])
        self.assertEqual(json.loads(F.CAP.outcome_path(fixture.route_file).read_text())['head_commit'],result)
        self.assertEqual(subprocess.check_output(['git','-C',str(self.worktree),'rev-parse','HEAD'],text=True).strip(),self.source)

    def test_explicit_starting_commit_is_kept_with_an_honest_warning(self):
        fixture=self.fixture();self.commit_result(fixture)
        receipt=self.finish(fixture,commit=self.source)
        self.assertEqual(receipt['commit'],self.source)
        self.assertIn('commit_observation',receipt)
        self.assertIn('unchanged-starting-tree',receipt['commit_observation']['warnings'])

    def test_an_existing_execution_result_is_not_replaced_by_another_caller_head(self):
        fixture=self.fixture();caller=self.commit_result(fixture)
        subprocess.run(['git','-C',str(self.worktree),'checkout','-qb','execution-result'],check=True)
        (self.worktree/'README.md').write_text('The result in the execution checkout.\n')
        subprocess.run(['git','-C',str(self.worktree),'add','README.md'],check=True)
        subprocess.run(['git','-C',str(self.worktree),'commit','-qm','Execution result'],check=True)
        expected=subprocess.check_output(['git','-C',str(self.worktree),'rev-parse','HEAD'],text=True).strip()
        receipt=self.finish(fixture)
        self.assertEqual(receipt['commit'],expected)
        self.assertEqual(receipt['commit_observation']['selected_from'],'execution-checkout')
        self.assertNotEqual(expected,caller)
        self.assertEqual(receipt['commit_observation']['warnings'],[])

    def test_a_different_repository_cannot_supply_the_result_commit(self):
        fixture=self.fixture();foreign=fixture.base/'other-project';foreign.mkdir()
        subprocess.run(['git','init','-q',str(foreign)],check=True)
        (foreign/'other.txt').write_text('Unrelated work.\n')
        subprocess.run(['git','-C',str(foreign),'add','other.txt'],check=True)
        subprocess.run(['git','-C',str(foreign),'-c','user.name=Fixture','-c','user.email=fixture@example.com',
                        'commit','-qm','Other repository'],check=True)
        receipt=self.finish(fixture,cwd=foreign)
        self.assertEqual(receipt['commit'],self.source)
        self.assertEqual(receipt['commit_observation']['selected_from'],'execution-checkout')

    def test_unavailable_common_directory_does_not_prove_a_shared_repository(self):
        fixture=self.fixture();self.commit_result(fixture)
        import inline_finish
        real=subprocess.run
        def unavailable(argv,*args,**kwargs):
            if '--git-common-dir' in argv:
                return subprocess.CompletedProcess(argv,128,'','unavailable')
            return real(argv,*args,**kwargs)
        with mock.patch.object(inline_finish.os,'getcwd',return_value=str(fixture.repo)), \
             mock.patch.object(inline_finish.subprocess,'run',side_effect=unavailable):
            commit,_cwd,observation=inline_finish.select_result_commit(fixture.route,None,F.CAP)
        self.assertEqual(commit,self.source)
        self.assertEqual(observation['selected_from'],'execution-checkout')

if __name__=='__main__':unittest.main()
