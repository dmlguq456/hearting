#!/usr/bin/env python3
"""Keep execution edits and require repository identity for finish fallback."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'utilities'))
spec=importlib.util.spec_from_file_location('result_location_fixture',ROOT/'utilities/inline_location.test.py')
I=importlib.util.module_from_spec(spec);sys.modules[spec.name]=I;spec.loader.exec_module(I)
import inline_finish as M

class ResultSelection(I.InlineLocationTest):
    def dirty(self,staged):
        f=self.fixture();caller=self.commit_result(f)
        (self.worktree/'README.md').write_text('Uncommitted execution result.\n')
        if staged:subprocess.run(['git','-C',str(self.worktree),'add','README.md'],check=True)
        return f,caller

    def dirty_finish(self,staged):
        f,_caller=self.dirty(staged);receipt=self.finish(f)
        self.assertEqual(receipt['commit'],self.source)
        self.assertIn('README.md',receipt['unscoped_tracked_dirt'])
        self.assertEqual(receipt['commit_observation']['execution_status'],'dirty')
        self.assertIn('execution-uncommitted-changes',receipt['commit_observation']['warnings'])
        self.assertTrue(subprocess.check_output(['git','-C',str(self.worktree),'status','--porcelain']))

    def test_unstaged_execution_keeps_its_commit_and_synthetic_dirt(self):self.dirty_finish(False)
    def test_staged_execution_keeps_its_commit_and_synthetic_dirt(self):self.dirty_finish(True)

    def test_explicit_commit_still_checks_original_staged_and_unstaged_concrete_scope(self):
        for staged in (False,True):
            with self.subTest(staged=staged):
                f,caller=self.dirty(staged);real=I.F.CAP._head_commit
                with mock.patch.object(M.os,'getcwd',return_value=str(f.repo)), \
                     mock.patch.object(I.F.CAP,'_head_commit',side_effect=lambda cwd:None if str(cwd)==str(self.worktree) else real(cwd)):
                    commit,cwd,obs=M.select_result_commit(f.route,caller,I.F.CAP)
                self.assertEqual(commit,caller);self.assertEqual(cwd,str(f.repo))
                self.assertEqual(M._finish_tracked_dirt(f.route,cwd,['source-scoped'],obs),['README.md'])
                for scope in (['README.md'],['source-scoped','README.md']):
                    with self.assertRaisesRegex(M.InlineFinishError,'finish-scoped-tracked-dirt'):
                        M._finish_tracked_dirt(f.route,cwd,scope,obs)

    def clone(self,f):
        target=f.base/'separate-clone'
        subprocess.run(['git','clone','-q',str(f.repo),str(target)],check=True)
        subprocess.run(['git','-C',str(target),'config','user.name','Fixture'],check=True)
        subprocess.run(['git','-C',str(target),'config','user.email','fixture@example.invalid'],check=True)
        (target/'README.md').write_text('Separate clone result.\n')
        subprocess.run(['git','-C',str(target),'commit','-qam','Clone result'],check=True)
        return target

    def test_missing_execution_cannot_promote_a_clone_with_shared_history(self):
        f=self.fixture();clone=self.clone(f);self.worktree.rename(f.base/'preserved-execution')
        done=subprocess.run(f.command,cwd=clone,env=f.env,text=True,capture_output=True)
        self.assertNotEqual(done.returncode,0,done.stdout)
        self.assertFalse(I.F.CAP.outcome_path(f.route_file).exists())

    def unavailable_execution(self,delete,explicit=False):
        f=self.fixture();result=self.commit_result(f)
        self.assertTrue(f.route.get('source_repository',{}).get('git_common_dir'))
        if delete:
            self.assertTrue(self.worktree.is_relative_to(f.base));shutil.rmtree(self.worktree)
        else:self.worktree.rename(f.base/'preserved-execution')
        receipt=self.finish(f,commit=result if explicit else None)
        self.assertEqual(receipt['commit'],result)
        self.assertTrue(receipt['commit_observation']['caller_repository_proven'])
        self.assertIn('execution-checkout-unavailable',receipt['commit_observation']['warnings'])

    def test_moved_checkout_recovers_only_from_recorded_common_directory(self):self.unavailable_execution(False)
    def test_deleted_checkout_recovers_only_from_recorded_common_directory(self):self.unavailable_execution(True)
    def test_explicit_result_recovers_with_the_same_repository_proof(self):self.unavailable_execution(False,True)

    def test_head_lookup_failure_keeps_common_directory_proof_independent(self):
        f=self.fixture();expected=self.commit_result(f);real=I.F.CAP._head_commit
        with mock.patch.object(M.os,'getcwd',return_value=str(f.repo)), \
             mock.patch.object(I.F.CAP,'_head_commit',side_effect=lambda cwd:None if str(cwd)==str(self.worktree) else real(cwd)):
            commit,cwd,obs=M.select_result_commit(f.route,None,I.F.CAP)
        self.assertEqual(commit,expected);self.assertTrue(obs['caller_repository_proven'])

    def test_common_directory_lookup_failure_never_uses_history_as_identity(self):
        f=self.fixture();self.commit_result(f);real=subprocess.run
        def unavailable(argv,*args,**kwargs):
            return subprocess.CompletedProcess(argv,128,'','unavailable') if '--git-common-dir' in argv else real(argv,*args,**kwargs)
        with mock.patch.object(M.os,'getcwd',return_value=str(f.repo)),mock.patch.object(M.subprocess,'run',side_effect=unavailable):
            commit,cwd,obs=M.select_result_commit(f.route,None,I.F.CAP)
        self.assertEqual(commit,self.source);self.assertFalse(obs['caller_repository_proven'])

    def test_status_failure_preserves_existing_unknown_failure_and_execution_head(self):
        f=self.fixture();self.commit_result(f);real=subprocess.run
        def unavailable(argv,*args,**kwargs):
            if 'status' in argv and str(self.worktree) in argv:return subprocess.CompletedProcess(argv,128,b'',b'unavailable')
            return real(argv,*args,**kwargs)
        with mock.patch.object(M.os,'getcwd',return_value=str(f.repo)),mock.patch.object(M.subprocess,'run',side_effect=unavailable):
            commit,cwd,obs=M.select_result_commit(f.route,None,I.F.CAP)
            self.assertEqual(commit,self.source);self.assertEqual(obs['execution_status'],'unavailable')
            with self.assertRaisesRegex(M.InlineFinishError,'finish-git-status-unavailable'):
                M._finish_tracked_dirt(f.route,cwd,['source-scoped'],obs)

    def test_damaged_execution_head_keeps_the_existing_unknown_failure(self):
        f=self.fixture();self.commit_result(f)
        head=Path(subprocess.check_output(['git','-C',str(self.worktree),'rev-parse','--git-path','HEAD'],text=True).strip())
        if not head.is_absolute():head=self.worktree/head
        head.write_text('invalid fixture head\n')
        done=subprocess.run(f.command,cwd=f.repo,env=f.env,text=True,capture_output=True)
        self.assertNotEqual(done.returncode,0,done.stdout)
        self.assertFalse(I.F.CAP.outcome_path(f.route_file).exists())
        self.assertEqual(head.read_text(),'invalid fixture head\n')

    def test_legacy_missing_checkout_without_origin_identity_stays_unknown(self):
        f=self.fixture();self.commit_result(f);legacy={**f.route};legacy.pop('source_repository',None)
        self.worktree.rename(f.base/'preserved-execution')
        with mock.patch.object(M.os,'getcwd',return_value=str(f.repo)):
            commit,cwd,obs=M.select_result_commit(legacy,None,I.F.CAP)
        self.assertFalse(commit);self.assertFalse(obs['caller_repository_proven'])

if __name__=='__main__':
    names=[name for name in ResultSelection.__dict__ if name.startswith('test_')]
    suite=unittest.TestSuite(ResultSelection(name) for name in names)
    if len(sys.argv)>1:suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:],sys.modules[__name__])
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
