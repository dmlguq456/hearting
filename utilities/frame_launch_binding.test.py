#!/usr/bin/env python3
"""Real inert subprocess coverage for approved repository handoffs; no models."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'utilities'), str(ROOT/'tools')]

def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT/'utilities'/file)
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module
    spec.loader.exec_module(module); return module

F = load('frame_launch_fixture', 'framed_start.test.py')
O = load('frame_launch_owner', 'dispatch-owner.py')
import frame_repository as B

class LaunchBinding(F.FrameFrictionContinuationTest):
    def prepare(self):
        self.new_repository_frame(); self.assertTrue(self.settle().get('owner_started'))
        self.outside = self.root.parent/'outside'; self.outside.mkdir()
        self.target = self.outside/'new-project'; self.target.mkdir()
        self.saved = self.parent.with_name('saved-parent')
        self.wrapper = self.root/'inert.py'
        self.wrapper.write_text("import os,sys,json\nfrom pathlib import Path\np=Path(sys.argv[sys.argv.index('--worktree')+1]) if '--worktree' in sys.argv else Path('.')\n(p/'payload.txt').write_text(str(os.stat('.').st_ino))\n")

    def swap(self):
        self.parent.rename(self.saved); self.parent.symlink_to(self.outside, target_is_directory=True)

    def last_launch(self, lifecycle):
        self.prepare(); real_run, real_popen = subprocess.run, subprocess.Popen
        fired = []
        def invoke(argv, *args, **kwargs):
            if str(self.wrapper) in argv:
                fired.append(True); self.swap()
            return (real_popen if lifecycle == 'foreground-scoped' else real_run)(argv, *args, **kwargs)
        with B.RepositoryBinding(self.record()) as binding:
            expected = os.fstat(binding.fds['leaf']).st_ino
            with mock.patch.object(O, 'select_launch_lifecycle', return_value=lifecycle), \
                 mock.patch.object(subprocess, 'Popen' if lifecycle == 'foreground-scoped' else 'run', side_effect=invoke):
                rc = O.run_owner_wrapper([sys.executable, str(self.wrapper), '--start', '--worktree', str(self.destination)], dict(os.environ))
        self.assertEqual(rc, 0); self.assertTrue(fired)
        self.assertFalse((self.target/'payload.txt').exists())
        self.assertEqual((self.saved/'new-project/payload.txt').read_text(), str(expected))

    def test_detached_last_verify_to_child_use(self): self.last_launch('detached')
    def test_foreground_last_verify_to_child_use(self): self.last_launch('foreground-scoped')

    def test_all_adapter_entries_consume_binding_and_keep_durable_labels(self):
        self.prepare()
        for harness in ('claude','codex','opencode'):
            with self.subTest(harness=harness), B.RepositoryBinding(self.record()):
                code = '''import importlib.util,sys,os,json
from pathlib import Path
sys.path.insert(0,sys.argv[1]+'/utilities')
import frame_repository as b
s=importlib.util.spec_from_file_location('adapter',sys.argv[1]+'/adapters/'+sys.argv[2]+'/bin/dispatch-headless.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
p=m.parser();real=p.parse_args;seen=[]
def parse(*a,**k):
 v=real(*a,**k);seen.append(v);return v
p.parse_args=parse;m.parser=lambda:p
if hasattr(m,'_bind_runtime_parent'):m._bind_runtime_parent=lambda a:None
def stop(*a,**k):
 assert seen[0].worktree==sys.argv[3]
 assert b.durable_argv(['--worktree',sys.argv[4]])==['--worktree',sys.argv[3]]
 Path(b.execution_path(seen[0].worktree),'adapter-'+sys.argv[2]).write_text(str(os.stat('.').st_ino))
 raise SystemExit(0)
m.WRAPPER_COMMON.select_execution_root=stop
raise SystemExit(m.main(['adapter','--dry-run','--worktree',sys.argv[4],'--slug','fixture','--capability','autopilot-code','--capability-mode','dev','--intensity','quick','--dispatch-depth','1','--worker-type','owner','--assigned-contract','autopilot-code','--owner','autopilot-code','--model-profile','balanced-deep']))
'''
                options = B.child_options(env=dict(os.environ))
                alias = f"/proc/self/fd/{B.current().fds['leaf']}"
                done = subprocess.run([sys.executable, '-c', code, str(ROOT), harness, str(self.destination), alias], capture_output=True, text=True, **options)
                self.assertEqual(done.returncode, 0, done.stdout+done.stderr)
                self.assertTrue((self.destination/('adapter-'+harness)).exists())

    def resume_probe(self, swap):
        self.prepare()
        with B.RepositoryBinding(self.record()): environment = B.child_options(env=dict(os.environ))['env']
        if swap: self.swap()
        code = "import sys;sys.path.insert(0,sys.argv[1]);import frame_repository as b;\n@b.process_entry\ndef run():\n import subprocess\n return subprocess.run([sys.executable,sys.argv[2]],**b.child_options()).returncode\nraise SystemExit(run())\n"
        done = subprocess.run([sys.executable,'-c',code,str(ROOT/'utilities'),str(self.wrapper)],env=environment,capture_output=True,text=True)
        self.assertFalse((self.target/'payload.txt').exists())
        if swap: self.assertEqual(done.returncode,65,done.stdout+done.stderr)
        else:
            self.assertEqual(done.returncode,0,done.stdout+done.stderr)
            self.assertTrue((self.destination/'payload.txt').exists())

    def test_fresh_resume_reopens_recorded_identity_without_inherited_fds(self): self.resume_probe(False)
    def test_fresh_resume_rejects_replaced_parent_without_using_stale_fds(self): self.resume_probe(True)

    def test_supervised_native_spawn_uses_same_directory(self):
        self.prepare()
        server = load('frame_launch_appserver','codex-app-server-supervisor.py')
        real = subprocess.Popen; fired=[]
        def invoke(argv,*args,**kwargs):
            fired.append(True); self.swap(); return real(argv,*args,**kwargs)
        with B.RepositoryBinding(self.record()), mock.patch.object(subprocess,'Popen',side_effect=invoke):
            instance = server.AppServer([sys.executable,str(self.wrapper)],str(self.destination),dict(os.environ))
        instance.process.wait(timeout=10); instance.close()
        self.assertTrue(fired); self.assertFalse((self.target/'payload.txt').exists())
        self.assertTrue((self.saved/'new-project/payload.txt').exists())

    def test_claude_stream_child_uses_same_directory(self):
        from types import SimpleNamespace
        self.prepare()
        supervisor = load('frame_launch_claude','claude-session-supervisor.py')
        real = subprocess.Popen
        def invoke(argv,*args,**kwargs):
            self.swap(); return real(argv,*args,**kwargs)
        with B.RepositoryBinding(self.record()), \
             mock.patch.object(supervisor,'claude_command',return_value=[sys.executable,str(self.wrapper)]), \
             mock.patch.object(subprocess,'Popen',side_effect=invoke):
            instance = supervisor.ClaudeStreamSession(SimpleNamespace(worktree=str(self.destination),state_file=None),'fixture')
        instance.process.wait(timeout=10); instance.close()
        self.assertFalse((self.target/'payload.txt').exists())
        self.assertTrue((self.saved/'new-project/payload.txt').exists())

    def test_real_fence_governor_shell_keeps_binding_at_last_payload_spawn(self):
        self.prepare()
        child = self.root/'governor-probe.py'
        child.write_text("""import importlib.util,sys,subprocess
