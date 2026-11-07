"""Real PTY/control loop with synthetic native UI and private dependency assets."""
from pathlib import Path
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]

FAKE_NATIVE=r"""
import json,os,select,socket,subprocess,sys,time,tty
from pathlib import Path
sid=sys.argv[1]
def event(kind,identity=sid):
    raw=open('/proc/self/stat').read(); start=raw.rsplit(') ',1)[1].split()[19]
    request={'op':'event','event':kind,'harness':'claude','sid':identity,
        'pid':os.getpid(),'start':start,'token':os.environ['HEARTING_SHELL_TOKEN'],
        'seq':time.monotonic_ns()//1000}
    registry=Path(os.environ['CLAUDE_CONFIG_DIR'])/'sessions'/str(os.getpid())
    registry=registry.with_suffix('.json');registry.parent.mkdir(parents=True,exist_ok=True)
    registry.write_text(json.dumps({'pid':os.getpid(),'procStart':start,'sessionId':identity,
        'status':'busy' if kind=='prompt' else 'idle'}))
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(5);channel.connect(os.environ['HEARTING_SHELL_SOCKET'])
        channel.sendall((json.dumps(request)+'\n').encode());channel.recv(8192)
if '--headless-event' in sys.argv:
    event('start',sid+'-child');raise SystemExit()
tty.setraw(0)
exit_fd=os.open(os.environ['LOOP_NATIVE_EXIT'],os.O_RDWR|os.O_NONBLOCK)
began=time.monotonic();event('start');delay=(time.monotonic()-began)*1000
print('\x1b[?2004hACK_MS='+str(delay)+'\r\nREADY\r\n❯ \r\n──────────────────\r\n',end='',flush=True)
pending=''
while True:
    readable,_,_=select.select([0,exit_fd],[],[])
    if exit_fd in readable:break
    data=os.read(0,65536)
    if not data or data==b'\x03':break
    pending+=data.decode()
    while '\r' in pending:
        line,pending=pending.split('\r',1)
        line=line.replace('\x1b[200~','').replace('\x1b[201~','')
        callback_missing='PEER_NO_CALLBACK' in line
        if not callback_missing:event('prompt')
        if line=='spawn-child':
            subprocess.run([sys.executable,__file__,sid,'--headless-event'],
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        if not callback_missing:event('stop')
        print('\x1b[2J\x1b[HDONE_'+line+'\r\n❯ '+pending+'\r\n──────────────────\r\n',end='',flush=True)
        if line=='form':print('Do you want to proceed?\r\n1. Yes\r\n2. No\r\n',end='',flush=True)
"""

HOST_LAUNCHER=r"""
const fs=require('node:fs'),net=require('node:net');
const cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG,'utf8'));
if(process.argv.includes(cfg.host)){
 const report=error=>fs.appendFileSync(cfg.diagnostic,String(error?.stack||error)+'\n');
 process.on('uncaughtException',report);process.on('unhandledRejection',report);
 const cp=require('node:child_process'),spawn=cp.spawn;
 cp.spawn=function(file,args,options){
  if(args?.some(arg=>String(arg).endsWith('/session_shell.py'))){const child=spawn(file,args,{...options,stdio:['pipe','pipe','pipe']});child.stderr.on('data',data=>fs.appendFileSync(cfg.diagnostic,data));return child;}
  return spawn(file,args,options);
 };
 process.env.HEARTING_SHELL_PYTHON=cfg.backend;
 fs.writeFileSync(cfg.hostPidFile,String(process.pid));
 const inlet=net.createServer(socket=>{let raw='';socket.on('data',data=>{
  raw+=data;if(!raw.includes('\n'))return;
  const input=JSON.parse(raw.split('\n')[0]);
  process.stdin.emit('data',Buffer.from(input.batch,'base64'));socket.end('{}\n');
 });});
 inlet.listen(cfg.inlet);
}
"""

