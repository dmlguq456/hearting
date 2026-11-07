"""Display failures preserve the existing native PID and subsequent human I/O."""
from pathlib import Path
import importlib.util
import json
import re
import os
import select
import signal
import struct
import sys
import termios
import fcntl
import pty
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('shell_survival_fixture',ROOT/'utilities/session_shell_pty.test.py')
fixture=importlib.util.module_from_spec(spec);sys.modules[spec.name]=fixture;spec.loader.exec_module(fixture)


def process(pid):
    try:
        row=Path('/proc')/str(pid);fields=(row/'stat').read_text().rsplit(') ',1)[1].split()
        return {'pid':pid,'start':fields[19],'live':fields[0] not in ('Z','X')}
    except FileNotFoundError:return {'pid':pid,'live':False}


def terminate_owned(identity):
    pid=identity['pid']
    if pid<=1:raise ValueError('unsafe child pid')
    fd=os.pidfd_open(pid)
    try:
        if process(pid).get('start')!=identity['start']:raise ValueError('child changed')
        signal.pidfd_send_signal(fd,signal.SIGTERM)
        return bool(select.select([fd],[],[],2)[0])
    finally:os.close(fd)


PRELOAD=r"""
const Module=require('node:module'),fs=require('node:fs'),load=Module._load;
if(process.argv.some(arg=>arg.endsWith('/session-shell/host.cjs'))){
 let ordinal=0;
 const fail=()=>{fs.writeFileSync(process.env.SURVIVAL_SEEN,'injected');throw Error('owned synthetic display failure');};
 Module._load=function(name,...args){const value=load.call(this,name,...args);
  if(name==='@xterm/headless')return {...value,Terminal:new Proxy(value.Terminal,{construct(Base,args){
   const terminal=Reflect.construct(Base,args),index=ordinal++,write=terminal.write;
   terminal.write=function(...args){
    if(fs.existsSync(process.env.SURVIVAL_FAULT)){
     const mode=process.env.SURVIVAL_MODE;
     if(mode==='data'&&index===0||mode==='scratch'&&index>0)fail();
     if(mode==='async'&&index===0)setImmediate(fail);
    }
    return write.apply(this,args);
   };return terminal;
  }})};
  if(name==='node-pty'&&value.native){const resize=value.native.resize;
   value.native.resize=function(...args){if(process.env.SURVIVAL_MODE==='resize'&&fs.existsSync(process.env.SURVIVAL_FAULT))fail();return resize(...args);};
  }
  return value;
 };
}
"""


class SessionShellSurvivalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionShellPTYTest.setUpClass.__func__(cls)

    def run_failure(self,mode):
        inflight=mode.startswith('held-')
        directory=self.root/('survival-'+mode);directory.mkdir(mode=0o700)
        native_file=directory/'native.json';exit_fifo=directory/'exit';os.mkfifo(exit_fifo,0o600)
        pause_fifo=directory/'pause';os.mkfifo(pause_fifo,0o600)
        native=directory/'native.py'
        native.write_text(fixture.FAKE_NATIVE.replace(
            "    with socket.socket(socket.AF_UNIX) as channel:\n        channel.settimeout(5);channel.connect(os.environ['HEARTING_SHELL_SOCKET'])\n        channel.sendall((json.dumps(request)+'\\n').encode());channel.recv(8192)",
            "    try:\n        with socket.socket(socket.AF_UNIX) as channel:\n            channel.settimeout(5);channel.connect(os.environ['HEARTING_SHELL_SOCKET'])\n            channel.sendall((json.dumps(request)+'\\n').encode());channel.recv(8192)\n    except OSError:\n        if not Path(os.environ['SURVIVAL_FAULT']).exists():raise").replace('    data=os.read(0,65536)',
            "    data=os.read(0,65536)\n    with open(os.environ['SURVIVAL_READ'],'ab') as f:f.write(data)").replace('sid=sys.argv[1]',
            "sid=sys.argv[1]\nPath(os.environ['SURVIVAL_PID']).write_text(json.dumps({'pid':os.getpid()}))",1).replace(
            "        if line=='form':", "        if line=='WARM' and os.environ.get('SURVIVAL_PAUSE'):\n            fd=os.open(os.environ['SURVIVAL_PAUSE'],os.O_RDWR);os.read(fd,1);os.close(fd)\n        if line=='form':"))
        preload=directory/'fault.cjs';preload.write_text(PRELOAD)
        env=dict(self.env,LOOP_NATIVE_EXIT=str(exit_fifo),SURVIVAL_PID=str(native_file),SURVIVAL_READ=str(directory/'native-read'),TERM='xterm-256color',
                 NODE_OPTIONS='--require='+str(preload),SURVIVAL_MODE=mode,
                 SURVIVAL_FAULT=str(directory/'fail-now'),SURVIVAL_SEEN=str(directory/'seen'))
        if inflight:env['SURVIVAL_PAUSE']=str(pause_fifo)
        policy=Path(env.get('XDG_CONFIG_HOME') or Path(env['HOME'])/'.config')/'hearting/session-shell.json'
        policy.parent.mkdir(parents=True,exist_ok=True);policy.write_text('{"enabled":true}');policy.chmod(0o600)
        cache=Path(env['XDG_STATE_HOME'])/'hearting/human-waits/projection.json';cache.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        sid='survival-native-'+mode;key='claude\0'+sid;now=time.time()
        cache.write_text(json.dumps({'schema_version':3,'observed_at':now,'items':[],'by_session':{},
            'total':0,'older':0,'complete':True,'route_status':{key:''},'route_sessions':{key:now}}));cache.chmod(0o600)
        master,slave=pty.openpty();outer_attrs=termios.tcgetattr(slave);success=False;fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',30,100,0,0))
        entry=fixture.subprocess.Popen([sys.executable,str(ROOT/'utilities/session-shell/entry.py'),'claude',sys.executable,str(native),sid],
            env=env,cwd=self.project,stdin=slave,stdout=slave,stderr=slave,start_new_session=True,
            preexec_fn=lambda:fcntl.ioctl(0,termios.TIOCSCTTY,0))
        os.close(slave);raw=bytearray();original=None
        def until(needle):
            deadline=time.monotonic()+5
            while needle not in raw:
                left=deadline-time.monotonic()
                if left<=0:break
                if select.select([master],[],[],left)[0]:
                    try:data=os.read(master,65536)
                    except OSError:break
                    if not data:break
                    raw.extend(data)
            self.assertIn(needle,raw,'original CLI I/O lost: '+repr(bytes(raw[-1200:])))
        try:
            until(b'READY');os.write(master,b'WARM\r');until(b'DONE_WARM')
            original=process(json.loads(native_file.read_text())['pid']);entry_identity=process(entry.pid)
            children=Path('/proc')/str(entry.pid)/'task'/str(entry.pid)/'children'
            hosts=[int(pid) for pid in children.read_text().split() if b'host.cjs' in (Path('/proc')/pid/'cmdline').read_bytes()]
            self.assertEqual(len(hosts),1);host=process(hosts[0])
            held=b'';peer_text=''
            if inflight:
                import session_shell as shell
                with mock.patch.dict(os.environ,env,clear=True):
                    deadline=time.monotonic()+5
                    while True:
                        binding,why=shell.exact_binding('claude',sid)
                        status=shell.control(binding,'status') if binding else {}
                        if status.get('empty'):break
                        self.assertLess(time.monotonic(),deadline,(why,status))
                        select.select([],[],[],.01)
                    peer=shell._peer_message()
                    peer_text,ref=peer.prepare_peer_message('PEER_NO_CALLBACK',
                        {'harness':'codex','session_id':'synthetic-sender'},
                        {'harness':'claude','session_id':sid},defer=True)
                    accepted=shell.deliver_pending(ref,'claude',sid)
                    self.assertTrue(accepted.get('ok'),accepted)
                    held=b'HELD_HUMAN_1388'+b'x'*(65536-len(b'HELD_HUMAN_1388'))
                    os.write(master,held)
                    deadline=time.monotonic()+5
                    while True:
                        status=shell.control(binding,'status')
                        if status.get('held_bytes')==len(held):break
                        self.assertLess(time.monotonic(),deadline,status)
                        select.select([],[],[],.01)
                Path(env['SURVIVAL_FAULT']).write_text('fail')
                if mode=='held-host':self.assertTrue(terminate_owned(host))
                fd=os.open(pause_fifo,os.O_RDWR|os.O_NONBLOCK);os.write(fd,b'1');os.close(fd)
            elif mode in ('host','backend'):
                Path(env['SURVIVAL_FAULT']).write_text('fail')
                victim=host
                if mode=='backend':
                    children=Path('/proc')/str(host['pid'])/'task'/str(host['pid'])/'children'
                    ids=[int(pid) for pid in children.read_text().split() if b'session_shell.py' in (Path('/proc')/pid/'cmdline').read_bytes()]
                    self.assertEqual(len(ids),1);victim=process(ids[0])
                self.assertTrue(terminate_owned(victim))
            else:
                Path(env['SURVIVAL_FAULT']).write_text('fail')
                if mode in ('resize','scratch'):fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',32,100,0,0))
                os.write(master,b'TRIGGER_DISPLAY_FAILURE\r')
            after=('AFTER_HOST_ERROR' if inflight else 'AFTER_HOST_ERROR 한글🙂').encode()
            os.write(master,after+b'\r')
            try:until(after if inflight else b'DONE_'+after)
            except AssertionError:
                try:entry_wait=Path('/proc',str(entry.pid),'wchan').read_text().strip()
                except FileNotFoundError:entry_wait='exited'
                print(json.dumps({'failed_boundary':mode,'entry_code':entry.poll(),
                                  'native':process(original['pid']), 'pid_record':json.loads(native_file.read_text()),
                                  'native_read':(directory/'native-read').read_bytes().decode(errors='replace'),
                                  'entry_wait':entry_wait}),flush=True)
                raise
            self.assertIsNone(entry.poll())
            self.assertIn(after+b'\r',(directory/'native-read').read_bytes())
            self.assertEqual(re.findall(rb'\x1b\[\?2004([hl])',raw)[-1],b'h')
            self.assertEqual(process(original['pid']),original)
            self.assertEqual(json.loads(native_file.read_text())['pid'],original['pid'])
            if inflight:
                self.assertEqual((directory/'native-read').read_bytes(),
                    b'WARM\r\x1b[200~'+peer_text.encode()+b'\x1b[201~\r'+held+after+b'\r')
            elif mode not in ('host','backend'):self.assertTrue(Path(env['SURVIVAL_SEEN']).exists())
            success=True
            print(json.dumps({'boundary':mode,'same_native_pid':original['pid'],'same_birth':original['start'],
                              'after_input_response':True,'operating_signals':0}),flush=True)
        finally:
            fd=os.open(pause_fifo,os.O_RDWR|os.O_NONBLOCK);os.write(fd,b'1');os.close(fd)
            fd=os.open(exit_fifo,os.O_RDWR|os.O_NONBLOCK);os.write(fd,b'q');os.close(fd)
            try:entry.wait(timeout=5)
            except fixture.subprocess.TimeoutExpired:
                terminate_owned(process(entry.pid));entry.wait(timeout=5)
            if original and process(original['pid'])['live']:terminate_owned(original)
            if success:
                self.assertEqual(entry.returncode,0)
                self.assertEqual(termios.tcgetattr(master),outer_attrs)
            os.close(master)

    def test_native_vt_write_failure_preserves_the_same_child(self):self.run_failure('data')
    def test_scratch_vt_failure_preserves_the_same_child(self):self.run_failure('scratch')
    def test_async_display_failure_preserves_the_same_child(self):self.run_failure('async')
    def test_resize_failure_preserves_the_same_child(self):self.run_failure('resize')
    def test_host_exit_preserves_the_same_child(self):self.run_failure('host')
    def test_backend_exit_preserves_the_same_child(self):self.run_failure('backend')
    def test_host_death_retains_paste_enter_and_held_bytes_once_in_order(self):self.run_failure('held-host')
    def test_live_host_drains_the_same_transaction_before_direct_input(self):self.run_failure('held-control')


if __name__=='__main__':unittest.main()