from pathlib import Path
root,parent,saved,outside,payload,governor = sys.argv[1:]
sys.path.insert(0,root+'/utilities')
s=importlib.util.spec_from_file_location('governor',root+'/utilities/model-worker-governor.py');g=importlib.util.module_from_spec(s);sys.modules[s.name]=g;s.loader.exec_module(g)
real=subprocess.Popen
fired=[]
def invoke(argv,*a,**k):
 if argv[0]=='sh':
  Path(parent).rename(saved);Path(parent).symlink_to(outside,target_is_directory=True);fired.append(True)
 return real(argv,*a,**k)
g.subprocess.Popen=invoke
sys.argv=['governor','--root',governor,'run','--class','dispatch','--','sh','-c',sys.executable+' '+payload]
rc=g.main()
assert fired, 'payload spawn did not run'
raise SystemExit(rc)
""")
        read_fd,write_fd=os.pipe()
        try:
            with B.RepositoryBinding(self.record()):
                argv=[sys.executable,str(ROOT/'utilities/launch-fence.py'),'--parent-pid',str(os.getpid()),
                      '--gate-fd',str(read_fd),'--',sys.executable,str(child),str(ROOT),str(self.parent),
                      str(self.saved),str(self.outside),str(self.wrapper),str(self.root/'governor')]
                proc=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                    **B.child_options(env=dict(os.environ),pass_fds=(read_fd,)))
                os.write(write_fd,b'1');out,err=proc.communicate(timeout=30)
            self.assertEqual(proc.returncode,0,out+err)
            self.assertFalse((self.target/'payload.txt').exists())
            self.assertTrue((self.saved/'new-project/payload.txt').exists())
        finally:
            os.close(read_fd);os.close(write_fd)

    def test_handoff_cannot_replace_the_approved_creation_evidence(self):
        self.prepare()
        with B.RepositoryBinding(self.record()):
            environment=B.child_options(env=dict(os.environ))['env']
        data=json.loads(environment[B._HANDOFF]);data['record']['first_leg']['repository_created']['inode']+=1
        environment[B._HANDOFF]=json.dumps(data)
        code="import sys;sys.path.insert(0,sys.argv[1]);import frame_repository as b;\n@b.process_entry\ndef run(): return 0\nraise SystemExit(run())\n"
        done=subprocess.run([sys.executable,'-c',code,str(ROOT/'utilities')],env=environment,capture_output=True,text=True)
        self.assertEqual(done.returncode,65,done.stdout+done.stderr)

    def test_an_explicit_other_worktree_does_not_inherit_a_stale_binding(self):
        self.prepare()
        with B.RepositoryBinding(self.record()):environment=B.child_options(env=dict(os.environ))['env']
        self.swap()
        with mock.patch.dict(os.environ,environment,clear=True):
            with B.process_binding(['--worktree',str(self.target)]) as binding:
                self.assertFalse(binding.active)

    def test_native_commands_start_relative_to_the_bound_process_cwd(self):
        self.prepare()
        for harness in ('claude','codex','opencode'):
            with self.subTest(harness=harness), B.RepositoryBinding(self.record()):
                module=load('frame_command_'+harness,ROOT/'adapters'/harness/'bin/dispatch-headless.py')
                args=module.parser().parse_args(['--worktree',str(self.destination),'--slug','fixture',
                    '--capability','autopilot-code','--capability-mode','dev','--intensity','quick',
                    '--dispatch-depth','2','--worker-type','stage','--assigned-contract','autopilot-code',
                    '--owner','autopilot-code','--model-profile','balanced-deep'])
                args.resolved_model_settings={'source':'inherit','model':None,'reasoning':None,'effort':None,'variant':None,'context_window':'100000'}
                args.artifact_root=str(self.root);args.agent_home=ROOT;args.jobs_path=self.jobs
                args.resolved_completion_delivery='poll';args.owner_route_binding=None
                args.nested_codex_home=None;args.execution_access_grant=None;args.nested_headless_network=False
                if harness=='opencode':
                    with mock.patch.object(module.opencode_runtime,'is_v2',return_value=True):
                        command=module.shell_command(args,self.root/'prompt',self.root/'log')
                    self.assertIn('cd -- . &&',command)
                elif harness=='claude':
                    command=module.shell_command(args,self.root/'prompt',self.root/'log')
                    self.assertIn('cd . &&',command)
                else:
                    command=module.shell_command(args,self.root/'prompt',self.root/'log')
                    self.assertIn('--cd .',command)

    def continuing_leg(self):
        self.new_repository_frame()
        later={**self.leg,'shape':'direct'}
        self.set_briefs([self.leg,later],[self.leg,later])
        self.set_interview({'legs':[self.leg,later]})
        receipt=self.settle();self.assertTrue(receipt.get('owner_started'),receipt)
        record=self.record();path=self.record_path()
        binding=F.RP.read_route_plan(str(path)+'#1',self.root)
        previous=json.loads(self.leg_routes()[0].read_text())
        compose=record['decision']['first_leg_compose']
        return lambda: F.R.compile_first_leg(binding['leg'],frame_route=previous,
            frame_cycle_id=record['decision']['frame_route']['cycle_id'],context=compose['context'],
            binding=binding,work_request=previous['work_request'],readiness=lambda cwd=None:self.readiness(previous,self.jobs),index=1)

    def test_continuing_leg_reuses_the_same_dedicated_repository(self):
        compile_leg=self.continuing_leg();route=compile_leg()
        self.assertEqual(route['cwd'],str(self.destination))
        self.assertFalse(self.destination.with_name('new-project-wt').exists())

    def test_continuing_leg_cannot_reinterpret_a_replaced_destination(self):
        compile_leg=self.continuing_leg()
        outside=self.root.parent/'later-outside';outside.mkdir();target=outside/'new-project';target.mkdir()
        subprocess.run(['git','init','-q',str(target)],check=True)
        subprocess.run(['git','-C',str(target),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                        'commit','--allow-empty','-qm','Outside fixture'],check=True)
        self.parent.rename(self.parent.with_name('saved-parent'));self.parent.symlink_to(outside,target_is_directory=True)
        with self.assertRaises(B.RepositoryBindingError):compile_leg()
        rows=subprocess.check_output(['git','-C',str(target),'worktree','list','--porcelain'],text=True)
        self.assertEqual(rows.count('worktree '),1)

    def test_manual_same_destination_after_snapshot_can_launch_without_frame_route_binding(self):
        self.new_repository_frame()
        def stop(name):
            if name=='after-compiled-snapshot':raise RuntimeError('fixture interrupted')
        with mock.patch.object(F.W,'_checkpoint',side_effect=stop):
            with self.assertRaisesRegex(RuntimeError,'fixture interrupted'):self.settle()
        record=self.record();self.assertNotIn('route',record['first_leg'])
        binding=F.RP.read_route_plan(str(self.record_path())+'#0',self.root)
        kwargs=F.R._leg_compose_kwargs(F.RP.leg_arguments(self.leg),frame_route=self.route,
            frame_cycle_id=record['decision']['frame_route']['cycle_id'],slug='manual-same-source')
        route=F.R.compose_route(**kwargs,**F.R._leg_evidence(self.leg,lambda:self.readiness(self.route,self.jobs)),
                               route_plan=binding,work_request=self.route['work_request'])
        path=F.R.publish_composed_route(route,str(self.root))
        script=self.root/'manual-inert.py';script.write_text("from pathlib import Path\nimport os,sys\nPath(sys.argv[1],'payload.txt').write_text(str(os.stat('.').st_ino))\n")
        with B.for_route(route):
            self.assertEqual(O.run_owner_wrapper([sys.executable,str(script),str(self.destination)],dict(os.environ)),0)
        self.assertEqual((self.destination/'payload.txt').read_text(),str(self.destination.stat().st_ino))
        self.assertNotIn('route',self.record()['first_leg'])

    def test_jsonl_writer_passes_the_binding_to_its_native_child(self):
        self.prepare();writer=load('frame_launch_writer','codex-jsonl-writer.py')
        audit=self.root/'native-cwd.txt';script=self.root/'native-inert.py'
        script.write_text("import os,sys,json\nfrom pathlib import Path\nvalue=os.stat('.').st_ino\ntry:leak=os.fstat(int(sys.argv[3])).st_ino==value\nexcept OSError:leak=False\nPath(sys.argv[2]).write_text(json.dumps({'inode':value,'directory_fd_inherited':leak}))\nif value==int(sys.argv[1]):Path('payload.txt').write_text('fixture')\n")
        real=subprocess.Popen
        def invoke(argv,*args,**kw):
            self.swap();return real(argv,*args,**kw)
        with B.RepositoryBinding(self.record()) as binding, mock.patch.object(subprocess,'Popen',side_effect=invoke):
            expected=os.fstat(binding.fds['leaf']).st_ino
            rc=writer.run(self.root/'native.jsonl','fixture',[sys.executable,str(script),str(expected),str(audit),str(binding.fds['leaf'])])
        self.assertEqual(rc,0);observation=json.loads(audit.read_text())
        self.assertEqual(observation['inode'],expected);self.assertFalse(observation['directory_fd_inherited'])
        self.assertFalse((self.target/'payload.txt').exists())
        self.assertTrue((self.saved/'new-project/payload.txt').exists())

    def test_native_requests_carry_the_real_path_that_outlives_the_server(self):
        # Codex checks a patch path against these roots by its text and runs shells in a
        # sandbox with its own /proc, so a /proc/<pid>/cwd alias rejected worktree writes.
        self.prepare();server=load('frame_rpc_server','codex-app-server-supervisor.py')
        with B.RepositoryBinding(self.record()) as binding:
            instance=server.AppServer([sys.executable,'-c','import time;time.sleep(20)'],str(self.destination),dict(os.environ))
            try:
                path=B.rpc_worktree(str(self.destination),instance)
                self.assertEqual(path,str(self.destination))
                self.assertEqual(Path(path).stat().st_ino,os.fstat(binding.fds['leaf']).st_ino)
                self.assertEqual(B.native_path(str(self.destination/'sub')),str(self.destination/'sub'))
                literal=str(self.destination)+'/request text'
                params=B.runtime_request({'cwd':str(self.destination),'input':[{'text':literal}],
                    'sandboxPolicy':{'writableRoots':[str(self.destination),str(self.outside)]}},instance)
                self.assertEqual(params['cwd'],str(self.destination))
                self.assertEqual(params['sandboxPolicy']['writableRoots'],[str(self.destination),str(self.outside)])
                self.assertEqual(params['input'][0]['text'],literal)
                self.assertTrue((self.destination/'src/module.py').is_relative_to(params['cwd']))
                self.assertFalse(any((self.root.parent/'elsewhere/x.py').is_relative_to(root)
                                     for root in params['sandboxPolicy']['writableRoots']))
            finally:instance.close()
        (Path(path)/'after-exit.txt').write_text('fixture')
        self.assertTrue((self.destination/'after-exit.txt').exists())

    def test_replaced_directory_is_never_handed_to_a_native_request(self):
        self.prepare();server=load('frame_rpc_server','codex-app-server-supervisor.py')
        with B.RepositoryBinding(self.record()):
            instance=server.AppServer([sys.executable,'-c','import time;time.sleep(20)'],str(self.destination),dict(os.environ))
            try:
                self.swap()
                with self.assertRaises(B.RepositoryBindingError):B.rpc_worktree(str(self.destination),instance)
                with self.assertRaises(B.RepositoryBindingError):B.native_path(str(self.destination))
            finally:instance.close()

    def test_read_only_request_stays_read_only(self):
        self.prepare();server=load('frame_rpc_server','codex-app-server-supervisor.py')
        args=__import__('argparse').Namespace(sandbox='read-only',network_access=False,
                                              worktree=str(self.destination),writable_root=[str(self.outside)])
        with B.RepositoryBinding(self.record()):
            instance=server.AppServer([sys.executable,'-c','import time;time.sleep(20)'],str(self.destination),dict(os.environ))
            try:
                params=B.runtime_request({'cwd':str(self.destination),'sandboxPolicy':server.sandbox_policy(args)},instance)
            finally:instance.close()
        self.assertEqual(params['sandboxPolicy'],{'type':'readOnly','networkAccess':False})

    def test_explicit_recovery_binds_its_independently_chosen_repository(self):
        self.prepare()
        subprocess.run(['git','init','-q',str(self.target)],check=True)
        subprocess.run(['git','-C',str(self.target),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                        'commit','--allow-empty','-qm','Manual fixture'],check=True)
        record=self.record();context=record['decision']['first_leg_compose']['context']
        binding=F.RP.read_route_plan(str(self.record_path())+'#0',self.root)
        kwargs=F.R._leg_compose_kwargs(F.RP.leg_arguments(self.leg),frame_route=self.route,
                    frame_cycle_id=record['decision']['frame_route']['cycle_id'],slug='manual-recovery')
        kwargs['cwd']=str(self.target)
        readiness=lambda:self.readiness({**self.route,'cwd':str(self.target)},self.jobs)
        route=F.R.compose_route(**kwargs,**F.R._leg_evidence(self.leg,readiness),
                              route_plan=binding,work_request=self.route['work_request'])
        path=F.R.publish_composed_route(route,str(self.root))
        other=self.root.parent/'unapproved-manual';other.mkdir();(other/'new-project').mkdir()
        saved=self.outside.with_name('saved-manual')
        real=subprocess.run
        def invoke(argv,*args,**kw):
            if str(self.wrapper) in argv:
                self.outside.rename(saved);self.outside.symlink_to(other,target_is_directory=True)
            return real(argv,*args,**kw)
        with B.for_route(route) as repository, \
             mock.patch.object(O,'select_launch_lifecycle',return_value='detached'), \
             mock.patch.object(subprocess,'run',side_effect=invoke):
            # On older code this context is inactive, which leaves the original
            # mutable argument exposed at the actual subprocess boundary.
            rc=O.run_owner_wrapper([sys.executable,str(self.wrapper),'--worktree',str(self.target)],dict(os.environ))
        self.assertEqual(rc,0)
        self.assertFalse((other/'new-project/payload.txt').exists())
        self.assertTrue((saved/'new-project/payload.txt').exists())

if __name__=='__main__':
    names=[n for n in LaunchBinding.__dict__ if n.startswith('test_')]
    suite=unittest.TestSuite(LaunchBinding(n) for n in names)
    if len(sys.argv)>1:suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:],sys.modules[__name__])
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