DRIVER=r"""
const fs=require('node:fs'),net=require('node:net'),path=require('node:path');
const pty=require('node-pty'),cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG,'utf8'));
let raw='',alive=true,ended,child,barrier;
const listeners=[];
const exit=new Promise(resolve=>ended=resolve);
function waitFor(pattern){return new Promise((resolve,reject)=>{
 if(pattern.test(raw))return resolve();
 const item={pattern,resolve:()=>{clearTimeout(timer);resolve();}};
 const timer=setTimeout(()=>{const at=listeners.indexOf(item);if(at>=0)listeners.splice(at,1);reject(Error('native output unavailable '+pattern));},12000);
 listeners.push(item);
});}
function rpc(endpoint,data){return new Promise((resolve,reject)=>{
 const socket=net.connect(endpoint),timer=setTimeout(()=>{socket.destroy();reject(Error('control timeout'));},6000);let raw='';
 socket.on('error',error=>{clearTimeout(timer);reject(error);});
 socket.on('connect',()=>socket.write(JSON.stringify(data)+'\n'));
 socket.on('data',part=>{raw+=part;if(raw.includes('\n')){clearTimeout(timer);socket.end();resolve(JSON.parse(raw.split('\n')[0]));}});
});}
function existing(file){return new Promise((resolve,reject)=>{
 if(fs.existsSync(file))return resolve();
 const watch=fs.watch(path.dirname(file),()=>{if(fs.existsSync(file)){clearTimeout(timer);watch.close();resolve();}});
 const timer=setTimeout(()=>{watch.close();reject(Error('binding unavailable'));},12000);
 if(fs.existsSync(file)){clearTimeout(timer);watch.close();resolve();}
});}
(async()=>{try{
 if(cfg.barrier)barrier=fs.openSync(cfg.barrier,fs.constants.O_RDWR|fs.constants.O_NONBLOCK);
 child=pty.spawn(cfg.python,[cfg.entry,'claude',cfg.python,cfg.fake,cfg.sid],
    {cols:100,rows:30,cwd:cfg.project,env:{...process.env,TERM:'xterm-256color',HEARTING_SHELL_DIRECTORY:cfg.directory,
        HEARTING_SHELL_PYTHON:cfg.backend,NODE_PATH:process.env.NODE_PATH,LOOP_NATIVE_EXIT:cfg.exit_fifo,NODE_OPTIONS:'--require '+cfg.launcher}});
 child.onData(text=>{raw+=text;if(raw.length>1024*1024)raw=raw.slice(-512*1024);
    for(let n=listeners.length-1;n>=0;n--)if(listeners[n].pattern.test(raw))listeners.splice(n,1)[0].resolve();});
 child.onExit(()=>{alive=false;ended();});
 await waitFor(/READY/);
 const ack=Number(raw.match(/ACK_MS=([0-9.]+)/)[1]);
 if(barrier!==undefined){fs.writeSync(barrier,Buffer.from('1'));fs.closeSync(barrier);barrier=undefined;}
 await existing(cfg.hostPidFile);const hostPID=Number(fs.readFileSync(cfg.hostPidFile,'utf8'));
 const file=path.join(cfg.registry,'claude','session-shell-'+hostPID+'.json');
 await existing(file);
 // A published identity is not yet a completed display/readiness observation.
 await waitFor(/7d [0-9?]/);
 if(cfg.scenario!=='initial'){
   await rpc(cfg.inlet,{batch:Buffer.from('warm\r').toString('base64')});await waitFor(/DONE_warm/);
 }
 if(cfg.scenario==='mixed'){
   await rpc(cfg.inlet,{batch:Buffer.from('second\rdraft').toString('base64')});await waitFor(/DONE_second/);
 }else if(cfg.scenario==='child'){
   await rpc(cfg.inlet,{batch:Buffer.from('spawn-child\r').toString('base64')});await waitFor(/DONE_spawn-child/);
 }else if(cfg.scenario==='form'){
   await rpc(cfg.inlet,{batch:Buffer.from('form\r').toString('base64')});await waitFor(/Do you want to proceed/);
 }else if(cfg.scenario==='unpainted-draft'){
   await rpc(cfg.inlet,{batch:Buffer.from('unpainted draft').toString('base64')});
 }else if(cfg.scenario==='missing-callback'||cfg.scenario==='missing-callback-enter'){
   const cp=require('node:child_process');
   const body="import json,sys;sys.path.insert(0,"+JSON.stringify(cfg.utilities)+");import session_shell as s;s.registry_root();p=s._peer_message();text,ref=p.prepare_peer_message('PEER_NO_CALLBACK '+sys.argv[2],{'harness':'codex','session_id':'synthetic-sender'},{'harness':'claude','session_id':sys.argv[1]},defer=True);print(json.dumps(s.deliver_pending(ref,'claude',sys.argv[1])))";
   const sent=JSON.parse(cp.execFileSync(cfg.python,['-c',body,cfg.sid,cfg.scenario],{env:{...process.env},encoding:'utf8'}));
   if(!sent.ok)throw Error('synthetic admission failed '+JSON.stringify(sent));
   await rpc(cfg.inlet,{batch:Buffer.from(cfg.scenario==='missing-callback-enter'?'held human\r':'held human').toString('base64')});
   if(cfg.scenario==='missing-callback-enter')await waitFor(/DONE_held human/);
   else await waitFor(/PEER_NO_CALLBACK/);
 }
 const displayLag=[];
 // Registry publication precedes the first complete display observation.
 if(cfg.scenario==='display')await waitFor(/7d [0-9?]/);
 if(cfg.scenario==='display')for(let index=0;index<4;index++){
   const title='event-title-'+index,before=process.hrtime.bigint();
   fs.writeFileSync(cfg.title+'.new',JSON.stringify({title,ts:Date.now()/1000}));fs.renameSync(cfg.title+'.new',cfg.title);
   await waitFor(new RegExp(title));displayLag.push(Number(process.hrtime.bigint()-before)/1e6);
 }
 const binding=JSON.parse(fs.readFileSync(file,'utf8'));
 const status=await rpc(binding.socket,{...binding,op:'status'});
 console.log(JSON.stringify({ack_ms:ack,status,display_lag_ms:displayLag}));
 }catch(error){console.error(error.stack);process.exitCode=1;}
 finally{
   if(fs.existsSync(cfg.diagnostic)){console.error(fs.readFileSync(cfg.diagnostic,'utf8'));process.exitCode=1;}
   if(barrier!==undefined){fs.writeSync(barrier,Buffer.from('1'));fs.closeSync(barrier);}
   if(child&&alive){
     const exitFD=fs.openSync(cfg.exit_fifo,fs.constants.O_RDWR|fs.constants.O_NONBLOCK);fs.writeSync(exitFD,Buffer.from('q'));fs.closeSync(exitFD);
     await new Promise((resolve,reject)=>{
     const timeout=setTimeout(()=>reject(Error('owned native child did not exit')),5000);
     exit.then(()=>{clearTimeout(timeout);resolve();});
   });}
 }
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
"""


class SessionShellPTYTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node=shutil.which('node')
        if not cls.node or not shutil.which('npm'):
            raise unittest.SkipTest('native PTY dependency builder needs node/npm')
        cls.temporary=tempfile.TemporaryDirectory(prefix='hearting-shell-loop-',dir='/var/tmp')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root=Path(cls.temporary.name)
        spec=importlib.util.spec_from_file_location('shell_loop_isolation',ROOT/'tools/run-tests.py')
        runner=importlib.util.module_from_spec(spec);sys.modules[spec.name]=runner;spec.loader.exec_module(runner)
        cls.env=runner.build_isolated_env(cls.root,repo_root=ROOT)
        cls.env['CLAUDE_CONFIG_DIR']=str(cls.root/'native-claude')
        policy=Path(cls.env.get('XDG_CONFIG_HOME') or Path(cls.env['HOME'])/'.config')/'hearting/session-shell.json'
        policy.parent.mkdir(parents=True,exist_ok=True);policy.write_text('{"enabled":true}');policy.chmod(0o600)
        cls.env['npm_config_cache']=str(cls.root/'npm-cache')
        cls.env['npm_config_devdir']=str(cls.root/'node-gyp')
        cls.env['npm_config_userconfig']=str(cls.root/'npmrc');(cls.root/'npmrc').write_text('')
        sys.path[:0]=[str(ROOT/'tools/install'),str(ROOT/'utilities')]
        import session_shell_install,session_shell
        with mock.patch.dict(os.environ,cls.env,clear=True):
            assets=session_shell_install.prepare_assets()
            cls.registry=Path(session_shell.registry_root())
        cls.env['NODE_PATH']=str(assets/'node_modules')
        (cls.registry/'claude').mkdir(parents=True,exist_ok=True)
        cls.fake=cls.root/'native.py';cls.fake.write_text(FAKE_NATIVE)
        cls.launcher=cls.root/'host.cjs';cls.launcher.write_text(HOST_LAUNCHER)
        cls.driver=cls.root/'driver.cjs';cls.driver.write_text(DRIVER)
        cls.project=cls.root/'project';cls.project.mkdir()

    def run_loop(self,scenario,*,blocked_backend=False):
        directory=self.root/('case-'+scenario);directory.mkdir(mode=0o700)
        exit_fifo=directory/'native-exit';os.mkfifo(exit_fifo,0o600)
        sid='00000000-0000-4000-8000-000000000052'
        import time
        cache=Path(self.env['XDG_STATE_HOME'])/'hearting/human-waits'
        cache.mkdir(mode=0o700,parents=True,exist_ok=True);cache.chmod(0o700)
        key='claude\0'+sid;now=time.time()
        (cache/'projection.json').write_text(json.dumps({'schema_version':3,'observed_at':now,'items':[],
            'by_session':{},'total':0,'older':0,'complete':True,'route_status':{key:''},'route_sessions':{key:now}}))
        (cache/'projection.json').chmod(0o600)
        backend=sys.executable;barrier=None
        if blocked_backend:
            barrier=directory/'barrier';os.mkfifo(barrier,0o600)
            backend=str(directory/'python')
            Path(backend).write_text('#!'+sys.executable+'\nimport os,runpy,sys\nf=os.open('+repr(str(barrier))+',os.O_RDONLY)\nos.read(f,1);os.close(f)\nsys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name="__main__")\n')
            Path(backend).chmod(0o700)
        config={'directory':str(directory),'host':str(ROOT/'utilities/session-shell/host.cjs'),
                'startFile':str(directory/'started'),'inlet':str(directory/'input.sock'),
                'entry':str(ROOT/'utilities/session-shell/entry.py'),'hostPidFile':str(directory/'host-pid'),
                'diagnostic':str(directory/'display-errors'),
                'launcher':str(self.launcher),'python':sys.executable,'fake':str(self.fake),'sid':sid,
                'project':str(self.project),'backend':backend,'barrier':str(barrier) if barrier else None,
                'registry':str(self.registry),'scenario':scenario,'utilities':str(ROOT/'utilities'),
                'exit_fifo':str(exit_fifo)}
        from fleet import titles
        with mock.patch.dict(os.environ,self.env,clear=True):
            title=Path(titles.sidecar_path(sid,'claude'))
        title.parent.mkdir(parents=True,exist_ok=True);config['title']=str(title)
        configfile=directory/'config.json';configfile.write_text(json.dumps(config))
        env=dict(self.env,LOOP_CONFIG=str(configfile))
        child=subprocess.Popen([self.node,str(self.driver)],env=env,cwd=self.project,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        birth=Path('/proc/'+str(child.pid)+'/stat').read_text().rsplit(') ',1)[1].split()[19]
        try:
            stdout,stderr=child.communicate(timeout=35)
        except subprocess.TimeoutExpired:
            fd=os.pidfd_open(child.pid)
            try:
                current=Path('/proc/'+str(child.pid)+'/stat').read_text().rsplit(') ',1)[1].split()[19]
                if child.pid>1 and current==birth:signal.pidfd_send_signal(fd,signal.SIGTERM)
            finally:os.close(fd)
            child.communicate(timeout=5)
            raise
        self.assertEqual(child.returncode,0,stderr)
        return json.loads(stdout.splitlines()[-1])

    def test_native_start_ack_does_not_wait_for_a_blocked_display_backend(self):
        result=self.run_loop('start',blocked_backend=True)
        self.assertLess(result['ack_ms'],250)

    def test_enter_and_following_draft_in_one_chunk_stays_nonempty(self):
        result=self.run_loop('mixed')
        self.assertEqual(result['status']['state'],'idle')
        self.assertFalse(result['status']['empty'])

    def test_headless_descendant_cannot_replace_the_native_session_identity(self):
        result=self.run_loop('child')
        self.assertTrue(result['status']['ok'])
        self.assertEqual(result['status']['sid'],'00000000-0000-4000-8000-000000000052')

    def test_record_changes_update_the_line_without_waiting_for_a_collection_tick(self):
        result=self.run_loop('display')
        print(json.dumps({'event':'session-shell-record-update','lag_ms':result['display_lag_ms']}))
        self.assertEqual(len(result['display_lag_ms']),4)
        self.assertLess(max(result['display_lag_ms']),500)

    def test_new_idle_session_has_a_verified_empty_composer_before_its_first_prompt(self):
        result=self.run_loop('initial')
        self.assertEqual(result['status']['state'],'idle')
        self.assertTrue(result['status']['empty'])

    def test_a_form_appearing_after_stop_is_not_an_empty_ready_composer(self):
        result=self.run_loop('form')
        self.assertFalse(result['status']['empty'])

    def test_a_draft_waiting_for_native_paint_is_not_proved_empty_by_the_old_screen(self):
        result=self.run_loop('unpainted-draft')
        self.assertFalse(result['status']['empty'])

    def test_a_held_enter_still_advances_the_existing_input_frontier(self):
        result=self.run_loop('missing-callback-enter')
        self.assertTrue(result['status']['empty'],result['status'])

    def test_missing_native_callback_cannot_leave_human_keyboard_bytes_held(self):
        result=self.run_loop('missing-callback')
        self.assertEqual(result['status']['held_bytes'],0)


if __name__=='__main__':unittest.main()
